#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_command_new_e2e as command_new
import openclaw_assistant_derived_e2e as assistant_derived
import openclaw_host_bridge_e2e as host_bridge
import openclaw_profile_memory_e2e as profile_memory


class ProviderRetryE2ETests(unittest.TestCase):
    def test_host_bridge_is_transient_agent_failure_detects_unexpected_eof(self) -> None:
        payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [
                    {"text": "unexpected EOF"},
                ],
            },
        }

        self.assertTrue(host_bridge.is_transient_agent_failure(payload))

    def test_assistant_derived_is_transient_agent_failure_detects_unexpected_eof(self) -> None:
        payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [
                    {"text": "unexpected EOF"},
                ],
            },
        }

        self.assertTrue(assistant_derived.is_transient_agent_failure(payload))

    def test_profile_memory_is_transient_agent_failure_detects_unexpected_eof(self) -> None:
        payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [
                    {"text": "unexpected EOF"},
                ],
            },
        }

        self.assertTrue(profile_memory.is_transient_agent_failure(payload))

    def test_command_new_is_transient_agent_failure_detects_unexpected_eof(self) -> None:
        payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [
                    {"text": "unexpected EOF"},
                ],
            },
        }

        self.assertTrue(command_new.is_transient_agent_failure(payload))

    def test_host_bridge_run_agent_message_retries_transient_payloads(self) -> None:
        dummy_result = subprocess.CompletedProcess(["openclaw"], 0, stdout="", stderr="")
        transient_payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [{"text": "unexpected EOF"}],
            },
        }
        success_payload = {
            "summary": "completed",
            "result": {
                "stopReason": "stop",
                "payloads": [{"text": "docs last"}],
            },
        }

        with patch.object(host_bridge, "run", return_value=dummy_result), patch.object(
            host_bridge,
            "parse_json_output",
            side_effect=[transient_payload, success_payload],
        ) as parse_mock, patch.object(host_bridge.time, "sleep") as sleep_mock:
            payload = host_bridge.run_agent_message(
                "openclaw",
                "Do you remember my workflow?",
                env={},
                cwd=Path("."),
            )

        self.assertEqual(payload, success_payload)
        self.assertEqual(parse_mock.call_count, 2)
        sleep_mock.assert_called_once()

    def test_assistant_derived_run_agent_message_retries_transient_payloads(self) -> None:
        dummy_result = subprocess.CompletedProcess(["openclaw"], 0, stdout="", stderr="")
        transient_payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [{"text": "unexpected EOF"}],
            },
        }
        success_payload = {
            "summary": "completed",
            "result": {
                "stopReason": "stop",
                "payloads": [{"text": "code first, tests next"}],
            },
        }

        with patch.object(assistant_derived, "run", return_value=dummy_result), patch.object(
            assistant_derived,
            "parse_json_output",
            side_effect=[transient_payload, success_payload],
        ) as parse_mock, patch.object(assistant_derived.time, "sleep") as sleep_mock:
            payload = assistant_derived.run_agent_message(
                "openclaw",
                "What is the workflow order?",
                env={},
                cwd=Path("."),
            )

        self.assertEqual(payload, success_payload)
        self.assertEqual(parse_mock.call_count, 2)
        sleep_mock.assert_called_once()

    def test_profile_memory_run_agent_message_retries_transient_payloads(self) -> None:
        dummy_result = subprocess.CompletedProcess(["openclaw"], 0, stdout="", stderr="")
        transient_payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [{"text": "unexpected EOF"}],
            },
        }
        success_payload = {
            "summary": "completed",
            "result": {
                "stopReason": "stop",
                "payloads": [{"text": "docs last"}],
            },
        }

        with patch.object(profile_memory, "run", return_value=dummy_result), patch.object(
            profile_memory,
            "parse_json_output",
            side_effect=[transient_payload, success_payload],
        ) as parse_mock, patch.object(profile_memory.time, "sleep") as sleep_mock:
            payload = profile_memory.run_agent_message(
                "Do you remember my workflow?",
                env={},
            )

        self.assertEqual(payload, success_payload)
        self.assertEqual(parse_mock.call_count, 2)
        sleep_mock.assert_called_once()

    def test_command_new_run_agent_message_retries_transient_payloads(self) -> None:
        dummy_result = subprocess.CompletedProcess(["openclaw"], 0, stdout="", stderr="")
        transient_payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [{"text": "unexpected EOF"}],
            },
        }
        success_payload = {
            "summary": "completed",
            "result": {
                "stopReason": "stop",
                "payloads": [{"text": "checkpoint token captured"}],
            },
        }

        with patch.object(command_new, "run", return_value=dummy_result), patch.object(
            command_new,
            "parse_json_output",
            side_effect=[transient_payload, success_payload],
        ) as parse_mock, patch.object(command_new.time, "sleep") as sleep_mock:
            payload = command_new.run_agent_message(
                "Remember this release checkpoint token",
                env={},
            )

        self.assertEqual(payload, success_payload)
        self.assertEqual(parse_mock.call_count, 2)
        sleep_mock.assert_called_once()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
