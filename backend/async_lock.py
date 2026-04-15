import asyncio
import threading
import weakref
from typing import Dict, Tuple


class LoopBoundAsyncLock:
    """Per-loop asyncio.Lock with correct cross-loop isolation.

    Each event loop gets its own independent Lock instance.  Concurrent
    acquires on *different* loops never interfere with each other because
    both the lock storage and the acquired-lock tracking are keyed by
    loop identity.

    Acquiring on loop A and releasing on loop B raises RuntimeError.
    """

    def __init__(self) -> None:
        self._state_guard = threading.Lock()
        # One lock per event loop – avoids the single-slot overwrite bug.
        self._locks: Dict[
            int,
            Tuple[weakref.ReferenceType[asyncio.AbstractEventLoop], asyncio.Lock],
        ] = {}
        # Track which lock instance each loop acquired so release() always
        # operates on the correct one.
        self._acquired: Dict[
            int,
            Tuple[weakref.ReferenceType[asyncio.AbstractEventLoop], asyncio.Lock],
        ] = {}

    def _prune_stale_entries(self) -> None:
        stale_lock_keys = [
            key for key, (loop_ref, _lock) in self._locks.items() if loop_ref() is None
        ]
        for key in stale_lock_keys:
            self._locks.pop(key, None)
            self._acquired.pop(key, None)

        stale_acquired_keys = [
            key for key, (loop_ref, _lock) in self._acquired.items() if loop_ref() is None
        ]
        for key in stale_acquired_keys:
            self._acquired.pop(key, None)

    def _get_lock(self) -> asyncio.Lock:
        current_loop = asyncio.get_running_loop()
        loop_id = id(current_loop)
        with self._state_guard:
            self._prune_stale_entries()

            entry = self._locks.get(loop_id)
            if entry is not None:
                loop_ref, lock = entry
                if loop_ref() is current_loop:
                    return lock

                lock = asyncio.Lock()
                self._locks[loop_id] = (weakref.ref(current_loop), lock)
                return lock

            lock = asyncio.Lock()
            self._locks[loop_id] = (weakref.ref(current_loop), lock)
            return lock

    async def acquire(self) -> bool:
        current_loop = asyncio.get_running_loop()
        loop_id = id(current_loop)
        lock = self._get_lock()
        await lock.acquire()
        with self._state_guard:
            self._prune_stale_entries()
            self._acquired[loop_id] = (weakref.ref(current_loop), lock)
        return True

    def release(self) -> None:
        try:
            current_loop = asyncio.get_running_loop()
            loop_id = id(current_loop)
        except RuntimeError:
            raise RuntimeError(
                "LoopBoundAsyncLock.release() called without a running event loop; "
                "release must happen on the same loop that called acquire()"
            )
        with self._state_guard:
            self._prune_stale_entries()
            entry = self._acquired.pop(loop_id, None)
        if entry is None:
            raise RuntimeError(
                "LoopBoundAsyncLock.release() called without a preceding "
                "acquire() on this event loop"
            )
        loop_ref, lock = entry
        if loop_ref() is not current_loop:
            raise RuntimeError(
                "LoopBoundAsyncLock.release() called on a different event loop "
                "than the one that acquired the lock"
            )
        lock.release()

    def locked(self) -> bool:
        try:
            lock = self._get_lock()
        except RuntimeError:
            return False
        return lock.locked()

    async def __aenter__(self) -> "LoopBoundAsyncLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False
