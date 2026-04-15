"""Focused write guard verification for experimental scoring.

Runs write_guard directly on gold set cases for B-off and C-off profiles.
Much faster than full ablation (no intent/gist, direct write_guard calls).

Usage:
    RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_EMBEDDING_API_KEY=... \
    backend/.venv/bin/python backend/tests/benchmark/run_write_guard_verify.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BENCHMARK_DIR.parent / "fixtures"
BACKEND_ROOT = BENCHMARK_DIR.parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db.sqlite_client import SQLiteClient


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


def compute_metrics(expected_actions, predicted_actions):
    labels = ["ADD", "UPDATE", "NOOP"]
    cm = {exp: {pred: 0 for pred in labels} for exp in labels}
    for exp, pred in zip(expected_actions, predicted_actions):
        if exp in cm and pred in cm[exp]:
            cm[exp][pred] += 1

    def _is_block(a):
        return a in {"UPDATE", "NOOP", "DELETE"}

    tp = sum(1 for e, p in zip(expected_actions, predicted_actions)
             if _is_block(e) and _is_block(p))
    fp = sum(1 for e, p in zip(expected_actions, predicted_actions)
             if not _is_block(e) and _is_block(p))
    fn = sum(1 for e, p in zip(expected_actions, predicted_actions)
             if _is_block(e) and not _is_block(p))

    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) > 0 else 0.0
    em = sum(1 for e, p in zip(expected_actions, predicted_actions) if e == p) / len(expected_actions)

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "exact_match": round(em, 4),
        "tp": tp, "fp": fp, "fn": fn,
        "confusion_matrix": cm,
    }


async def run_cell(cell_name: str, gold: List[Dict[str, Any]], env_overrides: Dict[str, str]):
    """Run write_guard for each gold case under given env config."""
    # Apply env overrides
    saved = {}
    for k, v in env_overrides.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix=f"verify_{cell_name}_")
    os.close(fd)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    client = SQLiteClient(db_url)
    await client.init_db()

    # Seed
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

    print(f"  [{cell_name}] Seeding {len(unique_existing)} memories...", flush=True)
    await _seed_memories(client, unique_existing)
    try:
        await client.rebuild_index()
    except Exception:
        pass

    print(f"  [{cell_name}] Running write_guard for {len(gold)} cases...", flush=True)
    expected = []
    predicted = []
    errors = []

    for i, row in enumerate(gold):
        content = str(row["content"])
        exp = str(row["expected_action"]).upper()
        expected.append(exp)

        try:
            result = await client.write_guard(content=content, domain="core")
            pred = str(result.get("action", "ADD")).upper()
        except Exception as exc:
            pred = "ADD"
            errors.append({"id": row["id"], "error": str(exc)})

        predicted.append(pred)

        if (i + 1) % 50 == 0:
            print(f"  [{cell_name}] Progress: {i + 1}/{len(gold)}", flush=True)

    await client.close()
    try:
        os.unlink(db_path)
    except Exception:
        pass

    # Restore env
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    metrics = compute_metrics(expected, predicted)
    metrics["errors"] = errors
    metrics["cell"] = cell_name
    return metrics


def print_cell_result(m: Dict[str, Any]):
    print(f"\n  [{m['cell']}] P={m['precision']:.3f} R={m['recall']:.3f} "
          f"F1={m['f1']:.3f} EM={m['exact_match']:.3f} "
          f"(TP={m['tp']} FP={m['fp']} FN={m['fn']})")
    cm = m["confusion_matrix"]
    print(f"  Confusion: Expected\\Predicted  ADD    UPDATE NOOP")
    for exp in ["ADD", "UPDATE", "NOOP"]:
        print(f"    {exp:>6}                    "
              f"{cm[exp]['ADD']:>4}   {cm[exp]['UPDATE']:>4}   {cm[exp]['NOOP']:>4}")


async def main():
    gold = _load_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl")
    assert len(gold) >= 100

    # Common env
    common_env = {
        "INTENT_LLM_ENABLED": "false",
        "WRITE_GUARD_LLM_ENABLED": "false",
        "COMPACT_GIST_LLM_ENABLED": "false",
    }

    # B-off: hash embedding, no reranker, no LLM
    b_off_env = {
        **common_env,
        "RETRIEVAL_EMBEDDING_BACKEND": "hash",
        "RETRIEVAL_RERANKER_ENABLED": "false",
        "WRITE_GUARD_SCORE_NORMALIZATION": os.environ.get(
            "WRITE_GUARD_SCORE_NORMALIZATION", "false"),
        "WRITE_GUARD_CROSS_CHECK_ADD_FLOOR": os.environ.get(
            "WRITE_GUARD_CROSS_CHECK_ADD_FLOOR", "0.10"),
    }

    # C-off: api embedding + reranker, no LLM
    c_off_env = {
        **common_env,
        "RETRIEVAL_EMBEDDING_BACKEND": "api",
        "RETRIEVAL_RERANKER_ENABLED": "true",
        "RETRIEVAL_RERANKER_WEIGHT": "0.30",
        "WRITE_GUARD_SCORE_NORMALIZATION": os.environ.get(
            "WRITE_GUARD_SCORE_NORMALIZATION", "false"),
        "WRITE_GUARD_CROSS_CHECK_ADD_FLOOR": os.environ.get(
            "WRITE_GUARD_CROSS_CHECK_ADD_FLOOR", "0.10"),
    }

    print("=" * 60)
    print("Write Guard Verification")
    print(f"  WRITE_GUARD_SCORE_NORMALIZATION = "
          f"{os.environ.get('WRITE_GUARD_SCORE_NORMALIZATION', 'false')}")
    print(f"  WRITE_GUARD_CROSS_CHECK_ADD_FLOOR = "
          f"{os.environ.get('WRITE_GUARD_CROSS_CHECK_ADD_FLOOR', '0.10')}")
    print("=" * 60)

    # Run B-off
    print("\n[B-off] Hash embedding, no reranker, no LLM")
    b_off = await run_cell("B-off", gold, b_off_env)
    print_cell_result(b_off)

    # Run C-off
    print("\n[C-off] API embedding + reranker, no LLM")
    c_off = await run_cell("C-off", gold, c_off_env)
    print_cell_result(c_off)

    # Non-regression check
    print("\n" + "=" * 60)
    print("NON-REGRESSION CHECK")
    print("=" * 60)

    b_baseline = {"precision": 0.977, "recall": 0.962, "exact_match": 0.960}
    c_baseline = {"precision": 0.650, "recall": 1.000, "exact_match": 0.370}

    b_pass = (b_off["precision"] >= b_baseline["precision"] - 0.005
              and b_off["recall"] >= b_baseline["recall"] - 0.005
              and b_off["exact_match"] >= b_baseline["exact_match"] - 0.005)

    c_improved = (c_off["exact_match"] > c_baseline["exact_match"]
                  and not (c_off["precision"] < c_baseline["precision"] - 0.05))

    print(f"  B-off: {'PASS' if b_pass else 'FAIL'} "
          f"(P={b_off['precision']:.3f} vs {b_baseline['precision']:.3f}, "
          f"R={b_off['recall']:.3f} vs {b_baseline['recall']:.3f}, "
          f"EM={b_off['exact_match']:.3f} vs {b_baseline['exact_match']:.3f})")
    print(f"  C-off: {'IMPROVED' if c_improved else 'NO IMPROVEMENT'} "
          f"(P={c_off['precision']:.3f} vs {c_baseline['precision']:.3f}, "
          f"R={c_off['recall']:.3f} vs {c_baseline['recall']:.3f}, "
          f"EM={c_off['exact_match']:.3f} vs {c_baseline['exact_match']:.3f})")

    overall = "PASS" if b_pass and c_improved else "FAIL"
    print(f"\n  OVERALL: {overall}")

    # Save results
    report = {
        "B-off": b_off,
        "C-off": c_off,
        "non_regression": {
            "b_pass": b_pass,
            "c_improved": c_improved,
            "overall": overall,
        }
    }
    report_path = BENCHMARK_DIR / "write_guard_verify_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n  Report saved to {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
