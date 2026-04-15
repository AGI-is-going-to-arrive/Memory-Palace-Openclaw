"""Write Guard C/D score analysis: collect per-case raw scores + offline simulation.

Phase 1: Seed DB with gold set memories, run search_advanced per case, collect:
  - Top-K semantic vector_scores (for gap analysis)
  - Top keyword text_score
  - Expected vs current-logic predicted action

Phase 2: Offline simulate multiple strategies:
  - Baseline (current thresholds 0.92/0.78)
  - Strategy A: margin-based (top1-top2 gap)
  - Strategy B: expanded cross-check
  - Strategy A+B combined

Output: per-action distributions, confusion matrices, P/R/F1/EM per strategy.

Usage:
    RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_EMBEDDING_API_KEY=... \
    RETRIEVAL_EMBEDDING_MODEL=... RETRIEVAL_EMBEDDING_DIM=1024 \
    RETRIEVAL_RERANKER_API_BASE=... RETRIEVAL_RERANKER_API_KEY=... \
    RETRIEVAL_RERANKER_MODEL=... \
    backend/.venv/bin/python backend/tests/benchmark/run_write_guard_score_analysis.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BENCHMARK_DIR.parent / "fixtures"
BACKEND_ROOT = BENCHMARK_DIR.parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db.sqlite_client import SQLiteClient

REPORT_PATH = BENCHMARK_DIR / "write_guard_score_analysis.json"
REPORT_MD = BENCHMARK_DIR / "write_guard_score_analysis.md"


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


async def _ensure_parent_chain(client, domain: str, full_path: str):
    segments = full_path.split("/")
    for depth in range(1, len(segments)):
        ancestor_parent = "/".join(segments[:depth - 1])
        ancestor_title = segments[depth - 1]
        try:
            await client.create_memory(
                parent_path=ancestor_parent,
                content="(ancestor placeholder)",
                priority=100,
                title=ancestor_title,
                domain=domain,
                index_now=False,
            )
        except Exception:
            pass


async def _seed_memories(client, memories: List[Dict[str, Any]]):
    for mem in memories:
        uri = mem.get("uri", "core://test/default")
        content = mem.get("content", "")
        domain = mem.get("domain", "core")
        parts = uri.split("://", 1)
        if len(parts) == 2:
            domain, full_path = parts
        else:
            full_path = uri
        path_segments = full_path.rsplit("/", 1)
        if len(path_segments) == 2:
            parent_path, title = path_segments
        else:
            parent_path, title = "", path_segments[0]
        try:
            await _ensure_parent_chain(client, domain, full_path)
            await client.create_memory(
                parent_path=parent_path, content=content,
                priority=10, title=title, domain=domain,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Phase 1: Collect raw scores
# ---------------------------------------------------------------------------

async def collect_raw_scores(
    gold: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Seed DB, run search_advanced for each case, return per-case raw scores."""
    os.environ["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
    os.environ["RETRIEVAL_RERANKER_ENABLED"] = "true"
    os.environ["RETRIEVAL_RERANKER_WEIGHT"] = "0.30"
    os.environ["INTENT_LLM_ENABLED"] = "false"
    os.environ["WRITE_GUARD_LLM_ENABLED"] = "false"
    os.environ["COMPACT_GIST_LLM_ENABLED"] = "false"

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="score_analysis_")
    os.close(fd)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    client = SQLiteClient(db_url)
    await client.init_db()

    # Seed all existing memories from gold set
    all_existing = []
    for row in gold:
        all_existing.extend(row.get("existing_memories", []))
    seen_uris: set = set()
    unique_existing = []
    for mem in all_existing:
        uri = mem.get("uri", "")
        if uri not in seen_uris:
            seen_uris.add(uri)
            unique_existing.append(mem)

    print(f"[analysis] Seeding {len(unique_existing)} memories...", file=sys.stderr, flush=True)
    await _seed_memories(client, unique_existing)

    try:
        await client.rebuild_index()
    except Exception:
        pass

    print(f"[analysis] Collecting scores for {len(gold)} cases...", file=sys.stderr, flush=True)
    scored_cases = []

    for i, row in enumerate(gold):
        content = str(row["content"])
        expected = str(row["expected_action"]).upper()
        domain = "core"
        filters = {"domain": domain}

        # Collect semantic search results (top-6 for gap analysis)
        try:
            semantic_payload = await client.search_advanced(
                query=content, mode="semantic",
                max_results=6, candidate_multiplier=6, filters=filters,
            )
        except Exception:
            semantic_payload = {"results": [], "degrade_reasons": []}

        # Collect keyword search results
        try:
            keyword_payload = await client.search_advanced(
                query=content, mode="keyword",
                max_results=6, candidate_multiplier=6, filters=filters,
            )
        except Exception:
            keyword_payload = {"results": [], "degrade_reasons": []}

        # Extract ALL semantic scores (sorted desc) for gap analysis
        sem_results = semantic_payload.get("results", [])
        sem_scores_all = sorted(
            [float(r.get("scores", {}).get("vector", 0) or 0) for r in sem_results],
            reverse=True,
        )

        # Extract ALL keyword scores (sorted desc)
        kw_results = keyword_payload.get("results", [])
        kw_scores_all = sorted(
            [float(r.get("scores", {}).get("text", 0) or 0) for r in kw_results],
            reverse=True,
        )

        # Extract final scores too
        sem_final_scores = sorted(
            [float(r.get("scores", {}).get("final", 0) or 0) for r in sem_results],
            reverse=True,
        )

        # Top-1 details
        sem_top_vector = sem_scores_all[0] if sem_scores_all else 0.0
        sem_top_id = None
        for r in sem_results:
            vs = float(r.get("scores", {}).get("vector", 0) or 0)
            if abs(vs - sem_top_vector) < 1e-6:
                sem_top_id = r.get("memory_id")
                break

        kw_top_text = kw_scores_all[0] if kw_scores_all else 0.0
        kw_top_id = None
        for r in kw_results:
            ts = float(r.get("scores", {}).get("text", 0) or 0)
            if abs(ts - kw_top_text) < 1e-6:
                kw_top_id = r.get("memory_id")
                break

        # Compute gaps
        top1_top2_gap = (sem_scores_all[0] - sem_scores_all[1]) if len(sem_scores_all) >= 2 else None
        top1_top3_gap = (sem_scores_all[0] - sem_scores_all[2]) if len(sem_scores_all) >= 3 else None

        scored_cases.append({
            "id": row["id"],
            "expected": expected,
            "sem_vector_score": round(sem_top_vector, 6),
            "sem_memory_id": sem_top_id,
            "sem_scores_topk": [round(s, 6) for s in sem_scores_all[:6]],
            "sem_final_scores_topk": [round(s, 6) for s in sem_final_scores[:6]],
            "sem_candidates_count": len(sem_results),
            "kw_text_score": round(kw_top_text, 6),
            "kw_memory_id": kw_top_id,
            "kw_scores_topk": [round(s, 6) for s in kw_scores_all[:6]],
            "kw_candidates_count": len(kw_results),
            "top1_top2_gap": round(top1_top2_gap, 6) if top1_top2_gap is not None else None,
            "top1_top3_gap": round(top1_top3_gap, 6) if top1_top3_gap is not None else None,
            "sem_degrade": semantic_payload.get("degrade_reasons", []),
            "kw_degrade": keyword_payload.get("degrade_reasons", []),
        })

        if (i + 1) % 20 == 0:
            print(f"[analysis] Progress: {i + 1}/{len(gold)}", file=sys.stderr, flush=True)

    await client.close()
    try:
        os.unlink(db_path)
    except Exception:
        pass

    return scored_cases


# ---------------------------------------------------------------------------
# Phase 2: Offline simulation
# ---------------------------------------------------------------------------

def simulate_baseline(cases: List[Dict[str, Any]],
                      sem_noop: float = 0.92, sem_update: float = 0.78,
                      kw_noop: float = 0.82, kw_update: float = 0.55) -> List[str]:
    """Current write_guard logic."""
    predictions = []
    for c in cases:
        vs = c["sem_vector_score"]
        ks = c["kw_text_score"]
        action = "ADD"

        noop_boundary_ceiling = min(1.0, sem_noop + 0.04)
        if vs >= sem_noop:
            is_boundary = vs < noop_boundary_ceiling
            kw_contradicts = ks < kw_update
            if is_boundary and kw_contradicts:
                action = "UPDATE"
            else:
                action = "NOOP"
        elif vs >= sem_update:
            action = "UPDATE"
        elif ks >= kw_noop:
            action = "NOOP"
        elif ks >= kw_update:
            action = "UPDATE"

        predictions.append(action)
    return predictions


def simulate_strategy_a(cases: List[Dict[str, Any]],
                        min_gap: float = 0.03,
                        sem_noop: float = 0.92, sem_update: float = 0.78,
                        kw_noop: float = 0.82, kw_update: float = 0.55) -> List[str]:
    """Margin-based: if top1-top2 gap < min_gap, distrust semantic → keyword fallback."""
    predictions = []
    for c in cases:
        vs = c["sem_vector_score"]
        ks = c["kw_text_score"]
        gap = c.get("top1_top2_gap")

        # If gap is too small and we have multiple candidates, distrust semantic
        trust_semantic = True
        if gap is not None and gap < min_gap and c.get("sem_candidates_count", 0) >= 2:
            trust_semantic = False

        action = "ADD"
        if trust_semantic:
            noop_boundary_ceiling = min(1.0, sem_noop + 0.04)
            if vs >= sem_noop:
                is_boundary = vs < noop_boundary_ceiling
                kw_contradicts = ks < kw_update
                if is_boundary and kw_contradicts:
                    action = "UPDATE"
                else:
                    action = "NOOP"
            elif vs >= sem_update:
                action = "UPDATE"
            elif ks >= kw_noop:
                action = "NOOP"
            elif ks >= kw_update:
                action = "UPDATE"
        else:
            # Semantic distrusted → keyword-only decision
            if ks >= kw_noop:
                action = "NOOP"
            elif ks >= kw_update:
                action = "UPDATE"
            # else: ADD

        predictions.append(action)
    return predictions


def simulate_strategy_b(cases: List[Dict[str, Any]],
                        kw_add_floor: float = 0.30,
                        sem_noop: float = 0.92, sem_update: float = 0.78,
                        kw_noop: float = 0.82, kw_update: float = 0.55) -> List[str]:
    """Expanded cross-check: in UPDATE zone, if keyword is very low → ADD."""
    predictions = []
    for c in cases:
        vs = c["sem_vector_score"]
        ks = c["kw_text_score"]
        action = "ADD"

        noop_boundary_ceiling = min(1.0, sem_noop + 0.04)
        if vs >= sem_noop:
            is_boundary = vs < noop_boundary_ceiling
            kw_contradicts = ks < kw_update
            if is_boundary and kw_contradicts:
                action = "UPDATE"
            else:
                action = "NOOP"
        elif vs >= sem_update:
            # Expanded cross-check: if keyword is very low, downgrade to ADD
            if ks < kw_add_floor:
                action = "ADD"
            else:
                action = "UPDATE"
        elif ks >= kw_noop:
            action = "NOOP"
        elif ks >= kw_update:
            action = "UPDATE"

        predictions.append(action)
    return predictions


def simulate_strategy_ab(cases: List[Dict[str, Any]],
                         min_gap: float = 0.03,
                         kw_add_floor: float = 0.30,
                         sem_noop: float = 0.92, sem_update: float = 0.78,
                         kw_noop: float = 0.82, kw_update: float = 0.55) -> List[str]:
    """Combined: margin-based + expanded cross-check."""
    predictions = []
    for c in cases:
        vs = c["sem_vector_score"]
        ks = c["kw_text_score"]
        gap = c.get("top1_top2_gap")

        trust_semantic = True
        if gap is not None and gap < min_gap and c.get("sem_candidates_count", 0) >= 2:
            trust_semantic = False

        action = "ADD"
        if trust_semantic:
            noop_boundary_ceiling = min(1.0, sem_noop + 0.04)
            if vs >= sem_noop:
                is_boundary = vs < noop_boundary_ceiling
                kw_contradicts = ks < kw_update
                if is_boundary and kw_contradicts:
                    action = "UPDATE"
                else:
                    action = "NOOP"
            elif vs >= sem_update:
                if ks < kw_add_floor:
                    action = "ADD"
                else:
                    action = "UPDATE"
            elif ks >= kw_noop:
                action = "NOOP"
            elif ks >= kw_update:
                action = "UPDATE"
        else:
            if ks >= kw_noop:
                action = "NOOP"
            elif ks >= kw_update:
                action = "UPDATE"

        predictions.append(action)
    return predictions


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def compute_metrics(cases: List[Dict[str, Any]], predictions: List[str]) -> Dict[str, Any]:
    """Compute P/R/F1/EM and confusion matrix."""
    def _is_block(a):
        return a in {"UPDATE", "NOOP", "DELETE"}

    tp = sum(1 for c, p in zip(cases, predictions) if _is_block(c["expected"]) and _is_block(p))
    fp = sum(1 for c, p in zip(cases, predictions) if not _is_block(c["expected"]) and _is_block(p))
    fn = sum(1 for c, p in zip(cases, predictions) if _is_block(c["expected"]) and not _is_block(p))

    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    em = sum(1 for c, p in zip(cases, predictions) if c["expected"] == p) / len(cases) if cases else 0

    # 3x3 confusion matrix
    labels = ["ADD", "UPDATE", "NOOP"]
    cm = {exp: {pred: 0 for pred in labels} for exp in labels}
    for c, p in zip(cases, predictions):
        exp = c["expected"]
        if exp in cm and p in cm[exp]:
            cm[exp][p] += 1

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "exact_match": round(em, 4),
        "tp": tp, "fp": fp, "fn": fn,
        "confusion_matrix": cm,
    }


def distribution_stats(values: List[float], label: str) -> Dict[str, Any]:
    if not values:
        return {"label": label, "n": 0}
    return {
        "label": label,
        "n": len(values),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(statistics.mean(values), 6),
        "median": round(statistics.median(values), 6),
        "p25": round(sorted(values)[len(values) // 4], 6),
        "p75": round(sorted(values)[3 * len(values) // 4], 6),
        "stdev": round(statistics.stdev(values), 6) if len(values) >= 2 else 0.0,
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def format_report(
    scored_cases: List[Dict[str, Any]],
    strategies: Dict[str, Dict[str, Any]],
) -> str:
    lines = [
        "# Write Guard Score Analysis Report",
        "",
    ]

    # Score distributions by expected action
    lines.append("## Phase 1: Score Distributions by Expected Action")
    lines.append("")

    for action in ["ADD", "UPDATE", "NOOP"]:
        subset = [c for c in scored_cases if c["expected"] == action]
        sem_scores = [c["sem_vector_score"] for c in subset if c["sem_vector_score"] > 0]
        kw_scores = [c["kw_text_score"] for c in subset if c["kw_text_score"] > 0]
        gaps = [c["top1_top2_gap"] for c in subset if c["top1_top2_gap"] is not None]

        lines.append(f"### {action} (N={len(subset)})")
        lines.append("")

        sem_stats = distribution_stats(sem_scores, "semantic_vector_score")
        kw_stats = distribution_stats(kw_scores, "keyword_text_score")
        gap_stats = distribution_stats(gaps, "top1_top2_gap")

        lines.append(f"| Metric | Semantic Score | Keyword Score | Top1-Top2 Gap |")
        lines.append(f"|--------|---------------|---------------|---------------|")
        for k in ["n", "min", "max", "mean", "median", "p25", "p75", "stdev"]:
            lines.append(
                f"| {k} | {sem_stats.get(k, '-')} | {kw_stats.get(k, '-')} | {gap_stats.get(k, '-')} |"
            )
        lines.append("")

    # Strategy comparison
    lines.append("## Phase 2: Strategy Comparison")
    lines.append("")
    lines.append("| Strategy | Precision | Recall | F1 | EM | TP | FP | FN |")
    lines.append("|----------|-----------|--------|----|----|----|----|-----|")
    for name, metrics in strategies.items():
        lines.append(
            f"| {name} | {metrics['precision']:.3f} | {metrics['recall']:.3f} "
            f"| {metrics['f1']:.3f} | {metrics['exact_match']:.3f} "
            f"| {metrics['tp']} | {metrics['fp']} | {metrics['fn']} |"
        )
    lines.append("")

    # Confusion matrices
    for name, metrics in strategies.items():
        cm = metrics["confusion_matrix"]
        lines.append(f"### Confusion Matrix: {name}")
        lines.append("")
        lines.append("| Expected \\ Predicted | ADD | UPDATE | NOOP |")
        lines.append("|---------------------|-----|--------|------|")
        for exp in ["ADD", "UPDATE", "NOOP"]:
            lines.append(
                f"| {exp} | {cm[exp]['ADD']} | {cm[exp]['UPDATE']} | {cm[exp]['NOOP']} |"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sweep over strategy parameters
# ---------------------------------------------------------------------------

def sweep_strategy_a(cases, gap_values=None):
    if gap_values is None:
        gap_values = [0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05, 0.06, 0.08]
    results = []
    for g in gap_values:
        preds = simulate_strategy_a(cases, min_gap=g)
        m = compute_metrics(cases, preds)
        m["min_gap"] = g
        results.append(m)
    return results


def sweep_strategy_b(cases, floor_values=None):
    if floor_values is None:
        floor_values = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50]
    results = []
    for f in floor_values:
        preds = simulate_strategy_b(cases, kw_add_floor=f)
        m = compute_metrics(cases, preds)
        m["kw_add_floor"] = f
        results.append(m)
    return results


def sweep_strategy_ab(cases, gap_values=None, floor_values=None):
    if gap_values is None:
        gap_values = [0.005, 0.01, 0.015, 0.02, 0.03, 0.04, 0.05]
    if floor_values is None:
        floor_values = [0.10, 0.20, 0.30, 0.40, 0.50]
    results = []
    for g in gap_values:
        for f in floor_values:
            preds = simulate_strategy_ab(cases, min_gap=g, kw_add_floor=f)
            m = compute_metrics(cases, preds)
            m["min_gap"] = g
            m["kw_add_floor"] = f
            results.append(m)
    return results


async def main():
    gold = _load_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl")
    assert len(gold) >= 100, f"Gold set too small: {len(gold)}"

    print(f"[analysis] Phase 1: Collecting raw scores ({len(gold)} cases)...",
          file=sys.stderr, flush=True)
    scored_cases = await collect_raw_scores(gold)

    # Score distribution summary
    sem_scores = [c["sem_vector_score"] for c in scored_cases if c["sem_vector_score"] > 0]
    kw_scores = [c["kw_text_score"] for c in scored_cases if c["kw_text_score"] > 0]
    gaps = [c["top1_top2_gap"] for c in scored_cases if c["top1_top2_gap"] is not None]

    print(f"[analysis] Semantic scores: n={len(sem_scores)}, "
          f"min={min(sem_scores):.4f}, max={max(sem_scores):.4f}, "
          f"mean={sum(sem_scores)/len(sem_scores):.4f}" if sem_scores else "[analysis] No semantic scores",
          file=sys.stderr, flush=True)
    print(f"[analysis] Keyword scores: n={len(kw_scores)}, "
          f"min={min(kw_scores):.4f}, max={max(kw_scores):.4f}, "
          f"mean={sum(kw_scores)/len(kw_scores):.4f}" if kw_scores else "[analysis] No keyword scores",
          file=sys.stderr, flush=True)
    print(f"[analysis] Top1-Top2 gaps: n={len(gaps)}, "
          f"min={min(gaps):.4f}, max={max(gaps):.4f}, "
          f"mean={sum(gaps)/len(gaps):.4f}" if gaps else "[analysis] No gap data",
          file=sys.stderr, flush=True)

    print(f"\n[analysis] Phase 2: Running strategy simulations...",
          file=sys.stderr, flush=True)

    # Baseline
    baseline_preds = simulate_baseline(scored_cases)
    baseline_metrics = compute_metrics(scored_cases, baseline_preds)

    # Strategy sweeps
    a_sweep = sweep_strategy_a(scored_cases)
    b_sweep = sweep_strategy_b(scored_cases)
    ab_sweep = sweep_strategy_ab(scored_cases)

    # Find best for each strategy
    best_a = max(a_sweep, key=lambda x: (x["exact_match"], x["f1"]))
    best_b = max(b_sweep, key=lambda x: (x["exact_match"], x["f1"]))
    best_ab = max(ab_sweep, key=lambda x: (x["exact_match"], x["f1"]))

    # Run best configs to get detailed predictions
    strategies = {
        "Baseline (0.92/0.78)": baseline_metrics,
        f"A: margin gap<{best_a['min_gap']}": compute_metrics(
            scored_cases, simulate_strategy_a(scored_cases, min_gap=best_a["min_gap"])),
        f"B: kw_floor>{best_b['kw_add_floor']}": compute_metrics(
            scored_cases, simulate_strategy_b(scored_cases, kw_add_floor=best_b["kw_add_floor"])),
        f"A+B: gap<{best_ab['min_gap']},kw>{best_ab['kw_add_floor']}": compute_metrics(
            scored_cases, simulate_strategy_ab(
                scored_cases, min_gap=best_ab["min_gap"], kw_add_floor=best_ab["kw_add_floor"])),
    }

    # Generate report
    report_md = format_report(scored_cases, strategies)
    REPORT_MD.write_text(report_md, encoding="utf-8")
    print(f"\n[analysis] Report saved to {REPORT_MD}", file=sys.stderr, flush=True)

    # Save full data
    report_data = {
        "scored_cases": scored_cases,
        "baseline": baseline_metrics,
        "sweep_a": a_sweep,
        "sweep_b": b_sweep,
        "sweep_ab": ab_sweep,
        "best_a": best_a,
        "best_b": best_b,
        "best_ab": best_ab,
        "strategies": {k: v for k, v in strategies.items()},
    }
    REPORT_PATH.write_text(json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[analysis] Data saved to {REPORT_PATH}", file=sys.stderr, flush=True)

    # Print summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name, m in strategies.items():
        print(f"  {name}: P={m['precision']:.3f} R={m['recall']:.3f} F1={m['f1']:.3f} EM={m['exact_match']:.3f}")
    print()
    print(f"  Best A config: min_gap={best_a['min_gap']}")
    print(f"  Best B config: kw_add_floor={best_b['kw_add_floor']}")
    print(f"  Best A+B config: min_gap={best_ab['min_gap']}, kw_add_floor={best_ab['kw_add_floor']}")

    # Print all A sweep results
    print("\n--- Strategy A sweep (margin-based) ---")
    print(f"{'gap':>6} | {'P':>6} | {'R':>6} | {'F1':>6} | {'EM':>6}")
    for r in a_sweep:
        print(f"{r['min_gap']:6.3f} | {r['precision']:6.3f} | {r['recall']:6.3f} | {r['f1']:6.3f} | {r['exact_match']:6.3f}")

    print("\n--- Strategy B sweep (expanded cross-check) ---")
    print(f"{'floor':>6} | {'P':>6} | {'R':>6} | {'F1':>6} | {'EM':>6}")
    for r in b_sweep:
        print(f"{r['kw_add_floor']:6.3f} | {r['precision']:6.3f} | {r['recall']:6.3f} | {r['f1']:6.3f} | {r['exact_match']:6.3f}")

    print("\n--- Top 10 A+B combined sweep ---")
    print(f"{'gap':>6} | {'floor':>6} | {'P':>6} | {'R':>6} | {'F1':>6} | {'EM':>6}")
    top_ab = sorted(ab_sweep, key=lambda x: (-x["exact_match"], -x["f1"]))[:10]
    for r in top_ab:
        print(f"{r['min_gap']:6.3f} | {r['kw_add_floor']:6.3f} | {r['precision']:6.3f} | {r['recall']:6.3f} | {r['f1']:6.3f} | {r['exact_match']:6.3f}")


if __name__ == "__main__":
    asyncio.run(main())
