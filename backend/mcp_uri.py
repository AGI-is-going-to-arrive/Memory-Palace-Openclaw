import re
import unicodedata
from typing import Iterable, Optional, Tuple
from urllib.parse import unquote


_INVALID_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"^[a-zA-Z]:($|/)")
MAX_URI_PATH_LENGTH = 2048
MAX_URI_DEPTH = 128


def _normalize_uri_component(value: str) -> str:
    return unicodedata.normalize("NFC", str(value or ""))


def _decode_uri_component(value: str, *, field_name: str) -> str:
    rendered = str(value or "")
    if _INVALID_PERCENT_ESCAPE_PATTERN.search(rendered):
        raise ValueError(f"URI {field_name} contains invalid percent escapes.")
    try:
        decoded = unquote(rendered, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"URI {field_name} contains invalid UTF-8 escapes.") from exc
    return _normalize_uri_component(decoded)


def _validate_uri_path(path: str) -> str:
    rendered = (
        _decode_uri_component(path, field_name="path")
        .replace("\\", "/")
        .strip()
        .strip("/")
    )
    if not rendered:
        return ""
    if "\x00" in rendered:
        raise ValueError("URI path contains invalid characters.")
    if _WINDOWS_ABSOLUTE_PATH_PATTERN.match(rendered):
        raise ValueError("URI path looks like a Windows absolute path.")
    if len(rendered) > MAX_URI_PATH_LENGTH:
        raise ValueError(
            f"URI path is too long ({len(rendered)} > {MAX_URI_PATH_LENGTH})."
        )

    segments = rendered.split("/")
    if any(not segment for segment in segments):
        raise ValueError("URI path contains empty segments.")
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("URI path contains invalid traversal segments.")
    if len(segments) > MAX_URI_DEPTH:
        raise ValueError(
            f"URI path is too deep ({len(segments)} > {MAX_URI_DEPTH})."
        )
    return rendered


def parse_uri(
    uri: str,
    *,
    valid_domains: Iterable[str],
    default_domain: str,
) -> Tuple[str, str]:
    """
    Parse a memory URI into (domain, path).

    Supported formats:
    - "core://agent"      -> ("core", "agent")
    - "writer://chapter"  -> ("writer", "chapter")
    - "memory-palace"     -> ("core", "memory-palace") [legacy fallback]
    """
    rendered = _normalize_uri_component(uri).strip()
    if not rendered:
        raise ValueError("URI must not be empty.")
    if "://" in rendered:
        raw_domain, raw_path = rendered.split("://", 1)
        domain = _decode_uri_component(raw_domain, field_name="domain").strip().lower()
        path = _validate_uri_path(raw_path)
        if domain not in set(valid_domains):
            raise ValueError(
                f"Unknown domain '{domain}'. Valid domains: {', '.join(valid_domains)}"
            )
        return (domain, path)

    return (default_domain, _validate_uri_path(rendered))


def make_uri(domain: str, path: str) -> str:
    """Create a URI from domain and path."""
    normalized_domain = str(domain or "").strip().lower()
    if not normalized_domain:
        raise ValueError("make_uri requires a non-empty domain.")
    return f"{normalized_domain}://{path}"


def validate_writable_domain(
    domain: str,
    *,
    read_only_domains: Iterable[str],
    operation: str,
    uri: Optional[str] = None,
) -> None:
    normalized = str(domain or "").strip().lower()
    read_only = set(read_only_domains)
    if normalized in read_only:
        target = str(uri or f"{normalized}://").strip()
        raise ValueError(
            f"{operation} does not allow writes to '{target}'. "
            "system:// is read-only and reserved for built-in views."
        )
