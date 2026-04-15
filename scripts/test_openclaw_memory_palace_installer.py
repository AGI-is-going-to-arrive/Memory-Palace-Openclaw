#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_memory_palace as wrapper
import openclaw_memory_palace_python_matrix as python_matrix
import openclaw_memory_palace_installer as installer
import test_onboarding_apply_validate_e2e as onboarding_apply_validate
import test_openclaw_memory_palace_package_install as package_install_smoke


class InstallerTests(unittest.TestCase):
    class _FakeHttpResponse:
        def __init__(self, body: str, status: int = 200) -> None:
            self._body = body.encode("utf-8")
            self.status = status

        def read(self, size: int = -1) -> bytes:
            if size is None or size < 0:
                return self._body
            return self._body[:size]

        def __enter__(self) -> "InstallerTests._FakeHttpResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = exc_type, exc, tb
            return None

    def test_post_json_probe_reads_large_embedding_json_without_truncation(self) -> None:
        body = json.dumps(
            {"data": [{"embedding": ["0.1234567890123456"] * 4096}]},
            ensure_ascii=False,
        )
        self.assertGreater(len(body.encode("utf-8")), 65536)

        with mock.patch.object(installer, "urlopen", return_value=self._FakeHttpResponse(body)):
            ok, detail = installer.post_json_probe(
                base_url="https://embedding.example/v1",
                endpoint="/embeddings",
                payload={"model": "embed-large", "input": "probe"},
                api_key="embed-key",
                timeout_seconds=8.0,
            )

        self.assertTrue(ok)
        self.assertEqual(detail, "")

    def test_post_json_probe_payload_reads_large_embedding_json_without_truncation(self) -> None:
        body = json.dumps(
            {"data": [{"embedding": ["0.1234567890123456"] * 4096}]},
            ensure_ascii=False,
        )
        self.assertGreater(len(body.encode("utf-8")), 65536)

        with mock.patch.object(installer, "urlopen", return_value=self._FakeHttpResponse(body)):
            ok, detail, parsed = installer.post_json_probe_payload(
                base_url="https://embedding.example/v1",
                endpoint="/embeddings",
                payload={"model": "embed-large", "input": "probe"},
                api_key="embed-key",
                timeout_seconds=8.0,
            )

        self.assertTrue(ok)
        self.assertEqual(detail, "")
        self.assertEqual(installer.extract_embedding_dimension(parsed), 4096)

    def test_load_env_file_strips_wrapping_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "quoted.env"
            env_path.write_text(
                'API_KEY="sk-test"\nBASE_URL=\'https://example.com/v1\'\nPLAIN=value\n',
                encoding="utf-8",
            )

            loaded = installer.load_env_file(env_path)

        self.assertEqual(loaded["API_KEY"], "sk-test")
        self.assertEqual(loaded["BASE_URL"], "https://example.com/v1")
        self.assertEqual(loaded["PLAIN"], "value")

    def test_load_env_file_supports_export_prefix_and_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "export.env"
            env_path.write_text(
                "\ufeffexport API_KEY=sk-test\nexport BASE_URL='https://example.com/v1'\n",
                encoding="utf-8",
            )

            loaded = installer.load_env_file(env_path)

        self.assertEqual(loaded["API_KEY"], "sk-test")
        self.assertEqual(loaded["BASE_URL"], "https://example.com/v1")

    @unittest.skipIf(os.name == "nt", "POSIX-only permission assertion")
    def test_write_json_file_restricts_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"

            installer.write_json_file(config_path, {"ok": True}, dry_run=False)

            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX-only permission assertion")
    def test_write_env_file_restricts_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "runtime.env"

            installer.write_env_file(
                env_path,
                {"OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-5.4"},
                dry_run=False,
            )

            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

    def test_runtime_paths_are_cached(self) -> None:
        installer._runtime_paths.cache_clear()
        first = installer._runtime_paths()
        second = installer._runtime_paths()

        self.assertIs(first, second)
        self.assertGreaterEqual(installer._runtime_paths.cache_info().hits, 1)

    def test_detect_config_path_prefers_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            explicit = Path(tmp_dir) / "custom.json"
            detected = installer.detect_config_path(str(explicit))
            self.assertEqual(detected, explicit.resolve())

    def test_detect_config_path_prefers_openclaw_config_path_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {"OPENCLAW_CONFIG_PATH": str(Path(tmp_dir) / "env-config.json")},
            clear=False,
        ):
            detected, source = installer.detect_config_path_with_source()
        self.assertEqual(detected, (Path(tmp_dir) / "env-config.json").resolve())
        self.assertEqual(source, "env:OPENCLAW_CONFIG_PATH")

    def test_detect_config_path_uses_openclaw_config_env_when_primary_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {"OPENCLAW_CONFIG": str(Path(tmp_dir) / "alt-config.json")},
            clear=False,
        ):
            installer.os.environ.pop("OPENCLAW_CONFIG_PATH", None)
            detected, source = installer.detect_config_path_with_source()
        self.assertEqual(detected, (Path(tmp_dir) / "alt-config.json").resolve())
        self.assertEqual(source, "env:OPENCLAW_CONFIG")

    def test_detect_config_path_prefers_existing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cwd = Path(tmp_dir) / "workspace"
            home = Path(tmp_dir) / "home"
            candidate = home / ".openclaw" / "openclaw.json"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("{}", encoding="utf-8")
            with mock.patch.object(installer, "detect_config_path_from_openclaw", return_value=None):
                detected = installer.detect_config_path(cwd=cwd, home=home)
            self.assertEqual(detected, candidate.resolve())

    def test_detect_config_path_prefers_xdg_config_home_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {"XDG_CONFIG_HOME": str(Path(tmp_dir) / "xdg-config")},
            clear=False,
        ):
            cwd = Path(tmp_dir) / "workspace"
            home = Path(tmp_dir) / "home"
            candidate = Path(installer.os.environ["XDG_CONFIG_HOME"]) / "openclaw" / "config.json"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("{}", encoding="utf-8")
            with mock.patch.object(installer, "detect_config_path_from_openclaw", return_value=None):
                detected, source = installer.detect_config_path_with_source(cwd=cwd, home=home)
        self.assertEqual(detected, candidate.resolve())
        self.assertEqual(source, f"detected:{candidate}")

    def test_detect_config_path_defaults_to_xdg_config_home_when_unset_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {"XDG_CONFIG_HOME": str(Path(tmp_dir) / "xdg-config")},
            clear=False,
        ):
            cwd = Path(tmp_dir) / "workspace"
            home = Path(tmp_dir) / "home"
            expected_candidate = Path(installer.os.environ["XDG_CONFIG_HOME"]) / "openclaw" / "config.json"
            expected = expected_candidate.resolve()
            with mock.patch.object(installer, "detect_config_path_from_openclaw", return_value=None):
                detected, source = installer.detect_config_path_with_source(cwd=cwd, home=home)
        self.assertEqual(detected, expected)
        self.assertEqual(source, f"default:{expected_candidate}")

    def test_candidate_config_paths_include_windows_native_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "APPDATA": str(Path(tmp_dir) / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(Path(tmp_dir) / "AppData" / "Local"),
            },
            clear=False,
        ):
            candidates = installer.candidate_config_paths(
                cwd=Path(tmp_dir) / "workspace",
                home=Path(tmp_dir) / "home",
            )

        self.assertIn(
            Path(tmp_dir) / "AppData" / "Roaming" / "OpenClaw" / "openclaw.json",
            candidates,
        )
        self.assertIn(
            Path(tmp_dir) / "AppData" / "Local" / "OpenClaw" / "config.json",
            candidates,
        )

    def test_detect_config_path_prefers_existing_appdata_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {"APPDATA": str(Path(tmp_dir) / "AppData" / "Roaming")},
            clear=False,
        ):
            cwd = Path(tmp_dir) / "workspace"
            home = Path(tmp_dir) / "home"
            candidate = Path(installer.os.environ["APPDATA"]) / "OpenClaw" / "openclaw.json"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("{}", encoding="utf-8")
            with mock.patch.object(installer, "detect_config_path_from_openclaw", return_value=None):
                detected, source = installer.detect_config_path_with_source(cwd=cwd, home=home)
        self.assertEqual(detected, candidate.resolve())
        self.assertEqual(source, f"detected:{candidate}")

    def test_parse_openclaw_version_text_handles_cli_banner(self) -> None:
        self.assertEqual(
            installer.parse_openclaw_version_text("OpenClaw 2026.3.13 (61d171a)"),
            (2026, 3, 13),
        )

    def test_detect_config_path_prefers_openclaw_cli_over_existing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cwd = Path(tmp_dir) / "workspace"
            home = Path(tmp_dir) / "home"
            candidate = home / ".openclaw" / "openclaw.json"
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("{}", encoding="utf-8")
            cli_path = (home / ".config" / "openclaw" / "config.json").resolve()
            with mock.patch.object(installer, "detect_config_path_from_openclaw", return_value=cli_path):
                detected, source = installer.detect_config_path_with_source(cwd=cwd, home=home)
            self.assertEqual(detected, cli_path)
            self.assertEqual(source, "openclaw config file")

    def test_detect_config_path_from_openclaw_ignores_hook_runner_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text("{}", encoding="utf-8")
            stdout = "\n".join(
                [
                    str(config_path),
                    "[plugins] hook runner initialized with 1 registered hooks",
                    "[plugins] hook runner initialized with 1 registered hooks",
                ]
            )
            completed = subprocess.CompletedProcess(
                args=["openclaw", "config", "file"],
                returncode=0,
                stdout=stdout,
                stderr="",
            )
            with mock.patch.object(installer, "resolve_openclaw_binary", return_value="openclaw"), mock.patch.object(
                installer.subprocess,
                "run",
                return_value=completed,
            ):
                detected = installer.detect_config_path_from_openclaw()
            self.assertEqual(detected, config_path.resolve())

    def test_detect_config_path_keeps_local_workspace_config_ahead_of_openclaw_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cwd = Path(tmp_dir) / "workspace"
            home = Path(tmp_dir) / "home"
            local_candidate = cwd / ".openclaw" / "config.json"
            local_candidate.parent.mkdir(parents=True, exist_ok=True)
            local_candidate.write_text("{}", encoding="utf-8")
            cli_path = (home / ".config" / "openclaw" / "config.json").resolve()
            with mock.patch.object(installer, "detect_config_path_from_openclaw", return_value=cli_path):
                detected, source = installer.detect_config_path_with_source(cwd=cwd, home=home)
            self.assertEqual(detected, local_candidate.resolve())
            self.assertEqual(source, f"detected:{local_candidate}")

    def test_detect_config_path_uses_openclaw_cli_probe_when_candidates_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cwd = Path(tmp_dir) / "workspace"
            home = Path(tmp_dir) / "home"
            cli_path = (Path(tmp_dir) / "detected.json").resolve()
            with mock.patch.object(installer, "detect_config_path_from_openclaw", return_value=cli_path):
                detected, source = installer.detect_config_path_with_source(cwd=cwd, home=home)
            self.assertEqual(detected, cli_path)
            self.assertEqual(source, "openclaw config file")

    def test_detect_config_path_forwards_openclaw_bin_to_cli_probe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            cwd = Path(tmp_dir) / "workspace"
            home = Path(tmp_dir) / "home"
            cli_path = (Path(tmp_dir) / "detected.json").resolve()
            with mock.patch.object(
                installer,
                "detect_config_path_from_openclaw",
                return_value=cli_path,
            ) as detect_from_cli:
                detected = installer.detect_config_path(
                    cwd=cwd,
                    home=home,
                    openclaw_bin="/custom/bin/openclaw",
                )
            self.assertEqual(detected, cli_path)
            detect_from_cli.assert_called_once_with(openclaw_bin="/custom/bin/openclaw")

    def test_detect_config_path_prefers_openclaw_bin_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {"OPENCLAW_BIN": "/env/bin/openclaw"},
            clear=False,
        ):
            cli_path = (Path(tmp_dir) / "detected.json").resolve()
            with mock.patch.object(
                installer,
                "detect_config_path_from_openclaw",
                return_value=cli_path,
            ) as detect_from_cli:
                detected = installer.detect_config_path()

        self.assertEqual(detected, cli_path)
        detect_from_cli.assert_called_once_with(openclaw_bin=None)

    def test_detect_installed_plugin_root_accepts_prefixed_json_output(self) -> None:
        payload = {
            "source": "/tmp/openclaw-memory-palace/index.ts",
        }
        completed = mock.Mock(returncode=0, stdout='[plugins] ready\n' + json.dumps(payload))

        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/openclaw"), mock.patch.object(
            installer.subprocess, "run", return_value=completed
        ):
            detected = installer.detect_installed_plugin_root()

        self.assertEqual(detected, Path("/tmp/openclaw-memory-palace").resolve())

    def test_detect_installed_plugin_root_normalizes_dist_entry_back_to_plugin_root(self) -> None:
        payload = {
            "source": "/tmp/openclaw-memory-palace/dist/index.js",
        }
        completed = mock.Mock(returncode=0, stdout=json.dumps(payload))

        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/openclaw"), mock.patch.object(
            installer.subprocess, "run", return_value=completed
        ):
            detected = installer.detect_installed_plugin_root()

        self.assertEqual(detected, Path("/tmp/openclaw-memory-palace").resolve())

    def test_detect_installed_plugin_root_reads_nested_plugin_source(self) -> None:
        payload = {
            "plugin": {
                "source": "/tmp/openclaw-memory-palace/dist/index.js",
            }
        }
        completed = mock.Mock(returncode=0, stdout=json.dumps(payload))

        with mock.patch.object(installer.shutil, "which", return_value="/usr/bin/openclaw"), mock.patch.object(
            installer.subprocess, "run", return_value=completed
        ):
            detected = installer.detect_installed_plugin_root()

        self.assertEqual(detected, Path("/tmp/openclaw-memory-palace").resolve())

    def test_resolve_plugin_install_root_from_info_accepts_directory_source(self) -> None:
        payload = {
            "plugin": {
                "source": "/tmp/openclaw-memory-palace",
            }
        }

        detected = installer.resolve_plugin_install_root_from_info(payload)

        self.assertEqual(detected, Path("/tmp/openclaw-memory-palace").resolve())

    def test_resolve_plugin_install_root_from_info_accepts_root_dir_and_install_path(self) -> None:
        for key in ("rootDir", "installPath"):
            with self.subTest(key=key):
                payload = {
                    "plugin": {
                        key: "/tmp/openclaw-memory-palace",
                    }
                }

                detected = installer.resolve_plugin_install_root_from_info(payload)

                self.assertEqual(detected, Path("/tmp/openclaw-memory-palace").resolve())

    def test_package_install_smoke_resolves_plugin_root_from_info(self) -> None:
        payload = {
            "plugin": {
                "source": "/tmp/openclaw-memory-palace",
            }
        }

        detected = package_install_smoke.resolve_plugin_install_root_from_info(payload)

        self.assertEqual(detected, Path("/tmp/openclaw-memory-palace").resolve())

    def test_get_plugin_info_value_reads_nested_plugin_payload(self) -> None:
        payload = {
            "plugin": {
                "source": "/tmp/openclaw-memory-palace/dist/index.js",
                "hookNames": ["memory-palace-visual-harvest"],
            }
        }

        self.assertEqual(
            installer.get_plugin_info_value(payload, "source"),
            "/tmp/openclaw-memory-palace/dist/index.js",
        )
        self.assertEqual(
            installer.get_plugin_info_value(payload, "hookNames"),
            ["memory-palace-visual-harvest"],
        )

    def test_package_install_scripts_compile_on_python_310_and_311_when_available(self) -> None:
        targets = [
            Path(package_install_smoke.__file__).resolve(),
            Path(python_matrix.__file__).resolve(),
            Path("scripts/installer/_utils.py").resolve(),
        ]
        resolutions: list[python_matrix.PythonResolution] = []
        missing_versions: list[str] = []
        for version in ("3.10", "3.11"):
            try:
                resolutions.append(python_matrix.resolve_python(version))
            except RuntimeError:
                missing_versions.append(version)
        if missing_versions:
            self.skipTest(
                f"Python interpreters unavailable for compatibility compile check: {', '.join(missing_versions)}"
            )

        for resolution in resolutions:
            for target in targets:
                with self.subTest(version=resolution.version, target=str(target)):
                    completed = subprocess.run(
                        [str(resolution.executable), "-m", "py_compile", str(target)],
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(
                        completed.returncode,
                        0,
                        msg=(
                            f"py_compile failed for Python {resolution.version} on {target}\n"
                            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
                        ),
                    )

    def test_merge_openclaw_config_is_idempotent(self) -> None:
        entry_payload = installer.build_plugin_entry(
            transport="stdio",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            database_url="sqlite+aiosqlite:////tmp/demo.db",
            timeout_ms=20_000,
            connect_retries=1,
            connect_backoff_ms=250,
        )
        first, _ = installer.merge_openclaw_config({}, entry_payload=entry_payload, activate=True)
        second, _ = installer.merge_openclaw_config(first, entry_payload=entry_payload, activate=True)
        self.assertEqual(first, second)
        self.assertEqual(first["hooks"]["internal"]["enabled"], True)

    def test_merge_openclaw_config_enables_memory_core_facade_compat(self) -> None:
        entry_payload = installer.build_plugin_entry(
            transport="stdio",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            database_url="sqlite+aiosqlite:////tmp/demo.db",
            timeout_ms=20_000,
            connect_retries=1,
            connect_backoff_ms=250,
        )

        merged, actions = installer.merge_openclaw_config(
            {},
            entry_payload=entry_payload,
            activate=True,
        )

        self.assertIn("memory-palace", merged["plugins"]["allow"])
        self.assertIn("memory-core", merged["plugins"]["allow"])
        self.assertEqual(merged["plugins"]["entries"]["memory-core"]["enabled"], True)
        self.assertIn(
            "ensured plugins.allow contains memory-core for host facade compatibility",
            actions,
        )
        self.assertIn(
            "enabled plugins.entries.memory-core for host facade compatibility",
            actions,
        )

    def test_profile_template_path_supports_windows_profiles(self) -> None:
        candidate = installer.profile_template_path("b", "windows")
        self.assertTrue(candidate.as_posix().endswith("deploy/profiles/windows/profile-b.env"))

    def test_host_platform_name_accepts_linux(self) -> None:
        self.assertEqual(installer.host_platform_name("linux"), "linux")

    def test_profile_template_path_supports_linux_profiles(self) -> None:
        candidate = installer.profile_template_path("b", "linux")
        self.assertTrue(candidate.as_posix().endswith("deploy/profiles/linux/profile-b.env"))

    def test_build_default_stdio_launch_falls_back_to_bash_when_zsh_missing(self) -> None:
        with mock.patch.object(installer, "host_platform_name", return_value="macos"), mock.patch.object(
            installer, "stdio_wrapper", return_value=Path("/tmp/run_memory_palace_mcp_stdio.sh")
        ), mock.patch.object(installer, "project_root", return_value=Path("/tmp/project")), mock.patch.object(
            installer, "_path_exists", side_effect=lambda value: str(value) == "/bin/bash"
        ), mock.patch.object(
            installer.shutil, "which", side_effect=lambda name: None
        ), mock.patch.dict(
            installer.os.environ, {}, clear=False
        ):
            command, args, cwd = installer.build_default_stdio_launch(host_platform="macos")

        self.assertEqual(command, "/bin/bash")
        self.assertEqual([Path(item).as_posix() for item in args], ["/tmp/run_memory_palace_mcp_stdio.sh"])
        self.assertEqual(Path(cwd).as_posix(), "/tmp/project")

    def test_build_default_stdio_launch_wraps_wrapper_with_bash_when_zsh_is_selected(self) -> None:
        with mock.patch.object(installer, "host_platform_name", return_value="macos"), mock.patch.object(
            installer, "stdio_wrapper", return_value=Path("/tmp/run memory palace mcp stdio.sh")
        ), mock.patch.object(installer, "project_root", return_value=Path("/tmp/project")), mock.patch.object(
            installer, "_path_exists", side_effect=lambda value: str(value) in {"/bin/zsh", "/bin/bash"}
        ), mock.patch.object(
            installer.shutil, "which", side_effect=lambda name: f"/bin/{name}"
        ), mock.patch.dict(
            installer.os.environ, {"SHELL": "/bin/zsh"}, clear=False
        ):
            command, args, cwd = installer.build_default_stdio_launch(host_platform="macos")

        self.assertEqual(command, "/bin/zsh")
        self.assertEqual(args, ["-lc", "/bin/bash '/tmp/run memory palace mcp stdio.sh'"])
        self.assertEqual(Path(cwd).as_posix(), "/tmp/project")

    def test_build_default_stdio_launch_falls_back_to_python_wrapper_when_shell_wrapper_is_unavailable(self) -> None:
        with mock.patch.object(installer, "host_platform_name", return_value="macos"), mock.patch.object(
            installer, "stdio_wrapper", return_value=Path("/tmp/run_memory_palace_mcp_stdio.sh")
        ), mock.patch.object(
            installer, "windows_stdio_wrapper", return_value=Path("/tmp/backend/mcp_wrapper.py")
        ), mock.patch.object(
            installer, "backend_root", return_value=Path("/tmp/backend")
        ), mock.patch.object(
            installer, "default_runtime_python_path", return_value=Path("/tmp/runtime/bin/python")
        ), mock.patch.object(
            installer, "_path_exists", return_value=False
        ), mock.patch.object(
            installer.shutil, "which", side_effect=lambda name: "/bin/zsh" if name == "zsh" else None
        ), mock.patch.dict(
            installer.os.environ, {"SHELL": "/bin/zsh"}, clear=False
        ):
            command, args, cwd = installer.build_default_stdio_launch(host_platform="macos")

        self.assertEqual(command, "/tmp/runtime/bin/python")
        self.assertEqual(args, ["/tmp/backend/mcp_wrapper.py"])
        self.assertEqual(cwd, "/tmp/backend")

    def test_apply_profile_sh_supports_linux(self) -> None:
        if os.name == "nt":
            self.skipTest("linux profile template script is exercised in Linux CI")
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "linux-profile.env"
            result = subprocess.run(
                [
                    "bash",
                    "scripts/apply_profile.sh",
                    "linux",
                    "b",
                    str(target),
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            self.assertTrue(target.is_file())
            rendered = target.read_text(encoding="utf-8")
            self.assertIn("RETRIEVAL_EMBEDDING_MODEL=hash-v1", rendered)
            self.assertIn("DATABASE_URL=sqlite+aiosqlite:////", rendered)

    def test_apply_profile_sh_uses_tmpdir_for_temp_files(self) -> None:
        script_text = (
            Path(__file__).resolve().parents[1] / "scripts" / "apply_profile.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('mktemp "${TMPDIR:-/tmp}/${file_path##*/}.XXXXXX"', script_text)

    def test_apply_profile_sh_detects_windows_uname_variants(self) -> None:
        script_text = (
            Path(__file__).resolve().parents[1] / "scripts" / "apply_profile.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('MINGW*|MSYS*|CYGWIN*) printf \'%s\\n\' "windows" ;;', script_text)

    def test_docker_one_click_hardens_temp_env_files(self) -> None:
        script_text = (
            Path(__file__).resolve().parents[1] / "scripts" / "docker_one_click.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('tmp_file="$(mktemp "/tmp/mp-env-upsert-XXXXXX")"', script_text)
        self.assertIn('chmod 600 "${tmp_file}"', script_text)
        self.assertIn(
            'PRESERVED_DOCKER_ENV_FILE="$(mktemp "${TMPDIR:-/tmp}/memory-palace-docker-env-preserve-XXXXXX")"',
            script_text,
        )
        self.assertIn('chmod 600 "${PRESERVED_DOCKER_ENV_FILE}"', script_text)

    def test_apply_profile_sh_normalizes_crlf_templates_before_autofill(self) -> None:
        if os.name == "nt":
            self.skipTest("bash-only script is exercised on POSIX hosts")
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            scripts_dir = project_root / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            (project_root / "deploy" / "profiles" / "linux").mkdir(parents=True, exist_ok=True)

            source_script = Path(__file__).resolve().parents[1] / "scripts" / "apply_profile.sh"
            target_script = scripts_dir / "apply_profile.sh"
            target_script.write_text(source_script.read_text(encoding="utf-8"), encoding="utf-8")
            target_script.chmod(0o755)

            (project_root / ".env.example").write_bytes(
                (
                    "DATABASE_URL=sqlite+aiosqlite:////home/<your-user>/memory_palace/agent_memory.db\r\n"
                    "RETRIEVAL_EMBEDDING_MODEL=hash-v1\r\n"
                ).encode("utf-8")
            )
            (project_root / "deploy" / "profiles" / "linux" / "profile-b.env").write_bytes(
                "PROFILE_MARKER=test-b\r\n".encode("utf-8")
            )

            target = project_root / "linux-profile.env"
            result = subprocess.run(
                [
                    "bash",
                    str(target_script),
                    "linux",
                    "b",
                    str(target),
                ],
                cwd=str(project_root),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            rendered = target.read_bytes()
            self.assertNotIn(b"\r", rendered)
            self.assertIn(b"DATABASE_URL=sqlite+aiosqlite:////", rendered)
            self.assertIn(b"/demo.db\n", rendered)
            self.assertNotIn(
                b"sqlite+aiosqlite:////home/<your-user>/memory_palace/agent_memory.db",
                rendered,
            )
            self.assertIn(b"PROFILE_MARKER=test-b\n", rendered)

    def test_apply_profile_sh_detects_windows_from_mingw_uname(self) -> None:
        if os.name == "nt":
            self.skipTest("bash-only script is exercised on POSIX hosts")
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            scripts_dir = project_root / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            (project_root / "deploy" / "profiles" / "windows").mkdir(parents=True, exist_ok=True)
            fake_bin = project_root / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)

            source_script = Path(__file__).resolve().parents[1] / "scripts" / "apply_profile.sh"
            target_script = scripts_dir / "apply_profile.sh"
            target_script.write_text(source_script.read_text(encoding="utf-8"), encoding="utf-8")
            target_script.chmod(0o755)

            (project_root / ".env.example").write_text(
                "DATABASE_URL=sqlite+aiosqlite:///C:/memory_palace/agent_memory.db\n",
                encoding="utf-8",
            )
            (project_root / "deploy" / "profiles" / "windows" / "profile-b.env").write_text(
                "PROFILE_MARKER=windows-b\n",
                encoding="utf-8",
            )
            (fake_bin / "uname").write_text(
                "#!/usr/bin/env bash\nprintf 'MINGW64_NT-10.0\\n'\n",
                encoding="utf-8",
            )
            (fake_bin / "uname").chmod(0o755)

            target = project_root / "windows-profile.env"
            result = subprocess.run(
                ["bash", str(target_script), "", "b", str(target)],
                cwd=str(project_root),
                env={**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"},
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            rendered = target.read_text(encoding="utf-8")
            self.assertIn("PROFILE_MARKER=windows-b", rendered)
            self.assertIn("DATABASE_URL=sqlite+aiosqlite:///", rendered)

    def test_apply_profile_sh_writes_temp_files_under_tmpdir(self) -> None:
        if os.name == "nt":
            self.skipTest("bash-only script is exercised on POSIX hosts")
        with tempfile.TemporaryDirectory() as tmp_dir:
            project_root = Path(tmp_dir)
            scripts_dir = project_root / "scripts"
            scripts_dir.mkdir(parents=True, exist_ok=True)
            (project_root / "deploy" / "profiles" / "linux").mkdir(parents=True, exist_ok=True)
            fake_bin = project_root / "fake-bin"
            fake_bin.mkdir(parents=True, exist_ok=True)
            mktemp_log = project_root / "mktemp.log"
            tmp_root = project_root / "tmp-root"
            tmp_root.mkdir(parents=True, exist_ok=True)

            source_script = Path(__file__).resolve().parents[1] / "scripts" / "apply_profile.sh"
            target_script = scripts_dir / "apply_profile.sh"
            target_script.write_text(source_script.read_text(encoding="utf-8"), encoding="utf-8")
            target_script.chmod(0o755)

            (project_root / ".env.example").write_text(
                "DATABASE_URL=sqlite+aiosqlite:////home/<your-user>/memory_palace/agent_memory.db\n",
                encoding="utf-8",
            )
            (project_root / "deploy" / "profiles" / "linux" / "profile-b.env").write_text(
                "PROFILE_MARKER=linux-b\n",
                encoding="utf-8",
            )
            (fake_bin / "mktemp").write_text(
                "\n".join(
                    [
                        "#!/usr/bin/env bash",
                        "template=\"$1\"",
                        f"printf '%s\\n' \"$template\" >> {str(mktemp_log)!r}",
                        "path=\"${template/XXXXXX/fake123}\"",
                        "mkdir -p \"$(dirname \"$path\")\"",
                        "touch \"$path\"",
                        "chmod 600 \"$path\"",
                        "printf '%s\\n' \"$path\"",
                    ]
                ) + "\n",
                encoding="utf-8",
            )
            (fake_bin / "mktemp").chmod(0o755)

            target = project_root / "linux-profile.env"
            result = subprocess.run(
                ["bash", str(target_script), "linux", "b", str(target)],
                cwd=str(project_root),
                env={
                    **os.environ,
                    "TMPDIR": str(tmp_root),
                    "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                },
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
            logged_templates = mktemp_log.read_text(encoding="utf-8").splitlines()
            self.assertGreaterEqual(len(logged_templates), 2)
            self.assertTrue(all(template.startswith(f"{tmp_root}/") for template in logged_templates))

    def test_sqlite_url_for_file_supports_windows_drive_paths(self) -> None:
        url = installer.sqlite_url_for_file(Path("C:/memory-palace/demo.db"))
        self.assertEqual(url, "sqlite+aiosqlite:///C:/memory-palace/demo.db")

    def test_sqlite_url_for_file_preserves_windows_unc_network_share_path(self) -> None:
        url = installer.sqlite_url_for_file(Path(r"\\server\share\memory-palace\demo.db"))
        self.assertEqual(url, "sqlite+aiosqlite://///server/share/memory-palace/demo.db")

    def test_read_json_file_accepts_trailing_commas_and_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                """{
  // comment
  models: {
    providers: {
      demo: {type: "openai",},
    },
  },
}
""",
                encoding="utf-8",
            )

            payload = installer.read_json_file(config_path)

        self.assertEqual(payload["models"]["providers"]["demo"]["type"], "openai")

    def test_backup_config_file_creates_timestamped_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text('{"plugins":{}}\n', encoding="utf-8")
            if os.name != "nt":
                config_path.chmod(0o644)

            backup_path = installer.backup_config_file(
                config_path,
                label="memory-palace-setup",
                dry_run=False,
            )

            self.assertIsNotNone(backup_path)
            self.assertTrue(str(backup_path).endswith(".bak"))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(Path(backup_path).stat().st_mode), 0o600)

    def test_runtime_requirements_path_prefers_runtime_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            backend_dir = Path(tmp_dir) / "backend"
            backend_dir.mkdir(parents=True, exist_ok=True)
            runtime_file = backend_dir / "requirements-runtime.txt"
            runtime_file.write_text("fastapi>=0.109.0\n", encoding="utf-8")
            (backend_dir / "requirements.txt").write_text("pytest>=8.0.0\n", encoding="utf-8")

            with mock.patch.object(installer, "backend_root", return_value=backend_dir):
                detected = installer.runtime_requirements_path()

        self.assertEqual(detected, runtime_file)

    def test_ensure_runtime_venv_retries_runtime_requirements_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime-root"
            backend_dir = Path(tmp_dir) / "backend"
            backend_dir.mkdir(parents=True, exist_ok=True)
            runtime_requirements = backend_dir / "requirements-runtime.txt"
            runtime_requirements.write_text("fastapi>=0.109.0\n", encoding="utf-8")
            runtime_python = installer.default_runtime_python_path(setup_root)

            attempts: list[list[str]] = []

            def fake_run(command, **kwargs):
                if command[:3] == [str(runtime_python), "-m", "pip"]:
                    attempts.append(list(command))
                    if len(attempts) == 1:
                        return subprocess.CompletedProcess(command, 1, "", "transient failure")
                    return subprocess.CompletedProcess(command, 0, "installed ok\n", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(installer, "backend_root", return_value=backend_dir), mock.patch.object(
                installer, "project_root", return_value=Path(tmp_dir)
            ), mock.patch.object(
                installer.venv.EnvBuilder,
                "create",
                side_effect=lambda target: runtime_python.parent.mkdir(parents=True, exist_ok=True)
                or runtime_python.write_text("", encoding="utf-8"),
            ), mock.patch.object(
                installer.subprocess, "run", side_effect=fake_run
            ), mock.patch.object(
                installer.time, "sleep"
            ) as sleep_mock:
                python_path, actions = installer.ensure_runtime_venv(
                    setup_root_path=setup_root,
                    dry_run=False,
                )

        self.assertEqual(python_path, runtime_python)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(str(runtime_requirements) in command for command in attempts))
        self.assertIn("installed backend requirements from requirements-runtime.txt into runtime venv", actions)
        sleep_mock.assert_called_once()

    def test_ensure_runtime_venv_accepts_python_314(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime-root"
            runtime_python = installer.default_runtime_python_path(setup_root)

            with mock.patch.object(installer.sys, "version_info", (3, 14, 0)), mock.patch.object(
                installer.venv.EnvBuilder,
                "create",
                side_effect=lambda target: runtime_python.parent.mkdir(parents=True, exist_ok=True)
                or runtime_python.write_text("", encoding="utf-8"),
            ), mock.patch.object(installer, "runtime_requirements_path", return_value=None):
                python_path, actions = installer.ensure_runtime_venv(
                    setup_root_path=setup_root,
                    dry_run=False,
                )

        self.assertEqual(python_path, runtime_python)
        self.assertTrue(any("created runtime venv" in item for item in actions))

    def test_ensure_runtime_venv_rejects_python_315(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            installer.sys,
            "version_info",
            (3, 15, 0),
        ):
            with self.assertRaisesRegex(RuntimeError, "Python 3.10-3.14"):
                installer.ensure_runtime_venv(
                    setup_root_path=Path(tmp_dir) / "runtime-root",
                    dry_run=True,
                )

    def test_build_plugin_entry_uses_windows_native_stdio_launch(self) -> None:
        entry_payload = installer.build_plugin_entry(
            transport="stdio",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            database_url="sqlite+aiosqlite:///C:/memory-palace/demo.db",
            timeout_ms=20_000,
            connect_retries=1,
            connect_backoff_ms=250,
            runtime_env_file=Path("C:/Users/demo/.openclaw/memory-palace/runtime.env"),
            runtime_python_path=Path("C:/Users/demo/.openclaw/memory-palace/runtime/Scripts/python.exe"),
            runtime_root=Path("C:/Users/demo/.openclaw/memory-palace"),
            transport_diagnostics_path=Path("C:/Users/demo/.openclaw/memory-palace/observability.json"),
            host_platform="windows",
        )

        stdio = entry_payload["config"]["stdio"]
        self.assertEqual(
            Path(stdio["command"]).as_posix(),
            Path("C:/Users/demo/.openclaw/memory-palace/runtime/Scripts/python.exe").as_posix(),
        )
        self.assertEqual(
            [Path(item).as_posix() for item in stdio["args"]],
            [installer.windows_stdio_wrapper().as_posix()],
        )
        self.assertEqual(Path(stdio["cwd"]).as_posix(), installer.backend_root().as_posix())
        self.assertEqual(stdio["env"]["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(stdio["env"]["PYTHONUTF8"], "1")

    def test_build_plugin_entry_enables_phase123_defaults(self) -> None:
        entry_payload = installer.build_plugin_entry(
            profile="b",
            transport="stdio",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            database_url="sqlite+aiosqlite:////tmp/demo.db",
            timeout_ms=20_000,
            connect_retries=1,
            connect_backoff_ms=250,
        )

        config = entry_payload["config"]
        self.assertEqual(
            config["profileMemory"],
            {
                "enabled": True,
                "injectBeforeAgentStart": True,
                "maxCharsPerBlock": 1200,
                "blocks": ["identity", "preferences", "workflow"],
            },
        )
        self.assertEqual(
            config["hostBridge"],
            {
                "enabled": True,
                "importUserMd": True,
                "importMemoryMd": True,
                "importDailyMemory": True,
                "writeBackSummary": False,
            },
        )
        self.assertEqual(
            config["capturePipeline"],
            {
                "mode": "v2",
                "captureAssistantDerived": True,
                "maxAssistantDerivedPerRun": 2,
                "pendingOnFailure": True,
            },
        )

    def test_build_plugin_entry_raises_stdio_timeout_for_profile_c(self) -> None:
        entry_payload = installer.build_plugin_entry(
            profile="c",
            transport="stdio",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            database_url="sqlite+aiosqlite:////tmp/demo.db",
            timeout_ms=20_000,
            connect_retries=1,
            connect_backoff_ms=250,
        )

        self.assertEqual(entry_payload["config"]["timeoutMs"], 60_000)
        self.assertEqual(entry_payload["config"]["smartExtraction"]["enabled"], False)
        self.assertEqual(entry_payload["config"]["smartExtraction"]["mode"], "disabled")
        self.assertEqual(entry_payload["config"]["reconcile"]["enabled"], False)

    def test_build_plugin_entry_enables_llm_assist_surface_for_profile_c_only_when_flags_are_true(self) -> None:
        disabled_entry = installer.build_plugin_entry(
            profile="c",
            transport="stdio",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            database_url="sqlite+aiosqlite:////tmp/demo.db",
            timeout_ms=20_000,
            connect_retries=1,
            connect_backoff_ms=250,
            env_values={
                "WRITE_GUARD_LLM_ENABLED": "false",
                "COMPACT_GIST_LLM_ENABLED": "false",
                "INTENT_LLM_ENABLED": "false",
            },
        )
        enabled_entry = installer.build_plugin_entry(
            profile="c",
            transport="stdio",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            database_url="sqlite+aiosqlite:////tmp/demo.db",
            timeout_ms=20_000,
            connect_retries=1,
            connect_backoff_ms=250,
            env_values={
                "WRITE_GUARD_LLM_ENABLED": "true",
                "COMPACT_GIST_LLM_ENABLED": "true",
                "INTENT_LLM_ENABLED": "true",
            },
        )

        self.assertEqual(disabled_entry["config"]["smartExtraction"]["enabled"], False)
        self.assertEqual(disabled_entry["config"]["smartExtraction"]["mode"], "disabled")
        self.assertEqual(disabled_entry["config"]["reconcile"]["enabled"], False)
        self.assertEqual(enabled_entry["config"]["smartExtraction"]["enabled"], True)
        self.assertEqual(enabled_entry["config"]["smartExtraction"]["mode"], "auto")
        self.assertEqual(enabled_entry["config"]["reconcile"]["enabled"], True)

    def test_build_plugin_entry_keeps_profile_d_default_advanced_surface(self) -> None:
        entry_payload = installer.build_plugin_entry(
            profile="d",
            transport="stdio",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            database_url="sqlite+aiosqlite:////tmp/demo.db",
            timeout_ms=20_000,
            connect_retries=1,
            connect_backoff_ms=250,
        )

        self.assertEqual(entry_payload["config"]["smartExtraction"]["enabled"], True)
        self.assertEqual(entry_payload["config"]["smartExtraction"]["mode"], "auto")
        self.assertEqual(entry_payload["config"]["reconcile"]["enabled"], True)

    def test_build_next_steps_renders_powershell_commands_for_windows(self) -> None:
        steps = installer.build_next_steps(
            config_path=Path("C:/Users/demo/.openclaw/openclaw.json"),
            transport="stdio",
            dry_run=True,
            host_platform="windows",
        )
        self.assertTrue(steps[0].startswith('$env:OPENCLAW_CONFIG_PATH='))
        self.assertIn("openclaw plugins inspect memory-palace --json", steps[0])
        self.assertEqual(steps[1], "py -3 scripts/openclaw_memory_palace_installer.py --dry-run --json")

    def test_merge_preserves_unrelated_fields(self) -> None:
        entry_payload = installer.build_plugin_entry(
            transport="sse",
            sse_url="http://127.0.0.1:8010/sse",
            api_key_env="MCP_API_KEY",
            database_url=None,
            timeout_ms=20_000,
            connect_retries=1,
            connect_backoff_ms=250,
        )
        payload = {
            "plugins": {
                "entries": {"other-plugin": {"enabled": True}},
                "slots": {"tools": "other-plugin"},
            },
            "other": {"keep": True},
        }
        merged, _ = installer.merge_openclaw_config(payload, entry_payload=entry_payload, activate=True)
        self.assertTrue(merged["other"]["keep"])
        self.assertEqual(merged["plugins"]["slots"]["tools"], "other-plugin")
        self.assertIn("other-plugin", merged["plugins"]["entries"])
        self.assertEqual(merged["plugins"]["entries"]["memory-core"]["enabled"], True)

    def test_collect_install_checks_warns_for_stdio_default_db_and_missing_backend_venv(self) -> None:
        with mock.patch.object(installer, "resolve_openclaw_binary", return_value=None), mock.patch.object(
            installer, "detect_openclaw_version", return_value=None
        ):
            checks = installer.collect_install_checks(
                config_path=Path("/tmp/openclaw.json"),
                config_path_source="explicit",
                transport="stdio",
                sse_url=None,
                api_key_env="MCP_API_KEY",
                database_url=None,
                plugin_path=Path("/repo/extensions/memory-palace"),
                backend_python_path=Path("/repo/backend/.venv/bin/python"),
                openclaw_bin="",
            )

        status_by_id = {item["id"]: item for item in checks}
        self.assertEqual(status_by_id["openclaw-bin"]["status"], "WARN")
        self.assertEqual(status_by_id["backend-venv"]["status"], "WARN")
        self.assertEqual(status_by_id["database-url"]["status"], "WARN")
        self.assertIn("user-state default database path", status_by_id["database-url"]["message"])

    def test_collect_install_checks_warns_for_missing_sse_api_key_env(self) -> None:
        with mock.patch.dict(installer.os.environ, {}, clear=False):
            installer.os.environ.pop("MCP_API_KEY", None)
            checks = installer.collect_install_checks(
                config_path=Path("/tmp/openclaw.json"),
                config_path_source="explicit",
                transport="sse",
                sse_url="http://127.0.0.1:8010/sse",
                api_key_env="MCP_API_KEY",
                database_url=None,
                plugin_path=Path("/repo/extensions/memory-palace"),
                backend_python_path=Path("/repo/backend/.venv/bin/python"),
                openclaw_bin="/usr/local/bin/openclaw",
            )

        status_by_id = {item["id"]: item for item in checks}
        self.assertEqual(status_by_id["sse-url"]["status"], "PASS")
        self.assertEqual(status_by_id["sse-api-key-env"]["status"], "WARN")
        self.assertIn("MCP_API_KEY", status_by_id["sse-api-key-env"]["message"])

    def test_collect_install_checks_warns_when_openclaw_version_is_below_hook_minimum(self) -> None:
        with mock.patch.object(
            installer,
            "detect_openclaw_version",
            return_value={
                "raw": "OpenClaw 2026.3.1",
                "parsed": (2026, 3, 1),
                "version": "2026.3.1",
                "meets_minimum": False,
            },
        ):
            checks = installer.collect_install_checks(
                config_path=Path("/tmp/openclaw.json"),
                config_path_source="explicit",
                transport="stdio",
                sse_url=None,
                api_key_env="MCP_API_KEY",
                database_url=None,
                plugin_path=Path("/repo/extensions/memory-palace"),
                backend_python_path=Path("/repo/backend/.venv/bin/python"),
                openclaw_bin="/usr/local/bin/openclaw",
            )

        status_by_id = {item["id"]: item for item in checks}
        self.assertEqual(status_by_id["openclaw-version"]["status"], "WARN")
        self.assertIn("2026.3.2", status_by_id["openclaw-version"]["message"])

    def test_collect_install_checks_includes_provider_probe_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime_env_file = Path(tmp_dir) / "runtime.env"
            probe_payload = installer.build_provider_probe_status(
                env_values={
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://embedding.example/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-large",
                },
                requested_profile="c",
                effective_profile="b",
                fallback_applied=True,
                profile_probe_failures=[{"component": "embedding", "detail": "HTTP 401"}],
                missing_profile_fields=[],
                probed_profile="c",
            )

            checks = installer.collect_install_checks(
                config_path=Path("/tmp/openclaw.json"),
                config_path_source="explicit",
                transport="stdio",
                sse_url=None,
                api_key_env="MCP_API_KEY",
                database_url="sqlite+aiosqlite:////tmp/bootstrap.db",
                plugin_path=Path("/repo/extensions/memory-palace"),
                backend_python_path=Path("/repo/backend/.venv/bin/python"),
                runtime_env_file=runtime_env_file,
                openclaw_bin="/usr/local/bin/openclaw",
                env_values={
                    "OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED": "c",
                    "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE": "b",
                },
                requested_profile="c",
                effective_profile="b",
                provider_probe=probe_payload,
            )

        status_by_id = {item["id"]: item for item in checks}
        self.assertEqual(status_by_id["provider-profile"]["status"], "WARN")
        self.assertIn("fell back", status_by_id["provider-profile"]["message"])
        self.assertEqual(status_by_id["provider-embedding"]["status"], "WARN")
        self.assertIn("HTTP 401", str(status_by_id["provider-embedding"]["details"]))

    def test_build_provider_probe_status_localizes_summary_and_missing_detail_for_zh(self) -> None:
        with mock.patch.object(installer, "cli_language", return_value="zh"):
            payload = installer.build_provider_probe_status(
                env_values={
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://embedding.example/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-large",
                },
                requested_profile="c",
                effective_profile="c",
                fallback_applied=False,
                profile_probe_failures=[],
                missing_profile_fields=["RETRIEVAL_RERANKER_API_BASE"],
                probed_profile="c",
            )

        self.assertIn("provider 字段仍未补齐", payload["summaryMessage"])
        self.assertEqual(payload["providers"]["embedding"]["detail"], "探测通过。")
        self.assertIn("缺失字段", payload["providers"]["reranker"]["detail"])

    def test_resolve_provider_probe_status_requires_real_probe_record_before_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            payload = installer.resolve_provider_probe_status(
                env_values={
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://embedding.example/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-large",
                    "RETRIEVAL_RERANKER_API_BASE": "https://reranker.example/v1",
                    "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                    "RETRIEVAL_RERANKER_MODEL": "rerank-large",
                },
                setup_root_path=Path(tmp_dir),
                requested_profile="c",
                effective_profile="c",
            )

        self.assertEqual(payload["summaryStatus"], "warn")
        self.assertEqual(payload["providers"]["embedding"]["status"], "not_checked")
        self.assertEqual(payload["providers"]["reranker"]["status"], "not_checked")
        self.assertIn("no successful probe", payload["providers"]["embedding"]["detail"].lower())

    def test_resolve_provider_probe_status_ignores_stale_persisted_signature(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            stale_env = {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://embedding-old.example/v1",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-large",
                "RETRIEVAL_RERANKER_API_BASE": "https://reranker-old.example/v1",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "rerank-large",
            }
            installer.write_json_file(
                installer.default_provider_probe_status_path(tmp_root),
                installer.build_provider_probe_status(
                    env_values=stale_env,
                    requested_profile="c",
                    effective_profile="c",
                    fallback_applied=False,
                    profile_probe_failures=[],
                    missing_profile_fields=[],
                    probe_recorded=True,
                ),
                dry_run=False,
            )

            payload = installer.resolve_provider_probe_status(
                env_values={
                    **stale_env,
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://embedding-new.example/v1",
                },
                setup_root_path=tmp_root,
                requested_profile="c",
                effective_profile="c",
            )

        self.assertEqual(payload["summaryStatus"], "warn")
        self.assertEqual(payload["providers"]["embedding"]["status"], "not_checked")
        self.assertEqual(payload["providers"]["embedding"]["baseUrl"], "https://embedding-new.example/v1")

    def test_build_install_guidance_prefers_source_checkout_after_public_package_probe_failure(self) -> None:
        payload = installer.build_install_guidance()

        self.assertEqual(payload["recommendedMethod"], "source-checkout")
        self.assertEqual(
            payload["installCommands"]["source-checkout"],
            "python3 scripts/openclaw_memory_palace.py setup --mode basic --profile b --transport stdio --json",
        )
        self.assertEqual(
            payload["installCommands"]["local-tgz"],
            "openclaw plugins install --dangerously-force-unsafe-install ./<generated-tgz>",
        )
        self.assertIn("Package not found on npm", payload["recommendedMethodNote"])
        self.assertIn("resolved to a skill", payload["recommendedMethodNote"])
        self.assertEqual(
            payload["installSteps"]["local-tgz"],
            [
                "openclaw plugins install --dangerously-force-unsafe-install ./<generated-tgz>",
                "npm exec --yes --package ./<generated-tgz> memory-palace-openclaw -- setup --mode basic --profile b --transport stdio --json",
                "openclaw memory-palace verify --json",
                "openclaw memory-palace doctor --json",
                "openclaw memory-palace smoke --json",
            ],
        )

    def test_apply_setup_defaults_localizes_fallback_warnings_for_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            installer,
            "cli_language",
            return_value="en",
        ):
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="c",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={},
            )

        self.assertEqual(effective_profile, "b")
        self.assertTrue(fallback_applied)
        self.assertTrue(env_values["MCP_API_KEY"])
        self.assertTrue(missing_fields)
        self.assertTrue(any("MCP_API_KEY was not provided" in item for item in warnings))
        self.assertTrue(any("fell back to Profile B" in item for item in warnings))
        self.assertTrue(any("Missing C/D fields:" in item for item in warnings))

    def test_apply_setup_defaults_passes_config_path_to_host_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.object(
            installer,
            "host_config_runtime_overrides",
            wraps=installer.host_config_runtime_overrides,
        ) as host_overrides:
            setup_root = Path(tmp_dir)
            config_path = setup_root / "openclaw.json"
            config_path.write_text("{}", encoding="utf-8")
            installer.apply_setup_defaults(
                profile="b",
                mode="basic",
                transport="stdio",
                config_path=config_path,
                setup_root_path=setup_root,
                existing_env={},
                host_platform="windows",
            )

        host_overrides.assert_called_once_with(config_path=config_path)

    def test_bootstrap_status_includes_install_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            config_path = tmp_root / "openclaw.json"
            env_file = tmp_root / "runtime.env"
            config_path.write_text("{}", encoding="utf-8")
            env_file.write_text(
                "\n".join(
                    [
                        "OPENCLAW_MEMORY_PALACE_TRANSPORT=stdio",
                        "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b",
                        "DATABASE_URL=sqlite+aiosqlite:////tmp/bootstrap.db",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                installer,
                "detect_config_path_with_source",
                return_value=(config_path, "explicit"),
            ), mock.patch.object(
                installer,
                "collect_install_checks",
                return_value=[{"id": "bundled-skill", "status": "PASS"}],
            ):
                payload = installer.bootstrap_status(
                    config=str(config_path),
                    setup_root_value=str(tmp_root),
                    env_file_value=str(env_file),
                )

        self.assertEqual(payload["checks"], [{"id": "bundled-skill", "status": "PASS"}])
        self.assertEqual(payload["installGuidance"]["recommendedMethod"], "source-checkout")

    def test_bootstrap_status_requires_onboarding_when_runtime_env_exists_but_memory_slot_is_not_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            config_path = tmp_root / "openclaw.json"
            env_file = tmp_root / "runtime.env"
            config_path.write_text(
                json.dumps(
                    {
                        "plugins": {
                            "allow": ["memory-palace", "memory-core"],
                            "load": {"paths": ["/tmp/plugin"]},
                            "slots": {"memory": "memory-core"},
                            "entries": {"memory-palace": {"enabled": True}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            env_file.write_text(
                "\n".join(
                    [
                        "OPENCLAW_MEMORY_PALACE_TRANSPORT=stdio",
                        "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b",
                        "DATABASE_URL=sqlite+aiosqlite:////tmp/bootstrap.db",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            payload = installer.bootstrap_status(
                config=str(config_path),
                setup_root_value=str(tmp_root),
                env_file_value=str(env_file),
            )

        self.assertTrue(payload["setup"]["requiresOnboarding"])
        self.assertIn("not fully wired", payload["summary"])

    def test_bootstrap_status_exposes_provider_probe_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            config_path = tmp_root / "openclaw.json"
            env_file = tmp_root / "runtime.env"
            config_path.write_text("{}", encoding="utf-8")
            env_file.write_text(
                "\n".join(
                    [
                        "OPENCLAW_MEMORY_PALACE_TRANSPORT=stdio",
                        "OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED=c",
                        "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b",
                        "DATABASE_URL=sqlite+aiosqlite:////tmp/bootstrap.db",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            installer.write_json_file(
                installer.default_provider_probe_status_path(tmp_root),
                {
                    "requestedProfile": "c",
                    "effectiveProfile": "b",
                    "probedProfile": "c",
                    "requiresProviders": True,
                    "fallbackApplied": True,
                    "summaryStatus": "warn",
                    "summaryMessage": "Requested Profile C fell back to Profile B after provider checks.",
                    "checkedAt": "2026-03-25T00:00:00Z",
                    "missingFields": [],
                    "providers": {
                        "embedding": {
                            "configured": True,
                            "status": "fail",
                            "detail": "HTTP 401",
                            "baseUrl": "https://embedding.example/v1",
                            "model": "embed-large",
                            "missingFields": [],
                            "detectedDim": "1024",
                        }
                    },
                },
                dry_run=False,
            )

            with mock.patch.object(
                installer,
                "detect_config_path_with_source",
                return_value=(config_path, "explicit"),
            ):
                payload = installer.bootstrap_status(
                    config=str(config_path),
                    setup_root_value=str(tmp_root),
                    env_file_value=str(env_file),
                )

        self.assertEqual(payload["setup"]["providerProbe"]["requestedProfile"], "c")
        self.assertTrue(payload["setup"]["providerProbe"]["fallbackApplied"])
        checks_by_id = {item["id"]: item for item in payload["checks"]}
        self.assertIn("provider-profile", checks_by_id)

    def test_preview_provider_probe_status_uses_current_overrides_and_can_persist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            config_path = tmp_root / "openclaw.json"
            env_file = tmp_root / "runtime.env"
            config_path.write_text("{}", encoding="utf-8")
            env_file.write_text(
                "\n".join(
                    [
                        "OPENCLAW_MEMORY_PALACE_TRANSPORT=stdio",
                        "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                installer,
                "detect_config_path_with_source",
                return_value=(config_path, "explicit"),
            ), mock.patch.object(
                installer,
                "probe_profile_model_connectivity",
                return_value=[],
            ) as probe_connectivity:
                payload = installer.preview_provider_probe_status(
                    config=str(config_path),
                    setup_root_value=str(tmp_root),
                    env_file_value=str(env_file),
                    profile="c",
                    mode="full",
                    transport="sse",
                    embedding_api_base="https://embedding.example/v1",
                    embedding_api_key="embed-secret",
                    embedding_model="embed-large",
                    embedding_dim="1024",
                    reranker_api_base="https://reranker.example/v1",
                    reranker_api_key="rerank-secret",
                    reranker_model="rerank-large",
                    llm_api_base="https://llm.example/v1",
                    llm_api_key="llm-secret",
                    llm_model="gpt-5.4-mini",
                    persist=True,
                )
                self.assertTrue(installer.default_provider_probe_status_path(tmp_root).is_file())

        self.assertEqual(payload["requestedProfile"], "c")
        self.assertEqual(payload["effectiveProfile"], "c")
        self.assertFalse(payload["fallbackApplied"])
        self.assertEqual(payload["providers"]["embedding"]["baseUrl"], "https://embedding.example/v1")
        self.assertEqual(payload["providers"]["embedding"]["detectedDim"], "1024")
        self.assertEqual(payload["providers"]["embedding"]["detectedMaxDim"], "1024")
        self.assertEqual(payload["providers"]["embedding"]["recommendedDim"], "1024")
        self.assertEqual(payload["providers"]["reranker"]["status"], "pass")
        self.assertEqual(payload["providers"]["llm"]["status"], "pass")
        probe_connectivity.assert_called_once()

    def test_preview_provider_probe_status_reads_current_process_provider_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            config_path = tmp_root / "openclaw.json"
            env_file = tmp_root / "runtime.env"
            config_path.write_text("{}", encoding="utf-8")
            env_file.write_text("", encoding="utf-8")

            env_overrides = {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://embedding.example/v1",
                "RETRIEVAL_EMBEDDING_API_KEY": "embed-secret",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-large",
                "RETRIEVAL_EMBEDDING_DIM": "1024",
                "RETRIEVAL_RERANKER_API_BASE": "https://reranker.example/v1",
                "RETRIEVAL_RERANKER_API_KEY": "rerank-secret",
                "RETRIEVAL_RERANKER_MODEL": "rerank-large",
                "WRITE_GUARD_LLM_API_BASE": "https://llm.example/v1/chat/completions",
                "WRITE_GUARD_LLM_API_KEY": "llm-secret",
                "WRITE_GUARD_LLM_MODEL": "gpt-5.4-mini",
                "COMPACT_GIST_LLM_API_BASE": "https://llm.example/v1/chat/completions",
                "COMPACT_GIST_LLM_API_KEY": "llm-secret",
                "COMPACT_GIST_LLM_MODEL": "gpt-5.4-mini",
            }

            with mock.patch.dict(os.environ, env_overrides, clear=False), mock.patch.object(
                installer,
                "detect_config_path_with_source",
                return_value=(config_path, "explicit"),
            ), mock.patch.object(
                installer,
                "probe_profile_model_connectivity",
                return_value=[],
            ), mock.patch.object(
                installer,
                "probe_embedding_dimension_recommendation_with_retries",
                return_value=(1024, ""),
            ):
                payload = installer.preview_provider_probe_status(
                    config=str(config_path),
                    setup_root_value=str(tmp_root),
                    env_file_value=str(env_file),
                    profile="d",
                    mode="basic",
                    transport="stdio",
                )

        self.assertEqual(payload["summaryStatus"], "pass")
        self.assertEqual(payload["missingFields"], [])
        self.assertEqual(payload["providers"]["embedding"]["baseUrl"], "https://embedding.example/v1")
        self.assertEqual(payload["providers"]["reranker"]["baseUrl"], "https://reranker.example/v1")
        self.assertEqual(payload["providers"]["llm"]["model"], "gpt-5.4-mini")

    def test_preview_provider_probe_status_does_not_adopt_host_llm_hints_for_profile_c_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            config_path = tmp_root / "openclaw.json"
            env_file = tmp_root / "runtime.env"
            config_path.write_text("{}", encoding="utf-8")
            env_file.write_text("", encoding="utf-8")
            captured_probe_env: dict[str, str] = {}

            def _capture_probe_env(env_values: Mapping[str, str], *, profile: str, timeout_seconds: float = 8.0):
                _ = timeout_seconds
                captured_probe_env.update(dict(env_values))
                self.assertEqual(profile, "c")
                return []

            with mock.patch.object(
                installer,
                "detect_config_path_with_source",
                return_value=(config_path, "explicit"),
            ), mock.patch.object(
                installer,
                "host_config_runtime_overrides",
                return_value={
                    "embedding_api_base": None,
                    "embedding_api_key": None,
                    "embedding_model": None,
                    "embedding_dim": None,
                    "reranker_api_base": None,
                    "reranker_api_key": None,
                    "reranker_model": None,
                    "llm_api_base": "https://host-llm.example/v1",
                    "llm_api_key": "host-llm-key",
                    "llm_model": "host-gpt-5.4-mini",
                },
            ), mock.patch.object(
                installer,
                "probe_profile_model_connectivity",
                side_effect=_capture_probe_env,
            ), mock.patch.object(
                installer,
                "probe_embedding_dimension_recommendation_with_retries",
                return_value=(1024, ""),
            ):
                payload = installer.preview_provider_probe_status(
                    config=str(config_path),
                    setup_root_value=str(tmp_root),
                    env_file_value=str(env_file),
                    profile="c",
                    mode="basic",
                    transport="stdio",
                    embedding_api_base="https://embedding.example/v1",
                    embedding_api_key="embed-secret",
                    embedding_model="embed-large",
                    embedding_dim="1024",
                    reranker_api_base="https://reranker.example/v1",
                    reranker_api_key="rerank-secret",
                    reranker_model="rerank-large",
                )

        self.assertEqual(payload["summaryStatus"], "pass")
        self.assertEqual(payload["providers"]["llm"]["status"], "not_required")
        self.assertIsNone(payload["providers"]["llm"]["baseUrl"])
        self.assertIsNone(payload["providers"]["llm"]["model"])
        self.assertEqual(captured_probe_env.get("WRITE_GUARD_LLM_ENABLED"), "false")
        self.assertEqual(captured_probe_env.get("COMPACT_GIST_LLM_ENABLED"), "false")
        self.assertEqual(captured_probe_env.get("INTENT_LLM_ENABLED"), "false")

    def test_build_install_report_includes_product_summary_and_next_steps(self) -> None:
        report = installer.build_install_report(
            config_path=Path("/tmp/openclaw.json"),
            config_path_source="explicit",
            transport="stdio",
            activate_slot=True,
            dry_run=False,
            actions=["ensured plugins.allow contains memory-palace"],
            merged_payload={"plugins": {}},
            sse_url=None,
            api_key_env="MCP_API_KEY",
            database_url="sqlite+aiosqlite:////tmp/demo.db",
        )

        self.assertIn("Installer completed", report["summary"])
        self.assertTrue(report["next_steps"])
        self.assertEqual(report["config_path_source"], "explicit")
        self.assertEqual(report["config_preview"], {"plugins": {"allow": None, "load": {"paths": None}, "slots": {"memory": None}, "entries": {"memory-palace": None}}})

    def test_required_profile_fields_treat_placeholder_values_as_missing(self) -> None:
        missing_fields = installer.required_profile_fields(
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "http://127.0.0.1:PORT/v1",
                "RETRIEVAL_EMBEDDING_API_KEY": "replace-with-your-key",
                "RETRIEVAL_EMBEDDING_MODEL": "Qwen/Qwen3-Embedding-8B",
                "RETRIEVAL_RERANKER_API_BASE": "https://<your-router-host>/v1",
                "RETRIEVAL_RERANKER_API_KEY": "replace-with-your-key",
                "RETRIEVAL_RERANKER_MODEL": "Qwen/Qwen3-Reranker-8B",
                "WRITE_GUARD_LLM_API_BASE": "https://<your-router-host>/v1",
                "WRITE_GUARD_LLM_API_KEY": "replace-with-your-key",
                "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
            },
            "c",
        )

        self.assertEqual(
            missing_fields,
            [
                "RETRIEVAL_EMBEDDING_API_BASE",
                "RETRIEVAL_EMBEDDING_API_KEY",
                "RETRIEVAL_RERANKER_API_BASE",
                "RETRIEVAL_RERANKER_API_KEY",
            ],
        )

    def test_required_profile_fields_only_require_llm_for_profile_d(self) -> None:
        profile_c_missing = installer.required_profile_fields(
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://embedding.example/v1",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-large",
                "RETRIEVAL_RERANKER_API_BASE": "https://reranker.example/v1",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "rerank-large",
            },
            "c",
        )
        profile_d_missing = installer.required_profile_fields(
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://embedding.example/v1",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-large",
                "RETRIEVAL_RERANKER_API_BASE": "https://reranker.example/v1",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "rerank-large",
            },
            "d",
        )

        self.assertEqual(profile_c_missing, [])
        self.assertEqual(
            profile_d_missing,
            [
                "WRITE_GUARD_LLM_API_BASE",
                "WRITE_GUARD_LLM_API_KEY",
                "WRITE_GUARD_LLM_MODEL",
            ],
        )

    def test_apply_setup_defaults_falls_back_to_profile_b_when_cd_models_are_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="c",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={},
                host_platform="windows",
            )

        self.assertEqual(effective_profile, "b")
        self.assertTrue(fallback_applied)
        self.assertEqual(env_values["RETRIEVAL_EMBEDDING_BACKEND"], "hash")
        self.assertIn("RETRIEVAL_EMBEDDING_API_BASE", missing_fields)
        self.assertIn("RETRIEVAL_RERANKER_API_BASE", missing_fields)
        self.assertTrue(
            any(
                installer._localized_onboarding_text("缺失的 C/D 字段", "Missing C/D fields")
                in item
                for item in warnings
            )
        )

    def test_apply_setup_defaults_falls_back_to_profile_b_when_cd_models_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="c",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={},
            )

        self.assertEqual(effective_profile, "b")
        self.assertTrue(fallback_applied)
        self.assertEqual(env_values["RETRIEVAL_EMBEDDING_BACKEND"], "hash")
        self.assertEqual(env_values["RETRIEVAL_RERANKER_ENABLED"], "false")
        self.assertTrue(env_values["MCP_API_KEY"])
        self.assertTrue(missing_fields)
        self.assertTrue(
            any(
                installer._localized_onboarding_text("回退到 Profile B", "fell back to Profile B")
                in item
                for item in warnings
            )
        )

    def test_apply_setup_defaults_rejects_remote_sse_without_explicit_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(ValueError, "Remote/shared SSE setup requires an explicit MCP_API_KEY"):
                installer.apply_setup_defaults(
                    profile="b",
                    mode="basic",
                    transport="sse",
                    setup_root_path=Path(tmp_dir),
                    existing_env={},
                    sse_url="https://memory.example.com/sse",
                )

    def test_apply_setup_defaults_allows_remote_sse_generation_only_with_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="b",
                mode="basic",
                transport="sse",
                setup_root_path=Path(tmp_dir),
                existing_env={},
                sse_url="https://memory.example.com/sse",
                allow_generate_remote_api_key=True,
            )

        self.assertEqual(effective_profile, "b")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertTrue(env_values["MCP_API_KEY"])
        self.assertTrue(
            any(
                installer._localized_onboarding_text("显式确认生成远程场景", "remote scenario after explicit confirmation")
                in item
                for item in warnings
            )
        )

    def test_apply_setup_defaults_prefers_explicit_mcp_api_key_over_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_values, _effective_profile, _warnings, _fallback_applied, _missing_fields = installer.apply_setup_defaults(
                profile="b",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={"MCP_API_KEY": "existing-key"},
                mcp_api_key="explicit-key",
            )

        self.assertEqual(env_values["MCP_API_KEY"], "explicit-key")

    def test_apply_setup_defaults_preserves_existing_database_url_for_same_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_values, effective_profile, _warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="b",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={
                    "DATABASE_URL": "sqlite+aiosqlite:////custom/location/keep-me.db",
                    installer._metadata_key("PROFILE_REQUESTED"): "b",
                },
            )

        self.assertEqual(effective_profile, "b")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertEqual(env_values["DATABASE_URL"], "sqlite+aiosqlite:////custom/location/keep-me.db")

    def test_apply_setup_defaults_preserves_existing_tuning_for_same_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_values, effective_profile, _warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="b",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={
                    installer._metadata_key("PROFILE_REQUESTED"): "b",
                    "RUNTIME_WRITE_BUSY_TIMEOUT_MS": "9999",
                    "RUNTIME_WRITE_WAL_ENABLED": "true",
                    "RUNTIME_AUTO_FLUSH_ENABLED": "false",
                },
            )

        self.assertEqual(effective_profile, "b")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertEqual(env_values["RUNTIME_WRITE_BUSY_TIMEOUT_MS"], "9999")
        self.assertEqual(env_values["RUNTIME_WRITE_WAL_ENABLED"], "true")
        self.assertEqual(env_values["RUNTIME_AUTO_FLUSH_ENABLED"], "false")

    def test_apply_setup_defaults_uses_current_process_model_env_for_profile_c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_EMBEDDING_DIM": "1024",
                "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                "WRITE_GUARD_LLM_API_BASE": "https://llm.local/v1",
                "WRITE_GUARD_LLM_API_KEY": "llm-key",
                "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
            },
            clear=False,
        ):
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="c",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={},
            )

        self.assertEqual(effective_profile, "c")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertEqual(env_values["RETRIEVAL_EMBEDDING_API_BASE"], "https://router.local/v1")
        self.assertEqual(env_values["RETRIEVAL_RERANKER_API_BASE"], "https://router.local/v1")
        self.assertEqual(env_values["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")
        self.assertFalse(
            any(
                installer._localized_onboarding_text("回退到 Profile B", "fell back to Profile B")
                in item
                for item in warnings
            )
        )

    def test_apply_setup_defaults_keeps_optional_llm_settings_for_profile_b(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                "OPENAI_BASE_URL": "https://llm.local/v1",
                "OPENAI_API_KEY": "llm-key",
                "OPENAI_MODEL": "gpt-5.4",
            },
            clear=False,
        ):
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="b",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={},
            )

        self.assertEqual(effective_profile, "b")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertEqual(env_values["RETRIEVAL_EMBEDDING_BACKEND"], "hash")
        self.assertEqual(env_values["RETRIEVAL_EMBEDDING_MODEL"], "hash-v1")
        self.assertEqual(env_values["RETRIEVAL_EMBEDDING_DIM"], "64")
        self.assertEqual(env_values["RETRIEVAL_RERANKER_ENABLED"], "false")
        for key in (
            "RETRIEVAL_EMBEDDING_API_BASE",
            "RETRIEVAL_EMBEDDING_API_KEY",
            "RETRIEVAL_RERANKER_API_BASE",
            "RETRIEVAL_RERANKER_API_KEY",
        ):
            self.assertNotIn(key, env_values)
        self.assertEqual(env_values["OPENAI_API_KEY"], "llm-key")
        self.assertEqual(env_values["WRITE_GUARD_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["WRITE_GUARD_LLM_API_KEY"], "llm-key")
        self.assertEqual(env_values["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")
        self.assertEqual(env_values["WRITE_GUARD_LLM_ENABLED"], "true")
        self.assertEqual(env_values["COMPACT_GIST_LLM_ENABLED"], "true")
        self.assertEqual(env_values["INTENT_LLM_ENABLED"], "true")
        self.assertFalse(
            any(
                installer._localized_onboarding_text("回退到 Profile B", "fell back to Profile B")
                in item
                for item in warnings
            )
        )

    def test_apply_setup_defaults_accepts_intent_llm_aliases_for_profile_c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                "INTENT_LLM_API_BASE": "https://llm.local/v1",
                "INTENT_LLM_API_KEY": "llm-key",
                "INTENT_LLM_MODEL": "gpt-5.4",
            },
            clear=False,
        ):
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="c",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={},
            )

        self.assertEqual(effective_profile, "c")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertEqual(env_values["WRITE_GUARD_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["WRITE_GUARD_LLM_API_KEY"], "llm-key")
        self.assertEqual(env_values["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")
        self.assertEqual(env_values["INTENT_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["INTENT_LLM_API_KEY"], "llm-key")
        self.assertEqual(env_values["INTENT_LLM_MODEL"], "gpt-5.4")
        self.assertEqual(env_values["INTENT_LLM_ENABLED"], "true")

    def test_apply_setup_defaults_normalizes_responses_alias_for_profile_c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1/rerank",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                "LLM_RESPONSES_URL": "https://llm.local/v1/responses",
                "INTENT_LLM_API_KEY": "llm-key",
                "INTENT_LLM_MODEL": "gpt-5.4",
            },
            clear=False,
        ):
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="c",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={},
            )

        self.assertEqual(effective_profile, "c")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertEqual(env_values["WRITE_GUARD_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["COMPACT_GIST_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["COMPACT_GIST_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["COMPACT_GIST_LLM_MODEL"], "gpt-5.4")
        self.assertEqual(env_values["INTENT_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["INTENT_LLM_MODEL"], "gpt-5.4")
        self.assertEqual(env_values["INTENT_LLM_ENABLED"], "true")
        self.assertFalse(
            any(
                installer._localized_onboarding_text("回退到 Profile B", "fell back to Profile B")
                in item
                for item in warnings
            )
        )

    def test_apply_setup_defaults_profile_d_enables_full_llm_suite_from_write_guard_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                "WRITE_GUARD_LLM_API_BASE": "https://llm.local/v1",
                "WRITE_GUARD_LLM_API_KEY": "llm-key",
                "WRITE_GUARD_LLM_MODEL": "gpt-5.4-mini",
            },
            clear=False,
        ):
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="d",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={},
            )

        self.assertEqual(effective_profile, "d")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertEqual(env_values["WRITE_GUARD_LLM_ENABLED"], "true")
        self.assertEqual(env_values["COMPACT_GIST_LLM_ENABLED"], "true")
        self.assertEqual(env_values["INTENT_LLM_ENABLED"], "true")
        self.assertEqual(env_values["INTENT_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["INTENT_LLM_API_KEY"], "llm-key")
        self.assertEqual(env_values["INTENT_LLM_MODEL"], "gpt-5.4-mini")

    def test_apply_setup_defaults_keeps_optional_llm_settings_after_fallback_to_profile_b(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "OPENAI_BASE_URL": "https://llm.local/v1",
                "OPENAI_API_KEY": "llm-key",
                "OPENAI_MODEL": "gpt-5.4",
            },
            clear=False,
        ):
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="c",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={},
            )

        self.assertEqual(effective_profile, "b")
        self.assertTrue(fallback_applied)
        self.assertTrue(missing_fields)
        self.assertEqual(env_values["RETRIEVAL_EMBEDDING_BACKEND"], "hash")
        self.assertEqual(env_values["RETRIEVAL_RERANKER_ENABLED"], "false")
        for key in (
            "RETRIEVAL_EMBEDDING_API_BASE",
            "RETRIEVAL_EMBEDDING_API_KEY",
            "RETRIEVAL_RERANKER_API_BASE",
            "RETRIEVAL_RERANKER_API_KEY",
        ):
            self.assertNotIn(key, env_values)
        self.assertEqual(env_values["OPENAI_API_KEY"], "llm-key")
        self.assertEqual(env_values["WRITE_GUARD_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["WRITE_GUARD_LLM_API_KEY"], "llm-key")
        self.assertEqual(env_values["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")
        self.assertEqual(env_values["WRITE_GUARD_LLM_ENABLED"], "true")
        self.assertEqual(env_values["COMPACT_GIST_LLM_ENABLED"], "true")
        self.assertTrue(
            any(
                installer._localized_onboarding_text("回退到 Profile B", "fell back to Profile B")
                in item
                for item in warnings
            )
        )

    def test_apply_setup_defaults_uses_existing_env_model_values_for_profile_d(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="d",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                    "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                    "RETRIEVAL_EMBEDDING_DIM": "1024",
                    "RETRIEVAL_RERANKER_API_BASE": "https://rerank.local/v1/rerank",
                    "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                    "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                    "WRITE_GUARD_LLM_API_BASE": "https://llm.local/v1",
                    "WRITE_GUARD_LLM_API_KEY": "llm-key",
                    "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                },
                strict_profile=True,
            )

        self.assertEqual(effective_profile, "d")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertEqual(env_values["RETRIEVAL_EMBEDDING_API_BASE"], "https://router.local/v1")
        self.assertEqual(env_values["RETRIEVAL_RERANKER_API_BASE"], "https://rerank.local/v1/rerank")
        self.assertEqual(env_values["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")
        self.assertFalse(
            any(
                installer._localized_onboarding_text("回退到 Profile B", "fell back to Profile B")
                in item
                for item in warnings
            )
        )

    def test_apply_setup_defaults_profile_d_shared_llm_overrides_existing_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(installer.os.environ, {}, clear=True):
            env_values, effective_profile, warnings, fallback_applied, missing_fields = installer.apply_setup_defaults(
                profile="d",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
                existing_env={
                    "WRITE_GUARD_LLM_API_BASE": "",
                    "WRITE_GUARD_LLM_API_KEY": "",
                    "WRITE_GUARD_LLM_MODEL": "replace-with-your-llm-model",
                    "COMPACT_GIST_LLM_API_BASE": "",
                    "COMPACT_GIST_LLM_API_KEY": "",
                    "COMPACT_GIST_LLM_MODEL": "replace-with-your-llm-model",
                    "INTENT_LLM_API_BASE": "",
                    "INTENT_LLM_API_KEY": "",
                    "INTENT_LLM_MODEL": "replace-with-your-llm-model",
                },
                embedding_api_base="https://router.local/v1/embeddings",
                embedding_api_key="embedding-key",
                embedding_model="embed-model",
                embedding_dim="1024",
                reranker_api_base="https://rerank.local/v1/rerank",
                reranker_api_key="reranker-key",
                reranker_model="reranker-model",
                llm_api_base="https://llm.local/v1",
                llm_api_key="llm-key",
                llm_model="gpt-5.4-mini",
                strict_profile=True,
            )

        self.assertEqual(effective_profile, "d")
        self.assertFalse(fallback_applied)
        self.assertEqual(missing_fields, [])
        self.assertEqual(env_values["WRITE_GUARD_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["WRITE_GUARD_LLM_API_KEY"], "llm-key")
        self.assertEqual(env_values["WRITE_GUARD_LLM_MODEL"], "gpt-5.4-mini")
        self.assertEqual(env_values["COMPACT_GIST_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["COMPACT_GIST_LLM_MODEL"], "gpt-5.4-mini")
        self.assertEqual(env_values["INTENT_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(env_values["INTENT_LLM_MODEL"], "gpt-5.4-mini")
        self.assertEqual(env_values["WRITE_GUARD_LLM_ENABLED"], "true")
        self.assertEqual(env_values["COMPACT_GIST_LLM_ENABLED"], "true")
        self.assertEqual(env_values["INTENT_LLM_ENABLED"], "true")
        self.assertFalse(
            any(
                installer._localized_onboarding_text("回退到 Profile B", "fell back to Profile B")
                in item
                for item in warnings
            )
        )

    def test_finalize_profile_env_sets_default_embedding_dim_for_profile_c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            payload, _warnings, _fallback = installer.finalize_profile_env(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                    "RETRIEVAL_RERANKER_API_BASE": "https://rerank.local/v1",
                    "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                    "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                    "WRITE_GUARD_LLM_API_BASE": "https://llm.local/v1",
                    "WRITE_GUARD_LLM_API_KEY": "llm-key",
                    "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                },
                effective_profile="c",
                requested_profile="c",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
            )

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "1024")

    def test_finalize_profile_env_keeps_explicit_embedding_dim_for_profile_d(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            payload, _warnings, _fallback = installer.finalize_profile_env(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                    "RETRIEVAL_EMBEDDING_DIM": "2048",
                    "RETRIEVAL_RERANKER_API_BASE": "https://rerank.local/v1",
                    "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                    "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                    "WRITE_GUARD_LLM_API_BASE": "https://llm.local/v1",
                    "WRITE_GUARD_LLM_API_KEY": "llm-key",
                    "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                },
                effective_profile="d",
                requested_profile="d",
                mode="basic",
                transport="stdio",
                setup_root_path=Path(tmp_dir),
            )

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "2048")

    def test_apply_setup_defaults_strict_profile_rejects_placeholder_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(ValueError, "Profile C requires:"):
                installer.apply_setup_defaults(
                    profile="c",
                    mode="basic",
                    transport="stdio",
                    setup_root_path=Path(tmp_dir),
                    existing_env={
                        "RETRIEVAL_EMBEDDING_API_BASE": "https://<your-router-host>/v1",
                        "RETRIEVAL_EMBEDDING_API_KEY": "replace-with-your-key",
                        "RETRIEVAL_EMBEDDING_MODEL": "Qwen3-Embedding-8B",
                        "RETRIEVAL_RERANKER_API_BASE": "https://<your-router-host>/v1",
                        "RETRIEVAL_RERANKER_API_KEY": "replace-with-your-key",
                        "RETRIEVAL_RERANKER_MODEL": "Qwen3-Reranker-8B",
                        "WRITE_GUARD_LLM_API_BASE": "https://<your-router-host>/v1",
                        "WRITE_GUARD_LLM_API_KEY": "replace-with-your-key",
                        "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                    },
                    strict_profile=True,
                )

    def test_cli_text_uses_zh_locale_when_lang_requests_chinese(self) -> None:
        with mock.patch.dict(installer.os.environ, {"LANG": "zh_CN.UTF-8"}, clear=False):
            message = installer.cli_text("profile_env_prompt_fallback")
        self.assertIn("未提供 env 文件", message)

    def test_perform_setup_prompts_for_profile_env_and_keeps_profile_c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            profile_env_path = Path(tmp_dir) / "profile-c.env"
            profile_env_path.write_text(
                "\n".join(
                    [
                        "RETRIEVAL_EMBEDDING_API_BASE=https://router.local/v1/embeddings",
                        "RETRIEVAL_EMBEDDING_API_KEY=embedding-key",
                        "RETRIEVAL_EMBEDDING_MODEL=embed-model",
                        "RETRIEVAL_RERANKER_API_BASE=https://router.local/v1/rerank",
                        "RETRIEVAL_RERANKER_API_KEY=reranker-key",
                        "RETRIEVAL_RERANKER_MODEL=reranker-model",
                        "WRITE_GUARD_LLM_API_BASE=https://llm.local/v1",
                        "WRITE_GUARD_LLM_API_KEY=llm-key",
                        "WRITE_GUARD_LLM_MODEL=gpt-5.4",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=True), mock.patch(
                "builtins.input",
                side_effect=["1", str(profile_env_path)],
            ), mock.patch("builtins.print"), mock.patch.object(
                installer, "probe_profile_model_connectivity", return_value=[]
            ), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "read_json_file", return_value={}
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    profile="c",
                    mode="basic",
                    transport="stdio",
                )

        self.assertEqual(report["requested_profile"], "c")
        self.assertEqual(report["effective_profile"], "c")
        self.assertFalse(report["fallback_applied"])
        self.assertEqual(report["profile_missing_fields"], [])
        self.assertTrue(any("profile env" in action for action in report["actions"]))

    def test_perform_setup_strict_profile_still_errors_when_prompt_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=True), mock.patch(
                "builtins.input",
                return_value="",
            ), mock.patch("builtins.print"):
                with self.assertRaisesRegex(ValueError, "Profile C requires:"):
                    installer.perform_setup(
                        config=str(config_path),
                        setup_root_value=str(setup_root),
                        profile="c",
                        mode="basic",
                        transport="stdio",
                        strict_profile=True,
                    )

    def test_perform_setup_explicit_env_file_keeps_profile_d_under_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
            env_file_path = Path(tmp_dir) / "profile-d.env"
            env_file_path.write_text(
                "\n".join(
                    [
                        "RETRIEVAL_EMBEDDING_API_BASE=https://router.local/v1/embeddings",
                        "RETRIEVAL_EMBEDDING_API_KEY=embedding-key",
                        "RETRIEVAL_EMBEDDING_MODEL=embed-model",
                        "RETRIEVAL_EMBEDDING_DIM=1024",
                        "RETRIEVAL_RERANKER_API_BASE=https://router.local/v1/rerank",
                        "RETRIEVAL_RERANKER_API_KEY=reranker-key",
                        "RETRIEVAL_RERANKER_MODEL=reranker-model",
                        "WRITE_GUARD_LLM_API_BASE=https://llm.local/v1",
                        "WRITE_GUARD_LLM_API_KEY=llm-key",
                        "WRITE_GUARD_LLM_MODEL=gpt-5.4",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=False), mock.patch.object(
                installer, "probe_profile_model_connectivity", return_value=[]
            ), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "read_json_file", return_value={}
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    env_file_value=str(env_file_path),
                    profile="d",
                    mode="basic",
                    transport="stdio",
                    strict_profile=True,
                )

        self.assertEqual(report["requested_profile"], "d")
        self.assertEqual(report["effective_profile"], "d")
        self.assertFalse(report["fallback_applied"])
        self.assertEqual(report["profile_missing_fields"], [])

    def test_perform_setup_manual_prompt_accepts_model_overrides_for_profile_c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
            manual_inputs = iter(
                [
                    "2",
                    "https://router.local/v1/embeddings",
                    "embed-model-custom",
                    "https://router.local/v1/rerank",
                    "reranker-model-custom",
                    "y",
                    "https://llm.local/v1",
                    "gpt-5.5-custom",
                ]
            )

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=True), mock.patch(
                "builtins.input",
                side_effect=lambda _prompt="": next(manual_inputs),
            ), mock.patch.object(
                installer.getpass,
                "getpass",
                side_effect=["embedding-key", "reranker-key", "llm-key"],
            ), mock.patch("builtins.print"), mock.patch.object(
                installer, "probe_profile_model_connectivity", return_value=[]
            ), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "read_json_file", return_value={}
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    profile="c",
                    mode="basic",
                    transport="stdio",
                )

        self.assertEqual(report["effective_profile"], "c")
        self.assertFalse(report["fallback_applied"])
        self.assertIn("captured manual profile fields", " ".join(report["actions"]))
        self.assertIn("captured shared LLM settings", " ".join(report["actions"]))

    def test_perform_setup_persists_config_path_metadata_in_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text("{}", encoding="utf-8")
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=False), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "read_json_file", return_value={}
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "detect_reindex_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ), mock.patch.object(
                installer, "detect_openclaw_version", return_value=None
            ):
                installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    env_file_value=str(env_file),
                    profile="b",
                    mode="basic",
                    transport="stdio",
                )

            env_values = installer.load_env_file(env_file)

        self.assertEqual(
            env_values["OPENCLAW_MEMORY_PALACE_CONFIG_PATH"],
            str(config_path.resolve()),
        )

    def test_perform_setup_records_config_backup_when_config_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(json.dumps({"plugins": {}}), encoding="utf-8")
            setup_root = Path(tmp_dir) / "runtime"
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=False), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    profile="b",
                    mode="basic",
                    transport="stdio",
                )

        self.assertTrue(str(report["config_backup_path"]).endswith(".bak"))
        self.assertTrue(any("backed up existing config" in item for item in report["actions"]))

    def test_perform_setup_surfaces_reindex_follow_up_when_retrieval_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text(
                "\n".join(
                    [
                        "RETRIEVAL_EMBEDDING_BACKEND=api",
                        "RETRIEVAL_EMBEDDING_MODEL=embed-model",
                        "RETRIEVAL_EMBEDDING_API_BASE=https://embedding.example/v1",
                        "RETRIEVAL_EMBEDDING_DIM=1024",
                        "RETRIEVAL_RERANKER_ENABLED=true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=False), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    profile="b",
                    mode="basic",
                    transport="stdio",
                )

        self.assertEqual(report["effective_profile"], "b")
        self.assertTrue(report["reindex_required"])
        self.assertIn("RETRIEVAL_EMBEDDING_DIM", report["reindex_reason_keys"])
        self.assertIn("openclaw memory-palace index --wait --json", report["next_steps"])
        self.assertTrue(any("重建索引" in item for item in report["warnings"]))

    def test_perform_setup_falls_back_to_b_when_profile_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1/rerank",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                "WRITE_GUARD_LLM_API_BASE": "https://llm.local/v1",
                "WRITE_GUARD_LLM_API_KEY": "llm-key",
                "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
            },
            clear=False,
        ):
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=True), mock.patch.object(
                installer,
                "probe_profile_model_connectivity",
                return_value=[{"component": "embedding", "detail": "HTTP 401"}],
            ), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "read_json_file", return_value={}
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    profile="c",
                    mode="basic",
                    transport="stdio",
                )

        self.assertEqual(report["requested_profile"], "c")
        self.assertEqual(report["effective_profile"], "b")
        self.assertTrue(report["fallback_applied"])
        self.assertEqual(report["profile_probe_failures"], [{"component": "embedding", "detail": "HTTP 401"}])
        self.assertTrue(any("embedding" in item for item in report["warnings"]))

    def test_perform_setup_keeps_profile_c_when_only_optional_llm_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1/rerank",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                "WRITE_GUARD_LLM_API_BASE": "https://llm.local/v1",
                "WRITE_GUARD_LLM_API_KEY": "llm-key",
                "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
            },
            clear=False,
        ):
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=False), mock.patch.object(
                installer,
                "probe_profile_model_connectivity",
                return_value=[{"component": "llm", "detail": "HTTP 500"}],
            ), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "read_json_file", return_value={}
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    profile="c",
                    mode="basic",
                    transport="stdio",
                )

        self.assertEqual(report["requested_profile"], "c")
        self.assertEqual(report["effective_profile"], "c")
        self.assertFalse(report["fallback_applied"])
        self.assertEqual(report["profile_probe_failures"], [])
        self.assertTrue(any("optional LLM" in item or "可选 LLM" in item for item in report["warnings"]))

    def test_perform_setup_fallback_to_b_keeps_optional_llm_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1/rerank",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "reranker-model",
            },
            clear=False,
        ):
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
            captured_env: dict[str, str] = {}

            def _capture_checks(*args, **kwargs):
                captured_env.clear()
                captured_env.update(kwargs.get("env_values") or {})
                return []

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=True), mock.patch.object(
                installer,
                "probe_profile_model_connectivity",
                return_value=[{"component": "embedding", "detail": "HTTP 401"}],
            ), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "read_json_file", return_value={}
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "write_env_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", side_effect=_capture_checks
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ), mock.patch.object(
                installer,
                "prompt_for_profile_c_optional_llm_choice",
                return_value=True,
            ), mock.patch.object(
                installer,
                "prompt_for_shared_llm_values",
                return_value=(
                    {
                        "LLM_API_BASE": "https://llm.local/v1",
                        "LLM_API_KEY": "llm-key",
                        "LLM_MODEL_NAME": "gpt-5.4-mini",
                    },
                    ["captured shared llm"],
                ),
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    profile="c",
                    mode="basic",
                    transport="stdio",
                )

        self.assertEqual(report["effective_profile"], "b")
        self.assertEqual(captured_env["WRITE_GUARD_LLM_ENABLED"], "true")
        self.assertEqual(captured_env["COMPACT_GIST_LLM_ENABLED"], "true")
        self.assertEqual(captured_env["INTENT_LLM_ENABLED"], "true")
        self.assertEqual(captured_env["LLM_MODEL_NAME"], "gpt-5.4-mini")

    def test_perform_setup_does_not_persist_env_before_plugin_root_resolves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(
                installer, "probe_profile_model_connectivity", return_value=[]
            ), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", side_effect=RuntimeError("plugin root failed")
            ), mock.patch.object(
                installer, "write_env_file"
            ) as write_env_file, mock.patch.object(
                installer, "persist_provider_probe_status"
            ) as persist_provider_probe_status:
                with self.assertRaisesRegex(RuntimeError, "plugin root failed"):
                    installer.perform_setup(
                        config=str(config_path),
                        setup_root_value=str(setup_root),
                        profile="b",
                        mode="basic",
                        transport="stdio",
                    )

            write_env_file.assert_not_called()
            persist_provider_probe_status.assert_not_called()

    def test_probe_profile_model_connectivity_honors_env_timeout_override(self) -> None:
        calls: list[float] = []

        def fake_probe(**kwargs):
            calls.append(kwargs["timeout_seconds"])
            return True, ""

        with mock.patch.dict(
            installer.os.environ,
            {"OPENCLAW_MEMORY_PALACE_PROFILE_PROBE_TIMEOUT_SEC": "20"},
            clear=False,
        ), mock.patch.object(installer, "post_json_probe", side_effect=fake_probe), mock.patch.object(
            installer,
            "probe_embedding_dimension_recommendation_with_retries",
            return_value=(1024, ""),
        ):
            failures = installer.probe_profile_model_connectivity(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "http://127.0.0.1:11434/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                    "RETRIEVAL_RERANKER_API_BASE": "http://127.0.0.1:8080",
                    "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                    "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                    "WRITE_GUARD_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                    "WRITE_GUARD_LLM_API_KEY": "llm-key",
                    "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                },
                profile="c",
            )

        self.assertEqual(failures, [])
        self.assertEqual(calls, [20.0, 20.0, 20.0])

    def test_probe_embedding_dimension_recommendation_returns_baseline_when_higher_dim_fails(self) -> None:
        def fake_probe(*, dimensions=None, **kwargs):
            _ = kwargs
            if dimensions is None:
                return 1024, ""
            return None, "HTTP 400"

        with mock.patch.object(
            installer,
            "probe_embedding_dimension_with_retries",
            side_effect=fake_probe,
        ) as probe_mock:
            detected, detail = installer.probe_embedding_dimension_recommendation_with_retries(
                base_url="https://embedding.example/v1",
                model="embed-large",
                api_key="embed-key",
                timeout_seconds=8.0,
                attempts=2,
            )

        self.assertEqual(detected, 1024)
        self.assertEqual(detail, "HTTP 400")
        self.assertGreaterEqual(probe_mock.call_count, 2)

    def test_probe_embedding_dimension_recommendation_searches_up_to_highest_supported_dimension(self) -> None:
        def fake_probe(*, dimensions=None, **kwargs):
            _ = kwargs
            if dimensions is None:
                return 1024, ""
            if dimensions <= 4096:
                return dimensions, ""
            return None, "HTTP 400"

        with mock.patch.object(
            installer,
            "probe_embedding_dimension_with_retries",
            side_effect=fake_probe,
        ):
            detected, detail = installer.probe_embedding_dimension_recommendation_with_retries(
                base_url="https://embedding.example/v1",
                model="embed-large",
                api_key="embed-key",
                timeout_seconds=8.0,
                attempts=2,
            )

        self.assertEqual(detected, 4096)
        self.assertEqual(detail, "HTTP 400")

    def test_probe_profile_model_connectivity_uses_remote_timeout_from_env_values(self) -> None:
        calls: list[float] = []

        def fake_probe(**kwargs):
            calls.append(kwargs["timeout_seconds"])
            return True, ""

        with mock.patch.object(installer, "post_json_probe", side_effect=fake_probe), mock.patch.object(
            installer,
            "probe_embedding_dimension_recommendation_with_retries",
            return_value=(1024, ""),
        ):
            failures = installer.probe_profile_model_connectivity(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                    "RETRIEVAL_RERANKER_API_BASE": "http://127.0.0.1:8080",
                    "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                    "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                    "WRITE_GUARD_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                    "WRITE_GUARD_LLM_API_KEY": "llm-key",
                    "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                    "RETRIEVAL_REMOTE_TIMEOUT_SEC": "30",
                },
                profile="c",
            )

        self.assertEqual(failures, [])
        self.assertEqual(calls, [30.0, 30.0, 30.0])

    def test_probe_profile_model_connectivity_retries_transient_embedding_failure(self) -> None:
        calls: list[tuple[str, str]] = []
        embedding_attempts = {"count": 0}

        def fake_probe(**kwargs):
            endpoint = str(kwargs["endpoint"])
            calls.append((str(kwargs["base_url"]), endpoint))
            if endpoint == "/embeddings":
                embedding_attempts["count"] += 1
                if embedding_attempts["count"] == 1:
                    return False, "HTTP 502"
            return True, ""

        with mock.patch.dict(
            installer.os.environ,
            {"OPENCLAW_MEMORY_PALACE_PROFILE_PROBE_RETRIES": "2"},
            clear=False,
        ), mock.patch.object(installer, "post_json_probe", side_effect=fake_probe), mock.patch.object(
            installer,
            "probe_embedding_dimension_recommendation_with_retries",
            return_value=(1024, ""),
        ), mock.patch.object(
            installer.time,
            "sleep",
        ) as sleep_mock:
            failures = installer.probe_profile_model_connectivity(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                    "RETRIEVAL_RERANKER_API_BASE": "http://127.0.0.1:8080",
                    "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                    "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                    "WRITE_GUARD_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                    "WRITE_GUARD_LLM_API_KEY": "llm-key",
                    "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                },
                profile="c",
            )

        self.assertEqual(failures, [])
        self.assertEqual(embedding_attempts["count"], 2)
        self.assertEqual(len(calls), 4)
        sleep_mock.assert_called_once()

    def test_probe_profile_model_connectivity_aligns_embedding_dim_from_probe_result(self) -> None:
        env_values = {
            "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1",
            "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
            "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
            "RETRIEVAL_EMBEDDING_DIM": "1024",
            "RETRIEVAL_RERANKER_API_BASE": "http://127.0.0.1:8080",
            "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
            "RETRIEVAL_RERANKER_MODEL": "reranker-model",
            "WRITE_GUARD_LLM_API_BASE": "http://127.0.0.1:8317/v1",
            "WRITE_GUARD_LLM_API_KEY": "llm-key",
            "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
        }

        with mock.patch.object(
            installer,
            "post_json_probe",
            return_value=(True, ""),
        ), mock.patch.object(
            installer,
            "probe_embedding_dimension_recommendation_with_retries",
            return_value=(4096, ""),
        ):
            failures = installer.probe_profile_model_connectivity(
                env_values,
                profile="c",
            )

        self.assertEqual(failures, [])
        self.assertEqual(env_values["RETRIEVAL_EMBEDDING_DIM"], "4096")

    def test_perform_setup_warns_when_embedding_probe_aligns_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1/rerank",
                "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                "WRITE_GUARD_LLM_API_BASE": "https://llm.local/v1",
                "WRITE_GUARD_LLM_API_KEY": "llm-key",
                "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
            },
            clear=False,
        ):
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            runtime_python = setup_root / "runtime" / "bin" / "python"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")

            def fake_probe(env_values, **kwargs):
                env_values["RETRIEVAL_EMBEDDING_DIM"] = "4096"
                return []

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=False), mock.patch.object(
                installer,
                "probe_profile_model_connectivity",
                side_effect=fake_probe,
            ), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "read_json_file", return_value={}
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    profile="c",
                    mode="basic",
                    transport="stdio",
                )

        self.assertEqual(report["effective_profile"], "c")
        self.assertTrue(
            any("RETRIEVAL_EMBEDDING_DIM" in item and "4096" in item for item in report["warnings"])
        )

    def test_remove_plugin_from_openclaw_config_strips_memory_palace_fields(self) -> None:
        payload = {
            "plugins": {
                "allow": ["memory-palace", "other-plugin"],
                "load": {"paths": ["/repo/extensions/memory-palace", "/repo/other"]},
                "slots": {"memory": "memory-palace", "tools": "other-plugin"},
                "entries": {
                    "memory-palace": {"enabled": True},
                    "other-plugin": {"enabled": True},
                },
            }
        }

        merged, actions = installer.remove_plugin_from_openclaw_config(
            payload,
            plugin_install_root=Path("/repo/extensions/memory-palace"),
        )

        self.assertEqual(merged["plugins"]["allow"], ["other-plugin"])
        self.assertEqual(merged["plugins"]["load"]["paths"], ["/repo/other"])
        self.assertNotIn("memory", merged["plugins"]["slots"])
        self.assertNotIn("memory-palace", merged["plugins"]["entries"])
        self.assertTrue(actions)

    def test_stage_release_package_falls_back_to_in_place_refresh_when_release_rename_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            plugin_dir = root / "plugin"
            backend_dir = root / "backend"
            frontend_dir = root / "frontend"
            deploy_dir = root / "deploy"
            scripts_dir = root / "scripts"
            env_example = root / ".env.example"
            release_dir = plugin_dir / "release"

            for directory in (plugin_dir, backend_dir, frontend_dir, deploy_dir / "profiles", scripts_dir, release_dir):
                directory.mkdir(parents=True, exist_ok=True)

            (backend_dir / "backend.txt").write_text("fresh backend\n", encoding="utf-8")
            (frontend_dir / "frontend.txt").write_text("fresh frontend\n", encoding="utf-8")
            (deploy_dir / "profiles" / "profile-b.env").write_text("PROFILE=B\n", encoding="utf-8")
            env_example.write_text("BASE=1\n", encoding="utf-8")
            for file_name in (
                "openclaw_json_output.py",
                "openclaw_memory_palace.py",
                "openclaw_memory_palace_launcher.mjs",
                "openclaw_memory_palace_installer.py",
                "openclaw_memory_palace_windows_smoke.ps1",
                "serve_memory_palace_dashboard.py",
                "run_memory_palace_mcp_stdio.sh",
            ):
                (scripts_dir / file_name).write_text(f"{file_name}\n", encoding="utf-8")

            (release_dir / "stale.txt").write_text("stale\n", encoding="utf-8")

            original_replace = installer.Path.replace

            def fake_replace(path_obj: Path, target: Path):
                if path_obj.resolve() == release_dir.resolve():
                    raise PermissionError("release locked")
                return original_replace(path_obj, target)

            with mock.patch.object(installer, "plugin_root", return_value=plugin_dir), mock.patch.object(
                installer, "backend_root", return_value=backend_dir
            ), mock.patch.object(
                installer, "frontend_root", return_value=frontend_dir
            ), mock.patch.object(
                installer, "deploy_root", return_value=deploy_dir
            ), mock.patch.object(
                installer, "scripts_root", return_value=scripts_dir
            ), mock.patch.object(
                installer, "env_example_path", return_value=env_example
            ), mock.patch.object(installer.Path, "replace", autospec=True, side_effect=fake_replace):
                report = installer.stage_release_package()
            self.assertTrue(report["ok"])
            self.assertEqual(Path(report["release_root"]), release_dir.resolve())
            self.assertFalse((release_dir / "stale.txt").exists())
            self.assertTrue((release_dir / "backend" / "backend.txt").is_file())
            self.assertTrue((release_dir / "frontend" / "frontend.txt").is_file())
            self.assertTrue((release_dir / "deploy" / "profiles" / "profile-b.env").is_file())
            self.assertTrue((release_dir / "scripts" / "run_memory_palace_mcp_stdio.sh").is_file())

    def test_stage_release_package_ignores_stray_pytest_snapshot_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            plugin_dir = root / "plugin"
            backend_dir = root / "backend"
            scripts_dir = root / "scripts"
            env_example = root / ".env.example"
            release_dir = plugin_dir / "release"

            for directory in (plugin_dir, backend_dir, scripts_dir, release_dir):
                directory.mkdir(parents=True, exist_ok=True)

            (backend_dir / "backend.txt").write_text("fresh backend\n", encoding="utf-8")
            (
                backend_dir
                / "\\private\\var\\folders\\3z\\demo\\T\\pytest-of-demo\\pytest-1\\test_snapshot_manager_atomic_w0\\snapshots"
                / "orphan.txt"
            ).parent.mkdir(parents=True, exist_ok=True)
            (
                backend_dir
                / "\\private\\var\\folders\\3z\\demo\\T\\pytest-of-demo\\pytest-1\\test_snapshot_manager_atomic_w0\\snapshots"
                / "orphan.txt"
            ).write_text("orphan\n", encoding="utf-8")
            env_example.write_text("BASE=1\n", encoding="utf-8")
            for file_name in (
                "openclaw_json_output.py",
                "openclaw_memory_palace.py",
                "openclaw_memory_palace_launcher.mjs",
                "openclaw_memory_palace_installer.py",
                "openclaw_memory_palace_windows_smoke.ps1",
                "serve_memory_palace_dashboard.py",
                "run_memory_palace_mcp_stdio.sh",
            ):
                (scripts_dir / file_name).write_text(f"{file_name}\n", encoding="utf-8")

            with mock.patch.object(installer, "plugin_root", return_value=plugin_dir), mock.patch.object(
                installer, "backend_root", return_value=backend_dir
            ), mock.patch.object(
                installer, "frontend_root", return_value=root / "frontend-missing"
            ), mock.patch.object(
                installer, "deploy_root", return_value=root / "deploy-missing"
            ), mock.patch.object(
                installer, "scripts_root", return_value=scripts_dir
            ), mock.patch.object(
                installer, "env_example_path", return_value=env_example
            ):
                report = installer.stage_release_package()

            self.assertTrue(report["ok"])
            self.assertTrue((release_dir / "backend" / "backend.txt").is_file())
            copied_names = {path.name for path in (release_dir / "backend").iterdir()}
            self.assertNotIn(
                "\\private\\var\\folders\\3z\\demo\\T\\pytest-of-demo\\pytest-1\\test_snapshot_manager_atomic_w0\\snapshots",
                copied_names,
            )

    def test_stage_release_package_excludes_local_audit_and_frontend_tmp_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            plugin_dir = root / "plugin"
            backend_dir = root / "backend"
            frontend_dir = root / "frontend"
            scripts_dir = root / "scripts"
            installer_pkg_dir = scripts_dir / "installer"
            env_example = root / ".env.example"
            release_dir = plugin_dir / "release"

            for directory in (plugin_dir, backend_dir, frontend_dir, scripts_dir, installer_pkg_dir, release_dir):
                directory.mkdir(parents=True, exist_ok=True)

            (backend_dir / "main.py").write_text("print('backend')\n", encoding="utf-8")
            (backend_dir / "AUDIT_REPORT.md").write_text("audit\n", encoding="utf-8")
            (backend_dir / "CLAUDE.md").write_text("internal\n", encoding="utf-8")

            (frontend_dir / "src").mkdir(parents=True, exist_ok=True)
            (frontend_dir / "src" / "App.jsx").write_text("export default null;\n", encoding="utf-8")
            (frontend_dir / ".tmp" / "page-check").mkdir(parents=True, exist_ok=True)
            (frontend_dir / ".tmp" / "page-check" / "capture.png").write_text("tmp\n", encoding="utf-8")
            (frontend_dir / "coverage").mkdir(parents=True, exist_ok=True)
            (frontend_dir / "coverage" / "lcov.info").write_text("TN:\n", encoding="utf-8")
            (frontend_dir / "AUDIT-REPORT.md").write_text("audit\n", encoding="utf-8")
            (frontend_dir / "CLAUDE.md").write_text("internal\n", encoding="utf-8")

            env_example.write_text("BASE=1\n", encoding="utf-8")
            for file_name in (
                "openclaw_json_output.py",
                "openclaw_memory_palace.py",
                "openclaw_memory_palace_launcher.mjs",
                "openclaw_memory_palace_installer.py",
                "openclaw_memory_palace_windows_smoke.ps1",
                "serve_memory_palace_dashboard.py",
                "run_memory_palace_mcp_stdio.sh",
            ):
                (scripts_dir / file_name).write_text(f"{file_name}\n", encoding="utf-8")
            (installer_pkg_dir / "__init__.py").write_text("from ._core import marker\n", encoding="utf-8")
            (installer_pkg_dir / "_core.py").write_text("marker = 'ok'\n", encoding="utf-8")

            with mock.patch.object(installer, "plugin_root", return_value=plugin_dir), mock.patch.object(
                installer, "backend_root", return_value=backend_dir
            ), mock.patch.object(
                installer, "frontend_root", return_value=frontend_dir
            ), mock.patch.object(
                installer, "deploy_root", return_value=root / "deploy-missing"
            ), mock.patch.object(
                installer, "scripts_root", return_value=scripts_dir
            ), mock.patch.object(
                installer, "env_example_path", return_value=env_example
            ):
                report = installer.stage_release_package()

            self.assertTrue(report["ok"])
            self.assertTrue((release_dir / "backend" / "main.py").is_file())
            self.assertFalse((release_dir / "backend" / "AUDIT_REPORT.md").exists())
            self.assertFalse((release_dir / "backend" / "CLAUDE.md").exists())
            self.assertTrue((release_dir / "frontend" / "src" / "App.jsx").is_file())
            self.assertFalse((release_dir / "frontend" / ".tmp").exists())
            self.assertFalse((release_dir / "frontend" / "coverage").exists())
            self.assertFalse((release_dir / "frontend" / "AUDIT-REPORT.md").exists())
            self.assertFalse((release_dir / "frontend" / "CLAUDE.md").exists())
            self.assertTrue((release_dir / "scripts" / "installer" / "__init__.py").is_file())
            self.assertTrue((release_dir / "scripts" / "installer" / "_core.py").is_file())

    def test_stage_release_package_keeps_frontend_dist_and_dashboard_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            plugin_dir = root / "plugin"
            backend_dir = root / "backend"
            frontend_dir = root / "frontend"
            scripts_dir = root / "scripts"
            env_example = root / ".env.example"
            release_dir = plugin_dir / "release"

            for directory in (plugin_dir, backend_dir, frontend_dir / "dist", scripts_dir, release_dir):
                directory.mkdir(parents=True, exist_ok=True)

            (backend_dir / "main.py").write_text("print('backend')\n", encoding="utf-8")
            (frontend_dir / "package.json").write_text('{"name":"dashboard"}\n', encoding="utf-8")
            (frontend_dir / "dist" / "index.html").write_text("<title>Memory Palace Dashboard</title>\n", encoding="utf-8")
            (frontend_dir / ".playwright-cli" / "cache").mkdir(parents=True, exist_ok=True)
            (frontend_dir / ".playwright-cli" / "cache" / "debug.txt").write_text("tmp\n", encoding="utf-8")
            env_example.write_text("BASE=1\n", encoding="utf-8")
            for file_name in (
                "openclaw_json_output.py",
                "openclaw_memory_palace.py",
                "openclaw_memory_palace_launcher.mjs",
                "openclaw_memory_palace_installer.py",
                "openclaw_memory_palace_windows_smoke.ps1",
                "serve_memory_palace_dashboard.py",
                "run_memory_palace_mcp_stdio.sh",
            ):
                (scripts_dir / file_name).write_text(f"{file_name}\n", encoding="utf-8")

            with mock.patch.object(installer, "plugin_root", return_value=plugin_dir), mock.patch.object(
                installer, "backend_root", return_value=backend_dir
            ), mock.patch.object(
                installer, "frontend_root", return_value=frontend_dir
            ), mock.patch.object(
                installer, "deploy_root", return_value=root / "deploy-missing"
            ), mock.patch.object(
                installer, "scripts_root", return_value=scripts_dir
            ), mock.patch.object(
                installer, "env_example_path", return_value=env_example
            ):
                report = installer.stage_release_package()

            self.assertTrue(report["ok"])
            self.assertTrue((release_dir / "frontend" / "dist" / "index.html").is_file())
            self.assertFalse((release_dir / "frontend" / ".playwright-cli").exists())
            self.assertTrue((release_dir / "scripts" / "serve_memory_palace_dashboard.py").is_file())

    def test_ensure_plugin_install_root_dry_run_uses_current_package_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package_root = Path(tmp_dir) / "package"
            package_root.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(installer, "detect_plugin_install_root", return_value=None), mock.patch.object(
                installer, "package_layout", return_value="package"
            ), mock.patch.object(
                installer, "plugin_root", return_value=package_root
            ), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/openclaw"
            ):
                plugin_root, actions, warnings = installer.ensure_plugin_install_root(dry_run=True)

        self.assertEqual(plugin_root, package_root.resolve())
        self.assertEqual(warnings, [])
        self.assertTrue(any("would install plugin from current package path" in item for item in actions))

    def test_ensure_plugin_install_root_prefers_env_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir, mock.patch.dict(
            installer.os.environ,
            {"OPENCLAW_MEMORY_PALACE_PLUGIN_ROOT_HINT": str(Path(tmp_dir) / "hinted-plugin")},
            clear=False,
        ):
            hinted_root = Path(tmp_dir) / "hinted-plugin"
            hinted_root.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(installer, "detect_plugin_install_root", return_value=None):
                plugin_root, actions, warnings = installer.ensure_plugin_install_root(dry_run=False)

        self.assertEqual(plugin_root, hinted_root.resolve())
        self.assertEqual(warnings, [])
        self.assertTrue(any("reused hinted plugin install root" in item for item in actions))

    def test_ensure_plugin_install_root_retries_detection_after_auto_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package_root = Path(tmp_dir) / "package"
            package_root.mkdir(parents=True, exist_ok=True)
            installed_root = Path(tmp_dir) / "installed-plugin"
            completed = subprocess.CompletedProcess(
                args=["openclaw", "plugins", "install", str(package_root)],
                returncode=0,
                stdout="ok",
                stderr="",
            )
            with mock.patch.object(installer, "resolve_plugin_install_root_hint", return_value=None), mock.patch.object(
                installer, "detect_plugin_install_root", side_effect=[None, None, installed_root]
            ) as detect_plugin_root, mock.patch.object(
                installer, "package_layout", return_value="package"
            ), mock.patch.object(
                installer, "resolve_openclaw_binary", return_value="/usr/bin/openclaw"
            ), mock.patch.object(
                installer, "plugin_root", return_value=package_root
            ), mock.patch.object(
                installer, "build_openclaw_plugins_install_command", return_value=["openclaw", "plugins", "install", str(package_root)]
            ), mock.patch.object(
                installer.subprocess, "run", return_value=completed
            ) as subprocess_run, mock.patch.object(
                installer.time, "sleep"
            ) as sleep_mock:
                plugin_root, actions, warnings = installer.ensure_plugin_install_root(dry_run=False)

        self.assertEqual(plugin_root, installed_root)
        self.assertEqual(warnings, [])
        self.assertTrue(any("installed plugin from current package path" in item for item in actions))
        self.assertEqual(detect_plugin_root.call_count, 3)
        sleep_mock.assert_called_once_with(0.5)
        subprocess_run.assert_called_once()

    def test_ensure_plugin_install_root_falls_back_to_expected_state_dir_after_auto_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            package_root = Path(tmp_dir) / "package"
            package_root.mkdir(parents=True, exist_ok=True)
            setup_root = Path(tmp_dir) / "home" / ".openclaw" / "memory-palace-runtime"
            expected_root = setup_root.parent / "state" / "extensions" / installer.PLUGIN_ID
            expected_root.mkdir(parents=True, exist_ok=True)
            completed = subprocess.CompletedProcess(
                args=["openclaw", "plugins", "install", str(package_root)],
                returncode=0,
                stdout="ok",
                stderr="",
            )
            with mock.patch.object(installer, "resolve_plugin_install_root_hint", return_value=None), mock.patch.object(
                installer, "detect_plugin_install_root", return_value=None
            ) as detect_plugin_root, mock.patch.object(
                installer, "package_layout", return_value="package"
            ), mock.patch.object(
                installer, "resolve_openclaw_binary", return_value="/usr/bin/openclaw"
            ), mock.patch.object(
                installer, "plugin_root", return_value=package_root
            ), mock.patch.object(
                installer, "build_openclaw_plugins_install_command", return_value=["openclaw", "plugins", "install", str(package_root)]
            ), mock.patch.object(
                installer.subprocess, "run", return_value=completed
            ) as subprocess_run:
                plugin_root, actions, warnings = installer.ensure_plugin_install_root(
                    setup_root_path=setup_root,
                    dry_run=False,
                )

        self.assertEqual(plugin_root, expected_root.resolve())
        self.assertEqual(warnings, [])
        self.assertTrue(any("state dir" in item for item in actions))
        self.assertGreaterEqual(detect_plugin_root.call_count, 1)
        subprocess_run.assert_called_once()

    def test_detect_plugin_install_root_prefers_current_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo_plugin_root = Path(tmp_dir) / "repo-plugin"
            repo_plugin_root.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(installer, "package_layout", return_value="repo"), mock.patch.object(
                installer,
                "plugin_root",
                return_value=repo_plugin_root,
            ), mock.patch.object(
                installer,
                "detect_installed_plugin_root",
                return_value=Path(tmp_dir) / "installed-plugin",
            ):
                detected = installer.detect_plugin_install_root()

        self.assertEqual(detected, repo_plugin_root.resolve())

    def test_ensure_frontend_dashboard_dry_run_reports_install_and_start_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            fake_frontend = Path(tmp_dir) / "frontend"
            fake_frontend.mkdir(parents=True, exist_ok=True)
            (fake_frontend / "package-lock.json").write_text("{}", encoding="utf-8")
            with mock.patch.object(installer, "frontend_root", return_value=fake_frontend), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/npm"
            ):
                dashboard, actions, warnings = installer.ensure_frontend_dashboard(
                    setup_root_path=setup_root,
                    dry_run=True,
                )

        self.assertEqual(warnings, [])
        self.assertEqual(dashboard["status"], "dry_run")
        self.assertEqual(dashboard["deliveryMode"], "vite_dev_server")
        self.assertFalse(dashboard["running"])
        self.assertTrue(any("would install dashboard dependencies" in item for item in actions))
        self.assertTrue(any("would start dashboard Vite dev server" in item for item in actions))

    def test_perform_setup_full_mode_warns_that_dashboard_is_dev_stack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"
            setup_root = Path(tmp_dir) / "runtime"
            runtime_python = setup_root / "runtime" / "Scripts" / "python.exe"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(installer, "supports_interactive_profile_prompt", return_value=False), mock.patch.object(
                installer, "ensure_runtime_venv", return_value=(runtime_python, [])
            ), mock.patch.object(
                installer, "ensure_plugin_install_root", return_value=(Path(tmp_dir) / "plugin", [], [])
            ), mock.patch.object(
                installer, "read_json_file", return_value={}
            ), mock.patch.object(
                installer, "merge_openclaw_config", return_value=({}, [])
            ), mock.patch.object(
                installer, "write_json_file"
            ), mock.patch.object(
                installer, "detect_restart_required", return_value=(False, [])
            ), mock.patch.object(
                installer, "collect_install_checks", return_value=[]
            ), mock.patch.object(
                installer, "build_setup_state", return_value={}
            ), mock.patch.object(
                installer, "build_next_steps", return_value=[]
            ), mock.patch.object(
                installer, "ensure_backend_http_api", return_value=({"status": "dry_run"}, [], [])
            ), mock.patch.object(
                installer,
                "ensure_frontend_dashboard",
                return_value=({"status": "dry_run", "deliveryMode": "vite_dev_server"}, [], []),
            ), mock.patch.object(
                installer,
                "detect_openclaw_version",
                return_value={"raw": "OpenClaw 2026.3.13", "parsed": (2026, 3, 13), "version": "2026.3.13", "meets_minimum": True},
            ):
                report = installer.perform_setup(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    profile="b",
                    mode="full",
                    transport="stdio",
                )

        self.assertTrue(any("Vite dev server" in warning for warning in report["warnings"]))

    def test_ensure_frontend_dashboard_prefers_vite_binary_after_dependencies_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            fake_frontend = Path(tmp_dir) / "frontend"
            vite_bin = fake_frontend / "node_modules" / ".bin" / (
                "vite.cmd" if os.name == "nt" else "vite"
            )
            vite_bin.parent.mkdir(parents=True, exist_ok=True)
            fake_frontend.mkdir(parents=True, exist_ok=True)
            (fake_frontend / "package-lock.json").write_text("{}", encoding="utf-8")
            (fake_frontend / "package.json").write_text('{"name":"dashboard"}\n', encoding="utf-8")
            vite_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            vite_bin.chmod(0o755)

            with mock.patch.object(installer, "frontend_root", return_value=fake_frontend), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/npm"
            ), mock.patch.object(
                installer,
                "_port_open",
                return_value=False,
            ), mock.patch.object(
                installer,
                "wait_for_dashboard_ready",
                return_value=True,
            ), mock.patch.object(
                installer,
                "_build_pid_file_record",
                return_value={"pid": 43210, "start_marker": "mock-start"},
            ), mock.patch.object(
                installer.subprocess,
                "Popen",
                return_value=mock.Mock(pid=43210),
            ) as popen:
                dashboard, actions, warnings = installer.ensure_frontend_dashboard(
                    setup_root_path=setup_root,
                    dry_run=False,
                )

        self.assertEqual(warnings, [])
        self.assertEqual(dashboard["status"], "running")
        self.assertTrue(any("started dashboard Vite dev server" in item for item in actions))
        self.assertEqual(popen.call_args.args[0][0], str(vite_bin))

    def test_ensure_frontend_dashboard_uses_packaged_static_bundle_without_npm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            fake_frontend = Path(tmp_dir) / "frontend"
            dist_dir = fake_frontend / "dist"
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "index.html").write_text("<html><body>Memory Palace</body></html>\n", encoding="utf-8")
            fake_scripts = Path(tmp_dir) / "scripts"
            fake_scripts.mkdir(parents=True, exist_ok=True)
            static_server = fake_scripts / "serve_memory_palace_dashboard.py"
            static_server.write_text("print('ok')\n", encoding="utf-8")
            runtime_python = Path(tmp_dir) / "runtime-python"
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(installer, "frontend_root", return_value=fake_frontend), mock.patch.object(
                installer, "scripts_root", return_value=fake_scripts
            ), mock.patch.object(
                installer, "package_layout", return_value="package"
            ), mock.patch.object(
                installer.shutil, "which", return_value=None
            ), mock.patch.object(
                installer, "_port_open", return_value=False
            ), mock.patch.object(
                installer, "wait_for_dashboard_ready", return_value=True
            ), mock.patch.object(
                installer,
                "_build_pid_file_record",
                return_value={"pid": 43210, "start_marker": "mock-start"},
            ), mock.patch.object(
                installer.subprocess,
                "Popen",
                return_value=mock.Mock(pid=43210),
            ) as popen:
                dashboard, actions, warnings = installer.ensure_frontend_dashboard(
                    setup_root_path=setup_root,
                    runtime_python_path=runtime_python,
                    dry_run=False,
                )

        self.assertEqual(warnings, [])
        self.assertEqual(dashboard["status"], "running")
        self.assertEqual(dashboard["deliveryMode"], "static_bundle")
        self.assertFalse(dashboard["installsDependenciesAtRuntime"])
        self.assertTrue(any("started packaged static dashboard" in item for item in actions))
        self.assertEqual(popen.call_args.args[0][0], str(runtime_python))
        self.assertIn("serve_memory_palace_dashboard.py", popen.call_args.args[0][1])

    def test_ensure_frontend_dashboard_dry_run_reports_packaged_static_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            fake_frontend = Path(tmp_dir) / "frontend"
            dist_dir = fake_frontend / "dist"
            dist_dir.mkdir(parents=True, exist_ok=True)
            (dist_dir / "index.html").write_text("<html><body>Memory Palace</body></html>\n", encoding="utf-8")
            fake_scripts = Path(tmp_dir) / "scripts"
            fake_scripts.mkdir(parents=True, exist_ok=True)
            (fake_scripts / "serve_memory_palace_dashboard.py").write_text("print('ok')\n", encoding="utf-8")
            runtime_python = Path(tmp_dir) / "runtime-python"
            runtime_python.write_text("", encoding="utf-8")

            with mock.patch.object(installer, "frontend_root", return_value=fake_frontend), mock.patch.object(
                installer, "scripts_root", return_value=fake_scripts
            ), mock.patch.object(
                installer, "package_layout", return_value="package"
            ):
                dashboard, actions, warnings = installer.ensure_frontend_dashboard(
                    setup_root_path=setup_root,
                    runtime_python_path=runtime_python,
                    dry_run=True,
                )

        self.assertEqual(warnings, [])
        self.assertEqual(dashboard["status"], "dry_run")
        self.assertEqual(dashboard["deliveryMode"], "static_bundle")
        self.assertTrue(any("would start packaged static dashboard" in item for item in actions))

    def test_ensure_frontend_dashboard_retries_dependency_install(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            fake_frontend = Path(tmp_dir) / "frontend"
            vite_bin = fake_frontend / "node_modules" / ".bin" / (
                "vite.cmd" if os.name == "nt" else "vite"
            )
            fake_frontend.mkdir(parents=True, exist_ok=True)
            (fake_frontend / "package-lock.json").write_text("{}", encoding="utf-8")
            (fake_frontend / "package.json").write_text('{"name":"dashboard"}\n', encoding="utf-8")
            attempts: list[list[str]] = []

            def fake_run(command, **kwargs):
                if command[:2] == ["/usr/bin/npm", "ci"]:
                    attempts.append(list(command))
                    if len(attempts) == 1:
                        return subprocess.CompletedProcess(command, 1, "", "network reset")
                    vite_bin.parent.mkdir(parents=True, exist_ok=True)
                    vite_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    vite_bin.chmod(0o755)
                    return subprocess.CompletedProcess(command, 0, "", "")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(installer, "frontend_root", return_value=fake_frontend), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/npm"
            ), mock.patch.object(
                installer,
                "_port_open",
                return_value=False,
            ), mock.patch.object(
                installer,
                "wait_for_dashboard_ready",
                return_value=True,
            ), mock.patch.object(
                installer.subprocess,
                "run",
                side_effect=fake_run,
            ), mock.patch.object(
                installer.subprocess,
                "Popen",
                return_value=mock.Mock(pid=43210),
            ), mock.patch.object(installer.time, "sleep") as sleep_mock:
                dashboard, actions, warnings = installer.ensure_frontend_dashboard(
                    setup_root_path=setup_root,
                    dry_run=False,
                )

        self.assertEqual(warnings, [])
        self.assertEqual(dashboard["status"], "running")
        self.assertEqual(len(attempts), 2)
        self.assertIn("--prefer-offline", attempts[0])
        self.assertTrue(any("installed dashboard dependencies" in item for item in actions))
        sleep_mock.assert_called_once()

    def test_ensure_frontend_dashboard_accepts_existing_external_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            fake_frontend = Path(tmp_dir) / "frontend"
            fake_frontend.mkdir(parents=True, exist_ok=True)
            node_modules = fake_frontend / "node_modules"
            node_modules.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(installer, "frontend_root", return_value=fake_frontend), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/npm"
            ), mock.patch.object(installer, "_port_open", return_value=True):
                with mock.patch.object(installer, "_dashboard_service_ready", return_value=True):
                    dashboard, actions, warnings = installer.ensure_frontend_dashboard(
                        setup_root_path=setup_root,
                        dry_run=False,
                    )

        self.assertEqual(warnings, [])
        self.assertTrue(dashboard["running"])
        self.assertEqual(dashboard["status"], "running_external")
        self.assertTrue(any("dashboard already reachable" in item for item in actions))

    def test_ensure_frontend_dashboard_warns_when_foreign_service_occupies_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            fake_frontend = Path(tmp_dir) / "frontend"
            fake_frontend.mkdir(parents=True, exist_ok=True)
            node_modules = fake_frontend / "node_modules"
            node_modules.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(installer, "frontend_root", return_value=fake_frontend), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/npm"
            ), mock.patch.object(installer, "_port_open", return_value=True), mock.patch.object(
                installer, "_dashboard_service_ready", return_value=False
            ), mock.patch.object(
                installer, "_find_available_loopback_port", return_value=55173
            ):
                dashboard, actions, warnings = installer.ensure_frontend_dashboard(
                    setup_root_path=setup_root,
                    dry_run=False,
                )

        self.assertTrue(any("reused existing dashboard dependencies" in item for item in actions))
        self.assertFalse(dashboard["running"])
        self.assertEqual(dashboard["status"], "port_in_use")
        self.assertTrue(any("已被其他服务占用" in item for item in warnings))
        self.assertTrue(any("--dashboard-port 55173" in item for item in warnings))

    def test_dashboard_status_reports_external_reachability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            with mock.patch.object(installer, "_port_open", return_value=True), mock.patch.object(
                installer, "_dashboard_service_ready", return_value=True
            ):
                report = installer.dashboard_status(setup_root_value=str(setup_root))

        self.assertTrue(report["ok"])
        self.assertEqual(report["dashboard"]["status"], "running_external")

    def test_ensure_frontend_dashboard_cleans_up_timed_out_process_and_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            fake_frontend = Path(tmp_dir) / "frontend"
            fake_frontend.mkdir(parents=True, exist_ok=True)
            (fake_frontend / "package-lock.json").write_text("{}", encoding="utf-8")
            (fake_frontend / "package.json").write_text('{"name":"dashboard"}\n', encoding="utf-8")
            node_modules = fake_frontend / "node_modules"
            node_modules.mkdir(parents=True, exist_ok=True)
            pid_path = installer.default_dashboard_pid_path(setup_root)

            with mock.patch.object(installer, "frontend_root", return_value=fake_frontend), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/npm"
            ), mock.patch.object(
                installer, "_port_open", return_value=False
            ), mock.patch.object(
                installer, "wait_for_dashboard_ready", return_value=False
            ), mock.patch.object(
                installer, "_terminate_process", return_value=True
            ) as terminate_process, mock.patch.object(
                installer,
                "_build_pid_file_record",
                return_value={"pid": 43210, "start_marker": "mock-start"},
            ), mock.patch.object(
                installer.subprocess, "Popen", return_value=mock.Mock(pid=43210)
            ):
                dashboard, actions, warnings = installer.ensure_frontend_dashboard(
                    setup_root_path=setup_root,
                    dry_run=False,
                )

        self.assertEqual(dashboard["status"], "start_timeout")
        terminate_process.assert_called_once_with(43210)
        self.assertFalse(pid_path.exists())
        self.assertTrue(any("stopped timed-out dashboard process" in item for item in actions))
        self.assertTrue(any("启动超时" in item for item in warnings))

    def test_terminate_process_uses_windows_process_tree_kill(self) -> None:
        original_name = installer.os.name
        installer.os.name = "nt"
        try:
            with mock.patch.object(
                installer,
                "_is_process_alive",
                return_value=True,
            ) as is_alive, mock.patch.object(
                installer,
                "_kill_process_tree_windows",
                return_value=True,
            ) as kill_tree:
                with mock.patch.object(
                    installer,
                    "_wait_for_process_exit",
                    return_value=True,
                ) as wait_for_exit:
                    terminated = installer._terminate_process(43210)
        finally:
            installer.os.name = original_name

        self.assertTrue(terminated)
        kill_tree.assert_called_once_with(43210, force=False)
        wait_for_exit.assert_called_once_with(43210)
        is_alive.assert_called_once_with(43210)

    def test_terminate_process_forces_windows_process_tree_when_needed(self) -> None:
        original_name = installer.os.name
        installer.os.name = "nt"
        try:
            with mock.patch.object(
                installer,
                "_is_process_alive",
                side_effect=[True, True],
            ) as is_alive, mock.patch.object(
                installer,
                "_kill_process_tree_windows",
                side_effect=[True, True],
            ) as kill_tree:
                with mock.patch.object(
                    installer,
                    "_wait_for_process_exit",
                    side_effect=[False, True],
                ) as wait_for_exit:
                    terminated = installer._terminate_process(24680)
        finally:
            installer.os.name = original_name

        self.assertTrue(terminated)
        self.assertEqual(
            kill_tree.call_args_list,
            [mock.call(24680, force=False), mock.call(24680, force=True)],
        )
        self.assertEqual(wait_for_exit.call_args_list, [mock.call(24680), mock.call(24680, timeout_seconds=5.0)])
        self.assertEqual(is_alive.call_count, 2)

    def test_terminate_process_uses_process_group_on_posix(self) -> None:
        original_name = installer.os.name
        installer.os.name = "posix"
        try:
            with mock.patch.object(
                installer,
                "_is_process_alive",
                return_value=True,
            ) as is_alive, mock.patch.object(
                installer.os,
                "killpg",
            ) as killpg, mock.patch.object(
                installer,
                "_wait_for_process_group_exit",
                return_value=True,
            ) as wait_for_group, mock.patch.object(
                installer.os,
                "getpgid",
                return_value=43210,
            ) as getpgid:
                terminated = installer._terminate_process(43210)
        finally:
            installer.os.name = original_name

        self.assertTrue(terminated)
        getpgid.assert_called_once_with(43210)
        killpg.assert_called_once_with(43210, installer.signal.SIGTERM)
        wait_for_group.assert_called_once_with(43210, timeout_seconds=5.0)
        is_alive.assert_called_once_with(43210)

    def test_dashboard_stop_skips_unmanaged_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            pid_path = installer.default_dashboard_pid_path(setup_root)
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text("12345\n", encoding="utf-8")

            with mock.patch.object(installer, "_is_process_alive", return_value=True), mock.patch.object(
                installer, "_pid_file_record_matches_running_process", return_value=False
            ), mock.patch.object(
                installer, "_terminate_process"
            ) as terminate_process:
                report = installer.dashboard_stop(setup_root_value=str(setup_root))

        self.assertFalse(pid_path.exists())
        self.assertFalse(terminate_process.called)
        self.assertTrue(any("不匹配" in warning for warning in report["warnings"]))

    def test_ensure_backend_http_api_dry_run_reports_start_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text("DATABASE_URL=sqlite+aiosqlite:////tmp/demo.db\n", encoding="utf-8")
            runtime_python = setup_root / "runtime" / "bin" / "python"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
            fake_backend = Path(tmp_dir) / "backend"
            fake_backend.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(installer, "backend_root", return_value=fake_backend):
                backend_api, actions, warnings = installer.ensure_backend_http_api(
                    setup_root_path=setup_root,
                    runtime_python_path=runtime_python,
                    runtime_env_file=env_file,
                    dry_run=True,
                )

        self.assertEqual(warnings, [])
        self.assertEqual(backend_api["status"], "dry_run")
        self.assertTrue(any("would start backend HTTP API" in item for item in actions))

    def test_ensure_backend_http_api_accepts_existing_external_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text("DATABASE_URL=sqlite+aiosqlite:////tmp/demo.db\n", encoding="utf-8")
            runtime_python = setup_root / "runtime" / "bin" / "python"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
            fake_backend = Path(tmp_dir) / "backend"
            fake_backend.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(installer, "backend_root", return_value=fake_backend), mock.patch.object(
                installer, "_port_open", return_value=True
            ), mock.patch.object(
                installer, "_backend_api_service_ready", return_value=True
            ):
                backend_api, actions, warnings = installer.ensure_backend_http_api(
                    setup_root_path=setup_root,
                    runtime_python_path=runtime_python,
                    runtime_env_file=env_file,
                    dry_run=False,
                )

        self.assertEqual(warnings, [])
        self.assertTrue(backend_api["running"])
        self.assertEqual(backend_api["status"], "running_external")
        self.assertTrue(any("backend HTTP API already reachable" in item for item in actions))

    def test_ensure_backend_http_api_warns_when_foreign_service_occupies_port(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text("DATABASE_URL=sqlite+aiosqlite:////tmp/demo.db\n", encoding="utf-8")
            runtime_python = setup_root / "runtime" / "bin" / "python"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
            fake_backend = Path(tmp_dir) / "backend"
            fake_backend.mkdir(parents=True, exist_ok=True)
            with mock.patch.object(installer, "backend_root", return_value=fake_backend), mock.patch.object(
                installer, "_port_open", return_value=True
            ), mock.patch.object(
                installer, "_backend_api_service_ready", return_value=False
            ), mock.patch.object(
                installer, "_find_available_loopback_port", return_value=58000
            ):
                backend_api, actions, warnings = installer.ensure_backend_http_api(
                    setup_root_path=setup_root,
                    runtime_python_path=runtime_python,
                    runtime_env_file=env_file,
                    dry_run=False,
                )

        self.assertEqual(actions, [])
        self.assertEqual(backend_api["status"], "port_in_use")
        self.assertTrue(any("--backend-api-port 58000" in item for item in warnings))

    def test_ensure_backend_http_api_cleans_up_timed_out_process_and_pid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text("DATABASE_URL=sqlite+aiosqlite:////tmp/demo.db\n", encoding="utf-8")
            runtime_python = setup_root / "runtime" / "bin" / "python"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
            fake_backend = Path(tmp_dir) / "backend"
            fake_backend.mkdir(parents=True, exist_ok=True)
            pid_path = installer.default_backend_api_pid_path(setup_root)

            with mock.patch.object(installer, "backend_root", return_value=fake_backend), mock.patch.object(
                installer, "_port_open", return_value=False
            ), mock.patch.object(
                installer, "wait_for_backend_api_ready", return_value=False
            ), mock.patch.object(
                installer, "_terminate_process", return_value=True
            ) as terminate_process, mock.patch.object(
                installer, "_is_process_alive", return_value=False
            ), mock.patch.object(
                installer,
                "_build_pid_file_record",
                return_value={"pid": 24680, "start_marker": "mock-start"},
            ), mock.patch.object(
                installer.subprocess, "Popen", return_value=mock.Mock(pid=24680)
            ):
                backend_api, actions, warnings = installer.ensure_backend_http_api(
                    setup_root_path=setup_root,
                    runtime_python_path=runtime_python,
                    runtime_env_file=env_file,
                    dry_run=False,
                )

        self.assertEqual(backend_api["status"], "start_timeout")
        terminate_process.assert_called_once_with(24680)
        self.assertFalse(pid_path.exists())
        self.assertTrue(any("stopped timed-out backend HTTP API process" in item for item in actions))
        self.assertTrue(any("启动超时" in item for item in warnings))

    def test_ensure_backend_http_api_propagates_config_path_from_runtime_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text(
                "\n".join(
                    [
                        "DATABASE_URL=sqlite+aiosqlite:////tmp/demo.db",
                        "OPENCLAW_MEMORY_PALACE_CONFIG_PATH=/tmp/isolated-openclaw.json",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            runtime_python = setup_root / "runtime" / "bin" / "python"
            runtime_python.parent.mkdir(parents=True, exist_ok=True)
            runtime_python.write_text("", encoding="utf-8")
            fake_backend = Path(tmp_dir) / "backend"
            fake_backend.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(installer, "backend_root", return_value=fake_backend), mock.patch.object(
                installer, "_port_open", return_value=False
            ), mock.patch.object(
                installer, "wait_for_backend_api_ready", return_value=True
            ), mock.patch.object(
                installer,
                "_build_pid_file_record",
                return_value={"pid": 24680, "start_marker": "mock-start"},
            ), mock.patch.object(
                installer.subprocess,
                "Popen",
                return_value=mock.Mock(pid=24680),
            ) as popen:
                backend_api, actions, warnings = installer.ensure_backend_http_api(
                    setup_root_path=setup_root,
                    runtime_python_path=runtime_python,
                    runtime_env_file=env_file,
                    dry_run=False,
                )

        self.assertEqual(warnings, [])
        self.assertEqual(backend_api["status"], "running")
        self.assertTrue(any("started backend HTTP API" in item for item in actions))
        self.assertEqual(
            popen.call_args.kwargs["env"]["OPENCLAW_CONFIG_PATH"],
            "/tmp/isolated-openclaw.json",
        )
        self.assertEqual(
            popen.call_args.kwargs["env"]["OPENCLAW_MEMORY_PALACE_ENV_FILE"],
            str(env_file),
        )

    def test_dashboard_start_requires_bootstrap_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(RuntimeError, "Bootstrap runtime env is missing"):
                installer.dashboard_start(setup_root_value=tmp_dir, dry_run=False)

    def test_perform_uninstall_cleans_config_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            setup_root.mkdir(parents=True, exist_ok=True)
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "plugins": {
                            "allow": ["memory-palace"],
                            "load": {"paths": ["/repo/extensions/memory-palace"]},
                            "slots": {"memory": "memory-palace"},
                            "entries": {"memory-palace": {"enabled": True}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(installer, "detect_config_path_with_source", return_value=(config_path, "explicit")), mock.patch.object(
                installer, "detect_plugin_install_root", return_value=Path("/repo/extensions/memory-palace")
            ), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/openclaw"
            ), mock.patch.object(
                installer.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
            ):
                report = installer.perform_uninstall(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    remove_runtime=True,
                    force=True,
                    dry_run=False,
                )
            self.assertTrue(report["ok"])
            self.assertFalse(setup_root.exists())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["plugins"]["allow"], [])
            self.assertNotIn("memory", payload["plugins"]["slots"])
            self.assertNotIn("memory-palace", payload["plugins"]["entries"])

    def test_perform_uninstall_restores_previous_memory_slot_and_keeps_runtime_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text(
                "OPENCLAW_MEMORY_PALACE_PREVIOUS_MEMORY_SLOT=legacy-memory\n",
                encoding="utf-8",
            )
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "plugins": {
                            "allow": ["memory-palace"],
                            "load": {"paths": ["/repo/extensions/memory-palace"]},
                            "slots": {"memory": "memory-palace"},
                            "entries": {"memory-palace": {"enabled": True}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(installer, "detect_config_path_with_source", return_value=(config_path, "explicit")), mock.patch.object(
                installer, "detect_plugin_install_root", return_value=Path("/repo/extensions/memory-palace")
            ), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/openclaw"
            ), mock.patch.object(
                installer.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
            ):
                report = installer.perform_uninstall(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    force=True,
                    dry_run=False,
                )

            self.assertTrue(report["ok"])
            self.assertTrue(setup_root.exists())
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["plugins"]["slots"]["memory"], "legacy-memory")

    def test_perform_uninstall_warns_when_previous_memory_slot_metadata_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            setup_root.mkdir(parents=True, exist_ok=True)
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "plugins": {
                            "allow": ["memory-palace"],
                            "load": {"paths": ["/repo/extensions/memory-palace"]},
                            "slots": {"memory": "memory-palace"},
                            "entries": {"memory-palace": {"enabled": True}},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(installer, "detect_config_path_with_source", return_value=(config_path, "explicit")), mock.patch.object(
                installer, "detect_plugin_install_root", return_value=Path("/repo/extensions/memory-palace")
            ), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/openclaw"
            ), mock.patch.object(
                installer.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
            ):
                report = installer.perform_uninstall(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    force=True,
                    dry_run=False,
                )

        self.assertTrue(any("未找到之前的 memory slot" in item for item in report["warnings"]))
        self.assertTrue(str(report["config_backup_path"]).endswith(".bak"))

    def test_perform_uninstall_dry_run_does_not_stop_processes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "plugins": {
                            "allow": ["memory-palace", "memory-core"],
                            "load": {"paths": ["/repo/extensions/memory-palace"]},
                            "slots": {"memory": "memory-palace"},
                            "entries": {
                                "memory-palace": {"enabled": True},
                                "memory-core": {"enabled": True},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                installer, "detect_config_path_with_source", return_value=(config_path, "explicit")
            ), mock.patch.object(
                installer, "detect_plugin_install_root", return_value=Path("/repo/extensions/memory-palace")
            ), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/openclaw"
            ), mock.patch.object(
                installer, "_read_pid_file", side_effect=[1234, 5678]
            ), mock.patch.object(
                installer, "_terminate_process"
            ) as terminate_process:
                report = installer.perform_uninstall(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    dry_run=True,
                )

        terminate_process.assert_not_called()
        self.assertIn("would execute `openclaw plugins uninstall memory-palace`", report["actions"])
        self.assertIn("would stop dashboard dev server", report["actions"])
        self.assertIn("would stop backend HTTP API", report["actions"])

    def test_perform_uninstall_forwards_config_path_to_openclaw_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            config_path = Path(tmp_dir) / "isolated-openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "plugins": {
                            "allow": ["memory-palace", "memory-core"],
                            "load": {"paths": ["/repo/extensions/memory-palace"]},
                            "slots": {"memory": "memory-palace"},
                            "entries": {
                                "memory-palace": {"enabled": True},
                                "memory-core": {"enabled": True},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            completed = mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(
                installer, "detect_config_path_with_source", return_value=(config_path, "explicit")
            ), mock.patch.object(
                installer, "detect_plugin_install_root", return_value=Path("/repo/extensions/memory-palace")
            ), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/openclaw"
            ), mock.patch.object(
                installer.subprocess, "run", return_value=completed
            ) as run_process:
                installer.perform_uninstall(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    dry_run=False,
                )

        self.assertEqual(
            run_process.call_args.kwargs["env"]["OPENCLAW_CONFIG_PATH"],
            str(config_path),
        )

    def test_perform_uninstall_removes_memory_core_compat_shim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            config_path = Path(tmp_dir) / "openclaw.json"
            config_path.write_text(
                json.dumps(
                    {
                        "plugins": {
                            "allow": ["memory-palace", "memory-core"],
                            "load": {"paths": ["/repo/extensions/memory-palace"]},
                            "slots": {"memory": "memory-palace"},
                            "entries": {
                                "memory-palace": {"enabled": True},
                                "memory-core": {"enabled": True},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                installer, "detect_config_path_with_source", return_value=(config_path, "explicit")
            ), mock.patch.object(
                installer, "detect_plugin_install_root", return_value=Path("/repo/extensions/memory-palace")
            ), mock.patch.object(
                installer.shutil, "which", return_value="/usr/bin/openclaw"
            ), mock.patch.object(
                installer.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")
            ):
                installer.perform_uninstall(
                    config=str(config_path),
                    setup_root_value=str(setup_root),
                    dry_run=False,
                )
                payload = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["plugins"]["allow"], [])
                self.assertNotIn("memory-palace", payload["plugins"]["entries"])
                self.assertNotIn("memory-core", payload["plugins"]["entries"])

    def test_detect_restart_required_reports_mismatched_runtime_env_keys(self) -> None:
        restart_required, mismatch_keys = installer.detect_restart_required(
            {
                "DATABASE_URL": "sqlite+aiosqlite:////tmp/new.db",
                "SEARCH_DEFAULT_MODE": "hybrid",
            },
            current_env={
                "DATABASE_URL": "sqlite+aiosqlite:////tmp/old.db",
                "SEARCH_DEFAULT_MODE": "hybrid",
            },
        )

        self.assertTrue(restart_required)
        self.assertEqual(mismatch_keys, ["DATABASE_URL"])

    def test_detect_restart_required_is_false_when_no_runtime_env_exists(self) -> None:
        restart_required, mismatch_keys = installer.detect_restart_required({})

        self.assertFalse(restart_required)
        self.assertEqual(mismatch_keys, [])

    def test_detect_reindex_required_reports_retrieval_pipeline_changes(self) -> None:
        reindex_required, changed_keys = installer.detect_reindex_required(
            {
                "RETRIEVAL_EMBEDDING_BACKEND": "api",
                "RETRIEVAL_EMBEDDING_DIM": "1024",
                "RETRIEVAL_RERANKER_ENABLED": "true",
            },
            {
                "RETRIEVAL_EMBEDDING_BACKEND": "hash",
                "RETRIEVAL_EMBEDDING_DIM": "64",
                "RETRIEVAL_RERANKER_ENABLED": "false",
            },
        )

        self.assertTrue(reindex_required)
        self.assertEqual(
            changed_keys,
            [
                "RETRIEVAL_EMBEDDING_BACKEND",
                "RETRIEVAL_EMBEDDING_DIM",
                "RETRIEVAL_RERANKER_ENABLED",
            ],
        )

    def test_detect_reindex_required_is_false_without_previous_runtime_env(self) -> None:
        reindex_required, changed_keys = installer.detect_reindex_required({}, {})

        self.assertFalse(reindex_required)
        self.assertEqual(changed_keys, [])

    def test_perform_migrate_reads_database_url_from_runtime_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text(
                "\n".join(
                    [
                        "DATABASE_URL=sqlite+aiosqlite:////tmp/memory.db",
                        f"{installer._metadata_key('MODE')}=full",
                        f"{installer._metadata_key('PROFILE_REQUESTED')}=c",
                        f"{installer._metadata_key('TRANSPORT')}=stdio",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                installer,
                "ensure_runtime_venv",
                return_value=(Path("/tmp/runtime/bin/python"), ["bootstrapped runtime venv"]),
            ), mock.patch.object(
                installer,
                "_run_runtime_migration_task",
                return_value={
                    "summary": "Applied 1 migration(s).",
                    "database_file": "/tmp/memory.db",
                    "migrations_dir": "/repo/backend/db/migrations",
                    "lock_file": "/tmp/memory.db.migrate.lock",
                    "dry_run": False,
                    "current_versions_before": ["0001"],
                    "pending_versions_before": ["0002"],
                    "applied_versions": ["0002"],
                    "current_versions": ["0001", "0002"],
                    "pending_versions_after": [],
                },
            ) as run_migration, mock.patch.object(
                installer,
                "inspect_backend_api_state",
                return_value={"running": False, "status": "stopped"},
            ):
                report = installer.perform_migrate(
                    config="/tmp/openclaw.json",
                    setup_root_value=str(setup_root),
                    env_file_value=str(env_file),
                    dry_run=False,
                )

        self.assertEqual(report["database_url"], "sqlite+aiosqlite:////tmp/memory.db")
        self.assertEqual(report["applied_versions"], ["0002"])
        self.assertEqual(report["requested_profile"], "c")
        self.assertEqual(report["mode"], "full")
        run_migration.assert_called_once_with(
            runtime_python=Path("/tmp/runtime/bin/python"),
            database_url="sqlite+aiosqlite:////tmp/memory.db",
            migrations_dir=None,
            lock_file_path=None,
            lock_timeout_seconds=10.0,
            dry_run=False,
        )

    def test_perform_migrate_errors_when_database_url_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text(
                f"{installer._metadata_key('MODE')}=basic\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                installer,
                "ensure_runtime_venv",
                return_value=(Path("/tmp/runtime/bin/python"), []),
            ), self.assertRaisesRegex(RuntimeError, "does not define DATABASE_URL"):
                installer.perform_migrate(
                    setup_root_value=str(setup_root),
                    env_file_value=str(env_file),
                )

    def test_perform_upgrade_reuses_persisted_mode_profile_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            setup_root = Path(tmp_dir) / "runtime"
            env_file = setup_root / "runtime.env"
            updated_env_file = setup_root / "updated-runtime.env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text(
                "\n".join(
                    [
                        "DATABASE_URL=sqlite+aiosqlite:////tmp/memory.db",
                        f"{installer._metadata_key('MODE')}=dev",
                        f"{installer._metadata_key('PROFILE_REQUESTED')}=d",
                        f"{installer._metadata_key('TRANSPORT')}=sse",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                installer,
                "perform_setup",
                return_value={
                    "ok": True,
                    "summary": "setup ok",
                    "config_path": "/tmp/openclaw.json",
                    "env_file": str(updated_env_file),
                    "plugin_root": "/tmp/plugin",
                    "mode": "dev",
                    "requested_profile": "d",
                    "effective_profile": "d",
                    "transport": "sse",
                    "warnings": ["setup warning"],
                    "actions": ["setup action"],
                    "next_steps": ["setup next step"],
                },
            ) as perform_setup, mock.patch.object(
                installer,
                "perform_migrate",
                return_value={
                    "ok": True,
                    "summary": "migrate ok",
                    "applied_versions": ["0003"],
                    "warnings": ["migrate warning"],
                    "actions": ["migrate action"],
                    "next_steps": ["migrate next step"],
                },
            ) as perform_migrate:
                report = installer.perform_upgrade(
                    config="/tmp/openclaw.json",
                    setup_root_value=str(setup_root),
                    env_file_value=str(env_file),
                    strict_profile=True,
                    dry_run=True,
                )

        self.assertTrue(report["ok"])
        self.assertEqual(report["mode"], "dev")
        self.assertEqual(report["requested_profile"], "d")
        self.assertEqual(report["transport"], "sse")
        resolved_config = str(Path("/tmp/openclaw.json").resolve())
        resolved_setup_root = str(setup_root.resolve())
        resolved_env_file = str(env_file.resolve())
        perform_setup.assert_called_once_with(
            config=resolved_config,
            setup_root_value=resolved_setup_root,
            env_file_value=resolved_env_file,
            transport="sse",
            mode="dev",
            profile="d",
            reconfigure=True,
            strict_profile=True,
            dry_run=True,
            json_output=True,
        )
        perform_migrate.assert_called_once_with(
            config=resolved_config,
            setup_root_value=resolved_setup_root,
            env_file_value=str(updated_env_file.resolve()),
            dry_run=True,
        )
        self.assertIn("setup warning", report["warnings"])
        self.assertIn("migrate warning", report["warnings"])
        self.assertIn("setup action", report["actions"])
        self.assertIn("migrate action", report["actions"])
        self.assertIn("setup next step", report["next_steps"])
        self.assertIn("migrate next step", report["next_steps"])


class WrapperTests(unittest.TestCase):
    def test_default_openclaw_bin_prefers_openclaw_bin_env(self) -> None:
        with (
            mock.patch.dict(os.environ, {"OPENCLAW_BIN": "/tmp/custom-openclaw"}, clear=False),
            mock.patch.object(wrapper.shutil, "which", return_value="/opt/homebrew/bin/openclaw"),
        ):
            self.assertEqual(wrapper.default_openclaw_bin(), "/tmp/custom-openclaw")

    def test_default_openclaw_bin_prefers_real_openclaw_when_available(self) -> None:
        with mock.patch.object(wrapper.shutil, "which", return_value="/opt/homebrew/bin/openclaw"):
            self.assertEqual(wrapper.default_openclaw_bin(), "/opt/homebrew/bin/openclaw")

    def test_default_openclaw_bin_falls_back_to_maintainer_wrapper_on_posix(self) -> None:
        if os.name == "nt":
            self.skipTest("repo wrapper preference is POSIX-only")

        expected = str(
            (Path(__file__).resolve().parents[1] / "scripts" / "dev" / "openclaw-local-wrapper").resolve()
        )
        with mock.patch.object(wrapper.shutil, "which", return_value=None):
            self.assertEqual(wrapper.default_openclaw_bin(), expected)

    def test_wrapper_parse_args_accepts_setup_stack_ports_and_dashboard_commands(self) -> None:
        with mock.patch.object(
            sys,
            "argv",
            [
                "memory-palace-openclaw",
                "setup",
                "--mode",
                "full",
                "--dashboard-port",
                "15173",
                "--backend-api-port",
                "18000",
                "--validate",
                "--openclaw-bin",
                "/usr/local/bin/openclaw",
            ],
        ):
            args = wrapper.parse_args()

        self.assertEqual(args.command, "setup")
        self.assertEqual(args.dashboard_port, 15173)
        self.assertEqual(args.backend_api_port, 18000)
        self.assertTrue(args.validate)
        self.assertEqual(args.openclaw_bin, "/usr/local/bin/openclaw")

        with mock.patch.object(
            sys,
            "argv",
            [
                "memory-palace-openclaw",
                "dashboard",
                "start",
                "--dashboard-port",
                "25173",
                "--backend-api-port",
                "28000",
            ],
        ):
            dashboard_args = wrapper.parse_args()

        self.assertEqual(dashboard_args.command, "dashboard")
        self.assertEqual(dashboard_args.dashboard_command, "start")
        self.assertEqual(dashboard_args.dashboard_port, 25173)
        self.assertEqual(dashboard_args.backend_api_port, 28000)

        with mock.patch.object(
            sys,
            "argv",
            [
                "memory-palace-openclaw",
                "migrate",
                "--setup-root",
                "/tmp/runtime",
                "--lock-timeout-sec",
                "30",
            ],
        ):
            migrate_args = wrapper.parse_args()

        self.assertEqual(migrate_args.command, "migrate")
        self.assertEqual(migrate_args.setup_root, "/tmp/runtime")
        self.assertEqual(migrate_args.lock_timeout_sec, 30.0)

        with mock.patch.object(
            sys,
            "argv",
            [
                "memory-palace-openclaw",
                "upgrade",
                "--strict-profile",
                "--dry-run",
            ],
        ):
            upgrade_args = wrapper.parse_args()

        self.assertEqual(upgrade_args.command, "upgrade")
        self.assertTrue(upgrade_args.strict_profile)
        self.assertTrue(upgrade_args.dry_run)

        with mock.patch.object(
            sys,
            "argv",
            [
                "memory-palace-openclaw",
                "onboarding",
                "--mode",
                "full",
                "--profile",
                "c",
                "--apply",
                "--validate",
                "--strict-profile",
                "--embedding-dim",
                "3072",
            ],
        ):
            onboarding_args = wrapper.parse_args()

        self.assertEqual(onboarding_args.command, "onboarding")
        self.assertEqual(onboarding_args.mode, "full")
        self.assertEqual(onboarding_args.profile, "c")
        self.assertTrue(onboarding_args.apply)
        self.assertTrue(onboarding_args.validate)
        self.assertTrue(onboarding_args.strict_profile)
        self.assertEqual(onboarding_args.embedding_dim, "3072")

    def test_build_dimension_guidance_prefers_detected_max_and_recommended_dim(self) -> None:
        payload = wrapper._build_dimension_guidance(
            {
                "requiresProviders": True,
                "effectiveProfile": "c",
                "providers": {
                    "embedding": {
                        "detectedDim": "2048",
                        "detectedMaxDim": "3072",
                        "recommendedDim": "3072",
                    }
                },
            },
            {"ok": True, "effectiveProfile": "c"},
        )

        self.assertEqual(payload["detectedMaxDimension"], "3072")
        self.assertEqual(payload["recommendedDimension"], "3072")
        self.assertTrue(payload["willWriteRecommendedDimension"])
        self.assertIn("3072", str(payload["summary"]))

    def test_profile_strategy_payload_marks_profile_b_as_llm_optional_but_retrieval_limited(self) -> None:
        payload = wrapper._profile_strategy_payload("b")

        boundaries = payload.get("boundaries")
        self.assertIsInstance(boundaries, list)
        self.assertTrue(
            any("hash embedding" in str(item) and "LLM" in str(item) for item in boundaries),
            boundaries,
        )

    def test_bundled_onboarding_skill_file_exists(self) -> None:
        self.skipTest("Onboarding skill packaging is covered by scripts/test_openclaw_memory_palace_package_install.py")

        with mock.patch.object(
            sys,
            "argv",
            [
                "memory-palace-openclaw",
                "provider-probe",
                "--profile",
                "c",
                "--embedding-api-base",
                "https://embedding.example/v1",
                "--persist",
                "--json",
            ],
        ):
            provider_probe_args = wrapper.parse_args()

        self.assertEqual(provider_probe_args.command, "provider-probe")
        self.assertEqual(provider_probe_args.profile, "c")
        self.assertEqual(provider_probe_args.embedding_api_base, "https://embedding.example/v1")
        self.assertTrue(provider_probe_args.persist)
        self.assertTrue(provider_probe_args.json)

    def test_onboarding_field_payload_respects_cli_language(self) -> None:
        with mock.patch.object(wrapper.installer, "cli_language", return_value="en"):
            payload_en = wrapper._field_payload("RETRIEVAL_EMBEDDING_API_BASE", required=True)
        with mock.patch.object(wrapper.installer, "cli_language", return_value="zh"):
            payload_zh = wrapper._field_payload("RETRIEVAL_EMBEDDING_API_BASE", required=True)

        self.assertEqual(payload_en["label"], "Embedding API base URL")
        self.assertIn("OpenAI-compatible embedding endpoint", str(payload_en["hint"]))
        self.assertEqual(payload_zh["label"], "Embedding API Base URL")
        self.assertIn("OpenAI-compatible embedding 接口地址", str(payload_zh["hint"]))

        with mock.patch.object(
            sys,
            "argv",
            [
                "memory-palace-openclaw",
                "onboarding",
                "--profile",
                "c",
                "--mode",
                "full",
                "--apply",
                "--validate",
                "--embedding-api-base",
                "https://embedding.example/v1",
                "--llm-model",
                "gpt-5.4",
                "--json",
            ],
        ):
            onboarding_args = wrapper.parse_args()

        self.assertEqual(onboarding_args.command, "onboarding")
        self.assertEqual(onboarding_args.profile, "c")
        self.assertEqual(onboarding_args.mode, "full")
        self.assertTrue(onboarding_args.apply)
        self.assertTrue(onboarding_args.validate)
        self.assertEqual(onboarding_args.embedding_api_base, "https://embedding.example/v1")
        self.assertEqual(onboarding_args.llm_model, "gpt-5.4")
        self.assertTrue(onboarding_args.json)

    def test_command_provider_probe_uses_installer_preview(self) -> None:
        payload = {
            "requestedProfile": "c",
            "effectiveProfile": "c",
            "summaryStatus": "pass",
            "summaryMessage": "Advanced provider checks passed for the current profile.",
            "providers": {
                "embedding": {
                    "status": "pass",
                    "baseUrl": "https://embedding.example/v1",
                    "model": "embed-large",
                    "detectedDim": "1024",
                    "detail": "Probe passed.",
                }
            },
        }
        args = argparse.Namespace(
            profile="c",
            mode="basic",
            transport="stdio",
            config=None,
            setup_root=None,
            env_file=None,
            embedding_api_base="https://embedding.example/v1",
            embedding_api_key="embed-key",
            embedding_model="embed-large",
            embedding_dim=None,
            reranker_api_base=None,
            reranker_api_key=None,
            reranker_model=None,
            llm_api_base=None,
            llm_api_key=None,
            llm_model=None,
            write_guard_llm_api_base=None,
            write_guard_llm_api_key=None,
            write_guard_llm_model=None,
            compact_gist_llm_api_base=None,
            compact_gist_llm_api_key=None,
            compact_gist_llm_model=None,
            persist=True,
            json=True,
        )

        with mock.patch.object(wrapper.installer, "preview_provider_probe_status", return_value=payload) as preview_mock:
            with mock.patch.object(sys, "stdout", new=io.StringIO()) as stdout:
                exit_code = wrapper.command_provider_probe(args)

        self.assertEqual(exit_code, 0)
        preview_mock.assert_called_once()
        self.assertIn('"requestedProfile": "c"', stdout.getvalue())

    def test_command_onboarding_reports_detected_embedding_dimension(self) -> None:
        args = argparse.Namespace(
            profile="c",
            mode="full",
            transport="stdio",
            config=None,
            setup_root=None,
            env_file=None,
            sse_url=None,
            api_key_env="MCP_API_KEY",
            timeout_ms=20000,
            connect_retries=1,
            connect_backoff_ms=250,
            database_path=None,
            mcp_api_key=None,
            allow_insecure_local=False,
            allow_generate_remote_api_key=False,
            reconfigure=False,
            strict_profile=False,
            dashboard_host=None,
            dashboard_port=None,
            backend_api_host=None,
            backend_api_port=None,
            no_activate=False,
            dry_run=False,
            json=True,
            apply=False,
            validate=False,
            openclaw_bin="/usr/local/bin/openclaw",
            embedding_api_base="https://embedding.example/v1",
            embedding_api_key="embed-key",
            embedding_model="embed-large",
            embedding_dim=None,
            reranker_api_base="https://reranker.example/v1",
            reranker_api_key="rerank-key",
            reranker_model="rerank-large",
            llm_api_base="https://llm.example/v1",
            llm_api_key="llm-key",
            llm_model="gpt-5.4",
            write_guard_llm_api_base=None,
            write_guard_llm_api_key=None,
            write_guard_llm_model=None,
            compact_gist_llm_api_base=None,
            compact_gist_llm_api_key=None,
            compact_gist_llm_model=None,
        )
        preview_payload = {
            "requestedProfile": "c",
            "effectiveProfile": "c",
            "summaryStatus": "pass",
            "summaryMessage": "Advanced provider checks passed for the current profile.",
            "providers": {
                "embedding": {
                    "status": "pass",
                    "baseUrl": "https://embedding.example/v1",
                    "model": "embed-large",
                    "detectedDim": "4096",
                    "detail": "Probe passed.",
                    "missingFields": [],
                },
                "reranker": {
                    "status": "pass",
                    "baseUrl": "https://reranker.example/v1",
                    "model": "rerank-large",
                    "detail": "Probe passed.",
                    "missingFields": [],
                },
                "llm": {
                    "status": "pass",
                    "baseUrl": "https://llm.example/v1",
                    "model": "gpt-5.4",
                    "detail": "Probe passed.",
                    "missingFields": [],
                },
            },
            "missingFields": [],
        }
        applied_env = {
            "WRITE_GUARD_LLM_ENABLED": "true",
            "COMPACT_GIST_LLM_ENABLED": "true",
        }

        with mock.patch.object(wrapper.installer, "load_env_file", return_value={}), mock.patch.object(
            wrapper.installer,
            "apply_setup_defaults",
            side_effect=[
                (applied_env, "c", [], False, []),
                (applied_env, "c", [], False, []),
            ],
        ), mock.patch.object(
            wrapper.installer,
            "preview_provider_probe_status",
            return_value=preview_payload,
        ):
            with mock.patch.object(sys, "stdout", new=io.StringIO()) as stdout:
                exit_code = wrapper.command_onboarding(args)

        self.assertEqual(exit_code, 0)
        rendered = stdout.getvalue()
        self.assertIn('"detectedMaxDimension": "4096"', rendered)
        self.assertIn('"recommendedDimension": "4096"', rendered)
        self.assertIn('"responsesAliasAccepted": true', rendered)
        self.assertIn('"effectiveProfile": "c"', rendered)

    def test_command_onboarding_apply_attaches_setup_report_and_validation(self) -> None:
        args = argparse.Namespace(
            profile="c",
            mode="basic",
            transport="stdio",
            config=None,
            setup_root=None,
            env_file=None,
            sse_url=None,
            api_key_env="MCP_API_KEY",
            timeout_ms=20000,
            connect_retries=1,
            connect_backoff_ms=250,
            database_path=None,
            mcp_api_key=None,
            allow_insecure_local=False,
            allow_generate_remote_api_key=False,
            reconfigure=False,
            strict_profile=False,
            dashboard_host=None,
            dashboard_port=None,
            backend_api_host=None,
            backend_api_port=None,
            no_activate=False,
            dry_run=False,
            json=True,
            apply=True,
            validate=True,
            openclaw_bin="/usr/local/bin/openclaw",
            embedding_api_base="https://embedding.example/v1",
            embedding_api_key="embed-key",
            embedding_model="embed-large",
            embedding_dim=None,
            reranker_api_base="https://reranker.example/v1",
            reranker_api_key="rerank-key",
            reranker_model="rerank-large",
            llm_api_base="https://llm.example/v1",
            llm_api_key="llm-key",
            llm_model="gpt-5.4",
            write_guard_llm_api_base=None,
            write_guard_llm_api_key=None,
            write_guard_llm_model=None,
            compact_gist_llm_api_base=None,
            compact_gist_llm_api_key=None,
            compact_gist_llm_model=None,
        )
        preview_payload = {
            "requestedProfile": "c",
            "effectiveProfile": "c",
            "summaryStatus": "pass",
            "summaryMessage": "Advanced provider checks passed for the current profile.",
            "providers": {
                "embedding": {"status": "pass", "detectedDim": "4096", "missingFields": []},
                "reranker": {"status": "pass", "missingFields": []},
                "llm": {"status": "pass", "missingFields": []},
            },
            "missingFields": [],
        }
        applied_env = {
            "WRITE_GUARD_LLM_ENABLED": "true",
            "COMPACT_GIST_LLM_ENABLED": "true",
        }
        setup_report = {
            "summary": "Setup completed for mode=basic, requested profile=C, effective profile=C.",
            "config_path": "/tmp/openclaw.json",
            "env_file": "/tmp/runtime.env",
            "plugin_root": "/tmp/plugin",
            "mode": "basic",
            "requested_profile": "c",
            "effective_profile": "c",
            "transport": "stdio",
            "warnings": [],
            "actions": [],
            "next_steps": [],
        }

        with mock.patch.object(wrapper.installer, "load_env_file", return_value={}), mock.patch.object(
            wrapper.installer,
            "apply_setup_defaults",
            side_effect=[
                (applied_env, "c", [], False, []),
                (applied_env, "c", [], False, []),
            ],
        ), mock.patch.object(
            wrapper.installer,
            "preview_provider_probe_status",
            return_value=preview_payload,
        ), mock.patch.object(
            wrapper,
            "perform_setup_from_namespace",
            return_value=setup_report,
        ), mock.patch.object(
            wrapper,
            "run_setup_validation",
            return_value={
                "ok": True,
                "failed_step": None,
                "steps": [{"name": "verify", "ok": True, "summary": "verify passed"}],
            },
        ) as validation_mock:
            with mock.patch.object(sys, "stdout", new=io.StringIO()) as stdout:
                exit_code = wrapper.command_onboarding(args)

        self.assertEqual(exit_code, 0)
        validation_mock.assert_called_once_with(
            openclaw_bin="/usr/local/bin/openclaw",
            config_path=Path("/tmp/openclaw.json"),
            timeout_seconds=wrapper.DEFAULT_OPENCLAW_JSON_TIMEOUT_SECONDS,
        )
        rendered = stdout.getvalue()
        self.assertIn('"appliedSetup"', rendered)
        self.assertIn('"effective_profile": "c"', rendered)

    def test_command_onboarding_apply_promotes_process_env_overrides_into_setup_args(self) -> None:
        args = argparse.Namespace(
            profile="c",
            mode="basic",
            transport="stdio",
            config=None,
            setup_root=None,
            env_file=None,
            sse_url=None,
            api_key_env="MCP_API_KEY",
            timeout_ms=20000,
            connect_retries=1,
            connect_backoff_ms=250,
            database_path=None,
            mcp_api_key=None,
            allow_insecure_local=False,
            allow_generate_remote_api_key=False,
            reconfigure=False,
            strict_profile=False,
            dashboard_host=None,
            dashboard_port=None,
            backend_api_host=None,
            backend_api_port=None,
            no_activate=False,
            dry_run=False,
            json=True,
            apply=True,
            validate=False,
            openclaw_bin="/usr/local/bin/openclaw",
            embedding_api_base=None,
            embedding_api_key=None,
            embedding_model=None,
            embedding_dim=None,
            reranker_api_base=None,
            reranker_api_key=None,
            reranker_model=None,
            llm_api_base=None,
            llm_api_key=None,
            llm_model=None,
            write_guard_llm_api_base=None,
            write_guard_llm_api_key=None,
            write_guard_llm_model=None,
            compact_gist_llm_api_base=None,
            compact_gist_llm_api_key=None,
            compact_gist_llm_model=None,
        )
        preview_payload = {
            "requestedProfile": "c",
            "effectiveProfile": "c",
            "summaryStatus": "pass",
            "summaryMessage": "Advanced provider checks passed for the current profile.",
            "providers": {
                "embedding": {"status": "pass", "detectedDim": "1024", "missingFields": []},
                "reranker": {"status": "pass", "missingFields": []},
                "llm": {"status": "pass", "missingFields": []},
            },
            "missingFields": [],
        }
        applied_env = {
            "WRITE_GUARD_LLM_ENABLED": "true",
            "COMPACT_GIST_LLM_ENABLED": "true",
        }
        setup_report = {
            "summary": "Setup completed for mode=basic, requested profile=C, effective profile=C.",
            "config_path": "/tmp/openclaw.json",
            "env_file": "/tmp/runtime.env",
            "plugin_root": "/tmp/plugin",
            "mode": "basic",
            "requested_profile": "c",
            "effective_profile": "c",
            "transport": "stdio",
            "warnings": [],
            "actions": [],
            "next_steps": [],
        }
        process_overrides = {
            "embedding_api_base": "https://embedding.example/v1",
            "embedding_api_key": "embed-key",
            "embedding_model": "embed-large",
            "embedding_dim": "1024",
            "reranker_api_base": "https://reranker.example/v1",
            "reranker_api_key": "rerank-key",
            "reranker_model": "rerank-large",
            "llm_api_base": "https://llm.example/v1",
            "llm_api_key": "llm-key",
            "llm_model": "gpt-5.4",
        }

        with mock.patch.object(wrapper.installer, "load_env_file", return_value={"RETRIEVAL_EMBEDDING_MODEL": "hash-v1"}), mock.patch.object(
            wrapper.installer,
            "apply_setup_defaults",
            side_effect=[
                (applied_env, "c", [], False, []),
                (applied_env, "c", [], False, []),
            ],
        ), mock.patch.object(
            wrapper.installer,
            "preview_provider_probe_status",
            return_value=preview_payload,
        ), mock.patch.object(
            wrapper.installer,
            "current_process_runtime_overrides",
            return_value=process_overrides,
        ), mock.patch.object(
            wrapper,
            "perform_setup_from_namespace",
            return_value=setup_report,
        ) as perform_setup_mock:
            with mock.patch.object(sys, "stdout", new=io.StringIO()):
                exit_code = wrapper.command_onboarding(args)

        self.assertEqual(exit_code, 0)
        promoted_args = perform_setup_mock.call_args.args[0]
        self.assertEqual(promoted_args.embedding_api_base, "https://embedding.example/v1")
        self.assertEqual(promoted_args.embedding_model, "embed-large")
        self.assertEqual(promoted_args.embedding_dim, "1024")
        self.assertEqual(promoted_args.reranker_model, "rerank-large")
        self.assertEqual(promoted_args.llm_model, "gpt-5.4")

    def test_command_onboarding_apply_marks_top_level_ok_false_when_validation_fails(self) -> None:
        args = argparse.Namespace(
            profile="c",
            mode="basic",
            transport="stdio",
            config=None,
            setup_root=None,
            env_file=None,
            sse_url=None,
            api_key_env="MCP_API_KEY",
            timeout_ms=20000,
            connect_retries=1,
            connect_backoff_ms=250,
            database_path=None,
            mcp_api_key=None,
            allow_insecure_local=False,
            allow_generate_remote_api_key=False,
            reconfigure=False,
            strict_profile=False,
            dashboard_host=None,
            dashboard_port=None,
            backend_api_host=None,
            backend_api_port=None,
            no_activate=False,
            dry_run=False,
            json=True,
            apply=True,
            validate=True,
            openclaw_bin="/usr/local/bin/openclaw",
            embedding_api_base=None,
            embedding_api_key=None,
            embedding_model=None,
            embedding_dim=None,
            reranker_api_base=None,
            reranker_api_key=None,
            reranker_model=None,
            llm_api_base=None,
            llm_api_key=None,
            llm_model=None,
            write_guard_llm_api_base=None,
            write_guard_llm_api_key=None,
            write_guard_llm_model=None,
            compact_gist_llm_api_base=None,
            compact_gist_llm_api_key=None,
            compact_gist_llm_model=None,
        )
        preview_payload = {
            "requestedProfile": "c",
            "effectiveProfile": "c",
            "summaryStatus": "pass",
            "summaryMessage": "Advanced provider checks passed for the current profile.",
            "providers": {},
            "missingFields": [],
        }
        setup_report = {
            "ok": True,
            "summary": "Setup completed.",
            "config_path": "/tmp/openclaw.json",
            "env_file": "/tmp/runtime.env",
            "plugin_root": "/tmp/plugin",
            "mode": "basic",
            "requested_profile": "c",
            "effective_profile": "c",
            "transport": "stdio",
            "warnings": [],
            "actions": [],
            "next_steps": [],
        }

        with mock.patch.object(wrapper.installer, "load_env_file", return_value={}), mock.patch.object(
            wrapper.installer,
            "apply_setup_defaults",
            return_value=({}, "c", [], False, []),
        ), mock.patch.object(
            wrapper.installer,
            "preview_provider_probe_status",
            return_value=preview_payload,
        ), mock.patch.object(
            wrapper,
            "perform_setup_from_namespace",
            return_value=setup_report,
        ), mock.patch.object(
            wrapper,
            "run_setup_validation",
            return_value={
                "ok": False,
                "failed_step": "doctor",
                "steps": [{"name": "doctor", "ok": False, "summary": "doctor failed"}],
            },
        ):
            with mock.patch.object(sys, "stdout", new=io.StringIO()) as stdout:
                exit_code = wrapper.command_onboarding(args)

        self.assertEqual(exit_code, 1)
        rendered = json.loads(stdout.getvalue())
        self.assertFalse(rendered["ok"])
        self.assertFalse(rendered["appliedSetup"]["ok"])
        self.assertFalse(rendered["appliedSetup"]["validation"]["ok"])

    def test_stdio_wrapper_defaults_openclaw_rerank_top_n_to_12(self) -> None:
        wrapper_paths = [
            Path(__file__).resolve().parent / "run_memory_palace_mcp_stdio.sh",
            Path(__file__).resolve().parents[1]
            / "extensions"
            / "memory-palace"
            / "release"
            / "scripts"
            / "run_memory_palace_mcp_stdio.sh",
        ]
        for wrapper_path in wrapper_paths:
            wrapper_script = wrapper_path.read_text(encoding="utf-8")
            self.assertIn('RETRIEVAL_RERANK_TOP_N="${RETRIEVAL_RERANK_TOP_N:-12}"', wrapper_script)
            self.assertIn('printf -v "${key}"', wrapper_script)
            self.assertNotIn('eval "${exports}"', wrapper_script)

    def test_stdio_wrapper_normalizes_quoted_env_values(self) -> None:
        wrapper_paths = [
            Path(__file__).resolve().parent / "run_memory_palace_mcp_stdio.sh",
            Path(__file__).resolve().parents[1]
            / "extensions"
            / "memory-palace"
            / "release"
            / "scripts"
            / "run_memory_palace_mcp_stdio.sh",
        ]
        for wrapper_path in wrapper_paths:
            wrapper_script = wrapper_path.read_text(encoding="utf-8")
            self.assertIn('if [[ ${#value} -ge 2 ]]; then', wrapper_script)
            self.assertIn('value="${value:1:${#value}-2}"', wrapper_script)
            self.assertIn('[[ ! "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]', wrapper_script)

    def test_stdio_wrapper_supports_export_prefixed_env_lines(self) -> None:
        wrapper_paths = [
            Path(__file__).resolve().parent / "run_memory_palace_mcp_stdio.sh",
            Path(__file__).resolve().parents[1]
            / "extensions"
            / "memory-palace"
            / "release"
            / "scripts"
            / "run_memory_palace_mcp_stdio.sh",
        ]
        for wrapper_path in wrapper_paths:
            wrapper_script = wrapper_path.read_text(encoding="utf-8")
            self.assertIn('if [[ "${normalized}" == export[[:space:]]* ]]; then', wrapper_script)
            self.assertIn('normalized="${normalized#export }"', wrapper_script)

    def test_release_gate_script_supports_windows_venv_python_paths_and_crlf_safe_env_checks(self) -> None:
        gate_script = (Path(__file__).resolve().parent / "pre_publish_check.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('${venv_root}/Scripts/python.exe', gate_script)
        self.assertIn("normalized=\"${line%$'\\r'}\"", gate_script)
        self.assertIn('printf \'%s\\n\' "npx --yes bun"', gate_script)
        self.assertIn("npm install --no-save typescript@^5.9.3 @types/node@^25.5.0", gate_script)
        self.assertIn("npm exec -- tsc --project tsconfig.json --noEmit", gate_script)
        self.assertIn("scripts/test_openclaw_memory_palace_installer.py", gate_script)
        self.assertIn("scripts/test_openclaw_command_new_e2e.py", gate_script)
        self.assertIn("scripts/test_openclaw_memory_palace_windows_native_validation.py", gate_script)
        self.assertIn("--skip-phase45", gate_script)
        self.assertIn("openclaw_compact_context_reflection_e2e.py", gate_script)

    def test_release_gate_forwards_extended_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            model_env = Path(tmp_dir) / "model.env"
            model_env.write_text("WRITE_GUARD_LLM_API_KEY=sk-test\n", encoding="utf-8")
            args = argparse.Namespace(
                report="/tmp/release.md",
                skip_backend_tests=True,
                skip_plugin_tests=True,
                enable_windows_native_validation=False,
                skip_onboarding_apply_validate=True,
                skip_frontend=True,
                skip_frontend_e2e=True,
                skip_profile_smoke=True,
                skip_review_smoke=True,
                enable_current_host_strict_ui=True,
                skip_current_host_strict_ui=False,
                current_host_ui_profile="c",
                current_host_ui_url="http://127.0.0.1:48231/#token=test",
                profile_modes="local",
                review_smoke_modes="local",
                profile_smoke_model_env=str(model_env),
                checkpoint_dir="/tmp/checkpoint",
                resume=True,
                legacy_bash_gate=False,
                skip_python_matrix=False,
                skip_phase45=False,
                phase45_profiles="c,d",
            )

            with mock.patch.object(wrapper, "run_process", return_value=0) as run_process:
                exit_code = wrapper.command_release_gate(args)

        self.assertEqual(exit_code, 0)
        run_process.assert_called_once_with(
            [
                sys.executable,
                str(wrapper.RELEASE_GATE_RUNNER_SCRIPT),
                "--report",
                "/tmp/release.md",
                "--skip-backend-tests",
                "--skip-plugin-tests",
                "--skip-onboarding-apply-validate",
                "--skip-frontend-tests",
                "--skip-frontend-e2e",
                "--skip-profile-smoke",
                "--skip-review-smoke",
                "--enable-current-host-strict-ui",
                "--current-host-ui-profile",
                "c",
                "--current-host-ui-url",
                "http://127.0.0.1:48231/#token=test",
                "--profile-smoke-modes",
                "local",
                "--phase45-profiles",
                "c,d",
                "--review-smoke-modes",
                "local",
                "--profile-smoke-model-env",
                str(model_env),
                "--checkpoint-dir",
                "/tmp/checkpoint",
                "--resume",
            ]
        )

    def test_release_gate_rejects_missing_profile_smoke_model_env_early(self) -> None:
        args = argparse.Namespace(
            report="/tmp/release.md",
            skip_backend_tests=False,
            skip_plugin_tests=False,
            enable_windows_native_validation=False,
            skip_onboarding_apply_validate=False,
            skip_frontend=False,
            skip_frontend_e2e=False,
            skip_profile_smoke=False,
            skip_review_smoke=False,
            enable_current_host_strict_ui=False,
            skip_current_host_strict_ui=False,
            current_host_ui_profile="",
            current_host_ui_url="",
            profile_modes="local",
            review_smoke_modes="local",
            profile_smoke_model_env="/tmp/definitely-missing-model.env",
            checkpoint_dir=None,
            resume=False,
            legacy_bash_gate=False,
            skip_python_matrix=False,
            skip_phase45=False,
            phase45_profiles="c,d",
        )

        with mock.patch.object(wrapper, "print_cli_error", return_value=2) as print_cli_error, mock.patch.object(
            wrapper, "run_process"
        ) as run_process:
            exit_code = wrapper.command_release_gate(args)

        self.assertEqual(exit_code, 2)
        run_process.assert_not_called()
        print_cli_error.assert_called_once()
        self.assertIn("--profile-smoke-model-env does not exist", print_cli_error.call_args.args[0])

    def test_release_gate_can_still_use_legacy_bash_gate(self) -> None:
        args = argparse.Namespace(
            report=None,
            skip_backend_tests=False,
            skip_plugin_tests=False,
            enable_windows_native_validation=False,
            skip_onboarding_apply_validate=True,
            skip_frontend=False,
            skip_frontend_e2e=False,
            skip_profile_smoke=False,
            skip_phase45=True,
            skip_review_smoke=False,
            enable_current_host_strict_ui=True,
            skip_current_host_strict_ui=False,
            current_host_ui_profile="",
            current_host_ui_url="",
            profile_modes="local,docker",
            review_smoke_modes="local,docker",
            profile_smoke_model_env=None,
            checkpoint_dir=None,
            resume=False,
            legacy_bash_gate=True,
            skip_python_matrix=False,
            phase45_profiles="c,d",
        )

        with mock.patch.object(wrapper, "run_process", return_value=0) as run_process:
            exit_code = wrapper.command_release_gate(args)

        self.assertEqual(exit_code, 0)
        run_process.assert_called_once_with(
            [
                "bash",
                wrapper.bash_script_path(wrapper.PRE_PUBLISH_SCRIPT),
                "--release-gate",
                "--skip-onboarding-apply-validate",
                "--skip-phase45",
                "--enable-current-host-strict-ui",
                "--profile-smoke-modes",
                "local,docker",
                "--review-smoke-modes",
                "local,docker",
            ]
        )

    def test_release_gate_rejects_conflicting_current_host_flags(self) -> None:
        args = argparse.Namespace(
            report=None,
            skip_backend_tests=False,
            skip_plugin_tests=False,
            enable_windows_native_validation=False,
            skip_onboarding_apply_validate=False,
            skip_frontend=False,
            skip_frontend_e2e=False,
            skip_profile_smoke=False,
            skip_phase45=False,
            skip_review_smoke=False,
            enable_current_host_strict_ui=True,
            skip_current_host_strict_ui=True,
            current_host_ui_profile="",
            current_host_ui_url="",
            profile_modes="local",
            review_smoke_modes="local",
            profile_smoke_model_env=None,
            checkpoint_dir=None,
            resume=False,
            legacy_bash_gate=False,
            skip_python_matrix=False,
            phase45_profiles="c,d",
        )

        with mock.patch.object(wrapper, "print_cli_error", return_value=2) as print_cli_error, mock.patch.object(
            wrapper, "run_process"
        ) as run_process:
            exit_code = wrapper.command_release_gate(args)

        self.assertEqual(exit_code, 2)
        run_process.assert_not_called()
        print_cli_error.assert_called_once()
        self.assertIn(
            "Cannot combine --enable-current-host-strict-ui with --skip-current-host-strict-ui.",
            print_cli_error.call_args.args[0],
        )

    def test_release_gate_rejects_windows_native_validation_with_legacy_bash_gate(self) -> None:
        args = argparse.Namespace(
            report=None,
            skip_backend_tests=False,
            skip_plugin_tests=False,
            enable_live_benchmark=False,
            enable_windows_native_validation=True,
            skip_onboarding_apply_validate=False,
            skip_frontend=False,
            skip_frontend_e2e=False,
            skip_profile_smoke=False,
            skip_phase45=False,
            skip_review_smoke=False,
            enable_current_host_strict_ui=False,
            skip_current_host_strict_ui=False,
            current_host_ui_profile="",
            current_host_ui_url="",
            profile_modes="local",
            review_smoke_modes="local",
            profile_smoke_model_env=None,
            checkpoint_dir=None,
            resume=False,
            legacy_bash_gate=True,
            skip_python_matrix=False,
            phase45_profiles="c,d",
        )

        with mock.patch.object(wrapper, "print_cli_error", return_value=2) as print_cli_error, mock.patch.object(
            wrapper, "run_process"
        ) as run_process:
            exit_code = wrapper.command_release_gate(args)

        self.assertEqual(exit_code, 2)
        run_process.assert_not_called()
        print_cli_error.assert_called_once()
        self.assertIn(
            "legacy-bash-gate does not support --enable-windows-native-validation",
            print_cli_error.call_args.args[0],
        )

    def test_release_gate_wrapper_uses_bash_compatible_script_path_on_windows(self) -> None:
        original_name = wrapper.os.name
        wrapper.os.name = "nt"
        with mock.patch.object(wrapper.shutil, "which", return_value=None):
            try:
                rendered = wrapper.bash_script_path(Path(r"C:\Users\demo\repo\scripts\pre_publish_check.sh"))
            finally:
                wrapper.os.name = original_name

        self.assertEqual(rendered, "C:/Users/demo/repo/scripts/pre_publish_check.sh")

    def test_wrapper_release_gate_forwards_windows_native_validation_flag(self) -> None:
        args = argparse.Namespace(
            report="report.md",
            legacy_bash_gate=False,
            enable_live_benchmark=False,
            enable_windows_native_validation=True,
            skip_backend_tests=False,
            skip_plugin_tests=False,
            skip_python_matrix=False,
            skip_onboarding_apply_validate=False,
            skip_frontend=False,
            skip_frontend_e2e=False,
            skip_profile_smoke=False,
            skip_phase45=False,
            skip_review_smoke=False,
            enable_current_host_strict_ui=False,
            skip_current_host_strict_ui=False,
            current_host_ui_profile="",
            current_host_ui_url="",
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
        self.assertIn("--enable-windows-native-validation", forwarded)

    def test_windows_smoke_script_uses_absolute_file_uri_generation(self) -> None:
        smoke_paths = [
            Path(__file__).resolve().parent / "openclaw_memory_palace_windows_smoke.ps1",
            Path(__file__).resolve().parents[1]
            / "extensions"
            / "memory-palace"
            / "release"
            / "scripts"
            / "openclaw_memory_palace_windows_smoke.ps1",
        ]
        for smoke_path in smoke_paths:
            smoke_script = smoke_path.read_text(encoding="utf-8")
            self.assertIn('([System.Uri]::new($resolved)).AbsoluteUri', smoke_script)
            self.assertNotIn('return "file:/$normalized"', smoke_script)

    def test_migrate_command_forwards_runtime_flags(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            setup_root="/tmp/runtime",
            env_file="/tmp/runtime/runtime.env",
            database_url=None,
            migrations_dir="/tmp/migrations",
            lock_file="/tmp/migrate.lock",
            lock_timeout_sec=45.0,
            dry_run=True,
            json=True,
        )

        with mock.patch.object(
            wrapper.installer,
            "perform_migrate",
            return_value={
                "ok": True,
                "summary": "Migration dry-run found no pending versions.",
                "env_file": "/tmp/runtime/runtime.env",
                "database_file": "/tmp/memory.db",
                "runtime_python": "/tmp/runtime/bin/python",
                "current_versions": ["0001"],
                "applied_versions": [],
                "warnings": [],
                "actions": [],
                "next_steps": [],
            },
        ) as perform_migrate, mock.patch("builtins.print"):
            exit_code = wrapper.command_migrate(args)

        self.assertEqual(exit_code, 0)
        perform_migrate.assert_called_once_with(
            config="/tmp/openclaw.json",
            setup_root_value="/tmp/runtime",
            env_file_value="/tmp/runtime/runtime.env",
            database_url=None,
            migrations_dir_value="/tmp/migrations",
            lock_file_value="/tmp/migrate.lock",
            lock_timeout_seconds=45.0,
            dry_run=True,
        )

    def test_upgrade_command_forwards_to_upgrade_handler(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            setup_root="/tmp/runtime",
            env_file="/tmp/runtime/runtime.env",
            strict_profile=True,
            dry_run=True,
            json=True,
        )

        with mock.patch.object(
            wrapper.installer,
            "perform_upgrade",
            return_value={
                "ok": True,
                "summary": "Upgrade dry-run completed.",
                "config_path": "/tmp/openclaw.json",
                "env_file": "/tmp/runtime/runtime.env",
                "mode": "basic",
                "requested_profile": "b",
                "effective_profile": "b",
                "transport": "stdio",
                "migrate": {"applied_versions": []},
                "warnings": [],
                "actions": [],
                "next_steps": [],
            },
        ) as perform_upgrade, mock.patch("builtins.print"):
            exit_code = wrapper.command_upgrade(args)

        self.assertEqual(exit_code, 0)
        perform_upgrade.assert_called_once_with(
            config="/tmp/openclaw.json",
            setup_root_value="/tmp/runtime",
            env_file_value="/tmp/runtime/runtime.env",
            strict_profile=True,
            dry_run=True,
        )

    def test_install_forwards_all_supported_flags_to_installer_script(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            transport="sse",
            sse_url="http://127.0.0.1:8010/sse",
            api_key_env="MCP_API_KEY",
            database_url=None,
            timeout_ms=20000,
            connect_retries=2,
            connect_backoff_ms=500,
            no_activate=True,
            dry_run=True,
            print_config_path=False,
            json=True,
        )

        with mock.patch.object(wrapper, "run_process", return_value=0) as run_process:
            exit_code = wrapper.command_install(args)

        self.assertEqual(exit_code, 0)
        run_process.assert_called_once_with(
            [
                sys.executable,
                str(wrapper.INSTALLER_SCRIPT),
                "--config",
                "/tmp/openclaw.json",
                "--transport",
                "sse",
                "--sse-url",
                "http://127.0.0.1:8010/sse",
                "--api-key-env",
                "MCP_API_KEY",
                "--timeout-ms",
                "20000",
                "--connect-retries",
                "2",
                "--connect-backoff-ms",
                "500",
                "--no-activate",
                "--dry-run",
                "--json",
            ]
        )

    def test_setup_forwards_remote_api_key_override(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            setup_root="/tmp/runtime",
            env_file=None,
            transport="sse",
            mode="full",
            profile="c",
            sse_url="https://memory.example.com/sse",
            api_key_env="MCP_API_KEY",
            timeout_ms=20000,
            connect_retries=1,
            connect_backoff_ms=250,
            database_path=None,
            mcp_api_key=None,
            allow_insecure_local=False,
            allow_generate_remote_api_key=True,
            dashboard_host="127.0.0.1",
            dashboard_port=55173,
            backend_api_host="127.0.0.1",
            backend_api_port=58000,
            reconfigure=True,
            strict_profile=False,
            no_activate=False,
            dry_run=True,
            json=True,
            embedding_api_base=None,
            embedding_api_key=None,
            embedding_model=None,
            embedding_dim=None,
            reranker_api_base=None,
            reranker_api_key=None,
            reranker_model=None,
            llm_api_base=None,
            llm_api_key=None,
            llm_model=None,
            write_guard_llm_api_base=None,
            write_guard_llm_api_key=None,
            write_guard_llm_model=None,
            compact_gist_llm_api_base=None,
            compact_gist_llm_api_key=None,
            compact_gist_llm_model=None,
            validate=False,
            openclaw_bin="openclaw",
        )

        with mock.patch.object(wrapper.installer, "perform_setup", return_value={"summary": "ok", "config_path": "/tmp/openclaw.json", "env_file": "/tmp/runtime.env", "plugin_root": "/tmp/plugin", "mode": "full", "requested_profile": "c", "effective_profile": "c", "transport": "sse", "warnings": [], "actions": [], "next_steps": []}) as perform_setup, mock.patch("builtins.print"):
            exit_code = wrapper.command_setup(args)

        self.assertEqual(exit_code, 0)
        self.assertTrue(perform_setup.call_args.kwargs["allow_generate_remote_api_key"])
        self.assertEqual(perform_setup.call_args.kwargs["dashboard_port"], 55173)
        self.assertEqual(perform_setup.call_args.kwargs["backend_api_port"], 58000)

    def test_setup_can_run_post_validation_chain(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            setup_root="/tmp/runtime",
            env_file=None,
            transport="stdio",
            mode="basic",
            profile="b",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            timeout_ms=20000,
            connect_retries=1,
            connect_backoff_ms=250,
            database_path=None,
            mcp_api_key=None,
            allow_insecure_local=False,
            allow_generate_remote_api_key=False,
            dashboard_host=None,
            dashboard_port=None,
            backend_api_host=None,
            backend_api_port=None,
            reconfigure=False,
            strict_profile=False,
            no_activate=False,
            dry_run=False,
            json=True,
            embedding_api_base=None,
            embedding_api_key=None,
            embedding_model=None,
            embedding_dim=None,
            reranker_api_base=None,
            reranker_api_key=None,
            reranker_model=None,
            llm_api_base=None,
            llm_api_key=None,
            llm_model=None,
            write_guard_llm_api_base=None,
            write_guard_llm_api_key=None,
            write_guard_llm_model=None,
            compact_gist_llm_api_base=None,
            compact_gist_llm_api_key=None,
            compact_gist_llm_model=None,
            validate=True,
            openclaw_bin="/usr/local/bin/openclaw",
        )

        with mock.patch.object(
            wrapper.installer,
            "perform_setup",
            return_value={
                "summary": "ok",
                "config_path": "/tmp/openclaw.json",
                "env_file": "/tmp/runtime.env",
                "plugin_root": "/tmp/plugin",
                "mode": "basic",
                "requested_profile": "b",
                "effective_profile": "b",
                "transport": "stdio",
                "warnings": [],
                "actions": [],
                "next_steps": [],
            },
        ), mock.patch.object(
            wrapper,
            "run_setup_validation",
            return_value={
                "ok": True,
                "failed_step": None,
                "steps": [{"name": "verify", "ok": True, "summary": "verify passed"}],
            },
        ) as run_setup_validation, mock.patch("builtins.print"):
            exit_code = wrapper.command_setup(args)

        self.assertEqual(exit_code, 0)
        run_setup_validation.assert_called_once_with(
            openclaw_bin="/usr/local/bin/openclaw",
            config_path=Path("/tmp/openclaw.json"),
            timeout_seconds=wrapper.DEFAULT_OPENCLAW_JSON_TIMEOUT_SECONDS,
        )

    def test_setup_marks_report_not_ok_when_post_validation_fails(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            setup_root="/tmp/runtime",
            env_file=None,
            transport="stdio",
            mode="basic",
            profile="b",
            sse_url=None,
            api_key_env="MCP_API_KEY",
            timeout_ms=20000,
            connect_retries=1,
            connect_backoff_ms=250,
            database_path=None,
            mcp_api_key=None,
            allow_insecure_local=False,
            allow_generate_remote_api_key=False,
            dashboard_host=None,
            dashboard_port=None,
            backend_api_host=None,
            backend_api_port=None,
            reconfigure=False,
            strict_profile=False,
            no_activate=False,
            dry_run=False,
            json=True,
            embedding_api_base=None,
            embedding_api_key=None,
            embedding_model=None,
            embedding_dim=None,
            reranker_api_base=None,
            reranker_api_key=None,
            reranker_model=None,
            llm_api_base=None,
            llm_api_key=None,
            llm_model=None,
            write_guard_llm_api_base=None,
            write_guard_llm_api_key=None,
            write_guard_llm_model=None,
            compact_gist_llm_api_base=None,
            compact_gist_llm_api_key=None,
            compact_gist_llm_model=None,
            validate=True,
            openclaw_bin="/usr/local/bin/openclaw",
        )

        with mock.patch.object(
            wrapper.installer,
            "perform_setup",
            return_value={
                "ok": True,
                "summary": "ok",
                "config_path": "/tmp/openclaw.json",
                "env_file": "/tmp/runtime.env",
                "plugin_root": "/tmp/plugin",
                "mode": "basic",
                "requested_profile": "b",
                "effective_profile": "b",
                "transport": "stdio",
                "warnings": [],
                "actions": [],
                "next_steps": [],
            },
        ), mock.patch.object(
            wrapper,
            "run_setup_validation",
            return_value={
                "ok": False,
                "failed_step": "doctor",
                "steps": [{"name": "doctor", "ok": False, "summary": "doctor failed"}],
            },
        ):
            with mock.patch("builtins.print") as print_mock:
                exit_code = wrapper.command_setup(args)

        self.assertEqual(exit_code, 1)
        payload = json.loads(print_mock.call_args.args[0])
        self.assertFalse(payload["ok"])
        self.assertFalse(payload["validation"]["ok"])

    def test_apply_profile_sh_rejects_placeholder_profile_c_template_values(self) -> None:
        if os.name == "nt":
            self.skipTest("shell script coverage is exercised on non-Windows hosts")
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "linux-profile-c.env"
            result = subprocess.run(
                [
                    "bash",
                    "scripts/apply_profile.sh",
                    "linux",
                    "c",
                    str(target),
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Profile C still contains placeholder provider values", result.stderr)

    def test_apply_profile_sh_rejects_placeholder_profile_d_template_values(self) -> None:
        if os.name == "nt":
            self.skipTest("shell script coverage is exercised on non-Windows hosts")
        with tempfile.TemporaryDirectory() as tmp_dir:
            target = Path(tmp_dir) / "linux-profile-d.env"
            result = subprocess.run(
                [
                    "bash",
                    "scripts/apply_profile.sh",
                    "linux",
                    "d",
                    str(target),
                ],
                cwd=str(Path(__file__).resolve().parents[1]),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("placeholder", (result.stderr or result.stdout).lower())

    def test_uninstall_forwards_runtime_and_file_flags(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            setup_root="/tmp/runtime",
            openclaw_bin="openclaw",
            keep_files=True,
            purge_runtime=False,
            keep_runtime=True,
            force=True,
            dry_run=True,
            json=True,
        )

        with mock.patch.object(
            wrapper.installer,
            "perform_uninstall",
            return_value={"ok": True, "summary": "removed", "config_path": "/tmp/openclaw.json", "setup_root": "/tmp/runtime"},
        ) as perform_uninstall, mock.patch("builtins.print"):
            exit_code = wrapper.command_uninstall(args)

        self.assertEqual(exit_code, 0)
        perform_uninstall.assert_called_once_with(
            config="/tmp/openclaw.json",
            setup_root_value="/tmp/runtime",
            openclaw_bin="openclaw",
            keep_files=True,
            remove_runtime=False,
            force=True,
            dry_run=True,
        )

    def test_enable_and_disable_forward_to_openclaw_plugins_subcommands(self) -> None:
        enable_args = argparse.Namespace(config="/tmp/openclaw.json", openclaw_bin="openclaw")
        disable_args = argparse.Namespace(config="/tmp/openclaw.json", openclaw_bin="openclaw")

        with mock.patch.object(
            wrapper,
            "resolve_config_path",
            return_value=Path("/tmp/openclaw.json"),
        ) as resolve_config_path, mock.patch.object(
            wrapper, "run_process", return_value=0
        ) as run_process:
            self.assertEqual(wrapper.command_enable(enable_args), 0)
            self.assertEqual(wrapper.command_disable(disable_args), 0)

        self.assertEqual(
            resolve_config_path.call_args_list[0],
            mock.call("/tmp/openclaw.json", openclaw_bin="openclaw"),
        )
        self.assertEqual(
            resolve_config_path.call_args_list[1],
            mock.call("/tmp/openclaw.json", openclaw_bin="openclaw"),
        )
        self.assertEqual(run_process.call_args_list[0].kwargs["config_path"], Path("/tmp/openclaw.json"))
        self.assertEqual(run_process.call_args_list[0].args[0], ["openclaw", "plugins", "enable", "memory-palace"])
        self.assertEqual(run_process.call_args_list[1].args[0], ["openclaw", "plugins", "disable", "memory-palace"])

    def test_setup_returns_clean_json_error_for_validation_failure(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            setup_root="/tmp/runtime",
            env_file=None,
            transport="sse",
            mode="basic",
            profile="b",
            sse_url="https://memory.example.com/sse",
            api_key_env="MCP_API_KEY",
            timeout_ms=20000,
            connect_retries=1,
            connect_backoff_ms=250,
            database_path=None,
            mcp_api_key=None,
            allow_insecure_local=False,
            allow_generate_remote_api_key=False,
            dashboard_host=None,
            dashboard_port=None,
            backend_api_host=None,
            backend_api_port=None,
            reconfigure=False,
            strict_profile=False,
            no_activate=False,
            dry_run=True,
            json=True,
            embedding_api_base=None,
            embedding_api_key=None,
            embedding_model=None,
            embedding_dim=None,
            reranker_api_base=None,
            reranker_api_key=None,
            reranker_model=None,
            llm_api_base=None,
            llm_api_key=None,
            llm_model=None,
            write_guard_llm_api_base=None,
            write_guard_llm_api_key=None,
            write_guard_llm_model=None,
            compact_gist_llm_api_base=None,
            compact_gist_llm_api_key=None,
            compact_gist_llm_model=None,
        )

        with mock.patch.object(
            wrapper.installer,
            "perform_setup",
            side_effect=ValueError("Remote/shared SSE setup requires an explicit MCP_API_KEY."),
        ), mock.patch("builtins.print") as print_mock:
            exit_code = wrapper.command_setup(args)

        self.assertEqual(exit_code, 2)
        self.assertEqual(print_mock.call_args.kwargs.get("file"), sys.stderr)
        self.assertIn("explicit MCP_API_KEY", print_mock.call_args.args[0])

    def test_dashboard_command_forwards_to_status_handler(self) -> None:
        args = argparse.Namespace(
            dashboard_command="status",
            setup_root="/tmp/runtime",
            json=True,
        )

        with mock.patch.object(
            wrapper.installer,
            "dashboard_status",
            return_value={"ok": True, "summary": "Dashboard status: running.", "dashboard": {"status": "running"}},
        ) as dashboard_status, mock.patch("builtins.print"):
            exit_code = wrapper.command_dashboard(args)

        self.assertEqual(exit_code, 0)
        dashboard_status.assert_called_once_with(setup_root_value="/tmp/runtime")

    def test_dashboard_command_start_forwards_port_overrides(self) -> None:
        args = argparse.Namespace(
            dashboard_command="start",
            setup_root="/tmp/runtime",
            dashboard_host="127.0.0.1",
            dashboard_port=55173,
            backend_api_host="127.0.0.1",
            backend_api_port=58000,
            dry_run=True,
            json=True,
        )

        with mock.patch.object(
            wrapper.installer,
            "dashboard_start",
            return_value={"ok": True, "summary": "started", "dashboard": {}, "backendApi": {}},
        ) as dashboard_start, mock.patch("builtins.print"):
            exit_code = wrapper.command_dashboard(args)

        self.assertEqual(exit_code, 0)
        dashboard_start.assert_called_once_with(
            setup_root_value="/tmp/runtime",
            dashboard_host="127.0.0.1",
            dashboard_port=55173,
            backend_api_host="127.0.0.1",
            backend_api_port=58000,
            dry_run=True,
        )

    def test_dashboard_command_returns_clean_error_when_start_requires_setup(self) -> None:
        args = argparse.Namespace(
            dashboard_command="start",
            setup_root="/tmp/runtime",
            dashboard_host=None,
            dashboard_port=None,
            backend_api_host=None,
            backend_api_port=None,
            dry_run=False,
            json=True,
        )

        with mock.patch.object(
            wrapper.installer,
            "dashboard_start",
            side_effect=RuntimeError("Bootstrap runtime env is missing."),
        ), mock.patch("builtins.print") as print_mock:
            exit_code = wrapper.command_dashboard(args)

        self.assertEqual(exit_code, 2)
        self.assertEqual(print_mock.call_args.kwargs.get("file"), sys.stderr)
        self.assertIn("Bootstrap runtime env is missing", print_mock.call_args.args[0])

    def test_dashboard_command_forwards_port_overrides_to_start_handler(self) -> None:
        args = argparse.Namespace(
            dashboard_command="start",
            setup_root="/tmp/runtime",
            dashboard_host="127.0.0.1",
            dashboard_port=55173,
            backend_api_host="127.0.0.1",
            backend_api_port=58000,
            dry_run=True,
            json=True,
        )

        with mock.patch.object(
            wrapper.installer,
            "dashboard_start",
            return_value={"ok": True, "summary": "started", "dashboard": {"status": "dry_run"}},
        ) as dashboard_start, mock.patch("builtins.print"):
            exit_code = wrapper.command_dashboard(args)

        self.assertEqual(exit_code, 0)
        dashboard_start.assert_called_once_with(
            setup_root_value="/tmp/runtime",
            dashboard_host="127.0.0.1",
            dashboard_port=55173,
            backend_api_host="127.0.0.1",
            backend_api_port=58000,
            dry_run=True,
        )

    def test_verify_forwards_config_path_into_openclaw_env(self) -> None:
        args = argparse.Namespace(config="/tmp/openclaw.json", openclaw_bin="openclaw", json=True)
        with mock.patch.object(
            wrapper,
            "resolve_config_path",
            return_value=Path("/tmp/openclaw.json"),
        ) as resolve_config_path, mock.patch.object(wrapper, "run_process", return_value=0) as run_process:
            exit_code = wrapper.command_verify(args)

        self.assertEqual(exit_code, 0)
        resolve_config_path.assert_called_once_with("/tmp/openclaw.json", openclaw_bin="openclaw")
        run_process.assert_called_once_with(
            ["openclaw", "memory-palace", "verify", "--json"],
            config_path=Path("/tmp/openclaw.json"),
        )

    def test_doctor_forwards_query_and_config_path(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            openclaw_bin="openclaw",
            query="memory palace",
            json=True,
        )
        with mock.patch.object(
            wrapper,
            "resolve_config_path",
            return_value=Path("/tmp/openclaw.json"),
        ) as resolve_config_path, mock.patch.object(wrapper, "run_process", return_value=0) as run_process:
            exit_code = wrapper.command_doctor(args)

        self.assertEqual(exit_code, 0)
        resolve_config_path.assert_called_once_with("/tmp/openclaw.json", openclaw_bin="openclaw")
        run_process.assert_called_once_with(
            ["openclaw", "memory-palace", "doctor", "--query", "memory palace", "--json"],
            config_path=Path("/tmp/openclaw.json"),
        )

    def test_smoke_forwards_read_probe_options(self) -> None:
        args = argparse.Namespace(
            config="/tmp/openclaw.json",
            openclaw_bin="openclaw",
            query="launch plan",
            path_or_uri="core://demo",
            expect_hit=True,
            json=True,
        )
        with mock.patch.object(
            wrapper,
            "resolve_config_path",
            return_value=Path("/tmp/openclaw.json"),
        ) as resolve_config_path, mock.patch.object(wrapper, "run_process", return_value=0) as run_process:
            exit_code = wrapper.command_smoke(args)

        self.assertEqual(exit_code, 0)
        resolve_config_path.assert_called_once_with("/tmp/openclaw.json", openclaw_bin="openclaw")
        run_process.assert_called_once_with(
            [
                "openclaw",
                "memory-palace",
                "smoke",
                "--query",
                "launch plan",
                "--path-or-uri",
                "core://demo",
                "--expect-hit",
                "--json",
            ],
            config_path=Path("/tmp/openclaw.json"),
        )

    def test_benchmark_forwards_visual_benchmark_flags(self) -> None:
        args = argparse.Namespace(
            profile=None,
            profiles="a,b,c,d",
            model_env="/tmp/model.env",
            case_count=200,
            case_limit=64,
            max_workers=1,
            required_coverage="raw_media_data_png,raw_media_data_jpeg",
            json_output="/tmp/benchmark.json",
            markdown_output="/tmp/benchmark.md",
        )

        with mock.patch.object(wrapper, "run_process", return_value=0) as run_process:
            exit_code = wrapper.command_benchmark(args)

        self.assertEqual(exit_code, 0)
        run_process.assert_called_once_with(
            [
                sys.executable,
                str(wrapper.VISUAL_BENCHMARK_SCRIPT),
                "--profiles",
                "a,b,c,d",
                "--model-env",
                "/tmp/model.env",
                "--case-count",
                "200",
                "--case-limit",
                "64",
                "--max-workers",
                "1",
                "--required-coverage",
                "raw_media_data_png,raw_media_data_jpeg",
                "--json-output",
                "/tmp/benchmark.json",
                "--markdown-output",
                "/tmp/benchmark.md",
            ]
        )


class OnboardingWrapperTests(unittest.TestCase):
    @staticmethod
    def _make_args(**overrides: object) -> argparse.Namespace:
        defaults: dict[str, object] = {
            "config": None,
            "setup_root": None,
            "env_file": None,
            "transport": "stdio",
            "mode": "basic",
            "profile": "b",
            "sse_url": None,
            "api_key_env": "MCP_API_KEY",
            "timeout_ms": 20_000,
            "connect_retries": 1,
            "connect_backoff_ms": 250,
            "database_path": None,
            "mcp_api_key": None,
            "allow_insecure_local": False,
            "allow_generate_remote_api_key": False,
            "reconfigure": False,
            "strict_profile": False,
            "dashboard_host": None,
            "dashboard_port": None,
            "backend_api_host": None,
            "backend_api_port": None,
            "no_activate": False,
            "dry_run": False,
            "json": True,
            "apply": False,
            "validate": False,
            "openclaw_bin": "openclaw",
            "embedding_api_base": None,
            "embedding_api_key": None,
            "embedding_model": None,
            "embedding_dim": None,
            "reranker_api_base": None,
            "reranker_api_key": None,
            "reranker_model": None,
            "llm_api_base": None,
            "llm_api_key": None,
            "llm_model": None,
            "write_guard_llm_api_base": None,
            "write_guard_llm_api_key": None,
            "write_guard_llm_model": None,
            "compact_gist_llm_api_base": None,
            "compact_gist_llm_api_key": None,
            "compact_gist_llm_model": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_command_onboarding_reports_profile_b_llm_boundary(self) -> None:
        args = self._make_args(
            profile="b",
            llm_api_base="https://llm.example/v1",
            llm_api_key="sk-test",
            llm_model="gpt-5.4",
        )
        provider_probe = {
            "requestedProfile": "b",
            "effectiveProfile": "b",
            "summaryStatus": "not_required",
            "summaryMessage": "Profile B does not require external retrieval providers.",
            "missingFields": [],
            "providers": {
                "embedding": {"status": "not_required", "detail": "Optional for Profile B."},
                "reranker": {"status": "not_required", "detail": "Optional for Profile B."},
                "llm": {
                    "status": "pass",
                    "detail": "Probe passed.",
                    "baseUrl": "https://llm.example/v1",
                    "model": "gpt-5.4",
                    "missingFields": [],
                },
            },
        }
        apply_preview = {
            "ok": True,
            "status": "ready",
            "requestedProfile": "b",
            "effectiveProfile": "b",
            "fallbackApplied": False,
            "strictProfile": False,
            "missingFields": [],
            "warnings": [],
            "currentFlags": {
                "writeGuardEnabled": True,
                "compactGistEnabled": True,
            },
        }

        with mock.patch.object(
            wrapper, "_build_apply_preview", return_value=apply_preview
        ), mock.patch.object(
            installer, "preview_provider_probe_status", return_value=provider_probe
        ), mock.patch.object(
            wrapper, "_load_existing_env_for_onboarding", return_value={}
        ), mock.patch.object(
            installer,
            "apply_setup_defaults",
            return_value=(
                {
                    "WRITE_GUARD_LLM_ENABLED": "true",
                    "COMPACT_GIST_LLM_ENABLED": "true",
                },
                "b",
                [],
                False,
                [],
            ),
        ):
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                exit_code = wrapper.command_onboarding(args)

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["requestedProfile"], "b")
        self.assertTrue(payload["llmSupport"]["currentFlags"]["writeGuardEnabled"])
        self.assertTrue(payload["llmSupport"]["currentFlags"]["compactGistEnabled"])
        self.assertEqual(payload["predictedApply"]["effectiveProfile"], "b")
        self.assertIn(
            wrapper._localized_onboarding_text(
                "Profile B 下如已配置 LLM",
                "If an LLM is configured under Profile B",
            ),
            " ".join(payload["nextSteps"]),
        )

    def test_build_apply_preview_profile_d_shared_llm_ignores_existing_placeholders(self) -> None:
        args = self._make_args(
            profile="d",
            strict_profile=True,
            embedding_api_base="https://embedding.example/v1",
            embedding_api_key="embed-key",
            embedding_model="embed-large",
            embedding_dim="1024",
            reranker_api_base="https://reranker.example/v1",
            reranker_api_key="rerank-key",
            reranker_model="rerank-large",
            llm_api_base="https://llm.example/v1/chat/completions",
            llm_api_key="llm-key",
            llm_model="gpt-5.4-mini",
        )
        existing_env = {
            "WRITE_GUARD_LLM_API_BASE": "",
            "WRITE_GUARD_LLM_API_KEY": "",
            "WRITE_GUARD_LLM_MODEL": "replace-with-your-llm-model",
            "COMPACT_GIST_LLM_API_BASE": "",
            "COMPACT_GIST_LLM_API_KEY": "",
            "COMPACT_GIST_LLM_MODEL": "replace-with-your-llm-model",
            "INTENT_LLM_API_BASE": "",
            "INTENT_LLM_API_KEY": "",
            "INTENT_LLM_MODEL": "replace-with-your-llm-model",
        }

        with mock.patch.object(wrapper, "_load_existing_env_for_onboarding", return_value=existing_env), mock.patch.object(
            wrapper.installer,
            "host_config_runtime_overrides",
            return_value={key: None for key in (
                "embedding_api_base",
                "embedding_api_key",
                "embedding_model",
                "embedding_dim",
                "reranker_api_base",
                "reranker_api_key",
                "reranker_model",
                "llm_api_base",
                "llm_api_key",
                "llm_model",
            )},
        ), mock.patch.object(
            wrapper.installer,
            "current_process_runtime_overrides",
            return_value={key: None for key in (
                "embedding_api_base",
                "embedding_api_key",
                "embedding_model",
                "embedding_dim",
                "reranker_api_base",
                "reranker_api_key",
                "reranker_model",
                "llm_api_base",
                "llm_api_key",
                "llm_model",
                "write_guard_llm_api_base",
                "write_guard_llm_api_key",
                "write_guard_llm_model",
                "compact_gist_llm_api_base",
                "compact_gist_llm_api_key",
                "compact_gist_llm_model",
            )},
        ):
            payload = wrapper._build_apply_preview(args)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["effectiveProfile"], "d")
        self.assertFalse(payload["fallbackApplied"])
        self.assertEqual(payload["missingFields"], [])
        self.assertTrue(payload["currentFlags"]["writeGuardEnabled"])
        self.assertTrue(payload["currentFlags"]["compactGistEnabled"])
        self.assertTrue(payload["currentFlags"]["intentLlmEnabled"])

    def test_command_onboarding_predicts_cd_fallback_when_probe_fails(self) -> None:
        args = self._make_args(profile="c")
        provider_probe = {
            "requestedProfile": "c",
            "effectiveProfile": "c",
            "requiresProviders": True,
            "summaryStatus": "warn",
            "summaryMessage": "The last advanced provider probe recorded one or more failures.",
            "missingFields": [],
            "providers": {
                "embedding": {
                    "status": "pass",
                    "detail": "Probe passed.",
                    "baseUrl": "https://embedding.example/v1",
                    "model": "embed-large",
                    "missingFields": [],
                    "detectedDim": "4096",
                },
                "reranker": {
                    "status": "fail",
                    "detail": "HTTP 401",
                    "baseUrl": "https://reranker.example/v1",
                    "model": "rerank-large",
                    "missingFields": [],
                },
                "llm": {
                    "status": "pass",
                    "detail": "Probe passed.",
                    "baseUrl": "https://llm.example/v1",
                    "model": "gpt-5.4",
                    "missingFields": [],
                },
            },
        }
        apply_preview = {
            "ok": True,
            "status": "ready",
            "requestedProfile": "c",
            "effectiveProfile": "c",
            "fallbackApplied": False,
            "strictProfile": False,
            "missingFields": [],
            "warnings": [],
            "currentFlags": {
                "writeGuardEnabled": False,
                "compactGistEnabled": False,
            },
        }

        with mock.patch.object(
            wrapper, "_build_apply_preview", return_value=apply_preview
        ), mock.patch.object(
            installer, "preview_provider_probe_status", return_value=provider_probe
        ), mock.patch.object(
            wrapper, "_load_existing_env_for_onboarding", return_value={}
        ), mock.patch.object(
            installer,
            "apply_setup_defaults",
            return_value=({}, "c", [], False, []),
        ):
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                exit_code = wrapper.command_onboarding(args)

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["requestedProfile"], "c")
        self.assertEqual(payload["embeddingDimension"]["detectedMaxDimension"], "4096")
        self.assertEqual(payload["predictedApply"]["status"], "fallback")
        self.assertEqual(payload["predictedApply"]["effectiveProfile"], "b")
        self.assertTrue(payload["predictedApply"]["fallbackApplied"])
        self.assertIn(
            wrapper._localized_onboarding_text(
                "这些 C/D provider 当前探活失败",
                "One or more C/D providers are currently failing health checks",
            ),
            " ".join(payload["predictedApply"]["warnings"]),
        )
        self.assertIn(
            wrapper._localized_onboarding_text(
                "先修复 reranker provider",
                "Fix the reranker provider",
            ),
            " ".join(payload["nextSteps"]),
        )

    def test_command_onboarding_keeps_profile_c_when_only_optional_llm_probe_fails(self) -> None:
        args = self._make_args(profile="c")
        provider_probe = {
            "requestedProfile": "c",
            "effectiveProfile": "c",
            "requiresProviders": True,
            "summaryStatus": "warn",
            "summaryMessage": "The last advanced provider probe recorded one or more failures.",
            "missingFields": [],
            "providers": {
                "embedding": {
                    "status": "pass",
                    "detail": "Probe passed.",
                    "baseUrl": "https://embedding.example/v1",
                    "model": "embed-large",
                    "missingFields": [],
                    "detectedDim": "4096",
                },
                "reranker": {
                    "status": "pass",
                    "detail": "Probe passed.",
                    "baseUrl": "https://reranker.example/v1",
                    "model": "rerank-large",
                    "missingFields": [],
                },
                "llm": {
                    "status": "fail",
                    "detail": "HTTP 500",
                    "baseUrl": "https://llm.example/v1",
                    "model": "gpt-5.4",
                    "missingFields": [],
                },
            },
        }
        apply_preview = {
            "ok": True,
            "status": "ready",
            "requestedProfile": "c",
            "effectiveProfile": "c",
            "fallbackApplied": False,
            "strictProfile": False,
            "missingFields": [],
            "warnings": [],
            "currentFlags": {
                "writeGuardEnabled": True,
                "compactGistEnabled": True,
            },
        }

        with mock.patch.object(
            wrapper, "_build_apply_preview", return_value=apply_preview
        ), mock.patch.object(
            installer, "preview_provider_probe_status", return_value=provider_probe
        ), mock.patch.object(
            wrapper, "_load_existing_env_for_onboarding", return_value={}
        ), mock.patch.object(
            installer,
            "apply_setup_defaults",
            return_value=({}, "c", [], False, []),
        ):
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                exit_code = wrapper.command_onboarding(args)

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["predictedApply"]["status"], "ready")
        self.assertEqual(payload["predictedApply"]["effectiveProfile"], "c")
        self.assertFalse(payload["predictedApply"]["fallbackApplied"])

    def test_command_onboarding_marks_strict_blocked_preview_as_failed(self) -> None:
        args = self._make_args(profile="d", strict_profile=True)
        provider_probe = {
            "requestedProfile": "d",
            "effectiveProfile": "d",
            "requiresProviders": True,
            "summaryStatus": "warn",
            "summaryMessage": "LLM probe failed.",
            "missingFields": [],
            "providers": {
                "embedding": {"status": "pass", "detail": "Probe passed."},
                "reranker": {"status": "pass", "detail": "Probe passed."},
                "llm": {"status": "fail", "detail": "HTTP 500"},
            },
        }
        apply_preview = {
            "ok": False,
            "status": "blocked",
            "requestedProfile": "d",
            "effectiveProfile": None,
            "fallbackApplied": False,
            "strictProfile": True,
            "missingFields": [],
            "warnings": [],
            "error": "Strict profile blocked.",
        }

        with mock.patch.object(
            wrapper, "_build_apply_preview", return_value=apply_preview
        ), mock.patch.object(
            installer, "preview_provider_probe_status", return_value=provider_probe
        ), mock.patch.object(
            wrapper, "_load_existing_env_for_onboarding", return_value={}
        ):
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                exit_code = wrapper.command_onboarding(args)

        self.assertEqual(exit_code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["predictedApply"]["status"], "blocked")

    def test_command_onboarding_blocks_apply_when_strict_probe_prediction_fails(self) -> None:
        args = self._make_args(profile="d", strict_profile=True, apply=True)
        provider_probe = {
            "requestedProfile": "d",
            "effectiveProfile": "d",
            "requiresProviders": True,
            "summaryStatus": "warn",
            "summaryMessage": "LLM probe failed.",
            "missingFields": [],
            "providers": {
                "embedding": {"status": "pass", "detail": "Probe passed."},
                "reranker": {"status": "pass", "detail": "Probe passed."},
                "llm": {"status": "fail", "detail": "HTTP 500"},
            },
        }
        apply_preview = {
            "ok": True,
            "status": "ready",
            "requestedProfile": "d",
            "effectiveProfile": "d",
            "fallbackApplied": False,
            "strictProfile": True,
            "missingFields": [],
            "warnings": [],
        }

        with mock.patch.object(
            wrapper, "_build_apply_preview", return_value=apply_preview
        ), mock.patch.object(
            installer, "preview_provider_probe_status", return_value=provider_probe
        ), mock.patch.object(
            wrapper, "_load_existing_env_for_onboarding", return_value={}
        ), mock.patch.object(
            wrapper, "perform_setup_from_namespace"
        ) as perform_setup:
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                exit_code = wrapper.command_onboarding(args)

        self.assertEqual(exit_code, 1)
        perform_setup.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["predictedApply"]["status"], "blocked")
        self.assertFalse(payload["predictedApply"]["ok"])
        self.assertIn("blocked", payload["summary"].lower())

    def test_localized_onboarding_text_switches_with_locale(self) -> None:
        with mock.patch.object(installer, "cli_language", return_value="zh"):
            self.assertEqual(
                wrapper._localized_onboarding_text("中文文案", "English copy"),
                "中文文案",
            )
        with mock.patch.object(installer, "cli_language", return_value="en"):
            self.assertEqual(
                wrapper._localized_onboarding_text("中文文案", "English copy"),
                "English copy",
            )

    def test_extract_setup_summary_maps_applied_setup_and_validation_payload(self) -> None:
        payload = {
            "appliedSetup": {
                "summary": "Profile C applied",
                "requested_profile": "c",
                "effective_profile": "d",
                "warnings": ["reranker degraded"],
                "actions": ["updated config", "wrote env"],
                "next_steps": ["restart openclaw"],
                "config_path": "/tmp/openclaw.json",
                "setup_root": "/tmp/setup-root",
                "env_file": "/tmp/runtime.env",
                "validation": {
                    "ok": True,
                    "failed_step": None,
                    "steps": [
                        {"name": "config", "ok": True, "summary": "config ok"},
                        {"name": "doctor", "ok": True, "summary": "doctor ok"},
                    ],
                },
            }
        }

        summary = onboarding_apply_validate.extract_setup_summary(payload)

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["summary"], "Profile C applied")
        self.assertEqual(summary["requestedProfile"], "c")
        self.assertEqual(summary["effectiveProfile"], "d")
        self.assertEqual(summary["warnings"], ["reranker degraded"])
        self.assertEqual(summary["actions"], ["updated config", "wrote env"])
        self.assertEqual(summary["nextSteps"], ["restart openclaw"])
        self.assertEqual(summary["configPath"], "/tmp/openclaw.json")
        self.assertEqual(summary["setupRoot"], "/tmp/setup-root")
        self.assertEqual(summary["envFile"], "/tmp/runtime.env")
        self.assertEqual(summary["payload"], payload["appliedSetup"])
        self.assertEqual(
            summary["validation"],
            {
                "ok": True,
                "failedStep": None,
                "steps": [
                    {"name": "config", "ok": True, "summary": "config ok"},
                    {"name": "doctor", "ok": True, "summary": "doctor ok"},
                ],
                "payload": payload["appliedSetup"]["validation"],
            },
        )

    def test_extract_setup_summary_defaults_invalid_nested_types(self) -> None:
        summary = onboarding_apply_validate.extract_setup_summary(
            {
                "appliedSetup": {
                    "summary": "Profile B applied",
                    "warnings": "not-a-list",
                    "actions": None,
                    "next_steps": {"step": "restart"},
                    "validation": ["not-a-dict"],
                }
            }
        )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(summary["actions"], [])
        self.assertEqual(summary["nextSteps"], [])
        self.assertEqual(
            summary["validation"],
            {"ok": False, "failedStep": None, "steps": [], "payload": {}},
        )

    def test_extract_setup_summary_requires_applied_setup_ok_flag(self) -> None:
        summary = onboarding_apply_validate.extract_setup_summary(
            {
                "appliedSetup": {
                    "ok": False,
                    "summary": "Setup returned a structured failure.",
                }
            }
        )

        self.assertFalse(summary["ok"])

    def test_provider_probe_ok_requires_pass_status(self) -> None:
        self.assertTrue(onboarding_apply_validate.provider_probe_ok({"ok": True, "summaryStatus": "pass"}))
        self.assertFalse(onboarding_apply_validate.provider_probe_ok({"ok": True, "summaryStatus": "warn"}))
        self.assertFalse(onboarding_apply_validate.provider_probe_ok({"ok": True, "summaryStatus": "fail"}))

    def test_build_markdown_renders_key_fields_validation_steps_and_errors(self) -> None:
        markdown = onboarding_apply_validate.build_markdown(
            {
                "generatedAt": "2026-04-12T00:00:00Z",
                "openclawBin": "/tmp/openclaw",
                "hostVersion": "2026.3.13",
                "workdir": "/tmp/workdir",
                "profilesLabel": "c",
                "allChecksPassed": False,
                "profiles": [
                    {
                        "profile": "c",
                        "requestedProfile": "c",
                        "effectiveProfile": "b",
                        "providerProbe": {"ok": True, "summary": "probe ok"},
                        "onboarding": {"ok": True, "summary": "onboarding ok"},
                        "setup": {"ok": True, "summary": "setup ok"},
                        "postApplyValidation": {
                            "ok": False,
                            "failedStep": "doctor",
                            "steps": [
                                {"name": "config", "ok": True, "summary": "config ok"},
                                {"name": "doctor", "ok": False, "summary": "doctor failed"},
                            ],
                        },
                        "verify": {"ok": True, "summary": "verify ok"},
                        "doctor": {"ok": False, "summary": "doctor failed"},
                        "smoke": {"ok": True, "summary": "smoke ok"},
                        "envFlags": {"SEARCH_DEFAULT_MODE": "hybrid"},
                        "artifacts": {"caseRoot": "/tmp/case-c"},
                        "allChecksPassed": False,
                    }
                ],
                "errors": [{"profile": "d", "error": "boom"}],
            }
        )

        self.assertIn("- generated_at: `2026-04-12T00:00:00Z`", markdown)
        self.assertIn("- openclaw_bin: `/tmp/openclaw`", markdown)
        self.assertIn("- all_checks_passed: `no`", markdown)
        self.assertIn("| C | B | ok | ok | ok | fail | ok | fail | ok | no |", markdown)
        self.assertIn("- requested/effective: `c -> b`", markdown)
        self.assertIn("- provider_probe: `probe ok`", markdown)
        self.assertIn("- validation_failed_step: `doctor`", markdown)
        self.assertIn("  - `config`: `ok` (config ok)", markdown)
        self.assertIn("  - `doctor`: `fail` (doctor failed)", markdown)
        self.assertIn("- verify: `ok` (verify ok)", markdown)
        self.assertIn("- doctor: `fail` (doctor failed)", markdown)
        self.assertIn("- smoke: `ok` (smoke ok)", markdown)
        self.assertIn("## Errors", markdown)
        self.assertIn("- `d`: boom", markdown)

    def test_onboarding_apply_validate_main_marks_report_passing_only_when_all_cases_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_path = Path(tmp_dir) / "report.json"
            markdown_path = Path(tmp_dir) / "report.md"
            args = argparse.Namespace(
                openclaw_bin="/custom/openclaw",
                profiles="c",
                report=str(report_path),
                markdown=str(markdown_path),
                workdir=str(Path(tmp_dir) / "workdir"),
                cleanup_case_roots=False,
            )
            stdout = io.StringIO()
            result_case = {
                "profile": "c",
                "requestedProfile": "c",
                "effectiveProfile": "c",
                "providerProbe": {"ok": True},
                "onboarding": {"ok": True},
                "setup": {"ok": True},
                "postApplyValidation": {"ok": True},
                "verify": {"ok": True},
                "doctor": {"ok": True},
                "smoke": {"ok": True},
                "allChecksPassed": True,
                "artifacts": {"caseRoot": str(Path(tmp_dir) / "case-c")},
            }

            with mock.patch.object(onboarding_apply_validate, "parse_args", return_value=args), mock.patch.object(
                onboarding_apply_validate.matrix, "resolve_openclaw_bin", return_value="/resolved/openclaw"
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "real_host_env", return_value={"HOME": tmp_dir}
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "host_version", return_value="2026.3.13"
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "load_host_config", return_value={"plugins": {}}
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "load_provider_config", return_value=object()
            ), mock.patch.object(
                onboarding_apply_validate, "utc_now_iso", return_value="2026-04-12T00:00:00Z"
            ), mock.patch.object(
                onboarding_apply_validate, "run_case", return_value=result_case
            ), mock.patch("sys.stdout", stdout):
                exit_code = onboarding_apply_validate.main()

            self.assertEqual(exit_code, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["allChecksPassed"])
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["profiles"], [result_case])
            self.assertTrue(markdown_path.is_file())
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["summary"], "Onboarding apply validate E2E passed.")
            self.assertEqual(payload["errors"], [])

    def test_onboarding_apply_validate_main_keeps_errors_and_fails_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_path = Path(tmp_dir) / "report.json"
            markdown_path = Path(tmp_dir) / "report.md"
            args = argparse.Namespace(
                openclaw_bin="/custom/openclaw",
                profiles="c,d",
                report=str(report_path),
                markdown=str(markdown_path),
                workdir=str(Path(tmp_dir) / "workdir"),
                cleanup_case_roots=False,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            passing_case = {
                "profile": "c",
                "requestedProfile": "c",
                "effectiveProfile": "c",
                "allChecksPassed": True,
                "artifacts": {"caseRoot": str(Path(tmp_dir) / "case-c")},
            }

            def fake_run_case(*, case, **kwargs):
                _ = kwargs
                if case.profile == "d":
                    raise RuntimeError("provider probe failed")
                return passing_case

            with mock.patch.object(onboarding_apply_validate, "parse_args", return_value=args), mock.patch.object(
                onboarding_apply_validate.matrix, "resolve_openclaw_bin", return_value="/resolved/openclaw"
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "real_host_env", return_value={"HOME": tmp_dir}
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "host_version", return_value="2026.3.13"
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "load_host_config", return_value={"plugins": {}}
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "load_provider_config", return_value=object()
            ), mock.patch.object(
                onboarding_apply_validate, "utc_now_iso", return_value="2026-04-12T00:00:00Z"
            ), mock.patch.object(
                onboarding_apply_validate, "run_case", side_effect=fake_run_case
            ), mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                exit_code = onboarding_apply_validate.main()

            self.assertEqual(exit_code, 1)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["allChecksPassed"])
            self.assertEqual(report["profiles"], [passing_case])
            self.assertEqual(
                report["errors"],
                [{"profile": "d", "error": "provider probe failed"}],
            )
            self.assertTrue(markdown_path.is_file())
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["summary"],
                "Onboarding apply validate E2E completed with failures.",
            )
            self.assertEqual(
                payload["errors"],
                [{"profile": "d", "error": "provider probe failed"}],
            )
            self.assertIn(
                "[onboarding-apply-validate] profile-d failed: provider probe failed",
                stderr.getvalue(),
            )

    def test_onboarding_apply_validate_main_loads_provider_settings_from_model_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            model_env = tmp_root / "models.env"
            model_env.write_text(
                "\n".join(
                    [
                        "OPENCLAW_TEST_EMBEDDING_MODEL=embed-from-file",
                        "OPENCLAW_TEST_RERANKER_MODEL=rerank-from-file",
                        "OPENCLAW_TEST_LLM_MODEL=llm-from-file",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            report_path = tmp_root / "report.json"
            markdown_path = tmp_root / "report.md"
            args = argparse.Namespace(
                openclaw_bin="/custom/openclaw",
                profiles="c",
                report=str(report_path),
                markdown=str(markdown_path),
                workdir=str(tmp_root / "workdir"),
                model_env=str(model_env),
                cleanup_case_roots=False,
            )
            captured_models: dict[str, str] = {}
            original_embedding_model = os.environ.get("OPENCLAW_TEST_EMBEDDING_MODEL")

            def fake_load_provider_config():
                captured_models["embedding"] = os.environ.get("OPENCLAW_TEST_EMBEDDING_MODEL", "")
                captured_models["reranker"] = os.environ.get("OPENCLAW_TEST_RERANKER_MODEL", "")
                captured_models["llm"] = os.environ.get("OPENCLAW_TEST_LLM_MODEL", "")
                return object()

            success_case = {
                "profile": "c",
                "requestedProfile": "c",
                "effectiveProfile": "c",
                "providerProbe": {"ok": True},
                "onboarding": {"ok": True},
                "setup": {"ok": True},
                "postApplyValidation": {"ok": True},
                "verify": {"ok": True},
                "doctor": {"ok": True},
                "smoke": {"ok": True},
                "allChecksPassed": True,
                "artifacts": {"caseRoot": str(tmp_root / "case-c")},
            }

            with mock.patch.object(onboarding_apply_validate, "parse_args", return_value=args), mock.patch.object(
                onboarding_apply_validate.matrix, "resolve_openclaw_bin", return_value="/resolved/openclaw"
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "real_host_env", return_value={"HOME": tmp_dir}
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "host_version", return_value="2026.3.13"
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "load_host_config", return_value={"plugins": {}}
            ), mock.patch.object(
                onboarding_apply_validate.matrix, "load_provider_config", side_effect=fake_load_provider_config
            ), mock.patch.object(
                onboarding_apply_validate, "utc_now_iso", return_value="2026-04-12T00:00:00Z"
            ), mock.patch.object(
                onboarding_apply_validate, "run_case", return_value=success_case
            ):
                exit_code = onboarding_apply_validate.main()

            self.assertEqual(exit_code, 0)
            self.assertEqual(captured_models["embedding"], "embed-from-file")
            self.assertEqual(captured_models["reranker"], "rerank-from-file")
            self.assertEqual(captured_models["llm"], "llm-from-file")
            self.assertEqual(
                json.loads(report_path.read_text(encoding="utf-8"))["modelEnvPath"],
                str(model_env.resolve()),
            )
            self.assertEqual(
                os.environ.get("OPENCLAW_TEST_EMBEDDING_MODEL"),
                original_embedding_model,
            )


if __name__ == "__main__":
    unittest.main()
