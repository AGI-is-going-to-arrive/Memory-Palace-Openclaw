import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from filelock import AsyncFileLock, Timeout as FileLockTimeout

from db.sqlite_paths import extract_sqlite_file_path
from mcp_runtime_services import (
    compact_context_to_reflection_impl,
    _resolve_auto_flush_process_lock_timeout_sec as _resolve_process_lock_timeout_sec,
)

logger = logging.getLogger(__name__)


class _NullContextManager:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def _internal_error_message(operation: str) -> str:
    return f"{operation} failed. Check server logs for details."


def _resolve_compact_context_process_lock_path(
    client: Any,
    *,
    session_id: str,
) -> Optional[Path]:
    database_url = getattr(client, "database_url", None) or os.getenv("DATABASE_URL")
    database_path = extract_sqlite_file_path(database_url)
    if database_path is None:
        return None
    session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return database_path.with_name(
        f"{database_path.name}.compact_context.{session_hash}.lock"
    )


async def compact_context_impl(
    reason: str = "manual",
    force: bool = False,
    max_lines: int = 12,
    *,
    to_json: Callable[[Dict[str, Any]], str],
    get_sqlite_client: Callable[[], Any],
    get_session_id: Callable[[], str],
    auto_flush_in_progress: set[str],
    auto_flush_in_progress_guard: Optional[Any] = None,
    run_write_lane: Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]],
    flush_session_summary_to_memory: Callable[..., Awaitable[Dict[str, Any]]],
) -> str:
    client = get_sqlite_client()
    try:
        lines = max(3, int(max_lines))
    except (TypeError, ValueError):
        return to_json({"ok": False, "error": "max_lines must be an integer >= 3."})

    session_id = get_session_id()
    with auto_flush_in_progress_guard or _NullContextManager():
        if session_id in auto_flush_in_progress:
            return to_json(
                {
                    "ok": False,
                    "error": "Compaction already in progress for current session.",
                    "session_id": session_id,
                }
            )
        auto_flush_in_progress.add(session_id)
    try:
        async def _write_task():
            return await flush_session_summary_to_memory(
                client=client,
                source="compact_context",
                reason=(reason or "manual"),
                force=bool(force),
                max_lines=lines,
            )

        lock_path = _resolve_compact_context_process_lock_path(
            client, session_id=session_id
        )
        if lock_path is not None:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                async with AsyncFileLock(
                    str(lock_path),
                    timeout=_resolve_process_lock_timeout_sec(),
                ):
                    result = await run_write_lane("compact_context", _write_task)
            except FileLockTimeout:
                return to_json(
                    {
                        "ok": False,
                        "error": "Compaction already in progress for current session/process.",
                        "session_id": session_id,
                    }
                )
        else:
            result = await run_write_lane("compact_context", _write_task)
        payload = {
            "ok": True,
            "session_id": session_id,
            "reason": reason or "manual",
            "force": bool(force),
            "max_lines": lines,
            **(result if isinstance(result, dict) else {"result": result}),
        }
        return to_json(payload)
    except Exception as e:
        logger.exception("compact_context failed: %s", e, exc_info=e)
        return to_json(
            {
                "ok": False,
                "error": _internal_error_message("compact_context"),
                "session_id": session_id,
            }
        )
    finally:
        with auto_flush_in_progress_guard or _NullContextManager():
            auto_flush_in_progress.discard(session_id)


async def compact_context_reflection_impl(
    reason: str = "reflection_lane",
    force: bool = True,
    max_lines: int = 12,
    seed_event: Optional[str] = None,
    reflection_root_uri: str = "core://reflection",
    reflection_agent_key: str = "anonymous",
    reflection_session_ref: str = "unknown-session",
    reflection_agent_id: Optional[str] = None,
    reflection_session_id: Optional[str] = None,
    reflection_session_key: Optional[str] = None,
    reflection_priority: int = 2,
    reflection_disclosure: str = "When recalling cross-session lessons, invariants, or open loops.",
    reflection_decay_hint_days: int = 14,
    reflection_retention_class: str = "rolling_session",
    *,
    to_json: Callable[[Dict[str, Any]], str],
    get_sqlite_client: Callable[[], Any],
    get_session_id: Callable[[], str],
    auto_flush_in_progress: set[str],
    auto_flush_in_progress_guard: Optional[Any] = None,
    run_write_lane: Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]],
    compact_context_to_reflection: Callable[..., Awaitable[Dict[str, Any]]],
) -> str:
    client = get_sqlite_client()
    try:
        lines = max(3, int(max_lines))
    except (TypeError, ValueError):
        return to_json({"ok": False, "error": "max_lines must be an integer >= 3."})

    session_id = get_session_id()
    with auto_flush_in_progress_guard or _NullContextManager():
        if session_id in auto_flush_in_progress:
            return to_json(
                {
                    "ok": False,
                    "error": "Compaction already in progress for current session.",
                    "session_id": session_id,
                }
            )
        auto_flush_in_progress.add(session_id)
    try:
        async def _write_task():
            return await compact_context_to_reflection(
                client=client,
                reason=(reason or "reflection_lane"),
                force=bool(force),
                max_lines=lines,
                seed_event=seed_event,
                reflection_root_uri=reflection_root_uri,
                reflection_agent_key=reflection_agent_key,
                reflection_session_ref=reflection_session_ref,
                reflection_agent_id=reflection_agent_id,
                reflection_session_id=reflection_session_id,
                reflection_session_key=reflection_session_key,
                reflection_priority=reflection_priority,
                reflection_disclosure=reflection_disclosure,
                reflection_decay_hint_days=reflection_decay_hint_days,
                reflection_retention_class=reflection_retention_class,
            )

        lock_path = _resolve_compact_context_process_lock_path(
            client, session_id=session_id
        )
        if lock_path is not None:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                async with AsyncFileLock(
                    str(lock_path),
                    timeout=_resolve_process_lock_timeout_sec(),
                ):
                    result = await run_write_lane(
                        "compact_context_reflection", _write_task
                    )
            except FileLockTimeout:
                return to_json(
                    {
                        "ok": False,
                        "error": "Compaction already in progress for current session/process.",
                        "session_id": session_id,
                    }
                )
        else:
            result = await run_write_lane("compact_context_reflection", _write_task)
        payload = {
            "ok": True,
            "session_id": session_id,
            "reason": reason or "reflection_lane",
            "force": bool(force),
            "max_lines": lines,
            **(result if isinstance(result, dict) else {"result": result}),
        }
        return to_json(payload)
    except Exception as e:
        logger.exception("compact_context_reflection failed: %s", e, exc_info=e)
        return to_json(
            {
                "ok": False,
                "error": _internal_error_message("compact_context_reflection"),
                "session_id": session_id,
            }
        )
    finally:
        with auto_flush_in_progress_guard or _NullContextManager():
            auto_flush_in_progress.discard(session_id)


async def rebuild_index_impl(
    memory_id: Optional[int] = None,
    reason: str = "manual",
    wait: bool = False,
    timeout_seconds: int = 30,
    sleep_consolidation: bool = False,
    *,
    get_sqlite_client: Callable[[], Any],
    runtime_state: Any,
    safe_int: Callable[[Any, int], int],
    to_json: Callable[[Dict[str, Any]], str],
) -> str:
    client = get_sqlite_client()
    await runtime_state.ensure_started(get_sqlite_client)
    worker_status = await runtime_state.index_worker.status()

    if memory_id is not None:
        parsed_memory_id = safe_int(memory_id, default=-1)
        if parsed_memory_id <= 0:
            return to_json({"ok": False, "error": "memory_id must be a positive integer."})
        memory_target: Optional[int] = parsed_memory_id
    else:
        memory_target = None

    if sleep_consolidation and memory_target is not None:
        return to_json(
            {
                "ok": False,
                "error": "memory_id is incompatible with sleep_consolidation=true.",
            }
        )

    if not worker_status.get("enabled"):
        if sleep_consolidation:
            return to_json(
                {
                    "ok": False,
                    "error": "sleep_consolidation requires runtime index worker.",
                }
            )
        try:
            if memory_target is None:
                result = await client.rebuild_index(reason=reason or "manual")
            else:
                result = await client.reindex_memory(
                    memory_id=memory_target,
                    reason=reason or "manual",
                )
            return to_json(
                {
                    "ok": True,
                    "queued": False,
                    "executed_sync": True,
                    "memory_id": memory_target,
                    "reason": reason or "manual",
                    "result": result,
                    "runtime_worker": worker_status,
                }
            )
        except Exception as exc:
            return to_json({"ok": False, "error": str(exc), "memory_id": memory_target})

    try:
        if sleep_consolidation:
            schedule_result = await runtime_state.sleep_consolidation.schedule(
                index_worker=runtime_state.index_worker,
                force=True,
                reason=reason or "manual",
            )
            if not schedule_result.get("scheduled"):
                payload = {
                    "ok": False,
                    "error": str(
                        schedule_result.get("reason")
                        or "sleep_consolidation_not_scheduled"
                    ),
                    "task_type": "sleep_consolidation",
                    "memory_id": memory_target,
                    "request_reason": reason or "manual",
                    **schedule_result,
                }
                if schedule_result.get("dropped"):
                    payload["runtime_worker"] = await runtime_state.index_worker.status()
                    payload["sleep_consolidation"] = (
                        await runtime_state.sleep_consolidation.status()
                    )
                return to_json(payload)
            enqueue_result = schedule_result
            task_type = "sleep_consolidation"
        elif memory_target is None:
            enqueue_result = await runtime_state.index_worker.enqueue_rebuild(
                reason=reason or "manual"
            )
            task_type = "rebuild_index"
        else:
            enqueue_result = await runtime_state.index_worker.enqueue_reindex_memory(
                memory_id=memory_target,
                reason=reason or "manual",
            )
            task_type = "reindex_memory"

        if enqueue_result.get("dropped"):
            return to_json(
                {
                    "ok": False,
                    "error": str(enqueue_result.get("reason") or "queue_full"),
                    "task_type": task_type,
                    "memory_id": memory_target,
                    "request_reason": reason or "manual",
                    **enqueue_result,
                    "runtime_worker": await runtime_state.index_worker.status(),
                    "sleep_consolidation": await runtime_state.sleep_consolidation.status(),
                }
            )

        payload: Dict[str, Any] = {
            "ok": True,
            "memory_id": memory_target,
            "reason": reason or "manual",
            "task_type": task_type,
            **enqueue_result,
        }

        job_id = enqueue_result.get("job_id")
        if wait and isinstance(job_id, str) and job_id:
            wait_result = await runtime_state.index_worker.wait_for_job(
                job_id=job_id,
                timeout_seconds=max(1.0, float(timeout_seconds)),
            )
            payload["wait_result"] = wait_result

        payload["runtime_worker"] = await runtime_state.index_worker.status()
        payload["sleep_consolidation"] = await runtime_state.sleep_consolidation.status()
        return to_json(payload)
    except Exception as exc:
        return to_json({"ok": False, "error": str(exc), "memory_id": memory_target})


async def index_status_impl(
    *,
    get_sqlite_client: Callable[[], Any],
    runtime_state: Any,
    build_index_status_payload: Callable[[Any], Awaitable[Dict[str, Any]]],
    build_sm_lite_stats: Callable[[], Awaitable[Dict[str, Any]]],
    enable_session_first_search: bool,
    enable_write_lane_queue: bool,
    enable_index_worker: bool,
    defer_index_on_write: bool,
    auto_flush_enabled: bool,
    auto_flush_parent_uri: str,
    utc_iso_now: Callable[[], str],
    to_json: Callable[[Dict[str, Any]], str],
) -> str:
    client = get_sqlite_client()

    try:
        payload = await build_index_status_payload(client)
        await runtime_state.ensure_started(get_sqlite_client)
        lane_status = await runtime_state.write_lanes.status()
        worker_status = await runtime_state.index_worker.status()
        payload["runtime"] = {
            "session_first_search_enabled": enable_session_first_search,
            "write_lane_queue_enabled": enable_write_lane_queue,
            "index_worker_enabled": enable_index_worker,
            "defer_index_on_write": defer_index_on_write,
            "auto_flush_enabled": auto_flush_enabled,
            "auto_flush_parent_uri": auto_flush_parent_uri,
            "write_lanes": lane_status,
            "index_worker": worker_status,
            "sleep_consolidation": await runtime_state.sleep_consolidation.status(),
        }
        try:
            sm_lite_payload = await build_sm_lite_stats()
        except Exception as exc:
            sm_lite_payload = {
                "degraded": True,
                "reason": str(exc),
                "storage": "runtime_ephemeral",
                "promotion_path": "compact_context + auto_flush",
                "session_cache": {},
                "flush_tracker": {},
            }
        payload["runtime"]["sm_lite"] = sm_lite_payload
        if bool(sm_lite_payload.get("degraded")):
            payload["degraded"] = True
            existing_reasons = payload.get("degrade_reasons")
            if not isinstance(existing_reasons, list):
                existing_reasons = []
            existing_reasons.append(
                f"sm_lite:{str(sm_lite_payload.get('reason') or 'degraded')}"
            )
            payload["degrade_reasons"] = list(dict.fromkeys(existing_reasons))
        payload.setdefault("ok", True)
        payload.setdefault("timestamp", utc_iso_now())
        return to_json(payload)
    except Exception as e:
        logger.exception("index_status failed: %s", e, exc_info=e)
        return to_json(
            {
                "ok": False,
                "index_available": False,
                "degraded": True,
                "reason": _internal_error_message("index_status"),
                "timestamp": utc_iso_now(),
            }
        )
