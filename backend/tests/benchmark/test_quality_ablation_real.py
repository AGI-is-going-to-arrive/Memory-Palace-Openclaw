"""Real ablation benchmark: Profile × LLM matrix with real retrieval paths.

Unlike test_quality_ablation.py (CI-fast, mock LLM), this test:
  - Uses real embedding / reranker / LLM services
  - Runs write_guard through real search_advanced (no fake scores)
  - Runs gist through real extractive_bullets / llm_gist paths
  - Tests across Profile B/C/D with LLM on/off (6 cells)

Matrix cells are gated by health checks — unavailable services cause
individual cells to skip, not the entire test.  B-off always runs (no
external dependencies).

Product profile semantics follow docs/DEPLOYMENT_PROFILES.md.
Test harness env overrides are for local validation only.

Artifacts:
  - quality_ablation_real_report.json
  - quality_ablation_real_report.md
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
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
    CellConfig,
    apply_cell_env,
    apply_llm_provider,
    check_cell_runnable,
    make_temp_db_url,
    seed_memories,
    select_llm_provider,
)

REPORT_JSON = BENCHMARK_DIR / "quality_ablation_real_report.json"
REPORT_MD = BENCHMARK_DIR / "quality_ablation_real_report.md"


# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


def _load_intent_gold() -> List[Dict[str, Any]]:
    return _load_jsonl(FIXTURES_DIR / "intent_product_gold_set.jsonl")


def _load_wg_gold() -> List[Dict[str, Any]]:
    return _load_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl")


def _load_gist_gold() -> List[Dict[str, Any]]:
    return _load_jsonl(FIXTURES_DIR / "gist_product_gold_set.jsonl")


# ---------------------------------------------------------------------------
# ROUGE-L (inline, no external dependency)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Tokenize for ROUGE-L: ASCII words stay whole, CJK splits per character."""
    return re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower())


def _lcs_length(a: List[str], b: List[str]) -> int:
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[m][n]


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


# ---------------------------------------------------------------------------
# Component runners
# ---------------------------------------------------------------------------

async def _run_intent(
    monkeypatch: pytest.MonkeyPatch,
    cell: CellConfig,
    gold: List[Dict[str, Any]],
    health: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Run intent classification for a single cell."""
    apply_cell_env(monkeypatch, cell, health)
    client = SQLiteClient("sqlite+aiosqlite:///:memory:")

    cases = []
    try:
        for row in gold:
            query = str(row["query"])
            expected = str(row["expected_intent"])
            normalized = client.preprocess_query(query)

            if cell.llm_enabled:
                try:
                    result = await client.classify_intent_with_llm(
                        query, normalized.get("rewritten_query")
                    )
                except Exception:
                    result = client.classify_intent(
                        query, normalized.get("rewritten_query")
                    )
            else:
                result = client.classify_intent(
                    query, normalized.get("rewritten_query")
                )

            predicted = str(result.get("intent") or "")
            cases.append({
                "id": row["id"],
                "query": query,
                "expected": expected,
                "predicted": predicted,
                "correct": predicted == expected,
            })
    finally:
        await client.close()

    correct = sum(1 for c in cases if c["correct"])
    total = len(cases)
    accuracy = correct / total if total > 0 else 0.0

    by_intent: Dict[str, Dict[str, int]] = {}
    for c in cases:
        key = c["expected"]
        if key not in by_intent:
            by_intent[key] = {"total": 0, "correct": 0}
        by_intent[key]["total"] += 1
        if c["correct"]:
            by_intent[key]["correct"] += 1

    return {
        "total": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "by_intent": {
            k: {**v, "accuracy": round(v["correct"] / v["total"], 4) if v["total"] > 0 else 0}
            for k, v in sorted(by_intent.items())
        },
        "errors": [c for c in cases if not c["correct"]][:20],
    }


async def _run_write_guard(
    monkeypatch: pytest.MonkeyPatch,
    cell: CellConfig,
    gold: List[Dict[str, Any]],
    health: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Run write guard through real retrieval path for a single cell."""
    apply_cell_env(monkeypatch, cell, health)
    db_url = make_temp_db_url()

    client = SQLiteClient(db_url)
    await client.init_db()
    cases = []

    try:
        # Seed existing memories from gold set
        all_existing = []
        for row in gold:
            all_existing.extend(row.get("existing_memories", []))

        # Deduplicate by URI
        seen_uris: set = set()
        unique_existing = []
        for mem in all_existing:
            uri = mem.get("uri", "")
            if uri not in seen_uris:
                seen_uris.add(uri)
                unique_existing.append(mem)

        await seed_memories(client, unique_existing)

        # Ensure embedding index is ready before running write guard.
        # For API embedding backends, seed_memories may trigger async indexing
        # that hasn't completed yet; rebuild_index forces synchronous completion.
        try:
            await client.rebuild_index()
        except Exception:
            pass  # Best effort — keyword fallback still works

        # Run write guard on each test case
        for row in gold:
            content = str(row["content"])
            expected = str(row["expected_action"]).upper()

            try:
                decision = await client.write_guard(
                    content=content,
                    domain="core",
                )
                predicted = str(decision.get("action", "")).upper()
            except Exception:
                predicted = "ERROR"

            cases.append({
                "id": row["id"],
                "expected": expected,
                "predicted": predicted,
            })
    finally:
        await client.close()
        # Clean up temp DB
        db_path = db_url.replace("sqlite+aiosqlite:///", "")
        try:
            Path(db_path).unlink(missing_ok=True)
        except Exception:
            pass

    def _is_block(action: str) -> bool:
        return action in {"UPDATE", "NOOP", "DELETE"}

    tp = sum(1 for c in cases if _is_block(c["expected"]) and _is_block(c["predicted"]))
    fp = sum(1 for c in cases if not _is_block(c["expected"]) and _is_block(c["predicted"]))
    fn = sum(1 for c in cases if _is_block(c["expected"]) and not _is_block(c["predicted"]))
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    exact_match = sum(1 for c in cases if c["expected"] == c["predicted"])

    return {
        "total": len(cases),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "exact_match_accuracy": round(exact_match / len(cases), 4) if cases else 0,
        "errors": [c for c in cases if c["expected"] != c["predicted"]][:20],
    }


def _make_extractive_reference(source: str) -> str:
    """Build an extractive reference from source for dual-reference scoring.

    Takes first and third sentences — mirrors how extractive_bullets works,
    so this reference is 'fair' to the extractive method.
    """
    sentences = [s.strip() for s in re.split(r'[.。]', source) if s.strip()]
    if len(sentences) >= 3:
        picked = [sentences[0], sentences[2]]
    else:
        picked = sentences[:2]
    return "; ".join(picked)[:150]


async def _run_gist(
    monkeypatch: pytest.MonkeyPatch,
    cell: CellConfig,
    gold: List[Dict[str, Any]],
    health: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Run gist generation (extractive_bullets or llm_gist) for a single cell.

    Uses dual-reference scoring: ROUGE-L is computed against both an
    abstractive reference (from gold set) and an extractive reference
    (built on-the-fly from source), then takes the max.  This prevents
    systematic bias toward either method.
    """
    apply_cell_env(monkeypatch, cell, health)
    client = SQLiteClient("sqlite+aiosqlite:///:memory:")

    cases = []
    method_used = "extractive_bullets"

    try:
        for row in gold:
            source = str(row["source_content"])
            abstractive_ref = str(row["reference_gist"])
            extractive_ref = _make_extractive_reference(source)

            degrade_reasons: List[str] = []
            gist_text = ""

            if cell.llm_enabled:
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
                    pass

            # Fallback to extractive bullets if LLM didn't produce a gist
            if not gist_text:
                gist_text = _extractive_bullets(source)
                method_used = "extractive_bullets"

            # Dual-reference: take max ROUGE-L across both reference styles
            rouge_abstractive = _rouge_l_f1(abstractive_ref, gist_text)
            rouge_extractive = _rouge_l_f1(extractive_ref, gist_text)
            rouge = max(rouge_abstractive, rouge_extractive)

            cases.append({
                "id": row["id"],
                "rouge_l": round(rouge, 4),
                "rouge_l_abstractive": round(rouge_abstractive, 4),
                "rouge_l_extractive": round(rouge_extractive, 4),
                "method": method_used,
            })
    finally:
        await client.close()

    rouge_mean = sum(c["rouge_l"] for c in cases) / len(cases) if cases else 0.0

    return {
        "total": len(cases),
        "rouge_l_mean": round(rouge_mean, 4),
        "method": method_used,
        "cases": cases,
    }


def _extractive_bullets(text: str) -> str:
    """Reproduce the heuristic extractive_bullets logic."""
    lines = text.strip().split("\n")
    bullets = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- "):
            bullets.append(stripped[2:][:90])
    if bullets:
        return "; ".join(bullets[:3])
    # Sentence fallback
    sentences = re.split(r'[.。!！?？;；]', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    return "; ".join(sentences[:2])[:280]


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _generate_report(
    matrix_results: Dict[str, Dict[str, Any]],
    health: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the JSON report. No raw env values — only normalized config."""
    return {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "health_checks": {
            k: {kk: vv for kk, vv in v.items() if kk != "reason"}
            for k, v in health.items()
        },
        "matrix": matrix_results,
    }


def _generate_markdown(report: Dict[str, Any]) -> str:
    """Build the Markdown report from JSON report."""
    lines = [
        "# Real Ablation Benchmark Report",
        "",
        f"> generated: {report['generated_at_utc']}",
        "",
        "## Health Checks",
        "",
        "| Service | Status |",
        "|---------|--------|",
    ]
    for svc, info in report["health_checks"].items():
        lines.append(f"| {svc} | {info['status']} |")

    lines.extend(["", "## Intent Classification", "",
                   "| Cell | Status | N | Accuracy |",
                   "|------|--------|--:|--------:|"])
    for cell_id, data in report["matrix"].items():
        if data.get("status") == "skipped":
            lines.append(f"| {cell_id} | skipped | — | — |")
        else:
            intent = data.get("intent", {})
            if intent:
                lines.append(f"| {cell_id} | ran | {intent['total']} | {intent['accuracy']:.3f} |")

    lines.extend(["", "## Write Guard", "",
                   "| Cell | Status | N | Precision | Recall | Exact Match |",
                   "|------|--------|--:|----------:|-------:|------------:|"])
    for cell_id, data in report["matrix"].items():
        if data.get("status") == "skipped":
            lines.append(f"| {cell_id} | skipped | — | — | — | — |")
        else:
            wg = data.get("write_guard", {})
            if wg:
                lines.append(
                    f"| {cell_id} | ran | {wg['total']} | {wg['precision']:.3f} "
                    f"| {wg['recall']:.3f} | {wg['exact_match_accuracy']:.3f} |"
                )

    lines.extend(["", "## Gist Quality", "",
                   "| Cell | Status | N | ROUGE-L | Method |",
                   "|------|--------|--:|--------:|--------|"])
    for cell_id, data in report["matrix"].items():
        if data.get("status") == "skipped":
            lines.append(f"| {cell_id} | skipped | — | — | — |")
        else:
            gist = data.get("gist", {})
            if gist:
                lines.append(
                    f"| {cell_id} | ran | {gist['total']} | {gist['rouge_l_mean']:.3f} "
                    f"| {gist['method']} |"
                )

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

import time as _time


class _Heartbeat:
    """Emit progress updates at fixed intervals."""

    def __init__(self, interval_sec: float = 120.0):
        self._interval = interval_sec
        self._start = _time.time()
        self._last_beat = self._start
        self._cell = ""
        self._component = ""
        self._provider = ""
        self._done = 0
        self._total = 0

    def set_context(self, cell: str, component: str, total: int, provider: str = ""):
        self._cell = cell
        self._component = component
        self._total = total
        self._done = 0
        if provider:
            self._provider = provider

    def tick(self, done: int = 0):
        if done:
            self._done = done
        now = _time.time()
        if now - self._last_beat >= self._interval:
            self._emit(now)
            self._last_beat = now

    def _emit(self, now: float):
        elapsed = now - self._start
        rate = self._done / max(1, now - self._last_beat + self._interval) if self._done else 0
        remaining = self._total - self._done
        eta = remaining / rate if rate > 0 else 0
        print(
            f"[heartbeat] elapsed={elapsed:.0f}s | cell={self._cell} | "
            f"component={self._component} | {self._done}/{self._total} | "
            f"provider={self._provider} | ETA={eta:.0f}s",
            file=sys.stderr, flush=True,
        )

    def summary(self, cell: str, component: str, result_key: str, value: Any):
        elapsed = _time.time() - self._start
        print(
            f"[heartbeat] elapsed={elapsed:.0f}s | {cell}/{component} done | "
            f"{result_key}={value}",
            file=sys.stderr, flush=True,
        )


@pytest.mark.asyncio
async def test_quality_ablation_real_matrix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run all 6 matrix cells, gated by health checks. B-off always runs."""
    _dbg = lambda msg: print(f"[ablation] {msg}", file=sys.stderr, flush=True)
    hb = _Heartbeat(interval_sec=120.0)

    _dbg("Loading gold sets...")
    intent_gold = _load_intent_gold()
    wg_gold = _load_wg_gold()
    gist_gold = _load_gist_gold()

    assert len(intent_gold) >= 100, f"intent gold set too small: {len(intent_gold)}"
    assert len(wg_gold) >= 100, f"write guard gold set too small: {len(wg_gold)}"
    assert len(gist_gold) >= 50, f"gist gold set too small: {len(gist_gold)}"

    _dbg("Running health checks...")
    health = await run_all_health_checks()
    _dbg(f"Health: embedding={health['embedding']['status']}, "
         f"reranker={health['reranker']['status']}, llm={health['llm']['status']}")

    matrix_results: Dict[str, Dict[str, Any]] = {}
    cells_run = 0
    cells_skipped = 0

    for cell_idx, cell in enumerate(MATRIX_CELLS):
        _dbg(f"\n{'='*50}")
        _dbg(f"Cell {cell_idx+1}/{len(MATRIX_CELLS)}: {cell.cell_id}")

        # --- Per-cell LLM provider selection ---
        provider_label = "none"
        provider_degraded = False
        if cell.llm_enabled:
            provider = select_llm_provider()
            if provider is None:
                cells_skipped += 1
                _dbg(f"{cell.cell_id}: SKIPPED — all 3 LLM tiers failed")
                matrix_results[cell.cell_id] = {
                    "config": cell.config_summary,
                    "status": "skipped",
                    "skip_reason": "All 3 LLM provider tiers unreachable",
                }
                continue
            apply_llm_provider(monkeypatch, provider)
            provider_label = provider.label
            provider_degraded = provider.degraded
            # Re-run health check with the selected provider
            health = await run_all_health_checks()

        skip_reason = check_cell_runnable(cell, health)
        if skip_reason:
            cells_skipped += 1
            _dbg(f"{cell.cell_id}: SKIPPED — {skip_reason}")
            matrix_results[cell.cell_id] = {
                "config": cell.config_summary,
                "status": "skipped",
                "skip_reason": skip_reason,
            }
            continue

        cells_run += 1
        cell_result: Dict[str, Any] = {
            "config": cell.config_summary,
            "status": "ran",
            "provider": provider_label,
        }
        if provider_degraded:
            cell_result["provider_degraded"] = True

        # --- Intent ---
        hb.set_context(cell.cell_id, "intent", len(intent_gold), provider_label)
        _dbg(f"{cell.cell_id}: running intent ({len(intent_gold)} cases) provider={provider_label}")
        cell_result["intent"] = await _run_intent(monkeypatch, cell, intent_gold, health)
        hb.summary(cell.cell_id, "intent", "accuracy", cell_result["intent"]["accuracy"])

        # --- Write Guard ---
        hb.set_context(cell.cell_id, "write_guard", len(wg_gold), provider_label)
        _dbg(f"{cell.cell_id}: running write_guard ({len(wg_gold)} cases)")
        cell_result["write_guard"] = await _run_write_guard(monkeypatch, cell, wg_gold, health)
        hb.summary(cell.cell_id, "write_guard", "exact_match",
                    cell_result["write_guard"]["exact_match_accuracy"])

        # --- Gist (diagnostic only per user constraint) ---
        hb.set_context(cell.cell_id, "gist", len(gist_gold), provider_label)
        _dbg(f"{cell.cell_id}: running gist ({len(gist_gold)} cases)")
        cell_result["gist"] = await _run_gist(monkeypatch, cell, gist_gold, health)
        hb.summary(cell.cell_id, "gist", "rouge_l", cell_result["gist"]["rouge_l_mean"])

        matrix_results[cell.cell_id] = cell_result

    _dbg(f"\n{'='*50}")
    _dbg(f"Matrix complete: {cells_run} ran, {cells_skipped} skipped")

    # B-off must always run
    assert "B-off" in matrix_results, "B-off cell must always run (no external dependencies)"

    # Generate report
    report = _generate_report(matrix_results, health)
    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    REPORT_MD.write_text(_generate_markdown(report), encoding="utf-8")

    # Timestamped archival copy
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_json = BENCHMARK_DIR / f"quality_ablation_real_report.{ts}.json"
    archive_md = BENCHMARK_DIR / f"quality_ablation_real_report.{ts}.md"
    archive_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    archive_md.write_text(_generate_markdown(report), encoding="utf-8")

    assert REPORT_JSON.exists()
    assert REPORT_MD.exists()

    # Sanity: B-off intent accuracy should be reasonable
    b_off = matrix_results["B-off"]
    assert b_off["intent"]["accuracy"] >= 0.5, (
        f"B-off intent accuracy {b_off['intent']['accuracy']:.3f} below 0.5 floor"
    )
