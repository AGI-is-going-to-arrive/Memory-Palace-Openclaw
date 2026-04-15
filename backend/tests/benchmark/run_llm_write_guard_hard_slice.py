"""LLM Write Guard hard-slice benchmark.

Tests the LLM write guard on cases that heuristic scoring can't distinguish:
- 29 UPDATE→ADD hard cases (heuristic misclassifies as ADD)
- 15 control ADD, 15 control UPDATE, 15 control NOOP

Runs in two modes:
1. Current LLM write guard (D-on baseline)
2. Diff-aware LLM write guard (experimental)

Usage:
    WRITE_GUARD_LLM_ENABLED=true \
    WRITE_GUARD_LLM_API_BASE=... WRITE_GUARD_LLM_API_KEY=... WRITE_GUARD_LLM_MODEL=... \
    RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_EMBEDDING_API_KEY=... \
    backend/.venv/bin/python backend/tests/benchmark/run_llm_write_guard_hard_slice.py
"""
from __future__ import annotations
import asyncio, json, os, sys, tempfile, random
from pathlib import Path
from typing import Any, Dict, List, Optional

BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BENCHMARK_DIR.parent / "fixtures"
BACKEND_ROOT = BENCHMARK_DIR.parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
from db.sqlite_client import SQLiteClient


def _load_jsonl(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if raw:
            rows.append(json.loads(raw))
    return rows


# Hard-slice case IDs (29 UPDATE→ADD errors from v3 heuristic)
HARD_IDS = {
    'wg-p-001', 'wg-p-003', 'wg-p-027', 'wg-p-029', 'wg-p-035', 'wg-p-038',
    'wg-p-041', 'wg-p-049', 'wg-p-062', 'wg-p-074', 'wg-p-075', 'wg-p-079',
    'wg-p-093', 'wg-p-101', 'wg-p-102', 'wg-p-105', 'wg-p-106', 'wg-p-116',
    'wg-p-128', 'wg-p-135', 'wg-p-136', 'wg-p-150', 'wg-p-154', 'wg-p-155',
    'wg-p-156', 'wg-p-158', 'wg-p-166', 'wg-p-167', 'wg-p-172',
}
# Control IDs (sampled from correct classifications, seed=42)
CTRL_ADD_IDS = {'wg-p-028', 'wg-p-011', 'wg-p-091', 'wg-p-073', 'wg-p-069',
                'wg-p-098', 'wg-p-119', 'wg-p-058', 'wg-p-177', 'wg-p-164',
                'wg-p-114', 'wg-p-088', 'wg-p-169', 'wg-p-143', 'wg-p-081'}
CTRL_UPD_IDS = {'wg-p-020', 'wg-p-016', 'wg-p-037', 'wg-p-060', 'wg-p-065',
                'wg-p-076', 'wg-p-067', 'wg-p-033', 'wg-p-070', 'wg-p-005',
                'wg-p-063', 'wg-p-071', 'wg-p-022', 'wg-p-046', 'wg-p-064'}
CTRL_NOOP_IDS = {'wg-p-163', 'wg-p-173', 'wg-p-051', 'wg-p-145', 'wg-p-109',
                 'wg-p-012', 'wg-p-121', 'wg-p-055', 'wg-p-103', 'wg-p-131',
                 'wg-p-159', 'wg-p-107', 'wg-p-147', 'wg-p-161', 'wg-p-113'}

HARD_SLICE_IDS = HARD_IDS | CTRL_ADD_IDS | CTRL_UPD_IDS | CTRL_NOOP_IDS


async def _ensure_parent_chain(client, domain, full_path):
    segments = full_path.split("/")
    for depth in range(1, len(segments)):
        ancestor_parent = "/".join(segments[:depth - 1])
        ancestor_title = segments[depth - 1]
        try:
            await client.create_memory(parent_path=ancestor_parent, content="(ancestor placeholder)",
                                       priority=100, title=ancestor_title, domain=domain, index_now=False)
        except Exception:
            pass


async def _seed_memories(client, memories):
    for mem in memories:
        uri = mem.get("uri", "core://test/default")
        content = mem.get("content", "")
        domain = mem.get("domain", "core")
        parts = uri.split("://", 1)
        if len(parts) == 2: domain, full_path = parts
        else: full_path = uri
        path_segments = full_path.rsplit("/", 1)
        if len(path_segments) == 2: parent_path, title = path_segments
        else: parent_path, title = "", path_segments[0]
        try:
            await _ensure_parent_chain(client, domain, full_path)
            await client.create_memory(parent_path=parent_path, content=content,
                                       priority=10, title=title, domain=domain)
        except Exception:
            pass


def compute_metrics(expected, predicted):
    labels = ["ADD", "UPDATE", "NOOP"]
    cm = {e: {p: 0 for p in labels} for e in labels}
    for e, p in zip(expected, predicted):
        if e in cm and p in cm[e]:
            cm[e][p] += 1
    tp = sum(1 for e, p in zip(expected, predicted) if e in {"UPDATE","NOOP"} and p in {"UPDATE","NOOP"})
    fp = sum(1 for e, p in zip(expected, predicted) if e == "ADD" and p in {"UPDATE","NOOP"})
    fn = sum(1 for e, p in zip(expected, predicted) if e in {"UPDATE","NOOP"} and p == "ADD")
    P = tp/(tp+fp) if (tp+fp) else 1.0
    R = tp/(tp+fn) if (tp+fn) else 1.0
    F1 = 2*P*R/(P+R) if (P+R) else 0.0
    EM = sum(1 for e, p in zip(expected, predicted) if e == p) / len(expected) if expected else 0
    return {"P": round(P,4), "R": round(R,4), "F1": round(F1,4), "EM": round(EM,4),
            "cm": cm, "tp": tp, "fp": fp, "fn": fn}


async def run_pass(client, gold, label, use_llm=False):
    expected, predicted, details = [], [], []
    for i, row in enumerate(gold):
        content = str(row["content"])
        exp = str(row["expected_action"]).upper()
        expected.append(exp)
        try:
            result = await client.write_guard(content=content, domain="core")
            pred = str(result.get("action", "ADD")).upper()
            method = str(result.get("method", ""))
        except Exception as exc:
            pred = "ADD"
            method = f"error:{type(exc).__name__}"
        predicted.append(pred)
        details.append({"id": row["id"], "expected": exp, "predicted": pred,
                         "method": method, "hard": row["id"] in HARD_IDS})
        if (i + 1) % 20 == 0:
            print(f"    [{label}] {i+1}/{len(gold)}", flush=True)
    return expected, predicted, details


def print_results(label, m, details, hard_ids):
    print(f"\n  [{label}]")
    print(f"  P={m['P']:.3f} R={m['R']:.3f} F1={m['F1']:.3f} EM={m['EM']:.3f}")
    cm = m["cm"]
    print(f"  Expected\\Pred   ADD  UPDATE  NOOP")
    for e in ["ADD", "UPDATE", "NOOP"]:
        print(f"    {e:>6}       {cm[e]['ADD']:>4}  {cm[e]['UPDATE']:>6}  {cm[e]['NOOP']:>4}")

    # Hard slice breakdown
    hard_details = [d for d in details if d["hard"]]
    if hard_details:
        hard_exp = [d["expected"] for d in hard_details]
        hard_pred = [d["predicted"] for d in hard_details]
        hard_correct = sum(1 for e, p in zip(hard_exp, hard_pred) if e == p)
        print(f"\n  Hard slice (29 UPDATE→ADD cases):")
        print(f"    Correct: {hard_correct}/29")
        # Breakdown
        hard_cm = {"ADD": 0, "UPDATE": 0, "NOOP": 0}
        for d in hard_details:
            hard_cm[d["predicted"]] = hard_cm.get(d["predicted"], 0) + 1
        print(f"    Predictions: ADD={hard_cm['ADD']} UPDATE={hard_cm['UPDATE']} NOOP={hard_cm['NOOP']}")
        # Methods used
        methods = {}
        for d in hard_details:
            methods[d["method"]] = methods.get(d["method"], 0) + 1
        print(f"    Methods: {methods}")


async def main():
    gold_all = _load_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl")

    # Filter to hard slice
    gold_hard = [r for r in gold_all if r["id"] in HARD_SLICE_IDS]
    gold_hard.sort(key=lambda r: r["id"])
    print(f"Hard slice: {len(gold_hard)} cases")
    print(f"  Hard UPDATE→ADD: {sum(1 for r in gold_hard if r['id'] in HARD_IDS)}")
    print(f"  Control ADD: {sum(1 for r in gold_hard if r['id'] in CTRL_ADD_IDS)}")
    print(f"  Control UPDATE: {sum(1 for r in gold_hard if r['id'] in CTRL_UPD_IDS)}")
    print(f"  Control NOOP: {sum(1 for r in gold_hard if r['id'] in CTRL_NOOP_IDS)}")

    common = {
        "RETRIEVAL_EMBEDDING_BACKEND": "api",
        "RETRIEVAL_RERANKER_ENABLED": "true",
        "RETRIEVAL_RERANKER_WEIGHT": "0.30",
        "INTENT_LLM_ENABLED": "false",
        "COMPACT_GIST_LLM_ENABLED": "false",
        "WRITE_GUARD_SCORE_NORMALIZATION": "true",
        "WRITE_GUARD_NORMALIZATION_FLOOR": "0.85",
        "WRITE_GUARD_CROSS_CHECK_ADD_FLOOR": "0.10",
    }

    # Seed DB
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="llm_hard_")
    os.close(fd)

    saved = {}
    for k, v in common.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    # Pass 1: D-off (heuristic only, LLM disabled)
    os.environ["WRITE_GUARD_LLM_ENABLED"] = "false"
    client = SQLiteClient(f"sqlite+aiosqlite:///{db_path}")
    await client.init_db()

    all_existing = []
    for row in gold_all:
        all_existing.extend(row.get("existing_memories", []))
    seen = set()
    unique = [m for m in all_existing if m.get("uri","") not in seen and not seen.add(m.get("uri",""))]
    print(f"\nSeeding {len(unique)} memories...", flush=True)
    await _seed_memories(client, unique)
    try:
        await client.rebuild_index()
    except Exception:
        pass

    print("\n--- Pass 1: D-off (heuristic v3, LLM disabled) ---")
    exp1, pred1, det1 = await run_pass(client, gold_hard, "D-off/hard-slice")
    m1 = compute_metrics(exp1, pred1)
    print_results("D-off heuristic v3", m1, det1, HARD_IDS)
    await client.close()

    # Pass 2: D-on (heuristic v3 + LLM enabled)
    os.environ["WRITE_GUARD_LLM_ENABLED"] = "true"
    client = SQLiteClient(f"sqlite+aiosqlite:///{db_path}")
    await client.init_db()

    print("\n--- Pass 2: D-on (heuristic v3 + current LLM) ---")
    exp2, pred2, det2 = await run_pass(client, gold_hard, "D-on/hard-slice", use_llm=True)
    m2 = compute_metrics(exp2, pred2)
    print_results("D-on current LLM", m2, det2, HARD_IDS)
    await client.close()

    try:
        os.unlink(db_path)
    except Exception:
        pass

    # Restore env
    for k, v in saved.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v

    # Summary
    print("\n" + "=" * 60)
    print("COMPARISON: D-off vs D-on on hard slice")
    print("=" * 60)
    print(f"  {'':30} {'D-off':>10} {'D-on':>10} {'Delta':>10}")
    for metric in ["P", "R", "F1", "EM"]:
        d = m2[metric] - m1[metric]
        print(f"  {metric:30} {m1[metric]:10.3f} {m2[metric]:10.3f} {d:>+10.3f}")

    hard1 = sum(1 for d in det1 if d["hard"] and d["expected"] == d["predicted"])
    hard2 = sum(1 for d in det2 if d["hard"] and d["expected"] == d["predicted"])
    print(f"\n  Hard slice UPDATE correct:   {hard1}/29      {hard2}/29      {hard2-hard1:>+10d}")

    # Save
    report = {"d_off": {"metrics": m1, "details": det1}, "d_on": {"metrics": m2, "details": det2}}
    out = BENCHMARK_DIR / "llm_hard_slice_report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\n  Report: {out}")


if __name__ == "__main__":
    asyncio.run(main())
