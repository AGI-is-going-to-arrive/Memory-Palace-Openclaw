"""Shared env helpers for benchmark scripts.

These helpers intentionally avoid hardcoded provider addresses or secrets so
benchmark tooling can be committed safely and configured per environment.
"""
from __future__ import annotations

import os


def _first_env(*keys: str) -> str:
    for key in keys:
        value = str(os.environ.get(key, "")).strip()
        if value:
            return value
    return ""


def _normalize_chat_base(api_base: str) -> str:
    normalized = api_base.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def build_real_retrieval_env() -> dict[str, str]:
    """Load the provider env required by real retrieval benchmark runners."""
    required = (
        "RETRIEVAL_EMBEDDING_API_BASE",
        "RETRIEVAL_EMBEDDING_API_KEY",
        "RETRIEVAL_EMBEDDING_MODEL",
        "RETRIEVAL_RERANKER_API_BASE",
        "RETRIEVAL_RERANKER_API_KEY",
        "RETRIEVAL_RERANKER_MODEL",
    )
    missing = [key for key in required if not str(os.environ.get(key, "")).strip()]
    if missing:
        missing_str = ", ".join(missing)
        raise RuntimeError(
            "Real retrieval benchmark env is incomplete. "
            f"Set these variables first: {missing_str}"
        )

    env = {
        "RETRIEVAL_EMBEDDING_API_BASE": str(os.environ["RETRIEVAL_EMBEDDING_API_BASE"]).strip(),
        "RETRIEVAL_EMBEDDING_API_KEY": str(os.environ["RETRIEVAL_EMBEDDING_API_KEY"]).strip(),
        "RETRIEVAL_EMBEDDING_MODEL": str(os.environ["RETRIEVAL_EMBEDDING_MODEL"]).strip(),
        "RETRIEVAL_EMBEDDING_DIM": _first_env("RETRIEVAL_EMBEDDING_DIM") or "1024",
        "RETRIEVAL_REMOTE_TIMEOUT_SEC": _first_env("RETRIEVAL_REMOTE_TIMEOUT_SEC") or "30",
        "RETRIEVAL_RERANKER_API_BASE": str(os.environ["RETRIEVAL_RERANKER_API_BASE"]).strip(),
        "RETRIEVAL_RERANKER_API_KEY": str(os.environ["RETRIEVAL_RERANKER_API_KEY"]).strip(),
        "RETRIEVAL_RERANKER_MODEL": str(os.environ["RETRIEVAL_RERANKER_MODEL"]).strip(),
        "RETRIEVAL_RERANKER_PROVIDER": _first_env("RETRIEVAL_RERANKER_PROVIDER") or "openai_compat",
        "EMBEDDING_PROVIDER_CHAIN_ENABLED": _first_env("EMBEDDING_PROVIDER_CHAIN_ENABLED") or "true",
        "EMBEDDING_PROVIDER_FAIL_OPEN": _first_env("EMBEDDING_PROVIDER_FAIL_OPEN") or "false",
        "EMBEDDING_PROVIDER_FALLBACK": _first_env("EMBEDDING_PROVIDER_FALLBACK") or "hash",
    }
    return env


def describe_real_retrieval_env(env: dict[str, str]) -> str:
    embedding_model = env.get("RETRIEVAL_EMBEDDING_MODEL", "<unset>")
    reranker_model = env.get("RETRIEVAL_RERANKER_MODEL", "<unset>")
    embedding_dim = env.get("RETRIEVAL_EMBEDDING_DIM", "<unset>")
    return (
        f"embedding={embedding_model} dim={embedding_dim}, "
        f"reranker={reranker_model}"
    )


def build_benchmark_llm_tiers() -> list[dict[str, object]]:
    """Resolve benchmark LLM tiers from environment variables."""
    tiers: list[dict[str, object]] = []

    primary_base = _normalize_chat_base(
        _first_env(
            "BENCHMARK_LLM_API_BASE",
            "INTENT_LLM_API_BASE",
            "WRITE_GUARD_LLM_API_BASE",
        )
    )
    primary_key = _first_env(
        "BENCHMARK_LLM_API_KEY",
        "INTENT_LLM_API_KEY",
        "WRITE_GUARD_LLM_API_KEY",
    )
    primary_model = _first_env(
        "BENCHMARK_LLM_MODEL",
        "INTENT_LLM_MODEL",
        "WRITE_GUARD_LLM_MODEL",
    )
    if primary_base and primary_model:
        tiers.append(
            {
                "tier": 1,
                "api_base": primary_base,
                "model": primary_model,
                "api_key": primary_key,
                "degraded": False,
            }
        )

    fallback_base = _normalize_chat_base(_first_env("BENCHMARK_LLM_FALLBACK_API_BASE"))
    fallback_key = _first_env("BENCHMARK_LLM_FALLBACK_API_KEY")
    fallback_model = _first_env("BENCHMARK_LLM_FALLBACK_MODEL")
    if fallback_base and fallback_model:
        tiers.append(
            {
                "tier": 2,
                "api_base": fallback_base,
                "model": fallback_model,
                "api_key": fallback_key,
                "degraded": True,
            }
        )

    return tiers
