"""Real A/B/C/D benchmark runner for retrieval profiles.

This module complements the deterministic Phase4/6 baseline artifacts by
executing real retrieval over locally materialized benchmark corpora.

Current real-run scope:
- Profile A: keyword only
- Profile B: hybrid + hash embedding
- Profile C: hybrid + API embedding (no reranker)
- Profile D: hybrid + API embedding + reranker

Datasets in this runner focus on corpora that can be materialized in-repo:
- squad_v2_dev
- beir_nfcorpus
"""

from __future__ import annotations

import io
import json
import math
import os
import random
import shutil
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set
from unittest.mock import patch

import requests
from sqlalchemy import select, text

BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db.sqlite_client import Path as MemoryPath, SQLiteClient

from .common import BENCHMARK_DIR, DATASETS_DIR

REAL_PROFILE_JSON_ARTIFACT = BENCHMARK_DIR / "profile_abcd_real_metrics.json"
REAL_PROFILE_MARKDOWN_ARTIFACT = BENCHMARK_DIR / "benchmark_results_profile_abcd_real.md"
REAL_PROFILE_CD_MARKDOWN_ARTIFACT = BENCHMARK_DIR / "benchmark_results_profile_cd_real.md"
REAL_PROFILE_FACTUAL_COMPARE_MARKDOWN_ARTIFACT = (
    BENCHMARK_DIR / "benchmark_results_profile_factual_pool_compare.md"
)
REAL_PROFILE_WORKDIR = BENCHMARK_DIR / ".real_profile_cache"
REAL_PROFILE_WORKDIR_ENV = "BENCHMARK_REAL_PROFILE_WORKDIR"
REAL_PROFILE_METRIC_TOP_K = 10
REAL_PROFILE_DEFAULT_MAX_RESULTS = 10
REAL_PROFILE_DEFAULT_CANDIDATE_MULTIPLIER = 8
REAL_PROFILE_RESULT_COLLAPSE = "doc_id_dedup"
REAL_PROFILE_DOC_PRIORITY = 10
REAL_PROFILE_NAMESPACE_PRIORITY = 999
REAL_PROFILE_ENTRYPOINT_SQLITE_CLIENT = "sqlite_client"
REAL_PROFILE_ENTRYPOINT_MCP_SEARCH_MEMORY = "mcp_search_memory"
REAL_PROFILE_DEFAULT_ENTRYPOINT = REAL_PROFILE_ENTRYPOINT_SQLITE_CLIENT
REAL_PROFILE_SUPPORTED_ENTRYPOINTS = {
    REAL_PROFILE_ENTRYPOINT_SQLITE_CLIENT,
    REAL_PROFILE_ENTRYPOINT_MCP_SEARCH_MEMORY,
}

PROFILE_D_INVALID_GATE_REASONS = {
    "embedding_fallback_hash",
    "embedding_request_failed",
    "reranker_request_failed",
}
PROFILE_D_REQUEST_FAILED_REASONS = {
    "embedding_request_failed",
    "reranker_request_failed",
}

DATASET_LABELS = {
    "squad_v2_dev": "SQuAD v2 Dev",
    "beir_nfcorpus": "BEIR NFCorpus",
}

REAL_DATASET_DEFAULTS = ("squad_v2_dev", "beir_nfcorpus")
REAL_RANDOM_SEED = 20260219

PHASE6_GATE_MODE_ENV = "BENCHMARK_PHASE6_GATE_MODE"
PHASE6_INVALID_RATE_THRESHOLD_ENV = "BENCHMARK_PHASE6_INVALID_RATE_THRESHOLD"
PHASE6_GATE_MODE_STRICT = "strict"
PHASE6_GATE_MODE_API_TOLERANT = "api_tolerant"
PHASE6_SUPPORTED_GATE_MODES = {
    PHASE6_GATE_MODE_STRICT,
    PHASE6_GATE_MODE_API_TOLERANT,
}
PHASE6_DEFAULT_INVALID_RATE_THRESHOLD = 0.05


@dataclass(frozen=True)
class QueryCase:
    query_id: str
    query: str
    relevant_doc_ids: Set[str]


@dataclass(frozen=True)
class DatasetBundle:
    key: str
    label: str
    domain: str
    queries: List[QueryCase]
    docs: List[tuple[str, str]]
    sample_bucket_size: int
    query_count_raw: int
    avg_relevant_per_query: float
    max_relevant_per_query: int
    multi_relevant_query_rate: float
    path_prefix: str = ""


@dataclass(frozen=True)
class ProfileConfig:
    key: str
    mode: str
    env_overrides: Mapping[str, str]
    reuse_data_from: Optional[str] = None


PROFILE_CONFIGS: Sequence[ProfileConfig] = (
    ProfileConfig(
        key="profile_a",
        mode="keyword",
        env_overrides={
            "RETRIEVAL_EMBEDDING_BACKEND": "none",
            "RETRIEVAL_RERANKER_ENABLED": "false",
        },
    ),
    ProfileConfig(
        key="profile_b",
        mode="hybrid",
        env_overrides={
            "RETRIEVAL_EMBEDDING_BACKEND": "hash",
            "RETRIEVAL_EMBEDDING_DIM": "64",
            "RETRIEVAL_RERANKER_ENABLED": "false",
        },
    ),
    ProfileConfig(
        key="profile_c",
        mode="hybrid",
        env_overrides={
            # Dev benchmark default: force API path for C/D real-run validation.
            # Release/deploy templates still keep router defaults and should be
            # re-validated before final delivery.
            "RETRIEVAL_EMBEDDING_BACKEND": "api",
            "RETRIEVAL_RERANKER_ENABLED": "false",
        },
    ),
    ProfileConfig(
        key="profile_d",
        mode="hybrid",
        env_overrides={
            "RETRIEVAL_EMBEDDING_BACKEND": "api",
            "RETRIEVAL_RERANKER_ENABLED": "true",
        },
        reuse_data_from="profile_c",
    ),
)
PROFILE_CONFIG_BY_KEY: Dict[str, ProfileConfig] = {config.key: config for config in PROFILE_CONFIGS}
PROFILE_KEY_ALIASES = {
    "a": "profile_a",
    "b": "profile_b",
    "c": "profile_c",
    "d": "profile_d",
    "profile_a": "profile_a",
    "profile_b": "profile_b",
    "profile_c": "profile_c",
    "profile_d": "profile_d",
}
REAL_PROFILE_DEFAULT_KEYS: tuple[str, ...] = tuple(config.key for config in PROFILE_CONFIGS)

SOTA_BENCHMARK_REFERENCES_2026_02 = {
    "bm25_avg_ndcg10": 0.43,
    "dense_avg_ndcg10_min": 0.48,
    "dense_avg_ndcg10_max": 0.52,
    "hybrid_avg_ndcg10_min": 0.54,
    "hybrid_avg_ndcg10_max": 0.56,
    "hybrid_rerank_avg_ndcg10_min": 0.58,
    "hybrid_rerank_avg_ndcg10_max": 0.62,
    "sota_avg_ndcg10_floor": 0.62,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sanitize_report_path(raw_path: Path | str) -> str:
    path_obj = Path(raw_path).expanduser()
    if not path_obj.is_absolute():
        return path_obj.as_posix()
    try:
        return path_obj.resolve(strict=False).relative_to(BACKEND_ROOT).as_posix()
    except Exception:
        pass
    try:
        return path_obj.resolve(strict=False).relative_to(BACKEND_ROOT.parent).as_posix()
    except Exception:
        pass
    try:
        return "<tmp>/" + path_obj.resolve(strict=False).relative_to(Path("/tmp")).as_posix()
    except Exception:
        pass
    return f"<abs>/{path_obj.name}"


def resolve_real_profile_workdir(workdir: Optional[Path] = None) -> Path:
    if workdir is not None:
        return Path(workdir).expanduser()
    env_value = str(os.getenv(REAL_PROFILE_WORKDIR_ENV, "")).strip()
    if env_value:
        return Path(env_value).expanduser()
    REAL_PROFILE_WORKDIR.mkdir(parents=True, exist_ok=True)
    return Path(
        tempfile.mkdtemp(
            prefix="run-",
            dir=str(REAL_PROFILE_WORKDIR),
        )
    )


def _slugify(value: str) -> str:
    lowered = "".join(ch.lower() if ch.isalnum() else "_" for ch in value.strip())
    compact = "_".join(chunk for chunk in lowered.split("_") if chunk)
    return compact or "untitled"


def _sample_bucket_size(sample_size: int) -> int:
    if sample_size <= 100:
        return 100
    if sample_size <= 200:
        return 200
    return 500


def _load_jsonl_rows(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            rows.append(json.loads(raw))
    return rows


def _squad_raw_path() -> Path:
    return DATASETS_DIR / "raw" / "squad_v2_dev" / "squad_dev_v2.json"


def _beir_raw_dir(dataset_key: str) -> Path:
    return DATASETS_DIR / "raw" / dataset_key


def _dataset_source_url(dataset_key: str) -> str:
    manifest_path = DATASETS_DIR / "manifests" / f"{dataset_key}.json"
    if not manifest_path.exists():
        raise RuntimeError(f"missing dataset manifest: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_url = str(payload.get("source_url") or "").strip()
    if not source_url:
        raise RuntimeError(f"manifest missing source_url: {manifest_path}")
    return source_url


def _ensure_beir_corpus_jsonl(dataset_key: str) -> Path:
    raw_dir = _beir_raw_dir(dataset_key)
    raw_dir.mkdir(parents=True, exist_ok=True)
    corpus_path = raw_dir / "corpus.jsonl"
    if corpus_path.exists():
        return corpus_path

    source_url = _dataset_source_url(dataset_key)
    response = requests.get(source_url, timeout=300)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content), "r") as archive:
        member_name = next(
            (
                name
                for name in archive.namelist()
                if name.endswith("/corpus.jsonl") or name == "corpus.jsonl"
            ),
            None,
        )
        if member_name is None:
            raise RuntimeError(f"{dataset_key}: corpus.jsonl not found in source zip")
        with archive.open(member_name, "r") as source, corpus_path.open("wb") as target:
            shutil.copyfileobj(source, target)

    return corpus_path


def _load_squad_corpus() -> Dict[str, str]:
    path = _squad_raw_path()
    if not path.exists():
        raise RuntimeError(f"missing SQuAD raw dataset: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))

    corpus: Dict[str, str] = {}
    for article in payload.get("data", []):
        title = str(article.get("title") or "untitled")
        title_slug = _slugify(title)
        for paragraph_idx, paragraph in enumerate(article.get("paragraphs", [])):
            context = str(paragraph.get("context") or "").strip()
            if not context:
                continue
            doc_id = f"squad_v2:{title_slug}:{paragraph_idx}"
            corpus[doc_id] = context
    return corpus


def _load_beir_corpus(dataset_key: str) -> Dict[str, str]:
    corpus_path = _ensure_beir_corpus_jsonl(dataset_key)
    corpus: Dict[str, str] = {}
    for row in _load_jsonl_rows(corpus_path):
        doc_id = str(row.get("_id") or row.get("id") or "").strip()
        if not doc_id:
            continue
        title = str(row.get("title") or "").strip()
        text = str(row.get("text") or "").strip()
        combined = text if not title else f"{title}\n{text}".strip()
        if combined:
            corpus[doc_id] = combined
    if not corpus:
        raise RuntimeError(f"{dataset_key}: loaded empty corpus from {corpus_path}")
    return corpus


def _load_dataset_corpus(dataset_key: str) -> Dict[str, str]:
    if dataset_key == "squad_v2_dev":
        return _load_squad_corpus()
    if dataset_key == "beir_nfcorpus":
        return _load_beir_corpus(dataset_key)
    raise RuntimeError(f"unsupported real benchmark dataset: {dataset_key}")


def _load_sample_queries(dataset_key: str, sample_size: int) -> tuple[List[Dict[str, Any]], int]:
    bucket_size = _sample_bucket_size(sample_size)
    sample_path = DATASETS_DIR / "processed" / f"{dataset_key}_sample_{bucket_size}.jsonl"
    if not sample_path.exists():
        raise RuntimeError(f"missing sample file: {sample_path}")
    rows = _load_jsonl_rows(sample_path)
    if not rows:
        raise RuntimeError(f"sample file has no rows: {sample_path}")
    return rows[:sample_size], bucket_size


def _build_query_cases(
    *,
    rows: Sequence[Mapping[str, Any]],
    corpus: Mapping[str, str],
    first_relevant_only: bool,
) -> List[QueryCase]:
    cases: List[QueryCase] = []
    for row in rows:
        query_id = str(row.get("id") or "").strip()
        query = str(row.get("query") or "").strip()
        if not query_id or not query:
            continue
        relevant_raw = row.get("relevant_uris_or_doc_ids")
        if not isinstance(relevant_raw, list):
            continue
        relevant_available = [str(item) for item in relevant_raw if str(item) in corpus]
        if first_relevant_only and relevant_available:
            relevant_available = relevant_available[:1]
        relevant_set = {item for item in relevant_available if item}
        if not relevant_set:
            continue
        cases.append(QueryCase(query_id=query_id, query=query, relevant_doc_ids=relevant_set))
    return cases


def _dataset_relevance_stats(queries: Sequence[QueryCase]) -> Dict[str, float | int]:
    if not queries:
        return {
            "avg_relevant_per_query": 0.0,
            "max_relevant_per_query": 0,
            "multi_relevant_query_rate": 0.0,
        }

    relevant_counts = [len(case.relevant_doc_ids) for case in queries]
    multi_relevant = sum(1 for count in relevant_counts if count > 1)
    query_count = len(relevant_counts)
    return {
        "avg_relevant_per_query": _round_metric(
            sum(relevant_counts) / float(query_count)
        ),
        "max_relevant_per_query": max(relevant_counts),
        "multi_relevant_query_rate": _round_metric(
            multi_relevant / float(query_count)
        ),
    }


def _select_doc_ids(
    *,
    corpus: Mapping[str, str],
    queries: Sequence[QueryCase],
    extra_distractors: int,
    seed: int,
) -> List[str]:
    required: Set[str] = set()
    for case in queries:
        required.update(case.relevant_doc_ids)

    pool = [doc_id for doc_id in corpus if doc_id not in required]
    rng = random.Random(seed)
    rng.shuffle(pool)
    distractors = pool[: max(0, int(extra_distractors))]
    selected = sorted(required.union(distractors))
    return selected


def build_dataset_bundle(
    *,
    dataset_key: str,
    sample_size: int,
    first_relevant_only: bool,
    extra_distractors: int,
    seed: int,
) -> DatasetBundle:
    corpus = _load_dataset_corpus(dataset_key)
    rows, bucket_size = _load_sample_queries(dataset_key, sample_size)
    queries = _build_query_cases(
        rows=rows,
        corpus=corpus,
        first_relevant_only=first_relevant_only,
    )
    if not queries:
        raise RuntimeError(
            f"{dataset_key}: no valid query cases after intersecting with corpus"
        )

    selected_doc_ids = _select_doc_ids(
        corpus=corpus,
        queries=queries,
        extra_distractors=extra_distractors,
        seed=seed,
    )
    docs = [(doc_id, corpus[doc_id]) for doc_id in selected_doc_ids]
    if not docs:
        raise RuntimeError(f"{dataset_key}: selected corpus docs is empty")

    return DatasetBundle(
        key=dataset_key,
        label=DATASET_LABELS.get(dataset_key, dataset_key),
        domain="notes",
        queries=queries,
        docs=docs,
        sample_bucket_size=bucket_size,
        query_count_raw=len(rows),
        **_dataset_relevance_stats(queries),
        path_prefix=f"benchmark/{dataset_key}",
    )


def compute_percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])

    p = max(0.0, min(1.0, float(percentile)))
    sorted_values = sorted(float(v) for v in values)
    pos = p * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    fraction = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * fraction


def compute_retrieval_metrics(
    *,
    retrieved_doc_ids: Sequence[str],
    relevant_doc_ids: Set[str],
    k: int = 10,
) -> Dict[str, float]:
    if not relevant_doc_ids:
        return {
            "hr_at_5": 0.0,
            "hr_at_10": 0.0,
            "mrr": 0.0,
            "ndcg_at_10": 0.0,
            "recall_at_10": 0.0,
        }

    unique_retrieved: List[str] = []
    seen: Set[str] = set()
    for doc_id in retrieved_doc_ids:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        unique_retrieved.append(doc_id)

    topk = list(unique_retrieved[: max(1, int(k))])
    top5 = topk[:5]
    top10 = topk[:10]

    hr_at_5 = 1.0 if any(doc_id in relevant_doc_ids for doc_id in top5) else 0.0
    hr_at_10 = 1.0 if any(doc_id in relevant_doc_ids for doc_id in top10) else 0.0

    first_rank: Optional[int] = None
    for idx, doc_id in enumerate(top10, start=1):
        if doc_id in relevant_doc_ids:
            first_rank = idx
            break
    mrr = 0.0 if first_rank is None else (1.0 / float(first_rank))

    hit_count = sum(1 for doc_id in top10 if doc_id in relevant_doc_ids)
    recall_at_10 = hit_count / float(len(relevant_doc_ids))

    gains = [1.0 if doc_id in relevant_doc_ids else 0.0 for doc_id in top10]
    dcg = sum(gain / math.log2(idx + 2.0) for idx, gain in enumerate(gains))
    ideal_hits = min(len(relevant_doc_ids), len(top10))
    idcg = sum(1.0 / math.log2(idx + 2.0) for idx in range(ideal_hits))
    ndcg_at_10 = 0.0 if idcg == 0 else (dcg / idcg)

    return {
        "hr_at_5": hr_at_5,
        "hr_at_10": hr_at_10,
        "mrr": mrr,
        "ndcg_at_10": ndcg_at_10,
        "recall_at_10": recall_at_10,
    }

@contextmanager
def patched_env(overrides: Mapping[str, str]):
    previous: Dict[str, Optional[str]] = {}
    for key, value in overrides.items():
        previous[key] = os.environ.get(key)
        os.environ[key] = str(value)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


def _reset_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()


async def _ensure_bundle_path_prefix(client: SQLiteClient, bundle: DatasetBundle) -> None:
    path_prefix = str(bundle.path_prefix or "").strip("/")
    if not path_prefix:
        return

    current_path = ""
    for segment in [part for part in path_prefix.split("/") if part]:
        current_path = f"{current_path}/{segment}" if current_path else segment
        async with client.session() as session:
            existing = await session.execute(
                select(MemoryPath)
                .where(MemoryPath.domain == bundle.domain)
                .where(MemoryPath.path == current_path)
            )
            if existing.scalar_one_or_none() is not None:
                continue
        parent_path = current_path.rsplit("/", 1)[0] if "/" in current_path else ""
        await client.create_memory(
            parent_path=parent_path,
            content="benchmark namespace container",
            priority=REAL_PROFILE_NAMESPACE_PRIORITY,
            title=segment,
            domain=bundle.domain,
            index_now=False,
        )


async def _populate_bundle_docs(
    client: SQLiteClient,
    bundle: DatasetBundle,
) -> Dict[int, str]:
    memory_to_doc: Dict[int, str] = {}
    parent_path = str(bundle.path_prefix or "").strip("/")
    await _ensure_bundle_path_prefix(client, bundle)
    for idx, (doc_id, content) in enumerate(bundle.docs):
        if not content.strip():
            continue
        result = await client.create_memory(
            parent_path=parent_path,
            content=content,
            priority=REAL_PROFILE_DOC_PRIORITY,
            title=f"{bundle.key}_{idx:05d}",
            domain=bundle.domain,
            index_now=True,
        )
        memory_id = int(result["id"])
        memory_to_doc[memory_id] = doc_id
    if not memory_to_doc:
        raise RuntimeError(f"{bundle.key}: no documents indexed for benchmark")
    return memory_to_doc


def _round_metric(value: float) -> float:
    return round(float(value), 6)


async def _reset_domain_retrieval_state(
    client: Any,
    domain: str,
    path_prefix: Optional[str] = None,
) -> bool:
    session_factory = getattr(client, "session", None)
    if not callable(session_factory):
        return False

    params: Dict[str, Any] = {"domain": str(domain)}
    path_filter = ""
    if path_prefix:
        prefix_value = str(path_prefix).strip("/")
        path_filter = " AND (path = :path_prefix_exact OR path LIKE :path_prefix_subtree)"
        params["path_prefix_exact"] = prefix_value
        params["path_prefix_subtree"] = f"{prefix_value}/%"

    async with client.session() as session:
        await session.execute(
            text(
                "UPDATE memories "
                "SET access_count = 0, last_accessed_at = NULL, vitality_score = 1.0 "
                "WHERE id IN ("
                "  SELECT DISTINCT memory_id "
                "  FROM paths "
                f"  WHERE domain = :domain{path_filter}"
                ")"
            ),
            params,
        )
        await session.commit()
    return True


def _normalize_entrypoint(entrypoint: str) -> str:
    value = str(entrypoint).strip().lower()
    if value not in REAL_PROFILE_SUPPORTED_ENTRYPOINTS:
        supported = ", ".join(sorted(REAL_PROFILE_SUPPORTED_ENTRYPOINTS))
        raise ValueError(f"unsupported entrypoint: {entrypoint!r} (supported: {supported})")
    return value


def _normalize_profile_keys(profile_keys: Sequence[str] | None) -> List[str]:
    if not profile_keys:
        return list(REAL_PROFILE_DEFAULT_KEYS)

    normalized: List[str] = []
    for raw_key in profile_keys:
        key = PROFILE_KEY_ALIASES.get(str(raw_key).strip().lower())
        if key is None:
            supported = ", ".join(sorted(PROFILE_KEY_ALIASES))
            raise ValueError(
                f"unsupported profile key: {raw_key!r} (supported: {supported})"
            )
        if key not in normalized:
            normalized.append(key)
    return normalized


def _resolve_execution_profile_keys(selected_profile_keys: Sequence[str]) -> List[str]:
    selected = _normalize_profile_keys(selected_profile_keys)
    expanded = set(selected)
    if "profile_d" in expanded:
        expanded.add("profile_c")
    return [
        config.key
        for config in PROFILE_CONFIGS
        if config.key in expanded
    ]


def _collapse_ranked_doc_ids(doc_ids: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    collapsed: List[str] = []
    for doc_id in doc_ids:
        normalized = str(doc_id or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        collapsed.append(normalized)
    return collapsed


def _path_prefix_matches(path: str, path_prefix: str) -> bool:
    prefix_value = str(path_prefix or "").strip().strip("/")
    path_value = str(path or "").strip().strip("/")
    if not prefix_value:
        return True
    return path_value == prefix_value or path_value.startswith(f"{prefix_value}/")


def _entrypoint_source_variant(entrypoint: str) -> str:
    if entrypoint == REAL_PROFILE_ENTRYPOINT_MCP_SEARCH_MEMORY:
        return "mcp_server.search_memory"
    return "sqlite_client.search_advanced"


async def _noop_async(*_args: Any, **_kwargs: Any) -> None:
    return None


async def _run_query_via_sqlite_client(
    *,
    client: SQLiteClient,
    case: QueryCase,
    bundle: DatasetBundle,
    profile_mode: str,
    max_results: int,
    candidate_multiplier: int,
) -> Dict[str, Any]:
    filters: Dict[str, Any] = {"domain": bundle.domain}
    if bundle.path_prefix:
        filters["path_prefix"] = bundle.path_prefix
    filters["max_priority"] = REAL_PROFILE_DOC_PRIORITY
    payload = await client.search_advanced(
        query=case.query,
        mode=profile_mode,
        max_results=max_results,
        candidate_multiplier=candidate_multiplier,
        filters=filters,
    )
    if isinstance(payload, dict):
        payload.setdefault("backend_method", "sqlite_client.search_advanced")
    return payload


async def _run_query_via_mcp_search_memory(
    *,
    client: SQLiteClient,
    case: QueryCase,
    bundle: DatasetBundle,
    profile_mode: str,
    max_results: int,
    candidate_multiplier: int,
) -> Dict[str, Any]:
    import mcp_server

    filters: Dict[str, Any] = {"domain": bundle.domain}
    if bundle.path_prefix:
        filters["path_prefix"] = bundle.path_prefix
    filters["max_priority"] = REAL_PROFILE_DOC_PRIORITY

    with (
        patch.object(mcp_server, "get_sqlite_client", lambda: client),
        patch.object(mcp_server, "_record_session_hit", _noop_async),
        patch.object(mcp_server, "_record_flush_event", _noop_async),
    ):
        raw = await mcp_server.search_memory(
            query=case.query,
            mode=profile_mode,
            max_results=max_results,
            candidate_multiplier=candidate_multiplier,
            include_session=False,
            filters=filters,
        )
    payload = json.loads(raw)
    if isinstance(payload, dict):
        payload.setdefault("backend_method", "mcp_server.search_memory")
    return payload


def _normalize_phase6_gate_mode(raw_mode: str) -> str:
    mode = str(raw_mode).strip().lower()
    if mode not in PHASE6_SUPPORTED_GATE_MODES:
        supported = ", ".join(sorted(PHASE6_SUPPORTED_GATE_MODES))
        raise ValueError(f"unsupported phase6 gate mode: {raw_mode!r} (supported: {supported})")
    return mode


def resolve_phase6_gate_config() -> Dict[str, Any]:
    mode = _normalize_phase6_gate_mode(
        os.getenv(PHASE6_GATE_MODE_ENV, PHASE6_GATE_MODE_STRICT)
    )
    raw_threshold = os.getenv(
        PHASE6_INVALID_RATE_THRESHOLD_ENV,
        str(PHASE6_DEFAULT_INVALID_RATE_THRESHOLD),
    )
    try:
        invalid_rate_threshold = float(raw_threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"invalid {PHASE6_INVALID_RATE_THRESHOLD_ENV}: {raw_threshold!r}"
        ) from exc
    if invalid_rate_threshold < 0.0 or invalid_rate_threshold > 1.0:
        raise ValueError(
            f"invalid {PHASE6_INVALID_RATE_THRESHOLD_ENV}: {invalid_rate_threshold!r} "
            "(expected between 0 and 1)"
        )
    return {
        "mode": mode,
        "invalid_rate_threshold": _round_metric(invalid_rate_threshold),
    }


def _coerce_reason_count_map(raw: Any) -> Dict[str, int]:
    if not isinstance(raw, Mapping):
        return {}
    parsed: Dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        try:
            parsed_value = int(value)
        except (TypeError, ValueError):
            continue
        if parsed_value > 0:
            parsed[key] = parsed_value
    return parsed


def build_phase6_gate(
    profile_d_rows: Sequence[Mapping[str, Any]],
    *,
    mode: str = PHASE6_GATE_MODE_STRICT,
    invalid_rate_threshold: float = 0.0,
) -> Dict[str, Any]:
    gate_mode = _normalize_phase6_gate_mode(mode)
    threshold = _round_metric(float(invalid_rate_threshold))
    gate_rows: List[Dict[str, Any]] = []
    invalid_union: Set[str] = set()
    invalid_reason_counts_total: Dict[str, int] = {}
    request_failed_reason_counts_total: Dict[str, int] = {}
    query_count_total = 0
    invalid_count_total = 0
    request_failed_count_total = 0
    for row in profile_d_rows:
        degradation = row.get("degradation", {})
        invalid_reasons = [
            reason
            for reason in degradation.get("invalid_reasons", [])
            if isinstance(reason, str)
        ]
        query_count = int(degradation.get("queries", 0) or 0)
        invalid_count = int(degradation.get("invalid_count", 0) or 0)
        request_failed_count = int(degradation.get("request_failed_count", 0) or 0)
        invalid_rate = float(degradation.get("invalid_rate", 0.0) or 0.0)
        request_failed_rate = float(degradation.get("request_failed_rate", 0.0) or 0.0)
        invalid_reason_counts = _coerce_reason_count_map(
            degradation.get("invalid_reason_counts")
        )
        request_failed_reason_counts = _coerce_reason_count_map(
            degradation.get("request_failed_reason_counts")
        )

        if query_count > 0:
            if invalid_count <= 0 and invalid_reasons:
                invalid_count = len(invalid_reasons)
            if invalid_rate <= 0 and invalid_count > 0:
                invalid_rate = float(invalid_count) / float(query_count)
            if request_failed_count <= 0 and request_failed_reason_counts:
                request_failed_count = sum(request_failed_reason_counts.values())
            if request_failed_rate <= 0 and request_failed_count > 0:
                request_failed_rate = float(request_failed_count) / float(query_count)

        if gate_mode == PHASE6_GATE_MODE_STRICT:
            row_valid = len(invalid_reasons) == 0
        else:
            row_valid = invalid_rate <= threshold

        invalid_union.update(invalid_reasons)
        query_count_total += max(0, query_count)
        invalid_count_total += max(0, invalid_count)
        request_failed_count_total += max(0, request_failed_count)
        for reason, count in invalid_reason_counts.items():
            invalid_reason_counts_total[reason] = invalid_reason_counts_total.get(reason, 0) + count
        for reason, count in request_failed_reason_counts.items():
            request_failed_reason_counts_total[reason] = (
                request_failed_reason_counts_total.get(reason, 0) + count
            )
        gate_rows.append(
            {
                "dataset": row.get("dataset"),
                "dataset_label": row.get("dataset_label"),
                "valid": row_valid,
                "invalid_reasons": invalid_reasons,
                "query_count": query_count,
                "invalid_count": invalid_count,
                "invalid_rate": _round_metric(invalid_rate),
                "invalid_reason_counts": invalid_reason_counts,
                "request_failed_count": request_failed_count,
                "request_failed_rate": _round_metric(request_failed_rate),
                "request_failed_reason_counts": request_failed_reason_counts,
            }
        )
    overall_invalid_rate = (
        float(invalid_count_total) / float(query_count_total) if query_count_total > 0 else 0.0
    )
    overall_request_failed_rate = (
        float(request_failed_count_total) / float(query_count_total)
        if query_count_total > 0
        else 0.0
    )
    return {
        "mode": gate_mode,
        "invalid_rate_threshold": threshold,
        "valid": all(bool(row.get("valid")) for row in gate_rows),
        "invalid_reasons": sorted(invalid_union),
        "invalid_reason_counts": dict(sorted(invalid_reason_counts_total.items())),
        "query_count": int(query_count_total),
        "invalid_count": int(invalid_count_total),
        "invalid_rate": _round_metric(overall_invalid_rate),
        "request_failed_count": int(request_failed_count_total),
        "request_failed_rate": _round_metric(overall_request_failed_rate),
        "request_failed_reason_counts": dict(sorted(request_failed_reason_counts_total.items())),
        "rows": gate_rows,
    }


def _build_comparison_rows(
    profiles: Mapping[str, Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    required_keys = ("profile_a", "profile_b", "profile_c", "profile_d")
    if any(key not in profiles for key in required_keys):
        return []
    rows_a = {row["dataset"]: row for row in profiles["profile_a"]["rows"]}
    rows_b = {row["dataset"]: row for row in profiles["profile_b"]["rows"]}
    rows_c = {row["dataset"]: row for row in profiles["profile_c"]["rows"]}
    rows_d = {row["dataset"]: row for row in profiles["profile_d"]["rows"]}

    comparison_rows: List[Dict[str, Any]] = []
    for dataset_key in rows_d:
        row_a = rows_a[dataset_key]
        row_b = rows_b[dataset_key]
        row_c = rows_c[dataset_key]
        row_d = rows_d[dataset_key]
        invalid_reasons = list(row_d.get("degradation", {}).get("invalid_reasons", []))
        comparison_rows.append(
            {
                "dataset": dataset_key,
                "dataset_label": row_d["dataset_label"],
                "a_hr10": row_a["quality"]["hr_at_10"],
                "b_hr10": row_b["quality"]["hr_at_10"],
                "c_hr10": row_c["quality"]["hr_at_10"],
                "d_hr10": row_d["quality"]["hr_at_10"],
                "a_ndcg10": row_a["quality"]["ndcg_at_10"],
                "b_ndcg10": row_b["quality"]["ndcg_at_10"],
                "c_ndcg10": row_c["quality"]["ndcg_at_10"],
                "d_ndcg10": row_d["quality"]["ndcg_at_10"],
                "a_p95": row_a["latency_ms"]["p95"],
                "b_p95": row_b["latency_ms"]["p95"],
                "c_p95": row_c["latency_ms"]["p95"],
                "d_p95": row_d["latency_ms"]["p95"],
                "valid": len(invalid_reasons) == 0,
                "invalid_reasons": invalid_reasons,
            }
        )
    return comparison_rows


async def _evaluate_dataset(
    *,
    client: SQLiteClient,
    bundle: DatasetBundle,
    profile_mode: str,
    memory_to_doc: Mapping[int, str],
    max_results: int,
    candidate_multiplier: int,
    entrypoint: str = REAL_PROFILE_ENTRYPOINT_SQLITE_CLIENT,
    query_runner: Optional[
        Callable[..., Awaitable[Dict[str, Any]]]
    ] = None,
) -> Dict[str, Any]:
    entrypoint_value = _normalize_entrypoint(entrypoint)
    effective_max_results = max(REAL_PROFILE_METRIC_TOP_K, int(max_results))
    effective_candidate_multiplier = max(1, int(candidate_multiplier))

    metric_rows: List[Dict[str, float]] = []
    latencies_ms: List[float] = []
    degrade_count = 0
    degrade_reasons_union: Set[str] = set()
    invalid_reasons_union: Set[str] = set()
    invalid_count = 0
    invalid_reason_counts: Dict[str, int] = {}
    request_failed_count = 0
    request_failed_reason_counts: Dict[str, int] = {}
    retrieval_state_reset_per_query = False
    backend_method_counts: Dict[str, int] = {}
    intent_applied_counts: Dict[str, int] = {}
    strategy_template_counts: Dict[str, int] = {}
    candidate_multiplier_applied_values: List[float] = []
    candidate_pool_size_values: List[float] = []
    session_first_enabled = False
    session_contributed_values: List[float] = []

    if query_runner is None:
        if entrypoint_value == REAL_PROFILE_ENTRYPOINT_MCP_SEARCH_MEMORY:
            query_runner = _run_query_via_mcp_search_memory
        else:
            query_runner = _run_query_via_sqlite_client

    for case in bundle.queries:
        retrieval_state_reset_per_query = (
            await _reset_domain_retrieval_state(
                client, bundle.domain, bundle.path_prefix or None
            )
            or retrieval_state_reset_per_query
        )
        start = perf_counter()
        payload = await query_runner(
            client=client,
            case=case,
            bundle=bundle,
            profile_mode=profile_mode,
            max_results=effective_max_results,
            candidate_multiplier=effective_candidate_multiplier,
        )
        elapsed_ms = (perf_counter() - start) * 1000.0
        latencies_ms.append(elapsed_ms)

        degraded = bool(payload.get("degraded"))
        if degraded:
            degrade_count += 1

        raw_reasons = payload.get("degrade_reasons")
        reasons = (
            [reason for reason in raw_reasons if isinstance(reason, str)]
            if isinstance(raw_reasons, list)
            else []
        )
        query_invalid_reasons = sorted(
            {reason for reason in reasons if reason in PROFILE_D_INVALID_GATE_REASONS}
        )
        query_request_failed_reasons = sorted(
            {reason for reason in query_invalid_reasons if reason in PROFILE_D_REQUEST_FAILED_REASONS}
        )
        degrade_reasons_union.update(reasons)
        invalid_reasons_union.update(query_invalid_reasons)
        if query_invalid_reasons:
            invalid_count += 1
            for reason in query_invalid_reasons:
                invalid_reason_counts[reason] = invalid_reason_counts.get(reason, 0) + 1
        if query_request_failed_reasons:
            request_failed_count += 1
            for reason in query_request_failed_reasons:
                request_failed_reason_counts[reason] = (
                    request_failed_reason_counts.get(reason, 0) + 1
                )

        backend_method = str(
            payload.get("backend_method") or _entrypoint_source_variant(entrypoint_value)
        )
        backend_method_counts[backend_method] = backend_method_counts.get(backend_method, 0) + 1

        intent_applied = payload.get("intent_applied")
        if intent_applied is None and isinstance(payload.get("metadata"), Mapping):
            intent_applied = payload["metadata"].get("intent")
        if isinstance(intent_applied, str) and intent_applied.strip():
            key = intent_applied.strip()
            intent_applied_counts[key] = intent_applied_counts.get(key, 0) + 1

        strategy_template_applied = payload.get("strategy_template_applied")
        if strategy_template_applied is None and isinstance(payload.get("metadata"), Mapping):
            strategy_template_applied = payload["metadata"].get("strategy_template")
        if isinstance(strategy_template_applied, str) and strategy_template_applied.strip():
            key = strategy_template_applied.strip()
            strategy_template_counts[key] = strategy_template_counts.get(key, 0) + 1

        candidate_multiplier_applied = payload.get("candidate_multiplier_applied")
        if candidate_multiplier_applied is None and isinstance(payload.get("metadata"), Mapping):
            candidate_multiplier_applied = payload["metadata"].get(
                "candidate_multiplier_applied"
            )
        try:
            candidate_multiplier_applied_values.append(float(candidate_multiplier_applied))
        except (TypeError, ValueError):
            pass

        candidate_pool_size = payload.get("candidate_pool_size")
        if candidate_pool_size is None and isinstance(payload.get("metadata"), Mapping):
            candidate_counts = payload["metadata"].get("candidate_counts")
            if isinstance(candidate_counts, Mapping):
                candidate_pool_size = candidate_counts.get("candidate_limit")
        try:
            candidate_pool_size_values.append(float(candidate_pool_size))
        except (TypeError, ValueError):
            pass

        session_first_enabled = session_first_enabled or bool(
            payload.get("session_first_enabled")
        )
        session_first_metrics = payload.get("session_first_metrics")
        if isinstance(session_first_metrics, Mapping):
            try:
                session_contributed_values.append(
                    float(session_first_metrics.get("session_contributed", 0))
                )
            except (TypeError, ValueError):
                pass

        results = payload.get("results")
        result_rows = results if isinstance(results, list) else []
        retrieved_doc_ids: List[str] = []
        for item in result_rows[:REAL_PROFILE_METRIC_TOP_K]:
            if not isinstance(item, Mapping):
                continue
            memory_id = item.get("memory_id")
            if memory_id is None:
                continue
            try:
                parsed_memory_id = int(memory_id)
            except (TypeError, ValueError):
                continue
            doc_id = memory_to_doc.get(parsed_memory_id)
            if doc_id:
                retrieved_doc_ids.append(doc_id)
        retrieved_doc_ids = _collapse_ranked_doc_ids(retrieved_doc_ids)

        metric_rows.append(
            compute_retrieval_metrics(
                retrieved_doc_ids=retrieved_doc_ids,
                relevant_doc_ids=case.relevant_doc_ids,
                k=REAL_PROFILE_METRIC_TOP_K,
            )
        )

    query_count = len(metric_rows)
    if query_count <= 0:
        raise RuntimeError(f"{bundle.key}: query_count is zero after evaluation")

    quality = {
        "hr_at_5": _round_metric(
            sum(item["hr_at_5"] for item in metric_rows) / float(query_count)
        ),
        "hr_at_10": _round_metric(
            sum(item["hr_at_10"] for item in metric_rows) / float(query_count)
        ),
        "mrr": _round_metric(sum(item["mrr"] for item in metric_rows) / float(query_count)),
        "ndcg_at_10": _round_metric(
            sum(item["ndcg_at_10"] for item in metric_rows) / float(query_count)
        ),
        "recall_at_10": _round_metric(
            sum(item["recall_at_10"] for item in metric_rows) / float(query_count)
        ),
    }

    row = {
        "dataset": bundle.key,
        "dataset_label": bundle.label,
        "mode": profile_mode,
        "entrypoint": entrypoint_value,
        "source_variant": _entrypoint_source_variant(entrypoint_value),
        "sample_size": query_count,
        "query_count": query_count,
        "query_count_raw": bundle.query_count_raw,
        "sample_bucket_size": bundle.sample_bucket_size,
        "corpus_doc_count": len(bundle.docs),
        "dataset_relevance": {
            "avg_relevant_per_query": float(bundle.avg_relevant_per_query),
            "max_relevant_per_query": int(bundle.max_relevant_per_query),
            "multi_relevant_query_rate": float(bundle.multi_relevant_query_rate),
        },
        "retrieval_depth": {
            "max_results": int(effective_max_results),
            "candidate_multiplier": int(effective_candidate_multiplier),
            "metric_top_k": int(REAL_PROFILE_METRIC_TOP_K),
            "result_collapse": REAL_PROFILE_RESULT_COLLAPSE,
            "retrieval_state_reset_per_query": bool(retrieval_state_reset_per_query),
        },
        "entrypoint_stats": {
            "backend_method_counts": dict(sorted(backend_method_counts.items())),
            "intent_applied_counts": dict(sorted(intent_applied_counts.items())),
            "strategy_template_applied_counts": dict(
                sorted(strategy_template_counts.items())
            ),
            "candidate_multiplier_applied_avg": _round_metric(
                sum(candidate_multiplier_applied_values)
                / float(len(candidate_multiplier_applied_values))
            )
            if candidate_multiplier_applied_values
            else None,
            "candidate_pool_size_avg": _round_metric(
                sum(candidate_pool_size_values) / float(len(candidate_pool_size_values))
            )
            if candidate_pool_size_values
            else None,
            "session_first_enabled": bool(session_first_enabled),
            "session_contributed_avg": _round_metric(
                sum(session_contributed_values) / float(len(session_contributed_values))
            )
            if session_contributed_values
            else 0.0,
        },
        "quality": quality,
        "latency_ms": {
            "p50": _round_metric(compute_percentile(latencies_ms, 0.50)),
            "p95": _round_metric(compute_percentile(latencies_ms, 0.95)),
            "p99": _round_metric(compute_percentile(latencies_ms, 0.99)),
        },
        "degradation": {
            "queries": query_count,
            "degraded": int(degrade_count),
            "degrade_rate": _round_metric(float(degrade_count) / float(query_count)),
            "degrade_reasons": sorted(degrade_reasons_union),
            "invalid_reasons": sorted(invalid_reasons_union),
            "invalid_count": int(invalid_count),
            "invalid_rate": _round_metric(float(invalid_count) / float(query_count)),
            "invalid_reason_counts": dict(sorted(invalid_reason_counts.items())),
            "request_failed_count": int(request_failed_count),
            "request_failed_rate": _round_metric(
                float(request_failed_count) / float(query_count)
            ),
            "request_failed_reason_counts": dict(
                sorted(request_failed_reason_counts.items())
            ),
            "valid": len(invalid_reasons_union) == 0,
        },
    }
    return row


async def _run_profile(
    *,
    config: ProfileConfig,
    bundles: Sequence[DatasetBundle],
    db_path: Path,
    max_results: int,
    candidate_multiplier: int,
    entrypoint: str,
    existing_mapping: Optional[Mapping[str, Mapping[int, str]]] = None,
    populate: bool,
) -> tuple[Dict[str, Any], Dict[str, Dict[int, str]]]:
    profile_mapping: Dict[str, Dict[int, str]] = (
        {key: dict(value) for key, value in existing_mapping.items()}
        if existing_mapping is not None
        else {}
    )

    with patched_env(config.env_overrides):
        client = SQLiteClient(_sqlite_url(db_path))
        try:
            await client.init_db()

            if populate:
                for bundle in bundles:
                    profile_mapping[bundle.key] = await _populate_bundle_docs(client, bundle)
            else:
                for bundle in bundles:
                    if bundle.key not in profile_mapping:
                        raise RuntimeError(
                            f"{config.key}: missing pre-populated mapping for {bundle.key}"
                        )

            rows: List[Dict[str, Any]] = []
            for bundle in bundles:
                rows.append(
                    await _evaluate_dataset(
                        client=client,
                        bundle=bundle,
                        profile_mode=config.mode,
                        memory_to_doc=profile_mapping[bundle.key],
                        max_results=int(max_results),
                        candidate_multiplier=int(candidate_multiplier),
                        entrypoint=entrypoint,
                    )
                )
            return (
                {
                    "profile": config.key,
                    "mode": config.mode,
                    "rows": rows,
                },
                profile_mapping,
            )
        finally:
            await client.close()


async def build_profile_abcd_real_metrics(
    *,
    sample_size: int = 30,
    dataset_keys: Sequence[str] = REAL_DATASET_DEFAULTS,
    profile_keys: Sequence[str] = REAL_PROFILE_DEFAULT_KEYS,
    factual_pool_cap: Optional[int] = None,
    first_relevant_only: bool = True,
    extra_distractors: int = 200,
    max_results: int = REAL_PROFILE_DEFAULT_MAX_RESULTS,
    candidate_multiplier: int = REAL_PROFILE_DEFAULT_CANDIDATE_MULTIPLIER,
    entrypoint: str = REAL_PROFILE_DEFAULT_ENTRYPOINT,
    seed: int = REAL_RANDOM_SEED,
    workdir: Optional[Path] = None,
) -> Dict[str, Any]:
    if int(sample_size) <= 0:
        raise ValueError("sample_size must be > 0")
    if int(max_results) <= 0:
        raise ValueError("max_results must be > 0")
    if int(candidate_multiplier) <= 0:
        raise ValueError("candidate_multiplier must be > 0")

    entrypoint_value = _normalize_entrypoint(entrypoint)
    selected_profile_keys = _normalize_profile_keys(profile_keys)
    execution_profile_keys = _resolve_execution_profile_keys(selected_profile_keys)
    effective_max_results = max(REAL_PROFILE_METRIC_TOP_K, int(max_results))
    effective_candidate_multiplier = max(1, int(candidate_multiplier))

    selected_keys = [key for key in dataset_keys if key in DATASET_LABELS]
    if not selected_keys:
        raise RuntimeError("no supported dataset_keys selected")

    bundles: List[DatasetBundle] = []
    for index, dataset_key in enumerate(selected_keys):
        bundles.append(
            build_dataset_bundle(
                dataset_key=dataset_key,
                sample_size=int(sample_size),
                first_relevant_only=bool(first_relevant_only),
                extra_distractors=int(extra_distractors),
                seed=seed + index * 17,
            )
        )

    profile_results: Dict[str, Dict[str, Any]] = {}
    profile_doc_mappings: Dict[str, Dict[str, Dict[int, str]]] = {}

    real_profile_workdir = resolve_real_profile_workdir(workdir)
    real_profile_workdir.mkdir(parents=True, exist_ok=True)
    db_paths = {
        config.key: real_profile_workdir / f"{config.key}.db"
        for config in PROFILE_CONFIGS
        if config.key in execution_profile_keys
    }

    executed_profile_results: Dict[str, Dict[str, Any]] = {}

    for config in PROFILE_CONFIGS:
        if config.key not in execution_profile_keys:
            continue
        profile_env_overrides = dict(config.env_overrides)
        if factual_pool_cap is not None:
            profile_env_overrides["RETRIEVAL_FACTUAL_CANDIDATE_MULTIPLIER_CAP"] = str(
                int(factual_pool_cap)
            )
        effective_config = ProfileConfig(
            key=config.key,
            mode=config.mode,
            env_overrides=profile_env_overrides,
            reuse_data_from=config.reuse_data_from,
        )
        if config.reuse_data_from is None:
            _reset_db(db_paths[config.key])
            existing_mapping = None
            populate = True
        else:
            source_key = config.reuse_data_from
            source_db_path = db_paths[source_key]
            if not source_db_path.exists():
                raise RuntimeError(
                    f"{config.key}: source profile db missing: {source_db_path}"
                )
            db_paths[config.key] = source_db_path
            existing_mapping = profile_doc_mappings.get(source_key)
            populate = False

        profile_payload, mapping = await _run_profile(
            config=effective_config,
            bundles=bundles,
            db_path=db_paths[config.key],
            max_results=effective_max_results,
            candidate_multiplier=effective_candidate_multiplier,
            entrypoint=entrypoint_value,
            existing_mapping=existing_mapping,
            populate=populate,
        )
        executed_profile_results[config.key] = profile_payload
        profile_doc_mappings[config.key] = mapping

    profile_results = {
        key: executed_profile_results[key]
        for key in selected_profile_keys
        if key in executed_profile_results
    }

    phase6_config = resolve_phase6_gate_config()
    if "profile_d" in executed_profile_results:
        phase6_gate = build_phase6_gate(
            executed_profile_results["profile_d"]["rows"],
            mode=str(phase6_config["mode"]),
            invalid_rate_threshold=float(phase6_config["invalid_rate_threshold"]),
        )
    else:
        phase6_gate = {
            "valid": True,
            "mode": str(phase6_config["mode"]),
            "invalid_rate_threshold": float(phase6_config["invalid_rate_threshold"]),
            "invalid_reasons": [],
            "invalid_count": 0,
            "query_count": 0,
            "invalid_rate": 0.0,
            "request_failed_count": 0,
            "request_failed_rate": 0.0,
            "rows": [],
        }
    phase6_comparison = _build_comparison_rows(profile_results)

    return {
        "generated_at_utc": _utc_now_iso(),
        "source": "backend/tests/benchmark/helpers/profile_abcd_real_runner.py",
        "source_variant": _entrypoint_source_variant(entrypoint_value),
        "sample_size_requested": int(sample_size),
        "dataset_scope": selected_keys,
        "dataset_labels": {bundle.key: bundle.label for bundle in bundles},
        "real_run_strategy": {
            "profiles": list(selected_profile_keys),
            "entrypoint": entrypoint_value,
            "factual_pool_cap": (
                int(factual_pool_cap) if factual_pool_cap is not None else None
            ),
            "first_relevant_only": bool(first_relevant_only),
            "relevance_mode": (
                "first_relevant_only" if first_relevant_only else "all_relevant"
            ),
            "extra_distractors": int(extra_distractors),
            "max_results": int(effective_max_results),
            "candidate_multiplier": int(effective_candidate_multiplier),
            "metric_top_k": int(REAL_PROFILE_METRIC_TOP_K),
            "result_collapse": REAL_PROFILE_RESULT_COLLAPSE,
            "seed": int(seed),
            "workdir": _sanitize_report_path(real_profile_workdir),
        },
        "profiles": profile_results,
        "phase6": {
            "gate": phase6_gate,
            "comparison_rows": phase6_comparison,
            "invalid_gate_reasons": sorted(PROFILE_D_INVALID_GATE_REASONS),
            "gate_config": phase6_config,
        },
    }


def _render_profile_rows_table(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    lines = [
        "| Dataset | Queries | Corpus Docs | Avg Rel/Q | Multi Rel Rate | HR@10 | MRR | NDCG@10 | Recall@10 | p95(ms) | Degrade Rate | Invalid Reasons |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in rows:
        degradation = row["degradation"]
        relevance = row.get("dataset_relevance", {})
        invalid_reasons = ",".join(degradation["invalid_reasons"]) or "-"
        lines.append(
            "| {dataset} | {queries} | {corpus_docs} | {avg_rel:.2f} | {multi_rel_rate:.1%} | {hr10:.3f} | {mrr:.3f} | {ndcg:.3f} | {recall:.3f} | {p95:.1f} | {degrade_rate:.1%} | {invalid} |".format(
                dataset=row["dataset_label"],
                queries=int(row["query_count"]),
                corpus_docs=int(row["corpus_doc_count"]),
                avg_rel=float(relevance.get("avg_relevant_per_query", 0.0)),
                multi_rel_rate=float(relevance.get("multi_relevant_query_rate", 0.0)),
                hr10=float(row["quality"]["hr_at_10"]),
                mrr=float(row["quality"]["mrr"]),
                ndcg=float(row["quality"]["ndcg_at_10"]),
                recall=float(row["quality"]["recall_at_10"]),
                p95=float(row["latency_ms"]["p95"]),
                degrade_rate=float(degradation["degrade_rate"]),
                invalid=invalid_reasons,
            )
        )
    return lines


def render_profile_abcd_real_markdown(payload: Mapping[str, Any]) -> str:
    first_relevant_only = bool(payload["real_run_strategy"]["first_relevant_only"])
    relevance_mode = str(
        payload["real_run_strategy"].get(
            "relevance_mode",
            "first_relevant_only" if first_relevant_only else "all_relevant",
        )
    )
    entrypoint = payload["real_run_strategy"].get(
        "entrypoint", REAL_PROFILE_DEFAULT_ENTRYPOINT
    )
    result_collapse = payload["real_run_strategy"].get(
        "result_collapse", REAL_PROFILE_RESULT_COLLAPSE
    )
    lines: List[str] = [
        "# Benchmark Results - profile_abcd_real",
        "",
        f"> generated_at_utc: {payload['generated_at_utc']}",
        f"> source_variant: {payload.get('source_variant', _entrypoint_source_variant(str(entrypoint)))}",
        f"> mode: real execution ({payload.get('source_variant', _entrypoint_source_variant(str(entrypoint)))} + runtime profile env)",
        "",
        "## Run Strategy",
        "",
        f"- dataset_scope: {', '.join(payload.get('dataset_scope', []))}",
        f"- profiles: {', '.join(payload['real_run_strategy'].get('profiles', list(payload.get('profiles', {}).keys())))}",
        f"- sample_size_requested: {payload.get('sample_size_requested')}",
        f"- entrypoint: {entrypoint}",
        f"- factual_pool_cap: {payload['real_run_strategy'].get('factual_pool_cap')}",
        f"- first_relevant_only: {payload['real_run_strategy']['first_relevant_only']}",
        f"- relevance_mode: {relevance_mode}",
        f"- result_collapse: {result_collapse}",
        f"- extra_distractors: {payload['real_run_strategy']['extra_distractors']}",
        f"- max_results: {payload['real_run_strategy'].get('max_results')}",
        f"- candidate_multiplier: {payload['real_run_strategy'].get('candidate_multiplier')}",
        f"- metric_top_k: {payload['real_run_strategy'].get('metric_top_k')}",
        "",
    ]
    if not first_relevant_only:
        lines.extend(
            [
                "> all_relevant mode: HR@10 reads as any-hit, Recall@10 reads as multi-relevant coverage, and NDCG@10 best reflects top-10 relevance density.",
                "",
            ]
        )

    profiles = payload["profiles"]
    ordered_profile_keys = [
        key for key in REAL_PROFILE_DEFAULT_KEYS if key in profiles
    ]
    for profile_key in ordered_profile_keys:
        profile_payload = profiles[profile_key]
        lines.extend(
            [
                f"## {profile_key}",
                "",
                f"- mode: `{profile_payload['mode']}`",
                "",
            ]
        )
        lines.extend(_render_profile_rows_table(profile_payload["rows"]))
        lines.append("")

    lines.extend(
        [
            "## Phase 6 Gate (Profile D)",
            "",
            (
                f"- overall_valid: {'true' if payload['phase6']['gate']['valid'] else 'false'}"
            ),
            f"- gate_mode: `{payload['phase6']['gate'].get('mode', PHASE6_GATE_MODE_STRICT)}`",
            "- invalid_rate_threshold: "
            + "{:.2%}".format(
                float(payload["phase6"]["gate"].get("invalid_rate_threshold", 0.0))
            ),
            (
                "- invalid_reasons: "
                + ", ".join(payload["phase6"]["gate"]["invalid_reasons"])
                if payload["phase6"]["gate"]["invalid_reasons"]
                else "- invalid_reasons: (none)"
            ),
            (
                f"- invalid_count: {int(payload['phase6']['gate'].get('invalid_count', 0))} "
                f"/ {int(payload['phase6']['gate'].get('query_count', 0))} "
                f"({float(payload['phase6']['gate'].get('invalid_rate', 0.0)):.2%})"
            ),
            (
                f"- request_failed_count: {int(payload['phase6']['gate'].get('request_failed_count', 0))} "
                f"({float(payload['phase6']['gate'].get('request_failed_rate', 0.0)):.2%})"
            ),
            "",
            "| Dataset | Valid | Invalid Reasons | Invalid Count | Invalid Rate | RequestFailed Count | RequestFailed Rate |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in payload["phase6"]["gate"]["rows"]:
        reasons = ",".join(row["invalid_reasons"]) if row["invalid_reasons"] else "-"
        lines.append(
            "| {dataset} | {valid} | {reasons} | {invalid_count} | {invalid_rate:.2%} | {request_failed_count} | {request_failed_rate:.2%} |".format(
                dataset=row["dataset_label"],
                valid="PASS" if row["valid"] else "INVALID",
                reasons=reasons,
                invalid_count=int(row.get("invalid_count", 0)),
                invalid_rate=float(row.get("invalid_rate", 0.0)),
                request_failed_count=int(row.get("request_failed_count", 0)),
                request_failed_rate=float(row.get("request_failed_rate", 0.0)),
            )
        )

    comparison_rows = payload["phase6"].get("comparison_rows", [])
    if comparison_rows:
        lines.extend(
            [
                "",
                "## A/B/C/D Comparison",
                "",
                "| Dataset | A HR@10 | B HR@10 | C HR@10 | D HR@10 | A NDCG@10 | B NDCG@10 | C NDCG@10 | D NDCG@10 | A p95 | B p95 | C p95 | D p95 | D Gate |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in comparison_rows:
            lines.append(
                "| {dataset} | {a_hr10:.3f} | {b_hr10:.3f} | {c_hr10:.3f} | {d_hr10:.3f} | "
                "{a_ndcg10:.3f} | {b_ndcg10:.3f} | {c_ndcg10:.3f} | {d_ndcg10:.3f} | "
                "{a_p95:.1f} | {b_p95:.1f} | {c_p95:.1f} | {d_p95:.1f} | {gate} |".format(
                    dataset=row["dataset_label"],
                    a_hr10=float(row["a_hr10"]),
                    b_hr10=float(row["b_hr10"]),
                    c_hr10=float(row["c_hr10"]),
                    d_hr10=float(row["d_hr10"]),
                    a_ndcg10=float(row["a_ndcg10"]),
                    b_ndcg10=float(row["b_ndcg10"]),
                    c_ndcg10=float(row["c_ndcg10"]),
                    d_ndcg10=float(row["d_ndcg10"]),
                    a_p95=float(row["a_p95"]),
                    b_p95=float(row["b_p95"]),
                    c_p95=float(row["c_p95"]),
                    d_p95=float(row["d_p95"]),
                    gate="PASS" if row["valid"] else "INVALID",
                )
            )
        lines.append("")
    return "\n".join(lines)


def render_profile_cd_real_markdown(payload: Mapping[str, Any]) -> str:
    first_relevant_only = bool(payload["real_run_strategy"]["first_relevant_only"])
    relevance_mode = str(
        payload["real_run_strategy"].get(
            "relevance_mode",
            "first_relevant_only" if first_relevant_only else "all_relevant",
        )
    )
    entrypoint = payload["real_run_strategy"].get(
        "entrypoint", REAL_PROFILE_DEFAULT_ENTRYPOINT
    )
    result_collapse = payload["real_run_strategy"].get(
        "result_collapse", REAL_PROFILE_RESULT_COLLAPSE
    )
    profiles = payload["profiles"]
    profile_c = profiles.get("profile_c")
    profile_d = profiles.get("profile_d")
    lines: List[str] = [
        "# Benchmark Results - profile_cd_real",
        "",
        f"> generated_at_utc: {payload['generated_at_utc']}",
        f"> source_variant: {payload.get('source_variant', _entrypoint_source_variant(str(entrypoint)))}",
        "> mode: real API embedding/reranker execution",
        f"> entrypoint: {entrypoint}",
        f"> factual_pool_cap: {payload['real_run_strategy'].get('factual_pool_cap')}",
        f"> relevance_mode: {relevance_mode}",
        f"> result_collapse: {result_collapse}",
        "",
    ]
    if not first_relevant_only:
        lines.extend(
            [
                "> all_relevant mode: prefer NDCG@10 for top-10 ordering quality and Recall@10 for coverage across multiple relevant documents.",
                "",
            ]
        )
    if profile_c is not None:
        lines.extend(["## profile_c", ""])
        lines.extend(_render_profile_rows_table(profile_c["rows"]))
        lines.append("")
    if profile_d is not None:
        lines.extend(["## profile_d", ""])
        lines.extend(_render_profile_rows_table(profile_d["rows"]))
        lines.append("")
    lines.append("")
    lines.extend(
        [
            "## Phase 6 Gate",
            "",
            f"- overall_valid: {'true' if payload['phase6']['gate']['valid'] else 'false'}",
            f"- gate_mode: `{payload['phase6']['gate'].get('mode', PHASE6_GATE_MODE_STRICT)}`",
            "- invalid_rate_threshold: "
            + "{:.2%}".format(
                float(payload["phase6"]["gate"].get("invalid_rate_threshold", 0.0))
            ),
            (
                "- invalid_reasons: "
                + ", ".join(payload["phase6"]["gate"]["invalid_reasons"])
                if payload["phase6"]["gate"]["invalid_reasons"]
                else "- invalid_reasons: (none)"
            ),
            (
                f"- invalid_count: {int(payload['phase6']['gate'].get('invalid_count', 0))} "
                f"/ {int(payload['phase6']['gate'].get('query_count', 0))} "
                f"({float(payload['phase6']['gate'].get('invalid_rate', 0.0)):.2%})"
            ),
            (
                f"- request_failed_count: {int(payload['phase6']['gate'].get('request_failed_count', 0))} "
                f"({float(payload['phase6']['gate'].get('request_failed_rate', 0.0)):.2%})"
            ),
            "",
        ]
    )
    return "\n".join(lines)


def write_profile_abcd_real_artifacts(
    payload: Mapping[str, Any],
    *,
    json_path: Path = REAL_PROFILE_JSON_ARTIFACT,
    markdown_path: Path = REAL_PROFILE_MARKDOWN_ARTIFACT,
    cd_markdown_path: Path = REAL_PROFILE_CD_MARKDOWN_ARTIFACT,
) -> Dict[str, Path]:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_profile_abcd_real_markdown(payload),
        encoding="utf-8",
    )
    cd_markdown_path.write_text(
        render_profile_cd_real_markdown(payload),
        encoding="utf-8",
    )
    return {
        "json": json_path,
        "markdown": markdown_path,
        "cd_markdown": cd_markdown_path,
    }


def render_factual_pool_cap_compare_markdown(
    baseline_payload: Mapping[str, Any],
    compare_payload: Mapping[str, Any],
) -> str:
    baseline_strategy = baseline_payload.get("real_run_strategy", {})
    compare_strategy = compare_payload.get("real_run_strategy", {})
    baseline_profiles = baseline_payload.get("profiles", {})
    compare_profiles = compare_payload.get("profiles", {})
    ordered_profile_keys = [
        key
        for key in REAL_PROFILE_DEFAULT_KEYS
        if key in baseline_profiles and key in compare_profiles
    ]

    lines = [
        "# Benchmark Results - factual_pool_cap comparison",
        "",
        f"> baseline_generated_at_utc: {baseline_payload.get('generated_at_utc')}",
        f"> compare_generated_at_utc: {compare_payload.get('generated_at_utc')}",
        f"> entrypoint: {baseline_strategy.get('entrypoint')}",
        f"> profiles: {', '.join(baseline_strategy.get('profiles', list(baseline_profiles.keys())))}",
        f"> dataset_scope: {', '.join(baseline_payload.get('dataset_scope', []))}",
        f"> baseline_factual_pool_cap: {baseline_strategy.get('factual_pool_cap')}",
        f"> compare_factual_pool_cap: {compare_strategy.get('factual_pool_cap')}",
        "",
    ]

    for profile_key in ordered_profile_keys:
        baseline_rows = {
            row["dataset"]: row for row in baseline_profiles[profile_key]["rows"]
        }
        compare_rows = {
            row["dataset"]: row for row in compare_profiles[profile_key]["rows"]
        }
        dataset_keys = [key for key in baseline_rows if key in compare_rows]
        if not dataset_keys:
            continue
        lines.extend(
            [
                f"## {profile_key}",
                "",
                "| Dataset | Baseline NDCG@10 | Compare NDCG@10 | Delta | Baseline Recall@10 | Compare Recall@10 | Delta | Baseline p95 | Compare p95 | Delta |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for dataset_key in dataset_keys:
            baseline_row = baseline_rows[dataset_key]
            compare_row = compare_rows[dataset_key]
            baseline_ndcg = float(baseline_row["quality"]["ndcg_at_10"])
            compare_ndcg = float(compare_row["quality"]["ndcg_at_10"])
            baseline_recall = float(baseline_row["quality"]["recall_at_10"])
            compare_recall = float(compare_row["quality"]["recall_at_10"])
            baseline_p95 = float(baseline_row["latency_ms"]["p95"])
            compare_p95 = float(compare_row["latency_ms"]["p95"])
            lines.append(
                "| {dataset} | {baseline_ndcg:.3f} | {compare_ndcg:.3f} | {delta_ndcg:+.3f} | "
                "{baseline_recall:.3f} | {compare_recall:.3f} | {delta_recall:+.3f} | "
                "{baseline_p95:.1f} | {compare_p95:.1f} | {delta_p95:+.1f} |".format(
                    dataset=baseline_row["dataset_label"],
                    baseline_ndcg=baseline_ndcg,
                    compare_ndcg=compare_ndcg,
                    delta_ndcg=(compare_ndcg - baseline_ndcg),
                    baseline_recall=baseline_recall,
                    compare_recall=compare_recall,
                    delta_recall=(compare_recall - baseline_recall),
                    baseline_p95=baseline_p95,
                    compare_p95=compare_p95,
                    delta_p95=(compare_p95 - baseline_p95),
                )
            )
        lines.append("")
    return "\n".join(lines)


def _profile_dataset_mean(rows: Sequence[Mapping[str, Any]], field: str) -> float:
    if not rows:
        return 0.0
    if field == "p95":
        values = [float(row["latency_ms"]["p95"]) for row in rows]
    else:
        values = [float(row["quality"][field]) for row in rows]
    return sum(values) / float(len(values))


def render_abcd_sota_analysis_markdown(payload: Mapping[str, Any]) -> str:
    profiles = payload["profiles"]
    real_run_strategy = payload.get("real_run_strategy", {})
    first_relevant_only = bool(real_run_strategy.get("first_relevant_only", True))
    relevance_mode = str(
        real_run_strategy.get(
            "relevance_mode",
            "first_relevant_only" if first_relevant_only else "all_relevant",
        )
    )
    entrypoint = str(real_run_strategy.get("entrypoint", REAL_PROFILE_DEFAULT_ENTRYPOINT))
    result_collapse = str(
        real_run_strategy.get("result_collapse", REAL_PROFILE_RESULT_COLLAPSE)
    )
    lines = [
        "# A/B/C/D Benchmark Analysis vs 2026-02 Reference",
        "",
        f"- generated_at_utc: {payload['generated_at_utc']}",
        f"- dataset_scope: {', '.join(payload.get('dataset_scope', []))}",
        f"- profiles: {', '.join(real_run_strategy.get('profiles', list(profiles.keys())))}",
        f"- sample_size_requested: {payload.get('sample_size_requested')}",
        f"- entrypoint: {entrypoint}",
        f"- factual_pool_cap: {real_run_strategy.get('factual_pool_cap')}",
        f"- relevance_mode: {relevance_mode}",
        f"- result_collapse: {result_collapse}",
        "",
        "## Profile Means (dataset-average)",
        "",
        "| Profile | HR@10 | MRR | NDCG@10 | Recall@10 | p95(ms) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    ordered_profile_keys = [
        key for key in REAL_PROFILE_DEFAULT_KEYS if key in profiles
    ]
    for profile_key in ordered_profile_keys:
        rows = profiles[profile_key]["rows"]
        lines.append(
            "| {profile} | {hr10:.3f} | {mrr:.3f} | {ndcg:.3f} | {recall:.3f} | {p95:.1f} |".format(
                profile=profile_key,
                hr10=_profile_dataset_mean(rows, "hr_at_10"),
                mrr=_profile_dataset_mean(rows, "mrr"),
                ndcg=_profile_dataset_mean(rows, "ndcg_at_10"),
                recall=_profile_dataset_mean(rows, "recall_at_10"),
                p95=_profile_dataset_mean(rows, "p95"),
            )
        )

    refs = SOTA_BENCHMARK_REFERENCES_2026_02
    d_rows = profiles.get("profile_d", {}).get("rows", [])
    d_ndcg = _profile_dataset_mean(d_rows, "ndcg_at_10") if d_rows else 0.0
    lines.extend(
        [
            "",
            "## 2026-02 Reference Ranges (from project docs)",
            "",
            "| Reference | Avg NDCG@10 |",
            "|---|---:|",
            f"| BM25 baseline | ~{refs['bm25_avg_ndcg10']:.2f} |",
            f"| Dense retrieval | ~{refs['dense_avg_ndcg10_min']:.2f}–{refs['dense_avg_ndcg10_max']:.2f} |",
            f"| Hybrid (embedding+BM25) | ~{refs['hybrid_avg_ndcg10_min']:.2f}–{refs['hybrid_avg_ndcg10_max']:.2f} |",
            f"| Hybrid + reranker | ~{refs['hybrid_rerank_avg_ndcg10_min']:.2f}–{refs['hybrid_rerank_avg_ndcg10_max']:.2f} |",
            f"| SOTA floor | ~{refs['sota_avg_ndcg10_floor']:.2f}+ |",
            "",
            "## Positioning",
            "",
            f"- Profile D dataset-mean NDCG@10: `{d_ndcg:.3f}`",
            f"- Relative to hybrid+reranker reference (`~{refs['hybrid_rerank_avg_ndcg10_min']:.2f}–{refs['hybrid_rerank_avg_ndcg10_max']:.2f}`): "
            + (
                "not-run"
                if not d_rows
                else (
                    "above-range"
                    if d_ndcg > refs["hybrid_rerank_avg_ndcg10_max"]
                    else "within-or-below-range"
                )
            ),
            "",
            "## Comparability Notes",
            "",
            "- 本报告使用项目内可落盘语料（SQuAD + BEIR NFCorpus）的小样本运行，不等价于完整 BEIR 全量评测。",
            f"- query relevance 采用 `{relevance_mode}` 口径；其中 `all_relevant` 更适合看覆盖率与排序密度，`first_relevant_only` 更适合做轻量回归。",
            f"- benchmark 入口为 `{entrypoint}`；其中 `mcp_search_memory` 会纳入 query preprocess / intent classification / search_memory contract。",
            f"- 指标计算前会做 `{result_collapse}`，避免 chunk 级重复命中放大或稀释 memory-level 指标。",
            "- Profile D 的有效性仍由 phase6 gate 判定（`embedding_fallback_hash` / `embedding_request_failed` / `reranker_request_failed`）。",
        ]
    )
    return "\n".join(lines) + "\n"
