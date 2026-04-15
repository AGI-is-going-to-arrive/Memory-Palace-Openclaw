from __future__ import annotations

import pytest

import runtime_bootstrap


class _FakeClient:
    def __init__(self, calls: list[object]) -> None:
        self._calls = calls

    async def init_db(self) -> None:
        self._calls.append("init_db")


@pytest.mark.asyncio
async def test_initialize_backend_runtime_runs_restore_init_and_runtime_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    fake_client = _FakeClient(calls)
    database_url = "sqlite+aiosqlite:////tmp/runtime-bootstrap.db"

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(
        runtime_bootstrap,
        "_try_restore_legacy_sqlite_file",
        lambda value: calls.append(("restore", value)),
    )
    monkeypatch.setattr(runtime_bootstrap, "get_sqlite_client", lambda: fake_client)

    async def _ensure_started(factory) -> None:
        calls.append(("ensure_started", factory() is fake_client))

    monkeypatch.setattr(runtime_bootstrap.runtime_state, "ensure_started", _ensure_started)

    returned = await runtime_bootstrap.initialize_backend_runtime()

    assert returned is fake_client
    assert calls == [
        ("restore", database_url),
        "init_db",
        ("ensure_started", True),
    ]


@pytest.mark.asyncio
async def test_initialize_backend_runtime_can_skip_runtime_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    fake_client = _FakeClient(calls)

    monkeypatch.setattr(runtime_bootstrap, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(runtime_bootstrap, "_try_restore_legacy_sqlite_file", lambda _value: None)

    async def _boom(_factory) -> None:
        raise AssertionError("runtime_state.ensure_started should not be called")

    monkeypatch.setattr(runtime_bootstrap.runtime_state, "ensure_started", _boom)

    returned = await runtime_bootstrap.initialize_backend_runtime(
        ensure_runtime_started=False
    )

    assert returned is fake_client
    assert calls == ["init_db"]


@pytest.mark.asyncio
async def test_initialize_backend_runtime_warns_for_network_filesystem_database_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    fake_client = _FakeClient(calls)
    database_url = "sqlite+aiosqlite:////tmp/runtime-bootstrap-network.db"

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setattr(runtime_bootstrap, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(runtime_bootstrap, "_try_restore_legacy_sqlite_file", lambda _value: None)
    monkeypatch.setattr(
        runtime_bootstrap,
        "warn_if_unreliable_file_lock_path",
        lambda path, *, label, log=None: calls.append(("warn", str(path), label)),
    )

    async def _ensure_started(_factory) -> None:
        return None

    monkeypatch.setattr(runtime_bootstrap.runtime_state, "ensure_started", _ensure_started)

    await runtime_bootstrap.initialize_backend_runtime()

    assert calls[0] == (
        "warn",
        "/tmp/runtime-bootstrap-network.db",
        "DATABASE_URL sqlite path",
    )
