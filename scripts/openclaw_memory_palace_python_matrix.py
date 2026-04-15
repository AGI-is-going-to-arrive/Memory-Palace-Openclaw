#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCRIPT = REPO_ROOT / "scripts" / "test_openclaw_memory_palace_package_install.py"
DEFAULT_VERSIONS = ("3.10", "3.11", "3.12", "3.13", "3.14")
REQUIRED_TOOLS = ("node", "npm", "openclaw", "bun")
OPTIONAL_TOOLS = ("npx",)


@dataclass(frozen=True)
class PythonResolution:
    version: str
    executable: Path


@dataclass(frozen=True)
class MatrixResult:
    version: str
    executable: str
    status: str
    elapsed_seconds: float
    detail: str


def normalize_versions(raw: str | None) -> list[str]:
    if not raw:
        return list(DEFAULT_VERSIONS)
    values: list[str] = []
    for item in str(raw).split(","):
        candidate = item.strip()
        if not candidate:
            continue
        if not candidate.startswith("3."):
            raise ValueError(f"Unsupported Python version token: {candidate}")
        values.append(candidate)
    if not values:
        raise ValueError("At least one Python version is required.")
    return values


def version_command_candidates(version: str) -> list[list[str]]:
    if os.name == "nt":
        return [
            ["py", f"-{version}"],
            [f"python{version}"],
            ["python"],
            ["python3"],
        ]
    return [
        [f"python{version}"],
        ["python3"],
        ["python"],
    ]


def resolve_python(version: str) -> PythonResolution:
    probe = (
        "import sys\n"
        "print(sys.executable)\n"
        "print(f'{sys.version_info.major}.{sys.version_info.minor}')\n"
    )
    for candidate in version_command_candidates(version):
        try:
            completed = subprocess.run(
                [*candidate, "-c", probe],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if completed.returncode != 0:
            continue
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        executable = Path(lines[0]).expanduser().resolve()
        resolved_version = lines[1]
        if resolved_version != version:
            continue
        return PythonResolution(version=version, executable=executable)
    pyenv_bin = shutil.which("pyenv")
    if pyenv_bin:
        try:
            prefix = subprocess.run(
                [pyenv_bin, "prefix", version],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            prefix = None
        if prefix and prefix.returncode == 0:
            executable = Path(prefix.stdout.strip()) / "bin" / "python"
            if executable.is_file():
                return PythonResolution(
                    version=version,
                    executable=executable.resolve(),
                )
    raise RuntimeError(f"Could not resolve a usable Python {version} interpreter.")


def collect_python_dirs(resolutions: list[PythonResolution]) -> set[Path]:
    return {item.executable.parent.resolve() for item in resolutions}


def filter_path(path_value: str, excluded_dirs: set[Path]) -> str:
    kept: list[str] = []
    seen: set[str] = set()
    for raw_entry in path_value.split(os.pathsep):
        entry = raw_entry.strip()
        if not entry:
            continue
        try:
            resolved = Path(entry).expanduser().resolve()
        except OSError:
            resolved = Path(entry).expanduser()
        if resolved in excluded_dirs:
            continue
        normalized = str(Path(entry))
        if normalized in seen:
            continue
        kept.append(entry)
        seen.add(normalized)
    return os.pathsep.join(kept)


def required_tool_paths() -> dict[str, Path]:
    resolved: dict[str, Path] = {}
    for tool in (*REQUIRED_TOOLS, *OPTIONAL_TOOLS):
        located = shutil.which(tool)
        if not located:
            if tool in REQUIRED_TOOLS:
                raise RuntimeError(f"Required tool missing from PATH: {tool}")
            continue
        resolved[tool] = Path(located).resolve()
    return resolved


def _write_posix_wrapper(path: Path, target: Path, *, strip_py_flag: bool) -> None:
    lines = [
        "#!/bin/sh",
        "set -eu",
    ]
    if strip_py_flag:
        lines.extend(
            [
                'case "${1:-}" in',
                "  -3.*) shift ;;",
                "esac",
            ]
        )
    lines.append(f'exec "{target}" "$@"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def _write_windows_wrapper(path: Path, target: Path, *, strip_py_flag: bool) -> None:
    lines = ["@echo off", "setlocal"]
    if strip_py_flag:
        lines.extend(
            [
                'set "first=%~1"',
                'if /I not "%first:~0,3%"=="-3." goto run',
                "shift",
                ":run",
            ]
        )
    else:
        lines.append(":run")
    lines.append(f'"{target}" %*')
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")


def write_wrapper(path: Path, target: Path, *, strip_py_flag: bool = False) -> None:
    if os.name == "nt":
        _write_windows_wrapper(path.with_suffix(".cmd"), target, strip_py_flag=strip_py_flag)
        return
    _write_posix_wrapper(path, target, strip_py_flag=strip_py_flag)


def build_isolated_tool_bin(
    *,
    python: PythonResolution,
    tool_paths: dict[str, Path],
) -> Path:
    root = Path(tempfile.mkdtemp(prefix=f"mp-python-matrix-{python.version.replace('.', '')}-"))
    write_wrapper(root / "python", python.executable)
    write_wrapper(root / "python3", python.executable)
    write_wrapper(root / f"python{python.version}", python.executable)
    if os.name == "nt":
        write_wrapper(root / "py", python.executable, strip_py_flag=True)
    for name, target in tool_paths.items():
        write_wrapper(root / name, target)
    return root


def run_matrix_case(
    *,
    python: PythonResolution,
    script_path: Path,
    tool_paths: dict[str, Path],
    filtered_path: str,
    skip_full_stack: bool,
) -> MatrixResult:
    isolated_bin = build_isolated_tool_bin(python=python, tool_paths=tool_paths)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join([str(isolated_bin), filtered_path]).strip(os.pathsep)
    env["OPENCLAW_PACKAGE_INSTALL_PYTHON_DIR"] = str(isolated_bin)
    if skip_full_stack:
        env["OPENCLAW_PACKAGE_INSTALL_SKIP_FULL_STACK"] = "1"
    started = time.monotonic()
    completed = subprocess.run(
        [str(python.executable), str(script_path)],
        cwd=str(REPO_ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    elapsed = time.monotonic() - started
    detail = completed.stdout.strip() or completed.stderr.strip()
    if completed.returncode == 0:
        return MatrixResult(
            version=python.version,
            executable=str(python.executable),
            status="PASS",
            elapsed_seconds=elapsed,
            detail=detail,
        )
    return MatrixResult(
        version=python.version,
        executable=str(python.executable),
        status="FAIL",
        elapsed_seconds=elapsed,
        detail=(
            f"STDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}".strip()
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the packaged OpenClaw Memory Palace install/smoke validation across a Python version matrix.",
    )
    parser.add_argument(
        "--versions",
        default=",".join(DEFAULT_VERSIONS),
        help="Comma-separated Python versions to validate, default: 3.10,3.11,3.12,3.13,3.14",
    )
    parser.add_argument(
        "--script",
        default=str(DEFAULT_SCRIPT),
        help="Validation script to run for each Python version.",
    )
    parser.add_argument(
        "--skip-full-stack",
        action="store_true",
        help="Forward OPENCLAW_PACKAGE_INSTALL_SKIP_FULL_STACK=1 for a faster matrix run.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    versions = normalize_versions(args.versions)
    script_path = Path(args.script).expanduser().resolve()
    if not script_path.is_file():
        raise SystemExit(f"Validation script not found: {script_path}")

    resolutions = [resolve_python(version) for version in versions]
    tool_paths = required_tool_paths()
    filtered_path = filter_path(
        os.environ.get("PATH", ""),
        collect_python_dirs(resolutions),
    )
    results: list[MatrixResult] = []
    for resolution in resolutions:
        print(
            f"[python-matrix] start python={resolution.version} executable={resolution.executable}",
            flush=True,
        )
        result = run_matrix_case(
            python=resolution,
            script_path=script_path,
            tool_paths=tool_paths,
            filtered_path=filtered_path,
            skip_full_stack=bool(args.skip_full_stack),
        )
        results.append(result)
        print(
            f"[python-matrix] {result.status.lower()} python={result.version} elapsed={result.elapsed_seconds:.1f}s",
            flush=True,
        )

    print("\nPython Matrix Summary")
    for result in results:
        print(
            f"- {result.version}: {result.status} ({result.elapsed_seconds:.1f}s) [{result.executable}]"
        )

    failures = [item for item in results if item.status != "PASS"]
    if failures:
        print("\nFailures")
        for failure in failures:
            print(f"## {failure.version}")
            print(failure.detail)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
