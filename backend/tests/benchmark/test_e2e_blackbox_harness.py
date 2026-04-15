"""OpenClaw Black-Box E2E Benchmark Harness

Communicates with Memory Palace exclusively via MCP tools (stdio transport).
No internal imports (sqlite_client, search_advanced, etc.).

Spec: backend/tests/benchmark/E2E_BLACKBOX_SPEC.md

Profiles:
  B: hybrid/hash (default, no external API)
  C: hybrid/api embedding (needs RETRIEVAL_EMBEDDING_* env)
  D: hybrid/api embedding + reranker (needs RETRIEVAL_RERANKER_* env)

Usage:
  # Profile B only (no API needed):
  pytest test_e2e_blackbox_harness.py -k "profile_b"

  # All available profiles:
  RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_RERANKER_API_BASE=... \\
    pytest test_e2e_blackbox_harness.py -v
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from helpers.e2e_eval import (
    ScenarioResult,
    aggregate_results as _aggregate_results,
    eval_delete as _eval_delete,
    eval_guard as _eval_guard,
    eval_read as _eval_read,
    eval_search as _eval_search,
)

try:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
except ModuleNotFoundError:
    pytest.skip("mcp SDK not installed", allow_module_level=True)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_ROOT = _PROJECT_ROOT / "backend"
_FIXTURES = _BACKEND_ROOT / "tests" / "fixtures"
_BENCH = Path(__file__).resolve().parent
_POSIX_STDIO = _PROJECT_ROOT / "scripts" / "run_memory_palace_mcp_stdio.sh"

_SCENARIOS_FILE = "e2e_blackbox_scenarios.jsonl"

_BENCH_DOMAINS = (
    "core,personal,project,writing,research,finance,learning,"
    "writer,game,notes,system"
)

# Env keys for profile detection
_EMBED_KEYS = [
    "RETRIEVAL_EMBEDDING_API_BASE",
    "RETRIEVAL_EMBEDDING_API_KEY",
    "RETRIEVAL_EMBEDDING_MODEL",
    "RETRIEVAL_EMBEDDING_DIM",
]
_RERANKER_KEYS = [
    "RETRIEVAL_RERANKER_ENABLED",
    "RETRIEVAL_RERANKER_API_BASE",
    "RETRIEVAL_RERANKER_MODEL",
    "RETRIEVAL_RERANKER_API_KEY",
]

# Drop parent env vars that could leak into subprocess
_DROP_PREFIXES = (
    "AUTO_LEARN_", "BENCHMARK_", "COMPACT_GIST_", "CORE_MEMORY_URIS",
    "DATABASE_URL", "EMBEDDING_PROVIDER_", "EXTERNAL_IMPORT_", "INTENT_",
    "LLM_", "MCP_API_KEY", "OPENAI_", "OPENCLAW_", "RETRIEVAL_",
    "ROUTER_", "RUNTIME_", "SEARCH_", "VALID_DOMAINS", "WRITE_GUARD_",
)


# ScenarioResult, eval_*, aggregate_results imported from helpers.e2e_eval

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_scenarios() -> List[Dict]:
    path = _FIXTURES / _SCENARIOS_FILE
    assert path.exists(), f"Scenarios file not found: {path}"
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _detect_available_profiles() -> List[str]:
    profiles = ["B"]
    if os.environ.get("RETRIEVAL_EMBEDDING_API_BASE"):
        profiles.append("C")
        if (os.environ.get("RETRIEVAL_RERANKER_API_BASE")
                and os.environ.get("RETRIEVAL_RERANKER_ENABLED", "").lower() == "true"
                and os.environ.get("RETRIEVAL_RERANKER_MODEL")):
            profiles.append("D")
    return profiles


def _build_env(db_path: Path, profile: str) -> Dict[str, str]:
    """Build clean env for MCP subprocess."""
    env = {
        k: v for k, v in os.environ.items()
        if not any(k.upper().startswith(p) for p in _DROP_PREFIXES)
    }
    env.update({
        "DATABASE_URL": f"sqlite+aiosqlite:////{db_path}",
        "VALID_DOMAINS": _BENCH_DOMAINS,
        "SEARCH_DEFAULT_MODE": "hybrid",
        "WRITE_GUARD_LLM_ENABLED": "false",
        "INTENT_LLM_ENABLED": "false",
        "COMPACT_GIST_LLM_ENABLED": "false",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })

    if profile == "B":
        env["RETRIEVAL_EMBEDDING_BACKEND"] = "hash"
        env["RETRIEVAL_RERANKER_ENABLED"] = "false"
    elif profile in ("C", "D"):
        env["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
        for k in _EMBED_KEYS:
            v = os.environ.get(k)
            if v:
                env[k] = v
        if profile == "C":
            env["RETRIEVAL_RERANKER_ENABLED"] = "false"
        else:  # D
            env["RETRIEVAL_RERANKER_ENABLED"] = "true"
            for k in _RERANKER_KEYS:
                v = os.environ.get(k)
                if v:
                    env[k] = v

    return env


def _build_server(env: Dict[str, str]) -> StdioServerParameters:
    if os.name == "nt":
        return StdioServerParameters(
            command=sys.executable,
            args=[str(_BACKEND_ROOT / "mcp_wrapper.py")],
            cwd=str(_BACKEND_ROOT), env=env,
        )
    if _POSIX_STDIO.is_file():
        return StdioServerParameters(
            command="/bin/bash",
            args=[str(_POSIX_STDIO)],
            cwd=str(_PROJECT_ROOT), env=env,
        )
    return StdioServerParameters(
        command=sys.executable,
        args=["mcp_server.py"],
        cwd=str(_BACKEND_ROOT), env=env,
    )


def _text_of(result: Any) -> str:
    return "\n".join(getattr(item, "text", str(item)) for item in result.content)


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return text


# ---------------------------------------------------------------------------
# Core evaluation logic
# ---------------------------------------------------------------------------


async def _run_tool(session: ClientSession, tool: str, args: Dict) -> Any:
    raw = _text_of(await session.call_tool(tool, args))
    return _parse_json(raw)


async def _run_setup(session: ClientSession, steps: List[Dict]) -> Dict[str, Any]:
    """Run setup steps, return metadata (e.g. created URIs)."""
    meta = {}
    for step in steps:
        result = await _run_tool(session, step["tool"], step["args"])
        if isinstance(result, dict) and result.get("uri"):
            meta[step["args"].get("title", "")] = result["uri"]
    return meta


async def _evaluate_scenario(
    session: ClientSession, scenario: Dict, profile: str,
) -> ScenarioResult:
    sr = ScenarioResult(
        scenario_id=scenario["scenario_id"],
        category=scenario["category"],
        profile=profile,
    )

    try:
        # Setup
        await _run_setup(session, scenario.get("setup", []))

        expected = scenario.get("expected", {})

        # Handle action_sequence (e.g. delete then search)
        if "action_sequence" in scenario:
            actions = scenario["action_sequence"]
            last_result = None
            t0 = time.perf_counter()
            for act in actions:
                last_result = await _run_tool(session, act["tool"], act["args"])
            sr.latency_ms = (time.perf_counter() - t0) * 1000
            result = last_result
        else:
            action = scenario["action"]
            t0 = time.perf_counter()
            result = await _run_tool(session, action["tool"], action["args"])
            sr.latency_ms = (time.perf_counter() - t0) * 1000

        # Evaluate based on category
        if scenario["category"] == "delete_verify":
            _eval_delete(sr, result, expected)
        elif scenario["category"] == "conflict_guard":
            _eval_guard(sr, result, expected)
        elif scenario["category"] == "namespace_read":
            _eval_read(sr, result, expected)
        else:
            _eval_search(sr, result, expected)

    except Exception as exc:
        sr.error = str(exc)

    return sr


# eval_search, eval_guard, eval_delete, eval_read, aggregate_results
# are all imported from helpers.e2e_eval (see top of file)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_blackbox_profile_b(tmp_path):
    """Black-box E2E benchmark on Profile B (hash embedding, no external API)."""
    results = await _run_profile("B", tmp_path)
    _write_report({"B": results}, tmp_path)

    agg = _aggregate_results(results)
    assert agg["n"] > 0, "No scenarios executed"
    # Profile B floor: baseline 0.722, allow ~15% regression
    assert agg["hr"] >= 0.60, (
        f"Profile B HR={agg['hr']:.3f} below floor 0.60"
    )


@pytest.mark.asyncio
async def test_e2e_blackbox_profile_c(tmp_path):
    """Black-box E2E on Profile C (API embedding)."""
    if not os.environ.get("RETRIEVAL_EMBEDDING_API_BASE"):
        pytest.skip("Profile C requires RETRIEVAL_EMBEDDING_API_BASE")
    results = await _run_profile("C", tmp_path)
    _write_report({"C": results}, tmp_path)

    agg = _aggregate_results(results)
    # Profile C floor: baseline 0.833, allow ~15% regression
    assert agg["hr"] >= 0.70, (
        f"Profile C HR={agg['hr']:.3f} below floor 0.70"
    )


@pytest.mark.asyncio
async def test_e2e_blackbox_profile_d(tmp_path):
    """Black-box E2E on Profile D (API embedding + reranker)."""
    profiles = _detect_available_profiles()
    if "D" not in profiles:
        pytest.skip("Profile D requires embedding + reranker env vars")
    results = await _run_profile("D", tmp_path)
    _write_report({"D": results}, tmp_path)

    agg = _aggregate_results(results)
    # Profile D floor: baseline 0.889, allow ~15% regression
    assert agg["hr"] >= 0.75, (
        f"Profile D HR={agg['hr']:.3f} below floor 0.75"
    )


@pytest.mark.asyncio
async def test_e2e_blackbox_all_profiles(tmp_path):
    """Run all available profiles and generate comparison report."""
    profiles = _detect_available_profiles()
    all_results: Dict[str, List[ScenarioResult]] = {}

    for pk in profiles:
        all_results[pk] = await _run_profile(pk, tmp_path)

    _write_report(all_results, tmp_path)

    # Validate monotonic improvement on MRR (ranking quality).
    # HR may not be strictly monotonic because reranker can occasionally
    # re-rank a borderline hit out of top-k, but MRR must improve.
    if len(profiles) >= 2:
        mrrs = {pk: _aggregate_results(rs)["mrr"] for pk, rs in all_results.items()}
        for i in range(len(profiles) - 1):
            lo, hi = profiles[i], profiles[i + 1]
            assert mrrs[lo] <= mrrs[hi], (
                f"MRR monotonic violation: {lo}({mrrs[lo]:.3f}) > {hi}({mrrs[hi]:.3f})"
            )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _run_profile(profile: str, tmp_path: Path) -> List[ScenarioResult]:
    scenarios = _load_scenarios()
    db_path = tmp_path / f"e2e_bb_{profile}.db"
    env = _build_env(db_path, profile)
    server = _build_server(env)

    results: List[ScenarioResult] = []
    stderr_path = tmp_path / f"e2e_bb_{profile}.stderr.log"

    with stderr_path.open("w+", encoding="utf-8") as errlog:
        async with stdio_client(server, errlog=errlog) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()

                # Verify tool inventory
                tools = await session.list_tools()
                discovered = {t.name for t in tools.tools}
                assert "search_memory" in discovered, (
                    f"search_memory not in MCP tools: {discovered}"
                )

                for sc in scenarios:
                    sr = await _evaluate_scenario(session, sc, profile)
                    results.append(sr)

    return results


def _write_report(
    all_results: Dict[str, List[ScenarioResult]], tmp_path: Path,
) -> None:
    report = {
        "benchmark": "e2e_blackbox",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profiles": {},
    }

    for pk, results in all_results.items():
        agg = _aggregate_results(results)
        per_scenario = [
            {
                "scenario_id": r.scenario_id,
                "category": r.category,
                "hit": r.hit,
                "rank": r.rank,
                "mrr": round(r.mrr, 4),
                "content_match": r.content_match,
                "guard_correct": r.guard_correct,
                "delete_verified": r.delete_verified,
                "latency_ms": round(r.latency_ms, 1),
                "error": r.error,
                "details": r.details,
            }
            for r in results
        ]
        report["profiles"][pk] = {
            "overall": agg,
            "per_scenario": per_scenario,
        }

    # Write to benchmark dir (persistent) and tmp_path (for pytest)
    for path in (_BENCH / "e2e_blackbox_report.json", tmp_path / "e2e_blackbox_report.json"):
        with open(path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
