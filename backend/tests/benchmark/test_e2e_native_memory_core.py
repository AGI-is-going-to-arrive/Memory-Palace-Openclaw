"""Memory-Core Replica Baseline Harness (native_memory_core_replica)

Deterministic keyword-only replica approximating OpenClaw memory-core
search semantics.  Uses the same 18 scenarios as the black-box MCP
benchmark, with identical scoring from helpers/e2e_eval.py.

This is NOT a "real OpenClaw native run".  It replicates the search
engine in isolation, without the full OpenClaw host/gateway/agent loop,
plugin routing, provider health, or session/hook behavior.

Engine: helpers/native_memory_core_replica.py
Spec:   backend/tests/benchmark/E2E_BLACKBOX_SPEC.md §5.2
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from helpers.e2e_eval import (
    ScenarioResult,
    aggregate_results,
    eval_delete,
    eval_read,
    eval_search,
)
from helpers.native_memory_core_replica import NativeMemoryCoreReplica

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_BENCH = Path(__file__).resolve().parent
_SCENARIOS_FILE = "e2e_blackbox_scenarios.jsonl"

# Categories with no memory-core equivalent — reported as MP advantage
_NA_CATEGORIES = {"conflict_guard", "compact_recall"}


# ---------------------------------------------------------------------------
# Scenario execution
# ---------------------------------------------------------------------------


def _load_scenarios() -> List[Dict]:
    path = _FIXTURES / _SCENARIOS_FILE
    assert path.exists(), f"Scenarios not found: {path}"
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _parse_uri(uri: str) -> Tuple[str, str]:
    """Parse 'domain://title' into (domain, title)."""
    parts = uri.split("://", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("unknown", uri)


def _execute_tool(mem: NativeMemoryCoreReplica, tool: str, args: Dict) -> Any:
    """Map MCP tool calls to NativeMemoryCoreReplica operations."""
    if tool == "create_memory":
        parent_uri = args.get("parent_uri", "")
        domain = parent_uri.split("://")[0] if "://" in parent_uri else "unknown"
        title = args.get("title", "untitled")
        content = args.get("content", "")
        uri = mem.create(domain, title, content)
        return {"created": True, "uri": uri}

    elif tool == "search_memory":
        query = args.get("query", "")
        results = mem.search(query)
        return {"ok": True, "results": results}

    elif tool == "read_memory":
        uri = args.get("uri", "")
        domain, title = _parse_uri(uri)
        content = mem.read(domain, title)
        return content if content else {"error": "not found"}

    elif tool == "update_memory":
        uri = args.get("uri", "")
        domain, title = _parse_uri(uri)
        old_s = args.get("old_string", "")
        new_s = args.get("new_string", "")
        full_content = args.get("content")
        ok = mem.update(domain, title, old_s, new_s, content=full_content)
        return {"ok": ok}

    elif tool == "delete_memory":
        uri = args.get("uri", "")
        domain, title = _parse_uri(uri)
        mem.delete(domain, title)
        return {"ok": True}

    elif tool == "add_alias":
        source_uri = args.get("source_uri", "")
        alias_uri = args.get("alias_uri", "")
        _, source_title = _parse_uri(source_uri)
        _, alias_title = _parse_uri(alias_uri)
        mem.add_alias(source_title, alias_title)
        return {"ok": True}

    elif tool == "compact_context":
        return {"ok": True, "native_na": True}

    return {"error": f"Unknown tool: {tool}"}


def _run_setup(mem: NativeMemoryCoreReplica, steps: List[Dict]) -> None:
    for step in steps:
        _execute_tool(mem, step["tool"], step["args"])


def _evaluate_scenario(mem: NativeMemoryCoreReplica, scenario: Dict) -> ScenarioResult:
    sr = ScenarioResult(
        scenario_id=scenario["scenario_id"],
        category=scenario["category"],
        profile="native_core_replica",
    )

    # Skip non-comparable categories
    if scenario["category"] in _NA_CATEGORIES:
        sr.comparable = False
        sr.details["reason"] = f"No native-core equivalent for {scenario['category']}"
        return sr

    try:
        _run_setup(mem, scenario.get("setup", []))
        expected = scenario.get("expected", {})

        if "action_sequence" in scenario:
            actions = scenario["action_sequence"]
            result = None
            t0 = time.perf_counter()
            for act in actions:
                result = _execute_tool(mem, act["tool"], act["args"])
            sr.latency_ms = (time.perf_counter() - t0) * 1000
        else:
            action = scenario["action"]
            t0 = time.perf_counter()
            result = _execute_tool(mem, action["tool"], action["args"])
            sr.latency_ms = (time.perf_counter() - t0) * 1000

        if scenario["category"] == "delete_verify":
            eval_delete(sr, result, expected)
        elif scenario["category"] == "namespace_read":
            eval_read(sr, result, expected)
        else:
            eval_search(sr, result, expected)

    except Exception as exc:
        sr.error = str(exc)

    return sr


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def _write_report(results: List[ScenarioResult], agg: Dict) -> None:
    report = {
        "benchmark": "e2e_native_memory_core_replica",
        "engine": "NativeMemoryCoreReplica",
        "engine_description": (
            "FTS5/BM25 + temporal-decay keyword-only replica "
            "approximating OpenClaw memory-core search semantics. "
            "Not a real OpenClaw native run."
        ),
        "engine_config": {
            "fts5": True,
            "bm25": True,
            "vector": False,
            "mmr": False,
            "temporal_decay": True,
            "temporal_decay_half_life_days": 30.0,
            "chunk_size_chars": 400,
            "chunk_overlap_chars": 80,
        },
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": "native_core_replica",
        "overall": agg,
        "per_scenario": [
            {
                "scenario_id": r.scenario_id,
                "category": r.category,
                "comparable": r.comparable,
                "hit": r.hit,
                "rank": r.rank,
                "mrr": round(r.mrr, 4),
                "content_match": r.content_match,
                "latency_ms": round(r.latency_ms, 1),
                "error": r.error,
                "details": r.details,
            }
            for r in results
        ],
    }

    out = _BENCH / "e2e_native_memory_core_report.json"
    with open(out, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)


def _build_comparison(
    results: List[ScenarioResult],
    native_agg: Dict,
    mp_report: Dict,
) -> Dict:
    """Build per-scenario and aggregate comparison: MP vs native-core replica."""
    native_by_id = {r.scenario_id: r for r in results}

    per_scenario = []
    for profile_key in ("B", "C", "D"):
        mp_profile = mp_report.get("profiles", {}).get(profile_key)
        if not mp_profile:
            continue
        for mp_sc in mp_profile["per_scenario"]:
            sid = mp_sc["scenario_id"]
            nr = native_by_id.get(sid)
            if not nr:
                continue
            per_scenario.append({
                "scenario_id": sid,
                "category": mp_sc["category"],
                "comparable": nr.comparable,
                "mp_profile": profile_key,
                "mp_hit": mp_sc["hit"],
                "mp_mrr": mp_sc["mrr"],
                "mp_latency_ms": mp_sc["latency_ms"],
                "replica_hit": nr.hit if nr.comparable else None,
                "replica_mrr": round(nr.mrr, 4) if nr.comparable else None,
                "replica_latency_ms": round(nr.latency_ms, 1) if nr.comparable else None,
                "winner": _determine_winner(mp_sc, nr, profile_key),
            })

    agg_comparison = {}
    for profile_key in ("B", "C", "D"):
        mp_profile = mp_report.get("profiles", {}).get(profile_key)
        if not mp_profile:
            continue
        mp_overall = mp_profile["overall"]
        agg_comparison[profile_key] = {
            "mp_hr": mp_overall["hr"],
            "mp_mrr": mp_overall["mrr"],
            "mp_denominator": mp_overall["n"],
            "replica_hr": native_agg["hr"],
            "replica_mrr": native_agg["mrr"],
            "replica_denominator": native_agg["n_comparable"],
            "denominator_note": (
                "MP aggregate uses all 18 scenarios as denominator; "
                "replica uses 16 comparable scenarios (SC07/SC09 excluded)"
            ),
            "mp_advantage_categories": sorted(_NA_CATEGORIES),
        }

    return {
        "benchmark": "e2e_mp_vs_native_memory_core_replica",
        "comparison_target": (
            "NativeMemoryCoreReplica — FTS5/BM25 + temporal-decay "
            "keyword-only replica approximating OpenClaw memory-core "
            "search semantics (default config, no embedding provider)"
        ),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "naming_note": (
            "All replica_* fields refer to NativeMemoryCoreReplica "
            "(FTS5/BM25 keyword-only benchmark replica), NOT a real "
            "OpenClaw native memory run."
        ),
        "replica_overall": native_agg,
        "comparison_aggregate": agg_comparison,
        "per_scenario": per_scenario,
        "conclusion_boundary": (
            "This comparison measures Memory Palace MCP e2e vs a "
            "deterministic FTS5/BM25 keyword-only replica that approximates "
            "OpenClaw memory-core search semantics (default configuration, "
            "no embedding provider).  This is NOT a real OpenClaw native run — "
            "it does not include the full host/gateway/agent loop, plugin "
            "routing, provider health checks, or session/hook behavior.  "
            "MP advantages in compact_context and write_guard have no "
            "memory-core equivalent and are excluded from comparable metrics.  "
            "Results inform relative positioning but do not constitute a "
            "claim that MP is superior to OpenClaw native memory in all "
            "real-world usage scenarios."
        ),
    }


def _determine_winner(mp_sc: Dict, nr: ScenarioResult, profile: str) -> str:
    if not nr.comparable:
        return "mp_advantage_no_native_equivalent"
    mp_hit = mp_sc.get("hit", False)
    native_hit = nr.hit
    if mp_hit and not native_hit:
        return f"mp_{profile}"
    elif native_hit and not mp_hit:
        return "replica"
    elif mp_hit and native_hit:
        mp_mrr = mp_sc.get("mrr", 0)
        native_mrr = nr.mrr
        if abs(mp_mrr - native_mrr) < 0.01:
            return "tie"
        return f"mp_{profile}" if mp_mrr > native_mrr else "replica"
    else:
        return "both_miss"


def _write_comparison(comparison: Dict) -> None:
    out = _BENCH / "e2e_mp_vs_native_core_comparison.json"
    with open(out, "w") as f:
        json.dump(comparison, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2e_native_memory_core_baseline(tmp_path):
    """Run all 18 scenarios against NativeMemoryCoreReplica (FTS5/BM25).

    This is a deterministic keyword-only baseline approximating
    OpenClaw memory-core search semantics.  Not a real OpenClaw native run.
    """
    scenarios = _load_scenarios()
    mem = NativeMemoryCoreReplica(tmp_path)
    results: List[ScenarioResult] = []

    for sc in scenarios:
        sr = _evaluate_scenario(mem, sc)
        results.append(sr)

    mem.close()

    agg = aggregate_results(results, na_categories=_NA_CATEGORIES)
    _write_report(results, agg)

    # Assertions
    assert agg["n_comparable"] > 0, "No comparable scenarios"
    assert agg.get("n_na", 0) == 2, f"Expected 2 N/A, got {agg.get('n_na')}"

    # Print summary for CI visibility
    print(f"\n=== NativeMemoryCoreReplica Baseline ===")
    print(f"  Comparable: {agg['n_comparable']}")
    print(f"  HR:  {agg['hr']:.3f}")
    print(f"  MRR: {agg['mrr']:.3f}")
    print(f"  ContentMatch: {agg['content_match_rate']:.3f}")
    print(f"  P95 Latency: {agg['p95_latency_ms']:.1f}ms")
    for cat, ca in agg.get("by_category", {}).items():
        print(f"    {cat}: HR={ca['hr']:.2f} MRR={ca['mrr']:.3f} (n={ca['n']})")


def test_e2e_native_core_vs_mp_comparison(tmp_path):
    """Generate comparison report: NativeMemoryCoreReplica vs MP black-box."""
    # Run native-core replica
    scenarios = _load_scenarios()
    mem = NativeMemoryCoreReplica(tmp_path)
    results: List[ScenarioResult] = []

    for sc in scenarios:
        sr = _evaluate_scenario(mem, sc)
        results.append(sr)

    mem.close()

    agg = aggregate_results(results, na_categories=_NA_CATEGORIES)
    _write_report(results, agg)

    # Load MP black-box report
    mp_report_path = _BENCH / "e2e_blackbox_report.json"
    if not mp_report_path.exists():
        pytest.skip("MP blackbox report not found; run test_e2e_blackbox_harness.py first")

    with open(mp_report_path) as f:
        mp_report = json.load(f)

    comparison = _build_comparison(results, agg, mp_report)
    _write_comparison(comparison)

    print(f"\n=== MP vs NativeMemoryCoreReplica Comparison ===")
    for pk, agg_c in comparison.get("comparison_aggregate", {}).items():
        print(f"  Profile {pk}: MP HR={agg_c['mp_hr']:.3f} MRR={agg_c['mp_mrr']:.3f}"
              f"  |  Replica HR={agg_c['replica_hr']:.3f} MRR={agg_c['replica_mrr']:.3f}")
