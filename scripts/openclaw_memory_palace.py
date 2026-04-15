#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import openclaw_memory_palace_installer as installer
from openclaw_json_output import extract_json_from_streams


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER_SCRIPT = REPO_ROOT / "scripts" / "openclaw_memory_palace_installer.py"
PRE_PUBLISH_SCRIPT = REPO_ROOT / "scripts" / "pre_publish_check.sh"
VISUAL_BENCHMARK_SCRIPT = REPO_ROOT / "scripts" / "openclaw_visual_memory_benchmark.py"
RELEASE_GATE_RUNNER_SCRIPT = REPO_ROOT / "scripts" / "openclaw_memory_palace_release_gate.py"
PLUGIN_ID = "memory-palace"
DEFAULT_OPENCLAW_JSON_TIMEOUT_SECONDS = int(
    os.environ.get("OPENCLAW_MEMORY_PALACE_OPENCLAW_JSON_TIMEOUT_SEC", "180")
)
SETUP_VALIDATION_RETRY_STEPS = {"doctor", "smoke"}
SETUP_VALIDATION_RETRY_DELAYS_SECONDS = (2.0, 5.0)


def default_openclaw_bin() -> str:
    resolved_openclaw = installer.resolve_openclaw_binary()
    if resolved_openclaw:
        return resolved_openclaw
    repo_wrapper = REPO_ROOT / "scripts" / "dev" / "openclaw-local-wrapper"
    if os.name != "nt" and repo_wrapper.is_file():
        return str(repo_wrapper)
    return "openclaw"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unified installer and validation wrapper for the OpenClaw Memory Palace plugin.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser("install", help="Install or update OpenClaw config for Memory Palace.")
    install_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    install_parser.add_argument("--transport", choices=("stdio", "sse"), default="stdio")
    install_parser.add_argument("--sse-url", help="SSE endpoint when --transport=sse.")
    install_parser.add_argument("--api-key-env", default="MCP_API_KEY", help="SSE API key env name.")
    install_parser.add_argument("--database-url", help="DATABASE_URL injected into stdio env.")
    install_parser.add_argument("--timeout-ms", type=int, default=20_000)
    install_parser.add_argument("--connect-retries", type=int, default=1)
    install_parser.add_argument("--connect-backoff-ms", type=int, default=250)
    install_parser.add_argument("--no-activate", action="store_true", help="Do not switch plugins.slots.memory.")
    install_parser.add_argument("--dry-run", action="store_true")
    install_parser.add_argument("--print-config-path", action="store_true")
    install_parser.add_argument("--json", action="store_true")

    setup_parser = subparsers.add_parser("setup", help="Bootstrap a user-state runtime and wire OpenClaw config.")
    setup_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    setup_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    setup_parser.add_argument("--env-file", help="Explicit runtime env file path.")
    setup_parser.add_argument("--transport", choices=("stdio", "sse"), default="stdio")
    setup_parser.add_argument("--mode", choices=("basic", "full", "dev"), default="basic")
    setup_parser.add_argument("--profile", choices=("a", "b", "c", "d"), default="b")
    setup_parser.add_argument("--sse-url", help="SSE endpoint when --transport=sse.")
    setup_parser.add_argument("--api-key-env", default="MCP_API_KEY", help="SSE API key env name.")
    setup_parser.add_argument("--timeout-ms", type=int, default=20_000)
    setup_parser.add_argument("--connect-retries", type=int, default=1)
    setup_parser.add_argument("--connect-backoff-ms", type=int, default=250)
    setup_parser.add_argument("--database-path", help="User-state sqlite database file path.")
    setup_parser.add_argument("--mcp-api-key", help="Explicit MCP_API_KEY. If omitted, setup can generate a local key.")
    setup_parser.add_argument("--allow-insecure-local", action="store_true")
    setup_parser.add_argument(
        "--allow-generate-remote-api-key",
        action="store_true",
        help="Explicitly allow setup to generate MCP_API_KEY for non-loopback SSE endpoints.",
    )
    setup_parser.add_argument("--reconfigure", action="store_true")
    setup_parser.add_argument("--strict-profile", action="store_true")
    setup_parser.add_argument("--dashboard-host")
    setup_parser.add_argument("--dashboard-port", type=int)
    setup_parser.add_argument("--backend-api-host")
    setup_parser.add_argument("--backend-api-port", type=int)
    setup_parser.add_argument("--no-activate", action="store_true")
    setup_parser.add_argument("--dry-run", action="store_true")
    setup_parser.add_argument("--json", action="store_true")
    setup_parser.add_argument("--embedding-api-base")
    setup_parser.add_argument("--embedding-api-key")
    setup_parser.add_argument("--embedding-model")
    setup_parser.add_argument("--embedding-dim")
    setup_parser.add_argument("--reranker-api-base")
    setup_parser.add_argument("--reranker-api-key")
    setup_parser.add_argument("--reranker-model")
    setup_parser.add_argument("--llm-api-base")
    setup_parser.add_argument("--llm-api-key")
    setup_parser.add_argument("--llm-model")
    setup_parser.add_argument("--write-guard-llm-api-base")
    setup_parser.add_argument("--write-guard-llm-api-key")
    setup_parser.add_argument("--write-guard-llm-model")
    setup_parser.add_argument("--compact-gist-llm-api-base")
    setup_parser.add_argument("--compact-gist-llm-api-key")
    setup_parser.add_argument("--compact-gist-llm-model")
    setup_parser.add_argument("--validate", action="store_true", help="Run verify + doctor + smoke after a successful setup.")
    setup_parser.add_argument("--openclaw-bin", default=default_openclaw_bin(), help="OpenClaw binary used for post-setup validation.")

    onboarding_parser = subparsers.add_parser(
        "onboarding",
        help="Structured chat-friendly onboarding flow for collecting provider settings and optionally applying setup.",
    )
    onboarding_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    onboarding_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    onboarding_parser.add_argument("--env-file", help="Explicit runtime env file path.")
    onboarding_parser.add_argument("--transport", choices=("stdio", "sse"), default="stdio")
    onboarding_parser.add_argument("--mode", choices=("basic", "full", "dev"), default="basic")
    onboarding_parser.add_argument("--profile", choices=("a", "b", "c", "d"), default="b")
    onboarding_parser.add_argument("--sse-url", help="SSE endpoint when --transport=sse.")
    onboarding_parser.add_argument("--api-key-env", default="MCP_API_KEY", help="SSE API key env name.")
    onboarding_parser.add_argument("--timeout-ms", type=int, default=20_000)
    onboarding_parser.add_argument("--connect-retries", type=int, default=1)
    onboarding_parser.add_argument("--connect-backoff-ms", type=int, default=250)
    onboarding_parser.add_argument("--database-path", help="User-state sqlite database file path.")
    onboarding_parser.add_argument("--mcp-api-key", help="Explicit MCP_API_KEY. If omitted, setup can generate a local key.")
    onboarding_parser.add_argument("--allow-insecure-local", action="store_true")
    onboarding_parser.add_argument("--allow-generate-remote-api-key", action="store_true")
    onboarding_parser.add_argument("--reconfigure", action="store_true")
    onboarding_parser.add_argument("--strict-profile", action="store_true")
    onboarding_parser.add_argument("--dashboard-host")
    onboarding_parser.add_argument("--dashboard-port", type=int)
    onboarding_parser.add_argument("--backend-api-host")
    onboarding_parser.add_argument("--backend-api-port", type=int)
    onboarding_parser.add_argument("--no-activate", action="store_true")
    onboarding_parser.add_argument("--dry-run", action="store_true")
    onboarding_parser.add_argument("--json", action="store_true")
    onboarding_parser.add_argument("--apply", action="store_true", help="Apply setup after reporting onboarding readiness.")
    onboarding_parser.add_argument("--validate", action="store_true", help="Run verify + doctor + smoke after apply.")
    onboarding_parser.add_argument("--openclaw-bin", default=default_openclaw_bin(), help="OpenClaw binary used for post-apply validation.")
    onboarding_parser.add_argument("--embedding-api-base")
    onboarding_parser.add_argument("--embedding-api-key")
    onboarding_parser.add_argument("--embedding-model")
    onboarding_parser.add_argument("--embedding-dim")
    onboarding_parser.add_argument("--reranker-api-base")
    onboarding_parser.add_argument("--reranker-api-key")
    onboarding_parser.add_argument("--reranker-model")
    onboarding_parser.add_argument("--llm-api-base")
    onboarding_parser.add_argument("--llm-api-key")
    onboarding_parser.add_argument("--llm-model")
    onboarding_parser.add_argument("--write-guard-llm-api-base")
    onboarding_parser.add_argument("--write-guard-llm-api-key")
    onboarding_parser.add_argument("--write-guard-llm-model")
    onboarding_parser.add_argument("--compact-gist-llm-api-base")
    onboarding_parser.add_argument("--compact-gist-llm-api-key")
    onboarding_parser.add_argument("--compact-gist-llm-model")

    uninstall_parser = subparsers.add_parser("uninstall", help="Disable/uninstall the plugin and clean local runtime state.")
    uninstall_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    uninstall_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    uninstall_parser.add_argument("--openclaw-bin", default=default_openclaw_bin())
    uninstall_parser.add_argument("--keep-files", action="store_true", help="Keep installed plugin files on disk.")
    uninstall_parser.add_argument("--force", action="store_true", help="Skip OpenClaw uninstall confirmation prompt.")
    uninstall_parser.add_argument("--purge-runtime", action="store_true", help="Delete the user-state runtime directory and database.")
    uninstall_parser.add_argument("--keep-runtime", action="store_true", help="Deprecated alias; runtime is now kept by default.")
    uninstall_parser.add_argument("--dry-run", action="store_true")
    uninstall_parser.add_argument("--json", action="store_true")

    enable_parser = subparsers.add_parser("enable", help="Enable the plugin entry in the current OpenClaw config.")
    enable_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    enable_parser.add_argument("--openclaw-bin", default=default_openclaw_bin())

    disable_parser = subparsers.add_parser("disable", help="Disable the plugin entry in the current OpenClaw config.")
    disable_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    disable_parser.add_argument("--openclaw-bin", default=default_openclaw_bin())

    dashboard_parser = subparsers.add_parser("dashboard", help="Inspect or control the bundled dashboard runtime.")
    dashboard_subparsers = dashboard_parser.add_subparsers(dest="dashboard_command", required=True)

    dashboard_status_parser = dashboard_subparsers.add_parser("status", help="Show dashboard runtime status.")
    dashboard_status_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    dashboard_status_parser.add_argument("--json", action="store_true")

    dashboard_start_parser = dashboard_subparsers.add_parser("start", help="Install dashboard deps if needed and start the dashboard.")
    dashboard_start_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    dashboard_start_parser.add_argument("--dashboard-host")
    dashboard_start_parser.add_argument("--dashboard-port", type=int)
    dashboard_start_parser.add_argument("--backend-api-host")
    dashboard_start_parser.add_argument("--backend-api-port", type=int)
    dashboard_start_parser.add_argument("--dry-run", action="store_true")
    dashboard_start_parser.add_argument("--json", action="store_true")

    dashboard_stop_parser = dashboard_subparsers.add_parser("stop", help="Stop a dashboard process launched by this CLI.")
    dashboard_stop_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    dashboard_stop_parser.add_argument("--dry-run", action="store_true")
    dashboard_stop_parser.add_argument("--json", action="store_true")

    subparsers.add_parser("stage-package", help="Stage repo runtime assets into extensions/memory-palace/release.")

    migrate_parser = subparsers.add_parser("migrate", help="Apply or inspect pending local runtime DB migrations.")
    migrate_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    migrate_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    migrate_parser.add_argument("--env-file", help="Explicit runtime env file path.")
    migrate_parser.add_argument("--database-url", help="Override DATABASE_URL instead of reading runtime.env.")
    migrate_parser.add_argument("--migrations-dir", help="Override the backend migrations directory.")
    migrate_parser.add_argument("--lock-file", help="Override the migration lock file path.")
    migrate_parser.add_argument("--lock-timeout-sec", type=float, default=10.0)
    migrate_parser.add_argument("--dry-run", action="store_true")
    migrate_parser.add_argument("--json", action="store_true")

    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="Reapply the current bootstrap setup and then run DB migrations.",
    )
    upgrade_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    upgrade_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    upgrade_parser.add_argument("--env-file", help="Explicit runtime env file path.")
    upgrade_parser.add_argument("--strict-profile", action="store_true")
    upgrade_parser.add_argument("--dry-run", action="store_true")
    upgrade_parser.add_argument("--json", action="store_true")

    verify_parser = subparsers.add_parser("verify", help="Run the plugin's user-facing verify command.")
    verify_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    verify_parser.add_argument("--openclaw-bin", default=default_openclaw_bin())
    verify_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="Run the plugin's deep diagnostic command.")
    doctor_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    doctor_parser.add_argument("--openclaw-bin", default=default_openclaw_bin())
    doctor_parser.add_argument("--query", help="Optional search probe query.")
    doctor_parser.add_argument("--json", action="store_true")

    smoke_parser = subparsers.add_parser("smoke", help="Run the plugin's smoke command.")
    smoke_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    smoke_parser.add_argument("--openclaw-bin", default=default_openclaw_bin())
    smoke_parser.add_argument("--query", help="Smoke search query.")
    smoke_parser.add_argument("--path-or-uri", help="Known path or URI for follow-up read.")
    smoke_parser.add_argument("--expect-hit", action="store_true")
    smoke_parser.add_argument("--json", action="store_true")

    bootstrap_status_parser = subparsers.add_parser(
        "bootstrap-status",
        help="Inspect the local bootstrap/setup status without requiring the dashboard API.",
    )
    bootstrap_status_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    bootstrap_status_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    bootstrap_status_parser.add_argument("--env-file", help="Explicit runtime env file path.")
    bootstrap_status_parser.add_argument("--json", action="store_true")

    provider_probe_parser = subparsers.add_parser(
        "provider-probe",
        help="Run the provider readiness preview used by onboarding and dashboard setup.",
    )
    provider_probe_parser.add_argument("--config", help="Explicit OpenClaw config path.")
    provider_probe_parser.add_argument("--setup-root", help="User-state setup root. Defaults to ~/.openclaw/memory-palace.")
    provider_probe_parser.add_argument("--env-file", help="Explicit runtime env file path.")
    provider_probe_parser.add_argument("--transport", choices=("stdio", "sse"), default="stdio")
    provider_probe_parser.add_argument("--mode", choices=("basic", "full", "dev"), default="basic")
    provider_probe_parser.add_argument("--profile", choices=("a", "b", "c", "d"), default="b")
    provider_probe_parser.add_argument("--sse-url", help="SSE endpoint when --transport=sse.")
    provider_probe_parser.add_argument("--mcp-api-key", help="Explicit MCP_API_KEY for SSE preview flows.")
    provider_probe_parser.add_argument("--allow-insecure-local", action="store_true")
    provider_probe_parser.add_argument("--embedding-api-base")
    provider_probe_parser.add_argument("--embedding-api-key")
    provider_probe_parser.add_argument("--embedding-model")
    provider_probe_parser.add_argument("--embedding-dim")
    provider_probe_parser.add_argument("--reranker-api-base")
    provider_probe_parser.add_argument("--reranker-api-key")
    provider_probe_parser.add_argument("--reranker-model")
    provider_probe_parser.add_argument("--llm-api-base")
    provider_probe_parser.add_argument("--llm-api-key")
    provider_probe_parser.add_argument("--llm-model")
    provider_probe_parser.add_argument("--write-guard-llm-api-base")
    provider_probe_parser.add_argument("--write-guard-llm-api-key")
    provider_probe_parser.add_argument("--write-guard-llm-model")
    provider_probe_parser.add_argument("--compact-gist-llm-api-base")
    provider_probe_parser.add_argument("--compact-gist-llm-api-key")
    provider_probe_parser.add_argument("--compact-gist-llm-model")
    provider_probe_parser.add_argument("--persist", action="store_true", help="Persist the latest provider probe payload into setup state.")
    provider_probe_parser.add_argument("--json", action="store_true")

    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="Run the real OpenClaw visual-memory benchmark wrapper.",
    )
    benchmark_parser.add_argument("--profile", help="Single profile to benchmark.")
    benchmark_parser.add_argument("--profiles", help="Comma-separated benchmark profiles.")
    benchmark_parser.add_argument("--model-env", help="Local model env file for benchmark profiles.")
    benchmark_parser.add_argument("--case-count", type=int, help="Synthetic case catalog size.")
    benchmark_parser.add_argument("--case-limit", type=int, help="Executed case count per profile.")
    benchmark_parser.add_argument("--max-workers", type=int, help="Benchmark worker count.")
    benchmark_parser.add_argument(
        "--required-coverage",
        help="Comma-separated coverage keys that must be fully green.",
    )
    benchmark_parser.add_argument("--json-output", help="Benchmark JSON artifact path.")
    benchmark_parser.add_argument("--markdown-output", help="Benchmark Markdown artifact path.")
    benchmark_parser.add_argument("--resume", action="store_true", help="Resume from an existing benchmark artifact.")

    gate_parser = subparsers.add_parser("release-gate", help="Run the first-batch release gate script.")
    gate_parser.add_argument("--report", help="Markdown report path.")
    gate_parser.add_argument("--skip-backend-tests", action="store_true")
    gate_parser.add_argument("--skip-plugin-tests", action="store_true")
    gate_parser.add_argument("--skip-python-matrix", action="store_true")
    gate_parser.add_argument(
        "--enable-live-benchmark",
        action="store_true",
        help="Enable the release-only live benchmark lane.",
    )
    gate_parser.add_argument(
        "--enable-windows-native-validation",
        action="store_true",
        help="Enable the maintainer-only Windows native validation lane.",
    )
    gate_parser.add_argument(
        "--skip-onboarding-apply-validate",
        action="store_true",
        help="Skip onboarding --apply --validate black-box E2E in the legacy bash gate.",
    )
    gate_parser.add_argument("--skip-frontend", "--skip-frontend-tests", dest="skip_frontend", action="store_true")
    gate_parser.add_argument("--skip-frontend-e2e", action="store_true")
    gate_parser.add_argument("--skip-profile-smoke", action="store_true")
    gate_parser.add_argument("--skip-phase45", action="store_true")
    gate_parser.add_argument("--skip-review-smoke", action="store_true")
    gate_parser.add_argument(
        "--enable-current-host-strict-ui",
        action="store_true",
        help="Enable the opt-in current-host strict UI acceptance step in the legacy bash gate.",
    )
    gate_parser.add_argument(
        "--skip-current-host-strict-ui",
        action="store_true",
        help="Skip the opt-in current-host strict UI acceptance step in the legacy bash gate.",
    )
    gate_parser.add_argument("--profile-modes", "--profile-smoke-modes", dest="profile_modes", default="local,docker")
    gate_parser.add_argument("--phase45-profiles", default="c,d")
    gate_parser.add_argument("--review-smoke-modes", default="local,docker")
    gate_parser.add_argument("--profile-smoke-model-env", help="Local model env file for profile smoke.")
    gate_parser.add_argument("--checkpoint-dir", help="Checkpoint directory for the Python release-gate runner.")
    gate_parser.add_argument("--resume", action="store_true", help="Resume the Python release-gate runner from checkpoint.")
    gate_parser.add_argument("--legacy-bash-gate", action="store_true", help="Use the original bash release-gate instead of the checkpoint runner.")

    return parser.parse_args()


def resolve_config_path(explicit: str | None, *, openclaw_bin: str | None = None) -> Path:
    return installer.detect_setup_config_path(explicit, openclaw_bin=openclaw_bin)


def _normalize_openclaw_command_binary(command: list[str]) -> list[str]:
    if not command:
        return command
    rendered = str(command[0] or "").strip()
    if not rendered:
        return command
    if Path(rendered).stem.lower() != "openclaw":
        return command
    resolved = installer.resolve_openclaw_binary(rendered)
    if not resolved:
        return command
    return [resolved, *command[1:]]


def run_process(command: list[str], *, config_path: Path | None = None) -> int:
    env = os.environ.copy()
    if config_path is not None:
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    completed = subprocess.run(_normalize_openclaw_command_binary(command), cwd=REPO_ROOT, env=env, check=False)
    return completed.returncode


def bash_script_path(path: Path) -> str:
    expanded = path.expanduser()
    if os.name != "nt":
        return str(expanded.resolve())
    cygpath = shutil.which("cygpath")
    if cygpath:
        proc = subprocess.run(
            [cygpath, "-u", str(expanded)],
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
    return str(expanded).replace("\\", "/")


def print_cli_error(message: str, *, json_output: bool) -> int:
    payload = {
        "ok": False,
        "error": str(message),
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2), file=sys.stderr)
    else:
        print(str(message), file=sys.stderr)
    return 2


def run_openclaw_json_command(
    command: list[str],
    *,
    config_path: Path | None = None,
    timeout_seconds: int = DEFAULT_OPENCLAW_JSON_TIMEOUT_SECONDS,
) -> dict[str, object]:
    env = os.environ.copy()
    if config_path is not None:
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    normalized_command = _normalize_openclaw_command_binary(command)
    try:
        completed = subprocess.run(
            normalized_command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=max(int(timeout_seconds), 1),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": normalized_command,
            "exit_code": 124,
            "payload": {
                "ok": False,
                "summary": f"command timed out after {max(int(timeout_seconds), 1)} seconds",
            },
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
        }
    try:
        payload = extract_json_from_streams(completed.stdout, completed.stderr)
    except Exception:
        payload = {
            "ok": False,
            "summary": (completed.stderr or completed.stdout or "").strip() or "command produced no JSON payload",
        }
    return {
        "command": normalized_command,
        "exit_code": completed.returncode,
        "payload": payload,
    }


def run_setup_validation(
    *,
    openclaw_bin: str,
    config_path: Path,
    timeout_seconds: int = DEFAULT_OPENCLAW_JSON_TIMEOUT_SECONDS,
) -> dict[str, object]:
    steps = []
    step_specs = [
        ("verify", [openclaw_bin, PLUGIN_ID, "verify", "--json"]),
        ("doctor", [openclaw_bin, PLUGIN_ID, "doctor", "--json"]),
        ("smoke", [openclaw_bin, PLUGIN_ID, "smoke", "--json"]),
    ]
    overall_ok = True
    failed_step = None
    for name, command in step_specs:
        result: dict[str, object] | None = None
        payload: dict[str, object] = {}
        ok = False
        attempts = 1 + (len(SETUP_VALIDATION_RETRY_DELAYS_SECONDS) if name in SETUP_VALIDATION_RETRY_STEPS else 0)
        for attempt_index in range(attempts):
            result = run_openclaw_json_command(
                command,
                config_path=config_path,
                timeout_seconds=timeout_seconds,
            )
            payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
            ok = bool(payload.get("ok", False))
            if ok:
                break
            if name not in SETUP_VALIDATION_RETRY_STEPS or attempt_index >= attempts - 1:
                break
            time.sleep(float(SETUP_VALIDATION_RETRY_DELAYS_SECONDS[attempt_index]))
        steps.append(
            {
                "name": name,
                "ok": ok,
                "exit_code": int(result.get("exit_code") or 0) if isinstance(result, dict) else 0,
                "summary": str(payload.get("summary") or ""),
                "status": str(payload.get("status") or ""),
                "code": str(payload.get("code") or ""),
            }
        )
        if not ok:
            overall_ok = False
            failed_step = name
            break
    return {
        "ok": overall_ok,
        "failed_step": failed_step,
        "steps": steps,
    }


def validate_readable_file_arg(path_value: str | None, *, label: str) -> str:
    rendered = str(path_value or "").strip()
    if not rendered:
        raise ValueError(f"{label} must not be empty.")
    candidate = Path(rendered).expanduser()
    if not candidate.is_file():
        raise ValueError(f"{label} does not exist or is not a file: {candidate}")
    if not os.access(candidate, os.R_OK):
        raise ValueError(f"{label} is not readable: {candidate}")
    return rendered


def command_install(args: argparse.Namespace) -> int:
    forwarded = [sys.executable, str(INSTALLER_SCRIPT)]
    for name in (
        "config",
        "transport",
        "sse_url",
        "api_key_env",
        "database_url",
        "timeout_ms",
        "connect_retries",
        "connect_backoff_ms",
    ):
        value = getattr(args, name)
        if value is None:
            continue
        flag = f"--{name.replace('_', '-')}"
        forwarded.extend([flag, str(value)])
    if args.no_activate:
        forwarded.append("--no-activate")
    if args.dry_run:
        forwarded.append("--dry-run")
    if args.print_config_path:
        forwarded.append("--print-config-path")
    if args.json:
        forwarded.append("--json")
    return run_process(forwarded)


def perform_setup_from_namespace(args: argparse.Namespace) -> dict[str, object]:
    return installer.perform_setup(
        config=args.config,
        setup_root_value=args.setup_root,
        env_file_value=args.env_file,
        transport=args.transport,
        mode=args.mode,
        profile=args.profile,
        sse_url=args.sse_url,
        api_key_env=args.api_key_env,
        timeout_ms=args.timeout_ms,
        connect_retries=args.connect_retries,
        connect_backoff_ms=args.connect_backoff_ms,
        no_activate=args.no_activate,
        dry_run=args.dry_run,
        json_output=args.json,
        reconfigure=args.reconfigure,
        strict_profile=args.strict_profile,
        database_path=args.database_path,
        mcp_api_key=args.mcp_api_key,
        allow_insecure_local=args.allow_insecure_local,
        allow_generate_remote_api_key=args.allow_generate_remote_api_key,
        dashboard_host=args.dashboard_host,
        dashboard_port=args.dashboard_port,
        backend_api_host=args.backend_api_host,
        backend_api_port=args.backend_api_port,
        embedding_api_base=args.embedding_api_base,
        embedding_api_key=args.embedding_api_key,
        embedding_model=args.embedding_model,
        embedding_dim=args.embedding_dim,
        reranker_api_base=args.reranker_api_base,
        reranker_api_key=args.reranker_api_key,
        reranker_model=args.reranker_model,
        llm_api_base=args.llm_api_base,
        llm_api_key=args.llm_api_key,
        llm_model=args.llm_model,
        write_guard_llm_api_base=args.write_guard_llm_api_base,
        write_guard_llm_api_key=args.write_guard_llm_api_key,
        write_guard_llm_model=args.write_guard_llm_model,
        compact_gist_llm_api_base=args.compact_gist_llm_api_base,
        compact_gist_llm_api_key=args.compact_gist_llm_api_key,
        compact_gist_llm_model=args.compact_gist_llm_model,
    )


def attach_setup_validation(
    *,
    report: dict[str, object],
    validate: bool,
    dry_run: bool,
    openclaw_bin: str,
) -> int:
    validation = None
    exit_code = 0
    report["ok"] = bool(report.get("ok", True))
    if validate:
        if dry_run:
            validation = {
                "ok": True,
                "skipped": True,
                "reason": "dry_run",
                "steps": [],
            }
        else:
            validation = run_setup_validation(
                openclaw_bin=openclaw_bin,
                config_path=Path(str(report["config_path"])),
                timeout_seconds=DEFAULT_OPENCLAW_JSON_TIMEOUT_SECONDS,
            )
            if not validation.get("ok"):
                exit_code = 1
                report["ok"] = False
    if validation is not None:
        report["validation"] = validation
    return exit_code


def print_setup_report(report: dict[str, object], *, json_output: bool) -> None:
    validation = report.get("validation") if isinstance(report.get("validation"), dict) else None
    if json_output:
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return

    print(report["summary"])
    print(f"config_path: {report['config_path']}")
    print(f"env_file: {report['env_file']}")
    print(f"plugin_root: {report['plugin_root']}")
    print(f"mode: {report['mode']}")
    print(f"profile: {report['requested_profile']} -> {report['effective_profile']}")
    print(f"transport: {report['transport']}")
    if report.get("warnings"):
        print("warnings:")
        for item in report["warnings"]:
            print(f"- {item}")
    if report.get("actions"):
        print("actions:")
        for item in report["actions"]:
            print(f"- {item}")
    if report.get("next_steps"):
        print("next_steps:")
        for item in report["next_steps"]:
            print(f"- {item}")
    if validation is not None:
        print("validation:")
        if validation.get("skipped"):
            print("- skipped: dry_run")
        else:
            for step in validation.get("steps", []):
                summary_suffix = f" ({step['summary']})" if step.get("summary") else ""
                status_label = "pass" if step.get("ok") else "fail"
                print(f"- {step.get('name')}: {status_label}{summary_suffix}")
            if validation.get("failed_step"):
                print(f"failed_step: {validation['failed_step']}")


def command_setup(args: argparse.Namespace) -> int:
    try:
        report = perform_setup_from_namespace(args)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        return print_cli_error(str(exc), json_output=args.json)
    exit_code = attach_setup_validation(
        report=report,
        validate=getattr(args, "validate", False),
        dry_run=args.dry_run,
        openclaw_bin=getattr(args, "openclaw_bin", default_openclaw_bin()),
    )
    print_setup_report(report, json_output=args.json)
    return exit_code
def command_provider_probe(args: argparse.Namespace) -> int:
    try:
        payload = installer.preview_provider_probe_status(
            profile=args.profile,
            mode=args.mode,
            transport=args.transport,
            config=args.config,
            setup_root_value=args.setup_root,
            env_file_value=args.env_file,
            sse_url=getattr(args, "sse_url", None),
            mcp_api_key=getattr(args, "mcp_api_key", None),
            allow_insecure_local=getattr(args, "allow_insecure_local", None),
            embedding_api_base=args.embedding_api_base,
            embedding_api_key=args.embedding_api_key,
            embedding_model=args.embedding_model,
            embedding_dim=args.embedding_dim,
            reranker_api_base=args.reranker_api_base,
            reranker_api_key=args.reranker_api_key,
            reranker_model=args.reranker_model,
            llm_api_base=args.llm_api_base,
            llm_api_key=args.llm_api_key,
            llm_model=args.llm_model,
            write_guard_llm_api_base=args.write_guard_llm_api_base,
            write_guard_llm_api_key=args.write_guard_llm_api_key,
            write_guard_llm_model=args.write_guard_llm_model,
            compact_gist_llm_api_base=args.compact_gist_llm_api_base,
            compact_gist_llm_api_key=args.compact_gist_llm_api_key,
            compact_gist_llm_model=args.compact_gist_llm_model,
            persist=getattr(args, "persist", True),
        )
    except (ValueError, RuntimeError) as exc:
        return print_cli_error(str(exc), json_output=args.json)

    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return 0

    print(str(payload.get("summaryMessage") or "Provider probe completed."))
    print(
        "profile: "
        f"{str(payload.get('requestedProfile') or 'b').upper()} -> "
        f"{str(payload.get('effectiveProfile') or 'b').upper()}"
    )
    print(f"status: {str(payload.get('summaryStatus') or 'unknown')}")
    if payload.get("checkedAt"):
        print(f"checked_at: {payload['checkedAt']}")
    missing_fields = payload.get("missingFields") if isinstance(payload.get("missingFields"), list) else []
    if missing_fields:
        print("missing_fields:")
        for field in missing_fields:
            print(f"- {field}")
    providers = payload.get("providers") if isinstance(payload.get("providers"), dict) else {}
    for component in ("embedding", "reranker", "llm"):
        item = providers.get(component)
        if not isinstance(item, dict):
            continue
        print(f"{component}: {item.get('status', 'unknown')}")
        if item.get("baseUrl"):
            print(f"  base: {item['baseUrl']}")
        if item.get("model"):
            print(f"  model: {item['model']}")
        if item.get("detectedDim"):
            print(f"  detected_dim: {item['detectedDim']}")
        if component == "embedding":
            configured_dim = str(item.get("configuredDim") or "").strip()
            detected_max_dim = str(item.get("detectedMaxDim") or "").strip()
            recommended_dim = str(item.get("recommendedDim") or "").strip()
            if configured_dim or detected_max_dim or recommended_dim:
                print(
                    "  dimensions: "
                    f"configured={configured_dim or '(none)'} | "
                    f"detected_max={detected_max_dim or '(none)'} | "
                    f"recommended={recommended_dim or '(none)'}"
                )
        if item.get("detail"):
            print(f"  detail: {item['detail']}")
    return 0


def _current_env_path_from_args(args: argparse.Namespace) -> Path:
    setup_root = (
        Path(args.setup_root).expanduser().resolve()
        if getattr(args, "setup_root", None)
        else installer.default_setup_root()
    )
    if getattr(args, "env_file", None):
        return Path(str(args.env_file)).expanduser().resolve()
    return installer.default_runtime_env_path(setup_root)


def _load_existing_env_for_onboarding(args: argparse.Namespace) -> dict[str, str]:
    return installer.load_env_file(_current_env_path_from_args(args))


def _hydrate_onboarding_apply_args_from_process_env(args: argparse.Namespace) -> None:
    process_overrides = installer.current_process_runtime_overrides()
    arg_to_override_key = {
        "embedding_api_base": "embedding_api_base",
        "embedding_api_key": "embedding_api_key",
        "embedding_model": "embedding_model",
        "embedding_dim": "embedding_dim",
        "reranker_api_base": "reranker_api_base",
        "reranker_api_key": "reranker_api_key",
        "reranker_model": "reranker_model",
        "llm_api_base": "llm_api_base",
        "llm_api_key": "llm_api_key",
        "llm_model": "llm_model",
        "write_guard_llm_api_base": "write_guard_llm_api_base",
        "write_guard_llm_api_key": "write_guard_llm_api_key",
        "write_guard_llm_model": "write_guard_llm_model",
        "compact_gist_llm_api_base": "compact_gist_llm_api_base",
        "compact_gist_llm_api_key": "compact_gist_llm_api_key",
        "compact_gist_llm_model": "compact_gist_llm_model",
    }
    for arg_name, override_key in arg_to_override_key.items():
        current_value = getattr(args, arg_name, None)
        if str(current_value or "").strip():
            continue
        override_value = process_overrides.get(override_key)
        if str(override_value or "").strip():
            setattr(args, arg_name, str(override_value).strip())


def _localized_onboarding_text(zh: str, en: str) -> str:
    helper = getattr(installer, "_localized_onboarding_text", None)
    if callable(helper):
        return str(helper(zh, en))
    return zh


def _field_payload(field: str, *, required: bool) -> dict[str, object]:
    spec = installer.PROFILE_MANUAL_FIELD_SPECS.get(field, {})
    use_zh = getattr(installer, "cli_language", lambda: "zh")() == "zh"
    return {
        "envKey": field,
        "required": required,
        "label": str(
            (spec.get("label_zh") if use_zh else spec.get("label_en"))
            or spec.get("label_en")
            or spec.get("label_zh")
            or field
        ),
        "hint": str(
            (spec.get("hint_zh") if use_zh else spec.get("hint_en"))
            or spec.get("hint_en")
            or spec.get("hint_zh")
            or ""
        ),
        "example": str(spec.get("example") or ""),
        "secret": bool(spec.get("secret")),
    }


def _provider_input_groups(profile: str) -> list[dict[str, object]]:
    normalized_profile = str(profile or "b").strip().lower() or "b"
    requires_advanced = normalized_profile in {"c", "d"}
    requires_llm = normalized_profile == "d"
    return [
        {
            "id": "embedding",
            "title": _localized_onboarding_text("Embedding Provider", "Embedding Provider"),
            "required": requires_advanced,
            "fields": [
                _field_payload("RETRIEVAL_EMBEDDING_API_BASE", required=requires_advanced),
                _field_payload("RETRIEVAL_EMBEDDING_API_KEY", required=requires_advanced),
                _field_payload("RETRIEVAL_EMBEDDING_MODEL", required=requires_advanced),
                {
                    "envKey": "RETRIEVAL_EMBEDDING_DIM",
                    "required": False,
                    "label": _localized_onboarding_text("Embedding 维度", "Embedding Dimension"),
                    "hint": _localized_onboarding_text(
                        "可先留空，onboarding 会先探测 provider 返回的最大可用维度并建议直接使用它。",
                        "You can leave this blank at first. Onboarding will probe the provider's maximum supported dimension and recommend using it directly.",
                    ),
                    "example": "4096",
                    "secret": False,
                },
            ],
        },
        {
            "id": "reranker",
            "title": _localized_onboarding_text("Reranker Provider", "Reranker Provider"),
            "required": requires_advanced,
            "fields": [
                _field_payload("RETRIEVAL_RERANKER_API_BASE", required=requires_advanced),
                _field_payload("RETRIEVAL_RERANKER_API_KEY", required=requires_advanced),
                _field_payload("RETRIEVAL_RERANKER_MODEL", required=requires_advanced),
            ],
        },
        {
            "id": "llm",
            "title": _localized_onboarding_text("LLM Provider", "LLM Provider"),
            "required": requires_llm,
            "fields": [
                _field_payload("WRITE_GUARD_LLM_API_BASE", required=requires_llm),
                _field_payload("WRITE_GUARD_LLM_API_KEY", required=requires_llm),
                _field_payload("WRITE_GUARD_LLM_MODEL", required=requires_llm),
            ],
            "notes": [
                _localized_onboarding_text(
                    "当前项目对 LLM 走 OpenAI-compatible chat 接口；实际请求面是 /chat/completions。",
                    "This project uses an OpenAI-compatible chat interface for LLMs; the runtime request path is /chat/completions.",
                ),
                _localized_onboarding_text(
                    "如果你给的是 /responses 地址，当前会先归一化成基础 base URL，再继续按 /chat/completions 使用。",
                    "If you provide a /responses URL, it is normalized to the base URL first and the runtime still calls /chat/completions.",
                ),
                _localized_onboarding_text(
                    "Profile B 也可以保留并启用外部 LLM provider；只是检索仍然停留在 hash embedding、无 reranker 的边界。",
                    "Profile B can still keep an external LLM provider enabled, but retrieval remains limited to hash embedding with no reranker.",
                ),
            ],
        },
    ]


def _profile_strategy_payload(requested_profile: str) -> dict[str, object]:
    normalized_profile = str(requested_profile or "b").strip().lower() or "b"
    return {
        "defaultBootstrapProfile": "b",
        "stronglyRecommendedProfilesWhenProvidersReady": ["c", "d"],
        "recommendedLongTermProfile": "c",
        "recommendedRemoteProfile": "d",
        "requestedProfile": normalized_profile,
        "requestedProfileNotes": {
            "a": _localized_onboarding_text(
                "最低配验证档，只建议做最保守检查。",
                "Lowest-capability validation profile. Use it only for the most conservative checks.",
            ),
            "b": _localized_onboarding_text(
                "默认起步档。hash embedding + 无 reranker，先把链路跑通。",
                "Default bootstrap profile. Hash embedding + no reranker, intended to get the chain running first.",
            ),
            "c": _localized_onboarding_text(
                "强烈推荐的长期档位。真实 embedding + reranker，本地/内网模型更常见。",
                "Strongly recommended long-term profile. Real embedding + reranker, commonly hosted locally or on a private network.",
            ),
            "d": _localized_onboarding_text(
                "完整高级面档位。能力强，要求 embedding / reranker / LLM 三类 provider 都准备好。",
                "Full advanced-surface profile. Strong capability, and assumes embedding / reranker / LLM providers are all ready.",
            ),
        }.get(normalized_profile, ""),
        "boundaries": [
            _localized_onboarding_text(
                "先跑通时仍建议从 Profile B 起步；provider 已就绪时不要长期停在 B。",
                "Start from Profile B if you only need a safe bootstrap, but do not stay on B once providers are ready.",
            ),
            _localized_onboarding_text(
                "Profile B 保留 hash embedding / 无 reranker 的检索边界，但如果你显式提供了 LLM，write_guard / compact_context gist 这类可选能力仍可启用。",
                "Profile B keeps the retrieval boundary at hash embedding with no reranker, but optional write_guard / compact_context gist features can still work if you provide an LLM.",
            ),
            _localized_onboarding_text(
                "Profile C/D 才能把真实 embedding + reranker 的检索深度发挥出来；这是当前项目的最大能力路径。",
                "Only Profile C/D unlocks real embedding + reranker depth; that is the maximum-capability path in this project.",
            ),
        ],
    }


def _llm_support_payload(current_env: dict[str, str]) -> dict[str, object]:
    write_guard_enabled = str(current_env.get("WRITE_GUARD_LLM_ENABLED") or "").strip().lower() == "true"
    compact_gist_enabled = str(current_env.get("COMPACT_GIST_LLM_ENABLED") or "").strip().lower() == "true"
    intent_enabled = str(current_env.get("INTENT_LLM_ENABLED") or "").strip().lower() == "true"
    return {
        "openaiCompatibleChat": True,
        "responsesAliasAccepted": True,
        "requestPathUsed": "/chat/completions",
        "acceptedBaseUrlForms": [
            "https://provider.example/v1",
            "https://provider.example/v1/chat/completions",
            "https://provider.example/v1/responses",
        ],
        "currentFlags": {
            "writeGuardEnabled": write_guard_enabled,
            "compactGistEnabled": compact_gist_enabled,
            "intentLlmEnabled": intent_enabled,
        },
        "notes": [
            _localized_onboarding_text(
                "如果只填一套共享 LLM 配置，当前安装器会默认把它复用到 write_guard、compact_context gist，以及 intent routing。",
                "If you provide one shared LLM configuration, the installer now reuses it for write_guard, compact_context gist, and intent routing by default.",
            ),
            _localized_onboarding_text(
                "Profile C 现在会在交互安装里明确询问是否开启这组可选 LLM 辅助；Profile D 则把这组共享 LLM 视为默认高级能力面。",
                "Profile C now explicitly asks whether to enable the optional LLM assist suite during interactive setup; Profile D treats the shared LLM suite as part of its default advanced surface.",
            ),
            _localized_onboarding_text(
                "如果最终按 Profile B 执行 setup，你显式提供的 LLM 配置仍可保留；只是 retrieval 不会升级到真实 embedding + reranker。",
                "If you ultimately apply Profile B, explicitly provided LLM settings can still be retained; retrieval just will not upgrade to real embedding + reranker.",
            ),
        ],
    }


def _build_apply_preview(args: argparse.Namespace) -> dict[str, object]:
    try:
        config_path = (
            resolve_config_path(args.config, openclaw_bin=getattr(args, "openclaw_bin", None))
            if os.name == "nt"
            else None
        )
        env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
            profile=args.profile,
            mode=args.mode,
            transport=args.transport,
            config_path=config_path,
            setup_root_path=(
                Path(args.setup_root).expanduser().resolve()
                if args.setup_root
                else installer.default_setup_root()
            ),
            existing_env=_load_existing_env_for_onboarding(args),
            database_path=args.database_path,
            sse_url=args.sse_url,
            mcp_api_key=args.mcp_api_key,
            allow_insecure_local=args.allow_insecure_local,
            allow_generate_remote_api_key=args.allow_generate_remote_api_key,
            dashboard_host=args.dashboard_host,
            dashboard_port=args.dashboard_port,
            backend_api_host=args.backend_api_host,
            backend_api_port=args.backend_api_port,
            embedding_api_base=args.embedding_api_base,
            embedding_api_key=args.embedding_api_key,
            embedding_model=args.embedding_model,
            embedding_dim=args.embedding_dim,
            reranker_api_base=args.reranker_api_base,
            reranker_api_key=args.reranker_api_key,
            reranker_model=args.reranker_model,
            llm_api_base=args.llm_api_base,
            llm_api_key=args.llm_api_key,
            llm_model=args.llm_model,
            write_guard_llm_api_base=args.write_guard_llm_api_base,
            write_guard_llm_api_key=args.write_guard_llm_api_key,
            write_guard_llm_model=args.write_guard_llm_model,
            compact_gist_llm_api_base=args.compact_gist_llm_api_base,
            compact_gist_llm_api_key=args.compact_gist_llm_api_key,
            compact_gist_llm_model=args.compact_gist_llm_model,
            strict_profile=args.strict_profile,
        )
    except ValueError as exc:
        return {
            "ok": False,
            "status": "blocked",
            "requestedProfile": args.profile,
            "effectiveProfile": None,
            "fallbackApplied": False,
            "strictProfile": bool(args.strict_profile),
            "missingFields": [],
            "warnings": [],
            "error": str(exc),
        }

    current_flags = {
        "writeGuardEnabled": str(env_values.get("WRITE_GUARD_LLM_ENABLED") or "").strip().lower() == "true",
        "compactGistEnabled": str(env_values.get("COMPACT_GIST_LLM_ENABLED") or "").strip().lower() == "true",
        "intentLlmEnabled": str(env_values.get("INTENT_LLM_ENABLED") or "").strip().lower() == "true",
    }
    return {
        "ok": True,
        "status": "fallback" if fallback_applied else "ready",
        "requestedProfile": str(args.profile or "b").strip().lower() or "b",
        "effectiveProfile": effective_profile,
        "fallbackApplied": fallback_applied,
        "strictProfile": bool(args.strict_profile),
        "missingFields": missing_fields,
        "warnings": warnings,
        "currentFlags": current_flags,
    }


def _build_dimension_guidance(provider_probe: dict[str, object], apply_preview: dict[str, object]) -> dict[str, object]:
    providers = provider_probe.get("providers") if isinstance(provider_probe.get("providers"), dict) else {}
    embedding = providers.get("embedding") if isinstance(providers.get("embedding"), dict) else {}
    requested_profile = str(
        apply_preview.get("requestedProfile")
        or provider_probe.get("requestedProfile")
        or "b"
    ).strip().lower() or "b"
    effective_profile = str(
        apply_preview.get("effectiveProfile")
        or provider_probe.get("effectiveProfile")
        or "b"
    ).strip().lower() or "b"
    detected_dim = str(
        embedding.get("detectedMaxDim") or embedding.get("detectedDim") or ""
    ).strip() or None
    recommended_dim = str(
        embedding.get("recommendedDim") or detected_dim or ""
    ).strip() or None
    requires_providers = bool(provider_probe.get("requiresProviders"))
    if effective_profile == "a":
        return {
            "envKey": "RETRIEVAL_EMBEDDING_DIM",
            "detectedMaxDimension": None,
            "recommendedDimension": None,
            "willWriteRecommendedDimension": False,
            "summary": _localized_onboarding_text(
                "当前是 Profile A；它不启用 embedding，因而不存在需要探测或写入的 embedding 维度。",
                "The current effective profile is A. It does not enable embeddings, so there is no embedding dimension to probe or write.",
            ),
        }
    if requested_profile == "b" and not requires_providers and effective_profile == "b":
        return {
            "envKey": "RETRIEVAL_EMBEDDING_DIM",
            "detectedMaxDimension": "64",
            "recommendedDimension": "64",
            "willWriteRecommendedDimension": True,
            "summary": _localized_onboarding_text(
                "当前是 Profile B；它使用固定的本地 hash embedding 维度 64，而不是外部 embedding provider 探测值。",
                "The current effective profile is B. It uses a fixed local hash embedding dimension of 64, not a probed external embedding-provider value.",
            ),
        }
    if detected_dim:
        summary = _localized_onboarding_text(
            f"已探测到 embedding provider 返回的最大可用维度为 {detected_dim}。 建议直接把 RETRIEVAL_EMBEDDING_DIM 设为 {recommended_dim or detected_dim}；setup 应用时会按这个值写入。",
            f"The embedding provider reports a maximum usable dimension of {detected_dim}. Set RETRIEVAL_EMBEDDING_DIM to {recommended_dim or detected_dim}; setup will write that value when applied.",
        )
    else:
        summary = _localized_onboarding_text(
            "当前还没有拿到 embedding 维度探测结果。通常是因为 embedding provider 还没配齐，或者探活本身未通过。",
            "No embedding-dimension probe result is available yet. Usually that means the embedding provider is still incomplete or the probe itself did not pass.",
        )
    return {
        "envKey": "RETRIEVAL_EMBEDDING_DIM",
        "detectedMaxDimension": detected_dim,
        "recommendedDimension": recommended_dim,
        "willWriteRecommendedDimension": bool(recommended_dim and apply_preview.get("ok")),
        "summary": summary,
    }


def _derive_predicted_apply_result(
    *,
    requested_profile: str,
    apply_preview: dict[str, object],
    provider_probe: dict[str, object],
) -> dict[str, object]:
    normalized_requested = str(requested_profile or "b").strip().lower() or "b"
    predicted = dict(apply_preview)
    if not predicted.get("ok"):
        return predicted
    if normalized_requested not in {"c", "d"}:
        return predicted

    providers = provider_probe.get("providers") if isinstance(provider_probe.get("providers"), dict) else {}
    failed_components = [
        component
        for component in ("embedding", "reranker", "llm")
        if isinstance(providers.get(component), dict)
        and str(providers[component].get("status") or "").strip().lower() == "fail"
    ]
    if normalized_requested == "c":
        required_failures = [component for component in failed_components if component != "llm"]
        optional_llm_failed = "llm" in failed_components
        if optional_llm_failed and not required_failures:
            current_flags = predicted.get("currentFlags")
            if isinstance(current_flags, dict):
                current_flags["writeGuardEnabled"] = False
                current_flags["compactGistEnabled"] = False
                current_flags["intentLlmEnabled"] = False
            predicted["predictedReason"] = "optional_llm_probe_failed"
            predicted["warnings"] = installer.dedupe_keep_order([
                *[
                    str(item)
                    for item in (predicted.get("warnings") or [])
                    if isinstance(item, str)
                ],
                _localized_onboarding_text(
                    "可选 LLM 探活失败时，setup 仍会保留 Profile C，但会先关闭 write guard / compact gist / intent LLM 增强链路。",
                    "When the optional LLM probe fails, setup still keeps Profile C but disables the write-guard / compact-gist / intent-LLM assist chain first.",
                ),
            ])
            return predicted
        failed_components = required_failures
    if failed_components and not bool(predicted.get("strictProfile")):
        predicted["status"] = "fallback"
        predicted["effectiveProfile"] = "b"
        predicted["fallbackApplied"] = True
        predicted["predictedReason"] = (
            "provider_probe_failed:" + ",".join(failed_components)
        )
        predicted["warnings"] = installer.dedupe_keep_order([
            *[
                str(item)
                for item in (predicted.get("warnings") or [])
                if isinstance(item, str)
            ],
            _localized_onboarding_text(
                "这些 C/D provider 当前探活失败；如果现在 apply，安装会临时回退到 Profile B。",
                "One or more C/D providers are currently failing health checks. If you apply now, setup will temporarily fall back to Profile B.",
            ),
        ])
    elif failed_components:
        predicted["status"] = "blocked"
        predicted["ok"] = False
        predicted["predictedReason"] = (
            "provider_probe_failed_strict:" + ",".join(failed_components)
        )
        predicted["error"] = _localized_onboarding_text(
            "Strict profile 已开启；这些 C/D provider 探活失败时不会回退到 B，而是直接报错。",
            "Strict profile is enabled. These failing C/D providers will not fall back to B; setup will stop with an error instead.",
        )
    return predicted


def _build_onboarding_next_steps(
    *,
    requested_profile: str,
    predicted_apply: dict[str, object],
    provider_probe: dict[str, object],
    dimension_guidance: dict[str, object],
) -> list[str]:
    steps: list[str] = []
    missing_fields = predicted_apply.get("missingFields") if isinstance(predicted_apply.get("missingFields"), list) else []
    if missing_fields:
        if str(requested_profile or "b").strip().lower() == "c":
            zh_missing = "先补齐 Profile C 必需的 embedding 和 reranker 字段。"
            en_missing = "Fill in the required embedding and reranker fields for Profile C first."
        else:
            zh_missing = "先补齐 Profile D 必需的 embedding、reranker 和 LLM 字段。"
            en_missing = "Fill in the required embedding, reranker, and LLM fields for Profile D first."
        steps.append(_localized_onboarding_text(
            zh_missing,
            en_missing,
        ))
    providers = provider_probe.get("providers") if isinstance(provider_probe.get("providers"), dict) else {}
    for component in ("embedding", "reranker", "llm"):
        item = providers.get(component)
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").strip().lower() == "fail":
            steps.append(
                _localized_onboarding_text(
                    f"先修复 {component} provider 的地址、密钥、模型名或连通性，再重新执行 onboarding 预检。",
                    f"Fix the {component} provider base URL, key, model name, or connectivity first, then rerun onboarding preview.",
                )
            )
    if dimension_guidance.get("detectedMaxDimension"):
        steps.append(
            _localized_onboarding_text(
                f"把 RETRIEVAL_EMBEDDING_DIM 固定为 {dimension_guidance['detectedMaxDimension']}。",
                f"Pin RETRIEVAL_EMBEDDING_DIM to {dimension_guidance['detectedMaxDimension']}.",
            )
        )
    if str(requested_profile or "b").strip().lower() == "b":
        steps.append(_localized_onboarding_text(
            "如果只是先跑通链路，现在可先保持 Profile B；provider 就绪后尽快升级到 C/D。",
            "If you only need the chain to start working first, stay on Profile B for now and upgrade to C/D once providers are ready.",
        ))
        steps.append(_localized_onboarding_text(
            "Profile B 下如已配置 LLM，write_guard / compact_context gist 仍可启用，但检索边界仍是 hash embedding、无 reranker。",
            "If an LLM is configured under Profile B, write_guard / compact_context gist can still stay enabled, but retrieval remains limited to hash embedding with no reranker.",
        ))
    else:
        steps.append(_localized_onboarding_text(
            "Profile C/D 是当前项目的最大能力路径；provider 全绿后再做最终签收。",
            "Profile C/D is the maximum-capability path in this project; wait for all provider checks to go green before final signoff.",
        ))
    return installer.dedupe_keep_order(steps)


def _print_onboarding_report(payload: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
        return

    print(str(payload.get("summary") or _localized_onboarding_text(
        "Onboarding 预检已完成。",
        "Onboarding readiness computed.",
    )))
    apply_prediction = payload.get("predictedApply") if isinstance(payload.get("predictedApply"), dict) else {}
    profile_strategy = payload.get("profileStrategy") if isinstance(payload.get("profileStrategy"), dict) else {}
    provider_probe = payload.get("providerProbe") if isinstance(payload.get("providerProbe"), dict) else {}
    dimension_guidance = payload.get("embeddingDimension") if isinstance(payload.get("embeddingDimension"), dict) else {}

    print(
        "profile_strategy: "
        f"default={profile_strategy.get('defaultBootstrapProfile', 'b').upper()} | "
        f"long_term={profile_strategy.get('recommendedLongTermProfile', 'c').upper()} | "
        f"remote={profile_strategy.get('recommendedRemoteProfile', 'd').upper()}"
    )
    print(
        "predicted_apply: "
        f"{str(apply_prediction.get('requestedProfile') or 'b').upper()} -> "
        f"{str(apply_prediction.get('effectiveProfile') or 'n/a').upper()} | "
        f"status={apply_prediction.get('status', 'unknown')}"
    )
    if provider_probe:
        print(f"provider_probe: {provider_probe.get('summaryStatus', 'unknown')}")
        if provider_probe.get("summaryMessage"):
            print(f"  summary: {provider_probe['summaryMessage']}")
    if dimension_guidance.get("summary"):
        print(f"embedding_dimension: {dimension_guidance['summary']}")
    next_steps = payload.get("nextSteps") if isinstance(payload.get("nextSteps"), list) else []
    if next_steps:
        print("next_steps:")
        for item in next_steps:
            print(f"- {item}")
    applied_setup = payload.get("appliedSetup") if isinstance(payload.get("appliedSetup"), dict) else None
    if applied_setup:
        print(f"applied_setup: {applied_setup.get('summary', '')}")


def command_onboarding(args: argparse.Namespace) -> int:
    apply_preview = _build_apply_preview(args)
    try:
        provider_probe = installer.preview_provider_probe_status(
            profile=args.profile,
            mode=args.mode,
            transport=args.transport,
            config=args.config,
            setup_root_value=args.setup_root,
            env_file_value=args.env_file,
            sse_url=args.sse_url,
            mcp_api_key=args.mcp_api_key,
            allow_insecure_local=args.allow_insecure_local,
            embedding_api_base=args.embedding_api_base,
            embedding_api_key=args.embedding_api_key,
            embedding_model=args.embedding_model,
            embedding_dim=args.embedding_dim,
            reranker_api_base=args.reranker_api_base,
            reranker_api_key=args.reranker_api_key,
            reranker_model=args.reranker_model,
            llm_api_base=args.llm_api_base,
            llm_api_key=args.llm_api_key,
            llm_model=args.llm_model,
            write_guard_llm_api_base=args.write_guard_llm_api_base,
            write_guard_llm_api_key=args.write_guard_llm_api_key,
            write_guard_llm_model=args.write_guard_llm_model,
            compact_gist_llm_api_base=args.compact_gist_llm_api_base,
            compact_gist_llm_api_key=args.compact_gist_llm_api_key,
            compact_gist_llm_model=args.compact_gist_llm_model,
            persist=False,
            dry_run=args.dry_run,
        )
    except (ValueError, RuntimeError) as exc:
        return print_cli_error(str(exc), json_output=args.json)

    predicted_apply = _derive_predicted_apply_result(
        requested_profile=args.profile,
        apply_preview=apply_preview,
        provider_probe=provider_probe,
    )
    existing_env = _load_existing_env_for_onboarding(args)
    llm_preview_env = {}
    if isinstance(predicted_apply, dict) and predicted_apply.get("ok"):
        try:
            config_path = (
                resolve_config_path(args.config, openclaw_bin=getattr(args, "openclaw_bin", None))
                if os.name == "nt"
                else None
            )
            llm_preview_env, _, _, _, _ = installer.apply_setup_defaults(
                profile=args.profile,
                mode=args.mode,
                transport=args.transport,
                config_path=config_path,
                setup_root_path=(
                    Path(args.setup_root).expanduser().resolve()
                    if args.setup_root
                    else installer.default_setup_root()
                ),
                existing_env=existing_env,
                database_path=args.database_path,
                sse_url=args.sse_url,
                mcp_api_key=args.mcp_api_key,
                allow_insecure_local=args.allow_insecure_local,
                allow_generate_remote_api_key=args.allow_generate_remote_api_key,
                dashboard_host=args.dashboard_host,
                dashboard_port=args.dashboard_port,
                backend_api_host=args.backend_api_host,
                backend_api_port=args.backend_api_port,
                embedding_api_base=args.embedding_api_base,
                embedding_api_key=args.embedding_api_key,
                embedding_model=args.embedding_model,
                embedding_dim=args.embedding_dim,
                reranker_api_base=args.reranker_api_base,
                reranker_api_key=args.reranker_api_key,
                reranker_model=args.reranker_model,
                llm_api_base=args.llm_api_base,
                llm_api_key=args.llm_api_key,
                llm_model=args.llm_model,
                write_guard_llm_api_base=args.write_guard_llm_api_base,
                write_guard_llm_api_key=args.write_guard_llm_api_key,
                write_guard_llm_model=args.write_guard_llm_model,
                compact_gist_llm_api_base=args.compact_gist_llm_api_base,
                compact_gist_llm_api_key=args.compact_gist_llm_api_key,
                compact_gist_llm_model=args.compact_gist_llm_model,
                strict_profile=False,
            )
        except ValueError:
            llm_preview_env = existing_env
    dimension_guidance = _build_dimension_guidance(provider_probe, predicted_apply)
    payload: dict[str, object] = {
        "ok": True,
        "summary": _localized_onboarding_text(
            "Onboarding 预检已完成。请用这份结果继续和用户对话，先补齐缺失字段、先做 provider probe，再决定是否 apply setup。",
            "Onboarding readiness is available. Use this report to continue the conversation, collect missing values, probe providers first, and only then apply setup.",
        ),
        "requestedProfile": str(args.profile or "b").strip().lower() or "b",
        "mode": args.mode,
        "transport": args.transport,
        "profileStrategy": _profile_strategy_payload(args.profile),
        "providerInputs": _provider_input_groups(args.profile),
        "llmSupport": _llm_support_payload(llm_preview_env or existing_env),
        "predictedApply": predicted_apply,
        "providerProbe": provider_probe,
        "embeddingDimension": dimension_guidance,
        "installGuidance": installer.build_install_guidance(),
        "reindexGate": installer.build_reindex_gate(
            existing_env=existing_env,
            preview_env=llm_preview_env,
        ),
    }
    payload["nextSteps"] = _build_onboarding_next_steps(
        requested_profile=args.profile,
        predicted_apply=predicted_apply,
        provider_probe=provider_probe,
        dimension_guidance=dimension_guidance,
    )
    if not bool(predicted_apply.get("ok", True)):
        payload["ok"] = False
        payload["summary"] = _localized_onboarding_text(
            "Onboarding 预检已完成，但按当前参数继续 apply 会被阻塞；请先修复 predictedApply 里的错误或缺口。",
            "Onboarding readiness is available, but apply would currently be blocked. Fix the error or missing inputs recorded in predictedApply first.",
        )
        if bool(args.strict_profile):
            exit_code = 1
        else:
            exit_code = 0
    else:
        exit_code = 0
    if args.apply and not bool(predicted_apply.get("ok", True)):
        payload["ok"] = False
        payload["summary"] = _localized_onboarding_text(
            "Onboarding apply 已被阻断；请先修复 predictedApply 里的错误或缺口，再重新执行 apply。",
            "Onboarding apply was blocked. Fix the error or missing inputs recorded in predictedApply first, then rerun apply.",
        )
        _print_onboarding_report(payload, json_output=args.json)
        return 1
    if args.apply:
        _hydrate_onboarding_apply_args_from_process_env(args)
        recommended_dim = str(dimension_guidance.get("recommendedDimension") or "").strip()
        if recommended_dim and not str(args.embedding_dim or "").strip():
            args.embedding_dim = recommended_dim
        try:
            setup_report = perform_setup_from_namespace(args)
        except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            return print_cli_error(str(exc), json_output=args.json)
        exit_code = attach_setup_validation(
            report=setup_report,
            validate=getattr(args, "validate", False),
            dry_run=args.dry_run,
            openclaw_bin=getattr(args, "openclaw_bin", default_openclaw_bin()),
        )
        payload["appliedSetup"] = setup_report
        if not bool(setup_report.get("ok", True)):
            payload["ok"] = False
            payload["summary"] = _localized_onboarding_text(
                "Onboarding apply 已返回结构化结果，但最终 setup/validation 失败；请先修复 appliedSetup.validation 后再签收。",
                "Onboarding apply returned a structured payload, but the final setup/validation failed. Fix appliedSetup.validation before signoff.",
            )

    _print_onboarding_report(payload, json_output=args.json)
    return exit_code


def command_uninstall(args: argparse.Namespace) -> int:
    try:
        report = installer.perform_uninstall(
            config=args.config,
            setup_root_value=args.setup_root,
            openclaw_bin=args.openclaw_bin,
            keep_files=args.keep_files,
            remove_runtime=args.purge_runtime,
            force=args.force,
            dry_run=args.dry_run,
        )
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        return print_cli_error(str(exc), json_output=args.json)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(report["summary"])
        print(f"config_path: {report['config_path']}")
        print(f"setup_root: {report['setup_root']}")
        if report.get("warnings"):
            print("warnings:")
            for item in report["warnings"]:
                print(f"- {item}")
        if report.get("actions"):
            print("actions:")
            for item in report["actions"]:
                print(f"- {item}")
    return 0


def command_enable(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config, openclaw_bin=args.openclaw_bin)
    return run_process([args.openclaw_bin, "plugins", "enable", PLUGIN_ID], config_path=config_path)


def command_disable(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config, openclaw_bin=args.openclaw_bin)
    return run_process([args.openclaw_bin, "plugins", "disable", PLUGIN_ID], config_path=config_path)


def command_dashboard(args: argparse.Namespace) -> int:
    try:
        if args.dashboard_command == "status":
            report = installer.dashboard_status(setup_root_value=args.setup_root)
        elif args.dashboard_command == "start":
            report = installer.dashboard_start(
                setup_root_value=args.setup_root,
                dashboard_host=args.dashboard_host,
                dashboard_port=args.dashboard_port,
                backend_api_host=args.backend_api_host,
                backend_api_port=args.backend_api_port,
                dry_run=args.dry_run,
            )
        elif args.dashboard_command == "stop":
            report = installer.dashboard_stop(
                setup_root_value=args.setup_root,
                dry_run=args.dry_run,
            )
        else:
            return print_cli_error(f"Unsupported dashboard command: {args.dashboard_command}", json_output=getattr(args, "json", False))
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        return print_cli_error(str(exc), json_output=getattr(args, "json", False))

    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(report["summary"])
        if report.get("dashboard"):
            print(json.dumps(report["dashboard"], ensure_ascii=True, indent=2))
        if report.get("backendApi"):
            print(json.dumps(report["backendApi"], ensure_ascii=True, indent=2))
        if report.get("warnings"):
            print("warnings:")
            for item in report["warnings"]:
                print(f"- {item}")
        if report.get("actions"):
            print("actions:")
            for item in report["actions"]:
                print(f"- {item}")
    return 0


def command_stage_package() -> int:
    report = installer.stage_release_package()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0


def command_migrate(args: argparse.Namespace) -> int:
    try:
        report = installer.perform_migrate(
            config=args.config,
            setup_root_value=args.setup_root,
            env_file_value=args.env_file,
            database_url=args.database_url,
            migrations_dir_value=args.migrations_dir,
            lock_file_value=args.lock_file,
            lock_timeout_seconds=args.lock_timeout_sec,
            dry_run=args.dry_run,
        )
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        return print_cli_error(str(exc), json_output=args.json)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(report["summary"])
        print(f"env_file: {report['env_file']}")
        print(f"database_file: {report['database_file']}")
        print(f"runtime_python: {report['runtime_python']}")
        print(f"current_versions: {', '.join(report.get('current_versions') or []) or '(none)'}")
        print(f"applied_versions: {', '.join(report.get('applied_versions') or []) or '(none)'}")
        if report.get("warnings"):
            print("warnings:")
            for item in report["warnings"]:
                print(f"- {item}")
        if report.get("actions"):
            print("actions:")
            for item in report["actions"]:
                print(f"- {item}")
        if report.get("next_steps"):
            print("next_steps:")
            for item in report["next_steps"]:
                print(f"- {item}")
    return 0


def command_upgrade(args: argparse.Namespace) -> int:
    try:
        report = installer.perform_upgrade(
            config=args.config,
            setup_root_value=args.setup_root,
            env_file_value=args.env_file,
            strict_profile=args.strict_profile,
            dry_run=args.dry_run,
        )
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        return print_cli_error(str(exc), json_output=args.json)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        print(report["summary"])
        print(f"config_path: {report['config_path']}")
        print(f"env_file: {report['env_file']}")
        print(f"mode: {report['mode']}")
        print(f"profile: {report['requested_profile']} -> {report['effective_profile']}")
        print(f"transport: {report['transport']}")
        migrate_report = report.get("migrate") if isinstance(report.get("migrate"), dict) else {}
        print(
            "migrate_applied_versions: "
            f"{', '.join(migrate_report.get('applied_versions') or []) or '(none)'}"
        )
        if report.get("warnings"):
            print("warnings:")
            for item in report["warnings"]:
                print(f"- {item}")
        if report.get("actions"):
            print("actions:")
            for item in report["actions"]:
                print(f"- {item}")
        if report.get("next_steps"):
            print("next_steps:")
            for item in report["next_steps"]:
                print(f"- {item}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config, openclaw_bin=args.openclaw_bin)
    command = [args.openclaw_bin, PLUGIN_ID, "verify"]
    if args.json:
        command.append("--json")
    return run_process(command, config_path=config_path)


def command_doctor(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config, openclaw_bin=args.openclaw_bin)
    command = [args.openclaw_bin, PLUGIN_ID, "doctor"]
    if args.query:
        command.extend(["--query", args.query])
    if args.json:
        command.append("--json")
    return run_process(command, config_path=config_path)


def command_smoke(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config, openclaw_bin=args.openclaw_bin)
    command = [args.openclaw_bin, PLUGIN_ID, "smoke"]
    if args.query:
        command.extend(["--query", args.query])
    if args.path_or_uri:
        command.extend(["--path-or-uri", args.path_or_uri])
    if args.expect_hit:
        command.append("--expect-hit")
    if args.json:
        command.append("--json")
    return run_process(command, config_path=config_path)


def command_bootstrap_status(args: argparse.Namespace) -> int:
    try:
        payload = installer.bootstrap_status(
            config=args.config,
            setup_root_value=args.setup_root,
            env_file_value=args.env_file,
        )
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        return print_cli_error(str(exc), json_output=args.json)
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2))
    else:
        setup = payload.get("setup") if isinstance(payload.get("setup"), dict) else {}
        print(str(payload.get("summary") or "Bootstrap status loaded."))
        print(f"config_path: {payload.get('configPath') or '(unknown)'}")
        print(f"env_file: {setup.get('envFile') or '(unknown)'}")
        print(
            "profile: "
            f"{setup.get('requestedProfile') or 'b'} -> {setup.get('effectiveProfile') or 'b'}"
        )
        if payload.get("warnings"):
            print("warnings:")
            for item in payload["warnings"]:
                print(f"- {item}")
        if payload.get("checks"):
            print("checks:")
            for item in payload["checks"]:
                print(
                    f"- {item.get('id', 'unknown')}: {item.get('status', 'UNKNOWN')}"
                    f" | {item.get('message', '')}"
                )
    return 0

def command_benchmark(args: argparse.Namespace) -> int:
    forwarded = [sys.executable, str(VISUAL_BENCHMARK_SCRIPT)]
    for name in (
        "profile",
        "profiles",
        "model_env",
        "case_count",
        "case_limit",
        "max_workers",
        "required_coverage",
        "json_output",
        "markdown_output",
    ):
        value = getattr(args, name)
        if value is None:
            continue
        flag = f"--{name.replace('_', '-')}"
        forwarded.extend([flag, str(value)])
    if getattr(args, "resume", False):
        forwarded.append("--resume")
    return run_process(forwarded)


def command_release_gate(args: argparse.Namespace) -> int:
    if getattr(args, "enable_current_host_strict_ui", False) and getattr(args, "skip_current_host_strict_ui", False):
        return print_cli_error(
            "Cannot combine --enable-current-host-strict-ui with --skip-current-host-strict-ui.",
            json_output=False,
        )

    legacy_bash_gate = bool(args.legacy_bash_gate)
    if legacy_bash_gate and getattr(args, "enable_windows_native_validation", False):
        return print_cli_error(
            "legacy-bash-gate does not support --enable-windows-native-validation; use the Python checkpoint release gate on a real Windows host.",
            json_output=False,
        )
    if legacy_bash_gate:
        if not shutil.which("bash"):
            return print_cli_error(
                "legacy-bash-gate requires bash, but bash is unavailable on this host.",
                json_output=False,
            )
        forwarded = ["bash", bash_script_path(PRE_PUBLISH_SCRIPT), "--release-gate"]
    else:
        forwarded = [sys.executable, str(RELEASE_GATE_RUNNER_SCRIPT)]
    if args.report:
        forwarded.extend(["--report", args.report])
    if args.skip_backend_tests:
        forwarded.append("--skip-backend-tests")
    if args.skip_plugin_tests:
        forwarded.append("--skip-plugin-tests")
    if getattr(args, "enable_live_benchmark", False):
        forwarded.append("--enable-live-benchmark")
    if not legacy_bash_gate and getattr(args, "enable_windows_native_validation", False):
        forwarded.append("--enable-windows-native-validation")
    if getattr(args, "skip_onboarding_apply_validate", False):
        forwarded.append("--skip-onboarding-apply-validate")
    if not legacy_bash_gate and getattr(args, "skip_python_matrix", False):
        forwarded.append("--skip-python-matrix")
    if args.skip_frontend:
        forwarded.append("--skip-frontend-tests")
    if args.skip_frontend_e2e:
        forwarded.append("--skip-frontend-e2e")
    if args.skip_profile_smoke:
        forwarded.append("--skip-profile-smoke")
    if getattr(args, "skip_phase45", False):
        forwarded.append("--skip-phase45")
    if args.skip_review_smoke:
        forwarded.append("--skip-review-smoke")
    if getattr(args, "enable_current_host_strict_ui", False):
        forwarded.append("--enable-current-host-strict-ui")
    if getattr(args, "skip_current_host_strict_ui", False):
        forwarded.append("--skip-current-host-strict-ui")
    if not legacy_bash_gate and getattr(args, "current_host_ui_profile", None):
        forwarded.extend(["--current-host-ui-profile", args.current_host_ui_profile])
    if not legacy_bash_gate and getattr(args, "current_host_ui_url", None):
        forwarded.extend(["--current-host-ui-url", args.current_host_ui_url])
    if args.profile_modes:
        forwarded.extend(["--profile-smoke-modes", args.profile_modes])
    if not legacy_bash_gate and getattr(args, "phase45_profiles", None):
        forwarded.extend(["--phase45-profiles", args.phase45_profiles])
    if args.review_smoke_modes:
        forwarded.extend(["--review-smoke-modes", args.review_smoke_modes])
    if args.profile_smoke_model_env:
        try:
            model_env_path = validate_readable_file_arg(
                args.profile_smoke_model_env,
                label="--profile-smoke-model-env",
            )
        except ValueError as exc:
            return print_cli_error(str(exc), json_output=False)
        forwarded.extend(["--profile-smoke-model-env", model_env_path])
    if not legacy_bash_gate and getattr(args, "checkpoint_dir", None):
        forwarded.extend(["--checkpoint-dir", args.checkpoint_dir])
    if not legacy_bash_gate and getattr(args, "resume", False):
        forwarded.append("--resume")
    return run_process(forwarded)


def main() -> int:
    args = parse_args()
    if args.command == "install":
        return command_install(args)
    if args.command == "setup":
        return command_setup(args)
    if args.command == "onboarding":
        return command_onboarding(args)
    if args.command == "provider-probe":
        return command_provider_probe(args)
    if args.command == "uninstall":
        return command_uninstall(args)
    if args.command == "enable":
        return command_enable(args)
    if args.command == "disable":
        return command_disable(args)
    if args.command == "dashboard":
        return command_dashboard(args)
    if args.command == "stage-package":
        return command_stage_package()
    if args.command == "migrate":
        return command_migrate(args)
    if args.command == "upgrade":
        return command_upgrade(args)
    if args.command == "verify":
        return command_verify(args)
    if args.command == "doctor":
        return command_doctor(args)
    if args.command == "smoke":
        return command_smoke(args)
    if args.command == "bootstrap-status":
        return command_bootstrap_status(args)
    if args.command == "benchmark":
        return command_benchmark(args)
    if args.command == "release-gate":
        return command_release_gate(args)
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
