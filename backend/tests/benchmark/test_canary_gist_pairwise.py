"""Pairwise B-off vs B-on gist canary: blinded A/B judge + factual coverage.

For each source_content, generates BOTH:
  - A = B-off gist (extractive_bullets, no LLM)
  - B = B-on gist (LLM gist via generate_compact_gist)

Then evaluates:
  1. Blinded pairwise A/B judge (randomized order, LLM judge)
  2. Factual coverage check (fact extraction + coverage per gist)
  3. Auxiliary: ROUGE-L, BERTScore, reference-free rubric (diagnostic only)

Requires LLM to be available (skips otherwise).

    WRITE_GUARD_LLM_API_BASE=... WRITE_GUARD_LLM_API_KEY=... WRITE_GUARD_LLM_MODEL=... \
    backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_canary_gist_pairwise.py
"""
from __future__ import annotations

import json
import random
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

import pytest

BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BENCHMARK_DIR.parent / "fixtures"
BACKEND_ROOT = BENCHMARK_DIR.parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from db.sqlite_client import SQLiteClient
from helpers.health_checks import run_all_health_checks
from helpers.real_retrieval_harness import (
    MATRIX_CELLS,
    apply_cell_env,
)

random.seed(2026_04_05)

CANARY_SIZE = 36  # same as bucketed canary

B_ON = [c for c in MATRIX_CELLS if c.cell_id == "B-on"][0]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


def _tokenize(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower())


def _lcs_length(x: List[str], y: List[str]) -> int:
    m, n = len(x), len(y)
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if x[i - 1] == y[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(curr[j - 1], prev[j])
        prev = curr
    return prev[n]


def _rouge_l_f1(reference: str, candidate: str) -> float:
    ref_tok = _tokenize(reference)
    cand_tok = _tokenize(candidate)
    if not ref_tok or not cand_tok:
        return 0.0
    lcs = _lcs_length(ref_tok, cand_tok)
    p = lcs / len(cand_tok)
    r = lcs / len(ref_tok)
    if (p + r) == 0:
        return 0.0
    return (2 * p * r) / (p + r)


def _extractive_bullets(text: str) -> str:
    lines = text.strip().split("\n")
    bullets = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:][:90])
    if bullets:
        return "; ".join(bullets[:3])
    sentences = re.split(r'[.。!！?？;；]', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    return "; ".join(sentences[:2])[:280]


def _make_extractive_reference(source: str) -> str:
    clean = source.replace("\n- ", ". ").replace("\n", ". ")
    sentences = [s.strip() for s in re.split(r'[.。]', clean) if s.strip()]
    if len(sentences) >= 3:
        picked = [sentences[0], sentences[2]]
    else:
        picked = sentences[:2]
    return "; ".join(picked)[:150]


def _stratified_sample(rows: List[Dict], key: str, n: int) -> List[Dict]:
    buckets: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        buckets[r.get(key, "unknown")].append(r)
    per_bucket = max(1, n // len(buckets))
    result = []
    for vals in buckets.values():
        result.extend(random.sample(vals, min(per_bucket, len(vals))))
    remaining = [r for r in rows if r not in result]
    while len(result) < n and remaining:
        result.append(remaining.pop())
    return result[:n]


def _count_by(rows, key):
    counts = {}
    for r in rows:
        v = r.get(key, "?") if isinstance(r, dict) else getattr(r, key, "?")
        counts[v] = counts.get(v, 0) + 1
    return counts


def _slice_pairwise(results, cases, key, value):
    """Extract pairwise results for a specific slice."""
    return [r for r, c in zip(results, cases) if c.get(key) == value]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_canary_gist_pairwise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blinded pairwise B-off vs B-on + factual coverage on bucketed gist canary."""
    _dbg = lambda msg: print(f"[pairwise] {msg}", file=sys.stderr, flush=True)

    health = await run_all_health_checks()
    if health["llm"]["status"] != "ok":
        pytest.skip(f"LLM unavailable: {health['llm'].get('reason', 'unknown')}")

    all_gist = _load_jsonl(FIXTURES_DIR / "gist_product_gold_set.jsonl")
    subset = _stratified_sample(all_gist, "length_bucket", CANARY_SIZE)

    _dbg(f"Subset: {len(subset)} cases")
    _dbg(f"  length: {_count_by(subset, 'length_bucket')}")
    _dbg(f"  format: {_count_by(subset, 'format_bucket')}")
    _dbg(f"  lang:   {_count_by(subset, 'lang')}")

    apply_cell_env(monkeypatch, B_ON, health)

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    await client.init_db()

    cases: List[Dict[str, Any]] = []
    rng = random.Random(42)

    try:
        for row in subset:
            source = str(row["source_content"])
            abstractive_ref = str(row["reference_gist"])
            extractive_ref = _make_extractive_reference(source)

            # --- A = B-off (extractive, no LLM) ---
            gist_a = _extractive_bullets(source)

            # --- B = B-on (LLM gist) ---
            gist_b = ""
            method_b = "extractive_bullets"
            degrade_reasons: List[str] = []
            try:
                result = await client.generate_compact_gist(
                    summary=source,
                    max_points=3,
                    max_chars=280,
                    degrade_reasons=degrade_reasons,
                )
                if result and result.get("gist_text"):
                    gist_b = str(result["gist_text"])
                    method_b = result.get("gist_method", "llm_gist")
            except Exception:
                degrade_reasons.append("generate_compact_gist raised exception")

            if not gist_b:
                gist_b = _extractive_bullets(source)
                method_b = "extractive_bullets_fallback"

            # ROUGE-L (auxiliary)
            rouge_a = max(_rouge_l_f1(abstractive_ref, gist_a),
                          _rouge_l_f1(extractive_ref, gist_a))
            rouge_b = max(_rouge_l_f1(abstractive_ref, gist_b),
                          _rouge_l_f1(extractive_ref, gist_b))

            cases.append({
                "id": row["id"],
                "source_content": source,
                "gist_a": gist_a,       # B-off
                "gist_b": gist_b,       # B-on
                "method_b": method_b,
                "rouge_a": round(rouge_a, 4),
                "rouge_b": round(rouge_b, 4),
                "lang": row.get("lang", ""),
                "length_bucket": row.get("length_bucket", "medium"),
                "format_bucket": row.get("format_bucket", "prose"),
            })
    finally:
        await client.close()

    llm_count = sum(1 for c in cases if c["method_b"] == "llm_gist")
    fallback_count = sum(1 for c in cases if c["method_b"] != "llm_gist")

    _dbg(f"\n=== Auditability ===")
    _dbg(f"  B-on llm_count: {llm_count}")
    _dbg(f"  B-on fallback_count: {fallback_count}")

    # --- Auxiliary: ROUGE-L ---
    rouge_a_mean = sum(c["rouge_a"] for c in cases) / len(cases) if cases else 0
    rouge_b_mean = sum(c["rouge_b"] for c in cases) / len(cases) if cases else 0
    _dbg(f"\n=== Auxiliary: ROUGE-L ===")
    _dbg(f"  B-off (A): {rouge_a_mean:.3f}")
    _dbg(f"  B-on  (B): {rouge_b_mean:.3f}")

    # --- Auxiliary: BERTScore ---
    from helpers.semantic_metrics import compute_bertscore_batch

    _dbg(f"\n=== Auxiliary: BERTScore ===")
    for lang in ("en", "zh"):
        lang_cases = [c for c in cases if c["lang"] == lang]
        if not lang_cases:
            continue
        refs = [c["source_content"] for c in lang_cases]
        cands_a = [c["gist_a"] for c in lang_cases]
        cands_b = [c["gist_b"] for c in lang_cases]
        bs_a = compute_bertscore_batch(refs, cands_a, lang=lang)
        bs_b = compute_bertscore_batch(refs, cands_b, lang=lang)
        if bs_a is not None and bs_b is not None:
            mean_a = sum(bs_a) / len(bs_a)
            mean_b = sum(bs_b) / len(bs_b)
            _dbg(f"  {lang}: B-off={mean_a:.3f}, B-on={mean_b:.3f}")
            for i, c in enumerate(lang_cases):
                c["bertscore_a"] = bs_a[i]
                c["bertscore_b"] = bs_b[i]
        else:
            _dbg(f"  {lang}: skipped (unavailable)")

    # === PRIMARY: Pairwise A/B Judge ===
    from helpers.pairwise_judge import DIMENSIONS, aggregate_pairwise, judge_pair

    _dbg(f"\n=== Pairwise A/B Judge (A=B-off, B=B-on) ===")
    _dbg(f"  Running blinded pairwise comparison...")

    pairwise_results = []
    for c in cases:
        result = judge_pair(c["source_content"], c["gist_a"], c["gist_b"], rng=rng)
        pairwise_results.append(result)

    pw_agg = aggregate_pairwise(pairwise_results)
    _dbg(f"  judged: {pw_agg['judged_count']}/{len(cases)}")

    if pw_agg["judged_count"] > 0:
        _dbg(f"\n--- Overall pairwise ---")
        for dim in DIMENSIONS:
            d = pw_agg[dim]
            _dbg(f"  {dim}: B-off wins={d['a_win']}({d['a_win_rate']:.0%}), "
                 f"B-on wins={d['b_win']}({d['b_win_rate']:.0%}), "
                 f"tie={d['tie']}({d['tie_rate']:.0%})")

        # Per-slice pairwise
        for slice_key, slice_vals in [
            ("length_bucket", ["short", "medium", "long"]),
            ("format_bucket", ["bullet", "prose", "mixed"]),
            ("lang", ["en", "zh"]),
        ]:
            _dbg(f"\n--- Pairwise by {slice_key} ---")
            for sv in slice_vals:
                slice_pw = _slice_pairwise(pairwise_results, cases, slice_key, sv)
                if not slice_pw:
                    continue
                valid_pw = [r for r in slice_pw if r is not None]
                if not valid_pw:
                    continue
                b_wins = sum(1 for r in valid_pw if r["overall"] == "B_WIN")
                a_wins = sum(1 for r in valid_pw if r["overall"] == "A_WIN")
                ties = sum(1 for r in valid_pw if r["overall"] == "TIE")
                n = len(valid_pw)
                _dbg(f"  {sv}: B-on wins={b_wins}/{n}({b_wins/n:.0%}), "
                     f"B-off wins={a_wins}/{n}({a_wins/n:.0%}), "
                     f"tie={ties}/{n}")

        # Hard slices check
        _dbg(f"\n--- Hard slices ---")
        for lb, fb in [("short", "bullet"), ("long", "bullet")]:
            hard = [
                r for r, c in zip(pairwise_results, cases)
                if c["length_bucket"] == lb and c["format_bucket"] == fb and r is not None
            ]
            if hard:
                b_wins = sum(1 for r in hard if r["overall"] == "B_WIN")
                a_wins = sum(1 for r in hard if r["overall"] == "A_WIN")
                _dbg(f"  {lb}+{fb}: B-on={b_wins}/{len(hard)}, B-off={a_wins}/{len(hard)}")
            else:
                _dbg(f"  {lb}+{fb}: no cases in sample")

    # === PRIMARY: Factual Coverage ===
    from helpers.factual_coverage import aggregate_coverage, evaluate_pair

    _dbg(f"\n=== Factual Coverage (A=B-off, B=B-on) ===")
    _dbg(f"  Extracting facts and checking coverage...")

    coverage_results = []
    for c in cases:
        result = evaluate_pair(c["source_content"], c["gist_a"], c["gist_b"])
        coverage_results.append(result)

    cov_agg = aggregate_coverage(coverage_results)
    _dbg(f"  evaluated: {cov_agg['evaluated_count']}/{len(cases)}")

    if cov_agg["evaluated_count"] > 0:
        _dbg(f"  total facts: {cov_agg['total_facts']}")
        _dbg(f"\n  B-off (A):")
        _dbg(f"    coverage rate:   {cov_agg['a_coverage_rate']:.1%}")
        _dbg(f"    missed rate:     {cov_agg['a_missed_rate']:.1%}")
        _dbg(f"    fabricated:      {cov_agg['a_fabricated_total']} total ({cov_agg['a_fabricated_rate']:.2f}/case)")
        _dbg(f"\n  B-on (B):")
        _dbg(f"    coverage rate:   {cov_agg['b_coverage_rate']:.1%}")
        _dbg(f"    missed rate:     {cov_agg['b_missed_rate']:.1%}")
        _dbg(f"    fabricated:      {cov_agg['b_fabricated_total']} total ({cov_agg['b_fabricated_rate']:.2f}/case)")

    # === Gates ===
    # Only apply if pairwise judge succeeded
    if pw_agg["judged_count"] > 0:
        bon_overall_wr = pw_agg["overall"]["b_win_rate"]
        _dbg(f"\n=== Superiority Gate ===")
        _dbg(f"  B-on overall win rate: {bon_overall_wr:.1%} (gate: >55%)")

        # Check faithfulness: B-on must not be worse
        faith = pw_agg["faithfulness"]
        faith_ok = faith["b_win"] >= faith["a_win"]
        _dbg(f"  faithfulness: B-on wins={faith['b_win']}, B-off wins={faith['a_win']} "
             f"({'OK' if faith_ok else 'FAIL: B-on weaker'})")

        # Hard slices: short+bullet and long+bullet must not collapse
        hard_ok = True
        for lb, fb in [("short", "bullet"), ("long", "bullet")]:
            hard = [
                r for r, c in zip(pairwise_results, cases)
                if c["length_bucket"] == lb and c["format_bucket"] == fb and r is not None
            ]
            if hard:
                b_wins = sum(1 for r in hard if r["overall"] == "B_WIN")
                a_wins = sum(1 for r in hard if r["overall"] == "A_WIN")
                if a_wins > b_wins and b_wins == 0:
                    _dbg(f"  hard slice {lb}+{fb}: COLLAPSED (B-on=0 wins)")
                    hard_ok = False

        gate_passed = bon_overall_wr > 0.55 and faith_ok and hard_ok
        _dbg(f"\n  GATE: {'PASS' if gate_passed else 'FAIL'}")
        if not gate_passed:
            _dbg(f"  (Gate failure is informational; test still passes for data collection)")

    # Test always passes — gates are informational for this canary
    assert len(cases) > 0
    assert llm_count > 0, (
        f"LLM health=ok but llm_count=0; pairwise comparison invalid"
    )
