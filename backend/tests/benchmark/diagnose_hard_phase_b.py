"""Phase B Diagnostic: Score breakdown for D-miss queries HQ11/12/14/16/20.

White-box (search_advanced direct) + captures fs_keyword results for comparison.
Uses same 60-corpus, same Profile D env, write guard disabled (threshold=0.99).

Usage:
  RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_RERANKER_API_BASE=... \
    python -m pytest diagnose_hard_phase_b.py -v -s
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent
_FIXTURES = _BACKEND / "tests" / "fixtures"
_BENCH = Path(__file__).resolve().parent

if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db.sqlite_client import SQLiteClient

sys.path.insert(0, str(_BENCH))
from test_e2e_native_harness import NativeMemory

_DIAG_IDS = {"HQ11", "HQ12", "HQ14", "HQ16", "HQ20"}


def _load_jsonl(name: str) -> List[Dict]:
    with open(_FIXTURES / name) as f:
        return [json.loads(l) for l in f if l.strip()]


def _db_url(p: Path) -> str:
    return f"sqlite+aiosqlite:///{p}"


async def _populate_whitebox(client: SQLiteClient, corpus: List[Dict]) -> Dict[str, Tuple[int, str]]:
    id_map: Dict[str, Tuple[int, str]] = {}
    for e in corpus:
        r = await client.create_memory(
            parent_path="", content=e["content"], priority=5,
            title=e["title"], domain=e["domain"], index_now=True,
        )
        id_map[e["fixture_id"]] = (r["id"], r["uri"])
    return id_map


def _run_fs(corpus: List[Dict], queries: List[Dict], tmp_path: Path) -> Dict[str, Dict]:
    mem = NativeMemory(tmp_path / "fs_diag")
    for e in corpus:
        mem.create(e["domain"], e["title"], e["content"])

    fs_results = {}
    for q in queries:
        results = mem.search(q["query"], max_results=10)
        uris = [r["uri"] for r in results]
        target = q["target_uri"]
        rank = (uris.index(target) + 1) if target in uris else 0
        fs_results[q["query_id"]] = {
            "top5_uris": uris[:5],
            "target_rank": rank,
            "hit": target in uris,
        }
    return fs_results


@pytest.mark.asyncio
async def test_diagnose_phase_b(tmp_path, monkeypatch):
    if not os.environ.get("RETRIEVAL_EMBEDDING_API_BASE"):
        pytest.skip("Requires embedding env")
    if not os.environ.get("RETRIEVAL_RERANKER_API_BASE"):
        pytest.skip("Requires reranker env")

    monkeypatch.setenv("VALID_DOMAINS",
        "core,personal,project,writing,research,finance,learning,"
        "writer,game,notes,system")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    # Disable write guard (same as hard benchmark fix)
    monkeypatch.setenv("WRITE_GUARD_SEMANTIC_NOOP_THRESHOLD", "0.99")
    monkeypatch.setenv("WRITE_GUARD_SEMANTIC_UPDATE_THRESHOLD", "0.99")
    monkeypatch.setenv("WRITE_GUARD_KEYWORD_NOOP_THRESHOLD", "0.99")
    monkeypatch.setenv("WRITE_GUARD_KEYWORD_UPDATE_THRESHOLD", "0.99")

    corpus = _load_jsonl("e2e_hard_corpus.jsonl")
    queries = _load_jsonl("e2e_hard_queries.jsonl")
    diag_queries = [q for q in queries if q["query_id"] in _DIAG_IDS]

    # White-box MP-D
    db = tmp_path / "diag_b.db"
    client = SQLiteClient(_db_url(db))
    await client.init_db()
    id_map = await _populate_whitebox(client, corpus)

    # Filesystem baseline
    fs = _run_fs(corpus, diag_queries, tmp_path)

    print(f"\n{'='*80}")
    print(f"Phase B Diagnostic: 5 D-miss queries (white-box + fs comparison)")
    print(f"Corpus: {len(corpus)}, Write guard: disabled (0.99)")
    print(f"{'='*80}")

    report = {}

    for q in diag_queries:
        qid = q["query_id"]
        target_uri = q["target_uri"]
        target_fid = q["target_id"]
        target_mid = id_map.get(target_fid, (None, None))[0]
        fs_data = fs.get(qid, {})

        print(f"\n{'─'*60}")
        print(f"{qid}: {q['query']}")
        print(f"  Target: {target_uri} (mid={target_mid})")
        print(f"  Difficulty: {q['difficulty']}")
        print(f"  FS result: hit={fs_data.get('hit')}, rank={fs_data.get('target_rank')}")
        print(f"  FS top-5: {fs_data.get('top5_uris', [])}")

        # Search with default max_results=10
        result = await client.search_advanced(
            query=q["query"], mode="hybrid", max_results=10,
            candidate_multiplier=8,
        )
        results_list = result.get("results", [])
        meta = result.get("metadata", {})

        uris_10 = [r.get("uri", "") for r in results_list[:10]]
        target_in_10 = target_uri in uris_10
        target_rank_10 = (uris_10.index(target_uri) + 1) if target_in_10 else 0

        print(f"\n  MP-D (max_results=10): hit={target_in_10}, rank={target_rank_10}")
        print(f"  Rerank applied: {meta.get('rerank_applied')}")

        for i, r in enumerate(results_list[:10]):
            uri = r.get("uri", "")
            scores = r.get("scores", {})
            is_target = uri == target_uri
            marker = " ◄◄◄ TARGET" if is_target else ""
            print(f"  [{i+1:2d}] {uri}{marker}")
            print(f"       final={float(r.get('score',0)):.4f} "
                  f"text={scores.get('text',0):.4f} "
                  f"vector={scores.get('vector',0):.4f} "
                  f"rerank={scores.get('rerank',0):.4f} "
                  f"recency={scores.get('recency',0):.4f}")

        # If not in top-10, search wider
        target_scores_wide = {}
        target_rank_wide = 0
        if not target_in_10:
            result_wide = await client.search_advanced(
                query=q["query"], mode="hybrid", max_results=50,
                candidate_multiplier=16,
            )
            for i, r in enumerate(result_wide.get("results", [])):
                if r.get("uri") == target_uri or r.get("memory_id") == target_mid:
                    target_rank_wide = i + 1
                    target_scores_wide = r.get("scores", {})
                    print(f"\n  >>> Target found at rank {target_rank_wide} (wide search, max=50):")
                    print(f"      final={float(r.get('score',0)):.4f} "
                          f"text={target_scores_wide.get('text',0):.4f} "
                          f"vector={target_scores_wide.get('vector',0):.4f} "
                          f"rerank={target_scores_wide.get('rerank',0):.4f}")
                    break
            else:
                print(f"\n  >>> Target NOT FOUND even in top-50 wide search")

        report[qid] = {
            "query": q["query"],
            "target_uri": target_uri,
            "difficulty": q["difficulty"],
            "mp_d_hit_top10": target_in_10,
            "mp_d_rank_top10": target_rank_10,
            "mp_d_rank_wide": target_rank_wide,
            "mp_d_target_scores": {
                k: round(float(v), 6) for k, v in
                (target_scores_wide or
                 (results_list[target_rank_10 - 1].get("scores", {})
                  if target_in_10 else {})).items()
            },
            "mp_d_top3": [r.get("uri", "") for r in results_list[:3]],
            "fs_hit": fs_data.get("hit", False),
            "fs_rank": fs_data.get("target_rank", 0),
            "fs_top3": fs_data.get("top5_uris", [])[:3],
        }

    # Save
    with open(_BENCH / "diag_phase_b.json", "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n\nReport: {_BENCH / 'diag_phase_b.json'}")
