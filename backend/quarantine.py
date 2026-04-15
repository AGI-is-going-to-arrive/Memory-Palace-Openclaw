"""
Flush quarantine service for Memory Palace.

Preserves events that would be destroyed by mark_flushed() when the write
guard returns NOOP/UPDATE (non-degraded, non-invalid).  Records are stored
in a dedicated ``flush_quarantine`` table with a configurable TTL and can
later be replayed, dismissed, or auto-expired.

This module intentionally avoids importing from ``mcp_server`` or
``runtime_state`` to prevent circular imports.  All database access uses
raw SQL via ``sqlalchemy.text()``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from db.sqlite_paths import extract_sqlite_file_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUARANTINE_ENABLED: bool = os.getenv("QUARANTINE_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
QUARANTINE_TTL_HOURS: int = max(
    1, int(os.getenv("QUARANTINE_TTL_HOURS", "72"))
)

# Cache table setup per database/engine instead of once per process.
_table_ensured_keys: set[str] = set()

# ---------------------------------------------------------------------------
# DDL helper (idempotent)
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = text(
    """
    CREATE TABLE IF NOT EXISTS flush_quarantine (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        source TEXT NOT NULL,
        summary TEXT NOT NULL,
        gist_text TEXT,
        trace_text TEXT,
        guard_action TEXT NOT NULL,
        guard_method TEXT,
        guard_reason TEXT,
        guard_target_uri TEXT,
        content_hash TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL,
        replayed_at TEXT,
        status TEXT NOT NULL DEFAULT 'pending'
    )
    """
)

_CREATE_INDEX_SESSION = text(
    "CREATE INDEX IF NOT EXISTS idx_flush_quarantine_session "
    "ON flush_quarantine(session_id)"
)
_CREATE_INDEX_STATUS = text(
    "CREATE INDEX IF NOT EXISTS idx_flush_quarantine_status "
    "ON flush_quarantine(status)"
)
_CREATE_INDEX_EXPIRES = text(
    "CREATE INDEX IF NOT EXISTS idx_flush_quarantine_expires "
    "ON flush_quarantine(expires_at)"
)


def _engine_cache_key(engine: Any) -> str:
    url = str(getattr(engine, "url", "") or "").strip()
    database_path = extract_sqlite_file_path(url) if url else None
    if database_path is not None:
        return str(database_path)
    return f"engine:{id(engine)}:{url}"


async def ensure_quarantine_table(engine: Any) -> None:
    """Create the quarantine table and indexes if they do not exist."""
    cache_key = _engine_cache_key(engine)
    if cache_key in _table_ensured_keys:
        return
    async with engine.begin() as conn:
        await conn.execute(_CREATE_TABLE_SQL)
        await conn.execute(_CREATE_INDEX_SESSION)
        await conn.execute(_CREATE_INDEX_STATUS)
        await conn.execute(_CREATE_INDEX_EXPIRES)
    _table_ensured_keys.add(cache_key)


def _reset_table_ensured() -> None:
    """Reset the module-level flag (for tests only)."""
    _table_ensured_keys.clear()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def write_quarantine_record(
    engine: Any,
    *,
    session_id: str,
    source: str,
    summary: str,
    gist_text: Optional[str],
    trace_text: Optional[str],
    guard_action: str,
    guard_method: Optional[str],
    guard_reason: Optional[str],
    guard_target_uri: Optional[str],
    content_hash: Optional[str],
    ttl_hours: int = QUARANTINE_TTL_HOURS,
) -> int:
    """Insert a quarantine record and return its ``id``."""
    await ensure_quarantine_table(engine)
    now = _utc_now()
    expires_at = now + timedelta(hours=max(0, ttl_hours))
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "INSERT INTO flush_quarantine "
                "(session_id, source, summary, gist_text, trace_text, "
                " guard_action, guard_method, guard_reason, guard_target_uri, "
                " content_hash, created_at, expires_at, status) "
                "VALUES (:session_id, :source, :summary, :gist_text, :trace_text, "
                " :guard_action, :guard_method, :guard_reason, :guard_target_uri, "
                " :content_hash, :created_at, :expires_at, 'pending')"
            ),
            {
                "session_id": session_id,
                "source": source,
                "summary": summary,
                "gist_text": gist_text,
                "trace_text": trace_text,
                "guard_action": guard_action,
                "guard_method": guard_method or "",
                "guard_reason": guard_reason or "",
                "guard_target_uri": guard_target_uri or "",
                "content_hash": content_hash or "",
                "created_at": now.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
        )
        return result.lastrowid  # type: ignore[return-value]


async def get_quarantine_stats(engine: Any) -> Dict[str, Any]:
    """Return counts grouped by status."""
    await ensure_quarantine_table(engine)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT status, COUNT(*) AS cnt "
                    "FROM flush_quarantine GROUP BY status"
                )
            )
        ).fetchall()
    counts: Dict[str, int] = {}
    total = 0
    for row in rows:
        counts[row[0]] = row[1]
        total += row[1]
    return {
        "total": total,
        "pending": counts.get("pending", 0),
        "replayed": counts.get("replayed", 0),
        "expired": counts.get("expired", 0),
        "dismissed": counts.get("dismissed", 0),
    }


async def get_quarantine_records(
    engine: Any,
    *,
    status: str = "pending",
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """List quarantine records filtered by status."""
    await ensure_quarantine_table(engine)
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT id, session_id, source, summary, gist_text, "
                    "trace_text, guard_action, guard_method, guard_reason, "
                    "guard_target_uri, content_hash, created_at, expires_at, "
                    "replayed_at, status "
                    "FROM flush_quarantine WHERE status = :status "
                    "ORDER BY id DESC LIMIT :limit"
                ),
                {"status": status, "limit": limit},
            )
        ).fetchall()
    return [
        {
            "id": r[0],
            "session_id": r[1],
            "source": r[2],
            "summary": r[3],
            "gist_text": r[4],
            "trace_text": r[5],
            "guard_action": r[6],
            "guard_method": r[7],
            "guard_reason": r[8],
            "guard_target_uri": r[9],
            "content_hash": r[10],
            "created_at": r[11],
            "expires_at": r[12],
            "replayed_at": r[13],
            "status": r[14],
        }
        for r in rows
    ]


async def replay_quarantine_record(engine: Any, *, record_id: int) -> bool:
    """Mark a record as replayed.  Returns ``True`` if the record was found."""
    await ensure_quarantine_table(engine)
    now = _utc_now()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE flush_quarantine SET status = 'replayed', "
                "replayed_at = :replayed_at WHERE id = :id AND status = 'pending'"
            ),
            {"id": record_id, "replayed_at": now.isoformat()},
        )
    return result.rowcount > 0  # type: ignore[union-attr]


async def dismiss_quarantine_record(engine: Any, *, record_id: int) -> bool:
    """Mark a record as dismissed.  Returns ``True`` if the record was found."""
    await ensure_quarantine_table(engine)
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE flush_quarantine SET status = 'dismissed' "
                "WHERE id = :id AND status = 'pending'"
            ),
            {"id": record_id},
        )
    return result.rowcount > 0  # type: ignore[union-attr]


async def expire_stale_quarantine(engine: Any) -> int:
    """Mark all expired-but-still-pending records.  Returns the count."""
    await ensure_quarantine_table(engine)
    now = _utc_now()
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                "UPDATE flush_quarantine SET status = 'expired' "
                "WHERE status = 'pending' AND expires_at < :now"
            ),
            {"now": now.isoformat()},
        )
    return result.rowcount  # type: ignore[return-value]
