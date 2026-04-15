import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path as FilePath
from typing import Optional
from urllib.parse import unquote

_SQLITE_ADAPTERS_REGISTERED = False
_SQLITE_FILE_PREFIXES = ("sqlite+aiosqlite:///", "sqlite:///")
_RESERVED_MEMORY_PATH_SEGMENTS = {"__root__"}


def _register_sqlite_adapters() -> None:
    """
    Register explicit sqlite adapters for Python datetime objects.

    Python 3.12+ deprecates sqlite3's implicit default datetime adapter.
    Registering our own adapter removes deprecation noise and keeps behavior stable.
    """
    global _SQLITE_ADAPTERS_REGISTERED
    if _SQLITE_ADAPTERS_REGISTERED:
        return
    sqlite3.register_adapter(datetime, lambda value: value.isoformat(sep=" "))
    _SQLITE_ADAPTERS_REGISTERED = True


def _utc_now() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def _utc_now_naive() -> datetime:
    """Naive UTC datetime for existing DB schema compatibility."""
    return _utc_now().replace(tzinfo=None)


def _normalize_sqlite_file_path(raw_path: str) -> str:
    value = str(raw_path or "").strip()
    if not value:
        return ""
    if re.match(r"^/[a-zA-Z]:[/\\]", value):
        value = value[1:]
    if os.name == "nt":
        value = value.replace("\\", "/")
    return value


def _split_sqlite_url_suffix(raw_path: str) -> tuple[str, str]:
    path_part = str(raw_path or "")
    query = ""
    fragment = ""

    if "#" in path_part:
        path_part, fragment = path_part.split("#", 1)
    if "?" in path_part:
        path_part, query = path_part.split("?", 1)
    elif fragment and "?" in fragment:
        fragment, query = fragment.split("?", 1)

    suffix = ""
    if query:
        suffix += f"?{query}"
    if fragment:
        suffix += f"#{fragment}"
    return path_part, suffix


def _normalize_sqlite_database_url(database_url: str) -> str:
    if not isinstance(database_url, str):
        return database_url
    for prefix in _SQLITE_FILE_PREFIXES:
        if not database_url.startswith(prefix):
            continue
        raw_path, suffix = _split_sqlite_url_suffix(database_url[len(prefix):])
        normalized_path = _normalize_sqlite_file_path(unquote(raw_path))
        if not normalized_path:
            return database_url
        return f"{prefix}{normalized_path}{suffix}"
    return database_url


def extract_sqlite_file_path(database_url: str) -> Optional[FilePath]:
    database_url = _normalize_sqlite_database_url(database_url)
    if not isinstance(database_url, str):
        return None
    for prefix in _SQLITE_FILE_PREFIXES:
        if not database_url.startswith(prefix):
            continue
        raw_path = database_url[len(prefix):]
        raw_path = raw_path.split("?", 1)[0].split("#", 1)[0]
        raw_path = _normalize_sqlite_file_path(raw_path)
        if not raw_path:
            return None
        if raw_path == ":memory:" or raw_path.startswith("file::memory:"):
            return None
        if raw_path.startswith("/") or (
            len(raw_path) >= 3 and raw_path[1] == ":" and raw_path[2] == "/"
        ):
            return FilePath(raw_path)
        return FilePath(raw_path)
    return None


def _extract_sqlite_file_path(database_url: str) -> Optional[FilePath]:
    return extract_sqlite_file_path(database_url)


def _resolve_init_lock_path(database_file: Optional[FilePath]) -> Optional[FilePath]:
    if database_file is None:
        return None
    if database_file.suffix:
        return database_file.with_suffix(f"{database_file.suffix}.init.lock")
    return FilePath(f"{database_file}.init.lock")


def is_valid_memory_path_segment(value: str) -> bool:
    candidate = unicodedata.normalize("NFC", str(value or "").strip())
    return (
        bool(candidate)
        and candidate not in {".", ".."}
        and candidate not in _RESERVED_MEMORY_PATH_SEGMENTS
        and not candidate.startswith(".")
        and not candidate.endswith(".")
        and ".." not in candidate
        and all(character.isalnum() or character in {"_", "-", "."} for character in candidate)
    )


def memory_path_segment_error_message() -> str:
    return (
        "Title must only contain letters or numbers (including Unicode letters), "
        "underscores, hyphens, or dots. Dots cannot appear at the start or end "
        "of a segment, and cannot appear twice in a row. Reserved names are not allowed."
    )
