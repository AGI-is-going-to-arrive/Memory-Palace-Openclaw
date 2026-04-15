from __future__ import annotations

from runtime_env import runtime_env_file_exists, should_load_project_dotenv


def test_runtime_env_file_exists_returns_false_for_missing_file(tmp_path) -> None:
    missing_path = tmp_path / "missing.env"

    assert runtime_env_file_exists(str(missing_path)) is False


def test_should_load_project_dotenv_when_runtime_env_path_is_missing(tmp_path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("OPENAI_MODEL=gpt-5.4\n", encoding="utf-8")

    assert should_load_project_dotenv(
        str(dotenv_path),
        runtime_env_path=str(tmp_path / "runtime.env"),
    )


def test_should_not_load_project_dotenv_when_runtime_env_file_exists(tmp_path) -> None:
    dotenv_path = tmp_path / ".env"
    runtime_env_path = tmp_path / "runtime.env"
    dotenv_path.write_text("OPENAI_MODEL=repo-default\n", encoding="utf-8")
    runtime_env_path.write_text("OPENAI_MODEL=runtime-value\n", encoding="utf-8")

    assert not should_load_project_dotenv(
        str(dotenv_path),
        runtime_env_path=str(runtime_env_path),
    )
