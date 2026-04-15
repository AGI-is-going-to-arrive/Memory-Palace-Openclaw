"""Tier 2 Calibration: Real OpenClaw memory-core search via CLI.

Runs the same scenarios as the replica benchmark through the REAL
OpenClaw memory-core search engine (``openclaw memory search``),
using an isolated ``--profile bench-native`` environment.

This is a calibration test — it validates whether the
NativeMemoryCoreReplica produces results consistent with the
real memory-core engine.  It is NOT the primary benchmark.

Prerequisites:
  1. ``openclaw --profile bench-native`` is configured with memory-core
  2. Workspace at ``~/.openclaw/workspace-bench-native/memory/`` is populated
  3. Memory index is built: ``openclaw --profile bench-native memory index --force``
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from helpers.e2e_eval import ScenarioResult, aggregate_results, eval_search, eval_delete, eval_read

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_BENCH = Path(__file__).resolve().parent
_SCENARIOS_FILE = "e2e_blackbox_scenarios.jsonl"
_PROFILE = "bench-native"

_NA_CATEGORIES = {"conflict_guard", "compact_recall"}

# Map scenario URIs to workspace file paths
# These must match the files written to ~/.openclaw/workspace-bench-native/memory/


def _load_scenarios() -> List[Dict]:
    path = _FIXTURES / _SCENARIOS_FILE
    assert path.exists(), f"Scenarios not found: {path}"
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _parse_uri(uri: str) -> Tuple[str, str]:
    parts = uri.split("://", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("unknown", uri)


def _real_memory_search(query: str, max_results: int = 10) -> List[Dict]:
    """Call real memory-core search via OpenClaw CLI."""
    cmd = [
        "openclaw", "--profile", _PROFILE, "--no-color",
        "memory", "search",
        "--query", query,
        "--max-results", str(max_results),
        "--json",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )
        # C-1 fix: parse stdout only (stderr may contain warnings)
        output = result.stdout.strip()
        if not output or result.returncode != 0:
            return []
        start = output.find("{")
        if start < 0:
            return []
        data = json.loads(output[start:])
        if isinstance(data, dict) and "results" in data:
            data = data["results"]
        if not isinstance(data, list):
            return []
        # Normalize to match our eval format
        results = []
        for r in data:
            path = r.get("path", "")
            snippet = r.get("snippet", "")
            score = r.get("score", 0)
            # Extract a pseudo-URI from the file path
            uri = _path_to_uri(path, snippet)
            results.append({
                "uri": uri,
                "path": path,
                "content": snippet,
                "snippet": snippet,
                "score": score,
            })
        return results
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        import sys
        print(f"[calibration] memory search failed: {exc}", file=sys.stderr)
        return []


def _path_to_uri(path: str, snippet: str) -> str:
    """Convert memory-core file path to a domain://title URI for eval.

    The workspace files are named like:
      memory/2026-04-06-bench-sc01.md  → personal://饮食禁忌
      memory/2026-04-06-bench-sc04a.md → personal://allergy-cats
    We extract the URI from the file's first heading line.
    """
    # Try to extract title from snippet (first line is usually # title)
    lines = snippet.strip().split("\n")
    title = ""
    for line in lines:
        line = line.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip()
            break

    if not title:
        # Fallback: extract from filename
        fname = Path(path).stem
        title = fname.replace("2026-04-06-bench-", "")

    # Map known titles to scenario URIs
    return _TITLE_TO_URI.get(title, f"unknown://{title}")


# Mapping from file heading titles to scenario expected URIs
_TITLE_TO_URI = {
    "饮食禁忌": "personal://饮食禁忌",
    "allergy-cats": "personal://allergy-cats",
    "mascot": "project://mascot",
    "rate-limit-v1": "project://rate-limit-v1",
    "rate-limit-v2": "project://rate-limit-v2",
    "运动习惯": "personal://运动习惯",
    "python-list-comp": "learning://python-list-comp",
    "git-rebase": "learning://git-rebase",
    "sql-joins": "learning://sql-joins",
    "docker-volumes": "learning://docker-volumes",
    "ide-preference": "personal://ide-preference",
    "morning-routine": "personal://morning-routine",
    "ci-pipeline": "project://ci-pipeline",
    "error-monitoring": "project://error-monitoring",
    "saas-costs": "finance://saas-costs",
    "rag-metrics": "research://rag-metrics",
    "sprint-planning": "project://sprint-planning-2026-04",
    "sprint-planning-2026-04": "project://sprint-planning-2026-04",
    "rust-ownership": "learning://rust-ownership",
    "q2-budget": "finance://q2-budget",
    "tech-preference": "personal://tech-preference",
    "cors-fix": "project://cors-fix-nginx",
    "cors-fix-nginx": "project://cors-fix-nginx",
    "db-stack": "project://db-stack",
    "standup-routine": "personal://standup-routine",
    "docker-tips": "learning://docker-tips",
    "transformer-paper": "research://transformer-paper",
    "chapter-3": "writing://chapter-3",
    "lang-pref": "personal://lang-pref",
    "temp-debug-flag": "project://temp-debug-flag",
    "architecture-overview": "project://architecture-overview",
    "perf-baseline": "project://perf-baseline",
}


def _evaluate_scenario_real(scenario: Dict) -> ScenarioResult:
    """Run a single scenario through real memory-core CLI search."""
    sr = ScenarioResult(
        scenario_id=scenario["scenario_id"],
        category=scenario["category"],
        profile="real_native_core",
    )

    if scenario["category"] in _NA_CATEGORIES:
        sr.comparable = False
        sr.details["reason"] = f"No native equivalent for {scenario['category']}"
        return sr

    expected = scenario.get("expected", {})

    try:
        # For search-based scenarios, run the query
        if "action_sequence" in scenario:
            # For delete_verify: we can't delete via CLI, just search
            actions = scenario["action_sequence"]
            last_action = actions[-1]
            if last_action["tool"] == "search_memory":
                t0 = time.perf_counter()
                results = _real_memory_search(last_action["args"]["query"])
                sr.latency_ms = (time.perf_counter() - t0) * 1000
                result = {"results": results}
            else:
                sr.error = f"Unsupported action_sequence ending: {last_action['tool']}"
                return sr
        elif scenario.get("action", {}).get("tool") == "search_memory":
            query = scenario["action"]["args"]["query"]
            t0 = time.perf_counter()
            results = _real_memory_search(query)
            sr.latency_ms = (time.perf_counter() - t0) * 1000
            result = {"results": results}
        elif scenario.get("action", {}).get("tool") == "read_memory":
            # read_memory: check if content exists in any search result
            uri = scenario["action"]["args"]["uri"]
            _, title = _parse_uri(uri)
            t0 = time.perf_counter()
            results = _real_memory_search(title)
            sr.latency_ms = (time.perf_counter() - t0) * 1000
            # For read, check if content substring is in any result
            text = " ".join(r.get("content", "") for r in results)
            result = text
        else:
            sr.error = f"Unsupported action tool: {scenario.get('action', {}).get('tool')}"
            return sr

        # Evaluate
        if scenario["category"] == "delete_verify":
            eval_delete(sr, result, expected)
        elif scenario["category"] == "namespace_read":
            eval_read(sr, result, expected)
        else:
            eval_search(sr, result, expected)

    except Exception as exc:
        sr.error = str(exc)

    return sr


def test_real_native_calibration():
    """Calibration: run spot-check scenarios through real OpenClaw memory-core.

    Compares real native results with NativeMemoryCoreReplica results.
    """
    # Verify memory-core is indexed
    status_cmd = subprocess.run(
        ["openclaw", "--profile", _PROFILE, "--no-color", "memory", "status", "--json"],
        capture_output=True, text=True, timeout=15,
    )
    if "files" not in status_cmd.stdout + status_cmd.stderr:
        pytest.skip("memory-core not indexed; run: openclaw --profile bench-native memory index --force")

    scenarios = _load_scenarios()
    results: List[ScenarioResult] = []

    for sc in scenarios:
        if sc["category"] in _NA_CATEGORIES:
            sr = ScenarioResult(
                scenario_id=sc["scenario_id"],
                category=sc["category"],
                profile="real_native_core",
                comparable=False,
            )
            sr.details["reason"] = f"N/A: {sc['category']}"
            results.append(sr)
            continue

        # Skip delete_verify (can't delete via CLI in calibration)
        if sc["category"] == "delete_verify":
            sr = ScenarioResult(
                scenario_id=sc["scenario_id"],
                category=sc["category"],
                profile="real_native_core",
                comparable=False,
            )
            sr.details["reason"] = "Skip: delete not testable via CLI calibration"
            results.append(sr)
            continue

        sr = _evaluate_scenario_real(sc)
        results.append(sr)

    agg = aggregate_results(results, na_categories=_NA_CATEGORIES | {"delete_verify"})

    # Load replica report for comparison
    replica_path = _BENCH / "e2e_native_memory_core_report.json"
    replica_data = {}
    if replica_path.exists():
        with open(replica_path) as f:
            replica_data = json.load(f)

    # Build calibration report
    calibration = _build_calibration_report(results, agg, replica_data)
    out = _BENCH / "e2e_real_native_calibration.json"
    with open(out, "w") as f:
        json.dump(calibration, f, indent=2, ensure_ascii=False)

    # Print summary
    print(f"\n=== Real Native Calibration ===")
    print(f"  Comparable: {agg.get('n_comparable', 0)}")
    print(f"  HR:  {agg.get('hr', 0):.3f}")
    print(f"  MRR: {agg.get('mrr', 0):.3f}")
    print(f"  ContentMatch: {agg.get('content_match_rate', 0):.3f}")

    if "calibration_delta" in calibration:
        cd = calibration["calibration_delta"]
        print(f"\n  Replica vs Real Delta:")
        print(f"    HR:  {cd.get('hr_delta', 0):+.3f}")
        print(f"    MRR: {cd.get('mrr_delta', 0):+.3f}")
        print(f"    Agreement: {cd.get('agreement_rate', 0):.1%}")

    for cat, ca in agg.get("by_category", {}).items():
        print(f"    {cat}: HR={ca['hr']:.2f} MRR={ca['mrr']:.3f}")


def _build_calibration_report(
    results: List[ScenarioResult],
    agg: Dict,
    replica_data: Dict,
) -> Dict:
    replica_by_id = {}
    for s in replica_data.get("per_scenario", []):
        replica_by_id[s["scenario_id"]] = s

    per_scenario = []
    agreements = 0
    comparable_count = 0

    for r in results:
        rep = replica_by_id.get(r.scenario_id, {})
        entry = {
            "scenario_id": r.scenario_id,
            "category": r.category,
            "comparable": r.comparable,
            "real_hit": r.hit if r.comparable else None,
            "real_mrr": round(r.mrr, 4) if r.comparable else None,
            "real_rank": r.rank if r.comparable else None,
            "real_latency_ms": round(r.latency_ms, 1) if r.comparable else None,
            "replica_hit": rep.get("hit"),
            "replica_mrr": rep.get("mrr"),
            "replica_rank": rep.get("rank"),
            "agreement": None,
            "details": r.details,
        }

        if r.comparable and rep.get("comparable", True) and rep.get("hit") is not None:
            comparable_count += 1
            entry["agreement"] = r.hit == rep["hit"]
            if entry["agreement"]:
                agreements += 1

        per_scenario.append(entry)

    delta = {}
    if replica_data.get("overall"):
        ro = replica_data["overall"]
        delta = {
            "hr_delta": agg.get("hr", 0) - ro.get("hr", 0),
            "mrr_delta": agg.get("mrr", 0) - ro.get("mrr", 0),
            "agreement_rate": agreements / comparable_count if comparable_count > 0 else 0,
            "comparable_scenarios": comparable_count,
            "agreements": agreements,
        }

    return {
        "benchmark": "e2e_real_native_calibration",
        "engine": "OpenClaw memory-core (real CLI search)",
        "profile": _PROFILE,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall": agg,
        "calibration_delta": delta,
        "per_scenario": per_scenario,
        "conclusion": (
            "This calibration compares the NativeMemoryCoreReplica "
            "(FTS5/BM25 keyword-only) against the real OpenClaw memory-core "
            "search engine (FTS5 + vector hybrid) running in an isolated profile. "
            "Differences are expected because the replica lacks vector search. "
            "The calibration informs whether the replica's fidelity claim "
            "needs adjustment."
        ),
    }
