import errno
import hashlib
import json
import logging
import math
import os
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, Sequence

from filelock import FileLock, Timeout
from env_utils import env_bool, env_csv, env_int
from filesystem_utils import warn_if_unreliable_file_lock_path


_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS = 1.0
_MAX_RATE_LIMIT_STATE_KEYS = 4096
_MAX_RATE_LIMIT_IDENTIFIER_BYTES = 128
logger = logging.getLogger(__name__)


def _normalize_extension(extension: str) -> str:
    value = str(extension or "").strip().lower()
    if not value:
        return ""
    if not value.startswith("."):
        value = f".{value}"
    return value


def _normalize_allowed_extensions(extensions: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for extension in extensions:
        value = _normalize_extension(extension)
        if value and value not in normalized:
            normalized.append(value)
    return tuple(normalized)


def _normalize_allowed_roots(roots: Sequence[str | Path]) -> tuple[Path, ...]:
    normalized: list[Path] = []
    for root in roots:
        raw = str(root or "").strip()
        if not raw:
            continue
        try:
            resolved = Path(raw).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved not in normalized:
            normalized.append(resolved)
    return tuple(normalized)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        value = float(default)
    else:
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            value = float(default)
    if not math.isfinite(value):
        value = float(default)
    return max(float(minimum), value)


def _stat_identity(file_stat: os.stat_result) -> Dict[str, int]:
    return {
        "device": int(getattr(file_stat, "st_dev", 0) or 0),
        "inode": int(getattr(file_stat, "st_ino", 0) or 0),
        "mtime_ns": int(getattr(file_stat, "st_mtime_ns", 0) or 0),
        "size_bytes": int(getattr(file_stat, "st_size", 0) or 0),
    }


def _absolute_path_without_symlink_resolution(path: Path) -> Path:
    return Path(os.path.abspath(str(path.expanduser())))


def _supports_secure_path_open() -> bool:
    return (
        os.name != "nt"
        and hasattr(os, "O_NOFOLLOW")
        and bool(getattr(os, "supports_dir_fd", None))
        and bool(getattr(os, "supports_follow_symlinks", None))
    )


def _lstat_component(name: str | Path, *, dir_fd: Optional[int] = None) -> os.stat_result:
    if dir_fd is None:
        return os.stat(name, follow_symlinks=False)
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _open_path_without_symlink_escape(
    candidate_path: Path,
    *,
    allowed_root: Optional[Path] = None,
) -> int:
    if not _supports_secure_path_open():
        _validate_insecure_platform_path(candidate_path, allowed_root=allowed_root)
        open_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        return os.open(candidate_path, open_flags)

    if allowed_root is None:
        raise OSError(errno.EPERM, "allowed_root is required for secure path open")

    relative_path = candidate_path.relative_to(allowed_root)
    path_parts = relative_path.parts
    if not path_parts:
        raise OSError(errno.EISDIR, "candidate path resolves to an allowed root")

    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    )
    current_fd = os.open(allowed_root, directory_flags)
    try:
        for component in path_parts[:-1]:
            component_stat = _lstat_component(component, dir_fd=current_fd)
            if stat.S_ISLNK(component_stat.st_mode):
                raise OSError(errno.ELOOP, "symlink path component is not allowed")
            if not stat.S_ISDIR(component_stat.st_mode):
                raise OSError(errno.ENOTDIR, "path component is not a directory")
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            current_stat = os.fstat(current_fd)
            if stat.S_ISLNK(current_stat.st_mode):
                raise OSError(errno.ELOOP, "symlink path component is not allowed")
            if not stat.S_ISDIR(current_stat.st_mode):
                raise OSError(errno.ENOTDIR, "path component is not a directory")

        final_stat = _lstat_component(path_parts[-1], dir_fd=current_fd)
        if stat.S_ISLNK(final_stat.st_mode):
            raise OSError(errno.ELOOP, "symlinks are not allowed")
        return os.open(
            path_parts[-1],
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
            dir_fd=current_fd,
        )
    finally:
        try:
            os.close(current_fd)
        except OSError:
            pass


def _is_path_within_root(path_value: Path, root: Path) -> bool:
    try:
        path_value.relative_to(root)
        return True
    except ValueError:
        return False


def _path_uses_link_or_junction(path_value: Path) -> bool:
    if path_value.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    if callable(is_junction):
        try:
            return bool(is_junction(path_value))
        except OSError:
            return False
    return False


def _validate_insecure_platform_path(
    candidate_path: Path,
    *,
    allowed_root: Optional[Path],
) -> None:
    if allowed_root is None:
        raise OSError(errno.EPERM, "allowed_root is required for insecure path open")

    try:
        relative_path = candidate_path.relative_to(allowed_root)
    except ValueError as exc:
        raise OSError(errno.EPERM, "candidate path resolves outside allowed root") from exc

    path_parts = relative_path.parts
    if not path_parts:
        raise OSError(errno.EISDIR, "candidate path resolves to an allowed root")

    current_path = allowed_root
    resolved_root = allowed_root.resolve(strict=False)
    if _path_uses_link_or_junction(current_path):
        raise OSError(errno.ELOOP, "symlinks and junctions are not allowed")
    if not _is_path_within_root(resolved_root, allowed_root):
        raise OSError(errno.EPERM, "resolved path escapes allowed root")

    for index, component in enumerate(path_parts):
        current_path = current_path / component
        try:
            component_stat = os.stat(current_path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise OSError(errno.ENOENT, "candidate path no longer exists") from exc

        if _path_uses_link_or_junction(current_path):
            raise OSError(errno.ELOOP, "symlinks and junctions are not allowed")

        try:
            resolved_component = current_path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            resolved_component = current_path
        if not _is_path_within_root(resolved_component, allowed_root):
            raise OSError(errno.EPERM, "resolved path escapes allowed root")

        is_last = index == len(path_parts) - 1
        if is_last:
            if not stat.S_ISREG(component_stat.st_mode):
                raise OSError(errno.EISDIR, "path is not a regular file")
            continue
        if not stat.S_ISDIR(component_stat.st_mode):
            raise OSError(errno.ENOTDIR, "path component is not a directory")


def _normalize_rate_limit_identifier(value: str, *, scope: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        return ""
    if len(rendered.encode("utf-8", errors="ignore")) <= _MAX_RATE_LIMIT_IDENTIFIER_BYTES:
        return rendered
    digest = hashlib.sha256(rendered.encode("utf-8", errors="ignore")).hexdigest()
    return f"{scope}-sha256:{digest}"


@dataclass(frozen=True)
class ExternalImportGuardConfig:
    enabled: bool = False
    allowed_roots: tuple[Path, ...] = ()
    allowed_exts: tuple[str, ...] = (".md", ".txt", ".json")
    max_total_bytes: int = 5 * 1024 * 1024
    max_files: int = 200
    rate_limit_window_seconds: int = 60
    rate_limit_max_requests: int = 10
    rate_limit_state_file: Optional[Path] = None
    rate_limit_state_lock_timeout_seconds: float = (
        _DEFAULT_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS
    )
    require_shared_rate_limit: bool = False

    @classmethod
    def from_env(cls) -> "ExternalImportGuardConfig":
        return cls(
            enabled=env_bool(
                "EXTERNAL_IMPORT_ENABLED",
                False,
                truthy_values=_TRUTHY_ENV_VALUES,
            ),
            allowed_roots=_normalize_allowed_roots(
                env_csv("EXTERNAL_IMPORT_ALLOWED_ROOTS")
            ),
            allowed_exts=_normalize_allowed_extensions(
                env_csv("EXTERNAL_IMPORT_ALLOWED_EXTS", ".md,.txt,.json")
            ),
            max_total_bytes=env_int(
                "EXTERNAL_IMPORT_MAX_TOTAL_BYTES", 5 * 1024 * 1024, minimum=1
            ),
            max_files=env_int("EXTERNAL_IMPORT_MAX_FILES", 200, minimum=1),
            rate_limit_window_seconds=env_int(
                "EXTERNAL_IMPORT_RATE_LIMIT_WINDOW_SECONDS", 60, minimum=1
            ),
            rate_limit_max_requests=env_int(
                "EXTERNAL_IMPORT_RATE_LIMIT_MAX_REQUESTS", 10, minimum=1
            ),
            rate_limit_state_file=(
                Path(state_file).expanduser().resolve(strict=False)
                if (state_file := str(os.getenv("EXTERNAL_IMPORT_RATE_LIMIT_STATE_FILE") or "").strip())
                else None
            ),
            rate_limit_state_lock_timeout_seconds=_env_float(
                "EXTERNAL_IMPORT_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS",
                _DEFAULT_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS,
                minimum=0.0,
            ),
            require_shared_rate_limit=env_bool(
                "EXTERNAL_IMPORT_REQUIRE_SHARED_RATE_LIMIT",
                False,
                truthy_values=_TRUTHY_ENV_VALUES,
            ),
        )


class ExternalImportGuard:
    def __init__(
        self,
        config: ExternalImportGuardConfig | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config or ExternalImportGuardConfig.from_env()
        if self._config.rate_limit_state_file is not None:
            is_network_filesystem, _ = warn_if_unreliable_file_lock_path(
                self._config.rate_limit_state_file,
                label="EXTERNAL_IMPORT_RATE_LIMIT_STATE_FILE",
                log=logger,
            )
            if (
                is_network_filesystem
                and not self._config.require_shared_rate_limit
            ):
                self._config = replace(self._config, rate_limit_state_file=None)
        self._clock = clock or time.time
        self._rate_limit_buckets: Dict[str, Deque[float]] = {}
        self._rate_limit_guard = threading.Lock()

    def policy_snapshot(self) -> Dict[str, Any]:
        roots = sorted(str(item) for item in self._config.allowed_roots)
        roots_payload = "|".join(roots)
        roots_fingerprint = (
            hashlib.sha256(roots_payload.encode("utf-8", errors="ignore")).hexdigest()
            if roots_payload
            else ""
        )
        return {
            "enabled": bool(self._config.enabled),
            "allowed_roots_count": len(roots),
            "allowed_roots_fingerprint": roots_fingerprint,
            "allowed_exts": list(self._config.allowed_exts),
            "max_total_bytes": int(self._config.max_total_bytes),
            "max_files": int(self._config.max_files),
            "rate_limit_window_seconds": int(self._config.rate_limit_window_seconds),
            "rate_limit_max_requests": int(self._config.rate_limit_max_requests),
            "rate_limit_storage": (
                "state_file"
                if self._config.rate_limit_state_file is not None
                else "process_memory"
            ),
            "rate_limit_state_lock_timeout_seconds": float(
                self._config.rate_limit_state_lock_timeout_seconds
            ),
            "require_shared_rate_limit": bool(
                self._config.require_shared_rate_limit
            ),
        }

    def validate_batch(
        self,
        *,
        file_paths: Sequence[str | Path],
        actor_id: str,
        session_id: str | None = None,
    ) -> Dict[str, Any]:
        requested = [str(path) for path in file_paths]
        actor = str(actor_id or "").strip()
        session = str(session_id or "").strip() or None
        result: Dict[str, Any] = {
            "ok": False,
            "reason": "rejected",
            "actor_id": actor,
            "session_id": session,
            "allowed_files": [],
            "rejected_files": [],
            "requested_file_count": len(requested),
            "file_count": 0,
            "max_files": int(self._config.max_files),
            "total_bytes": 0,
            "max_total_bytes": int(self._config.max_total_bytes),
            "retry_after_seconds": 0,
            "rate_limit_storage": (
                "state_file"
                if self._config.rate_limit_state_file is not None
                else "process_memory"
            ),
            "require_shared_rate_limit": bool(
                self._config.require_shared_rate_limit
            ),
            "policy": self.policy_snapshot(),
        }

        if not self._config.enabled:
            result["reason"] = "external_import_disabled"
            return result
        if not self._config.allowed_roots:
            result["reason"] = "allowed_roots_not_configured"
            return result
        if not self._config.allowed_exts:
            result["reason"] = "allowed_exts_not_configured"
            return result
        if not actor:
            result["reason"] = "actor_id_required"
            return result
        if (
            self._config.require_shared_rate_limit
            and self._config.rate_limit_state_file is None
        ):
            result["reason"] = "rate_limit_shared_state_required"
            result["config_errors"] = [
                (
                    "EXTERNAL_IMPORT_RATE_LIMIT_STATE_FILE is required when "
                    "EXTERNAL_IMPORT_REQUIRE_SHARED_RATE_LIMIT=true"
                )
            ]
            return result

        rate_limit_state = self._check_and_record_rate_limit(
            actor_id=actor,
            session_id=session,
        )
        result["rate_limit"] = rate_limit_state
        if not rate_limit_state.get("allowed", False):
            result["reason"] = str(rate_limit_state.get("reason") or "rate_limited")
            result["retry_after_seconds"] = int(
                rate_limit_state.get("retry_after_seconds") or 0
            )
            state_error = str(rate_limit_state.get("state_error") or "").strip()
            if state_error:
                result["rate_limit_state_error"] = state_error
            return result

        if not requested:
            result["reason"] = "no_files_provided"
            return result

        if len(requested) > self._config.max_files:
            result["reason"] = "max_files_exceeded"
            result["rejected_files"] = [
                {
                    "path": raw_path,
                    "reason": "max_files_exceeded",
                    "detail": (
                        f"requested={len(requested)} exceeds max_files={self._config.max_files}"
                    ),
                }
                for raw_path in requested
            ]
            return result

        allowed_files: list[Dict[str, Any]] = []
        rejected_files: list[Dict[str, Any]] = []
        total_bytes = 0
        for raw_path in requested:
            inspected = self._inspect_candidate(raw_path)
            if inspected.get("ok"):
                file_info = inspected["file"]
                total_bytes += int(file_info["size_bytes"])
                allowed_files.append(file_info)
                continue
            rejected_files.append(
                {
                    "path": raw_path,
                    "reason": inspected.get("reason", "rejected"),
                    "detail": inspected.get("detail", ""),
                }
            )

        result["allowed_files"] = allowed_files
        result["rejected_files"] = rejected_files
        result["file_count"] = len(allowed_files)
        result["total_bytes"] = int(total_bytes)

        if rejected_files:
            result["reason"] = "file_validation_failed"
            return result

        if total_bytes > self._config.max_total_bytes:
            overflow = total_bytes - self._config.max_total_bytes
            result["reason"] = "max_total_bytes_exceeded"
            result["rejected_files"] = [
                {
                    "path": "<batch>",
                    "reason": "max_total_bytes_exceeded",
                    "detail": (
                        f"total_bytes={total_bytes} exceeds "
                        f"max_total_bytes={self._config.max_total_bytes} by {overflow}"
                    ),
                }
            ]
            return result

        result["ok"] = True
        result["reason"] = "ok"
        return result

    def _inspect_candidate(self, raw_path: str) -> Dict[str, Any]:
        requested = str(raw_path or "").strip()
        if not requested:
            return {
                "ok": False,
                "reason": "invalid_path",
                "detail": "path is empty",
            }

        candidate = _absolute_path_without_symlink_resolution(Path(requested))
        allowed_root = self._match_allowed_root(candidate)
        if allowed_root is None:
            return {
                "ok": False,
                "reason": "path_not_allowed",
                "detail": "candidate path is outside allowed roots",
            }

        extension = candidate.suffix.lower()
        if extension not in self._config.allowed_exts:
            return {
                "ok": False,
                "reason": "extension_not_allowed",
                "detail": f"extension {extension!r} is not allowed",
            }

        try:
            descriptor = _open_path_without_symlink_escape(
                candidate,
                allowed_root=allowed_root,
            )
            with os.fdopen(descriptor, "rb") as handle:
                file_stat = os.fstat(handle.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    return {
                        "ok": False,
                        "reason": "not_a_file",
                        "detail": "path is not a regular file",
                    }
                try:
                    current_path_stat = os.stat(candidate, follow_symlinks=False)
                except FileNotFoundError:
                    return {
                        "ok": False,
                        "reason": "file_changed_during_validation",
                        "detail": "file changed during validation",
                    }
                except OSError:
                    return {
                        "ok": False,
                        "reason": "file_read_failed",
                        "detail": "failed to stat file metadata",
                    }
                if stat.S_ISLNK(current_path_stat.st_mode):
                    return {
                        "ok": False,
                        "reason": "symlink_not_allowed",
                        "detail": "symlinks are not allowed",
                    }
                if not stat.S_ISREG(current_path_stat.st_mode):
                    return {
                        "ok": False,
                        "reason": "not_a_file",
                        "detail": "path is not a regular file",
                    }
                if os.name != "nt":
                    real_candidate = Path(os.path.realpath(candidate))
                    if real_candidate != candidate:
                        return {
                            "ok": False,
                            "reason": "symlink_not_allowed",
                            "detail": "symlinks are not allowed",
                        }
                if not os.path.samestat(current_path_stat, file_stat):
                    return {
                        "ok": False,
                        "reason": "file_changed_during_validation",
                        "detail": "file changed during validation",
                    }
                size_bytes = int(file_stat.st_size)
                if size_bytes > self._config.max_total_bytes:
                    return {
                        "ok": False,
                        "reason": "max_total_bytes_exceeded",
                        "detail": (
                            f"file_bytes={size_bytes} exceeds "
                            f"max_total_bytes={self._config.max_total_bytes}"
                        ),
                    }
                raw_bytes = handle.read()
        except OSError as exc:
            if getattr(exc, "errno", None) == errno.ELOOP:
                return {
                    "ok": False,
                    "reason": "symlink_not_allowed",
                    "detail": "symlinks are not allowed",
                }
            if getattr(exc, "errno", None) == errno.EPERM:
                return {
                    "ok": False,
                    "reason": "path_not_allowed",
                    "detail": "resolved path escapes allowed roots",
                }
            if getattr(exc, "errno", None) == errno.ENOENT:
                return {
                    "ok": False,
                    "reason": "file_changed_during_validation",
                    "detail": "file changed during validation",
                }
            if getattr(exc, "errno", None) in {errno.EISDIR, errno.ENOTDIR}:
                return {
                    "ok": False,
                    "reason": "not_a_file",
                    "detail": "path is not a regular file",
                }
            return {
                "ok": False,
                "reason": "file_read_failed",
                "detail": "failed to read file content",
            }
        try:
            content = raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "reason": "encoding_not_supported",
                "detail": "file must be UTF-8 decodable",
            }
        identity = _stat_identity(file_stat)
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            resolved = candidate
        if not self._is_within_allowed_roots(resolved):
            return {
                "ok": False,
                "reason": "path_not_allowed",
                "detail": "resolved path escapes allowed roots",
            }

        return {
            "ok": True,
            "file": {
                "path": requested,
                "resolved_path": str(resolved),
                "extension": extension,
                "size_bytes": size_bytes,
                "content": content,
                "identity": identity,
            },
        }

    def _match_allowed_root(self, candidate_path: Path) -> Optional[Path]:
        matched_root: Optional[Path] = None
        matched_depth = -1
        for root in self._config.allowed_roots:
            try:
                candidate_path.relative_to(root)
            except ValueError:
                continue
            depth = len(root.parts)
            if depth > matched_depth:
                matched_root = root
                matched_depth = depth
        return matched_root

    def _is_within_allowed_roots(self, resolved_path: Path) -> bool:
        for root in self._config.allowed_roots:
            try:
                resolved_path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _check_and_record_rate_limit(
        self,
        *,
        actor_id: str,
        session_id: str | None,
    ) -> Dict[str, Any]:
        now = float(self._clock())
        window_seconds = float(self._config.rate_limit_window_seconds)
        max_requests = int(self._config.rate_limit_max_requests)
        keys = self._rate_limit_keys(actor_id=actor_id, session_id=session_id)
        with self._rate_limit_guard:
            state_file = self._config.rate_limit_state_file
            if state_file is not None:
                return self._check_and_record_rate_limit_with_state_file(
                    keys=keys,
                    now=now,
                    window_seconds=window_seconds,
                    max_requests=max_requests,
                    state_file=state_file,
                )
            buckets: Dict[str, Deque[float]] = {}
            for key in keys:
                bucket = self._rate_limit_buckets.setdefault(key, deque())
                buckets[key] = bucket
                rate_limit_state = self._evaluate_rate_limit_bucket(
                    bucket=bucket,
                    key=key,
                    now=now,
                    window_seconds=window_seconds,
                    max_requests=max_requests,
                    keys=keys,
                )
                if not rate_limit_state.get("allowed", False):
                    return rate_limit_state

            for bucket in buckets.values():
                bucket.append(now)

            primary_key = keys[0]
            primary_bucket = buckets[primary_key]
            remaining = max_requests - len(primary_bucket)
            return {
                "allowed": True,
                "reason": "ok",
                "key": primary_key,
                "keys": list(keys),
                "scope": self._rate_limit_scope_from_key(primary_key),
                "window_seconds": int(window_seconds),
                "max_requests": max_requests,
                "remaining": max(0, int(remaining)),
                "retry_after_seconds": 0,
            }

    @staticmethod
    def _rate_limit_keys(*, actor_id: str, session_id: str | None) -> tuple[str, ...]:
        normalized_actor_id = _normalize_rate_limit_identifier(actor_id, scope="actor")
        actor_key = f"{normalized_actor_id}::*"
        keys = [actor_key]
        if session_id:
            normalized_session_id = _normalize_rate_limit_identifier(
                session_id,
                scope="session",
            )
            session_key = f"{normalized_actor_id}::{normalized_session_id}"
            if session_key not in keys:
                keys.append(session_key)
        return tuple(keys)

    @staticmethod
    def _rate_limit_scope_from_key(key: str) -> str:
        if key.endswith("::*"):
            return "actor"
        return "session"

    def _check_and_record_rate_limit_with_state_file(
        self,
        *,
        keys: Sequence[str],
        now: float,
        window_seconds: float,
        max_requests: int,
        state_file: Path,
    ) -> Dict[str, Any]:
        lock_file = Path(f"{state_file}.lock")
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(
                str(lock_file),
                timeout=float(self._config.rate_limit_state_lock_timeout_seconds),
            ):
                payload, load_error = self._load_rate_limit_state_payload(state_file)
                if load_error:
                    return self._rate_limit_state_unavailable(
                        key=keys[0],
                        keys=keys,
                        window_seconds=window_seconds,
                        max_requests=max_requests,
                        state_error=load_error,
                    )

                buckets: Dict[str, Deque[float]] = {}
                for key in keys:
                    bucket_values, bucket_error = self._extract_bucket_from_payload(
                        payload=payload,
                        key=key,
                    )
                    if bucket_error:
                        return self._rate_limit_state_unavailable(
                            key=key,
                            keys=keys,
                            window_seconds=window_seconds,
                            max_requests=max_requests,
                            state_error=bucket_error,
                        )

                    bucket = deque(bucket_values)
                    rate_limit_state = self._evaluate_rate_limit_bucket(
                        bucket=bucket,
                        key=key,
                        now=now,
                        window_seconds=window_seconds,
                        max_requests=max_requests,
                        keys=keys,
                    )
                    if not rate_limit_state.get("allowed", False):
                        return rate_limit_state
                    buckets[key] = bucket

                self._prune_rate_limit_state_payload(
                    payload=payload,
                    now=now,
                    window_seconds=window_seconds,
                    protected_keys=set(keys),
                )
                for key, bucket in buckets.items():
                    bucket.append(now)
                    payload[key] = list(bucket)
                save_error = self._write_rate_limit_state_payload(
                    state_file=state_file,
                    payload=payload,
                )
                if save_error:
                    return self._rate_limit_state_unavailable(
                        key=keys[0],
                        keys=keys,
                        window_seconds=window_seconds,
                        max_requests=max_requests,
                        state_error=save_error,
                    )

                for key, bucket in buckets.items():
                    self._rate_limit_buckets[key] = deque(bucket)

                primary_key = keys[0]
                primary_bucket = buckets[primary_key]
                remaining = max_requests - len(primary_bucket)
                return {
                    "allowed": True,
                    "reason": "ok",
                    "key": primary_key,
                    "keys": list(keys),
                    "scope": self._rate_limit_scope_from_key(primary_key),
                    "window_seconds": int(window_seconds),
                    "max_requests": max_requests,
                    "remaining": max(0, int(remaining)),
                    "retry_after_seconds": 0,
                }
        except Timeout:
            return self._rate_limit_state_unavailable(
                key=keys[0],
                keys=keys,
                window_seconds=window_seconds,
                max_requests=max_requests,
                state_error="state_lock_timeout",
            )
        except OSError:
            return self._rate_limit_state_unavailable(
                key=keys[0],
                keys=keys,
                window_seconds=window_seconds,
                max_requests=max_requests,
                state_error="state_io_error",
            )

    def _evaluate_rate_limit_bucket(
        self,
        *,
        bucket: Deque[float],
        key: str,
        now: float,
        window_seconds: float,
        max_requests: int,
        keys: Sequence[str],
    ) -> Dict[str, Any]:
        cutoff = now - window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if bucket and not math.isfinite(float(bucket[0])):
            return self._rate_limit_state_unavailable(
                key=key,
                keys=keys,
                window_seconds=window_seconds,
                max_requests=max_requests,
                state_error="invalid_bucket_timestamp",
            )

        if len(bucket) >= max_requests:
            retry_raw = window_seconds - (now - float(bucket[0]))
            if not math.isfinite(retry_raw):
                return self._rate_limit_state_unavailable(
                    key=key,
                    keys=keys,
                    window_seconds=window_seconds,
                    max_requests=max_requests,
                    state_error="invalid_retry_after",
                )
            retry_after = max(1, int(math.ceil(retry_raw)))
            return {
                "allowed": False,
                "reason": "rate_limited",
                "key": key,
                "keys": list(keys),
                "scope": self._rate_limit_scope_from_key(key),
                "window_seconds": int(window_seconds),
                "max_requests": max_requests,
                "remaining": 0,
                "retry_after_seconds": retry_after,
            }

        return {
            "allowed": True,
            "reason": "ok",
            "key": key,
            "keys": list(keys),
            "scope": self._rate_limit_scope_from_key(key),
            "window_seconds": int(window_seconds),
            "max_requests": max_requests,
            "remaining": max(0, int(max_requests - len(bucket))),
            "retry_after_seconds": 0,
        }

    @staticmethod
    def _rate_limit_state_unavailable(
        *,
        key: str,
        keys: Sequence[str],
        window_seconds: float,
        max_requests: int,
        state_error: str,
    ) -> Dict[str, Any]:
        return {
            "allowed": False,
            "reason": "rate_limit_state_unavailable",
            "key": key,
            "keys": list(keys),
            "scope": ExternalImportGuard._rate_limit_scope_from_key(key),
            "window_seconds": int(window_seconds),
            "max_requests": max_requests,
            "remaining": 0,
            "retry_after_seconds": 0,
            "state_error": state_error,
        }

    @staticmethod
    def _load_rate_limit_state_payload(
        state_file: Path,
    ) -> tuple[Dict[str, Any], Optional[str]]:
        if not state_file.exists():
            return {}, None
        if not state_file.is_file():
            return {}, "state_file_not_regular_file"
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except OSError:
            return {}, "state_file_read_failed"
        except json.JSONDecodeError:
            return {}, "state_file_invalid_json"
        if not isinstance(payload, dict):
            return {}, "state_file_invalid_payload"
        return payload, None

    @staticmethod
    def _extract_bucket_from_payload(
        *, payload: Dict[str, Any], key: str
    ) -> tuple[list[float], Optional[str]]:
        values = payload.get(key)
        if values is None:
            return [], None
        if not isinstance(values, list):
            return [], "state_bucket_invalid_type"
        parsed: list[float] = []
        for item in values:
            try:
                timestamp = float(item)
            except (TypeError, ValueError):
                return [], "state_bucket_invalid_timestamp"
            if not math.isfinite(timestamp) or timestamp < 0:
                return [], "state_bucket_invalid_timestamp"
            parsed.append(timestamp)
        return parsed, None

    @staticmethod
    def _write_rate_limit_state_payload(
        *, state_file: Path, payload: Dict[str, Any]
    ) -> Optional[str]:
        tmp_file = state_file.with_name(
            f"{state_file.name}.tmp.{os.getpid()}.{threading.get_ident()}"
        )
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_file.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_file.replace(state_file)
            return None
        except OSError:
            return "state_file_write_failed"
        finally:
            try:
                if tmp_file.exists():
                    tmp_file.unlink()
            except OSError:
                pass

    @staticmethod
    def _prune_rate_limit_state_payload(
        *,
        payload: Dict[str, Any],
        now: float,
        window_seconds: float,
        protected_keys: set[str],
    ) -> None:
        cutoff = float(now) - float(window_seconds)
        for key in list(payload.keys()):
            if key in protected_keys:
                continue
            values = payload.get(key)
            if not isinstance(key, str) or not isinstance(values, list):
                payload.pop(key, None)
                continue

            cleaned: list[float] = []
            valid = True
            for item in values:
                try:
                    timestamp = float(item)
                except (TypeError, ValueError):
                    valid = False
                    break
                if not math.isfinite(timestamp) or timestamp < 0:
                    valid = False
                    break
                if timestamp > cutoff:
                    cleaned.append(timestamp)

            if not valid or not cleaned:
                payload.pop(key, None)
                continue
            payload[key] = cleaned

        candidate_keys = [
            key
            for key in payload.keys()
            if key not in protected_keys and isinstance(payload.get(key), list)
        ]
        overflow = len(candidate_keys) - _MAX_RATE_LIMIT_STATE_KEYS
        if overflow <= 0:
            return
        eviction_candidates = sorted(
            candidate_keys,
            key=lambda key: max(float(item) for item in payload.get(key, [])),
        )
        for key in eviction_candidates[:overflow]:
            payload.pop(key, None)
