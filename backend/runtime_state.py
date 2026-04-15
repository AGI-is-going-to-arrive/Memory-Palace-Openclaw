"""
Runtime state helpers for Phase-2 memory behavior.

This module provides:
1) Write-lane coordination (session lane + global lane).
2) Session-first retrieval cache (ephemeral, process-local).
3) Flush tracking for threshold-based context compaction.
4) Background index worker queue for async reindex/rebuild tasks.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import logging
import math
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set

from env_utils import (
    env_bool as _env_bool,
    env_float as _env_float,
    env_int as _env_int,
    utc_iso_now as _utc_iso_now,
)
from async_lock import LoopBoundAsyncLock

logger = logging.getLogger(__name__)

_LATIN_QUERY_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_CJK_QUERY_TOKEN_PATTERN = re.compile(
    r"[\u3040-\u309F\u30A0-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7A3\uF900-\uFAFF\U00020000-\U0002EBEF]+"
)
_SQLITE_LOCK_ERROR_MARKERS = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "sqlite_busy",
)
_ROLLUP_DETAIL_PRIORITY_KEYS = (
    "error",
    "reason",
    "decision",
    "action",
    "uri",
    "target",
    "category",
    "workflow",
    "preference",
)
_HIGH_VALUE_EARLY_FLUSH_KEYWORDS = (
    "preference",
    "workflow",
    "default workflow",
    "remember",
    "write_guard",
    "contradiction",
    "provider",
    "switched",
    "updated",
    "decision",
    "偏好",
    "工作流",
    "默认工作流",
    "记住",
    "矛盾",
    "切换",
    "更新",
)


def _normalize_session_id(session_id: Optional[str]) -> str:
    value = (session_id or "").strip()
    return value if value else "default"


def _is_cjk_query_token(token: str) -> bool:
    return bool(token) and bool(_CJK_QUERY_TOKEN_PATTERN.fullmatch(token))


def _append_unique_token(target: List[str], seen: Set[str], token: str) -> None:
    if token and token not in seen:
        seen.add(token)
        target.append(token)


def _tokenize_query(query: str) -> List[str]:
    normalized = unicodedata.normalize("NFC", str(query or "")).strip().casefold()
    if not normalized:
        return []

    latin_tokens: List[str] = []
    latin_seen: Set[str] = set()
    cjk_tokens: List[str] = []
    cjk_seen: Set[str] = set()
    merged_tokens: List[str] = []
    merged_seen: Set[str] = set()

    for token in _LATIN_QUERY_TOKEN_PATTERN.findall(normalized):
        if _is_cjk_query_token(token):
            continue
        _append_unique_token(latin_tokens, latin_seen, token)
    for chunk in _CJK_QUERY_TOKEN_PATTERN.findall(normalized):
        _append_unique_token(cjk_tokens, cjk_seen, chunk)
        for index in range(len(chunk) - 1):
            _append_unique_token(cjk_tokens, cjk_seen, chunk[index : index + 2])

    buckets = [latin_tokens, cjk_tokens]
    indices = [0, 0]
    while len(merged_tokens) < 24:
        progressed = False
        for bucket_index, bucket in enumerate(buckets):
            next_index = indices[bucket_index]
            if next_index >= len(bucket):
                continue
            progressed = True
            indices[bucket_index] += 1
            _append_unique_token(
                merged_tokens,
                merged_seen,
                bucket[next_index],
            )
            if len(merged_tokens) >= 24:
                break
        if not progressed:
            break

    return merged_tokens[:24]


def _ensure_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_inline_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _trim_with_ellipsis(text: str, limit: int) -> str:
    clean_limit = max(16, int(limit))
    normalized = _normalize_inline_whitespace(text)
    if len(normalized) <= clean_limit:
        return normalized
    return normalized[: max(1, clean_limit - 3)].rstrip() + "..."


def _clean_rollup_line(line: str) -> str:
    normalized = _normalize_inline_whitespace(line)
    return re.sub(r"^[\-\*\#>\u2022]+\s*", "", normalized).strip()


def _parse_rollup_detail_line(line: str) -> Optional[tuple[str, str]]:
    cleaned = _clean_rollup_line(line)
    if not cleaned:
        return None
    match = re.match(r"(?P<label>[^:=]{1,48})\s*[:=]\s*(?P<value>.+)", cleaned)
    if not match:
        return None
    label = re.sub(r"[\s_-]+", " ", match.group("label")).strip().casefold()
    return label, cleaned


def _summarize_event_for_rollup(event_text: str, *, max_chars: int = 220) -> str:
    lines = [_clean_rollup_line(line) for line in str(event_text or "").splitlines()]
    non_empty_lines = [line for line in lines if line]
    if not non_empty_lines:
        return _trim_with_ellipsis(event_text, max_chars)

    main_line = non_empty_lines[0]
    detail_line: Optional[str] = None

    parsed_details: List[tuple[str, str]] = []
    for candidate in non_empty_lines[1:]:
        parsed = _parse_rollup_detail_line(candidate)
        if parsed is not None:
            parsed_details.append(parsed)

    for key in _ROLLUP_DETAIL_PRIORITY_KEYS:
        for label, raw_line in parsed_details:
            if key in label:
                detail_line = raw_line
                break
        if detail_line is not None:
            break

    summary = main_line if detail_line is None else f"{main_line} | {detail_line}"
    return _trim_with_ellipsis(summary, max_chars)


@dataclass
class SessionSearchHit:
    uri: str
    memory_id: Optional[int]
    snippet: str
    updated_at: str
    priority: Optional[int]
    source: str


class WriteLaneCoordinator:
    """
    Two-layer write coordination:
    - Session lane: serial writes within the same session.
    - Global lane: bounded write concurrency across all sessions.
    """

    def __init__(self) -> None:
        self._global_concurrency = _env_int(
            "RUNTIME_WRITE_GLOBAL_CONCURRENCY", 1, minimum=1
        )
        self._global_acquire_timeout_seconds = _env_float(
            "RUNTIME_WRITE_GLOBAL_ACQUIRE_TIMEOUT_SEC", 30.0, minimum=0.01
        )
        self._wait_warn_ms = _env_int("RUNTIME_WRITE_WAIT_WARN_MS", 2000, minimum=1)
        self._task_timeout_seconds = float(
            _env_int("RUNTIME_WRITE_TASK_TIMEOUT_SEC", 120, minimum=1)
        )
        self._lock_retry_attempts = _env_int(
            "RUNTIME_WRITE_LOCK_RETRY_ATTEMPTS", 3, minimum=1
        )
        self._lock_retry_base_delay_seconds = max(
            0.0,
            float(_env_int("RUNTIME_WRITE_LOCK_RETRY_BASE_DELAY_MS", 50, minimum=0)) / 1000.0,
        )
        self._lock_retry_max_delay_seconds = max(
            0.0,
            _env_float("RUNTIME_WRITE_LOCK_RETRY_MAX_DELAY_MS", 1000.0, minimum=0.0)
            / 1000.0,
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._global_sem: Optional[asyncio.Semaphore] = None
        self._session_locks: Dict[tuple[int, str], asyncio.Lock] = {}
        self._session_waiting: Dict[str, int] = {}
        self._global_waiting = 0
        self._global_active = 0
        self._writes_total = 0
        self._writes_failed = 0
        self._writes_success = 0
        self._lock_retries_total = 0
        self._lock_retries_exhausted = 0
        self._last_error: Optional[str] = None
        self._session_wait_samples: Deque[int] = deque(maxlen=200)
        self._global_wait_samples: Deque[int] = deque(maxlen=200)
        self._duration_samples: Deque[int] = deque(maxlen=200)
        self._state_guard = threading.Lock()
        self._thread_global_sem = threading.Semaphore(self._global_concurrency)
        self._thread_session_locks: Dict[str, threading.Lock] = {}
        self._guard: Optional[asyncio.Lock] = None

    def _ensure_loop_state(self) -> None:
        current_loop = asyncio.get_running_loop()
        if (
            self._loop is current_loop
            and self._global_sem is not None
            and self._guard is not None
        ):
            return
        loop_changed = self._loop is not None and self._loop is not current_loop
        self._loop = current_loop
        self._global_sem = asyncio.Semaphore(self._global_concurrency)
        self._guard = asyncio.Lock()

    @staticmethod
    def _p95(values: Deque[int]) -> int:
        if not values:
            return 0
        ranked = sorted(values)
        idx = max(0, math.ceil(len(ranked) * 0.95) - 1)
        return int(ranked[idx])

    async def _record_write_metrics(
        self,
        *,
        success: bool,
        session_wait_ms: int,
        global_wait_ms: int,
        duration_ms: int,
        error: Optional[str] = None,
    ) -> None:
        self._ensure_loop_state()
        with self._state_guard:
            self._writes_total += 1
            if success:
                self._writes_success += 1
            else:
                self._writes_failed += 1
                self._last_error = error or "unknown_error"
            self._session_wait_samples.append(max(0, int(session_wait_ms)))
            self._global_wait_samples.append(max(0, int(global_wait_ms)))
            self._duration_samples.append(max(0, int(duration_ms)))

    async def _get_session_lock_and_mark_waiting(
        self, session_id: str
    ) -> tuple[tuple[int, str], asyncio.Lock, threading.Lock]:
        self._ensure_loop_state()
        current_loop = asyncio.get_running_loop()
        lock_key = (id(current_loop), session_id)
        with self._state_guard:
            lock = self._session_locks.get(lock_key)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[lock_key] = lock
            thread_lock = self._thread_session_locks.get(session_id)
            if thread_lock is None:
                thread_lock = threading.Lock()
                self._thread_session_locks[session_id] = thread_lock
            self._session_waiting[session_id] = self._session_waiting.get(session_id, 0) + 1
            return lock_key, lock, thread_lock

    def _decrement_session_waiting_unlocked(self, lane: str) -> None:
        current = max(0, int(self._session_waiting.get(lane, 0)))
        next_value = max(0, current - 1)
        if next_value <= 0:
            self._session_waiting.pop(lane, None)
            return
        self._session_waiting[lane] = next_value

    async def _maybe_cleanup_session_lane(
        self,
        lane: str,
        lock_key: tuple[int, str],
        lock: asyncio.Lock,
    ) -> None:
        self._ensure_loop_state()
        with self._state_guard:
            waiting = max(0, int(self._session_waiting.get(lane, 0)))
            current_lock = self._session_locks.get(lock_key)
            if current_lock is lock and waiting <= 0 and not lock.locked():
                self._session_locks.pop(lock_key, None)
            local_lane_still_active = any(
                key[1] == lane and existing_lock.locked()
                for key, existing_lock in self._session_locks.items()
            )
            thread_lock = self._thread_session_locks.get(lane)
            if (
                waiting <= 0
                and not local_lane_still_active
                and thread_lock is not None
                and not thread_lock.locked()
            ):
                self._thread_session_locks.pop(lane, None)
            if (
                waiting <= 0
                and lane not in self._thread_session_locks
                and not any(key[1] == lane for key in self._session_locks)
            ):
                self._session_waiting.pop(lane, None)

    async def _acquire_thread_lock_until_available(
        self,
        waitable: Any,
        *,
        poll_seconds: float = 0.05,
    ) -> None:
        timeout_seconds = max(0.01, float(poll_seconds))
        while True:
            acquired = await self._acquire_thread_waitable(
                waitable,
                timeout_seconds=timeout_seconds,
            )
            if acquired:
                return
            await asyncio.sleep(0)

    async def _acquire_thread_waitable(
        self,
        waitable: Any,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> bool:
        # The cross-thread global lane must remain process-wide, but we do not
        # busy-wait on it from the event loop.
        if timeout_seconds is None:
            await asyncio.to_thread(waitable.acquire)
            return True
        return bool(
            await asyncio.to_thread(
                waitable.acquire,
                True,
                max(0.0, float(timeout_seconds)),
            )
        )

    @staticmethod
    def _remaining_timeout_seconds(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _format_timeout_seconds(self, seconds: float) -> str:
        return f"{max(0.0, float(seconds)):g}"

    async def _acquire_global_lane(self, global_sem: asyncio.Semaphore) -> None:
        deadline = time.monotonic() + self._global_acquire_timeout_seconds
        global_loop_acquired = False
        global_thread_acquired = False
        timeout_message = (
            "write lane global acquire timed out after "
            f"{self._format_timeout_seconds(self._global_acquire_timeout_seconds)}s"
        )
        try:
            await asyncio.wait_for(
                global_sem.acquire(),
                timeout=self._remaining_timeout_seconds(deadline),
            )
            global_loop_acquired = True
            global_thread_acquired = await self._acquire_thread_waitable(
                self._thread_global_sem,
                timeout_seconds=self._remaining_timeout_seconds(deadline),
            )
            if not global_thread_acquired:
                raise TimeoutError(timeout_message)
        except asyncio.TimeoutError as exc:
            if global_thread_acquired:
                self._thread_global_sem.release()
            if global_loop_acquired:
                global_sem.release()
            raise TimeoutError(timeout_message) from exc
        except Exception:
            if global_thread_acquired:
                self._thread_global_sem.release()
            if global_loop_acquired:
                global_sem.release()
            raise

    async def _run_task_with_timeout(
        self,
        task: Callable[[], Awaitable[Any]],
    ) -> Any:
        try:
            return await asyncio.wait_for(
                task(),
                timeout=self._task_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"write lane task timed out after {int(self._task_timeout_seconds)}s"
            ) from exc

    @staticmethod
    def _iter_exception_messages(exc: BaseException) -> List[str]:
        pending: List[BaseException] = [exc]
        seen: Set[int] = set()
        messages: List[str] = []

        while pending:
            current = pending.pop(0)
            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)
            rendered = str(current).strip().lower()
            if rendered:
                messages.append(rendered)
            for nested in (
                getattr(current, "orig", None),
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            ):
                if isinstance(nested, BaseException):
                    pending.append(nested)
        return messages

    @classmethod
    def _is_transient_sqlite_lock_error(cls, exc: BaseException) -> bool:
        if not isinstance(exc, Exception):
            return False
        if isinstance(exc, sqlite3.OperationalError):
            return any(
                marker in message
                for message in cls._iter_exception_messages(exc)
                for marker in _SQLITE_LOCK_ERROR_MARKERS
            )
        type_name = type(exc).__name__.lower()
        if "operationalerror" not in type_name:
            return False
        return any(
            marker in message
            for message in cls._iter_exception_messages(exc)
            for marker in _SQLITE_LOCK_ERROR_MARKERS
        )

    async def _run_task_with_lock_retry(
        self,
        task: Callable[[], Awaitable[Any]],
    ) -> Any:
        last_error: Optional[Exception] = None
        for attempt in range(max(1, self._lock_retry_attempts)):
            try:
                result = await self._run_task_with_timeout(task)
                if attempt > 0:
                    logger.info(
                        "sqlite lock retry succeeded after %d attempt(s)",
                        attempt,
                    )
                return result
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise
            except Exception as exc:
                if not self._is_transient_sqlite_lock_error(exc):
                    raise
                last_error = exc
                with self._state_guard:
                    self._lock_retries_total += 1
                if attempt >= max(1, self._lock_retry_attempts) - 1:
                    with self._state_guard:
                        self._lock_retries_exhausted += 1
                    logger.warning(
                        "sqlite lock retry exhausted after %d attempt(s): %s",
                        attempt + 1,
                        exc,
                    )
                    raise
                delay = min(
                    self._lock_retry_base_delay_seconds * (2**attempt),
                    self._lock_retry_max_delay_seconds,
                )
                logger.debug(
                    "sqlite lock contention, retrying in %.0fms (attempt %d/%d)",
                    delay * 1000,
                    attempt + 1,
                    self._lock_retry_attempts,
                )
                if delay > 0:
                    await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    async def run_write(
        self,
        *,
        session_id: Optional[str],
        operation: str,
        task: Callable[[], Awaitable[Any]],
    ) -> Any:
        self._ensure_loop_state()
        global_sem = self._global_sem
        if global_sem is None:
            raise RuntimeError("write lane global semaphore is unavailable")
        write_start = time.monotonic()
        lane = _normalize_session_id(session_id)
        session_wait_start = time.monotonic()
        session_lock_key, session_lock, session_thread_lock = (
            await self._get_session_lock_and_mark_waiting(lane)
        )
        session_waiting_counted = True
        session_thread_acquired = False
        try:
            async with session_lock:
                try:
                    await self._acquire_thread_lock_until_available(session_thread_lock)
                    session_thread_acquired = True
                    waited_session_ms = int((time.monotonic() - session_wait_start) * 1000)
                    with self._state_guard:
                        if session_waiting_counted:
                            self._decrement_session_waiting_unlocked(lane)
                            session_waiting_counted = False

                    waited_global_ms = 0
                    global_wait_start = time.monotonic()
                    global_waiting_counted = True
                    global_loop_acquired = False
                    global_acquired = False
                    global_active_counted = False

                    with self._state_guard:
                        self._global_waiting += 1

                    try:
                        await self._acquire_global_lane(global_sem)
                        global_loop_acquired = True
                        global_acquired = True
                        waited_global_ms = int((time.monotonic() - global_wait_start) * 1000)

                        with self._state_guard:
                            if global_waiting_counted:
                                self._global_waiting = max(0, self._global_waiting - 1)
                                global_waiting_counted = False
                            self._global_active += 1
                            global_active_counted = True

                        # Keep this as a metric hook, even when no logger is attached yet.
                        _ = operation
                        _ = waited_session_ms
                        _ = waited_global_ms
                        result = await self._run_task_with_lock_retry(task)
                        duration_ms = int((time.monotonic() - write_start) * 1000)
                        await self._record_write_metrics(
                            success=True,
                            session_wait_ms=waited_session_ms,
                            global_wait_ms=waited_global_ms,
                            duration_ms=duration_ms,
                        )
                        return result
                    except TimeoutError as exc:
                        duration_ms = int((time.monotonic() - write_start) * 1000)
                        await self._record_write_metrics(
                            success=False,
                            session_wait_ms=waited_session_ms,
                            global_wait_ms=waited_global_ms,
                            duration_ms=duration_ms,
                            error=str(exc),
                        )
                        raise
                    except asyncio.CancelledError:
                        if waited_global_ms <= 0:
                            waited_global_ms = int((time.monotonic() - global_wait_start) * 1000)
                        duration_ms = int((time.monotonic() - write_start) * 1000)
                        await asyncio.shield(
                            self._record_write_metrics(
                                success=False,
                                session_wait_ms=waited_session_ms,
                                global_wait_ms=waited_global_ms,
                                duration_ms=duration_ms,
                                error="cancelled",
                            )
                        )
                        raise
                    except Exception as exc:
                        duration_ms = int((time.monotonic() - write_start) * 1000)
                        await self._record_write_metrics(
                            success=False,
                            session_wait_ms=waited_session_ms,
                            global_wait_ms=waited_global_ms,
                            duration_ms=duration_ms,
                            error=str(exc),
                        )
                        raise
                    finally:
                        if global_waiting_counted:
                            with self._state_guard:
                                self._global_waiting = max(0, self._global_waiting - 1)

                        if global_active_counted:
                            with self._state_guard:
                                self._global_active = max(0, self._global_active - 1)

                        if global_acquired:
                            self._thread_global_sem.release()
                        if global_loop_acquired:
                            global_sem.release()
                finally:
                    if session_thread_acquired:
                        session_thread_lock.release()
        except asyncio.CancelledError:
            if session_waiting_counted:
                duration_ms = int((time.monotonic() - write_start) * 1000)
                waited_session_ms = int((time.monotonic() - session_wait_start) * 1000)
                await self._record_write_metrics(
                    success=False,
                    session_wait_ms=waited_session_ms,
                    global_wait_ms=0,
                    duration_ms=duration_ms,
                    error="cancelled",
                )
            raise
        finally:
            if session_waiting_counted:
                with self._state_guard:
                    self._decrement_session_waiting_unlocked(lane)
            await self._maybe_cleanup_session_lane(lane, session_lock_key, session_lock)

    async def status(self) -> Dict[str, Any]:
        self._ensure_loop_state()
        with self._state_guard:
            busy_sessions = {
                session: waiting
                for session, waiting in self._session_waiting.items()
                if waiting > 0
            }
            max_session_wait_ms = max(busy_sessions.values(), default=0)
            writes_total = max(0, self._writes_total)
            writes_failed = max(0, self._writes_failed)
            return {
                "global_concurrency": self._global_concurrency,
                "global_active": self._global_active,
                "global_waiting": self._global_waiting,
                "session_waiting_count": sum(busy_sessions.values()),
                "session_waiting_sessions": len(busy_sessions),
                "max_session_waiting": max_session_wait_ms,
                "wait_warn_ms": self._wait_warn_ms,
                "global_acquire_timeout_sec": round(
                    float(self._global_acquire_timeout_seconds), 3
                ),
                "task_timeout_sec": int(self._task_timeout_seconds),
                "lock_retry_attempts": self._lock_retry_attempts,
                "lock_retry_base_delay_ms": int(
                    round(self._lock_retry_base_delay_seconds * 1000)
                ),
                "lock_retry_max_delay_ms": int(
                    round(self._lock_retry_max_delay_seconds * 1000)
                ),
                "lock_retries_total": self._lock_retries_total,
                "lock_retries_exhausted": self._lock_retries_exhausted,
                "writes_total": writes_total,
                "writes_failed": writes_failed,
                "writes_success": max(0, self._writes_success),
                "failure_rate": (
                    round(writes_failed / writes_total, 6) if writes_total > 0 else 0.0
                ),
                "session_wait_ms_p95": self._p95(self._session_wait_samples),
                "global_wait_ms_p95": self._p95(self._global_wait_samples),
                "duration_ms_p95": self._p95(self._duration_samples),
                "last_error": self._last_error,
            }


class SessionSearchCache:
    """Ephemeral per-session retrieval cache used by session-first search."""

    def __init__(self) -> None:
        self._max_hits_per_session = _env_int(
            "RUNTIME_SESSION_CACHE_MAX_HITS", 200, minimum=20
        )
        self._max_sessions = _env_int(
            "RUNTIME_SESSION_CACHE_MAX_SESSIONS", 256, minimum=16
        )
        self._half_life_seconds = float(
            _env_int("RUNTIME_SESSION_CACHE_HALF_LIFE_SECONDS", 6 * 3600, minimum=60)
        )
        self._expiry_seconds = max(300.0, self._half_life_seconds * 4.0)
        self._hits: Dict[str, Deque[SessionSearchHit]] = {}
        self._session_last_seen: Dict[str, tuple[float, int]] = {}
        self._touch_sequence = 0
        self._guard = LoopBoundAsyncLock()

    @staticmethod
    def _parse_hit_timestamp(value: str, fallback: datetime) -> datetime:
        try:
            return _ensure_utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return fallback

    def _prune_expired_locked(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self._expiry_seconds)
        stale_sessions: List[str] = []
        for session_id, queue in self._hits.items():
            fresh_items = [
                item
                for item in queue
                if self._parse_hit_timestamp(item.updated_at, now) >= cutoff
            ]
            if not fresh_items:
                stale_sessions.append(session_id)
                continue
            if len(fresh_items) != len(queue):
                self._hits[session_id] = deque(
                    fresh_items,
                    maxlen=self._max_hits_per_session,
                )
        for session_id in stale_sessions:
            self._hits.pop(session_id, None)
            self._session_last_seen.pop(session_id, None)

    def _touch_session_locked(self, session_id: str, *, now_ts: Optional[float] = None) -> None:
        queue = self._hits.get(session_id)
        if queue is None:
            return
        self._touch_sequence += 1
        self._session_last_seen[session_id] = (
            float(time.monotonic() if now_ts is None else now_ts),
            self._touch_sequence,
        )
        self._hits.pop(session_id, None)
        self._hits[session_id] = queue

    def _evict_oldest_session_if_needed_locked(self) -> None:
        while len(self._hits) >= self._max_sessions:
            oldest_sid = min(
                self._hits.keys(),
                key=lambda sid: self._session_last_seen.get(sid, (float("-inf"), -1)),
            )
            self._hits.pop(oldest_sid, None)
            self._session_last_seen.pop(oldest_sid, None)

    async def record_hit(
        self,
        *,
        session_id: Optional[str],
        uri: str,
        memory_id: Optional[int],
        snippet: str,
        priority: Optional[int] = None,
        source: str = "runtime",
        updated_at: Optional[str] = None,
    ) -> None:
        sid = _normalize_session_id(session_id)
        clean_snippet = (snippet or "").strip()
        if not uri or not clean_snippet:
            return
        hit = SessionSearchHit(
            uri=uri,
            memory_id=memory_id,
            snippet=clean_snippet,
            updated_at=updated_at or _utc_iso_now(),
            priority=priority,
            source=source,
        )
        async with self._guard:
            self._prune_expired_locked(datetime.now(timezone.utc))
            queue = self._hits.get(sid)
            if queue is None:
                self._evict_oldest_session_if_needed_locked()
                queue = deque(maxlen=self._max_hits_per_session)
                self._hits[sid] = queue
            queue.append(hit)
            self._touch_session_locked(sid)

    async def search(
        self, *, session_id: Optional[str], query: str, limit: int
    ) -> List[Dict[str, Any]]:
        sid = _normalize_session_id(session_id)
        terms = _tokenize_query(query)
        if not terms:
            return []
        now = datetime.now(timezone.utc)
        async with self._guard:
            self._prune_expired_locked(now)
            queue = self._hits.get(sid)
            if queue is None:
                snapshot = []
            else:
                self._touch_session_locked(sid, now_ts=time.monotonic())
                snapshot = list(queue)
        if not snapshot:
            return []

        by_uri: Dict[str, Dict[str, Any]] = {}

        for item in snapshot:
            text = unicodedata.normalize("NFC", item.snippet).casefold()
            hits = sum(1 for term in terms if term in text)
            if hits <= 0:
                continue

            text_score = min(1.0, hits / max(1, len(terms)))
            updated_dt = self._parse_hit_timestamp(item.updated_at, now)

            age_seconds = max(0.0, (now - updated_dt).total_seconds())
            recency_score = math.exp(-age_seconds / self._half_life_seconds)
            priority_value = item.priority if isinstance(item.priority, int) else 0
            priority_score = 1.0 / (1.0 + max(0, priority_value))
            final_score = (0.70 * text_score) + (0.20 * recency_score) + (0.10 * priority_score)

            prev = by_uri.get(item.uri)
            candidate = {
                "uri": item.uri,
                "memory_id": item.memory_id,
                "snippet": item.snippet[:300],
                "priority": priority_value,
                "score": round(final_score, 6),
                "keyword_score": round(text_score, 6),
                "semantic_score": 0.0,
                "updated_at": item.updated_at,
                "source": item.source,
                "match_type": "session_queue",
            }
            if prev is None or candidate["score"] > prev["score"]:
                by_uri[item.uri] = candidate

        ranked = sorted(by_uri.values(), key=lambda row: row["score"], reverse=True)
        return ranked[: max(1, limit)]

    async def summary(self) -> Dict[str, Any]:
        """Return lightweight, process-local stats for SM-Lite observability."""
        now = datetime.now(timezone.utc)
        async with self._guard:
            self._prune_expired_locked(now)
            snapshot = {sid: len(queue) for sid, queue in self._hits.items()}

        session_count = len(snapshot)
        total_hits = sum(snapshot.values())
        max_hits = max(snapshot.values(), default=0)
        top_sessions = sorted(
            (
                {"session_id": session_id, "hits": hit_count}
                for session_id, hit_count in snapshot.items()
                if hit_count > 0
            ),
            key=lambda item: item["hits"],
            reverse=True,
        )[:5]

        return {
            "session_count": session_count,
            "total_hits": total_hits,
            "max_hits_in_session": max_hits,
            "max_hits_per_session": self._max_hits_per_session,
            "max_sessions": self._max_sessions,
            "half_life_seconds": self._half_life_seconds,
            "expiry_seconds": self._expiry_seconds,
            "top_sessions": top_sessions,
        }


class SessionFlushTracker:
    """Tracks session activity and produces compact flush summaries."""

    def __init__(self) -> None:
        self._trigger_chars = _env_int("RUNTIME_FLUSH_TRIGGER_CHARS", 6000, minimum=800)
        self._min_events = _env_int("RUNTIME_FLUSH_MIN_EVENTS", 6, minimum=1)
        self._max_events = _env_int("RUNTIME_FLUSH_MAX_EVENTS", 80, minimum=10)
        self._max_event_chars = _env_int(
            "RUNTIME_FLUSH_EVENT_MAX_CHARS",
            400,
            minimum=64,
        )
        self._max_sessions = _env_int("RUNTIME_FLUSH_MAX_SESSIONS", 256, minimum=16)
        self._rolling_summary_max_chars = _env_int(
            "RUNTIME_FLUSH_ROLLING_SUMMARY_MAX_CHARS", 2000, minimum=200
        )
        self._rolling_summary_tail_keep = _env_int(
            "RUNTIME_FLUSH_RECENT_TAIL_KEEP", 12, minimum=3
        )
        self._high_value_early_flush_enabled = _env_bool(
            "RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED",
            True,
        )
        self._high_value_min_events = _env_int(
            "RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS",
            2,
            minimum=1,
        )
        self._high_value_min_chars = _env_int(
            "RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS",
            120,
            minimum=32,
        )
        self._high_value_min_chars_cjk = _env_int(
            "RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS_CJK",
            min(self._high_value_min_chars, 100),
            minimum=16,
        )
        self._rollup_event_summary_max_chars = 220
        self._events: Dict[str, Deque[str]] = {}
        self._dropped_events: Dict[str, int] = {}
        self._truncated_events: Dict[str, int] = {}
        self._rolling_summaries: Dict[str, str] = {}
        self._rolled_events: Dict[str, int] = {}
        self._overflow_compactions: Dict[str, int] = {}
        self._pending_trigger_reasons: Dict[str, str] = {}
        self._flush_results_total = 0
        self._completed_flushes = 0
        self._persisted_flushes = 0
        self._trigger_breakdown: Counter[str] = Counter()
        self._result_reason_breakdown: Counter[str] = Counter()
        self._source_breakdown: Counter[str] = Counter()
        self._last_flush_session_id: Optional[str] = None
        self._last_trigger_reason: Optional[str] = None
        self._last_result_reason: Optional[str] = None
        self._last_source_hash: Optional[str] = None
        self._source_hash_observations = 0
        self._source_hash_changes = 0
        self._session_last_seen: Dict[str, tuple[float, int]] = {}
        self._touch_sequence = 0
        self._guard = LoopBoundAsyncLock()

    def _touch_session_locked(self, session_id: str, *, now_ts: Optional[float] = None) -> None:
        queue = self._events.get(session_id)
        if queue is None:
            return
        self._touch_sequence += 1
        self._session_last_seen[session_id] = (
            float(time.monotonic() if now_ts is None else now_ts),
            self._touch_sequence,
        )
        self._events.pop(session_id, None)
        self._events[session_id] = queue

    def _compress_event_to_rolling_locked(self, session_id: str, event_text: str) -> None:
        """Compress an overflow event into the rolling summary (extractive)."""
        compressed = _summarize_event_for_rollup(
            event_text,
            max_chars=self._rollup_event_summary_max_chars,
        )

        current = self._rolling_summaries.get(session_id, "")
        new_entry = f"* {compressed}"
        if current:
            candidate = f"{current}\n{new_entry}"
        else:
            candidate = new_entry

        if len(candidate) > self._rolling_summary_max_chars:
            lines_list = candidate.split('\n')
            while len('\n'.join(lines_list)) > self._rolling_summary_max_chars and len(lines_list) > 1:
                lines_list.pop(0)
            candidate = '\n'.join(lines_list)

        self._rolling_summaries[session_id] = candidate
        self._rolled_events[session_id] = self._rolled_events.get(session_id, 0) + 1
        self._overflow_compactions[session_id] = self._overflow_compactions.get(session_id, 0) + 1

    def _has_high_value_signal_locked(self, queue: List[str]) -> bool:
        return any(
            keyword in _normalize_inline_whitespace(item).casefold()
            for item in queue
            for keyword in _HIGH_VALUE_EARLY_FLUSH_KEYWORDS
        )

    def _has_high_value_diversity_locked(self, queue: List[str]) -> bool:
        required_unique = max(1, min(self._high_value_min_events, len(queue)))
        normalized_unique = {
            _normalize_inline_whitespace(item).casefold()
            for item in queue
            if _normalize_inline_whitespace(item)
        }
        return len(normalized_unique) >= required_unique

    def _queue_contains_cjk_locked(self, queue: List[str]) -> bool:
        return any(_CJK_QUERY_TOKEN_PATTERN.search(str(item or "")) for item in queue)

    def _effective_high_value_min_chars_locked(self, queue: List[str]) -> int:
        if self._queue_contains_cjk_locked(queue):
            return min(self._high_value_min_chars, self._high_value_min_chars_cjk)
        return self._high_value_min_chars

    def _evict_oldest_session_if_needed_locked(self) -> None:
        while len(self._events) >= self._max_sessions:
            oldest_sid = min(
                self._events.keys(),
                key=lambda sid: self._session_last_seen.get(sid, (float("-inf"), -1)),
            )
            self._events.pop(oldest_sid, None)
            self._session_last_seen.pop(oldest_sid, None)
            self._dropped_events.pop(oldest_sid, None)
            self._truncated_events.pop(oldest_sid, None)
            self._rolling_summaries.pop(oldest_sid, None)
            self._rolled_events.pop(oldest_sid, None)
            self._overflow_compactions.pop(oldest_sid, None)
            self._pending_trigger_reasons.pop(oldest_sid, None)

    async def record_event(self, *, session_id: Optional[str], message: str) -> None:
        text = (message or "").strip()
        if not text:
            return
        sid = _normalize_session_id(session_id)
        was_truncated = len(text) > self._max_event_chars
        normalized_text = text[: self._max_event_chars]
        async with self._guard:
            queue = self._events.get(sid)
            if queue is None:
                self._evict_oldest_session_if_needed_locked()
                queue = deque(maxlen=self._max_events)
                self._events[sid] = queue
            if len(queue) >= self._max_events:
                # P3-1: compress oldest event into rolling summary instead of dropping
                oldest_event = queue[0]  # peek at oldest before deque auto-drops it
                self._compress_event_to_rolling_locked(sid, oldest_event)
                logger.info(
                    "Flush tracker compressed oldest event into rolling summary for session %s "
                    "(max_events=%s, rolled_total=%s).",
                    sid,
                    self._max_events,
                    self._rolled_events.get(sid, 0),
                )
            if was_truncated:
                self._truncated_events[sid] = self._truncated_events.get(sid, 0) + 1
            queue.append(normalized_text)
            self._touch_session_locked(sid)

    async def should_flush(self, *, session_id: Optional[str]) -> bool:
        sid = _normalize_session_id(session_id)
        async with self._guard:
            queue = self._events.get(sid)
            if not queue:
                return False
            self._touch_session_locked(sid, now_ts=time.monotonic())
            queue_snapshot = list(queue)
            total_chars = sum(len(item) for item in queue_snapshot)
            normal_threshold_hit = (
                len(queue_snapshot) >= self._min_events and total_chars >= self._trigger_chars
            )
            if normal_threshold_hit:
                self._pending_trigger_reasons[sid] = "normal_threshold"
                return True
            if not self._high_value_early_flush_enabled:
                return False
            if len(queue_snapshot) < self._high_value_min_events:
                return False
            high_value_min_chars = self._effective_high_value_min_chars_locked(
                queue_snapshot
            )
            if total_chars < high_value_min_chars:
                return False
            if not self._has_high_value_signal_locked(queue_snapshot):
                return False
            high_value_hit = self._has_high_value_diversity_locked(queue_snapshot)
            if high_value_hit:
                self._pending_trigger_reasons[sid] = "high_value_early"
            return high_value_hit

    async def build_summary(self, *, session_id: Optional[str], limit: int = 12) -> str:
        sid = _normalize_session_id(session_id)
        async with self._guard:
            queue_ref = self._events.get(sid)
            if queue_ref:
                self._touch_session_locked(sid, now_ts=time.monotonic())
            queue = list(queue_ref or ())
            dropped_events = self._dropped_events.get(sid, 0)
            truncated_events = self._truncated_events.get(sid, 0)
            rolling_summary = self._rolling_summaries.get(sid, "")
            rolled_events = self._rolled_events.get(sid, 0)
            overflow_compactions = self._overflow_compactions.get(sid, 0)
        if not queue and dropped_events <= 0 and truncated_events <= 0 and not rolling_summary:
            return ""

        # Use the config as a floor for the tail window
        effective_tail = max(limit, self._rolling_summary_tail_keep)
        tail = queue[-max(1, effective_tail):]
        lines: List[str] = []

        # Meta
        lines.append("- [meta] summary_version: v2-progressive")

        # Audit section
        if dropped_events > 0:
            lines.append(
                f"- [audit] {dropped_events} older event(s) were dropped before flush "
                f"because max_events_per_session={self._max_events} was exceeded."
            )
        if truncated_events > 0:
            lines.append(
                f"- [audit] {truncated_events} event(s) were truncated to "
                f"{self._max_event_chars} chars before flush."
            )
        if rolled_events > 0:
            lines.append(
                f"- [audit] {rolled_events} older event(s) were compressed into rolling summary "
                f"({overflow_compactions} overflow compaction(s), {len(rolling_summary)} chars)."
            )

        # Rolling summary section (older events, compressed)
        if rolling_summary.strip():
            lines.append("")
            lines.append("## Older Events (rolling summary)")
            lines.append(rolling_summary)

        # Recent events section (raw tail)
        if tail:
            lines.append("")
            lines.append("## Recent Events")
            lines.extend(f"- {line}" for line in tail)

        return "Session compaction notes:\n" + "\n".join(lines)

    async def mark_flushed(self, *, session_id: Optional[str]) -> None:
        sid = _normalize_session_id(session_id)
        async with self._guard:
            self._events.pop(sid, None)
            self._dropped_events.pop(sid, None)
            self._truncated_events.pop(sid, None)
            self._session_last_seen.pop(sid, None)
            self._rolling_summaries.pop(sid, None)
            self._rolled_events.pop(sid, None)
            self._overflow_compactions.pop(sid, None)

    async def note_flush_result(
        self,
        *,
        session_id: Optional[str],
        source: str,
        flushed: bool,
        data_persisted: bool,
        result_reason: str,
        source_hash: Optional[str] = None,
        trigger_reason: Optional[str] = None,
    ) -> None:
        sid = _normalize_session_id(session_id)
        clean_trigger = _normalize_inline_whitespace(
            trigger_reason or self._pending_trigger_reasons.get(sid) or "unknown"
        ).lower() or "unknown"
        clean_result = _normalize_inline_whitespace(result_reason).lower() or "unknown"
        clean_source = _normalize_inline_whitespace(source).lower() or "unknown"
        clean_hash = _normalize_inline_whitespace(source_hash or "")

        async with self._guard:
            self._flush_results_total += 1
            if flushed:
                self._completed_flushes += 1
            if data_persisted:
                self._persisted_flushes += 1
            self._trigger_breakdown[clean_trigger] += 1
            self._result_reason_breakdown[clean_result] += 1
            self._source_breakdown[clean_source] += 1
            self._last_flush_session_id = sid
            self._last_trigger_reason = clean_trigger
            self._last_result_reason = clean_result
            if clean_hash:
                self._source_hash_observations += 1
                if self._last_source_hash and self._last_source_hash != clean_hash:
                    self._source_hash_changes += 1
                self._last_source_hash = clean_hash
            self._pending_trigger_reasons.pop(sid, None)

    async def pending_session_ids(self) -> List[str]:
        async with self._guard:
            return [sid for sid, queue in self._events.items() if queue]

    async def summary(self) -> Dict[str, Any]:
        """Return pending flush workload stats for SM-Lite observability."""
        async with self._guard:
            snapshot = {
                sid: {
                    "events": len(queue),
                    "chars": sum(len(item) for item in queue),
                    "dropped_events": self._dropped_events.get(sid, 0),
                    "truncated_events": self._truncated_events.get(sid, 0),
                    "rolled_events": self._rolled_events.get(sid, 0),
                    "rolling_summary_chars": len(self._rolling_summaries.get(sid, "")),
                    "overflow_compactions": self._overflow_compactions.get(sid, 0),
                }
                for sid, queue in self._events.items()
                if queue
            }

        session_count = len(snapshot)
        pending_events = sum(item["events"] for item in snapshot.values())
        pending_chars = sum(item["chars"] for item in snapshot.values())
        dropped_events = sum(item["dropped_events"] for item in snapshot.values())
        truncated_events = sum(item["truncated_events"] for item in snapshot.values())
        rolled_events = sum(item["rolled_events"] for item in snapshot.values())
        rolling_summary_chars = sum(item["rolling_summary_chars"] for item in snapshot.values())
        overflow_compactions = sum(item["overflow_compactions"] for item in snapshot.values())
        write_guard_deduped = self._result_reason_breakdown.get("write_guard_deduped", 0)
        completed_flushes = max(0, self._completed_flushes)
        top_sessions = sorted(
            (
                {
                    "session_id": session_id,
                    "events": stats["events"],
                    "chars": stats["chars"],
                    "dropped_events": stats["dropped_events"],
                    "truncated_events": stats["truncated_events"],
                    "rolled_events": stats["rolled_events"],
                    "rolling_summary_chars": stats["rolling_summary_chars"],
                    "overflow_compactions": stats["overflow_compactions"],
                }
                for session_id, stats in snapshot.items()
            ),
            key=lambda item: (
                item["dropped_events"],
                item["rolled_events"],
                item["truncated_events"],
                item["events"],
                item["chars"],
            ),
            reverse=True,
        )[:5]

        return {
            "session_count": session_count,
            "pending_events": pending_events,
            "pending_chars": pending_chars,
            "dropped_events": dropped_events,
            "truncated_events": truncated_events,
            "rolled_events": rolled_events,
            "rolling_summary_chars": rolling_summary_chars,
            "overflow_compactions": overflow_compactions,
            "trigger_chars": self._trigger_chars,
            "min_events": self._min_events,
            "max_events_per_session": self._max_events,
            "event_max_chars": self._max_event_chars,
            "max_sessions": self._max_sessions,
            "flush_results_total": self._flush_results_total,
            "completed_flushes": completed_flushes,
            "persisted_flushes": self._persisted_flushes,
            "early_flush_count": self._trigger_breakdown.get("high_value_early", 0),
            "trigger_breakdown": dict(self._trigger_breakdown),
            "result_reason_breakdown": dict(self._result_reason_breakdown),
            "source_breakdown": dict(self._source_breakdown),
            "last_flush_session_id": self._last_flush_session_id,
            "last_trigger_reason": self._last_trigger_reason,
            "last_result_reason": self._last_result_reason,
            "last_source_hash": self._last_source_hash,
            "source_hash_observations": self._source_hash_observations,
            "source_hash_changes": self._source_hash_changes,
            "write_guard_deduped_ratio": (
                round(write_guard_deduped / completed_flushes, 6)
                if completed_flushes > 0
                else 0.0
            ),
            "top_sessions": top_sessions,
        }


@dataclass
class GuardDecisionEvent:
    timestamp: str
    operation: str
    action: str
    method: str
    reason: str
    target_id: Optional[int]
    blocked: bool
    degraded: bool
    degrade_reasons: List[str]


class GuardDecisionTracker:
    """In-process observability tracker for write_guard decisions."""

    def __init__(self) -> None:
        self._max_events = _env_int("RUNTIME_GUARD_EVENT_LIMIT", 300, minimum=50)
        self._events: Deque[GuardDecisionEvent] = deque(maxlen=self._max_events)
        self._guard = LoopBoundAsyncLock()

    async def record_event(
        self,
        *,
        operation: str,
        action: str,
        method: str,
        reason: str,
        target_id: Optional[int] = None,
        blocked: bool = False,
        degraded: bool = False,
        degrade_reasons: Optional[List[str]] = None,
    ) -> None:
        event = GuardDecisionEvent(
            timestamp=_utc_iso_now(),
            operation=(operation or "unknown").strip() or "unknown",
            action=(action or "unknown").strip().upper() or "UNKNOWN",
            method=(method or "unknown").strip().lower() or "unknown",
            reason=(reason or "").strip(),
            target_id=target_id if isinstance(target_id, int) else None,
            blocked=bool(blocked),
            degraded=bool(degraded),
            degrade_reasons=[
                item
                for item in (degrade_reasons or [])
                if isinstance(item, str) and item.strip()
            ],
        )
        async with self._guard:
            self._events.append(event)

    async def summary(self) -> Dict[str, Any]:
        async with self._guard:
            snapshot = list(self._events)

        if not snapshot:
            return {
                "window_size": self._max_events,
                "total_events": 0,
                "blocked_events": 0,
                "degraded_events": 0,
                "action_breakdown": {},
                "method_breakdown": {},
                "operation_breakdown": {},
                "top_reasons": [],
                "last_event_at": None,
            }

        action_counter = Counter(item.action for item in snapshot)
        method_counter = Counter(item.method for item in snapshot)
        operation_counter = Counter(item.operation for item in snapshot)
        reason_counter = Counter(item.reason for item in snapshot if item.reason)
        blocked_events = sum(1 for item in snapshot if item.blocked)
        degraded_events = sum(1 for item in snapshot if item.degraded)

        return {
            "window_size": self._max_events,
            "total_events": len(snapshot),
            "blocked_events": blocked_events,
            "degraded_events": degraded_events,
            "action_breakdown": dict(action_counter),
            "method_breakdown": dict(method_counter),
            "operation_breakdown": dict(operation_counter),
            "top_reasons": [
                {"reason": reason, "count": count}
                for reason, count in reason_counter.most_common(5)
            ],
            "last_event_at": snapshot[-1].timestamp,
        }


@dataclass
class ImportLearnAuditEvent:
    timestamp: str
    event_type: str
    operation: str
    decision: str
    reason: str
    source: str
    session_id: str
    actor_id: Optional[str]
    batch_id: Optional[str]
    metadata: Dict[str, Any]


class ImportLearnAuditTracker:
    """In-process audit tracker for import/learn/reject/rollback workflows."""

    _ALLOWED_EVENT_TYPES = {"import", "learn", "reject", "rollback", "unknown"}
    _ALLOWED_DECISIONS = {"accepted", "rejected", "executed", "rolled_back", "unknown"}

    def __init__(self) -> None:
        self._max_events = _env_int(
            "RUNTIME_IMPORT_LEARN_AUDIT_LIMIT", 300, minimum=50
        )
        self._events: Deque[ImportLearnAuditEvent] = deque(maxlen=self._max_events)
        self._guard = LoopBoundAsyncLock()

    async def record_event(
        self,
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
    ) -> None:
        normalized_event_type = (
            (event_type or "unknown").strip().lower() or "unknown"
        )
        if normalized_event_type not in self._ALLOWED_EVENT_TYPES:
            normalized_event_type = "unknown"

        normalized_decision = (decision or "unknown").strip().lower() or "unknown"
        if normalized_decision not in self._ALLOWED_DECISIONS:
            normalized_decision = "unknown"

        event = ImportLearnAuditEvent(
            timestamp=_utc_iso_now(),
            event_type=normalized_event_type,
            operation=(operation or "unknown").strip() or "unknown",
            decision=normalized_decision,
            reason=(reason or "").strip(),
            source=(source or "unknown").strip() or "unknown",
            session_id=_normalize_session_id(session_id),
            actor_id=(str(actor_id).strip() if actor_id is not None else None) or None,
            batch_id=(str(batch_id).strip() if batch_id is not None else None) or None,
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
        )
        async with self._guard:
            self._events.append(event)

    async def summary(self) -> Dict[str, Any]:
        async with self._guard:
            snapshot = list(self._events)

        if not snapshot:
            return {
                "window_size": self._max_events,
                "total_events": 0,
                "event_type_breakdown": {},
                "operation_breakdown": {},
                "decision_breakdown": {},
                "rejected_events": 0,
                "rollback_events": 0,
                "top_reasons": [],
                "last_event_at": None,
                "recent_events": [],
            }

        event_type_counter = Counter(item.event_type for item in snapshot)
        operation_counter = Counter(item.operation for item in snapshot)
        decision_counter = Counter(item.decision for item in snapshot)
        reason_counter = Counter(item.reason for item in snapshot if item.reason)
        rejected_events = sum(
            1
            for item in snapshot
            if item.decision == "rejected" or item.event_type == "reject"
        )
        rollback_events = sum(
            1
            for item in snapshot
            if item.decision == "rolled_back" or item.event_type == "rollback"
        )
        recent_events = [
            {
                "timestamp": item.timestamp,
                "event_type": item.event_type,
                "operation": item.operation,
                "decision": item.decision,
                "reason": item.reason,
                "source": item.source,
                "session_id": item.session_id,
                "actor_id": item.actor_id,
                "batch_id": item.batch_id,
                "metadata": dict(item.metadata),
            }
            for item in snapshot[-5:]
        ]

        return {
            "window_size": self._max_events,
            "total_events": len(snapshot),
            "event_type_breakdown": dict(event_type_counter),
            "operation_breakdown": dict(operation_counter),
            "decision_breakdown": dict(decision_counter),
            "rejected_events": rejected_events,
            "rollback_events": rollback_events,
            "top_reasons": [
                {"reason": reason, "count": count}
                for reason, count in reason_counter.most_common(5)
            ],
            "last_event_at": snapshot[-1].timestamp,
            "recent_events": recent_events,
        }


@dataclass
class SessionPromotionEvent:
    timestamp: str
    session_id: str
    source: str
    trigger_reason: str
    uri: str
    memory_id: Optional[int]
    gist_method: str
    quality: float
    degraded: bool
    degrade_reasons: List[str]
    index_queued: int
    index_dropped: int
    index_deduped: int


class SessionPromotionTracker:
    """In-process tracker for SM-Lite promotion events."""

    def __init__(self) -> None:
        self._max_events = _env_int("RUNTIME_PROMOTION_EVENT_LIMIT", 200, minimum=20)
        self._events: Deque[SessionPromotionEvent] = deque(maxlen=self._max_events)
        self._guard = LoopBoundAsyncLock()

    async def record_event(
        self,
        *,
        session_id: Optional[str],
        source: str,
        trigger_reason: str,
        uri: str,
        memory_id: Optional[int],
        gist_method: str,
        quality: Optional[float],
        degraded: bool = False,
        degrade_reasons: Optional[List[str]] = None,
        index_queued: int = 0,
        index_dropped: int = 0,
        index_deduped: int = 0,
    ) -> None:
        def _safe_non_negative_int(value: Any) -> int:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return 0

        event = SessionPromotionEvent(
            timestamp=_utc_iso_now(),
            session_id=_normalize_session_id(session_id),
            source=(source or "compact_context").strip().lower() or "compact_context",
            trigger_reason=(trigger_reason or "manual").strip() or "manual",
            uri=(uri or "").strip(),
            memory_id=memory_id if isinstance(memory_id, int) and memory_id > 0 else None,
            gist_method=(gist_method or "unknown").strip().lower() or "unknown",
            quality=float(quality) if isinstance(quality, (int, float)) else 0.0,
            degraded=bool(degraded),
            degrade_reasons=[
                item
                for item in (degrade_reasons or [])
                if isinstance(item, str) and item.strip()
            ],
            index_queued=_safe_non_negative_int(index_queued),
            index_dropped=_safe_non_negative_int(index_dropped),
            index_deduped=_safe_non_negative_int(index_deduped),
        )
        async with self._guard:
            self._events.append(event)

    async def summary(self) -> Dict[str, Any]:
        async with self._guard:
            snapshot = list(self._events)

        if not snapshot:
            return {
                "window_size": self._max_events,
                "total_promotions": 0,
                "degraded_promotions": 0,
                "source_breakdown": {},
                "reason_breakdown": {},
                "gist_method_breakdown": {},
                "avg_quality": 0.0,
                "index_queue": {
                    "queued": 0,
                    "dropped": 0,
                    "deduped": 0,
                },
                "top_sessions": [],
                "last_promotion_at": None,
            }

        source_counter = Counter(item.source for item in snapshot)
        reason_counter = Counter(item.trigger_reason for item in snapshot)
        gist_counter = Counter(item.gist_method for item in snapshot)
        session_counter = Counter(item.session_id for item in snapshot)
        degraded_promotions = sum(1 for item in snapshot if item.degraded)
        quality_values = [max(0.0, min(1.0, item.quality)) for item in snapshot]
        avg_quality = sum(quality_values) / max(1, len(quality_values))
        index_queued = sum(item.index_queued for item in snapshot)
        index_dropped = sum(item.index_dropped for item in snapshot)
        index_deduped = sum(item.index_deduped for item in snapshot)

        return {
            "window_size": self._max_events,
            "total_promotions": len(snapshot),
            "degraded_promotions": degraded_promotions,
            "source_breakdown": dict(source_counter),
            "reason_breakdown": dict(reason_counter),
            "gist_method_breakdown": dict(gist_counter),
            "avg_quality": round(avg_quality, 6),
            "index_queue": {
                "queued": index_queued,
                "dropped": index_dropped,
                "deduped": index_deduped,
            },
            "top_sessions": [
                {"session_id": session_id, "count": count}
                for session_id, count in session_counter.most_common(5)
            ],
            "last_promotion_at": snapshot[-1].timestamp,
        }


@dataclass
class CleanupReviewRecord:
    review_id: str
    token: str
    confirmation_phrase: str
    action: str
    reviewer: str
    selections: List[Dict[str, Any]]
    created_at: float
    expires_at: float


class CleanupReviewCoordinator:
    """Ephemeral confirmation flow for risky cleanup actions."""

    def __init__(self) -> None:
        self._default_ttl_seconds = _env_int(
            "RUNTIME_CLEANUP_REVIEW_TTL_SECONDS", 900, minimum=60
        )
        self._max_pending = _env_int("RUNTIME_CLEANUP_REVIEW_MAX_PENDING", 64, minimum=8)
        self._records: Dict[str, CleanupReviewRecord] = {}
        self._guard = LoopBoundAsyncLock()

    def _cleanup_expired_locked(self, now_ts: float) -> None:
        expired = [
            key for key, record in self._records.items() if record.expires_at <= now_ts
        ]
        for key in expired:
            self._records.pop(key, None)

    async def create_review(
        self,
        *,
        action: str,
        selections: List[Dict[str, Any]],
        reviewer: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        normalized_action = (action or "delete").strip().lower() or "delete"
        normalized_reviewer = (reviewer or "").strip() or "human"
        normalized_ttl = (
            self._default_ttl_seconds
            if ttl_seconds is None
            else max(60, int(ttl_seconds))
        )
        now_ts = time.time()
        expires_at = now_ts + float(normalized_ttl)
        review_id = f"cleanup-{uuid.uuid4().hex[:10]}"
        token = uuid.uuid4().hex
        confirmation_phrase = f"CONFIRM {normalized_action.upper()} {len(selections)}"
        record = CleanupReviewRecord(
            review_id=review_id,
            token=token,
            confirmation_phrase=confirmation_phrase,
            action=normalized_action,
            reviewer=normalized_reviewer,
            selections=selections,
            created_at=now_ts,
            expires_at=expires_at,
        )

        async with self._guard:
            self._cleanup_expired_locked(now_ts)
            while len(self._records) >= self._max_pending:
                oldest_key = min(
                    self._records.items(), key=lambda item: item[1].created_at
                )[0]
                self._records.pop(oldest_key, None)
            self._records[review_id] = record

        return {
            "review_id": review_id,
            "token": token,
            "confirmation_phrase": confirmation_phrase,
            "action": normalized_action,
            "reviewer": normalized_reviewer,
            "expires_at": datetime.fromtimestamp(expires_at, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }

    async def consume_review(
        self,
        *,
        review_id: str,
        token: str,
        confirmation_phrase: str,
    ) -> Dict[str, Any]:
        review_id_value = (review_id or "").strip()
        token_value = (token or "").strip()
        phrase_value = (confirmation_phrase or "").strip()
        if not review_id_value:
            return {"ok": False, "error": "review_id is required"}
        if not token_value:
            return {"ok": False, "error": "token is required"}
        if not phrase_value:
            return {"ok": False, "error": "confirmation_phrase is required"}

        now_ts = time.time()
        async with self._guard:
            self._cleanup_expired_locked(now_ts)
            record = self._records.get(review_id_value)
            if record is None:
                return {"ok": False, "error": "review_not_found_or_expired"}
            if not hmac.compare_digest(record.token, token_value):
                return {"ok": False, "error": "invalid_review_token"}
            if not hmac.compare_digest(record.confirmation_phrase, phrase_value):
                return {"ok": False, "error": "confirmation_phrase_mismatch"}
            self._records.pop(review_id_value, None)

        return {
            "ok": True,
            "review": {
                "review_id": record.review_id,
                "action": record.action,
                "reviewer": record.reviewer,
                "selections": list(record.selections),
                "created_at": datetime.fromtimestamp(record.created_at, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "expires_at": datetime.fromtimestamp(record.expires_at, timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            },
        }

    async def summary(self) -> Dict[str, Any]:
        now_ts = time.time()
        async with self._guard:
            self._cleanup_expired_locked(now_ts)
            pending = list(self._records.values())
        return {
            "pending_reviews": len(pending),
            "default_ttl_seconds": self._default_ttl_seconds,
            "max_pending": self._max_pending,
        }


class VitalityDecayCoordinator:
    """Single-flight wrapper around daily vitality decay."""

    def __init__(self) -> None:
        self._guard = LoopBoundAsyncLock()
        self._check_interval_seconds = _env_int(
            "RUNTIME_VITALITY_DECAY_CHECK_INTERVAL_SECONDS", 600, minimum=10
        )
        self._last_check_ts = 0.0
        self._last_result: Dict[str, Any] = {
            "applied": False,
            "reason": "not_started",
        }

    async def run_decay(
        self,
        *,
        client_factory: Callable[[], Any],
        force: bool = False,
        reason: str = "runtime",
    ) -> Dict[str, Any]:
        async with self._guard:
            now_ts = time.time()
            if (
                not force
                and self._last_check_ts > 0
                and (now_ts - self._last_check_ts) < self._check_interval_seconds
            ):
                return dict(self._last_result)

            if not callable(client_factory):
                self._last_result = {
                    "applied": False,
                    "degraded": True,
                    "reason": "client_factory_unavailable",
                }
                self._last_check_ts = now_ts
                return dict(self._last_result)

            client = client_factory()
            decay_method = getattr(client, "apply_vitality_decay", None)
            if not callable(decay_method):
                self._last_result = {
                    "applied": False,
                    "degraded": True,
                    "reason": "apply_vitality_decay_unavailable",
                }
                self._last_check_ts = now_ts
                return dict(self._last_result)

            try:
                payload = decay_method(force=bool(force), reason=(reason or "runtime"))
                if inspect.isawaitable(payload):
                    payload = await payload
                if not isinstance(payload, dict):
                    payload = {"applied": False, "raw": payload}
                payload.setdefault("degraded", False)
            except Exception as exc:
                payload = {
                    "applied": False,
                    "degraded": True,
                    "reason": str(exc),
                }

            self._last_result = payload
            self._last_check_ts = now_ts
            return dict(payload)

    async def status(self) -> Dict[str, Any]:
        async with self._guard:
            return {
                **dict(self._last_result),
                "check_interval_seconds": self._check_interval_seconds,
            }


@dataclass
class IndexTask:
    job_id: str
    task_type: str
    memory_id: Optional[int]
    reason: str
    requested_at: str


class IndexTaskWorker:
    """Background worker that executes reindex/rebuild tasks serially."""

    _FINAL_STATES: Set[str] = {"succeeded", "failed", "dropped", "cancelled"}

    def __init__(self) -> None:
        self._enabled = _env_bool("RUNTIME_INDEX_WORKER_ENABLED", True)
        self._queue_maxsize = _env_int("RUNTIME_INDEX_QUEUE_MAXSIZE", 256, minimum=8)
        self._recent_limit = _env_int("RUNTIME_INDEX_RECENT_JOBS", 30, minimum=5)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue[IndexTask]] = None
        self._client_factory: Optional[Callable[[], Any]] = None
        self._runner: Optional[asyncio.Task] = None
        self._guard: Optional[asyncio.Lock] = None

        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._job_events: Dict[str, asyncio.Event] = {}
        self._recent_job_ids: Deque[str] = deque()
        self._pending_memory_jobs: Dict[int, str] = {}
        self._rebuild_job_id: Optional[str] = None
        self._sleep_job_id: Optional[str] = None

        self._enqueued_total = 0
        self._succeeded_total = 0
        self._failed_total = 0
        self._dropped_total = 0
        self._cancelled_total = 0
        self._active_job_id: Optional[str] = None
        self._active_execution_task: Optional[asyncio.Task[Any]] = None
        self._last_error: Optional[str] = None
        self._last_finished_at: Optional[str] = None
        self._cancelled_job_ids: Set[str] = set()
        self._generation = 0

    def _ensure_loop_state(self) -> None:
        current_loop = asyncio.get_running_loop()
        if (
            self._loop is current_loop
            and self._queue is not None
            and self._guard is not None
        ):
            return

        previous_loop = self._loop
        previous_runner = self._runner
        previous_execution_task = self._active_execution_task
        if previous_loop is not None and previous_loop is not current_loop:
            self._mark_pending_jobs_loop_reset()
            self._cancel_stale_loop_tasks(
                previous_loop,
                previous_runner,
                previous_execution_task,
            )
        self._generation += 1
        self._loop = current_loop
        self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._guard = asyncio.Lock()
        self._runner = None
        self._active_execution_task = None
        self._active_job_id = None
        self._pending_memory_jobs.clear()
        self._rebuild_job_id = None
        self._sleep_job_id = None
        self._cancelled_job_ids.clear()

    @staticmethod
    def _cancel_task_if_pending(task: Optional[asyncio.Task[Any]]) -> None:
        if task is None or task.done():
            return
        task.cancel()

    def _cancel_stale_loop_tasks(
        self,
        previous_loop: asyncio.AbstractEventLoop,
        previous_runner: Optional[asyncio.Task[Any]],
        previous_execution_task: Optional[asyncio.Task[Any]],
    ) -> None:
        for task in (previous_execution_task, previous_runner):
            if task is None or task.done():
                continue
            try:
                if previous_loop.is_running():
                    previous_loop.call_soon_threadsafe(
                        self._cancel_task_if_pending,
                        task,
                    )
                else:
                    self._cancel_task_if_pending(task)
            except RuntimeError:
                continue

    async def _run_on_owner_loop(
        self,
        operation: Callable[[], Awaitable[Any]],
    ) -> Any:
        owner_loop = self._loop
        current_loop = asyncio.get_running_loop()
        if owner_loop is None or owner_loop is current_loop or not owner_loop.is_running():
            return await operation()
        try:
            future = asyncio.run_coroutine_threadsafe(operation(), owner_loop)
        except RuntimeError:
            return await operation()
        return await asyncio.wrap_future(future)

    def _mark_pending_jobs_loop_reset(self) -> None:
        finished_at = _utc_iso_now()
        for job_id, record in list(self._jobs.items()):
            status = str(record.get("status") or "")
            if status in self._FINAL_STATES:
                continue
            record["status"] = "failed"
            record["error"] = "event_loop_reset"
            record["finished_at"] = finished_at
            self._failed_total += 1
            self._last_error = "event_loop_reset"
            self._last_finished_at = finished_at
            event = self._job_events.get(job_id)
            if event is not None:
                event.set()
            self._append_recent_job_locked(job_id)
        self._job_events = {
            job_id: event
            for job_id, event in self._job_events.items()
            if str(self._jobs.get(job_id, {}).get("status") or "") not in self._FINAL_STATES
        }

    async def ensure_started(self, client_factory: Callable[[], Any]) -> None:
        async def _impl() -> None:
            if not self._enabled:
                return
            self._ensure_loop_state()
            assert self._guard is not None
            async with self._guard:
                self._client_factory = client_factory
                if self._runner is None or self._runner.done():
                    generation = self._generation
                    queue = self._queue
                    guard = self._guard
                    assert queue is not None
                    self._runner = asyncio.create_task(
                        self._run_loop(generation=generation, queue=queue, guard=guard),
                        name="runtime-index-worker",
                    )

        await self._run_on_owner_loop(_impl)

    async def shutdown(self) -> None:
        async def _impl() -> None:
            runner: Optional[asyncio.Task] = None
            self._ensure_loop_state()
            assert self._guard is not None
            async with self._guard:
                runner = self._runner
                self._runner = None
            if runner is None:
                return
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass

        await self._run_on_owner_loop(_impl)

    async def enqueue_reindex_memory(
        self,
        *,
        memory_id: int,
        reason: str = "write",
    ) -> Dict[str, Any]:
        async def _impl() -> Dict[str, Any]:
            if memory_id <= 0:
                raise ValueError("memory_id must be a positive integer.")
            if not self._enabled:
                return {"queued": False, "reason": "index_worker_disabled"}

            self._ensure_loop_state()
            assert self._guard is not None and self._queue is not None
            requested_at = _utc_iso_now()
            async with self._guard:
                existing_job_id = self._pending_memory_jobs.get(memory_id)
                if existing_job_id:
                    return {
                        "queued": False,
                        "deduped": True,
                        "job_id": existing_job_id,
                        "memory_id": memory_id,
                    }

                job_id = f"idx-{uuid.uuid4().hex[:10]}"
                task = IndexTask(
                    job_id=job_id,
                    task_type="reindex_memory",
                    memory_id=memory_id,
                    reason=reason or "write",
                    requested_at=requested_at,
                )
                event = asyncio.Event()
                self._job_events[job_id] = event
                self._jobs[job_id] = {
                    "job_id": job_id,
                    "task_type": task.task_type,
                    "memory_id": memory_id,
                    "reason": task.reason,
                    "requested_at": requested_at,
                    "status": "queued",
                }
                self._pending_memory_jobs[memory_id] = job_id

                try:
                    self._queue.put_nowait(task)
                except asyncio.QueueFull:
                    self._jobs[job_id]["status"] = "dropped"
                    self._jobs[job_id]["error"] = "queue_full"
                    self._jobs[job_id]["finished_at"] = _utc_iso_now()
                    self._dropped_total += 1
                    self._pending_memory_jobs.pop(memory_id, None)
                    event.set()
                    self._append_recent_job_locked(job_id)
                    return {
                        "queued": False,
                        "dropped": True,
                        "job_id": job_id,
                        "memory_id": memory_id,
                        "reason": "queue_full",
                    }

                self._enqueued_total += 1
                return {
                    "queued": True,
                    "job_id": job_id,
                    "memory_id": memory_id,
                }

        return await self._run_on_owner_loop(_impl)

    async def enqueue_rebuild(self, *, reason: str = "manual") -> Dict[str, Any]:
        async def _impl() -> Dict[str, Any]:
            if not self._enabled:
                return {"queued": False, "reason": "index_worker_disabled"}

            self._ensure_loop_state()
            assert self._guard is not None and self._queue is not None
            requested_at = _utc_iso_now()
            async with self._guard:
                if self._rebuild_job_id:
                    return {
                        "queued": False,
                        "deduped": True,
                        "job_id": self._rebuild_job_id,
                    }

                job_id = f"idx-{uuid.uuid4().hex[:10]}"
                task = IndexTask(
                    job_id=job_id,
                    task_type="rebuild_index",
                    memory_id=None,
                    reason=reason or "manual",
                    requested_at=requested_at,
                )
                event = asyncio.Event()
                self._job_events[job_id] = event
                self._jobs[job_id] = {
                    "job_id": job_id,
                    "task_type": task.task_type,
                    "reason": task.reason,
                    "requested_at": requested_at,
                    "status": "queued",
                }
                self._rebuild_job_id = job_id

                try:
                    self._queue.put_nowait(task)
                except asyncio.QueueFull:
                    self._jobs[job_id]["status"] = "dropped"
                    self._jobs[job_id]["error"] = "queue_full"
                    self._jobs[job_id]["finished_at"] = _utc_iso_now()
                    self._dropped_total += 1
                    self._rebuild_job_id = None
                    event.set()
                    self._append_recent_job_locked(job_id)
                    return {
                        "queued": False,
                        "dropped": True,
                        "job_id": job_id,
                        "reason": "queue_full",
                    }

                self._enqueued_total += 1
                return {"queued": True, "job_id": job_id}

        return await self._run_on_owner_loop(_impl)

    async def enqueue_sleep_consolidation(
        self,
        *,
        reason: str = "sleep_cycle",
    ) -> Dict[str, Any]:
        async def _impl() -> Dict[str, Any]:
            if not self._enabled:
                return {"queued": False, "reason": "index_worker_disabled"}

            self._ensure_loop_state()
            assert self._guard is not None and self._queue is not None
            requested_at = _utc_iso_now()
            async with self._guard:
                if self._sleep_job_id:
                    return {
                        "queued": False,
                        "deduped": True,
                        "job_id": self._sleep_job_id,
                    }

                job_id = f"idx-{uuid.uuid4().hex[:10]}"
                task = IndexTask(
                    job_id=job_id,
                    task_type="sleep_consolidation",
                    memory_id=None,
                    reason=reason or "sleep_cycle",
                    requested_at=requested_at,
                )
                event = asyncio.Event()
                self._job_events[job_id] = event
                self._jobs[job_id] = {
                    "job_id": job_id,
                    "task_type": task.task_type,
                    "reason": task.reason,
                    "requested_at": requested_at,
                    "status": "queued",
                }
                self._sleep_job_id = job_id

                try:
                    self._queue.put_nowait(task)
                except asyncio.QueueFull:
                    self._jobs[job_id]["status"] = "dropped"
                    self._jobs[job_id]["error"] = "queue_full"
                    self._jobs[job_id]["finished_at"] = _utc_iso_now()
                    self._dropped_total += 1
                    self._sleep_job_id = None
                    event.set()
                    self._append_recent_job_locked(job_id)
                    return {
                        "queued": False,
                        "dropped": True,
                        "job_id": job_id,
                        "reason": "queue_full",
                    }

                self._enqueued_total += 1
                return {"queued": True, "job_id": job_id}

        return await self._run_on_owner_loop(_impl)

    async def wait_for_job(
        self, *, job_id: str, timeout_seconds: float = 10.0
    ) -> Dict[str, Any]:
        async def _impl() -> Dict[str, Any]:
            if not job_id:
                return {"ok": False, "error": "job_id is required."}
            self._ensure_loop_state()
            assert self._guard is not None
            async with self._guard:
                job = dict(self._jobs.get(job_id, {}))
                event = self._job_events.get(job_id)
            if not job:
                return {"ok": False, "error": f"job '{job_id}' not found."}
            if job.get("status") in self._FINAL_STATES or event is None:
                return {"ok": True, "job": job}
            try:
                await asyncio.wait_for(
                    event.wait(),
                    timeout=max(0.1, float(timeout_seconds)),
                )
            except asyncio.TimeoutError:
                pass
            async with self._guard:
                current = dict(self._jobs.get(job_id, {}))
            return {"ok": True, "job": current}

        return await self._run_on_owner_loop(_impl)

    async def get_job(self, *, job_id: str) -> Dict[str, Any]:
        async def _impl() -> Dict[str, Any]:
            self._ensure_loop_state()
            assert self._guard is not None
            async with self._guard:
                job = self._jobs.get(job_id)
                if not job:
                    return {"ok": False, "error": f"job '{job_id}' not found."}
                return {"ok": True, "job": dict(job)}

        return await self._run_on_owner_loop(_impl)

    async def cancel_job(
        self,
        *,
        job_id: str,
        reason: str = "manual_cancel",
    ) -> Dict[str, Any]:
        async def _impl() -> Dict[str, Any]:
            normalized_job_id = (job_id or "").strip()
            if not normalized_job_id:
                return {"ok": False, "error": "job_id is required."}

            cancel_reason = (reason or "").strip() or "manual_cancel"
            cancellation_ts = _utc_iso_now()
            execution_task: Optional[asyncio.Task[Any]] = None

            self._ensure_loop_state()
            assert self._guard is not None
            async with self._guard:
                job = self._jobs.get(normalized_job_id)
                if not job:
                    return {
                        "ok": False,
                        "error": f"job '{normalized_job_id}' not found.",
                    }

                status = str(job.get("status") or "")
                if status in self._FINAL_STATES:
                    return {
                        "ok": False,
                        "error": "job_already_finalized",
                        "job": dict(job),
                    }

                if status == "queued":
                    job["status"] = "cancelled"
                    job["cancel_reason"] = cancel_reason
                    job["cancelled_at"] = cancellation_ts
                    job["finished_at"] = cancellation_ts
                    self._cancelled_total += 1
                    self._cancelled_job_ids.add(normalized_job_id)
                    if job.get("task_type") == "rebuild_index":
                        if self._rebuild_job_id == normalized_job_id:
                            self._rebuild_job_id = None
                    elif job.get("task_type") == "sleep_consolidation":
                        if self._sleep_job_id == normalized_job_id:
                            self._sleep_job_id = None
                    else:
                        memory_id = job.get("memory_id")
                        if isinstance(memory_id, int):
                            self._pending_memory_jobs.pop(memory_id, None)
                    event = self._job_events.get(normalized_job_id)
                    if event is not None:
                        event.set()
                    self._append_recent_job_locked(normalized_job_id)
                    self._last_finished_at = cancellation_ts
                    return {"ok": True, "cancelled": True, "job": dict(job)}

                if status in {"running", "cancelling"}:
                    if status != "cancelling":
                        job["status"] = "cancelling"
                        job["cancel_requested"] = True
                        job["cancel_reason"] = cancel_reason
                        job["cancel_requested_at"] = cancellation_ts
                    execution_task = (
                        self._active_execution_task
                        if self._active_job_id == normalized_job_id
                        else None
                    )
                    if execution_task is None:
                        return {
                            "ok": False,
                            "error": "running_job_handle_unavailable",
                            "job": dict(job),
                        }
                    snapshot = dict(job)
                else:
                    return {
                        "ok": False,
                        "error": f"job_not_cancellable_from_status:{status or 'unknown'}",
                        "job": dict(job),
                    }

            execution_task.cancel()
            return {"ok": True, "cancel_requested": True, "job": snapshot}

        return await self._run_on_owner_loop(_impl)

    async def status(self) -> Dict[str, Any]:
        async def _impl() -> Dict[str, Any]:
            self._ensure_loop_state()
            assert self._guard is not None and self._queue is not None
            async with self._guard:
                recent_jobs = [
                    dict(self._jobs[job_id])
                    for job_id in self._recent_job_ids
                    if job_id in self._jobs
                ]
                cancelling_count = sum(
                    1
                    for item in self._jobs.values()
                    if item.get("status") == "cancelling"
                )
                return {
                    "enabled": self._enabled,
                    "running": self._runner is not None and not self._runner.done(),
                    "queue_depth": self._queue.qsize(),
                    "queue_maxsize": self._queue_maxsize,
                    "active_job_id": self._active_job_id,
                    "cancelling_jobs": cancelling_count,
                    "pending_memory_jobs": len(self._pending_memory_jobs),
                    "rebuild_pending": self._rebuild_job_id is not None,
                    "sleep_pending": self._sleep_job_id is not None,
                    "stats": {
                        "enqueued": self._enqueued_total,
                        "succeeded": self._succeeded_total,
                        "failed": self._failed_total,
                        "dropped": self._dropped_total,
                        "cancelled": self._cancelled_total,
                    },
                    "last_error": self._last_error,
                    "last_finished_at": self._last_finished_at,
                    "recent_jobs": recent_jobs,
                }

        return await self._run_on_owner_loop(_impl)

    async def _run_loop(
        self,
        *,
        generation: int,
        queue: asyncio.Queue[IndexTask],
        guard: asyncio.Lock,
    ) -> None:
        while True:
            task = await queue.get()
            if generation != self._generation:
                queue.task_done()
                break
            should_skip = False
            async with guard:
                if generation != self._generation:
                    queue.task_done()
                    break
                if task.job_id in self._cancelled_job_ids:
                    self._cancelled_job_ids.discard(task.job_id)
                    should_skip = True
            if should_skip:
                queue.task_done()
                continue

            await self._mark_running(task, generation=generation, guard=guard)
            execution_task = asyncio.create_task(
                self._execute_task(task), name=f"runtime-index-job-{task.job_id}"
            )
            async with guard:
                if generation == self._generation:
                    self._active_execution_task = execution_task
            try:
                payload = await execution_task
            except asyncio.CancelledError:
                async with guard:
                    record = self._jobs.get(task.job_id) or {}
                    is_job_cancelling = record.get("status") == "cancelling"
                if is_job_cancelling:
                    await self._mark_finished(
                        task,
                        status="cancelled",
                        error="job_cancelled",
                        generation=generation,
                        guard=guard,
                    )
                    continue
                await self._mark_finished(
                    task,
                    status="failed",
                    error="worker_cancelled",
                    generation=generation,
                    guard=guard,
                )
                raise
            except Exception as exc:
                await self._mark_finished(
                    task,
                    status="failed",
                    error=str(exc),
                    generation=generation,
                    guard=guard,
                )
            else:
                await self._mark_finished(
                    task,
                    status="succeeded",
                    result=payload,
                    generation=generation,
                    guard=guard,
                )
            finally:
                async with guard:
                    if (
                        generation == self._generation
                        and self._active_execution_task is execution_task
                    ):
                        self._active_execution_task = None
                queue.task_done()

    async def _execute_task(self, task: IndexTask) -> Dict[str, Any]:
        async with self._guard:
            factory = self._client_factory
        if not callable(factory):
            raise RuntimeError("index worker is not initialized with sqlite client factory.")
        client = factory()

        if task.task_type == "reindex_memory":
            result = client.reindex_memory(
                memory_id=int(task.memory_id or 0),
                reason=task.reason,
            )
        elif task.task_type == "rebuild_index":
            result = client.rebuild_index(reason=task.reason)
        elif task.task_type == "sleep_consolidation":
            result = self._run_sleep_consolidation(client=client, reason=task.reason)
        else:
            raise ValueError(f"Unknown index task type '{task.task_type}'.")

        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, dict):
            return result
        return {"result": result}

    @staticmethod
    def _normalize_sleep_dedup_content(content: Any) -> str:
        if not isinstance(content, str):
            return ""
        return re.sub(r"\s+", " ", content).strip().lower()

    @staticmethod
    def _split_uri(uri: Any) -> Optional[tuple[str, str]]:
        if not isinstance(uri, str) or "://" not in uri:
            return None
        domain, path = uri.split("://", 1)
        domain_value = domain.strip()
        path_value = path.strip().strip("/")
        if not domain_value or not path_value:
            return None
        return domain_value, path_value

    @staticmethod
    def _parent_path(path: str) -> str:
        if "/" not in path:
            return path
        return path.rsplit("/", 1)[0]

    @staticmethod
    def _build_sleep_fragment_gist(
        *, domain: str, parent_path: str, snippets: List[str]
    ) -> str:
        lines = [f"Sleep consolidation rollup for {domain}://{parent_path}"]
        for snippet in snippets:
            text = re.sub(r"\s+", " ", snippet).strip()
            if text:
                lines.append(f"- {text[:180]}")
            if len(lines) >= 7:
                break
        return "\n".join(lines)

    @staticmethod
    def _iso_to_timestamp(value: Any) -> float:
        if not isinstance(value, str):
            return 0.0
        candidate = value.strip()
        if not candidate:
            return 0.0
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            parsed = _ensure_utc_datetime(datetime.fromisoformat(candidate))
            return float(parsed.timestamp())
        except ValueError:
            return 0.0

    async def _run_sleep_consolidation(
        self,
        *,
        client: Any,
        reason: str,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "ok": True,
            "task": "sleep_consolidation",
            "reason": reason or "sleep_cycle",
            "started_at": _utc_iso_now(),
            "degraded": False,
            "degrade_reasons": [],
        }
        dedup_apply_enabled = _env_bool("RUNTIME_SLEEP_DEDUP_APPLY", False)
        fragment_rollup_apply_enabled = _env_bool(
            "RUNTIME_SLEEP_FRAGMENT_ROLLUP_APPLY", False
        )
        payload["policy"] = {
            "dedup_apply_enabled": dedup_apply_enabled,
            "fragment_rollup_apply_enabled": fragment_rollup_apply_enabled,
        }

        orphans_summary: Dict[str, Any] = {
            "scanned": 0,
            "deprecated": 0,
            "orphaned": 0,
        }
        orphan_items: List[Dict[str, Any]] = []
        get_orphans = getattr(client, "get_all_orphan_memories", None)
        if callable(get_orphans):
            try:
                orphans_raw = get_orphans()
                if inspect.isawaitable(orphans_raw):
                    orphans_raw = await orphans_raw
                if isinstance(orphans_raw, list):
                    orphan_items = [item for item in orphans_raw if isinstance(item, dict)]
                    orphans_summary["scanned"] = len(orphan_items)
                    for item in orphan_items:
                        if not isinstance(item, dict):
                            continue
                        category = str(item.get("category") or "").strip().lower()
                        if category == "deprecated":
                            orphans_summary["deprecated"] += 1
                        elif category == "orphaned":
                            orphans_summary["orphaned"] += 1
                else:
                    payload["degraded"] = True
                    payload["degrade_reasons"].append("sleep_orphans_invalid_payload")
            except Exception:
                payload["degraded"] = True
                payload["degrade_reasons"].append("sleep_orphans_scan_failed")
        else:
            payload["degraded"] = True
            payload["degrade_reasons"].append("sleep_orphans_scan_unavailable")

        dedup_summary: Dict[str, Any] = {
            "scanned_orphans": 0,
            "duplicate_groups": 0,
            "deleted_duplicates": 0,
            "kept_memory_ids": [],
            "deleted_memory_ids": [],
            "preview_duplicates": [],
            "preview_only": not dedup_apply_enabled,
            "errors": [],
        }
        get_memory_version = getattr(client, "get_memory_version", None)
        delete_memory = getattr(client, "permanently_delete_memory", None)
        if orphan_items:
            if callable(get_memory_version):
                groups_by_hash: Dict[str, List[Dict[str, Any]]] = {}
                for item in orphan_items:
                    try:
                        memory_id = int(item.get("id") or 0)
                    except (TypeError, ValueError):
                        continue
                    if memory_id <= 0:
                        continue
                    try:
                        version_raw = get_memory_version(memory_id)
                        if inspect.isawaitable(version_raw):
                            version_raw = await version_raw
                    except Exception as exc:
                        dedup_summary["errors"].append(
                            {"memory_id": memory_id, "error": str(exc)}
                        )
                        continue
                    if not isinstance(version_raw, dict):
                        continue
                    normalized_content = self._normalize_sleep_dedup_content(
                        version_raw.get("content")
                    )
                    if not normalized_content:
                        continue
                    content_hash = hashlib.sha256(
                        normalized_content.encode("utf-8")
                    ).hexdigest()
                    groups_by_hash.setdefault(content_hash, []).append(
                        {
                            "memory_id": memory_id,
                            "deprecated": bool(version_raw.get("deprecated")),
                            "created_at_ts": self._iso_to_timestamp(
                                version_raw.get("created_at")
                            ),
                        }
                    )
                    dedup_summary["scanned_orphans"] += 1

                for content_hash in sorted(groups_by_hash.keys()):
                    records = groups_by_hash[content_hash]
                    deduped_records: List[Dict[str, Any]] = []
                    seen_ids: Set[int] = set()
                    for record in records:
                        memory_id = int(record.get("memory_id") or 0)
                        if memory_id <= 0 or memory_id in seen_ids:
                            continue
                        seen_ids.add(memory_id)
                        deduped_records.append(record)
                    if len(deduped_records) < 2:
                        continue
                    dedup_summary["duplicate_groups"] += 1
                    ordered = sorted(
                        deduped_records,
                        key=lambda record: (
                            1 if bool(record.get("deprecated")) else 0,
                            -float(record.get("created_at_ts") or 0.0),
                            int(record.get("memory_id") or 0),
                        ),
                    )
                    keep_id = int(ordered[0].get("memory_id") or 0)
                    if keep_id <= 0:
                        continue
                    dedup_summary["kept_memory_ids"].append(keep_id)
                    duplicate_ids = [
                        int(record.get("memory_id") or 0) for record in ordered[1:]
                    ]
                    duplicate_ids = [memory_id for memory_id in duplicate_ids if memory_id > 0]
                    dedup_summary["preview_duplicates"].append(
                        {"keep": keep_id, "duplicates": duplicate_ids}
                    )
                    if not dedup_apply_enabled:
                        continue
                    if not callable(delete_memory):
                        payload["degraded"] = True
                        payload["degrade_reasons"].append("sleep_orphan_dedup_unavailable")
                        continue

                    for duplicate_id in duplicate_ids:
                        try:
                            delete_raw = delete_memory(duplicate_id, require_orphan=True)
                            if inspect.isawaitable(delete_raw):
                                await delete_raw
                            dedup_summary["deleted_duplicates"] += 1
                            dedup_summary["deleted_memory_ids"].append(duplicate_id)
                        except Exception as exc:
                            dedup_summary["errors"].append(
                                {"memory_id": duplicate_id, "error": str(exc)}
                            )

                if dedup_summary["errors"]:
                    payload["degraded"] = True
                    payload["degrade_reasons"].append(
                        "sleep_orphan_dedup_partial_failure"
                    )
            else:
                if dedup_apply_enabled:
                    payload["degraded"] = True
                    payload["degrade_reasons"].append("sleep_orphan_dedup_unavailable")
                dedup_summary["errors"].append({"error": "memory_version_reader_unavailable"})

        cleanup_preview: Dict[str, Any] = {"candidate_count": 0}
        get_candidates = getattr(client, "get_vitality_cleanup_candidates", None)
        if callable(get_candidates):
            try:
                cleanup_raw = get_candidates(limit=50)
                if inspect.isawaitable(cleanup_raw):
                    cleanup_raw = await cleanup_raw
                if isinstance(cleanup_raw, dict):
                    items = cleanup_raw.get("items")
                    if isinstance(items, list):
                        cleanup_preview["candidate_count"] = len(items)
                    elif isinstance(cleanup_raw.get("count"), int):
                        cleanup_preview["candidate_count"] = int(cleanup_raw["count"])
                    else:
                        payload["degraded"] = True
                        payload["degrade_reasons"].append(
                            "sleep_cleanup_preview_invalid_payload"
                        )
                else:
                    payload["degraded"] = True
                    payload["degrade_reasons"].append(
                        "sleep_cleanup_preview_invalid_payload"
                    )
            except Exception:
                payload["degraded"] = True
                payload["degrade_reasons"].append("sleep_cleanup_preview_failed")
        else:
            payload["degraded"] = True
            payload["degrade_reasons"].append("sleep_cleanup_preview_unavailable")

        fragment_rollup: Dict[str, Any] = {
            "groups_scanned": 0,
            "preview_groups": 0,
            "groups_aggregated": 0,
            "gist_upserts": 0,
            "memory_coverage": 0,
            "skipped_existing_gist": 0,
            "preview_only": not fragment_rollup_apply_enabled,
            "errors": [],
        }
        get_recent_memories = getattr(client, "get_recent_memories", None)
        get_memory_by_id = getattr(client, "get_memory_by_id", None)
        upsert_memory_gist = getattr(client, "upsert_memory_gist", None)
        get_latest_memory_gist = getattr(client, "get_latest_memory_gist", None)
        if (
            callable(get_recent_memories)
            and callable(get_memory_by_id)
            and (
                not fragment_rollup_apply_enabled
                or callable(upsert_memory_gist)
            )
        ):
            try:
                recent_raw = get_recent_memories(limit=120)
                if inspect.isawaitable(recent_raw):
                    recent_raw = await recent_raw
                if isinstance(recent_raw, list):
                    groups: Dict[str, Dict[str, Any]] = {}
                    for item in recent_raw:
                        if not isinstance(item, dict):
                            continue
                        try:
                            memory_id = int(item.get("memory_id") or 0)
                        except (TypeError, ValueError):
                            continue
                        if memory_id <= 0:
                            continue
                        parts = self._split_uri(item.get("uri"))
                        if parts is None:
                            continue
                        domain, path = parts
                        parent_path = self._parent_path(path)
                        key = f"{domain}://{parent_path}"
                        group = groups.setdefault(
                            key,
                            {
                                "domain": domain,
                                "parent_path": parent_path,
                                "memory_ids": [],
                            },
                        )
                        if memory_id not in group["memory_ids"]:
                            group["memory_ids"].append(memory_id)

                    fragment_rollup["groups_scanned"] = len(groups)

                    for group_key in sorted(groups.keys()):
                        group = groups[group_key]
                        memory_ids = list(group.get("memory_ids") or [])
                        if len(memory_ids) < 3:
                            continue

                        snippets: List[str] = []
                        source_parts: List[str] = []
                        for memory_id in memory_ids[:6]:
                            try:
                                memory_raw = get_memory_by_id(int(memory_id))
                                if inspect.isawaitable(memory_raw):
                                    memory_raw = await memory_raw
                            except Exception as exc:
                                fragment_rollup["errors"].append(
                                    {
                                        "group": f"{group['domain']}://{group['parent_path']}",
                                        "memory_id": int(memory_id),
                                        "error": str(exc),
                                    }
                                )
                                continue
                            if not isinstance(memory_raw, dict):
                                continue
                            content = str(memory_raw.get("content") or "").strip()
                            if not content:
                                continue
                            snippet = re.sub(r"\s+", " ", content).strip()[:180]
                            snippets.append(snippet)
                            source_parts.append(f"{int(memory_id)}:{snippet}")

                        if len(snippets) < 3:
                            continue
                        fragment_rollup["preview_groups"] += 1
                        if not fragment_rollup_apply_enabled:
                            continue

                        gist_text = self._build_sleep_fragment_gist(
                            domain=str(group["domain"]),
                            parent_path=str(group["parent_path"]),
                            snippets=snippets,
                        )
                        source_hash = "sleep-rollup:" + hashlib.sha256(
                            "||".join(source_parts).encode("utf-8")
                        ).hexdigest()

                        try:
                            if callable(get_latest_memory_gist):
                                latest_raw = get_latest_memory_gist(int(memory_ids[0]))
                                if inspect.isawaitable(latest_raw):
                                    latest_raw = await latest_raw
                                if isinstance(latest_raw, dict):
                                    latest_method = str(
                                        latest_raw.get("gist_method") or ""
                                    ).strip()
                                    if (
                                        latest_method
                                        and latest_method != "sleep_fragment_rollup"
                                    ):
                                        fragment_rollup["skipped_existing_gist"] += 1
                                        continue
                            upsert_raw = upsert_memory_gist(
                                memory_id=int(memory_ids[0]),
                                gist_text=gist_text,
                                source_hash=source_hash,
                                gist_method="sleep_fragment_rollup",
                                quality_score=0.55,
                            )
                            if inspect.isawaitable(upsert_raw):
                                await upsert_raw
                            fragment_rollup["groups_aggregated"] += 1
                            fragment_rollup["gist_upserts"] += 1
                            fragment_rollup["memory_coverage"] += len(memory_ids)
                        except Exception as exc:
                            fragment_rollup["errors"].append(
                                {
                                    "group": f"{group['domain']}://{group['parent_path']}",
                                    "error": str(exc),
                                }
                            )
                else:
                    payload["degraded"] = True
                    payload["degrade_reasons"].append(
                        "sleep_fragment_rollup_invalid_payload"
                    )
            except Exception:
                payload["degraded"] = True
                payload["degrade_reasons"].append("sleep_fragment_rollup_failed")
        else:
            if fragment_rollup_apply_enabled:
                payload["degraded"] = True
                payload["degrade_reasons"].append("sleep_fragment_rollup_unavailable")
            else:
                fragment_rollup["errors"].append(
                    {"error": "recent_memory_reader_unavailable"}
                )

        if fragment_rollup["errors"]:
            payload["degraded"] = True
            payload["degrade_reasons"].append("sleep_fragment_rollup_partial_failure")

        rebuild = client.rebuild_index(reason=f"sleep_consolidation:{reason or 'sleep_cycle'}")
        if inspect.isawaitable(rebuild):
            rebuild = await rebuild

        payload["orphans"] = orphans_summary
        payload["dedup"] = dedup_summary
        payload["cleanup_preview"] = cleanup_preview
        payload["fragment_rollup"] = fragment_rollup
        payload["rebuild_result"] = rebuild
        payload["finished_at"] = _utc_iso_now()
        payload["degrade_reasons"] = list(dict.fromkeys(payload["degrade_reasons"]))
        return payload

    async def _mark_running(
        self,
        task: IndexTask,
        *,
        generation: int,
        guard: asyncio.Lock,
    ) -> None:
        async with guard:
            if generation != self._generation:
                return
            record = self._jobs.get(task.job_id)
            if record is None:
                return
            record["status"] = "running"
            record["started_at"] = _utc_iso_now()
            self._active_job_id = task.job_id

    async def _mark_finished(
        self,
        task: IndexTask,
        *,
        status: str,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        generation: int,
        guard: asyncio.Lock,
    ) -> None:
        finished_at = _utc_iso_now()
        async with guard:
            if generation != self._generation:
                return
            record = self._jobs.get(task.job_id)
            if record is None:
                return
            existing_status = str(record.get("status") or "")
            if existing_status in self._FINAL_STATES and existing_status != status:
                return
            record["status"] = status
            record["finished_at"] = finished_at
            if result is not None:
                record["result"] = result
            if error:
                record["error"] = error
                if status == "failed":
                    self._last_error = error
            if task.memory_id is not None:
                self._pending_memory_jobs.pop(task.memory_id, None)
            if task.task_type == "rebuild_index" and self._rebuild_job_id == task.job_id:
                self._rebuild_job_id = None
            if (
                task.task_type == "sleep_consolidation"
                and self._sleep_job_id == task.job_id
            ):
                self._sleep_job_id = None
            if status == "succeeded":
                self._succeeded_total += 1
            elif status == "failed":
                self._failed_total += 1
            elif status == "dropped":
                self._dropped_total += 1
            elif status == "cancelled":
                self._cancelled_total += 1
            self._last_finished_at = finished_at
            if self._active_job_id == task.job_id:
                self._active_job_id = None

            event = self._job_events.get(task.job_id)
            if event is not None:
                event.set()
            self._append_recent_job_locked(task.job_id)

    def _append_recent_job_locked(self, job_id: str) -> None:
        if job_id in self._recent_job_ids:
            self._recent_job_ids.remove(job_id)
        self._recent_job_ids.appendleft(job_id)
        while len(self._recent_job_ids) > self._recent_limit:
            stale_id = self._recent_job_ids.pop()
            if stale_id in self._jobs:
                stale_status = self._jobs[stale_id].get("status")
                if stale_status in self._FINAL_STATES:
                    self._jobs.pop(stale_id, None)
                    self._job_events.pop(stale_id, None)


class SleepTimeConsolidator:
    """Periodic scheduler for low-risk sleep-time consolidation jobs."""

    _QUEUE_FULL_RETRY_SECONDS = 30.0

    def __init__(self) -> None:
        self._enabled = _env_bool("RUNTIME_SLEEP_CONSOLIDATION_ENABLED", True)
        self._check_interval_seconds = _env_int(
            "RUNTIME_SLEEP_CONSOLIDATION_INTERVAL_SECONDS", 1800, minimum=60
        )
        self._last_check_ts = 0.0
        self._last_result: Dict[str, Any] = {
            "scheduled": False,
            "reason": "not_started",
            "retry_after_seconds": self._check_interval_seconds,
        }
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._guard: Optional[asyncio.Lock] = None

    def _ensure_loop_state(self) -> None:
        current_loop = asyncio.get_running_loop()
        if self._loop is current_loop and self._guard is not None:
            return
        self._loop = current_loop
        self._guard = asyncio.Lock()

    def _resolve_retry_interval_seconds_locked(self) -> float:
        retry_after = self._last_result.get("retry_after_seconds")
        if isinstance(retry_after, (int, float)):
            retry_after_float = float(retry_after)
            if retry_after_float > 0:
                return min(retry_after_float, float(self._check_interval_seconds))
        return float(self._check_interval_seconds)

    async def schedule(
        self,
        *,
        index_worker: IndexTaskWorker,
        force: bool = False,
        reason: str = "runtime",
    ) -> Dict[str, Any]:
        self._ensure_loop_state()
        assert self._guard is not None
        requested_at = _utc_iso_now()
        async with self._guard:
            now_ts = time.time()
            if not self._enabled:
                self._last_result = {
                    "scheduled": False,
                    "reason": "sleep_consolidation_disabled",
                    "check_interval_seconds": self._check_interval_seconds,
                    "retry_after_seconds": self._check_interval_seconds,
                }
                self._last_check_ts = now_ts
                return dict(self._last_result)

            retry_interval_seconds = self._resolve_retry_interval_seconds_locked()
            if (
                not force
                and self._last_check_ts > 0
                and (now_ts - self._last_check_ts) < retry_interval_seconds
            ):
                return dict(self._last_result)

            self._last_result = {
                "scheduled": False,
                "reason": reason or "runtime",
                "forced": bool(force),
                "check_interval_seconds": self._check_interval_seconds,
                "retry_after_seconds": retry_interval_seconds,
                "enqueue_reason": "pending",
                "requested_at": requested_at,
            }
            self._last_check_ts = now_ts

        payload = await index_worker.enqueue_sleep_consolidation(
            reason=reason or "runtime"
        )
        async with self._guard:
            scheduled = bool(payload.get("queued")) or bool(payload.get("deduped"))
            enqueue_reason = str(payload.get("reason") or "")
            retry_after_seconds = float(self._check_interval_seconds)
            if not scheduled and enqueue_reason == "queue_full":
                retry_after_seconds = min(
                    retry_after_seconds, self._QUEUE_FULL_RETRY_SECONDS
                )
            self._last_result = {
                "scheduled": scheduled,
                "reason": reason or "runtime",
                "forced": bool(force),
                "check_interval_seconds": self._check_interval_seconds,
                "retry_after_seconds": retry_after_seconds,
                "enqueue_reason": enqueue_reason,
                "requested_at": requested_at,
                **payload,
            }
            return dict(self._last_result)

    async def status(self) -> Dict[str, Any]:
        self._ensure_loop_state()
        assert self._guard is not None
        async with self._guard:
            return {
                "enabled": self._enabled,
                "check_interval_seconds": self._check_interval_seconds,
                **dict(self._last_result),
            }


class RuntimeState:
    def __init__(self) -> None:
        self.write_lanes = WriteLaneCoordinator()
        self.session_cache = SessionSearchCache()
        self.flush_tracker = SessionFlushTracker()
        self.promotion_tracker = SessionPromotionTracker()
        self.guard_tracker = GuardDecisionTracker()
        self.import_learn_tracker = ImportLearnAuditTracker()
        self.cleanup_reviews = CleanupReviewCoordinator()
        self.vitality_decay = VitalityDecayCoordinator()
        self.index_worker = IndexTaskWorker()
        self.sleep_consolidation = SleepTimeConsolidator()

    async def ensure_started(self, client_factory: Callable[[], Any]) -> None:
        await self.index_worker.ensure_started(client_factory)
        await self.vitality_decay.run_decay(
            client_factory=client_factory,
            force=False,
            reason="runtime.ensure_started",
        )
        await self.sleep_consolidation.schedule(
            index_worker=self.index_worker,
            force=False,
            reason="runtime.ensure_started",
        )

    async def shutdown(self) -> None:
        await self.index_worker.shutdown()


runtime_state = RuntimeState()
