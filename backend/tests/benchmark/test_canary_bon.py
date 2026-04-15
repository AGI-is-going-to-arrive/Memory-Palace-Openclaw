"""B-on canary: LLM-enabled targeted check.

Tests B-on cell (Profile B + LLM enabled) on subsets to measure LLM delta
vs the B-off baseline.  Skips if LLM service is unavailable.

    WRITE_GUARD_LLM_API_BASE=... WRITE_GUARD_LLM_API_KEY=... WRITE_GUARD_LLM_MODEL=... \
    backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_canary_bon.py
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
    make_temp_db_url,
    seed_memories,
)

random.seed(2026_04_05)

CANARY_WG_SIZE = 30
CANARY_GIST_SIZE = 36  # 2 per (length × format × lang) cell = 18 cells × 2

B_ON = [c for c in MATRIX_CELLS if c.cell_id == "B-on"][0]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


def _tokenize(text: str) -> List[str]:
    """Tokenize for ROUGE-L: ASCII words stay whole, CJK splits per character."""
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


def _count_by(rows: List[Dict], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for r in rows:
        v = r.get(key, "?")
        counts[v] = counts.get(v, 0) + 1
    return counts


def _print_bucket_rouge(_dbg, cases, key, value):
    bucket = [c for c in cases if c.get(key) == value]
    if not bucket:
        return
    mean = sum(c["rouge_l"] for c in bucket) / len(bucket)
    llm = sum(1 for c in bucket if c.get("method") == "llm_gist")
    fb = sum(1 for c in bucket if c.get("method") == "extractive_bullets")
    _dbg(f"  {value}: ROUGE-L={mean:.3f} (n={len(bucket)}, llm={llm}, fb={fb})")


def _make_extractive_reference(source: str) -> str:
    sentences = [s.strip() for s in re.split(r'[.。]', source) if s.strip()]
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_canary_write_guard_bon(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-on canary: 30-case WG subset with LLM enabled."""
    _dbg = lambda msg: print(f"[canary-wg-bon] {msg}", file=sys.stderr, flush=True)

    health = await run_all_health_checks()
    if health["llm"]["status"] != "ok":
        pytest.skip(f"LLM unavailable: {health['llm'].get('reason', 'unknown')}")

    all_wg = _load_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl")
    subset = _stratified_sample(all_wg, "expected_action", CANARY_WG_SIZE)

    _dbg(f"Subset: {len(subset)} cases, LLM: on")
    for action in ("ADD", "UPDATE", "NOOP"):
        count = sum(1 for r in subset if r["expected_action"] == action)
        _dbg(f"  {action}: {count}")

    apply_cell_env(monkeypatch, B_ON, health)

    db_url = make_temp_db_url()
    client = SQLiteClient(db_url)
    await client.init_db()

    cases = []
    try:
        all_existing = []
        for row in subset:
            all_existing.extend(row.get("existing_memories", []))
        seen_uris: set = set()
        unique_existing = []
        for mem in all_existing:
            uri = mem.get("uri", "")
            if uri not in seen_uris:
                seen_uris.add(uri)
                unique_existing.append(mem)

        _dbg(f"Seeding {len(unique_existing)} existing memories...")
        await seed_memories(client, unique_existing)

        for row in subset:
            content = str(row["content"])
            expected = str(row["expected_action"]).upper()
            try:
                decision = await client.write_guard(content=content, domain="core")
                predicted = str(decision.get("action", "")).upper()
            except Exception:
                predicted = "ERROR"
            cases.append({
                "id": row["id"],
                "expected": expected,
                "predicted": predicted,
                "scenario": row.get("scenario", ""),
                "lang": row.get("lang", ""),
            })
    finally:
        await client.close()
        db_path = db_url.replace("sqlite+aiosqlite:///", "")
        Path(db_path).unlink(missing_ok=True)

    exact_match = sum(1 for c in cases if c["expected"] == c["predicted"])
    em_acc = exact_match / len(cases) if cases else 0
    _dbg(f"\n=== GLOBAL: Exact Match = {em_acc:.3f} ({exact_match}/{len(cases)}) ===")

    _dbg("\n--- By expected_action ---")
    for action in ("ADD", "UPDATE", "NOOP"):
        action_cases = [c for c in cases if c["expected"] == action]
        if action_cases:
            hit = sum(1 for c in action_cases if c["expected"] == c["predicted"])
            _dbg(f"  {action}: {hit}/{len(action_cases)} = {hit/len(action_cases):.3f}")

    _dbg("\n--- By lang ---")
    for lang in ("en", "zh"):
        lang_cases = [c for c in cases if c["lang"] == lang]
        if lang_cases:
            hit = sum(1 for c in lang_cases if c["expected"] == c["predicted"])
            _dbg(f"  {lang}: {hit}/{len(lang_cases)} = {hit/len(lang_cases):.3f}")

    misses = [c for c in cases if c["expected"] != c["predicted"]]
    if misses:
        _dbg(f"\n--- Misclassified ({len(misses)}) ---")
        for m in misses[:10]:
            _dbg(f"  {m['id']}: expected={m['expected']}, predicted={m['predicted']}, lang={m['lang']}, scenario={m['scenario']}")

    # B-on should not regress below B-off baseline
    assert em_acc >= 0.60, (
        f"B-on WG exact match {em_acc:.3f} below 0.60 gate"
    )


@pytest.mark.asyncio
async def test_canary_gist_bon(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-on canary: bucketed gist eval with ROUGE-L + BERTScore + rubric + auditability."""
    _dbg = lambda msg: print(f"[canary-gist-bon] {msg}", file=sys.stderr, flush=True)

    health = await run_all_health_checks()
    if health["llm"]["status"] != "ok":
        pytest.skip(f"LLM unavailable: {health['llm'].get('reason', 'unknown')}")

    all_gist = _load_jsonl(FIXTURES_DIR / "gist_product_gold_set.jsonl")
    subset = _stratified_sample(all_gist, "length_bucket", CANARY_GIST_SIZE)

    _dbg(f"Subset: {len(subset)} cases, LLM: on")
    _dbg(f"  length: {_count_by(subset, 'length_bucket')}")
    _dbg(f"  format: {_count_by(subset, 'format_bucket')}")
    _dbg(f"  lang:   {_count_by(subset, 'lang')}")

    apply_cell_env(monkeypatch, B_ON, health)

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    await client.init_db()
    cases = []
    all_degrade_reasons: List[str] = []

    try:
        for row in subset:
            source = str(row["source_content"])
            abstractive_ref = str(row["reference_gist"])
            extractive_ref = _make_extractive_reference(source)

            degrade_reasons: List[str] = []
            gist_text = ""
            method_used = "extractive_bullets"

            try:
                result = await client.generate_compact_gist(
                    summary=source,
                    max_points=3,
                    max_chars=280,
                    degrade_reasons=degrade_reasons,
                )
                if result and result.get("gist_text"):
                    gist_text = str(result["gist_text"])
                    method_used = result.get("gist_method", "llm_gist")
            except Exception:
                degrade_reasons.append("generate_compact_gist raised exception")

            if not gist_text:
                gist_text = _extractive_bullets(source)
                method_used = "extractive_bullets"

            all_degrade_reasons.extend(degrade_reasons)

            rouge_abs = _rouge_l_f1(abstractive_ref, gist_text)
            rouge_ext = _rouge_l_f1(extractive_ref, gist_text)
            rouge = max(rouge_abs, rouge_ext)

            cases.append({
                "id": row["id"],
                "source_content": source,
                "candidate_gist": gist_text,
                "rouge_l": round(rouge, 4),
                "rouge_l_abstractive": round(rouge_abs, 4),
                "rouge_l_extractive": round(rouge_ext, 4),
                "method": method_used,
                "degrade_reasons": list(degrade_reasons),
                "lang": row.get("lang", ""),
                "scenario": row.get("scenario", ""),
                "length_bucket": row.get("length_bucket", "medium"),
                "format_bucket": row.get("format_bucket", "prose"),
            })
    finally:
        await client.close()

    rouge_mean = sum(c["rouge_l"] for c in cases) / len(cases) if cases else 0
    llm_count = sum(1 for c in cases if c["method"] == "llm_gist")
    fallback_count = sum(1 for c in cases if c["method"] == "extractive_bullets")
    method_counts = _count_by(cases, "method")

    _dbg(f"\n=== GLOBAL: ROUGE-L = {rouge_mean:.3f} ===")

    _dbg(f"\n=== Auditability ===")
    _dbg(f"  llm_count: {llm_count}")
    _dbg(f"  fallback_count: {fallback_count}")
    _dbg(f"  method_counts: {method_counts}")
    if all_degrade_reasons:
        reason_counts: Dict[str, int] = {}
        for r in all_degrade_reasons:
            reason_counts[r] = reason_counts.get(r, 0) + 1
        _dbg(f"  degrade_reasons: {reason_counts}")
    else:
        _dbg(f"  degrade_reasons: (none)")

    # --- Per-bucket: length ---
    _dbg("\n--- By length_bucket ---")
    for lb in ("short", "medium", "long"):
        _print_bucket_rouge(_dbg, cases, "length_bucket", lb)

    # --- Per-bucket: format ---
    _dbg("\n--- By format_bucket ---")
    for fb in ("bullet", "prose", "mixed"):
        _print_bucket_rouge(_dbg, cases, "format_bucket", fb)

    # --- Per-bucket: lang ---
    _dbg("\n--- By lang ---")
    for lang in ("en", "zh"):
        _print_bucket_rouge(_dbg, cases, "lang", lang)

    # --- BERTScore per lang + per-bucket (observation only) ---
    from helpers.semantic_metrics import compute_bertscore_batch

    for lang in ("en", "zh"):
        lang_cases = [c for c in cases if c["lang"] == lang]
        if not lang_cases:
            continue
        refs = [c["source_content"] for c in lang_cases]
        cands = [c["candidate_gist"] for c in lang_cases]
        bert_scores = compute_bertscore_batch(refs, cands, lang=lang)
        if bert_scores is not None:
            for i, c in enumerate(lang_cases):
                c["bertscore_f1"] = bert_scores[i]
            _dbg(f"\n--- BERTScore ({lang}): mean={sum(bert_scores)/len(bert_scores):.3f} ---")
            for lb in ("short", "medium", "long"):
                lb_cases = [c for c in lang_cases if c["length_bucket"] == lb and "bertscore_f1" in c]
                if lb_cases:
                    m = sum(c["bertscore_f1"] for c in lb_cases) / len(lb_cases)
                    _dbg(f"  {lb}: {m:.3f} (n={len(lb_cases)})")
            for fb in ("bullet", "prose", "mixed"):
                fb_cases = [c for c in lang_cases if c["format_bucket"] == fb and "bertscore_f1" in c]
                if fb_cases:
                    m = sum(c["bertscore_f1"] for c in fb_cases) / len(fb_cases)
                    _dbg(f"  {fb}: {m:.3f} (n={len(fb_cases)})")
        else:
            _dbg(f"\n--- BERTScore ({lang}): skipped (unavailable) ---")

    # --- Rubric judge (observation only; reference-free) ---
    from helpers.rubric_judge import DIMENSIONS, aggregate_scores, judge_batch

    rubric_inputs = [
        {"source_content": c["source_content"], "candidate_gist": c["candidate_gist"],
         "expected_lang": c["lang"] or "en"}
        for c in cases
    ]
    rubric_scores = judge_batch(rubric_inputs)
    for i, s in enumerate(rubric_scores):
        if s is not None:
            cases[i]["rubric"] = s
    rubric_agg = aggregate_scores(rubric_scores)

    _dbg(f"\n--- Rubric Judge (reference-free) ---")
    _dbg(f"  judged: {rubric_agg['judged_count']}/{len(cases)}")
    if rubric_agg["judged_count"] > 0:
        for dim in DIMENSIONS:
            _dbg(f"  {dim}: {rubric_agg[f'{dim}_mean']:.2f}/5")
        _dbg(f"  overall: {rubric_agg['overall_mean']:.2f}/5")
        # Per-bucket rubric
        for lb in ("short", "medium", "long"):
            lb_cases = [c for c in cases if c["length_bucket"] == lb and "rubric" in c]
            if lb_cases:
                overall = sum(sum(c["rubric"][d] for d in DIMENSIONS) / len(DIMENSIONS) for c in lb_cases) / len(lb_cases)
                _dbg(f"  rubric {lb}: {overall:.2f}/5 (n={len(lb_cases)})")
    else:
        _dbg(f"  skipped (LLM unavailable)")

    # --- Gates ---
    assert rouge_mean >= 0.30, (
        f"B-on gist ROUGE-L {rouge_mean:.3f} below 0.30 absolute floor"
    )
    assert llm_count > 0, (
        f"LLM health=ok but llm_count=0; all {len(cases)} cases fell back to extractive. "
        f"degrade_reasons={all_degrade_reasons[:5]}"
    )
