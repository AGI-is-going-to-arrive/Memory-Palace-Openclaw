#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_memory_palace_profile_smoke as smoke


class ProfileSmokeTests(unittest.TestCase):
    def test_extract_path_or_uri_reads_non_empty_fields(self) -> None:
        self.assertEqual(
            smoke.extract_path_or_uri({"path": "memory-palace/core/demo.md", "uri": "core://demo"}),
            ("memory-palace/core/demo.md", "core://demo"),
        )
        self.assertEqual(smoke.extract_path_or_uri({"path": "  ", "uri": ""}), (None, None))

    def test_parse_json_stdout_accepts_prefixed_cli_logs(self) -> None:
        payload = smoke.parse_json_stdout(
            '[plugins] hook runner initialized with 1 registered hooks\n{"ok": true, "status": "loaded"}'
        )

        self.assertEqual(payload, {"ok": True, "status": "loaded"})

    def test_parse_json_stdout_accepts_trailing_runtime_logs_after_json(self) -> None:
        payload = smoke.parse_json_stdout(
            '{"ok": true, "status": "loaded"}\nHTTP Request: POST http://127.0.0.1:11434/v1/embeddings "HTTP/1.1 200 OK"'
        )

        self.assertEqual(payload, {"ok": True, "status": "loaded"})

    def test_parse_json_output_streams_uses_stderr_when_stdout_is_log_only(self) -> None:
        payload = smoke.parse_json_output_streams(
            "[plugins] hook runner initialized with 1 registered hooks\n",
            'Processing request of type CallToolRequest\n{"ok": true, "status": "pass"}',
        )

        self.assertEqual(payload, {"ok": True, "status": "pass"})

    def test_validate_local_outputs_accepts_nested_plugins_info_shape(self) -> None:
        outputs = {
            "plugins_info": {
                "plugin": {
                    "status": "loaded",
                    "toolNames": ["memory_search", "memory_get", "memory_store_visual"],
                }
            },
            "status_slot": {"memoryPlugin": {"slot": "memory-palace"}},
            "memory_status": {"status": {"ok": True}},
            "memory_verify": {"ok": True},
            "memory_doctor": {"ok": True},
            "memory_search": {
                "results": [{"path": "memory-palace/core/preference_concise.md"}],
            },
            "memory_get": {"text": "用户偏好简洁回答"},
            "memory_store_visual": {"runtime_visual_probe": "cli_store_visual_only"},
            "memory_index": {"ok": True},
            "visual_search": {"results": [{"path": "memory-palace/core/visual/demo.md"}]},
        }

        smoke.validate_local_outputs(outputs)

    def test_extract_index_command_ok_accepts_nested_wait_result(self) -> None:
        payload = {
            "transport": "stdio",
            "result": {
                "ok": True,
                "wait_result": {
                    "ok": True,
                    "job": {"status": "succeeded"},
                },
            },
        }

        self.assertTrue(smoke.extract_index_command_ok(payload))

    def test_extract_index_command_ok_accepts_succeeded_wait_job_without_flat_ok(self) -> None:
        payload = {
            "result": {
                "wait_result": {
                    "job": {"status": "succeeded"},
                },
            },
        }

        self.assertTrue(smoke.extract_index_command_ok(payload))

    def test_extract_index_command_ok_accepts_manual_rebuild_summary_without_nested_ok(self) -> None:
        payload = {
            "transport": "stdio",
            "result": {
                "requested_memories": 1,
                "indexed_chunks": 1,
                "failure_count": 0,
                "failures": [],
                "finished_at": "2026-03-18T21:44:54.695115",
            },
        }

        self.assertTrue(smoke.extract_index_command_ok(payload))

    def test_ensure_mcp_api_key_keeps_existing_value(self) -> None:
        values = {"MCP_API_KEY": "existing-key"}

        key = smoke.ensure_mcp_api_key(values, platform="docker", profile="c")

        self.assertEqual(key, "existing-key")
        self.assertEqual(values["MCP_API_KEY"], "existing-key")

    def test_ensure_mcp_api_key_generates_value_when_missing(self) -> None:
        values: dict[str, str] = {}

        key = smoke.ensure_mcp_api_key(values, platform="macos", profile="b")

        self.assertEqual(key, "smoke-macos-b-api-key")
        self.assertEqual(values["MCP_API_KEY"], "smoke-macos-b-api-key")

    @unittest.skipIf(os.name == "nt", "POSIX-only permission assertion")
    def test_write_env_file_restricts_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"

            smoke.write_env_file(env_path, {"OPENAI_API_KEY": "sk-test"})

            self.assertEqual(stat.S_IMODE(env_path.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX-only permission assertion")
    def test_build_openclaw_config_restricts_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "openclaw.json"

            smoke.build_openclaw_config(
                config_path,
                transport="stdio",
                stdio_env={"OPENAI_API_KEY": "sk-test"},
            )

            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            plugin_cfg = payload["plugins"]["entries"]["memory-palace"]["config"]
            self.assertEqual(plugin_cfg["timeoutMs"], 120000)

    def test_local_native_platform_name_matches_supported_triplet(self) -> None:
        expected = "windows" if smoke.os.name == "nt" else ("linux" if sys.platform.startswith("linux") else "macos")
        self.assertEqual(smoke.local_native_platform_name(), expected)

    def test_resolve_compatible_llm_env_normalizes_responses_alias_base(self) -> None:
        resolved = smoke.resolve_compatible_llm_env(
            {
                "LLM_RESPONSES_URL": "http://127.0.0.1:8318/v1/responses",
                "INTENT_LLM_MODEL": "gpt-5.4",
            }
        )

        self.assertEqual(resolved["api_base"], "http://127.0.0.1:8318/v1")
        self.assertEqual(resolved["model"], "gpt-5.4")

    def test_resolve_compatible_llm_env_ignores_placeholder_model_values(self) -> None:
        resolved = smoke.resolve_compatible_llm_env(
            {
                "SMART_EXTRACTION_LLM_MODEL": "replace-with-your-llm-model",
                "SMART_EXTRACTION_LLM_API_BASE": "http://<replace-with-your-host>",
                "OPENAI_BASE_URL": "http://127.0.0.1:8318/v1/chat/completions",
                "OPENAI_MODEL": "gpt-5.4-mini",
                "OPENAI_API_KEY": "sk-test",
            }
        )

        self.assertEqual(resolved["api_base"], "http://127.0.0.1:8318/v1")
        self.assertEqual(resolved["model"], "gpt-5.4-mini")
        self.assertEqual(resolved["api_key"], "sk-test")

    def test_prewarm_profile_model_backends_skips_ab_profiles(self) -> None:
        self.assertEqual(smoke.prewarm_profile_model_backends("b", {}), [])

    def test_prewarm_profile_model_backends_calls_expected_cd_endpoints(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def fake_probe(*, base_url, endpoint, payload, api_key, timeout_seconds):
            _ = api_key
            _ = timeout_seconds
            calls.append((str(base_url), endpoint, str(payload.get("model") or "")))
            return True, "ok"

        results = smoke.prewarm_profile_model_backends(
            "c",
            {
                "RETRIEVAL_EMBEDDING_API_BASE": "http://127.0.0.1:11434/v1",
                "RETRIEVAL_EMBEDDING_API_KEY": "ollama",
                "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                "RETRIEVAL_RERANKER_API_BASE": "http://127.0.0.1:8080",
                "RETRIEVAL_RERANKER_API_KEY": "no-key",
                "RETRIEVAL_RERANKER_MODEL": "rerank-model",
                "WRITE_GUARD_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                "WRITE_GUARD_LLM_API_KEY": "sk-key",
                "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                "COMPACT_GIST_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                "COMPACT_GIST_LLM_API_KEY": "sk-key",
                "COMPACT_GIST_LLM_MODEL": "gpt-5.4",
            },
            post_probe=fake_probe,
        )

        self.assertEqual(
            calls,
            [
                ("http://127.0.0.1:11434/v1", "/embeddings", "embed-model"),
                ("http://127.0.0.1:8080", "/rerank", "rerank-model"),
                ("http://127.0.0.1:8317/v1", "/chat/completions", "gpt-5.4"),
                ("http://127.0.0.1:8317/v1", "/chat/completions", "gpt-5.4"),
            ],
        )
        self.assertEqual([item["status"] for item in results], ["pass", "pass", "pass", "pass"])

    def test_prewarm_profile_model_backends_retries_transient_embedding_failure_once(self) -> None:
        calls: list[tuple[str, str, str]] = []
        embedding_attempts = 0

        def fake_probe(*, base_url, endpoint, payload, api_key, timeout_seconds):
            nonlocal embedding_attempts
            _ = api_key
            _ = timeout_seconds
            calls.append((str(base_url), endpoint, str(payload.get("model") or "")))
            if endpoint == "/embeddings":
                embedding_attempts += 1
                if embedding_attempts == 1:
                    return False, "The read operation timed out"
            return True, "ok"

        with mock.patch.object(smoke.time, "sleep", return_value=None) as sleep_mock:
            results = smoke.prewarm_profile_model_backends(
                "d",
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": "http://127.0.0.1:11434/v1",
                    "RETRIEVAL_EMBEDDING_API_KEY": "ollama",
                    "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                    "RETRIEVAL_RERANKER_API_BASE": "http://127.0.0.1:8080",
                    "RETRIEVAL_RERANKER_API_KEY": "no-key",
                    "RETRIEVAL_RERANKER_MODEL": "rerank-model",
                    "WRITE_GUARD_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                    "WRITE_GUARD_LLM_API_KEY": "sk-key",
                    "WRITE_GUARD_LLM_MODEL": "gpt-5.4",
                    "COMPACT_GIST_LLM_API_BASE": "http://127.0.0.1:8317/v1",
                    "COMPACT_GIST_LLM_API_KEY": "sk-key",
                    "COMPACT_GIST_LLM_MODEL": "gpt-5.4",
                },
                post_probe=fake_probe,
                retry_delay_seconds=0.0,
            )

        self.assertEqual(embedding_attempts, 2)
        self.assertEqual(sleep_mock.call_count, 1)
        self.assertEqual(
            calls,
            [
                ("http://127.0.0.1:11434/v1", "/embeddings", "embed-model"),
                ("http://127.0.0.1:11434/v1", "/embeddings", "embed-model"),
                ("http://127.0.0.1:8080", "/rerank", "rerank-model"),
                ("http://127.0.0.1:8317/v1", "/chat/completions", "gpt-5.4"),
                ("http://127.0.0.1:8317/v1", "/chat/completions", "gpt-5.4"),
            ],
        )
        self.assertEqual([item["status"] for item in results], ["pass", "pass", "pass", "pass"])

    def test_should_apply_local_embedding_fallback_only_for_cd_transient_embedding_failures(self) -> None:
        self.assertTrue(
            smoke.should_apply_local_embedding_fallback(
                "d",
                [{"component": "embedding", "status": "fail", "detail": "The read operation timed out"}],
            )
        )
        self.assertFalse(
            smoke.should_apply_local_embedding_fallback(
                "b",
                [{"component": "embedding", "status": "fail", "detail": "The read operation timed out"}],
            )
        )
        self.assertFalse(
            smoke.should_apply_local_embedding_fallback(
                "d",
                [{"component": "embedding", "status": "fail", "detail": "http 403"}],
            )
        )

    def test_apply_local_embedding_fallback_uses_local_ollama_and_adapts_for_docker(self) -> None:
        with mock.patch.object(
            smoke,
            "ensure_local_ollama_embedding_alias",
            return_value="qwen3-embedding:8b-q8_0-ctx8192",
        ), mock.patch.object(
            smoke,
            "probe_embedding_dimension",
            return_value=4096,
        ) as probe_mock:
            local_payload = smoke.apply_local_embedding_fallback(
                {"RETRIEVAL_EMBEDDING_DIM": "1024"},
                platform="macos",
                target_dim="1024",
            )
            docker_payload = smoke.apply_local_embedding_fallback(
                {"RETRIEVAL_EMBEDDING_DIM": "1024"},
                platform="docker",
                target_dim="1024",
            )

        self.assertEqual(local_payload["RETRIEVAL_EMBEDDING_API_BASE"], "http://127.0.0.1:11434/v1")
        self.assertEqual(local_payload["RETRIEVAL_EMBEDDING_API_KEY"], "ollama")
        self.assertEqual(local_payload["RETRIEVAL_EMBEDDING_MODEL"], "qwen3-embedding:8b-q8_0-ctx8192")
        self.assertEqual(local_payload["RETRIEVAL_EMBEDDING_DIM"], "4096")
        self.assertEqual(docker_payload["RETRIEVAL_EMBEDDING_API_BASE"], "http://host.docker.internal:11434/v1")
        self.assertEqual(probe_mock.call_count, 2)

    def test_seed_local_memory_passes_runtime_env_to_subprocess(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(cmd: list[str], *, env=None, timeout: int = 0, **_kwargs):
            captured["cmd"] = cmd
            captured["env"] = dict(env or {})
            captured["timeout"] = timeout
            return CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(smoke, "run", side_effect=fake_run):
            smoke.seed_local_memory(
                "sqlite+aiosqlite:////tmp/demo.db",
                env_values={
                    "RETRIEVAL_EMBEDDING_DIM": "1024",
                    "RETRIEVAL_EMBEDDING_BACKEND": "api",
                },
            )

        env = captured["env"]
        self.assertEqual(env["DATABASE_URL"], "sqlite+aiosqlite:////tmp/demo.db")
        self.assertEqual(env["RETRIEVAL_EMBEDDING_DIM"], "1024")
        self.assertEqual(env["RETRIEVAL_EMBEDDING_BACKEND"], "api")

    def test_ensure_successful_prewarm_results_raises_on_failure(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "reranker prewarm failed: HTTP 403"):
            smoke.ensure_successful_prewarm_results(
                [
                    {
                        "component": "embedding",
                        "status": "pass",
                        "detail": "ok",
                    },
                    {
                        "component": "reranker",
                        "status": "fail",
                        "detail": "HTTP 403",
                    },
                ]
            )

    def test_is_transient_prewarm_failure_matches_timeout_and_5xx(self) -> None:
        self.assertTrue(smoke.is_transient_prewarm_failure("The read operation timed out"))
        self.assertTrue(smoke.is_transient_prewarm_failure("http 503: service unavailable"))
        self.assertFalse(smoke.is_transient_prewarm_failure("http 403"))

    def test_write_env_file_persists_generated_mcp_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            values: dict[str, str] = {}

            smoke.ensure_mcp_api_key(values, platform="docker", profile="d")
            smoke.write_env_file(env_path, values)

            loaded = smoke.load_env_file(env_path)

        self.assertEqual(loaded["MCP_API_KEY"], "smoke-docker-d-api-key")

    def test_load_env_file_accepts_utf8_model_env_with_non_ascii_comments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text(
                "# 中文注释\nRETRIEVAL_EMBEDDING_MODEL=embed-model\n",
                encoding="utf-8",
            )

            loaded = smoke.load_env_file(env_path)

        self.assertEqual(loaded["RETRIEVAL_EMBEDDING_MODEL"], "embed-model")

    def test_load_env_file_strips_wrapping_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text(
                'OPENAI_MODEL="gpt-5.4"\nOPENAI_BASE_URL=\'https://example.com/v1\'\n',
                encoding="utf-8",
            )

            loaded = smoke.load_env_file(env_path)

        self.assertEqual(loaded["OPENAI_MODEL"], "gpt-5.4")
        self.assertEqual(loaded["OPENAI_BASE_URL"], "https://example.com/v1")

    def test_load_env_file_accepts_export_prefixed_lines_and_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text(
                "\ufeffexport RETRIEVAL_EMBEDDING_MODEL=qwen3-embedding:8b-q8_0\n"
                "export WRITE_GUARD_LLM_MODEL='gpt-5.4'\n",
                encoding="utf-8",
            )

            loaded = smoke.load_env_file(env_path)

        self.assertEqual(loaded["RETRIEVAL_EMBEDDING_MODEL"], "qwen3-embedding:8b-q8_0")
        self.assertEqual(loaded["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")

    def test_default_openclaw_command_uses_node_entrypoint_for_windows_cmd_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            npm_root = Path(tmp_dir)
            cmd_path = npm_root / "openclaw.CMD"
            node_path = npm_root / "node.exe"
            module_path = npm_root / "node_modules" / "openclaw" / "openclaw.mjs"
            cmd_path.write_text("@echo off\n", encoding="utf-8")
            node_path.write_text("", encoding="utf-8")
            module_path.parent.mkdir(parents=True, exist_ok=True)
            module_path.write_text("export {};\n", encoding="utf-8")

            original_bin = smoke.DEFAULT_OPENCLAW_BIN
            original_os_name = smoke.os.name
            with mock.patch.object(
                smoke.shutil,
                "which",
                side_effect=lambda name: str(node_path) if name == "node" else None,
            ):
                smoke.DEFAULT_OPENCLAW_BIN = str(cmd_path)
                smoke.os.name = "nt"
                try:
                    command = smoke.default_openclaw_command()
                finally:
                    smoke.DEFAULT_OPENCLAW_BIN = original_bin
                    smoke.os.name = original_os_name

        self.assertEqual(command, [str(node_path), str(module_path)])

    def test_default_openclaw_command_does_not_fallback_to_repo_module_on_posix(self) -> None:
        original_bin = smoke.DEFAULT_OPENCLAW_BIN
        original_os_name = smoke.os.name
        with mock.patch.object(
            smoke.shutil,
            "which",
            side_effect=lambda name: "/usr/bin/node" if name == "node" else None,
        ):
            smoke.DEFAULT_OPENCLAW_BIN = "openclaw"
            smoke.os.name = "posix"
            try:
                command = smoke.default_openclaw_command()
            finally:
                smoke.DEFAULT_OPENCLAW_BIN = original_bin
                smoke.os.name = original_os_name

        self.assertEqual(command, ["openclaw"])

    def test_resolve_node_cli_executable_prefers_windows_cmd_path(self) -> None:
        with mock.patch.object(smoke.os, "name", "nt"), mock.patch.object(smoke.shutil, "which") as which_mock:
            which_mock.side_effect = lambda name: {
                "npm": r"C:\code\nodejs\npm.CMD",
                "npx": r"C:\code\nodejs\npx.CMD",
            }.get(name)

            self.assertEqual(smoke.resolve_node_cli_executable("npm"), r"C:\code\nodejs\npm.CMD")
            self.assertEqual(smoke.resolve_node_cli_executable("npx"), r"C:\code\nodejs\npx.CMD")

    def test_resolve_openclaw_command_wraps_explicit_windows_cmd_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            npm_root = Path(tmp_dir)
            cmd_path = npm_root / "openclaw.cmd"
            node_path = npm_root / "node.exe"
            module_path = npm_root / "node_modules" / "openclaw" / "openclaw.mjs"
            cmd_path.write_text("@echo off\n", encoding="utf-8")
            node_path.write_text("", encoding="utf-8")
            module_path.parent.mkdir(parents=True, exist_ok=True)
            module_path.write_text("export {};\n", encoding="utf-8")

            original_os_name = smoke.os.name
            with mock.patch.object(
                smoke.shutil,
                "which",
                side_effect=lambda name: str(node_path) if name == "node" else None,
            ):
                smoke.os.name = "nt"
                try:
                    command = smoke.resolve_openclaw_command(str(cmd_path))
                finally:
                    smoke.os.name = original_os_name

        self.assertEqual(command, [str(node_path), str(module_path)])

    def test_resolve_openclaw_command_resolves_windows_command_name_via_which(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            npm_root = Path(tmp_dir)
            cmd_path = npm_root / "openclaw.cmd"
            node_path = npm_root / "node.exe"
            module_path = npm_root / "node_modules" / "openclaw" / "openclaw.mjs"
            cmd_path.write_text("@echo off\n", encoding="utf-8")
            node_path.write_text("", encoding="utf-8")
            module_path.parent.mkdir(parents=True, exist_ok=True)
            module_path.write_text("export {};\n", encoding="utf-8")

            original_os_name = smoke.os.name
            with mock.patch.object(
                smoke.shutil,
                "which",
                side_effect=lambda name: {"openclaw": str(cmd_path), "node": str(node_path)}.get(name),
            ):
                smoke.os.name = "nt"
                try:
                    command = smoke.resolve_openclaw_command("openclaw")
                finally:
                    smoke.os.name = original_os_name

        self.assertEqual(command, [str(node_path), str(module_path)])

    def test_build_docker_visual_probe_generates_base64_data_url_and_query_token(self) -> None:
        probe = smoke.build_docker_visual_probe("c", token_seed="token-seed")

        self.assertIn("docker-visual-probe-c-token-seed", probe.token)
        self.assertEqual(probe.query, probe.token)
        self.assertTrue(probe.media_ref.startswith("data:image/png;base64,"))
        self.assertIn(probe.token, probe.summary)
        self.assertIn(probe.token, probe.ocr)
        self.assertIn("media_ref: data:image/png;sha256-", probe.expected_get_fragments[0])
        self.assertIn("data:image/png;base64,", probe.forbidden_get_fragments)

    def test_build_visual_regression_probes_covers_png_jpeg_webp_blob_and_presigned(self) -> None:
        probes = smoke.build_visual_regression_probes("d", token_seed="seed")

        self.assertEqual(len(probes), 5)
        self.assertTrue(any(probe.media_ref.startswith("data:image/png;base64,") for probe in probes))
        self.assertTrue(any(probe.media_ref.startswith("data:image/jpeg;base64,") for probe in probes))
        self.assertTrue(any(probe.media_ref.startswith("data:image/webp;base64,") for probe in probes))
        self.assertTrue(any(probe.media_ref.startswith("blob:") for probe in probes))
        self.assertTrue(any("X-Amz-Signature=" in probe.media_ref for probe in probes))
        presigned = next(probe for probe in probes if "X-Amz-Signature=" in probe.media_ref)
        self.assertGreater(len(presigned.media_ref), 512)
        self.assertIn("media_ref: sha256-", presigned.expected_get_fragments)

    def test_terminate_active_process_groups_kills_registered_groups(self) -> None:
        original_kill_process_group = smoke.kill_process_group
        killed: list[tuple[int, int]] = []
        with smoke._ACTIVE_PROCESS_GROUPS_LOCK:
            smoke._ACTIVE_PROCESS_GROUPS.clear()

        def fake_kill_process_group(pid: int, sig: int, *, force: bool | None = None) -> None:
            _ = force
            killed.append((pid, int(sig)))

        smoke.kill_process_group = fake_kill_process_group
        try:
            smoke.register_active_process_group(111)
            smoke.register_active_process_group(222)

            terminated = smoke.terminate_active_process_groups()
            forced = smoke.terminate_active_process_groups(force=True)
        finally:
            smoke.kill_process_group = original_kill_process_group
            with smoke._ACTIVE_PROCESS_GROUPS_LOCK:
                smoke._ACTIVE_PROCESS_GROUPS.clear()

        force_signal = int(getattr(smoke.signal, "SIGKILL", smoke.signal.SIGTERM))
        self.assertEqual(set(terminated), {111, 222})
        self.assertEqual(set(forced), {111, 222})
        self.assertEqual(
            set(killed),
            {
                (111, int(smoke.signal.SIGTERM)),
                (222, int(smoke.signal.SIGTERM)),
                (111, force_signal),
                (222, force_signal),
            },
        )

    def test_kill_process_group_uses_windows_process_tree_fallback(self) -> None:
        original_killpg = smoke._OS_KILLPG
        original_name = smoke.os.name
        calls: list[tuple[int, bool]] = []

        smoke._OS_KILLPG = None
        smoke.os.name = "nt"
        original_windows_kill = smoke._kill_process_tree_windows
        smoke._kill_process_tree_windows = lambda pid, *, force: calls.append((pid, force))
        try:
            smoke.kill_process_group(123, smoke.signal.SIGTERM)
            smoke.kill_process_group(123, smoke.signal.SIGTERM, force=True)
        finally:
            smoke._kill_process_tree_windows = original_windows_kill
            smoke.os.name = original_name
            smoke._OS_KILLPG = original_killpg

        self.assertEqual(calls, [(123, False), (123, True)])

    def test_handle_termination_signal_can_reenter_when_lock_is_held(self) -> None:
        original_terminate = smoke.terminate_active_process_groups
        original_sleep = smoke.time.sleep
        calls: list[bool] = []

        smoke.terminate_active_process_groups = lambda *, force=False: calls.append(force) or []
        smoke.time.sleep = lambda _seconds: None
        try:
            with self.assertRaises(SystemExit) as raised:
                with smoke._ACTIVE_PROCESS_GROUPS_LOCK:
                    smoke.handle_termination_signal(int(smoke.signal.SIGTERM), None)
        finally:
            smoke.terminate_active_process_groups = original_terminate
            smoke.time.sleep = original_sleep

        self.assertEqual(raised.exception.code, 128 + int(smoke.signal.SIGTERM))
        self.assertEqual(calls, [False, True])

    def test_validate_docker_visual_storage_requires_sanitized_content_only(self) -> None:
        probe = smoke.build_docker_visual_probe("a", token_seed="storage")
        smoke.validate_docker_visual_storage(
            {
                "memory_probe_row_found": True,
                "chunk_probe_row_found": True,
                "fragment_counts": [
                    {"fragment": fragment, "memory_hits": 0, "chunk_hits": 0}
                    for fragment in probe.forbidden_db_fragments
                ],
            },
            probe=probe,
        )

        with self.assertRaises(AssertionError):
            smoke.validate_docker_visual_storage(
                {
                    "memory_probe_row_found": True,
                    "chunk_probe_row_found": True,
                    "fragment_counts": [
                        {"fragment": probe.forbidden_db_fragments[0], "memory_hits": 1, "chunk_hits": 0},
                        *[
                            {"fragment": fragment, "memory_hits": 0, "chunk_hits": 0}
                            for fragment in probe.forbidden_db_fragments[1:]
                        ],
                    ],
                },
                probe=probe,
            )

    def test_resolve_docker_backend_container_id_retries_until_backend_container_appears(self) -> None:
        missing_container = CompletedProcess(["docker"], 0, "", "")
        found_container = CompletedProcess(["docker"], 0, "container-123\n", "")
        calls = [missing_container, found_container]

        with mock.patch.object(smoke, "run", side_effect=lambda *args, **kwargs: calls.pop(0)) as run_mock, mock.patch.object(
            smoke.time, "sleep"
        ) as sleep_mock:
            container_id = smoke._resolve_docker_backend_container_id(
                env={},
                compose_project_name="memory-palace-smoke-a-demo",
                attempts=3,
                retry_delay_seconds=0.1,
            )

        self.assertEqual(container_id, "container-123")
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.1)

    def test_run_docker_backend_exec_retries_transient_backend_not_running(self) -> None:
        missing_container = CompletedProcess(["docker"], 0, "", "")
        transient_exec = CompletedProcess(
            ["docker"],
            1,
            "",
            'service "backend" is not running',
        )
        found_container = CompletedProcess(
            ["docker"],
            0,
            "container-123\n",
            "",
        )
        success = CompletedProcess(["docker"], 0, '{"ok": true}', "")
        calls = [missing_container, found_container, transient_exec, found_container, success]

        with mock.patch.object(smoke, "run", side_effect=lambda *args, **kwargs: calls.pop(0)) as run_mock, mock.patch.object(
            smoke.time, "sleep"
        ) as sleep_mock:
            result = smoke._run_docker_backend_exec(
                env={},
                compose_project_name="memory-palace-smoke-a-demo",
                command_args=["python", "-c", "print('ok')"],
                attempts=3,
                retry_delay_seconds=0.1,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(run_mock.call_count, 5)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_run_docker_backend_exec_reports_missing_backend_container_clearly(self) -> None:
        missing_container = CompletedProcess(["docker"], 0, "", "")

        with mock.patch.object(smoke, "run", return_value=missing_container) as run_mock, mock.patch.object(
            smoke.time, "sleep"
        ) as sleep_mock:
            result = smoke._run_docker_backend_exec(
                env={},
                compose_project_name="memory-palace-smoke-c-demo",
                command_args=["python", "-c", "print('ok')"],
                attempts=2,
                retry_delay_seconds=0.1,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not resolve a running backend container", result.stderr)
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(0.1)

    def test_inspect_docker_visual_storage_rejects_empty_stdout(self) -> None:
        probe = smoke.VisualRegressionProbe(
            token="demo",
            query="demo",
            media_ref="blob:https://example/demo",
            summary="docker visual probe demo",
            ocr="demo ocr",
            scene="demo scene",
            why_relevant="demo relevance",
        )

        with mock.patch.object(
            smoke,
            "_run_docker_backend_exec",
            return_value=CompletedProcess(["docker"], 0, "", ""),
        ):
            with self.assertRaisesRegex(RuntimeError, "empty stdout"):
                smoke.inspect_docker_visual_storage(
                    Path("/tmp/demo.env"),
                    {"COMPOSE_PROJECT_NAME": "memory-palace-smoke-demo"},
                    compose_project_name="memory-palace-smoke-demo",
                    probe=probe,
                )

    def test_run_openclaw_json_command_retries_transient_sqlite_lock(self) -> None:
        calls = []
        original_run = smoke.run
        original_sleep = smoke.time.sleep

        def fake_run(cmd, *, env=None, cwd=None, timeout=300):
            calls.append(list(cmd))
            if len(calls) == 1:
                return CompletedProcess(
                    cmd,
                    1,
                    stdout="database is locked",
                    stderr="sqlite3.OperationalError: database is locked",
                )
            return CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")

        smoke.run = fake_run
        smoke.time.sleep = lambda _seconds: None
        try:
            payload = smoke.run_openclaw_json_command(
                ["openclaw", "memory-palace", "doctor", "--json"],
                config_path=Path("/tmp/openclaw.json"),
                state_dir=Path("/tmp/state"),
            )
        finally:
            smoke.run = original_run
            smoke.time.sleep = original_sleep

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(calls), 2)

    def test_run_openclaw_json_command_uses_isolated_state_dir_per_invocation(self) -> None:
        original_run = smoke.run
        captured_state_dirs: list[str] = []

        def fake_run(cmd, *, env=None, cwd=None, timeout=300):
            _ = cmd
            _ = cwd
            _ = timeout
            captured_state_dirs.append(str((env or {}).get("OPENCLAW_STATE_DIR") or ""))
            return CompletedProcess(cmd, 0, stdout='{"ok": true}', stderr="")

        smoke.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                config_path = root / "openclaw.json"
                state_root = root / "state"
                first = smoke.run_openclaw_json_command(
                    ["openclaw", "memory-palace", "status", "--json"],
                    config_path=config_path,
                    state_dir=state_root,
                    max_attempts=1,
                )
                second = smoke.run_openclaw_json_command(
                    ["openclaw", "memory-palace", "search", "demo", "--json"],
                    config_path=config_path,
                    state_dir=state_root,
                    max_attempts=1,
                )
        finally:
            smoke.run = original_run

        self.assertEqual(first, {"ok": True})
        self.assertEqual(second, {"ok": True})
        self.assertEqual(len(captured_state_dirs), 2)
        self.assertNotEqual(captured_state_dirs[0], captured_state_dirs[1])
        self.assertTrue(all(Path(item).parent == state_root for item in captured_state_dirs))
        self.assertTrue(all(not Path(item).exists() for item in captured_state_dirs))

    def test_run_openclaw_json_command_cleans_isolated_state_dir_after_failure(self) -> None:
        original_run = smoke.run
        captured_state_dirs: list[str] = []

        def fake_run(cmd, *, env=None, cwd=None, timeout=300):
            _ = cwd
            _ = timeout
            captured_state_dirs.append(str((env or {}).get("OPENCLAW_STATE_DIR") or ""))
            return CompletedProcess(cmd, 1, stdout="boom", stderr="boom")

        smoke.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir)
                with self.assertRaisesRegex(RuntimeError, "openclaw command failed"):
                    smoke.run_openclaw_json_command(
                        ["openclaw", "memory-palace", "status", "--json"],
                        config_path=root / "openclaw.json",
                        state_dir=root / "state",
                        max_attempts=1,
                    )
        finally:
            smoke.run = original_run

        self.assertEqual(len(captured_state_dirs), 1)
        self.assertFalse(Path(captured_state_dirs[0]).exists())

    def test_run_openclaw_json_command_rejects_shared_config_and_state_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_config = root / ".openclaw" / "openclaw.json"
            shared_runtime_root = root / ".openclaw" / "memory-palace"
            shared_state = shared_runtime_root / "state"

            with mock.patch.object(smoke, "DEFAULT_SHARED_CONFIG_PATH", shared_config), mock.patch.object(
                smoke, "DEFAULT_SHARED_MEMORY_PALACE_ROOT", shared_runtime_root
            ):
                with self.assertRaisesRegex(RuntimeError, "shared OpenClaw runtime paths"):
                    smoke.run_openclaw_json_command(
                        ["openclaw", "memory-palace", "status", "--json"],
                        config_path=shared_config,
                        state_dir=shared_state,
                        max_attempts=1,
                    )

    def test_run_openclaw_json_command_rejects_shared_openclaw_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            shared_config = root / ".openclaw" / "openclaw.json"
            shared_runtime_root = root / ".openclaw" / "memory-palace"
            shared_state_root = root / ".openclaw" / "state"

            with mock.patch.object(smoke, "DEFAULT_SHARED_CONFIG_PATH", shared_config), mock.patch.object(
                smoke, "DEFAULT_SHARED_OPENCLAW_ROOT", root / ".openclaw"
            ), mock.patch.object(
                smoke, "DEFAULT_SHARED_MEMORY_PALACE_ROOT", shared_runtime_root
            ):
                with self.assertRaisesRegex(RuntimeError, "shared OpenClaw runtime paths"):
                    smoke.run_openclaw_json_command(
                        ["openclaw", "memory-palace", "status", "--json"],
                        config_path=root / "isolated-openclaw.json",
                        state_dir=shared_state_root,
                        max_attempts=1,
                    )

    def test_run_openclaw_smoke_reports_failing_step_name(self) -> None:
        original_runner = smoke.run_openclaw_json_command

        def fake_run_openclaw_json_command(command, **kwargs):
            _ = kwargs
            if list(command[-3:]) == ["memory-palace", "status", "--json"]:
                raise RuntimeError("command timeout")
            return {"ok": True, "status": "loaded", "toolNames": ["memory_search", "memory_get"], "memoryPlugin": {"slot": "memory-palace"}, "results": [{"path": "memory-palace/core/preference_concise.md"}], "text": "用户偏好简洁回答", "runtime_visual_probe": "cli_store_visual_only", "result": {"ok": True}}

        smoke.run_openclaw_json_command = fake_run_openclaw_json_command
        try:
            with self.assertRaisesRegex(RuntimeError, "smoke step memory_status failed"):
                smoke.run_openclaw_smoke(
                    config_path=Path("/tmp/openclaw.json"),
                    state_dir=Path("/tmp/state"),
                    query_text="简洁回答",
                    visual_query="whiteboard",
                )
        finally:
            smoke.run_openclaw_json_command = original_runner

    def test_run_openclaw_smoke_uses_runtime_probe_image_uri(self) -> None:
        original_runner = smoke.run_openclaw_json_command
        captured_commands: list[list[str]] = []

        def fake_run_openclaw_json_command(command, **kwargs):
            _ = kwargs
            captured_commands.append(list(command))
            return {
                "ok": True,
                "status": "loaded",
                "toolNames": ["memory_search", "memory_get"],
                "memoryPlugin": {"slot": "memory-palace"},
                "results": [{"path": "memory-palace/core/preference_concise.md"}],
                "text": "用户偏好简洁回答",
                "runtime_visual_probe": "cli_store_visual_only",
                "result": {"ok": True},
            }

        smoke.run_openclaw_json_command = fake_run_openclaw_json_command
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                smoke.run_openclaw_smoke(
                    config_path=Path(tmp_dir) / "openclaw.json",
                    state_dir=Path(tmp_dir) / "state",
                    query_text="简洁回答",
                    visual_query="whiteboard",
                )
        finally:
            smoke.run_openclaw_json_command = original_runner

        store_visual_command = next(
            command
            for command in captured_commands
            if "memory-palace" in command and "store-visual" in command and "--media-ref" in command
        )
        media_ref = store_visual_command[store_visual_command.index("--media-ref") + 1]
        self.assertTrue(media_ref.startswith("file://"))

    def test_run_does_not_wait_for_grandchild_holding_inherited_stdio(self) -> None:
        started_at = time.perf_counter()
        completed = smoke.run(
            [
                sys.executable,
                "-c",
                (
                    "import subprocess, sys; "
                    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(2)']); "
                    "print('done')"
                ),
            ],
            timeout=5,
        )
        elapsed = time.perf_counter() - started_at

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.strip(), "done")
        self.assertLess(elapsed, 1.0)

    def test_prepare_subprocess_command_wraps_windows_batch_files(self) -> None:
        original_name = smoke.os.name
        smoke.os.name = "nt"
        try:
            prepared = smoke._prepare_subprocess_command(
                [
                    r"C:\Users\demo\AppData\Roaming\npm\openclaw.CMD",
                    "memory-palace",
                    "store-visual",
                    "--media-ref",
                    "https://cdn.example.local/a.png?x=1&y=2",
                ]
            )
        finally:
            smoke.os.name = original_name

        self.assertIsInstance(prepared, str)
        self.assertTrue(prepared.startswith("call "))
        self.assertIn('"https://cdn.example.local/a.png?x=1&y=2"', prepared)

    def test_load_env_file_accepts_utf8_comments_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text(
                "# 中文注释\nRETRIEVAL_EMBEDDING_MODEL=embed-model\n",
                encoding="utf-8",
            )

            payload = smoke.load_env_file(env_path)

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "embed-model")

    def test_resolve_repo_python_executable_uses_first_existing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            candidate = Path(tmp_dir) / ("python.exe" if os.name == "nt" else "python")
            candidate.write_text("", encoding="utf-8")
            original_candidates = smoke._repo_python_candidates
            smoke._repo_python_candidates = lambda: [Path(tmp_dir) / "missing-python", candidate]
            try:
                resolved = smoke.resolve_repo_python_executable()
            finally:
                smoke._repo_python_candidates = original_candidates

        self.assertEqual(resolved, str(candidate))

    def test_build_profile_env_uses_model_env_for_llm_model_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "macos",
                    "b",
                    env_path,
                    {
                        "OPENAI_MODEL": "external-llm",
                        "LLM_MODEL_NAME": "external-llm",
                        "WRITE_GUARD_LLM_MODEL": "external-guard-llm",
                        "COMPACT_GIST_LLM_MODEL": "external-gist-llm",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(payload["OPENAI_MODEL"], "external-llm")
        self.assertEqual(payload["LLM_MODEL_NAME"], "external-llm")
        self.assertEqual(payload["WRITE_GUARD_LLM_MODEL"], "external-guard-llm")
        self.assertEqual(payload["COMPACT_GIST_LLM_MODEL"], "external-gist-llm")

    def test_build_profile_env_linux_uses_local_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env("linux", "b", env_path, {})
            finally:
                smoke.run = original_run

        self.assertEqual(payload["DATABASE_URL"], smoke.sqlite_url_for_file(env_path.with_suffix(".db")))

    def test_build_profile_env_windows_uses_local_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env("windows", "b", env_path, {})
            finally:
                smoke.run = original_run

        self.assertEqual(payload["DATABASE_URL"], smoke.sqlite_url_for_file(env_path.with_suffix(".db")))

    def test_build_profile_env_rejects_shared_runtime_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_runtime_root = Path(tmp_dir) / ".openclaw" / "memory-palace"
            env_path = shared_runtime_root / "runtime.env"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text("", encoding="utf-8")

            with mock.patch.object(smoke, "DEFAULT_SHARED_MEMORY_PALACE_ROOT", shared_runtime_root), mock.patch.object(
                smoke,
                "DEFAULT_SHARED_MEMORY_PALACE_DB",
                shared_runtime_root / "data" / "memory-palace.db",
            ):
                with self.assertRaisesRegex(RuntimeError, "shared OpenClaw runtime paths"):
                    smoke.build_profile_env("linux", "b", env_path, {})

    def test_build_openclaw_config_rejects_shared_database_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            shared_runtime_root = Path(tmp_dir) / ".openclaw" / "memory-palace"
            config_path = Path(tmp_dir) / "openclaw.json"
            database_url = smoke.sqlite_url_for_file(
                shared_runtime_root / "data" / "memory-palace.db"
            )

            with mock.patch.object(smoke, "DEFAULT_SHARED_MEMORY_PALACE_ROOT", shared_runtime_root), mock.patch.object(
                smoke,
                "DEFAULT_SHARED_MEMORY_PALACE_DB",
                shared_runtime_root / "data" / "memory-palace.db",
            ):
                with self.assertRaisesRegex(RuntimeError, "shared OpenClaw runtime paths"):
                    smoke.build_openclaw_config(
                        config_path,
                        transport="stdio",
                        stdio_env={"DATABASE_URL": database_url},
                    )

    def test_sqlite_url_for_file_preserves_windows_unc_network_share_path(self) -> None:
        unc_path = Path(r"\\server\share\memory-palace\profile.db")

        self.assertEqual(
            smoke.sqlite_url_for_file(unc_path),
            "sqlite+aiosqlite://///server/share/memory-palace/profile.db",
        )


    def test_build_profile_env_rejects_profile_c_placeholder_runtime_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                with self.assertRaisesRegex(ValueError, "Profile C requires runtime model env"):
                    smoke.build_profile_env("macos", "c", env_path, {})
            finally:
                smoke.run = original_run

    def test_build_profile_env_accepts_profile_c_runtime_values_without_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "macos",
                    "c",
                    env_path,
                    {
                        "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                        "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                        "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                        "RETRIEVAL_EMBEDDING_DIM": "1024",
                        "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                        "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                        "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_BACKEND"], "api")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "https://router.local/v1")
        self.assertEqual(payload["RETRIEVAL_RERANKER_API_BASE"], "https://router.local/v1")
        self.assertEqual(payload["RETRIEVAL_RERANKER_MODEL"], "reranker-model")
        self.assertEqual(payload["WRITE_GUARD_LLM_ENABLED"], "false")
        self.assertEqual(payload["WRITE_GUARD_LLM_API_BASE"], "")
        self.assertEqual(payload["COMPACT_GIST_LLM_API_BASE"], "")

    def test_load_env_file_maps_openclaw_test_aliases_to_runtime_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "model.env"
            env_path.write_text(
                "\n".join(
                    [
                        "OPENCLAW_TEST_EMBEDDING_API_BASE=http://embed.local/v1",
                        "OPENCLAW_TEST_EMBEDDING_API_KEY=embed-key",
                        "OPENCLAW_TEST_EMBEDDING_MODEL=embed-model",
                        "OPENCLAW_TEST_EMBEDDING_DIM=1024",
                        "OPENCLAW_TEST_RERANKER_API_BASE=http://rerank.local/v1",
                        "OPENCLAW_TEST_RERANKER_API_KEY=rerank-key",
                        "OPENCLAW_TEST_RERANKER_MODEL=rerank-model",
                        "OPENCLAW_TEST_LLM_API_BASE=http://llm.local/v1",
                        "OPENCLAW_TEST_LLM_API_KEY=llm-key",
                        "OPENCLAW_TEST_LLM_MODEL=gpt-5.4-mini",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = smoke.load_env_file(env_path)

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "http://embed.local/v1")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_KEY"], "embed-key")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "embed-model")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "1024")
        self.assertEqual(payload["RETRIEVAL_RERANKER_API_BASE"], "http://rerank.local/v1")
        self.assertEqual(payload["RETRIEVAL_RERANKER_API_KEY"], "rerank-key")
        self.assertEqual(payload["RETRIEVAL_RERANKER_MODEL"], "rerank-model")
        self.assertEqual(payload["LLM_API_BASE"], "http://llm.local/v1")
        self.assertEqual(payload["LLM_API_KEY"], "llm-key")
        self.assertEqual(payload["LLM_MODEL_NAME"], "gpt-5.4-mini")

    def test_build_profile_env_accepts_openclaw_test_aliases_for_profile_d(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "macos",
                    "d",
                    env_path,
                    {
                        "OPENCLAW_TEST_EMBEDDING_API_BASE": "https://embed.local/v1/embeddings",
                        "OPENCLAW_TEST_EMBEDDING_API_KEY": "embedding-key",
                        "OPENCLAW_TEST_EMBEDDING_MODEL": "embed-model",
                        "OPENCLAW_TEST_EMBEDDING_DIM": "1024",
                        "OPENCLAW_TEST_RERANKER_API_BASE": "https://rerank.local/v1/rerank",
                        "OPENCLAW_TEST_RERANKER_API_KEY": "reranker-key",
                        "OPENCLAW_TEST_RERANKER_MODEL": "reranker-model",
                        "OPENCLAW_TEST_LLM_API_BASE": "https://llm.local/v1",
                        "OPENCLAW_TEST_LLM_API_KEY": "llm-key",
                        "OPENCLAW_TEST_LLM_MODEL": "gpt-5.4",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "https://embed.local/v1")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_KEY"], "embedding-key")
        self.assertEqual(payload["RETRIEVAL_RERANKER_API_BASE"], "https://rerank.local/v1/rerank")
        self.assertEqual(payload["RETRIEVAL_RERANKER_API_KEY"], "reranker-key")
        self.assertEqual(payload["WRITE_GUARD_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(payload["WRITE_GUARD_LLM_API_KEY"], "llm-key")
        self.assertEqual(payload["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")

    def test_build_profile_env_accepts_optional_profile_c_llm_when_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "macos",
                    "c",
                    env_path,
                    {
                        "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                        "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                        "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                        "RETRIEVAL_EMBEDDING_DIM": "1024",
                        "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                        "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                        "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                        "WRITE_GUARD_LLM_ENABLED": "true",
                        "LLM_API_BASE": "https://llm.local/v1",
                        "LLM_API_KEY": "llm-key",
                        "LLM_MODEL_NAME": "gpt-5.4",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(payload["WRITE_GUARD_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(payload["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")
        self.assertEqual(payload["COMPACT_GIST_LLM_MODEL"], "gpt-5.4")

    def test_build_profile_env_accepts_embeddings_aliases_for_profile_d(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "macos",
                    "d",
                    env_path,
                    {
                        "EMBEDDINGS_BASE_URL": "https://embed.local/v1/embeddings",
                        "EMBEDDINGS_API_KEY": "embedding-key",
                        "EMBEDDINGS_MODEL": "embed-model",
                        "RETRIEVAL_EMBEDDING_DIM": "1024",
                        "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                        "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                        "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                        "OPENAI_BASE_URL": "https://llm.local/v1",
                        "OPENAI_API_KEY": "llm-key",
                        "OPENAI_MODEL": "gpt-5.4",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "https://embed.local/v1")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_KEY"], "embedding-key")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "embed-model")
        self.assertEqual(payload["WRITE_GUARD_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(payload["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")

    def test_build_profile_env_accepts_intent_llm_aliases_for_profile_d(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "macos",
                    "d",
                    env_path,
                    {
                        "EMBEDDINGS_BASE_URL": "https://embed.local/v1/embeddings",
                        "EMBEDDINGS_API_KEY": "embedding-key",
                        "EMBEDDINGS_MODEL": "embed-model",
                        "RETRIEVAL_EMBEDDING_DIM": "1024",
                        "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                        "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                        "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                        "INTENT_LLM_API_BASE": "https://llm.local/v1",
                        "INTENT_LLM_API_KEY": "llm-key",
                        "INTENT_LLM_MODEL": "gpt-5.4",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(payload["WRITE_GUARD_LLM_API_BASE"], "https://llm.local/v1")
        self.assertEqual(payload["WRITE_GUARD_LLM_API_KEY"], "llm-key")
        self.assertEqual(payload["WRITE_GUARD_LLM_MODEL"], "gpt-5.4")
        self.assertEqual(payload["COMPACT_GIST_LLM_MODEL"], "gpt-5.4")

    def test_build_profile_env_prefers_embeddings_alias_over_hash_seed_for_profile_d(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "macos",
                    "d",
                    env_path,
                    {
                        "RETRIEVAL_EMBEDDING_BACKEND": "hash",
                        "RETRIEVAL_EMBEDDING_MODEL": "hash-v1",
                        "EMBEDDINGS_BASE_URL": "https://embed.local/v1/embeddings",
                        "EMBEDDINGS_API_KEY": "embedding-key",
                        "EMBEDDINGS_MODEL": "embed-model",
                        "RETRIEVAL_EMBEDDING_DIM": "1024",
                        "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                        "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                        "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                        "OPENAI_BASE_URL": "https://llm.local/v1",
                        "OPENAI_API_KEY": "llm-key",
                        "OPENAI_MODEL": "gpt-5.4",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_API_BASE"], "https://embed.local/v1")
        self.assertEqual(payload["RETRIEVAL_EMBEDDING_MODEL"], "embed-model")

    def test_build_profile_env_defaults_embedding_dim_for_profile_c(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "macos",
                    "c",
                    env_path,
                    {
                        "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                        "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                        "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                        "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                        "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                        "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                        "LLM_API_BASE": "https://llm.local/v1",
                        "LLM_API_KEY": "llm-key",
                        "LLM_MODEL_NAME": "gpt-5.4",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(payload["RETRIEVAL_EMBEDDING_DIM"], "1024")

    def test_build_profile_env_copies_optional_reranker_fallback_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "macos",
                    "c",
                    env_path,
                    {
                        "RETRIEVAL_EMBEDDING_API_BASE": "https://router.local/v1/embeddings",
                        "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                        "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                        "RETRIEVAL_EMBEDDING_DIM": "1024",
                        "RETRIEVAL_RERANKER_API_BASE": "https://router.local/v1",
                        "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                        "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                        "RETRIEVAL_RERANKER_FALLBACK_API_BASE": "https://fallback.local/v1",
                        "RETRIEVAL_RERANKER_FALLBACK_API_KEY": "fallback-key",
                        "RETRIEVAL_RERANKER_FALLBACK_MODEL": "fallback-model",
                        "LLM_API_BASE": "https://llm.local/v1",
                        "LLM_API_KEY": "llm-key",
                        "LLM_MODEL_NAME": "gpt-5.4",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(
            payload["RETRIEVAL_RERANKER_FALLBACK_API_BASE"], "https://fallback.local/v1"
        )
        self.assertEqual(payload["RETRIEVAL_RERANKER_FALLBACK_API_KEY"], "fallback-key")
        self.assertEqual(payload["RETRIEVAL_RERANKER_FALLBACK_MODEL"], "fallback-model")
        self.assertEqual(payload["OPENCLAW_MEMORY_PALACE_PROFILE_REQUESTED"], "c")
        self.assertEqual(payload["OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE"], "c")

    def test_run_docker_case_keeps_secrets_out_of_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            run_results = [
                CompletedProcess(["bash"], 0, stdout="", stderr=""),
                CompletedProcess(["docker"], 0, stdout="", stderr=""),
            ]

            def fake_run(*_args, **_kwargs):
                return run_results.pop(0)

            def fake_build_profile_env(platform: str, profile: str, target: Path, model_env: dict[str, str]):
                _ = platform
                _ = profile
                target.write_text("MCP_API_KEY=from-file\n", encoding="utf-8")
                return model_env

            with mock.patch.object(smoke, "ensure_docker_daemon_available"), mock.patch.object(
                smoke.tempfile, "TemporaryDirectory"
            ) as temp_dir_mock, mock.patch.object(
                smoke, "build_profile_env", side_effect=fake_build_profile_env
            ), mock.patch.object(smoke, "run", side_effect=fake_run) as run_mock, mock.patch.object(
                smoke, "find_free_port", side_effect=["18080", "3000"]
            ), mock.patch.object(
                smoke, "wait_for_http", side_effect=RuntimeError("stop-after-up")
            ):
                temp_dir_mock.return_value.__enter__.return_value = str(temp_root)
                temp_dir_mock.return_value.__exit__.return_value = False
                with self.assertRaisesRegex(RuntimeError, "stop-after-up"):
                    smoke.run_docker_case(
                        "c",
                        {
                            "RETRIEVAL_EMBEDDING_API_KEY": "embed-secret",
                            "RETRIEVAL_RERANKER_API_KEY": "rerank-secret",
                            "WRITE_GUARD_LLM_API_KEY": "llm-secret",
                        },
                        build_images=False,
                        skip_frontend_e2e=True,
                    )

        up_command = run_mock.call_args_list[0].args[0]
        self.assertIsInstance(up_command, list)
        rendered = " ".join(up_command)
        self.assertNotIn("embed-secret", rendered)
        self.assertNotIn("rerank-secret", rendered)
        self.assertNotIn("llm-secret", rendered)
        self.assertNotIn("MCP_API_KEY=", rendered)
        self.assertIn("RETRIEVAL_EMBEDDING_API_KEY", run_mock.call_args_list[0].kwargs["env"])
        self.assertIn("RETRIEVAL_RERANKER_API_KEY", run_mock.call_args_list[0].kwargs["env"])
        self.assertIn("WRITE_GUARD_LLM_API_KEY", run_mock.call_args_list[0].kwargs["env"])

    def test_build_profile_env_adapts_loopback_model_endpoints_for_docker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / "profile.env"
            env_path.write_text("", encoding="utf-8")
            original_run = smoke.run

            def fake_run(cmd, *, env=None, cwd=None, timeout=300):
                return CompletedProcess(cmd, 0, stdout="", stderr="")

            smoke.run = fake_run
            try:
                payload = smoke.build_profile_env(
                    "docker",
                    "c",
                    env_path,
                    {
                        "RETRIEVAL_EMBEDDING_API_BASE": "http://127.0.0.1:1234/v1/embeddings",
                        "RETRIEVAL_EMBEDDING_API_KEY": "embedding-key",
                        "RETRIEVAL_EMBEDDING_MODEL": "embed-model",
                        "RETRIEVAL_EMBEDDING_DIM": "1024",
                        "RETRIEVAL_RERANKER_API_BASE": "http://127.0.0.1:9999/v1",
                        "RETRIEVAL_RERANKER_API_KEY": "reranker-key",
                        "RETRIEVAL_RERANKER_MODEL": "reranker-model",
                        "RETRIEVAL_RERANKER_FALLBACK_API_BASE": "http://localhost:1234/v1",
                        "RETRIEVAL_RERANKER_FALLBACK_API_KEY": "fallback-key",
                        "RETRIEVAL_RERANKER_FALLBACK_MODEL": "fallback-model",
                        "ROUTER_API_BASE": "http://127.0.0.1:7000/v1",
                        "LLM_API_BASE": "http://127.0.0.1:8100/v1",
                        "LLM_API_KEY": "llm-key",
                        "LLM_MODEL_NAME": "gpt-5.4",
                    },
                )
            finally:
                smoke.run = original_run

        self.assertEqual(
            payload["RETRIEVAL_EMBEDDING_API_BASE"], "http://host.docker.internal:1234/v1"
        )
        self.assertEqual(
            payload["RETRIEVAL_RERANKER_API_BASE"], "http://host.docker.internal:9999/v1"
        )
        self.assertEqual(
            payload["RETRIEVAL_RERANKER_FALLBACK_API_BASE"],
            "http://host.docker.internal:1234/v1",
        )
        self.assertEqual(payload["ROUTER_API_BASE"], "http://host.docker.internal:7000/v1")
        self.assertEqual(payload["WRITE_GUARD_LLM_API_BASE"], "")

    def test_write_report_creates_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            report_path = Path(tmp_dir) / "nested" / "profile-smoke.md"

            smoke.write_report(
                report_path,
                [smoke.SmokeResult("local", "b", "PASS", "ok")],
            )

            self.assertTrue(report_path.is_file())
            self.assertIn("OpenClaw Memory Palace Profile Smoke Report", report_path.read_text(encoding="utf-8"))

if __name__ == "__main__":
    unittest.main()
