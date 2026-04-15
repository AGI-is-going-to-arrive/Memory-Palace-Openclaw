#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openclaw_compact_context_reflection_e2e as compact_reflection


class CompactContextReflectionE2ETests(unittest.TestCase):
    def test_build_temp_openclaw_config_enables_compact_reflection_probe_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                json.dumps(
                    {
                        "hooks": {"internal": {"enabled": False}},
                        "agents": {"defaults": {}},
                        "plugins": {"entries": {"memory-palace": {"config": {"stdio": {"env": {}}}}}},
                    }
                ),
                encoding="utf-8",
            )
            runtime_env.write_text("OPENAI_API_KEY=secret\n", encoding="utf-8")

            payload = compact_reflection.build_temp_openclaw_config(
                base_config, runtime_env, workspace_dir
            )

        config = payload["plugins"]["entries"]["memory-palace"]["config"]
        self.assertTrue(payload["hooks"]["internal"]["enabled"])
        self.assertEqual(config["autoRecall"]["enabled"], False)
        self.assertEqual(config["smartExtraction"]["enabled"], False)
        self.assertEqual(config["reconcile"]["enabled"], False)
        self.assertEqual(config["timeoutMs"], 120000)
        self.assertEqual(config["reflection"]["enabled"], True)
        self.assertEqual(config["reflection"]["source"], "compact_context")
        self.assertEqual(config["reflection"]["rootUri"], "core://reflection")

    def test_extract_compact_context_runtime_reads_status_payload(self) -> None:
        payload = {
            "runtimeState": {
                "lastCompactContext": {
                    "flushed": True,
                    "dataPersisted": True,
                    "uri": "core://reflection/main/2026/03/26/item",
                }
            }
        }

        extracted = compact_reflection.extract_compact_context_runtime(payload)

        self.assertEqual(
            extracted,
            {
                "flushed": True,
                "dataPersisted": True,
                "uri": "core://reflection/main/2026/03/26/item",
            },
        )

    def test_extract_flush_tracker_runtime_reads_status_payload(self) -> None:
        payload = {
            "status": {
                "runtime": {
                    "sm_lite": {
                        "flush_tracker": {
                            "flush_results_total": 3,
                            "early_flush_count": 1,
                            "last_source_hash": "hash-123",
                        }
                    }
                }
            }
        }

        extracted = compact_reflection.extract_flush_tracker_runtime(payload)

        self.assertEqual(
            extracted,
            {
                "flush_results_total": 3,
                "early_flush_count": 1,
                "last_source_hash": "hash-123",
            },
        )

    def test_reflection_uses_atomic_path_requires_hash_without_source_uri(self) -> None:
        self.assertTrue(
            compact_reflection.reflection_uses_atomic_path(
                "# Reflection Lane\n- compact_source_hash: seeded-hash\n- compact_gist_method: extractive_bullets\n"
            )
        )
        self.assertFalse(
            compact_reflection.reflection_uses_atomic_path(
                "# Reflection Lane\n- compact_source_uri: core://agent/auto_flush_1\n- compact_source_hash: seeded-hash\n"
            )
        )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
