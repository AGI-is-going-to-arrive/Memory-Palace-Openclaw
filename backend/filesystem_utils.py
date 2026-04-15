from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_NETWORK_FILESYSTEM_TYPES = frozenset(
    {
        "nfs",
        "nfs4",
        "smbfs",
        "cifs",
        "sshfs",
        "fuse.sshfs",
    }
)
_WARNED_UNRELIABLE_LOCK_PATHS: set[tuple[str, str]] = set()


def _unescape_mount_path(value: str) -> str:
    return (
        str(value or "")
        .replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _normalize_match_path(value: str) -> str:
    normalized = os.path.normpath(str(value or "").strip() or os.sep)
    return normalized or os.sep


def _path_is_within(candidate_path: str, mount_point: str) -> bool:
    try:
        return os.path.commonpath([candidate_path, mount_point]) == mount_point
    except ValueError:
        return False


def _select_matching_filesystem_type(
    resolved_path: Path,
    entries: Iterable[tuple[str, str]],
) -> str | None:
    candidate_path = _normalize_match_path(str(resolved_path))
    best_match: str | None = None
    best_length = -1
    for mount_point, filesystem_type in entries:
        normalized_mount_point = _normalize_match_path(mount_point)
        if not _path_is_within(candidate_path, normalized_mount_point):
            continue
        if len(normalized_mount_point) <= best_length:
            continue
        best_match = str(filesystem_type or "").strip().lower() or None
        best_length = len(normalized_mount_point)
    return best_match


def _iter_proc_mount_entries(text: str) -> Iterable[tuple[str, str]]:
    for raw_line in str(text or "").splitlines():
        parts = raw_line.split()
        if len(parts) < 3:
            continue
        mount_point = _unescape_mount_path(parts[1])
        filesystem_type = str(parts[2] or "").strip().lower()
        if mount_point and filesystem_type:
            yield mount_point, filesystem_type


def _iter_mount_command_entries(text: str) -> Iterable[tuple[str, str]]:
    for raw_line in str(text or "").splitlines():
        if " on " not in raw_line or " (" not in raw_line:
            continue
        _, _, remainder = raw_line.partition(" on ")
        mount_point, _, tail = remainder.partition(" (")
        filesystem_type = tail.split(",", 1)[0].split(")", 1)[0].strip().lower()
        mount_point = _unescape_mount_path(mount_point.strip())
        if mount_point and filesystem_type:
            yield mount_point, filesystem_type


def _read_proc_mounts() -> str | None:
    for candidate in (
        Path("/proc/self/mounts"),
        Path("/proc/mounts"),
    ):
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return None


def _run_mount_command() -> str | None:
    try:
        completed = subprocess.run(
            ["mount"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.stdout else None


def detect_filesystem_type(
    path: Path,
    *,
    proc_mounts_text: str | None = None,
    mount_output: str | None = None,
) -> str | None:
    try:
        resolved_path = Path(path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        resolved_path = Path(path).expanduser()

    resolved_proc_mounts = proc_mounts_text if proc_mounts_text is not None else _read_proc_mounts()
    if resolved_proc_mounts:
        filesystem_type = _select_matching_filesystem_type(
            resolved_path,
            _iter_proc_mount_entries(resolved_proc_mounts),
        )
        if filesystem_type:
            return filesystem_type

    resolved_mount_output = mount_output if mount_output is not None else _run_mount_command()
    if resolved_mount_output:
        filesystem_type = _select_matching_filesystem_type(
            resolved_path,
            _iter_mount_command_entries(resolved_mount_output),
        )
        if filesystem_type:
            return filesystem_type

    return None


def is_probably_network_filesystem(path: Path) -> tuple[bool, str | None]:
    filesystem_type = detect_filesystem_type(path)
    return filesystem_type in _NETWORK_FILESYSTEM_TYPES, filesystem_type


def warn_if_unreliable_file_lock_path(
    path: Path | None,
    *,
    label: str,
    log: logging.Logger | None = None,
) -> tuple[bool, str | None]:
    if path is None:
        return False, None

    is_network_filesystem, filesystem_type = is_probably_network_filesystem(path)
    if not is_network_filesystem:
        return False, filesystem_type

    normalized_path = _normalize_match_path(str(path))
    warning_key = (str(label or "").strip() or "path", normalized_path)
    if warning_key not in _WARNED_UNRELIABLE_LOCK_PATHS:
        (log or logger).warning(
            "%s is on a network filesystem (%s); file locks may be unreliable. "
            "Prefer a local disk for database and shared lock/state files.",
            label,
            filesystem_type or "unknown",
        )
        _WARNED_UNRELIABLE_LOCK_PATHS.add(warning_key)
    return True, filesystem_type
