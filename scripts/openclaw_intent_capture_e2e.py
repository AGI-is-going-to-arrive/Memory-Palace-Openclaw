#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import openclaw_memory_palace_profile_smoke as smoke
from openclaw_profile_memory_e2e import (
    build_temp_openclaw_config,
    parse_json_output,
    resolve_current_openclaw_config,
    run,
    run_agent_message,
    stop_gateway_process,
    wait_for_gateway,
)

DEFAULT_REPORT_PATH = REPO_ROOT / ".tmp" / "openclaw_intent_capture_e2e.json"

CASES = [
    {
        "name": "compliment_context",
        "kind": "negative",
        "message": "I like your analysis. Please reply OK.",
        "query": "your analysis",
        "expected_reason": "compliment_context",
        "expected_hits": 0,
    },
    {
        "name": "negated_preference",
        "kind": "negative",
        "message": "I don't like dark mode. Please reply OK.",
        "query": "dark mode",
        "expected_reason": "negated_preference",
        "expected_hits": 0,
    },
    {
        "name": "explicit_memory_intent_workflow_cn",
        "kind": "skip_memory_intent",
        "message": "请记住：alpha 的默认 workflow 是先列清单，再实现，最后补测试。请只回复“收到”。",
        "query": "先列清单，再实现，最后补测试",
        "expected_reason": "memory_intent",
        "expected_hits": 0,
    },
    {
        "name": "declarative_workflow_cn",
        "kind": "direct_workflow",
        "message": "以后默认按这个 workflow 协作：先列清单，再实现，最后补测试。请只回复“收到”。",
        "query": "先列清单，再实现，最后补测试",
        "expected_reason": "capture_signal",
        "expected_hits": 1,
        "expected_path_fragment": "/captured/workflow/",
    },
    {
        "name": "workflow_question_pollution_cn",
        "kind": "direct_workflow_question",
        "message": "alpha 的默认 workflow 是什么？只回答 workflow 内容。",
        "query": "alpha 的默认 workflow 是什么",
        "expected_reason": "no_capture",
        "expected_hits": 0,
    },
    {
        "name": "recent_plan_pending_cn",
        "kind": "pending_event",
        "message": "今天晚些时候我要复盘数据库迁移结果并记录风险。请只回复“收到”。",
        "query": "数据库迁移结果",
        "expected_reason": "recent_future_plan",
        "expected_hits": 1,
        "expected_path_fragment": "/pending/rule-capture/",
    },
    {
        "name": "mixed_positive_preference",
        "kind": "direct_preference",
        "message": "I don't like Java, but I like TypeScript. Please reply OK.",
        "query": "typescript",
        "expected_reason": "capture_signal",
        "expected_hits": 1,
        "expected_path_fragment": "/captured/preference/",
    },
]


def log_progress(message: str) -> None:
    print(f"[intent-capture-e2e] {message}", flush=True)


def extract_runtime_state(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        runtime_state = payload.get("runtimeState")
        if isinstance(runtime_state, dict):
            return runtime_state
        for value in payload.values():
            nested = extract_runtime_state(value)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = extract_runtime_state(value)
            if nested is not None:
                return nested
    return None


def setup_env(*, gateway_timeout_seconds: float = 45.0) -> tuple[dict[str, str], subprocess.Popen[str]]:
    tmp_root = Path(tempfile.mkdtemp(prefix="mp-intent-capture-e2e-"))
    workspace_dir = tmp_root / "workspace"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    runtime_env_path = tmp_root / "profile-b.env"
    runtime_env = smoke.build_profile_env(smoke.local_native_platform_name(), "b", runtime_env_path, {})
    config_payload = build_temp_openclaw_config(
        resolve_current_openclaw_config(),
        runtime_env_path,
        workspace_dir,
    )
    memory_entry = config_payload.get("plugins", {}).get("entries", {}).get("memory-palace", {})
    config = memory_entry.get("config") if isinstance(memory_entry, dict) else None
    stdio = config.get("stdio") if isinstance(config, dict) else None
    env_block = stdio.get("env") if isinstance(stdio, dict) else None
    if not isinstance(env_block, dict):
        raise RuntimeError("memory-palace stdio env block missing from temp config")
    for key in (
        "DATABASE_URL",
        "OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT",
        "OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH",
        "OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR",
    ):
        env_block.pop(key, None)
    env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(runtime_env_path)
    env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT"] = str(tmp_root)
    env_block["OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR"] = str(workspace_dir)
    env_block["OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH"] = str(tmp_root / "transport-diagnostics.json")

    config_path = tmp_root / "openclaw.json"
    config_path.write_text(
        json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    state_dir = tmp_root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    smoke.assert_isolated_test_runtime_paths(
        context="intent_capture_setup",
        config_path=config_path,
        runtime_env_path=runtime_env_path,
        state_dir=state_dir,
        database_url=runtime_env.get("DATABASE_URL"),
    )

    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)

    gateway_port = int(smoke.find_free_port())
    gateway_url = f"ws://127.0.0.1:{gateway_port}"
    env["OPENCLAW_GATEWAY_URL"] = gateway_url

    gateway = config_payload.get("gateway", {}) if isinstance(config_payload.get("gateway"), dict) else {}
    auth = gateway.get("auth", {}) if isinstance(gateway.get("auth"), dict) else {}
    token = str(auth.get("token") or "").strip()
    if token:
        env["OPENCLAW_GATEWAY_TOKEN"] = token

    gateway_log_path = tmp_root / "gateway.log"
    with gateway_log_path.open("w", encoding="utf-8") as gateway_log:
        proc = subprocess.Popen(
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
    wait_for_gateway(gateway_url, env=env, timeout_seconds=gateway_timeout_seconds)
    return env, proc


def run_doctor(env: dict[str, str], query: str, *, timeout: int) -> dict[str, Any]:
    return parse_json_output(
        run(
            smoke.openclaw_command("memory-palace", "doctor", "--query", query, "--json"),
            env=env,
            timeout=timeout,
        ),
        context="openclaw memory-palace doctor",
    )


def run_search(
    env: dict[str, str],
    query: str,
    *,
    index_timeout: int,
    search_timeout: int,
) -> dict[str, Any]:
    parse_json_output(
        run(
            smoke.openclaw_command("memory-palace", "index", "--wait", "--json"),
            env=env,
            timeout=index_timeout,
        ),
        context="openclaw memory-palace index",
    )
    return parse_json_output(
        run(
            smoke.openclaw_command("memory-palace", "search", query, "--json"),
            env=env,
            timeout=search_timeout,
        ),
        context="openclaw memory-palace search",
    )


def run_case(
    case: dict[str, Any],
    *,
    agent_timeout: int,
    status_timeout: int,
    index_timeout: int,
    search_timeout: int,
    gateway_timeout_seconds: float,
) -> dict[str, Any]:
    started_at = time.monotonic()
    log_progress(f"case={case['name']} setup_env")
    env, gateway = setup_env(gateway_timeout_seconds=gateway_timeout_seconds)
    try:
        log_progress(f"case={case['name']} run_agent_message")
        agent_payload = run_agent_message(str(case["message"]), env=env, timeout=agent_timeout)
        log_progress(f"case={case['name']} run_doctor")
        doctor_payload = run_doctor(env, str(case["query"]), timeout=status_timeout)
        runtime_state = extract_runtime_state(doctor_payload) or {}
        log_progress(f"case={case['name']} run_search")
        search_payload = run_search(
            env,
            str(case["query"]),
            index_timeout=index_timeout,
            search_timeout=search_timeout,
        )
        results = search_payload.get("results") if isinstance(search_payload, dict) else None
        def result_location(item: dict[str, Any]) -> str:
            path_value, uri_value = smoke.extract_path_or_uri(item)
            path_or_uri = item.get("path_or_uri")
            explicit_path_or_uri = (
                str(path_or_uri).strip()
                if isinstance(path_or_uri, str) and str(path_or_uri).strip()
                else ""
            )
            return str(path_value or uri_value or explicit_path_or_uri or "").strip()

        relevant_results = [
            item
            for item in (results if isinstance(results, list) else [])
            if "/captured/" in result_location(item).replace("\\", "/")
            or "/pending/rule-capture/" in result_location(item).replace("\\", "/")
        ]
        result_count = len(relevant_results)
        top_path = (
            result_location(relevant_results[0])
            if relevant_results
            else ""
        )
        last_decision = runtime_state.get("lastRuleCaptureDecision")
        decision_uri = (
            str(last_decision.get("uri") or "").strip()
            if isinstance(last_decision, dict)
            else ""
        )
        ok = True
        failures: list[str] = []

        expected_hits = int(case["expected_hits"])
        expected_path_fragment = str(case.get("expected_path_fragment") or "").strip()
        pending_doctor_match = (
            str(case.get("kind") or "").strip() == "pending_event"
            and expected_path_fragment
            and expected_path_fragment in decision_uri
        )

        if expected_hits == 0 and result_count != 0:
            ok = False
            failures.append(f"expected no hits, got {result_count}")
        if expected_hits > 0 and result_count < expected_hits and not pending_doctor_match:
            ok = False
            failures.append(f"expected at least {expected_hits} hit(s), got {result_count}")

        expected_reason = str(case["expected_reason"])
        actual_reason = (
            str(last_decision.get("reason") or "").strip()
            if isinstance(last_decision, dict)
            else ""
        )
        if actual_reason != expected_reason:
            ok = False
            failures.append(
                f"expected reason {expected_reason!r}, got {actual_reason!r}"
            )

        if expected_path_fragment and expected_path_fragment not in top_path and expected_path_fragment not in decision_uri:
            ok = False
            failures.append(
                f"expected top path/decision uri to contain {expected_path_fragment!r}, got path={top_path!r} uri={decision_uri!r}"
            )

        return {
            "name": case["name"],
            "kind": case.get("kind"),
            "ok": ok,
            "elapsed_seconds": round(time.monotonic() - started_at, 2),
            "message": case["message"],
            "query": case["query"],
            "agent_status": agent_payload.get("status") if isinstance(agent_payload, dict) else None,
            "last_rule_capture_decision": last_decision,
            "result_count": result_count,
            "raw_result_count": len(results) if isinstance(results, list) else 0,
            "top_path": top_path or None,
            "decision_uri": decision_uri or None,
            "failures": failures,
        }
    finally:
        stop_gateway_process(gateway)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenClaw intent/capture E2E cases")
    parser.add_argument(
        "--case",
        action="append",
        dest="cases",
        default=[],
        help="Run only the named case. Repeatable.",
    )
    parser.add_argument(
        "--list-cases",
        action="store_true",
        help="List available case names and exit.",
    )
    parser.add_argument(
        "--report-path",
        default=str(DEFAULT_REPORT_PATH),
        help="Report output path.",
    )
    parser.add_argument("--agent-timeout", type=int, default=600, help="Timeout in seconds for agent calls.")
    parser.add_argument("--status-timeout", type=int, default=600, help="Timeout in seconds for status calls.")
    parser.add_argument("--index-timeout", type=int, default=600, help="Timeout in seconds for index calls.")
    parser.add_argument("--search-timeout", type=int, default=600, help="Timeout in seconds for search calls.")
    parser.add_argument(
        "--gateway-timeout-seconds",
        type=float,
        default=45.0,
        help="Timeout in seconds while waiting for gateway health.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.list_cases:
        print("\n".join(case["name"] for case in CASES))
        return 0

    selected_cases = CASES
    if args.cases:
        requested = set(args.cases)
        selected_cases = [case for case in CASES if case["name"] in requested]
        missing = sorted(requested - {case["name"] for case in selected_cases})
        if missing:
            raise SystemExit(f"unknown case(s): {', '.join(missing)}")

    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    case_results = [
        run_case(
            case,
            agent_timeout=args.agent_timeout,
            status_timeout=args.status_timeout,
            index_timeout=args.index_timeout,
            search_timeout=args.search_timeout,
            gateway_timeout_seconds=args.gateway_timeout_seconds,
        )
        for case in selected_cases
    ]
    payload = {
        "ok": all(result.get("ok") for result in case_results),
        "cases": case_results,
    }
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
