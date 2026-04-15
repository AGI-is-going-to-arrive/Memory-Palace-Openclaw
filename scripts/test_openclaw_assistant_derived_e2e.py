#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_assistant_derived_e2e as assistant_e2e


class OpenClawAssistantDerivedE2ETests(unittest.TestCase):
    def test_command_with_gateway_token_appends_token_for_agent_commands(self) -> None:
        env = {
            "OPENCLAW_GATEWAY_URL": "ws://127.0.0.1:18080",
            "OPENCLAW_GATEWAY_TOKEN": "phase45-token",
        }

        with patch.object(
            assistant_e2e,
            "_command_supports_token_flag",
            return_value=True,
        ):
            command = assistant_e2e.command_with_gateway_token(
                "openclaw",
                env=env,
                args=["agent", "--agent", "main", "--message", "hello", "--json"],
            )

        self.assertEqual(command[1:-2], ["agent", "--agent", "main", "--message", "hello", "--json"])
        self.assertEqual(command[-2:], ["--token", "phase45-token"])

    def test_command_with_gateway_token_skips_unsupported_agent_flag(self) -> None:
        env = {
            "OPENCLAW_GATEWAY_URL": "ws://127.0.0.1:18080",
            "OPENCLAW_GATEWAY_TOKEN": "phase45-token",
        }

        with patch.object(
            assistant_e2e,
            "_command_supports_token_flag",
            return_value=False,
        ):
            command = assistant_e2e.command_with_gateway_token(
                "openclaw",
                env=env,
                args=["agent", "--agent", "main", "--message", "hello", "--json"],
            )

        self.assertEqual(command[1:], ["agent", "--agent", "main", "--message", "hello", "--json"])

    def test_command_with_gateway_token_keeps_gateway_run_untouched(self) -> None:
        env = {
            "OPENCLAW_GATEWAY_URL": "ws://127.0.0.1:18080",
            "OPENCLAW_GATEWAY_TOKEN": "phase45-token",
        }

        command = assistant_e2e.command_with_gateway_token(
            "openclaw",
            env=env,
            args=["gateway", "run", "--port", "18080"],
        )

        self.assertEqual(command[1:], ["gateway", "run", "--port", "18080"])

    def test_run_forwards_gateway_token_when_gateway_url_is_present(self) -> None:
        captured_commands: list[list[str]] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            with (
                patch.object(assistant_e2e.subprocess, "run") as run_mock,
                patch.object(
                    assistant_e2e,
                    "_command_supports_token_flag",
                    return_value=True,
                ),
            ):
                run_mock.return_value = subprocess.CompletedProcess(
                    ["openclaw"],
                    0,
                    stdout=b"{}",
                    stderr=b"",
                )
                assistant_e2e.run(
                    ["openclaw", "agent", "--agent", "main", "--message", "hello", "--json"],
                    env={
                        "OPENCLAW_GATEWAY_URL": "ws://127.0.0.1:18080",
                        "OPENCLAW_GATEWAY_TOKEN": "phase45-token",
                    },
                    cwd=Path(tmp_dir),
                )
                captured_commands.append(list(run_mock.call_args.args[0]))

        self.assertEqual(captured_commands[0][-2:], ["--token", "phase45-token"])


if __name__ == "__main__":
    unittest.main()
