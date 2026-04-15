from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db.sqlite_client import AutoPathCounter, Memory, SQLiteClient


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


@pytest.mark.asyncio
async def test_create_memory_auto_id_retries_concurrent_unique_path_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "auto-id-retry-concurrent.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )

    original_get_next_numeric_id = client._get_next_numeric_id
    worker_count = 6
    observed_next_values: list[int] = []

    async def _barrier_numeric_id(session, parent_path: str, domain: str = "core") -> int:
        next_value = await original_get_next_numeric_id(session, parent_path, domain)
        if parent_path == "parent":
            observed_next_values.append(next_value)
            await asyncio.sleep(0.05)
        return next_value

    monkeypatch.setattr(client, "_get_next_numeric_id", _barrier_numeric_id)

    results = await asyncio.gather(
        *[
            client.create_memory(
                parent_path="parent",
                content=f"child-{index}",
                priority=1,
                domain="core",
            )
            for index in range(worker_count)
        ]
    )

    await client.close()

    uris = {item["uri"] for item in results}
    numeric_suffixes = sorted(int(item["path"].rsplit("/", 1)[-1]) for item in results)
    assert len(uris) == worker_count
    assert numeric_suffixes == list(range(1, worker_count + 1))
    assert observed_next_values == [1] * worker_count


@pytest.mark.asyncio
async def test_create_memory_auto_id_retries_transient_sqlite_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "auto-id-retry-lock.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )

    original_flush = AsyncSession.flush
    lock_injected = False

    async def _flaky_flush(session: AsyncSession, *args, **kwargs):
        nonlocal lock_injected
        if not lock_injected and any(isinstance(item, Memory) for item in session.new):
            lock_injected = True
            raise sqlite3.OperationalError("database is locked")
        return await original_flush(session, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", _flaky_flush)

    created = await client.create_memory(
        parent_path="parent",
        content="new child",
        priority=1,
        domain="core",
    )

    await client.close()

    assert lock_injected is True
    assert created["uri"] == "core://parent/1"


@pytest.mark.asyncio
async def test_create_memory_rejects_blank_content_and_non_writable_domains(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "create-memory-validation.db"))
    await client.init_db()

    with pytest.raises(ValueError, match="Content must not be empty"):
        await client.create_memory(
            parent_path="",
            content="   \n\t",
            priority=1,
            title="blank-content",
            domain="core",
        )

    with pytest.raises(ValueError, match="Invalid domain 'invalid_domain_xyz'"):
        await client.create_memory(
            parent_path="",
            content="invalid domain",
            priority=1,
            title="invalid-domain",
            domain="invalid_domain_xyz",
        )

    with pytest.raises(ValueError, match="read-only"):
        await client.create_memory(
            parent_path="",
            content="reserved domain",
            priority=1,
            title="system-write",
            domain="system",
        )

    await client.close()


@pytest.mark.asyncio
async def test_create_memory_accepts_unicode_titles(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "create-memory-unicode-title.db"))
    await client.init_db()

    japanese = await client.create_memory(
        parent_path="",
        content="japanese title",
        priority=1,
        title="日本語メモ",
        domain="core",
    )
    korean = await client.create_memory(
        parent_path="",
        content="korean title",
        priority=1,
        title="한국어메모",
        domain="core",
    )
    accented = await client.create_memory(
        parent_path="",
        content="accented title",
        priority=1,
        title="café",
        domain="core",
    )

    await client.close()

    assert japanese["uri"] == "core://日本語メモ"
    assert korean["uri"] == "core://한국어메모"
    assert accented["uri"] == "core://café"


@pytest.mark.asyncio
async def test_update_and_alias_writes_reject_blank_content_and_read_only_domains(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "update-memory-validation.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )

    with pytest.raises(ValueError, match="Content must not be empty"):
        parent_mem = await client.get_memory_by_path("parent", "core")
        await client.update_memory(
            path="parent",
            content="   ",
            domain="core",
            expected_old_id=parent_mem["id"],
        )

    with pytest.raises(ValueError, match="read-only"):
        await client.add_path(
            new_path="system-alias",
            target_path="parent",
            new_domain="system",
            target_domain="core",
        )

    with pytest.raises(ValueError, match="read-only"):
        await client.remove_path(path="anything", domain="system")

    await client.close()


@pytest.mark.asyncio
async def test_restore_path_rejects_non_writable_domains(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "restore-path-validation.db"))
    await client.init_db()
    created = await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )

    with pytest.raises(ValueError, match="read-only"):
        await client.restore_path(
            path="restored",
            domain="system",
            memory_id=created["id"],
        )

    await client.close()


@pytest.mark.asyncio
async def test_alias_and_restore_path_reject_invalid_path_segments(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "alias-restore-path-validation.db"))
    await client.init_db()
    created = await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )

    with pytest.raises(ValueError, match="Invalid new_path 'bad title!'"):
        await client.add_path(
            new_path="bad title!",
            target_path="parent",
            new_domain="core",
            target_domain="core",
        )

    with pytest.raises(ValueError, match="Invalid target_path 'bad title!'"):
        await client.add_path(
            new_path="valid-alias",
            target_path="bad title!",
            new_domain="core",
            target_domain="core",
        )

    with pytest.raises(ValueError, match="Invalid path 'bad title!'"):
        await client.restore_path(
            path="bad title!",
            domain="core",
            memory_id=created["id"],
        )

    await client.close()


@pytest.mark.asyncio
async def test_create_memory_auto_id_raises_stable_value_error_after_retry_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "auto-id-retry-exhausted.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )
    await client.create_memory(
        parent_path="parent",
        content="occupied",
        priority=1,
        title="1",
        domain="core",
    )

    async def _always_reserve_one(*_args, **_kwargs) -> int:
        return 1

    monkeypatch.setattr(client, "_reserve_next_numeric_id", _always_reserve_one)

    with pytest.raises(ValueError, match="conflicted; retry the request"):
        await client.create_memory(
            parent_path="parent",
            content="new child",
            priority=1,
            domain="core",
        )

    await client.close()


@pytest.mark.asyncio
async def test_create_memory_auto_id_advances_after_explicit_numeric_title_without_prior_auto_ids(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "auto-id-explicit-floor.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )
    await client.create_memory(
        parent_path="parent",
        content="occupied",
        priority=1,
        title="9",
        domain="core",
    )

    created = await client.create_memory(
        parent_path="parent",
        content="new child",
        priority=1,
        domain="core",
    )

    await client.close()

    assert created["uri"] == "core://parent/10"


@pytest.mark.asyncio
async def test_create_memory_auto_id_bootstraps_from_existing_numeric_children_without_counter(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "auto-id-bootstrap-counter.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )
    await client.create_memory(
        parent_path="parent",
        content="existing numeric child",
        priority=1,
        title="7",
        domain="core",
    )

    async with client.session() as session:
        await session.execute(delete(AutoPathCounter))

    created = await client.create_memory(
        parent_path="parent",
        content="new child",
        priority=1,
        domain="core",
    )

    await client.close()

    assert created["uri"] == "core://parent/8"


@pytest.mark.asyncio
async def test_create_memory_auto_id_advances_after_explicit_numeric_title(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "auto-id-explicit-floor.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )
    first_auto = await client.create_memory(
        parent_path="parent",
        content="first auto child",
        priority=1,
        domain="core",
    )
    explicit = await client.create_memory(
        parent_path="parent",
        content="explicit child",
        priority=1,
        title="7",
        domain="core",
    )
    next_auto = await client.create_memory(
        parent_path="parent",
        content="next auto child",
        priority=1,
        domain="core",
    )

    await client.close()

    assert first_auto["uri"] == "core://parent/1"
    assert explicit["uri"] == "core://parent/7"
    assert next_auto["uri"] == "core://parent/8"


@pytest.mark.asyncio
async def test_create_memory_auto_id_ignores_stale_counter_floor(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "auto-id-stale-counter-floor.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="root",
        priority=1,
        title="parent",
        domain="core",
    )
    await client.create_memory(
        parent_path="parent",
        content="occupied",
        priority=1,
        title="7",
        domain="core",
    )

    async with client.session() as session:
        await session.execute(delete(AutoPathCounter))
        session.add(
            AutoPathCounter(
                domain="core",
                parent_path="parent",
                next_id=2,
            )
        )

    created = await client.create_memory(
        parent_path="parent",
        content="new child",
        priority=1,
        domain="core",
    )

    await client.close()

    assert created["uri"] == "core://parent/8"


@pytest.mark.asyncio
async def test_create_memory_rejects_empty_content_at_sqlite_client_boundary(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "create-memory-empty-content.db"))
    await client.init_db()

    with pytest.raises(ValueError, match="Content must not be empty"):
        await client.create_memory(
            parent_path="",
            content="   ",
            priority=1,
            title="empty-content",
            domain="core",
        )

    await client.close()


@pytest.mark.asyncio
async def test_create_memory_rejects_invalid_domain_at_sqlite_client_boundary(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "create-memory-invalid-domain.db"))
    await client.init_db()

    with pytest.raises(ValueError, match="Invalid domain"):
        await client.create_memory(
            parent_path="",
            content="valid content",
            priority=1,
            title="invalid-domain",
            domain="invalid_domain_xyz",
        )

    await client.close()


@pytest.mark.asyncio
async def test_update_memory_rejects_empty_content_at_sqlite_client_boundary(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "update-memory-empty-content.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="seed",
        priority=1,
        title="item",
        domain="core",
    )

    with pytest.raises(ValueError, match="Content must not be empty"):
        item_mem = await client.get_memory_by_path("item", "core")
        await client.update_memory(
            path="item",
            content="",
            domain="core",
            expected_old_id=item_mem["id"],
        )

    await client.close()


@pytest.mark.asyncio
async def test_update_memory_stale_read_raises_concurrent_modification(
    tmp_path: Path,
) -> None:
    """Regression: when two processes share a DB file, a stale-read update
    must raise rather than silently overwriting the other writer's result.

    Scenario:
      1. Client A reads memory (id=X, content="hello")
      2. Client B updates the same path → new id=Y, content="beta"
      3. Client A tries to update with expected_old_id=X → must FAIL
         because the current id is now Y, not X.
    """
    db_file = tmp_path / "stale-read.db"
    client_a = SQLiteClient(_sqlite_url(db_file))
    client_b = SQLiteClient(_sqlite_url(db_file))
    await client_a.init_db()
    await client_b.init_db()

    # Seed a memory
    await client_a.create_memory(
        parent_path="",
        content="hello",
        priority=1,
        title="item",
        domain="core",
    )

    # A reads the current version
    mem_a = await client_a.get_memory_by_path("item", "core")
    assert mem_a is not None
    old_id_seen_by_a = mem_a["id"]

    # B reads the current version, then updates the memory
    mem_b = await client_b.get_memory_by_path("item", "core")
    await client_b.update_memory(
        path="item", content="beta", domain="core",
        expected_old_id=mem_b["id"],
    )

    # A tries to update with stale expected_old_id → must raise
    with pytest.raises(ValueError, match="Concurrent modification detected"):
        await client_a.update_memory(
            path="item",
            content="alpha",
            domain="core",
            expected_old_id=old_id_seen_by_a,
        )

    # Verify B's content survived
    final = await client_a.get_memory_by_path("item", "core")
    assert final is not None
    assert final["content"] == "beta", (
        f"Expected B's content 'beta' to survive, got '{final['content']}'"
    )

    await client_a.close()
    await client_b.close()


@pytest.mark.asyncio
async def test_add_path_rejects_invalid_domain_and_path_segments(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "add-path-validation.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="seed",
        priority=1,
        title="target",
        domain="core",
    )

    with pytest.raises(ValueError, match="Invalid domain"):
        await client.add_path(
            new_path="alias",
            target_path="target",
            new_domain="invalid_domain_xyz",
            target_domain="core",
        )

    with pytest.raises(ValueError, match="Invalid new_path"):
        await client.add_path(
            new_path="../bad",
            target_path="target",
            new_domain="core",
            target_domain="core",
        )

    await client.close()
