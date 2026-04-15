from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from runtime_state import SessionFlushTracker, SessionSearchCache, _tokenize_query


def test_tokenize_query_keeps_cjk_terms_and_bigrams() -> None:
    tokens = _tokenize_query("数据库锁 workflow")
    assert "workflow" in tokens
    assert "数据库锁" in tokens
    assert "数据" in tokens
    assert "据库" in tokens
    assert "库锁" in tokens


def test_tokenize_query_supports_korean_japanese_single_cjk_and_accents() -> None:
    korean_tokens = _tokenize_query("서울은 아름다운 도시입니다")
    japanese_tokens = _tokenize_query("こんにちは世界")
    single_cjk_tokens = _tokenize_query("大")
    accented_tokens = _tokenize_query("café résumé")

    assert "서울은" in korean_tokens
    assert "도시입니다" in korean_tokens
    assert "こんにちは世界" in japanese_tokens
    assert "こん" in japanese_tokens
    assert "ちは" in japanese_tokens
    assert "世界" in japanese_tokens
    assert single_cjk_tokens == ["大"]
    assert accented_tokens == ["café", "résumé"]


def test_tokenize_query_preserves_cjk_tokens_when_latin_query_is_long() -> None:
    long_query = ("word " * 40) + ("词语" * 8)
    tokens = _tokenize_query(long_query)

    assert "word" in tokens
    assert "词语词语词语词语词语词语词语词语" in tokens
    assert "词语" in tokens


@pytest.mark.asyncio
async def test_session_search_cache_matches_cjk_queries() -> None:
    cache = SessionSearchCache()
    await cache.record_hit(
        session_id="demo",
        uri="core://agents/main/profile/workflow",
        memory_id=1,
        snippet="昨天索引失败，原因是数据库锁，需要重试。",
    )

    results = await cache.search(session_id="demo", query="数据库锁", limit=5)

    assert len(results) == 1
    assert results[0]["uri"] == "core://agents/main/profile/workflow"
    assert results[0]["match_type"] == "session_queue"


@pytest.mark.asyncio
async def test_session_search_cache_normalizes_unicode_equivalent_queries() -> None:
    cache = SessionSearchCache()
    await cache.record_hit(
        session_id="demo",
        uri="core://agents/main/profile/cafe",
        memory_id=2,
        snippet="café résumé workflow",
    )

    results = await cache.search(session_id="demo", query="cafe\u0301", limit=5)

    assert len(results) == 1
    assert results[0]["uri"] == "core://agents/main/profile/cafe"


@pytest.mark.asyncio
async def test_session_search_cache_prunes_expired_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_SESSION_CACHE_HALF_LIFE_SECONDS", "60")
    cache = SessionSearchCache()
    expired_at = (
        datetime.now(timezone.utc) - timedelta(seconds=301)
    ).isoformat().replace("+00:00", "Z")
    fresh_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    await cache.record_hit(
        session_id="expired",
        uri="core://expired",
        memory_id=1,
        snippet="old cache entry",
        updated_at=expired_at,
    )
    await cache.record_hit(
        session_id="fresh",
        uri="core://fresh",
        memory_id=2,
        snippet="recent cache entry",
        updated_at=fresh_at,
    )

    expired_results = await cache.search(session_id="expired", query="old", limit=5)
    summary = await cache.summary()

    assert expired_results == []
    assert summary["session_count"] == 1
    assert summary["total_hits"] == 1
    assert summary["expiry_seconds"] == 300.0
    assert "expired" not in cache._hits
    assert "fresh" in cache._hits


@pytest.mark.asyncio
async def test_session_search_cache_accepts_naive_iso_timestamps() -> None:
    cache = SessionSearchCache()
    naive_updated_at = datetime.now().replace(microsecond=0).isoformat()

    await cache.record_hit(
        session_id="naive",
        uri="core://naive",
        memory_id=3,
        snippet="naive timestamp cache entry",
        updated_at=naive_updated_at,
    )

    results = await cache.search(session_id="naive", query="naive", limit=5)
    summary = await cache.summary()

    assert len(results) == 1
    assert results[0]["uri"] == "core://naive"
    assert summary["session_count"] == 1
    assert summary["total_hits"] == 1


@pytest.mark.asyncio
async def test_session_search_cache_caps_tracked_sessions_with_lru_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_SESSION_CACHE_MAX_SESSIONS", "16")
    cache = SessionSearchCache()

    for index in range(16):
        await cache.record_hit(
            session_id=f"session-{index}",
            uri=f"core://session/{index}",
            memory_id=index,
            snippet=f"snippet-{index}",
        )

    refreshed = await cache.search(session_id="session-0", query="snippet-0", limit=1)

    await cache.record_hit(
        session_id="session-16",
        uri="core://session/16",
        memory_id=16,
        snippet="snippet-16",
    )
    summary = await cache.summary()

    assert len(refreshed) == 1
    assert summary["session_count"] == 16
    assert summary["max_sessions"] == 16
    assert "session-1" not in cache._hits
    assert "session-0" in cache._hits
    assert "session-16" in cache._hits


@pytest.mark.asyncio
async def test_session_flush_tracker_caps_tracked_sessions_with_lru_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_MAX_SESSIONS", "16")
    tracker = SessionFlushTracker()

    for index in range(16):
        await tracker.record_event(
            session_id=f"session-{index}",
            message=f"event-{index}",
        )

    await tracker.record_event(session_id="session-0", message="refresh-oldest")
    await tracker.record_event(session_id="session-16", message="new-session")

    summary = await tracker.summary()

    assert summary["session_count"] == 16
    assert summary["max_sessions"] == 16
    assert "session-1" not in tracker._events
    assert "session-0" in tracker._events
    assert "session-16" in tracker._events


@pytest.mark.asyncio
async def test_session_flush_tracker_lists_pending_sessions() -> None:
    tracker = SessionFlushTracker()

    await tracker.record_event(session_id="alpha", message="event-a")
    await tracker.record_event(session_id="beta", message="event-b")
    await tracker.mark_flushed(session_id="alpha")

    pending = await tracker.pending_session_ids()

    assert pending == ["beta"]
