#!/usr/bin/env python3
"""
Replacement Acceptance E2E Test for Memory Palace

Validates that the Memory Palace plugin can serve as a drop-in replacement
for the default OpenClaw memory system. This is NOT a benchmark -- it is a
functional acceptance gate that exercises create/read/update/search/compact
across Profile B (local hash) and Profile C (external embedding provider).

7 standard scenarios + 2 opt-in scenarios:
  S1: Write-then-Recall (Profile B)
  S2: Update-then-Recall-Latest (Profile B)
  S3: Cross-Session Recall Injection (Profile B)
  S4: Compact-Index-then-Recall (Profile C)
  S5: Provider-Missing Fallback (Profile C degraded)
  S6: compact_context via MCP stdio (Profile B)
  S7: Profile D write guard + hybrid search
  S8: Short high-value immediate recall (Profile B, opt-in)
  S9: Duplicate high-value text does not flush storm (Profile B, opt-in)

Usage:
    python scripts/test_replacement_acceptance_e2e.py
    python scripts/test_replacement_acceptance_e2e.py --skip-profile-c
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error as urllib_error, request as urllib_request

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"

# ---- Profile C defaults (read from env, no hardcoded credentials) ----
_DEFAULT_EMBED_API_BASE = "http://127.0.0.1:11435/v1"
_DEFAULT_EMBED_API_KEY = ""
_DEFAULT_EMBED_MODEL = "qwen3-embedding:8b-q8_0-ctx8192"
_DEFAULT_EMBED_DIM = "1024"

_DEFAULT_RERANKER_API_BASE = "http://127.0.0.1:8080/v1"
_DEFAULT_RERANKER_API_KEY = ""
_DEFAULT_RERANKER_MODEL = "Qwen3-Reranker-8B"

_DEFAULT_LLM_API_BASE = "http://127.0.0.1:8318/v1/chat/completions"  # noqa: F841
_DEFAULT_LLM_API_KEY = ""  # noqa: F841
_DEFAULT_LLM_MODEL = "gpt-5.4-mini"  # noqa: F841

BACKEND_STARTUP_TIMEOUT = 30.0
BACKEND_STARTUP_POLL = 0.3
INCLUDE_HIGH_VALUE_RUNTIME_SCENARIOS = str(
    os.environ.get("OPENCLAW_ACCEPTANCE_INCLUDE_HIGH_VALUE_RUNTIME_SCENARIOS", "")
).strip().lower() in {"1", "true", "yes"}


# =====================================================================
# Helpers
# =====================================================================

def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _force_kill_signal():
    return signal.SIGTERM if sys.platform == "win32" else signal.SIGTERM


def _health_ok(port: int) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    try:
        req = urllib_request.Request(url, method="GET")
        with urllib_request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _http_json(
    method: str,
    url: str,
    body: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 60.0,
) -> dict:
    """Simple stdlib-only HTTP JSON helper."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib_request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib_error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return {"_http_error": exc.code, "_detail": json.loads(raw)}
        except Exception:
            return {"_http_error": exc.code, "_detail": raw}


def _mcp_tool_text(result: Any) -> str:
    return "".join(
        block.text for block in getattr(result, "content", [])
        if hasattr(block, "text")
    )


def _parse_json_text(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except Exception:
        return {}


def _run_direct_tool_session(
    *,
    env: Dict[str, str],
    code: str,
    timeout: float = 90.0,
) -> Dict[str, Any]:
    process = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    stdout = (process.stdout or "").strip()
    stderr = (process.stderr or "").strip()
    if process.returncode != 0:
        return {
            "_error": f"subprocess_exit_{process.returncode}",
            "stdout": stdout,
            "stderr": stderr,
        }
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return {"_error": "empty_stdout", "stdout": stdout, "stderr": stderr}
    payload = _parse_json_text(lines[-1])
    if not payload:
        return {
            "_error": "invalid_json_stdout",
            "stdout": stdout,
            "stderr": stderr,
        }
    if stderr:
        payload["_stderr"] = stderr
    return payload


# =====================================================================
# Backend lifecycle
# =====================================================================

class ManagedBackend:
    """Start/stop a uvicorn backend in a temp workspace."""

    def __init__(
        self,
        *,
        workspace: Path,
        port: int,
        env_overrides: Optional[dict] = None,
        label: str = "backend",
    ):
        self.workspace = workspace
        self.port = port
        self.label = label
        self.db_path = workspace / "data" / "memory-palace.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.stdout_path = workspace / f"{label}.stdout.log"
        self.stderr_path = workspace / f"{label}.stderr.log"
        self.capture_logs = str(os.environ.get("OPENCLAW_ACCEPTANCE_CAPTURE_BACKEND_LOGS") or "").strip().lower() in {
            "1", "true", "yes",
        }

        self.env = self._build_env(env_overrides or {})
        self.process: Optional[subprocess.Popen] = None
        self._stdout_handle = None
        self._stderr_handle = None

    def _build_env(self, overrides: dict) -> dict:
        env = dict(os.environ)
        # Isolation: unique DB, no real openclaw config
        env["DATABASE_URL"] = self.database_url
        env["MCP_API_KEY"] = ""
        env["MCP_API_KEY_ALLOW_INSECURE_LOCAL"] = "true"
        env["RUNTIME_AUTO_FLUSH_ENABLED"] = "false"
        env["RUNTIME_INDEX_WORKER_ENABLED"] = "true"
        env["RUNTIME_INDEX_DEFER_ON_WRITE"] = "false"
        env["WRITE_GUARD_LLM_ENABLED"] = "false"
        env["COMPACT_GIST_LLM_ENABLED"] = "false"
        # Prevent loading project .env by pointing to an empty temp env file
        empty_env_file = self.workspace / ".env.empty"
        empty_env_file.write_text("# empty isolation env\n", encoding="utf-8")
        env["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(empty_env_file)
        env.update(overrides)
        return env

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        stdout_target = None
        stderr_target = None
        if self.capture_logs:
            self._stdout_handle = open(self.stdout_path, "w", encoding="utf-8")
            self._stderr_handle = open(self.stderr_path, "w", encoding="utf-8")
            stdout_target = self._stdout_handle
            stderr_target = self._stderr_handle
        cmd = [
            sys.executable, "-m", "uvicorn", "main:app",
            "--app-dir", str(BACKEND_DIR),
            "--host", "127.0.0.1",
            "--port", str(self.port),
            "--log-level", "warning",
        ]
        self.process = subprocess.Popen(
            cmd,
            env=self.env,
            stdout=stdout_target,
            stderr=stderr_target,
            cwd=str(BACKEND_DIR),
        )
        deadline = time.monotonic() + BACKEND_STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                stderr = ""
                if self.capture_logs and self.stderr_path.exists():
                    stderr = self.stderr_path.read_text(encoding="utf-8", errors="replace")
                raise RuntimeError(
                    f"[{self.label}] Backend exited unexpectedly "
                    f"(rc={self.process.returncode}): {stderr[:500]}"
                )
            if _health_ok(self.port):
                return
            time.sleep(BACKEND_STARTUP_POLL)
        self.stop()
        raise TimeoutError(f"[{self.label}] Backend did not become healthy within {BACKEND_STARTUP_TIMEOUT}s")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            if self._stdout_handle is not None:
                self._stdout_handle.close()
                self._stdout_handle = None
            if self._stderr_handle is not None:
                self._stderr_handle.close()
                self._stderr_handle = None
            return
        try:
            self.process.send_signal(_force_kill_signal())
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
                self.process.wait(timeout=3)
            except Exception:
                pass
        finally:
            if self._stdout_handle is not None:
                self._stdout_handle.close()
                self._stdout_handle = None
            if self._stderr_handle is not None:
                self._stderr_handle.close()
                self._stderr_handle = None


# =====================================================================
# REST API wrappers (using /browse/* + /maintenance/*)
# =====================================================================

def api_create_memory(
    base_url: str,
    *,
    parent_path: str,
    content: str,
    title: Optional[str] = None,
    priority: int = 5,
    disclosure: str = "",
    domain: str = "core",
) -> dict:
    body: Dict[str, Any] = {
        "parent_path": parent_path,
        "content": content,
        "priority": priority,
        "domain": domain,
    }
    if title:
        body["title"] = title
    if disclosure:
        body["disclosure"] = disclosure
    return _http_json("POST", f"{base_url}/browse/node", body=body)


def api_get_memory(
    base_url: str,
    *,
    path: str,
    domain: str = "core",
    timeout: float = 60.0,
) -> dict:
    from urllib.parse import urlencode
    params = urlencode({"path": path, "domain": domain})
    return _http_json("GET", f"{base_url}/browse/node?{params}", timeout=timeout)


def api_update_memory(
    base_url: str,
    *,
    path: str,
    content: str,
    domain: str = "core",
) -> dict:
    from urllib.parse import urlencode
    params = urlencode({"path": path, "domain": domain})
    body = {"content": content}
    return _http_json("PUT", f"{base_url}/browse/node?{params}", body=body)


def api_search_memory(
    base_url: str,
    *,
    query: str,
    mode: str = "keyword",
    max_results: int = 10,
    domain: Optional[str] = None,
    timeout: float = 60.0,
) -> dict:
    body: Dict[str, Any] = {
        "query": query,
        "mode": mode,
        "max_results": max_results,
    }
    if domain:
        body["filters"] = {"domain": domain}
    return _http_json(
        "POST",
        f"{base_url}/maintenance/observability/search",
        body=body,
        timeout=timeout,
    )


def api_rebuild_index(
    base_url: str,
    *,
    wait: bool = True,
    timeout_seconds: int = 60,
    timeout: float = 90.0,
) -> dict:
    body = {"wait": wait, "timeout_seconds": timeout_seconds}
    return _http_json(
        "POST",
        f"{base_url}/maintenance/index/rebuild",
        body=body,
        timeout=timeout,
    )


# =====================================================================
# Scenario result builder
# =====================================================================

class ScenarioResult:
    def __init__(self, scenario_id: str, name: str):
        self.scenario_id = scenario_id
        self.name = name
        self._start = time.monotonic()
        self.setup = ""
        self.action = ""
        self.expected = ""
        self.actual = ""
        self.pass_fail = "FAIL"
        self.artifact_path = ""
        self.notes: list[str] = []

    def finish(self) -> dict:
        duration_ms = round((time.monotonic() - self._start) * 1000)
        return {
            "scenario": self.scenario_id,
            "name": self.name,
            "setup": self.setup,
            "action": self.action,
            "expected": self.expected,
            "actual": self.actual,
            "pass_fail": self.pass_fail,
            "artifact_path": self.artifact_path,
            "duration_ms": duration_ms,
            "notes": self.notes,
        }


# =====================================================================
# Scenarios
# =====================================================================

def run_s1(backend: ManagedBackend) -> dict:
    """S1: Write-then-Recall (Profile B)"""
    r = ScenarioResult("S1", "Write-then-Recall (Profile B)")
    r.setup = f"Backend on port {backend.port}, Profile B, temp workspace"
    marker = f"Phase3_acceptance_S1_{secrets.token_hex(4)}"
    content = f"Phase3测试记忆条目_唯一标识_{marker}"

    try:
        # Create at root level (no parent nesting needed)
        create_resp = api_create_memory(
            backend.base_url,
            parent_path="",
            content=content,
            title="accept_s1",
            domain="core",
        )
        r.action = f"create_memory -> {json.dumps(create_resp, ensure_ascii=False)[:200]}"

        if create_resp.get("_http_error"):
            r.actual = f"HTTP error on create: {create_resp}"
            return r.finish()

        # Search
        search_resp = api_search_memory(
            backend.base_url,
            query=f"Phase3测试记忆条目 {marker}",
            mode="keyword",
        )
        r.action += f" | search -> {json.dumps(search_resp, ensure_ascii=False)[:200]}"
        r.expected = f"Search results contain '{marker}'"

        # Check results -- search must find it, no direct-read fallback
        results = search_resp.get("results", [])
        found_in_search = any(marker in json.dumps(item, ensure_ascii=False) for item in results)

        r.actual = f"Content found in search: {found_in_search}"
        r.pass_fail = "PASS" if found_in_search else "FAIL"
    except Exception as exc:
        r.actual = f"Exception: {exc}"
    return r.finish()


def run_s2(backend: ManagedBackend) -> dict:
    """S2: Update-then-Recall-Latest (Profile B)"""
    r = ScenarioResult("S2", "Update-then-Recall-Latest (Profile B)")
    r.setup = f"Reuse S1 backend, port {backend.port}"
    marker = secrets.token_hex(4)
    original_content = f"original_content_S2_{marker}"
    updated_content = f"updated_content_S2_v2_{marker}"

    try:
        # Create at root level
        create_resp = api_create_memory(
            backend.base_url,
            parent_path="",
            content=original_content,
            title="accept_s2",
            domain="core",
        )
        r.action = f"create -> {json.dumps(create_resp, ensure_ascii=False)[:150]}"

        if create_resp.get("_http_error"):
            r.actual = f"HTTP error on create: {create_resp}"
            return r.finish()

        # Update
        update_resp = api_update_memory(
            backend.base_url,
            path="accept_s2",
            content=updated_content,
            domain="core",
        )
        r.action += f" | update -> {json.dumps(update_resp, ensure_ascii=False)[:150]}"

        if update_resp.get("_http_error"):
            r.actual = f"HTTP error on update: {update_resp}"
            return r.finish()

        # Read back to verify
        read_resp = api_get_memory(backend.base_url, path="accept_s2", domain="core")
        node_content = (read_resp.get("node") or {}).get("content", "")

        r.expected = f"Content matches updated version with marker '{marker}'"
        has_updated = updated_content in node_content
        has_original = original_content in node_content

        r.actual = f"Updated found: {has_updated}, Original still present: {has_original}"
        r.pass_fail = "PASS" if has_updated and not has_original else "FAIL"
    except Exception as exc:
        r.actual = f"Exception: {exc}"
    return r.finish()


def run_s3(workspace: Path) -> dict:
    """S3: Cross-Session Recall Injection (Profile B)"""
    r = ScenarioResult("S3", "Cross-Session Recall Injection (Profile B)")
    port = _find_free_port()
    marker = secrets.token_hex(4)
    content = f"cross_session_persist_{marker}"

    profile_b_env = {
        "SEARCH_DEFAULT_MODE": "keyword",
        "RETRIEVAL_EMBEDDING_BACKEND": "hash",
    }
    backend = ManagedBackend(
        workspace=workspace,
        port=port,
        env_overrides=profile_b_env,
        label="S3-phase1",
    )
    backend2: Optional[ManagedBackend] = None
    r.setup = f"Start backend, create memory, stop, restart, read back. Port {port}"

    try:
        # Phase 1: start, write, stop
        backend.start()
        create_resp = api_create_memory(
            backend.base_url,
            parent_path="",
            content=content,
            title="accept_s3",
            domain="core",
        )
        r.action = f"phase1 create -> {json.dumps(create_resp, ensure_ascii=False)[:150]}"

        if create_resp.get("_http_error"):
            r.actual = f"HTTP error on create: {create_resp}"
            backend.stop()
            return r.finish()

        # Verify write before restart
        read_resp_1 = api_get_memory(backend.base_url, path="accept_s3", domain="core")
        pre_restart = (read_resp_1.get("node") or {}).get("content", "")
        r.action += f" | pre-restart content ok: {marker in pre_restart}"

        pid1 = backend.process.pid if backend.process else None
        backend.stop()
        time.sleep(1.0)  # give OS time to release port

        # Phase 2: restart with same workspace (same DB), read back
        backend2 = ManagedBackend(
            workspace=workspace,
            port=port,
            env_overrides=profile_b_env,
            label="S3-phase2",
        )
        backend2.start()
        pid2 = backend2.process.pid if backend2.process else None

        # Verify PID changed (proves real restart, not reuse)
        if pid1 and pid2 and pid1 == pid2:
            r.notes.append(f"WARNING: PID did not change ({pid1}=={pid2})")

        read_resp = api_get_memory(backend2.base_url, path="accept_s3", domain="core")
        node_content = (read_resp.get("node") or {}).get("content", "")

        r.expected = f"Content with marker '{marker}' persists after restart (pid1={pid1}, pid2={pid2})"
        found = marker in node_content
        pid_changed = pid1 != pid2
        r.actual = f"Content found after restart: {found}, PID changed: {pid_changed}"
        r.pass_fail = "PASS" if found and pid_changed else "FAIL"

        backend2.stop()
    except Exception as exc:
        r.actual = f"Exception: {exc}"
        for b in [backend, backend2]:
            if b is not None:
                try:
                    b.stop()
                except Exception:
                    pass
    return r.finish()


def run_s4(workspace: Path) -> dict:
    """S4: Compact-Index-then-Recall (Profile C)"""
    r = ScenarioResult("S4", "Compact-Index-then-Recall (Profile C)")
    port = _find_free_port()
    marker = secrets.token_hex(4)
    content = f"profile_c_compact_test_{marker}"

    embed_base = os.environ.get("RETRIEVAL_EMBEDDING_API_BASE", _DEFAULT_EMBED_API_BASE)
    embed_key = os.environ.get("RETRIEVAL_EMBEDDING_API_KEY", _DEFAULT_EMBED_API_KEY)
    embed_model = os.environ.get("RETRIEVAL_EMBEDDING_MODEL", _DEFAULT_EMBED_MODEL)
    embed_dim = os.environ.get("RETRIEVAL_EMBEDDING_DIM", _DEFAULT_EMBED_DIM)

    reranker_base = os.environ.get("RETRIEVAL_RERANKER_API_BASE", _DEFAULT_RERANKER_API_BASE)
    reranker_key = os.environ.get("RETRIEVAL_RERANKER_API_KEY", _DEFAULT_RERANKER_API_KEY)
    reranker_model = os.environ.get("RETRIEVAL_RERANKER_MODEL", _DEFAULT_RERANKER_MODEL)

    profile_c_env = {
        "SEARCH_DEFAULT_MODE": "hybrid",
        "RETRIEVAL_EMBEDDING_BACKEND": "api",
        "RETRIEVAL_EMBEDDING_API_BASE": embed_base,
        "RETRIEVAL_EMBEDDING_API_KEY": embed_key,
        "RETRIEVAL_EMBEDDING_MODEL": embed_model,
        "RETRIEVAL_EMBEDDING_DIM": embed_dim,
        "RETRIEVAL_RERANKER_ENABLED": "true",
        "RETRIEVAL_RERANKER_API_BASE": reranker_base,
        "RETRIEVAL_RERANKER_API_KEY": reranker_key,
        "RETRIEVAL_RERANKER_MODEL": reranker_model,
        "RUNTIME_INDEX_WORKER_ENABLED": "true",
        "RUNTIME_INDEX_DEFER_ON_WRITE": "false",
    }
    remote_timeout = 90.0

    backend = ManagedBackend(
        workspace=workspace,
        port=port,
        env_overrides=profile_c_env,
        label="S4-profile-c",
    )
    r.setup = f"Profile C, embed={embed_base}, reranker={reranker_base}, port {port}"

    try:
        backend.start()

        # Create at root level
        create_resp = api_create_memory(
            backend.base_url,
            parent_path="",
            content=content,
            title="accept_s4",
            domain="core",
        )
        r.action = f"create -> {json.dumps(create_resp, ensure_ascii=False)[:150]}"

        if create_resp.get("_http_error"):
            r.actual = f"HTTP error on create: {create_resp}"
            backend.stop()
            return r.finish()

        # Note: compact_context is MCP-only (no REST endpoint).
        # We verify the index rebuild + hybrid search chain which is the REST-accessible path.
        # compact_context would be tested via MCP transport in a full host integration.
        r.notes.append("compact_context skipped (MCP-only, no REST endpoint)")

        # Rebuild index (force reindex so embedding is computed)
        time.sleep(0.5)
        rebuild_resp = api_rebuild_index(
            backend.base_url,
            wait=True,
            timeout_seconds=60,
            timeout=remote_timeout,
        )
        r.action += f" | rebuild_index -> {json.dumps(rebuild_resp, ensure_ascii=False)[:150]}"

        # Search with hybrid mode -- must find via hybrid, no fallback allowed
        search_resp = api_search_memory(
            backend.base_url,
            query=f"profile_c_compact_test {marker}",
            mode="hybrid",
            timeout=remote_timeout,
        )
        r.action += " | search(hybrid)"

        results = search_resp.get("results", [])
        found_hybrid = any(marker in json.dumps(item, ensure_ascii=False) for item in results)
        mode_applied = search_resp.get("mode_applied", search_resp.get("mode", "unknown"))

        r.expected = f"Content with marker '{marker}' retrievable via hybrid after compact+reindex"
        r.actual = f"Found in hybrid: {found_hybrid}, mode_applied: {mode_applied}"
        r.pass_fail = "PASS" if found_hybrid else "FAIL"
        if not found_hybrid:
            r.notes.append("Hybrid search failed to find content after compact+reindex")

        backend.stop()
    except Exception as exc:
        r.actual = f"Exception: {exc}"
        try:
            backend.stop()
        except Exception:
            pass
    return r.finish()


def run_s5(workspace: Path) -> dict:
    """S5: Provider-Missing Fallback (Profile C -> degraded)"""
    r = ScenarioResult("S5", "Provider-Missing Fallback (Profile C degraded)")
    port = _find_free_port()
    marker = secrets.token_hex(4)
    content = f"degraded_fallback_test_{marker}"

    embed_base = os.environ.get("RETRIEVAL_EMBEDDING_API_BASE", _DEFAULT_EMBED_API_BASE)
    embed_key = os.environ.get("RETRIEVAL_EMBEDDING_API_KEY", _DEFAULT_EMBED_API_KEY)
    embed_model = os.environ.get("RETRIEVAL_EMBEDDING_MODEL", _DEFAULT_EMBED_MODEL)
    embed_dim = os.environ.get("RETRIEVAL_EMBEDDING_DIM", _DEFAULT_EMBED_DIM)

    # Intentionally broken reranker URL
    broken_reranker_base = "http://127.0.0.1:1/v1"

    profile_c_degraded_env = {
        "SEARCH_DEFAULT_MODE": "hybrid",
        "RETRIEVAL_EMBEDDING_BACKEND": "api",
        "RETRIEVAL_EMBEDDING_API_BASE": embed_base,
        "RETRIEVAL_EMBEDDING_API_KEY": embed_key,
        "RETRIEVAL_EMBEDDING_MODEL": embed_model,
        "RETRIEVAL_EMBEDDING_DIM": embed_dim,
        "RETRIEVAL_RERANKER_ENABLED": "true",
        "RETRIEVAL_RERANKER_API_BASE": broken_reranker_base,
        "RETRIEVAL_RERANKER_API_KEY": "sk-broken",
        "RETRIEVAL_RERANKER_MODEL": "broken-model",
        "RUNTIME_INDEX_WORKER_ENABLED": "true",
        "RUNTIME_INDEX_DEFER_ON_WRITE": "false",
    }
    remote_timeout = 90.0

    backend = ManagedBackend(
        workspace=workspace,
        port=port,
        env_overrides=profile_c_degraded_env,
        label="S5-degraded",
    )
    r.setup = f"Profile C with broken reranker at {broken_reranker_base}, port {port}"

    try:
        backend.start()

        # Create should still work (reranker not needed for write)
        create_resp = api_create_memory(
            backend.base_url,
            parent_path="",
            content=content,
            title="accept_s5",
            domain="core",
        )
        r.action = f"create -> {json.dumps(create_resp, ensure_ascii=False)[:150]}"

        if create_resp.get("_http_error"):
            r.actual = f"HTTP error on create: {create_resp}"
            backend.stop()
            return r.finish()

        # Wait for index so embedding is computed before search
        time.sleep(0.5)
        api_rebuild_index(
            backend.base_url,
            wait=True,
            timeout_seconds=30,
            timeout=remote_timeout,
        )

        # Search with hybrid mode -- this is the mode that triggers reranker
        search_resp = api_search_memory(
            backend.base_url,
            query=f"degraded_fallback_test {marker}",
            mode="hybrid",
            timeout=remote_timeout,
        )
        r.action += f" | search(hybrid) -> status={search_resp.get('_http_error', 'ok')}"

        # Check: no crash (we got a response), and results exist
        is_http_error = "_http_error" in search_resp
        results = search_resp.get("results", [])
        found = any(marker in json.dumps(item, ensure_ascii=False) for item in results)

        # Check for degraded indicators -- broken reranker should trigger degradation
        degraded = search_resp.get("degraded", False)
        degrade_reasons = search_resp.get("degrade_reasons", [])

        r.expected = (
            f"No crash, results returned via hybrid mode, "
            f"content with marker '{marker}' retrievable, "
            f"reranker degradation expected (degraded=True or graceful skip)"
        )
        r.actual = (
            f"HTTP error: {is_http_error}, found: {found}, "
            f"degraded: {degraded}, reasons: {degrade_reasons}"
        )
        # PASS if: no HTTP 5xx AND content found AND system didn't crash
        # Note: degraded may be True (reranker failed) or False (reranker silently skipped)
        # Both are acceptable fallback behaviors -- the key is no crash + results returned
        no_server_error = not is_http_error or search_resp.get("_http_error", 0) < 500
        r.pass_fail = "PASS" if no_server_error and found else "FAIL"
        if degraded:
            r.notes.append(f"Degraded mode confirmed: {degrade_reasons}")
        else:
            r.notes.append("Reranker failure was silently absorbed (no degraded flag)")

        backend.stop()
    except Exception as exc:
        r.actual = f"Exception: {exc}"
        try:
            backend.stop()
        except Exception:
            pass
    return r.finish()


# =====================================================================
# S7: Profile D (Full API-first: embedding + reranker + LLM guard)
# =====================================================================

def run_s7(workspace: Path) -> dict:
    """S7: Profile D CRUD + Write Guard + Search (full API stack)"""
    r = ScenarioResult("S7", "Profile D: Write Guard + Hybrid Search")
    port = _find_free_port()
    marker = secrets.token_hex(4)

    embed_base = os.environ.get("RETRIEVAL_EMBEDDING_API_BASE", _DEFAULT_EMBED_API_BASE)
    embed_key = os.environ.get("RETRIEVAL_EMBEDDING_API_KEY", _DEFAULT_EMBED_API_KEY)
    embed_model = os.environ.get("RETRIEVAL_EMBEDDING_MODEL", _DEFAULT_EMBED_MODEL)
    embed_dim = os.environ.get("RETRIEVAL_EMBEDDING_DIM", _DEFAULT_EMBED_DIM)

    reranker_base = os.environ.get("RETRIEVAL_RERANKER_API_BASE", _DEFAULT_RERANKER_API_BASE)
    reranker_key = os.environ.get("RETRIEVAL_RERANKER_API_KEY", _DEFAULT_RERANKER_API_KEY)
    reranker_model = os.environ.get("RETRIEVAL_RERANKER_MODEL", _DEFAULT_RERANKER_MODEL)

    llm_base = os.environ.get("WRITE_GUARD_LLM_API_BASE", _DEFAULT_LLM_API_BASE)
    llm_key = os.environ.get("WRITE_GUARD_LLM_API_KEY", _DEFAULT_LLM_API_KEY)
    llm_model = os.environ.get("WRITE_GUARD_LLM_MODEL", _DEFAULT_LLM_MODEL)

    profile_d_env = {
        "SEARCH_DEFAULT_MODE": "hybrid",
        "RETRIEVAL_EMBEDDING_BACKEND": "api",
        "RETRIEVAL_EMBEDDING_API_BASE": embed_base,
        "RETRIEVAL_EMBEDDING_API_KEY": embed_key,
        "RETRIEVAL_EMBEDDING_MODEL": embed_model,
        "RETRIEVAL_EMBEDDING_DIM": embed_dim,
        "RETRIEVAL_RERANKER_ENABLED": "true",
        "RETRIEVAL_RERANKER_API_BASE": reranker_base,
        "RETRIEVAL_RERANKER_API_KEY": reranker_key,
        "RETRIEVAL_RERANKER_MODEL": reranker_model,
        "RETRIEVAL_RERANKER_WEIGHT": "0.35",
        # Profile D: LLM-powered write guard
        "WRITE_GUARD_LLM_ENABLED": "true",
        "WRITE_GUARD_LLM_API_BASE": llm_base,
        "WRITE_GUARD_LLM_API_KEY": llm_key,
        "WRITE_GUARD_LLM_MODEL": llm_model,
        # Force the duplicate/contradiction decision through the LLM guard.
        # Scores are normalized to the 0.0-1.0 range, so thresholds above 1.0
        # disable embedding/keyword fast-paths while still allowing retrieval
        # to build the candidate shortlist for the LLM decision.
        "WRITE_GUARD_SEMANTIC_NOOP_THRESHOLD": "1.01",
        "WRITE_GUARD_SEMANTIC_UPDATE_THRESHOLD": "1.01",
        "WRITE_GUARD_KEYWORD_NOOP_THRESHOLD": "1.01",
        "WRITE_GUARD_KEYWORD_UPDATE_THRESHOLD": "1.01",
        # Profile D: LLM-powered compact gist
        "COMPACT_GIST_LLM_ENABLED": "true",
        "COMPACT_GIST_LLM_API_BASE": llm_base,
        "COMPACT_GIST_LLM_API_KEY": llm_key,
        "COMPACT_GIST_LLM_MODEL": llm_model,
        "RUNTIME_INDEX_WORKER_ENABLED": "true",
        "RUNTIME_INDEX_DEFER_ON_WRITE": "false",
    }
    remote_timeout = 90.0

    backend = ManagedBackend(
        workspace=workspace,
        port=port,
        env_overrides=profile_d_env,
        label="S7-profile-d",
    )
    r.setup = (
        f"Profile D, embed={embed_base}, reranker={reranker_base}, "
        f"llm={llm_base}, write_guard=true, compact_gist=true, port {port}"
    )

    try:
        backend.start()

        # 1. Create the baseline memory.
        baseline_content = (
            f"profile_d_write_guard_test_{marker}: "
            "default_provider=alpha; enforcement_mode=monitor; "
            "this is the active configuration."
        )
        create_resp = api_create_memory(
            backend.base_url,
            parent_path="",
            content=baseline_content,
            title=f"accept_s7_{marker}",
            domain="core",
        )
        r.action = f"create -> {json.dumps(create_resp, ensure_ascii=False)[:200]}"

        if create_resp.get("_http_error"):
            r.actual = f"HTTP error on create: {create_resp}"
            backend.stop()
            return r.finish()

        # First write: no prior candidate exists, so ADD is expected.
        guard1_method = create_resp.get("guard_method", "unknown")
        guard1_action = create_resp.get("guard_action", "unknown")
        r.notes.append(f"write1: guard_method={guard1_method}, guard_action={guard1_action}")

        # 2. Rebuild index so embedding is computed for the first memory
        time.sleep(0.5)
        rebuild_resp = api_rebuild_index(
            backend.base_url,
            wait=True,
            timeout_seconds=60,
            timeout=remote_timeout,
        )
        r.action += f" | rebuild_index -> {json.dumps(rebuild_resp, ensure_ascii=False)[:100]}"

        # 2b. Wait for index to fully complete so write_guard can find candidates
        time.sleep(1.0)

        # 3. Write a contradictory update with the same marker.
        #    Because fast-path thresholds are set above 1.0, the guard must use
        #    the LLM branch to decide whether this supersedes the baseline entry.
        forced_llm_content = (
            f"profile_d_write_guard_test_{marker}: "
            "correction - default_provider=beta; enforcement_mode=enforce; "
            "this replaces the earlier alpha/monitor configuration."
        )
        create2_resp = api_create_memory(
            backend.base_url,
            parent_path="",
            content=forced_llm_content,
            title=f"accept_s7_dup_{marker}",
            domain="core",
        )
        r.action += f" | create2 -> {json.dumps(create2_resp, ensure_ascii=False)[:200]}"

        if create2_resp.get("_http_error"):
            r.actual = f"HTTP error on create2: {create2_resp}"
            backend.stop()
            return r.finish()

        guard2_method = create2_resp.get("guard_method", "unknown")
        guard2_action = create2_resp.get("guard_action", "unknown")
        guard2_reason = create2_resp.get("guard_reason", "")
        guard2_target = create2_resp.get("guard_target_uri") or create2_resp.get("uri") or ""
        r.notes.append(
            "write2: "
            f"guard_method={guard2_method}, "
            f"guard_action={guard2_action}, "
            f"guard_target={guard2_target}, "
            f"guard_reason={guard2_reason}"
        )

        # 4. Search with hybrid mode
        search_resp = api_search_memory(
            backend.base_url,
            query=f"profile_d_write_guard_test {marker}",
            mode="hybrid",
            timeout=remote_timeout,
        )
        r.action += " | search(hybrid)"

        results = search_resp.get("results", [])
        found = any(marker in json.dumps(item, ensure_ascii=False) for item in results)
        mode_applied = search_resp.get("mode_applied", search_resp.get("mode", "unknown"))

        # S7 now requires the contradictory second write to go through the LLM
        # guard, rather than allowing embedding/keyword fast-paths to pass.
        # The current backend preserves model-supplied method strings, so a
        # successful LLM proof may show up as "llm"/"llm_diff_rescue" or as a
        # free-form method description that is outside the fixed heuristic set.
        heuristic_guard_methods = {
            "keyword",
            "embedding",
            "embedding_cross_check",
            "normalized_cross_check",
            "keyword_single_pipeline",
            "embedding_single_pipeline",
            "visual_hash",
            "visual_namespace",
            "exception",
            "unknown",
            "none",
        }
        llm_triggered = guard2_method in {"llm", "llm_diff_rescue"} or (
            bool(guard2_reason) and guard2_method not in heuristic_guard_methods
        )
        any_guard_active = guard1_method != "unknown" and guard2_method != "unknown"

        r.expected = (
            f"Profile D: write guard evaluates, hybrid search finds marker '{marker}', "
            "forced contradictory write is decided by the LLM guard"
        )
        r.actual = (
            f"Found: {found}, mode={mode_applied}, "
            f"write1_guard={guard1_method}/{guard1_action}, "
            f"write2_guard={guard2_method}/{guard2_action}, "
            f"write2_target={guard2_target or '-'}, "
            f"llm_triggered={llm_triggered}"
        )
        # PASS if: search still finds the marker, guard fields are populated,
        # and the contradictory second write was decided by the LLM branch.
        r.pass_fail = "PASS" if found and any_guard_active and llm_triggered else "FAIL"
        if llm_triggered and guard2_method not in {"llm", "llm_diff_rescue"}:
            r.notes.append(
                "write2 returned a model-supplied guard_method string; "
                "counted as LLM evidence because it is outside the fixed "
                "heuristic guard method set."
            )
        if not llm_triggered:
            r.notes.append(
                "Forced-LLM guard proof failed: the contradictory second write did "
                "not report guard_method=llm."
            )

        backend.stop()
    except Exception as exc:
        r.actual = f"Exception: {exc}"
        try:
            backend.stop()
        except Exception:
            pass
    return r.finish()


# =====================================================================
# S6: compact_context via MCP stdio
# =====================================================================

def _run_mcp_stdio_compact_scenario(workspace: Path) -> dict:
    """S6: compact_context via MCP stdio transport.

    Spawns the MCP server over stdio, creates a memory, calls
    compact_context(force=true), then searches to confirm content
    is still retrievable.
    """
    r = ScenarioResult("S6", "compact_context via MCP stdio (Profile B)")
    marker = secrets.token_hex(4)
    content = f"stdio_compact_test_{marker}"
    db_path = workspace / "data" / "memory-palace.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite+aiosqlite:///{db_path}"

    env_file = workspace / ".env.empty"
    env_file.write_text("# empty isolation env\n", encoding="utf-8")

    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env["MCP_API_KEY"] = ""
    env["MCP_API_KEY_ALLOW_INSECURE_LOCAL"] = "true"
    env["RUNTIME_AUTO_FLUSH_ENABLED"] = "false"
    env["RUNTIME_INDEX_WORKER_ENABLED"] = "true"
    env["RUNTIME_INDEX_DEFER_ON_WRITE"] = "false"
    env["WRITE_GUARD_LLM_ENABLED"] = "false"
    env["COMPACT_GIST_LLM_ENABLED"] = "false"
    env["SEARCH_DEFAULT_MODE"] = "keyword"
    env["RETRIEVAL_EMBEDDING_BACKEND"] = "hash"
    env["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(env_file)

    r.setup = f"MCP stdio, Profile B, temp workspace {workspace.name}"

    try:
        result = asyncio.run(_mcp_stdio_compact_async(env, content, marker, r))
        return result
    except Exception as exc:
        r.actual = f"Exception: {exc}"
        return r.finish()


async def _mcp_stdio_compact_async(
    env: dict, content: str, marker: str, r: "ScenarioResult",
) -> dict:
    """Async core: connect to MCP stdio, create → compact → search."""
    try:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError:
        r.actual = "SKIP: mcp package not available in current Python environment"
        r.pass_fail = "SKIP"
        return r.finish()

    posix_wrapper = REPO_ROOT / "scripts" / "run_memory_palace_mcp_stdio.sh"
    if posix_wrapper.is_file() and sys.platform != "win32":
        server = StdioServerParameters(
            command="/bin/bash",
            args=[str(posix_wrapper)],
            cwd=str(REPO_ROOT),
            env=env,
        )
    else:
        server = StdioServerParameters(
            command=sys.executable,
            args=["mcp_server.py"],
            cwd=str(BACKEND_DIR),
            env=env,
        )

    stderr_file = open(os.devnull, "w")  # noqa: SIM115

    try:
        async with stdio_client(server, errlog=stderr_file) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                # 1. Verify tools are available
                tools = await session.list_tools()
                tool_names = {t.name for t in tools.tools}
                if "compact_context" not in tool_names:
                    r.actual = f"compact_context not in tool list: {tool_names}"
                    return r.finish()

                # 2. Create memory
                create_result = await session.call_tool(
                    "create_memory",
                    {
                        "parent_uri": "core://",
                        "content": content,
                        "title": f"accept_s6_{marker}",
                        "priority": 5,
                    },
                )
                create_text = _mcp_tool_text(create_result)
                r.action = f"create_memory -> {create_text[:150]}"

                # 3. Call compact_context (force=true)
                compact_result = await session.call_tool(
                    "compact_context",
                    {"force": True, "reason": "acceptance_test_s6"},
                )
                compact_text = _mcp_tool_text(compact_result)
                r.action += f" | compact_context -> {compact_text[:150]}"

                # 4. Search for the content
                search_result = await session.call_tool(
                    "search_memory",
                    {"query": f"stdio_compact_test {marker}"},
                )
                search_text = _mcp_tool_text(search_result)
                r.action += " | search_memory"

                found = marker in search_text
                r.expected = f"Content with marker '{marker}' retrievable after compact_context via MCP stdio"
                r.actual = f"Found after compact: {found}"
                r.pass_fail = "PASS" if found else "FAIL"
    except Exception as exc:
        r.actual = f"MCP stdio error: {exc}"
    finally:
        stderr_file.close()

    return r.finish()


def _run_mcp_stdio_short_high_value_recall_scenario(workspace: Path) -> dict:
    """S8: short high-value session can flush without force and recall immediately."""
    r = ScenarioResult("S8", "Short High-Value Immediate Recall (Profile B)")
    marker = secrets.token_hex(4)
    db_path = workspace / "data" / "memory-palace.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite+aiosqlite:///{db_path}"

    env_file = workspace / ".env.empty"
    env_file.write_text("# empty isolation env\n", encoding="utf-8")

    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env["MCP_API_KEY"] = ""
    env["MCP_API_KEY_ALLOW_INSECURE_LOCAL"] = "true"
    env["RUNTIME_AUTO_FLUSH_ENABLED"] = "false"
    env["RUNTIME_INDEX_WORKER_ENABLED"] = "true"
    env["RUNTIME_INDEX_DEFER_ON_WRITE"] = "false"
    env["WRITE_GUARD_LLM_ENABLED"] = "false"
    env["COMPACT_GIST_LLM_ENABLED"] = "false"
    env["SEARCH_DEFAULT_MODE"] = "keyword"
    env["RETRIEVAL_EMBEDDING_BACKEND"] = "hash"
    env["RUNTIME_FLUSH_MIN_EVENTS"] = "6"
    env["RUNTIME_FLUSH_TRIGGER_CHARS"] = "6000"
    env["RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED"] = "true"
    env["RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS"] = "2"
    env["RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS"] = "120"
    env["RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS_CJK"] = "100"
    env["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(env_file)

    r.setup = f"Direct MCP tool session, Profile B, high-value early flush enabled, temp workspace {workspace.name}"

    query_one = (
        f"default workflow preference marker {marker} remember this workflow for future recall"
    )
    query_two = (
        f"remember the default workflow marker {marker} for this short preference session"
    )
    code = f"""
import asyncio, json, sys
sys.path.insert(0, {str(BACKEND_DIR)!r})
import mcp_server

async def main():
    await mcp_server.startup()
    await mcp_server.search_memory({query_one!r}, mode="keyword", max_results=10)
    await mcp_server.search_memory({query_two!r}, mode="keyword", max_results=10)
    compact_raw = await mcp_server.compact_context(reason="acceptance_test_s8", force=False, max_lines=12)
    compact = json.loads(compact_raw)
    search_raw = await mcp_server.search_memory({marker!r}, mode="keyword", max_results=10)
    tracker = await mcp_server.runtime_state.flush_tracker.summary()
    print(json.dumps({{"compact": compact, "search_raw": search_raw, "tracker": tracker}}, ensure_ascii=False))

asyncio.run(main())
"""
    payload = _run_direct_tool_session(env=env, code=code)
    if payload.get("_error"):
        r.actual = f"Direct tool session error: {payload.get('_error')} {payload.get('stderr', '')[:180]}"
        return r.finish()

    compact_payload = payload.get("compact") or {}
    search_text = str(payload.get("search_raw") or "")
    tracker = payload.get("tracker") or {}
    flushed = bool(compact_payload.get("flushed"))
    persisted = bool(compact_payload.get("data_persisted"))
    source_hash = str(compact_payload.get("source_hash") or "")
    reason = str(compact_payload.get("reason") or "")
    trace_text = str(compact_payload.get("trace_text") or "")
    early_flush_count = int(tracker.get("early_flush_count") or 0)
    flush_results_total = int(tracker.get("flush_results_total") or 0)
    last_source_hash = str(tracker.get("last_source_hash") or "")

    r.action = "direct tool session: search_memory x2 (same process session) | compact_context(force=false)"
    r.expected = (
        f"Two short high-value events flush without force and marker '{marker}' "
        "is immediately searchable."
    )
    r.actual = (
        f"flushed={flushed}, data_persisted={persisted}, "
        f"reason={reason or '-'}, source_hash={source_hash[:12] or '-'}, "
        f"flush_results_total={flush_results_total}, early_flush_count={early_flush_count}, "
        f"found={marker in search_text}"
    )
    r.pass_fail = (
        "PASS"
        if (
            flushed
            and persisted
            and bool(source_hash)
            and marker in search_text
            and marker in trace_text
            and flush_results_total >= 1
            and early_flush_count >= 1
            and last_source_hash == source_hash
        )
        else "FAIL"
    )
    return r.finish()


def _run_mcp_stdio_duplicate_high_value_no_storm_scenario(workspace: Path) -> dict:
    """S9: repeated duplicate high-value events must not flush storm."""
    r = ScenarioResult("S9", "Duplicate High-Value Does Not Flush Storm (Profile B)")
    marker = secrets.token_hex(4)
    db_path = workspace / "data" / "memory-palace.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite+aiosqlite:///{db_path}"

    env_file = workspace / ".env.empty"
    env_file.write_text("# empty isolation env\n", encoding="utf-8")

    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    env["MCP_API_KEY"] = ""
    env["MCP_API_KEY_ALLOW_INSECURE_LOCAL"] = "true"
    env["RUNTIME_AUTO_FLUSH_ENABLED"] = "false"
    env["RUNTIME_INDEX_WORKER_ENABLED"] = "true"
    env["RUNTIME_INDEX_DEFER_ON_WRITE"] = "false"
    env["WRITE_GUARD_LLM_ENABLED"] = "false"
    env["COMPACT_GIST_LLM_ENABLED"] = "false"
    env["SEARCH_DEFAULT_MODE"] = "keyword"
    env["RETRIEVAL_EMBEDDING_BACKEND"] = "hash"
    env["RUNTIME_FLUSH_MIN_EVENTS"] = "6"
    env["RUNTIME_FLUSH_TRIGGER_CHARS"] = "6000"
    env["RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED"] = "true"
    env["RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS"] = "2"
    env["RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS"] = "120"
    env["RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS_CJK"] = "100"
    env["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(env_file)

    r.setup = f"Direct MCP tool session, Profile B, duplicate high-value guardrails enabled, temp workspace {workspace.name}"

    duplicate_query = (
        f"remember workflow preference marker {marker} for duplicate guardrail check"
    )
    unique_query = (
        f"default workflow marker {marker} escalates to unique follow-up preference event"
    )
    code = f"""
import asyncio, json, sys
sys.path.insert(0, {str(BACKEND_DIR)!r})
import mcp_server

async def main():
    await mcp_server.startup()
    await mcp_server.search_memory({duplicate_query!r}, mode="keyword", max_results=10)
    await mcp_server.search_memory({duplicate_query!r}, mode="keyword", max_results=10)
    compact_one = json.loads(await mcp_server.compact_context(reason="acceptance_test_s9_precheck_1", force=False, max_lines=12))
    compact_two = json.loads(await mcp_server.compact_context(reason="acceptance_test_s9_precheck_2", force=False, max_lines=12))
    await mcp_server.search_memory({unique_query!r}, mode="keyword", max_results=10)
    compact_three = json.loads(await mcp_server.compact_context(reason="acceptance_test_s9_flush", force=False, max_lines=12))
    compact_four = json.loads(await mcp_server.compact_context(reason="acceptance_test_s9_postflush", force=False, max_lines=12))
    search_raw = await mcp_server.search_memory({marker!r}, mode="keyword", max_results=10)
    tracker = await mcp_server.runtime_state.flush_tracker.summary()
    print(json.dumps({{
        "compact_one": compact_one,
        "compact_two": compact_two,
        "compact_three": compact_three,
        "compact_four": compact_four,
        "search_raw": search_raw,
        "tracker": tracker
    }}, ensure_ascii=False))

asyncio.run(main())
"""
    payload = _run_direct_tool_session(env=env, code=code)
    if payload.get("_error"):
        r.actual = f"Direct tool session error: {payload.get('_error')} {payload.get('stderr', '')[:180]}"
        return r.finish()

    compact_one = payload.get("compact_one") or {}
    compact_two = payload.get("compact_two") or {}
    compact_three = payload.get("compact_three") or {}
    compact_four = payload.get("compact_four") or {}
    search_text = str(payload.get("search_raw") or "")
    tracker = payload.get("tracker") or {}
    precheck_one = bool(compact_one.get("flushed"))
    precheck_two = bool(compact_two.get("flushed"))
    actual_flush = bool(compact_three.get("flushed"))
    postflush = bool(compact_four.get("flushed"))
    reason_three = str(compact_three.get("reason") or "")
    found = marker in search_text
    flush_results_total = int(tracker.get("flush_results_total") or 0)
    early_flush_count = int(tracker.get("early_flush_count") or 0)

    r.action = (
        "direct tool session: duplicate high-value query x2 | compact_context x2 (expect no flush) "
        "| unique query | compact_context"
    )
    r.expected = (
        "Duplicate high-value events do not flush; one unique follow-up triggers exactly one flush; "
        "immediate re-check does not flush again."
    )
    r.actual = (
        f"precheck1={precheck_one}, precheck2={precheck_two}, "
        f"flush_once={actual_flush}, postflush={postflush}, "
        f"reason={reason_three or '-'}, found={found}, "
        f"flush_results_total={flush_results_total}, early_flush_count={early_flush_count}"
    )
    r.pass_fail = (
        "PASS"
        if (
            (not precheck_one)
            and (not precheck_two)
            and actual_flush
            and (not postflush)
            and found
            and flush_results_total == 1
            and early_flush_count == 1
        )
        else "FAIL"
    )
    return r.finish()


# =====================================================================
# Report generation
# =====================================================================

def generate_json_report(results: list[dict], report_path: Path) -> None:
    total = len(results)
    passed = sum(1 for r in results if r["pass_fail"] == "PASS")
    skipped = sum(1 for r in results if r["pass_fail"] == "SKIP")
    failed = total - passed - skipped
    executed = total - skipped
    report = {
        "title": "Replacement Acceptance E2E Report",
        "generated_at": _utc_iso_now(),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "pass_rate": f"{passed}/{executed} ({passed / executed * 100:.1f}%)" if executed else "N/A",
        },
        "scenarios": results,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def generate_md_report(results: list[dict], report_path: Path) -> None:
    total = len(results)
    passed = sum(1 for r in results if r["pass_fail"] == "PASS")
    skipped = sum(1 for r in results if r["pass_fail"] == "SKIP")
    failed = total - passed - skipped
    executed = total - skipped
    lines = [
        "# Replacement Acceptance E2E Report",
        "",
        f"Generated: {_utc_iso_now()}",
        "",
        "## Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total scenarios | {total} |",
        f"| Passed | {passed} |",
        f"| Failed | {failed} |",
        f"| Skipped | {skipped} |",
        f"| Pass rate | {passed}/{executed} ({passed / executed * 100:.1f}%) |" if executed else "| Pass rate | N/A |",
        "",
        "## Scenario Results",
        "",
    ]
    for r in results:
        emoji = r["pass_fail"]  # PASS / FAIL / SKIP
        lines.append(f"### {r['scenario']}: {r['name']} [{emoji}]")
        lines.append("")
        lines.append(f"- **Duration**: {r['duration_ms']}ms")
        lines.append(f"- **Setup**: {r['setup']}")
        lines.append(f"- **Action**: {r['action'][:300]}")
        lines.append(f"- **Expected**: {r['expected']}")
        lines.append(f"- **Actual**: {r['actual']}")
        if r.get("notes"):
            lines.append(f"- **Notes**: {'; '.join(r['notes'])}")
        lines.append("")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


# =====================================================================
# Main
# =====================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replacement Acceptance E2E Test for Memory Palace",
    )
    parser.add_argument(
        "--skip-profile-c",
        action="store_true",
        help="Skip S4 and S5 (Profile C scenarios requiring external providers)",
    )
    parser.add_argument(
        "--json-report",
        default=str(REPO_ROOT / "backend" / "tests" / "benchmark" / "replacement_acceptance_report.json"),
        help="Path for JSON report output",
    )
    parser.add_argument(
        "--md-report",
        default=str(REPO_ROOT / "backend" / "tests" / "benchmark" / "replacement_acceptance_report.md"),
        help="Path for Markdown report output",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    json_report_path = Path(args.json_report)
    md_report_path = Path(args.md_report)
    skip_profile_c = args.skip_profile_c

    results: list[dict] = []

    # ---- Profile B workspace (shared for S1, S2) ----
    workspace_b = Path(tempfile.mkdtemp(prefix="mp_accept_b_"))
    port_b = _find_free_port()

    profile_b_env = {
        "SEARCH_DEFAULT_MODE": "keyword",
        "RETRIEVAL_EMBEDDING_BACKEND": "hash",
    }
    backend_b = ManagedBackend(
        workspace=workspace_b,
        port=port_b,
        env_overrides=profile_b_env,
        label="profile-b",
    )

    print(f"[acceptance] Starting Profile B backend on port {port_b} ...")
    try:
        backend_b.start()
        print(f"[acceptance] Profile B backend healthy.")

        # S1
        print("[acceptance] Running S1: Write-then-Recall ...")
        s1 = run_s1(backend_b)
        results.append(s1)
        print(f"  -> {s1['pass_fail']} ({s1['duration_ms']}ms)")

        # S2
        print("[acceptance] Running S2: Update-then-Recall-Latest ...")
        s2 = run_s2(backend_b)
        results.append(s2)
        print(f"  -> {s2['pass_fail']} ({s2['duration_ms']}ms)")

        backend_b.stop()
    except Exception as exc:
        print(f"[acceptance] Profile B backend failure: {exc}")
        results.append({
            "scenario": "S1", "name": "Write-then-Recall (Profile B)",
            "pass_fail": "FAIL", "actual": str(exc), "duration_ms": 0,
            "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
        })
        results.append({
            "scenario": "S2", "name": "Update-then-Recall-Latest (Profile B)",
            "pass_fail": "FAIL", "actual": str(exc), "duration_ms": 0,
            "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
        })
        try:
            backend_b.stop()
        except Exception:
            pass

    # S3 (uses its own workspace + restart cycle)
    print("[acceptance] Running S3: Cross-Session Recall Injection ...")
    workspace_s3 = Path(tempfile.mkdtemp(prefix="mp_accept_s3_"))
    try:
        s3 = run_s3(workspace_s3)
        results.append(s3)
        print(f"  -> {s3['pass_fail']} ({s3['duration_ms']}ms)")
    except Exception as exc:
        results.append({
            "scenario": "S3", "name": "Cross-Session Recall Injection (Profile B)",
            "pass_fail": "FAIL", "actual": str(exc), "duration_ms": 0,
            "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
        })

    # S4 & S5 (Profile C)
    if skip_profile_c:
        print("[acceptance] Skipping S4 and S5 (--skip-profile-c)")
        for sid, sname in [
            ("S4", "Compact-Index-then-Recall (Profile C)"),
            ("S5", "Provider-Missing Fallback (Profile C degraded)"),
        ]:
            results.append({
                "scenario": sid, "name": sname,
                "pass_fail": "SKIP", "actual": "Skipped by --skip-profile-c",
                "duration_ms": 0,
                "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
            })
    else:
        print("[acceptance] Running S4: Compact-Index-then-Recall (Profile C) ...")
        workspace_s4 = Path(tempfile.mkdtemp(prefix="mp_accept_s4_"))
        try:
            s4 = run_s4(workspace_s4)
            results.append(s4)
            print(f"  -> {s4['pass_fail']} ({s4['duration_ms']}ms)")
        except Exception as exc:
            results.append({
                "scenario": "S4", "name": "Compact-Index-then-Recall (Profile C)",
                "pass_fail": "FAIL", "actual": str(exc), "duration_ms": 0,
                "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
            })

        print("[acceptance] Running S5: Provider-Missing Fallback ...")
        workspace_s5 = Path(tempfile.mkdtemp(prefix="mp_accept_s5_"))
        try:
            s5 = run_s5(workspace_s5)
            results.append(s5)
            print(f"  -> {s5['pass_fail']} ({s5['duration_ms']}ms)")
        except Exception as exc:
            results.append({
                "scenario": "S5", "name": "Provider-Missing Fallback (Profile C degraded)",
                "pass_fail": "FAIL", "actual": str(exc), "duration_ms": 0,
                "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
            })

    # S6: compact_context via MCP stdio (Profile B, no external deps)
    print("[acceptance] Running S6: compact_context via MCP stdio ...")
    workspace_s6 = Path(tempfile.mkdtemp(prefix="mp_accept_s6_"))
    try:
        s6 = _run_mcp_stdio_compact_scenario(workspace_s6)
        results.append(s6)
        print(f"  -> {s6['pass_fail']} ({s6['duration_ms']}ms)")
    except Exception as exc:
        results.append({
            "scenario": "S6", "name": "compact_context via MCP stdio (Profile B)",
            "pass_fail": "FAIL", "actual": str(exc), "duration_ms": 0,
            "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
        })

    # S7: Profile D (full API stack with write guard + compact gist)
    if skip_profile_c:
        print("[acceptance] Skipping S7 (--skip-profile-c)")
        results.append({
            "scenario": "S7", "name": "Profile D: Write Guard + Hybrid Search",
            "pass_fail": "SKIP", "actual": "Skipped by --skip-profile-c",
            "duration_ms": 0,
            "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
        })
    else:
        print("[acceptance] Running S7: Profile D (Write Guard + Hybrid Search) ...")
        workspace_s7 = Path(tempfile.mkdtemp(prefix="mp_accept_s7_"))
        try:
            s7 = run_s7(workspace_s7)
            results.append(s7)
            print(f"  -> {s7['pass_fail']} ({s7['duration_ms']}ms)")
        except Exception as exc:
            results.append({
                "scenario": "S7", "name": "Profile D: Write Guard + Hybrid Search",
                "pass_fail": "FAIL", "actual": str(exc), "duration_ms": 0,
                "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
            })

    if INCLUDE_HIGH_VALUE_RUNTIME_SCENARIOS:
        # S8: short-session high-value immediate recall (Profile B)
        print("[acceptance] Running S8: short-session high-value immediate recall ...")
        workspace_s8 = Path(tempfile.mkdtemp(prefix="mp_accept_s8_"))
        try:
            s8 = _run_mcp_stdio_short_high_value_recall_scenario(workspace_s8)
            results.append(s8)
            print(f"  -> {s8['pass_fail']} ({s8['duration_ms']}ms)")
        except Exception as exc:
            results.append({
                "scenario": "S8", "name": "Short High-Value Immediate Recall (Profile B)",
                "pass_fail": "FAIL", "actual": str(exc), "duration_ms": 0,
                "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
            })

        # S9: duplicate high-value text does not flush storm (Profile B)
        print("[acceptance] Running S9: duplicate high-value does not flush storm ...")
        workspace_s9 = Path(tempfile.mkdtemp(prefix="mp_accept_s9_"))
        try:
            s9 = _run_mcp_stdio_duplicate_high_value_no_storm_scenario(workspace_s9)
            results.append(s9)
            print(f"  -> {s9['pass_fail']} ({s9['duration_ms']}ms)")
        except Exception as exc:
            results.append({
                "scenario": "S9", "name": "Duplicate High-Value Does Not Flush Storm (Profile B)",
                "pass_fail": "FAIL", "actual": str(exc), "duration_ms": 0,
                "setup": "", "action": "", "expected": "", "artifact_path": "", "notes": [],
            })

    # ---- Cleanup temp dirs ----
    cleanup_workspaces = [workspace_b, workspace_s3, workspace_s6]
    if INCLUDE_HIGH_VALUE_RUNTIME_SCENARIOS:
        cleanup_workspaces.extend([workspace_s8, workspace_s9])
    for ws in cleanup_workspaces:
        try:
            shutil.rmtree(ws, ignore_errors=True)
        except Exception:
            pass
    if not skip_profile_c:
        for ws_name in ("workspace_s4", "workspace_s5"):
            ws = locals().get(ws_name)
            if ws:
                try:
                    shutil.rmtree(ws, ignore_errors=True)
                except Exception:
                    pass

    # ---- Generate reports ----
    generate_json_report(results, json_report_path)
    generate_md_report(results, md_report_path)

    total = len(results)
    passed = sum(1 for r in results if r["pass_fail"] == "PASS")
    skipped = sum(1 for r in results if r["pass_fail"] == "SKIP")
    failed = total - passed - skipped

    print()
    print("=" * 60)
    print(f"  Replacement Acceptance E2E: {passed}/{total} PASS"
          f"{f', {skipped} SKIP' if skipped else ''}"
          f"{f', {failed} FAIL' if failed else ''}")
    print(f"  JSON report: {json_report_path}")
    print(f"  MD report:   {md_report_path}")
    print("=" * 60)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
