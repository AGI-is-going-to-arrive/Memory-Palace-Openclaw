from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def normalize_runtime_env_path(value: Optional[str]) -> str:
    return str(value or "").strip()


def runtime_env_file_exists(value: Optional[str]) -> bool:
    rendered = normalize_runtime_env_path(value)
    if not rendered:
        return False
    try:
        return Path(rendered).expanduser().is_file()
    except (OSError, ValueError):
        return False


def should_load_project_dotenv(
    dotenv_path: str,
    *,
    runtime_env_path: Optional[str],
) -> bool:
    return os.path.exists(dotenv_path) and not runtime_env_file_exists(runtime_env_path)
