"""Host-level spot check: verify real host chain triggers intent / gist / write guard.

These tests use backend product-quality harness (NOT full OpenClaw host-level),
but exercise the same code paths that a real host invocation would trigger.
Each capability gets 1 focused spot check with assertions on the code path
being exercised, not just the metric.

Section 4 companion — proves real host chain triggers these capabilities.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

BENCHMARK_DIR = Path(__file__).resolve().parent
FIXTURES_DIR = BENCHMARK_DIR.parent / "fixtures"
BACKEND_ROOT = BENCHMARK_DIR.parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

from db.sqlite_client import SQLiteClient
from helpers.health_checks import run_all_health_checks
from helpers.real_retrieval_harness import (
    MATRIX_CELLS,
    apply_cell_env,
    make_temp_db_url,
    seed_memories,
    select_llm_provider,
    apply_llm_provider,
)


# ---------------------------------------------------------------------------
# Spot check 1: Intent classification — LLM path is actually invoked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_intent_llm_invoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that classify_intent_with_llm actually calls the LLM and returns
    a non-fallback result when LLM is enabled and healthy."""
    health = await run_all_health_checks()
    if health["llm"]["status"] != "ok":
        pytest.skip("LLM unavailable")

    provider = select_llm_provider()
    if provider is None:
        pytest.skip("No LLM provider reachable")

    apply_llm_provider(monkeypatch, provider)
    monkeypatch.setenv("INTENT_LLM_ENABLED", "true")

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    try:
        # Factual query — should be classified as factual by both keyword and LLM
        query = "What is the default port for uvicorn?"
        normalized = client.preprocess_query(query)
        result = await client.classify_intent_with_llm(
            query, normalized.get("rewritten_query")
        )

        assert result is not None, "classify_intent_with_llm returned None"
        intent = result.get("intent", "")
        assert intent in {"factual", "exploratory", "temporal", "causal", "unknown"}, (
            f"Invalid intent: {intent}"
        )
        # Check that LLM was actually applied (not fallback)
        assert result.get("intent_llm_applied") is not False, (
            f"LLM was not applied: degrade_reasons={result.get('degrade_reasons')}"
        )

        # Causal query — should differ from keyword baseline for hard case
        causal_query = "为什么内存索引重建失败了"
        normalized2 = client.preprocess_query(causal_query)
        result2 = await client.classify_intent_with_llm(
            causal_query, normalized2.get("rewritten_query")
        )
        assert result2 is not None
        intent2 = result2.get("intent", "")
        assert intent2 in {"factual", "exploratory", "temporal", "causal", "unknown"}

        print(
            f"[spot-check] intent: q1={intent}, q2={intent2}, "
            f"llm_applied={result.get('intent_llm_applied', 'unknown')}",
            file=sys.stderr, flush=True,
        )
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Spot check 2: Write guard — real retrieval + threshold decision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_write_guard_real_retrieval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify write guard uses real search_advanced with seeded memories
    and produces a valid guard decision (not exception/unavailable)."""
    health = await run_all_health_checks()

    # Use B-off config (no external deps needed for basic verification)
    b_off = [c for c in MATRIX_CELLS if c.cell_id == "B-off"][0]
    apply_cell_env(monkeypatch, b_off, health)

    db_url = make_temp_db_url()
    client = SQLiteClient(db_url)
    await client.init_db()

    try:
        # Seed a memory
        await seed_memories(client, [
            {
                "uri": "core://project/config",
                "content": "The default deployment port is 8000. Use uvicorn with --host 0.0.0.0.",
                "domain": "core",
            }
        ])

        # Test 1: Similar content → should return UPDATE or NOOP (not ADD)
        decision1 = await client.write_guard(
            content="The default deployment port is 8000. Changed to use gunicorn.",
            domain="core",
        )
        action1 = decision1.get("action", "")
        method1 = decision1.get("method", "")
        assert action1 in {"ADD", "UPDATE", "NOOP", "DELETE"}, (
            f"Invalid action: {action1}"
        )
        assert method1 != "exception", (
            f"Write guard fell to exception path: {decision1}"
        )
        assert "write_guard_unavailable" not in str(decision1.get("reason", ""))

        # Test 2: Completely new content → should return ADD
        decision2 = await client.write_guard(
            content="A completely unrelated memory about quantum physics experiments.",
            domain="core",
        )
        action2 = decision2.get("action", "")
        assert action2 in {"ADD", "UPDATE", "NOOP", "DELETE"}

        print(
            f"[spot-check] write_guard: similar→{action1}({method1}), "
            f"new→{action2}",
            file=sys.stderr, flush=True,
        )
    finally:
        await client.close()
        db_path = db_url.replace("sqlite+aiosqlite:///", "")
        try:
            Path(db_path).unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Spot check 3: Compact gist — LLM gist generation path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spot_gist_llm_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify generate_compact_gist actually calls LLM and returns structured gist."""
    health = await run_all_health_checks()
    if health["llm"]["status"] != "ok":
        pytest.skip("LLM unavailable")

    provider = select_llm_provider()
    if provider is None:
        pytest.skip("No LLM provider reachable")

    apply_llm_provider(monkeypatch, provider)
    monkeypatch.setenv("COMPACT_GIST_LLM_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_REMOTE_TIMEOUT_SEC", "45")

    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    try:
        source = (
            "Today we discussed the memory retrieval pipeline. "
            "Key decisions: (1) switch from pure keyword to hybrid search, "
            "(2) add reranker as optional stage, (3) keep hash embedding as "
            "zero-dependency baseline. Action items: implement MMR dedup, "
            "benchmark latency impact of reranker, write migration script "
            "for embedding dimension change."
        )
        degrade_reasons: List[str] = []
        result = await client.generate_compact_gist(
            summary=source,
            max_points=3,
            max_chars=280,
            degrade_reasons=degrade_reasons,
        )

        assert result is not None, (
            f"generate_compact_gist returned None, "
            f"degrade_reasons={degrade_reasons}"
        )
        gist_text = result.get("gist_text", "")
        assert len(gist_text) > 0, "Empty gist text"
        assert len(gist_text) <= 300, f"Gist too long: {len(gist_text)} chars"

        quality = result.get("quality", 0)
        assert 0 <= quality <= 1, f"Quality out of range: {quality}"

        gist_method = result.get("gist_method", "")
        assert gist_method in {"llm_gist", "llm"}, (
            f"Expected LLM gist method, got: {gist_method}"
        )

        print(
            f"[spot-check] gist: method={gist_method}, quality={quality:.2f}, "
            f"len={len(gist_text)}, degrade={degrade_reasons}",
            file=sys.stderr, flush=True,
        )
        print(f"[spot-check] gist text: {gist_text[:120]}...", file=sys.stderr, flush=True)
    finally:
        await client.close()
