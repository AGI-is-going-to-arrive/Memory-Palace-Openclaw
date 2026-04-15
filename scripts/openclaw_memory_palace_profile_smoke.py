#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import locale
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openclaw_json_output import extract_json_from_streams, extract_last_json_from_text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NATIVE_PATH_CLS = type(PROJECT_ROOT)
BACKEND_ROOT = PROJECT_ROOT / "backend"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
OPENCLAW_PLUGIN_ROOT = PROJECT_ROOT / "extensions" / "memory-palace"
DEFAULT_SHARED_OPENCLAW_ROOT = Path.home() / ".openclaw"
DEFAULT_SHARED_CONFIG_PATH = DEFAULT_SHARED_OPENCLAW_ROOT / "openclaw.json"
DEFAULT_SHARED_MEMORY_PALACE_ROOT = DEFAULT_SHARED_OPENCLAW_ROOT / "memory-palace"
DEFAULT_SHARED_MEMORY_PALACE_DB = DEFAULT_SHARED_MEMORY_PALACE_ROOT / "data" / "memory-palace.db"
DEFAULT_MODEL_ENV = os.environ.get("OPENCLAW_PROFILE_MODEL_ENV", "")
DEFAULT_OPENCLAW_BIN = (
    str(os.environ.get("OPENCLAW_BIN") or "").strip()
    or shutil.which("openclaw")
    or "openclaw"
)
REPORT_PATH = PROJECT_ROOT / "docs" / "OPENCLAW_PLUGIN_PROFILE_SMOKE_REPORT.md"
_PLAYWRIGHT_BROWSER_READY = False
TRANSPARENT_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/w8AAusB9Y9s3P8AAAAASUVORK5CYII="
)
TRANSIENT_LOCK_MARKERS = (
    "database is locked",
    "sqlite3.operationalerror: database is locked",
)
_DOCKER_BACKEND_EXEC_TRANSIENT_MARKERS = (
    'service "backend" is not running',
    "service backend is not running",
    "is restarting",
    "no container found",
)
_ACTIVE_PROCESS_GROUPS: set[int] = set()
_ACTIVE_PROCESS_GROUPS_LOCK = threading.RLock()
_SIGNAL_CLEANUP_INSTALLED = False
_OS_KILLPG = getattr(os, "killpg", None)
LOCAL_OLLAMA_EMBED_BASE_MODEL = "qwen3-embedding:8b-q8_0"
LOCAL_OLLAMA_EMBED_ALIAS = "qwen3-embedding:8b-q8_0-ctx8192"
LOCAL_OLLAMA_EMBED_CTX_SIZE = 8192
LOCAL_OLLAMA_EMBED_API_BASE = "http://127.0.0.1:11434/v1"
LOCAL_OLLAMA_EMBED_API_KEY = "ollama"


def _resolve_windows_openclaw_wrapper(command_path: str | os.PathLike[str]) -> list[str] | None:
    candidate = NATIVE_PATH_CLS(command_path)
    if os.name != "nt" or candidate.suffix.lower() not in {".cmd", ".bat"}:
        return None
    node_bin = shutil.which("node")
    if not node_bin:
        return None
    wrapped_module = candidate.parent / "node_modules" / "openclaw" / "openclaw.mjs"
    if wrapped_module.is_file():
        return [node_bin, str(wrapped_module)]
    return None


def resolve_openclaw_command(explicit_bin: str | os.PathLike[str] | None = None) -> list[str]:
    rendered = str(explicit_bin or "").strip()
    if rendered:
        has_explicit_path_hint = (
            any(separator in rendered for separator in ("\\", "/"))
            or rendered.startswith(".")
            or (len(rendered) >= 2 and rendered[1] == ":")
        )
        resolved_path = rendered if has_explicit_path_hint else (shutil.which(rendered) or rendered)
        wrapped = _resolve_windows_openclaw_wrapper(resolved_path)
        return wrapped or [resolved_path]

    openclaw_bin = NATIVE_PATH_CLS(DEFAULT_OPENCLAW_BIN)
    wrapped = _resolve_windows_openclaw_wrapper(openclaw_bin)
    if wrapped:
        return wrapped

    return [DEFAULT_OPENCLAW_BIN]


def default_openclaw_command() -> list[str]:
    return resolve_openclaw_command()


def openclaw_command(*args: str, explicit_bin: str | os.PathLike[str] | None = None) -> list[str]:
    return [*resolve_openclaw_command(explicit_bin), *args]


def local_native_platform_name() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "macos"


def _resolve_guard_path(path_value: str | os.PathLike[str]) -> Path:
    return Path(path_value).expanduser().resolve(strict=False)


def _path_within_root(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def sqlite_file_path_from_url(database_url: str | None) -> Path | None:
    rendered = str(database_url or "").strip()
    if not rendered or rendered in {":memory:", "sqlite:///:memory:", "sqlite+aiosqlite:///:memory:"}:
        return None
    prefix = "sqlite+aiosqlite://"
    if not rendered.startswith(prefix):
        return None
    path_part = rendered[len(prefix):]
    if not path_part:
        return None
    if path_part.startswith("///"):
        return _resolve_guard_path(f"//{path_part.lstrip('/')}")
    if len(path_part) >= 4 and path_part[0] == "/" and path_part[2] == ":" and path_part[3] == "/":
        return _resolve_guard_path(path_part.lstrip("/"))
    return _resolve_guard_path(f"/{path_part.lstrip('/')}")


def assert_isolated_test_runtime_paths(
    *,
    context: str,
    config_path: Path | None = None,
    runtime_env_path: Path | None = None,
    state_dir: Path | None = None,
    database_url: str | None = None,
) -> None:
    shared_config_path = _resolve_guard_path(DEFAULT_SHARED_CONFIG_PATH)
    shared_openclaw_state_root = _resolve_guard_path(DEFAULT_SHARED_OPENCLAW_ROOT / "state")
    shared_runtime_root = _resolve_guard_path(DEFAULT_SHARED_MEMORY_PALACE_ROOT)
    shared_database_path = _resolve_guard_path(DEFAULT_SHARED_MEMORY_PALACE_DB)
    violations: list[str] = []

    if config_path is not None and _resolve_guard_path(config_path) == shared_config_path:
        violations.append(f"config_path={config_path}")
    if runtime_env_path is not None and _path_within_root(_resolve_guard_path(runtime_env_path), shared_runtime_root):
        violations.append(f"runtime_env_path={runtime_env_path}")
    if state_dir is not None and _path_within_root(_resolve_guard_path(state_dir), shared_runtime_root):
        violations.append(f"state_dir={state_dir}")
    if state_dir is not None and _path_within_root(_resolve_guard_path(state_dir), shared_openclaw_state_root):
        violations.append(f"state_dir={state_dir}")
    database_path = sqlite_file_path_from_url(database_url)
    if database_path is not None and (
        database_path == shared_database_path or _path_within_root(database_path, shared_runtime_root)
    ):
        violations.append(f"database_url={database_url}")

    if violations:
        raise RuntimeError(
            f"{context} refuses to use shared OpenClaw runtime paths during automated tests: "
            + ", ".join(violations)
        )


def _command_exists(command: str) -> bool:
    return bool(shutil.which(command))


def _run_text(argv: list[str], *, timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def probe_embedding_dimension(
    base_url: str,
    model: str,
    *,
    api_key: str | None = None,
    dimensions: int | None = None,
    timeout_seconds: float = 30.0,
) -> int:
    payload: dict[str, Any] = {
        "model": model,
        "input": "memory palace profile smoke embedding dimension probe",
    }
    if isinstance(dimensions, int) and dimensions > 0:
        payload["dimensions"] = dimensions

    normalized_base = str(base_url or "").strip().rstrip("/")
    ok, detail = post_json_warmup(
        base_url=normalized_base,
        endpoint="/embeddings",
        payload=payload,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    if not ok:
        raise RuntimeError(f"embedding probe failed: {detail}")

    target = f"{normalized_base}/embeddings"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if str(api_key or "").strip():
        headers["Authorization"] = f"Bearer {str(api_key).strip()}"
    request = urllib_request.Request(target, data=data, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"embedding probe failed: {exc}") from exc

    data_rows = parsed.get("data") if isinstance(parsed, dict) else None
    first_row = data_rows[0] if isinstance(data_rows, list) and data_rows else None
    embedding = first_row.get("embedding") if isinstance(first_row, dict) else None
    if isinstance(embedding, list) and embedding:
        return len(embedding)

    if normalized_base == LOCAL_OLLAMA_EMBED_API_BASE:
        ollama_target = "http://127.0.0.1:11434/api/embed"
        ollama_request = urllib_request.Request(
            ollama_target,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(ollama_request, timeout=timeout_seconds) as response:
                parsed = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"embedding probe failed: {exc}") from exc
        embedding = parsed.get("embedding") if isinstance(parsed, dict) else None
        if isinstance(embedding, list) and embedding:
            return len(embedding)
        embeddings = parsed.get("embeddings") if isinstance(parsed, dict) else None
        first_embedding = embeddings[0] if isinstance(embeddings, list) and embeddings else None
        if isinstance(first_embedding, list) and first_embedding:
            return len(first_embedding)

    raise RuntimeError(f"embedding probe returned no embedding payload: {parsed!r}")


def ensure_local_ollama_embedding_alias() -> str:
    if not _command_exists("ollama"):
        raise RuntimeError("ollama is not installed; cannot switch embedding to local Ollama")
    listed = _run_text(["ollama", "list"], timeout=60)
    if listed.returncode != 0:
        raise RuntimeError(f"ollama list failed:\n{listed.stderr or listed.stdout}")
    if LOCAL_OLLAMA_EMBED_ALIAS in (listed.stdout or ""):
        return LOCAL_OLLAMA_EMBED_ALIAS
    if LOCAL_OLLAMA_EMBED_BASE_MODEL not in (listed.stdout or ""):
        raise RuntimeError(
            f"ollama model {LOCAL_OLLAMA_EMBED_BASE_MODEL} is missing; cannot create ctx-limited alias"
        )
    modelfile = "\n".join(
        [
            f"FROM {LOCAL_OLLAMA_EMBED_BASE_MODEL}",
            f"PARAMETER num_ctx {LOCAL_OLLAMA_EMBED_CTX_SIZE}",
            "",
        ]
    )
    created = _run_text(
        ["ollama", "create", LOCAL_OLLAMA_EMBED_ALIAS, "-f", "-"],
        input_text=modelfile,
        timeout=300,
    )
    if created.returncode != 0:
        raise RuntimeError(f"ollama create failed:\n{created.stderr or created.stdout}")
    return LOCAL_OLLAMA_EMBED_ALIAS


def should_apply_local_embedding_fallback(
    profile: str,
    prewarm_results: list[dict[str, str]],
) -> bool:
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile not in {"c", "d"}:
        return False
    for item in prewarm_results:
        if str(item.get("component") or "").strip().lower() != "embedding":
            continue
        if str(item.get("status") or "").strip().lower() != "fail":
            return False
        return is_transient_prewarm_failure(item.get("detail"))
    return False


def apply_local_embedding_fallback(
    env_values: dict[str, str],
    *,
    platform: str,
    target_dim: str = "1024",
) -> dict[str, str]:
    resolved = dict(env_values)
    embed_model = ensure_local_ollama_embedding_alias()
    try:
        requested_dim = int(
            str(target_dim or DEFAULT_PROFILE_EMBEDDING_DIM).strip()
            or DEFAULT_PROFILE_EMBEDDING_DIM
        )
    except ValueError:
        requested_dim = int(DEFAULT_PROFILE_EMBEDDING_DIM)
    actual_dim = probe_embedding_dimension(
        LOCAL_OLLAMA_EMBED_API_BASE,
        embed_model,
        api_key=LOCAL_OLLAMA_EMBED_API_KEY,
        dimensions=requested_dim,
        timeout_seconds=30.0,
    )
    resolved["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
    resolved["RETRIEVAL_EMBEDDING_API_BASE"] = LOCAL_OLLAMA_EMBED_API_BASE
    resolved["RETRIEVAL_EMBEDDING_API_KEY"] = LOCAL_OLLAMA_EMBED_API_KEY
    resolved["RETRIEVAL_EMBEDDING_MODEL"] = embed_model
    resolved["RETRIEVAL_EMBEDDING_DIM"] = str(actual_dim)
    if str(platform or "").strip().lower() == "docker":
        adapted = adapt_loopback_base_for_docker(resolved["RETRIEVAL_EMBEDDING_API_BASE"])
        if adapted:
            resolved["RETRIEVAL_EMBEDDING_API_BASE"] = adapted
    return resolved


def extract_index_command_ok(payload: dict[str, Any] | Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is True:
        return True
    result = payload.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("ok") is True:
        return True
    if (
        int(result.get("failure_count", 1) or 0) == 0
        and (
            "indexed_chunks" in result
            or "requested_memories" in result
            or "finished_at" in result
        )
    ):
        return True
    wait_result = result.get("wait_result")
    if isinstance(wait_result, dict):
        if wait_result.get("ok") is True:
            return True
        job = wait_result.get("job")
        if isinstance(job, dict) and str(job.get("status") or "").strip().lower() == "succeeded":
            return True
    return False


def first_non_blank_env_value(*values: str | None, skip_placeholders: bool = False) -> str:
    for value in values:
        rendered = str(value or "").strip()
        if skip_placeholders and is_placeholder_runtime_env_value(rendered):
            continue
        if rendered:
            return rendered
    return ""


def resolve_compatible_llm_env(model_env: dict[str, str]) -> dict[str, str]:
    return {
        "api_base": normalize_chat_base(
            first_non_blank_env_value(
                model_env.get("SMART_EXTRACTION_LLM_API_BASE"),
                model_env.get("WRITE_GUARD_LLM_API_BASE"),
                model_env.get("COMPACT_GIST_LLM_API_BASE"),
                model_env.get("LLM_API_BASE"),
                model_env.get("INTENT_LLM_API_BASE"),
                model_env.get("LLM_RESPONSES_URL"),
                model_env.get("OPENAI_BASE_URL"),
                model_env.get("OPENAI_API_BASE"),
                skip_placeholders=True,
            )
        )
        or "",
        "api_key": first_non_blank_env_value(
            model_env.get("SMART_EXTRACTION_LLM_API_KEY"),
            model_env.get("WRITE_GUARD_LLM_API_KEY"),
            model_env.get("COMPACT_GIST_LLM_API_KEY"),
            model_env.get("LLM_API_KEY"),
            model_env.get("INTENT_LLM_API_KEY"),
            model_env.get("OPENAI_API_KEY"),
            skip_placeholders=True,
        ),
        "model": first_non_blank_env_value(
            model_env.get("SMART_EXTRACTION_LLM_MODEL"),
            model_env.get("WRITE_GUARD_LLM_MODEL"),
            model_env.get("COMPACT_GIST_LLM_MODEL"),
            model_env.get("LLM_MODEL_NAME"),
            model_env.get("INTENT_LLM_MODEL"),
            model_env.get("OPENAI_MODEL"),
            model_env.get("LLM_MODEL"),
            skip_placeholders=True,
        ),
    }


@dataclass
class SmokeResult:
    mode: str
    profile: str
    status: str
    summary: str
    details: str = ""


@dataclass(frozen=True)
class StoredVisualRecord:
    path: str | None
    uri: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class VisualRegressionProbe:
    token: str
    query: str
    media_ref: str
    summary: str
    ocr: str
    scene: str
    why_relevant: str
    expected_get_fragments: tuple[str, ...] = ()
    forbidden_get_fragments: tuple[str, ...] = ()
    forbidden_db_fragments: tuple[str, ...] = ()


def register_active_process_group(pid: int) -> None:
    with _ACTIVE_PROCESS_GROUPS_LOCK:
        _ACTIVE_PROCESS_GROUPS.add(pid)


def unregister_active_process_group(pid: int) -> None:
    with _ACTIVE_PROCESS_GROUPS_LOCK:
        _ACTIVE_PROCESS_GROUPS.discard(pid)


def _force_kill_signal() -> signal.Signals:
    return getattr(signal, "SIGKILL", signal.SIGTERM)


def _kill_process_tree_windows(pid: int, *, force: bool) -> None:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "").lower()
    if any(
        marker in detail
        for marker in (
            "not found",
            "no running instance",
            "no instance",
            "cannot find the process",
            "does not exist",
        )
    ):
        return


def kill_process_group(
    pid: int,
    sig: signal.Signals,
    *,
    force: bool | None = None,
) -> None:
    sigkill = getattr(signal, "SIGKILL", None)
    force_kill = (
        (sigkill is not None and sig == sigkill)
        if force is None
        else bool(force)
    )
    try:
        if callable(_OS_KILLPG):
            _OS_KILLPG(pid, sig)
        elif os.name == "nt":
            _kill_process_tree_windows(pid, force=force_kill)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate_active_process_groups(*, force: bool = False) -> list[int]:
    with _ACTIVE_PROCESS_GROUPS_LOCK:
        pids = list(_ACTIVE_PROCESS_GROUPS)
    target_signal = _force_kill_signal() if force else signal.SIGTERM
    for pid in pids:
        kill_process_group(pid, target_signal, force=force)
    return pids


def handle_termination_signal(signum: int, _frame: object | None) -> None:
    terminate_active_process_groups(force=False)
    time.sleep(0.2)
    terminate_active_process_groups(force=True)
    raise SystemExit(128 + int(signum))


def install_signal_cleanup_handlers() -> None:
    global _SIGNAL_CLEANUP_INSTALLED
    if _SIGNAL_CLEANUP_INSTALLED:
        return
    for signum in (signal.SIGINT, signal.SIGTERM):
        if signal.getsignal(signum) is signal.SIG_IGN:
            continue
        signal.signal(signum, handle_termination_signal)
    _SIGNAL_CLEANUP_INSTALLED = True


def _prepare_subprocess_command(cmd: list[str]) -> list[str] | str:
    if os.name != "nt" or not cmd:
        return cmd
    executable = str(cmd[0] or "").strip().lower()
    if not executable.endswith((".cmd", ".bat")):
        return cmd
    rendered = " ".join(_quote_cmd_exe_arg(part) for part in cmd)
    return f"call {rendered}"


def _quote_cmd_exe_arg(value: str | os.PathLike[str]) -> str:
    rendered = str(value)
    if not rendered:
        return '""'
    needs_quotes = any(ch.isspace() for ch in rendered) or any(
        ch in rendered for ch in '&|()<>^'
    )
    escaped = rendered.replace('"', '""')
    return f'"{escaped}"' if needs_quotes else escaped


def run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    stdout_fd, stdout_path = tempfile.mkstemp(prefix="mp-profile-smoke-stdout-", suffix=".log")
    stderr_fd, stderr_path = tempfile.mkstemp(prefix="mp-profile-smoke-stderr-", suffix=".log")
    launch_cmd = _prepare_subprocess_command(cmd)
    use_shell = isinstance(launch_cmd, str)
    try:
        with os.fdopen(stdout_fd, "w+", encoding="utf-8") as stdout_handle, os.fdopen(
            stderr_fd, "w+", encoding="utf-8"
        ) as stderr_handle:
            process = subprocess.Popen(
                launch_cmd,
                env=env,
                cwd=str(cwd) if cwd else None,
                shell=use_shell,
                text=True,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            register_active_process_group(process.pid)
            try:
                stdout_data, stderr_data = process.communicate(timeout=timeout)
                _ = stdout_data
                _ = stderr_data
            except subprocess.TimeoutExpired as exc:
                kill_process_group(process.pid, signal.SIGTERM, force=False)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    kill_process_group(process.pid, _force_kill_signal(), force=True)
                    process.wait(timeout=5)
                raise exc
            finally:
                unregister_active_process_group(process.pid)
            stdout_handle.flush()
            stderr_handle.flush()
            stdout_handle.seek(0)
            stderr_handle.seek(0)
            return subprocess.CompletedProcess(
                cmd,
                process.returncode,
                stdout=stdout_handle.read(),
                stderr=stderr_handle.read(),
            )
    finally:
        for temp_path in (stdout_path, stderr_path):
            _remove_temp_log_file(temp_path)


def _remove_temp_log_file(path: str, *, retries: int = 12, delay_seconds: float = 0.25) -> None:
    try:
        os.unlink(path)
        return
    except FileNotFoundError:
        return
    except PermissionError:
        if os.name != "nt":
            return

    def _retry_remove() -> None:
        for _ in range(retries):
            try:
                os.unlink(path)
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(delay_seconds)
            except OSError:
                return

    threading.Thread(target=_retry_remove, name="mp-profile-smoke-log-cleanup", daemon=True).start()


def _read_text_with_fallback_encodings(path: Path) -> str:
    encodings = [
        "utf-8",
        "utf-8-sig",
        locale.getpreferredencoding(False) or "",
        "gb18030",
        "gbk",
        "cp936",
    ]
    attempted: set[str] = set()
    last_error: UnicodeDecodeError | None = None
    for encoding in encodings:
        normalized = str(encoding or "").strip()
        if not normalized or normalized in attempted:
            continue
        attempted.add(normalized)
        try:
            return path.read_text(encoding=normalized)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        return path.read_text(encoding="utf-8", errors="replace")
    return path.read_text(encoding="utf-8")


def load_env_file(path: Path) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    data: dict[str, str] = {}
    for raw in _read_text_with_fallback_encodings(path).splitlines():
        line = raw.lstrip("\ufeff").strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized_key):
            continue
        stripped = value.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
            stripped = stripped[1:-1]
        data[normalized_key] = stripped
    return normalize_test_model_env_aliases(data)


def normalize_test_model_env_aliases(values: dict[str, str]) -> dict[str, str]:
    data = dict(values)
    alias_pairs = (
        ("OPENCLAW_TEST_EMBEDDING_API_BASE", "RETRIEVAL_EMBEDDING_API_BASE"),
        ("OPENCLAW_TEST_EMBEDDING_API_KEY", "RETRIEVAL_EMBEDDING_API_KEY"),
        ("OPENCLAW_TEST_EMBEDDING_MODEL", "RETRIEVAL_EMBEDDING_MODEL"),
        ("OPENCLAW_TEST_EMBEDDING_DIM", "RETRIEVAL_EMBEDDING_DIM"),
        ("OPENCLAW_TEST_RERANKER_API_BASE", "RETRIEVAL_RERANKER_API_BASE"),
        ("OPENCLAW_TEST_RERANKER_API_KEY", "RETRIEVAL_RERANKER_API_KEY"),
        ("OPENCLAW_TEST_RERANKER_MODEL", "RETRIEVAL_RERANKER_MODEL"),
        ("OPENCLAW_TEST_LLM_API_BASE", "LLM_API_BASE"),
        ("OPENCLAW_TEST_LLM_API_KEY", "LLM_API_KEY"),
        ("OPENCLAW_TEST_LLM_MODEL", "LLM_MODEL_NAME"),
    )
    for alias_key, runtime_key in alias_pairs:
        alias_value = str(data.get(alias_key) or "").strip()
        if alias_value and runtime_key not in data:
            data[runtime_key] = alias_value
    return data


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def normalize_host_cli_path(value: str | os.PathLike[str]) -> Path:
    raw = str(value or "").strip()
    if not raw:
        return Path(raw)
    if os.name != "nt":
        return Path(raw)
    mnt_match = re.match(r"^/mnt/([A-Za-z])/(.*)$", raw)
    if mnt_match:
        drive = mnt_match.group(1).upper()
        remainder = mnt_match.group(2)
        return Path(f"{drive}:/{remainder}")
    root_drive_match = re.match(r"^/([A-Za-z])/(.*)$", raw)
    if root_drive_match:
        drive = root_drive_match.group(1).upper()
        remainder = root_drive_match.group(2)
        return Path(f"{drive}:/{remainder}")
    return Path(raw)


def shell_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if os.name == "nt":
        cygpath = shutil.which("cygpath")
        if cygpath:
            proc = subprocess.run(
                [cygpath, "-u", str(resolved)],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=15,
            )
            converted = str(proc.stdout or "").strip()
            if proc.returncode == 0 and converted:
                return converted
        text = str(resolved).replace("\\", "/")
        drive_match = re.match(r"^([A-Za-z]):/(.*)$", text)
        if drive_match:
            drive = drive_match.group(1).lower()
            remainder = drive_match.group(2)
            return f"/mnt/{drive}/{remainder}"
    return resolved.as_posix()


def _repo_python_candidates() -> list[Path]:
    return [
        PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
        PROJECT_ROOT / ".venv" / "bin" / "python",
        BACKEND_ROOT / ".venv" / "Scripts" / "python.exe",
        BACKEND_ROOT / ".venv" / "bin" / "python",
        Path(sys.executable),
    ]


def resolve_repo_python_executable() -> str:
    for candidate in _repo_python_candidates():
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def sqlite_url_for_file(path: Path) -> str:
    raw_rendered = str(path.expanduser()).replace("\\", "/")
    if raw_rendered.startswith("//"):
        return f"sqlite+aiosqlite://///{raw_rendered.lstrip('/')}"
    if len(raw_rendered) >= 3 and raw_rendered[1] == ":" and raw_rendered[2] == "/":
        return f"sqlite+aiosqlite:///{raw_rendered}"
    resolved = path.expanduser().resolve()
    rendered = str(resolved).replace("\\", "/")
    if rendered.startswith("//"):
        return f"sqlite+aiosqlite://///{rendered.lstrip('/')}"
    if len(rendered) >= 3 and rendered[1] == ":" and rendered[2] == "/":
        return f"sqlite+aiosqlite:///{rendered}"
    return f"sqlite+aiosqlite:////{rendered.lstrip('/')}"


def ensure_mcp_api_key(values: dict[str, str], *, platform: str, profile: str) -> str:
    existing = str(values.get("MCP_API_KEY") or "").strip()
    if existing:
        return existing
    generated = f"smoke-{platform}-{profile}-api-key"
    values["MCP_API_KEY"] = generated
    return generated


def normalize_embedding_base(raw: str | None) -> str | None:
    if not raw:
        return raw
    value = raw.strip()
    if value.endswith("/embeddings"):
        return value[: -len("/embeddings")]
    return value


def normalize_reranker_base(raw: str | None) -> str | None:
    if not raw:
        return raw
    value = raw.strip()
    if value.endswith("/rerank"):
        return value[: -len("/rerank")]
    return value


def normalize_chat_base(raw: str | None) -> str | None:
    if not raw:
        return raw
    value = raw.strip()
    lowered = value.lower()
    for suffix in ("/chat/completions", "/responses"):
        if lowered.endswith(suffix):
            return value[: -len(suffix)]
    return value


def post_json_warmup(
    *,
    base_url: str | None,
    endpoint: str,
    payload: dict[str, Any],
    api_key: str | None,
    timeout_seconds: float,
) -> tuple[bool, str]:
    normalized_base = str(base_url or "").strip().rstrip("/")
    if not normalized_base:
        return False, "base url missing"
    target = f"{normalized_base}{endpoint}"
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if str(api_key or "").strip():
        headers["Authorization"] = f"Bearer {str(api_key).strip()}"
    request = urllib_request.Request(target, data=data, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            response.read(256)
            return True, f"http {response.status}"
    except urllib_error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:160]
        except Exception:
            detail = ""
        return False, f"http {exc.code}{(': ' + detail) if detail else ''}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def prewarm_profile_model_backends(
    profile: str,
    env_values: dict[str, str],
    *,
    post_probe=post_json_warmup,
    timeout_seconds: float = 60.0,
    retry_attempts: int = 2,
    retry_delay_seconds: float = 1.0,
) -> list[dict[str, str]]:
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile not in {"c", "d"}:
        return []

    tasks = [
        {
            "component": "embedding",
            "base_url": normalize_embedding_base(env_values.get("RETRIEVAL_EMBEDDING_API_BASE")),
            "endpoint": "/embeddings",
            "payload": {
                "model": env_values.get("RETRIEVAL_EMBEDDING_MODEL"),
                "input": "memory palace prewarm probe",
            },
            "api_key": env_values.get("RETRIEVAL_EMBEDDING_API_KEY"),
        },
        {
            "component": "reranker",
            "base_url": normalize_reranker_base(env_values.get("RETRIEVAL_RERANKER_API_BASE")),
            "endpoint": "/rerank",
            "payload": {
                "model": env_values.get("RETRIEVAL_RERANKER_MODEL"),
                "query": "memory palace prewarm probe",
                "documents": ["probe document one", "probe document two"],
            },
            "api_key": env_values.get("RETRIEVAL_RERANKER_API_KEY"),
        },
        {
            "component": "write_guard_llm",
            "base_url": normalize_chat_base(env_values.get("WRITE_GUARD_LLM_API_BASE")),
            "endpoint": "/chat/completions",
            "payload": {
                "model": env_values.get("WRITE_GUARD_LLM_MODEL"),
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "Reply with JSON only."},
                    {"role": "user", "content": "Return {\"ok\":true}."},
                ],
            },
            "api_key": env_values.get("WRITE_GUARD_LLM_API_KEY"),
        },
        {
            "component": "compact_gist_llm",
            "base_url": normalize_chat_base(env_values.get("COMPACT_GIST_LLM_API_BASE")),
            "endpoint": "/chat/completions",
            "payload": {
                "model": env_values.get("COMPACT_GIST_LLM_MODEL"),
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "Reply with JSON only."},
                    {"role": "user", "content": "Return {\"ok\":true}."},
                ],
            },
            "api_key": env_values.get("COMPACT_GIST_LLM_API_KEY"),
        },
    ]

    results: list[dict[str, str]] = []
    for task in tasks:
        if not str(task["base_url"] or "").strip():
            continue
        ok = False
        detail = ""
        elapsed = 0.0
        for attempt in range(1, max(1, retry_attempts) + 1):
            started = time.time()
            ok, detail = post_probe(
                base_url=task["base_url"],
                endpoint=task["endpoint"],
                payload=task["payload"],
                api_key=task["api_key"],
                timeout_seconds=timeout_seconds,
            )
            elapsed = time.time() - started
            if ok or attempt >= max(1, retry_attempts) or not is_transient_prewarm_failure(detail):
                break
            time.sleep(max(0.0, retry_delay_seconds))
        print(
            f"[prewarm] {task['component']} {'pass' if ok else 'fail'} elapsed={elapsed:.1f}s detail={detail}",
            file=sys.stderr,
            flush=True,
        )
        results.append(
            {
                "component": str(task["component"]),
                "status": "pass" if ok else "fail",
                "detail": detail,
            }
        )
    return results


def ensure_successful_prewarm_results(prewarm_results: list[dict[str, str]]) -> None:
    for item in prewarm_results:
        if str(item.get("status") or "").strip().lower() != "fail":
            continue
        component = str(item.get("component") or "model").strip() or "model"
        detail = str(item.get("detail") or item.get("error") or "").strip()
        raise RuntimeError(
            f"{component} prewarm failed{(': ' + detail) if detail else ''}"
        )


def is_transient_prewarm_failure(detail: str | None) -> bool:
    normalized = str(detail or "").strip().lower()
    if not normalized:
        return False
    return any(
        marker in normalized
        for marker in (
            "timed out",
            "timeout",
            "connection reset",
            "connection aborted",
            "temporarily unavailable",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
        )
    )


def adapt_loopback_base_for_docker(raw: str | None) -> str | None:
    if not raw:
        return raw
    value = raw.strip()
    if not value:
        return value
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost"}:
        return value
    netloc = parsed.netloc.replace(host, "host.docker.internal", 1)
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


_PROFILE_RUNTIME_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "c": (
        "RETRIEVAL_EMBEDDING_API_BASE",
        "RETRIEVAL_EMBEDDING_API_KEY",
        "RETRIEVAL_EMBEDDING_MODEL",
        "RETRIEVAL_EMBEDDING_DIM",
        "RETRIEVAL_RERANKER_API_BASE",
        "RETRIEVAL_RERANKER_API_KEY",
        "RETRIEVAL_RERANKER_MODEL",
    ),
    "d": (
        "RETRIEVAL_EMBEDDING_API_BASE",
        "RETRIEVAL_EMBEDDING_API_KEY",
        "RETRIEVAL_EMBEDDING_MODEL",
        "RETRIEVAL_EMBEDDING_DIM",
        "RETRIEVAL_RERANKER_API_BASE",
        "RETRIEVAL_RERANKER_API_KEY",
        "RETRIEVAL_RERANKER_MODEL",
        "WRITE_GUARD_LLM_API_BASE",
        "WRITE_GUARD_LLM_API_KEY",
        "WRITE_GUARD_LLM_MODEL",
    ),
}
DEFAULT_PROFILE_EMBEDDING_DIM = "1024"


def is_placeholder_runtime_env_value(raw: str | None) -> bool:
    value = str(raw or "").strip()
    if not value:
        return True
    lowered = value.lower()
    return any(
        marker in lowered
        for marker in (
            "replace-with-your-key",
            "replace-with-your-model",
            "replace-with-your-llm-model",
            "<your-router-host>",
            "127.0.0.1:port",
            "https://<",
            "http://<",
        )
    )


def validate_profile_runtime_env(profile: str, values: dict[str, str]) -> None:
    required_keys = _PROFILE_RUNTIME_REQUIRED_KEYS.get(profile, ())
    if not required_keys:
        return
    missing_keys = [key for key in required_keys if is_placeholder_runtime_env_value(values.get(key))]
    if missing_keys:
        joined = ", ".join(missing_keys)
        raise ValueError(
            f"Profile {profile.upper()} requires runtime model env for: {joined}"
        )


def _env_value_is_truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def should_configure_memory_llm(profile: str, model_env: Mapping[str, str]) -> bool:
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile == "d":
        return True
    if any(
        _env_value_is_truthy(model_env.get(key))
        for key in ("WRITE_GUARD_LLM_ENABLED", "COMPACT_GIST_LLM_ENABLED")
    ):
        return True
    return any(
        str(model_env.get(key) or "").strip()
        for key in (
            "WRITE_GUARD_LLM_API_BASE",
            "WRITE_GUARD_LLM_API_KEY",
            "WRITE_GUARD_LLM_MODEL",
            "COMPACT_GIST_LLM_API_BASE",
            "COMPACT_GIST_LLM_API_KEY",
            "COMPACT_GIST_LLM_MODEL",
        )
    )


def slugify_token(raw: str, *, fallback: str) -> str:
    lowered = raw.strip().lower()
    slug = re.sub(r"[^a-z0-9-]+", "-", lowered).strip("-")
    return slug or fallback


def build_openclaw_env(*, config_path: Path, state_dir: Path) -> dict[str, str]:
    assert_isolated_test_runtime_paths(
        context="build_openclaw_env",
        config_path=config_path,
        state_dir=state_dir,
    )
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    return env


def parse_json_stdout(stdout: str) -> dict[str, Any]:
    payload = extract_last_json_from_text(stdout)
    return payload if isinstance(payload, dict) else {"value": payload}


def parse_json_output_streams(stdout: str, stderr: str) -> dict[str, Any]:
    payload = extract_json_from_streams(stdout, stderr)
    return payload if isinstance(payload, dict) else {"value": payload}


def is_transient_lock_output(stdout: str, stderr: str) -> bool:
    text = f"{stdout}\n{stderr}".lower()
    return any(marker in text for marker in TRANSIENT_LOCK_MARKERS)


def extract_path_or_uri(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    path = payload.get("path")
    uri = payload.get("uri")
    return (
        str(path) if isinstance(path, str) and path.strip() else None,
        str(uri) if isinstance(uri, str) and uri.strip() else None,
    )


def run_openclaw_json_command(
    command: list[str],
    *,
    config_path: Path,
    state_dir: Path,
    timeout: int = 300,
    max_attempts: int = 8,
    base_sleep_seconds: float = 1.0,
) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    isolated_state_dir = Path(
        tempfile.mkdtemp(prefix="openclaw-step-", dir=str(state_dir))
    )
    try:
        env = build_openclaw_env(config_path=config_path, state_dir=isolated_state_dir)
        proc: subprocess.CompletedProcess[str] | None = None
        for attempt in range(1, max_attempts + 1):
            proc = run(command, env=env, cwd=PROJECT_ROOT, timeout=timeout)
            if proc.returncode == 0:
                if not str(proc.stdout or "").strip() and not str(proc.stderr or "").strip():
                    return {}
                return parse_json_output_streams(proc.stdout, proc.stderr)
            if is_transient_lock_output(proc.stdout or "", proc.stderr or "") and attempt < max_attempts:
                time.sleep(base_sleep_seconds * attempt)
                continue
            raise RuntimeError(
                "openclaw command failed:\n"
                f"COMMAND: {' '.join(shlex.quote(part) for part in command)}\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        raise RuntimeError("openclaw command runner exited without executing a command")
    finally:
        shutil.rmtree(isolated_state_dir, ignore_errors=True)


def ensure_visual_search_hits(
    payload: dict[str, Any],
    *,
    expected_path: str | None = None,
) -> None:
    visual_hits = payload.get("results") if isinstance(payload, dict) else None
    assert isinstance(visual_hits, list) and visual_hits, payload
    assert any(
        "whiteboard" in str(item.get("snippet", "")).lower()
        or "sha256-" in str(item.get("path", "")).lower()
        or "/visual/" in str(item.get("path", "")).lower()
        or "visual" in str(item.get("path", "")).lower()
        for item in visual_hits
        if isinstance(item, dict)
    ), payload
    if expected_path:
        assert any(
            isinstance(item, dict) and str(item.get("path") or "") == expected_path
            for item in visual_hits
        ), payload


def _profile_template_path(platform: str, profile: str) -> Path:
    normalized_platform = str(platform or "").strip().lower()
    normalized_profile = str(profile or "").strip().lower()
    return (
        PROJECT_ROOT
        / "deploy"
        / "profiles"
        / normalized_platform
        / f"profile-{normalized_profile}.env"
    )


def _build_profile_seed(platform: str, profile: str) -> dict[str, str]:
    base_env = PROJECT_ROOT / ".env.example"
    profile_env = _profile_template_path(platform, profile)
    if not base_env.is_file():
        raise RuntimeError(f"Missing base env template: {base_env}")
    if not profile_env.is_file():
        raise RuntimeError(f"Missing profile template: {profile_env}")
    data = load_env_file(base_env)
    data.update(load_env_file(profile_env))
    return data


def resolve_embedding_model_for_profile(model_env: dict[str, str]) -> str:
    retrieval_model = str(model_env.get("RETRIEVAL_EMBEDDING_MODEL") or "").strip()
    alias_model = str(model_env.get("EMBEDDINGS_MODEL") or "").strip()
    retrieval_backend = str(model_env.get("RETRIEVAL_EMBEDDING_BACKEND") or "").strip().lower()
    if alias_model and (
        not retrieval_model
        or retrieval_model == "hash-v1"
        or retrieval_backend in {"hash", "none"}
    ):
        return alias_model
    return retrieval_model or alias_model


def build_profile_env(platform: str, profile: str, target: Path, model_env: dict[str, str]) -> dict[str, str]:
    model_env = normalize_test_model_env_aliases(model_env)
    data = _build_profile_seed(platform, profile)
    data["OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED"] = str(profile or "").strip().lower() or "b"
    data["OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE"] = str(profile or "").strip().lower() or "b"
    if platform != "docker":
        db_path = target.with_suffix(".db")
        if db_path.exists():
            db_path.unlink()
        data["DATABASE_URL"] = sqlite_url_for_file(db_path)
    assert_isolated_test_runtime_paths(
        context="build_profile_env",
        runtime_env_path=target,
        database_url=data.get("DATABASE_URL"),
    )

    ensure_mcp_api_key(data, platform=platform, profile=profile)

    for key in [
        "OPENAI_MODEL",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_API_KEY",
        "LLM_MODEL_NAME",
        "LLM_RESPONSES_URL",
        "LLM_API_KEY",
        "LLM_REASONING_EFFORT",
        "WRITE_GUARD_LLM_MODEL",
        "WRITE_GUARD_LLM_API_BASE",
        "WRITE_GUARD_LLM_API_KEY",
        "WRITE_GUARD_LLM_ENABLED",
        "COMPACT_GIST_LLM_MODEL",
        "COMPACT_GIST_LLM_API_BASE",
        "COMPACT_GIST_LLM_API_KEY",
        "COMPACT_GIST_LLM_ENABLED",
        "ROUTER_API_BASE",
        "ROUTER_API_KEY",
        "RETRIEVAL_RERANKER_API_BASE",
        "RETRIEVAL_RERANKER_API_KEY",
        "RETRIEVAL_RERANKER_MODEL",
        "RETRIEVAL_RERANKER_WEIGHT",
        "RETRIEVAL_RERANKER_FALLBACK_API_BASE",
        "RETRIEVAL_RERANKER_FALLBACK_API_KEY",
        "RETRIEVAL_RERANKER_FALLBACK_MODEL",
        "RETRIEVAL_RERANKER_FALLBACK_PROVIDER",
        "RETRIEVAL_RERANKER_FALLBACK_TIMEOUT_SEC",
    ]:
        if key in model_env:
            data[key] = model_env[key]

    data["RUNTIME_SLEEP_CONSOLIDATION_ENABLED"] = "false"
    data["RUNTIME_AUTO_FLUSH_ENABLED"] = "false"

    if profile in {"c", "d"}:
        data["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
        resolved_embedding_api_key = (
            model_env.get("RETRIEVAL_EMBEDDING_API_KEY")
            or model_env.get("EMBEDDINGS_API_KEY")
        )
        resolved_embedding_model = resolve_embedding_model_for_profile(model_env)
        if resolved_embedding_api_key:
            data["RETRIEVAL_EMBEDDING_API_KEY"] = str(resolved_embedding_api_key).strip()
        if resolved_embedding_model:
            data["RETRIEVAL_EMBEDDING_MODEL"] = str(resolved_embedding_model).strip()
        if "RETRIEVAL_EMBEDDING_DIM" in model_env:
            data["RETRIEVAL_EMBEDDING_DIM"] = model_env["RETRIEVAL_EMBEDDING_DIM"]
        if not str(data.get("RETRIEVAL_EMBEDDING_DIM") or "").strip():
            data["RETRIEVAL_EMBEDDING_DIM"] = DEFAULT_PROFILE_EMBEDDING_DIM
        normalized_base = normalize_embedding_base(
            model_env.get("RETRIEVAL_EMBEDDING_API_BASE")
            or model_env.get("EMBEDDINGS_BASE_URL")
        )
        if normalized_base:
            data["RETRIEVAL_EMBEDDING_API_BASE"] = normalized_base
        if platform == "docker":
            for key in (
                "RETRIEVAL_EMBEDDING_API_BASE",
                "RETRIEVAL_RERANKER_API_BASE",
                "RETRIEVAL_RERANKER_FALLBACK_API_BASE",
                "ROUTER_API_BASE",
                "LLM_API_BASE",
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
                "WRITE_GUARD_LLM_API_BASE",
                "COMPACT_GIST_LLM_API_BASE",
            ):
                adapted = adapt_loopback_base_for_docker(data.get(key))
                if adapted:
                    data[key] = adapted

    if should_configure_memory_llm(profile, model_env):
        resolved_llm = resolve_compatible_llm_env(model_env)
        resolved_llm_api_base = resolved_llm["api_base"]
        resolved_llm_api_key = resolved_llm["api_key"]
        resolved_llm_model = resolved_llm["model"]
        if resolved_llm_api_base:
            if "SMART_EXTRACTION_LLM_API_BASE" not in model_env:
                data["SMART_EXTRACTION_LLM_API_BASE"] = str(
                    resolved_llm_api_base
                ).strip()
            data["WRITE_GUARD_LLM_API_BASE"] = str(
                model_env.get("WRITE_GUARD_LLM_API_BASE") or resolved_llm_api_base
            ).strip()
            if "COMPACT_GIST_LLM_API_BASE" not in model_env:
                data["COMPACT_GIST_LLM_API_BASE"] = str(resolved_llm_api_base).strip()
        if resolved_llm_api_key:
            if "SMART_EXTRACTION_LLM_API_KEY" not in model_env:
                data["SMART_EXTRACTION_LLM_API_KEY"] = str(
                    resolved_llm_api_key
                ).strip()
            data["WRITE_GUARD_LLM_API_KEY"] = str(
                model_env.get("WRITE_GUARD_LLM_API_KEY") or resolved_llm_api_key
            ).strip()
            if "COMPACT_GIST_LLM_API_KEY" not in model_env:
                data["COMPACT_GIST_LLM_API_KEY"] = str(resolved_llm_api_key).strip()
        if resolved_llm_model:
            if "SMART_EXTRACTION_LLM_MODEL" not in model_env:
                data["SMART_EXTRACTION_LLM_MODEL"] = str(
                    resolved_llm_model
                ).strip()
            data["WRITE_GUARD_LLM_MODEL"] = str(
                model_env.get("WRITE_GUARD_LLM_MODEL") or resolved_llm_model
            ).strip()
            if "COMPACT_GIST_LLM_MODEL" not in model_env:
                data["COMPACT_GIST_LLM_MODEL"] = str(resolved_llm_model).strip()

        if platform == "docker":
            for key in (
                "SMART_EXTRACTION_LLM_API_BASE",
                "WRITE_GUARD_LLM_API_BASE",
                "COMPACT_GIST_LLM_API_BASE",
            ):
                adapted = adapt_loopback_base_for_docker(data.get(key))
                if adapted:
                    data[key] = adapted

    validate_profile_runtime_env(profile, data)
    write_env_file(target, data)
    return data


def build_openclaw_config(
    path: Path,
    *,
    transport: str,
    workspace_dir: Path | None = None,
    stdio_env: dict[str, str] | None = None,
    sse_url: str | None = None,
    sse_api_key: str | None = None,
) -> None:
    assert_isolated_test_runtime_paths(
        context="build_openclaw_config",
        config_path=path,
        database_url=(stdio_env or {}).get("DATABASE_URL") if transport == "stdio" else None,
    )
    resolved_workspace = (workspace_dir or (path.parent / "workspace")).resolve()
    config: dict[str, Any] = {
        "plugins": {
            "allow": ["memory-palace"],
            "load": {"paths": [str(OPENCLAW_PLUGIN_ROOT)]},
            "slots": {"memory": "memory-palace"},
            "entries": {
                "memory-palace": {
                    "enabled": True,
                    "config": {
                        "transport": transport,
                        "timeoutMs": 120000,
                    },
                }
            },
        },
        "agents": {
            "list": [
                {
                    "id": "main",
                    "default": True,
                    "workspace": str(resolved_workspace),
                }
            ]
        },
    }
    plugin_cfg = config["plugins"]["entries"]["memory-palace"]["config"]
    if transport == "stdio":
        plugin_cfg["stdio"] = {"env": stdio_env or {}}
    else:
        plugin_cfg["sse"] = {"url": sse_url, "apiKey": sse_api_key}
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2))
    try:
        path.chmod(0o600)
    except OSError:
        pass


def seed_local_memory(database_url: str, *, env_values: dict[str, str] | None = None) -> None:
    script = f"""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, {str(BACKEND_ROOT)!r})
from db.sqlite_client import SQLiteClient, Base

async def main():
    client = SQLiteClient({database_url!r})
    async with client.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await client.create_memory(
        parent_path='',
        content='用户偏好简洁回答；openclaw memory palace smoke',
        priority=1,
        title='preference_concise',
        disclosure='在生成回答时应用',
        domain='core',
    )
    await client.close()

asyncio.run(main())
"""
    env = os.environ.copy()
    if env_values:
        env.update({key: str(value) for key, value in env_values.items()})
    env["DATABASE_URL"] = database_url
    proc = run([resolve_repo_python_executable(), "-c", script], env=env, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)


def seed_docker_memory(env_file: Path, compose_project_name: str) -> None:
    script = """
import asyncio, os, sys
sys.path.insert(0, '/app')
from db.sqlite_client import SQLiteClient, Base

async def main():
    client = SQLiteClient(os.environ['DATABASE_URL'])
    async with client.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await client.create_memory(
        parent_path='',
        content='用户偏好简洁回答；openclaw memory palace smoke',
        priority=1,
        title='preference_concise',
        disclosure='在生成回答时应用',
        domain='core',
    )
    await client.close()

asyncio.run(main())
"""
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = compose_project_name
    proc = _run_docker_backend_exec(
        env=env,
        compose_project_name=compose_project_name,
        command_args=["python", "-c", script],
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)


def run_openclaw_smoke(
    *,
    config_path: Path,
    state_dir: Path,
    query_text: str,
    visual_query: str,
) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    visual_probe_path = state_dir / "smoke-whiteboard.png"
    ensure_probe_image(visual_probe_path)
    visual_probe_uri = visual_probe_path.resolve().as_uri()

    commands = {
        "plugins_info": openclaw_command("plugins", "inspect", "memory-palace", "--json"),
        "status_slot": openclaw_command("status", "--json"),
        "memory_status": openclaw_command("memory-palace", "status", "--json"),
        "memory_verify": openclaw_command("memory-palace", "verify", "--json"),
        "memory_doctor": openclaw_command("memory-palace", "doctor", "--query", query_text, "--json"),
        "memory_search": openclaw_command("memory-palace", "search", query_text, "--json"),
        "memory_get": openclaw_command(
            "memory-palace",
            "get",
            "memory-palace/core/preference_concise.md",
            "--json",
        ),
        "memory_store_visual": openclaw_command(
            "memory-palace",
            "store-visual",
            "--media-ref",
            visual_probe_uri,
            "--summary",
            "whiteboard launch plan",
            "--ocr",
            "launch checklist",
            "--entities",
            "Alice,whiteboard",
            "--source-channel",
            "discord",
            "--why-relevant",
            "planning reference",
            "--json",
        ),
        "memory_index": openclaw_command("memory-palace", "index", "--wait", "--json"),
        "visual_search": openclaw_command("memory-palace", "search", visual_query, "--json"),
    }
    timeouts = {
        "plugins_info": 90,
        "status_slot": 90,
        "memory_status": 90,
        "memory_verify": 120,
        "memory_doctor": 120,
        "memory_search": 90,
        "memory_get": 90,
        "memory_store_visual": 120,
        "memory_index": 180,
        "visual_search": 90,
    }

    for name, command in commands.items():
        started = time.time()
        print(f"[smoke-step] start {name}", file=sys.stderr, flush=True)
        try:
            checks[name] = run_openclaw_json_command(
                command,
                config_path=config_path,
                state_dir=state_dir,
                timeout=timeouts.get(name, 120),
            )
        except Exception as exc:
            elapsed = time.time() - started
            print(
                f"[smoke-step] fail {name} elapsed={elapsed:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            detail = str(exc).strip()
            if detail:
                raise RuntimeError(f"smoke step {name} failed: {detail}") from exc
            raise RuntimeError(f"smoke step {name} failed") from exc
        elapsed = time.time() - started
        print(
            f"[smoke-step] pass {name} elapsed={elapsed:.1f}s",
            file=sys.stderr,
            flush=True,
        )

    return checks


def run_compact_context_reflection_probe(
    profile: str,
    model_env: dict[str, str],
) -> dict[str, Any]:
    script_path = PROJECT_ROOT / "scripts" / "openclaw_compact_context_reflection_e2e.py"
    with tempfile.TemporaryDirectory(prefix=f"mp-compact-context-{profile}-") as tmp:
        tmp_path = Path(tmp)
        model_env_path = tmp_path / "model.env"
        report_path = tmp_path / "compact-context-reflection.json"
        write_env_file(model_env_path, model_env)
        proc = run(
            [
                resolve_repo_python_executable(),
                str(script_path),
                "--profile",
                profile,
                "--model-env",
                str(model_env_path),
                "--report",
                str(report_path),
            ],
            cwd=PROJECT_ROOT,
            env=os.environ.copy(),
            timeout=900,
        )
        payload = parse_json_output_streams(proc.stdout, proc.stderr)
        if proc.returncode != 0 or not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError(
                "compact_context reflection probe failed:\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        return payload


def ensure_probe_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(TRANSPARENT_PNG_BASE64))


def validate_local_outputs(outputs: dict[str, dict[str, Any]]) -> None:
    plugins_info = outputs["plugins_info"]
    plugin_record = (
        plugins_info.get("plugin")
        if isinstance(plugins_info.get("plugin"), dict)
        else plugins_info
    )
    assert plugin_record["status"] == "loaded"
    assert "memory_search" in plugin_record["toolNames"]
    assert "memory_get" in plugin_record["toolNames"]

    status = outputs["status_slot"]
    assert status["memoryPlugin"]["slot"] == "memory-palace"

    memory_status = outputs["memory_status"]
    assert memory_status["status"]["ok"] is True

    verify_payload = outputs["memory_verify"]
    assert verify_payload["ok"] is True

    doctor_payload = outputs["memory_doctor"]
    assert doctor_payload["ok"] is True

    search = outputs["memory_search"]
    assert search["results"], search
    assert search["results"][0]["path"] == "memory-palace/core/preference_concise.md"

    get_payload = outputs["memory_get"]
    assert "用户偏好简洁回答" in get_payload["text"]

    visual_store = outputs["memory_store_visual"]
    assert isinstance(visual_store, dict), visual_store
    assert visual_store.get("runtime_visual_probe") == "cli_store_visual_only", visual_store

    index_payload = outputs["memory_index"]
    assert extract_index_command_ok(index_payload), index_payload

    ensure_visual_search_hits(outputs["visual_search"])


def build_docker_visual_probe(profile: str, *, token_seed: str) -> VisualRegressionProbe:
    token = slugify_token(
        f"docker-visual-probe-{profile}-{token_seed}",
        fallback=f"docker-visual-probe-{profile}",
    )
    raw_blob = base64.b64encode(((token + "|") * 12).encode("utf-8")).decode("ascii")
    media_ref = f"data:image/png;base64,{raw_blob}"
    return VisualRegressionProbe(
        token=token,
        query=token,
        media_ref=media_ref,
        summary=f"docker visual persistence probe {token}",
        ocr=f"{token} release continuity board",
        scene=f"compose restart validation wall {token}",
        why_relevant=f"persist visual record across docker compose down up for {token}",
        expected_get_fragments=("media_ref: data:image/png;sha256-",),
        forbidden_get_fragments=(media_ref, "data:image/png;base64,"),
        forbidden_db_fragments=(media_ref, raw_blob, "data:image/png;base64,"),
    )


def store_visual_probe(
    *,
    config_path: Path,
    state_dir: Path,
    probe: VisualRegressionProbe,
) -> StoredVisualRecord:
    payload = run_openclaw_json_command(
        openclaw_command(
            "memory-palace",
            "store-visual",
            "--media-ref",
            probe.media_ref,
            "--summary",
            probe.summary,
            "--ocr",
            probe.ocr,
            "--scene",
            probe.scene,
            "--why-relevant",
            probe.why_relevant,
            "--source-channel",
            "docker-smoke",
            "--json",
        ),
        config_path=config_path,
        state_dir=state_dir,
        timeout=300,
    )
    assert payload.get("runtime_visual_probe") == "cli_store_visual_only", payload
    path, uri = extract_path_or_uri(payload)
    assert path or uri, payload
    return StoredVisualRecord(path=path, uri=uri, payload=payload)


def verify_visual_probe_search_and_get(
    *,
    config_path: Path,
    state_dir: Path,
    probe: VisualRegressionProbe,
    record: StoredVisualRecord,
) -> None:
    search_payload = run_openclaw_json_command(
        openclaw_command("memory-palace", "search", probe.query, "--json"),
        config_path=config_path,
        state_dir=state_dir,
        timeout=300,
    )
    ensure_visual_search_hits(search_payload, expected_path=record.path)

    get_target = record.path or record.uri
    assert get_target, record
    get_payload = run_openclaw_json_command(
        openclaw_command("memory-palace", "get", get_target, "--json"),
        config_path=config_path,
        state_dir=state_dir,
        timeout=300,
    )
    text = str(get_payload.get("text") or "")
    assert probe.summary in text, get_payload
    assert probe.ocr in text, get_payload
    for expected in probe.expected_get_fragments:
        assert expected in text, get_payload
    for forbidden in probe.forbidden_get_fragments:
        assert forbidden not in text, get_payload


def build_visual_regression_probes(profile: str, *, token_seed: str) -> list[VisualRegressionProbe]:
    probes: list[VisualRegressionProbe] = [build_docker_visual_probe(profile, token_seed=token_seed)]
    jpeg_token = slugify_token(
        f"docker-visual-jpeg-{profile}-{token_seed}",
        fallback=f"docker-visual-jpeg-{profile}",
    )
    jpeg_blob = base64.b64encode(((jpeg_token + "|") * 10).encode("utf-8")).decode("ascii")
    jpeg_ref = f"data:image/jpeg;base64,{jpeg_blob}"
    probes.append(
        VisualRegressionProbe(
            token=jpeg_token,
            query=jpeg_token,
            media_ref=jpeg_ref,
            summary=f"docker visual jpeg probe {jpeg_token}",
            ocr=f"{jpeg_token} jpeg lane",
            scene=f"jpeg sanitization wall {jpeg_token}",
            why_relevant=f"confirm jpeg data url sanitization for {jpeg_token}",
            expected_get_fragments=("media_ref: data:image/jpeg;sha256-",),
            forbidden_get_fragments=(jpeg_ref, "data:image/jpeg;base64,"),
            forbidden_db_fragments=(jpeg_ref, jpeg_blob, "data:image/jpeg;base64,"),
        )
    )
    webp_token = slugify_token(
        f"docker-visual-webp-{profile}-{token_seed}",
        fallback=f"docker-visual-webp-{profile}",
    )
    webp_blob = base64.b64encode(((webp_token + "|") * 10).encode("utf-8")).decode("ascii")
    webp_ref = f"data:image/webp;base64,{webp_blob}"
    probes.append(
        VisualRegressionProbe(
            token=webp_token,
            query=webp_token,
            media_ref=webp_ref,
            summary=f"docker visual webp probe {webp_token}",
            ocr=f"{webp_token} webp lane",
            scene=f"webp sanitization wall {webp_token}",
            why_relevant=f"confirm webp data url sanitization for {webp_token}",
            expected_get_fragments=("media_ref: data:image/webp;sha256-",),
            forbidden_get_fragments=(webp_ref, "data:image/webp;base64,"),
            forbidden_db_fragments=(webp_ref, webp_blob, "data:image/webp;base64,"),
        )
    )
    blob_token = slugify_token(
        f"docker-visual-blob-{profile}-{token_seed}",
        fallback=f"docker-visual-blob-{profile}",
    )
    blob_ref = f"blob:https://openclaw.local/{blob_token}"
    probes.append(
        VisualRegressionProbe(
            token=blob_token,
            query=blob_token,
            media_ref=blob_ref,
            summary=f"docker visual blob probe {blob_token}",
            ocr=f"{blob_token} blob lane",
            scene=f"blob transport board {blob_token}",
            why_relevant=f"confirm blob media ref remains searchable for {blob_token}",
            expected_get_fragments=(f"media_ref: {blob_ref}",),
        )
    )
    presigned_token = slugify_token(
        f"docker-visual-presigned-{profile}-{token_seed}",
        fallback=f"docker-visual-presigned-{profile}",
    )
    long_signature = (presigned_token * 24)[:720]
    presigned_ref = (
        "https://cdn.openclaw.local/visuals/"
        f"{presigned_token}.png"
        "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={presigned_token}%2F20260311%2Fus-east-1%2Fs3%2Faws4_request"
        "&X-Amz-Date=20260311T000000Z"
        "&X-Amz-Expires=900"
        f"&X-Amz-Security-Token={long_signature}"
        f"&X-Amz-Signature={long_signature}"
    )
    probes.append(
        VisualRegressionProbe(
            token=presigned_token,
            query=presigned_token,
            media_ref=presigned_ref,
            summary=f"docker visual presigned probe {presigned_token}",
            ocr=f"{presigned_token} presigned lane",
            scene=f"presigned sanitization wall {presigned_token}",
            why_relevant=f"confirm long presigned url sanitization for {presigned_token}",
            expected_get_fragments=("media_ref: sha256-",),
            forbidden_get_fragments=(presigned_ref, "X-Amz-Signature=", "X-Amz-Security-Token="),
            forbidden_db_fragments=(presigned_ref, "X-Amz-Signature=", "X-Amz-Security-Token="),
        )
    )
    return probes


def run_docker_compose(
    env_file: Path,
    compose_env: dict[str, str],
    *compose_args: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-f",
            "docker-compose.yml",
            *compose_args,
        ],
        cwd=PROJECT_ROOT,
        env=compose_env,
        timeout=timeout,
    )


def docker_compose_down(env_file: Path, compose_env: dict[str, str]) -> None:
    proc = run_docker_compose(
        env_file,
        compose_env,
        "down",
        "--remove-orphans",
        timeout=600,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)


def docker_compose_up(env_file: Path, compose_env: dict[str, str]) -> None:
    proc = run_docker_compose(
        env_file,
        compose_env,
        "up",
        "-d",
        "--force-recreate",
        "--remove-orphans",
        timeout=1800,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)


def _is_transient_docker_backend_exec_failure(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode == 0:
        return False
    detail = f"{proc.stdout}\n{proc.stderr}".lower()
    return any(marker in detail for marker in _DOCKER_BACKEND_EXEC_TRANSIENT_MARKERS)


def _resolve_docker_backend_container_id(
    *,
    env: dict[str, str],
    compose_project_name: str,
    attempts: int = 10,
    retry_delay_seconds: float = 3.0,
) -> str:
    last_result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(max(1, attempts)):
        last_result = run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={compose_project_name}",
                "--filter",
                "label=com.docker.compose.service=backend",
                "--format",
                "{{.ID}}",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            timeout=60,
        )
        container_lines = str(last_result.stdout or "").strip().splitlines()
        container_id = container_lines[0].strip() if container_lines else ""
        if last_result.returncode == 0 and container_id:
            return container_id
        if attempt < max(1, attempts) - 1:
            time.sleep(max(0.1, retry_delay_seconds))
    detail = ""
    if last_result is not None:
        detail = f"\nSTDOUT:\n{last_result.stdout}\nSTDERR:\n{last_result.stderr}"
    raise RuntimeError(
        f"Could not resolve a running backend container for compose project {compose_project_name}.{detail}"
    )


def _run_docker_backend_exec(
    *,
    env: dict[str, str],
    compose_project_name: str,
    command_args: list[str],
    timeout: int = 300,
    attempts: int = 10,
    retry_delay_seconds: float = 3.0,
) -> subprocess.CompletedProcess[str]:
    last_result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(max(1, attempts)):
        container_lookup = run(
            [
                "docker",
                "ps",
                "--filter",
                f"label=com.docker.compose.project={compose_project_name}",
                "--filter",
                "label=com.docker.compose.service=backend",
                "--format",
                "{{.ID}}",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            timeout=60,
        )
        container_lines = str(container_lookup.stdout or "").strip().splitlines()
        container_id = container_lines[0].strip() if container_lines else ""
        if container_lookup.returncode != 0 or not container_id:
            last_result = subprocess.CompletedProcess(
                ["docker", "exec", "<backend>", *command_args],
                1,
                stdout="",
                stderr=(
                    f"Could not resolve a running backend container for compose project {compose_project_name}.\n"
                    f"STDOUT:\n{container_lookup.stdout}\n"
                    f"STDERR:\n{container_lookup.stderr}"
                ),
            )
            if attempt < max(1, attempts) - 1:
                time.sleep(max(0.1, retry_delay_seconds))
            continue

        last_result = run(
            ["docker", "exec", container_id, *command_args],
            cwd=PROJECT_ROOT,
            env=env,
            timeout=timeout,
        )
        if not _is_transient_docker_backend_exec_failure(last_result) or attempt >= max(1, attempts) - 1:
            return last_result
        time.sleep(max(0.1, retry_delay_seconds))
    assert last_result is not None
    return last_result


def inspect_docker_visual_storage(
    env_file: Path,
    compose_env: dict[str, str],
    *,
    compose_project_name: str,
    probe: VisualRegressionProbe,
) -> dict[str, Any]:
    script = f"""
import json
import os
import sqlite3
from urllib.parse import unquote

database_url = os.environ["DATABASE_URL"]
prefix = "sqlite+aiosqlite:///"
raw_path = database_url[len(prefix):] if database_url.startswith(prefix) else database_url
raw_path = raw_path.split("?", 1)[0].split("#", 1)[0]
db_path = unquote(raw_path)

probe_summary = {probe.summary!r}
forbidden_fragments = {list(probe.forbidden_db_fragments)!r}

conn = sqlite3.connect(db_path)
try:
    memory_content = (
        conn.execute(
            "SELECT content FROM memories WHERE content LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{{probe_summary}}%",),
        ).fetchone()
        or ("",)
    )[0]
    chunk_text = (
        conn.execute(
            "SELECT chunk_text FROM memory_chunks WHERE chunk_text LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%{{probe_summary}}%",),
        ).fetchone()
        or ("",)
    )[0]
    fragment_counts = []
    for fragment in forbidden_fragments:
        fragment_counts.append({{
            "fragment": fragment,
            "memory_hits": conn.execute(
                "SELECT count(*) FROM memories WHERE instr(content, ?) > 0",
                (fragment,),
            ).fetchone()[0],
            "chunk_hits": conn.execute(
                "SELECT count(*) FROM memory_chunks WHERE instr(chunk_text, ?) > 0",
                (fragment,),
            ).fetchone()[0],
        }})
    payload = {{
        "database_path": db_path,
        "memory_probe_row_found": bool(memory_content),
        "chunk_probe_row_found": bool(chunk_text),
        "fragment_counts": fragment_counts,
    }}
finally:
    conn.close()

print(json.dumps(payload))
"""
    _ = env_file
    proc = _run_docker_backend_exec(
        env=compose_env,
        compose_project_name=compose_project_name,
        command_args=["python", "-c", script],
        timeout=180,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    stdout = proc.stdout.strip()
    if not stdout:
        raise RuntimeError(
            "docker backend visual storage inspection returned empty stdout"
        )
    return parse_json_stdout(stdout)


def validate_docker_visual_storage(report: dict[str, Any], *, probe: VisualRegressionProbe) -> None:
    assert report.get("memory_probe_row_found") is True, report
    assert report.get("chunk_probe_row_found") is True, report
    fragment_counts = report.get("fragment_counts")
    assert isinstance(fragment_counts, list), report
    assert len(fragment_counts) == len(probe.forbidden_db_fragments), report
    for item in fragment_counts:
        assert item.get("memory_hits") == 0, report
        assert item.get("chunk_hits") == 0, report


def run_docker_visual_regressions(
    *,
    config_path: Path,
    state_dir: Path,
    env_file: Path,
    compose_env: dict[str, str],
    frontend_port: str,
    backend_port: str,
    probes: list[VisualRegressionProbe],
) -> None:
    stored_records: list[tuple[VisualRegressionProbe, StoredVisualRecord]] = []
    for probe in probes:
        stored_records.append(
            (
                probe,
                store_visual_probe(
                    config_path=config_path,
                    state_dir=state_dir,
                    probe=probe,
                ),
            )
        )
    run_openclaw_json_command(
        openclaw_command("memory-palace", "index", "--wait", "--json"),
        config_path=config_path,
        state_dir=state_dir,
        timeout=300,
    )
    for probe, record in stored_records:
        verify_visual_probe_search_and_get(
            config_path=config_path,
            state_dir=state_dir,
            probe=probe,
            record=record,
        )
        if probe.forbidden_db_fragments:
            storage_report = inspect_docker_visual_storage(
                env_file,
                compose_env,
                compose_project_name=str(compose_env.get("COMPOSE_PROJECT_NAME") or ""),
                probe=probe,
            )
            validate_docker_visual_storage(storage_report, probe=probe)

    docker_compose_down(env_file, compose_env)
    docker_compose_up(env_file, compose_env)
    wait_for_http(f"http://127.0.0.1:{frontend_port}/")
    wait_for_http(f"http://127.0.0.1:{backend_port}/health")

    for probe, record in stored_records:
        verify_visual_probe_search_and_get(
            config_path=config_path,
            state_dir=state_dir,
            probe=probe,
            record=record,
        )


def run_local_case(
    profile: str,
    model_env: dict[str, str],
    *,
    skip_frontend_e2e: bool,
) -> SmokeResult:
    with tempfile.TemporaryDirectory(prefix=f"mp-openclaw-local-{profile}-") as tmp:
        tmp_path = Path(tmp)
        env_file = tmp_path / f"profile-{profile}.env"
        config_path = tmp_path / f"openclaw-{profile}.json"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        env_values = build_profile_env(local_native_platform_name(), profile, env_file, model_env)
        prewarm_results = prewarm_profile_model_backends(profile, env_values)
        if should_apply_local_embedding_fallback(profile, prewarm_results):
            env_values = apply_local_embedding_fallback(
                env_values,
                platform=local_native_platform_name(),
                target_dim=env_values.get("RETRIEVAL_EMBEDDING_DIM") or DEFAULT_PROFILE_EMBEDDING_DIM,
            )
            prewarm_results = prewarm_profile_model_backends(profile, env_values)
        ensure_successful_prewarm_results(prewarm_results)
        seed_local_memory(env_values["DATABASE_URL"], env_values=env_values)
        build_openclaw_config(
            config_path,
            transport="stdio",
            workspace_dir=tmp_path / "workspace",
            stdio_env=env_values,
        )
        outputs = run_openclaw_smoke(
            config_path=config_path,
            state_dir=state_dir,
            query_text="简洁回答",
            visual_query="whiteboard",
        )
        validate_local_outputs(outputs)
        visual_regression_probes = build_visual_regression_probes(profile, token_seed=tmp_path.name)
        local_probe_records = [
            (
                probe,
                store_visual_probe(
                    config_path=config_path,
                    state_dir=state_dir,
                    probe=probe,
                ),
            )
            for probe in visual_regression_probes
        ]
        run_openclaw_json_command(
            openclaw_command("memory-palace", "index", "--wait", "--json"),
            config_path=config_path,
            state_dir=state_dir,
            timeout=300,
        )
        for probe, record in local_probe_records:
            verify_visual_probe_search_and_get(
                config_path=config_path,
                state_dir=state_dir,
                probe=probe,
                record=record,
            )
        if not skip_frontend_e2e:
            run_frontend_e2e(
                env_overrides={
                    **env_values,
                    "PLAYWRIGHT_E2E_API_KEY": f"playwright-local-{profile}-key",
                    "PLAYWRIGHT_E2E_DATABASE_URL": env_values["DATABASE_URL"],
                    "PLAYWRIGHT_E2E_OUTPUT_DIR": str(tmp_path / "playwright-results"),
                }
            )
        summary = "status/search/get/index/store-visual all passed"
        if profile in {"c", "d"}:
            run_compact_context_reflection_probe(profile, model_env)
            summary += "; compact-context reflection passed"
        if skip_frontend_e2e:
            summary += "; frontend-e2e skipped"
        else:
            summary += "; frontend-e2e passed"
        return SmokeResult("local", profile, "PASS", summary)


def wait_for_http(url: str, timeout_seconds: int = 180) -> None:
    import urllib.request

    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def ensure_frontend_playwright_tooling() -> None:
    global _PLAYWRIGHT_BROWSER_READY

    npm_bin = resolve_node_cli_executable("npm")
    npx_bin = resolve_node_cli_executable("npx")

    if not Path(npm_bin).exists():
        raise RuntimeError("npm is not installed; cannot run frontend Playwright E2E")
    if not Path(npx_bin).exists():
        raise RuntimeError("npx is not installed; cannot run frontend Playwright E2E")

    playwright_pkg = FRONTEND_ROOT / "node_modules" / "@playwright" / "test"
    if not playwright_pkg.exists():
        proc = run([npm_bin, "install"], cwd=FRONTEND_ROOT, timeout=1800)
        if proc.returncode != 0:
            raise RuntimeError(
                "frontend npm install failed:\n"
                f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )

    if _PLAYWRIGHT_BROWSER_READY:
        return

    proc = run([npx_bin, "playwright", "install", "chromium"], cwd=FRONTEND_ROOT, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(
            "playwright install chromium failed:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    _PLAYWRIGHT_BROWSER_READY = True


def resolve_node_cli_executable(name: str) -> str:
    resolved = shutil.which(name)
    if resolved:
        return resolved
    if os.name == "nt":
        for suffix in (".cmd", ".bat", ".exe"):
            candidate = shutil.which(f"{name}{suffix}")
            if candidate:
                return candidate
    return name


def run_frontend_e2e(
    *,
    env_overrides: dict[str, str],
    external_base_url: str | None = None,
) -> None:
    ensure_frontend_playwright_tooling()
    npm_bin = resolve_node_cli_executable("npm")

    env = os.environ.copy()
    env.update({key: value for key, value in env_overrides.items() if value is not None})

    if external_base_url:
        env["PLAYWRIGHT_E2E_EXTERNAL_BASE_URL"] = external_base_url
        env.pop("PLAYWRIGHT_E2E_API_PORT", None)
        env.pop("PLAYWRIGHT_E2E_UI_PORT", None)
    else:
        api_port = find_free_port()
        ui_port = find_free_port()
        while ui_port == api_port:
            ui_port = find_free_port()
        env["PLAYWRIGHT_E2E_API_PORT"] = api_port
        env["PLAYWRIGHT_E2E_UI_PORT"] = ui_port

    proc = run([npm_bin, "run", "test:e2e"], cwd=FRONTEND_ROOT, env=env, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(
            "frontend Playwright E2E failed:\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )


def find_free_port() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return str(sock.getsockname()[1])


def is_port_conflict(text: str) -> bool:
    lowered = text.lower()
    return (
        "port is already allocated" in lowered
        or "address already in use" in lowered
        or "ports are not available" in lowered
        or "bind for 0.0.0.0" in lowered
    )


def ensure_docker_daemon_available() -> None:
    proc = run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        cwd=PROJECT_ROOT,
        timeout=30,
    )
    if proc.returncode == 0:
        return
    detail = (proc.stderr or proc.stdout or "").strip()
    raise RuntimeError(
        "Docker daemon unavailable"
        + (f": {detail}" if detail else "")
    )


def run_docker_case(
    profile: str,
    model_env: dict[str, str],
    build_images: bool,
    *,
    skip_frontend_e2e: bool,
) -> SmokeResult:
    ensure_docker_daemon_available()
    with tempfile.TemporaryDirectory(prefix=f"mp-openclaw-docker-{profile}-") as tmp:
        tmp_path = Path(tmp)
        env_file = tmp_path / f"docker-profile-{profile}.env"
        config_path = tmp_path / f"openclaw-docker-{profile}.json"
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        env_values = build_profile_env("docker", profile, env_file, model_env)
        prewarm_results = prewarm_profile_model_backends(profile, env_values)
        if should_apply_local_embedding_fallback(profile, prewarm_results):
            env_values = apply_local_embedding_fallback(
                env_values,
                platform="docker",
                target_dim=env_values.get("RETRIEVAL_EMBEDDING_DIM") or DEFAULT_PROFILE_EMBEDDING_DIM,
            )
            write_env_file(env_file, env_values)
        else:
            write_env_file(env_file, env_values)
        last_error = ""
        for attempt in range(1, 4):
            frontend_port = find_free_port()
            backend_port = find_free_port()
            compose_project_name = slugify_token(
                f"memory-palace-smoke-{profile}-{tmp_path.name}-{attempt}",
                fallback=f"memory-palace-smoke-{profile}-{attempt}",
            )
            mcp_api_key = f"smoke-docker-{profile}-{tmp_path.name}-{attempt}"

            env = os.environ.copy()
            env["MEMORY_PALACE_DOCKER_ENV_FILE"] = shell_path(env_file)
            env["COMPOSE_PROJECT_NAME"] = compose_project_name
            env["MEMORY_PALACE_FRONTEND_PORT"] = frontend_port
            env["MEMORY_PALACE_BACKEND_PORT"] = backend_port
            env["NOCTURNE_FRONTEND_PORT"] = frontend_port
            env["NOCTURNE_BACKEND_PORT"] = backend_port
            env["MEMORY_PALACE_DATA_VOLUME"] = slugify_token(
                f"memory-palace-smoke-{profile}-{tmp_path.name}-data-{attempt}",
                fallback=f"memory-palace-smoke-{profile}-data-{attempt}",
            ).replace("-", "_")
            env["MEMORY_PALACE_SNAPSHOTS_VOLUME"] = slugify_token(
                f"memory-palace-smoke-{profile}-{tmp_path.name}-snapshots-{attempt}",
                fallback=f"memory-palace-smoke-{profile}-snapshots-{attempt}",
            ).replace("-", "_")
            env["NOCTURNE_DATA_VOLUME"] = env["MEMORY_PALACE_DATA_VOLUME"]
            env["NOCTURNE_SNAPSHOTS_VOLUME"] = env["MEMORY_PALACE_SNAPSHOTS_VOLUME"]
            env["MCP_API_KEY"] = mcp_api_key
            env["MCP_API_KEY_ALLOW_INSECURE_LOCAL"] = "false"
            for key, value in model_env.items():
                env[key] = value

            bash_bin = shutil.which("bash") or "bash"
            up_command = [
                bash_bin,
                "scripts/docker_one_click.sh",
                "--profile",
                profile,
                "--frontend-port",
                frontend_port,
                "--backend-port",
                backend_port,
                "--allow-runtime-env-injection",
                *(["--no-build"] if not build_images else []),
            ]
            proc = run(
                up_command,
                cwd=PROJECT_ROOT,
                env=env,
                timeout=1800,
            )
            if proc.returncode != 0:
                combined = proc.stderr or proc.stdout
                last_error = combined
                try:
                    docker_compose_down(env_file, env)
                except Exception:  # noqa: BLE001
                    pass
                if attempt < 3 and is_port_conflict(combined):
                    continue
                raise RuntimeError(combined)

            try:
                wait_for_http(f"http://127.0.0.1:{frontend_port}/")
                wait_for_http(f"http://127.0.0.1:{backend_port}/health")
                seed_docker_memory(env_file, compose_project_name)
                build_openclaw_config(
                    config_path,
                    transport="sse",
                    workspace_dir=tmp_path / "workspace",
                    sse_url=f"http://127.0.0.1:{frontend_port}/sse",
                    sse_api_key=mcp_api_key,
                )
                outputs = run_openclaw_smoke(
                    config_path=config_path,
                    state_dir=state_dir,
                    query_text="简洁回答",
                    visual_query="whiteboard",
                )
                validate_local_outputs(outputs)
                run_docker_visual_regressions(
                    config_path=config_path,
                    state_dir=state_dir,
                    env_file=env_file,
                    compose_env=env,
                    frontend_port=frontend_port,
                    backend_port=backend_port,
                    probes=build_visual_regression_probes(profile, token_seed=tmp_path.name),
                )
                summary = (
                    "status/search/get/index/store-visual all passed; "
                    "visual persistence survived docker down+up; "
                    "data-url blob not persisted in sqlite content/chunks"
                )
                if skip_frontend_e2e:
                    summary += "; frontend-e2e skipped"
                else:
                    summary += "; frontend-e2e covered by standalone local gate"
                return SmokeResult("docker", profile, "PASS", summary)
            finally:
                run_docker_compose(
                    env_file,
                    env,
                    "down",
                    "--remove-orphans",
                    timeout=600,
                )

        raise RuntimeError(last_error or f"docker smoke failed for profile {profile}")


def build_report(results: list[SmokeResult]) -> str:
    lines = [
        "# OpenClaw Memory Palace Profile Smoke Report",
        "",
        "| Mode | Profile | Status | Summary |",
        "|---|---|---|---|",
    ]
    for item in results:
        lines.append(f"| {item.mode} | {item.profile.upper()} | {item.status} | {item.summary} |")
    failures = [item for item in results if item.status != "PASS"]
    if failures:
        lines.extend(["", "## Failures", ""])
        for item in failures:
            lines.append(f"### {item.mode} / {item.profile.upper()}")
            lines.append("")
            lines.append("```text")
            lines.append(item.details.strip())
            lines.append("```")
            lines.append("")
    return "\n".join(lines) + "\n"


def write_report(path: Path, results: list[SmokeResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(results), encoding="utf-8")


def main() -> int:
    install_signal_cleanup_handlers()
    parser = argparse.ArgumentParser()
    parser.add_argument("--modes", default="local,docker", help="comma-separated: local,docker")
    parser.add_argument("--profiles", default="a,b,c,d", help="comma-separated: a,b,c,d")
    parser.add_argument("--model-env", default=str(DEFAULT_MODEL_ENV))
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument("--skip-frontend-e2e", action="store_true")
    args = parser.parse_args()

    model_env = load_env_file(normalize_host_cli_path(args.model_env)) if args.model_env else {}
    results: list[SmokeResult] = []
    build_images = True

    for mode in [item.strip() for item in args.modes.split(",") if item.strip()]:
        for profile in [item.strip().lower() for item in args.profiles.split(",") if item.strip()]:
            try:
                if mode == "local":
                    results.append(
                        run_local_case(
                            profile,
                            model_env,
                            skip_frontend_e2e=args.skip_frontend_e2e,
                        )
                    )
                elif mode == "docker":
                    results.append(
                        run_docker_case(
                            profile,
                            model_env,
                            build_images,
                            skip_frontend_e2e=args.skip_frontend_e2e,
                        )
                    )
                    build_images = False
                else:
                    raise RuntimeError(f"unsupported mode: {mode}")
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                summary = message.splitlines()[0] if message.splitlines() else exc.__class__.__name__
                results.append(SmokeResult(mode, profile, "FAIL", summary, message or repr(exc)))

    report_path = normalize_host_cli_path(args.report)
    write_report(report_path, results)
    print(report_path)
    return 0 if all(item.status == "PASS" for item in results) else 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Some OpenClaw/profile-smoke subprocess combinations can leave the
    # interpreter alive after all work is complete. Force a clean CLI exit
    # after the report is written so automation does not hang indefinitely.
    os._exit(exit_code)
