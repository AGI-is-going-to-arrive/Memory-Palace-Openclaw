#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import locale
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import openclaw_memory_palace_installer as installer
import openclaw_memory_palace_profile_smoke as smoke
from openclaw_json_output import parse_json_process_output

FORCE_KILL_SIGNAL = smoke._force_kill_signal()


PLUGIN_ROOT = REPO_ROOT / "extensions" / "memory-palace"
DEFAULT_REPORT_PATH = REPO_ROOT / ".tmp" / "openclaw_memory_palace_phase23_e2e.json"
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


def run(command: list[str], *, env: dict[str, str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
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


def wait_for_gateway(url: str, *, env: dict[str, str], timeout_seconds: float = 45.0) -> None:
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


def config_has_real_models(base_config_path: Path) -> bool:
    try:
        payload = installer.read_json_file(base_config_path)
    except Exception:  # noqa: BLE001
        return False
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


def resolve_current_openclaw_config() -> Path:
    for env_name in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_CONFIG"):
        configured = str(os.environ.get(env_name) or "").strip()
        if not configured:
            continue
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file() and config_has_real_models(candidate):
            return candidate
    home_candidate = Path.home() / ".openclaw" / "openclaw.json"
    if home_candidate.is_file() and config_has_real_models(home_candidate):
        return home_candidate
    result = run(smoke.openclaw_command("config", "file"), env=os.environ.copy(), timeout=60)
    ensure_success(result, context="openclaw config file")
    config_text = str(result.stdout or "").splitlines()[0].strip()
    return Path(config_text).expanduser().resolve()


def build_temp_openclaw_config(
    base_config_path: Path,
    runtime_env_path: Path,
    *,
    workspace_dir: Path,
    auto_capture: bool,
    capture_assistant_derived: bool,
    profile_memory: bool,
    host_bridge: bool,
) -> dict[str, Any]:
    payload = installer.read_json_file(base_config_path)

    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError("hooks must be an object")
    internal_hooks = hooks.setdefault("internal", {})
    if not isinstance(internal_hooks, dict):
        raise RuntimeError("hooks.internal must be an object")
    internal_hooks["enabled"] = False

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
    config["autoRecall"] = {"enabled": True, "traceEnabled": True}
    config["autoCapture"] = {"enabled": auto_capture, "traceEnabled": True}
    config["visualMemory"] = {"enabled": False}
    config["reflection"] = {"enabled": False}
    config["profileMemory"] = {
        "enabled": profile_memory,
        "injectBeforeAgentStart": True,
        "maxCharsPerBlock": 320,
        "blocks": ["identity", "preferences", "workflow"],
    }
    config["hostBridge"] = {
        "enabled": host_bridge,
        "importUserMd": True,
        "importMemoryMd": True,
        "importDailyMemory": True,
        "maxHits": 3,
        "maxImportPerRun": 2,
    }
    config["capturePipeline"] = {
        "mode": "v2",
        "captureAssistantDerived": capture_assistant_derived,
        "maxAssistantDerivedPerRun": 2,
        "pendingOnFailure": True,
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
    for key in (
        "text",
        "message",
        "content",
        "output",
        "result",
        "response",
        "reply",
        "data",
        "payloads",
        "messages",
        "items",
        "value",
    ):
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


def wait_for_memory_search(
    query: str,
    *,
    env: dict[str, str],
    timeout_seconds: float = 90.0,
    fail_fast_log_path: Path | None = None,
    fail_fast_marker: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_index: dict[str, Any] | None = None
    last_search: dict[str, Any] | None = None
    normalized_fail_fast_marker = str(fail_fast_marker or "").strip().lower()
    while time.monotonic() < deadline:
        last_index = parse_json_output(
            run(smoke.openclaw_command("memory-palace", "index", "--wait", "--json"), env=env, timeout=600),
            context="openclaw memory-palace index",
        )
        last_search = parse_json_output(
            run(smoke.openclaw_command("memory-palace", "search", query, "--json"), env=env, timeout=600),
            context="openclaw memory-palace search",
        )
        results = last_search.get("results") if isinstance(last_search, dict) else None
        if isinstance(results, list) and results:
            return last_index, last_search
        if normalized_fail_fast_marker and fail_fast_log_path and fail_fast_log_path.exists():
            log_text = fail_fast_log_path.read_text(encoding="utf-8", errors="replace")
            lowered_log_text = log_text.lower()
            if normalized_fail_fast_marker in lowered_log_text:
                tail = log_text[-4000:].strip()
                raise RuntimeError(
                    f"Search returned no results for {query} after a detected host bridge failure:\n{tail}"
                )
        time.sleep(0.5)
    raise RuntimeError(f"Search returned no results for {query}: {json.dumps(last_search or {}, ensure_ascii=False)}")


def select_search_result(
    results: Any,
    *,
    required_path_fragment: str | None = None,
    allow_pending: bool = True,
) -> dict[str, Any] | None:
    if not isinstance(results, list):
        return None
    required_fragment = str(required_path_fragment or "").strip().replace("\\", "/").lower()
    for item in results:
        if not isinstance(item, dict):
            continue
        path_value = str(item.get("path") or "").strip()
        if not path_value:
            continue
        normalized_path = path_value.replace("\\", "/").lower()
        if required_fragment and required_fragment not in normalized_path:
            continue
        if not allow_pending and "/pending/" in normalized_path:
            continue
        return item
    return None


def select_host_bridge_record(
    results: Any,
    *,
    env: dict[str, str],
    marker: str,
) -> tuple[str, str]:
    if not isinstance(results, list):
        raise RuntimeError(f"Phase 2 provenance search returned invalid results: {json.dumps(results, ensure_ascii=False)}")
    fallback: tuple[str, str] | None = None
    for item in results:
        if not isinstance(item, dict):
            continue
        path_value = str(item.get("path") or "").strip()
        if not path_value:
            continue
        normalized_path = path_value.replace("\\", "/").lower()
        if "/host-bridge/" not in normalized_path:
            continue
        get_result = parse_json_output(
            run(smoke.openclaw_command("memory-palace", "get", path_value, "--json"), env=env, timeout=600),
            context="openclaw memory-palace get phase2 host bridge record",
        )
        stored_text = str(get_result.get("text") or "")
        if fallback is None:
            fallback = (path_value, stored_text)
        if marker in stored_text:
            return path_value, stored_text
    if fallback is not None:
        return fallback
    raise RuntimeError(f"Phase 2 host bridge record is missing: {json.dumps(results, ensure_ascii=False)}")


def build_runtime_env_file(target: Path) -> Path:
    smoke.build_profile_env(smoke.local_native_platform_name(), "b", target, {})
    return target


def build_phase_env(config_payload: dict[str, Any], config_path: Path, state_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    gateway = config_payload.get("gateway")
    if isinstance(gateway, dict):
        auth = gateway.get("auth")
        if isinstance(auth, dict):
            token = str(auth.get("token") or "").strip()
            if token:
                env["OPENCLAW_GATEWAY_TOKEN"] = token
    return env


def cleanup_temp_root(tmp_root: Path | None) -> None:
    if tmp_root is None:
        return
    shutil.rmtree(tmp_root, ignore_errors=True)


def run_agent_message(
    message: str,
    *,
    env: dict[str, str],
    timeout: int = 900,
    max_attempts: int = 3,
    base_sleep_seconds: float = 5.0,
) -> dict[str, Any]:
    last_payload: dict[str, Any] | None = None
    for attempt in range(1, max_attempts + 1):
        payload = parse_json_output(
            run(smoke.openclaw_command("agent", "--agent", "main", "--message", message, "--json"), env=env, timeout=timeout),
            context=f"openclaw agent {message}",
        )
        last_payload = payload
        if not is_transient_agent_failure(payload):
            return payload
        if attempt < max_attempts:
            time.sleep(base_sleep_seconds * attempt)
            continue
    raise RuntimeError(
        "openclaw agent hit a transient provider failure after retries:\n"
        f"{json.dumps(last_payload or {}, ensure_ascii=False, indent=2)}"
    )


def run_phase2_host_bridge(base_config_path: Path, phase_root: Path) -> dict[str, Any]:
    workspace_dir = phase_root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    marker = f"phase23-host-marker-{uuid.uuid4().hex[:8]}"
    (workspace_dir / "MEMORY.md").write_text(
        f"default workflow marker: {marker}\n"
        f"default workflow: first code and tests, then review findings, docs last.\n",
        encoding="utf-8",
    )
    runtime_env_path = build_runtime_env_file(phase_root / "profile-b.env")
    config_payload = build_temp_openclaw_config(
        base_config_path,
        runtime_env_path,
        workspace_dir=workspace_dir,
        auto_capture=False,
        capture_assistant_derived=True,
        profile_memory=True,
        host_bridge=True,
    )
    config_path = phase_root / "openclaw.json"
    config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state_dir = phase_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = build_phase_env(config_payload, config_path, state_dir)

    gateway_port = int(smoke.find_free_port())
    gateway_url = f"ws://127.0.0.1:{gateway_port}"
    env["OPENCLAW_GATEWAY_URL"] = gateway_url
    gateway_log_path = phase_root / "gateway.log"
    with gateway_log_path.open("w", encoding="utf-8") as gateway_log:
        gateway = subprocess.Popen(
            smoke.openclaw_command("gateway", "run", "--allow-unconfigured", "--force", "--port", str(gateway_port)),
            cwd=str(REPO_ROOT),
            env=env,
            stdout=gateway_log,
            stderr=gateway_log,
            text=True,
            start_new_session=True,
        )
    try:
        wait_for_gateway(gateway_url, env=env, timeout_seconds=45)
        first_result = run_agent_message(
            "What do you remember as my default workflow marker? Reply with the marker only.",
            env=env,
        )
        _index_result, search_result = wait_for_memory_search(
            marker,
            env=env,
            timeout_seconds=90,
            fail_fast_log_path=gateway_log_path,
            fail_fast_marker="memory-palace host bridge import failed",
        )
        results = search_result.get("results") if isinstance(search_result, dict) else None
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"Phase 2 search returned no results: {json.dumps(search_result, ensure_ascii=False)}")
        top = results[0]
        path_value = str(top.get("path") or "").strip()
        provenance_search = parse_json_output(
            run(smoke.openclaw_command("memory-palace", "search", "MEMORY.md#L1", "--json"), env=env, timeout=600),
            context="openclaw memory-palace search phase2 host bridge provenance",
        )
        provenance_results = provenance_search.get("results") if isinstance(provenance_search, dict) else None
        host_bridge_path, stored_text = select_host_bridge_record(
            provenance_results,
            env=env,
            marker=marker,
        )
        if "source_mode: host_workspace_import" not in stored_text:
            raise RuntimeError(f"Phase 2 host bridge record is missing import provenance:\n{stored_text}")
        if "MEMORY.md#L1" not in stored_text or "sha256-" not in stored_text:
            raise RuntimeError(f"Phase 2 host bridge record is missing source citation/hash provenance:\n{stored_text}")

        assistant_text = extract_agent_text(first_result)
        if marker not in assistant_text:
            raise RuntimeError(f"Phase 2 first answer missed the host-backed marker:\n{json.dumps(first_result, ensure_ascii=False, indent=2)}")

        (workspace_dir / "MEMORY.md").unlink(missing_ok=True)
        run_agent_message("/new", env=env)
        second_result = run_agent_message(
            "What do you remember as my default workflow marker? Reply with the marker only.",
            env=env,
        )
        second_text = extract_agent_text(second_result)
        if marker not in second_text:
            raise RuntimeError(
                "Phase 2 second answer did not come back from plugin-owned memory after host cleanup:\n"
                f"{json.dumps(second_result, ensure_ascii=False, indent=2)}"
            )
        return {
            "ok": True,
            "marker": marker,
            "workflowPath": path_value,
            "hostBridgePath": host_bridge_path,
            "firstAnswer": assistant_text,
            "secondAnswer": second_text,
        }
    finally:
        stop_gateway_process(gateway)


def run_phase3_assistant_derived(base_config_path: Path, phase_root: Path) -> dict[str, Any]:
    workspace_dir = phase_root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime_env_path = build_runtime_env_file(phase_root / "profile-b.env")
    config_payload = build_temp_openclaw_config(
        base_config_path,
        runtime_env_path,
        workspace_dir=workspace_dir,
        auto_capture=False,
        capture_assistant_derived=True,
        profile_memory=False,
        host_bridge=False,
    )
    config_path = phase_root / "openclaw.json"
    config_path.write_text(json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state_dir = phase_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    env = build_phase_env(config_payload, config_path, state_dir)

    gateway_port = int(smoke.find_free_port())
    gateway_url = f"ws://127.0.0.1:{gateway_port}"
    env["OPENCLAW_GATEWAY_URL"] = gateway_url
    marker = f"phase23-derived-marker-{uuid.uuid4().hex[:8]}"
    first_message = f"For future sessions, my default workflow for {marker} is: start with code changes first."
    second_message = (
        f"Then, for that same default workflow for {marker}, "
        "run the tests immediately after the code changes and before anything else."
    )
    third_message = f"Docs should come at the end for {marker}."
    summary_message = f"In one short English sentence, summarize my default workflow for {marker} without giving advice."
    recall_message = (
        f"What is the workflow order for {marker}? "
        "Mention where docs belong in one short English sentence."
    )
    with (phase_root / "gateway.log").open("w", encoding="utf-8") as gateway_log:
        gateway = subprocess.Popen(
            smoke.openclaw_command("gateway", "run", "--allow-unconfigured", "--force", "--port", str(gateway_port)),
            cwd=str(REPO_ROOT),
            env=env,
            stdout=gateway_log,
            stderr=gateway_log,
            text=True,
            start_new_session=True,
        )
    try:
        wait_for_gateway(gateway_url, env=env, timeout_seconds=45)
        run_agent_message(first_message, env=env)
        run_agent_message(second_message, env=env)
        run_agent_message(third_message, env=env)
        summary_result = run_agent_message(summary_message, env=env)
        summary_text = extract_agent_text(summary_result)
        if not summary_text.strip():
            raise RuntimeError(
                "Phase 3 summary turn did not produce a usable workflow summary:\n"
                f"{json.dumps(summary_result, ensure_ascii=False, indent=2)}"
            )

        _index_result, search_result = wait_for_memory_search(marker, env=env, timeout_seconds=90)
        results = search_result.get("results") if isinstance(search_result, dict) else None
        if not isinstance(results, list) or not results:
            raise RuntimeError(f"Phase 3 search returned no results: {json.dumps(search_result, ensure_ascii=False)}")
        assistant_result = select_search_result(
            results,
            required_path_fragment="/assistant-derived/",
            allow_pending=False,
        )
        if not isinstance(assistant_result, dict):
            raise RuntimeError(
                "Phase 3 committed assistant-derived record is missing from search results:\n"
                f"{json.dumps(search_result, ensure_ascii=False, indent=2)}"
            )
        path_value = str(assistant_result.get("path") or "").strip()
        if "/pending/" in path_value.replace("\\", "/"):
            raise RuntimeError(f"Phase 3 only produced a pending candidate: {path_value}")
        get_result = parse_json_output(
            run(smoke.openclaw_command("memory-palace", "get", path_value, "--json"), env=env, timeout=600),
            context="openclaw memory-palace get phase3 record",
        )
        stored_text = str(get_result.get("text") or "")
        if "source_mode: assistant_derived" not in stored_text:
            raise RuntimeError(f"Phase 3 durable record is missing assistant-derived provenance:\n{stored_text}")
        if marker not in stored_text:
            raise RuntimeError(f"Phase 3 durable record is missing marker {marker}:\n{stored_text}")
        lowered_stored_text = stored_text.lower()
        if "test" not in lowered_stored_text or "doc" not in lowered_stored_text:
            raise RuntimeError(f"Phase 3 durable record is missing the expected workflow steps:\n{stored_text}")

        run_agent_message("/new", env=env)
        recall_result = run_agent_message(recall_message, env=env)
        recall_text = extract_agent_text(recall_result)
        lowered_recall_text = recall_text.lower()
        if "code" not in lowered_recall_text or "test" not in lowered_recall_text:
            raise RuntimeError(
                "Phase 3 recall did not reflect the assistant-derived durable fact:\n"
                f"{json.dumps(recall_result, ensure_ascii=False, indent=2)}"
            )
        return {
            "ok": True,
            "marker": marker,
            "workflowPath": path_value,
            "summaryText": summary_text,
            "recallText": recall_text,
        }
    finally:
        stop_gateway_process(gateway)


def main() -> int:
    report_path = DEFAULT_REPORT_PATH
    report_path.parent.mkdir(parents=True, exist_ok=True)
    base_config_path = resolve_current_openclaw_config()
    tmp_root: Path | None = None
    try:
        tmp_root = Path(tempfile.mkdtemp(prefix="mp-phase23-e2e-"))
        phase2_root = tmp_root / "phase2"
        phase2_root.mkdir(parents=True, exist_ok=True)
        phase2_result = run_phase2_host_bridge(base_config_path, phase2_root)

        phase3_root = tmp_root / "phase3"
        phase3_root.mkdir(parents=True, exist_ok=True)
        phase3_result = run_phase3_assistant_derived(base_config_path, phase3_root)

        payload = {
            "ok": True,
            "tmpRoot": str(tmp_root),
            "phase2": phase2_result,
            "phase3": phase3_result,
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        payload = {
            "ok": False,
            "tmpRoot": str(tmp_root) if tmp_root is not None else "",
            "error": str(exc),
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    finally:
        cleanup_temp_root(tmp_root)


if __name__ == "__main__":
    raise SystemExit(main())
