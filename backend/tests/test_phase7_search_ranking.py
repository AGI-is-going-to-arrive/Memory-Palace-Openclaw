import hashlib
import re
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import func, select

from db.sqlite_client import (
    EmbeddingCache,
    Memory,
    Path as MemoryPath,
    SQLiteClient,
    _utc_now_naive,
)
from db.sqlite_client_retrieval import SQLiteClientRetrievalMixin


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


async def _get_memory_by_path(client: SQLiteClient, path: str) -> Memory:
    async with client.session() as session:
        result = await session.execute(
            select(Memory)
            .join(MemoryPath, Memory.id == MemoryPath.memory_id)
            .where(MemoryPath.domain == "core")
            .where(MemoryPath.path == path)
        )
        memory = result.scalar_one()
        return memory


def test_chunk_content_prefers_cjk_sentence_boundaries() -> None:
    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    client._chunk_size = 80
    client._chunk_overlap = 0
    content = (
        "这是第一句用于测试分块质量并且包含更多文字来验证中文句号边界是否生效。"
        "这里是第二句继续说明分块逻辑。"
    )

    chunks = client._chunk_content(content)

    assert [chunk_text for _, _, _, chunk_text in chunks] == [
        "这是第一句用于测试分块质量并且包含更多文字来验证中文句号边界是否生效。",
        "这里是第二句继续说明分块逻辑。",
    ]


def test_mmr_tokens_keep_cjk_chunks_and_bigrams() -> None:
    tokens = SQLiteClientRetrievalMixin._mmr_tokens(
        {
            "snippet": "部署 deployment guide",
            "metadata": {"path": "core/部署/guide"},
        }
    )

    assert "deployment" in tokens
    assert "guide" in tokens
    assert "部署" in tokens
    assert "部" not in tokens


def test_redundancy_ratio_detects_duplicate_cjk_results() -> None:
    ratio = SQLiteClientRetrievalMixin._redundancy_ratio(
        [
            {
                "snippet": "部署 deployment guide",
                "metadata": {"path": "core/部署/guide"},
            },
            {
                "snippet": "部署 指南 deployment handbook",
                "metadata": {"path": "core/部署/handbook"},
            },
        ]
    )

    assert ratio > 0.0


def test_hash_embedding_keeps_cjk_tokens_in_mixed_text() -> None:
    client = SQLiteClient("sqlite+aiosqlite:///:memory:")

    mixed = client._hash_embedding("部署 deployment guide", dim=64)
    latin_only = client._hash_embedding("deployment guide", dim=64)
    pure_cjk = client._hash_embedding("部署 指南", dim=64)

    assert mixed != latin_only
    assert any(value != 0.0 for value in pure_cjk)


def test_chunk_content_keeps_code_fences_in_one_chunk_when_possible() -> None:
    client = SQLiteClient("sqlite+aiosqlite:///:memory:")
    client._chunk_size = 32
    client._chunk_overlap = 0
    content = "前言说明\n```python\nprint('alpha')\nprint('beta')\n```\n结尾补充"

    chunks = client._chunk_content(content)

    assert len(chunks) == 2
    assert chunks[0][3].count("```") == 2
    assert "print('beta')" in chunks[0][3]
    assert chunks[1][3].lstrip() == "结尾补充"


@pytest.mark.asyncio
async def test_search_advanced_uses_gist_recall_and_stage_provenance(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-gist.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="silent board notes without the target term",
        priority=1,
        title="board-note",
        domain="core",
    )
    memory = await _get_memory_by_path(client, "board-note")
    await client.upsert_memory_gist(
        memory_id=memory.id,
        gist_text="whiteboard launch checklist and action items",
        source_hash="phase7-gist-source",
        gist_method="extractive_bullets",
        quality_score=0.91,
    )

    payload = await client.search_advanced(
        query="whiteboard",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    assert payload["results"]
    result = payload["results"][0]
    assert result["uri"] == "core://board-note"
    assert "gist" in result["metadata"]["search_provenance"]["stages"]
    assert payload["metadata"]["candidate_counts"]["gist_rows"] >= 1
    assert result["score"] == result["scores"]["final"]
    assert "vitality" in result["scores"]
    assert "access" in result["scores"]
    assert "length_norm" in result["scores"]


@pytest.mark.asyncio
async def test_init_db_creates_gist_fts_infra(tmp_path: Path) -> None:
    db_path = tmp_path / "phase7-gist-fts.db"
    client = SQLiteClient(_sqlite_url(db_path))
    await client.init_db()
    await client.close()

    with sqlite3.connect(db_path) as conn:
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        trigger_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }

    assert "memory_gists_fts" in table_names
    assert {"memory_gists_ai", "memory_gists_ad", "memory_gists_au"} <= trigger_names


@pytest.mark.asyncio
async def test_search_advanced_updates_gist_fts_after_gist_refresh(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-gist-refresh.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="workflow anchor",
        priority=1,
        title="workflow-anchor",
        domain="core",
    )
    memory = await _get_memory_by_path(client, "workflow-anchor")
    await client.upsert_memory_gist(
        memory_id=memory.id,
        gist_text="first rollout checklist",
        source_hash="phase7-gist-refresh",
        gist_method="extractive_bullets",
        quality_score=0.8,
    )
    await client.upsert_memory_gist(
        memory_id=memory.id,
        gist_text="second rollout checklist",
        source_hash="phase7-gist-refresh",
        gist_method="extractive_bullets",
        quality_score=0.9,
    )

    second_payload = await client.search_advanced(
        query="second rollout",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    first_payload = await client.search_advanced(
        query="first rollout",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    assert second_payload["results"]
    assert second_payload["results"][0]["uri"] == "core://workflow-anchor"
    assert second_payload["metadata"]["candidate_counts"]["gist_rows"] >= 1
    assert first_payload["results"] == []


@pytest.mark.asyncio
async def test_search_advanced_caps_candidate_multiplier_with_hard_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SEARCH_HARD_MAX_CANDIDATE_MULTIPLIER", "3")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-candidate-cap.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="candidate multiplier cap regression",
        priority=1,
        title="candidate-cap",
        domain="core",
    )

    payload = await client.search_advanced(
        query="candidate multiplier",
        mode="keyword",
        max_results=4,
        candidate_multiplier=99,
        filters={"domain": "core"},
    )
    await client.close()

    assert payload["metadata"]["candidate_multiplier_applied"] == 3
    assert payload["metadata"]["candidate_counts"]["candidate_limit"] == 12


@pytest.mark.asyncio
async def test_search_advanced_caps_candidate_multiplier_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SEARCH_HARD_MAX_CANDIDATE_MULTIPLIER", "3")
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-candidate-cap.db"))
    await client.init_db()

    payload = await client.search_advanced(
        query="whiteboard",
        mode="keyword",
        max_results=5,
        candidate_multiplier=999,
        filters={"domain": "core"},
    )
    await client.close()

    metadata = payload["metadata"]
    assert metadata["candidate_multiplier_applied"] == 3
    assert metadata["search_hard_max_candidate_multiplier"] == 3


@pytest.mark.asyncio
async def test_create_memory_rejects_out_of_range_priority_value(tmp_path: Path) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-priority-range.db"))
    await client.init_db()

    with pytest.raises(ValueError, match="priority must be between 0 and 999"):
        await client.create_memory(
            parent_path="",
            content="invalid priority",
            priority=1000,
            title="invalid-priority",
            domain="core",
        )

    await client.close()


@pytest.mark.asyncio
async def test_fetch_alias_candidate_rows_expands_missing_alias_paths(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-alias-expansion.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="timeline namespace",
        priority=1,
        title="timeline",
        domain="core",
    )
    await client.create_memory(
        parent_path="",
        content="release checklist and launch tasks",
        priority=1,
        title="release-anchor",
        domain="core",
    )
    await client.add_path(
        new_path="timeline/release-anchor",
        target_path="release-anchor",
        new_domain="core",
        target_domain="core",
        priority=3,
        disclosure="timeline recall",
    )
    memory = await _get_memory_by_path(client, "release-anchor")

    async with client.session() as session:
        rows = await client._fetch_alias_candidate_rows(
            session,
            memory_seeds=[
                {
                    "memory_id": memory.id,
                    "chunk_id": None,
                    "domain": "core",
                    "path": "release-anchor",
                    "uri": "core://release-anchor",
                    "signal": 0.92,
                    "chunk_text": "release checklist and launch tasks",
                    "char_start": 0,
                    "char_end": len("release checklist and launch tasks"),
                    "chunk_length": len("release checklist and launch tasks"),
                    "gist_quality": 0.0,
                }
            ],
            seen_uris={"core://release-anchor"},
            domain_filter="core",
            path_prefix_filter=None,
            priority_filter=None,
            updated_after_filter=None,
        )
    await client.close()

    assert [row["path"] for row in rows] == ["timeline/release-anchor"]
    assert rows[0]["recall_kind"] == "alias"
    assert rows[0]["origin_uri"] == "core://release-anchor"
    assert rows[0]["context_score"] > 0.0


@pytest.mark.asyncio
async def test_search_advanced_expands_ancestor_recall_with_provenance(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-ancestor-recall.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="program umbrella",
        priority=1,
        title="projects",
        domain="core",
    )
    await client.create_memory(
        parent_path="projects",
        content="release checklist for launch planning",
        priority=1,
        title="launch-plan",
        domain="core",
    )

    payload = await client.search_advanced(
        query="release checklist",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    assert payload["metadata"]["candidate_counts"]["ancestor_rows"] >= 1
    assert payload["results"][0]["uri"] == "core://projects/launch-plan"
    ancestor_result = next(
        item for item in payload["results"] if item["uri"] == "core://projects"
    )
    provenance = ancestor_result["metadata"]["search_provenance"]
    assert provenance["recall_kind"] == "ancestor"
    assert provenance["origin_uri"] == "core://projects/launch-plan"
    assert provenance["ancestor_depth"] == 1
    assert "ancestor" in provenance["stages"]


@pytest.mark.asyncio
async def test_search_advanced_path_prefix_filter_is_tree_scoped(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-path-prefix-tree.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="benchmark root",
        priority=999,
        title="benchmark",
        domain="notes",
        index_now=False,
    )
    await client.create_memory(
        parent_path="benchmark",
        content="benchmark beir namespace",
        priority=999,
        title="beir_nfcorpus",
        domain="notes",
        index_now=False,
    )
    await client.create_memory(
        parent_path="benchmark",
        content="benchmark beir sibling namespace",
        priority=999,
        title="beir_nfcorpus_extra",
        domain="notes",
        index_now=False,
    )
    await client.create_memory(
        parent_path="benchmark/beir_nfcorpus",
        content="omega fats target document",
        priority=10,
        title="doc-a",
        domain="notes",
    )
    await client.create_memory(
        parent_path="benchmark/beir_nfcorpus_extra",
        content="omega fats sibling document",
        priority=10,
        title="doc-b",
        domain="notes",
    )

    payload = await client.search_advanced(
        query="omega fats",
        mode="keyword",
        max_results=10,
        candidate_multiplier=4,
        filters={
            "domain": "notes",
            "path_prefix": "benchmark/beir_nfcorpus",
            "max_priority": 10,
        },
    )
    await client.close()

    uris = [item["uri"] for item in payload["results"]]
    assert "notes://benchmark/beir_nfcorpus/doc-a" in uris
    assert "notes://benchmark/beir_nfcorpus_extra/doc-b" not in uris


@pytest.mark.asyncio
async def test_search_advanced_reorders_with_vitality_and_access_signals(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-vitality.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="release checklist for retrieval ranking",
        priority=1,
        title="alpha-hit",
        domain="core",
    )
    await client.create_memory(
        parent_path="",
        content="release checklist for retrieval ranking",
        priority=1,
        title="zeta-hit",
        domain="core",
    )

    async with client.session() as session:
        result = await session.execute(
            select(Memory, MemoryPath)
            .join(MemoryPath, Memory.id == MemoryPath.memory_id)
            .where(MemoryPath.domain == "core")
            .where(MemoryPath.path.in_(["alpha-hit", "zeta-hit"]))
        )
        for memory, path_obj in result.all():
            if path_obj.path == "zeta-hit":
                memory.vitality_score = client._vitality_max_score
                memory.access_count = 18
                memory.last_accessed_at = _utc_now_naive()
            else:
                memory.vitality_score = max(0.05, client._vitality_max_score * 0.1)
                memory.access_count = 0
                memory.last_accessed_at = None
            session.add(memory)

    payload = await client.search_advanced(
        query="release checklist",
        mode="keyword",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    assert [item["uri"] for item in payload["results"][:2]] == [
        "core://zeta-hit",
        "core://alpha-hit",
    ]
    assert payload["results"][0]["scores"]["vitality"] > payload["results"][1]["scores"]["vitality"]
    assert payload["results"][0]["scores"]["access"] > payload["results"][1]["scores"]["access"]


@pytest.mark.asyncio
async def test_search_advanced_prefers_pending_recent_plan_events_for_temporal_plan_queries(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-pending-event-ranking.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="agents namespace",
        priority=4,
        title="agents",
        domain="core",
        index_now=False,
    )
    await client.create_memory(
        parent_path="agents",
        content="main namespace",
        priority=4,
        title="main",
        domain="core",
        index_now=False,
    )
    await client.create_memory(
        parent_path="agents/main",
        content="pending namespace",
        priority=4,
        title="pending",
        domain="core",
        index_now=False,
    )
    await client.create_memory(
        parent_path="agents/main/pending",
        content="rule capture namespace",
        priority=4,
        title="rule-capture",
        domain="core",
        index_now=False,
    )
    await client.create_memory(
        parent_path="agents/main/pending/rule-capture",
        content="event namespace",
        priority=4,
        title="event",
        domain="core",
        index_now=False,
    )
    await client.create_memory(
        parent_path="agents/main",
        content="captured namespace",
        priority=4,
        title="captured",
        domain="core",
        index_now=False,
    )
    await client.create_memory(
        parent_path="agents/main/captured",
        content="preference namespace",
        priority=4,
        title="preference",
        domain="core",
        index_now=False,
    )
    await client.create_memory(
        parent_path="agents/main/pending/rule-capture/event",
        content=(
            "# Memory Palace Durable Fact\n"
            "- category: event\n"
            "- source_mode: rule_capture\n"
            "- capture_layer: auto_capture_pending\n"
            "- confidence: 0.68\n"
            "- pending_candidate: true\n\n"
            "## Summary\n"
            "我明天打算去湖边散步\n"
        ),
        priority=4,
        title="sha256-pendingwalk",
        domain="core",
    )
    await client.create_memory(
        parent_path="agents/main/captured/preference",
        content="我平时喜欢在湖边散步",
        priority=1,
        title="sha256-preferencewalk",
        domain="core",
    )

    payload = await client.search_advanced(
        query="明天打算去湖边散步",
        mode="hybrid",
        max_results=5,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    assert payload["results"]
    assert payload["results"][0]["uri"] == "core://agents/main/pending/rule-capture/event/sha256-pendingwalk"
    assert payload["results"][0]["scores"]["pending_event"] > 0.0


@pytest.mark.asyncio
async def test_search_advanced_semantic_python_scoring_ignores_row_order_limit_bias(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-semantic-python-order.db"))
    await client.init_db()
    client._vector_engine_effective = "legacy"
    client._sqlite_vec_knn_ready = False

    for idx in range(160):
        content = (
            "zz_unique_probe_term biomedical signal"
            if idx == 159
            else f"common filler document {idx}"
        )
        await client.create_memory(
            parent_path="",
            content=content,
            priority=1,
            title=f"doc-{idx:03d}",
            domain="core",
        )

    payload = await client.search_advanced(
        query="zz_unique_probe_term",
        mode="semantic",
        max_results=1,
        candidate_multiplier=1,
        filters={"domain": "core"},
    )
    await client.close()

    assert payload["results"][0]["uri"] == "core://doc-159"
    assert payload["metadata"]["vector_engine_path"] == "legacy_python_scoring"


@pytest.mark.asyncio
async def test_search_advanced_reinforces_same_memory_only_once_across_alias_hits(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-access-dedupe.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="timeline namespace",
        priority=1,
        title="timeline",
        domain="core",
    )
    await client.create_memory(
        parent_path="",
        content="duplicate alias release checklist",
        priority=1,
        title="release-duplicate",
        domain="core",
    )
    await client.add_path(
        new_path="timeline/release-duplicate",
        target_path="release-duplicate",
        new_domain="core",
        target_domain="core",
        priority=2,
    )

    payload = await client.search_advanced(
        query="duplicate alias release checklist",
        mode="keyword",
        max_results=5,
        candidate_multiplier=1,
        filters={"domain": "core"},
    )
    memory = await _get_memory_by_path(client, "release-duplicate")
    await client.close()

    duplicate_hits = [item for item in payload["results"] if item["memory_id"] == memory.id]
    assert len(duplicate_hits) >= 2
    assert memory.access_count == 1


@pytest.mark.asyncio
async def test_reranker_provider_supports_cohere_payload_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_PROVIDER", "cohere")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "rerank-v1")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-provider.db"))
    await client.init_db()

    captured: dict[str, object] = {}

    async def _fake_post_json(base: str, endpoint: str, payload, api_key: str = ""):
        captured["base"] = base
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["api_key"] = api_key
        return {
            "results": [
                {"index": 1, "relevance_score": 0.88},
                {"index": 0, "relevance_score": 0.41},
            ]
        }

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    scores = await client._get_rerank_scores(
        "release ranking", ["doc-a", "doc-b"], degrade_reasons=[]
    )
    await client.close()

    assert captured["base"] == "https://rerank.example/v1"
    assert captured["endpoint"] == "/rerank"
    assert captured["payload"] == {
        "model": "rerank-v1",
        "query": "release ranking",
        "documents": [{"text": "doc-a"}, {"text": "doc-b"}],
        "top_n": 2,
    }
    assert scores == {1: 0.88, 0: 0.41}


@pytest.mark.asyncio
async def test_reranker_small_batch_provider_used_within_document_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_PROVIDER", "openai")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://primary-rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_KEY", "primary-key")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "primary-model")
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_PROVIDER", "cohere")
    monkeypatch.setenv(
        "RETRIEVAL_RERANKER_SMALL_BATCH_API_BASE",
        "https://small-batch-rerank.example/v1",
    )
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_API_KEY", "small-batch-key")
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_MODEL", "small-batch-model")
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_MAX_DOCUMENTS", "25")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-small-batch.db"))
    await client.init_db()

    captured: dict[str, object] = {}

    async def _fake_post_json(base: str, endpoint: str, payload, api_key: str = ""):
        captured["base"] = base
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["api_key"] = api_key
        return {"results": [{"index": 0, "relevance_score": 0.91}]}

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    scores = await client._get_rerank_scores(
        "release ranking", ["doc-a", "doc-b"], degrade_reasons=[]
    )
    await client.close()

    assert captured["base"] == "https://small-batch-rerank.example/v1"
    assert captured["endpoint"] == "/rerank"
    assert captured["api_key"] == "small-batch-key"
    assert captured["payload"] == {
        "model": "small-batch-model",
        "query": "release ranking",
        "documents": [{"text": "doc-a"}, {"text": "doc-b"}],
        "top_n": 2,
    }
    assert scores == {0: 0.91}


@pytest.mark.asyncio
async def test_reranker_small_batch_provider_skipped_above_document_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_PROVIDER", "openai")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://primary-rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_KEY", "primary-key")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "primary-model")
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_PROVIDER", "cohere")
    monkeypatch.setenv(
        "RETRIEVAL_RERANKER_SMALL_BATCH_API_BASE",
        "https://small-batch-rerank.example/v1",
    )
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_API_KEY", "small-batch-key")
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_MODEL", "small-batch-model")
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_MAX_DOCUMENTS", "25")

    client = SQLiteClient(
        _sqlite_url(tmp_path / "phase7-rerank-small-batch-threshold.db")
    )
    await client.init_db()

    captured: dict[str, object] = {}

    async def _fake_post_json(base: str, endpoint: str, payload, api_key: str = ""):
        captured["base"] = base
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["api_key"] = api_key
        return {"results": [{"index": 0, "relevance_score": 0.66}]}

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    documents = [f"doc-{idx}" for idx in range(26)]
    scores = await client._get_rerank_scores(
        "release ranking", documents, degrade_reasons=[]
    )
    await client.close()

    assert captured["base"] == "https://primary-rerank.example/v1"
    assert captured["endpoint"] == "/rerank"
    assert captured["api_key"] == "primary-key"
    assert captured["payload"] == {
        "model": "primary-model",
        "query": "release ranking",
        "documents": documents,
    }
    assert scores == {0: 0.66}


@pytest.mark.asyncio
async def test_reranker_uses_dedicated_timeout_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "rerank-v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_TIMEOUT_SEC", "2.5")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-timeout.db"))
    await client.init_db()

    captured: dict[str, object] = {}

    async def _fake_post_json(
        base: str,
        endpoint: str,
        payload,
        api_key: str = "",
        timeout_sec: float | None = None,
    ):
        captured["base"] = base
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["api_key"] = api_key
        captured["timeout_sec"] = timeout_sec
        return {
            "results": [
                {"index": 0, "relevance_score": 0.77},
            ]
        }

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    scores = await client._get_rerank_scores(
        "release ranking", ["doc-a"], degrade_reasons=[]
    )
    await client.close()

    assert captured["base"] == "https://rerank.example/v1"
    assert captured["endpoint"] == "/rerank"
    assert captured["timeout_sec"] == pytest.approx(2.5)
    assert scores == {0: 0.77}


@pytest.mark.asyncio
async def test_reranker_falls_back_to_secondary_provider_when_primary_request_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://primary-rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_KEY", "primary-key")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "primary-model")
    monkeypatch.setenv("RETRIEVAL_RERANKER_FALLBACK_API_BASE", "https://fallback-rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_FALLBACK_API_KEY", "fallback-key")
    monkeypatch.setenv("RETRIEVAL_RERANKER_FALLBACK_MODEL", "fallback-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-fallback.db"))
    await client.init_db()

    calls: list[tuple[str, str, str, str]] = []

    async def _fake_post_json(
        base: str,
        endpoint: str,
        payload,
        api_key: str = "",
        timeout_sec: float | None = None,
    ):
        calls.append((base, endpoint, str(payload.get("model") or ""), api_key))
        _ = timeout_sec
        if "primary-rerank" in base:
            return None
        return {
            "results": [
                {"index": 0, "relevance_score": 0.93},
                {"index": 1, "relevance_score": 0.41},
            ]
        }

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    degrade_reasons: list[str] = []
    scores = await client._get_rerank_scores(
        "release ranking", ["doc-a", "doc-b"], degrade_reasons=degrade_reasons
    )
    await client.close()

    assert calls == [
        ("https://primary-rerank.example/v1", "/rerank", "primary-model", "primary-key"),
        ("https://primary-rerank.example/v1", "/rerank", "primary-model", "primary-key"),
        ("https://fallback-rerank.example/v1", "/rerank", "fallback-model", "fallback-key"),
    ]
    assert scores == {0: 0.93, 1: 0.41}
    assert "reranker_request_failed" in degrade_reasons
    assert "reranker_primary_failed_fallback_attempted" in degrade_reasons
    assert "reranker_fallback_used" in degrade_reasons


@pytest.mark.asyncio
async def test_reranker_request_timeout_adds_structured_degrade_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://primary-rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_KEY", "primary-key")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "primary-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-timeout.db"))
    await client.init_db()

    async def _fake_post_json(
        _base: str,
        _endpoint: str,
        _payload,
        api_key: str = "",
        timeout_sec: float | None = None,
        error_sink: dict[str, object] | None = None,
    ):
        _ = api_key
        _ = timeout_sec
        if error_sink is not None:
            error_sink.update(
                {
                    "category": "request_error",
                    "error_type": "ConnectTimeout",
                    "message": "reranker timeout while connecting",
                }
            )
        return None

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    degrade_reasons: list[str] = []
    scores = await client._get_rerank_scores(
        "release ranking", ["doc-a", "doc-b"], degrade_reasons=degrade_reasons
    )
    await client.close()

    assert scores == {}
    assert "reranker_request_failed" in degrade_reasons
    assert "reranker_request_failed:request_error" in degrade_reasons
    assert "reranker_request_failed:request_error:ConnectTimeout" in degrade_reasons
    assert "reranker_request_failed:timeout" in degrade_reasons


@pytest.mark.asyncio
async def test_reranker_retries_primary_once_before_returning_scores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://primary-rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_KEY", "primary-key")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "primary-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-primary-retry.db"))
    await client.init_db()

    calls: list[tuple[str, str, str, str]] = []

    async def _fake_post_json(
        base: str,
        endpoint: str,
        payload,
        api_key: str = "",
        timeout_sec: float | None = None,
    ):
        calls.append((base, endpoint, str(payload.get("model") or ""), api_key))
        _ = timeout_sec
        if len(calls) == 1:
            return None
        return {
            "results": [
                {"index": 1, "relevance_score": 0.87},
                {"index": 0, "relevance_score": 0.44},
            ]
        }

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    degrade_reasons: list[str] = []
    scores = await client._get_rerank_scores(
        "release ranking", ["doc-a", "doc-b"], degrade_reasons=degrade_reasons
    )
    await client.close()

    assert calls == [
        ("https://primary-rerank.example/v1", "/rerank", "primary-model", "primary-key"),
        ("https://primary-rerank.example/v1", "/rerank", "primary-model", "primary-key"),
    ]
    assert scores == {1: 0.87, 0: 0.44}
    assert "reranker_request_failed" not in degrade_reasons
    assert "reranker_primary_failed_fallback_attempted" not in degrade_reasons


@pytest.mark.asyncio
async def test_reranker_provider_supports_lmstudio_chat_payload_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_PROVIDER", "lmstudio_chat")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "qwen3-reranker-8b")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-lmstudio-chat.db"))
    await client.init_db()

    captured: dict[str, object] = {}

    async def _fake_post_json(base: str, endpoint: str, payload, api_key: str = ""):
        captured["base"] = base
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["api_key"] = api_key
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"results":[{"index":1,"score":0.81},{"index":0,"score":0.27}]}',
                    }
                }
            ]
        }

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    scores = await client._get_rerank_scores(
        "release ranking", ["doc-a", "doc-b"], degrade_reasons=[]
    )
    await client.close()

    assert captured["base"] == "http://127.0.0.1:1234/v1"
    assert captured["endpoint"] == "/chat/completions"
    assert captured["payload"] == {
        "model": "qwen3-reranker-8b",
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return strict JSON only."},
            {
                "role": "user",
                "content": client._build_lmstudio_rerank_prompt("release ranking", ["doc-a", "doc-b"]),
            },
        ],
    }
    assert scores == {1: 0.81, 0: 0.27}


@pytest.mark.asyncio
async def test_reranker_provider_supports_lmstudio_responses_payload_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_PROVIDER", "lmstudio_responses")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "http://127.0.0.1:1234/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "qwen3-reranker-8b")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-lmstudio-responses.db"))
    await client.init_db()

    captured: dict[str, object] = {}

    async def _fake_post_json(base: str, endpoint: str, payload, api_key: str = ""):
        captured["base"] = base
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        captured["api_key"] = api_key
        return {
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"results":[{"index":0,"score":0.93},{"index":1,"score":0.02}]}',
                        }
                    ],
                }
            ]
        }

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    scores = await client._get_rerank_scores(
        "release ranking", ["doc-a", "doc-b"], degrade_reasons=[]
    )
    await client.close()

    assert captured["base"] == "http://127.0.0.1:1234/v1"
    assert captured["endpoint"] == "/responses"
    assert captured["payload"] == {
        "model": "qwen3-reranker-8b",
        "input": client._build_lmstudio_rerank_prompt("release ranking", ["doc-a", "doc-b"]),
        "temperature": 0,
    }
    assert scores == {0: 0.93, 1: 0.02}


@pytest.mark.asyncio
async def test_search_advanced_keyword_mode_skips_reranker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "rerank-v1")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-keyword-rerank-skip.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="keyword mode should stay deterministic",
        priority=1,
        title="keyword-doc",
        domain="core",
    )

    calls = {"value": 0}

    async def _fake_get_rerank_scores(*_args, **_kwargs):
        calls["value"] += 1
        return {0: 0.99}

    monkeypatch.setattr(client, "_get_rerank_scores", _fake_get_rerank_scores)
    payload = await client.search_advanced(
        query="keyword mode",
        mode="keyword",
        max_results=3,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    assert calls["value"] == 0
    assert payload["metadata"]["rerank_applied"] is False
    assert payload["metadata"]["rerank_documents"] == 0


@pytest.mark.asyncio
async def test_search_advanced_metadata_includes_trace_fields(
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-trace.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content="trace ready retrieval document",
        priority=1,
        title="trace-doc",
        domain="core",
    )

    payload = await client.search_advanced(
        query="trace ready",
        mode="hybrid",
        max_results=3,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    metadata = payload["metadata"]
    assert "stage_timings_ms" in metadata
    assert "candidate_counts" in metadata
    assert "rerank_provider" in metadata
    assert "rerank_timeout_sec" in metadata
    assert "rerank_top_n" in metadata
    assert "same_uri_collapse" in metadata
    assert "mmr_duplicate_ratio_before" in metadata
    assert "mmr_duplicate_ratio_after" in metadata
    assert metadata["candidate_counts"]["candidate_limit"] == 6
    assert "alias_rows" in metadata["candidate_counts"]
    assert "ancestor_rows" in metadata["candidate_counts"]


@pytest.mark.asyncio
async def test_search_advanced_collapses_same_uri_chunk_hits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_CHUNK_SIZE", "128")
    monkeypatch.setenv("RETRIEVAL_CHUNK_OVERLAP", "0")
    monkeypatch.setenv("RETRIEVAL_COLLAPSE_SAME_URI", "true")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-collapse-same-uri.db"))
    await client.init_db()
    await client.create_memory(
        parent_path="",
        content=("whiteboard launch checklist " * 64).strip(),
        priority=1,
        title="whiteboard-plan",
        domain="core",
    )

    payload = await client.search_advanced(
        query="whiteboard",
        mode="keyword",
        max_results=10,
        candidate_multiplier=4,
        filters={"domain": "core"},
    )
    await client.close()

    assert [item["uri"] for item in payload["results"]] == ["core://whiteboard-plan"]
    collapse = payload["metadata"]["same_uri_collapse"]
    assert collapse["applied"] is True
    assert collapse["collapsed_rows"] >= 1
    assert payload["metadata"]["candidate_counts"]["combined_candidates"] > 1
    assert payload["metadata"]["candidate_counts"]["combined_candidates_collapsed"] == 1
    provenance = payload["results"][0]["metadata"]["search_provenance"]
    assert provenance["same_uri_collapsed"] is True
    assert provenance["same_uri_hits"] >= 2


@pytest.mark.asyncio
async def test_create_memory_dedupes_embedding_cache_rows_for_repeated_chunks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETRIEVAL_CHUNK_SIZE", "128")
    monkeypatch.setenv("RETRIEVAL_CHUNK_OVERLAP", "0")

    content = ("whiteboard launch checklist " * 64).strip()
    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-embedding-cache-dedupe.db"))
    await client.init_db()

    expected_unique_hashes = {
        hashlib.sha256(
            re.sub(r"\s+", " ", chunk_text.strip().lower()).encode("utf-8")
        ).hexdigest()
        for _, _, _, chunk_text in client._chunk_content(content)
    }

    await client.create_memory(
        parent_path="",
        content=content,
        priority=1,
        title="whiteboard-cache",
        domain="core",
    )

    async with client.session() as session:
        cache_count = await session.scalar(
            select(func.count()).select_from(EmbeddingCache)
        )
    await client.close()

    assert cache_count == len(expected_unique_hashes)


@pytest.mark.asyncio
async def test_search_advanced_caps_rerank_documents_before_remote_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "rerank-v1")
    monkeypatch.setenv("RETRIEVAL_RERANK_TOP_N", "2")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-topn.db"))
    await client.init_db()
    for title in ("alpha-hit", "beta-hit", "gamma-hit", "delta-hit"):
        await client.create_memory(
            parent_path="",
            content="release checklist remote rerank probe",
            priority=1,
            title=title,
            domain="core",
        )

    captured_documents: list[str] = []

    async def _fake_get_rerank_scores(
        _query: str,
        documents: list[str],
        degrade_reasons=None,
    ) -> dict[int, float]:
        _ = degrade_reasons
        captured_documents[:] = list(documents)
        return {idx: 0.01 * (len(documents) - idx) for idx in range(len(documents))}

    monkeypatch.setattr(client, "_get_rerank_scores", _fake_get_rerank_scores)
    payload = await client.search_advanced(
        query="release checklist",
        mode="hybrid",
        max_results=4,
        candidate_multiplier=3,
        filters={"domain": "core"},
    )
    await client.close()

    assert len(captured_documents) == 2
    metadata = payload["metadata"]
    assert metadata["rerank_candidate_pool"] >= 4
    assert metadata["rerank_group_count"] >= 4
    assert metadata["rerank_grouping"] == "memory"
    assert metadata["rerank_pruned_groups"] >= 2
    assert metadata["rerank_documents"] == 2
    assert metadata["rerank_top_n"] == 2
    assert metadata["rerank_top_n_effective"] == 2
    assert metadata["rerank_group_by_memory"] is True


@pytest.mark.asyncio
async def test_search_advanced_collapses_duplicate_chunks_for_same_uri(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETRIEVAL_COLLAPSE_SAME_URI", "true")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-same-uri-collapse.db"))
    await client.init_db()
    repeated_content = " ".join(
        [
            "alpha filler block " * 12,
            "zz_phase7_unique_marker launch planning note " * 8,
            "beta filler block " * 12,
            "zz_phase7_unique_marker rollout checklist " * 8,
            "gamma filler block " * 12,
            "zz_phase7_unique_marker incident drill " * 8,
        ]
    )
    await client.create_memory(
        parent_path="",
        content=repeated_content,
        priority=1,
        title="multi-chunk-doc",
        domain="core",
    )

    payload = await client.search_advanced(
        query="zz_phase7_unique_marker",
        mode="keyword",
        max_results=10,
        candidate_multiplier=4,
        filters={"domain": "core"},
    )
    await client.close()

    results = [
        item for item in payload["results"] if item["uri"] == "core://multi-chunk-doc"
    ]
    assert len(results) == 1
    assert payload["metadata"]["same_uri_collapse"]["applied"] is True
    assert payload["metadata"]["same_uri_collapse"]["collapsed_rows"] >= 1
    assert (
        payload["results"][0]["metadata"]["search_provenance"]["same_uri_collapsed"]
        is True
    )


@pytest.mark.asyncio
async def test_search_advanced_limits_reranker_to_top_n_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "rerank-v1")
    monkeypatch.setenv("RETRIEVAL_RERANK_TOP_N", "2")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-topn.db"))
    await client.init_db()
    for idx in range(4):
        await client.create_memory(
            parent_path="",
            content=f"rerank limit probe release candidate {idx} with unique text {idx}",
            priority=1,
            title=f"rerank-doc-{idx}",
            domain="core",
        )

    captured: dict[str, object] = {}

    async def _fake_get_rerank_scores(query: str, documents, degrade_reasons=None):
        captured["query"] = query
        captured["documents"] = list(documents)
        captured["degrade_reasons"] = list(degrade_reasons or [])
        return {0: 0.91, 1: 0.77}

    monkeypatch.setattr(client, "_get_rerank_scores", _fake_get_rerank_scores)
    payload = await client.search_advanced(
        query="release candidate",
        mode="hybrid",
        max_results=4,
        candidate_multiplier=4,
        filters={"domain": "core"},
    )
    await client.close()

    assert captured["query"] == "release candidate"
    assert len(captured["documents"]) == 2
    assert payload["metadata"]["rerank_candidate_pool"] == 4
    assert payload["metadata"]["rerank_group_count"] == 4
    assert payload["metadata"]["rerank_grouping"] == "memory"
    assert payload["metadata"]["rerank_pruned_groups"] == 2
    assert payload["metadata"]["rerank_documents"] == 2
    assert payload["metadata"]["rerank_top_n"] == 2
    assert payload["metadata"]["rerank_top_n_effective"] == 2


@pytest.mark.asyncio
async def test_search_advanced_expands_semantic_candidate_stage_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETRIEVAL_SEMANTIC_OVERFETCH_FACTOR", "3")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-semantic-overfetch.db"))
    await client.init_db()
    created = await client.create_memory(
        parent_path="",
        content="semantic overfetch probe",
        priority=1,
        title="semantic-overfetch-doc",
        domain="core",
    )

    captured: dict[str, int] = {}

    async def _fake_get_embedding(*_args, **_kwargs):
        return [0.0] * client._embedding_dim

    async def _fake_fetch_semantic_rows(
        _session,
        *,
        where_clause,
        where_params,
        query_embedding,
        semantic_pool_limit,
        candidate_limit,
    ):
        _ = where_clause
        _ = where_params
        _ = query_embedding
        captured["semantic_pool_limit"] = int(semantic_pool_limit)
        captured["candidate_limit"] = int(candidate_limit)
        return [
            {
                "chunk_id": None,
                "memory_id": int(created["id"]),
                "chunk_text": "semantic overfetch probe",
                "char_start": 0,
                "char_end": len("semantic overfetch probe"),
                "domain": "core",
                "path": "semantic-overfetch-doc",
                "priority": 1,
                "disclosure": None,
                "created_at": _utc_now_naive(),
                "vitality_score": 1.0,
                "access_count": 0,
                "last_accessed_at": None,
                "chunk_length": len("semantic overfetch probe"),
                "vector_similarity": 0.95,
            }
        ]

    monkeypatch.setattr(client, "_get_embedding", _fake_get_embedding)
    monkeypatch.setattr(
        client,
        "_fetch_semantic_rows_python_scoring",
        _fake_fetch_semantic_rows,
    )
    client._vector_engine_effective = "legacy"
    client._sqlite_vec_knn_ready = False

    payload = await client.search_advanced(
        query="semantic overfetch",
        mode="semantic",
        max_results=2,
        candidate_multiplier=2,
        filters={"domain": "core"},
    )
    await client.close()

    assert captured["semantic_pool_limit"] == 128
    assert captured["candidate_limit"] == 12
    assert payload["metadata"]["candidate_counts"]["candidate_limit"] == 4
    assert payload["metadata"]["candidate_counts"]["semantic_candidate_limit"] == 12
    assert payload["results"][0]["uri"] == "core://semantic-overfetch-doc"


@pytest.mark.asyncio
async def test_search_advanced_caps_default_rerank_pool_with_adaptive_limit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "rerank-v1")

    client = SQLiteClient(_sqlite_url(tmp_path / "phase7-rerank-adaptive-cap.db"))
    await client.init_db()
    for idx in range(20):
        await client.create_memory(
            parent_path="",
            content=f"adaptive rerank probe release candidate {idx}",
            priority=1,
            title=f"adaptive-rerank-{idx:02d}",
            domain="core",
        )

    captured_documents: list[str] = []

    async def _fake_get_rerank_scores(_query: str, documents: list[str], degrade_reasons=None):
        _ = degrade_reasons
        captured_documents[:] = list(documents)
        return {idx: 1.0 for idx in range(len(documents))}

    monkeypatch.setattr(client, "_get_rerank_scores", _fake_get_rerank_scores)
    payload = await client.search_advanced(
        query="adaptive rerank probe",
        mode="hybrid",
        max_results=4,
        candidate_multiplier=5,
        filters={"domain": "core"},
    )
    await client.close()

    assert payload["metadata"]["rerank_candidate_pool"] == 20
    assert len(captured_documents) == 16
    assert payload["metadata"]["rerank_documents"] == 16
    assert payload["metadata"]["rerank_top_n"] == 48
    assert payload["metadata"]["rerank_top_n_effective"] == 16


@pytest.mark.asyncio
async def test_search_advanced_reports_small_batch_rerank_route_in_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_ENABLED", "true")
    monkeypatch.setenv("RETRIEVAL_RERANKER_PROVIDER", "openai")
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://primary-rerank.example/v1")
    monkeypatch.setenv("RETRIEVAL_RERANKER_MODEL", "primary-model")
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_PROVIDER", "cohere")
    monkeypatch.setenv(
        "RETRIEVAL_RERANKER_SMALL_BATCH_API_BASE",
        "https://small-batch-rerank.example/v1",
    )
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_MODEL", "small-batch-model")
    monkeypatch.setenv("RETRIEVAL_RERANKER_SMALL_BATCH_MAX_DOCUMENTS", "25")

    client = SQLiteClient(
        _sqlite_url(tmp_path / "phase7-rerank-small-batch-metadata.db")
    )
    await client.init_db()
    for idx in range(20):
        await client.create_memory(
            parent_path="",
            content=f"small batch provider probe release candidate {idx}",
            priority=1,
            title=f"small-batch-provider-{idx:02d}",
            domain="core",
        )

    async def _fake_get_rerank_scores(
        _query: str, documents: list[str], degrade_reasons=None
    ):
        _ = degrade_reasons
        return {idx: 1.0 for idx in range(len(documents))}

    monkeypatch.setattr(client, "_get_rerank_scores", _fake_get_rerank_scores)
    payload = await client.search_advanced(
        query="small batch provider probe",
        mode="hybrid",
        max_results=4,
        candidate_multiplier=5,
        filters={"domain": "core"},
    )
    await client.close()

    assert payload["metadata"]["rerank_documents"] == 16
    assert payload["metadata"]["rerank_provider"] == "cohere"
    assert payload["metadata"]["rerank_route"] == "small_batch"
