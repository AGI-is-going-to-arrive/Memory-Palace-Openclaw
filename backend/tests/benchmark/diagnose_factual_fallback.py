"""Diagnose: Which queries trigger factual_high_precision fallback, on which profiles."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent
_FIXTURES = _BACKEND / "tests" / "fixtures"
_BENCH = Path(__file__).resolve().parent

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db.sqlite_client import SQLiteClient


def _load_jsonl(name: str) -> List[Dict]:
    with open(_FIXTURES / name) as f:
        return [json.loads(l) for l in f if l.strip()]


def _db_url(p: Path) -> str:
    return f"sqlite+aiosqlite:///{p}"


_BENCH_DOMAINS = (
    "core,personal,project,writing,research,finance,learning,"
    "writer,game,notes,system"
)

_EMBED_KEYS = [
    "RETRIEVAL_EMBEDDING_API_BASE", "RETRIEVAL_EMBEDDING_API_KEY",
    "RETRIEVAL_EMBEDDING_MODEL", "RETRIEVAL_EMBEDDING_DIM",
]
_RERANKER_KEYS = [
    "RETRIEVAL_RERANKER_ENABLED", "RETRIEVAL_RERANKER_API_BASE",
    "RETRIEVAL_RERANKER_MODEL", "RETRIEVAL_RERANKER_API_KEY",
]


async def _populate(client: SQLiteClient, corpus: List[Dict]) -> Dict[str, Tuple[int, str]]:
    id_map = {}
    for e in corpus:
        r = await client.create_memory(
            parent_path="", content=e["content"], priority=5,
            title=e["title"], domain=e["domain"], index_now=True,
        )
        id_map[e["fixture_id"]] = (r["id"], r["uri"])
    return id_map


@pytest.mark.asyncio
async def test_factual_fallback_analysis(tmp_path, monkeypatch):
    """Run all 20 hard queries on B/C/D, log intent + fallback for each."""

    monkeypatch.setenv("VALID_DOMAINS", _BENCH_DOMAINS)
    monkeypatch.setenv("WRITE_GUARD_SEMANTIC_NOOP_THRESHOLD", "0.99")
    monkeypatch.setenv("WRITE_GUARD_SEMANTIC_UPDATE_THRESHOLD", "0.99")
    monkeypatch.setenv("WRITE_GUARD_KEYWORD_NOOP_THRESHOLD", "0.99")
    monkeypatch.setenv("WRITE_GUARD_KEYWORD_UPDATE_THRESHOLD", "0.99")

    corpus = _load_jsonl("e2e_hard_corpus.jsonl")
    queries = _load_jsonl("e2e_hard_queries.jsonl")

    profiles_config = {"B": "hash", "C": "api", "D": "api"}
    has_embed = bool(os.environ.get("RETRIEVAL_EMBEDDING_API_BASE"))
    has_reranker = bool(
        os.environ.get("RETRIEVAL_RERANKER_API_BASE")
        and os.environ.get("RETRIEVAL_RERANKER_ENABLED", "").lower() == "true"
    )
    profiles_to_run = ["B"]
    if has_embed:
        profiles_to_run.append("C")
    if has_embed and has_reranker:
        profiles_to_run.append("D")

    report = {}

    for pk in profiles_to_run:
        monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", profiles_config[pk])
        if pk in ("C", "D"):
            for k in _EMBED_KEYS:
                v = os.environ.get(k)
                if v:
                    monkeypatch.setenv(k, v)
        if pk == "D":
            monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
            for k in _RERANKER_KEYS:
                v = os.environ.get(k)
                if v:
                    monkeypatch.setenv(k, v)
        else:
            monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "false")

        db = tmp_path / f"fallback_{pk}.db"
        client = SQLiteClient(_db_url(db))
        await client.init_db()
        id_map = await _populate(client, corpus)

        profile_results = []
        for q in queries:
            target_uri = q["target_uri"]
            target_mid = id_map.get(q["target_id"], (None, None))[0]

            result = await client.search_advanced(
                query=q["query"], mode="hybrid", max_results=10,
                candidate_multiplier=8,
            )
            meta = result.get("metadata", {})
            results_list = result.get("results", [])
            uris = [r.get("uri", "") for r in results_list[:10]]
            hit = target_uri in uris
            rank = (uris.index(target_uri) + 1) if hit else 0

            # Check if factual fallback would have triggered
            # (all text_scores < 0.01)
            text_scores = [
                r.get("scores", {}).get("text", 0)
                for r in results_list
            ]
            max_text = max(text_scores) if text_scores else 0

            profile_results.append({
                "query_id": q["query_id"],
                "hit": hit,
                "rank": rank,
                "max_text_score": round(float(max_text), 4),
                "factual_fallback_eligible": max_text < 0.01,
            })

        report[pk] = profile_results

    # Print summary
    print(f"\n{'='*80}")
    print("Factual Fallback Trigger Analysis")
    print(f"{'='*80}\n")

    for pk in profiles_to_run:
        results = report[pk]
        fallback_count = sum(1 for r in results if r["factual_fallback_eligible"])
        hits = sum(1 for r in results if r["hit"])
        print(f"Profile {pk}: {fallback_count}/20 queries eligible for fallback, {hits}/20 hits")

        fb_queries = [r for r in results if r["factual_fallback_eligible"]]
        non_fb = [r for r in results if not r["factual_fallback_eligible"]]
        if fb_queries:
            fb_hits = sum(1 for r in fb_queries if r["hit"])
            print(f"  Fallback-eligible ({len(fb_queries)}): {fb_hits} hits")
            for r in fb_queries:
                mark = "" if r["hit"] else " << MISS"
                print(f"    {r['query_id']} hit={r['hit']} rank={r['rank']} max_text={r['max_text_score']}{mark}")
        if non_fb:
            nfb_hits = sum(1 for r in non_fb if r["hit"])
            print(f"  Non-fallback ({len(non_fb)}): {nfb_hits} hits")
            for r in non_fb:
                if not r["hit"]:
                    print(f"    {r['query_id']} hit={r['hit']} max_text={r['max_text_score']} << MISS")
        print()

    with open(_BENCH / "diag_factual_fallback.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
