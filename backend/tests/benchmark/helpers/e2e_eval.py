"""Shared evaluation helpers for E2E benchmark harnesses.

Provides ScenarioResult dataclass, per-scenario evaluation functions,
and aggregation logic used by:
  - test_e2e_blackbox_harness.py  (MP MCP black-box)
  - test_e2e_native_harness.py    (fs_keyword_baseline, historical)
  - test_e2e_native_memory_core.py (memory-core replica)

Extracted to avoid duplicating scoring logic across harnesses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Set


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ScenarioResult:
    scenario_id: str
    category: str
    profile: str
    hit: bool = False
    rank: int = 0
    mrr: float = 0.0
    content_match: bool = False
    guard_correct: bool = False
    delete_verified: bool = False
    latency_ms: float = 0.0
    error: str = ""
    comparable: bool = True
    details: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-scenario evaluation
# ---------------------------------------------------------------------------


def eval_search(sr: ScenarioResult, result: Any, expected: Dict) -> None:
    """Evaluate search_memory results (single or multi-URI)."""
    if not isinstance(result, dict):
        sr.error = f"Unexpected result type: {type(result)}"
        return

    results = result.get("results", [])
    uris = [r.get("uri", "") for r in results]
    sr.details["returned_uris"] = uris[:10]
    sr.details["result_count"] = len(results)

    # Single URI match
    target = expected.get("must_contain_uri")
    if target:
        if target in uris:
            rank = uris.index(target) + 1
            sr.hit = True
            sr.rank = rank
            sr.mrr = 1.0 / rank
        else:
            sr.hit = False
            sr.rank = 0
            sr.mrr = 0.0

    # Multi-URI match
    targets = expected.get("must_contain_uris", [])
    if targets:
        ranks = []
        for t in targets:
            if t in uris:
                ranks.append(uris.index(t) + 1)
        hits = len(ranks)
        min_hits = expected.get("min_hits", len(targets))
        sr.hit = hits >= min_hits
        sr.mrr = (sum(1.0 / r for r in ranks) / len(targets)) if ranks else 0.0
        sr.rank = min(ranks) if ranks else 0
        sr.details["multi_hit_count"] = hits
        sr.details["multi_hit_ranks"] = ranks

    # Rank within threshold
    rank_limit = expected.get("must_rank_within")
    if rank_limit and sr.rank > 0:
        sr.details["rank_within_target"] = sr.rank <= rank_limit

    # Content substring check (searches both content and snippet fields)
    substring = expected.get("content_substring")
    if substring and results:
        sr.content_match = any(
            substring.lower()
            in (r.get("content", "") + r.get("snippet", "")).lower()
            for r in results
        )


def eval_guard(sr: ScenarioResult, result: Any, expected: Dict) -> None:
    """Evaluate create_memory guard behavior (MP-only, SC07)."""
    if not isinstance(result, dict):
        sr.error = f"Unexpected result type: {type(result)}"
        return

    guard_action = result.get("guard_action", "")
    allowed = expected.get("guard_action_in", [])
    sr.guard_correct = guard_action in allowed

    if expected.get("created_false"):
        sr.guard_correct = sr.guard_correct and not result.get("created", True)

    sr.hit = sr.guard_correct
    sr.mrr = 1.0 if sr.guard_correct else 0.0
    sr.details["guard_action"] = guard_action
    sr.details["created"] = result.get("created")
    sr.details["guard_target_uri"] = result.get("guard_target_uri")
    if not sr.guard_correct:
        sr.details["full_response"] = {
            k: v
            for k, v in result.items()
            if k in ("guard_action", "created", "guard_target_uri", "message")
        }


def eval_delete(sr: ScenarioResult, result: Any, expected: Dict) -> None:
    """Evaluate delete + search verification (SC10)."""
    if not isinstance(result, dict):
        sr.error = f"Unexpected result type: {type(result)}"
        return

    results = result.get("results", [])
    uris = [r.get("uri", "") for r in results]
    forbidden = expected.get("must_not_contain_uri")
    if forbidden is None:
        sr.error = "must_not_contain_uri not specified in expected"
        return
    sr.delete_verified = forbidden not in uris
    sr.hit = sr.delete_verified
    sr.mrr = 1.0 if sr.delete_verified else 0.0
    sr.details["returned_uris"] = uris[:10]


def eval_read(sr: ScenarioResult, result: Any, expected: Dict) -> None:
    """Evaluate read_memory / memory_get result (SC11)."""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    substring = expected.get("content_substring", "")
    sr.content_match = substring.lower() in text.lower()
    sr.hit = sr.content_match
    sr.mrr = 1.0 if sr.content_match else 0.0


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def aggregate_results(
    results: List[ScenarioResult],
    *,
    na_categories: Set[str] | None = None,
) -> Dict:
    """Aggregate scenario results into summary metrics.

    If *na_categories* is provided, scenarios whose category is in the set
    are excluded from comparable metrics and counted as N/A.  Otherwise all
    results are treated as comparable (backwards-compatible with blackbox harness).
    """
    if na_categories is not None:
        comparable = [r for r in results if r.category not in na_categories and r.comparable]
        na_count = len(results) - len(comparable)
    else:
        comparable = [r for r in results if r.comparable]
        na_count = len(results) - len(comparable)

    if not comparable:
        base: Dict[str, Any] = {"n": len(results), "n_comparable": 0, "hr": 0, "mrr": 0}
        if na_categories is not None:
            base["n_na"] = na_count
            base["na_categories"] = sorted(na_categories)
        return base

    n = len(comparable)
    hits = sum(1 for r in comparable if r.hit)
    latencies = sorted(r.latency_ms for r in comparable)
    p95_idx = min(int(n * 0.95), n - 1)

    by_category: Dict[str, List[ScenarioResult]] = {}
    for r in comparable:
        by_category.setdefault(r.category, []).append(r)

    cat_agg = {}
    for cat, cat_results in by_category.items():
        cn = len(cat_results)
        cat_agg[cat] = {
            "n": cn,
            "hr": sum(1 for r in cat_results if r.hit) / cn,
            "mrr": sum(r.mrr for r in cat_results) / cn,
        }

    agg: Dict[str, Any] = {
        "n": len(results),
        "n_comparable": n,
        "hr": hits / n,
        "mrr": sum(r.mrr for r in comparable) / n,
        "content_match_rate": sum(1 for r in comparable if r.content_match) / n,
        "p95_latency_ms": latencies[p95_idx],
        "by_category": cat_agg,
    }
    if na_categories is not None:
        agg["n_na"] = na_count
        agg["na_categories"] = sorted(na_categories)

    return agg
