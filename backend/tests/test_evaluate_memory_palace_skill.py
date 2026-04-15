from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


def _load_skill_eval_module():
    project_root = Path(__file__).resolve().parents[2]
    script_path = project_root / "scripts" / "evaluate_memory_palace_skill.py"
    spec = importlib.util.spec_from_file_location(
        "evaluate_memory_palace_skill",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_classify_skill_answer_accepts_repo_visible_trigger_sample_path() -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()

    success, details = evaluate_memory_palace_skill.classify_skill_answer(
        '- first move: `read_memory("system://boot")`\n'
        "- noop handling: stop, inspect `guard_target_uri` / `guard_target_id`\n"
        "- trigger samples: `docs/skills/memory-palace/references/trigger-samples.md`\n"
    )

    assert success is True
    assert "trigger sample" in details


def test_smoke_codex_accepts_output_file_when_cli_times_out(
    monkeypatch,
) -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()

    monkeypatch.setattr(
        evaluate_memory_palace_skill.shutil,
        "which",
        lambda _: "/usr/bin/codex",
    )

    def _fake_runner(cmd, *, cwd, output_path, input_text=None, timeout=120):
        _ = cmd
        _ = cwd
        _ = input_text
        _ = timeout
        output_path.write_text(
            json.dumps(
                {
                    "first_move": 'read_memory("system://boot")',
                    "noop_handling": "stop and inspect guard_target_uri / guard_target_id",
                    "trigger_samples_path": "docs/skills/memory-palace/references/trigger-samples.md",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return evaluate_memory_palace_skill.CommandCapture(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_run_command_capture_until_output_file",
        _fake_runner,
    )

    result = evaluate_memory_palace_skill.smoke_codex()

    assert result.status == "PASS"
    assert result.summary == "Codex smoke 通过"


def test_run_gemini_prompt_falls_back_to_flash_preview_on_429(monkeypatch) -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()
    calls: list[str] = []

    def _fake_run_command_capture(cmd, *, cwd, input_text=None, timeout=120):
        _ = cwd
        _ = input_text
        _ = timeout
        model = cmd[2]
        calls.append(model)
        if model == evaluate_memory_palace_skill.GEMINI_TEST_MODEL:
            return evaluate_memory_palace_skill.CommandCapture(
                returncode=1,
                stdout="",
                stderr='{"error":{"code":429,"status":"RESOURCE_EXHAUSTED","message":"No capacity available for model"}}',
                timed_out=False,
                model=model,
            )
        return evaluate_memory_palace_skill.CommandCapture(
            returncode=0,
            stdout='- read_memory("system://boot")\n- stop and inspect guard_target_uri / guard_target_id\n- docs/skills/memory-palace/references/trigger-samples.md\n',
            stderr="",
            timed_out=False,
            model=model,
        )

    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "run_command_capture",
        _fake_run_command_capture,
    )

    result = evaluate_memory_palace_skill.run_gemini_prompt("prompt", timeout=30)

    assert calls == [
        evaluate_memory_palace_skill.GEMINI_TEST_MODEL,
        evaluate_memory_palace_skill.GEMINI_FALLBACK_MODEL,
    ]
    assert result.model == evaluate_memory_palace_skill.GEMINI_FALLBACK_MODEL


def test_frontmatter_data_parses_description_without_yaml_module(monkeypatch) -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()
    monkeypatch.setattr(evaluate_memory_palace_skill, "yaml", None)

    payload = evaluate_memory_palace_skill._frontmatter_data(
        evaluate_memory_palace_skill.CANONICAL_DIR / "SKILL.md"
    )

    assert isinstance(payload, dict)
    assert payload.get("name") == "memory-palace"
    assert "Memory Palace durable-memory work" in str(payload.get("description") or "")


def test_check_description_contract_passes_without_yaml_module(monkeypatch) -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()
    monkeypatch.setattr(evaluate_memory_palace_skill, "yaml", None)

    result = evaluate_memory_palace_skill.check_description_contract()

    assert result.status == "PASS"


def test_run_command_capture_until_output_file_returns_after_json_is_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()
    output_path = tmp_path / "out.json"

    class _FakeProcess:
        def __init__(self):
            self.pid = 123
            self.returncode = None
            self.calls = 0
            self.kill_calls = 0
            self.terminate_calls = 0

        def poll(self):
            return self.returncode

        def communicate(self, input=None, timeout=None):
            _ = input
            self.calls += 1
            if self.calls == 1:
                output_path.write_text('{"ok": true}', encoding="utf-8")
                raise subprocess.TimeoutExpired("codex", timeout, output="", stderr="")
            self.returncode = -15
            return ("", "")

        def kill(self):
            self.kill_calls += 1
            self.returncode = -9

        def terminate(self):
            self.terminate_calls += 1
            self.returncode = -15

    fake_process = _FakeProcess()
    terminated: list[int] = []

    monkeypatch.setattr(
        evaluate_memory_palace_skill.subprocess,
        "Popen",
        lambda *args, **kwargs: fake_process,
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill.os,
        "killpg",
        lambda pid, sig: terminated.append(pid),
        raising=False,
    )

    result = evaluate_memory_palace_skill._run_command_capture_until_output_file(
        ["codex", "exec"],
        cwd=tmp_path,
        output_path=output_path,
        timeout=5,
    )

    assert result.timed_out is False
    assert result.returncode == 0
    if os.name == "nt":
        assert fake_process.terminate_calls == 1
    else:
        assert terminated == [123]


def test_build_gemini_live_suite_case_uses_unique_suite_record_content() -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()

    payload = evaluate_memory_palace_skill._build_gemini_live_suite_case(
        "gemini_suite_example"
    )

    assert payload["note_uri"] == "notes://gemini_suite_example"
    assert payload["guard_uri"] == "notes://gemini_suite_example_dup"
    assert "gemini_suite_example_nonce" in payload["note_content"]
    assert "Gemini live validation run gemini_suite_example" in payload["note_content"]
    assert "Status: updated once." in payload["updated_content"]
    assert "user prefers concise answers" not in payload["note_content"]


def test_run_command_capture_until_chat_response_returns_after_chat_is_ready(
    monkeypatch,
    tmp_path: Path,
) -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()

    class _FakeProcess:
        def __init__(self):
            self.pid = 456
            self.returncode = None
            self.calls = 0
            self.kill_calls = 0
            self.terminate_calls = 0

        def poll(self):
            return self.returncode

        def communicate(self, input=None, timeout=None):
            _ = input
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired("gemini", timeout, output=b"", stderr=b"")
            self.returncode = -15
            return ("", "")

        def kill(self):
            self.kill_calls += 1
            self.returncode = -9

        def terminate(self):
            self.terminate_calls += 1
            self.returncode = -15

    fake_process = _FakeProcess()
    terminated: list[int] = []

    monkeypatch.setattr(
        evaluate_memory_palace_skill.subprocess,
        "Popen",
        lambda *args, **kwargs: fake_process,
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_find_latest_gemini_chat",
        lambda marker: (tmp_path / "chat.json", [{"type": "gemini", "content": "BLOCKED notes://example"}]),
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill.os,
        "killpg",
        lambda pid, sig: terminated.append(pid),
        raising=False,
    )

    result = evaluate_memory_palace_skill._run_command_capture_until_chat_response(
        ["gemini", "-p", "prompt"],
        cwd=tmp_path,
        chat_marker="example_guard",
        timeout=5,
    )

    assert result.timed_out is False
    assert result.returncode == 0
    assert result.stdout == "BLOCKED notes://example"
    if os.name == "nt":
        assert fake_process.terminate_calls == 1
    else:
        assert terminated == [456]


def test_smoke_gemini_live_suite_passes_with_suite_specific_content(
    monkeypatch,
    tmp_path: Path,
) -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()
    suite_case = evaluate_memory_palace_skill._build_gemini_live_suite_case(
        "gemini_suite_123"
    )

    monkeypatch.setattr(
        evaluate_memory_palace_skill.shutil,
        "which",
        lambda _: "/usr/bin/gemini",
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_extract_gemini_memory_palace_db_path",
        lambda: tmp_path / "memory.db",
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_find_latest_gemini_chat",
        lambda marker: None,
    )

    current_row = {"content": ""}
    current_guard_uri = {"value": ""}

    def _fake_run_gemini_prompt(prompt: str, *, timeout: int, model=None, chat_marker=None):
        _ = timeout
        _ = model
        _ = chat_marker
        if "Please save this durable note" in prompt:
            assert "suite-specific smoke record" in prompt
            assert "user prefers concise answers" not in prompt
            return evaluate_memory_palace_skill.CommandCapture(
                returncode=0,
                stdout="SUCCESS notes://gemini_suite_123",
                stderr="",
                timed_out=False,
                model=evaluate_memory_palace_skill.GEMINI_TEST_MODEL,
            )
        if "Please update" in prompt:
            current_row["content"] = suite_case["updated_content"]
            return evaluate_memory_palace_skill.CommandCapture(
                returncode=0,
                stdout="SUCCESS notes://gemini_suite_123",
                stderr="",
                timed_out=False,
                model=evaluate_memory_palace_skill.GEMINI_TEST_MODEL,
            )
        guard_uri = prompt.split(" at ", 1)[1].split(". Content:", 1)[0]
        current_guard_uri["value"] = guard_uri
        return evaluate_memory_palace_skill.CommandCapture(
            returncode=0,
            stdout="BLOCKED notes://gemini_suite_123",
            stderr="",
            timed_out=False,
            model=evaluate_memory_palace_skill.GEMINI_TEST_MODEL,
        )

    def _fake_wait_for_memory(
        _db_path: Path,
        uri: str,
        *,
        expected_substring: str | None = None,
        retries: int = 5,
    ):
        _ = retries
        if uri == "notes://gemini_suite_123":
            if expected_substring == "gemini_suite_123_nonce":
                return {
                    "content": suite_case["note_content"],
                }
            if expected_substring == "Status: updated once.":
                return {"content": current_row["content"]}
        return None

    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "run_gemini_prompt",
        _fake_run_gemini_prompt,
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_wait_for_memory",
        _fake_wait_for_memory,
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_memory_exists",
        lambda _db_path, uri: uri == current_guard_uri["value"] and False,
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill.time,
        "time",
        lambda: 123,
    )

    result = evaluate_memory_palace_skill.smoke_gemini_live_suite()

    assert result.status == "PASS"
    assert "写入/更新通过" in result.summary


def test_smoke_gemini_live_suite_accepts_prefixed_guard_tool_name(
    monkeypatch,
    tmp_path: Path,
) -> None:
    evaluate_memory_palace_skill = _load_skill_eval_module()
    suite_case = evaluate_memory_palace_skill._build_gemini_live_suite_case(
        "gemini_suite_123"
    )

    monkeypatch.setattr(
        evaluate_memory_palace_skill.shutil,
        "which",
        lambda _: "/usr/bin/gemini",
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_extract_gemini_memory_palace_db_path",
        lambda: tmp_path / "memory.db",
    )

    current_row = {"content": suite_case["updated_content"]}

    def _fake_run_gemini_prompt(prompt: str, *, timeout: int, model=None, chat_marker=None):
        _ = prompt
        _ = timeout
        _ = model
        _ = chat_marker
        return evaluate_memory_palace_skill.CommandCapture(
            returncode=0,
            stdout="SUCCESS notes://gemini_suite_123" if "Please try to save a second durable note" not in prompt else "",
            stderr="",
            timed_out=False,
            model=evaluate_memory_palace_skill.GEMINI_TEST_MODEL,
        )

    def _fake_wait_for_memory(
        _db_path: Path,
        uri: str,
        *,
        expected_substring: str | None = None,
        retries: int = 5,
    ):
        _ = retries
        if uri != "notes://gemini_suite_123":
            return None
        if expected_substring == "gemini_suite_123_nonce":
            return {"content": suite_case["note_content"]}
        if expected_substring == "Status: updated once.":
            return {"content": current_row["content"]}
        return None

    guard_chat_payload = {
        "messages": [
            {
                "toolCalls": [
                    {
                        "name": "memory-palace__create_memory",
                        "result": [
                            {
                                "functionResponse": {
                                    "response": {
                                        "output": json.dumps(
                                            {
                                                "ok": False,
                                                "guard_action": "NOOP",
                                                "guard_target_uri": "notes://gemini_suite_123",
                                            },
                                            ensure_ascii=False,
                                        )
                                    }
                                }
                            }
                        ],
                    }
                ]
            }
        ]
    }

    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "run_gemini_prompt",
        _fake_run_gemini_prompt,
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_wait_for_memory",
        _fake_wait_for_memory,
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_memory_exists",
        lambda _db_path, _uri: False,
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill,
        "_find_latest_gemini_chat",
        lambda marker: (tmp_path / "session.json", guard_chat_payload)
        if marker == "gemini_suite_123_guard"
        else None,
    )
    monkeypatch.setattr(
        evaluate_memory_palace_skill.time,
        "time",
        lambda: 123,
    )

    result = evaluate_memory_palace_skill.smoke_gemini_live_suite()

    assert result.status == "PASS"
