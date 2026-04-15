from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.sqlite_client import IndexMeta, SQLiteClient


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


@pytest.mark.asyncio
async def test_search_advanced_like_fallback_treats_percent_as_literal(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "like-fallback.db"))
    await client.init_db()
    client._fts_available = False

    await client.create_memory(
        parent_path="",
        content="literal 100% coverage",
        priority=1,
        title="literal-percent",
        domain="core",
    )
    await client.create_memory(
        parent_path="",
        content="literal 1000 coverage",
        priority=1,
        title="wildcard-percent",
        domain="core",
    )

    payload = await client.search_advanced(
        query="100%",
        mode="keyword",
        max_results=10,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    uris = [item["uri"] for item in payload["results"]]
    assert "core://literal-percent" in uris
    assert "core://wildcard-percent" not in uris


@pytest.mark.asyncio
async def test_search_advanced_legacy_like_fallback_treats_underscore_as_literal(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "legacy-like-fallback.db"))
    await client.init_db()
    client._fts_available = False

    await client.create_memory(
        parent_path="",
        content="literal_1 marker",
        priority=1,
        title="literal-underscore",
        domain="core",
        index_now=False,
    )
    await client.create_memory(
        parent_path="",
        content="literalA1 marker",
        priority=1,
        title="wildcard-underscore",
        domain="core",
        index_now=False,
    )

    payload = await client.search_advanced(
        query="literal_1",
        mode="keyword",
        max_results=10,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    uris = [item["uri"] for item in payload["results"]]
    assert "core://literal-underscore" in uris
    assert "core://wildcard-underscore" not in uris


@pytest.mark.asyncio
async def test_search_advanced_keeps_fts_enabled_after_transient_search_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "fts-transient.db"))
    await client.init_db()
    assert client._fts_available is True

    await client.create_memory(
        parent_path="",
        content="release checklist",
        priority=1,
        title="release-plan",
        domain="core",
    )

    original_execute = AsyncSession.execute
    triggered = {"value": False}

    async def _patched_execute(self, statement, *args, **kwargs):
        sql_text = str(statement)
        if "memory_chunks_fts MATCH" in sql_text and not triggered["value"]:
            triggered["value"] = True
            raise sqlite3.OperationalError("database is locked")
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _patched_execute)

    payload = await client.search_advanced(
        query="release checklist",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )

    async with client.session() as session:
        result = await session.execute(
            select(IndexMeta.value).where(IndexMeta.key == "fts_available")
        )
        fts_available_meta = result.scalar_one()

    await client.close()

    assert triggered["value"] is True
    assert payload["results"]
    assert payload["results"][0]["uri"] == "core://release-plan"
    assert client._fts_available is True
    assert fts_available_meta == "1"


@pytest.mark.asyncio
async def test_search_advanced_disables_fts_after_permanent_search_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "fts-permanent.db"))
    await client.init_db()
    assert client._fts_available is True

    await client.create_memory(
        parent_path="",
        content="release checklist",
        priority=1,
        title="release-plan",
        domain="core",
    )

    original_execute = AsyncSession.execute

    async def _patched_execute(self, statement, *args, **kwargs):
        if "memory_chunks_fts MATCH" in str(statement):
            raise sqlite3.OperationalError("no such table: memory_chunks_fts")
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _patched_execute)

    payload = await client.search_advanced(
        query="release checklist",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )

    async with client.session() as session:
        result = await session.execute(
            select(IndexMeta.value).where(IndexMeta.key == "fts_available")
        )
        fts_available_meta = result.scalar_one()

    await client.close()

    assert payload["results"]
    assert payload["results"][0]["uri"] == "core://release-plan"
    assert client._fts_available is False
    assert fts_available_meta == "0"


@pytest.mark.asyncio
async def test_search_advanced_treats_mixed_cjk_operator_query_as_literal_fallback(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "mixed-cjk-operator.db"))
    await client.init_db()

    await client.create_memory(
        parent_path="",
        content="这是一个白板启动计划",
        priority=1,
        title="cn-plan",
        domain="core",
    )

    payload = await client.search_advanced(
        query="白板 OR 启动",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )

    await client.close()

    assert payload["results"]
    assert payload["results"][0]["uri"] == "core://cn-plan"


@pytest.mark.asyncio
async def test_search_advanced_like_fallback_filters_low_signal_partial_token_matches(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "like-fallback-low-signal.db"))
    await client.init_db()
    client._fts_available = False

    await client.create_memory(
        parent_path="",
        content="version 2 current payload",
        priority=1,
        title="low-signal",
        domain="core",
    )
    await client.create_memory(
        parent_path="",
        content="version 1 unique payload with anchor",
        priority=1,
        title="exact-target",
        domain="core",
    )

    payload = await client.search_advanced(
        query="version 1 unique payload",
        mode="keyword",
        max_results=10,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    uris = [item["uri"] for item in payload["results"]]
    assert "core://exact-target" in uris
    assert "core://low-signal" not in uris


@pytest.mark.asyncio
async def test_search_advanced_skips_fts_for_punctuation_only_query(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "punctuation-only-query.db"))
    await client.init_db()

    await client.create_memory(
        parent_path="",
        content='literal quote marker "',
        priority=1,
        title="quote-marker",
        domain="core",
    )

    original_execute = AsyncSession.execute
    match_calls = {"value": 0}

    async def _patched_execute(self, statement, *args, **kwargs):
        if "memory_chunks_fts MATCH" in str(statement):
            match_calls["value"] += 1
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _patched_execute)

    payload = await client.search_advanced(
        query='"',
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )

    await client.close()

    assert match_calls["value"] == 0
    assert payload["results"]
    assert payload["results"][0]["uri"] == "core://quote-marker"


@pytest.mark.asyncio
async def test_search_advanced_like_fallback_handles_unicode_casefold(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "unicode-casefold.db"))
    await client.init_db()
    client._fts_available = False

    await client.create_memory(
        parent_path="",
        content="İstanbul guide",
        priority=1,
        title="istanbul-guide",
        domain="core",
    )

    payload = await client.search_advanced(
        query="istanbul",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )

    await client.close()

    assert payload["results"]
    assert payload["results"][0]["uri"] == "core://istanbul-guide"
