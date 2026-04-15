#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_memory_palace_phase45_e2e as phase45


class Phase45E2ETests(unittest.TestCase):
    def test_resolve_openclaw_bin_path_keeps_plain_command_names(self) -> None:
        self.assertEqual(phase45.resolve_openclaw_bin_path("openclaw"), "openclaw")

    def test_resolve_openclaw_bin_path_resolves_relative_maintainer_wrapper(self) -> None:
        expected = str((Path.cwd() / "scripts" / "dev" / "openclaw-local-wrapper").resolve())
        self.assertEqual(
            phase45.resolve_openclaw_bin_path("./scripts/dev/openclaw-local-wrapper"),
            expected,
        )

    def test_phase_recorder_tracks_pass_and_fail_events(self) -> None:
        recorder = phase45.PhaseRecorder(event_limit=4)

        with patch.object(phase45.time, "monotonic", side_effect=[10.0, 11.25, 20.0, 22.5]), patch.object(
            phase45,
            "utc_now_iso",
            side_effect=[
                "2026-03-25T01:00:00Z",
                "2026-03-25T01:00:01Z",
                "2026-03-25T01:00:02Z",
                "2026-03-25T01:00:03Z",
            ],
        ):
            started = recorder.start("prewarm.backends", profile="c")
            recorder.succeed("prewarm.backends", started, profile="c")
            started = recorder.start("recall", marker="phase45-demo")
            recorder.fail("recall", started, RuntimeError("timeout"), marker="phase45-demo")

        snapshot = recorder.snapshot()
        self.assertEqual(snapshot["failed_step"], "recall")
        self.assertEqual(snapshot["phase_timings"]["prewarm.backends"], 1.25)
        self.assertEqual(snapshot["phase_timings"]["recall"], 2.5)
        self.assertEqual(
            [event["status"] for event in snapshot["phase_events"]],
            ["start", "pass", "start", "fail"],
        )
        self.assertEqual(snapshot["phase_events"][-1]["error"], "timeout")

    def test_is_preexisting_phase45_fallback_detects_stale_timestamp(self) -> None:
        self.assertTrue(
            phase45.is_preexisting_phase45_fallback(
                {
                    "id": "last-fallback-path",
                    "details": {
                        "at": "2026-03-18T17:54:13.288Z",
                    },
                },
                "2026-03-19T01:00:00Z",
            )
        )
        self.assertFalse(
            phase45.is_preexisting_phase45_fallback(
                {
                    "id": "last-fallback-path",
                    "details": {
                        "at": "2026-03-19T01:00:01Z",
                    },
                },
                "2026-03-19T01:00:00Z",
            )
        )

    def test_ensure_phase45_diagnostics_ignores_stale_last_fallback_warning(self) -> None:
        payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "pass",
                    "message": "Last capture path: core://agents/main/captured/llm-extracted/workflow/current.",
                },
                {
                    "id": "last-fallback-path",
                    "status": "warn",
                    "details": {
                        "at": "2026-03-18T17:54:13.288Z",
                        "reason": "smart_extraction_write_result_not_ok",
                    },
                },
                {"id": "capture-layer-distribution", "status": "pass", "message": "llm_extracted=1"},
                {"id": "search-probe", "status": "pass"},
                {"id": "read-probe", "status": "pass"},
            ],
        }

        phase45.ensure_phase45_diagnostics(
            payload,
            payload,
            payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

    def test_ensure_phase45_diagnostics_allows_last_fallback_warning_after_successful_capture(self) -> None:
        payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "pass",
                    "message": "Last capture path: core://agents/main/captured/llm-extracted/workflow/current.",
                },
                {
                    "id": "last-fallback-path",
                    "status": "warn",
                    "details": {
                        "at": "2026-03-19T01:00:01Z",
                        "reason": "smart_extraction_write_result_not_ok",
                    },
                },
                {"id": "capture-layer-distribution", "status": "pass", "message": "llm_extracted=1"},
                {"id": "search-probe", "status": "pass"},
                {"id": "read-probe", "status": "pass"},
            ],
        }

        phase45.ensure_phase45_diagnostics(
            payload,
            payload,
            payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

    def test_ensure_phase45_diagnostics_allows_last_rule_capture_decision_warning(self) -> None:
        payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "pass",
                    "message": "Last capture path: core://agents/main/captured/llm-extracted/workflow/current.",
                },
                {"id": "last-fallback-path", "status": "pass"},
                {
                    "id": "last-rule-capture-decision",
                    "status": "warn",
                    "message": "No recent rule-capture decision is recorded yet.",
                },
                {"id": "capture-layer-distribution", "status": "pass", "message": "llm_extracted=1"},
                {"id": "search-probe", "status": "pass"},
                {"id": "read-probe", "status": "pass"},
            ],
        }

        phase45.ensure_phase45_diagnostics(
            payload,
            payload,
            payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

    def test_ensure_phase45_diagnostics_allows_last_capture_path_warning_when_read_probe_passed(self) -> None:
        verify_payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "warn",
                    "message": "No recent capture path is recorded yet.",
                },
                {
                    "id": "last-fallback-path",
                    "status": "warn",
                    "details": {
                        "at": "2026-03-19T01:00:01Z",
                        "reason": "smart_extraction_write_result_not_ok",
                    },
                },
            ],
        }
        doctor_payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {"id": "capture-layer-distribution", "status": "pass", "message": "llm_extracted=1"},
            ],
        }
        smoke_payload = {
            "checks": [
                {"id": "search-probe", "status": "pass"},
                {"id": "read-probe", "status": "pass"},
            ],
        }

        phase45.ensure_phase45_diagnostics(
            verify_payload,
            doctor_payload,
            smoke_payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

    def test_ensure_phase45_diagnostics_allows_search_probe_warning_for_intent_llm_fallback(self) -> None:
        verify_payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "pass",
                    "message": "Last capture path: core://agents/main/captured/llm-extracted/workflow/current.",
                },
                {"id": "last-fallback-path", "status": "pass"},
            ],
        }
        doctor_payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {"id": "capture-layer-distribution", "status": "pass", "message": "llm_extracted=1"},
                {
                    "id": "search-probe",
                    "status": "warn",
                    "details": {
                        "results": [{"path": "memory-palace/core/agents/main/profile/workflow.md"}],
                        "degrade_reasons": ["intent_llm_request_failed"],
                    },
                },
            ],
        }
        smoke_payload = {
            "checks": [
                {"id": "search-probe", "status": "pass"},
                {"id": "read-probe", "status": "pass"},
            ],
        }

        phase45.ensure_phase45_diagnostics(
            verify_payload,
            doctor_payload,
            smoke_payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

    def test_ensure_phase45_diagnostics_allows_smoke_search_probe_warning_for_intent_llm_fallback(self) -> None:
        verify_payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "pass",
                    "message": "Last capture path: core://agents/main/captured/llm-extracted/workflow/current.",
                },
                {"id": "last-fallback-path", "status": "pass"},
            ],
        }
        doctor_payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {"id": "capture-layer-distribution", "status": "pass", "message": "llm_extracted=1"},
            ],
        }
        smoke_payload = {
            "checks": [
                {
                    "id": "search-probe",
                    "status": "warn",
                    "details": {
                        "results": [{"path": "memory-palace/core/agents/main/profile/workflow.md"}],
                        "degrade_reasons": ["intent_llm_request_failed"],
                    },
                },
                {"id": "read-probe", "status": "pass"},
            ],
        }

        phase45.ensure_phase45_diagnostics(
            verify_payload,
            doctor_payload,
            smoke_payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

    def test_normalize_phase45_report_status_returns_pass_for_allowed_warnings(self) -> None:
        payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "pass",
                    "message": "Last capture path: core://agents/main/captured/llm-extracted/workflow/current.",
                },
                {
                    "id": "last-fallback-path",
                    "status": "warn",
                    "details": {
                        "at": "2026-03-19T01:00:01Z",
                        "reason": "smart_extraction_write_result_not_ok",
                    },
                },
                {"id": "host-bridge", "status": "warn"},
                {"id": "auto-capture", "status": "warn"},
                {"id": "last-rule-capture-decision", "status": "warn"},
                {"id": "sleep-consolidation", "status": "warn"},
            ],
        }

        status = phase45.normalize_phase45_report_status(
            "verify",
            payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

        self.assertEqual(status, "pass")

    def test_normalize_phase45_report_status_allows_search_probe_warning_for_intent_llm_fallback(self) -> None:
        payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {"id": "capture-layer-distribution", "status": "pass", "message": "llm_extracted=1"},
                {
                    "id": "search-probe",
                    "status": "warn",
                    "details": {
                        "results": [{"path": "memory-palace/core/agents/main/profile/workflow.md"}],
                        "degrade_reasons": ["intent_llm_request_failed"],
                    },
                },
            ],
        }

        status = phase45.normalize_phase45_report_status(
            "doctor",
            payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

        self.assertEqual(status, "pass")

    def test_normalize_phase45_report_status_allows_last_capture_path_warning_with_read_probe_pass(self) -> None:
        payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "warn",
                    "message": "No recent capture path is recorded yet.",
                },
                {
                    "id": "last-fallback-path",
                    "status": "warn",
                    "details": {
                        "at": "2026-03-19T01:00:01Z",
                        "reason": "smart_extraction_write_result_not_ok",
                    },
                },
                {"id": "read-probe", "status": "pass"},
            ],
        }

        status = phase45.normalize_phase45_report_status(
            "verify",
            payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

        self.assertEqual(status, "pass")

    def test_normalize_phase45_report_status_allows_profile_memory_state_warning(self) -> None:
        payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "pass",
                    "message": "Last capture path: core://agents/main/captured/llm-extracted/workflow/current.",
                },
                {"id": "last-fallback-path", "status": "pass"},
                {"id": "profile-memory-state", "status": "warn"},
            ],
        }

        status = phase45.normalize_phase45_report_status(
            "verify",
            payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

        self.assertEqual(status, "pass")

    def test_normalize_phase45_report_status_keeps_other_unexpected_warning(self) -> None:
        payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {
                    "id": "last-capture-path",
                    "status": "pass",
                    "message": "Last capture path: core://agents/main/captured/llm-extracted/workflow/current.",
                },
                {"id": "last-fallback-path", "status": "pass"},
                {"id": "transport-health", "status": "warn"},
            ],
        }

        status = phase45.normalize_phase45_report_status(
            "verify",
            payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at="2026-03-19T01:00:00Z",
        )

        self.assertEqual(status, "warn")

    def test_wait_for_llm_extracted_current_waits_for_complete_workflow_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cwd = Path(tmp_dir)
            marker = "phase45-demo"
            stale_text = (
                "# Memory Palace Durable Fact\n"
                "- source_mode: llm_extracted\n"
                "- capture_layer: smart_extraction\n"
                f"## Summary\nDefault workflow: start with code changes first for {marker}; run the tests immediately after.\n"
            )
            ready_text = stale_text + "Docs should come at the end.\n"
            run_side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
                MagicMock(returncode=0),
            ]
            parse_side_effect = [
                {"ok": True},
                {"text": stale_text},
                {"ok": True},
                {"text": ready_text},
            ]

            with patch.object(
                phase45.assistant_e2e,
                "run",
                side_effect=run_side_effect,
            ) as run_mock, patch.object(
                phase45.assistant_e2e,
                "parse_json_output",
                side_effect=parse_side_effect,
            ), patch.object(
                phase45.time,
                "sleep",
                return_value=None,
            ):
                _index_result, get_payload, target_uri = phase45.wait_for_llm_extracted_current(
                    "openclaw",
                    env={},
                    cwd=cwd,
                    ready_check=lambda text: phase45.phase45_llm_record_ready(text, marker=marker),
                )

        self.assertEqual(target_uri, "core://agents/main/captured/llm-extracted/workflow/current")
        self.assertIn("Docs should come at the end.", str(get_payload.get("text") or ""))
        self.assertEqual(run_mock.call_count, 4)

    def test_wait_for_llm_extracted_current_bounds_inner_timeouts_by_remaining_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cwd = Path(tmp_dir)
            marker = "phase45-deadline"
            ready_text = (
                "# Memory Palace Durable Fact\n"
                "- source_mode: llm_extracted\n"
                "- capture_layer: smart_extraction\n"
                f"## Summary\nDefault workflow: start with code changes first for {marker}; run the tests immediately after.\n"
                "Docs should come at the end.\n"
            )
            run_side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0),
            ]
            monotonic_values = iter([0.0, 2.0, 2.0])

            with patch.object(
                phase45.assistant_e2e,
                "run",
                side_effect=run_side_effect,
            ) as run_mock, patch.object(
                phase45.assistant_e2e,
                "parse_json_output",
                side_effect=[
                    {"ok": True},
                    {"text": ready_text},
                ],
            ), patch.object(
                phase45.time,
                "monotonic",
                side_effect=lambda: next(monotonic_values),
            ):
                phase45.wait_for_llm_extracted_current(
                    "openclaw",
                    env={},
                    cwd=cwd,
                    timeout_seconds=5.0,
                    ready_check=lambda text: phase45.phase45_llm_record_ready(text, marker=marker),
                )

        first_timeout = run_mock.call_args_list[0].kwargs["timeout"]
        first_command = run_mock.call_args_list[0].args[0]
        second_timeout = run_mock.call_args_list[1].kwargs["timeout"]
        self.assertEqual(first_timeout, 10)
        self.assertIn("3", first_command)
        self.assertEqual(second_timeout, 10)

    def test_wait_for_llm_extracted_current_checks_profile_fallback_when_current_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cwd = Path(tmp_dir)
            marker = "phase45-profile-fallback"
            fallback_text = (
                "# Memory Palace Profile Block\n"
                f"- marker: {marker}\n"
                "test then doc\n"
            )
            run_side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=1, stdout="", stderr="not found"),
            ]

            with patch.object(
                phase45.assistant_e2e,
                "run",
                side_effect=run_side_effect,
            ) as run_mock, patch.object(
                phase45.assistant_e2e,
                "parse_json_output",
                side_effect=[{"ok": True}],
            ), patch.object(
                phase45,
                "try_optional_phase45_get",
                return_value={"text": fallback_text},
            ) as fallback_mock, patch.object(
                phase45.time,
                "sleep",
                return_value=None,
            ):
                _index_result, get_payload, target_uri = phase45.wait_for_llm_extracted_current(
                    "openclaw",
                    env={},
                    cwd=cwd,
                    ready_check=lambda text: phase45.phase45_llm_record_ready(text, marker=marker),
                    fallback_targets=("memory-palace/core/agents/main/profile/workflow.md",),
                    fallback_ready_check=lambda text: phase45.phase45_profile_record_ready(
                        text,
                        marker=marker,
                    ),
                )

        self.assertEqual(
            target_uri,
            "memory-palace/core/agents/main/profile/workflow.md",
        )
        self.assertEqual(get_payload.get("text"), fallback_text)
        self.assertEqual(run_mock.call_count, 2)
        fallback_mock.assert_called_once()

    def test_wait_for_llm_extracted_current_returns_profile_fallback_before_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cwd = Path(tmp_dir)
            marker = "phase45-fallback"
            fallback_text = (
                "# Memory Palace Profile Block\n"
                f"Default workflow marker: {marker}\n"
                "Run the test first, then write the doc.\n"
            )

            run_side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=1, stderr="not found", stdout=""),
                MagicMock(returncode=0),
            ]

            with patch.object(
                phase45.assistant_e2e,
                "run",
                side_effect=run_side_effect,
            ) as run_mock, patch.object(
                phase45.assistant_e2e,
                "parse_json_output",
                side_effect=[
                    {"ok": True},
                    {"text": fallback_text},
                ],
            ), patch.object(
                phase45.time,
                "sleep",
                return_value=None,
            ):
                _index_result, get_payload, target_uri = phase45.wait_for_llm_extracted_current(
                    "openclaw",
                    env={},
                    cwd=cwd,
                    ready_check=lambda text: phase45.phase45_llm_record_ready(text, marker=marker),
                    fallback_targets=("memory-palace/core/agents/main/profile/workflow.md",),
                    fallback_ready_check=lambda text: phase45.phase45_profile_record_ready(
                        text,
                        marker=marker,
                    ),
                )

        self.assertEqual(
            target_uri,
            "memory-palace/core/agents/main/profile/workflow.md",
        )
        self.assertIn("write the doc", str(get_payload.get("text") or "").lower())
        self.assertEqual(run_mock.call_count, 3)

    def test_try_optional_phase45_get_returns_none_when_target_is_missing(self) -> None:
        with patch.object(
            phase45.assistant_e2e,
            "run",
            return_value=MagicMock(returncode=1),
        ), patch.object(
            phase45.assistant_e2e,
            "parse_json_output",
            side_effect=RuntimeError("URI 'core://agents/main/profile/workflow' not found."),
        ):
            payload = phase45.try_optional_phase45_get(
                "openclaw",
                "memory-palace/core/agents/main/profile/workflow.md",
                env={},
                cwd=Path.cwd(),
            )

        self.assertIsNone(payload)

    def test_try_optional_phase45_get_re_raises_non_not_found_errors(self) -> None:
        with patch.object(
            phase45.assistant_e2e,
            "run",
            return_value=MagicMock(returncode=1),
        ), patch.object(
            phase45.assistant_e2e,
            "parse_json_output",
            side_effect=RuntimeError("phase45 gateway timeout"),
        ):
            with self.assertRaisesRegex(RuntimeError, "gateway timeout"):
                phase45.try_optional_phase45_get(
                    "openclaw",
                    "memory-palace/core/agents/main/profile/workflow.md",
                    env={},
                    cwd=Path.cwd(),
                )

    def test_model_env_supports_phase45_accepts_openai_compatible_env(self) -> None:
        self.assertTrue(
            phase45.model_env_supports_phase45(
                {
                    "OPENAI_BASE_URL": "http://127.0.0.1:8317/v1",
                    "OPENAI_MODEL": "gpt-5.4",
                }
            )
        )
        self.assertFalse(phase45.model_env_supports_phase45({}))

    def test_model_env_supports_phase45_accepts_intent_llm_aliases(self) -> None:
        self.assertTrue(
            phase45.model_env_supports_phase45(
                {
                    "INTENT_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                    "INTENT_LLM_MODEL": "gpt-5.4",
                }
            )
        )

    def test_model_env_supports_phase45_accepts_responses_alias_base(self) -> None:
        self.assertTrue(
            phase45.model_env_supports_phase45(
                {
                    "LLM_RESPONSES_URL": "http://127.0.0.1:8318/v1/responses",
                    "INTENT_LLM_MODEL": "gpt-5.4",
                }
            )
        )

    def test_build_temp_openclaw_config_enables_phase45_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            runtime_python = tmp_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "gateway": {"auth": {"token": "demo-token"}},
                        "agents": {"defaults": {}, "list": []},
                        "models": {"providers": {"demo": {"type": "openai"}}},
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
                "\n".join(
                    [
                        "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=c",
                        "DATABASE_URL=sqlite+aiosqlite:///tmp/demo.db",
                        "OPENAI_BASE_URL=http://127.0.0.1:8317/v1",
                        "OPENAI_MODEL=gpt-5.4",
                        "OPENAI_API_KEY=super-secret",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                phase45.installer,
                "build_default_stdio_launch",
                return_value=("python.exe", ["mcp_wrapper.py"], str(tmp_root / "backend")),
            ):
                payload = phase45.build_temp_openclaw_config(
                    base_config,
                    runtime_env,
                    workspace_dir,
                    runtime_python,
                )

        plugins = payload["plugins"]["entries"]["memory-palace"]["config"]
        self.assertEqual(payload["agents"]["defaults"]["workspace"], str(workspace_dir))
        self.assertTrue(payload["agents"]["defaults"]["skipBootstrap"])
        self.assertTrue(payload["hooks"]["internal"]["enabled"])
        self.assertFalse(plugins["hostBridge"]["enabled"])
        self.assertFalse(plugins["capturePipeline"]["captureAssistantDerived"])
        self.assertTrue(plugins["smartExtraction"]["enabled"])
        self.assertTrue(plugins["reconcile"]["enabled"])
        self.assertIn("transport-diagnostics.json", plugins["observability"]["transportDiagnosticsPath"])
        self.assertEqual(plugins["stdio"]["env"]["EXISTING_FLAG"], "keep-me")
        self.assertEqual(
            plugins["stdio"]["env"]["OPENCLAW_MEMORY_PALACE_ENV_FILE"],
            str(runtime_env),
        )
        self.assertEqual(plugins["stdio"]["env"]["OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON"], str(runtime_python))
        self.assertEqual(plugins["stdio"]["command"], "python.exe")
        self.assertEqual(plugins["stdio"]["args"], ["mcp_wrapper.py"])
        self.assertEqual(plugins["stdio"]["cwd"], str(tmp_root / "backend"))
        self.assertNotIn("OPENAI_API_KEY", plugins["stdio"]["env"])
        self.assertNotIn("OPENAI_MODEL", plugins["stdio"]["env"])
        self.assertNotIn("DATABASE_URL", plugins["stdio"]["env"])

    def test_build_temp_openclaw_config_preserves_runtime_python_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "models": {"providers": {"demo": {"type": "openai"}}},
                        "plugins": {"entries": {"memory-palace": {"config": {"stdio": {"env": {}}}}}},
                    }
                ),
                encoding="utf-8",
            )
            runtime_env.write_text("OPENAI_MODEL=gpt-5.4-mini\n", encoding="utf-8")
            runtime_python = tmp_root / "runtime-python.exe"
            runtime_python.write_text("", encoding="utf-8")

            with patch.dict(
                phase45.os.environ,
                {
                    "OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON": str(runtime_python),
                    "OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT": "/tmp/runtime-root",
                },
                clear=False,
            ), patch.object(
                phase45.installer,
                "build_default_stdio_launch",
                return_value=("runtime-python", ["mcp_wrapper.py"], str(tmp_root / "backend")),
            ):
                payload = phase45.build_temp_openclaw_config(base_config, runtime_env, workspace_dir)

        env_block = payload["plugins"]["entries"]["memory-palace"]["config"]["stdio"]["env"]
        stdio = payload["plugins"]["entries"]["memory-palace"]["config"]["stdio"]
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON"], str(runtime_python))
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT"], str(tmp_root))
        self.assertEqual(stdio["command"], "runtime-python")
        self.assertEqual(stdio["args"], ["mcp_wrapper.py"])
        self.assertEqual(stdio["cwd"], str(tmp_root / "backend"))

    def test_build_temp_openclaw_config_scrubs_inherited_stdio_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "models": {"providers": {"demo": {"type": "openai"}}},
                        "plugins": {
                            "entries": {
                                "memory-palace": {
                                    "config": {
                                        "stdio": {
                                            "env": {
                                                "DATABASE_URL": "sqlite+aiosqlite:////Users/demo/.openclaw/memory-palace/data/memory-palace.db",
                                                "OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT": "/Users/demo/.openclaw/memory-palace",
                                                "OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH": "/Users/demo/.openclaw/memory-palace/observability/openclaw_transport_diagnostics.json",
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
            runtime_env.write_text("OPENAI_MODEL=gpt-5.4-mini\n", encoding="utf-8")

            payload = phase45.build_temp_openclaw_config(base_config, runtime_env, workspace_dir)

        env_block = payload["plugins"]["entries"]["memory-palace"]["config"]["stdio"]["env"]
        self.assertNotIn("DATABASE_URL", env_block)
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"], str(runtime_env))
        self.assertEqual(env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT"], str(tmp_root))
        self.assertEqual(
            env_block["OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH"],
            str(tmp_root / "transport-diagnostics.json"),
        )

    def test_build_temp_openclaw_config_drops_unrelated_host_plugin_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "models": {"providers": {"demo": {"type": "openai"}}},
                        "plugins": {
                            "entries": {
                                "memory-lancedb": {
                                    "enabled": True,
                                    "config": {"embedding": "broken-host-config"},
                                },
                                "memory-palace": {
                                    "config": {
                                        "stdio": {"env": {}},
                                    }
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime_env.write_text("OPENAI_MODEL=gpt-5.4-mini\n", encoding="utf-8")

            payload = phase45.build_temp_openclaw_config(
                base_config,
                runtime_env,
                workspace_dir,
            )

        entries = payload["plugins"]["entries"]
        self.assertEqual(set(entries.keys()), {"memory-palace"})

    def test_build_phase_env_rejects_shared_runtime_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_config = root / ".openclaw" / "openclaw.json"
            shared_runtime_root = root / ".openclaw" / "memory-palace"
            shared_state = shared_runtime_root / "state"

            with patch.object(phase45.smoke, "DEFAULT_SHARED_CONFIG_PATH", shared_config), patch.object(
                phase45.smoke, "DEFAULT_SHARED_MEMORY_PALACE_ROOT", shared_runtime_root
            ):
                with self.assertRaisesRegex(RuntimeError, "shared OpenClaw runtime paths"):
                    phase45.build_phase_env({}, shared_config, shared_state)

    def test_build_runtime_env_file_seeds_profile_c_memory_llm_from_compatible_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "profile-c.env"
            captured = {}

            def fake_build_profile_env(platform: str, profile: str, env_target: Path, model_env: dict[str, str]):
                captured["platform"] = platform
                captured["profile"] = profile
                captured["target"] = env_target
                captured["model_env"] = dict(model_env)
                return model_env

            with patch.object(phase45.smoke, "build_profile_env", side_effect=fake_build_profile_env):
                result = phase45.build_runtime_env_file(
                    target,
                    {
                        "OPENAI_BASE_URL": "http://127.0.0.1:8318/v1/chat/completions",
                        "OPENAI_API_KEY": "sk-12345678",
                        "OPENAI_MODEL": "gpt-5.4-mini",
                    },
                    profile="c",
                )

        self.assertEqual(result, target)
        self.assertEqual(captured["profile"], "c")
        self.assertEqual(captured["target"], target)
        self.assertEqual(
            captured["model_env"]["WRITE_GUARD_LLM_API_BASE"],
            "http://127.0.0.1:8318/v1",
        )
        self.assertEqual(captured["model_env"]["WRITE_GUARD_LLM_API_KEY"], "sk-12345678")
        self.assertEqual(captured["model_env"]["WRITE_GUARD_LLM_MODEL"], "gpt-5.4-mini")

    def test_build_temp_openclaw_config_overrides_agent_model_from_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "models": {
                            "providers": {
                                "localgpt54": {
                                    "baseUrl": "http://127.0.0.1:8317/v1",
                                    "api": "openai-completions",
                                    "models": [{"id": "gpt-5.4", "name": "gpt-5.4"}],
                                }
                            }
                        },
                        "agents": {
                            "defaults": {
                                "model": {"primary": "localgpt54/gpt-5.4"},
                                "models": {"localgpt54/gpt-5.4": {"alias": "gpt-5.4"}},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            runtime_env.write_text(
                "\n".join(
                    [
                        "OPENAI_BASE_URL=http://host.docker.internal:8317/v1",
                        "OPENAI_API_KEY=sk-test",
                        "OPENAI_MODEL=gpt-5.4-mini",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            payload = phase45.build_temp_openclaw_config(base_config, runtime_env, workspace_dir)

        providers = payload["models"]["providers"]
        self.assertIn("phase45-openai", providers)
        self.assertEqual(providers["phase45-openai"]["baseUrl"], "http://host.docker.internal:8317/v1")
        self.assertEqual(payload["agents"]["defaults"]["model"]["primary"], "phase45-openai/gpt-5.4-mini")
        self.assertEqual(
            payload["agents"]["defaults"]["models"]["phase45-openai/gpt-5.4-mini"]["alias"],
            "gpt-5.4-mini",
        )

    def test_main_preserves_tmp_root_after_failure_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            base_config = root / "base-openclaw.json"
            report_path = root / "report.json"
            run_root = root / "phase45-run"
            run_root.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            reranker_manager = MagicMock()
            reranker_manager.__enter__.return_value = {
                "managed": False,
                "started": False,
                "pid": None,
                "root": "",
                "base_url": "",
                "model": "",
            }
            reranker_manager.__exit__.return_value = False

            gateway_manager = MagicMock()
            gateway_manager.__enter__.side_effect = RuntimeError("simulated gateway failure")
            gateway_manager.__exit__.return_value = False

            with patch.object(sys, "argv", [
                "openclaw_memory_palace_phase45_e2e.py",
                "--report",
                str(report_path),
            ]), patch.object(
                phase45,
                "load_model_env",
                return_value={"OPENAI_API_KEY": "super-secret"},
            ), patch.object(
                phase45,
                "apply_local_model_env_overrides",
                side_effect=lambda payload: payload,
            ), patch.object(
                phase45,
                "resolve_base_config",
                return_value=base_config,
            ), patch.object(
                phase45,
                "collect_phase45_support_report",
                return_value={
                    "supported": True,
                    "reason": "ok",
                    "base_config_path": str(base_config),
                    "model_env_keys": ["OPENAI_API_KEY"],
                    "checks": [],
                },
            ), patch.object(
                phase45.tempfile,
                "mkdtemp",
                return_value=str(run_root),
            ), patch.object(
                phase45,
                "managed_reranker_runtime",
                return_value=reranker_manager,
            ), patch.object(
                phase45,
                "build_runtime_env_file",
                side_effect=lambda target, _model_env, _profile: target,
            ), patch.object(
                phase45.smoke,
                "load_env_file",
                return_value={"OPENAI_API_KEY": "super-secret"},
            ), patch.object(
                phase45.smoke,
                "prewarm_profile_model_backends",
                return_value=[
                    {"component": "embedding", "status": "pass"},
                    {"component": "reranker", "status": "pass"},
                ],
            ), patch.object(
                phase45,
                "build_temp_openclaw_config",
                return_value={"gateway": {"auth": {"token": "demo-token"}}},
            ), patch.object(
                phase45,
                "build_phase_env",
                return_value={},
            ), patch.object(
                phase45,
                "managed_phase45_gateway",
                return_value=gateway_manager,
            ):
                exit_code = phase45.main()
                payload = json.loads(report_path.read_text(encoding="utf-8"))

                self.assertEqual(exit_code, 1)
                self.assertTrue(run_root.exists())
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["tmp_root"], str(run_root))
                self.assertTrue(payload["artifacts_preserved"])
                self.assertEqual(payload["failed_step"], "gateway.capture_phase")
                self.assertIn("setup.runtime", payload["phase_timings"])
                self.assertIn("gateway.capture_phase", payload["phase_timings"])
                self.assertEqual(payload["phase_events"][-1]["status"], "fail")
                self.assertEqual(payload["phase_events"][-1]["step"], "gateway.capture_phase")

    def test_main_cleanup_on_failure_flag_removes_tmp_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            base_config = root / "base-openclaw.json"
            report_path = root / "report.json"
            run_root = root / "phase45-run"
            run_root.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            reranker_manager = MagicMock()
            reranker_manager.__enter__.return_value = {
                "managed": False,
                "started": False,
                "pid": None,
                "root": "",
                "base_url": "",
                "model": "",
            }
            reranker_manager.__exit__.return_value = False

            gateway_manager = MagicMock()
            gateway_manager.__enter__.side_effect = RuntimeError("simulated gateway failure")
            gateway_manager.__exit__.return_value = False

            with patch.object(sys, "argv", [
                "openclaw_memory_palace_phase45_e2e.py",
                "--report",
                str(report_path),
                "--cleanup-on-failure",
            ]), patch.object(
                phase45,
                "load_model_env",
                return_value={"OPENAI_API_KEY": "super-secret"},
            ), patch.object(
                phase45,
                "apply_local_model_env_overrides",
                side_effect=lambda payload: payload,
            ), patch.object(
                phase45,
                "resolve_base_config",
                return_value=base_config,
            ), patch.object(
                phase45,
                "collect_phase45_support_report",
                return_value={
                    "supported": True,
                    "reason": "ok",
                    "base_config_path": str(base_config),
                    "model_env_keys": ["OPENAI_API_KEY"],
                    "checks": [],
                },
            ), patch.object(
                phase45.tempfile,
                "mkdtemp",
                return_value=str(run_root),
            ), patch.object(
                phase45,
                "managed_reranker_runtime",
                return_value=reranker_manager,
            ), patch.object(
                phase45,
                "build_runtime_env_file",
                side_effect=lambda target, _model_env, _profile: target,
            ), patch.object(
                phase45.smoke,
                "load_env_file",
                return_value={"OPENAI_API_KEY": "super-secret"},
            ), patch.object(
                phase45.smoke,
                "prewarm_profile_model_backends",
                return_value=[
                    {"component": "embedding", "status": "pass"},
                    {"component": "reranker", "status": "pass"},
                ],
            ), patch.object(
                phase45,
                "build_temp_openclaw_config",
                return_value={"gateway": {"auth": {"token": "demo-token"}}},
            ), patch.object(
                phase45,
                "build_phase_env",
                return_value={},
            ), patch.object(
                phase45,
                "managed_phase45_gateway",
                return_value=gateway_manager,
            ):
                exit_code = phase45.main()
                payload = json.loads(report_path.read_text(encoding="utf-8"))

                self.assertEqual(exit_code, 1)
                self.assertFalse(run_root.exists())
                self.assertFalse(payload["ok"])
                self.assertFalse(payload["artifacts_preserved"])

    def test_main_success_payload_includes_phase_timings_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            base_config = root / "base-openclaw.json"
            report_path = root / "report.json"
            run_root = root / "phase45-run"
            run_root.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            reranker_manager = MagicMock()
            reranker_manager.__enter__.return_value = {
                "managed": False,
                "started": False,
                "pid": None,
                "root": "",
                "base_url": "http://10.0.0.1:8080/v1",
                "model": "Qwen3-Reranker-8B",
                "validated": True,
            }
            reranker_manager.__exit__.return_value = False

            verify_payload = {
                "status": "pass",
                "checks": [
                    {"id": "smart-extraction", "status": "pass"},
                    {"id": "reconcile-mode", "status": "pass"},
                    {
                        "id": "last-capture-path",
                        "status": "pass",
                        "message": "Last capture path: core://agents/main/captured/llm-extracted/workflow/current.",
                    },
                    {"id": "last-fallback-path", "status": "pass"},
                ],
            }
            doctor_payload = {
                "status": "pass",
                "checks": [
                    {"id": "capture-layer-distribution", "status": "pass", "message": "llm_extracted=1"},
                    {"id": "smart-extraction", "status": "pass"},
                    {"id": "reconcile-mode", "status": "pass"},
                ],
            }
            smoke_payload = {
                "status": "pass",
                "checks": [
                    {"id": "search-probe", "status": "pass"},
                    {"id": "read-probe", "status": "pass"},
                ],
            }
            parse_outputs = iter([verify_payload, doctor_payload, smoke_payload, {"ok": True}])

            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        sys,
                        "argv",
                        [
                            "openclaw_memory_palace_phase45_e2e.py",
                            "--report",
                            str(report_path),
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "load_model_env",
                        return_value={
                            "OPENAI_BASE_URL": "http://127.0.0.1:8317/v1",
                            "OPENAI_MODEL": "gpt-5.4-mini",
                            "RETRIEVAL_EMBEDDING_MODEL": "qwen3-embedding:8b-q8_0-ctx8192",
                            "RETRIEVAL_EMBEDDING_DIM": "1024",
                        },
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "apply_local_model_env_overrides",
                        side_effect=lambda payload: payload,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "resolve_base_config",
                        return_value=base_config,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "collect_phase45_support_report",
                        return_value={
                            "supported": True,
                            "reason": "ok",
                            "base_config_path": str(base_config),
                            "model_env_keys": [
                                "OPENAI_BASE_URL",
                                "OPENAI_MODEL",
                                "RETRIEVAL_EMBEDDING_MODEL",
                                "RETRIEVAL_EMBEDDING_DIM",
                            ],
                            "checks": [],
                        },
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.tempfile,
                        "mkdtemp",
                        return_value=str(run_root),
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "managed_reranker_runtime",
                        return_value=reranker_manager,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "build_runtime_env_file",
                        side_effect=lambda target, _model_env, _profile: target,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.smoke,
                        "load_env_file",
                        return_value={"OPENAI_BASE_URL": "http://127.0.0.1:8317/v1"},
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.smoke,
                        "prewarm_profile_model_backends",
                        return_value=[
                            {"component": "embedding", "status": "pass"},
                            {"component": "reranker", "status": "pass"},
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "build_temp_openclaw_config",
                        return_value={"gateway": {"auth": {"token": "demo-token"}}},
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "build_phase_env",
                        return_value={},
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "managed_phase45_gateway",
                        side_effect=[
                            contextlib.nullcontext("ws://127.0.0.1:7777"),
                            contextlib.nullcontext("ws://127.0.0.1:7778"),
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.assistant_e2e,
                        "run_agent_message",
                        side_effect=[
                            {"ok": True},
                            {"ok": True},
                            {"ok": True},
                            {"ok": True},
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "wait_for_llm_extracted_current",
                        return_value=(
                            {"ok": True},
                            {
                                "text": "\n".join(
                                    [
                                        "# Memory Palace Durable Fact",
                                        "- source_mode: llm_extracted",
                                        "- capture_layer: smart_extraction",
                                        "",
                                        "test then doc",
                                    ]
                                )
                            },
                            "core://agents/main/captured/llm-extracted/workflow/current",
                        ),
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "try_optional_phase45_get",
                        return_value={"text": "phase45-abcd workflow test doc"},
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.assistant_e2e,
                        "run",
                        return_value=MagicMock(),
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.assistant_e2e,
                        "parse_json_output",
                        side_effect=lambda *_args, **_kwargs: next(parse_outputs),
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.assistant_e2e,
                        "extract_text_fragments",
                        return_value=[
                            "code changes first, tests immediately after, docs last",
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.smoke,
                        "extract_index_command_ok",
                        return_value=True,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.secrets,
                        "token_hex",
                        return_value="abcd",
                    )
                )
                exit_code = phase45.main()
                payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["failed_step"])
        self.assertIn("prewarm.backends", payload["phase_timings"])
        self.assertIn("verify", payload["phase_timings"])
        self.assertIn("doctor", payload["phase_timings"])
        self.assertIn("smoke", payload["phase_timings"])
        self.assertIn("recall", payload["phase_timings"])
        statuses = {(event["step"], event["status"]) for event in payload["phase_events"]}
        self.assertIn(("agent_message.initial_preference", "pass"), statuses)
        self.assertIn(("agent_message.reinforce_tests", "pass"), statuses)
        self.assertIn(("agent_message.reinforce_docs", "pass"), statuses)
        self.assertIn(("wait_for_llm_extracted_current", "pass"), statuses)
        self.assertIn(("agent_command.new", "pass"), statuses)
        self.assertIn(("recall", "pass"), statuses)

    def test_main_records_warn_event_when_profile_capture_fallback_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            base_config = root / "base-openclaw.json"
            report_path = root / "report.json"
            run_root = root / "phase45-run"
            run_root.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            reranker_manager = MagicMock()
            reranker_manager.__enter__.return_value = {
                "managed": False,
                "started": False,
                "pid": None,
                "root": "",
                "base_url": "http://10.0.0.1:8080/v1",
                "model": "Qwen3-Reranker-8B",
                "validated": True,
            }
            reranker_manager.__exit__.return_value = False

            verify_payload = {
                "status": "pass",
                "checks": [
                    {"id": "smart-extraction", "status": "pass"},
                    {"id": "reconcile-mode", "status": "pass"},
                    {
                        "id": "last-capture-path",
                        "status": "warn",
                        "message": "Last capture path: memory-palace/core/agents/main/profile/workflow.md.",
                    },
                    {
                        "id": "last-fallback-path",
                        "status": "warn",
                        "message": "Last fallback path: memory-palace/core/agents/main/profile/workflow.md.",
                        "details": {
                            "reason": "smart_extraction_candidates_empty",
                        },
                    },
                ],
            }
            doctor_payload = {
                "status": "pass",
                "checks": [
                    {"id": "capture-layer-distribution", "status": "pass", "message": "manual_learn=1"},
                    {"id": "smart-extraction", "status": "pass"},
                    {"id": "reconcile-mode", "status": "pass"},
                ],
            }
            smoke_payload = {
                "status": "pass",
                "checks": [
                    {"id": "search-probe", "status": "pass"},
                    {"id": "read-probe", "status": "pass"},
                ],
            }
            parse_outputs = iter([verify_payload, doctor_payload, smoke_payload, {"ok": True}])

            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    patch.object(
                        sys,
                        "argv",
                        [
                            "openclaw_memory_palace_phase45_e2e.py",
                            "--report",
                            str(report_path),
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "load_model_env",
                        return_value={
                            "OPENAI_BASE_URL": "http://127.0.0.1:8317/v1",
                            "OPENAI_MODEL": "gpt-5.4-mini",
                            "RETRIEVAL_EMBEDDING_MODEL": "qwen3-embedding:8b-q8_0-ctx8192",
                            "RETRIEVAL_EMBEDDING_DIM": "1024",
                        },
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "apply_local_model_env_overrides",
                        side_effect=lambda payload: payload,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "resolve_base_config",
                        return_value=base_config,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "collect_phase45_support_report",
                        return_value={
                            "supported": True,
                            "reason": "ok",
                            "base_config_path": str(base_config),
                            "model_env_keys": [
                                "OPENAI_BASE_URL",
                                "OPENAI_MODEL",
                                "RETRIEVAL_EMBEDDING_MODEL",
                                "RETRIEVAL_EMBEDDING_DIM",
                            ],
                            "checks": [],
                        },
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.tempfile,
                        "mkdtemp",
                        return_value=str(run_root),
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "managed_reranker_runtime",
                        return_value=reranker_manager,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "build_runtime_env_file",
                        side_effect=lambda target, _model_env, _profile: target,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.smoke,
                        "load_env_file",
                        return_value={"OPENAI_BASE_URL": "http://127.0.0.1:8317/v1"},
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.smoke,
                        "prewarm_profile_model_backends",
                        return_value=[
                            {"component": "embedding", "status": "pass"},
                            {"component": "reranker", "status": "pass"},
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "build_temp_openclaw_config",
                        return_value={"gateway": {"auth": {"token": "demo-token"}}},
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "build_phase_env",
                        return_value={},
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "managed_phase45_gateway",
                        side_effect=[
                            contextlib.nullcontext("ws://127.0.0.1:7777"),
                            contextlib.nullcontext("ws://127.0.0.1:7778"),
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.assistant_e2e,
                        "run_agent_message",
                        side_effect=[
                            {"ok": True},
                            {"ok": True},
                            {"ok": True},
                            {"ok": True},
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "wait_for_llm_extracted_current",
                        return_value=(
                            {"ok": True},
                            {"text": "phase45-abcd workflow test doc"},
                            "memory-palace/core/agents/main/profile/workflow.md",
                        ),
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45,
                        "try_optional_phase45_get",
                        return_value={"text": "phase45-abcd workflow test doc"},
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.assistant_e2e,
                        "run",
                        return_value=MagicMock(),
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.assistant_e2e,
                        "parse_json_output",
                        side_effect=lambda *_args, **_kwargs: next(parse_outputs),
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.assistant_e2e,
                        "extract_text_fragments",
                        return_value=[
                            "code changes first, tests immediately after, docs last",
                        ],
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.smoke,
                        "extract_index_command_ok",
                        return_value=True,
                    )
                )
                stack.enter_context(
                    patch.object(
                        phase45.secrets,
                        "token_hex",
                        return_value="abcd",
                    )
                )
                exit_code = phase45.main()
                payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        fallback_warn = next(
            (
                event
                for event in payload["phase_events"]
                if event["step"] == "wait_for_llm_extracted_current"
                and event["status"] == "warn"
            ),
            None,
        )
        self.assertIsNotNone(fallback_warn)
        self.assertEqual(
            fallback_warn["details"]["reason"],
            "smart_extraction_current_missing_but_profile_record_present",
        )

    def test_phase45_e2e_supported_requires_base_models_and_model_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            ok, reason = phase45.phase45_e2e_supported(base_config, {})
            self.assertFalse(ok)
            self.assertIn("model env", reason)

            with patch.object(
                phase45,
                "probe_llm_service",
                return_value={"choices": [{"message": {"content": "{\"ok\":true}"}}]},
            ):
                ok, reason = phase45.phase45_e2e_supported(
                    base_config,
                    {
                        "WRITE_GUARD_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                        "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                    },
                )
            self.assertTrue(ok)
            self.assertIn(str(base_config), reason)

    def test_phase45_e2e_supported_accepts_trailing_comma_base_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            base_config.write_text(
                """{
  "models": {
    "providers": {
      "demo": {"type": "openai",},
    },
  },
  "agents": {
    "defaults": {
      "model": {"primary": "demo/gpt-5.4",},
    },
  },
}
""",
                encoding="utf-8",
            )

            with patch.object(
                phase45,
                "probe_llm_service",
                return_value={"choices": [{"message": {"content": "{\"ok\":true}"}}]},
            ):
                ok, reason = phase45.phase45_e2e_supported(
                    base_config,
                    {
                        "OPENAI_BASE_URL": "http://127.0.0.1:8317/v1",
                        "OPENAI_MODEL": "gpt-5.4",
                    },
                )

        self.assertTrue(ok)
        self.assertIn(str(base_config), reason)

    def test_phase45_e2e_supported_accepts_openclaw_test_llm_and_provider_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )
            model_env_path = tmp_root / "models.env"
            model_env_path.write_text(
                "\n".join(
                    [
                        "OPENCLAW_TEST_EMBEDDING_API_BASE=http://embed.local/v1/embeddings",
                        "OPENCLAW_TEST_EMBEDDING_API_KEY=embed-key",
                        "OPENCLAW_TEST_EMBEDDING_MODEL=embed-model",
                        "OPENCLAW_TEST_EMBEDDING_DIM=1024",
                        "OPENCLAW_TEST_RERANKER_API_BASE=http://rerank.local/v1/rerank",
                        "OPENCLAW_TEST_RERANKER_API_KEY=rerank-key",
                        "OPENCLAW_TEST_RERANKER_MODEL=rerank-model",
                        "OPENCLAW_TEST_LLM_API_BASE=http://llm.local/v1",
                        "OPENCLAW_TEST_LLM_API_KEY=llm-key",
                        "OPENCLAW_TEST_LLM_MODEL=gpt-5.4",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(
                phase45,
                "probe_llm_service",
                return_value={"choices": [{"message": {"content": "{\"ok\":true}"}}]},
            ), patch.object(
                phase45,
                "probe_embedding_dimension",
                return_value=1024,
            ), patch.object(
                phase45,
                "probe_reranker_service",
                return_value={"results": [{"index": 0, "score": 0.9}]},
            ):
                ok, reason = phase45.phase45_e2e_supported(
                    base_config,
                    phase45.load_model_env(str(model_env_path)),
                )

        self.assertTrue(ok)
        self.assertIn(str(base_config), reason)

    def test_collect_phase45_support_report_surfaces_provider_preflight_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            with patch.object(
                phase45,
                "probe_llm_service",
                side_effect=RuntimeError("HTTP Error 401: Unauthorized"),
            ), patch.object(
                phase45,
                "model_env_has_usable_reranker",
                return_value=False,
            ):
                payload = phase45.collect_phase45_support_report(
                    base_config,
                    {
                        "WRITE_GUARD_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                        "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                    },
                )

        self.assertFalse(payload["supported"])
        checks_by_id = {item["id"]: item for item in payload["checks"]}
        self.assertEqual(checks_by_id["llm-provider"]["status"], "fail")
        self.assertIn("401", checks_by_id["llm-provider"]["message"])

    def test_collect_phase45_support_report_keeps_optional_provider_checks_as_warn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            with patch.object(
                phase45,
                "probe_llm_service",
                return_value={"choices": [{"message": {"content": "{\"ok\":true}"}}]},
            ), patch.object(
                phase45,
                "model_env_has_usable_reranker",
                return_value=False,
            ):
                payload = phase45.collect_phase45_support_report(
                    base_config,
                    {
                        "WRITE_GUARD_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                        "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                    },
                )

        self.assertTrue(payload["supported"])
        checks_by_id = {item["id"]: item for item in payload["checks"]}
        self.assertEqual(checks_by_id["reranker-provider"]["status"], "warn")

    def test_managed_phase45_gateway_starts_gateway_in_own_process_group(self) -> None:
        gateway_process = MagicMock()
        gateway_process.pid = 4321
        gateway_process.poll.return_value = None

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            phase45.subprocess,
            "Popen",
            return_value=gateway_process,
        ) as popen_mock, patch.object(
            phase45.assistant_e2e,
            "wait_for_gateway",
        ) as wait_for_gateway, patch.object(
            phase45.smoke,
            "kill_process_group",
        ) as kill_process_group:
            workspace_dir = Path(tmp_dir)
            gateway_log_path = workspace_dir / "gateway.log"
            gateway_log_path.write_text("", encoding="utf-8")
            env = {"OPENCLAW_GATEWAY_TOKEN": "token"}

            with phase45.managed_phase45_gateway(
                "openclaw",
                env=env,
                workspace_dir=workspace_dir,
                gateway_log_path=gateway_log_path,
            ):
                self.assertIn("OPENCLAW_GATEWAY_URL", env)
            self.assertNotIn("OPENCLAW_GATEWAY_URL", env)

        self.assertTrue(popen_mock.call_args.kwargs["start_new_session"])
        wait_for_gateway.assert_called_once()
        kill_process_group.assert_called_once_with(4321, phase45.signal.SIGTERM)
        gateway_process.terminate.assert_called_once()
        gateway_process.wait.assert_called_once_with(
            timeout=phase45.GATEWAY_TERMINATE_WAIT_SECONDS
        )
        argv = popen_mock.call_args.args[0]
        self.assertIn("--force", argv)
        self.assertNotIn("OPENCLAW_GATEWAY_URL", popen_mock.call_args.kwargs["env"])
        self.assertIn("OPENCLAW_GATEWAY_URL", wait_for_gateway.call_args.kwargs["env"])
        self.assertEqual(
            wait_for_gateway.call_args.kwargs["timeout_seconds"],
            phase45.PHASE45_GATEWAY_HEALTH_TIMEOUT_SECONDS,
        )

    def test_managed_phase45_gateway_omits_force_without_port_tools(self) -> None:
        gateway_process = MagicMock()
        gateway_process.pid = 4321
        gateway_process.poll.return_value = None

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            phase45.subprocess,
            "Popen",
            return_value=gateway_process,
        ) as popen_mock, patch.object(
            phase45.assistant_e2e,
            "wait_for_gateway",
        ), patch.object(
            phase45.smoke,
            "kill_process_group",
        ), patch.object(
            phase45,
            "os",
        ) as os_mock, patch.object(
            phase45.shutil,
            "which",
            return_value=None,
        ):
            os_mock.name = "posix"
            workspace_dir = Path(tmp_dir)
            gateway_log_path = workspace_dir / "gateway.log"
            gateway_log_path.write_text("", encoding="utf-8")
            env = {"OPENCLAW_GATEWAY_TOKEN": "token"}

            with phase45.managed_phase45_gateway(
                "openclaw",
                env=env,
                workspace_dir=workspace_dir,
                gateway_log_path=gateway_log_path,
            ):
                self.assertIn("OPENCLAW_GATEWAY_URL", env)
            self.assertNotIn("OPENCLAW_GATEWAY_URL", env)

        argv = popen_mock.call_args.args[0]
        self.assertNotIn("--force", argv)
        self.assertNotIn("OPENCLAW_GATEWAY_URL", popen_mock.call_args.kwargs["env"])

    def test_apply_local_model_env_overrides_prefers_remote_embedding_when_available(self) -> None:
        with patch.object(
            phase45,
            "ensure_local_ollama_embedding_alias",
            return_value="qwen3-embedding:8b-q8_0-ctx8192",
        ), patch.object(
            phase45,
            "probe_embedding_dimension",
            side_effect=[1024, 1024],
        ) as probe_embedding_dimension:
            payload = phase45.apply_local_model_env_overrides(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://ai.gitee.com/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "remote-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "Qwen3-Embedding-8B",
                    "RETRIEVAL_RERANKER_API_BASE": "https://rerank.example/v1",
                    "RETRIEVAL_RERANKER_API_KEY": "remote-rerank-key",
                    "RETRIEVAL_RERANKER_MODEL": "Qwen3-Reranker-8B",
                }
            )

        self.assertEqual(probe_embedding_dimension.call_count, 2)
        self.assertEqual(
            probe_embedding_dimension.call_args_list[0].kwargs,
            {
                "api_key": "remote-key",
                "dimensions": 1024,
                "timeout_seconds": 30.0,
            },
        )
        self.assertEqual(
            probe_embedding_dimension.call_args_list[1].kwargs,
            {
                "api_key": "ollama",
                "dimensions": 1024,
                "timeout_seconds": 30.0,
            },
        )
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "https://ai.gitee.com/v1")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "Qwen3-Embedding-8B")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_KEY"], "remote-key")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "1024")
        self.assertEqual(payload["EMBEDDING_PROVIDER_CHAIN_ENABLED"], "true")
        self.assertEqual(payload["EMBEDDING_PROVIDER_FALLBACK"], "router")
        self.assertEqual(payload["ROUTER_API_BASE"], "http://127.0.0.1:11434/v1")
        self.assertEqual(payload["ROUTER_EMBEDDING_MODEL"], "qwen3-embedding:8b-q8_0-ctx8192")
        self.assertEqual(payload["RETRIEVAL_RERANKER_API_BASE"], "https://rerank.example/v1")
        self.assertEqual(payload["RETRIEVAL_RERANKER_API_KEY"], "remote-rerank-key")
        self.assertEqual(payload["RETRIEVAL_RERANKER_MODEL"], "Qwen3-Reranker-8B")

    def test_apply_local_model_env_overrides_prefers_embeddings_alias_over_hash_seed(self) -> None:
        with patch.object(
            phase45,
            "ensure_local_ollama_embedding_alias",
            return_value="qwen3-embedding:8b-q8_0-ctx8192",
        ), patch.object(
            phase45,
            "probe_embedding_dimension",
            side_effect=[1024, 1024],
        ) as probe_embedding_dimension:
            payload = phase45.apply_local_model_env_overrides(
                {
                    "RETRIEVAL_EMBEDDING_BACKEND": "hash",
                    "RETRIEVAL_EMBEDDING_MODEL": "hash-v1",
                    "EMBEDDINGS_BASE_URL": "https://ai.gitee.com/v1/embeddings",
                    "EMBEDDINGS_API_KEY": "remote-key",
                    "EMBEDDINGS_MODEL": "Qwen3-Embedding-8B",
                    "RETRIEVAL_RERANKER_API_BASE": "https://rerank.example/v1",
                    "RETRIEVAL_RERANKER_API_KEY": "remote-rerank-key",
                    "RETRIEVAL_RERANKER_MODEL": "Qwen3-Reranker-8B",
                }
            )

        self.assertEqual(probe_embedding_dimension.call_count, 2)
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "https://ai.gitee.com/v1")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "Qwen3-Embedding-8B")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_KEY"], "remote-key")

    def test_apply_local_model_env_overrides_keeps_remote_embedding_when_local_fallback_unavailable(self) -> None:
        with patch.object(
            phase45,
            "ensure_local_ollama_embedding_alias",
            side_effect=RuntimeError("ollama missing"),
        ), patch.object(
            phase45,
            "probe_embedding_dimension",
            return_value=1024,
        ) as probe_embedding_dimension:
            payload = phase45.apply_local_model_env_overrides(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://ai.gitee.com/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "remote-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "Qwen3-Embedding-8B",
                    "RETRIEVAL_RERANKER_API_BASE": "https://rerank.example/v1",
                    "RETRIEVAL_RERANKER_MODEL": "Qwen3-Reranker-8B",
                }
            )

        probe_embedding_dimension.assert_called_once_with(
            "https://ai.gitee.com/v1",
            "Qwen3-Embedding-8B",
            api_key="remote-key",
            dimensions=1024,
            timeout_seconds=30.0,
        )
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "https://ai.gitee.com/v1")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "Qwen3-Embedding-8B")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "1024")
        self.assertNotIn("EMBEDDING_PROVIDER_CHAIN_ENABLED", payload)
        self.assertNotIn("ROUTER_API_BASE", payload)

    def test_apply_local_model_env_overrides_keeps_remote_embedding_when_remote_probe_fails(self) -> None:
        with patch.object(
            phase45,
            "probe_embedding_dimension",
            side_effect=RuntimeError("remote embedding timeout"),
        ), patch.object(
            phase45,
            "ensure_local_ollama_embedding_alias",
            side_effect=AssertionError("local fallback should not run"),
        ):
            payload = phase45.apply_local_model_env_overrides(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://ai.gitee.com/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "remote-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "Qwen3-Embedding-8B",
                    "RETRIEVAL_EMBEDDING_DIM": "1024",
                    "RETRIEVAL_RERANKER_API_BASE": "https://rerank.example/v1",
                    "RETRIEVAL_RERANKER_MODEL": "Qwen3-Reranker-8B",
                }
            )

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "https://ai.gitee.com/v1")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_KEY"], "remote-key")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "Qwen3-Embedding-8B")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "1024")
        self.assertNotIn("EMBEDDING_PROVIDER_CHAIN_ENABLED", payload)
        self.assertNotIn("ROUTER_API_BASE", payload)

    def test_model_env_has_explicit_remote_embedding_detects_remote_only(self) -> None:
        self.assertTrue(
            phase45.model_env_has_explicit_remote_embedding(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://ai.gitee.com/v1",
                    "RETRIEVAL_EMBEDDING_MODEL": "Qwen3-Embedding-8B",
                }
            )
        )
        self.assertFalse(
            phase45.model_env_has_explicit_remote_embedding(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "http://127.0.0.1:11434/v1",
                    "RETRIEVAL_EMBEDDING_MODEL": "qwen3-embedding:8b-q8_0-ctx8192",
                }
            )
        )
        self.assertFalse(phase45.model_env_has_explicit_remote_embedding({}))

    def test_apply_local_model_env_overrides_disables_chain_when_local_dim_mismatches(self) -> None:
        with patch.object(
            phase45,
            "ensure_local_ollama_embedding_alias",
            return_value="qwen3-embedding:8b-q8_0-ctx8192",
        ), patch.object(
            phase45,
            "probe_embedding_dimension",
            side_effect=[1024, 768],
        ):
            payload = phase45.apply_local_model_env_overrides(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://ai.gitee.com/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "remote-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "Qwen3-Embedding-8B",
                    "RETRIEVAL_RERANKER_API_BASE": "https://rerank.example/v1",
                    "RETRIEVAL_RERANKER_MODEL": "Qwen3-Reranker-8B",
                }
            )

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "1024")
        self.assertNotIn("EMBEDDING_PROVIDER_CHAIN_ENABLED", payload)
        self.assertNotIn("ROUTER_API_BASE", payload)

    def test_apply_local_model_env_overrides_defaults_local_embedding_dim_to_1024(self) -> None:
        with patch.object(
            phase45,
            "ensure_local_ollama_embedding_alias",
            side_effect=AssertionError("local explicit embedding should not require alias"),
        ), patch.object(
            phase45,
            "probe_embedding_dimension",
            return_value=1024,
        ) as probe_embedding_dimension:
            payload = phase45.apply_local_model_env_overrides(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "http://127.0.0.1:11434/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "ollama",
                    "RETRIEVAL_EMBEDDING_MODEL": "qwen3-embedding:8b-q8_0",
                }
            )

        self.assertEqual(
            probe_embedding_dimension.call_args.kwargs,
            {
                "api_key": "ollama",
                "dimensions": 1024,
                "timeout_seconds": 30.0,
            },
        )
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "1024")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "qwen3-embedding:8b-q8_0")
        self.assertNotIn("EMBEDDING_PROVIDER_CHAIN_ENABLED", payload)

    def test_apply_local_model_env_overrides_falls_back_to_local_embedding_with_detected_dim(self) -> None:
        with patch.object(
            phase45,
            "ensure_local_ollama_embedding_alias",
            return_value="qwen3-embedding:8b-q8_0-ctx8192",
        ), patch.object(
            phase45,
            "probe_embedding_dimension",
            return_value=1024,
        ) as probe_embedding_dimension:
            payload = phase45.apply_local_model_env_overrides(
                {
                    "RETRIEVAL_RERANKER_API_KEY": "no-key",
                }
            )

        probe_embedding_dimension.assert_called_once_with(
            "http://127.0.0.1:11434/v1",
            "qwen3-embedding:8b-q8_0-ctx8192",
            api_key="ollama",
            dimensions=1024,
            timeout_seconds=30.0,
        )
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "http://127.0.0.1:11434/v1")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "qwen3-embedding:8b-q8_0-ctx8192")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_KEY"], "ollama")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "1024")

    def test_apply_local_model_env_overrides_falls_back_to_local_reranker_when_remote_missing(self) -> None:
        with patch.object(
            phase45,
            "ensure_local_ollama_embedding_alias",
            return_value="qwen3-embedding:8b-q8_0-ctx8192",
        ), patch.object(
            phase45,
            "probe_embedding_dimension",
            return_value=1024,
        ):
            payload = phase45.apply_local_model_env_overrides(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://ai.gitee.com/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "remote-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "Qwen3-Embedding-8B",
                }
            )

        self.assertEqual(payload["RETRIEVAL_RERANKER_API_BASE"], "http://127.0.0.1:8080")
        self.assertEqual(payload["RETRIEVAL_RERANKER_MODEL"], "Qwen3-Reranker-8B")

    def test_resolve_base_config_prefers_openclaw_env_before_cli_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "openclaw.json"
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"OPENCLAW_CONFIG_PATH": str(base_config)}, clear=False), patch.object(
                phase45.assistant_e2e,
                "resolve_current_openclaw_config",
                side_effect=AssertionError("CLI config probe should not run"),
            ):
                resolved = phase45.resolve_base_config("openclaw", "")

        self.assertEqual(resolved, base_config.resolve())

    def test_stop_gateway_process_escalates_to_direct_kill_after_group_timeout(self) -> None:
        fake_process = MagicMock()
        fake_process.pid = 654
        fake_process.poll.return_value = None
        fake_process.wait.side_effect = [
            subprocess.TimeoutExpired(
                ["openclaw"], phase45.GATEWAY_TERMINATE_WAIT_SECONDS
            ),
            None,
        ]

        with patch.object(phase45.smoke, "kill_process_group") as kill_group_mock:
            phase45.stop_gateway_process(fake_process)

        self.assertEqual(kill_group_mock.call_count, 2)
        fake_process.terminate.assert_called_once()
        fake_process.kill.assert_called_once()
        self.assertEqual(
            fake_process.wait.call_args_list,
            [
                call(timeout=phase45.GATEWAY_TERMINATE_WAIT_SECONDS),
                call(timeout=phase45.GATEWAY_FORCE_KILL_WAIT_SECONDS),
            ],
        )

    def test_ensure_required_check_ids_extracts_expected_checks(self) -> None:
        payload = {
            "checks": [
                {"id": "smart-extraction", "status": "pass"},
                {"id": "reconcile-mode", "status": "pass"},
                {"id": "last-capture-path", "status": "pass"},
                {"id": "last-fallback-path", "status": "pass"},
            ]
        }

        check_map = phase45.ensure_required_check_ids(
            payload,
            phase45.REQUIRED_PHASE45_VERIFY_IDS,
            context="verify",
        )

        self.assertEqual(set(check_map), phase45.REQUIRED_PHASE45_VERIFY_IDS)

    def test_managed_reranker_runtime_skips_local_boot_for_remote_service(self) -> None:
        with patch.object(phase45, "managed_local_reranker", side_effect=AssertionError("should not start local reranker")), patch.object(
            phase45,
            "probe_reranker_service",
            return_value={"results": [{"index": 0, "relevance_score": 0.9}]},
        ):
            manager = phase45.managed_reranker_runtime(
                {
                    "RETRIEVAL_RERANKER_API_BASE": "https://rerank.example/v1",
                    "RETRIEVAL_RERANKER_MODEL": "Qwen3-Reranker-8B",
                }
            )
            with manager as runtime:
                self.assertFalse(runtime["managed"])
                self.assertEqual(runtime["base_url"], "https://rerank.example/v1")
                self.assertTrue(runtime["validated"])

    def test_managed_local_reranker_rejects_unknown_existing_listener(self) -> None:
        with patch.object(phase45, "_port_is_open", return_value=True), patch.object(
            phase45,
            "probe_reranker_service",
            side_effect=RuntimeError("unexpected service"),
        ):
            with self.assertRaisesRegex(RuntimeError, "unexpected service"):
                with phase45.managed_local_reranker(model_name="Qwen3-Reranker-8B"):
                    self.fail("context should not yield when validation fails")

    def test_ensure_successful_prewarm_requires_success(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "reranker prewarm failed"):
            phase45.ensure_successful_prewarm(
                [{"component": "reranker", "status": "fail", "detail": "timeout"}],
                "reranker",
            )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
