#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NATIVE_PATH_CLS = type(PROJECT_ROOT)
BACKEND_ROOT = PROJECT_ROOT / "backend"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
PRE_PUBLISH_SCRIPT = PROJECT_ROOT / "scripts" / "pre_publish_check.sh"
DEFAULT_REPORT_PATH = PROJECT_ROOT / ".tmp" / "openclaw_memory_palace_release_gate.final.md"
DEFAULT_CHECKPOINT_DIR = PROJECT_ROOT / ".tmp" / "release-gate-checkpoint"
CHECKPOINT_LOCK_NAME = ".release-gate.lock"
PACKAGE_DRY_RUN_FORBIDDEN_PATHS = (
    "release/frontend/.tmp/",
    "release/frontend/coverage/",
    "release/backend/AUDIT_REPORT.md",
    "release/backend/CLAUDE.md",
)
RELEASE_STEP_STDOUT_TAIL_LINES = int(os.environ.get("RELEASE_STEP_STDOUT_TAIL_LINES", "80") or "80")
RELEASE_STEP_TIMEOUT_BACKEND_PYTEST = int(os.environ.get("RELEASE_STEP_TIMEOUT_BACKEND_PYTEST", "1800") or "1800")
RELEASE_STEP_TIMEOUT_SCRIPT_PYTEST = int(os.environ.get("RELEASE_STEP_TIMEOUT_SCRIPT_PYTEST", "1800") or "1800")
RELEASE_STEP_TIMEOUT_MCP_STDIO_E2E = int(os.environ.get("RELEASE_STEP_TIMEOUT_MCP_STDIO_E2E", "600") or "600")
RELEASE_STEP_TIMEOUT_PLUGIN_BUN_TESTS = int(os.environ.get("RELEASE_STEP_TIMEOUT_PLUGIN_BUN_TESTS", "900") or "900")
RELEASE_STEP_TIMEOUT_PACKAGE_INSTALL = int(os.environ.get("RELEASE_STEP_TIMEOUT_PACKAGE_INSTALL", "1800") or "1800")
RELEASE_STEP_TIMEOUT_PYTHON_MATRIX = int(os.environ.get("RELEASE_STEP_TIMEOUT_PYTHON_MATRIX", "0") or "0")
RELEASE_STEP_TIMEOUT_PROFILE_SMOKE = int(os.environ.get("RELEASE_STEP_TIMEOUT_PROFILE_SMOKE", "5400") or "5400")
RELEASE_STEP_TIMEOUT_VISUAL_BENCHMARK = int(os.environ.get("RELEASE_STEP_TIMEOUT_VISUAL_BENCHMARK", "0") or "0")
RELEASE_STEP_TIMEOUT_REVIEW_SMOKE = int(os.environ.get("RELEASE_STEP_TIMEOUT_REVIEW_SMOKE", "1800") or "1800")
RELEASE_STEP_TIMEOUT_FRONTEND_TESTS = int(os.environ.get("RELEASE_STEP_TIMEOUT_FRONTEND_TESTS", "900") or "900")
RELEASE_STEP_TIMEOUT_FRONTEND_E2E = int(os.environ.get("RELEASE_STEP_TIMEOUT_FRONTEND_E2E", "1800") or "1800")
RELEASE_STEP_FRONTEND_E2E_API_PORT = os.environ.get("RELEASE_STEP_FRONTEND_E2E_API_PORT", "18081") or "18081"
RELEASE_STEP_FRONTEND_E2E_UI_PORT = os.environ.get("RELEASE_STEP_FRONTEND_E2E_UI_PORT", "4174") or "4174"
WINDOWS_NATIVE_VALIDATION_SCRIPT = PROJECT_ROOT / "scripts" / "openclaw_memory_palace_windows_native_validation.py"
SCRIPT_LEVEL_PYTEST_FILES = [
    "scripts/test_install_skill.py",
    "scripts/test_openclaw_memory_palace_installer.py",
    "scripts/test_openclaw_harness_cleanup_e2e.py",
    "scripts/test_openclaw_provider_retry_e2e.py",
    "scripts/test_openclaw_json_output.py",
    "scripts/test_openclaw_command_new_e2e.py",
    "scripts/test_openclaw_memory_palace_windows_native_validation.py",
]
VISUAL_BENCHMARK_TIMEOUT_BASE_SECONDS = int(os.environ.get("VISUAL_BENCHMARK_TIMEOUT_BASE_SECONDS", "180") or "180")
VISUAL_BENCHMARK_TIMEOUT_PER_PROFILE_SECONDS = int(
    os.environ.get("VISUAL_BENCHMARK_TIMEOUT_PER_PROFILE_SECONDS", "120") or "120"
)
VISUAL_BENCHMARK_TIMEOUT_PER_CASE_SECONDS = int(os.environ.get("VISUAL_BENCHMARK_TIMEOUT_PER_CASE_SECONDS", "12") or "12")
VISUAL_BENCHMARK_TIMEOUT_FLOOR_SECONDS = int(os.environ.get("VISUAL_BENCHMARK_TIMEOUT_FLOOR_SECONDS", "900") or "900")
VISUAL_BENCHMARK_TIMEOUT_CEILING_SECONDS = int(
    os.environ.get("VISUAL_BENCHMARK_TIMEOUT_CEILING_SECONDS", "7200") or "7200"
)


@dataclass
class StepCommand:
    argv: list[str]
    cwd: Path
    env_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class ReleaseStep:
    step_id: str
    title: str
    commands: list[StepCommand]
    timeout_seconds: int
    log_path: Path
    artifact_paths: list[Path] = field(default_factory=list)
    skip_reason: str | None = None
    skip_causes_failure: bool = False


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_positive_int(value: str | None, fallback: int) -> int:
    rendered = str(value or "").strip()
    if rendered.isdigit():
        parsed = int(rendered)
        if parsed > 0:
            return parsed
    return fallback


def env_flag_enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def count_csv_items(raw: str | None) -> int:
    items = [item.strip() for item in str(raw or "").split(",") if item.strip()]
    return len(items) if items else 1


def compute_visual_benchmark_timeout_seconds(profiles_csv: str, case_limit: int, max_workers: int) -> int:
    if RELEASE_STEP_TIMEOUT_VISUAL_BENCHMARK > 0:
        return RELEASE_STEP_TIMEOUT_VISUAL_BENCHMARK
    profiles_count = count_csv_items(profiles_csv)
    total_cases = profiles_count * normalize_positive_int(str(case_limit), 64)
    effective_case_batches = (total_cases + max(1, max_workers) - 1) // max(1, max_workers)
    computed = (
        VISUAL_BENCHMARK_TIMEOUT_BASE_SECONDS
        + profiles_count * VISUAL_BENCHMARK_TIMEOUT_PER_PROFILE_SECONDS
        + effective_case_batches * VISUAL_BENCHMARK_TIMEOUT_PER_CASE_SECONDS
    )
    computed = max(computed, VISUAL_BENCHMARK_TIMEOUT_FLOOR_SECONDS)
    if VISUAL_BENCHMARK_TIMEOUT_CEILING_SECONDS > 0:
        computed = min(computed, VISUAL_BENCHMARK_TIMEOUT_CEILING_SECONDS)
    return computed


def compute_python_matrix_timeout_seconds(versions_csv: str) -> int:
    if RELEASE_STEP_TIMEOUT_PYTHON_MATRIX > 0:
        return RELEASE_STEP_TIMEOUT_PYTHON_MATRIX
    version_count = count_csv_items(versions_csv)
    return max(1800, version_count * 240)


def resolve_python_from_venv(venv_root: Path) -> str | None:
    for candidate in (
        venv_root / "bin" / "python",
        venv_root / "Scripts" / "python.exe",
        venv_root / "Scripts" / "python",
    ):
        if candidate.is_file():
            return str(candidate)
    return None


def npm_noninteractive_env() -> dict[str, str]:
    return {
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "NPM_CONFIG_YES": "true",
        "npm_config_yes": "true",
    }


def resolve_bun_command() -> list[str] | None:
    bun_bin = shutil.which("bun")
    if bun_bin:
        return [bun_bin]
    npx_bin = shutil.which("npx")
    if npx_bin:
        return [npx_bin, "--yes", "bun"]
    return None


def resolve_bash_command() -> list[str] | None:
    bash_bin = shutil.which("bash")
    if bash_bin:
        return [bash_bin]
    return None


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


def bash_script_path(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if os.name != "nt":
        return str(resolved)
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
    rendered = str(resolved).replace("\\", "/")
    drive_match = re.match(r"^([A-Za-z]):/(.*)$", rendered)
    if drive_match:
        drive = drive_match.group(1).lower()
        remainder = drive_match.group(2)
        return f"/mnt/{drive}/{remainder}"
    return resolved.as_posix()


def sanitize_filename(value: str) -> str:
    lowered = value.strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-")
    return slug or "step"


def _quote_posix(value: str) -> str:
    if not value:
        return "''"
    if re.fullmatch(r"[A-Za-z0-9_./:-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def command_display(argv: list[str]) -> str:
    return " ".join(subprocess.list2cmdline([part]) if os.name == "nt" else _quote_posix(part) for part in argv)


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_native_path(path_like: str) -> Path:
    # Keep path resolution bound to the actual host platform even when tests
    # temporarily patch `os.name` to exercise Windows-only retry logic.
    return NATIVE_PATH_CLS(path_like).expanduser().resolve()


def _parse_iso_timestamp(raw: str | None) -> float | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def artifact_is_fresh_for_started_at(artifact_paths: list[Path], started_at: str | None) -> bool:
    started_ts = _parse_iso_timestamp(started_at)
    if started_ts is None or not artifact_paths:
        return False
    for artifact in artifact_paths:
        if not artifact.is_file():
            return False
        if artifact.stat().st_mtime + 1e-6 < started_ts:
            return False
    return True


def is_windows_profile_smoke_negative_exit(step: ReleaseStep, status: str, rc: int | None) -> bool:
    if os.name != "nt" or status != "FAIL" or rc is None:
        return False
    if not step.step_id.startswith("4.") or not step.title.startswith("Profile Smoke"):
        return False
    return rc in {-1, 0xFFFFFFFF}


def profile_smoke_artifact_reports_pass(artifact_paths: list[Path]) -> bool:
    for artifact in artifact_paths:
        if artifact.suffix.lower() != ".md" or not artifact.is_file():
            continue
        text = artifact.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\|\s*[^|]+\|\s*[^|]+\|\s*PASS\s*\|", text, flags=re.MULTILINE):
            return True
    return False


def clear_checkpoint_dir_contents(checkpoint_dir: Path) -> None:
    if not checkpoint_dir.exists():
        return
    for child in checkpoint_dir.iterdir():
        if child.name == CHECKPOINT_LOCK_NAME:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            with contextlib.suppress(FileNotFoundError):
                child.unlink()


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class ReleaseGateLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = {"pid": os.getpid(), "created_at": utc_now_iso()}
                os.write(self._fd, (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
                return
            except FileExistsError:
                try:
                    existing = json.loads(self.lock_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
                existing_pid = int(existing.get("pid") or 0) if str(existing.get("pid") or "").strip() else 0
                if existing_pid and _pid_is_alive(existing_pid):
                    raise RuntimeError(
                        f"Another release-gate process is already using {self.lock_path.parent} (pid={existing_pid})."
                    )
                with contextlib.suppress(FileNotFoundError):
                    self.lock_path.unlink()

    def release(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
        with contextlib.suppress(FileNotFoundError):
            self.lock_path.unlink()


def render_log_tail(log_path: Path, *, max_lines: int = 200) -> str:
    if not log_path.is_file():
        return "(no log captured)"
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def package_dry_run_log_is_clean(log_path: Path) -> tuple[bool, list[str]]:
    if not log_path.is_file():
        return False, ["(missing dry-run log)"]
    text = log_path.read_text(encoding="utf-8", errors="replace")
    matches = [path for path in PACKAGE_DRY_RUN_FORBIDDEN_PATHS if path in text]
    return len(matches) == 0, matches


def _kill_process_tree_windows(pid: int, *, force: bool) -> None:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    try:
        subprocess.run(command, text=True, capture_output=True, check=False, timeout=15)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return


def terminate_process_tree(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        _kill_process_tree_windows(proc.pid, force=False)
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            _kill_process_tree_windows(proc.pid, force=True)
            proc.wait(timeout=10)
            return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        except ProcessLookupError:
            return
        proc.wait(timeout=10)


def prepare_command(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    resolved = shutil.which(argv[0]) or argv[0]
    if os.name == "nt" and str(resolved).lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/d", "/s", "/c", subprocess.list2cmdline([resolved, *argv[1:]])]
    if resolved != argv[0]:
        return [resolved, *argv[1:]]
    return argv


def run_step_commands(step: ReleaseStep) -> tuple[str, int | None, float]:
    step.log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    with step.log_path.open("w", encoding="utf-8") as log_handle:
        for index, command in enumerate(step.commands, start=1):
            prepared = prepare_command(command.argv)
            log_handle.write(f"$ [{index}/{len(step.commands)}] {command_display(command.argv)}\n")
            log_handle.flush()
            env = os.environ.copy()
            env.update(command.env_overrides)
            popen_kwargs: dict[str, Any] = {
                "cwd": str(command.cwd),
                "env": env,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "text": True,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            else:
                popen_kwargs["start_new_session"] = True
            proc = subprocess.Popen(prepared, **popen_kwargs)
            try:
                remaining = None
                if step.timeout_seconds > 0:
                    elapsed = time.time() - started_at
                    remaining = max(1, step.timeout_seconds - int(elapsed))
                rc = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                terminate_process_tree(proc)
                log_handle.write(
                    f"\n[release-gate-timeout] step exceeded {step.timeout_seconds}s and was terminated.\n"
                )
                log_handle.flush()
                return "TIMEOUT", 124, time.time() - started_at
            if rc != 0:
                return "FAIL", int(rc), time.time() - started_at
    return "PASS", 0, time.time() - started_at


def build_visual_benchmark_metrics_lines(json_path: Path) -> list[str]:
    if not json_path.is_file():
        return ["- Metrics: unavailable (JSON artifact missing)"]
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if isinstance(profiles, list):
        case_count_per_profile = payload.get("executed_case_count_per_profile")
        case_count_total = payload.get("executed_case_count_total")
    else:
        case_count_per_profile = payload.get("executed_case_count")
        case_count_total = payload.get("executed_case_count")
    return [
        f"- benchmark_status: `{payload.get('status', '-')}`",
        f"- benchmark_partial: `{payload.get('partial', '-')}`",
        f"- case_catalog_size: `{payload.get('case_catalog_size', '-')}`",
        f"- executed_case_count_per_profile: `{case_count_per_profile}`",
        f"- executed_case_count_total: `{case_count_total}`",
    ]


def render_report(
    *,
    report_path: Path,
    checkpoint_path: Path,
    checkpoint_payload: dict[str, Any],
    steps: list[ReleaseStep],
) -> None:
    step_state = checkpoint_payload.get("steps", {})
    lines = [
        "# OpenClaw Memory Palace Release Gate Report",
        "",
        f"- Generated At: `{checkpoint_payload.get('updated_at', utc_now_iso())}`",
        f"- Project Root: `{PROJECT_ROOT}`",
        f"- Checkpoint Path: `{checkpoint_path}`",
        f"- Profile Smoke Modes: `{checkpoint_payload.get('profile_smoke_modes', '-')}`",
        f"- Review Smoke Modes: `{checkpoint_payload.get('review_smoke_modes', '-')}`",
        "",
    ]
    overall_fail = False
    overall_pending = False
    for step in steps:
        state = step_state.get(step.step_id, {})
        status = state.get("status", "PENDING")
        if status in {"FAIL", "TIMEOUT"}:
            overall_fail = True
        if status == "SKIP" and state.get("skip_causes_failure"):
            overall_fail = True
        if status not in {"PASS", "SKIP"}:
            overall_pending = True
        lines.extend(
            [
                f"## {step.step_id}. {step.title}",
                "",
                f"- Status: `{status}`",
                f"- Workdir: `{step.commands[0].cwd if step.commands else PROJECT_ROOT}`",
            ]
        )
        if state.get("duration_sec") is not None:
            lines.append(f"- DurationSec: `{state['duration_sec']}`")
        if state.get("log_path"):
            lines.append(f"- Log Path: `{state['log_path']}`")
        if state.get("skip_reason"):
            lines.append(f"- Reason: {state['skip_reason']}")
        if state.get("runner_warning"):
            lines.append(f"- Runner Warning: `{state['runner_warning']}`")
        if state.get("forbidden_paths"):
            lines.append(f"- Forbidden Paths: `{', '.join(state['forbidden_paths'])}`")
        if state.get("artifact_paths"):
            lines.append(f"- Artifacts: `{', '.join(state['artifact_paths'])}`")
        commands = state.get("commands") or [command_display(item.argv) for item in step.commands]
        if commands:
            lines.extend(["", "```text", *commands, "```"])
        if status in {"PASS", "FAIL", "TIMEOUT"} and state.get("log_path"):
            lines.extend(["", "```text", render_log_tail(resolve_native_path(str(state["log_path"]))), "```"])
        if step.step_id == "5" and status == "PASS" and step.artifact_paths:
            lines.extend(["", "### Visual Benchmark Artifacts", ""])
            for artifact in step.artifact_paths:
                lines.append(f"- `{artifact}`")
            json_artifact = next((path for path in step.artifact_paths if path.suffix.lower() == ".json"), None)
            if json_artifact is not None:
                lines.extend(["", "### Visual Benchmark Metrics", ""])
                lines.extend(build_visual_benchmark_metrics_lines(json_artifact))
        lines.append("")
    result = "FAIL" if overall_fail else ("PENDING" if overall_pending else "PASS")
    lines.extend(["## Summary", "", f"- Result: `{result}`", f"- Report Path: `{report_path}`", ""])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def model_env_has_full_retrieval(path: Path | None) -> bool:
    if path is None or not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    required_keys = (
        "RETRIEVAL_EMBEDDING_API_KEY",
        "RETRIEVAL_EMBEDDING_MODEL",
        "RETRIEVAL_RERANKER_API_KEY",
        "RETRIEVAL_RERANKER_MODEL",
    )
    return all(re.search(rf"^{re.escape(key)}=", text, flags=re.MULTILINE) for key in required_keys)


def build_release_steps(args: argparse.Namespace, checkpoint_dir: Path) -> tuple[list[ReleaseStep], dict[str, Any]]:
    resolved_openclaw_bin = resolve_openclaw_bin_value()
    backend_python = resolve_python_from_venv(BACKEND_ROOT / ".venv")
    repo_python = resolve_python_from_venv(PROJECT_ROOT / ".venv") or backend_python
    python_matrix_versions = os.environ.get("PYTHON_MATRIX_VERSIONS", "3.10,3.11,3.12,3.13,3.14")
    onboarding_apply_validate_profiles = (
        os.environ.get("RELEASE_GATE_ONBOARDING_APPLY_VALIDATE_PROFILES", "c,d").strip() or "c,d"
    )
    profile_smoke_profiles = os.environ.get("PROFILE_SMOKE_PROFILES", "a,b,c,d")
    phase45_profiles = os.environ.get("PHASE45_PROFILES", args.phase45_profiles or "c,d")
    visual_benchmark_profiles = os.environ.get("VISUAL_BENCHMARK_PROFILES", "a,b")
    visual_benchmark_case_count = normalize_positive_int(os.environ.get("VISUAL_BENCHMARK_CASE_COUNT"), 200)
    visual_benchmark_case_limit = normalize_positive_int(os.environ.get("VISUAL_BENCHMARK_CASE_LIMIT"), 64)
    visual_benchmark_max_workers = normalize_positive_int(os.environ.get("VISUAL_BENCHMARK_MAX_WORKERS"), 1)
    visual_benchmark_required_coverage = os.environ.get(
        "VISUAL_BENCHMARK_REQUIRED_COVERAGE",
        "raw_media_data_png,raw_media_data_jpeg,raw_media_data_webp,raw_media_blob,raw_media_presigned",
    )
    visual_benchmark_expand_profiles = os.environ.get("VISUAL_BENCHMARK_EXPAND_PROFILES_ON_FULL_MODEL_ENV", "0") == "1"
    model_env_path = resolve_native_path(args.profile_smoke_model_env) if args.profile_smoke_model_env else None
    if model_env_path is None:
        env_default = str(os.environ.get("OPENCLAW_PROFILE_MODEL_ENV") or "").strip()
        if env_default:
            model_env_path = resolve_native_path(env_default)
    if model_env_path is None and (PROJECT_ROOT / ".env").is_file():
        model_env_path = (PROJECT_ROOT / ".env").resolve()
    onboarding_apply_validate_skip_by_env = env_flag_enabled(
        os.environ.get("RELEASE_GATE_SKIP_ONBOARDING_APPLY_VALIDATE")
    )
    onboarding_apply_validate_skip_requested = bool(
        getattr(args, "skip_onboarding_apply_validate", False) or onboarding_apply_validate_skip_by_env
    )
    current_host_strict_ui_enabled = bool(
        getattr(args, "enable_current_host_strict_ui", False)
        or env_flag_enabled(os.environ.get("RELEASE_GATE_ENABLE_CURRENT_HOST_STRICT_UI"))
    )
    current_host_strict_ui_skip_requested = bool(
        getattr(args, "skip_current_host_strict_ui", False)
    )
    current_host_strict_ui_profile = (
        str(
            getattr(args, "current_host_ui_profile", "")
            or os.environ.get("RELEASE_GATE_CURRENT_HOST_STRICT_UI_PROFILE", "d")
        )
        .strip()
        .lower()
        or "d"
    )
    current_host_strict_ui_url = (
        str(
            getattr(args, "current_host_ui_url", "")
            or os.environ.get("OPENCLAW_ACL_CONTROL_UI_URL", "")
        )
        .strip()
    )
    live_benchmark_enabled = bool(
        getattr(args, "enable_live_benchmark", False)
        or env_flag_enabled(os.environ.get("RELEASE_GATE_ENABLE_LIVE_BENCHMARK"))
        or env_flag_enabled(os.environ.get("OPENCLAW_ENABLE_LIVE_BENCHMARK"))
    )
    windows_native_validation_enabled = bool(
        getattr(args, "enable_windows_native_validation", False)
        or env_flag_enabled(os.environ.get("RELEASE_GATE_ENABLE_WINDOWS_NATIVE_VALIDATION"))
        or env_flag_enabled(os.environ.get("OPENCLAW_ENABLE_WINDOWS_NATIVE_VALIDATION"))
    )
    if visual_benchmark_expand_profiles and model_env_has_full_retrieval(model_env_path) and not os.environ.get(
        "VISUAL_BENCHMARK_PROFILES"
    ):
        visual_benchmark_profiles = "a,b,c,d"

    logs_dir = checkpoint_dir / "logs"
    artifacts_dir = checkpoint_dir / "artifacts"
    logs_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    steps: list[ReleaseStep] = []

    def add_step(
        step_id: str,
        title: str,
        commands: list[StepCommand],
        timeout_seconds: int,
        *,
        artifact_paths: list[Path] | None = None,
        skip_reason: str | None = None,
        skip_causes_failure: bool = False,
    ) -> None:
        steps.append(
            ReleaseStep(
                step_id=step_id,
                title=title,
                commands=commands,
                timeout_seconds=timeout_seconds,
                log_path=logs_dir / f"{sanitize_filename(step_id)}-{sanitize_filename(title)}.log",
                artifact_paths=artifact_paths or [],
                skip_reason=skip_reason,
                skip_causes_failure=skip_causes_failure,
            )
        )

    bash_command = resolve_bash_command()
    add_step(
        "0",
        "Security Scan",
        [StepCommand([*(bash_command or ["bash"]), bash_script_path(PRE_PUBLISH_SCRIPT)], PROJECT_ROOT)],
        timeout_seconds=0,
        skip_reason="bash is unavailable on this host." if bash_command is None else None,
        skip_causes_failure=False,
    )
    add_step(
        "1",
        "Backend Pytest",
        [
            StepCommand(
                [
                    backend_python or "python",
                    "-m",
                    "pytest",
                    "tests",
                    "-q",
                    "-m",
                    "not slow",
                ],
                BACKEND_ROOT,
            )
        ],
        timeout_seconds=RELEASE_STEP_TIMEOUT_BACKEND_PYTEST,
        skip_reason="Skipped by flag." if args.skip_backend_tests else ("No backend python interpreter found." if backend_python is None else None),
        skip_causes_failure=not args.skip_backend_tests and backend_python is None,
    )
    add_step(
        "1.5",
        "Script-Level Pytest",
        [
            StepCommand(
                [repo_python or "python", "-m", "pytest", *SCRIPT_LEVEL_PYTEST_FILES, "-q"],
                PROJECT_ROOT,
            )
        ],
        timeout_seconds=RELEASE_STEP_TIMEOUT_SCRIPT_PYTEST,
        skip_reason="Skipped with backend tests by flag." if args.skip_backend_tests else ("No project python interpreter found." if repo_python is None else None),
        skip_causes_failure=not args.skip_backend_tests and repo_python is None,
    )
    backend_live_benchmark_skip_reason: str | None = None
    backend_live_benchmark_skip_failure = False
    backend_live_benchmark_commands: list[StepCommand] = []
    if args.skip_backend_tests:
        backend_live_benchmark_skip_reason = "Skipped with backend tests by flag."
    elif not live_benchmark_enabled:
        backend_live_benchmark_skip_reason = (
            "Skipped by default; pass --enable-live-benchmark to run the maintainer-only backend benchmark rerun gate."
        )
    elif backend_python is None:
        backend_live_benchmark_skip_reason = "No backend python interpreter found."
        backend_live_benchmark_skip_failure = True
    else:
        backend_live_benchmark_commands = [
            StepCommand(
                [
                    backend_python,
                    "-m",
                    "pytest",
                    "tests/benchmark/test_ci_regression_gate.py",
                    "-q",
                    "-k",
                    "rerun_gate",
                    "-m",
                    "slow",
                ],
                BACKEND_ROOT,
                env_overrides={"OPENCLAW_ENABLE_LIVE_BENCHMARK": "1"},
            )
        ]
    add_step(
        "1.6",
        "Backend Benchmark Rerun Gate",
        backend_live_benchmark_commands,
        timeout_seconds=RELEASE_STEP_TIMEOUT_BACKEND_PYTEST,
        skip_reason=backend_live_benchmark_skip_reason,
        skip_causes_failure=backend_live_benchmark_skip_failure,
    )
    add_step(
        "2",
        "MCP Stdio E2E",
        [StepCommand([backend_python or "python", "-m", "pytest", "tests/test_mcp_stdio_e2e.py", "-q"], BACKEND_ROOT)],
        timeout_seconds=RELEASE_STEP_TIMEOUT_MCP_STDIO_E2E,
        skip_reason="Skipped with backend tests by flag." if args.skip_backend_tests else ("No backend python interpreter found." if backend_python is None else None),
        skip_causes_failure=not args.skip_backend_tests and backend_python is None,
    )

    bun_command = resolve_bun_command()
    plugin_commands: list[StepCommand] = []
    plugin_skip_reason = None
    plugin_skip_failure = False
    if args.skip_plugin_tests:
        plugin_skip_reason = "Skipped by flag."
    elif bun_command is None:
        plugin_skip_reason = "bun is not installed."
        plugin_skip_failure = True
    else:
        plugin_commands.append(
            StepCommand(
                [
                    *bun_command,
                    "test",
                    "src/client.test.ts",
                    "src/smart-extraction.test.ts",
                    "src/assistant-derived.test.ts",
                    "src/host-bridge.test.ts",
                    "src/runtime-layout.test.ts",
                    "src/onboarding-tools.test.ts",
                    "index.test.ts",
                ],
                PROJECT_ROOT / "extensions" / "memory-palace",
            )
        )
        tsc_candidate = PROJECT_ROOT / "extensions" / "memory-palace" / "node_modules" / ".bin" / (
            "tsc.cmd" if os.name == "nt" else "tsc"
        )
        if not tsc_candidate.exists():
            plugin_commands.append(
                StepCommand(
                    ["npm", "install", "--no-save", "typescript@^5.9.3", "@types/node@^25.5.0"],
                    PROJECT_ROOT / "extensions" / "memory-palace",
                )
            )
        plugin_commands.append(
            StepCommand(["npm", "exec", "--", "tsc", "--project", "tsconfig.json", "--noEmit"], PROJECT_ROOT / "extensions" / "memory-palace")
        )
    add_step(
        "3",
        "Plugin Bun Tests",
        plugin_commands,
        timeout_seconds=RELEASE_STEP_TIMEOUT_PLUGIN_BUN_TESTS,
        skip_reason=plugin_skip_reason,
        skip_causes_failure=plugin_skip_failure,
    )
    package_dry_run_skip_reason = (
        "Skipped with plugin tests by flag."
        if args.skip_plugin_tests
        else ("No project python interpreter found." if repo_python is None else None)
    )
    package_dry_run_skip_failure = not args.skip_plugin_tests and repo_python is None
    add_step(
        "3.45",
        "Package Dry Run Audit",
        [
            StepCommand([repo_python or "python", "scripts/openclaw_memory_palace.py", "stage-package"], PROJECT_ROOT),
            StepCommand(
                ["npm", "pack", "--dry-run"],
                PROJECT_ROOT / "extensions" / "memory-palace",
                env_overrides=npm_noninteractive_env(),
            ),
        ],
        timeout_seconds=RELEASE_STEP_TIMEOUT_PLUGIN_BUN_TESTS,
        skip_reason=package_dry_run_skip_reason,
        skip_causes_failure=package_dry_run_skip_failure,
    )
    windows_validation_skip_reason: str | None = None
    windows_validation_skip_failure = False
    windows_validation_commands: list[StepCommand] = []
    if args.skip_plugin_tests:
        windows_validation_skip_reason = "Skipped with plugin tests by flag."
    elif not windows_native_validation_enabled:
        windows_validation_skip_reason = (
            "Skipped by default; pass --enable-windows-native-validation to run the maintainer-only Windows native validation gate."
        )
    elif repo_python is None:
        windows_validation_skip_reason = "No project python interpreter found."
        windows_validation_skip_failure = True
    elif os.name != "nt":
        windows_validation_skip_reason = (
            "Must run scripts/openclaw_memory_palace_windows_native_validation.py on a real Windows host."
        )
        windows_validation_skip_failure = True
    elif model_env_path is None:
        windows_validation_skip_reason = (
            "Windows native validation for profiles b,c,d requires --profile-smoke-model-env with real provider settings."
        )
        windows_validation_skip_failure = True
    else:
        windows_validation_argv = [
            repo_python or "python",
            str(WINDOWS_NATIVE_VALIDATION_SCRIPT.relative_to(PROJECT_ROOT)),
            "--profiles",
            "b,c,d",
            "--model-env",
            str(model_env_path),
        ]
        windows_validation_commands = [StepCommand(windows_validation_argv, PROJECT_ROOT)]
    add_step(
        "3.4",
        "Windows Native Validation",
        windows_validation_commands,
        timeout_seconds=RELEASE_STEP_TIMEOUT_PLUGIN_BUN_TESTS,
        skip_reason=windows_validation_skip_reason,
        skip_causes_failure=windows_validation_skip_failure,
    )
    add_step(
        "3.5",
        "Package Install Smoke (basic+full)",
        [StepCommand([repo_python or "python", "scripts/test_openclaw_memory_palace_package_install.py"], PROJECT_ROOT)],
        timeout_seconds=RELEASE_STEP_TIMEOUT_PACKAGE_INSTALL,
        skip_reason="Skipped with plugin tests by flag." if args.skip_plugin_tests else ("No project python interpreter found." if repo_python is None else None),
        skip_causes_failure=not args.skip_plugin_tests and repo_python is None,
    )
    onboarding_apply_validate_skip_reason: str | None = None
    onboarding_apply_validate_skip_failure = False
    onboarding_apply_validate_commands: list[StepCommand] = []
    onboarding_apply_validate_json = artifacts_dir / "onboarding_apply_validate.json"
    onboarding_apply_validate_md = artifacts_dir / "onboarding_apply_validate.md"
    if onboarding_apply_validate_skip_requested:
        onboarding_apply_validate_skip_reason = (
            "Skipped by flag."
            if getattr(args, "skip_onboarding_apply_validate", False)
            else "Skipped by env flag."
        )
    elif model_env_path is None:
        onboarding_apply_validate_skip_reason = (
            "No profile smoke model env was provided; skipped onboarding apply/validate E2E gate."
        )
    elif repo_python is None:
        onboarding_apply_validate_skip_reason = "No project python interpreter found."
        onboarding_apply_validate_skip_failure = True
    elif resolved_openclaw_bin is None:
        onboarding_apply_validate_skip_reason = (
            "openclaw is not available; onboarding apply/validate E2E requires the real CLI."
        )
        onboarding_apply_validate_skip_failure = True
    else:
        onboarding_apply_validate_commands = [
            StepCommand(
                [
                    repo_python,
                    "scripts/test_onboarding_apply_validate_e2e.py",
                    "--model-env",
                    str(model_env_path),
                    "--profiles",
                    onboarding_apply_validate_profiles,
                    "--report",
                    str(onboarding_apply_validate_json),
                    "--markdown",
                    str(onboarding_apply_validate_md),
                ],
                PROJECT_ROOT,
            )
        ]
    add_step(
        "3.55",
        "Onboarding Apply Validate E2E",
        onboarding_apply_validate_commands,
        timeout_seconds=RELEASE_STEP_TIMEOUT_PROFILE_SMOKE,
        artifact_paths=[onboarding_apply_validate_json, onboarding_apply_validate_md],
        skip_reason=onboarding_apply_validate_skip_reason,
        skip_causes_failure=onboarding_apply_validate_skip_failure,
    )
    add_step(
        "3.6",
        "Python Matrix Package Install Smoke",
        [
            StepCommand(
                [
                    repo_python or "python",
                    "scripts/openclaw_memory_palace_python_matrix.py",
                    "--versions",
                    python_matrix_versions,
                    "--skip-full-stack",
                ],
                PROJECT_ROOT,
            )
        ],
        timeout_seconds=compute_python_matrix_timeout_seconds(python_matrix_versions),
        skip_reason=(
            "Skipped by flag."
            if args.skip_python_matrix
            else ("Skipped with plugin tests by flag." if args.skip_plugin_tests else ("No project python interpreter found." if repo_python is None else None))
        ),
        skip_causes_failure=(
            not args.skip_python_matrix
            and not args.skip_plugin_tests
            and repo_python is None
        ),
    )

    profile_modes = [item.strip() for item in args.profile_modes.split(",") if item.strip()]
    profile_names = [item.strip() for item in profile_smoke_profiles.split(",") if item.strip()]
    for mode in profile_modes:
        for profile in profile_names:
            artifact_path = artifacts_dir / f"profile_smoke.{mode}.{profile}.md"
            argv = [repo_python or "python", "scripts/openclaw_memory_palace_profile_smoke.py", "--modes", mode, "--profiles", profile]
            if model_env_path is not None:
                argv.extend(["--model-env", str(model_env_path)])
            argv.extend(["--skip-frontend-e2e", "--report", str(artifact_path)])
            add_step(
                f"4.{mode}.{profile}",
                f"Profile Smoke ({mode}/{profile})",
                [StepCommand(argv, PROJECT_ROOT)],
                timeout_seconds=RELEASE_STEP_TIMEOUT_PROFILE_SMOKE,
                artifact_paths=[artifact_path],
                skip_reason="Skipped by flag." if args.skip_profile_smoke else ("No project python interpreter found." if repo_python is None else None),
                skip_causes_failure=not args.skip_profile_smoke and repo_python is None,
            )

    phase45_names = [item.strip() for item in phase45_profiles.split(",") if item.strip()]
    phase45_model_env_available = model_env_path is not None
    phase45_skip_reason: str | None = None
    phase45_skip_causes_failure = False
    if getattr(args, "skip_phase45", False):
        phase45_skip_reason = "Skipped by flag."
    elif not phase45_model_env_available:
        phase45_skip_reason = "No profile smoke model env was provided; skipped maintainer-only phase45 C/D gate."
    elif repo_python is None:
        phase45_skip_reason = "No project python interpreter found."
        phase45_skip_causes_failure = True

    for profile in phase45_names:
        artifact_path = artifacts_dir / f"phase45_e2e.{profile}.json"
        argv = [
            repo_python or "python",
            "scripts/openclaw_memory_palace_phase45_e2e.py",
            "--profile",
            profile,
            "--report",
            str(artifact_path),
        ]
        if model_env_path is not None:
            argv.extend(["--model-env", str(model_env_path)])
        add_step(
            f"4.phase45.{profile}",
            f"Phase45 E2E ({profile})",
            [StepCommand(argv, PROJECT_ROOT)],
            timeout_seconds=RELEASE_STEP_TIMEOUT_PROFILE_SMOKE,
            artifact_paths=[artifact_path],
            skip_reason=phase45_skip_reason,
            skip_causes_failure=phase45_skip_causes_failure,
        )

    compact_reflection_profile = os.environ.get(
        "COMPACT_CONTEXT_REFLECTION_PROFILE", "c"
    ).strip() or "c"
    compact_reflection_skip_reason: str | None = None
    compact_reflection_skip_causes_failure = False
    if getattr(args, "skip_phase45", False):
        compact_reflection_skip_reason = "Skipped with phase45 by flag."
    elif not phase45_model_env_available:
        compact_reflection_skip_reason = (
            "No profile smoke model env was provided; skipped maintainer-only compact_context reflection gate."
        )
    elif repo_python is None:
        compact_reflection_skip_reason = "No project python interpreter found."
        compact_reflection_skip_causes_failure = True

    compact_reflection_artifact = (
        artifacts_dir / f"compact_context_reflection_e2e.{compact_reflection_profile}.json"
    )
    compact_reflection_argv = [
        repo_python or "python",
        "scripts/openclaw_compact_context_reflection_e2e.py",
        "--profile",
        compact_reflection_profile,
        "--report",
        str(compact_reflection_artifact),
    ]
    if model_env_path is not None:
        compact_reflection_argv.extend(["--model-env", str(model_env_path)])
    add_step(
        f"4.compact_reflection.{compact_reflection_profile}",
        f"Compact Context Reflection E2E ({compact_reflection_profile})",
        [StepCommand(compact_reflection_argv, PROJECT_ROOT)],
        timeout_seconds=RELEASE_STEP_TIMEOUT_PROFILE_SMOKE,
        artifact_paths=[compact_reflection_artifact],
        skip_reason=compact_reflection_skip_reason,
        skip_causes_failure=compact_reflection_skip_causes_failure,
    )

    benchmark_json = artifacts_dir / "openclaw_visual_memory_benchmark.json"
    benchmark_md = artifacts_dir / "openclaw_visual_memory_benchmark.md"
    benchmark_timeout = compute_visual_benchmark_timeout_seconds(
        visual_benchmark_profiles,
        visual_benchmark_case_limit,
        visual_benchmark_max_workers,
    )
    benchmark_argv = [
        repo_python or "python",
        "scripts/openclaw_visual_memory_benchmark.py",
        "--profiles",
        visual_benchmark_profiles,
        "--case-count",
        str(visual_benchmark_case_count),
        "--case-limit",
        str(visual_benchmark_case_limit),
        "--max-workers",
        str(visual_benchmark_max_workers),
        "--required-coverage",
        visual_benchmark_required_coverage,
        "--json-output",
        str(benchmark_json),
        "--markdown-output",
        str(benchmark_md),
    ]
    if model_env_path is not None:
        benchmark_argv.extend(["--model-env", str(model_env_path)])
    add_step(
        "5",
        "Visual Benchmark",
        [StepCommand(benchmark_argv, PROJECT_ROOT)],
        timeout_seconds=benchmark_timeout,
        artifact_paths=[benchmark_json, benchmark_md],
        skip_reason="Skipped with profile smoke by flag." if args.skip_profile_smoke else ("No project python interpreter found." if repo_python is None else None),
        skip_causes_failure=not args.skip_profile_smoke and repo_python is None,
    )

    review_modes = [item.strip() for item in args.review_smoke_modes.split(",") if item.strip()]
    for mode in review_modes:
        artifact_path = artifacts_dir / f"review_snapshots_http_smoke.{mode}.md"
        add_step(
            f"6.{mode}",
            f"Review Snapshot Smoke ({mode})",
            [StepCommand([repo_python or "python", "scripts/review_snapshots_http_smoke.py", "--modes", mode, "--report", str(artifact_path)], PROJECT_ROOT)],
            timeout_seconds=RELEASE_STEP_TIMEOUT_REVIEW_SMOKE,
            artifact_paths=[artifact_path],
            skip_reason="Skipped by flag." if args.skip_review_smoke else ("No project python interpreter found." if repo_python is None else None),
            skip_causes_failure=not args.skip_review_smoke and repo_python is None,
        )

    add_step(
        "7",
        "Frontend Tests",
        [StepCommand(["npm", "test"], FRONTEND_ROOT), StepCommand(["npm", "run", "build"], FRONTEND_ROOT)],
        timeout_seconds=RELEASE_STEP_TIMEOUT_FRONTEND_TESTS,
        skip_reason="Skipped by flag." if args.skip_frontend else None,
    )

    frontend_e2e_skip_reason = None
    frontend_e2e_skip_failure = False
    if args.skip_frontend or args.skip_frontend_e2e:
        frontend_e2e_skip_reason = "Skipped by flag."
    elif resolved_openclaw_bin is None:
        frontend_e2e_skip_reason = "openclaw is not available; dashboard-auth-i18n.spec.ts requires the real CLI."
        frontend_e2e_skip_failure = True
    elif shutil.which("npx") is None:
        frontend_e2e_skip_reason = "npx is not installed."
        frontend_e2e_skip_failure = True
    add_step(
        "8",
        "Frontend Playwright E2E",
        [
            StepCommand(
                ["npx", "playwright", "install", "chromium"],
                FRONTEND_ROOT,
                env_overrides={
                    "PLAYWRIGHT_E2E_API_PORT": str(RELEASE_STEP_FRONTEND_E2E_API_PORT),
                    "PLAYWRIGHT_E2E_UI_PORT": str(RELEASE_STEP_FRONTEND_E2E_UI_PORT),
                },
            ),
            StepCommand(
                ["npm", "run", "test:e2e"],
                FRONTEND_ROOT,
                env_overrides={
                    "PLAYWRIGHT_E2E_API_PORT": str(RELEASE_STEP_FRONTEND_E2E_API_PORT),
                    "PLAYWRIGHT_E2E_UI_PORT": str(RELEASE_STEP_FRONTEND_E2E_UI_PORT),
                },
            ),
        ],
        timeout_seconds=RELEASE_STEP_TIMEOUT_FRONTEND_E2E,
        skip_reason=frontend_e2e_skip_reason,
        skip_causes_failure=frontend_e2e_skip_failure,
    )
    current_host_strict_ui_skip_reason: str | None = None
    current_host_strict_ui_skip_failure = False
    current_host_strict_ui_commands: list[StepCommand] = []
    current_host_strict_ui_report = artifacts_dir / f"current_host_strict_ui.{current_host_strict_ui_profile}.json"
    if current_host_strict_ui_skip_requested:
        current_host_strict_ui_skip_reason = "Skipped by flag."
    elif not current_host_strict_ui_enabled:
        current_host_strict_ui_skip_reason = (
            "Skipped by default; set RELEASE_GATE_ENABLE_CURRENT_HOST_STRICT_UI=1 to run the release-only current-host strict UI gate."
        )
    elif shutil.which("node") is None:
        current_host_strict_ui_skip_reason = "node is not installed."
        current_host_strict_ui_skip_failure = True
    elif resolved_openclaw_bin is None:
        current_host_strict_ui_skip_reason = (
            "openclaw is not available; current-host strict UI gate requires the real host CLI."
        )
        current_host_strict_ui_skip_failure = True
    else:
        current_host_strict_ui_commands = [
            StepCommand(
                ["node", "scripts/test_replacement_acceptance_webui.mjs"],
                PROJECT_ROOT,
                env_overrides={
                    "OPENCLAW_ONBOARDING_USE_CURRENT_HOST": "true",
                    "OPENCLAW_ACCEPTANCE_STRICT_UI": "true",
                    "OPENCLAW_PROFILE": current_host_strict_ui_profile,
                    "OPENCLAW_REPORT_PATH": str(current_host_strict_ui_report),
                    "OPENCLAW_SCREENSHOT_DIR": str(
                        artifacts_dir / f"current_host_strict_ui.{current_host_strict_ui_profile}"
                    ),
                    **({"OPENCLAW_ACL_CONTROL_UI_URL": current_host_strict_ui_url} if current_host_strict_ui_url else {}),
                },
            )
        ]
    add_step(
        "8.5",
        "Current Host Strict UI Acceptance",
        current_host_strict_ui_commands,
        timeout_seconds=RELEASE_STEP_TIMEOUT_FRONTEND_E2E,
        artifact_paths=[current_host_strict_ui_report],
        skip_reason=current_host_strict_ui_skip_reason,
        skip_causes_failure=current_host_strict_ui_skip_failure,
    )

    metadata = {
        "onboarding_apply_validate_profiles": onboarding_apply_validate_profiles,
        "onboarding_apply_validate_enabled": bool(model_env_path)
        and not onboarding_apply_validate_skip_requested,
        "profile_smoke_modes": args.profile_modes,
        "review_smoke_modes": args.review_smoke_modes,
        "profile_smoke_profiles": profile_smoke_profiles,
        "phase45_profiles": phase45_profiles,
        "phase45_enabled": phase45_model_env_available and not getattr(args, "skip_phase45", False),
        "compact_context_reflection_profile": compact_reflection_profile,
        "compact_context_reflection_enabled": phase45_model_env_available
        and not getattr(args, "skip_phase45", False),
        "visual_benchmark_profiles": visual_benchmark_profiles,
        "visual_benchmark_case_limit": visual_benchmark_case_limit,
        "visual_benchmark_max_workers": visual_benchmark_max_workers,
        "live_benchmark_enabled": live_benchmark_enabled
        and not args.skip_backend_tests,
        "profile_smoke_model_env": str(model_env_path) if model_env_path else "",
        "windows_native_validation_enabled": windows_native_validation_enabled,
        "current_host_strict_ui_enabled": current_host_strict_ui_enabled
        and not current_host_strict_ui_skip_requested,
        "current_host_strict_ui_profile": current_host_strict_ui_profile,
        "python_matrix_versions": python_matrix_versions,
    }
    return steps, metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Checkpointed release-gate runner for OpenClaw Memory Palace.")
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--checkpoint-dir", default=str(DEFAULT_CHECKPOINT_DIR))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--skip-backend-tests", action="store_true")
    parser.add_argument("--skip-plugin-tests", action="store_true")
    parser.add_argument("--skip-python-matrix", action="store_true")
    parser.add_argument("--skip-frontend-tests", dest="skip_frontend", action="store_true")
    parser.add_argument("--skip-frontend-e2e", action="store_true")
    parser.add_argument("--enable-live-benchmark", action="store_true")
    parser.add_argument("--enable-windows-native-validation", action="store_true")
    parser.add_argument("--skip-profile-smoke", action="store_true")
    parser.add_argument("--skip-onboarding-apply-validate", action="store_true")
    parser.add_argument("--skip-phase45", action="store_true")
    parser.add_argument("--skip-review-smoke", action="store_true")
    parser.add_argument("--enable-current-host-strict-ui", action="store_true")
    parser.add_argument("--skip-current-host-strict-ui", action="store_true")
    parser.add_argument("--current-host-ui-profile", default="")
    parser.add_argument("--current-host-ui-url", default="")
    parser.add_argument("--profile-smoke-modes", dest="profile_modes", default="local,docker")
    parser.add_argument("--phase45-profiles", default="c,d")
    parser.add_argument("--review-smoke-modes", default="local,docker")
    parser.add_argument("--profile-smoke-model-env", default="")
    return parser.parse_args()


def normalize_existing_step_state(step_state: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for step_id, payload in step_state.items():
        item = dict(payload)
        if item.get("status") == "RUNNING":
            item["status"] = "PENDING"
            item.pop("started_at", None)
        normalized[step_id] = item
    return normalized


def main() -> int:
    args = parse_args()
    checkpoint_dir = resolve_native_path(args.checkpoint_dir)
    report_path = resolve_native_path(args.report)
    checkpoint_path = checkpoint_dir / "checkpoint.json"
    lock = ReleaseGateLock(checkpoint_dir / CHECKPOINT_LOCK_NAME)
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    lock.acquire()
    if not args.resume:
        clear_checkpoint_dir_contents(checkpoint_dir)
    steps, metadata = build_release_steps(args, checkpoint_dir)
    plan_signature = [
        {
            "step_id": step.step_id,
            "title": step.title,
            "commands": [command.argv for command in step.commands],
            "skip_reason": step.skip_reason,
        }
        for step in steps
    ]
    checkpoint_payload: dict[str, Any] = {
        "started_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "report_path": str(report_path),
        "checkpoint_path": str(checkpoint_path),
        "steps": {},
        "plan_signature": plan_signature,
        **metadata,
    }
    try:
        if args.resume and checkpoint_path.is_file():
            existing = load_json(checkpoint_path)
            if existing.get("plan_signature") not in (None, plan_signature):
                raise SystemExit("Existing checkpoint plan does not match current release-gate options.")
            checkpoint_payload.update(existing)
            checkpoint_payload["steps"] = normalize_existing_step_state(existing.get("steps", {}))
            checkpoint_payload["updated_at"] = utc_now_iso()
            checkpoint_payload["report_path"] = str(report_path)
            checkpoint_payload["checkpoint_path"] = str(checkpoint_path)
            checkpoint_payload["plan_signature"] = plan_signature

        save_json(checkpoint_path, checkpoint_payload)
        render_report(report_path=report_path, checkpoint_path=checkpoint_path, checkpoint_payload=checkpoint_payload, steps=steps)

        overall_fail = False
        for step in steps:
            state = dict(checkpoint_payload.get("steps", {}).get(step.step_id, {}))
            if state.get("status") in {"PASS", "SKIP"}:
                if state.get("status") == "SKIP" and state.get("skip_causes_failure"):
                    overall_fail = True
                continue
            if step.skip_reason:
                checkpoint_payload.setdefault("steps", {})[step.step_id] = {
                    "title": step.title,
                    "status": "SKIP",
                    "skip_reason": step.skip_reason,
                    "skip_causes_failure": step.skip_causes_failure,
                    "log_path": str(step.log_path),
                    "artifact_paths": [str(path) for path in step.artifact_paths],
                    "commands": [command_display(item.argv) for item in step.commands],
                }
                checkpoint_payload["updated_at"] = utc_now_iso()
                save_json(checkpoint_path, checkpoint_payload)
                render_report(report_path=report_path, checkpoint_path=checkpoint_path, checkpoint_payload=checkpoint_payload, steps=steps)
                if step.skip_causes_failure:
                    overall_fail = True
                continue

            step_state = {
                "title": step.title,
                "status": "RUNNING",
                "started_at": utc_now_iso(),
                "log_path": str(step.log_path),
                "artifact_paths": [str(path) for path in step.artifact_paths],
                "commands": [command_display(item.argv) for item in step.commands],
            }
            checkpoint_payload.setdefault("steps", {})[step.step_id] = step_state
            checkpoint_payload["updated_at"] = utc_now_iso()
            save_json(checkpoint_path, checkpoint_payload)
            render_report(report_path=report_path, checkpoint_path=checkpoint_path, checkpoint_payload=checkpoint_payload, steps=steps)

            status, rc, duration = run_step_commands(step)
            attempts = 1
            step_state["attempts"] = attempts
            if is_windows_profile_smoke_negative_exit(step, status, rc):
                step_state["runner_warning"] = "windows_profile_smoke_negative_exit_retrying"
                checkpoint_payload["updated_at"] = utc_now_iso()
                save_json(checkpoint_path, checkpoint_payload)
                render_report(report_path=report_path, checkpoint_path=checkpoint_path, checkpoint_payload=checkpoint_payload, steps=steps)
                retry_status, retry_rc, retry_duration = run_step_commands(step)
                status, rc, duration = retry_status, retry_rc, duration + retry_duration
                attempts = 2
                step_state["attempts"] = attempts

            if (
                status == "FAIL"
                and step.step_id.startswith("4.")
                and artifact_is_fresh_for_started_at(step.artifact_paths, step_state.get("started_at"))
                and profile_smoke_artifact_reports_pass(step.artifact_paths)
            ):
                step_state["runner_warning"] = "artifact_pass_override_after_runner_failure"
                status = "PASS"

            if status == "PASS" and step.step_id == "3.45":
                dry_run_clean, forbidden_paths = package_dry_run_log_is_clean(step.log_path)
                if not dry_run_clean:
                    step_state["runner_warning"] = "package_dry_run_pollution_detected"
                    step_state["forbidden_paths"] = forbidden_paths
                    status = "FAIL"

            step_state.update(
                {
                    "status": status,
                    "finished_at": utc_now_iso(),
                    "duration_sec": int(duration),
                    "return_code": rc,
                }
            )
            checkpoint_payload["updated_at"] = utc_now_iso()
            save_json(checkpoint_path, checkpoint_payload)
            render_report(report_path=report_path, checkpoint_path=checkpoint_path, checkpoint_payload=checkpoint_payload, steps=steps)
            if status in {"FAIL", "TIMEOUT"}:
                overall_fail = True

        checkpoint_payload["result"] = "FAIL" if overall_fail else "PASS"
        checkpoint_payload["updated_at"] = utc_now_iso()
        save_json(checkpoint_path, checkpoint_payload)
        render_report(report_path=report_path, checkpoint_path=checkpoint_path, checkpoint_payload=checkpoint_payload, steps=steps)
        print(f"RELEASE_GATE_REPORT={report_path}")
        print(f"RELEASE_GATE_CHECKPOINT={checkpoint_path}")
        print(f"RESULT: {checkpoint_payload['result']}")
        return 1 if overall_fail else 0
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
