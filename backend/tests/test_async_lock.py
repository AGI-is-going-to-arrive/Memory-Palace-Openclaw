import asyncio
import gc
import weakref

from async_lock import LoopBoundAsyncLock


async def _acquire_and_release(lock: LoopBoundAsyncLock) -> int:
    await lock.acquire()
    assert lock.locked() is True
    lock.release()
    return id(asyncio.get_running_loop())


def test_loop_bound_async_lock_prunes_stale_loop_entries() -> None:
    lock = LoopBoundAsyncLock()

    first_loop = asyncio.new_event_loop()
    first_loop_ref = weakref.ref(first_loop)
    try:
        first_loop_id = first_loop.run_until_complete(_acquire_and_release(lock))
    finally:
        first_loop.close()
        del first_loop
        gc.collect()

    assert first_loop_ref() is None

    second_loop = asyncio.new_event_loop()
    try:
        second_loop_id = second_loop.run_until_complete(_acquire_and_release(lock))
    finally:
        second_loop.close()

    assert first_loop_id != second_loop_id
    assert first_loop_id not in lock._locks
    assert first_loop_id not in lock._acquired
    assert second_loop_id in lock._locks


def test_loop_bound_async_lock_release_without_same_loop_acquire_raises() -> None:
    lock = LoopBoundAsyncLock()

    first_loop = asyncio.new_event_loop()
    try:
        first_loop.run_until_complete(lock.acquire())
    finally:
        first_loop.close()

    async def _release_on_other_loop() -> None:
        lock.release()

    second_loop = asyncio.new_event_loop()
    try:
        try:
            second_loop.run_until_complete(_release_on_other_loop())
        except RuntimeError:
            pass
        else:
            raise AssertionError("Expected release on another loop to fail")
    finally:
        second_loop.close()
