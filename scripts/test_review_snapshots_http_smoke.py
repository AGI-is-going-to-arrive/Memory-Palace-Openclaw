#!/usr/bin/env python3
from __future__ import annotations

import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import review_snapshots_http_smoke as smoke


class StopProcessTests(unittest.TestCase):
    def test_stop_process_uses_windows_taskkill_fallback(self) -> None:
        process = SimpleNamespace(pid=123, poll=lambda: None, wait=mock.Mock(return_value=0))
        original_name = smoke.os.name
        original_killpg = smoke._OS_KILLPG

        with mock.patch.object(smoke, "_kill_process_tree_windows") as kill_tree:
            smoke.os.name = "nt"
            smoke._OS_KILLPG = None
            try:
                smoke.stop_process(process)  # type: ignore[arg-type]
            finally:
                smoke.os.name = original_name
                smoke._OS_KILLPG = original_killpg

        kill_tree.assert_called_once_with(123, force=False)
        process.wait.assert_called_once_with(timeout=10)

    def test_stop_process_uses_killpg_on_posix(self) -> None:
        process = SimpleNamespace(pid=456, poll=lambda: None, wait=mock.Mock(return_value=0))
        original_name = smoke.os.name
        original_killpg = smoke._OS_KILLPG
        killpg = mock.Mock()

        smoke.os.name = "posix"
        smoke._OS_KILLPG = killpg
        try:
            smoke.stop_process(process)  # type: ignore[arg-type]
        finally:
            smoke.os.name = original_name
            smoke._OS_KILLPG = original_killpg

        killpg.assert_called_once_with(456, signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=10)

    def test_stop_process_force_kills_after_timeout(self) -> None:
        wait_mock = mock.Mock(side_effect=[subprocess.TimeoutExpired(cmd="x", timeout=10), 0])
        process = SimpleNamespace(pid=789, poll=lambda: None, wait=wait_mock)
        original_name = smoke.os.name
        original_killpg = smoke._OS_KILLPG
        killpg = mock.Mock()

        smoke.os.name = "posix"
        smoke._OS_KILLPG = killpg
        try:
            smoke.stop_process(process)  # type: ignore[arg-type]
        finally:
            smoke.os.name = original_name
            smoke._OS_KILLPG = original_killpg

        force_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        self.assertEqual(
            killpg.call_args_list,
            [mock.call(789, signal.SIGTERM), mock.call(789, force_signal)],
        )
        self.assertEqual(wait_mock.call_args_list, [mock.call(timeout=10), mock.call(timeout=10)])


class DockerModeTests(unittest.TestCase):
    def test_run_docker_mode_builds_env_file_via_python_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            env_file = temp_root / "docker-profile-b.env"
            built_payload = {"MCP_API_KEY": "docker-secret"}
            original_mkdtemp = smoke.tempfile.mkdtemp

            def fake_build_profile_env(platform: str, profile: str, target: Path, model_env: dict[str, str]):
                _ = platform
                _ = profile
                _ = model_env
                target.write_text("MCP_API_KEY=docker-secret\n", encoding="utf-8")
                return built_payload

            smoke.tempfile.mkdtemp = lambda prefix="", dir=None: str(temp_root)
            try:
                with mock.patch.object(
                    smoke.profile_smoke,
                    "build_profile_env",
                    side_effect=fake_build_profile_env,
                ) as build_profile_env, mock.patch.object(
                    smoke,
                    "run",
                    return_value=subprocess.CompletedProcess(["bash"], 1, "", "docker failed"),
                ):
                    result = smoke.run_docker_mode()
            finally:
                smoke.tempfile.mkdtemp = original_mkdtemp

        self.assertEqual(result.mode, "docker")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("docker_one_click failed", result.summary)
        build_profile_env.assert_called_once_with("docker", "b", env_file, {})

    def test_run_docker_backend_exec_retries_transient_backend_not_running(self) -> None:
        missing_container = subprocess.CompletedProcess(
            ["docker"],
            0,
            "",
            "",
        )
        transient_exec = subprocess.CompletedProcess(
            ["docker"],
            1,
            "",
            'service "backend" is not running',
        )
        found_container = subprocess.CompletedProcess(
            ["docker"],
            0,
            "container-123\n",
            "",
        )
        success = subprocess.CompletedProcess(["docker"], 0, '{"ok":true}', "")
        calls = [missing_container, found_container, transient_exec, found_container, success]

        with mock.patch.object(smoke, "run", side_effect=lambda *args, **kwargs: calls.pop(0)) as run_mock, mock.patch.object(
            smoke.time, "sleep"
        ) as sleep_mock:
            result = smoke._run_docker_backend_exec(
                env={},
                compose_project_name="review-http-docker-demo",
                command_args=["python", "-c", "print('ok')"],
                attempts=3,
                retry_delay_seconds=0.1,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run_mock.call_count, 5)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_run_docker_mode_retries_transient_port_bind_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            env_file = temp_root / "docker-profile-b.env"
            original_mkdtemp = smoke.tempfile.mkdtemp

            smoke.tempfile.mkdtemp = lambda prefix="", dir=None: str(temp_root)
            port_bind_fail = subprocess.CompletedProcess(
                ["bash"],
                1,
                "",
                "Error response from daemon: ports are not available",
            )
            down_ok = subprocess.CompletedProcess(["docker"], 0, "", "")
            up_ok = subprocess.CompletedProcess(["bash"], 0, "", "")
            final_down_ok = subprocess.CompletedProcess(["docker"], 0, "", "")
            run_results = [port_bind_fail, down_ok, up_ok, final_down_ok]

            def fake_run(*_args, **_kwargs):
                return run_results.pop(0)

            try:
                with mock.patch.object(
                    smoke.profile_smoke,
                    "build_profile_env",
                    side_effect=lambda platform, profile, target, model_env: (
                        target.write_text("MCP_API_KEY=docker-secret\n", encoding="utf-8"),
                        {"MCP_API_KEY": "docker-secret"},
                    )[1],
                ), mock.patch.object(smoke, "run", side_effect=fake_run) as run_mock, mock.patch.object(
                    smoke, "find_free_port", side_effect=[18080, 3000, 18081, 3001]
                ), mock.patch.object(smoke.time, "sleep") as sleep_mock, mock.patch.object(
                    smoke, "wait_for_http", side_effect=RuntimeError("stop-after-up")
                ):
                    result = smoke.run_docker_mode()
            finally:
                smoke.tempfile.mkdtemp = original_mkdtemp

        self.assertEqual(result.mode, "docker")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("stop-after-up", result.summary)
        self.assertEqual(run_mock.call_count, 4)
        sleep_mock.assert_called_once_with(2)

    def test_run_docker_mode_fails_cleanly_when_bash_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            env_file = temp_root / "docker-profile-b.env"
            original_mkdtemp = smoke.tempfile.mkdtemp

            smoke.tempfile.mkdtemp = lambda prefix="", dir=None: str(temp_root)
            try:
                with mock.patch.object(
                    smoke.profile_smoke,
                    "build_profile_env",
                    side_effect=lambda platform, profile, target, model_env: (
                        target.write_text("MCP_API_KEY=docker-secret\n", encoding="utf-8"),
                        {"MCP_API_KEY": "docker-secret"},
                    )[1],
                ), mock.patch.object(smoke.shutil, "which", side_effect=lambda name: None if name == "bash" else "/usr/bin/docker"):
                    result = smoke.run_docker_mode()
            finally:
                smoke.tempfile.mkdtemp = original_mkdtemp

        self.assertEqual(result.mode, "docker")
        self.assertEqual(result.status, "FAIL")
        self.assertIn("bash is required", result.summary)
        self.assertIn("PATH", result.details)


if __name__ == "__main__":
    unittest.main()
