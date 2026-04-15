import asyncio
import json
import logging
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException

import mcp_server
from api import browse as browse_api
from api import maintenance as maintenance_api
from db.sqlite_client import SQLiteClient
from runtime_state import GuardDecisionTracker


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


def _force_meta_line(payload: Dict[str, Any]) -> str:
    return f"MP_FORCE_META={json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"


def _force_control_block(*lines: str) -> list[str]:
    return [
        "",
        "<!-- MEMORY_PALACE_FORCE_CONTROL_V1 -->",
        *[line for line in lines if line],
        "<!-- /MEMORY_PALACE_FORCE_CONTROL_V1 -->",
    ]


class _FakeClient:
    def __init__(
        self,
        *,
        guard_decision: Dict[str, Any],
        memory: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.guard_decision = guard_decision
        self.memory = memory or {
            "id": 7,
            "content": "hello world",
            "priority": 1,
            "disclosure": None,
        }
        self.create_called = False
        self.update_called = False
        self.update_payload: Dict[str, Any] = {}
        self.remove_called = False
        self.alias_payload: Dict[str, Any] = {}

    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        return dict(self.guard_decision)

    async def create_memory(self, **_: Any) -> Dict[str, Any]:
        self.create_called = True
        return {
            "id": 11,
            "path": "agent/new_note",
            "uri": "core://agent/new_note",
            "index_targets": [11],
        }

    async def get_memory_by_path(
        self,
        path: str,
        domain: str = "core",
        reinforce_access: bool = True,
    ) -> Optional[Dict[str, Any]]:
        _ = path
        _ = domain
        _ = reinforce_access
        return dict(self.memory)

    async def get_memory_by_id(self, memory_id: int) -> Dict[str, Any]:
        payload = dict(self.memory)
        payload["id"] = int(memory_id)
        payload["paths"] = [{"domain": "core", "path": "agent/current"}]
        return payload

    async def update_memory(self, **kwargs: Any) -> Dict[str, Any]:
        self.update_called = True
        self.update_payload = dict(kwargs)
        return {
            "uri": f"{kwargs.get('domain', 'core')}://{kwargs.get('path', '')}",
            "new_memory_id": 19,
            "index_targets": [19],
        }

    async def remove_path(self, _path: str, _domain: str = "core") -> bool:
        self.remove_called = True
        return True

    async def add_path(self, **kwargs: Any) -> Dict[str, Any]:
        self.alias_payload = dict(kwargs)
        new_domain = kwargs.get("new_domain", "core")
        new_path = kwargs.get("new_path", "")
        target_domain = kwargs.get("target_domain", "core")
        target_path = kwargs.get("target_path", "")
        return {
            "new_uri": f"{new_domain}://{new_path}",
            "target_uri": f"{target_domain}://{target_path}",
            "memory_id": 7,
        }


class _GuardErrorClient(_FakeClient):
    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        raise RuntimeError("guard_down")


async def _noop_async(*_: Any, **__: Any) -> None:
    return None


async def _raise_follow_up_error(*_: Any, **__: Any) -> None:
    raise RuntimeError("follow_up_down")


async def _false_async(*_: Any, **__: Any) -> bool:
    return False


async def _empty_list_async(*_: Any, **__: Any) -> list[Any]:
    return []


async def _run_write_inline(_operation: str, task):
    return await task()


def _patch_mcp_dependencies(monkeypatch: pytest.MonkeyPatch, fake_client: _FakeClient) -> None:
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_guard_event", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server, "_maybe_auto_flush", _noop_async)
    monkeypatch.setattr(mcp_server, "_snapshot_path_create", _noop_async)
    monkeypatch.setattr(mcp_server, "_snapshot_memory_content", _noop_async)
    monkeypatch.setattr(mcp_server, "_snapshot_path_meta", _noop_async)
    monkeypatch.setattr(mcp_server, "_should_defer_index_on_write", _false_async)
    monkeypatch.setattr(mcp_server, "_enqueue_index_targets", _empty_list_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)


class _ConcurrentCreateGuardClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__(guard_decision={"action": "ADD", "reason": "allow", "method": "keyword"})
        self.guard_calls = 0
        self.create_calls = 0
        self.first_guard_started = asyncio.Event()
        self.allow_first_guard_to_finish = asyncio.Event()
        self.first_create_finished = asyncio.Event()
        self.second_guard_started_before_first_create_finished = False

    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        self.guard_calls += 1
        if self.guard_calls == 1:
            self.first_guard_started.set()
            await self.allow_first_guard_to_finish.wait()
        elif not self.first_create_finished.is_set():
            self.second_guard_started_before_first_create_finished = True
        return dict(self.guard_decision)

    async def create_memory(self, **kwargs: Any) -> Dict[str, Any]:
        self.create_calls += 1
        if self.create_calls == 1:
            self.first_create_finished.set()
        suffix = self.create_calls
        return {
            "id": 10 + suffix,
            "path": f"agent/new_note_{suffix}",
            "uri": f"core://agent/new_note_{suffix}",
            "index_targets": [10 + suffix],
        }


class _ConcurrentUpdateGuardClient(_FakeClient):
    def __init__(self) -> None:
        super().__init__(
            guard_decision={
                "action": "UPDATE",
                "reason": "allow_same_memory",
                "method": "keyword",
                "target_id": 7,
                "target_uri": "core://agent/current",
            }
        )
        self.guard_calls = 0
        self.update_calls = 0
        self.first_guard_started = asyncio.Event()
        self.allow_first_guard_to_finish = asyncio.Event()
        self.first_update_finished = asyncio.Event()
        self.second_guard_started_before_first_update_finished = False

    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        self.guard_calls += 1
        if self.guard_calls == 1:
            self.first_guard_started.set()
            await self.allow_first_guard_to_finish.wait()
        elif not self.first_update_finished.is_set():
            self.second_guard_started_before_first_update_finished = True
        return dict(self.guard_decision)

    async def update_memory(self, **kwargs: Any) -> Dict[str, Any]:
        self.update_calls += 1
        if self.update_calls == 1:
            self.first_update_finished.set()
        return await super().update_memory(**kwargs)


@pytest.mark.asyncio
async def test_write_guard_identical_content_hits_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "guard-identical.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()
    created = await client.create_memory(
        parent_path="",
        content="alpha beta gamma",
        priority=1,
        title="note_a",
        domain="core",
    )
    decision = await client.write_guard(content="alpha beta gamma", domain="core")
    await client.close()

    assert decision["action"] == "NOOP"
    assert decision["target_id"] == created["id"]
    assert decision["method"] in {"embedding", "keyword"}


@pytest.mark.asyncio
async def test_create_memory_runs_write_guard_inside_write_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _ConcurrentCreateGuardClient()
    _patch_mcp_dependencies(monkeypatch, fake_client)
    lane_lock = asyncio.Lock()

    async def _run_serialized_write(_operation: str, task):
        async with lane_lock:
            return await task()

    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_serialized_write)

    first_task = asyncio.create_task(
        mcp_server.create_memory("core://agent", "first payload", priority=1, title="first")
    )
    await fake_client.first_guard_started.wait()
    second_task = asyncio.create_task(
        mcp_server.create_memory("core://agent", "second payload", priority=1, title="second")
    )
    fake_client.allow_first_guard_to_finish.set()
    first_raw, second_raw = await asyncio.gather(first_task, second_task)
    first_payload = json.loads(first_raw)
    second_payload = json.loads(second_raw)

    assert first_payload["ok"] is True
    assert second_payload["ok"] is True
    assert fake_client.second_guard_started_before_first_create_finished is False


@pytest.mark.asyncio
async def test_create_memory_accepts_unicode_title_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={"action": "ADD", "reason": "allow", "method": "keyword"}
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        "core://agent",
        "unicode payload",
        priority=1,
        title="日本語メモ",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_records_session_hit_without_force_control_trailer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={"action": "ADD", "reason": "allow", "method": "keyword"}
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)
    recorded_calls: list[dict[str, Any]] = []

    async def _record_session_hit(**kwargs: Any) -> None:
        recorded_calls.append(kwargs)

    monkeypatch.setattr(mcp_server, "_record_session_hit", _record_session_hit)

    raw = await mcp_server.create_memory(
        "core://agent",
        "\n".join(
            [
                "stable workflow payload",
                *_force_control_block(
                    _force_meta_line({"force": True, "reason": "unit_test"})
                ),
            ]
        ),
        priority=1,
        title="clean_note",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert recorded_calls == [
        {
            "uri": "core://agent/new_note",
            "memory_id": 11,
            "snippet": "stable workflow payload",
            "priority": 1,
            "source": "create_memory",
        }
    ]


@pytest.mark.asyncio
async def test_create_memory_internal_errors_do_not_leak_secret_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingCreateClient(_FakeClient):
        async def create_memory(self, **_: Any) -> Dict[str, Any]:
            raise RuntimeError("Authorization: Bearer super-secret")

    fake_client = _ExplodingCreateClient(
        guard_decision={"action": "ADD", "reason": "allow", "method": "keyword"}
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        "core://agent",
        "payload with secret error",
        priority=1,
        title="new_note",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert "super-secret" not in payload["message"]
    assert "create_memory failed" in payload["message"]


@pytest.mark.asyncio
async def test_update_memory_snapshots_execute_inside_write_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "allow_same_memory",
            "method": "keyword",
            "target_id": 7,
            "target_uri": "core://agent/current",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)
    events: list[tuple[str, bool]] = []
    lane_state = {"inside": False}

    async def _snapshot_memory_content(_uri: str) -> bool:
        events.append(("content", lane_state["inside"]))
        return True

    async def _snapshot_path_meta(_uri: str) -> bool:
        events.append(("meta", lane_state["inside"]))
        return True

    async def _run_serialized_write(_operation: str, task):
        lane_state["inside"] = True
        try:
            return await task()
        finally:
            lane_state["inside"] = False

    monkeypatch.setattr(mcp_server, "_snapshot_memory_content", _snapshot_memory_content)
    monkeypatch.setattr(mcp_server, "_snapshot_path_meta", _snapshot_path_meta)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_serialized_write)

    content_raw = await mcp_server.update_memory(
        "core://agent/current",
        append="\nupdated",
    )
    content_payload = json.loads(content_raw)

    meta_raw = await mcp_server.update_memory(
        "core://agent/current",
        priority=3,
    )
    meta_payload = json.loads(meta_raw)

    assert content_payload["ok"] is True
    assert meta_payload["ok"] is True
    assert events == [
        ("content", True),
        ("meta", True),
    ]


@pytest.mark.asyncio
async def test_create_memory_logs_follow_up_side_effect_failures_without_failing_write(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_client = _FakeClient(
        guard_decision={"action": "ADD", "reason": "allow", "method": "keyword"}
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _raise_follow_up_error)

    with caplog.at_level(logging.WARNING, logger="mcp_tool_write"):
        raw = await mcp_server.create_memory(
            "core://agent",
            "payload with failing telemetry",
            priority=1,
            title="new_note",
        )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["uri"] == "core://agent/new_note"
    assert "Non-fatal follow-up failure after successful create_memory" in caplog.text
    assert "core://agent/new_note" in caplog.text
    assert "follow_up_down" in caplog.text


@pytest.mark.asyncio
async def test_update_memory_logs_follow_up_side_effect_failures_without_failing_write(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "allow_same_memory",
            "method": "keyword",
            "target_id": 7,
            "target_uri": "core://agent/current",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _raise_follow_up_error)

    with caplog.at_level(logging.WARNING, logger="mcp_tool_write"):
        raw = await mcp_server.update_memory(
            "core://agent/current",
            old_string="world",
            new_string="planet",
        )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["updated"] is True
    assert payload["uri"] == "core://agent/current"
    assert "Non-fatal follow-up failure after successful update_memory" in caplog.text
    assert "core://agent/current" in caplog.text
    assert "follow_up_down" in caplog.text


@pytest.mark.asyncio
async def test_delete_memory_logs_follow_up_side_effect_failures_without_failing_write(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_client = _FakeClient(
        guard_decision={"action": "ADD", "reason": "allow", "method": "keyword"}
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)
    monkeypatch.setattr(mcp_server, "_snapshot_path_delete", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _raise_follow_up_error)

    with caplog.at_level(logging.WARNING, logger="mcp_tool_write"):
        raw = await mcp_server.delete_memory("core://agent/current")

    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["deleted"] is True
    assert payload["uri"] == "core://agent/current"
    assert payload["message"] == "Success: Memory 'core://agent/current' deleted."
    assert "Non-fatal follow-up failure after successful delete_memory" in caplog.text
    assert "core://agent/current" in caplog.text
    assert "follow_up_down" in caplog.text


@pytest.mark.asyncio
async def test_add_alias_logs_follow_up_side_effect_failures_without_failing_write(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_client = _FakeClient(
        guard_decision={"action": "ADD", "reason": "allow", "method": "keyword"}
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)
    monkeypatch.setattr(mcp_server, "_maybe_auto_flush", _raise_follow_up_error)

    with caplog.at_level(logging.WARNING, logger="mcp_tool_write"):
        raw = await mcp_server.add_alias(
            "core://agent/alias",
            "core://agent/current",
            priority=1,
        )

    assert (
        raw
        == "Success: Alias 'core://agent/alias' now points to same memory as "
        "'core://agent/current'"
    )
    assert "Non-fatal follow-up failure after successful add_alias" in caplog.text
    assert "core://agent/alias" in caplog.text
    assert "follow_up_down" in caplog.text


@pytest.mark.asyncio
async def test_update_memory_runs_write_guard_inside_write_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _ConcurrentUpdateGuardClient()
    _patch_mcp_dependencies(monkeypatch, fake_client)
    lane_lock = asyncio.Lock()

    async def _run_serialized_write(_operation: str, task):
        async with lane_lock:
            return await task()

    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_serialized_write)

    first_task = asyncio.create_task(
        mcp_server.update_memory("core://agent/current", old_string="world", new_string="planet")
    )
    await fake_client.first_guard_started.wait()
    second_task = asyncio.create_task(
        mcp_server.update_memory("core://agent/current", old_string="world", new_string="galaxy")
    )
    fake_client.allow_first_guard_to_finish.set()
    first_raw, second_raw = await asyncio.gather(first_task, second_task)
    first_payload = json.loads(first_raw)
    second_payload = json.loads(second_raw)

    assert first_payload["ok"] is True
    assert second_payload["ok"] is True
    assert fake_client.second_guard_started_before_first_update_finished is False


@pytest.mark.asyncio
async def test_update_memory_snapshots_inside_write_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "allow_same_memory",
            "method": "keyword",
            "target_id": 7,
            "target_uri": "core://agent/current",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)
    events: list[str] = []

    async def _snapshot_memory_content(_uri: str) -> None:
        events.append("snapshot")

    async def _run_serialized_write(_operation: str, task):
        events.append("lane-enter")
        try:
            return await task()
        finally:
            events.append("lane-exit")

    monkeypatch.setattr(mcp_server, "_snapshot_memory_content", _snapshot_memory_content)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_serialized_write)

    raw = await mcp_server.update_memory(
        "core://agent/current",
        old_string="world",
        new_string="planet",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert events == ["lane-enter", "snapshot", "lane-exit"]


@pytest.mark.asyncio
async def test_update_memory_reloads_current_content_inside_write_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "allow_same_memory",
            "method": "keyword",
            "target_id": 7,
            "target_uri": "core://agent/current",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    async def _run_write_after_external_change(_operation: str, task):
        fake_client.memory["content"] = "hello brave world"
        return await task()

    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_after_external_change)

    raw = await mcp_server.update_memory(
        "core://agent/current",
        old_string="world",
        new_string="planet",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert fake_client.update_payload["content"] == "hello brave planet"


@pytest.mark.asyncio
async def test_update_memory_timeout_returns_retryable_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "allow_same_memory",
            "method": "keyword",
            "target_id": 7,
            "target_uri": "core://agent/current",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    async def _run_write_lane_timeout(_operation: str, _task):
        raise TimeoutError("write lane task timed out after 1s")

    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_lane_timeout)

    raw = await mcp_server.update_memory(
        "core://agent/current",
        old_string="world",
        new_string="planet",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["updated"] is False
    assert payload["reason"] == "write_lane_timeout"
    assert payload["retryable"] is True
    assert payload["timeout_seconds"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_write_guard_exclude_memory_id_allows_add(tmp_path: Path) -> None:
    db_path = tmp_path / "guard-exclude.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()
    created = await client.create_memory(
        parent_path="",
        content="exclusive payload",
        priority=1,
        title="note_b",
        domain="core",
    )
    decision = await client.write_guard(
        content="exclusive payload",
        domain="core",
        exclude_memory_id=created["id"],
    )
    await client.close()

    assert decision["action"] == "ADD"
    assert decision["target_id"] is None


@pytest.mark.asyncio
async def test_add_path_retries_transient_database_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SQLiteClient("sqlite+aiosqlite:////tmp/add-path-retry.db")
    attempts = 0

    class _FakeResult:
        def __init__(self, value: Any) -> None:
            self._value = value

        def scalar_one_or_none(self) -> Any:
            return self._value

    class _FakeSession:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.added: list[Any] = []

        async def execute(self, _query: Any) -> _FakeResult:
            self.execute_calls += 1
            if self.execute_calls == 1:
                return _FakeResult(7)
            return _FakeResult(None)

        def add(self, value: Any) -> None:
            self.added.append(value)

    @asynccontextmanager
    async def _fake_session():
        nonlocal attempts
        current_attempt = attempts
        session = _FakeSession()
        try:
            yield session
            if current_attempt == 0:
                raise sqlite3.OperationalError("database is locked")
        finally:
            attempts += 1

    monkeypatch.setattr(client, "session", _fake_session)
    sleep_calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("db.sqlite_client.asyncio.sleep", _fake_sleep)

    payload = await client.add_path(
        new_path="pref_alias",
        target_path="pref_concise",
        new_domain="notes",
        target_domain="core",
        priority=1,
        disclosure="when answering",
    )
    await client.close()

    assert payload == {
        "new_uri": "notes://pref_alias",
        "target_uri": "core://pref_concise",
        "memory_id": 7,
    }
    assert attempts == 2
    assert sleep_calls == [0.05]


@pytest.mark.asyncio
async def test_write_guard_is_fail_closed_when_search_backends_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "guard-search-unavailable.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    async def _raise_search_advanced(*_: Any, **__: Any) -> Dict[str, Any]:
        raise RuntimeError("search backend unavailable")

    monkeypatch.setattr(client, "search_advanced", _raise_search_advanced)
    decision = await client.write_guard(content="new candidate", domain="core")
    await client.close()

    assert decision["action"] == "NOOP"
    assert decision["method"] == "exception"
    assert decision["reason"] == "write_guard_unavailable"
    assert decision["degraded"] is True
    assert "write_guard_semantic_failed:RuntimeError" in decision["degrade_reasons"]
    assert "write_guard_keyword_failed:RuntimeError" in decision["degrade_reasons"]


@pytest.mark.asyncio
async def test_write_guard_blocks_ambiguous_add_when_only_keyword_pipeline_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "guard-single-keyword.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    async def _single_pipeline_search(*_: Any, **kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("mode") == "semantic":
            raise RuntimeError("semantic backend unavailable")
        return {
            "results": [
                {
                    "memory_id": 9,
                    "uri": "core://agent/workflow",
                    "snippet": "tests immediately after the code changes",
                    "scores": {"text": 0.54, "final": 0.54},
                }
            ],
            "degrade_reasons": [],
        }

    async def _llm_none(*_: Any, **__: Any) -> Dict[str, Any] | None:
        return None

    monkeypatch.setattr(client, "search_advanced", _single_pipeline_search)
    monkeypatch.setattr(client, "_write_guard_llm_decision", _llm_none)

    decision = await client.write_guard(content="tests immediately after", domain="core")
    await client.close()

    assert decision["action"] == "NOOP"
    assert decision["method"] == "keyword_single_pipeline"
    assert decision["target_uri"] == "core://agent/workflow"
    assert "write_guard_single_pipeline_keyword_blocked" in decision["degrade_reasons"]


@pytest.mark.asyncio
async def test_write_guard_blocks_ambiguous_add_when_only_semantic_pipeline_survives(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "guard-single-semantic.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    async def _single_pipeline_search(*_: Any, **kwargs: Any) -> Dict[str, Any]:
        if kwargs.get("mode") == "keyword":
            raise RuntimeError("keyword backend unavailable")
        return {
            "results": [
                {
                    "memory_id": 8,
                    "uri": "core://agent/profile",
                    "snippet": "preferred workflow profile",
                    "scores": {"vector": 0.76, "final": 0.76},
                }
            ],
            "degrade_reasons": [],
        }

    async def _llm_none(*_: Any, **__: Any) -> Dict[str, Any] | None:
        return None

    monkeypatch.setattr(client, "search_advanced", _single_pipeline_search)
    monkeypatch.setattr(client, "_write_guard_llm_decision", _llm_none)

    decision = await client.write_guard(content="preferred workflow profile", domain="core")
    await client.close()

    assert decision["action"] == "NOOP"
    assert decision["method"] == "embedding_single_pipeline"
    assert decision["target_uri"] == "core://agent/profile"
    assert "write_guard_single_pipeline_semantic_blocked" in decision["degrade_reasons"]


@pytest.mark.asyncio
async def test_write_guard_visual_hash_hit_skips_search_backends_and_returns_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "guard-visual-hash-hit.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()
    created = await client.create_memory(
        parent_path="",
        content="\n".join(
            [
                "# Visual Memory",
                "",
                "- kind: visual-memory",
                "- media_ref: file:/tmp/demo.png",
                "- provenance_media_ref_sha256: sha256-visual-hit",
            ]
        ),
        priority=2,
        title="sha256-visual-hit",
        domain="core",
    )

    async def _raise_search_advanced(*_: Any, **__: Any) -> Dict[str, Any]:
        raise AssertionError("search_advanced should not be called for visual hash fast path")

    monkeypatch.setattr(client, "search_advanced", _raise_search_advanced)
    decision = await client.write_guard(
        content="\n".join(
            [
                "# Visual Memory",
                "",
                "- kind: visual-memory",
                "- media_ref: file:/tmp/demo.png",
                "- provenance_media_ref_sha256: sha256-visual-hit",
            ]
        ),
        domain="core",
    )
    await client.close()

    assert decision["action"] == "UPDATE"
    assert decision["method"] == "visual_hash"
    assert decision["target_id"] == created["id"]
    assert decision["target_uri"] == created["uri"]


@pytest.mark.asyncio
async def test_write_guard_visual_hash_miss_skips_search_backends_and_returns_add(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "guard-visual-hash-miss.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    async def _raise_search_advanced(*_: Any, **__: Any) -> Dict[str, Any]:
        raise AssertionError("search_advanced should not be called for visual hash fast path")

    monkeypatch.setattr(client, "search_advanced", _raise_search_advanced)
    decision = await client.write_guard(
        content="\n".join(
            [
                "# Visual Memory",
                "",
                "- kind: visual-memory",
                "- media_ref: file:/tmp/demo-miss.png",
                "- provenance_media_ref_sha256: sha256-visual-miss",
            ]
        ),
        domain="core",
    )
    await client.close()

    assert decision["action"] == "ADD"
    assert decision["method"] == "visual_hash"
    assert decision["target_id"] is None


@pytest.mark.asyncio
async def test_write_guard_visual_namespace_container_skips_search_backends(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "guard-visual-namespace.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    async def _raise_search_advanced(*_: Any, **__: Any) -> Dict[str, Any]:
        raise AssertionError("search_advanced should not be called for visual namespace fast path")

    monkeypatch.setattr(client, "search_advanced", _raise_search_advanced)
    decision = await client.write_guard(
        content="\n".join(
            [
                "# Visual Namespace Container",
                "visual_namespace_container: true",
                *_force_control_block(
                    "VISUAL_NS_FORCE_URI=core://visual/2026/03/13",
                ),
            ]
        ),
        domain="core",
        path_prefix="visual/2026/03",
    )
    await client.close()

    assert decision["action"] == "ADD"
    assert decision["method"] == "visual_namespace"


@pytest.mark.asyncio
async def test_write_guard_ignores_namespace_container_candidates_for_normal_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "guard-ignore-namespace.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    async def _namespace_only_search_advanced(*_: Any, **__: Any) -> Dict[str, Any]:
        return {
            "results": [
                {
                    "memory_id": 11,
                    "uri": "core://agents/main/captured/llm-extracted/workflow",
                    "snippet": (
                        "# Memory Palace Namespace\n"
                        "- lane: capture\n"
                        "- namespace_uri: core://agents/main/captured/llm-extracted/workflow\n\n"
                        "Container node for capture records."
                    ),
                    "scores": {
                        "vector": 0.94,
                        "text": 0.88,
                        "final": 0.94,
                    },
                }
            ],
            "degrade_reasons": [],
        }

    monkeypatch.setattr(client, "search_advanced", _namespace_only_search_advanced)
    decision = await client.write_guard(
        content="\n".join(
            [
                "# Memory Palace Durable Fact",
                "- category: workflow",
                "- source_mode: llm_extracted",
                "- capture_layer: smart_extraction",
                "",
                "## Summary",
                "Default workflow: code changes first, tests immediately after, docs last.",
            ]
        ),
        domain="core",
        path_prefix="agents/main/captured/llm-extracted/workflow",
    )
    await client.close()

    assert decision["action"] == "ADD"
    assert decision["target_uri"] is None


@pytest.mark.asyncio
async def test_ensure_visual_namespace_chain_creates_missing_visual_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "ensure-visual-namespace-chain.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: client)

    raw = await mcp_server.ensure_visual_namespace_chain(
        "core://visual/2026/03/13/sha256-demo"
    )
    payload = json.loads(raw)

    visual_root = await client.get_memory_by_path("visual", domain="core", reinforce_access=False)
    year_bucket = await client.get_memory_by_path("visual/2026", domain="core", reinforce_access=False)
    month_bucket = await client.get_memory_by_path("visual/2026/03", domain="core", reinforce_access=False)
    day_bucket = await client.get_memory_by_path("visual/2026/03/13", domain="core", reinforce_access=False)
    await client.close()

    assert payload["ok"] is True
    assert payload["created_paths"] == [
        "core://visual",
        "core://visual/2026",
        "core://visual/2026/03",
        "core://visual/2026/03/13",
    ]
    assert visual_root is not None
    assert year_bucket is not None
    assert month_bucket is not None
    assert day_bucket is not None


@pytest.mark.asyncio
async def test_ensure_visual_namespace_chain_reports_existing_paths_without_recreating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "ensure-visual-namespace-existing.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="# Visual Namespace Container",
        priority=5,
        title="visual",
        domain="core",
        index_now=False,
    )

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: client)

    raw = await mcp_server.ensure_visual_namespace_chain(
        "core://visual/2026/03/13/sha256-demo"
    )
    payload = json.loads(raw)
    await client.close()

    assert payload["ok"] is True
    assert payload["existing_paths"] == ["core://visual"]
    assert payload["created_paths"] == [
        "core://visual/2026",
        "core://visual/2026/03",
        "core://visual/2026/03/13",
    ]


@pytest.mark.asyncio
async def test_create_memory_is_blocked_when_guard_returns_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "NOOP",
            "reason": "duplicate content",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agent/existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agent",
        content="duplicate content",
        priority=1,
        title="new_note",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_returns_guard_fields_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "ADD",
            "reason": "no strong duplicate signal",
            "method": "keyword",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agent",
        content="new information",
        priority=2,
        title="fresh_note",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_allows_forced_visual_duplicate_new_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://visual/2026/03/10/sha256-existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://visual/2026/03/10",
        content="\n".join(
            [
                "# Visual Memory",
                "",
                "- kind: visual-memory",
                "- media_ref: file:/tmp/demo.png#variant=new-01",
                "- duplicate_policy: new",
                "- duplicate_variant: new-01",
                "- provenance_variant_uri: core://visual/2026/03/10/sha256-demo--new-01",
                *_force_control_block(
                    "VISUAL_DUP_FORCE_VARIANT_URI=core://visual/2026/03/10/sha256-demo--new-01",
                    "VISUAL_DUP_FORCE_RULE=RETAIN_DISTINCT_VARIANT_RECORD",
                ),
            ]
        ),
        priority=2,
        title="sha256-demo--new-01",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "visual_variant_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_ignores_mid_body_visual_force_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://visual/2026/03/10/sha256-existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    filler_lines = [f"user note line {index}" for index in range(1, 15)]
    raw = await mcp_server.create_memory(
        parent_uri="core://visual/2026/03/10",
        content="\n".join(
            [
                "# Visual Memory",
                "",
                "- kind: visual-memory",
                "- media_ref: file:/tmp/demo.png#variant=new-01",
                "- duplicate_policy: new",
                "VISUAL_DUP_FORCE_VARIANT_URI=core://visual/2026/03/10/sha256-demo--new-01",
                "VISUAL_DUP_FORCE_RULE=RETAIN_DISTINCT_VARIANT_RECORD",
                "",
                "## User Notes",
                *filler_lines,
            ]
        ),
        priority=2,
        title="sha256-demo--new-01",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "UPDATE"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_allows_structured_forced_visual_duplicate_new_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://visual/2026/03/10/sha256-existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://visual/2026/03/10",
        content="\n".join(
            [
                "# Visual Memory",
                "",
                "- kind: visual-memory",
                "- media_ref: file:/tmp/demo.png#variant=new-01",
                "- duplicate_policy: new",
                *_force_control_block(
                    _force_meta_line(
                        {
                            "kind": "visual_duplicate_variant",
                            "requested_uri": "core://visual/2026/03/10/sha256-demo--new-01",
                            "variant_uri": "core://visual/2026/03/10/sha256-demo--new-01",
                            "origin_uri": "core://visual/2026/03/10/sha256-existing",
                            "duplicate_policy": "new",
                            "duplicate_variant": "new-01",
                        }
                    ),
                ),
            ]
        ),
        priority=2,
        title="sha256-demo--new-01",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "visual_variant_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_ignores_mid_body_force_meta_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://visual/2026/03/10/sha256-existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    filler_lines = [f"tail line {index}" for index in range(1, 15)]
    raw = await mcp_server.create_memory(
        parent_uri="core://visual/2026/03/10",
        content="\n".join(
            [
                "# Visual Memory",
                "",
                "- kind: visual-memory",
                "- media_ref: file:/tmp/demo.png#variant=new-01",
                "- duplicate_policy: new",
                _force_meta_line(
                    {
                        "kind": "visual_duplicate_variant",
                        "requested_uri": "core://visual/2026/03/10/sha256-demo--new-01",
                        "variant_uri": "core://visual/2026/03/10/sha256-demo--new-01",
                        "origin_uri": "core://visual/2026/03/10/sha256-existing",
                        "duplicate_policy": "new",
                        "duplicate_variant": "new-01",
                    }
                ),
                "",
                "## User Notes",
                *filler_lines,
            ]
        ),
        priority=2,
        title="sha256-demo--new-01",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "UPDATE"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_allows_forced_visual_distinct_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://visual/2026/03/10/sha256-existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://visual/2026/03/10",
        content="\n".join(
            [
                "# Visual Memory",
                "",
                "- kind: visual-memory",
                "- media_ref: file:/tmp/distinct.png",
                "- provenance_media_ref_sha256: sha256-distinct",
                *_force_control_block(
                    "- visual_force_create_uri: core://visual/2026/03/10/sha256-distinct",
                    "- visual_force_create_token: abc123",
                    "- visual_force_create_reason: disambiguate non-duplicate visual record after write_guard collision",
                ),
            ]
        ),
        priority=2,
        title="sha256-distinct",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "visual_distinct_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_allows_forced_visual_namespace_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://visual/2026/03/10/sha256-existing--new-01",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://visual/2026/03",
        content="\n".join(
            [
                "# Visual Namespace Container",
                "visual_namespace_container: true",
                *_force_control_block(
                    "VISUAL_NS_FORCE_URI=core://visual/2026/03/03",
                    "VISUAL_NS_FORCE_RULE=NO_DEDUP_WITH_PARENT_OR_SIBLING",
                    "- visual_force_create_uri: core://visual/2026/03/03",
                    "- visual_force_create_token: nsforce123",
                    "- visual_force_create_reason: disambiguate non-duplicate visual record after write_guard collision",
                ),
            ]
        ),
        priority=5,
        title="03",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "visual_namespace_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_allows_forced_memory_palace_namespace_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured",
        content="\n".join(
            [
                "# Memory Palace Namespace",
                "- lane: capture",
                "- namespace_uri: core://agents/main/captured/llm-extracted",
                "- parent_uri: core://agents/main/captured",
                "- segment: llm-extracted",
                "- depth: 4",
                "- uniqueness_token: deadbeefcafe",
                "",
                "Container node for capture records.",
                *_force_control_block(
                    "MP_NS_FORCE_CREATE_LANE=capture",
                    "MP_NS_FORCE_CREATE_URI=core://agents/main/captured/llm-extracted",
                ),
            ]
        ),
        priority=4,
        title="llm-extracted",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "memory_palace_namespace_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_keeps_memory_palace_namespace_blocked_without_force_control_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured",
        content="\n".join(
            [
                "# Memory Palace Namespace",
                "- lane: capture",
                "- namespace_uri: core://agents/main/captured/llm-extracted",
                "- parent_uri: core://agents/main/captured",
                "",
                "Container node for capture records.",
            ]
        ),
        priority=4,
        title="llm-extracted",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "UPDATE"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_allows_structured_forced_memory_palace_namespace_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured",
        content="\n".join(
            [
                "# Memory Palace Namespace",
                "- lane: capture",
                "- namespace_uri: core://agents/main/captured/llm-extracted",
                "- parent_uri: core://agents/main/captured",
                "",
                "Container node for capture records.",
                *_force_control_block(
                    _force_meta_line(
                        {
                            "kind": "memory_palace_namespace_force_create",
                            "requested_uri": "core://agents/main/captured/llm-extracted",
                            "target_uri": "core://agents/main/captured/llm-extracted",
                            "lane": "capture",
                        }
                    )
                ),
            ]
        ),
        priority=4,
        title="llm-extracted",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "memory_palace_namespace_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_allows_long_force_control_block_at_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured",
        content="\n".join(
            [
                "# Memory Palace Namespace",
                "- lane: capture",
                "- namespace_uri: core://agents/main/captured/llm-extracted",
                "- parent_uri: core://agents/main/captured",
                "",
                "Container node for capture records.",
                *_force_control_block(
                    "MP_NS_FORCE_CREATE_LANE=capture",
                    "MP_NS_FORCE_CREATE_URI=core://agents/main/captured/llm-extracted",
                    *[f"MP_NS_DEBUG_{index}=value-{index}" for index in range(1, 18)],
                ),
            ]
        ),
        priority=4,
        title="llm-extracted",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "memory_palace_namespace_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_allows_forced_host_bridge_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/profile/workflow",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/host-bridge/workflow",
        content="\n".join(
            [
                "# Host Workspace Import",
                "- category: workflow",
                "- capture_layer: host_bridge",
                "- source_mode: host_workspace_import",
                "- confidence: 0.88",
                "",
                "## Content",
                "default workflow: code first, tests immediately after, docs last",
                "",
                "## Provenance",
                "- MEMORY.md#L1 sha256-deadbeefcafe",
                *_force_control_block(
                    "- host_bridge_force_create_uri: core://agents/main/host-bridge/workflow/sha256-demo12345678",
                    "- host_bridge_force_create_reason: retain host-bridge provenance record after write_guard collision",
                ),
            ]
        ),
        priority=2,
        title="sha256-demo12345678",
        disclosure="Host bridge durable import",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "host_bridge_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_allows_structured_forced_host_bridge_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/profile/workflow",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/host-bridge/workflow",
        content="\n".join(
            [
                "# Host Workspace Import",
                "- category: workflow",
                "- capture_layer: host_bridge",
                "- source_mode: host_workspace_import",
                "",
                "## Content",
                "default workflow: code first, tests immediately after, docs last",
                *_force_control_block(
                    _force_meta_line(
                        {
                            "kind": "host_bridge_force_create",
                            "requested_uri": "core://agents/main/host-bridge/workflow/sha256-demo12345678",
                            "target_uri": "core://agents/main/host-bridge/workflow/sha256-demo12345678",
                        }
                    ),
                ),
            ]
        ),
        priority=2,
        title="sha256-demo12345678",
        disclosure="Host bridge durable import",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "host_bridge_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_allows_explicit_memory_force_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_uri = "core://agents/main/captured/workflow/current"
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured/workflow/existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured/workflow",
        content="\n".join(
            [
                "# Memory Palace Durable Fact",
                "- category: workflow",
                "- capture_layer: manual_learn",
                "",
                "## Content",
                "Please remember this workflow as a separate durable record.",
                *_force_control_block(
                    "- create_after_merge_update_write_guard: true",
                    f"- target_uri: {requested_uri}",
                ),
            ]
        ),
        priority=1,
        title="current",
        disclosure="Explicit user-confirmed durable memory",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "explicit_memory_force_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_keeps_explicit_force_override_blocked_for_plain_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_uri = "core://agents/main/captured/workflow/current"
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured/workflow/existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured/workflow",
        content="\n".join(
            [
                "# Plain Note",
                "This should stay blocked.",
                *_force_control_block(
                    "- create_after_merge_update_write_guard: true",
                    f"- target_uri: {requested_uri}",
                ),
            ]
        ),
        priority=1,
        title="current",
        disclosure="Explicit user-confirmed durable memory",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "UPDATE"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_ignores_structured_explicit_memory_force_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_uri = "core://agents/main/captured/workflow/current"
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured/workflow/existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured/workflow",
        content="\n".join(
            [
                "# Memory Palace Durable Fact",
                "- category: workflow",
                "",
                "## Content",
                "Structured force meta alone must not bypass write_guard.",
                *_force_control_block(
                    _force_meta_line(
                        {
                            "kind": "explicit_memory_force_create",
                            "requested_uri": requested_uri,
                            "target_uri": requested_uri,
                        }
                    ),
                ),
            ]
        ),
        priority=1,
        title="current",
        disclosure="Explicit user-confirmed durable memory",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "UPDATE"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_keeps_explicit_force_override_blocked_outside_captured_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_uri = "core://agents/main/profile/workflow/current"
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/profile/workflow/existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/profile/workflow",
        content="\n".join(
            [
                "# Memory Palace Durable Fact",
                "- category: workflow",
                "",
                "## Content",
                "Out-of-scope explicit force retry must stay blocked.",
                *_force_control_block(
                    "- create_after_merge_update_write_guard: true",
                    f"- target_uri: {requested_uri}",
                ),
            ]
        ),
        priority=1,
        title="current",
        disclosure="Explicit user-confirmed durable memory",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "UPDATE"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_allows_explicit_memory_force_override_for_auto_captured_sha_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_uri = "core://agents/main/captured/workflow/sha256-force-demo1234"
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured/workflow/sha256-existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured/workflow",
        content="\n".join(
            [
                "# Auto Captured Memory",
                "- category: workflow",
                "- captured_at: 2026-04-14T00:00:00Z",
                "- agent_id: main",
                "",
                "## Content",
                "Please store this blocked workflow variant as a separate manual durable memory.",
                *_force_control_block(
                    "- create_after_merge_update_write_guard: true",
                    f"- target_uri: {requested_uri}",
                ),
            ]
        ),
        priority=1,
        title="sha256-force-demo1234",
        disclosure="Explicit user-confirmed durable memory",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "explicit_memory_force_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_prefers_host_bridge_override_when_multiple_force_create_meta_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_uri = "core://agents/main/host-bridge/workflow/sha256-demo12345678"
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/profile/workflow",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/host-bridge/workflow",
        content="\n".join(
            [
                "# Mixed Force Create Meta",
                *_force_control_block(
                    _force_meta_line(
                        {
                            "kind": "host_bridge_force_create",
                            "requested_uri": requested_uri,
                            "target_uri": requested_uri,
                        }
                    ),
                    _force_meta_line(
                        {
                            "kind": "durable_synthesis_force_variant",
                            "requested_uri": requested_uri,
                            "target_uri": "core://agents/main/captured/llm-extracted/workflow/current",
                        }
                    ),
                ),
            ]
        ),
        priority=2,
        title="sha256-demo12345678",
        disclosure="Host bridge durable import",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "host_bridge_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_does_not_override_host_bridge_without_distinct_guard_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": None,
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/host-bridge/workflow",
        content="\n".join(
            [
                "# Host Workspace Import",
                "- category: workflow",
                "- capture_layer: host_bridge",
                "- source_mode: host_workspace_import",
                "",
                "## Content",
                "default workflow: code first, tests immediately after, docs last",
                *_force_control_block(
                    _force_meta_line(
                        {
                            "kind": "host_bridge_force_create",
                            "requested_uri": "core://agents/main/host-bridge/workflow/sha256-demo12345678",
                            "target_uri": "core://agents/main/host-bridge/workflow/sha256-demo12345678",
                        }
                    ),
                ),
            ]
        ),
        priority=2,
        title="sha256-demo12345678",
        disclosure="Host bridge durable import",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "UPDATE"
    assert payload["guard_method"] == "embedding"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_does_not_override_host_bridge_when_guard_target_matches_requested_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_uri = "core://agents/main/host-bridge/workflow/sha256-demo12345678"
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": requested_uri,
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/host-bridge/workflow",
        content="\n".join(
            [
                "# Host Workspace Import",
                "- category: workflow",
                "- capture_layer: host_bridge",
                "- source_mode: host_workspace_import",
                "",
                "## Content",
                "default workflow: code first, tests immediately after, docs last",
                *_force_control_block(
                    _force_meta_line(
                        {
                            "kind": "host_bridge_force_create",
                            "requested_uri": requested_uri,
                            "target_uri": requested_uri,
                        }
                    ),
                ),
            ]
        ),
        priority=2,
        title="sha256-demo12345678",
        disclosure="Host bridge durable import",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "UPDATE"
    assert payload["guard_method"] == "embedding"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_allows_forced_durable_synthesis_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured/llm-extracted/workflow",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured/llm-extracted/workflow",
        content="\n".join(
            [
                "# Memory Palace Durable Fact",
                "- category: workflow",
                "- source_mode: llm_extracted",
                "- capture_layer: smart_extraction",
                "- confidence: 0.93",
                "- pending_candidate: false",
                "",
                "## Summary",
                "Default workflow: code changes first, tests immediately after, docs last",
                "",
                "## Evidence",
                "- (none)",
                *_force_control_block(
                    "- durable_synthesis_force_current: true",
                    "- target_uri: core://agents/main/captured/llm-extracted/workflow/current",
                ),
            ]
        ),
        priority=2,
        title="current",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "durable_synthesis_current_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_allows_structured_forced_durable_synthesis_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured/llm-extracted/workflow",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured/llm-extracted/workflow",
        content="\n".join(
            [
                "# Memory Palace Durable Fact",
                "- category: workflow",
                "- source_mode: llm_extracted",
                "- capture_layer: smart_extraction",
                "",
                "## Summary",
                "Default workflow: code changes first, tests immediately after, docs last",
                *_force_control_block(
                    _force_meta_line(
                        {
                            "kind": "durable_synthesis_force_current",
                            "requested_uri": "core://agents/main/captured/llm-extracted/workflow/current",
                            "target_uri": "core://agents/main/captured/llm-extracted/workflow/current",
                            "source_mode": "llm_extracted",
                            "capture_layer": "smart_extraction",
                        }
                    ),
                ),
            ]
        ),
        priority=2,
        title="current",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "durable_synthesis_current_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_allows_forced_durable_synthesis_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agents/main/captured/llm-extracted/workflow",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agents/main/captured/llm-extracted/workflow",
        content="\n".join(
            [
                "# Memory Palace Durable Fact",
                "- category: workflow",
                "- source_mode: llm_extracted",
                "- capture_layer: smart_extraction",
                "- confidence: 0.93",
                "- pending_candidate: false",
                "",
                "## Summary",
                "Default workflow: code changes first, tests immediately after, docs last",
                "",
                "## Evidence",
                "- (none)",
                *_force_control_block(
                    "- durable_synthesis_force_variant: true",
                    "- target_uri: core://agents/main/captured/llm-extracted/workflow/current",
                ),
            ]
        ),
        priority=2,
        title="current--force-1234abcd",
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["created"] is True
    assert payload["guard_action"] == "ADD"
    assert payload["guard_method"] == "durable_synthesis_variant_override"
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_create_memory_keeps_non_forced_visual_duplicate_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "semantic similarity 0.901 >= 0.780",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://visual/2026/03/10/sha256-existing",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://visual/2026/03/10",
        content="\n".join(
            [
                "# Visual Memory",
                "",
                "- kind: visual-memory",
                "- duplicate_policy: new",
                "- duplicate_variant: new-01",
            ]
        ),
        priority=2,
        title="sha256-demo--new-01",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "UPDATE"
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_is_fail_closed_when_guard_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _GuardErrorClient(
        guard_decision={"action": "ADD", "reason": "unused", "method": "keyword"}
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agent",
        content="new information",
        priority=2,
        title="fresh_note",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert payload["guard_method"] == "exception"
    assert fake_client.create_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_action",
    ["unexpected_action", "", None],
)
async def test_create_memory_invalid_guard_action_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    invalid_action: Any,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": invalid_action,
            "reason": "model_output_not_supported",
            "method": "embedding",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agent",
        content="new information",
        priority=2,
        title="fresh_note",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert "invalid_guard_action" in str(payload.get("guard_reason") or "")
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_missing_guard_action_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "reason": "guard_payload_missing_action",
            "method": "embedding",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agent",
        content="new information",
        priority=2,
        title="fresh_note",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert "invalid_guard_action:MISSING" in str(payload.get("guard_reason") or "")
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_create_memory_guard_bypass_action_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "BYPASS",
            "reason": "unexpected_bypass",
            "method": "embedding",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.create_memory(
        parent_uri="core://agent",
        content="new information",
        priority=2,
        title="fresh_note",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert "invalid_guard_action:BYPASS" in str(payload.get("guard_reason") or "")
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_update_memory_is_blocked_when_guard_returns_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "NOOP",
            "reason": "no effective change",
            "method": "embedding",
            "target_id": 7,
            "target_uri": "core://agent/current",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.update_memory(
        uri="core://agent/current",
        old_string="world",
        new_string="planet",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["updated"] is False
    assert payload["guard_action"] == "NOOP"
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_update_memory_is_fail_closed_when_guard_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _GuardErrorClient(
        guard_decision={"action": "ADD", "reason": "unused", "method": "keyword"}
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.update_memory(
        uri="core://agent/current",
        old_string="world",
        new_string="planet",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["updated"] is False
    assert payload["guard_action"] == "NOOP"
    assert payload["guard_method"] == "exception"
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_update_memory_missing_guard_action_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "reason": "guard_payload_missing_action",
            "method": "embedding",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.update_memory(
        uri="core://agent/current",
        old_string="world",
        new_string="planet",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["updated"] is False
    assert payload["guard_action"] == "NOOP"
    assert "invalid_guard_action:MISSING" in str(payload.get("guard_reason") or "")
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_update_memory_guard_update_without_target_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "possible_duplicate_without_target",
            "method": "embedding",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.update_memory(
        uri="core://agent/current",
        old_string="world",
        new_string="planet",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["updated"] is False
    assert payload["guard_action"] == "UPDATE"
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_update_memory_metadata_only_marks_guard_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "ADD",
            "reason": "unused",
            "method": "keyword",
        }
    )
    _patch_mcp_dependencies(monkeypatch, fake_client)

    raw = await mcp_server.update_memory(
        uri="core://agent/current",
        priority=5,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["updated"] is True
    assert payload["guard_action"] == "BYPASS"
    assert fake_client.update_called is True


@pytest.mark.asyncio
async def test_browse_create_node_is_blocked_by_write_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "NOOP",
            "reason": "duplicate",
            "method": "embedding",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="duplicate",
            priority=1,
            domain="core",
        )
    )

    assert payload["success"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert payload["force_write_available"] is True
    assert "duplicate" in str(payload["guard_user_reason"]).lower()
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_browse_create_node_rejects_unknown_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={"action": "ADD", "reason": "allow", "method": "keyword"}
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        await browse_api.create_node(
            browse_api.NodeCreate(
                parent_path="agent",
                title="new_note",
                content="create payload",
                priority=1,
                domain="unknown-domain",
            )
        )

    assert exc_info.value.status_code == 422
    assert "Unknown domain" in str(exc_info.value.detail)
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_browse_create_node_records_guard_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "NOOP",
            "reason": "duplicate",
            "method": "embedding",
        }
    )
    tracker = GuardDecisionTracker()
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(browse_api.runtime_state, "guard_tracker", tracker)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="duplicate",
            priority=1,
            domain="core",
        )
    )
    stats = await tracker.summary()

    assert payload["created"] is False
    assert stats["total_events"] == 1
    assert stats["blocked_events"] == 1
    assert stats["operation_breakdown"]["browse.create_node"] == 1


@pytest.mark.asyncio
async def test_browse_create_node_allows_force_write_after_guard_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "NOOP",
            "reason": "duplicate",
            "method": "embedding",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    # Step 1: first call without force_write to get the override token.
    blocked_payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="duplicate",
            priority=1,
            domain="core",
        )
    )
    assert blocked_payload["success"] is False
    assert blocked_payload["force_write_available"] is True
    override_token = blocked_payload.get("guard_override_token")
    assert override_token is not None

    # Step 2: retry with force_write=True and the server-issued token.
    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="duplicate",
            priority=1,
            domain="core",
            force_write=True,
            guard_override_token=override_token,
        )
    )

    assert payload["success"] is True
    assert payload["created"] is True
    assert payload["guard_overridden"] is True
    assert fake_client.create_called is True


@pytest.mark.asyncio
async def test_browse_create_node_is_fail_closed_when_guard_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _GuardErrorClient(
        guard_decision={"action": "ADD", "reason": "unused", "method": "keyword"}
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="create payload",
            priority=1,
            domain="core",
        )
    )

    assert payload["success"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert payload["guard_method"] == "exception"
    assert fake_client.create_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_action",
    ["UNEXPECTED_ACTION", "", None],
)
async def test_browse_create_node_invalid_guard_action_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    invalid_action: Any,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": invalid_action,
            "reason": "guard_model_bad_action",
            "method": "keyword",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="create payload",
            priority=1,
            domain="core",
        )
    )

    assert payload["success"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert "invalid_guard_action" in str(payload.get("guard_reason") or "")
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_browse_create_node_missing_guard_action_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "reason": "guard_payload_missing_action",
            "method": "embedding",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="create payload",
            priority=1,
            domain="core",
        )
    )

    assert payload["success"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert "invalid_guard_action:MISSING" in str(payload.get("guard_reason") or "")
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_browse_create_node_guard_bypass_action_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "BYPASS",
            "reason": "unexpected_bypass",
            "method": "embedding",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="create payload",
            priority=1,
            domain="core",
        )
    )

    assert payload["success"] is False
    assert payload["created"] is False
    assert payload["guard_action"] == "NOOP"
    assert "invalid_guard_action:BYPASS" in str(payload.get("guard_reason") or "")
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_browse_update_node_metadata_only_marks_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "ADD",
            "reason": "unused",
            "method": "keyword",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.update_node(
        path="agent/current",
        domain="core",
        body=browse_api.NodeUpdate(priority=9),
    )

    assert payload["success"] is True
    assert payload["updated"] is True
    assert payload["guard_action"] == "BYPASS"
    assert fake_client.update_called is True


@pytest.mark.asyncio
async def test_browse_update_node_blocks_guard_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "NOOP",
            "reason": "duplicate",
            "method": "keyword",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.update_node(
        path="agent/current",
        domain="core",
        body=browse_api.NodeUpdate(content="replace payload"),
    )

    assert payload["success"] is False
    assert payload["updated"] is False
    assert payload["guard_action"] == "NOOP"
    assert payload["force_write_available"] is True
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_browse_update_node_allows_force_write_after_guard_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "duplicate",
            "method": "keyword",
            "target_id": 11,
            "target_uri": "core://agent/existing",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    # Step 1: get the override token from a blocked response.
    blocked_payload = await browse_api.update_node(
        path="agent/current",
        domain="core",
        body=browse_api.NodeUpdate(content="replace payload"),
    )
    assert blocked_payload["success"] is False
    assert blocked_payload["force_write_available"] is True
    override_token = blocked_payload.get("guard_override_token")
    assert override_token is not None

    # Step 2: retry with force_write=True and the token.
    payload = await browse_api.update_node(
        path="agent/current",
        domain="core",
        body=browse_api.NodeUpdate(
            content="replace payload",
            force_write=True,
            guard_override_token=override_token,
        ),
    )

    assert payload["success"] is True
    assert payload["updated"] is True
    assert payload["guard_overridden"] is True
    assert fake_client.update_called is True


@pytest.mark.asyncio
async def test_browse_update_node_is_fail_closed_when_guard_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _GuardErrorClient(
        guard_decision={"action": "ADD", "reason": "unused", "method": "keyword"}
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.update_node(
        path="agent/current",
        domain="core",
        body=browse_api.NodeUpdate(content="replace payload"),
    )

    assert payload["success"] is False
    assert payload["updated"] is False
    assert payload["guard_action"] == "NOOP"
    assert payload["guard_method"] == "exception"
    assert payload["force_write_available"] is False
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_browse_create_node_rejects_force_write_when_guard_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force_write=True must be rejected when the guard was unavailable."""
    fake_client = _GuardErrorClient(
        guard_decision={"action": "ADD", "reason": "unused", "method": "keyword"}
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="forced_note",
            content="force payload",
            priority=1,
            domain="core",
            force_write=True,
        )
    )

    assert payload["success"] is False
    assert payload["created"] is False
    assert payload["reason"] == "force_write_rejected"
    assert payload["force_write_available"] is False
    assert fake_client.create_called is False


@pytest.mark.asyncio
async def test_browse_update_node_rejects_force_write_when_guard_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """force_write=True must be rejected when the guard was unavailable."""
    fake_client = _GuardErrorClient(
        guard_decision={"action": "ADD", "reason": "unused", "method": "keyword"}
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.update_node(
        path="agent/current",
        domain="core",
        body=browse_api.NodeUpdate(content="force payload", force_write=True),
    )

    assert payload["success"] is False
    assert payload["updated"] is False
    assert payload["reason"] == "force_write_rejected"
    assert payload["force_write_available"] is False
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_browse_update_node_missing_guard_action_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "reason": "guard_payload_missing_action",
            "method": "embedding",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.update_node(
        path="agent/current",
        domain="core",
        body=browse_api.NodeUpdate(content="replace payload"),
    )

    assert payload["success"] is False
    assert payload["updated"] is False
    assert payload["guard_action"] == "NOOP"
    assert "invalid_guard_action:MISSING" in str(payload.get("guard_reason") or "")
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_browse_update_node_guard_update_without_target_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={
            "action": "UPDATE",
            "reason": "possible_duplicate_without_target",
            "method": "embedding",
        }
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    payload = await browse_api.update_node(
        path="agent/current",
        domain="core",
        body=browse_api.NodeUpdate(content="replace payload"),
    )

    assert payload["success"] is False
    assert payload["updated"] is False
    assert payload["guard_action"] == "UPDATE"
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_browse_update_node_rejects_read_only_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeClient(
        guard_decision={"action": "ADD", "reason": "allow", "method": "keyword"}
    )
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        await browse_api.update_node(
            path="agent/current",
            domain="system",
            body=browse_api.NodeUpdate(content="replace payload"),
        )

    assert exc_info.value.status_code == 422
    assert "read-only" in str(exc_info.value.detail)
    assert fake_client.update_called is False


@pytest.mark.asyncio
async def test_observability_summary_includes_guard_stats(
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
                "total_rows": 0,
                "distinct_memory_count": 0,
                "total_distinct_memory_count": 0,
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

    tracker = GuardDecisionTracker()
    await tracker.record_event(
        operation="create_memory",
        action="NOOP",
        method="embedding",
        reason="duplicate",
        blocked=True,
    )

    client = SQLiteClient(_sqlite_url(tmp_path / "observability_guard_stats.db"))
    await client.init_db()
    try:
        monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: _DummyClient(client.engine))
        monkeypatch.setattr(maintenance_api.runtime_state, "guard_tracker", tracker)
        monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
        monkeypatch.setattr(maintenance_api.runtime_state.index_worker, "status", _index_worker_status)
        monkeypatch.setattr(maintenance_api.runtime_state.write_lanes, "status", _write_lane_status)
        monkeypatch.setenv(
            maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV,
            str(tmp_path / "missing-transport-diagnostics.json"),
        )

        payload = await maintenance_api.get_observability_summary()
    finally:
        await client.close()

    assert payload["status"] == "ok"
    assert "guard_stats" in payload
    assert payload["guard_stats"]["total_events"] == 1
    assert payload["guard_stats"]["blocked_events"] == 1


# ── W-6 Regression: hash backend auto-disables score normalization ──


class TestWriteGuardScoreNormalizationAutoDetection:
    """Verify that _write_guard_score_normalization is auto-derived from
    RETRIEVAL_EMBEDDING_BACKEND, not manually forced in tests."""

    @pytest.mark.parametrize(
        "backend, expected_norm",
        [
            ("hash", False),
            ("", False),
            ("api", True),
            ("router", True),
            ("openai", True),
        ],
    )
    def test_normalization_default_derived_from_backend(
        self, tmp_path, monkeypatch, backend, expected_norm
    ):
        monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", backend)
        # Ensure no explicit override — test the auto-detection path
        monkeypatch.delenv("WRITE_GUARD_SCORE_NORMALIZATION", raising=False)

        client = SQLiteClient(_sqlite_url(tmp_path / "test_norm.db"))
        assert client._write_guard_score_normalization is expected_norm, (
            f"backend={backend!r}: expected normalization={expected_norm}, "
            f"got {client._write_guard_score_normalization}"
        )

    def test_explicit_override_takes_precedence(self, tmp_path, monkeypatch):
        """Even for hash backend, an explicit env var override should win."""
        monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "hash")
        monkeypatch.setenv("WRITE_GUARD_SCORE_NORMALIZATION", "true")

        client = SQLiteClient(_sqlite_url(tmp_path / "test_norm_override.db"))
        assert client._write_guard_score_normalization is True

    def test_hash_backend_explicit_false_is_redundant_but_safe(
        self, tmp_path, monkeypatch
    ):
        """Explicitly setting false on hash should be a no-op."""
        monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "hash")
        monkeypatch.setenv("WRITE_GUARD_SCORE_NORMALIZATION", "false")

        client = SQLiteClient(_sqlite_url(tmp_path / "test_norm_noop.db"))
        assert client._write_guard_score_normalization is False
