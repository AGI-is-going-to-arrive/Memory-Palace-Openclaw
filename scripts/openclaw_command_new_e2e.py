#!/usr/bin/env python3
from __future__ import annotations

import json
import locale
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import openclaw_memory_palace_installer as installer
import openclaw_memory_palace_profile_smoke as smoke
from openclaw_json_output import parse_json_process_output

FORCE_KILL_SIGNAL = smoke._force_kill_signal()

PLUGIN_ROOT = REPO_ROOT / "extensions" / "memory-palace"
DEFAULT_REPORT_PATH = REPO_ROOT / ".tmp" / "openclaw_command_new_e2e.json"
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


def wait_for_gateway(url: str, *, env: dict[str, str], timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_error = ""
    while time.monotonic() < deadline:
        command = smoke.openclaw_command("gateway", "health", "--url", url)
        token = str(env.get("OPENCLAW_GATEWAY_TOKEN") or "").strip()
        if token:
            command.extend(["--token", token])
        result = run(command, env=env, timeout=30)
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


def wait_for_reflection_result(
    token: str,
    *,
    env: dict[str, str],
    timeout_seconds: float = 90.0,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_search: dict[str, Any] | None = None
    last_index: dict[str, Any] | None = None
    queries = (token, "command_new")
    while time.monotonic() < deadline:
        last_index = parse_json_output(
            run(
                smoke.openclaw_command("memory-palace", "index", "--wait", "--json"),
                env=env,
                timeout=600,
            ),
            context="openclaw memory-palace index",
        )
        for query in queries:
            last_search = parse_json_output(
                run(
                    smoke.openclaw_command(
                        "memory-palace",
                        "search",
                        query,
                        "--include-reflection",
                        "--max-results",
                        "20",
                        "--json",
                    ),
                    env=env,
                    timeout=600,
                ),
                context=f"openclaw memory-palace search --include-reflection ({query})",
            )
            results = last_search.get("results") if isinstance(last_search, dict) else None
            reflection_result = select_reflection_result(results)
            if reflection_result is not None:
                return last_index, last_search, reflection_result
        time.sleep(0.5)
    raise RuntimeError(
        f"Reflection search returned no results: {json.dumps(last_search or {}, ensure_ascii=False)}"
    )


def resolve_current_openclaw_config() -> Path:
    result = run(smoke.openclaw_command("config", "file"), env=os.environ.copy(), timeout=60)
    ensure_success(result, context="openclaw config file")
    config_text = str(result.stdout or "").splitlines()[0].strip()
    return Path(config_text).expanduser().resolve()


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


def select_reflection_result(results: Any) -> dict[str, Any] | None:
    if not isinstance(results, list):
        return None
    for item in results:
        if not isinstance(item, dict):
            continue
        path_value = str(item.get("path") or "").strip()
        if not path_value:
            continue
        if "/reflection/" in path_value.replace("\\", "/"):
            return item
    return None


def run_agent_message(
    message: str,
    *,
    env: dict[str, str],
    timeout: int = 600,
    max_attempts: int = 3,
    base_sleep_seconds: float = 5.0,
) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        payload = parse_json_output(
            run(
                smoke.openclaw_command("agent", "--agent", "main", "--message", message, "--json"),
                env=env,
                timeout=timeout,
            ),
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
) -> dict[str, Any]:
    payload = installer.read_json_file(base_config_path)

    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError("hooks must be an object")
    internal_hooks = hooks.setdefault("internal", {})
    if not isinstance(internal_hooks, dict):
        raise RuntimeError("hooks.internal must be an object")
    internal_hooks["enabled"] = True
    agents = payload.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise RuntimeError("agents must be an object")
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        raise RuntimeError("agents.defaults must be an object")
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
    config["autoRecall"] = {"enabled": False}
    config["autoCapture"] = {"enabled": False}
    config["visualMemory"] = {"enabled": False}
    config["profileMemory"] = {"enabled": False}
    config["hostBridge"] = {"enabled": False}
    config["capturePipeline"] = {"captureAssistantDerived": False}
    config["smartExtraction"] = {"enabled": False}
    config["reconcile"] = {"enabled": False}
    config["reflection"] = {
        "enabled": True,
        "autoRecall": False,
        "source": "command_new",
        "rootUri": "core://reflection",
        "traceEnabled": True,
    }

    stdio = config.setdefault("stdio", {})
    if not isinstance(stdio, dict):
        raise RuntimeError("plugins.entries.memory-palace.config.stdio must be an object")
    env_block = stdio.setdefault("env", {})
    if not isinstance(env_block, dict):
        raise RuntimeError("plugins.entries.memory-palace.config.stdio.env must be an object")
    env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(runtime_env_path)
    config.pop("sse", None)
    return payload


def cleanup_temp_root(tmp_root: Path | None) -> None:
    if tmp_root is None:
        return
    shutil.rmtree(tmp_root, ignore_errors=True)


def main() -> int:
    report_path = DEFAULT_REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    base_config_path = resolve_current_openclaw_config()

    tmp_root: Path | None = None
    try:
        tmp_root = Path(tempfile.mkdtemp(prefix="mp-command-new-e2e-"))
        workspace_dir = tmp_root / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        runtime_env_path = tmp_root / "profile-b.env"
        smoke.build_profile_env(smoke.local_native_platform_name(), "b", runtime_env_path, {})

        config_payload = build_temp_openclaw_config(base_config_path, runtime_env_path, workspace_dir)
        config_path = tmp_root / "openclaw.json"
        config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        state_dir = tmp_root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
        env["OPENCLAW_STATE_DIR"] = str(state_dir)
        gateway_token = (
            config_payload.get("gateway", {}).get("auth", {}).get("token")
            if isinstance(config_payload.get("gateway"), dict)
            and isinstance(config_payload.get("gateway", {}).get("auth"), dict)
            else None
        )
        if isinstance(gateway_token, str) and gateway_token.strip():
            env["OPENCLAW_GATEWAY_TOKEN"] = gateway_token.strip()

        token = f"command-new-{uuid.uuid4().hex[:8]}"
        first_message = f"Remember this release checkpoint token: {token}. Follow up tomorrow."

        gateway_port = int(smoke.find_free_port())
        gateway_url = f"ws://127.0.0.1:{gateway_port}"
        gateway_log_path = tmp_root / "gateway.log"
        env["OPENCLAW_GATEWAY_URL"] = gateway_url
        with gateway_log_path.open("w", encoding="utf-8") as gateway_log:
            gateway = subprocess.Popen(
                [
                    *smoke.openclaw_command(
                        "gateway",
                        "run",
                        "--allow-unconfigured",
                        "--force",
                        "--port",
                        str(gateway_port),
                    ),
                ],
                cwd=str(REPO_ROOT),
                env=env,
                stdout=gateway_log,
                stderr=gateway_log,
                text=True,
                start_new_session=True,
            )
        index_result: dict[str, Any] | None = None
        search_result: dict[str, Any] | None = None
        try:
            wait_for_gateway(gateway_url, env=env, timeout_seconds=45)

            first_result = run_agent_message(
                first_message,
                env=env,
            )

            reset_result = run_agent_message(
                "/new",
                env=env,
            )

            index_result, search_result, reflection_result = wait_for_reflection_result(
                token,
                env=env,
                timeout_seconds=90,
            )
        finally:
            stop_gateway_process(gateway)
        if index_result is None or search_result is None:
            raise RuntimeError("Reflection polling did not produce index/search results.")
        path_value = str(reflection_result.get("path") or "").strip()
        if "reflection" not in path_value.replace("\\", "/"):
            raise RuntimeError(f"Top search result is not in reflection lane: {json.dumps(reflection_result, ensure_ascii=False)}")

        get_result = parse_json_output(
            run(
                smoke.openclaw_command("memory-palace", "get", path_value, "--json"),
                env=env,
                timeout=600,
            ),
            context="openclaw memory-palace get reflection",
        )
        text = str(get_result.get("text") or "")
        required_fragments = [
            "- source: command_new",
            "- trigger: command:new",
            "- retention_class: session_boundary",
            token,
        ]
        for fragment in required_fragments:
            if fragment not in text:
                raise RuntimeError(f"Missing reflection fragment `{fragment}` in:\n{text}")
        if "- summary_method: message_rollup_v1" not in text and "- summary_method: transcript_rollup_v1" not in text:
            raise RuntimeError("Missing reflection summary_method in:\n" + text)

        payload = {
            "ok": True,
            "tmp_root": str(tmp_root),
            "config_path": str(config_path),
            "state_dir": str(state_dir),
            "runtime_env_path": str(runtime_env_path),
            "token": token,
            "first_message_session": first_result.get("meta", {}).get("agentMeta", {}).get("sessionId")
            if isinstance(first_result, dict)
            else None,
            "reset_session": reset_result.get("meta", {}).get("agentMeta", {}).get("sessionId")
            if isinstance(reset_result, dict)
            else None,
            "index_ok": smoke.extract_index_command_ok(index_result),
            "reflection_path": path_value,
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
