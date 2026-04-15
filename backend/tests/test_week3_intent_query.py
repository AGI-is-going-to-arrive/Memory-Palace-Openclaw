import json
import logging
from pathlib import Path
from typing import Any, Dict

import pytest

import mcp_server
from api import maintenance as maintenance_api
from db.sqlite_client import SQLiteClient


async def _noop_async(*_: Any, **__: Any) -> None:
    return None


class _FakeSearchClient:
    def __init__(self) -> None:
        self.search_query: str = ""
        self.intent_profile: Dict[str, Any] = {}
        self.received_filters: Dict[str, Any] = {}

    def preprocess_query(self, query: str) -> Dict[str, Any]:
        rewritten = " ".join(query.lower().replace("?", "").split())
        return {
            "original_query": query,
            "normalized_query": rewritten,
            "rewritten_query": rewritten,
            "tokens": rewritten.split(),
            "changed": rewritten != query,
        }

    def classify_intent(self, _query: str, rewritten_query: str) -> Dict[str, Any]:
        if "when" in rewritten_query:
            return {
                "intent": "temporal",
                "strategy_template": "temporal_time_filtered",
                "method": "keyword_heuristic",
                "confidence": 0.86,
                "signals": ["temporal_keywords"],
            }
        if "why" in rewritten_query:
            return {
                "intent": "causal",
                "strategy_template": "causal_wide_pool",
                "method": "keyword_heuristic",
                "confidence": 0.82,
                "signals": ["causal_keywords"],
            }
        return {
            "intent": "factual",
            "strategy_template": "factual_high_precision",
            "method": "keyword_heuristic",
            "confidence": 0.72,
            "signals": ["default_factual"],
        }

    async def search_advanced(
        self,
        *,
        query: str,
        mode: str,
        max_results: int,
        candidate_multiplier: int,
        filters: Dict[str, Any],
        intent_profile: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        self.search_query = query
        self.intent_profile = dict(intent_profile or {})
        self.received_filters = dict(filters)
        _ = mode
        _ = max_results
        _ = candidate_multiplier
        return {
            "mode": "hybrid",
            "backend_method": "hybrid_trace",
            "degraded": False,
            "degrade_reasons": [],
            "metadata": {
                "intent": self.intent_profile.get("intent"),
                "strategy_template": self.intent_profile.get("strategy_template"),
                "candidate_multiplier_applied": candidate_multiplier,
                "stage_timings_ms": {
                    "rewrite": 1.2,
                    "vector_lookup": 5.8,
                    "rerank": 1.5,
                },
                "candidate_counts": {
                    "prefilter": 24,
                    "postfilter": 12,
                    "returned": 8,
                },
                "mmr_applied": True,
                "mmr_lambda": 0.35,
                "rerank_model": "cross-encoder-mini",
                "rerank_top_k": 12,
                "vector_engine_name": "faiss_hnsw",
                "vector_engine_latency_ms": 4.2,
            },
            "results": [
                {
                    "uri": "core://agent/index",
                    "memory_id": 11,
                    "snippet": "Index rebuilt last night.",
                    "priority": 1,
                    "updated_at": "2026-02-16T12:00:00Z",
                    "scores": {"final": 0.9, "text": 0.6, "vector": 0.7},
                    "metadata": {
                        "domain": "core",
                        "path": "agent/index",
                        "priority": 1,
                        "updated_at": "2026-02-16T12:00:00Z",
                    },
                }
            ],
        }


class _IntentLlmSearchClient(_FakeSearchClient):
    async def classify_intent_with_llm(
        self, _query: str, _rewritten_query: str
    ) -> Dict[str, Any]:
        return {
            "intent": "causal",
            "strategy_template": "causal_wide_pool",
            "method": "intent_llm",
            "confidence": 0.91,
            "signals": ["intent_llm:causal"],
            "intent_llm_enabled": True,
            "intent_llm_applied": True,
        }


class _IntentLlmFailureSearchClient(_FakeSearchClient):
    async def classify_intent_with_llm(
        self, _query: str, _rewritten_query: str
    ) -> Dict[str, Any]:
        raise RuntimeError("intent_llm_forced_failure")


class _NoIntentClient:
    def __init__(self) -> None:
        self.search_query: str = ""
        self.intent_profile: Dict[str, Any] = {}

    def preprocess_query(self, query: str) -> Dict[str, Any]:
        return {
            "original_query": query,
            "normalized_query": query,
            "rewritten_query": query,
            "tokens": [],
            "changed": False,
        }

    async def search_advanced(
        self,
        *,
        query: str,
        mode: str,
        max_results: int,
        candidate_multiplier: int,
        filters: Dict[str, Any],
        intent_profile: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        self.search_query = query
        self.intent_profile = dict(intent_profile or {})
        _ = mode
        _ = max_results
        _ = candidate_multiplier
        _ = filters
        return {"mode": "hybrid", "degraded": False, "degrade_reasons": [], "results": []}


class _LegacySearchClient:
    def __init__(self) -> None:
        self.search_query: str = ""
        self.received_filters: Dict[str, Any] = {}

    def preprocess_query(self, query: str) -> Dict[str, Any]:
        return {
            "original_query": query,
            "normalized_query": query,
            "rewritten_query": query,
            "tokens": [],
            "changed": False,
        }

    def classify_intent(self, _query: str, _rewritten_query: str) -> Dict[str, Any]:
        return {
            "intent": "factual",
            "strategy_template": "factual_high_precision",
            "method": "keyword_heuristic",
            "confidence": 0.72,
            "signals": ["default_factual"],
        }

    async def search_advanced(
        self,
        *,
        query: str,
        mode: str,
        max_results: int,
        candidate_multiplier: int,
        filters: Dict[str, Any],
    ) -> Dict[str, Any]:
        self.search_query = query
        self.received_filters = dict(filters)
        _ = mode
        _ = max_results
        _ = candidate_multiplier
        return {"mode": "hybrid", "degraded": False, "degrade_reasons": [], "results": []}


class _SearchRevalidationClient(_FakeSearchClient):
    def __init__(
        self,
        *,
        results: list[dict[str, Any]],
        current_memories: dict[tuple[str, str], dict[str, Any] | None],
    ) -> None:
        super().__init__()
        self._results = results
        self._current_memories = current_memories
        self.path_reads: list[tuple[str, str, bool]] = []

    async def search_advanced(
        self,
        *,
        query: str,
        mode: str,
        max_results: int,
        candidate_multiplier: int,
        filters: Dict[str, Any],
        intent_profile: Dict[str, Any] | None = None,
        verbose: bool | None = None,
    ) -> Dict[str, Any]:
        payload = await super().search_advanced(
            query=query,
            mode=mode,
            max_results=max_results,
            candidate_multiplier=candidate_multiplier,
            filters=filters,
            intent_profile=intent_profile,
        )
        payload["results"] = list(self._results)
        self.intent_profile = dict(intent_profile or {})
        if verbose is False:
            payload["metadata"] = {
                "intent": self.intent_profile.get("intent"),
                "strategy_template": self.intent_profile.get("strategy_template"),
                "candidate_multiplier_applied": candidate_multiplier,
            }
        return payload

    async def get_memory_by_path(
        self,
        path: str,
        domain: str = "core",
        reinforce_access: bool = True,
    ) -> dict[str, Any] | None:
        self.path_reads.append((domain, path, reinforce_access))
        return self._current_memories.get((domain, path))


@pytest.mark.asyncio
async def test_search_memory_uses_preprocessed_query_and_returns_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeSearchClient()
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory(
        query="When did we rebuild index?",
        mode="hybrid",
        include_session=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["query"] == "When did we rebuild index?"
    assert payload["query_effective"] == "when did we rebuild index"
    assert payload["intent"] == "temporal"
    assert payload["intent_profile"]["strategy_template"] == "temporal_time_filtered"
    assert fake_client.search_query == "when did we rebuild index"
    assert fake_client.intent_profile["intent"] == "temporal"
    assert payload["intent_applied"] == "temporal"
    assert payload["strategy_template_applied"] == "temporal_time_filtered"
    assert payload["candidate_multiplier_applied"] == 4


@pytest.mark.asyncio
async def test_search_memory_degrades_to_unknown_when_classifier_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _NoIntentClient()
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory(
        query="index diagnostics",
        mode="hybrid",
        include_session=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["intent"] == "unknown"
    assert payload["strategy_template"] == "default"
    assert "intent_classification_unavailable" in payload.get("degrade_reasons", [])
    assert fake_client.intent_profile == {}


@pytest.mark.asyncio
async def test_search_memory_supports_legacy_search_advanced_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _LegacySearchClient()
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory(
        query="legacy compatibility",
        mode="hybrid",
        include_session=False,
        filters={"domain": "core"},
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["search_api_kind"] == "advanced"
    assert fake_client.search_query == "legacy compatibility"
    assert fake_client.received_filters == {"domain": "core"}
    assert "intent_profile_not_supported_by_search_api" in payload.get(
        "degrade_reasons", []
    )


@pytest.mark.asyncio
async def test_search_memory_marks_degrade_when_intent_llm_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeSearchClient()
    monkeypatch.setattr(mcp_server, "INTENT_LLM_ENABLED", True)
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory(
        query="Why did rebuild fail?",
        mode="hybrid",
        include_session=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["intent_llm_enabled"] is True
    assert payload["intent_llm_applied"] is False
    assert "intent_llm_unavailable" in payload.get("degrade_reasons", [])


@pytest.mark.asyncio
async def test_search_memory_uses_intent_llm_profile_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _IntentLlmSearchClient()
    monkeypatch.setattr(mcp_server, "INTENT_LLM_ENABLED", True)
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory(
        query="Why did rebuild fail?",
        mode="hybrid",
        include_session=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["intent"] == "causal"
    assert payload["intent_llm_enabled"] is True
    assert payload["intent_llm_applied"] is True
    assert fake_client.intent_profile["method"] == "intent_llm"


@pytest.mark.asyncio
async def test_search_memory_falls_back_to_rule_classifier_when_intent_llm_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _IntentLlmFailureSearchClient()
    monkeypatch.setattr(mcp_server, "INTENT_LLM_ENABLED", True)
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory(
        query="Why did rebuild fail?",
        mode="hybrid",
        include_session=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["intent"] == "causal"
    assert payload["strategy_template"] == "causal_wide_pool"
    assert payload["intent_llm_enabled"] is True
    assert payload["intent_llm_applied"] is False
    assert "intent_classification_failed" in payload.get("degrade_reasons", [])
    assert "intent_llm_fallback_rule_applied" in payload.get("degrade_reasons", [])


@pytest.mark.asyncio
async def test_search_memory_revalidates_missing_session_hit_and_reorders_by_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _SearchRevalidationClient(
        results=[
            {
                "uri": "core://agent/fresh-global",
                "memory_id": 21,
                "snippet": "global high score",
                "priority": 2,
                "updated_at": "2026-02-16T12:00:00Z",
                "scores": {"final": 0.97, "text": 0.74, "vector": 0.88},
                "metadata": {
                    "domain": "core",
                    "path": "agent/fresh-global",
                    "priority": 2,
                    "updated_at": "2026-02-16T12:00:00Z",
                },
            },
            {
                "uri": "core://agent/shared",
                "memory_id": 31,
                "snippet": "fresh global snippet",
                "priority": 0,
                "updated_at": "2026-02-17T12:00:00Z",
                "scores": {"final": 0.81, "text": 0.66, "vector": 0.71},
                "metadata": {
                    "domain": "core",
                    "path": "agent/shared",
                    "priority": 0,
                    "updated_at": "2026-02-17T12:00:00Z",
                },
            },
        ],
        current_memories={
            ("core", "agent/fresh-global"): {
                "id": 21,
                "content": "global high score content",
                "priority": 2,
                "created_at": "2026-02-16T12:00:00Z",
            },
            ("core", "agent/shared"): {
                "id": 31,
                "content": "shared record refreshed from storage",
                "priority": 0,
                "created_at": "2026-02-17T12:00:00Z",
            },
            ("core", "agent/session-only"): {
                "id": 41,
                "content": "session only record still exists",
                "priority": 1,
                "created_at": "2026-02-15T12:00:00Z",
            },
            ("core", "agent/missing"): None,
        },
    )

    async def _session_search(*, session_id: str, query: str, limit: int):
        _ = (session_id, query, limit)
        return [
            {
                "uri": "core://agent/missing",
                "domain": "core",
                "path": "agent/missing",
                "memory_id": 99,
                "snippet": "stale ghost result",
                "priority": 0,
                "score": 0.99,
                "updated_at": "2026-02-14T12:00:00Z",
                "source": "session_queue",
                "match_type": "session_queue",
            },
            {
                "uri": "core://agent/shared",
                "domain": "core",
                "path": "agent/shared",
                "memory_id": 30,
                "snippet": "stale session snippet",
                "priority": 5,
                "score": 0.91,
                "updated_at": "2026-02-14T12:00:00Z",
                "source": "session_queue",
                "match_type": "session_queue",
            },
            {
                "uri": "core://agent/session-only",
                "domain": "core",
                "path": "agent/session-only",
                "memory_id": 41,
                "snippet": "session only stale snippet",
                "priority": 1,
                "score": 0.52,
                "updated_at": "2026-02-13T12:00:00Z",
                "source": "session_queue",
                "match_type": "session_queue",
            },
        ]

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server.runtime_state.session_cache, "search", _session_search)

    raw = await mcp_server.search_memory(
        query="shared workflow",
        mode="hybrid",
        max_results=3,
        include_session=True,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert [item["uri"] for item in payload["results"]] == [
        "core://agent/fresh-global",
        "core://agent/shared",
        "core://agent/session-only",
    ]
    assert payload["results"][1]["snippet"] != "stale session snippet"
    assert payload["results"][1]["memory_id"] == 31
    assert payload["session_first_metrics"]["session_replaced_by_global"] == 1
    assert payload["session_first_metrics"]["revalidation_dropped"] == 1
    assert payload["session_first_metrics"]["sorted_by_score"] is True
    assert ("core", "agent/missing", False) in fake_client.path_reads
    assert ("core", "agent/shared", False) in fake_client.path_reads


@pytest.mark.asyncio
async def test_search_memory_verbose_false_prunes_high_noise_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _SearchRevalidationClient(
        results=[
            {
                "uri": "core://agent/index",
                "memory_id": 11,
                "snippet": "Index rebuilt last night.",
                "priority": 1,
                "updated_at": "2026-02-16T12:00:00Z",
                "scores": {"final": 0.9, "text": 0.6, "vector": 0.7},
                "metadata": {
                    "domain": "core",
                    "path": "agent/index",
                    "priority": 1,
                    "updated_at": "2026-02-16T12:00:00Z",
                },
            }
        ],
        current_memories={
            ("core", "agent/index"): {
                "id": 11,
                "content": "Index rebuilt last night.",
                "priority": 1,
                "created_at": "2026-02-16T12:00:00Z",
            }
        },
    )
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory(
        query="index diagnostics",
        mode="hybrid",
        include_session=False,
        verbose=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert "query_preprocess" not in payload
    assert "intent_profile" not in payload
    assert "backend_metadata" not in payload
    assert "session_first_metrics" not in payload
    assert "search_verbose_not_supported_by_search_api" not in payload.get(
        "degrade_reasons", []
    )


@pytest.mark.asyncio
async def test_observability_search_returns_intent_and_query_effective(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeSearchClient()

    async def _ensure_started(_factory) -> None:
        return None

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()

    payload = maintenance_api.SearchConsoleRequest(
        query="Why did index rebuild fail?",
        mode="hybrid",
        include_session=False,
    )
    response = await maintenance_api.run_observability_search(payload)

    assert response["ok"] is True
    assert response["query"] == "Why did index rebuild fail?"
    assert response["query_effective"] == "why did index rebuild fail"
    assert response["intent"] == "causal"
    assert response["intent_profile"]["strategy_template"] == "causal_wide_pool"
    assert fake_client.search_query == "why did index rebuild fail"
    assert fake_client.intent_profile["intent"] == "causal"
    assert fake_client.received_filters == {}
    assert response["backend_method"] == "hybrid_trace"
    assert response["candidate_multiplier_applied"] == 4
    assert response["search_trace"]["stage_timings_ms"]["vector_lookup"] == 5.8
    assert response["search_trace"]["candidate_counts"]["returned"] == 8
    assert response["search_trace"]["mmr"]["applied"] is True
    assert response["search_trace"]["rerank"]["model"] == "cross-encoder-mini"
    assert response["search_trace"]["vector_engine"]["name"] == "faiss_hnsw"


@pytest.mark.asyncio
async def test_observability_search_falls_back_to_rule_classifier_when_intent_llm_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _IntentLlmFailureSearchClient()

    async def _ensure_started(_factory) -> None:
        return None

    monkeypatch.setattr(maintenance_api, "_INTENT_LLM_ENABLED", True)
    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)

    payload = maintenance_api.SearchConsoleRequest(
        query="Why did index rebuild fail?",
        mode="hybrid",
        include_session=False,
    )
    response = await maintenance_api.run_observability_search(payload)

    assert response["ok"] is True
    assert response["intent"] == "causal"
    assert response["intent_applied"] == "causal"
    assert response["strategy_template"] == "causal_wide_pool"
    assert response["intent_llm_enabled"] is True
    assert response["intent_llm_applied"] is False
    assert "intent_classification_failed" in response["degrade_reasons"]
    assert "intent_llm_fallback_rule_applied" in response["degrade_reasons"]


@pytest.mark.asyncio
async def test_observability_search_exposes_session_first_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeSearchClient()

    async def _ensure_started(_factory) -> None:
        return None

    async def _session_search(*_args: Any, **_kwargs: Any):
        return [
            {
                "uri": "core://agent/index",
                "memory_id": 11,
                "snippet": "session cached entry",
                "priority": 1,
                "score": 0.95,
                "keyword_score": 0.91,
                "updated_at": "2026-02-16T12:00:00Z",
            }
        ]

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.session_cache, "search", _session_search
    )

    payload = maintenance_api.SearchConsoleRequest(
        query="index diagnostics",
        mode="hybrid",
        include_session=True,
    )
    response = await maintenance_api.run_observability_search(payload)

    assert response["ok"] is True
    metrics = response["session_first_metrics"]
    assert metrics["session_candidates"] == 1
    assert metrics["global_candidates"] == 1
    assert metrics["dedup_dropped"] == 1
    assert metrics["session_contributed"] == 1
    assert metrics["global_contributed"] == 0


@pytest.mark.asyncio
async def test_observability_search_marks_degrade_when_session_cache_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeSearchClient()

    async def _ensure_started(_factory) -> None:
        return None

    async def _session_search_fail(*_args: Any, **_kwargs: Any):
        raise RuntimeError("session_cache_forced_failure")

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.session_cache, "search", _session_search_fail
    )

    payload = maintenance_api.SearchConsoleRequest(
        query="index diagnostics",
        mode="hybrid",
        include_session=True,
    )
    response = await maintenance_api.run_observability_search(payload)

    assert response["ok"] is True
    assert response["degraded"] is True
    assert "session_cache_lookup_failed" in response.get("degrade_reasons", [])


@pytest.mark.asyncio
async def test_search_memory_logs_warning_when_session_cache_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_client = _FakeSearchClient()

    async def _session_search_fail(*_args: Any, **_kwargs: Any):
        raise RuntimeError("session_cache_forced_failure")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server.runtime_state.session_cache, "search", _session_search_fail)

    with caplog.at_level(logging.WARNING, logger="mcp_tool_search"):
        raw = await mcp_server.search_memory(
            query="index diagnostics",
            mode="hybrid",
            include_session=True,
        )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["degraded"] is True
    assert (
        "session queue lookup failed; continued with global retrieval only."
        in payload.get("degrade_reasons", [])
    )
    assert "Session cache lookup failed for query 'index diagnostics'" in caplog.text


@pytest.mark.asyncio
async def test_observability_search_accepts_integer_max_priority_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeSearchClient()

    async def _ensure_started(_factory) -> None:
        return None

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()

    payload = maintenance_api.SearchConsoleRequest(
        query="index diagnostics",
        mode="hybrid",
        include_session=False,
        filters={"max_priority": "2"},
    )
    response = await maintenance_api.run_observability_search(payload)

    assert response["ok"] is True
    assert fake_client.received_filters == {"max_priority": 2}


@pytest.mark.asyncio
async def test_observability_search_rejects_non_integer_max_priority_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeSearchClient()

    async def _ensure_started(_factory) -> None:
        return None

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)

    payload = maintenance_api.SearchConsoleRequest(
        query="index diagnostics",
        mode="hybrid",
        include_session=False,
        filters={"max_priority": "1.9"},
    )
    with pytest.raises(maintenance_api.HTTPException) as exc_info:
        await maintenance_api.run_observability_search(payload)

    assert exc_info.value.status_code == 422
    assert "filters.max_priority must be an integer" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_preprocess_query_preserves_uri_and_multilingual_content(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "week3-preprocess.db"
    client = SQLiteClient(f"sqlite+aiosqlite:///{db_path}")

    uri_result = client.preprocess_query("core://agent/index")
    mixed_lang_result = client.preprocess_query("昨天 index 为什么失败")

    await client.close()

    assert uri_result["rewritten_query"] == "core://agent/index"
    assert mixed_lang_result["rewritten_query"] == "昨天 index 为什么失败"


@pytest.mark.asyncio
async def test_observability_session_cache_uses_original_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeSearchClient()
    captured: Dict[str, Any] = {}

    async def _ensure_started(_factory) -> None:
        return None

    async def _session_search(*, session_id: str, query: str, limit: int):
        captured["session_id"] = session_id
        captured["query"] = query
        captured["limit"] = limit
        return []

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(maintenance_api.runtime_state.session_cache, "search", _session_search)

    payload = maintenance_api.SearchConsoleRequest(
        query="Why did index rebuild fail?",
        mode="hybrid",
        include_session=True,
        session_id="api-observability",
    )
    response = await maintenance_api.run_observability_search(payload)

    assert response["ok"] is True
    assert captured["query"] == "Why did index rebuild fail?"
    assert captured["session_id"] == "api-observability"
