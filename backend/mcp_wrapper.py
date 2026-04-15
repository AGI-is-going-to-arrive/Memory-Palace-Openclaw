#!/usr/bin/env python
"""Windows stdio wrapper for Memory Palace MCP.

This launcher mirrors the shell wrapper responsibilities for Windows-native
OpenClaw setups:
- load runtime env from OPENCLAW_MEMORY_PALACE_ENV_FILE when present
- derive a default DATABASE_URL from OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT
- normalize CRLF to LF when forwarding stdio
- propagate subprocess failures to the caller
"""
import os
import signal
import subprocess
import sys
import threading
import errno
from pathlib import Path
from typing import Callable, Dict, List, MutableMapping, Optional, Tuple

IO_CHUNK_SIZE = 4096
ISOLATED_RUNTIME_ENV_PREFIXES = (
    "OPENAI_",
    "LLM_",
    "SMART_EXTRACTION_LLM_",
    "WRITE_GUARD_LLM_",
    "COMPACT_GIST_LLM_",
    "RETRIEVAL_EMBEDDING_",
    "RETRIEVAL_RERANKER_",
    "ROUTER_",
    "EMBEDDING_PROVIDER_",
)
ISOLATED_RUNTIME_ENV_KEYS = {
    "DATABASE_URL",
    "MCP_API_KEY",
    "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE",
    "OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED",
}
WRAPPER_ENV_ALLOWLIST = {
    "OPENCLAW_MEMORY_PALACE_ENV_FILE",
    "OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT",
    "OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON",
    "OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR",
    "OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
}


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _normalize_crlf_chunk(data: bytes) -> bytes:
    """Pass through data unchanged to preserve binary safety for MCP JSON-RPC."""
    if not data:
        return b""
    return data


def _is_isolated_runtime_env_key(key: str) -> bool:
    return key in ISOLATED_RUNTIME_ENV_KEYS or any(
        key.startswith(prefix) for prefix in ISOLATED_RUNTIME_ENV_PREFIXES
    )


def load_env_file(path: Path, target_env: MutableMapping[str, str] | None = None) -> None:
    if not path.is_file():
        return
    env = target_env if target_env is not None else os.environ
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.replace("\ufeff", "", 1).strip()
        if not line:
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        # The runtime env file is the authoritative source for isolated profile
        # launches and must override any inherited host-shell values.
        env[key] = _strip_wrapping_quotes(value.strip())


def build_child_env(runtime_env_path: Path | None) -> Dict[str, str]:
    child_env = dict(os.environ)
    if runtime_env_path is None:
        return child_env
    preserved = {
        key: value
        for key, value in child_env.items()
        if key in WRAPPER_ENV_ALLOWLIST
    }
    child_env = {
        key: value
        for key, value in child_env.items()
        if not _is_isolated_runtime_env_key(key)
    }
    child_env.update(preserved)
    load_env_file(runtime_env_path, child_env)
    return child_env


def sqlite_url_for_file(path: Path) -> str:
    raw_rendered = str(path.expanduser()).replace("\\", "/")
    if raw_rendered.startswith("//"):
        return f"sqlite+aiosqlite://///{raw_rendered.lstrip('/')}"
    if len(raw_rendered) >= 3 and raw_rendered[1] == ":" and raw_rendered[2] == "/":
        return f"sqlite+aiosqlite:///{raw_rendered}"
    rendered = str(path.expanduser().resolve()).replace("\\", "/")
    if rendered.startswith("//"):
        return f"sqlite+aiosqlite://///{rendered.lstrip('/')}"
    if len(rendered) >= 3 and rendered[1] == ":" and rendered[2] == "/":
        return f"sqlite+aiosqlite:///{rendered}"
    return f"sqlite+aiosqlite:////{rendered.lstrip('/')}"


def _iter_forwarded_signals() -> List[int]:
    signals: List[int] = []
    for name in ("SIGINT", "SIGTERM", "CTRL_BREAK_EVENT"):
        value = getattr(signal, name, None)
        if value is None or value in signals:
            continue
        signals.append(int(value))
    return signals


def _subprocess_creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


def _request_parent_stdin_shutdown() -> None:
    stdin = getattr(sys, "stdin", None)
    candidates = []
    buffer = getattr(stdin, "buffer", None)
    if buffer is not None:
        candidates.append(buffer)
    if stdin is not None:
        candidates.append(stdin)
    seen: set[int] = set()
    for stream in candidates:
        stream_id = id(stream)
        if stream_id in seen:
            continue
        seen.add(stream_id)
        try:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        except Exception:
            continue


def _close_fd_quietly(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _is_expected_stdin_shutdown_error(exc: BaseException) -> bool:
    if isinstance(exc, ValueError):
        return "closed file" in str(exc).lower()
    if isinstance(exc, OSError):
        return exc.errno in {
            errno.EBADF,
            errno.EINVAL,
        } or getattr(exc, "winerror", None) in {6}
    return False


class _InterruptibleStdinReader:
    def __init__(self, stream: object) -> None:
        self._stream = stream
        self._buffer = getattr(stream, "buffer", None) or stream
        self._guard = threading.Lock()
        self._closed = False
        self._dup_fd: Optional[int] = None

        fileno = getattr(self._buffer, "fileno", None)
        if callable(fileno):
            try:
                fd = int(fileno())
            except Exception:
                fd = -1
            if fd >= 0:
                try:
                    self._dup_fd = os.dup(fd)
                except OSError:
                    self._dup_fd = None

    def read(self, size: int) -> bytes:
        with self._guard:
            if self._closed:
                return b""
            dup_fd = self._dup_fd
            buffer = self._buffer

        if dup_fd is not None:
            return os.read(dup_fd, size)
        read = getattr(buffer, "read", None)
        if not callable(read):
            return b""
        chunk = read(size)
        if isinstance(chunk, bytes):
            return chunk
        if chunk is None:
            return b""
        return str(chunk).encode("utf-8", errors="ignore")

    def shutdown(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True
            dup_fd = self._dup_fd
            self._dup_fd = None

        if dup_fd is not None:
            _close_fd_quietly(dup_fd)
            return
        _request_parent_stdin_shutdown()


def _stop_forwarding(
    stop_forwarding: threading.Event,
    *,
    shutdown_stdin: bool = True,
    stdin_shutdown: Optional[Callable[[], None]] = None,
) -> None:
    stop_forwarding.set()
    if shutdown_stdin:
        if stdin_shutdown is not None:
            stdin_shutdown()
        else:
            _request_parent_stdin_shutdown()


def _make_signal_forwarder(
    process: subprocess.Popen[bytes],
    stop_forwarding: threading.Event,
) -> Callable[[int, object], None]:
    def _kill_if_still_running() -> None:
        try:
            process.wait(timeout=5.0)
            return
        except Exception:
            pass
        if process.poll() is not None:
            return
        try:
            process.kill()
        except Exception:
            pass

    def _handle(signum: int, _frame: object) -> None:
        _stop_forwarding(stop_forwarding)
        if process.poll() is not None:
            return
        try:
            process.send_signal(signum)
        except Exception:
            try:
                process.terminate()
            except Exception:
                pass
        threading.Thread(target=_kill_if_still_running, daemon=True).start()

    return _handle


def install_signal_forwarding(
    process: subprocess.Popen[bytes],
    stop_forwarding: threading.Event,
) -> Callable[[], None]:
    registered: Dict[int, object] = {}
    handler = _make_signal_forwarder(process, stop_forwarding)
    for signum in _iter_forwarded_signals():
        try:
            registered[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        except Exception:
            continue

    def _restore() -> None:
        for signum, previous in registered.items():
            try:
                signal.signal(signum, previous)
            except Exception:
                pass

    return _restore


def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    mcp_server_path = os.path.join(script_dir, "mcp_server.py")
    runtime_env_raw = os.getenv("OPENCLAW_MEMORY_PALACE_ENV_FILE")
    runtime_env_path = Path(runtime_env_raw) if runtime_env_raw else None
    child_env = build_child_env(runtime_env_path)
    runtime_root = Path(child_env.get("OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT") or script_dir)
    child_env.setdefault("DATABASE_URL", sqlite_url_for_file(runtime_root / "data" / "memory-palace.db"))
    child_env.setdefault("RETRIEVAL_REMOTE_TIMEOUT_SEC", "1")
    child_env.setdefault("RETRIEVAL_RERANK_TOP_N", "12")

    try:
        process = subprocess.Popen(
            [sys.executable, mcp_server_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            bufsize=0,
            cwd=script_dir,
            env=child_env,
            creationflags=_subprocess_creationflags(),
        )
    except OSError as exc:
        print(f"Failed to start MCP server: {exc}", file=sys.stderr)
        sys.exit(1)

    io_errors: List[Tuple[str, str]] = []
    stop_forwarding = threading.Event()
    stdin_reader = _InterruptibleStdinReader(getattr(sys, "stdin", None))
    restore_signal_handlers = install_signal_forwarding(process, stop_forwarding)

    def _record_io_error(channel: str, exc: Exception) -> None:
        io_errors.append((channel, str(exc)))
        _stop_forwarding(
            stop_forwarding,
            stdin_shutdown=stdin_reader.shutdown,
        )

    def forward_stdin() -> None:
        """Forward stdin to subprocess while normalizing CRLF to LF."""
        try:
            while not stop_forwarding.is_set():
                if process.poll() is not None:
                    _stop_forwarding(
                        stop_forwarding,
                        stdin_shutdown=stdin_reader.shutdown,
                    )
                    break
                chunk = stdin_reader.read(IO_CHUNK_SIZE)
                if not chunk:
                    break
                data = _normalize_crlf_chunk(chunk)
                if not data:
                    continue
                if process.stdin is None:
                    break
                process.stdin.write(data)
                process.stdin.flush()
        except Exception as exc:
            if stop_forwarding.is_set() and _is_expected_stdin_shutdown_error(exc):
                return
            _record_io_error("stdin", exc)
        finally:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except Exception:
                pass

    def forward_stdout() -> None:
        """Forward stdout from subprocess while normalizing CRLF to LF."""
        try:
            while not stop_forwarding.is_set():
                if process.stdout is None:
                    break
                chunk = process.stdout.read(IO_CHUNK_SIZE)
                if not chunk:
                    break
                data = _normalize_crlf_chunk(chunk)
                if not data:
                    continue
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
        except Exception as exc:
            _record_io_error("stdout", exc)
        finally:
            _stop_forwarding(
                stop_forwarding,
                stdin_shutdown=stdin_reader.shutdown,
            )

    stdin_thread = threading.Thread(target=forward_stdin, daemon=True)
    stdout_thread = threading.Thread(target=forward_stdout, daemon=True)
    stdin_thread.start()
    stdout_thread.start()

    try:
        process.wait()
    finally:
        _stop_forwarding(
            stop_forwarding,
            stdin_shutdown=stdin_reader.shutdown,
        )
        stdout_thread.join(timeout=1)
        stdin_thread.join(timeout=1)
        restore_signal_handlers()

    return_code = int(process.returncode or 0)
    if io_errors:
        channel, message = io_errors[0]
        print(f"Wrapper I/O error ({channel}): {message}", file=sys.stderr)
        if return_code == 0:
            return_code = 1
    sys.exit(return_code)


if __name__ == "__main__":
    main()
