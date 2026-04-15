import os
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from filelock import AsyncFileLock, Timeout as FileLockTimeout

from db.sqlite_paths import extract_sqlite_file_path
from env_utils import env_float
import quarantine as _quarantine_mod

logger = logging.getLogger(__name__)
_AUTO_FLUSH_PROCESS_LOCK_TIMEOUT_ENV = "RUNTIME_AUTO_FLUSH_PROCESS_LOCK_TIMEOUT_SEC"
_DEFAULT_AUTO_FLUSH_PROCESS_LOCK_TIMEOUT_SEC = 15.0
_REFLECTION_OPEN_LOOP_PATTERNS = (
    re.compile(r"\b(todo|follow up|next step|blocked|pending|need to|action item)\b", re.IGNORECASE),
    re.compile(r"(待办|后续|阻塞|未完成|下一步|需要继续)"),
)
_REFLECTION_LESSON_PATTERNS = (
    re.compile(r"\b(learned|lesson|avoid|should|better to|next time)\b", re.IGNORECASE),
    re.compile(r"(经验|教训|下次|应该|避免|最好)"),
)
_REFLECTION_INVARIANT_PATTERNS = (
    re.compile(r"\b(always|never|must|policy|rule)\b", re.IGNORECASE),
    re.compile(r"(必须|不要|永远|规则|原则)"),
)


class _NullContextManager:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _resolve_auto_flush_process_lock_timeout_sec() -> float:
    return env_float(
        _AUTO_FLUSH_PROCESS_LOCK_TIMEOUT_ENV,
        _DEFAULT_AUTO_FLUSH_PROCESS_LOCK_TIMEOUT_SEC,
        minimum=0.0,
    )


async def _write_quarantine_before_flush(
    *,
    client: Any,
    session_id: Optional[str],
    source: str,
    summary: str,
    gist_text: Optional[str],
    guard_decision: Dict[str, Any],
    content_hash: str,
) -> Tuple[Optional[int], bool]:
    if not _quarantine_mod.QUARANTINE_ENABLED:
        return None, False
    engine = getattr(client, "engine", None)
    if engine is None:
        logger.debug("Skipping quarantine write because client has no engine")
        return None, False
    try:
        quarantine_id = await _quarantine_mod.write_quarantine_record(
            engine=engine,
            session_id=session_id,
            source=source,
            summary=summary,
            gist_text=gist_text,
            trace_text=summary,
            guard_action=str(guard_decision.get("action") or ""),
            guard_method=str(guard_decision.get("method") or ""),
            guard_reason=str(guard_decision.get("reason") or ""),
            guard_target_uri=str(guard_decision.get("target_uri") or ""),
            content_hash=content_hash,
            ttl_hours=_quarantine_mod.QUARANTINE_TTL_HOURS,
        )
    except Exception as exc:
        logger.warning("Quarantine write failed: %s", exc, exc_info=True)
        return None, True
    return quarantine_id, False


def normalize_path_prefix_impl(path_prefix: Optional[str]) -> str:
    raw = str(path_prefix or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return "corrections"
    parts = [part for part in raw.split("/") if part]
    return "/".join(parts) if parts else "corrections"


def safe_non_negative_int_impl(value: Any) -> int:
    try:
        return max(0, int(value))
    except (OverflowError, TypeError, ValueError):
        return 0


def sanitize_import_learn_summary_impl(payload: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    return {
        "window_size": safe_non_negative_int_impl(payload.get("window_size")),
        "total_events": safe_non_negative_int_impl(payload.get("total_events")),
        "event_type_breakdown": payload.get("event_type_breakdown")
        if isinstance(payload.get("event_type_breakdown"), dict)
        else {},
        "operation_breakdown": payload.get("operation_breakdown")
        if isinstance(payload.get("operation_breakdown"), dict)
        else {},
        "decision_breakdown": payload.get("decision_breakdown")
        if isinstance(payload.get("decision_breakdown"), dict)
        else {},
        "rejected_events": safe_non_negative_int_impl(payload.get("rejected_events")),
        "rollback_events": safe_non_negative_int_impl(payload.get("rollback_events")),
        "top_reasons": payload.get("top_reasons")
        if isinstance(payload.get("top_reasons"), list)
        else [],
        "last_event_at": payload.get("last_event_at"),
        "recent_events": payload.get("recent_events")
        if isinstance(payload.get("recent_events"), list)
        else [],
    }


async def load_persisted_import_learn_summary_impl(
    client: Any,
    *,
    import_learn_audit_meta_key: str,
) -> Optional[Dict[str, Any]]:
    get_runtime_meta = getattr(client, "get_runtime_meta", None)
    if not callable(get_runtime_meta):
        return None
    try:
        raw_value = await get_runtime_meta(import_learn_audit_meta_key)
    except Exception:
        return None
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    return sanitize_import_learn_summary_impl(parsed)


def merge_import_learn_summaries_impl(
    runtime_summary: Dict[str, Any], persisted_summary: Dict[str, Any]
) -> Dict[str, Any]:
    merged = dict(runtime_summary)
    if safe_non_negative_int_impl(merged.get("total_events")) <= 0:
        merged["total_events"] = safe_non_negative_int_impl(
            persisted_summary.get("total_events")
        )
    if safe_non_negative_int_impl(merged.get("rejected_events")) <= 0:
        merged["rejected_events"] = safe_non_negative_int_impl(
            persisted_summary.get("rejected_events")
        )
    if safe_non_negative_int_impl(merged.get("rollback_events")) <= 0:
        merged["rollback_events"] = safe_non_negative_int_impl(
            persisted_summary.get("rollback_events")
        )
    if not isinstance(merged.get("event_type_breakdown"), dict) or not merged.get(
        "event_type_breakdown"
    ):
        merged["event_type_breakdown"] = dict(
            persisted_summary.get("event_type_breakdown") or {}
        )
    if not merged.get("last_event_at"):
        merged["last_event_at"] = persisted_summary.get("last_event_at")
    merged["persisted_snapshot"] = persisted_summary
    return merged


async def record_import_learn_event_impl(
    *,
    runtime_state: Any,
    import_learn_meta_persist_lock: Any,
    import_learn_audit_meta_key: str,
    to_json: Callable[[Dict[str, Any]], str],
    get_sqlite_client: Callable[[], Any],
    event_type: str,
    operation: str,
    decision: str,
    reason: str,
    source: str,
    session_id: Optional[str],
    actor_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    persist_runtime_meta: bool = True,
) -> None:
    await runtime_state.import_learn_tracker.record_event(
        event_type=event_type,
        operation=operation,
        decision=decision,
        reason=reason,
        source=source,
        session_id=session_id,
        actor_id=actor_id,
        batch_id=batch_id,
        metadata=metadata,
    )
    if not persist_runtime_meta:
        return
    try:
        client = get_sqlite_client()
        set_runtime_meta = getattr(client, "set_runtime_meta", None)
        if callable(set_runtime_meta):
            async with import_learn_meta_persist_lock:
                summary_payload = await runtime_state.import_learn_tracker.summary()
                await set_runtime_meta(
                    import_learn_audit_meta_key,
                    to_json(summary_payload),
                )
    except Exception as exc:
        logger.warning(
            "Failed to persist import-learn runtime metadata: %s",
            exc,
        )


def build_source_hash_impl(source: str) -> str:
    payload = (source or "").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def safe_segment_impl(value: Optional[str]) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "anonymous"
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if normalized:
        return normalized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"agent-{digest}"


def normalize_text_impl(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def append_uri_path_impl(
    base_uri: str,
    *segments: Optional[str],
    parse_uri: Callable[[str], Tuple[str, str]],
    make_uri: Callable[[str, str], str],
) -> str:
    domain, base_path = parse_uri(base_uri)
    parts = [segment for segment in str(base_path or "").split("/") if segment]
    for entry in segments:
        rendered = str(entry or "").strip()
        if not rendered:
            continue
        parts.extend(part for part in rendered.split("/") if part)
    return make_uri(domain, "/".join(parts))


def build_reflection_uri_impl(
    reflection_root_uri: str,
    agent_key: str,
    session_ref: str,
    source_text: str,
    *,
    parse_uri: Callable[[str], Tuple[str, str]],
    make_uri: Callable[[str, str], str],
) -> str:
    root_uri = append_uri_path_impl(
        reflection_root_uri,
        agent_key,
        parse_uri=parse_uri,
        make_uri=make_uri,
    )
    timestamp_segments = datetime.now(timezone.utc).isoformat()[:10].split("-")
    digest = hashlib.sha256(
        f"{session_ref}:{normalize_text_impl(source_text)}".encode("utf-8")
    ).hexdigest()[:12]
    return append_uri_path_impl(
        root_uri,
        *timestamp_segments,
        f"session-{safe_segment_impl(session_ref)}-{digest}",
        parse_uri=parse_uri,
        make_uri=make_uri,
    )


def bucket_reflection_lines_impl(summary: str) -> Dict[str, List[str]]:
    event: List[str] = []
    invariant: List[str] = []
    derived: List[str] = []
    open_loops: List[str] = []
    lessons: List[str] = []
    lines = [
        re.sub(r"^[-*]\s*", "", line).strip()
        for line in str(summary or "").splitlines()
    ]
    lines = [line for line in lines if line]
    for line in lines:
        if any(pattern.search(line) for pattern in _REFLECTION_OPEN_LOOP_PATTERNS):
            open_loops.append(line)
            continue
        if any(pattern.search(line) for pattern in _REFLECTION_INVARIANT_PATTERNS):
            invariant.append(line)
            continue
        if any(pattern.search(line) for pattern in _REFLECTION_LESSON_PATTERNS):
            lessons.append(line)
            continue
        if len(event) < 3:
            event.append(line)
            continue
        derived.append(line)
    if not event and lines:
        event.append(lines[0])
    return {
        "event": event,
        "invariant": invariant,
        "derived": derived,
        "open_loops": open_loops,
        "lessons": lessons,
    }


def build_reflection_content_impl(
    *,
    source: str,
    summary: str,
    generated_at: str,
    trigger: Optional[str] = None,
    summary_method: Optional[str] = None,
    agent_id: Optional[str] = None,
    session_id: Optional[str] = None,
    session_key: Optional[str] = None,
    compact_source_uri: Optional[str] = None,
    compact_source_hash: Optional[str] = None,
    compact_gist_method: Optional[str] = None,
    message_count: Optional[int] = None,
    turn_count_estimate: Optional[int] = None,
    decay_hint_days: Optional[int] = None,
    retention_class: Optional[str] = None,
) -> str:
    buckets = bucket_reflection_lines_impl(summary)
    lines: List[str] = [
        "# Reflection Lane",
        f"- source: {source}",
        f"- generated_at: {generated_at}",
        f"- trigger: {trigger or source}",
        f"- summary_method: {summary_method or 'message_rollup_v1'}",
    ]
    if agent_id:
        lines.append(f"- agent_id: {agent_id}")
    if session_id:
        lines.append(f"- session_id: {session_id}")
    if session_key:
        lines.append(f"- session_key: {session_key}")
    if compact_source_uri:
        lines.append(f"- compact_source_uri: {compact_source_uri}")
    if compact_source_hash:
        lines.append(f"- compact_source_hash: {compact_source_hash}")
    if compact_gist_method:
        lines.append(f"- compact_gist_method: {compact_gist_method}")
    if message_count is not None:
        lines.append(f"- message_count: {int(message_count)}")
    if turn_count_estimate is not None:
        lines.append(f"- turn_count_estimate: {int(turn_count_estimate)}")
    if decay_hint_days is not None:
        lines.append(f"- decay_hint_days: {int(decay_hint_days)}")
    if retention_class:
        lines.append(f"- retention_class: {retention_class}")

    def _append_bucket(title: str, entries: List[str]) -> None:
        lines.append("")
        lines.append(f"## {title}")
        if entries:
            lines.extend(f"- {item}" for item in entries)
        else:
            lines.append("- (none)")

    _append_bucket("event", buckets["event"])
    _append_bucket("invariant", buckets["invariant"])
    _append_bucket("derived", buckets["derived"])
    _append_bucket("open_loops", buckets["open_loops"])
    _append_bucket("lessons", buckets["lessons"])
    return "\n".join(lines)


async def compact_context_to_reflection_impl(
    *,
    client: Any,
    reason: str,
    force: bool,
    max_lines: int,
    seed_event: Optional[str],
    auto_flush_parent_uri: str,
    reflection_root_uri: str,
    reflection_agent_key: str,
    reflection_session_ref: str,
    reflection_agent_id: Optional[str],
    reflection_session_id: Optional[str],
    reflection_session_key: Optional[str],
    reflection_priority: int,
    reflection_disclosure: str,
    reflection_decay_hint_days: int,
    reflection_retention_class: str,
    runtime_state: Any,
    get_session_id: Callable[[], str],
    utc_now_naive: Callable[[], Any],
    utc_iso_now: Callable[[], str],
    generate_gist: Callable[..., Awaitable[Dict[str, Any]]],
    build_source_hash: Callable[[str], str],
    ensure_parent_path_exists: Callable[..., Awaitable[Tuple[str, str, List[Dict[str, Any]]]]],
    normalize_guard_decision: Callable[..., Dict[str, Any]],
    record_guard_event: Callable[..., Awaitable[None]],
    guard_fields: Callable[[Dict[str, Any]], Dict[str, Any]],
    should_defer_index_on_write: Callable[[], Awaitable[bool]],
    enqueue_index_targets: Callable[..., Awaitable[Dict[str, List[Dict[str, Any]]]]],
    safe_int: Callable[[Any, int], int],
    parse_uri: Callable[[str], Tuple[str, str]],
    make_uri: Callable[[str, str], str],
    record_session_hit: Callable[..., Awaitable[None]],
) -> Dict[str, Any]:
    session_id = str(get_session_id() or "").strip() or "default"
    async def _record_flush_result(
        *,
        result_reason: str,
        flushed: bool,
        data_persisted: bool,
        source_hash: Optional[str] = None,
        trigger_reason: Optional[str] = None,
    ) -> None:
        recorder = getattr(runtime_state.flush_tracker, "note_flush_result", None)
        if not callable(recorder):
            return
        try:
            await recorder(
                session_id=session_id,
                source="compact_context_reflection",
                trigger_reason=("force" if force else trigger_reason),
                flushed=flushed,
                data_persisted=data_persisted,
                result_reason=result_reason,
                source_hash=source_hash,
            )
        except Exception:
            pass
    seed_event_text = str(seed_event or "").strip()
    if seed_event_text:
        await runtime_state.flush_tracker.record_event(
            session_id=session_id,
            message=seed_event_text,
        )
    should_flush = force or await runtime_state.flush_tracker.should_flush(
        session_id=session_id
    )
    if not should_flush:
        return {
            "flushed": False,
            "reason": "threshold_not_reached",
            "data_persisted": False,
            "reflection_written": False,
        }

    summary = await runtime_state.flush_tracker.build_summary(
        session_id=session_id, limit=max(1, max_lines)
    )
    if not summary.strip():
        await _record_flush_result(
            result_reason="no_pending_events",
            flushed=False,
            data_persisted=False,
        )
        return {
            "flushed": False,
            "reason": "no_pending_events",
            "data_persisted": False,
            "reflection_written": False,
        }

    gist_payload = await generate_gist(summary, client=client)
    gist_text = str(gist_payload.get("gist_text") or "").strip()
    gist_method = str(gist_payload.get("gist_method") or "truncate_fallback")
    quality_value = gist_payload.get("quality")
    try:
        gist_quality = float(quality_value)
    except (TypeError, ValueError):
        gist_quality = 0.0
    source_hash = build_source_hash(summary)

    compact_guard_domain, compact_guard_parent_path, _ = await ensure_parent_path_exists(
        client,
        auto_flush_parent_uri,
    )
    compact_preview_content = (
        f"# Runtime Session Flush\n"
        f"- session_id: {session_id}\n"
        f"- reason: {reason}\n"
        f"- flushed_at: {utc_iso_now()}\n"
        f"- gist_method: {gist_method}\n"
        f"- quality: {round(gist_quality, 3)}\n"
        f"- source_hash: {source_hash}\n\n"
        f"## Gist\n"
        f"{gist_text or '(gist unavailable)'}\n\n"
        f"## Trace\n"
        f"{summary}"
    )
    guard_decision = normalize_guard_decision(
        {"action": "ADD", "method": "none", "reason": "guard_not_evaluated"}
    )
    try:
        guard_decision = normalize_guard_decision(
            await client.write_guard(
                content=compact_preview_content,
                domain=compact_guard_domain,
                path_prefix=compact_guard_parent_path if compact_guard_parent_path else None,
            )
        )
    except Exception as guard_exc:
        guard_decision = normalize_guard_decision(
            {
                "action": "NOOP",
                "method": "exception",
                "reason": f"write_guard_unavailable: {guard_exc}",
                "degraded": True,
                "degrade_reasons": ["write_guard_exception"],
            }
        )
    guard_action = str(guard_decision.get("action") or "NOOP").upper()
    guard_blocked = guard_action != "ADD"
    try:
        await record_guard_event(
            operation="compact_context_reflection",
            decision=guard_decision,
            blocked=guard_blocked,
        )
    except Exception as exc:
        logger.warning(
            "Failed to persist import-learn runtime metadata: %s",
            exc,
            exc_info=True,
        )
    if guard_blocked:
        guard_is_degraded = bool(guard_decision.get("degraded"))
        guard_reason = str(guard_decision.get("reason") or "")
        guard_is_invalid = guard_reason.startswith("invalid_guard_action:")
        if (
            guard_action in {"NOOP", "UPDATE"}
            and not guard_is_degraded
            and not guard_is_invalid
        ):
            quarantine_id, quarantine_failed = await _write_quarantine_before_flush(
                client=client,
                session_id=session_id,
                source="compact_context_reflection",
                summary=summary,
                gist_text=gist_text,
                guard_decision=guard_decision,
                content_hash=source_hash,
            )
            if quarantine_failed:
                await _record_flush_result(
                    result_reason="quarantine_write_failed",
                    flushed=False,
                    data_persisted=False,
                    source_hash=source_hash,
                )
                payload: Dict[str, Any] = {
                    "flushed": False,
                    "reason": "quarantine_write_failed",
                    "data_persisted": False,
                    "reflection_written": False,
                    "quarantined": False,
                    "degraded": True,
                    "degrade_reasons": ["quarantine_write_failed"],
                    **guard_fields(guard_decision),
                }
                return payload
            await runtime_state.flush_tracker.mark_flushed(session_id=session_id)
            await _record_flush_result(
                result_reason="write_guard_deduped",
                flushed=True,
                data_persisted=False,
                source_hash=source_hash,
            )
            payload: Dict[str, Any] = {
                "flushed": True,
                "reason": "write_guard_deduped",
                "data_persisted": False,
                "reflection_written": False,
                "quarantined": quarantine_id is not None,
                **guard_fields(guard_decision),
            }
            if quarantine_id is not None:
                payload["quarantine_id"] = quarantine_id
            guard_target_uri = guard_decision.get("target_uri")
            if isinstance(guard_target_uri, str) and guard_target_uri.strip():
                payload["uri"] = guard_target_uri
            return payload

        payload = {
            "flushed": False,
            "reason": "write_guard_blocked",
            "data_persisted": False,
            "reflection_written": False,
            **guard_fields(guard_decision),
        }
        await _record_flush_result(
            result_reason="write_guard_blocked",
            flushed=False,
            data_persisted=False,
            source_hash=source_hash,
        )
        if bool(guard_decision.get("degraded")):
            payload["degraded"] = True
            degrade_reasons = guard_decision.get("degrade_reasons")
            if isinstance(degrade_reasons, list) and degrade_reasons:
                payload["degrade_reasons"] = list(dict.fromkeys(degrade_reasons))
        return payload

    summary_text = summary.strip()
    reflection_uri = build_reflection_uri_impl(
        reflection_root_uri,
        safe_segment_impl(reflection_agent_key),
        reflection_session_ref,
        summary_text,
        parse_uri=parse_uri,
        make_uri=make_uri,
    )
    reflection_content = build_reflection_content_impl(
        source="compact_context",
        trigger="compact_context",
        summary_method="compact_context_trace_v1",
        generated_at=utc_iso_now(),
        agent_id=reflection_agent_id,
        session_id=reflection_session_id,
        session_key=reflection_session_key,
        compact_source_hash=source_hash,
        compact_gist_method=gist_method,
        decay_hint_days=reflection_decay_hint_days,
        retention_class=reflection_retention_class,
        summary=summary_text,
    )
    reflection_domain, reflection_path = parse_uri(reflection_uri)
    reflection_parent_path, _, reflection_title = reflection_path.rpartition("/")
    await ensure_parent_path_exists(
        client,
        make_uri(reflection_domain, reflection_parent_path),
    )
    defer_index = await should_defer_index_on_write()
    reflection_created = False
    reflection_merged = False
    reflection_memory_id: Optional[int] = None
    index_targets: List[int] = []
    existing = await client.get_memory_by_path(reflection_path, reflection_domain)
    if existing:
        existing_content = str(existing.get("content") or "")
        reflection_memory_id = safe_int(existing.get("id"), default=0) or None
        if reflection_content.strip() not in existing_content:
            merged_content = (
                f"{existing_content}\n\n---\n\n{reflection_content}"
                if existing_content.strip()
                else reflection_content
            )
            update_result = await client.update_memory(
                reflection_path,
                content=merged_content,
                domain=reflection_domain,
                index_now=not defer_index,
                expected_old_id=safe_int(existing.get("id"), default=0) or None,
            )
            reflection_merged = True
            reflection_memory_id = safe_int(
                update_result.get("new_memory_id"),
                default=safe_int(existing.get("id"), default=0),
            ) or reflection_memory_id
            index_targets = [
                safe_int(item, default=0)
                for item in list(update_result.get("index_targets") or [])
                if safe_int(item, default=0) > 0
            ]
    else:
        create_result = await client.create_memory(
            parent_path=reflection_parent_path,
            content=reflection_content,
            priority=reflection_priority,
            title=reflection_title or None,
            disclosure=reflection_disclosure,
            domain=reflection_domain,
            index_now=not defer_index,
        )
        reflection_created = True
        reflection_memory_id = safe_int(create_result.get("id"), default=0) or None
        index_targets = [
            safe_int(item, default=0)
            for item in list(create_result.get("index_targets") or [])
            if safe_int(item, default=0) > 0
        ]

    index_enqueue = {"queued": [], "dropped": [], "deduped": []}
    if defer_index and index_targets:
        index_enqueue = await enqueue_index_targets(
            {"index_targets": index_targets},
            reason="compact_context_reflection",
        )

    upsert_gist = getattr(client, "upsert_memory_gist", None)
    gist_persisted = False
    gist_store_error: Optional[str] = None
    if callable(upsert_gist) and reflection_memory_id:
        try:
            await upsert_gist(
                memory_id=reflection_memory_id,
                gist_text=gist_text or summary_text,
                source_hash=source_hash,
                gist_method=gist_method,
                quality_score=gist_quality,
            )
            gist_persisted = True
        except Exception as exc:
            gist_store_error = str(exc)

    await runtime_state.flush_tracker.mark_flushed(session_id=session_id)
    await _record_flush_result(
        result_reason="stored",
        flushed=True,
        data_persisted=True,
        source_hash=source_hash,
    )
    await record_session_hit(
        uri=reflection_uri,
        memory_id=reflection_memory_id,
        snippet=reflection_content[:300],
        priority=reflection_priority,
        source="compact_context_reflection",
        updated_at=utc_iso_now(),
    )

    payload: Dict[str, Any] = {
        "flushed": True,
        "uri": reflection_uri,
        "reflection_uri": reflection_uri,
        "reflection_written": True,
        "reflection_created": reflection_created,
        "reflection_merged": reflection_merged,
        "data_persisted": True,
        "committed_directly_to_reflection": True,
        **guard_fields(guard_decision),
        "gist_method": gist_method,
        "gist_text": gist_text or summary_text,
        "quality": round(gist_quality, 3),
        "source_hash": source_hash,
        "trace_text": summary_text,
        "gist_persisted": gist_persisted,
        "index_queued": len(index_enqueue["queued"]),
        "index_dropped": len(index_enqueue["dropped"]),
        "index_deduped": len(index_enqueue["deduped"]),
    }
    gist_degrade_reasons = gist_payload.get("degrade_reasons")
    if isinstance(gist_degrade_reasons, list):
        payload["degrade_reasons"] = [
            str(item).strip()
            for item in gist_degrade_reasons
            if isinstance(item, str) and item.strip()
        ]
    if gist_store_error:
        payload["gist_store_error"] = gist_store_error
    if index_enqueue["dropped"]:
        degrade_reasons = payload.setdefault("degrade_reasons", [])
        if "index_enqueue_dropped" not in degrade_reasons:
            degrade_reasons.append("index_enqueue_dropped")
    return payload


async def ensure_parent_path_exists_impl(
    client: Any,
    parent_uri: str,
    *,
    parse_uri: Callable[[str], Tuple[str, str]],
    make_uri: Callable[[str, str], str],
    auto_flush_priority: int,
    safe_int: Callable[[Any, int], int],
) -> Tuple[str, str, List[Dict[str, Any]]]:
    domain, parent_path = parse_uri(parent_uri)
    if not parent_path:
        return domain, parent_path, []

    segments = [segment for segment in parent_path.split("/") if segment]
    current_path = ""
    created_nodes: List[Dict[str, Any]] = []
    for segment in segments:
        next_path = f"{current_path}/{segment}" if current_path else segment
        exists = await client.get_memory_by_path(next_path, domain)
        if not exists:
            created = await client.create_memory(
                parent_path=current_path,
                content=f"[runtime] auto-created flush namespace: {make_uri(domain, next_path)}",
                priority=max(1, auto_flush_priority),
                title=segment,
                disclosure="Runtime flush namespace",
                domain=domain,
            )
            created_nodes.append(
                {
                    "memory_id": safe_int(created.get("id"), default=0),
                    "domain": domain,
                    "path": next_path,
                    "uri": make_uri(domain, next_path),
                }
            )
        current_path = next_path
    return domain, parent_path, created_nodes


async def generate_gist_impl(
    summary: str,
    *,
    client: Any = None,
    max_points: int = 3,
    max_chars: int = 280,
    trim_sentence: Callable[[str, int], str],
) -> Dict[str, Any]:
    source = (summary or "").strip()
    if not source:
        return {"gist_text": "", "gist_method": "empty", "quality": 0.0}

    degrade_reasons: List[str] = []
    llm_gist_builder = getattr(client, "generate_compact_gist", None) if client else None
    if callable(llm_gist_builder):
        try:
            llm_payload = await llm_gist_builder(
                summary=source,
                max_points=max_points,
                max_chars=max_chars,
                degrade_reasons=degrade_reasons,
            )
            if isinstance(llm_payload, dict):
                llm_gist_text = str(llm_payload.get("gist_text") or "").strip()
                if llm_gist_text:
                    quality_value = llm_payload.get("quality")
                    try:
                        quality = float(quality_value)
                    except (TypeError, ValueError):
                        quality = 0.72
                    payload = {
                        "gist_text": llm_gist_text,
                        "gist_method": str(llm_payload.get("gist_method") or "llm_gist"),
                        "quality": round(max(0.0, min(1.0, quality)), 3),
                    }
                    if degrade_reasons:
                        payload["degrade_reasons"] = list(dict.fromkeys(degrade_reasons))
                    return payload
                degrade_reasons.append("compact_gist_llm_empty")
        except Exception as exc:
            degrade_reasons.append(f"compact_gist_llm_exception:{type(exc).__name__}")

    bullet_lines: List[str] = []
    for line in source.splitlines():
        line_value = line.strip()
        if not line_value:
            continue
        if line_value.startswith("Session compaction notes:"):
            continue
        if line_value.startswith("- "):
            bullet_lines.append(line_value[2:].strip())
        else:
            bullet_lines.append(line_value)

    extractive_parts: List[str] = []
    for line in bullet_lines:
        if not line:
            continue
        extractive_parts.append(trim_sentence(line, 90))
        if len(extractive_parts) >= max(1, max_points):
            break

    extractive_gist = "; ".join(part for part in extractive_parts if part)
    if extractive_gist:
        gist_text = extractive_gist[: max(40, max_chars)].strip()
        quality = min(0.95, max(0.45, len(gist_text) / max(120.0, len(source) * 0.8)))
        payload = {
            "gist_text": gist_text,
            "gist_method": "extractive_bullets",
            "quality": round(float(quality), 3),
        }
        if degrade_reasons:
            payload["degrade_reasons"] = list(dict.fromkeys(degrade_reasons))
        return payload

    flattened = re.sub(r"\s+", " ", source)
    sentences = [item.strip() for item in re.split(r"(?<=[.!?。！？])\s+", flattened) if item.strip()]
    if sentences:
        gist_text = trim_sentence(sentences[0], max(48, max_chars))
        quality = 0.4 if len(sentences) == 1 else 0.52
        payload = {
            "gist_text": gist_text,
            "gist_method": "sentence_fallback",
            "quality": round(float(quality), 3),
        }
        if degrade_reasons:
            payload["degrade_reasons"] = list(dict.fromkeys(degrade_reasons))
        return payload

    gist_text = trim_sentence(flattened, max(32, max_chars))
    payload = {
        "gist_text": gist_text,
        "gist_method": "truncate_fallback",
        "quality": 0.3,
    }
    if degrade_reasons:
        payload["degrade_reasons"] = list(dict.fromkeys(degrade_reasons))
    return payload


async def flush_session_summary_to_memory_impl(
    *,
    client: Any,
    source: str,
    reason: str,
    force: bool,
    max_lines: int,
    session_id: Optional[str] = None,
    runtime_state: Any,
    get_session_id: Callable[[], str],
    auto_flush_parent_uri: str,
    auto_flush_priority: int,
    utc_now_naive: Callable[[], Any],
    utc_iso_now: Callable[[], str],
    generate_gist: Callable[..., Awaitable[Dict[str, Any]]],
    build_source_hash: Callable[[str], str],
    ensure_parent_path_exists: Callable[..., Awaitable[Tuple[str, str, List[Dict[str, Any]]]]],
    normalize_guard_decision: Callable[..., Dict[str, Any]],
    record_guard_event: Callable[..., Awaitable[None]],
    guard_fields: Callable[[Dict[str, Any]], Dict[str, Any]],
    should_defer_index_on_write: Callable[[], Awaitable[bool]],
    enqueue_index_targets: Callable[..., Awaitable[Dict[str, List[Dict[str, Any]]]]],
    safe_int: Callable[[Any, int], int],
    make_uri: Callable[[str, str], str],
    record_session_hit: Callable[..., Awaitable[None]],
    get_sqlite_client: Callable[[], Any],
) -> Dict[str, Any]:
    session_id = str(session_id or get_session_id() or "").strip() or "default"
    async def _record_flush_result(
        *,
        result_reason: str,
        flushed: bool,
        data_persisted: bool,
        source_hash: Optional[str] = None,
        trigger_reason: Optional[str] = None,
    ) -> None:
        recorder = getattr(runtime_state.flush_tracker, "note_flush_result", None)
        if not callable(recorder):
            return
        try:
            await recorder(
                session_id=session_id,
                source=source,
                trigger_reason=("force" if force else trigger_reason),
                flushed=flushed,
                data_persisted=data_persisted,
                result_reason=result_reason,
                source_hash=source_hash,
            )
        except Exception:
            pass
    should_flush = force or await runtime_state.flush_tracker.should_flush(
        session_id=session_id
    )
    if not should_flush:
        return {
            "flushed": False,
            "reason": "threshold_not_reached",
            "data_persisted": False,
        }

    summary = await runtime_state.flush_tracker.build_summary(
        session_id=session_id, limit=max(1, max_lines)
    )
    if not summary.strip():
        await _record_flush_result(
            result_reason="no_pending_events",
            flushed=False,
            data_persisted=False,
        )
        return {
            "flushed": False,
            "reason": "no_pending_events",
            "data_persisted": False,
        }

    gist_payload = await generate_gist(summary, client=client)
    gist_text = str(gist_payload.get("gist_text") or "").strip()
    gist_method = str(gist_payload.get("gist_method") or "truncate_fallback")
    quality_value = gist_payload.get("quality")
    try:
        gist_quality = float(quality_value)
    except (TypeError, ValueError):
        gist_quality = 0.0
    source_hash = build_source_hash(summary)

    domain, parent_path, _ = await ensure_parent_path_exists(
        client,
        auto_flush_parent_uri,
    )
    flush_title = f"auto_flush_{utc_now_naive().strftime('%Y%m%d_%H%M%S')}"
    content = (
        f"# Runtime Session Flush\n"
        f"- session_id: {session_id}\n"
        f"- reason: {reason}\n"
        f"- flushed_at: {utc_iso_now()}\n"
        f"- gist_method: {gist_method}\n"
        f"- quality: {round(gist_quality, 3)}\n"
        f"- source_hash: {source_hash}\n\n"
        f"## Gist\n"
        f"{gist_text or '(gist unavailable)'}\n\n"
        f"## Trace\n"
        f"{summary}"
    )
    guard_decision = normalize_guard_decision(
        {"action": "ADD", "method": "none", "reason": "guard_not_evaluated"}
    )
    try:
        guard_decision = normalize_guard_decision(
            await client.write_guard(
                content=content,
                domain=domain,
                path_prefix=parent_path if parent_path else None,
            )
        )
    except Exception as guard_exc:
        guard_decision = normalize_guard_decision(
            {
                "action": "NOOP",
                "method": "exception",
                "reason": f"write_guard_unavailable: {guard_exc}",
                "degraded": True,
                "degrade_reasons": ["write_guard_exception"],
            }
        )
    guard_action = str(guard_decision.get("action") or "NOOP").upper()
    guard_blocked = guard_action != "ADD"
    try:
        await record_guard_event(
            operation="compact_context",
            decision=guard_decision,
            blocked=guard_blocked,
        )
    except Exception as exc:
        logger.warning(
            "Failed to persist import-learn runtime metadata: %s",
            exc,
            exc_info=True,
        )
    if guard_blocked:
        guard_is_degraded = bool(guard_decision.get("degraded"))
        guard_reason = str(guard_decision.get("reason") or "")
        guard_is_invalid = guard_reason.startswith("invalid_guard_action:")
        if (
            guard_action in {"NOOP", "UPDATE"}
            and not guard_is_degraded
            and not guard_is_invalid
        ):
            quarantine_id, quarantine_failed = await _write_quarantine_before_flush(
                client=client,
                session_id=session_id,
                source="compact_context",
                summary=summary,
                gist_text=gist_text,
                guard_decision=guard_decision,
                content_hash=source_hash,
            )
            if quarantine_failed:
                await _record_flush_result(
                    result_reason="quarantine_write_failed",
                    flushed=False,
                    data_persisted=False,
                    source_hash=source_hash,
                )
                payload: Dict[str, Any] = {
                    "flushed": False,
                    "reason": "quarantine_write_failed",
                    "data_persisted": False,
                    "quarantined": False,
                    "degraded": True,
                    "degrade_reasons": ["quarantine_write_failed"],
                    **guard_fields(guard_decision),
                }
                return payload
            await runtime_state.flush_tracker.mark_flushed(session_id=session_id)
            await _record_flush_result(
                result_reason="write_guard_deduped",
                flushed=True,
                data_persisted=False,
                source_hash=source_hash,
            )
            payload: Dict[str, Any] = {
                "flushed": True,
                "reason": "write_guard_deduped",
                "data_persisted": False,
                "quarantined": quarantine_id is not None,
                **guard_fields(guard_decision),
            }
            if quarantine_id is not None:
                payload["quarantine_id"] = quarantine_id
            guard_target_uri = guard_decision.get("target_uri")
            if isinstance(guard_target_uri, str) and guard_target_uri.strip():
                payload["uri"] = guard_target_uri
            return payload

        payload = {
            "flushed": False,
            "reason": "write_guard_blocked",
            "data_persisted": False,
            **guard_fields(guard_decision),
        }
        await _record_flush_result(
            result_reason="write_guard_blocked",
            flushed=False,
            data_persisted=False,
            source_hash=source_hash,
        )
        if bool(guard_decision.get("degraded")):
            payload["degraded"] = True
            degrade_reasons = guard_decision.get("degrade_reasons")
            if isinstance(degrade_reasons, list) and degrade_reasons:
                payload["degrade_reasons"] = list(dict.fromkeys(degrade_reasons))
        return payload

    defer_index = await should_defer_index_on_write()
    result = await client.create_memory(
        parent_path=parent_path,
        content=content,
        priority=auto_flush_priority,
        title=flush_title,
        disclosure="Runtime auto flush summary",
        domain=domain,
        index_now=not defer_index,
    )
    index_enqueue = {"queued": [], "dropped": [], "deduped": []}
    if defer_index:
        index_enqueue = await enqueue_index_targets(result, reason="compact_context")
    created_memory_id = safe_int(result.get("id"), default=-1)
    gist_persisted = False
    gist_store_error: Optional[str] = None
    upsert_gist = getattr(client, "upsert_memory_gist", None)
    if callable(upsert_gist) and created_memory_id > 0:
        try:
            await upsert_gist(
                memory_id=created_memory_id,
                gist_text=gist_text or summary,
                source_hash=source_hash,
                gist_method=gist_method,
                quality_score=gist_quality,
            )
            gist_persisted = True
        except Exception as exc:
            gist_store_error = str(exc)
    await runtime_state.flush_tracker.mark_flushed(session_id=session_id)
    await _record_flush_result(
        result_reason="stored",
        flushed=True,
        data_persisted=True,
        source_hash=source_hash,
    )

    created_uri = result.get("uri", make_uri(domain, result.get("path", flush_title)))
    await record_session_hit(
        uri=created_uri,
        memory_id=result.get("id"),
        snippet=content[:300],
        priority=auto_flush_priority,
        source="auto_flush",
        updated_at=utc_iso_now(),
    )
    payload: Dict[str, Any] = {
        "flushed": True,
        "uri": created_uri,
        "data_persisted": True,
        **guard_fields(guard_decision),
        "gist_method": gist_method,
        "gist_text": gist_text or summary,
        "quality": round(gist_quality, 3),
        "source_hash": source_hash,
        "trace_text": summary,
        "gist_persisted": gist_persisted,
        "index_queued": len(index_enqueue["queued"]),
        "index_dropped": len(index_enqueue["dropped"]),
        "index_deduped": len(index_enqueue["deduped"]),
    }
    gist_degrade_reasons = gist_payload.get("degrade_reasons")
    if isinstance(gist_degrade_reasons, list):
        payload["degrade_reasons"] = [
            str(reason).strip()
            for reason in gist_degrade_reasons
            if isinstance(reason, str) and reason.strip()
        ]
    if gist_store_error:
        payload["gist_store_error"] = gist_store_error
    if index_enqueue["dropped"]:
        degrade_reasons = payload.setdefault("degrade_reasons", [])
        if "index_enqueue_dropped" not in degrade_reasons:
            degrade_reasons.append("index_enqueue_dropped")
    try:
        await runtime_state.promotion_tracker.record_event(
            session_id=session_id,
            source=source,
            trigger_reason=reason,
            uri=str(created_uri),
            memory_id=created_memory_id if created_memory_id > 0 else None,
            gist_method=gist_method,
            quality=gist_quality,
            degraded=bool(payload.get("degrade_reasons")) or bool(gist_store_error),
            degrade_reasons=payload.get("degrade_reasons"),
            index_queued=payload.get("index_queued", 0),
            index_dropped=payload.get("index_dropped", 0),
            index_deduped=payload.get("index_deduped", 0),
        )
    except Exception:
        pass
    return payload


async def maybe_auto_flush_impl(
    client: Any,
    *,
    reason: str,
    auto_flush_enabled: bool,
    get_session_id: Callable[[], str],
    auto_flush_in_progress: set[str],
    auto_flush_in_progress_guard: Optional[Any] = None,
    flush_session_summary_to_memory: Callable[..., Awaitable[Dict[str, Any]]],
    auto_flush_summary_lines: int,
) -> Optional[Dict[str, Any]]:
    def _resolve_auto_flush_process_lock_path() -> Optional[Path]:
        database_url = getattr(client, "database_url", None) or os.getenv("DATABASE_URL")
        database_path = extract_sqlite_file_path(database_url)
        if database_path is None:
            return None
        session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
        return database_path.with_name(
            f"{database_path.name}.auto_flush.{session_hash}.lock"
        )

    if not auto_flush_enabled:
        return None
    session_id = get_session_id()
    with auto_flush_in_progress_guard or _NullContextManager():
        if session_id in auto_flush_in_progress:
            return None
        auto_flush_in_progress.add(session_id)
    try:
        lock_path = _resolve_auto_flush_process_lock_path()
        if lock_path is None:
            return await flush_session_summary_to_memory(
                client=client,
                source="auto_flush",
                reason=reason,
                force=False,
                max_lines=auto_flush_summary_lines,
            )

        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with AsyncFileLock(
                str(lock_path),
                timeout=_resolve_auto_flush_process_lock_timeout_sec(),
            ):
                return await flush_session_summary_to_memory(
                    client=client,
                    source="auto_flush",
                    reason=reason,
                    force=False,
                    max_lines=auto_flush_summary_lines,
                )
        except FileLockTimeout:
            logger.warning(
                "Skipped auto-flush for session %s because the process lock is busy.",
                session_id,
            )
            return None
    finally:
        with auto_flush_in_progress_guard or _NullContextManager():
            auto_flush_in_progress.discard(session_id)


async def run_explicit_learn_service_impl(
    *,
    content: str,
    source: str,
    reason: Optional[str],
    session_id: str,
    actor_id: Optional[str] = None,
    domain: str = "notes",
    path_prefix: str = "corrections",
    execute: bool = False,
    client: Optional[Any] = None,
    auto_learn_explicit_enabled: Callable[[], bool],
    auto_learn_require_reason: Callable[[], bool],
    auto_learn_allowed_domains: Callable[[], List[str]],
    normalize_path_prefix: Callable[[Optional[str]], str],
    make_uri: Callable[[str, str], str],
    get_sqlite_client: Callable[[], Any],
    normalize_guard_decision: Callable[..., Dict[str, Any]],
    guard_fields: Callable[[Dict[str, Any]], Dict[str, Any]],
    record_import_learn_event: Callable[..., Awaitable[None]],
    build_source_hash: Callable[[str], str],
    ensure_parent_path_exists: Callable[..., Awaitable[Tuple[str, str, List[Dict[str, Any]]]]],
    auto_flush_priority: int,
    safe_int: Callable[[Any, int], int],
) -> Dict[str, Any]:
    operation = "learn_explicit"
    normalized_source = str(source or "").strip()
    normalized_reason = str(reason or "").strip()
    normalized_session_id = str(session_id or "").strip()
    normalized_actor_id = (str(actor_id).strip() if actor_id is not None else None) or None
    normalized_domain = str(domain or "").strip().lower() or "notes"
    normalized_path_prefix = normalize_path_prefix(path_prefix)
    target_parent_path = f"{normalized_path_prefix}/{normalized_session_id}".strip("/")
    target_parent_uri = make_uri(normalized_domain, target_parent_path)

    payload: Dict[str, Any] = {
        "ok": True,
        "accepted": False,
        "operation": operation,
        "source": normalized_source,
        "reason_text": normalized_reason,
        "session_id": normalized_session_id,
        "actor_id": normalized_actor_id,
        "domain": normalized_domain,
        "path_prefix": normalized_path_prefix,
        "target_parent_uri": target_parent_uri,
        "execute": bool(execute),
        "reason": "rejected",
    }

    if not auto_learn_explicit_enabled():
        payload["reason"] = "auto_learn_explicit_disabled"
        await record_import_learn_event(
            event_type="reject",
            operation=operation,
            decision="rejected",
            reason="auto_learn_explicit_disabled",
            source=normalized_source or "learn",
            session_id=normalized_session_id,
            actor_id=normalized_actor_id,
            metadata={"domain": normalized_domain},
            persist_runtime_meta=False,
        )
        return payload

    if not normalized_source:
        payload["reason"] = "source_required"
        await record_import_learn_event(
            event_type="reject",
            operation=operation,
            decision="rejected",
            reason="source_required",
            source="learn",
            session_id=normalized_session_id,
            actor_id=normalized_actor_id,
        )
        return payload

    if auto_learn_require_reason() and not normalized_reason:
        payload["reason"] = "reason_required"
        await record_import_learn_event(
            event_type="reject",
            operation=operation,
            decision="rejected",
            reason="reason_required",
            source=normalized_source,
            session_id=normalized_session_id,
            actor_id=normalized_actor_id,
        )
        return payload

    if not normalized_session_id:
        payload["reason"] = "session_id_required"
        await record_import_learn_event(
            event_type="reject",
            operation=operation,
            decision="rejected",
            reason="session_id_required",
            source=normalized_source,
            session_id=None,
            actor_id=normalized_actor_id,
        )
        return payload

    normalized_content = str(content or "").strip()
    if not normalized_content:
        payload["reason"] = "content_required"
        await record_import_learn_event(
            event_type="reject",
            operation=operation,
            decision="rejected",
            reason="content_required",
            source=normalized_source,
            session_id=normalized_session_id,
            actor_id=normalized_actor_id,
        )
        return payload

    allowed_domains = auto_learn_allowed_domains()
    payload["allowed_domains"] = allowed_domains
    if normalized_domain not in allowed_domains:
        payload["reason"] = "domain_not_allowed"
        await record_import_learn_event(
            event_type="reject",
            operation=operation,
            decision="rejected",
            reason="domain_not_allowed",
            source=normalized_source,
            session_id=normalized_session_id,
            actor_id=normalized_actor_id,
            metadata={"domain": normalized_domain, "allowed_domains": allowed_domains},
        )
        return payload

    learn_client = client or get_sqlite_client()
    try:
        guard_decision = normalize_guard_decision(
            await learn_client.write_guard(
                content=normalized_content,
                domain=normalized_domain,
                path_prefix=target_parent_path,
            )
        )
    except Exception as guard_exc:
        payload["reason"] = "write_guard_unavailable"
        payload.update(
            {
                "guard_action": "ERROR",
                "guard_reason": f"write_guard_unavailable:{type(guard_exc).__name__}",
                "guard_method": "exception",
                "guard_target_id": None,
                "guard_target_uri": None,
                "degraded": True,
                "degrade_reasons": ["write_guard_exception"],
            }
        )
        await record_import_learn_event(
            event_type="reject",
            operation=operation,
            decision="rejected",
            reason="write_guard_unavailable",
            source=normalized_source,
            session_id=normalized_session_id,
            actor_id=normalized_actor_id,
            metadata={
                "domain": normalized_domain,
                "guard_error": type(guard_exc).__name__,
            },
        )
        return payload

    payload.update(guard_fields(guard_decision))
    guard_action = str(guard_decision.get("action") or "ADD").upper()
    if guard_action != "ADD":
        payload["reason"] = f"write_guard_blocked:{guard_action.lower()}"
        await record_import_learn_event(
            event_type="reject",
            operation=operation,
            decision="rejected",
            reason=payload["reason"],
            source=normalized_source,
            session_id=normalized_session_id,
            actor_id=normalized_actor_id,
            metadata={
                "guard_action": guard_action,
                "guard_method": guard_decision.get("method"),
            },
        )
        return payload

    payload["accepted"] = True
    payload["reason"] = "prepared"
    source_hash = build_source_hash(
        f"{normalized_source}\n{normalized_reason}\n{normalized_content}"
    )
    payload["source_hash"] = source_hash

    safe_session_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", normalized_session_id).strip("-")
    if not safe_session_id:
        safe_session_id = "session"
    batch_id = f"learn-{safe_session_id[:24]}-{source_hash[:8]}-{uuid.uuid4().hex[:6]}"
    payload["batch_id"] = batch_id

    if not execute:
        await record_import_learn_event(
            event_type="learn",
            operation=operation,
            decision="accepted",
            reason="prepared",
            source=normalized_source,
            session_id=normalized_session_id,
            actor_id=normalized_actor_id,
            batch_id=batch_id,
            metadata={
                "domain": normalized_domain,
                "path_prefix": normalized_path_prefix,
                "target_parent_uri": target_parent_uri,
            },
        )
        return payload

    learn_title = f"learn-{source_hash[:8]}-{uuid.uuid4().hex[:8]}"
    created_namespace_memories: List[Dict[str, Any]] = []
    try:
        (
            created_domain,
            created_parent_path,
            created_namespace_memories,
        ) = await ensure_parent_path_exists(
            learn_client,
            target_parent_uri,
        )
        created = await learn_client.create_memory(
            parent_path=created_parent_path,
            content=normalized_content,
            priority=max(1, auto_flush_priority),
            title=learn_title,
            disclosure="Explicit learn trigger",
            domain=created_domain,
        )
    except Exception as exec_exc:
        payload["accepted"] = False
        payload["reason"] = "create_memory_failed"
        payload["error"] = str(exec_exc) or type(exec_exc).__name__
        if created_namespace_memories:
            payload["created_namespace_memories"] = created_namespace_memories
            namespace_memory_ids = [
                safe_int(item.get("memory_id"), default=0)
                for item in created_namespace_memories
                if isinstance(item, dict)
            ]
            namespace_memory_ids = [item for item in namespace_memory_ids if item > 0]
            payload["rollback"] = {
                "enabled": True,
                "mode": "namespace_cleanup_only",
                "memory_id": 0,
                "batch_id": batch_id,
                "namespace_memory_ids": namespace_memory_ids,
                "side_effects_audit_required": True,
                "residual_artifacts_review_required": True,
                "side_effects_note": "execute_failed_namespace_cleanup_required",
            }
        await record_import_learn_event(
            event_type="reject",
            operation=operation,
            decision="rejected",
            reason="create_memory_failed",
            source=normalized_source,
            session_id=normalized_session_id,
            actor_id=normalized_actor_id,
            batch_id=batch_id,
            metadata={
                "domain": normalized_domain,
                "path_prefix": normalized_path_prefix,
                "target_parent_uri": target_parent_uri,
                "error": type(exec_exc).__name__,
                "created_namespace_count": len(created_namespace_memories),
            },
        )
        return payload

    created_memory_payload = {
        "id": int(created.get("id") or 0),
        "uri": str(created.get("uri") or ""),
        "domain": str(created.get("domain") or created_domain),
        "path": str(created.get("path") or ""),
    }
    payload["reason"] = "executed"
    payload["executed"] = True
    payload["created_memory"] = created_memory_payload
    payload["created_namespace_memories"] = created_namespace_memories
    namespace_memory_ids = [
        safe_int(item.get("memory_id"), default=0)
        for item in created_namespace_memories
        if isinstance(item, dict)
    ]
    namespace_memory_ids = [item for item in namespace_memory_ids if item > 0]
    payload["rollback"] = {
        "enabled": True,
        "mode": "delete_memory_id",
        "memory_id": created_memory_payload["id"],
        "batch_id": batch_id,
        "namespace_memory_ids": namespace_memory_ids,
        "side_effects_audit_required": True,
        "residual_artifacts_review_required": True,
        "side_effects_note": (
            "rollback_covers_created_memory_ids_and_best_effort_namespace_cleanup"
            if namespace_memory_ids
            else "rollback_only_covers_created_memory_ids"
        ),
    }
    await record_import_learn_event(
        event_type="learn",
        operation=operation,
        decision="executed",
        reason="executed",
        source=normalized_source,
        session_id=normalized_session_id,
        actor_id=normalized_actor_id,
        batch_id=batch_id,
        metadata={
            "domain": normalized_domain,
            "path_prefix": normalized_path_prefix,
            "target_parent_uri": target_parent_uri,
            "created_memory_id": created_memory_payload["id"],
            "created_uri": created_memory_payload["uri"],
            "created_namespace_count": len(namespace_memory_ids),
        },
    )
    return payload
