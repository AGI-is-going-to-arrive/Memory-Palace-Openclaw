#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
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


DEFAULT_OPENCLAW_BIN = shutil.which("openclaw") or "openclaw"
DEFAULT_SEQUENCE = ("b", "c", "b", "d")
DEFAULT_REPORT = (
    REPO_ROOT
    / ".tmp"
    / "openclaw-profile-switch-acceptance"
    / "profile-switch-acceptance.json"
)
DEFAULT_MARKDOWN = (
    REPO_ROOT
    / ".tmp"
    / "openclaw-profile-switch-acceptance"
    / "profile-switch-acceptance.md"
)
DEFAULT_WORKDIR = Path(tempfile.gettempdir()) / "openclaw-profile-switch-acceptance" / "runs"
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
PRESENCE_KEYS = {
    "embeddingApiBasePresent": "RETRIEVAL_EMBEDDING_API_BASE",
    "rerankerApiBasePresent": "RETRIEVAL_RERANKER_API_BASE",
    "writeGuardLlmApiBasePresent": "WRITE_GUARD_LLM_API_BASE",
    "compactGistLlmApiBasePresent": "COMPACT_GIST_LLM_API_BASE",
    "intentLlmApiBasePresent": "INTENT_LLM_API_BASE",
}


@dataclass(frozen=True)
class SwitchStep:
    index: int
    requested_profile: str

    @property
    def name(self) -> str:
        return f"step-{self.index}-{self.requested_profile}"

    @property
    def require_retrieval(self) -> bool:
        return self.requested_profile in {"c", "d"}

    @property
    def require_llm_suite(self) -> bool:
        return self.requested_profile == "d"

    def as_profile_case(self) -> matrix.ProfileCase:
        return matrix.ProfileCase(
            name=self.name,
            requested_profile=self.requested_profile,
            require_retrieval=self.require_retrieval,
            require_llm_suite=self.require_llm_suite,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run sequential OpenClaw Memory Palace profile switches in one shared "
            "temporary runtime root and report setup/verify/doctor/smoke results."
        )
    )
    parser.add_argument(
        "--openclaw-bin",
        default=str(matrix.resolve_openclaw_bin(str(DEFAULT_OPENCLAW_BIN))),
        help="OpenClaw binary or wrapper path.",
    )
    parser.add_argument(
        "--sequence",
        default=",".join(DEFAULT_SEQUENCE),
        help="Comma-separated profile switch sequence. Default: b,c,b,d",
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--markdown", default=str(DEFAULT_MARKDOWN))
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    parser.add_argument(
        "--cleanup-shared-root",
        action="store_true",
        help="Delete the shared temporary runtime root after a successful run.",
    )
    return parser.parse_args()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def normalize_sequence(raw: str) -> list[SwitchStep]:
    values = [item.strip().lower() for item in str(raw or "").split(",") if item.strip()]
    if not values:
        raise ValueError("Profile sequence is empty.")
    invalid = [item for item in values if item not in {"a", "b", "c", "d"}]
    if invalid:
        raise ValueError(f"Unsupported profile(s) in sequence: {', '.join(invalid)}")
    return [SwitchStep(index=index, requested_profile=value) for index, value in enumerate(values, start=1)]


def bool_from_env(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def non_blank(raw: str | None) -> str:
    return str(raw or "").strip()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_shared_paths(workdir: Path) -> dict[str, Path]:
    workdir.mkdir(parents=True, exist_ok=True)
    shared_root = Path(tempfile.mkdtemp(prefix="profile-switch-", dir=str(workdir)))
    paths = {
        "sharedRoot": shared_root,
        "homeDir": shared_root / "home",
        "stateDir": shared_root / "state",
        "workspaceDir": shared_root / "workspace",
        "setupRoot": shared_root / "memory-palace",
        "configPath": shared_root / "openclaw.json",
        "envFile": shared_root / "runtime.env",
        "databasePath": shared_root / "data" / "memory-palace.db",
    }
    for directory_key in ("homeDir", "stateDir", "workspaceDir", "setupRoot"):
        paths[directory_key].mkdir(parents=True, exist_ok=True)
    paths["databasePath"].parent.mkdir(parents=True, exist_ok=True)
    credentials_dir = paths["stateDir"] / "credentials"
    credentials_dir.mkdir(parents=True, exist_ok=True)
    for candidate in (paths["stateDir"], credentials_dir):
        try:
            candidate.chmod(0o700)
        except OSError:
            pass
    return paths


def build_shared_host_config(*, config_path: Path, host_config: dict[str, Any], workspace_dir: Path) -> None:
    payload = matrix.minimal_host_config(
        host_config,
        workspace_dir=workspace_dir,
        include_acl_agents=False,
    )
    matrix.write_json(config_path, payload)


def build_setup_command(
    *,
    step: SwitchStep,
    paths: dict[str, Path],
    provider_flags: dict[str, str],
    reconfigure: bool,
) -> list[str]:
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "openclaw_memory_palace.py"),
        "setup",
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
        step.requested_profile,
        "--transport",
        "stdio",
        "--strict-profile",
        "--json",
    ]
    if reconfigure:
        command.append("--reconfigure")
    for flag_name, value in provider_flags.items():
        command.extend([f"--{flag_name.replace('_', '-')}", value])
    return command


def build_plugin_command(openclaw_bin: str, *args: str) -> list[str]:
    return smoke.openclaw_command(*args, explicit_bin=openclaw_bin)


def summarize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result") if isinstance(payload.get("result"), dict) else None
    summary = (
        payload.get("summary")
        or (result or {}).get("summary")
        or (result or {}).get("message")
        or payload.get("message")
    )
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        warnings = (result or {}).get("warnings")
    return {
        "ok": matrix.command_ok(payload),
        "summary": summary,
        "warnings": warnings if isinstance(warnings, list) else [],
        "payload": payload,
    }


def collect_env_flags(env_values: dict[str, str]) -> dict[str, Any]:
    snapshot = {key: env_values.get(key) for key in FLAG_KEYS}
    for output_key, env_key in PRESENCE_KEYS.items():
        snapshot[output_key] = bool(non_blank(env_values.get(env_key)))
    return snapshot


def detect_contamination(
    *,
    step: SwitchStep,
    effective_profile: str,
    env_flags: dict[str, Any],
    previous_step: dict[str, Any] | None,
) -> dict[str, Any]:
    issues: list[str] = []
    current_profile = str(effective_profile or "").strip().lower()
    search_mode = str(env_flags.get("SEARCH_DEFAULT_MODE") or "").strip().lower()
    embedding_backend = str(env_flags.get("RETRIEVAL_EMBEDDING_BACKEND") or "").strip().lower()
    reranker_enabled = bool_from_env(str(env_flags.get("RETRIEVAL_RERANKER_ENABLED") or ""))
    llm_enabled = any(
        bool_from_env(str(env_flags.get(key) or ""))
        for key in ("WRITE_GUARD_LLM_ENABLED", "COMPACT_GIST_LLM_ENABLED", "INTENT_LLM_ENABLED")
    )

    if current_profile != step.requested_profile:
        issues.append(
            f"effective profile mismatch: requested={step.requested_profile} effective={current_profile or 'unknown'}"
        )

    if step.requested_profile == "a":
        if search_mode and search_mode != "keyword":
            issues.append(f"profile A expected SEARCH_DEFAULT_MODE=keyword, got {search_mode}")
        if embedding_backend not in {"none", ""}:
            issues.append(f"profile A expected RETRIEVAL_EMBEDDING_BACKEND=none, got {embedding_backend}")
        if reranker_enabled:
            issues.append("profile A should not keep reranker enabled")
        if llm_enabled:
            issues.append("profile A should not keep LLM feature flags enabled")
    elif step.requested_profile == "b":
        if search_mode and search_mode != "hybrid":
            issues.append(f"profile B expected SEARCH_DEFAULT_MODE=hybrid, got {search_mode}")
        if embedding_backend not in {"hash", ""}:
            issues.append(f"profile B expected RETRIEVAL_EMBEDDING_BACKEND=hash, got {embedding_backend}")
        if reranker_enabled:
            issues.append("profile B should not keep reranker enabled")
        for presence_key in (
            "embeddingApiBasePresent",
            "rerankerApiBasePresent",
        ):
            if env_flags.get(presence_key):
                issues.append(f"profile B leaked advanced provider flag: {presence_key}")
    elif step.requested_profile == "c":
        if search_mode and search_mode != "hybrid":
            issues.append(f"profile C expected SEARCH_DEFAULT_MODE=hybrid, got {search_mode}")
        if embedding_backend not in {"api", "router"}:
            issues.append(f"profile C expected API/router embedding backend, got {embedding_backend}")
        if not reranker_enabled:
            issues.append("profile C expected reranker enabled")
        if not env_flags.get("embeddingApiBasePresent"):
            issues.append("profile C expected embedding API base to be present")
        if not env_flags.get("rerankerApiBasePresent"):
            issues.append("profile C expected reranker API base to be present")
    elif step.requested_profile == "d":
        if search_mode and search_mode != "hybrid":
            issues.append(f"profile D expected SEARCH_DEFAULT_MODE=hybrid, got {search_mode}")
        if embedding_backend not in {"api", "router"}:
            issues.append(f"profile D expected API/router embedding backend, got {embedding_backend}")
        if not reranker_enabled:
            issues.append("profile D expected reranker enabled")
        if not env_flags.get("embeddingApiBasePresent"):
            issues.append("profile D expected embedding API base to be present")
        if not env_flags.get("rerankerApiBasePresent"):
            issues.append("profile D expected reranker API base to be present")
        if not llm_enabled:
            issues.append("profile D expected advanced LLM feature flags to be enabled")

    previous_profile = None
    if previous_step:
        previous_profile = str(previous_step.get("effectiveProfile") or previous_step.get("requestedProfile") or "")
        previous_flags = previous_step.get("envFlags") if isinstance(previous_step.get("envFlags"), dict) else {}
        if step.requested_profile == "b" and str(previous_profile).lower() in {"c", "d"}:
            if env_flags.get("embeddingApiBasePresent"):
                issues.append("advanced embedding API base leaked after switching back to profile B")
            if env_flags.get("rerankerApiBasePresent"):
                issues.append("advanced reranker API base leaked after switching back to profile B")
        if step.requested_profile in {"c", "d"} and str(previous_profile).lower() == "b":
            previous_backend = str(previous_flags.get("RETRIEVAL_EMBEDDING_BACKEND") or "").strip().lower()
            if previous_backend == "hash" and embedding_backend == "hash":
                issues.append("hash embedding backend appears to have persisted after upgrading from profile B")

    return {
        "detected": bool(issues),
        "issues": issues,
        "previousEffectiveProfile": previous_profile,
    }


def ensure_seed_memory(env_values: dict[str, str], database_path: Path) -> None:
    database_url = str(env_values.get("DATABASE_URL") or "").strip()
    if not database_url:
        raise RuntimeError("runtime.env does not contain DATABASE_URL after setup")
    if database_path.exists() and database_path.stat().st_size > 0:
        return
    smoke.seed_local_memory(database_url, env_values=env_values)


def with_profile_llm_suite(provider_flags: dict[str, str], *, requested_profile: str) -> dict[str, str]:
    if requested_profile != "d":
        return dict(provider_flags)
    llm_base = non_blank(provider_flags.get("llm_api_base"))
    llm_key = non_blank(provider_flags.get("llm_api_key"))
    llm_model = non_blank(provider_flags.get("llm_model"))
    if not (llm_base and llm_model):
        return dict(provider_flags)
    merged = dict(provider_flags)
    if llm_base:
        merged.setdefault("write_guard_llm_api_base", llm_base)
        merged.setdefault("compact_gist_llm_api_base", llm_base)
    if llm_key:
        merged.setdefault("write_guard_llm_api_key", llm_key)
        merged.setdefault("compact_gist_llm_api_key", llm_key)
    if llm_model:
        merged.setdefault("write_guard_llm_model", llm_model)
        merged.setdefault("compact_gist_llm_model", llm_model)
    return merged


def run_step(
    *,
    step: SwitchStep,
    openclaw_bin: str,
    host_env: dict[str, str],
    paths: dict[str, Path],
    provider: matrix.ProviderConfig,
    previous_step: dict[str, Any] | None,
) -> dict[str, Any]:
    provider_flags, provider_trace = matrix.resolve_provider_flags(step.as_profile_case(), provider)
    provider_flags = with_profile_llm_suite(provider_flags, requested_profile=step.requested_profile)
    command = build_setup_command(
        step=step,
        paths=paths,
        provider_flags=provider_flags,
        reconfigure=step.index > 1,
    )
    started = time.time()
    setup_run = matrix.run(command, env=host_env, timeout=900)
    duration = round(time.time() - started, 3)
    if setup_run.returncode != 0:
        raise RuntimeError(
            f"{step.name} setup failed:\nSTDOUT:\n{setup_run.stdout}\nSTDERR:\n{setup_run.stderr}"
        )
    setup_payload = matrix.read_json_payload(setup_run.stdout, setup_run.stderr)
    env_values = installer.load_env_file(paths["envFile"])
    ensure_seed_memory(env_values, paths["databasePath"])

    verify_payload = smoke.run_openclaw_json_command(
        build_plugin_command(openclaw_bin, "memory-palace", "verify", "--json"),
        config_path=paths["configPath"],
        state_dir=paths["stateDir"],
        timeout=180,
    )
    doctor_payload = smoke.run_openclaw_json_command(
        build_plugin_command(openclaw_bin, "memory-palace", "doctor", "--json"),
        config_path=paths["configPath"],
        state_dir=paths["stateDir"],
        timeout=180,
    )
    smoke_payload = smoke.run_openclaw_json_command(
        build_plugin_command(openclaw_bin, "memory-palace", "smoke", "--json"),
        config_path=paths["configPath"],
        state_dir=paths["stateDir"],
        timeout=300,
    )
    effective_profile = str(
        setup_payload.get("effective_profile")
        or setup_payload.get("setup", {}).get("effectiveProfile")
        or setup_payload.get("effective_profile".upper())
        or step.requested_profile
    ).strip().lower()
    env_flags = collect_env_flags(env_values)
    contamination = detect_contamination(
        step=step,
        effective_profile=effective_profile,
        env_flags=env_flags,
        previous_step=previous_step,
    )
    verify_result = summarize_payload(verify_payload)
    doctor_result = summarize_payload(doctor_payload)
    smoke_result = summarize_payload(smoke_payload)
    setup_warnings = setup_payload.get("warnings")
    if not isinstance(setup_warnings, list):
        setup_warnings = []
    return {
        "index": step.index,
        "name": step.name,
        "requestedProfile": step.requested_profile,
        "effectiveProfile": effective_profile,
        "providerTrace": provider_trace,
        "setup": {
            "ok": True,
            "durationSeconds": duration,
            "summary": setup_payload.get("summary"),
            "warnings": setup_warnings,
            "payload": setup_payload,
        },
        "verify": verify_result,
        "doctor": doctor_result,
        "smoke": smoke_result,
        "envFlags": env_flags,
        "contamination": contamination,
        "allChecksPassed": (
            verify_result["ok"]
            and doctor_result["ok"]
            and smoke_result["ok"]
            and not contamination["detected"]
        ),
    }


def build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Profile Switch Acceptance E2E",
        "",
        f"- sequence: `{report.get('sequenceLabel', '')}`",
        f"- host version: `{report.get('hostVersion', '')}`",
        f"- shared root: `{report.get('sharedRoot', '')}`",
        f"- all checks passed: `{'yes' if report.get('allChecksPassed') else 'no'}`",
        f"- contamination detected: `{'yes' if report.get('contaminationDetected') else 'no'}`",
        "",
        "## Step Summary",
        "",
        "| Step | Requested | Effective | Setup | Verify | Doctor | Smoke | Contamination |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for step in report.get("steps", []):
        contamination = step.get("contamination") if isinstance(step.get("contamination"), dict) else {}
        lines.append(
            "| "
            f"{step.get('index')} | "
            f"{step.get('requestedProfile')} | "
            f"{step.get('effectiveProfile')} | "
            f"{'ok' if step.get('setup', {}).get('ok') else 'fail'} | "
            f"{'ok' if step.get('verify', {}).get('ok') else 'fail'} | "
            f"{'ok' if step.get('doctor', {}).get('ok') else 'fail'} | "
            f"{'ok' if step.get('smoke', {}).get('ok') else 'fail'} | "
            f"{'yes' if contamination.get('detected') else 'no'} |"
        )
    if report.get("errors"):
        lines.extend(["", "## Errors", ""])
        for item in report["errors"]:
            lines.append(f"- `{item.get('scope')}`: {item.get('error')}")
    for step in report.get("steps", []):
        lines.extend(
            [
                "",
                f"## Step {step.get('index')}: {str(step.get('requestedProfile') or '').upper()}",
                "",
                f"- requested/effective: `{step.get('requestedProfile')} -> {step.get('effectiveProfile')}`",
                f"- setup duration: `{step.get('setup', {}).get('durationSeconds')}` seconds",
                f"- provider trace: `{json.dumps(step.get('providerTrace', {}), ensure_ascii=False)}`",
                f"- env flags: `{json.dumps(step.get('envFlags', {}), ensure_ascii=False)}`",
                f"- verify ok: `{'yes' if step.get('verify', {}).get('ok') else 'no'}`",
                f"- doctor ok: `{'yes' if step.get('doctor', {}).get('ok') else 'no'}`",
                f"- smoke ok: `{'yes' if step.get('smoke', {}).get('ok') else 'no'}`",
            ]
        )
        contamination = step.get("contamination") if isinstance(step.get("contamination"), dict) else {}
        if contamination.get("detected"):
            lines.append("- contamination issues:")
            for issue in contamination.get("issues", []):
                lines.append(f"  - {issue}")
        else:
            lines.append("- contamination issues: none")
        warnings = step.get("setup", {}).get("warnings")
        if isinstance(warnings, list) and warnings:
            lines.append("- setup warnings:")
            for warning in warnings:
                lines.append(f"  - {warning}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    sequence = normalize_sequence(args.sequence)
    report_path = Path(args.report).expanduser().resolve()
    markdown_path = Path(args.markdown).expanduser().resolve()
    workdir = Path(args.workdir).expanduser().resolve()
    ensure_parent(report_path)
    ensure_parent(markdown_path)
    openclaw_bin = matrix.resolve_openclaw_bin(args.openclaw_bin)

    host_base_env = matrix.real_host_env(openclaw_bin)
    host_version = matrix.host_version(openclaw_bin, env=host_base_env)
    host_config = matrix.load_host_config(openclaw_bin, env=host_base_env)
    provider = matrix.load_provider_config()

    paths = build_shared_paths(workdir)
    build_shared_host_config(
        config_path=paths["configPath"],
        host_config=host_config,
        workspace_dir=paths["workspaceDir"],
    )
    step_host_env = matrix.host_env(
        openclaw_bin=openclaw_bin,
        home_dir=paths["homeDir"],
        state_dir=paths["stateDir"],
        config_path=paths["configPath"],
    )

    steps: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    aborted_at_step: int | None = None

    try:
        previous_step: dict[str, Any] | None = None
        for step in sequence:
            print(f"[profile-switch] {step.name} start", flush=True)
            try:
                result = run_step(
                    step=step,
                    openclaw_bin=openclaw_bin,
                    host_env=step_host_env,
                    paths=paths,
                    provider=provider,
                    previous_step=previous_step,
                )
            except Exception as exc:  # noqa: BLE001
                aborted_at_step = step.index
                errors.append({"scope": step.name, "error": str(exc)})
                steps.append(
                    {
                        "index": step.index,
                        "name": step.name,
                        "requestedProfile": step.requested_profile,
                        "effectiveProfile": None,
                        "setup": {"ok": False, "durationSeconds": 0.0, "summary": None, "warnings": []},
                        "verify": {"ok": False, "summary": None, "warnings": []},
                        "doctor": {"ok": False, "summary": None, "warnings": []},
                        "smoke": {"ok": False, "summary": None, "warnings": []},
                        "envFlags": {},
                        "contamination": {"detected": False, "issues": []},
                        "allChecksPassed": False,
                        "error": str(exc),
                    }
                )
                print(f"[profile-switch] {step.name} fail error={exc}", flush=True)
                break
            steps.append(result)
            previous_step = result
            print(
                f"[profile-switch] {step.name} done pass={result.get('allChecksPassed')} "
                f"effective={result.get('effectiveProfile')}",
                flush=True,
            )

        report = {
            "suite": "profile-switch-acceptance-e2e",
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sequence": [step.requested_profile for step in sequence],
            "sequenceLabel": " -> ".join(step.requested_profile for step in sequence),
            "hostVersion": host_version,
            "openclawBin": openclaw_bin,
            "sharedRoot": str(paths["sharedRoot"]),
            "paths": {key: str(value) for key, value in paths.items()},
            "steps": steps,
            "errors": errors,
            "abortedAtStep": aborted_at_step,
            "contaminationDetected": any(
                bool((step.get("contamination") or {}).get("detected")) for step in steps if isinstance(step, dict)
            ),
            "allChecksPassed": bool(
                steps
                and aborted_at_step is None
                and all(bool(step.get("allChecksPassed")) for step in steps)
            ),
        }
        write_json(report_path, report)
        markdown_path.write_text(build_markdown(report), encoding="utf-8")
        print(json.dumps({"report": str(report_path), "markdown": str(markdown_path)}, ensure_ascii=False), flush=True)
        return 0 if report["allChecksPassed"] else 1
    finally:
        if args.cleanup_shared_root and not errors and paths["sharedRoot"].exists():
            shutil.rmtree(paths["sharedRoot"], ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
