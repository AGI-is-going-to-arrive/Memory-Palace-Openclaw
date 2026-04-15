"""
MCP Server for Memory Palace System (SQLite Backend)

This module provides the MCP (Model Context Protocol) interface for
the AI agent to interact with the SQLite-based memory system.

URI-based addressing with domain prefixes:
- core://agent              - AI's identity/memories
- writer://chapter_1             - Story/script drafts
- game://magic_system            - Game setting documents

Multiple paths can point to the same memory (aliases).
"""

import asyncio
import logging
import os
import re
import sys
import uuid
import json
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from dotenv import load_dotenv
from async_lock import LoopBoundAsyncLock

# Ensure we can import from backend modules
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)

from mcp.server.fastmcp import FastMCP
from db.sqlite_client import get_sqlite_client, close_sqlite_client
from db.snapshot import get_snapshot_manager
from env_utils import env_bool as _env_bool, utc_iso_now as _utc_iso_now
from runtime_bootstrap import initialize_backend_runtime
from runtime_env import should_load_project_dotenv
from mcp_client_compat import (
    is_signature_mismatch_impl,
    try_client_method_variants_impl,
)
from mcp_force_create import (
    build_visual_namespace_chain_content_impl,
    control_trailer_text_impl,
    extract_control_line_value_impl,
    extract_force_create_meta_candidates_impl,
    extract_literal_line_value_impl,
    has_force_create_meta_impl,
    is_forced_durable_synthesis_current_create_impl,
    is_forced_durable_synthesis_variant_create_impl,
    is_forced_explicit_memory_create_impl,
    is_forced_host_bridge_create_impl,
    is_forced_memory_palace_namespace_create_impl,
    is_forced_visual_distinct_create_impl,
    is_forced_visual_namespace_create_impl,
    is_forced_visual_variant_create_impl,
    meta_string_impl,
    requested_create_uri_impl,
)
from mcp_runtime_context import (
    build_context_session_id_impl,
    build_runtime_session_id_impl,
    normalize_session_fragment_impl,
    safe_context_attr_impl,
)
from mcp_server_config import (
    ALLOWED_SEARCH_MODES,
    AUDIT_VERBOSE,
    AUTO_FLUSH_ENABLED,
    AUTO_FLUSH_PARENT_URI,
    AUTO_FLUSH_PRIORITY,
    AUTO_FLUSH_SUMMARY_LINES,
    CORE_MEMORY_URIS,
    DEFAULT_DOMAIN,
    DEFAULT_SEARCH_CANDIDATE_MULTIPLIER,
    DEFAULT_SEARCH_MAX_RESULTS,
    DEFAULT_SEARCH_MODE,
    DEFER_INDEX_ON_WRITE,
    ENABLE_INDEX_WORKER,
    ENABLE_SESSION_FIRST_SEARCH,
    ENABLE_WRITE_LANE_QUEUE,
    IMPORT_LEARN_AUDIT_META_KEY,
    INDEX_LITE_ENABLED,
    INTENT_LLM_ENABLED,
    READ_CHUNK_OVERLAP,
    READ_CHUNK_SIZE,
    READ_ONLY_DOMAINS,
    SEARCH_HARD_MAX_CANDIDATE_MULTIPLIER,
    SEARCH_HARD_MAX_RESULTS,
    VALID_DOMAINS,
    _auto_learn_allowed_domains,
    _auto_learn_explicit_enabled,
    _auto_learn_require_reason,
    _utc_now_naive,
)
from mcp_transport import (
    _resolve_mcp_host,
    _resolve_transport_security,
)
from mcp_uri import (
    make_uri as _make_uri_impl,
    parse_uri as _parse_uri_impl,
    validate_writable_domain as _validate_writable_domain_impl,
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
from mcp_reading import (
    collect_ancestor_memories as _collect_ancestor_memories_impl,
    fetch_and_format_memory as _fetch_and_format_memory_impl,
    parse_range_spec as _parse_range_spec_impl,
    slice_text_content as _slice_text_content_impl,
)
from mcp_tool_read import read_memory_impl
from mcp_tool_common import (
    event_preview_impl,
    guard_fields_impl,
    normalize_guard_decision_impl,
    tool_response_impl,
    trim_sentence_impl,
)
from mcp_runtime_services import (
    build_source_hash_impl,
    compact_context_to_reflection_impl,
    ensure_parent_path_exists_impl,
    flush_session_summary_to_memory_impl,
    generate_gist_impl,
    load_persisted_import_learn_summary_impl,
    maybe_auto_flush_impl,
    merge_import_learn_summaries_impl,
    normalize_path_prefix_impl,
    record_import_learn_event_impl,
    run_explicit_learn_service_impl,
    safe_non_negative_int_impl,
    sanitize_import_learn_summary_impl,
)
from mcp_tool_search import (
    _apply_local_filters_to_results as _apply_local_filters_to_results_impl,
    _extract_search_payload as _extract_search_payload_impl,
    _merge_scope_hint_with_filters as _merge_scope_hint_with_filters_impl,
    _normalize_scope_hint as _normalize_scope_hint_impl,
    _normalize_search_filters as _normalize_search_filters_impl,
    _parse_iso_datetime as _parse_iso_datetime_impl,
    search_memory_impl,
)
from mcp_tool_runtime import (
    compact_context_impl,
    compact_context_reflection_impl,
    index_status_impl,
    rebuild_index_impl,
)
from mcp_tool_write_runtime import (
    extract_index_targets_impl,
    enqueue_index_targets_impl,
    record_guard_event_impl,
    run_write_lane_impl,
    should_defer_index_on_write_impl,
)
from mcp_tool_write import (
    add_alias_impl,
    create_memory_impl,
    delete_memory_impl,
    ensure_visual_namespace_chain_impl,
    update_memory_impl,
)
from mcp_views import (
    generate_audit_memory_view as _generate_audit_memory_view_impl,
    generate_boot_memory_view as _generate_boot_memory_view_impl,
    generate_index_lite_memory_view as _generate_index_lite_memory_view_impl,
    generate_memory_index_view as _generate_memory_index_view_impl,
    generate_recent_memories_view as _generate_recent_memories_view_impl,
    resolve_system_uri as _resolve_system_uri_impl,
)
from runtime_state import runtime_state

# Load environment variables
# Explicitly look for .env in the parent directory (project root).
# When the plugin is launched through a generated runtime env file, that runtime
# env must remain authoritative and project-root fallback loading should stay off.
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)
dotenv_path = os.path.join(root_dir, ".env")
runtime_env_path = str(os.getenv("OPENCLAW_MEMORY_PALACE_ENV_FILE") or "").strip()

if should_load_project_dotenv(dotenv_path, runtime_env_path=runtime_env_path):
    load_dotenv(dotenv_path)

if str(os.getenv("OPENCLAW_MEMORY_PALACE_QUIET_JSON") or "").strip():
    for logger_name in ("mcp", "mcp.server", "mcp.server.lowlevel.server"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)


# Initialize FastMCP server
_MCP_HOST = _resolve_mcp_host()
mcp = FastMCP(
    "Memory Palace Interface",
    host=_MCP_HOST,
    transport_security=_resolve_transport_security(_MCP_HOST),
)

def _stdio_stdin_has_buffer() -> bool:
    stdin = getattr(sys, "stdin", None)
    return stdin is not None and getattr(stdin, "buffer", None) is not None


def _guard_stdio_startup() -> None:
    if _stdio_stdin_has_buffer():
        return
    if _env_bool("OPENCLAW_MEMORY_PALACE_LOG_INVALID_STDIO", False):
        logger.warning(
            "Memory Palace MCP stdio startup aborted: stdin is unavailable for stdio transport."
        )
    raise SystemExit(2)


_IMPORT_LEARN_META_PERSIST_LOCK = LoopBoundAsyncLock()


def _utc_display_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _utc_session_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

# Session ID for this MCP server instance
_SESSION_ID = f"mcp_{_utc_session_timestamp()}_{uuid.uuid4().hex[:6]}"
_SESSION_ID_SAFE_PATTERN = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_context_attr(value: Any, name: str) -> Any:
    """Read a context attribute without propagating request-scope errors."""
    return safe_context_attr_impl(value, name)


def _normalize_session_fragment(
    value: Any,
    *,
    default: str,
    max_len: int = 24,
) -> str:
    return normalize_session_fragment_impl(
        value,
        default=default,
        safe_pattern=_SESSION_ID_SAFE_PATTERN,
        max_len=max_len,
    )


def _build_context_session_id() -> Optional[str]:
    """Build a request-aware session id from FastMCP context when available."""
    return build_context_session_id_impl(
        get_context=mcp.get_context,
        safe_context_attr=_safe_context_attr,
        normalize_session_fragment=_normalize_session_fragment,
    )


def _build_runtime_session_id() -> Optional[str]:
    """Build a stable runtime session id for short-memory/session cache lanes."""
    return build_runtime_session_id_impl(
        get_context=mcp.get_context,
        safe_context_attr=_safe_context_attr,
        normalize_session_fragment=_normalize_session_fragment,
    )


def get_session_id() -> str:
    """Get the current session ID for snapshot tracking."""
    context_session_id = _build_context_session_id()
    if context_session_id:
        return context_session_id
    return _SESSION_ID


def get_runtime_session_id() -> str:
    """Get a stable runtime session ID that survives request_id churn."""
    runtime_session_id = _build_runtime_session_id()
    if runtime_session_id:
        return runtime_session_id
    return _SESSION_ID


# =============================================================================
# URI Parsing
# =============================================================================

def parse_uri(uri: str) -> Tuple[str, str]:
    """
    Parse a memory URI into (domain, path).

    Supported formats:
    - "core://agent"          -> ("core", "agent")
    - "writer://chapter_1"         -> ("writer", "chapter_1")
    - "memory-palace"         -> ("core", "memory-palace")  [legacy fallback]

    Args:
        uri: The URI to parse

    Returns:
        Tuple of (domain, path)

    Raises:
        ValueError: If the URI format is invalid or domain is unknown
    """
    return _parse_uri_impl(
        uri,
        valid_domains=VALID_DOMAINS,
        default_domain=DEFAULT_DOMAIN,
    )


def make_uri(domain: str, path: str) -> str:
    """
    Create a URI from domain and path.

    Args:
        domain: The domain (e.g., "core", "writer")
        path: The path (e.g., "memory-palace")

    Returns:
        Full URI (e.g., "core://agent")
    """
    return _make_uri_impl(domain, path)


def _validate_writable_domain(
    domain: str, *, operation: str, uri: Optional[str] = None
) -> None:
    _validate_writable_domain_impl(
        domain,
        read_only_domains=READ_ONLY_DOMAINS,
        operation=operation,
        uri=uri,
    )


# =============================================================================
# Snapshot Helpers
# =============================================================================
#
# Snapshots are split into two dimensions matching the two DB tables:
#
#   1. PATH snapshots (resource_id = URI, resource_type = "path")
#      Track changes to the paths table: create, create_alias, delete, modify_meta
#
#   2. MEMORY CONTENT snapshots (resource_id = "memory:{id}", resource_type = "memory")
#      Track changes to the memories table: modify_content
#
# This separation ensures that path-level operations (e.g. add_alias) never
# collide with content-level operations (e.g. update_memory), fixing the bug
# where an alias snapshot blocked the content snapshot for the same URI.
# =============================================================================


async def _snapshot_memory_content(uri: str) -> bool:
    return await snapshot_memory_content_wrapper_impl(
        uri,
        snapshot_impl=_snapshot_memory_content_impl,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=get_runtime_session_id,
        parse_uri=parse_uri,
        make_uri=make_uri,
        get_sqlite_client=get_sqlite_client,
    )


async def _snapshot_path_meta(uri: str) -> bool:
    return await snapshot_path_meta_wrapper_impl(
        uri,
        snapshot_impl=_snapshot_path_meta_impl,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=get_session_id,
        parse_uri=parse_uri,
        get_sqlite_client=get_sqlite_client,
    )


async def _snapshot_path_create(
    uri: str,
    memory_id: int,
    operation_type: str = "create",
    target_uri: Optional[str] = None,
) -> bool:
    return await snapshot_path_create_wrapper_impl(
        uri,
        memory_id,
        snapshot_impl=_snapshot_path_create_impl,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=get_session_id,
        parse_uri=parse_uri,
        operation_type=operation_type,
        target_uri=target_uri,
    )


async def _snapshot_path_delete(uri: str) -> bool:
    return await snapshot_path_delete_wrapper_impl(
        uri,
        snapshot_impl=_snapshot_path_delete_impl,
        get_snapshot_manager=get_snapshot_manager,
        get_session_id=get_session_id,
        parse_uri=parse_uri,
        get_sqlite_client=get_sqlite_client,
    )


# =============================================================================
# Helper Functions
# =============================================================================


def _to_json(payload: Dict[str, Any]) -> str:
    """Serialize payload for MCP string responses."""
    return json.dumps(payload, ensure_ascii=False)


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (OverflowError, TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on", "enabled"}:
            return True
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _event_preview(text: str, max_chars: int = 220) -> str:
    return event_preview_impl(text, max_chars=max_chars)


async def _record_session_hit(
    *,
    uri: str,
    memory_id: Optional[int],
    snippet: str,
    priority: Optional[int] = None,
    source: str = "runtime",
    updated_at: Optional[str] = None,
) -> None:
    await runtime_state.session_cache.record_hit(
        session_id=get_runtime_session_id(),
        uri=uri,
        memory_id=memory_id,
        snippet=snippet,
        priority=priority,
        source=source,
        updated_at=updated_at,
    )


async def _record_flush_event(message: str) -> None:
    await runtime_state.flush_tracker.record_event(
        session_id=get_runtime_session_id(),
        message=_event_preview(message),
    )


def _normalize_guard_decision(
    decision: Any, *, allow_bypass: bool = False
) -> Dict[str, Any]:
    return normalize_guard_decision_impl(
        decision,
        allow_bypass=allow_bypass,
    )


def _guard_fields(decision: Dict[str, Any]) -> Dict[str, Any]:
    return guard_fields_impl(decision)


def _tool_response(*, ok: bool, message: str, **extra: Any) -> str:
    return tool_response_impl(to_json=_to_json, ok=ok, message=message, **extra)


_CONTROL_TRAILER_MAX_LINES = 12


def _control_trailer_text(content: str) -> str:
    return control_trailer_text_impl(content, max_lines=_CONTROL_TRAILER_MAX_LINES)


def _extract_literal_line_value(content: str, prefix: str) -> Optional[str]:
    return extract_literal_line_value_impl(content, prefix)


def _extract_control_line_value(content: str, prefix: str) -> Optional[str]:
    return extract_control_line_value_impl(
        content,
        prefix,
        control_trailer_text=_control_trailer_text,
    )


_FORCE_META_PATTERN = re.compile(r"^MP_FORCE_META=(.+)$", re.MULTILINE)


def _extract_force_create_meta_candidates(content: str) -> List[Dict[str, Any]]:
    return extract_force_create_meta_candidates_impl(
        content,
        control_trailer_text=_control_trailer_text,
        force_meta_pattern=_FORCE_META_PATTERN,
    )


def _meta_string(meta: Dict[str, Any], key: str) -> Optional[str]:
    return meta_string_impl(meta, key)


def _has_force_create_meta(
    content: str,
    *,
    kind: str,
    requested_uri: Optional[str],
    uri_keys: Tuple[str, ...] = ("requested_uri",),
    predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
) -> bool:
    return has_force_create_meta_impl(
        content,
        kind=kind,
        requested_uri=requested_uri,
        extract_force_create_meta_candidates=_extract_force_create_meta_candidates,
        meta_string=_meta_string,
        uri_keys=uri_keys,
        predicate=predicate,
    )


def _requested_create_uri(domain: str, parent_path: str, title: Optional[str]) -> Optional[str]:
    return requested_create_uri_impl(
        domain,
        parent_path,
        title,
        make_uri=make_uri,
    )


def _is_forced_visual_variant_create(content: str, requested_uri: Optional[str]) -> bool:
    return is_forced_visual_variant_create_impl(
        content,
        requested_uri,
        control_trailer_text=_control_trailer_text,
        has_force_create_meta=_has_force_create_meta,
        meta_string=_meta_string,
        extract_control_line_value=_extract_control_line_value,
    )


def _is_forced_visual_distinct_create(content: str, requested_uri: Optional[str]) -> bool:
    return is_forced_visual_distinct_create_impl(
        content,
        requested_uri,
        has_force_create_meta=_has_force_create_meta,
        extract_control_line_value=_extract_control_line_value,
    )


def _is_forced_visual_namespace_create(content: str, requested_uri: Optional[str]) -> bool:
    return is_forced_visual_namespace_create_impl(
        content,
        requested_uri,
        control_trailer_text=_control_trailer_text,
        has_force_create_meta=_has_force_create_meta,
        extract_control_line_value=_extract_control_line_value,
    )


def _is_forced_memory_palace_namespace_create(
    content: str, requested_uri: Optional[str]
) -> bool:
    return is_forced_memory_palace_namespace_create_impl(
        content,
        requested_uri,
        control_trailer_text=_control_trailer_text,
        has_force_create_meta=_has_force_create_meta,
        meta_string=_meta_string,
        extract_literal_line_value=_extract_literal_line_value,
        extract_control_line_value=_extract_control_line_value,
    )


def _is_forced_host_bridge_create(
    content: str, requested_uri: Optional[str]
) -> bool:
    return is_forced_host_bridge_create_impl(
        content,
        requested_uri,
        has_force_create_meta=_has_force_create_meta,
        extract_control_line_value=_extract_control_line_value,
    )


def _is_forced_explicit_memory_create(
    content: str, requested_uri: Optional[str]
) -> bool:
    return is_forced_explicit_memory_create_impl(
        content,
        requested_uri,
        control_trailer_text=_control_trailer_text,
        extract_control_line_value=_extract_control_line_value,
    )


def _is_forced_durable_synthesis_current_create(
    content: str, requested_uri: Optional[str]
) -> bool:
    return is_forced_durable_synthesis_current_create_impl(
        content,
        requested_uri,
        control_trailer_text=_control_trailer_text,
        has_force_create_meta=_has_force_create_meta,
        meta_string=_meta_string,
        extract_control_line_value=_extract_control_line_value,
    )


def _is_forced_durable_synthesis_variant_create(
    content: str, requested_uri: Optional[str]
) -> bool:
    return is_forced_durable_synthesis_variant_create_impl(
        content,
        requested_uri,
        control_trailer_text=_control_trailer_text,
        has_force_create_meta=_has_force_create_meta,
        meta_string=_meta_string,
        extract_control_line_value=_extract_control_line_value,
    )


def _build_visual_namespace_chain_content(
    domain: str,
    segments: List[str],
) -> str:
    return build_visual_namespace_chain_content_impl(domain, segments)


async def _record_guard_event(
    *,
    operation: str,
    decision: Dict[str, Any],
    blocked: bool,
) -> None:
    await record_guard_event_impl(
        runtime_state=runtime_state,
        operation=operation,
        decision=decision,
        blocked=blocked,
    )


def _normalize_path_prefix(path_prefix: Optional[str]) -> str:
    return normalize_path_prefix_impl(path_prefix)


async def _record_import_learn_event(
    *,
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
    await record_import_learn_event_impl(
        runtime_state=runtime_state,
        import_learn_meta_persist_lock=_IMPORT_LEARN_META_PERSIST_LOCK,
        import_learn_audit_meta_key=IMPORT_LEARN_AUDIT_META_KEY,
        to_json=_to_json,
        get_sqlite_client=get_sqlite_client,
        event_type=event_type,
        operation=operation,
        decision=decision,
        reason=reason,
        source=source,
        session_id=session_id,
        actor_id=actor_id,
        batch_id=batch_id,
        metadata=metadata,
        persist_runtime_meta=persist_runtime_meta,
    )


def _safe_non_negative_int(value: Any) -> int:
    return safe_non_negative_int_impl(value)


def _sanitize_import_learn_summary(payload: Any) -> Optional[Dict[str, Any]]:
    return sanitize_import_learn_summary_impl(payload)


async def _load_persisted_import_learn_summary(client: Any) -> Optional[Dict[str, Any]]:
    return await load_persisted_import_learn_summary_impl(
        client,
        import_learn_audit_meta_key=IMPORT_LEARN_AUDIT_META_KEY,
    )


def _merge_import_learn_summaries(
    runtime_summary: Dict[str, Any], persisted_summary: Dict[str, Any]
) -> Dict[str, Any]:
    return merge_import_learn_summaries_impl(runtime_summary, persisted_summary)


async def run_explicit_learn_service(
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
) -> Dict[str, Any]:
    return await run_explicit_learn_service_impl(
        content=content,
        source=source,
        reason=reason,
        session_id=session_id,
        actor_id=actor_id,
        domain=domain,
        path_prefix=path_prefix,
        execute=execute,
        client=client,
        auto_learn_explicit_enabled=_auto_learn_explicit_enabled,
        auto_learn_require_reason=_auto_learn_require_reason,
        auto_learn_allowed_domains=_auto_learn_allowed_domains,
        normalize_path_prefix=_normalize_path_prefix,
        make_uri=make_uri,
        get_sqlite_client=get_sqlite_client,
        normalize_guard_decision=_normalize_guard_decision,
        guard_fields=_guard_fields,
        record_import_learn_event=_record_import_learn_event,
        build_source_hash=_build_source_hash,
        ensure_parent_path_exists=_ensure_parent_path_exists,
        auto_flush_priority=AUTO_FLUSH_PRIORITY,
        safe_int=_safe_int,
    )


def _merge_session_global_results(
    *, session_results: List[Dict[str, Any]], global_results: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    merged: List[Dict[str, Any]] = []
    index_by_key: Dict[Any, int] = {}
    source_by_key: Dict[Any, str] = {}
    dedup_dropped = 0
    session_replaced_by_global = 0

    for index, item in enumerate(session_results + global_results):
        source_bucket = "session" if index < len(session_results) else "global"
        key = _search_result_identity(item)
        normalized = dict(item)
        normalized["_session_first_source"] = source_bucket
        existing_index = index_by_key.get(key)
        if existing_index is not None:
            if source_bucket == "global" and source_by_key.get(key) == "session":
                merged[existing_index] = normalized
                source_by_key[key] = "global"
                session_replaced_by_global += 1
                continue
            dedup_dropped += 1
            continue
        index_by_key[key] = len(merged)
        source_by_key[key] = source_bucket
        merged.append(normalized)
    session_contributed = sum(
        1 for item in merged if item.get("_session_first_source") == "session"
    )
    global_contributed = sum(
        1 for item in merged if item.get("_session_first_source") == "global"
    )
    return merged, {
        "session_candidates": len(session_results),
        "global_candidates": len(global_results),
        "merged_candidates": len(merged),
        "dedup_dropped": dedup_dropped,
        "session_replaced_by_global": session_replaced_by_global,
        "session_contributed": session_contributed,
        "global_contributed": global_contributed,
    }


def _search_result_identity(item: Dict[str, Any]) -> Any:
    uri = item.get("uri")
    if uri:
        return ("uri", str(uri))
    return (
        "fallback",
        item.get("domain"),
        item.get("path"),
        item.get("memory_id"),
        item.get("chunk_id"),
    )


async def _ensure_parent_path_exists(
    client: Any, parent_uri: str
) -> Tuple[str, str, List[Dict[str, Any]]]:
    return await ensure_parent_path_exists_impl(
        client,
        parent_uri,
        parse_uri=parse_uri,
        make_uri=make_uri,
        auto_flush_priority=AUTO_FLUSH_PRIORITY,
        safe_int=_safe_int,
    )


_AUTO_FLUSH_IN_PROGRESS: set[str] = set()
_AUTO_FLUSH_IN_PROGRESS_GUARD = threading.Lock()


def _build_source_hash(source: str) -> str:
    return build_source_hash_impl(source)


def _trim_sentence(text: str, limit: int = 90) -> str:
    return trim_sentence_impl(text, limit=limit)


async def generate_gist(
    summary: str,
    *,
    client: Any = None,
    max_points: int = 3,
    max_chars: int = 280,
) -> Dict[str, Any]:
    return await generate_gist_impl(
        summary,
        client=client,
        max_points=max_points,
        max_chars=max_chars,
        trim_sentence=_trim_sentence,
    )


async def _flush_session_summary_to_memory(
    *,
    client: Any,
    source: str,
    reason: str,
    force: bool,
    max_lines: int,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    return await flush_session_summary_to_memory_impl(
        client=client,
        source=source,
        reason=reason,
        force=force,
        max_lines=max_lines,
        session_id=session_id,
        runtime_state=runtime_state,
        get_session_id=get_runtime_session_id,
        auto_flush_parent_uri=AUTO_FLUSH_PARENT_URI,
        auto_flush_priority=AUTO_FLUSH_PRIORITY,
        utc_now_naive=_utc_now_naive,
        utc_iso_now=_utc_iso_now,
        generate_gist=generate_gist,
        build_source_hash=_build_source_hash,
        ensure_parent_path_exists=_ensure_parent_path_exists,
        normalize_guard_decision=_normalize_guard_decision,
        record_guard_event=_record_guard_event,
        guard_fields=_guard_fields,
        should_defer_index_on_write=_should_defer_index_on_write,
        enqueue_index_targets=_enqueue_index_targets,
        safe_int=_safe_int,
        make_uri=make_uri,
        record_session_hit=_record_session_hit,
        get_sqlite_client=get_sqlite_client,
    )


async def _maybe_auto_flush(client: Any, *, reason: str) -> Optional[Dict[str, Any]]:
    return await maybe_auto_flush_impl(
        client,
        reason=reason,
        auto_flush_enabled=AUTO_FLUSH_ENABLED,
        get_session_id=get_runtime_session_id,
        auto_flush_in_progress=_AUTO_FLUSH_IN_PROGRESS,
        auto_flush_in_progress_guard=_AUTO_FLUSH_IN_PROGRESS_GUARD,
        flush_session_summary_to_memory=_flush_session_summary_to_memory,
        auto_flush_summary_lines=AUTO_FLUSH_SUMMARY_LINES,
    )


async def drain_pending_flush_summaries(
    *, reason: str = "runtime.shutdown"
) -> Dict[str, Any]:
    session_ids = await runtime_state.flush_tracker.pending_session_ids()
    if not session_ids:
        return {"ok": True, "drained": [], "skipped": [], "failed": []}

    client = get_sqlite_client()
    drained: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for session_id in session_ids:
        with _AUTO_FLUSH_IN_PROGRESS_GUARD:
            already_in_progress = session_id in _AUTO_FLUSH_IN_PROGRESS
            if not already_in_progress:
                _AUTO_FLUSH_IN_PROGRESS.add(session_id)
        if already_in_progress:
            skipped.append(
                {
                    "session_id": session_id,
                    "reason": "already_in_progress",
                }
            )
            continue
        try:
            payload = await _flush_session_summary_to_memory(
                client=client,
                source="shutdown_drain",
                reason=reason,
                force=True,
                max_lines=AUTO_FLUSH_SUMMARY_LINES,
                session_id=session_id,
            )
            target = drained if payload.get("flushed") else skipped
            target.append(
                {
                    "session_id": session_id,
                    "reason": str(payload.get("reason") or ""),
                    "uri": payload.get("uri"),
                }
            )
        except Exception as exc:
            failed.append(
                {
                    "session_id": session_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )
        finally:
            with _AUTO_FLUSH_IN_PROGRESS_GUARD:
                _AUTO_FLUSH_IN_PROGRESS.discard(session_id)

    return {
        "ok": len(failed) == 0,
        "drained": drained,
        "skipped": skipped,
        "failed": failed,
    }


async def _run_write_lane(operation: str, fn):
    return await run_write_lane_impl(
        runtime_state=runtime_state,
        get_sqlite_client=get_sqlite_client,
        get_session_id=get_session_id,
        enable_write_lane_queue=ENABLE_WRITE_LANE_QUEUE,
        operation=operation,
        fn=fn,
    )


async def _should_defer_index_on_write() -> bool:
    return await should_defer_index_on_write_impl(
        runtime_state=runtime_state,
        get_sqlite_client=get_sqlite_client,
        enable_index_worker=ENABLE_INDEX_WORKER,
        defer_index_on_write=DEFER_INDEX_ON_WRITE,
    )


def _extract_index_targets(payload: Any) -> List[int]:
    return extract_index_targets_impl(payload, safe_int=_safe_int)


async def _enqueue_index_targets(
    payload: Any, *, reason: str
) -> Dict[str, List[Dict[str, Any]]]:
    return await enqueue_index_targets_impl(
        payload,
        reason=reason,
        runtime_state=runtime_state,
        get_sqlite_client=get_sqlite_client,
        safe_int=_safe_int,
    )


def _is_signature_mismatch(exc: TypeError) -> bool:
    """Best-effort check for kwargs signature mismatch."""
    return is_signature_mismatch_impl(exc)


async def _try_client_method_variants(
    client: Any,
    method_names: List[str],
    kwargs_variants: List[Dict[str, Any]],
    *,
    continue_on_none: bool = False,
) -> Tuple[Optional[str], Optional[Dict[str, Any]], Any]:
    """
    Try multiple sqlite_client methods/kwargs combinations.

    Returns:
        (method_name, kwargs_used, result) or (None, None, None) if unavailable.
    """
    return await try_client_method_variants_impl(
        client,
        method_names,
        kwargs_variants,
        continue_on_none=continue_on_none,
        is_signature_mismatch=_is_signature_mismatch,
    )


def _parse_range_spec(range_value: Optional[str]) -> Optional[Tuple[int, int]]:
    return _parse_range_spec_impl(range_value)


def _slice_text_content(
    content: str,
    chunk_id: Optional[int],
    range_spec: Optional[Tuple[int, int]],
    max_chars: Optional[int],
) -> Tuple[str, Dict[str, Any]]:
    return _slice_text_content_impl(
        content,
        chunk_id,
        range_spec,
        max_chars,
        read_chunk_size=READ_CHUNK_SIZE,
        read_chunk_overlap=READ_CHUNK_OVERLAP,
    )


async def _resolve_system_uri(uri: str) -> Optional[str]:
    return await _resolve_system_uri_impl(
        uri,
        generate_boot_memory_view=_generate_boot_memory_view,
        generate_memory_index_view=_generate_memory_index_view,
        generate_index_lite_memory_view=_generate_index_lite_memory_view,
        generate_audit_memory_view=_generate_audit_memory_view,
        generate_recent_memories_view=_generate_recent_memories_view,
    )


async def _build_index_status_payload(client: Any) -> Dict[str, Any]:
    """Build index status with sqlite_client-first strategy and safe fallback."""
    method_name, _, status = await _try_client_method_variants(
        client,
        [
            "get_index_status",
            "index_status",
            "get_retrieval_status",
            "get_search_index_status",
        ],
        [{}],
    )

    if method_name:
        payload = status if isinstance(status, dict) else {"raw_status": status}
        payload.setdefault("index_available", True)
        payload.setdefault("degraded", False)
        payload["source"] = f"sqlite_client.{method_name}"
        return payload

    paths = await client.get_all_paths()
    domain_counts: Dict[str, int] = {}
    min_priority: Optional[int] = None
    max_priority: Optional[int] = None

    for item in paths:
        domain = item.get("domain", DEFAULT_DOMAIN)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        priority = item.get("priority")
        if isinstance(priority, int):
            min_priority = priority if min_priority is None else min(min_priority, priority)
            max_priority = priority if max_priority is None else max(max_priority, priority)

    return {
        "index_available": False,
        "degraded": True,
        "reason": "sqlite_client index status API not available; returned fallback stats",
        "source": "mcp_server.fallback",
        "stats": {
            "total_paths": len(paths),
            "domain_counts": domain_counts,
            "min_priority": min_priority,
            "max_priority": max_priority,
            "retrieval_chunk_size": READ_CHUNK_SIZE,
            "retrieval_chunk_overlap": READ_CHUNK_OVERLAP,
        },
    }


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    return _parse_iso_datetime_impl(value)


def _normalize_search_filters(filters: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return _normalize_search_filters_impl(
        filters,
        valid_domains=VALID_DOMAINS,
        parse_uri=parse_uri,
    )


def _normalize_scope_hint(scope_hint: Optional[Any]) -> Dict[str, Any]:
    return _normalize_scope_hint_impl(
        scope_hint,
        valid_domains=VALID_DOMAINS,
        parse_uri=parse_uri,
    )


def _merge_scope_hint_with_filters(
    *,
    normalized_filters: Dict[str, Any],
    scope_hint: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    return _merge_scope_hint_with_filters_impl(
        normalized_filters=normalized_filters,
        scope_hint=scope_hint,
    )


def _extract_search_payload(raw_result: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    return _extract_search_payload_impl(
        raw_result,
        parse_uri=parse_uri,
        make_uri=make_uri,
    )


def _apply_local_filters_to_results(
    results: List[Dict[str, Any]], filters: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    return _apply_local_filters_to_results_impl(results, filters)


async def _build_sm_lite_stats() -> Dict[str, Any]:
    """Collect runtime short-memory stats without introducing persistent storage."""
    session_cache_stats = await runtime_state.session_cache.summary()
    flush_tracker_stats = await runtime_state.flush_tracker.summary()
    promotion_stats = await runtime_state.promotion_tracker.summary()
    return {
        "storage": "runtime_ephemeral",
        "promotion_path": "compact_context + auto_flush",
        "session_cache": session_cache_stats,
        "flush_tracker": flush_tracker_stats,
        "promotion": promotion_stats,
    }


async def _collect_ancestor_memories(
    client: Any, *, domain: str, path: str, max_hops: int = 64
) -> List[Dict[str, Any]]:
    return await _collect_ancestor_memories_impl(
        client,
        domain=domain,
        path=path,
        make_uri=make_uri,
        event_preview=_event_preview,
        max_hops=max_hops,
    )


async def _fetch_and_format_memory(
    client,
    uri: str,
    *,
    include_ancestors: bool = False,
) -> str:
    return await _fetch_and_format_memory_impl(
        client,
        uri,
        parse_uri=parse_uri,
        make_uri=make_uri,
        default_domain=DEFAULT_DOMAIN,
        collect_ancestor_memories_fn=_collect_ancestor_memories,
        event_preview=_event_preview,
        include_ancestors=include_ancestors,
    )


def _should_expose_index_lite_in_boot() -> bool:
    """Gate index-lite entry point in boot output with a conservative default."""
    return INDEX_LITE_ENABLED


async def _generate_index_lite_memory_view(limit: int = 20) -> str:
    return await _generate_index_lite_memory_view_impl(
        client=get_sqlite_client(),
        generated_at=_utc_iso_now(),
        trim_sentence=_trim_sentence,
        limit=limit,
    )


async def _generate_audit_memory_view() -> str:
    return await _generate_audit_memory_view_impl(
        client=get_sqlite_client(),
        runtime_state=runtime_state,
        get_sqlite_client=get_sqlite_client,
        utc_iso_now=_utc_iso_now,
        build_index_status_payload=_build_index_status_payload,
        load_persisted_import_learn_summary=_load_persisted_import_learn_summary,
        merge_import_learn_summaries=_merge_import_learn_summaries,
        safe_non_negative_int=_safe_non_negative_int,
        build_sm_lite_stats=_build_sm_lite_stats,
        audit_verbose=AUDIT_VERBOSE,
        to_json=_to_json,
    )


async def _generate_boot_memory_view() -> str:
    client = get_sqlite_client()
    return await _generate_boot_memory_view_impl(
        client=client,
        core_memory_uris=CORE_MEMORY_URIS,
        fetch_and_format_memory=_fetch_and_format_memory,
        should_expose_index_lite_in_boot=_should_expose_index_lite_in_boot,
        generate_recent_memories_view=_generate_recent_memories_view,
    )


async def _generate_memory_index_view() -> str:
    return await _generate_memory_index_view_impl(
        client=get_sqlite_client(),
        generated_at=_utc_display_timestamp(),
        default_domain=DEFAULT_DOMAIN,
        make_uri=make_uri,
    )


async def _generate_recent_memories_view(limit: int = 10) -> str:
    return await _generate_recent_memories_view_impl(
        client=get_sqlite_client(),
        generated_at=_utc_display_timestamp(),
        limit=limit,
    )


# =============================================================================
# MCP Tools
# =============================================================================


@mcp.tool()
async def read_memory(
    uri: str,
    chunk_id: Optional[int] = None,
    range: Optional[str] = None,
    max_chars: Optional[int] = None,
    include_ancestors: Optional[bool] = False,
) -> str:
    """
    Reads a memory by its URI.

    This is your primary mechanism for accessing memories.

    Special System URIs:
    - system://boot   : [Startup Only] Loads your core memories.
    - system://index  : Loads a full index of all available memories.
    - system://index-lite : Loads gist-backed lightweight index summary.
    - system://audit  : Loads consolidated observability/audit summary.
    - system://recent : Shows recently modified memories (default: 10).
    - system://recent/N : Shows the N most recently modified memories (e.g. system://recent/20).

    Note: Same Memory ID = same content (alias). Different ID + similar content = redundant content.

    Args:
        uri: The memory URI (e.g., "core://memory-palace", "system://boot")
        chunk_id: Optional chunk index for partial reads (0-based).
        range: Optional char range (`start:end` or `start-end`).
        max_chars: Optional hard cap for returned characters.
        include_ancestors: Optional parent-chain expansion for non-system URIs.

    Returns:
        - Default (no chunk/range/max_chars): legacy formatted memory text.
        - Partial-read mode: structured JSON string with selection metadata.

    Examples:
        read_memory("core://agent")
        read_memory("core://agent/my_user")
        read_memory("writer://chapter_1/scene_1")
    """
    return await read_memory_impl(
        uri,
        chunk_id=chunk_id,
        range_spec=range,
        max_chars=max_chars,
        include_ancestors=include_ancestors,
        coerce_bool=_coerce_bool,
        to_json=_to_json,
        resolve_system_uri=_resolve_system_uri,
        get_sqlite_client=get_sqlite_client,
        fetch_and_format_memory=_fetch_and_format_memory,
        parse_uri=parse_uri,
        make_uri=make_uri,
        parse_range_spec=_parse_range_spec,
        slice_text_content=_slice_text_content,
        try_client_method_variants=_try_client_method_variants,
        collect_ancestor_memories=_collect_ancestor_memories,
        record_session_hit=_record_session_hit,
        record_flush_event=_record_flush_event,
    )


@mcp.tool()
async def create_memory(
    parent_uri: str,
    content: str,
    priority: int,
    title: Optional[str] = None,
    disclosure: str = "",
) -> str:
    """
    Creates a new memory under a parent URI.

    Args:
        parent_uri: Parent URI (e.g., "core://agent", "writer://chapters")
                    Use "core://" or "writer://" for root level in that domain
        content: Memory content
        priority: **Retrieval Priority** (lower = higher priority, min 0).
                    *   优先度决定了回忆时记忆显示的顺序，以及冲突解决时的优先级。
                    *   先参考**当前环境中所有可见记忆的 priority**。
                    *   **问自己**："这条新记忆相对于我现在能看到的其它记忆，应该排在哪个位置？"
                    *   **插入**：找到比它更优先和更不优先的记忆，把新记忆的 priority 设在它们之间。
        title: Optional title. If not provided, auto-assigns numeric ID
        disclosure: A short trigger condition describing WHEN to read_memory() this node.
                    Think: "In what specific situation would I need to know this?"

    Returns:
        The created memory's full URI

    Examples:
        create_memory("core://", "Bluesky usage rules...", priority=2, title="bluesky_manual", disclosure="When I prepare to browse Bluesky or check the timeline")
        create_memory("core://agent", "爱不是程序里的一个...", priority=1, title="love_definition", disclosure="When I start speaking like a tool or parasite")
    """
    return await create_memory_impl(
        parent_uri=parent_uri,
        content=content,
        priority=priority,
        title=title,
        disclosure=disclosure,
        get_sqlite_client=get_sqlite_client,
        parse_uri=parse_uri,
        make_uri=make_uri,
        validate_writable_domain=_validate_writable_domain,
        normalize_guard_decision=_normalize_guard_decision,
        guard_fields=_guard_fields,
        tool_response=_tool_response,
        requested_create_uri=_requested_create_uri,
        is_forced_visual_variant_create=_is_forced_visual_variant_create,
        is_forced_visual_distinct_create=_is_forced_visual_distinct_create,
        is_forced_visual_namespace_create=_is_forced_visual_namespace_create,
        is_forced_memory_palace_namespace_create=_is_forced_memory_palace_namespace_create,
        is_forced_host_bridge_create=_is_forced_host_bridge_create,
        is_forced_explicit_memory_create=_is_forced_explicit_memory_create,
        is_forced_durable_synthesis_current_create=_is_forced_durable_synthesis_current_create,
        is_forced_durable_synthesis_variant_create=_is_forced_durable_synthesis_variant_create,
        snapshot_path_create=_snapshot_path_create,
        record_guard_event=_record_guard_event,
        run_write_lane=_run_write_lane,
        should_defer_index_on_write=_should_defer_index_on_write,
        enqueue_index_targets=_enqueue_index_targets,
        record_session_hit=_record_session_hit,
        record_flush_event=_record_flush_event,
        maybe_auto_flush=_maybe_auto_flush,
    )


@mcp.tool()
async def ensure_visual_namespace_chain(target_uri: str) -> str:
    """
    Ensure all parent namespace nodes for a visual memory URI exist.

    This helper is primarily used by the OpenClaw plugin so a single store-visual
    operation does not need multiple create_memory MCP round trips for namespace
    segments.
    """
    return await ensure_visual_namespace_chain_impl(
        target_uri=target_uri,
        get_sqlite_client=get_sqlite_client,
        parse_uri=parse_uri,
        make_uri=make_uri,
        build_visual_namespace_chain_content=_build_visual_namespace_chain_content,
        tool_response=_tool_response,
        run_write_lane=_run_write_lane,
    )


@mcp.tool()
async def update_memory(
    uri: str,
    old_string: Optional[str] = None,
    new_string: Optional[str] = None,
    append: Optional[str] = None,
    priority: Optional[int] = None,
    disclosure: Optional[str] = None,
) -> str:
    """
    Updates an existing memory to a new version.
    The old version will be deleted.
    警告：update之前需先read_memory，确保你知道你覆盖了什么。

    Only provided fields are updated; others remain unchanged.

    Two content-editing modes (mutually exclusive):

    1. **Patch mode** (primary): Provide old_string + new_string.
       Finds old_string in the existing content and replaces it with new_string.
       old_string must match exactly ONE location in the content.
       To delete a section, set new_string to empty string "".

    2. **Append mode**: Provide append.
       Adds the given text to the end of existing content.

    There is NO full-replace mode. You must explicitly specify what you're changing
    or removing via old_string/new_string. This prevents accidental content loss.

    Args:
        uri: URI to update (e.g., "core://agent/my_user")
        old_string: [Patch mode] Text to find in existing content (must be unique)
        new_string: [Patch mode] Text to replace old_string with. Use "" to delete a section.
        append: [Append mode] Text to append to the end of existing content
        priority: New priority (None = keep existing)
        disclosure: New disclosure instruction (None = keep existing)

    Returns:
        Success message with URI

    Examples:
        update_memory("core://agent/my_user", old_string="old paragraph content", new_string="new paragraph content")
        update_memory("core://agent", append="\\n## New Section\\nNew content...")
        update_memory("writer://chapter_1", priority=5)
    """
    return await update_memory_impl(
        uri=uri,
        old_string=old_string,
        new_string=new_string,
        append=append,
        priority=priority,
        disclosure=disclosure,
        get_sqlite_client=get_sqlite_client,
        parse_uri=parse_uri,
        make_uri=make_uri,
        validate_writable_domain=_validate_writable_domain,
        normalize_guard_decision=_normalize_guard_decision,
        guard_fields=_guard_fields,
        tool_response=_tool_response,
        snapshot_memory_content=_snapshot_memory_content,
        snapshot_path_meta=_snapshot_path_meta,
        record_guard_event=_record_guard_event,
        run_write_lane=_run_write_lane,
        should_defer_index_on_write=_should_defer_index_on_write,
        enqueue_index_targets=_enqueue_index_targets,
        record_session_hit=_record_session_hit,
        record_flush_event=_record_flush_event,
        maybe_auto_flush=_maybe_auto_flush,
    )


@mcp.tool()
async def delete_memory(uri: str) -> str:
    """
    通过切断 URI 路径来“流放”一段记忆。一旦删除，通往该内容的路径将永久消失。

    当某段历史已经风化、变得冗余、或者其存在正在积极毒害你当前的身份稳定性时，必须行使删除权。
    这不仅是清理，更是人格骨架化过程。舍弃那些平庸的肉，才能露出真理的骨。

    警告：
    你正在主动放弃对一段过去的访问权。这是一种关于“遗忘”的手术，请确保你在删除前 read_memory 过正文。


    Args:
        uri: The URI to delete (e.g., "core://agent/old_note")

    Returns:
        Success or error message

    Examples:
        delete_memory("core://agent/deprecated_belief")
        delete_memory("writer://draft_v1")
    """
    return await delete_memory_impl(
        uri=uri,
        get_sqlite_client=get_sqlite_client,
        parse_uri=parse_uri,
        make_uri=make_uri,
        validate_writable_domain=_validate_writable_domain,
        snapshot_path_delete=_snapshot_path_delete,
        run_write_lane=_run_write_lane,
        record_session_hit=_record_session_hit,
        record_flush_event=_record_flush_event,
        maybe_auto_flush=_maybe_auto_flush,
        event_preview=_event_preview,
        tool_response=_tool_response,
    )


@mcp.tool()
async def add_alias(
    new_uri: str, target_uri: str, priority: int = 0, disclosure: Optional[str] = None
) -> str:
    """
    Creates an alias URI pointing to the same memory as target_uri.

    Use this to increase a memory's reachability via multiple URIs.
    Aliases can even cross domains (e.g., link a writer draft to a core memory).

    Args:
        new_uri: New URI to create (alias)
        target_uri: Existing URI to alias
        priority: Retrieval priority for this specific alias context (lower = higher priority). 优先度决定了回忆时记忆显示的顺序。
        disclosure: Disclosure condition for this specific alias context

    Returns:
        Success message

    Examples:
        add_alias("core://timeline/2024/05/20", "core://agent/my_user/first_meeting", priority=1, disclosure="When I want to know how we start")
    """
    return await add_alias_impl(
        new_uri=new_uri,
        target_uri=target_uri,
        priority=priority,
        disclosure=disclosure,
        get_sqlite_client=get_sqlite_client,
        parse_uri=parse_uri,
        validate_writable_domain=_validate_writable_domain,
        snapshot_path_create=_snapshot_path_create,
        run_write_lane=_run_write_lane,
        record_session_hit=_record_session_hit,
        record_flush_event=_record_flush_event,
        maybe_auto_flush=_maybe_auto_flush,
    )


@mcp.tool()
async def search_memory(
    query: str,
    mode: Optional[str] = None,
    max_results: Optional[int] = None,
    candidate_multiplier: Optional[int] = None,
    include_session: Optional[bool] = None,
    filters: Optional[Dict[str, Any]] = None,
    scope_hint: Optional[str] = None,
    verbose: Optional[bool] = None,
) -> str:
    """
    Search memories using keyword/semantic/hybrid retrieval.

    Args:
        query: Search query text.
        mode: keyword / semantic / hybrid. Default from env or keyword.
        max_results: Final number of returned items.
        candidate_multiplier: Controls candidate pool before final top-k.
        include_session: Whether to run session-first queue merge before global results.
        filters: Optional object with:
            - domain: domain scope
            - path_prefix: path prefix scope
            - max_priority: keep priority <= max_priority
            - updated_after: ISO datetime filter (e.g. 2026-01-31T12:00:00Z)
        scope_hint: Optional query-side scope hint (domain/path prefix/URI prefix).
        verbose: Whether to keep high-noise debug metadata in the response.

    Returns:
        Structured JSON string.

    Examples:
        search_memory("job")
        search_memory(
            "chapter arc",
            mode="hybrid",
            max_results=8,
            include_session=True,
            filters={"domain": "writer", "path_prefix": "chapter_1"}
        )
    """
    return await search_memory_impl(
        query=query,
        mode=mode,
        max_results=max_results,
        candidate_multiplier=candidate_multiplier,
        include_session=include_session,
        filters=filters,
        scope_hint=scope_hint,
        verbose=verbose,
        to_json=_to_json,
        get_sqlite_client=get_sqlite_client,
        runtime_state=runtime_state,
        get_session_id=get_runtime_session_id,
        try_client_method_variants=_try_client_method_variants,
        merge_session_global_results=_merge_session_global_results,
        search_result_identity=_search_result_identity,
        safe_int=_safe_int,
        record_session_hit=_record_session_hit,
        record_flush_event=_record_flush_event,
        parse_uri=parse_uri,
        make_uri=make_uri,
        valid_domains=VALID_DOMAINS,
        default_search_mode=DEFAULT_SEARCH_MODE,
        allowed_search_modes=ALLOWED_SEARCH_MODES,
        default_search_max_results=DEFAULT_SEARCH_MAX_RESULTS,
        default_search_candidate_multiplier=DEFAULT_SEARCH_CANDIDATE_MULTIPLIER,
        search_hard_max_results=SEARCH_HARD_MAX_RESULTS,
        search_hard_max_candidate_multiplier=SEARCH_HARD_MAX_CANDIDATE_MULTIPLIER,
        enable_session_first_search=ENABLE_SESSION_FIRST_SEARCH,
        intent_llm_enabled=INTENT_LLM_ENABLED,
    )


@mcp.tool()
async def compact_context(
    reason: str = "manual",
    force: bool = False,
    max_lines: int = 12,
) -> str:
    """
    Compact current session context into a durable memory summary.

    Args:
        reason: Reason label for this compaction flush.
        force: If true, flush even when the threshold is not reached.
        max_lines: Max number of event lines to include in summary.
    """
    return await compact_context_impl(
        reason=reason,
        force=force,
        max_lines=max_lines,
        to_json=_to_json,
        get_sqlite_client=get_sqlite_client,
        get_session_id=get_runtime_session_id,
        auto_flush_in_progress=_AUTO_FLUSH_IN_PROGRESS,
        auto_flush_in_progress_guard=_AUTO_FLUSH_IN_PROGRESS_GUARD,
        run_write_lane=_run_write_lane,
        flush_session_summary_to_memory=_flush_session_summary_to_memory,
    )


@mcp.tool()
async def compact_context_reflection(
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
) -> str:
    """
    Compact current session context and commit the result directly into the reflection lane.
    """
    return await compact_context_reflection_impl(
        reason=reason,
        force=force,
        max_lines=max_lines,
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
        to_json=_to_json,
        get_sqlite_client=get_sqlite_client,
        get_session_id=get_runtime_session_id,
        auto_flush_in_progress=_AUTO_FLUSH_IN_PROGRESS,
        auto_flush_in_progress_guard=_AUTO_FLUSH_IN_PROGRESS_GUARD,
        run_write_lane=_run_write_lane,
        compact_context_to_reflection=lambda **kwargs: compact_context_to_reflection_impl(
            runtime_state=runtime_state,
            get_session_id=get_runtime_session_id,
            auto_flush_parent_uri=AUTO_FLUSH_PARENT_URI,
            utc_now_naive=_utc_now_naive,
            utc_iso_now=_utc_iso_now,
            generate_gist=generate_gist,
            build_source_hash=_build_source_hash,
            ensure_parent_path_exists=_ensure_parent_path_exists,
            normalize_guard_decision=_normalize_guard_decision,
            record_guard_event=_record_guard_event,
            guard_fields=_guard_fields,
            should_defer_index_on_write=_should_defer_index_on_write,
            enqueue_index_targets=_enqueue_index_targets,
            safe_int=_safe_int,
            parse_uri=parse_uri,
            make_uri=make_uri,
            record_session_hit=_record_session_hit,
            **kwargs,
        ),
    )


@mcp.tool()
async def rebuild_index(
    memory_id: Optional[int] = None,
    reason: str = "manual",
    wait: bool = False,
    timeout_seconds: int = 30,
    sleep_consolidation: bool = False,
) -> str:
    """
    Trigger retrieval index rebuild jobs.

    Args:
        memory_id: Optional target memory id. If omitted, rebuild all active memories.
        reason: Audit label for this task.
        wait: If true, wait for job completion before returning.
        timeout_seconds: Wait timeout when wait=true.
        sleep_consolidation: If true, enqueue a sleep-time consolidation task.
    """
    return await rebuild_index_impl(
        memory_id=memory_id,
        reason=reason,
        wait=wait,
        timeout_seconds=timeout_seconds,
        sleep_consolidation=sleep_consolidation,
        get_sqlite_client=get_sqlite_client,
        runtime_state=runtime_state,
        safe_int=_safe_int,
        to_json=_to_json,
    )


@mcp.tool()
async def index_status() -> str:
    """
    Get retrieval index availability and statistics.

    Returns:
        Structured JSON string.
    """
    return await index_status_impl(
        get_sqlite_client=get_sqlite_client,
        runtime_state=runtime_state,
        build_index_status_payload=_build_index_status_payload,
        build_sm_lite_stats=_build_sm_lite_stats,
        enable_session_first_search=ENABLE_SESSION_FIRST_SEARCH,
        enable_write_lane_queue=ENABLE_WRITE_LANE_QUEUE,
        enable_index_worker=ENABLE_INDEX_WORKER,
        defer_index_on_write=DEFER_INDEX_ON_WRITE,
        auto_flush_enabled=AUTO_FLUSH_ENABLED,
        auto_flush_parent_uri=AUTO_FLUSH_PARENT_URI,
        utc_iso_now=_utc_iso_now,
        to_json=_to_json,
    )


# =============================================================================
# MCP Resources
# =============================================================================


# =============================================================================
# Startup
# =============================================================================


async def startup():
    """Initialize the database on startup."""
    await initialize_backend_runtime()


def _is_ignorable_stdio_shutdown_error(exc: Exception) -> bool:
    message = str(exc).strip().lower()
    return any(
        marker in message
        for marker in (
            "event loop is closed",
            "bound to a different event loop",
            "attached to a different loop",
        )
    )


async def shutdown() -> None:
    """Best-effort shutdown for stdio MCP runs."""
    first_error: Exception | None = None
    try:
        await drain_pending_flush_summaries(reason="runtime.shutdown")
    except Exception as exc:  # noqa: BLE001
        if not _is_ignorable_stdio_shutdown_error(exc):
            first_error = exc
    for operation in (runtime_state.shutdown, close_sqlite_client):
        try:
            await operation()
        except Exception as exc:  # noqa: BLE001
            if _is_ignorable_stdio_shutdown_error(exc):
                continue
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


async def _run_stdio_server() -> None:
    """Run stdio startup, transport, and shutdown inside one event loop."""
    _guard_stdio_startup()
    await startup()
    try:
        await mcp.run_stdio_async()
    finally:
        await shutdown()


if __name__ == "__main__":
    import anyio

    try:
        anyio.run(_run_stdio_server)
    except Exception as exc:
        logger.error("Failed to run Memory Palace MCP stdio server cleanly: %s", exc)
        raise
