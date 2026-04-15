"""
Browse API - Clean URI-based memory navigation

This replaces the old Entity/Relation/Chapter conceptual split with a simple
hierarchical browser. Every path is just a node with content and children.
"""

import hashlib
import hmac
import os
import re
import threading
import time
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Any
from db import get_sqlite_client
from db.snapshot import get_snapshot_manager
from env_utils import env_bool as _env_bool, utc_iso_now as _utc_iso_now
from db.sqlite_client import (
    Path as PathModel,
    is_valid_memory_path_segment,
    memory_path_segment_error_message,
)
from mcp_runtime_services import (
    build_source_hash_impl,
    ensure_parent_path_exists_impl,
    flush_session_summary_to_memory_impl,
    generate_gist_impl,
    maybe_auto_flush_impl,
)
from mcp_server_config import (
    AUTO_FLUSH_ENABLED,
    AUTO_FLUSH_PARENT_URI,
    AUTO_FLUSH_PRIORITY,
    AUTO_FLUSH_SUMMARY_LINES,
    DEFER_INDEX_ON_WRITE,
    ENABLE_INDEX_WORKER,
    _utc_now_naive,
)
from mcp_snapshot import (
    snapshot_memory_content as _snapshot_memory_content_impl,
    snapshot_path_create as _snapshot_path_create_impl,
    snapshot_path_delete as _snapshot_path_delete_impl,
    snapshot_path_meta as _snapshot_path_meta_impl,
)
from mcp_snapshot_wrappers import (
    snapshot_memory_content_wrapper_impl,
    snapshot_path_create_wrapper_impl,
    snapshot_path_delete_wrapper_impl,
    snapshot_path_meta_wrapper_impl,
)
from mcp_tool_common import (
    event_preview_impl,
    guard_fields_impl,
    normalize_guard_decision_impl,
    trim_sentence_impl,
)
from mcp_tool_write_runtime import (
    enqueue_index_targets_impl,
    record_guard_event_impl,
    run_write_lane_impl,
    should_defer_index_on_write_impl,
)
from mcp_uri import make_uri as _make_uri_impl, parse_uri as _parse_uri_impl
from runtime_state import runtime_state
from .maintenance import require_maintenance_api_key
from sqlalchemy import select

router = APIRouter(prefix="/browse", tags=["browse"])
_READ_ONLY_DOMAINS = {"system"}
_VALID_DOMAINS = list(
    dict.fromkeys(
        [
            d.strip().lower()
            for d in str(os.getenv("VALID_DOMAINS", "core,writer,game,notes,system")).split(",")
            if d.strip()
        ]
        + sorted(_READ_ONLY_DOMAINS)
    )
)


class NodeUpdate(BaseModel):
    content: str | None = None
    priority: int | None = None
    disclosure: str | None = None
    force_write: bool = False
    guard_override_token: str | None = None


class NodeCreate(BaseModel):
    parent_path: str = ""
    title: str | None = None
    content: str
    priority: int = 0
    disclosure: str | None = None
    domain: str = "core"
    force_write: bool = False
    guard_override_token: str | None = None


ENABLE_WRITE_LANE_QUEUE = _env_bool("RUNTIME_WRITE_LANE_QUEUE", True)
_BROWSE_DEFAULT_SESSION_ID = "browse.dashboard"
_BROWSE_AUTO_FLUSH_IN_PROGRESS: set[str] = set()
_BROWSE_AUTO_FLUSH_IN_PROGRESS_GUARD = threading.Lock()

# ── Guard Override Token ──────────────────────────────────────────────
# Short-lived HMAC token that binds a force_write to a specific
# (action, target, path, timestamp).  Prevents callers from bypassing
# the guard with a bare boolean.
_GUARD_TOKEN_SECRET = os.getenv("MCP_API_KEY", "") or os.urandom(32).hex()
_GUARD_TOKEN_TTL_SECONDS = 300  # 5 minutes

def _content_fingerprint(content: str) -> str:
    """Short hash of content to bind the token to the exact payload."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _issue_guard_override_token(
    operation: str,
    domain: str,
    path: str,
    guard_action: str,
    content: str,
    title: str | None = None,
) -> str:
    ts = str(int(time.time()))
    cfp = _content_fingerprint(content)
    msg = f"{operation}|{domain}|{path}|{title or ''}|{guard_action}|{cfp}|{ts}"
    sig = hmac.new(
        _GUARD_TOKEN_SECRET.encode() if isinstance(_GUARD_TOKEN_SECRET, str) else _GUARD_TOKEN_SECRET,
        msg.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{ts}:{sig}"


def _verify_guard_override_token(
    token: str | None,
    operation: str,
    domain: str,
    path: str,
    guard_action: str,
    content: str,
    title: str | None = None,
) -> bool:
    if not token or ":" not in token:
        return False
    ts_str, sig = token.split(":", 1)
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    if abs(time.time() - ts) > _GUARD_TOKEN_TTL_SECONDS:
        return False
    cfp = _content_fingerprint(content)
    msg = f"{operation}|{domain}|{path}|{title or ''}|{guard_action}|{cfp}|{ts_str}"
    expected = hmac.new(
        _GUARD_TOKEN_SECRET.encode() if isinstance(_GUARD_TOKEN_SECRET, str) else _GUARD_TOKEN_SECRET,
        msg.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def _normalize_domain_or_422(domain: str) -> str:
    normalized = str(domain or "").strip().lower()
    if not normalized:
        normalized = "core"
    if normalized not in _VALID_DOMAINS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown domain '{normalized}'. Valid domains: {', '.join(_VALID_DOMAINS)}",
        )
    return normalized


def _ensure_writable_domain_or_422(domain: str, *, operation: str) -> str:
    normalized = _normalize_domain_or_422(domain)
    if normalized in _READ_ONLY_DOMAINS:
        raise HTTPException(
            status_code=422,
            detail=f"{operation} does not allow writes to '{normalized}://'. system:// is read-only.",
        )
    return normalized


def _normalize_title_or_422(title: str | None) -> str | None:
    normalized = (title or "").strip() or None
    if normalized is None:
        return None
    if not is_valid_memory_path_segment(normalized):
        raise HTTPException(status_code=422, detail=memory_path_segment_error_message())
    return normalized


def _normalize_guard_decision(payload: Any, *, allow_bypass: bool = False) -> dict[str, Any]:
    return normalize_guard_decision_impl(payload, allow_bypass=allow_bypass)


def _guard_fields(payload: dict[str, Any]) -> dict[str, Any]:
    return guard_fields_impl(payload)


def _browse_session_id(session_id: str | None) -> str:
    value = str(session_id or "").strip()
    if not value:
        return _BROWSE_DEFAULT_SESSION_ID
    sanitized = re.sub(r"[^a-zA-Z0-9_.:-]+", "-", value).strip(".:-")
    return sanitized[:128] or _BROWSE_DEFAULT_SESSION_ID


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return default


def _parse_uri(uri: str) -> tuple[str, str]:
    return _parse_uri_impl(
        uri,
        valid_domains=_VALID_DOMAINS,
        default_domain="core",
    )


def _parse_write_lane_timeout_seconds(message: str) -> float | None:
    matched = re.search(r"after\s+([0-9]+(?:\.[0-9]+)?)s", str(message or "").lower())
    if not matched:
        return None
    try:
        return float(matched.group(1))
    except ValueError:
        return None


def _browse_timeout_payload(
    *,
    message: str,
    success_field: str,
    uri: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "success": False,
        success_field: False,
        "reason": "write_lane_timeout",
        "retryable": True,
        "message": (
            f"Error: {message}. Wait for the current write to finish and retry."
        ),
    }
    if uri:
        payload["uri"] = uri
    timeout_seconds = _parse_write_lane_timeout_seconds(message)
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
        payload["retry_after_seconds"] = timeout_seconds
    if extra:
        payload.update(extra)
    return payload


async def _record_session_hit(
    *,
    session_id: str | None,
    uri: str,
    memory_id: int | None,
    snippet: str,
    priority: int | None = None,
    source: str = "browse",
    updated_at: str | None = None,
) -> None:
    await runtime_state.session_cache.record_hit(
        session_id=_browse_session_id(session_id),
        uri=uri,
        memory_id=memory_id,
        snippet=snippet,
        priority=priority,
        source=source,
        updated_at=updated_at,
    )


async def _record_flush_event(message: str, *, session_id: str | None) -> None:
    await runtime_state.flush_tracker.record_event(
        session_id=_browse_session_id(session_id),
        message=event_preview_impl(message),
    )


async def _generate_gist(
    summary: str,
    *,
    client: Any = None,
    max_points: int = 3,
    max_chars: int = 280,
) -> dict[str, Any]:
    return await generate_gist_impl(
        summary,
        client=client,
        max_points=max_points,
        max_chars=max_chars,
        trim_sentence=trim_sentence_impl,
    )


async def _ensure_parent_path_exists(client: Any, parent_uri: str):
    return await ensure_parent_path_exists_impl(
        client,
        parent_uri,
        parse_uri=_parse_uri,
        make_uri=_make_uri_impl,
        auto_flush_priority=AUTO_FLUSH_PRIORITY,
        safe_int=_safe_int,
    )


async def _record_guard_event(
    operation: str,
    decision: dict[str, Any],
    blocked: bool,
) -> None:
    try:
        await record_guard_event_impl(
            runtime_state=runtime_state,
            operation=operation,
            decision=decision,
            blocked=blocked,
        )
    except Exception:
        return


async def _should_defer_index_on_write() -> bool:
    return await should_defer_index_on_write_impl(
        runtime_state=runtime_state,
        get_sqlite_client=get_sqlite_client,
        enable_index_worker=ENABLE_INDEX_WORKER,
        defer_index_on_write=DEFER_INDEX_ON_WRITE,
    )


async def _enqueue_index_targets(payload: Any, *, reason: str) -> dict[str, list[dict[str, Any]]]:
    return await enqueue_index_targets_impl(
        payload,
        reason=reason,
        runtime_state=runtime_state,
        get_sqlite_client=get_sqlite_client,
        safe_int=_safe_int,
    )


async def _flush_session_summary_to_memory(
    *,
    client: Any,
    source: str,
    reason: str,
    force: bool,
    max_lines: int,
    session_id: str | None,
) -> dict[str, Any]:
    return await flush_session_summary_to_memory_impl(
        client=client,
        source=source,
        reason=reason,
        force=force,
        max_lines=max_lines,
        session_id=_browse_session_id(session_id),
        runtime_state=runtime_state,
        get_session_id=lambda: _browse_session_id(session_id),
        auto_flush_parent_uri=AUTO_FLUSH_PARENT_URI,
        auto_flush_priority=AUTO_FLUSH_PRIORITY,
        utc_now_naive=_utc_now_naive,
        utc_iso_now=_utc_iso_now,
        generate_gist=_generate_gist,
        build_source_hash=build_source_hash_impl,
        ensure_parent_path_exists=_ensure_parent_path_exists,
        normalize_guard_decision=_normalize_guard_decision,
        record_guard_event=_record_guard_event,
        guard_fields=_guard_fields,
        should_defer_index_on_write=_should_defer_index_on_write,
        enqueue_index_targets=_enqueue_index_targets,
        safe_int=_safe_int,
        make_uri=_make_uri_impl,
        record_session_hit=lambda **kwargs: _record_session_hit(
            session_id=session_id,
            **kwargs,
        ),
        get_sqlite_client=get_sqlite_client,
    )


async def _maybe_auto_flush(client: Any, *, reason: str, session_id: str | None) -> dict[str, Any] | None:
    return await maybe_auto_flush_impl(
        client,
        reason=reason,
        auto_flush_enabled=AUTO_FLUSH_ENABLED,
        get_session_id=lambda: _browse_session_id(session_id),
        auto_flush_in_progress=_BROWSE_AUTO_FLUSH_IN_PROGRESS,
        auto_flush_in_progress_guard=_BROWSE_AUTO_FLUSH_IN_PROGRESS_GUARD,
        flush_session_summary_to_memory=lambda **kwargs: _flush_session_summary_to_memory(
            session_id=session_id,
            **kwargs,
        ),
        auto_flush_summary_lines=AUTO_FLUSH_SUMMARY_LINES,
    )


async def _snapshot_path_create(uri: str, memory_id: int, *, session_id: str | None, operation_type: str = "create") -> bool:
    return await snapshot_path_create_wrapper_impl(
        uri,
        memory_id,
        snapshot_impl=_snapshot_path_create_impl,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=lambda: _browse_session_id(session_id),
        parse_uri=_parse_uri,
        operation_type=operation_type,
    )


async def _snapshot_memory_content(uri: str, *, session_id: str | None) -> bool:
    return await snapshot_memory_content_wrapper_impl(
        uri,
        snapshot_impl=_snapshot_memory_content_impl,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=lambda: _browse_session_id(session_id),
        parse_uri=_parse_uri,
        make_uri=_make_uri_impl,
        get_sqlite_client=get_sqlite_client,
    )


async def _snapshot_path_meta(uri: str, *, session_id: str | None) -> bool:
    return await snapshot_path_meta_wrapper_impl(
        uri,
        snapshot_impl=_snapshot_path_meta_impl,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=lambda: _browse_session_id(session_id),
        parse_uri=_parse_uri,
        get_sqlite_client=get_sqlite_client,
    )


async def _snapshot_path_delete(uri: str, *, session_id: str | None) -> bool:
    return await snapshot_path_delete_wrapper_impl(
        uri,
        snapshot_impl=_snapshot_path_delete_impl,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=lambda: _browse_session_id(session_id),
        parse_uri=_parse_uri,
        get_sqlite_client=get_sqlite_client,
    )

def _guard_user_feedback(
    operation: str,
    decision: dict[str, Any],
) -> dict[str, Any]:
    action = str(decision.get("action") or "NOOP").upper()
    reason = str(decision.get("reason") or "").strip()
    target_uri = decision.get("target_uri")
    target_uri = target_uri if isinstance(target_uri, str) and target_uri.strip() else None
    reason_lower = reason.lower()

    if reason_lower.startswith("write_guard_unavailable"):
        user_reason = (
            "Automatic duplicate checking is unavailable right now, so the write was paused instead of guessing."
        )
        feedback_code = "guard_unavailable"
    elif reason_lower.startswith("invalid_guard_action:"):
        user_reason = (
            "The duplicate-check result was invalid, so the write was paused for safety."
        )
        feedback_code = "guard_invalid"
    elif action == "UPDATE":
        user_reason = (
            "This looks close to an existing memory, so automatic storage paused before creating a duplicate."
        )
        feedback_code = "possible_duplicate_update"
    elif action == "DELETE":
        user_reason = (
            "This write could replace an existing memory, so it was paused for review."
        )
        feedback_code = "possible_replace"
    else:
        user_reason = (
            "This may duplicate an existing memory, so automatic storage paused first."
        )
        feedback_code = "possible_duplicate_noop"

    recovery_hint = (
        "Review the suggested memory first, or choose Store anyway if you still want this saved here."
        if action in {"NOOP", "UPDATE", "DELETE"}
        else "Choose Store anyway only if you still want this saved despite the guard pause."
    )

    # Only allow force_write when the guard gave a definitive duplicate/conflict verdict.
    # guard_unavailable and guard_invalid are fail-closed states where override is unsafe.
    allow_override = feedback_code not in ("guard_unavailable", "guard_invalid")

    return {
        "guard_feedback_code": feedback_code,
        "guard_user_reason": user_reason,
        "guard_recovery_hint": recovery_hint if allow_override else (
            "The duplicate checker could not run. Please try again later."
        ),
        "force_write_available": allow_override,
        "user_message": (
            "Automatic storage paused for review."
            if operation == "create_node"
            else "Automatic update paused for review."
        ),
        "guard_suggested_uri": target_uri,
    }
async def _run_write_lane(operation: str, task, *, session_id: str | None = None):
    return await run_write_lane_impl(
        runtime_state=runtime_state,
        get_sqlite_client=get_sqlite_client,
        get_session_id=lambda: _browse_session_id(session_id),
        enable_write_lane_queue=ENABLE_WRITE_LANE_QUEUE,
        operation=operation,
        fn=task,
    )


@router.get("/node")
async def get_node(
    path: str = Query("", description="URI path like 'memory-palace' or 'memory-palace/salem'"),
    domain: str = Query("core"),
    _auth: None = Depends(require_maintenance_api_key),
):
    """
    Get a node's content and its direct children.
    
    This is the only read endpoint you need - it gives you:
    - The current node's full content (or virtual root)
    - Preview of all children (next level)
    - Breadcrumb trail for navigation
    """
    client = get_sqlite_client()
    domain = _normalize_domain_or_422(domain)
    
    if not path:
        # Virtual Root Node
        memory = {
            "content": "",
            "priority": 0,
            "disclosure": None,
            "created_at": None
        }
        # Get roots as children (no memory_id = virtual root)
        children_raw = await client.get_children(None, domain=domain)
        breadcrumbs = [{"path": "", "label": "root"}]
    else:
        # Get the node itself
        memory = await client.get_memory_by_path(
            path, domain=domain, reinforce_access=False
        )
        
        if not memory:
            raise HTTPException(status_code=404, detail=f"Path not found: {domain}://{path}")
        
        # Get children across all aliases of this memory
        children_raw = await client.get_children(memory["id"])
        
        # Build breadcrumbs
        segments = path.split("/")
        breadcrumbs = [{"path": "", "label": "root"}]
        accumulated = ""
        for seg in segments:
            accumulated = f"{accumulated}/{seg}" if accumulated else seg
            breadcrumbs.append({"path": accumulated, "label": seg})
    
    children = [
        {
            "domain": c["domain"],
            "path": c["path"],
            "uri": f"{c['domain']}://{c['path']}",
            "name": c["path"].split("/")[-1],  # Last segment
            "priority": c["priority"],
            "disclosure": c.get("disclosure"),
            "content_snippet": c["content_snippet"],
            "gist_text": c.get("gist_text"),
            "gist_method": c.get("gist_method"),
            "gist_quality": c.get("gist_quality"),
            "source_hash": c.get("gist_source_hash"),
        }
        for c in children_raw
    ]
    children.sort(key=lambda x: (x["priority"] if x["priority"] is not None else 999, x["path"]))
    
    # Get all aliases (other paths pointing to the same memory)
    aliases = []
    if path and memory.get("id"):
        async with client.readonly_session() as session:
            result = await session.execute(
                select(PathModel.domain, PathModel.path)
                .where(PathModel.memory_id == memory["id"])
            )
            aliases = [
                f"{row[0]}://{row[1]}"
                for row in result.all()
                if not (row[0] == domain and row[1] == path)  # exclude current
            ]
    
    return {
        "node": {
            "path": path,
            "domain": domain,
            "uri": f"{domain}://{path}",
            "name": path.split("/")[-1] if path else "root",
            "content": memory["content"],
            "priority": memory["priority"],
            "disclosure": memory["disclosure"],
            "created_at": memory["created_at"],
            "aliases": aliases,
            "gist_text": memory.get("gist_text"),
            "gist_method": memory.get("gist_method"),
            "gist_quality": memory.get("gist_quality"),
            "source_hash": memory.get("gist_source_hash"),
        },
        "children": children,
        "breadcrumbs": breadcrumbs
    }


@router.post("/node")
async def create_node(
    body: NodeCreate,
    session_id: str | None = None,
    _auth: None = Depends(require_maintenance_api_key),
):
    """
    Create a new node under a parent path.

    The write_guard check runs INSIDE the write lane to prevent TOCTOU races
    under concurrent multi-agent writes (matches the MCP tool path pattern).
    """
    client = get_sqlite_client()
    parent_path = body.parent_path.strip().strip("/")
    domain = _ensure_writable_domain_or_422(body.domain, operation="create_node")
    title = _normalize_title_or_422(body.title)
    requested_uri = _make_uri_impl(
        domain,
        "/".join(part for part in (parent_path, title or "") if part),
    )
    defer_index = await _should_defer_index_on_write()

    async def _write_task():
        # ── Guard check (inside write lane to prevent TOCTOU) ──
        try:
            guard_decision = _normalize_guard_decision(
                await client.write_guard(
                    content=body.content,
                    domain=domain,
                    path_prefix=parent_path if parent_path else None,
                )
            )
        except Exception as exc:
            guard_decision = _normalize_guard_decision(
                {
                    "action": "NOOP",
                    "reason": f"write_guard_unavailable: {exc}",
                    "method": "exception",
                    "degraded": True,
                    "degrade_reasons": ["write_guard_exception"],
                }
            )

        guard_action = str(guard_decision.get("action") or "NOOP").upper()
        blocked = guard_action != "ADD"
        await _record_guard_event("browse.create_node", guard_decision, blocked=blocked)

        if blocked and not body.force_write:
            feedback = _guard_user_feedback("create_node", guard_decision)
            # Issue a server-bound override token so the client can retry
            # with a proof-of-intent rather than a bare boolean.
            override_token = _issue_guard_override_token(
                "create_node", domain, parent_path, guard_action,
                content=body.content, title=title,
            ) if feedback.get("force_write_available") else None
            return {
                "__guard_blocked": True,
                "success": False,
                "created": False,
                "reason": "write_guard_blocked",
                "message": (
                    "Skipped: write_guard blocked create_node "
                    f"(action={guard_action}, method={guard_decision.get('method')})."
                ),
                **_guard_fields(guard_decision),
                **feedback,
                "guard_override_token": override_token,
            }

        if blocked and body.force_write:
            fc = _guard_user_feedback("create_node", guard_decision).get("guard_feedback_code")
            if fc in ("guard_unavailable", "guard_invalid"):
                return {
                    "__guard_blocked": True,
                    "success": False,
                    "created": False,
                    "reason": "force_write_rejected",
                    "message": "Cannot override: the write guard was unavailable or returned an invalid result.",
                    **_guard_fields(guard_decision),
                    **_guard_user_feedback("create_node", guard_decision),
                }
            # Verify the override token to prevent bare-boolean bypass.
            if not _verify_guard_override_token(
                body.guard_override_token, "create_node", domain, parent_path, guard_action,
                content=body.content, title=title,
            ):
                return {
                    "__guard_blocked": True,
                    "success": False,
                    "created": False,
                    "reason": "invalid_override_token",
                    "message": "The guard override token is missing, expired, or does not match this operation.",
                    **_guard_fields(guard_decision),
                    **_guard_user_feedback("create_node", guard_decision),
                }

        # ── Actual write ──
        result = await client.create_memory(
            parent_path=parent_path,
            content=body.content,
            priority=body.priority,
            title=title,
            disclosure=body.disclosure,
            domain=domain,
            index_now=not defer_index,
        )
        created_uri = str(result.get("uri") or requested_uri)
        await _snapshot_path_create(
            created_uri,
            _safe_int(result.get("id"), default=0),
            session_id=session_id,
            operation_type="create",
        )
        return {
            "__guard_blocked": False,
            "success": True,
            "ok": True,
            "created": True,
            **result,
            **_guard_fields(guard_decision),
            **(_guard_user_feedback("create_node", guard_decision) if blocked else {}),
            "guard_overridden": blocked and body.force_write,
            "uri": created_uri,
            "user_message": (
                "Stored after your confirmation."
                if blocked and body.force_write
                else "Stored in long-term memory."
            ),
        }

    try:
        result = await _run_write_lane(
            "browse.create_node",
            _write_task,
            session_id=session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except TimeoutError as exc:
        return _browse_timeout_payload(
            message=str(exc),
            success_field="created",
            uri=requested_uri,
        )
    except Exception as exc:
        return {
            "ok": False,
            "success": False,
            "created": False,
            "reason": "internal_error",
            "retryable": False,
            "uri": requested_uri,
            "message": f"Error: create_node failed. {exc}",
        }

    if result.get("created"):
        index_enqueue = {"queued": [], "dropped": [], "deduped": []}
        if defer_index:
            index_enqueue = await _enqueue_index_targets(result, reason="browse.create_node")
        result["index_queued"] = len(index_enqueue["queued"])
        result["index_dropped"] = len(index_enqueue["dropped"])
        result["index_deduped"] = len(index_enqueue["deduped"])
        try:
            await _record_session_hit(
                session_id=session_id,
                uri=str(result.get("uri") or requested_uri),
                memory_id=_safe_int(result.get("id"), default=0) or None,
                snippet=str(body.content or "")[:300],
                priority=body.priority,
                source="browse.create_node",
            )
            await _record_flush_event(
                f"create {str(result.get('uri') or requested_uri)}",
                session_id=session_id,
            )
            await _maybe_auto_flush(
                client,
                reason="browse.create_node",
                session_id=session_id,
            )
        except Exception:
            pass
    # Strip internal marker before returning to client.
    result.pop("__guard_blocked", None)
    return result


@router.put("/node")
async def update_node(
    path: str = Query(...),
    domain: str = Query("core"),
    session_id: str | None = None,
    body: NodeUpdate = ...,
    _auth: None = Depends(require_maintenance_api_key),
):
    """
    Update a node's content.

    The write_guard check runs INSIDE the write lane (same as create_node)
    to prevent TOCTOU races under concurrent multi-agent writes.
    """
    client = get_sqlite_client()
    domain = _ensure_writable_domain_or_422(domain, operation="update_node")

    # Check exists (read is safe outside write lane)
    memory = await client.get_memory_by_path(
        path, domain=domain, reinforce_access=False
    )
    if not memory:
        raise HTTPException(status_code=404, detail=f"Path not found: {domain}://{path}")
    full_uri = _make_uri_impl(domain, path)
    defer_index = await _should_defer_index_on_write()

    async def _write_task():
        # ── Guard check (inside write lane to prevent TOCTOU) ──
        if body.content is not None:
            try:
                guard_decision = _normalize_guard_decision(
                    await client.write_guard(
                        content=body.content,
                        domain=domain,
                        path_prefix=path.rsplit("/", 1)[0] if "/" in path else None,
                        exclude_memory_id=memory.get("id"),
                    )
                )
            except Exception as exc:
                guard_decision = _normalize_guard_decision(
                    {
                        "action": "NOOP",
                        "reason": f"write_guard_unavailable: {exc}",
                        "method": "exception",
                        "degraded": True,
                        "degrade_reasons": ["write_guard_exception"],
                    }
                )
        else:
            guard_decision = _normalize_guard_decision(
                {"action": "BYPASS", "reason": "metadata_only_update", "method": "none"},
                allow_bypass=True,
            )

        guard_action = str(guard_decision.get("action") or "NOOP").upper()
        blocked = False
        if body.content is not None:
            if guard_action == "ADD":
                blocked = False
            elif guard_action == "UPDATE":
                target_id = guard_decision.get("target_id")
                current_memory_id = memory.get("id")
                if (
                    not isinstance(target_id, int)
                    or not isinstance(current_memory_id, int)
                    or target_id != current_memory_id
                ):
                    blocked = True
            else:
                blocked = True

        await _record_guard_event("browse.update_node", guard_decision, blocked=blocked)

        if blocked and not body.force_write:
            feedback = _guard_user_feedback("update_node", guard_decision)
            override_token = _issue_guard_override_token(
                "update_node", domain, path, guard_action,
                content=body.content or "", title=path.split("/")[-1] if path else None,
            ) if feedback.get("force_write_available") else None
            return {
                "__guard_blocked": True,
                "success": False,
                "updated": False,
                "reason": "write_guard_blocked",
                "message": (
                    "Skipped: write_guard blocked update_node "
                    f"(action={guard_action}, method={guard_decision.get('method')})."
                ),
                **_guard_fields(guard_decision),
                **feedback,
                "guard_override_token": override_token,
            }

        if blocked and body.force_write:
            fc = _guard_user_feedback("update_node", guard_decision).get("guard_feedback_code")
            if fc in ("guard_unavailable", "guard_invalid"):
                return {
                    "__guard_blocked": True,
                    "success": False,
                    "updated": False,
                    "reason": "force_write_rejected",
                    "message": "Cannot override: the write guard was unavailable or returned an invalid result.",
                    **_guard_fields(guard_decision),
                    **_guard_user_feedback("update_node", guard_decision),
                }
            if not _verify_guard_override_token(
                body.guard_override_token, "update_node", domain, path, guard_action,
                content=body.content or "", title=path.split("/")[-1] if path else None,
            ):
                return {
                    "__guard_blocked": True,
                    "success": False,
                    "updated": False,
                    "reason": "invalid_override_token",
                    "message": "The guard override token is missing, expired, or does not match this operation.",
                    **_guard_fields(guard_decision),
                    **_guard_user_feedback("update_node", guard_decision),
                }

        # ── Actual write ──
        if body.content is not None:
            await _snapshot_memory_content(full_uri, session_id=session_id)
        if body.priority is not None or body.disclosure is not None:
            await _snapshot_path_meta(full_uri, session_id=session_id)
        result = await client.update_memory(
            path=path,
            domain=domain,
            content=body.content,
            priority=body.priority,
            disclosure=body.disclosure,
            expected_old_id=memory.get("id") if body.content is not None else None,
            index_now=not defer_index,
        )
        return {
            "__guard_blocked": False,
            "success": True,
            "ok": True,
            "updated": True,
            "memory_id": result["new_memory_id"],
            "uri": str(result.get("uri") or full_uri),
            "index_targets": result.get("index_targets"),
            **_guard_fields(guard_decision),
            **(_guard_user_feedback("update_node", guard_decision) if blocked else {}),
            "guard_overridden": blocked and body.force_write,
            "user_message": (
                "Updated after your confirmation."
                if blocked and body.force_write
                else "Long-term memory updated."
            ),
        }

    try:
        result = await _run_write_lane(
            "browse.update_node",
            _write_task,
            session_id=session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except TimeoutError as exc:
        return _browse_timeout_payload(
            message=str(exc),
            success_field="updated",
            uri=full_uri,
        )
    except Exception as exc:
        return {
            "ok": False,
            "success": False,
            "updated": False,
            "reason": "internal_error",
            "retryable": False,
            "uri": full_uri,
            "message": f"Error: update_node failed. {exc}",
        }

    if result.get("updated"):
        index_enqueue = {"queued": [], "dropped": [], "deduped": []}
        if defer_index:
            index_enqueue = await _enqueue_index_targets(result, reason="browse.update_node")
        result["index_queued"] = len(index_enqueue["queued"])
        result["index_dropped"] = len(index_enqueue["dropped"])
        result["index_deduped"] = len(index_enqueue["deduped"])
        preview_text = body.content
        if preview_text is None:
            preview_text = (
                f"meta update priority={body.priority if body.priority is not None else '(unchanged)'} "
                f"disclosure={body.disclosure if body.disclosure is not None else '(unchanged)'}"
            )
        try:
            await _record_session_hit(
                session_id=session_id,
                uri=str(result.get("uri") or full_uri),
                memory_id=_safe_int(result.get("memory_id"), default=0) or None,
                snippet=str(preview_text)[:300],
                priority=body.priority if body.priority is not None else memory.get("priority"),
                source="browse.update_node",
            )
            await _record_flush_event(
                f"update {str(result.get('uri') or full_uri)}",
                session_id=session_id,
            )
            await _maybe_auto_flush(
                client,
                reason="browse.update_node",
                session_id=session_id,
            )
        except Exception:
            pass
    result.pop("__guard_blocked", None)
    return result


@router.delete("/node")
async def delete_node(
    path: str = Query(...),
    domain: str = Query("core"),
    session_id: str | None = None,
    _auth: None = Depends(require_maintenance_api_key),
):
    """
    Delete a single path. If the path has children, this operation is rejected.
    """
    client = get_sqlite_client()
    domain = _ensure_writable_domain_or_422(domain, operation="delete_node")
    full_uri = _make_uri_impl(domain, path)

    async def _write_task():
        memory = await client.get_memory_by_path(
            path,
            domain=domain,
            reinforce_access=False,
        )
        if not memory:
            return {"removed": False}
        await _snapshot_path_delete(full_uri, session_id=session_id)
        result = await client.remove_path(path=path, domain=domain)
        return {
            **result,
            "removed": True,
            "memory": memory,
            "uri": full_uri,
        }

    try:
        result = await _run_write_lane(
            "browse.delete_node",
            _write_task,
            session_id=session_id,
        )
    except ValueError as e:
        message = str(e)
        if "not found" in message:
            raise HTTPException(status_code=404, detail=message)
        raise HTTPException(status_code=409, detail=message)
    except TimeoutError as exc:
        return _browse_timeout_payload(
            message=str(exc),
            success_field="deleted",
            uri=full_uri,
        )
    except Exception as exc:
        return {
            "ok": False,
            "success": False,
            "deleted": False,
            "reason": "internal_error",
            "retryable": False,
            "uri": full_uri,
            "message": f"Error: delete_node failed. {exc}",
        }

    if not result.get("removed"):
        raise HTTPException(status_code=404, detail=f"Path not found: {domain}://{path}")

    deleted_memory = result.get("memory") or {}
    try:
        await _record_session_hit(
            session_id=session_id,
            uri=full_uri,
            memory_id=_safe_int(deleted_memory.get("id"), default=0) or None,
            snippet=f"[deleted] {event_preview_impl(str(deleted_memory.get('content') or ''))}",
            priority=deleted_memory.get("priority"),
            source="browse.delete_node",
            updated_at=deleted_memory.get("created_at"),
        )
        await _record_flush_event(f"delete {full_uri}", session_id=session_id)
        await _maybe_auto_flush(
            client,
            reason="browse.delete_node",
            session_id=session_id,
        )
    except Exception:
        pass

    return {
        "ok": True,
        "success": True,
        "deleted": True,
        **result,
    }
