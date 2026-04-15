from __future__ import annotations

import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import mcp_runtime_services


class _FakeClient:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url


@pytest.mark.asyncio
async def test_maybe_auto_flush_uses_process_lock_for_file_backed_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[object] = []

    class _FakeLock:
        def __init__(self, path: str, timeout: float) -> None:
            recorded.append(("lock", path, timeout))

        async def __aenter__(self):
            recorded.append("enter")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            recorded.append("exit")
            return False

    async def _flush(**kwargs):
        recorded.append(("flush", kwargs["reason"], kwargs["max_lines"]))
        return {"ok": True, "source": kwargs["source"]}

    monkeypatch.setattr(mcp_runtime_services, "AsyncFileLock", _FakeLock)

    payload = await mcp_runtime_services.maybe_auto_flush_impl(
        _FakeClient(f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}"),
        reason="threshold",
        auto_flush_enabled=True,
        get_session_id=lambda: "session-42",
        auto_flush_in_progress=set(),
        flush_session_summary_to_memory=_flush,
        auto_flush_summary_lines=12,
    )

    assert payload == {"ok": True, "source": "auto_flush"}
    lock_call = recorded[0]
    assert lock_call[0] == "lock"
    assert Path(lock_call[1]).parent == tmp_path
    assert Path(lock_call[1]).name.startswith("demo.db.auto_flush.")
    assert Path(lock_call[1]).name.endswith(".lock")
    assert ("flush", "threshold", 12) in recorded


@pytest.mark.asyncio
async def test_maybe_auto_flush_skips_when_process_lock_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flush_called = False

    class _BusyLock:
        def __init__(self, _path: str, timeout: float) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            raise mcp_runtime_services.FileLockTimeout("busy")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def _flush(**kwargs):
        nonlocal flush_called
        flush_called = True
        return kwargs

    monkeypatch.setattr(mcp_runtime_services, "AsyncFileLock", _BusyLock)

    payload = await mcp_runtime_services.maybe_auto_flush_impl(
        _FakeClient(f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}"),
        reason="threshold",
        auto_flush_enabled=True,
        get_session_id=lambda: "session-43",
        auto_flush_in_progress=set(),
        flush_session_summary_to_memory=_flush,
        auto_flush_summary_lines=12,
    )

    assert payload is None
    assert flush_called is False


@pytest.mark.asyncio
async def test_maybe_auto_flush_uses_env_timeout_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, float]] = []

    class _FakeLock:
        def __init__(self, path: str, timeout: float) -> None:
            _ = path
            recorded.append(("lock", timeout))

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def _flush(**kwargs):
        return {"ok": True, "reason": kwargs["reason"]}

    monkeypatch.setattr(mcp_runtime_services, "AsyncFileLock", _FakeLock)
    monkeypatch.setenv("RUNTIME_AUTO_FLUSH_PROCESS_LOCK_TIMEOUT_SEC", "19.5")

    payload = await mcp_runtime_services.maybe_auto_flush_impl(
        _FakeClient(f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}"),
        reason="threshold",
        auto_flush_enabled=True,
        get_session_id=lambda: "session-44",
        auto_flush_in_progress=set(),
        flush_session_summary_to_memory=_flush,
        auto_flush_summary_lines=12,
    )

    assert payload == {"ok": True, "reason": "threshold"}
    assert recorded == [("lock", 19.5)]
