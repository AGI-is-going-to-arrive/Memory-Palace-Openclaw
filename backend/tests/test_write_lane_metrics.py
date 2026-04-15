import asyncio
import sqlite3
import threading
import time

import pytest

import runtime_state as runtime_state_module
from runtime_state import WriteLaneCoordinator


@pytest.mark.asyncio
async def test_write_lane_status_includes_new_metrics_fields_with_defaults() -> None:
    coordinator = WriteLaneCoordinator()

    status = await coordinator.status()

    assert status["global_concurrency"] >= 1
    assert status["global_active"] == 0
    assert status["global_waiting"] == 0
    assert status["session_waiting_count"] == 0
    assert status["session_waiting_sessions"] == 0
    assert status["max_session_waiting"] == 0
    assert status["wait_warn_ms"] >= 1
    assert status["global_acquire_timeout_sec"] > 0
    assert status["task_timeout_sec"] >= 1
    assert status["lock_retry_attempts"] >= 1
    assert status["lock_retry_base_delay_ms"] >= 0
    assert status["lock_retry_max_delay_ms"] >= 0
    assert status["writes_total"] == 0
    assert status["writes_failed"] == 0
    assert status["writes_success"] == 0
    assert status["failure_rate"] == 0.0
    assert status["session_wait_ms_p95"] == 0
    assert status["global_wait_ms_p95"] == 0
    assert status["duration_ms_p95"] == 0
    assert status["last_error"] is None


@pytest.mark.asyncio
async def test_write_lane_metrics_track_outcomes_and_latency_percentiles() -> None:
    coordinator = WriteLaneCoordinator()

    async def _hold(started: asyncio.Event) -> str:
        started.set()
        await asyncio.sleep(0.03)
        return "hold_done"

    async def _ok(value: str) -> str:
        return value

    global_started = asyncio.Event()
    global_first = asyncio.create_task(
        coordinator.run_write(
            session_id="global-first",
            operation="create_memory",
            task=lambda: _hold(global_started),
        )
    )
    await global_started.wait()
    global_second = await coordinator.run_write(
        session_id="global-second",
        operation="create_memory",
        task=lambda: _ok("global_waited"),
    )
    assert global_second == "global_waited"
    assert await global_first == "hold_done"

    session_started = asyncio.Event()
    session_first = asyncio.create_task(
        coordinator.run_write(
            session_id="shared-session",
            operation="update_memory",
            task=lambda: _hold(session_started),
        )
    )
    await session_started.wait()
    session_second = await coordinator.run_write(
        session_id="shared-session",
        operation="update_memory",
        task=lambda: _ok("session_waited"),
    )
    assert session_second == "session_waited"
    assert await session_first == "hold_done"

    async def _fail() -> str:
        raise RuntimeError("write_failed_for_test")

    with pytest.raises(RuntimeError, match="write_failed_for_test"):
        await coordinator.run_write(
            session_id="failure-session",
            operation="delete_memory",
            task=_fail,
        )

    status = await coordinator.status()

    assert status["writes_total"] == 5
    assert status["writes_success"] == 4
    assert status["writes_failed"] == 1
    assert status["failure_rate"] == pytest.approx(0.2)
    assert status["session_wait_ms_p95"] > 0
    assert status["global_wait_ms_p95"] > 0
    assert status["duration_ms_p95"] > 0
    assert status["last_error"] == "write_failed_for_test"


@pytest.mark.asyncio
async def test_write_lane_preserves_waiter_order_under_global_contention() -> None:
    coordinator = WriteLaneCoordinator()
    release_holder = asyncio.Event()
    holder_started = asyncio.Event()
    execution_order: list[str] = []

    async def _hold_global_slot() -> str:
        holder_started.set()
        await release_holder.wait()
        execution_order.append("holder")
        return "holder_done"

    async def _record(name: str) -> str:
        execution_order.append(name)
        return name

    holder = asyncio.create_task(
        coordinator.run_write(
            session_id="holder",
            operation="create_memory",
            task=_hold_global_slot,
        )
    )
    await holder_started.wait()

    second = asyncio.create_task(
        coordinator.run_write(
            session_id="second",
            operation="update_memory",
            task=lambda: _record("second"),
        )
    )
    third = asyncio.create_task(
        coordinator.run_write(
            session_id="third",
            operation="delete_memory",
            task=lambda: _record("third"),
        )
    )

    for _ in range(100):
        if (await coordinator.status())["global_waiting"] >= 2:
            break
        await asyncio.sleep(0.001)
    else:
        pytest.fail("queued writers did not enter global waiting state in time")

    release_holder.set()

    assert await holder == "holder_done"
    assert await second == "second"
    assert await third == "third"
    assert execution_order == ["holder", "second", "third"]


@pytest.mark.asyncio
async def test_write_lane_metrics_count_cancelled_global_wait_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_GLOBAL_CONCURRENCY", "1")
    coordinator = WriteLaneCoordinator()
    release_holder = asyncio.Event()

    async def _hold_global_slot() -> str:
        await release_holder.wait()
        return "holder_done"

    async def _quick_success() -> str:
        return "quick_done"

    holder = asyncio.create_task(
        coordinator.run_write(
            session_id="holder",
            operation="create_memory",
            task=_hold_global_slot,
        )
    )

    for _ in range(100):
        if (await coordinator.status())["global_active"] == 1:
            break
        await asyncio.sleep(0.001)
    else:
        pytest.fail("holder did not acquire global write slot in time")

    waiter = asyncio.create_task(
        coordinator.run_write(
            session_id="waiter",
            operation="update_memory",
            task=_quick_success,
        )
    )

    for _ in range(100):
        if (await coordinator.status())["global_waiting"] >= 1:
            break
        await asyncio.sleep(0.001)
    else:
        pytest.fail("waiter did not enter global waiting state in time")

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release_holder.set()
    assert await holder == "holder_done"

    post_result = await asyncio.wait_for(
        coordinator.run_write(
            session_id="post-cancel",
            operation="delete_memory",
            task=_quick_success,
        ),
        timeout=0.2,
    )
    assert post_result == "quick_done"

    status = await coordinator.status()

    assert status["global_waiting"] == 0
    assert status["global_active"] == 0
    assert status["writes_total"] == 3
    assert status["writes_success"] == 2
    assert status["writes_failed"] == 1
    assert status["failure_rate"] == pytest.approx(1 / 3)
    assert status["last_error"] == "cancelled"


@pytest.mark.asyncio
async def test_write_lane_metrics_count_cancelled_session_wait_as_failure() -> None:
    coordinator = WriteLaneCoordinator()
    release_holder = asyncio.Event()

    async def _hold_session_lane() -> str:
        await release_holder.wait()
        return "holder_done"

    async def _quick_success() -> str:
        return "quick_done"

    holder = asyncio.create_task(
        coordinator.run_write(
            session_id="shared-session",
            operation="create_memory",
            task=_hold_session_lane,
        )
    )

    for _ in range(100):
        if (await coordinator.status())["global_active"] == 1:
            break
        await asyncio.sleep(0.001)
    else:
        pytest.fail("holder did not acquire write lane in time")

    waiter = asyncio.create_task(
        coordinator.run_write(
            session_id="shared-session",
            operation="update_memory",
            task=_quick_success,
        )
    )

    for _ in range(100):
        if (await coordinator.status())["session_waiting_count"] >= 1:
            break
        await asyncio.sleep(0.001)
    else:
        pytest.fail("waiter did not enter session waiting state in time")

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release_holder.set()
    assert await holder == "holder_done"

    recovered = await asyncio.wait_for(
        coordinator.run_write(
            session_id="shared-session",
            operation="delete_memory",
            task=_quick_success,
        ),
        timeout=0.2,
    )
    assert recovered == "quick_done"

    status = await coordinator.status()

    assert status["session_waiting_count"] == 0
    assert status["global_waiting"] == 0
    assert status["global_active"] == 0
    assert status["writes_total"] == 3
    assert status["writes_success"] == 2
    assert status["writes_failed"] == 1
    assert status["failure_rate"] == pytest.approx(1 / 3)
    assert status["last_error"] == "cancelled"


@pytest.mark.asyncio
async def test_write_lane_timeout_releases_lane_for_following_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_TASK_TIMEOUT_SEC", "1")
    coordinator = WriteLaneCoordinator()
    started = asyncio.Event()

    async def _stuck_write() -> str:
        started.set()
        await asyncio.sleep(5)
        return "unreachable"

    timed_out = asyncio.create_task(
        coordinator.run_write(
            session_id="timeout-session",
            operation="update_memory",
            task=_stuck_write,
        )
    )

    await started.wait()

    with pytest.raises(TimeoutError, match="write lane task timed out after 1s"):
        await timed_out

    recovered = await asyncio.wait_for(
        coordinator.run_write(
            session_id="timeout-session",
            operation="update_memory",
            task=lambda: asyncio.sleep(0, result="recovered"),
        ),
        timeout=0.2,
    )

    assert recovered == "recovered"

    status = await coordinator.status()

    assert status["writes_total"] == 2
    assert status["writes_success"] == 1
    assert status["writes_failed"] == 1
    assert status["last_error"] == "write lane task timed out after 1s"


@pytest.mark.asyncio
async def test_write_lane_global_acquire_timeout_releases_lane_for_following_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_GLOBAL_CONCURRENCY", "1")
    monkeypatch.setenv("RUNTIME_WRITE_GLOBAL_ACQUIRE_TIMEOUT_SEC", "1")
    coordinator = WriteLaneCoordinator()
    release_holder = asyncio.Event()
    holder_started = asyncio.Event()

    async def _hold_global_slot() -> str:
        holder_started.set()
        await release_holder.wait()
        return "holder_done"

    holder = asyncio.create_task(
        coordinator.run_write(
            session_id="global-holder",
            operation="update_memory",
            task=_hold_global_slot,
        )
    )

    await holder_started.wait()

    with pytest.raises(TimeoutError, match="write lane global acquire timed out after 1s"):
        await coordinator.run_write(
            session_id="global-timeout",
            operation="update_memory",
            task=lambda: asyncio.sleep(0, result="unreachable"),
        )

    release_holder.set()
    assert await holder == "holder_done"

    recovered = await asyncio.wait_for(
        coordinator.run_write(
            session_id="global-timeout",
            operation="update_memory",
            task=lambda: asyncio.sleep(0, result="recovered"),
        ),
        timeout=0.2,
    )

    assert recovered == "recovered"

    status = await coordinator.status()

    assert status["writes_total"] == 3
    assert status["writes_success"] == 2
    assert status["writes_failed"] == 1
    assert status["global_waiting"] == 0
    assert status["global_active"] == 0
    assert status["last_error"] == "write lane global acquire timed out after 1s"


@pytest.mark.asyncio
async def test_write_lane_thread_global_acquire_timeout_does_not_leak_loop_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_GLOBAL_CONCURRENCY", "1")
    monkeypatch.setenv("RUNTIME_WRITE_GLOBAL_ACQUIRE_TIMEOUT_SEC", "0.05")
    coordinator = WriteLaneCoordinator()

    assert coordinator._thread_global_sem.acquire(timeout=0.1) is True
    try:
        with pytest.raises(TimeoutError, match=r"write lane global acquire timed out after 0\.05s"):
            await coordinator.run_write(
                session_id="thread-global-timeout",
                operation="update_memory",
                task=lambda: asyncio.sleep(0, result="unreachable"),
            )

        status = await coordinator.status()
        assert status["global_active"] == 0
        assert status["global_waiting"] == 0
        assert status["writes_total"] == 1
        assert status["writes_failed"] == 1
    finally:
        coordinator._thread_global_sem.release()

    recovered = await asyncio.wait_for(
        coordinator.run_write(
            session_id="thread-global-timeout",
            operation="update_memory",
            task=lambda: asyncio.sleep(0, result="recovered"),
        ),
        timeout=0.2,
    )

    assert recovered == "recovered"


@pytest.mark.asyncio
async def test_write_lane_retries_transient_sqlite_lock_errors_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_LOCK_RETRY_ATTEMPTS", "3")
    monkeypatch.setenv("RUNTIME_WRITE_LOCK_RETRY_BASE_DELAY_MS", "1")
    coordinator = WriteLaneCoordinator()
    attempts = 0

    async def _flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = await coordinator.run_write(
        session_id="retry-session",
        operation="update_memory",
        task=_flaky,
    )
    status = await coordinator.status()

    assert result == "ok"
    assert attempts == 3
    assert status["writes_total"] == 1
    assert status["writes_success"] == 1
    assert status["writes_failed"] == 0


@pytest.mark.asyncio
async def test_write_lane_caps_sqlite_lock_retry_backoff_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_LOCK_RETRY_ATTEMPTS", "4")
    monkeypatch.setenv("RUNTIME_WRITE_LOCK_RETRY_BASE_DELAY_MS", "100")
    monkeypatch.setenv("RUNTIME_WRITE_LOCK_RETRY_MAX_DELAY_MS", "150")
    coordinator = WriteLaneCoordinator()
    attempts = 0
    sleep_delays: list[float] = []
    original_sleep = runtime_state_module.asyncio.sleep

    async def _capturing_sleep(delay: float, result=None):  # type: ignore[override]
        sleep_delays.append(delay)
        return await original_sleep(0, result)

    monkeypatch.setattr(runtime_state_module.asyncio, "sleep", _capturing_sleep)

    async def _flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = await coordinator.run_write(
        session_id="retry-cap-session",
        operation="update_memory",
        task=_flaky,
    )

    assert result == "ok"
    assert attempts == 4
    assert sleep_delays == [0.1, 0.15, 0.15]


@pytest.mark.asyncio
async def test_write_lane_raises_after_exhausting_sqlite_lock_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_LOCK_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("RUNTIME_WRITE_LOCK_RETRY_BASE_DELAY_MS", "1")
    coordinator = WriteLaneCoordinator()
    attempts = 0

    async def _always_locked() -> str:
        nonlocal attempts
        attempts += 1
        raise sqlite3.OperationalError("database table is locked")

    with pytest.raises(sqlite3.OperationalError, match="database table is locked"):
        await coordinator.run_write(
            session_id="retry-fail-session",
            operation="update_memory",
            task=_always_locked,
        )

    status = await coordinator.status()

    assert attempts == 2
    assert status["writes_total"] == 1
    assert status["writes_success"] == 0
    assert status["writes_failed"] == 1
    assert status["last_error"] == "database table is locked"


@pytest.mark.asyncio
async def test_write_lane_releases_idle_session_locks_after_write_completion() -> None:
    coordinator = WriteLaneCoordinator()

    async def _ok() -> str:
        return "ok"

    assert await coordinator.run_write(
        session_id="session-a",
        operation="create_memory",
        task=_ok,
    ) == "ok"
    assert await coordinator.run_write(
        session_id="session-b",
        operation="update_memory",
        task=_ok,
    ) == "ok"

    assert coordinator._session_waiting == {}
    assert coordinator._session_locks == {}


@pytest.mark.asyncio
async def test_write_lane_reclaims_idle_session_lock_entries() -> None:
    coordinator = WriteLaneCoordinator()

    async def _ok() -> str:
        return "done"

    for idx in range(20):
        result = await coordinator.run_write(
            session_id=f"session-{idx}",
            operation="update_memory",
            task=_ok,
        )
        assert result == "done"

    assert coordinator._session_locks == {}
    assert coordinator._session_waiting == {}


@pytest.mark.asyncio
async def test_write_lane_uses_asyncio_session_locks_within_event_loop() -> None:
    coordinator = WriteLaneCoordinator()
    started = asyncio.Event()
    release_holder = asyncio.Event()

    async def _hold() -> str:
        started.set()
        await release_holder.wait()
        return "held"

    holder = asyncio.create_task(
        coordinator.run_write(
            session_id="async-lock-session",
            operation="update_memory",
            task=_hold,
        )
    )

    await started.wait()

    assert coordinator._session_locks
    lock = next(iter(coordinator._session_locks.values()))
    assert isinstance(lock, asyncio.Lock)
    assert lock.locked() is True

    release_holder.set()
    assert await holder == "held"


def test_write_lane_rebinds_loop_state_across_asyncio_run_calls() -> None:
    coordinator = WriteLaneCoordinator()

    async def _run_write(value: str) -> str:
        return await coordinator.run_write(
            session_id=value,
            operation="update_memory",
            task=lambda: asyncio.sleep(0, result=value),
        )

    first = asyncio.run(_run_write("first"))
    first_loop = coordinator._loop
    first_guard = coordinator._guard
    first_sem = coordinator._global_sem

    second = asyncio.run(_run_write("second"))

    assert first == "first"
    assert second == "second"
    assert coordinator._loop is not None
    assert coordinator._loop is not first_loop
    assert coordinator._guard is not None and coordinator._guard is not first_guard
    assert coordinator._global_sem is not None and coordinator._global_sem is not first_sem


def test_write_lane_status_rebinds_loop_state_across_asyncio_run_calls() -> None:
    coordinator = WriteLaneCoordinator()

    first_status = asyncio.run(coordinator.status())
    first_loop = coordinator._loop
    second_status = asyncio.run(coordinator.status())

    assert first_status["writes_total"] == 0
    assert second_status["writes_total"] == 0
    assert coordinator._loop is not None
    assert coordinator._loop is not first_loop


def test_write_lane_preserves_global_concurrency_across_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_GLOBAL_CONCURRENCY", "1")
    coordinator = WriteLaneCoordinator()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    errors: list[BaseException] = []
    results: list[str] = []

    def _run(session_id: str, label: str) -> None:
        async def _task() -> str:
            if label == "first":
                first_started.set()
                await asyncio.to_thread(release_first.wait)
            else:
                second_started.set()
            return label

        try:
            results.append(
                asyncio.run(
                    coordinator.run_write(
                        session_id=session_id,
                        operation="update_memory",
                        task=_task,
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first_thread = threading.Thread(
        target=_run,
        args=("session-a", "first"),
        daemon=True,
    )
    second_thread = threading.Thread(
        target=_run,
        args=("session-b", "second"),
        daemon=True,
    )

    first_thread.start()
    assert first_started.wait(timeout=5)

    second_thread.start()
    time.sleep(0.15)

    status_while_blocked = asyncio.run(coordinator.status())

    assert second_started.is_set() is False
    assert status_while_blocked["global_active"] == 1
    assert status_while_blocked["global_waiting"] >= 1

    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert errors == []
    assert sorted(results) == ["first", "second"]

    final_status = asyncio.run(coordinator.status())
    assert final_status["global_active"] == 0
    assert final_status["global_waiting"] == 0


def test_write_lane_serializes_same_session_across_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_GLOBAL_CONCURRENCY", "2")
    coordinator = WriteLaneCoordinator()
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    errors: list[BaseException] = []
    results: list[str] = []

    def _run(label: str) -> None:
        async def _task() -> str:
            if label == "first":
                first_started.set()
                await asyncio.to_thread(release_first.wait)
            else:
                second_started.set()
            return label

        try:
            results.append(
                asyncio.run(
                    coordinator.run_write(
                        session_id="shared-session",
                        operation="update_memory",
                        task=_task,
                    )
                )
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first_thread = threading.Thread(target=_run, args=("first",), daemon=True)
    second_thread = threading.Thread(target=_run, args=("second",), daemon=True)

    first_thread.start()
    assert first_started.wait(timeout=5)

    second_thread.start()
    time.sleep(0.15)

    status_while_blocked = asyncio.run(coordinator.status())

    assert second_started.is_set() is False
    assert status_while_blocked["global_active"] == 1
    assert status_while_blocked["global_waiting"] == 0
    assert status_while_blocked["session_waiting_count"] >= 1

    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert errors == []
    assert results == ["first", "second"]

    final_status = asyncio.run(coordinator.status())
    assert final_status["global_active"] == 0
    assert final_status["global_waiting"] == 0
    assert final_status["session_waiting_count"] == 0


@pytest.mark.asyncio
async def test_write_lane_pressure_recovers_cleanly_after_many_contended_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_WRITE_GLOBAL_CONCURRENCY", "2")
    monkeypatch.setenv("RUNTIME_WRITE_TASK_TIMEOUT_SEC", "5")
    coordinator = WriteLaneCoordinator()
    running = 0
    max_running = 0
    running_guard = asyncio.Lock()

    async def _contended_task(index: int) -> int:
        nonlocal running, max_running
        async with running_guard:
            running += 1
            max_running = max(max_running, running)
        try:
            await asyncio.sleep(0.02 if index % 4 == 0 else 0.005)
            return index
        finally:
            async with running_guard:
                running -= 1

    tasks = [
        asyncio.create_task(
            coordinator.run_write(
                session_id=f"pressure-session-{index % 6}",
                operation="update_memory",
                task=lambda idx=index: _contended_task(idx),
            )
        )
        for index in range(30)
    ]

    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5.0)

    assert sorted(results) == list(range(30))
    assert max_running <= 2

    post_storm = await asyncio.wait_for(
        coordinator.run_write(
            session_id="pressure-post",
            operation="update_memory",
            task=lambda: asyncio.sleep(0, result="post-storm"),
        ),
        timeout=0.5,
    )
    assert post_storm == "post-storm"

    status = await coordinator.status()

    assert status["writes_total"] == 31
    assert status["writes_success"] == 31
    assert status["writes_failed"] == 0
    assert status["global_active"] == 0
    assert status["global_waiting"] == 0
    assert coordinator._session_waiting == {}
    assert coordinator._session_locks == {}
