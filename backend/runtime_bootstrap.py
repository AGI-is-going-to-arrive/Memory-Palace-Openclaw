from __future__ import annotations

import os
import sqlite3
import stat
import logging
from pathlib import Path
from typing import Any, Callable, Optional

from db import get_sqlite_client
from db.sqlite_paths import extract_sqlite_file_path
from filesystem_utils import warn_if_unreliable_file_lock_path
from runtime_state import runtime_state


_LEGACY_REQUIRED_TABLE_NAMES: tuple[str, ...] = ("memories",)
logger = logging.getLogger(__name__)


def _is_regular_file_no_symlink(path: Path) -> bool:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(st.st_mode) and not stat.S_ISLNK(st.st_mode)


def _sqlite_quick_check_ok(conn: sqlite3.Connection) -> bool:
    try:
        rows = conn.execute("PRAGMA quick_check(1)").fetchall()
    except sqlite3.Error:
        return False
    if not rows:
        return False
    return all(str(row[0]).lower() == "ok" for row in rows if row)


def _sqlite_has_required_legacy_tables(conn: sqlite3.Connection) -> bool:
    placeholders = ",".join("?" for _ in _LEGACY_REQUIRED_TABLE_NAMES)
    if not placeholders:
        return True
    try:
        rows = conn.execute(
            f"""
            SELECT name
            FROM sqlite_master
            WHERE type='table'
              AND name IN ({placeholders})
            LIMIT 1
            """,
            tuple(_LEGACY_REQUIRED_TABLE_NAMES),
        ).fetchall()
    except sqlite3.Error:
        return False
    return bool(rows)


def try_restore_legacy_sqlite_file(database_url: Optional[str]) -> None:
    """
    Compatibility helper:
    if the new DB file does not exist but a legacy filename exists in the same
    directory, copy it to the new path so upgrades keep old data.
    """
    target_path = extract_sqlite_file_path(database_url)
    if not target_path or target_path.exists():
        return
    target_dir = target_path.parent
    if not target_dir.exists():
        return

    legacy_candidates = (
        "agent_memory.db",
        "nocturne_memory.db",
        "nocturne.db",
    )
    for legacy_name in legacy_candidates:
        legacy_path = target_dir / legacy_name
        if not legacy_path.exists():
            continue

        if not _is_regular_file_no_symlink(legacy_path):
            logger.info("[compat] Skipped legacy database file %s: not a regular file", legacy_path)
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True) as source_conn:
                if not _sqlite_quick_check_ok(source_conn):
                    logger.info("[compat] Skipped legacy database file %s: sqlite quick_check failed", legacy_path)
                    continue
                if not _sqlite_has_required_legacy_tables(source_conn):
                    logger.info("[compat] Skipped legacy database file %s: missing expected legacy tables", legacy_path)
                    continue
                with sqlite3.connect(target_path) as target_conn:
                    source_conn.backup(target_conn)
        except sqlite3.Error as exc:
            logger.warning("[compat] Skipped legacy database file %s: sqlite error: %s", legacy_path, exc)
            if target_path.exists():
                try:
                    target_path.unlink()
                except OSError:
                    pass
            continue

        logger.info("[compat] Restored legacy database file from %s to %s", legacy_path, target_path)
        return


def _try_restore_legacy_sqlite_file(database_url: Optional[str]) -> None:
    try_restore_legacy_sqlite_file(database_url)


async def initialize_backend_runtime(
    *,
    ensure_runtime_started: bool = True,
    database_url: Optional[str] = None,
    client_factory: Optional[Callable[[], Any]] = None,
) -> Any:
    resolved_database_url = (
        database_url if database_url is not None else os.getenv("DATABASE_URL")
    )
    warn_if_unreliable_file_lock_path(
        extract_sqlite_file_path(resolved_database_url),
        label="DATABASE_URL sqlite path",
        log=logger,
    )
    _try_restore_legacy_sqlite_file(resolved_database_url)
    resolved_client_factory = client_factory or get_sqlite_client
    sqlite_client = resolved_client_factory()
    await sqlite_client.init_db()
    if ensure_runtime_started:
        await runtime_state.ensure_started(resolved_client_factory)
    return sqlite_client
