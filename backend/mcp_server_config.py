import os
from typing import List

from env_utils import (
    env_bool as _env_bool,
    env_csv as _env_csv,
    env_int as _env_int,
    utc_now_naive as _utc_now_naive,
)

READ_ONLY_DOMAINS = {"system"}
VALID_DOMAINS = list(
    dict.fromkeys(
        [
            d.strip().lower()
            for d in os.getenv("VALID_DOMAINS", "core,writer,game,notes,system").split(",")
            if d.strip()
        ]
        + sorted(READ_ONLY_DOMAINS)
    )
)
DEFAULT_DOMAIN = "core"
CORE_MEMORY_URIS = [
    uri.strip()
    for uri in os.getenv("CORE_MEMORY_URIS", "").split(",")
    if uri.strip()
]

ALLOWED_SEARCH_MODES = {"keyword", "semantic", "hybrid"}
DEFAULT_SEARCH_MODE = os.getenv("SEARCH_DEFAULT_MODE", "keyword").strip().lower()
if DEFAULT_SEARCH_MODE not in ALLOWED_SEARCH_MODES:
    DEFAULT_SEARCH_MODE = "keyword"

DEFAULT_SEARCH_MAX_RESULTS = _env_int("SEARCH_DEFAULT_MAX_RESULTS", 10, minimum=1)
DEFAULT_SEARCH_CANDIDATE_MULTIPLIER = _env_int(
    "SEARCH_DEFAULT_CANDIDATE_MULTIPLIER", 4, minimum=1
)
SEARCH_HARD_MAX_RESULTS = _env_int("SEARCH_HARD_MAX_RESULTS", 100, minimum=1)
SEARCH_HARD_MAX_CANDIDATE_MULTIPLIER = _env_int(
    "SEARCH_HARD_MAX_CANDIDATE_MULTIPLIER", 50, minimum=1
)
READ_CHUNK_SIZE = _env_int("RETRIEVAL_CHUNK_SIZE", 1000, minimum=1)
READ_CHUNK_OVERLAP = _env_int("RETRIEVAL_CHUNK_OVERLAP", 200, minimum=0)
ENABLE_SESSION_FIRST_SEARCH = _env_bool("RUNTIME_SESSION_FIRST_SEARCH", True)
ENABLE_WRITE_LANE_QUEUE = _env_bool("RUNTIME_WRITE_LANE_QUEUE", True)
ENABLE_INDEX_WORKER = _env_bool("RUNTIME_INDEX_WORKER_ENABLED", True)
DEFER_INDEX_ON_WRITE = _env_bool("RUNTIME_INDEX_DEFER_ON_WRITE", True)
AUTO_FLUSH_ENABLED = _env_bool("RUNTIME_AUTO_FLUSH_ENABLED", True)
AUTO_FLUSH_PRIORITY = _env_int("RUNTIME_AUTO_FLUSH_PRIORITY", 2, minimum=0)
AUTO_FLUSH_SUMMARY_LINES = _env_int("RUNTIME_AUTO_FLUSH_SUMMARY_LINES", 12, minimum=3)
AUTO_FLUSH_PARENT_URI = (
    os.getenv("RUNTIME_AUTO_FLUSH_PARENT_URI", "notes://").strip() or "notes://"
)
INDEX_LITE_ENABLED = _env_bool("INDEX_LITE_ENABLED", False)
AUDIT_VERBOSE = _env_bool("AUDIT_VERBOSE", False)
INTENT_LLM_ENABLED = _env_bool("INTENT_LLM_ENABLED", False)
IMPORT_LEARN_AUDIT_META_KEY = "audit.import_learn.summary.v1"


def _auto_learn_explicit_enabled() -> bool:
    return _env_bool("AUTO_LEARN_EXPLICIT_ENABLED", False)


def _auto_learn_require_reason() -> bool:
    return _env_bool("AUTO_LEARN_REQUIRE_REASON", True)


def _auto_learn_allowed_domains() -> List[str]:
    domains = []
    for entry in _env_csv("AUTO_LEARN_ALLOWED_DOMAINS", "notes"):
        value = entry.strip().lower()
        if value and value not in domains:
            domains.append(value)
    return domains or ["notes"]
