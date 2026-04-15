import sqlite3
from pathlib import Path

import pytest

from db.sqlite_client import SQLiteClient


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


class _StubSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, statement, *_args, **_kwargs):
        rendered = str(statement)
        self.calls.append(rendered)
        if "memory_chunks_fts" in rendered:
            raise sqlite3.OperationalError("database is locked")
        return None


@pytest.mark.asyncio
async def test_clear_memory_index_ignores_transient_fts_lock_without_disabling(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "fts-runtime-lock.db"))
    client._fts_available = True
    session = _StubSession()

    async def _noop_delete_vec_knn_rows(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(client, "_delete_vec_knn_rows", _noop_delete_vec_knn_rows)

    await client._clear_memory_index(session, memory_id=7)

    assert client._fts_available is True
    assert any("memory_chunks_fts" in item for item in session.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_uri"),
    [
        ("%", "core://literal-percent"),
        ("_", "core://literal-underscore"),
    ],
)
async def test_search_advanced_treats_gist_like_wildcards_as_literals(
    tmp_path: Path,
    query: str,
    expected_uri: str,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "gist-like-literal.db"))
    await client.init_db()

    percent_record = await client.create_memory(
        parent_path="",
        content="plain text percent anchor",
        priority=1,
        title="literal-percent",
        domain="core",
    )
    underscore_record = await client.create_memory(
        parent_path="",
        content="plain text underscore anchor",
        priority=1,
        title="literal-underscore",
        domain="core",
    )
    neutral_record = await client.create_memory(
        parent_path="",
        content="plain text neutral anchor",
        priority=1,
        title="neutral",
        domain="core",
    )

    await client.upsert_memory_gist(
        memory_id=percent_record["id"],
        gist_text="release checklist with a literal % marker",
        source_hash="gist-percent",
        gist_method="extractive_bullets",
        quality_score=0.95,
    )
    await client.upsert_memory_gist(
        memory_id=underscore_record["id"],
        gist_text="release checklist with a literal _ marker",
        source_hash="gist-underscore",
        gist_method="extractive_bullets",
        quality_score=0.94,
    )
    await client.upsert_memory_gist(
        memory_id=neutral_record["id"],
        gist_text="release checklist with no wildcard marker",
        source_hash="gist-neutral",
        gist_method="extractive_bullets",
        quality_score=0.93,
    )

    payload = await client.search_advanced(
        query=query,
        mode="keyword",
        max_results=10,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )

    uris = [item["uri"] for item in payload["results"]]

    await client.close()

    assert expected_uri in uris
    assert "core://neutral" not in uris
    assert client._fts_available is True


@pytest.mark.asyncio
async def test_rebuild_index_clears_stale_fts_rows_before_reindex(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "stale-fts-rebuild.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    created = await client.create_memory(
        parent_path="",
        content="stable rebuild anchor",
        priority=1,
        title="rebuild-anchor",
        domain="core",
    )
    await client.reindex_memory(created["id"], reason="seed")

    conn = sqlite3.connect(db_path)
    chunk_row = conn.execute(
        "SELECT id FROM memory_chunks WHERE memory_id = ? ORDER BY id LIMIT 1",
        (created["id"],),
    ).fetchone()
    assert chunk_row is not None
    chunk_id = int(chunk_row[0])
    conn.execute(
        "UPDATE memory_chunks_fts SET memory_id = ? WHERE rowid = ?",
        (created["id"] + 9999, chunk_id),
    )
    conn.commit()
    conn.close()

    result = await client.rebuild_index(reason="stale-fts-regression")

    conn = sqlite3.connect(db_path)
    chunk_count = conn.execute("SELECT count(*) FROM memory_chunks").fetchone()[0]
    fts_count = conn.execute("SELECT count(*) FROM memory_chunks_fts").fetchone()[0]
    orphan_fts_count = conn.execute(
        """
        SELECT count(*)
        FROM memory_chunks_fts f
        LEFT JOIN memory_chunks mc ON mc.id = f.chunk_id
        WHERE mc.id IS NULL
        """
    ).fetchone()[0]
    active_row = conn.execute(
        """
        SELECT f.memory_id
        FROM memory_chunks_fts f
        JOIN memory_chunks mc ON mc.id = f.chunk_id
        ORDER BY f.rowid
        LIMIT 1
        """
    ).fetchone()
    conn.close()

    await client.close()

    assert result["failure_count"] == 0
    assert chunk_count == 1
    assert fts_count == chunk_count
    assert orphan_fts_count == 0
    assert active_row is not None
    assert int(active_row[0]) == created["id"]


@pytest.mark.asyncio
async def test_reindex_memory_deletes_stale_fts_rowid_before_insert(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "stale-fts-reindex.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()

    created = await client.create_memory(
        parent_path="",
        content="single memory reindex anchor",
        priority=1,
        title="reindex-anchor",
        domain="core",
    )
    await client.reindex_memory(created["id"], reason="seed")

    conn = sqlite3.connect(db_path)
    chunk_row = conn.execute(
        "SELECT id FROM memory_chunks WHERE memory_id = ? ORDER BY id LIMIT 1",
        (created["id"],),
    ).fetchone()
    assert chunk_row is not None
    chunk_id = int(chunk_row[0])
    conn.execute(
        "UPDATE memory_chunks_fts SET memory_id = ? WHERE rowid = ?",
        (created["id"] + 4242, chunk_id),
    )
    conn.commit()
    conn.close()

    result = await client.reindex_memory(created["id"], reason="stale-rowid-regression")

    conn = sqlite3.connect(db_path)
    chunk_count = conn.execute("SELECT count(*) FROM memory_chunks").fetchone()[0]
    fts_count = conn.execute("SELECT count(*) FROM memory_chunks_fts").fetchone()[0]
    orphan_fts_count = conn.execute(
        """
        SELECT count(*)
        FROM memory_chunks_fts f
        LEFT JOIN memory_chunks mc ON mc.id = f.chunk_id
        WHERE mc.id IS NULL
        """
    ).fetchone()[0]
    active_row = conn.execute(
        """
        SELECT f.memory_id
        FROM memory_chunks_fts f
        JOIN memory_chunks mc ON mc.id = f.chunk_id
        ORDER BY f.rowid
        LIMIT 1
        """
    ).fetchone()
    conn.close()

    await client.close()

    assert result["indexed_chunks"] == 1
    assert chunk_count == 1
    assert fts_count == chunk_count
    assert orphan_fts_count == 0
    assert active_row is not None
    assert int(active_row[0]) == created["id"]
