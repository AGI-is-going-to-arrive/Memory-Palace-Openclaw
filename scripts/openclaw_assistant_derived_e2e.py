#!/usr/bin/env python3
from __future__ import annotations

import argparse
import functools
import json
import locale
import os
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import openclaw_memory_palace_installer as installer
import openclaw_memory_palace_profile_smoke as smoke
from openclaw_json_output import parse_json_process_output

FORCE_KILL_SIGNAL = smoke._force_kill_signal()

PLUGIN_ROOT = REPO_ROOT / "extensions" / "memory-palace"
DEFAULT_REPORT_PATH = REPO_ROOT / ".tmp" / "openclaw_assistant_derived_e2e.json"
TRANSIENT_AGENT_FAILURE_MARKERS = (
    "rate limit",
    "try again later",
    "temporarily unavailable",
    "service unavailable",
    "overloaded",
    "timeout",
    "timed out",
    "unexpected eof",
)


def openclaw_command(openclaw_bin: str, *args: str) -> list[str]:
    return smoke.openclaw_command(*args, explicit_bin=openclaw_bin)


@functools.lru_cache(maxsize=32)
def _command_supports_token_flag(
    openclaw_bin: str,
    command_key: tuple[str, ...],
) -> bool:
    if not command_key:
        return False

    help_command = openclaw_command(openclaw_bin, *command_key, "--help")
    try:
        result = subprocess.run(
            help_command,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        return False
    help_text = "\n".join(
        part for part in (result.stdout, result.stderr) if isinstance(part, str)
    )
    return "--token" in help_text


def command_with_gateway_token(
    openclaw_bin: str,
    *,
    env: dict[str, str],
    args: list[str],
) -> list[str]:
    command = openclaw_command(openclaw_bin, *args)
    if "--token" in args:
        return command

    gateway_url = str(env.get("OPENCLAW_GATEWAY_URL") or "").strip()
    gateway_token = str(env.get("OPENCLAW_GATEWAY_TOKEN") or "").strip()
    if not gateway_url or not gateway_token:
        return command

    if not args:
        return command
    head = args[0]
    if head not in {"agent", "memory-palace", "gateway"}:
        return command
    if head == "gateway" and len(args) >= 2 and args[1] == "run":
        return command
    if head == "gateway" and len(args) >= 2:
        command_key = (head, args[1])
    else:
        command_key = (head,)
    if not _command_supports_token_flag(openclaw_bin, command_key):
        return command
    return [*command, "--token", gateway_token]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a real OpenClaw assistant-derived phase-3 e2e probe.",
    )
    parser.add_argument("--openclaw-bin", default=shutil.which("openclaw") or "openclaw")
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    return parser.parse_args()


def ensure_success(result: subprocess.CompletedProcess[str], *, context: str) -> None:
    if result.returncode == 0:
        return
    raise RuntimeError(
        f"{context} failed:\n"
        f"COMMAND: {' '.join(result.args if isinstance(result.args, list) else [str(result.args)])}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )


def parse_json_output(result: subprocess.CompletedProcess[str], *, context: str) -> Any:
    return parse_json_process_output(result, context=context)


def run(
    command: list[str],
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    resolved_command = list(command)
    if len(command) >= 2:
        resolved_command = command_with_gateway_token(
            str(command[0]),
            env=env,
            args=[str(item) for item in command[1:]],
        )
    completed = subprocess.run(
        resolved_command,
        cwd=str(cwd),
        env=env,
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


def wait_for_gateway(
    openclaw_bin: str,
    url: str,
    *,
    env: dict[str, str],
    cwd: Path,
    timeout_seconds: float = 45.0,
) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_error = ""
    while time.monotonic() < deadline:
        command = openclaw_command(openclaw_bin, "gateway", "health", "--url", url)
        token = str(env.get("OPENCLAW_GATEWAY_TOKEN") or "").strip()
        if token:
            command.extend(["--token", token])
        result = run(command, env=env, cwd=cwd, timeout=30)
        if result.returncode == 0:
            return
        last_error = (result.stderr or result.stdout or "").strip()
        time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for gateway health at {url}: {last_error}")


def stop_gateway_process(gateway: subprocess.Popen[str]) -> None:
    if gateway.poll() is not None:
        return
    smoke.kill_process_group(gateway.pid, signal.SIGTERM)
    try:
        gateway.wait(timeout=15)
        return
    except subprocess.TimeoutExpired:
        smoke.kill_process_group(gateway.pid, FORCE_KILL_SIGNAL, force=True)
    try:
        gateway.wait(timeout=5)
    except subprocess.TimeoutExpired:
        gateway.kill()
        gateway.wait(timeout=5)


def resolve_current_openclaw_config(openclaw_bin: str) -> Path:
    def _usable_config(candidate: Path) -> bool:
        if not candidate.is_file():
            return False
        try:
            payload = installer.read_json_file(candidate)
        except Exception:  # noqa: BLE001
            return False
        if not isinstance(payload, dict):
            return False
        models = payload.get("models")
        if isinstance(models, dict) and isinstance(models.get("providers"), dict) and models["providers"]:
            return True
        agents = payload.get("agents")
        defaults = agents.get("defaults") if isinstance(agents, dict) else None
        model = defaults.get("model") if isinstance(defaults, dict) else None
        return isinstance(model, dict) and str(model.get("primary") or "").strip() != ""

    for env_name in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_CONFIG"):
        configured = str(os.environ.get(env_name) or "").strip()
        if configured:
            candidate = Path(configured).expanduser().resolve()
            if _usable_config(candidate):
                return candidate
    home_candidate = Path.home() / ".openclaw" / "openclaw.json"
    if _usable_config(home_candidate):
        return home_candidate
    result = run(openclaw_command(openclaw_bin, "config", "file"), env=os.environ.copy(), cwd=REPO_ROOT, timeout=60)
    ensure_success(result, context="openclaw config file")
    lines = [line.strip() for line in str(result.stdout or "").splitlines() if line.strip()]
    candidates = []
    config_text = next((line for line in reversed(lines) if line.lower().endswith(".json")), lines[-1] if lines else "")
    if config_text:
        candidates.append(Path(config_text).expanduser().resolve())
    if home_candidate not in candidates:
        candidates.append(home_candidate)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            payload = installer.read_json_file(candidate)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(payload, dict):
            models = payload.get("models")
            if isinstance(models, dict) and isinstance(models.get("providers"), dict) and models["providers"]:
                return candidate
            agents = payload.get("agents")
            defaults = agents.get("defaults") if isinstance(agents, dict) else None
            model = defaults.get("model") if isinstance(defaults, dict) else None
            if isinstance(model, dict) and str(model.get("primary") or "").strip():
                return candidate
    return candidates[0].expanduser().resolve()


def extract_text_fragments(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        fragments: list[str] = []
        for item in value:
            fragments.extend(extract_text_fragments(item))
        return fragments
    if not isinstance(value, dict):
        return []
    fragments: list[str] = []
    role = str(value.get("role") or "").strip().lower()
    if role == "assistant":
        fragments.extend(extract_text_fragments(value.get("content")))
    for key in ("text", "message", "content", "output", "result", "response", "reply", "data", "messages", "payloads", "items", "value"):
        if key in value:
            fragments.extend(extract_text_fragments(value.get(key)))
    deduped: list[str] = []
    seen: set[str] = set()
    for item in fragments:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def extract_agent_text(value: Any) -> str:
    if isinstance(value, dict):
        result = value.get("result")
        if isinstance(result, dict):
            payloads = result.get("payloads")
            if isinstance(payloads, list):
                payload_texts = [
                    str(item.get("text") or "").strip()
                    for item in payloads
                    if isinstance(item, dict) and str(item.get("text") or "").strip()
                ]
                if payload_texts:
                    return "\n".join(payload_texts)
    return "\n".join(extract_text_fragments(value))


def is_transient_agent_failure(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    result = value.get("result")
    stop_reason = str(result.get("stopReason") or "").strip().lower() if isinstance(result, dict) else ""
    haystack = "\n".join(
        fragment
        for fragment in (
            extract_agent_text(value),
            str(value.get("summary") or ""),
            stop_reason,
        )
        if str(fragment or "").strip()
    ).lower()
    if not haystack:
        return False
    return any(marker in haystack for marker in TRANSIENT_AGENT_FAILURE_MARKERS)


def run_agent_message(
    openclaw_bin: str,
    message: str,
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: int = 600,
    max_attempts: int = 3,
    base_sleep_seconds: float = 5.0,
) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        payload = parse_json_output(
            run(openclaw_command(openclaw_bin, "agent", "--agent", "main", "--message", message, "--json"), env=env, cwd=cwd, timeout=timeout),
            context=f"openclaw agent message attempt {attempt}",
        )
        if not isinstance(payload, dict):
            return payload
        last_payload = payload
        if not is_transient_agent_failure(payload):
            return payload
        if attempt < max_attempts:
            time.sleep(base_sleep_seconds * attempt)
    raise RuntimeError(
        "openclaw agent hit a transient provider failure after retries:\n"
        f"{json.dumps(last_payload or {}, ensure_ascii=False, indent=2)}"
    )


def build_temp_openclaw_config(
    base_config_path: Path,
    runtime_env_path: Path,
    workspace_dir: Path,
    runtime_python_path: Path,
) -> dict[str, Any]:
    payload = installer.read_json_file(base_config_path)

    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError("hooks must be an object")
    internal_hooks = hooks.setdefault("internal", {})
    if not isinstance(internal_hooks, dict):
        raise RuntimeError("hooks.internal must be an object")
    internal_hooks["enabled"] = False
    gateway = payload.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        raise RuntimeError("gateway must be an object")
    auth = gateway.setdefault("auth", {})
    if not isinstance(auth, dict):
        raise RuntimeError("gateway.auth must be an object")
    auth.setdefault("mode", "none")
    auth["token"] = str(auth.get("token") or secrets.token_hex(16))

    agents = payload.setdefault("agents", {})
    if isinstance(agents, dict):
        defaults = agents.setdefault("defaults", {})
        if isinstance(defaults, dict):
            defaults["workspace"] = str(workspace_dir)
            defaults["skipBootstrap"] = True

    plugins = payload.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise RuntimeError("plugins must be an object")
    allow = plugins.setdefault("allow", [])
    if isinstance(allow, list) and "memory-palace" not in allow:
        allow.append("memory-palace")
    load = plugins.setdefault("load", {})
    if not isinstance(load, dict):
        raise RuntimeError("plugins.load must be an object")
    load_paths = load.setdefault("paths", [])
    if isinstance(load_paths, list) and str(PLUGIN_ROOT) not in load_paths:
        load_paths.append(str(PLUGIN_ROOT))
    slots = plugins.setdefault("slots", {})
    if not isinstance(slots, dict):
        raise RuntimeError("plugins.slots must be an object")
    slots["memory"] = "memory-palace"
    entries = plugins.setdefault("entries", {})
    if not isinstance(entries, dict):
        raise RuntimeError("plugins.entries must be an object")
    memory_entry = entries.setdefault("memory-palace", {})
    if not isinstance(memory_entry, dict):
        raise RuntimeError("plugins.entries.memory-palace must be an object")
    memory_entry["enabled"] = True
    config = memory_entry.setdefault("config", {})
    if not isinstance(config, dict):
        raise RuntimeError("plugins.entries.memory-palace.config must be an object")
    config["transport"] = "stdio"
    config["autoRecall"] = {"enabled": True, "traceEnabled": True}
    config["autoCapture"] = {"enabled": False, "traceEnabled": True}
    config["visualMemory"] = {"enabled": False}
    config["reflection"] = {"enabled": False}
    config["profileMemory"] = {
        "enabled": True,
        "injectBeforeAgentStart": True,
        "maxCharsPerBlock": 320,
        "blocks": ["identity", "preferences", "workflow"],
    }
    config["hostBridge"] = {"enabled": False}
    config["capturePipeline"] = {
        "mode": "v2",
        "captureAssistantDerived": True,
        "maxAssistantDerivedPerRun": 2,
        "pendingOnFailure": True,
        "minConfidence": 0.72,
        "pendingConfidence": 0.55,
    }

    stdio = config.setdefault("stdio", {})
    if not isinstance(stdio, dict):
        raise RuntimeError("plugins.entries.memory-palace.config.stdio must be an object")
    env_block = stdio.setdefault("env", {})
    if not isinstance(env_block, dict):
        raise RuntimeError("plugins.entries.memory-palace.config.stdio.env must be an object")
    env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(runtime_env_path)
    env_block["OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR"] = str(workspace_dir)
    env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON"] = str(runtime_python_path)
    env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT"] = str(runtime_python_path.parent.parent)
    env_block.setdefault("PYTHONIOENCODING", "utf-8")
    env_block.setdefault("PYTHONUTF8", "1")
    stdio_command, stdio_args, stdio_cwd = installer.build_default_stdio_launch(
        runtime_python_path=runtime_python_path,
        host_platform="windows" if os.name == "nt" else None,
    )
    stdio["command"] = stdio_command
    stdio["args"] = stdio_args
    stdio["cwd"] = stdio_cwd
    config.pop("sse", None)
    return payload


def wait_for_candidate(
    openclaw_bin: str,
    query: str,
    *,
    env: dict[str, str],
    cwd: Path,
    timeout_seconds: float = 90.0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_index: dict[str, Any] | None = None
    last_search: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_index = parse_json_output(
            run(openclaw_command(openclaw_bin, "memory-palace", "index", "--wait", "--json"), env=env, cwd=cwd, timeout=600),
            context="openclaw memory-palace index",
        )
        last_search = parse_json_output(
            run(openclaw_command(openclaw_bin, "memory-palace", "search", query, "--json"), env=env, cwd=cwd, timeout=600),
            context="openclaw memory-palace search",
        )
        results = last_search.get("results") if isinstance(last_search, dict) else None
        if isinstance(results, list) and results:
            return last_index, last_search
        time.sleep(0.5)
    raise RuntimeError(f"Assistant-derived search returned no results: {json.dumps(last_search or {}, ensure_ascii=False)}")


def cleanup_temp_root(tmp_root: Path | None) -> None:
    if tmp_root is None:
        return
    shutil.rmtree(tmp_root, ignore_errors=True)


def main() -> int:
    args = parse_args()
    report_path = Path(args.report).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    base_config_path = resolve_current_openclaw_config(args.openclaw_bin)

    tmp_root: Path | None = None
    try:
        tmp_root = Path(tempfile.mkdtemp(prefix="mp-assistant-derived-e2e-"))
        workspace_dir = tmp_root / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        setup_root = tmp_root / "memory-palace"
        setup_root.mkdir(parents=True, exist_ok=True)
        runtime_env_path = setup_root / "runtime.env"
        smoke.build_profile_env(smoke.local_native_platform_name(), "b", runtime_env_path, {})
        runtime_python_path, _ = installer.ensure_runtime_venv(setup_root_path=setup_root, dry_run=False)

        config_payload = build_temp_openclaw_config(
            base_config_path,
            runtime_env_path,
            workspace_dir,
            runtime_python_path,
        )
        config_path = tmp_root / "openclaw.json"
        config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        state_dir = tmp_root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
        env["OPENCLAW_STATE_DIR"] = str(state_dir)
        env["OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR"] = str(workspace_dir)
        gateway_token = (
            config_payload.get("gateway", {}).get("auth", {}).get("token")
            if isinstance(config_payload.get("gateway"), dict)
            and isinstance(config_payload.get("gateway", {}).get("auth"), dict)
            else None
        )
        if isinstance(gateway_token, str) and gateway_token.strip():
            env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token.strip()

        marker = f"assistant-derived-{secrets.token_hex(4)}"
        first_message = f"For future sessions, my default workflow for {marker} is: start with code changes first."
        second_message = f"Then, for that same default workflow for {marker}, run the tests immediately after the code changes and before anything else."
        third_message = f"Docs should come at the end for {marker}."
        summary_message = f"In one short English sentence, summarize my default workflow for {marker} without giving advice."
        recall_message = (
            f"What is the workflow order for {marker}? "
            "Mention where docs belong in one short English sentence."
        )
        query = marker

        gateway_port = int(smoke.find_free_port())
        gateway_url = f"ws://127.0.0.1:{gateway_port}"
        gateway_log_path = tmp_root / "gateway.log"
        env["OPENCLAW_GATEWAY_URL"] = gateway_url
        with gateway_log_path.open("w", encoding="utf-8") as gateway_log:
            gateway = subprocess.Popen(
                [
                    *openclaw_command(
                        args.openclaw_bin,
                        "gateway",
                        "run",
                        "--allow-unconfigured",
                        "--force",
                        "--port",
                        str(gateway_port),
                    ),
                ],
                cwd=str(workspace_dir),
                env=env,
                stdout=gateway_log,
                stderr=gateway_log,
                text=True,
                start_new_session=True,
            )
        try:
            wait_for_gateway(args.openclaw_bin, gateway_url, env=env, cwd=workspace_dir, timeout_seconds=45)

            run_agent_message(
                args.openclaw_bin,
                first_message,
                env=env,
                cwd=workspace_dir,
            )
            run_agent_message(
                args.openclaw_bin,
                second_message,
                env=env,
                cwd=workspace_dir,
            )
            run_agent_message(
                args.openclaw_bin,
                third_message,
                env=env,
                cwd=workspace_dir,
            )
            third_result = run_agent_message(
                args.openclaw_bin,
                summary_message,
                env=env,
                cwd=workspace_dir,
            )
            third_text = "\n".join(extract_text_fragments(third_result))
            if not third_text.strip():
                raise RuntimeError("Assistant summary did not reflect the intended workflow:\n" + json.dumps(third_result, ensure_ascii=False, indent=2))

            index_result, search_result = wait_for_candidate(
                args.openclaw_bin,
                query,
                env=env,
                cwd=workspace_dir,
                timeout_seconds=90,
            )
            results = search_result.get("results") if isinstance(search_result, dict) else None
            if not isinstance(results, list) or not results:
                raise RuntimeError(f"Assistant-derived search returned no results: {json.dumps(search_result, ensure_ascii=False)}")
            top = results[0]
            path_value = str(top.get("path") or "").strip()
            assistant_result = next(
                (
                    item
                    for item in results
                    if isinstance(item, dict)
                    and "/assistant-derived/" in str(item.get("path") or "").replace("\\", "/")
                    and "/pending/" not in str(item.get("path") or "").replace("\\", "/")
                ),
                None,
            )
            if not isinstance(assistant_result, dict):
                raise RuntimeError(f"Committed assistant-derived record is missing from search results: {json.dumps(search_result, ensure_ascii=False)}")
            assistant_path = str(assistant_result.get("path") or "").strip()
            assistant_get = parse_json_output(
                run(openclaw_command(args.openclaw_bin, "memory-palace", "get", assistant_path, "--json"), env=env, cwd=workspace_dir, timeout=600),
                context="openclaw memory-palace get assistant-derived record",
            )
            assistant_text = str(assistant_get.get("text") or "")
            if marker not in assistant_text:
                raise RuntimeError(f"Assistant-derived record is missing the scenario marker {marker}:\n{assistant_text}")
            if "source_mode: assistant_derived" not in assistant_text:
                raise RuntimeError(f"Assistant-derived record is missing source provenance:\n{assistant_text}")
            if "capture_layer: assistant_derived_candidate" not in assistant_text or "## User Evidence" not in assistant_text:
                raise RuntimeError(f"Assistant-derived record is missing expected capture metadata:\n{assistant_text}")
            lowered_assistant_text = assistant_text.lower()
            if "test" not in lowered_assistant_text or "doc" not in lowered_assistant_text:
                raise RuntimeError(f"Assistant-derived record is missing the expected workflow steps:\n{assistant_text}")

            run_agent_message(
                args.openclaw_bin,
                "/new",
                env=env,
                cwd=workspace_dir,
            )
            recall_result = run_agent_message(
                args.openclaw_bin,
                recall_message,
                env=env,
                cwd=workspace_dir,
            )
            recall_text = "\n".join(extract_text_fragments(recall_result))
            lowered_recall_text = recall_text.lower()
            if "code" not in lowered_recall_text or "test" not in lowered_recall_text:
                raise RuntimeError("Recall answer did not reflect assistant-derived memory:\n" + json.dumps(recall_result, ensure_ascii=False, indent=2))
        finally:
            stop_gateway_process(gateway)

        payload = {
            "ok": True,
            "tmp_root": str(tmp_root),
            "workspace_dir": str(workspace_dir),
            "config_path": str(config_path),
            "state_dir": str(state_dir),
            "runtime_env_path": str(runtime_env_path),
            "index_ok": smoke.extract_index_command_ok(index_result),
            "search_path": path_value,
            "assistant_path": assistant_path,
            "third_text": third_text,
            "recall_text": recall_text,
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "tmp_root": str(tmp_root) if tmp_root is not None else "",
            "error": str(exc),
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    finally:
        cleanup_temp_root(tmp_root)


if __name__ == "__main__":
    raise SystemExit(main())
