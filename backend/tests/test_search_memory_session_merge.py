import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

import mcp_server


async def _noop_async(*_: Any, **__: Any) -> None:
    return None


class _SessionMergeSearchClient:
    def __init__(
        self,
        *,
        global_results: List[Dict[str, Any]],
        memories_by_key: Optional[Dict[Tuple[str, str], Optional[Dict[str, Any]]]] = None,
    ) -> None:
        self.global_results = list(global_results)
        self.memories_by_key = dict(memories_by_key or {})

    def preprocess_query(self, query: str) -> Dict[str, Any]:
        normalized = query.strip()
        return {
            "original_query": query,
            "normalized_query": normalized,
            "rewritten_query": normalized,
            "tokens": normalized.lower().split(),
            "changed": False,
        }

    def classify_intent(self, _query: str, _rewritten_query: str) -> Dict[str, Any]:
        return {
            "intent": "factual",
            "strategy_template": "factual_high_precision",
            "method": "rule",
            "confidence": 0.8,
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
            "results": list(self.global_results),
        }

    async def get_memory_by_path(
        self,
        path: str,
        domain: str,
        reinforce_access: bool = False,
    ) -> Optional[Dict[str, Any]]:
        _ = reinforce_access
        return self.memories_by_key.get((domain, path))


@pytest.mark.asyncio
async def test_search_memory_prefers_high_score_results_after_session_merge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _SessionMergeSearchClient(
        global_results=[
            {
                "uri": "core://global/high-score",
                "memory_id": 101,
                "snippet": "global high score",
                "priority": 1,
                "score": 0.91,
                "updated_at": "2026-03-20T00:00:00Z",
                "metadata": {"domain": "core", "path": "global/high-score"},
            }
        ],
        memories_by_key={
            ("core", "global/high-score"): {
                "id": 101,
                "content": "global high score refreshed",
                "priority": 1,
                "updated_at": "2026-03-20T00:00:00Z",
            }
        },
    )

    async def _session_search(*_args: Any, **_kwargs: Any) -> List[Dict[str, Any]]:
        return [
            {
                "uri": "core://session/low-score",
                "memory_id": 11,
                "snippet": "session low score",
                "priority": 1,
                "score": 0.12,
                "updated_at": "2026-03-19T00:00:00Z",
            }
        ]

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server.runtime_state.session_cache, "search", _session_search)

    raw = await mcp_server.search_memory(
        query="release plan",
        mode="hybrid",
        max_results=1,
        include_session=True,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["results"][0]["uri"] == "core://global/high-score"


@pytest.mark.asyncio
async def test_search_memory_replaces_session_duplicate_with_global_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_uri = "core://agent/index"
    fake_client = _SessionMergeSearchClient(
        global_results=[
            {
                "uri": target_uri,
                "memory_id": 7,
                "snippet": "fresh global snippet",
                "priority": 0,
                "score": 0.73,
                "updated_at": "2026-03-21T00:00:00Z",
                "metadata": {"domain": "core", "path": "agent/index"},
            }
        ],
        memories_by_key={
            ("core", "agent/index"): {
                "id": 7,
                "content": "fresh content from storage",
                "priority": 0,
                "updated_at": "2026-03-21T00:00:00Z",
            }
        },
    )

    async def _session_search(*_args: Any, **_kwargs: Any) -> List[Dict[str, Any]]:
        return [
            {
                "uri": target_uri,
                "memory_id": 7,
                "snippet": "stale session snippet",
                "priority": 0,
                "score": 0.11,
                "updated_at": "2026-03-18T00:00:00Z",
            }
        ]

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server.runtime_state.session_cache, "search", _session_search)

    raw = await mcp_server.search_memory(
        query="index report",
        mode="hybrid",
        max_results=3,
        include_session=True,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["results"][0]["uri"] == target_uri
    assert payload["results"][0]["snippet"] == "fresh content from storage"
    assert payload["session_first_metrics"]["dedup_dropped"] == 0
    assert payload["session_first_metrics"]["session_replaced_by_global"] == 1


@pytest.mark.asyncio
async def test_search_memory_drops_stale_session_result_missing_from_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _SessionMergeSearchClient(
        global_results=[],
        memories_by_key={("core", "stale/path"): None},
    )

    async def _session_search(*_args: Any, **_kwargs: Any) -> List[Dict[str, Any]]:
        return [
            {
                "uri": "core://stale/path",
                "memory_id": 55,
                "snippet": "ghost result",
                "priority": 2,
                "score": 0.88,
                "updated_at": "2026-03-17T00:00:00Z",
            }
        ]

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server.runtime_state.session_cache, "search", _session_search)

    raw = await mcp_server.search_memory(
        query="ghost result",
        mode="hybrid",
        max_results=3,
        include_session=True,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["results"] == []
    assert payload["count"] == 0
    assert payload["session_first_metrics"]["revalidation_dropped"] == 1


@pytest.mark.asyncio
async def test_search_memory_verbose_false_omits_debug_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _SessionMergeSearchClient(
        global_results=[
            {
                "uri": "core://agent/index",
                "memory_id": 9,
                "snippet": "concise result",
                "priority": 0,
                "score": 0.51,
                "updated_at": "2026-03-22T00:00:00Z",
                "metadata": {"domain": "core", "path": "agent/index"},
            }
        ],
        memories_by_key={
            ("core", "agent/index"): {
                "id": 9,
                "content": "durable content",
                "priority": 0,
                "updated_at": "2026-03-22T00:00:00Z",
            }
        },
    )

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory(
        query="index",
        mode="hybrid",
        max_results=3,
        include_session=False,
        verbose=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["results"][0]["snippet"] == "durable content"
    assert "query_preprocess" not in payload
    assert "intent_profile" not in payload
    assert "session_first_metrics" not in payload
    assert "backend_metadata" not in payload
    assert "backend_method" not in payload
