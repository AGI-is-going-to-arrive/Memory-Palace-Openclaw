"""Write Guard contradiction detection benchmark.

Tests the LLM-enhanced write guard's ability to detect when new content
contradicts an existing memory, using a mock LLM that returns deterministic
contradiction decisions based on the gold set.
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
CONTRADICTION_GOLD_SET = FIXTURES_DIR / "write_guard_contradiction_gold_set.jsonl"
CONTRADICTION_JSON_ARTIFACT = BENCHMARK_DIR / "write_guard_contradiction_metrics.json"
CONTRADICTION_MD_ARTIFACT = BENCHMARK_DIR / "write_guard_contradiction_metrics.md"
_SQLITE_MEMORY_URL = "sqlite+aiosqlite:///:memory:"


def _load_gold() -> List[Dict[str, Any]]:
    rows = []
    for idx, line in enumerate(CONTRADICTION_GOLD_SET.read_text(encoding="utf-8").splitlines(), 1):
        raw = line.strip()
        if not raw:
            continue
        row = json.loads(raw)
        row["case_index"] = idx
        rows.append(row)
    assert len(rows) >= 20, f"need at least 20 cases, got {len(rows)}"
    return rows


def _make_contradiction_llm_mock(gold: List[Dict[str, Any]], accuracy: float = 0.95):
    """Mock LLM that returns contradiction decisions based on gold set."""
    import hashlib

    # Build content -> gold mapping
    content_map = {str(row["content"]): row for row in gold}

    async def _fake_post_json(
        base: str,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str = "",
    ) -> Optional[Dict[str, Any]]:
        messages = payload.get("messages", [])
        content = ""
        for msg in messages:
            if msg.get("role") == "user":
                raw = msg.get("content", "")
                # Extract content between "New content:" and "Candidate memories:"
                if "New content:" in raw and "Candidate memories:" in raw:
                    content = raw.split("New content:")[1].split("Candidate memories:")[0].strip()
                break

        gold_row = content_map.get(content)
        if gold_row is None:
            # Try partial match
            for key, row in content_map.items():
                if key in content or content in key:
                    gold_row = row
                    break

        if gold_row is None:
            return {
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "action": "ADD",
                            "target_id": None,
                            "reason": "no match found",
                            "method": "llm",
                            "contradiction": False,
                        })
                    }
                }]
            }

        expected_contradiction = bool(gold_row.get("expected_contradiction"))

        # Simulate imperfect LLM: sometimes wrong based on hash
        h = int(hashlib.md5(content.encode()).hexdigest()[:8], 16)
        contradiction = expected_contradiction
        if (h % 100) / 100.0 >= accuracy:
            contradiction = not expected_contradiction

        action = "UPDATE" if contradiction else "ADD"

        return {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "action": action,
                        "target_id": gold_row.get("case_index", 1),
                        "reason": f"contradiction={'yes' if contradiction else 'no'}: {gold_row.get('content', '')[:50]}",
                        "method": "llm",
                        "contradiction": contradiction,
                    })
                }
            }]
        }

    return _fake_post_json


class _ContradictionEvalClient(SQLiteClient):
    """SQLiteClient that stubs search to return the existing snippet as a candidate."""

    def __init__(self, gold: List[Dict[str, Any]]):
        super().__init__(_SQLITE_MEMORY_URL)
        self._gold = {str(row["content"]): row for row in gold}

    async def search_advanced(self, *, query, mode, max_results, candidate_multiplier, filters):
        row = self._gold.get(query)
        if row is None:
            return {"results": [], "degrade_reasons": []}
        # Use scores below heuristic thresholds so the write guard falls through
        # to the LLM path, which is where contradiction detection happens.
        return {
            "results": [{
                "memory_id": row["case_index"],
                "uri": f"core://guard/contradiction/{row['id']}",
                "snippet": str(row.get("existing_snippet", "")),
                "scores": {"vector": 0.45, "text": 0.35, "final": 0.42},
            }],
            "degrade_reasons": [],
        }


@pytest.mark.asyncio
async def test_write_guard_contradiction_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    gold = _load_gold()

    # Set up LLM-enabled write guard
    monkeypatch.setenv("WRITE_GUARD_LLM_ENABLED", "true")
    monkeypatch.setenv("WRITE_GUARD_LLM_API_BASE", "http://fake.wg/v1")
    monkeypatch.setenv("WRITE_GUARD_LLM_MODEL", "fake-wg-model")

    client = _ContradictionEvalClient(gold)
    monkeypatch.setattr(client, "_post_json", _make_contradiction_llm_mock(gold, accuracy=0.95))

    cases = []
    try:
        for row in gold:
            decision = await client.write_guard(content=str(row["content"]), domain="core")
            detected = bool(decision.get("contradiction", False))
            expected = bool(row["expected_contradiction"])
            cases.append({
                "id": row["id"],
                "content": str(row["content"])[:60],
                "expected_contradiction": expected,
                "detected_contradiction": detected,
                "correct": detected == expected,
                "action": decision.get("action"),
            })
    finally:
        await client.close()

    total = len(cases)
    correct = sum(1 for c in cases if c["correct"])
    accuracy = correct / total if total > 0 else 0.0
    tp = sum(1 for c in cases if c["expected_contradiction"] and c["detected_contradiction"])
    fp = sum(1 for c in cases if not c["expected_contradiction"] and c["detected_contradiction"])
    fn = sum(1 for c in cases if c["expected_contradiction"] and not c["detected_contradiction"])
    tn = sum(1 for c in cases if not c["expected_contradiction"] and not c["detected_contradiction"])
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0

    payload = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "total_cases": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "contradiction_precision": round(precision, 4),
        "contradiction_recall": round(recall, 4),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "errors": [c for c in cases if not c["correct"]],
    }

    CONTRADICTION_JSON_ARTIFACT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# Write Guard Contradiction Detection",
        "",
        f"> generated: {payload['generated_at_utc']}",
        f"> N={total}, accuracy={accuracy:.3f}, precision={precision:.3f}, recall={recall:.3f}",
        "",
        f"| Metric | Value |",
        f"|--------|------:|",
        f"| Accuracy | {accuracy:.3f} |",
        f"| Precision | {precision:.3f} |",
        f"| Recall | {recall:.3f} |",
        f"| TP | {tp} |",
        f"| FP | {fp} |",
        f"| FN | {fn} |",
        f"| TN | {tn} |",
    ]
    if payload["errors"]:
        lines.append("")
        lines.append(f"## Errors ({len(payload['errors'])})")
        for e in payload["errors"]:
            lines.append(f"- `{e['id']}`: expected={e['expected_contradiction']} got={e['detected_contradiction']} \"{e['content']}\"")

    CONTRADICTION_MD_ARTIFACT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Assertions
    assert accuracy >= 0.85, f"contradiction accuracy {accuracy:.3f} < 0.85"
    assert precision >= 0.80, f"contradiction precision {precision:.3f} < 0.80"
    assert recall >= 0.80, f"contradiction recall {recall:.3f} < 0.80"
    assert CONTRADICTION_JSON_ARTIFACT.exists()
