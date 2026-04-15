from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import mcp_server
import mcp_tool_runtime


async def _noop_async(*_args, **_kwargs) -> None:
    return None


async def _run_write_inline(_operation: str, task):
    return await task()


class _CompactClient:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url


@pytest.mark.asyncio
async def test_compact_context_uses_process_lock_for_file_backed_database(
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
        return {"flushed": True, "reason": kwargs["reason"]}

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _CompactClient(f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}"))
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(mcp_server, "_flush_session_summary_to_memory", _flush)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_tool_runtime, "AsyncFileLock", _FakeLock)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["flushed"] is True
    lock_call = recorded[0]
    assert lock_call[0] == "lock"
    assert Path(lock_call[1]).parent == tmp_path
    assert Path(lock_call[1]).name.startswith("demo.db.compact_context.")
    assert Path(lock_call[1]).name.endswith(".lock")
    assert ("flush", "unit_test", 5) in recorded


@pytest.mark.asyncio
async def test_compact_context_reports_busy_process_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BusyLock:
        def __init__(self, _path: str, timeout: float) -> None:
            _ = timeout

        async def __aenter__(self):
            raise mcp_tool_runtime.FileLockTimeout("busy")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def _flush(**kwargs):
        raise AssertionError(f"flush should not run when process lock is busy: {kwargs}")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _CompactClient(f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}"))
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(mcp_server, "_flush_session_summary_to_memory", _flush)
    monkeypatch.setattr(mcp_tool_runtime, "AsyncFileLock", _BusyLock)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"] == "Compaction already in progress for current session/process."
