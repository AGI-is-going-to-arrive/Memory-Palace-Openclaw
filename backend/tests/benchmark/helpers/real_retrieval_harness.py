"""Real retrieval harness for ablation testing.

Provides utilities to:
  1. Create a temporary SQLite DB with real schema
  2. Seed it with existing memories from gold set data
  3. Build FTS + optional vector indices
  4. Run write_guard / search_advanced through the real retrieval path

This harness does NOT inject fake scores or mock search_advanced.
Profile configuration is done via monkeypatch env vars before client creation.

NOTE: This is a test harness choice for local validation.  Profile env
overrides here reflect docs/DEPLOYMENT_PROFILES.md product semantics but
are applied in an isolated test DB — they do not redefine product profiles.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pytest
from helpers.benchmark_env import build_benchmark_llm_tiers


BACKEND_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ProfileEnv:
    """Normalized env overrides for a product profile.

    These mirror docs/DEPLOYMENT_PROFILES.md product semantics.
    """
    key: str
    label: str
    env: Mapping[str, str]


# Product profile definitions — per docs/DEPLOYMENT_PROFILES.md
PRODUCT_PROFILES: Sequence[ProfileEnv] = (
    ProfileEnv(
        key="B",
        label="Profile B (hash, no reranker)",
        env={
            "RETRIEVAL_EMBEDDING_BACKEND": "hash",
            "RETRIEVAL_EMBEDDING_DIM": "64",
            "RETRIEVAL_RERANKER_ENABLED": "false",
            "RETRIEVAL_RERANKER_WEIGHT": "0",
        },
    ),
    ProfileEnv(
        key="C",
        label="Profile C (api embed + reranker, weight=0.30)",
        env={
            "RETRIEVAL_EMBEDDING_BACKEND": "api",
            # dim from provider probe — not set here, let client detect
            "RETRIEVAL_RERANKER_ENABLED": "true",
            "RETRIEVAL_RERANKER_WEIGHT": "0.30",
        },
    ),
    ProfileEnv(
        key="D",
        label="Profile D (api embed + reranker, weight=0.35)",
        env={
            "RETRIEVAL_EMBEDDING_BACKEND": "api",
            "RETRIEVAL_RERANKER_ENABLED": "true",
            "RETRIEVAL_RERANKER_WEIGHT": "0.35",
        },
    ),
)

PROFILE_BY_KEY: Dict[str, ProfileEnv] = {p.key: p for p in PRODUCT_PROFILES}


@dataclass(frozen=True)
class CellConfig:
    """A single cell in the Profile × LLM test matrix."""
    cell_id: str
    profile: ProfileEnv
    llm_enabled: bool

    @property
    def config_summary(self) -> Dict[str, Any]:
        """Normalized config for artifact output (no raw URLs/keys)."""
        return {
            "embedding_backend": self.profile.env.get("RETRIEVAL_EMBEDDING_BACKEND", "hash"),
            "embedding_dim": int(self.profile.env.get("RETRIEVAL_EMBEDDING_DIM", "0")) or "provider_probe",
            "reranker_enabled": self.profile.env.get("RETRIEVAL_RERANKER_ENABLED", "false") == "true",
            "reranker_weight": float(self.profile.env.get("RETRIEVAL_RERANKER_WEIGHT", "0")),
            "llm_enabled": self.llm_enabled,
        }


# 6-cell matrix
MATRIX_CELLS: Sequence[CellConfig] = (
    CellConfig("B-off", PROFILE_BY_KEY["B"], llm_enabled=False),
    CellConfig("B-on",  PROFILE_BY_KEY["B"], llm_enabled=True),
    CellConfig("C-off", PROFILE_BY_KEY["C"], llm_enabled=False),
    CellConfig("C-on",  PROFILE_BY_KEY["C"], llm_enabled=True),
    CellConfig("D-off", PROFILE_BY_KEY["D"], llm_enabled=False),
    CellConfig("D-on",  PROFILE_BY_KEY["D"], llm_enabled=True),
)


def cell_requires_embedding(cell: CellConfig) -> bool:
    return cell.profile.env.get("RETRIEVAL_EMBEDDING_BACKEND") != "hash"


def cell_requires_reranker(cell: CellConfig) -> bool:
    return cell.profile.env.get("RETRIEVAL_RERANKER_ENABLED") == "true"


def cell_requires_llm(cell: CellConfig) -> bool:
    return cell.llm_enabled


def check_cell_runnable(
    cell: CellConfig,
    health: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    """Return skip reason if cell cannot run, else None."""
    if cell_requires_embedding(cell) and health["embedding"]["status"] != "ok":
        return f"embedding service unavailable: {health['embedding'].get('reason', 'unknown')}"
    if cell_requires_reranker(cell) and health["reranker"]["status"] != "ok":
        return f"reranker service unavailable: {health['reranker'].get('reason', 'unknown')}"
    if cell_requires_llm(cell) and health["llm"]["status"] != "ok":
        return f"LLM service unavailable: {health['llm'].get('reason', 'unknown')}"
    return None


def apply_cell_env(
    monkeypatch: pytest.MonkeyPatch,
    cell: CellConfig,
    health: Dict[str, Dict[str, Any]],
) -> None:
    """Apply env vars for a matrix cell.  Must be called BEFORE SQLiteClient init."""
    # Profile env
    for k, v in cell.profile.env.items():
        monkeypatch.setenv(k, v)

    # If embedding is api and health check resolved a specific dim, apply it
    if cell.profile.env.get("RETRIEVAL_EMBEDDING_BACKEND") == "api":
        dim = health.get("embedding", {}).get("dim")
        if dim and dim > 0:
            monkeypatch.setenv("RETRIEVAL_EMBEDDING_DIM", str(dim))

    # LLM switches
    llm_val = "true" if cell.llm_enabled else "false"
    monkeypatch.setenv("INTENT_LLM_ENABLED", llm_val)
    monkeypatch.setenv("WRITE_GUARD_LLM_ENABLED", llm_val)
    monkeypatch.setenv("COMPACT_GIST_LLM_ENABLED", llm_val)

    # For LLM-on cells, ensure the LLM env vars are propagated from
    # whatever the user set.  We don't hardcode addresses — just ensure
    # the INTENT_LLM_* vars mirror WRITE_GUARD_LLM_* if not independently set.
    if cell.llm_enabled:
        for prefix in ("INTENT_LLM", "COMPACT_GIST_LLM"):
            for suffix in ("API_BASE", "API_KEY", "MODEL"):
                var = f"{prefix}_{suffix}"
                if not os.environ.get(var):
                    fallback = os.environ.get(f"WRITE_GUARD_LLM_{suffix}", "")
                    if fallback:
                        monkeypatch.setenv(var, fallback)


async def _ensure_parent_chain(client: Any, domain: str, full_path: str) -> None:
    """Create ancestor path nodes so that create_memory's parent validation passes.

    For path "test/project/81", ensures "test" and "test/project" exist as
    placeholder memories (content="(ancestor placeholder)").
    """
    segments = full_path.split("/")
    # Build ancestors: for ["test","project","81"] → ["test", "test/project"]
    for depth in range(1, len(segments)):
        ancestor_parent = "/".join(segments[:depth - 1])  # "" for root
        ancestor_title = segments[depth - 1]
        try:
            await client.create_memory(
                parent_path=ancestor_parent,
                content="(ancestor placeholder)",
                priority=100,
                title=ancestor_title,
                domain=domain,
                index_now=False,
            )
        except (ValueError, Exception):
            # Already exists or other constraint — safe to skip
            pass


async def seed_memories(
    client: Any,
    memories: List[Dict[str, Any]],
) -> None:
    """Insert existing memories into the test DB via the client's create path.

    Each memory dict should have: uri, content, domain.
    Properly splits URIs into parent_path + title and ensures the ancestor
    chain exists before creating the leaf memory.
    """
    for mem in memories:
        uri = mem.get("uri", "core://test/default")
        content = mem.get("content", "")
        domain = mem.get("domain", "core")

        # Parse URI → domain + full path
        parts = uri.split("://", 1)
        if len(parts) == 2:
            domain = parts[0]
            full_path = parts[1]
        else:
            full_path = uri

        # Split into parent_path + title
        path_segments = full_path.rsplit("/", 1)
        if len(path_segments) == 2:
            parent_path, title = path_segments[0], path_segments[1]
        else:
            parent_path, title = "", path_segments[0]

        try:
            # Ensure all ancestors exist first
            await _ensure_parent_chain(client, domain, full_path)
            await client.create_memory(
                parent_path=parent_path,
                content=content,
                priority=10,
                title=title,
                domain=domain,
            )
        except Exception:
            # Best effort — some memories may fail due to constraints
            pass


def make_temp_db_url() -> str:
    """Create a temporary SQLite DB file and return its async URL."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="ablation_real_")
    os.close(fd)
    return f"sqlite+aiosqlite:///{path}"


# ---------------------------------------------------------------------------
# 3-tier LLM provider fallback
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMProvider:
    """A resolved LLM provider endpoint."""
    tier: int
    api_base: str
    model: str
    api_key: str
    degraded: bool  # True if tier 3 (different model family)

    @property
    def label(self) -> str:
        return f"tier{self.tier}:{self.model}@{self.api_base.split('//')[1].split('/')[0]}"


def _probe_llm_tier(api_base: str, api_key: str, model: str, timeout: float = 40.0) -> bool:
    """Quick health probe on a single LLM endpoint."""
    import json
    import urllib.request

    url = f"{api_base}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "max_tokens": 10,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return bool(result.get("choices"))
    except Exception:
        return False


def select_llm_provider(*, timeout: float = 40.0) -> Optional[LLMProvider]:
    """Probe configured tiers in order, return first healthy provider or None."""
    import sys
    tier_configs = build_benchmark_llm_tiers()
    if not tier_configs:
        print(
            "[provider] No benchmark LLM tier configured. "
            "Set BENCHMARK_LLM_API_BASE/MODEL or INTENT_LLM_* / WRITE_GUARD_LLM_*.",
            file=sys.stderr,
            flush=True,
        )
        return None

    for tier_cfg in tier_configs:
        ok = _probe_llm_tier(
            tier_cfg["api_base"], tier_cfg["api_key"],
            tier_cfg["model"], timeout,
        )
        if ok:
            provider = LLMProvider(
                tier=tier_cfg["tier"],
                api_base=tier_cfg["api_base"],
                model=tier_cfg["model"],
                api_key=tier_cfg["api_key"],
                degraded=tier_cfg["degraded"],
            )
            print(f"[provider] Selected {provider.label}", file=sys.stderr, flush=True)
            return provider
        print(f"[provider] Tier {tier_cfg['tier']} ({tier_cfg['api_base']}) failed",
              file=sys.stderr, flush=True)
    return None


def apply_llm_provider(
    monkeypatch: pytest.MonkeyPatch,
    provider: LLMProvider,
) -> None:
    """Set env vars to route all LLM calls through the selected provider."""
    for prefix in ("WRITE_GUARD_LLM", "INTENT_LLM", "COMPACT_GIST_LLM"):
        monkeypatch.setenv(f"{prefix}_API_BASE", provider.api_base)
        monkeypatch.setenv(f"{prefix}_API_KEY", provider.api_key)
        monkeypatch.setenv(f"{prefix}_MODEL", provider.model)
