#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import json
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import openclaw_memory_palace_installer as installer
import openclaw_memory_palace_profile_smoke as smoke
import test_openclaw_profile_matrix_latest as matrix


def default_openclaw_bin_value() -> str:
    explicit = str(os.environ.get("OPENCLAW_BIN") or "").strip()
    if explicit:
        return explicit
    return shutil.which("openclaw") or "openclaw"


DEFAULT_OPENCLAW_BIN = default_openclaw_bin_value()
DEFAULT_PROFILES = ("c", "d")
DEFAULT_REPORT = (
    REPO_ROOT
    / ".tmp"
    / "openclaw-onboarding-apply-validate"
    / "onboarding-apply-validate.json"
)
DEFAULT_MARKDOWN = (
    REPO_ROOT
    / ".tmp"
    / "openclaw-onboarding-apply-validate"
    / "onboarding-apply-validate.md"
)
DEFAULT_WORKDIR = Path(tempfile.gettempdir()) / "openclaw-onboarding-apply-validate" / "runs"
FLAG_KEYS = (
    "SEARCH_DEFAULT_MODE",
    "RETRIEVAL_EMBEDDING_BACKEND",
    "RETRIEVAL_EMBEDDING_MODEL",
    "RETRIEVAL_EMBEDDING_DIM",
    "RETRIEVAL_RERANKER_ENABLED",
    "RETRIEVAL_RERANKER_MODEL",
    "WRITE_GUARD_LLM_ENABLED",
    "COMPACT_GIST_LLM_ENABLED",
    "INTENT_LLM_ENABLED",
    "OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED",
    "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE",
)


@dataclass(frozen=True)
class OnboardingCase:
    profile: str
    require_retrieval: bool
    require_llm_suite: bool

    @property
    def name(self) -> str:
        return f"profile-{self.profile}"

    def as_profile_case(self) -> matrix.ProfileCase:
        return matrix.ProfileCase(
            name=self.name,
            requested_profile=self.profile,
            require_retrieval=self.require_retrieval,
            require_llm_suite=self.require_llm_suite,
        )


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run onboarding --apply --validate as a black-box E2E under isolated "
            "OpenClaw config/state/setup-root directories."
        )
    )
    parser.add_argument(
        "--openclaw-bin",
        default=str(matrix.resolve_openclaw_bin(default_openclaw_bin_value())),
        help="OpenClaw binary or wrapper path.",
    )
    parser.add_argument(
        "--profiles",
        default=",".join(DEFAULT_PROFILES),
        help="Comma-separated profiles to test. Default: c,d",
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--markdown", default=str(DEFAULT_MARKDOWN))
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    parser.add_argument(
        "--model-env",
        default="",
        help="Optional env file whose provider settings should be loaded for this run.",
    )
    parser.add_argument(
        "--cleanup-case-roots",
        action="store_true",
        help="Delete per-profile temp roots after writing the top-level report.",
    )
    return parser.parse_args()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_profiles(raw: str) -> list[OnboardingCase]:
    values = [item.strip().lower() for item in str(raw or "").split(",") if item.strip()]
    if not values:
        raise ValueError("Profile list is empty.")
    invalid = [item for item in values if item not in {"a", "b", "c", "d"}]
    if invalid:
        raise ValueError(f"Unsupported profile(s): {', '.join(invalid)}")
    return [
        OnboardingCase(
            profile=value,
            require_retrieval=value in {"c", "d"},
            require_llm_suite=value == "d",
        )
        for value in values
    ]


def resolve_model_env_path(raw: str | None) -> Path | None:
    rendered = str(raw or "").strip()
    if not rendered:
        return None
    path = Path(rendered).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Model env file does not exist: {path}")
    return path


@contextmanager
def temporary_env(overrides: dict[str, str]) -> Any:
    if not overrides:
        yield
        return
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def build_case_paths(case_root: Path) -> dict[str, Path]:
    paths = {
        "caseRoot": case_root,
        "homeDir": case_root / "home",
        "stateDir": case_root / "state",
        "workspaceDir": case_root / "workspace",
        "setupRoot": case_root / "memory-palace",
        "configPath": case_root / "openclaw.json",
        "envFile": case_root / "runtime.env",
        "databasePath": case_root / "data" / "memory-palace.db",
    }
    for key in ("homeDir", "stateDir", "workspaceDir", "setupRoot"):
        paths[key].mkdir(parents=True, exist_ok=True)
    paths["databasePath"].parent.mkdir(parents=True, exist_ok=True)
    credentials_dir = paths["stateDir"] / "credentials"
    credentials_dir.mkdir(parents=True, exist_ok=True)
    for candidate in (paths["stateDir"], credentials_dir):
        try:
            candidate.chmod(0o700)
        except OSError:
            pass
    return paths


def collect_env_flags(env_values: dict[str, str]) -> dict[str, str | None]:
    return {key: env_values.get(key) for key in FLAG_KEYS}


def provider_probe_ok(payload: dict[str, Any]) -> bool:
    status = str(payload.get("summaryStatus") or payload.get("status") or "").strip().lower()
    if status != "pass":
        return False
    return bool(payload.get("ok", True))


def command_ok(payload: dict[str, Any]) -> bool:
    return matrix.command_ok(payload)


def summarize_json_record(record: dict[str, Any], *, payload_ok: bool | None = None) -> dict[str, Any]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    ok = payload_ok
    if ok is None:
        ok = bool(record.get("exitCode") == 0 and command_ok(payload))
    return {
        "ok": ok,
        "exitCode": int(record.get("exitCode") or 0),
        "durationSeconds": record.get("durationSeconds"),
        "summary": (
            payload.get("summaryMessage")
            or payload.get("summary")
            or payload.get("message")
            or payload.get("error")
        ),
        "payload": payload,
        "parseError": record.get("parseError"),
        "error": record.get("error"),
    }


def run_python_json_command(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    started = time.time()
    completed = matrix.run(command, env=env, timeout=timeout)
    payload: dict[str, Any]
    parse_error = None
    try:
        payload = matrix.read_json_payload(completed.stdout, completed.stderr)
    except Exception as exc:  # noqa: BLE001
        parse_error = str(exc)
        payload = {
            "ok": False,
            "summary": (completed.stderr or completed.stdout or str(exc)).strip() or "command produced no JSON payload",
        }
    return {
        "command": command,
        "exitCode": completed.returncode,
        "durationSeconds": round(time.time() - started, 3),
        "payload": payload,
        "parseError": parse_error,
    }


def run_plugin_json_command(
    command: list[str],
    *,
    config_path: Path,
    state_dir: Path,
    timeout: int,
) -> dict[str, Any]:
    started = time.time()
    try:
        payload = smoke.run_openclaw_json_command(
            command,
            config_path=config_path,
            state_dir=state_dir,
            timeout=timeout,
        )
        return {
            "command": command,
            "exitCode": 0,
            "durationSeconds": round(time.time() - started, 3),
            "payload": payload,
            "error": None,
            "parseError": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "command": command,
            "exitCode": 1,
            "durationSeconds": round(time.time() - started, 3),
            "payload": {
                "ok": False,
                "summary": str(exc),
            },
            "error": str(exc),
            "parseError": None,
        }


def build_bootstrap_status_command(paths: dict[str, Path]) -> list[str]:
    return [
        sys.executable,
        str(REPO_ROOT / "scripts" / "openclaw_memory_palace.py"),
        "bootstrap-status",
        "--config",
        str(paths["configPath"]),
        "--setup-root",
        str(paths["setupRoot"]),
        "--env-file",
        str(paths["envFile"]),
        "--json",
    ]


def build_provider_probe_command(
    case: OnboardingCase,
    paths: dict[str, Path],
    provider_flags: dict[str, str],
) -> list[str]:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "openclaw_memory_palace.py"),
        "provider-probe",
        "--config",
        str(paths["configPath"]),
        "--setup-root",
        str(paths["setupRoot"]),
        "--env-file",
        str(paths["envFile"]),
        "--mode",
        "basic",
        "--profile",
        case.profile,
        "--transport",
        "stdio",
        "--json",
    ]
    for flag_name, value in provider_flags.items():
        command.extend([f"--{flag_name.replace('_', '-')}", value])
    return command


def build_onboarding_command(
    case: OnboardingCase,
    paths: dict[str, Path],
    provider_flags: dict[str, str],
    *,
    openclaw_bin: str,
) -> list[str]:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "openclaw_memory_palace.py"),
        "onboarding",
        "--config",
        str(paths["configPath"]),
        "--setup-root",
        str(paths["setupRoot"]),
        "--env-file",
        str(paths["envFile"]),
        "--database-path",
        str(paths["databasePath"]),
        "--mode",
        "basic",
        "--profile",
        case.profile,
        "--transport",
        "stdio",
        "--strict-profile",
        "--apply",
        "--validate",
        "--json",
        "--openclaw-bin",
        openclaw_bin,
    ]
    for flag_name, value in provider_flags.items():
        command.extend([f"--{flag_name.replace('_', '-')}", value])
    return command


def extract_setup_summary(onboarding_payload: dict[str, Any]) -> dict[str, Any]:
    setup_payload = onboarding_payload.get("appliedSetup") if isinstance(onboarding_payload.get("appliedSetup"), dict) else {}
    validation_payload = setup_payload.get("validation") if isinstance(setup_payload.get("validation"), dict) else {}
    setup_ok = bool(setup_payload.get("ok")) if "ok" in setup_payload else bool(setup_payload)
    return {
        "ok": setup_ok,
        "summary": setup_payload.get("summary"),
        "requestedProfile": setup_payload.get("requested_profile"),
        "effectiveProfile": setup_payload.get("effective_profile"),
        "warnings": setup_payload.get("warnings") if isinstance(setup_payload.get("warnings"), list) else [],
        "actions": setup_payload.get("actions") if isinstance(setup_payload.get("actions"), list) else [],
        "nextSteps": setup_payload.get("next_steps") if isinstance(setup_payload.get("next_steps"), list) else [],
        "configPath": setup_payload.get("config_path"),
        "setupRoot": setup_payload.get("setup_root"),
        "envFile": setup_payload.get("env_file"),
        "payload": setup_payload,
        "validation": {
            "ok": bool(validation_payload.get("ok")),
            "failedStep": validation_payload.get("failed_step"),
            "steps": validation_payload.get("steps") if isinstance(validation_payload.get("steps"), list) else [],
            "payload": validation_payload,
        },
    }


def run_case(
    *,
    case: OnboardingCase,
    openclaw_bin: str,
    host_version: str,
    host_config: dict[str, Any],
    provider: matrix.ProviderConfig,
    workdir: Path,
) -> dict[str, Any]:
    case_root = Path(tempfile.mkdtemp(prefix=f"onboarding-{case.profile}-", dir=str(workdir)))
    paths = build_case_paths(case_root)
    matrix.write_json(
        paths["configPath"],
        matrix.minimal_host_config(host_config, workspace_dir=paths["workspaceDir"], include_acl_agents=False),
    )
    host_env = matrix.host_env(
        openclaw_bin=openclaw_bin,
        home_dir=paths["homeDir"],
        state_dir=paths["stateDir"],
        config_path=paths["configPath"],
    )
    provider_flags, provider_trace = matrix.resolve_provider_flags(case.as_profile_case(), provider)

    bootstrap_before = run_python_json_command(
        build_bootstrap_status_command(paths),
        env=host_env,
        timeout=120,
    )
    provider_probe_record = run_python_json_command(
        build_provider_probe_command(case, paths, provider_flags),
        env=host_env,
        timeout=180,
    )
    onboarding_record = run_python_json_command(
        build_onboarding_command(case, paths, provider_flags, openclaw_bin=openclaw_bin),
        env=host_env,
        timeout=900,
    )
    bootstrap_after = run_python_json_command(
        build_bootstrap_status_command(paths),
        env=host_env,
        timeout=120,
    )

    onboarding_payload = onboarding_record["payload"] if isinstance(onboarding_record.get("payload"), dict) else {}
    setup_summary = extract_setup_summary(onboarding_payload)
    env_values = installer.load_env_file(paths["envFile"])
    env_flags = collect_env_flags(env_values)
    provider_probe_payload = (
        provider_probe_record["payload"]
        if isinstance(provider_probe_record.get("payload"), dict)
        else {}
    )

    verify_record = run_plugin_json_command(
        smoke.openclaw_command("memory-palace", "verify", "--json", explicit_bin=openclaw_bin),
        config_path=paths["configPath"],
        state_dir=paths["stateDir"],
        timeout=180,
    )
    doctor_record = run_plugin_json_command(
        smoke.openclaw_command("memory-palace", "doctor", "--json", explicit_bin=openclaw_bin),
        config_path=paths["configPath"],
        state_dir=paths["stateDir"],
        timeout=180,
    )
    smoke_record = run_plugin_json_command(
        smoke.openclaw_command("memory-palace", "smoke", "--json", explicit_bin=openclaw_bin),
        config_path=paths["configPath"],
        state_dir=paths["stateDir"],
        timeout=300,
    )

    effective_profile = str(
        setup_summary.get("effectiveProfile")
        or bootstrap_after.get("payload", {}).get("setup", {}).get("effectiveProfile")
        or ""
    ).strip().lower()
    report = {
        "profile": case.profile,
        "requestedProfile": case.profile,
        "effectiveProfile": effective_profile,
        "hostVersion": host_version,
        "providerTrace": provider_trace,
        "commands": {
            "bootstrapStatus": build_bootstrap_status_command(paths),
            "providerProbe": build_provider_probe_command(case, paths, provider_flags),
            "onboarding": build_onboarding_command(case, paths, provider_flags, openclaw_bin=openclaw_bin),
            "verify": smoke.openclaw_command("memory-palace", "verify", "--json", explicit_bin=openclaw_bin),
            "doctor": smoke.openclaw_command("memory-palace", "doctor", "--json", explicit_bin=openclaw_bin),
            "smoke": smoke.openclaw_command("memory-palace", "smoke", "--json", explicit_bin=openclaw_bin),
        },
        "bootstrapStatusBefore": summarize_json_record(bootstrap_before),
        "providerProbe": summarize_json_record(
            provider_probe_record,
            payload_ok=bool(provider_probe_record.get("exitCode") == 0 and provider_probe_ok(provider_probe_payload)),
        ),
        "onboarding": summarize_json_record(
            onboarding_record,
            payload_ok=bool(onboarding_record.get("exitCode") == 0 and onboarding_payload.get("ok")),
        ),
        "setup": setup_summary,
        "postApplyValidation": setup_summary.get("validation"),
        "verify": summarize_json_record(verify_record),
        "doctor": summarize_json_record(doctor_record),
        "smoke": summarize_json_record(smoke_record),
        "bootstrapStatusAfter": summarize_json_record(bootstrap_after),
        "envFlags": env_flags,
        "artifacts": {
            "caseRoot": str(paths["caseRoot"]),
            "homeDir": str(paths["homeDir"]),
            "stateDir": str(paths["stateDir"]),
            "workspaceDir": str(paths["workspaceDir"]),
            "configPath": str(paths["configPath"]),
            "setupRoot": str(paths["setupRoot"]),
            "envFile": str(paths["envFile"]),
            "databasePath": str(paths["databasePath"]),
            "caseReport": str(paths["caseRoot"] / "case-report.json"),
        },
    }
    report["effectiveProfileMatchesRequested"] = bool(
        effective_profile and effective_profile == case.profile
    )
    report["allChecksPassed"] = all(
        [
            report["providerProbe"]["ok"],
            report["onboarding"]["ok"],
            bool(report["setup"].get("ok")),
            bool(report["postApplyValidation"].get("ok")),
            report["verify"]["ok"],
            report["doctor"]["ok"],
            report["smoke"]["ok"],
            report["effectiveProfileMatchesRequested"],
        ]
    )
    write_json(paths["caseRoot"] / "case-report.json", report)
    print(
        f"[onboarding-apply-validate] {case.name} done "
        f"pass={report['allChecksPassed']} effective={effective_profile or 'unknown'}",
        flush=True,
    )
    return report


def build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Onboarding Apply Validate E2E",
        "",
        f"- generated_at: `{report.get('generatedAt', '')}`",
        f"- openclaw_bin: `{report.get('openclawBin', '')}`",
        f"- host_version: `{report.get('hostVersion', '')}`",
        f"- workdir: `{report.get('workdir', '')}`",
        f"- profiles: `{report.get('profilesLabel', '')}`",
        f"- all_checks_passed: `{'yes' if report.get('allChecksPassed') else 'no'}`",
        "",
        "## Summary",
        "",
        "| Profile | Effective | Probe | Onboarding | Setup | Validation | Verify | Doctor | Smoke | All checks |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in report.get("profiles", []):
        lines.append(
            f"| {str(case.get('profile') or '').upper()} | "
            f"{str(case.get('effectiveProfile') or '').upper()} | "
            f"{'ok' if case.get('providerProbe', {}).get('ok') else 'fail'} | "
            f"{'ok' if case.get('onboarding', {}).get('ok') else 'fail'} | "
            f"{'ok' if case.get('setup', {}).get('ok') else 'fail'} | "
            f"{'ok' if case.get('postApplyValidation', {}).get('ok') else 'fail'} | "
            f"{'ok' if case.get('verify', {}).get('ok') else 'fail'} | "
            f"{'ok' if case.get('doctor', {}).get('ok') else 'fail'} | "
            f"{'ok' if case.get('smoke', {}).get('ok') else 'fail'} | "
            f"{'yes' if case.get('allChecksPassed') else 'no'} |"
        )

    for case in report.get("profiles", []):
        lines.extend(
            [
                "",
                f"## Profile {str(case.get('profile') or '').upper()}",
                "",
                f"- requested/effective: `{case.get('requestedProfile')} -> {case.get('effectiveProfile')}`",
                f"- provider_probe: `{case.get('providerProbe', {}).get('summary') or 'n/a'}`",
                f"- onboarding: `{case.get('onboarding', {}).get('summary') or 'n/a'}`",
                f"- setup: `{case.get('setup', {}).get('summary') or 'n/a'}`",
                f"- validation_failed_step: `{case.get('postApplyValidation', {}).get('failedStep') or ''}`",
                f"- env_flags: `{json.dumps(case.get('envFlags', {}), ensure_ascii=False)}`",
                f"- artifacts: `{json.dumps(case.get('artifacts', {}), ensure_ascii=False)}`",
            ]
        )
        validation_steps = case.get("postApplyValidation", {}).get("steps")
        if isinstance(validation_steps, list) and validation_steps:
            lines.append("- validation steps:")
            for step in validation_steps:
                lines.append(
                    f"  - `{step.get('name')}`: "
                    f"`{'ok' if step.get('ok') else 'fail'}` "
                    f"({step.get('summary') or ''})"
                )
        for step_name in ("verify", "doctor", "smoke"):
            step = case.get(step_name, {})
            lines.append(
                f"- {step_name}: "
                f"`{'ok' if step.get('ok') else 'fail'}` "
                f"({step.get('summary') or 'no summary'})"
            )
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for item in report["errors"]:
            lines.append(f"- `{item.get('profile')}`: {item.get('error')}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    cases = normalize_profiles(args.profiles)
    model_env_path = resolve_model_env_path(getattr(args, "model_env", ""))
    openclaw_bin = matrix.resolve_openclaw_bin(args.openclaw_bin)
    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)

    host_env = matrix.real_host_env(openclaw_bin)
    host_version = matrix.host_version(openclaw_bin, env=host_env)
    host_config = matrix.load_host_config(openclaw_bin, env=host_env)
    provider_env = installer.load_env_file(model_env_path) if model_env_path else {}
    with temporary_env(provider_env):
        provider = matrix.load_provider_config()

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for case in cases:
        try:
            results.append(
                run_case(
                    case=case,
                    openclaw_bin=openclaw_bin,
                    host_version=host_version,
                    host_config=host_config,
                    provider=provider,
                    workdir=workdir,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append({"profile": case.profile, "error": str(exc)})
            print(
                f"[onboarding-apply-validate] {case.name} failed: {exc}",
                file=sys.stderr,
                flush=True,
            )

    report = {
        "suite": "onboarding-apply-validate-e2e",
        "generatedAt": utc_now_iso(),
        "openclawBin": openclaw_bin,
        "hostVersion": host_version,
        "workdir": str(workdir),
        "modelEnvPath": str(model_env_path) if model_env_path else "",
        "profilesLabel": ",".join(case.profile for case in cases),
        "profiles": results,
        "errors": errors,
    }
    report["allChecksPassed"] = bool(results) and all(
        bool(item.get("allChecksPassed")) for item in results
    ) and not errors

    report_path = Path(args.report).expanduser().resolve()
    markdown_path = Path(args.markdown).expanduser().resolve()
    write_json(report_path, report)
    ensure_parent(markdown_path)
    markdown_path.write_text(build_markdown(report), encoding="utf-8")

    if args.cleanup_case_roots:
        for item in results:
            case_root = item.get("artifacts", {}).get("caseRoot")
            if case_root:
                shutil.rmtree(Path(str(case_root)), ignore_errors=True)

    print(
        json.dumps(
            {
                "ok": report["allChecksPassed"],
                "summary": (
                    "Onboarding apply validate E2E passed."
                    if report["allChecksPassed"]
                    else "Onboarding apply validate E2E completed with failures."
                ),
                "report": str(report_path),
                "markdown": str(markdown_path),
                "profiles": [item["profile"] for item in results],
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["allChecksPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
