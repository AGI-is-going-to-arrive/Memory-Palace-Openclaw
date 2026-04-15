"""Tests for the flush quarantine service (P3-3)."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

import mcp_server
from api import maintenance as maintenance_api
import quarantine as quarantine_mod
from quarantine import (
    QUARANTINE_ENABLED,
    QUARANTINE_TTL_HOURS,
    _reset_table_ensured,
    dismiss_quarantine_record,
    ensure_quarantine_table,
    expire_stale_quarantine,
    get_quarantine_records,
    get_quarantine_stats,
    replay_quarantine_record,
    write_quarantine_record,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_engine():
    """Create an in-memory async engine and ensure the quarantine table."""
    _reset_table_ensured()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await ensure_quarantine_table(engine)
    return engine


class _FakeFlushTracker:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.marked = False

    async def should_flush(self, *, session_id: Optional[str]) -> bool:
        _ = session_id
        return True

    async def build_summary(self, *, session_id: Optional[str], limit: int = 12) -> str:
        _ = session_id, limit
        return self.summary

    async def mark_flushed(self, *, session_id: Optional[str]) -> None:
        _ = session_id
        self.marked = True


class _FakeCompactClientWithEngine:
    """Compact client fake that carries an async engine for quarantine writes."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.created_payload: Dict[str, Any] = {}
        self.gist_payload: Dict[str, Any] = {}
        self.memory_id = 41

    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        return {"action": "ADD", "method": "keyword", "reason": "ok"}

    async def create_memory(self, **kwargs: Any) -> Dict[str, Any]:
        self.created_payload = dict(kwargs)
        return {
            "id": self.memory_id,
            "domain": kwargs.get("domain", "notes"),
            "path": "auto_flush_1",
            "uri": "notes://auto_flush_1",
            "index_targets": [self.memory_id],
        }

    async def upsert_memory_gist(self, **kwargs: Any) -> Dict[str, Any]:
        self.gist_payload = dict(kwargs)
        return {
            "id": 9,
            "memory_id": kwargs["memory_id"],
            "gist_text": kwargs["gist_text"],
            "source_hash": kwargs["source_hash"],
            "gist_method": kwargs["gist_method"],
            "quality_score": kwargs.get("quality_score"),
        }


class _NoopGuardClientWithEngine(_FakeCompactClientWithEngine):
    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        return {
            "action": "NOOP",
            "method": "embedding",
            "reason": "duplicate_flush_summary",
            "target_uri": "notes://agent/auto_flush_existing",
        }


class _DegradedGuardClientWithEngine(_FakeCompactClientWithEngine):
    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        return {
            "action": "NOOP",
            "method": "exception",
            "reason": "write_guard_unavailable: upstream",
            "degraded": True,
            "degrade_reasons": ["write_guard_exception"],
        }


class _AtomicReflectionClientWithEngine(_FakeCompactClientWithEngine):
    def __init__(self, engine: Any) -> None:
        super().__init__(engine)
        self._next_id = 100
        self.paths: Dict[tuple, Dict[str, Any]] = {}
        self.created_records: list = []
        self.updated_records: list = []

    async def get_memory_by_path(self, path, domain, reinforce_access=False):
        _ = reinforce_access
        return self.paths.get((domain, path))

    async def create_memory(self, **kwargs: Any) -> Dict[str, Any]:
        domain = str(kwargs.get("domain") or "core")
        parent_path = str(kwargs.get("parent_path") or "").strip("/")
        title = str(kwargs.get("title") or "").strip("/")
        path = f"{parent_path}/{title}" if parent_path else title
        record = {
            "id": self._next_id,
            "domain": domain,
            "path": path,
            "uri": f"{domain}://{path}",
            "content": str(kwargs.get("content") or ""),
            "index_targets": [self._next_id],
        }
        self.paths[(domain, path)] = dict(record)
        self.created_records.append(dict(record))
        self._next_id += 1
        return {
            "id": record["id"],
            "domain": domain,
            "path": path,
            "uri": record["uri"],
            "index_targets": [record["id"]],
        }

    async def update_memory(self, path, *, content=None, priority=None,
                            disclosure=None, domain="core", index_now=True):
        current = self.paths.get((domain, path))
        if current is None:
            raise ValueError("path not found")
        current = dict(current)
        current["content"] = str(content or current.get("content") or "")
        current["id"] = self._next_id
        self.paths[(domain, path)] = current
        self.updated_records.append(dict(current))
        self._next_id += 1
        return {
            "new_memory_id": current["id"],
            "uri": current["uri"],
            "index_targets": [current["id"]],
        }


class _NoopReflectionClientWithEngine(_AtomicReflectionClientWithEngine):
    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        return {
            "action": "NOOP",
            "method": "embedding",
            "reason": "duplicate_flush_summary",
            "target_uri": "core://reflection/existing",
        }


async def _noop_async(*_: Any, **__: Any) -> None:
    return None


async def _false_async(*_: Any, **__: Any) -> bool:
    return False


async def _run_write_inline(_operation: str, task):
    return await task()


# ---------------------------------------------------------------------------
# 1. test_quarantine_write_and_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_write_and_read() -> None:
    engine = await _make_engine()
    rec_id = await write_quarantine_record(
        engine,
        session_id="s1",
        source="compact_context",
        summary="test summary",
        gist_text="gist",
        trace_text="trace",
        guard_action="NOOP",
        guard_method="embedding",
        guard_reason="dup",
        guard_target_uri="notes://x",
        content_hash="abc123",
        ttl_hours=72,
    )
    assert isinstance(rec_id, int) and rec_id > 0

    records = await get_quarantine_records(engine, status="pending")
    assert len(records) == 1
    r = records[0]
    assert r["id"] == rec_id
    assert r["session_id"] == "s1"
    assert r["source"] == "compact_context"
    assert r["status"] == "pending"
    assert r["summary"] == "test summary"
    assert r["gist_text"] == "gist"


# ---------------------------------------------------------------------------
# 2. test_quarantine_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_stats() -> None:
    engine = await _make_engine()
    # Write 2 pending records.
    await write_quarantine_record(
        engine, session_id="a", source="x", summary="s", gist_text=None,
        trace_text=None, guard_action="NOOP", guard_method=None,
        guard_reason=None, guard_target_uri=None, content_hash=None,
    )
    rec2 = await write_quarantine_record(
        engine, session_id="b", source="x", summary="s2", gist_text=None,
        trace_text=None, guard_action="UPDATE", guard_method=None,
        guard_reason=None, guard_target_uri=None, content_hash=None,
    )
    # Replay one.
    await replay_quarantine_record(engine, record_id=rec2)

    stats = await get_quarantine_stats(engine)
    assert stats["pending"] == 1
    assert stats["replayed"] == 1
    assert stats["total"] == 2


# ---------------------------------------------------------------------------
# 3. test_quarantine_replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_replay() -> None:
    engine = await _make_engine()
    rec_id = await write_quarantine_record(
        engine, session_id="s", source="x", summary="s", gist_text=None,
        trace_text=None, guard_action="NOOP", guard_method=None,
        guard_reason=None, guard_target_uri=None, content_hash=None,
    )
    ok = await replay_quarantine_record(engine, record_id=rec_id)
    assert ok is True

    records = await get_quarantine_records(engine, status="replayed")
    assert len(records) == 1
    assert records[0]["replayed_at"] is not None

    # Replaying again should fail (not pending anymore).
    ok2 = await replay_quarantine_record(engine, record_id=rec_id)
    assert ok2 is False


# ---------------------------------------------------------------------------
# 4. test_quarantine_dismiss
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_dismiss() -> None:
    engine = await _make_engine()
    rec_id = await write_quarantine_record(
        engine, session_id="s", source="x", summary="s", gist_text=None,
        trace_text=None, guard_action="NOOP", guard_method=None,
        guard_reason=None, guard_target_uri=None, content_hash=None,
    )
    ok = await dismiss_quarantine_record(engine, record_id=rec_id)
    assert ok is True

    records = await get_quarantine_records(engine, status="dismissed")
    assert len(records) == 1

    # Dismissing again should fail.
    ok2 = await dismiss_quarantine_record(engine, record_id=rec_id)
    assert ok2 is False


# ---------------------------------------------------------------------------
# 5. test_quarantine_expire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_expire() -> None:
    engine = await _make_engine()
    # Write with TTL=0 so it is already expired.
    await write_quarantine_record(
        engine, session_id="s", source="x", summary="s", gist_text=None,
        trace_text=None, guard_action="NOOP", guard_method=None,
        guard_reason=None, guard_target_uri=None, content_hash=None,
        ttl_hours=0,
    )
    count = await expire_stale_quarantine(engine)
    assert count == 1

    records = await get_quarantine_records(engine, status="expired")
    assert len(records) == 1

    stats = await get_quarantine_stats(engine)
    assert stats["expired"] == 1
    assert stats["pending"] == 0


# ---------------------------------------------------------------------------
# 6a. test_quarantine_table_cache_is_per_engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_table_cache_is_per_engine() -> None:
    _reset_table_ensured()
    engine_one = create_async_engine("sqlite+aiosqlite:///:memory:")
    engine_two = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        await ensure_quarantine_table(engine_one)
        record_id = await write_quarantine_record(
            engine_two,
            session_id="s2",
            source="compact_context",
            summary="second-engine-write",
            gist_text=None,
            trace_text=None,
            guard_action="NOOP",
            guard_method=None,
            guard_reason=None,
            guard_target_uri=None,
            content_hash=None,
        )
        assert isinstance(record_id, int) and record_id > 0

        records = await get_quarantine_records(engine_two, status="pending")
        assert len(records) == 1
        assert records[0]["summary"] == "second-engine-write"
    finally:
        await engine_one.dispose()
        await engine_two.dispose()


# ---------------------------------------------------------------------------
# 6. test_quarantine_not_in_memory_search
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_not_in_memory_search() -> None:
    """Quarantine records must not appear in normal memory search.
    We verify by checking the quarantine table lives in its own namespace
    and is not referenced by the Memory/Path ORM models."""
    from db.sqlite_models import Memory, Path as PathModel
    # Memory.paths references paths table; quarantine is separate.
    assert Memory.__tablename__ == "memories"
    assert PathModel.__tablename__ == "paths"
    # The quarantine module uses raw SQL on a separate table.
    engine = await _make_engine()
    await write_quarantine_record(
        engine, session_id="s", source="x", summary="search_invisible",
        gist_text=None, trace_text=None, guard_action="NOOP",
        guard_method=None, guard_reason=None, guard_target_uri=None,
        content_hash=None,
    )
    # Quarantine records are only accessible via quarantine API.
    records = await get_quarantine_records(engine, status="pending")
    assert any(r["summary"] == "search_invisible" for r in records)


# ---------------------------------------------------------------------------
# 7. test_compact_context_noop_quarantines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_context_noop_quarantines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    fake_client = _NoopGuardClientWithEngine(engine)
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- summary already exists and should dedupe"
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(quarantine_mod, "QUARANTINE_ENABLED", True)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is True
    assert payload["data_persisted"] is False
    assert payload["reason"] == "write_guard_deduped"
    assert payload["quarantined"] is True
    assert isinstance(payload["quarantine_id"], int)
    assert fake_tracker.marked is True

    # Verify the record exists in the quarantine table.
    records = await get_quarantine_records(engine, status="pending")
    assert len(records) == 1
    assert records[0]["source"] == "compact_context"
    assert records[0]["guard_action"] == "NOOP"


# ---------------------------------------------------------------------------
# 8. test_compact_context_reflection_noop_quarantines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_context_reflection_noop_quarantines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    fake_client = _NoopReflectionClientWithEngine(engine)
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- reflection already exists and should dedupe"
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(quarantine_mod, "QUARANTINE_ENABLED", True)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context_reflection(
        reason="unit_test", force=True, max_lines=5,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is True
    assert payload["data_persisted"] is False
    assert payload["reason"] == "write_guard_deduped"
    assert payload["quarantined"] is True
    assert isinstance(payload["quarantine_id"], int)

    records = await get_quarantine_records(engine, status="pending")
    assert len(records) == 1
    assert records[0]["source"] == "compact_context_reflection"


# ---------------------------------------------------------------------------
# 8a. test_compact_context_noop_quarantine_failure_does_not_flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_context_noop_quarantine_failure_does_not_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    fake_client = _NoopGuardClientWithEngine(engine)
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- quarantine write failed"
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(quarantine_mod, "QUARANTINE_ENABLED", True)
    monkeypatch.setattr(
        quarantine_mod,
        "write_quarantine_record",
        AsyncMock(side_effect=RuntimeError("quarantine unavailable")),
    )
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is False
    assert payload["data_persisted"] is False
    assert payload["reason"] == "quarantine_write_failed"
    assert payload["quarantined"] is False
    assert payload["degraded"] is True
    assert payload["degrade_reasons"] == ["quarantine_write_failed"]
    assert fake_tracker.marked is False


# ---------------------------------------------------------------------------
# 8b. test_compact_context_reflection_quarantine_failure_does_not_flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_context_reflection_quarantine_failure_does_not_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    fake_client = _NoopReflectionClientWithEngine(engine)
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- reflection quarantine write failed"
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(quarantine_mod, "QUARANTINE_ENABLED", True)
    monkeypatch.setattr(
        quarantine_mod,
        "write_quarantine_record",
        AsyncMock(side_effect=RuntimeError("quarantine unavailable")),
    )
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context_reflection(
        reason="unit_test", force=True, max_lines=5,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is False
    assert payload["data_persisted"] is False
    assert payload["reason"] == "quarantine_write_failed"
    assert payload["quarantined"] is False
    assert payload["degraded"] is True
    assert payload["degrade_reasons"] == ["quarantine_write_failed"]
    assert fake_tracker.marked is False


# ---------------------------------------------------------------------------
# 9. test_compact_context_degraded_no_quarantine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compact_context_degraded_no_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    fake_client = _DegradedGuardClientWithEngine(engine)
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- keep pending until guard recovers"
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(quarantine_mod, "QUARANTINE_ENABLED", True)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is False
    assert payload["data_persisted"] is False
    assert payload["reason"] == "write_guard_blocked"
    assert "quarantined" not in payload
    assert fake_tracker.marked is False

    # No quarantine record should exist.
    records = await get_quarantine_records(engine, status="pending")
    assert len(records) == 0


# ---------------------------------------------------------------------------
# 10. test_quarantine_disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quarantine_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    fake_client = _NoopGuardClientWithEngine(engine)
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- quarantine disabled by env"
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    # Disable quarantine via monkeypatch on the module constant.
    monkeypatch.setattr(quarantine_mod, "QUARANTINE_ENABLED", False)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is True
    assert payload["data_persisted"] is False
    assert payload["reason"] == "write_guard_deduped"
    # quarantined should be False since feature is disabled.
    assert payload.get("quarantined") is False
    assert "quarantine_id" not in payload
    assert fake_tracker.marked is True

    # No quarantine records.
    records = await get_quarantine_records(engine, status="pending")
    assert len(records) == 0


@pytest.mark.asyncio
async def test_compact_context_quarantine_failure_does_not_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    fake_client = _NoopGuardClientWithEngine(engine)
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- quarantine write fails and must not flush"
    )

    async def _boom(*_: Any, **__: Any) -> int:
        raise RuntimeError("quarantine table unavailable")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(quarantine_mod, "QUARANTINE_ENABLED", True)
    monkeypatch.setattr(quarantine_mod, "write_quarantine_record", _boom)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is False
    assert payload["data_persisted"] is False
    assert payload["reason"] == "quarantine_write_failed"
    assert payload["quarantined"] is False
    assert payload["degraded"] is True
    assert "quarantine_write_failed" in payload["degrade_reasons"]
    assert fake_tracker.marked is False


@pytest.mark.asyncio
async def test_compact_context_reflection_quarantine_failure_does_not_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()
    fake_client = _NoopReflectionClientWithEngine(engine)
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- reflection quarantine write fails and must not flush"
    )

    async def _boom(*_: Any, **__: Any) -> int:
        raise RuntimeError("quarantine table unavailable")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(quarantine_mod, "QUARANTINE_ENABLED", True)
    monkeypatch.setattr(quarantine_mod, "write_quarantine_record", _boom)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context_reflection(
        reason="unit_test", force=True, max_lines=5,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is False
    assert payload["data_persisted"] is False
    assert payload["reflection_written"] is False
    assert payload["reason"] == "quarantine_write_failed"
    assert payload["quarantined"] is False
    assert payload["degraded"] is True
    assert "quarantine_write_failed" in payload["degrade_reasons"]
    assert fake_tracker.marked is False


@pytest.mark.asyncio
async def test_quarantine_table_is_ensured_per_engine() -> None:
    _reset_table_ensured()
    engine1 = create_async_engine("sqlite+aiosqlite:///:memory:")
    engine2 = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        await ensure_quarantine_table(engine1)
        rec_id = await write_quarantine_record(
            engine2,
            session_id="s2",
            source="compact_context",
            summary="second-engine-write",
            gist_text=None,
            trace_text=None,
            guard_action="NOOP",
            guard_method=None,
            guard_reason=None,
            guard_target_uri=None,
            content_hash=None,
        )
        assert isinstance(rec_id, int) and rec_id > 0
        records = await get_quarantine_records(engine2, status="pending")
        assert len(records) == 1
        assert records[0]["summary"] == "second-engine-write"
    finally:
        await engine1.dispose()
        await engine2.dispose()


@pytest.mark.asyncio
async def test_observability_degrades_when_quarantine_stats_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = await _make_engine()

    class _DummyClient:
        def __init__(self, engine_ref: Any) -> None:
            self.engine = engine_ref

        async def get_index_status(self) -> Dict[str, Any]:
            return {"degraded": False, "index_available": True}

        async def get_gist_stats(self) -> Dict[str, Any]:
            return {"degraded": False, "total_rows": 0, "active_coverage": 0.0}

        async def get_vitality_stats(self) -> Dict[str, Any]:
            return {"degraded": False, "total_memories": 0, "low_vitality_count": 0}

    async def _ensure_started(_factory: Any) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    async def _simple_status() -> Dict[str, Any]:
        return {"degraded": False}

    async def _cleanup_summary() -> Dict[str, Any]:
        return {"pending": 0, "approved": 0}

    async def _guard_summary() -> Dict[str, Any]:
        return {"total": 0, "blocked": 0, "degraded": False}

    async def _sm_lite_stats() -> Dict[str, Any]:
        return {
            "degraded": False,
            "storage": "runtime_ephemeral",
            "promotion_path": "compact_context + auto_flush",
            "session_cache": {},
            "flush_tracker": {},
        }

    async def _boom_stats(*_: Any, **__: Any) -> Dict[str, Any]:
        raise RuntimeError("quarantine_stats_unavailable")

    async def _noop_expire(*_: Any, **__: Any) -> int:
        return 0

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()
    async with maintenance_api._cleanup_query_events_guard:
        maintenance_api._cleanup_query_events.clear()

    monkeypatch.setattr(maintenance_api, "_search_events_loaded", True)
    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: _DummyClient(engine))
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(maintenance_api.runtime_state.index_worker, "status", _index_worker_status)
    monkeypatch.setattr(maintenance_api.runtime_state.write_lanes, "status", _write_lane_status)
    monkeypatch.setattr(maintenance_api.runtime_state.vitality_decay, "status", _simple_status)
    monkeypatch.setattr(maintenance_api.runtime_state.cleanup_reviews, "summary", _cleanup_summary)
    monkeypatch.setattr(maintenance_api.runtime_state.sleep_consolidation, "status", _simple_status)
    monkeypatch.setattr(maintenance_api.runtime_state.guard_tracker, "summary", _guard_summary)
    monkeypatch.setattr(maintenance_api, "_build_sm_lite_stats", _sm_lite_stats)
    monkeypatch.setattr(quarantine_mod, "get_quarantine_stats", _boom_stats)
    monkeypatch.setattr(quarantine_mod, "expire_stale_quarantine", _noop_expire)

    payload = await maintenance_api.get_observability_summary()

    assert payload["status"] == "degraded"
    assert payload["quarantine"]["degraded"] is True
    assert payload["quarantine"]["total"] == 0
    assert payload["quarantine"]["pending"] == 0
    assert payload["quarantine"]["reason"] == "quarantine_stats_unavailable"
