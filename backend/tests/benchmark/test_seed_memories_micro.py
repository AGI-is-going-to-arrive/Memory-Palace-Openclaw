"""Micro-test: verify seed_memories actually inserts into DB and search can find them.

This is a targeted verification for the P0 fix (path= → parent_path+title,
init_db, ancestor chain).  Run with:

    backend/.venv/bin/pytest -xvs backend/tests/benchmark/test_seed_memories_micro.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BENCHMARK_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = BENCHMARK_DIR.parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from db.sqlite_client import SQLiteClient
from helpers.real_retrieval_harness import make_temp_db_url, seed_memories


MICRO_MEMORIES = [
    {
        "uri": "core://test/coding/1",
        "content": "Refactored the API gateway authentication module for better throughput",
        "domain": "core",
    },
    {
        "uri": "core://test/project/2",
        "content": "Sprint retrospective: team velocity improved by 15% after CI pipeline fix",
        "domain": "core",
    },
    {
        "uri": "core://test/daily/3",
        "content": "Planned weekend hiking trip to Mount Fuji with detailed route notes",
        "domain": "core",
    },
]


@pytest.mark.asyncio
async def test_seed_creates_memories():
    """After seed, each memory should be retrievable by keyword search."""
    db_url = make_temp_db_url()
    client = SQLiteClient(db_url)
    await client.init_db()

    try:
        await seed_memories(client, MICRO_MEMORIES)

        # Verify each memory is findable via search_advanced
        for mem in MICRO_MEMORIES:
            # Pick a distinctive keyword from the content
            keywords = {
                "API gateway": "API gateway",
                "Sprint retrospective": "Sprint retrospective",
                "Mount Fuji": "Mount Fuji",
            }
            # Find matching keyword for this memory
            query = None
            for kw in keywords:
                if kw in mem["content"]:
                    query = keywords[kw]
                    break
            assert query, f"No keyword found for {mem['uri']}"

            results_raw = await client.search_advanced(
                query=query,
                mode="keyword",
                max_results=5,
            )
            results = results_raw.get("results", results_raw) if isinstance(results_raw, dict) else results_raw
            uris_found = [r.get("uri", "") for r in results]
            assert any(
                mem["uri"].split("://", 1)[1] in u for u in uris_found
            ), (
                f"seed_memories failed: {mem['uri']} not found when searching "
                f"'{query}'. Got URIs: {uris_found}"
            )
    finally:
        await client.close()
        db_path = db_url.replace("sqlite+aiosqlite:///", "")
        Path(db_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_seed_count_matches():
    """All 3 micro memories should be seeded (no silent failures)."""
    db_url = make_temp_db_url()
    client = SQLiteClient(db_url)
    await client.init_db()

    try:
        await seed_memories(client, MICRO_MEMORIES)

        # Count via browse-like search for all core:// memories
        results_raw = await client.search_advanced(
            query="test",
            mode="keyword",
            max_results=50,
        )
        results = results_raw.get("results", results_raw) if isinstance(results_raw, dict) else results_raw
        # Should find at least our 3 seeded memories (ancestors are placeholders)
        content_results = [
            r for r in results
            if r.get("content", "") != "(ancestor placeholder)"
        ]
        assert len(content_results) >= 3, (
            f"Expected >= 3 real memories, got {len(content_results)}: "
            f"{[r.get('uri') for r in content_results]}"
        )
    finally:
        await client.close()
        db_path = db_url.replace("sqlite+aiosqlite:///", "")
        Path(db_path).unlink(missing_ok=True)
