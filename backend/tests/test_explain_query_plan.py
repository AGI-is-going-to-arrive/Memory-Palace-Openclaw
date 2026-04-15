"""
Test that the EXPLAIN QUERY PLAN diagnostic path in
get_vitality_cleanup_candidates executes without error and uses
parameterised queries (C-8 hardening).
"""

import pytest
from db.sqlite_client import SQLiteClient


@pytest.mark.asyncio
async def test_explain_query_plan_executes_without_error(tmp_path):
    """get_vitality_cleanup_candidates runs EXPLAIN QUERY PLAN internally.
    Verify it doesn't raise 'Incorrect number of bindings supplied'
    or silently degrade.
    """
    db_path = tmp_path / "test_explain.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    client = SQLiteClient(db_url)
    await client.init_db()

    try:
        # Seed one memory so the query has data to plan over.
        await client.create_memory(
            parent_path="",
            content="test memory for explain query plan",
            priority=0,
            title="explain_test",
            domain="core",
        )

        # This method calls EXPLAIN QUERY PLAN internally.
        # The plan results live in summary.query_profile.
        result = await client.get_vitality_cleanup_candidates(
            domain="core",
            limit=10,
        )

        assert isinstance(result, dict)
        qp = result.get("summary", {}).get("query_profile", {})
        degrade = qp.get("degrade_reason")
        degraded = qp.get("degraded", False)
        plan = qp.get("plan_details")

        # EXPLAIN must have succeeded (not degraded) and produced plan rows.
        assert not degraded, f"EXPLAIN degraded: {degrade}"
        assert isinstance(plan, list) and len(plan) > 0, (
            f"EXPLAIN produced no plan details: {plan}"
        )
    finally:
        await client.close()
