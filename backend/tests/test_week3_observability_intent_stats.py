import asyncio
import json
from pathlib import Path
from typing import Any, Dict

import pytest

from api import maintenance as maintenance_api


class _FakeIntentClient:
    def __init__(self) -> None:
        self.meta_store: Dict[str, str] = {}
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
        _ = query
        _ = mode
        _ = max_results
        _ = candidate_multiplier
        self.received_filters = dict(filters)
        profile = intent_profile or {}
        return {
            "mode": "hybrid",
            "backend_method": "hybrid_trace",
            "degraded": False,
            "degrade_reasons": [],
            "results": [],
            "metadata": {
                "intent": profile.get("intent"),
                "strategy_template": profile.get("strategy_template", "default"),
                "candidate_multiplier_applied": candidate_multiplier,
                "stage_timings_ms": {
                    "rewrite": 1.2,
                    "vector_lookup": 5.8,
                    "rerank": 1.5,
                },
                "candidate_counts": {
                    "prefilter": 24,
                    "postfilter": 12,
                    "returned": max_results,
                },
                "mmr_applied": True,
                "mmr_lambda": 0.35,
                "rerank_model": "cross-encoder-mini",
                "rerank_top_k": 12,
                "vector_engine_name": "faiss_hnsw",
                "vector_engine_latency_ms": 4.2,
            },
        }

    async def get_index_status(self) -> Dict[str, Any]:
        return {"degraded": False, "index_available": True}

    async def get_gist_stats(self) -> Dict[str, Any]:
        return {"degraded": False, "total_rows": 0, "active_coverage": 0.0}

    async def get_vitality_stats(self) -> Dict[str, Any]:
        return {"degraded": False, "total_memories": 0, "low_vitality_count": 0}

    async def get_runtime_meta(self, key: str) -> str | None:
        return self.meta_store.get(key)

    async def set_runtime_meta(self, key: str, value: str) -> None:
        self.meta_store[key] = value


class _LegacyIntentClient(_FakeIntentClient):
    async def search_advanced(
        self,
        *,
        query: str,
        mode: str,
        max_results: int,
        candidate_multiplier: int,
        filters: Dict[str, Any],
    ) -> Dict[str, Any]:
        _ = query
        _ = mode
        _ = max_results
        _ = candidate_multiplier
        _ = filters
        return {
            "mode": "hybrid",
            "degraded": False,
            "degrade_reasons": [],
            "results": [],
            "metadata": {
                "intent": None,
                "strategy_template": "default",
            },
        }


class _LegacySearchOnlyClient:
    def __init__(self) -> None:
        self.received_query: str = ""
        self.received_limit: int = 0
        self.received_domain: str | None = None

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
        return {
            "intent": "factual",
            "strategy_template": "factual_high_precision",
            "method": "keyword_heuristic",
            "confidence": 0.72,
            "signals": ["default_factual"],
        }

    async def search(
        self,
        query: str,
        limit: int = 10,
        domain: str | None = None,
    ) -> list[Dict[str, Any]]:
        self.received_query = query
        self.received_limit = limit
        self.received_domain = domain
        return [
            {
                "uri": "core://agent/index",
                "memory_id": 31,
                "snippet": "index diagnostics",
                "priority": 0,
                "updated_at": "2026-03-01T12:00:00Z",
                "domain": "core",
                "path": "agent/index",
            },
            {
                "uri": "core://agent/notes",
                "memory_id": 32,
                "snippet": "agent notes",
                "priority": 2,
                "updated_at": "2026-03-01T12:05:00Z",
                "domain": "core",
                "path": "agent/notes",
            },
        ]


class _RacePersistIntentClient(_FakeIntentClient):
    def __init__(self, delays: list[float]) -> None:
        super().__init__()
        self._delays = list(delays)
        self._set_call_count = 0

    async def set_runtime_meta(self, key: str, value: str) -> None:
        delay = 0.0
        if self._set_call_count < len(self._delays):
            delay = self._delays[self._set_call_count]
        self._set_call_count += 1
        if delay > 0:
            await asyncio.sleep(delay)
        await super().set_runtime_meta(key, value)


@pytest.mark.asyncio
async def test_observability_summary_tracks_intent_and_strategy_breakdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeIntentClient()

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()
    maintenance_api._search_events_loaded = False

    temporal_payload = maintenance_api.SearchConsoleRequest(
        query="When did we rebuild index?",
        mode="hybrid",
        include_session=False,
    )
    causal_payload = maintenance_api.SearchConsoleRequest(
        query="Why did rebuild fail?",
        mode="hybrid",
        include_session=False,
    )

    temporal_result = await maintenance_api.run_observability_search(temporal_payload)
    causal_result = await maintenance_api.run_observability_search(causal_payload)
    summary = await maintenance_api.get_observability_summary()

    assert temporal_result["intent"] == "temporal"
    assert temporal_result["strategy_template"] == "temporal_time_filtered"
    assert causal_result["intent"] == "causal"
    assert causal_result["strategy_template"] == "causal_wide_pool"

    stats = summary["search_stats"]
    assert stats["intent_breakdown"]["temporal"] == 1
    assert stats["intent_breakdown"]["causal"] == 1
    assert stats["strategy_hit_breakdown"]["temporal_time_filtered"] == 1
    assert stats["strategy_hit_breakdown"]["causal_wide_pool"] == 1
    assert stats["search_trace"]["backend_method_breakdown"]["hybrid_trace"] == 2
    assert stats["search_trace"]["candidate_multiplier_applied"]["avg"] == 4.0
    assert stats["search_trace"]["stage_timings_ms"]["vector_lookup"]["max"] == 5.8
    assert stats["search_trace"]["candidate_counts"]["returned"]["last"] == 8
    assert stats["search_trace"]["mmr"]["applied"]["last"] is True
    assert stats["search_trace"]["rerank"]["model"]["last"] == "cross-encoder-mini"
    assert stats["search_trace"]["vector_engine"]["name"]["last"] == "faiss_hnsw"
    assert len(stats["search_trace"]["recent_events"]) == 2


@pytest.mark.asyncio
async def test_observability_marks_strategy_applied_from_backend_metadata_on_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _LegacyIntentClient()

    async def _ensure_started(_factory) -> None:
        return None

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()
    maintenance_api._search_events_loaded = False

    payload = maintenance_api.SearchConsoleRequest(
        query="When did we rebuild index?",
        mode="hybrid",
        include_session=False,
    )
    result = await maintenance_api.run_observability_search(payload)

    assert result["intent"] == "temporal"
    assert result["strategy_template"] == "temporal_time_filtered"
    assert result["intent_applied"] == "unknown"
    assert result["strategy_template_applied"] == "default"
    assert "intent_profile_not_supported" in result["degrade_reasons"]


@pytest.mark.asyncio
async def test_observability_search_supports_legacy_search_api_without_search_advanced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _LegacySearchOnlyClient()

    async def _ensure_started(_factory) -> None:
        return None

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()
    maintenance_api._search_events_loaded = False

    payload = maintenance_api.SearchConsoleRequest(
        query="When did we rebuild index?",
        mode="hybrid",
        max_results=2,
        candidate_multiplier=3,
        include_session=False,
        scope_hint="core://agent/index",
    )
    result = await maintenance_api.run_observability_search(payload)

    assert result["ok"] is True
    assert result["backend_method"] == "search"
    assert result["search_api_kind"] == "legacy_fallback"
    assert result["mode_applied"] == "keyword"
    assert result["intent"] == "temporal"
    assert result["intent_applied"] == "unknown"
    assert result["strategy_template_applied"] == "default"
    assert result["scope_effective"] == {
        "domain": "core",
        "path_prefix": "agent/index",
    }
    assert fake_client.received_query == "when did we rebuild index"
    assert fake_client.received_limit == 6
    assert fake_client.received_domain == "core"
    assert [item["uri"] for item in result["results"]] == ["core://agent/index"]
    assert result["results"][0]["metadata"]["path"] == "agent/index"
    assert "search_api_compat_fallback:search" in result["degrade_reasons"]
    assert "intent_profile_not_supported" in result["degrade_reasons"]
    assert "intent_profile_not_supported_by_search_api" in result["degrade_reasons"]
    assert "mode_not_supported_by_search_api" in result["degrade_reasons"]
    assert "candidate_multiplier_not_supported_by_search_api" in result["degrade_reasons"]
    assert result["search_trace"]["search_api_kind"] == "legacy_fallback"
    assert any(
        reason.startswith("path_prefix filter dropped 1 result")
        for reason in result["degrade_reasons"]
    )


@pytest.mark.asyncio
async def test_observability_search_scope_hint_applies_and_echoes_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeIntentClient()

    async def _ensure_started(_factory) -> None:
        return None

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()
    maintenance_api._search_events_loaded = False

    payload = maintenance_api.SearchConsoleRequest(
        query="When did we rebuild index?",
        mode="hybrid",
        include_session=False,
        scope_hint="core://agent",
    )
    result = await maintenance_api.run_observability_search(payload)

    assert result["scope_hint"] == "core://agent"
    assert result["scope_hint_applied"] is True
    assert result["scope_strategy_applied"] == "uri_prefix"
    assert result["scope_effective"] == {"domain": "core", "path_prefix": "agent"}
    assert fake_client.received_filters == {"domain": "core", "path_prefix": "agent"}


@pytest.mark.asyncio
async def test_observability_summary_includes_sm_lite_runtime_stats(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_client = _FakeIntentClient()

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    async def _session_cache_summary() -> Dict[str, Any]:
        return {
            "session_count": 2,
            "total_hits": 6,
            "max_hits_in_session": 4,
            "max_hits_per_session": 200,
            "half_life_seconds": 21600.0,
            "top_sessions": [],
        }

    async def _flush_tracker_summary() -> Dict[str, Any]:
        return {
            "session_count": 1,
            "pending_events": 3,
            "pending_chars": 20,
            "trigger_chars": 6000,
            "min_events": 6,
            "max_events_per_session": 80,
            "top_sessions": [],
        }

    async def _promotion_summary() -> Dict[str, Any]:
        return {
            "total_promotions": 2,
            "degraded_promotions": 1,
            "avg_quality": 0.74,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.session_cache, "summary", _session_cache_summary
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.flush_tracker, "summary", _flush_tracker_summary
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.promotion_tracker, "summary", _promotion_summary
    )
    monkeypatch.setenv(
        maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV,
        str(tmp_path / "missing-transport-diagnostics.json"),
    )

    summary = await maintenance_api.get_observability_summary()

    assert summary["status"] == "ok"
    runtime = summary["health"]["runtime"]
    assert "sm_lite" in runtime
    assert runtime["sm_lite"]["session_cache"]["session_count"] == 2
    assert runtime["sm_lite"]["flush_tracker"]["pending_events"] == 3
    assert runtime["sm_lite"]["promotion"]["total_promotions"] == 2


@pytest.mark.asyncio
async def test_observability_summary_includes_sanitized_transport_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_client = _FakeIntentClient()
    snapshot_path = tmp_path / "transport-diagnostics.json"
    snapshot_path.write_text(
        json.dumps(
            {
                "updated_at": "2026-01-01T00:00:00Z",
                "status": "warn",
                "configured_transport": "auto",
                "active_transport": "stdio",
                "fallback_order": ["stdio", "sse"],
                "diagnostics": {
                    "connect_attempts": 3,
                    "connect_retry_count": 1,
                    "call_retry_count": 2,
                    "request_retries": 2,
                    "fallback_count": 1,
                    "reuse_count": 5,
                    "last_connected_at": "2026-01-01T00:00:00Z",
                    "connect_latency_ms": {
                        "last": 120.0,
                        "avg": 120.0,
                        "p95": 120.0,
                        "max": 120.0,
                        "samples": 1,
                    },
                    "last_error": "Authorization: Bearer super-secret",
                    "last_health_check_at": "2026-01-01T00:01:00Z",
                    "last_health_check_error": "token=doctor-secret",
                    "healthcheck_tool": "index_status",
                    "healthcheck_ttl_ms": 5000,
                    "recent_events": [
                        {
                            "at": "2026-01-01T00:01:00Z",
                            "category": "healthcheck",
                            "status": "fail",
                            "transport": "sse",
                            "tool": "index_status",
                            "message": "X-MCP-API-Key: hidden-value",
                        }
                    ],
                },
                "last_report": {
                    "command": "doctor",
                    "ok": False,
                    "status": "warn",
                    "summary": "doctor completed with warnings.",
                    "checks": [
                        {
                            "id": "transport-health",
                            "status": "fail",
                            "message": "Transport health check failed.",
                            "action": "Run doctor with apiKey=hidden",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )
    monkeypatch.setenv(
        maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV, str(snapshot_path)
    )

    summary = await maintenance_api.get_observability_summary()

    assert summary["status"] == "degraded"
    transport = summary["transport"]
    assert transport["available"] is True
    assert transport["degraded"] is True
    assert transport["active_transport"] == "stdio"
    assert transport["diagnostics"]["connect_attempts"] == 3
    assert transport["diagnostics"]["connect_latency_ms"] == {
        "last": 120.0,
        "avg": 120.0,
        "p95": 120.0,
        "max": 120.0,
        "samples": 1,
    }
    assert transport["diagnostics"]["last_error"] == "Authorization: Bearer [REDACTED]"
    assert transport["diagnostics"]["last_health_check_error"] == "token=[REDACTED]"
    assert (
        transport["diagnostics"]["recent_events"][0]["message"]
        == "X-MCP-API-Key: [REDACTED]"
    )
    breakdown = transport["diagnostics"]["exception_breakdown"]
    assert breakdown["total"] == 4
    assert breakdown["status_counts"] == {"fail": 3, "warn": 1}
    assert breakdown["source_counts"] == {
        "recent_events": 1,
        "last_report_checks": 1,
        "last_error": 1,
        "last_health_check_error": 1,
    }
    assert breakdown["category_counts"] == {
        "healthcheck": 3,
        "transport": 1,
    }
    assert breakdown["tool_counts"] == {"index_status": 3}
    assert breakdown["check_id_counts"] == {"transport-health": 1}
    assert breakdown["last_exception_at"] == "2026-01-01T00:01:00Z"
    assert breakdown["signature_breakdown"]["total"] == 3
    assert (
        breakdown["signature_breakdown"]["signature_counts"][
            "fail | transport | stdio | Authorization: Bearer [REDACTED]"
        ]
        == 1
    )
    assert (
        breakdown["signature_breakdown"]["signature_counts"][
            "fail | healthcheck | index_status | token=[REDACTED]"
        ]
        == 2
    )
    incident_breakdown = breakdown["incident_breakdown"]
    assert incident_breakdown["incident_count"] == 1
    assert incident_breakdown["canonical_cause_counts"] == {
        "healthcheck_auth_failure": 4,
    }
    assert any(
        item["canonical_cause"] == "healthcheck_auth_failure"
        and item["signal_count"] == 4
        and item["highest_status"] == "fail"
        and item["sources"]
        == ["recent_events", "last_report_checks", "last_error", "last_health_check_error"]
        for item in incident_breakdown["items"]
    )
    assert (
        transport["last_report"]["checks"][0]["action"]
        == "Run doctor with apiKey=[REDACTED]"
    )


@pytest.mark.asyncio
async def test_observability_summary_aggregates_multiple_transport_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_client = _FakeIntentClient()
    snapshot_path = tmp_path / "transport-diagnostics.json"
    instance_dir = tmp_path / "transport-diagnostics.instances"
    instance_dir.mkdir()
    snapshot_path.write_text(
        json.dumps(
            {
                "instance_id": "pid-legacy",
                "updated_at": "2026-01-01T00:00:00Z",
                "status": "pass",
                "configured_transport": "auto",
                "active_transport": "stdio",
                "diagnostics": {
                    "connect_attempts": 2,
                    "connect_retry_count": 0,
                    "call_retry_count": 1,
                    "request_retries": 2,
                    "fallback_count": 0,
                    "reuse_count": 3,
                    "connect_latency_ms": {
                        "last": 40.0,
                        "avg": 35.0,
                        "p95": 40.0,
                        "max": 40.0,
                        "samples": 2,
                    },
                    "recent_events": [
                        {
                            "at": "2026-01-01T00:00:00Z",
                            "category": "connect",
                            "status": "pass",
                            "transport": "stdio",
                            "latency_ms": 40.0,
                            "message": "connected",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (instance_dir / "pid-101.json").write_text(
        json.dumps(
            {
                "instance_id": "pid-101",
                "process_id": 101,
                "updated_at": "2026-01-01T00:02:00Z",
                "status": "warn",
                "configured_transport": "auto",
                "active_transport": "sse",
                "fallback_order": ["stdio", "sse"],
                "diagnostics": {
                    "connect_attempts": 3,
                    "connect_retry_count": 1,
                    "call_retry_count": 0,
                    "request_retries": 2,
                    "fallback_count": 1,
                    "reuse_count": 4,
                    "recent_events": [
                        {
                            "at": "2026-01-01T00:01:59Z",
                            "category": "connect",
                            "status": "warn",
                            "transport": "sse",
                            "latency_ms": 70.0,
                            "message": "connected after fallback",
                        },
                        {
                            "at": "2026-01-01T00:02:00Z",
                            "category": "healthcheck",
                            "status": "warn",
                            "transport": "sse",
                            "message": "retrying",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (instance_dir / "pid-102.json").write_text(
        json.dumps(
            {
                "instance_id": "pid-102",
                "process_id": 102,
                "updated_at": "2026-01-01T00:03:00Z",
                "status": "pass",
                "configured_transport": "stdio",
                "active_transport": "stdio",
                "diagnostics": {
                    "connect_attempts": 1,
                    "connect_retry_count": 0,
                    "call_retry_count": 2,
                    "request_retries": 1,
                    "fallback_count": 0,
                    "reuse_count": 1,
                    "connect_latency_ms": {
                        "last": 30.0,
                        "avg": 30.0,
                        "p95": 30.0,
                        "max": 30.0,
                        "samples": 1,
                    },
                    "recent_events": [
                        {
                            "at": "2026-01-01T00:02:59Z",
                            "category": "connect",
                            "status": "pass",
                            "transport": "stdio",
                            "latency_ms": 30.0,
                            "message": "connected",
                        },
                        {
                            "at": "2026-01-01T00:03:00Z",
                            "category": "tool_call",
                            "status": "pass",
                            "transport": "stdio",
                            "message": "tool call passed",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )
    monkeypatch.setenv(
        maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV, str(snapshot_path)
    )

    summary = await maintenance_api.get_observability_summary()

    assert summary["status"] == "degraded"
    transport = summary["transport"]
    assert transport["available"] is True
    assert transport["snapshot_count"] == 3
    assert transport["status"] == "warn"
    assert transport["active_transport"] == "sse"
    assert transport["configured_transports"] == ["stdio", "auto"]
    assert transport["diagnostics"]["connect_attempts"] == 6
    assert transport["diagnostics"]["reuse_count"] == 8
    assert transport["diagnostics"]["connect_latency_ms"] == {
        "last": 30.0,
        "avg": 42.5,
        "p95": 70.0,
        "max": 70.0,
        "samples": 4,
    }
    breakdown = transport["diagnostics"]["exception_breakdown"]
    assert breakdown["total"] == 2
    assert breakdown["status_counts"] == {"warn": 2}
    assert breakdown["source_counts"] == {"recent_events": 2}
    assert breakdown["category_counts"] == {"connect": 1, "healthcheck": 1}
    assert breakdown["tool_counts"] == {}
    assert breakdown["check_id_counts"] == {}
    assert breakdown["last_exception_at"] == "2026-01-01T00:02:00Z"
    assert breakdown["signature_breakdown"]["total"] == 2
    assert breakdown["signature_breakdown"]["signature_counts"] == {
        "warn | connect | sse | connected after fallback": 1,
        "warn | healthcheck | sse | retrying": 1,
    }
    assert breakdown["incident_breakdown"]["incident_count"] == 2
    assert breakdown["incident_breakdown"]["canonical_cause_counts"] == {
        "transport_connect_fallback": 1,
        "healthcheck | sse | retrying": 1,
    }
    assert len(transport["instances"]) == 3
    assert transport["instances"][0]["instance_id"] == "pid-102"
    assert transport["diagnostics"]["recent_events"][0]["message"] == "tool call passed"


@pytest.mark.asyncio
async def test_observability_summary_tolerates_partial_transport_snapshot_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_client = _FakeIntentClient()
    snapshot_path = tmp_path / "transport-diagnostics.json"
    instance_dir = tmp_path / "transport-diagnostics.instances"
    instance_dir.mkdir()
    snapshot_path.write_text(
        json.dumps(
            {
                "instance_id": "pid-legacy",
                "updated_at": "2026-01-01T00:00:00Z",
                "status": "pass",
                "configured_transport": "stdio",
                "active_transport": "stdio",
                "diagnostics": {
                    "connect_attempts": 1,
                    "reuse_count": 2,
                    "recent_events": [
                        {
                            "at": "2026-01-01T00:00:00Z",
                            "category": "connect",
                            "status": "pass",
                            "transport": "stdio",
                            "message": "connected",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (instance_dir / "pid-bad.json").write_text("{not-json", encoding="utf-8")
    (instance_dir / "pid-healthy.json").write_text(
        json.dumps(
            {
                "instance_id": "pid-healthy",
                "updated_at": "2026-01-01T00:01:00Z",
                "status": "warn",
                "configured_transport": "auto",
                "active_transport": "sse",
                "diagnostics": {
                    "connect_attempts": 2,
                    "reuse_count": 1,
                    "recent_events": [
                        {
                            "at": "2026-01-01T00:01:00Z",
                            "category": "healthcheck",
                            "status": "warn",
                            "transport": "sse",
                            "message": "retrying",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )
    monkeypatch.setenv(
        maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV, str(snapshot_path)
    )

    summary = await maintenance_api.get_observability_summary()

    assert summary["status"] == "degraded"
    transport = summary["transport"]
    assert transport["available"] is True
    assert transport["degraded"] is True
    assert transport["snapshot_count"] == 2
    assert transport["status"] == "warn"
    assert "pid-bad.json" in transport["reason"]
    assert transport["diagnostics"]["connect_attempts"] == 3
    assert transport["diagnostics"]["recent_events"][0]["message"] == "retrying"
    breakdown = transport["diagnostics"]["exception_breakdown"]
    assert breakdown["total"] == 2
    assert breakdown["status_counts"] == {"fail": 1, "warn": 1}
    assert breakdown["source_counts"] == {"snapshot_load": 1, "recent_events": 1}
    assert breakdown["category_counts"] == {"snapshot_load": 1, "healthcheck": 1}
    assert breakdown["last_exception_at"] == "2026-01-01T00:01:00Z"
    assert breakdown["signature_breakdown"]["total"] == 2
    assert any(
        key.startswith("fail | snapshot_load | pid-bad.json:")
        for key in breakdown["signature_breakdown"]["signature_counts"]
    )
    assert breakdown["incident_breakdown"]["incident_count"] == 2
    assert breakdown["incident_breakdown"]["canonical_cause_counts"] == {
        "transport_snapshot_load_failed": 1,
        "healthcheck | sse | retrying": 1,
    }


@pytest.mark.asyncio
async def test_observability_summary_keeps_non_focus_snapshot_errors_in_breakdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_client = _FakeIntentClient()
    snapshot_path = tmp_path / "transport-diagnostics.json"
    instance_dir = tmp_path / "transport-diagnostics.instances"
    instance_dir.mkdir()
    (instance_dir / "pid-focus.json").write_text(
        json.dumps(
            {
                "instance_id": "pid-focus",
                "updated_at": "2026-01-01T00:03:00Z",
                "status": "warn",
                "configured_transport": "auto",
                "active_transport": "sse",
                "diagnostics": {
                    "connect_attempts": 2,
                    "recent_events": [
                        {
                            "at": "2026-01-01T00:03:00Z",
                            "category": "healthcheck",
                            "status": "warn",
                            "transport": "sse",
                            "message": "retrying",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    (instance_dir / "pid-secondary.json").write_text(
        json.dumps(
            {
                "instance_id": "pid-secondary",
                "updated_at": "2026-01-01T00:02:00Z",
                "status": "warn",
                "configured_transport": "auto",
                "active_transport": "stdio",
                "diagnostics": {
                    "connect_attempts": 1,
                    "last_error": "HTTP 503 Service Unavailable",
                    "last_health_check_error": "401 Unauthorized token=[REDACTED]",
                    "healthcheck_tool": "index_status",
                    "recent_events": [],
                },
            }
        ),
        encoding="utf-8",
    )

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )
    monkeypatch.setenv(
        maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV, str(snapshot_path)
    )

    summary = await maintenance_api.get_observability_summary()

    transport = summary["transport"]
    breakdown = transport["diagnostics"]["exception_breakdown"]
    assert breakdown["total"] == 3
    assert breakdown["source_counts"] == {
        "recent_events": 1,
        "last_error": 1,
        "last_health_check_error": 1,
    }
    assert breakdown["incident_breakdown"]["canonical_cause_counts"] == {
        "healthcheck_auth_failure": 1,
        "transport_upstream_unavailable": 1,
        "healthcheck | sse | retrying": 1,
    }
    assert any(
        item["canonical_cause"] == "transport_upstream_unavailable"
        and item["transport"] == "stdio"
        and item["sources"] == ["last_error"]
        for item in breakdown["incident_breakdown"]["items"]
    )
    assert any(
        item["canonical_cause"] == "healthcheck_auth_failure"
        and item["tool"] == "index_status"
        and item["transport"] == "stdio"
        and item["sources"] == ["last_health_check_error"]
        for item in breakdown["incident_breakdown"]["items"]
    )


@pytest.mark.asyncio
async def test_observability_summary_keeps_warn_transport_incidents_grouped_by_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_client = _FakeIntentClient()
    snapshot_path = tmp_path / "transport-diagnostics.json"
    instance_dir = tmp_path / "transport-diagnostics.instances"
    instance_dir.mkdir()
    (instance_dir / "pid-warn.json").write_text(
        json.dumps(
            {
                "instance_id": "pid-warn",
                "updated_at": "2026-01-01T00:05:00Z",
                "status": "warn",
                "configured_transport": "auto",
                "active_transport": "sse",
                "diagnostics": {
                    "connect_attempts": 3,
                    "recent_events": [
                        {
                            "at": "2026-01-01T00:03:00Z",
                            "category": "connect",
                            "status": "warn",
                            "transport": "sse",
                            "message": "connected after fallback",
                        },
                        {
                            "at": "2026-01-01T00:04:00Z",
                            "category": "connect",
                            "status": "warn",
                            "transport": "sse",
                            "message": "connect timeout after 1500ms while opening SSE stream",
                        },
                        {
                            "at": "2026-01-01T00:05:00Z",
                            "category": "healthcheck",
                            "status": "warn",
                            "transport": "sse",
                            "tool": "index_status",
                            "message": "HTTP 429 Too Many Requests",
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )
    monkeypatch.setenv(
        maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV, str(snapshot_path)
    )

    summary = await maintenance_api.get_observability_summary()

    assert summary["status"] == "degraded"
    transport = summary["transport"]
    assert transport["status"] == "warn"
    breakdown = transport["diagnostics"]["exception_breakdown"]
    assert breakdown["status_counts"] == {"warn": 3}
    assert breakdown["incident_breakdown"]["canonical_cause_counts"] == {
        "transport_connect_fallback": 1,
        "transport_timeout": 1,
        "transport_rate_limited": 1,
    }
    cause_families = {
        item["canonical_cause"]: item["cause_family"]
        for item in breakdown["incident_breakdown"]["items"]
    }
    assert cause_families == {
        "transport_connect_fallback": "latency",
        "transport_timeout": "latency",
        "transport_rate_limited": "upstream",
    }


@pytest.mark.asyncio
async def test_observability_summary_keeps_fail_transport_incidents_grouped_by_family(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_client = _FakeIntentClient()
    snapshot_path = tmp_path / "transport-diagnostics.json"
    instance_dir = tmp_path / "transport-diagnostics.instances"
    instance_dir.mkdir()
    (instance_dir / "pid-fail.json").write_text(
        json.dumps(
            {
                "instance_id": "pid-fail",
                "updated_at": "2026-01-01T00:06:00Z",
                "status": "fail",
                "configured_transport": "stdio",
                "active_transport": "stdio",
                "diagnostics": {
                    "connect_attempts": 8,
                    "last_error": "sqlite3.OperationalError: database is locked",
                    "recent_events": [
                        {
                            "at": "2026-01-01T00:00:00Z",
                            "category": "connect",
                            "status": "fail",
                            "transport": "stdio",
                            "message": "dial tcp 127.0.0.1:8123: connect: connection refused",
                        },
                        {
                            "at": "2026-01-01T00:01:00Z",
                            "category": "connect",
                            "status": "fail",
                            "transport": "stdio",
                            "message": "dial tcp 10.10.0.8:443: no route to host",
                        },
                        {
                            "at": "2026-01-01T00:02:00Z",
                            "category": "connect",
                            "status": "fail",
                            "transport": "stdio",
                            "message": "getaddrinfo ENOTFOUND memory-palace.local",
                        },
                        {
                            "at": "2026-01-01T00:03:00Z",
                            "category": "transport",
                            "status": "fail",
                            "transport": "stdio",
                            "message": "socket hang up while streaming tool results",
                        },
                        {
                            "at": "2026-01-01T00:04:00Z",
                            "category": "transport",
                            "status": "fail",
                            "transport": "stdio",
                            "message": "SSL: CERTIFICATE_VERIFY_FAILED during TLS handshake",
                        },
                        {
                            "at": "2026-01-01T00:05:00Z",
                            "category": "transport",
                            "status": "fail",
                            "transport": "stdio",
                            "message": "HTTP 413 Payload Too Large",
                        },
                        {
                            "at": "2026-01-01T00:06:00Z",
                            "category": "transport",
                            "status": "fail",
                            "transport": "stdio",
                            "message": "protocol error: unexpected content-type text/html; invalid json",
                        },
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )
    monkeypatch.setenv(
        maintenance_api._TRANSPORT_DIAGNOSTICS_PATH_ENV, str(snapshot_path)
    )

    summary = await maintenance_api.get_observability_summary()

    assert summary["status"] == "degraded"
    transport = summary["transport"]
    assert transport["status"] == "fail"
    breakdown = transport["diagnostics"]["exception_breakdown"]
    assert breakdown["status_counts"] == {"fail": 8}
    assert breakdown["incident_breakdown"]["canonical_cause_counts"] == {
        "transport_connection_refused": 1,
        "transport_network_unreachable": 1,
        "transport_dns_failure": 1,
        "transport_connection_reset": 1,
        "transport_tls_failure": 1,
        "transport_payload_too_large": 1,
        "transport_protocol_error": 1,
        "sqlite_database_locked": 1,
    }
    cause_families = {
        item["canonical_cause"]: item["cause_family"]
        for item in breakdown["incident_breakdown"]["items"]
    }
    assert cause_families == {
        "transport_connection_refused": "network",
        "transport_network_unreachable": "network",
        "transport_dns_failure": "network",
        "transport_connection_reset": "network",
        "transport_tls_failure": "tls",
        "transport_payload_too_large": "upstream",
        "transport_protocol_error": "upstream",
        "sqlite_database_locked": "storage",
    }


def test_load_single_transport_observability_returns_unavailable_without_any_snapshot(
    tmp_path: Path,
) -> None:
    transport = maintenance_api._load_single_transport_observability(
        tmp_path / "transport-diagnostics.json"
    )

    assert transport["available"] is False
    assert transport["degraded"] is False
    assert transport["status"] == "unavailable"
    assert transport["reason"] == "transport_trace_unavailable"
    assert transport["snapshot_count"] == 0
    assert transport["instances"] == []
    assert transport["diagnostics"]["exception_breakdown"]["total"] == 0
    assert transport["diagnostics"]["exception_breakdown"]["signature_breakdown"]["total"] == 0
    assert (
        transport["diagnostics"]["exception_breakdown"]["signature_breakdown"]["signature_counts"]
        == {}
    )
    assert (
        transport["diagnostics"]["exception_breakdown"]["incident_breakdown"]["incident_count"]
        == 0
    )


@pytest.mark.parametrize(
    ("category", "tool", "check_id", "transport", "message", "expected"),
    [
        (
            "healthcheck",
            "index_status",
            "",
            "sse",
            "Health check failed with 401 Unauthorized: API key missing or invalid",
            "healthcheck_auth_failure",
        ),
        (
            "connect",
            "",
            "",
            "sse",
            "401 Unauthorized while opening SSE stream",
            "healthcheck_auth_failure",
        ),
        (
            "connect",
            "",
            "",
            "sse",
            "connected after fallback",
            "transport_connect_fallback",
        ),
        (
            "connect",
            "",
            "",
            "sse",
            "connect timeout after 5000ms while opening SSE stream",
            "transport_timeout",
        ),
        (
            "connect",
            "",
            "",
            "stdio",
            "dial tcp 127.0.0.1:8123: connect: connection refused",
            "transport_connection_refused",
        ),
        (
            "connect",
            "",
            "",
            "sse",
            "dial tcp 10.10.0.8:443: no route to host",
            "transport_network_unreachable",
        ),
        (
            "transport",
            "",
            "",
            "sse",
            "socket hang up while streaming tool results",
            "transport_connection_reset",
        ),
        (
            "connect",
            "",
            "",
            "sse",
            "getaddrinfo ENOTFOUND memory-palace.local",
            "transport_dns_failure",
        ),
        (
            "transport",
            "",
            "",
            "sse",
            "SSL: CERTIFICATE_VERIFY_FAILED during TLS handshake",
            "transport_tls_failure",
        ),
        (
            "transport",
            "",
            "",
            "stdio",
            "sqlite3.OperationalError: database is locked",
            "sqlite_database_locked",
        ),
        (
            "snapshot_load",
            "",
            "",
            "",
            "pid-bad.json: Expecting property name enclosed in double quotes",
            "transport_snapshot_load_failed",
        ),
        (
            "healthcheck",
            "index_status",
            "",
            "sse",
            "HTTP 429 Too Many Requests",
            "transport_rate_limited",
        ),
        (
            "report_check",
            "",
            "payload",
            "",
            "413 Payload Too Large",
            "transport_payload_too_large",
        ),
        (
            "report_check",
            "",
            "protocol",
            "",
            "protocol error: unexpected content-type text/html; invalid json",
            "transport_protocol_error",
        ),
        (
            "transport",
            "",
            "",
            "sse",
            "HTTP 413 Payload Too Large",
            "transport_payload_too_large",
        ),
        (
            "healthcheck",
            "index_status",
            "",
            "sse",
            "HTTP 503 Service Unavailable",
            "transport_upstream_unavailable",
        ),
        (
            "transport",
            "",
            "",
            "sse",
            "protocol error: unexpected content-type text/html; invalid json",
            "transport_protocol_error",
        ),
        (
            "healthcheck",
            "index_status",
            "",
            "sse",
            "retrying",
            "fallback",
        ),
    ],
)
def test_canonicalize_transport_exception_cause_maps_common_failure_families(
    category: str,
    tool: str,
    check_id: str,
    transport: str,
    message: str,
    expected: str,
) -> None:
    assert (
        maintenance_api._canonicalize_transport_exception_cause(
            category=category,
            tool=tool,
            check_id=check_id,
            transport=transport,
            message=message,
            fallback_signature="fallback",
        )
        == expected
    )


@pytest.mark.parametrize(
    ("canonical_cause", "expected_family"),
    [
        ("healthcheck_auth_failure", "auth"),
        ("transport_connect_fallback", "latency"),
        ("transport_timeout", "latency"),
        ("transport_connection_refused", "network"),
        ("transport_network_unreachable", "network"),
        ("transport_connection_reset", "network"),
        ("transport_dns_failure", "network"),
        ("transport_tls_failure", "tls"),
        ("transport_rate_limited", "upstream"),
        ("transport_payload_too_large", "upstream"),
        ("transport_upstream_unavailable", "upstream"),
        ("transport_protocol_error", "upstream"),
        ("sqlite_database_locked", "storage"),
        ("transport_snapshot_load_failed", "observability"),
        ("report_check | transport-health | Transport health check failed.", "healthcheck"),
        ("fallback", "other"),
    ],
)
def test_transport_incident_cause_family_maps_known_codes(
    canonical_cause: str, expected_family: str
) -> None:
    assert (
        maintenance_api._transport_incident_cause_family(canonical_cause)
        == expected_family
    )


def test_resolve_transport_report_check_signal_prefers_correlated_healthcheck_error() -> None:
    snapshot = {
        "active_transport": "sse",
        "diagnostics": {
            "healthcheck_tool": "index_status",
            "last_health_check_error": "HTTP 503 Service Unavailable",
            "last_error": "dial tcp 127.0.0.1:8123: connect: connection refused",
        },
    }
    check = {
        "id": "transport-health",
        "status": "fail",
        "message": "Transport health check failed.",
    }

    assert maintenance_api._resolve_transport_report_check_signal(snapshot, check) == {
        "category": "healthcheck",
        "tool": "index_status",
        "transport": "sse",
        "message": "HTTP 503 Service Unavailable",
    }


@pytest.mark.asyncio
async def test_observability_search_events_are_persisted_across_memory_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _FakeIntentClient()

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()
    maintenance_api._search_events_loaded = False

    payload = maintenance_api.SearchConsoleRequest(
        query="When did we rebuild index?",
        mode="hybrid",
        include_session=False,
    )
    result = await maintenance_api.run_observability_search(payload)

    assert result["ok"] is True
    assert fake_client.meta_store.get("observability.search_events.v1")

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()
    maintenance_api._search_events_loaded = False

    summary = await maintenance_api.get_observability_summary()
    assert summary["search_stats"]["total_queries"] == 1
    assert summary["search_stats"]["intent_breakdown"]["temporal"] == 1
    recent_event = summary["search_stats"]["search_trace"]["recent_events"][0]
    assert recent_event["search_trace"]["candidate_counts"]["returned"] == 8
    assert recent_event["search_trace"]["vector_engine"]["name"] == "faiss_hnsw"


@pytest.mark.asyncio
async def test_observability_persistence_avoids_concurrent_snapshot_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = _RacePersistIntentClient(delays=[0.05, 0.0])

    async def _ensure_started(_factory) -> None:
        return None

    async def _index_worker_status() -> Dict[str, Any]:
        return {"enabled": True, "running": False, "recent_jobs": [], "stats": {}}

    async def _write_lane_status() -> Dict[str, Any]:
        return {
            "global_concurrency": 1,
            "global_active": 0,
            "global_waiting": 0,
            "session_waiting_count": 0,
            "session_waiting_sessions": 0,
            "max_session_waiting": 0,
            "wait_warn_ms": 2000,
        }

    monkeypatch.setattr(maintenance_api, "get_sqlite_client", lambda: fake_client)
    monkeypatch.setattr(maintenance_api.runtime_state, "ensure_started", _ensure_started)
    monkeypatch.setattr(
        maintenance_api.runtime_state.index_worker, "status", _index_worker_status
    )
    monkeypatch.setattr(
        maintenance_api.runtime_state.write_lanes, "status", _write_lane_status
    )

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()
    maintenance_api._search_events_loaded = False

    temporal_payload = maintenance_api.SearchConsoleRequest(
        query="When did we rebuild index?",
        mode="hybrid",
        include_session=False,
    )
    causal_payload = maintenance_api.SearchConsoleRequest(
        query="Why did rebuild fail?",
        mode="hybrid",
        include_session=False,
    )

    await asyncio.gather(
        maintenance_api.run_observability_search(temporal_payload),
        maintenance_api.run_observability_search(causal_payload),
    )

    async with maintenance_api._search_events_guard:
        maintenance_api._search_events.clear()
    maintenance_api._search_events_loaded = False

    summary = await maintenance_api.get_observability_summary()
    assert summary["search_stats"]["total_queries"] == 2
    assert summary["search_stats"]["intent_breakdown"]["temporal"] == 1
    assert summary["search_stats"]["intent_breakdown"]["causal"] == 1
