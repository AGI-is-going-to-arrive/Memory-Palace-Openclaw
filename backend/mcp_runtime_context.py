import re
from typing import Any, Callable, Optional, Sequence


def safe_context_attr_impl(value: Any, name: str) -> Any:
    try:
        return getattr(value, name)
    except Exception:
        return None


def normalize_session_fragment_impl(
    value: Any,
    *,
    default: str,
    safe_pattern: re.Pattern[str],
    max_len: int = 24,
) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    safe = safe_pattern.sub("-", text).strip("-")
    if not safe:
        return default
    return safe[:max_len]


def _context_object_fragment(
    value: Any,
    *,
    safe_context_attr: Callable[[Any, str], Any],
    normalize_session_fragment: Callable[..., str],
    attributes: Sequence[str],
    default: str,
    max_len: int = 24,
) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for attribute in attributes:
        candidate = safe_context_attr(value, attribute)
        text = str(candidate or "").strip()
        if not text:
            continue
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        parts.append(text)
    if parts:
        return normalize_session_fragment(
            "|".join(parts),
            default=default,
            max_len=max_len,
        )
    return default


def build_context_session_id_impl(
    *,
    get_context: Callable[[], Any],
    safe_context_attr: Callable[[Any, str], Any],
    normalize_session_fragment: Callable[..., str],
) -> Optional[str]:
    try:
        ctx = get_context()
    except Exception:
        return None

    if ctx is None:
        return None

    client_id = safe_context_attr(ctx, "client_id")
    request_id = safe_context_attr(ctx, "request_id")
    session_obj = safe_context_attr(ctx, "session")
    request_context = safe_context_attr(ctx, "request_context")
    request_obj = None
    if request_context is not None:
        if session_obj is None:
            session_obj = safe_context_attr(request_context, "session")
        request_obj = safe_context_attr(request_context, "request")

    if (
        not client_id
        and not request_id
        and session_obj is None
        and request_obj is None
    ):
        return None

    client_fragment = normalize_session_fragment(client_id, default="client")
    session_fragment = _context_object_fragment(
        session_obj,
        safe_context_attr=safe_context_attr,
        normalize_session_fragment=normalize_session_fragment,
        attributes=("session_id", "client_id", "connection_id", "id", "identifier", "trace_id"),
        default="session",
    )
    request_seed = request_id
    request_fragment = (
        normalize_session_fragment(
            request_seed,
            default="request",
            max_len=32,
        )
        if request_seed
        else _context_object_fragment(
            request_obj,
            safe_context_attr=safe_context_attr,
            normalize_session_fragment=normalize_session_fragment,
            attributes=("request_id", "trace_id", "span_id", "method", "path", "url", "id", "identifier"),
            default="request",
            max_len=32,
        )
    )
    return f"mcp_ctx_{client_fragment}_{session_fragment}_{request_fragment}"


def build_runtime_session_id_impl(
    *,
    get_context: Callable[[], Any],
    safe_context_attr: Callable[[Any, str], Any],
    normalize_session_fragment: Callable[..., str],
) -> Optional[str]:
    try:
        ctx = get_context()
    except Exception:
        return None

    if ctx is None:
        return None

    client_id = safe_context_attr(ctx, "client_id")
    session_obj = safe_context_attr(ctx, "session")
    request_context = safe_context_attr(ctx, "request_context")

    if request_context is not None and session_obj is None:
        session_obj = safe_context_attr(request_context, "session")

    if not client_id and session_obj is None:
        return None

    client_fragment = normalize_session_fragment(client_id, default="client")
    session_fragment = _context_object_fragment(
        session_obj,
        safe_context_attr=safe_context_attr,
        normalize_session_fragment=normalize_session_fragment,
        attributes=("session_id", "client_id", "connection_id", "id", "identifier", "trace_id"),
        default="session",
    )
    return f"mcp_rt_{client_fragment}_{session_fragment}"
