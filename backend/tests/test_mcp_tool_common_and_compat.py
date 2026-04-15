from __future__ import annotations

import pytest

from mcp_client_compat import try_client_method_variants_impl
from mcp_tool_common import trim_sentence_impl


def test_trim_sentence_impl_never_exceeds_small_limit() -> None:
    assert trim_sentence_impl("abcdefghij", limit=9) == "abcdef..."
    assert len(trim_sentence_impl("abcdefghij", limit=9)) <= 9


def test_trim_sentence_impl_falls_back_to_hard_cut_for_tiny_limit() -> None:
    assert trim_sentence_impl("abcdefghi", limit=3) == "abc"
    assert trim_sentence_impl("abcdefghi", limit=1) == "a"
    assert trim_sentence_impl("abcdefghi", limit=0) == ""


@pytest.mark.asyncio
async def test_try_client_method_variants_falls_back_after_not_implemented_error() -> None:
    class _Client:
        def primary(self, **_kwargs):
            raise NotImplementedError("primary variant is unavailable on this client")

        async def fallback(self, **kwargs):
            return {"ok": True, "kwargs": kwargs}

    method_name, used_kwargs, result = await try_client_method_variants_impl(
        _Client(),
        ["primary", "fallback"],
        [{"probe": 1}],
        continue_on_none=False,
        is_signature_mismatch=lambda exc: False,
    )

    assert method_name == "fallback"
    assert used_kwargs == {"probe": 1}
    assert result == {"ok": True, "kwargs": {"probe": 1}}


@pytest.mark.asyncio
async def test_try_client_method_variants_reuses_cached_successful_variant() -> None:
    class _Client:
        def __init__(self) -> None:
            self.primary_calls = 0
            self.fallback_calls = 0

        def primary(self, **_kwargs):
            self.primary_calls += 1
            raise NotImplementedError("primary variant is unavailable on this client")

        async def fallback(self, **kwargs):
            self.fallback_calls += 1
            return {"ok": True, "kwargs": kwargs}

    client = _Client()

    first = await try_client_method_variants_impl(
        client,
        ["primary", "fallback"],
        [{"probe": 1}],
        continue_on_none=False,
        is_signature_mismatch=lambda exc: False,
    )
    second = await try_client_method_variants_impl(
        client,
        ["primary", "fallback"],
        [{"probe": 1}],
        continue_on_none=False,
        is_signature_mismatch=lambda exc: False,
    )

    assert first[0] == "fallback"
    assert second[0] == "fallback"
    assert client.primary_calls == 1
    assert client.fallback_calls == 2


@pytest.mark.asyncio
async def test_try_client_method_variants_does_not_swallow_real_attribute_errors() -> None:
    class _Client:
        def primary(self, **_kwargs):
            raise AttributeError("boom")

    with pytest.raises(AttributeError, match="boom"):
        await try_client_method_variants_impl(
            _Client(),
            ["primary"],
            [{"probe": 1}],
            continue_on_none=False,
            is_signature_mismatch=lambda exc: False,
        )


@pytest.mark.asyncio
async def test_try_client_method_variants_does_not_swallow_non_signature_type_errors() -> None:
    class _Client:
        def primary(self, **_kwargs):
            raise TypeError("database is locked")

    with pytest.raises(TypeError, match="database is locked"):
        await try_client_method_variants_impl(
            _Client(),
            ["primary"],
            [{"probe": 1}],
            continue_on_none=False,
            is_signature_mismatch=lambda exc: False,
        )
