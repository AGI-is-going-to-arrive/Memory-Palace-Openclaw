"""Minimal diagnostic: Does MCP search degrade to keyword mode for hard queries?

Creates 60 corpus via MCP, then searches 5 queries, capturing full raw response
including degraded, degrade_reasons, metadata.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List

import pytest

try:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client
except ModuleNotFoundError:
    pytest.skip("mcp SDK not installed", allow_module_level=True)

_BENCH = Path(__file__).resolve().parent
_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

sys.path.insert(0, str(_BENCH))
from test_e2e_blackbox_harness import _build_env, _build_server, _text_of, _parse_json

_DIAG_IDS = {"HQ11", "HQ14", "HQ20"}


def _load_jsonl(name: str) -> List[Dict]:
    with open(_FIXTURES / name) as f:
        return [json.loads(l) for l in f if l.strip()]


@pytest.mark.asyncio
async def test_mcp_degradation_check(tmp_path):
    if not os.environ.get("RETRIEVAL_EMBEDDING_API_BASE"):
        pytest.skip("Requires embedding env")

    corpus = _load_jsonl("e2e_hard_corpus.jsonl")
    queries = [q for q in _load_jsonl("e2e_hard_queries.jsonl") if q["query_id"] in _DIAG_IDS]

    db_path = tmp_path / "diag_degrade.db"
    env = _build_env(db_path, "D")
    env["WRITE_GUARD_SEMANTIC_NOOP_THRESHOLD"] = "0.99"
    env["WRITE_GUARD_SEMANTIC_UPDATE_THRESHOLD"] = "0.99"
    env["WRITE_GUARD_KEYWORD_NOOP_THRESHOLD"] = "0.99"
    env["WRITE_GUARD_KEYWORD_UPDATE_THRESHOLD"] = "0.99"
    server = _build_server(env)

    stderr_path = tmp_path / "degrade.stderr.log"
    with stderr_path.open("w+", encoding="utf-8") as errlog:
        async with stdio_client(server, errlog=errlog) as (rs, ws):
            async with ClientSession(rs, ws) as session:
                await session.initialize()

                # Create corpus
                rejects = 0
                for entry in corpus:
                    raw = _parse_json(_text_of(
                        await session.call_tool("create_memory", {
                            "parent_uri": f"{entry['domain']}://",
                            "content": entry["content"],
                            "priority": 5,
                            "title": entry["title"],
                        })
                    ))
                    if isinstance(raw, dict) and not raw.get("created", True):
                        rejects += 1

                print(f"\nCorpus: {len(corpus)} attempted, {rejects} rejected")

                # Search with full raw response capture
                for q in queries:
                    raw = _parse_json(_text_of(
                        await session.call_tool("search_memory", {
                            "query": q["query"],
                        })
                    ))

                    print(f"\n--- {q['query_id']}: {q['query'][:50]} ---")
                    if isinstance(raw, dict):
                        print(f"  ok={raw.get('ok')}")
                        print(f"  degraded={raw.get('degraded')}")
                        print(f"  degrade_reasons={raw.get('degrade_reasons', [])}")
                        results = raw.get("results", [])
                        print(f"  result_count={len(results)}")
                        for i, r in enumerate(results[:5]):
                            print(f"  [{i+1}] {r.get('uri')} score={r.get('score','?')}")

                        # Key diagnostic fields
                        for field in (
                            'mode_requested', 'mode_applied',
                            'semantic_search_unavailable',
                            'backend_method', 'search_api_kind',
                            'strategy_template', 'strategy_template_applied',
                            'intent', 'intent_profile',
                            'candidate_pool_size',
                        ):
                            if field in raw:
                                print(f"  {field}={raw[field]}")
                        bm = raw.get('backend_metadata', {})
                        if isinstance(bm, dict):
                            for bf in ('degraded', 'degrade_reasons', 'rerank_applied',
                                       'vector_engine_selected', 'semantic_search_unavailable'):
                                if bf in bm:
                                    print(f"  backend.{bf}={bm[bf]}")
                    else:
                        print(f"  raw (non-dict): {str(raw)[:200]}")

    # Also print stderr for embedding errors
    stderr_content = (tmp_path / "degrade.stderr.log").read_text(encoding="utf-8", errors="replace")
    error_lines = [l for l in stderr_content.splitlines() if "error" in l.lower() or "degrad" in l.lower() or "fallback" in l.lower() or "timeout" in l.lower()]
    if error_lines:
        print(f"\n--- STDERR signals ({len(error_lines)}) ---")
        for l in error_lines[:20]:
            print(f"  {l[:200]}")
