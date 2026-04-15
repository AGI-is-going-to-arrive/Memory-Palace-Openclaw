from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException

from db import get_sqlite_client
from runtime_state import runtime_state

from .maintenance_models import IndexJobCancelRequest, IndexJobRetryRequest


def _raise_on_enqueue_drop(result: Dict[str, Any], *, operation: str) -> None:
    if not isinstance(result, dict):
        return
    if not result.get("dropped"):
        return

    reason = str(result.get("reason") or "queue_full")
    status_code = 503 if reason == "queue_full" else 409
    detail: Dict[str, Any] = {
        "error": "index_job_enqueue_failed",
        "reason": reason,
        "operation": operation,
    }
    job_id = result.get("job_id")
    if isinstance(job_id, str) and job_id:
        detail["job_id"] = job_id
    raise HTTPException(status_code=status_code, detail=detail)


async def get_index_worker_status():
    await runtime_state.ensure_started(get_sqlite_client)
    return await runtime_state.index_worker.status()


async def get_index_job(job_id: str):
    await runtime_state.ensure_started(get_sqlite_client)
    result = await runtime_state.index_worker.get_job(job_id=job_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=str(result.get("error") or "job not found"))
    result["runtime_worker"] = await runtime_state.index_worker.status()
    return result


async def cancel_index_job(job_id: str, payload: Optional[IndexJobCancelRequest] = None):
    await runtime_state.ensure_started(get_sqlite_client)
    reason = "api_cancel"
    if isinstance(payload, IndexJobCancelRequest):
        reason = payload.reason

    result = await runtime_state.index_worker.cancel_job(job_id=job_id, reason=reason)
    if not result.get("ok"):
        error = str(result.get("error") or "job cancellation failed")
        normalized_error = error.lower()
        status_code = (
            404
            if "not found" in normalized_error or "job_not_found" in normalized_error
            else 409
        )
        raise HTTPException(status_code=status_code, detail=error)
    result["runtime_worker"] = await runtime_state.index_worker.status()
    return result


async def retry_index_job(job_id: str, payload: Optional[IndexJobRetryRequest] = None):
    await runtime_state.ensure_started(get_sqlite_client)
    original_result = await runtime_state.index_worker.get_job(job_id=job_id)
    if not original_result.get("ok"):
        raise HTTPException(status_code=404, detail=str(original_result.get("error") or "job not found"))

    original_job = original_result.get("job") or {}
    task_type = str(original_job.get("task_type") or "").strip()
    current_status = str(original_job.get("status") or "").strip().lower()
    if current_status not in {"failed", "dropped", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "job_retry_not_allowed",
                "reason": f"status:{current_status or 'unknown'}",
                "job_id": job_id,
                "task_type": task_type or "unknown",
            },
        )

    retry_reason = f"retry:{job_id}"
    if isinstance(payload, IndexJobRetryRequest) and payload.reason.strip():
        retry_reason = payload.reason.strip()

    enqueue_result: Dict[str, Any]
    if task_type == "reindex_memory":
        try:
            memory_id = int(original_job.get("memory_id") or 0)
        except (TypeError, ValueError):
            memory_id = 0
        if memory_id <= 0:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "job_retry_invalid_memory_id",
                    "job_id": job_id,
                    "task_type": task_type,
                },
            )
        enqueue_result = await runtime_state.index_worker.enqueue_reindex_memory(
            memory_id=memory_id,
            reason=retry_reason,
        )
        _raise_on_enqueue_drop(enqueue_result, operation="retry_reindex_memory")
    elif task_type == "rebuild_index":
        enqueue_result = await runtime_state.index_worker.enqueue_rebuild(reason=retry_reason)
        _raise_on_enqueue_drop(enqueue_result, operation="retry_rebuild_index")
    elif task_type == "sleep_consolidation":
        enqueue_result = await runtime_state.sleep_consolidation.schedule(
            index_worker=runtime_state.index_worker,
            force=True,
            reason=retry_reason,
        )
        _raise_on_enqueue_drop(enqueue_result, operation="retry_sleep_consolidation")
        if not enqueue_result.get("scheduled"):
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "job_retry_not_scheduled",
                    "reason": str(
                        enqueue_result.get("reason") or "sleep_consolidation_not_scheduled"
                    ),
                    "job_id": job_id,
                    "task_type": task_type,
                },
            )
    else:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "job_retry_unsupported_task_type",
                "job_id": job_id,
                "task_type": task_type or "unknown",
            },
        )

    response: Dict[str, Any] = {
        "ok": True,
        "retry_of_job_id": job_id,
        "task_type": task_type,
        "reason": retry_reason,
        **enqueue_result,
    }
    response["runtime_worker"] = await runtime_state.index_worker.status()
    if task_type == "sleep_consolidation":
        response["sleep_consolidation"] = await runtime_state.sleep_consolidation.status()
    return response


async def trigger_sleep_consolidation(
    reason: str = "api",
    wait: bool = False,
    timeout_seconds: int = 30,
):
    await runtime_state.ensure_started(get_sqlite_client)
    worker_status = await runtime_state.index_worker.status()
    if not worker_status.get("enabled"):
        raise HTTPException(status_code=409, detail="index_worker_disabled")

    schedule_result = await runtime_state.sleep_consolidation.schedule(
        index_worker=runtime_state.index_worker,
        force=True,
        reason=reason or "api",
    )
    _raise_on_enqueue_drop(schedule_result, operation="sleep_consolidation")
    if not schedule_result.get("scheduled"):
        raise HTTPException(
            status_code=409,
            detail=str(schedule_result.get("reason") or "sleep_consolidation_not_scheduled"),
        )

    payload = {"ok": True, "reason": reason or "api", **schedule_result}
    job_id = schedule_result.get("job_id")
    if wait and isinstance(job_id, str) and job_id:
        payload["wait_result"] = await runtime_state.index_worker.wait_for_job(
            job_id=job_id,
            timeout_seconds=max(1.0, float(timeout_seconds)),
        )
    payload["runtime_worker"] = await runtime_state.index_worker.status()
    payload["sleep_consolidation"] = await runtime_state.sleep_consolidation.status()
    return payload


async def rebuild_index(
    reason: str = "api",
    wait: bool = False,
    timeout_seconds: int = 30,
    *,
    client_factory: Callable[[], Any] = get_sqlite_client,
):
    client = client_factory()
    await runtime_state.ensure_started(get_sqlite_client)
    worker_status = await runtime_state.index_worker.status()

    if not worker_status.get("enabled"):
        try:
            result = await client.rebuild_index(reason=reason or "api")
            return {
                "ok": True,
                "queued": False,
                "executed_sync": True,
                "reason": reason or "api",
                "result": result,
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    enqueue_result = await runtime_state.index_worker.enqueue_rebuild(
        reason=reason or "api"
    )
    _raise_on_enqueue_drop(enqueue_result, operation="rebuild_index")
    payload = {"ok": True, "reason": reason or "api", **enqueue_result}
    job_id = enqueue_result.get("job_id")
    if wait and isinstance(job_id, str) and job_id:
        payload["wait_result"] = await runtime_state.index_worker.wait_for_job(
            job_id=job_id,
            timeout_seconds=max(1.0, float(timeout_seconds)),
        )
    payload["runtime_worker"] = await runtime_state.index_worker.status()
    return payload


async def reindex_memory(
    memory_id: int,
    reason: str = "api",
    wait: bool = False,
    timeout_seconds: int = 30,
    *,
    client_factory: Callable[[], Any] = get_sqlite_client,
):
    if memory_id <= 0:
        raise HTTPException(status_code=400, detail="memory_id must be a positive integer")

    client = client_factory()
    await runtime_state.ensure_started(get_sqlite_client)
    worker_status = await runtime_state.index_worker.status()

    if not worker_status.get("enabled"):
        try:
            result = await client.reindex_memory(
                memory_id=memory_id,
                reason=reason or "api",
            )
            return {
                "ok": True,
                "queued": False,
                "executed_sync": True,
                "memory_id": memory_id,
                "reason": reason or "api",
                "result": result,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    enqueue_result = await runtime_state.index_worker.enqueue_reindex_memory(
        memory_id=memory_id,
        reason=reason or "api",
    )
    _raise_on_enqueue_drop(enqueue_result, operation="reindex_memory")
    payload = {
        "ok": True,
        "memory_id": memory_id,
        "reason": reason or "api",
        **enqueue_result,
    }
    job_id = enqueue_result.get("job_id")
    if wait and isinstance(job_id, str) and job_id:
        payload["wait_result"] = await runtime_state.index_worker.wait_for_job(
            job_id=job_id,
            timeout_seconds=max(1.0, float(timeout_seconds)),
        )
    payload["runtime_worker"] = await runtime_state.index_worker.status()
    return payload
