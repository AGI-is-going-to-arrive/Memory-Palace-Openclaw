#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_memory_palace as wrapper
import openclaw_memory_palace_release_gate as gate


def _make_args(**overrides: object) -> argparse.Namespace:
    payload: dict[str, object] = {
        "report": "",
        "checkpoint_dir": "",
        "resume": False,
        "enable_live_benchmark": False,
        "enable_windows_native_validation": False,
        "skip_backend_tests": False,
        "skip_plugin_tests": False,
        "skip_python_matrix": False,
        "skip_frontend": False,
        "skip_frontend_e2e": False,
        "skip_profile_smoke": False,
        "skip_phase45": False,
        "skip_review_smoke": False,
        "profile_modes": "local",
        "phase45_profiles": "c,d",
        "review_smoke_modes": "local",
        "profile_smoke_model_env": "",
    }
    payload.update(overrides)
    return argparse.Namespace(**payload)


class ReleaseGateRunnerTests(unittest.TestCase):
    def test_render_report_marks_incomplete_runs_as_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_path = Path(tmp_dir) / "report.md"
            checkpoint_path = Path(tmp_dir) / "checkpoint.json"
            step = gate.ReleaseStep(
                step_id="3.45",
                title="Package Dry Run Audit",
                commands=[gate.StepCommand(["python", "dry-run.py"], Path(tmp_dir))],
                timeout_seconds=30,
                log_path=Path(tmp_dir) / "logs" / "dry-run.log",
            )
            payload = {
                "updated_at": "2026-04-03T00:00:00Z",
                "profile_smoke_modes": "local",
                "review_smoke_modes": "local",
                "steps": {
                    "3.45": {
                        "title": "Package Dry Run Audit",
                        "status": "RUNNING",
                        "commands": ["python dry-run.py"],
                    }
                },
            }

            gate.render_report(
                report_path=report_path,
                checkpoint_path=checkpoint_path,
                checkpoint_payload=payload,
                steps=[step],
            )

            rendered = report_path.read_text(encoding="utf-8")
        self.assertIn("- Result: `PENDING`", rendered)

    def test_build_release_steps_expands_profile_and_review_modes(self) -> None:
        args = argparse.Namespace(
            report="",
            checkpoint_dir="",
            resume=False,
            skip_backend_tests=False,
            skip_plugin_tests=False,
            skip_python_matrix=False,
            skip_frontend=False,
            skip_frontend_e2e=False,
            skip_profile_smoke=False,
            skip_phase45=False,
            skip_review_smoke=False,
            profile_modes="local,docker",
            phase45_profiles="c,d",
            review_smoke_modes="local,docker",
            profile_smoke_model_env="",
        )

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            gate.os.environ,
            {
                "PROFILE_SMOKE_PROFILES": "a,b",
                "VISUAL_BENCHMARK_PROFILES": "a",
            },
            clear=False,
        ), mock.patch.object(gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]), mock.patch.object(
            gate, "resolve_bun_command", return_value=["bun"]
        ), mock.patch.object(gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"), mock.patch.object(
            gate.shutil, "which", return_value="/tmp/bin"
        ):
            steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        step_ids = [step.step_id for step in steps]
        self.assertIn("4.local.a", step_ids)
        self.assertIn("4.local.b", step_ids)
        self.assertIn("4.docker.a", step_ids)
        self.assertIn("4.docker.b", step_ids)
        self.assertIn("1.5", step_ids)
        self.assertIn("6.local", step_ids)
        self.assertIn("6.docker", step_ids)
        self.assertIn("5", step_ids)
        self.assertIn("3.6", step_ids)
        self.assertIn("3.45", step_ids)
        self.assertIn("4.phase45.c", step_ids)
        self.assertIn("4.phase45.d", step_ids)
        self.assertIn("4.compact_reflection.c", step_ids)
        self.assertEqual(metadata["profile_smoke_profiles"], "a,b")
        self.assertEqual(metadata["phase45_profiles"], "c,d")
        self.assertFalse(metadata["phase45_enabled"])
        self.assertFalse(metadata["compact_context_reflection_enabled"])
        self.assertEqual(metadata["compact_context_reflection_profile"], "c")
        self.assertEqual(metadata["python_matrix_versions"], "3.10,3.11,3.12,3.13,3.14")

    def test_build_release_steps_includes_onboarding_tools_in_plugin_bun_tests(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
            steps, _metadata = gate.build_release_steps(args, Path(tmp_dir))

        plugin_step = next(step for step in steps if step.step_id == "3")
        self.assertIn("src/onboarding-tools.test.ts", plugin_step.commands[0].argv)

    def test_build_release_steps_adds_package_dry_run_audit(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
            steps, _metadata = gate.build_release_steps(args, Path(tmp_dir))

        dry_run_step = next(step for step in steps if step.step_id == "3.45")
        self.assertEqual(dry_run_step.commands[0].argv[-2:], ["scripts/openclaw_memory_palace.py", "stage-package"])
        self.assertEqual(dry_run_step.commands[1].argv, ["npm", "pack", "--dry-run"])
        self.assertEqual(dry_run_step.commands[1].env_overrides["COREPACK_ENABLE_DOWNLOAD_PROMPT"], "0")
        self.assertEqual(dry_run_step.commands[1].env_overrides["NPM_CONFIG_YES"], "true")

    def test_build_release_steps_adds_script_level_pytest_suite(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
            steps, _metadata = gate.build_release_steps(args, Path(tmp_dir))

        script_pytest_step = next(step for step in steps if step.step_id == "1.5")
        self.assertEqual(script_pytest_step.commands[0].argv[:3], ["/tmp/repo-python", "-m", "pytest"])
        self.assertIn("scripts/test_openclaw_memory_palace_installer.py", script_pytest_step.commands[0].argv)
        self.assertIn("scripts/test_openclaw_command_new_e2e.py", script_pytest_step.commands[0].argv)
        self.assertIn("scripts/test_openclaw_memory_palace_windows_native_validation.py", script_pytest_step.commands[0].argv)

    def test_build_release_steps_excludes_live_backend_benchmark_tests_from_backend_pytest(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
            steps, _metadata = gate.build_release_steps(args, Path(tmp_dir))

        backend_pytest_step = next(step for step in steps if step.step_id == "1")
        self.assertEqual(
            backend_pytest_step.commands[0].argv[:4],
            ["/tmp/backend-python", "-m", "pytest", "tests"],
        )
        marker_positions = [
            index for index, token in enumerate(backend_pytest_step.commands[0].argv) if token == "-m"
        ]
        self.assertGreaterEqual(len(marker_positions), 2)
        marker_expr = backend_pytest_step.commands[0].argv[marker_positions[-1] + 1]
        self.assertEqual(marker_expr, "not slow")

    def test_build_release_steps_skips_backend_benchmark_rerun_by_default_without_failure(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
            steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        benchmark_step = next(step for step in steps if step.step_id == "1.6")
        self.assertEqual(
            benchmark_step.skip_reason,
            "Skipped by default; pass --enable-live-benchmark to run the maintainer-only backend benchmark rerun gate.",
        )
        self.assertFalse(benchmark_step.skip_causes_failure)
        self.assertFalse(metadata["live_benchmark_enabled"])

    def test_build_release_steps_enables_backend_benchmark_rerun_when_requested(self) -> None:
        args = _make_args(enable_live_benchmark=True)

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
            steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        benchmark_step = next(step for step in steps if step.step_id == "1.6")
        self.assertIsNone(benchmark_step.skip_reason)
        self.assertFalse(benchmark_step.skip_causes_failure)
        self.assertEqual(
            benchmark_step.commands[0].argv[:4],
            ["/tmp/backend-python", "-m", "pytest", "tests/benchmark/test_ci_regression_gate.py"],
        )
        self.assertEqual(benchmark_step.commands[0].env_overrides["OPENCLAW_ENABLE_LIVE_BENCHMARK"], "1")
        self.assertIn("rerun_gate", benchmark_step.commands[0].argv)
        self.assertTrue(metadata["live_benchmark_enabled"])

    def test_build_release_steps_skips_windows_validation_by_default_without_failure(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"), mock.patch.object(gate.os, "name", "posix"):
            steps, _metadata = gate.build_release_steps(args, Path(tmp_dir))

        windows_step = next(step for step in steps if step.step_id == "3.4")
        self.assertEqual(
            windows_step.skip_reason,
            "Skipped by default; pass --enable-windows-native-validation to run the maintainer-only Windows native validation gate.",
        )
        self.assertFalse(windows_step.skip_causes_failure)
        self.assertEqual(windows_step.commands, [])
        self.assertFalse(_metadata["windows_native_validation_enabled"])

    def test_build_release_steps_requires_real_windows_host_when_windows_validation_is_enabled(self) -> None:
        args = _make_args(enable_windows_native_validation=True)

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"), mock.patch.object(gate.os, "name", "posix"):
            steps, _metadata = gate.build_release_steps(args, Path(tmp_dir))

        windows_step = next(step for step in steps if step.step_id == "3.4")
        self.assertEqual(
            windows_step.skip_reason,
            "Must run scripts/openclaw_memory_palace_windows_native_validation.py on a real Windows host.",
        )
        self.assertTrue(windows_step.skip_causes_failure)
        self.assertEqual(windows_step.commands, [])

    def test_build_release_steps_uses_real_windows_validation_script_when_available(self) -> None:
        args = _make_args(
            profile_smoke_model_env="C:/tmp/models.env",
            enable_windows_native_validation=True,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            checkpoint_root = Path(tmp_dir)
            with mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"), mock.patch.object(gate.os, "name", "nt"):
                steps, _metadata = gate.build_release_steps(args, checkpoint_root)

        windows_step = next(step for step in steps if step.step_id == "3.4")
        self.assertIsNone(windows_step.skip_reason)
        self.assertFalse(windows_step.skip_causes_failure)
        self.assertTrue(any("openclaw_memory_palace_windows_native_validation.py" in arg for arg in windows_step.commands[0].argv))
        self.assertIn("--profiles", windows_step.commands[0].argv)
        self.assertIn("b,c,d", windows_step.commands[0].argv)
        self.assertIn("--model-env", windows_step.commands[0].argv)
        self.assertTrue(_metadata["windows_native_validation_enabled"])

    def test_build_release_steps_enables_phase45_when_model_env_is_provided(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir:
            model_env_path = Path(tmp_dir) / "models.env"
            model_env_path.write_text("OPENAI_BASE_URL=http://127.0.0.1:8317/v1\n", encoding="utf-8")
            args.profile_smoke_model_env = str(model_env_path)
            with mock.patch.object(gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]), mock.patch.object(
                gate, "resolve_bun_command", return_value=["bun"]
            ), mock.patch.object(gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"), mock.patch.object(
                gate.shutil, "which", return_value="/tmp/bin"
            ):
                steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        phase45_steps = {step.step_id: step for step in steps if step.step_id.startswith("4.phase45.")}
        self.assertEqual(set(phase45_steps), {"4.phase45.c", "4.phase45.d"})
        for step in phase45_steps.values():
            self.assertIsNone(step.skip_reason)
            self.assertTrue(any("openclaw_memory_palace_phase45_e2e.py" in arg for arg in step.commands[0].argv))
            self.assertIn("--model-env", step.commands[0].argv)
        self.assertTrue(metadata["phase45_enabled"])
        compact_step = next(
            step for step in steps if step.step_id == "4.compact_reflection.c"
        )
        self.assertIsNone(compact_step.skip_reason)
        self.assertTrue(
            any(
                "openclaw_compact_context_reflection_e2e.py" in arg
                for arg in compact_step.commands[0].argv
            )
        )
        self.assertTrue(metadata["compact_context_reflection_enabled"])

    def test_build_release_steps_skips_phase45_without_model_env_without_failure(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
            steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        phase45_steps = [step for step in steps if step.step_id.startswith("4.phase45.")]
        self.assertEqual(len(phase45_steps), 2)
        self.assertFalse(metadata["phase45_enabled"])
        for step in phase45_steps:
            self.assertEqual(
                step.skip_reason,
                "No profile smoke model env was provided; skipped maintainer-only phase45 C/D gate.",
            )
            self.assertFalse(step.skip_causes_failure)
        compact_step = next(
            step for step in steps if step.step_id == "4.compact_reflection.c"
        )
        self.assertEqual(
            compact_step.skip_reason,
            "No profile smoke model env was provided; skipped maintainer-only compact_context reflection gate.",
        )
        self.assertFalse(compact_step.skip_causes_failure)
        self.assertFalse(metadata["compact_context_reflection_enabled"])

    def test_build_release_steps_skips_onboarding_apply_validate_without_model_env_without_failure(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
            steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        onboarding_step = next(step for step in steps if step.step_id == "3.55")
        self.assertEqual(
            onboarding_step.skip_reason,
            "No profile smoke model env was provided; skipped onboarding apply/validate E2E gate.",
        )
        self.assertFalse(onboarding_step.skip_causes_failure)
        self.assertFalse(metadata["onboarding_apply_validate_enabled"])
        self.assertEqual(metadata["onboarding_apply_validate_profiles"], "c,d")

    def test_build_release_steps_uses_openclaw_profile_model_env_fallback(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir:
            model_env_path = Path(tmp_dir) / "models.env"
            model_env_path.write_text(
                "RETRIEVAL_EMBEDDING_API_BASE=https://embedding.example/v1\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                gate.os.environ,
                {"OPENCLAW_PROFILE_MODEL_ENV": str(model_env_path)},
                clear=False,
            ), mock.patch.object(
                gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
            ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
                gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
            ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
                _steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        self.assertEqual(metadata["profile_smoke_model_env"], str(model_env_path.resolve()))

    def test_build_release_steps_enables_onboarding_apply_validate_with_model_env(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir:
            model_env_path = Path(tmp_dir) / "models.env"
            model_env_path.write_text(
                "\n".join(
                    [
                        "RETRIEVAL_EMBEDDING_API_BASE=https://embedding.example/v1",
                        "RETRIEVAL_EMBEDDING_MODEL=embed-large",
                        "RETRIEVAL_RERANKER_API_BASE=https://reranker.example/v1",
                        "RETRIEVAL_RERANKER_MODEL=reranker-large",
                        "OPENAI_BASE_URL=https://llm.example/v1",
                        "OPENAI_MODEL=gpt-5.4",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            args.profile_smoke_model_env = str(model_env_path)
            with mock.patch.object(
                gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
            ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
                gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
            ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
                steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        onboarding_step = next(step for step in steps if step.step_id == "3.55")
        self.assertIsNone(onboarding_step.skip_reason)
        self.assertFalse(onboarding_step.skip_causes_failure)
        self.assertEqual(onboarding_step.commands[0].argv[:2], ["/tmp/repo-python", "scripts/test_onboarding_apply_validate_e2e.py"])
        self.assertIn("--model-env", onboarding_step.commands[0].argv)
        self.assertIn(str(model_env_path.resolve()), onboarding_step.commands[0].argv)
        self.assertIn("--profiles", onboarding_step.commands[0].argv)
        self.assertIn("c,d", onboarding_step.commands[0].argv)
        self.assertEqual(onboarding_step.commands[0].env_overrides, {})
        self.assertEqual(len(onboarding_step.artifact_paths), 2)
        self.assertTrue(metadata["onboarding_apply_validate_enabled"])

    def test_build_release_steps_skips_current_host_strict_ui_by_default_without_failure(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]
        ), mock.patch.object(gate, "resolve_bun_command", return_value=["bun"]), mock.patch.object(
            gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"
        ), mock.patch.object(gate.shutil, "which", return_value="/tmp/bin"):
            steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        strict_ui_step = next(step for step in steps if step.step_id == "8.5")
        self.assertEqual(
            strict_ui_step.skip_reason,
            "Skipped by default; set RELEASE_GATE_ENABLE_CURRENT_HOST_STRICT_UI=1 to run the release-only current-host strict UI gate.",
        )
        self.assertFalse(strict_ui_step.skip_causes_failure)
        self.assertFalse(metadata["current_host_strict_ui_enabled"])
        self.assertEqual(metadata["current_host_strict_ui_profile"], "d")

    def test_build_release_steps_enables_current_host_strict_ui_when_requested(self) -> None:
        args = _make_args()

        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            gate.os.environ,
            {
                "RELEASE_GATE_ENABLE_CURRENT_HOST_STRICT_UI": "1",
                "RELEASE_GATE_CURRENT_HOST_STRICT_UI_PROFILE": "c",
            },
            clear=False,
        ), mock.patch.object(gate, "resolve_python_from_venv", side_effect=["/tmp/backend-python", "/tmp/repo-python"]), mock.patch.object(
            gate, "resolve_bun_command", return_value=["bun"]
        ), mock.patch.object(gate, "bash_script_path", return_value="/tmp/pre_publish_check.sh"), mock.patch.object(
            gate.shutil, "which", return_value="/tmp/bin"
        ):
            steps, metadata = gate.build_release_steps(args, Path(tmp_dir))

        strict_ui_step = next(step for step in steps if step.step_id == "8.5")
        self.assertIsNone(strict_ui_step.skip_reason)
        self.assertFalse(strict_ui_step.skip_causes_failure)
        self.assertEqual(strict_ui_step.commands[0].argv, ["node", "scripts/test_replacement_acceptance_webui.mjs"])
        self.assertEqual(
            strict_ui_step.commands[0].env_overrides["OPENCLAW_ONBOARDING_USE_CURRENT_HOST"],
            "true",
        )
        self.assertEqual(
            strict_ui_step.commands[0].env_overrides["OPENCLAW_ACCEPTANCE_STRICT_UI"],
            "true",
        )
        self.assertEqual(strict_ui_step.commands[0].env_overrides["OPENCLAW_PROFILE"], "c")
        self.assertTrue(metadata["current_host_strict_ui_enabled"])
        self.assertEqual(metadata["current_host_strict_ui_profile"], "c")

    def test_normalize_existing_step_state_marks_running_steps_pending(self) -> None:
        payload = gate.normalize_existing_step_state(
            {
                "1": {"status": "RUNNING", "started_at": "now"},
                "2": {"status": "PASS"},
            }
        )

        self.assertEqual(payload["1"]["status"], "PENDING")
        self.assertNotIn("started_at", payload["1"])
        self.assertEqual(payload["2"]["status"], "PASS")

    def test_main_resume_skips_completed_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkpoint_dir = Path(tmp_dir) / "checkpoint"
            report_path = Path(tmp_dir) / "report.md"
            checkpoint_path = checkpoint_dir / "checkpoint.json"
            first_log = checkpoint_dir / "logs" / "step1.log"
            second_log = checkpoint_dir / "logs" / "step2.log"
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "started_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "steps": {
                            "1": {
                                "title": "Step One",
                                "status": "PASS",
                                "log_path": str(first_log),
                                "commands": ["python -c pass"],
                            }
                        },
                        "plan_signature": [
                            {"step_id": "1", "title": "Step One", "commands": [["python", "-c", "pass"]], "skip_reason": None},
                            {"step_id": "2", "title": "Step Two", "commands": [["python", "-c", "pass"]], "skip_reason": None},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                report=str(report_path),
                checkpoint_dir=str(checkpoint_dir),
                resume=True,
                skip_backend_tests=False,
                skip_plugin_tests=False,
                skip_python_matrix=False,
                skip_frontend=False,
                skip_frontend_e2e=False,
                skip_profile_smoke=False,
                skip_phase45=False,
                skip_review_smoke=False,
                profile_modes="local",
                phase45_profiles="c,d",
                review_smoke_modes="local",
                profile_smoke_model_env="",
            )
            steps = [
                gate.ReleaseStep(
                    step_id="1",
                    title="Step One",
                    commands=[gate.StepCommand(["python", "-c", "pass"], Path(tmp_dir))],
                    timeout_seconds=30,
                    log_path=first_log,
                ),
                gate.ReleaseStep(
                    step_id="2",
                    title="Step Two",
                    commands=[gate.StepCommand(["python", "-c", "pass"], Path(tmp_dir))],
                    timeout_seconds=30,
                    log_path=second_log,
                ),
            ]
            metadata = {"profile_smoke_modes": "local", "review_smoke_modes": "local"}

            with mock.patch.object(gate, "parse_args", return_value=args), mock.patch.object(
                gate, "build_release_steps", return_value=(steps, metadata)
            ), mock.patch.object(gate, "run_step_commands", return_value=("PASS", 0, 1.0)) as run_step_commands:
                exit_code = gate.main()

        self.assertEqual(exit_code, 0)
        run_step_commands.assert_called_once()
        self.assertEqual(run_step_commands.call_args[0][0].step_id, "2")

    def test_main_retries_windows_profile_smoke_negative_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkpoint_dir = Path(tmp_dir) / "checkpoint"
            report_path = Path(tmp_dir) / "report.md"
            args = argparse.Namespace(
                report=str(report_path),
                checkpoint_dir=str(checkpoint_dir),
                resume=False,
                skip_backend_tests=False,
                skip_plugin_tests=False,
                skip_python_matrix=False,
                skip_frontend=False,
                skip_frontend_e2e=False,
                skip_profile_smoke=False,
                skip_phase45=False,
                skip_review_smoke=False,
                profile_modes="local",
                phase45_profiles="c,d",
                review_smoke_modes="local",
                profile_smoke_model_env="",
            )
            step = gate.ReleaseStep(
                step_id="4.local.a",
                title="Profile Smoke (local/a)",
                commands=[gate.StepCommand(["python", "smoke.py"], Path(tmp_dir))],
                timeout_seconds=30,
                log_path=checkpoint_dir / "logs" / "profile.log",
                artifact_paths=[checkpoint_dir / "artifacts" / "profile.md"],
            )
            metadata = {"profile_smoke_modes": "local", "review_smoke_modes": "local"}
            with mock.patch.object(gate, "parse_args", return_value=args), mock.patch.object(
                gate, "build_release_steps", return_value=([step], metadata)
            ), mock.patch.object(gate.os, "name", "nt"), mock.patch.object(
                gate, "run_step_commands", side_effect=[("FAIL", 0xFFFFFFFF, 1.0), ("PASS", 0, 1.0)]
            ) as run_step_commands:
                exit_code = gate.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_step_commands.call_count, 2)

    def test_main_allows_fresh_profile_smoke_artifact_to_override_negative_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkpoint_dir = Path(tmp_dir) / "checkpoint"
            report_path = Path(tmp_dir) / "report.md"
            artifact_path = checkpoint_dir / "artifacts" / "profile.md"
            args = argparse.Namespace(
                report=str(report_path),
                checkpoint_dir=str(checkpoint_dir),
                resume=False,
                skip_backend_tests=False,
                skip_plugin_tests=False,
                skip_python_matrix=False,
                skip_frontend=False,
                skip_frontend_e2e=False,
                skip_profile_smoke=False,
                skip_phase45=False,
                skip_review_smoke=False,
                profile_modes="local",
                phase45_profiles="c,d",
                review_smoke_modes="local",
                profile_smoke_model_env="",
            )
            step = gate.ReleaseStep(
                step_id="4.local.a",
                title="Profile Smoke (local/a)",
                commands=[gate.StepCommand(["python", "smoke.py"], Path(tmp_dir))],
                timeout_seconds=30,
                log_path=checkpoint_dir / "logs" / "profile.log",
                artifact_paths=[artifact_path],
            )
            metadata = {"profile_smoke_modes": "local", "review_smoke_modes": "local"}

            def _run_step_commands(_step):
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_text(
                    "# OpenClaw Memory Palace Profile Smoke Report\n\n| Mode | Profile | Status | Summary |\n|---|---|---|---|\n| local | A | PASS | ok |\n",
                    encoding="utf-8",
                )
                return ("FAIL", 0xFFFFFFFF, 1.0)

            with mock.patch.object(gate, "parse_args", return_value=args), mock.patch.object(
                gate, "build_release_steps", return_value=([step], metadata)
            ), mock.patch.object(gate.os, "name", "nt"), mock.patch.object(
                gate, "run_step_commands", side_effect=_run_step_commands
            ) as run_step_commands:
                exit_code = gate.main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_step_commands.call_count, 2)

    def test_main_non_resume_clears_stale_checkpoint_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkpoint_dir = Path(tmp_dir) / "checkpoint"
            report_path = Path(tmp_dir) / "report.md"
            stale_artifact = checkpoint_dir / "artifacts" / "stale.md"
            stale_artifact.parent.mkdir(parents=True, exist_ok=True)
            stale_artifact.write_text("stale", encoding="utf-8")
            args = argparse.Namespace(
                report=str(report_path),
                checkpoint_dir=str(checkpoint_dir),
                resume=False,
                skip_backend_tests=False,
                skip_plugin_tests=False,
                skip_python_matrix=False,
                skip_frontend=False,
                skip_frontend_e2e=False,
                skip_profile_smoke=False,
                skip_phase45=False,
                skip_review_smoke=False,
                profile_modes="local",
                phase45_profiles="c,d",
                review_smoke_modes="local",
                profile_smoke_model_env="",
            )
            step = gate.ReleaseStep(
                step_id="1",
                title="Step One",
                commands=[gate.StepCommand(["python", "-c", "pass"], Path(tmp_dir))],
                timeout_seconds=30,
                log_path=checkpoint_dir / "logs" / "step.log",
            )
            metadata = {"profile_smoke_modes": "local", "review_smoke_modes": "local"}
            with mock.patch.object(gate, "parse_args", return_value=args), mock.patch.object(
                gate, "build_release_steps", return_value=([step], metadata)
            ), mock.patch.object(gate, "run_step_commands", return_value=("PASS", 0, 1.0)):
                exit_code = gate.main()

        self.assertEqual(exit_code, 0)
        self.assertFalse(stale_artifact.exists())

    def test_main_fails_when_package_dry_run_log_contains_forbidden_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            checkpoint_dir = Path(tmp_dir) / "checkpoint"
            report_path = Path(tmp_dir) / "report.md"
            dry_run_log = checkpoint_dir / "logs" / "dry-run.log"
            args = argparse.Namespace(
                report=str(report_path),
                checkpoint_dir=str(checkpoint_dir),
                resume=False,
                skip_backend_tests=False,
                skip_plugin_tests=False,
                skip_python_matrix=False,
                skip_frontend=False,
                skip_frontend_e2e=False,
                skip_profile_smoke=False,
                skip_phase45=False,
                skip_review_smoke=False,
                profile_modes="local",
                phase45_profiles="c,d",
                review_smoke_modes="local",
                profile_smoke_model_env="",
            )
            step = gate.ReleaseStep(
                step_id="3.45",
                title="Package Dry Run Audit",
                commands=[gate.StepCommand(["python", "dry-run.py"], Path(tmp_dir))],
                timeout_seconds=30,
                log_path=dry_run_log,
            )
            metadata = {"profile_smoke_modes": "local", "review_smoke_modes": "local"}

            def _run_step_commands(_step):
                dry_run_log.parent.mkdir(parents=True, exist_ok=True)
                dry_run_log.write_text(
                    "npm notice release/frontend/.tmp/debug.log\n",
                    encoding="utf-8",
                )
                return ("PASS", 0, 1.0)

            with mock.patch.object(gate, "parse_args", return_value=args), mock.patch.object(
                gate, "build_release_steps", return_value=([step], metadata)
            ), mock.patch.object(gate, "run_step_commands", side_effect=_run_step_commands):
                exit_code = gate.main()

            checkpoint = json.loads((checkpoint_dir / "checkpoint.json").read_text(encoding="utf-8"))
            state = checkpoint["steps"]["3.45"]

        self.assertEqual(exit_code, 1)
        self.assertEqual(state["status"], "FAIL")
        self.assertEqual(state["runner_warning"], "package_dry_run_pollution_detected")
        self.assertEqual(state["forbidden_paths"], ["release/frontend/.tmp/"])

    def test_wrapper_release_gate_forwards_phase45_flags(self) -> None:
        args = argparse.Namespace(
            report="report.md",
            legacy_bash_gate=False,
            enable_live_benchmark=False,
            skip_backend_tests=False,
            skip_plugin_tests=False,
            skip_python_matrix=False,
            skip_frontend=False,
            skip_frontend_e2e=False,
            skip_profile_smoke=False,
            skip_phase45=True,
            skip_review_smoke=False,
            profile_modes="local",
            phase45_profiles="d",
            review_smoke_modes="docker",
            profile_smoke_model_env="",
            checkpoint_dir="checkpoint",
            resume=False,
        )

        with mock.patch.object(wrapper, "run_process", return_value=0) as run_process:
            exit_code = wrapper.command_release_gate(args)

        self.assertEqual(exit_code, 0)
        forwarded = run_process.call_args.args[0]
        self.assertIn("--skip-phase45", forwarded)
        self.assertIn("--phase45-profiles", forwarded)
        self.assertIn("d", forwarded)

    def test_wrapper_release_gate_forwards_live_benchmark_flag(self) -> None:
        args = argparse.Namespace(
            report="report.md",
            legacy_bash_gate=False,
            enable_live_benchmark=True,
            skip_backend_tests=False,
            skip_plugin_tests=False,
            skip_python_matrix=False,
            skip_frontend=False,
            skip_frontend_e2e=False,
            skip_profile_smoke=False,
            skip_phase45=False,
            skip_review_smoke=False,
            profile_modes="local",
            phase45_profiles="c,d",
            review_smoke_modes="docker",
            profile_smoke_model_env="",
            checkpoint_dir="checkpoint",
            resume=False,
        )

        with mock.patch.object(wrapper, "run_process", return_value=0) as run_process:
            exit_code = wrapper.command_release_gate(args)

        self.assertEqual(exit_code, 0)
        forwarded = run_process.call_args.args[0]
        self.assertIn("--enable-live-benchmark", forwarded)


if __name__ == "__main__":
    unittest.main()
