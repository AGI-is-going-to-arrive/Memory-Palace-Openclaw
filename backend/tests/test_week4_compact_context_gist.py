import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

import mcp_server
import quarantine as quarantine_mod
from api import browse as browse_api
from api import maintenance as maintenance_api
from db.sqlite_client import Memory, MemoryGist, SQLiteClient
from runtime_state import SessionFlushTracker


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


class _FakeFlushTracker:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.marked = False

    async def should_flush(self, *, session_id: Optional[str]) -> bool:
        _ = session_id
        return True

    async def build_summary(self, *, session_id: Optional[str], limit: int = 12) -> str:
        _ = session_id
        _ = limit
        return self.summary

    async def mark_flushed(self, *, session_id: Optional[str]) -> None:
        _ = session_id
        self.marked = True


class _FakeCompactClient:
    def __init__(self) -> None:
        self.created_payload: Dict[str, Any] = {}
        self.gist_payload: Dict[str, Any] = {}
        self.memory_id = 41
        self.write_guard_calls = 0

    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        self.write_guard_calls += 1
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


class _AtomicReflectionClient(_FakeCompactClient):
    def __init__(self) -> None:
        super().__init__()
        self._next_id = 100
        self.paths: Dict[tuple[str, str], Dict[str, Any]] = {}
        self.created_records: list[Dict[str, Any]] = []
        self.updated_records: list[Dict[str, Any]] = []

    async def get_memory_by_path(
        self,
        path: str,
        domain: str,
        reinforce_access: bool = False,
    ) -> Optional[Dict[str, Any]]:
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

    async def update_memory(
        self,
        path: str,
        *,
        content: Optional[str] = None,
        priority: Optional[int] = None,
        disclosure: Optional[str] = None,
        domain: str = "core",
        index_now: bool = True,
    ) -> Dict[str, Any]:
        _ = priority, disclosure, index_now
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


class _LLMGistClient:
    def __init__(
        self,
        *,
        payload: Optional[Dict[str, Any]] = None,
        error: Optional[Exception] = None,
        degrade_reason: Optional[str] = None,
    ) -> None:
        self.payload = payload
        self.error = error
        self.degrade_reason = degrade_reason

    async def generate_compact_gist(
        self,
        *,
        summary: str,
        max_points: int = 3,
        max_chars: int = 280,
        degrade_reasons: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        _ = summary
        _ = max_points
        _ = max_chars
        if self.error is not None:
            raise self.error
        if self.degrade_reason and isinstance(degrade_reasons, list):
            degrade_reasons.append(self.degrade_reason)
        if self.payload is None:
            return None
        return dict(self.payload)


async def _noop_async(*_: Any, **__: Any) -> None:
    return None


async def _false_async(*_: Any, **__: Any) -> bool:
    return False


async def _run_write_inline(_operation: str, task):
    return await task()


@pytest.mark.asyncio
async def test_generate_gist_prefers_extractive_bullets() -> None:
    payload = await mcp_server.generate_gist(
        "Session compaction notes:\n- rebuilt index after timeout\n- retried with fallback mode\n- marked incident resolved"
    )

    assert payload["gist_method"] == "extractive_bullets"
    assert payload["gist_text"]
    assert payload["quality"] > 0.0


@pytest.mark.asyncio
async def test_generate_gist_prefers_llm_when_available() -> None:
    payload = await mcp_server.generate_gist(
        "Session compaction notes:\n- user requested incident summary",
        client=_LLMGistClient(
            payload={
                "gist_text": "Incident summary prepared with owner and ETA.",
                "gist_method": "llm_gist",
                "quality": 0.91,
            }
        ),
    )

    assert payload["gist_method"] == "llm_gist"
    assert payload["gist_text"] == "Incident summary prepared with owner and ETA."
    assert payload["quality"] == pytest.approx(0.91)


@pytest.mark.asyncio
async def test_compact_context_returns_gist_fields_and_persists_gist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeCompactClient()
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- user asked for rollback checklist\n- system generated runbook and owner map"
    )

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is True
    assert payload["data_persisted"] is True
    assert payload["gist_method"] == "extractive_bullets"
    assert payload["gist_text"]
    assert isinstance(payload["quality"], float)
    assert len(payload["source_hash"]) == 64
    assert payload["trace_text"] == fake_tracker.summary
    assert payload["gist_persisted"] is True
    assert fake_tracker.marked is True
    assert fake_client.gist_payload["memory_id"] == fake_client.memory_id
    assert fake_client.gist_payload["source_hash"] == payload["source_hash"]
    assert "## Gist" in fake_client.created_payload["content"]
    assert "## Trace" in fake_client.created_payload["content"]


@pytest.mark.asyncio
async def test_compact_context_source_hash_is_deterministic_for_identical_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run_once() -> Dict[str, Any]:
        fake_client = _FakeCompactClient()
        fake_tracker = _FakeFlushTracker(
            "Session compaction notes:\n"
            "- user requested rollback checklist\n"
            "- system generated runbook and owner map"
        )
        monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
        monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
        monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
        monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
        monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
        mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()
        return json.loads(await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5))

    first = await _run_once()
    second = await _run_once()

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["trace_text"] == second["trace_text"]
    assert first["source_hash"] == second["source_hash"]
    assert first["gist_method"] == second["gist_method"]


@pytest.mark.asyncio
async def test_compact_context_preserves_multiline_trace_text_verbatim_in_persisted_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    multiline_summary = (
        "Session compaction notes:\n"
        "- [meta] summary_version: v2-progressive\n"
        "\n"
        "## Older Events (rolling summary)\n"
        "* workflow changed | reason: provider switched\n"
        "\n"
        "## Recent Events\n"
        "- user requested fallback validation\n"
        "- system recorded the final checkpoint"
    )
    fake_client = _FakeCompactClient()
    fake_tracker = _FakeFlushTracker(multiline_summary)

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    payload = json.loads(await mcp_server.compact_context(reason="unit_test", force=True, max_lines=8))

    assert payload["ok"] is True
    assert payload["trace_text"] == multiline_summary
    assert "## Trace\n" in fake_client.created_payload["content"]
    assert multiline_summary in fake_client.created_payload["content"]
    assert "workflow changed | reason: provider switched" in fake_client.created_payload["content"]


@pytest.mark.asyncio
async def test_compact_context_real_flush_tracker_rollup_chain_still_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = SessionFlushTracker()
    tracker._max_events = 1
    tracker._rolling_summary_max_chars = 400

    await tracker.record_event(
        session_id="default",
        message=(
            "workflow changed for reflection lane\n"
            "reason: provider switched to local fallback\n"
            "uri: core://reflection/agent-alpha/session-1"
        ),
    )
    await tracker.record_event(
        session_id="default",
        message="recent tail event for compact context",
    )

    fake_client = _FakeCompactClient()
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "get_session_id", lambda: "default")
    monkeypatch.setattr(mcp_server, "get_runtime_session_id", lambda: "default")
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    payload = json.loads(
        await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    )

    assert payload["ok"] is True
    assert payload["flushed"] is True
    assert payload["data_persisted"] is True
    assert "## Older Events (rolling summary)" in payload["trace_text"]
    assert "## Recent Events" in payload["trace_text"]
    assert (
        "* workflow changed for reflection lane | reason: provider switched to local fallback"
        in payload["trace_text"]
    )
    assert payload["gist_text"]
    assert fake_client.write_guard_calls == 1
    assert "## Gist" in fake_client.created_payload["content"]
    assert "## Trace" in fake_client.created_payload["content"]


@pytest.mark.asyncio
async def test_compact_context_high_value_early_flush_still_calls_write_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED", "true")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS", "2")
    monkeypatch.setenv("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS", "200")
    tracker = SessionFlushTracker()
    tracker._min_events = 6
    tracker._trigger_chars = 6000

    await tracker.record_event(
        session_id="default",
        message="remember preferred workflow for recall " + ("x" * 120),
    )
    await tracker.record_event(
        session_id="default",
        message="routine checkpoint captured " + ("y" * 120),
    )

    fake_client = _FakeCompactClient()
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "get_session_id", lambda: "default")
    monkeypatch.setattr(mcp_server, "get_runtime_session_id", lambda: "default")
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    payload = json.loads(
        await mcp_server.compact_context(reason="unit_test", force=False, max_lines=5)
    )

    assert payload["ok"] is True
    assert payload["flushed"] is True
    assert payload["data_persisted"] is True
    assert fake_client.write_guard_calls == 1
    assert tracker._events.get("default") is None


@pytest.mark.asyncio
async def test_compact_context_falls_back_with_degrade_reasons_when_llm_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingCompactClient(_FakeCompactClient):
        async def generate_compact_gist(
            self,
            *,
            summary: str,
            max_points: int = 3,
            max_chars: int = 280,
            degrade_reasons: Optional[List[str]] = None,
        ) -> Optional[Dict[str, Any]]:
            _ = summary
            _ = max_points
            _ = max_chars
            _ = degrade_reasons
            raise RuntimeError("upstream timeout")

    fake_client = _FailingCompactClient()
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- user asked for fallback summary\n- system returned deterministic bullets"
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["gist_method"] == "extractive_bullets"
    assert "degrade_reasons" in payload
    assert "compact_gist_llm_exception:RuntimeError" in payload["degrade_reasons"]


@pytest.mark.asyncio
async def test_compact_context_write_guard_exception_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _GuardFailClient(_FakeCompactClient):
        async def write_guard(self, **_: Any) -> Dict[str, Any]:
            raise RuntimeError("guard unavailable")

    fake_client = _GuardFailClient()
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- keep pending until guard recovers"
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is False
    assert payload["data_persisted"] is False
    assert payload["reason"] == "write_guard_blocked"
    assert payload["guard_action"] == "NOOP"
    assert payload["guard_method"] == "exception"
    assert "gist_text" not in payload
    assert "trace_text" not in payload
    assert fake_client.created_payload == {}
    assert fake_tracker.marked is False


@pytest.mark.asyncio
async def test_compact_context_write_guard_noop_marks_pending_flushed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _GuardNoopClient(_FakeCompactClient):
        async def write_guard(self, **_: Any) -> Dict[str, Any]:
            return {
                "action": "NOOP",
                "method": "embedding",
                "reason": "duplicate_flush_summary",
                "target_uri": "notes://agent/auto_flush_existing",
            }

    fake_client = _GuardNoopClient()
    fake_client.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- summary already exists and should dedupe"
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is True
    assert payload["data_persisted"] is False
    assert payload["reason"] == "write_guard_deduped"
    assert payload["guard_action"] == "NOOP"
    assert payload["uri"] == "notes://agent/auto_flush_existing"
    assert "gist_text" not in payload
    assert "trace_text" not in payload
    assert fake_client.created_payload == {}
    assert fake_tracker.marked is True


@pytest.mark.asyncio
async def test_compact_context_reflection_commits_directly_without_intermediate_flush_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _AtomicReflectionClient()
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- user asked for rollback checklist\n- system generated runbook and owner map"
    )

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context_reflection(
        reason="reflection_lane",
        force=True,
        max_lines=5,
        reflection_root_uri="core://reflection",
        reflection_agent_key="agent-alpha",
        reflection_session_ref="session-1",
        reflection_agent_id="agent-alpha",
        reflection_session_id="session-1",
        reflection_priority=2,
        reflection_disclosure="When recalling cross-session lessons, invariants, or open loops.",
        reflection_decay_hint_days=14,
        reflection_retention_class="rolling_session",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is True
    assert payload["data_persisted"] is True
    assert payload["reflection_written"] is True
    assert payload["committed_directly_to_reflection"] is True
    assert payload["uri"].startswith("core://reflection/agent-alpha/")
    assert payload["trace_text"] == fake_tracker.summary
    assert fake_tracker.marked is True
    assert all(
        not record["path"].startswith("auto_flush_") for record in fake_client.created_records
    )
    reflection_record = next(
        record
        for record in fake_client.created_records
        if "# Reflection Lane" in record["content"]
    )
    assert "# Reflection Lane" in reflection_record["content"]
    assert "- compact_source_hash: " in reflection_record["content"]
    assert "- compact_gist_method: " in reflection_record["content"]


@pytest.mark.asyncio
async def test_compact_context_reflection_does_not_leave_intermediate_flush_on_reflection_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingAtomicReflectionClient(_AtomicReflectionClient):
        async def create_memory(self, **kwargs: Any) -> Dict[str, Any]:
            parent_path = str(kwargs.get("parent_path") or "")
            if parent_path.startswith("reflection/agent-alpha/"):
                raise RuntimeError("reflection create failed")
            return await super().create_memory(**kwargs)

    fake_client = _FailingAtomicReflectionClient()
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- keep pending until reflection write succeeds"
    )

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context_reflection(
        reason="reflection_lane",
        force=True,
        max_lines=5,
        reflection_root_uri="core://reflection",
        reflection_agent_key="agent-alpha",
        reflection_session_ref="session-1",
        reflection_agent_id="agent-alpha",
        reflection_session_id="session-1",
        reflection_priority=2,
        reflection_disclosure="When recalling cross-session lessons, invariants, or open loops.",
        reflection_decay_hint_days=14,
        reflection_retention_class="rolling_session",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "reflection create failed" in payload["error"]
    assert fake_tracker.marked is False
    assert all(
        not record["path"].startswith("auto_flush_") for record in fake_client.created_records
    )


@pytest.mark.asyncio
async def test_compact_context_reflection_treats_guard_event_recording_failures_as_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _AtomicReflectionClient()
    fake_tracker = _FakeFlushTracker(
        "Session compaction notes:\n- keep going even if guard tracker persistence fails"
    )

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server.runtime_state, "flush_tracker", fake_tracker)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    async def _boom(**kwargs: Any) -> None:
        _ = kwargs
        raise RuntimeError("guard tracker failed")
    monkeypatch.setattr(mcp_server, "_record_guard_event", _boom)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context_reflection(
        reason="reflection_lane",
        force=True,
        max_lines=5,
        reflection_root_uri="core://reflection",
        reflection_agent_key="agent-alpha",
        reflection_session_ref="session-1",
        reflection_agent_id="agent-alpha",
        reflection_session_id="session-1",
        reflection_priority=2,
        reflection_disclosure="When recalling cross-session lessons, invariants, or open loops.",
        reflection_decay_hint_days=14,
        reflection_retention_class="rolling_session",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["reflection_written"] is True
    assert payload["uri"].startswith("core://reflection/agent-alpha/")


@pytest.mark.asyncio
async def test_generate_compact_gist_uses_llm_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPACT_GIST_LLM_ENABLED", "true")
    monkeypatch.setenv("COMPACT_GIST_LLM_API_BASE", "http://fake.llm")
    monkeypatch.setenv("COMPACT_GIST_LLM_MODEL", "fake-model")
    monkeypatch.delenv("WRITE_GUARD_LLM_ENABLED", raising=False)

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")

    async def _fake_post_json(
        base: str,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str = "",
        timeout_sec: Optional[float] = None,
        error_sink: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _ = api_key
        assert base == "http://fake.llm"
        assert endpoint == "/chat/completions"
        assert payload["model"] == "fake-model"
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"gist_text": "Semantic gist from llm", "quality": 0.87}
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    degrade_reasons: List[str] = []
    payload = await client.generate_compact_gist(
        summary="Session summary content",
        degrade_reasons=degrade_reasons,
    )
    await client.close()

    assert payload is not None
    assert payload["gist_method"] == "llm_gist"
    assert payload["gist_text"] == "Semantic gist from llm"
    assert payload["quality"] == pytest.approx(0.87)
    assert degrade_reasons == []


@pytest.mark.asyncio
async def test_generate_compact_gist_records_degrade_reason_on_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPACT_GIST_LLM_ENABLED", "true")
    monkeypatch.setenv("COMPACT_GIST_LLM_API_BASE", "http://fake.llm")
    monkeypatch.setenv("COMPACT_GIST_LLM_MODEL", "fake-model")

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")

    async def _fake_post_json(
        base: str,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str = "",
        timeout_sec: Optional[float] = None,
        error_sink: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        _ = base
        _ = endpoint
        _ = payload
        _ = api_key
        return {"choices": [{"message": {"content": "not-json"}}]}

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    degrade_reasons: List[str] = []
    payload = await client.generate_compact_gist(
        summary="Session summary content",
        degrade_reasons=degrade_reasons,
    )
    await client.close()

    assert payload is None
    assert "compact_gist_llm_response_invalid" in degrade_reasons


@pytest.mark.asyncio
async def test_generate_compact_gist_supports_legacy_llm_env_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COMPACT_GIST_LLM_ENABLED", "true")
    monkeypatch.delenv("COMPACT_GIST_LLM_API_BASE", raising=False)
    monkeypatch.delenv("COMPACT_GIST_LLM_API_KEY", raising=False)
    monkeypatch.delenv("COMPACT_GIST_LLM_MODEL", raising=False)
    monkeypatch.delenv("WRITE_GUARD_LLM_API_BASE", raising=False)
    monkeypatch.delenv("WRITE_GUARD_LLM_API_KEY", raising=False)
    monkeypatch.delenv("WRITE_GUARD_LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_RESPONSES_URL", "http://127.0.0.1:8317/v1/responses")
    monkeypatch.setenv("LLM_API_KEY", "sk-12345678")
    monkeypatch.setenv("LLM_MODEL_NAME", "gpt-5.2")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "none")

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")

    async def _fake_post_json(
        base: str,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str = "",
        timeout_sec: Optional[float] = None,
        error_sink: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        assert base == "http://127.0.0.1:8317/v1"
        assert endpoint == "/chat/completions"
        assert payload["model"] == "gpt-5.2"
        assert "reasoning" not in payload
        assert api_key == "sk-12345678"
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"gist_text": "Semantic gist from legacy alias", "quality": 0.9}
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    degrade_reasons: List[str] = []
    payload = await client.generate_compact_gist(
        summary="Session summary content",
        degrade_reasons=degrade_reasons,
    )
    await client.close()

    assert payload is not None
    assert payload["gist_method"] == "llm_gist"
    assert payload["gist_text"] == "Semantic gist from legacy alias"
    assert payload["quality"] == pytest.approx(0.9)
    assert degrade_reasons == []


@pytest.mark.asyncio
async def test_upsert_memory_gist_updates_same_source_hash(tmp_path: Path) -> None:
    db_path = tmp_path / "week4-gist.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    created = await client.create_memory(
        parent_path="",
        content="first memory payload",
        priority=1,
        title="week4_note",
        domain="core",
    )
    first = await client.upsert_memory_gist(
        memory_id=created["id"],
        gist_text="initial gist",
        source_hash="source_hash_v1",
        gist_method="extractive_bullets",
        quality_score=0.71,
    )
    second = await client.upsert_memory_gist(
        memory_id=created["id"],
        gist_text="updated gist",
        source_hash="source_hash_v1",
        gist_method="sentence_fallback",
        quality_score=0.66,
    )
    latest = await client.get_latest_memory_gist(created["id"])

    await client.close()

    assert second["id"] == first["id"]
    assert latest is not None
    assert latest["gist_text"] == "updated gist"
    assert latest["source_hash"] == "source_hash_v1"
    assert latest["gist_method"] == "sentence_fallback"


@pytest.mark.asyncio
async def test_upsert_memory_gist_uses_latest_created_at_across_hashes(tmp_path: Path) -> None:
    db_path = tmp_path / "week4-gist-latest.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    created = await client.create_memory(
        parent_path="",
        content="latest gist source memory",
        priority=1,
        title="latest_note",
        domain="core",
    )
    await client.upsert_memory_gist(
        memory_id=created["id"],
        gist_text="gist A first",
        source_hash="hash_a",
        gist_method="extractive_bullets",
        quality_score=0.81,
    )
    await asyncio.sleep(0.01)
    await client.upsert_memory_gist(
        memory_id=created["id"],
        gist_text="gist B",
        source_hash="hash_b",
        gist_method="truncate_fallback",
        quality_score=0.45,
    )
    await asyncio.sleep(0.01)
    await client.upsert_memory_gist(
        memory_id=created["id"],
        gist_text="gist A refreshed",
        source_hash="hash_a",
        gist_method="sentence_fallback",
        quality_score=0.63,
    )

    latest = await client.get_latest_memory_gist(created["id"])
    await client.close()

    assert latest is not None
    assert latest["source_hash"] == "hash_a"
    assert latest["gist_text"] == "gist A refreshed"


@pytest.mark.asyncio
async def test_upsert_memory_gist_concurrent_same_key_keeps_single_row(tmp_path: Path) -> None:
    db_path = tmp_path / "week4-gist-concurrency.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    created = await client.create_memory(
        parent_path="",
        content="concurrency source memory",
        priority=1,
        title="concurrency_note",
        domain="core",
    )

    async def _write_gist(i: int) -> None:
        await client.upsert_memory_gist(
            memory_id=created["id"],
            gist_text=f"gist from writer {i}",
            source_hash="same_hash",
            gist_method="extractive_bullets",
            quality_score=0.7,
        )

    await asyncio.gather(*[_write_gist(i) for i in range(8)])

    async with client.session() as session:
        count = int(
            (
                await session.execute(
                    select(func.count(MemoryGist.id))
                    .where(MemoryGist.memory_id == created["id"])
                    .where(MemoryGist.source_content_hash == "same_hash")
                )
            ).scalar()
            or 0
        )

    await client.close()
    assert count == 1


@pytest.mark.asyncio
async def test_upsert_memory_gist_works_on_in_memory_database() -> None:
    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    await client.init_db()

    created = await client.create_memory(
        parent_path="",
        content="in-memory gist source",
        priority=1,
        title="memory_note",
        domain="core",
    )
    await client.upsert_memory_gist(
        memory_id=created["id"],
        gist_text="in-memory gist",
        source_hash="in_memory_hash",
        gist_method="extractive_bullets",
        quality_score=0.55,
    )
    latest = await client.get_latest_memory_gist(created["id"])
    await client.close()

    assert latest is not None
    assert latest["source_hash"] == "in_memory_hash"
    assert latest["gist_text"] == "in-memory gist"


@pytest.mark.asyncio
async def test_get_gist_stats_uses_active_memory_coverage(tmp_path: Path) -> None:
    db_path = tmp_path / "week4-gist-stats.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    active = await client.create_memory(
        parent_path="",
        content="active memory",
        priority=1,
        title="active_note",
        domain="core",
    )
    deprecated = await client.create_memory(
        parent_path="",
        content="deprecated memory",
        priority=2,
        title="deprecated_note",
        domain="core",
    )

    async with client.session() as session:
        deprecated_memory = await session.get(Memory, deprecated["id"])
        assert deprecated_memory is not None
        deprecated_memory.deprecated = True

    await client.upsert_memory_gist(
        memory_id=active["id"],
        gist_text="active gist",
        source_hash="hash_active",
        gist_method="extractive_bullets",
        quality_score=0.9,
    )
    await client.upsert_memory_gist(
        memory_id=deprecated["id"],
        gist_text="deprecated gist",
        source_hash="hash_deprecated",
        gist_method="extractive_bullets",
        quality_score=0.8,
    )

    stats = await client.get_gist_stats()
    await client.close()

    assert stats["total_distinct_memory_count"] == 2
    assert stats["distinct_memory_count"] == 1
    assert stats["active_memory_count"] == 1
    assert stats["coverage_ratio"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_get_memory_and_children_include_gist_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "week4-gist-read.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    parent = await client.create_memory(
        parent_path="",
        content="parent body",
        priority=1,
        title="parent",
        domain="core",
    )
    child = await client.create_memory(
        parent_path="parent",
        content="child body content",
        priority=2,
        title="child",
        domain="core",
    )
    await client.upsert_memory_gist(
        memory_id=parent["id"],
        gist_text="parent gist",
        source_hash="hash_parent",
        gist_method="extractive_bullets",
        quality_score=0.88,
    )
    await client.upsert_memory_gist(
        memory_id=child["id"],
        gist_text="child gist",
        source_hash="hash_child",
        gist_method="sentence_fallback",
        quality_score=0.66,
    )

    parent_memory = await client.get_memory_by_path("parent", domain="core")
    children = await client.get_children(parent["id"])

    await client.close()

    assert parent_memory is not None
    assert parent_memory["gist_text"] == "parent gist"
    assert parent_memory["gist_method"] == "extractive_bullets"
    assert parent_memory["gist_source_hash"] == "hash_parent"
    assert len(children) == 1
    assert children[0]["path"] == "parent/child"
    assert children[0]["gist_text"] == "child gist"
    assert children[0]["gist_method"] == "sentence_fallback"
    assert children[0]["gist_source_hash"] == "hash_child"


@pytest.mark.asyncio
async def test_browse_get_node_returns_gist_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "week4-browse-gist.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    parent = await client.create_memory(
        parent_path="",
        content="browse parent",
        priority=1,
        title="browse_parent",
        domain="core",
    )
    child = await client.create_memory(
        parent_path="browse_parent",
        content="browse child",
        priority=1,
        title="child",
        domain="core",
    )
    await client.upsert_memory_gist(
        memory_id=parent["id"],
        gist_text="browse parent gist",
        source_hash="browse_parent_hash",
        gist_method="extractive_bullets",
        quality_score=0.77,
    )
    await client.upsert_memory_gist(
        memory_id=child["id"],
        gist_text="browse child gist",
        source_hash="browse_child_hash",
        gist_method="truncate_fallback",
        quality_score=0.51,
    )

    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: client)
    payload = await browse_api.get_node(path="browse_parent", domain="core")

    await client.close()

    assert payload["node"]["gist_text"] == "browse parent gist"
    assert payload["node"]["source_hash"] == "browse_parent_hash"
    assert payload["children"][0]["gist_text"] == "browse child gist"
    assert payload["children"][0]["source_hash"] == "browse_child_hash"


@pytest.mark.asyncio
async def test_observability_summary_includes_gist_stats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _DummyClient:
        def __init__(self, engine: Any) -> None:
            self.engine = engine

        async def get_index_status(self) -> Dict[str, Any]:
            return {"degraded": False, "index_available": True}

        async def get_gist_stats(self) -> Dict[str, Any]:
            return {
                "total_rows": 4,
                "distinct_memory_count": 3,
                "active_memory_count": 10,
                "coverage_ratio": 0.3,
                "quality_coverage_ratio": 1.0,
                "avg_quality_score": 0.71,
                "method_breakdown": {"extractive_bullets": 3, "truncate_fallback": 1},
                "latest_created_at": "2026-02-17T00:00:00Z",
            }

        async def get_vitality_stats(self) -> Dict[str, Any]:
            return {
                "degraded": False,
                "total_paths": 0,
                "low_vitality_paths": 0,
                "deprecation_candidates": 0,
                "total_memories": 0,
            }

    async def _ensure_started(_factory) -> None:
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

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()

    client = SQLiteClient(_sqlite_url(tmp_path / "observability_gist_stats.db"))
    await client.init_db()
    monkeypatch.setattr(maintenance_api, "_search_events_loaded", True)
    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: _DummyClient(client.engine))
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(maintenance_api.runtime_state.index_worker, "status", _index_worker_status)
    monkeypatch.setattr(maintenance_api.runtime_state.write_lanes, "status", _write_lane_status)
    monkeypatch.setenv(
        maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV,
        str(tmp_path / "missing-transport-diagnostics.json"),
    )

    try:
        payload = await maintenance_api.get_observability_summary()
    finally:
        await client.close()

    assert payload["status"] == "ok"
    assert payload["gist_stats"]["degraded"] is False
    assert payload["gist_stats"]["total_rows"] == 4
    assert payload["gist_stats"]["method_breakdown"]["extractive_bullets"] == 3


@pytest.mark.asyncio
async def test_observability_status_degrades_when_gist_stats_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _DummyClient:
        def __init__(self, engine: Any) -> None:
            self.engine = engine

        async def get_index_status(self) -> Dict[str, Any]:
            return {"degraded": False, "index_available": True}

    async def _ensure_started(_factory) -> None:
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

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()

    client = SQLiteClient(_sqlite_url(tmp_path / "observability_gist_unavailable.db"))
    await client.init_db()
    monkeypatch.setattr(maintenance_api, "_search_events_loaded", True)
    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: _DummyClient(client.engine))
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(maintenance_api.runtime_state.index_worker, "status", _index_worker_status)
    monkeypatch.setattr(maintenance_api.runtime_state.write_lanes, "status", _write_lane_status)

    try:
        payload = await maintenance_api.get_observability_summary()
    finally:
        await client.close()

    assert payload["status"] == "degraded"
    assert payload["gist_stats"]["degraded"] is True
    assert payload["gist_stats"]["reason"] == "gist_stats_unavailable"


@pytest.mark.asyncio
async def test_observability_status_degrades_when_quarantine_stats_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _DummyClient:
        def __init__(self, engine: Any) -> None:
            self.engine = engine

        async def get_index_status(self) -> Dict[str, Any]:
            return {"degraded": False, "index_available": True}

        async def get_gist_stats(self) -> Dict[str, Any]:
            return {
                "degraded": False,
                "total_rows": 0,
                "distinct_memory_count": 0,
                "active_memory_count": 0,
                "coverage_ratio": 0.0,
                "quality_coverage_ratio": 0.0,
                "avg_quality_score": 0.0,
                "method_breakdown": {},
                "latest_created_at": None,
            }

        async def get_vitality_stats(self) -> Dict[str, Any]:
            return {
                "degraded": False,
                "total_paths": 0,
                "low_vitality_paths": 0,
                "deprecation_candidates": 0,
                "total_memories": 0,
            }

    async def _ensure_started(_factory) -> None:
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

    client = SQLiteClient(_sqlite_url(tmp_path / "observability_quarantine.db"))
    await client.init_db()
    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()

    async def _raise_quarantine(*_args: Any, **_kwargs: Any) -> int:
        raise RuntimeError("quarantine stats unavailable")

    monkeypatch.setattr(maintenance_api, "_search_events_loaded", True)
    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: _DummyClient(client.engine))
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(maintenance_api.runtime_state.index_worker, "status", _index_worker_status)
    monkeypatch.setattr(maintenance_api.runtime_state.write_lanes, "status", _write_lane_status)
    monkeypatch.setattr(
        maintenance_api,
        "_load_transport_observability",
        lambda: {"status": "ok", "degraded": False, "instances": [], "last_report": None},
    )
    monkeypatch.setattr(quarantine_mod, "expire_stale_quarantine", _raise_quarantine)
    monkeypatch.setenv(
        maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV,
        str(tmp_path / "missing-transport-diagnostics.json"),
    )

    try:
        payload = await maintenance_api.get_observability_summary()
    finally:
        await client.close()

    assert payload["status"] == "degraded"
    assert payload["quarantine"]["total"] == 0
    assert payload["quarantine"]["pending"] == 0
    assert payload["quarantine"]["replayed"] == 0
    assert payload["quarantine"]["expired"] == 0
    assert payload["quarantine"]["dismissed"] == 0
    assert payload["quarantine"]["degraded"] is True
    assert payload["quarantine"]["reason"] == "quarantine stats unavailable"
