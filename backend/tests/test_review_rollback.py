import builtins
import errno
import importlib.util
import json
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
from filelock import FileLock
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import review as review_api
from db import snapshot as snapshot_mod
from db.snapshot import SnapshotManager
from db.sqlite_client import SQLiteClient


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


def _assert_snapshot_summary(
    snapshot: dict,
    *,
    resource_id: str,
    resource_type: str,
    operation_type: str,
    uri: str | None,
) -> None:
    assert snapshot["resource_id"] == resource_id
    assert snapshot["resource_type"] == resource_type
    assert snapshot["operation_type"] == operation_type
    assert snapshot["uri"] == uri
    assert isinstance(snapshot["snapshot_time"], str)
    assert snapshot["file_bytes"] > 0
    assert snapshot["age_days"] >= 0


@pytest.mark.asyncio
async def test_rollback_path_create_cascades_descendants_and_cleans_orphans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-create-cascade.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    root = await client.create_memory(
        parent_path="",
        content="root content",
        priority=1,
        title="parent",
        domain="core",
    )
    child = await client.create_memory(
        parent_path="parent",
        content="child content",
        priority=1,
        title="child",
        domain="core",
    )
    grandchild = await client.create_memory(
        parent_path="parent/child",
        content="grandchild content",
        priority=1,
        title="grand",
        domain="core",
    )

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: client)

    payload = await review_api._rollback_path(
        {
            "operation_type": "create",
            "domain": "core",
            "path": "parent",
            "uri": "core://parent",
            "memory_id": root["id"],
        }
    )

    assert payload["deleted"] is True
    assert payload["descendants_deleted"] == 2
    assert payload["orphan_memories_deleted"] >= 2

    assert await client.get_memory_by_path("parent", "core") is None
    assert await client.get_memory_by_path("parent/child", "core") is None
    assert await client.get_memory_by_path("parent/child/grand", "core") is None

    assert await client.get_memory_by_id(root["id"]) is None
    assert await client.get_memory_by_id(child["id"]) is None
    assert await client.get_memory_by_id(grandchild["id"]) is None

    await client.close()


@pytest.mark.asyncio
async def test_rollback_path_create_cascades_descendants_under_alias_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-create-alias-cascade.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    root = await client.create_memory(
        parent_path="",
        content="root content",
        priority=1,
        title="parent",
        domain="core",
    )
    await client.add_path(
        new_path="aliasparent",
        target_path="parent",
        new_domain="writer",
        target_domain="core",
    )
    alias_child = await client.create_memory(
        parent_path="aliasparent",
        content="alias child content",
        priority=1,
        title="child",
        domain="writer",
    )

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: client)

    payload = await review_api._rollback_path(
        {
            "operation_type": "create",
            "domain": "core",
            "path": "parent",
            "uri": "core://parent",
            "memory_id": root["id"],
        }
    )

    assert payload["deleted"] is True
    assert payload["descendants_deleted"] >= 1
    assert await client.get_memory_by_path("parent", "core", reinforce_access=False) is None
    assert await client.get_memory_by_path(
        "aliasparent", "writer", reinforce_access=False
    ) is None
    assert await client.get_memory_by_path(
        "aliasparent/child", "writer", reinforce_access=False
    ) is None
    assert await client.get_memory_by_id(alias_child["id"]) is None

    await client.close()


@pytest.mark.asyncio
async def test_rollback_path_delete_rejects_restore_when_parent_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-delete-missing-parent.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    await client.create_memory(
        parent_path="",
        content="root content",
        priority=1,
        title="parent",
        domain="core",
    )
    child = await client.create_memory(
        parent_path="parent",
        content="child content",
        priority=1,
        title="child",
        domain="core",
    )

    await client.remove_path("parent/child", "core")
    await client.remove_path("parent", "core")

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: client)

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_path(
            {
                "operation_type": "delete",
                "domain": "core",
                "path": "parent/child",
                "uri": "core://parent/child",
                "memory_id": child["id"],
                "priority": 1,
                "disclosure": None,
            }
        )

    assert exc_info.value.status_code == 409
    assert "Parent path 'core://parent' not found" in str(exc_info.value.detail)
    assert (
        await client.get_memory_by_path("parent/child", "core", reinforce_access=False)
        is None
    )
    await client.close()


@pytest.mark.asyncio
async def test_rollback_path_delete_restores_path_successfully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restore_calls = []

    class _RestorePathClient:
        async def get_memory_version(self, memory_id: int):
            return {"id": int(memory_id), "content": "restorable"}

        async def restore_path(
            self,
            *,
            path: str,
            domain: str,
            memory_id: int,
            priority: int,
            disclosure: str | None,
        ):
            restore_calls.append((path, domain, int(memory_id), priority, disclosure))
            return {"restored": True}

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _RestorePathClient())

    payload = await review_api._rollback_path(
        {
            "operation_type": "delete",
            "domain": "core",
            "path": "parent/child",
            "uri": "core://parent/child",
            "memory_id": 7,
            "priority": 3,
            "disclosure": "team-only",
        }
    )

    assert payload == {"restored": True, "new_version": 7}
    assert restore_calls == [("parent/child", "core", 7, 3, "team-only")]


@pytest.mark.asyncio
async def test_restore_path_clears_migration_pointer_and_reindexes_content(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "restore-path-reindex.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    original = await client.create_memory(
        parent_path="",
        content="version 1 unique payload",
        priority=1,
        title="agent",
        domain="core",
    )
    updated = await client.update_memory(
        path="agent",
        content="version 2 current payload",
        domain="core",
        expected_old_id=original["id"],
    )

    await client.restore_path(
        path="restored-agent",
        domain="core",
        memory_id=original["id"],
        priority=1,
        disclosure=None,
    )

    restored_memory = await client.get_memory_by_id(original["id"])
    assert restored_memory is not None
    assert restored_memory["deprecated"] is False
    assert restored_memory["migrated_to"] is None

    payload = await client.search_advanced(
        query="version 1 unique payload",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    uris = [item["uri"] for item in payload["results"]]
    assert "core://restored-agent" in uris
    assert updated["new_memory_id"] != original["id"]

    await client.close()


@pytest.mark.asyncio
async def test_rollback_path_delete_returns_410_when_target_version_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingDeletedVersionClient:
        async def get_memory_version(self, memory_id: int):
            _ = memory_id
            return None

    monkeypatch.setattr(
        review_api, "get_sqlite_client", lambda: _MissingDeletedVersionClient()
    )

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_path(
            {
                "operation_type": "delete",
                "domain": "core",
                "path": "parent/child",
                "uri": "core://parent/child",
                "memory_id": 7,
                "priority": 3,
                "disclosure": "team-only",
            }
        )

    assert exc_info.value.status_code == 410
    assert "memory_id=7" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_rollback_path_create_alias_returns_409_when_alias_still_exists_after_remove_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AliasStillExistsClient:
        async def remove_path(self, path: str, domain: str) -> None:
            _ = path
            _ = domain
            raise ValueError("alias_remove_failed")

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path
            _ = domain
            _ = reinforce_access
            return {"id": 42}

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _AliasStillExistsClient())

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_path(
            {
                "operation_type": "create_alias",
                "domain": "core",
                "path": "parent-alias",
                "uri": "core://parent-alias",
            }
        )

    assert exc_info.value.status_code == 409
    assert "Cannot rollback alias 'core://parent-alias'" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_rollback_path_create_alias_returns_no_change_when_alias_already_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _AliasAlreadyRemovedClient:
        async def remove_path(self, path: str, domain: str) -> None:
            _ = path, domain
            raise ValueError("path not found")

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return None

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _AliasAlreadyRemovedClient())

    payload = await review_api._rollback_path(
        {
            "operation_type": "create_alias",
            "domain": "core",
            "path": "parent-alias",
            "uri": "core://parent-alias",
        }
    )

    assert payload == {"deleted": True, "alias_removed": False, "no_change": True}


class _StubSnapshotManager:
    def get_snapshot(self, _session_id: str, resource_id: str):
        return {
            "resource_id": resource_id,
            "resource_type": "path",
            "snapshot_time": "2026-02-19T00:00:00",
            "data": {
                "operation_type": "create",
                "domain": "core",
                "path": resource_id,
                "uri": f"core://{resource_id}",
            },
        }


class _SnapshotManagerWithPayload:
    def __init__(self, snapshot: dict | None) -> None:
        self._snapshot = snapshot

    def get_snapshot(self, _session_id: str, _resource_id: str):
        return self._snapshot


def test_rollback_endpoint_returns_5xx_when_internal_error_occurs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(_data: dict, **_kwargs) -> dict:
        raise RuntimeError("boom-secret-detail")

    monkeypatch.setattr(review_api, "get_snapshot_manager", lambda: _StubSnapshotManager())
    monkeypatch.setattr(review_api, "_rollback_path", _boom)
    monkeypatch.setenv("MCP_API_KEY", "review-test-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)

    app = FastAPI()
    app.include_router(review_api.router)

    with TestClient(app) as client:
        response = client.post(
            "/review/sessions/s1/rollback/parent",
            json={},
            headers={"X-MCP-API-Key": "review-test-secret"},
        )

    assert response.status_code == 500
    assert response.json().get("detail") == {
        "error": "rollback_failed",
        "reason": "internal_error",
        "operation": "rollback_resource",
    }


def test_rollback_endpoint_returns_success_for_path_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _rollback_path_success(_data: dict, **_kwargs) -> dict:
        return {"deleted": True, "alias_removed": True}

    monkeypatch.setattr(
        review_api,
        "get_snapshot_manager",
        lambda: _SnapshotManagerWithPayload(
            {
                "resource_id": "alias-node",
                "resource_type": "path",
                "snapshot_time": "2026-02-19T00:00:00",
                "data": {
                    "operation_type": "create_alias",
                    "domain": "core",
                    "path": "alias-node",
                    "uri": "core://alias-node",
                },
            }
        ),
    )
    monkeypatch.setattr(review_api, "_rollback_path", _rollback_path_success)
    monkeypatch.setenv("MCP_API_KEY", "review-test-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)

    app = FastAPI()
    app.include_router(review_api.router)

    with TestClient(app) as client:
        response = client.post(
            "/review/sessions/s1/rollback/alias-node",
            json={},
            headers={"X-MCP-API-Key": "review-test-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "resource_id": "alias-node",
        "resource_type": "path",
        "success": True,
        "message": "Removed alias 'alias-node'.",
        "new_version": None,
    }


def test_rollback_endpoint_preserves_helper_http_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _rollback_path_conflict(_data: dict, **_kwargs) -> dict:
        raise HTTPException(status_code=409, detail="path conflict")

    monkeypatch.setattr(review_api, "get_snapshot_manager", lambda: _StubSnapshotManager())
    monkeypatch.setattr(review_api, "_rollback_path", _rollback_path_conflict)
    monkeypatch.setenv("MCP_API_KEY", "review-test-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)

    app = FastAPI()
    app.include_router(review_api.router)

    with TestClient(app) as client:
        response = client.post(
            "/review/sessions/s1/rollback/parent",
            json={},
            headers={"X-MCP-API-Key": "review-test-secret"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "path conflict"


def test_rollback_endpoint_rejects_unknown_memory_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        review_api,
        "get_snapshot_manager",
        lambda: _SnapshotManagerWithPayload(
            {
                "resource_id": "memory:weird",
                "resource_type": "memory",
                "snapshot_time": "2026-02-19T00:00:00",
                "data": {
                    "operation_type": "weird",
                    "domain": "core",
                    "path": "node",
                    "uri": "core://node",
                },
            }
        ),
    )
    monkeypatch.setenv("MCP_API_KEY", "review-test-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)

    app = FastAPI()
    app.include_router(review_api.router)

    with TestClient(app) as client:
        response = client.post(
            "/review/sessions/s1/rollback/node",
            json={},
            headers={"X-MCP-API-Key": "review-test-secret"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unknown memory operation: weird"


def test_list_deprecated_endpoint_hides_internal_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingClient:
        async def get_deprecated_memories(self):
            raise RuntimeError("deprecated-secret-detail")

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _FailingClient())
    monkeypatch.setenv("MCP_API_KEY", "review-test-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)

    app = FastAPI()
    app.include_router(review_api.router)

    with TestClient(app) as client:
        response = client.get(
            "/review/deprecated",
            headers={"X-MCP-API-Key": "review-test-secret"},
        )

    assert response.status_code == 500
    assert response.json().get("detail") == {
        "error": "list_deprecated_failed",
        "reason": "internal_error",
        "operation": "list_deprecated_memories",
    }


def test_compare_text_hides_internal_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(_text_a: str, _text_b: str):
        raise RuntimeError("diff-secret-detail")

    monkeypatch.setattr(review_api, "get_text_diff", _boom)
    monkeypatch.setenv("MCP_API_KEY", "review-test-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)

    app = FastAPI()
    app.include_router(review_api.router)

    with TestClient(app) as client:
        response = client.post(
            "/review/diff",
            json={"text_a": "old", "text_b": "new"},
            headers={"X-MCP-API-Key": "review-test-secret"},
        )

    assert response.status_code == 500
    assert response.json().get("detail") == {
        "error": "compare_text_failed",
        "reason": "internal_error",
        "operation": "compare_text",
    }


def test_review_diff_endpoint_works_without_diff_match_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = Path(review_api.__file__).with_name("utils.py")
    original_import = builtins.__import__

    def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "diff_match_patch":
            raise ModuleNotFoundError("No module named 'diff_match_patch'")
        return original_import(name, globals, locals, fromlist, level)

    spec = importlib.util.spec_from_file_location("review_utils_without_dmp", module_path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    builtins.__import__ = _guarded_import
    try:
        spec.loader.exec_module(module)
    finally:
        builtins.__import__ = original_import

    monkeypatch.setattr(review_api, "get_text_diff", module.get_text_diff)
    monkeypatch.setenv("MCP_API_KEY", "review-test-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)

    app = FastAPI()
    app.include_router(review_api.router)

    with TestClient(app) as client:
        response = client.post(
            "/review/diff",
            json={"text_a": "old line\n", "text_b": "new line\n"},
            headers={"X-MCP-API-Key": "review-test-secret"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert "<table class=\"diff\"" in payload["diff_html"]
    assert "--- old_version" in payload["diff_unified"]
    assert "+++ new_version" in payload["diff_unified"]
    assert "新增" in payload["summary"]


def test_diff_endpoint_rejects_invalid_session_id_with_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_API_KEY", "review-test-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)

    app = FastAPI()
    app.include_router(review_api.router)

    with TestClient(app) as client:
        response = client.get(
            "/review/sessions/abc%5Cdef/diff/core%3A%2F%2Fmemory-palace",
            headers={"X-MCP-API-Key": "review-test-secret"},
        )

    assert response.status_code == 400
    assert "Invalid session_id" in str(response.json().get("detail"))


def test_build_review_internal_session_id_namespaces_internal_keys() -> None:
    assert review_api._build_review_internal_session_id("rollback", "session-123") == (
        "review.internal.rollback:session-123"
    )
    assert review_api._build_review_internal_session_id("delete_memory") == (
        "review.internal.delete_memory"
    )
    assert review_api._build_review_internal_session_id(
        "rollback",
        "review.internal.rollback:session-123",
    ) == "review.internal.rollback:session-123"


@pytest.mark.asyncio
async def test_rollback_resource_uses_namespaced_internal_lane_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, str] = {}

    class _StubSnapshotManager:
        def get_snapshot(self, session_id: str, _resource_id: str):
            observed["external_session_id"] = session_id
            return {
                "resource_id": "core://memory-palace",
                "resource_type": "path",
                "snapshot_time": "2026-02-19T00:00:00",
                "data": {
                    "operation_type": "create_alias",
                    "domain": "core",
                    "path": "memory-palace",
                    "uri": "core://memory-palace",
                },
            }

    async def _rollback_path_stub(data: dict, *, lane_session_id: str | None = None) -> dict:
        _ = data
        observed["lane_session_id"] = str(lane_session_id or "")
        return {"deleted": True}

    monkeypatch.setattr(review_api, "get_snapshot_manager", lambda: _StubSnapshotManager())
    monkeypatch.setattr(review_api, "_rollback_path", _rollback_path_stub)

    payload = await review_api.rollback_resource(
        "user-session",
        "core://memory-palace",
        review_api.RollbackRequest(reason="test"),
    )

    assert payload.success is True
    assert observed["external_session_id"] == "user-session"
    assert observed["lane_session_id"] == "review.internal.rollback:user-session"


def test_snapshot_manager_rejects_traversal_session_id(tmp_path: Path) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    with pytest.raises(ValueError):
        manager.clear_session("..")
    with pytest.raises(ValueError):
        manager.clear_session("bad:name")
    with pytest.raises(ValueError):
        manager.clear_session("safe\u200bsession")


def test_get_snapshot_manager_is_thread_safe_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_factory = snapshot_mod.SnapshotManager
    original_instance = snapshot_mod._snapshot_manager
    created_instances: list[object] = []
    barrier = threading.Barrier(8)
    release_constructor = threading.Event()
    results: list[object] = []
    errors: list[Exception] = []

    class _StubSnapshotManager:
        def __init__(self) -> None:
            created_instances.append(object())
            release_constructor.wait(timeout=1)

    monkeypatch.setattr(snapshot_mod, "SnapshotManager", _StubSnapshotManager)
    monkeypatch.setattr(snapshot_mod, "_snapshot_manager", None)

    def _worker() -> None:
        try:
            barrier.wait(timeout=5)
            results.append(snapshot_mod.get_snapshot_manager())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    release_constructor.set()
    for thread in threads:
        thread.join()

    snapshot_mod.SnapshotManager = original_factory
    snapshot_mod._snapshot_manager = original_instance

    assert errors == []
    assert len(created_instances) == 1
    assert len({id(item) for item in results}) == 1


def test_snapshot_manager_concurrent_create_snapshot_preserves_all_resources(
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "concurrent-session"
    resource_ids = [f"core://node/{index}" for index in range(12)]
    barrier = threading.Barrier(len(resource_ids))
    errors: list[Exception] = []

    def _worker(resource_id: str) -> None:
        try:
            barrier.wait(timeout=5)
            created = manager.create_snapshot(
                session_id=session_id,
                resource_id=resource_id,
                resource_type="path",
                snapshot_data={
                    "operation_type": "create",
                    "uri": resource_id,
                },
            )
            assert created is True
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(resource_id,)) for resource_id in resource_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    snapshots = manager.list_snapshots(session_id)
    assert len(snapshots) == len(resource_ids)
    assert {item["resource_id"] for item in snapshots} == set(resource_ids)
    for resource_id in resource_ids:
        snapshot = manager.get_snapshot(session_id, resource_id)
        assert snapshot is not None
        assert snapshot["resource_id"] == resource_id


def test_snapshot_manager_uses_utc_z_timestamps(tmp_path: Path) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "utc-z"
    resource_id = "core://agent/index"

    assert manager.create_snapshot(
        session_id=session_id,
        resource_id=resource_id,
        resource_type="memory",
        snapshot_data={"operation_type": "modify", "uri": resource_id},
    )

    manifest = json.loads(Path(manager._get_manifest_path(session_id)).read_text(encoding="utf-8"))
    snapshot = manager.get_snapshot(session_id, resource_id)

    assert manifest["created_at"].endswith("Z")
    assert snapshot is not None
    assert str(snapshot["snapshot_time"]).endswith("Z")


def test_snapshot_manager_atomic_manifest_write_preserves_previous_manifest_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "atomic-session"

    assert manager.create_snapshot(
        session_id=session_id,
        resource_id="core://stable",
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": "core://stable"},
    )

    manifest_path = Path(manager._get_manifest_path(session_id))
    before_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_replace = snapshot_mod.os.replace

    def _boom_replace(src: str, dst: str) -> None:
        if dst == str(manifest_path):
            raise RuntimeError("replace failed")
        original_replace(src, dst)

    monkeypatch.setattr(snapshot_mod.os, "replace", _boom_replace)

    broken_manifest = {
        "session_id": session_id,
        "created_at": "broken",
        "resources": {"core://broken": {"resource_type": "path"}},
    }
    with pytest.raises(RuntimeError, match="replace failed"):
        manager._save_manifest(session_id, broken_manifest)

    after_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert after_payload == before_payload
    assert list(manifest_path.parent.glob(".tmp-*.json")) == []


def test_snapshot_manager_atomic_write_retries_windows_replace_conflict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    target_path = tmp_path / "snapshots" / "manifest.json"
    attempts = {"count": 0}
    sleep_calls: list[float] = []
    original_replace = snapshot_mod.os.replace

    def _flaky_replace(src: str, dst: str) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1 and dst == str(target_path):
            err = PermissionError("sharing violation")
            err.winerror = 32
            raise err
        original_replace(src, dst)

    monkeypatch.setattr(snapshot_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(snapshot_mod.os, "replace", _flaky_replace)
    monkeypatch.setattr(snapshot_mod.time, "sleep", lambda delay: sleep_calls.append(delay))

    manager._atomic_write_json(
        str(target_path),
        {"session_id": "retry", "resources": {}},
    )

    assert attempts["count"] == 2
    assert sleep_calls == [0.05]
    assert json.loads(target_path.read_text(encoding="utf-8"))["session_id"] == "retry"


def test_snapshot_manager_recovers_missing_manifest_from_resource_files(
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "recover-session"
    resource_id = "core://stable"

    assert manager.create_snapshot(
        session_id=session_id,
        resource_id=resource_id,
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": resource_id},
    )

    manifest_path = Path(manager._get_manifest_path(session_id))
    manifest_path.unlink()

    snapshots = manager.list_snapshots(session_id)

    assert len(snapshots) == 1
    _assert_snapshot_summary(
        snapshots[0],
        resource_id=resource_id,
        resource_type="path",
        operation_type="create",
        uri=resource_id,
    )
    recreated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert resource_id in recreated_manifest["resources"]


def test_snapshot_manager_reads_legacy_snapshot_filenames(
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "legacy-session"
    resource_id = "core://legacy/path"

    resources_dir = Path(manager._get_resources_dir(session_id))
    resources_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = resources_dir / f"{manager._legacy_sanitize_resource_id(resource_id)}.json"
    manager._atomic_write_json(
        str(legacy_path),
        {
            "resource_id": resource_id,
            "resource_type": "path",
            "snapshot_time": "2026-03-20T00:00:00",
            "data": {"operation_type": "create", "uri": resource_id},
        },
    )

    assert manager.has_snapshot(session_id, resource_id) is True
    snapshot = manager.get_snapshot(session_id, resource_id)

    assert snapshot is not None
    assert snapshot["resource_id"] == resource_id
    assert any(item["resource_id"] == resource_id for item in manager.list_snapshots(session_id))


def test_snapshot_manager_reads_previous_long_hash_snapshot_filenames(
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "compat-session"
    resource_id = "core://" + ("very/long/path/" * 12) + "memory"

    resources_dir = Path(manager._get_resources_dir(session_id))
    resources_dir.mkdir(parents=True, exist_ok=True)
    compat_path = (
        resources_dir / f"{manager._compat_sanitize_resource_id(resource_id)}.json"
    )
    manager._atomic_write_json(
        str(compat_path),
        {
            "resource_id": resource_id,
            "resource_type": "path",
            "snapshot_time": "2026-03-20T00:00:00",
            "data": {"operation_type": "create", "uri": resource_id},
        },
    )

    assert manager.has_snapshot(session_id, resource_id) is True
    snapshot = manager.get_snapshot(session_id, resource_id)

    assert snapshot is not None
    assert snapshot["resource_id"] == resource_id
    assert len(manager._sanitize_resource_id(resource_id)) < len(
        manager._compat_sanitize_resource_id(resource_id)
    )


def test_snapshot_manager_remove_tree_uses_supported_callback_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, object] = {}

    def _fake_rmtree(path: str, **kwargs) -> None:
        captured_kwargs.update(kwargs)

    monkeypatch.setattr(snapshot_mod.shutil, "rmtree", _fake_rmtree)

    snapshot_mod._remove_tree("/tmp/demo")

    callback_key = "onexc" if sys.version_info >= (3, 12) else "onerror"
    assert callback_key in captured_kwargs
    assert callable(captured_kwargs[callback_key])


def test_snapshot_manager_recovers_manifest_entries_from_snapshot_files(
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "recover-session"
    resource_id = "core://recover-me"

    assert manager.create_snapshot(
        session_id=session_id,
        resource_id=resource_id,
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": resource_id},
    )

    manifest_path = Path(manager._get_manifest_path(session_id))
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "created_at": "broken",
                "resources": {},
            }
        ),
        encoding="utf-8",
    )

    snapshots = manager.list_snapshots(session_id)

    assert len(snapshots) == 1
    _assert_snapshot_summary(
        snapshots[0],
        resource_id=resource_id,
        resource_type="path",
        operation_type="create",
        uri=resource_id,
    )
    repaired_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert resource_id in repaired_manifest["resources"]


def test_handle_remove_readonly_accepts_onexc_exception_instance(tmp_path: Path) -> None:
    target = tmp_path / "readonly.txt"
    target.write_text("content", encoding="utf-8")
    target.chmod(stat.S_IREAD)

    snapshot_mod._handle_remove_readonly_onexc(
        snapshot_mod.os.remove,
        str(target),
        PermissionError("blocked"),
    )

    assert not target.exists()


def test_force_remove_uses_onexc_callback_on_python_312_plus(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    target = tmp_path / "readonly-dir"
    target.mkdir()
    observed: dict[str, object] = {}

    def _fake_rmtree(path: str, **kwargs: object) -> None:
        observed["path"] = path
        observed["kwargs"] = kwargs

    monkeypatch.setattr(snapshot_mod.shutil, "rmtree", _fake_rmtree)
    monkeypatch.setattr(snapshot_mod.sys, "version_info", (3, 12, 0))

    snapshot_mod._force_remove(str(target))

    assert observed["path"] == str(target)
    kwargs = observed["kwargs"]
    assert isinstance(kwargs, dict)
    assert "onexc" in kwargs
    assert "onerror" not in kwargs


def test_snapshot_manager_keeps_windows_snapshot_paths_within_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / ("very-long-root-" * 6) / "snapshots"))
    resource_id = "core://" + "/".join(["nested-node"] * 24)

    monkeypatch.setattr(snapshot_mod.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        manager,
        "_get_resources_dir",
        lambda _session_id: "C:/Users/demo/AppData/Local/OpenClaw/memory-palace/session-demo/resources",
    )

    snapshot_path = manager._get_snapshot_path("session-demo", resource_id)

    assert len(snapshot_path) <= 240
    assert snapshot_path.endswith(".json")
    assert "_compat" not in snapshot_path


def test_snapshot_manager_recovers_orphaned_snapshot_into_manifest(tmp_path: Path) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "orphaned-session"
    resource_id = "memory:41"
    snapshot_path = Path(manager._get_snapshot_path(session_id, resource_id))
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_payload = {
        "resource_id": resource_id,
        "resource_type": "memory",
        "snapshot_time": "2026-03-20T10:00:00",
        "data": {
            "operation_type": "modify_content",
            "uri": "core://same-uri",
        },
    }
    snapshot_path.write_text(json.dumps(snapshot_payload), encoding="utf-8")

    manifest = manager._load_manifest(session_id)

    assert manifest["resources"][resource_id]["file"] == snapshot_path.name
    assert manager.find_memory_snapshot_by_uri(session_id, "core://same-uri") == resource_id
    snapshots = manager.list_snapshots(session_id)
    assert len(snapshots) == 1
    _assert_snapshot_summary(
        snapshots[0],
        resource_id=resource_id,
        resource_type="memory",
        operation_type="modify_content",
        uri="core://same-uri",
    )
    assert snapshots[0]["snapshot_time"] == "2026-03-20T10:00:00"


def test_snapshot_manager_concurrent_same_resource_dedupes_to_single_snapshot(
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "dedupe-session"
    resource_id = "core://same-node"
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            barrier.wait(timeout=5)
            created = manager.create_snapshot(
                session_id=session_id,
                resource_id=resource_id,
                resource_type="path",
                snapshot_data={
                    "operation_type": "create",
                    "uri": resource_id,
                },
            )
            results.append(created)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(results) == [False, True]
    snapshots = manager.list_snapshots(session_id)
    assert len(snapshots) == 1
    _assert_snapshot_summary(
        snapshots[0],
        resource_id=resource_id,
        resource_type="path",
        operation_type="create",
        uri=resource_id,
    )


def test_snapshot_manager_atomic_resource_write_preserves_previous_snapshot_on_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "resource-atomic-session"
    resource_id = "core://stable"

    assert manager.create_snapshot(
        session_id=session_id,
        resource_id=resource_id,
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": resource_id},
    )

    snapshot_path = Path(manager._get_snapshot_path(session_id, resource_id))
    before_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    original_replace = snapshot_mod.os.replace

    def _boom_replace(src: str, dst: str) -> None:
        if dst == str(snapshot_path):
            raise RuntimeError("replace failed")
        original_replace(src, dst)

    monkeypatch.setattr(snapshot_mod.os, "replace", _boom_replace)

    with pytest.raises(RuntimeError, match="replace failed"):
        manager.create_snapshot(
            session_id=session_id,
            resource_id=resource_id,
            resource_type="path",
            snapshot_data={"operation_type": "delete", "uri": resource_id},
            force=True,
        )

    after_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert after_payload == before_payload
    assert list(snapshot_path.parent.glob(".tmp-*.json")) == []


def test_snapshot_manager_force_snapshot_restores_previous_snapshot_if_manifest_write_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "force-atomic-session"
    resource_id = "core://stable"

    assert manager.create_snapshot(
        session_id=session_id,
        resource_id=resource_id,
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": resource_id},
    )

    snapshot_path = Path(manager._get_snapshot_path(session_id, resource_id))
    manifest_path = Path(manager._get_manifest_path(session_id))
    before_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    before_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_replace = snapshot_mod.os.replace

    def _boom_replace(src: str, dst: str) -> None:
        if dst == str(manifest_path):
            raise RuntimeError("manifest replace failed")
        original_replace(src, dst)

    monkeypatch.setattr(snapshot_mod.os, "replace", _boom_replace)

    with pytest.raises(RuntimeError, match="manifest replace failed"):
        manager.create_snapshot(
            session_id=session_id,
            resource_id=resource_id,
            resource_type="path",
            snapshot_data={"operation_type": "delete", "uri": resource_id},
            force=True,
        )

    after_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    after_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert after_snapshot == before_snapshot
    assert after_manifest == before_manifest


def test_snapshot_manager_concurrent_same_uri_memory_snapshot_dedupes(
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "memory-uri-dedupe-session"
    barrier = threading.Barrier(2)
    results: list[bool] = []
    errors: list[Exception] = []

    def _worker(resource_id: str) -> None:
        try:
            barrier.wait(timeout=5)
            created = manager.create_snapshot(
                session_id=session_id,
                resource_id=resource_id,
                resource_type="memory",
                snapshot_data={
                    "operation_type": "modify_content",
                    "uri": "core://same-uri",
                    "memory_id": resource_id.split(":")[-1],
                },
            )
            results.append(created)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=_worker, args=("memory:1",)),
        threading.Thread(target=_worker, args=("memory:2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(results) == [False, True]
    snapshots = manager.list_snapshots(session_id)
    assert len(snapshots) == 1
    assert snapshots[0]["resource_type"] == "memory"
    assert snapshots[0]["uri"] == "core://same-uri"


def test_snapshot_manager_list_sessions_skips_locked_session_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SNAPSHOT_LIST_LOCK_TIMEOUT_SEC", "0")
    manager = SnapshotManager(str(tmp_path / "snapshots"))

    assert manager.create_snapshot(
        session_id="free-session",
        resource_id="core://free",
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": "core://free"},
    )
    assert manager.create_snapshot(
        session_id="locked-session",
        resource_id="core://locked",
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": "core://locked"},
    )

    external_lock = FileLock(manager._get_session_lock_path("locked-session"), timeout=0)
    external_lock.acquire()
    try:
        started = time.monotonic()
        sessions = manager.list_sessions()
        elapsed = time.monotonic() - started
    finally:
        external_lock.release()

    session_ids = {item["session_id"] for item in sessions}
    assert "free-session" in session_ids
    assert "locked-session" not in session_ids
    assert elapsed < 0.5


def test_snapshot_manager_clear_session_removes_residual_lock_file(
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "clear-lock-session"

    assert manager.create_snapshot(
        session_id=session_id,
        resource_id="core://node",
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": "core://node"},
    )
    lock_path = Path(manager._get_session_lock_path(session_id))

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("stale-lock", encoding="utf-8")

    assert lock_path.exists()
    assert manager.clear_session(session_id) == 1
    assert not lock_path.exists()


def test_snapshot_manager_storage_summary_reports_threshold_warnings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SNAPSHOT_WARN_MAX_SESSIONS", "1")
    monkeypatch.setenv("SNAPSHOT_WARN_MAX_RESOURCES_PER_SESSION", "1")
    manager = SnapshotManager(str(tmp_path / "snapshots"))

    assert manager.create_snapshot(
        session_id="session-a",
        resource_id="core://a",
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": "core://a"},
    )
    assert manager.create_snapshot(
        session_id="session-a",
        resource_id="core://a-2",
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": "core://a-2"},
    )
    assert manager.create_snapshot(
        session_id="session-b",
        resource_id="core://b",
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": "core://b"},
    )

    summary = manager.storage_summary()

    assert summary["session_count"] == 2
    assert summary["total_resources"] == 3
    assert any(
        item.get("code") == "snapshot_sessions_over_warn_limit"
        for item in summary["warnings"]
    )
    assert len(summary["sessions"]) == 2
    session_a = next(item for item in summary["sessions"] if item["session_id"] == "session-a")
    assert session_a["resource_count"] == 2
    assert session_a["estimated_reclaim_bytes"] == session_a["total_bytes"]
    assert session_a["age_days"] >= 0
    assert session_a["over_warning_threshold"] is True
    assert "snapshot_resources_over_warn_limit" in session_a["warning_codes"]
    assert isinstance(session_a["oldest_snapshot_time"], str)
    assert isinstance(session_a["newest_snapshot_time"], str)
    assert summary["largest_sessions"][0]["session_id"] == "session-a"


def test_snapshot_manager_enforces_per_session_resource_limit_only_when_opted_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SNAPSHOT_ENFORCE_MAX_RESOURCES_PER_SESSION", "1")
    manager = SnapshotManager(str(tmp_path / "snapshots"))

    assert manager.create_snapshot(
        session_id="session-a",
        resource_id="core://a",
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": "core://a"},
    )

    with pytest.raises(RuntimeError, match="resource limit exceeded"):
        manager.create_snapshot(
            session_id="session-a",
            resource_id="core://b",
            resource_type="path",
            snapshot_data={"operation_type": "create", "uri": "core://b"},
        )


def test_review_storage_endpoint_returns_snapshot_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StorageManager:
        def storage_summary(self):
            return {
                "session_count": 2,
                "total_resources": 5,
                "total_bytes": 4096,
                "sessions": [
                    {
                        "session_id": "session-a",
                        "created_at": "2026-03-20T10:00:00Z",
                        "resource_count": 3,
                        "total_bytes": 3072,
                        "oldest_snapshot_time": "2026-03-20T10:00:00Z",
                        "newest_snapshot_time": "2026-03-21T10:00:00Z",
                        "age_days": 5,
                        "estimated_reclaim_bytes": 3072,
                        "warning_codes": ["snapshot_session_bytes_over_warn_limit"],
                        "over_warning_threshold": True,
                    }
                ],
                "warnings": [],
            }

    monkeypatch.setattr(review_api, "get_snapshot_manager", lambda: _StorageManager())
    monkeypatch.setenv("MCP_API_KEY", "review-test-secret")
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)

    app = FastAPI()
    app.include_router(review_api.router)

    with TestClient(app) as client:
        response = client.get(
            "/review/storage",
            headers={"X-MCP-API-Key": "review-test-secret"},
        )

    assert response.status_code == 200
    assert response.json()["session_count"] == 2
    assert response.json()["total_bytes"] == 4096
    assert response.json()["sessions"][0]["estimated_reclaim_bytes"] == 3072
    assert response.json()["sessions"][0]["warning_codes"] == [
        "snapshot_session_bytes_over_warn_limit"
    ]


def test_snapshot_manager_force_snapshot_keeps_previous_payload_when_disk_full(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = SnapshotManager(str(tmp_path / "snapshots"))
    session_id = "disk-full-session"
    resource_id = "core://stable"

    assert manager.create_snapshot(
        session_id=session_id,
        resource_id=resource_id,
        resource_type="path",
        snapshot_data={"operation_type": "create", "uri": resource_id},
    )

    snapshot_path = Path(manager._get_snapshot_path(session_id, resource_id))
    before_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))

    def _disk_full(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(snapshot_mod.json, "dump", _disk_full)

    with pytest.raises(OSError, match="No space left on device"):
        manager.create_snapshot(
            session_id=session_id,
            resource_id=resource_id,
            resource_type="path",
            snapshot_data={"operation_type": "delete", "uri": resource_id},
            force=True,
        )

    after_payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert after_payload == before_payload
    assert list(snapshot_path.parent.glob(".tmp-*.json")) == []


@pytest.mark.asyncio
async def test_rollback_path_create_alias_routes_writes_through_write_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane_calls = []

    class _AliasClient:
        async def remove_path(self, path: str, domain: str):
            return {"removed_uri": f"{domain}://{path}"}

    async def _run_write_lane_stub(operation: str, task, *, session_id=None):
        lane_calls.append({"operation": operation, "session_id": session_id})
        return await task()

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _AliasClient())
    monkeypatch.setattr(review_api, "_run_write_lane", _run_write_lane_stub)

    payload = await review_api._rollback_path(
        {
            "operation_type": "create_alias",
            "domain": "core",
            "path": "alias-node",
            "uri": "core://alias-node",
        },
        lane_session_id="review.internal.rollback:lane-path",
    )

    assert payload == {"deleted": True, "alias_removed": True}
    assert lane_calls == [
        {
            "operation": "rollback.remove_alias",
            "session_id": "review.internal.rollback:lane-path",
        }
    ]


@pytest.mark.asyncio
async def test_rollback_memory_content_routes_writes_through_write_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane_calls = []

    class _MemoryContentClient:
        async def get_memory_version(self, memory_id: int):
            memory_id = int(memory_id)
            if memory_id == 123:
                return {"id": 123, "migrated_to": 999}
            if memory_id == 999:
                return {"id": 999, "migrated_to": None}
            return None

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return {"id": 999, "content": "current"}

        async def rollback_to_memory(self, path: str, memory_id: int, domain: str):
            _ = path, domain
            return {"restored_memory_id": int(memory_id)}

    async def _run_write_lane_stub(operation: str, task, *, session_id=None):
        lane_calls.append({"operation": operation, "session_id": session_id})
        return await task()

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _MemoryContentClient())
    monkeypatch.setattr(review_api, "_run_write_lane", _run_write_lane_stub)

    payload = await review_api._rollback_memory_content(
        {
            "memory_id": 123,
            "path": "agent/node",
            "domain": "core",
            "uri": "core://agent/node",
            "all_paths": [],
        },
        lane_session_id="review.internal.rollback:lane-memory",
    )

    assert payload == {"new_version": 123}
    assert lane_calls == [
        {
            "operation": "rollback.rollback_to_memory",
            "session_id": "review.internal.rollback:lane-memory",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_memory_id", [None, "not-an-int", 0])
async def test_rollback_memory_content_returns_400_for_invalid_snapshot_memory_id(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_memory_id: object,
) -> None:
    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: object())

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_memory_content(
            {
                "memory_id": snapshot_memory_id,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "all_paths": [],
            }
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Snapshot missing memory_id"


@pytest.mark.asyncio
async def test_rollback_memory_content_returns_410_when_target_version_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingMemoryVersionClient:
        async def get_memory_version(self, memory_id: int):
            _ = memory_id
            return None

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            raise AssertionError("get_memory_by_path should not run for missing snapshot")

    monkeypatch.setattr(
        review_api, "get_sqlite_client", lambda: _MissingMemoryVersionClient()
    )

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_memory_content(
            {
                "memory_id": 123,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "all_paths": [],
            }
        )

    assert exc_info.value.status_code == 410
    assert "memory_id=123" in str(exc_info.value.detail)




@pytest.mark.asyncio
async def test_rollback_memory_content_falls_back_to_snapshot_all_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups: list[tuple[str, str]] = []
    lane_calls = []
    rollback_calls = []

    class _FallbackMemoryContentClient:
        async def get_memory_version(self, memory_id: int):
            memory_id = int(memory_id)
            if memory_id == 123:
                return {"id": 123, "migrated_to": 999}
            if memory_id == 999:
                return {"id": 999, "migrated_to": None}
            return None

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = reinforce_access
            lookups.append((domain, path))
            if (domain, path) == ("core", "agent/node"):
                return None
            if (domain, path) == ("writer", "mirror/node"):
                return {"id": 999, "content": "current"}
            raise AssertionError(f"unexpected lookup: {(domain, path)}")

        async def rollback_to_memory(self, path: str, memory_id: int, domain: str):
            rollback_calls.append((domain, path, int(memory_id)))
            return {"restored_memory_id": int(memory_id)}

    async def _run_write_lane_stub(operation: str, task, *, session_id=None):
        lane_calls.append({"operation": operation, "session_id": session_id})
        return await task()

    monkeypatch.setattr(
        review_api, "get_sqlite_client", lambda: _FallbackMemoryContentClient()
    )
    monkeypatch.setattr(review_api, "_run_write_lane", _run_write_lane_stub)

    payload = await review_api._rollback_memory_content(
        {
            "memory_id": 123,
            "path": "agent/node",
            "domain": "core",
            "uri": "core://agent/node",
            "all_paths": ["core://agent/node", "writer://mirror/node"],
        },
        lane_session_id="review.internal.rollback:lane-fallback",
    )

    assert payload == {"new_version": 123}
    assert lookups == [("core", "agent/node"), ("writer", "mirror/node")]
    assert rollback_calls == [("writer", "mirror/node", 123)]
    assert lane_calls == [
        {
            "operation": "rollback.rollback_to_memory",
            "session_id": "review.internal.rollback:lane-fallback",
        }
    ]


@pytest.mark.asyncio
async def test_rollback_memory_content_returns_409_for_invalid_current_memory_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _InvalidCurrentMemoryIdClient:
        async def get_memory_version(self, memory_id: int):
            memory_id = int(memory_id)
            if memory_id == 123:
                return {"id": 123, "migrated_to": None}
            return None

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return {"id": "not-an-int", "content": "current"}

        async def rollback_to_memory(self, path: str, memory_id: int, domain: str):
            _ = path, memory_id, domain
            raise AssertionError("rollback_to_memory should not run for invalid id")

    monkeypatch.setattr(
        review_api, "get_sqlite_client", lambda: _InvalidCurrentMemoryIdClient()
    )

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_memory_content(
            {
                "memory_id": 123,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "all_paths": [],
            }
        )

    assert exc_info.value.status_code == 409
    assert "current memory_id is invalid" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_rollback_memory_content_returns_404_when_all_paths_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingPathsClient:
        async def get_memory_version(self, memory_id: int):
            return {"id": int(memory_id), "migrated_to": None}

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return None

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _MissingPathsClient())

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_memory_content(
            {
                "memory_id": 123,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "all_paths": ["writer://mirror/node", "notes://backup/node"],
            }
        )

    assert exc_info.value.status_code == 404
    assert "no alternative paths found" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_rollback_memory_content_returns_no_change_when_snapshot_matches_current_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoChangeClient:
        async def get_memory_version(self, memory_id: int):
            return {"id": int(memory_id), "migrated_to": None}

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return {"id": 123, "content": "current"}

        async def rollback_to_memory(self, path: str, memory_id: int, domain: str):
            _ = path, memory_id, domain
            raise AssertionError("rollback_to_memory should not run for no-change rollback")

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _NoChangeClient())

    payload = await review_api._rollback_memory_content(
        {
            "memory_id": 123,
            "path": "agent/node",
            "domain": "core",
            "uri": "core://agent/node",
            "all_paths": [],
        }
    )

    assert payload == {"no_change": True, "new_version": 123}


@pytest.mark.asyncio
async def test_rollback_legacy_modify_routes_combined_restore_through_single_write_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane_calls = []

    class _LegacyModifyClient:
        async def get_memory_version(self, memory_id: int):
            memory_id = int(memory_id)
            if memory_id == 123:
                return {"id": 123, "migrated_to": 999}
            if memory_id == 999:
                return {"id": 999, "migrated_to": None}
            return None

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return {"id": 999, "priority": 5, "disclosure": "new"}

        async def rollback_to_memory(
            self,
            path: str,
            memory_id: int,
            domain: str,
            *,
            restore_path_metadata: bool = False,
            restore_priority: int | None = None,
            restore_disclosure: str | None = None,
        ):
            _ = path, domain
            assert restore_path_metadata is True
            assert restore_priority == 1
            assert restore_disclosure == "old"
            return {"restored_memory_id": int(memory_id)}

        async def update_memory(self, **_: object):
            raise AssertionError("update_memory should not run for combined legacy rollback")

    async def _run_write_lane_stub(operation: str, task, *, session_id=None):
        lane_calls.append({"operation": operation, "session_id": session_id})
        return await task()

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _LegacyModifyClient())
    monkeypatch.setattr(review_api, "_run_write_lane", _run_write_lane_stub)

    payload = await review_api._rollback_legacy_modify(
        {
            "memory_id": 123,
            "path": "agent/node",
            "domain": "core",
            "uri": "core://agent/node",
            "priority": 1,
            "disclosure": "old",
        },
        lane_session_id="review.internal.rollback:legacy-combined",
    )

    assert payload == {"new_version": 123}
    assert lane_calls == [
        {
            "operation": "rollback.restore_legacy_modify",
            "session_id": "review.internal.rollback:legacy-combined",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_memory_id", [None, "not-an-int", 0])
async def test_rollback_legacy_modify_returns_400_for_invalid_snapshot_memory_id(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_memory_id: object,
) -> None:
    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: object())

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_legacy_modify(
            {
                "memory_id": snapshot_memory_id,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "priority": 1,
                "disclosure": "old",
            }
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Snapshot missing memory_id"


@pytest.mark.asyncio
async def test_rollback_legacy_modify_returns_410_when_target_version_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingLegacyVersionClient:
        async def get_memory_version(self, memory_id: int):
            _ = memory_id
            return None

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            raise AssertionError("get_memory_by_path should not run for missing snapshot")

    monkeypatch.setattr(
        review_api, "get_sqlite_client", lambda: _MissingLegacyVersionClient()
    )

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_legacy_modify(
            {
                "memory_id": 123,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "priority": 1,
                "disclosure": "old",
            }
        )

    assert exc_info.value.status_code == 410
    assert "memory_id=123" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_rollback_legacy_modify_returns_404_when_path_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingLegacyPathClient:
        async def get_memory_version(self, memory_id: int):
            return {"id": int(memory_id), "migrated_to": None}

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return None

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _MissingLegacyPathClient())

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_legacy_modify(
            {
                "memory_id": 123,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "priority": 1,
                "disclosure": "old",
            }
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "'core://agent/node' no longer exists"


@pytest.mark.asyncio
async def test_rollback_legacy_modify_returns_409_for_invalid_current_memory_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _InvalidLegacyCurrentMemoryIdClient:
        async def get_memory_version(self, memory_id: int):
            memory_id = int(memory_id)
            if memory_id == 123:
                return {"id": 123, "migrated_to": None}
            return None

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return {"id": "bad-id", "priority": 5, "disclosure": "current"}

        async def rollback_to_memory(self, path: str, memory_id: int, domain: str):
            _ = path, memory_id, domain
            raise AssertionError("rollback_to_memory should not run for invalid id")

        async def update_memory(self, **_: object):
            raise AssertionError("update_memory should not run for invalid id")

    monkeypatch.setattr(
        review_api, "get_sqlite_client", lambda: _InvalidLegacyCurrentMemoryIdClient()
    )

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_legacy_modify(
            {
                "memory_id": 123,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "priority": 1,
                "disclosure": "old",
            }
        )

    assert exc_info.value.status_code == 409
    assert "current memory_id is invalid" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_rollback_legacy_modify_restores_metadata_only_without_content_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meta_calls = []

    class _LegacyMetaOnlyClient:
        async def get_memory_version(self, memory_id: int):
            return {"id": int(memory_id), "migrated_to": None}

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return {"id": 123, "priority": 5, "disclosure": "internal"}

        async def rollback_to_memory(self, path: str, memory_id: int, domain: str):
            _ = path, memory_id, domain
            raise AssertionError("rollback_to_memory should not run for metadata-only rollback")

        async def restore_path_metadata(
            self,
            *,
            path: str,
            domain: str,
            priority: int,
            disclosure: str | None,
        ):
            meta_calls.append((path, domain, priority, disclosure))
            return {"metadata_restored": True}

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _LegacyMetaOnlyClient())

    payload = await review_api._rollback_legacy_modify(
        {
            "memory_id": 123,
            "path": "agent/node",
            "domain": "core",
            "uri": "core://agent/node",
            "priority": 1,
            "disclosure": "public",
        }
    )

    assert payload == {"new_version": 123}
    assert meta_calls == [("agent/node", "core", 1, "public")]


@pytest.mark.asyncio
async def test_rollback_legacy_modify_restores_content_only_without_metadata_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lane_calls = []
    rollback_calls = []

    class _LegacyVersionOnlyClient:
        async def get_memory_version(self, memory_id: int):
            versions = {
                123: {"id": 123, "migrated_to": 999},
                999: {"id": 999, "migrated_to": None},
            }
            return versions.get(int(memory_id))

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return {"id": 999, "priority": 1, "disclosure": "old"}

        async def rollback_to_memory(self, path: str, memory_id: int, domain: str):
            rollback_calls.append((path, domain, int(memory_id)))
            return {"restored_memory_id": int(memory_id)}

        async def restore_path_metadata(self, **_: object):
            raise AssertionError(
                "restore_path_metadata should not run for version-only rollback"
            )

    async def _run_write_lane_stub(operation: str, task, *, session_id=None):
        lane_calls.append({"operation": operation, "session_id": session_id})
        return await task()

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _LegacyVersionOnlyClient())
    monkeypatch.setattr(review_api, "_run_write_lane", _run_write_lane_stub)

    payload = await review_api._rollback_legacy_modify(
        {
            "memory_id": 123,
            "path": "agent/node",
            "domain": "core",
            "uri": "core://agent/node",
            "priority": 1,
            "disclosure": "old",
        },
        lane_session_id="review.internal.rollback:legacy-version-only",
    )

    assert payload == {"new_version": 123}
    assert rollback_calls == [("agent/node", "core", 123)]
    assert lane_calls == [
        {
            "operation": "rollback.rollback_to_memory",
            "session_id": "review.internal.rollback:legacy-version-only",
        }
    ]


@pytest.mark.asyncio
async def test_rollback_legacy_modify_restores_content_and_metadata_atomically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-legacy-modify-success.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    created = await client.create_memory(
        parent_path="",
        content="original content",
        priority=1,
        title="node",
        domain="core",
    )
    node_mem = await client.get_memory_by_path("node", "core")
    await client.update_memory(
        path="node",
        domain="core",
        content="current content",
        priority=5,
        disclosure="current disclosure",
        expected_old_id=node_mem["id"],
    )

    lane_calls = []

    async def _run_write_lane_stub(operation: str, task, *, session_id=None):
        lane_calls.append({"operation": operation, "session_id": session_id})
        return await task()

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: client)
    monkeypatch.setattr(review_api, "_run_write_lane", _run_write_lane_stub)

    payload = await review_api._rollback_legacy_modify(
        {
            "memory_id": created["id"],
            "path": "node",
            "domain": "core",
            "uri": "core://node",
            "priority": 1,
            "disclosure": None,
        },
        lane_session_id="review.internal.rollback:legacy-success",
    )

    current = await client.get_memory_by_path("node", "core", reinforce_access=False)

    assert payload == {"new_version": created["id"]}
    assert current is not None
    assert current["id"] == created["id"]
    assert current["content"] == "original content"
    assert current["priority"] == 1
    assert current["disclosure"] is None
    assert lane_calls == [
        {
            "operation": "rollback.restore_legacy_modify",
            "session_id": "review.internal.rollback:legacy-success",
        }
    ]

    await client.close()


@pytest.mark.asyncio
async def test_rollback_legacy_modify_failure_does_not_leave_partial_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-legacy-modify-failure.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    created = await client.create_memory(
        parent_path="",
        content="original content",
        priority=1,
        title="node",
        domain="core",
    )
    node_mem2 = await client.get_memory_by_path("node", "core")
    updated = await client.update_memory(
        path="node",
        domain="core",
        content="current content",
        priority=5,
        disclosure="current disclosure",
        expected_old_id=node_mem2["id"],
    )

    async def _boom_reindex(self, session, memory_id: int):
        _ = self, session, memory_id
        raise RuntimeError("reindex failed")

    async def _run_write_lane_stub(_operation: str, task, *, session_id=None):
        _ = session_id
        return await task()

    monkeypatch.setattr(SQLiteClient, "_reindex_memory", _boom_reindex)
    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: client)
    monkeypatch.setattr(review_api, "_run_write_lane", _run_write_lane_stub)

    with pytest.raises(RuntimeError, match="reindex failed"):
        await review_api._rollback_legacy_modify(
            {
                "memory_id": created["id"],
                "path": "node",
                "domain": "core",
                "uri": "core://node",
                "priority": 1,
                "disclosure": None,
            },
            lane_session_id="review.internal.rollback:legacy-failure",
        )

    current = await client.get_memory_by_path("node", "core", reinforce_access=False)

    assert current is not None
    assert current["id"] == updated["new_memory_id"]
    assert current["content"] == "current content"
    assert current["priority"] == 5
    assert current["disclosure"] == "current disclosure"

    await client.close()


@pytest.mark.asyncio
async def test_rollback_path_modify_meta_can_clear_disclosure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-modify-meta-clear-disclosure.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    await client.create_memory(
        parent_path="",
        content="content",
        priority=5,
        title="node",
        domain="core",
    )
    await client.update_memory(
        path="node",
        domain="core",
        priority=8,
        disclosure="current disclosure",
    )

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: client)

    payload = await review_api._rollback_path(
        {
            "operation_type": "modify_meta",
            "domain": "core",
            "path": "node",
            "uri": "core://node",
            "priority": 5,
            "disclosure": None,
        }
    )

    current = await client.get_memory_by_path("node", "core", reinforce_access=False)

    assert payload == {"metadata_restored": True}
    assert current is not None
    assert current["priority"] == 5
    assert current["disclosure"] is None

    await client.close()


@pytest.mark.asyncio
async def test_rollback_path_modify_meta_returns_404_when_path_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _MissingMetaPathClient:
        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return None

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _MissingMetaPathClient())

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_path(
            {
                "operation_type": "modify_meta",
                "domain": "core",
                "path": "node",
                "uri": "core://node",
                "priority": 5,
                "disclosure": None,
            }
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "'core://node' no longer exists"


@pytest.mark.asyncio
async def test_rollback_path_rejects_unknown_operation_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: object())

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_path(
            {
                "operation_type": "mystery",
                "domain": "core",
                "path": "node",
                "uri": "core://node",
            }
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Unknown path operation: mystery"


@pytest.mark.asyncio
async def test_rollback_path_create_returns_409_when_snapshot_memory_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-create-mismatch.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    original = await client.create_memory(
        parent_path="",
        content="original content",
        priority=1,
        title="parent",
        domain="core",
    )
    await client.remove_path("parent", "core")
    replacement = await client.create_memory(
        parent_path="",
        content="replacement content",
        priority=1,
        title="parent",
        domain="core",
    )

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: client)

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_path(
            {
                "operation_type": "create",
                "domain": "core",
                "path": "parent",
                "uri": "core://parent",
                "memory_id": original["id"],
            }
        )

    assert exc_info.value.status_code == 409
    assert "does not match current memory_id" in str(exc_info.value.detail)
    current = await client.get_memory_by_path("parent", "core", reinforce_access=False)
    assert current is not None
    assert current["id"] == replacement["id"]
    await client.close()


@pytest.mark.asyncio
async def test_rollback_memory_content_returns_409_for_cross_chain_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CrossChainClient:
        async def get_memory_version(self, memory_id: int):
            versions = {
                10: {"id": 10, "migrated_to": 11},
                11: {"id": 11, "migrated_to": None},
                99: {"id": 99, "migrated_to": None},
            }
            return versions.get(int(memory_id))

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return {"id": 99, "content": "current"}

        async def rollback_to_memory(self, path: str, memory_id: int, domain: str):
            _ = path, memory_id, domain
            raise AssertionError("rollback_to_memory should not run for cross-chain rollback")

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _CrossChainClient())

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_memory_content(
            {
                "memory_id": 10,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "all_paths": [],
            }
        )

    assert exc_info.value.status_code == 409
    assert "not in the same version chain" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_rollback_legacy_modify_returns_409_for_cross_chain_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CrossChainLegacyClient:
        async def get_memory_version(self, memory_id: int):
            versions = {
                20: {"id": 20, "migrated_to": 21},
                21: {"id": 21, "migrated_to": None},
                88: {"id": 88, "migrated_to": None},
            }
            return versions.get(int(memory_id))

        async def get_memory_by_path(
            self,
            path: str,
            domain: str,
            reinforce_access: bool = False,
        ):
            _ = path, domain, reinforce_access
            return {"id": 88, "priority": 2, "disclosure": "internal"}

        async def rollback_to_memory(self, path: str, memory_id: int, domain: str):
            _ = path, memory_id, domain
            raise AssertionError("rollback_to_memory should not run for cross-chain rollback")

        async def update_memory(
            self,
            path: str,
            domain: str,
            priority: int | None = None,
            disclosure: str | None = None,
        ):
            _ = path, domain, priority, disclosure
            raise AssertionError("update_memory should not run for cross-chain rollback")

    monkeypatch.setattr(review_api, "get_sqlite_client", lambda: _CrossChainLegacyClient())

    with pytest.raises(HTTPException) as exc_info:
        await review_api._rollback_legacy_modify(
            {
                "memory_id": 20,
                "path": "agent/node",
                "domain": "core",
                "uri": "core://agent/node",
                "priority": 1,
                "disclosure": "public",
            }
        )

    assert exc_info.value.status_code == 409
    assert "not in the same version chain" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_sqlite_client_rollback_to_memory_restores_path_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-restore-meta.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    original = await client.create_memory(
        parent_path="",
        content="version 1",
        priority=1,
        title="agent",
        domain="core",
        disclosure="old disclosure",
    )
    await client.update_memory(
        path="agent",
        content="version 2",
        priority=5,
        disclosure="new disclosure",
        domain="core",
        expected_old_id=original["id"],
    )

    payload = await client.rollback_to_memory(
        "agent",
        original["id"],
        "core",
        restore_path_metadata=True,
        restore_priority=1,
        restore_disclosure="old disclosure",
    )

    current = await client.get_memory_by_path("agent", "core", reinforce_access=False)
    assert payload["restored_memory_id"] == original["id"]
    assert current is not None
    assert current["id"] == original["id"]
    assert current["priority"] == 1
    assert current["disclosure"] == "old disclosure"

    await client.close()


@pytest.mark.asyncio
async def test_sqlite_client_rollback_to_memory_restores_alias_path_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-restore-alias-meta.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    original = await client.create_memory(
        parent_path="",
        content="version 1",
        priority=1,
        title="agent",
        domain="core",
        disclosure="old disclosure",
    )
    await client.add_path(
        new_path="aliases/agent",
        target_path="agent",
        new_domain="core",
        target_domain="core",
        priority=7,
        disclosure="alias disclosure",
    )
    await client.update_memory(
        path="agent",
        content="version 2",
        priority=5,
        disclosure="new disclosure",
        domain="core",
        expected_old_id=original["id"],
    )

    await client.rollback_to_memory(
        "agent",
        original["id"],
        "core",
        restore_path_metadata=True,
        restore_priority=1,
        restore_disclosure="old disclosure",
    )

    current = await client.get_memory_by_path("agent", "core", reinforce_access=False)
    alias_current = await client.get_memory_by_path(
        "aliases/agent",
        "core",
        reinforce_access=False,
    )
    assert current is not None
    assert alias_current is not None
    assert current["id"] == original["id"]
    assert alias_current["id"] == original["id"]
    assert current["priority"] == 1
    assert current["disclosure"] == "old disclosure"
    assert alias_current["priority"] == 1
    assert alias_current["disclosure"] == "old disclosure"

    await client.close()


@pytest.mark.asyncio
async def test_sqlite_client_rollback_to_memory_restores_alias_metadata_too(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-alias-metadata.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    original = await client.create_memory(
        parent_path="",
        content="version 1",
        priority=1,
        title="agent",
        domain="core",
        disclosure="old disclosure",
    )
    await client.add_path(
        new_path="agent-alias",
        target_path="agent",
        new_domain="core",
        target_domain="core",
        priority=9,
        disclosure="alias disclosure",
    )
    await client.update_memory(
        path="agent",
        content="version 2",
        priority=5,
        disclosure="new disclosure",
        domain="core",
        expected_old_id=original["id"],
    )

    await client.rollback_to_memory(
        "agent",
        original["id"],
        "core",
        restore_path_metadata=True,
        restore_priority=1,
        restore_disclosure="old disclosure",
    )

    alias_memory = await client.get_memory_by_path(
        "agent-alias",
        "core",
        reinforce_access=False,
    )
    assert alias_memory is not None
    assert alias_memory["id"] == original["id"]
    assert alias_memory["priority"] == 1
    assert alias_memory["disclosure"] == "old disclosure"

    await client.close()


@pytest.mark.asyncio
async def test_sqlite_client_rollback_to_memory_restores_alias_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-restore-alias-meta.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    original = await client.create_memory(
        parent_path="",
        content="version 1",
        priority=1,
        title="agent",
        domain="core",
        disclosure="old disclosure",
    )
    await client.add_path(
        new_path="agent-alias",
        target_path="agent",
        new_domain="core",
        target_domain="core",
        priority=7,
        disclosure="alias current disclosure",
    )
    await client.update_memory(
        path="agent",
        content="version 2",
        priority=5,
        disclosure="new disclosure",
        domain="core",
        expected_old_id=original["id"],
    )

    await client.rollback_to_memory(
        "agent",
        original["id"],
        "core",
        restore_path_metadata=True,
        restore_priority=1,
        restore_disclosure="old disclosure",
    )

    current = await client.get_memory_by_path("agent", "core", reinforce_access=False)
    alias_current = await client.get_memory_by_path(
        "agent-alias", "core", reinforce_access=False
    )
    assert current is not None
    assert alias_current is not None
    assert alias_current["id"] == current["id"] == original["id"]
    assert alias_current["priority"] == 1
    assert alias_current["disclosure"] == "old disclosure"

    await client.close()


@pytest.mark.asyncio
async def test_sqlite_client_rollback_to_memory_restores_alias_path_metadata(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-restore-alias-meta.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    original = await client.create_memory(
        parent_path="",
        content="version 1",
        priority=1,
        title="agent",
        domain="core",
        disclosure="old disclosure",
    )
    await client.add_path(
        new_path="agent-alias",
        target_path="agent",
        new_domain="core",
        target_domain="core",
        priority=4,
        disclosure="alias disclosure",
    )
    await client.update_memory(
        path="agent",
        content="version 2",
        priority=5,
        disclosure="new disclosure",
        domain="core",
        expected_old_id=original["id"],
    )

    payload = await client.rollback_to_memory(
        "agent",
        original["id"],
        "core",
        restore_path_metadata=True,
        restore_priority=1,
        restore_disclosure="old disclosure",
    )

    current = await client.get_memory_by_path("agent", "core", reinforce_access=False)
    alias = await client.get_memory_by_path(
        "agent-alias",
        "core",
        reinforce_access=False,
    )

    assert payload["restored_memory_id"] == original["id"]
    assert current is not None
    assert alias is not None
    assert current["id"] == original["id"]
    assert alias["id"] == original["id"]
    assert current["priority"] == 1
    assert alias["priority"] == 1
    assert current["disclosure"] == "old disclosure"
    assert alias["disclosure"] == "old disclosure"

    await client.close()


@pytest.mark.asyncio
async def test_sqlite_client_rollback_to_memory_rolls_back_on_reindex_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "review-rollback-atomicity.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    original = await client.create_memory(
        parent_path="",
        content="version 1",
        priority=1,
        title="agent",
        domain="core",
        disclosure="old disclosure",
    )
    update_payload = await client.update_memory(
        path="agent",
        content="version 2",
        priority=5,
        disclosure="new disclosure",
        domain="core",
        expected_old_id=original["id"],
    )
    current_id = update_payload["new_memory_id"]

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("reindex boom")

    monkeypatch.setattr(client, "_reindex_memory", _boom)

    with pytest.raises(RuntimeError, match="reindex boom"):
        await client.rollback_to_memory(
            "agent",
            original["id"],
            "core",
            restore_path_metadata=True,
            restore_priority=1,
            restore_disclosure="old disclosure",
        )

    current = await client.get_memory_by_path("agent", "core", reinforce_access=False)
    assert current is not None
    assert current["id"] == current_id
    assert current["priority"] == 5
    assert current["disclosure"] == "new disclosure"

    await client.close()
