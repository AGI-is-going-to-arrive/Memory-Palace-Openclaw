from __future__ import annotations

import errno
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import mcp_wrapper
from db.sqlite_paths import _normalize_sqlite_database_url


def test_load_env_file_overrides_inherited_values() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        env_path = Path(tmp_dir) / "runtime.env"
        env_path.write_text("FOO=bar\nEXISTING=new\nEMPTY=\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"EXISTING": "old", "EMPTY": "keep"}, clear=False):
            mcp_wrapper.load_env_file(env_path)
            assert os.environ["FOO"] == "bar"
            assert os.environ["EXISTING"] == "new"
            assert os.environ["EMPTY"] == ""


def test_load_env_file_strips_wrapping_quotes() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        env_path = Path(tmp_dir) / "runtime.env"
        env_path.write_text(
            'OPENAI_MODEL="gpt-5.4"\nOPENAI_BASE_URL=\'http://127.0.0.1:8317/v1\'\n',
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            mcp_wrapper.load_env_file(env_path)
            assert os.environ["OPENAI_MODEL"] == "gpt-5.4"
            assert os.environ["OPENAI_BASE_URL"] == "http://127.0.0.1:8317/v1"


def test_load_env_file_accepts_export_prefix_and_bom() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        env_path = Path(tmp_dir) / "runtime.env"
        env_path.write_text(
            "\ufeffexport RETRIEVAL_EMBEDDING_MODEL=qwen3-embedding:8b-q8_0\n"
            "export WRITE_GUARD_LLM_MODEL='gpt-5.4'\n",
            encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {}, clear=False):
            mcp_wrapper.load_env_file(env_path)
            assert os.environ["RETRIEVAL_EMBEDDING_MODEL"] == "qwen3-embedding:8b-q8_0"
            assert os.environ["WRITE_GUARD_LLM_MODEL"] == "gpt-5.4"


def test_build_child_env_drops_inherited_runtime_model_values_when_env_file_is_present() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        env_path = Path(tmp_dir) / "runtime.env"
        env_path.write_text("OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b\n", encoding="utf-8")
        with mock.patch.dict(
            os.environ,
            {
                "OPENAI_MODEL": "leak-model",
                "OPENAI_API_KEY": "leak-key",
                "DATABASE_URL": "sqlite+aiosqlite:////tmp/leak.db",
                "OPENCLAW_MEMORY_PALACE_ENV_FILE": str(env_path),
                "OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT": tmp_dir,
            },
            clear=False,
        ):
            child_env = mcp_wrapper.build_child_env(env_path)

    assert child_env["OPENCLAW_MEMORY_PALACE_ENV_FILE"] == str(env_path)
    assert child_env["OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT"] == tmp_dir
    assert "OPENAI_MODEL" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert "DATABASE_URL" not in child_env
    assert child_env["OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE"] == "b"


def test_normalize_crlf_chunk_preserves_binary_safe_input() -> None:
    assert mcp_wrapper._normalize_crlf_chunk(b"line1\r\nline2\r") == b"line1\r\nline2\r"


def test_sqlite_url_for_file_supports_windows_drive_paths() -> None:
    path = Path("C:/memory-palace/demo.db")
    assert mcp_wrapper.sqlite_url_for_file(path) == "sqlite+aiosqlite:///C:/memory-palace/demo.db"


def test_sqlite_url_for_file_preserves_windows_unc_network_share_path() -> None:
    path = Path(r"\\server\share\memory-palace\demo.db")
    assert mcp_wrapper.sqlite_url_for_file(path) == "sqlite+aiosqlite://///server/share/memory-palace/demo.db"


def test_normalize_sqlite_database_url_preserves_query_before_fragment() -> None:
    normalized = _normalize_sqlite_database_url(
        "sqlite+aiosqlite:////tmp/memory-palace.db?mode=ro#primary"
    )

    assert normalized == "sqlite+aiosqlite:////tmp/memory-palace.db?mode=ro#primary"


def test_install_signal_forwarding_proxies_and_restores_handlers() -> None:
    forwarded: list[tuple[int, object]] = []
    previous_handlers = {}
    installed_handlers = {}

    def _record_signal(signum: int, handler: object) -> None:
        forwarded.append((signum, handler))
        installed_handlers[signum] = handler

    process = mock.Mock()
    process.poll.return_value = None
    stop_forwarding = threading.Event()

    with mock.patch.object(mcp_wrapper.signal, "getsignal", side_effect=lambda signum: f"prev-{signum}"), \
        mock.patch.object(mcp_wrapper.signal, "signal", side_effect=_record_signal):
        restore = mcp_wrapper.install_signal_forwarding(process, stop_forwarding)
        expected_signals = mcp_wrapper._iter_forwarded_signals()

        assert sorted(installed_handlers) == sorted(expected_signals)
        for signum in expected_signals:
            previous_handlers[signum] = f"prev-{signum}"
            handler = installed_handlers[signum]
            assert callable(handler)
            handler(signum, None)
            process.send_signal.assert_any_call(signum)

        assert stop_forwarding.is_set()
        restore()

    restored = forwarded[-len(previous_handlers):]
    assert restored == [(signum, previous_handlers[signum]) for signum in previous_handlers]


def test_iter_forwarded_signals_includes_ctrl_break_event_when_available(monkeypatch) -> None:
    monkeypatch.setattr(mcp_wrapper.signal, "CTRL_BREAK_EVENT", 21, raising=False)

    forwarded = mcp_wrapper._iter_forwarded_signals()

    assert 21 in forwarded
    assert forwarded.count(21) == 1


def test_subprocess_creationflags_uses_new_process_group_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(mcp_wrapper.os, "name", "nt", raising=False)
    monkeypatch.setattr(
        mcp_wrapper.subprocess, "CREATE_NEW_PROCESS_GROUP", 512, raising=False
    )

    assert mcp_wrapper._subprocess_creationflags() == 512


def test_request_parent_stdin_shutdown_closes_buffer_when_present() -> None:
    closed: list[str] = []

    class _Buffer:
        def close(self) -> None:
            closed.append("buffer")

    class _Stdin:
        buffer = _Buffer()

    with mock.patch.object(mcp_wrapper.sys, "stdin", _Stdin()):
        mcp_wrapper._request_parent_stdin_shutdown()

    assert closed == ["buffer"]


def test_stop_forwarding_sets_event_and_requests_parent_stdin_shutdown() -> None:
    stop_forwarding = threading.Event()

    with mock.patch.object(
        mcp_wrapper,
        "_request_parent_stdin_shutdown",
    ) as shutdown_mock:
        mcp_wrapper._stop_forwarding(stop_forwarding)

    assert stop_forwarding.is_set()
    shutdown_mock.assert_called_once_with()


def test_stop_forwarding_prefers_explicit_stdin_shutdown_callback() -> None:
    stop_forwarding = threading.Event()
    callbacks: list[str] = []

    with mock.patch.object(
        mcp_wrapper,
        "_request_parent_stdin_shutdown",
    ) as shutdown_mock:
        mcp_wrapper._stop_forwarding(
            stop_forwarding,
            stdin_shutdown=lambda: callbacks.append("custom"),
        )

    assert stop_forwarding.is_set()
    assert callbacks == ["custom"]
    shutdown_mock.assert_not_called()


def test_interruptible_stdin_reader_shutdown_closes_duplicated_fd(monkeypatch) -> None:
    duplicated_fds: list[int] = []
    closed_fds: list[int] = []

    class _Buffer:
        def fileno(self) -> int:
            return 11

    class _Stdin:
        buffer = _Buffer()

    monkeypatch.setattr(mcp_wrapper.os, "dup", lambda fd: duplicated_fds.append(fd) or 22)
    monkeypatch.setattr(mcp_wrapper.os, "close", lambda fd: closed_fds.append(fd))

    reader = mcp_wrapper._InterruptibleStdinReader(_Stdin())
    reader.shutdown()

    assert duplicated_fds == [11]
    assert closed_fds == [22]


def test_interruptible_stdin_reader_shutdown_falls_back_to_parent_stdin_shutdown(
    monkeypatch,
) -> None:
    class _Buffer:
        def fileno(self) -> int:
            raise OSError("no fd")

    class _Stdin:
        buffer = _Buffer()

    with mock.patch.object(
        mcp_wrapper,
        "_request_parent_stdin_shutdown",
    ) as shutdown_mock:
        reader = mcp_wrapper._InterruptibleStdinReader(_Stdin())
        reader.shutdown()

    shutdown_mock.assert_called_once_with()


def test_is_expected_stdin_shutdown_error_matches_closed_handle_cases() -> None:
    bad_fd = OSError(errno.EBADF, "bad fd")
    bad_fd.winerror = 6

    assert mcp_wrapper._is_expected_stdin_shutdown_error(ValueError("I/O operation on closed file")) is True
    assert mcp_wrapper._is_expected_stdin_shutdown_error(bad_fd) is True
    assert mcp_wrapper._is_expected_stdin_shutdown_error(RuntimeError("other")) is False


def test_signal_forwarder_escalates_to_kill_when_child_does_not_exit() -> None:
    process = mock.Mock()
    process.poll.side_effect = [None, None]
    process.wait.side_effect = TimeoutError("still running")
    stop_forwarding = threading.Event()
    launched_targets = []

    def _build_thread(*, target, daemon):
        launched_targets.append((target, daemon))
        return mock.Mock(start=lambda: target())

    with mock.patch.object(mcp_wrapper.threading, "Thread", side_effect=_build_thread):
        handler = mcp_wrapper._make_signal_forwarder(process, stop_forwarding)
        handler(signal.SIGTERM, None)

    assert stop_forwarding.is_set()
    process.send_signal.assert_called_once_with(signal.SIGTERM)
    process.kill.assert_called_once_with()
    assert launched_targets and launched_targets[0][1] is True


def test_signal_forwarder_requests_parent_stdin_shutdown() -> None:
    process = mock.Mock()
    process.poll.return_value = None
    stop_forwarding = threading.Event()

    with mock.patch.object(
        mcp_wrapper,
        "_request_parent_stdin_shutdown",
    ) as shutdown_mock, mock.patch.object(
        mcp_wrapper.threading,
        "Thread",
        return_value=mock.Mock(start=lambda: None),
    ):
        handler = mcp_wrapper._make_signal_forwarder(process, stop_forwarding)
        handler(signal.SIGTERM, None)

    shutdown_mock.assert_called_once_with()
    process.send_signal.assert_called_once_with(signal.SIGTERM)
