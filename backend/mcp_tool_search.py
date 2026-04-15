import logging
import inspect
import re
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from env_utils import parse_iso_datetime_with_options as shared_parse_iso_datetime
from search_api_compat import (
    SEARCH_METHOD_PRIORITY,
    search_api_fallback_reason,
    search_api_kind,
)

logger = logging.getLogger(__name__)


def _internal_error_message(operation: str) -> str:
    return f"{operation} failed. Check server logs for details."


def _highlight_snippet(snippet: str, query: str) -> str:
    """Insert ``<<`` / ``>>`` markers around query-term matches in *snippet*.

    The markers are lightweight plain-text delimiters that the frontend can
    convert into ``<mark>`` elements.  The function is case-insensitive and
    preserves the original casing of the matched text.
    """
    if not snippet or not query:
        return snippet
    # Tokenise query into individual words (drop very short noise tokens).
    tokens = [t for t in re.split(r"\s+", query.strip()) if len(t) >= 2]
    if not tokens:
        return snippet
    # Build a single alternation pattern, longest tokens first so that
    # longer matches take priority.
    tokens.sort(key=len, reverse=True)
    pattern = "|".join(re.escape(t) for t in tokens)
    try:
        return re.sub(
            f"({pattern})",
            r"<<\1>>",
            snippet,
            flags=re.IGNORECASE,
        )
    except re.error:
        return snippet


def _normalize_path_fragment(value: Optional[Any]) -> str:
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return ""
    return "/".join(part for part in raw.split("/") if part)


def _coerce_verbose_flag(value: Optional[Any]) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return bool(value)


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    return shared_parse_iso_datetime(
        value,
        naive_utc=True,
        raise_on_error=True,
        error_message=(
            f"Invalid datetime '{value}'. Use ISO-8601 like '2026-01-31T12:00:00Z'."
        ),
    )


def _normalize_search_filters(
    filters: Optional[Dict[str, Any]],
    *,
    valid_domains: List[str],
    parse_uri: Callable[[str], Tuple[str, str]],
) -> Dict[str, Any]:
    """Validate and normalize search filters."""
    if filters is None:
        return {}
    if not isinstance(filters, dict):
        raise ValueError(
            "filters must be an object with optional fields: "
            "domain/path_prefix/max_priority/updated_after."
        )

    allowed_keys = {"domain", "path_prefix", "max_priority", "updated_after"}
    unknown = set(filters.keys()) - allowed_keys
    if unknown:
        raise ValueError(
            f"Unknown filters: {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(allowed_keys))}."
        )

    normalized: Dict[str, Any] = {}

    domain = filters.get("domain")
    if domain is not None:
        domain_value = str(domain).strip().lower()
        if domain_value:
            if domain_value not in valid_domains:
                raise ValueError(
                    f"Unknown domain '{domain_value}'. "
                    f"Valid domains: {', '.join(valid_domains)}"
                )
            normalized["domain"] = domain_value

    path_prefix = filters.get("path_prefix")
    if path_prefix is not None:
        path_value = str(path_prefix).strip()
        if path_value:
            if "://" in path_value:
                parsed_domain, parsed_path = parse_uri(path_value)
                normalized.setdefault("domain", parsed_domain)
                normalized["path_prefix"] = parsed_path
            else:
                normalized["path_prefix"] = _normalize_path_fragment(path_value)

    max_priority = filters.get("max_priority")
    if max_priority is not None:
        try:
            normalized["max_priority"] = int(max_priority)
        except (TypeError, ValueError) as exc:
            raise ValueError("filters.max_priority must be an integer.") from exc

    updated_after = filters.get("updated_after")
    if updated_after is not None:
        parsed = _parse_iso_datetime(str(updated_after))
        if parsed is not None:
            normalized["updated_after"] = parsed.isoformat()

    return normalized


def _normalize_scope_hint(
    scope_hint: Optional[Any],
    *,
    valid_domains: List[str],
    parse_uri: Callable[[str], Tuple[str, str]],
) -> Dict[str, Any]:
    """Normalize scope hint from query side without changing schema contracts."""
    if scope_hint is None:
        return {
            "provided": False,
            "raw": None,
            "domain": None,
            "path_prefix": None,
            "strategy": "none",
        }

    raw_value = str(scope_hint).strip()
    if not raw_value:
        return {
            "provided": False,
            "raw": raw_value,
            "domain": None,
            "path_prefix": None,
            "strategy": "none",
        }

    lowered_raw = raw_value.lower()
    host_workspace_memory_markers = (
        "memory.md",
        "memory/*.md",
        "workspace memory",
        "workspace-memory",
    )
    if any(marker in lowered_raw for marker in host_workspace_memory_markers):
        return {
            "provided": False,
            "raw": raw_value,
            "domain": None,
            "path_prefix": None,
            "strategy": "host_workspace_memory_ignored",
        }

    if "://" in raw_value:
        parsed_domain, parsed_path = parse_uri(raw_value)
        return {
            "provided": True,
            "raw": raw_value,
            "domain": parsed_domain,
            "path_prefix": parsed_path or None,
            "strategy": "uri_prefix" if parsed_path else "domain_uri",
        }

    lowered = raw_value.lower()
    if lowered in valid_domains:
        return {
            "provided": True,
            "raw": raw_value,
            "domain": lowered,
            "path_prefix": None,
            "strategy": "domain",
        }

    prefix = _normalize_path_fragment(raw_value)
    return {
        "provided": bool(prefix),
        "raw": raw_value,
        "domain": None,
        "path_prefix": prefix or None,
        "strategy": "path_prefix" if prefix else "none",
    }


def _merge_scope_hint_with_filters(
    *,
    normalized_filters: Dict[str, Any],
    scope_hint: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Merge scope hint into filters with filter-first conflict handling."""
    merged = dict(normalized_filters)
    provided = bool(scope_hint.get("provided"))
    hint_domain = scope_hint.get("domain")
    hint_path_prefix = scope_hint.get("path_prefix")
    conflicts: List[str] = []
    applied = False
    domain_conflict = False

    if provided and isinstance(hint_domain, str) and hint_domain:
        existing_domain = merged.get("domain")
        if existing_domain is None:
            merged["domain"] = hint_domain
            applied = True
        elif str(existing_domain) != hint_domain:
            conflicts.append("domain_conflict")
            domain_conflict = True

    if provided and isinstance(hint_path_prefix, str) and hint_path_prefix:
        if domain_conflict:
            resolution = {
                "provided": provided,
                "raw": scope_hint.get("raw"),
                "strategy": "filters_preferred",
                "applied": applied,
                "effective": {
                    "domain": merged.get("domain"),
                    "path_prefix": merged.get("path_prefix"),
                },
                "conflicts": conflicts,
            }
            return merged, resolution
        existing_prefix = merged.get("path_prefix")
        hint_prefix_norm = hint_path_prefix.strip("/")
        if existing_prefix is None:
            merged["path_prefix"] = hint_prefix_norm
            applied = True
        else:
            existing_prefix_norm = str(existing_prefix).strip("/")
            if not existing_prefix_norm:
                merged["path_prefix"] = hint_prefix_norm
                applied = True
            elif existing_prefix_norm == hint_prefix_norm:
                pass
            elif existing_prefix_norm.startswith(hint_prefix_norm):
                pass
            elif hint_prefix_norm.startswith(existing_prefix_norm):
                merged["path_prefix"] = hint_prefix_norm
                applied = True
            else:
                conflicts.append("path_prefix_conflict")

    resolution = {
        "provided": provided,
        "raw": scope_hint.get("raw"),
        "strategy": (
            str(scope_hint.get("strategy") or "none")
            if applied
            else ("filters_preferred" if provided else "none")
        ),
        "applied": applied,
        "effective": {
            "domain": merged.get("domain"),
            "path_prefix": merged.get("path_prefix"),
        },
        "conflicts": conflicts,
    }
    return merged, resolution


def _normalize_search_item(
    item: Any,
    *,
    parse_uri: Callable[[str], Tuple[str, str]],
    make_uri: Callable[[str, str], str],
) -> Dict[str, Any]:
    """Normalize one sqlite search result item."""
    if not isinstance(item, dict):
        return {"raw": item}

    metadata_obj = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    scores_obj = item.get("scores") if isinstance(item.get("scores"), dict) else {}
    char_range = item.get("char_range")

    domain = item.get("domain")
    path = item.get("path")
    uri = item.get("uri")

    if domain is None:
        domain = metadata_obj.get("domain")
    if path is None:
        path = metadata_obj.get("path")

    if uri and (domain is None or path is None):
        try:
            parsed_domain, parsed_path = parse_uri(str(uri))
            if domain is None:
                domain = parsed_domain
            if path is None:
                path = parsed_path
        except ValueError:
            pass

    if uri is None and domain is not None and path is not None:
        uri = make_uri(str(domain), str(path))

    snippet = (
        item.get("snippet")
        or item.get("content_snippet")
        or item.get("preview")
        or item.get("excerpt")
    )
    if snippet is None and item.get("content"):
        snippet = str(item["content"])[:200]

    priority = item.get("priority")
    if priority is None:
        priority = metadata_obj.get("priority")
    if priority is not None:
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            pass

    chunk_start = item.get("chunk_start")
    chunk_end = item.get("chunk_end")
    if isinstance(char_range, (list, tuple)) and len(char_range) >= 2:
        chunk_start = char_range[0]
        chunk_end = char_range[1]

    normalized: Dict[str, Any] = {
        "uri": uri,
        "domain": domain,
        "path": path,
        "memory_id": item.get("memory_id", item.get("id")),
        "name": item.get("name"),
        "priority": priority,
        "score": item.get("score", scores_obj.get("final")),
        "semantic_score": item.get("semantic_score", scores_obj.get("vector")),
        "keyword_score": item.get("keyword_score", scores_obj.get("text")),
        "snippet": snippet,
        "updated_at": item.get("updated_at")
        or metadata_obj.get("updated_at")
        or item.get("created_at"),
        "chunk_id": item.get("chunk_id"),
        "chunk_start": chunk_start,
        "chunk_end": chunk_end,
        "match_type": item.get("match_type"),
        "source": item.get("source"),
        "disclosure": item.get("disclosure", metadata_obj.get("disclosure")),
    }
    return {k: v for k, v in normalized.items() if v is not None}


def _extract_search_payload(
    raw_result: Any,
    *,
    parse_uri: Callable[[str], Tuple[str, str]],
    make_uri: Callable[[str, str], str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Extract results list + metadata from unknown sqlite return shape."""
    metadata: Dict[str, Any] = {}
    raw_items: List[Any] = []

    if isinstance(raw_result, dict):
        if isinstance(raw_result.get("results"), list):
            raw_items = raw_result["results"]
        elif isinstance(raw_result.get("items"), list):
            raw_items = raw_result["items"]
        elif isinstance(raw_result.get("matches"), list):
            raw_items = raw_result["matches"]
        metadata = {
            k: v
            for k, v in raw_result.items()
            if k not in {"results", "items", "matches"}
        }
    elif isinstance(raw_result, list):
        raw_items = raw_result
    elif raw_result is not None:
        metadata["raw_result"] = raw_result

    normalized_items = [
        _normalize_search_item(item, parse_uri=parse_uri, make_uri=make_uri)
        for item in raw_items
    ]
    return normalized_items, metadata


def _apply_local_filters_to_results(
    results: List[Dict[str, Any]], filters: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Apply requested filters locally when backend cannot enforce them."""
    filtered = list(results)
    degradation_reasons: List[str] = []

    domain = filters.get("domain")
    if domain:
        filtered = [item for item in filtered if item.get("domain") == domain]

    path_prefix = filters.get("path_prefix")
    if path_prefix:
        dropped = 0
        kept: List[Dict[str, Any]] = []
        for item in filtered:
            path = item.get("path")
            if path and str(path).startswith(path_prefix):
                kept.append(item)
            else:
                dropped += 1
        if dropped:
            degradation_reasons.append(
                f"path_prefix filter dropped {dropped} result(s) with missing/non-matching path."
            )
        filtered = kept

    max_priority = filters.get("max_priority")
    if max_priority is not None:
        dropped = 0
        kept = []
        for item in filtered:
            priority = item.get("priority")
            if isinstance(priority, int) and priority <= max_priority:
                kept.append(item)
            else:
                dropped += 1
        if dropped:
            degradation_reasons.append(
                f"max_priority filter dropped {dropped} result(s) with missing/non-matching priority."
            )
        filtered = kept

    updated_after = filters.get("updated_after")
    if updated_after:
        cutoff = _parse_iso_datetime(updated_after)
        dropped = 0
        comparable = 0
        dropped_without_timestamp = 0
        kept = []
        for item in filtered:
            updated_raw = item.get("updated_at")
            if not updated_raw:
                dropped_without_timestamp += 1
                dropped += 1
                continue
            try:
                updated = _parse_iso_datetime(str(updated_raw))
            except ValueError:
                dropped_without_timestamp += 1
                dropped += 1
                continue
            if updated is None or cutoff is None:
                dropped_without_timestamp += 1
                dropped += 1
                continue
            comparable += 1
            if updated >= cutoff:
                kept.append(item)
            else:
                dropped += 1

        if comparable == 0 and filtered:
            degradation_reasons.append(
                "updated_after filter dropped results locally because they have no parseable updated_at."
            )
        else:
            if dropped_without_timestamp:
                degradation_reasons.append(
                    "updated_after filter dropped "
                    f"{dropped_without_timestamp} result(s) with missing/non-parseable updated_at."
                )
            if dropped:
                degradation_reasons.append(
                    f"updated_after filter dropped {dropped} result(s)."
                )
        filtered = kept

    return filtered, degradation_reasons


def _extract_result_domain_path(
    item: Dict[str, Any],
    *,
    parse_uri: Callable[[str], Tuple[str, str]],
) -> Tuple[Optional[str], Optional[str]]:
    domain = item.get("domain")
    path = item.get("path")
    if domain and path:
        return str(domain), str(path)
    uri = item.get("uri")
    if not uri:
        return None, None
    try:
        parsed_domain, parsed_path = parse_uri(str(uri))
    except ValueError:
        return None, None
    return parsed_domain, parsed_path


def _search_result_display_score(item: Dict[str, Any]) -> Optional[float]:
    candidates = [
        item.get("score"),
        item.get("keyword_score"),
        item.get("semantic_score"),
    ]
    for value in candidates:
        try:
            if value is None:
                continue
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _sort_search_results_for_response(
    results: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    indexed_results = list(enumerate(results))

    def _sort_key(entry: Tuple[int, Dict[str, Any]]) -> Tuple[int, float, int]:
        index, item = entry
        score = _search_result_display_score(item)
        if score is None:
            return (1, 0.0, index)
        return (0, -score, index)

    return [item for _, item in sorted(indexed_results, key=_sort_key)]


def _strip_search_runtime_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in item.items()
        if not str(key).startswith("_session_first_")
    }


def _prune_search_payload_for_non_verbose(payload: Dict[str, Any]) -> Dict[str, Any]:
    trimmed = dict(payload)
    for key in (
        "query_preprocess",
        "intent_profile",
        "intent_llm_enabled",
        "intent_llm_applied",
        "strategy_template",
        "candidate_pool_size",
        "session_queue_count",
        "global_queue_count",
        "session_first_metrics",
        "backend_method",
        "backend_metadata",
        "scope_hint",
        "scope_hint_applied",
        "scope_strategy_applied",
        "scope_effective",
        "scope_conflicts",
        "intent_applied",
        "strategy_template_applied",
        "candidate_multiplier_applied",
    ):
        trimmed.pop(key, None)
    return trimmed


async def _revalidate_search_results(
    results: List[Dict[str, Any]],
    *,
    client: Any,
    parse_uri: Callable[[str], Tuple[str, str]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    get_memory_by_path = getattr(client, "get_memory_by_path", None)
    if not callable(get_memory_by_path):
        return list(results), {
            "revalidation_attempted": False,
            "revalidation_dropped": 0,
            "revalidation_refreshed": 0,
            "revalidation_errors": 0,
        }

    kept: List[Dict[str, Any]] = []
    dropped = 0
    refreshed = 0
    errors = 0

    for item in results:
        domain, path = _extract_result_domain_path(item, parse_uri=parse_uri)
        if not domain or not path:
            kept.append(dict(item))
            continue

        try:
            try:
                current = await get_memory_by_path(path, domain, reinforce_access=False)
            except TypeError:
                current = await get_memory_by_path(path, domain)
        except Exception:
            errors += 1
            kept.append(dict(item))
            continue

        if current is None:
            dropped += 1
            continue

        normalized = dict(item)
        if current.get("id") is not None:
            normalized["memory_id"] = current.get("id")
        updated_at = (
            current.get("updated_at")
            or (
                current.get("metadata", {}).get("updated_at")
                if isinstance(current.get("metadata"), dict)
                else None
            )
            or current.get("created_at")
        )
        if updated_at:
            normalized["updated_at"] = updated_at

        snippet_source = (
            current.get("gist_text")
            or current.get("content")
            or current.get("segment")
        )
        if isinstance(snippet_source, str):
            refreshed_snippet = snippet_source.strip()[:300]
            if refreshed_snippet and refreshed_snippet != normalized.get("snippet"):
                normalized["snippet"] = refreshed_snippet
                refreshed += 1

        kept.append(normalized)

    return kept, {
        "revalidation_attempted": True,
        "revalidation_dropped": dropped,
        "revalidation_refreshed": refreshed,
        "revalidation_errors": errors,
    }


async def search_memory_impl(
    query: str,
    mode: Optional[str] = None,
    max_results: Optional[int] = None,
    candidate_multiplier: Optional[int] = None,
    include_session: Optional[bool] = None,
    filters: Optional[Dict[str, Any]] = None,
    scope_hint: Optional[str] = None,
    verbose: Optional[bool] = None,
    *,
    to_json: Callable[[Dict[str, Any]], str],
    get_sqlite_client: Callable[[], Any],
    runtime_state: Any,
    get_session_id: Callable[[], str],
    try_client_method_variants: Callable[..., Awaitable[Tuple[Optional[str], Dict[str, Any], Any]]],
    merge_session_global_results: Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Tuple[List[Dict[str, Any]], Dict[str, Any]]],
    search_result_identity: Callable[[Dict[str, Any]], Any],
    safe_int: Callable[[Any, int], int],
    record_session_hit: Callable[..., Awaitable[None]],
    record_flush_event: Callable[[str], Awaitable[None]],
    parse_uri: Callable[[str], Tuple[str, str]],
    make_uri: Callable[[str, str], str],
    valid_domains: List[str],
    default_search_mode: str,
    allowed_search_modes: set[str],
    default_search_max_results: int,
    default_search_candidate_multiplier: int,
    search_hard_max_results: int,
    search_hard_max_candidate_multiplier: int,
    enable_session_first_search: bool,
    intent_llm_enabled: bool,
) -> str:
    degraded_reasons: List[str] = []

    try:
        if not isinstance(query, str):
            return to_json({"ok": False, "error": "query must be a string."})
        query_value = query.strip()
        if not query_value:
            return to_json({"ok": False, "error": "query must not be empty."})
        verbose_enabled = _coerce_verbose_flag(verbose)

        mode_requested = (mode or default_search_mode).strip().lower()
        if mode_requested not in allowed_search_modes:
            return to_json(
                {
                    "ok": False,
                    "error": (
                        f"Invalid mode '{mode_requested}'. "
                        f"Allowed: {', '.join(sorted(allowed_search_modes))}."
                    ),
                }
            )

        resolved_max_results = (
            default_search_max_results if max_results is None else int(max_results)
        )
        resolved_candidate_multiplier = (
            default_search_candidate_multiplier
            if candidate_multiplier is None
            else int(candidate_multiplier)
        )

        if resolved_max_results <= 0:
            return to_json({"ok": False, "error": "max_results must be > 0."})
        if resolved_candidate_multiplier <= 0:
            return to_json(
                {"ok": False, "error": "candidate_multiplier must be > 0."}
            )

        resolved_max_results = min(resolved_max_results, search_hard_max_results)
        resolved_candidate_multiplier = min(
            resolved_candidate_multiplier, search_hard_max_candidate_multiplier
        )

        raw_filters = filters
        scope_hint_value: Optional[Any] = scope_hint
        if isinstance(raw_filters, dict):
            raw_filters = dict(raw_filters)
            if "scope_hint" in raw_filters:
                if scope_hint_value is None:
                    scope_hint_value = raw_filters.get("scope_hint")
                raw_filters.pop("scope_hint", None)

        normalized_filters = _normalize_search_filters(
            raw_filters,
            valid_domains=valid_domains,
            parse_uri=parse_uri,
        )
        normalized_scope_hint = _normalize_scope_hint(
            scope_hint_value,
            valid_domains=valid_domains,
            parse_uri=parse_uri,
        )
        normalized_filters, scope_resolution = _merge_scope_hint_with_filters(
            normalized_filters=normalized_filters,
            scope_hint=normalized_scope_hint,
        )
        for conflict in scope_resolution.get("conflicts", []):
            degraded_reasons.append(f"scope_hint_{conflict}")

        client = get_sqlite_client()

        query_preprocess: Dict[str, Any] = {
            "original_query": query_value,
            "normalized_query": query_value,
            "rewritten_query": query_value,
            "tokens": [],
            "changed": False,
        }
        intent_profile: Dict[str, Any] = {
            "intent": None,
            "strategy_template": "default",
            "method": "fallback",
            "confidence": 0.0,
            "signals": ["fallback_default"],
        }

        preprocess_fn = getattr(client, "preprocess_query", None)
        if callable(preprocess_fn):
            try:
                preprocess_payload = preprocess_fn(query_value)
                if isinstance(preprocess_payload, dict):
                    query_preprocess.update(preprocess_payload)
            except Exception:
                degraded_reasons.append("query_preprocess_failed")
        else:
            degraded_reasons.append("query_preprocess_unavailable")

        query_effective = (
            str(query_preprocess.get("rewritten_query") or "").strip() or query_value
        )

        classify_fn = None
        fallback_classify_fn = getattr(client, "classify_intent", None)
        classify_with_intent_llm = False
        if intent_llm_enabled:
            classify_fn = getattr(client, "classify_intent_with_llm", None)
            classify_with_intent_llm = callable(classify_fn)
            if not callable(classify_fn):
                degraded_reasons.append("intent_llm_unavailable")
                classify_fn = fallback_classify_fn
        else:
            classify_fn = fallback_classify_fn
        if callable(classify_fn):
            try:
                classify_payload = classify_fn(query_value, query_effective)
                if inspect.isawaitable(classify_payload):
                    classify_payload = await classify_payload
                if isinstance(classify_payload, dict):
                    intent_profile.update(classify_payload)
                    classify_degrade_reasons = classify_payload.get("degrade_reasons")
                    if isinstance(classify_degrade_reasons, list):
                        for reason in classify_degrade_reasons:
                            if isinstance(reason, str) and reason.strip():
                                degraded_reasons.append(reason.strip())
            except Exception:
                degraded_reasons.append("intent_classification_failed")
                if classify_with_intent_llm and callable(fallback_classify_fn):
                    try:
                        fallback_payload = fallback_classify_fn(
                            query_value,
                            query_effective,
                        )
                        if inspect.isawaitable(fallback_payload):
                            fallback_payload = await fallback_payload
                        if isinstance(fallback_payload, dict):
                            intent_profile.update(fallback_payload)
                            degraded_reasons.append("intent_llm_fallback_rule_applied")
                            fallback_degrade_reasons = fallback_payload.get(
                                "degrade_reasons"
                            )
                            if isinstance(fallback_degrade_reasons, list):
                                for reason in fallback_degrade_reasons:
                                    if isinstance(reason, str) and reason.strip():
                                        degraded_reasons.append(reason.strip())
                    except Exception:
                        degraded_reasons.append(
                            "intent_classification_fallback_failed"
                        )
        else:
            degraded_reasons.append("intent_classification_unavailable")

        intent_for_search: Optional[Dict[str, Any]] = None
        if intent_profile.get("intent") in {
            "factual",
            "exploratory",
            "temporal",
            "causal",
        }:
            intent_for_search = intent_profile

        candidate_pool_size = min(
            search_hard_max_results,
            resolved_max_results * resolved_candidate_multiplier,
        )
        if include_session is None:
            include_session_queue = enable_session_first_search
        elif isinstance(include_session, str):
            include_session_queue = (
                include_session.strip().lower() in {"1", "true", "yes", "on", "enabled"}
            )
        else:
            include_session_queue = bool(include_session)

        kwargs_variants = [
            {
                "query": query_effective,
                "mode": mode_requested,
                "max_results": resolved_max_results,
                "candidate_multiplier": resolved_candidate_multiplier,
                "filters": normalized_filters,
                "intent_profile": intent_for_search,
            },
            {
                "query": query_effective,
                "mode": mode_requested,
                "max_results": resolved_max_results,
                "candidate_multiplier": resolved_candidate_multiplier,
                "filters": normalized_filters,
            },
            {
                "query": query_effective,
                "mode": mode_requested,
                "max_results": resolved_max_results,
                "candidate_multiplier": resolved_candidate_multiplier,
                **normalized_filters,
            },
            {
                "query": query_effective,
                "mode": mode_requested,
                "limit": candidate_pool_size,
                **normalized_filters,
            },
            {
                "query": query_effective,
                "limit": candidate_pool_size,
                "domain": normalized_filters.get("domain"),
            },
        ]
        method_name, kwargs_used, raw_result = await try_client_method_variants(
            client,
            list(SEARCH_METHOD_PRIORITY),
            kwargs_variants,
        )

        if method_name is None:
            return to_json(
                {
                    "ok": False,
                    "error": "No compatible sqlite_client search API found.",
                }
            )

        resolved_search_api_kind = search_api_kind(method_name)
        fallback_reason = search_api_fallback_reason(method_name)
        if fallback_reason is not None:
            degraded_reasons.append(fallback_reason)

        if (
            intent_for_search is not None
            and kwargs_used is not None
            and "intent_profile" not in kwargs_used
        ):
            degraded_reasons.append("intent_profile_not_supported_by_search_api")

        raw_results, backend_metadata = _extract_search_payload(
            raw_result,
            parse_uri=parse_uri,
            make_uri=make_uri,
        )
        filtered_results, local_filter_reasons = _apply_local_filters_to_results(
            raw_results, normalized_filters
        )
        degraded_reasons.extend(local_filter_reasons)
        backend_degrade_reasons = backend_metadata.get("degrade_reasons")
        if isinstance(backend_degrade_reasons, list):
            for reason in backend_degrade_reasons:
                if isinstance(reason, str):
                    degraded_reasons.append(reason)
        elif isinstance(backend_degrade_reasons, str):
            degraded_reasons.append(backend_degrade_reasons)

        if kwargs_used and "mode" not in kwargs_used and mode_requested != "keyword":
            degraded_reasons.append(
                f"sqlite_client.{method_name} did not accept mode; "
                "search downgraded to keyword behavior."
            )

        if kwargs_used and "candidate_multiplier" not in kwargs_used:
            degraded_reasons.append(
                "candidate_multiplier may not be enforced by sqlite_client; "
                "MCP applied top-k truncation only."
            )

        mode_applied = str(backend_metadata.get("mode", mode_requested)).lower()
        if kwargs_used and "mode" not in kwargs_used:
            mode_applied = "keyword"

        if mode_applied not in allowed_search_modes:
            mode_applied = "keyword"

        if mode_applied != mode_requested:
            degraded_reasons.append(
                f"Requested mode '{mode_requested}' but applied '{mode_applied}'."
            )

        session_results: List[Dict[str, Any]] = []
        if include_session_queue:
            try:
                session_results = await runtime_state.session_cache.search(
                    session_id=get_session_id(),
                    query=query_value,
                    limit=resolved_max_results,
                )
            except Exception as exc:
                logger.warning(
                    "Session cache lookup failed for query %r: %s",
                    query_value,
                    exc,
                )
                degraded_reasons.append(
                    "session queue lookup failed; continued with global retrieval only."
                )

        merged_results, session_first_metrics = merge_session_global_results(
            session_results=session_results,
            global_results=filtered_results,
        )
        revalidated_results, revalidation_metrics = await _revalidate_search_results(
            merged_results,
            client=client,
            parse_uri=parse_uri,
        )
        sorted_results = _sort_search_results_for_response(revalidated_results)
        final_results = sorted_results[:resolved_max_results]
        session_before = sum(
            1 for item in sorted_results if item.get("_session_first_source") == "session"
        )
        global_before = sum(
            1 for item in sorted_results if item.get("_session_first_source") == "global"
        )
        merged_before = len(sorted_results)
        session_after = 0
        global_after = 0
        for item in final_results:
            if not isinstance(item, dict):
                continue
            if item.get("_session_first_source") == "session":
                session_after += 1
            else:
                global_after += 1
        session_first_metrics.update(revalidation_metrics)
        session_first_metrics["session_contributed_before_truncation"] = session_before
        session_first_metrics["global_contributed_before_truncation"] = global_before
        session_first_metrics["merged_candidates_before_truncation"] = merged_before
        session_first_metrics["session_contributed"] = session_after
        session_first_metrics["global_contributed"] = global_after
        session_first_metrics["merged_candidates"] = len(final_results)
        session_first_metrics["returned_candidates"] = len(final_results)
        session_first_metrics["sorted_by_score"] = True
        response_results = []
        for item in final_results:
            if not isinstance(item, dict):
                continue
            stripped = _strip_search_runtime_fields(item)
            if stripped.get("snippet") and query_effective:
                stripped["snippet"] = _highlight_snippet(
                    stripped["snippet"], query_effective
                )
            response_results.append(stripped)
        payload: Dict[str, Any] = {
            "ok": True,
            "query": query_value,
            "query_effective": query_effective,
            "query_preprocess": query_preprocess,
            "intent": intent_profile.get("intent") or "unknown",
            "intent_profile": intent_profile,
            "intent_llm_enabled": intent_llm_enabled,
            "intent_llm_applied": bool(intent_profile.get("intent_llm_applied")),
            "strategy_template": intent_profile.get(
                "strategy_template", "default"
            ),
            "mode_requested": mode_requested,
            "mode_applied": mode_applied,
            "max_results": resolved_max_results,
            "candidate_multiplier": resolved_candidate_multiplier,
            "candidate_pool_size": candidate_pool_size,
            "session_first_enabled": include_session_queue,
            "session_queue_count": len(session_results),
            "global_queue_count": len(filtered_results),
            "session_first_metrics": session_first_metrics,
            "filters": normalized_filters,
            "scope_hint": scope_resolution.get("raw"),
            "scope_hint_applied": bool(scope_resolution.get("applied")),
            "scope_strategy_applied": scope_resolution.get("strategy"),
            "scope_effective": scope_resolution.get("effective", {}),
            "count": len(response_results),
            "results": response_results,
            "backend_method": f"sqlite_client.{method_name}",
            "search_api_kind": resolved_search_api_kind,
            "degraded": bool(degraded_reasons) or bool(backend_metadata.get("degraded")),
            "semantic_search_unavailable": "embedding_fallback_hash" in degraded_reasons,
        }
        if scope_resolution.get("conflicts"):
            payload["scope_conflicts"] = scope_resolution.get("conflicts")

        if backend_metadata:
            backend_metadata.setdefault("search_api_kind", resolved_search_api_kind)
            payload["backend_metadata"] = backend_metadata
            applied_metadata = (
                backend_metadata.get("metadata")
                if isinstance(backend_metadata.get("metadata"), dict)
                else backend_metadata
            )
            if isinstance(applied_metadata, dict):
                payload["intent_applied"] = applied_metadata.get("intent")
                payload["strategy_template_applied"] = applied_metadata.get(
                    "strategy_template"
                )
                payload["candidate_multiplier_applied"] = applied_metadata.get(
                    "candidate_multiplier_applied"
                )

        if degraded_reasons:
            payload["degrade_reasons"] = list(dict.fromkeys(degraded_reasons))
        if not verbose_enabled:
            payload = _prune_search_payload_for_non_verbose(payload)

        post_search_degrade_reasons: List[str] = []
        try:
            for item in response_results:
                uri = item.get("uri")
                snippet = item.get("snippet")
                if not uri or not snippet:
                    continue
                memory_id_raw = item.get("memory_id")
                memory_id_value: Optional[int]
                if memory_id_raw is None:
                    memory_id_value = None
                else:
                    parsed_id = safe_int(memory_id_raw, default=-1)
                    memory_id_value = parsed_id if parsed_id >= 0 else None
                await record_session_hit(
                    uri=str(uri),
                    memory_id=memory_id_value,
                    snippet=str(snippet)[:300],
                    priority=item.get("priority"),
                    source="search_memory",
                    updated_at=item.get("updated_at"),
                )
        except Exception as exc:
            logger.warning(
                "search_memory session hit recording failed for %r: %s",
                query_value,
                exc,
            )
            post_search_degrade_reasons.append("record_session_hit_failed")
        try:
            await record_flush_event(f"search '{query_value}'")
        except Exception as exc:
            logger.warning(
                "search_memory flush recording failed for %r: %s",
                query_value,
                exc,
            )
            post_search_degrade_reasons.append("record_flush_event_failed")

        if post_search_degrade_reasons:
            degraded_reasons.extend(post_search_degrade_reasons)
            payload["degraded"] = True
            payload["degrade_reasons"] = list(dict.fromkeys(degraded_reasons))

        return to_json(payload)

    except Exception as e:
        logger.exception("search_memory failed for %r: %s", query_value, e, exc_info=e)
        return to_json({"ok": False, "error": _internal_error_message("search_memory")})
