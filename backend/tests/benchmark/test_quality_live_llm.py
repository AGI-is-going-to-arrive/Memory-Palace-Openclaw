"""Live LLM quality benchmark: real API calls against MiniMax-M2.5.

Unlike test_quality_ablation.py which uses deterministic mocks, this test
sends every gold-set query to a real LLM endpoint and measures accuracy.

Components tested:
- Intent classification: real LLM vs keyword_scoring_v2
- Write Guard: real LLM-assisted vs heuristic-only

Usage:
    backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_quality_live_llm.py

Environment variables (set automatically in the test; override via shell if needed):
    LIVE_LLM_API_BASE   - default https://api.edgefn.net/v1
    LIVE_LLM_API_KEY    - required
    LIVE_LLM_MODEL      - default MiniMax-M2.5
"""
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from db.sqlite_client import SQLiteClient

BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BENCHMARK_DIR.parent / "fixtures"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

LIVE_REPORT_JSON = BENCHMARK_DIR / "quality_live_llm_report.json"
LIVE_REPORT_MD = BENCHMARK_DIR / "quality_live_llm_report.md"

_SQLITE_MEMORY_URL = "sqlite+aiosqlite:///:memory:"

# Rate-limit: pause between API calls to avoid 429.
# MiniMax-M2.5 has RPM=10 limit, so we need ~6s between calls.
_API_CALL_DELAY = float(os.environ.get("LIVE_LLM_CALL_DELAY", "6.5"))


def _strip_llm_wrapper(text: str) -> str:
    """Strip <think> tags and markdown code blocks from LLM response text.

    MiniMax-M2.5 often returns:
        <think>reasoning here...</think>
        ```json
        {"key": "value"}
        ```
    This function returns the clean JSON string for downstream parsing.
    """
    if not text:
        return text
    # Strip <think>...</think> blocks
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    # Extract from markdown code blocks (prefer ```json over bare ```)
    json_block = re.search(r"```json\s*\n?(.*?)\n?\s*```", cleaned, re.DOTALL)
    if json_block:
        return json_block.group(1).strip()
    bare_block = re.search(r"```\s*\n?(.*?)\n?\s*```", cleaned, re.DOTALL)
    if bare_block:
        return bare_block.group(1).strip()
    return cleaned


# ---------------------------------------------------------------------------
# Intent: live LLM
# ---------------------------------------------------------------------------

def _load_intent_gold() -> List[Dict[str, Any]]:
    path = FIXTURES_DIR / "intent_gold_set.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        rows.append(json.loads(raw))
    return rows


async def _run_intent_live(
    monkeypatch: pytest.MonkeyPatch,
    gold: List[Dict[str, Any]],
    api_base: str,
    api_key: str,
    model: str,
) -> Dict[str, Any]:
    """Run intent classification with a real LLM endpoint."""
    monkeypatch.setenv("INTENT_LLM_ENABLED", "true")
    monkeypatch.setenv("INTENT_LLM_API_BASE", api_base)
    monkeypatch.setenv("INTENT_LLM_API_KEY", api_key)
    monkeypatch.setenv("INTENT_LLM_MODEL", model)

    client = SQLiteClient(_SQLITE_MEMORY_URL)

    # Wrap _extract_chat_message_text to handle <think> tags
    original_extract = SQLiteClient._extract_chat_message_text

    def _patched_extract(payload: Dict[str, Any]) -> str:
        raw = original_extract(payload)
        if not raw:
            return raw
        return _strip_llm_wrapper(raw)

    monkeypatch.setattr(SQLiteClient, "_extract_chat_message_text", staticmethod(_patched_extract))

    cases = []
    api_errors = 0
    start_time = time.monotonic()
    max_retries = 2

    try:
        for i, row in enumerate(gold):
            query = str(row["query"])
            expected = str(row["expected_intent"])
            normalized = client.preprocess_query(query)

            predicted = ""
            method = ""
            for attempt in range(max_retries + 1):
                try:
                    result = await client.classify_intent_with_llm(
                        query, normalized.get("rewritten_query")
                    )
                    predicted = str(result.get("intent") or "")
                    method = str(result.get("method") or "")
                    degraded = bool(result.get("degraded"))
                    if degraded and attempt < max_retries:
                        await asyncio.sleep(_API_CALL_DELAY * 2)
                        continue
                    if degraded:
                        api_errors += 1
                    break
                except Exception as exc:
                    if attempt < max_retries:
                        await asyncio.sleep(_API_CALL_DELAY * 2)
                        continue
                    predicted = ""
                    method = f"error:{type(exc).__name__}"
                    api_errors += 1
                    break

            cases.append({
                "id": row["id"],
                "query": query,
                "expected": expected,
                "predicted": predicted,
                "method": method,
                "correct": predicted == expected,
            })

            if _API_CALL_DELAY > 0 and i < len(gold) - 1:
                await asyncio.sleep(_API_CALL_DELAY)
    finally:
        await client.close()

    elapsed = time.monotonic() - start_time
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
        "mode": f"live_llm ({model})",
        "total": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "api_errors": api_errors,
        "elapsed_seconds": round(elapsed, 1),
        "by_intent": {
            k: {**v, "accuracy": round(v["correct"] / v["total"], 4) if v["total"] > 0 else 0}
            for k, v in sorted(by_intent.items())
        },
        "errors": [c for c in cases if not c["correct"]],
    }


async def _run_intent_keyword(
    monkeypatch: pytest.MonkeyPatch,
    gold: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Run intent classification with keyword-only (baseline)."""
    monkeypatch.setenv("INTENT_LLM_ENABLED", "false")
    client = SQLiteClient(_SQLITE_MEMORY_URL)
    cases = []
    try:
        for row in gold:
            query = str(row["query"])
            expected = str(row["expected_intent"])
            normalized = client.preprocess_query(query)
            result = client.classify_intent(query, normalized.get("rewritten_query"))
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
    return {
        "mode": "keyword_scoring_v2",
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total > 0 else 0.0,
        "errors": [c for c in cases if not c["correct"]],
    }


# ---------------------------------------------------------------------------
# Write Guard: live LLM
# ---------------------------------------------------------------------------

def _load_wg_gold() -> List[Dict[str, Any]]:
    path = FIXTURES_DIR / "write_guard_gold_set.jsonl"
    rows = []
    for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        raw = line.strip()
        if not raw:
            continue
        row = json.loads(raw)
        row["case_index"] = idx
        rows.append(row)
    return rows


def _make_wg_eval_client(rows_by_query: Dict[str, Dict[str, Any]]) -> SQLiteClient:
    """Create a SQLiteClient subclass with mocked search_advanced for write guard eval."""

    class _EvalClient(SQLiteClient):
        def __init__(self):
            super().__init__(_SQLITE_MEMORY_URL)
            self._rows_by_query = rows_by_query

        async def search_advanced(self, *, query, mode, max_results, candidate_multiplier, filters):
            row = self._rows_by_query.get(query)
            if row is None:
                return {"results": [], "degrade_reasons": []}
            if mode == "semantic":
                score = float(row.get("semantic_vector_score") or 0.0)
                if score <= 0.0:
                    return {"results": [], "degrade_reasons": []}
                return {
                    "results": [{
                        "memory_id": row["case_index"],
                        "uri": f"core://guard/{row['id']}",
                        "snippet": f"candidate {row['id']}",
                        "scores": {"vector": score, "text": 0.05, "final": score},
                    }],
                    "degrade_reasons": [],
                }
            if mode == "keyword":
                score = float(row.get("keyword_text_score") or 0.0)
                if score <= 0.0:
                    return {"results": [], "degrade_reasons": []}
                return {
                    "results": [{
                        "memory_id": 1000 + row["case_index"],
                        "uri": f"core://guard/{row['id']}",
                        "snippet": f"candidate {row['id']}",
                        "scores": {"vector": 0.05, "text": score, "final": score},
                    }],
                    "degrade_reasons": [],
                }
            return {"results": [], "degrade_reasons": []}

    return _EvalClient()


def _is_block(action: str) -> bool:
    return action in {"UPDATE", "NOOP", "DELETE"}


async def _run_wg_live(
    monkeypatch: pytest.MonkeyPatch,
    gold: List[Dict[str, Any]],
    api_base: str,
    api_key: str,
    model: str,
) -> Dict[str, Any]:
    """Run write guard with real LLM endpoint."""
    rows_by_query = {str(row["content"]): row for row in gold}

    monkeypatch.setenv("WRITE_GUARD_LLM_ENABLED", "true")
    monkeypatch.setenv("WRITE_GUARD_LLM_API_BASE", api_base)
    monkeypatch.setenv("WRITE_GUARD_LLM_API_KEY", api_key)
    monkeypatch.setenv("WRITE_GUARD_LLM_MODEL", model)

    client = _make_wg_eval_client(rows_by_query)

    # Patch _extract_chat_message_text to handle <think> tags
    original_extract = SQLiteClient._extract_chat_message_text

    def _patched_extract(payload: Dict[str, Any]) -> str:
        raw = original_extract(payload)
        if not raw:
            return raw
        return _strip_llm_wrapper(raw)

    monkeypatch.setattr(SQLiteClient, "_extract_chat_message_text", staticmethod(_patched_extract))

    cases = []
    api_errors = 0
    start_time = time.monotonic()

    max_retries = 2
    try:
        for i, row in enumerate(gold):
            predicted = "ERROR"
            for attempt in range(max_retries + 1):
                try:
                    decision = await client.write_guard(content=str(row["content"]), domain="core")
                    predicted = str(decision.get("action", "")).upper()
                    if predicted and predicted != "ERROR":
                        break
                except Exception:
                    if attempt < max_retries:
                        await asyncio.sleep(_API_CALL_DELAY * 2)
                        continue
                    api_errors += 1
                    break

            expected = str(row["expected_action"]).upper()
            cases.append({
                "id": row["id"],
                "expected": expected,
                "predicted": predicted,
                "expected_block": _is_block(expected),
                "predicted_block": _is_block(predicted),
            })

            if _API_CALL_DELAY > 0 and i < len(gold) - 1:
                await asyncio.sleep(_API_CALL_DELAY)
    finally:
        await client.close()

    elapsed = time.monotonic() - start_time
    tp = sum(1 for c in cases if c["expected_block"] and c["predicted_block"])
    fp = sum(1 for c in cases if not c["expected_block"] and c["predicted_block"])
    fn = sum(1 for c in cases if c["expected_block"] and not c["predicted_block"])
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    exact_match = sum(1 for c in cases if c["expected"] == c["predicted"])

    return {
        "mode": f"live_llm ({model})",
        "total": len(cases),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "exact_match_accuracy": round(exact_match / len(cases), 4) if cases else 0,
        "api_errors": api_errors,
        "elapsed_seconds": round(elapsed, 1),
        "errors": [c for c in cases if c["expected"] != c["predicted"]],
    }


async def _run_wg_heuristic(
    monkeypatch: pytest.MonkeyPatch,
    gold: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Run write guard heuristic-only (baseline)."""
    rows_by_query = {str(row["content"]): row for row in gold}

    monkeypatch.setenv("WRITE_GUARD_LLM_ENABLED", "false")
    client = _make_wg_eval_client(rows_by_query)
    cases = []
    try:
        for row in gold:
            decision = await client.write_guard(content=str(row["content"]), domain="core")
            predicted = str(decision.get("action", "")).upper()
            expected = str(row["expected_action"]).upper()
            cases.append({
                "id": row["id"],
                "expected": expected,
                "predicted": predicted,
                "expected_block": _is_block(expected),
                "predicted_block": _is_block(predicted),
            })
    finally:
        await client.close()

    tp = sum(1 for c in cases if c["expected_block"] and c["predicted_block"])
    fp = sum(1 for c in cases if not c["expected_block"] and c["predicted_block"])
    fn = sum(1 for c in cases if c["expected_block"] and not c["predicted_block"])
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    exact_match = sum(1 for c in cases if c["expected"] == c["predicted"])

    return {
        "mode": "heuristic",
        "total": len(cases),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "exact_match_accuracy": round(exact_match / len(cases), 4) if cases else 0,
        "errors": [c for c in cases if c["expected"] != c["predicted"]],
    }


# ---------------------------------------------------------------------------
# Main test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("LIVE_LLM_API_KEY"),
    reason="LIVE_LLM_API_KEY not set; skip live LLM benchmark",
)
async def test_quality_live_llm_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run all components against a real LLM endpoint, produce comparison report."""
    api_base = os.environ.get("LIVE_LLM_API_BASE", "https://api.edgefn.net/v1")
    api_key = os.environ["LIVE_LLM_API_KEY"]
    model = os.environ.get("LIVE_LLM_MODEL", "MiniMax-M2.5")

    intent_gold = _load_intent_gold()
    wg_gold = _load_wg_gold()

    # Baselines
    intent_keyword = await _run_intent_keyword(monkeypatch, intent_gold)
    wg_heuristic = await _run_wg_heuristic(monkeypatch, wg_gold)

    # Live LLM
    intent_live = await _run_intent_live(monkeypatch, intent_gold, api_base, api_key, model)
    wg_live = await _run_wg_live(monkeypatch, wg_gold, api_base, api_key, model)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "llm_model": model,
        "llm_api_base": api_base,
        "intent": {
            "keyword_only": intent_keyword,
            "live_llm": intent_live,
            "delta_accuracy": round(intent_live["accuracy"] - intent_keyword["accuracy"], 4),
        },
        "write_guard": {
            "heuristic_only": wg_heuristic,
            "live_llm": wg_live,
            "delta_precision": round(wg_live["precision"] - wg_heuristic["precision"], 4),
            "delta_recall": round(wg_live["recall"] - wg_heuristic["recall"], 4),
            "delta_exact_match": round(
                wg_live["exact_match_accuracy"] - wg_heuristic["exact_match_accuracy"], 4
            ),
        },
    }

    LIVE_REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # Markdown report
    lines = [
        "# Live LLM Quality Benchmark Report",
        "",
        f"> model: {model}",
        f"> generated: {report['generated_at_utc']}",
        "",
        "## Intent Classification",
        "",
        "| Mode | N | Accuracy | API Errors | Time (s) | Delta |",
        "|------|--:|--------:|-----------:|---------:|------:|",
        f"| keyword_scoring_v2 | {intent_keyword['total']} "
        f"| {intent_keyword['accuracy']:.3f} | - | - | baseline |",
        f"| Live LLM ({model}) | {intent_live['total']} "
        f"| {intent_live['accuracy']:.3f} | {intent_live['api_errors']} "
        f"| {intent_live['elapsed_seconds']} "
        f"| {'+' if report['intent']['delta_accuracy'] >= 0 else ''}"
        f"{report['intent']['delta_accuracy']:.3f} |",
        "",
        "### Per-intent breakdown (Live LLM)",
        "",
        "| Intent | N | Correct | Accuracy |",
        "|--------|--:|--------:|---------:|",
    ]
    for intent, stats in intent_live.get("by_intent", {}).items():
        lines.append(
            f"| {intent} | {stats['total']} | {stats['correct']} | {stats['accuracy']:.3f} |"
        )

    if intent_live["errors"]:
        lines.extend([
            "",
            f"### Errors (Live LLM): {len(intent_live['errors'])} of {intent_live['total']}",
            "",
        ])
        for e in intent_live["errors"][:15]:
            lines.append(
                f"- `{e['id']}`: \"{e['query'][:60]}\" "
                f"expected={e['expected']} got={e['predicted']}"
            )

    lines.extend([
        "",
        "## Write Guard",
        "",
        "| Mode | N | Precision | Recall | Exact Match | API Errors | Time (s) |",
        "|------|--:|----------:|-------:|------------:|-----------:|---------:|",
        f"| heuristic | {wg_heuristic['total']} "
        f"| {wg_heuristic['precision']:.3f} | {wg_heuristic['recall']:.3f} "
        f"| {wg_heuristic['exact_match_accuracy']:.3f} | - | - |",
        f"| Live LLM ({model}) | {wg_live['total']} "
        f"| {wg_live['precision']:.3f} | {wg_live['recall']:.3f} "
        f"| {wg_live['exact_match_accuracy']:.3f} | {wg_live['api_errors']} "
        f"| {wg_live['elapsed_seconds']} |",
        "",
    ])

    if wg_live["errors"]:
        lines.append(
            f"### Errors (Live LLM): {len(wg_live['errors'])} of {wg_live['total']}"
        )
        lines.append("")
        for e in wg_live["errors"][:15]:
            lines.append(f"- `{e['id']}`: expected={e['expected']} got={e['predicted']}")

    lines.extend([
        "",
        "## Summary",
        "",
        "| Component | Heuristic | Live LLM | Delta |",
        "|-----------|----------:|---------:|------:|",
        f"| Intent Accuracy | {intent_keyword['accuracy']:.3f} "
        f"| {intent_live['accuracy']:.3f} "
        f"| {'+' if report['intent']['delta_accuracy'] >= 0 else ''}"
        f"{report['intent']['delta_accuracy']:.3f} |",
        f"| WG Precision | {wg_heuristic['precision']:.3f} "
        f"| {wg_live['precision']:.3f} "
        f"| {'+' if report['write_guard']['delta_precision'] >= 0 else ''}"
        f"{report['write_guard']['delta_precision']:.3f} |",
        f"| WG Recall | {wg_heuristic['recall']:.3f} "
        f"| {wg_live['recall']:.3f} "
        f"| {'+' if report['write_guard']['delta_recall'] >= 0 else ''}"
        f"{report['write_guard']['delta_recall']:.3f} |",
        f"| WG Exact Match | {wg_heuristic['exact_match_accuracy']:.3f} "
        f"| {wg_live['exact_match_accuracy']:.3f} "
        f"| {'+' if report['write_guard']['delta_exact_match'] >= 0 else ''}"
        f"{report['write_guard']['delta_exact_match']:.3f} |",
        "",
    ])

    LIVE_REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Assertions: tolerant thresholds for live API calls.
    # A real LLM may not beat a gold-set-tuned keyword classifier; the value
    # of this test is measuring real performance, not asserting superiority.
    max_error_rate = 0.10  # allow up to 10% API errors (rate limits, timeouts)
    intent_error_rate = intent_live["api_errors"] / max(intent_live["total"], 1)
    wg_error_rate = wg_live["api_errors"] / max(wg_live["total"], 1)
    assert intent_error_rate <= max_error_rate, (
        f"Intent: {intent_live['api_errors']} API errors "
        f"({intent_error_rate:.1%}) > {max_error_rate:.0%} threshold"
    )
    assert wg_error_rate <= max_error_rate, (
        f"Write Guard: {wg_live['api_errors']} API errors "
        f"({wg_error_rate:.1%}) > {max_error_rate:.0%} threshold"
    )
    # Live LLM should achieve reasonable accuracy (not necessarily better than tuned heuristic)
    assert intent_live["accuracy"] >= 0.70, (
        f"Live LLM intent accuracy {intent_live['accuracy']:.3f} below 0.70 floor"
    )
    assert wg_live["exact_match_accuracy"] >= 0.70, (
        f"Live LLM WG exact match {wg_live['exact_match_accuracy']:.3f} below 0.70 floor"
    )

    # Report files exist
    assert LIVE_REPORT_JSON.exists()
    assert LIVE_REPORT_MD.exists()
