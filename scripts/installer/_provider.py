#!/usr/bin/env python3
from __future__ import annotations

from ._constants import *
from ._utils import *
from ._utils import _metadata_key, _normalize_port


# ---------------------------------------------------------------------------
# IMP-1: Multi-source host model hint reuse
# ---------------------------------------------------------------------------


@dataclass
class ProviderSeed:
    """A single configuration hint discovered from any source."""

    value: str
    source: str          # SEED_SOURCE_* constant
    confidence: str      # SEED_CONFIDENCE_* constant
    provider_type: str   # "embedding" | "reranker" | "llm"
    field: str           # "api_base" | "api_key" | "model" | "dim"


def _empty_seed_buckets() -> dict[str, list[ProviderSeed]]:
    """Return a typed-bucket dict keyed by provider type."""
    return {pt: [] for pt in PROVIDER_TYPES}


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    """Read and parse a JSON file; return *None* on any failure."""
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return None


def _looks_like_embedding_model(name: str) -> bool:
    lowered = name.lower()
    return any(ind in lowered for ind in EMBEDDING_MODEL_INDICATORS)


def _looks_like_reranker_model(name: str) -> bool:
    lowered = name.lower()
    return any(ind in lowered for ind in RERANKER_MODEL_INDICATORS)


def _infer_provider_type_from_model_name(name: str) -> str:
    """Best-effort heuristic; defaults to ``"llm"``."""
    if _looks_like_embedding_model(name):
        return "embedding"
    if _looks_like_reranker_model(name):
        return "reranker"
    return "llm"


def _deep_get(data: dict[str, Any], *keys: str) -> Any:
    """Traverse nested dicts; return *None* on miss."""
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


# ---- Host config loader ---------------------------------------------------


def load_openclaw_host_hints(
    config_path: Path | None = None,
) -> dict[str, list[ProviderSeed]]:
    """Read OpenClaw host configuration and return seeds keyed by provider type.

    Searches ``OPENCLAW_CONFIG_SEARCH_PATHS`` unless *config_path* is given.
    Returns an empty dict when no config is found or parsing fails.
    """
    buckets = _empty_seed_buckets()

    # Locate config file
    paths_to_try: tuple[Path, ...] = (
        (config_path,) if config_path is not None else OPENCLAW_CONFIG_SEARCH_PATHS
    )
    config: dict[str, Any] | None = None
    for candidate in paths_to_try:
        try:
            resolved = Path(candidate).expanduser().resolve()
        except Exception:  # noqa: BLE001
            continue
        if resolved.is_file():
            config = _safe_read_json(resolved)
            if config is not None:
                break

    if config is None:
        return buckets

    source = SEED_SOURCE_HOST_CONFIG
    confidence = SEED_CONFIDENCE_MEDIUM

    # --- agents.defaults.model → LLM seed (HC-2: strictly LLM only) --------
    raw_default_model = _deep_get(config, "agents", "defaults", "model")
    # OpenClaw schema: may be a dict with `.primary` or a plain string
    if isinstance(raw_default_model, dict):
        default_model = str(raw_default_model.get("primary") or raw_default_model.get("id") or "").strip()
    else:
        default_model = str(raw_default_model or "").strip()
    if default_model:
        buckets["llm"].append(
            ProviderSeed(
                value=default_model,
                source=source,
                confidence=confidence,
                provider_type="llm",
                field=SEED_FIELD_MODEL,
            )
        )

    # --- agents.defaults.memorySearch → embedding hint ----------------------
    memory_search = _deep_get(config, "agents", "defaults", "memorySearch")
    if isinstance(memory_search, dict):
        ms_model = str(memory_search.get("model") or "").strip()
        if ms_model and _looks_like_embedding_model(ms_model):
            buckets["embedding"].append(
                ProviderSeed(
                    value=ms_model,
                    source=source,
                    confidence=confidence,
                    provider_type="embedding",
                    field=SEED_FIELD_MODEL,
                )
            )
        ms_api_base = str(memory_search.get("apiBase") or memory_search.get("api_base") or "").strip()
        if ms_api_base:
            buckets["embedding"].append(
                ProviderSeed(
                    value=ms_api_base,
                    source=source,
                    confidence=confidence,
                    provider_type="embedding",
                    field=SEED_FIELD_API_BASE,
                )
            )
    elif isinstance(memory_search, str) and memory_search.strip():
        # Scalar value — may be a model name
        ms_val = memory_search.strip()
        if _looks_like_embedding_model(ms_val):
            buckets["embedding"].append(
                ProviderSeed(
                    value=ms_val,
                    source=source,
                    confidence=SEED_CONFIDENCE_LOW,
                    provider_type="embedding",
                    field=SEED_FIELD_MODEL,
                )
            )

    # --- models.providers → per-provider seeds (type-isolated) ---------------
    providers_block = _deep_get(config, "models", "providers")
    if isinstance(providers_block, list):
        for entry in providers_block:
            if not isinstance(entry, dict):
                continue
            # OpenClaw schema: model name may be in `model`, `modelId`, `id`,
            # or nested in `models[0].id`
            entry_model = str(
                entry.get("model")
                or entry.get("modelId")
                or entry.get("id")
                or ""
            ).strip()
            if not entry_model:
                # Try nested models array: models[].id
                nested_models = entry.get("models")
                if isinstance(nested_models, list) and nested_models:
                    first = nested_models[0]
                    if isinstance(first, dict):
                        entry_model = str(first.get("id") or first.get("model") or "").strip()
                    elif isinstance(first, str):
                        entry_model = first.strip()
            entry_api_base = str(
                entry.get("baseUrl")
                or entry.get("apiBase")
                or entry.get("api_base")
                or entry.get("baseURL")
                or ""
            ).strip()
            entry_api_key = str(entry.get("apiKey") or entry.get("api_key") or "").strip()
            explicit_type = str(entry.get("type") or entry.get("provider_type") or "").strip().lower()

            if explicit_type in PROVIDER_TYPES:
                ptype = explicit_type
            elif entry_model:
                ptype = _infer_provider_type_from_model_name(entry_model)
            else:
                continue  # cannot determine type — skip

            if entry_model:
                buckets[ptype].append(
                    ProviderSeed(
                        value=entry_model,
                        source=source,
                        confidence=confidence,
                        provider_type=ptype,
                        field=SEED_FIELD_MODEL,
                    )
                )
            if entry_api_base:
                buckets[ptype].append(
                    ProviderSeed(
                        value=entry_api_base,
                        source=source,
                        confidence=confidence,
                        provider_type=ptype,
                        field=SEED_FIELD_API_BASE,
                    )
                )
            if entry_api_key:
                buckets[ptype].append(
                    ProviderSeed(
                        value=entry_api_key,
                        source=source,
                        confidence=SEED_CONFIDENCE_LOW,
                        provider_type=ptype,
                        field=SEED_FIELD_API_KEY,
                    )
                )
    elif isinstance(providers_block, dict):
        for provider_name, entry in providers_block.items():
            if not isinstance(entry, dict):
                continue
            entry_model = str(
                entry.get("model")
                or entry.get("modelId")
                or entry.get("id")
                or ""
            ).strip()
            if not entry_model:
                nested_models = entry.get("models")
                if isinstance(nested_models, list) and nested_models:
                    first = nested_models[0]
                    if isinstance(first, dict):
                        entry_model = str(first.get("id") or first.get("model") or "").strip()
                    elif isinstance(first, str):
                        entry_model = first.strip()
            entry_api_base = str(
                entry.get("baseUrl")
                or entry.get("apiBase")
                or entry.get("api_base")
                or entry.get("baseURL")
                or ""
            ).strip()
            entry_api_key = str(entry.get("apiKey") or entry.get("api_key") or "").strip()
            explicit_type = str(entry.get("type") or entry.get("provider_type") or "").strip().lower()

            if explicit_type in PROVIDER_TYPES:
                ptype = explicit_type
            elif entry_model:
                ptype = _infer_provider_type_from_model_name(entry_model)
            else:
                ptype = "llm"  # dict-keyed providers are often chat models

            if entry_model:
                buckets[ptype].append(
                    ProviderSeed(
                        value=entry_model,
                        source=source,
                        confidence=confidence,
                        provider_type=ptype,
                        field=SEED_FIELD_MODEL,
                    )
                )
            if entry_api_base:
                buckets[ptype].append(
                    ProviderSeed(
                        value=entry_api_base,
                        source=source,
                        confidence=confidence,
                        provider_type=ptype,
                        field=SEED_FIELD_API_BASE,
                    )
                )
            if entry_api_key:
                buckets[ptype].append(
                    ProviderSeed(
                        value=entry_api_key,
                        source=source,
                        confidence=SEED_CONFIDENCE_LOW,
                        provider_type=ptype,
                        field=SEED_FIELD_API_KEY,
                    )
                )

    # --- env / auth profile → API keys (optional) ---------------------------
    env_block = _deep_get(config, "env")
    if isinstance(env_block, dict):
        for env_key, env_val in env_block.items():
            val = str(env_val or "").strip()
            if not val:
                continue
            upper = env_key.upper()
            if "EMBEDDING" in upper and ("KEY" in upper or "API_KEY" in upper):
                buckets["embedding"].append(
                    ProviderSeed(
                        value=val,
                        source=source,
                        confidence=SEED_CONFIDENCE_LOW,
                        provider_type="embedding",
                        field=SEED_FIELD_API_KEY,
                    )
                )
            elif "RERANKER" in upper and ("KEY" in upper or "API_KEY" in upper):
                buckets["reranker"].append(
                    ProviderSeed(
                        value=val,
                        source=source,
                        confidence=SEED_CONFIDENCE_LOW,
                        provider_type="reranker",
                        field=SEED_FIELD_API_KEY,
                    )
                )
            elif ("OPENAI" in upper or "LLM" in upper) and ("KEY" in upper or "API_KEY" in upper):
                buckets["llm"].append(
                    ProviderSeed(
                        value=val,
                        source=source,
                        confidence=SEED_CONFIDENCE_LOW,
                        provider_type="llm",
                        field=SEED_FIELD_API_KEY,
                    )
                )

    return buckets


# ---- Local fallback provider discovery (IMP-1b) ----------------------------


def discover_local_provider_candidates() -> dict[str, list[ProviderSeed]]:
    """Probe the local Ollama instance and return low-confidence fallback seeds.

    This is the lowest-priority layer in the provider seed chain.  It probes
    ``http://127.0.0.1:11434/api/tags`` with a short timeout and classifies
    discovered models into *embedding* / *llm* buckets using the same
    heuristics as :func:`_looks_like_embedding_model` and
    :func:`_looks_like_reranker_model`.

    Returns an empty-bucket dict when Ollama is unreachable, times out, or
    returns an unexpected response format.  No exceptions are propagated.
    """
    buckets = _empty_seed_buckets()

    try:
        req = Request(
            f"{OLLAMA_DEFAULT_URL}/api/tags",
            headers={
                "Accept": "application/json",
                "User-Agent": "memory-palace-installer/1.0",
            },
            method="GET",
        )
        with urlopen(req, timeout=LOCAL_DISCOVERY_TIMEOUT_SEC) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
    except Exception:  # noqa: BLE001 — Ollama not running / timeout / bad JSON
        return buckets

    if not isinstance(data, dict):
        return buckets

    models = data.get("models")
    if not isinstance(models, list):
        return buckets

    source = SEED_SOURCE_LOCAL_DISCOVERY
    confidence = SEED_CONFIDENCE_LOW
    api_base = OLLAMA_DEFAULT_URL

    # Track which provider types we've already added an api_base seed for,
    # so we emit at most one api_base seed per type.
    api_base_emitted: set[str] = set()

    for entry in models:
        if not isinstance(entry, dict):
            continue
        model_name = str(entry.get("name") or entry.get("model") or "").strip()
        if not model_name:
            continue

        ptype = _infer_provider_type_from_model_name(model_name)

        # Rerankers rarely run on Ollama; skip to avoid noise.
        if ptype == "reranker":
            continue

        # Model seed
        buckets[ptype].append(
            ProviderSeed(
                value=model_name,
                source=source,
                confidence=confidence,
                provider_type=ptype,
                field=SEED_FIELD_MODEL,
            )
        )

        # Corresponding api_base seed (one per provider type)
        if ptype not in api_base_emitted:
            buckets[ptype].append(
                ProviderSeed(
                    value=api_base,
                    source=source,
                    confidence=confidence,
                    provider_type=ptype,
                    field=SEED_FIELD_API_BASE,
                )
            )
            api_base_emitted.add(ptype)

    return buckets


# ---- Runtime-env / explicit-args seed helpers ------------------------------


def _seeds_from_override_dict(
    overrides: dict[str, str | None],
    *,
    source: str,
    confidence: str,
) -> dict[str, list[ProviderSeed]]:
    """Convert an overrides dict (as returned by ``runtime_overrides_from_env``)
    into provider-typed seed buckets."""
    buckets = _empty_seed_buckets()
    _field_map: dict[str, tuple[str, str]] = {
        "embedding_api_base": ("embedding", SEED_FIELD_API_BASE),
        "embedding_api_key": ("embedding", SEED_FIELD_API_KEY),
        "embedding_model": ("embedding", SEED_FIELD_MODEL),
        "embedding_dim": ("embedding", SEED_FIELD_DIM),
        "reranker_api_base": ("reranker", SEED_FIELD_API_BASE),
        "reranker_api_key": ("reranker", SEED_FIELD_API_KEY),
        "reranker_model": ("reranker", SEED_FIELD_MODEL),
        "llm_api_base": ("llm", SEED_FIELD_API_BASE),
        "llm_api_key": ("llm", SEED_FIELD_API_KEY),
        "llm_model": ("llm", SEED_FIELD_MODEL),
        "write_guard_llm_api_base": ("llm", SEED_FIELD_API_BASE),
        "write_guard_llm_api_key": ("llm", SEED_FIELD_API_KEY),
        "write_guard_llm_model": ("llm", SEED_FIELD_MODEL),
        "compact_gist_llm_api_base": ("llm", SEED_FIELD_API_BASE),
        "compact_gist_llm_api_key": ("llm", SEED_FIELD_API_KEY),
        "compact_gist_llm_model": ("llm", SEED_FIELD_MODEL),
    }
    for key, (ptype, field) in _field_map.items():
        val = str(overrides.get(key) or "").strip()
        if val:
            buckets[ptype].append(
                ProviderSeed(
                    value=val,
                    source=source,
                    confidence=confidence,
                    provider_type=ptype,
                    field=field,
                )
            )
    return buckets


def _merge_seed_buckets(
    *bucket_dicts: dict[str, list[ProviderSeed]],
) -> dict[str, list[ProviderSeed]]:
    """Merge multiple bucket dicts, preserving insertion order (= priority)."""
    merged = _empty_seed_buckets()
    for bd in bucket_dicts:
        for ptype in PROVIDER_TYPES:
            merged[ptype].extend(bd.get(ptype, []))
    return merged


# ---- Public aggregator ----------------------------------------------------


def collect_provider_seeds(
    *,
    explicit_args: dict[str, str | None] | None = None,
    chat_inputs: dict[str, str | None] | None = None,
    runtime_env: Mapping[str, str] | None = None,
    host_config_path: Path | None = None,
    process_env: Mapping[str, str] | None = None,
) -> dict[str, list[ProviderSeed]]:
    """Collect provider seeds from all available sources, respecting priority.

    Priority (high → low):
        1. *explicit_args*  — CLI flags passed to the installer
        2. *chat_inputs*    — values typed by the user during interactive prompt
        3. *runtime_env*    — existing ``runtime.env`` on disk
        4. host config      — OpenClaw host ``config.json``
        5. *process_env*    — inherited shell / process environment
        6. local discovery  — Ollama loopback probe (lowest-priority fallback)

    **HC-2 guarantee**: seeds from ``agents.defaults.model`` (LLM) are *never*
    placed into the embedding or reranker buckets. Type isolation is enforced
    at every extraction point.

    Returns ``{"embedding": [...], "reranker": [...], "llm": [...]}``.
    """
    layers: list[dict[str, list[ProviderSeed]]] = []

    # 1. explicit CLI args
    if explicit_args:
        layers.append(
            _seeds_from_override_dict(
                {k: v for k, v in explicit_args.items() if v is not None},
                source=SEED_SOURCE_EXPLICIT,
                confidence=SEED_CONFIDENCE_HIGH,
            )
        )

    # 2. chat / interactive inputs
    if chat_inputs:
        layers.append(
            _seeds_from_override_dict(
                {k: v for k, v in chat_inputs.items() if v is not None},
                source=SEED_SOURCE_CHAT_INPUT,
                confidence=SEED_CONFIDENCE_HIGH,
            )
        )

    # 3. persisted runtime.env
    if runtime_env is not None:
        overrides = runtime_overrides_from_env(runtime_env)
        layers.append(
            _seeds_from_override_dict(
                overrides,
                source=SEED_SOURCE_RUNTIME_ENV,
                confidence=SEED_CONFIDENCE_HIGH,
            )
        )

    # 4. host config (OpenClaw config.json)
    try:
        host_seeds = load_openclaw_host_hints(config_path=host_config_path)
    except Exception:  # noqa: BLE001
        host_seeds = _empty_seed_buckets()
    layers.append(host_seeds)

    # 5. process environment
    if process_env is not None:
        proc_overrides = runtime_overrides_from_env(process_env)
        layers.append(
            _seeds_from_override_dict(
                proc_overrides,
                source=SEED_SOURCE_PROCESS_ENV,
                confidence=SEED_CONFIDENCE_LOW,
            )
        )

    # 6. local fallback discovery (lowest priority — Ollama loopback probe)
    try:
        local_seeds = discover_local_provider_candidates()
    except Exception:  # noqa: BLE001
        local_seeds = _empty_seed_buckets()
    layers.append(local_seeds)

    return _merge_seed_buckets(*layers)


def _first_seed_value(
    buckets: Mapping[str, list[ProviderSeed]],
    provider_type: str,
    field: str,
) -> str | None:
    for seed in buckets.get(provider_type, []):
        if seed.field != field:
            continue
        rendered = str(seed.value or "").strip()
        if rendered:
            return rendered
    return None


def host_config_runtime_overrides(
    config_path: Path | None = None,
) -> dict[str, str | None]:
    buckets = load_openclaw_host_hints(config_path=config_path)
    return {
        "embedding_api_base": _first_seed_value(buckets, "embedding", SEED_FIELD_API_BASE),
        "embedding_api_key": _first_seed_value(buckets, "embedding", SEED_FIELD_API_KEY),
        "embedding_model": _first_seed_value(buckets, "embedding", SEED_FIELD_MODEL),
        "embedding_dim": _first_seed_value(buckets, "embedding", SEED_FIELD_DIM),
        "reranker_api_base": _first_seed_value(buckets, "reranker", SEED_FIELD_API_BASE),
        "reranker_api_key": _first_seed_value(buckets, "reranker", SEED_FIELD_API_KEY),
        "reranker_model": _first_seed_value(buckets, "reranker", SEED_FIELD_MODEL),
        "llm_api_base": _first_seed_value(buckets, "llm", SEED_FIELD_API_BASE),
        "llm_api_key": _first_seed_value(buckets, "llm", SEED_FIELD_API_KEY),
        "llm_model": _first_seed_value(buckets, "llm", SEED_FIELD_MODEL),
    }


def build_profile_seed(
    *,
    profile: str,
    setup_root_path: Path,
    existing_env: Mapping[str, str] | None = None,
    host_platform: str | None = None,
    preserve_existing: bool = False,
) -> dict[str, str]:
    base = load_env_file(env_example_path())
    existing = dict(existing_env or {})
    template = load_env_file(profile_template_path(profile, host_platform))
    merged: dict[str, str] = {}
    merged.update(base)
    if preserve_existing:
        merged.update(template)
        merged.update(existing)
    else:
        merged.update(existing)
        merged.update(template)
    existing_database_url = str(existing.get("DATABASE_URL") or "").strip()
    merged["DATABASE_URL"] = existing_database_url or sqlite_url_for_file(default_database_file(setup_root_path))
    return merged


def _first_non_placeholder_override(*values: str | None) -> str | None:
    for value in values:
        rendered = str(value or "").strip()
        if not is_placeholder_profile_value(rendered):
            return rendered
    return None


def runtime_overrides_from_env(env_source: Mapping[str, str] | None = None) -> dict[str, str | None]:
    return {
        "embedding_api_base": _first_non_placeholder_override(
            env_value_with_aliases(env_source, "RETRIEVAL_EMBEDDING_API_BASE"),
            env_value(env_source, "ROUTER_API_BASE"),
        ),
        "embedding_api_key": _first_non_placeholder_override(
            env_value_with_aliases(env_source, "RETRIEVAL_EMBEDDING_API_KEY"),
            env_value(env_source, "ROUTER_API_KEY"),
        ),
        "embedding_model": _first_non_placeholder_override(
            env_value_with_aliases(env_source, "RETRIEVAL_EMBEDDING_MODEL"),
            env_value(env_source, "ROUTER_EMBEDDING_MODEL"),
        ),
        "embedding_dim": _first_non_placeholder_override(env_value(env_source, "RETRIEVAL_EMBEDDING_DIM")),
        "reranker_api_base": _first_non_placeholder_override(
            env_value_with_aliases(env_source, "RETRIEVAL_RERANKER_API_BASE"),
            env_value(env_source, "ROUTER_API_BASE"),
        ),
        "reranker_api_key": _first_non_placeholder_override(
            env_value_with_aliases(env_source, "RETRIEVAL_RERANKER_API_KEY"),
            env_value(env_source, "ROUTER_API_KEY"),
        ),
        "reranker_model": _first_non_placeholder_override(env_value_with_aliases(env_source, "RETRIEVAL_RERANKER_MODEL")),
        "llm_api_base": _first_non_placeholder_override(
            env_value(env_source, "LLM_API_BASE"),
            env_value(env_source, "INTENT_LLM_API_BASE"),
            env_value(env_source, "LLM_RESPONSES_URL"),
            env_value(env_source, "OPENAI_BASE_URL"),
            env_value(env_source, "OPENAI_API_BASE"),
            env_value(env_source, "WRITE_GUARD_LLM_API_BASE"),
        ),
        "llm_api_key": _first_non_placeholder_override(
            env_value(env_source, "LLM_API_KEY"),
            env_value(env_source, "INTENT_LLM_API_KEY"),
            env_value(env_source, "OPENAI_API_KEY"),
            env_value(env_source, "WRITE_GUARD_LLM_API_KEY"),
        ),
        "llm_model": _first_non_placeholder_override(
            env_value(env_source, "LLM_MODEL_NAME"),
            env_value(env_source, "LLM_MODEL"),
            env_value(env_source, "INTENT_LLM_MODEL"),
            env_value(env_source, "OPENAI_MODEL"),
            env_value(env_source, "WRITE_GUARD_LLM_MODEL"),
        ),
        "write_guard_llm_api_base": _first_non_placeholder_override(
            env_value(env_source, "WRITE_GUARD_LLM_API_BASE"),
            env_value(env_source, "LLM_API_BASE"),
            env_value(env_source, "INTENT_LLM_API_BASE"),
            env_value(env_source, "LLM_RESPONSES_URL"),
            env_value(env_source, "OPENAI_BASE_URL"),
            env_value(env_source, "OPENAI_API_BASE"),
        ),
        "write_guard_llm_api_key": _first_non_placeholder_override(
            env_value(env_source, "WRITE_GUARD_LLM_API_KEY"),
            env_value(env_source, "LLM_API_KEY"),
            env_value(env_source, "INTENT_LLM_API_KEY"),
            env_value(env_source, "OPENAI_API_KEY"),
        ),
        "write_guard_llm_model": _first_non_placeholder_override(
            env_value(env_source, "WRITE_GUARD_LLM_MODEL"),
            env_value(env_source, "LLM_MODEL_NAME"),
            env_value(env_source, "LLM_MODEL"),
            env_value(env_source, "INTENT_LLM_MODEL"),
            env_value(env_source, "OPENAI_MODEL"),
        ),
        "compact_gist_llm_api_base": _first_non_placeholder_override(
            env_value(env_source, "COMPACT_GIST_LLM_API_BASE"),
            env_value(env_source, "LLM_API_BASE"),
            env_value(env_source, "INTENT_LLM_API_BASE"),
            env_value(env_source, "LLM_RESPONSES_URL"),
            env_value(env_source, "OPENAI_BASE_URL"),
            env_value(env_source, "OPENAI_API_BASE"),
        ),
        "compact_gist_llm_api_key": _first_non_placeholder_override(
            env_value(env_source, "COMPACT_GIST_LLM_API_KEY"),
            env_value(env_source, "LLM_API_KEY"),
            env_value(env_source, "INTENT_LLM_API_KEY"),
            env_value(env_source, "OPENAI_API_KEY"),
        ),
        "compact_gist_llm_model": _first_non_placeholder_override(
            env_value(env_source, "COMPACT_GIST_LLM_MODEL"),
            env_value(env_source, "LLM_MODEL_NAME"),
            env_value(env_source, "LLM_MODEL"),
            env_value(env_source, "INTENT_LLM_MODEL"),
            env_value(env_source, "OPENAI_MODEL"),
        ),
    }


def current_process_runtime_overrides() -> dict[str, str | None]:
    return runtime_overrides_from_env(os.environ)


def persisted_requested_profile(env_source: Mapping[str, str] | None) -> str | None:
    if not env_source:
        return None
    for key in (_metadata_key("PROFILE_REQUESTED"), _metadata_key("PROFILE_EFFECTIVE")):
        candidate = str(env_source.get(key) or "").strip().lower()
        if candidate in PROFILE_VALUES:
            return candidate
    return None


RETRIEVAL_PROVIDER_RUNTIME_ENV_KEYS = (
    "ROUTER_API_BASE",
    "ROUTER_API_KEY",
    "ROUTER_EMBEDDING_MODEL",
    "RETRIEVAL_EMBEDDING_API_BASE",
    "RETRIEVAL_EMBEDDING_API_KEY",
    "RETRIEVAL_EMBEDDING_MODEL",
    "RETRIEVAL_EMBEDDING_DIM",
    "RETRIEVAL_RERANKER_API_BASE",
    "RETRIEVAL_RERANKER_API_KEY",
    "RETRIEVAL_RERANKER_MODEL",
    "RETRIEVAL_RERANKER_WEIGHT",
    "RETRIEVAL_RERANKER_FALLBACK_API_BASE",
    "RETRIEVAL_RERANKER_FALLBACK_API_KEY",
    "RETRIEVAL_RERANKER_FALLBACK_MODEL",
    "RETRIEVAL_RERANKER_FALLBACK_PROVIDER",
    "RETRIEVAL_RERANKER_FALLBACK_TIMEOUT_SEC",
)

def strip_retrieval_provider_runtime_env(data: dict[str, str]) -> None:
    for key in RETRIEVAL_PROVIDER_RUNTIME_ENV_KEYS:
        data.pop(key, None)


def sync_optional_llm_runtime_flags(data: dict[str, str]) -> None:
    llm_feature_specs = (
        (
            "WRITE_GUARD_LLM_ENABLED",
            (
                "WRITE_GUARD_LLM_API_BASE",
                "WRITE_GUARD_LLM_API_KEY",
                "WRITE_GUARD_LLM_MODEL",
            ),
        ),
        (
            "COMPACT_GIST_LLM_ENABLED",
            (
                "COMPACT_GIST_LLM_API_BASE",
                "COMPACT_GIST_LLM_API_KEY",
                "COMPACT_GIST_LLM_MODEL",
            ),
        ),
        (
            "INTENT_LLM_ENABLED",
            (
                "INTENT_LLM_API_BASE",
                "INTENT_LLM_API_KEY",
                "INTENT_LLM_MODEL",
            ),
        ),
    )
    for enabled_key, config_keys in llm_feature_specs:
        configured = all(not is_placeholder_profile_value(data.get(key)) for key in config_keys)
        data[enabled_key] = "true" if configured else "false"


def apply_runtime_field_overrides(
    data: dict[str, str],
    *,
    database_path: str | None = None,
    sse_url: str | None = None,
    mcp_api_key: str | None = None,
    allow_insecure_local: bool | None = None,
    backend_api_host: str | None = None,
    backend_api_port: str | int | None = None,
    dashboard_host: str | None = None,
    dashboard_port: str | int | None = None,
    embedding_api_base: str | None = None,
    embedding_api_key: str | None = None,
    embedding_model: str | None = None,
    embedding_dim: str | int | None = None,
    reranker_api_base: str | None = None,
    reranker_api_key: str | None = None,
    reranker_model: str | None = None,
    llm_api_base: str | None = None,
    llm_api_key: str | None = None,
    llm_model: str | None = None,
    write_guard_llm_api_base: str | None = None,
    write_guard_llm_api_key: str | None = None,
    write_guard_llm_model: str | None = None,
    compact_gist_llm_api_base: str | None = None,
    compact_gist_llm_api_key: str | None = None,
    compact_gist_llm_model: str | None = None,
) -> None:
    if database_path:
        data["DATABASE_URL"] = sqlite_url_for_file(Path(database_path).expanduser())
    if sse_url is not None:
        data[_metadata_key("SSE_URL")] = str(sse_url).strip()
    if mcp_api_key is not None and str(mcp_api_key).strip():
        data["MCP_API_KEY"] = str(mcp_api_key).strip()
    if allow_insecure_local is not None:
        data["MCP_API_KEY_ALLOW_INSECURE_LOCAL"] = bool_to_env(allow_insecure_local)
    if backend_api_host is not None and str(backend_api_host).strip():
        data[_metadata_key("BACKEND_API_HOST")] = str(backend_api_host).strip()
    if backend_api_port is not None and str(backend_api_port).strip():
        data[_metadata_key("BACKEND_API_PORT")] = str(
            _normalize_port(backend_api_port, default=BACKEND_API_PORT, label="backend API")
        )
    if dashboard_host is not None and str(dashboard_host).strip():
        data[_metadata_key("DASHBOARD_HOST")] = str(dashboard_host).strip()
    if dashboard_port is not None and str(dashboard_port).strip():
        data[_metadata_key("DASHBOARD_PORT")] = str(
            _normalize_port(dashboard_port, default=DASHBOARD_PORT, label="dashboard")
        )

    if embedding_api_base is not None and str(embedding_api_base).strip():
        normalized = normalize_embedding_api_base(str(embedding_api_base))
        data["RETRIEVAL_EMBEDDING_API_BASE"] = normalized
        data["ROUTER_API_BASE"] = normalized
    if embedding_api_key is not None and str(embedding_api_key).strip():
        data["RETRIEVAL_EMBEDDING_API_KEY"] = str(embedding_api_key).strip()
        data["ROUTER_API_KEY"] = str(embedding_api_key).strip()
    if embedding_model is not None and str(embedding_model).strip():
        value = str(embedding_model).strip()
        data["RETRIEVAL_EMBEDDING_MODEL"] = value
        data["ROUTER_EMBEDDING_MODEL"] = value
    if embedding_dim is not None and str(embedding_dim).strip():
        data["RETRIEVAL_EMBEDDING_DIM"] = str(embedding_dim).strip()

    if reranker_api_base is not None and str(reranker_api_base).strip():
        data["RETRIEVAL_RERANKER_API_BASE"] = normalize_base_url(str(reranker_api_base))
    if reranker_api_key is not None and str(reranker_api_key).strip():
        data["RETRIEVAL_RERANKER_API_KEY"] = str(reranker_api_key).strip()
    if reranker_model is not None and str(reranker_model).strip():
        data["RETRIEVAL_RERANKER_MODEL"] = str(reranker_model).strip()

    if llm_api_base is not None and str(llm_api_base).strip():
        normalized = normalize_chat_api_base(str(llm_api_base))
        data["LLM_API_BASE"] = normalized
        data["OPENAI_BASE_URL"] = normalized
        data["OPENAI_API_BASE"] = normalized
    if llm_api_key is not None and str(llm_api_key).strip():
        value = str(llm_api_key).strip()
        data["LLM_API_KEY"] = value
        data["OPENAI_API_KEY"] = value
    if llm_model is not None and str(llm_model).strip():
        value = str(llm_model).strip()
        data["LLM_MODEL_NAME"] = value
        data["LLM_MODEL"] = value
        data["OPENAI_MODEL"] = value

    resolved_write_guard_api_base = str(write_guard_llm_api_base or llm_api_base or "").strip()
    resolved_write_guard_api_key = str(write_guard_llm_api_key or llm_api_key or "").strip()
    resolved_write_guard_model = str(write_guard_llm_model or llm_model or "").strip()
    if resolved_write_guard_api_base:
        data["WRITE_GUARD_LLM_API_BASE"] = normalize_chat_api_base(resolved_write_guard_api_base)
    if resolved_write_guard_api_key:
        data["WRITE_GUARD_LLM_API_KEY"] = resolved_write_guard_api_key
    if resolved_write_guard_model:
        data["WRITE_GUARD_LLM_MODEL"] = resolved_write_guard_model

    resolved_compact_api_base = str(compact_gist_llm_api_base or llm_api_base or "").strip()
    resolved_compact_api_key = str(compact_gist_llm_api_key or llm_api_key or "").strip()
    resolved_compact_model = str(compact_gist_llm_model or llm_model or "").strip()
    if resolved_compact_api_base:
        data["COMPACT_GIST_LLM_API_BASE"] = normalize_chat_api_base(resolved_compact_api_base)
    if resolved_compact_api_key:
        data["COMPACT_GIST_LLM_API_KEY"] = resolved_compact_api_key
    if resolved_compact_model:
        data["COMPACT_GIST_LLM_MODEL"] = resolved_compact_model

    existing_intent_api_base = str(data.get("INTENT_LLM_API_BASE") or "").strip()
    if is_placeholder_profile_value(existing_intent_api_base):
        existing_intent_api_base = ""
    existing_intent_api_key = str(data.get("INTENT_LLM_API_KEY") or "").strip()
    if is_placeholder_profile_value(existing_intent_api_key):
        existing_intent_api_key = ""
    existing_intent_model = str(data.get("INTENT_LLM_MODEL") or "").strip()
    if is_placeholder_profile_value(existing_intent_model):
        existing_intent_model = ""

    resolved_intent_api_base = first_non_blank(
        existing_intent_api_base,
        str(write_guard_llm_api_base or "").strip(),
        str(llm_api_base or "").strip(),
    )
    resolved_intent_api_key = first_non_blank(
        existing_intent_api_key,
        str(write_guard_llm_api_key or "").strip(),
        str(llm_api_key or "").strip(),
    )
    resolved_intent_model = first_non_blank(
        existing_intent_model,
        str(write_guard_llm_model or "").strip(),
        str(llm_model or "").strip(),
    )
    if resolved_intent_api_base:
        data["INTENT_LLM_API_BASE"] = normalize_chat_api_base(resolved_intent_api_base)
    if resolved_intent_api_key:
        data["INTENT_LLM_API_KEY"] = resolved_intent_api_key
    if resolved_intent_model:
        data["INTENT_LLM_MODEL"] = resolved_intent_model

    current_intent_api_base = str(data.get("INTENT_LLM_API_BASE") or "").strip()
    if is_placeholder_profile_value(current_intent_api_base):
        current_intent_api_base = ""
    current_intent_api_key = str(data.get("INTENT_LLM_API_KEY") or "").strip()
    if is_placeholder_profile_value(current_intent_api_key):
        current_intent_api_key = ""
    current_intent_model = str(data.get("INTENT_LLM_MODEL") or "").strip()
    if is_placeholder_profile_value(current_intent_model):
        current_intent_model = ""

    resolved_intent_api_base = first_non_blank(
        current_intent_api_base,
        str(llm_api_base or "").strip(),
        str(write_guard_llm_api_base or "").strip(),
    )
    resolved_intent_api_key = first_non_blank(
        current_intent_api_key,
        str(llm_api_key or "").strip(),
        str(write_guard_llm_api_key or "").strip(),
    )
    resolved_intent_model = first_non_blank(
        current_intent_model,
        str(llm_model or "").strip(),
        str(write_guard_llm_model or "").strip(),
    )
    if resolved_intent_api_base:
        data["INTENT_LLM_API_BASE"] = normalize_chat_api_base(resolved_intent_api_base)
    if resolved_intent_api_key:
        data["INTENT_LLM_API_KEY"] = resolved_intent_api_key
    if resolved_intent_model:
        data["INTENT_LLM_MODEL"] = resolved_intent_model


def required_profile_fields(data: Mapping[str, str], profile: str) -> list[str]:
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile not in {"c", "d"}:
        return []
    required = [
        "RETRIEVAL_EMBEDDING_API_BASE",
        "RETRIEVAL_EMBEDDING_API_KEY",
        "RETRIEVAL_EMBEDDING_MODEL",
        "RETRIEVAL_RERANKER_API_BASE",
        "RETRIEVAL_RERANKER_API_KEY",
        "RETRIEVAL_RERANKER_MODEL",
    ]
    if normalized_profile == "d":
        required.extend(
            [
                "WRITE_GUARD_LLM_API_BASE",
                "WRITE_GUARD_LLM_API_KEY",
                "WRITE_GUARD_LLM_MODEL",
            ]
        )
    return [key for key in required if is_placeholder_profile_value(data.get(key))]


def profile_required_input_fields(profile: str) -> list[str]:
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile not in {"c", "d"}:
        return []
    required = [
        "RETRIEVAL_EMBEDDING_API_BASE",
        "RETRIEVAL_EMBEDDING_API_KEY",
        "RETRIEVAL_EMBEDDING_MODEL",
        "RETRIEVAL_RERANKER_API_BASE",
        "RETRIEVAL_RERANKER_API_KEY",
        "RETRIEVAL_RERANKER_MODEL",
    ]
    if normalized_profile == "d":
        required.extend(
            [
                "WRITE_GUARD_LLM_API_BASE",
                "WRITE_GUARD_LLM_API_KEY",
                "WRITE_GUARD_LLM_MODEL",
            ]
        )
    return required


def post_json_probe(
    *,
    base_url: str,
    endpoint: str,
    payload: Mapping[str, Any],
    api_key: str | None,
    timeout_seconds: float = 8.0,
) -> tuple[bool, str]:
    url = f"{normalize_base_url(base_url)}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "memory-palace-installer/1.0",
    }
    token = str(api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-API-Key"] = token
    request = Request(
        url,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = int(getattr(response, "status", 200) or 200)
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace").strip()
        if detail:
            detail = detail[:400]
        return False, f"HTTP {exc.code}: {detail or exc.reason or 'request failed'}"
    except (URLError, OSError, TimeoutError) as exc:
        return False, str(exc)
    if status_code < 200 or status_code >= 300:
        return False, f"HTTP {status_code}"
    try:
        json.loads(body)
    except ValueError:
        return False, "response was not valid JSON"
    return True, ""


def post_json_probe_payload(
    *,
    base_url: str,
    endpoint: str,
    payload: Mapping[str, Any],
    api_key: str | None,
    timeout_seconds: float = 8.0,
) -> tuple[bool, str, Any | None]:
    url = f"{normalize_base_url(base_url)}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "memory-palace-installer/1.0",
    }
    token = str(api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-API-Key"] = token
    request = Request(
        url,
        data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = int(getattr(response, "status", 200) or 200)
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace").strip()
        if detail:
            detail = detail[:400]
        return False, f"HTTP {exc.code}: {detail or exc.reason or 'request failed'}", None
    except (URLError, OSError, TimeoutError) as exc:
        return False, str(exc), None
    if status_code < 200 or status_code >= 300:
        return False, f"HTTP {status_code}", None
    try:
        parsed = json.loads(body)
    except ValueError:
        return False, "response was not valid JSON", None
    return True, "", parsed


def extract_embedding_dimension(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            embedding = first.get("embedding")
            if isinstance(embedding, list) and embedding:
                return len(embedding)
        elif isinstance(first, list) and first:
            return len(first)
    embeddings = payload.get("embeddings")
    if isinstance(embeddings, list) and embeddings:
        first = embeddings[0]
        if isinstance(first, list) and first:
            return len(first)
    embedding = payload.get("embedding")
    if isinstance(embedding, list) and embedding:
        return len(embedding)
    return None


def resolve_profile_probe_timeout_seconds(
    env_values: Mapping[str, str],
    *,
    default: float = 8.0,
) -> float:
    timeout_seconds = default
    remote_timeout = str(env_values.get("RETRIEVAL_REMOTE_TIMEOUT_SEC") or "").strip()
    if remote_timeout:
        try:
            parsed_timeout = float(remote_timeout)
        except ValueError:
            parsed_timeout = timeout_seconds
        else:
            if parsed_timeout > 0:
                timeout_seconds = min(parsed_timeout, 120.0)
    timeout_override = str(
        os.getenv("OPENCLAW_MEMORY_PALACE_PROFILE_PROBE_TIMEOUT_SEC") or ""
    ).strip()
    if timeout_override:
        try:
            parsed_timeout = float(timeout_override)
        except ValueError:
            parsed_timeout = timeout_seconds
        else:
            if parsed_timeout > 0:
                timeout_seconds = min(parsed_timeout, 120.0)
    return timeout_seconds


def resolve_profile_probe_retries(default: int = 2) -> int:
    raw_value = str(os.getenv("OPENCLAW_MEMORY_PALACE_PROFILE_PROBE_RETRIES") or "").strip()
    if not raw_value:
        return max(1, default)
    try:
        parsed = int(raw_value)
    except ValueError:
        return max(1, default)
    return max(1, min(parsed, 5))


def run_profile_probe_with_retries(
    *,
    base_url: str,
    endpoint: str,
    payload: Mapping[str, Any],
    api_key: str | None,
    timeout_seconds: float,
    attempts: int,
) -> tuple[bool, str]:
    normalized_attempts = max(1, attempts)
    last_detail = ""
    for attempt in range(1, normalized_attempts + 1):
        ok, detail = post_json_probe(
            base_url=base_url,
            endpoint=endpoint,
            payload=payload,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        if ok:
            return True, detail
        last_detail = detail
        if attempt < normalized_attempts:
            time.sleep(min(0.5 * attempt, 1.5))
    return False, last_detail


def probe_embedding_dimension_with_retries(
    *,
    base_url: str,
    model: str,
    api_key: str | None,
    timeout_seconds: float,
    attempts: int,
    dimensions: int | None = None,
) -> tuple[int | None, str]:
    normalized_attempts = max(1, attempts)
    last_detail = ""
    payload = {
        "model": model,
        "input": "memory palace embedding dimension probe",
    }
    if isinstance(dimensions, int) and dimensions > 0:
        payload["dimensions"] = dimensions
    for attempt in range(1, normalized_attempts + 1):
        ok, detail, parsed = post_json_probe_payload(
            base_url=base_url,
            endpoint="/embeddings",
            payload=payload,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        if ok:
            detected_dim = extract_embedding_dimension(parsed)
            if detected_dim is not None:
                return detected_dim, ""
            last_detail = "embedding probe returned no embedding payload"
        else:
            last_detail = detail
        if attempt < normalized_attempts:
            time.sleep(min(0.5 * attempt, 1.5))
    return None, last_detail


def probe_embedding_dimension_recommendation_with_retries(
    *,
    base_url: str,
    model: str,
    api_key: str | None,
    timeout_seconds: float,
    attempts: int,
) -> tuple[int | None, str]:
    baseline_dim, detail = probe_embedding_dimension_with_retries(
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        attempts=attempts,
    )
    if baseline_dim is None:
        return None, detail
    max_probe = max(baseline_dim, EMBEDDING_DIMENSION_PROBE_MAX)
    lower_bound = baseline_dim
    upper_bound: int | None = None
    last_detail = ""

    candidate = max(baseline_dim + 1, min(baseline_dim * 2, max_probe))
    while candidate <= max_probe:
        detected_dim, candidate_detail = probe_embedding_dimension_with_retries(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            attempts=attempts,
            dimensions=candidate,
        )
        if detected_dim is None or detected_dim < candidate:
            upper_bound = candidate
            last_detail = candidate_detail
            break
        lower_bound = detected_dim
        if lower_bound >= max_probe:
            return lower_bound, "probe_ceiling_reached"
        next_candidate = min(max_probe, max(lower_bound + 1, candidate * 2))
        if next_candidate <= candidate:
            return lower_bound, ""
        candidate = next_candidate

    if upper_bound is None:
        return lower_bound, "probe_ceiling_reached"

    search_low = lower_bound + 1
    search_high = upper_bound - 1
    while search_low <= search_high:
        mid = (search_low + search_high) // 2
        detected_dim, candidate_detail = probe_embedding_dimension_with_retries(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            attempts=attempts,
            dimensions=mid,
        )
        if detected_dim is not None and detected_dim >= mid:
            lower_bound = detected_dim
            search_low = max(detected_dim + 1, mid + 1)
        else:
            search_high = mid - 1
            last_detail = candidate_detail

    return lower_bound, last_detail


def probe_profile_model_connectivity(
    env_values: Mapping[str, str],
    *,
    profile: str,
    timeout_seconds: float = 8.0,
) -> list[dict[str, str]]:
    timeout_seconds = resolve_profile_probe_timeout_seconds(env_values, default=timeout_seconds)
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile not in {"c", "d"}:
        return []
    failures: list[dict[str, str]] = []
    probe_attempts = resolve_profile_probe_retries()

    embedding_base = normalize_embedding_api_base(env_values.get("RETRIEVAL_EMBEDDING_API_BASE"))
    embedding_key = str(env_values.get("RETRIEVAL_EMBEDDING_API_KEY") or "").strip()
    embedding_model = str(env_values.get("RETRIEVAL_EMBEDDING_MODEL") or "").strip()
    ok, detail = run_profile_probe_with_retries(
        base_url=embedding_base,
        endpoint="/embeddings",
        payload={"model": embedding_model, "input": "memory palace connectivity probe"},
        api_key=embedding_key,
        timeout_seconds=timeout_seconds,
        attempts=probe_attempts,
    )
    if not ok:
        failures.append({"component": "embedding", "detail": detail})
    else:
        detected_dim, dim_detail = probe_embedding_dimension_recommendation_with_retries(
            base_url=embedding_base,
            model=embedding_model,
            api_key=embedding_key,
            timeout_seconds=timeout_seconds,
            attempts=probe_attempts,
        )
        if detected_dim is None:
            failures.append({"component": "embedding", "detail": dim_detail})
        elif isinstance(env_values, dict):
            env_values["RETRIEVAL_EMBEDDING_DIM"] = str(detected_dim)

    reranker_base = normalize_reranker_api_base(env_values.get("RETRIEVAL_RERANKER_API_BASE"))
    reranker_key = str(env_values.get("RETRIEVAL_RERANKER_API_KEY") or "").strip()
    reranker_model = str(env_values.get("RETRIEVAL_RERANKER_MODEL") or "").strip()
    ok, detail = run_profile_probe_with_retries(
        base_url=reranker_base,
        endpoint="/rerank",
        payload={
            "model": reranker_model,
            "query": "memory palace connectivity probe",
            "documents": ["probe document"],
        },
        api_key=reranker_key,
        timeout_seconds=timeout_seconds,
        attempts=probe_attempts,
    )
    if not ok:
        failures.append({"component": "reranker", "detail": detail})

    llm_base = normalize_chat_api_base(env_values.get("WRITE_GUARD_LLM_API_BASE"))
    llm_key = str(env_values.get("WRITE_GUARD_LLM_API_KEY") or "").strip()
    llm_model = str(env_values.get("WRITE_GUARD_LLM_MODEL") or "").strip()
    llm_required = normalized_profile == "d"
    llm_explicitly_configured = not any(
        is_placeholder_profile_value(value)
        for value in (llm_base, llm_key, llm_model)
    )
    if llm_required or llm_explicitly_configured:
        ok, detail = run_profile_probe_with_retries(
            base_url=llm_base,
            endpoint="/chat/completions",
            payload={
                "model": llm_model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "Reply with JSON only."},
                    {"role": "user", "content": "Return {\"ok\":true}."},
                ],
            },
            api_key=llm_key,
            timeout_seconds=timeout_seconds,
            attempts=probe_attempts,
        )
        if not ok:
            failures.append({"component": "llm", "detail": detail})

    return failures


_PROFILE_PLACEHOLDER_MARKERS = (
    "replace-with-your-",
    "replace-with-your-key",
    "<your-",
    "127.0.0.1:port",
    "host.docker.internal:port",
    "https://<",
    "http://<",
)


def is_placeholder_profile_value(raw: str | None) -> bool:
    value = str(raw or "").strip()
    if not value:
        return True
    lowered = value.lower()
    return any(marker in lowered for marker in _PROFILE_PLACEHOLDER_MARKERS)


def prompt_for_profile_env_file(*, profile: str, missing_fields: list[str]) -> tuple[dict[str, str] | None, list[str], Path | None]:
    while True:
        raw = input(cli_text("profile_env_prompt_request", profile=profile.upper())).strip()
        if not raw:
            print(cli_text("profile_env_prompt_fallback"))
            return None, [], None
        candidate = Path(raw.strip("\"'")).expanduser()
        if not candidate.is_file():
            print(cli_text("profile_env_prompt_invalid_path", path=str(candidate)))
            continue
        loaded = load_env_file(candidate.resolve())
        if not loaded:
            print(cli_text("profile_env_prompt_empty_file", path=str(candidate.resolve())))
            continue
        resolved = candidate.resolve()
        print(cli_text("profile_env_prompt_imported", path=str(resolved)))
        return loaded, [cli_text("profile_env_prompt_action", path=str(resolved))], resolved


def profile_field_label(field: str) -> str:
    spec = PROFILE_MANUAL_FIELD_SPECS.get(field, {})
    language = cli_language()
    if language == "zh":
        return str(spec.get("label_zh") or field)
    return str(spec.get("label_en") or field)


def profile_field_hint(field: str) -> str:
    spec = PROFILE_MANUAL_FIELD_SPECS.get(field, {})
    language = cli_language()
    if language == "zh":
        return str(spec.get("hint_zh") or field)
    return str(spec.get("hint_en") or field)


def _localized_onboarding_text(zh: str, en: str) -> str:
    return zh if cli_language() == "zh" else en


def _provider_probe_summary_not_required() -> str:
    return _localized_onboarding_text(
        "当前生效档位不依赖外部 provider。",
        "Current effective profile does not require external model providers.",
    )


def _provider_probe_summary_pass() -> str:
    return _localized_onboarding_text(
        "当前档位的高级 provider 检查已通过。",
        "Advanced provider checks passed for the current profile.",
    )


def _provider_probe_summary_incomplete() -> str:
    return _localized_onboarding_text(
        "高级档位依赖的 provider 字段仍未补齐。",
        "Advanced profile provider fields are still incomplete.",
    )


def _provider_probe_summary_not_checked() -> str:
    return _localized_onboarding_text(
        "高级档位的 provider 配置已填写，但尚未记录成功探测结果。",
        "Advanced profile provider settings are configured, but no successful probe is recorded yet.",
    )


def _provider_probe_summary_failures() -> str:
    return _localized_onboarding_text(
        "最近一次高级 provider 探测记录到了失败项。",
        "The last advanced provider probe recorded one or more failures.",
    )


def _provider_probe_summary_fallback(requested_profile: str, effective_profile: str) -> str:
    return _localized_onboarding_text(
        f"请求的 Profile {requested_profile.upper()} 在 provider 检查后回退到了 Profile {effective_profile.upper()}。",
        f"Requested Profile {requested_profile.upper()} fell back to Profile {effective_profile.upper()} after provider checks.",
    )


def _provider_probe_detail_optional() -> str:
    return _localized_onboarding_text(
        "当前生效档位下该 provider 为可选项。",
        "Optional for the current effective profile.",
    )


def _provider_probe_detail_missing(fields: list[str]) -> str:
    rendered = ", ".join(fields)
    return _localized_onboarding_text(
        f"缺失字段: {rendered}",
        f"Missing fields: {rendered}",
    )


def _provider_probe_detail_pass() -> str:
    return _localized_onboarding_text("探测通过。", "Probe passed.")


def _provider_probe_detail_not_checked() -> str:
    return _localized_onboarding_text(
        "配置已填写，但尚未记录成功探测结果。",
        "Configured, but no successful probe is recorded yet.",
    )


def _provider_probe_summary_embedding_dimension(detected_dim: str, recommended_dim: str) -> str:
    return _localized_onboarding_text(
        f"Embedding 探测到的最大维度为 {detected_dim}；建议把 RETRIEVAL_EMBEDDING_DIM 设为 {recommended_dim}。",
        f"Embedding probe detected max dimension {detected_dim}; recommend RETRIEVAL_EMBEDDING_DIM={recommended_dim}.",
    )


def _component_title(component: str) -> str:
    if component == "embedding":
        return _localized_onboarding_text("Embedding", "Embedding")
    if component == "reranker":
        return _localized_onboarding_text("Reranker", "Reranker")
    if component == "llm":
        return _localized_onboarding_text("LLM", "LLM")
    return component


def _component_usage_summary(component: str) -> str:
    if component == "embedding":
        return _localized_onboarding_text(
            "用于语义召回、向量索引和 embedding 维度对齐。",
            "Used for semantic recall, vector indexing, and embedding-dimension alignment.",
        )
    if component == "reranker":
        return _localized_onboarding_text(
            "用于混合检索后的精排，决定高相关结果的排序质量。",
            "Used for post-retrieval reranking and final result ordering quality.",
        )
    if component == "llm":
        return _localized_onboarding_text(
            "用于 write guard、compact gist 等 LLM 辅助链路。",
            "Used for write-guard and compact-gist LLM-assisted flows.",
        )
    return ""


def _component_accepted_forms(component: str) -> list[str]:
    if component == "embedding":
        return [
            _localized_onboarding_text(
                "API Base 可填写 OpenAI-compatible embedding base，例如 `https://host/v1`，也可直接写到 `.../embeddings`。",
                "API base may be an OpenAI-compatible embedding base such as `https://host/v1`, or the explicit `.../embeddings` URL.",
            ),
            _localized_onboarding_text(
                "API Key 直接提供服务端要求的鉴权字符串。",
                "API key should be the exact credential required by the embedding service.",
            ),
            _localized_onboarding_text(
                "Model 必须是该 endpoint 实际接受的模型名。",
                "Model must match the exact model name accepted by the endpoint.",
            ),
        ]
    if component == "reranker":
        return [
            _localized_onboarding_text(
                "API Base 可填写 OpenAI-compatible rerank base，例如 `https://host/v1`，也可直接写到 `.../rerank`。",
                "API base may be an OpenAI-compatible rerank base such as `https://host/v1`, or the explicit `.../rerank` URL.",
            ),
            _localized_onboarding_text(
                "API Key 直接提供 reranker 服务要求的鉴权字符串。",
                "API key should be the exact credential required by the reranker service.",
            ),
            _localized_onboarding_text(
                "Model 必须是 reranker endpoint 实际接受的模型名。",
                "Model must match the exact model name accepted by the reranker endpoint.",
            ),
        ]
    if component == "llm":
        return [
            _localized_onboarding_text(
                "当前项目的 LLM 主路径是 OpenAI-compatible `/chat/completions`。",
                "The primary LLM path in this project is OpenAI-compatible `/chat/completions`.",
            ),
            _localized_onboarding_text(
                "API Base 可以填写 `https://host/v1`、`.../chat/completions`；输入 `.../responses` 会被归一化成 base，但 write-guard / gist 仍按 `/chat/completions` 访问。",
                "API base may be `https://host/v1` or `.../chat/completions`; an input ending in `.../responses` is normalized to a base URL, but write-guard/gist still call `/chat/completions`.",
            ),
            _localized_onboarding_text(
                "如果上游只支持 `/responses` 而不支持 `/chat/completions`，当前 onboarding 不应把它当成 write-guard / gist 的最终 LLM endpoint。",
                "If the upstream only supports `/responses` and not `/chat/completions`, onboarding should not treat it as the final write-guard/gist LLM endpoint.",
            ),
        ]
    return []


def _mask_example_value(value: str | None) -> str | None:
    rendered = str(value or "").strip()
    if not rendered:
        return None
    if rendered.startswith("sk-"):
        return "sk-***"
    if "replace-with-your-key" in rendered:
        return "sk-***"
    return rendered


def _onboarding_command_preview(
    *,
    command: str,
    mode: str,
    profile: str,
    transport: str,
) -> str:
    base = f"{repo_python_command('scripts/openclaw_memory_palace.py')} onboarding --mode {mode} --profile {profile} --transport {transport}"
    if command == "probe":
        return f"{base} --json"
    if command == "apply":
        return f"{base} --apply --json"
    if command == "apply_validate":
        return f"{base} --apply --validate --json"
    return base


def _build_onboarding_provider_sections(
    *,
    profile: str,
    provider_probe: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    normalized_profile = str(profile or "").strip().lower() or "b"
    required_fields = set(profile_required_input_fields(normalized_profile))
    providers = (
        dict(provider_probe.get("providers"))
        if isinstance(provider_probe, Mapping) and isinstance(provider_probe.get("providers"), dict)
        else {}
    )
    sections: list[dict[str, Any]] = []
    for component in ONBOARDING_PROVIDER_COMPONENT_ORDER:
        fields = PROVIDER_PROBE_COMPONENT_FIELDS[component]
        provider_payload = providers.get(component) if isinstance(providers.get(component), dict) else {}
        sections.append(
            {
                "component": component,
                "title": _component_title(component),
                "required": any(field in required_fields for field in fields),
                "usageSummary": _component_usage_summary(component),
                "acceptedForms": _component_accepted_forms(component),
                "envFields": [
                    {
                        "name": field,
                        "label": profile_field_label(field),
                        "hint": profile_field_hint(field),
                        "example": _mask_example_value(PROFILE_MANUAL_FIELD_SPECS.get(field, {}).get("example")),
                        "secret": bool(PROFILE_MANUAL_FIELD_SPECS.get(field, {}).get("secret")),
                    }
                    for field in fields
                ],
                "status": str(provider_payload.get("status") or "unknown"),
                "detail": str(provider_payload.get("detail") or "").strip() or None,
                "baseUrl": str(provider_payload.get("baseUrl") or "").strip() or None,
                "model": str(provider_payload.get("model") or "").strip() or None,
                "missingFields": list(provider_payload.get("missingFields") or []),
                "detectedDim": str(provider_payload.get("detectedDim") or "").strip() or None,
            }
        )
    return sections


def _build_onboarding_questions(missing_fields: list[str]) -> list[dict[str, str]]:
    questions: list[dict[str, str]] = []
    for field in missing_fields:
        component = next(
            (
                key
                for key, values in PROVIDER_PROBE_COMPONENT_FIELDS.items()
                if field in values
            ),
            "provider",
        )
        questions.append(
            {
                "field": field,
                "component": component,
                "label": profile_field_label(field),
                "hint": profile_field_hint(field),
                "prompt": _localized_onboarding_text(
                    f"请提供 `{field}`。{profile_field_hint(field)}",
                    f"Please provide `{field}`. {profile_field_hint(field)}",
                ),
            }
        )
    return questions


def _build_profile_boundary(profile: str) -> dict[str, Any]:
    normalized_profile = str(profile or "").strip().lower() or "b"
    if normalized_profile == "a":
        return {
            "profile": "a",
            "summary": _localized_onboarding_text(
                "Profile A 只保留纯关键词起步路径，不接外部 embedding、reranker、LLM。",
                "Profile A keeps a keyword-only bootstrap path with no external embedding, reranker, or LLM.",
            ),
            "externalLlmKeptDuringSetup": False,
            "writeGuardBehavior": "fallback",
            "compactContextBehavior": "extractive_bullets",
        }
    if normalized_profile == "b":
        return {
            "profile": "b",
            "summary": _localized_onboarding_text(
                "Profile B 是最稳的 bootstrap 基线：本地 hash embedding、无 reranker；如果显式提供可用 LLM，也可以保留 write guard / gist 这类可选 LLM 辅助链路。",
                "Profile B is the safest bootstrap baseline: local hash embeddings, no reranker, and it can still keep optional write-guard / gist LLM assists when valid LLM settings are provided explicitly.",
            ),
            "externalLlmKeptDuringSetup": True,
            "writeGuardBehavior": "llm_or_fallback",
            "compactContextBehavior": "llm_or_extractive_bullets",
        }
    return {
        "profile": normalized_profile,
        "summary": _localized_onboarding_text(
            "Profile C/D 是当前强烈推荐路径：使用真实 embedding、reranker，以及可选 LLM 辅助链路。",
            "Profile C/D is the strongly recommended path: real embedding, reranker, and optional LLM-assisted flows.",
        ),
        "externalLlmKeptDuringSetup": True,
        "writeGuardBehavior": "llm_or_fallback",
        "compactContextBehavior": "llm_or_extractive_bullets",
    }


def onboarding_provider_field_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    for component, fields in PROVIDER_PROBE_COMPONENT_FIELDS.items():
        catalog[component] = {
            "component": component,
            "required": True,
            "acceptedBaseUrlForms": list(ONBOARDING_PROVIDER_BASE_URL_FORMS.get(component, [])),
            "fields": [
                {
                    "envKey": field,
                    "label": profile_field_label(field),
                    "hint": profile_field_hint(field),
                    "example": str(PROFILE_MANUAL_FIELD_SPECS.get(field, {}).get("example") or ""),
                    "secret": bool(PROFILE_MANUAL_FIELD_SPECS.get(field, {}).get("secret")),
                }
                for field in fields
            ],
        }
    return catalog


def onboarding_profile_boundary(profile: str) -> dict[str, Any]:
    normalized = str(profile or "").strip().lower() or "b"
    if normalized == "a":
        return {
            "profile": "a",
            "recommended": False,
            "summary": "Keyword-only validation path.",
            "searchMode": "keyword",
            "embeddingBackend": "none",
            "rerankerEnabled": False,
            "llmAssistsActive": False,
            "llmBoundary": "Advanced provider settings are stripped for Profile A.",
        }
    if normalized == "b":
        return {
            "profile": "b",
            "recommended": False,
            "summary": "Safe bootstrap baseline with local hash embeddings.",
            "searchMode": "hybrid",
            "embeddingBackend": "hash",
            "rerankerEnabled": False,
            "llmAssistsActive": True,
            "llmBoundary": (
                "Profile B still uses local hash embeddings and no reranker, but setup can retain optional "
                "OpenAI-compatible write-guard / gist LLM assists when valid LLM settings are provided."
            ),
        }
    return {
        "profile": normalized,
        "recommended": True,
        "summary": "Strongly recommended runtime path when real providers are ready.",
        "searchMode": "hybrid",
        "embeddingBackend": "api",
        "rerankerEnabled": True,
        "llmAssistsActive": True,
        "llmBoundary": (
            "Profile C/D keeps external provider settings and can enable OpenAI-compatible write-guard / gist assists."
        ),
    }


def onboarding_llm_support_summary() -> dict[str, Any]:
    return {
        "openaiCompatible": True,
        "primaryEndpoint": "/chat/completions",
        "acceptedBaseUrlForms": list(ONBOARDING_PROVIDER_BASE_URL_FORMS["llm"]),
        "responsesInputAliasAccepted": True,
        "responsesRuntimeNotes": (
            "Inputs ending in /responses are accepted as a base-url alias and normalized, "
            "but write-guard / compact-gist LLM calls currently target /chat/completions."
        ),
    }


def prompt_profile_input_method(*, profile: str, missing_fields: list[str]) -> str:
    print(cli_text("profile_env_prompt_intro", profile=profile.upper()))
    print(cli_text("profile_env_prompt_missing", fields=", ".join(missing_fields)))
    while True:
        raw = input(cli_text("profile_prompt_choice", profile=profile.upper())).strip().lower()
        if not raw:
            print(cli_text("profile_env_prompt_fallback"))
            return "fallback"
        if raw in {"1", "env", "file"}:
            return "env"
        if raw in {"2", "manual", "m"}:
            return "manual"
        print(cli_text("profile_prompt_choice_invalid"))


def prompt_for_profile_manual_values(*, profile: str, missing_fields: list[str]) -> tuple[dict[str, str], list[str]]:
    print(cli_text("profile_manual_intro", profile=profile.upper()))
    captured: dict[str, str] = {}
    captured_fields: list[str] = []
    for field in missing_fields:
        spec = PROFILE_MANUAL_FIELD_SPECS.get(field, {})
        example = str(spec.get("example") or "")
        if example:
            print(cli_text("profile_manual_example", example=example))
        print(cli_text("profile_manual_hint", hint=profile_field_hint(field)))
        prompt = cli_text("profile_manual_prompt", label=profile_field_label(field))
        if bool(spec.get("secret")):
            try:
                raw = getpass.getpass(prompt)
            except (EOFError, KeyboardInterrupt):
                raise
            except Exception:
                raw = input(prompt)
        else:
            raw = input(prompt)
        value = str(raw or "").strip()
        if not value:
            continue
        captured[field] = value
        captured_fields.append(field)
    actions = []
    if captured_fields:
        actions.append(cli_text("profile_manual_input_action", fields=", ".join(captured_fields)))
    return captured, actions


def prompt_for_profile_c_optional_llm_choice() -> bool:
    print(cli_text("profile_c_llm_intro"))
    print(f"- {cli_text('profile_c_llm_option_write_guard')}")
    print(f"- {cli_text('profile_c_llm_option_compact_gist')}")
    print(f"- {cli_text('profile_c_llm_option_intent')}")
    while True:
        raw = input(cli_text("profile_c_llm_choice")).strip().lower()
        if not raw or raw in {"n", "no", "0"}:
            print(cli_text("profile_c_llm_skip"))
            return False
        if raw in {"y", "yes", "1"}:
            return True
        print(cli_text("profile_c_llm_choice_invalid"))


def prompt_for_shared_llm_values(*, features: list[str]) -> tuple[dict[str, str], list[str]]:
    feature_text = ", ".join(features)
    print(cli_text("profile_shared_llm_intro", features=feature_text))
    print(cli_text("profile_shared_llm_example", example="https://provider.example/v1"))
    print(cli_text("profile_shared_llm_hint", hint="OpenAI-compatible chat endpoint; /chat/completions suffix is optional."))
    base = input(cli_text("profile_shared_llm_base_prompt")).strip()
    try:
        api_key = getpass.getpass(cli_text("profile_shared_llm_key_prompt"))
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        api_key = input(cli_text("profile_shared_llm_key_prompt"))
    print(cli_text("profile_shared_llm_example", example="gpt-5.4-mini"))
    print(cli_text("profile_shared_llm_hint", hint="Use the exact model name accepted by the chat endpoint."))
    model = input(cli_text("profile_shared_llm_model_prompt")).strip()

    if not base or not str(api_key or "").strip() or not model:
        print(cli_text("profile_shared_llm_cancelled"))
        return {}, []

    captured = {
        "LLM_API_BASE": base,
        "LLM_API_KEY": str(api_key).strip(),
        "LLM_MODEL": model,
        "LLM_MODEL_NAME": model,
    }
    actions = [cli_text("profile_shared_llm_action", features=feature_text)]
    return captured, actions


def prompted_profile_env_path(*, setup_root_path: Path, profile: str) -> Path:
    return setup_root_path / f"profile-{str(profile).strip().lower() or 'b'}.interactive.env"


def persist_prompted_profile_env(
    *,
    prompted_env: Mapping[str, str] | None,
    setup_root_path: Path,
    profile: str,
    dry_run: bool,
) -> Path | None:
    if not prompted_env:
        return None
    target = prompted_profile_env_path(setup_root_path=setup_root_path, profile=profile)
    sanitized = {key: value for key, value in prompted_env.items() if str(value or "").strip()}
    if not sanitized:
        return None
    write_env_file(target, sanitized, dry_run=dry_run)
    return target
