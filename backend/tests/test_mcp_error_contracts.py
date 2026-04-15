import asyncio
import json
from pathlib import Path

import pytest

import mcp_server
import mcp_tool_write_runtime
from db.sqlite_paths import (
    _normalize_sqlite_database_url,
    extract_sqlite_file_path,
    is_valid_memory_path_segment,
    memory_path_segment_error_message,
)


class _MissingMemoryClient:
    async def get_memory_by_path(self, _path: str, _domain: str):
        return None


class _FourthRangeVariantClient:
    async def read_memory_segment(
        self,
        *,
        uri: str,
        chunk_id: int | None = None,
        range: str | None = None,
        max_chars: int | None = None,
    ):
        assert uri == "core://agent/index"
        assert chunk_id is None
        assert range == "1:4"
        assert max_chars is None
        return {
            "id": 17,
            "content": "bcd",
            "selection": [1, 4],
        }

    async def get_memory_by_path(self, _path: str, _domain: str):
        return {
            "id": 17,
            "content": "abcd",
            "priority": 3,
            "created_at": "2026-02-15T00:00:00Z",
        }


class _FourthRangeVariantClientWithMetadata(_FourthRangeVariantClient):
    async def get_memory_by_path(self, _path: str, _domain: str):
        return {
            "id": 17,
            "content": "abcd",
            "priority": 7,
            "created_at": "2026-02-16T12:00:00Z",
        }


class _NoWriteClient:
    async def write_guard(self, **_kwargs):  # pragma: no cover - should never be called
        raise AssertionError("write_guard should not be called for system:// writes")

    async def get_memory_by_path(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("get_memory_by_path should not be called for system:// writes")

    async def remove_path(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("remove_path should not be called for system:// writes")

    async def add_path(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("add_path should not be called for system:// writes")


class _DuplicatePatchMemoryClient:
    async def get_memory_by_path(self, _path: str, _domain: str):
        return {"id": 1, "content": "same same"}


class _RuntimeStateStub:
    def __init__(self) -> None:
        self.started = 0

    async def ensure_started(self, _get_sqlite_client) -> None:
        self.started += 1


class _SearchClientWithOneHit:
    def preprocess_query(self, query: str):
        return {
            "original_query": query,
            "normalized_query": query,
            "rewritten_query": query,
            "tokens": [],
            "changed": False,
        }

    def classify_intent(self, _query: str, _rewritten_query: str):
        return {
            "intent": "factual",
            "strategy_template": "factual_high_precision",
            "method": "rule",
            "confidence": 0.8,
            "signals": ["default_factual"],
        }

    async def search_advanced(self, **_kwargs):
        return {
            "mode": "hybrid",
            "degraded": False,
            "degrade_reasons": [],
            "results": [
                {
                    "uri": "core://agent/index",
                    "memory_id": 11,
                    "snippet": "Index rebuilt last night.",
                    "priority": 1,
                    "updated_at": "2026-02-16T12:00:00Z",
                    "metadata": {
                        "domain": "core",
                        "path": "agent/index",
                        "priority": 1,
                        "updated_at": "2026-02-16T12:00:00Z",
                    },
                }
            ],
        }


class _SearchClientWithEmbeddingFallback(_SearchClientWithOneHit):
    async def search_advanced(self, **_kwargs):
        payload = await super().search_advanced(**_kwargs)
        payload["degraded"] = True
        payload["degrade_reasons"] = [
            "embedding_request_failed",
            "embedding_fallback_hash",
        ]
        return payload


@pytest.mark.asyncio
async def test_read_memory_partial_validation_errors_return_json() -> None:
    raw = await mcp_server.read_memory("core://agent/index", chunk_id=-1)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "chunk_id must be >= 0" in payload["error"]


@pytest.mark.asyncio
async def test_read_memory_rejects_chunk_id_and_range_combination() -> None:
    raw = await mcp_server.read_memory(
        "core://agent/index",
        chunk_id=0,
        range="0:32",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "chunk_id and range cannot be used together" in payload["error"]


def test_root_uri_roundtrip_does_not_introduce_extra_slashes() -> None:
    domain, path = mcp_server.parse_uri("core://")

    assert domain == "core"
    assert path == ""
    assert mcp_server.make_uri(domain, path) == "core://"


def test_parse_uri_rejects_empty_legacy_uri() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        mcp_server.parse_uri("")


def test_make_uri_normalizes_domain_case() -> None:
    assert mcp_server.make_uri("Core", "agent/index") == "core://agent/index"


def test_make_uri_rejects_empty_domain() -> None:
    with pytest.raises(ValueError, match="non-empty domain"):
        mcp_server.make_uri("", "agent/index")


@pytest.mark.parametrize(
    "uri",
    [
        "core://../../etc/passwd",
        "core://%2e%2e/etc/passwd",
        "core://agent/%2e%2e/admin",
        "core://..%2fetc/passwd",
        "core://agent/%zz/admin",
        "core://agent//child",
        "../etc/passwd",
        "C:\\Users\\tester",
    ],
)
def test_parse_uri_rejects_invalid_path_segments(uri: str) -> None:
    with pytest.raises(ValueError):
        mcp_server.parse_uri(uri)


def test_parse_uri_normalizes_unicode_path_segments() -> None:
    domain, path = mcp_server.parse_uri("core://caf\u0065\u0301")

    assert domain == "core"
    assert path == "café"


def test_parse_uri_normalizes_windows_backslashes_in_path() -> None:
    domain, path = mcp_server.parse_uri(r"core://agent\child")

    assert domain == "core"
    assert path == "agent/child"


def test_parse_uri_rejects_excessive_depth_and_length() -> None:
    with pytest.raises(ValueError, match="too deep"):
        mcp_server.parse_uri("core://" + "/".join(["segment"] * 129))
    with pytest.raises(ValueError, match="too long"):
        mcp_server.parse_uri("core://" + ("a" * 2049))


def test_extract_sqlite_file_path_preserves_literal_percent_sequences() -> None:
    assert extract_sqlite_file_path("sqlite:///path%2520to%2520db") == Path(
        "path%20to%20db"
    )
    assert extract_sqlite_file_path("sqlite:///my%252Fdb") == Path("my%2Fdb")


def test_local_priority_filter_reports_non_matching_priority_drop() -> None:
    filtered, reasons = mcp_server._apply_local_filters_to_results(
        [{"uri": "core://agent/index", "priority": "high"}],
        {"max_priority": 5},
    )

    assert filtered == []
    assert reasons == [
        "max_priority filter dropped 1 result(s) with missing/non-matching priority."
    ]


def test_local_updated_after_filter_drops_missing_and_invalid_timestamps() -> None:
    filtered, reasons = mcp_server._apply_local_filters_to_results(
        [
            {"uri": "core://agent/missing"},
            {"uri": "core://agent/invalid", "updated_at": "not-a-timestamp"},
            {"uri": "core://agent/old", "updated_at": "2026-01-01T00:00:00Z"},
            {"uri": "core://agent/new", "updated_at": "2026-03-01T00:00:00Z"},
        ],
        {"updated_after": "2026-02-01T00:00:00Z"},
    )

    assert [item["uri"] for item in filtered] == ["core://agent/new"]
    assert reasons == [
        "updated_after filter dropped 2 result(s) with missing/non-parseable updated_at.",
        "updated_after filter dropped 3 result(s).",
    ]


@pytest.mark.asyncio
async def test_read_memory_partial_not_found_returns_json(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _MissingMemoryClient())

    raw = await mcp_server.read_memory("core://agent/missing", chunk_id=0)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "not found" in payload["error"]


@pytest.mark.asyncio
async def test_read_memory_partial_range_uses_fourth_fallback_variant(monkeypatch) -> None:
    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        mcp_server,
        "get_sqlite_client",
        lambda: _FourthRangeVariantClient(),
    )
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.read_memory("core://agent/index", range="1:4")
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["backend_method"] == "sqlite_client.read_memory_segment"
    assert payload["content"] == "bcd"
    assert payload["selection"]["mode"] == "sqlite_char_range"
    assert payload["selection"]["start"] == 1
    assert payload["selection"]["end"] == 4


@pytest.mark.asyncio
async def test_read_memory_partial_reports_follow_up_recording_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_follow_up(*_args, **_kwargs) -> None:
        raise RuntimeError("follow-up failure")

    monkeypatch.setattr(
        mcp_server,
        "get_sqlite_client",
        lambda: _FourthRangeVariantClient(),
    )
    monkeypatch.setattr(mcp_server, "_record_session_hit", _raise_follow_up)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _raise_follow_up)

    raw = await mcp_server.read_memory("core://agent/index", range="1:4")
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["degraded"] is True
    assert "record_session_hit_failed" in payload["degrade_reasons"]
    assert "record_flush_event_failed" in payload["degrade_reasons"]


@pytest.mark.asyncio
async def test_read_memory_partial_records_priority_and_timestamp_from_memory_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded_calls: list[dict[str, object]] = []

    async def _record_session_hit(**kwargs) -> None:
        recorded_calls.append(kwargs)

    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        mcp_server,
        "get_sqlite_client",
        lambda: _FourthRangeVariantClient(),
    )
    monkeypatch.setattr(mcp_server, "_record_session_hit", _record_session_hit)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.read_memory("core://agent/index", range="1:4")
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert recorded_calls == [
        {
            "uri": "core://agent/index",
            "memory_id": 17,
            "snippet": "bcd",
            "priority": 3,
            "source": "read_memory_partial",
            "updated_at": "2026-02-15T00:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_search_memory_reports_follow_up_recording_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_follow_up(*_args, **_kwargs) -> None:
        raise RuntimeError("follow-up failure")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _SearchClientWithOneHit())
    monkeypatch.setattr(mcp_server, "_record_session_hit", _raise_follow_up)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _raise_follow_up)

    raw = await mcp_server.search_memory("agent index", mode="hybrid", max_results=3)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["degraded"] is True
    assert "record_session_hit_failed" in payload["degrade_reasons"]
    assert "record_flush_event_failed" in payload["degrade_reasons"]


@pytest.mark.asyncio
async def test_search_memory_surfaces_semantic_search_unavailable_on_hash_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        mcp_server, "get_sqlite_client", lambda: _SearchClientWithEmbeddingFallback()
    )
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory("agent index", mode="hybrid", max_results=3)
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["degraded"] is True
    assert payload["semantic_search_unavailable"] is True
    assert "embedding_fallback_hash" in payload["degrade_reasons"]


@pytest.mark.asyncio
async def test_search_memory_internal_errors_do_not_leak_secret_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingSearchClient(_SearchClientWithOneHit):
        async def search_advanced(self, **_kwargs):
            raise RuntimeError("Authorization: Bearer super-secret")

    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _ExplodingSearchClient())
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)

    raw = await mcp_server.search_memory("agent index", mode="hybrid", max_results=3)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "super-secret" not in payload["error"]
    assert "search_memory failed" in payload["error"]


@pytest.mark.asyncio
async def test_compact_context_internal_errors_do_not_leak_secret_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CompactClient:
        def __init__(self, database_url: str) -> None:
            self.database_url = database_url

    async def _raise_flush(**_kwargs):
        raise RuntimeError("Authorization: Bearer super-secret")

    async def _run_write_inline(_operation: str, task):
        return await task()

    monkeypatch.setattr(
        mcp_server,
        "get_sqlite_client",
        lambda: _CompactClient("sqlite+aiosqlite:///tmp/compact-context.db"),
    )
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)
    monkeypatch.setattr(mcp_server, "_flush_session_summary_to_memory", _raise_flush)
    mcp_server._AUTO_FLUSH_IN_PROGRESS.clear()

    raw = await mcp_server.compact_context(reason="unit_test", force=True, max_lines=5)
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "super-secret" not in payload["error"]
    assert "compact_context failed" in payload["error"]


@pytest.mark.asyncio
async def test_update_memory_identical_patch_returns_tool_response_json() -> None:
    raw = await mcp_server.update_memory(
        uri="core://agent/index",
        old_string="same-content",
        new_string="same-content",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["updated"] is False
    assert "identical" in payload["message"]


@pytest.mark.asyncio
async def test_delete_memory_returns_structured_success_payload(monkeypatch) -> None:
    class _DeleteClient:
        async def get_memory_by_path(self, _path: str, _domain: str = "core"):
            return {
                "id": 1,
                "content": "hello",
                "priority": 1,
                "created_at": "2026-01-01T00:00:00Z",
            }

        async def remove_path(self, _path: str, _domain: str = "core"):
            return True

    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    async def _run_write_inline(_operation: str, task):
        return await task()

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _DeleteClient())
    monkeypatch.setattr(mcp_server, "_snapshot_path_delete", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server, "_maybe_auto_flush", _noop_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)

    raw = await mcp_server.delete_memory("core://agent/note")
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["deleted"] is True
    assert payload["uri"] == "core://agent/note"
    assert payload["message"] == "Success: Memory 'core://agent/note' deleted."


@pytest.mark.asyncio
async def test_delete_memory_returns_structured_not_found_payload(monkeypatch) -> None:
    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    async def _run_write_inline(_operation: str, task):
        return await task()

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _MissingMemoryClient())
    monkeypatch.setattr(mcp_server, "_snapshot_path_delete", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server, "_maybe_auto_flush", _noop_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)

    raw = await mcp_server.delete_memory("core://agent/missing")
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["deleted"] is False
    assert payload["uri"] == "core://agent/missing"
    assert payload["message"] == "Error: Memory at 'core://agent/missing' not found."


@pytest.mark.asyncio
async def test_delete_memory_write_lane_timeout_returns_structured_json(monkeypatch) -> None:
    async def _timeout_write_lane(_operation: str, _task):
        raise TimeoutError("write lane delete_memory timed out after 45s")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", object)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _timeout_write_lane)

    raw = await mcp_server.delete_memory("core://agent/slow")
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["deleted"] is False
    assert payload["uri"] == "core://agent/slow"
    assert payload["reason"] == "write_lane_timeout"
    assert payload["retryable"] is True
    assert payload["timeout_seconds"] == 45.0
    assert payload["retry_after_seconds"] == 45.0


@pytest.mark.asyncio
async def test_delete_memory_generic_exception_returns_structured_json(monkeypatch) -> None:
    async def _boom_write_lane(_operation: str, _task):
        raise RuntimeError("delete exploded")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", object)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _boom_write_lane)

    raw = await mcp_server.delete_memory("core://agent/broken")
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["deleted"] is False
    assert payload["message"] == "Error: delete_memory failed. Check server logs for details."


@pytest.mark.asyncio
async def test_add_alias_returns_legacy_success_string(monkeypatch) -> None:
    class _AliasClient:
        async def add_path(self, **_kwargs):
            return {
                "new_uri": "core://agent/alias",
                "target_uri": "core://agent/target",
                "memory_id": 7,
            }

    async def _noop_async(*_args, **_kwargs) -> None:
        return None

    async def _run_write_inline(_operation: str, task):
        return await task()

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _AliasClient())
    monkeypatch.setattr(mcp_server, "_snapshot_path_create", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_session_hit", _noop_async)
    monkeypatch.setattr(mcp_server, "_record_flush_event", _noop_async)
    monkeypatch.setattr(mcp_server, "_maybe_auto_flush", _noop_async)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _run_write_inline)

    raw = await mcp_server.add_alias("core://agent/alias", "core://agent/target", priority=1)

    assert (
        raw
        == "Success: Alias 'core://agent/alias' now points to same memory as "
        "'core://agent/target'"
    )


@pytest.mark.asyncio
async def test_update_memory_write_lane_timeout_returns_structured_json(
    monkeypatch,
) -> None:
    class _UpdateClient:
        async def get_memory_by_path(self, _path: str, _domain: str):
            return {"id": 1, "content": "seed"}

    async def _timeout_write_lane(_operation: str, _task):
        raise TimeoutError("write lane task timed out after 120s")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _UpdateClient())
    monkeypatch.setattr(mcp_server, "_run_write_lane", _timeout_write_lane)

    raw = await mcp_server.update_memory("core://agent/index", append=" more")
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["updated"] is False
    assert payload["reason"] == "write_lane_timeout"
    assert payload["retryable"] is True
    assert payload["timeout_seconds"] == 120.0
    assert payload["retry_after_seconds"] == 120.0


@pytest.mark.asyncio
async def test_add_alias_write_lane_timeout_returns_retryable_error_string(
    monkeypatch,
) -> None:
    async def _timeout_write_lane(_operation: str, _task):
        raise TimeoutError("write lane global acquire timed out after 30s")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", object)
    monkeypatch.setattr(mcp_server, "_run_write_lane", _timeout_write_lane)

    raw = await mcp_server.add_alias("core://agent/alias", "core://agent/target")

    assert raw.startswith("Error: write lane global acquire timed out after 30s")
    assert "retry" in raw.lower()


def test_unicode_memory_path_segments_are_allowed() -> None:
    assert is_valid_memory_path_segment("日本語メモ")
    assert is_valid_memory_path_segment("한국어")
    assert is_valid_memory_path_segment("café")
    assert is_valid_memory_path_segment("v1.2.3")
    assert is_valid_memory_path_segment("Node.js")
    assert not is_valid_memory_path_segment(".hidden")
    assert not is_valid_memory_path_segment("trailing.")
    assert not is_valid_memory_path_segment("release..candidate")
    assert "Unicode letters" in memory_path_segment_error_message()
    assert "twice in a row" in memory_path_segment_error_message()


@pytest.mark.asyncio
async def test_update_memory_duplicate_patch_requires_unique_old_string(monkeypatch) -> None:
    monkeypatch.setattr(
        mcp_server,
        "get_sqlite_client",
        lambda: _DuplicatePatchMemoryClient(),
    )

    raw = await mcp_server.update_memory(
        uri="core://agent/index",
        old_string="same",
        new_string="different",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["updated"] is False
    assert "found 2 times" in payload["message"]


@pytest.mark.asyncio
async def test_search_memory_rejects_non_string_query() -> None:
    raw = await mcp_server.search_memory(123)  # type: ignore[arg-type]
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert payload["error"] == "query must be a string."


@pytest.mark.asyncio
async def test_search_memory_invalid_mode_validated_before_db_init(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("should_not_init_db_for_invalid_mode")

    monkeypatch.setattr(mcp_server, "get_sqlite_client", _boom)

    raw = await mcp_server.search_memory("memory queue", mode="invalid-mode")
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "Invalid mode" in payload["error"]


@pytest.mark.asyncio
async def test_create_memory_rejects_system_domain_writes(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _NoWriteClient())

    raw = await mcp_server.create_memory(
        parent_uri="system://",
        content="blocked",
        priority=1,
        title="blocked",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "read-only" in payload["message"]


@pytest.mark.asyncio
async def test_update_memory_rejects_system_domain_writes() -> None:
    raw = await mcp_server.update_memory(
        uri="system://boot",
        append="\nblocked",
    )
    payload = json.loads(raw)

    assert payload["ok"] is False
    assert "read-only" in payload["message"]


@pytest.mark.asyncio
async def test_delete_memory_rejects_system_domain_writes(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _NoWriteClient())
    raw = await mcp_server.delete_memory("system://boot")
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["deleted"] is False
    assert "read-only" in payload["message"]


@pytest.mark.asyncio
async def test_add_alias_rejects_system_domain_writes(monkeypatch) -> None:
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: _NoWriteClient())
    raw = await mcp_server.add_alias("core://alias-node", "system://boot")
    assert raw.startswith("Error:")
    assert "read-only" in raw


def test_get_session_id_uses_request_aware_context(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeRequestContext:
        def __init__(self, session_obj, request_obj) -> None:
            self.session = session_obj
            self.request = request_obj

    class _FakeContext:
        def __init__(
            self,
            *,
            client_id: str,
            request_id: str,
            session_obj,
            request_obj,
        ) -> None:
            self._client_id = client_id
            self._request_id = request_id
            self._session_obj = session_obj
            self._request_context = _FakeRequestContext(session_obj, request_obj)

        @property
        def client_id(self):
            return self._client_id

        @property
        def request_id(self):
            return self._request_id

        @property
        def session(self):
            return self._session_obj

        @property
        def request_context(self):
            return self._request_context

    shared_session = object()
    ctx_a = _FakeContext(
        client_id="client-A",
        request_id="req-001",
        session_obj=shared_session,
        request_obj=object(),
    )
    ctx_b = _FakeContext(
        client_id="client-A",
        request_id="req-002",
        session_obj=shared_session,
        request_obj=object(),
    )

    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: ctx_a)
    session_a = mcp_server.get_session_id()
    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: ctx_b)
    session_b = mcp_server.get_session_id()

    assert session_a.startswith("mcp_ctx_")
    assert session_b.startswith("mcp_ctx_")
    assert session_a != session_b


def test_get_runtime_session_id_stays_stable_across_request_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRequestContext:
        def __init__(self, session_obj, request_obj) -> None:
            self.session = session_obj
            self.request = request_obj

    class _FakeSession:
        def __init__(self, session_id: str, connection_id: str) -> None:
            self.session_id = session_id
            self.connection_id = connection_id

    class _FakeContext:
        def __init__(
            self,
            *,
            client_id: str,
            request_id: str,
            session_obj,
            request_obj,
        ) -> None:
            self._client_id = client_id
            self._request_id = request_id
            self._session_obj = session_obj
            self._request_context = _FakeRequestContext(session_obj, request_obj)

        @property
        def client_id(self):
            return self._client_id

        @property
        def request_id(self):
            return self._request_id

        @property
        def session(self):
            return self._session_obj

        @property
        def request_context(self):
            return self._request_context

    shared_session = _FakeSession("shared-session", "connection-1")
    ctx_a = _FakeContext(
        client_id="client-A",
        request_id="req-001",
        session_obj=shared_session,
        request_obj=object(),
    )
    ctx_b = _FakeContext(
        client_id="client-A",
        request_id="req-002",
        session_obj=shared_session,
        request_obj=object(),
    )

    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: ctx_a)
    runtime_a = mcp_server.get_runtime_session_id()
    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: ctx_b)
    runtime_b = mcp_server.get_runtime_session_id()

    assert runtime_a.startswith("mcp_rt_")
    assert runtime_b.startswith("mcp_rt_")
    assert runtime_a == runtime_b


@pytest.mark.asyncio
async def test_run_write_lane_serializes_writes_when_queue_disabled() -> None:
    runtime_state = _RuntimeStateStub()
    order: list[str] = []
    holder_ready = asyncio.Event()
    release_holder = asyncio.Event()

    async def _holder() -> str:
        order.append("holder:start")
        holder_ready.set()
        await release_holder.wait()
        order.append("holder:end")
        return "holder"

    async def _waiter() -> str:
        order.append("waiter:start")
        return "waiter"

    holder_task = asyncio.create_task(
        mcp_tool_write_runtime.run_write_lane_impl(
            runtime_state=runtime_state,
            get_sqlite_client=lambda: object(),
            get_session_id=lambda: "session-a",
            enable_write_lane_queue=False,
            operation="holder",
            fn=_holder,
        )
    )
    await holder_ready.wait()

    waiter_task = asyncio.create_task(
        mcp_tool_write_runtime.run_write_lane_impl(
            runtime_state=runtime_state,
            get_sqlite_client=lambda: object(),
            get_session_id=lambda: "session-b",
            enable_write_lane_queue=False,
            operation="waiter",
            fn=_waiter,
        )
    )
    await asyncio.sleep(0)
    assert order == ["holder:start"]

    release_holder.set()
    assert await holder_task == "holder"
    assert await waiter_task == "waiter"
    assert order == ["holder:start", "holder:end", "waiter:start"]
    assert runtime_state.started == 2


def test_get_session_id_falls_back_when_context_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_context_error():
        raise RuntimeError("context unavailable")

    monkeypatch.setattr(mcp_server.mcp, "get_context", _raise_context_error)
    assert mcp_server.get_session_id() == mcp_server._SESSION_ID


def test_get_session_id_uses_request_shape_when_request_id_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeRequest:
        def __init__(self, method: str, path: str) -> None:
            self.method = method
            self.path = path

    class _FakeRequestContext:
        def __init__(self, request: object) -> None:
            self.request = request
            self.session = object()

    class _FakeContext:
        def __init__(self, client_id: str, request: object) -> None:
            self.client_id = client_id
            self.request_id = None
            self.session = object()
            self.request_context = _FakeRequestContext(request)

    ctx_a = _FakeContext("client-A", _FakeRequest("POST", "/memory/a"))
    ctx_b = _FakeContext("client-A", _FakeRequest("POST", "/memory/b"))

    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: ctx_a)
    session_a = mcp_server.get_session_id()
    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: ctx_b)
    session_b = mcp_server.get_session_id()

    assert session_a.startswith("mcp_ctx_")
    assert session_b.startswith("mcp_ctx_")
    assert session_a != session_b


def test_get_session_id_uses_stable_context_identifiers_without_object_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _StableSession:
        session_id = "shared-session"

    class _StableRequest:
        def __init__(self, request_id: str) -> None:
            self.request_id = request_id

    class _FakeRequestContext:
        def __init__(self, session_obj, request_obj) -> None:
            self.session = session_obj
            self.request = request_obj

    class _FakeContext:
        def __init__(self, request_obj) -> None:
            self.client_id = "client-A"
            self.request_id = ""
            self.session = _StableSession()
            self.request_context = _FakeRequestContext(self.session, request_obj)

    monkeypatch.setattr(
        mcp_server.mcp,
        "get_context",
        lambda: _FakeContext(_StableRequest("request-alpha")),
    )
    session_a = mcp_server.get_session_id()
    monkeypatch.setattr(
        mcp_server.mcp,
        "get_context",
        lambda: _FakeContext(_StableRequest("request-beta")),
    )
    session_b = mcp_server.get_session_id()

    assert session_a.startswith("mcp_ctx_")
    assert session_b.startswith("mcp_ctx_")
    assert "shared-session" in session_a
    assert "shared-session" in session_b
    assert session_a != session_b
