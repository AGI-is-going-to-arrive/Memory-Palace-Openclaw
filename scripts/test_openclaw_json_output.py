#!/usr/bin/env python3
from __future__ import annotations

import json
import unittest
from subprocess import CompletedProcess

import openclaw_json_output as json_output


class OpenClawJsonOutputTests(unittest.TestCase):
    def test_extract_last_json_from_text_accepts_prefixed_logs(self) -> None:
        payload = json_output.extract_last_json_from_text(
            '[plugins] hook runner initialized with 1 registered hooks\n{"ok": true, "status": "loaded"}'
        )

        self.assertEqual(payload, {"ok": True, "status": "loaded"})

    def test_extract_last_json_from_text_accepts_trailing_logs(self) -> None:
        payload = json_output.extract_last_json_from_text(
            '{"ok": true, "status": "loaded"}\nHTTP Request: POST http://127.0.0.1:11434/v1/embeddings "HTTP/1.1 200 OK"'
        )

        self.assertEqual(payload, {"ok": True, "status": "loaded"})

    def test_extract_json_from_streams_reads_json_from_stderr(self) -> None:
        payload = json_output.extract_json_from_streams(
            "[plugins] hook runner initialized with 1 registered hooks\n",
            'Processing request of type CallToolRequest\n{"ok": true, "status": "pass"}',
        )

        self.assertEqual(payload, {"ok": True, "status": "pass"})

    def test_extract_last_json_from_text_prefers_dict_over_later_list(self) -> None:
        payload = json_output.extract_last_json_from_text(
            '{"ok": true, "status": "pass"}\n["trailing", "telemetry"]'
        )

        self.assertEqual(payload, {"ok": True, "status": "pass"})

    def test_extract_last_json_from_text_prefers_later_dict_over_earlier_log_list(self) -> None:
        payload = json_output.extract_last_json_from_text(
            'Semantic vector search disabled until reindex: stored vector dimensions are mixed '
            '(configured_dim=1024, detected_dims=[1024, 4096]).\n'
            '{"ok": true, "status": "warn"}'
        )

        self.assertEqual(payload, {"ok": True, "status": "warn"})

    def test_extract_json_from_streams_prefers_dict_payload_over_stdout_list(self) -> None:
        payload = json_output.extract_json_from_streams(
            '["streaming", "events"]',
            '{"ok": true, "status": "pass"}',
        )

        self.assertEqual(payload, {"ok": True, "status": "pass"})

    def test_parse_json_process_output_reports_empty_both_streams(self) -> None:
        result = CompletedProcess(["openclaw"], 0, stdout="", stderr="")

        with self.assertRaisesRegex(RuntimeError, "empty stdout and stderr"):
            json_output.parse_json_process_output(result, context="openclaw memory-palace status")

    def test_parse_json_process_output_prefers_valid_payload_from_stderr(self) -> None:
        result = CompletedProcess(
            ["openclaw", "memory-palace", "status", "--json"],
            0,
            stdout="[plugins] hook runner initialized with 1 registered hooks\n",
            stderr='Processing request of type CallToolRequest\n{"ok": true, "status": "pass"}',
        )

        payload = json_output.parse_json_process_output(
            result,
            context="openclaw memory-palace status",
        )

        self.assertEqual(payload, {"ok": True, "status": "pass"})

    def test_extract_last_json_from_text_raises_when_no_json_exists(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            json_output.extract_last_json_from_text("plain log only")


if __name__ == "__main__":
    unittest.main()
