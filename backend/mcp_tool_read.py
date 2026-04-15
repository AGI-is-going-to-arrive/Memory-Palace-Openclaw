import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


async def read_memory_impl(
    uri: str,
    chunk_id: Optional[int] = None,
    range_spec: Optional[str] = None,
    max_chars: Optional[int] = None,
    include_ancestors: Optional[bool] = False,
    *,
    coerce_bool: Callable[[Any, bool], bool],
    to_json: Callable[[Dict[str, Any]], str],
    resolve_system_uri: Callable[[str], Awaitable[Optional[str]]],
    get_sqlite_client: Callable[[], Any],
    fetch_and_format_memory: Callable[[Any, str], Awaitable[str]],
    parse_uri: Callable[[str], Tuple[str, str]],
    make_uri: Callable[[str, str], str],
    parse_range_spec: Callable[[Optional[str]], Optional[Tuple[int, int]]],
    slice_text_content: Callable[[str, Optional[int], Optional[Tuple[int, int]], Optional[int]], Tuple[str, Dict[str, Any]]],
    try_client_method_variants: Callable[..., Awaitable[Tuple[Optional[str], Dict[str, Any], Any]]],
    collect_ancestor_memories: Callable[..., Awaitable[List[Dict[str, Any]]]],
    record_session_hit: Callable[..., Awaitable[None]],
    record_flush_event: Callable[[str], Awaitable[None]],
) -> str:
    include_ancestors_flag = coerce_bool(include_ancestors, default=False)
    partial_mode = any(v is not None for v in (chunk_id, range_spec, max_chars))

    def _partial_error(message: str) -> str:
        return to_json({"ok": False, "error": message})

    if not partial_mode:
        try:
            system_view = await resolve_system_uri(uri)
            if system_view is not None:
                return system_view
        except ValueError as e:
            return f"Error: {str(e)}"

        client = get_sqlite_client()
        try:
            rendered = await fetch_and_format_memory(
                client,
                uri,
                include_ancestors=include_ancestors_flag,
            )
            try:
                domain, path = parse_uri(uri)
                try:
                    memory = await client.get_memory_by_path(
                        path,
                        domain,
                        reinforce_access=False,
                    )
                except TypeError:
                    memory = await client.get_memory_by_path(path, domain)
                if memory:
                    full_uri = make_uri(domain, path)
                    await record_session_hit(
                        uri=full_uri,
                        memory_id=memory.get("id"),
                        snippet=str(memory.get("content", ""))[:300],
                        priority=memory.get("priority"),
                        source="read_memory",
                        updated_at=memory.get("created_at"),
                    )
                    await record_flush_event(f"read {full_uri}")
            except Exception as exc:
                logger.warning(
                    "read_memory session hit recording failed for %s: %s",
                    uri,
                    exc,
                )
            return rendered
        except Exception as e:
            return f"Error: {str(e)}"

    try:
        if chunk_id is not None:
            chunk_id = int(chunk_id)
            if chunk_id < 0:
                return _partial_error("chunk_id must be >= 0.")

        parsed_range = parse_range_spec(range_spec)
        if chunk_id is not None and parsed_range is not None:
            return _partial_error("chunk_id and range cannot be used together.")

        if max_chars is not None:
            max_chars = int(max_chars)
            if max_chars <= 0:
                return _partial_error("max_chars must be > 0.")
    except ValueError as e:
        return _partial_error(str(e))

    degraded_reasons: List[str] = []
    backend_method = "mcp_server.local_slice"
    content_source = "memory"
    content = ""
    selection_meta: Dict[str, Any] = {}
    memory_id: Optional[int] = None
    memory_priority: Optional[int] = None
    memory_updated_at: Optional[str] = None

    try:
        system_view = await resolve_system_uri(uri)
    except ValueError as e:
        return _partial_error(str(e))

    if system_view is not None:
        content_source = "system_uri"
        content = system_view
        selected, selection_meta = slice_text_content(
            content=content,
            chunk_id=chunk_id,
            range_spec=parsed_range,
            max_chars=max_chars,
        )
        payload: Dict[str, Any] = {
            "ok": True,
            "uri": uri.strip(),
            "source": content_source,
            "backend_method": backend_method,
            "selection": selection_meta,
            "content": selected,
            "include_ancestors": False,
            "degraded": False,
        }
        return to_json(payload)

    client = get_sqlite_client()

    try:
        domain, path = parse_uri(uri)
    except ValueError as e:
        return _partial_error(str(e))

    method_name, _, raw_memory = await try_client_method_variants(
        client,
        [
            "read_memory_segment",
            "read_memory_slice",
            "read_memory_chunk",
            "get_memory_slice",
            "get_memory_chunk",
            "get_memory_by_path",
        ],
        [
            {
                "uri": make_uri(domain, path),
                "chunk_id": chunk_id,
                "chunk_index": chunk_id,
                "start": parsed_range[0] if parsed_range is not None else None,
                "end": parsed_range[1] if parsed_range is not None else None,
                "max_chars": max_chars,
                "domain": domain,
            },
            {
                "path": path,
                "domain": domain,
                "chunk_id": chunk_id,
                "range": range_spec,
                "max_chars": max_chars,
            },
            {
                "domain": domain,
                "path": path,
                "chunk_id": chunk_id,
                "range": range_spec,
                "max_chars": max_chars,
            },
            {
                "uri": make_uri(domain, path),
                "chunk_id": chunk_id,
                "range": range_spec,
                "max_chars": max_chars,
            },
        ],
        continue_on_none=True,
    )

    sqlite_selected_range = None
    if method_name:
        backend_method = f"sqlite_client.{method_name}"
        if isinstance(raw_memory, dict):
            content = str(
                raw_memory.get(
                    "content",
                    raw_memory.get("segment", raw_memory.get("text", "")),
                )
            )
            memory_id = raw_memory.get("id", raw_memory.get("memory_id"))
            raw_priority = raw_memory.get("priority")
            memory_priority = raw_priority if isinstance(raw_priority, int) else None
            raw_updated_at = raw_memory.get("created_at", raw_memory.get("updated_at"))
            memory_updated_at = (
                str(raw_updated_at)
                if raw_updated_at is not None and str(raw_updated_at).strip()
                else None
            )
            sqlite_selected_range = (
                raw_memory.get("selection")
                or raw_memory.get("selected_range")
                or raw_memory.get("char_range")
            )
        elif isinstance(raw_memory, str):
            content = raw_memory
        else:
            degraded_reasons.append(
                "sqlite_client partial-read API returned unsupported payload shape."
            )
    else:
        degraded_reasons.append(
            "sqlite_client partial-read API unavailable; used local slicing fallback."
        )

    if not content or memory_priority is None or memory_updated_at is None:
        memory = None
        get_memory_by_path = getattr(client, "get_memory_by_path", None)
        if callable(get_memory_by_path):
            try:
                memory = await get_memory_by_path(
                    path,
                    domain,
                    reinforce_access=False,
                )
            except TypeError:
                memory = await get_memory_by_path(path, domain)
        if memory:
            memory_id = memory.get("id", memory_id)
            if memory_priority is None:
                raw_priority = memory.get("priority")
                memory_priority = raw_priority if isinstance(raw_priority, int) else None
            if memory_updated_at is None:
                raw_updated_at = memory.get("created_at", memory.get("updated_at"))
                memory_updated_at = (
                    str(raw_updated_at)
                    if raw_updated_at is not None and str(raw_updated_at).strip()
                    else None
                )
            if not content:
                content = str(memory.get("content", ""))
        elif not content:
            return _partial_error(f"URI '{make_uri(domain, path)}' not found.")

    if sqlite_selected_range:
        if isinstance(sqlite_selected_range, (list, tuple)) and len(sqlite_selected_range) >= 2:
            selection_meta = {
                "mode": "sqlite_char_range",
                "start": int(sqlite_selected_range[0]),
                "end": int(sqlite_selected_range[1]),
                "selected_chars": len(content),
                "total_chars": len(content),
                "truncated_by_max_chars": False,
            }
        elif isinstance(sqlite_selected_range, dict):
            selection_meta = sqlite_selected_range
        else:
            selection_meta = {
                "mode": "sqlite_selection",
                "selected_chars": len(content),
                "truncated_by_max_chars": False,
            }
        selected = content
        if max_chars is not None and len(selected) > max_chars:
            selected = selected[:max_chars]
            degraded_reasons.append(
                "max_chars was applied in MCP layer after sqlite_client partial read."
            )
            selection_meta = {
                "mode": "sqlite_slice_with_max_chars",
                "start": 0,
                "end": len(selected),
                "selected_chars": len(selected),
                "total_chars": len(content),
                "truncated_by_max_chars": True,
            }
    else:
        selected, selection_meta = slice_text_content(
            content=content,
            chunk_id=chunk_id,
            range_spec=parsed_range,
            max_chars=max_chars,
        )

    ancestors_payload: List[Dict[str, Any]] = []
    if include_ancestors_flag:
        try:
            ancestors_payload = await collect_ancestor_memories(
                client,
                domain=domain,
                path=path,
            )
        except Exception as exc:
            degraded_reasons.append("include_ancestors_lookup_failed")
            logger.debug(
                "read_memory include_ancestors lookup failed for %s: %s",
                make_uri(domain, path),
                exc,
            )

    payload = {
        "ok": True,
        "uri": make_uri(domain, path),
        "memory_id": memory_id,
        "source": content_source,
        "backend_method": backend_method,
        "selection": selection_meta,
        "content": selected,
        "include_ancestors": include_ancestors_flag,
        "degraded": bool(degraded_reasons),
    }
    if include_ancestors_flag:
        payload["ancestors"] = ancestors_payload
    if degraded_reasons:
        payload["degrade_reasons"] = list(dict.fromkeys(degraded_reasons))

    post_read_degrade_reasons: List[str] = []
    try:
        await record_session_hit(
            uri=make_uri(domain, path),
            memory_id=memory_id,
            snippet=selected[:300],
            priority=memory_priority,
            source="read_memory_partial",
            updated_at=memory_updated_at,
        )
    except Exception as exc:
        logger.warning(
            "read_memory partial session hit recording failed for %s: %s",
            make_uri(domain, path),
            exc,
        )
        post_read_degrade_reasons.append("record_session_hit_failed")
    try:
        await record_flush_event(f"read-partial {make_uri(domain, path)}")
    except Exception as exc:
        logger.warning(
            "read_memory partial flush recording failed for %s: %s",
            make_uri(domain, path),
            exc,
        )
        post_read_degrade_reasons.append("record_flush_event_failed")

    if post_read_degrade_reasons:
        degraded_reasons.extend(post_read_degrade_reasons)
        payload["degraded"] = True
        payload["degrade_reasons"] = list(dict.fromkeys(degraded_reasons))

    return to_json(payload)
