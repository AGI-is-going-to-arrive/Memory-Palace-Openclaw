"""Hybrid gist canary: slice-aware routing (short→extractive, medium/long→LLM).

Generates three gists per source:
  - A = B-off (always extractive)
  - B = B-on (always LLM attempt)
  - H = Hybrid (routed by slice policy)

Evaluates:
  1. Pairwise H vs A (hybrid vs B-off): does hybrid beat pure extractive?
  2. Factual coverage H vs A: does routing fix the fabrication gap?
  3. Comparison of H metrics vs B-on from previous canary

    WRITE_GUARD_LLM_API_BASE=... WRITE_GUARD_LLM_API_KEY=... WRITE_GUARD_LLM_MODEL=... \
    backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_canary_gist_hybrid.py
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
from helpers.gist_routing_policy import describe_policy, route_gist
from helpers.health_checks import run_all_health_checks
from helpers.real_retrieval_harness import (
    MATRIX_CELLS,
    apply_cell_env,
)

random.seed(2026_04_05)

CANARY_SIZE = 36

B_ON = [c for c in MATRIX_CELLS if c.cell_id == "B-on"][0]


# ---------------------------------------------------------------------------
# Shared helpers (same as pairwise canary)
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
        v = r.get(key, "?")
        counts[v] = counts.get(v, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_canary_gist_hybrid(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hybrid gist canary: slice-aware routing vs pure B-off."""
    _dbg = lambda msg: print(f"[hybrid] {msg}", file=sys.stderr, flush=True)

    health = await run_all_health_checks()
    if health["llm"]["status"] != "ok":
        pytest.skip(f"LLM unavailable: {health['llm'].get('reason', 'unknown')}")

    all_gist = _load_jsonl(FIXTURES_DIR / "gist_product_gold_set.jsonl")
    subset = _stratified_sample(all_gist, "length_bucket", CANARY_SIZE)

    _dbg(f"Subset: {len(subset)} cases")
    _dbg(f"  length: {_count_by(subset, 'length_bucket')}")
    _dbg(f"  format: {_count_by(subset, 'format_bucket')}")
    _dbg(f"  lang:   {_count_by(subset, 'lang')}")
    _dbg(f"  policy: {describe_policy()}")

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
            lb = row.get("length_bucket", "medium")
            fb = row.get("format_bucket", "prose")
            lang = row.get("lang", "en")

            # --- A = B-off (always extractive) ---
            gist_a = _extractive_bullets(source)

            # --- H = Hybrid (routed by policy) ---
            routed_method = route_gist(lb, fb, lang)

            if routed_method == "llm_gist":
                gist_h = ""
                method_h = "extractive_bullets"
                degrade: List[str] = []
                try:
                    result = await client.generate_compact_gist(
                        summary=source,
                        max_points=3,
                        max_chars=280,
                        degrade_reasons=degrade,
                    )
                    if result and result.get("gist_text"):
                        gist_h = str(result["gist_text"])
                        method_h = "llm_gist"
                except Exception:
                    degrade.append("exception")

                if not gist_h:
                    gist_h = _extractive_bullets(source)
                    method_h = "extractive_fallback"
            else:
                gist_h = _extractive_bullets(source)
                method_h = "extractive_by_policy"

            # ROUGE-L
            rouge_a = max(_rouge_l_f1(abstractive_ref, gist_a),
                          _rouge_l_f1(extractive_ref, gist_a))
            rouge_h = max(_rouge_l_f1(abstractive_ref, gist_h),
                          _rouge_l_f1(extractive_ref, gist_h))

            cases.append({
                "id": row["id"],
                "source_content": source,
                "gist_a": gist_a,
                "gist_h": gist_h,
                "routed_method": routed_method,
                "actual_method": method_h,
                "rouge_a": round(rouge_a, 4),
                "rouge_h": round(rouge_h, 4),
                "lang": lang,
                "length_bucket": lb,
                "format_bucket": fb,
            })
    finally:
        await client.close()

    # --- Routing stats ---
    routed_ext = sum(1 for c in cases if c["routed_method"] == "extractive")
    routed_llm = sum(1 for c in cases if c["routed_method"] == "llm_gist")
    actual_llm = sum(1 for c in cases if c["actual_method"] == "llm_gist")
    actual_ext_policy = sum(1 for c in cases if c["actual_method"] == "extractive_by_policy")
    actual_fb = sum(1 for c in cases if c["actual_method"] == "extractive_fallback")

    _dbg(f"\n=== Routing Stats ===")
    _dbg(f"  routed to extractive: {routed_ext}")
    _dbg(f"  routed to llm_gist:   {routed_llm}")
    _dbg(f"  actual llm_gist:      {actual_llm}")
    _dbg(f"  actual ext (policy):  {actual_ext_policy}")
    _dbg(f"  actual ext (fallback):{actual_fb}")

    # --- Auxiliary: ROUGE-L ---
    rouge_a_mean = sum(c["rouge_a"] for c in cases) / len(cases)
    rouge_h_mean = sum(c["rouge_h"] for c in cases) / len(cases)
    _dbg(f"\n=== Auxiliary: ROUGE-L ===")
    _dbg(f"  B-off (A):   {rouge_a_mean:.3f}")
    _dbg(f"  Hybrid (H):  {rouge_h_mean:.3f}")

    for lb in ("short", "medium", "long"):
        lb_cases = [c for c in cases if c["length_bucket"] == lb]
        if lb_cases:
            ra = sum(c["rouge_a"] for c in lb_cases) / len(lb_cases)
            rh = sum(c["rouge_h"] for c in lb_cases) / len(lb_cases)
            _dbg(f"  {lb}: A={ra:.3f}, H={rh:.3f}")

    # --- Auxiliary: BERTScore ---
    from helpers.semantic_metrics import compute_bertscore_batch

    _dbg(f"\n=== Auxiliary: BERTScore ===")
    for lang in ("en", "zh"):
        lang_cases = [c for c in cases if c["lang"] == lang]
        if not lang_cases:
            continue
        refs = [c["source_content"] for c in lang_cases]
        cands_a = [c["gist_a"] for c in lang_cases]
        cands_h = [c["gist_h"] for c in lang_cases]
        bs_a = compute_bertscore_batch(refs, cands_a, lang=lang)
        bs_h = compute_bertscore_batch(refs, cands_h, lang=lang)
        if bs_a is not None and bs_h is not None:
            _dbg(f"  {lang}: B-off={sum(bs_a)/len(bs_a):.3f}, Hybrid={sum(bs_h)/len(bs_h):.3f}")

    # === PRIMARY: Pairwise H vs A ===
    from helpers.pairwise_judge import DIMENSIONS, aggregate_pairwise, judge_pair

    _dbg(f"\n=== Pairwise Judge: Hybrid (H) vs B-off (A) ===")

    pairwise_results = []
    for c in cases:
        result = judge_pair(c["source_content"], c["gist_a"], c["gist_h"], rng=rng)
        pairwise_results.append(result)

    pw_agg = aggregate_pairwise(pairwise_results)
    _dbg(f"  judged: {pw_agg['judged_count']}/{len(cases)}")

    if pw_agg["judged_count"] > 0:
        _dbg(f"\n--- Overall pairwise (A=B-off, B=Hybrid) ---")
        for dim in DIMENSIONS:
            d = pw_agg[dim]
            _dbg(f"  {dim}: B-off={d['a_win']}({d['a_win_rate']:.0%}), "
                 f"Hybrid={d['b_win']}({d['b_win_rate']:.0%}), "
                 f"tie={d['tie']}({d['tie_rate']:.0%})")

        # Per-slice
        for slice_key, slice_vals in [
            ("length_bucket", ["short", "medium", "long"]),
            ("format_bucket", ["bullet", "prose", "mixed"]),
            ("lang", ["en", "zh"]),
        ]:
            _dbg(f"\n--- Pairwise by {slice_key} ---")
            for sv in slice_vals:
                slice_pw = [r for r, c in zip(pairwise_results, cases)
                            if c.get(slice_key) == sv and r is not None]
                if not slice_pw:
                    continue
                h_wins = sum(1 for r in slice_pw if r["overall"] == "B_WIN")
                a_wins = sum(1 for r in slice_pw if r["overall"] == "A_WIN")
                ties = sum(1 for r in slice_pw if r["overall"] == "TIE")
                n = len(slice_pw)
                _dbg(f"  {sv}: Hybrid={h_wins}/{n}({h_wins/n:.0%}), "
                     f"B-off={a_wins}/{n}({a_wins/n:.0%}), "
                     f"tie={ties}/{n}")

        # Hard slices
        _dbg(f"\n--- Hard slices ---")
        for lb, fb in [("short", "bullet"), ("long", "bullet")]:
            hard = [r for r, c in zip(pairwise_results, cases)
                    if c["length_bucket"] == lb and c["format_bucket"] == fb and r is not None]
            if hard:
                h_wins = sum(1 for r in hard if r["overall"] == "B_WIN")
                a_wins = sum(1 for r in hard if r["overall"] == "A_WIN")
                _dbg(f"  {lb}+{fb}: Hybrid={h_wins}/{len(hard)}, B-off={a_wins}/{len(hard)}")
            else:
                _dbg(f"  {lb}+{fb}: no cases in sample")

    # === PRIMARY: Factual Coverage H vs A ===
    from helpers.factual_coverage import aggregate_coverage, evaluate_pair

    _dbg(f"\n=== Factual Coverage: B-off (A) vs Hybrid (H) ===")

    coverage_results = []
    for c in cases:
        result = evaluate_pair(c["source_content"], c["gist_a"], c["gist_h"])
        coverage_results.append(result)

    cov_agg = aggregate_coverage(coverage_results)
    _dbg(f"  evaluated: {cov_agg['evaluated_count']}/{len(cases)}")

    if cov_agg["evaluated_count"] > 0:
        _dbg(f"  total facts: {cov_agg['total_facts']}")
        _dbg(f"\n  B-off (A):")
        _dbg(f"    coverage:    {cov_agg['a_coverage_rate']:.1%}")
        _dbg(f"    fabricated:  {cov_agg['a_fabricated_total']} ({cov_agg['a_fabricated_rate']:.2f}/case)")
        _dbg(f"\n  Hybrid (H):")
        _dbg(f"    coverage:    {cov_agg['b_coverage_rate']:.1%}")
        _dbg(f"    fabricated:  {cov_agg['b_fabricated_total']} ({cov_agg['b_fabricated_rate']:.2f}/case)")

        # Per-length coverage
        _dbg(f"\n--- Factual coverage by length ---")
        for lb in ("short", "medium", "long"):
            lb_cov = [r for r, c in zip(coverage_results, cases)
                      if c["length_bucket"] == lb and r is not None]
            if lb_cov:
                total_f = sum(r["facts_count"] for r in lb_cov)
                a_cov = sum(r["a_covered"] for r in lb_cov) / total_f if total_f else 0
                h_cov = sum(r["b_covered"] for r in lb_cov) / total_f if total_f else 0
                a_fab = sum(r["a_fabricated"] for r in lb_cov)
                h_fab = sum(r["b_fabricated"] for r in lb_cov)
                _dbg(f"  {lb}: A_cov={a_cov:.1%} fab={a_fab}, H_cov={h_cov:.1%} fab={h_fab}")

    # === Superiority Gate ===
    if pw_agg["judged_count"] > 0:
        h_overall_wr = pw_agg["overall"]["b_win_rate"]
        faith = pw_agg["faithfulness"]
        faith_ok = faith["b_win"] >= faith["a_win"]

        hard_ok = True
        for lb, fb in [("short", "bullet"), ("long", "bullet")]:
            hard = [r for r, c in zip(pairwise_results, cases)
                    if c["length_bucket"] == lb and c["format_bucket"] == fb and r is not None]
            if hard:
                h_wins = sum(1 for r in hard if r["overall"] == "B_WIN")
                a_wins = sum(1 for r in hard if r["overall"] == "A_WIN")
                if a_wins > h_wins and h_wins == 0:
                    hard_ok = False

        fab_ok = True
        if cov_agg.get("evaluated_count", 0) > 0:
            fab_ok = cov_agg["b_fabricated_rate"] <= cov_agg["a_fabricated_rate"] + 0.30

        _dbg(f"\n=== Superiority Gate ===")
        _dbg(f"  overall win rate: {h_overall_wr:.1%} (gate: >55%)")
        _dbg(f"  faithfulness: Hybrid={faith['b_win']}, B-off={faith['a_win']} "
             f"({'OK' if faith_ok else 'FAIL'})")
        _dbg(f"  hard slices: {'OK' if hard_ok else 'FAIL'}")
        _dbg(f"  fabrication: Hybrid={cov_agg.get('b_fabricated_rate', '?'):.2f}/case, "
             f"B-off={cov_agg.get('a_fabricated_rate', '?'):.2f}/case "
             f"({'OK' if fab_ok else 'FAIL'})")

        gate_passed = h_overall_wr > 0.55 and faith_ok and hard_ok and fab_ok
        _dbg(f"\n  GATE: {'PASS' if gate_passed else 'FAIL'}")

    assert len(cases) > 0
    assert actual_llm > 0, "LLM health=ok but no LLM gist generated"
