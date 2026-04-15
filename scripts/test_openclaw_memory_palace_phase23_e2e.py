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

import openclaw_memory_palace_phase23_e2e as phase23


class Phase23E2ETests(unittest.TestCase):
    def test_extract_text_fragments_dedupes_nested_assistant_output(self) -> None:
        payload = {
            "messages": [
                {"role": "assistant", "content": [{"type": "text", "text": "alpha"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "alpha"}]},
            ],
            "response": {"role": "assistant", "content": [{"type": "text", "text": "beta"}]},
        }

        fragments = phase23.extract_text_fragments(payload)

        self.assertCountEqual(fragments, ["alpha", "beta"])

    def test_build_temp_openclaw_config_enables_phase23_flags(self) -> None:
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
                    "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b\n"
                    "DATABASE_URL=sqlite+aiosqlite:///tmp/demo.db\n"
                    "OPENAI_MODEL=gpt-5.4\n"
                    "OPENAI_API_KEY=super-secret\n"
                ),
                encoding="utf-8",
            )

            payload = phase23.build_temp_openclaw_config(
                base_config,
                runtime_env,
                workspace_dir=workspace_dir,
                auto_capture=False,
                capture_assistant_derived=True,
                profile_memory=True,
                host_bridge=True,
            )

        plugins = payload["plugins"]["entries"]["memory-palace"]["config"]
        self.assertEqual(payload["agents"]["defaults"]["workspace"], str(workspace_dir))
        self.assertTrue(payload["agents"]["defaults"]["skipBootstrap"])
        self.assertFalse(payload["hooks"]["internal"]["enabled"])
        self.assertTrue(plugins["hostBridge"]["enabled"])
        self.assertTrue(plugins["capturePipeline"]["captureAssistantDerived"])
        self.assertFalse(plugins["autoCapture"]["enabled"])
        self.assertTrue(plugins["profileMemory"]["enabled"])
        self.assertEqual(plugins["stdio"]["env"]["EXISTING_FLAG"], "keep-me")
        self.assertEqual(
            plugins["stdio"]["env"]["OPENCLAW_MEMORY_PALACE_ENV_FILE"],
            str(runtime_env),
        )
        self.assertNotIn("OPENAI_API_KEY", plugins["stdio"]["env"])
        self.assertNotIn("OPENAI_MODEL", plugins["stdio"]["env"])
        self.assertNotIn("DATABASE_URL", plugins["stdio"]["env"])

    def test_wait_for_memory_search_fails_fast_when_gateway_log_reports_host_bridge_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "gateway.log"
            log_path.write_text(
                "memory-palace host bridge import failed: Skipped: write_guard blocked create_memory\n",
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(args=["openclaw"], returncode=0, stdout="{}", stderr="")

            with patch.object(phase23, "run", return_value=completed), patch.object(
                phase23,
                "parse_json_output",
                side_effect=[
                    {"ok": True},
                    {"results": []},
                ],
            ):
                with self.assertRaisesRegex(RuntimeError, "detected host bridge failure"):
                    phase23.wait_for_memory_search(
                        "phase23-marker",
                        env={},
                        timeout_seconds=5,
                        fail_fast_log_path=log_path,
                        fail_fast_marker="memory-palace host bridge import failed",
                    )

    def test_main_cleans_tmp_root_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            base_config = root / "base-openclaw.json"
            report_path = root / "report.json"
            run_root = root / "phase23-run"
            run_root.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )

            with patch.object(
                phase23,
                "DEFAULT_REPORT_PATH",
                report_path,
            ), patch.object(
                phase23,
                "resolve_current_openclaw_config",
                return_value=base_config,
            ), patch.object(
                phase23.tempfile,
                "mkdtemp",
                return_value=str(run_root),
            ), patch.object(
                phase23,
                "run_phase2_host_bridge",
                side_effect=RuntimeError("simulated phase2 failure"),
            ):
                exit_code = phase23.main()
                payload = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 1)
        self.assertFalse(run_root.exists())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["tmpRoot"], str(run_root))

    def test_extract_agent_text_prefers_result_payloads(self) -> None:
        payload = {
            "result": {
                "payloads": [
                    {"text": "phase23-marker"},
                ]
            }
        }

        self.assertEqual(phase23.extract_agent_text(payload), "phase23-marker")

    def test_is_transient_agent_failure_detects_retryable_provider_text(self) -> None:
        payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [
                    {"text": "API rate limit reached. Please try again later."},
                ],
            },
        }

        self.assertTrue(phase23.is_transient_agent_failure(payload))

    def test_is_transient_agent_failure_detects_unexpected_eof(self) -> None:
        payload = {
            "summary": "completed",
            "result": {
                "stopReason": "error",
                "payloads": [
                    {"text": "unexpected EOF"},
                ],
            },
        }

        self.assertTrue(phase23.is_transient_agent_failure(payload))

    def test_is_transient_agent_failure_ignores_normal_agent_payloads(self) -> None:
        payload = {
            "summary": "completed",
            "result": {
                "stopReason": "stop",
                "payloads": [
                    {"text": "Your default workflow is code and tests first."},
                ],
            },
        }

        self.assertFalse(phase23.is_transient_agent_failure(payload))

    def test_resolve_current_openclaw_config_prefers_openclaw_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                json.dumps({"models": {"providers": {"demo": {"type": "openai"}}}}),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"OPENCLAW_CONFIG_PATH": str(config_path)}, clear=False), patch.object(
                phase23,
                "run",
                side_effect=AssertionError("CLI config probe should not run"),
            ):
                resolved = phase23.resolve_current_openclaw_config()

        self.assertEqual(resolved, config_path.resolve())

    def test_resolve_current_openclaw_config_accepts_json_like_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                """{
  models: {
    providers: {
      demo: {type: "openai",},
    },
  },
  agents: {
    defaults: {
      model: {primary: "demo/gpt-5.4",},
    },
  },
}
""",
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"OPENCLAW_CONFIG_PATH": str(config_path)}, clear=False), patch.object(
                phase23,
                "run",
                side_effect=AssertionError("CLI config probe should not run"),
            ):
                resolved = phase23.resolve_current_openclaw_config()

        self.assertEqual(resolved, config_path.resolve())

    def test_stop_gateway_process_escalates_to_direct_kill_after_group_timeout(self) -> None:
        fake_process = unittest.mock.Mock()
        fake_process.pid = 321
        fake_process.poll.return_value = None
        fake_process.wait.side_effect = [subprocess.TimeoutExpired(["openclaw"], 15), None]

        with patch.object(phase23.smoke, "kill_process_group") as kill_group_mock:
            phase23.stop_gateway_process(fake_process)

        self.assertEqual(kill_group_mock.call_count, 2)
        fake_process.kill.assert_not_called()

    def test_select_search_result_prefers_committed_assistant_derived_record(self) -> None:
        results = [
            {"path": "memory-palace/core/profile/workflow.md"},
            {"path": "memory-palace/core/pending/assistant-derived/workflow.md"},
            {"path": "memory-palace/core/assistant-derived/workflow.md"},
        ]

        selected = phase23.select_search_result(
            results,
            required_path_fragment="/assistant-derived/",
            allow_pending=False,
        )

        self.assertEqual(
            selected,
            {"path": "memory-palace/core/assistant-derived/workflow.md"},
        )

    def test_select_host_bridge_record_prefers_candidate_containing_marker(self) -> None:
        marker = "phase23-marker-1234"
        results = [
            {"path": "memory-palace/core/agents/main/host-bridge/workflow/sha256-line2.md"},
            {"path": "memory-palace/core/agents/main/host-bridge/workflow/sha256-line1.md"},
        ]

        def fake_run(command: list[str], *, env: dict[str, str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
            _ = env, timeout
            path_value = command[-2]
            payload = {
                "text": (
                    f"memory text for {path_value}"
                    if path_value.endswith("sha256-line2.md")
                    else f"memory text for {path_value}\n{marker}"
                ),
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

        with patch.object(phase23, "run", side_effect=fake_run):
            path_value, stored_text = phase23.select_host_bridge_record(
                results,
                env={},
                marker=marker,
            )

        self.assertEqual(
            path_value,
            "memory-palace/core/agents/main/host-bridge/workflow/sha256-line1.md",
        )
        self.assertIn(marker, stored_text)

    def test_select_host_bridge_record_falls_back_to_first_host_bridge_candidate(self) -> None:
        results = [
            {"path": "memory-palace/core/agents/main/host-bridge/workflow/sha256-line2.md"},
            {"path": "memory-palace/core/agents/main/host-bridge/workflow/sha256-line1.md"},
        ]

        def fake_run(command: list[str], *, env: dict[str, str], timeout: int = 900) -> subprocess.CompletedProcess[str]:
            _ = env, timeout
            path_value = command[-2]
            payload = {
                "text": f"memory text for {path_value}\n- source_mode: host_workspace_import",
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

        with patch.object(phase23, "run", side_effect=fake_run):
            path_value, stored_text = phase23.select_host_bridge_record(
                results,
                env={},
                marker="phase23-marker-missing",
            )

        self.assertEqual(
            path_value,
            "memory-palace/core/agents/main/host-bridge/workflow/sha256-line2.md",
        )
        self.assertIn("source_mode: host_workspace_import", stored_text)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
