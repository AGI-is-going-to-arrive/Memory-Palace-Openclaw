from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def test_posix_stdio_wrapper_prefers_runtime_env_file_over_inherited_model_values() -> None:
    if os.name == "nt":
        return

    repo_root = Path(__file__).resolve().parents[2]
    wrapper_path = repo_root / "scripts" / "run_memory_palace_mcp_stdio.sh"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        runtime_env = tmp_path / "runtime.env"
        runtime_env.write_text(
            "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b\n",
            encoding="utf-8",
        )
        output_path = tmp_path / "captured-env.json"
        fake_python = tmp_path / "fake-python"
        fake_python.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, os, sys",
                    "payload = {",
                    "  'openai_model': os.environ.get('OPENAI_MODEL'),",
                    "  'openai_api_key': os.environ.get('OPENAI_API_KEY'),",
                    "  'database_url': os.environ.get('DATABASE_URL'),",
                    "  'effective_profile': os.environ.get('OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE'),",
                    "}",
                    "with open(os.environ['WRAPPER_TEST_OUTPUT'], 'w', encoding='utf-8') as handle:",
                    "    json.dump(payload, handle)",
                    "sys.exit(0)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

        env = os.environ.copy()
        env.update(
            {
                "OPENCLAW_MEMORY_PALACE_ENV_FILE": str(runtime_env),
                "OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON": str(fake_python),
                "OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT": str(tmp_path),
                "WRAPPER_TEST_OUTPUT": str(output_path),
                "OPENAI_MODEL": "leaked-model",
                "OPENAI_API_KEY": "leaked-key",
                "DATABASE_URL": "sqlite+aiosqlite:////tmp/leaked.db",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )

        completed = subprocess.run(
            ["bash", str(wrapper_path)],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["effective_profile"] == "b"
    assert payload["openai_model"] is None
    assert payload["openai_api_key"] is None
    assert payload["database_url"] != "sqlite+aiosqlite:////tmp/leaked.db"
    assert str(tmp_path / "data" / "memory-palace.db").replace("\\", "/") in payload["database_url"]


def test_posix_stdio_wrapper_rejects_blocked_process_env_keys_from_runtime_env() -> None:
    if os.name == "nt":
        return

    repo_root = Path(__file__).resolve().parents[2]
    wrapper_path = repo_root / "scripts" / "run_memory_palace_mcp_stdio.sh"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        runtime_env = tmp_path / "runtime.env"
        runtime_env.write_text(
            "\n".join(
                [
                    "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b",
                    "PATH=/tmp/poisoned-path",
                    "LD_PRELOAD=/tmp/poisoned.so",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        output_path = tmp_path / "captured-env.json"
        fake_python = tmp_path / "fake-python"
        fake_python.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, os, sys",
                    "payload = {",
                    "  'path': os.environ.get('PATH'),",
                    "  'ld_preload': os.environ.get('LD_PRELOAD'),",
                    "}",
                    "with open(os.environ['WRAPPER_TEST_OUTPUT'], 'w', encoding='utf-8') as handle:",
                    "    json.dump(payload, handle)",
                    "sys.exit(0)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

        inherited_path = os.environ.get("PATH", "")
        env = os.environ.copy()
        env.update(
            {
                "OPENCLAW_MEMORY_PALACE_ENV_FILE": str(runtime_env),
                "OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON": str(fake_python),
                "OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT": str(tmp_path),
                "WRAPPER_TEST_OUTPUT": str(output_path),
                "PATH": inherited_path,
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
            }
        )
        env.pop("LD_PRELOAD", None)

        completed = subprocess.run(
            ["bash", str(wrapper_path)],
            cwd=str(repo_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert completed.returncode == 0, (completed.stdout, completed.stderr)
        payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert payload["path"] == inherited_path
    assert payload["ld_preload"] is None
