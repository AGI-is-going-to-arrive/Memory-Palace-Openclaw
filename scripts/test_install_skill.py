from __future__ import annotations

from unittest import mock

import install_skill


def test_stdio_server_invocation_uses_python_wrapper_on_windows() -> None:
    with mock.patch.object(install_skill.os, "name", "nt"), mock.patch.object(
        install_skill, "repo_python_wrapper_absolute", return_value=install_skill.Path("/tmp/backend/mcp_wrapper.py")
    ):
        command, args = install_skill._stdio_server_invocation(relative=False)

    assert command == install_skill.sys.executable
    assert len(args) == 1
    assert install_skill._normalized(args[0]).endswith("/tmp/backend/mcp_wrapper.py")


def test_stdio_server_invocation_uses_repo_shell_wrapper_on_posix() -> None:
    with mock.patch.object(install_skill.os, "name", "posix"):
        command, args = install_skill._stdio_server_invocation(relative=True)

    assert command == "bash"
    assert args == [str(install_skill.repo_wrapper_relative())]


def test_codex_server_block_points_to_python_wrapper_on_windows() -> None:
    with mock.patch.object(install_skill.os, "name", "nt"), mock.patch.object(
        install_skill, "repo_python_wrapper_absolute", return_value=install_skill.Path("/tmp/backend/mcp_wrapper.py")
    ):
        block = install_skill._codex_server_block_text()

    assert f'command = "{install_skill.sys.executable}"' in block
    assert "mcp_wrapper.py" in block
    assert 'command = "bash"' not in block


def test_ensure_wrapper_script_accepts_python_wrapper_on_windows() -> None:
    with mock.patch.object(install_skill.os, "name", "nt"), mock.patch.object(
        install_skill, "repo_wrapper_absolute", return_value=install_skill.Path("/tmp/missing.sh")
    ), mock.patch.object(
        install_skill, "repo_python_wrapper_absolute", return_value=install_skill.Path("/tmp/backend/mcp_wrapper.py")
    ), mock.patch.object(install_skill.Path, "is_file", return_value=True):
        install_skill.ensure_wrapper_script()
