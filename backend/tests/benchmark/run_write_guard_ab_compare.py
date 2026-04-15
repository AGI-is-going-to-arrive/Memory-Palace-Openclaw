"""Write Guard A/B comparison: baseline vs experimental on same DB.

For each cell (B-off, C-off):
  1. Seed DB once
  2. Run write_guard with EXPERIMENTAL=false (baseline)
  3. Run write_guard with EXPERIMENTAL=true (experimental)
  4. Compare P/R/F1/EM side by side

This eliminates all confounders (different DB, seeding order, etc).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
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
    n = len(expected_actions)
    em = sum(1 for e, p in zip(expected_actions, predicted_actions) if e == p) / n if n else 0

    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "exact_match": round(em, 4),
        "tp": tp, "fp": fp, "fn": fn,
        "n": n,
        "confusion_matrix": cm,
    }


async def run_write_guard_pass(client, gold, label):
    """Run write_guard for all gold cases, return metrics."""
    expected = []
    predicted = []
    for i, row in enumerate(gold):
        content = str(row["content"])
        exp = str(row["expected_action"]).upper()
        expected.append(exp)
        try:
            result = await client.write_guard(content=content, domain="core")
            pred = str(result.get("action", "ADD")).upper()
        except Exception:
            pred = "ADD"
        predicted.append(pred)
        if (i + 1) % 50 == 0:
            print(f"    [{label}] {i + 1}/{len(gold)}", flush=True)
    return compute_metrics(expected, predicted)


async def run_cell(cell_name: str, gold: List[Dict[str, Any]], cell_env: Dict[str, str]):
    """Run baseline and experimental back-to-back on the SAME seeded DB."""
    # Save and apply env
    saved = {}
    for k, v in cell_env.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v

    # Force baseline first
    os.environ["WRITE_GUARD_SCORE_NORMALIZATION"] = "false"

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix=f"ab_{cell_name}_")
    os.close(fd)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    client = SQLiteClient(db_url)
    await client.init_db()

    # Seed once
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

    print(f"\n[{cell_name}] Seeding {len(unique_existing)} memories...", flush=True)
    await _seed_memories(client, unique_existing)
    try:
        await client.rebuild_index()
    except Exception:
        pass

    # Pass 1: BASELINE (experimental=false)
    print(f"  [{cell_name}] Pass 1: BASELINE (experimental=false)", flush=True)
    # Re-create client to pick up env change
    await client.close()
    os.environ["WRITE_GUARD_SCORE_NORMALIZATION"] = "false"
    client = SQLiteClient(db_url)
    await client.init_db()
    baseline = await run_write_guard_pass(client, gold, f"{cell_name}/baseline")
    await client.close()

    # Pass 2: EXPERIMENTAL (experimental=true)
    print(f"  [{cell_name}] Pass 2: EXPERIMENTAL (experimental=true)", flush=True)
    os.environ["WRITE_GUARD_SCORE_NORMALIZATION"] = "true"
    os.environ["WRITE_GUARD_CROSS_CHECK_ADD_FLOOR"] = "0.10"
    client = SQLiteClient(db_url)
    await client.init_db()
    experimental = await run_write_guard_pass(client, gold, f"{cell_name}/experimental")
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

    return {"baseline": baseline, "experimental": experimental}


def print_comparison(cell: str, baseline, experimental):
    print(f"\n{'=' * 70}")
    print(f"  {cell}")
    print(f"{'=' * 70}")
    print(f"  {'Metric':<12} {'Baseline':>10} {'Experimental':>14} {'Delta':>10}")
    print(f"  {'-' * 48}")
    for m in ["precision", "recall", "f1", "exact_match"]:
        b = baseline[m]
        e = experimental[m]
        d = e - b
        flag = "  " if abs(d) < 0.005 else ("+" if d > 0 else "-")
        print(f"  {m:<12} {b:>10.3f} {e:>14.3f} {d:>+10.3f} {flag}")
    print()
    for label, data in [("Baseline", baseline), ("Experimental", experimental)]:
        cm = data["confusion_matrix"]
        print(f"  {label} Confusion Matrix:")
        print(f"    Expected\\Pred   ADD  UPDATE  NOOP")
        for exp in ["ADD", "UPDATE", "NOOP"]:
            print(f"    {exp:>6}         {cm[exp]['ADD']:>4}  {cm[exp]['UPDATE']:>6}  {cm[exp]['NOOP']:>4}")
        print()


async def main():
    gold = _load_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl")
    assert len(gold) >= 100

    common_off = {
        "INTENT_LLM_ENABLED": "false",
        "WRITE_GUARD_LLM_ENABLED": "false",
        "WRITE_GUARD_LLM_DIFF_RESCUE_ENABLED": "false",
        "COMPACT_GIST_LLM_ENABLED": "false",
    }
    common_on = {
        "INTENT_LLM_ENABLED": "true",
        "WRITE_GUARD_LLM_ENABLED": "true",
        "WRITE_GUARD_LLM_DIFF_RESCUE_ENABLED": "false",
        "COMPACT_GIST_LLM_ENABLED": "false",
    }

    cells = {
        "B-off": {
            **common_off,
            "RETRIEVAL_EMBEDDING_BACKEND": "hash",
            "RETRIEVAL_RERANKER_ENABLED": "false",
        },
        "B-on": {
            **common_on,
            "RETRIEVAL_EMBEDDING_BACKEND": "hash",
            "RETRIEVAL_RERANKER_ENABLED": "false",
        },
        "C-off": {
            **common_off,
            "RETRIEVAL_EMBEDDING_BACKEND": "api",
            "RETRIEVAL_RERANKER_ENABLED": "true",
            "RETRIEVAL_RERANKER_WEIGHT": "0.30",
        },
        "C-on": {
            **common_on,
            "RETRIEVAL_EMBEDDING_BACKEND": "api",
            "RETRIEVAL_RERANKER_ENABLED": "true",
            "RETRIEVAL_RERANKER_WEIGHT": "0.30",
        },
        "D-off": {
            **common_off,
            "RETRIEVAL_EMBEDDING_BACKEND": "api",
            "RETRIEVAL_RERANKER_ENABLED": "true",
            "RETRIEVAL_RERANKER_WEIGHT": "0.30",
        },
        "D-on": {
            **common_on,
            "RETRIEVAL_EMBEDDING_BACKEND": "api",
            "RETRIEVAL_RERANKER_ENABLED": "true",
            "RETRIEVAL_RERANKER_WEIGHT": "0.30",
        },
    }

    print("=" * 70)
    print("Write Guard A/B Comparison (same DB, back-to-back)")
    print(f"Gold set: {len(gold)} cases")
    print(f"WRITE_GUARD_CROSS_CHECK_ADD_FLOOR = 0.10")
    print("=" * 70)

    results = {}
    for cell_name, cell_env in cells.items():
        r = await run_cell(cell_name, gold, cell_env)
        results[cell_name] = r
        print_comparison(cell_name, r["baseline"], r["experimental"])

    # Summary table
    print("\n" + "=" * 70)
    print("SUMMARY TABLE")
    print("=" * 70)
    print(f"  {'Cell':<8} | {'':^22} Baseline {'':^22} | {'':^22} Experimental {'':^18}")
    print(f"  {'':8} | {'P':>6} {'R':>6} {'F1':>6} {'EM':>6} | {'P':>6} {'R':>6} {'F1':>6} {'EM':>6} | {'ΔEM':>6}")
    print(f"  {'-' * 75}")
    for cell in ["B-off", "B-on", "C-off", "C-on", "D-off", "D-on"]:
        if cell not in results:
            continue
        b = results[cell]["baseline"]
        e = results[cell]["experimental"]
        d_em = e["exact_match"] - b["exact_match"]
        print(f"  {cell:<8} | {b['precision']:6.3f} {b['recall']:6.3f} {b['f1']:6.3f} {b['exact_match']:6.3f}"
              f" | {e['precision']:6.3f} {e['recall']:6.3f} {e['f1']:6.3f} {e['exact_match']:6.3f}"
              f" | {d_em:>+6.3f}")

    # Non-regression
    print(f"\n  NON-REGRESSION:")
    for cell in ["B-off", "B-on", "C-off", "C-on", "D-off", "D-on"]:
        if cell not in results:
            continue
        b = results[cell]["baseline"]
        e = results[cell]["experimental"]
        if cell == "B-off":
            ok = (e["precision"] >= b["precision"] - 0.005
                  and e["recall"] >= b["recall"] - 0.005
                  and e["exact_match"] >= b["exact_match"] - 0.005)
            print(f"    {cell}: {'PASS' if ok else 'FAIL'} (no regression)")
        else:
            improved = e["exact_match"] > b["exact_match"]
            no_p_crash = e["precision"] >= b["precision"] - 0.05
            print(f"    {cell}: {'IMPROVED' if (improved and no_p_crash) else 'NO CHANGE/FAIL'}"
                  f" (EM {b['exact_match']:.3f} -> {e['exact_match']:.3f})")

    # Save
    report_path = BENCHMARK_DIR / "write_guard_ab_compare_report.json"
    report_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\n  Report: {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
