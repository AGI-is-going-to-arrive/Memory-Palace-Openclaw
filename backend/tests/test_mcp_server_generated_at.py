import pytest

import mcp_server


@pytest.mark.asyncio
async def test_generate_memory_index_view_uses_utc_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def _fake_impl(*, client, generated_at: str, default_domain: str, make_uri):
        _ = client
        _ = default_domain
        _ = make_uri
        captured["generated_at"] = generated_at
        return "ok"

    monkeypatch.setattr(mcp_server, "_generate_memory_index_view_impl", _fake_impl)
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: object())

    result = await mcp_server._generate_memory_index_view()

    assert result == "ok"
    assert captured["generated_at"].endswith("Z")


@pytest.mark.asyncio
async def test_generate_recent_memories_view_uses_utc_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    async def _fake_impl(*, client, generated_at: str, limit: int):
        _ = client
        _ = limit
        captured["generated_at"] = generated_at
        return "ok"

    monkeypatch.setattr(mcp_server, "_generate_recent_memories_view_impl", _fake_impl)
    monkeypatch.setattr(mcp_server, "get_sqlite_client", lambda: object())

    result = await mcp_server._generate_recent_memories_view(limit=3)

    assert result == "ok"
    assert captured["generated_at"].endswith("Z")
