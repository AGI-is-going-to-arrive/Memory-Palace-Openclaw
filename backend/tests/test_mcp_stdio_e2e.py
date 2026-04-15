from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys


def _load_harness():
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "evaluate_memory_palace_mcp_e2e.py"
    spec = importlib.util.spec_from_file_location("evaluate_memory_palace_mcp_e2e", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_mcp_stdio_e2e_suite_passes() -> None:
    harness = _load_harness()
    results, stderr_output = harness.run_suite_sync()

    failing = [item for item in results if item.status == "FAIL"]
    assert not failing, [(item.name, item.summary, item.details) for item in failing]
    assert "bound to a different event loop" not in stderr_output


def test_live_mcp_stdio_harness_sanitizes_parent_env() -> None:
    harness = _load_harness()
    previous = dict(os.environ)
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/parent.db"
    os.environ["RETRIEVAL_CHUNK_SIZE"] = "999"
    os.environ["WRITE_GUARD_LLM_ENABLED"] = "true"
    os.environ["OPENCLAW_CONFIG_PATH"] = "/tmp/openclaw.json"
    try:
        env = harness._build_suite_env(Path("/tmp/live-mcp.db"))
    finally:
        os.environ.clear()
        os.environ.update(previous)

    assert env["DATABASE_URL"].endswith("/tmp/live-mcp.db")
    assert env["RETRIEVAL_EMBEDDING_BACKEND"] == "none"
    assert env["WRITE_GUARD_LLM_ENABLED"] == "false"
    assert env["SEARCH_DEFAULT_MODE"] == "keyword"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert "OPENCLAW_CONFIG_PATH" not in env
