import asyncio
import threading
import weakref
from typing import Any, Awaitable, Callable, Dict, List

_FALLBACK_WRITE_LOCKS_GUARD = threading.Lock()
_FALLBACK_WRITE_LOCKS: dict[int, tuple[weakref.ReferenceType[asyncio.AbstractEventLoop], asyncio.Lock]] = {}


def _get_fallback_write_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    loop_key = id(loop)
    with _FALLBACK_WRITE_LOCKS_GUARD:
        stale_keys = [
            key
            for key, (loop_ref, _lock) in _FALLBACK_WRITE_LOCKS.items()
            if loop_ref() is None
        ]
        for key in stale_keys:
            _FALLBACK_WRITE_LOCKS.pop(key, None)

        entry = _FALLBACK_WRITE_LOCKS.get(loop_key)
        if entry is not None:
            loop_ref, lock = entry
            if loop_ref() is loop:
                return lock

        lock = asyncio.Lock()
        _FALLBACK_WRITE_LOCKS[loop_key] = (weakref.ref(loop), lock)
        return lock


async def record_guard_event_impl(
    *,
    runtime_state: Any,
    operation: str,
    decision: Dict[str, Any],
    blocked: bool,
) -> None:
    await runtime_state.guard_tracker.record_event(
        operation=operation,
        action=str(decision.get("action") or "UNKNOWN"),
        method=str(decision.get("method") or "unknown"),
        reason=str(decision.get("reason") or ""),
        target_id=decision.get("target_id"),
        blocked=blocked,
        degraded=bool(decision.get("degraded")),
        degrade_reasons=decision.get("degrade_reasons"),
    )


async def run_write_lane_impl(
    *,
    runtime_state: Any,
    get_sqlite_client: Callable[[], Any],
    get_session_id: Callable[[], str],
    enable_write_lane_queue: bool,
    operation: str,
    fn: Callable[[], Awaitable[Any]],
) -> Any:
    await runtime_state.ensure_started(get_sqlite_client)
    write_lanes = getattr(runtime_state, "write_lanes", None)
    if write_lanes is not None:
        return await write_lanes.run_write(
            session_id=get_session_id() if enable_write_lane_queue else None,
            operation=operation,
            task=fn,
        )
    async with _get_fallback_write_lock():
        return await fn()


async def should_defer_index_on_write_impl(
    *,
    runtime_state: Any,
    get_sqlite_client: Callable[[], Any],
    enable_index_worker: bool,
    defer_index_on_write: bool,
) -> bool:
    if not enable_index_worker or not defer_index_on_write:
        return False
    await runtime_state.ensure_started(get_sqlite_client)
    worker_status = await runtime_state.index_worker.status()
    return bool(worker_status.get("enabled") and worker_status.get("running"))


def extract_index_targets_impl(
    payload: Any, *, safe_int: Callable[[Any, int], int]
) -> List[int]:
    if not isinstance(payload, dict):
        return []
    values = payload.get("index_targets")
    if not isinstance(values, list):
        return []
    targets: List[int] = []
    for item in values:
        parsed = safe_int(item, default=-1)
        if parsed > 0:
            targets.append(parsed)
    return list(dict.fromkeys(targets))


async def enqueue_index_targets_impl(
    payload: Any,
    *,
    reason: str,
    runtime_state: Any,
    get_sqlite_client: Callable[[], Any],
    safe_int: Callable[[Any, int], int],
) -> Dict[str, List[Dict[str, Any]]]:
    targets = extract_index_targets_impl(payload, safe_int=safe_int)
    if not targets:
        return {"queued": [], "dropped": [], "deduped": []}
    await runtime_state.ensure_started(get_sqlite_client)
    queued: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    deduped: List[Dict[str, Any]] = []
    for memory_id in targets:
        item = await runtime_state.index_worker.enqueue_reindex_memory(
            memory_id=memory_id,
            reason=reason,
        )
        if item.get("queued"):
            queued.append(item)
        elif item.get("dropped"):
            dropped.append(item)
        else:
            deduped.append(item)
    return {"queued": queued, "dropped": dropped, "deduped": deduped}
