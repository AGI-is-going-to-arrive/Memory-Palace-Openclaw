#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
DEFAULT_REPORT = Path("/tmp/review_snapshots_http_smoke.md")
_OS_KILLPG = getattr(os, "killpg", None)
_FORCE_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
TMP_ROOT = PROJECT_ROOT / ".tmp"
_DOCKER_BACKEND_EXEC_TRANSIENT_MARKERS = (
    'service "backend" is not running',
    "service backend is not running",
    "is restarting",
    "no container found",
)
_DOCKER_PORT_BIND_TRANSIENT_MARKERS = (
    "ports are not available",
    "/forwards/expose returned unexpected status: 500",
)


def _maybe_reexec_with_backend_python() -> None:
    backend_venv = (BACKEND_ROOT / ".venv").resolve()
    if Path(sys.prefix).resolve() == backend_venv:
        return
    candidates = (
        BACKEND_ROOT / ".venv" / "bin" / "python",
        BACKEND_ROOT / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            os.execv(str(candidate), [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]])


try:
    import requests
    from openclaw_json_output import extract_json_from_streams
except ModuleNotFoundError as exc:
    if exc.name == "requests":
        _maybe_reexec_with_backend_python()
    raise

import openclaw_memory_palace_profile_smoke as profile_smoke


def resolve_backend_python() -> str:
    candidates = (
        BACKEND_ROOT / ".venv" / "bin" / "python",
        BACKEND_ROOT / ".venv" / "Scripts" / "python.exe",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


@dataclass
class SmokeResult:
    mode: str
    status: str
    summary: str
    details: str = ""


def run(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        env=env,
        cwd=str(cwd) if cwd else None,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def load_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def write_env_file(path: Path, values: dict[str, str]) -> None:
    lines = [f"{key}={value}" for key, value in values.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_http(url: str, *, headers: dict[str, str] | None = None, timeout_seconds: int = 180) -> None:
    deadline = time.time() + timeout_seconds
    last_error = ""
    while time.time() < deadline:
        try:
            response = requests.get(url, headers=headers, timeout=5)
            if response.status_code < 500:
                return
            last_error = f"status={response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def parse_json_output(proc: subprocess.CompletedProcess[str]) -> Any:
    if not str(proc.stdout or "").strip() and not str(proc.stderr or "").strip():
        return None
    return extract_json_from_streams(proc.stdout, proc.stderr)


def make_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    response = requests.request(method, url, headers=headers, json=payload, timeout=30)
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return response.status_code, body


def _is_transient_backend_exec_failure(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode == 0:
        return False
    detail = f"{proc.stdout}\n{proc.stderr}".lower()
    return any(marker in detail for marker in _DOCKER_BACKEND_EXEC_TRANSIENT_MARKERS)


def _run_docker_backend_exec(
    *,
    env: dict[str, str],
    compose_project_name: str,
    command_args: list[str],
    timeout: int = 300,
    attempts: int = 10,
    retry_delay_seconds: float = 3.0,
) -> subprocess.CompletedProcess[str]:
    command = None
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
        container_id = str(container_lookup.stdout or "").strip().splitlines()
        container_id = container_id[0].strip() if container_id else ""
        if container_lookup.returncode == 0 and container_id:
            command = [
                "docker",
                "exec",
                "--user",
                "10001:10001",
                container_id,
                *command_args,
            ]
            last_result = run(
                command,
                cwd=PROJECT_ROOT,
                env=env,
                timeout=timeout,
            )
            if not _is_transient_backend_exec_failure(last_result) or attempt >= max(1, attempts) - 1:
                return last_result
        else:
            last_result = container_lookup
            if attempt >= max(1, attempts) - 1:
                return last_result
        time.sleep(max(0.1, retry_delay_seconds))
    assert last_result is not None
    return last_result


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


def _run_docker_backend_python_file(
    *,
    env: dict[str, str],
    compose_project_name: str,
    temp_root: Path,
    script_name: str,
    python_code: str,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    container_id = _resolve_docker_backend_container_id(
        env=env,
        compose_project_name=compose_project_name,
    )
    local_script = temp_root / script_name
    local_script.write_text(str(python_code).strip() + "\n", encoding="utf-8")
    remote_script = f"/tmp/{script_name}"
    copy_result = run(
        ["docker", "cp", str(local_script), f"{container_id}:{remote_script}"],
        cwd=PROJECT_ROOT,
        env=env,
        timeout=60,
    )
    if copy_result.returncode != 0:
        return copy_result
    return run(
        ["docker", "exec", "--user", "10001:10001", container_id, "python", remote_script],
        cwd=PROJECT_ROOT,
        env=env,
        timeout=timeout,
    )


def _is_transient_docker_port_bind_failure(proc: subprocess.CompletedProcess[str]) -> bool:
    if proc.returncode == 0:
        return False
    detail = f"{proc.stdout}\n{proc.stderr}".lower()
    return any(marker in detail for marker in _DOCKER_PORT_BIND_TRANSIENT_MARKERS)


def _sanitize_compose_project_name(raw_value: str) -> str:
    sanitized = re.sub(r"[^a-z0-9-]+", "-", str(raw_value or "").strip().lower()).strip("-")
    return sanitized or "memory-palace"


def _extract_compose_project_name(output_text: str, fallback: str) -> str:
    match = re.search(r"Compose project:\s*([a-z0-9-]+)", str(output_text or ""), re.IGNORECASE)
    if match:
        return _sanitize_compose_project_name(match.group(1))
    return _sanitize_compose_project_name(fallback)


def writer_code(snapshot_dir: str, memory_name: str, original_content: str, updated_content: str) -> str:
    return textwrap.dedent(
        f"""
        import asyncio
        import json
        import sys
        from pathlib import Path

        repo_root = Path({str(PROJECT_ROOT)!r})
        backend_root = repo_root / "backend"
        sys.path.insert(0, str(backend_root))

        from db import snapshot as snapshot_mod
        from db import sqlite_client as sqlite_mod
        import mcp_server

        async def main():
            await sqlite_mod.close_sqlite_client()
            snapshot_mod._snapshot_manager = snapshot_mod.SnapshotManager({snapshot_dir!r})
            client = sqlite_mod.get_sqlite_client()
            await client.init_db()
            created = json.loads(await mcp_server.create_memory(
                parent_uri="core://",
                content={original_content!r},
                priority=1,
                title={memory_name!r},
                disclosure="http smoke",
            ))
            updated = json.loads(await mcp_server.update_memory(
                uri=f"core://{memory_name}",
                old_string={original_content!r},
                new_string={updated_content!r},
            ))
            session_id = mcp_server.get_session_id()
            snapshots = snapshot_mod.get_snapshot_manager().list_snapshots(session_id)
            result = {{
                "session_id": session_id,
                "created": created,
                "updated": updated,
                "snapshots": snapshots,
            }}
            print(json.dumps(result, ensure_ascii=False))
            await sqlite_mod.close_sqlite_client()

        asyncio.run(main())
        """
    ).strip()


def reader_code(snapshot_dir: str, memory_name: str) -> str:
    return textwrap.dedent(
        f"""
        import asyncio
        import json
        import sys
        from pathlib import Path

        repo_root = Path({str(PROJECT_ROOT)!r})
        backend_root = repo_root / "backend"
        sys.path.insert(0, str(backend_root))

        from db import snapshot as snapshot_mod
        from db import sqlite_client as sqlite_mod

        async def main():
            await sqlite_mod.close_sqlite_client()
            snapshot_mod._snapshot_manager = snapshot_mod.SnapshotManager({snapshot_dir!r})
            client = sqlite_mod.get_sqlite_client()
            await client.init_db()
            current = await client.get_memory_by_path({memory_name!r}, "core", reinforce_access=False)
            print(json.dumps({{"content": current.get("content") if current else None}}, ensure_ascii=False))
            await sqlite_mod.close_sqlite_client()

        asyncio.run(main())
        """
    ).strip()


def docker_writer_code(memory_name: str, original_content: str, updated_content: str) -> str:
    return textwrap.dedent(
        f"""
        import asyncio
        import json
        import sys
        sys.path.insert(0, "/app/backend")
        from db import snapshot as snapshot_mod
        from db import sqlite_client as sqlite_mod

        async def main():
            await sqlite_mod.close_sqlite_client()
            snapshot_mod._snapshot_manager = snapshot_mod.SnapshotManager("/app/snapshots")
            client = sqlite_mod.get_sqlite_client()
            await client.init_db()
            created = await client.create_memory(
                parent_path="",
                content={original_content!r},
                priority=1,
                title={memory_name!r},
                disclosure="http smoke",
                domain="core",
            )
            session_id = f"review.http.docker.{memory_name}"
            memory_full = await client.get_memory_by_id(created["id"])
            all_paths = memory_full.get("paths", []) if memory_full else []
            snapshot_mod.get_snapshot_manager().create_snapshot(
                session_id=session_id,
                resource_id=f"memory:{{created['id']}}",
                resource_type="memory",
                snapshot_data={{
                    "operation_type": "modify_content",
                    "memory_id": created["id"],
                    "uri": created["uri"],
                    "domain": created["domain"],
                    "path": created["path"],
                    "all_paths": all_paths,
                }},
            )
            updated = await client.update_memory(
                path={memory_name!r},
                content={updated_content!r},
                domain="core",
            )
            snapshots = snapshot_mod.get_snapshot_manager().list_snapshots(session_id)
            print(json.dumps({{"session_id": session_id, "created": created, "updated": updated, "snapshots": snapshots}}, ensure_ascii=False))
            await sqlite_mod.close_sqlite_client()

        asyncio.run(main())
        """
    ).strip()


def docker_reader_code(memory_name: str) -> str:
    return textwrap.dedent(
        f"""
        import asyncio
        import json
        import sys
        sys.path.insert(0, "/app/backend")
        from db import sqlite_client as sqlite_mod

        async def main():
            await sqlite_mod.close_sqlite_client()
            client = sqlite_mod.get_sqlite_client()
            await client.init_db()
            current = await client.get_memory_by_path({memory_name!r}, "core", reinforce_access=False)
            print(json.dumps({{"content": current.get("content") if current else None}}, ensure_ascii=False))
            await sqlite_mod.close_sqlite_client()

        asyncio.run(main())
        """
    ).strip()


def server_code(snapshot_dir: str, port: int) -> str:
    return textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        import uvicorn

        repo_root = Path({str(PROJECT_ROOT)!r})
        backend_root = repo_root / "backend"
        sys.path.insert(0, str(backend_root))

        from db import snapshot as snapshot_mod

        snapshot_mod._snapshot_manager = snapshot_mod.SnapshotManager({snapshot_dir!r})

        from main import app

        uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
        """
    ).strip()


def start_local_backend(env: dict[str, str], snapshot_dir: str, port: int) -> subprocess.Popen[str]:
    python_bin = resolve_backend_python()
    return subprocess.Popen(
        [python_bin, "-c", server_code(snapshot_dir, port)],
        cwd=str(BACKEND_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


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


def stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt" or not callable(_OS_KILLPG):
        _kill_process_tree_windows(process.pid, force=False)
    else:
        try:
            _OS_KILLPG(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name == "nt" or not callable(_OS_KILLPG):
            _kill_process_tree_windows(process.pid, force=True)
        else:
            try:
                _OS_KILLPG(process.pid, _FORCE_KILL_SIGNAL)
            except ProcessLookupError:
                return
        process.wait(timeout=10)


def exercise_review_http(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    encoded_session = urllib.parse.quote(session_id, safe="")
    memory_snapshot = next((item for item in snapshots if item.get("resource_type") == "memory"), None)
    if not isinstance(memory_snapshot, dict):
        raise RuntimeError(f"memory snapshot missing from writer payload: {json.dumps(snapshots, ensure_ascii=False)}")
    memory_resource_id = str(memory_snapshot["resource_id"])
    encoded_resource = urllib.parse.quote(memory_resource_id, safe="")

    sessions_status, sessions_body = make_request("GET", f"{base_url}/review/sessions", headers=headers)
    if sessions_status != 200:
        raise RuntimeError(f"list sessions failed: {sessions_status} {sessions_body}")
    listed_sessions = {item.get("session_id") for item in sessions_body}
    if session_id not in listed_sessions:
        raise RuntimeError(f"session {session_id} missing from review sessions: {sessions_body}")

    snapshots_status, snapshots_body = make_request(
        "GET",
        f"{base_url}/review/sessions/{encoded_session}/snapshots",
        headers=headers,
    )
    if snapshots_status != 200:
        raise RuntimeError(f"list snapshots failed: {snapshots_status} {snapshots_body}")

    detail_status, detail_body = make_request(
        "GET",
        f"{base_url}/review/sessions/{encoded_session}/snapshots/{encoded_resource}",
        headers=headers,
    )
    if detail_status != 200:
        raise RuntimeError(f"snapshot detail failed: {detail_status} {detail_body}")

    diff_status, diff_body = make_request(
        "GET",
        f"{base_url}/review/sessions/{encoded_session}/diff/{encoded_resource}",
        headers=headers,
    )
    if diff_status != 200:
        raise RuntimeError(f"snapshot diff failed: {diff_status} {diff_body}")

    rollback_status, rollback_body = make_request(
        "POST",
        f"{base_url}/review/sessions/{encoded_session}/rollback/{encoded_resource}",
        headers=headers,
        payload={},
    )
    if rollback_status != 200 or rollback_body.get("success") is not True:
        raise RuntimeError(f"rollback failed: {rollback_status} {rollback_body}")

    delete_status, delete_body = make_request(
        "DELETE",
        f"{base_url}/review/sessions/{encoded_session}/snapshots/{encoded_resource}",
        headers=headers,
    )
    if delete_status != 200:
        raise RuntimeError(f"delete snapshot failed: {delete_status} {delete_body}")

    clear_status, clear_body = make_request(
        "DELETE",
        f"{base_url}/review/sessions/{encoded_session}",
        headers=headers,
    )
    clear_missing = (
        clear_status == 404
        and (
            ("already empty" in str(clear_body).lower())
            or ("not found" in str(clear_body).lower())
        )
    )
    if clear_status != 200 and not clear_missing:
        raise RuntimeError(f"clear session failed: {clear_status} {clear_body}")

    return {
        "listed_snapshot_count": len(snapshots_body),
        "detail_resource_id": detail_body.get("resource_id"),
        "diff_summary": diff_body.get("diff_summary"),
        "rollback_message": rollback_body.get("message"),
        "delete_message": delete_body.get("message"),
        "clear_message": clear_body.get("message") if isinstance(clear_body, dict) else str(clear_body),
        "clear_missing_after_delete": clear_missing,
    }


def run_local_mode() -> SmokeResult:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="review-http-local-", dir=str(TMP_ROOT)))
    db_path = temp_root / "review_http.db"
    snapshot_dir = temp_root / "snapshots"
    memory_name = f"review_http_demo_{temp_root.name}"
    original_content = f"review-http-seed::{memory_name}::alpha-quartz-2147"
    updated_content = f"review-http-seed::{memory_name}::beta-lantern-8851"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "DB_MIGRATION_LOCK_FILE": str(temp_root / "review_http.lock"),
            "MCP_API_KEY": secrets.token_hex(24),
            "MCP_API_KEY_ALLOW_INSECURE_LOCAL": "false",
            "RETRIEVAL_EMBEDDING_BACKEND": "none",
            "RETRIEVAL_RERANKER_ENABLED": "false",
            "WRITE_GUARD_LLM_ENABLED": "false",
            "COMPACT_GIST_LLM_ENABLED": "false",
            "INTENT_LLM_ENABLED": "false",
            "SNAPSHOT_LOCK_TIMEOUT_SEC": "5",
        }
    )
    port = find_free_port()
    process = start_local_backend(env, str(snapshot_dir), port)
    base_url = f"http://127.0.0.1:{port}"
    headers = {"X-MCP-API-Key": env["MCP_API_KEY"]}

    try:
        wait_for_http(f"{base_url}/health", timeout_seconds=120)
        python_bin = resolve_backend_python()
        writer = run(
            [python_bin, "-c", writer_code(str(snapshot_dir), memory_name, original_content, updated_content)],
            env=env,
            cwd=BACKEND_ROOT,
            timeout=120,
        )
        if writer.returncode != 0:
            raise RuntimeError(f"writer failed:\nSTDOUT:\n{writer.stdout}\nSTDERR:\n{writer.stderr}")
        writer_payload = parse_json_output(writer)
        session_id = str(writer_payload["session_id"])
        snapshots = list(writer_payload["snapshots"])
        http_result = exercise_review_http(
            base_url=base_url,
            headers=headers,
            session_id=session_id,
            snapshots=snapshots,
        )
        reader = run(
            [python_bin, "-c", reader_code(str(snapshot_dir), memory_name)],
            env=env,
            cwd=BACKEND_ROOT,
            timeout=120,
        )
        if reader.returncode != 0:
            raise RuntimeError(f"reader failed:\nSTDOUT:\n{reader.stdout}\nSTDERR:\n{reader.stderr}")
        reader_payload = parse_json_output(reader)
        if reader_payload.get("content") != original_content:
            raise RuntimeError(f"restored content mismatch: {reader_payload}")
        return SmokeResult(
            mode="local",
            status="PASS",
            summary="sessions/snapshots/detail/diff/rollback/delete/clear all passed",
            details=json.dumps(
                {
                    "session_id": session_id,
                    "snapshot_count": len(snapshots),
                    "restored_content": reader_payload.get("content"),
                    "original_content": original_content,
                    **http_result,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        stderr = ""
        if process.stderr is not None:
            try:
                stderr = process.stderr.read() or ""
            except Exception:  # noqa: BLE001
                stderr = ""
        return SmokeResult(
            mode="local",
            status="FAIL",
            summary=str(exc).splitlines()[0],
            details=f"{exc}\n\nSERVER_STDERR:\n{stderr}",
        )
    finally:
        stop_process(process)


def run_docker_mode() -> SmokeResult:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_root = Path(tempfile.mkdtemp(prefix="review-http-docker-", dir=str(TMP_ROOT)))
    env_file = temp_root / "docker-profile-b.env"
    docker_env_file = shell_path(env_file)
    memory_name = f"review_http_demo_{temp_root.name}"
    original_content = f"review-http-seed::{memory_name}::alpha-quartz-2147"
    updated_content = f"review-http-seed::{memory_name}::beta-lantern-8851"
    mcp_api_key = f"review-http-docker-{temp_root.name}"
    compose_project_name = _sanitize_compose_project_name(
        f"review-http-docker-{temp_root.name}".replace("_", "-")
    )
    data_volume = f"review_http_docker_{temp_root.name}_data".replace("-", "_")
    snapshots_volume = f"review_http_docker_{temp_root.name}_snapshots".replace("-", "_")

    try:
        profile_smoke.build_profile_env("docker", "b", env_file, {})
    except Exception as exc:  # noqa: BLE001
        return SmokeResult("docker", "FAIL", "build_profile_env failed", str(exc))

    env = os.environ.copy()
    env.update(
        {
            "MEMORY_PALACE_DOCKER_ENV_FILE": docker_env_file,
            "COMPOSE_PROJECT_NAME": compose_project_name,
            "MEMORY_PALACE_DATA_VOLUME": data_volume,
            "MEMORY_PALACE_SNAPSHOTS_VOLUME": snapshots_volume,
            "SNAPSHOT_LOCK_TIMEOUT_SEC": "5",
            "MCP_API_KEY": mcp_api_key,
            "MCP_API_KEY_ALLOW_INSECURE_LOCAL": "false",
        }
    )

    up: subprocess.CompletedProcess[str] | None = None
    backend_port = 0
    frontend_port = 0
    bash_bin = shutil.which("bash")
    if not bash_bin:
        return SmokeResult(
            "docker",
            "FAIL",
            "bash is required for docker mode",
            "bash is not installed or not available in PATH.",
        )
    for attempt in range(3):
        backend_port = find_free_port()
        frontend_port = find_free_port()
        try:
            up = run(
                [
                    bash_bin,
                    "scripts/docker_one_click.sh",
                    "--profile",
                    "b",
                    "--frontend-port",
                    str(frontend_port),
                    "--backend-port",
                    str(backend_port),
                    "--no-auto-port",
                    "--no-build",
                    "--allow-runtime-env-injection",
                ],
                cwd=PROJECT_ROOT,
                env=env,
                timeout=1800,
            )
        except FileNotFoundError as exc:
            return SmokeResult("docker", "FAIL", "bash is required for docker mode", str(exc))
        if up.returncode == 0:
            break
        if not _is_transient_docker_port_bind_failure(up) or attempt >= 2:
            return SmokeResult("docker", "FAIL", "docker_one_click failed", up.stderr or up.stdout)
        run(
            [
                "docker",
                "compose",
                "--env-file",
                docker_env_file,
                "-f",
                "docker-compose.yml",
                "down",
                "--remove-orphans",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            timeout=600,
        )
        time.sleep(2)
    assert up is not None

    combined_up_output = f"{up.stdout}\n{up.stderr}"
    compose_project_name = _extract_compose_project_name(combined_up_output, compose_project_name)

    base_url = f"http://127.0.0.1:{backend_port}"
    auth_key = mcp_api_key
    headers = {"X-MCP-API-Key": auth_key}

    try:
        wait_for_http(f"{base_url}/health", timeout_seconds=180)
        writer = _run_docker_backend_python_file(
            env=env,
            compose_project_name=compose_project_name,
            temp_root=temp_root,
            script_name="review_http_writer.py",
            python_code=docker_writer_code(memory_name, original_content, updated_content),
            timeout=300,
        )
        if writer.returncode != 0:
            raise RuntimeError(f"docker writer failed:\nSTDOUT:\n{writer.stdout}\nSTDERR:\n{writer.stderr}")
        writer_payload = parse_json_output(writer)
        if not isinstance(writer_payload, dict):
            raise RuntimeError(
                "docker writer returned invalid JSON:\n"
                f"STDOUT:\n{writer.stdout}\nSTDERR:\n{writer.stderr}"
            )
        if not writer_payload.get("snapshots"):
            raise RuntimeError(
                "docker writer produced no snapshots: "
                f"{json.dumps(writer_payload, ensure_ascii=False)}"
            )
        session_id = str(writer_payload["session_id"])
        snapshots = list(writer_payload["snapshots"])
        http_result = exercise_review_http(
            base_url=base_url,
            headers=headers,
            session_id=session_id,
            snapshots=snapshots,
        )
        reader = _run_docker_backend_python_file(
            env=env,
            compose_project_name=compose_project_name,
            temp_root=temp_root,
            script_name="review_http_reader.py",
            python_code=docker_reader_code(memory_name),
            timeout=300,
        )
        if reader.returncode != 0:
            raise RuntimeError(f"docker reader failed:\nSTDOUT:\n{reader.stdout}\nSTDERR:\n{reader.stderr}")
        reader_payload = parse_json_output(reader)
        if not isinstance(reader_payload, dict):
            raise RuntimeError(
                "docker reader returned invalid JSON:\n"
                f"STDOUT:\n{reader.stdout}\nSTDERR:\n{reader.stderr}"
            )
        if reader_payload.get("content") != original_content:
            raise RuntimeError(f"docker restored content mismatch: {reader_payload}")
        return SmokeResult(
            mode="docker",
            status="PASS",
            summary="sessions/snapshots/detail/diff/rollback/delete/clear all passed",
            details=json.dumps(
                {
                    "session_id": session_id,
                    "snapshot_count": len(snapshots),
                    "restored_content": reader_payload.get("content"),
                    "original_content": original_content,
                    **http_result,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        summary = str(exc).splitlines()[0] if str(exc).splitlines() else type(exc).__name__
        return SmokeResult("docker", "FAIL", summary, str(exc))
    finally:
        run(
            [
                "docker",
                "compose",
                "--env-file",
                docker_env_file,
                "-f",
                "docker-compose.yml",
                "down",
                "--remove-orphans",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            timeout=600,
        )


def build_report(results: list[SmokeResult]) -> str:
    lines = [
        "# Review Snapshots HTTP Smoke Report",
        "",
        "| Mode | Status | Summary |",
        "|---|---|---|",
    ]
    for result in results:
        lines.append(f"| {result.mode} | {result.status} | {result.summary} |")
    failures = [item for item in results if item.status != "PASS"]
    if failures:
        lines.extend(["", "## Failures", ""])
        for failure in failures:
            lines.append(f"### {failure.mode}")
            lines.append("")
            lines.append("```text")
            lines.append(failure.details.strip())
            lines.append("```")
            lines.append("")
    else:
        lines.extend(["", "## Details", ""])
        for result in results:
            lines.append(f"### {result.mode}")
            lines.append("")
            lines.append("```json")
            lines.append(result.details.strip())
            lines.append("```")
            lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--modes", default="local,docker", help="comma-separated: local,docker")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    selected_modes = [item.strip().lower() for item in args.modes.split(",") if item.strip()]
    results: list[SmokeResult] = []
    for mode in selected_modes:
        try:
            if mode == "local":
                results.append(run_local_mode())
            elif mode == "docker":
                results.append(run_docker_mode())
            else:
                results.append(SmokeResult(mode, "FAIL", f"unsupported mode: {mode}"))
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            summary = message.splitlines()[0] if message.splitlines() else exc.__class__.__name__
            results.append(SmokeResult(mode, "FAIL", summary, message or repr(exc)))

    report_path = profile_smoke.normalize_host_cli_path(args.report)
    report_path.write_text(build_report(results), encoding="utf-8")
    print(report_path)
    return 0 if all(item.status == "PASS" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
