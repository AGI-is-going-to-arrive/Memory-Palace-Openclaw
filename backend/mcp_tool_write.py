import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from db.sqlite_paths import (
    is_valid_memory_path_segment,
    memory_path_segment_error_message,
)

logger = logging.getLogger(__name__)


def _log_follow_up_failure(operation: str, uri: str, exc: Exception) -> None:
    logger.warning(
        "Non-fatal follow-up failure after successful %s for %s: %s",
        operation,
        uri,
        exc,
        exc_info=exc,
    )


def _log_internal_failure(operation: str, exc: Exception) -> None:
    logger.exception("%s failed: %s", operation, exc, exc_info=exc)


def _internal_error_message(operation: str) -> str:
    return f"Error: {operation} failed. Check server logs for details."


def _guard_target_differs(
    guard_decision: Dict[str, Any], requested_uri: Optional[str]
) -> bool:
    target_uri = guard_decision.get("target_uri")
    return (
        isinstance(target_uri, str)
        and bool(target_uri)
        and target_uri != requested_uri
    )


def _parse_write_lane_timeout_seconds(message: str) -> Optional[float]:
    matched = re.search(r"after\s+([0-9]+(?:\.[0-9]+)?)s", str(message or "").lower())
    if not matched:
        return None
    try:
        return float(matched.group(1))
    except ValueError:
        return None


def _write_lane_timeout_response(
    *,
    tool_response: Callable[..., str],
    success_field: str,
    message: str,
    uri: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    timeout_seconds = _parse_write_lane_timeout_seconds(message)
    payload: Dict[str, Any] = {
        success_field: False,
        "reason": "write_lane_timeout",
        "retryable": True,
    }
    if uri:
        payload["uri"] = uri
    if timeout_seconds is not None:
        payload["timeout_seconds"] = timeout_seconds
        payload["retry_after_seconds"] = timeout_seconds
    if extra:
        payload.update(extra)
    return tool_response(
        ok=False,
        message=f"Error: {message}. Wait for the current write to finish and retry.",
        **payload,
    )


async def create_memory_impl(
    parent_uri: str,
    content: str,
    priority: int,
    title: Optional[str] = None,
    disclosure: str = "",
    *,
    get_sqlite_client: Callable[[], Any],
    parse_uri: Callable[[str], tuple[str, str]],
    make_uri: Callable[[str, str], str],
    validate_writable_domain: Callable[..., None],
    normalize_guard_decision: Callable[..., Dict[str, Any]],
    guard_fields: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_response: Callable[..., str],
    requested_create_uri: Callable[[str, str, Optional[str]], Optional[str]],
    is_forced_visual_variant_create: Callable[[str, Optional[str]], bool],
    is_forced_visual_distinct_create: Callable[[str, Optional[str]], bool],
    is_forced_visual_namespace_create: Callable[[str, Optional[str]], bool],
    is_forced_memory_palace_namespace_create: Callable[[str, Optional[str]], bool],
    is_forced_host_bridge_create: Callable[[str, Optional[str]], bool],
    is_forced_explicit_memory_create: Callable[[str, Optional[str]], bool],
    is_forced_durable_synthesis_current_create: Callable[[str, Optional[str]], bool],
    is_forced_durable_synthesis_variant_create: Callable[[str, Optional[str]], bool],
    snapshot_path_create: Callable[..., Awaitable[bool]],
    record_guard_event: Callable[..., Awaitable[None]],
    run_write_lane: Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]],
    should_defer_index_on_write: Callable[[], Awaitable[bool]],
    enqueue_index_targets: Callable[..., Awaitable[Dict[str, List[Dict[str, Any]]]]],
    record_session_hit: Callable[..., Awaitable[None]],
    record_flush_event: Callable[[str], Awaitable[None]],
    maybe_auto_flush: Callable[..., Awaitable[None]],
) -> str:
    client = get_sqlite_client()
    guard_decision = normalize_guard_decision(
        {"action": "ADD", "method": "none", "reason": "guard_not_evaluated"}
    )

    try:
        if title:
            if not is_valid_memory_path_segment(title):
                return tool_response(
                    ok=False,
                    message=f"Error: {memory_path_segment_error_message()}",
                    created=False,
                    **guard_fields(guard_decision),
                )

        domain, parent_path = parse_uri(parent_uri)
        validate_writable_domain(
            domain,
            operation="create_memory",
            uri=parent_uri,
        )
        requested_uri = requested_create_uri(domain, parent_path, title)
        defer_index = await should_defer_index_on_write()

        async def _write_task():
            local_guard_decision = guard_decision
            try:
                local_guard_decision = normalize_guard_decision(
                    await client.write_guard(
                        content=content,
                        domain=domain,
                        path_prefix=parent_path if parent_path else None,
                    )
                )
            except Exception as guard_exc:
                local_guard_decision = normalize_guard_decision(
                    {
                        "action": "NOOP",
                        "method": "exception",
                        "reason": f"write_guard_unavailable: {guard_exc}",
                        "degraded": True,
                        "degrade_reasons": ["write_guard_exception"],
                    }
                )

            guard_action = str(local_guard_decision.get("action") or "NOOP").upper()
            blocked = guard_action != "ADD"

            # C-4 fix: Never allow content-marker overrides when the guard
            # itself was unavailable or returned an invalid result.  These are
            # fail-closed safety states that must not be bypassable by any
            # caller-controlled content markers.
            _guard_reason = str(local_guard_decision.get("reason") or "").lower()
            _guard_is_fail_closed = (
                _guard_reason.startswith("write_guard_unavailable")
                or _guard_reason.startswith("invalid_guard_action:")
            )

            if (
                blocked
                and not _guard_is_fail_closed
                and is_forced_visual_variant_create(content, requested_uri)
                and _guard_target_differs(local_guard_decision, requested_uri)
            ):
                local_guard_decision = normalize_guard_decision(
                    {
                        **local_guard_decision,
                        "action": "ADD",
                        "method": "visual_variant_override",
                        "reason": (
                            "visual_duplicate_new_forced_variant:"
                            f"{guard_action.lower()}->{requested_uri}"
                        ),
                        "target_uri": requested_uri,
                    }
                )
                guard_action = "ADD"
                blocked = False
            elif (
                blocked
                and not _guard_is_fail_closed
                and is_forced_visual_distinct_create(content, requested_uri)
                and _guard_target_differs(local_guard_decision, requested_uri)
            ):
                local_guard_decision = normalize_guard_decision(
                    {
                        **local_guard_decision,
                        "action": "ADD",
                        "method": "visual_distinct_override",
                        "reason": (
                            "visual_force_create_distinct_record:"
                            f"{guard_action.lower()}->{requested_uri}"
                        ),
                        "target_uri": requested_uri,
                    }
                )
                guard_action = "ADD"
                blocked = False
            elif (
                blocked
                and not _guard_is_fail_closed
                and is_forced_visual_namespace_create(content, requested_uri)
                and _guard_target_differs(local_guard_decision, requested_uri)
            ):
                local_guard_decision = normalize_guard_decision(
                    {
                        **local_guard_decision,
                        "action": "ADD",
                        "method": "visual_namespace_override",
                        "reason": (
                            "visual_namespace_container_override:"
                            f"{guard_action.lower()}->{requested_uri}"
                        ),
                        "target_uri": requested_uri,
                    }
                )
                guard_action = "ADD"
                blocked = False
            elif (
                blocked
                and not _guard_is_fail_closed
                and is_forced_memory_palace_namespace_create(content, requested_uri)
                and _guard_target_differs(local_guard_decision, requested_uri)
            ):
                local_guard_decision = normalize_guard_decision(
                    {
                        **local_guard_decision,
                        "action": "ADD",
                        "method": "memory_palace_namespace_override",
                        "reason": (
                            "memory_palace_namespace_override:"
                            f"{guard_action.lower()}->{requested_uri}"
                        ),
                        "target_uri": requested_uri,
                    }
                )
                guard_action = "ADD"
                blocked = False
            elif (
                blocked
                and not _guard_is_fail_closed
                and is_forced_host_bridge_create(content, requested_uri)
                and _guard_target_differs(local_guard_decision, requested_uri)
            ):
                local_guard_decision = normalize_guard_decision(
                    {
                        **local_guard_decision,
                        "action": "ADD",
                        "method": "host_bridge_override",
                        "reason": (
                            "host_bridge_force_create:"
                            f"{guard_action.lower()}->{requested_uri}"
                        ),
                        "target_uri": requested_uri,
                    }
                )
                guard_action = "ADD"
                blocked = False
            elif (
                blocked
                and not _guard_is_fail_closed
                and is_forced_explicit_memory_create(content, requested_uri)
                and _guard_target_differs(local_guard_decision, requested_uri)
            ):
                local_guard_decision = normalize_guard_decision(
                    {
                        **local_guard_decision,
                        "action": "ADD",
                        "method": "explicit_memory_force_override",
                        "reason": (
                            "explicit_memory_force_create:"
                            f"{guard_action.lower()}->{requested_uri}"
                        ),
                        "target_uri": requested_uri,
                    }
                )
                guard_action = "ADD"
                blocked = False
            elif (
                blocked
                and not _guard_is_fail_closed
                and is_forced_durable_synthesis_current_create(content, requested_uri)
                and _guard_target_differs(local_guard_decision, requested_uri)
            ):
                local_guard_decision = normalize_guard_decision(
                    {
                        **local_guard_decision,
                        "action": "ADD",
                        "method": "durable_synthesis_current_override",
                        "reason": (
                            "durable_synthesis_force_current:"
                            f"{guard_action.lower()}->{requested_uri}"
                        ),
                        "target_uri": requested_uri,
                    }
                )
                guard_action = "ADD"
                blocked = False
            elif (
                blocked
                and not _guard_is_fail_closed
                and is_forced_durable_synthesis_variant_create(content, requested_uri)
                and _guard_target_differs(local_guard_decision, requested_uri)
            ):
                local_guard_decision = normalize_guard_decision(
                    {
                        **local_guard_decision,
                        "action": "ADD",
                        "method": "durable_synthesis_variant_override",
                        "reason": (
                            "durable_synthesis_force_variant:"
                            f"{guard_action.lower()}->{requested_uri}"
                        ),
                        "target_uri": requested_uri,
                    }
                )
                guard_action = "ADD"
                blocked = False
            try:
                await record_guard_event(
                    operation="create_memory",
                    decision=local_guard_decision,
                    blocked=blocked,
                )
            except Exception:
                pass
            if blocked:
                return {
                    "blocked": True,
                    "guard_decision": local_guard_decision,
                }
            # Strip force-create control trailers before persisting so
            # internal metadata never pollutes the stored content or index.
            from mcp_force_create import strip_force_control_trailer
            clean_content = strip_force_control_trailer(content)
            result = await client.create_memory(
                parent_path=parent_path,
                content=clean_content,
                priority=priority,
                title=title,
                disclosure=disclosure if disclosure else None,
                domain=domain,
                index_now=not defer_index,
            )
            created_uri = result.get("uri", make_uri(domain, result["path"]))
            await snapshot_path_create(created_uri, result["id"], operation_type="create")
            return {
                "blocked": False,
                "guard_decision": local_guard_decision,
                "clean_content": clean_content,
                "result": result,
            }

        write_outcome = await run_write_lane("create_memory", _write_task)
        guard_decision = normalize_guard_decision(write_outcome.get("guard_decision"))
        if write_outcome.get("blocked"):
            target_uri = guard_decision.get("target_uri")
            message = (
                "Skipped: write_guard blocked create_memory "
                f"(action={guard_decision.get('action')}, method={guard_decision.get('method')})."
            )
            if isinstance(target_uri, str) and target_uri:
                message += f" suggested_target={target_uri}"
            return tool_response(
                ok=False,
                message=message,
                created=False,
                reason="write_guard_blocked",
                uri=target_uri,
                **guard_fields(guard_decision),
            )
        result = write_outcome.get("result") or {}
        clean_content = str(write_outcome.get("clean_content") or content)
        index_enqueue = {"queued": [], "dropped": [], "deduped": []}
        if defer_index:
            index_enqueue = await enqueue_index_targets(result, reason="create_memory")
        created_uri = result.get("uri", make_uri(domain, result["path"]))
        try:
            await record_session_hit(
                uri=created_uri,
                memory_id=result.get("id"),
                snippet=clean_content[:300],
                priority=priority,
                source="create_memory",
            )
            await record_flush_event(f"create {created_uri}")
            await maybe_auto_flush(client, reason="create_memory")
        except Exception as exc:
            _log_follow_up_failure("create_memory", created_uri, exc)

        queued_count = len(index_enqueue["queued"])
        dropped_count = len(index_enqueue["dropped"])
        deduped_count = len(index_enqueue["deduped"])
        if queued_count or dropped_count or deduped_count:
            index_parts: List[str] = []
            if queued_count:
                index_parts.append(f"index queued: {queued_count} task")
            if dropped_count:
                index_parts.append(f"index dropped: {dropped_count} task")
            if deduped_count:
                index_parts.append(f"index deduped: {deduped_count} task")
            return tool_response(
                ok=True,
                message=(
                    f"Success: Memory created at '{created_uri}' "
                    f"({'; '.join(index_parts)})"
                ),
                created=True,
                uri=created_uri,
                index_queued=queued_count,
                index_dropped=dropped_count,
                index_deduped=deduped_count,
                **guard_fields(guard_decision),
            )
        return tool_response(
            ok=True,
            message=f"Success: Memory created at '{created_uri}'",
            created=True,
            uri=created_uri,
            index_queued=0,
            index_dropped=0,
            index_deduped=0,
            **guard_fields(guard_decision),
        )

    except ValueError as e:
        return tool_response(
            ok=False,
            message=f"Error: {str(e)}",
            created=False,
            **guard_fields(guard_decision),
        )
    except TimeoutError as e:
        return _write_lane_timeout_response(
            tool_response=tool_response,
            success_field="created",
            message=str(e),
            uri=requested_uri if "requested_uri" in locals() and requested_uri else parent_uri,
            extra=guard_fields(guard_decision),
        )
    except Exception as e:
        _log_internal_failure("create_memory", e)
        return tool_response(
            ok=False,
            message=_internal_error_message("create_memory"),
            created=False,
            **guard_fields(guard_decision),
        )


async def ensure_visual_namespace_chain_impl(
    target_uri: str,
    *,
    get_sqlite_client: Callable[[], Any],
    parse_uri: Callable[[str], tuple[str, str]],
    make_uri: Callable[[str, str], str],
    build_visual_namespace_chain_content: Callable[[str, List[str]], str],
    tool_response: Callable[..., str],
    run_write_lane: Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]],
) -> str:
    client = get_sqlite_client()
    try:
        domain, path = parse_uri(target_uri)
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) <= 1:
            return tool_response(
                ok=True,
                message="No namespace chain required.",
                created_paths=[],
                existing_paths=[],
            )

        async def _write_task() -> Dict[str, List[str]]:
            created_paths: List[str] = []
            existing_paths: List[str] = []
            for index in range(0, len(segments) - 1):
                current_segments = segments[: index + 1]
                current_path = "/".join(current_segments)
                current_uri = make_uri(domain, current_path)
                existing = await client.get_memory_by_path(
                    current_path,
                    domain=domain,
                    reinforce_access=False,
                )
                if existing is not None:
                    existing_paths.append(current_uri)
                    continue

                parent_path = "/".join(current_segments[:-1])
                created = await client.create_memory(
                    parent_path=parent_path,
                    content=build_visual_namespace_chain_content(domain, current_segments),
                    priority=5,
                    title=current_segments[-1],
                    disclosure="Internal namespace container for visual-memory records",
                    domain=domain,
                    index_now=False,
                )
                created_paths.append(str(created.get("uri") or current_uri))
            return {
                "created_paths": created_paths,
                "existing_paths": existing_paths,
            }

        payload = await run_write_lane("ensure_visual_namespace_chain", _write_task)

        return tool_response(
            ok=True,
            message="Visual namespace chain ensured.",
            created_paths=payload.get("created_paths", []),
            existing_paths=payload.get("existing_paths", []),
        )
    except Exception as exc:
        return tool_response(
            ok=False,
            message=f"Error: {exc}",
            created_paths=[],
            existing_paths=[],
        )


async def update_memory_impl(
    uri: str,
    old_string: Optional[str] = None,
    new_string: Optional[str] = None,
    append: Optional[str] = None,
    priority: Optional[int] = None,
    disclosure: Optional[str] = None,
    *,
    get_sqlite_client: Callable[[], Any],
    parse_uri: Callable[[str], tuple[str, str]],
    make_uri: Callable[[str, str], str],
    validate_writable_domain: Callable[..., None],
    normalize_guard_decision: Callable[..., Dict[str, Any]],
    guard_fields: Callable[[Dict[str, Any]], Dict[str, Any]],
    tool_response: Callable[..., str],
    snapshot_memory_content: Callable[[str], Awaitable[bool]],
    snapshot_path_meta: Callable[[str], Awaitable[bool]],
    record_guard_event: Callable[..., Awaitable[None]],
    run_write_lane: Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]],
    should_defer_index_on_write: Callable[[], Awaitable[bool]],
    enqueue_index_targets: Callable[..., Awaitable[Dict[str, List[Dict[str, Any]]]]],
    record_session_hit: Callable[..., Awaitable[None]],
    record_flush_event: Callable[[str], Awaitable[None]],
    maybe_auto_flush: Callable[..., Awaitable[None]],
) -> str:
    guard_decision = normalize_guard_decision(
        {"action": "BYPASS", "method": "none", "reason": "guard_not_evaluated"},
        allow_bypass=True,
    )

    try:
        domain, path = parse_uri(uri)
        full_uri = make_uri(domain, path)
        validate_writable_domain(
            domain,
            operation="update_memory",
            uri=full_uri,
        )
        current_memory_id: Optional[int] = None

        if old_string is not None and append is not None:
            return tool_response(
                ok=False,
                message=(
                    "Error: Cannot use both old_string/new_string (patch) and append "
                    "at the same time. Pick one."
                ),
                updated=False,
                uri=full_uri,
                **guard_fields(guard_decision),
            )

        if old_string is not None and new_string is None:
            return tool_response(
                ok=False,
                message=(
                    'Error: old_string provided without new_string. '
                    'To delete a section, use new_string="".'
                ),
                updated=False,
                uri=full_uri,
                **guard_fields(guard_decision),
            )

        if new_string is not None and old_string is None:
            return tool_response(
                ok=False,
                message=(
                    "Error: new_string provided without old_string. "
                    "Both are required for patch mode."
                ),
                updated=False,
                uri=full_uri,
                **guard_fields(guard_decision),
            )

        if old_string is not None and old_string == new_string:
            return tool_response(
                ok=False,
                message=(
                    "Error: old_string and new_string are identical. "
                    "No change would be made."
                ),
                updated=False,
                uri=full_uri,
                **guard_fields(guard_decision),
            )

        if append is not None and not append:
            return tool_response(
                ok=False,
                message=(
                    f"Error: Empty append for '{full_uri}'. "
                    "Provide non-empty text to append."
                ),
                updated=False,
                uri=full_uri,
                **guard_fields(guard_decision),
            )

        if old_string is None and append is None and priority is None and disclosure is None:
            return tool_response(
                ok=False,
                message=(
                    f"Error: No update fields provided for '{full_uri}'. "
                    "Use patch mode (old_string + new_string), append mode (append), "
                    "or metadata fields (priority/disclosure)."
                ),
                updated=False,
                uri=full_uri,
                **guard_fields(guard_decision),
            )

        client = get_sqlite_client()
        defer_index = await should_defer_index_on_write()

        async def _write_task():
            local_guard_decision = guard_decision
            local_current_memory_id = current_memory_id
            local_content: Optional[str] = None

            if old_string is not None or append is not None:
                memory = await client.get_memory_by_path(path, domain)
                if not memory:
                    return {
                        "failed": True,
                        "message": f"Error: Memory at '{full_uri}' not found.",
                    }

                local_current_memory_id = memory.get("id")
                current_content = memory.get("content", "")

                if old_string is not None:
                    count = current_content.count(old_string)
                    if count == 0:
                        return {
                            "failed": True,
                            "message": (
                                f"Error: old_string not found in memory content at '{full_uri}'. "
                                "Make sure it matches the existing text exactly."
                            ),
                        }
                    if count > 1:
                        return {
                            "failed": True,
                            "message": (
                                f"Error: old_string found {count} times in memory content at '{full_uri}'. "
                                "Provide more surrounding context to make it unique."
                            ),
                        }
                    local_content = current_content.replace(old_string, new_string, 1)
                    if local_content == current_content:
                        return {
                            "failed": True,
                            "message": (
                                f"Error: Replacement produced identical content at '{full_uri}'. "
                                "The old_string was found but replacing it with new_string "
                                "resulted in no change. Check for subtle whitespace differences."
                            ),
                        }
                else:
                    local_content = current_content + append

            if local_content is not None:
                try:
                    local_guard_decision = normalize_guard_decision(
                        await client.write_guard(
                            content=local_content,
                            domain=domain,
                            path_prefix=path.rsplit("/", 1)[0] if "/" in path else None,
                            exclude_memory_id=local_current_memory_id,
                        )
                    )
                except Exception as guard_exc:
                    local_guard_decision = normalize_guard_decision(
                        {
                            "action": "NOOP",
                            "method": "exception",
                            "reason": f"write_guard_unavailable: {guard_exc}",
                            "degraded": True,
                            "degrade_reasons": ["write_guard_exception"],
                        }
                    )
            else:
                local_guard_decision = normalize_guard_decision(
                    {
                        "action": "BYPASS",
                        "method": "none",
                        "reason": "metadata_only_update",
                    },
                    allow_bypass=True,
            )

            guard_action = str(local_guard_decision.get("action") or "NOOP").upper()
            blocked = False
            if local_content is not None:
                if guard_action == "ADD":
                    blocked = False
                elif guard_action == "UPDATE":
                    target_id = local_guard_decision.get("target_id")
                    if (
                        not isinstance(target_id, int)
                        or not isinstance(local_current_memory_id, int)
                        or target_id != local_current_memory_id
                    ):
                        blocked = True
                else:
                    blocked = True
            try:
                await record_guard_event(
                    operation="update_memory",
                    decision=local_guard_decision,
                    blocked=blocked,
                )
            except Exception:
                pass
            if blocked:
                return {
                    "blocked": True,
                    "guard_decision": local_guard_decision,
                }
            if local_content is not None:
                await snapshot_memory_content(full_uri)
            if priority is not None or disclosure is not None:
                await snapshot_path_meta(full_uri)
            return {
                "failed": False,
                "blocked": False,
                "guard_decision": local_guard_decision,
                "content_preview": local_content,
                "result": await client.update_memory(
                    path=path,
                    content=local_content,
                    priority=priority,
                    disclosure=disclosure,
                    domain=domain,
                    index_now=not defer_index,
                    expected_old_id=local_current_memory_id,
                ),
            }

        write_outcome = await run_write_lane("update_memory", _write_task)
        guard_decision = normalize_guard_decision(
            write_outcome.get("guard_decision"), allow_bypass=True
        )
        if write_outcome.get("failed"):
            return tool_response(
                ok=False,
                message=str(write_outcome.get("message") or "Error: update_memory failed."),
                updated=False,
                uri=full_uri,
                **guard_fields(guard_decision),
            )
        if write_outcome.get("blocked"):
            return tool_response(
                ok=False,
                message=(
                    "Skipped: write_guard blocked update_memory "
                    f"(action={guard_decision.get('action')}, method={guard_decision.get('method')})."
                ),
                updated=False,
                reason="write_guard_blocked",
                uri=full_uri,
                **guard_fields(guard_decision),
            )
        update_result = write_outcome.get("result") or {}
        index_enqueue = {"queued": [], "dropped": [], "deduped": []}
        if defer_index:
            index_enqueue = await enqueue_index_targets(
                update_result, reason="update_memory"
            )

        preview_text = write_outcome.get("content_preview")
        if preview_text is None:
            preview_text = (
                f"meta update priority={priority if priority is not None else '(unchanged)'} "
                f"disclosure={disclosure if disclosure is not None else '(unchanged)'}"
            )
        try:
            await record_session_hit(
                uri=full_uri,
                memory_id=(
                    update_result.get("new_memory_id")
                    if isinstance(update_result, dict)
                    else None
                ),
                snippet=str(preview_text)[:300],
                priority=priority,
                source="update_memory",
            )
            await record_flush_event(f"update {full_uri}")
            await maybe_auto_flush(client, reason="update_memory")
        except Exception as exc:
            _log_follow_up_failure("update_memory", full_uri, exc)

        queued_count = len(index_enqueue["queued"])
        dropped_count = len(index_enqueue["dropped"])
        deduped_count = len(index_enqueue["deduped"])
        if queued_count or dropped_count or deduped_count:
            index_parts: List[str] = []
            if queued_count:
                index_parts.append(f"index queued: {queued_count} task")
            if dropped_count:
                index_parts.append(f"index dropped: {dropped_count} task")
            if deduped_count:
                index_parts.append(f"index deduped: {deduped_count} task")
            return tool_response(
                ok=True,
                message=(
                    f"Success: Memory at '{full_uri}' updated "
                    f"({'; '.join(index_parts)})"
                ),
                updated=True,
                uri=full_uri,
                index_queued=queued_count,
                index_dropped=dropped_count,
                index_deduped=deduped_count,
                **guard_fields(guard_decision),
            )
        return tool_response(
            ok=True,
            message=f"Success: Memory at '{full_uri}' updated",
            updated=True,
            uri=full_uri,
            index_queued=0,
            index_dropped=0,
            index_deduped=0,
            **guard_fields(guard_decision),
        )

    except ValueError as e:
        return tool_response(
            ok=False,
            message=f"Error: {str(e)}",
            updated=False,
            **guard_fields(guard_decision),
        )
    except TimeoutError as e:
        return _write_lane_timeout_response(
            tool_response=tool_response,
            success_field="updated",
            message=str(e),
            uri=full_uri if "full_uri" in locals() else uri,
            extra=guard_fields(guard_decision),
        )
    except Exception as e:
        _log_internal_failure("update_memory", e)
        return tool_response(
            ok=False,
            message=_internal_error_message("update_memory"),
            updated=False,
            **guard_fields(guard_decision),
        )


async def delete_memory_impl(
    uri: str,
    *,
    get_sqlite_client: Callable[[], Any],
    parse_uri: Callable[[str], tuple[str, str]],
    make_uri: Callable[[str, str], str],
    validate_writable_domain: Callable[..., None],
    snapshot_path_delete: Callable[[str], Awaitable[bool]],
    run_write_lane: Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]],
    record_session_hit: Callable[..., Awaitable[None]],
    record_flush_event: Callable[[str], Awaitable[None]],
    maybe_auto_flush: Callable[..., Awaitable[None]],
    event_preview: Callable[[str], str],
    tool_response: Callable[..., str],
) -> str:
    client = get_sqlite_client()

    try:
        domain, path = parse_uri(uri)
        full_uri = make_uri(domain, path)
        validate_writable_domain(
            domain,
            operation="delete_memory",
            uri=full_uri,
        )

        async def _write_task():
            memory = await client.get_memory_by_path(path, domain)
            if not memory:
                return {"removed": False}
            await snapshot_path_delete(full_uri)
            await client.remove_path(path, domain)
            return {
                "removed": True,
                "memory": memory,
            }

        remove_result = await run_write_lane("delete_memory", _write_task)
        if not remove_result.get("removed"):
            return tool_response(
                ok=False,
                message=f"Error: Memory at '{full_uri}' not found.",
                deleted=False,
                uri=full_uri,
            )
        memory = remove_result.get("memory") or {}

        try:
            await record_session_hit(
                uri=full_uri,
                memory_id=memory.get("id"),
                snippet=f"[deleted] {event_preview(str(memory.get('content', '')))}",
                priority=memory.get("priority"),
                source="delete_memory",
                updated_at=memory.get("created_at"),
            )
            await record_flush_event(f"delete {full_uri}")
            await maybe_auto_flush(client, reason="delete_memory")
        except Exception as exc:
            _log_follow_up_failure("delete_memory", full_uri, exc)

        return tool_response(
            ok=True,
            message=f"Success: Memory '{full_uri}' deleted.",
            deleted=True,
            uri=full_uri,
        )

    except ValueError as e:
        return tool_response(
            ok=False,
            message=f"Error: {str(e)}",
            deleted=False,
        )
    except TimeoutError as e:
        return _write_lane_timeout_response(
            tool_response=tool_response,
            success_field="deleted",
            message=str(e),
            uri=full_uri if "full_uri" in locals() else uri,
        )
    except Exception as e:
        _log_internal_failure("delete_memory", e)
        return tool_response(
            ok=False,
            message=_internal_error_message("delete_memory"),
            deleted=False,
        )


async def add_alias_impl(
    new_uri: str,
    target_uri: str,
    priority: int = 0,
    disclosure: Optional[str] = None,
    *,
    get_sqlite_client: Callable[[], Any],
    parse_uri: Callable[[str], tuple[str, str]],
    validate_writable_domain: Callable[..., None],
    snapshot_path_create: Callable[..., Awaitable[bool]],
    run_write_lane: Callable[[str, Callable[[], Awaitable[Any]]], Awaitable[Any]],
    record_session_hit: Callable[..., Awaitable[None]],
    record_flush_event: Callable[[str], Awaitable[None]],
    maybe_auto_flush: Callable[..., Awaitable[None]],
) -> str:
    client = get_sqlite_client()

    try:
        new_domain, new_path = parse_uri(new_uri)
        target_domain, target_path = parse_uri(target_uri)
        validate_writable_domain(
            new_domain,
            operation="add_alias",
            uri=new_uri,
        )
        validate_writable_domain(
            target_domain,
            operation="add_alias",
            uri=target_uri,
        )

        async def _write_task():
            result = await client.add_path(
                new_path=new_path,
                target_path=target_path,
                new_domain=new_domain,
                target_domain=target_domain,
                priority=priority,
                disclosure=disclosure,
            )
            await snapshot_path_create(
                uri=result["new_uri"],
                memory_id=result["memory_id"],
                operation_type="create_alias",
                target_uri=result["target_uri"],
            )
            return result

        result = await run_write_lane("add_alias", _write_task)

        try:
            await record_session_hit(
                uri=result["new_uri"],
                memory_id=result.get("memory_id"),
                snippet=f"[alias] {result['new_uri']} -> {result['target_uri']}",
                priority=priority,
                source="add_alias",
            )
            await record_flush_event(
                f"add-alias {result['new_uri']} -> {result['target_uri']}"
            )
            await maybe_auto_flush(client, reason="add_alias")
        except Exception as exc:
            _log_follow_up_failure("add_alias", result["new_uri"], exc)

        return (
            f"Success: Alias '{result['new_uri']}' now points to same memory as "
            f"'{result['target_uri']}'"
        )

    except ValueError as e:
        return f"Error: {str(e)}"
    except TimeoutError as e:
        return f"Error: {str(e)}. Wait for the current write to finish and retry."
    except Exception as e:
        _log_internal_failure("add_alias", e)
        return _internal_error_message("add_alias")
