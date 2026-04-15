from __future__ import annotations

import sys
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import mcp_server


@pytest.mark.parametrize(
    "message",
    [
        "Event loop is closed",
        "Task got Future attached to a different loop",
        "bound to a different event loop",
    ],
)
def test_is_ignorable_stdio_shutdown_error_detects_known_loop_messages(message: str) -> None:
    assert mcp_server._is_ignorable_stdio_shutdown_error(RuntimeError(message)) is True


@pytest.mark.asyncio
async def test_shutdown_ignores_known_loop_shutdown_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _drain_pending(**_kwargs) -> None:
        return None

    async def _runtime_shutdown() -> None:
        raise RuntimeError("Event loop is closed")

    async def _close_client() -> None:
        raise RuntimeError("Task got Future attached to a different loop")

    monkeypatch.setattr(mcp_server, "drain_pending_flush_summaries", _drain_pending)
    monkeypatch.setattr(mcp_server.runtime_state, "shutdown", _runtime_shutdown)
    monkeypatch.setattr(mcp_server, "close_sqlite_client", _close_client)

    await mcp_server.shutdown()


@pytest.mark.asyncio
async def test_shutdown_re_raises_non_ignorable_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _drain_pending(**_kwargs) -> None:
        return None

    async def _runtime_shutdown() -> None:
        raise RuntimeError("boom")

    async def _close_client() -> None:
        return None

    monkeypatch.setattr(mcp_server, "drain_pending_flush_summaries", _drain_pending)
    monkeypatch.setattr(mcp_server.runtime_state, "shutdown", _runtime_shutdown)
    monkeypatch.setattr(mcp_server, "close_sqlite_client", _close_client)

    with pytest.raises(RuntimeError, match="boom"):
        await mcp_server.shutdown()


@pytest.mark.asyncio
async def test_shutdown_drains_pending_flush_summaries_before_runtime_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _drain_pending(**_kwargs) -> None:
        calls.append("drain")

    async def _runtime_shutdown() -> None:
        calls.append("runtime")

    async def _close_client() -> None:
        calls.append("close")

    monkeypatch.setattr(mcp_server, "drain_pending_flush_summaries", _drain_pending)
    monkeypatch.setattr(mcp_server.runtime_state, "shutdown", _runtime_shutdown)
    monkeypatch.setattr(mcp_server, "close_sqlite_client", _close_client)

    await mcp_server.shutdown()

    assert calls == ["drain", "runtime", "close"]
