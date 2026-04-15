from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from fastapi.testclient import TestClient

import main as backend_main


class _FakeInstaller:
    def __init__(self, env_file: str = "/tmp/runtime.env") -> None:
        self._env_file = env_file
        self.last_bootstrap_status_kwargs: dict[str, object] | None = None
        self.last_perform_setup_kwargs: dict[str, object] | None = None
        self.last_provider_probe_kwargs: dict[str, object] | None = None

    def bootstrap_status(self, **kwargs):
        self.last_bootstrap_status_kwargs = dict(kwargs)
        return {
            "ok": True,
            "summary": "ready",
            "setup": {
                "requiresOnboarding": False,
                "restartRequired": True,
                "envFile": self._env_file,
                "configPath": "/tmp/openclaw.json",
                "mode": "basic",
                "requestedProfile": "b",
                "effectiveProfile": "b",
                "transport": "stdio",
                "mcpApiKeyConfigured": True,
                "embeddingConfigured": False,
                "rerankerConfigured": False,
                "llmConfigured": False,
                "frontendAvailable": True,
                "warnings": [],
            },
            "checks": [
                {
                    "id": "bundled-skill",
                    "status": "PASS",
                    "message": "Plugin-bundled OpenClaw skill is present.",
                }
            ],
        }

    def load_env_file(self, _path):
        return {
            "DATABASE_URL": "sqlite+aiosqlite:////tmp/bootstrap-restarted.db",
            "MCP_API_KEY": "bootstrap-secret",
        }

    def perform_setup(self, **kwargs):
        self.last_perform_setup_kwargs = dict(kwargs)
        return {
            "ok": True,
            "summary": "setup ok",
            "env_file": self._env_file,
            "effective_profile": "b",
            "fallback_applied": False,
            "restart_required": True,
            "warnings": [],
            "actions": [],
            "next_steps": [],
            "setup": self.bootstrap_status()["setup"],
        }

    def preview_provider_probe_status(self, **kwargs):
        self.last_provider_probe_kwargs = dict(kwargs)
        return {
            "requestedProfile": kwargs.get("profile"),
            "effectiveProfile": kwargs.get("profile"),
            "probedProfile": kwargs.get("profile"),
            "requiresProviders": kwargs.get("profile") in {"c", "d"},
            "fallbackApplied": False,
            "summaryStatus": "pass",
            "summaryMessage": "Advanced provider checks passed for the current profile.",
            "missingFields": [],
            "providers": {
                "embedding": {
                    "configured": True,
                    "status": "pass",
                    "detail": "Probe passed.",
                    "baseUrl": kwargs.get("embedding_api_base"),
                    "model": kwargs.get("embedding_model"),
                    "missingFields": [],
                    "detectedDim": kwargs.get("embedding_dim"),
                }
            },
        }


@contextmanager
def _build_client(
    monkeypatch,
    tmp_path,
    *,
    client=("127.0.0.1", 50000),
    base_url="http://127.0.0.1",
    configured_api_key: str | None = None,
):
    asyncio.run(backend_main.runtime_state.shutdown())
    asyncio.run(backend_main.close_sqlite_client())
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:////{(tmp_path / 'bootstrap.db').as_posix().lstrip('/')}")
    if configured_api_key:
        monkeypatch.setenv("MCP_API_KEY", configured_api_key)
    else:
        monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)
    env_file = tmp_path / "runtime.env"
    env_file.write_text("MCP_API_KEY=bootstrap-secret\nDATABASE_URL=sqlite+aiosqlite:////tmp/bootstrap-restarted.db\n", encoding="utf-8")
    monkeypatch.setattr(backend_main, "_resolve_bootstrap_installer", lambda: _FakeInstaller(str(env_file)))
    try:
        with TestClient(backend_main.app, client=client, base_url=base_url) as test_client:
            yield test_client
    finally:
        asyncio.run(backend_main.runtime_state.shutdown())
        asyncio.run(backend_main.close_sqlite_client())


def test_bootstrap_status_marks_restart_supported_for_loopback(monkeypatch, tmp_path) -> None:
    with _build_client(monkeypatch, tmp_path) as client:
        response = client.get("/bootstrap/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["setup"]["restartSupported"] is True
    assert payload["setup"]["restartRequired"] is True
    assert payload["checks"][0]["id"] == "bundled-skill"


def test_bootstrap_status_prefers_isolated_runtime_context_from_env(monkeypatch, tmp_path) -> None:
    fake_installer = _FakeInstaller(str(tmp_path / "isolated.env"))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "isolated-openclaw.json"))
    monkeypatch.setenv("OPENCLAW_MEMORY_PALACE_ENV_FILE", str(tmp_path / "isolated.env"))
    monkeypatch.setenv("OPENCLAW_MEMORY_PALACE_SETUP_ROOT", str(tmp_path / "isolated-root"))

    with _build_client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(backend_main, "_resolve_bootstrap_installer", lambda: fake_installer)
        response = client.get("/bootstrap/status")

    assert response.status_code == 200
    assert fake_installer.last_bootstrap_status_kwargs == {
        "config": str(tmp_path / "isolated-openclaw.json"),
        "setup_root_value": str(tmp_path / "isolated-root"),
        "env_file_value": str(tmp_path / "isolated.env"),
    }


def test_bootstrap_status_rejects_non_loopback_without_api_key(monkeypatch, tmp_path) -> None:
    with _build_client(
        monkeypatch,
        tmp_path,
        client=("203.0.113.10", 50000),
        base_url="http://203.0.113.10",
    ) as client:
        response = client.get("/bootstrap/status")

    assert response.status_code == 403
    detail = response.json().get("detail") or {}
    assert detail.get("error") == "bootstrap_access_denied"


def test_bootstrap_status_rejects_non_loopback_even_with_configured_api_key(
    monkeypatch, tmp_path
) -> None:
    with _build_client(
        monkeypatch,
        tmp_path,
        client=("203.0.113.10", 50000),
        base_url="http://203.0.113.10",
        configured_api_key="bootstrap-secret",
    ) as client:
        response = client.get(
            "/bootstrap/status",
            headers={"X-MCP-API-Key": "bootstrap-secret"},
        )

    assert response.status_code == 403
    detail = response.json().get("detail") or {}
    assert detail.get("error") == "bootstrap_access_denied"


def test_bootstrap_restart_schedules_background_restart(monkeypatch, tmp_path) -> None:
    calls: list[dict[str, object]] = []

    def _fake_restart_local_backend_background(*, launch_command, launch_env, launch_cwd):
        calls.append(
            {
                "launch_command": launch_command,
                "launch_env": dict(launch_env),
                "launch_cwd": launch_cwd,
            }
        )

    monkeypatch.setattr(
        backend_main,
        "_restart_local_backend_background",
        _fake_restart_local_backend_background,
    )

    with _build_client(monkeypatch, tmp_path) as client:
        response = client.post("/bootstrap/restart")

    assert response.status_code == 200
    payload = response.json()
    assert payload["restartAccepted"] is True
    assert payload["restartSupported"] is True
    assert calls
    assert calls[0]["launch_command"][0] == backend_main.sys.executable
    assert calls[0]["launch_env"]["MCP_API_KEY"] == "bootstrap-secret"


def test_build_restart_env_clears_stale_provider_values(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / "runtime.env"
    env_file.write_text(
        "MCP_API_KEY=bootstrap-secret\nDATABASE_URL=sqlite+aiosqlite:////tmp/bootstrap-restarted.db\n",
        encoding="utf-8",
    )
    installer = _FakeInstaller(str(env_file))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "stale-openclaw.json"))
    monkeypatch.setenv("OPENCLAW_MEMORY_PALACE_ENV_FILE", str(tmp_path / "stale.env"))
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://stale.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "stale-reranker")

    restart_env, resolved_env_file = backend_main._build_restart_env(installer)

    assert resolved_env_file == str(env_file)
    assert restart_env["OPENCLAW_CONFIG_PATH"] == "/tmp/openclaw.json"
    assert restart_env["OPENCLAW_MEMORY_PALACE_ENV_FILE"] == str(env_file)
    assert restart_env["MCP_API_KEY"] == "bootstrap-secret"
    assert "RETRIEVAL_EMBEDDING_API_BASE" not in restart_env
    assert "RETRIEVAL_RERANKER_MODEL" not in restart_env


def test_bootstrap_restart_returns_429_within_cooldown(monkeypatch, tmp_path) -> None:
    """Second restart within the cooldown window must return 429."""
    # Reset global cooldown state from any prior test.
    backend_main._last_restart_ts = 0.0

    def _fake_restart_local_backend_background(*, launch_command, launch_env, launch_cwd):
        pass

    monkeypatch.setattr(
        backend_main,
        "_restart_local_backend_background",
        _fake_restart_local_backend_background,
    )

    with _build_client(monkeypatch, tmp_path) as client:
        first = client.post("/bootstrap/restart")
        assert first.status_code == 200

        second = client.post("/bootstrap/restart")
        assert second.status_code == 429
        detail = second.json().get("detail") or {}
        assert detail.get("error") == "bootstrap_restart_cooldown"
        assert "retry_after_seconds" in detail

    # Reset global cooldown state for other tests.
    backend_main._last_restart_ts = 0.0


def test_terminate_self_for_restart_prefers_ctrl_break_on_windows(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(backend_main.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(backend_main.signal, "CTRL_BREAK_EVENT", 1, raising=False)
    monkeypatch.setattr(backend_main.os, "getpid", lambda: 4321)
    monkeypatch.setattr(backend_main.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    backend_main._terminate_self_for_restart()

    assert calls == [(4321, 1)]


def test_terminate_self_for_restart_falls_back_to_sigterm_when_ctrl_break_fails(
    monkeypatch,
) -> None:
    calls: list[tuple[int, int]] = []
    ctrl_break = getattr(backend_main.signal, "CTRL_BREAK_EVENT", 1)
    monkeypatch.setattr(backend_main.sys, "platform", "win32", raising=False)
    monkeypatch.setattr(backend_main.signal, "CTRL_BREAK_EVENT", ctrl_break, raising=False)
    monkeypatch.setattr(backend_main.os, "getpid", lambda: 4321)

    def _fake_kill(pid: int, sig: int) -> None:
        calls.append((pid, sig))
        if sig == ctrl_break:
            raise OSError("ctrl-break unsupported")

    monkeypatch.setattr(backend_main.os, "kill", _fake_kill)

    backend_main._terminate_self_for_restart()

    assert calls == [(4321, ctrl_break), (4321, backend_main.signal.SIGTERM)]


def test_build_restart_supervisor_env_includes_timeout_and_log_path(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("BOOTSTRAP_RESTART_WAIT_TIMEOUT_SEC", "45")
    monkeypatch.setenv("BOOTSTRAP_RESTART_POLL_INTERVAL_SEC", "0.5")

    launch_cwd = str(tmp_path / "backend")
    helper_env = backend_main._build_restart_supervisor_env(
        launch_command=[
            backend_main.sys.executable,
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "18084",
        ],
        launch_env={"MCP_API_KEY": "bootstrap-secret"},
        launch_cwd=launch_cwd,
    )

    assert helper_env["MEMORY_PALACE_RESTART_HOST"] == "127.0.0.1"
    assert helper_env["MEMORY_PALACE_RESTART_PORT"] == "18084"
    assert helper_env["MEMORY_PALACE_RESTART_WAIT_TIMEOUT_SEC"] == "45.0"
    assert helper_env["MEMORY_PALACE_RESTART_POLL_INTERVAL_SEC"] == "0.5"
    assert helper_env["MEMORY_PALACE_RESTART_LOG_PATH"].endswith(
        ".tmp/bootstrap-restart-supervisor.log"
    )


def test_build_restart_supervisor_env_uses_safe_defaults_for_invalid_env_values(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("BOOTSTRAP_RESTART_WAIT_TIMEOUT_SEC", "invalid")
    monkeypatch.setenv("BOOTSTRAP_RESTART_POLL_INTERVAL_SEC", "0")

    helper_env = backend_main._build_restart_supervisor_env(
        launch_command=[backend_main.sys.executable],
        launch_env={},
        launch_cwd=str(tmp_path / "backend"),
    )

    assert helper_env["MEMORY_PALACE_RESTART_HOST"] == "127.0.0.1"
    assert helper_env["MEMORY_PALACE_RESTART_PORT"] == "8000"
    assert helper_env["MEMORY_PALACE_RESTART_WAIT_TIMEOUT_SEC"] == "30.0"
    assert helper_env["MEMORY_PALACE_RESTART_POLL_INTERVAL_SEC"] == "0.05"


def test_resolve_backend_bind_host_defaults_to_loopback(monkeypatch) -> None:
    monkeypatch.delenv("HOST", raising=False)
    assert backend_main._resolve_backend_bind_host() == "127.0.0.1"


def test_resolve_backend_bind_port_defaults_to_8000_for_invalid_values(monkeypatch) -> None:
    monkeypatch.setenv("PORT", "invalid")
    assert backend_main._resolve_backend_bind_port() == 8000


def test_bootstrap_apply_returns_maintenance_key_for_current_session(monkeypatch, tmp_path) -> None:
    with _build_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/bootstrap/apply",
            json={"mode": "basic", "profile": "b", "transport": "stdio"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["maintenanceApiKey"] == "boot************"  # 16 chars: 4 visible + 12 masked
    assert payload["maintenanceApiKeySet"] is True


def test_bootstrap_apply_prefers_isolated_runtime_context_from_env(monkeypatch, tmp_path) -> None:
    fake_installer = _FakeInstaller(str(tmp_path / "isolated.env"))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "isolated-openclaw.json"))
    monkeypatch.setenv("OPENCLAW_MEMORY_PALACE_ENV_FILE", str(tmp_path / "isolated.env"))
    monkeypatch.setenv("OPENCLAW_MEMORY_PALACE_SETUP_ROOT", str(tmp_path / "isolated-root"))

    with _build_client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(backend_main, "_resolve_bootstrap_installer", lambda: fake_installer)
        response = client.post(
            "/bootstrap/apply",
            json={"mode": "basic", "profile": "b", "transport": "stdio"},
        )

    assert response.status_code == 200
    assert fake_installer.last_perform_setup_kwargs is not None
    assert fake_installer.last_perform_setup_kwargs["config"] == str(tmp_path / "isolated-openclaw.json")
    assert fake_installer.last_perform_setup_kwargs["setup_root_value"] == str(tmp_path / "isolated-root")
    assert fake_installer.last_perform_setup_kwargs["env_file_value"] == str(tmp_path / "isolated.env")


def test_bootstrap_apply_masks_short_api_keys(monkeypatch, tmp_path) -> None:
    class _ShortKeyInstaller(_FakeInstaller):
        def load_env_file(self, _path):
            return {"MCP_API_KEY": "abc"}

    with _build_client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(backend_main, "_resolve_bootstrap_installer", lambda: _ShortKeyInstaller())
        response = client.post(
            "/bootstrap/apply",
            json={"mode": "basic", "profile": "b", "transport": "stdio"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["maintenanceApiKey"] == "***"  # <=8 chars: fully masked
    assert payload["maintenanceApiKeySet"] is True


def test_bootstrap_apply_returns_null_key_when_not_configured(monkeypatch, tmp_path) -> None:
    class _NoKeyInstaller(_FakeInstaller):
        def load_env_file(self, _path):
            return {}

    with _build_client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(backend_main, "_resolve_bootstrap_installer", lambda: _NoKeyInstaller())
        response = client.post(
            "/bootstrap/apply",
            json={"mode": "basic", "profile": "b", "transport": "stdio"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["maintenanceApiKey"] is None
    assert payload["maintenanceApiKeySet"] is False


def test_bootstrap_apply_can_include_validation_chain(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        backend_main,
        "_run_post_setup_validation",
        lambda *_args, **_kwargs: {
            "ok": True,
            "failed_step": None,
            "steps": [
                {"name": "verify", "ok": True, "exit_code": 0, "summary": "verify passed"},
                {"name": "doctor", "ok": True, "exit_code": 0, "summary": "doctor warn"},
                {"name": "smoke", "ok": True, "exit_code": 0, "summary": "smoke warn"},
            ],
        },
    )

    with _build_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/bootstrap/apply",
            json={"mode": "basic", "profile": "b", "transport": "stdio", "validate": True},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["validation"]["ok"] is True
    assert [step["name"] for step in payload["validation"]["steps"]] == ["verify", "doctor", "smoke"]


def test_bootstrap_apply_uses_async_validation_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        backend_main,
        "_run_post_setup_validation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("sync validation path should not run inside endpoint")
        ),
    )

    async def _fake_validation(*_args, **_kwargs):
        return {
            "ok": True,
            "failed_step": None,
            "steps": [{"name": "verify", "ok": True, "exit_code": 0, "summary": "ok"}],
        }

    monkeypatch.setattr(
        backend_main,
        "_run_post_setup_validation_async",
        _fake_validation,
    )

    with _build_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/bootstrap/apply",
            json={"mode": "basic", "profile": "b", "transport": "stdio", "validate": True},
        )

    assert response.status_code == 200
    assert response.json()["validation"]["ok"] is True


@pytest.mark.asyncio
async def test_run_post_setup_validation_async_offloads_blocking_work(
    monkeypatch, tmp_path
) -> None:
    installer = _FakeInstaller(str(tmp_path / "runtime.env"))
    main_thread_id = threading.get_ident()
    run_thread_ids: list[int] = []
    tick_count = 0
    stop_ticks = False

    def _fake_parse_jsonish_stdout(raw: str):
        return {"ok": True, "summary": raw}

    def _fake_subprocess_run(*_args, **_kwargs):
        run_thread_ids.append(threading.get_ident())
        time.sleep(0.02)
        return SimpleNamespace(returncode=0, stdout="step ok", stderr="")

    async def _ticker() -> None:
        nonlocal tick_count, stop_ticks
        while not stop_ticks:
            tick_count += 1
            await asyncio.sleep(0)

    monkeypatch.setattr(
        installer,
        "parse_jsonish_stdout",
        _fake_parse_jsonish_stdout,
        raising=False,
    )
    monkeypatch.setattr(backend_main.subprocess, "run", _fake_subprocess_run)

    ticker_task = asyncio.create_task(_ticker())
    try:
        result = await backend_main._run_post_setup_validation_async(
            installer,
            config_path=tmp_path / "openclaw.json",
        )
    finally:
        stop_ticks = True
        await ticker_task

    assert result["ok"] is True
    assert len(result["steps"]) == 3
    assert run_thread_ids
    assert all(thread_id != main_thread_id for thread_id in run_thread_ids)
    assert tick_count > 5


def test_run_post_setup_validation_redacts_non_json_output(monkeypatch, tmp_path) -> None:
    installer = _FakeInstaller(str(tmp_path / "runtime.env"))

    def _raise_parse_error(_raw: str):
        raise ValueError("not json")

    def _fake_subprocess_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="Authorization: Bearer super-secret",
            stderr="MCP_API_KEY=hidden-value",
        )

    monkeypatch.setattr(
        installer,
        "parse_jsonish_stdout",
        _raise_parse_error,
        raising=False,
    )
    monkeypatch.setattr(backend_main.subprocess, "run", _fake_subprocess_run)

    result = backend_main._run_post_setup_validation(
        installer,
        config_path=tmp_path / "openclaw.json",
    )

    assert result["ok"] is False
    assert result["failed_step"] == "verify"
    assert result["steps"][0]["summary"] == (
        "openclaw command produced no JSON payload. See server logs for details."
    )


def test_bootstrap_provider_probe_returns_preview_status(monkeypatch, tmp_path) -> None:
    with _build_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/bootstrap/provider-probe",
            json={
                "mode": "full",
                "profile": "c",
                "transport": "sse",
                "embeddingApiBase": "https://embedding.example/v1",
                "embeddingModel": "embed-large",
                "embeddingDim": "1024",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["providerProbe"]["requestedProfile"] == "c"
    assert payload["providerProbe"]["providers"]["embedding"]["baseUrl"] == "https://embedding.example/v1"
    assert payload["providerProbe"]["providers"]["embedding"]["detectedDim"] == "1024"


def test_bootstrap_provider_probe_top_level_ok_tracks_summary_status(monkeypatch, tmp_path) -> None:
    fake_installer = _FakeInstaller(str(tmp_path / "runtime.env"))

    def _warn_probe(**kwargs):
        payload = _FakeInstaller.preview_provider_probe_status(fake_installer, **kwargs)
        payload["summaryStatus"] = "warn"
        payload["summaryMessage"] = "Provider probe is incomplete."
        return payload

    with _build_client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(fake_installer, "preview_provider_probe_status", _warn_probe)
        monkeypatch.setattr(backend_main, "_resolve_bootstrap_installer", lambda: fake_installer)
        response = client.post(
            "/bootstrap/provider-probe",
            json={"mode": "basic", "profile": "c", "transport": "stdio"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["providerProbe"]["summaryStatus"] == "warn"


def test_bootstrap_provider_probe_prefers_isolated_runtime_context_from_env(monkeypatch, tmp_path) -> None:
    fake_installer = _FakeInstaller(str(tmp_path / "isolated.env"))
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "isolated-openclaw.json"))
    monkeypatch.setenv("OPENCLAW_MEMORY_PALACE_ENV_FILE", str(tmp_path / "isolated.env"))
    monkeypatch.setenv("OPENCLAW_MEMORY_PALACE_SETUP_ROOT", str(tmp_path / "isolated-root"))

    with _build_client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(backend_main, "_resolve_bootstrap_installer", lambda: fake_installer)
        response = client.post(
            "/bootstrap/provider-probe",
            json={"mode": "basic", "profile": "c", "transport": "stdio"},
        )

    assert response.status_code == 200
    assert fake_installer.last_provider_probe_kwargs is not None
    assert fake_installer.last_provider_probe_kwargs["config"] == str(tmp_path / "isolated-openclaw.json")
    assert fake_installer.last_provider_probe_kwargs["setup_root_value"] == str(tmp_path / "isolated-root")
    assert fake_installer.last_provider_probe_kwargs["env_file_value"] == str(tmp_path / "isolated.env")


def test_bootstrap_provider_probe_forwards_sse_runtime_flags(monkeypatch, tmp_path) -> None:
    fake_installer = _FakeInstaller(str(tmp_path / "runtime.env"))

    with _build_client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(backend_main, "_resolve_bootstrap_installer", lambda: fake_installer)
        response = client.post(
            "/bootstrap/provider-probe",
            json={
                "mode": "basic",
                "profile": "c",
                "transport": "sse",
                "sseUrl": "https://memory.example/sse",
                "mcpApiKey": "probe-secret",
                "allowInsecureLocal": True,
            },
        )

    assert response.status_code == 200
    assert fake_installer.last_provider_probe_kwargs is not None
    assert fake_installer.last_provider_probe_kwargs["transport"] == "sse"
    assert fake_installer.last_provider_probe_kwargs["sse_url"] == "https://memory.example/sse"
    assert fake_installer.last_provider_probe_kwargs["mcp_api_key"] == "probe-secret"
    assert fake_installer.last_provider_probe_kwargs["allow_insecure_local"] is True


def test_run_post_setup_validation_passes_timeout_to_openclaw_subprocesses(
    monkeypatch,
    tmp_path,
) -> None:
    installer = _FakeInstaller(str(tmp_path / "runtime.env"))
    seen_timeouts: list[object] = []

    def _fake_parse_jsonish_stdout(raw: str):
        return {"ok": True, "summary": raw}

    def _fake_subprocess_run(*_args, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        return SimpleNamespace(returncode=0, stdout="step ok", stderr="")

    monkeypatch.setattr(
        installer,
        "parse_jsonish_stdout",
        _fake_parse_jsonish_stdout,
        raising=False,
    )
    monkeypatch.setattr(backend_main.subprocess, "run", _fake_subprocess_run)

    result = backend_main._run_post_setup_validation(
        installer,
        config_path=tmp_path / "openclaw.json",
    )

    assert result["ok"] is True
    assert len(seen_timeouts) == 3
    assert all(isinstance(timeout, (int, float)) and timeout > 0 for timeout in seen_timeouts)


def test_bootstrap_apply_requires_api_key_when_configured(monkeypatch, tmp_path) -> None:
    with _build_client(
        monkeypatch,
        tmp_path,
        configured_api_key="bootstrap-secret",
    ) as client:
        response = client.post(
            "/bootstrap/apply",
            json={"mode": "basic", "profile": "b", "transport": "stdio"},
        )

    assert response.status_code == 401
    detail = response.json().get("detail") or {}
    assert detail.get("error") == "bootstrap_auth_failed"
    assert detail.get("reason") == "invalid_or_missing_api_key"


def test_bootstrap_apply_accepts_configured_api_key(monkeypatch, tmp_path) -> None:
    with _build_client(
        monkeypatch,
        tmp_path,
        configured_api_key="bootstrap-secret",
    ) as client:
        response = client.post(
            "/bootstrap/apply",
            headers={"X-MCP-API-Key": "bootstrap-secret"},
            json={"mode": "basic", "profile": "b", "transport": "stdio"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["maintenanceApiKey"] == "boot************"  # 16 chars: 4 visible + 12 masked
    assert payload["maintenanceApiKeySet"] is True


def test_bootstrap_apply_redacts_subprocess_failure_output(monkeypatch, tmp_path) -> None:
    class _BrokenInstaller(_FakeInstaller):
        def perform_setup(self, **_kwargs):
            raise backend_main.subprocess.CalledProcessError(
                returncode=17,
                cmd=["python", "openclaw_memory_palace.py", "setup"],
                stderr="Authorization: Bearer super-secret\nMCP_API_KEY=hidden-value",
            )

    with _build_client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(
            backend_main,
            "_resolve_bootstrap_installer",
            lambda: _BrokenInstaller(),
        )
        response = client.post(
            "/bootstrap/apply",
            json={"mode": "basic", "profile": "b", "transport": "stdio"},
        )

    assert response.status_code == 500
    detail = response.json().get("detail") or {}
    assert detail.get("error") == "bootstrap_apply_failed"
    assert detail.get("message") == "bootstrap installer failed with exit code 17. See server logs for details."


def test_build_local_restart_command_uses_server_scope_instead_of_host_header() -> None:
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/bootstrap/restart",
            "headers": [(b"host", b"127.0.0.1:3000")],
            "scheme": "http",
            "server": ("127.0.0.1", 18084),
            "client": ("127.0.0.1", 50000),
            "query_string": b"",
        }
    )

    command = backend_main._build_local_restart_command(request)

    host_index = command.index("--host")
    port_index = command.index("--port")
    assert command[host_index + 1] == "127.0.0.1"
    assert command[port_index + 1] == "18084"
