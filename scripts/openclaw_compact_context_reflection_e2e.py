#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import openclaw_command_new_e2e as command_new
import openclaw_memory_palace_profile_smoke as smoke

DEFAULT_REPORT_PATH = REPO_ROOT / ".tmp" / "openclaw_compact_context_reflection_e2e.json"


def openclaw_command(openclaw_bin: str, *args: str) -> list[str]:
    return smoke.openclaw_command(*args, explicit_bin=openclaw_bin)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a real OpenClaw compact_context reflection probe.",
    )
    parser.add_argument("--openclaw-bin", default=shutil.which("openclaw") or "openclaw")
    parser.add_argument("--model-env", default=str(smoke.DEFAULT_MODEL_ENV or ""))
    parser.add_argument("--profile", choices=("b", "c", "d"), default="c")
    parser.add_argument(
        "--probe",
        choices=("reflection", "high-value", "duplicate-high-value"),
        default="reflection",
    )
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    return parser.parse_args()


def run(command: list[str], *, env: dict[str, str], timeout: int = 600) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout=command_new._decode_output(completed.stdout),
        stderr=command_new._decode_output(completed.stderr),
    )


def resolve_current_openclaw_config(openclaw_bin: str) -> Path:
    result = run(
        openclaw_command(openclaw_bin, "config", "file"),
        env=os.environ.copy(),
        timeout=60,
    )
    command_new.ensure_success(result, context="openclaw config file")
    config_text = str(result.stdout or "").splitlines()[0].strip()
    return Path(config_text).expanduser().resolve()


def build_temp_openclaw_config(
    base_config_path: Path,
    runtime_env_path: Path,
    workspace_dir: Path,
) -> dict[str, Any]:
    payload = command_new.build_temp_openclaw_config(
        base_config_path, runtime_env_path, workspace_dir
    )
    config = payload["plugins"]["entries"]["memory-palace"]["config"]
    # Real C/D provider stacks can exceed the default host timeout during
    # compact_context reflection, especially when embedding falls back first.
    config["timeoutMs"] = 120000
    config["autoRecall"] = {"enabled": False}
    config["smartExtraction"] = {"enabled": False}
    config["reconcile"] = {"enabled": False}
    config["reflection"] = {
        "enabled": True,
        "autoRecall": False,
        "source": "compact_context",
        "rootUri": "core://reflection",
        "traceEnabled": True,
    }
    return payload


def extract_compact_context_runtime(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    runtime_state = payload.get("runtimeState")
    if not isinstance(runtime_state, dict):
        return None
    compact = runtime_state.get("lastCompactContext")
    return compact if isinstance(compact, dict) else None


def extract_flush_tracker_runtime(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    if not isinstance(status, dict):
        return None
    runtime = status.get("runtime")
    if not isinstance(runtime, dict):
        return None
    sm_lite = runtime.get("sm_lite")
    if not isinstance(sm_lite, dict):
        return None
    flush_tracker = sm_lite.get("flush_tracker")
    return flush_tracker if isinstance(flush_tracker, dict) else None


def reflection_uses_atomic_path(text: str) -> bool:
    rendered = str(text or "")
    return "- compact_source_hash:" in rendered and "- compact_source_uri:" not in rendered


def _high_value_queries(token: str, *, duplicate: bool) -> tuple[str, str]:
    first = f"remember workflow preference marker {token} for future recall"
    second = (
        first
        if duplicate
        else f"remember the default workflow marker {token} for this short preference session"
    )
    return first, second


def main() -> int:
    args = parse_args()
    report_path = Path(args.report).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    base_config_path = resolve_current_openclaw_config(args.openclaw_bin)

    tmp_root: Path | None = None
    try:
        tmp_root = Path(tempfile.mkdtemp(prefix="mp-compact-context-reflection-e2e-"))
        workspace_dir = tmp_root / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runtime_env_path = tmp_root / f"profile-{args.profile}.env"
        model_env = (
            smoke.load_env_file(Path(args.model_env).expanduser().resolve())
            if str(args.model_env or "").strip()
            else {}
        )
        smoke.build_profile_env(
            smoke.local_native_platform_name(),
            args.profile,
            runtime_env_path,
            model_env,
        )
        if args.probe in {"high-value", "duplicate-high-value"}:
            with runtime_env_path.open("a", encoding="utf-8") as handle:
                handle.write("RUNTIME_FLUSH_HIGH_VALUE_EARLY_ENABLED=true\n")
                handle.write("RUNTIME_FLUSH_HIGH_VALUE_MIN_EVENTS=2\n")
                handle.write("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS=120\n")
                handle.write("RUNTIME_FLUSH_HIGH_VALUE_MIN_CHARS_CJK=100\n")
                handle.write("RUNTIME_FLUSH_MIN_EVENTS=6\n")
                handle.write("RUNTIME_FLUSH_TRIGGER_CHARS=6000\n")

        config_payload = build_temp_openclaw_config(
            base_config_path, runtime_env_path, workspace_dir
        )
        config_path = tmp_root / "openclaw.json"
        config_path.write_text(
            json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        state_dir = tmp_root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
        env["OPENCLAW_STATE_DIR"] = str(state_dir)

        token = f"compact-reflection-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any]
        if args.probe == "reflection":
            seed_event = f"probe search '{token}' follow up tomorrow keep docs last"
            probe_result = command_new.parse_json_output(
                run(
                    openclaw_command(
                        args.openclaw_bin,
                        "memory-palace",
                        "probe-compact-reflection",
                        "--seed-event",
                        seed_event,
                        "--agent-key",
                        "main",
                        "--session-ref",
                        token,
                        "--json",
                    ),
                    env=env,
                    timeout=600,
                ),
                context="openclaw memory-palace probe-compact-reflection",
            )
            if not isinstance(probe_result, dict):
                raise RuntimeError(
                    f"probe-compact-reflection returned a non-object payload: {probe_result!r}"
                )
            result_payload = probe_result.get("result")
            if not isinstance(result_payload, dict) or result_payload.get("ok") is not True:
                raise RuntimeError(
                    f"probe-compact-reflection failed: {json.dumps(probe_result, ensure_ascii=False)}"
                )
            reflection_uri = str(probe_result.get("reflectionUri") or "").strip()
            if not reflection_uri.startswith("core://reflection/"):
                raise RuntimeError(
                    f"probe-compact-reflection did not return a reflection URI: {json.dumps(probe_result, ensure_ascii=False)}"
                )
            reflection_text = str(probe_result.get("reflectionTextExcerpt") or "")
            if "- source: compact_context" not in reflection_text:
                raise RuntimeError(
                    f"Missing compact_context source marker in probe output: {json.dumps(probe_result, ensure_ascii=False)}"
                )

            status_payload = command_new.parse_json_output(
                run(
                    openclaw_command(args.openclaw_bin, "memory-palace", "status", "--json"),
                    env=env,
                    timeout=600,
                ),
                context="openclaw memory-palace status",
            )
            compact_runtime = extract_compact_context_runtime(status_payload)

            payload = {
                "ok": True,
                "tmp_root": str(tmp_root),
                "config_path": str(config_path),
                "state_dir": str(state_dir),
                "runtime_env_path": str(runtime_env_path),
                "profile": args.profile,
                "probe": args.probe,
                "token": token,
                "seed_event": seed_event,
                "reflection_uri": reflection_uri,
                "used_atomic_path": reflection_uses_atomic_path(reflection_text),
                "runtime_last_compact_context": compact_runtime,
                "probe_result": probe_result,
            }
        else:
            duplicate = args.probe == "duplicate-high-value"
            first_query, second_query = _high_value_queries(token, duplicate=duplicate)
            probe_result = command_new.parse_json_output(
                run(
                    openclaw_command(
                        args.openclaw_bin,
                        "memory-palace",
                        "probe-high-value-flush",
                        "--first-query",
                        first_query,
                        "--second-query",
                        second_query,
                        "--reason",
                        f"host_level_{args.probe.replace('-', '_')}",
                        "--json",
                    ),
                    env=env,
                    timeout=600,
                ),
                context="openclaw memory-palace probe-high-value-flush",
            )
            if not isinstance(probe_result, dict):
                raise RuntimeError(
                    f"probe-high-value-flush returned a non-object payload: {probe_result!r}"
                )
            result_payload = probe_result.get("result")
            if not isinstance(result_payload, dict) or result_payload.get("ok") is not True:
                raise RuntimeError(
                    f"probe-high-value-flush failed: {json.dumps(probe_result, ensure_ascii=False)}"
                )
            flush_tracker_runtime = extract_flush_tracker_runtime(probe_result)
            if not isinstance(flush_tracker_runtime, dict):
                raise RuntimeError(
                    f"probe-high-value-flush did not include flush tracker runtime: {json.dumps(probe_result, ensure_ascii=False)}"
                )
            flushed = bool(result_payload.get("flushed"))
            early_flush_count = int(flush_tracker_runtime.get("early_flush_count") or 0)
            flush_results_total = int(flush_tracker_runtime.get("flush_results_total") or 0)
            if duplicate:
                if flushed or early_flush_count != 0 or flush_results_total != 0:
                    raise RuntimeError(
                        f"duplicate high-value probe should not flush: {json.dumps(probe_result, ensure_ascii=False)}"
                    )
            else:
                if not flushed or early_flush_count < 1 or flush_results_total < 1:
                    raise RuntimeError(
                        f"high-value probe did not trigger early flush: {json.dumps(probe_result, ensure_ascii=False)}"
                    )

            payload = {
                "ok": True,
                "tmp_root": str(tmp_root),
                "config_path": str(config_path),
                "state_dir": str(state_dir),
                "runtime_env_path": str(runtime_env_path),
                "profile": args.profile,
                "probe": args.probe,
                "token": token,
                "queries": [first_query, second_query],
                "flush_tracker_runtime": flush_tracker_runtime,
                "probe_result": probe_result,
            }
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "tmp_root": str(tmp_root) if tmp_root is not None else "",
            "error": str(exc),
        }
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    finally:
        if tmp_root is not None:
            command_new.cleanup_temp_root(tmp_root)


if __name__ == "__main__":
    raise SystemExit(main())
