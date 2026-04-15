from __future__ import annotations

import json
from typing import Any, Dict

import pytest

import mcp_server


async def _noop_async(*_: Any, **__: Any) -> None:
    return None


class _SearchClient:
    def __init__(
        self,
        *,
        global_results: list[dict[str, Any]],
        current_memories: dict[tuple[str, str], dict[str, Any] | None],
    ) -> None:
        self._global_results = global_results
        self._current_memories = current_memories

    def preprocess_query(self, query: str) -> Dict[str, Any]:
        return {
            "original_query": query,
            "normalized_query": query.strip().lower(),
            "rewritten_query": query.strip(),
            "tokens": query.split(),
            "changed": False,
        }

    def classify_intent(self, _query: str, _rewritten_query: str) -> Dict[str, Any]:
        return {
            "intent": "factual",
            "strategy_template": "factual_high_precision",
            "method": "rule",
            "confidence": 0.9,
            "signals": ["rule_match"],
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
        _ = query
        _ = mode
        _ = max_results
        _ = candidate_multiplier
        _ = filters
        _ = intent_profile
        return {
            "mode": "hybrid",
            "degraded": False,
            "degrade_reasons": [],
            "results": list(self._global_results),
            "metadata": {
                "intent": "factual",
                "strategy_template": "factual_high_precision",
                "candidate_multiplier_applied": candidate_multiplier,
            },
        }

    async def get_memory_by_path(
        self,
        path: str,
        domain: str = "core",
        reinforce_access: bool = True,
    ) -> dict[str, Any] | None:
        _ = reinforce_access
        return self._current_memories.get((domain, path))


@pytest.mark.asyncio
async def test_search_memory_replaces_session_duplicates_and_sorts_before_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SearchClient(
        global_results=[
            {
                "uri": "core://agent/dup",
                "domain": "core",
                "path": "agent/dup",
                "memory_id": 11,
                "snippet": "global duplicate result",
                "score": 0.9,
                "updated_at": "2026-03-24T00:00:00Z",
            },
            {
                "uri": "core://agent/global-high",
                "domain": "core",
                "path": "agent/global-high",
                "memory_id": 12,
                "snippet": "global high result",
                "score": 0.8,
                "updated_at": "2026-03-24T00:00:00Z",
            },
        ],
        current_memories={
            ("core", "agent/dup"): {
                "id": 11,
                "content": "fresh duplicate content from database",
                "created_at": "2026-03-24T00:00:00Z",
            },
            ("core", "agent/global-high"): {
                "id": 12,
                "content": "global high content from database",
                "created_at": "2026-03-24T00:00:00Z",
            },
            ("core", "agent/session-top"): {
                "id": 21,
                "content": "session top content from database",
                "created_at": "2026-03-24T00:00:00Z",
            },
        },
    )

    async def _session_search(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "uri": "core://agent/dup",
                "domain": "core",
                "path": "agent/dup",
                "memory_id": 10,
                "snippet": "stale session duplicate",
                "score": 0.2,
                "updated_at": "2026-03-20T00:00:00Z",
                "source": "session_queue",
            },
            {
                "uri": "core://agent/session-top",
                "domain": "core",
                "path": "agent/session-top",
                "memory_id": 21,
                "snippet": "session top result",
                "score": 0.95,
                "updated_at": "2026-03-24T00:00:00Z",
                "source": "session_queue",
            },
        ]

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server.runtime_state.session_cache, "search", _session_search)

    raw = await mcp_server.search_memory(
        query="release plan",
        mode="hybrid",
        max_results=2,
        include_session=True,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert [item["uri"] for item in payload["results"]] == [
        "core://agent/session-top",
        "core://agent/dup",
    ]
    assert payload["results"][1]["snippet"] == "fresh duplicate content from database"
    assert payload["session_first_metrics"]["session_replaced_by_global"] == 1
    assert payload["session_first_metrics"]["sorted_by_score"] is True


@pytest.mark.asyncio
async def test_search_memory_revalidation_drops_missing_session_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SearchClient(
        global_results=[],
        current_memories={
            ("core", "agent/live"): {
                "id": 31,
                "content": "live entry still exists",
                "created_at": "2026-03-24T00:00:00Z",
            },
            ("core", "agent/missing"): None,
        },
    )

    async def _session_search(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "uri": "core://agent/missing",
                "domain": "core",
                "path": "agent/missing",
                "memory_id": 30,
                "snippet": "ghost result",
                "score": 0.99,
                "updated_at": "2026-03-24T00:00:00Z",
                "source": "session_queue",
            },
            {
                "uri": "core://agent/live",
                "domain": "core",
                "path": "agent/live",
                "memory_id": 31,
                "snippet": "live result",
                "score": 0.5,
                "updated_at": "2026-03-24T00:00:00Z",
                "source": "session_queue",
            },
        ]

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server.runtime_state.session_cache, "search", _session_search)

    raw = await mcp_server.search_memory(
        query="release plan",
        mode="hybrid",
        include_session=True,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert [item["uri"] for item in payload["results"]] == ["core://agent/live"]
    assert payload["session_first_metrics"]["revalidation_dropped"] == 1


@pytest.mark.asyncio
async def test_search_memory_verbose_false_omits_debug_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _SearchClient(
        global_results=[
            {
                "uri": "core://agent/index",
                "domain": "core",
                "path": "agent/index",
                "memory_id": 1,
                "snippet": "index report",
                "score": 0.7,
                "updated_at": "2026-03-24T00:00:00Z",
            }
        ],
        current_memories={
            ("core", "agent/index"): {
                "id": 1,
                "content": "index report",
                "created_at": "2026-03-24T00:00:00Z",
            }
        },
    )

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory(
        query="release plan",
        mode="hybrid",
        include_session=False,
        verbose=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["results"][0]["uri"] == "core://agent/index"
    assert "query_preprocess" not in payload
    assert "session_first_metrics" not in payload
    assert "backend_metadata" not in payload
