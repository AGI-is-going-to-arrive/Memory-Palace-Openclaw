"""
Snapshot Manager for Selective Rollback

This module implements a snapshot system that allows the human to review and
selectively roll back the AI's database operations.

Design Principles:
1. Snapshots are taken BEFORE the first modification to a resource in a session
2. Multiple modifications to the same resource in one session share ONE snapshot
3. Rollback creates a NEW version with snapshot content (preserves version chain)
4. Session-based organization for easy cleanup

    Storage Structure:
    snapshots/
    └── {session_id}/
        ├── manifest.json          # Session metadata and resource index
        └── resources/
            └── {safe_resource_id}.json
"""

import os
import json
import errno
import hashlib
import shutil
import stat
import sys
import tempfile
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from pathlib import Path
from filelock import FileLock, Timeout
from env_utils import parse_iso_datetime_with_options


# Default snapshot directory (relative to workspace root)
DEFAULT_SNAPSHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "snapshots"
)
_SNAPSHOT_FILENAME_PREFIX_CHARS = 48
_SNAPSHOT_FILENAME_COMPAT_PREFIX_CHARS = 100


def _retry_remove_readonly(func, path: str) -> None:
    """Retry a failed removal after clearing a read-only bit."""
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def _handle_remove_readonly(func, path, exc_info):
    """Legacy shutil.rmtree callback for Python < 3.12."""
    exc_type, exc_value, _ = exc_info
    if issubclass(exc_type, PermissionError):
        _retry_remove_readonly(func, path)
    else:
        raise exc_value


def _handle_remove_readonly_onexc(func, path, exc):
    """shutil.rmtree callback for Python >= 3.12."""
    if isinstance(exc, PermissionError):
        _retry_remove_readonly(func, path)
    else:
        raise exc


def _remove_tree(path: str) -> None:
    """Delete a directory tree with the supported shutil callback API."""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_handle_remove_readonly_onexc)
    else:
        shutil.rmtree(path, onerror=_handle_remove_readonly)


def _force_remove(path: str):
    """Delete files or directories regardless of read-only attributes."""
    if not os.path.exists(path):
        return
    if os.path.isdir(path):
        _remove_tree(path)
    else:
        try:
            os.remove(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)
        except FileNotFoundError:
            pass


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    """Best-effort ISO timestamp parsing with UTC normalization."""
    if not isinstance(value, str):
        return None
    parsed = parse_iso_datetime_with_options(
        value,
        normalize_utc=True,
        assume_utc_for_naive=True,
    )
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed


def _read_optional_positive_int_env(name: str) -> Optional[int]:
    raw_value = str(os.getenv(name, "") or "").strip()
    if not raw_value:
        return None
    try:
        parsed = int(raw_value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


class SnapshotManager:
    """
    Manages snapshots for selective rollback functionality.

    Each session (typically one agent task/conversation) has its own snapshot space.
    Within a session, each resource gets at most ONE snapshot - the state before
    the first modification.
    """

    def __init__(self, snapshot_dir: Optional[str] = None):
        self.snapshot_dir = snapshot_dir or DEFAULT_SNAPSHOT_DIR
        self._ensure_dir_exists(self.snapshot_dir)
        raw_timeout = os.getenv("SNAPSHOT_LOCK_TIMEOUT_SEC", "").strip()
        raw_list_timeout = os.getenv("SNAPSHOT_LIST_LOCK_TIMEOUT_SEC", "").strip()
        try:
            self.lock_timeout_seconds = max(0.0, float(raw_timeout)) if raw_timeout else 10.0
        except ValueError:
            self.lock_timeout_seconds = 10.0
        try:
            self.list_lock_timeout_seconds = (
                max(0.0, float(raw_list_timeout)) if raw_list_timeout else 0.0
            )
        except ValueError:
            self.list_lock_timeout_seconds = 0.0
        self.warn_max_sessions = _read_optional_positive_int_env("SNAPSHOT_WARN_MAX_SESSIONS")
        self.warn_max_total_bytes = _read_optional_positive_int_env("SNAPSHOT_WARN_MAX_TOTAL_BYTES")
        self.warn_max_session_bytes = _read_optional_positive_int_env("SNAPSHOT_WARN_MAX_SESSION_BYTES")
        self.warn_max_resources_per_session = _read_optional_positive_int_env(
            "SNAPSHOT_WARN_MAX_RESOURCES_PER_SESSION"
        )
        self.enforce_max_session_bytes = _read_optional_positive_int_env(
            "SNAPSHOT_ENFORCE_MAX_SESSION_BYTES"
        )
        self.enforce_max_resources_per_session = _read_optional_positive_int_env(
            "SNAPSHOT_ENFORCE_MAX_RESOURCES_PER_SESSION"
        )

    @staticmethod
    def _validate_session_id(session_id: str) -> str:
        """Validate session_id to prevent path traversal and invalid paths."""
        value = str(session_id or "").strip()
        if not value:
            raise ValueError("session_id must not be empty")
        if value in {".", ".."}:
            raise ValueError("session_id contains invalid path segment")
        if "/" in value or "\\" in value or "\x00" in value:
            raise ValueError("session_id contains invalid characters")
        if any(char in value for char in '<>:"|?*'):
            raise ValueError("session_id contains invalid filename characters")
        if any(char.isspace() for char in value):
            raise ValueError("session_id must not contain whitespace")
        if any(unicodedata.category(char) == "Cf" for char in value):
            raise ValueError("session_id contains non-printing format characters")
        if value.endswith(".") or value.endswith(" "):
            raise ValueError("session_id must not end with dot or space")
        return value

    @staticmethod
    def _ensure_dir_exists(path: str):
        """Create directory if it doesn't exist."""
        Path(path).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _sanitize_resource_id_with_limit(resource_id: str, prefix_chars: int) -> str:
        """
        Convert a resource_id to a safe filename.
        
        Resource IDs like URIs "core://path/to/memory" need sanitization.
        We use a deterministic hash suffix for uniqueness to prevent collisions
        (e.g. "core://a/b" vs "core://a_b") while keeping readability.
        """
        # Calculate hash of the ORIGINAL resource_id for uniqueness.
        # Keep a readable prefix, but reduce filename collision risk.
        id_hash = hashlib.sha256(resource_id.encode("utf-8")).hexdigest()[:12]

        # Replace problematic characters
        # 1. Handle protocol separator specifically for better readability
        safe_id = resource_id.replace("://", "__")
        
        # 2. Replace remaining colons, slashes, and backslashes
        safe_id = safe_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        
        # 3. Replace relation arrow
        safe_id = safe_id.replace(">", "_to_")
        
        # Keep filenames short enough that common snapshot paths stay well
        # under legacy Windows MAX_PATH limits.
        if len(safe_id) > prefix_chars:
            safe_id = safe_id[:prefix_chars]
        
        # Always append hash to guarantee uniqueness
        return f"{safe_id}_{id_hash}"

    @classmethod
    def _sanitize_resource_id(cls, resource_id: str) -> str:
        return cls._sanitize_resource_id_with_limit(
            resource_id,
            _SNAPSHOT_FILENAME_PREFIX_CHARS,
        )

    @classmethod
    def _compat_sanitize_resource_id(cls, resource_id: str) -> str:
        return cls._sanitize_resource_id_with_limit(
            resource_id,
            _SNAPSHOT_FILENAME_COMPAT_PREFIX_CHARS,
        )

    @staticmethod
    def _legacy_sanitize_resource_id(resource_id: str) -> str:
        """Resolve legacy snapshot filenames created with the old md5 suffix."""
        legacy_hash = hashlib.md5(resource_id.encode("utf-8")).hexdigest()[:8]
        safe_id = resource_id.replace("://", "__")
        safe_id = safe_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        safe_id = safe_id.replace(">", "_to_")
        if len(safe_id) > _SNAPSHOT_FILENAME_COMPAT_PREFIX_CHARS:
            safe_id = safe_id[:_SNAPSHOT_FILENAME_COMPAT_PREFIX_CHARS]
        return f"{safe_id}_{legacy_hash}"
    
    def _get_session_dir(self, session_id: str) -> str:
        """Get the directory path for a session."""
        safe_session_id = self._validate_session_id(session_id)
        return os.path.join(self.snapshot_dir, safe_session_id)
    
    def _get_resources_dir(self, session_id: str) -> str:
        """Get the resources subdirectory for a session."""
        return os.path.join(self._get_session_dir(session_id), "resources")
    
    def _get_manifest_path(self, session_id: str) -> str:
        """Get the manifest file path for a session."""
        return os.path.join(self._get_session_dir(session_id), "manifest.json")

    def _get_locks_dir(self) -> str:
        """Get the lock directory path."""
        return os.path.join(self.snapshot_dir, ".locks")

    def _get_session_lock_path(self, session_id: str) -> str:
        """Get the per-session snapshot manager lock path."""
        safe_session_id = self._sanitize_resource_id(self._validate_session_id(session_id))
        return os.path.join(self._get_locks_dir(), f"{safe_session_id}.lock")

    def _acquire_session_lock(
        self,
        session_id: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> FileLock:
        """Acquire the per-session snapshot manager file lock."""
        self._ensure_dir_exists(self._get_locks_dir())
        lock_path = self._get_session_lock_path(session_id)
        effective_timeout = (
            self.lock_timeout_seconds
            if timeout_seconds is None
            else max(0.0, float(timeout_seconds))
        )
        lock = FileLock(lock_path, timeout=effective_timeout)
        try:
            lock.acquire()
        except Timeout as exc:
            raise RuntimeError(
                "Timed out waiting for snapshot session lock: "
                f"{lock_path} ({effective_timeout}s)"
            ) from exc
        return lock

    def _remove_session_lock_file(self, session_id: str) -> None:
        """Best-effort cleanup for stale lock files after a session is removed."""
        lock_path = self._get_session_lock_path(session_id)
        try:
            _force_remove(lock_path)
        except OSError:
            pass
    
    def _get_snapshot_path(self, session_id: str, resource_id: str) -> str:
        """Get the snapshot file path for a specific resource."""
        resources_dir = self._get_resources_dir(session_id)
        safe_id = self._sanitize_resource_id(resource_id)
        snapshot_path = os.path.join(resources_dir, f"{safe_id}.json")
        if os.name == "nt" and len(snapshot_path) > 240:
            max_name_len = max(
                24,
                240 - (len(resources_dir) + 1),
            )
            hash_suffix_len = 1 + 12  # "_" + sha256[:12]
            prefix_chars = max(8, max_name_len - hash_suffix_len - len(".json"))
            safe_id = self._sanitize_resource_id_with_limit(resource_id, prefix_chars)
            snapshot_path = os.path.join(resources_dir, f"{safe_id}.json")
        return snapshot_path

    def _get_candidate_snapshot_paths(self, session_id: str, resource_id: str) -> List[str]:
        """Resolve current and legacy snapshot filenames for the same resource."""
        resources_dir = self._get_resources_dir(session_id)
        current_path = self._get_snapshot_path(session_id, resource_id)
        compat_name = f"{self._compat_sanitize_resource_id(resource_id)}.json"
        legacy_name = f"{self._legacy_sanitize_resource_id(resource_id)}.json"
        candidates = [current_path]
        compat_path = os.path.join(resources_dir, compat_name)
        if compat_path not in candidates:
            candidates.append(compat_path)
        legacy_path = os.path.join(resources_dir, legacy_name)
        if legacy_path not in candidates:
            candidates.append(legacy_path)
        return candidates

    def _resolve_snapshot_path(
        self,
        session_id: str,
        resource_id: str,
        *,
        preferred_file: Optional[str] = None,
    ) -> str:
        """Resolve the on-disk snapshot path, including legacy filenames."""
        if preferred_file:
            preferred_path = os.path.join(self._get_resources_dir(session_id), preferred_file)
            if os.path.exists(preferred_path):
                return preferred_path
        for candidate in self._get_candidate_snapshot_paths(session_id, resource_id):
            if os.path.exists(candidate):
                return candidate
        return self._get_snapshot_path(session_id, resource_id)

    @staticmethod
    def _fsync_directory(path: str) -> None:
        """Best-effort fsync for parent directory after atomic replace."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _atomic_write_json(self, path: str, payload: Dict[str, Any]) -> None:
        """Atomically persist a JSON payload to disk."""
        directory = os.path.dirname(path)
        self._ensure_dir_exists(directory)

        fd: Optional[int] = None
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                prefix=".tmp-",
                suffix=".json",
                dir=directory,
                text=True,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = None
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            self._replace_atomic(tmp_path, path)
            self._fsync_directory(directory)
            tmp_path = None
        finally:
            if fd is not None:
                os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                _force_remove(tmp_path)

    @staticmethod
    def _replace_atomic(src: str, dst: str) -> None:
        max_attempts = 3 if os.name == "nt" else 1
        for attempt in range(max_attempts):
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                if os.name != "nt" or attempt >= max_attempts - 1:
                    raise
            except OSError as exc:
                winerror = getattr(exc, "winerror", None)
                retryable = winerror in {5, 32, 33} or exc.errno in {
                    errno.EACCES,
                    errno.EPERM,
                }
                if os.name != "nt" or not retryable or attempt >= max_attempts - 1:
                    raise
            time.sleep(0.05 * (attempt + 1))
    
    def _load_manifest(self, session_id: str) -> Dict[str, Any]:
        """Load or create session manifest."""
        manifest_path = self._get_manifest_path(session_id)
        
        if os.path.exists(manifest_path):
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
        else:
            manifest = {
                "session_id": session_id,
                "created_at": _utc_iso_now(),
                "resources": {}  # resource_id -> metadata
            }

        return self._recover_manifest_resources(session_id, manifest)

    def _recover_manifest_resources(
        self, session_id: str, manifest: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Backfill manifest entries for valid snapshot files already present on disk."""
        resources_dir = Path(self._get_resources_dir(session_id))
        if not resources_dir.exists():
            return manifest

        resources = manifest.setdefault("resources", {})
        known_files = set()
        recovered = False
        for resource_id, meta in list(resources.items()):
            if not isinstance(meta, dict):
                resources.pop(resource_id, None)
                recovered = True
                continue
            preferred_file = str(meta.get("file") or "") or None
            snapshot_path = self._resolve_snapshot_path(
                session_id,
                resource_id,
                preferred_file=preferred_file,
            )
            if not os.path.exists(snapshot_path):
                resources.pop(resource_id, None)
                recovered = True
                continue
            file_name = os.path.basename(snapshot_path)
            if meta.get("file") != file_name:
                meta["file"] = file_name
                recovered = True
            known_files.add(file_name)
        for snapshot_path in resources_dir.glob("*.json"):
            if snapshot_path.name in known_files:
                continue
            try:
                payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue

            resource_id = payload.get("resource_id")
            resource_type = payload.get("resource_type")
            snapshot_time = payload.get("snapshot_time")
            data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
            if not isinstance(resource_id, str) or not resource_id.strip():
                continue
            if not isinstance(resource_type, str) or not resource_type.strip():
                continue
            if not isinstance(snapshot_time, str) or not snapshot_time.strip():
                continue

            resources[resource_id] = {
                "resource_type": resource_type,
                "snapshot_time": snapshot_time,
                "operation_type": data.get("operation_type", "modify"),
                "file": snapshot_path.name,
                "uri": data.get("uri"),
            }
            known_files.add(snapshot_path.name)
            recovered = True

        if recovered and os.path.exists(self._get_session_dir(session_id)):
            self._save_manifest(session_id, manifest)

        return manifest
    
    def _save_manifest(self, session_id: str, manifest: Dict[str, Any]):
        """Save session manifest."""
        self._ensure_dir_exists(self._get_session_dir(session_id))
        manifest_path = self._get_manifest_path(session_id)
        self._atomic_write_json(manifest_path, manifest)

    @staticmethod
    def _estimate_json_size_bytes(payload: Dict[str, Any]) -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _session_total_bytes_unlocked(
        self,
        session_id: str,
        *,
        manifest: Optional[Dict[str, Any]] = None,
    ) -> int:
        session_dir = self._get_session_dir(session_id)
        if not os.path.isdir(session_dir):
            return 0
        total_bytes = 0
        manifest_path = self._get_manifest_path(session_id)
        if os.path.exists(manifest_path):
            total_bytes += os.path.getsize(manifest_path)
        resources = (
            manifest.get("resources", {}) if isinstance(manifest, dict) else None
        )
        if isinstance(resources, dict) and resources:
            for resource_id, meta in resources.items():
                file_name = meta.get("file") if isinstance(meta, dict) else None
                snapshot_path = (
                    os.path.join(self._get_resources_dir(session_id), str(file_name))
                    if file_name
                    else self._resolve_snapshot_path(session_id, str(resource_id))
                )
                if os.path.exists(snapshot_path):
                    total_bytes += os.path.getsize(snapshot_path)
            return total_bytes
        resources_dir = self._get_resources_dir(session_id)
        if os.path.isdir(resources_dir):
            for entry in os.scandir(resources_dir):
                if entry.is_file():
                    total_bytes += entry.stat().st_size
        return total_bytes

    def _session_storage_record_unlocked(
        self,
        session_id: str,
        manifest: Dict[str, Any],
    ) -> Dict[str, Any]:
        resources = manifest.get("resources", {}) if isinstance(manifest, dict) else {}
        snapshot_times = [
            str(meta.get("snapshot_time") or "").strip()
            for meta in resources.values()
            if isinstance(meta, dict) and str(meta.get("snapshot_time") or "").strip()
        ]
        created_at = manifest.get("created_at")
        total_bytes = self._session_total_bytes_unlocked(session_id, manifest=manifest)
        now = datetime.now(timezone.utc)
        created_at_dt = _parse_iso_datetime(created_at)
        oldest_snapshot_time = min(snapshot_times) if snapshot_times else None
        newest_snapshot_time = max(snapshot_times) if snapshot_times else None
        oldest_snapshot_dt = _parse_iso_datetime(oldest_snapshot_time)
        newest_snapshot_dt = _parse_iso_datetime(newest_snapshot_time)
        warning_codes = self._build_session_warning_codes(
            resource_count=len(resources),
            total_bytes=total_bytes,
        )
        return {
            "session_id": session_id,
            "created_at": created_at,
            "resource_count": len(resources),
            "total_bytes": total_bytes,
            "oldest_snapshot_time": oldest_snapshot_time,
            "newest_snapshot_time": newest_snapshot_time,
            "age_days": self._age_days(now, created_at_dt),
            "estimated_reclaim_bytes": total_bytes,
            "warning_codes": warning_codes,
            "over_warning_threshold": bool(warning_codes),
        }

    @staticmethod
    def _age_seconds(
        now: datetime,
        timestamp: Optional[datetime],
    ) -> Optional[int]:
        if timestamp is None:
            return None
        return max(0, int((now - timestamp).total_seconds()))

    @classmethod
    def _age_days(
        cls,
        now: datetime,
        timestamp: Optional[datetime],
    ) -> Optional[int]:
        seconds = cls._age_seconds(now, timestamp)
        if seconds is None:
            return None
        return max(0, seconds // 86400)

    def _build_session_warning_codes(
        self,
        *,
        resource_count: int,
        total_bytes: int,
    ) -> List[str]:
        warning_codes: List[str] = []
        if self.warn_max_session_bytes and total_bytes > self.warn_max_session_bytes:
            warning_codes.append("snapshot_session_bytes_over_warn_limit")
        if (
            self.warn_max_resources_per_session
            and resource_count > self.warn_max_resources_per_session
        ):
            warning_codes.append("snapshot_resources_over_warn_limit")
        return warning_codes

    def _build_storage_warnings(self, session_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        warnings: List[Dict[str, Any]] = []
        total_sessions = len(session_records)
        total_resources = sum(int(item.get("resource_count") or 0) for item in session_records)
        total_bytes = sum(int(item.get("total_bytes") or 0) for item in session_records)
        if self.warn_max_sessions and total_sessions > self.warn_max_sessions:
            warnings.append(
                {
                    "code": "snapshot_sessions_over_warn_limit",
                    "message": (
                        f"Snapshot session count exceeded warning threshold "
                        f"({total_sessions} > {self.warn_max_sessions})."
                    ),
                }
            )
        if self.warn_max_total_bytes and total_bytes > self.warn_max_total_bytes:
            warnings.append(
                {
                    "code": "snapshot_total_bytes_over_warn_limit",
                    "message": (
                        f"Snapshot storage exceeded warning threshold "
                        f"({total_bytes} > {self.warn_max_total_bytes} bytes)."
                    ),
                }
            )
        if self.warn_max_session_bytes:
            oversized = [
                item for item in session_records if int(item.get("total_bytes") or 0) > self.warn_max_session_bytes
            ]
            if oversized:
                warnings.append(
                    {
                        "code": "snapshot_session_bytes_over_warn_limit",
                        "message": (
                            f"{len(oversized)} session(s) exceeded the per-session size warning "
                            f"threshold ({self.warn_max_session_bytes} bytes)."
                        ),
                    }
                )
        if self.warn_max_resources_per_session:
            oversized = [
                item
                for item in session_records
                if int(item.get("resource_count") or 0) > self.warn_max_resources_per_session
            ]
            if oversized:
                warnings.append(
                    {
                        "code": "snapshot_resources_over_warn_limit",
                        "message": (
                            f"{len(oversized)} session(s) exceeded the per-session snapshot count warning "
                            f"threshold ({self.warn_max_resources_per_session})."
                        ),
                    }
                )
        _ = total_resources
        return warnings

    def storage_summary(self, *, top_n: int = 5) -> Dict[str, Any]:
        session_records: List[Dict[str, Any]] = []
        if os.path.exists(self.snapshot_dir):
            for entry in os.scandir(self.snapshot_dir):
                session_id = entry.name
                if session_id.startswith(".") or not entry.is_dir():
                    continue
                try:
                    lock = self._acquire_session_lock(
                        session_id,
                        timeout_seconds=self.list_lock_timeout_seconds,
                    )
                except RuntimeError:
                    continue
                try:
                    session_dir = self._get_session_dir(session_id)
                    if not os.path.isdir(session_dir):
                        continue
                    manifest = self._load_manifest(session_id)
                    resource_count = len(manifest.get("resources", {}))
                    if resource_count == 0:
                        continue
                    session_records.append(
                        self._session_storage_record_unlocked(session_id, manifest)
                    )
                finally:
                    lock.release()
        session_records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        total_resources = sum(int(item.get("resource_count") or 0) for item in session_records)
        total_bytes = sum(int(item.get("total_bytes") or 0) for item in session_records)
        largest_sessions = sorted(
            session_records,
            key=lambda item: int(item.get("total_bytes") or 0),
            reverse=True,
        )[: max(1, top_n)]
        oldest_sessions = sorted(
            session_records,
            key=lambda item: str(item.get("created_at") or ""),
        )[: max(1, top_n)]
        warnings = self._build_storage_warnings(session_records)
        return {
            "snapshot_dir": self.snapshot_dir,
            "session_count": len(session_records),
            "total_resources": total_resources,
            "total_bytes": total_bytes,
            "automatic_pruning": False,
            "enforcement_mode": "reject_new_session_snapshots_only",
            "limits": {
                "warn_max_sessions": self.warn_max_sessions,
                "warn_max_total_bytes": self.warn_max_total_bytes,
                "warn_max_session_bytes": self.warn_max_session_bytes,
                "warn_max_resources_per_session": self.warn_max_resources_per_session,
                "enforce_max_session_bytes": self.enforce_max_session_bytes,
                "enforce_max_resources_per_session": self.enforce_max_resources_per_session,
            },
            "warnings": warnings,
            "sessions": session_records,
            "largest_sessions": largest_sessions,
            "oldest_sessions": oldest_sessions,
        }

    def _enforce_session_snapshot_limits(
        self,
        *,
        session_id: str,
        manifest: Dict[str, Any],
        resource_id: str,
        projected_snapshot_bytes: int,
    ) -> None:
        resources = manifest.get("resources", {}) if isinstance(manifest, dict) else {}
        current_count = len(resources)
        current_bytes = self._session_total_bytes_unlocked(session_id, manifest=manifest)
        existing_meta = resources.get(resource_id) if isinstance(resources, dict) else None
        existing_file = existing_meta.get("file") if isinstance(existing_meta, dict) else None
        existing_snapshot_path = (
            os.path.join(self._get_resources_dir(session_id), str(existing_file))
            if existing_file
            else self._resolve_snapshot_path(session_id, resource_id)
        )
        existing_snapshot_bytes = (
            os.path.getsize(existing_snapshot_path) if os.path.exists(existing_snapshot_path) else 0
        )
        projected_count = current_count + (0 if resource_id in resources else 1)
        projected_bytes = current_bytes - existing_snapshot_bytes + projected_snapshot_bytes
        if (
            self.enforce_max_resources_per_session
            and projected_count > self.enforce_max_resources_per_session
        ):
            raise RuntimeError(
                "snapshot per-session resource limit exceeded "
                f"({projected_count} > {self.enforce_max_resources_per_session})."
            )
        if self.enforce_max_session_bytes and projected_bytes > self.enforce_max_session_bytes:
            raise RuntimeError(
                "snapshot per-session storage limit exceeded "
                f"({projected_bytes} > {self.enforce_max_session_bytes} bytes)."
            )

    def _has_snapshot_unlocked(self, session_id: str, resource_id: str) -> bool:
        """Unlocked helper for snapshot existence check."""
        manifest = self._load_manifest(session_id)
        if resource_id in manifest.get("resources", {}):
            return True
        return any(
            os.path.exists(snapshot_path)
            for snapshot_path in self._get_candidate_snapshot_paths(session_id, resource_id)
        )

    def _find_memory_snapshot_by_uri_unlocked(
        self, session_id: str, uri: str
    ) -> Optional[str]:
        """Unlocked helper for URI → memory snapshot lookup."""
        manifest = self._load_manifest(session_id)
        for resource_id, meta in manifest.get("resources", {}).items():
            if meta.get("resource_type") == "memory" and meta.get("uri") == uri:
                return resource_id
        return None

    def _clear_session_unlocked(self, session_id: str) -> int:
        """Unlocked helper to delete all snapshots in a session."""
        session_dir = self._get_session_dir(session_id)
        if not os.path.exists(session_dir):
            return 0
        manifest = self._load_manifest(session_id)
        count = len(manifest.get("resources", {}))
        _force_remove(session_dir)
        return count

    def has_snapshot(self, session_id: str, resource_id: str) -> bool:
        """Check if a snapshot exists for this resource in this session."""
        lock = self._acquire_session_lock(session_id)
        try:
            return self._has_snapshot_unlocked(session_id, resource_id)
        finally:
            lock.release()

    def find_memory_snapshot_by_uri(self, session_id: str, uri: str) -> Optional[str]:
        """
        Find an existing memory content snapshot for a given URI.
        
        When a memory is updated multiple times in one session, each update
        creates a new memory_id (version chain: id=1 → id=5 → id=12 → ...).
        The snapshot resource_id is "memory:{id}", so a naive has_snapshot()
        check on the new id misses the existing snapshot for the old id.
        
        This method scans the manifest for any "memory" type snapshot whose
        stored URI matches the given one, ensuring only ONE content snapshot
        per URI per session regardless of how many updates occur.
        
        Args:
            session_id: Session identifier
            uri: The memory URI (e.g. "core://foo/bar")
            
        Returns:
            The resource_id of the existing snapshot (e.g. "memory:1"),
            or None if no matching snapshot exists.
        """
        lock = self._acquire_session_lock(session_id)
        try:
            return self._find_memory_snapshot_by_uri_unlocked(session_id, uri)
        finally:
            lock.release()
    
    def create_snapshot(
        self,
        session_id: str,
        resource_id: str,
        resource_type: str,
        snapshot_data: Dict[str, Any],
        force: bool = False
    ) -> bool:
        """
        Create a snapshot for a resource.
        
        IMPORTANT: This should be called BEFORE any modification.
        If a snapshot already exists for this resource in this session,
        this call is a no-op (returns False) unless force=True.
        
        Args:
            session_id: Unique session identifier
            resource_id: Resource identifier (e.g., memory URI)
            resource_type: Resource type (e.g., 'memory')
            snapshot_data: The complete resource state to snapshot
            force: If True, overwrite any existing snapshot for this resource.
                   Used by delete operations to ensure the final snapshot
                   reflects the delete rather than an earlier modify.
            
        Returns:
            True if snapshot was created, False if it already existed (and force=False)
        """
        lock = self._acquire_session_lock(session_id)
        try:
            if not force and self._has_snapshot_unlocked(session_id, resource_id):
                return False

            self._ensure_dir_exists(self._get_resources_dir(session_id))
            manifest = self._load_manifest(session_id)

            snapshot = {
                "resource_id": resource_id,
                "resource_type": resource_type,
                "snapshot_time": _utc_iso_now(),
                "data": snapshot_data
            }

            snapshot_path = self._resolve_snapshot_path(session_id, resource_id)
            previous_snapshot: Optional[Dict[str, Any]] = None
            if force and os.path.exists(snapshot_path):
                try:
                    with open(snapshot_path, "r", encoding="utf-8") as f:
                        previous_snapshot_raw = json.load(f)
                    if isinstance(previous_snapshot_raw, dict):
                        previous_snapshot = previous_snapshot_raw
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    previous_snapshot = None

            if (
                resource_type == "memory"
                and not force
                and isinstance(snapshot_data.get("uri"), str)
                and self._find_memory_snapshot_by_uri_unlocked(
                    session_id, str(snapshot_data["uri"])
                ) is not None
            ):
                return False

            self._enforce_session_snapshot_limits(
                session_id=session_id,
                manifest=manifest,
                resource_id=resource_id,
                projected_snapshot_bytes=self._estimate_json_size_bytes(snapshot),
            )

            self._atomic_write_json(snapshot_path, snapshot)

            try:
                manifest["resources"][resource_id] = {
                    "resource_type": resource_type,
                    "snapshot_time": snapshot["snapshot_time"],
                    "operation_type": snapshot_data.get("operation_type", "modify"),
                    "file": os.path.basename(snapshot_path),
                    "uri": snapshot_data.get("uri")
                }
                self._save_manifest(session_id, manifest)
            except Exception:
                if previous_snapshot is not None:
                    self._atomic_write_json(snapshot_path, previous_snapshot)
                elif not force:
                    _force_remove(snapshot_path)
                raise

            return True
        finally:
            lock.release()
    
    def get_snapshot(self, session_id: str, resource_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a snapshot for a resource.
        
        Returns:
            The snapshot data, or None if not found
        """
        lock = self._acquire_session_lock(session_id)
        try:
            manifest = self._load_manifest(session_id)
            resource_meta = manifest.get("resources", {}).get(resource_id)

            if resource_meta and resource_meta.get("file"):
                snapshot_path = os.path.join(
                    self._get_resources_dir(session_id),
                    resource_meta["file"]
                )
            else:
                snapshot_path = self._resolve_snapshot_path(session_id, resource_id)

            if not os.path.exists(snapshot_path):
                return None

            with open(snapshot_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        finally:
            lock.release()
    
    def list_sessions(self) -> List[Dict[str, Any]]:
        """
        List all sessions with snapshots.
        
        Returns:
            List of session metadata (id, created_at, resource_count)
        """
        sessions = []

        if not os.path.exists(self.snapshot_dir):
            return sessions

        for entry in os.scandir(self.snapshot_dir):
            session_id = entry.name
            if session_id.startswith("."):
                continue
            if not entry.is_dir():
                continue
            try:
                lock = self._acquire_session_lock(
                    session_id,
                    timeout_seconds=self.list_lock_timeout_seconds,
                )
            except RuntimeError:
                continue
            remove_lock_file = False
            try:
                session_dir = self._get_session_dir(session_id)
                if not os.path.isdir(session_dir):
                    remove_lock_file = True
                    continue
                manifest = self._load_manifest(session_id)
                resource_count = len(manifest.get("resources", {}))

                if resource_count == 0:
                    self._clear_session_unlocked(session_id)
                    remove_lock_file = True
                    continue

                sessions.append(
                    self._session_storage_record_unlocked(session_id, manifest)
                )
            finally:
                lock.release()
                if remove_lock_file:
                    self._remove_session_lock_file(session_id)

        sessions.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        return sessions
    
    def list_snapshots(self, session_id: str) -> List[Dict[str, Any]]:
        """
        List all snapshots in a session.
        
        Returns:
            List of snapshot metadata (resource_id, resource_type, snapshot_time, operation_type)
        """
        lock = self._acquire_session_lock(session_id)
        try:
            manifest = self._load_manifest(session_id)
            snapshots = []

            for resource_id, meta in manifest.get("resources", {}).items():
                file_name = meta.get("file") if isinstance(meta, dict) else None
                snapshot_path = (
                    os.path.join(self._get_resources_dir(session_id), str(file_name))
                    if file_name
                    else self._resolve_snapshot_path(session_id, resource_id)
                )
                file_bytes = (
                    os.path.getsize(snapshot_path)
                    if os.path.exists(snapshot_path)
                    else 0
                )
                snapshots.append({
                    "resource_id": resource_id,
                    "resource_type": meta.get("resource_type"),
                    "snapshot_time": meta.get("snapshot_time"),
                    "operation_type": meta.get("operation_type", "modify"),
                    "uri": meta.get("uri"),
                    "file_bytes": file_bytes,
                    "age_days": self._age_days(
                        datetime.now(timezone.utc),
                        _parse_iso_datetime(meta.get("snapshot_time")),
                    ),
                })

            return snapshots
        finally:
            lock.release()
    
    def delete_snapshot(self, session_id: str, resource_id: str) -> bool:
        """
        Delete a specific snapshot.
        
        Returns:
            True if deleted, False if not found
        """
        lock = self._acquire_session_lock(session_id)
        remove_lock_file = False
        try:
            manifest = self._load_manifest(session_id)
            resource_meta = manifest.get("resources", {}).get(resource_id)

            if resource_meta and resource_meta.get("file"):
                snapshot_path = os.path.join(
                    self._get_resources_dir(session_id),
                    resource_meta["file"]
                )
            else:
                snapshot_path = self._resolve_snapshot_path(session_id, resource_id)

            if not os.path.exists(snapshot_path):
                return False

            if resource_id in manifest.get("resources", {}):
                del manifest["resources"][resource_id]

                if not manifest["resources"]:
                    self._clear_session_unlocked(session_id)
                    remove_lock_file = True
                else:
                    self._save_manifest(session_id, manifest)
                    _force_remove(snapshot_path)
            else:
                _force_remove(snapshot_path)

            return True
        finally:
            lock.release()
            if remove_lock_file:
                self._remove_session_lock_file(session_id)
    
    def clear_session(self, session_id: str) -> int:
        """
        Delete all snapshots in a session.
        
        Returns:
            Number of snapshots deleted
        """
        lock = self._acquire_session_lock(session_id)
        try:
            return self._clear_session_unlocked(session_id)
        finally:
            lock.release()
            if not os.path.exists(self._get_session_dir(session_id)):
                self._remove_session_lock_file(session_id)


# Global singleton
_snapshot_manager: Optional[SnapshotManager] = None
_snapshot_manager_lock = threading.Lock()


def get_snapshot_manager() -> SnapshotManager:
    """Get the global SnapshotManager instance."""
    global _snapshot_manager
    if _snapshot_manager is None:
        with _snapshot_manager_lock:
            if _snapshot_manager is None:
                _snapshot_manager = SnapshotManager()
    return _snapshot_manager
