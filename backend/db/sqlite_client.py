"""
SQLite Client for Memory Palace System

This module implements the SQLite-based memory storage with:
- Path-based addressing (mem://path/to/memory)
- Version control via deprecated flag
- Multiple paths (aliases) pointing to same memory
"""

import asyncio
import logging
import os
import re
import json
import math
import hashlib
import sqlite3
import time
import httpx
import unicodedata
from pathlib import Path as FilePath
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple, Sequence, Mapping
from contextlib import asynccontextmanager
from urllib.parse import unquote

from filelock import AsyncFileLock, Timeout as FileLockTimeout

from env_utils import (
    env_bool as shared_env_bool,
    env_float as shared_env_float,
    env_int as shared_env_int,
    parse_iso_datetime as shared_parse_iso_datetime,
)
from sqlalchemy import (
    create_engine,
    select,
    update,
    delete,
    func,
    and_,
    or_,
    text,
    event,
    tuple_,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.exc import (
    IntegrityError as SQLAlchemyIntegrityError,
    OperationalError as SQLAlchemyOperationalError,
)
from dotenv import load_dotenv
from runtime_env import should_load_project_dotenv
from .migration_runner import apply_pending_migrations
from .sqlite_client_retrieval import SQLiteClientRetrievalMixin
from .sqlite_models import (
    AutoPathCounter,
    Base,
    EmbeddingCache,
    IndexMeta,
    Memory,
    MemoryChunk,
    MemoryChunkVec,
    MemoryGist,
    MemoryTag,
    Path,
    SchemaMigration,
)
from .sqlite_paths import (
    _extract_sqlite_file_path,
    _normalize_sqlite_database_url,
    _register_sqlite_adapters,
    _resolve_init_lock_path,
    _utc_now,
    _utc_now_naive,
    is_valid_memory_path_segment,
    memory_path_segment_error_message,
)

# Load environment variables from project root only.
# When the plugin provides an isolated runtime env file, keep that runtime env as
# the source of truth instead of rehydrating project-root defaults.
_current_dir = os.path.dirname(os.path.abspath(__file__))
_backend_dir = os.path.dirname(_current_dir)
_project_root = os.path.dirname(_backend_dir)
_dotenv_path = os.path.join(_project_root, ".env")
_runtime_env_path = str(os.getenv("OPENCLAW_MEMORY_PALACE_ENV_FILE") or "").strip()
if should_load_project_dotenv(_dotenv_path, runtime_env_path=_runtime_env_path):
    load_dotenv(_dotenv_path)

_INIT_DB_LOCK_RETRY_ATTEMPTS = 3
_INIT_DB_LOCK_RETRY_BASE_DELAY_SEC = 0.5
_RERANKER_REQUEST_MAX_ATTEMPTS = 2
_RERANKER_REQUEST_BASE_BACKOFF_SEC = 0.25
_CJK_NEGATING_PREFIX_PATTERN = r"[不没無无非别莫未]"
_ENV_CONFLICT_WARNINGS_EMITTED: set[Tuple[str, ...]] = set()

_register_sqlite_adapters()
logger = logging.getLogger(__name__)

_SEARCH_ASCII_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_]+")
_SEARCH_LITERAL_TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9_]+|[\u3400-\u9FFF]{2,}")
_FTS_RESERVED_TOKENS = {"and", "or", "not", "near"}
_AUTO_PATH_RETRY_ATTEMPTS = 5
_AUTO_PATH_RETRY_BASE_DELAY_SEC = 0.01
_CREATE_MEMORY_LOCK_RETRY_ATTEMPTS = 5
_CREATE_MEMORY_LOCK_RETRY_BASE_DELAY_SEC = 0.05
_DEFAULT_VALID_DOMAINS = ("core", "writer", "game", "notes", "system")
_READ_ONLY_DOMAINS = {"system"}
_INTENT_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "temporal": (
        "when",
        "timeline",
        "history",
        "before",
        "after",
        "recent",
        "recently",
        "latest",
        "yesterday",
        "today",
        "ago",
        "last",
        "happened",
        "since",
        "during",
        "earlier",
        "previously",
        "prior",
        "昨天",
        "最近",
        "之前",
        "之后",
        "时间",
        "上次",
        "何时",
        "什么时候",
        "上周",
        "今天早上",
        "以前",
        "从那以后",
        "历史",
        "变化",
        "变更",
        "改动",
    ),
    "causal": (
        "why",
        "cause",
        "caused",
        "causes",
        "because",
        "reason",
        "reasons",
        "root cause",
        "fault",
        "problem",
        "problems",
        "fail",
        "failure",
        "failures",
        "failed",
        "failing",
        "broke",
        "broken",
        "bug",
        "bugs",
        "error",
        "errors",
        "issue",
        "issues",
        "debug",
        "wrong",
        "crash",
        "crashed",
        "导致",
        "原因",
        "因果",
        "为什么",
        "故障",
        "失败",
        "出错",
        "怎么回事",
        "有问题",
        "什么问题",
        "出了什么",
    ),
    "exploratory": (
        "explore",
        "brainstorm",
        "ideas",
        "compare",
        "alternatives",
        "alternative",
        "options",
        "tradeoff",
        "tradeoffs",
        "suggest",
        "suggestion",
        "suggestions",
        "evaluate",
        "review",
        "approaches",
        "approach",
        "strategies",
        "strategy",
        "recommend",
        "recommendation",
        "pros and cons",
        "可能",
        "探索",
        "方案",
        "对比",
        "建议",
        "比较",
        "选择",
        "优缺点",
        "优化",
        "改进",
    ),
}

# Implicit causal/temporal patterns: match when no keyword wins but query
# contains structural signals (e.g. "what went wrong", "debug: X broken").
# REGRESSION GUARD (2026-04-06): "怎么了$" must stay in CAUSAL, not TEMPORAL.
# Reason: "X怎么了" = "what happened to X" (causal inquiry). Without an explicit
# time anchor it must not trigger recency-heavy scoring (temporal_time_filtered
# uses recency=0.38 which drowns out vector signal on equal-age corpora).
# Evidence: HQ11 benchmark — moving it to temporal caused rank-1 target to vanish.
_CAUSAL_IMPLICIT_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bwent wrong\b", re.IGNORECASE),
    re.compile(r"\bwhat.{0,15}(issue|error|bug|problem)\b", re.IGNORECASE),
    re.compile(r"\bdebug\b.*\b(broken|degraded|failing|slow|timing out)\b", re.IGNORECASE),
    re.compile(r"出了?什么(问题|错|事)"),
    re.compile(r"怎么回事"),
    # "怎么了" = "what happened to X" — causal inquiry, not temporal.
    # Without an explicit time anchor it should not trigger recency-heavy scoring.
    re.compile(r"怎么了$"),
)
_TEMPORAL_IMPLICIT_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bwhat happened\b", re.IGNORECASE),
    re.compile(r"\bthis morning\b", re.IGNORECASE),
    re.compile(r"发生了什么"),
)


def _parse_valid_domains(raw_value: str | None) -> tuple[str, ...]:
    parsed: list[str] = []
    source = str(raw_value or "").strip()
    if not source:
        source = ",".join(_DEFAULT_VALID_DOMAINS)
    for value in source.split(","):
        candidate = value.strip().lower()
        if candidate and candidate not in parsed:
            parsed.append(candidate)
    for read_only_domain in sorted(_READ_ONLY_DOMAINS):
        if read_only_domain not in parsed:
            parsed.append(read_only_domain)
    return tuple(parsed or _DEFAULT_VALID_DOMAINS)


# =============================================================================
# SQLite Client
# =============================================================================


class SQLiteClient(SQLiteClientRetrievalMixin):
    """
    Async SQLite client for memory operations.

    Core operations:
    - read: Get memory by path
    - create: New memory with auto-generated or specified path segment
    - update: Create new version, deprecate old, repoint path
    - add_path: Create alias to existing memory
    - search: Substring search on path and content
    """

    def __init__(self, database_url: str):
        """
        Initialize the SQLite client.

        Args:
            database_url: SQLAlchemy async URL, e.g.
                         "sqlite+aiosqlite:///memory_palace.db"
        """
        self.database_url = _normalize_sqlite_database_url(database_url)
        self._database_file = _extract_sqlite_file_path(database_url)
        self._init_lock_path = _resolve_init_lock_path(self._database_file)
        self._init_lock_timeout_seconds = max(
            0.0, float(os.getenv("DB_INIT_LOCK_TIMEOUT_SEC", "30") or "30")
        )
        self.engine = create_async_engine(self.database_url, echo=False)
        self._runtime_write_wal_enabled = self._env_bool("RUNTIME_WRITE_WAL_ENABLED", False)
        self._runtime_write_journal_mode_requested = (
            self._normalize_runtime_write_journal_mode(
                os.getenv("RUNTIME_WRITE_JOURNAL_MODE", "delete"),
                wal_enabled=self._runtime_write_wal_enabled,
            )
        )
        self._runtime_write_wal_synchronous_requested = (
            self._normalize_runtime_write_wal_synchronous(
                os.getenv("RUNTIME_WRITE_WAL_SYNCHRONOUS", "normal")
            )
        )
        self._runtime_write_busy_timeout_ms = max(
            1, self._env_int("RUNTIME_WRITE_BUSY_TIMEOUT_MS", 120)
        )
        self._runtime_write_wal_autocheckpoint = max(
            1, self._env_int("RUNTIME_WRITE_WAL_AUTOCHECKPOINT", 1000)
        )
        self._runtime_write_journal_mode_effective = "delete"
        self._runtime_write_wal_synchronous_effective = "default"
        self._runtime_write_busy_timeout_effective_ms = int(
            self._runtime_write_busy_timeout_ms
        )
        self._runtime_write_wal_autocheckpoint_effective = int(
            self._runtime_write_wal_autocheckpoint
        )
        self._runtime_write_pragma_status = "pending"
        self._runtime_write_pragma_error = ""
        self._register_runtime_write_pragma_hook()
        self.async_session = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
        self._read_only_domains = frozenset(_READ_ONLY_DOMAINS)
        self._valid_domains = _parse_valid_domains(os.getenv("VALID_DOMAINS"))
        self._embedding_backend = (
            os.getenv("RETRIEVAL_EMBEDDING_BACKEND", "hash").strip().lower() or "hash"
        )
        self._embedding_model = (
            self._first_env(
                [
                    "RETRIEVAL_EMBEDDING_MODEL",
                    "ROUTER_EMBEDDING_MODEL",
                    "OPENAI_EMBEDDING_MODEL",
                ],
                default="hash-v1",
            )
            or "hash-v1"
        )
        self._embedding_provider_chain_enabled = self._env_bool(
            "EMBEDDING_PROVIDER_CHAIN_ENABLED", False
        )
        self._embedding_provider_fail_open = self._env_bool(
            "EMBEDDING_PROVIDER_FAIL_OPEN", False
        )
        self._embedding_provider_fallback = (
            str(os.getenv("EMBEDDING_PROVIDER_FALLBACK") or "hash").strip().lower()
            or "hash"
        )
        self._embedding_api_base = self._resolve_embedding_api_base(
            self._embedding_backend
        )
        self._embedding_api_key = self._resolve_embedding_api_key(
            self._embedding_backend
        )
        self._embedding_provider_candidates = self._build_embedding_provider_candidates()
        self._embedding_dim = max(16, self._env_int("RETRIEVAL_EMBEDDING_DIM", 64))
        self._remote_http_timeout_sec = max(
            1.0, self._env_float("RETRIEVAL_REMOTE_TIMEOUT_SEC", 8.0)
        )
        self._remote_http_client: Optional[httpx.AsyncClient] = None
        self._remote_http_client_guard = asyncio.Lock()
        self._reranker_enabled = self._env_bool("RETRIEVAL_RERANKER_ENABLED", False)
        self._reranker_api_base = self._normalize_reranker_api_base(
            self._first_env_with_conflict_warning(
                [
                    "RETRIEVAL_RERANKER_API_BASE",
                    "RETRIEVAL_RERANKER_BASE",
                    "ROUTER_API_BASE",
                    "OPENAI_BASE_URL",
                    "OPENAI_API_BASE",
                ],
                label="Reranker API base",
            )
        )
        self._reranker_api_key = self._first_env(
            [
                "RETRIEVAL_RERANKER_API_KEY",
                "RETRIEVAL_RERANKER_KEY",
                "ROUTER_API_KEY",
                "OPENAI_API_KEY",
            ]
        )
        self._reranker_provider = self._normalize_reranker_provider(
            self._first_env(
                [
                    "RETRIEVAL_RERANKER_PROVIDER",
                    "ROUTER_RERANKER_PROVIDER",
                ]
            )
        )
        self._reranker_model = self._first_env(
            ["RETRIEVAL_RERANKER_MODEL", "ROUTER_RERANKER_MODEL"]
        )
        self._reranker_small_batch_max_documents = max(
            0,
            self._env_int("RETRIEVAL_RERANKER_SMALL_BATCH_MAX_DOCUMENTS", 25),
        )
        self._reranker_small_batch_api_base = self._normalize_reranker_api_base(
            self._first_env(["RETRIEVAL_RERANKER_SMALL_BATCH_API_BASE"])
        )
        self._reranker_small_batch_api_key = self._first_env(
            ["RETRIEVAL_RERANKER_SMALL_BATCH_API_KEY"],
            default=self._reranker_api_key,
        )
        self._reranker_small_batch_provider = self._normalize_reranker_provider(
            self._first_env(
                ["RETRIEVAL_RERANKER_SMALL_BATCH_PROVIDER"],
                default=self._reranker_provider,
            )
        )
        self._reranker_small_batch_model = self._first_env(
            ["RETRIEVAL_RERANKER_SMALL_BATCH_MODEL"],
            default=self._reranker_model,
        )
        self._reranker_fallback_api_base = self._normalize_reranker_api_base(
            self._first_env(
                [
                    "RETRIEVAL_RERANKER_FALLBACK_API_BASE",
                    "RETRIEVAL_RERANKER_SECONDARY_API_BASE",
                ]
            )
        )
        self._reranker_fallback_api_key = self._first_env(
            [
                "RETRIEVAL_RERANKER_FALLBACK_API_KEY",
                "RETRIEVAL_RERANKER_SECONDARY_API_KEY",
            ]
        )
        self._reranker_fallback_provider = self._normalize_reranker_provider(
            self._first_env(
                [
                    "RETRIEVAL_RERANKER_FALLBACK_PROVIDER",
                    "RETRIEVAL_RERANKER_SECONDARY_PROVIDER",
                    "RETRIEVAL_RERANKER_PROVIDER",
                ]
            )
        )
        self._reranker_fallback_model = self._first_env(
            [
                "RETRIEVAL_RERANKER_FALLBACK_MODEL",
                "RETRIEVAL_RERANKER_SECONDARY_MODEL",
            ]
        )
        self._rerank_weight = min(
            1.0, max(0.0, self._env_float("RETRIEVAL_RERANKER_WEIGHT", 0.25))
        )
        self._factual_candidate_multiplier_cap = self._env_int(
            "RETRIEVAL_FACTUAL_CANDIDATE_MULTIPLIER_CAP",
            2,
        )
        self._search_hard_max_candidate_multiplier = self._env_int(
            "SEARCH_HARD_MAX_CANDIDATE_MULTIPLIER",
            50,
        )
        self._search_hard_max_candidate_multiplier = max(
            1, self._search_hard_max_candidate_multiplier
        )
        self._write_guard_semantic_noop_threshold = min(
            1.0,
            max(
                0.0,
                self._env_float("WRITE_GUARD_SEMANTIC_NOOP_THRESHOLD", 0.92),
            ),
        )
        self._write_guard_semantic_update_threshold = min(
            self._write_guard_semantic_noop_threshold,
            max(
                0.0,
                self._env_float("WRITE_GUARD_SEMANTIC_UPDATE_THRESHOLD", 0.78),
            ),
        )
        self._write_guard_keyword_noop_threshold = min(
            1.0,
            max(
                0.0,
                self._env_float("WRITE_GUARD_KEYWORD_NOOP_THRESHOLD", 0.82),
            ),
        )
        self._write_guard_keyword_update_threshold = min(
            self._write_guard_keyword_noop_threshold,
            max(
                0.0,
                self._env_float("WRITE_GUARD_KEYWORD_UPDATE_THRESHOLD", 0.55),
            ),
        )
        self._write_guard_single_pipeline_semantic_floor = min(
            self._write_guard_semantic_update_threshold,
            max(
                0.0,
                self._env_float(
                    "WRITE_GUARD_SINGLE_PIPELINE_SEMANTIC_FLOOR",
                    max(0.0, self._write_guard_semantic_update_threshold - 0.05),
                ),
            ),
        )
        self._write_guard_single_pipeline_keyword_floor = min(
            self._write_guard_keyword_update_threshold,
            max(
                0.0,
                self._env_float(
                    "WRITE_GUARD_SINGLE_PIPELINE_KEYWORD_FLOOR",
                    max(0.0, self._write_guard_keyword_update_threshold - 0.05),
                ),
            ),
        )
        self._write_guard_score_normalization = self._env_bool(
            "WRITE_GUARD_SCORE_NORMALIZATION",
            self._embedding_backend not in {"hash", ""},
        )
        self._write_guard_cross_check_add_floor = min(
            1.0,
            max(
                0.0,
                self._env_float("WRITE_GUARD_CROSS_CHECK_ADD_FLOOR", 0.10),
            ),
        )
        self._write_guard_normalization_floor = max(
            0.0,
            min(
                0.99,
                self._env_float("WRITE_GUARD_NORMALIZATION_FLOOR", 0.85),
            ),
        )
        self._reranker_timeout_sec = max(
            0.1,
            self._env_float(
                "RETRIEVAL_RERANKER_TIMEOUT_SEC",
                self._remote_http_timeout_sec,
            ),
        )
        self._reranker_small_batch_timeout_sec = max(
            0.1,
            self._env_float(
                "RETRIEVAL_RERANKER_SMALL_BATCH_TIMEOUT_SEC",
                self._reranker_timeout_sec,
            ),
        )
        self._reranker_fallback_timeout_sec = max(
            0.1,
            self._env_float(
                "RETRIEVAL_RERANKER_FALLBACK_TIMEOUT_SEC",
                self._reranker_timeout_sec,
            ),
        )
        self._reranker_top_n = max(
            0, self._env_int("RETRIEVAL_RERANK_TOP_N", 48)
        )
        self._reranker_group_by_memory = self._env_bool(
            "RETRIEVAL_RERANK_GROUP_BY_MEMORY", True
        )
        self._semantic_overfetch_factor = max(
            1, self._env_int("RETRIEVAL_SEMANTIC_OVERFETCH_FACTOR", 3)
        )
        self._chunk_size = max(128, self._env_int("RETRIEVAL_CHUNK_SIZE", 500))
        requested_chunk_overlap = self._env_int("RETRIEVAL_CHUNK_OVERLAP", 80)
        self._chunk_overlap = max(0, min(self._chunk_size - 1, requested_chunk_overlap))
        if requested_chunk_overlap >= self._chunk_size:
            logger.warning(
                "RETRIEVAL_CHUNK_OVERLAP=%s exceeds effective RETRIEVAL_CHUNK_SIZE=%s; clamped to %s",
                requested_chunk_overlap,
                self._chunk_size,
                self._chunk_overlap,
            )
        self._weight_vector = self._env_float("RETRIEVAL_HYBRID_SEMANTIC_WEIGHT", 0.7)
        self._weight_text = self._env_float("RETRIEVAL_HYBRID_KEYWORD_WEIGHT", 0.3)
        self._weight_priority = self._env_float("RETRIEVAL_WEIGHT_PRIORITY", 0.1)
        self._weight_recency = self._env_float("RETRIEVAL_WEIGHT_RECENCY", 0.06)
        self._weight_path_prefix = self._env_float("RETRIEVAL_WEIGHT_PATH_PREFIX", 0.04)
        self._weight_vitality = self._env_float("RETRIEVAL_WEIGHT_VITALITY", 0.08)
        self._weight_access = self._env_float("RETRIEVAL_WEIGHT_ACCESS", 0.05)
        self._weight_pending_event = self._env_float(
            "RETRIEVAL_WEIGHT_PENDING_EVENT", 0.18
        )
        self._weight_length_norm = self._env_float(
            "RETRIEVAL_WEIGHT_LENGTH_NORM", 0.03
        )
        self._recency_half_life_days = max(
            1.0, self._env_float("RETRIEVAL_RECENCY_HALF_LIFE_DAYS", 30.0)
        )
        self._access_half_life_days = max(
            1.0, self._env_float("RETRIEVAL_ACCESS_HALF_LIFE_DAYS", 14.0)
        )
        self._gist_recall_enabled = self._env_bool(
            "RETRIEVAL_GIST_RECALL_ENABLED", True
        )
        self._collapse_same_uri_results = self._env_bool(
            "RETRIEVAL_COLLAPSE_SAME_URI", True
        )
        self._mmr_enabled = self._env_bool("RETRIEVAL_MMR_ENABLED", False)
        self._mmr_lambda = min(1.0, max(0.0, self._env_float("RETRIEVAL_MMR_LAMBDA", 0.65)))
        self._mmr_candidate_factor = max(
            1, self._env_int("RETRIEVAL_MMR_CANDIDATE_FACTOR", 3)
        )
        self._intent_llm_enabled = self._env_bool("INTENT_LLM_ENABLED", False)
        self._intent_llm_api_base = self._normalize_chat_api_base(
            self._first_env_with_conflict_warning(
                [
                    "INTENT_LLM_API_BASE",
                    "LLM_RESPONSES_URL",
                    "OPENAI_BASE_URL",
                    "OPENAI_API_BASE",
                    "ROUTER_API_BASE",
                ],
                label="Intent LLM API base",
            )
        )
        self._intent_llm_api_key = self._first_env(
            [
                "INTENT_LLM_API_KEY",
                "LLM_API_KEY",
                "OPENAI_API_KEY",
                "ROUTER_API_KEY",
            ]
        )
        self._intent_llm_model = self._first_env(
            [
                "INTENT_LLM_MODEL",
                "LLM_MODEL_NAME",
                "OPENAI_MODEL",
                "ROUTER_CHAT_MODEL",
            ]
        )
        self._vitality_max_score = max(
            0.1, self._env_float("VITALITY_MAX_SCORE", 3.0)
        )
        self._vitality_reinforce_delta = max(
            0.0, self._env_float("VITALITY_REINFORCE_DELTA", 0.08)
        )
        self._vitality_decay_half_life_days = max(
            1.0, self._env_float("VITALITY_DECAY_HALF_LIFE_DAYS", 30.0)
        )
        self._vitality_decay_min_score = max(
            0.0, self._env_float("VITALITY_DECAY_MIN_SCORE", 0.05)
        )
        self._vitality_cleanup_threshold = max(
            0.0, self._env_float("VITALITY_CLEANUP_THRESHOLD", 0.35)
        )
        self._vitality_cleanup_inactive_days = max(
            0.0, self._env_float("VITALITY_CLEANUP_INACTIVE_DAYS", 14.0)
        )
        self._vitality_temporal_decay_in_retrieval = self._env_bool(
            "RETRIEVAL_VITALITY_TEMPORAL_DECAY_ENABLED", True
        )
        self._fts_available = False
        self._gist_fts_available = False
        self._vector_available = self._embedding_backend not in {
            "none",
            "off",
            "disabled",
            "false",
            "0",
        }
        self._sqlite_vec_enabled = self._env_bool("RETRIEVAL_SQLITE_VEC_ENABLED", False)
        self._sqlite_vec_extension_path = self._first_env(
            ["RETRIEVAL_SQLITE_VEC_EXTENSION_PATH"]
        )
        self._vector_engine_requested = self._normalize_vector_engine(
            os.getenv("RETRIEVAL_VECTOR_ENGINE", "legacy")
        )
        self._sqlite_vec_read_ratio = min(
            100, max(0, self._env_int("RETRIEVAL_SQLITE_VEC_READ_RATIO", 0))
        )
        self._sqlite_vec_capability: Dict[str, Any] = {
            "status": "disabled",
            "sqlite_vec_readiness": "hold",
            "diag_code": "sqlite_vec_disabled",
            "extension_path_input": self._sqlite_vec_extension_path,
            "extension_path": "",
            "extension_loaded": False,
            "extension_path_exists": False,
        }
        self._sqlite_vec_knn_table = "memory_chunks_vec0"
        self._sqlite_vec_knn_ready = False
        self._sqlite_vec_knn_dim = max(16, int(self._embedding_dim))
        self._semantic_vector_block_reason = ""
        self._semantic_vector_stored_dim: Optional[int] = None
        self._semantic_vector_detected_dims: List[int] = []
        self._vector_engine_effective = "legacy"

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        minimum = default if default < 0 else 0
        return shared_env_int(name, default, minimum=minimum)

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        minimum = default if default < 0 else 0.0
        return shared_env_float(name, default, minimum=minimum)

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        return shared_env_bool(name, default)

    @staticmethod
    def _first_env(names: List[str], default: str = "") -> str:
        for name in names:
            value = os.getenv(name)
            if value is None:
                continue
            candidate = value.strip()
            if candidate:
                return candidate
        return default

    @staticmethod
    def _first_env_with_conflict_warning(
        names: List[str], default: str = "", *, label: str
    ) -> str:
        populated: List[Tuple[str, str]] = []
        for name in names:
            value = os.getenv(name)
            if value is None:
                continue
            candidate = value.strip()
            if candidate:
                populated.append((name, candidate))
        if not populated:
            return default

        selected_name, selected_value = populated[0]
        distinct_values = {value for _, value in populated}
        warning_key = tuple(names)
        if len(distinct_values) > 1 and warning_key not in _ENV_CONFLICT_WARNINGS_EMITTED:
            ignored_names = ", ".join(name for name, _ in populated[1:])
            logger.warning(
                "%s resolved from %s; ignoring also-set env vars: %s",
                label,
                selected_name,
                ignored_names,
            )
            _ENV_CONFLICT_WARNINGS_EMITTED.add(warning_key)
        return selected_value

    @staticmethod
    def _normalize_runtime_write_journal_mode(
        value: Optional[str], *, wal_enabled: bool
    ) -> str:
        mode = str(value or "delete").strip().lower() or "delete"
        if mode == "wal" and wal_enabled:
            return "wal"
        return "delete"

    @staticmethod
    def _normalize_runtime_write_wal_synchronous(value: Optional[str]) -> str:
        mode = str(value or "normal").strip().lower() or "normal"
        numeric_map = {
            "0": "off",
            "1": "normal",
            "2": "full",
            "3": "extra",
        }
        mode = numeric_map.get(mode, mode)
        if mode in {"off", "normal", "full", "extra"}:
            return mode
        return "normal"

    def _register_runtime_write_pragma_hook(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def _on_connect(dbapi_connection, _connection_record) -> None:
            self._apply_runtime_write_pragmas(dbapi_connection)
            self._register_unicode_search_functions(dbapi_connection)
            self._load_sqlite_vec_extension_on_connect(dbapi_connection)

    @staticmethod
    def _unicode_search_fold(value: Any) -> str:
        normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
        return "".join(
            char for char in normalized if unicodedata.category(char) != "Mn"
        )

    def _register_unicode_search_functions(self, dbapi_connection) -> None:
        try:
            dbapi_connection.create_function(
                "unicode_search_fold", 1, self._unicode_search_fold
            )
        except Exception:
            # Search fallback still works without this helper, so registration
            # must stay best-effort for older sqlite bindings.
            pass

    def _apply_runtime_write_pragmas(self, dbapi_connection) -> None:
        status = "disabled"
        error = ""
        journal_mode_effective = "delete"
        wal_synchronous_effective = "default"
        busy_timeout_effective = int(self._runtime_write_busy_timeout_ms)
        wal_autocheckpoint_effective = int(self._runtime_write_wal_autocheckpoint)

        requested_mode = (
            "wal"
            if (
                self._runtime_write_wal_enabled
                and self._runtime_write_journal_mode_requested == "wal"
            )
            else "delete"
        )
        _SAFE_JOURNAL_MODES = {"wal", "delete"}
        _SAFE_SYNCHRONOUS_MODES = {"off", "normal", "full", "extra"}

        cursor = None
        try:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            safe_busy_timeout = max(1, int(self._runtime_write_busy_timeout_ms))
            cursor.execute(f"PRAGMA busy_timeout={safe_busy_timeout}")
            cursor.execute("PRAGMA busy_timeout")
            busy_timeout_row = cursor.fetchone()
            if busy_timeout_row and busy_timeout_row[0] is not None:
                busy_timeout_effective = max(1, int(busy_timeout_row[0]))

            if requested_mode.lower() not in _SAFE_JOURNAL_MODES:
                requested_mode = "delete"
            cursor.execute(f"PRAGMA journal_mode={requested_mode.upper()}")
            journal_mode_row = cursor.fetchone()
            if journal_mode_row and journal_mode_row[0] is not None:
                journal_mode_effective = (
                    str(journal_mode_row[0]).strip().lower() or "delete"
                )
            else:
                journal_mode_effective = requested_mode

            if requested_mode == "wal":
                if journal_mode_effective != "wal":
                    status = "fallback_delete"
                    error = f"journal_mode_unavailable:{journal_mode_effective}"
                    cursor.execute("PRAGMA journal_mode=DELETE")
                    delete_mode_row = cursor.fetchone()
                    if delete_mode_row and delete_mode_row[0] is not None:
                        journal_mode_effective = (
                            str(delete_mode_row[0]).strip().lower() or "delete"
                        )
                    else:
                        journal_mode_effective = "delete"
                else:
                    status = "enabled"
                    sync_target = self._runtime_write_wal_synchronous_requested
                    if sync_target.lower() not in _SAFE_SYNCHRONOUS_MODES:
                        sync_target = "normal"
                    cursor.execute(f"PRAGMA synchronous={sync_target.upper()}")
                    cursor.execute("PRAGMA synchronous")
                    sync_row = cursor.fetchone()
                    if sync_row and sync_row[0] is not None:
                        wal_synchronous_effective = (
                            self._normalize_runtime_write_wal_synchronous(
                                str(sync_row[0])
                            )
                        )
                    else:
                        wal_synchronous_effective = sync_target
                    safe_autocheckpoint = max(1, int(self._runtime_write_wal_autocheckpoint))
                    cursor.execute(f"PRAGMA wal_autocheckpoint={safe_autocheckpoint}")
                    cursor.execute("PRAGMA wal_autocheckpoint")
                    wal_checkpoint_row = cursor.fetchone()
                    if wal_checkpoint_row and wal_checkpoint_row[0] is not None:
                        wal_autocheckpoint_effective = max(
                            1, int(wal_checkpoint_row[0])
                        )
            else:
                status = "disabled"
        except Exception as exc:
            status = "fallback_delete"
            error = f"pragma_apply_failed:{type(exc).__name__}"
            try:
                if cursor is None:
                    cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=DELETE")
                delete_mode_row = cursor.fetchone()
                if delete_mode_row and delete_mode_row[0] is not None:
                    journal_mode_effective = (
                        str(delete_mode_row[0]).strip().lower() or "delete"
                    )
                else:
                    journal_mode_effective = "delete"
            except Exception as rollback_exc:
                status = "error"
                suffix = f"journal_mode_reset_failed:{type(rollback_exc).__name__}"
                error = f"{error};{suffix}" if error else suffix
                journal_mode_effective = "unknown"
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            self._runtime_write_journal_mode_effective = (
                "wal" if journal_mode_effective == "wal" else "delete"
            )
            if self._runtime_write_journal_mode_effective == "wal":
                self._runtime_write_wal_synchronous_effective = (
                    self._normalize_runtime_write_wal_synchronous(
                        wal_synchronous_effective
                    )
                )
            else:
                self._runtime_write_wal_synchronous_effective = "default"
            self._runtime_write_busy_timeout_effective_ms = int(
                max(1, busy_timeout_effective)
            )
            self._runtime_write_wal_autocheckpoint_effective = int(
                max(1, wal_autocheckpoint_effective)
            )
            self._runtime_write_pragma_status = status
            self._runtime_write_pragma_error = error
            if requested_mode == "wal" and status in {"fallback_delete", "error"}:
                logger.warning(
                    "Runtime write WAL unavailable; falling back to %s "
                    "(status=%s, error=%s)",
                    self._runtime_write_journal_mode_effective,
                    status,
                    error or "none",
                )

    def _load_sqlite_vec_extension_on_connect(self, dbapi_connection) -> None:
        """
        Best-effort sqlite-vec extension loading for each SQLite connection.

        This hook is intentionally fail-closed/safe: if loading is unavailable
        or fails, retrieval will naturally fall back to legacy scoring.
        """
        if not self._sqlite_vec_enabled:
            return

        extension_input = str(self._sqlite_vec_extension_path or "").strip()
        if not extension_input:
            return

        resolved_extension = self._resolve_sqlite_extension_file(extension_input)
        if resolved_extension is None:
            return
        if not resolved_extension.is_file():
            return
        extension_path = str(resolved_extension)

        enable_sync = getattr(dbapi_connection, "enable_load_extension", None)
        load_sync = getattr(dbapi_connection, "load_extension", None)
        if callable(enable_sync) and callable(load_sync):
            try:
                enable_sync(True)
            except Exception:
                return
            try:
                load_sync(extension_path)
            except Exception:
                # Keep safe degradation path to legacy vector scoring.
                pass
            finally:
                try:
                    enable_sync(False)
                except Exception:
                    pass
            return

        awaiter = getattr(dbapi_connection, "await_", None)
        driver_connection = getattr(dbapi_connection, "driver_connection", None)
        enable_async = (
            getattr(driver_connection, "enable_load_extension", None)
            if driver_connection is not None
            else None
        )
        load_async = (
            getattr(driver_connection, "load_extension", None)
            if driver_connection is not None
            else None
        )
        if not (callable(awaiter) and callable(enable_async) and callable(load_async)):
            return

        try:
            awaiter(enable_async(True))
        except Exception:
            return
        try:
            awaiter(load_async(extension_path))
        except Exception:
            pass
        finally:
            try:
                awaiter(enable_async(False))
            except Exception:
                pass

    def _resolve_embedding_api_base(self, backend: str) -> str:
        backend_value = (backend or "").strip().lower()
        if backend_value == "router":
            return self._normalize_embedding_api_base(
                self._first_env_with_conflict_warning(
                    [
                        "ROUTER_API_BASE",
                        "RETRIEVAL_EMBEDDING_API_BASE",
                        "RETRIEVAL_EMBEDDING_BASE",
                    ],
                    label="Embedding API base",
                )
            )
        if backend_value == "openai":
            return self._normalize_embedding_api_base(
                self._first_env_with_conflict_warning(
                    [
                        "OPENAI_BASE_URL",
                        "OPENAI_API_BASE",
                        "RETRIEVAL_EMBEDDING_API_BASE",
                        "RETRIEVAL_EMBEDDING_BASE",
                    ],
                    label="Embedding API base",
                )
            )
        return self._normalize_embedding_api_base(
            self._first_env_with_conflict_warning(
                [
                    "RETRIEVAL_EMBEDDING_API_BASE",
                    "RETRIEVAL_EMBEDDING_BASE",
                    "ROUTER_API_BASE",
                    "OPENAI_BASE_URL",
                    "OPENAI_API_BASE",
                ],
                label="Embedding API base",
            )
        )

    def _resolve_embedding_api_key(self, backend: str) -> str:
        backend_value = (backend or "").strip().lower()
        if backend_value == "router":
            return self._first_env(
                ["ROUTER_API_KEY", "RETRIEVAL_EMBEDDING_API_KEY", "RETRIEVAL_EMBEDDING_KEY"]
            )
        if backend_value == "openai":
            return self._first_env(
                ["OPENAI_API_KEY", "RETRIEVAL_EMBEDDING_API_KEY", "RETRIEVAL_EMBEDDING_KEY"]
            )
        return self._first_env(
            ["RETRIEVAL_EMBEDDING_API_KEY", "RETRIEVAL_EMBEDDING_KEY", "ROUTER_API_KEY", "OPENAI_API_KEY"]
        )

    def _resolve_embedding_model(self, backend: str) -> str:
        backend_value = (backend or "").strip().lower()
        if backend_value == "router":
            return (
                self._first_env(
                    [
                        "ROUTER_EMBEDDING_MODEL",
                        "RETRIEVAL_EMBEDDING_MODEL",
                        "OPENAI_EMBEDDING_MODEL",
                    ],
                    default=self._embedding_model,
                )
                or self._embedding_model
            )
        if backend_value == "openai":
            return (
                self._first_env(
                    [
                        "OPENAI_EMBEDDING_MODEL",
                        "RETRIEVAL_EMBEDDING_MODEL",
                        "ROUTER_EMBEDDING_MODEL",
                    ],
                    default=self._embedding_model,
                )
                or self._embedding_model
            )
        return (
            self._first_env(
                [
                    "RETRIEVAL_EMBEDDING_MODEL",
                    "OPENAI_EMBEDDING_MODEL",
                    "ROUTER_EMBEDDING_MODEL",
                ],
                default=self._embedding_model,
            )
            or self._embedding_model
        )

    def _resolve_chain_fallback_backend(self) -> str:
        value = (self._embedding_provider_fallback or "hash").strip().lower()
        if value in {
            "api",
            "router",
            "openai",
            "hash",
            "none",
            "off",
            "disabled",
            "false",
            "0",
        }:
            return value
        return "hash"

    def _build_embedding_provider_candidates(self) -> List[str]:
        primary_backend = (self._embedding_backend or "hash").strip().lower() or "hash"
        candidates: List[str] = [primary_backend]

        if not self._embedding_provider_chain_enabled:
            return candidates

        if self._embedding_provider_fail_open:
            for backend in ("api", "router", "openai"):
                if backend not in candidates:
                    candidates.append(backend)
            return candidates

        fallback_backend = self._resolve_chain_fallback_backend()
        if (
            fallback_backend in {"api", "router", "openai"}
            and fallback_backend not in candidates
        ):
            candidates.append(fallback_backend)
        return candidates

    async def _run_init_db_unlocked(self):
        """Run the full database bootstrap without any process-level lock."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Migration: add migrated_to column if not present (for existing DBs)
            await conn.run_sync(self._migrate_add_migrated_to)
        await apply_pending_migrations(self.database_url)
        async with self.engine.begin() as conn:
            capabilities = await conn.run_sync(self._setup_index_infra)
            self._fts_available = capabilities.get("fts_available", False)
            self._gist_fts_available = capabilities.get("gist_fts_available", False)
            self._vector_available = capabilities.get("vector_available", True)
            self._sqlite_vec_knn_ready = bool(
                capabilities.get("sqlite_vec_knn_ready", False)
            )
            self._sqlite_vec_capability = self._probe_sqlite_vec_capability()
            self._refresh_vector_engine_state()
            await conn.run_sync(self._sync_set_vector_engine_meta)
            await conn.run_sync(self._sync_set_write_lane_wal_meta)
        await self._bootstrap_indexes()

    async def _run_init_db_with_retry(self) -> None:
        for attempt in range(1, _INIT_DB_LOCK_RETRY_ATTEMPTS + 1):
            try:
                await self._run_init_db_unlocked()
                return
            except (sqlite3.OperationalError, SQLAlchemyOperationalError) as exc:
                if (
                    not self._is_sqlite_lock_error(exc)
                    or attempt == _INIT_DB_LOCK_RETRY_ATTEMPTS
                ):
                    raise
                await asyncio.sleep(
                    _INIT_DB_LOCK_RETRY_BASE_DELAY_SEC * float(attempt)
                )

    async def init_db(self):
        """Create tables, run migrations, and serialize startup across processes."""
        if self._init_lock_path is None:
            await self._run_init_db_with_retry()
            return

        self._init_lock_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, _INIT_DB_LOCK_RETRY_ATTEMPTS + 1):
            lock = AsyncFileLock(
                str(self._init_lock_path), timeout=self._init_lock_timeout_seconds
            )
            try:
                async with lock:
                    await self._run_init_db_with_retry()
                    return
            except FileLockTimeout:
                if attempt == _INIT_DB_LOCK_RETRY_ATTEMPTS:
                    raise
                await asyncio.sleep(
                    _INIT_DB_LOCK_RETRY_BASE_DELAY_SEC * float(attempt)
                )

    @staticmethod
    def _migrate_add_migrated_to(connection):
        """Add migrated_to column to memories table if it doesn't exist."""
        from sqlalchemy import inspect

        inspector = inspect(connection)
        columns = [col["name"] for col in inspector.get_columns("memories")]
        if "migrated_to" not in columns:
            connection.execute(
                text("ALTER TABLE memories ADD COLUMN migrated_to INTEGER")
            )

    def _setup_index_infra(self, connection) -> Dict[str, bool]:
        """Create index tables and probe optional SQLite capabilities."""
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_memory_chunks_memory_id "
                "ON memory_chunks(memory_id)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_chunks_memory_chunk "
                "ON memory_chunks(memory_id, chunk_index)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_memory_chunks_vec_memory_id "
                "ON memory_chunks_vec(memory_id)"
            )
        )

        fts_available = False
        try:
            connection.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts "
                    "USING fts5("
                    "chunk_id UNINDEXED, "
                    "memory_id UNINDEXED, "
                    "chunk_text"
                    ")"
                )
            )
            fts_available = True
        except Exception:
            # SQLite builds without FTS5 support should continue with LIKE fallback.
            fts_available = False

        gist_fts_available = False
        try:
            gist_fts_exists_before = connection.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'memory_gists_fts' "
                    "LIMIT 1"
                )
            ).first() is not None
            connection.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_gists_fts "
                    "USING fts5(gist_text, content='memory_gists', content_rowid='id')"
                )
            )
            gist_trigger_names = (
                "memory_gists_ai",
                "memory_gists_ad",
                "memory_gists_au",
            )
            existing_gist_triggers = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'trigger' AND name IN "
                        "('memory_gists_ai', 'memory_gists_ad', 'memory_gists_au')"
                    )
                ).all()
            }
            if "memory_gists_ai" not in existing_gist_triggers:
                connection.execute(
                    text(
                        "CREATE TRIGGER IF NOT EXISTS memory_gists_ai "
                        "AFTER INSERT ON memory_gists BEGIN "
                        "  INSERT INTO memory_gists_fts(rowid, gist_text) "
                        "  VALUES (new.id, new.gist_text); "
                        "END"
                    )
                )
            if "memory_gists_ad" not in existing_gist_triggers:
                connection.execute(
                    text(
                        "CREATE TRIGGER IF NOT EXISTS memory_gists_ad "
                        "AFTER DELETE ON memory_gists BEGIN "
                        "  INSERT INTO memory_gists_fts(memory_gists_fts, rowid, gist_text) "
                        "  VALUES ('delete', old.id, old.gist_text); "
                        "END"
                    )
                )
            if "memory_gists_au" not in existing_gist_triggers:
                connection.execute(
                    text(
                        "CREATE TRIGGER IF NOT EXISTS memory_gists_au "
                        "AFTER UPDATE ON memory_gists BEGIN "
                        "  INSERT INTO memory_gists_fts(memory_gists_fts, rowid, gist_text) "
                        "  VALUES ('delete', old.id, old.gist_text); "
                        "  INSERT INTO memory_gists_fts(rowid, gist_text) "
                        "  VALUES (new.id, new.gist_text); "
                        "END"
                    )
                )
            if (not gist_fts_exists_before) or (
                len(existing_gist_triggers) < len(gist_trigger_names)
            ):
                connection.execute(
                    text(
                        "INSERT INTO memory_gists_fts(memory_gists_fts) VALUES ('rebuild')"
                    )
                )
            gist_fts_available = True
        except Exception:
            gist_fts_available = False

        self._probe_semantic_vector_state(connection)
        sqlite_vec_knn_ready = self._setup_sqlite_vec_knn_infra(connection)

        now = _utc_now_naive().isoformat()
        self._sync_set_index_meta(connection, "fts_available", "1" if fts_available else "0", now)
        self._sync_set_index_meta(
            connection,
            "gist_fts_available",
            "1" if gist_fts_available else "0",
            now,
        )
        self._sync_set_index_meta(
            connection, "vector_available", "1" if self._vector_available else "0", now
        )
        self._sync_set_index_meta(connection, "embedding_backend", self._embedding_backend, now)
        self._sync_set_index_meta(connection, "embedding_model", self._embedding_model, now)
        self._sync_set_index_meta(
            connection,
            "embedding_provider_chain_enabled",
            "1" if self._embedding_provider_chain_enabled else "0",
            now,
        )
        self._sync_set_index_meta(
            connection,
            "embedding_provider_fail_open",
            "1" if self._embedding_provider_fail_open else "0",
            now,
        )
        self._sync_set_index_meta(
            connection,
            "embedding_provider_fallback",
            self._resolve_chain_fallback_backend(),
            now,
        )
        self._sync_set_index_meta(
            connection,
            "sqlite_vec_knn_ready",
            "1" if sqlite_vec_knn_ready else "0",
            now,
        )
        return {
            "fts_available": fts_available,
            "gist_fts_available": gist_fts_available,
            "vector_available": self._vector_available,
            "sqlite_vec_knn_ready": sqlite_vec_knn_ready,
        }

    def _probe_semantic_vector_state(self, connection) -> None:
        configured_dim = max(16, int(self._embedding_dim))
        detected_dims: List[int] = []
        try:
            dim_rows = connection.execute(
                text(
                    "SELECT DISTINCT dim "
                    "FROM memory_chunks_vec "
                    "WHERE dim IS NOT NULL AND dim > 0 "
                    "LIMIT 4"
                )
            ).fetchall()
            detected_dims = sorted(
                {
                    max(16, int(row[0]))
                    for row in dim_rows
                    if row[0] is not None
                }
            )
        except Exception:
            detected_dims = []

        self._semantic_vector_detected_dims = detected_dims
        self._semantic_vector_stored_dim = (
            detected_dims[0] if len(detected_dims) == 1 else None
        )
        if len(detected_dims) > 1:
            self._semantic_vector_block_reason = (
                "stored_vector_dims_mixed_requires_reindex"
            )
            logger.warning(
                "Semantic vector search disabled until reindex: stored vector dimensions are mixed "
                "(configured_dim=%s, detected_dims=%s).",
                configured_dim,
                detected_dims,
            )
            return
        if detected_dims and detected_dims[0] != configured_dim:
            self._semantic_vector_block_reason = (
                "embedding_dim_mismatch_requires_reindex"
            )
            logger.warning(
                "Semantic vector search disabled until reindex: embedding dimension mismatch "
                "(configured_dim=%s, stored_dim=%s).",
                configured_dim,
                detected_dims[0],
            )
            return
        self._semantic_vector_block_reason = ""

    def _setup_sqlite_vec_knn_infra(self, connection) -> bool:
        """
        Best-effort setup for vec0 KNN virtual table.

        Failures are intentionally non-fatal and keep legacy fallback path intact.
        """
        self._sqlite_vec_knn_ready = False
        self._sqlite_vec_knn_dim = max(16, int(self._embedding_dim))
        if not self._sqlite_vec_enabled:
            return False

        if self._semantic_vector_block_reason:
            return False

        vector_dim = self._semantic_vector_stored_dim or max(16, int(self._embedding_dim))
        self._sqlite_vec_knn_dim = vector_dim
        table_name = self._sqlite_vec_knn_table
        try:
            connection.execute(
                text(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {table_name} "
                    f"USING vec0(vector float[{vector_dim}] distance_metric=cosine)"
                )
            )
            connection.execute(
                text(
                    f"DELETE FROM {table_name}"
                )
            )
            connection.execute(
                text(
                    f"INSERT INTO {table_name}(rowid, vector) "
                    "SELECT chunk_id, vec_f32(vector) "
                    "FROM memory_chunks_vec "
                    "WHERE dim = :vector_dim"
                ),
                {"vector_dim": vector_dim},
            )
            self._sqlite_vec_knn_ready = True
            return True
        except Exception:
            self._sqlite_vec_knn_ready = False
            return False

    def _sync_set_vector_engine_meta(self, connection) -> None:
        now = _utc_now_naive().isoformat()
        sqlite_vec_status = str(self._sqlite_vec_capability.get("status", "disabled"))
        sqlite_vec_diag_code = str(self._sqlite_vec_capability.get("diag_code", ""))
        sqlite_vec_readiness = str(
            self._sqlite_vec_capability.get("sqlite_vec_readiness", "hold")
        )
        self._sync_set_index_meta(
            connection,
            "sqlite_vec_enabled",
            "1" if self._sqlite_vec_enabled else "0",
            now,
        )
        self._sync_set_index_meta(
            connection,
            "sqlite_vec_read_ratio",
            str(int(self._sqlite_vec_read_ratio)),
            now,
        )
        self._sync_set_index_meta(
            connection,
            "sqlite_vec_status",
            sqlite_vec_status,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "sqlite_vec_readiness",
            sqlite_vec_readiness,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "sqlite_vec_diag_code",
            sqlite_vec_diag_code,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "vector_engine_requested",
            self._vector_engine_requested,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "vector_engine_effective",
            self._vector_engine_effective,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "sqlite_vec_knn_ready",
            "1" if self._sqlite_vec_knn_ready else "0",
            now,
        )
        self._sync_set_index_meta(
            connection,
            "sqlite_vec_knn_dim",
            str(int(self._sqlite_vec_knn_dim)),
            now,
        )

    def _sync_set_write_lane_wal_meta(self, connection) -> None:
        now = _utc_now_naive().isoformat()
        self._sync_set_index_meta(
            connection,
            "runtime_write_wal_enabled",
            "1" if self._runtime_write_wal_enabled else "0",
            now,
        )
        self._sync_set_index_meta(
            connection,
            "runtime_write_journal_mode_requested",
            self._runtime_write_journal_mode_requested,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "runtime_write_journal_mode_effective",
            self._runtime_write_journal_mode_effective,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "runtime_write_wal_synchronous_requested",
            self._runtime_write_wal_synchronous_requested,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "runtime_write_wal_synchronous_effective",
            self._runtime_write_wal_synchronous_effective,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "runtime_write_busy_timeout_ms",
            str(int(self._runtime_write_busy_timeout_effective_ms)),
            now,
        )
        self._sync_set_index_meta(
            connection,
            "runtime_write_wal_autocheckpoint",
            str(int(self._runtime_write_wal_autocheckpoint_effective)),
            now,
        )
        self._sync_set_index_meta(
            connection,
            "runtime_write_pragma_status",
            self._runtime_write_pragma_status,
            now,
        )
        self._sync_set_index_meta(
            connection,
            "runtime_write_pragma_error",
            self._runtime_write_pragma_error,
            now,
        )

    async def _bootstrap_indexes(self) -> None:
        """
        Build missing chunk/vector indexes for existing active memories.
        """
        async with self.session() as session:
            missing_query = (
                select(Memory.id)
                .outerjoin(MemoryChunk, Memory.id == MemoryChunk.memory_id)
                .where(Memory.deprecated == False)
                .group_by(Memory.id)
                .having(func.count(MemoryChunk.id) == 0)
            )
            missing_ids = [row[0] for row in (await session.execute(missing_query)).all()]
            reindexed = 0
            for memory_id in missing_ids:
                reindexed += await self._reindex_memory(session, memory_id)
            await self._set_index_meta(session, "bootstrap_indexed_memories", str(len(missing_ids)))
            await self._set_index_meta(session, "bootstrap_indexed_chunks", str(reindexed))

    @staticmethod
    def _sync_set_index_meta(connection, key: str, value: str, updated_at: str):
        statement = text(
            "INSERT INTO index_meta(key, value, updated_at) "
            "VALUES (:key, :value, :updated_at) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = excluded.value, "
            "updated_at = excluded.updated_at"
        )
        params = {"key": key, "value": value, "updated_at": updated_at}
        for attempt in range(10):
            try:
                connection.execute(statement, params)
                return
            except (sqlite3.OperationalError, SQLAlchemyOperationalError) as exc:
                if not SQLiteClient._is_sqlite_lock_error(exc) or attempt >= 9:
                    raise
                time.sleep(0.1 * (attempt + 1))

    async def _set_index_meta(
        self, session: AsyncSession, key: str, value: str
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO index_meta(key, value, updated_at) "
                "VALUES (:key, :value, :updated_at) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, "
                "updated_at = excluded.updated_at"
            ),
            {"key": key, "value": value, "updated_at": _utc_now_naive().isoformat()},
        )

    async def get_runtime_meta(self, key: str) -> Optional[str]:
        """Read a runtime metadata value from index_meta."""
        key_value = (key or "").strip()
        if not key_value:
            return None
        async with self.session() as session:
            result = await session.execute(
                select(IndexMeta.value).where(IndexMeta.key == key_value)
            )
            value = result.scalar_one_or_none()
            return str(value) if value is not None else None

    async def set_runtime_meta(self, key: str, value: str) -> None:
        """Persist a runtime metadata value into index_meta."""
        key_value = (key or "").strip()
        if not key_value:
            raise ValueError("key must not be empty")
        async with self.session() as session:
            await self._set_index_meta(session, key_value, value)

    async def upsert_memory_gist(
        self,
        *,
        memory_id: int,
        gist_text: str,
        source_hash: str,
        gist_method: str = "fallback",
        quality_score: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Create or update a gist record for a memory and content hash.

        Upsert key = (memory_id, source_content_hash). Existing rows are refreshed
        to avoid duplicate gists for unchanged content.
        """
        parsed_memory_id = int(memory_id)
        if parsed_memory_id <= 0:
            raise ValueError("memory_id must be a positive integer")

        gist_value = (gist_text or "").strip()
        if not gist_value:
            raise ValueError("gist_text must not be empty")

        source_hash_value = (source_hash or "").strip()
        if not source_hash_value:
            raise ValueError("source_hash must not be empty")

        method_value = (gist_method or "fallback").strip().lower() or "fallback"
        quality_value: Optional[float]
        if quality_score is None:
            quality_value = None
        else:
            try:
                quality_value = float(quality_score)
            except (TypeError, ValueError) as exc:
                raise ValueError("quality_score must be a float value or null") from exc

        async with self.session() as session:
            memory_row = await session.get(Memory, parsed_memory_id)
            if memory_row is None:
                raise ValueError(f"memory_id={parsed_memory_id} not found")
            now_value = _utc_now_naive()
            await session.execute(
                text(
                    "INSERT INTO memory_gists("
                    "memory_id, gist_text, source_content_hash, gist_method, quality_score, created_at"
                    ") VALUES ("
                    ":memory_id, :gist_text, :source_content_hash, :gist_method, :quality_score, :created_at"
                    ") ON CONFLICT(memory_id, source_content_hash) DO UPDATE SET "
                    "gist_text = excluded.gist_text, "
                    "gist_method = excluded.gist_method, "
                    "quality_score = excluded.quality_score, "
                    "created_at = excluded.created_at"
                ),
                {
                    "memory_id": parsed_memory_id,
                    "gist_text": gist_value,
                    "source_content_hash": source_hash_value,
                    "gist_method": method_value,
                    "quality_score": quality_value,
                    "created_at": now_value,
                },
            )
            row = (
                await session.execute(
                    select(MemoryGist)
                    .where(MemoryGist.memory_id == parsed_memory_id)
                    .where(MemoryGist.source_content_hash == source_hash_value)
                    .limit(1)
                )
            ).scalar_one()

            return {
                "id": row.id,
                "memory_id": row.memory_id,
                "gist_text": row.gist_text,
                "source_hash": row.source_content_hash,
                "gist_method": row.gist_method,
                "quality_score": row.quality_score,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }

    @staticmethod
    def _memory_gist_to_dict(row: MemoryGist) -> Dict[str, Any]:
        return {
            "id": row.id,
            "memory_id": row.memory_id,
            "gist_text": row.gist_text,
            "source_hash": row.source_content_hash,
            "gist_method": row.gist_method,
            "quality_score": row.quality_score,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    async def _get_latest_gists_map(
        self, session: AsyncSession, memory_ids: List[int]
    ) -> Dict[int, Dict[str, Any]]:
        normalized_ids: List[int] = []
        for item in memory_ids:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed > 0:
                normalized_ids.append(parsed)
        normalized_ids = list(dict.fromkeys(normalized_ids))
        if not normalized_ids:
            return {}

        result = await session.execute(
            select(MemoryGist)
            .where(MemoryGist.memory_id.in_(normalized_ids))
            .order_by(
                MemoryGist.memory_id.asc(),
                MemoryGist.created_at.desc(),
                MemoryGist.id.desc(),
            )
        )
        mapping: Dict[int, Dict[str, Any]] = {}
        for row in result.scalars().all():
            if row.memory_id in mapping:
                continue
            mapping[row.memory_id] = self._memory_gist_to_dict(row)
        return mapping

    async def get_latest_memory_gist(self, memory_id: int) -> Optional[Dict[str, Any]]:
        """Read latest gist row for a memory."""
        parsed_memory_id = int(memory_id)
        if parsed_memory_id <= 0:
            return None
        async with self.session() as session:
            gist_map = await self._get_latest_gists_map(session, [parsed_memory_id])
            return gist_map.get(parsed_memory_id)

    async def get_gist_stats(self) -> Dict[str, Any]:
        """Return compact observability stats for gist materialization."""
        async with self.session() as session:
            total_rows = int(
                (await session.execute(select(func.count(MemoryGist.id)))).scalar() or 0
            )
            total_distinct_memory_count = int(
                (
                    await session.execute(
                        select(func.count(func.distinct(MemoryGist.memory_id)))
                    )
                ).scalar()
                or 0
            )
            distinct_memory_count = int(
                (
                    await session.execute(
                        select(func.count(func.distinct(MemoryGist.memory_id)))
                        .join(Memory, Memory.id == MemoryGist.memory_id)
                        .where(Memory.deprecated == False)
                    )
                ).scalar()
                or 0
            )
            active_memory_count = int(
                (
                    await session.execute(
                        select(func.count(Memory.id)).where(Memory.deprecated == False)
                    )
                ).scalar()
                or 0
            )
            with_quality_count = int(
                (
                    await session.execute(
                        select(func.count(MemoryGist.id)).where(
                            MemoryGist.quality_score.isnot(None)
                        )
                    )
                ).scalar()
                or 0
            )
            avg_quality_raw = (
                await session.execute(
                    select(func.avg(MemoryGist.quality_score)).where(
                        MemoryGist.quality_score.isnot(None)
                    )
                )
            ).scalar()
            avg_quality = round(float(avg_quality_raw or 0.0), 3)
            latest_created_at = (
                await session.execute(select(func.max(MemoryGist.created_at)))
            ).scalar()
            method_rows = (
                await session.execute(
                    select(MemoryGist.gist_method, func.count(MemoryGist.id)).group_by(
                        MemoryGist.gist_method
                    )
                )
            ).all()

            method_breakdown: Dict[str, int] = {}
            for method_name, count_value in method_rows:
                method_key = str(method_name or "unknown")
                method_breakdown[method_key] = int(count_value or 0)

            coverage_ratio = (
                round(distinct_memory_count / active_memory_count, 3)
                if active_memory_count > 0
                else 0.0
            )

            return {
                "total_rows": total_rows,
                "distinct_memory_count": distinct_memory_count,
                "total_distinct_memory_count": total_distinct_memory_count,
                "active_memory_count": active_memory_count,
                "coverage_ratio": coverage_ratio,
                "quality_coverage_ratio": (
                    round(with_quality_count / total_rows, 3) if total_rows > 0 else 0.0
                ),
                "avg_quality_score": avg_quality,
                "method_breakdown": method_breakdown,
                "latest_created_at": latest_created_at.isoformat()
                if latest_created_at
                else None,
            }

    @staticmethod
    def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
        return shared_parse_iso_datetime(value)

    @staticmethod
    def _normalize_db_datetime(value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    @staticmethod
    def _content_snippet(content: str, limit: int = 200) -> str:
        text = (content or "").strip()
        if len(text) <= limit:
            return text
        return text[:limit] + "..."

    @staticmethod
    def _build_vitality_state_hash(
        *,
        memory_id: int,
        vitality_score: float,
        access_count: int,
        path_count: int,
        deprecated: bool,
    ) -> str:
        # Keep state hash stable across wall-clock time.
        # Dynamic fields like inactive_days would drift every few seconds and
        # make review-confirm flow fail with false stale_state mismatches.
        payload = (
            f"{int(memory_id)}|{round(float(vitality_score), 6)}|{int(access_count)}|"
            f"{int(path_count)}|{int(bool(deprecated))}"
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _join_api_url(base: str, endpoint: str) -> str:
        return f"{base.rstrip('/')}/{endpoint.lstrip('/')}"

    @staticmethod
    def _normalize_chat_api_base(base: str) -> str:
        normalized = (base or "").strip().rstrip("/")
        if not normalized:
            return ""

        lowered = normalized.lower()
        for suffix in ("/chat/completions", "/responses"):
            if lowered.endswith(suffix):
                return normalized[: -len(suffix)]
        return normalized

    @staticmethod
    def _normalize_embedding_api_base(base: str) -> str:
        normalized = (base or "").strip().rstrip("/")
        if not normalized:
            return ""
        lowered = normalized.lower()
        if lowered.endswith("/embeddings"):
            return normalized[: -len("/embeddings")]
        return normalized

    @staticmethod
    def _normalize_reranker_api_base(base: str) -> str:
        normalized = (base or "").strip().rstrip("/")
        if not normalized:
            return ""
        lowered = normalized.lower()
        if lowered.endswith("/rerank"):
            return normalized[: -len("/rerank")]
        return normalized

    @staticmethod
    def _normalize_reranker_provider(provider: Optional[str]) -> str:
        value = str(provider or "openai_compat").strip().lower()
        if value in {"openai", "openai_compat", "router", "siliconflow"}:
            return "openai_compat"
        if value in {"cohere", "cohere_v1"}:
            return "cohere"
        if value in {"lmstudio", "lmstudio_chat"}:
            return "lmstudio_chat"
        if value in {"lmstudio_responses", "responses"}:
            return "lmstudio_responses"
        return "openai_compat"

    @staticmethod
    def _normalize_vector_engine(value: Optional[str]) -> str:
        engine = str(value or "legacy").strip().lower() or "legacy"
        if engine in {"legacy", "vec", "dual"}:
            return engine
        return "legacy"

    @staticmethod
    def _resolve_sqlite_extension_file(path_input: str) -> Optional[FilePath]:
        raw_path = str(path_input or "").strip()
        if not raw_path:
            return None
        try:
            base = FilePath(raw_path).expanduser().resolve(strict=False)
        except OSError:
            return None
        candidates = [base]
        if base.suffix == "":
            candidates.extend(
                FilePath(str(base) + suffix) for suffix in (".dylib", ".so", ".dll")
            )
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        for candidate in candidates:
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue
        return None

    def _probe_sqlite_vec_capability(self) -> Dict[str, Any]:
        capability: Dict[str, Any] = {
            "status": "disabled",
            "sqlite_vec_readiness": "hold",
            "diag_code": "",
            "extension_path_input": self._sqlite_vec_extension_path,
            "extension_path": "",
            "extension_loaded": False,
            "extension_path_exists": False,
        }

        if not self._sqlite_vec_enabled:
            capability["diag_code"] = "sqlite_vec_disabled"
            return capability

        extension_input = str(self._sqlite_vec_extension_path or "").strip()
        if not extension_input:
            capability["status"] = "skipped_no_extension_path"
            capability["diag_code"] = "path_not_provided"
            return capability

        resolved_extension = self._resolve_sqlite_extension_file(extension_input)
        if resolved_extension is None:
            capability["status"] = "invalid_extension_path"
            capability["diag_code"] = "path_not_found"
            return capability

        capability["extension_path"] = str(resolved_extension)
        capability["extension_path_exists"] = True
        if not resolved_extension.is_file():
            capability["status"] = "invalid_extension_path"
            capability["diag_code"] = "path_not_file"
            return capability

        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(":memory:")
            try:
                connection.enable_load_extension(True)
            except (AttributeError, sqlite3.Error):
                capability["status"] = "extension_loading_unavailable"
                capability["diag_code"] = "enable_load_extension_failed"
                return capability

            try:
                connection.load_extension(str(resolved_extension))
            except sqlite3.Error:
                capability["status"] = "extension_load_failed"
                capability["diag_code"] = "load_extension_failed"
                return capability
            finally:
                try:
                    connection.enable_load_extension(False)
                except sqlite3.Error:
                    pass

            capability["status"] = "ok"
            capability["sqlite_vec_readiness"] = "ready"
            capability["diag_code"] = ""
            capability["extension_loaded"] = True
            return capability
        except sqlite3.Error:
            capability["status"] = "sqlite_runtime_error"
            capability["diag_code"] = "sqlite_runtime_error"
            return capability
        finally:
            if connection is not None:
                connection.close()

    def _refresh_vector_engine_state(self) -> None:
        requested = self._normalize_vector_engine(self._vector_engine_requested)
        self._vector_engine_requested = requested
        if requested == "legacy":
            self._vector_engine_effective = "legacy"
            return

        capability_ready = (
            str(self._sqlite_vec_capability.get("sqlite_vec_readiness", "hold")) == "ready"
        )
        if not self._sqlite_vec_enabled or not capability_ready:
            self._vector_engine_effective = "legacy"
            return
        self._vector_engine_effective = requested

    def _resolve_vector_engine_for_query(self, query: str) -> str:
        effective = self._normalize_vector_engine(self._vector_engine_effective)
        if effective in {"legacy", "vec"}:
            return effective

        if self._sqlite_vec_read_ratio <= 0:
            return "legacy"
        if self._sqlite_vec_read_ratio >= 100:
            return "vec"

        normalized_query = (query or "").strip().lower()
        digest = hashlib.sha256(normalized_query.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:2], byteorder="big") % 100
        return "vec" if bucket < self._sqlite_vec_read_ratio else "legacy"

    @staticmethod
    def _append_degrade_reason(
        degrade_reasons: Optional[List[str]], reason: str
    ) -> None:
        if degrade_reasons is None or not reason:
            return
        if reason not in degrade_reasons:
            degrade_reasons.append(reason)

    @classmethod
    def _append_request_failure_reasons(
        cls,
        degrade_reasons: Optional[List[str]],
        *,
        prefix: str,
        error_info: Optional[Dict[str, Any]],
        backend: Optional[str] = None,
    ) -> None:
        cls._append_degrade_reason(degrade_reasons, prefix)

        backend_value = str(backend or "").strip().lower()
        if backend_value:
            cls._append_degrade_reason(degrade_reasons, f"{prefix}:{backend_value}")

        if not isinstance(error_info, dict):
            return

        category = str(error_info.get("category") or "").strip().lower()
        if not category:
            return

        if category == "request_error":
            error_type = str(error_info.get("error_type") or "").strip()
            message = str(error_info.get("message") or "").strip().lower()
            if "timeout" in error_type.lower() or "timeout" in message:
                cls._append_degrade_reason(degrade_reasons, f"{prefix}:timeout")
                if backend_value:
                    cls._append_degrade_reason(
                        degrade_reasons, f"{prefix}:{backend_value}:timeout"
                    )
                if error_type:
                    cls._append_degrade_reason(
                        degrade_reasons, f"{prefix}:timeout:{error_type}"
                    )
                    if backend_value:
                        cls._append_degrade_reason(
                            degrade_reasons,
                            f"{prefix}:{backend_value}:timeout:{error_type}",
                        )

        category_reason = f"{prefix}:{category}"
        cls._append_degrade_reason(degrade_reasons, category_reason)
        if backend_value:
            cls._append_degrade_reason(
                degrade_reasons, f"{prefix}:{backend_value}:{category}"
            )

        detail_reason = ""
        if category == "http_status":
            status_code = error_info.get("status_code")
            if status_code is not None:
                detail_reason = str(status_code).strip()
        else:
            detail_reason = str(error_info.get("error_type") or "").strip()

        if not detail_reason:
            return

        cls._append_degrade_reason(
            degrade_reasons, f"{category_reason}:{detail_reason}"
        )
        if backend_value:
            cls._append_degrade_reason(
                degrade_reasons,
                f"{prefix}:{backend_value}:{category}:{detail_reason}",
            )

    @staticmethod
    def _collect_keyword_hits(
        source_text: str, token_set: set[str], keywords: List[str]
    ) -> List[str]:
        hits: List[str] = []
        for raw_keyword in keywords:
            keyword = (raw_keyword or "").strip().lower()
            if not keyword:
                continue
            # English keywords use word boundaries to avoid substring false positives.
            if re.fullmatch(r"[a-z0-9_ ]+", keyword):
                if re.search(rf"\b{re.escape(keyword)}\b", source_text):
                    hits.append(keyword)
                continue
            # CJK keywords keep substring matching, but skip obvious negated forms
            # such as "不可能" to reduce false positives.
            if re.search(
                rf"(?<!{_CJK_NEGATING_PREFIX_PATTERN}){re.escape(keyword)}",
                source_text,
            ):
                hits.append(keyword)
        # Keep deterministic order and remove duplicates.
        return list(dict.fromkeys(hits))

    @staticmethod
    def _extract_visual_guard_hash(content: str) -> Optional[str]:
        if not isinstance(content, str) or "- kind: visual-memory" not in content:
            return None
        matched = re.search(
            r"^- provenance_media_ref_sha256:\s*(sha256-[A-Za-z0-9_-]+)\s*$",
            content,
            re.MULTILINE,
        )
        if not matched:
            return None
        value = matched.group(1).strip()
        return value or None

    @staticmethod
    def _is_visual_namespace_container_content(content: str) -> bool:
        if not isinstance(content, str):
            return False
        return "visual_namespace_container: true" in content

    def preprocess_query(self, query: str) -> Dict[str, Any]:
        """
        Normalize a raw query into a deterministic retrieval-friendly form.

        Returns:
            {
              "original_query": str,
              "normalized_query": str,
              "rewritten_query": str,
              "tokens": list[str],
              "changed": bool
            }
        """
        original = (query or "").strip()
        normalized = re.sub(r"\s+", " ", original)
        lowered = normalized.lower()
        tokens = re.findall(r"[a-z0-9_]+", lowered)
        deduped_tokens = list(dict.fromkeys(tokens))

        has_uri_hint = "://" in normalized or "/" in normalized
        has_non_ascii = any(ord(ch) > 127 for ch in normalized)
        if has_uri_hint or has_non_ascii:
            # Preserve raw query for path/URI and multilingual lookups.
            rewritten = normalized
        else:
            rewritten = " ".join(deduped_tokens[:16]) if deduped_tokens else normalized
        changed = rewritten != original
        return {
            "original_query": original,
            "normalized_query": normalized,
            "rewritten_query": rewritten,
            "tokens": deduped_tokens[:16],
            "changed": changed,
        }

    def classify_intent(
        self, query: str, rewritten_query: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Lightweight intent classifier for retrieval strategy routing.

        Supported intents:
        - factual
        - exploratory
        - temporal
        - causal
        """
        source = " ".join(
            part.strip().lower() for part in [query or "", rewritten_query or ""] if part
        )
        source = re.sub(r"\s+", " ", source).strip()
        token_set = set(re.findall(r"[a-z0-9_]+", source))

        hits_by_intent: Dict[str, List[str]] = {
            intent: self._collect_keyword_hits(source, token_set, keywords)
            for intent, keywords in _INTENT_KEYWORDS.items()
        }
        scores = {intent: len(hits) for intent, hits in hits_by_intent.items()}

        # "why ... after/before ..." queries are usually causal requests where
        # the time word only describes the triggering event.
        causal_strong_hits = {
            "why",
            "because",
            "cause",
            "reason",
            "root cause",
            "debug",
            "wrong",
            "broke",
            "broken",
            "problem",
            "problems",
            "issue",
            "issues",
            "bug",
            "bugs",
            "error",
            "errors",
            "failing",
            "为什么",
            "原因",
            "因果",
            "怎么回事",
            "出错",
            "有问题",
            "什么问题",
        }
        temporal_weak_hits = {"before", "after", "since", "yesterday", "recently", "last", "之前", "之后", "昨天", "最近"}
        causal_hits = set(hits_by_intent.get("causal", []))
        temporal_hits = set(hits_by_intent.get("temporal", []))
        prefer_causal_over_temporal = bool(causal_hits & causal_strong_hits) and bool(
            temporal_hits
        ) and temporal_hits <= temporal_weak_hits
        if prefer_causal_over_temporal:
            scores["causal"] = max(scores.get("causal", 0), scores.get("temporal", 0) + 1)

        ranked = sorted(
            ((intent, score) for intent, score in scores.items() if score > 0),
            key=lambda item: item[1],
            reverse=True,
        )

        if not ranked:
            # No keyword hits — check implicit patterns before defaulting to factual.
            for pattern in _CAUSAL_IMPLICIT_PATTERNS:
                if pattern.search(source):
                    return {
                        "intent": "causal",
                        "strategy_template": "causal_wide_pool",
                        "method": "keyword_scoring_v2",
                        "confidence": 0.58,
                        "signals": [f"implicit_causal:{pattern.pattern[:30]}"],
                    }
            for pattern in _TEMPORAL_IMPLICIT_PATTERNS:
                if pattern.search(source):
                    return {
                        "intent": "temporal",
                        "strategy_template": "temporal_time_filtered",
                        "method": "keyword_scoring_v2",
                        "confidence": 0.58,
                        "signals": [f"implicit_temporal:{pattern.pattern[:30]}"],
                    }
            return {
                "intent": "factual",
                "strategy_template": "factual_high_precision",
                "method": "keyword_scoring_v2",
                "confidence": 0.55,
                "signals": ["default_factual"],
            }

        if len(ranked) > 1:
            top_intent, top_score = ranked[0]
            runner_intent, runner_score = ranked[1]

            if top_score == runner_score:
                tied_intents = {intent for intent, score in ranked if score == top_score}

                # Tie-break priority: exploratory > temporal > causal > factual.
                # Rationale: explicit framing ("explore X", "before Y") is a stronger
                # user signal than implicit causal keywords ("debug", "failure").
                for preferred in ("exploratory", "temporal", "causal", "factual"):
                    if preferred in tied_intents:
                        winner_hits = hits_by_intent.get(preferred, [])[:3]
                        return {
                            "intent": preferred,
                            "strategy_template": self._intent_strategy_template(preferred),
                            "method": "keyword_scoring_v2",
                            "confidence": 0.52,
                            "signals": [f"tie_break:{preferred}:{hit}" for hit in winner_hits]
                                or [f"tie_break:{preferred}"],
                        }

                ambiguous_signals: List[str] = []
                for intent, _ in ranked[:2]:
                    for hit in hits_by_intent.get(intent, [])[:2]:
                        ambiguous_signals.append(f"{intent}:{hit}")
                if not ambiguous_signals:
                    ambiguous_signals = ["ambiguous_keyword_overlap"]
                return {
                    "intent": "unknown",
                    "strategy_template": "default",
                    "method": "keyword_scoring_v2",
                    "confidence": 0.42,
                    "signals": ambiguous_signals,
                }

            # Conservative fallback: only fall back to unknown when the winner
            # has very weak evidence (1 hit) and the runner is equally close.
            # With the expanded keyword set, 2+ hits is a strong enough signal.
            if (
                top_score <= 1
                and (top_score - runner_score) <= 0
                and not (
                    prefer_causal_over_temporal
                    and top_intent == "causal"
                    and runner_intent == "temporal"
                )
            ):
                ambiguous_signals = []
                for intent in (top_intent, runner_intent):
                    for hit in hits_by_intent.get(intent, [])[:2]:
                        ambiguous_signals.append(f"{intent}:{hit}")
                if not ambiguous_signals:
                    ambiguous_signals = ["ambiguous_keyword_overlap"]
                return {
                    "intent": "unknown",
                    "strategy_template": "default",
                    "method": "keyword_scoring_v2",
                    "confidence": 0.46,
                    "signals": ambiguous_signals,
                }

        winner_intent = ranked[0][0]
        top_score = ranked[0][1]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0
        margin = max(0, top_score - runner_up)
        confidence = round(min(0.96, 0.58 + top_score * 0.07 + margin * 0.04), 2)

        strategy_by_intent = {
            "factual": "factual_high_precision",
            "temporal": "temporal_time_filtered",
            "causal": "causal_wide_pool",
            "exploratory": "exploratory_high_recall",
        }
        winner_signals = [
            f"{winner_intent}:{hit}" for hit in hits_by_intent.get(winner_intent, [])[:5]
        ] or [f"{winner_intent}:keyword_signal"]

        return {
            "intent": winner_intent,
            "strategy_template": strategy_by_intent.get(
                winner_intent, "factual_high_precision"
            ),
            "method": "keyword_scoring_v2",
            "confidence": confidence,
            "signals": winner_signals,
        }

    @staticmethod
    def _intent_strategy_template(intent: str) -> str:
        mapping = {
            "factual": "factual_high_precision",
            "exploratory": "exploratory_high_recall",
            "temporal": "temporal_time_filtered",
            "causal": "causal_wide_pool",
            "unknown": "default",
        }
        return mapping.get(intent, "default")

    async def classify_intent_with_llm(
        self, query: str, rewritten_query: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Experimental intent classifier with LLM routing and safe fallback.

        Returns heuristic classification when LLM is disabled or fails.
        """
        fallback = self.classify_intent(query, rewritten_query)
        if not self._intent_llm_enabled:
            return fallback

        degrade_reasons: List[str] = []
        if not self._intent_llm_api_base or not self._intent_llm_model:
            degrade_reasons.append("intent_llm_config_missing")
            return {
                **fallback,
                "intent_llm_enabled": True,
                "intent_llm_applied": False,
                "degraded": True,
                "degrade_reason": degrade_reasons[0],
                "degrade_reasons": degrade_reasons,
            }

        system_prompt = (
            "You classify retrieval intent for a memory search system. "
            "Return strict JSON only with keys: intent, confidence, signals. "
            "intent must be one of: factual, exploratory, temporal, causal, unknown.\n\n"
            "Definitions and examples:\n"
            "- factual: specific fact lookup. E.g. \"What is the default port?\" / \"如何配置数据库连接\"\n"
            "- exploratory: open-ended brainstorming or discovery. "
            "E.g. \"brainstorm UI ideas\" / \"关于UI原型的头脑风暴\"\n"
            "- temporal: recent activity or time-based recall. "
            "E.g. \"recent activity on X\" / \"文献综述矩阵的近期活动\" / \"两天前我在处理...\"\n"
            "- causal: root-cause analysis or why-questions. "
            "E.g. \"why is X failing\" / \"为什么实验方案表现不好\" / \"哪里出了问题\""
        )
        user_prompt = (
            "Original query:\n"
            f"{query}\n\n"
            "Rewritten query:\n"
            f"{rewritten_query or query}\n\n"
            "Decide intent for retrieval strategy."
        )
        payload = {
            "model": self._intent_llm_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = await self._post_json(
            self._intent_llm_api_base,
            "/chat/completions",
            payload,
            self._intent_llm_api_key,
        )
        if response is None:
            degrade_reasons.append("intent_llm_request_failed")
            return {
                **fallback,
                "intent_llm_enabled": True,
                "intent_llm_applied": False,
                "degraded": True,
                "degrade_reason": degrade_reasons[0],
                "degrade_reasons": degrade_reasons,
            }

        message_text = self._extract_chat_message_text(response)
        if not message_text:
            degrade_reasons.append("intent_llm_response_empty")
            return {
                **fallback,
                "intent_llm_enabled": True,
                "intent_llm_applied": False,
                "degraded": True,
                "degrade_reason": degrade_reasons[0],
                "degrade_reasons": degrade_reasons,
            }

        parsed = self._parse_chat_json_object(message_text)

        if parsed is None:
            degrade_reasons.append("intent_llm_response_invalid")
            return {
                **fallback,
                "intent_llm_enabled": True,
                "intent_llm_applied": False,
                "degraded": True,
                "degrade_reason": degrade_reasons[0],
                "degrade_reasons": degrade_reasons,
            }

        intent_value = str(parsed.get("intent") or "").strip().lower()
        if intent_value not in {"factual", "exploratory", "temporal", "causal", "unknown"}:
            degrade_reasons.append("intent_llm_intent_invalid")
            return {
                **fallback,
                "intent_llm_enabled": True,
                "intent_llm_applied": False,
                "degraded": True,
                "degrade_reason": degrade_reasons[0],
                "degrade_reasons": degrade_reasons,
            }

        confidence_raw = parsed.get("confidence")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.62
        confidence = round(max(0.0, min(1.0, confidence)), 2)

        signals_raw = parsed.get("signals")
        if isinstance(signals_raw, list):
            signals = [
                str(item).strip()
                for item in signals_raw
                if isinstance(item, str) and str(item).strip()
            ][:6]
        else:
            signals = []
        if not signals:
            signals = [f"intent_llm:{intent_value}"]

        return {
            "intent": intent_value,
            "strategy_template": self._intent_strategy_template(intent_value),
            "method": "intent_llm",
            "confidence": confidence,
            "signals": signals,
            "intent_llm_enabled": True,
            "intent_llm_applied": True,
        }

    @staticmethod
    def _normalize_unit_score(value: Any) -> Optional[float]:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None

        if math.isnan(numeric) or math.isinf(numeric):
            return None
        if 0.0 <= numeric <= 1.0:
            return numeric
        if -1.0 <= numeric <= 1.0:
            return (numeric + 1.0) / 2.0
        try:
            return 1.0 / (1.0 + math.exp(-numeric))
        except OverflowError:
            return 0.0 if numeric < 0 else 1.0

    @staticmethod
    def _extract_embedding_from_response(payload: Any) -> Optional[List[float]]:
        candidates: List[Any] = []
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list) and data:
                first_item = data[0]
                if isinstance(first_item, dict):
                    candidates.append(first_item.get("embedding"))
                elif isinstance(first_item, list):
                    candidates.append(first_item)

            candidates.append(payload.get("embedding"))

            result = payload.get("result")
            if isinstance(result, dict):
                candidates.append(result.get("embedding"))
                result_data = result.get("data")
                if isinstance(result_data, list) and result_data:
                    first_result = result_data[0]
                    if isinstance(first_result, dict):
                        candidates.append(first_result.get("embedding"))
                    elif isinstance(first_result, list):
                        candidates.append(first_result)

        for candidate in candidates:
            if not isinstance(candidate, list):
                continue
            try:
                return [float(v) for v in candidate]
            except (TypeError, ValueError):
                continue
        return None

    def _validate_embedding_dimension(
        self,
        embedding: Optional[List[float]],
        *,
        degrade_reasons: Optional[List[str]] = None,
        backend: Optional[str] = None,
    ) -> Optional[List[float]]:
        if embedding is None:
            return None

        expected_dim = int(self._embedding_dim)
        actual_dim = len(embedding)
        if actual_dim == expected_dim:
            return embedding

        self._append_degrade_reason(
            degrade_reasons, "embedding_response_dim_mismatch"
        )
        self._append_degrade_reason(
            degrade_reasons,
            f"embedding_response_dim_mismatch:{actual_dim}!={expected_dim}",
        )
        backend_value = str(backend or "").strip().lower()
        if backend_value:
            self._append_degrade_reason(
                degrade_reasons,
                f"embedding_response_dim_mismatch:{backend_value}:{actual_dim}!={expected_dim}",
            )
        return None

    def _extract_rerank_scores(
        self, payload: Any, total_documents: int
    ) -> Dict[int, float]:
        if total_documents <= 0 or not isinstance(payload, dict):
            return {}

        rows: List[Any] = []
        for key in ("results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break

        if not rows:
            result = payload.get("result")
            if isinstance(result, dict):
                for key in ("results", "data"):
                    value = result.get(key)
                    if isinstance(value, list):
                        rows = value
                        break
        if not rows:
            llm_text = self._extract_llm_response_text(payload)
            parsed = self._parse_chat_json_object(llm_text) if llm_text else None
            if isinstance(parsed, dict) and parsed is not payload:
                return self._extract_rerank_scores(parsed, total_documents)

        parsed_scores: Dict[int, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue

            raw_index = row.get("index", row.get("document_index"))
            try:
                idx = int(raw_index)
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= total_documents:
                continue

            raw_score = row.get("score")
            if raw_score is None:
                raw_score = row.get("relevance_score")

            normalized_score = self._normalize_unit_score(raw_score)
            if normalized_score is None:
                continue
            previous = parsed_scores.get(idx)
            if previous is None or normalized_score > previous:
                parsed_scores[idx] = normalized_score

        return parsed_scores

    @staticmethod
    def _build_lmstudio_rerank_prompt(query: str, documents: List[str]) -> str:
        doc_lines = [
            f"{index}. {document}"
            for index, document in enumerate(documents)
        ]
        return (
            "You are a reranker. Return strict JSON only with this schema:\n"
            '{"results":[{"index":0,"score":0.0}]}\n'
            "Rules:\n"
            "- score must be a float between 0 and 1\n"
            "- include every document exactly once\n"
            "- higher score means more relevant to the query\n"
            "- no markdown, no explanation, no extra keys\n\n"
            f"Query:\n{query}\n\n"
            "Documents:\n"
            + "\n".join(doc_lines)
        )

    def _build_rerank_request(
        self,
        query: str,
        documents: List[str],
        *,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        provider_value = self._normalize_reranker_provider(provider or self._reranker_provider)
        model_value = str(model or self._reranker_model or "").strip()
        if provider_value == "cohere":
            return "/rerank", {
                "model": model_value,
                "query": query,
                "documents": [{"text": item} for item in documents],
                "top_n": len(documents),
            }
        if provider_value == "lmstudio_chat":
            return "/chat/completions", {
                "model": model_value,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": "Return strict JSON only.",
                    },
                    {
                        "role": "user",
                        "content": self._build_lmstudio_rerank_prompt(query, documents),
                    },
                ],
            }
        if provider_value == "lmstudio_responses":
            return "/responses", {
                "model": model_value,
                "input": self._build_lmstudio_rerank_prompt(query, documents),
                "temperature": 0,
            }
        return "/rerank", {
            "model": model_value,
            "query": query,
            "documents": documents,
        }

    def _resolve_reranker_attempts(
        self, documents: Optional[Sequence[str]] = None
    ) -> List[Dict[str, Any]]:
        attempts: List[Dict[str, Any]] = []
        identities: set[Tuple[str, str, str, str]] = set()

        def _append_attempt(
            *,
            name: str,
            base: str,
            api_key: str,
            provider: str,
            model: str,
            timeout_sec: float,
        ) -> None:
            base_value = self._normalize_reranker_api_base(base)
            provider_value = self._normalize_reranker_provider(provider)
            model_value = str(model or "").strip()
            api_key_value = str(api_key or "").strip()
            if not base_value or not model_value:
                return

            identity = (
                base_value,
                provider_value,
                model_value,
                api_key_value,
            )
            if identity in identities:
                return
            identities.add(identity)
            attempts.append(
                {
                    "name": name,
                    "base": base_value,
                    "api_key": api_key_value,
                    "provider": provider_value,
                    "model": model_value,
                    "timeout_sec": timeout_sec,
                }
            )

        document_count = len(documents) if documents is not None else None
        if (
            document_count is not None
            and self._reranker_small_batch_max_documents > 0
            and document_count <= self._reranker_small_batch_max_documents
        ):
            _append_attempt(
                name="small_batch",
                base=self._reranker_small_batch_api_base,
                api_key=self._reranker_small_batch_api_key,
                provider=self._reranker_small_batch_provider,
                model=self._reranker_small_batch_model,
                timeout_sec=self._reranker_small_batch_timeout_sec,
            )

        primary_model = str(self._reranker_model or "").strip()
        if self._reranker_api_base and primary_model:
            _append_attempt(
                name="primary",
                base=self._reranker_api_base,
                api_key=self._reranker_api_key,
                provider=self._reranker_provider,
                model=primary_model,
                timeout_sec=self._reranker_timeout_sec,
            )

        fallback_model = str(self._reranker_fallback_model or "").strip()
        if self._reranker_fallback_api_base and fallback_model:
            _append_attempt(
                name="fallback",
                base=self._reranker_fallback_api_base,
                api_key=self._reranker_fallback_api_key,
                provider=self._reranker_fallback_provider,
                model=fallback_model,
                timeout_sec=self._reranker_fallback_timeout_sec,
            )
        return attempts

    async def _call_post_json(
        self,
        base: str,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str = "",
        *,
        timeout_sec: Optional[float] = None,
        error_sink: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return await self._post_json_with_optional_error_sink(
            base,
            endpoint,
            payload,
            api_key,
            timeout_sec=timeout_sec,
            error_sink=error_sink,
        )

    async def _post_json_with_optional_error_sink(
        self,
        base: str,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str = "",
        *,
        timeout_sec: Optional[float] = None,
        error_sink: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {}
        if timeout_sec is not None:
            kwargs["timeout_sec"] = float(timeout_sec)
        if error_sink is not None:
            kwargs["error_sink"] = error_sink

        while True:
            try:
                return await self._post_json(base, endpoint, payload, api_key, **kwargs)
            except TypeError as exc:
                text = str(exc)
                removed = False
                for keyword in ("timeout_sec", "error_sink"):
                    if keyword in kwargs and keyword in text:
                        kwargs.pop(keyword, None)
                        removed = True
                if not removed:
                    raise
                if not kwargs:
                    return await self._post_json(base, endpoint, payload, api_key)

    async def _call_reranker_post_json(
        self,
        base: str,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str = "",
        *,
        timeout_sec: Optional[float] = None,
        error_sink: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        for attempt in range(1, _RERANKER_REQUEST_MAX_ATTEMPTS + 1):
            attempt_error_info: Dict[str, Any] = {}
            response = await self._call_post_json(
                base,
                endpoint,
                payload,
                api_key,
                timeout_sec=timeout_sec,
                error_sink=attempt_error_info,
            )
            if response is not None or attempt == _RERANKER_REQUEST_MAX_ATTEMPTS:
                if error_sink is not None:
                    error_sink.clear()
                    error_sink.update(attempt_error_info)
                return response
            await asyncio.sleep(
                _RERANKER_REQUEST_BASE_BACKOFF_SEC * float(attempt)
            )
        return None

    async def _post_json(
        self,
        base: str,
        endpoint: str,
        payload: Dict[str, Any],
        api_key: str = "",
        timeout_sec: Optional[float] = None,
        error_sink: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not base:
            return None

        url = self._join_api_url(base, endpoint)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            headers["X-API-Key"] = api_key

        try:
            timeout = httpx.Timeout(
                max(0.1, float(timeout_sec or self._remote_http_timeout_sec))
            )
            client = await self._get_remote_http_client()
            response = await client.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            parsed = response.json()
            if isinstance(parsed, dict):
                return parsed
            return {"data": parsed}
        except httpx.HTTPStatusError as exc:
            if error_sink is not None:
                response = exc.response
                error_sink.update(
                    {
                        "category": "http_status",
                        "status_code": response.status_code if response is not None else None,
                        "body": (
                            response.text[:1000]
                            if response is not None and isinstance(response.text, str)
                            else ""
                        ),
                    }
                )
            return None
        except httpx.RequestError as exc:
            if error_sink is not None:
                error_sink.update(
                    {
                        "category": "request_error",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
            return None
        except httpx.InvalidURL as exc:
            if error_sink is not None:
                error_sink.update(
                    {
                        "category": "invalid_url",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
            return None
        except (ValueError, TypeError) as exc:
            if error_sink is not None:
                error_sink.update(
                    {
                        "category": "response_parse_error",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
            return None

    async def _get_remote_http_client(self) -> httpx.AsyncClient:
        async with self._remote_http_client_guard:
            if self._remote_http_client is None:
                self._remote_http_client = httpx.AsyncClient()
            return self._remote_http_client

    async def _fetch_remote_embedding(
        self, content: str, degrade_reasons: Optional[List[str]] = None
    ) -> Optional[List[float]]:
        if self._embedding_backend not in {"router", "api", "openai"}:
            return None
        if not self._embedding_api_base or not self._embedding_model:
            self._append_degrade_reason(degrade_reasons, "embedding_config_missing")
            return None

        payload = {
            "model": self._embedding_model,
            "input": content,
            "dimensions": self._embedding_dim,
        }
        error_info: Dict[str, Any] = {}
        response = await self._post_json_with_optional_error_sink(
            self._embedding_api_base,
            "/embeddings",
            payload,
            self._embedding_api_key,
            error_sink=error_info,
        )
        if response is None:
            self._append_request_failure_reasons(
                degrade_reasons,
                prefix="embedding_request_failed",
                error_info=error_info,
                backend=(self._embedding_backend or "").strip().lower() or None,
            )
            return None

        embedding = self._extract_embedding_from_response(response)
        if embedding is None:
            self._append_degrade_reason(degrade_reasons, "embedding_response_invalid")
            return None
        return self._validate_embedding_dimension(
            embedding,
            degrade_reasons=degrade_reasons,
            backend=(self._embedding_backend or "").strip().lower() or None,
        )

    async def _fetch_remote_embedding_for_backend(
        self,
        *,
        backend: str,
        content: str,
        degrade_reasons: Optional[List[str]] = None,
    ) -> Optional[List[float]]:
        backend_value = (backend or "").strip().lower()
        if backend_value not in {"router", "api", "openai"}:
            return None

        api_base = self._resolve_embedding_api_base(backend_value)
        model = self._resolve_embedding_model(backend_value)
        api_key = self._resolve_embedding_api_key(backend_value)
        if not api_base or not model:
            self._append_degrade_reason(degrade_reasons, "embedding_config_missing")
            self._append_degrade_reason(
                degrade_reasons, f"embedding_config_missing:{backend_value}"
            )
            return None

        payload = {
            "model": model,
            "input": content,
            "dimensions": self._embedding_dim,
        }
        error_info: Dict[str, Any] = {}
        response = await self._post_json_with_optional_error_sink(
            api_base,
            "/embeddings",
            payload,
            api_key,
            error_sink=error_info,
        )
        if response is None:
            self._append_request_failure_reasons(
                degrade_reasons,
                prefix="embedding_request_failed",
                error_info=error_info,
                backend=backend_value,
            )
            return None

        embedding = self._extract_embedding_from_response(response)
        if embedding is None:
            self._append_degrade_reason(degrade_reasons, "embedding_response_invalid")
            self._append_degrade_reason(
                degrade_reasons, f"embedding_response_invalid:{backend_value}"
            )
            return None
        return self._validate_embedding_dimension(
            embedding,
            degrade_reasons=degrade_reasons,
            backend=backend_value,
        )

    async def _get_embedding_via_provider_chain(
        self,
        *,
        normalized: str,
        degrade_reasons: Optional[List[str]] = None,
    ) -> Tuple[List[float], str]:
        attempted_backends: set[str] = set()
        for backend in self._embedding_provider_candidates:
            backend_value = (backend or "").strip().lower()
            if not backend_value:
                continue
            attempted_backends.add(backend_value)

            if backend_value in {"hash", "none", "off", "disabled", "false", "0"}:
                continue

            embedding = await self._fetch_remote_embedding_for_backend(
                backend=backend_value,
                content=normalized,
                degrade_reasons=degrade_reasons,
            )
            if embedding is not None:
                return embedding, backend_value
            self._append_degrade_reason(
                degrade_reasons, f"embedding_provider_failed:{backend_value}"
            )
            if not self._embedding_provider_fail_open:
                break

        fallback_backend = self._resolve_chain_fallback_backend()
        if (
            fallback_backend in {"api", "router", "openai"}
            and fallback_backend not in attempted_backends
        ):
            embedding = await self._fetch_remote_embedding_for_backend(
                backend=fallback_backend,
                content=normalized,
                degrade_reasons=degrade_reasons,
            )
            if embedding is not None:
                return embedding, fallback_backend
            self._append_degrade_reason(
                degrade_reasons, f"embedding_provider_failed:{fallback_backend}"
            )

        if fallback_backend in {"hash", "", "default"} or self._embedding_provider_fail_open:
            self._append_degrade_reason(degrade_reasons, "embedding_fallback_hash")
            return self._hash_embedding(normalized, self._embedding_dim), "hash"

        self._append_degrade_reason(degrade_reasons, "embedding_provider_chain_blocked")
        raise RuntimeError("embedding_provider_chain_blocked")

    async def _get_rerank_scores(
        self,
        query: str,
        documents: List[str],
        degrade_reasons: Optional[List[str]] = None,
    ) -> Dict[int, float]:
        if not self._reranker_enabled or not documents:
            return {}
        reranker_attempts = self._resolve_reranker_attempts(documents)
        if not reranker_attempts:
            self._append_degrade_reason(degrade_reasons, "reranker_config_missing")
            return {}

        for index, attempt in enumerate(reranker_attempts):
            endpoint, payload = self._build_rerank_request(
                query,
                documents,
                provider=str(attempt.get("provider") or ""),
                model=str(attempt.get("model") or ""),
            )
            error_info: Dict[str, Any] = {}
            response = await self._call_reranker_post_json(
                str(attempt.get("base") or ""),
                endpoint,
                payload,
                str(attempt.get("api_key") or ""),
                timeout_sec=float(attempt.get("timeout_sec") or self._reranker_timeout_sec),
                error_sink=error_info,
            )
            if response is None:
                self._append_request_failure_reasons(
                    degrade_reasons,
                    prefix="reranker_request_failed",
                    error_info=error_info,
                    backend=str(attempt.get("provider") or "").strip().lower() or None,
                )
                if index + 1 < len(reranker_attempts):
                    self._append_degrade_reason(
                        degrade_reasons, "reranker_primary_failed_fallback_attempted"
                    )
                    continue
                return {}

            parsed_scores = self._extract_rerank_scores(response, len(documents))
            if parsed_scores:
                if str(attempt.get("name") or "") == "fallback":
                    self._append_degrade_reason(degrade_reasons, "reranker_fallback_used")
                return parsed_scores

            self._append_degrade_reason(degrade_reasons, "reranker_response_invalid")
            if index + 1 < len(reranker_attempts):
                self._append_degrade_reason(
                    degrade_reasons, "reranker_primary_invalid_fallback_attempted"
                )
                continue
            return {}

        return {}

    def _chunk_content(self, content: str) -> List[Tuple[int, int, int, str]]:
        if not content:
            return []

        chunks: List[Tuple[int, int, int, str]] = []
        total_len = len(content)
        start = 0
        index = 0

        while start < total_len:
            end = self._advance_chunk_budget(content, start, total_len)
            if end < total_len:
                extended_end = self._extend_chunk_end_for_code_fence(
                    content,
                    start,
                    end,
                    total_len,
                )
                if extended_end > end:
                    end = extended_end
                else:
                    split_point = self._find_chunk_split_point(content, start, end)
                    if split_point > start:
                        end = split_point

            if end <= start:
                end = min(total_len, start + self._chunk_size)
                if end <= start:
                    break

            chunk_text = content[start:end]
            if chunk_text.strip():
                chunks.append((index, start, end, chunk_text))
                index += 1

            if end >= total_len:
                break
            start = max(end - self._chunk_overlap, start + 1)

        return chunks

    @staticmethod
    def _chunk_char_weight(char: str) -> int:
        if unicodedata.east_asian_width(char) in {"W", "F"}:
            return 2
        return 1

    def _advance_chunk_budget(self, content: str, start: int, total_len: int) -> int:
        if start >= total_len:
            return total_len

        budget = 0
        end = start
        while end < total_len:
            budget += self._chunk_char_weight(content[end])
            end += 1
            if budget >= self._chunk_size:
                break
        return end

    def _extend_chunk_end_for_code_fence(
        self,
        content: str,
        start: int,
        end: int,
        total_len: int,
    ) -> int:
        window = content[start:end]
        search_end = min(total_len, end + max(64, self._chunk_size // 2))
        nearby_opening_index = content.find("```", end, min(total_len, end + 32))

        if "```" in window and window.count("```") % 2 == 1:
            closing_index = content.find("```", end, search_end)
        elif nearby_opening_index != -1:
            closing_index = content.find("```", nearby_opening_index + 3, search_end)
        else:
            return end

        if closing_index == -1:
            return end

        line_end = content.find("\n", closing_index + 3, search_end)
        if line_end != -1:
            if content.startswith("\n\n", line_end):
                return min(search_end, line_end + 2)
            return min(search_end, line_end + 1)
        return min(search_end, closing_index + 3)

    def _find_chunk_split_point(self, content: str, start: int, end: int) -> int:
        paragraph_min_split = start + max(16, int((end - start) * 0.25))
        if paragraph_min_split < end:
            paragraph_split = content.rfind("\n\n", paragraph_min_split, end)
            if paragraph_split >= paragraph_min_split:
                return paragraph_split + 2

        min_split = start + max(32, int((end - start) * 0.5))
        if min_split >= end:
            return end

        structure_candidates = [
            content.rfind(marker, min_split, end) for marker in ("\n```", "\n#")
        ]
        structure_split = max(structure_candidates)
        if structure_split >= min_split:
            return structure_split

        hard_boundary = -1
        for pos in range(end - 1, min_split - 1, -1):
            
            char = content[pos]
            if char in "。！？；":
                hard_boundary = max(hard_boundary, pos + 1)
                continue
            if char in ".!?;:":
                prev_char = content[pos - 1] if pos > start else ""
                if prev_char and not prev_char.isspace():
                    hard_boundary = max(hard_boundary, pos + 1)
                    continue
            if char == "\n":
                hard_boundary = max(hard_boundary, pos + 1)
                continue

        if hard_boundary > start:
            return hard_boundary

        split_newline = content.rfind("\n", min_split, end)
        if split_newline >= min_split:
            return split_newline

        split_cjk_punct = max(
            content.rfind(marker, min_split, end)
            for marker in ("，", "、")
        )
        if split_cjk_punct >= min_split:
            return split_cjk_punct + 1

        split_space = content.rfind(" ", min_split, end)
        if split_space >= min_split:
            return split_space

        return end

    def _hash_embedding(self, content: str, dim: Optional[int] = None) -> List[float]:
        embed_dim = dim or self._embedding_dim
        vector = [0.0] * embed_dim

        normalized = re.sub(r"\s+", " ", content.strip().lower())
        tokens = SQLiteClientRetrievalMixin._tokenize_mmr_source(normalized)
        if not tokens and normalized:
            tokens = list(normalized)

        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            for i in range(0, 8, 2):
                idx = digest[i] % embed_dim
                sign = -1.0 if (digest[i + 1] & 1) else 1.0
                weight = 1.0 + (digest[(i + 2) % len(digest)] / 255.0)
                vector[idx] += sign * weight

        norm = math.sqrt(sum(v * v for v in vector))
        if norm <= 0:
            return [0.0] * embed_dim
        return [v / norm for v in vector]

    async def _get_embedding(
        self,
        session: AsyncSession,
        content: str,
        degrade_reasons: Optional[List[str]] = None,
    ) -> List[float]:
        normalized = re.sub(r"\s+", " ", content.strip().lower())
        text_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

        primary_embedding_backend = (
            (self._embedding_backend or "hash").strip().lower() or "hash"
        )

        def _resolve_embedding_cache_key(backend: str) -> Tuple[str, str]:
            backend_value = (backend or "hash").strip().lower() or "hash"
            requested_dim = max(16, int(self._embedding_dim))
            if backend_value in {"router", "api", "openai"}:
                api_base = self._resolve_embedding_api_base(backend_value)
                model = self._resolve_embedding_model(backend_value) or self._embedding_model
            elif backend_value in {"hash", "local"}:
                api_base = ""
                model = f"{backend_value}-dim-{requested_dim}"
            else:
                api_base = ""
                model = self._embedding_model or backend_value

            namespace = hashlib.sha256(
                f"{backend_value}|{api_base.rstrip('/').lower()}|{model}|dim={requested_dim}".encode("utf-8")
            ).hexdigest()[:24]
            return f"{namespace}:{text_hash}", model

        cache_key, cache_model = _resolve_embedding_cache_key(primary_embedding_backend)

        cache_row = await self._read_embedding_cache_row_best_effort(
            cache_key,
            session=session,
        )
        if cache_row:
            try:
                embedding = json.loads(cache_row.embedding)
                if isinstance(embedding, list):
                    return [float(v) for v in embedding]
            except (TypeError, ValueError):
                pass

        embedding: Optional[List[float]] = None
        cache_backend_used = primary_embedding_backend
        if self._embedding_provider_chain_enabled:
            embedding, cache_backend_used = await self._get_embedding_via_provider_chain(
                normalized=normalized,
                degrade_reasons=degrade_reasons,
            )
        else:
            backend_value = (self._embedding_backend or "hash").strip().lower()
            cache_backend_used = backend_value or "hash"

            if backend_value in {"router", "api", "openai"}:
                embedding = await self._fetch_remote_embedding(
                    normalized, degrade_reasons=degrade_reasons
                )
                if embedding is None:
                    self._append_degrade_reason(degrade_reasons, "embedding_fallback_hash")
                    cache_backend_used = "hash"
            elif backend_value not in {
                "hash",
                "local",
                "none",
                "off",
                "disabled",
                "false",
                "0",
            }:
                self._append_degrade_reason(degrade_reasons, "embedding_backend_unsupported")

        if embedding is None:
            embedding = self._hash_embedding(normalized, self._embedding_dim)
            cache_backend_used = "hash"
        cache_key_to_persist, cache_model_to_persist = _resolve_embedding_cache_key(
            cache_backend_used
        )

        payload = json.dumps(embedding, separators=(",", ":"))
        await self._persist_embedding_cache_best_effort(
            cache_key=cache_key_to_persist,
            text_hash=text_hash,
            model=cache_model_to_persist,
            payload=payload,
            session=session,
        )
        return embedding

    async def _read_embedding_cache_row_best_effort(
        self,
        cache_key: str,
        *,
        session: Optional[AsyncSession] = None,
    ) -> Optional[EmbeddingCache]:
        try:
            if session is not None:
                with session.no_autoflush:
                    return await session.get(EmbeddingCache, cache_key)
            async with self.async_session() as cache_session:
                with cache_session.no_autoflush:
                    return await cache_session.get(EmbeddingCache, cache_key)
        except (sqlite3.OperationalError, SQLAlchemyOperationalError) as exc:
            if not self._is_sqlite_lock_error(exc):
                raise
            logger.debug(
                "embedding cache read skipped due to sqlite lock",
                extra={"cache_key": cache_key},
            )
            return None

    async def _upsert_embedding_cache(
        self,
        *,
        cache_key: str,
        text_hash: str,
        model: str,
        payload: str,
        session: Optional[AsyncSession] = None,
    ) -> None:
        timestamp = _utc_now_naive()
        upsert_statement = sqlite_insert(EmbeddingCache.__table__).values(
            cache_key=cache_key,
            text_hash=text_hash,
            model=model,
            embedding=payload,
            updated_at=timestamp,
        )
        upsert_statement = upsert_statement.on_conflict_do_update(
            index_elements=[EmbeddingCache.__table__.c.cache_key],
            set_={
                "text_hash": text_hash,
                "model": model,
                "embedding": payload,
                "updated_at": timestamp,
            },
        )

        if session is not None:
            await session.execute(upsert_statement)
            return

        async with self.async_session() as cache_session:
            await cache_session.execute(upsert_statement)
            await cache_session.commit()

    async def _persist_embedding_cache_best_effort(
        self,
        *,
        cache_key: str,
        text_hash: str,
        model: str,
        payload: str,
        session: Optional[AsyncSession] = None,
    ) -> bool:
        try:
            await self._upsert_embedding_cache(
                cache_key=cache_key,
                text_hash=text_hash,
                model=model,
                payload=payload,
                session=session,
            )
            return True
        except (sqlite3.OperationalError, SQLAlchemyOperationalError) as exc:
            if not self._is_sqlite_lock_error(exc):
                raise
            logger.debug(
                "embedding cache persist skipped due to sqlite lock",
                extra={"cache_key": cache_key},
            )
            return False

    async def _clear_memory_index(self, session: AsyncSession, memory_id: int) -> None:
        if self._fts_available:
            try:
                await session.execute(
                    text(
                        "DELETE FROM memory_chunks_fts "
                        "WHERE memory_id = :memory_id"
                    ),
                    {"memory_id": memory_id},
                )
            except Exception as exc:
                # FTS virtual table might be unavailable at runtime after migrations.
                await self._handle_fts_runtime_error(
                    session,
                    exc,
                    context="delete",
                )

        await session.execute(
            delete(MemoryChunkVec).where(MemoryChunkVec.memory_id == memory_id)
        )
        await self._delete_vec_knn_rows(session, memory_id=memory_id)
        await session.execute(
            delete(MemoryChunk).where(MemoryChunk.memory_id == memory_id)
        )

    async def _clear_all_retrieval_indexes(self, session: AsyncSession) -> None:
        """Drop all retrieval artifacts before a full rebuild."""
        if self._fts_available:
            try:
                await session.execute(text("DELETE FROM memory_chunks_fts"))
            except Exception as exc:
                await self._handle_fts_runtime_error(
                    session,
                    exc,
                    context="delete",
                )

        await session.execute(delete(MemoryChunkVec))

        if self._sqlite_vec_knn_table:
            try:
                await session.execute(
                    text(f"DELETE FROM {self._sqlite_vec_knn_table}")
                )
            except Exception:
                # vec0 table is optional; keep rebuild path robust.
                self._sqlite_vec_knn_ready = False

        await session.execute(delete(MemoryChunk))

    async def _delete_vec_knn_rows(
        self, session: AsyncSession, *, memory_id: int
    ) -> None:
        try:
            await session.execute(
                text(
                    f"DELETE FROM {self._sqlite_vec_knn_table} "
                    "WHERE rowid IN ("
                    "  SELECT id FROM memory_chunks WHERE memory_id = :memory_id"
                    ")"
                ),
                {"memory_id": int(memory_id)},
            )
        except Exception:
            # vec0 table is optional; keep clear-index path robust.
            self._sqlite_vec_knn_ready = False

    async def _upsert_vec_knn_rows(
        self, session: AsyncSession, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        if not rows:
            return
        try:
            await session.execute(
                text(
                    f"DELETE FROM {self._sqlite_vec_knn_table} "
                    "WHERE rowid = :chunk_id"
                ),
                [
                    {"chunk_id": int(row.get("chunk_id") or 0)}
                    for row in rows
                    if int(row.get("chunk_id") or 0) > 0
                ],
            )
            await session.execute(
                text(
                    f"INSERT INTO {self._sqlite_vec_knn_table}("
                    "rowid, vector"
                    ") VALUES (:chunk_id, vec_f32(:vector))"
                ),
                [
                    {
                        "chunk_id": int(row.get("chunk_id") or 0),
                        "vector": str(row.get("vector") or "[]"),
                    }
                    for row in rows
                    if int(row.get("chunk_id") or 0) > 0
                ],
            )
            self._sqlite_vec_knn_ready = True
        except Exception:
            # vec0 table is optional; writes continue through legacy table.
            self._sqlite_vec_knn_ready = False

    async def _reindex_memory(self, session: AsyncSession, memory_id: int) -> int:
        await self._clear_memory_index(session, memory_id)

        memory_result = await session.execute(
            select(Memory).where(Memory.id == memory_id)
        )
        memory = memory_result.scalar_one_or_none()
        if not memory or memory.deprecated:
            await self._set_index_meta(session, "last_indexed_memory_id", str(memory_id))
            await self._set_index_meta(session, "last_indexed_at", _utc_now_naive().isoformat())
            return 0

        chunks = self._chunk_content(memory.content or "")
        if not chunks:
            await self._set_index_meta(session, "last_indexed_memory_id", str(memory_id))
            await self._set_index_meta(session, "last_indexed_at", _utc_now_naive().isoformat())
            return 0

        chunk_rows: List[MemoryChunk] = []
        for chunk_index, char_start, char_end, chunk_text in chunks:
            chunk_rows.append(
                MemoryChunk(
                    memory_id=memory_id,
                    chunk_index=chunk_index,
                    chunk_text=chunk_text,
                    char_start=char_start,
                    char_end=char_end,
                )
            )

        session.add_all(chunk_rows)
        await session.flush()

        vec_rows: List[MemoryChunkVec] = []
        vec_knn_rows: List[Dict[str, Any]] = []
        embedding_cache: Dict[str, List[float]] = {}
        for chunk in chunk_rows:
            if self._vector_available:
                embedding_key = re.sub(r"\s+", " ", (chunk.chunk_text or "").strip().lower())
                embedding = embedding_cache.get(embedding_key)
                if embedding is None:
                    embedding = await self._get_embedding(session, chunk.chunk_text)
                    embedding_cache[embedding_key] = embedding
                vector_payload = json.dumps(embedding, separators=(",", ":"))
                vec_rows.append(
                    MemoryChunkVec(
                        chunk_id=chunk.id,
                        memory_id=memory_id,
                        vector=vector_payload,
                        model=self._embedding_model,
                        dim=len(embedding),
                    )
                )
                vec_knn_rows.append(
                    {
                        "chunk_id": int(chunk.id),
                        "vector": vector_payload,
                    }
                )
            if self._fts_available:
                try:
                    await session.execute(
                        text(
                            "DELETE FROM memory_chunks_fts "
                            "WHERE rowid = :rowid"
                        ),
                        {
                            "rowid": chunk.id,
                        },
                    )
                except Exception as exc:
                    await self._handle_fts_runtime_error(
                        session,
                        exc,
                        context="delete",
                    )
            if self._fts_available:
                try:
                    await session.execute(
                        text(
                            "INSERT INTO memory_chunks_fts("
                            "rowid, chunk_id, memory_id, chunk_text"
                            ") VALUES (:rowid, :chunk_id, :memory_id, :chunk_text)"
                        ),
                        {
                            "rowid": chunk.id,
                            "chunk_id": chunk.id,
                            "memory_id": memory_id,
                            "chunk_text": chunk.chunk_text,
                        },
                    )
                except Exception as exc:
                    await self._handle_fts_runtime_error(
                        session,
                        exc,
                        context="insert",
                    )

        if vec_rows:
            session.add_all(vec_rows)
            await session.flush()
            await self._upsert_vec_knn_rows(session, vec_knn_rows)
        await self._set_index_meta(session, "last_indexed_memory_id", str(memory_id))
        await self._set_index_meta(session, "last_indexed_at", _utc_now_naive().isoformat())
        return len(chunk_rows)

    @staticmethod
    def _cosine_similarity(v1: List[float], v2: List[float]) -> float:
        if not v1 or not v2:
            return 0.0
        length = min(len(v1), len(v2))
        if length == 0:
            return 0.0
        return float(sum(v1[i] * v2[i] for i in range(length)))

    async def _fetch_semantic_rows_python_scoring(
        self,
        session: AsyncSession,
        *,
        where_clause: str,
        where_params: Dict[str, Any],
        query_embedding: List[float],
        semantic_pool_limit: int,
        candidate_limit: int,
    ) -> List[Dict[str, Any]]:
        semantic_result = await session.execute(
            text(
                "SELECT "
                "mc.id AS chunk_id, mc.memory_id AS memory_id, "
                "mc.chunk_text AS chunk_text, mc.char_start AS char_start, mc.char_end AS char_end, "
                "mcv.vector AS vector_json, "
                "p.domain AS domain, p.path AS path, p.priority AS priority, p.disclosure AS disclosure, "
                "m.created_at AS created_at, "
                "m.vitality_score AS vitality_score, "
                "m.access_count AS access_count, "
                "m.last_accessed_at AS last_accessed_at, "
                "LENGTH(mc.chunk_text) AS chunk_length "
                "FROM memory_chunks_vec mcv "
                "JOIN memory_chunks mc ON mc.id = mcv.chunk_id "
                "JOIN memories m ON m.id = mc.memory_id "
                "JOIN paths p ON p.memory_id = mc.memory_id "
                f"WHERE {where_clause} "
            ),
            where_params,
        )

        semantic_scored: List[Tuple[float, Dict[str, Any]]] = []
        for row in semantic_result.mappings().all():
            vector_payload = row.get("vector_json")
            if not vector_payload:
                continue
            try:
                chunk_vec = [float(v) for v in json.loads(vector_payload)]
            except (TypeError, ValueError):
                continue
            similarity = self._cosine_similarity(query_embedding, chunk_vec)
            semantic_scored.append((similarity, dict(row)))

        semantic_scored.sort(key=lambda item: item[0], reverse=True)
        semantic_rows: List[Dict[str, Any]] = []
        scoring_limit = max(candidate_limit, min(semantic_pool_limit, len(semantic_scored)))
        for similarity, row in semantic_scored[:scoring_limit]:
            row["vector_similarity"] = similarity
            semantic_rows.append(row)
        return semantic_rows[:candidate_limit]

    async def _fetch_semantic_rows_vec_native_topk(
        self,
        session: AsyncSession,
        *,
        where_clause: str,
        where_params: Dict[str, Any],
        query_embedding: List[float],
        semantic_pool_limit: int,
        candidate_limit: int,
    ) -> List[Dict[str, Any]]:
        if not self._sqlite_vec_knn_ready:
            raise RuntimeError("sqlite_vec_knn_not_ready")
        if len(query_embedding) != int(self._sqlite_vec_knn_dim):
            raise RuntimeError(
                f"sqlite_vec_knn_dim_mismatch:{len(query_embedding)}!={self._sqlite_vec_knn_dim}"
            )

        query_vector_json = json.dumps(
            [float(value) for value in query_embedding],
            separators=(",", ":"),
        )
        base_vec_k = max(1, int(candidate_limit))

        async def _query_with_k(vec_k: int) -> List[Dict[str, Any]]:
            semantic_result = await session.execute(
                text(
                    "WITH knn AS ("
                    "  SELECT "
                    "    rowid AS chunk_id, "
                    "    CAST(distance AS REAL) AS vector_distance "
                    f"  FROM {self._sqlite_vec_knn_table} "
                    "  WHERE vector MATCH vec_f32(:query_vector_json) "
                    "    AND k = :vec_k "
                    "  ORDER BY distance ASC "
                    "), "
                    "semantic_scored AS ("
                    "  SELECT "
                    "    mc.id AS chunk_id, mc.memory_id AS memory_id, "
                    "    mc.chunk_text AS chunk_text, mc.char_start AS char_start, mc.char_end AS char_end, "
                    "    p.domain AS domain, p.path AS path, p.priority AS priority, p.disclosure AS disclosure, "
                    "    m.created_at AS created_at, "
                    "    m.vitality_score AS vitality_score, "
                    "    m.access_count AS access_count, "
                    "    m.last_accessed_at AS last_accessed_at, "
                    "    LENGTH(mc.chunk_text) AS chunk_length, "
                    "    knn.vector_distance AS vector_distance "
                    "  FROM knn "
                    "  JOIN memory_chunks mc ON mc.id = knn.chunk_id "
                    "  JOIN memories m ON m.id = mc.memory_id "
                    "  JOIN paths p ON p.memory_id = mc.memory_id "
                    f"  WHERE {where_clause} "
                    ") "
                    "SELECT "
                    "  chunk_id, memory_id, chunk_text, char_start, char_end, "
                    "  domain, path, priority, disclosure, created_at, "
                    "  vitality_score, access_count, last_accessed_at, chunk_length, "
                    "  vector_distance, (1.0 - vector_distance) AS vector_similarity "
                    "FROM semantic_scored "
                    "WHERE vector_distance IS NOT NULL "
                    "ORDER BY vector_distance ASC "
                    "LIMIT :candidate_limit"
                ),
                {
                    **where_params,
                    "query_vector_json": query_vector_json,
                    "vec_k": int(max(1, vec_k)),
                    "candidate_limit": candidate_limit,
                },
            )
            semantic_rows = [dict(row) for row in semantic_result.mappings().all()]
            for row in semantic_rows:
                try:
                    similarity = float(row.get("vector_similarity") or 0.0)
                    if not math.isfinite(similarity):
                        similarity = 0.0
                    row["vector_similarity"] = similarity
                except (TypeError, ValueError):
                    row["vector_similarity"] = 0.0
            return semantic_rows

        semantic_rows = await _query_with_k(base_vec_k)
        if (
            len(semantic_rows) < int(candidate_limit)
            and int(base_vec_k) < int(semantic_pool_limit)
        ):
            fallback_vec_k = min(
                int(semantic_pool_limit),
                max(int(base_vec_k) * 2, int(base_vec_k) + 16),
            )
            semantic_rows = await _query_with_k(int(fallback_vec_k))
        return semantic_rows

    @staticmethod
    def _normalize_positive_int_ids(raw_ids: Optional[List[Any]]) -> List[int]:
        normalized_ids: List[int] = []
        seen_ids = set()
        if not raw_ids:
            return normalized_ids
        for item in raw_ids:
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed <= 0 or parsed in seen_ids:
                continue
            seen_ids.add(parsed)
            normalized_ids.append(parsed)
        return normalized_ids

    async def close(self):
        """Close the database connection."""
        async with self._remote_http_client_guard:
            remote_http_client = self._remote_http_client
            self._remote_http_client = None
        if remote_http_client is not None:
            await remote_http_client.aclose()
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self):
        """Get an async session context manager."""
        async with self.async_session() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def readonly_session(self):
        """Get a read-only async session that never commits on exit."""
        async with self.async_session() as session:
            try:
                yield session
            finally:
                await session.rollback()

    async def _reinforce_memory_access(
        self,
        session: AsyncSession,
        memory_ids: List[int],
    ) -> int:
        """
        Reinforce vitality when memories are read/retrieved.

        Reinforcement is intentionally bounded to avoid runaway scores.
        """
        normalized_ids = sorted(self._normalize_positive_int_ids(memory_ids))
        if not normalized_ids:
            return 0

        rows = await session.execute(
            select(Memory)
            .where(Memory.id.in_(normalized_ids))
            .where(Memory.deprecated == False)
        )
        memories = list(rows.scalars().all())
        if not memories:
            return 0

        now_value = _utc_now_naive()
        for memory in memories:
            current_access = max(0, int(memory.access_count or 0))
            next_access = current_access + 1
            diminishing_factor = 1.0 + math.log1p(next_access)
            boost = self._vitality_reinforce_delta / max(1.0, diminishing_factor)

            memory.access_count = next_access
            memory.last_accessed_at = now_value
            memory.vitality_score = min(
                self._vitality_max_score,
                max(0.0, float(memory.vitality_score or 1.0)) + boost,
            )
            session.add(memory)

        return len(memories)

    @classmethod
    def _is_sqlite_lock_error(cls, exc: Exception) -> bool:
        return any(
            "database is locked" in message or "database table is locked" in message
            for message in cls._iter_exception_messages(exc)
        )

    async def _best_effort_reinforce_memory_access(
        self,
        session: AsyncSession,
        memory_ids: List[int],
    ) -> bool:
        try:
            await self._reinforce_memory_access(session, memory_ids)
            await session.flush()
            return True
        except (sqlite3.OperationalError, SQLAlchemyOperationalError) as exc:
            if not self._is_sqlite_lock_error(exc):
                raise
            await session.rollback()
            return False

    async def apply_vitality_decay(
        self,
        *,
        force: bool = False,
        reason: str = "runtime",
        reference_time: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Apply at most once-per-day vitality decay unless forced.
        """
        now_value = self._normalize_db_datetime(reference_time) or _utc_now_naive()
        day_key = now_value.strftime("%Y-%m-%d")
        last_decay_day_key = "vitality.last_decay_day.v1"

        async with self.session() as session:
            meta_result = await session.execute(
                select(IndexMeta.value).where(IndexMeta.key == last_decay_day_key)
            )
            meta_value = meta_result.scalar_one_or_none()
            last_decay_day = str(meta_value) if meta_value is not None else None
            if (not force) and last_decay_day == day_key:
                return {
                    "applied": False,
                    "reason": "already_applied_today",
                    "day": day_key,
                    "last_decay_day": last_decay_day,
                }

            result = await session.execute(
                select(Memory).where(Memory.deprecated == False)
            )
            memories = list(result.scalars().all())

            updated_count = 0
            low_vitality_count = 0
            for memory in memories:
                current_score = max(0.0, float(memory.vitality_score or 1.0))
                access_count = max(0, int(memory.access_count or 0))
                reference_dt = (
                    self._normalize_db_datetime(memory.last_accessed_at)
                    or self._normalize_db_datetime(memory.created_at)
                    or now_value
                )
                age_days = max(
                    0.0, (now_value - reference_dt).total_seconds() / 86400.0
                )
                resistance = 1.0 + min(2.0, math.log1p(access_count) * 0.35)
                effective_age_days = age_days / resistance
                decay_ratio = math.exp(
                    -effective_age_days / self._vitality_decay_half_life_days
                )
                next_score = max(
                    self._vitality_decay_min_score, current_score * decay_ratio
                )
                if next_score < current_score - 1e-9:
                    memory.vitality_score = next_score
                    session.add(memory)
                    updated_count += 1
                if next_score <= self._vitality_cleanup_threshold:
                    low_vitality_count += 1

            await self._set_index_meta(session, last_decay_day_key, day_key)
            await self._set_index_meta(
                session,
                "vitality.last_decay_at",
                now_value.isoformat(),
            )
            await self._set_index_meta(
                session,
                "vitality.last_decay_reason",
                (reason or "runtime").strip() or "runtime",
            )

            return {
                "applied": True,
                "day": day_key,
                "checked_memories": len(memories),
                "updated_memories": updated_count,
                "low_vitality_count": low_vitality_count,
                "half_life_days": self._vitality_decay_half_life_days,
                "threshold": self._vitality_cleanup_threshold,
            }

    async def get_vitality_cleanup_candidates(
        self,
        *,
        threshold: Optional[float] = None,
        inactive_days: Optional[float] = None,
        limit: int = 50,
        domain: Optional[str] = None,
        path_prefix: Optional[str] = None,
        memory_ids: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """
        Query low-vitality cleanup candidates for human review.
        """
        threshold_value = (
            self._vitality_cleanup_threshold
            if threshold is None
            else max(0.0, float(threshold))
        )
        inactive_days_value = (
            self._vitality_cleanup_inactive_days
            if inactive_days is None
            else max(0.0, float(inactive_days))
        )
        limit_value = max(1, min(500, int(limit)))
        domain_value = domain.strip() if isinstance(domain, str) and domain.strip() else None
        path_prefix_value = (
            str(path_prefix).strip()
            if isinstance(path_prefix, str) and str(path_prefix).strip()
            else None
        )

        filter_ids: Optional[List[int]] = None
        if memory_ids is not None:
            filter_ids = self._normalize_positive_int_ids(memory_ids)
            if not filter_ids:
                return {
                    "items": [],
                    "summary": {
                        "total_candidates": 0,
                        "threshold": threshold_value,
                        "inactive_days": inactive_days_value,
                    },
                }

        now_value = _utc_now_naive()
        inactive_cutoff = now_value - timedelta(days=inactive_days_value)
        reference_dt_expr = func.coalesce(Memory.last_accessed_at, Memory.created_at)
        query_started_at = time.perf_counter()

        async with self.session() as session:
            memory_query = (
                select(Memory)
                .where(Memory.deprecated == False)
                .where(Memory.vitality_score <= threshold_value)
            )
            if inactive_days_value > 0:
                memory_query = memory_query.where(
                    or_(
                        and_(
                            Memory.last_accessed_at.is_not(None),
                            Memory.last_accessed_at <= inactive_cutoff,
                        ),
                        and_(
                            Memory.last_accessed_at.is_(None),
                            Memory.created_at <= inactive_cutoff,
                        ),
                    )
                )
            if filter_ids is not None:
                memory_query = memory_query.where(Memory.id.in_(filter_ids))
            if domain_value or path_prefix_value:
                path_scope_conditions = [Path.memory_id == Memory.id]
                if domain_value:
                    path_scope_conditions.append(Path.domain == domain_value)
                if path_prefix_value:
                    path_scope_conditions.append(
                        self._build_path_prefix_sqlalchemy_condition(
                            Path.path, path_prefix_value
                        )
                    )
                memory_query = memory_query.where(
                    select(Path.memory_id).where(*path_scope_conditions).exists()
                )

            memory_query = (
                memory_query.order_by(
                    Memory.vitality_score.asc(),
                    reference_dt_expr.asc(),
                    Memory.id.asc(),
                ).limit(limit_value)
            )
            plan_details: List[str] = []
            used_memory_cleanup_index = False
            used_path_scope_index = False
            full_scan_targets: List[str] = []
            explain_degrade_reason: Optional[str] = None
            try:
                # C-8: Compile the query to get parameterised SQL, then
                # convert positional ? placeholders to :named placeholders
                # so we can use text() + dict params through session.execute.
                # This avoids literal_binds entirely -- no user-controlled
                # values ever enter the SQL string.
                _compiled = memory_query.compile(
                    dialect=self.engine.sync_engine.dialect,
                )
                _named_sql = _compiled.string
                for _pname in (_compiled.positiontup or []):
                    _named_sql = _named_sql.replace("?", f":{_pname}", 1)
                _explain_stmt = text(f"EXPLAIN QUERY PLAN {_named_sql}")
                explain_rows = (
                    await session.execute(_explain_stmt, _compiled.params)
                ).all()
                for row in explain_rows:
                    detail_text = ""
                    try:
                        detail_text = str(row[3] or "")
                    except Exception:
                        detail_text = ""
                    if not detail_text:
                        continue
                    plan_details.append(detail_text)
                    detail_upper = detail_text.upper()
                    if "IDX_MEMORIES_CLEANUP_" in detail_upper:
                        used_memory_cleanup_index = True
                    if "IDX_PATHS_MEMORY_DOMAIN_PATH" in detail_upper:
                        used_path_scope_index = True
                    if (
                        "SCAN " in detail_upper
                        and "USING INDEX" not in detail_upper
                        and "USING COVERING INDEX" not in detail_upper
                    ):
                        if "MEMORIES" in detail_upper:
                            full_scan_targets.append("memories")
                        elif "PATHS" in detail_upper:
                            full_scan_targets.append("paths")
            except Exception:
                explain_degrade_reason = "cleanup_explain_failed"

            memory_rows = list((await session.execute(memory_query)).scalars().all())
            query_ms = round((time.perf_counter() - query_started_at) * 1000.0, 3)
            if not memory_rows:
                return {
                    "items": [],
                    "summary": {
                        "total_candidates": 0,
                        "threshold": threshold_value,
                        "inactive_days": inactive_days_value,
                        "query_profile": {
                            "query_ms": query_ms,
                            "memory_rows_considered": 0,
                            "path_rows_loaded": 0,
                            "index_usage": {
                                "memory_cleanup_index": used_memory_cleanup_index,
                                "path_scope_index": used_path_scope_index,
                            },
                            "full_scan": bool(full_scan_targets),
                            "full_scan_targets": sorted(set(full_scan_targets)),
                            "plan_details": plan_details[:8],
                            "degraded": explain_degrade_reason is not None,
                            "degrade_reason": explain_degrade_reason,
                        },
                    },
                }

            all_memory_ids = [int(memory.id) for memory in memory_rows]
            path_count_rows = (
                await session.execute(
                    select(Path.memory_id, func.count(Path.memory_id))
                    .where(Path.memory_id.in_(all_memory_ids))
                    .group_by(Path.memory_id)
                )
            ).all()
            path_count_by_memory: Dict[int, int] = {
                int(memory_id): int(count or 0)
                for memory_id, count in path_count_rows
            }

            ranked_path_query = (
                select(
                    Path.memory_id.label("memory_id"),
                    Path.domain.label("domain"),
                    Path.path.label("path"),
                    func.row_number()
                    .over(
                        partition_by=Path.memory_id,
                        order_by=(Path.priority.asc(), Path.path.asc()),
                    )
                    .label("row_num"),
                )
                .where(Path.memory_id.in_(all_memory_ids))
            )
            if domain_value:
                ranked_path_query = ranked_path_query.where(Path.domain == domain_value)
            if path_prefix_value:
                ranked_path_query = ranked_path_query.where(
                    self._build_path_prefix_sqlalchemy_condition(
                        Path.path, path_prefix_value
                    )
                )
            ranked_paths = ranked_path_query.subquery()
            top_path_rows = (
                await session.execute(
                    select(
                        ranked_paths.c.memory_id,
                        ranked_paths.c.domain,
                        ranked_paths.c.path,
                    ).where(ranked_paths.c.row_num == 1)
                )
            ).all()
            top_path_by_memory: Dict[int, Tuple[str, str]] = {
                int(row.memory_id): (str(row.domain), str(row.path))
                for row in top_path_rows
            }
            path_rows_loaded = len(path_count_rows) + len(top_path_rows)

            items: List[Dict[str, Any]] = []
            for memory in memory_rows:
                memory_id = int(memory.id)
                path_count = int(path_count_by_memory.get(memory_id, 0))
                top_path = top_path_by_memory.get(memory_id)
                if (domain_value or path_prefix_value) and top_path is None:
                    continue

                vitality_score = max(0.0, float(memory.vitality_score or 0.0))
                access_count = max(0, int(memory.access_count or 0))
                reference_dt = (
                    self._normalize_db_datetime(memory.last_accessed_at)
                    or self._normalize_db_datetime(memory.created_at)
                    or now_value
                )
                inactive_days_value_real = max(
                    0.0, (now_value - reference_dt).total_seconds() / 86400.0
                )

                reason_codes = ["low_vitality", "inactive"]
                if path_count == 0:
                    reason_codes.append("orphaned")

                state_hash = self._build_vitality_state_hash(
                    memory_id=memory_id,
                    vitality_score=vitality_score,
                    access_count=access_count,
                    path_count=path_count,
                    deprecated=bool(memory.deprecated),
                )

                items.append(
                    {
                        "memory_id": memory_id,
                        "uri": (
                            f"{top_path[0]}://{top_path[1]}" if top_path else None
                        ),
                        "path_count": path_count,
                        "vitality_score": round(vitality_score, 6),
                        "access_count": access_count,
                        "last_accessed_at": (
                            self._normalize_db_datetime(memory.last_accessed_at).isoformat()
                            if memory.last_accessed_at is not None
                            else None
                        ),
                        "inactive_days": round(inactive_days_value_real, 3),
                        "content_snippet": self._content_snippet(memory.content),
                        "reason_codes": reason_codes,
                        "can_delete": path_count == 0 or bool(memory.deprecated),
                        "state_hash": state_hash,
                    }
                )

            return {
                "items": items,
                "summary": {
                    "total_candidates": len(items),
                    "threshold": threshold_value,
                    "inactive_days": inactive_days_value,
                    "query_profile": {
                        "query_ms": query_ms,
                        "memory_rows_considered": len(memory_rows),
                        "path_rows_loaded": path_rows_loaded,
                        "index_usage": {
                            "memory_cleanup_index": used_memory_cleanup_index,
                            "path_scope_index": used_path_scope_index,
                        },
                        "full_scan": bool(full_scan_targets),
                        "full_scan_targets": sorted(set(full_scan_targets)),
                        "plan_details": plan_details[:8],
                        "degraded": explain_degrade_reason is not None,
                        "degrade_reason": explain_degrade_reason,
                    },
                },
            }

    async def get_vitality_stats(self) -> Dict[str, Any]:
        """Aggregate vitality stats for maintenance observability."""
        threshold_value = self._vitality_cleanup_threshold
        async with self.session() as session:
            total = int(
                (
                    await session.execute(
                        select(func.count(Memory.id)).where(Memory.deprecated == False)
                    )
                ).scalar()
                or 0
            )
            avg_score = float(
                (
                    await session.execute(
                        select(func.avg(Memory.vitality_score)).where(
                            Memory.deprecated == False
                        )
                    )
                ).scalar()
                or 0.0
            )
            min_score = float(
                (
                    await session.execute(
                        select(func.min(Memory.vitality_score)).where(
                            Memory.deprecated == False
                        )
                    )
                ).scalar()
                or 0.0
            )
            max_score = float(
                (
                    await session.execute(
                        select(func.max(Memory.vitality_score)).where(
                            Memory.deprecated == False
                        )
                    )
                ).scalar()
                or 0.0
            )
            low_count = int(
                (
                    await session.execute(
                        select(func.count(Memory.id))
                        .where(Memory.deprecated == False)
                        .where(Memory.vitality_score <= threshold_value)
                    )
                ).scalar()
                or 0
            )

        return {
            "total_memories": total,
            "avg_score": round(avg_score, 6),
            "min_score": round(min_score, 6),
            "max_score": round(max_score, 6),
            "low_vitality_count": low_count,
            "threshold": threshold_value,
        }

    # =========================================================================
    # Read Operations
    # =========================================================================

    async def get_memory_by_path(
        self, path: str, domain: str = "core", reinforce_access: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        Get a memory by its path.

        Args:
            path: The path to look up
            domain: The domain/namespace (e.g., "core", "writer", "game")
            reinforce_access: Whether to reinforce access_count/vitality on read

        Returns:
            Memory dict with id, content, priority, disclosure, created_at
            or None if not found
        """
        session_factory = self.session if reinforce_access else self.readonly_session
        async with session_factory() as session:
            result = await session.execute(
                select(Memory, Path)
                .join(Path, Memory.id == Path.memory_id)
                .where(Path.domain == domain)
                .where(Path.path == path)
                .where(Memory.deprecated == False)
            )
            row = result.first()

            if not row:
                return None

            memory, path_obj = row
            gist_map = await self._get_latest_gists_map(session, [memory.id])
            gist = gist_map.get(memory.id) or {}
            payload = {
                "id": memory.id,
                "content": memory.content,
                "priority": path_obj.priority,  # From Path
                "disclosure": path_obj.disclosure,  # From Path
                "deprecated": memory.deprecated,
                "created_at": memory.created_at.isoformat()
                if memory.created_at
                else None,
                "domain": path_obj.domain,
                "path": path_obj.path,
                "gist_text": gist.get("gist_text"),
                "gist_method": gist.get("gist_method"),
                "gist_quality": gist.get("quality_score"),
                "gist_source_hash": gist.get("source_hash"),
            }
            if reinforce_access:
                await self._best_effort_reinforce_memory_access(session, [memory.id])
            return payload

    async def get_memory_by_id(self, memory_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a memory by its ID (including deprecated ones).

        Args:
            memory_id: The memory ID

        Returns:
            Memory dict or None if not found
        """
        async with self.readonly_session() as session:
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            memory = result.scalar_one_or_none()

            if not memory:
                return None

            # Get all paths pointing to this memory (with domain info)
            paths_result = await session.execute(
                select(Path.domain, Path.path).where(Path.memory_id == memory_id)
            )
            # Return as list of "domain://path" URIs
            paths = [f"{row[0]}://{row[1]}" for row in paths_result.all()]

            payload = {
                "id": memory.id,
                "content": memory.content,
                # Priority/Disclosure removed as they are path-dependent
                "deprecated": memory.deprecated,
                "migrated_to": memory.migrated_to,
                "created_at": memory.created_at.isoformat()
                if memory.created_at
                else None,
                "paths": paths,
            }
            if not bool(memory.deprecated):
                await self._best_effort_reinforce_memory_access(session, [memory.id])
            return payload

    async def get_children(
        self, memory_id: Optional[int] = None, domain: str = "core"
    ) -> List[Dict[str, Any]]:
        """
        Get direct children of a memory node.

        When memory_id is given, finds ALL paths (aliases) pointing to that
        memory across all domains, then collects direct children under each.
        This models human associative recall: once you reach a memory, the
        sub-memories depend on WHAT it IS, not WHICH path you used to get here.

        When memory_id is None (virtual root), returns root-level paths
        (paths with no '/') in the given domain.

        Args:
            memory_id: The memory ID to find children for.
                       If None, returns domain root elements.
            domain: Only used when memory_id is None (root browsing).

        Returns:
            List of child memories (deduplicated by domain+path),
            sorted by priority then path.
        """
        async with self.readonly_session() as session:
            if memory_id is None:
                # Virtual root: return paths with no slashes in the given domain
                query = (
                    select(Memory, Path)
                    .join(Path, Memory.id == Path.memory_id)
                    .where(Path.domain == domain)
                    .where(Memory.deprecated == False)
                    .where(Path.path.not_like("%/%"))
                    .order_by(Path.priority.asc(), Path.path)
                )

                result = await session.execute(query)
                rows = result.all()
                gist_map = await self._get_latest_gists_map(
                    session, [memory.id for memory, _ in rows]
                )

                children = []
                for memory, path_obj in rows:
                    gist = gist_map.get(memory.id) or {}
                    children.append(
                        {
                            "domain": path_obj.domain,
                            "path": path_obj.path,
                            "name": path_obj.path.rsplit("/", 1)[-1],
                            "content_snippet": memory.content[:100] + "..."
                            if len(memory.content) > 100
                            else memory.content,
                            "priority": path_obj.priority,
                            "disclosure": path_obj.disclosure,
                            "gist_text": gist.get("gist_text"),
                            "gist_method": gist.get("gist_method"),
                            "gist_quality": gist.get("quality_score"),
                            "gist_source_hash": gist.get("source_hash"),
                        }
                    )

                return children

            # --- memory_id provided: find children across all aliases ---

            # 1. Find all paths pointing to this memory
            parent_paths_result = await session.execute(
                select(Path.domain, Path.path).where(Path.memory_id == memory_id)
            )
            parent_paths = parent_paths_result.all()

            if not parent_paths:
                return []

            # 2. Build OR conditions for children under each parent path
            child_conditions = []
            for parent_domain, parent_path in parent_paths:
                safe_parent = (
                    parent_path.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                safe_prefix = f"{safe_parent}/"

                child_conditions.append(
                    and_(
                        Path.domain == parent_domain,
                        Path.path.like(f"{safe_prefix}%", escape="\\"),
                        Path.path.not_like(f"{safe_prefix}%/%", escape="\\"),
                    )
                )

            # 3. Query all children in one shot
            query = (
                select(Memory, Path)
                .join(Path, Memory.id == Path.memory_id)
                .where(Memory.deprecated == False)
                .where(or_(*child_conditions))
                .order_by(Path.priority.asc(), Path.path)
            )

            result = await session.execute(query)
            rows = result.all()
            gist_map = await self._get_latest_gists_map(
                session, [memory.id for memory, _ in rows]
            )

            # 4. Deduplicate by (domain, path)
            seen = set()
            children = []
            for memory, path_obj in rows:
                key = (path_obj.domain, path_obj.path)
                if key in seen:
                    continue
                seen.add(key)
                gist = gist_map.get(memory.id) or {}

                children.append(
                    {
                        "domain": path_obj.domain,
                        "path": path_obj.path,
                        "name": path_obj.path.rsplit("/", 1)[-1],
                        "content_snippet": memory.content[:100] + "..."
                        if len(memory.content) > 100
                        else memory.content,
                        "priority": path_obj.priority,
                        "disclosure": path_obj.disclosure,
                        "gist_text": gist.get("gist_text"),
                        "gist_method": gist.get("gist_method"),
                        "gist_quality": gist.get("quality_score"),
                        "gist_source_hash": gist.get("source_hash"),
                    }
                )

            return children

    async def get_all_paths(self, domain: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all paths with their memory info.

        Args:
            domain: If specified, only return paths in this domain.
                    If None, return paths from all domains.

        Returns:
            List of path info dicts
        """
        async with self.session() as session:
            query = (
                select(Path, Memory)
                .join(Memory, Path.memory_id == Memory.id)
                .where(Memory.deprecated == False)
            )

            if domain is not None:
                query = query.where(Path.domain == domain)

            query = query.order_by(Path.domain, Path.path)
            result = await session.execute(query)

            paths = []
            for path_obj, memory in result.all():
                paths.append(
                    {
                        "domain": path_obj.domain,
                        "path": path_obj.path,
                        "uri": f"{path_obj.domain}://{path_obj.path}",
                        "name": path_obj.path.rsplit("/", 1)[
                            -1
                        ],  # Last segment of path
                        "priority": path_obj.priority,  # From Path
                        "memory_id": memory.id,
                    }
                )

            return paths

    # =========================================================================
    # Create Operations
    # =========================================================================

    # 512 KiB – generous for structured notes, strict enough to prevent OOM via
    # oversized payloads that would cascade into write_guard, chunking, and
    # embedding API calls.
    MAX_CONTENT_LENGTH = 512 * 1024

    @staticmethod
    def _validate_memory_content(content: str) -> None:
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Content must not be empty")
        if len(content.encode("utf-8")) > SQLiteClient.MAX_CONTENT_LENGTH:
            raise ValueError(
                f"Content exceeds maximum allowed size "
                f"({SQLiteClient.MAX_CONTENT_LENGTH // 1024} KiB)"
            )

    @staticmethod
    def _validate_memory_path(path: str, *, field_name: str = "path") -> None:
        candidate = str(path or "").strip()
        segments = candidate.split("/") if candidate else []
        if (
            not candidate
            or candidate.startswith("/")
            or candidate.endswith("/")
            or any(not segment for segment in segments)
            or any(not is_valid_memory_path_segment(segment) for segment in segments)
        ):
            raise ValueError(
                f"Invalid {field_name} '{path}'. Each path segment must only contain "
                "alphanumeric characters, underscores, or hyphens."
            )

    @staticmethod
    def _validate_priority_value(
        priority: int,
        *,
        field_name: str = "priority",
        minimum: int = 0,
        maximum: int = 999,
    ) -> None:
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise ValueError(
                f"{field_name} must be an integer between {minimum} and {maximum}."
            )
        if priority < minimum or priority > maximum:
            raise ValueError(
                f"{field_name} must be between {minimum} and {maximum}."
            )

    def _normalize_writable_domain(self, domain: str) -> str:
        normalized = str(domain or "").strip().lower()
        if normalized not in self._valid_domains:
            raise ValueError(
                f"Invalid domain '{normalized}'. Valid domains: {', '.join(self._valid_domains)}"
            )
        if normalized in self._read_only_domains:
            raise ValueError(
                f"Writes to '{normalized}://' are not allowed. "
                "system:// is read-only and reserved for built-in views."
            )
        return normalized

    async def create_memory(
        self,
        parent_path: str,
        content: str,
        priority: int,
        title: Optional[str] = None,
        disclosure: Optional[str] = None,
        domain: str = "core",
        index_now: bool = True,
    ) -> Dict[str, Any]:
        """
        Create a new memory under a parent path.

        Args:
            parent_path: Parent path (e.g. "memory-palace/salem")
            content: Memory content
            priority: Retrieval priority (lower = higher priority, min 0)
            title: Optional path segment name. If None, auto-assigns numeric ID.
                   This becomes the last segment of the path, NOT stored in memories table.
            disclosure: When to expand this memory
            domain: The domain/namespace (e.g., "core", "writer", "game")

        Returns:
            Created memory info with full path
        """
        domain = self._normalize_writable_domain(domain)
        self._validate_memory_content(content)
        self._validate_priority_value(priority)
        if parent_path:
            self._validate_memory_path(parent_path, field_name="parent_path")
        auto_id_mode = not bool(title)
        path_retry_attempts = _AUTO_PATH_RETRY_ATTEMPTS if auto_id_mode else 1
        lock_retry_attempts = _CREATE_MEMORY_LOCK_RETRY_ATTEMPTS if auto_id_mode else 1
        max_attempts = max(path_retry_attempts, lock_retry_attempts)
        last_error: Optional[Exception] = None

        for attempt in range(max_attempts):
            try:
                async with self.session() as session:
                    # Validate parent exists (if specified)
                    if parent_path:
                        parent_exists = await session.execute(
                            select(Path)
                            .where(Path.domain == domain)
                            .where(Path.path == parent_path)
                        )
                        if not parent_exists.scalar_one_or_none():
                            raise ValueError(
                                f"Parent '{domain}://{parent_path}' does not exist. "
                                f"Create the parent first, or use '{domain}://' as root."
                            )
                    if title and not is_valid_memory_path_segment(title):
                        raise ValueError(memory_path_segment_error_message())

                    if title:
                        final_path = (
                            f"{parent_path}/{title}" if parent_path else title
                        )
                    else:
                        next_num = await self._reserve_next_numeric_id(
                            session, parent_path, domain
                        )
                        final_path = (
                            f"{parent_path}/{next_num}"
                            if parent_path
                            else str(next_num)
                        )

                    existing = await session.execute(
                        select(Path)
                        .where(Path.domain == domain)
                        .where(Path.path == final_path)
                    )
                    if existing.scalar_one_or_none():
                        if auto_id_mode and attempt < path_retry_attempts - 1:
                            await asyncio.sleep(
                                _AUTO_PATH_RETRY_BASE_DELAY_SEC * (attempt + 1)
                            )
                            continue
                        if auto_id_mode:
                            raise ValueError(
                                f"Path creation for '{domain}://{parent_path or ''}' conflicted; retry the request."
                            )
                        raise ValueError(
                            f"Path '{domain}://{final_path}' already exists"
                        )

                    memory = Memory(content=content)
                    session.add(memory)
                    await session.flush()

                    path_obj = Path(
                        domain=domain,
                        path=final_path,
                        memory_id=memory.id,
                        priority=priority,
                        disclosure=disclosure,
                    )
                    session.add(path_obj)
                    await session.flush()

                    if title and title.isdigit():
                        await self._advance_auto_path_counter_floor(
                            session,
                            parent_path=parent_path,
                            domain=domain,
                            floor_value=int(title) + 1,
                        )

                    indexed_chunks = 0
                    if index_now:
                        indexed_chunks = await self._reindex_memory(session, memory.id)

                    return {
                        "id": memory.id,
                        "domain": domain,
                        "path": final_path,
                        "uri": f"{domain}://{final_path}",
                        "priority": priority,
                        "indexed_chunks": indexed_chunks,
                        "index_pending": not index_now,
                        "index_targets": [memory.id],
                    }
            except SQLAlchemyIntegrityError as exc:
                last_error = exc
                unique_path_conflict = (
                    "unique constraint failed: paths.domain, paths.path"
                    in " ".join(self._iter_exception_messages(exc))
                )
                if auto_id_mode and unique_path_conflict and attempt < path_retry_attempts - 1:
                    await asyncio.sleep(
                        _AUTO_PATH_RETRY_BASE_DELAY_SEC * (attempt + 1)
                    )
                    continue
                if unique_path_conflict:
                    raise ValueError(
                        f"Path creation for '{domain}://{parent_path or ''}' conflicted; retry the request."
                    ) from exc
                raise
            except (sqlite3.OperationalError, SQLAlchemyOperationalError) as exc:
                last_error = exc
                if not self._is_sqlite_lock_error(exc) or attempt >= lock_retry_attempts - 1:
                    raise
                await asyncio.sleep(
                    _CREATE_MEMORY_LOCK_RETRY_BASE_DELAY_SEC * float(attempt + 1)
                )
                continue

        if last_error is not None and self._is_sqlite_lock_error(last_error):
            raise last_error

        raise ValueError(
            f"Path creation for '{domain}://{parent_path or ''}' conflicted; retry the request."
        ) from last_error

    async def _get_next_numeric_id(
        self, session: AsyncSession, parent_path: str, domain: str = "core"
    ) -> int:
        """Get the next numeric ID for auto-naming under a parent path in a domain."""
        prefix = f"{parent_path}/" if parent_path else ""

        # Prepare LIKE clause with escaping if parent_path exists
        if parent_path:
            safe_parent = (
                parent_path.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            like_pattern = f"{safe_parent}/%"
            like_clause = Path.path.like(like_pattern, escape="\\")
        else:
            like_clause = Path.path.like("%")

        result = await session.execute(
            select(Path.path).where(Path.domain == domain).where(like_clause)
        )

        max_num = 0
        for (path,) in result.all():
            remainder = path[len(prefix) :] if prefix else path
            # Only consider direct children
            if "/" not in remainder:
                try:
                    num = int(remainder)
                    max_num = max(max_num, num)
                except ValueError:
                    pass

        return max_num + 1

    async def _reserve_next_numeric_id(
        self, session: AsyncSession, parent_path: str, domain: str = "core"
    ) -> int:
        """Reserve the next numeric child path atomically for a parent."""
        counter_parent_path = str(parent_path or "").strip().strip("/")
        seed_value = max(
            1,
            int(await self._get_next_numeric_id(session, counter_parent_path, domain)),
        )
        counter_table = AutoPathCounter.__table__
        now_value = _utc_now_naive()
        statement = (
            sqlite_insert(counter_table)
            .values(
                domain=domain,
                parent_path=counter_parent_path,
                next_id=seed_value + 1,
                updated_at=now_value,
            )
            .on_conflict_do_update(
                index_elements=[
                    counter_table.c.domain,
                    counter_table.c.parent_path,
                ],
                set_={
                    "next_id": func.max(counter_table.c.next_id, seed_value) + 1,
                    "updated_at": now_value,
                },
            )
            .returning((counter_table.c.next_id - 1).label("reserved_id"))
        )
        reserved_id = await session.scalar(statement)
        if reserved_id is None:
            raise RuntimeError(
                f"Failed to reserve an auto path id for '{domain}://{counter_parent_path}'."
            )
        return int(reserved_id)

    async def _advance_auto_path_counter_floor(
        self,
        session: AsyncSession,
        *,
        parent_path: str,
        domain: str = "core",
        floor_value: int,
    ) -> None:
        """Keep auto-id allocation monotonic when numeric titles are inserted explicitly."""
        if int(floor_value) <= 0:
            return

        counter_parent_path = str(parent_path or "").strip().strip("/")
        counter_table = AutoPathCounter.__table__
        now_value = _utc_now_naive()
        statement = sqlite_insert(counter_table).values(
            domain=domain,
            parent_path=counter_parent_path,
            next_id=int(floor_value),
            updated_at=now_value,
        )
        statement = statement.on_conflict_do_update(
            index_elements=[
                counter_table.c.domain,
                counter_table.c.parent_path,
            ],
            set_={
                "next_id": func.max(counter_table.c.next_id, int(floor_value)),
                "updated_at": now_value,
            },
        )
        await session.execute(statement)

    # =========================================================================
    # Update Operations
    # =========================================================================

    async def update_memory(
        self,
        path: str,
        content: Optional[str] = None,
        priority: Optional[int] = None,
        disclosure: Optional[str] = None,
        domain: str = "core",
        index_now: bool = True,
        expected_old_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Update a memory (creates new version, deprecates old, repoints path).

        Args:
            path: Path to update
            content: New content (None = keep old)
            priority: New priority (None = keep old)
            disclosure: New disclosure (None = keep old)
            domain: The domain/namespace (e.g., "core", "writer", "game")
            expected_old_id: If set, the caller asserts that the current memory
                id for this path is *expected_old_id*.  When the actual id
                differs (because another process updated the path between the
                caller's read and this write), a ValueError is raised instead
                of silently overwriting.  This prevents stale-read覆盖.

        Returns:
            Updated memory info including old and new memory IDs
        """
        if content is None and priority is None and disclosure is None:
            raise ValueError(
                f"No update fields provided for '{domain}://{path}'. "
                "At least one of content, priority, or disclosure must be set."
            )
        domain = self._normalize_writable_domain(domain)
        if content is not None:
            self._validate_memory_content(content)
        if priority is not None:
            self._validate_priority_value(priority)

        async with self.session() as session:
            # 1. Get current memory and path
            result = await session.execute(
                select(Memory, Path)
                .join(Path, Memory.id == Path.memory_id)
                .where(Path.domain == domain)
                .where(Path.path == path)
                .where(Memory.deprecated == False)
            )
            row = result.first()

            if not row:
                raise ValueError(
                    f"Path '{domain}://{path}' not found or memory is deprecated"
                )

            old_memory, path_obj = row
            old_id = old_memory.id

            # Stale-read guard: when content is being updated (= new version
            # created), the caller MUST supply expected_old_id so that we
            # can detect stale-read overwrites.  Metadata-only updates
            # (priority/disclosure) are idempotent and do not need the check.
            if content is not None and expected_old_id is None:
                raise ValueError(
                    f"expected_old_id is required when updating content for "
                    f"'{domain}://{path}'. Pass the memory id that the caller "
                    "read before computing the update."
                )
            if expected_old_id is not None and old_id != expected_old_id:
                raise ValueError(
                    f"Concurrent modification detected for '{domain}://{path}': "
                    f"expected memory id={expected_old_id} but current is "
                    f"id={old_id}. Another process updated this path. "
                    "Retry the operation."
                )

            # Update Path Metadata
            if priority is not None:
                path_obj.priority = priority
            if disclosure is not None:
                path_obj.disclosure = disclosure

            new_memory_id = old_id
            index_targets: List[int] = []

            if content is not None:
                # Content update requested: ALWAYS create a new version.
                #
                # Previously this checked `content != old_memory.content` and
                # silently skipped when content was identical.  This caused a
                # TOCTOU bug: the MCP layer reads content in session A, computes
                # the replacement, then passes it here (session B).  If the DB
                # content was already updated between the two reads (or if the
                # MCP transport subtly normalised whitespace), the equality
                # check would pass, no new version was created, yet "Success"
                # was returned to the caller.
                #
                # The MCP layer is responsible for validating the change; the
                # DB layer should unconditionally persist whatever it receives.
                new_memory = Memory(content=content)
                session.add(new_memory)
                await session.flush()
                new_memory_id = new_memory.id

                # Mark old as deprecated and set migration pointer to new version.
                # The WHERE deprecated=False acts as a CAS guard: if another
                # process already deprecated this memory (concurrent update),
                # rowcount will be 0 and we raise instead of silently
                # overwriting the other writer's result.
                cas_result = await session.execute(
                    update(Memory)
                    .where(Memory.id == old_id)
                    .where(Memory.deprecated == False)
                    .values(deprecated=True, migrated_to=new_memory.id)
                )
                if cas_result.rowcount == 0:
                    raise ValueError(
                        f"Concurrent modification detected for '{domain}://{path}': "
                        f"memory id={old_id} was already updated by another process. "
                        "Retry the operation."
                    )

                # Repoint ALL paths pointing to the old memory to the new memory
                # This ensures aliases stay in sync with the content update
                await session.execute(
                    update(Path)
                    .where(Path.memory_id == old_id)
                    .values(memory_id=new_memory.id)
                )

                await self._clear_memory_index(session, old_id)
                index_targets = [new_memory.id]
                if index_now:
                    await self._reindex_memory(session, new_memory.id)

            if content is None:
                # Only metadata changed, explicitly add the path object for flush
                session.add(path_obj)

            return {
                "domain": domain,
                "path": path,
                "uri": f"{domain}://{path}",
                "old_memory_id": old_id,
                "new_memory_id": new_memory_id,
                "index_pending": bool(index_targets) and not index_now,
                "index_targets": index_targets,
            }

    async def rollback_to_memory(
        self,
        path: str,
        target_memory_id: int,
        domain: str = "core",
        index_now: bool = True,
        restore_path_metadata: bool = False,
        restore_priority: Optional[int] = None,
        restore_disclosure: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Rollback a path to point to a specific memory version.

        Args:
            path: Path to rollback
            target_memory_id: Memory ID to restore to
            domain: The domain/namespace (e.g., "core", "writer", "game")

        Returns:
            Rollback result info
        """
        async with self.session() as session:
            # 1. Get current memory_id
            result = await session.execute(
                select(Path.memory_id, Path)
                .where(Path.domain == domain)
                .where(Path.path == path)
            )
            row = result.first()
            current_id = row[0] if row else None
            path_obj = row[1] if row else None

            if current_id is None or path_obj is None:
                raise ValueError(f"Path '{domain}://{path}' not found")

            # 2. Verify target memory exists
            target = await session.execute(
                select(Memory).where(Memory.id == target_memory_id)
            )
            if not target.scalar_one_or_none():
                raise ValueError(f"Target memory ID {target_memory_id} not found")

            # 3. Mark current as deprecated and point to restored version
            await session.execute(
                update(Memory)
                .where(Memory.id == current_id)
                .values(deprecated=True, migrated_to=target_memory_id)
            )

            # 4. Un-deprecate target and clear its migration pointer (it's the active version now)
            await session.execute(
                update(Memory)
                .where(Memory.id == target_memory_id)
                .values(deprecated=False, migrated_to=None)
            )

            affected_paths = [
                (str(row[0]), str(row[1]))
                for row in (
                    await session.execute(
                        select(Path.domain, Path.path).where(Path.memory_id == current_id)
                    )
                ).all()
            ]

            # 5. Repoint ALL paths that were pointing to the old memory
            await session.execute(
                update(Path)
                .where(Path.memory_id == current_id)
                .values(memory_id=target_memory_id)
            )

            if restore_path_metadata and affected_paths:
                update_values: Dict[str, Any] = {"disclosure": restore_disclosure}
                if restore_priority is not None:
                    update_values["priority"] = restore_priority
                await session.execute(
                    update(Path)
                    .where(tuple_(Path.domain, Path.path).in_(affected_paths))
                    .values(**update_values)
                )

            await self._clear_memory_index(session, current_id)
            if index_now:
                await self._reindex_memory(session, target_memory_id)

            return {
                "domain": domain,
                "path": path,
                "uri": f"{domain}://{path}",
                "old_memory_id": current_id,
                "restored_memory_id": target_memory_id,
                "index_pending": not index_now,
                "index_targets": [target_memory_id],
            }

    async def restore_path_metadata(
        self,
        path: str,
        *,
        priority: int,
        disclosure: Optional[str],
        domain: str = "core",
    ) -> Dict[str, Any]:
        self._validate_priority_value(priority)
        async with self.session() as session:
            result = await session.execute(
                select(Path).where(Path.domain == domain).where(Path.path == path)
            )
            path_obj = result.scalar_one_or_none()
            if path_obj is None:
                raise ValueError(f"Path '{domain}://{path}' not found")

            path_obj.priority = priority
            path_obj.disclosure = disclosure
            session.add(path_obj)

            return {
                "domain": domain,
                "path": path,
                "uri": f"{domain}://{path}",
                "priority": path_obj.priority,
                "disclosure": path_obj.disclosure,
            }

    async def reindex_memory(
        self, memory_id: int, reason: str = "manual"
    ) -> Dict[str, Any]:
        """Rebuild retrieval index rows for one memory."""
        if int(memory_id) <= 0:
            raise ValueError("memory_id must be a positive integer.")

        target_id = int(memory_id)
        indexed_chunks = 0
        exists = False
        deprecated = False
        now_iso = _utc_now_naive().isoformat()

        async with self.session() as session:
            memory_result = await session.execute(
                select(Memory).where(Memory.id == target_id)
            )
            memory = memory_result.scalar_one_or_none()
            exists = memory is not None
            deprecated = bool(memory.deprecated) if memory else False

            indexed_chunks = await self._reindex_memory(session, target_id)
            await self._set_index_meta(session, "last_reindex_reason", reason or "manual")
            await self._set_index_meta(session, "last_reindex_request_memory_id", str(target_id))
            await self._set_index_meta(session, "last_reindex_request_at", now_iso)

        return {
            "memory_id": target_id,
            "indexed_chunks": indexed_chunks,
            "exists": exists,
            "deprecated": deprecated,
            "indexed_at": now_iso,
            "reason": reason or "manual",
        }

    async def rebuild_index(
        self, include_deprecated: bool = False, reason: str = "manual"
    ) -> Dict[str, Any]:
        """Rebuild retrieval index rows for all selected memories."""
        async with self.session() as session:
            query = select(Memory.id).order_by(Memory.id.asc())
            if not include_deprecated:
                query = query.where(Memory.deprecated == False)
            rows = await session.execute(query)
            memory_ids = [int(memory_id) for (memory_id,) in rows.all()]
            await self._clear_all_retrieval_indexes(session)

        total_chunks = 0
        failure_items: List[Dict[str, Any]] = []
        for target_id in memory_ids:
            try:
                item = await self.reindex_memory(
                    memory_id=target_id,
                    reason=f"rebuild:{reason or 'manual'}",
                )
                total_chunks += int(item.get("indexed_chunks", 0) or 0)
            except Exception as exc:
                failure_items.append({"memory_id": target_id, "error": str(exc)})

        finished_at = _utc_now_naive().isoformat()
        async with self.session() as session:
            await self._set_index_meta(session, "last_rebuild_at", finished_at)
            await self._set_index_meta(session, "last_rebuild_reason", reason or "manual")
            await self._set_index_meta(session, "last_rebuild_memories", str(len(memory_ids)))
            await self._set_index_meta(session, "last_rebuild_chunks", str(total_chunks))
            await self._set_index_meta(session, "last_rebuild_failures", str(len(failure_items)))

        return {
            "requested_memories": len(memory_ids),
            "indexed_chunks": total_chunks,
            "failure_count": len(failure_items),
            "failures": failure_items,
            "include_deprecated": bool(include_deprecated),
            "reason": reason or "manual",
            "finished_at": finished_at,
        }

    # =========================================================================
    # Path Operations
    # =========================================================================

    async def add_path(
        self,
        new_path: str,
        target_path: str,
        new_domain: str = "core",
        target_domain: str = "core",
        priority: int = 0,
        disclosure: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Create an alias path pointing to the same memory as target_path.

        Args:
            new_path: New path to create
            target_path: Existing path to alias
            new_domain: Domain for the new path
            target_domain: Domain of the target path
            priority: Priority for this new alias
            disclosure: Disclosure trigger for this new alias

        Returns:
            Created alias info
        """
        new_domain = self._normalize_writable_domain(new_domain)
        target_domain = self._normalize_writable_domain(target_domain)
        self._validate_priority_value(priority)
        self._validate_memory_path(new_path, field_name="new_path")
        self._validate_memory_path(target_path, field_name="target_path")
        max_attempts = 10
        for attempt in range(max_attempts):
            try:
                async with self.session() as session:
                    # Get target memory_id
                    result = await session.execute(
                        select(Path.memory_id)
                        .where(Path.domain == target_domain)
                        .where(Path.path == target_path)
                    )
                    target_id = result.scalar_one_or_none()

                    if target_id is None:
                        raise ValueError(
                            f"Target path '{target_domain}://{target_path}' not found"
                        )

                    # Validate parent of new_path exists
                    if "/" in new_path:
                        parent_path = new_path.rsplit("/", 1)[0]
                        parent_exists = await session.execute(
                            select(Path)
                            .where(Path.domain == new_domain)
                            .where(Path.path == parent_path)
                        )
                        if not parent_exists.scalar_one_or_none():
                            raise ValueError(
                                f"Parent '{new_domain}://{parent_path}' does not exist. "
                                f"Create the parent first, or use a shallower alias path."
                            )

                    # Check if new path exists in the new domain
                    existing = await session.execute(
                        select(Path)
                        .where(Path.domain == new_domain)
                        .where(Path.path == new_path)
                    )
                    if existing.scalar_one_or_none():
                        raise ValueError(f"Path '{new_domain}://{new_path}' already exists")

                    # Create alias
                    path_obj = Path(
                        domain=new_domain,
                        path=new_path,
                        memory_id=target_id,
                        priority=priority,
                        disclosure=disclosure,
                    )
                    session.add(path_obj)

                    return {
                        "new_uri": f"{new_domain}://{new_path}",
                        "target_uri": f"{target_domain}://{target_path}",
                        "memory_id": target_id,
                    }
            except (sqlite3.OperationalError, SQLAlchemyOperationalError) as exc:
                if not self._is_sqlite_lock_error(exc) or attempt >= max_attempts - 1:
                    raise
                await asyncio.sleep(0.05 * float(attempt + 1))

    async def remove_path(self, path: str, domain: str = "core") -> Dict[str, Any]:
        """
        Remove a path (but not the memory it points to).

        Refuses to delete a path that still has children. The caller must
        delete all child paths first before removing the parent.

        Args:
            path: Path to remove
            domain: The domain/namespace (e.g., "core", "writer", "game")

        Returns:
            Removal info

        Raises:
            ValueError: If the path has children or does not exist
        """
        domain = self._normalize_writable_domain(domain)
        async with self.session() as session:
            result = await session.execute(
                select(Path).where(Path.domain == domain).where(Path.path == path)
            )
            path_obj = result.scalar_one_or_none()

            if not path_obj:
                raise ValueError(f"Path '{domain}://{path}' not found")

            # Block deletion if child paths exist
            safe_path = (
                path.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            child_prefix = f"{safe_path}/"
            child_result = await session.execute(
                select(func.count())
                .select_from(Path)
                .where(Path.domain == domain)
                .where(Path.path.like(f"{child_prefix}%", escape="\\"))
            )
            child_count = child_result.scalar()

            if child_count > 0:
                # Fetch up to 5 child URIs for a helpful error message
                sample_result = await session.execute(
                    select(Path.path)
                    .where(Path.domain == domain)
                    .where(Path.path.like(f"{child_prefix}%", escape="\\"))
                    .order_by(Path.path)
                    .limit(5)
                )
                sample_paths = [
                    f"{domain}://{row[0]}" for row in sample_result.all()
                ]
                listing = ", ".join(sample_paths)
                suffix = f" (and {child_count - 5} more)" if child_count > 5 else ""
                raise ValueError(
                    f"Cannot delete '{domain}://{path}': "
                    f"it still has {child_count} child path(s). "
                    f"Delete children first: {listing}{suffix}"
                )

            memory_id = path_obj.memory_id
            await session.delete(path_obj)

            return {"removed_uri": f"{domain}://{path}", "memory_id": memory_id}

    async def restore_path(
        self,
        path: str,
        domain: str,
        memory_id: int,
        priority: int = 0,
        disclosure: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Restore a path pointing to a specific memory ID (used for rollback).

        Args:
            path: Path to restore
            domain: Domain
            memory_id: Memory ID to point to
            priority: Path priority
            disclosure: Path disclosure

        Returns:
            Restored path info
        """
        domain = self._normalize_writable_domain(domain)
        safe_path = (path or "").strip("/")
        if not safe_path:
            raise ValueError("Path cannot be empty")
        self._validate_memory_path(safe_path)

        async with self.session() as session:
            # Check if memory exists
            memory_result = await session.execute(
                select(Memory).where(Memory.id == memory_id)
            )
            if not memory_result.scalar_one_or_none():
                raise ValueError(f"Memory ID {memory_id} not found")

            if "/" in safe_path:
                parent_path = safe_path.rsplit("/", 1)[0]
                parent_result = await session.execute(
                    select(Path.path)
                    .where(Path.domain == domain)
                    .where(Path.path == parent_path)
                )
                if parent_result.scalar_one_or_none() is None:
                    raise ValueError(
                        f"Parent path '{domain}://{parent_path}' not found"
                    )

            # Ensure memory is not deprecated (un-deprecate if needed)
            # This is critical for rollback: if we restore a path to a memory that was
            # deprecated (e.g. by a subsequent update), we must make it visible again.
            await session.execute(
                update(Memory)
                .where(Memory.id == memory_id)
                .values(deprecated=False, migrated_to=None)
            )

            # Check if path already exists (collision)
            existing = await session.execute(
                select(Path).where(Path.domain == domain).where(Path.path == safe_path)
            )
            if existing.scalar_one_or_none():
                raise ValueError(f"Path '{domain}://{safe_path}' already exists")

            # Create path
            path_obj = Path(
                domain=domain,
                path=safe_path,
                memory_id=memory_id,
                priority=priority,
                disclosure=disclosure,
            )
            session.add(path_obj)
            await self._reindex_memory(session, memory_id)

            return {"uri": f"{domain}://{safe_path}", "memory_id": memory_id}

    # =========================================================================
    # Search Operations
    # =========================================================================

    @staticmethod
    def _escape_like_pattern(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @classmethod
    def _contains_like_pattern(cls, value: str) -> str:
        return f"%{cls._escape_like_pattern(value)}%"

    @classmethod
    def _collect_literal_search_tokens(
        cls,
        query: str,
        *,
        include_cjk: bool,
    ) -> List[str]:
        pattern = (
            _SEARCH_LITERAL_TOKEN_PATTERN if include_cjk else _SEARCH_ASCII_TOKEN_PATTERN
        )
        raw_tokens = pattern.findall(str(query or "").strip())
        if not raw_tokens:
            return []

        has_non_operator = any(
            token.lower() not in _FTS_RESERVED_TOKENS for token in raw_tokens
        )
        tokens: List[str] = []
        seen: set[str] = set()
        for token in raw_tokens:
            lowered = token.lower()
            if has_non_operator and lowered in _FTS_RESERVED_TOKENS:
                continue
            if lowered in seen:
                continue
            seen.add(lowered)
            tokens.append(token)
        return tokens[:8]

    @classmethod
    def _build_safe_fts_query(cls, query: str) -> Optional[str]:
        tokens = cls._collect_literal_search_tokens(query, include_cjk=False)
        if not tokens:
            return None
        return " ".join(f'"{token}"' for token in tokens)

    @classmethod
    def _build_like_fallback_terms(cls, query: str) -> List[str]:
        raw_query = str(query or "").strip()
        terms: List[str] = []
        seen: set[str] = set()

        def _push(value: str) -> None:
            candidate = str(value or "").strip()
            if not candidate:
                return
            folded = cls._unicode_search_fold(candidate)
            if not folded or folded in seen:
                return
            seen.add(folded)
            terms.append(candidate)

        _push(raw_query)
        if any(char.isspace() for char in raw_query):
            for token in cls._collect_literal_search_tokens(
                raw_query, include_cjk=True
            ):
                _push(token)
        return terms or ([raw_query] if raw_query else [])

    @classmethod
    def _casefold_contains(cls, haystack: str, needle: str) -> bool:
        if not needle:
            return False
        return cls._unicode_search_fold(needle) in cls._unicode_search_fold(haystack)

    @staticmethod
    def _iter_exception_messages(exc: Exception) -> List[str]:
        pending: List[BaseException] = [exc]
        messages: List[str] = []
        seen_ids: set[int] = set()

        while pending:
            current = pending.pop()
            current_id = id(current)
            if current_id in seen_ids:
                continue
            seen_ids.add(current_id)

            try:
                message = str(current).strip().lower()
            except Exception:
                message = current.__class__.__name__.strip().lower()
            if message:
                messages.append(message)

            for nested in (
                getattr(current, "orig", None),
                getattr(current, "__cause__", None),
                getattr(current, "__context__", None),
            ):
                if isinstance(nested, BaseException):
                    pending.append(nested)

        return messages

    @classmethod
    def _should_disable_fts_after_error(cls, exc: Exception) -> bool:
        messages = cls._iter_exception_messages(exc)
        if any(
            "database is locked" in message or "database table is locked" in message
            for message in messages
        ):
            return False
        if any(
            marker in message
            for message in messages
            for marker in (
                "database schema is locked",
                "timeout",
                "temporarily unavailable",
                "interrupted",
                "fts5: syntax error",
                "malformed match expression",
                "unterminated string",
                "no such column:",
            )
        ):
            return False
        return True

    async def _handle_fts_runtime_error(
        self,
        session: AsyncSession,
        exc: Exception,
        *,
        context: str,
    ) -> None:
        if self._should_disable_fts_after_error(exc):
            self._fts_available = False
            await self._set_index_meta(session, "fts_available", "0")
            logger.warning("Disabling FTS after %s failure: %s", context, exc)
            return
        logger.warning(
            "FTS %s failed; falling back without disabling FTS: %s",
            context,
            exc,
        )

    async def _handle_gist_fts_runtime_error(
        self,
        session: AsyncSession,
        exc: Exception,
        *,
        context: str,
    ) -> None:
        if self._should_disable_fts_after_error(exc):
            self._gist_fts_available = False
            await self._set_index_meta(session, "gist_fts_available", "0")
            logger.warning("Disabling gist FTS after %s failure: %s", context, exc)
            return
        logger.warning(
            "Gist FTS %s failed; falling back without disabling gist FTS: %s",
            context,
            exc,
        )

    @staticmethod
    def _make_snippet(text_content: str, query: str, around: int = 50) -> str:
        if not text_content:
            return ""
        if not query:
            return text_content[:120] + ("..." if len(text_content) > 120 else "")

        text_lower = text_content.lower()
        query_lower = query.lower()
        pos = text_lower.find(query_lower)
        if pos < 0:
            return text_content[:120] + ("..." if len(text_content) > 120 else "")

        start = max(0, pos - around)
        end = min(len(text_content), pos + len(query) + around)
        prefix = "..." if start > 0 else ""
        suffix = "..." if end < len(text_content) else ""
        return f"{prefix}{text_content[start:end]}{suffix}"

    @staticmethod
    def _like_text_score(query: str, chunk_text: str, path: str) -> float:
        if not query:
            return 0.0
        score = 0.0
        if SQLiteClient._casefold_contains(chunk_text or "", query):
            score += 0.7
        if SQLiteClient._casefold_contains(path or "", query):
            score += 0.3
        return min(score, 1.0)

    @classmethod
    def _like_match_statistics(
        cls, query: str, chunk_text: str, path: str
    ) -> Dict[str, float | int | bool]:
        raw_query = str(query or "").strip()
        if not raw_query:
            return {
                "full_query_match": False,
                "matched_terms": 0,
                "total_terms": 0,
                "min_required_terms": 0,
                "relevant": False,
                "score": 0.0,
            }

        full_query_match = bool(
            cls._casefold_contains(chunk_text or "", raw_query)
            or cls._casefold_contains(path or "", raw_query)
        )
        terms = [
            term
            for term in cls._collect_literal_search_tokens(raw_query, include_cjk=True)
            if cls._unicode_search_fold(term) not in {"and", "or", "not"}
        ]
        if not terms:
            terms = [raw_query]
        matched_terms = sum(
            1
            for term in terms
            if cls._casefold_contains(chunk_text or "", term)
            or cls._casefold_contains(path or "", term)
        )
        total_terms = len(terms)
        min_required_terms = (
            1
            if total_terms <= 1
            else max(2, math.ceil(total_terms * 0.75))
        )
        relevant = full_query_match or matched_terms >= min_required_terms
        coverage = (matched_terms / total_terms) if total_terms else 0.0
        score = max(cls._like_text_score(raw_query, chunk_text, path), coverage)
        return {
            "full_query_match": full_query_match,
            "matched_terms": matched_terms,
            "total_terms": total_terms,
            "min_required_terms": min_required_terms,
            "relevant": relevant,
            "score": min(float(score), 1.0),
        }

    @staticmethod
    def _normalize_write_guard_query(query: str) -> str:
        raw_query = str(query or "").strip()
        if not raw_query:
            return ""
        if not raw_query.startswith("# Auto Captured Memory"):
            return raw_query
        if "- category:" not in raw_query or "- captured_at:" not in raw_query:
            return raw_query
        if "## Content" not in raw_query:
            return raw_query

        _, _, content_block = raw_query.partition("## Content")
        if not content_block:
            return raw_query

        normalized = re.sub(
            r"<!-- MEMORY_PALACE_FORCE_CONTROL_V1 -->[\s\S]*?<!-- /MEMORY_PALACE_FORCE_CONTROL_V1 -->",
            "",
            content_block,
        ).strip()
        return normalized or raw_query

    async def _is_exact_structured_write_guard_duplicate(
        self,
        *,
        normalized_query: str,
        candidate: Optional[Dict[str, Any]],
    ) -> bool:
        if not normalized_query or not isinstance(candidate, dict):
            return False
        memory_id = candidate.get("memory_id")
        if not isinstance(memory_id, int) or memory_id <= 0:
            return False

        async with self.session() as session:
            result = await session.execute(
                select(Memory.content).where(Memory.id == memory_id)
            )
            existing_content = result.scalar_one_or_none()

        if not isinstance(existing_content, str) or not existing_content.strip():
            return False
        return self._normalize_write_guard_query(existing_content) == normalized_query

    @staticmethod
    def _normalize_guard_action(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        action = value.strip().upper()
        if action in {"ADD", "UPDATE", "NOOP", "DELETE"}:
            return action
        return None

    @staticmethod
    def _extract_chat_message_text(payload: Dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            return ""
        message = first_choice.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text_content = item.get("text")
                if isinstance(text_content, str) and text_content.strip():
                    parts.append(text_content.strip())
            return "\n".join(parts).strip()
        return ""

    @staticmethod
    def _extract_response_output_text(payload: Dict[str, Any]) -> str:
        output = payload.get("output")
        if not isinstance(output, list):
            return ""
        parts: List[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if not isinstance(content_item, dict):
                    continue
                text_value = content_item.get("text")
                if isinstance(text_value, str) and text_value.strip():
                    parts.append(text_value.strip())
                    continue
                if content_item.get("type") == "output_text":
                    output_text = content_item.get("text")
                    if isinstance(output_text, str) and output_text.strip():
                        parts.append(output_text.strip())
        return "\n".join(parts).strip()

    @classmethod
    def _extract_llm_response_text(cls, payload: Dict[str, Any]) -> str:
        chat_text = cls._extract_chat_message_text(payload)
        if chat_text:
            return chat_text
        return cls._extract_response_output_text(payload)

    @staticmethod
    def _parse_chat_json_object(raw_text: str) -> Optional[Dict[str, Any]]:
        candidate = (raw_text or "").strip()
        if not candidate:
            return None

        parse_candidates = [candidate]
        if candidate.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
            stripped = re.sub(r"\s*```$", "", stripped)
            parse_candidates.append(stripped.strip())

        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            parse_candidates.append(candidate[start : end + 1])

        for item in parse_candidates:
            try:
                parsed = json.loads(item)
            except (TypeError, ValueError):
                # Real-world model outputs may be JSON-like (e.g. unquoted keys).
                # Try a conservative normalization before giving up.
                normalized = item
                normalized = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_\-]*)(\s*:)", r'\1"\2"\3', normalized)
                normalized = re.sub(r",\s*([}\]])", r"\1", normalized)
                if "'" in normalized and '"' not in normalized:
                    normalized = normalized.replace("'", '"')
                if normalized != item:
                    try:
                        parsed = json.loads(normalized)
                    except (TypeError, ValueError):
                        continue
                else:
                    continue
            if isinstance(parsed, dict):
                return parsed
        return None

    async def generate_compact_gist(
        self,
        *,
        summary: str,
        max_points: int = 3,
        max_chars: int = 280,
        degrade_reasons: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        source = (summary or "").strip()
        if not source:
            return None

        llm_enabled = self._env_bool(
            "COMPACT_GIST_LLM_ENABLED",
            self._env_bool("WRITE_GUARD_LLM_ENABLED", False),
        )
        if not llm_enabled:
            self._append_degrade_reason(degrade_reasons, "compact_gist_llm_disabled")
            return None

        llm_api_base = self._first_env(
            [
                "COMPACT_GIST_LLM_API_BASE",
                "WRITE_GUARD_LLM_API_BASE",
                "LLM_RESPONSES_URL",
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
                "ROUTER_API_BASE",
            ]
        )
        llm_api_base = self._normalize_chat_api_base(llm_api_base)
        llm_api_key = self._first_env(
            [
                "COMPACT_GIST_LLM_API_KEY",
                "WRITE_GUARD_LLM_API_KEY",
                "LLM_API_KEY",
                "OPENAI_API_KEY",
                "ROUTER_API_KEY",
            ]
        )
        llm_model = self._first_env(
            [
                "COMPACT_GIST_LLM_MODEL",
                "WRITE_GUARD_LLM_MODEL",
                "LLM_MODEL_NAME",
                "OPENAI_MODEL",
                "ROUTER_CHAT_MODEL",
            ]
        )
        if not llm_api_base or not llm_model:
            self._append_degrade_reason(degrade_reasons, "compact_gist_llm_config_missing")
            return None

        try:
            bounded_points = max(1, int(max_points))
        except (TypeError, ValueError):
            bounded_points = 3
        try:
            bounded_chars = max(80, int(max_chars))
        except (TypeError, ValueError):
            bounded_chars = 280

        # Length-aware prompt: short inputs get conservative constraints
        source_len = len(source)
        if source_len <= 300:
            fidelity_hint = (
                " When the source is short, preserve its original wording — "
                "do not rephrase for style if compression gain is negligible. "
                "Do not strengthen or weaken qualifiers (e.g. keep 'some progress' as-is)."
            )
        else:
            fidelity_hint = (
                " Compress aggressively but do not infer unstated facts. "
                "Preserve uncertainty markers and stated constraints."
            )

        payload = {
            "model": llm_model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a compact_context semantic gist generator. "
                        "Return strict JSON only with keys: gist_text, quality. "
                        "quality must be a float in [0,1]."
                        + fidelity_hint
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Summarize the following session trace into at most {bounded_points} "
                        f"high-signal points and <= {bounded_chars} chars.\n\n"
                        f"{source}"
                    ),
                },
            ],
        }

        gist_timeout = max(
            self._remote_http_timeout_sec,
            self._env_float("COMPACT_GIST_TIMEOUT_SEC", 45.0),
        )
        response = await self._post_json(
            llm_api_base,
            "/chat/completions",
            payload,
            llm_api_key,
            timeout_sec=gist_timeout,
        )
        if response is None:
            self._append_degrade_reason(degrade_reasons, "compact_gist_llm_request_failed")
            return None

        message_text = self._extract_chat_message_text(response)
        if not message_text:
            self._append_degrade_reason(degrade_reasons, "compact_gist_llm_response_empty")
            return None

        parsed = self._parse_chat_json_object(message_text)
        if parsed is None:
            self._append_degrade_reason(degrade_reasons, "compact_gist_llm_response_invalid")
            return None

        gist_text = str(parsed.get("gist_text") or "").strip()
        if not gist_text:
            self._append_degrade_reason(degrade_reasons, "compact_gist_llm_gist_missing")
            return None
        if len(gist_text) > bounded_chars:
            gist_text = gist_text[: max(24, bounded_chars - 3)].rstrip() + "..."

        quality_value = parsed.get("quality")
        try:
            quality = float(quality_value)
        except (TypeError, ValueError):
            quality = 0.72
        quality = max(0.0, min(1.0, quality))

        return {
            "gist_text": gist_text,
            "gist_method": "llm_gist",
            "quality": round(quality, 3),
        }

    @staticmethod
    def _is_structured_namespace_candidate(snippet: str) -> bool:
        normalized = str(snippet or "").strip()
        if not normalized:
            return False
        return bool(
            re.search(r"#\s*Memory Palace Namespace\b", normalized, re.IGNORECASE)
            or re.search(r"\bnamespace_uri\s*:", normalized, re.IGNORECASE)
            or re.search(
                r"Container node for (reflection|capture|profile) records\.",
                normalized,
                re.IGNORECASE,
            )
            or re.search(r"#\s*Visual Namespace Container\b", normalized, re.IGNORECASE)
            or re.search(r"\bvisual_namespace_container\s*:\s*true\b", normalized, re.IGNORECASE)
            or re.search(r"\bKind:\s*internal namespace container\b", normalized, re.IGNORECASE)
        )

    @staticmethod
    def _collect_guard_candidates(
        payload: Dict[str, Any],
        *,
        exclude_memory_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        rows = payload.get("results")
        if not isinstance(rows, list):
            return []

        by_memory_id: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            memory_id = row.get("memory_id")
            if not isinstance(memory_id, int) or memory_id <= 0:
                continue
            if exclude_memory_id is not None and memory_id == exclude_memory_id:
                continue

            scores = row.get("scores")
            if not isinstance(scores, dict):
                scores = {}

            snippet = str(row.get("snippet") or "")
            if SQLiteClient._is_structured_namespace_candidate(snippet):
                continue

            candidate = {
                "memory_id": memory_id,
                "uri": str(row.get("uri") or ""),
                "snippet": snippet[:220],
                "vector_score": float(scores.get("vector") or 0.0),
                "text_score": float(scores.get("text") or 0.0),
                "final_score": float(scores.get("final") or 0.0),
            }
            existing = by_memory_id.get(memory_id)
            if existing is None or candidate["final_score"] > existing["final_score"]:
                by_memory_id[memory_id] = candidate

        return sorted(
            by_memory_id.values(), key=lambda item: item.get("final_score", 0.0), reverse=True
        )

    @staticmethod
    def _guard_candidate_view(candidate: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not candidate:
            return None
        return {
            "memory_id": candidate.get("memory_id"),
            "uri": candidate.get("uri"),
            "vector_score": round(float(candidate.get("vector_score") or 0.0), 6),
            "text_score": round(float(candidate.get("text_score") or 0.0), 6),
            "final_score": round(float(candidate.get("final_score") or 0.0), 6),
        }

    def _build_guard_decision(
        self,
        *,
        action: str,
        reason: str,
        method: str,
        target_id: Optional[int] = None,
        target_uri: Optional[str] = None,
        degrade_reasons: Optional[List[str]] = None,
        semantic_top: Optional[Dict[str, Any]] = None,
        keyword_top: Optional[Dict[str, Any]] = None,
        extras: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        decision = {
            "action": action,
            "target_id": target_id,
            "target_uri": target_uri,
            "reason": reason,
            "method": method,
            "degraded": bool(degrade_reasons),
            "degrade_reasons": list(degrade_reasons or []),
            "candidates": {
                "semantic_top": self._guard_candidate_view(semantic_top),
                "keyword_top": self._guard_candidate_view(keyword_top),
            },
        }
        if extras:
            decision.update(extras)
        return decision

    async def _write_guard_llm_diff_rescue(
        self,
        *,
        content: str,
        semantic_top: Dict[str, Any],
        degrade_reasons: List[str],
    ) -> Optional[Dict[str, Any]]:
        """LLM content-level diff rescue for hard cases.

        Called when heuristic cross-check wants to return ADD but semantic
        score is in the UPDATE zone. Asks the LLM to compare old vs new
        content and decide if it's truly new (ADD) or an update.

        Returns a guard decision dict if LLM overrides, None otherwise.
        """
        if not self._env_bool("WRITE_GUARD_LLM_DIFF_RESCUE_ENABLED", False):
            return None
        if not self._env_bool("WRITE_GUARD_LLM_ENABLED", False):
            return None

        llm_api_base = self._first_env(
            ["WRITE_GUARD_LLM_API_BASE", "LLM_RESPONSES_URL",
             "OPENAI_BASE_URL", "OPENAI_API_BASE", "ROUTER_API_BASE"]
        )
        llm_api_base = self._normalize_chat_api_base(llm_api_base)
        llm_api_key = self._first_env(
            ["WRITE_GUARD_LLM_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "ROUTER_API_KEY"]
        )
        llm_model = self._first_env(
            ["WRITE_GUARD_LLM_MODEL", "LLM_MODEL_NAME", "OPENAI_MODEL", "ROUTER_CHAT_MODEL"]
        )
        if not llm_api_base or not llm_model:
            return None

        existing_snippet = str(semantic_top.get("snippet") or "")
        target_id = semantic_top.get("memory_id")
        target_uri = semantic_top.get("uri")
        vector_score = float(semantic_top.get("vector_score") or 0.0)

        system_prompt = (
            "You are a write guard for a memory system. "
            "The heuristic scoring is uncertain whether new content is genuinely "
            "new (ADD) or an update to an existing memory (UPDATE). "
            "Compare the existing memory with the new content carefully.\n\n"
            "Return strict JSON with keys: action, reason.\n"
            "action must be one of: ADD, UPDATE.\n\n"
            "Decision rules:\n"
            "- UPDATE if the new content covers the SAME specific topic/subject as the "
            "existing memory AND adds, modifies, corrects, or extends information about it. "
            "Key test: would a human merge these two notes into one?\n"
            "- ADD if the new content is about a DIFFERENT topic, a different project, "
            "a different time period, or introduces genuinely unrelated information, "
            "even if some words or themes overlap.\n"
            "- Be strict about ADD: sharing a general domain (e.g. both about 'work') "
            "is NOT enough for UPDATE. The content must be about the same specific subject."
        )
        user_prompt = (
            f"Existing memory (id={target_id}, uri={target_uri}, "
            f"similarity={vector_score:.3f}):\n"
            f"{existing_snippet}\n\n"
            f"New content:\n{content}\n\n"
            "Are these about the same topic? Is the new content an update/extension "
            "of the existing memory, or genuinely different information?"
        )

        _diff_timeout = max(
            1.0,
            self._env_float("WRITE_GUARD_LLM_DIFF_TIMEOUT_SEC", 45.0),
        )
        # Use streaming to support reasoning models that return content=null
        # in non-streaming mode but emit content deltas in streaming mode.
        message_text = ""
        try:
            url = self._join_api_url(llm_api_base, "/chat/completions")
            _headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
            if llm_api_key:
                _headers["Authorization"] = f"Bearer {llm_api_key}"
            _payload = {
                "model": llm_model, "temperature": 0, "stream": True,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
            client = await self._get_remote_http_client()
            timeout = httpx.Timeout(max(0.1, _diff_timeout))
            async with client.stream(
                "POST", url, json=_payload, headers=_headers, timeout=timeout
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    try:
                        chunk = json.loads(line[6:])
                        delta_content = (
                            chunk.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content")
                        )
                        if isinstance(delta_content, str):
                            message_text += delta_content
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue
        except Exception:
            self._append_degrade_reason(degrade_reasons, "write_guard_llm_diff_failed")
            return None

        if not message_text.strip():
            return None

        parsed = self._parse_chat_json_object(message_text)
        if parsed is None:
            return None

        action = self._normalize_guard_action(parsed.get("action"))
        if action not in {"ADD", "UPDATE"}:
            return None

        reason = str(parsed.get("reason") or "llm_diff_rescue")
        return self._build_guard_decision(
            action=action,
            target_id=target_id if action == "UPDATE" else None,
            target_uri=target_uri if action == "UPDATE" else None,
            reason=f"llm_diff_rescue: {reason}",
            method="llm_diff_rescue",
            degrade_reasons=degrade_reasons,
            semantic_top=semantic_top,
        )

    async def _write_guard_llm_decision(
        self,
        *,
        content: str,
        semantic_candidates: List[Dict[str, Any]],
        keyword_candidates: List[Dict[str, Any]],
        degrade_reasons: List[str],
    ) -> Optional[Dict[str, Any]]:
        if not self._env_bool("WRITE_GUARD_LLM_ENABLED", False):
            self._append_degrade_reason(degrade_reasons, "write_guard_llm_disabled")
            return None

        llm_api_base = self._first_env(
            [
                "WRITE_GUARD_LLM_API_BASE",
                "LLM_RESPONSES_URL",
                "OPENAI_BASE_URL",
                "OPENAI_API_BASE",
                "ROUTER_API_BASE",
            ]
        )
        llm_api_base = self._normalize_chat_api_base(llm_api_base)
        llm_api_key = self._first_env(
            ["WRITE_GUARD_LLM_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "ROUTER_API_KEY"]
        )
        llm_model = self._first_env(
            ["WRITE_GUARD_LLM_MODEL", "LLM_MODEL_NAME", "OPENAI_MODEL", "ROUTER_CHAT_MODEL"]
        )
        if not llm_api_base or not llm_model:
            self._append_degrade_reason(degrade_reasons, "write_guard_llm_config_missing")
            return None

        shortlist: List[Dict[str, Any]] = []
        seen_ids: set[int] = set()
        for item in semantic_candidates + keyword_candidates:
            memory_id = item.get("memory_id")
            if not isinstance(memory_id, int) or memory_id in seen_ids:
                continue
            seen_ids.add(memory_id)
            shortlist.append(item)
            if len(shortlist) >= 5:
                break

        if not shortlist:
            self._append_degrade_reason(degrade_reasons, "write_guard_llm_no_candidates")
            return None

        candidate_lines = []
        for idx, item in enumerate(shortlist, start=1):
            candidate_lines.append(
                f"{idx}. memory_id={item.get('memory_id')} uri={item.get('uri')} "
                f"vector={item.get('vector_score', 0.0):.3f} text={item.get('text_score', 0.0):.3f} "
                f"snippet={item.get('snippet', '')}"
            )

        system_prompt = (
            "You are a write guard for a memory system. "
            "Return strict JSON only with keys: action,target_id,reason,method,contradiction. "
            "Allowed action: ADD,UPDATE,NOOP,DELETE. "
            "contradiction is a boolean: true if the new content REVERSES, DISABLES, "
            "SWITCHES, or CONTRADICTS a fact in a candidate memory (e.g. preference "
            "changes, mode rollbacks, provider switches, value overrides). "
            "If contradiction is true, action should usually be UPDATE with the "
            "target_id of the contradicted memory."
        )
        user_prompt = (
            "New content:\n"
            f"{content}\n\n"
            "Candidate memories:\n"
            f"{chr(10).join(candidate_lines)}\n\n"
            "Decide: does the new content contradict any candidate memory? "
            "If yes, set contradiction=true and action=UPDATE with that memory's target_id. "
            "If it is a duplicate, set action=NOOP. "
            "If it is genuinely new information, set action=ADD and contradiction=false."
        )
        payload = {
            "model": llm_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response = await self._post_json(
            llm_api_base,
            "/chat/completions",
            payload,
            llm_api_key,
        )
        if response is None:
            self._append_degrade_reason(degrade_reasons, "write_guard_llm_request_failed")
            return None

        message_text = self._extract_chat_message_text(response)
        if not message_text:
            self._append_degrade_reason(degrade_reasons, "write_guard_llm_response_empty")
            return None

        parsed = self._parse_chat_json_object(message_text)

        if parsed is None:
            self._append_degrade_reason(degrade_reasons, "write_guard_llm_response_invalid")
            return None

        action = self._normalize_guard_action(parsed.get("action"))
        if action is None:
            self._append_degrade_reason(degrade_reasons, "write_guard_llm_action_invalid")
            return None

        target_id = parsed.get("target_id")
        if not isinstance(target_id, int) or target_id <= 0:
            target_id = None
        reason = str(parsed.get("reason") or "llm_decision")
        method = str(parsed.get("method") or "llm")

        target_uri = None
        if target_id is not None:
            matched = next(
                (item for item in shortlist if item.get("memory_id") == target_id), None
            )
            if matched is not None:
                target_uri = matched.get("uri")

        extras: Dict[str, Any] = {}
        if "contradiction" in parsed:
            extras["contradiction"] = bool(parsed["contradiction"])

        return self._build_guard_decision(
            action=action,
            reason=reason,
            method=method,
            target_id=target_id,
            target_uri=target_uri,
            degrade_reasons=degrade_reasons,
            semantic_top=semantic_candidates[0] if semantic_candidates else None,
            keyword_top=keyword_candidates[0] if keyword_candidates else None,
            extras=extras if extras else None,
        )

    async def _lookup_visual_hash_candidate(
        self,
        session: AsyncSession,
        *,
        visual_hash: str,
        domain: str,
        path_prefix: Optional[str],
        exclude_memory_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        escaped_hash = self._escape_like_pattern(visual_hash)
        query = (
            select(Path.memory_id, Path.domain, Path.path)
            .join(Memory, Memory.id == Path.memory_id)
            .where(Path.domain == domain)
            .where(Memory.deprecated.is_(False))
            .where(Memory.content.like(f"%{escaped_hash}%", escape="\\"))
            .order_by(Path.priority.asc(), Path.memory_id.desc())
            .limit(8)
        )
        if exclude_memory_id is not None:
            query = query.where(Path.memory_id != int(exclude_memory_id))
        normalized_prefix = str(path_prefix or "").strip("/")
        if normalized_prefix:
            escaped_prefix = self._escape_like_pattern(normalized_prefix)
            query = query.where(Path.path.like(f"{escaped_prefix}%", escape="\\"))

        result = await session.execute(query)
        for row in result.all():
            memory_id = row[0]
            path_value = row[2]
            if not isinstance(memory_id, int) or memory_id <= 0:
                continue
            if not isinstance(path_value, str) or not path_value:
                continue
            return {
                "memory_id": memory_id,
                "uri": f"{domain}://{path_value}",
                "visual_hash": visual_hash,
            }
        return None

    async def write_guard(
        self,
        *,
        content: str,
        domain: str = "core",
        path_prefix: Optional[str] = None,
        exclude_memory_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        raw_query = (content or "").strip()
        query = self._normalize_write_guard_query(raw_query)
        if not query:
            return self._build_guard_decision(
                action="NOOP",
                reason="empty_content",
                method="keyword",
            )

        if self._is_visual_namespace_container_content(query):
            return self._build_guard_decision(
                action="ADD",
                reason="visual_namespace_container",
                method="visual_namespace",
            )

        visual_hash = self._extract_visual_guard_hash(query)
        if visual_hash:
            async with self.session() as session:
                visual_candidate = await self._lookup_visual_hash_candidate(
                    session,
                    visual_hash=visual_hash,
                    domain=domain,
                    path_prefix=path_prefix,
                    exclude_memory_id=exclude_memory_id,
                )
            if visual_candidate is not None:
                return self._build_guard_decision(
                    action="UPDATE",
                    target_id=visual_candidate.get("memory_id"),
                    target_uri=visual_candidate.get("uri"),
                    reason=f"visual media hash {visual_hash} already indexed",
                    method="visual_hash",
                )
            return self._build_guard_decision(
                action="ADD",
                reason=f"visual media hash {visual_hash} not found",
                method="visual_hash",
            )

        filters: Dict[str, Any] = {"domain": domain or "core"}
        if isinstance(path_prefix, str) and path_prefix.strip():
            filters["path_prefix"] = path_prefix.strip("/")

        degrade_reasons: List[str] = []

        semantic_payload: Dict[str, Any]
        keyword_payload: Dict[str, Any]
        semantic_unavailable = False
        keyword_unavailable = False
        try:
            semantic_payload = await self.search_advanced(
                query=query,
                mode="semantic",
                max_results=6,
                candidate_multiplier=6,
                filters=filters,
            )
        except Exception as exc:
            semantic_unavailable = True
            self._append_degrade_reason(
                degrade_reasons, f"write_guard_semantic_failed:{type(exc).__name__}"
            )
            semantic_payload = {"results": [], "degrade_reasons": []}
        try:
            keyword_payload = await self.search_advanced(
                query=query,
                mode="keyword",
                max_results=6,
                candidate_multiplier=6,
                filters=filters,
            )
        except Exception as exc:
            keyword_unavailable = True
            self._append_degrade_reason(
                degrade_reasons, f"write_guard_keyword_failed:{type(exc).__name__}"
            )
            keyword_payload = {"results": [], "degrade_reasons": []}

        for payload in (semantic_payload, keyword_payload):
            reasons = payload.get("degrade_reasons")
            if not isinstance(reasons, list):
                continue
            for reason in reasons:
                if isinstance(reason, str):
                    self._append_degrade_reason(degrade_reasons, reason)

        semantic_candidates = self._collect_guard_candidates(
            semantic_payload,
            exclude_memory_id=exclude_memory_id,
        )
        keyword_candidates = self._collect_guard_candidates(
            keyword_payload,
            exclude_memory_id=exclude_memory_id,
        )

        # If both retrieval signals are unavailable, fail closed instead of allowing ADD.
        if semantic_unavailable and keyword_unavailable:
            return self._build_guard_decision(
                action="NOOP",
                reason="write_guard_unavailable",
                method="exception",
                degrade_reasons=degrade_reasons,
            )

        semantic_top = (
            max(
                semantic_candidates,
                key=lambda item: float(item.get("vector_score") or 0.0),
            )
            if semantic_candidates
            else None
        )
        keyword_top_for_cross_check = (
            max(
                keyword_candidates,
                key=lambda item: float(item.get("text_score") or 0.0),
            )
            if keyword_candidates
            else None
        )

        if raw_query != query:
            for candidate in (keyword_top_for_cross_check, semantic_top):
                if await self._is_exact_structured_write_guard_duplicate(
                    normalized_query=query,
                    candidate=candidate,
                ):
                    return self._build_guard_decision(
                        action="NOOP",
                        target_id=candidate.get("memory_id"),
                        target_uri=candidate.get("uri"),
                        reason="normalized structured content matches an existing durable memory",
                        method="structured_body_exact",
                        degrade_reasons=degrade_reasons,
                        semantic_top=semantic_top,
                        keyword_top=keyword_top_for_cross_check,
                    )

        # Score normalization for dense API embeddings (C/D profiles).
        # Dense models like qwen3-embedding compress cosine similarity into
        # a narrow band (~0.85-1.0), making fixed thresholds unreliable.
        # Normalization stretches the effective range so NOOP only fires on
        # true duplicates (~1.0 raw). Expanded cross-check uses keyword
        # absence as ADD signal in the UPDATE zone.
        # Hash embedding (Profile B) has wide score distribution where
        # existing thresholds work well — normalization is automatically
        # disabled for hash backends.
        _is_non_hash_backend = self._embedding_backend not in {"hash", ""}
        _no_hash_fallback = not any(
            "embedding_fallback_hash" in str(r) for r in degrade_reasons
        )
        _norm = (
            self._write_guard_score_normalization
            and _is_non_hash_backend
            and _no_hash_fallback
        )
        if semantic_top is not None:
            vector_score = float(semantic_top.get("vector_score") or 0.0)
            kw_cross_score = (
                float(keyword_top_for_cross_check.get("text_score") or 0.0)
                if keyword_top_for_cross_check is not None
                else 0.0
            )

            # Experimental: use normalized score for NOOP decision.
            # Dense API embeddings (qwen3 etc) compress cosine similarity into
            # a narrow band (~0.85-1.0). Normalization stretches this range so
            # the NOOP threshold only fires on true duplicates (~1.0 raw).
            # Raw score is kept for UPDATE decision (still needs 0.78+ to match).
            _noop_score = vector_score
            if _norm:
                _norm_floor = self._write_guard_normalization_floor
                _norm_range = max(0.01, 1.0 - _norm_floor)
                _noop_score = max(0.0, min(1.0, (vector_score - _norm_floor) / _norm_range))

            if _noop_score >= self._write_guard_semantic_noop_threshold:
                # Cross-check: when semantic is in the boundary zone (noop threshold
                # to noop+0.04) and keyword is low, the content may be a rephrased
                # update rather than a true duplicate. Very high semantic (>= threshold+0.04)
                # is trusted as NOOP regardless of keyword score.
                noop_boundary_ceiling = min(1.0, self._write_guard_semantic_noop_threshold + 0.04)
                is_boundary = _noop_score < noop_boundary_ceiling
                kw_contradicts = kw_cross_score < self._write_guard_keyword_update_threshold
                if is_boundary and kw_contradicts:
                    return self._build_guard_decision(
                        action="UPDATE",
                        target_id=semantic_top.get("memory_id"),
                        target_uri=semantic_top.get("uri"),
                        reason=(
                            f"semantic {vector_score:.3f} "
                            f"(normalized {_noop_score:.3f}) in noop boundary "
                            f"but keyword {kw_cross_score:.3f} < "
                            f"{self._write_guard_keyword_update_threshold:.3f} "
                            "suggests updated content, not duplicate"
                        ),
                        method="embedding_cross_check",
                        degrade_reasons=degrade_reasons,
                        semantic_top=semantic_top,
                        keyword_top=keyword_top_for_cross_check,
                    )
                return self._build_guard_decision(
                    action="NOOP",
                    target_id=semantic_top.get("memory_id"),
                    target_uri=semantic_top.get("uri"),
                    reason=(
                        "semantic similarity "
                        f"{vector_score:.3f} "
                        f"(normalized {_noop_score:.3f}) >= "
                        f"{self._write_guard_semantic_noop_threshold:.3f}"
                    ),
                    method="embedding",
                    degrade_reasons=degrade_reasons,
                    semantic_top=semantic_top,
                    keyword_top=keyword_top_for_cross_check,
                )
            if vector_score >= self._write_guard_semantic_update_threshold:
                # Experimental: expanded cross-check in UPDATE zone.
                # Use GLOBAL keyword top (not same-target) for the cross-check:
                # "does the new content have ANY keyword overlap with ANY existing
                # memory?" If not, the high semantic score is likely a false
                # positive from embedding floor compression.
                if _norm:
                    _kw_weak = (
                        kw_cross_score
                        < self._write_guard_cross_check_add_floor
                    )
                    if _kw_weak:
                        # Before returning ADD, let LLM do content-level diff
                        # rescue if enabled. The heuristic can't distinguish these
                        # cases, but LLM can compare old vs new content.
                        _llm_rescue = await self._write_guard_llm_diff_rescue(
                            content=query,
                            semantic_top=semantic_top,
                            degrade_reasons=degrade_reasons,
                        )
                        if _llm_rescue is not None:
                            return _llm_rescue
                        return self._build_guard_decision(
                            action="ADD",
                            reason=(
                                f"normalized: semantic {vector_score:.3f} in update zone "
                                f"but global keyword {kw_cross_score:.3f} < "
                                f"{self._write_guard_cross_check_add_floor:.3f}"
                            ),
                            method="normalized_cross_check",
                            degrade_reasons=degrade_reasons,
                            semantic_top=semantic_top,
                            keyword_top=keyword_top_for_cross_check,
                        )
                return self._build_guard_decision(
                    action="UPDATE",
                    target_id=semantic_top.get("memory_id"),
                    target_uri=semantic_top.get("uri"),
                    reason=(
                        "semantic similarity "
                        f"{vector_score:.3f} >= "
                        f"{self._write_guard_semantic_update_threshold:.3f}"
                    ),
                    method="embedding",
                    degrade_reasons=degrade_reasons,
                    semantic_top=semantic_top,
                    keyword_top=keyword_top_for_cross_check,
                )

        keyword_top = (
            max(
                keyword_candidates,
                key=lambda item: float(item.get("text_score") or 0.0),
            )
            if keyword_candidates
            else None
        )
        if keyword_top is not None:
            text_score = float(keyword_top.get("text_score") or 0.0)
            if text_score >= self._write_guard_keyword_noop_threshold:
                return self._build_guard_decision(
                    action="NOOP",
                    target_id=keyword_top.get("memory_id"),
                    target_uri=keyword_top.get("uri"),
                    reason=(
                        "keyword overlap score "
                        f"{text_score:.3f} >= "
                        f"{self._write_guard_keyword_noop_threshold:.3f}"
                    ),
                    method="keyword",
                    degrade_reasons=degrade_reasons,
                    semantic_top=semantic_top,
                    keyword_top=keyword_top,
                )
            if text_score >= self._write_guard_keyword_update_threshold:
                return self._build_guard_decision(
                    action="UPDATE",
                    target_id=keyword_top.get("memory_id"),
                    target_uri=keyword_top.get("uri"),
                    reason=(
                        "keyword overlap score "
                        f"{text_score:.3f} >= "
                        f"{self._write_guard_keyword_update_threshold:.3f}"
                    ),
                    method="keyword",
                    degrade_reasons=degrade_reasons,
                    semantic_top=semantic_top,
                    keyword_top=keyword_top,
                )

        llm_decision = await self._write_guard_llm_decision(
            content=query,
            semantic_candidates=semantic_candidates,
            keyword_candidates=keyword_candidates,
            degrade_reasons=degrade_reasons,
        )
        if llm_decision is not None:
            return llm_decision

        if semantic_unavailable and keyword_top is not None:
            text_score = float(keyword_top.get("text_score") or 0.0)
            if text_score >= self._write_guard_single_pipeline_keyword_floor:
                self._append_degrade_reason(
                    degrade_reasons,
                    "write_guard_single_pipeline_keyword_blocked",
                )
                return self._build_guard_decision(
                    action="NOOP",
                    target_id=keyword_top.get("memory_id"),
                    target_uri=keyword_top.get("uri"),
                    reason=(
                        "single-pipeline keyword guard blocked add at "
                        f"{text_score:.3f} >= "
                        f"{self._write_guard_single_pipeline_keyword_floor:.3f}"
                    ),
                    method="keyword_single_pipeline",
                    degrade_reasons=degrade_reasons,
                    semantic_top=semantic_top,
                    keyword_top=keyword_top,
                )
        if keyword_unavailable and semantic_top is not None:
            vector_score = float(semantic_top.get("vector_score") or 0.0)
            if vector_score >= self._write_guard_single_pipeline_semantic_floor:
                self._append_degrade_reason(
                    degrade_reasons,
                    "write_guard_single_pipeline_semantic_blocked",
                )
                return self._build_guard_decision(
                    action="NOOP",
                    target_id=semantic_top.get("memory_id"),
                    target_uri=semantic_top.get("uri"),
                    reason=(
                        "single-pipeline semantic guard blocked add at "
                        f"{vector_score:.3f} >= "
                        f"{self._write_guard_single_pipeline_semantic_floor:.3f}"
                    ),
                    method="embedding_single_pipeline",
                    degrade_reasons=degrade_reasons,
                    semantic_top=semantic_top,
                    keyword_top=keyword_top,
                )

        return self._build_guard_decision(
            action="ADD",
            reason="no strong duplicate signal",
            method="keyword",
            degrade_reasons=degrade_reasons,
            semantic_top=semantic_top,
            keyword_top=keyword_top,
        )

    async def search_advanced(
        self,
        query: str,
        mode: str = "keyword",
        max_results: int = 8,
        candidate_multiplier: int = 4,
        filters: Optional[Dict[str, Any]] = None,
        intent_profile: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Advanced retrieval with keyword/semantic/hybrid modes.

        Returns chunk-level hits with component scores and metadata.
        """
        query = (query or "").strip()
        intent_applied: Optional[str] = None
        strategy_template = "default"
        try:
            requested_candidate_multiplier = int(candidate_multiplier)
        except (TypeError, ValueError):
            requested_candidate_multiplier = 4
        applied_candidate_multiplier = max(1, requested_candidate_multiplier)

        if isinstance(intent_profile, dict):
            intent_candidate = str(intent_profile.get("intent") or "").strip().lower()
            if intent_candidate in {"factual", "exploratory", "temporal", "causal"}:
                intent_applied = intent_candidate
                if intent_candidate == "factual":
                    strategy_template = "factual_high_precision"
                    if self._factual_candidate_multiplier_cap > 0:
                        applied_candidate_multiplier = min(
                            applied_candidate_multiplier,
                            self._factual_candidate_multiplier_cap,
                        )
                elif intent_candidate == "exploratory":
                    strategy_template = "exploratory_high_recall"
                    applied_candidate_multiplier = max(applied_candidate_multiplier, 6)
                elif intent_candidate == "temporal":
                    strategy_template = "temporal_time_filtered"
                    applied_candidate_multiplier = max(applied_candidate_multiplier, 5)
                elif intent_candidate == "causal":
                    strategy_template = "causal_wide_pool"
                    applied_candidate_multiplier = max(applied_candidate_multiplier, 8)
        applied_candidate_multiplier = min(
            max(1, applied_candidate_multiplier),
            self._search_hard_max_candidate_multiplier,
        )

        strategy_metadata = {
            "intent": intent_applied,
            "strategy_template": strategy_template,
            "candidate_multiplier_applied": applied_candidate_multiplier,
            "factual_candidate_multiplier_cap": int(
                self._factual_candidate_multiplier_cap
            ),
            "search_hard_max_candidate_multiplier": int(
                self._search_hard_max_candidate_multiplier
            ),
        }
        default_mmr_metadata = {
            "mmr_applied": False,
            "mmr_candidate_count": 0,
            "mmr_selected_count": 0,
            "mmr_duplicate_ratio_before": 0.0,
            "mmr_duplicate_ratio_after": 0.0,
        }
        default_trace_metadata = {
            "stage_timings_ms": {},
            "candidate_counts": {
                "candidate_limit": 0,
                "semantic_candidate_limit": 0,
                "keyword_rows": 0,
                "semantic_rows": 0,
                "gist_rows": 0,
                "alias_rows": 0,
                "ancestor_rows": 0,
                "combined_candidates": 0,
                "combined_candidates_collapsed": 0,
                "returned_results": 0,
            },
            "rerank_applied": False,
            "rerank_provider": self._reranker_provider,
            "rerank_route": "primary",
            "rerank_candidate_pool": 0,
            "rerank_group_count": 0,
            "rerank_grouping": (
                "memory" if self._reranker_group_by_memory else "chunk"
            ),
            "rerank_pruned_groups": 0,
            "rerank_documents": 0,
            "rerank_hits": 0,
            "rerank_hit_ratio": 0.0,
            "rerank_timeout_sec": round(float(self._reranker_timeout_sec), 3),
            "rerank_top_n": int(self._reranker_top_n),
            "rerank_top_n_effective": 0,
            "rerank_group_by_memory": bool(self._reranker_group_by_memory),
            "same_uri_collapse": {
                "applied": False,
                "rows_before": 0,
                "rows_after": 0,
                "collapsed_rows": 0,
                "collapsed_groups": 0,
            },
        }
        vector_engine_metadata = {
            "vector_engine_requested": self._vector_engine_requested,
            "vector_engine_effective": self._vector_engine_effective,
            "vector_engine_selected": "legacy",
            "vector_engine_path": "not_applicable",
            "sqlite_vec_knn_ready": bool(self._sqlite_vec_knn_ready),
            "sqlite_vec_knn_dim": int(self._sqlite_vec_knn_dim),
            "sqlite_vec_enabled": self._sqlite_vec_enabled,
            "sqlite_vec_read_ratio": int(self._sqlite_vec_read_ratio),
            "sqlite_vec_status": str(self._sqlite_vec_capability.get("status", "disabled")),
            "sqlite_vec_readiness": str(
                self._sqlite_vec_capability.get("sqlite_vec_readiness", "hold")
            ),
            "semantic_vector_block_reason": self._semantic_vector_block_reason or None,
            "semantic_vector_stored_dim": self._semantic_vector_stored_dim,
            "semantic_vector_detected_dims": list(self._semantic_vector_detected_dims),
        }

        if not query:
            degrade_reasons = ["empty_query"]
            return {
                "results": [],
                "mode": "keyword",
                "requested_mode": mode,
                "degraded": True,
                "degrade_reason": "empty_query",
                "degrade_reasons": degrade_reasons,
                "metadata": {
                    "degraded": True,
                    "degrade_reasons": degrade_reasons,
                    **strategy_metadata,
                    **default_trace_metadata,
                    **vector_engine_metadata,
                    **default_mmr_metadata,
                },
            }

        mode_value = (mode or "keyword").strip().lower()
        if mode_value not in {"keyword", "semantic", "hybrid"}:
            raise ValueError("mode must be one of: keyword, semantic, hybrid")

        requested_mode = mode_value
        degrade_reasons: List[str] = []
        if mode_value in {"semantic", "hybrid"} and not self._vector_available:
            mode_value = "keyword"
            self._append_degrade_reason(degrade_reasons, "vector_backend_disabled")

        try:
            parsed_max_results = int(max_results)
        except (TypeError, ValueError):
            parsed_max_results = 8
        max_results = max(1, parsed_max_results)
        candidate_multiplier = applied_candidate_multiplier
        candidate_limit = max_results * candidate_multiplier
        semantic_candidate_limit = candidate_limit
        filters = filters or {}

        async with self.session() as session:
            where_parts = ["m.deprecated = 0"]
            where_params: Dict[str, Any] = {}

            domain_filter = filters.get("domain")
            path_prefix_filter = filters.get("path_prefix")
            priority_filter = filters.get("max_priority", filters.get("priority"))
            updated_after_filter = self._parse_iso_datetime(filters.get("updated_after"))

            if path_prefix_filter and isinstance(path_prefix_filter, str) and "://" in path_prefix_filter:
                prefix_domain, prefix_path = path_prefix_filter.split("://", 1)
                prefix_domain = prefix_domain.strip().lower()
                prefix_path = prefix_path.strip("/")
                if prefix_domain:
                    domain_filter = domain_filter or prefix_domain
                path_prefix_filter = prefix_path

            if domain_filter:
                where_parts.append("p.domain = :domain_filter")
                where_params["domain_filter"] = str(domain_filter)

            path_prefix_filter = self._append_path_prefix_where_clause(
                where_parts=where_parts,
                where_params=where_params,
                column_sql="p.path",
                path_prefix=path_prefix_filter,
            )

            if priority_filter is not None:
                try:
                    where_parts.append("p.priority <= :priority_filter")
                    where_params["priority_filter"] = int(priority_filter)
                except (TypeError, ValueError):
                    pass

            if updated_after_filter is not None:
                where_parts.append("m.created_at >= :updated_after_filter")
                where_params["updated_after_filter"] = updated_after_filter.strftime(
                    "%Y-%m-%d %H:%M:%S.%f"
                )

            where_clause = " AND ".join(where_parts)

            keyword_rows: List[Dict[str, Any]] = []
            semantic_rows: List[Dict[str, Any]] = []
            gist_rows: List[Dict[str, Any]] = []
            stage_timings_ms: Dict[str, float] = {}
            total_started_at = time.perf_counter()

            semantic_block_reason = str(self._semantic_vector_block_reason or "").strip()
            semantic_fallback_to_keyword = (
                mode_value == "semantic" and bool(semantic_block_reason)
            )

            if mode_value in {"keyword", "hybrid"} or semantic_fallback_to_keyword:
                keyword_started_at = time.perf_counter()
                if self._fts_available:
                    fts_query = self._build_safe_fts_query(query)
                    if fts_query:
                        try:
                            keyword_result = await session.execute(
                                text(
                                    "SELECT "
                                    "mc.id AS chunk_id, mc.memory_id AS memory_id, "
                                    "mc.chunk_text AS chunk_text, mc.char_start AS char_start, mc.char_end AS char_end, "
                                    "p.domain AS domain, p.path AS path, p.priority AS priority, p.disclosure AS disclosure, "
                                    "m.created_at AS created_at, "
                                    "m.vitality_score AS vitality_score, "
                                    "m.access_count AS access_count, "
                                    "m.last_accessed_at AS last_accessed_at, "
                                    "LENGTH(mc.chunk_text) AS chunk_length, "
                                    "bm25(memory_chunks_fts) AS text_rank "
                                    "FROM memory_chunks_fts "
                                    "JOIN memory_chunks mc ON mc.id = memory_chunks_fts.chunk_id "
                                    "JOIN memories m ON m.id = mc.memory_id "
                                    "JOIN paths p ON p.memory_id = mc.memory_id "
                                    f"WHERE {where_clause} "
                                    "AND memory_chunks_fts MATCH :fts_query "
                                    "ORDER BY text_rank ASC "
                                    "LIMIT :candidate_limit"
                                ),
                                {
                                    **where_params,
                                    "fts_query": fts_query,
                                    "candidate_limit": candidate_limit,
                                },
                            )
                            keyword_rows = [
                                dict(row) for row in keyword_result.mappings().all()
                            ]
                        except Exception as exc:
                            await self._handle_fts_runtime_error(
                                session,
                                exc,
                                context="search",
                            )

                if not keyword_rows:
                    like_terms = self._build_like_fallback_terms(query)
                    like_params = {
                        **where_params,
                        "candidate_limit": candidate_limit,
                    }
                    like_clauses: List[str] = []
                    for index, term in enumerate(like_terms):
                        pattern_key = f"like_pattern_{index}"
                        like_params[pattern_key] = self._contains_like_pattern(
                            self._unicode_search_fold(term)
                        )
                        like_clauses.append(
                            f"unicode_search_fold(mc.chunk_text) LIKE :{pattern_key} ESCAPE '\\'"
                        )
                        like_clauses.append(
                            f"unicode_search_fold(p.path) LIKE :{pattern_key} ESCAPE '\\'"
                        )
                    keyword_result = await session.execute(
                        text(
                            "SELECT "
                            "mc.id AS chunk_id, mc.memory_id AS memory_id, "
                            "mc.chunk_text AS chunk_text, mc.char_start AS char_start, mc.char_end AS char_end, "
                            "p.domain AS domain, p.path AS path, p.priority AS priority, p.disclosure AS disclosure, "
                            "m.created_at AS created_at, "
                            "m.vitality_score AS vitality_score, "
                            "m.access_count AS access_count, "
                            "m.last_accessed_at AS last_accessed_at, "
                            "LENGTH(mc.chunk_text) AS chunk_length "
                            "FROM memory_chunks mc "
                            "JOIN memories m ON m.id = mc.memory_id "
                            "JOIN paths p ON p.memory_id = mc.memory_id "
                            f"WHERE {where_clause} "
                            f"AND ({' OR '.join(like_clauses)}) "
                            "ORDER BY p.priority ASC, m.created_at DESC "
                            "LIMIT :candidate_limit"
                        ),
                        like_params,
                    )
                    keyword_rows = [dict(row) for row in keyword_result.mappings().all()]

                # Legacy fallback for pre-index data
                if not keyword_rows:
                    search_pattern = self._contains_like_pattern(query)
                    legacy_query = (
                        select(Memory, Path)
                        .join(Path, Memory.id == Path.memory_id)
                        .where(Memory.deprecated == False)
                        .where(
                            or_(
                                Path.path.like(search_pattern, escape="\\"),
                                Memory.content.like(search_pattern, escape="\\"),
                            )
                        )
                    )
                    if domain_filter:
                        legacy_query = legacy_query.where(Path.domain == str(domain_filter))
                    if path_prefix_filter:
                        legacy_query = legacy_query.where(
                            self._build_path_prefix_sqlalchemy_condition(
                                Path.path, path_prefix_filter
                            )
                        )
                    if priority_filter is not None:
                        try:
                            legacy_query = legacy_query.where(
                                Path.priority <= int(priority_filter)
                            )
                        except (TypeError, ValueError):
                            pass
                    if updated_after_filter is not None:
                        legacy_query = legacy_query.where(
                            Memory.created_at >= updated_after_filter
                        )

                    legacy_result = await session.execute(
                        legacy_query.order_by(Path.priority.asc(), Memory.created_at.desc()).limit(
                            candidate_limit
                        )
                    )
                    for memory, path_obj in legacy_result.all():
                        keyword_rows.append(
                            {
                                "chunk_id": None,
                                "memory_id": memory.id,
                                "chunk_text": memory.content,
                                "char_start": 0,
                                "char_end": len(memory.content or ""),
                                "domain": path_obj.domain,
                                "path": path_obj.path,
                                "priority": path_obj.priority,
                                "disclosure": path_obj.disclosure,
                                "created_at": memory.created_at,
                                "vitality_score": memory.vitality_score,
                                "access_count": memory.access_count,
                                "last_accessed_at": memory.last_accessed_at,
                                "chunk_length": len(memory.content or ""),
                            }
                        )
                stage_timings_ms["keyword"] = round(
                    (time.perf_counter() - keyword_started_at) * 1000.0, 3
                )

            if mode_value in {"semantic", "hybrid"} and semantic_block_reason:
                self._append_degrade_reason(degrade_reasons, semantic_block_reason)
                if semantic_fallback_to_keyword:
                    mode_value = "keyword"
                    vector_engine_metadata["vector_engine_selected"] = "disabled"
                    vector_engine_metadata["vector_engine_path"] = (
                        "keyword_fallback_dim_mismatch"
                    )
                else:
                    vector_engine_metadata["vector_engine_selected"] = "disabled"
                    vector_engine_metadata["vector_engine_path"] = (
                        "semantic_disabled_dim_mismatch"
                    )

            if mode_value in {"semantic", "hybrid"} and not semantic_block_reason:
                semantic_started_at = time.perf_counter()
                requested_vector_engine = self._normalize_vector_engine(
                    self._vector_engine_requested
                )
                selected_vector_engine = self._resolve_vector_engine_for_query(query)
                vector_engine_metadata["vector_engine_selected"] = selected_vector_engine
                if (
                    requested_vector_engine != "legacy"
                    and self._vector_engine_effective == "legacy"
                ):
                    self._append_degrade_reason(
                        degrade_reasons, "sqlite_vec_fallback_legacy"
                    )

                query_embedding = await self._get_embedding(
                    session,
                    query,
                    degrade_reasons=degrade_reasons,
                )
                semantic_pool_limit = min(
                    max(candidate_limit * 12, max_results * 64, 128),
                    5000,
                )
                semantic_candidate_limit = min(
                    semantic_pool_limit,
                    max(
                        candidate_limit,
                        candidate_limit * max(1, self._semantic_overfetch_factor),
                    ),
                )
                if selected_vector_engine == "vec":
                    if not self._sqlite_vec_knn_ready:
                        self._append_degrade_reason(
                            degrade_reasons, "sqlite_vec_knn_unavailable"
                        )
                        semantic_rows = await self._fetch_semantic_rows_python_scoring(
                            session,
                            where_clause=where_clause,
                            where_params=where_params,
                            query_embedding=query_embedding,
                            semantic_pool_limit=semantic_pool_limit,
                            candidate_limit=semantic_candidate_limit,
                        )
                        vector_engine_metadata["vector_engine_path"] = (
                            "legacy_python_fallback"
                        )
                    else:
                        try:
                            semantic_rows = await self._fetch_semantic_rows_vec_native_topk(
                                session,
                                where_clause=where_clause,
                                where_params=where_params,
                                query_embedding=query_embedding,
                                semantic_pool_limit=semantic_pool_limit,
                                candidate_limit=semantic_candidate_limit,
                            )
                            vector_engine_metadata["vector_engine_path"] = (
                                "vec_native_topk_sql"
                            )
                        except Exception:
                            self._append_degrade_reason(
                                degrade_reasons, "sqlite_vec_native_query_failed"
                            )
                            semantic_rows = await self._fetch_semantic_rows_python_scoring(
                                session,
                                where_clause=where_clause,
                                where_params=where_params,
                                query_embedding=query_embedding,
                                semantic_pool_limit=semantic_pool_limit,
                                candidate_limit=semantic_candidate_limit,
                            )
                            vector_engine_metadata["vector_engine_path"] = (
                                "legacy_python_fallback"
                            )
                else:
                    semantic_rows = await self._fetch_semantic_rows_python_scoring(
                        session,
                        where_clause=where_clause,
                        where_params=where_params,
                        query_embedding=query_embedding,
                        semantic_pool_limit=semantic_pool_limit,
                        candidate_limit=semantic_candidate_limit,
                    )
                    vector_engine_metadata["vector_engine_path"] = (
                        "legacy_python_scoring"
                    )
                stage_timings_ms["semantic"] = round(
                    (time.perf_counter() - semantic_started_at) * 1000.0, 3
                )

            gist_started_at = time.perf_counter()
            gist_rows = await self._fetch_gist_candidate_rows(
                session,
                query=query,
                where_clause=where_clause,
                where_params=where_params,
                candidate_limit=candidate_limit,
            )
            stage_timings_ms["gist"] = round(
                (time.perf_counter() - gist_started_at) * 1000.0, 3
            )

            candidates: Dict[Tuple[str, str, Any], Dict[str, Any]] = {}

            def upsert_candidate(
                row: Dict[str, Any],
                *,
                vector_score: float,
                text_score: float,
                context_score: float,
                stage: str,
                stage_score: float,
                rerankable: bool,
            ) -> None:
                key = (str(row.get("domain", "")), str(row.get("path", "")), row.get("chunk_id"))
                item = candidates.get(key)
                if item is None:
                    item = {
                        "memory_id": row.get("memory_id"),
                        "chunk_id": row.get("chunk_id"),
                        "chunk_text": row.get("chunk_text") or "",
                        "char_start": int(row.get("char_start") or 0),
                        "char_end": int(row.get("char_end") or 0),
                        "domain": row.get("domain") or "core",
                        "path": row.get("path") or "",
                        "priority": int(row.get("priority") or 0),
                        "disclosure": row.get("disclosure"),
                        "created_at": row.get("created_at"),
                        "vitality_score": float(row.get("vitality_score") or 0.0),
                        "access_count": int(row.get("access_count") or 0),
                        "last_accessed_at": row.get("last_accessed_at"),
                        "chunk_length": int(row.get("chunk_length") or 0),
                        "gist_quality": float(row.get("gist_quality") or 0.0),
                        "vector_score": 0.0,
                        "text_score": 0.0,
                        "context_score": 0.0,
                        "recall_kind": str(row.get("recall_kind") or "direct"),
                        "origin_uri": row.get("origin_uri"),
                        "origin_memory_id": row.get("origin_memory_id"),
                        "ancestor_depth": row.get("ancestor_depth"),
                        "rerankable": bool(rerankable),
                        "stage_hits": set(),
                        "stage_scores": {},
                    }
                    candidates[key] = item

                item["vector_score"] = max(item["vector_score"], vector_score)
                item["text_score"] = max(item["text_score"], text_score)
                item["context_score"] = max(item["context_score"], context_score)
                item["gist_quality"] = max(item["gist_quality"], float(row.get("gist_quality") or 0.0))
                item["rerankable"] = bool(item.get("rerankable")) or bool(rerankable)
                stage_hits = item.get("stage_hits")
                if not isinstance(stage_hits, set):
                    stage_hits = set()
                    item["stage_hits"] = stage_hits
                stage_hits.add(stage)
                stage_scores = item.get("stage_scores")
                if not isinstance(stage_scores, dict):
                    stage_scores = {}
                    item["stage_scores"] = stage_scores
                stage_scores[stage] = round(
                    max(float(stage_scores.get(stage) or 0.0), max(0.0, stage_score)), 6
                )

            for row in keyword_rows:
                text_rank = row.get("text_rank")
                if text_rank is not None:
                    try:
                        score = 1.0 / (1.0 + max(float(text_rank), 0.0))
                    except (TypeError, ValueError):
                        like_stats = self._like_match_statistics(
                            query, row.get("chunk_text", ""), row.get("path", "")
                        )
                        if not bool(like_stats.get("relevant")):
                            continue
                        score = float(like_stats.get("score") or 0.0)
                else:
                    like_stats = self._like_match_statistics(
                        query, row.get("chunk_text", ""), row.get("path", "")
                    )
                    if not bool(like_stats.get("relevant")):
                        continue
                    score = float(like_stats.get("score") or 0.0)
                upsert_candidate(
                    row,
                    vector_score=0.0,
                    text_score=score,
                    context_score=0.0,
                    stage="keyword",
                    stage_score=score,
                    rerankable=True,
                )

            for row in semantic_rows:
                similarity = float(row.get("vector_similarity", 0.0))
                vector_score = max(0.0, min(1.0, (similarity + 1.0) / 2.0))
                upsert_candidate(
                    row,
                    vector_score=vector_score,
                    text_score=0.0,
                    context_score=0.0,
                    stage="semantic",
                    stage_score=vector_score,
                    rerankable=True,
                )

            for row in gist_rows:
                gist_text_score = self._like_text_score(
                    query, row.get("chunk_text", ""), row.get("path", "")
                )
                gist_quality = max(0.0, min(1.0, float(row.get("gist_quality") or 0.0)))
                upsert_candidate(
                    row,
                    vector_score=0.0,
                    text_score=max(gist_text_score, gist_quality * 0.35),
                    context_score=0.0,
                    stage="gist",
                    stage_score=max(gist_text_score, gist_quality),
                    rerankable=True,
                )

            alias_rows: List[Dict[str, Any]] = []
            ancestor_rows: List[Dict[str, Any]] = []
            if candidates:
                memory_seeds, path_seeds = self._select_context_recall_seeds(
                    list(candidates.values()),
                    max_results=max_results,
                )
                seen_uris = {
                    self._candidate_uri(item.get("domain"), item.get("path"))
                    for item in candidates.values()
                }

                alias_started_at = time.perf_counter()
                try:
                    alias_rows = await self._fetch_alias_candidate_rows(
                        session,
                        memory_seeds=memory_seeds,
                        seen_uris=set(seen_uris),
                        domain_filter=domain_filter,
                        path_prefix_filter=path_prefix_filter,
                        priority_filter=priority_filter,
                        updated_after_filter=updated_after_filter,
                    )
                except Exception:
                    alias_rows = []
                    self._append_degrade_reason(
                        degrade_reasons, "alias_recall_lookup_failed"
                    )
                stage_timings_ms["alias_recall"] = round(
                    (time.perf_counter() - alias_started_at) * 1000.0, 3
                )

                for row in alias_rows:
                    alias_text_score = self._like_text_score(
                        query, row.get("chunk_text", ""), row.get("path", "")
                    )
                    context_signal = float(row.get("context_score") or 0.0)
                    upsert_candidate(
                        row,
                        vector_score=0.0,
                        text_score=alias_text_score * 0.55,
                        context_score=context_signal,
                        stage="alias",
                        stage_score=max(alias_text_score, context_signal),
                        rerankable=False,
                    )
                    seen_uris.add(
                        self._candidate_uri(row.get("domain"), row.get("path"))
                    )

                ancestor_started_at = time.perf_counter()
                try:
                    ancestor_rows = await self._fetch_ancestor_candidate_rows(
                        session,
                        path_seeds=path_seeds,
                        seen_uris=set(seen_uris),
                        domain_filter=domain_filter,
                        path_prefix_filter=path_prefix_filter,
                        priority_filter=priority_filter,
                        updated_after_filter=updated_after_filter,
                    )
                except Exception:
                    ancestor_rows = []
                    self._append_degrade_reason(
                        degrade_reasons, "ancestor_recall_lookup_failed"
                    )
                stage_timings_ms["ancestor_recall"] = round(
                    (time.perf_counter() - ancestor_started_at) * 1000.0, 3
                )

                for row in ancestor_rows:
                    ancestor_text_score = self._like_text_score(
                        query, row.get("chunk_text", ""), row.get("path", "")
                    )
                    context_signal = float(row.get("context_score") or 0.0)
                    upsert_candidate(
                        row,
                        vector_score=0.0,
                        text_score=ancestor_text_score * 0.40,
                        context_score=context_signal,
                        stage="ancestor",
                        stage_score=max(ancestor_text_score, context_signal),
                        rerankable=False,
                    )

            if not candidates:
                degraded = bool(degrade_reasons)
                semantic_search_unavailable = (
                    mode_value in {"semantic", "hybrid"}
                    and "embedding_fallback_hash" in degrade_reasons
                )
                return {
                    "results": [],
                    "mode": mode_value,
                    "requested_mode": requested_mode,
                    "degraded": degraded,
                    "semantic_search_unavailable": semantic_search_unavailable,
                    "degrade_reason": degrade_reasons[0] if degrade_reasons else None,
                    "degrade_reasons": list(degrade_reasons),
                    "metadata": {
                        "degraded": degraded,
                        "semantic_search_unavailable": semantic_search_unavailable,
                        "degrade_reasons": list(degrade_reasons),
                        **strategy_metadata,
                        **default_trace_metadata,
                        **vector_engine_metadata,
                        **default_mmr_metadata,
                    },
                }

            if mode_value == "keyword":
                weights = {
                    "vector": 0.0,
                    "text": 0.80,
                    "priority": 0.12,
                    "recency": 0.06,
                    "path_prefix": 0.02,
                    "context": 0.05,
                }
            elif mode_value == "semantic":
                weights = {
                    "vector": 0.82,
                    "text": 0.0,
                    "priority": 0.10,
                    "recency": 0.06,
                    "path_prefix": 0.02,
                    "context": 0.05,
                }
            else:
                weights = {
                    "vector": self._weight_vector,
                    "text": self._weight_text,
                    "priority": self._weight_priority,
                    "recency": self._weight_recency,
                    "path_prefix": self._weight_path_prefix,
                    "context": 0.05,
                }

            if strategy_template != "default" and mode_value == "hybrid":
                if strategy_template == "factual_high_precision":
                    weights = {
                        "vector": 0.22,
                        "text": 0.58,
                        "priority": 0.12,
                        "recency": 0.06,
                        "path_prefix": 0.02,
                        "context": 0.05,
                    }
                elif strategy_template == "exploratory_high_recall":
                    weights = {
                        "vector": 0.58,
                        "text": 0.24,
                        "priority": 0.08,
                        "recency": 0.07,
                        "path_prefix": 0.03,
                        "context": 0.05,
                    }
                elif strategy_template == "temporal_time_filtered":
                    weights = {
                        "vector": 0.28,
                        "text": 0.22,
                        "priority": 0.08,
                        "recency": 0.38,
                        "path_prefix": 0.04,
                        "context": 0.05,
                    }
                elif strategy_template == "causal_wide_pool":
                    weights = {
                        "vector": 0.52,
                        "text": 0.28,
                        "priority": 0.08,
                        "recency": 0.08,
                        "path_prefix": 0.04,
                        "context": 0.05,
                    }

            now = _utc_now_naive()
            scored_results: List[Dict[str, Any]] = []
            prefix_value = str(path_prefix_filter) if path_prefix_filter else ""
            candidate_items = list(candidates.values())
            component_scores_by_index: Dict[int, Dict[str, float]] = {}
            base_scores_by_index: Dict[int, float] = {}
            for idx, item in enumerate(candidate_items):
                components = self._compute_candidate_score_components(
                    item=item,
                    query=query,
                    prefix_value=prefix_value,
                    now_value=now,
                )
                component_scores_by_index[idx] = components
                base_scores_by_index[idx] = self._compute_base_candidate_score(
                    components=components,
                    weights=weights,
                )
            # FACTUAL FALLBACK (2026-04-06): when factual_high_precision was
            # selected but NO candidate has a meaningful text_score, the high
            # text weight (0.58) is wasted and the low vector weight (0.22)
            # suppresses the only useful signal.  Fall back to default hybrid
            # weights so vector can still drive ranking.
            # Evidence: HQ14 benchmark — "ensure API changes don't break" vs
            # "api-versioning" has zero keyword overlap; factual pushed vector
            # from 0.70 to 0.22, causing target to drop out of top-10.
            if (
                strategy_template == "factual_high_precision"
                and mode_value == "hybrid"
                and component_scores_by_index
            ):
                max_text = max(
                    (cs.get("text", 0.0) for cs in component_scores_by_index.values()),
                    default=0.0,
                )
                if max_text < 0.01:
                    default_weights = {
                        "vector": self._weight_vector,
                        "text": self._weight_text,
                        "priority": self._weight_priority,
                        "recency": self._weight_recency,
                        "path_prefix": self._weight_path_prefix,
                        "context": 0.05,
                    }
                    for idx_fb in base_scores_by_index:
                        base_scores_by_index[idx_fb] = self._compute_base_candidate_score(
                            components=component_scores_by_index[idx_fb],
                            weights=default_weights,
                        )

            rerank_scores_by_index: Dict[int, float] = {}
            rerank_documents_count = 0
            rerank_candidate_pool = 0
            rerank_group_count = 0
            rerank_pruned_groups = 0
            rerank_grouping = (
                "memory" if self._reranker_group_by_memory else "chunk"
            )
            rerank_provider_value = self._reranker_provider
            rerank_route = "primary"
            rerank_top_n_effective = 0
            rerank_started_at = time.perf_counter()
            if candidate_items and self._reranker_enabled and mode_value != "keyword":
                rerank_plan = self._build_rerank_plan(
                    candidate_items=candidate_items,
                    base_scores=base_scores_by_index,
                    max_results=max_results,
                )
                rerank_documents = list(rerank_plan.get("documents") or [])
                rerank_index_groups = [
                    list(group)
                    for group in (rerank_plan.get("index_groups") or [])
                    if isinstance(group, list)
                ]
                rerank_candidate_pool = int(rerank_plan.get("candidate_pool") or 0)
                rerank_group_count = int(rerank_plan.get("group_count") or 0)
                rerank_pruned_groups = int(rerank_plan.get("pruned_count") or 0)
                rerank_grouping = str(rerank_plan.get("grouping") or rerank_grouping)
                rerank_top_n_effective = int(rerank_plan.get("selected_count") or 0)
                rerank_documents_count = len(rerank_documents)
                if rerank_documents:
                    planned_reranker_attempts = self._resolve_reranker_attempts(
                        rerank_documents
                    )
                    if planned_reranker_attempts:
                        rerank_provider_value = str(
                            planned_reranker_attempts[0].get("provider")
                            or rerank_provider_value
                        )
                        rerank_route = str(
                            planned_reranker_attempts[0].get("name") or rerank_route
                        )
                    raw_rerank_scores = await self._get_rerank_scores(
                        query,
                        rerank_documents,
                        degrade_reasons=degrade_reasons,
                    )
                    rerank_scores_by_index = {}
                    for doc_index, score in raw_rerank_scores.items():
                        if not isinstance(doc_index, int):
                            continue
                        if doc_index < 0 or doc_index >= len(rerank_index_groups):
                            continue
                        for candidate_index in rerank_index_groups[doc_index]:
                            previous = rerank_scores_by_index.get(candidate_index)
                            if previous is None or float(score or 0.0) > previous:
                                rerank_scores_by_index[candidate_index] = float(
                                    score or 0.0
                                )
            stage_timings_ms["rerank"] = round(
                (time.perf_counter() - rerank_started_at) * 1000.0, 3
            )

            scoring_started_at = time.perf_counter()
            for idx, item in enumerate(candidate_items):
                components = component_scores_by_index.get(idx) or {}
                base_score = float(base_scores_by_index.get(idx, 0.0))
                rerank_score = rerank_scores_by_index.get(idx, 0.0)
                final_score = base_score + (self._rerank_weight * rerank_score)
                created_at = item.get("created_at")
                if isinstance(created_at, str):
                    created_at = self._parse_iso_datetime(created_at)

                snippet = self._make_snippet(item["chunk_text"], query)
                domain = item.get("domain") or "core"
                path = item.get("path") or ""
                stage_scores = item.get("stage_scores")
                if not isinstance(stage_scores, dict):
                    stage_scores = {}
                stage_hits = item.get("stage_hits")
                if not isinstance(stage_hits, set):
                    stage_hits = set()

                scored_results.append(
                    {
                        "uri": f"{domain}://{path}",
                        "memory_id": item["memory_id"],
                        "chunk_id": item.get("chunk_id"),
                        "snippet": snippet,
                        "char_range": [item["char_start"], item["char_end"]],
                        # Keep a top-level score for direct callers while the
                        # richer component breakdown lives under scores.final.
                        "score": round(final_score, 6),
                        "scores": {
                            "vector": round(float(components.get("vector", 0.0)), 6),
                            "text": round(float(components.get("text", 0.0)), 6),
                            "context": round(float(components.get("context", 0.0)), 6),
                            "priority": round(float(components.get("priority", 0.0)), 6),
                            "recency": round(float(components.get("recency", 0.0)), 6),
                            "path_prefix": round(
                                float(components.get("path_prefix", 0.0)), 6
                            ),
                            "vitality": round(float(components.get("vitality", 0.0)), 6),
                            "access": round(float(components.get("access", 0.0)), 6),
                            "pending_event": round(
                                float(components.get("pending_event", 0.0)), 6
                            ),
                            "length_norm": round(
                                float(components.get("length_norm", 0.0)), 6
                            ),
                            "rerank": round(rerank_score, 6),
                            "final": round(final_score, 6),
                        },
                        "metadata": {
                            "domain": domain,
                            "path": path,
                            "priority": item.get("priority", 0),
                            "disclosure": item.get("disclosure"),
                            "search_provenance": {
                                "stages": sorted(str(stage) for stage in stage_hits),
                                "stage_scores": {
                                    str(stage): round(float(score or 0.0), 6)
                                    for stage, score in stage_scores.items()
                                },
                                "recall_kind": str(item.get("recall_kind") or "direct"),
                                "origin_uri": item.get("origin_uri"),
                                "origin_memory_id": item.get("origin_memory_id"),
                                "ancestor_depth": item.get("ancestor_depth"),
                                "gist_quality": round(
                                    float(item.get("gist_quality") or 0.0), 6
                                ),
                                "chunk_length": int(item.get("chunk_length") or 0),
                            },
                            "updated_at": created_at.isoformat()
                            if isinstance(created_at, datetime)
                            else None,
                        },
                    }
                )
            stage_timings_ms["scoring"] = round(
                (time.perf_counter() - scoring_started_at) * 1000.0, 3
            )

            scored_results.sort(key=lambda row: row["scores"]["final"], reverse=True)
            collapse_metadata = {
                "applied": False,
                "rows_before": len(scored_results),
                "rows_after": len(scored_results),
                "collapsed_rows": 0,
                "collapsed_groups": 0,
            }
            collapsed_results = scored_results
            if self._collapse_same_uri_results:
                collapsed_results, collapse_metadata = self._collapse_scored_results_by_uri(
                    scored_results
                )
            mmr_metadata: Dict[str, Any] = {
                "mmr_applied": False,
                "mmr_candidate_count": 0,
                "mmr_selected_count": 0,
                "mmr_duplicate_ratio_before": self._redundancy_ratio(collapsed_results),
                "mmr_duplicate_ratio_after": 0.0,
            }
            mmr_started_at = time.perf_counter()
            if self._mmr_enabled and mode_value == "hybrid":
                try:
                    top_results, mmr_metadata = self._apply_mmr_rerank(
                        collapsed_results,
                        max_results=max_results,
                    )
                except Exception:
                    self._append_degrade_reason(degrade_reasons, "mmr_rerank_failed")
                    top_results = collapsed_results[:max_results]
                    mmr_metadata = {
                        "mmr_applied": False,
                        "mmr_candidate_count": min(
                            len(collapsed_results),
                            max(1, max_results * max(1, self._mmr_candidate_factor)),
                        ),
                        "mmr_selected_count": len(top_results),
                        "mmr_duplicate_ratio_before": self._redundancy_ratio(
                            collapsed_results
                        ),
                    }
            else:
                top_results = collapsed_results[:max_results]
                mmr_metadata["mmr_selected_count"] = len(top_results)
            mmr_metadata["mmr_duplicate_ratio_after"] = self._redundancy_ratio(
                top_results
            )
            stage_timings_ms["mmr"] = round(
                (time.perf_counter() - mmr_started_at) * 1000.0, 3
            )
            stage_timings_ms["total"] = round(
                (time.perf_counter() - total_started_at) * 1000.0, 3
            )
            reinforced_memory_ids = list(
                dict.fromkeys(
                    int(row.get("memory_id"))
                    for row in top_results
                    if row.get("memory_id") is not None
                )
            )
            await self._best_effort_reinforce_memory_access(
                session,
                reinforced_memory_ids,
            )
            degraded = bool(degrade_reasons)
            semantic_search_unavailable = (
                mode_value in {"semantic", "hybrid"}
                and "embedding_fallback_hash" in degrade_reasons
            )
            return {
                "results": top_results,
                "mode": mode_value,
                "requested_mode": requested_mode,
                "degraded": degraded,
                "semantic_search_unavailable": semantic_search_unavailable,
                "degrade_reason": degrade_reasons[0] if degrade_reasons else None,
                "degrade_reasons": list(degrade_reasons),
                "metadata": {
                    "degraded": degraded,
                    "semantic_search_unavailable": semantic_search_unavailable,
                    "degrade_reasons": list(degrade_reasons),
                    "stage_timings_ms": stage_timings_ms,
                    "candidate_counts": {
                        "candidate_limit": candidate_limit,
                        "semantic_candidate_limit": semantic_candidate_limit,
                        "keyword_rows": len(keyword_rows),
                        "semantic_rows": len(semantic_rows),
                        "gist_rows": len(gist_rows),
                        "alias_rows": len(alias_rows),
                        "ancestor_rows": len(ancestor_rows),
                        "combined_candidates": len(candidate_items),
                        "combined_candidates_collapsed": len(collapsed_results),
                        "returned_results": len(top_results),
                    },
                    "rerank_applied": bool(rerank_scores_by_index),
                    "rerank_provider": rerank_provider_value,
                    "rerank_route": rerank_route,
                    "rerank_candidate_pool": int(rerank_candidate_pool),
                    "rerank_group_count": int(rerank_group_count),
                    "rerank_grouping": rerank_grouping,
                    "rerank_pruned_groups": int(rerank_pruned_groups),
                    "rerank_documents": rerank_documents_count,
                    "rerank_hits": sum(
                        1
                        for score in rerank_scores_by_index.values()
                        if float(score or 0.0) > 0.0
                    ),
                    "rerank_hit_ratio": round(
                        (
                            sum(
                                1
                                for score in rerank_scores_by_index.values()
                                if float(score or 0.0) > 0.0
                            )
                            / max(1, rerank_documents_count)
                        ),
                        6,
                    ),
                    "rerank_timeout_sec": round(float(self._reranker_timeout_sec), 3),
                    "rerank_top_n": int(self._reranker_top_n),
                    "rerank_top_n_effective": int(rerank_top_n_effective),
                    "rerank_group_by_memory": bool(self._reranker_group_by_memory),
                    "same_uri_collapse": collapse_metadata,
                    **strategy_metadata,
                    **vector_engine_metadata,
                    **mmr_metadata,
                },
            }

    async def search(
        self, query: str, limit: int = 10, domain: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Legacy-compatible search by path/content.

        Args:
            query: Search query
            limit: Max results
            domain: If specified, only search in this domain.
                    If None, search across all domains.

        Returns:
            Legacy result structure used by existing MCP layer.
        """
        filters = {"domain": domain} if domain is not None else {}
        advanced_payload = await self.search_advanced(
            query=query,
            mode="keyword",
            max_results=max(1, limit),
            candidate_multiplier=4,
            filters=filters,
        )
        advanced_results = (
            advanced_payload.get("results", [])
            if isinstance(advanced_payload, dict)
            else advanced_payload
        )

        matches: List[Dict[str, Any]] = []
        seen_memory_ids = set()

        for row in advanced_results:
            memory_id = row.get("memory_id")
            if memory_id in seen_memory_ids:
                continue
            seen_memory_ids.add(memory_id)

            metadata = row.get("metadata", {})
            domain_value = metadata.get("domain", "core")
            path_value = metadata.get("path", "")
            matches.append(
                {
                    "domain": domain_value,
                    "path": path_value,
                    "uri": row.get("uri", f"{domain_value}://{path_value}"),
                    "name": path_value.rsplit("/", 1)[-1] if path_value else "",
                    "snippet": row.get("snippet", ""),
                    "priority": metadata.get("priority", 0),
                }
            )

            if len(matches) >= limit:
                break

        return matches

    async def read_memory_segment(
        self,
        *,
        uri: Optional[str] = None,
        memory_id: Optional[int] = None,
        chunk_id: Optional[int] = None,
        start: Optional[int] = None,
        end: Optional[int] = None,
        max_chars: Optional[int] = None,
        domain: str = "core",
    ) -> Optional[Dict[str, Any]]:
        """
        Read a memory fragment by uri/memory/chunk.
        """
        async with self.session() as session:
            if chunk_id is not None:
                chunk_result = await session.execute(
                    select(MemoryChunk, Memory, Path)
                    .join(Memory, MemoryChunk.memory_id == Memory.id)
                    .join(Path, Path.memory_id == MemoryChunk.memory_id)
                    .where(MemoryChunk.id == chunk_id)
                    .where(Memory.deprecated == False)
                    .order_by(Path.priority.asc())
                )
                row = chunk_result.first()
                if not row:
                    return None

                chunk_obj, memory_obj, path_obj = row
                payload = {
                    "memory_id": memory_obj.id,
                    "chunk_id": chunk_obj.id,
                    "uri": f"{path_obj.domain}://{path_obj.path}",
                    "segment": chunk_obj.chunk_text,
                    "content": chunk_obj.chunk_text,
                    "char_range": [chunk_obj.char_start, chunk_obj.char_end],
                    "metadata": {
                        "domain": path_obj.domain,
                        "path": path_obj.path,
                        "priority": path_obj.priority,
                        "disclosure": path_obj.disclosure,
                        "updated_at": memory_obj.created_at.isoformat()
                        if memory_obj.created_at
                        else None,
                    },
                }
                await self._best_effort_reinforce_memory_access(session, [memory_obj.id])
                return payload

            target_memory: Optional[Memory] = None
            target_path: Optional[Path] = None

            if uri:
                if "://" in uri:
                    uri_domain, uri_path = uri.split("://", 1)
                else:
                    uri_domain, uri_path = domain, uri
                mem_result = await session.execute(
                    select(Memory, Path)
                    .join(Path, Memory.id == Path.memory_id)
                    .where(Path.domain == uri_domain)
                    .where(Path.path == uri_path)
                    .where(Memory.deprecated == False)
                )
                row = mem_result.first()
                if not row:
                    return None
                target_memory, target_path = row
            elif memory_id is not None:
                mem_result = await session.execute(
                    select(Memory, Path)
                    .join(Path, Memory.id == Path.memory_id)
                    .where(Memory.id == memory_id)
                    .where(Memory.deprecated == False)
                    .order_by(Path.priority.asc())
                )
                row = mem_result.first()
                if row:
                    target_memory, target_path = row
                else:
                    memory_result = await session.execute(
                        select(Memory).where(Memory.id == memory_id)
                    )
                    target_memory = memory_result.scalar_one_or_none()
                    if not target_memory:
                        return None
            else:
                return None

            full_content = target_memory.content or ""
            content_len = len(full_content)
            start_idx = max(0, int(start or 0))

            if end is not None:
                end_idx = min(content_len, max(start_idx, int(end)))
            elif max_chars is not None:
                end_idx = min(content_len, start_idx + max(1, int(max_chars)))
            else:
                end_idx = content_len

            segment = full_content[start_idx:end_idx]
            uri_value = (
                f"{target_path.domain}://{target_path.path}"
                if target_path is not None
                else None
            )
            payload = {
                "memory_id": target_memory.id,
                "chunk_id": None,
                "uri": uri_value,
                "segment": segment,
                "content": segment,
                "char_range": [start_idx, end_idx],
                "metadata": {
                    "domain": target_path.domain if target_path else None,
                    "path": target_path.path if target_path else None,
                    "priority": target_path.priority if target_path else None,
                    "disclosure": target_path.disclosure if target_path else None,
                    "updated_at": target_memory.created_at.isoformat()
                    if target_memory.created_at
                    else None,
                },
            }
            await self._best_effort_reinforce_memory_access(session, [target_memory.id])
            return payload

    async def get_index_status(self) -> Dict[str, Any]:
        """
        Return index capabilities, table counts, and current index metadata.
        """
        async with self.session() as session:
            memory_count_result = await session.execute(
                select(func.count()).select_from(Memory).where(Memory.deprecated == False)
            )
            chunk_count_result = await session.execute(
                select(func.count()).select_from(MemoryChunk)
            )
            vector_count_result = await session.execute(
                select(func.count()).select_from(MemoryChunkVec)
            )
            cache_count_result = await session.execute(
                select(func.count()).select_from(EmbeddingCache)
            )

            fts_exists_result = await session.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'memory_chunks_fts' "
                    "LIMIT 1"
                )
            )
            fts_exists = fts_exists_result.first() is not None
            self._fts_available = self._fts_available and fts_exists
            gist_fts_exists_result = await session.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'memory_gists_fts' "
                    "LIMIT 1"
                )
            )
            gist_fts_exists = gist_fts_exists_result.first() is not None
            self._gist_fts_available = self._gist_fts_available and gist_fts_exists

            meta_rows = await session.execute(select(IndexMeta))
            meta = {row.key: row.value for row in meta_rows.scalars().all()}

            return {
                "capabilities": {
                    "fts_available": self._fts_available and fts_exists,
                    "gist_fts_available": self._gist_fts_available and gist_fts_exists,
                    "vector_available": self._vector_available,
                    "embedding_backend": self._embedding_backend,
                    "embedding_model": self._embedding_model,
                    "embedding_dim": self._embedding_dim,
                    "embedding_provider_chain_enabled": self._embedding_provider_chain_enabled,
                    "embedding_provider_fail_open": self._embedding_provider_fail_open,
                    "embedding_provider_fallback": self._resolve_chain_fallback_backend(),
                    "embedding_provider_candidates": list(self._embedding_provider_candidates),
                    "sqlite_vec_enabled": self._sqlite_vec_enabled,
                    "sqlite_vec_read_ratio": int(self._sqlite_vec_read_ratio),
                    "sqlite_vec_status": str(self._sqlite_vec_capability.get("status", "disabled")),
                    "sqlite_vec_readiness": str(
                        self._sqlite_vec_capability.get("sqlite_vec_readiness", "hold")
                    ),
                    "sqlite_vec_diag_code": str(
                        self._sqlite_vec_capability.get("diag_code", "")
                    ),
                    "sqlite_vec_knn_ready": bool(self._sqlite_vec_knn_ready),
                    "sqlite_vec_knn_dim": int(self._sqlite_vec_knn_dim),
                    "semantic_vector_block_reason": self._semantic_vector_block_reason or None,
                    "semantic_vector_stored_dim": self._semantic_vector_stored_dim,
                    "semantic_vector_detected_dims": list(self._semantic_vector_detected_dims),
                    "vector_engine_requested": self._vector_engine_requested,
                    "vector_engine_effective": self._vector_engine_effective,
                    "runtime_write_wal_enabled": self._runtime_write_wal_enabled,
                    "runtime_write_journal_mode_requested": self._runtime_write_journal_mode_requested,
                    "runtime_write_journal_mode_effective": self._runtime_write_journal_mode_effective,
                    "runtime_write_wal_synchronous_requested": self._runtime_write_wal_synchronous_requested,
                    "runtime_write_wal_synchronous_effective": self._runtime_write_wal_synchronous_effective,
                    "runtime_write_busy_timeout_ms": int(
                        self._runtime_write_busy_timeout_effective_ms
                    ),
                    "runtime_write_wal_autocheckpoint": int(
                        self._runtime_write_wal_autocheckpoint_effective
                    ),
                    "runtime_write_pragma_status": self._runtime_write_pragma_status,
                    "runtime_write_pragma_error": self._runtime_write_pragma_error,
                    "reranker_enabled": self._reranker_enabled,
                    "reranker_model": self._reranker_model,
                    "rerank_weight": self._rerank_weight,
                },
                "counts": {
                    "active_memories": int(memory_count_result.scalar() or 0),
                    "memory_chunks": int(chunk_count_result.scalar() or 0),
                    "memory_chunks_vec": int(vector_count_result.scalar() or 0),
                    "embedding_cache": int(cache_count_result.scalar() or 0),
                },
                "meta": meta,
            }

    # =========================================================================
    # Recent Memories
    # =========================================================================

    async def get_recent_memories(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get the most recently created/updated non-deprecated memories
        that have at least one path (URI) pointing to them.

        Since updates create new Memory rows (old ones are deprecated),
        created_at on non-deprecated rows effectively means "last modified".

        Args:
            limit: Maximum number of results to return

        Returns:
            List of dicts with uri, priority, disclosure, created_at,
            ordered by created_at DESC (most recent first).
        """
        async with self.session() as session:
            # Subquery: find non-deprecated memory IDs that have paths
            # Group by memory_id to avoid duplicates when a memory has multiple paths
            result = await session.execute(
                select(Memory, Path)
                .join(Path, Memory.id == Path.memory_id)
                .where(Memory.deprecated == False)
                .order_by(Memory.created_at.desc())
            )

            seen_memory_ids = set()
            memories = []

            for memory, path_obj in result.all():
                if memory.id in seen_memory_ids:
                    continue
                seen_memory_ids.add(memory.id)

                memories.append(
                    {
                        "memory_id": memory.id,
                        "uri": f"{path_obj.domain}://{path_obj.path}",
                        "priority": path_obj.priority,
                        "disclosure": path_obj.disclosure,
                        "created_at": memory.created_at.isoformat()
                        if memory.created_at
                        else None,
                    }
                )

                if len(memories) >= limit:
                    break

            return memories

    # =========================================================================
    # Deprecated Memory Operations (for human's review)
    # =========================================================================

    async def get_memory_version(self, memory_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a specific memory version by ID (including deprecated ones).

        Args:
            memory_id: The memory ID

        Returns:
            Memory details
        """
        async with self.session() as session:
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            memory = result.scalar_one_or_none()

            if not memory:
                return None

            # Get paths pointing to this memory
            paths_result = await session.execute(
                select(Path).where(Path.memory_id == memory_id)
            )
            paths = [f"{p.domain}://{p.path}" for p in paths_result.scalars().all()]

            return {
                "memory_id": memory.id,
                "content": memory.content,
                # Importance/Disclosure removed
                "created_at": memory.created_at.isoformat()
                if memory.created_at
                else None,
                "deprecated": memory.deprecated,
                "migrated_to": memory.migrated_to,
                "paths": paths,
            }

    async def get_deprecated_memories(self) -> List[Dict[str, Any]]:
        """
        Get all deprecated memories for human's review.

        Returns:
            List of deprecated memories
        """
        async with self.session() as session:
            result = await session.execute(
                select(Memory)
                .where(Memory.deprecated == True)
                .order_by(Memory.created_at.desc())
            )

            memories = []
            for memory in result.scalars().all():
                memories.append(
                    {
                        "id": memory.id,
                        "content_snippet": memory.content[:200] + "..."
                        if len(memory.content) > 200
                        else memory.content,
                        "migrated_to": memory.migrated_to,
                        "created_at": memory.created_at.isoformat()
                        if memory.created_at
                        else None,
                    }
                )

            return memories

    async def _resolve_migration_chain(
        self, session: AsyncSession, start_id: int, max_hops: int = 50
    ) -> Optional[Dict[str, Any]]:
        """
        Follow the migrated_to chain from start_id to the final target.

        The final target is the memory at the end of the chain (migrated_to=NULL).
        Returns None if the chain is broken (missing memory) or too long (cycle).
        """
        current_id = start_id
        for _ in range(max_hops):
            result = await session.execute(
                select(Memory).where(Memory.id == current_id)
            )
            memory = result.scalar_one_or_none()
            if not memory:
                return None  # Broken chain
            if memory.migrated_to is None:
                # Final target reached
                paths_result = await session.execute(
                    select(Path).where(Path.memory_id == memory.id)
                )
                paths = [f"{p.domain}://{p.path}" for p in paths_result.scalars().all()]
                return {
                    "id": memory.id,
                    "content": memory.content,
                    "content_snippet": (
                        memory.content[:200] + "..."
                        if len(memory.content) > 200
                        else memory.content
                    ),
                    "created_at": memory.created_at.isoformat()
                    if memory.created_at
                    else None,
                    "deprecated": memory.deprecated,
                    "paths": paths,
                }
            current_id = memory.migrated_to
        return None  # Chain too long, likely a cycle

    async def get_all_orphan_memories(self) -> List[Dict[str, Any]]:
        """
        Get all orphan memories in the system.

        Two categories:
        - "deprecated": deprecated=True, created by update_memory. Has migrated_to.
        - "orphaned": deprecated=False but no paths point to it. Created by path deletion.

        For deprecated memories with migrated_to, resolves the migration chain to
        find the final target and its current paths.
        """
        async with self.session() as session:
            orphans = []

            # 1. Deprecated memories (from update_memory)
            deprecated_result = await session.execute(
                select(Memory)
                .where(Memory.deprecated == True)
                .order_by(Memory.created_at.desc())
            )

            for memory in deprecated_result.scalars().all():
                item = {
                    "id": memory.id,
                    "content_snippet": (
                        memory.content[:200] + "..."
                        if len(memory.content) > 200
                        else memory.content
                    ),
                    "created_at": memory.created_at.isoformat()
                    if memory.created_at
                    else None,
                    "deprecated": True,
                    "migrated_to": memory.migrated_to,
                    "category": "deprecated",
                    "migration_target": None,
                }

                if memory.migrated_to:
                    target = await self._resolve_migration_chain(
                        session, memory.migrated_to
                    )
                    if target:
                        item["migration_target"] = {
                            "id": target["id"],
                            "paths": target["paths"],
                            "content_snippet": target["content_snippet"],
                        }

                orphans.append(item)

            # 2. Truly orphaned memories (non-deprecated, no paths)
            orphaned_result = await session.execute(
                select(Memory)
                .outerjoin(Path, Memory.id == Path.memory_id)
                .where(Memory.deprecated == False)
                .where(Path.memory_id.is_(None))
                .order_by(Memory.created_at.desc())
            )

            for memory in orphaned_result.scalars().all():
                orphans.append(
                    {
                        "id": memory.id,
                        "content_snippet": (
                            memory.content[:200] + "..."
                            if len(memory.content) > 200
                            else memory.content
                        ),
                        "created_at": memory.created_at.isoformat()
                        if memory.created_at
                        else None,
                        "deprecated": False,
                        "migrated_to": memory.migrated_to,
                        "category": "orphaned",
                        "migration_target": None,
                    }
                )

            return orphans

    async def get_orphan_detail(self, memory_id: int) -> Optional[Dict[str, Any]]:
        """
        Get full detail of an orphan memory for content viewing and diff comparison.

        Returns full content of both the orphan and its final migration target
        (if applicable).
        """
        async with self.session() as session:
            result = await session.execute(select(Memory).where(Memory.id == memory_id))
            memory = result.scalar_one_or_none()
            if not memory:
                return None

            # Determine category
            if memory.deprecated:
                category = "deprecated"
            else:
                paths_count_result = await session.execute(
                    select(func.count())
                    .select_from(Path)
                    .where(Path.memory_id == memory_id)
                )
                category = "orphaned" if paths_count_result.scalar() == 0 else "active"

            detail = {
                "id": memory.id,
                "content": memory.content,
                "created_at": memory.created_at.isoformat()
                if memory.created_at
                else None,
                "deprecated": memory.deprecated,
                "migrated_to": memory.migrated_to,
                "category": category,
                "migration_target": None,
            }

            # Resolve migration chain for diff comparison
            if memory.migrated_to:
                target = await self._resolve_migration_chain(
                    session, memory.migrated_to
                )
                if target:
                    detail["migration_target"] = {
                        "id": target["id"],
                        "content": target["content"],
                        "paths": target["paths"],
                        "created_at": target["created_at"],
                    }

            return detail

    async def permanently_delete_memory(
        self,
        memory_id: int,
        *,
        require_orphan: bool = False,
        expected_state_hash: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Permanently delete a memory (human only).

        Before deletion, repairs the version chain: if any other memory
        has migrated_to pointing to this one, it will be updated to skip
        over and point to this memory's own migrated_to target.

        Example: A(migrated_to=B) → B(migrated_to=C) → C
                 Delete B → A(migrated_to=C) → C

        Args:
            memory_id: Memory ID to delete
            require_orphan: If True, verify the memory is still an orphan
                (deprecated or path-less) within the same transaction.
                Raises PermissionError if the memory has active paths.
            expected_state_hash: Optional stale-check hash generated from
                get_vitality_cleanup_candidates. When provided, deletion only
                proceeds if the current memory state hash matches.

        Returns:
            Deletion info

        Raises:
            ValueError: Memory ID not found
            PermissionError: Memory has active paths (only when require_orphan=True)
            RuntimeError: Candidate state hash changed (when expected_state_hash is provided)
        """
        async with self.session() as session:
            # 1. Get the memory being deleted
            target_result = await session.execute(
                select(
                    Memory.deprecated,
                    Memory.migrated_to,
                    Memory.vitality_score,
                    Memory.access_count,
                ).where(Memory.id == memory_id)
            )
            target_row = target_result.first()
            if not target_row:
                raise ValueError(f"Memory ID {memory_id} not found")

            deprecated, successor_id, vitality_score, access_count = target_row

            expected_hash_value = (expected_state_hash or "").strip()
            path_count: Optional[int] = None
            if require_orphan or expected_hash_value:
                path_count_result = await session.execute(
                    select(func.count())
                    .select_from(Path)
                    .where(Path.memory_id == memory_id)
                )
                path_count = int(path_count_result.scalar() or 0)

            if expected_hash_value:
                current_hash = self._build_vitality_state_hash(
                    memory_id=memory_id,
                    vitality_score=max(0.0, float(vitality_score or 0.0)),
                    access_count=max(0, int(access_count or 0)),
                    path_count=max(0, int(path_count or 0)),
                    deprecated=bool(deprecated),
                )
                if current_hash != expected_hash_value:
                    raise RuntimeError("stale_state")

            # 2. If caller requires orphan safety, verify within this transaction
            if require_orphan and not deprecated:
                if int(path_count or 0) > 0:
                    raise PermissionError(
                        f"Memory {memory_id} is no longer an orphan "
                        f"(has {int(path_count or 0)} active path(s)). Deletion aborted."
                    )

            # 3. Repair the chain: any memory pointing to the deleted node
            #    should now point to the deleted node's successor
            await session.execute(
                update(Memory)
                .where(Memory.migrated_to == memory_id)
                .values(migrated_to=successor_id)
            )

            # 4. Remove any paths pointing to this memory
            await session.execute(delete(Path).where(Path.memory_id == memory_id))

            # 5. Delete retrieval artifacts before removing the owning memory row.
            await self._clear_memory_index(session, memory_id)

            # 6. Delete the memory
            result = await session.execute(delete(Memory).where(Memory.id == memory_id))

            if result.rowcount == 0:
                raise ValueError(f"Memory ID {memory_id} not found")

            return {"deleted_memory_id": memory_id, "chain_repaired_to": successor_id}


# =============================================================================
# Global Singleton
# =============================================================================

_sqlite_client: Optional[SQLiteClient] = None


def get_sqlite_client() -> SQLiteClient:
    """Get the global SQLiteClient instance."""
    global _sqlite_client
    if _sqlite_client is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError(
                "DATABASE_URL environment variable is not set. Please check your .env file."
            )
        _sqlite_client = SQLiteClient(database_url)
    return _sqlite_client


async def close_sqlite_client():
    """Close the global SQLiteClient connection."""
    global _sqlite_client
    if _sqlite_client:
        await _sqlite_client.close()
        _sqlite_client = None
