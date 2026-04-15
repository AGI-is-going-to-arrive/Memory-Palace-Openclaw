"""Analyze reranker score discriminability for UPDATE vs NOOP cases.

Collects reranker scores from search_advanced results for the 200 gold cases,
then checks whether reranker score can distinguish UPDATE from NOOP.
"""
from __future__ import annotations
import asyncio, json, os, sys, tempfile, statistics
from pathlib import Path
from typing import Any, Dict, List

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


async def main():
    gold = _load_jsonl(FIXTURES_DIR / "write_guard_product_gold_set.jsonl")

    os.environ["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
    os.environ["RETRIEVAL_RERANKER_ENABLED"] = "true"
    os.environ["RETRIEVAL_RERANKER_WEIGHT"] = "0.30"
    os.environ["INTENT_LLM_ENABLED"] = "false"
    os.environ["WRITE_GUARD_LLM_ENABLED"] = "false"
    os.environ["COMPACT_GIST_LLM_ENABLED"] = "false"

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="rerank_analysis_")
    os.close(fd)
    client = SQLiteClient(f"sqlite+aiosqlite:///{db_path}")
    await client.init_db()

    all_existing = []
    for row in gold:
        all_existing.extend(row.get("existing_memories", []))
    seen = set()
    unique = [m for m in all_existing if m.get("uri", "") not in seen and not seen.add(m.get("uri", ""))]
    print(f"Seeding {len(unique)} memories...", flush=True)
    await _seed_memories(client, unique)
    try:
        await client.rebuild_index()
    except Exception:
        pass

    print(f"Collecting scores for {len(gold)} cases...", flush=True)
    scored = []
    for i, row in enumerate(gold):
        content = str(row["content"])
        expected = str(row["expected_action"]).upper()

        try:
            sem_payload = await client.search_advanced(
                query=content, mode="semantic", max_results=6,
                candidate_multiplier=6, filters={"domain": "core"})
        except Exception:
            sem_payload = {"results": []}

        results = sem_payload.get("results", [])
        # Extract per-result reranker scores
        rerank_scores = []
        for r in results:
            s = r.get("scores", {})
            rerank_scores.append({
                "memory_id": r.get("memory_id"),
                "vector": float(s.get("vector", 0) or 0),
                "rerank": float(s.get("rerank", 0) or 0),
                "final": float(s.get("final", 0) or 0),
                "text": float(s.get("text", 0) or 0),
            })

        # Sort by vector score desc
        rerank_scores.sort(key=lambda x: x["vector"], reverse=True)
        top1 = rerank_scores[0] if rerank_scores else None

        scored.append({
            "id": row["id"],
            "expected": expected,
            "top1_vector": top1["vector"] if top1 else 0,
            "top1_rerank": top1["rerank"] if top1 else 0,
            "top1_final": top1["final"] if top1 else 0,
            "top1_text": top1["text"] if top1 else 0,
            "top1_memory_id": top1["memory_id"] if top1 else None,
            "all_rerank_scores": [r["rerank"] for r in rerank_scores],
        })
        if (i + 1) % 50 == 0:
            print(f"  Progress: {i+1}/{len(gold)}", flush=True)

    await client.close()
    try:
        os.unlink(db_path)
    except Exception:
        pass

    # Analysis: group by expected action
    # Also identify the 38 UPDATE→NOOP cases (those with vector >= 0.92)
    update_to_noop = [c for c in scored if c["expected"] == "UPDATE" and c["top1_vector"] >= 0.92]
    correct_noop = [c for c in scored if c["expected"] == "NOOP"]
    correct_update_low = [c for c in scored if c["expected"] == "UPDATE" and c["top1_vector"] < 0.92]
    add_cases = [c for c in scored if c["expected"] == "ADD"]

    def stats(vals, label):
        if not vals: return f"{label}: no data"
        return (f"{label}: n={len(vals)} min={min(vals):.4f} max={max(vals):.4f} "
                f"mean={statistics.mean(vals):.4f} med={statistics.median(vals):.4f}"
                + (f" stdev={statistics.stdev(vals):.4f}" if len(vals) >= 2 else ""))

    print("\n" + "=" * 70)
    print("RERANKER SCORE ANALYSIS")
    print("=" * 70)

    for label, group in [
        ("ADD (70)", add_cases),
        ("UPDATE below NOOP thresh (correct in baseline)", correct_update_low),
        ("UPDATE above NOOP thresh (→NOOP errors)", update_to_noop),
        ("NOOP", correct_noop),
    ]:
        print(f"\n{label} (n={len(group)}):")
        reranks = [c["top1_rerank"] for c in group]
        vectors = [c["top1_vector"] for c in group]
        finals = [c["top1_final"] for c in group]
        print(f"  {stats(reranks, 'rerank')}")
        print(f"  {stats(vectors, 'vector')}")
        print(f"  {stats(finals, 'final')}")
        rr_nonzero = [r for r in reranks if r > 0]
        print(f"  rerank=0: {len(reranks)-len(rr_nonzero)}, rerank>0: {len(rr_nonzero)}")

    # Can reranker distinguish UPDATE→NOOP from correct NOOP?
    print("\n" + "=" * 70)
    print("KEY QUESTION: Can reranker separate UPDATE→NOOP from NOOP?")
    print("=" * 70)
    un_reranks = [c["top1_rerank"] for c in update_to_noop if c["top1_rerank"] > 0]
    n_reranks = [c["top1_rerank"] for c in correct_noop if c["top1_rerank"] > 0]
    print(f"\nUPDATE→NOOP rerank>0: {stats(un_reranks, 'rerank')}")
    print(f"correct NOOP rerank>0: {stats(n_reranks, 'rerank')}")

    # Save raw data
    out_path = BENCHMARK_DIR / "reranker_signal_analysis.json"
    out_path.write_text(json.dumps(scored, indent=2, ensure_ascii=False))
    print(f"\nRaw data saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
