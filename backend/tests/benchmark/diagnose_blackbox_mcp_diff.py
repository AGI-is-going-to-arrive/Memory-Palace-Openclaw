"""Diagnostic: Black-box MCP layer diff for HQ07/HQ11/HQ17.

Runs the EXACT same flow as test_e2e_hard_benchmark (MCP stdio, Profile D,
60-corpus create then search), but captures raw MCP returns for each step.

Usage:
  RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_RERANKER_API_BASE=... \
    python -m pytest diagnose_blackbox_mcp_diff.py -v -s
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

try:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
except ModuleNotFoundError:
    pytest.skip("mcp SDK not installed", allow_module_level=True)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_BACKEND_ROOT = _PROJECT_ROOT / "backend"
_FIXTURES = _BACKEND_ROOT / "tests" / "fixtures"
_BENCH = Path(__file__).resolve().parent

sys.path.insert(0, str(_BENCH))
from test_e2e_blackbox_harness import _build_env, _build_server, _text_of, _parse_json

_DIAG_IDS = {"HQ07", "HQ11", "HQ17"}


def _load_jsonl(name: str) -> List[Dict]:
    with open(_FIXTURES / name) as f:
        return [json.loads(l) for l in f if l.strip()]


@pytest.mark.asyncio
async def test_diagnose_mcp_diff(tmp_path):
    """Trace raw MCP returns for HQ07/HQ11/HQ17 on Profile D."""

    if not os.environ.get("RETRIEVAL_EMBEDDING_API_BASE"):
        pytest.skip("Requires embedding env")
    if not os.environ.get("RETRIEVAL_RERANKER_API_BASE"):
        pytest.skip("Requires reranker env")

    corpus = _load_jsonl("e2e_hard_corpus.jsonl")
    queries = _load_jsonl("e2e_hard_queries.jsonl")
    diag_queries = [q for q in queries if q["query_id"] in _DIAG_IDS]

    db_path = tmp_path / "diag_mcp.db"
    env = _build_env(db_path, "D")
    server = _build_server(env)

    diag = {}

    stderr_path = tmp_path / "diag_mcp.stderr.log"
    with stderr_path.open("w+", encoding="utf-8") as errlog:
        async with stdio_client(server, errlog=errlog) as (rs, ws):
            async with ClientSession(rs, ws) as session:
                await session.initialize()

                # Phase 1: Create all corpus, capture raw returns for targets
                print(f"\n{'='*80}")
                print(f"Phase 1: Creating {len(corpus)} corpus entries via MCP")
                print(f"{'='*80}\n")

                target_fids = {q["target_id"] for q in diag_queries}
                create_returns: Dict[str, Any] = {}

                for entry in corpus:
                    raw = _parse_json(_text_of(
                        await session.call_tool("create_memory", {
                            "parent_uri": f"{entry['domain']}://",
                            "content": entry["content"],
                            "priority": 5,
                            "title": entry["title"],
                        })
                    ))

                    if entry["fixture_id"] in target_fids:
                        create_returns[entry["fixture_id"]] = {
                            "fixture_id": entry["fixture_id"],
                            "input_parent_uri": f"{entry['domain']}://",
                            "input_title": entry["title"],
                            "input_domain": entry["domain"],
                            "mcp_raw_return": raw,
                        }
                        print(f"  TARGET {entry['fixture_id']}:")
                        print(f"    Input: parent_uri={entry['domain']}://, title={entry['title']}")
                        if isinstance(raw, dict):
                            print(f"    Return: uri={raw.get('uri')}, id={raw.get('id')}, "
                                  f"created={raw.get('created')}, guard={raw.get('guard_action')}")
                        else:
                            print(f"    Return (non-dict): {str(raw)[:200]}")

                print(f"\nTotal created: {len(corpus)}")
                print(f"Target create returns captured: {len(create_returns)}")

                # Phase 2: Search for each diag query, capture raw returns
                print(f"\n{'='*80}")
                print(f"Phase 2: Searching for {len(diag_queries)} diagnostic queries")
                print(f"{'='*80}\n")

                for q in diag_queries:
                    qid = q["query_id"]
                    expected_uri = q["target_uri"]
                    target_create = create_returns.get(q["target_id"], {})
                    actual_created_uri = None
                    if isinstance(target_create.get("mcp_raw_return"), dict):
                        actual_created_uri = target_create["mcp_raw_return"].get("uri")

                    raw = _parse_json(_text_of(
                        await session.call_tool("search_memory", {
                            "query": q["query"],
                        })
                    ))

                    print(f"--- {qid}: {q['query'][:60]} ---")
                    print(f"  Expected URI (from fixture): {expected_uri}")
                    print(f"  Actual created URI (from MCP): {actual_created_uri}")
                    print(f"  URI match: {expected_uri == actual_created_uri}")

                    search_uris = []
                    if isinstance(raw, dict):
                        results = raw.get("results", [])
                        print(f"  Search returned {len(results)} results:")
                        for i, r in enumerate(results[:10]):
                            uri = r.get("uri", "")
                            search_uris.append(uri)
                            is_expected = uri == expected_uri
                            is_actual = uri == actual_created_uri
                            marker = ""
                            if is_expected:
                                marker += " <<< EXPECTED_URI"
                            if is_actual:
                                marker += " <<< ACTUAL_CREATED"
                            if not is_expected and not is_actual and actual_created_uri and uri.endswith(actual_created_uri.split("://", 1)[-1] if "://" in actual_created_uri else ""):
                                marker += " <<< PARTIAL_MATCH"
                            print(f"    [{i+1}] {uri}{marker}")
                            snippet = r.get("snippet", "")[:80] if r.get("snippet") else ""
                            if snippet:
                                print(f"         snippet: {snippet}")
                    else:
                        print(f"  Search returned non-dict: {str(raw)[:200]}")

                    # Diagnosis
                    expected_in_results = expected_uri in search_uris
                    actual_in_results = actual_created_uri in search_uris if actual_created_uri else None
                    print(f"\n  DIAGNOSIS:")
                    print(f"    expected_uri in results: {expected_in_results}")
                    print(f"    actual_created_uri in results: {actual_in_results}")
                    if not expected_in_results and actual_in_results:
                        print(f"    >>> ROOT CAUSE: URI mismatch! Fixture says '{expected_uri}' "
                              f"but MCP created '{actual_created_uri}'")
                    elif not expected_in_results and not actual_in_results:
                        print(f"    >>> ROOT CAUSE: Target not in search results at all")
                    elif expected_in_results:
                        rank = search_uris.index(expected_uri) + 1
                        print(f"    >>> Target found at rank {rank}")

                    diag[qid] = {
                        "query": q["query"],
                        "expected_uri": expected_uri,
                        "actual_created_uri": actual_created_uri,
                        "uri_match": expected_uri == actual_created_uri,
                        "expected_in_results": expected_in_results,
                        "actual_in_results": actual_in_results,
                        "search_top5_uris": search_uris[:5],
                    }
                    print()

    # Write diagnostic
    report_path = _BENCH / "diag_blackbox_mcp_diff.json"
    with open(report_path, "w") as f:
        json.dump(diag, f, indent=2, ensure_ascii=False)
    print(f"Report: {report_path}")
