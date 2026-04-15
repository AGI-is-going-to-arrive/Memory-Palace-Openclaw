"""B-off canary: small-batch targeted check after P0/P1 fixes.

Runs ONLY B-off cell on subsets (30 WG + 20 gist) and outputs per-slice
breakdown to verify the repair direction before committing to a full run.

    backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_canary_boff.py
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
from helpers.real_retrieval_harness import (
    MATRIX_CELLS,
    apply_cell_env,
    make_temp_db_url,
    seed_memories,
)

random.seed(2026_04_05)

CANARY_WG_SIZE = 30
CANARY_GIST_SIZE = 36  # 2 per (length × format × lang) cell = 18 cells × 2

B_OFF = [c for c in MATRIX_CELLS if c.cell_id == "B-off"][0]


# ---------------------------------------------------------------------------
# Helpers (copied from test_quality_ablation_real.py to stay self-contained)
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
    _dbg(f"  {value}: ROUGE-L={mean:.3f} (n={len(bucket)})")


def _make_extractive_reference(source: str) -> str:
    sentences = [s.strip() for s in re.split(r'[.。]', source) if s.strip()]
    if len(sentences) >= 3:
        picked = [sentences[0], sentences[2]]
    else:
        picked = sentences[:2]
    return "; ".join(picked)[:150]


# ---------------------------------------------------------------------------
# Stratified sampling
# ---------------------------------------------------------------------------

def _stratified_sample(rows: List[Dict], key: str, n: int) -> List[Dict]:
    """Sample n rows stratified by key, balanced across values."""
    buckets: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        buckets[r.get(key, "unknown")].append(r)
    per_bucket = max(1, n // len(buckets))
    result = []
    for vals in buckets.values():
        result.extend(random.sample(vals, min(per_bucket, len(vals))))
    # Top up if short
    remaining = [r for r in rows if r not in result]
    while len(result) < n and remaining:
        result.append(remaining.pop())
    return result[:n]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_canary_write_guard_boff(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-off canary: 30-case WG subset with per-slice breakdown."""
    _dbg = lambda msg: print(f"[canary-wg] {msg}", file=sys.stderr, flush=True)

    all_wg = _load_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl")
    subset = _stratified_sample(all_wg, "expected_action", CANARY_WG_SIZE)

    _dbg(f"Subset: {len(subset)} cases")
    for action in ("ADD", "UPDATE", "NOOP"):
        count = sum(1 for r in subset if r["expected_action"] == action)
        _dbg(f"  {action}: {count}")

    health = {"embedding": {"status": "unavailable"}, "reranker": {"status": "unavailable"}, "llm": {"status": "unavailable"}}
    apply_cell_env(monkeypatch, B_OFF, health)

    db_url = make_temp_db_url()
    client = SQLiteClient(db_url)
    await client.init_db()

    cases = []
    try:
        # Seed all existing memories
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

    # Global metrics
    exact_match = sum(1 for c in cases if c["expected"] == c["predicted"])
    em_acc = exact_match / len(cases) if cases else 0
    _dbg(f"\n=== GLOBAL: Exact Match = {em_acc:.3f} ({exact_match}/{len(cases)}) ===")

    # Per-slice breakdown: expected_action
    _dbg("\n--- By expected_action ---")
    for action in ("ADD", "UPDATE", "NOOP"):
        action_cases = [c for c in cases if c["expected"] == action]
        if action_cases:
            hit = sum(1 for c in action_cases if c["expected"] == c["predicted"])
            _dbg(f"  {action}: {hit}/{len(action_cases)} = {hit/len(action_cases):.3f}")

    # Per-slice: lang
    _dbg("\n--- By lang ---")
    for lang in ("en", "zh"):
        lang_cases = [c for c in cases if c["lang"] == lang]
        if lang_cases:
            hit = sum(1 for c in lang_cases if c["expected"] == c["predicted"])
            _dbg(f"  {lang}: {hit}/{len(lang_cases)} = {hit/len(lang_cases):.3f}")

    # Per-slice: scenario
    _dbg("\n--- By scenario ---")
    scenarios = sorted(set(c["scenario"] for c in cases))
    for scen in scenarios:
        scen_cases = [c for c in cases if c["scenario"] == scen]
        if scen_cases:
            hit = sum(1 for c in scen_cases if c["expected"] == c["predicted"])
            _dbg(f"  {scen}: {hit}/{len(scen_cases)} = {hit/len(scen_cases):.3f}")

    # Confusion details
    misses = [c for c in cases if c["expected"] != c["predicted"]]
    if misses:
        _dbg(f"\n--- Misclassified ({len(misses)}) ---")
        for m in misses[:10]:
            _dbg(f"  {m['id']}: expected={m['expected']}, predicted={m['predicted']}, lang={m['lang']}, scenario={m['scenario']}")

    # Gate: B-off WG EM must pass the full-rerun threshold.
    # This is the encoded rerun gate — if canary doesn't pass 0.60,
    # full 6-cell matrix should not be started.
    assert em_acc >= 0.60, (
        f"B-off WG exact match {em_acc:.3f} below 0.60 rerun gate. "
        f"Check seed_memories, FTS indexing, and gold set."
    )


@pytest.mark.asyncio
async def test_canary_gist_boff(monkeypatch: pytest.MonkeyPatch) -> None:
    """B-off canary: bucketed gist eval with ROUGE-L + BERTScore + rubric."""
    _dbg = lambda msg: print(f"[canary-gist] {msg}", file=sys.stderr, flush=True)

    all_gist = _load_jsonl(FIXTURES_DIR / "gist_product_gold_set.jsonl")
    # Stratify by length_bucket to ensure all buckets are represented
    subset = _stratified_sample(all_gist, "length_bucket", CANARY_GIST_SIZE)

    _dbg(f"Subset: {len(subset)} cases")
    _dbg(f"  length: {_count_by(subset, 'length_bucket')}")
    _dbg(f"  format: {_count_by(subset, 'format_bucket')}")
    _dbg(f"  lang:   {_count_by(subset, 'lang')}")

    health = {"embedding": {"status": "unavailable"}, "reranker": {"status": "unavailable"}, "llm": {"status": "unavailable"}}
    apply_cell_env(monkeypatch, B_OFF, health)

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    cases = []

    try:
        for row in subset:
            source = str(row["source_content"])
            abstractive_ref = str(row["reference_gist"])
            extractive_ref = _make_extractive_reference(source)

            gist_text = _extractive_bullets(source)

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
                "lang": row.get("lang", ""),
                "scenario": row.get("scenario", ""),
                "length_bucket": row.get("length_bucket", "medium"),
                "format_bucket": row.get("format_bucket", "prose"),
            })
    finally:
        await client.close()

    rouge_mean = sum(c["rouge_l"] for c in cases) / len(cases) if cases else 0

    _dbg(f"\n=== GLOBAL: ROUGE-L = {rouge_mean:.3f} ===")

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

    _dbg(f"\nMethod: extractive_bullets (B-off, no LLM)")

    # --- BERTScore per lang (observation only) ---
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
            _dbg(f"\n--- BERTScore ({lang}): mean={sum(bert_scores)/len(bert_scores):.3f} (n={len(bert_scores)}) ---")
            # Per-bucket BERTScore
            for lb in ("short", "medium", "long"):
                lb_cases = [c for c in lang_cases if c["length_bucket"] == lb and "bertscore_f1" in c]
                if lb_cases:
                    m = sum(c["bertscore_f1"] for c in lb_cases) / len(lb_cases)
                    _dbg(f"  {lb}: {m:.3f} (n={len(lb_cases)})")
        else:
            _dbg(f"\n--- BERTScore ({lang}): skipped (unavailable) ---")

    # --- Rubric judge (observation only; reference-free) ---
    from helpers.rubric_judge import aggregate_scores, judge_batch

    rubric_inputs = [
        {"source_content": c["source_content"], "candidate_gist": c["candidate_gist"],
         "expected_lang": c["lang"] or "en"}
        for c in cases
    ]
    rubric_scores = judge_batch(rubric_inputs)
    rubric_agg = aggregate_scores(rubric_scores)

    _dbg(f"\n--- Rubric Judge (reference-free) ---")
    _dbg(f"  judged: {rubric_agg['judged_count']}/{len(cases)}")
    if rubric_agg["judged_count"] > 0:
        for dim in ("coverage", "faithfulness", "conciseness", "language_match"):
            _dbg(f"  {dim}: {rubric_agg[f'{dim}_mean']:.2f}/5")
        _dbg(f"  overall: {rubric_agg['overall_mean']:.2f}/5")
    else:
        _dbg(f"  skipped (LLM unavailable for B-off)")
