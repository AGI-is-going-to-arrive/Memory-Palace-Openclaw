"""Hard-Mode E2E Benchmark — 60 corpus + 20 paraphrase queries

Tests MP (via MCP) and filesystem keyword baseline on the same
60-item corpus with 20 hard queries that have minimal keyword overlap.

This is designed to differentiate semantic search from keyword grep
at a scale where noise matters.

Usage:
  # Profile B only:
  pytest test_e2e_hard_benchmark.py::test_hard_profile_b -v

  # All profiles + filesystem baseline:
  RETRIEVAL_EMBEDDING_API_BASE=... RETRIEVAL_RERANKER_API_BASE=... \\
    pytest test_e2e_hard_benchmark.py -v
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
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

_CORPUS_FILE = "e2e_hard_corpus.jsonl"
_QUERIES_FILE = "e2e_hard_queries.jsonl"

# Import filesystem baseline
sys.path.insert(0, str(_BENCH))
from test_e2e_native_harness import NativeMemory


# Reuse env/server builders from blackbox harness
from test_e2e_blackbox_harness import (
    _build_env,
    _build_server,
    _detect_available_profiles,
    _text_of,
    _parse_json,
)


@dataclass
class HardResult:
    query_id: str
    difficulty: str
    profile: str
    hit: bool = False
    rank: int = 0
    mrr: float = 0.0
    content_match: bool = False
    latency_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


def _load_corpus() -> List[Dict]:
    with open(_FIXTURES / _CORPUS_FILE) as f:
        return [json.loads(l) for l in f if l.strip()]


def _load_queries() -> List[Dict]:
    with open(_FIXTURES / _QUERIES_FILE) as f:
        return [json.loads(l) for l in f if l.strip()]


# ---------------------------------------------------------------------------
# MP (MCP) side
# ---------------------------------------------------------------------------


async def _run_mp_hard(profile: str, tmp_path: Path) -> List[HardResult]:
    corpus = _load_corpus()
    queries = _load_queries()
    db_path = tmp_path / f"hard_{profile}.db"
    env = _build_env(db_path, profile)
    # Disable write guard semantic merge during corpus loading.
    # Default threshold 0.78 causes false-positive merges in a 60-item
    # corpus (e.g. rate-limit-v2 merged into db-stack). Setting to 0.99
    # effectively disables semantic guard without changing production code.
    env["WRITE_GUARD_SEMANTIC_NOOP_THRESHOLD"] = "0.99"
    env["WRITE_GUARD_SEMANTIC_UPDATE_THRESHOLD"] = "0.99"
    env["WRITE_GUARD_KEYWORD_NOOP_THRESHOLD"] = "0.99"
    env["WRITE_GUARD_KEYWORD_UPDATE_THRESHOLD"] = "0.99"
    server = _build_server(env)
    results: List[HardResult] = []

    stderr_path = tmp_path / f"hard_{profile}.stderr.log"
    with stderr_path.open("w+", encoding="utf-8") as errlog:
        async with stdio_client(server, errlog=errlog) as (rs, ws):
            async with ClientSession(rs, ws) as session:
                await session.initialize()

                # Populate full corpus — verify all entries created
                guard_rejects = []
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
                        guard_rejects.append((
                            entry["fixture_id"],
                            raw.get("guard_action"),
                            raw.get("guard_target_uri"),
                        ))
                assert not guard_rejects, (
                    f"Write guard rejected {len(guard_rejects)} entries during "
                    f"corpus loading (should be 0): {guard_rejects[:5]}"
                )

                # Run queries
                for q in queries:
                    t0 = time.perf_counter()
                    raw = _parse_json(_text_of(
                        await session.call_tool("search_memory", {
                            "query": q["query"],
                        })
                    ))
                    lat = (time.perf_counter() - t0) * 1000

                    hr = HardResult(
                        query_id=q["query_id"],
                        difficulty=q["difficulty"],
                        profile=profile,
                        latency_ms=round(lat, 1),
                    )

                    if isinstance(raw, dict):
                        search_results = raw.get("results", [])
                        uris = [r.get("uri", "") for r in search_results]
                        target = q["target_uri"]

                        if target in uris:
                            rank = uris.index(target) + 1
                            hr.hit = True
                            hr.rank = rank
                            hr.mrr = 1.0 / rank

                        hr.details["returned_uris"] = uris[:5]
                        hr.details["result_count"] = len(search_results)

                    results.append(hr)

    return results


# ---------------------------------------------------------------------------
# Filesystem keyword side
# ---------------------------------------------------------------------------


def _run_fs_hard(tmp_path: Path) -> List[HardResult]:
    corpus = _load_corpus()
    queries = _load_queries()
    mem = NativeMemory(tmp_path / "fs_hard")
    results: List[HardResult] = []

    # Populate
    for entry in corpus:
        mem.create(entry["domain"], entry["title"], entry["content"])

    # Query
    for q in queries:
        t0 = time.perf_counter()
        search_results = mem.search(q["query"])
        lat = (time.perf_counter() - t0) * 1000

        hr = HardResult(
            query_id=q["query_id"],
            difficulty=q["difficulty"],
            profile="fs_keyword",
            latency_ms=round(lat, 1),
        )

        uris = [r["uri"] for r in search_results]
        target = q["target_uri"]

        if target in uris:
            rank = uris.index(target) + 1
            hr.hit = True
            hr.rank = rank
            hr.mrr = 1.0 / rank

        hr.details["returned_uris"] = uris[:5]
        hr.details["result_count"] = len(search_results)
        results.append(hr)

    return results


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _agg(results: List[HardResult]) -> Dict:
    n = len(results)
    if not n:
        return {"n": 0, "hr": 0, "mrr": 0}
    hits = sum(1 for r in results if r.hit)

    by_diff: Dict[str, List[HardResult]] = {}
    for r in results:
        by_diff.setdefault(r.difficulty, []).append(r)

    diff_agg = {}
    for d, drs in by_diff.items():
        dn = len(drs)
        diff_agg[d] = {
            "n": dn,
            "hr": sum(1 for r in drs if r.hit) / dn,
            "mrr": sum(r.mrr for r in drs) / dn,
        }

    latencies = sorted(r.latency_ms for r in results)
    p95_idx = min(int(n * 0.95), n - 1)

    return {
        "n": n,
        "hr": hits / n,
        "mrr": sum(r.mrr for r in results) / n,
        "p95_latency_ms": latencies[p95_idx],
        "by_difficulty": diff_agg,
    }


def _write_hard_report(all_results: Dict[str, List[HardResult]], tmp_path: Path) -> None:
    report = {
        "benchmark": "e2e_hard_mode",
        "corpus_size": len(_load_corpus()),
        "query_count": len(_load_queries()),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profiles": {},
    }

    for pk, results in all_results.items():
        agg = _agg(results)
        report["profiles"][pk] = {
            "overall": agg,
            "per_query": [
                {
                    "query_id": r.query_id,
                    "difficulty": r.difficulty,
                    "hit": r.hit,
                    "rank": r.rank,
                    "mrr": round(r.mrr, 4),
                    "latency_ms": r.latency_ms,
                    "details": r.details,
                }
                for r in results
            ],
        }

    for path in (_BENCH / "e2e_hard_report.json", tmp_path / "e2e_hard_report.json"):
        with open(path, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hard_profile_b(tmp_path):
    """Hard benchmark on Profile B (hash embedding)."""
    mp = await _run_mp_hard("B", tmp_path)
    fs = _run_fs_hard(tmp_path)
    _write_hard_report({"B": mp, "fs_keyword": fs}, tmp_path)

    mp_agg = _agg(mp)
    fs_agg = _agg(fs)
    assert mp_agg["n"] == 20
    assert fs_agg["n"] == 20


@pytest.mark.asyncio
async def test_hard_all_profiles(tmp_path):
    """Hard benchmark on all available profiles + filesystem baseline."""
    profiles = _detect_available_profiles()
    all_results: Dict[str, List[HardResult]] = {}

    for pk in profiles:
        all_results[pk] = await _run_mp_hard(pk, tmp_path)

    all_results["fs_keyword"] = _run_fs_hard(tmp_path)
    _write_hard_report(all_results, tmp_path)

    # Print summary
    for pk, results in all_results.items():
        agg = _agg(results)
        misses = [r.query_id for r in results if not r.hit]
        print(f"{pk}: HR={agg['hr']:.3f} MRR={agg['mrr']:.3f} misses={misses}")
