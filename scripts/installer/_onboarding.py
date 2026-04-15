#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json

from ._constants import *
from ._utils import *
from ._utils import _metadata_key
from ._provider import *
from ._provider import (
    _localized_onboarding_text,
    _provider_probe_detail_missing,
    _provider_probe_detail_not_checked,
    _provider_probe_detail_optional,
    _provider_probe_detail_pass,
    _provider_probe_summary_embedding_dimension,
    _provider_probe_summary_failures,
    _provider_probe_summary_fallback,
    _provider_probe_summary_incomplete,
    _provider_probe_summary_not_checked,
    _provider_probe_summary_not_required,
    _provider_probe_summary_pass,
)

def finalize_profile_env(
    data: dict[str, str],
    *,
    effective_profile: str,
    requested_profile: str,
    mode: str,
    transport: str,
    setup_root_path: Path,
    allow_generate_mcp_api_key: bool = True,
    explicit_embedding_dim: bool = False,
) -> tuple[dict[str, str], list[str], bool]:
    warnings: list[str] = []
    fallback_applied = requested_profile != effective_profile
    data = dict(data)

    if effective_profile == "a":
        strip_retrieval_provider_runtime_env(data)
        data["SEARCH_DEFAULT_MODE"] = "keyword"
        data["RETRIEVAL_EMBEDDING_BACKEND"] = "none"
        data["RETRIEVAL_RERANKER_ENABLED"] = "false"
    elif effective_profile == "b":
        strip_retrieval_provider_runtime_env(data)
        data["SEARCH_DEFAULT_MODE"] = "hybrid"
        data["RETRIEVAL_EMBEDDING_BACKEND"] = "hash"
        data["RETRIEVAL_EMBEDDING_MODEL"] = "hash-v1"
        data["RETRIEVAL_RERANKER_ENABLED"] = "false"
        # HC-9: When C/D was requested but fell back to B, do NOT persist
        # the hash-embedding dim=64.  The backend defaults to 64 for hash
        # embedding when the key is absent, so omitting it here is safe.
        if fallback_applied and requested_profile in {"c", "d"}:
            data.pop("RETRIEVAL_EMBEDDING_DIM", None)
        else:
            data["RETRIEVAL_EMBEDDING_DIM"] = "64"
    else:
        # HC-9 fix: clear stale dim inherited from a previous A/B config,
        # but ONLY when the caller did NOT explicitly provide a new dim.
        #
        # After build_profile_seed + apply_runtime_field_overrides the data
        # dict may contain dim="64" inherited from an old B runtime.env even
        # though the C/D template already overwrote backend to "api".
        # Checking prev_backend alone is insufficient because the template
        # may have already changed it.  Instead we use a direct signal:
        # explicit_embedding_dim tells us the caller intentionally set a dim.
        prev_dim = str(data.get("RETRIEVAL_EMBEDDING_DIM", "")).strip()

        data["SEARCH_DEFAULT_MODE"] = "hybrid"
        data["RETRIEVAL_EMBEDDING_BACKEND"] = "api"

        if not explicit_embedding_dim and prev_dim in ("64", ""):
            # dim is either the hash default (64) or absent — stale / empty.
            # Clear it so the setdefault below can apply the C/D default.
            data.pop("RETRIEVAL_EMBEDDING_DIM", None)

        data.setdefault("RETRIEVAL_EMBEDDING_DIM", "1024")
        data["RETRIEVAL_RERANKER_ENABLED"] = "true"
        if effective_profile == "c":
            data.setdefault("RETRIEVAL_RERANKER_WEIGHT", "0.30")
        else:
            data.setdefault("RETRIEVAL_RERANKER_WEIGHT", "0.35")

    sync_optional_llm_runtime_flags(data)

    data["RUNTIME_INDEX_WORKER_ENABLED"] = "true"
    data["RUNTIME_INDEX_DEFER_ON_WRITE"] = "true"
    data.setdefault("RUNTIME_AUTO_FLUSH_ENABLED", "true")
    data[_metadata_key("SETUP_ROOT")] = str(setup_root_path)
    data[_metadata_key("MODE")] = mode
    data[_metadata_key("PROFILE_REQUESTED")] = requested_profile
    data[_metadata_key("PROFILE_EFFECTIVE")] = effective_profile
    data[_metadata_key("TRANSPORT")] = transport
    data.setdefault(_metadata_key("BACKEND_API_HOST"), BACKEND_API_HOST)
    data.setdefault(_metadata_key("BACKEND_API_PORT"), str(BACKEND_API_PORT))
    data.setdefault(_metadata_key("DASHBOARD_HOST"), DASHBOARD_HOST)
    data.setdefault(_metadata_key("DASHBOARD_PORT"), str(DASHBOARD_PORT))

    if not str(data.get("DATABASE_URL") or "").strip():
        data["DATABASE_URL"] = sqlite_url_for_file(default_database_file(setup_root_path))

    current_mcp_api_key = str(data.get("MCP_API_KEY") or "").strip()
    if not current_mcp_api_key and allow_generate_mcp_api_key:
        generated = secrets.token_hex(24)
        data["MCP_API_KEY"] = generated
        warnings.append(
            _localized_onboarding_text(
                "MCP_API_KEY 未提供，已为当前本机安装自动生成本地 key。",
                "MCP_API_KEY was not provided, so setup generated a local key for this local installation.",
            )
        )

    if fallback_applied:
        warnings.append(
            _localized_onboarding_text(
                f"Profile {requested_profile.upper()} 所需模型配置不完整，当前已自动回退到 Profile B。",
                f"Profile {requested_profile.upper()} is missing required model settings, so setup automatically fell back to Profile B.",
            )
        )
    return data, warnings, fallback_applied


_OPTIONAL_LLM_ENABLE_KEYS = (
    "WRITE_GUARD_LLM_ENABLED",
    "COMPACT_GIST_LLM_ENABLED",
    "INTENT_LLM_ENABLED",
)


def _llm_suite_explicitly_enabled(env_source: Mapping[str, str] | None) -> bool:
    if not env_source:
        return False
    return any(
        str(env_source.get(key) or "").strip().lower() == "true"
        for key in _OPTIONAL_LLM_ENABLE_KEYS
    )


def _allow_host_llm_seeds(
    *,
    profile: str,
    existing_env: Mapping[str, str] | None,
    prompted_env: Mapping[str, str] | None,
    llm_api_base: str | None = None,
    llm_api_key: str | None = None,
    llm_model: str | None = None,
    write_guard_llm_api_base: str | None = None,
    write_guard_llm_api_key: str | None = None,
    write_guard_llm_model: str | None = None,
    compact_gist_llm_api_base: str | None = None,
    compact_gist_llm_api_key: str | None = None,
    compact_gist_llm_model: str | None = None,
) -> bool:
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile == "d":
        return True
    if normalized_profile != "c":
        return False

    explicit_values = (
        llm_api_base,
        llm_api_key,
        llm_model,
        write_guard_llm_api_base,
        write_guard_llm_api_key,
        write_guard_llm_model,
        compact_gist_llm_api_base,
        compact_gist_llm_api_key,
        compact_gist_llm_model,
    )
    if any(not is_placeholder_profile_value(value) for value in explicit_values):
        return True
    if _llm_suite_explicitly_enabled(prompted_env):
        return True
    if _llm_suite_explicitly_enabled(existing_env):
        return True
    return False


def apply_setup_defaults(
    *,
    profile: str,
    mode: str,
    transport: str,
    setup_root_path: Path,
    existing_env: Mapping[str, str] | None = None,
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
    prompted_env: Mapping[str, str] | None = None,
    strict_profile: bool = False,
    allow_generate_remote_api_key: bool = False,
    host_platform: str | None = None,
) -> tuple[dict[str, str], str, list[str], bool, list[str]]:
    requested_profile = str(profile or "b").strip().lower() or "b"
    if requested_profile not in PROFILE_VALUES:
        raise ValueError(f"Unsupported profile: {profile}")
    persisted_profile = persisted_requested_profile(existing_env)
    prompted_overrides = runtime_overrides_from_env(prompted_env)
    existing_overrides = runtime_overrides_from_env(existing_env)
    host_seed_overrides = host_config_runtime_overrides()
    env_overrides = current_process_runtime_overrides()
    allow_host_llm_seeds = _allow_host_llm_seeds(
        profile=requested_profile,
        existing_env=existing_env,
        prompted_env=prompted_env,
        llm_api_base=llm_api_base,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        write_guard_llm_api_base=write_guard_llm_api_base,
        write_guard_llm_api_key=write_guard_llm_api_key,
        write_guard_llm_model=write_guard_llm_model,
        compact_gist_llm_api_base=compact_gist_llm_api_base,
        compact_gist_llm_api_key=compact_gist_llm_api_key,
        compact_gist_llm_model=compact_gist_llm_model,
    )

    candidate = build_profile_seed(
        profile=requested_profile,
        setup_root_path=setup_root_path,
        existing_env=existing_env,
        host_platform=host_platform,
        preserve_existing=persisted_profile == requested_profile,
    )
    apply_runtime_field_overrides(
        candidate,
        database_path=database_path,
        sse_url=sse_url,
        mcp_api_key=first_non_blank(mcp_api_key, env_value(prompted_env, "MCP_API_KEY")),
        allow_insecure_local=allow_insecure_local,
        backend_api_host=backend_api_host,
        backend_api_port=backend_api_port,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        embedding_api_base=first_non_blank(
            embedding_api_base,
            prompted_overrides["embedding_api_base"],
            existing_overrides["embedding_api_base"],
            host_seed_overrides["embedding_api_base"],
            env_overrides["embedding_api_base"],
        ),
        embedding_api_key=first_non_blank(
            embedding_api_key,
            prompted_overrides["embedding_api_key"],
            existing_overrides["embedding_api_key"],
            host_seed_overrides["embedding_api_key"],
            env_overrides["embedding_api_key"],
        ),
        embedding_model=first_non_blank(
            embedding_model,
            prompted_overrides["embedding_model"],
            existing_overrides["embedding_model"],
            host_seed_overrides["embedding_model"],
            env_overrides["embedding_model"],
        ),
        embedding_dim=first_non_blank(
            embedding_dim,
            prompted_overrides["embedding_dim"],
            existing_overrides["embedding_dim"],
            host_seed_overrides["embedding_dim"],
            env_overrides["embedding_dim"],
        ),
        reranker_api_base=first_non_blank(
            reranker_api_base,
            prompted_overrides["reranker_api_base"],
            existing_overrides["reranker_api_base"],
            host_seed_overrides["reranker_api_base"],
            env_overrides["reranker_api_base"],
        ),
        reranker_api_key=first_non_blank(
            reranker_api_key,
            prompted_overrides["reranker_api_key"],
            existing_overrides["reranker_api_key"],
            host_seed_overrides["reranker_api_key"],
            env_overrides["reranker_api_key"],
        ),
        reranker_model=first_non_blank(
            reranker_model,
            prompted_overrides["reranker_model"],
            existing_overrides["reranker_model"],
            host_seed_overrides["reranker_model"],
            env_overrides["reranker_model"],
        ),
        llm_api_base=first_non_blank(
            llm_api_base,
            prompted_overrides["llm_api_base"],
            existing_overrides["llm_api_base"],
            host_seed_overrides["llm_api_base"] if allow_host_llm_seeds else None,
            env_overrides["llm_api_base"],
        ),
        llm_api_key=first_non_blank(
            llm_api_key,
            prompted_overrides["llm_api_key"],
            existing_overrides["llm_api_key"],
            host_seed_overrides["llm_api_key"] if allow_host_llm_seeds else None,
            env_overrides["llm_api_key"],
        ),
        llm_model=first_non_blank(
            llm_model,
            prompted_overrides["llm_model"],
            existing_overrides["llm_model"],
            host_seed_overrides["llm_model"] if allow_host_llm_seeds else None,
            env_overrides["llm_model"],
        ),
        write_guard_llm_api_base=first_non_blank(
            write_guard_llm_api_base,
            prompted_overrides["write_guard_llm_api_base"],
            existing_overrides["write_guard_llm_api_base"],
            env_overrides["write_guard_llm_api_base"],
        ),
        write_guard_llm_api_key=first_non_blank(
            write_guard_llm_api_key,
            prompted_overrides["write_guard_llm_api_key"],
            existing_overrides["write_guard_llm_api_key"],
            env_overrides["write_guard_llm_api_key"],
        ),
        write_guard_llm_model=first_non_blank(
            write_guard_llm_model,
            prompted_overrides["write_guard_llm_model"],
            existing_overrides["write_guard_llm_model"],
            env_overrides["write_guard_llm_model"],
        ),
        compact_gist_llm_api_base=first_non_blank(
            compact_gist_llm_api_base,
            prompted_overrides["compact_gist_llm_api_base"],
            existing_overrides["compact_gist_llm_api_base"],
            env_overrides["compact_gist_llm_api_base"],
        ),
        compact_gist_llm_api_key=first_non_blank(
            compact_gist_llm_api_key,
            prompted_overrides["compact_gist_llm_api_key"],
            existing_overrides["compact_gist_llm_api_key"],
            env_overrides["compact_gist_llm_api_key"],
        ),
        compact_gist_llm_model=first_non_blank(
            compact_gist_llm_model,
            prompted_overrides["compact_gist_llm_model"],
            existing_overrides["compact_gist_llm_model"],
            env_overrides["compact_gist_llm_model"],
        ),
    )
    missing_fields = required_profile_fields(candidate, requested_profile)
    if missing_fields and strict_profile:
        raise ValueError(
            f"Profile {requested_profile.upper()} requires: {', '.join(missing_fields)}"
        )

    effective_profile = requested_profile if not missing_fields else "b"
    final_data = build_profile_seed(
        profile=effective_profile,
        setup_root_path=setup_root_path,
        existing_env=existing_env,
        host_platform=host_platform,
        preserve_existing=persisted_profile == effective_profile,
    )
    apply_runtime_field_overrides(
        final_data,
        database_path=database_path,
        sse_url=sse_url,
        mcp_api_key=first_non_blank(mcp_api_key, env_value(prompted_env, "MCP_API_KEY")),
        allow_insecure_local=allow_insecure_local,
        backend_api_host=backend_api_host,
        backend_api_port=backend_api_port,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        embedding_api_base=first_non_blank(
            embedding_api_base,
            prompted_overrides["embedding_api_base"],
            existing_overrides["embedding_api_base"],
            host_seed_overrides["embedding_api_base"],
            env_overrides["embedding_api_base"],
        ),
        embedding_api_key=first_non_blank(
            embedding_api_key,
            prompted_overrides["embedding_api_key"],
            existing_overrides["embedding_api_key"],
            host_seed_overrides["embedding_api_key"],
            env_overrides["embedding_api_key"],
        ),
        embedding_model=first_non_blank(
            embedding_model,
            prompted_overrides["embedding_model"],
            existing_overrides["embedding_model"],
            host_seed_overrides["embedding_model"],
            env_overrides["embedding_model"],
        ),
        embedding_dim=first_non_blank(
            embedding_dim,
            prompted_overrides["embedding_dim"],
            existing_overrides["embedding_dim"],
            host_seed_overrides["embedding_dim"],
            env_overrides["embedding_dim"],
        ),
        reranker_api_base=first_non_blank(
            reranker_api_base,
            prompted_overrides["reranker_api_base"],
            existing_overrides["reranker_api_base"],
            host_seed_overrides["reranker_api_base"],
            env_overrides["reranker_api_base"],
        ),
        reranker_api_key=first_non_blank(
            reranker_api_key,
            prompted_overrides["reranker_api_key"],
            existing_overrides["reranker_api_key"],
            host_seed_overrides["reranker_api_key"],
            env_overrides["reranker_api_key"],
        ),
        reranker_model=first_non_blank(
            reranker_model,
            prompted_overrides["reranker_model"],
            existing_overrides["reranker_model"],
            host_seed_overrides["reranker_model"],
            env_overrides["reranker_model"],
        ),
        llm_api_base=first_non_blank(
            llm_api_base,
            prompted_overrides["llm_api_base"],
            existing_overrides["llm_api_base"],
            host_seed_overrides["llm_api_base"] if allow_host_llm_seeds else None,
            env_overrides["llm_api_base"],
        ),
        llm_api_key=first_non_blank(
            llm_api_key,
            prompted_overrides["llm_api_key"],
            existing_overrides["llm_api_key"],
            host_seed_overrides["llm_api_key"] if allow_host_llm_seeds else None,
            env_overrides["llm_api_key"],
        ),
        llm_model=first_non_blank(
            llm_model,
            prompted_overrides["llm_model"],
            existing_overrides["llm_model"],
            host_seed_overrides["llm_model"] if allow_host_llm_seeds else None,
            env_overrides["llm_model"],
        ),
        write_guard_llm_api_base=first_non_blank(
            write_guard_llm_api_base,
            prompted_overrides["write_guard_llm_api_base"],
            existing_overrides["write_guard_llm_api_base"],
            env_overrides["write_guard_llm_api_base"],
        ),
        write_guard_llm_api_key=first_non_blank(
            write_guard_llm_api_key,
            prompted_overrides["write_guard_llm_api_key"],
            existing_overrides["write_guard_llm_api_key"],
            env_overrides["write_guard_llm_api_key"],
        ),
        write_guard_llm_model=first_non_blank(
            write_guard_llm_model,
            prompted_overrides["write_guard_llm_model"],
            existing_overrides["write_guard_llm_model"],
            env_overrides["write_guard_llm_model"],
        ),
        compact_gist_llm_api_base=first_non_blank(
            compact_gist_llm_api_base,
            prompted_overrides["compact_gist_llm_api_base"],
            existing_overrides["compact_gist_llm_api_base"],
            env_overrides["compact_gist_llm_api_base"],
        ),
        compact_gist_llm_api_key=first_non_blank(
            compact_gist_llm_api_key,
            prompted_overrides["compact_gist_llm_api_key"],
            existing_overrides["compact_gist_llm_api_key"],
            env_overrides["compact_gist_llm_api_key"],
        ),
        compact_gist_llm_model=first_non_blank(
            compact_gist_llm_model,
            prompted_overrides["compact_gist_llm_model"],
            existing_overrides["compact_gist_llm_model"],
            env_overrides["compact_gist_llm_model"],
        ),
    )
    resolved_sse_url = (
        str(sse_url).strip()
        if sse_url is not None
        else str(final_data.get(_metadata_key("SSE_URL")) or "").strip()
    )
    allow_generate_mcp_api_key = (
        transport == "stdio"
        or is_loopback_sse_url(resolved_sse_url)
        or allow_generate_remote_api_key
    )
    finalized, warnings, fallback_applied = finalize_profile_env(
        final_data,
        effective_profile=effective_profile,
        requested_profile=requested_profile,
        mode=mode,
        transport=transport,
        setup_root_path=setup_root_path,
        allow_generate_mcp_api_key=allow_generate_mcp_api_key,
        explicit_embedding_dim=bool(
            str(embedding_dim or "").strip()
        ),
    )
    if transport == "sse" and resolved_sse_url and not is_loopback_sse_url(resolved_sse_url):
        if allow_generate_remote_api_key and not str(mcp_api_key or "").strip():
            warnings.append(
                _localized_onboarding_text(
                    "当前 SSE 地址不是本机 loopback；已按显式确认生成远程场景 MCP_API_KEY。",
                    "The SSE URL is not loopback, so setup generated MCP_API_KEY for the remote scenario after explicit confirmation.",
                )
            )
        if not str(finalized.get("MCP_API_KEY") or "").strip():
            raise ValueError(
                "Remote/shared SSE setup requires an explicit MCP_API_KEY. "
                "Pass --mcp-api-key or re-run with --allow-generate-remote-api-key."
            )
    if missing_fields and not strict_profile:
        warnings.append(
            _localized_onboarding_text(
                "缺失的 C/D 字段: " + ", ".join(missing_fields),
                "Missing C/D fields: " + ", ".join(missing_fields),
            )
        )
    return finalized, effective_profile, warnings, fallback_applied, missing_fields


def configured_runtime_state(env_values: Mapping[str, str], *, setup_root_path: Path) -> dict[str, Any]:
    effective_profile = str(env_values.get(_metadata_key("PROFILE_EFFECTIVE")) or "b").strip().lower() or "b"
    requested_profile = str(env_values.get(_metadata_key("PROFILE_REQUESTED")) or effective_profile).strip().lower() or effective_profile
    transport = str(env_values.get(_metadata_key("TRANSPORT")) or "stdio").strip().lower() or "stdio"
    mode = str(env_values.get(_metadata_key("MODE")) or "basic").strip().lower() or "basic"
    embedding_configured = all(
        not is_placeholder_profile_value(env_values.get(key))
        for key in ("RETRIEVAL_EMBEDDING_API_BASE", "RETRIEVAL_EMBEDDING_API_KEY", "RETRIEVAL_EMBEDDING_MODEL")
    )
    reranker_configured = all(
        not is_placeholder_profile_value(env_values.get(key))
        for key in ("RETRIEVAL_RERANKER_API_BASE", "RETRIEVAL_RERANKER_API_KEY", "RETRIEVAL_RERANKER_MODEL")
    )
    llm_configured = all(
        not is_placeholder_profile_value(env_values.get(key))
        for key in ("WRITE_GUARD_LLM_API_BASE", "WRITE_GUARD_LLM_API_KEY", "WRITE_GUARD_LLM_MODEL")
    )
    warnings: list[str] = []
    if requested_profile in {"c", "d"} and effective_profile == "b":
        warnings.append(
            f"上次请求的是 Profile {requested_profile.upper()}，但实际回退到了 Profile B。"
        )
    if transport == "sse" and not str(env_values.get(_metadata_key("SSE_URL")) or "").strip():
        warnings.append("当前 transport 为 SSE，但尚未配置 SSE URL。")
    return {
        "requiresOnboarding": not bool(env_values),
        "restartRequired": False,
        "envFile": str(default_runtime_env_path(setup_root_path)),
        "configPath": "",
        "mode": mode,
        "requestedProfile": requested_profile,
        "effectiveProfile": effective_profile,
        "transport": transport,
        "mcpApiKeyConfigured": bool(str(env_values.get("MCP_API_KEY") or "").strip()),
        "embeddingConfigured": embedding_configured,
        "rerankerConfigured": reranker_configured,
        "llmConfigured": llm_configured,
        "frontendAvailable": frontend_root().is_dir(),
        "dashboard": inspect_dashboard_state(setup_root_path=setup_root_path, env_values=env_values),
        "backendApi": inspect_backend_api_state(setup_root_path=setup_root_path, env_values=env_values),
        "warnings": warnings,
    }


def default_provider_probe_status_path(setup_root_path: Path) -> Path:
    return setup_root_path / "provider-probe-status.json"


def build_provider_probe_signature_hash(
    env_values: Mapping[str, str],
    *,
    requested_profile: str,
    effective_profile: str,
) -> str:
    signature_payload: dict[str, Any] = {
        "requestedProfile": str(requested_profile or "").strip().lower() or "b",
        "effectiveProfile": str(effective_profile or "").strip().lower() or "b",
        "transport": str(env_values.get(_metadata_key("TRANSPORT")) or "").strip(),
        "sseUrl": str(env_values.get(_metadata_key("SSE_URL")) or "").strip(),
        "allowInsecureLocal": str(
            env_values.get("MCP_API_KEY_ALLOW_INSECURE_LOCAL") or ""
        ).strip().lower(),
        "providerFields": {},
    }
    provider_fields = {
        "RETRIEVAL_EMBEDDING_DIM",
        "RETRIEVAL_RERANKER_ENABLED",
        "WRITE_GUARD_LLM_ENABLED",
        "COMPACT_GIST_LLM_ENABLED",
        "INTENT_LLM_ENABLED",
    }
    for fields in PROVIDER_PROBE_COMPONENT_FIELDS.values():
        provider_fields.update(fields)
    for field in sorted(provider_fields):
        value = str(env_values.get(field) or "").strip()
        if not value:
            continue
        if field.endswith("_API_KEY"):
            signature_payload["providerFields"][field] = hashlib.sha256(
                value.encode("utf-8")
            ).hexdigest()
        else:
            signature_payload["providerFields"][field] = value
    encoded = json.dumps(signature_payload, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_provider_probe_status(setup_root_path: Path) -> dict[str, Any] | None:
    path = default_provider_probe_status_path(setup_root_path)
    if not path.is_file():
        return None
    try:
        payload = read_json_file(path)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def persist_provider_probe_status(
    *,
    setup_root_path: Path,
    payload: dict[str, Any] | None,
    dry_run: bool,
) -> None:
    if not payload:
        return
    write_json_file(default_provider_probe_status_path(setup_root_path), payload, dry_run=dry_run)


def _provider_requires_external_models(
    *,
    requested_profile: str,
    effective_profile: str,
) -> bool:
    return str(requested_profile or "").strip().lower() in {"c", "d"} or str(
        effective_profile or ""
    ).strip().lower() in {"c", "d"}


def _provider_missing_fields_by_component(
    env_values: Mapping[str, str],
    *,
    requested_profile: str,
    effective_profile: str,
) -> dict[str, list[str]]:
    if not _provider_requires_external_models(
        requested_profile=requested_profile,
        effective_profile=effective_profile,
    ):
        return {component: [] for component in PROVIDER_PROBE_COMPONENT_FIELDS}
    required_fields = set(profile_required_input_fields(requested_profile))
    missing: dict[str, list[str]] = {}
    for component, fields in PROVIDER_PROBE_COMPONENT_FIELDS.items():
        missing[component] = [
            field
            for field in fields
            if field in required_fields and is_placeholder_profile_value(env_values.get(field))
        ]
    return missing


def build_provider_probe_status(
    *,
    env_values: Mapping[str, str],
    requested_profile: str,
    effective_profile: str,
    fallback_applied: bool,
    profile_probe_failures: list[dict[str, str]] | None,
    missing_profile_fields: list[str] | None,
    probed_profile: str | None = None,
    probe_recorded: bool = True,
) -> dict[str, Any]:
    normalized_requested = str(requested_profile or "").strip().lower() or "b"
    normalized_effective = str(effective_profile or "").strip().lower() or normalized_requested
    normalized_probed = str(probed_profile or normalized_effective).strip().lower() or normalized_effective
    requires_providers = _provider_requires_external_models(
        requested_profile=normalized_requested,
        effective_profile=normalized_effective,
    )
    failure_map = {
        str(item.get("component") or "").strip().lower(): str(item.get("detail") or "").strip()
        for item in (profile_probe_failures or [])
        if str(item.get("component") or "").strip()
    }
    missing_by_component = _provider_missing_fields_by_component(
        env_values,
        requested_profile=normalized_requested,
        effective_profile=normalized_effective,
    )
    required_fields = set(profile_required_input_fields(normalized_requested))
    has_unchecked_required_provider = False
    if requires_providers and not probe_recorded:
        for component, fields in PROVIDER_PROBE_COMPONENT_FIELDS.items():
            if not any(field in required_fields for field in fields):
                continue
            if missing_by_component.get(component):
                continue
            if component in failure_map:
                continue
            configured = all(not is_placeholder_profile_value(env_values.get(field)) for field in fields)
            if configured:
                has_unchecked_required_provider = True
                break
    providers: dict[str, dict[str, Any]] = {}
    summary_status = "pass"
    summary_message = _provider_probe_summary_not_required()
    if requires_providers:
        summary_status = "pass"
        summary_message = _provider_probe_summary_pass()
        if missing_profile_fields:
            summary_status = "warn"
            summary_message = _provider_probe_summary_incomplete()
        elif has_unchecked_required_provider:
            summary_status = "warn"
            summary_message = _provider_probe_summary_not_checked()
        if failure_map:
            summary_status = "warn"
            summary_message = _provider_probe_summary_failures()
        if fallback_applied:
            summary_status = "warn"
            summary_message = _provider_probe_summary_fallback(
                normalized_requested,
                normalized_effective,
            )
    for component, fields in PROVIDER_PROBE_COMPONENT_FIELDS.items():
        configured = all(not is_placeholder_profile_value(env_values.get(field)) for field in fields)
        component_required = any(field in required_fields for field in fields)
        if not requires_providers:
            status = "not_required"
            detail = _provider_probe_detail_optional()
        elif not component_required:
            if component in failure_map:
                status = "fail"
                detail = failure_map[component]
            elif configured and probe_recorded and normalized_probed in {"c", "d"}:
                status = "pass"
                detail = _provider_probe_detail_pass()
            else:
                status = "not_required"
                detail = _provider_probe_detail_optional()
        elif missing_by_component.get(component):
            status = "missing"
            detail = _provider_probe_detail_missing(missing_by_component[component])
        elif component in failure_map:
            status = "fail"
            detail = failure_map[component]
        elif configured and probe_recorded and normalized_probed in {"c", "d"}:
            status = "pass"
            detail = _provider_probe_detail_pass()
        else:
            status = "not_checked"
            detail = _provider_probe_detail_not_checked()
        provider_payload: dict[str, Any] = {
            "configured": configured,
            "status": status,
            "detail": detail,
            "baseUrl": str(env_values.get(fields[0]) or "").strip() or None,
            "model": str(env_values.get(fields[2]) or "").strip() or None,
            "missingFields": missing_by_component.get(component) or [],
        }
        if component == "embedding":
            configured_dim = str(env_values.get("RETRIEVAL_EMBEDDING_DIM") or "").strip() or None
            detected_dim = (
                str(env_values.get("RETRIEVAL_EMBEDDING_DIM") or "").strip() or None
                if status == "pass"
                else None
            )
            provider_payload["configuredDim"] = configured_dim
            provider_payload["detectedDim"] = detected_dim
            provider_payload["detectedMaxDim"] = detected_dim
            provider_payload["recommendedDim"] = detected_dim
        providers[component] = provider_payload
    embedding_payload = providers.get("embedding") if isinstance(providers.get("embedding"), dict) else {}
    recommended_embedding_dim = str(embedding_payload.get("recommendedDim") or "").strip()
    detected_max_embedding_dim = str(embedding_payload.get("detectedMaxDim") or "").strip()
    if (
        requires_providers
        and summary_status == "pass"
        and recommended_embedding_dim
        and detected_max_embedding_dim
    ):
        summary_message = (
            f"{summary_message} "
            f"{_provider_probe_summary_embedding_dimension(detected_max_embedding_dim, recommended_embedding_dim)}"
        )
    return {
        "requestedProfile": normalized_requested,
        "effectiveProfile": normalized_effective,
        "probedProfile": normalized_probed,
        "requiresProviders": requires_providers,
        "fallbackApplied": bool(fallback_applied),
        "summaryStatus": summary_status,
        "summaryMessage": summary_message,
        "missingFields": list(missing_profile_fields or []),
        "checkedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "providerSignatureHash": build_provider_probe_signature_hash(
            env_values,
            requested_profile=normalized_requested,
            effective_profile=normalized_effective,
        ),
        "providers": providers,
    }


def resolve_provider_probe_status(
    *,
    env_values: Mapping[str, str],
    setup_root_path: Path,
    requested_profile: str,
    effective_profile: str,
) -> dict[str, Any]:
    normalized_requested = str(requested_profile or "").strip().lower() or "b"
    normalized_effective = str(effective_profile or "").strip().lower() or normalized_requested
    persisted = load_provider_probe_status(setup_root_path)
    if isinstance(persisted, dict):
        persisted_requested = str(persisted.get("requestedProfile") or "").strip().lower()
        persisted_effective = str(persisted.get("effectiveProfile") or "").strip().lower()
        persisted_signature = str(persisted.get("providerSignatureHash") or "").strip()
        current_signature = build_provider_probe_signature_hash(
            env_values,
            requested_profile=normalized_requested,
            effective_profile=normalized_effective,
        )
        if (
            persisted_requested == normalized_requested
            and persisted_effective == normalized_effective
            and persisted_signature
            and persisted_signature == current_signature
        ):
            return persisted
    missing_fields = profile_required_input_fields(normalized_requested)
    unresolved_missing = [
        field for field in missing_fields if is_placeholder_profile_value(env_values.get(field))
    ]
    return build_provider_probe_status(
        env_values=env_values,
        requested_profile=normalized_requested,
        effective_profile=normalized_effective,
        fallback_applied=normalized_requested in {"c", "d"} and normalized_effective == "b",
        profile_probe_failures=[],
        missing_profile_fields=unresolved_missing,
        probe_recorded=False,
    )


def preview_provider_probe_status(
    *,
    profile: str,
    mode: str = "basic",
    transport: str = "stdio",
    config: str | None = None,
    setup_root_value: str | None = None,
    env_file_value: str | None = None,
    sse_url: str | None = None,
    mcp_api_key: str | None = None,
    allow_insecure_local: bool | None = None,
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
    persist: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized_profile = str(profile or "").strip().lower() or "b"
    if normalized_profile not in PROFILE_VALUES:
        raise ValueError(f"Unsupported profile: {profile}")

    normalized_mode = str(mode or "").strip().lower() or "basic"
    if normalized_mode not in MODE_VALUES:
        raise ValueError(f"Unsupported mode: {mode}")

    normalized_transport = str(transport or "").strip().lower() or "stdio"
    if normalized_transport not in TRANSPORT_VALUES:
        raise ValueError(f"Unsupported transport: {transport}")

    config_path, _ = detect_config_path_with_source(config)
    setup_root_path = (
        Path(setup_root_value).expanduser().resolve()
        if setup_root_value
        else default_setup_root()
    )
    env_file_path = (
        Path(env_file_value).expanduser().resolve()
        if env_file_value
        else default_runtime_env_path(setup_root_path)
    )
    env_values = load_env_file(env_file_path)
    host_seed_overrides = host_config_runtime_overrides(config_path=config_path)
    env_overrides = current_process_runtime_overrides()
    allow_host_llm_seeds = _allow_host_llm_seeds(
        profile=normalized_profile,
        existing_env=env_values,
        prompted_env=None,
        llm_api_base=llm_api_base,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        write_guard_llm_api_base=write_guard_llm_api_base,
        write_guard_llm_api_key=write_guard_llm_api_key,
        write_guard_llm_model=write_guard_llm_model,
        compact_gist_llm_api_base=compact_gist_llm_api_base,
        compact_gist_llm_api_key=compact_gist_llm_api_key,
        compact_gist_llm_model=compact_gist_llm_model,
    )
    apply_runtime_field_overrides(
        env_values,
        sse_url=sse_url,
        mcp_api_key=mcp_api_key,
        allow_insecure_local=allow_insecure_local,
        embedding_api_base=first_non_blank(
            embedding_api_base,
            host_seed_overrides["embedding_api_base"],
            env_overrides["embedding_api_base"],
        ),
        embedding_api_key=first_non_blank(
            embedding_api_key,
            host_seed_overrides["embedding_api_key"],
            env_overrides["embedding_api_key"],
        ),
        embedding_model=first_non_blank(
            embedding_model,
            host_seed_overrides["embedding_model"],
            env_overrides["embedding_model"],
        ),
        embedding_dim=first_non_blank(
            embedding_dim,
            host_seed_overrides["embedding_dim"],
            env_overrides["embedding_dim"],
        ),
        reranker_api_base=first_non_blank(
            reranker_api_base,
            host_seed_overrides["reranker_api_base"],
            env_overrides["reranker_api_base"],
        ),
        reranker_api_key=first_non_blank(
            reranker_api_key,
            host_seed_overrides["reranker_api_key"],
            env_overrides["reranker_api_key"],
        ),
        reranker_model=first_non_blank(
            reranker_model,
            host_seed_overrides["reranker_model"],
            env_overrides["reranker_model"],
        ),
        llm_api_base=first_non_blank(
            llm_api_base,
            host_seed_overrides["llm_api_base"] if allow_host_llm_seeds else None,
            env_overrides["llm_api_base"],
        ),
        llm_api_key=first_non_blank(
            llm_api_key,
            host_seed_overrides["llm_api_key"] if allow_host_llm_seeds else None,
            env_overrides["llm_api_key"],
        ),
        llm_model=first_non_blank(
            llm_model,
            host_seed_overrides["llm_model"] if allow_host_llm_seeds else None,
            env_overrides["llm_model"],
        ),
        write_guard_llm_api_base=first_non_blank(
            write_guard_llm_api_base,
            env_overrides["write_guard_llm_api_base"],
        ),
        write_guard_llm_api_key=first_non_blank(
            write_guard_llm_api_key,
            env_overrides["write_guard_llm_api_key"],
        ),
        write_guard_llm_model=first_non_blank(
            write_guard_llm_model,
            env_overrides["write_guard_llm_model"],
        ),
        compact_gist_llm_api_base=first_non_blank(
            compact_gist_llm_api_base,
            env_overrides["compact_gist_llm_api_base"],
        ),
        compact_gist_llm_api_key=first_non_blank(
            compact_gist_llm_api_key,
            env_overrides["compact_gist_llm_api_key"],
        ),
        compact_gist_llm_model=first_non_blank(
            compact_gist_llm_model,
            env_overrides["compact_gist_llm_model"],
        ),
    )

    env_values, _warnings, _fallback_applied = finalize_profile_env(
        env_values,
        effective_profile=normalized_profile,
        requested_profile=normalized_profile,
        mode=normalized_mode,
        transport=normalized_transport,
        setup_root_path=setup_root_path,
        allow_generate_mcp_api_key=False,
    )

    missing_profile_fields = required_profile_fields(env_values, normalized_profile)
    probe_env_values = dict(env_values)
    profile_probe_failures: list[dict[str, str]] = []
    if normalized_profile in {"c", "d"} and not missing_profile_fields:
        profile_probe_failures = probe_profile_model_connectivity(
            probe_env_values,
            profile=normalized_profile,
        )
        if not profile_probe_failures:
            detected_cap, _detected_detail = probe_embedding_dimension_recommendation_with_retries(
                base_url=normalize_embedding_api_base(
                    str(probe_env_values.get("RETRIEVAL_EMBEDDING_API_BASE") or "")
                ),
                model=str(probe_env_values.get("RETRIEVAL_EMBEDDING_MODEL") or ""),
                api_key=str(probe_env_values.get("RETRIEVAL_EMBEDDING_API_KEY") or ""),
                timeout_seconds=resolve_profile_probe_timeout_seconds(
                    probe_env_values,
                    default=8.0,
                ),
                attempts=resolve_profile_probe_retries(),
            )
            if detected_cap is not None:
                probe_env_values["RETRIEVAL_EMBEDDING_DIM"] = str(detected_cap)

    payload = build_provider_probe_status(
        env_values=probe_env_values,
        requested_profile=normalized_profile,
        effective_profile=normalized_profile,
        fallback_applied=False,
        profile_probe_failures=profile_probe_failures,
        missing_profile_fields=missing_profile_fields,
        probed_profile=normalized_profile if normalized_profile in {"c", "d"} else None,
    )
    payload["configPath"] = str(config_path)
    payload["envFile"] = str(env_file_path)
    if persist:
        persist_provider_probe_status(
            setup_root_path=setup_root_path,
            payload=payload,
            dry_run=dry_run,
        )
    return payload


def _onboarding_field_payload(field: str) -> dict[str, Any]:
    spec = PROFILE_MANUAL_FIELD_SPECS.get(field, {})
    return {
        "envKey": field,
        "label": {
            "en": str(spec.get("label_en") or field),
            "zh": str(spec.get("label_zh") or field),
        },
        "example": str(spec.get("example") or ""),
        "hint": {
            "en": str(spec.get("hint_en") or ""),
            "zh": str(spec.get("hint_zh") or ""),
        },
        "secret": bool(spec.get("secret")),
    }


def _onboarding_component_payload(component: str) -> dict[str, Any]:
    fields = PROVIDER_PROBE_COMPONENT_FIELDS[component]
    if component == "llm":
        required_profiles = ["d"]
    else:
        required_profiles = ["c", "d"]
    if component == "embedding":
        request_path = "/embeddings"
        accepted_forms = [
            "https://provider.example/v1",
            "https://provider.example/v1/embeddings",
        ]
    elif component == "reranker":
        request_path = "/rerank"
        accepted_forms = [
            "https://provider.example/v1",
            "https://provider.example/v1/rerank",
        ]
    else:
        request_path = "/chat/completions"
        accepted_forms = [
            "https://provider.example/v1",
            "https://provider.example/v1/chat/completions",
            "https://provider.example/v1/responses",
        ]
    return {
        "component": component,
        "requiredForProfiles": required_profiles,
        "requestPath": request_path,
        "acceptedBaseUrlForms": accepted_forms,
        "fields": [_onboarding_field_payload(field) for field in fields],
    }


def _onboarding_profile_boundary_payload() -> dict[str, Any]:
    return {
        "b": {
            "profile": "b",
            "safeBaseline": True,
            "recommendedWhen": "Need the fastest safe bootstrap path with zero external model dependencies.",
            "keepsExternalProviderSettings": False,
            "llmSettingsRetainedDuringSetup": True,
            "summary": (
                "Profile B is the safe bootstrap baseline. It uses local hash embedding, "
                "keeps reranker off, and still allows optional write-guard / gist LLM assists when valid "
                "LLM settings are supplied."
            ),
            "fallbackBehavior": {
                "writeGuard": (
                    "Profile B can keep an external write-guard LLM when one is configured; otherwise "
                    "write guard falls back to non-LLM paths when available."
                ),
                "compactContext": (
                    "When no LLM is configured, compact_context falls back to extractive bullets. "
                    "If a valid LLM is configured, gist generation can stay enabled in Profile B."
                ),
            },
        },
        "cd": {
            "profiles": ["c", "d"],
            "stronglyRecommended": True,
            "recommendedWhen": (
                "Providers are ready and you want the strongest retrieval path with embedding + reranker "
                "+ optional LLM-assisted guard/gist behavior."
            ),
            "keepsExternalProviderSettings": True,
            "llmSettingsRetainedDuringSetup": True,
            "summary": (
                "Profiles C/D are the strongly recommended target once provider readiness is green. "
                "They keep external embedding, reranker, and optional LLM settings."
            ),
        },
    }


def _resolve_onboarding_provided_overrides(
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
) -> dict[str, str]:
    data: dict[str, str] = {}
    apply_runtime_field_overrides(
        data,
        database_path=database_path,
        sse_url=sse_url,
        mcp_api_key=mcp_api_key,
        allow_insecure_local=allow_insecure_local,
        backend_api_host=backend_api_host,
        backend_api_port=backend_api_port,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        embedding_api_base=embedding_api_base,
        embedding_api_key=embedding_api_key,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        reranker_api_base=reranker_api_base,
        reranker_api_key=reranker_api_key,
        reranker_model=reranker_model,
        llm_api_base=llm_api_base,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        write_guard_llm_api_base=write_guard_llm_api_base,
        write_guard_llm_api_key=write_guard_llm_api_key,
        write_guard_llm_model=write_guard_llm_model,
        compact_gist_llm_api_base=compact_gist_llm_api_base,
        compact_gist_llm_api_key=compact_gist_llm_api_key,
        compact_gist_llm_model=compact_gist_llm_model,
    )
    return data


def build_onboarding_report(
    *,
    profile: str = "b",
    mode: str = "basic",
    transport: str = "stdio",
    config: str | None = None,
    setup_root_value: str | None = None,
    env_file_value: str | None = None,
    database_path: str | None = None,
    sse_url: str | None = None,
    mcp_api_key: str | None = None,
    allow_insecure_local: bool | None = None,
    dashboard_host: str | None = None,
    dashboard_port: str | int | None = None,
    backend_api_host: str | None = None,
    backend_api_port: str | int | None = None,
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
) -> dict[str, Any]:
    normalized_profile = str(profile or "").strip().lower() or "b"
    if normalized_profile not in PROFILE_VALUES:
        raise ValueError(f"Unsupported profile: {profile}")
    normalized_mode = str(mode or "").strip().lower() or "basic"
    if normalized_mode not in MODE_VALUES:
        raise ValueError(f"Unsupported mode: {mode}")
    normalized_transport = str(transport or "").strip().lower() or "stdio"
    if normalized_transport not in TRANSPORT_VALUES:
        raise ValueError(f"Unsupported transport: {transport}")

    config_path, _config_source = detect_config_path_with_source(config)
    setup_root_path = (
        Path(setup_root_value).expanduser().resolve()
        if setup_root_value
        else default_setup_root()
    )
    env_file_path = (
        Path(env_file_value).expanduser().resolve()
        if env_file_value
        else default_runtime_env_path(setup_root_path)
    )
    current_status = bootstrap_status(
        config=str(config_path),
        setup_root_value=str(setup_root_path),
        env_file_value=str(env_file_path),
    )
    existing_env = load_env_file(env_file_path)
    preview_env, preview_effective, preview_warnings, preview_fallback, missing_fields = apply_setup_defaults(
        profile=normalized_profile,
        mode=normalized_mode,
        transport=normalized_transport,
        setup_root_path=setup_root_path,
        existing_env=existing_env,
        database_path=database_path,
        sse_url=sse_url,
        mcp_api_key=mcp_api_key,
        allow_insecure_local=allow_insecure_local,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        backend_api_host=backend_api_host,
        backend_api_port=backend_api_port,
        embedding_api_base=embedding_api_base,
        embedding_api_key=embedding_api_key,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        reranker_api_base=reranker_api_base,
        reranker_api_key=reranker_api_key,
        reranker_model=reranker_model,
        llm_api_base=llm_api_base,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        write_guard_llm_api_base=write_guard_llm_api_base,
        write_guard_llm_api_key=write_guard_llm_api_key,
        write_guard_llm_model=write_guard_llm_model,
        compact_gist_llm_api_base=compact_gist_llm_api_base,
        compact_gist_llm_api_key=compact_gist_llm_api_key,
        compact_gist_llm_model=compact_gist_llm_model,
    )
    provider_probe = (
        preview_provider_probe_status(
            profile=normalized_profile,
            mode=normalized_mode,
            transport=normalized_transport,
            config=str(config_path),
            setup_root_value=str(setup_root_path),
            env_file_value=str(env_file_path),
            embedding_api_base=embedding_api_base,
            embedding_api_key=embedding_api_key,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            reranker_api_base=reranker_api_base,
            reranker_api_key=reranker_api_key,
            reranker_model=reranker_model,
            llm_api_base=llm_api_base,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            write_guard_llm_api_base=write_guard_llm_api_base,
            write_guard_llm_api_key=write_guard_llm_api_key,
            write_guard_llm_model=write_guard_llm_model,
            compact_gist_llm_api_base=compact_gist_llm_api_base,
            compact_gist_llm_api_key=compact_gist_llm_api_key,
            compact_gist_llm_model=compact_gist_llm_model,
            persist=False,
        )
        if normalized_profile in {"c", "d"}
        else build_provider_probe_status(
            env_values=preview_env,
            requested_profile=normalized_profile,
            effective_profile=preview_effective,
            fallback_applied=preview_fallback,
            profile_probe_failures=[],
            missing_profile_fields=missing_fields,
        )
    )
    provider_entries = (
        provider_probe.get("providers")
        if isinstance(provider_probe.get("providers"), Mapping)
        else {}
    )
    detected_embedding_dim = (
        str(
            (
                provider_entries.get("embedding")
                if isinstance(provider_entries.get("embedding"), Mapping)
                else {}
            ).get("detectedDim")
            or ""
        ).strip()
        or None
    )
    if (
        normalized_profile in {"c", "d"}
        and not missing_fields
        and str(embedding_api_base or "").strip()
        and str(embedding_api_key or "").strip()
        and str(embedding_model or "").strip()
    ):
        probe_timeout = resolve_profile_probe_timeout_seconds(preview_env, default=8.0)
        probe_attempts = resolve_profile_probe_retries()
        detected_cap, _detected_detail = probe_embedding_dimension_recommendation_with_retries(
            base_url=normalize_embedding_api_base(str(embedding_api_base)),
            model=str(embedding_model),
            api_key=str(embedding_api_key),
            timeout_seconds=probe_timeout,
            attempts=probe_attempts,
        )
        if detected_cap is not None:
            detected_embedding_dim = str(detected_cap)
            embedding_entry = (
                provider_entries.get("embedding")
                if isinstance(provider_entries.get("embedding"), dict)
                else None
            )
            if embedding_entry is not None:
                embedding_entry["detectedDim"] = detected_embedding_dim
                embedding_entry["detectedMaxDim"] = detected_embedding_dim
                embedding_entry["recommendedDim"] = detected_embedding_dim
    provider_failures = [
        component
        for component, payload in provider_entries.items()
        if isinstance(payload, Mapping) and str(payload.get("status") or "").strip().lower() == "fail"
    ]
    effective_if_apply = preview_effective
    if normalized_profile in {"c", "d"} and (missing_fields or provider_failures):
        effective_if_apply = "b"

    provided_overrides = _resolve_onboarding_provided_overrides(
        database_path=database_path,
        sse_url=sse_url,
        mcp_api_key=mcp_api_key,
        allow_insecure_local=allow_insecure_local,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        backend_api_host=backend_api_host,
        backend_api_port=backend_api_port,
        embedding_api_base=embedding_api_base,
        embedding_api_key=embedding_api_key,
        embedding_model=embedding_model,
        embedding_dim=embedding_dim,
        reranker_api_base=reranker_api_base,
        reranker_api_key=reranker_api_key,
        reranker_model=reranker_model,
        llm_api_base=llm_api_base,
        llm_api_key=llm_api_key,
        llm_model=llm_model,
        write_guard_llm_api_base=write_guard_llm_api_base,
        write_guard_llm_api_key=write_guard_llm_api_key,
        write_guard_llm_model=write_guard_llm_model,
        compact_gist_llm_api_base=compact_gist_llm_api_base,
        compact_gist_llm_api_key=compact_gist_llm_api_key,
        compact_gist_llm_model=compact_gist_llm_model,
    )
    unused_advanced_fields = []
    if normalized_profile in {"a", "b"}:
        unused_advanced_fields = [
            key
            for key in RETRIEVAL_PROVIDER_RUNTIME_ENV_KEYS
            if str(provided_overrides.get(key) or "").strip()
        ]

    next_actions: list[str] = []
    if normalized_profile in {"c", "d"} and missing_fields:
        missing_sections = []
        for component, fields in PROVIDER_PROBE_COMPONENT_FIELDS.items():
            if any(field in missing_fields for field in fields):
                missing_sections.append(component)
        for component in missing_sections:
            next_actions.append(f"Collect the {component} provider fields before applying Profile {normalized_profile.upper()}.")
    if provider_failures:
        for component in provider_failures:
            next_actions.append(
                f"Fix the {component} provider credentials or connectivity, then probe again before apply."
            )
    if effective_if_apply == "b" and normalized_profile in {"c", "d"}:
        next_actions.append(
            f"If you apply now, setup will stay usable by falling back to Profile B until Profile {normalized_profile.upper()} is ready."
        )
    if normalized_profile == "b":
        next_actions.append("Profile B is the safe bootstrap baseline with zero external provider requirements.")
        next_actions.append(
            "Profile B setup strips external embedding and reranker settings, but it can still keep optional LLM assists when valid write-guard / gist settings are provided."
        )
    if detected_embedding_dim:
        next_actions.append(
            f"Detected embedding dimension {detected_embedding_dim}; use this as RETRIEVAL_EMBEDDING_DIM when applying Profile {normalized_profile.upper()}."
        )
    if not next_actions:
        next_actions.append("Provider readiness looks usable. You can apply the requested profile now.")

    command_parts = [
        repo_python_command("scripts/openclaw_memory_palace.py onboarding", host_platform=host_platform_name()),
        f"--mode {normalized_mode}",
        f"--profile {normalized_profile}",
        f"--transport {normalized_transport}",
        "--json",
    ]
    if str(config_path):
        command_parts.extend(["--config", shlex.quote(str(config_path))])
    if str(setup_root_path):
        command_parts.extend(["--setup-root", shlex.quote(str(setup_root_path))])
    if detected_embedding_dim and normalized_profile in {"c", "d"}:
        command_parts.extend(["--embedding-dim", detected_embedding_dim])
    apply_command_preview = " ".join([*command_parts, "--apply"])

    # --- IMP-2: Install guidance (HC-6: no repo-URL direct install) ----------
    source_checkout = (
        f"{repo_python_command('scripts/openclaw_memory_palace.py setup', host_platform=host_platform_name())} "
        "--mode basic --profile b --transport stdio --json"
    )
    local_tgz_parts = ["openclaw", "plugins", "install"]
    if supports_dangerously_force_unsafe_install("openclaw"):
        local_tgz_parts.append("--dangerously-force-unsafe-install")
    local_tgz_parts.append("./<generated-tgz>")
    local_tgz = " ".join(local_tgz_parts)
    install_guidance = {
        "supportedInstallMethods": ["source-checkout", "local-tgz"],
        "repoUrlDirectInstallSupported": False,
        "recommendedMethod": "source-checkout",
        "installCommands": {
            "source-checkout": source_checkout,
            "local-tgz": local_tgz,
        },
        "installSteps": {
            "source-checkout": [
                source_checkout,
                "openclaw memory-palace verify --json",
                "openclaw memory-palace doctor --json",
                "openclaw memory-palace smoke --json",
            ],
            "local-tgz": [
                local_tgz,
                "npm exec --yes --package ./<generated-tgz> memory-palace-openclaw -- setup --mode basic --profile b --transport stdio --json",
                "openclaw memory-palace verify --json",
                "openclaw memory-palace doctor --json",
                "openclaw memory-palace smoke --json",
            ],
        },
        "recommendedMethodNote": (
            "Run the source-checkout command from the repository root on the same machine as OpenClaw. "
            "In the current rerun, the public npm spec `@openclaw/memory-palace` returned `Package not found on npm`, "
            "and `openclaw plugins install memory-palace` resolved to a skill rather than a plugin."
        ),
    }

    current_setup = current_status.get("setup") if isinstance(current_status.get("setup"), Mapping) else {}
    return {
        "ok": True,
        "summary": (
            f"Onboarding guidance prepared for Profile {normalized_profile.upper()} "
            f"(effective-if-applied: Profile {effective_if_apply.upper()})."
        ),
        "requestedProfile": normalized_profile,
        "effectiveProfileIfApplied": effective_if_apply,
        "safeBaselineProfile": "b",
        "recommendedTargetProfiles": ["c", "d"],
        "recommendedDefaultTargetProfile": "c",
        "mode": normalized_mode,
        "transport": normalized_transport,
        "requiresProviders": normalized_profile in {"c", "d"},
        "currentSetup": {
            "requiresOnboarding": bool(current_setup.get("requiresOnboarding", True)),
            "requestedProfile": str(current_setup.get("requestedProfile") or "b"),
            "effectiveProfile": str(current_setup.get("effectiveProfile") or "b"),
            "transport": str(current_setup.get("transport") or "stdio"),
            "configPath": str(current_setup.get("configPath") or config_path),
            "envFile": str(current_setup.get("envFile") or env_file_path),
            "restartRequired": bool(current_setup.get("restartRequired")),
        },
        "profileBoundaries": _onboarding_profile_boundary_payload(),
        "providerRequirements": {
            component: _onboarding_component_payload(component)
            for component in ("embedding", "reranker", "llm")
        },
        "llmSupport": {
            "openaiCompatible": True,
            "requestPath": "/chat/completions",
            "acceptedBaseUrlForms": [
                "https://provider.example/v1",
                "https://provider.example/v1/chat/completions",
                "https://provider.example/v1/responses",
            ],
            "responsesAliasInputSupported": True,
            "directResponsesRequestUsed": False,
            "optionalProfileCSuite": {
                "promptedDuringInteractiveSetup": True,
                "features": ["write_guard", "compact_gist", "intent_llm"],
            },
            "profileDDefaultSuite": {
                "features": ["write_guard", "compact_gist", "intent_llm"],
                "llmRequired": True,
            },
        },
        "reindexGate": detect_reindex_required(existing_env, preview_env),
        "installGuidance": install_guidance,
        "missingFields": missing_fields,
        "unusedAdvancedFieldsForProfile": unused_advanced_fields,
        "providerProbe": provider_probe,
        "detectedMaxEmbeddingDimension": detected_embedding_dim,
        "recommendedEmbeddingDimension": detected_embedding_dim,
        "warnings": dedupe_keep_order(preview_warnings),
        "nextActions": dedupe_keep_order(next_actions),
        "commandPreview": {
            "inspect": " ".join(command_parts),
            "apply": apply_command_preview,
            "validate": [
                "openclaw memory-palace verify --json",
                "openclaw memory-palace doctor --json",
                "openclaw memory-palace smoke --json",
            ],
        },
    }
