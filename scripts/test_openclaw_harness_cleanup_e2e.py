#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import openclaw_command_new_e2e as command_new
import openclaw_assistant_derived_e2e as assistant_derived
import openclaw_host_bridge_e2e as host_bridge
import openclaw_profile_memory_e2e as profile_memory


class HarnessCleanupE2ETests(unittest.TestCase):
    def test_host_bridge_build_temp_config_does_not_inline_runtime_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "gateway": {"auth": {"token": "demo-token"}},
                        "agents": {"defaults": {}},
                        "plugins": {
                            "entries": {
                                "memory-palace": {
                                    "config": {
                                        "stdio": {
                                            "env": {
                                                "EXISTING_FLAG": "keep-me",
                                            }
                                        }
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime_env.write_text(
                (
                    "DATABASE_URL=sqlite+aiosqlite:///tmp/demo.db\n"
                    "OPENAI_MODEL=gpt-5.4\n"
                    "OPENAI_API_KEY=super-secret\n"
                ),
                encoding="utf-8",
            )

            payload = host_bridge.build_temp_openclaw_config(base_config, runtime_env, workspace_dir)

        env_block = payload["plugins"]["entries"]["memory-palace"]["config"]["stdio"]["env"]
        self.assertEqual(env_block["EXISTING_FLAG"], "keep-me")
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"], str(runtime_env))
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR"], str(workspace_dir))
        self.assertNotIn("OPENAI_API_KEY", env_block)
        self.assertNotIn("OPENAI_MODEL", env_block)
        self.assertNotIn("DATABASE_URL", env_block)

    def test_assistant_derived_build_temp_config_does_not_inline_runtime_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "gateway": {"auth": {"token": "demo-token"}},
                        "agents": {"defaults": {}},
                        "plugins": {
                            "entries": {
                                "memory-palace": {
                                    "config": {
                                        "stdio": {
                                            "env": {
                                                "EXISTING_FLAG": "keep-me",
                                            }
                                        }
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime_env.write_text(
                (
                    "DATABASE_URL=sqlite+aiosqlite:///tmp/demo.db\n"
                    "OPENAI_MODEL=gpt-5.4\n"
                    "OPENAI_API_KEY=super-secret\n"
                ),
                encoding="utf-8",
            )

            payload = assistant_derived.build_temp_openclaw_config(base_config, runtime_env, workspace_dir)

        env_block = payload["plugins"]["entries"]["memory-palace"]["config"]["stdio"]["env"]
        self.assertEqual(env_block["EXISTING_FLAG"], "keep-me")
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"], str(runtime_env))
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR"], str(workspace_dir))
        self.assertNotIn("OPENAI_API_KEY", env_block)
        self.assertNotIn("OPENAI_MODEL", env_block)
        self.assertNotIn("DATABASE_URL", env_block)

    def test_profile_memory_build_temp_config_does_not_inline_runtime_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "gateway": {"auth": {"token": "demo-token"}},
                        "agents": {"defaults": {}},
                        "plugins": {
                            "entries": {
                                "memory-palace": {
                                    "config": {
                                        "stdio": {
                                            "env": {
                                                "EXISTING_FLAG": "keep-me",
                                            }
                                        }
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime_env.write_text(
                (
                    "DATABASE_URL=sqlite+aiosqlite:///tmp/demo.db\n"
                    "OPENAI_MODEL=gpt-5.4\n"
                    "OPENAI_API_KEY=super-secret\n"
                ),
                encoding="utf-8",
            )

            payload = profile_memory.build_temp_openclaw_config(base_config, runtime_env, workspace_dir)

        env_block = payload["plugins"]["entries"]["memory-palace"]["config"]["stdio"]["env"]
        self.assertEqual(env_block["EXISTING_FLAG"], "keep-me")
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"], str(runtime_env))
        self.assertNotIn("OPENAI_API_KEY", env_block)
        self.assertNotIn("OPENAI_MODEL", env_block)
        self.assertNotIn("DATABASE_URL", env_block)

    def test_command_new_build_temp_config_does_not_inline_runtime_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "gateway": {"auth": {"token": "demo-token"}},
                        "agents": {"defaults": {}},
                        "plugins": {
                            "entries": {
                                "memory-palace": {
                                    "config": {
                                        "stdio": {
                                            "env": {
                                                "EXISTING_FLAG": "keep-me",
                                            }
                                        }
                                    }
                                }
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime_env.write_text(
                (
                    "DATABASE_URL=sqlite+aiosqlite:///tmp/demo.db\n"
                    "OPENAI_MODEL=gpt-5.4\n"
                    "OPENAI_API_KEY=super-secret\n"
                ),
                encoding="utf-8",
            )

            payload = command_new.build_temp_openclaw_config(base_config, runtime_env, workspace_dir)

        env_block = payload["plugins"]["entries"]["memory-palace"]["config"]["stdio"]["env"]
        self.assertEqual(env_block["EXISTING_FLAG"], "keep-me")
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"], str(runtime_env))
        self.assertNotIn("OPENAI_API_KEY", env_block)
        self.assertNotIn("OPENAI_MODEL", env_block)
        self.assertNotIn("DATABASE_URL", env_block)

    def test_host_bridge_main_cleans_tmp_root_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            base_config = root / "base-openclaw.json"
            report_path = root / "report.json"
            run_root = root / "host-bridge-run"
            run_root.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            with patch.object(
                host_bridge,
                "parse_args",
                return_value=argparse.Namespace(openclaw_bin="openclaw", report=str(report_path)),
            ), patch.object(
                host_bridge,
                "resolve_current_openclaw_config",
                return_value=base_config,
            ), patch.object(
                host_bridge.tempfile,
                "mkdtemp",
                return_value=str(run_root),
            ), patch.object(
                host_bridge.smoke,
                "build_profile_env",
                return_value=None,
            ), patch.object(
                host_bridge,
                "build_temp_openclaw_config",
                return_value={"gateway": {"auth": {"token": "demo-token"}}},
            ), patch.object(
                host_bridge.subprocess,
                "Popen",
                return_value=MagicMock(),
            ), patch.object(
                host_bridge,
                "wait_for_gateway",
                side_effect=RuntimeError("simulated gateway failure"),
            ), patch.object(
                host_bridge,
                "stop_gateway_process",
                return_value=None,
            ):
                exit_code = host_bridge.main()
                payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(run_root.exists())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["tmp_root"], str(run_root))

    def test_assistant_derived_main_cleans_tmp_root_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            base_config = root / "base-openclaw.json"
            report_path = root / "report.json"
            run_root = root / "assistant-derived-run"
            run_root.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            with patch.object(
                assistant_derived,
                "parse_args",
                return_value=argparse.Namespace(openclaw_bin="openclaw", report=str(report_path)),
            ), patch.object(
                assistant_derived,
                "resolve_current_openclaw_config",
                return_value=base_config,
            ), patch.object(
                assistant_derived.tempfile,
                "mkdtemp",
                return_value=str(run_root),
            ), patch.object(
                assistant_derived.smoke,
                "build_profile_env",
                return_value=None,
            ), patch.object(
                assistant_derived,
                "build_temp_openclaw_config",
                return_value={"gateway": {"auth": {"token": "demo-token"}}},
            ), patch.object(
                assistant_derived.subprocess,
                "Popen",
                return_value=MagicMock(),
            ), patch.object(
                assistant_derived,
                "wait_for_gateway",
                side_effect=RuntimeError("simulated gateway failure"),
            ), patch.object(
                assistant_derived,
                "stop_gateway_process",
                return_value=None,
            ):
                exit_code = assistant_derived.main()
                payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(run_root.exists())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["tmp_root"], str(run_root))

    def test_profile_memory_main_cleans_tmp_root_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            base_config = root / "base-openclaw.json"
            report_path = root / "report.json"
            run_root = root / "profile-memory-run"
            run_root.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            with patch.object(
                profile_memory,
                "DEFAULT_REPORT_PATH",
                report_path,
            ), patch.object(
                profile_memory,
                "resolve_current_openclaw_config",
                return_value=base_config,
            ), patch.object(
                profile_memory.tempfile,
                "mkdtemp",
                return_value=str(run_root),
            ), patch.object(
                profile_memory.smoke,
                "build_profile_env",
                return_value=None,
            ), patch.object(
                profile_memory,
                "build_temp_openclaw_config",
                return_value={"gateway": {"auth": {"token": "demo-token"}}},
            ), patch.object(
                profile_memory.subprocess,
                "Popen",
                return_value=MagicMock(),
            ), patch.object(
                profile_memory,
                "wait_for_gateway",
                side_effect=RuntimeError("simulated gateway failure"),
            ), patch.object(
                profile_memory,
                "stop_gateway_process",
                return_value=None,
            ):
                exit_code = profile_memory.main()
                payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(run_root.exists())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["tmp_root"], str(run_root))

    def test_command_new_main_cleans_tmp_root_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            base_config = root / "base-openclaw.json"
            report_path = root / "report.json"
            run_root = root / "command-new-run"
            run_root.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            with patch.object(
                command_new,
                "DEFAULT_REPORT_PATH",
                report_path,
            ), patch.object(
                command_new,
                "resolve_current_openclaw_config",
                return_value=base_config,
            ), patch.object(
                command_new.tempfile,
                "mkdtemp",
                return_value=str(run_root),
            ), patch.object(
                command_new.smoke,
                "build_profile_env",
                return_value=None,
            ), patch.object(
                command_new,
                "build_temp_openclaw_config",
                return_value={"gateway": {"auth": {"token": "demo-token"}}},
            ), patch.object(
                command_new.subprocess,
                "Popen",
                return_value=MagicMock(),
            ), patch.object(
                command_new,
                "wait_for_gateway",
                side_effect=RuntimeError("simulated gateway failure"),
            ), patch.object(
                command_new,
                "stop_gateway_process",
                return_value=None,
            ):
                exit_code = command_new.main()
                payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(run_root.exists())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["tmp_root"], str(run_root))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
