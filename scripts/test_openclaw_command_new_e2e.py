#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_command_new_e2e as command_new


class CommandNewE2ETests(unittest.TestCase):
    def test_build_temp_openclaw_config_keeps_internal_hooks_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "hooks": {"internal": {"enabled": False}},
                        "agents": {"defaults": {}},
                        "plugins": {"entries": {"memory-palace": {"config": {"stdio": {"env": {}}}}}},
                    }
                ),
                encoding="utf-8",
            )
            runtime_env.write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")

            payload = command_new.build_temp_openclaw_config(base_config, runtime_env, workspace_dir)

        self.assertTrue(payload["hooks"]["internal"]["enabled"])
        self.assertEqual(payload["plugins"]["entries"]["memory-palace"]["config"]["profileMemory"], {"enabled": False})
        self.assertEqual(payload["plugins"]["entries"]["memory-palace"]["config"]["hostBridge"], {"enabled": False})
        self.assertEqual(
            payload["plugins"]["entries"]["memory-palace"]["config"]["capturePipeline"],
            {"captureAssistantDerived": False},
        )
        self.assertEqual(payload["plugins"]["entries"]["memory-palace"]["config"]["smartExtraction"], {"enabled": False})
        self.assertEqual(payload["plugins"]["entries"]["memory-palace"]["config"]["reconcile"], {"enabled": False})
        env_block = payload["plugins"]["entries"]["memory-palace"]["config"]["stdio"]["env"]
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"], str(runtime_env))
        self.assertNotIn("OPENAI_API_KEY", env_block)

    def test_select_reflection_result_prefers_reflection_lane(self) -> None:
        results = [
            {"path": "memory-palace/core/profile/workflow.md"},
            {"path": "memory-palace/core/reflection/agent-alpha/2026/03/18/item.md"},
        ]

        selected = command_new.select_reflection_result(results)

        self.assertEqual(
            selected,
            {"path": "memory-palace/core/reflection/agent-alpha/2026/03/18/item.md"},
        )

    def test_wait_for_reflection_result_falls_back_to_command_new_query(self) -> None:
        dummy_result = subprocess.CompletedProcess(["openclaw"], 0, stdout="{}", stderr="")
        reflection_payload = {
            "results": [
                {"path": "memory-palace/core/reflection/agent-alpha/2026/03/18/item.md"},
            ]
        }

        with patch.object(command_new, "run", return_value=dummy_result) as run_mock, patch.object(
            command_new,
            "parse_json_output",
            side_effect=[
                {"ok": True},
                {"results": []},
                reflection_payload,
            ],
        ):
            index_result, search_result, reflection_result = command_new.wait_for_reflection_result(
                "command-new-1234",
                env={},
                timeout_seconds=5,
            )

        self.assertEqual(index_result, {"ok": True})
        self.assertEqual(search_result, reflection_payload)
        self.assertEqual(
            reflection_result,
            {"path": "memory-palace/core/reflection/agent-alpha/2026/03/18/item.md"},
        )
        calls = [args[0] for args, _ in run_mock.call_args_list]
        self.assertIn(
            [
                "memory-palace",
                "search",
                "command-new-1234",
                "--include-reflection",
                "--max-results",
                "20",
                "--json",
            ],
            [call[1:] for call in calls],
        )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
