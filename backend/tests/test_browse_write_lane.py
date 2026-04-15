from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException

from api import browse as browse_api


class _FakeBrowseClient:
    def __init__(self) -> None:
        self.memory = {
            "id": 7,
            "content": "origin",
            "priority": 1,
            "disclosure": None,
        }
        self.create_called = False
        self.update_called = False
        self.remove_called = False

    async def write_guard(self, **_: Any) -> Dict[str, Any]:
        return {
            "action": "ADD",
            "reason": "allow",
            "method": "keyword",
        }

    async def create_memory(self, **_: Any) -> Dict[str, Any]:
        self.create_called = True
        return {
            "id": 11,
            "path": "agent/new_note",
            "uri": "core://agent/new_note",
            "index_targets": [11],
        }

    async def get_memory_by_path(
        self, path: str, domain: str = "core", reinforce_access: bool = True
    ) -> Optional[Dict[str, Any]]:
        _ = path
        _ = domain
        _ = reinforce_access
        return dict(self.memory)

    async def update_memory(self, **_: Any) -> Dict[str, Any]:
        self.update_called = True
        return {
            "uri": "core://agent/new_note",
            "new_memory_id": 19,
            "index_targets": [19],
        }

    async def remove_path(self, path: str, domain: str = "core") -> Dict[str, Any]:
        _ = path
        _ = domain
        self.remove_called = True
        return {
            "deleted": True,
            "memory_id": 19,
            "descendants_deleted": 0,
            "orphan_memories_deleted": 0,
        }


def _patch_browse_side_effect_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _noop_async(*args, **kwargs):
        _ = args
        _ = kwargs
        return None

    monkeypatch.setattr(browse_api, "_snapshot_path_create", _noop_async)
    monkeypatch.setattr(browse_api, "_snapshot_memory_content", _noop_async)
    monkeypatch.setattr(browse_api, "_snapshot_path_meta", _noop_async)
    monkeypatch.setattr(browse_api, "_snapshot_path_delete", _noop_async)
    monkeypatch.setattr(browse_api, "_record_session_hit", _noop_async)
    monkeypatch.setattr(browse_api, "_record_flush_event", _noop_async)
    monkeypatch.setattr(browse_api, "_maybe_auto_flush", _noop_async)

    async def _false() -> bool:
        return False

    async def _empty_index(*args, **kwargs):
        _ = args
        _ = kwargs
        return {"queued": [], "dropped": [], "deduped": []}

    monkeypatch.setattr(browse_api, "_should_defer_index_on_write", _false)
    monkeypatch.setattr(browse_api, "_enqueue_index_targets", _empty_index)


@pytest.mark.asyncio
async def test_browse_write_endpoints_run_through_write_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeBrowseClient()
    lane_calls: list[dict[str, Any]] = []

    async def _run_write_lane_impl(
        *,
        runtime_state,
        get_sqlite_client,
        get_session_id,
        enable_write_lane_queue,
        operation,
        fn,
    ):
        _ = runtime_state
        _ = get_sqlite_client
        lane_calls.append(
            {
                "session_id": get_session_id() if enable_write_lane_queue else None,
                "operation": operation,
            }
        )
        return await fn()

    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(browse_api, "run_write_lane_impl", _run_write_lane_impl)
    monkeypatch.setattr(browse_api, "ENABLE_WRITE_LANE_QUEUE", True)
    _patch_browse_side_effect_noops(monkeypatch)

    create_payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="create payload",
            priority=1,
            domain="core",
        ),
        session_id=None,
    )
    update_payload = await browse_api.update_node(
        path="agent/new_note",
        domain="core",
        session_id=None,
        body=browse_api.NodeUpdate(content="update payload"),
    )
    delete_payload = await browse_api.delete_node(
        path="agent/new_note",
        domain="core",
        session_id=None,
    )

    assert create_payload["success"] is True
    assert create_payload["created"] is True
    assert update_payload["success"] is True
    assert update_payload["updated"] is True
    assert delete_payload["success"] is True

    assert fake_client.create_called is True
    assert fake_client.update_called is True
    assert fake_client.remove_called is True
    assert lane_calls == [
        {"session_id": "browse.dashboard", "operation": "browse.create_node"},
        {"session_id": "browse.dashboard", "operation": "browse.update_node"},
        {"session_id": "browse.dashboard", "operation": "browse.delete_node"},
    ]


@pytest.mark.asyncio
async def test_browse_write_endpoints_keep_global_lane_when_queue_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeBrowseClient()
    lane_calls: list[dict[str, Any]] = []

    async def _run_write_lane_impl(
        *,
        runtime_state,
        get_sqlite_client,
        get_session_id,
        enable_write_lane_queue,
        operation,
        fn,
    ):
        _ = runtime_state
        _ = get_sqlite_client
        lane_calls.append(
            {
                "session_id": get_session_id() if enable_write_lane_queue else None,
                "operation": operation,
            }
        )
        return await fn()

    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(browse_api, "run_write_lane_impl", _run_write_lane_impl)
    monkeypatch.setattr(browse_api, "ENABLE_WRITE_LANE_QUEUE", False)
    _patch_browse_side_effect_noops(monkeypatch)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="create payload",
            priority=1,
            domain="core",
        ),
        session_id=None,
    )

    assert payload["success"] is True
    assert fake_client.create_called is True
    assert lane_calls == [
        {"session_id": None, "operation": "browse.create_node"},
    ]


@pytest.mark.asyncio
async def test_browse_create_rejects_invalid_title_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeBrowseClient()
    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)

    with pytest.raises(HTTPException) as exc_info:
        await browse_api.create_node(
            browse_api.NodeCreate(
                parent_path="agent",
                title="bad/title",
                content="create payload",
                priority=1,
                domain="core",
            ),
            session_id=None,
        )

    assert exc_info.value.status_code == 422
    assert "Title must only contain" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_browse_create_accepts_unicode_title_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeBrowseClient()
    lane_calls: list[dict[str, Any]] = []

    async def _run_write_lane_impl(
        *,
        runtime_state,
        get_sqlite_client,
        get_session_id,
        enable_write_lane_queue,
        operation,
        fn,
    ):
        _ = runtime_state
        _ = get_sqlite_client
        lane_calls.append(
            {
                "session_id": get_session_id() if enable_write_lane_queue else None,
                "operation": operation,
            }
        )
        return await fn()

    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(browse_api, "run_write_lane_impl", _run_write_lane_impl)
    monkeypatch.setattr(browse_api, "ENABLE_WRITE_LANE_QUEUE", True)
    _patch_browse_side_effect_noops(monkeypatch)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="日本語メモ",
            content="unicode title payload",
            priority=1,
            domain="core",
        ),
        session_id=None,
    )

    assert payload["success"] is True
    assert payload["created"] is True
    assert fake_client.create_called is True
    assert lane_calls == [
        {"session_id": "browse.dashboard", "operation": "browse.create_node"},
    ]


@pytest.mark.asyncio
async def test_browse_write_endpoints_apply_snapshot_session_and_flush_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeBrowseClient()
    fake_client.memory["created_at"] = "2026-04-14T00:00:00Z"
    lane_calls: list[dict[str, Any]] = []
    snapshot_calls: list[tuple[str, str, Optional[int]]] = []
    session_hits: list[dict[str, Any]] = []
    flush_events: list[dict[str, Any]] = []
    auto_flush_calls: list[dict[str, Any]] = []
    index_enqueue_calls: list[dict[str, Any]] = []

    async def _run_write_lane_impl(
        *,
        runtime_state,
        get_sqlite_client,
        get_session_id,
        enable_write_lane_queue,
        operation,
        fn,
    ):
        _ = runtime_state
        _ = get_sqlite_client
        lane_calls.append(
            {
                "session_id": get_session_id() if enable_write_lane_queue else None,
                "operation": operation,
            }
        )
        return await fn()

    async def _snapshot_path_create(uri: str, memory_id: int, *, session_id: Optional[str], operation_type: str = "create"):
        snapshot_calls.append(("create", uri, memory_id))
        _ = session_id
        _ = operation_type
        return True

    async def _snapshot_memory_content(uri: str, *, session_id: Optional[str]):
        snapshot_calls.append(("content", uri, None))
        _ = session_id
        return True

    async def _snapshot_path_meta(uri: str, *, session_id: Optional[str]):
        snapshot_calls.append(("meta", uri, None))
        _ = session_id
        return True

    async def _snapshot_path_delete(uri: str, *, session_id: Optional[str]):
        snapshot_calls.append(("delete", uri, None))
        _ = session_id
        return True

    async def _record_session_hit(**kwargs):
        session_hits.append(dict(kwargs))

    async def _record_flush_event(message: str, *, session_id: Optional[str]):
        flush_events.append({"message": message, "session_id": session_id})

    async def _maybe_auto_flush(client, *, reason: str, session_id: Optional[str]):
        _ = client
        auto_flush_calls.append({"reason": reason, "session_id": session_id})
        return None

    async def _should_defer() -> bool:
        return True

    async def _enqueue(payload: Any, *, reason: str):
        index_enqueue_calls.append({"reason": reason, "payload": payload})
        return {"queued": [{"job_id": "idx-1"}], "dropped": [], "deduped": []}

    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(browse_api, "run_write_lane_impl", _run_write_lane_impl)
    monkeypatch.setattr(browse_api, "_snapshot_path_create", _snapshot_path_create)
    monkeypatch.setattr(browse_api, "_snapshot_memory_content", _snapshot_memory_content)
    monkeypatch.setattr(browse_api, "_snapshot_path_meta", _snapshot_path_meta)
    monkeypatch.setattr(browse_api, "_snapshot_path_delete", _snapshot_path_delete)
    monkeypatch.setattr(browse_api, "_record_session_hit", _record_session_hit)
    monkeypatch.setattr(browse_api, "_record_flush_event", _record_flush_event)
    monkeypatch.setattr(browse_api, "_maybe_auto_flush", _maybe_auto_flush)
    monkeypatch.setattr(browse_api, "_should_defer_index_on_write", _should_defer)
    monkeypatch.setattr(browse_api, "_enqueue_index_targets", _enqueue)
    monkeypatch.setattr(browse_api, "ENABLE_WRITE_LANE_QUEUE", True)

    create_payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="create payload",
            priority=1,
            domain="core",
        ),
        session_id=None,
    )
    update_payload = await browse_api.update_node(
        path="agent/new_note",
        domain="core",
        session_id=None,
        body=browse_api.NodeUpdate(
            content="update payload",
            priority=5,
            disclosure="team",
        ),
    )
    delete_payload = await browse_api.delete_node(
        path="agent/new_note",
        domain="core",
        session_id=None,
    )

    assert create_payload["ok"] is True
    assert create_payload["index_queued"] == 1
    assert update_payload["ok"] is True
    assert update_payload["index_queued"] == 1
    assert delete_payload["ok"] is True
    assert delete_payload["deleted"] is True

    assert lane_calls == [
        {"session_id": "browse.dashboard", "operation": "browse.create_node"},
        {"session_id": "browse.dashboard", "operation": "browse.update_node"},
        {"session_id": "browse.dashboard", "operation": "browse.delete_node"},
    ]
    assert snapshot_calls == [
        ("create", "core://agent/new_note", 11),
        ("content", "core://agent/new_note", None),
        ("meta", "core://agent/new_note", None),
        ("delete", "core://agent/new_note", None),
    ]
    assert [item["reason"] for item in index_enqueue_calls] == [
        "browse.create_node",
        "browse.update_node",
    ]
    assert [item["source"] for item in session_hits] == [
        "browse.create_node",
        "browse.update_node",
        "browse.delete_node",
    ]
    assert [item["message"] for item in flush_events] == [
        "create core://agent/new_note",
        "update core://agent/new_note",
        "delete core://agent/new_note",
    ]
    assert [item["reason"] for item in auto_flush_calls] == [
        "browse.create_node",
        "browse.update_node",
        "browse.delete_node",
    ]


@pytest.mark.asyncio
async def test_browse_create_returns_stable_timeout_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeBrowseClient()

    async def _timeout_write_lane(*args, **kwargs):
        _ = args
        _ = kwargs
        raise TimeoutError("write lane global acquire timed out after 1s")

    monkeypatch.setattr(browse_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(browse_api, "_run_write_lane", _timeout_write_lane)
    async def _no_defer() -> bool:
        return False

    monkeypatch.setattr(browse_api, "_should_defer_index_on_write", _no_defer)

    payload = await browse_api.create_node(
        browse_api.NodeCreate(
            parent_path="agent",
            title="new_note",
            content="create payload",
            priority=1,
            domain="core",
        ),
        session_id=None,
    )

    assert payload["ok"] is False
    assert payload["success"] is False
    assert payload["created"] is False
    assert payload["reason"] == "write_lane_timeout"
    assert payload["retryable"] is True
    assert payload["timeout_seconds"] == pytest.approx(1.0)
    assert payload["uri"] == "core://agent/new_note"
