"""Write Guard threshold sweep for C/D profiles.

Phase 1: Seed DB, run search_advanced ONCE per case, collect raw scores.
Phase 2: Offline simulate threshold combos, output P/R/F1/EM grid.

This avoids 63× redundant API calls by separating score collection from
threshold simulation.

Usage:
    RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_EMBEDDING_API_KEY=... \
    RETRIEVAL_EMBEDDING_MODEL=... RETRIEVAL_EMBEDDING_DIM=1024 \
    RETRIEVAL_RERANKER_API_BASE=... RETRIEVAL_RERANKER_API_KEY=... \
    RETRIEVAL_RERANKER_MODEL=... \
    backend/.venv/bin/python backend/tests/benchmark/run_write_guard_threshold_sweep.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BENCHMARK_DIR.parent / "fixtures"
BACKEND_ROOT = BENCHMARK_DIR.parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db.sqlite_client import SQLiteClient

REPORT_PATH = BENCHMARK_DIR / "write_guard_threshold_sweep.json"
REPORT_MD = BENCHMARK_DIR / "write_guard_threshold_sweep.md"


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
    """Seed DB, run search_advanced for each case, return raw scores."""
    # Set C-off profile env
    os.environ["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
    os.environ["RETRIEVAL_RERANKER_ENABLED"] = "true"
    os.environ["RETRIEVAL_RERANKER_WEIGHT"] = "0.30"
    os.environ["INTENT_LLM_ENABLED"] = "false"
    os.environ["WRITE_GUARD_LLM_ENABLED"] = "false"
    os.environ["COMPACT_GIST_LLM_ENABLED"] = "false"

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="sweep_")
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

    print(f"[sweep] Seeding {len(unique_existing)} memories...", file=sys.stderr, flush=True)
    await _seed_memories(client, unique_existing)

    try:
        await client.rebuild_index()
    except Exception:
        pass

    print(f"[sweep] Collecting scores for {len(gold)} cases...", file=sys.stderr, flush=True)
    scored_cases = []

    for i, row in enumerate(gold):
        content = str(row["content"])
        expected = str(row["expected_action"]).upper()
        domain = "core"
        filters = {"domain": domain}

        # Collect semantic search results
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

        # Extract top scores
        sem_results = semantic_payload.get("results", [])
        kw_results = keyword_payload.get("results", [])

        sem_top_vector = 0.0
        sem_top_id = None
        for r in sem_results:
            vs = float(r.get("scores", {}).get("vector", 0) or 0)
            if vs > sem_top_vector:
                sem_top_vector = vs
                sem_top_id = r.get("memory_id")

        kw_top_text = 0.0
        kw_top_id = None
        for r in kw_results:
            ts = float(r.get("scores", {}).get("text", 0) or 0)
            if ts > kw_top_text:
                kw_top_text = ts
                kw_top_id = r.get("memory_id")

        scored_cases.append({
            "id": row["id"],
            "expected": expected,
            "sem_vector_score": round(sem_top_vector, 6),
            "sem_memory_id": sem_top_id,
            "kw_text_score": round(kw_top_text, 6),
            "kw_memory_id": kw_top_id,
            "sem_degrade": semantic_payload.get("degrade_reasons", []),
            "kw_degrade": keyword_payload.get("degrade_reasons", []),
        })

        if (i + 1) % 20 == 0:
            print(f"[sweep] {i+1}/{len(gold)} cases scored", file=sys.stderr, flush=True)

    await client.close()
    try:
        Path(db_path).unlink(missing_ok=True)
    except Exception:
        pass

    return scored_cases


# ---------------------------------------------------------------------------
# Phase 2: Simulate thresholds offline
# ---------------------------------------------------------------------------

def simulate_threshold(
    cases: List[Dict[str, Any]],
    sem_noop: float,
    sem_update: float,
    kw_noop: float = 0.82,
    kw_update: float = 0.55,
) -> Dict[str, Any]:
    """Replay write_guard threshold logic with given parameters."""
    predictions = []
    for c in cases:
        vs = c["sem_vector_score"]
        ks = c["kw_text_score"]

        # Simulate write_guard threshold logic (simplified, matches sqlite_client.py)
        action = "ADD"  # default

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
        # else: remains ADD

        predictions.append(action)

    # Compute metrics
    def _is_block(a):
        return a in {"UPDATE", "NOOP", "DELETE"}

    tp = sum(1 for c, p in zip(cases, predictions) if _is_block(c["expected"]) and _is_block(p))
    fp = sum(1 for c, p in zip(cases, predictions) if not _is_block(c["expected"]) and _is_block(p))
    fn = sum(1 for c, p in zip(cases, predictions) if _is_block(c["expected"]) and not _is_block(p))

    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    em = sum(1 for c, p in zip(cases, predictions) if c["expected"] == p) / len(cases) if cases else 0

    return {
        "sem_noop": sem_noop,
        "sem_update": sem_update,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "exact_match": round(em, 4),
        "tp": tp, "fp": fp, "fn": fn,
    }


def run_sweep(cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run threshold sweep across NOOP × UPDATE grid."""
    noop_range = [round(0.88 + i * 0.01, 2) for i in range(7)]   # 0.88..0.94
    update_range = [round(0.72 + i * 0.01, 2) for i in range(9)]  # 0.72..0.80

    results = []
    for noop in noop_range:
        for update in update_range:
            if update > noop:
                continue  # update threshold must be <= noop threshold
            r = simulate_threshold(cases, sem_noop=noop, sem_update=update)
            results.append(r)

    return results


def format_report(sweep: List[Dict[str, Any]], baseline_em: float, baseline_f1: float) -> str:
    """Generate markdown report."""
    lines = [
        "# Write Guard Threshold Sweep Report",
        "",
        f"> Baseline (original 0.92/0.78): EM={baseline_em:.3f}, F1={baseline_f1:.3f}",
        f"> Non-regression gate: EM >= {baseline_em:.3f} AND F1 >= {baseline_f1:.3f}",
        "",
        "## Full Grid",
        "",
        "| NOOP | UPDATE | Precision | Recall | F1 | EM | Gate |",
        "|-----:|-------:|----------:|-------:|---:|---:|------|",
    ]

    best = None
    for r in sorted(sweep, key=lambda x: (-x["f1"], -x["exact_match"])):
        passes = r["exact_match"] >= baseline_em and r["f1"] >= baseline_f1
        gate = "PASS" if passes else ""
        lines.append(
            f"| {r['sem_noop']:.2f} | {r['sem_update']:.2f} "
            f"| {r['precision']:.3f} | {r['recall']:.3f} "
            f"| {r['f1']:.3f} | {r['exact_match']:.3f} | {gate} |"
        )
        if passes and (best is None or r["f1"] > best["f1"]):
            best = r

    lines.extend(["", "## Recommendation", ""])
    if best:
        lines.append(
            f"**Recommended**: NOOP={best['sem_noop']:.2f}, UPDATE={best['sem_update']:.2f} "
            f"→ P={best['precision']:.3f}, R={best['recall']:.3f}, "
            f"F1={best['f1']:.3f}, EM={best['exact_match']:.3f} (PASS)"
        )
    else:
        lines.append("**No non-regressing threshold found.** Recommend reverting C/D threshold change.")

    lines.append("")
    return "\n".join(lines)


async def main():
    gold = _load_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl")
    assert len(gold) >= 100, f"Gold set too small: {len(gold)}"

    print(f"[sweep] Phase 1: Collecting raw scores ({len(gold)} cases)...",
          file=sys.stderr, flush=True)
    scored_cases = await collect_raw_scores(gold)

    # Score distribution summary
    sem_scores = [c["sem_vector_score"] for c in scored_cases if c["sem_vector_score"] > 0]
    kw_scores = [c["kw_text_score"] for c in scored_cases if c["kw_text_score"] > 0]
    print(f"[sweep] Semantic scores: n={len(sem_scores)}, "
          f"min={min(sem_scores):.4f}, max={max(sem_scores):.4f}, "
          f"mean={sum(sem_scores)/len(sem_scores):.4f}" if sem_scores else "[sweep] No semantic scores",
          file=sys.stderr, flush=True)
    print(f"[sweep] Keyword scores: n={len(kw_scores)}, "
          f"min={min(kw_scores):.4f}, max={max(kw_scores):.4f}, "
          f"mean={sum(kw_scores)/len(kw_scores):.4f}" if kw_scores else "[sweep] No keyword scores",
          file=sys.stderr, flush=True)

    print(f"\n[sweep] Phase 2: Threshold sweep...", file=sys.stderr, flush=True)
    sweep_results = run_sweep(scored_cases)

    # Baseline: original thresholds (0.92/0.78)
    baseline = simulate_threshold(scored_cases, sem_noop=0.92, sem_update=0.78)
    baseline_em = baseline["exact_match"]
    baseline_f1 = baseline["f1"]
    print(f"[sweep] Baseline (0.92/0.78): P={baseline['precision']:.3f} R={baseline['recall']:.3f} "
          f"F1={baseline_f1:.3f} EM={baseline_em:.3f}", file=sys.stderr, flush=True)

    # Find best non-regressing point
    passing = [r for r in sweep_results if r["exact_match"] >= baseline_em and r["f1"] >= baseline_f1]
    if passing:
        best = max(passing, key=lambda x: x["f1"])
        print(f"[sweep] Best PASS: NOOP={best['sem_noop']:.2f} UPDATE={best['sem_update']:.2f} "
              f"P={best['precision']:.3f} R={best['recall']:.3f} "
              f"F1={best['f1']:.3f} EM={best['exact_match']:.3f}",
              file=sys.stderr, flush=True)
    else:
        print("[sweep] NO non-regressing threshold found.", file=sys.stderr, flush=True)

    # Save reports
    best_point = max(passing, key=lambda x: x["f1"]) if passing else None
    report_data = {
        "baseline": baseline,
        "scored_cases_summary": {
            "total": len(scored_cases),
            "sem_scores_nonzero": len(sem_scores),
            "kw_scores_nonzero": len(kw_scores),
        },
        "sweep": sweep_results,
        "recommendation": best_point,
    }
    REPORT_PATH.write_text(json.dumps(report_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    REPORT_MD.write_text(format_report(sweep_results, baseline_em, baseline_f1), encoding="utf-8")

    print(f"\n[sweep] Reports written to:", file=sys.stderr, flush=True)
    print(f"  {REPORT_PATH}", file=sys.stderr, flush=True)
    print(f"  {REPORT_MD}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
