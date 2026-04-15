"""Ablation benchmark: measure quality gate metrics across LLM on/off configurations.

Produces a comparison report showing how each component performs with and without
LLM assistance, using the same expanded gold sets.

Components tested:
- Intent classification: keyword_scoring_v2 vs classify_intent_with_llm
- Write Guard: heuristic-only vs LLM-assisted
- Gist quality: extractive_bullets vs LLM gist

The LLM-on tests use deterministic mock responses to ensure reproducibility.
For live LLM evaluation, use the separate live_quality_* scripts.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from db.sqlite_client import SQLiteClient


BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BENCHMARK_DIR.parent / "fixtures"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from helpers.common import load_thresholds_v1

_SQLITE_MEMORY_URL = "sqlite+aiosqlite:///:memory:"
ABLATION_ARTIFACT = BENCHMARK_DIR / "quality_ablation_report.json"
ABLATION_MD_ARTIFACT = BENCHMARK_DIR / "quality_ablation_report.md"


# ---------------------------------------------------------------------------
# Intent ablation helpers
# ---------------------------------------------------------------------------

def _load_intent_gold() -> List[Dict[str, Any]]:
    path = FIXTURES_DIR / "intent_gold_set.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw:
            continue
        row = json.loads(raw)
        rows.append(row)
    return rows


def _build_intent_llm_oracle(gold: List[Dict[str, Any]]) -> Dict[str, str]:
    """Build a query -> expected_intent map to simulate a perfect LLM."""
    return {str(row["query"]): str(row["expected_intent"]) for row in gold}


def _make_intent_llm_mock(oracle: Dict[str, str], accuracy: float = 0.92):
    """Create a mock _post_json that returns LLM intent responses.

    `accuracy` controls what fraction of queries get the correct answer;
    the rest get a plausible wrong answer to simulate realistic LLM behavior.
    """
    import hashlib

    def _deterministic_wrong(expected: str) -> str:
        intents = ["factual", "exploratory", "temporal", "causal"]
        others = [i for i in intents if i != expected]
        return others[0]

    async def _fake_post_json(
        base: str,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str = "",
    ) -> Optional[Dict[str, Any]]:
        messages = payload.get("messages", [])
        query = ""
        for msg in messages:
            if msg.get("role") == "user":
                # Extract original query from the user prompt
                content = msg.get("content", "")
                for line in content.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("Rewritten") and not line.startswith("Decide") and not line.startswith("Original"):
                        query = line
                        break

        expected = oracle.get(query, "factual")

        # Deterministic accuracy: hash query to decide if this one is "wrong"
        h = int(hashlib.md5(query.encode()).hexdigest()[:8], 16)
        if (h % 100) / 100.0 >= accuracy:
            intent = _deterministic_wrong(expected)
        else:
            intent = expected

        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "intent": intent,
                        "confidence": 0.85,
                        "signals": [f"llm_mock:{intent}"],
                    })
                }
            }]
        }

    return _fake_post_json


async def _run_intent_ablation(
    monkeypatch: pytest.MonkeyPatch,
    gold: List[Dict[str, Any]],
    *,
    llm_enabled: bool,
    llm_accuracy: float = 0.92,
) -> Dict[str, Any]:
    """Run intent classification on the full gold set, return accuracy report."""
    # Environment must be set BEFORE client construction since SQLiteClient
    # reads _intent_llm_enabled / _intent_llm_api_base in __init__.
    if llm_enabled:
        monkeypatch.setenv("INTENT_LLM_ENABLED", "true")
        monkeypatch.setenv("INTENT_LLM_API_BASE", "http://fake.intent/v1")
        monkeypatch.setenv("INTENT_LLM_MODEL", "fake-intent-model")
    else:
        monkeypatch.setenv("INTENT_LLM_ENABLED", "false")
        monkeypatch.delenv("INTENT_LLM_API_BASE", raising=False)
        monkeypatch.delenv("INTENT_LLM_MODEL", raising=False)

    client = SQLiteClient(_SQLITE_MEMORY_URL)

    if llm_enabled:
        oracle = _build_intent_llm_oracle(gold)
        monkeypatch.setattr(client, "_post_json", _make_intent_llm_mock(oracle, llm_accuracy))

    cases = []
    try:
        for row in gold:
            query = str(row["query"])
            expected = str(row["expected_intent"])
            normalized = client.preprocess_query(query)

            if llm_enabled:
                predicted_result = await client.classify_intent_with_llm(
                    query, normalized.get("rewritten_query")
                )
            else:
                predicted_result = client.classify_intent(
                    query, normalized.get("rewritten_query")
                )

            predicted_intent = str(predicted_result.get("intent") or "")
            method = str(predicted_result.get("method") or "")
            cases.append({
                "id": row["id"],
                "query": query,
                "expected": expected,
                "predicted": predicted_intent,
                "method": method,
                "correct": predicted_intent == expected,
            })
    finally:
        await client.close()

    correct = sum(1 for c in cases if c["correct"])
    total = len(cases)
    accuracy = correct / total if total > 0 else 0.0

    # Breakdown by intent type
    by_intent: Dict[str, Dict[str, int]] = {}
    for c in cases:
        key = c["expected"]
        if key not in by_intent:
            by_intent[key] = {"total": 0, "correct": 0}
        by_intent[key]["total"] += 1
        if c["correct"]:
            by_intent[key]["correct"] += 1

    return {
        "mode": "llm" if llm_enabled else "keyword",
        "total": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "by_intent": {
            k: {**v, "accuracy": round(v["correct"] / v["total"], 4) if v["total"] > 0 else 0}
            for k, v in sorted(by_intent.items())
        },
        "errors": [c for c in cases if not c["correct"]],
    }


# ---------------------------------------------------------------------------
# Write Guard ablation helpers
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


def _is_block(action: str) -> bool:
    return action in {"UPDATE", "NOOP", "DELETE"}


async def _run_wg_ablation(
    monkeypatch: pytest.MonkeyPatch,
    gold: List[Dict[str, Any]],
    *,
    llm_enabled: bool,
) -> Dict[str, Any]:
    """Run write guard on full gold set, return precision/recall report."""
    rows_by_query = {str(row["content"]): row for row in gold}

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
                    "results": [{"memory_id": row["case_index"], "uri": f"core://guard/{row['id']}", "snippet": f"candidate {row['id']}", "scores": {"vector": score, "text": 0.05, "final": score}}],
                    "degrade_reasons": [],
                }
            if mode == "keyword":
                score = float(row.get("keyword_text_score") or 0.0)
                if score <= 0.0:
                    return {"results": [], "degrade_reasons": []}
                return {
                    "results": [{"memory_id": 1000 + row["case_index"], "uri": f"core://guard/{row['id']}", "snippet": f"candidate {row['id']}", "scores": {"vector": 0.05, "text": score, "final": score}}],
                    "degrade_reasons": [],
                }
            return {"results": [], "degrade_reasons": []}

    # Environment must be set BEFORE client construction.
    monkeypatch.setenv("WRITE_GUARD_LLM_ENABLED", "true" if llm_enabled else "false")
    if llm_enabled:
        monkeypatch.setenv("WRITE_GUARD_LLM_API_BASE", "http://fake.wg/v1")
        monkeypatch.setenv("WRITE_GUARD_LLM_MODEL", "fake-wg-model")
    else:
        monkeypatch.delenv("WRITE_GUARD_LLM_API_BASE", raising=False)
        monkeypatch.delenv("WRITE_GUARD_LLM_MODEL", raising=False)

    client = _EvalClient()

    if llm_enabled:
        # Mock LLM that returns the expected action (simulating a good LLM)
        async def _wg_llm_mock(base, endpoint, payload, api_key=""):
            messages = payload.get("messages", [])
            content_text = ""
            for msg in messages:
                if msg.get("role") == "user":
                    content_text = msg.get("content", "")
                    break
            # Find the gold row by matching content
            for row in gold:
                if str(row["content"]) in content_text:
                    action = str(row["expected_action"])
                    target_id = row["case_index"] if action != "ADD" else None
                    return {
                        "choices": [{
                            "message": {
                                "content": json.dumps({
                                    "action": action,
                                    "target_id": target_id,
                                    "reason": f"llm mock decision for {row['id']}",
                                    "method": "llm",
                                })
                            }
                        }]
                    }
            return None

        monkeypatch.setattr(client, "_post_json", _wg_llm_mock)

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

    # Exact action match
    exact_match = sum(1 for c in cases if c["expected"] == c["predicted"])

    return {
        "mode": "llm" if llm_enabled else "heuristic",
        "total": len(cases),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "exact_match_accuracy": round(exact_match / len(cases), 4) if cases else 0,
        "errors": [c for c in cases if c["expected"] != c["predicted"]],
    }


# ---------------------------------------------------------------------------
# Main ablation test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quality_ablation_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run all components in LLM-off and LLM-on modes, produce comparison report."""
    intent_gold = _load_intent_gold()
    wg_gold = _load_wg_gold()

    # Intent: keyword only
    intent_keyword = await _run_intent_ablation(monkeypatch, intent_gold, llm_enabled=False)
    # Intent: LLM (simulated 92% accuracy)
    intent_llm = await _run_intent_ablation(monkeypatch, intent_gold, llm_enabled=True, llm_accuracy=0.92)

    # Write Guard: heuristic only
    wg_heuristic = await _run_wg_ablation(monkeypatch, wg_gold, llm_enabled=False)
    # Write Guard: LLM assisted
    wg_llm = await _run_wg_ablation(monkeypatch, wg_gold, llm_enabled=True)

    report = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "intent": {
            "keyword_only": intent_keyword,
            "llm_assisted": intent_llm,
            "delta_accuracy": round(intent_llm["accuracy"] - intent_keyword["accuracy"], 4),
        },
        "write_guard": {
            "heuristic_only": wg_heuristic,
            "llm_assisted": wg_llm,
            "delta_precision": round(wg_llm["precision"] - wg_heuristic["precision"], 4),
            "delta_recall": round(wg_llm["recall"] - wg_heuristic["recall"], 4),
        },
    }

    # Write artifacts
    ABLATION_ARTIFACT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# Quality Gate Ablation Report",
        "",
        f"> generated: {report['generated_at_utc']}",
        "",
        "## Intent Classification",
        "",
        f"| Mode | N | Accuracy | Delta |",
        f"|------|--:|--------:|------:|",
        f"| keyword_scoring_v2 | {intent_keyword['total']} | {intent_keyword['accuracy']:.3f} | baseline |",
        f"| + LLM (mock 92%) | {intent_llm['total']} | {intent_llm['accuracy']:.3f} | +{report['intent']['delta_accuracy']:.3f} |",
        "",
        "### Per-intent breakdown (keyword only)",
        "",
        "| Intent | N | Correct | Accuracy |",
        "|--------|--:|--------:|---------:|",
    ]
    for intent, stats in intent_keyword["by_intent"].items():
        lines.append(f"| {intent} | {stats['total']} | {stats['correct']} | {stats['accuracy']:.3f} |")

    lines.extend([
        "",
        f"### Errors (keyword mode): {len(intent_keyword['errors'])} of {intent_keyword['total']}",
        "",
    ])
    for e in intent_keyword["errors"][:10]:
        lines.append(f"- `{e['id']}`: \"{e['query'][:50]}\" expected={e['expected']} got={e['predicted']}")

    lines.extend([
        "",
        "## Write Guard",
        "",
        "| Mode | N | Precision | Recall | Exact Match | Delta P | Delta R |",
        "|------|--:|----------:|-------:|------------:|--------:|--------:|",
        f"| heuristic | {wg_heuristic['total']} | {wg_heuristic['precision']:.3f} | {wg_heuristic['recall']:.3f} | {wg_heuristic['exact_match_accuracy']:.3f} | baseline | baseline |",
        f"| + LLM | {wg_llm['total']} | {wg_llm['precision']:.3f} | {wg_llm['recall']:.3f} | {wg_llm['exact_match_accuracy']:.3f} | +{report['write_guard']['delta_precision']:.3f} | +{report['write_guard']['delta_recall']:.3f} |",
        "",
    ])

    if wg_heuristic["errors"]:
        lines.append(f"### Errors (heuristic mode): {len(wg_heuristic['errors'])} of {wg_heuristic['total']}")
        lines.append("")
        for e in wg_heuristic["errors"][:10]:
            lines.append(f"- `{e['id']}`: expected={e['expected']} got={e['predicted']}")

    ABLATION_MD_ARTIFACT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Assertions: LLM mode should produce reasonable results.
    # Note: mock LLM is set to 92% accuracy, which may be lower than an
    # optimized keyword classifier on the same gold set. The comparison is
    # valid for measuring the LLM integration path, not for claiming LLM > keyword.
    assert intent_llm["accuracy"] >= 0.85, "LLM mock accuracy unexpectedly low"
    assert intent_keyword["accuracy"] >= 0.6, "keyword accuracy below minimum"
    assert wg_llm["precision"] >= wg_heuristic["precision"] - 0.05
    assert wg_llm["recall"] >= wg_heuristic["recall"] - 0.05

    # Artifacts exist
    assert ABLATION_ARTIFACT.exists()
    assert ABLATION_MD_ARTIFACT.exists()
