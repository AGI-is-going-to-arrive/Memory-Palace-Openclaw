"""Health checks for external services used by real ablation benchmarks.

Each check returns a dict with:
  - status: "ok" | "skip"
  - provider_type: str (e.g. "api", "ollama_fallback", "hash")
  - dim: int (embedding only)
  - reason: str (when status == "skip")

These checks are used by test_quality_ablation_real.py to decide which
matrix cells can run.  No addresses, keys, or hosts appear in the return
values — only normalized provider_type strings.

Uses synchronous urllib to avoid httpx/aiohttp event-loop conflicts
inside pytest-asyncio strict mode.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any, Dict


def _sync_post_json(
    url: str,
    payload: dict,
    api_key: str = "",
    timeout_sec: float = 10.0,
) -> Dict[str, Any]:
    """Synchronous JSON POST using stdlib urllib (no event-loop conflicts)."""
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Embedding health check
# ---------------------------------------------------------------------------

async def check_embedding_health(*, timeout_sec: float = 10.0) -> Dict[str, Any]:
    """Probe the embedding service. Falls back to localhost Ollama."""
    api_base = os.environ.get("RETRIEVAL_EMBEDDING_API_BASE", "").rstrip("/")
    api_key = os.environ.get("RETRIEVAL_EMBEDDING_API_KEY", "")
    model = os.environ.get("RETRIEVAL_EMBEDDING_MODEL", "")

    if api_base and model:
        result = _probe_embedding(api_base, api_key, model, timeout_sec)
        if result["status"] == "ok":
            result["provider_type"] = "api"
            return result

    # Ollama fallback
    ollama_base = "http://localhost:11434/v1"
    ollama_model = os.environ.get(
        "EMBEDDING_FALLBACK_OLLAMA_MODEL",
        "qwen3-embedding:8b-q8_0-ctx8192",
    )
    ollama_result = _probe_embedding(ollama_base, "", ollama_model, timeout_sec)
    if ollama_result["status"] == "ok":
        ollama_result["provider_type"] = "ollama_fallback"
        return ollama_result

    return {
        "status": "skip",
        "provider_type": "unavailable",
        "dim": 0,
        "reason": "Neither primary embedding API nor Ollama fallback is reachable",
    }


def _probe_embedding(
    base: str, key: str, model: str, timeout_sec: float,
) -> Dict[str, Any]:
    url = f"{base}/embeddings"
    try:
        data = _sync_post_json(
            url, {"model": model, "input": "health check probe"}, key, timeout_sec,
        )
        embedding = data.get("data", [{}])[0].get("embedding", [])
        dim = len(embedding)
        if dim <= 0:
            return {"status": "skip", "dim": 0, "reason": "empty embedding vector"}
        return {"status": "ok", "dim": dim}
    except Exception as exc:
        return {"status": "skip", "dim": 0, "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Reranker health check
# ---------------------------------------------------------------------------

async def check_reranker_health(*, timeout_sec: float = 10.0) -> Dict[str, Any]:
    api_base = os.environ.get("RETRIEVAL_RERANKER_API_BASE", "").rstrip("/")
    api_key = os.environ.get("RETRIEVAL_RERANKER_API_KEY", "")
    model = os.environ.get("RETRIEVAL_RERANKER_MODEL", "")

    if not api_base or not model:
        return {"status": "skip", "reason": "RETRIEVAL_RERANKER_API_BASE or MODEL not set"}

    url = f"{api_base}/rerank"
    try:
        data = _sync_post_json(
            url,
            {"model": model, "query": "health check", "documents": ["probe document"]},
            api_key, timeout_sec,
        )
        results = data.get("results", data.get("data", []))
        if not results:
            return {"status": "skip", "reason": "reranker returned empty results"}
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "skip", "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# LLM health check
# ---------------------------------------------------------------------------

async def check_llm_health(*, timeout_sec: float = 40.0) -> Dict[str, Any]:
    api_base = os.environ.get("INTENT_LLM_API_BASE", "").rstrip("/")
    api_key = os.environ.get("INTENT_LLM_API_KEY", "")
    model = os.environ.get("INTENT_LLM_MODEL", "")

    if not api_base:
        api_base = os.environ.get("WRITE_GUARD_LLM_API_BASE", "").rstrip("/")
        api_key = os.environ.get("WRITE_GUARD_LLM_API_KEY", api_key)
        model = os.environ.get("WRITE_GUARD_LLM_MODEL", model)

    if not api_base or not model:
        return {"status": "skip", "reason": "No LLM API base configured"}

    if not api_base.startswith("http"):
        api_base = f"http://{api_base}"

    url = f"{api_base}/chat/completions"
    try:
        data = _sync_post_json(
            url,
            {"model": model, "messages": [{"role": "user", "content": "Reply with exactly: ok"}], "max_tokens": 10},
            api_key, timeout_sec,
        )
        choices = data.get("choices", [])
        if not choices:
            return {"status": "skip", "reason": "LLM returned no choices"}
        return {"status": "ok"}
    except Exception as exc:
        return {"status": "skip", "reason": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

async def run_all_health_checks() -> Dict[str, Dict[str, Any]]:
    return {
        "embedding": await check_embedding_health(),
        "reranker": await check_reranker_health(),
        "llm": await check_llm_health(),
    }
