from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import mcp_server


def test_stdio_stdin_has_buffer_returns_false_when_stdin_is_missing() -> None:
    with mock.patch.object(mcp_server.sys, "stdin", None):
        assert mcp_server._stdio_stdin_has_buffer() is False


def test_guard_stdio_startup_exits_quietly_by_default() -> None:
    with mock.patch.object(mcp_server.sys, "stdin", None):
        with pytest.raises(SystemExit) as exc:
            mcp_server._guard_stdio_startup()

    assert exc.value.code == 2


def test_guard_stdio_startup_prints_single_line_without_traceback_when_logging_is_enabled(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with mock.patch.object(mcp_server.sys, "stdin", None), mock.patch.dict(
        mcp_server.os.environ,
        {"OPENCLAW_MEMORY_PALACE_LOG_INVALID_STDIO": "1"},
        clear=False,
    ):
        with caplog.at_level("WARNING", logger=mcp_server.logger.name):
            with pytest.raises(SystemExit) as exc:
                mcp_server._guard_stdio_startup()

    assert exc.value.code == 2
    assert "stdin is unavailable for stdio transport" in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.asyncio
async def test_startup_uses_runtime_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[bool] = []

    async def _fake_initialize(*, ensure_runtime_started: bool = True) -> None:
        called.append(ensure_runtime_started)

    monkeypatch.setattr(mcp_server, "initialize_backend_runtime", _fake_initialize)

    await mcp_server.startup()

    assert called == [True]
