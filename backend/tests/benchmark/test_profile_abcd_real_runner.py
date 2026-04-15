import json
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

BENCHMARK_DIR = Path(__file__).resolve().parent
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import mcp_server  # noqa: E402
from db.sqlite_client import Memory, Path as MemoryPath, SQLiteClient  # noqa: E402
from helpers import profile_abcd_real_runner as real_runner  # noqa: E402
from helpers.profile_abcd_real_runner import (  # noqa: E402
    DatasetBundle,
    QueryCase,
    REAL_PROFILE_DEFAULT_ENTRYPOINT,
    REAL_PROFILE_ENTRYPOINT_MCP_SEARCH_MEMORY,
    REAL_PROFILE_WORKDIR,
    _build_query_cases,
    _collapse_ranked_doc_ids,
    _evaluate_dataset,
    _normalize_profile_keys,
    _run_query_via_mcp_search_memory,
    _resolve_execution_profile_keys,
    _reset_domain_retrieval_state,
    build_phase6_gate,
    compute_percentile,
    compute_retrieval_metrics,
    render_abcd_sota_analysis_markdown,
    render_factual_pool_cap_compare_markdown,
    render_profile_abcd_real_markdown,
    render_profile_cd_real_markdown,
    resolve_real_profile_workdir,
)


def test_compute_retrieval_metrics_binary_relevance_contract() -> None:
    metrics = compute_retrieval_metrics(
        retrieved_doc_ids=["d2", "d1", "d3", "d4"],
        relevant_doc_ids={"d1", "d4"},
        k=10,
    )
    assert metrics["hr_at_5"] == pytest.approx(1.0)
    assert metrics["hr_at_10"] == pytest.approx(1.0)
    assert metrics["mrr"] == pytest.approx(0.5)
    assert metrics["recall_at_10"] == pytest.approx(1.0)
    assert metrics["ndcg_at_10"] == pytest.approx(0.6509209, abs=1e-6)


def test_compute_percentile_linear_interpolation() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert compute_percentile(values, 0.50) == pytest.approx(2.5)
    assert compute_percentile(values, 0.95) == pytest.approx(3.85)
    assert compute_percentile([9.0], 0.95) == pytest.approx(9.0)


def test_build_phase6_gate_marks_invalid_when_profile_d_has_invalid_reasons() -> None:
    gate = build_phase6_gate(
        [
            {
                "dataset": "squad_v2_dev",
                "dataset_label": "SQuAD v2 Dev",
                "degradation": {"invalid_reasons": []},
            },
            {
                "dataset": "beir_nfcorpus",
                "dataset_label": "BEIR NFCorpus",
                "degradation": {"invalid_reasons": ["embedding_request_failed"]},
            },
        ]
    )
    assert gate["valid"] is False
    assert gate["invalid_reasons"] == ["embedding_request_failed"]
    assert gate["rows"][0]["valid"] is True
    assert gate["rows"][1]["valid"] is False


def test_build_phase6_gate_api_tolerant_allows_small_invalid_rate() -> None:
    gate = build_phase6_gate(
        [
            {
                "dataset": "beir_nfcorpus",
                "dataset_label": "BEIR NFCorpus",
                "degradation": {
                    "queries": 500,
                    "invalid_reasons": ["reranker_request_failed"],
                    "invalid_count": 2,
                    "invalid_rate": 0.004,
                    "request_failed_count": 2,
                    "request_failed_rate": 0.004,
                    "invalid_reason_counts": {"reranker_request_failed": 2},
                    "request_failed_reason_counts": {"reranker_request_failed": 2},
                },
            }
        ],
        mode="api_tolerant",
        invalid_rate_threshold=0.05,
    )
    assert gate["mode"] == "api_tolerant"
    assert gate["valid"] is True
    assert gate["invalid_count"] == 2
    assert gate["request_failed_count"] == 2
    assert gate["request_failed_reason_counts"] == {"reranker_request_failed": 2}
    assert gate["rows"][0]["valid"] is True


def test_build_phase6_gate_api_tolerant_marks_invalid_when_rate_exceeds_threshold() -> None:
    gate = build_phase6_gate(
        [
            {
                "dataset": "beir_nfcorpus",
                "dataset_label": "BEIR NFCorpus",
                "degradation": {
                    "queries": 500,
                    "invalid_reasons": ["reranker_request_failed"],
                    "invalid_count": 30,
                    "invalid_rate": 0.06,
                    "request_failed_count": 30,
                    "request_failed_rate": 0.06,
                },
            }
        ],
        mode="api_tolerant",
        invalid_rate_threshold=0.05,
    )
    assert gate["valid"] is False
    assert gate["rows"][0]["valid"] is False


def test_resolve_real_profile_workdir_respects_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected = tmp_path / "custom-real-cache"
    monkeypatch.setenv("BENCHMARK_REAL_PROFILE_WORKDIR", str(expected))
    assert resolve_real_profile_workdir() == expected


def test_resolve_real_profile_workdir_prefers_explicit_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(
        "BENCHMARK_REAL_PROFILE_WORKDIR",
        str(tmp_path / "env-cache"),
    )
    explicit = tmp_path / "explicit-cache"
    assert resolve_real_profile_workdir(explicit) == explicit


def test_resolve_real_profile_workdir_allocates_unique_run_dir_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BENCHMARK_REAL_PROFILE_WORKDIR", raising=False)

    first = resolve_real_profile_workdir()
    second = resolve_real_profile_workdir()

    assert first != second
    assert first.parent == REAL_PROFILE_WORKDIR
    assert second.parent == REAL_PROFILE_WORKDIR
    assert first.name.startswith("run-")
    assert second.name.startswith("run-")


class _ProbeSearchClient:
    def __init__(self, result_rows):
        self._result_rows = list(result_rows)
        self.calls = []

    async def search_advanced(
        self,
        *,
        query: str,
        mode: str,
        max_results: int,
        candidate_multiplier: int,
        filters,
    ):
        self.calls.append(
            {
                "query": query,
                "mode": mode,
                "max_results": max_results,
                "candidate_multiplier": candidate_multiplier,
                "filters": dict(filters),
            }
        )
        return {
            "results": list(self._result_rows),
            "degraded": False,
            "degrade_reasons": [],
        }


@pytest.mark.asyncio
async def test_evaluate_dataset_passes_depth_params_and_keeps_top10_metrics() -> None:
    result_rows = [{"memory_id": memory_id} for memory_id in range(1, 13)]
    client = _ProbeSearchClient(result_rows)
    bundle = DatasetBundle(
        key="squad_v2_dev",
        label="SQuAD v2 Dev",
        domain="bench_squad_v2_dev",
        queries=[QueryCase(query_id="q-1", query="alpha", relevant_doc_ids={"doc_12"})],
        docs=[("doc_1", "doc one")],
        sample_bucket_size=100,
        query_count_raw=1,
        avg_relevant_per_query=1.0,
        max_relevant_per_query=1,
        multi_relevant_query_rate=0.0,
    )
    memory_to_doc = {memory_id: f"doc_{memory_id}" for memory_id in range(1, 13)}

    row = await _evaluate_dataset(
        client=client,  # type: ignore[arg-type]
        bundle=bundle,
        profile_mode="hybrid",
        memory_to_doc=memory_to_doc,
        max_results=12,
        candidate_multiplier=9,
    )

    assert client.calls == [
        {
            "query": "alpha",
            "mode": "hybrid",
            "max_results": 12,
            "candidate_multiplier": 9,
            "filters": {"domain": "bench_squad_v2_dev", "max_priority": 10},
        }
    ]
    # Relevant doc is rank 12 in returned list; Top10 metrics must remain 0.
    assert row["quality"]["hr_at_10"] == pytest.approx(0.0)
    assert row["quality"]["mrr"] == pytest.approx(0.0)
    assert row["quality"]["ndcg_at_10"] == pytest.approx(0.0)
    assert row["quality"]["recall_at_10"] == pytest.approx(0.0)
    assert row["retrieval_depth"] == {
        "max_results": 12,
        "candidate_multiplier": 9,
        "metric_top_k": 10,
        "retrieval_state_reset_per_query": False,
        "result_collapse": "doc_id_dedup",
    }
    assert row["dataset_relevance"] == {
        "avg_relevant_per_query": 1.0,
        "max_relevant_per_query": 1,
        "multi_relevant_query_rate": 0.0,
    }
    assert row["entrypoint"] == "sqlite_client"
    assert row["source_variant"] == "sqlite_client.search_advanced"
    assert row["entrypoint_stats"]["backend_method_counts"] == {
        "sqlite_client.search_advanced": 1
    }
    assert row["entrypoint_stats"]["candidate_multiplier_applied_avg"] is None
    assert row["entrypoint_stats"]["candidate_pool_size_avg"] is None


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


@pytest.mark.asyncio
async def test_reset_domain_retrieval_state_clears_access_and_vitality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VALID_DOMAINS", "core,writer,game,notes,system,bench_nfcorpus")
    client = SQLiteClient(_sqlite_url(tmp_path / "profile-reset-state.db"))
    await client.init_db()
    created = await client.create_memory(
        parent_path="",
        content="benchmark doc",
        priority=1,
        title="doc_a",
        domain="bench_nfcorpus",
    )
    memory_id = int(created["id"])

    async with client.session() as session:
        result = await session.execute(select(Memory).where(Memory.id == memory_id))
        memory = result.scalar_one()
        memory.access_count = 9
        memory.vitality_score = 2.7
        memory.last_accessed_at = memory.created_at
        session.add(memory)
        await session.commit()

    changed = await _reset_domain_retrieval_state(client, "bench_nfcorpus")

    async with client.session() as session:
        result = await session.execute(
            select(Memory, MemoryPath)
            .join(MemoryPath, Memory.id == MemoryPath.memory_id)
            .where(MemoryPath.domain == "bench_nfcorpus")
            .where(MemoryPath.path == "doc_a")
        )
        memory, _path = result.one()
    await client.close()

    assert changed is True
    assert int(memory.access_count or 0) == 0
    assert float(memory.vitality_score or 0.0) == pytest.approx(1.0)
    assert memory.last_accessed_at is None


@pytest.mark.asyncio
async def test_reset_domain_retrieval_state_path_prefix_is_tree_scoped(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "profile-reset-prefix.db"))
    await client.init_db()
    bundle_a = DatasetBundle(
        key="beir_nfcorpus",
        label="BEIR NFCorpus",
        domain="notes",
        queries=[],
        docs=[("doc_1", "alpha")],
        sample_bucket_size=100,
        query_count_raw=0,
        avg_relevant_per_query=0.0,
        max_relevant_per_query=0,
        multi_relevant_query_rate=0.0,
        path_prefix="benchmark/beir_nfcorpus",
    )
    bundle_b = DatasetBundle(
        key="beir_nfcorpus_extra",
        label="BEIR NFCorpus Extra",
        domain="notes",
        queries=[],
        docs=[("doc_2", "beta")],
        sample_bucket_size=100,
        query_count_raw=0,
        avg_relevant_per_query=0.0,
        max_relevant_per_query=0,
        multi_relevant_query_rate=0.0,
        path_prefix="benchmark/beir_nfcorpus_extra",
    )
    await real_runner._populate_bundle_docs(client, bundle_a)
    await real_runner._populate_bundle_docs(client, bundle_b)

    async with client.session() as session:
        result = await session.execute(
            select(Memory, MemoryPath)
            .join(MemoryPath, Memory.id == MemoryPath.memory_id)
            .where(MemoryPath.domain == "notes")
            .where(
                MemoryPath.path.in_(
                    [
                        "benchmark/beir_nfcorpus/beir_nfcorpus_00000",
                        "benchmark/beir_nfcorpus_extra/beir_nfcorpus_extra_00000",
                    ]
                )
            )
        )
        for memory, path_obj in result.all():
            memory.access_count = 9
            memory.vitality_score = 2.7
            memory.last_accessed_at = memory.created_at
            session.add(memory)

    changed = await _reset_domain_retrieval_state(
        client, "notes", "benchmark/beir_nfcorpus"
    )
    assert changed is True

    async with client.session() as session:
        result = await session.execute(
            select(Memory, MemoryPath)
            .join(MemoryPath, Memory.id == MemoryPath.memory_id)
            .where(MemoryPath.domain == "notes")
            .where(
                MemoryPath.path.in_(
                    [
                        "benchmark/beir_nfcorpus/beir_nfcorpus_00000",
                        "benchmark/beir_nfcorpus_extra/beir_nfcorpus_extra_00000",
                    ]
                )
            )
        )
        rows = {path_obj.path: memory for memory, path_obj in result.all()}
    await client.close()

    target = rows["benchmark/beir_nfcorpus/beir_nfcorpus_00000"]
    sibling = rows["benchmark/beir_nfcorpus_extra/beir_nfcorpus_extra_00000"]
    assert int(target.access_count or 0) == 0
    assert float(target.vitality_score or 0.0) == pytest.approx(1.0)
    assert sibling.access_count == 9
    assert float(sibling.vitality_score or 0.0) == pytest.approx(2.7)


@pytest.mark.asyncio
async def test_evaluate_dataset_resets_retrieval_state_before_each_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_rows = [{"memory_id": memory_id} for memory_id in range(1, 4)]
    client = _ProbeSearchClient(result_rows)
    bundle = DatasetBundle(
        key="beir_nfcorpus",
        label="BEIR NFCorpus",
        domain="bench_beir_nfcorpus",
        queries=[
            QueryCase(query_id="q-1", query="alpha", relevant_doc_ids={"doc_1"}),
            QueryCase(query_id="q-2", query="beta", relevant_doc_ids={"doc_2"}),
        ],
        docs=[("doc_1", "doc one"), ("doc_2", "doc two")],
        sample_bucket_size=100,
        query_count_raw=2,
        avg_relevant_per_query=1.0,
        max_relevant_per_query=1,
        multi_relevant_query_rate=0.0,
    )
    memory_to_doc = {memory_id: f"doc_{memory_id}" for memory_id in range(1, 4)}
    resets: list[str] = []

    async def _fake_reset(_client, domain: str, path_prefix: str | None = None) -> bool:
        _ = path_prefix
        resets.append(domain)
        return True

    monkeypatch.setattr(real_runner, "_reset_domain_retrieval_state", _fake_reset)
    row = await _evaluate_dataset(
        client=client,  # type: ignore[arg-type]
        bundle=bundle,
        profile_mode="hybrid",
        memory_to_doc=memory_to_doc,
        max_results=10,
        candidate_multiplier=4,
    )

    assert resets == ["bench_beir_nfcorpus", "bench_beir_nfcorpus"]
    assert row["retrieval_depth"]["retrieval_state_reset_per_query"] is True


def test_build_query_cases_keeps_all_relevant_docs_when_requested() -> None:
    rows = [
        {
            "id": "q-1",
            "query": "omega fats",
            "relevant_uris_or_doc_ids": ["doc-1", "doc-2", "doc-missing"],
        }
    ]
    corpus = {"doc-1": "alpha", "doc-2": "beta", "doc-3": "gamma"}

    first_only = _build_query_cases(
        rows=rows,
        corpus=corpus,
        first_relevant_only=True,
    )
    all_relevant = _build_query_cases(
        rows=rows,
        corpus=corpus,
        first_relevant_only=False,
    )

    assert first_only[0].relevant_doc_ids == {"doc-1"}
    assert all_relevant[0].relevant_doc_ids == {"doc-1", "doc-2"}


def test_collapse_ranked_doc_ids_preserves_first_hit_order() -> None:
    assert _collapse_ranked_doc_ids(
        ["doc-2", "doc-2", "doc-1", "doc-3", "doc-1", "", "doc-3"]
    ) == ["doc-2", "doc-1", "doc-3"]


@pytest.mark.asyncio
async def test_evaluate_dataset_collapses_duplicate_doc_hits_before_metrics() -> None:
    result_rows = [{"memory_id": 1}, {"memory_id": 2}, {"memory_id": 3}]
    client = _ProbeSearchClient(result_rows)
    bundle = DatasetBundle(
        key="beir_nfcorpus",
        label="BEIR NFCorpus",
        domain="bench_beir_nfcorpus",
        queries=[QueryCase(query_id="q-1", query="alpha", relevant_doc_ids={"doc-2"})],
        docs=[("doc-1", "doc one"), ("doc-2", "doc two")],
        sample_bucket_size=100,
        query_count_raw=1,
        avg_relevant_per_query=1.0,
        max_relevant_per_query=1,
        multi_relevant_query_rate=0.0,
    )
    memory_to_doc = {1: "doc-1", 2: "doc-1", 3: "doc-2"}

    row = await _evaluate_dataset(
        client=client,  # type: ignore[arg-type]
        bundle=bundle,
        profile_mode="hybrid",
        memory_to_doc=memory_to_doc,
        max_results=10,
        candidate_multiplier=4,
    )

    assert row["quality"]["mrr"] == pytest.approx(0.5)
    assert row["quality"]["hr_at_10"] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_run_query_via_mcp_search_memory_disables_session_merge_and_keeps_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import mcp_server

    client = SQLiteClient(_sqlite_url(tmp_path / "profile-mcp-runner.db"))
    bundle = DatasetBundle(
        key="beir_nfcorpus",
        label="BEIR NFCorpus",
        domain="bench_beir_nfcorpus",
        queries=[QueryCase(query_id="q-1", query="omega", relevant_doc_ids={"doc_1"})],
        docs=[("doc_1", "doc one")],
        sample_bucket_size=100,
        query_count_raw=1,
        avg_relevant_per_query=1.0,
        max_relevant_per_query=1,
        multi_relevant_query_rate=0.0,
    )
    case = bundle.queries[0]
    captured: dict[str, object] = {}

    async def _fake_search_memory(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "ok": True,
                "backend_method": "mcp_server.search_memory",
                "candidate_multiplier_applied": 6,
                "candidate_pool_size": 24,
                "intent_applied": "factual",
                "strategy_template_applied": "factual_high_precision",
                "session_first_enabled": False,
                "session_first_metrics": {"session_contributed": 0},
                "results": [{"memory_id": 1}],
                "degraded": False,
                "degrade_reasons": [],
            }
        )

    monkeypatch.setattr(mcp_server, "search_memory", _fake_search_memory)
    payload = await _run_query_via_mcp_search_memory(
        client=client,
        case=case,
        bundle=bundle,
        profile_mode="hybrid",
        max_results=12,
        candidate_multiplier=9,
    )
    await client.close()

    assert captured == {
        "query": "omega",
        "mode": "hybrid",
        "max_results": 12,
        "candidate_multiplier": 9,
        "include_session": False,
        "filters": {"domain": "bench_beir_nfcorpus", "max_priority": 10},
    }
    assert payload["backend_method"] == "mcp_server.search_memory"
    assert payload["candidate_multiplier_applied"] == 6
    assert payload["candidate_pool_size"] == 24


@pytest.mark.asyncio
async def test_evaluate_dataset_collects_mcp_entrypoint_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = DatasetBundle(
        key="beir_nfcorpus",
        label="BEIR NFCorpus",
        domain="bench_beir_nfcorpus",
        queries=[
            QueryCase(query_id="q-1", query="alpha", relevant_doc_ids={"doc_1", "doc_2"}),
            QueryCase(query_id="q-2", query="beta", relevant_doc_ids={"doc_2", "doc_3"}),
        ],
        docs=[("doc_1", "doc one"), ("doc_2", "doc two"), ("doc_3", "doc three")],
        sample_bucket_size=100,
        query_count_raw=2,
        avg_relevant_per_query=2.0,
        max_relevant_per_query=2,
        multi_relevant_query_rate=1.0,
    )
    memory_to_doc = {1: "doc_1", 2: "doc_2", 3: "doc_3"}
    resets: list[str] = []

    async def _fake_reset(_client, domain: str, path_prefix: str | None = None) -> bool:
        _ = path_prefix
        resets.append(domain)
        return False

    async def _fake_query_runner(**kwargs):
        case = kwargs["case"]
        if case.query == "alpha":
            return {
                "backend_method": "mcp_server.search_memory",
                "intent_applied": "factual",
                "strategy_template_applied": "factual_high_precision",
                "candidate_multiplier_applied": 6,
                "candidate_pool_size": 24,
                "session_first_enabled": False,
                "session_first_metrics": {"session_contributed": 0},
                "results": [{"memory_id": 1}, {"memory_id": 2}],
                "degraded": False,
                "degrade_reasons": [],
            }
        return {
            "backend_method": "mcp_server.search_memory",
            "intent_applied": "exploratory",
            "strategy_template_applied": "exploratory_high_recall",
            "candidate_multiplier_applied": 8,
            "candidate_pool_size": 32,
            "session_first_enabled": False,
            "session_first_metrics": {"session_contributed": 0},
            "results": [{"memory_id": 2}, {"memory_id": 3}],
            "degraded": False,
            "degrade_reasons": [],
        }

    monkeypatch.setattr(real_runner, "_reset_domain_retrieval_state", _fake_reset)
    row = await _evaluate_dataset(
        client=object(),  # type: ignore[arg-type]
        bundle=bundle,
        profile_mode="hybrid",
        memory_to_doc=memory_to_doc,
        max_results=10,
        candidate_multiplier=4,
        entrypoint=REAL_PROFILE_ENTRYPOINT_MCP_SEARCH_MEMORY,
        query_runner=_fake_query_runner,
    )

    assert resets == ["bench_beir_nfcorpus", "bench_beir_nfcorpus"]
    assert row["entrypoint"] == "mcp_search_memory"
    assert row["source_variant"] == "mcp_server.search_memory"
    assert row["dataset_relevance"] == {
        "avg_relevant_per_query": 2.0,
        "max_relevant_per_query": 2,
        "multi_relevant_query_rate": 1.0,
    }
    assert row["entrypoint_stats"] == {
        "backend_method_counts": {"mcp_server.search_memory": 2},
        "intent_applied_counts": {"exploratory": 1, "factual": 1},
        "strategy_template_applied_counts": {
            "exploratory_high_recall": 1,
            "factual_high_precision": 1,
        },
        "candidate_multiplier_applied_avg": pytest.approx(7.0),
        "candidate_pool_size_avg": pytest.approx(28.0),
        "session_first_enabled": False,
        "session_contributed_avg": pytest.approx(0.0),
    }


@pytest.mark.asyncio
async def test_run_query_via_mcp_search_memory_supports_path_prefix_scoped_benchmark_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    client = SQLiteClient(_sqlite_url(tmp_path / "benchmark-mcp-entrypoint.db"))
    await client.init_db()
    bundle = DatasetBundle(
        key="beir_nfcorpus",
        label="BEIR NFCorpus",
        domain="notes",
        queries=[
            QueryCase(
                query_id="q-1",
                query="omega fats",
                relevant_doc_ids={"doc-1"},
            )
        ],
        docs=[("doc-1", "omega fats and dietary supplement review")],
        sample_bucket_size=100,
        query_count_raw=1,
        avg_relevant_per_query=1.0,
        max_relevant_per_query=1,
        multi_relevant_query_rate=0.0,
        path_prefix="benchmark/beir_nfcorpus",
    )
    memory_to_doc = await real_runner._populate_bundle_docs(client, bundle)
    assert memory_to_doc

    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    payload = await _run_query_via_mcp_search_memory(
        client=client,
        case=bundle.queries[0],
        bundle=bundle,
        profile_mode="hybrid",
        max_results=10,
        candidate_multiplier=8,
    )
    await client.close()

    assert payload["ok"] is True
    assert payload["backend_method"] == "sqlite_client.search_advanced"
    assert payload["count"] >= 1
    assert payload["results"][0]["memory_id"] in memory_to_doc
    assert all(
        item["memory_id"] in memory_to_doc for item in payload.get("results", [])
    )
    assert payload["scope_effective"] == {
        "domain": "notes",
        "path_prefix": "benchmark/beir_nfcorpus",
    }
    assert payload["intent_applied"] == "factual"
    assert payload["candidate_multiplier_applied"] == 2


def test_render_profile_abcd_real_markdown_marks_all_relevant_mode() -> None:
    payload = {
        "generated_at_utc": "2026-03-09T12:00:00+00:00",
        "source_variant": "mcp_server.search_memory",
        "dataset_scope": ["beir_nfcorpus"],
        "sample_size_requested": 10,
        "real_run_strategy": {
            "entrypoint": "mcp_search_memory",
            "factual_pool_cap": 0,
            "first_relevant_only": False,
            "relevance_mode": "all_relevant",
            "extra_distractors": 200,
            "max_results": 10,
            "candidate_multiplier": 8,
            "metric_top_k": 10,
            "result_collapse": "doc_id_dedup",
        },
        "profiles": {
            key: {
                "profile": key,
                "mode": "hybrid",
                "rows": [
                    {
                        "dataset_label": "BEIR NFCorpus",
                        "query_count": 10,
                        "corpus_doc_count": 210,
                        "dataset_relevance": {
                            "avg_relevant_per_query": 2.4,
                            "max_relevant_per_query": 6,
                            "multi_relevant_query_rate": 0.7,
                        },
                        "entrypoint_stats": {
                            "backend_method_counts": {"mcp_server.search_memory": 10},
                            "intent_applied_counts": {"factual": 10},
                            "strategy_template_applied_counts": {
                                "factual_high_precision": 10
                            },
                            "candidate_multiplier_applied_avg": 2.0,
                            "candidate_pool_size_avg": 20.0,
                            "session_first_enabled": False,
                            "session_contributed_avg": 0.0,
                        },
                        "quality": {
                            "hr_at_5": 1.0,
                            "hr_at_10": 1.0,
                            "mrr": 0.9,
                            "ndcg_at_10": 0.6,
                            "recall_at_10": 0.45,
                        },
                        "latency_ms": {"p50": 10.0, "p95": 20.0, "p99": 30.0},
                        "degradation": {
                            "degrade_rate": 0.0,
                            "invalid_reasons": [],
                            "invalid_count": 0,
                            "invalid_rate": 0.0,
                            "request_failed_count": 0,
                            "request_failed_rate": 0.0,
                        },
                    }
                ],
            }
            for key in ("profile_a", "profile_b", "profile_c", "profile_d")
        },
        "phase6": {
            "gate": {
                "valid": True,
                "mode": "strict",
                "invalid_rate_threshold": 0.05,
                "invalid_reasons": [],
                "invalid_count": 0,
                "query_count": 10,
                "invalid_rate": 0.0,
                "request_failed_count": 0,
                "request_failed_rate": 0.0,
                "rows": [
                    {
                        "dataset_label": "BEIR NFCorpus",
                        "valid": True,
                        "invalid_reasons": [],
                        "invalid_count": 0,
                        "invalid_rate": 0.0,
                        "request_failed_count": 0,
                        "request_failed_rate": 0.0,
                    }
                ],
            },
            "comparison_rows": [
                {
                    "dataset_label": "BEIR NFCorpus",
                    "a_hr10": 1.0,
                    "b_hr10": 1.0,
                    "c_hr10": 1.0,
                    "d_hr10": 1.0,
                    "a_ndcg10": 0.6,
                    "b_ndcg10": 0.6,
                    "c_ndcg10": 0.6,
                    "d_ndcg10": 0.6,
                    "a_p95": 20.0,
                    "b_p95": 20.0,
                    "c_p95": 20.0,
                    "d_p95": 20.0,
                    "valid": True,
                }
            ],
        },
    }

    markdown = render_profile_abcd_real_markdown(payload)
    cd_markdown = render_profile_cd_real_markdown(payload)

    assert "relevance_mode: all_relevant" in markdown
    assert "HR@10 reads as any-hit" in markdown
    assert "entrypoint: mcp_search_memory" in markdown
    assert "factual_pool_cap: 0" in markdown
    assert "result_collapse: doc_id_dedup" in markdown
    assert "source_variant: mcp_server.search_memory" in markdown
    assert "2.40" in markdown
    assert "70.0%" in markdown
    assert "relevance_mode: all_relevant" in cd_markdown
    assert "prefer NDCG@10" in cd_markdown
    assert "entrypoint: mcp_search_memory" in cd_markdown
    assert "factual_pool_cap: 0" in cd_markdown
    assert "result_collapse: doc_id_dedup" in cd_markdown


def test_profile_selection_normalizes_aliases_and_expands_profile_d_dependency() -> None:
    assert _normalize_profile_keys(["d", "c", "profile_d"]) == [
        "profile_d",
        "profile_c",
    ]
    assert _resolve_execution_profile_keys(["d"]) == ["profile_c", "profile_d"]
    assert _resolve_execution_profile_keys(["b", "d"]) == [
        "profile_b",
        "profile_c",
        "profile_d",
    ]


def test_render_markdown_supports_profile_subset_without_ab_rows() -> None:
    payload = {
        "generated_at_utc": "2026-03-10T00:00:00+00:00",
        "source_variant": "sqlite_client.search_advanced",
        "dataset_scope": ["beir_nfcorpus"],
        "sample_size_requested": 4,
        "real_run_strategy": {
            "profiles": ["profile_c", "profile_d"],
            "entrypoint": REAL_PROFILE_DEFAULT_ENTRYPOINT,
            "factual_pool_cap": None,
            "first_relevant_only": False,
            "relevance_mode": "all_relevant",
            "extra_distractors": 10,
            "max_results": 10,
            "candidate_multiplier": 8,
            "metric_top_k": 10,
            "result_collapse": "doc_id_dedup",
        },
        "profiles": {
            key: {
                "profile": key,
                "mode": "hybrid",
                "rows": [
                    {
                        "dataset": "beir_nfcorpus",
                        "dataset_label": "BEIR NFCorpus",
                        "query_count": 4,
                        "corpus_doc_count": 14,
                        "dataset_relevance": {
                            "avg_relevant_per_query": 2.0,
                            "max_relevant_per_query": 3,
                            "multi_relevant_query_rate": 0.5,
                        },
                        "quality": {
                            "hr_at_5": 1.0,
                            "hr_at_10": 1.0,
                            "mrr": 0.75,
                            "ndcg_at_10": 0.6,
                            "recall_at_10": 0.5,
                        },
                        "latency_ms": {"p50": 12.0, "p95": 30.0, "p99": 40.0},
                        "degradation": {
                            "degrade_rate": 0.0,
                            "invalid_reasons": [],
                            "invalid_count": 0,
                            "invalid_rate": 0.0,
                            "request_failed_count": 0,
                            "request_failed_rate": 0.0,
                        },
                    }
                ],
            }
            for key in ("profile_c", "profile_d")
        },
        "phase6": {
            "gate": {
                "valid": True,
                "mode": "strict",
                "invalid_rate_threshold": 0.05,
                "invalid_reasons": [],
                "invalid_count": 0,
                "query_count": 4,
                "invalid_rate": 0.0,
                "request_failed_count": 0,
                "request_failed_rate": 0.0,
                "rows": [],
            },
            "comparison_rows": [],
        },
    }

    markdown = render_profile_abcd_real_markdown(payload)
    cd_markdown = render_profile_cd_real_markdown(payload)
    analysis_markdown = render_abcd_sota_analysis_markdown(payload)

    assert "- profiles: profile_c, profile_d" in markdown
    assert "## profile_c" in markdown
    assert "## profile_d" in markdown
    assert "## profile_a" not in markdown
    assert "## A/B/C/D Comparison" not in markdown
    assert "- profiles: profile_c, profile_d" in analysis_markdown
    assert "| profile_c |" in analysis_markdown
    assert "| profile_d |" in analysis_markdown
    assert "| profile_a |" not in analysis_markdown
    assert "## profile_c" in cd_markdown
    assert "## profile_d" in cd_markdown


def test_render_factual_pool_cap_compare_markdown_shows_deltas() -> None:
    baseline_payload = {
        "generated_at_utc": "2026-03-10T01:00:00+00:00",
        "dataset_scope": ["beir_nfcorpus"],
        "real_run_strategy": {
            "entrypoint": "mcp_search_memory",
            "profiles": ["profile_d"],
            "factual_pool_cap": 2,
        },
        "profiles": {
            "profile_d": {
                "rows": [
                    {
                        "dataset": "beir_nfcorpus",
                        "dataset_label": "BEIR NFCorpus",
                        "quality": {
                            "ndcg_at_10": 0.60,
                            "recall_at_10": 0.44,
                        },
                        "latency_ms": {"p95": 1600.0},
                    }
                ]
            }
        },
    }
    compare_payload = {
        "generated_at_utc": "2026-03-10T01:10:00+00:00",
        "real_run_strategy": {
            "entrypoint": "mcp_search_memory",
            "profiles": ["profile_d"],
            "factual_pool_cap": 0,
        },
        "profiles": {
            "profile_d": {
                "rows": [
                    {
                        "dataset": "beir_nfcorpus",
                        "dataset_label": "BEIR NFCorpus",
                        "quality": {
                            "ndcg_at_10": 0.66,
                            "recall_at_10": 0.51,
                        },
                        "latency_ms": {"p95": 1500.0},
                    }
                ]
            }
        },
    }

    markdown = render_factual_pool_cap_compare_markdown(
        baseline_payload,
        compare_payload,
    )

    assert "baseline_factual_pool_cap: 2" in markdown
    assert "compare_factual_pool_cap: 0" in markdown
    assert "## profile_d" in markdown
    assert "BEIR NFCorpus" in markdown
    assert "+0.060" in markdown
    assert "+0.070" in markdown
    assert "-100.0" in markdown
