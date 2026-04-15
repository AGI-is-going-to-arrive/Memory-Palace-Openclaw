#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import locale
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request

import openclaw_memory_palace_installer as installer
from openclaw_json_output import extract_json_from_streams


REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER_SCRIPT = REPO_ROOT / "scripts" / "openclaw_memory_palace.py"
PACKAGE_INSTALL_SCRIPT = REPO_ROOT / "scripts" / "test_openclaw_memory_palace_package_install.py"
HOST_BRIDGE_E2E_SCRIPT = REPO_ROOT / "scripts" / "openclaw_host_bridge_e2e.py"
ASSISTANT_DERIVED_E2E_SCRIPT = REPO_ROOT / "scripts" / "openclaw_assistant_derived_e2e.py"
PHASE45_E2E_SCRIPT = REPO_ROOT / "scripts" / "openclaw_memory_palace_phase45_e2e.py"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / ".tmp" / "windows-native-validation"
DEFAULT_OPENCLAW_BIN = str(os.environ.get("OPENCLAW_BIN") or "").strip() or shutil.which("openclaw") or "openclaw"
PROFILE_VALUES = ("a", "b", "c", "d")
MODEL_ENV_REQUIRED_PROFILES = {"c", "d"}
REPORT_JSON_NAME = "windows_native_validation_report.json"
REPORT_MARKDOWN_NAME = "windows_native_validation_report.md"
WINDOWS_ONLY_MESSAGE = "Windows native validation can only execute on Windows hosts."
TRANSPARENT_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/w8AAusB9Y9s3P8AAAAASUVORK5CYII="
)
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


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _strip_wrapping_quotes(value: str) -> str:
    trimmed = value.strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {'"', "'"}:
        return trimmed[1:-1]
    return trimmed


def parse_profiles(raw: str) -> list[str]:
    profiles: list[str] = []
    seen: set[str] = set()
    for item in str(raw or "").split(","):
        profile = item.strip().lower()
        if not profile:
            continue
        if profile not in PROFILE_VALUES:
            raise ValueError(f"Unsupported profile: {profile}")
        if profile in seen:
            continue
        profiles.append(profile)
        seen.add(profile)
    if not profiles:
        raise ValueError("At least one profile is required.")
    return profiles


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Windows-native OpenClaw validation for Memory Palace.",
    )
    parser.add_argument("--profiles", default="b", help="Comma-separated profiles to validate. Default: b")
    parser.add_argument("--mode", choices=("basic", "full", "dev"), default="basic")
    parser.add_argument("--transport", choices=("stdio", "sse"), default="stdio")
    parser.add_argument("--model-env", help="Optional runtime model env file for profiles C/D.")
    parser.add_argument(
        "--artifacts-dir",
        default=str(DEFAULT_ARTIFACTS_DIR),
        help="Directory for sanitized JSON/Markdown reports.",
    )
    parser.add_argument("--skip-package-install", action="store_true")
    parser.add_argument("--skip-advanced", action="store_true")
    parser.add_argument("--skip-full-stack", action="store_true")
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--openclaw-bin", default=DEFAULT_OPENCLAW_BIN)
    return parser.parse_args(argv)


def ensure_windows(platform_name: str | None = None) -> None:
    normalized = (platform_name or sys.platform).lower()
    if not normalized.startswith("win"):
        raise RuntimeError(WINDOWS_ONLY_MESSAGE)


def load_env_file(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    if not path.is_file():
        raise FileNotFoundError(f"Model env file not found: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.replace("\ufeff", "", 1).strip()
        if not line or line.startswith("#"):
            continue
        normalized = line.removeprefix("export ").strip()
        if "=" not in normalized:
            continue
        key, value = normalized.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _strip_wrapping_quotes(value)
    return values


def validate_requested_profiles(profiles: Sequence[str], model_env: Mapping[str, str]) -> None:
    missing = [profile for profile in profiles if profile in MODEL_ENV_REQUIRED_PROFILES and not model_env]
    if missing:
        joined = ", ".join(profile.upper() for profile in missing)
        raise ValueError(f"Profiles {joined} require --model-env with real runtime model settings.")


def make_public_command_label(command: Sequence[str]) -> str:
    if not command:
        return "command"
    rendered = [Path(command[0]).name or str(command[0])]
    rendered.extend(str(part) for part in command[1:])
    return " ".join(rendered)


def _decode_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    encodings = [
        "utf-8",
        "utf-8-sig",
        locale.getpreferredencoding(False) or "",
        "gb18030",
        "gbk",
        "cp936",
    ]
    attempted: set[str] = set()
    for encoding in encodings:
        normalized = str(encoding or "").strip()
        if not normalized or normalized in attempted:
            continue
        attempted.add(normalized)
        try:
            return value.decode(normalized)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def run_command(
    command: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    timeout: int = 1800,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    completed = runner(
        list(command),
        cwd=str(cwd or REPO_ROOT),
        env=dict(env or os.environ.copy()),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout=_decode_output(completed.stdout),
        stderr=_decode_output(completed.stderr),
    )


def parse_json_output(stdout: str, stderr: str, *, context: str) -> Any:
    try:
        return extract_json_from_streams(stdout, stderr)
    except json.JSONDecodeError as exc:
        text_stdout = str(stdout or "").strip()
        text_stderr = str(stderr or "").strip()
        if not text_stdout and not text_stderr:
            raise RuntimeError(f"{context} returned empty stdout and stderr") from exc
        raise RuntimeError(f"{context} returned invalid JSON") from exc


def run_json_command(
    command: Sequence[str],
    *,
    context: str,
    env: Mapping[str, str],
    cwd: Path | None = None,
    timeout: int = 1800,
    runner: Runner = subprocess.run,
) -> Any:
    completed = run_command(command, env=env, cwd=cwd, timeout=timeout, runner=runner)
    if completed.returncode != 0:
        stdout = str(completed.stdout or "").strip()
        stderr = str(completed.stderr or "").strip()
        stdout_tail = stdout[-4000:]
        stderr_tail = stderr[-4000:]
        details: list[str] = [f"{context} failed"]
        if stdout_tail:
            details.append(f"stdout: {stdout_tail}")
        if stderr_tail:
            details.append(f"stderr: {stderr_tail}")
        raise RuntimeError("\n".join(details))
    return parse_json_output(completed.stdout, completed.stderr, context=context)


def config_payload_has_real_models(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    models = payload.get("models")
    providers = models.get("providers") if isinstance(models, dict) else None
    if isinstance(providers, dict) and providers:
        return True
    agents = payload.get("agents")
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    model = defaults.get("model") if isinstance(defaults, dict) else None
    return isinstance(model, dict) and str(model.get("primary") or "").strip() != ""


def phase23_e2e_supported(env: Mapping[str, str]) -> tuple[bool, str]:
    candidate_paths: list[Path] = []
    explicit_config = str(env.get("OPENCLAW_CONFIG_PATH") or "").strip()
    if explicit_config:
        candidate_paths.append(Path(explicit_config).expanduser().resolve())
    home_config = Path.home() / ".openclaw" / "openclaw.json"
    if home_config not in candidate_paths:
        candidate_paths.append(home_config)
    for candidate in candidate_paths:
        if not candidate.is_file():
            continue
        try:
            payload = installer.read_json_file(candidate)
        except Exception:  # noqa: BLE001
            continue
        if config_payload_has_real_models(payload):
            return True, f"usable model providers found in {candidate}"
    return False, "phase2/3 real OpenClaw e2e requires a config with real model providers; skipping in this environment"


def phase45_model_env_supported(model_env: Mapping[str, str]) -> bool:
    base_url = installer.normalize_chat_api_base(
        str(model_env.get("SMART_EXTRACTION_LLM_API_BASE") or "").strip()
        or str(model_env.get("WRITE_GUARD_LLM_API_BASE") or "").strip()
        or str(model_env.get("COMPACT_GIST_LLM_API_BASE") or "").strip()
        or str(model_env.get("LLM_API_BASE") or "").strip()
        or str(model_env.get("INTENT_LLM_API_BASE") or "").strip()
        or str(model_env.get("LLM_RESPONSES_URL") or "").strip()
        or str(model_env.get("OPENAI_BASE_URL") or "").strip()
        or str(model_env.get("OPENAI_API_BASE") or "").strip()
    )
    model = (
        str(model_env.get("SMART_EXTRACTION_LLM_MODEL") or "").strip()
        or str(model_env.get("WRITE_GUARD_LLM_MODEL") or "").strip()
        or str(model_env.get("COMPACT_GIST_LLM_MODEL") or "").strip()
        or str(model_env.get("LLM_MODEL_NAME") or "").strip()
        or str(model_env.get("INTENT_LLM_MODEL") or "").strip()
        or str(model_env.get("OPENAI_MODEL") or "").strip()
        or str(model_env.get("LLM_MODEL") or "").strip()
    )
    return bool(base_url and model)


def phase45_e2e_supported(
    env: Mapping[str, str],
    profile: str,
    model_env: Mapping[str, str],
) -> tuple[bool, str]:
    if profile not in MODEL_ENV_REQUIRED_PROFILES:
        return False, f"phase4/5 real OpenClaw e2e only runs for profiles {', '.join(sorted(MODEL_ENV_REQUIRED_PROFILES)).upper()}"
    if not model_env:
        return False, "phase4/5 real OpenClaw e2e requires --model-env for smart extraction"
    if not phase45_model_env_supported(model_env):
        return False, "phase4/5 real OpenClaw e2e requires SMART_EXTRACTION_LLM_* or compatible WRITE_GUARD/INTENT/LLM_RESPONSES/OPENAI model env values"
    phase23_supported, phase23_reason = phase23_e2e_supported(env)
    if not phase23_supported:
        return False, phase23_reason
    llm_model = (
        str(model_env.get("SMART_EXTRACTION_LLM_MODEL") or "").strip()
        or str(model_env.get("WRITE_GUARD_LLM_MODEL") or "").strip()
        or str(model_env.get("COMPACT_GIST_LLM_MODEL") or "").strip()
        or str(model_env.get("LLM_MODEL_NAME") or "").strip()
        or str(model_env.get("INTENT_LLM_MODEL") or "").strip()
        or str(model_env.get("OPENAI_MODEL") or "").strip()
        or "unknown-model"
    )
    return True, f"usable smart-extraction env found ({profile.upper()}, {llm_model})"


def reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def reserve_distinct_ports(count: int) -> list[int]:
    ports: list[int] = []
    while len(ports) < max(1, count):
        candidate = reserve_free_port()
        if candidate not in ports:
            ports.append(candidate)
    return ports


def wait_for_http_ready(
    url: str,
    *,
    timeout_seconds: int = 120,
    validator: Callable[[str, str], bool] | None = None,
) -> None:
    deadline = time.time() + max(5, timeout_seconds)
    last_error = ""
    while time.time() < deadline:
        try:
            with urllib_request.urlopen(url, timeout=5) as response:
                content_type = str(response.headers.get("Content-Type") or "").lower()
                body = response.read(4096).decode("utf-8", errors="ignore")
                if validator is None or validator(content_type, body):
                    return
                last_error = f"validator rejected response from {url}"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def wait_for_sse_route(url: str, *, timeout_seconds: int = 120) -> None:
    deadline = time.time() + max(5, timeout_seconds)
    last_error = ""
    while time.time() < deadline:
        request = urllib_request.Request(url, method="GET")
        try:
            with urllib_request.urlopen(request, timeout=5) as response:
                if int(response.status) == 200:
                    return
                last_error = f"http {response.status}"
        except urllib_error.HTTPError as exc:
            last_error = f"http {exc.code}"
            if exc.code in {200, 401, 403, 405}:
                return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(f"Timed out waiting for SSE route {url}: {last_error}")


def build_openclaw_env(
    *,
    base_env: Mapping[str, str],
    model_env: Mapping[str, str],
    config_path: Path,
    state_dir: Path,
) -> dict[str, str]:
    env = {
        key: value
        for key, value in base_env.items()
        if not any(key.startswith(prefix) for prefix in ISOLATED_RUNTIME_ENV_PREFIXES)
    }
    env.update(model_env)
    env["OPENCLAW_CONFIG_PATH"] = config_path.as_posix()
    env["OPENCLAW_STATE_DIR"] = state_dir.as_posix()
    return env


def build_setup_command(
    *,
    profile: str,
    mode: str,
    transport: str,
    config_path: Path,
    setup_root: Path,
    sse_url: str | None = None,
    backend_api_port: int | None = None,
    dashboard_port: int | None = None,
) -> list[str]:
    command = [
        sys.executable,
        str(WRAPPER_SCRIPT),
        "setup",
        "--config",
        str(config_path),
        "--setup-root",
        str(setup_root),
        "--mode",
        mode,
        "--profile",
        profile,
        "--transport",
        transport,
        "--json",
    ]
    if transport == "sse":
        resolved_sse_url = str(sse_url or "").strip()
        if not resolved_sse_url:
            raise ValueError("SSE validation requires an explicit sse_url.")
        command.extend(["--sse-url", resolved_sse_url])
    if backend_api_port is not None:
        command.extend(["--backend-api-port", str(int(backend_api_port))])
    if dashboard_port is not None:
        command.extend(["--dashboard-port", str(int(dashboard_port))])
    if profile in MODEL_ENV_REQUIRED_PROFILES:
        command.append("--strict-profile")
    return command


def build_verify_chain_commands(openclaw_bin: str) -> list[tuple[str, list[str], int]]:
    return [
        ("config_validate", [openclaw_bin, "config", "validate", "--json"], 120),
        ("plugins_info", [openclaw_bin, "plugins", "inspect", "memory-palace", "--json"], 120),
        ("openclaw_status", [openclaw_bin, "status", "--json"], 120),
        ("memory_status", [openclaw_bin, "memory-palace", "status", "--json"], 180),
        ("memory_verify", [openclaw_bin, "memory-palace", "verify", "--json"], 300),
        ("memory_doctor", [openclaw_bin, "memory-palace", "doctor", "--json"], 300),
        ("memory_smoke", [openclaw_bin, "memory-palace", "smoke", "--json"], 300),
    ]


def build_file_uri(path: Path) -> str:
    return path.resolve().as_uri()


def ensure_probe_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(TRANSPARENT_PNG_BASE64))


def extract_path_or_uri(payload: Any) -> tuple[str | None, str | None]:
    if isinstance(payload, dict):
        path_value = payload.get("path")
        uri_value = payload.get("uri")
        if isinstance(path_value, str) and path_value.strip():
            return path_value, uri_value if isinstance(uri_value, str) and uri_value.strip() else None
        result = payload.get("result")
        if isinstance(result, dict):
            nested = extract_path_or_uri(result)
            if nested != (None, None):
                return nested
        results = payload.get("results")
        if isinstance(results, list):
            for item in results:
                nested = extract_path_or_uri(item)
                if nested != (None, None):
                    return nested
    return None, None


def build_advanced_commands(
    *,
    openclaw_bin: str,
    probe_token: str,
    media_path: Path,
) -> list[tuple[str, list[str], int]]:
    return [
        (
            "store_visual",
            [
                openclaw_bin,
                "memory-palace",
                "store-visual",
                "--media-ref",
                build_file_uri(media_path),
                "--summary",
                f"windows native validation {probe_token}",
                "--ocr",
                probe_token,
                "--why-relevant",
                "windows-native-validation",
                "--json",
            ],
            300,
        ),
        ("memory_index", [openclaw_bin, "memory-palace", "index", "--wait", "--json"], 600),
        ("memory_search", [openclaw_bin, "memory-palace", "search", probe_token, "--json"], 180),
    ]


def build_get_command(openclaw_bin: str, target: str) -> list[str]:
    return [openclaw_bin, "memory-palace", "get", target, "--json"]


def build_phase23_e2e_commands(
    *,
    openclaw_bin: str,
    profile_dir: Path,
) -> list[tuple[str, list[str], int]]:
    host_bridge_report = profile_dir / "phase23-host-bridge.json"
    assistant_report = profile_dir / "phase23-assistant-derived.json"
    return [
        (
            "host_bridge_e2e",
            [
                sys.executable,
                str(HOST_BRIDGE_E2E_SCRIPT),
                "--openclaw-bin",
                openclaw_bin,
                "--report",
                str(host_bridge_report),
            ],
            1800,
        ),
        (
            "assistant_derived_e2e",
            [
                sys.executable,
                str(ASSISTANT_DERIVED_E2E_SCRIPT),
                "--openclaw-bin",
                openclaw_bin,
                "--report",
                str(assistant_report),
            ],
            1800,
        ),
    ]


def build_phase45_e2e_commands(
    *,
    openclaw_bin: str,
    profile_dir: Path,
    profile: str,
    model_env_path: str | None,
) -> list[tuple[str, list[str], int]]:
    if model_env_path is None:
        raise ValueError("phase45 e2e requires a materialized model env path")
    phase45_report = profile_dir / "phase45-smart-extraction.json"
    return [
        (
            "phase45_e2e",
            [
                sys.executable,
                str(PHASE45_E2E_SCRIPT),
                "--openclaw-bin",
                openclaw_bin,
                "--profile",
                profile,
                "--model-env",
                str(model_env_path),
                "--report",
                str(phase45_report),
            ],
            1800,
        )
    ]


def build_full_stack_commands(
    *,
    profile: str,
    transport: str,
    config_path: Path,
    setup_root: Path,
    backend_api_port: int | None = None,
    dashboard_port: int | None = None,
) -> list[tuple[str, list[str], int]]:
    sse_url = (
        f"http://127.0.0.1:{int(dashboard_port)}/sse"
        if transport == "sse" and dashboard_port is not None
        else None
    )
    return [
        (
            "full_setup",
            build_setup_command(
                profile=profile,
                mode="full",
                transport=transport,
                config_path=config_path,
                setup_root=setup_root,
                sse_url=sse_url,
                backend_api_port=backend_api_port,
                dashboard_port=dashboard_port,
            ),
            1800,
        ),
        (
            "dashboard_status_before",
            [
                sys.executable,
                str(WRAPPER_SCRIPT),
                "dashboard",
                "status",
                "--setup-root",
                str(setup_root),
                "--json",
            ],
            180,
        ),
        (
            "dashboard_start",
            [
                sys.executable,
                str(WRAPPER_SCRIPT),
                "dashboard",
                "start",
                "--setup-root",
                str(setup_root),
                "--json",
            ],
            1800,
        ),
        (
            "dashboard_status_after_start",
            [
                sys.executable,
                str(WRAPPER_SCRIPT),
                "dashboard",
                "status",
                "--setup-root",
                str(setup_root),
                "--json",
            ],
            180,
        ),
        (
            "dashboard_stop",
            [
                sys.executable,
                str(WRAPPER_SCRIPT),
                "dashboard",
                "stop",
                "--setup-root",
                str(setup_root),
                "--json",
            ],
            300,
        ),
        (
            "dashboard_status_after_stop",
            [
                sys.executable,
                str(WRAPPER_SCRIPT),
                "dashboard",
                "status",
                "--setup-root",
                str(setup_root),
                "--json",
            ],
            180,
        ),
    ]


def build_phase_result(
    *,
    name: str,
    status: str,
    summary: str,
    steps: Sequence[Mapping[str, Any]] | None = None,
    details: str | None = None,
) -> dict[str, Any]:
    payload = {
        "name": name,
        "status": status,
        "summary": summary,
    }
    if steps is not None:
        payload["steps"] = list(steps)
    if details:
        payload["details"] = details
    return payload


def execute_command_sequence(
    commands: Sequence[tuple[str, list[str], int]],
    *,
    env: Mapping[str, str],
    runner: Runner,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for name, command, timeout in commands:
        run_json_command(command, context=name, env=env, timeout=timeout, runner=runner)
        steps.append(
            {
                "name": name,
                "status": "passed",
                "command": make_public_command_label(command),
            }
        )
    return steps


def execute_profile(
    *,
    profile: str,
    mode: str,
    transport: str,
    openclaw_bin: str,
    artifacts_dir: Path,
    base_env: Mapping[str, str],
    model_env: Mapping[str, str],
    model_env_path: str | None,
    skip_advanced: bool,
    skip_full_stack: bool,
    keep_artifacts: bool,
    runner: Runner,
) -> dict[str, Any]:
    profile_dir = artifacts_dir / f"profile-{profile}"
    basic_dir = profile_dir / "basic"
    state_dir = basic_dir / "state"
    config_path = basic_dir / "openclaw.json"
    setup_root = basic_dir / "runtime"
    basic_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    env = build_openclaw_env(base_env=base_env, model_env=model_env, config_path=config_path, state_dir=state_dir)
    phases: list[dict[str, Any]] = []
    overall_ok = True

    try:
        setup_payload: Any | None = None
        setup_attempts = 3 if transport == "sse" else 1
        last_setup_error: Exception | None = None
        for attempt in range(1, setup_attempts + 1):
            setup_backend_api_port: int | None = None
            setup_dashboard_port: int | None = None
            if transport == "sse":
                setup_backend_api_port, setup_dashboard_port = reserve_distinct_ports(2)
            setup_sse_url = (
                f"http://127.0.0.1:{setup_dashboard_port}/sse"
                if transport == "sse" and setup_dashboard_port is not None
                else None
            )
            try:
                setup_payload = run_json_command(
                    build_setup_command(
                        profile=profile,
                        mode=mode,
                        transport=transport,
                        config_path=config_path,
                        setup_root=setup_root,
                        sse_url=setup_sse_url,
                        backend_api_port=setup_backend_api_port,
                        dashboard_port=setup_dashboard_port,
                    ),
                    context="setup",
                    env=env,
                    timeout=1800,
                    runner=runner,
                )
                if transport == "sse" and setup_dashboard_port is not None and setup_backend_api_port is not None:
                    dashboard_payload = (
                        setup_payload.get("dashboard") if isinstance(setup_payload, dict) and isinstance(setup_payload.get("dashboard"), dict) else {}
                    )
                    backend_payload = (
                        setup_payload.get("backend_api") if isinstance(setup_payload, dict) and isinstance(setup_payload.get("backend_api"), dict) else {}
                    )
                    dashboard_status = str(dashboard_payload.get("status") or "").strip().lower()
                    backend_status = str(backend_payload.get("status") or "").strip().lower()
                    if dashboard_status == "running_external" or backend_status == "running_external":
                        raise RuntimeError(
                            f"setup reused external services (backend={backend_status or 'unknown'}, dashboard={dashboard_status or 'unknown'})"
                        )
                    resolved_dashboard_port = int(dashboard_payload.get("port") or setup_dashboard_port)
                    resolved_backend_port = int(backend_payload.get("port") or setup_backend_api_port)
                    wait_for_http_ready(
                        f"http://127.0.0.1:{resolved_dashboard_port}/",
                        validator=lambda content_type, body: "text/html" in content_type
                        and ("memory palace" in body.lower() or "vite/client" in body.lower()),
                    )
                    wait_for_http_ready(
                        f"http://127.0.0.1:{resolved_backend_port}/openapi.json",
                        validator=lambda content_type, body: "json" in content_type
                        and "memory palace api" in body.lower(),
                    )
                    wait_for_sse_route(f"http://127.0.0.1:{resolved_dashboard_port}/sse")
                break
            except Exception as exc:
                last_setup_error = exc
                if attempt >= setup_attempts:
                    raise
        if setup_payload is None and last_setup_error is not None:
            raise last_setup_error
        effective_profile = ""
        if isinstance(setup_payload, dict):
            effective_profile = str(setup_payload.get("effective_profile") or "").strip().lower()
        if profile in MODEL_ENV_REQUIRED_PROFILES and effective_profile and effective_profile != profile:
            raise RuntimeError(
                f"setup returned effective profile {effective_profile.upper()} for requested profile {profile.upper()}"
            )
        phases.append(build_phase_result(name="setup", status="passed", summary="setup + install chain passed"))
    except Exception as exc:
        phases.append(
            build_phase_result(
                name="setup",
                status="failed",
                summary="setup chain failed",
                details=str(exc),
            )
        )
        overall_ok = False
        phases.append(
            build_phase_result(
                name="verify_chain",
                status="skipped",
                summary="skipped because setup failed",
            )
        )
        if skip_advanced:
            phases.append(build_phase_result(name="advanced", status="skipped", summary="skipped by flag"))
        else:
            phases.append(build_phase_result(name="advanced", status="skipped", summary="skipped because setup failed"))
        if skip_full_stack:
            phases.append(build_phase_result(name="full_stack", status="skipped", summary="skipped by flag"))
        else:
            phases.append(build_phase_result(name="full_stack", status="skipped", summary="skipped because setup failed"))
        if not keep_artifacts:
            shutil.rmtree(profile_dir, ignore_errors=True)
        return {
            "profile": profile,
            "mode": mode,
            "transport": transport,
            "modelEnvProvided": bool(model_env),
            "ok": overall_ok,
            "phases": phases,
        }

    try:
        steps = execute_command_sequence(
            build_verify_chain_commands(openclaw_bin),
            env=env,
            runner=runner,
        )
        phases.append(
            build_phase_result(
                name="verify_chain",
                status="passed",
                summary="config + verify chain passed",
                steps=steps,
            )
        )
    except Exception as exc:
        phases.append(
            build_phase_result(
                name="verify_chain",
                status="failed",
                summary="config/verify chain failed",
                details=str(exc),
            )
        )
        overall_ok = False

    if skip_advanced:
        phases.append(build_phase_result(name="advanced", status="skipped", summary="skipped by flag"))
    else:
        try:
            probe_path = basic_dir / "whiteboard.png"
            ensure_probe_image(probe_path)
            probe_token = f"windows-native-{profile}-probe"
            advanced_steps = execute_command_sequence(
                build_advanced_commands(
                    openclaw_bin=openclaw_bin,
                    probe_token=probe_token,
                    media_path=probe_path,
                ),
                env=env,
                runner=runner,
            )
            search_payload = run_json_command(
                [openclaw_bin, "memory-palace", "search", probe_token, "--json"],
                context="memory_search_verify",
                env=env,
                timeout=180,
                runner=runner,
            )
            target_path, target_uri = extract_path_or_uri(search_payload)
            if not target_path and not target_uri:
                raise RuntimeError("advanced get target missing")
            get_command = build_get_command(openclaw_bin, target_path or target_uri or "")
            run_json_command(
                get_command,
                context="memory_get",
                env=env,
                timeout=180,
                runner=runner,
            )
            advanced_steps.append(
                {
                    "name": "memory_get",
                    "status": "passed",
                    "command": make_public_command_label(get_command),
                }
            )
            phase23_supported, phase23_reason = phase23_e2e_supported(env)
            phase45_supported, phase45_reason = phase45_e2e_supported(env, profile, model_env)
            if phase23_supported:
                advanced_steps.extend(
                    execute_command_sequence(
                        build_phase23_e2e_commands(
                            openclaw_bin=openclaw_bin,
                            profile_dir=profile_dir,
                        ),
                        env=env,
                        runner=runner,
                    )
                )
                if phase45_supported:
                    advanced_steps.extend(
                        execute_command_sequence(
                            build_phase45_e2e_commands(
                                openclaw_bin=openclaw_bin,
                                profile_dir=profile_dir,
                                profile=profile,
                                model_env_path=model_env_path,
                            ),
                            env=env,
                            runner=runner,
                        )
                    )
                    advanced_summary = "store-visual + index + search + get + phase2/3 + phase4/5 e2e passed"
                else:
                    advanced_steps.append(
                        {
                            "name": "phase45_e2e",
                            "status": "skipped",
                            "details": phase45_reason,
                        }
                    )
                    advanced_summary = "store-visual + index + search + get + phase2/3 e2e passed (phase4/5 e2e skipped)"
            else:
                advanced_steps.append(
                    {
                        "name": "phase23_e2e",
                        "status": "skipped",
                        "details": phase23_reason,
                    }
                )
                advanced_steps.append(
                    {
                        "name": "phase45_e2e",
                        "status": "skipped",
                        "details": phase45_reason,
                    }
                )
                advanced_summary = "store-visual + index + search + get passed (phase2/3 + phase4/5 e2e skipped)"
            phases.append(
                build_phase_result(
                    name="advanced",
                    status="passed",
                    summary=advanced_summary,
                    steps=advanced_steps,
                )
            )
        except Exception as exc:
            phases.append(
                build_phase_result(
                    name="advanced",
                    status="failed",
                    summary="advanced validation failed",
                    details=str(exc),
                )
            )
            overall_ok = False

    if skip_full_stack:
        phases.append(build_phase_result(name="full_stack", status="skipped", summary="skipped by flag"))
    else:
        try:
            full_dir = profile_dir / "full"
            full_dir.mkdir(parents=True, exist_ok=True)
            full_config_path = full_dir / "openclaw.json"
            full_setup_root = full_dir / "runtime"
            full_state_dir = full_dir / "state"
            full_state_dir.mkdir(parents=True, exist_ok=True)
            full_env = build_openclaw_env(
                base_env=base_env,
                model_env=model_env,
                config_path=full_config_path,
                state_dir=full_state_dir,
            )
            full_backend_api_port: int | None = None
            full_dashboard_port: int | None = None
            if transport == "sse":
                full_backend_api_port, full_dashboard_port = reserve_distinct_ports(2)
            full_steps = execute_command_sequence(
                build_full_stack_commands(
                    profile=profile,
                    transport=transport,
                    config_path=full_config_path,
                    setup_root=full_setup_root,
                    backend_api_port=full_backend_api_port,
                    dashboard_port=full_dashboard_port,
                ),
                env=full_env,
                runner=runner,
            )
            phases.append(
                build_phase_result(
                    name="full_stack",
                    status="passed",
                    summary="full stack setup + dashboard lifecycle passed",
                    steps=full_steps,
                )
            )
        except Exception as exc:
            phases.append(
                build_phase_result(
                    name="full_stack",
                    status="failed",
                    summary="full stack validation failed",
                    details=str(exc),
                )
            )
            overall_ok = False

    if not keep_artifacts:
        shutil.rmtree(profile_dir, ignore_errors=True)

    return {
        "profile": profile,
        "mode": mode,
        "transport": transport,
        "modelEnvProvided": bool(model_env),
        "ok": overall_ok,
        "phases": phases,
    }


def build_report(
    *,
    profiles: Sequence[str],
    mode: str,
    transport: str,
    artifacts_dir: Path,
    model_env_provided: bool,
    openclaw_bin: str,
    profile_results: Sequence[Mapping[str, Any]],
    package_result: Mapping[str, Any],
    ) -> dict[str, Any]:
    overall_ok = all(bool(item.get("ok")) for item in profile_results) and package_result.get("status") != "failed"
    return {
        "ok": overall_ok,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "platform": "windows",
        "profiles": list(profiles),
        "mode": mode,
        "transport": transport,
        "modelEnvProvided": model_env_provided,
        "openclawBin": Path(openclaw_bin).name or openclaw_bin,
        "artifactsDir": artifacts_dir.name,
        "profileResults": list(profile_results),
        "packageInstall": dict(package_result),
    }


def build_running_report(
    *,
    profiles: Sequence[str],
    mode: str,
    transport: str,
    artifacts_dir: Path,
    model_env_provided: bool,
    openclaw_bin: str,
    profile_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": False,
        "running": True,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "platform": "windows",
        "profiles": list(profiles),
        "mode": mode,
        "transport": transport,
        "modelEnvProvided": model_env_provided,
        "openclawBin": Path(openclaw_bin).name or openclaw_bin,
        "artifactsDir": artifacts_dir.name,
        "profileResults": list(profile_results),
        "packageInstall": {
            "status": "running",
            "summary": "validation still in progress",
        },
    }


def render_markdown_report(report: Mapping[str, Any]) -> str:
    lines = [
        "# Windows Native Validation Report",
        "",
        f"- Overall: {'RUNNING' if report.get('running') else ('PASS' if report.get('ok') else 'FAIL')}",
        f"- Profiles: {', '.join(report.get('profiles', []))}",
        f"- Mode: {report.get('mode')}",
        f"- Transport: {report.get('transport')}",
        f"- Model env provided: {'yes' if report.get('modelEnvProvided') else 'no'}",
        f"- Generated at: {report.get('generatedAt')}",
        "",
        "## Profiles",
        "",
    ]
    for profile_result in report.get("profileResults", []):
        lines.append(f"### Profile {str(profile_result.get('profile', '')).upper()}")
        lines.append("")
        lines.append(f"- Overall: {'PASS' if profile_result.get('ok') else 'FAIL'}")
        for phase in profile_result.get("phases", []):
            lines.append(f"- {phase.get('name')}: {phase.get('status')} - {phase.get('summary')}")
        lines.append("")
    package_result = report.get("packageInstall", {})
    lines.extend(
        [
            "## Package Install",
            "",
            f"- Status: {package_result.get('status')}",
            f"- Summary: {package_result.get('summary')}",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_report_files(report: Mapping[str, Any], artifacts_dir: Path) -> tuple[Path, Path]:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifacts_dir / REPORT_JSON_NAME
    markdown_path = artifacts_dir / REPORT_MARKDOWN_NAME
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")
    return json_path, markdown_path


def prepare_artifacts_dir(artifacts_dir: Path, profiles: Sequence[str]) -> None:
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    for report_name in (REPORT_JSON_NAME, REPORT_MARKDOWN_NAME):
        report_path = artifacts_dir / report_name
        if report_path.exists():
            report_path.unlink()
    for profile in profiles:
        shutil.rmtree(artifacts_dir / f"profile-{profile}", ignore_errors=True)


def execute_package_install(
    *,
    skip_package_install: bool,
    skip_full_stack: bool,
    base_env: Mapping[str, str],
    runner: Runner,
) -> dict[str, Any]:
    if skip_package_install:
        return {
            "status": "skipped",
            "summary": "skipped by flag",
        }
    try:
        command_env = dict(base_env)
        if skip_full_stack:
            command_env["OPENCLAW_PACKAGE_INSTALL_SKIP_FULL_STACK"] = "1"
        completed = run_command(
            [sys.executable, str(PACKAGE_INSTALL_SCRIPT)],
            env=command_env,
            timeout=3600,
            runner=runner,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "package install validation failed").strip())
        return {
            "status": "passed",
            "summary": "package install validation passed",
        }
    except Exception as exc:
        return {
            "status": "failed",
            "summary": "package install validation failed",
            "details": str(exc),
        }


def emit_report_summary(report: Mapping[str, Any]) -> None:
    for profile_result in report.get("profileResults", []):
        profile = str(profile_result.get("profile") or "").upper()
        status = "PASS" if profile_result.get("ok") else "FAIL"
        print(f"[windows-native-validation] profile {profile}: {status}")
        for phase in profile_result.get("phases", []):
            details = str(phase.get("details") or "").strip()
            print(
                f"[windows-native-validation]   {phase.get('name')}: "
                f"{phase.get('status')} - {phase.get('summary')}"
            )
            if details:
                print(f"[windows-native-validation]     details: {details}")
    package_result = report.get("packageInstall", {})
    print(
        f"[windows-native-validation] package-install: "
        f"{package_result.get('status')} - {package_result.get('summary')}"
    )
    package_details = str(package_result.get("details") or "").strip()
    if package_details:
        print(f"[windows-native-validation]   details: {package_details}")


def execute_validation(
    args: argparse.Namespace,
    *,
    platform_name: str | None = None,
    runner: Runner = subprocess.run,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    ensure_windows(platform_name)
    profiles = parse_profiles(args.profiles)
    artifacts_dir = Path(args.artifacts_dir).expanduser().resolve()
    model_env_path = Path(args.model_env).expanduser().resolve() if args.model_env else None
    model_env = load_env_file(model_env_path)
    validate_requested_profiles(profiles, model_env)
    prepare_artifacts_dir(artifacts_dir, profiles)

    base_env = os.environ.copy()
    if any(profile in MODEL_ENV_REQUIRED_PROFILES for profile in profiles):
        base_env.setdefault("OPENCLAW_MEMORY_PALACE_PROFILE_PROBE_TIMEOUT_SEC", "20")
    profile_results: list[dict[str, Any]] = []
    for profile in profiles:
        profile_results.append(
            execute_profile(
                profile=profile,
                mode=args.mode,
                transport=args.transport,
                openclaw_bin=args.openclaw_bin,
                artifacts_dir=artifacts_dir,
                base_env=base_env,
                model_env=model_env,
                model_env_path=str(model_env_path) if model_env_path else None,
                skip_advanced=args.skip_advanced,
                skip_full_stack=args.skip_full_stack,
                keep_artifacts=args.keep_artifacts,
                runner=runner,
            )
        )
        if callable(progress_callback):
            progress_callback(
                build_running_report(
                    profiles=profiles,
                    mode=args.mode,
                    transport=args.transport,
                    artifacts_dir=artifacts_dir,
                    model_env_provided=bool(model_env),
                    openclaw_bin=args.openclaw_bin,
                    profile_results=profile_results,
                )
            )
    package_result = execute_package_install(
        skip_package_install=args.skip_package_install,
        skip_full_stack=args.skip_full_stack,
        base_env=base_env,
        runner=runner,
    )
    return build_report(
        profiles=profiles,
        mode=args.mode,
        transport=args.transport,
        artifacts_dir=artifacts_dir,
        model_env_provided=bool(model_env),
        openclaw_bin=args.openclaw_bin,
        profile_results=profile_results,
        package_result=package_result,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    artifacts_dir = Path(args.artifacts_dir).expanduser().resolve()
    initial_profiles = parse_profiles(args.profiles)
    write_report_files(
        build_running_report(
            profiles=initial_profiles,
            mode=args.mode,
            transport=args.transport,
            artifacts_dir=artifacts_dir,
            model_env_provided=bool(args.model_env),
            openclaw_bin=args.openclaw_bin,
            profile_results=[],
        ),
        artifacts_dir,
    )
    try:
        report = execute_validation(
            args,
            progress_callback=lambda current: write_report_files(current, artifacts_dir),
        )
    except Exception as exc:
        failure_report = {
            "ok": False,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "platform": "windows",
            "profiles": parse_profiles(args.profiles),
            "mode": args.mode,
            "transport": args.transport,
            "modelEnvProvided": bool(args.model_env),
            "openclawBin": Path(args.openclaw_bin).name or args.openclaw_bin,
            "artifactsDir": artifacts_dir.name,
            "profileResults": [],
            "packageInstall": {"status": "skipped", "summary": "not executed"},
            "failureSummary": "validation failed before any profile result was recorded",
        }
        write_report_files(failure_report, artifacts_dir)
        print(str(exc), file=sys.stderr)
        return 1

    json_path, markdown_path = write_report_files(report, artifacts_dir)
    status_text = "PASS" if report["ok"] else "FAIL"
    print(f"[windows-native-validation] {status_text}")
    emit_report_summary(report)
    print(f"[windows-native-validation] json={json_path}")
    print(f"[windows-native-validation] markdown={markdown_path}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
