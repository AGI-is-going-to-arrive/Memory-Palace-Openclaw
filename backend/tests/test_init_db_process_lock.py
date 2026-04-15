import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

import db.sqlite_client as sqlite_client_module
from db.sqlite_client import (
    SQLiteClient,
    _extract_sqlite_file_path,
    _normalize_sqlite_database_url,
    _resolve_init_lock_path,
)
from db.sqlite_paths import extract_sqlite_file_path


@pytest.mark.asyncio
async def test_init_db_serializes_same_database_bootstrap(monkeypatch, tmp_path):
    database_path = tmp_path / "init-lock.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    client_a = SQLiteClient(database_url)
    client_b = SQLiteClient(database_url)

    concurrency_lock = asyncio.Lock()
    current_concurrency = 0
    max_concurrency = 0

    async def fake_unlocked_init(self):
        nonlocal current_concurrency, max_concurrency
        async with concurrency_lock:
            current_concurrency += 1
            max_concurrency = max(max_concurrency, current_concurrency)
        await asyncio.sleep(0.05)
        async with concurrency_lock:
            current_concurrency -= 1

    monkeypatch.setattr(
        SQLiteClient, "_run_init_db_unlocked", fake_unlocked_init, raising=True
    )

    try:
        await asyncio.gather(client_a.init_db(), client_b.init_db())
    finally:
        await client_a.engine.dispose()
        await client_b.engine.dispose()

    assert max_concurrency == 1


@pytest.mark.asyncio
async def test_init_db_retries_transient_database_lock(monkeypatch, tmp_path):
    database_path = tmp_path / "init-lock-retry.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    client = SQLiteClient(database_url)
    attempts = 0
    sleep_calls: list[float] = []

    async def fake_unlocked_init(self):
        nonlocal attempts
        _ = self
        attempts += 1
        if attempts < 2:
            raise sqlite3.OperationalError("database is locked")

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(
        SQLiteClient, "_run_init_db_unlocked", fake_unlocked_init, raising=True
    )
    monkeypatch.setattr(sqlite_client_module.asyncio, "sleep", fake_sleep)

    try:
        await client.init_db()
    finally:
        await client.engine.dispose()

    assert attempts == 2
    assert sleep_calls == [0.5]


@pytest.mark.asyncio
async def test_init_db_retries_transient_file_lock_timeout(monkeypatch, tmp_path):
    database_path = tmp_path / "init-file-lock-retry.db"
    database_url = f"sqlite+aiosqlite:///{database_path}"
    client = SQLiteClient(database_url)
    lock_attempts = 0
    unlocked_attempts = 0
    sleep_calls: list[float] = []

    class _FakeAsyncFileLock:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            nonlocal lock_attempts
            lock_attempts += 1
            if lock_attempts < 2:
                raise sqlite_client_module.FileLockTimeout("init lock busy")
            return self

        async def __aexit__(self, exc_type, exc, tb):
            _ = exc_type
            _ = exc
            _ = tb
            return False

    async def fake_unlocked_init(self):
        nonlocal unlocked_attempts
        _ = self
        unlocked_attempts += 1

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(sqlite_client_module, "AsyncFileLock", _FakeAsyncFileLock)
    monkeypatch.setattr(
        SQLiteClient, "_run_init_db_unlocked", fake_unlocked_init, raising=True
    )
    monkeypatch.setattr(sqlite_client_module.asyncio, "sleep", fake_sleep)

    try:
        await client.init_db()
    finally:
        await client.engine.dispose()

    assert lock_attempts == 2
    assert unlocked_attempts == 1
    assert sleep_calls == [0.5]


def test_extract_sqlite_file_path_skips_memory_targets_and_query_string() -> None:
    relative = _extract_sqlite_file_path("sqlite+aiosqlite:///relative.db?cache=shared")
    absolute = _extract_sqlite_file_path("sqlite+aiosqlite:////tmp/demo.db?mode=rwc")
    memory_target = _extract_sqlite_file_path("sqlite+aiosqlite:///:memory:")
    shared_memory_target = _extract_sqlite_file_path(
        "sqlite+aiosqlite:///file::memory:?cache=shared"
    )

    assert relative == Path("relative.db")
    assert absolute == Path("/tmp/demo.db")
    assert memory_target is None
    assert shared_memory_target is None


def test_public_extract_sqlite_file_path_supports_sqlite_sync_urls() -> None:
    relative = extract_sqlite_file_path("sqlite:///relative.db?cache=shared")
    absolute = extract_sqlite_file_path("sqlite:////tmp/demo.db?mode=rwc")
    double_encoded = extract_sqlite_file_path("sqlite:///path%2520to%2520db.sqlite")

    assert relative == Path("relative.db")
    assert absolute == Path("/tmp/demo.db")
    assert double_encoded == Path("path%20to%20db.sqlite")


def test_extract_sqlite_file_path_preserves_literal_percent_sequences() -> None:
    escaped_space = extract_sqlite_file_path("sqlite:///path%2520to%2520db.sqlite")
    escaped_slash = extract_sqlite_file_path("sqlite:///my%252Fdb.sqlite")

    assert escaped_space == Path("path%20to%20db.sqlite")
    assert escaped_slash == Path("my%2Fdb.sqlite")

def test_normalize_sqlite_database_url_normalizes_fragment_then_query_suffix() -> None:
    assert _normalize_sqlite_database_url(
        "sqlite+aiosqlite:////tmp/demo.db#fragment?mode=rwc"
    ) == "sqlite+aiosqlite:////tmp/demo.db?mode=rwc#fragment"


def test_extract_sqlite_file_path_normalizes_windows_drive_prefix() -> None:
    if os.name != "nt":
        return
    windows_style = _extract_sqlite_file_path("sqlite+aiosqlite:////C:/tmp/demo.db")
    backslash_style = _extract_sqlite_file_path("sqlite+aiosqlite:///C:\\tmp\\demo.db")

    assert windows_style == Path("C:/tmp/demo.db")
    assert backslash_style == Path("C:/tmp/demo.db")


def test_normalize_sqlite_database_url_handles_windows_drive_prefix() -> None:
    if os.name != "nt":
        return

    assert _normalize_sqlite_database_url("sqlite+aiosqlite:////C:/tmp/demo.db") == (
        "sqlite+aiosqlite:///C:/tmp/demo.db"
    )
    assert _normalize_sqlite_database_url("sqlite+aiosqlite:///C:\\tmp\\demo.db") == (
        "sqlite+aiosqlite:///C:/tmp/demo.db"
    )


def test_normalize_sqlite_database_url_preserves_query_before_fragment() -> None:
    assert _normalize_sqlite_database_url(
        "sqlite+aiosqlite:////tmp/demo.db?mode=ro#anchor"
    ) == "sqlite+aiosqlite:////tmp/demo.db?mode=ro#anchor"


def test_resolve_init_lock_path_uses_database_suffix() -> None:
    database_path = Path("/tmp/demo.db")

    assert _resolve_init_lock_path(database_path) == Path("/tmp/demo.db.init.lock")


def test_sync_set_index_meta_retries_transient_lock() -> None:
    class _FakeConnection:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, statement, params):
            _ = statement
            _ = params
            self.calls += 1
            if self.calls < 3:
                raise sqlite3.OperationalError("database is locked")
            return None

    connection = _FakeConnection()

    SQLiteClient._sync_set_index_meta(
        connection,
        "fts_available",
        "1",
        "2026-03-17T00:00:00",
    )

    assert connection.calls == 3
