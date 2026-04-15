import json
from pathlib import Path

import pytest

from security import import_guard
from security.import_guard import ExternalImportGuard, ExternalImportGuardConfig


class _FixedClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _build_guard(
    tmp_path: Path,
    *,
    max_total_bytes: int = 1024,
    max_files: int = 10,
    rate_limit_window_seconds: int = 60,
    rate_limit_max_requests: int = 10,
    rate_limit_state_file: Path | None = None,
    rate_limit_state_lock_timeout_seconds: float = 1.0,
    require_shared_rate_limit: bool = False,
    clock=None,
) -> tuple[ExternalImportGuard, Path]:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir(exist_ok=True)
    config = ExternalImportGuardConfig(
        enabled=True,
        allowed_roots=(allowed_root.resolve(),),
        allowed_exts=(".md", ".txt", ".json"),
        max_total_bytes=max_total_bytes,
        max_files=max_files,
        rate_limit_window_seconds=rate_limit_window_seconds,
        rate_limit_max_requests=rate_limit_max_requests,
        rate_limit_state_file=rate_limit_state_file,
        rate_limit_state_lock_timeout_seconds=rate_limit_state_lock_timeout_seconds,
        require_shared_rate_limit=require_shared_rate_limit,
    )
    return ExternalImportGuard(config=config, clock=clock), allowed_root


def test_external_import_guard_allows_safe_batch(tmp_path: Path) -> None:
    guard, allowed_root = _build_guard(tmp_path)
    file_path = allowed_root / "safe.md"
    file_path.write_text("hello-safe", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is True
    assert result["reason"] == "ok"
    assert result["file_count"] == 1
    assert result["total_bytes"] == file_path.stat().st_size
    assert len(result["allowed_files"]) == 1
    assert result["rejected_files"] == []
    assert result["allowed_files"][0]["content"] == "hello-safe"
    assert isinstance(result["allowed_files"][0]["identity"], dict)


def test_external_import_guard_rejects_path_traversal_outside_allowed_roots(
    tmp_path: Path,
) -> None:
    guard, allowed_root = _build_guard(tmp_path)
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    traversal_path = allowed_root / ".." / "outside.txt"

    result = guard.validate_batch(
        file_paths=[traversal_path],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["file_count"] == 0
    assert result["rejected_files"][0]["reason"] == "path_not_allowed"


def test_external_import_guard_rejects_file_swaps_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, allowed_root = _build_guard(tmp_path)
    safe_file = allowed_root / "safe.txt"
    safe_file.write_text("inside", encoding="utf-8")
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")

    original_open = import_guard.os.open

    def _swap_before_open(path, flags, *args, **kwargs):
        if str(path) in {str(safe_file), safe_file.name} and safe_file.exists():
            safe_file.unlink()
            try:
                safe_file.symlink_to(outside_file)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable on this platform: {exc}")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(import_guard.os, "open", _swap_before_open)

    result = guard.validate_batch(
        file_paths=[safe_file],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["rejected_files"][0]["reason"] in {
        "symlink_not_allowed",
        "not_a_file",
    }


def test_external_import_guard_rejects_intermediate_symlink_swaps_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(import_guard.os, "O_NOFOLLOW") or import_guard.os.open not in getattr(
        import_guard.os, "supports_dir_fd", set()
    ):
        pytest.skip("secure directory fd walking is unavailable on this platform")

    guard, allowed_root = _build_guard(tmp_path)
    nested_dir = allowed_root / "nested"
    nested_dir.mkdir()
    safe_file = nested_dir / "safe.txt"
    safe_file.write_text("inside", encoding="utf-8")

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "safe.txt"
    outside_file.write_text("outside", encoding="utf-8")

    original_open = import_guard.os.open
    monkeypatch.setattr(import_guard, "_supports_secure_path_open", lambda: True)

    def _swap_directory_before_open(path, flags, *args, **kwargs):
        if str(path) == "nested" and nested_dir.exists() and not nested_dir.is_symlink():
            safe_file.unlink()
            nested_dir.rmdir()
            try:
                nested_dir.symlink_to(outside_dir, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable on this platform: {exc}")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(import_guard.os, "open", _swap_directory_before_open)

    result = guard.validate_batch(
        file_paths=[safe_file],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["rejected_files"][0]["reason"] in {
        "symlink_not_allowed",
        "not_a_file",
    }


def test_external_import_guard_rejects_directory_swaps_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, allowed_root = _build_guard(tmp_path)
    nested_dir = allowed_root / "nested"
    nested_dir.mkdir()
    safe_file = nested_dir / "safe.txt"
    safe_file.write_text("inside", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "safe.txt"
    outside_file.write_text("outside", encoding="utf-8")
    parked_dir = allowed_root / "nested-original"

    original_abspath = import_guard.os.path.abspath

    def _swap_after_abspath(path_value: str):
        resolved = original_abspath(path_value)
        if str(path_value) == str(safe_file) and nested_dir.exists() and not parked_dir.exists():
            nested_dir.rename(parked_dir)
            try:
                nested_dir.symlink_to(outside_dir, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable on this platform: {exc}")
        return resolved

    monkeypatch.setattr(import_guard.os.path, "abspath", _swap_after_abspath)

    result = guard.validate_batch(
        file_paths=[safe_file],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["rejected_files"][0]["reason"] in {
        "file_changed_during_validation",
        "symlink_not_allowed",
    }


def test_external_import_guard_rejects_resolved_path_escape_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, allowed_root = _build_guard(tmp_path)
    safe_file = allowed_root / "safe.txt"
    safe_file.write_text("inside", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    path_cls = type(safe_file)
    original_resolve = path_cls.resolve

    def _fake_resolve(self: Path, strict: bool = False) -> Path:
        if self == safe_file:
            return outside_dir / "safe.txt"
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(path_cls, "resolve", _fake_resolve)

    result = guard.validate_batch(
        file_paths=[safe_file],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["rejected_files"][0]["reason"] == "path_not_allowed"


def test_external_import_guard_insecure_path_validation_rejects_resolved_escape_before_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, allowed_root = _build_guard(tmp_path)
    nested_dir = allowed_root / "nested"
    nested_dir.mkdir()
    safe_file = nested_dir / "safe.txt"
    safe_file.write_text("inside", encoding="utf-8")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "safe.txt"
    outside_file.write_text("outside", encoding="utf-8")

    path_cls = type(safe_file)
    original_resolve = path_cls.resolve
    monkeypatch.setattr(import_guard, "_supports_secure_path_open", lambda: False)

    def _fake_resolve(self: Path, strict: bool = False) -> Path:
        if self == nested_dir:
            return outside_dir
        if self == safe_file:
            return outside_file
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(path_cls, "resolve", _fake_resolve)

    result = guard.validate_batch(
        file_paths=[safe_file],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["rejected_files"][0]["reason"] == "path_not_allowed"


def test_external_import_guard_insecure_path_validation_rejects_junction_like_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, allowed_root = _build_guard(tmp_path)
    safe_file = allowed_root / "safe.txt"
    safe_file.write_text("inside", encoding="utf-8")

    monkeypatch.setattr(import_guard, "_supports_secure_path_open", lambda: False)
    monkeypatch.setattr(
        import_guard.os.path,
        "isjunction",
        lambda candidate: Path(candidate) == safe_file,
        raising=False,
    )

    result = guard.validate_batch(
        file_paths=[safe_file],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["rejected_files"][0]["reason"] == "symlink_not_allowed"


def test_external_import_guard_rejects_extension_not_in_whitelist(tmp_path: Path) -> None:
    guard, allowed_root = _build_guard(tmp_path)
    bad_file = allowed_root / "payload.exe"
    bad_file.write_text("binary", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[bad_file],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["rejected_files"][0]["reason"] == "extension_not_allowed"


def test_external_import_guard_rejects_when_total_size_exceeds_limit(
    tmp_path: Path,
) -> None:
    guard, allowed_root = _build_guard(tmp_path, max_total_bytes=8, max_files=5)
    first = allowed_root / "a.txt"
    second = allowed_root / "b.txt"
    first.write_text("12345", encoding="utf-8")
    second.write_text("67890", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[first, second],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "max_total_bytes_exceeded"
    assert result["file_count"] == 2
    assert result["total_bytes"] == 10
    assert result["rejected_files"][0]["reason"] == "max_total_bytes_exceeded"


def test_external_import_guard_rejects_single_file_larger_than_total_limit(
    tmp_path: Path,
) -> None:
    guard, allowed_root = _build_guard(tmp_path, max_total_bytes=8, max_files=5)
    oversized = allowed_root / "oversized.txt"
    oversized.write_text("123456789", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[oversized],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["file_count"] == 0
    assert result["total_bytes"] == 0
    assert result["allowed_files"] == []
    assert result["rejected_files"][0]["reason"] == "max_total_bytes_exceeded"


def test_external_import_guard_rejects_oversized_single_file_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard, allowed_root = _build_guard(tmp_path, max_total_bytes=8, max_files=5)
    file_path = allowed_root / "too-large.txt"
    file_path.write_text("1234567890", encoding="utf-8")

    original_fdopen = import_guard.os.fdopen

    class _NoReadHandle:
        def __init__(self, handle) -> None:
            self._handle = handle

        def fileno(self) -> int:
            return self._handle.fileno()

        def read(self) -> bytes:
            raise AssertionError("oversized file should be rejected before read()")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            self._handle.close()
            return False

    def _fdopen_without_read(fd: int, mode: str = "rb"):
        return _NoReadHandle(original_fdopen(fd, mode))

    monkeypatch.setattr(import_guard.os, "fdopen", _fdopen_without_read)

    result = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "file_validation_failed"
    assert result["file_count"] == 0
    assert result["rejected_files"][0]["reason"] == "max_total_bytes_exceeded"


def test_external_import_guard_rejects_when_file_count_exceeds_limit(
    tmp_path: Path,
) -> None:
    guard, allowed_root = _build_guard(tmp_path, max_files=1)
    first = allowed_root / "a.txt"
    second = allowed_root / "b.txt"
    first.write_text("1", encoding="utf-8")
    second.write_text("2", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[first, second],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is False
    assert result["reason"] == "max_files_exceeded"
    assert result["allowed_files"] == []
    assert len(result["rejected_files"]) == 2
    assert all(item["reason"] == "max_files_exceeded" for item in result["rejected_files"])


def test_external_import_guard_hits_rate_limit_and_returns_retry_after_seconds(
    tmp_path: Path,
) -> None:
    fixed_clock = _FixedClock(now=1000.0)
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_window_seconds=30,
        rate_limit_max_requests=1,
        clock=fixed_clock,
    )
    file_path = allowed_root / "safe.txt"
    file_path.write_text("ok", encoding="utf-8")

    first = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-rate",
    )
    second = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-rate",
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["reason"] == "rate_limited"
    assert second["retry_after_seconds"] == 30


def test_external_import_guard_rate_limit_blocks_actor_across_session_rotation(
    tmp_path: Path,
) -> None:
    fixed_clock = _FixedClock(now=1500.0)
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_window_seconds=30,
        rate_limit_max_requests=1,
        clock=fixed_clock,
    )
    file_path = allowed_root / "safe.txt"
    file_path.write_text("ok", encoding="utf-8")

    first = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-a",
    )
    second = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-b",
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["reason"] == "rate_limited"
    assert second["rate_limit"]["scope"] == "actor"
    assert second["rate_limit"]["key"] == "actor-a::*"


def test_external_import_guard_hashes_long_actor_and_session_ids_for_rate_limit_keys(
    tmp_path: Path,
) -> None:
    fixed_clock = _FixedClock(now=1600.0)
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_window_seconds=30,
        rate_limit_max_requests=1,
        clock=fixed_clock,
    )
    file_path = allowed_root / "safe.txt"
    file_path.write_text("ok", encoding="utf-8")
    actor_id = "actor-" + ("x" * 512)
    session_id = "session-" + ("y" * 512)

    result = guard.validate_batch(
        file_paths=[file_path],
        actor_id=actor_id,
        session_id=session_id,
    )

    assert result["ok"] is True
    keys = result["rate_limit"]["keys"]
    assert all(actor_id not in key for key in keys)
    assert all(session_id not in key for key in keys)
    assert max(len(key) for key in keys) < 200
    assert result["rate_limit"]["key"].startswith("actor-sha256:")


def test_external_import_guard_rejects_when_shared_rate_limit_required_without_state_file(
    tmp_path: Path,
) -> None:
    fixed_clock = _FixedClock(now=1750.0)
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_window_seconds=30,
        rate_limit_max_requests=1,
        require_shared_rate_limit=True,
        clock=fixed_clock,
    )
    file_path = allowed_root / "safe.txt"
    file_path.write_text("ok", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-a",
    )

    assert result["ok"] is False
    assert result["reason"] == "rate_limit_shared_state_required"
    assert result["rate_limit_storage"] == "process_memory"
    assert isinstance(result.get("config_errors"), list)


def test_external_import_guard_rate_limit_state_file_blocks_across_instances(
    tmp_path: Path,
) -> None:
    fixed_clock = _FixedClock(now=2000.0)
    state_file = tmp_path / "rate_limit_state.json"
    guard_a, allowed_root = _build_guard(
        tmp_path,
        rate_limit_window_seconds=30,
        rate_limit_max_requests=1,
        rate_limit_state_file=state_file,
        clock=fixed_clock,
    )
    guard_b, _ = _build_guard(
        tmp_path,
        rate_limit_window_seconds=30,
        rate_limit_max_requests=1,
        rate_limit_state_file=state_file,
        clock=fixed_clock,
    )
    file_path = allowed_root / "safe.txt"
    file_path.write_text("ok", encoding="utf-8")

    first = guard_a.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-shared",
    )
    second = guard_b.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-shared",
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["reason"] == "rate_limited"
    assert second["rate_limit_storage"] == "state_file"


def test_external_import_guard_degrades_network_state_file_to_process_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "rate_limit_state.json"
    monkeypatch.setattr(
        import_guard,
        "warn_if_unreliable_file_lock_path",
        lambda path, *, label, log=None: (True, "nfs4"),
    )
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_state_file=state_file,
    )
    file_path = allowed_root / "safe.md"
    file_path.write_text("hello-safe", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is True
    assert result["rate_limit_storage"] == "process_memory"
    assert result["policy"]["rate_limit_storage"] == "process_memory"


def test_external_import_guard_keeps_shared_rate_limit_on_network_state_file_when_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_file = tmp_path / "rate_limit_state.json"
    monkeypatch.setattr(
        import_guard,
        "warn_if_unreliable_file_lock_path",
        lambda path, *, label, log=None: (True, "nfs4"),
    )
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_state_file=state_file,
        require_shared_rate_limit=True,
    )
    file_path = allowed_root / "safe.md"
    file_path.write_text("hello-safe", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert result["ok"] is True
    assert result["rate_limit_storage"] == "state_file"
    assert result["policy"]["rate_limit_storage"] == "state_file"


def test_external_import_guard_state_file_lock_timeout_is_configurable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_clock = _FixedClock(now=2250.0)
    state_file = tmp_path / "rate_limit_state.json"
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_window_seconds=30,
        rate_limit_max_requests=1,
        rate_limit_state_file=state_file,
        rate_limit_state_lock_timeout_seconds=2.75,
        clock=fixed_clock,
    )
    file_path = allowed_root / "safe.txt"
    file_path.write_text("ok", encoding="utf-8")
    captured: dict[str, float | str] = {}

    class _TimeoutFileLock:
        def __init__(self, lock_path: str, timeout: float) -> None:
            captured["lock_path"] = lock_path
            captured["timeout"] = timeout

        def __enter__(self):
            raise import_guard.Timeout("forced lock timeout")

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    monkeypatch.setattr(import_guard, "FileLock", _TimeoutFileLock)

    result = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-lock-timeout",
    )

    assert captured["lock_path"] == str(Path(f"{state_file}.lock"))
    assert captured["timeout"] == pytest.approx(2.75)
    assert result["ok"] is False
    assert result["reason"] == "rate_limit_state_unavailable"
    assert result["rate_limit_state_error"] == "state_lock_timeout"
    assert result["policy"]["rate_limit_state_lock_timeout_seconds"] == pytest.approx(
        2.75
    )


def test_external_import_guard_state_file_prunes_stale_session_buckets(
    tmp_path: Path,
) -> None:
    fixed_clock = _FixedClock(now=5000.0)
    state_file = tmp_path / "rate_limit_state.json"
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_window_seconds=1,
        rate_limit_max_requests=100,
        rate_limit_state_file=state_file,
        clock=fixed_clock,
    )
    file_path = allowed_root / "safe.txt"
    file_path.write_text("ok", encoding="utf-8")

    for index in range(5):
        result = guard.validate_batch(
            file_paths=[file_path],
            actor_id="actor-a",
            session_id=f"session-{index}",
        )
        assert result["ok"] is True
        fixed_clock.now += 2.0

    payload = json.loads(state_file.read_text(encoding="utf-8"))
    actor_bucket = payload.get("actor-a::*")
    session_keys = sorted(
        key for key in payload.keys() if key.startswith("actor-a::session-")
    )

    assert isinstance(actor_bucket, list)
    assert len(actor_bucket) == 1
    assert session_keys == ["actor-a::session-4"]


def test_external_import_guard_prunes_state_file_bucket_count(
    tmp_path: Path,
) -> None:
    guard, _ = _build_guard(tmp_path)
    payload = {f"actor-{index}::*": [1000.0 + index] for index in range(4105)}
    payload["protected::*"] = [9999.0]

    guard._prune_rate_limit_state_payload(  # type: ignore[attr-defined]
        payload=payload,
        now=6000.0,
        window_seconds=10_000.0,
        protected_keys={"protected::*"},
    )

    assert "protected::*" in payload
    assert len(payload) == 4097
    assert "actor-0::*" not in payload
    assert "actor-4104::*" in payload


def test_external_import_guard_fails_closed_when_state_file_is_not_regular_file(
    tmp_path: Path,
) -> None:
    fixed_clock = _FixedClock(now=3000.0)
    state_dir = tmp_path / "rate_limit_state_dir"
    state_dir.mkdir()
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_window_seconds=30,
        rate_limit_max_requests=1,
        rate_limit_state_file=state_dir,
        clock=fixed_clock,
    )
    file_path = allowed_root / "safe.txt"
    file_path.write_text("ok", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-state-file-dir",
    )

    assert result["ok"] is False
    assert result["reason"] == "rate_limit_state_unavailable"
    assert result["rate_limit_state_error"] == "state_file_not_regular_file"


def test_external_import_guard_fails_closed_when_state_bucket_has_nan_timestamp(
    tmp_path: Path,
) -> None:
    fixed_clock = _FixedClock(now=4000.0)
    state_file = tmp_path / "rate_limit_state.json"
    state_file.write_text(
        '{"actor-a::session-nan": ["nan"]}',
        encoding="utf-8",
    )
    guard, allowed_root = _build_guard(
        tmp_path,
        rate_limit_window_seconds=30,
        rate_limit_max_requests=1,
        rate_limit_state_file=state_file,
        clock=fixed_clock,
    )
    file_path = allowed_root / "safe.txt"
    file_path.write_text("ok", encoding="utf-8")

    result = guard.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-nan",
    )

    assert result["ok"] is False
    assert result["reason"] == "rate_limit_state_unavailable"
    assert result["rate_limit_state_error"] == "state_bucket_invalid_timestamp"


def test_external_import_guard_from_env_reads_lock_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTERNAL_IMPORT_ENABLED", "true")
    monkeypatch.setenv("EXTERNAL_IMPORT_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("EXTERNAL_IMPORT_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS", "3.5")

    config = ExternalImportGuardConfig.from_env()

    assert config.enabled is True
    assert config.allowed_roots == (tmp_path.resolve(),)
    assert config.rate_limit_state_lock_timeout_seconds == pytest.approx(3.5)


def test_external_import_guard_is_fail_closed_when_disabled_or_no_allowed_roots(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "note.txt"
    file_path.write_text("x", encoding="utf-8")

    disabled = ExternalImportGuard(
        config=ExternalImportGuardConfig(
            enabled=False,
            allowed_roots=(tmp_path.resolve(),),
            allowed_exts=(".txt",),
        )
    )
    no_roots = ExternalImportGuard(
        config=ExternalImportGuardConfig(
            enabled=True,
            allowed_roots=(),
            allowed_exts=(".txt",),
        )
    )

    disabled_result = disabled.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-1",
    )
    no_roots_result = no_roots.validate_batch(
        file_paths=[file_path],
        actor_id="actor-a",
        session_id="session-1",
    )

    assert disabled_result["ok"] is False
    assert disabled_result["reason"] == "external_import_disabled"
    assert no_roots_result["ok"] is False
    assert no_roots_result["reason"] == "allowed_roots_not_configured"
