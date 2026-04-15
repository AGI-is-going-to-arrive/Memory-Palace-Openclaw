"""Tests for the gist quality audit service (P3-2)."""

import asyncio
import json
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import gist_audit as gist_audit_mod
from gist_audit import (
    _reset_table_ensured,
    audit_gist,
    ensure_gist_audit_table,
    get_gist_audit_stats,
    run_gist_audit_batch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_engine():
    """Create an in-memory async engine with all prerequisite tables."""
    _reset_table_ensured()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await ensure_gist_audit_table(engine)
    # Create prerequisite tables for batch queries
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS memories ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  content TEXT NOT NULL,"
                "  deprecated INTEGER DEFAULT 0,"
                "  migrated_to INTEGER,"
                "  created_at TEXT,"
                "  vitality_score REAL DEFAULT 1.0,"
                "  last_accessed_at TEXT,"
                "  access_count INTEGER DEFAULT 0"
                ")"
            )
        )
        await conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS memory_gists ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  memory_id INTEGER NOT NULL,"
                "  gist_text TEXT NOT NULL,"
                "  source_content_hash TEXT NOT NULL,"
                "  gist_method TEXT NOT NULL DEFAULT 'fallback',"
                "  quality_score REAL,"
                "  created_at TEXT"
                ")"
            )
        )
    return engine


async def _dispose_engine(engine) -> None:
    """Best-effort async engine cleanup to avoid stray aiosqlite worker threads."""
    await engine.dispose()


def _mock_llm_response(scores: Dict[str, Any]) -> AsyncMock:
    """Build a mock LLM callable that returns an OpenAI-style response."""
    response_content = json.dumps(scores)
    mock = AsyncMock(
        return_value={
            "choices": [
                {
                    "message": {
                        "content": response_content,
                    }
                }
            ]
        }
    )
    return mock


async def _insert_memory(engine, content: str = "original trace text") -> int:
    """Insert a memory and return its ID."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text("INSERT INTO memories (content) VALUES (:content)"),
            {"content": content},
        )
        return result.lastrowid


async def _insert_gist(
    engine, memory_id: int, gist_text: str = "bullet summary",
    gist_method: str = "extractive_bullets",
    source_content_hash: str = "abc123",
) -> int:
    """Insert a memory gist and return its ID."""
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO memory_gists "
                "(memory_id, gist_text, gist_method, source_content_hash, quality_score) "
                "VALUES (:memory_id, :gist_text, :gist_method, :source_content_hash, 0.75)"
            ),
            {
                "memory_id": memory_id,
                "gist_text": gist_text,
                "gist_method": gist_method,
                "source_content_hash": source_content_hash,
            },
        )
        return result.lastrowid


async def _get_audit_count(engine) -> int:
    async with engine.begin() as conn:
        row = (
            await conn.execute(text("SELECT COUNT(*) FROM gist_audit_results"))
        ).fetchone()
        return row[0] if row else 0


async def _get_gist_quality_score(engine, gist_id: int):
    """Read the original gist quality_score to verify it was not modified."""
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text("SELECT quality_score FROM memory_gists WHERE id = :id"),
                {"id": gist_id},
            )
        ).fetchone()
        return row[0] if row else None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_gist_with_mock_llm():
    """Mock LLM returns valid JSON with scores — verify parsing."""
    scores = {
        "coverage_score": 0.85,
        "factual_preservation_score": 0.92,
        "actionability_score": 0.78,
        "missing_anchors": ["deadline date", "team lead name"],
        "hallucination_or_overcompression_flags": ["overly simplified timeline"],
    }
    mock_llm = _mock_llm_response(scores)

    result = await audit_gist(
        source_text="The project deadline is March 15. Team lead is Alice.",
        gist_text="Project has a deadline. Alice leads.",
        gist_method="extractive_bullets",
        llm_post_json=mock_llm,
    )

    assert result["degraded"] is False
    assert result["coverage_score"] == 0.85
    assert result["factual_preservation_score"] == 0.92
    assert result["actionability_score"] == 0.78
    assert "deadline date" in result["missing_anchors"]
    assert "overly simplified timeline" in result["hallucination_flags"]
    mock_llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_audit_gist_llm_failure_graceful():
    """Mock LLM raises exception — verify degraded result."""
    mock_llm = AsyncMock(side_effect=RuntimeError("connection refused"))

    result = await audit_gist(
        source_text="Some trace text.",
        gist_text="Summary.",
        gist_method="sentence_fallback",
        llm_post_json=mock_llm,
    )

    assert result["degraded"] is True
    assert result["degraded_reason"] == "llm_call_exception"
    assert result["coverage_score"] is None
    assert result["factual_preservation_score"] is None
    assert result["actionability_score"] is None


@pytest.mark.asyncio
async def test_audit_gist_llm_invalid_json():
    """Mock LLM returns garbage — verify degraded result."""
    mock_llm = AsyncMock(
        return_value={
            "choices": [
                {"message": {"content": "This is not JSON at all, sorry!"}}
            ]
        }
    )

    result = await audit_gist(
        source_text="Trace.",
        gist_text="Gist.",
        gist_method="truncate_fallback",
        llm_post_json=mock_llm,
    )

    assert result["degraded"] is True
    assert result["degraded_reason"] == "llm_response_invalid_json"


@pytest.mark.asyncio
async def test_audit_gist_llm_empty_response():
    """Mock LLM returns empty — verify degraded result."""
    mock_llm = AsyncMock(return_value={"choices": []})

    result = await audit_gist(
        source_text="Trace.",
        gist_text="Gist.",
        gist_method="llm_gist",
        llm_post_json=mock_llm,
    )

    assert result["degraded"] is True
    assert result["degraded_reason"] == "llm_response_empty"


@pytest.mark.asyncio
async def test_audit_gist_llm_none_response():
    """Mock LLM returns None — verify degraded result."""
    mock_llm = AsyncMock(return_value=None)

    result = await audit_gist(
        source_text="Trace.",
        gist_text="Gist.",
        gist_method="llm_gist",
        llm_post_json=mock_llm,
    )

    assert result["degraded"] is True
    assert result["degraded_reason"] == "llm_response_none"


@pytest.mark.asyncio
async def test_run_gist_audit_batch_no_llm():
    """No LLM available — verify status=skipped."""
    engine = await _make_engine()
    try:
        result = await run_gist_audit_batch(
            engine=engine,
            llm_post_json=None,
            limit=10,
        )

        assert result["status"] == "skipped"
        assert result["reason"] == "llm_unavailable"
    finally:
        await _dispose_engine(engine)


@pytest.mark.asyncio
async def test_run_gist_audit_batch_with_gists():
    """Create memory + gist, run batch, verify audit result stored."""
    engine = await _make_engine()
    try:
        memory_id = await _insert_memory(engine, "The deadline is March 15.")
        gist_id = await _insert_gist(engine, memory_id, "Deadline in March.")

        scores = {
            "coverage_score": 0.8,
            "factual_preservation_score": 0.9,
            "actionability_score": 0.7,
            "missing_anchors": [],
            "hallucination_or_overcompression_flags": [],
        }
        mock_llm = _mock_llm_response(scores)

        result = await run_gist_audit_batch(
            engine=engine,
            llm_post_json=mock_llm,
            limit=10,
        )

        assert result["status"] == "completed"
        assert result["audited"] == 1
        assert result["avg_coverage"] == 0.8

        # Verify the audit result is stored
        count = await _get_audit_count(engine)
        assert count == 1
    finally:
        await _dispose_engine(engine)


@pytest.mark.asyncio
async def test_get_gist_audit_stats():
    """Write audit results, verify stats aggregation."""
    engine = await _make_engine()
    try:
        # Insert two audit results directly
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO gist_audit_results "
                    "(gist_id, memory_id, gist_method, coverage_score, "
                    " factual_preservation_score, actionability_score, "
                    " missing_anchors, hallucination_flags, judge_model, "
                    " judge_raw_response, created_at, source_content_hash) "
                    "VALUES "
                    "(1, 1, 'extractive_bullets', 0.8, 0.9, 0.7, "
                    " :anchors1, '[]', 'test', '', datetime('now'), 'h1'),"
                    "(2, 2, 'sentence_fallback', 0.6, 0.8, 0.5, "
                    " :anchors2, :flags2, 'test', '', datetime('now'), 'h2')"
                ),
                {
                    "anchors1": json.dumps(["missing fact A"]),
                    "anchors2": json.dumps(["missing fact A", "missing fact B"]),
                    "flags2": json.dumps(["hallucinated claim X"]),
                },
            )

        stats = await get_gist_audit_stats(engine)

        assert stats["total_audited"] == 2
        assert stats["avg_coverage_score"] == 0.7  # (0.8 + 0.6) / 2
        assert stats["avg_factual_preservation_score"] == 0.85  # (0.9 + 0.8) / 2
        assert stats["avg_actionability_score"] == 0.6  # (0.7 + 0.5) / 2
        assert stats["hallucination_flag_count"] == 1
        assert "missing fact A" in stats["common_missing_anchors"]
        assert "extractive_bullets" in stats["method_breakdown"]
        assert "sentence_fallback" in stats["method_breakdown"]
        assert stats["last_audit_at"] is not None
    finally:
        await _dispose_engine(engine)


@pytest.mark.asyncio
async def test_gist_audit_does_not_modify_live_gist():
    """Run audit — verify original gist quality_score unchanged."""
    engine = await _make_engine()
    try:
        memory_id = await _insert_memory(engine, "Some trace content.")
        gist_id = await _insert_gist(engine, memory_id, "Summary of trace.")

        original_quality = await _get_gist_quality_score(engine, gist_id)
        assert original_quality == 0.75

        scores = {
            "coverage_score": 0.5,
            "factual_preservation_score": 0.4,
            "actionability_score": 0.3,
            "missing_anchors": ["everything"],
            "hallucination_or_overcompression_flags": [],
        }
        mock_llm = _mock_llm_response(scores)

        await run_gist_audit_batch(
            engine=engine,
            llm_post_json=mock_llm,
            limit=10,
        )

        # Original gist quality_score must be untouched
        after_quality = await _get_gist_quality_score(engine, gist_id)
        assert after_quality == 0.75
    finally:
        await _dispose_engine(engine)


@pytest.mark.asyncio
async def test_gist_audit_idempotent():
    """Run audit twice on same gist — second run should not create duplicates."""
    engine = await _make_engine()
    try:
        memory_id = await _insert_memory(engine, "Trace for idempotency test.")
        gist_id = await _insert_gist(engine, memory_id, "Idempotent gist.")

        scores = {
            "coverage_score": 0.9,
            "factual_preservation_score": 0.95,
            "actionability_score": 0.88,
            "missing_anchors": [],
            "hallucination_or_overcompression_flags": [],
        }
        mock_llm = _mock_llm_response(scores)

        # First run
        result1 = await run_gist_audit_batch(
            engine=engine, llm_post_json=mock_llm, limit=10
        )
        assert result1["audited"] == 1

        # Second run — already audited within 24h, should find no pending
        result2 = await run_gist_audit_batch(
            engine=engine, llm_post_json=mock_llm, limit=10
        )
        assert result2["audited"] == 0
        assert result2.get("reason") == "no_pending_gists"

        # Only 1 audit record should exist
        count = await _get_audit_count(engine)
        assert count == 1
    finally:
        await _dispose_engine(engine)


@pytest.mark.asyncio
async def test_get_gist_audit_stats_empty():
    """Stats on empty table return sensible defaults."""
    engine = await _make_engine()
    try:
        stats = await get_gist_audit_stats(engine)

        assert stats["total_audited"] == 0
        assert stats["avg_coverage_score"] is None
        assert stats["common_missing_anchors"] == []
        assert stats["method_breakdown"] == {}
    finally:
        await _dispose_engine(engine)


@pytest.mark.asyncio
async def test_ensure_gist_audit_table_idempotent():
    """Calling ensure_gist_audit_table twice does not error."""
    _reset_table_ensured()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        await ensure_gist_audit_table(engine)
        await ensure_gist_audit_table(engine)
        count = await _get_audit_count(engine)
        assert count == 0
    finally:
        await _dispose_engine(engine)


@pytest.mark.asyncio
async def test_audit_gist_scores_clamped():
    """Scores outside [0, 1] are clamped."""
    scores = {
        "coverage_score": 1.5,
        "factual_preservation_score": -0.3,
        "actionability_score": 0.5,
        "missing_anchors": [],
        "hallucination_or_overcompression_flags": [],
    }
    mock_llm = _mock_llm_response(scores)

    result = await audit_gist(
        source_text="trace",
        gist_text="gist",
        gist_method="llm_gist",
        llm_post_json=mock_llm,
    )

    assert result["coverage_score"] == 1.0
    assert result["factual_preservation_score"] == 0.0
    assert result["actionability_score"] == 0.5


# ---------------------------------------------------------------------------
# Regression tests (P3-2 bug fixes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gist_audit_batch_skips_recently_audited():
    """A gist with a recent audit (even if it also has an old audit) should NOT be re-selected.

    Reproduces the bug where LEFT JOIN matched the OLD row and re-selected the gist
    despite having a fresh audit within min_age_hours.
    """
    engine = await _make_engine()
    try:
        memory_id = await _insert_memory(engine, "Trace for recent-audit skip test.")
        gist_id = await _insert_gist(engine, memory_id, "Gist with mixed-age audits.")

        async with engine.begin() as conn:
            # Old audit: 48 hours ago
            await conn.execute(
                text(
                    "INSERT INTO gist_audit_results "
                    "(gist_id, memory_id, gist_method, coverage_score, "
                    " factual_preservation_score, actionability_score, "
                    " missing_anchors, hallucination_flags, judge_model, "
                    " judge_raw_response, created_at, source_content_hash) "
                    "VALUES (:gid, :mid, 'extractive_bullets', 0.7, 0.8, 0.6, "
                    " '[]', '[]', 'test', '', datetime('now', '-48 hours'), 'h1')"
                ),
                {"gid": gist_id, "mid": memory_id},
            )
            # Recent audit: 1 hour ago — should prevent re-selection
            await conn.execute(
                text(
                    "INSERT INTO gist_audit_results "
                    "(gist_id, memory_id, gist_method, coverage_score, "
                    " factual_preservation_score, actionability_score, "
                    " missing_anchors, hallucination_flags, judge_model, "
                    " judge_raw_response, created_at, source_content_hash) "
                    "VALUES (:gid, :mid, 'extractive_bullets', 0.9, 0.95, 0.85, "
                    " '[]', '[]', 'test', '', datetime('now', '-1 hours'), 'h1')"
                ),
                {"gid": gist_id, "mid": memory_id},
            )

        # Verify we have 2 audit records before the batch run
        pre_count = await _get_audit_count(engine)
        assert pre_count == 2

        scores = {
            "coverage_score": 0.5,
            "factual_preservation_score": 0.5,
            "actionability_score": 0.5,
            "missing_anchors": [],
            "hallucination_or_overcompression_flags": [],
        }
        mock_llm = _mock_llm_response(scores)

        result = await run_gist_audit_batch(
            engine=engine,
            llm_post_json=mock_llm,
            limit=10,
            min_age_hours=24,
        )

        # The gist should NOT be re-audited — its latest audit is 1h old (< 24h)
        assert result["audited"] == 0
        assert result.get("reason") == "no_pending_gists"

        # No new audit records should be created
        post_count = await _get_audit_count(engine)
        assert post_count == 2
    finally:
        await _dispose_engine(engine)


@pytest.mark.asyncio
async def test_gist_audit_endpoint_returns_queued_immediately():
    """POST /maintenance/gist-audit/run should return immediately with job_id.

    Validates the response structure contains status=queued, job_id, enqueued_at
    and that the background job tracker is populated.
    """
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

    from api.maintenance import _gist_audit_jobs, run_gist_audit

    # We need to mock the get_sqlite_client and GIST_AUDIT_ENABLED
    from unittest.mock import patch, MagicMock

    mock_engine = MagicMock()
    mock_engine.begin = MagicMock(return_value=AsyncMock())
    mock_client = MagicMock()
    mock_client.engine = mock_engine

    with patch("api.maintenance.get_sqlite_client", return_value=mock_client), \
         patch("gist_audit.GIST_AUDIT_ENABLED", True):
        response = await run_gist_audit(limit=5)

    # JSONResponse — parse the body
    assert hasattr(response, "body")
    body = json.loads(response.body.decode())

    assert body["status"] == "queued"
    assert "job_id" in body
    assert "enqueued_at" in body

    job_id = body["job_id"]
    assert job_id in _gist_audit_jobs
    job = _gist_audit_jobs[job_id]
    assert job["enqueued_at"] == body["enqueued_at"]

    # Clean up the job tracker
    _gist_audit_jobs.pop(job_id, None)
