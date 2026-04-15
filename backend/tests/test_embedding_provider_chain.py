import logging
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from db.sqlite_client import SQLiteClient


def _sqlite_url(db_path: Path) -> str:
    return f"sqlite+aiosqlite:///{db_path}"


def _clear_embedding_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "RETRIEVAL_EMBEDDING_API_BASE",
        "RETRIEVAL_EMBEDDING_BASE",
        "ROUTER_API_BASE",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "RETRIEVAL_EMBEDDING_API_KEY",
        "RETRIEVAL_EMBEDDING_KEY",
        "ROUTER_API_KEY",
        "OPENAI_API_KEY",
        "RETRIEVAL_EMBEDDING_MODEL",
        "ROUTER_EMBEDDING_MODEL",
        "OPENAI_EMBEDDING_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.asyncio
async def test_embedding_provider_chain_disabled_keeps_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("EMBEDDING_PROVIDER_CHAIN_ENABLED", "false")

    client = SQLiteClient(_sqlite_url(tmp_path / "provider-chain-disabled.db"))
    await client.init_db()

    degrade_reasons: list[str] = []
    async with client.session() as session:
        embedding = await client._get_embedding(
            session,
            "provider chain disabled fallback sample",
            degrade_reasons=degrade_reasons,
        )
    await client.close()

    assert len(embedding) == client._embedding_dim
    assert "embedding_config_missing" in degrade_reasons
    assert "embedding_fallback_hash" in degrade_reasons


@pytest.mark.asyncio
async def test_embedding_provider_chain_uses_configured_fallback_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "router")
    monkeypatch.setenv("EMBEDDING_PROVIDER_CHAIN_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FAIL_OPEN", "false")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FALLBACK", "api")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://embedding.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "chain-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "provider-chain-fallback-api.db"))
    await client.init_db()

    call_meta: dict[str, str] = {"base": "", "endpoint": "", "api_key": ""}

    async def _fake_post_json(base: str, endpoint: str, payload, api_key: str = ""):
        call_meta["base"] = base
        call_meta["endpoint"] = endpoint
        call_meta["api_key"] = api_key
        assert payload["model"] == "chain-model"
        assert payload["dimensions"] == client._embedding_dim
        return {"data": [{"embedding": [0.11] * client._embedding_dim}]}

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    degrade_reasons: list[str] = []
    async with client.session() as session:
        embedding = await client._get_embedding(
            session,
            "provider chain fallback provider sample",
            degrade_reasons=degrade_reasons,
        )
    await client.close()

    assert embedding == [0.11] * client._embedding_dim
    assert call_meta["base"] == "https://embedding.example/v1"
    assert call_meta["endpoint"] == "/embeddings"
    assert call_meta["api_key"] == "test-key"
    assert "embedding_fallback_hash" not in degrade_reasons


@pytest.mark.asyncio
async def test_reranker_api_base_warns_when_multiple_env_sources_are_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("RETRIEVAL_RERANKER_API_BASE", "https://preferred.example/v1")
    monkeypatch.setenv("ROUTER_API_BASE", "https://ignored-router.example/v1")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ignored-openai.example/v1")

    with caplog.at_level(logging.WARNING):
        client = SQLiteClient(_sqlite_url(tmp_path / "reranker-base-conflict.db"))

    await client.close()

    assert client._reranker_api_base == "https://preferred.example/v1"
    assert "Reranker API base resolved from RETRIEVAL_RERANKER_API_BASE" in caplog.text
    assert "ROUTER_API_BASE" in caplog.text
    assert "OPENAI_BASE_URL" in caplog.text


@pytest.mark.asyncio
async def test_remote_embedding_dim_mismatch_falls_back_to_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://embedding.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "dim-check-model")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_DIM", "16")

    client = SQLiteClient(_sqlite_url(tmp_path / "embedding-dim-mismatch.db"))
    await client.init_db()

    async def _fake_post_json(*_args, **_kwargs):
        return {"data": [{"embedding": [0.25] * 8}]}

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    degrade_reasons: list[str] = []
    async with client.session() as session:
        embedding = await client._get_embedding(
            session,
            "remote embedding dimension mismatch sample",
            degrade_reasons=degrade_reasons,
        )
    await client.close()

    assert len(embedding) == 16
    assert embedding != [0.25] * 8
    assert "embedding_response_dim_mismatch" in degrade_reasons
    assert "embedding_response_dim_mismatch:8!=16" in degrade_reasons
    assert "embedding_fallback_hash" in degrade_reasons


@pytest.mark.asyncio
async def test_remote_embedding_timeout_adds_structured_degrade_reasons(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://embedding.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "timeout-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "embedding-timeout-diagnostics.db"))
    await client.init_db()

    async def _fake_post_json(
        _base: str,
        _endpoint: str,
        _payload,
        api_key: str = "",
        timeout_sec: float | None = None,
        error_sink: dict[str, Any] | None = None,
    ):
        _ = api_key
        _ = timeout_sec
        if error_sink is not None:
            error_sink.update(
                {
                    "category": "request_error",
                    "error_type": "ConnectTimeout",
                    "message": "embedding timeout while connecting",
                }
            )
        return None

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    degrade_reasons: list[str] = []
    async with client.session() as session:
        embedding = await client._get_embedding(
            session,
            "remote embedding timeout diagnostic sample",
            degrade_reasons=degrade_reasons,
        )
    await client.close()

    assert len(embedding) == client._embedding_dim
    assert "embedding_request_failed" in degrade_reasons
    assert "embedding_request_failed:api" in degrade_reasons
    assert "embedding_request_failed:request_error" in degrade_reasons
    assert "embedding_request_failed:request_error:ConnectTimeout" in degrade_reasons
    assert "embedding_request_failed:timeout" in degrade_reasons
    assert "embedding_request_failed:api:timeout" in degrade_reasons
    assert "embedding_fallback_hash" in degrade_reasons


@pytest.mark.asyncio
async def test_embedding_provider_chain_fail_closed_when_fallback_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("EMBEDDING_PROVIDER_CHAIN_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FAIL_OPEN", "false")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FALLBACK", "none")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://embedding.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "chain-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "provider-chain-fail-closed.db"))
    await client.init_db()

    async def _always_fail(*_args, **_kwargs):
        return None

    monkeypatch.setattr(client, "_post_json", _always_fail)
    degrade_reasons: list[str] = []
    async with client.session() as session:
        with pytest.raises(RuntimeError, match="embedding_provider_chain_blocked"):
            await client._get_embedding(
                session,
                "provider chain fail closed sample",
                degrade_reasons=degrade_reasons,
            )
    await client.close()

    assert "embedding_provider_chain_blocked" in degrade_reasons


@pytest.mark.asyncio
async def test_embedding_provider_chain_fail_open_still_hash_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("EMBEDDING_PROVIDER_CHAIN_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FAIL_OPEN", "true")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FALLBACK", "none")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://embedding.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "chain-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "provider-chain-fail-open.db"))
    await client.init_db()

    async def _always_fail(*_args, **_kwargs):
        return None

    monkeypatch.setattr(client, "_post_json", _always_fail)
    degrade_reasons: list[str] = []
    async with client.session() as session:
        embedding = await client._get_embedding(
            session,
            "provider chain fail open hash fallback sample",
            degrade_reasons=degrade_reasons,
        )
    await client.close()

    assert len(embedding) == client._embedding_dim
    assert "embedding_fallback_hash" in degrade_reasons


@pytest.mark.asyncio
async def test_embedding_provider_chain_cache_hit_avoids_second_remote_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("EMBEDDING_PROVIDER_CHAIN_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FAIL_OPEN", "false")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FALLBACK", "hash")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://embedding.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "cache-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "provider-chain-cache.db"))
    await client.init_db()

    call_counter = {"value": 0}

    async def _fake_post_json(*_args, **_kwargs):
        call_counter["value"] += 1
        return {"data": [{"embedding": [0.5] * client._embedding_dim}]}

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    async with client.session() as session:
        first = await client._get_embedding(session, "provider chain cache sample")
        await session.flush()
        second = await client._get_embedding(session, "provider chain cache sample")
    await client.close()

    assert first == [0.5] * client._embedding_dim
    assert second == [0.5] * client._embedding_dim
    assert call_counter["value"] == 1


@pytest.mark.asyncio
async def test_embedding_provider_chain_cache_hit_avoids_duplicate_pending_inserts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("EMBEDDING_PROVIDER_CHAIN_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FAIL_OPEN", "false")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FALLBACK", "hash")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://embedding.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "cache-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "provider-chain-pending-cache.db"))
    await client.init_db()

    call_counter = {"value": 0}

    async def _fake_post_json(*_args, **_kwargs):
        call_counter["value"] += 1
        return {"data": [{"embedding": [0.5] * client._embedding_dim}]}

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    async with client.session() as session:
        first = await client._get_embedding(session, "provider chain pending cache sample")
        second = await client._get_embedding(session, "provider chain pending cache sample")
        await session.flush()

    await client.close()

    assert first == [0.5] * client._embedding_dim
    assert second == [0.5] * client._embedding_dim
    assert call_counter["value"] == 1


@pytest.mark.asyncio
async def test_get_embedding_ignores_embedding_cache_lock_without_poisoning_caller_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("EMBEDDING_PROVIDER_CHAIN_ENABLED", "false")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://embedding.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "lock-safe-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "provider-chain-cache-lock.db"))
    await client.init_db()

    async def _fake_post_json(*_args, **_kwargs):
        return {"data": [{"embedding": [0.9] * client._embedding_dim}]}

    async def _locked_upsert(*_args: Any, **_kwargs: Any):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    monkeypatch.setattr(client, "_upsert_embedding_cache", _locked_upsert)

    async with client.session() as session:
        embedding = await client._get_embedding(session, "provider chain cache lock sample")
        probe = await session.execute(text("SELECT 1"))

    await client.close()

    assert embedding == [0.9] * client._embedding_dim
    assert probe.scalar() == 1


@pytest.mark.asyncio
async def test_remote_embedding_cache_namespace_includes_requested_dimension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "api")
    monkeypatch.setenv("EMBEDDING_PROVIDER_CHAIN_ENABLED", "false")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://embedding.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "test-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "shared-model")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_DIM", "16")

    db_path = tmp_path / "provider-cache-dim-namespace.db"
    call_counter = {"value": 0}

    first_client = SQLiteClient(_sqlite_url(db_path))
    await first_client.init_db()

    async def _fake_post_json_first(*_args, **_kwargs):
        call_counter["value"] += 1
        return {"data": [{"embedding": [0.16] * first_client._embedding_dim}]}

    monkeypatch.setattr(first_client, "_post_json", _fake_post_json_first)
    async with first_client.session() as session:
        first = await first_client._get_embedding(session, "dimension namespace sample")
        await session.flush()
    await first_client.close()

    monkeypatch.setenv("RETRIEVAL_EMBEDDING_DIM", "32")
    second_client = SQLiteClient(_sqlite_url(db_path))
    await second_client.init_db()

    async def _fake_post_json_second(*_args, **_kwargs):
        call_counter["value"] += 1
        return {"data": [{"embedding": [0.32] * second_client._embedding_dim}]}

    monkeypatch.setattr(second_client, "_post_json", _fake_post_json_second)
    async with second_client.session() as session:
        second = await second_client._get_embedding(session, "dimension namespace sample")
        await session.flush()
        cache_count = await session.scalar(text("SELECT COUNT(*) FROM embedding_cache"))
    await second_client.close()

    assert first == [0.16] * 16
    assert second == [0.32] * 32
    assert call_counter["value"] == 2
    assert cache_count == 2


@pytest.mark.asyncio
async def test_embedding_provider_chain_cache_tracks_actual_provider_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_embedding_env(monkeypatch)
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_BACKEND", "router")
    monkeypatch.setenv("EMBEDDING_PROVIDER_CHAIN_ENABLED", "true")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FAIL_OPEN", "false")
    monkeypatch.setenv("EMBEDDING_PROVIDER_FALLBACK", "api")
    monkeypatch.setenv("ROUTER_API_BASE", "https://router.example/v1")
    monkeypatch.setenv("ROUTER_API_KEY", "router-key")
    monkeypatch.setenv("ROUTER_EMBEDDING_MODEL", "router-model")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_BASE", "https://api.example/v1")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_API_KEY", "api-key")
    monkeypatch.setenv("RETRIEVAL_EMBEDDING_MODEL", "api-model")

    client = SQLiteClient(_sqlite_url(tmp_path / "provider-chain-cache-namespace.db"))
    await client.init_db()

    state = {"router_up": False}

    async def _fake_post_json(base: str, endpoint: str, payload, api_key: str = ""):
        _ = endpoint
        _ = api_key
        if base == "https://router.example/v1":
            if not state["router_up"]:
                return None
            assert payload["model"] == "router-model"
            assert payload["dimensions"] == client._embedding_dim
            return {"data": [{"embedding": [0.91] * client._embedding_dim}]}
        assert base == "https://api.example/v1"
        assert payload["model"] == "api-model"
        assert payload["dimensions"] == client._embedding_dim
        return {"data": [{"embedding": [0.11] * client._embedding_dim}]}

    monkeypatch.setattr(client, "_post_json", _fake_post_json)
    async with client.session() as session:
        first = await client._get_embedding(
            session, "provider namespace separation sample"
        )
        await session.flush()
        state["router_up"] = True
        second = await client._get_embedding(
            session, "provider namespace separation sample"
        )
    await client.close()

    assert first == [0.11] * client._embedding_dim
    assert second == [0.91] * client._embedding_dim


@pytest.mark.asyncio
async def test_post_json_reuses_async_http_client_and_closes_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = SQLiteClient(_sqlite_url(tmp_path / "remote-http-client-pool.db"))
    created_clients: list["_FakeAsyncClient"] = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"ok": True}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            _ = args
            _ = kwargs
            self.post_calls: list[dict[str, object]] = []
            self.closed = False
            created_clients.append(self)

        async def post(self, url: str, **kwargs):
            self.post_calls.append({"url": url, **kwargs})
            return _FakeResponse()

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    first = await client._post_json(
        "https://api.example/v1",
        "/embeddings",
        {"model": "demo", "input": "first"},
        "demo-key",
        timeout_sec=3.0,
    )
    second = await client._post_json(
        "https://api.example/v1",
        "/rerank",
        {"model": "demo", "input": "second"},
        "demo-key",
        timeout_sec=5.0,
    )

    await client.close()

    assert first == {"ok": True}
    assert second == {"ok": True}
    assert len(created_clients) == 1
    assert len(created_clients[0].post_calls) == 2
    assert created_clients[0].closed is True
