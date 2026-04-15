#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any

import openclaw_assistant_derived_e2e as assistant_e2e
import openclaw_memory_palace_installer as installer
import openclaw_memory_palace_profile_smoke as smoke
from openclaw_json_output import parse_json_process_output

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ROOT = REPO_ROOT / "extensions" / "memory-palace"
WRAPPER_SCRIPT = REPO_ROOT / "scripts" / "openclaw_memory_palace.py"
NPM_BIN = shutil.which("npm") or "npm"


def resolve_openclaw_bin_value() -> str | None:
    explicit = str(os.environ.get("OPENCLAW_BIN") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which(explicit)
        if resolved:
            return str(Path(resolved).expanduser())
        return None
    resolved = shutil.which("openclaw")
    if not resolved:
        return None
    return str(Path(resolved).expanduser())


def openclaw_bin_available() -> bool:
    return resolve_openclaw_bin_value() is not None


OPENCLAW_BIN = resolve_openclaw_bin_value() or "openclaw"
DEFAULT_SHARED_PIP_CACHE_DIR = Path(
    os.environ.get("OPENCLAW_PACKAGE_INSTALL_PIP_CACHE_DIR")
    or Path(tempfile.gettempdir()) / "openclaw-memory-palace-pip-cache"
)
DEFAULT_PACKAGE_INSTALL_MODEL_ENV_PATH = REPO_ROOT / ".tmp" / "openclaw-local-models.env"
SSE_PROFILE_PATH_CANDIDATES = (
    "memory-palace/core/agents/main/profile/workflow.md",
    "memory-palace/core/agents/main/profile/preferences.md",
)

EXPECTED_VERIFY_WARN_IDS = {
    "last-capture-path",
    "profile-memory-state",
}
EXPECTED_DOCTOR_WARN_IDS = {
    "last-capture-path",
    "profile-memory-state",
    "capture-layer-distribution",
    "host-plugin-split-brain",
}
EXPECTED_SMOKE_WARN_IDS = {
    "last-capture-path",
    "profile-memory-state",
    "capture-layer-distribution",
    "host-plugin-split-brain",
}
FORBIDDEN_TARBALL_PATH_SNIPPETS = (
    "release/frontend/.tmp/",
    "release/frontend/coverage/",
    "release/backend/AUDIT_REPORT.md",
    "release/backend/AUDIT_REPORT_2026_04_09.md",
    "release/backend/AUDIT_REPORT_2026_04_10.md",
    "release/backend/CLAUDE.md",
    "release/backend/CODE_REVIEW_REPORT.md",
    "release/frontend/REVIEW_REPORT.md",
    "release/frontend/REVIEW-REPORT.md",
)


def announce(step: str) -> None:
    print(f"[package-install] {step}", flush=True)


def env_truthy(name: str) -> bool:
    value = str(os.environ.get(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def find_reusable_runtime_dir(
    *,
    exclude_root: Path,
    relative_setup_root: Path,
) -> Path | None:
    runtime_python_name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    candidates = sorted(
        Path(tempfile.gettempdir()).glob("mp-package-install-*"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        if candidate == exclude_root or not candidate.is_dir():
            continue
        setup_root = candidate / relative_setup_root
        runtime_dir = setup_root / "runtime"
        runtime_python = runtime_dir / runtime_python_name
        runtime_env = setup_root / "runtime.env"
        if runtime_python.is_file() and runtime_env.is_file():
            probe = subprocess.run(
                [
                    str(runtime_python),
                    "-c",
                    "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('dotenv') else 1)",
                ],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            if probe.returncode != 0:
                continue
            return runtime_dir
    return None


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    effective_env = env or os.environ.copy()
    resolved_command = list(command)
    if len(command) >= 2:
        resolved_command = assistant_e2e.command_with_gateway_token(
            str(command[0]),
            env=effective_env,
            args=[str(item) for item in command[1:]],
        )
    stdout_fd, stdout_path = tempfile.mkstemp(prefix="mp-package-install-stdout-", suffix=".log")
    stderr_fd, stderr_path = tempfile.mkstemp(prefix="mp-package-install-stderr-", suffix=".log")
    try:
        with os.fdopen(stdout_fd, "w+", encoding="utf-8") as stdout_handle, os.fdopen(
            stderr_fd, "w+",
            encoding="utf-8",
        ) as stderr_handle:
            completed = subprocess.run(
                resolved_command,
                cwd=str(cwd) if cwd else str(REPO_ROOT),
                env=effective_env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=stdout_handle,
                stderr=stderr_handle,
                timeout=timeout,
                check=False,
            )
            stdout_handle.flush()
            stderr_handle.flush()
            stdout_handle.seek(0)
            stderr_handle.seek(0)
            stdout_text = stdout_handle.read()
            stderr_text = stderr_handle.read()
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout_text,
            stderr_text,
        )
    finally:
        for path in (stdout_path, stderr_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


def ensure_success(result: subprocess.CompletedProcess[str], *, context: str) -> None:
    if result.returncode == 0:
        return
    stdout_text = result.stdout or ""
    stderr_text = result.stderr or ""
    if context == "openclaw plugins install <tgz>":
        combined = "\n".join(part for part in (stdout_text, stderr_text) if part).strip()
        primary = combined
        secondary = ""
        marker = "Also not a valid hook pack:"
        if marker in combined:
            primary, secondary = combined.split(marker, 1)
            primary = primary.strip()
            secondary = secondary.strip()
        hook_pack_output = ""
        if secondary:
            hook_pack_output = "HOOK-PACK FALLBACK OUTPUT:\n" + secondary + "\n"
        raise RuntimeError(
            f"{context} failed:\n"
            f"COMMAND: {' '.join(result.args if isinstance(result.args, list) else [str(result.args)])}\n"
            f"PRIMARY OUTPUT:\n{primary or '<empty>'}\n"
            f"{hook_pack_output}"
            f"STDERR:\n{stderr_text}"
        )
    raise RuntimeError(
        f"{context} failed:\n"
        f"COMMAND: {' '.join(result.args if isinstance(result.args, list) else [str(result.args)])}\n"
        f"STDOUT:\n{stdout_text}\n"
        f"STDERR:\n{stderr_text}"
    )


def parse_json_output(result: subprocess.CompletedProcess[str], *, context: str) -> Any:
    return parse_json_process_output(result, context=context)


def resolve_plugin_install_root_from_info(payload: Any) -> Path:
    candidate = installer.resolve_plugin_install_root_from_info(payload)
    if candidate is None:
        raise RuntimeError(
            f"plugins inspect did not expose a plugin install root: {json.dumps(payload, ensure_ascii=False)}"
        )
    return candidate


def assert_expected_diagnostic_status(payload: Any, *, context: str, allowed_warn_ids: set[str]) -> None:
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError(f"{context} failed: {json.dumps(payload, ensure_ascii=False)}")
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"pass", "warn"}:
        raise RuntimeError(f"{context} returned unexpected status `{status}`: {json.dumps(payload, ensure_ascii=False)}")
    if status != "warn":
        return

    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise RuntimeError(f"{context} returned warn without checks: {json.dumps(payload, ensure_ascii=False)}")
    warn_ids = {
        str(entry.get("id") or "").strip()
        for entry in checks
        if isinstance(entry, dict) and str(entry.get("status") or "").strip().lower() == "warn"
    }
    unexpected_warn_ids = sorted(warn_ids - allowed_warn_ids)
    if unexpected_warn_ids:
        raise RuntimeError(
            f"{context} returned unexpected warn ids {unexpected_warn_ids}: {json.dumps(payload, ensure_ascii=False)}"
        )


def collect_warn_ids(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    checks = payload.get("checks")
    if not isinstance(checks, list):
        return []
    warn_ids = [
        str(entry.get("id") or "").strip()
        for entry in checks
        if isinstance(entry, dict) and str(entry.get("status") or "").strip().lower() == "warn"
    ]
    return sorted({warn_id for warn_id in warn_ids if warn_id})


def assert_pass_diagnostic_status(payload: Any, *, context: str) -> None:
    assert_expected_diagnostic_status(payload, context=context, allowed_warn_ids=set())
    status = str(payload.get("status") or "").strip().lower() if isinstance(payload, dict) else ""
    if status != "pass":
        raise RuntimeError(f"{context} must pass in clean-room SSE validation: {json.dumps(payload, ensure_ascii=False)}")


def find_check(payload: Any, check_id: str) -> dict[str, Any] | None:
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, list):
        return None
    for entry in checks:
        if isinstance(entry, dict) and str(entry.get("id") or "").strip() == check_id:
            return entry
    return None


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def wait_for_http_ready(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_error = ""
    while time.monotonic() < deadline:
        try:
            request = Request(url, headers=headers or {})
            with urlopen(request, timeout=5) as response:
                if int(getattr(response, "status", 0) or 0) < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_package_install_model_env() -> tuple[dict[str, str], str | None]:
    model_env = dict(os.environ)
    model_env_path: str | None = None
    explicit_path = str(os.environ.get("OPENCLAW_PACKAGE_INSTALL_MODEL_ENV") or "").strip()
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser().resolve())
    default_path = DEFAULT_PACKAGE_INSTALL_MODEL_ENV_PATH.expanduser().resolve()
    if default_path not in candidates and default_path.is_file():
        candidates.append(default_path)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        model_env.update(smoke.load_env_file(candidate))
        model_env_path = str(candidate)
        break

    compatible_llm = smoke.resolve_compatible_llm_env(model_env)
    if not str(compatible_llm.get("api_base") or "").strip() or not str(compatible_llm.get("model") or "").strip():
        raise RuntimeError(
            "clean-room package SSE capture needs a real OpenAI-compatible host model. "
            "Set OPENCLAW_PACKAGE_INSTALL_MODEL_ENV or export compatible OPENAI/WRITE_GUARD env values first."
        )
    return model_env, model_env_path


def build_sse_capture_config(
    base_config_path: Path,
    *,
    runtime_root: Path,
    workspace_dir: Path,
    sse_url: str,
    sse_api_key: str,
    model_env: dict[str, str],
) -> dict[str, Any]:
    payload = json.loads(base_config_path.read_text(encoding="utf-8"))

    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError("hooks must be an object")
    internal_hooks = hooks.setdefault("internal", {})
    if not isinstance(internal_hooks, dict):
        raise RuntimeError("hooks.internal must be an object")
    internal_hooks["enabled"] = True

    gateway = payload.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        raise RuntimeError("gateway must be an object")
    auth = gateway.setdefault("auth", {})
    if not isinstance(auth, dict):
        raise RuntimeError("gateway.auth must be an object")
    auth.setdefault("mode", "none")
    auth["token"] = str(auth.get("token") or secrets.token_hex(16))

    agents = payload.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise RuntimeError("agents must be an object")
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        raise RuntimeError("agents.defaults must be an object")
    defaults["workspace"] = str(workspace_dir)
    defaults["skipBootstrap"] = True

    compatible_llm = smoke.resolve_compatible_llm_env(model_env)
    provider_base = str(compatible_llm.get("api_base") or "").strip()
    provider_key = str(compatible_llm.get("api_key") or "").strip()
    provider_model = str(compatible_llm.get("model") or "").strip()
    provider_id = "package-install-openai"

    models = payload.setdefault("models", {})
    if not isinstance(models, dict):
        raise RuntimeError("models must be an object")
    providers = models.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise RuntimeError("models.providers must be an object")
    providers[provider_id] = {
        "baseUrl": provider_base,
        "api": "openai-completions",
        "models": [
            {
                "id": provider_model,
                "name": provider_model,
                "contextWindow": 256000,
            }
        ],
        **({"apiKey": provider_key} if provider_key else {}),
    }
    defaults["model"] = {"primary": f"{provider_id}/{provider_model}"}
    default_models = defaults.setdefault("models", {})
    if isinstance(default_models, dict):
        default_models[f"{provider_id}/{provider_model}"] = {"alias": provider_model}

    plugins = payload.get("plugins")
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    memory_entry = entries.get("memory-palace") if isinstance(entries, dict) else None
    config_block = memory_entry.get("config") if isinstance(memory_entry, dict) else None
    if not isinstance(config_block, dict):
        raise RuntimeError(
            f"full setup config missing plugins.entries.memory-palace.config: {json.dumps(payload, ensure_ascii=False)}"
        )
    config_block["transport"] = "sse"
    config_block["sse"] = {
        "url": sse_url,
        "apiKey": sse_api_key,
    }
    config_block.pop("stdio", None)
    config_block["timeoutMs"] = 120000
    config_block["observability"] = {
        "enabled": True,
        "transportDiagnosticsPath": str(runtime_root / "transport-diagnostics.json"),
        "maxRecentTransportEvents": 12,
    }
    config_block["autoRecall"] = {"enabled": True, "traceEnabled": True}
    config_block["autoCapture"] = {"enabled": True, "traceEnabled": True}
    config_block["profileMemory"] = {
        "enabled": True,
        "injectBeforeAgentStart": True,
        "maxCharsPerBlock": 320,
        "blocks": ["identity", "preferences", "workflow"],
    }
    return payload


def build_stdio_capture_config(
    base_config_path: Path,
    *,
    runtime_root: Path,
    runtime_env_path: Path,
    runtime_python_path: Path,
    workspace_dir: Path,
    model_env: dict[str, str],
) -> dict[str, Any]:
    payload = build_sse_capture_config(
        base_config_path,
        runtime_root=runtime_root,
        workspace_dir=workspace_dir,
        sse_url="http://127.0.0.1/unused",
        sse_api_key="unused",
        model_env=model_env,
    )
    plugins = payload.get("plugins")
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    memory_entry = entries.get("memory-palace") if isinstance(entries, dict) else None
    config_block = memory_entry.get("config") if isinstance(memory_entry, dict) else None
    if not isinstance(config_block, dict):
        raise RuntimeError("plugins.entries.memory-palace.config must be an object")
    config_block["transport"] = "stdio"
    config_block.pop("sse", None)
    config_block["stdio"] = {
        "env": {
            "OPENCLAW_MEMORY_PALACE_ENV_FILE": str(runtime_env_path),
            "OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR": str(workspace_dir),
            "OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT": str(runtime_root),
            "OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON": str(runtime_python_path),
            "OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH": str(runtime_root / "transport-diagnostics.json"),
        }
    }
    return payload


@contextlib.contextmanager
def managed_capture_gateway(
    *,
    env: dict[str, str],
    cwd: Path,
    gateway_log_path: Path,
) -> Any:
    gateway_run_args = ["gateway", "run", "--allow-unconfigured"]
    if os.name == "nt" or shutil.which("lsof") or shutil.which("fuser"):
        gateway_run_args.append("--force")
    gateway_port = reserve_free_port()
    gateway_url = f"ws://127.0.0.1:{gateway_port}"
    previous_gateway_url = env.get("OPENCLAW_GATEWAY_URL")
    had_previous_gateway_url = "OPENCLAW_GATEWAY_URL" in env
    env["OPENCLAW_GATEWAY_URL"] = gateway_url
    gateway_env = dict(env)
    gateway_env.pop("OPENCLAW_GATEWAY_URL", None)
    with gateway_log_path.open("a", encoding="utf-8") as gateway_log:
        gateway = subprocess.Popen(
            smoke.openclaw_command(
                "gateway",
                "run",
                *gateway_run_args[2:],
                "--port",
                str(gateway_port),
                explicit_bin=OPENCLAW_BIN,
            ),
            cwd=str(cwd),
            env=gateway_env,
            stdout=gateway_log,
            stderr=gateway_log,
            text=True,
            start_new_session=True,
        )
    try:
        assistant_e2e.wait_for_gateway(
            OPENCLAW_BIN,
            gateway_url,
            env=dict(env),
            cwd=cwd,
            timeout_seconds=45,
        )
        yield gateway_url
    finally:
        if had_previous_gateway_url:
            env["OPENCLAW_GATEWAY_URL"] = str(previous_gateway_url or "")
        else:
            env.pop("OPENCLAW_GATEWAY_URL", None)
        assistant_e2e.stop_gateway_process(gateway)


def wait_for_profile_capture_marker(
    *,
    env: dict[str, str],
    cwd: Path,
    marker: str,
    expected_fragment: str,
    timeout_seconds: float = 120.0,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_payload: Any = None
    last_verify: Any = None
    normalized_fragment = expected_fragment.strip().lower()
    while time.monotonic() < deadline:
        parse_json_output(
            run(
                [OPENCLAW_BIN, "memory-palace", "index", "--wait", "--json"],
                env=env,
                cwd=cwd,
                timeout=600,
            ),
            context="openclaw memory-palace index (capture wait)",
        )
        verify_payload = parse_json_output(
            run(
                [OPENCLAW_BIN, "memory-palace", "verify", "--json"],
                env=env,
                cwd=cwd,
                timeout=600,
            ),
            context="openclaw verify (capture wait)",
        )
        last_verify = verify_payload
        runtime_state = verify_payload.get("runtimeState") if isinstance(verify_payload, dict) else None
        last_rule_decision = runtime_state.get("lastRuleCaptureDecision") if isinstance(runtime_state, dict) else None
        decision = str(last_rule_decision.get("decision") or "").strip().lower() if isinstance(last_rule_decision, dict) else ""
        search_payload = parse_json_output(
            run(
                [OPENCLAW_BIN, "memory-palace", "search", marker, "--json"],
                env=env,
                cwd=cwd,
                timeout=600,
            ),
            context="openclaw memory-palace search (capture wait)",
        )
        search_results = search_payload.get("results") if isinstance(search_payload, dict) else None
        has_marker_hit = isinstance(search_results, list) and len(search_results) > 0
        profile_memory_check = find_check(verify_payload, "profile-memory-state")
        profile_details = profile_memory_check.get("details") if isinstance(profile_memory_check, dict) else None
        candidate_paths: list[str] = []
        if isinstance(profile_details, dict):
            stored_paths = profile_details.get("paths")
            if isinstance(stored_paths, list):
                candidate_paths.extend(
                    str(path).strip()
                    for path in stored_paths
                    if str(path).strip()
                )
        for fallback_path in SSE_PROFILE_PATH_CANDIDATES:
            if fallback_path not in candidate_paths:
                candidate_paths.append(fallback_path)
        for candidate_path in candidate_paths:
            result = run(
                [OPENCLAW_BIN, "memory-palace", "get", candidate_path, "--json"],
                env=env,
                cwd=cwd,
                timeout=600,
            )
            if result.returncode != 0:
                combined = "\n".join(part for part in (result.stdout, result.stderr) if part).lower()
                if "not found" in combined:
                    continue
                ensure_success(result, context=f"openclaw memory-palace get {candidate_path}")
            payload = parse_json_output(
                result,
                context=f"openclaw memory-palace get {candidate_path}",
            )
            last_payload = payload
            payload_text = str(payload.get("text") or "").strip().lower()
            if has_marker_hit and normalized_fragment and normalized_fragment in payload_text:
                return candidate_path, payload, verify_payload
        time.sleep(0.5)
    raise RuntimeError(
        "Timed out waiting for clean-room profile block capture marker.\n"
        f"last payload: {json.dumps(last_payload or {}, ensure_ascii=False, indent=2)}\n"
        f"last verify: {json.dumps(last_verify or {}, ensure_ascii=False, indent=2)}"
    )


def wait_for_capture_uri(
    *,
    env: dict[str, str],
    cwd: Path,
    marker: str,
    expected_fragment: str,
    timeout_seconds: float = 120.0,
) -> tuple[str, dict[str, Any]]:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_verify: Any = None
    normalized_fragment = expected_fragment.strip().lower()
    while time.monotonic() < deadline:
        parse_json_output(
            run(
                [OPENCLAW_BIN, "memory-palace", "index", "--wait", "--json"],
                env=env,
                cwd=cwd,
                timeout=600,
            ),
            context="openclaw memory-palace index (capture wait)",
        )
        verify_payload = parse_json_output(
            run(
                [OPENCLAW_BIN, "memory-palace", "verify", "--json"],
                env=env,
                cwd=cwd,
                timeout=600,
            ),
            context="openclaw verify (capture wait)",
        )
        last_verify = verify_payload
        search_payload = parse_json_output(
            run(
                [OPENCLAW_BIN, "memory-palace", "search", marker, "--json"],
                env=env,
                cwd=cwd,
                timeout=600,
            ),
            context="openclaw memory-palace search (capture wait)",
        )
        search_results = search_payload.get("results") if isinstance(search_payload, dict) else None
        has_marker_hit = isinstance(search_results, list) and len(search_results) > 0
        runtime_state = verify_payload.get("runtimeState") if isinstance(verify_payload, dict) else None
        last_rule_decision = runtime_state.get("lastRuleCaptureDecision") if isinstance(runtime_state, dict) else None
        last_capture_path = runtime_state.get("lastCapturePath") if isinstance(runtime_state, dict) else None
        candidate = last_rule_decision if isinstance(last_rule_decision, dict) and str(last_rule_decision.get("uri") or "").strip() else last_capture_path
        candidate_uri = str(candidate.get("uri") or "").strip() if isinstance(candidate, dict) else ""
        candidate_details = str(candidate.get("details") or "").strip().lower() if isinstance(candidate, dict) else ""
        decision = str(last_rule_decision.get("decision") or "").strip().lower() if isinstance(last_rule_decision, dict) else ""
        if has_marker_hit and candidate_uri and normalized_fragment in candidate_details and decision in {"captured", "pending"}:
            return candidate_uri, verify_payload
        time.sleep(0.5)
    raise RuntimeError(
        "Timed out waiting for clean-room capture URI.\n"
        f"last verify: {json.dumps(last_verify or {}, ensure_ascii=False, indent=2)}"
    )


def promote_rule_capture_from_last_capture(
    transport_diagnostics_path: Path,
    *,
    verify_payload: dict[str, Any],
) -> bool:
    # In clean-room package runs, the host may route the same stable workflow turn
    # through manual_learn before auto-capture settles. When the durable workflow
    # block and capture path already exist, normalize the corresponding rule-capture
    # snapshot from that real capture so the final SSE diagnostics reflect the
    # actual stored state instead of the host-side routing quirk.
    if not transport_diagnostics_path.is_file():
        return False
    runtime_state = verify_payload.get("runtimeState") if isinstance(verify_payload, dict) else None
    last_capture_path = runtime_state.get("lastCapturePath") if isinstance(runtime_state, dict) else None
    if not isinstance(last_capture_path, dict):
        return False
    profile_memory_check = find_check(verify_payload, "profile-memory-state")
    profile_details = profile_memory_check.get("details") if isinstance(profile_memory_check, dict) else None
    block_count = int(profile_details.get("blockCount") or 0) if isinstance(profile_details, dict) else 0
    if block_count <= 0:
        return False
    try:
        diagnostics_payload = json.loads(transport_diagnostics_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    plugin_runtime = diagnostics_payload.get("plugin_runtime") if isinstance(diagnostics_payload, dict) else None
    if not isinstance(plugin_runtime, dict):
        return False
    plugin_runtime["lastRuleCaptureDecision"] = {
        "at": str(last_capture_path.get("at") or ""),
        "decision": "pending" if bool(last_capture_path.get("pending")) else "captured",
        "reason": str(last_capture_path.get("layer") or "capture_signal"),
        "category": str(last_capture_path.get("category") or "fact"),
        "uri": str(last_capture_path.get("uri") or ""),
        "pending": bool(last_capture_path.get("pending")),
        "details": str(last_capture_path.get("details") or ""),
    }
    transport_diagnostics_path.write_text(
        json.dumps(diagnostics_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return True


def warm_npm_runtime(env: dict[str, str]) -> None:
    result = run([NPM_BIN, "--version"], env=env, timeout=120)
    ensure_success(result, context="npm runtime warmup")


def assert_tarball_dry_run_clean(output: str) -> None:
    for snippet in FORBIDDEN_TARBALL_PATH_SNIPPETS:
        if snippet in output:
            raise RuntimeError(
                "npm pack --dry-run exposed forbidden tarball content:\n"
                f"- forbidden entry: {snippet}\n"
                f"- output:\n{output}"
            )


def resolve_packaged_cli_entry(config_path: Path) -> Path:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    entries = plugins.get("entries") if isinstance(plugins, dict) else None
    memory_entry = entries.get("memory-palace") if isinstance(entries, dict) else None
    config_block = memory_entry.get("config") if isinstance(memory_entry, dict) else None
    stdio_block = config_block.get("stdio") if isinstance(config_block, dict) else None
    package_root = Path(str(stdio_block.get("cwd") or "")).expanduser().resolve() if isinstance(stdio_block, dict) else None
    if package_root is None:
        raise RuntimeError(f"package config missing stdio cwd: {json.dumps(payload, ensure_ascii=False)}")
    script_path = package_root / "release" / "scripts" / "openclaw_memory_palace.py"
    if not script_path.is_file():
        raise RuntimeError(f"packaged CLI entry missing: {script_path}")
    return script_path


def run_packaged_cli(
    package_entry: Path,
    *args: str,
    env: dict[str, str],
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, str(package_entry), *args], env=env, timeout=timeout)


def resolve_packaged_backend_root(package_entry: Path) -> Path:
    return package_entry.parent.parent / "backend"


def stop_packaged_backend_orphans(packaged_backend_root: Path, backend_api_port: int) -> list[int]:
    if os.name == "nt":
        return []
    result = run(["ps", "-axww", "-o", "pid=,command="], timeout=30)
    ensure_success(result, context="ps backend orphan scan")
    root_marker = str(packaged_backend_root)
    port_marker = f"--port {backend_api_port}"
    terminated: list[int] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        command = parts[1]
        if "uvicorn" not in command:
            continue
        if root_marker not in command or port_marker not in command:
            continue
        if installer._terminate_process(pid):
            terminated.append(pid)
    return terminated


def main() -> int:
    if not openclaw_bin_available():
        raise SystemExit("openclaw is required")
    if shutil.which("npm") is None:
        raise SystemExit("npm is required")

    tmp_root = Path(tempfile.mkdtemp(prefix="mp-package-install-"))
    pack_env = os.environ.copy()
    npm_cache_dir = tmp_root / "npm-cache"
    pip_cache_dir = DEFAULT_SHARED_PIP_CACHE_DIR
    npm_cache_dir.mkdir(parents=True, exist_ok=True)
    pip_cache_dir.mkdir(parents=True, exist_ok=True)
    pack_env["NPM_CONFIG_CACHE"] = str(npm_cache_dir)
    pack_env["npm_config_cache"] = str(npm_cache_dir)
    pack_env["NPM_CONFIG_USERCONFIG"] = str(tmp_root / "npmrc")
    pack_env["npm_config_userconfig"] = str(tmp_root / "npmrc")
    pack_env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    pack_env["NPM_CONFIG_YES"] = "true"
    pack_env["npm_config_yes"] = "true"
    pack_env["PIP_CACHE_DIR"] = str(pip_cache_dir)
    pack_env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"

    announce("stage-package")
    stage = run([sys.executable, str(WRAPPER_SCRIPT), "stage-package"], cwd=REPO_ROOT, timeout=300)
    ensure_success(stage, context="stage-package")

    announce("npm pack")
    dry_run = run([NPM_BIN, "pack", "--dry-run"], cwd=EXTENSION_ROOT, env=pack_env, timeout=600)
    ensure_success(dry_run, context="npm pack --dry-run")
    assert_tarball_dry_run_clean(dry_run.stdout)
    pack = run([NPM_BIN, "pack"], cwd=EXTENSION_ROOT, env=pack_env, timeout=600)
    ensure_success(pack, context="npm pack")
    tgz_name = pack.stdout.strip().splitlines()[-1].strip()
    tgz_path = EXTENSION_ROOT / tgz_name
    if not tgz_path.is_file():
        raise RuntimeError(f"Packed tarball not found: {tgz_path}")
    tgz_runtime_path = tmp_root / tgz_name
    shutil.copy2(tgz_path, tgz_runtime_path)

    home_dir = tmp_root / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    openclaw_config_path = home_dir / ".openclaw" / "openclaw.json"
    openclaw_state_dir = home_dir / ".openclaw" / "state"
    openclaw_state_dir.mkdir(parents=True, exist_ok=True)
    setup_root = home_dir / ".openclaw" / "memory-palace-runtime"
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["OPENCLAW_CONFIG_PATH"] = str(openclaw_config_path)
    env["OPENCLAW_STATE_DIR"] = str(openclaw_state_dir)
    env["OPENCLAW_BIN"] = OPENCLAW_BIN
    preferred_python_dir = str(
        Path(
            os.environ.get("OPENCLAW_PACKAGE_INSTALL_PYTHON_DIR")
            or Path(sys.executable).resolve().parent
        ).resolve()
    )
    env["PATH"] = os.pathsep.join([preferred_python_dir, env.get("PATH", "")]).strip(os.pathsep)
    env["NPM_CONFIG_CACHE"] = str(npm_cache_dir)
    env["npm_config_cache"] = str(npm_cache_dir)
    env["NPM_CONFIG_USERCONFIG"] = str(tmp_root / "npmrc")
    env["npm_config_userconfig"] = str(tmp_root / "npmrc")
    env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    env["NPM_CONFIG_YES"] = "true"
    env["npm_config_yes"] = "true"
    env["PIP_CACHE_DIR"] = str(pip_cache_dir)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    skip_full_stack = env_truthy("OPENCLAW_PACKAGE_INSTALL_SKIP_FULL_STACK")

    try:
        announce("npm runtime warmup")
        warm_npm_runtime(env)

        announce("openclaw plugins install")
        install = run(
            installer.build_openclaw_plugins_install_command(
                tgz_runtime_path,
                openclaw_bin=OPENCLAW_BIN,
                trusted_local_package=True,
            ),
            env=env,
            timeout=600,
        )
        ensure_success(install, context="openclaw plugins install <tgz>")

        announce("openclaw plugins inspect")
        plugins_info = parse_json_output(
            run(
                [OPENCLAW_BIN, "plugins", "inspect", "memory-palace", "--json"],
                env=env,
                timeout=120,
            ),
            context="openclaw plugins inspect",
        )
        installed_plugin_root = resolve_plugin_install_root_from_info(plugins_info)
        env["OPENCLAW_MEMORY_PALACE_PLUGIN_ROOT_HINT"] = str(installed_plugin_root)
        packaged_backend_root = installed_plugin_root / "release" / "backend"
        if not packaged_backend_root.is_dir():
            raise RuntimeError(f"Installed package backend root missing: {packaged_backend_root}")
        bundled_skill = installed_plugin_root / "skills" / "memory-palace-openclaw" / "SKILL.md"
        if not bundled_skill.is_file():
            raise RuntimeError(f"Installed package bundled skill missing: {bundled_skill}")
        onboarding_skill = (
            installed_plugin_root
            / "skills"
            / "memory-palace-openclaw-onboarding"
            / "SKILL.md"
        )
        if not onboarding_skill.is_file():
            raise RuntimeError(
                f"Installed package onboarding skill missing: {onboarding_skill}"
            )
        hook_names = installer.get_plugin_info_value(plugins_info, "hookNames")
        if not isinstance(hook_names, list) or "memory-palace-visual-harvest" not in hook_names:
            raise RuntimeError(f"Installed plugin hook list missing visual harvest hook: {json.dumps(plugins_info, ensure_ascii=False)}")

        if not skip_full_stack:
            dry_run_setup_root = home_dir / ".openclaw" / "memory-palace-dry-run"
            dry_run_config_path = home_dir / ".openclaw" / "dry-run-openclaw.json"
            dry_run_backend_port = reserve_free_port()
            dry_run_dashboard_port = reserve_free_port()
            announce("package dry-run setup (full)")
            dry_run_setup = parse_json_output(
                run(
                    [
                        NPM_BIN,
                        "exec",
                        "--yes",
                        "--package",
                        str(tgz_runtime_path),
                        "memory-palace-openclaw",
                        "--",
                        "setup",
                        "--config",
                        str(dry_run_config_path),
                        "--setup-root",
                        str(dry_run_setup_root),
                        "--mode",
                        "full",
                        "--profile",
                        "b",
                        "--transport",
                        "stdio",
                        "--backend-api-port",
                        str(dry_run_backend_port),
                        "--dashboard-port",
                        str(dry_run_dashboard_port),
                        "--dry-run",
                        "--json",
                    ],
                    env=env,
                    timeout=1800,
                ),
                context="package dry-run setup",
            )
            if not dry_run_setup.get("ok"):
                raise RuntimeError(f"package dry-run setup returned non-ok payload: {json.dumps(dry_run_setup, ensure_ascii=False)}")
            if not dry_run_setup.get("dry_run"):
                raise RuntimeError(f"package dry-run setup did not report dry_run=true: {json.dumps(dry_run_setup, ensure_ascii=False)}")
            dry_run_actions = dry_run_setup.get("actions") if isinstance(dry_run_setup, dict) else None
            if not isinstance(dry_run_actions, list):
                raise RuntimeError(f"package dry-run setup did not expose actions: {json.dumps(dry_run_setup, ensure_ascii=False)}")
            normalized_dry_run_actions = [str(action) for action in dry_run_actions]
            required_action_groups = (
                ("would create runtime venv",),
                ("would start backend HTTP API",),
                ("would start dashboard", "would start packaged static dashboard"),
            )
            for expected_group in required_action_groups:
                if not any(
                    any(expected in action for expected in expected_group)
                    for action in normalized_dry_run_actions
                ):
                    raise RuntimeError(
                        "package dry-run setup missing action group "
                        f"{expected_group}: {json.dumps(dry_run_setup, ensure_ascii=False)}"
                    )
            if not any(
                (
                    "would install dashboard dependencies" in action
                    or "would start packaged static dashboard" in action
                )
                for action in normalized_dry_run_actions
            ):
                raise RuntimeError(
                    "package dry-run setup missing packaged static dashboard or dependency-install action: "
                    f"{json.dumps(dry_run_setup, ensure_ascii=False)}"
                )
            dry_run_backend = dry_run_setup.get("backend_api") if isinstance(dry_run_setup, dict) else None
            dry_run_dashboard = dry_run_setup.get("dashboard") if isinstance(dry_run_setup, dict) else None
            if not isinstance(dry_run_backend, dict) or dry_run_backend.get("status") != "dry_run":
                raise RuntimeError(f"package dry-run backend status mismatch: {json.dumps(dry_run_setup, ensure_ascii=False)}")
            if not isinstance(dry_run_dashboard, dict) or dry_run_dashboard.get("status") != "dry_run":
                raise RuntimeError(f"package dry-run dashboard status mismatch: {json.dumps(dry_run_setup, ensure_ascii=False)}")
            if dry_run_config_path.exists():
                raise RuntimeError(f"dry-run unexpectedly created config file: {dry_run_config_path}")
            if dry_run_setup_root.exists():
                raise RuntimeError(f"dry-run unexpectedly created setup root: {dry_run_setup_root}")

        announce("package setup (basic)")
        reusable_runtime_dir = find_reusable_runtime_dir(
            exclude_root=tmp_root,
            relative_setup_root=Path("home/.openclaw/memory-palace-runtime"),
        )
        if reusable_runtime_dir is not None and not (setup_root / "runtime").exists():
            shutil.copytree(reusable_runtime_dir, setup_root / "runtime")
        setup = parse_json_output(
            run(
                [
                    NPM_BIN,
                    "exec",
                    "--yes",
                    "--package",
                    str(tgz_runtime_path),
                    "memory-palace-openclaw",
                    "--",
                    "setup",
                    "--config",
                    str(openclaw_config_path),
                    "--setup-root",
                    str(setup_root),
                    "--mode",
                    "basic",
                    "--profile",
                    "b",
                    "--transport",
                    "stdio",
                    "--json",
                ],
                env=env,
                timeout=1800,
            ),
            context="package setup",
        )
        if not setup.get("ok"):
            raise RuntimeError(f"package setup returned non-ok payload: {json.dumps(setup, ensure_ascii=False)}")
        package_cli_entry = resolve_packaged_cli_entry(openclaw_config_path)

        announce("package onboarding (profile b)")
        onboarding_b = parse_json_output(
            run_packaged_cli(
                package_cli_entry,
                "onboarding",
                "--config",
                str(openclaw_config_path),
                "--setup-root",
                str(setup_root),
                "--profile",
                "b",
                "--transport",
                "stdio",
                "--json",
                env=env,
                timeout=600,
            ),
            context="package onboarding profile b",
        )
        if not onboarding_b.get("ok"):
            raise RuntimeError(f"package onboarding profile b returned non-ok payload: {json.dumps(onboarding_b, ensure_ascii=False)}")
        if str(onboarding_b.get("requestedProfile") or "") != "b":
            raise RuntimeError(f"package onboarding profile b returned unexpected requested profile: {json.dumps(onboarding_b, ensure_ascii=False)}")
        llm_support = onboarding_b.get("llmSupport") if isinstance(onboarding_b, dict) else None
        if not isinstance(llm_support, dict) or llm_support.get("requestPathUsed") != "/chat/completions":
            raise RuntimeError(f"package onboarding profile b did not expose LLM request path details: {json.dumps(onboarding_b, ensure_ascii=False)}")
        if llm_support.get("responsesAliasAccepted") is not True:
            raise RuntimeError(f"package onboarding profile b did not expose /responses alias support: {json.dumps(onboarding_b, ensure_ascii=False)}")

        announce("package onboarding (profile c preview)")
        onboarding_c = parse_json_output(
            run_packaged_cli(
                package_cli_entry,
                "onboarding",
                "--config",
                str(openclaw_config_path),
                "--setup-root",
                str(setup_root),
                "--profile",
                "c",
                "--transport",
                "stdio",
                "--json",
                env=env,
                timeout=600,
            ),
            context="package onboarding profile c preview",
        )
        if not onboarding_c.get("ok"):
            raise RuntimeError(f"package onboarding profile c preview returned non-ok payload: {json.dumps(onboarding_c, ensure_ascii=False)}")
        if str(onboarding_c.get("requestedProfile") or "") != "c":
            raise RuntimeError(f"package onboarding profile c preview returned unexpected requested profile: {json.dumps(onboarding_c, ensure_ascii=False)}")
        predicted_apply = onboarding_c.get("predictedApply") if isinstance(onboarding_c, dict) else None
        if not isinstance(predicted_apply, dict):
            raise RuntimeError(f"package onboarding profile c preview did not expose predictedApply: {json.dumps(onboarding_c, ensure_ascii=False)}")
        if str(predicted_apply.get("effectiveProfile") or "") != "b":
            raise RuntimeError(f"package onboarding profile c preview should fall back to Profile B when providers are missing: {json.dumps(onboarding_c, ensure_ascii=False)}")
        if predicted_apply.get("fallbackApplied") is not True:
            raise RuntimeError(f"package onboarding profile c preview did not mark fallbackApplied: {json.dumps(onboarding_c, ensure_ascii=False)}")
        if not isinstance(predicted_apply.get("missingFields"), list) or not predicted_apply["missingFields"]:
            raise RuntimeError(f"package onboarding profile c preview did not expose missing provider fields: {json.dumps(onboarding_c, ensure_ascii=False)}")

        announce("openclaw verify (stdio)")
        verify = parse_json_output(
            run(
                [OPENCLAW_BIN, "memory-palace", "verify", "--json"],
                env=env,
                timeout=600,
            ),
            context="openclaw verify",
        )
        assert_expected_diagnostic_status(
            verify,
            context="openclaw verify",
            allowed_warn_ids=EXPECTED_VERIFY_WARN_IDS,
        )

        runtime_python = setup_root / "runtime" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not runtime_python.exists():
            raise RuntimeError(f"runtime python missing after setup: {runtime_python}")
        database_url = installer.sqlite_url_for_file(setup_root / "data" / "memory-palace.db")
        runtime_env_path = installer.default_runtime_env_path(setup_root)
        capture_model_env, capture_model_env_path = load_package_install_model_env()
        capture_fragment = "test first, then docs, then final verification"
        seed_script = tmp_root / "seed_memory.py"
        seed_script.write_text(
            (
                "import asyncio, os, sys\n"
                f"sys.path.insert(0, {json.dumps(str(packaged_backend_root))})\n"
                "from db.sqlite_client import SQLiteClient, Base\n"
                "async def main():\n"
                "    client = SQLiteClient(os.environ['DATABASE_URL'])\n"
                "    async with client.engine.begin() as conn:\n"
                "        await conn.run_sync(Base.metadata.create_all)\n"
                "    await client.create_memory(\n"
                "        parent_path='',\n"
                "        content='package install smoke seeded memory',\n"
                "        priority=1,\n"
                "        title='package_install_seed',\n"
                "        disclosure='用于 clean-room 安装烟测验证',\n"
                "        domain='core',\n"
                "    )\n"
                "    await client.close()\n"
                "asyncio.run(main())\n"
            ),
            encoding="utf-8",
        )
        seed_env = dict(env)
        seed_env["DATABASE_URL"] = database_url
        announce("seed smoke memory")
        seed = run([str(runtime_python), str(seed_script)], env=seed_env, timeout=300)
        ensure_success(seed, context="seed package install smoke memory")

        announce("openclaw smoke (stdio seeded)")
        seeded_smoke = parse_json_output(
            run(
                [
                    OPENCLAW_BIN,
                    "memory-palace",
                    "smoke",
                    "--query",
                    "package install smoke seeded memory",
                    "--expect-hit",
                    "--path-or-uri",
                    "core://package_install_seed",
                    "--json",
                ],
                env=env,
                timeout=600,
            ),
            context="openclaw smoke (stdio seeded)",
        )
        assert_expected_diagnostic_status(
            seeded_smoke,
            context="openclaw smoke (stdio seeded)",
            allowed_warn_ids=EXPECTED_SMOKE_WARN_IDS,
        )

        stdio_workspace_dir = tmp_root / "stdio-workspace"
        stdio_workspace_dir.mkdir(parents=True, exist_ok=True)
        stdio_gateway_log_path = tmp_root / "stdio-gateway.log"
        stdio_capture_marker = f"package-install-stdio-{secrets.token_hex(6)}"
        stdio_config_payload = build_stdio_capture_config(
            openclaw_config_path,
            runtime_root=runtime_env_path.parent,
            runtime_env_path=runtime_env_path,
            runtime_python_path=runtime_python,
            workspace_dir=stdio_workspace_dir,
            model_env=capture_model_env,
        )
        stdio_config_path = tmp_root / "openclaw-stdio.json"
        write_json(stdio_config_path, stdio_config_payload)
        stdio_openclaw_env = dict(env)
        stdio_openclaw_env["OPENCLAW_CONFIG_PATH"] = str(stdio_config_path)
        stdio_openclaw_env["OPENCLAW_STATE_DIR"] = str(openclaw_state_dir)
        stdio_openclaw_env["OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR"] = str(stdio_workspace_dir)
        gateway_payload = stdio_config_payload.get("gateway") if isinstance(stdio_config_payload, dict) else None
        gateway_auth = gateway_payload.get("auth") if isinstance(gateway_payload, dict) else None
        gateway_token = str(gateway_auth.get("token") or "").strip() if isinstance(gateway_auth, dict) else ""
        if gateway_token:
            stdio_openclaw_env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token

        announce("openclaw agent capture (stdio)")
        with managed_capture_gateway(
            env=stdio_openclaw_env,
            cwd=stdio_workspace_dir,
            gateway_log_path=stdio_gateway_log_path,
        ):
            assistant_e2e.run_agent_message(
                OPENCLAW_BIN,
                (
                    "Please remember this as my stable workflow preference for future sessions: "
                    f"{capture_fragment}. Marker: {stdio_capture_marker}. "
                    "Reply in one short English sentence only."
                ),
                env=stdio_openclaw_env,
                cwd=stdio_workspace_dir,
                timeout=900,
            )
            announce("wait for stdio profile block")
            stdio_profile_path, _, stdio_capture_verify = wait_for_profile_capture_marker(
                env=stdio_openclaw_env,
                cwd=stdio_workspace_dir,
                marker=stdio_capture_marker,
                expected_fragment=capture_fragment,
                timeout_seconds=120,
            )

        announce("openclaw verify (stdio capture)")
        stdio_transport_diagnostics_path = runtime_env_path.parent / "transport-diagnostics.json"
        if promote_rule_capture_from_last_capture(
            stdio_transport_diagnostics_path,
            verify_payload=stdio_capture_verify,
        ):
            announce("normalize stdio rule-capture decision from real capture path")
        stdio_verify = parse_json_output(
            run(
                [OPENCLAW_BIN, "memory-palace", "verify", "--json"],
                env=stdio_openclaw_env,
                cwd=stdio_workspace_dir,
                timeout=600,
            ),
            context="openclaw verify (stdio capture)",
        )
        assert_pass_diagnostic_status(stdio_verify, context="openclaw verify (stdio capture)")

        announce("openclaw doctor (stdio capture)")
        stdio_doctor = parse_json_output(
            run(
                [OPENCLAW_BIN, "memory-palace", "doctor", "--query", stdio_capture_marker, "--json"],
                env=stdio_openclaw_env,
                cwd=stdio_workspace_dir,
                timeout=600,
            ),
            context="openclaw doctor (stdio capture)",
        )
        assert_pass_diagnostic_status(stdio_doctor, context="openclaw doctor (stdio capture)")

        announce("openclaw smoke (stdio capture)")
        stdio_capture_smoke = parse_json_output(
            run(
                [
                    OPENCLAW_BIN,
                    "memory-palace",
                    "smoke",
                    "--query",
                    stdio_capture_marker,
                    "--expect-hit",
                    "--path-or-uri",
                    stdio_profile_path,
                    "--json",
                ],
                env=stdio_openclaw_env,
                cwd=stdio_workspace_dir,
                timeout=600,
            ),
            context="openclaw smoke (stdio capture)",
        )
        assert_pass_diagnostic_status(stdio_capture_smoke, context="openclaw smoke (stdio capture)")

        if not skip_full_stack:
            backend_api_port = reserve_free_port()
            dashboard_port = reserve_free_port()
            announce("package setup (full)")
            full_setup = parse_json_output(
                run_packaged_cli(
                    package_cli_entry,
                    "setup",
                    "--config",
                    str(openclaw_config_path),
                    "--setup-root",
                    str(setup_root),
                    "--mode",
                    "full",
                    "--profile",
                    "b",
                    "--transport",
                    "stdio",
                    "--backend-api-port",
                    str(backend_api_port),
                    "--dashboard-port",
                    str(dashboard_port),
                    "--json",
                    env=env,
                    timeout=1800,
                ),
                context="package full setup",
            )
            if not full_setup.get("ok"):
                raise RuntimeError(f"package full setup returned non-ok payload: {json.dumps(full_setup, ensure_ascii=False)}")

            backend_api = full_setup.get("backend_api") if isinstance(full_setup, dict) else None
            dashboard = full_setup.get("dashboard") if isinstance(full_setup, dict) else None
            if not isinstance(backend_api, dict) or backend_api.get("status") not in {"running", "running_external"}:
                raise RuntimeError(f"full setup did not produce a ready backend API: {json.dumps(full_setup, ensure_ascii=False)}")
            if not isinstance(dashboard, dict) or dashboard.get("status") not in {"running", "running_external"}:
                raise RuntimeError(f"full setup did not produce a ready dashboard: {json.dumps(full_setup, ensure_ascii=False)}")

            announce("backend openapi probe")
            backend_probe = run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json, sys, urllib.request; "
                        f"data=json.load(urllib.request.urlopen('http://127.0.0.1:{backend_api_port}/openapi.json', timeout=5)); "
                        "print(data.get('info', {}).get('title', ''))"
                    ),
                ],
                env=env,
                timeout=30,
            )
            ensure_success(backend_probe, context="backend openapi probe")
            if "Memory Palace API" not in backend_probe.stdout:
                raise RuntimeError(f"Unexpected backend title: {backend_probe.stdout}")

            announce("dashboard html probe")
            dashboard_probe = run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys, urllib.request; "
                        f"resp=urllib.request.urlopen('http://127.0.0.1:{dashboard_port}', timeout=5); "
                        "body=resp.read(4096).decode('utf-8', errors='ignore'); "
                        "print(resp.headers.get('Content-Type', '')); "
                        "print('memory palace' if 'memory palace' in body.lower() or 'vite/client' in body.lower() else body[:120])"
                    ),
                ],
                env=env,
                timeout=30,
            )
            ensure_success(dashboard_probe, context="dashboard probe")
            if "text/html" not in dashboard_probe.stdout.lower():
                raise RuntimeError(f"Dashboard probe did not return HTML: {dashboard_probe.stdout}")

            announce("dashboard status")
            dashboard_status = parse_json_output(
                run_packaged_cli(
                    package_cli_entry,
                    "dashboard",
                    "status",
                    "--setup-root",
                    str(setup_root),
                    "--json",
                    env=env,
                    timeout=120,
                ),
                context="dashboard status",
            )
            if not isinstance(dashboard_status.get("backendApi"), dict):
                raise RuntimeError(f"dashboard status did not expose backendApi: {json.dumps(dashboard_status, ensure_ascii=False)}")
            if int(dashboard_status["backendApi"].get("port") or 0) != backend_api_port:
                raise RuntimeError(f"dashboard status backend port mismatch: {json.dumps(dashboard_status, ensure_ascii=False)}")
            if int(dashboard_status["dashboard"].get("port") or 0) != dashboard_port:
                raise RuntimeError(f"dashboard status dashboard port mismatch: {json.dumps(dashboard_status, ensure_ascii=False)}")

            runtime_env_values = installer.load_env_file(installer.default_runtime_env_path(setup_root))
            sse_api_key = str(runtime_env_values.get("MCP_API_KEY") or "").strip()
            if not sse_api_key:
                raise RuntimeError("full setup runtime env did not expose MCP_API_KEY for SSE validation")
            sse_port = reserve_free_port()
            sse_log_path = tmp_root / "sse-sidecar.log"
            sse_workspace_dir = tmp_root / "sse-workspace"
            sse_workspace_dir.mkdir(parents=True, exist_ok=True)
            sse_gateway_log_path = tmp_root / "sse-gateway.log"
            sse_env = dict(env)
            sse_env["DATABASE_URL"] = database_url
            sse_env["MCP_API_KEY"] = sse_api_key
            sse_env["HOST"] = "127.0.0.1"
            sse_env["PORT"] = str(sse_port)
            with sse_log_path.open("w", encoding="utf-8") as sse_log:
                sse_process = subprocess.Popen(
                    [str(runtime_python), str(packaged_backend_root / "run_sse.py")],
                    cwd=str(packaged_backend_root),
                    env=sse_env,
                    stdout=sse_log,
                    stderr=sse_log,
                    text=True,
                )
            try:
                announce("wait for SSE sidecar")
                wait_for_http_ready(
                    f"http://127.0.0.1:{sse_port}/sse",
                    headers={
                        "Accept": "text/event-stream",
                        "X-MCP-API-Key": sse_api_key,
                    },
                    timeout_seconds=45,
                )

                announce("load SSE host model config")
                sse_model_env = capture_model_env
                sse_model_env_path = capture_model_env_path
                sse_capture_marker = f"package-install-sse-{secrets.token_hex(6)}"
                sse_config_payload = build_sse_capture_config(
                    openclaw_config_path,
                    runtime_root=runtime_env_path.parent,
                    workspace_dir=sse_workspace_dir,
                    sse_url=f"http://127.0.0.1:{sse_port}/sse",
                    sse_api_key=sse_api_key,
                    model_env=sse_model_env,
                )
                sse_config_path = tmp_root / "openclaw-sse.json"
                write_json(sse_config_path, sse_config_payload)
                sse_openclaw_env = dict(env)
                sse_openclaw_env["OPENCLAW_CONFIG_PATH"] = str(sse_config_path)
                sse_openclaw_env["OPENCLAW_STATE_DIR"] = str(openclaw_state_dir)
                sse_openclaw_env["OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR"] = str(sse_workspace_dir)
                gateway_payload = sse_config_payload.get("gateway") if isinstance(sse_config_payload, dict) else None
                gateway_auth = gateway_payload.get("auth") if isinstance(gateway_payload, dict) else None
                gateway_token = str(gateway_auth.get("token") or "").strip() if isinstance(gateway_auth, dict) else ""
                if gateway_token:
                    sse_openclaw_env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token

                announce("openclaw agent capture (sse)")
                with managed_capture_gateway(
                    env=sse_openclaw_env,
                    cwd=sse_workspace_dir,
                    gateway_log_path=sse_gateway_log_path,
                ):
                    assistant_e2e.run_agent_message(
                        OPENCLAW_BIN,
                        (
                            "Please remember this as my stable workflow preference for future sessions: "
                            f"{capture_fragment}. Marker: {sse_capture_marker}. "
                            "Reply in one short English sentence only."
                        ),
                        env=sse_openclaw_env,
                        cwd=sse_workspace_dir,
                        timeout=900,
                    )
                    announce("wait for SSE profile block")
                    sse_profile_path, _, sse_capture_verify = wait_for_profile_capture_marker(
                        env=sse_openclaw_env,
                        cwd=sse_workspace_dir,
                        marker=sse_capture_marker,
                        expected_fragment=capture_fragment,
                        timeout_seconds=120,
                    )

                announce("openclaw verify (sse)")
                sse_transport_diagnostics_path = runtime_env_path.parent / "transport-diagnostics.json"
                if promote_rule_capture_from_last_capture(
                    sse_transport_diagnostics_path,
                    verify_payload=sse_capture_verify,
                ):
                    announce("normalize SSE rule-capture decision from real capture path")
                sse_verify = parse_json_output(
                    run(
                        [OPENCLAW_BIN, "memory-palace", "verify", "--json"],
                        env=sse_openclaw_env,
                        cwd=sse_workspace_dir,
                        timeout=600,
                    ),
                    context="openclaw verify (sse)",
                )
                assert_pass_diagnostic_status(sse_verify, context="openclaw verify (sse)")

                announce("openclaw doctor (sse)")
                sse_doctor = parse_json_output(
                    run(
                        [OPENCLAW_BIN, "memory-palace", "doctor", "--query", sse_capture_marker, "--json"],
                        env=sse_openclaw_env,
                        cwd=sse_workspace_dir,
                        timeout=600,
                    ),
                    context="openclaw doctor (sse)",
                )
                assert_pass_diagnostic_status(sse_doctor, context="openclaw doctor (sse)")

                announce("openclaw smoke (sse)")
                sse_smoke = parse_json_output(
                    run(
                        [
                            OPENCLAW_BIN,
                            "memory-palace",
                            "smoke",
                            "--query",
                            sse_capture_marker,
                            "--expect-hit",
                            "--path-or-uri",
                            sse_profile_path,
                            "--json",
                        ],
                        env=sse_openclaw_env,
                        cwd=sse_workspace_dir,
                        timeout=600,
                    ),
                    context="openclaw smoke (sse)",
                )
                assert_pass_diagnostic_status(sse_smoke, context="openclaw smoke (sse)")
            finally:
                sse_process.terminate()
                try:
                    sse_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    sse_process.kill()
                    sse_process.wait(timeout=15)

            announce("dashboard stop")
            dashboard_stop = parse_json_output(
                run_packaged_cli(
                    package_cli_entry,
                    "dashboard",
                    "stop",
                    "--setup-root",
                    str(setup_root),
                    "--json",
                    env=env,
                    timeout=120,
                ),
                context="dashboard stop",
            )
            if not dashboard_stop.get("ok"):
                raise RuntimeError(f"dashboard stop failed: {json.dumps(dashboard_stop, ensure_ascii=False)}")
            orphaned_backend_pids = stop_packaged_backend_orphans(
                resolve_packaged_backend_root(package_cli_entry),
                backend_api_port,
            )
            if orphaned_backend_pids:
                announce(
                    "cleaned packaged backend orphans "
                    + ",".join(str(pid) for pid in orphaned_backend_pids)
                )
            if not installer._wait_for_port_closed("127.0.0.1", backend_api_port, timeout_seconds=15):
                raise RuntimeError(
                    f"packaged backend still reachable after dashboard stop on port {backend_api_port}"
                )

        auto_install_setup_ok = None
        if not skip_full_stack:
            auto_home_dir = tmp_root / "auto-home"
            auto_home_dir.mkdir(parents=True, exist_ok=True)
            auto_config_path = auto_home_dir / ".openclaw" / "openclaw.json"
            auto_state_dir = auto_home_dir / ".openclaw" / "state"
            auto_state_dir.mkdir(parents=True, exist_ok=True)
            auto_setup_root = auto_home_dir / ".openclaw" / "memory-palace-runtime"
            source_runtime_dir = setup_root / "runtime"
            target_runtime_dir = auto_setup_root / "runtime"
            if source_runtime_dir.is_dir() and not target_runtime_dir.exists():
                shutil.copytree(source_runtime_dir, target_runtime_dir)
            auto_env = dict(env)
            auto_env["HOME"] = str(auto_home_dir)
            auto_env["OPENCLAW_CONFIG_PATH"] = str(auto_config_path)
            auto_env["OPENCLAW_STATE_DIR"] = str(auto_state_dir)
            auto_env.pop("OPENCLAW_MEMORY_PALACE_PLUGIN_ROOT_HINT", None)

            announce("package auto-install setup (basic)")
            auto_setup = parse_json_output(
                run_packaged_cli(
                    package_cli_entry,
                    "setup",
                    "--config",
                    str(auto_config_path),
                    "--setup-root",
                    str(auto_setup_root),
                    "--mode",
                    "basic",
                    "--profile",
                    "b",
                    "--transport",
                    "stdio",
                    "--json",
                    env=auto_env,
                    timeout=1800,
                ),
                context="package auto-install setup",
            )
            if not auto_setup.get("ok"):
                raise RuntimeError(f"package auto-install setup returned non-ok payload: {json.dumps(auto_setup, ensure_ascii=False)}")
            auto_install_actions = auto_setup.get("actions") if isinstance(auto_setup, dict) else None
            if not isinstance(auto_install_actions, list) or not any(
                "installed plugin from current package path" in action for action in auto_install_actions
            ):
                raise RuntimeError(f"package auto-install setup did not auto-install plugin: {json.dumps(auto_setup, ensure_ascii=False)}")

            announce("openclaw plugins inspect after auto-install")
            auto_plugins_info = parse_json_output(
                run(
                    [OPENCLAW_BIN, "plugins", "inspect", "memory-palace", "--json"],
                    env=auto_env,
                    timeout=120,
                ),
                context="openclaw plugins inspect after auto-install setup",
            )
            auto_plugin_root = resolve_plugin_install_root_from_info(auto_plugins_info)
            if not (auto_plugin_root / "release" / "backend").is_dir():
                raise RuntimeError(
                    f"Auto-install setup did not yield a packaged plugin root: {auto_plugin_root}"
                )
            auto_install_setup_ok = True

        summary = {
            "ok": True,
            "tmp_root": str(tmp_root),
            "packed_tgz": str(tgz_runtime_path),
            "installed_plugin_root": str(installed_plugin_root),
            "setup_root": str(setup_root),
            "onboarding_b_request_path": llm_support.get("requestPathUsed"),
            "onboarding_c_effective_profile": predicted_apply.get("effectiveProfile"),
            "onboarding_c_missing_fields": len(predicted_apply.get("missingFields") or []),
            "verify_status": verify.get("status"),
            "verify_warn_ids": collect_warn_ids(verify),
            "smoke_status": seeded_smoke.get("status"),
            "smoke_warn_ids": collect_warn_ids(seeded_smoke),
            "smoke_mode": "seeded_retrieval",
            "stdio_capture_profile_path": stdio_profile_path,
            "stdio_capture_marker": stdio_capture_marker,
            "stdio_capture_verify_status": stdio_verify.get("status"),
            "stdio_capture_verify_warn_ids": collect_warn_ids(stdio_verify),
            "stdio_capture_doctor_status": stdio_doctor.get("status"),
            "stdio_capture_doctor_warn_ids": collect_warn_ids(stdio_doctor),
            "stdio_capture_smoke_status": stdio_capture_smoke.get("status"),
            "stdio_capture_smoke_warn_ids": collect_warn_ids(stdio_capture_smoke),
            "stdio_capture_model_env_path": capture_model_env_path,
            "full_stack_skipped": skip_full_stack,
        }
        if not skip_full_stack:
            summary["full_setup_backend_status"] = backend_api.get("status")
            summary["full_setup_dashboard_status"] = dashboard.get("status")
            summary["sse_verify_status"] = sse_verify.get("status")
            summary["sse_verify_warn_ids"] = collect_warn_ids(sse_verify)
            summary["sse_doctor_status"] = sse_doctor.get("status")
            summary["sse_doctor_warn_ids"] = collect_warn_ids(sse_doctor)
            summary["sse_smoke_status"] = sse_smoke.get("status")
            summary["sse_smoke_warn_ids"] = collect_warn_ids(sse_smoke)
            summary["sse_capture_marker"] = sse_capture_marker
            summary["sse_capture_profile_path"] = sse_profile_path
            summary["sse_model_env_path"] = sse_model_env_path
            summary["auto_install_setup_ok"] = auto_install_setup_ok
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    finally:
        try:
            tgz_path.unlink()
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
