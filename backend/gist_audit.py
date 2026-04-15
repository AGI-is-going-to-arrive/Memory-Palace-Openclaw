"""
Gist quality audit service for Memory Palace.

Evaluates existing gists against their source content using an LLM judge.
Results are stored in a dedicated table and surfaced in observability.
This module is async, non-blocking, and never modifies live flush/retrieval.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import text
from db.sqlite_paths import extract_sqlite_file_path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GIST_AUDIT_ENABLED: bool = os.getenv("GIST_AUDIT_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
GIST_AUDIT_BATCH_SIZE: int = max(
    1, int(os.getenv("GIST_AUDIT_BATCH_SIZE", "20"))
)

# Cache table setup per database/engine instead of once per process.
_table_ensured_keys: set[str] = set()

# ---------------------------------------------------------------------------
# DDL helper (idempotent)
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = text(
    """
    CREATE TABLE IF NOT EXISTS gist_audit_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gist_id INTEGER NOT NULL,
        memory_id INTEGER NOT NULL,
        gist_method TEXT NOT NULL,
        coverage_score REAL,
        factual_preservation_score REAL,
        actionability_score REAL,
        missing_anchors TEXT,
        hallucination_flags TEXT,
        judge_model TEXT,
        judge_raw_response TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        source_content_hash TEXT
    )
    """
)

_CREATE_INDEX_GIST_ID = text(
    "CREATE INDEX IF NOT EXISTS idx_gist_audit_gist_id "
    "ON gist_audit_results(gist_id)"
)
_CREATE_INDEX_MEMORY_ID = text(
    "CREATE INDEX IF NOT EXISTS idx_gist_audit_memory_id "
    "ON gist_audit_results(memory_id)"
)
_CREATE_INDEX_CREATED = text(
    "CREATE INDEX IF NOT EXISTS idx_gist_audit_created "
    "ON gist_audit_results(created_at)"
)


def _engine_cache_key(engine: Any) -> str:
    url = str(getattr(engine, "url", "") or "").strip()
    database_path = extract_sqlite_file_path(url) if url else None
    if database_path is not None:
        return str(database_path)
    return f"engine:{id(engine)}:{url}"


async def ensure_gist_audit_table(engine: Any) -> None:
    """Create the gist_audit_results table and indexes if they do not exist."""
    cache_key = _engine_cache_key(engine)
    if cache_key in _table_ensured_keys:
        return
    async with engine.begin() as conn:
        await conn.execute(_CREATE_TABLE_SQL)
        await conn.execute(_CREATE_INDEX_GIST_ID)
        await conn.execute(_CREATE_INDEX_MEMORY_ID)
        await conn.execute(_CREATE_INDEX_CREATED)
    _table_ensured_keys.add(cache_key)


def _reset_table_ensured() -> None:
    """Reset the module-level flag (for tests only)."""
    _table_ensured_keys.clear()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value: Any) -> Optional[float]:
    """Convert value to a clamped float in [0.0, 1.0], or None."""
    if value is None:
        return None
    try:
        f = float(value)
        return max(0.0, min(1.0, f))
    except (TypeError, ValueError):
        return None


def _safe_string_list(value: Any) -> List[str]:
    """Coerce a value to a list of strings."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _parse_json_response(raw: str) -> Optional[Dict[str, Any]]:
    """Best-effort JSON parse, stripping markdown fences if present."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last lines (fences)
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Core audit function
# ---------------------------------------------------------------------------


_JUDGE_SYSTEM_PROMPT = (
    "You are a gist quality evaluator for a memory system. "
    "Given the original trace text and a generated gist, evaluate the gist quality.\n\n"
    "Return strict JSON with these keys:\n"
    "- coverage_score (float 0.0-1.0): fraction of important information captured\n"
    "- factual_preservation_score (float 0.0-1.0): accuracy without distortion\n"
    "- actionability_score (float 0.0-1.0): can someone act without reading original\n"
    "- missing_anchors (list of strings): key facts from trace missing in gist\n"
    "- hallucination_or_overcompression_flags (list of strings): claims in gist not in trace"
)


def _build_judge_user_prompt(
    source_text: str, gist_text: str, gist_method: str
) -> str:
    return (
        f"Original trace:\n{source_text}\n\n"
        f"Gist ({gist_method}):\n{gist_text}"
    )


def _degraded_result(reason: str = "llm_error") -> Dict[str, Any]:
    """Return a result with all scores set to None."""
    return {
        "coverage_score": None,
        "factual_preservation_score": None,
        "actionability_score": None,
        "missing_anchors": [],
        "hallucination_flags": [],
        "degraded": True,
        "degraded_reason": reason,
    }


async def audit_gist(
    *,
    source_text: str,
    gist_text: str,
    gist_method: str,
    llm_post_json: Callable,
) -> Dict[str, Any]:
    """
    Call the LLM judge to evaluate a gist.

    ``llm_post_json`` should accept the same signature as
    ``SQLiteClient._post_json``: ``(base_url, path, payload, api_key, **kw)``.
    For this module we pass a simplified callable that accepts ``(payload,)``
    and returns the raw LLM response dict.
    """
    payload = {
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _build_judge_user_prompt(
                    source_text, gist_text, gist_method
                ),
            },
        ],
        "temperature": 0,
    }

    try:
        response = await llm_post_json(payload)
    except Exception as exc:
        logger.warning("gist_audit: LLM call failed: %s", exc)
        return _degraded_result("llm_call_exception")

    if response is None:
        return _degraded_result("llm_response_none")

    # Extract message text from OpenAI-style response
    message_text = None
    try:
        choices = response.get("choices") or []
        if choices:
            message_text = (choices[0].get("message") or {}).get("content")
    except (AttributeError, IndexError, TypeError):
        pass

    if not message_text:
        return _degraded_result("llm_response_empty")

    parsed = _parse_json_response(message_text)
    if parsed is None:
        return _degraded_result("llm_response_invalid_json")

    return {
        "coverage_score": _safe_float(parsed.get("coverage_score")),
        "factual_preservation_score": _safe_float(
            parsed.get("factual_preservation_score")
        ),
        "actionability_score": _safe_float(parsed.get("actionability_score")),
        "missing_anchors": _safe_string_list(
            parsed.get("missing_anchors")
        ),
        "hallucination_flags": _safe_string_list(
            parsed.get("hallucination_or_overcompression_flags")
        ),
        "degraded": False,
        "judge_raw_response": message_text,
    }


# ---------------------------------------------------------------------------
# Batch audit runner
# ---------------------------------------------------------------------------


async def run_gist_audit_batch(
    *,
    engine: Any,
    llm_post_json: Optional[Callable],
    limit: int = 20,
    min_age_hours: int = 1,
) -> Dict[str, Any]:
    """
    Run a batch of gist quality audits.

    Queries gists that have not been audited yet (no corresponding
    gist_audit_results row) or were audited more than 24 hours ago.
    """
    if llm_post_json is None:
        return {"status": "skipped", "reason": "llm_unavailable"}

    await ensure_gist_audit_table(engine)

    # Ensure prerequisite tables exist for the query
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

    bounded_limit = max(1, min(limit, 100))

    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT g.id AS gist_id, g.memory_id, g.gist_text, "
                    "g.gist_method, g.source_content_hash, m.content AS memory_content "
                    "FROM memory_gists g "
                    "JOIN memories m ON m.id = g.memory_id "
                    "WHERE m.deprecated = 0 "
                    "  AND NOT EXISTS ( "
                    "    SELECT 1 FROM gist_audit_results a "
                    "    WHERE a.gist_id = g.id "
                    "      AND a.created_at >= datetime('now', '-' || :min_age_hours || ' hours') "
                    "  ) "
                    "ORDER BY g.created_at DESC "
                    "LIMIT :limit"
                ),
                {"limit": bounded_limit, "min_age_hours": min_age_hours},
            )
        ).fetchall()

    if not rows:
        return {"status": "completed", "audited": 0, "reason": "no_pending_gists"}

    audited = 0
    scores_coverage: List[float] = []
    scores_factual: List[float] = []
    scores_actionability: List[float] = []

    for row in rows:
        gist_id = row[0]
        memory_id = row[1]
        gist_text = row[2]
        gist_method = row[3]
        source_content_hash = row[4]
        source_text = row[5]

        result = await audit_gist(
            source_text=source_text or "",
            gist_text=gist_text or "",
            gist_method=gist_method or "unknown",
            llm_post_json=llm_post_json,
        )

        now = _utc_now().isoformat()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO gist_audit_results "
                    "(gist_id, memory_id, gist_method, "
                    " coverage_score, factual_preservation_score, "
                    " actionability_score, missing_anchors, "
                    " hallucination_flags, judge_model, "
                    " judge_raw_response, created_at, source_content_hash) "
                    "VALUES (:gist_id, :memory_id, :gist_method, "
                    " :coverage_score, :factual_preservation_score, "
                    " :actionability_score, :missing_anchors, "
                    " :hallucination_flags, :judge_model, "
                    " :judge_raw_response, :created_at, :source_content_hash)"
                ),
                {
                    "gist_id": gist_id,
                    "memory_id": memory_id,
                    "gist_method": gist_method or "unknown",
                    "coverage_score": result.get("coverage_score"),
                    "factual_preservation_score": result.get(
                        "factual_preservation_score"
                    ),
                    "actionability_score": result.get("actionability_score"),
                    "missing_anchors": json.dumps(
                        result.get("missing_anchors", [])
                    ),
                    "hallucination_flags": json.dumps(
                        result.get("hallucination_flags", [])
                    ),
                    "judge_model": "audit_judge",
                    "judge_raw_response": result.get("judge_raw_response", ""),
                    "created_at": now,
                    "source_content_hash": source_content_hash or "",
                },
            )

        audited += 1
        if result.get("coverage_score") is not None:
            scores_coverage.append(result["coverage_score"])
        if result.get("factual_preservation_score") is not None:
            scores_factual.append(result["factual_preservation_score"])
        if result.get("actionability_score") is not None:
            scores_actionability.append(result["actionability_score"])

    summary: Dict[str, Any] = {
        "status": "completed",
        "audited": audited,
    }
    if scores_coverage:
        summary["avg_coverage"] = round(
            sum(scores_coverage) / len(scores_coverage), 3
        )
    if scores_factual:
        summary["avg_factual"] = round(
            sum(scores_factual) / len(scores_factual), 3
        )
    if scores_actionability:
        summary["avg_actionability"] = round(
            sum(scores_actionability) / len(scores_actionability), 3
        )
    return summary


# ---------------------------------------------------------------------------
# Aggregated stats
# ---------------------------------------------------------------------------


async def get_gist_audit_stats(engine: Any) -> Dict[str, Any]:
    """Return aggregated audit statistics."""
    await ensure_gist_audit_table(engine)

    async with engine.begin() as conn:
        # Total audited
        total_row = (
            await conn.execute(
                text("SELECT COUNT(*) FROM gist_audit_results")
            )
        ).fetchone()
        total_audited = total_row[0] if total_row else 0

        if total_audited == 0:
            return {
                "total_audited": 0,
                "avg_coverage_score": None,
                "avg_factual_preservation_score": None,
                "avg_actionability_score": None,
                "common_missing_anchors": [],
                "hallucination_flag_count": 0,
                "method_breakdown": {},
                "last_audit_at": None,
            }

        # Average scores
        avg_row = (
            await conn.execute(
                text(
                    "SELECT "
                    "  AVG(coverage_score), "
                    "  AVG(factual_preservation_score), "
                    "  AVG(actionability_score) "
                    "FROM gist_audit_results "
                    "WHERE coverage_score IS NOT NULL"
                )
            )
        ).fetchone()

        avg_coverage = round(avg_row[0], 3) if avg_row and avg_row[0] is not None else None
        avg_factual = round(avg_row[1], 3) if avg_row and avg_row[1] is not None else None
        avg_actionability = round(avg_row[2], 3) if avg_row and avg_row[2] is not None else None

        # Last audit timestamp
        last_row = (
            await conn.execute(
                text(
                    "SELECT MAX(created_at) FROM gist_audit_results"
                )
            )
        ).fetchone()
        last_audit_at = last_row[0] if last_row else None

        # Hallucination flag count
        flag_rows = (
            await conn.execute(
                text(
                    "SELECT hallucination_flags FROM gist_audit_results "
                    "WHERE hallucination_flags IS NOT NULL "
                    "AND hallucination_flags != '[]'"
                )
            )
        ).fetchall()
        hallucination_flag_count = 0
        for fr in flag_rows:
            try:
                flags = json.loads(fr[0])
                hallucination_flag_count += len(flags)
            except (json.JSONDecodeError, TypeError):
                pass

        # Common missing anchors (top 5)
        anchor_rows = (
            await conn.execute(
                text(
                    "SELECT missing_anchors FROM gist_audit_results "
                    "WHERE missing_anchors IS NOT NULL "
                    "AND missing_anchors != '[]'"
                )
            )
        ).fetchall()
        anchor_counts: Dict[str, int] = {}
        for ar in anchor_rows:
            try:
                anchors = json.loads(ar[0])
                for a in anchors:
                    anchor_str = str(a).strip()
                    if anchor_str:
                        anchor_counts[anchor_str] = anchor_counts.get(anchor_str, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass
        common_missing_anchors = sorted(
            anchor_counts.items(), key=lambda x: x[1], reverse=True
        )[:5]
        common_missing_anchors = [a for a, _ in common_missing_anchors]

        # Method breakdown
        method_rows = (
            await conn.execute(
                text(
                    "SELECT gist_method, "
                    "  COUNT(*) AS cnt, "
                    "  AVG(coverage_score), "
                    "  AVG(factual_preservation_score), "
                    "  AVG(actionability_score) "
                    "FROM gist_audit_results "
                    "GROUP BY gist_method"
                )
            )
        ).fetchall()
        method_breakdown: Dict[str, Any] = {}
        for mr in method_rows:
            method_breakdown[mr[0]] = {
                "count": mr[1],
                "avg_coverage": round(mr[2], 3) if mr[2] is not None else None,
                "avg_factual": round(mr[3], 3) if mr[3] is not None else None,
                "avg_actionability": round(mr[4], 3) if mr[4] is not None else None,
            }

    return {
        "total_audited": total_audited,
        "avg_coverage_score": avg_coverage,
        "avg_factual_preservation_score": avg_factual,
        "avg_actionability_score": avg_actionability,
        "common_missing_anchors": common_missing_anchors,
        "hallucination_flag_count": hallucination_flag_count,
        "method_breakdown": method_breakdown,
        "last_audit_at": last_audit_at,
    }
