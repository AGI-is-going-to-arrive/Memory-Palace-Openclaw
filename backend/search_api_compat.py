from __future__ import annotations


ADVANCED_SEARCH_METHOD_NAMES = (
    "search_advanced",
    "search_memories",
    "search_memory",
    "search_with_filters",
    "search_v2",
)
LEGACY_SEARCH_METHOD_NAMES = ("search",)
SEARCH_METHOD_PRIORITY = ADVANCED_SEARCH_METHOD_NAMES + LEGACY_SEARCH_METHOD_NAMES


def search_api_kind(method_name: str | None) -> str:
    normalized = str(method_name or "").strip()
    if normalized in LEGACY_SEARCH_METHOD_NAMES:
        return "legacy_fallback"
    if normalized in ADVANCED_SEARCH_METHOD_NAMES:
        return "advanced"
    return "unknown"


def search_api_fallback_reason(method_name: str | None) -> str | None:
    normalized = str(method_name or "").strip()
    if normalized in LEGACY_SEARCH_METHOD_NAMES:
        return f"search_api_compat_fallback:{normalized}"
    return None
