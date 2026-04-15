"""Holdout validation: 8 queries NOT used in any tuning, against same 60 corpus."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Dict, List

import pytest

try:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
except ModuleNotFoundError:
    pytest.skip("mcp SDK not installed", allow_module_level=True)

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_BENCH = Path(__file__).resolve().parent

sys.path.insert(0, str(_BENCH))
from test_e2e_blackbox_harness import _build_env, _build_server, _text_of, _parse_json, _detect_available_profiles
from test_e2e_native_harness import NativeMemory


def _load_jsonl(name: str) -> List[Dict]:
    with open(_FIXTURES / name) as f:
        return [json.loads(l) for l in f if l.strip()]


async def _run_holdout_mp(profile: str, corpus: List[Dict], queries: List[Dict], tmp_path: Path) -> List[Dict]:
    db_path = tmp_path / f"holdout_{profile}.db"
    env = _build_env(db_path, profile)
    env["WRITE_GUARD_SEMANTIC_NOOP_THRESHOLD"] = "0.99"
    env["WRITE_GUARD_SEMANTIC_UPDATE_THRESHOLD"] = "0.99"
    env["WRITE_GUARD_KEYWORD_NOOP_THRESHOLD"] = "0.99"
    env["WRITE_GUARD_KEYWORD_UPDATE_THRESHOLD"] = "0.99"
    server = _build_server(env)
    results = []

    stderr_path = tmp_path / f"holdout_{profile}.stderr.log"
    with stderr_path.open("w+", encoding="utf-8") as errlog:
        async with stdio_client(server, errlog=errlog) as (rs, ws):
            async with ClientSession(rs, ws) as session:
                await session.initialize()
                for entry in corpus:
                    await session.call_tool("create_memory", {
                        "parent_uri": f"{entry['domain']}://",
                        "content": entry["content"],
                        "priority": 5, "title": entry["title"],
                    })
                for q in queries:
                    t0 = time.perf_counter()
                    raw = _parse_json(_text_of(
                        await session.call_tool("search_memory", {"query": q["query"]})
                    ))
                    lat = (time.perf_counter() - t0) * 1000
                    uris = [r.get("uri", "") for r in raw.get("results", [])] if isinstance(raw, dict) else []
                    target = q["target_uri"]
                    hit = target in uris
                    rank = (uris.index(target) + 1) if hit else 0
                    results.append({
                        "query_id": q["query_id"], "hit": hit, "rank": rank,
                        "mrr": round(1.0 / rank, 4) if rank else 0,
                        "latency_ms": round(lat, 1),
                    })
    return results


def _run_holdout_fs(corpus: List[Dict], queries: List[Dict], tmp_path: Path) -> List[Dict]:
    mem = NativeMemory(tmp_path / "holdout_fs")
    for e in corpus:
        mem.create(e["domain"], e["title"], e["content"])
    results = []
    for q in queries:
        t0 = time.perf_counter()
        sr = mem.search(q["query"], max_results=10)
        lat = (time.perf_counter() - t0) * 1000
        uris = [r["uri"] for r in sr]
        target = q["target_uri"]
        hit = target in uris
        rank = (uris.index(target) + 1) if hit else 0
        results.append({
            "query_id": q["query_id"], "hit": hit, "rank": rank,
            "mrr": round(1.0 / rank, 4) if rank else 0,
            "latency_ms": round(lat, 1),
        })
    return results


@pytest.mark.asyncio
async def test_holdout_validation(tmp_path):
    """Run holdout set on all available profiles + fs_keyword."""
    corpus = _load_jsonl("e2e_hard_corpus.jsonl")
    queries = _load_jsonl("e2e_hard_holdout.jsonl")
    profiles = _detect_available_profiles()

    all_results = {}
    for pk in profiles:
        all_results[pk] = await _run_holdout_mp(pk, corpus, queries, tmp_path)
    all_results["fs_keyword"] = _run_holdout_fs(corpus, queries, tmp_path)

    print(f"\n{'='*60}")
    print(f"HOLDOUT VALIDATION: {len(queries)} queries, {len(corpus)} corpus")
    print(f"{'='*60}\n")

    for pk, results in all_results.items():
        n = len(results)
        hits = sum(1 for r in results if r["hit"])
        mrr = sum(r["mrr"] for r in results) / n if n else 0
        print(f"{pk:12s} HR={hits/n:.3f} MRR={mrr:.3f}")
        misses = [r["query_id"] for r in results if not r["hit"]]
        if misses:
            print(f"  misses: {misses}")

    report = {"holdout": True, "corpus_size": len(corpus), "query_count": len(queries), "profiles": all_results}
    with open(_BENCH / "e2e_hard_holdout_report.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
