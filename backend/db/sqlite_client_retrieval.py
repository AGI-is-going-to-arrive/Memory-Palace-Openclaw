import math
import re
import time
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .sqlite_models import EmbeddingCache, IndexMeta, Memory, MemoryChunk, MemoryChunkVec, Path

_LATIN_MMR_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_CJK_MMR_TOKEN_PATTERN = re.compile(
    r"[\u3040-\u309F\u30A0-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7A3\uF900-\uFAFF\U00020000-\U0002EBEF]+"
)


class SQLiteClientRetrievalMixin:
    _RECENT_PLAN_QUERY_PATTERNS = (
        re.compile(r"\b(tomorrow|tonight|later today|this evening|this afternoon|this weekend|next week|next month|the day after tomorrow|plan|plans|planned|planning|schedule)\b", re.IGNORECASE),
        re.compile(r"(明天|今晚|今天晚些时候|今天下午|今天晚上|周末|这周末|下周|下个月|后天|打算|计划|安排|要去|会去)"),
    )

    @staticmethod
    def _mmr_tokens(row: Dict[str, Any]) -> set[str]:
        snippet = str(row.get("snippet") or "")
        metadata = row.get("metadata")
        path = ""
        if isinstance(metadata, dict):
            path = str(metadata.get("path") or "")
        source = f"{snippet} {path}"
        return set(SQLiteClientRetrievalMixin._tokenize_mmr_source(source))

    @staticmethod
    def _tokenize_mmr_source(source: str) -> List[str]:
        normalized = unicodedata.normalize("NFC", str(source or "")).strip().casefold()
        if not normalized:
            return []

        latin_tokens: List[str] = []
        latin_seen: set[str] = set()
        cjk_tokens: List[str] = []
        cjk_seen: set[str] = set()
        merged_tokens: List[str] = []
        merged_seen: set[str] = set()

        def append_unique(target: List[str], seen: set[str], token: str) -> None:
            if token and token not in seen:
                seen.add(token)
                target.append(token)

        for token in _LATIN_MMR_TOKEN_PATTERN.findall(normalized):
            if _CJK_MMR_TOKEN_PATTERN.fullmatch(token):
                continue
            append_unique(latin_tokens, latin_seen, token)
        for chunk in _CJK_MMR_TOKEN_PATTERN.findall(normalized):
            append_unique(cjk_tokens, cjk_seen, chunk)
            for index in range(len(chunk) - 1):
                append_unique(cjk_tokens, cjk_seen, chunk[index : index + 2])

        buckets = (latin_tokens, cjk_tokens)
        indices = [0, 0]
        while True:
            progressed = False
            for bucket_index, bucket in enumerate(buckets):
                next_index = indices[bucket_index]
                if next_index >= len(bucket):
                    continue
                progressed = True
                indices[bucket_index] += 1
                append_unique(merged_tokens, merged_seen, bucket[next_index])
            if not progressed:
                break

        return merged_tokens

    @staticmethod
    def _jaccard_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
        if not tokens_a or not tokens_b:
            return 0.0
        union = tokens_a | tokens_b
        if not union:
            return 0.0
        return len(tokens_a & tokens_b) / len(union)

    @staticmethod
    def _redundancy_ratio(rows: List[Dict[str, Any]]) -> float:
        if len(rows) < 2:
            return 0.0

        token_cache = [SQLiteClientRetrievalMixin._mmr_tokens(item) for item in rows]
        redundancy: List[float] = []
        for idx, tokens in enumerate(token_cache):
            if idx == 0 or not tokens:
                redundancy.append(0.0)
                continue
            max_overlap = 0.0
            for prior in token_cache[:idx]:
                max_overlap = max(
                    max_overlap,
                    SQLiteClientRetrievalMixin._jaccard_similarity(tokens, prior),
                )
            redundancy.append(max_overlap)
        return round(sum(redundancy) / max(1, len(redundancy)), 6)

    @staticmethod
    def _length_normalization_score(query_text: str, content_length: Any) -> float:
        try:
            length_value = max(1.0, float(content_length or 0.0))
        except (TypeError, ValueError):
            return 0.0
        target_length = min(max(len((query_text or "").strip()) * 8, 160), 1200)
        ratio = length_value / max(1.0, float(target_length))
        return round(1.0 / (1.0 + abs(math.log(max(ratio, 1e-6)))), 6)

    def _access_signal_score(
        self,
        *,
        access_count: Any,
        last_accessed_at: Optional[datetime],
        now_value: datetime,
    ) -> float:
        try:
            normalized_access = max(0, int(access_count or 0))
        except (TypeError, ValueError):
            normalized_access = 0
        access_count_score = min(
            1.0, math.log1p(normalized_access) / math.log1p(16)
        )

        if isinstance(last_accessed_at, datetime):
            ref_now = (
                datetime.now(last_accessed_at.tzinfo)
                if last_accessed_at.tzinfo is not None
                else now_value
            )
            access_age_days = max(
                0.0, (ref_now - last_accessed_at).total_seconds() / 86400.0
            )
            recent_access_score = math.exp(
                -access_age_days / self._access_half_life_days
            )
        else:
            recent_access_score = 0.0
        return round(
            min(1.0, (access_count_score * 0.6) + (recent_access_score * 0.4)), 6
        )

    @staticmethod
    def _normalize_path_prefix_value(path_prefix: Any) -> str:
        raw = str(path_prefix or "").strip().replace("\\", "/").strip("/")
        if not raw:
            return ""
        return "/".join(part for part in raw.split("/") if part)

    @classmethod
    def _path_matches_prefix(cls, path: Any, path_prefix: Any) -> bool:
        prefix_value = cls._normalize_path_prefix_value(path_prefix)
        if not prefix_value:
            return True
        path_value = str(path or "").strip().strip("/")
        return path_value == prefix_value or path_value.startswith(f"{prefix_value}/")

    @classmethod
    def _build_path_prefix_sqlalchemy_condition(cls, column: Any, path_prefix: Any) -> Any:
        prefix_value = cls._normalize_path_prefix_value(path_prefix)
        if not prefix_value:
            return None
        return or_(column == prefix_value, column.startswith(f"{prefix_value}/"))

    def _append_path_prefix_where_clause(
        self,
        *,
        where_parts: List[str],
        where_params: Dict[str, Any],
        column_sql: str,
        path_prefix: Any,
        param_key_prefix: str = "path_prefix",
    ) -> str:
        prefix_value = self._normalize_path_prefix_value(path_prefix)
        if not prefix_value:
            return ""
        escaped_prefix = self._escape_like_pattern(prefix_value)
        exact_key = f"{param_key_prefix}_exact"
        subtree_key = f"{param_key_prefix}_subtree"
        where_parts.append(
            f"({column_sql} = :{exact_key} OR {column_sql} LIKE :{subtree_key} ESCAPE '\\')"
        )
        where_params[exact_key] = prefix_value
        where_params[subtree_key] = f"{escaped_prefix}/%"
        return prefix_value

    def _compute_candidate_score_components(
        self,
        *,
        item: Mapping[str, Any],
        query: str,
        prefix_value: str,
        now_value: datetime,
    ) -> Dict[str, float]:
        created_at = item.get("created_at")
        if isinstance(created_at, str):
            created_at = self._parse_iso_datetime(created_at)
        last_accessed_at = item.get("last_accessed_at")
        if isinstance(last_accessed_at, str):
            last_accessed_at = self._parse_iso_datetime(last_accessed_at)

        if isinstance(created_at, datetime):
            ref_now = (
                datetime.now(created_at.tzinfo)
                if created_at.tzinfo is not None
                else now_value
            )
            age_days = max(0.0, (ref_now - created_at).total_seconds() / 86400.0)
        else:
            age_days = 365.0

        priority_score = 1.0 / (1.0 + max(item.get("priority", 0), 0))
        recency_score = math.exp(-age_days / self._recency_half_life_days)
        path_prefix_score = (
            1.0 if prefix_value and str(item.get("path", "")).startswith(prefix_value) else 0.0
        )
        raw_vitality = float(item.get("vitality_score") or 0.0)
        normalized_vitality = max(
            0.0,
            min(1.0, raw_vitality / max(1.0, float(self._vitality_max_score))),
        )
        # Apply temporal decay: memories not accessed recently get a vitality
        # penalty in retrieval scoring, so stale high-vitality memories rank
        # lower than fresh mid-vitality ones (Mem0-inspired exp(-λΔt) approach).
        if self._vitality_temporal_decay_in_retrieval and isinstance(last_accessed_at, datetime):
            ref_now_access = (
                datetime.now(last_accessed_at.tzinfo)
                if last_accessed_at.tzinfo is not None
                else now_value
            )
            access_age_days = max(0.0, (ref_now_access - last_accessed_at).total_seconds() / 86400.0)
            access_decay = math.exp(-access_age_days / self._vitality_decay_half_life_days)
            vitality_score = normalized_vitality * access_decay
        else:
            vitality_score = normalized_vitality
        access_score = self._access_signal_score(
            access_count=item.get("access_count"),
            last_accessed_at=last_accessed_at,
            now_value=now_value,
        )
        path_value = str(item.get("path") or "").strip("/")
        query_targets_recent_plan = any(
            pattern.search(str(query or "")) for pattern in self._RECENT_PLAN_QUERY_PATTERNS
        )
        pending_event_score = (
            1.0
            if query_targets_recent_plan
            and "/pending/rule-capture/event/" in f"/{path_value}/"
            else 0.0
        )
        length_norm_score = self._length_normalization_score(
            query, item.get("chunk_length")
        )
        return {
            "vector": round(max(0.0, min(1.0, float(item.get("vector_score") or 0.0))), 6),
            "text": round(max(0.0, min(1.0, float(item.get("text_score") or 0.0))), 6),
            "context": round(
                max(0.0, min(1.0, float(item.get("context_score") or 0.0))), 6
            ),
            "priority": round(priority_score, 6),
            "recency": round(recency_score, 6),
            "path_prefix": round(path_prefix_score, 6),
            "vitality": round(vitality_score, 6),
            "access": round(access_score, 6),
            "pending_event": round(pending_event_score, 6),
            "length_norm": round(length_norm_score, 6),
        }

    def _compute_base_candidate_score(
        self,
        *,
        components: Mapping[str, Any],
        weights: Mapping[str, float],
    ) -> float:
        return (
            float(weights.get("vector", 0.0)) * float(components.get("vector", 0.0))
            + float(weights.get("text", 0.0)) * float(components.get("text", 0.0))
            + float(weights.get("context", 0.0)) * float(components.get("context", 0.0))
            + float(weights.get("priority", 0.0)) * float(components.get("priority", 0.0))
            + float(weights.get("recency", 0.0)) * float(components.get("recency", 0.0))
            + float(weights.get("path_prefix", 0.0))
            * float(components.get("path_prefix", 0.0))
            + (self._weight_vitality * float(components.get("vitality", 0.0)))
            + (self._weight_access * float(components.get("access", 0.0)))
            + (self._weight_pending_event * float(components.get("pending_event", 0.0)))
            + (self._weight_length_norm * float(components.get("length_norm", 0.0)))
        )

    def _build_rerank_plan(
        self,
        *,
        candidate_items: Sequence[Dict[str, Any]],
        base_scores: Mapping[int, float],
        max_results: int,
    ) -> Dict[str, Any]:
        rerankable_indices = [
            idx for idx, item in enumerate(candidate_items) if bool(item.get("rerankable"))
        ]
        if not rerankable_indices:
            return {
                "documents": [],
                "index_groups": [],
                "candidate_pool": 0,
                "group_count": 0,
                "selected_count": 0,
                "pruned_count": 0,
                "grouping": "memory" if self._reranker_group_by_memory else "chunk",
            }

        rerank_limit = self._reranker_top_n
        adaptive_limit = max(16, int(max_results) * 3)
        if rerank_limit <= 0:
            rerank_limit = adaptive_limit
        else:
            rerank_limit = min(rerank_limit, adaptive_limit)

        ordered = sorted(
            rerankable_indices,
            key=lambda idx: (
                -float(base_scores.get(idx, 0.0)),
                self._candidate_uri(
                    candidate_items[idx].get("domain"),
                    candidate_items[idx].get("path"),
                ),
                int(candidate_items[idx].get("chunk_id") or 0),
            ),
        )

        grouped_candidates: Dict[Tuple[str, Any], Dict[str, Any]] = {}
        for idx in ordered:
            item = candidate_items[idx]
            group_key: Tuple[str, Any]
            if self._reranker_group_by_memory and item.get("memory_id") is not None:
                try:
                    group_key = ("memory", int(item.get("memory_id")))
                except (TypeError, ValueError):
                    group_key = ("memory", str(item.get("memory_id")))
            else:
                group_key = ("chunk", idx)

            existing = grouped_candidates.get(group_key)
            if existing is None:
                grouped_candidates[group_key] = {
                    "representative_index": idx,
                    "indices": [idx],
                    "document": str(item.get("chunk_text") or ""),
                    "score": float(base_scores.get(idx, 0.0)),
                }
                continue

            existing["indices"].append(idx)
            current_score = float(base_scores.get(idx, 0.0))
            if current_score > float(existing.get("score", 0.0)) + 1e-12:
                existing["representative_index"] = idx
                existing["document"] = str(item.get("chunk_text") or "")
                existing["score"] = current_score

        grouped_order = sorted(
            grouped_candidates.values(),
            key=lambda item: (
                -float(item.get("score", 0.0)),
                int(item.get("representative_index", 0)),
            ),
        )
        selected_groups = grouped_order[:rerank_limit]

        return {
            "documents": [str(item.get("document") or "") for item in selected_groups],
            "index_groups": [list(item.get("indices") or []) for item in selected_groups],
            "candidate_pool": len(rerankable_indices),
            "group_count": len(grouped_order),
            "selected_count": len(selected_groups),
            "pruned_count": max(0, len(grouped_order) - len(selected_groups)),
            "grouping": "memory" if self._reranker_group_by_memory else "chunk",
        }

    @staticmethod
    def _candidate_uri(domain: Any, path: Any) -> str:
        return f"{str(domain or 'core')}://{str(path or '').strip('/')}"

    def _collapse_scored_results_by_uri(
        self, scored_results: Sequence[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if not scored_results:
            return [], {
                "applied": False,
                "rows_before": 0,
                "rows_after": 0,
                "collapsed_rows": 0,
                "collapsed_groups": 0,
            }

        collapsed: Dict[str, Dict[str, Any]] = {}
        group_sizes: Dict[str, int] = {}
        ordered_uris: List[str] = []
        for row in scored_results:
            uri = str(row.get("uri") or "")
            if not uri:
                continue

            group_sizes[uri] = group_sizes.get(uri, 0) + 1
            existing = collapsed.get(uri)
            if existing is None:
                collapsed[uri] = row
                ordered_uris.append(uri)
                continue

            existing_provenance = (
                existing.get("metadata", {}).get("search_provenance", {})
                if isinstance(existing.get("metadata"), dict)
                else {}
            )
            current_provenance = (
                row.get("metadata", {}).get("search_provenance", {})
                if isinstance(row.get("metadata"), dict)
                else {}
            )

            merged_stages = sorted(
                {
                    *(existing_provenance.get("stages", []) or []),
                    *(current_provenance.get("stages", []) or []),
                }
            )
            merged_stage_scores: Dict[str, float] = {}
            for source in (
                existing_provenance.get("stage_scores", {}),
                current_provenance.get("stage_scores", {}),
            ):
                if not isinstance(source, Mapping):
                    continue
                for stage_name, score in source.items():
                    try:
                        normalized_score = float(score or 0.0)
                    except (TypeError, ValueError):
                        continue
                    previous = merged_stage_scores.get(str(stage_name))
                    if previous is None or normalized_score > previous:
                        merged_stage_scores[str(stage_name)] = normalized_score

            if isinstance(existing.get("metadata"), dict):
                search_provenance = existing["metadata"].get("search_provenance")
                if isinstance(search_provenance, dict):
                    search_provenance["stages"] = merged_stages
                    search_provenance["stage_scores"] = {
                        stage: round(score, 6)
                        for stage, score in sorted(merged_stage_scores.items())
                    }

        collapsed_results: List[Dict[str, Any]] = []
        collapsed_groups = 0
        for uri in ordered_uris:
            row = collapsed[uri]
            group_size = int(group_sizes.get(uri, 0))
            if group_size > 1:
                collapsed_groups += 1
            metadata = row.get("metadata")
            if isinstance(metadata, dict):
                search_provenance = metadata.get("search_provenance")
                if isinstance(search_provenance, dict):
                    search_provenance["same_uri_collapsed"] = group_size > 1
                    search_provenance["same_uri_hits"] = group_size
            collapsed_results.append(row)

        return collapsed_results, {
            "applied": True,
            "rows_before": len(scored_results),
            "rows_after": len(collapsed_results),
            "collapsed_rows": max(0, len(scored_results) - len(collapsed_results)),
            "collapsed_groups": collapsed_groups,
        }

    @staticmethod
    def _direct_stage_signal(item: Mapping[str, Any]) -> float:
        stage_scores = item.get("stage_scores")
        if not isinstance(stage_scores, Mapping):
            return 0.0

        best_score = 0.0
        for stage_name in ("keyword", "semantic", "gist"):
            try:
                best_score = max(best_score, float(stage_scores.get(stage_name) or 0.0))
            except (TypeError, ValueError):
                continue
        return round(max(0.0, min(1.0, best_score)), 6)

    def _passes_search_scope(
        self,
        *,
        domain: str,
        path: str,
        priority: Any,
        created_at: Any,
        domain_filter: Optional[Any],
        path_prefix_filter: Optional[Any],
        priority_filter: Optional[Any],
        updated_after_filter: Optional[datetime],
    ) -> bool:
        if domain_filter and str(domain_filter) != str(domain):
            return False

        prefix_value = self._normalize_path_prefix_value(path_prefix_filter)
        if prefix_value and not self._path_matches_prefix(path, prefix_value):
            return False

        if priority_filter is not None:
            try:
                if int(priority or 0) > int(priority_filter):
                    return False
            except (TypeError, ValueError):
                pass

        if updated_after_filter is not None:
            normalized_created_at: Optional[datetime]
            if isinstance(created_at, datetime):
                normalized_created_at = self._normalize_db_datetime(created_at)
            elif isinstance(created_at, str):
                normalized_created_at = self._normalize_db_datetime(
                    self._parse_iso_datetime(created_at)
                )
            else:
                normalized_created_at = None

            if normalized_created_at is None:
                return False

            if normalized_created_at < self._normalize_db_datetime(updated_after_filter):
                return False

        return True

    def _select_context_recall_seeds(
        self,
        candidate_items: Sequence[Dict[str, Any]],
        *,
        max_results: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        memory_seeds: Dict[int, Dict[str, Any]] = {}
        path_seeds: List[Dict[str, Any]] = []
        min_signal = 0.16

        for item in candidate_items:
            memory_id_raw = item.get("memory_id")
            try:
                memory_id = int(memory_id_raw)
            except (TypeError, ValueError):
                continue

            signal = self._direct_stage_signal(item)
            if signal < min_signal:
                continue

            domain = str(item.get("domain") or "core")
            path = str(item.get("path") or "").strip("/")
            if not path:
                continue

            chunk_text = str(item.get("chunk_text") or "")
            char_start = int(item.get("char_start") or 0)
            char_end = int(item.get("char_end") or len(chunk_text))
            seed_item = {
                "memory_id": memory_id,
                "chunk_id": item.get("chunk_id"),
                "domain": domain,
                "path": path,
                "uri": self._candidate_uri(domain, path),
                "signal": signal,
                "chunk_text": chunk_text,
                "char_start": char_start,
                "char_end": max(char_start, char_end),
                "chunk_length": int(item.get("chunk_length") or len(chunk_text)),
                "created_at": item.get("created_at"),
                "vitality_score": float(item.get("vitality_score") or 0.0),
                "access_count": int(item.get("access_count") or 0),
                "last_accessed_at": item.get("last_accessed_at"),
                "gist_quality": float(item.get("gist_quality") or 0.0),
            }
            path_seeds.append(seed_item)

            existing = memory_seeds.get(memory_id)
            if existing is None or signal > float(existing.get("signal") or 0.0):
                memory_seeds[memory_id] = dict(seed_item)

        memory_limit = min(max(2, int(max_results)), 6)
        path_limit = min(max(4, int(max_results) * 2), 12)

        ordered_memory_seeds = sorted(
            memory_seeds.values(),
            key=lambda item: (-float(item.get("signal") or 0.0), str(item.get("uri") or "")),
        )[:memory_limit]
        ordered_path_seeds = sorted(
            path_seeds,
            key=lambda item: (-float(item.get("signal") or 0.0), str(item.get("uri") or "")),
        )[:path_limit]
        return ordered_memory_seeds, ordered_path_seeds

    async def _fetch_alias_candidate_rows(
        self,
        session: AsyncSession,
        *,
        memory_seeds: Sequence[Dict[str, Any]],
        seen_uris: set[str],
        domain_filter: Optional[Any],
        path_prefix_filter: Optional[Any],
        priority_filter: Optional[Any],
        updated_after_filter: Optional[datetime],
        per_memory_limit: int = 3,
    ) -> List[Dict[str, Any]]:
        if not memory_seeds:
            return []

        seed_by_memory = {
            int(seed["memory_id"]): seed
            for seed in memory_seeds
            if seed.get("memory_id") is not None
        }
        if not seed_by_memory:
            return []

        query = (
            select(Path, Memory)
            .join(Memory, Path.memory_id == Memory.id)
            .where(Path.memory_id.in_(list(seed_by_memory.keys())))
            .where(Memory.deprecated == False)
            .order_by(Path.priority.asc(), Path.path.asc(), Memory.created_at.desc())
        )
        result = await session.execute(query)

        alias_rows: List[Dict[str, Any]] = []
        per_memory_counts: Dict[int, int] = {}
        for path_obj, memory in result.all():
            seed = seed_by_memory.get(int(path_obj.memory_id))
            if seed is None:
                continue

            uri = self._candidate_uri(path_obj.domain, path_obj.path)
            if uri in seen_uris:
                continue

            if not self._passes_search_scope(
                domain=str(path_obj.domain),
                path=str(path_obj.path),
                priority=path_obj.priority,
                created_at=memory.created_at,
                domain_filter=domain_filter,
                path_prefix_filter=path_prefix_filter,
                priority_filter=priority_filter,
                updated_after_filter=updated_after_filter,
            ):
                continue

            memory_id = int(path_obj.memory_id)
            current_count = per_memory_counts.get(memory_id, 0)
            if current_count >= max(1, per_memory_limit):
                continue

            chunk_text = str(seed.get("chunk_text") or memory.content or "")
            char_start = int(seed.get("char_start") or 0)
            char_end = int(seed.get("char_end") or len(chunk_text))
            alias_rows.append(
                {
                    "chunk_id": seed.get("chunk_id"),
                    "memory_id": memory_id,
                    "chunk_text": chunk_text,
                    "char_start": char_start,
                    "char_end": max(char_start, char_end),
                    "domain": path_obj.domain,
                    "path": path_obj.path,
                    "priority": path_obj.priority,
                    "disclosure": path_obj.disclosure,
                    "created_at": memory.created_at,
                    "vitality_score": memory.vitality_score,
                    "access_count": memory.access_count,
                    "last_accessed_at": memory.last_accessed_at,
                    "chunk_length": int(seed.get("chunk_length") or len(chunk_text)),
                    "gist_quality": float(seed.get("gist_quality") or 0.0),
                    "context_score": round(
                        max(0.0, min(1.0, float(seed.get("signal") or 0.0) * 0.62)),
                        6,
                    ),
                    "recall_kind": "alias",
                    "origin_uri": seed.get("uri"),
                    "origin_memory_id": seed.get("memory_id"),
                }
            )
            per_memory_counts[memory_id] = current_count + 1

        alias_rows.sort(
            key=lambda row: (
                -float(row.get("context_score") or 0.0),
                int(row.get("priority") or 0),
                self._candidate_uri(row.get("domain"), row.get("path")),
            )
        )
        return alias_rows

    async def _fetch_ancestor_candidate_rows(
        self,
        session: AsyncSession,
        *,
        path_seeds: Sequence[Dict[str, Any]],
        seen_uris: set[str],
        domain_filter: Optional[Any],
        path_prefix_filter: Optional[Any],
        priority_filter: Optional[Any],
        updated_after_filter: Optional[datetime],
        max_hops: int = 3,
    ) -> List[Dict[str, Any]]:
        if not path_seeds:
            return []

        ancestor_requests: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for seed in path_seeds:
            path_value = str(seed.get("path") or "").strip("/")
            segments = [segment for segment in path_value.split("/") if segment]
            if len(segments) <= 1:
                continue

            seed_signal = float(seed.get("signal") or 0.0)
            if seed_signal <= 0.0:
                continue

            domain = str(seed.get("domain") or "core")
            max_depth = min(max_hops, len(segments) - 1)
            for depth in range(1, max_depth + 1):
                ancestor_path = "/".join(segments[:-depth])
                if not ancestor_path:
                    continue
                request_key = (domain, ancestor_path)
                context_score = round(
                    max(
                        0.0,
                        min(
                            1.0,
                            seed_signal * max(0.20, 0.70 - ((depth - 1) * 0.16)),
                        ),
                    ),
                    6,
                )
                existing = ancestor_requests.get(request_key)
                if existing is None or context_score > float(
                    existing.get("context_score") or 0.0
                ):
                    ancestor_requests[request_key] = {
                        "domain": domain,
                        "path": ancestor_path,
                        "context_score": context_score,
                        "origin_uri": seed.get("uri"),
                        "origin_memory_id": seed.get("memory_id"),
                        "ancestor_depth": depth,
                    }

        if not ancestor_requests:
            return []

        result = await session.execute(
            select(Path, Memory)
            .join(Memory, Path.memory_id == Memory.id)
            .where(
                or_(
                    *[
                        and_(Path.domain == domain, Path.path == path)
                        for domain, path in ancestor_requests.keys()
                    ]
                )
            )
            .where(Memory.deprecated == False)
        )
        matched_rows = result.all()
        gist_map = await self._get_latest_gists_map(
            session, [memory.id for _path_obj, memory in matched_rows]
        )

        ancestor_rows: List[Dict[str, Any]] = []
        for path_obj, memory in matched_rows:
            request = ancestor_requests.get((str(path_obj.domain), str(path_obj.path)))
            if request is None:
                continue

            uri = self._candidate_uri(path_obj.domain, path_obj.path)
            if uri in seen_uris:
                continue

            if not self._passes_search_scope(
                domain=str(path_obj.domain),
                path=str(path_obj.path),
                priority=path_obj.priority,
                created_at=memory.created_at,
                domain_filter=domain_filter,
                path_prefix_filter=path_prefix_filter,
                priority_filter=priority_filter,
                updated_after_filter=updated_after_filter,
            ):
                continue

            gist = gist_map.get(memory.id) or {}
            chunk_text = str(gist.get("gist_text") or memory.content or "")
            ancestor_rows.append(
                {
                    "chunk_id": None,
                    "memory_id": memory.id,
                    "chunk_text": chunk_text,
                    "char_start": 0,
                    "char_end": len(chunk_text),
                    "domain": path_obj.domain,
                    "path": path_obj.path,
                    "priority": path_obj.priority,
                    "disclosure": path_obj.disclosure,
                    "created_at": memory.created_at,
                    "vitality_score": memory.vitality_score,
                    "access_count": memory.access_count,
                    "last_accessed_at": memory.last_accessed_at,
                    "chunk_length": len(chunk_text),
                    "gist_quality": float(gist.get("quality_score") or 0.0),
                    "context_score": float(request.get("context_score") or 0.0),
                    "recall_kind": "ancestor",
                    "origin_uri": request.get("origin_uri"),
                    "origin_memory_id": request.get("origin_memory_id"),
                    "ancestor_depth": request.get("ancestor_depth"),
                }
            )

        ancestor_rows.sort(
            key=lambda row: (
                -float(row.get("context_score") or 0.0),
                int(row.get("ancestor_depth") or 0),
                int(row.get("priority") or 0),
                self._candidate_uri(row.get("domain"), row.get("path")),
            )
        )
        return ancestor_rows

    def _apply_mmr_rerank(
        self, scored_results: List[Dict[str, Any]], max_results: int
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        if not scored_results:
            return [], {
                "mmr_applied": False,
                "mmr_candidate_count": 0,
                "mmr_selected_count": 0,
            }

        selection_limit = max(1, int(max_results))
        candidate_limit = min(
            len(scored_results), selection_limit * max(1, self._mmr_candidate_factor)
        )
        candidate_pool = list(scored_results[:candidate_limit])
        if len(candidate_pool) <= 1:
            selected = candidate_pool[:selection_limit]
            return selected, {
                "mmr_applied": False,
                "mmr_candidate_count": len(candidate_pool),
                "mmr_selected_count": len(selected),
            }

        max_final = max(
            float(item.get("scores", {}).get("final", 0.0)) for item in candidate_pool
        )
        if max_final <= 0:
            max_final = 1.0

        token_cache = [self._mmr_tokens(item) for item in candidate_pool]
        selected_indices: List[int] = []
        remaining = set(range(len(candidate_pool)))

        while remaining and len(selected_indices) < selection_limit:
            best_idx: Optional[int] = None
            best_score = float("-inf")
            best_relevance = float("-inf")
            best_diversity = float("inf")

            for idx in remaining:
                raw_final = float(candidate_pool[idx].get("scores", {}).get("final", 0.0))
                relevance = max(0.0, raw_final) / max_final

                if not selected_indices:
                    diversity_penalty = 0.0
                else:
                    diversity_penalty = max(
                        self._jaccard_similarity(token_cache[idx], token_cache[picked])
                        for picked in selected_indices
                    )
                mmr_score = (self._mmr_lambda * relevance) - (
                    (1.0 - self._mmr_lambda) * diversity_penalty
                )

                if best_idx is None or mmr_score > best_score + 1e-12:
                    best_idx = idx
                    best_score = mmr_score
                    best_relevance = relevance
                    best_diversity = diversity_penalty
                    continue

                if abs(mmr_score - best_score) <= 1e-12:
                    if relevance > best_relevance + 1e-12:
                        best_idx = idx
                        best_relevance = relevance
                        best_diversity = diversity_penalty
                        continue
                    if (
                        abs(relevance - best_relevance) <= 1e-12
                        and diversity_penalty < best_diversity - 1e-12
                    ):
                        best_idx = idx
                        best_diversity = diversity_penalty
                        continue
                    if (
                        abs(relevance - best_relevance) <= 1e-12
                        and abs(diversity_penalty - best_diversity) <= 1e-12
                    ):
                        current_uri = str(candidate_pool[idx].get("uri") or "")
                        best_uri = str(candidate_pool[best_idx].get("uri") or "")
                        if current_uri < best_uri:
                            best_idx = idx

            if best_idx is None:
                break
            selected_indices.append(best_idx)
            remaining.discard(best_idx)

        selected_results = [candidate_pool[idx] for idx in selected_indices]
        return selected_results, {
            "mmr_applied": True,
            "mmr_candidate_count": len(candidate_pool),
            "mmr_selected_count": len(selected_results),
        }

    async def _fetch_gist_candidate_rows(
        self,
        session: Any,
        *,
        query: str,
        where_clause: str,
        where_params: Dict[str, Any],
        candidate_limit: int,
    ) -> List[Dict[str, Any]]:
        if not self._gist_recall_enabled or not query.strip():
            return []

        gist_rows: List[Dict[str, Any]] = []
        fts_query = self._build_safe_fts_query(query)
        if self._gist_fts_available and fts_query:
            try:
                gist_result = await session.execute(
                    text(
                        "SELECT "
                        "NULL AS chunk_id, "
                        "g.memory_id AS memory_id, "
                        "g.gist_text AS chunk_text, "
                        "0 AS char_start, "
                        "LENGTH(g.gist_text) AS char_end, "
                        "p.domain AS domain, "
                        "p.path AS path, "
                        "p.priority AS priority, "
                        "p.disclosure AS disclosure, "
                        "m.created_at AS created_at, "
                        "m.vitality_score AS vitality_score, "
                        "m.access_count AS access_count, "
                        "m.last_accessed_at AS last_accessed_at, "
                        "LENGTH(g.gist_text) AS chunk_length, "
                        "COALESCE(g.quality_score, 0.0) AS gist_quality "
                        "FROM memory_gists_fts "
                        "JOIN memory_gists g ON g.id = memory_gists_fts.rowid "
                        "JOIN ("
                        "  SELECT memory_id, MAX(id) AS latest_gist_id "
                        "  FROM memory_gists "
                        "  GROUP BY memory_id"
                        ") latest ON latest.latest_gist_id = g.id "
                        "JOIN memories m ON m.id = g.memory_id "
                        "JOIN paths p ON p.memory_id = g.memory_id "
                        f"WHERE {where_clause} "
                        "AND memory_gists_fts MATCH :fts_query "
                        "ORDER BY bm25(memory_gists_fts) ASC, "
                        "COALESCE(g.quality_score, 0.0) DESC, p.priority ASC, m.created_at DESC "
                        "LIMIT :candidate_limit"
                    ),
                    {
                        **where_params,
                        "fts_query": fts_query,
                        "candidate_limit": candidate_limit,
                    },
                )
                gist_rows = [dict(row) for row in gist_result.mappings().all()]
            except Exception as exc:
                await self._handle_gist_fts_runtime_error(
                    session,
                    exc,
                    context="search",
                )

        if gist_rows:
            return gist_rows

        gist_pattern = f"%{self._escape_like_pattern(query.lower())}%"
        gist_result = await session.execute(
            text(
                "SELECT "
                "NULL AS chunk_id, "
                "g.memory_id AS memory_id, "
                "g.gist_text AS chunk_text, "
                "0 AS char_start, "
                "LENGTH(g.gist_text) AS char_end, "
                "p.domain AS domain, "
                "p.path AS path, "
                "p.priority AS priority, "
                "p.disclosure AS disclosure, "
                "m.created_at AS created_at, "
                "m.vitality_score AS vitality_score, "
                "m.access_count AS access_count, "
                "m.last_accessed_at AS last_accessed_at, "
                "LENGTH(g.gist_text) AS chunk_length, "
                "COALESCE(g.quality_score, 0.0) AS gist_quality "
                "FROM memory_gists g "
                "JOIN ("
                "  SELECT memory_id, MAX(id) AS latest_gist_id "
                "  FROM memory_gists "
                "  GROUP BY memory_id"
                ") latest ON latest.latest_gist_id = g.id "
                "JOIN memories m ON m.id = g.memory_id "
                "JOIN paths p ON p.memory_id = g.memory_id "
                f"WHERE {where_clause} "
                "AND LOWER(g.gist_text) LIKE :gist_pattern ESCAPE '\\' "
                "ORDER BY COALESCE(g.quality_score, 0.0) DESC, p.priority ASC, m.created_at DESC "
                "LIMIT :candidate_limit"
            ),
            {
                **where_params,
                "gist_pattern": gist_pattern,
                "candidate_limit": candidate_limit,
            },
        )
        return [dict(row) for row in gist_result.mappings().all()]
