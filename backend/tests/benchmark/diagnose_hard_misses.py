"""Diagnostic script: Score breakdown for hard-mode misses on Profile D.

Runs search_advanced() directly (white-box) against the 60-item hard corpus
to extract per-candidate score components for HQ07, HQ11, HQ17.

This is a one-shot diagnostic, not production code. Does not modify anything.

Usage:
  RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_RERANKER_API_BASE=... \
    python -m pytest diagnose_hard_misses.py -v -s
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent
_FIXTURES = _BACKEND / "tests" / "fixtures"
_BENCH = Path(__file__).resolve().parent

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db.sqlite_client import SQLiteClient

_CORPUS_FILE = "e2e_hard_corpus.jsonl"
_QUERIES_FILE = "e2e_hard_queries.jsonl"

_BENCH_DOMAINS = (
    "core,personal,project,writing,research,finance,learning,"
    "writer,game,notes,system"
)

# The 3 target queries for diagnosis
_DIAG_QUERY_IDS = {"HQ07", "HQ11", "HQ17"}


def _load_jsonl(name: str) -> List[Dict]:
    with open(_FIXTURES / name) as f:
        return [json.loads(line) for line in f if line.strip()]


def _db_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


async def _populate(client: SQLiteClient, corpus: List[Dict]) -> Dict[str, Tuple[int, str]]:
    """Create all corpus entries, return fixture_id -> (memory_id, uri)."""
    id_map: Dict[str, Tuple[int, str]] = {}
    created_ns = set()

    for entry in corpus:
        domain = entry["domain"]
        title = entry["title"]
        content = entry["content"]

        # Ensure domain namespace exists
        ns_key = f"{domain}://"
        if ns_key not in created_ns:
            created_ns.add(ns_key)

        r = await client.create_memory(
            parent_path="",
            content=content,
            priority=5,
            title=title,
            domain=domain,
            index_now=True,
        )
        id_map[entry["fixture_id"]] = (r["id"], r["uri"])

    return id_map


@pytest.mark.asyncio
async def test_diagnose_hard_misses(tmp_path, monkeypatch):
    """White-box diagnostic for HQ07/HQ11/HQ17 on Profile D."""

    # Check that embedding + reranker are available
    if not os.environ.get("RETRIEVAL_EMBEDDING_API_BASE"):
        pytest.skip("Requires RETRIEVAL_EMBEDDING_API_BASE for Profile D")
    if not os.environ.get("RETRIEVAL_RERANKER_API_BASE"):
        pytest.skip("Requires RETRIEVAL_RERANKER_API_BASE for Profile D")

    monkeypatch.setenv("VALID_DOMAINS", _BENCH_DOMAINS)
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")

    corpus = _load_jsonl(_CORPUS_FILE)
    queries = _load_jsonl(_QUERIES_FILE)
    diag_queries = [q for q in queries if q["query_id"] in _DIAG_QUERY_IDS]

    db = tmp_path / "diag_hard.db"
    client = SQLiteClient(_db_url(db))
    await client.init_db()

    id_map = await _populate(client, corpus)
    assert len(id_map) == len(corpus)

    print(f"\n{'='*80}")
    print(f"DIAGNOSTIC: Hard-mode misses on Profile D")
    print(f"Corpus: {len(corpus)} entries")
    print(f"{'='*80}\n")

    results = {}

    for q in diag_queries:
        qid = q["query_id"]
        target_uri = q["target_uri"]
        target_fid = q["target_id"]
        target_mid = id_map.get(target_fid, (None, None))[0]

        print(f"--- {qid}: {q['query'][:60]} ---")
        print(f"  Target: {target_uri} (mid={target_mid})")
        print(f"  Difficulty: {q['difficulty']}")

        result = await client.search_advanced(
            query=q["query"],
            mode="hybrid",
            max_results=10,
            candidate_multiplier=8,
        )

        search_results = result.get("results", [])
        meta = result.get("metadata", {})
        degraded = meta.get("degraded", False)

        print(f"  Results: {len(search_results)}, degraded={degraded}")
        print(f"  Rerank applied: {meta.get('rerank_applied', False)}")

        target_found = False
        target_rank = 0
        target_scores = {}

        for i, r in enumerate(search_results[:10]):
            uri = r.get("uri", "")
            scores = r.get("scores", {})
            final = float(r.get("score", 0))
            is_target = (uri == target_uri) or (r.get("memory_id") == target_mid)

            if is_target:
                target_found = True
                target_rank = i + 1
                target_scores = scores

            marker = " <<<< TARGET" if is_target else ""
            print(f"  [{i+1}] {uri}")
            print(f"      final={final:.4f} text={scores.get('text',0):.4f} "
                  f"vector={scores.get('vector',0):.4f} rerank={scores.get('rerank',0):.4f} "
                  f"recency={scores.get('recency',0):.4f}{marker}")

        if not target_found:
            print(f"  TARGET NOT IN TOP-10!")
            # Check if target was in candidate pool but scored too low
            # by searching with higher max_results
            result_wide = await client.search_advanced(
                query=q["query"],
                mode="hybrid",
                max_results=50,
                candidate_multiplier=16,
            )
            wide_results = result_wide.get("results", [])
            for i, r in enumerate(wide_results):
                uri = r.get("uri", "")
                if (uri == target_uri) or (r.get("memory_id") == target_mid):
                    scores = r.get("scores", {})
                    print(f"  Found target at rank {i+1} (with max_results=50):")
                    print(f"      final={float(r.get('score',0)):.4f} "
                          f"text={scores.get('text',0):.4f} "
                          f"vector={scores.get('vector',0):.4f} "
                          f"rerank={scores.get('rerank',0):.4f}")
                    target_rank = i + 1
                    target_scores = scores
                    break
            else:
                print(f"  TARGET NOT FOUND EVEN IN TOP-50!")

        results[qid] = {
            "query": q["query"],
            "target_uri": target_uri,
            "target_found_in_top10": target_found,
            "target_rank": target_rank,
            "target_scores": {k: round(float(v), 6) for k, v in target_scores.items()},
            "top3_uris": [r.get("uri", "") for r in search_results[:3]],
            "top3_scores": [
                {k: round(float(v), 6) for k, v in r.get("scores", {}).items()}
                for r in search_results[:3]
            ],
        }

        print()

    # Write diagnostic report
    report_path = _BENCH / "diag_hard_misses.json"
    with open(report_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Diagnostic report written to: {report_path}")
