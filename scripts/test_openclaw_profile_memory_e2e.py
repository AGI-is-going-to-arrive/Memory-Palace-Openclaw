#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import openclaw_profile_memory_e2e as profile_e2e


class ProfileMemoryE2ETests(unittest.TestCase):
    def test_build_temp_openclaw_config_sets_isolated_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(json.dumps({}), encoding="utf-8")
            runtime_env.write_text(
                "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b\nDATABASE_URL=sqlite+aiosqlite:///tmp/demo.db\n",
                encoding="utf-8",
            )

            payload = profile_e2e.build_temp_openclaw_config(
                base_config,
                runtime_env,
                workspace_dir,
            )

        self.assertEqual(payload["agents"]["defaults"]["workspace"], str(workspace_dir))
        self.assertTrue(payload["agents"]["defaults"]["skipBootstrap"])
        self.assertFalse(payload["hooks"]["internal"]["enabled"])
        self.assertTrue(
            payload["plugins"]["entries"]["memory-palace"]["config"]["profileMemory"]["enabled"]
        )

    def test_build_temp_openclaw_config_accepts_json_like_base_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            base_config = tmp_root / "base-openclaw.json"
            runtime_env = tmp_root / "runtime.env"
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            base_config.write_text(
                """{
  gateway: {
    auth: {
      token: "demo-token",
    },
  },
  agents: {
    defaults: {},
  },
}
""",
                encoding="utf-8",
            )
            runtime_env.write_text(
                "OPENCLAW_MEMORY_PALACE_PROFILE_EFFECTIVE=b\nDATABASE_URL=sqlite+aiosqlite:///tmp/demo.db\n",
                encoding="utf-8",
            )

            payload = profile_e2e.build_temp_openclaw_config(
                base_config,
                runtime_env,
                workspace_dir,
            )

        self.assertEqual(payload["gateway"]["auth"]["token"], "demo-token")
        self.assertEqual(payload["agents"]["defaults"]["workspace"], str(workspace_dir))

    def test_decode_output_falls_back_to_gbk(self) -> None:
        text = "记得，默认流程是先代码和测试，文档最后再补"
        encoded = text.encode("gbk")

        self.assertEqual(profile_e2e._decode_output(encoded), text)

    def test_extract_latest_session_reply_reads_utf8_session_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            session_dir = tmp_root / "state" / "agents" / "main" / "sessions"
            session_dir.mkdir(parents=True, exist_ok=True)
            session_file = session_dir / "session-1.jsonl"
            session_file.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session", "id": "session-1"}, ensure_ascii=False),
                        json.dumps(
                            {
                                "type": "message",
                                "message": {
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "记得，默认流程是先代码和测试，文档最后再补。"}],
                                },
                            },
                            ensure_ascii=False,
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            payload = {
                "result": {
                    "meta": {
                        "agentMeta": {
                            "sessionId": "session-1",
                        }
                    }
                }
            }

            reply = profile_e2e.extract_latest_session_reply(tmp_root / "state", payload)

        self.assertEqual(reply, "记得，默认流程是先代码和测试，文档最后再补。")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
