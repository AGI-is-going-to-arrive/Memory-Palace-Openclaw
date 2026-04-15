#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from subprocess import CompletedProcess

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_memory_palace_windows_native_validation as validation


class WindowsNativeValidationTests(unittest.TestCase):
    def test_parse_args_supports_requested_flags(self) -> None:
        args = validation.parse_args(
            [
                "--profiles",
                "a,c",
                "--mode",
                "full",
                "--transport",
                "sse",
                "--model-env",
                "C:/models/runtime.env",
                "--artifacts-dir",
                "C:/artifacts",
                "--skip-package-install",
                "--skip-advanced",
                "--skip-full-stack",
                "--keep-artifacts",
                "--openclaw-bin",
                "C:/tools/openclaw.cmd",
            ]
        )

        self.assertEqual(args.profiles, "a,c")
        self.assertEqual(args.mode, "full")
        self.assertEqual(args.transport, "sse")
        self.assertEqual(args.model_env, "C:/models/runtime.env")
        self.assertEqual(args.artifacts_dir, "C:/artifacts")
        self.assertTrue(args.skip_package_install)
        self.assertTrue(args.skip_advanced)
        self.assertTrue(args.skip_full_stack)
        self.assertTrue(args.keep_artifacts)
        self.assertEqual(args.openclaw_bin, "C:/tools/openclaw.cmd")

    def test_parse_profiles_normalizes_and_dedupes(self) -> None:
        profiles = validation.parse_profiles(" b, c ,b,d ")

        self.assertEqual(profiles, ["b", "c", "d"])

    def test_validate_requested_profiles_rejects_cd_without_model_env(self) -> None:
        with self.assertRaisesRegex(ValueError, "Profiles C require --model-env"):
            validation.validate_requested_profiles(["c"], {})

    def test_phase23_e2e_supported_accepts_defaults_model_primary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "agents": {
                            "defaults": {
                                "model": {
                                    "primary": "gpt-5.4",
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            supported, reason = validation.phase23_e2e_supported(
                {"OPENCLAW_CONFIG_PATH": str(config_path)}
            )

        self.assertTrue(supported)
        self.assertIn(str(config_path.resolve()), reason)

    def test_phase23_e2e_supported_accepts_trailing_comma_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                """{
  "agents": {
    "defaults": {
      "model": {
        "primary": "gpt-5.4",
      },
    },
  },
}
""",
                encoding="utf-8",
            )

            supported, reason = validation.phase23_e2e_supported(
                {"OPENCLAW_CONFIG_PATH": str(config_path)}
            )

        self.assertTrue(supported)
        self.assertIn(str(config_path.resolve()), reason)

    def test_phase23_e2e_supported_uses_detected_host_config_when_home_path_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "AppData" / "Roaming" / "OpenClaw" / "openclaw.json"
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(
                json.dumps(
                    {
                        "models": {
                            "providers": {
                                "local": {
                                    "baseUrl": "http://127.0.0.1:8317/v1",
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                validation.installer,
                "detect_setup_config_path_with_source",
                return_value=(config_path.resolve(), "openclaw config file"),
            ):
                supported, reason = validation.phase23_e2e_supported({})

        self.assertTrue(supported)
        self.assertIn(str(config_path.resolve()), reason)

    def test_load_env_file_strips_wrapping_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "models.env"
            env_path.write_text(
                'OPENAI_MODEL="gpt-5.4"\nOPENAI_BASE_URL=\'https://example.com/v1\'\n',
                encoding="utf-8",
            )

            payload = validation.load_env_file(env_path)

        self.assertEqual(payload["OPENAI_MODEL"], "gpt-5.4")
        self.assertEqual(payload["OPENAI_BASE_URL"], "https://example.com/v1")

    def test_load_env_file_accepts_export_prefix_and_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "models.env"
            env_path.write_text(
                "\ufeffexport RETRIEVAL_EMBEDDING_MODEL=qwen3-embedding:8b-q8_0\n"
                "export WRITE_GUARD_LLM_MODEL='gpt-5.4'\n",
                encoding="utf-8",
            )

            payload = validation.load_env_file(env_path)

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "qwen3-embedding:8b-q8_0")
        self.assertEqual(payload["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")

    def test_build_setup_command_contains_expected_windows_flags(self) -> None:
        command = validation.build_setup_command(
            profile="b",
            mode="basic",
            transport="stdio",
            config_path=Path("C:/tmp/openclaw.json"),
            setup_root=Path("C:/tmp/runtime"),
        )

        self.assertEqual(command[0], validation.sys.executable)
        self.assertEqual(command[1], str(validation.WRAPPER_SCRIPT))
        self.assertIn("--config", command)
        self.assertIn("--setup-root", command)
        self.assertIn("--mode", command)
        self.assertIn("--profile", command)
        self.assertIn("--transport", command)
        self.assertEqual(command[-1], "--json")

    def test_build_setup_command_enables_strict_profile_for_cd(self) -> None:
        command = validation.build_setup_command(
            profile="c",
            mode="basic",
            transport="stdio",
            config_path=Path("C:/tmp/openclaw.json"),
            setup_root=Path("C:/tmp/runtime"),
        )

        self.assertIn("--strict-profile", command)

    def test_build_setup_command_includes_sse_url_and_ports_for_sse_transport(self) -> None:
        command = validation.build_setup_command(
            profile="b",
            mode="full",
            transport="sse",
            config_path=Path("C:/tmp/openclaw.json"),
            setup_root=Path("C:/tmp/runtime"),
            sse_url="http://127.0.0.1:55173/sse",
            backend_api_port=58000,
            dashboard_port=55173,
        )

        self.assertIn("--sse-url", command)
        self.assertIn("http://127.0.0.1:55173/sse", command)
        self.assertIn("--backend-api-port", command)
        self.assertIn("58000", command)
        self.assertIn("--dashboard-port", command)
        self.assertIn("55173", command)

    def test_build_full_stack_commands_include_dashboard_lifecycle(self) -> None:
        commands = validation.build_full_stack_commands(
            profile="d",
            transport="stdio",
            config_path=Path("C:/tmp/full-openclaw.json"),
            setup_root=Path("C:/tmp/full-runtime"),
        )
        names = [name for name, _command, _timeout in commands]

        self.assertEqual(
            names,
            [
                "full_setup",
                "dashboard_status_before",
                "dashboard_start",
                "dashboard_status_after_start",
                "dashboard_stop",
                "dashboard_status_after_stop",
            ],
        )

    def test_build_full_stack_commands_include_sse_url_when_requested(self) -> None:
        commands = validation.build_full_stack_commands(
            profile="b",
            transport="sse",
            config_path=Path("C:/tmp/full-openclaw.json"),
            setup_root=Path("C:/tmp/full-runtime"),
            backend_api_port=58000,
            dashboard_port=55173,
        )
        full_setup_command = commands[0][1]

        self.assertIn("--sse-url", full_setup_command)
        self.assertIn("http://127.0.0.1:55173/sse", full_setup_command)
        self.assertIn("--backend-api-port", full_setup_command)
        self.assertIn("58000", full_setup_command)
        self.assertIn("--dashboard-port", full_setup_command)
        self.assertIn("55173", full_setup_command)

    def test_build_phase23_e2e_commands_include_both_real_openclaw_scripts(self) -> None:
        commands = validation.build_phase23_e2e_commands(
            openclaw_bin="openclaw",
            profile_dir=Path("C:/tmp/profile-b"),
        )

        self.assertEqual([name for name, _command, _timeout in commands], ["host_bridge_e2e", "assistant_derived_e2e"])
        self.assertTrue(any(str(validation.HOST_BRIDGE_E2E_SCRIPT) in command for _name, command, _timeout in commands))
        self.assertTrue(any(str(validation.ASSISTANT_DERIVED_E2E_SCRIPT) in command for _name, command, _timeout in commands))

    def test_phase45_helpers_require_model_env_and_build_command(self) -> None:
        supported, reason = validation.phase45_e2e_supported({}, "b", {})
        self.assertFalse(supported)
        self.assertIn("only runs for profiles", reason)

        supported, reason = validation.phase45_e2e_supported({}, "c", {})
        self.assertFalse(supported)
        self.assertIn("--model-env", reason)

        commands = validation.build_phase45_e2e_commands(
            openclaw_bin="openclaw",
            profile_dir=Path("C:/tmp/profile-c"),
            profile="c",
            model_env_path="C:/tmp/models.env",
        )

        self.assertEqual([name for name, _command, _timeout in commands], ["phase45_e2e"])
        self.assertTrue(any(str(validation.PHASE45_E2E_SCRIPT) in command for _name, command, _timeout in commands))
        self.assertTrue(any("--profile" in command and "c" in command for _name, command, _timeout in commands))

    def test_phase45_model_env_supported_accepts_intent_llm_aliases(self) -> None:
        self.assertTrue(
            validation.phase45_model_env_supported(
                {
                    "INTENT_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                    "INTENT_LLM_MODEL": "gpt-5.4",
                }
            )
        )

    def test_phase45_model_env_supported_accepts_responses_alias_base(self) -> None:
        self.assertTrue(
            validation.phase45_model_env_supported(
                {
                    "LLM_RESPONSES_URL": "http://127.0.0.1:8318/v1/responses",
                    "INTENT_LLM_MODEL": "gpt-5.4",
                }
            )
        )

    def test_write_report_files_serializes_sanitized_reports(self) -> None:
        report = validation.build_report(
            profiles=["b", "c"],
            mode="basic",
            transport="stdio",
            artifacts_dir=Path("/tmp/windows-artifacts"),
            model_env_provided=True,
            openclaw_bin="C:/private/bin/openclaw.cmd",
            profile_results=[
                {
                    "profile": "b",
                    "ok": True,
                    "phases": [
                        {"name": "setup", "status": "passed", "summary": "setup passed"},
                    ],
                }
            ],
            package_result={"status": "passed", "summary": "package ok"},
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path, markdown_path = validation.write_report_files(report, Path(tmp_dir))
            json_text = json_path.read_text(encoding="utf-8")
            markdown_text = markdown_path.read_text(encoding="utf-8")

        self.assertIn('"modelEnvProvided": true', json_text)
        self.assertNotIn("C:/private/bin/openclaw.cmd", json_text)
        self.assertIn("openclaw.cmd", json_text)
        self.assertIn('"artifactsDir": "windows-artifacts"', json_text)
        self.assertNotIn("/tmp/windows-artifacts", json_text)
        self.assertIn("Model env provided: yes", markdown_text)
        self.assertNotIn("C:/private/bin/openclaw.cmd", markdown_text)

    def test_build_openclaw_env_strips_inherited_model_runtime_values(self) -> None:
        env = validation.build_openclaw_env(
            base_env={
                "PATH": "/usr/bin",
                "KEEP_ME": "1",
                "OPENAI_MODEL": "shell-model",
                "OPENAI_BASE_URL": "http://shell.example/v1",
                "RETRIEVAL_EMBEDDING_API_BASE": "http://shell-embed/v1",
                "WRITE_GUARD_LLM_MODEL": "shell-guard",
            },
            model_env={
                "OPENAI_MODEL": "runtime-model",
                "OPENAI_BASE_URL": "http://runtime.example/v1",
            },
            config_path=Path("C:/tmp/openclaw.json"),
            state_dir=Path("C:/tmp/state"),
        )

        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["KEEP_ME"], "1")
        self.assertEqual(env["OPENAI_MODEL"], "runtime-model")
        self.assertEqual(env["OPENAI_BASE_URL"], "http://runtime.example/v1")
        self.assertNotIn("RETRIEVAL_EMBEDDING_API_BASE", env)
        self.assertNotIn("WRITE_GUARD_LLM_MODEL", env)
        self.assertEqual(env["OPENCLAW_CONFIG_PATH"], "C:/tmp/openclaw.json")
        self.assertEqual(env["OPENCLAW_STATE_DIR"], "C:/tmp/state")

    def test_prepare_artifacts_dir_removes_stale_reports_and_target_profiles_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            artifacts_dir = Path(tmp_dir)
            (artifacts_dir / validation.REPORT_JSON_NAME).write_text("{}", encoding="utf-8")
            (artifacts_dir / validation.REPORT_MARKDOWN_NAME).write_text("# old\n", encoding="utf-8")
            stale_profile = artifacts_dir / "profile-b"
            stale_profile.mkdir()
            (stale_profile / "stale.json").write_text("{}", encoding="utf-8")
            untouched_profile = artifacts_dir / "profile-a"
            untouched_profile.mkdir()
            (untouched_profile / "keep.json").write_text("{}", encoding="utf-8")

            validation.prepare_artifacts_dir(artifacts_dir, ["b", "c"])

            self.assertFalse((artifacts_dir / validation.REPORT_JSON_NAME).exists())
            self.assertFalse((artifacts_dir / validation.REPORT_MARKDOWN_NAME).exists())
            self.assertFalse(stale_profile.exists())
            self.assertTrue(untouched_profile.exists())

    def test_execute_validation_requires_windows_host(self) -> None:
        args = argparse.Namespace(
            profiles="b",
            mode="basic",
            transport="stdio",
            model_env=None,
            artifacts_dir="/tmp/artifacts",
            skip_package_install=True,
            skip_advanced=True,
            skip_full_stack=True,
            keep_artifacts=False,
            openclaw_bin="openclaw",
        )

        with self.assertRaisesRegex(RuntimeError, "only execute on Windows hosts"):
            validation.execute_validation(args, platform_name="linux")

    def test_execute_validation_uses_runner_for_profile_and_package_steps(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(command, *, cwd=None, env=None, text=True, capture_output=True, timeout=0, check=False):
            _ = cwd
            _ = env
            _ = text
            _ = capture_output
            _ = timeout
            _ = check
            rendered = list(command)
            calls.append(rendered)
            stdout = "{}"
            if rendered[1:] and rendered[1] == str(validation.WRAPPER_SCRIPT) and "dashboard" in rendered:
                stdout = json.dumps({"ok": True, "status": "running"})
            elif rendered[0] == validation.sys.executable and rendered[1] == str(validation.PACKAGE_INSTALL_SCRIPT):
                stdout = ""
            return CompletedProcess(rendered, 0, stdout=stdout, stderr="")

        args = argparse.Namespace(
            profiles="b",
            mode="basic",
            transport="stdio",
            model_env=None,
            artifacts_dir="/tmp/artifacts",
            skip_package_install=False,
            skip_advanced=True,
            skip_full_stack=True,
            keep_artifacts=False,
            openclaw_bin="openclaw",
        )

        report = validation.execute_validation(args, platform_name="win32", runner=fake_runner)

        self.assertTrue(report["ok"])
        self.assertEqual(report["packageInstall"]["status"], "passed")
        self.assertTrue(any(call[:3] == [validation.sys.executable, str(validation.WRAPPER_SCRIPT), "setup"] for call in calls))
        self.assertTrue(any(call[:2] == [validation.sys.executable, str(validation.PACKAGE_INSTALL_SCRIPT)] for call in calls))

    def test_execute_validation_emits_running_progress_reports(self) -> None:
        progress_reports: list[dict[str, object]] = []

        def fake_runner(command, *, cwd=None, env=None, text=True, capture_output=True, timeout=0, check=False):
            _ = cwd
            _ = env
            _ = text
            _ = capture_output
            _ = timeout
            _ = check
            rendered = list(command)
            stdout = "{}"
            if rendered[1:] and rendered[1] == str(validation.WRAPPER_SCRIPT) and "dashboard" in rendered:
                stdout = json.dumps({"ok": True, "status": "running"})
            elif rendered[0] == validation.sys.executable and rendered[1] == str(validation.PACKAGE_INSTALL_SCRIPT):
                stdout = ""
            return CompletedProcess(rendered, 0, stdout=stdout, stderr="")

        args = argparse.Namespace(
            profiles="a,b",
            mode="basic",
            transport="stdio",
            model_env=None,
            artifacts_dir="/tmp/artifacts",
            skip_package_install=True,
            skip_advanced=True,
            skip_full_stack=True,
            keep_artifacts=False,
            openclaw_bin="openclaw",
        )

        report = validation.execute_validation(
            args,
            platform_name="win32",
            runner=fake_runner,
            progress_callback=progress_reports.append,
        )

        self.assertTrue(report["ok"])
        self.assertEqual(len(progress_reports), 2)
        self.assertTrue(all(item.get("running") is True for item in progress_reports))
        self.assertEqual(progress_reports[0]["profiles"], ["a", "b"])
        self.assertEqual(len(progress_reports[0]["profileResults"]), 1)
        self.assertEqual(len(progress_reports[1]["profileResults"]), 2)

    def test_execute_profile_fails_advanced_phase_when_search_has_no_target(self) -> None:
        def fake_runner(command, *, cwd=None, env=None, text=True, capture_output=True, timeout=0, check=False):
            _ = cwd
            _ = env
            _ = text
            _ = capture_output
            _ = timeout
            _ = check
            rendered = list(command)
            if rendered[:3] == [validation.sys.executable, str(validation.WRAPPER_SCRIPT), "setup"]:
                return CompletedProcess(rendered, 0, stdout='{"ok": true}', stderr="")
            if rendered[:3] == ["openclaw", "memory-palace", "search"]:
                return CompletedProcess(rendered, 0, stdout='{"results": []}', stderr="")
            return CompletedProcess(rendered, 0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = validation.execute_profile(
                profile="b",
                mode="basic",
                transport="stdio",
                openclaw_bin="openclaw",
                artifacts_dir=Path(tmp_dir),
                base_env={},
                model_env={},
                model_env_path=None,
                skip_advanced=False,
                skip_full_stack=True,
                keep_artifacts=True,
                runner=fake_runner,
            )

        phases = {phase["name"]: phase for phase in result["phases"]}
        self.assertEqual(phases["setup"]["status"], "passed")
        self.assertEqual(phases["verify_chain"]["status"], "passed")
        self.assertEqual(phases["advanced"]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
