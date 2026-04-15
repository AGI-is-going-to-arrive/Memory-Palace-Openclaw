#!/usr/bin/env python3
from __future__ import annotations

from ._constants import *

def host_platform_name(host_platform: str | None = None) -> str:
    normalized = str(host_platform or "").strip().lower()
    if normalized:
        if normalized in {"windows", "macos", "linux"}:
            return normalized
        raise ValueError(f"Unsupported host platform: {host_platform}")
    if os.name == "nt":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return "macos"


def cli_language() -> str:
    candidates = [
        os.getenv("OPENCLAW_LOCALE"),
        os.getenv("OPENCLAW_LANG"),
        os.getenv("LC_ALL"),
        os.getenv("LANGUAGE"),
        os.getenv("LANG"),
    ]
    try:
        default_locale = locale.getlocale()[0]
    except (ValueError, TypeError, AttributeError, IndexError):
        default_locale = None
    candidates.append(default_locale)
    for raw in candidates:
        value = str(raw or "").strip().lower()
        if not value:
            continue
        if value.startswith("zh"):
            return "zh"
        if value.startswith("en"):
            return "en"
    return "en"


def cli_text(key: str, **kwargs: str) -> str:
    language = cli_language()
    template = CLI_I18N_MESSAGES.get(language, {}).get(key) or CLI_I18N_MESSAGES["en"][key]
    return template.format(**kwargs)


def supports_interactive_profile_prompt(*, profile: str, dry_run: bool, json_output: bool) -> bool:
    if str(profile or "").strip().lower() not in {"c", "d"}:
        return False
    if dry_run or json_output:
        return False
    if str(os.getenv("CI") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    stdin = getattr(sys, "stdin", None)
    stdout = getattr(sys, "stdout", None)
    try:
        return bool(stdin and stdout and stdin.isatty() and stdout.isatty())
    except Exception:
        return False


@dataclass(frozen=True)
class RuntimePaths:
    layout: str
    project_root: Path
    plugin_root: Path
    backend_root: Path
    frontend_root: Path
    deploy_root: Path
    scripts_root: Path


@lru_cache(maxsize=1)
def _runtime_paths() -> RuntimePaths:
    script_path = Path(__file__).resolve()
    candidates: list[Path] = [script_path.parent, *script_path.parents]
    for candidate in candidates:
        repo_plugin = candidate / "extensions" / "memory-palace" / "openclaw.plugin.json"
        if repo_plugin.is_file():
            return RuntimePaths(
                layout="repo",
                project_root=candidate,
                plugin_root=candidate / "extensions" / "memory-palace",
                backend_root=candidate / "backend",
                frontend_root=candidate / "frontend",
                deploy_root=candidate / "deploy",
                scripts_root=candidate / "scripts",
            )

        packaged_plugin = candidate / "openclaw.plugin.json"
        packaged_backend = candidate / "release" / "backend"
        packaged_scripts = candidate / "release" / "scripts"
        if packaged_plugin.is_file() and packaged_scripts.is_dir():
            return RuntimePaths(
                layout="package",
                project_root=candidate,
                plugin_root=candidate,
                backend_root=packaged_backend,
                frontend_root=candidate / "release" / "frontend",
                deploy_root=candidate / "release" / "deploy",
                scripts_root=packaged_scripts,
            )

    fallback_root = script_path.parents[1]
    return RuntimePaths(
        layout="repo",
        project_root=fallback_root,
        plugin_root=fallback_root / "extensions" / "memory-palace",
        backend_root=fallback_root / "backend",
        frontend_root=fallback_root / "frontend",
        deploy_root=fallback_root / "deploy",
        scripts_root=fallback_root / "scripts",
    )


def project_root() -> Path:
    return _runtime_paths().project_root


def plugin_root() -> Path:
    return _runtime_paths().plugin_root


def backend_root() -> Path:
    return _runtime_paths().backend_root


def frontend_root() -> Path:
    return _runtime_paths().frontend_root


def deploy_root() -> Path:
    return _runtime_paths().deploy_root


def scripts_root() -> Path:
    return _runtime_paths().scripts_root


def stdio_wrapper() -> Path:
    return scripts_root() / "run_memory_palace_mcp_stdio.sh"


def windows_stdio_wrapper() -> Path:
    return backend_root() / "mcp_wrapper.py"


def env_example_path() -> Path:
    return project_root() / ".env.example"


def package_layout() -> str:
    return _runtime_paths().layout


def resolve_openclaw_binary(explicit: str | None = None) -> str | None:
    return explicit or first_non_blank(os.getenv("OPENCLAW_BIN"), shutil.which("openclaw"))


@lru_cache(maxsize=8)
def supports_dangerously_force_unsafe_install(openclaw_bin: str | None = None) -> bool:
    openclaw_binary = resolve_openclaw_binary(openclaw_bin)
    if not openclaw_binary:
        return False
    try:
        completed = subprocess.run(
            [openclaw_binary, "plugins", "install", "--help"],
            cwd=project_root(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=15,
        )
    except Exception:
        return False
    help_text = f"{completed.stdout}\n{completed.stderr}"
    return "--dangerously-force-unsafe-install" in help_text


def build_openclaw_plugins_install_command(
    target: str | Path,
    *,
    openclaw_bin: str | None = None,
    trusted_local_package: bool = False,
) -> list[str]:
    openclaw_binary = resolve_openclaw_binary(openclaw_bin) or "openclaw"
    command = [openclaw_binary, "plugins", "install"]
    if trusted_local_package and supports_dangerously_force_unsafe_install(openclaw_binary):
        command.append("--dangerously-force-unsafe-install")
    command.append(str(target))
    return command


def parse_openclaw_version_text(raw: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?:OpenClaw\s+)?(\d+)\.(\d+)\.(\d+)", str(raw or ""))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def detect_openclaw_version(openclaw_bin: str | None = None) -> dict[str, Any] | None:
    openclaw_binary = resolve_openclaw_binary(openclaw_bin)
    if not openclaw_binary:
        return None
    try:
        completed = subprocess.run(
            [openclaw_binary, "--version"],
            cwd=project_root(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=15,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    raw = str(completed.stdout or completed.stderr or "").strip()
    parsed = parse_openclaw_version_text(raw)
    return {
        "raw": raw,
        "parsed": parsed,
        "version": ".".join(str(part) for part in parsed) if parsed else "",
        "meets_minimum": bool(parsed and parsed >= MIN_OPENCLAW_VERSION),
    }


def resolve_plugin_install_root_hint(*, setup_root_path: Path | None = None) -> Path | None:
    hinted = first_non_blank(os.getenv("OPENCLAW_MEMORY_PALACE_PLUGIN_ROOT_HINT"))
    if not hinted:
        return None
    candidate = Path(hinted).expanduser().resolve()
    if not candidate.exists():
        return None
    if setup_root_path is not None:
        expected_root = setup_root_path.parent / "state" / "extensions" / PLUGIN_ID
        try:
            candidate.relative_to(expected_root)
        except ValueError:
            if candidate != expected_root:
                return None
    return candidate


def candidate_config_paths(*, cwd: Path | None = None, home: Path | None = None) -> list[Path]:
    current = cwd or Path.cwd()
    home_dir = home or Path.home()
    appdata = first_non_blank(os.getenv("APPDATA"))
    localappdata = first_non_blank(os.getenv("LOCALAPPDATA"))
    xdg_config_home = first_non_blank(os.getenv("XDG_CONFIG_HOME"))
    windows_candidates = []
    for root in (appdata, localappdata):
        if not root:
            continue
        base = Path(root).expanduser()
        windows_candidates.extend(
            [
                base / "OpenClaw" / "openclaw.json",
                base / "OpenClaw" / "config.json",
                base / "OpenClaw" / "settings.json",
            ]
        )
    xdg_candidates = (
        [
            Path(xdg_config_home).expanduser() / "openclaw" / "config.json",
            Path(xdg_config_home).expanduser() / "openclaw" / "settings.json",
        ]
        if xdg_config_home
        else []
    )
    return list(
        dict.fromkeys(
            [
                current / ".openclaw" / "config.json",
                current / ".openclaw" / "settings.json",
                *windows_candidates,
                *xdg_candidates,
                home_dir / ".openclaw" / "openclaw.json",
                home_dir / ".config" / "openclaw" / "config.json",
                home_dir / ".config" / "openclaw" / "settings.json",
                home_dir / ".openclaw" / "config.json",
                home_dir / ".openclaw" / "settings.json",
            ]
        )
    )


def detect_config_path_from_openclaw(openclaw_bin: str | None = None) -> Path | None:
    openclaw_binary = resolve_openclaw_binary(openclaw_bin)
    if not openclaw_binary:
        return None
    try:
        completed = subprocess.run(
            [openclaw_binary, "config", "file"],
            cwd=project_root(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=15,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    path_candidates: list[Path] = []
    for line in lines:
        candidate = line.strip().strip("'\"").replace("~", str(Path.home()), 1)
        if not candidate.lower().endswith(".json"):
            continue
        path = Path(candidate).expanduser()
        path_candidates.append(path)
        if path.exists():
            return path.resolve()
    if not path_candidates:
        return None
    return path_candidates[0].resolve()


def get_plugin_info_value(payload: Any, key: str) -> Any:
    if not isinstance(payload, dict):
        return None
    if key in payload:
        return payload.get(key)
    plugin_obj = payload.get("plugin")
    if isinstance(plugin_obj, dict):
        return plugin_obj.get(key)
    return None


def resolve_plugin_source_path(payload: Any) -> Path | None:
    for key in ("source", "rootDir", "installPath"):
        candidate = str(get_plugin_info_value(payload, key) or "").strip()
        if candidate:
            return Path(candidate).expanduser().resolve()
    return None


def resolve_plugin_install_root_from_source_path(source_path: Path) -> Path:
    if source_path.name == "dist":
        return source_path.parent
    if source_path.name in {"index.ts", "index.js"}:
        if source_path.parent.name == "dist":
            return source_path.parent.parent
        return source_path.parent
    if source_path.suffix in {".js", ".ts"}:
        return source_path.parent
    return source_path


def resolve_plugin_install_root_from_info(payload: Any) -> Path | None:
    source_path = resolve_plugin_source_path(payload)
    if source_path is None:
        return None
    return resolve_plugin_install_root_from_source_path(source_path)


def detect_installed_plugin_root(openclaw_bin: str | None = None) -> Path | None:
    openclaw_binary = resolve_openclaw_binary(openclaw_bin)
    if not openclaw_binary:
        return None
    try:
        completed = subprocess.run(
            [openclaw_binary, "plugins", "inspect", PLUGIN_ID, "--json"],
            cwd=project_root(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=20,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    try:
        payload = parse_jsonish_stdout(completed.stdout or "")
    except ValueError:
        return None
    return resolve_plugin_install_root_from_info(payload)


def parse_jsonish_stdout(stdout: str) -> Any:
    text = str(stdout or "").strip()
    if not text:
        raise ValueError("empty stdout")
    decoder = json.JSONDecoder()
    fallback: Any | None = None
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            candidate = text[index:]
            parsed, end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if candidate[end:].strip():
            if fallback is None:
                fallback = parsed
            continue
        return parsed
    if fallback is not None:
        return fallback
    raise ValueError("stdout did not contain a standalone JSON document")


def detect_config_path_with_source(
    explicit: str | None = None,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
    openclaw_bin: str | None = None,
) -> tuple[Path, str]:
    if explicit:
        return Path(explicit).expanduser().resolve(), "explicit"
    for env_name in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_CONFIG"):
        if value := str(os.getenv(env_name) or "").strip():
            return Path(value).expanduser().resolve(), f"env:{env_name}"
    candidates = candidate_config_paths(cwd=cwd, home=home)
    cwd_candidates = candidates[:2]
    home_candidates = candidates[2:]
    for candidate in cwd_candidates:
        if candidate.exists():
            return candidate.resolve(), f"detected:{candidate}"
    cli_path = detect_config_path_from_openclaw(openclaw_bin=openclaw_bin)
    if cli_path is not None:
        return cli_path, "openclaw config file"
    for candidate in home_candidates:
        if candidate.exists():
            return candidate.resolve(), f"detected:{candidate}"
    default_candidate = home_candidates[0] if home_candidates else candidates[0]
    return default_candidate.resolve(), f"default:{default_candidate}"


def detect_config_path(
    explicit: str | None = None,
    *,
    cwd: Path | None = None,
    home: Path | None = None,
    openclaw_bin: str | None = None,
) -> Path:
    return detect_config_path_with_source(
        explicit,
        cwd=cwd,
        home=home,
        openclaw_bin=openclaw_bin,
    )[0]


def _strip_json_like_comments(text: str) -> str:
    result: list[str] = []
    in_string = False
    escape = False
    in_line_comment = False
    in_block_comment = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        next_char = text[index + 1] if index + 1 < length else ""
        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                result.append(char)
            index += 1
            continue
        if in_block_comment:
            if char == "*" and next_char == "/":
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue
        if in_string:
            result.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == "/" and next_char == "/":
            in_line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            in_block_comment = True
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _strip_json_like_trailing_commas(text: str) -> str:
    result: list[str] = []
    in_string = False
    escape = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            result.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char == ",":
            lookahead = index + 1
            while lookahead < length and text[lookahead].isspace():
                lookahead += 1
            if lookahead < length and text[lookahead] in "}]":
                index += 1
                continue
        result.append(char)
        index += 1
    return "".join(result)


def _quote_json_like_keys(text: str) -> str:
    result: list[str] = []
    in_string = False
    escape = False
    index = 0
    length = len(text)

    def previous_significant() -> str:
        for item in reversed(result):
            if not item.isspace():
                return item
        return ""

    while index < length:
        char = text[index]
        if in_string:
            result.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue
        if char.isalpha() or char in {"_", "$"}:
            token_end = index + 1
            while token_end < length and (text[token_end].isalnum() or text[token_end] in {"_", "$"}):
                token_end += 1
            token = text[index:token_end]
            lookahead = token_end
            while lookahead < length and text[lookahead].isspace():
                lookahead += 1
            if lookahead < length and text[lookahead] == ":" and previous_significant() in {"{", ","}:
                result.append(f'"{token}"')
            else:
                result.append(token)
            index = token_end
            continue
        result.append(char)
        index += 1
    return "".join(result)


def parse_json_like_object_text(text: str, *, source: str) -> dict[str, Any]:
    normalized = _strip_json_like_comments(text)
    normalized = _quote_json_like_keys(normalized)
    normalized = _strip_json_like_trailing_commas(normalized)
    payload = json.loads(normalized)
    if not isinstance(payload, dict):
        raise SystemExit(f"OpenClaw config must be a JSON object: {source}")
    return payload


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return parse_json_like_object_text(path.read_text(encoding="utf-8"), source=str(path))


def _apply_private_file_permissions(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        current_mode = stat.S_IMODE(path.stat().st_mode)
        if current_mode != 0o600:
            path.chmod(0o600)
    except OSError:
        return


def write_json_file(path: Path, payload: dict[str, Any], *, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _apply_private_file_permissions(path)


def backup_config_file(path: Path, *, label: str, dry_run: bool) -> Path | None:
    if dry_run or not path.is_file():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = path.with_name(f"{path.name}.{label}.{timestamp}.bak")
    shutil.copy2(path, backup_path)
    _apply_private_file_permissions(backup_path)
    return backup_path


def _strip_wrapping_quotes(value: str) -> str:
    trimmed = str(value or "").strip()
    if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in {"'", '"'}:
        return trimmed[1:-1]
    return trimmed


def load_env_file(path: Path | None) -> dict[str, str]:
    if not path or not path.is_file():
        return {}
    data: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.lstrip("\ufeff").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, value = line.split("=", 1)
        data[key.strip()] = _strip_wrapping_quotes(value)
    return data


def env_value(env_source: Mapping[str, str] | None, key: str) -> str | None:
    if not env_source:
        return None
    value = str(env_source.get(key) or "").strip()
    return value or None


def write_env_file(path: Path, values: Mapping[str, str], *, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered_keys = sorted({key for key in values if key and not key.startswith("#")})
    lines = [f"{key}={values[key]}" for key in ordered_keys]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _apply_private_file_permissions(path)


def normalize_path_text(value: str | Path | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("\\", "/").rstrip("/").casefold()


def path_match_candidates(path: Path | None) -> set[str]:
    if path is None:
        return set()
    candidate = Path(path).expanduser()
    variants = {
        normalize_path_text(candidate),
        normalize_path_text(candidate.as_posix()),
    }
    try:
        resolved = candidate.resolve(strict=False)
    except Exception:
        resolved = candidate
    variants.add(normalize_path_text(resolved))
    variants.add(normalize_path_text(resolved.as_posix()))
    return {item for item in variants if item}


def dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            ordered.append(item)
            seen.add(item)
    return ordered


def deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = deep_merge(merged.get(key), value)
        return merged
    return override


def default_setup_root(*, home: Path | None = None) -> Path:
    return (home or Path.home()) / ".openclaw" / "memory-palace"


def default_runtime_env_path(setup_root_path: Path) -> Path:
    return setup_root_path / "runtime.env"


def default_database_file(setup_root_path: Path) -> Path:
    return setup_root_path / "data" / "memory-palace.db"


def default_transport_diagnostics_path(setup_root_path: Path) -> Path:
    return setup_root_path / "observability" / "openclaw_transport_diagnostics.json"


def default_runtime_python_path(setup_root_path: Path, host_platform: str | None = None) -> Path:
    if host_platform_name(host_platform) == "windows":
        return setup_root_path / "runtime" / "Scripts" / "python.exe"
    return setup_root_path / "runtime" / "bin" / "python"


def default_dashboard_runtime_dir(setup_root_path: Path) -> Path:
    return setup_root_path / "dashboard"


def default_dashboard_pid_path(setup_root_path: Path) -> Path:
    return default_dashboard_runtime_dir(setup_root_path) / "dashboard.pid"


def runtime_requirements_path() -> Path | None:
    root = backend_root()
    for name in RUNTIME_REQUIREMENTS_FILE_NAMES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def default_dashboard_log_path(setup_root_path: Path) -> Path:
    return default_dashboard_runtime_dir(setup_root_path) / "dashboard.log"


def default_dashboard_url() -> str:
    return f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"


def default_backend_api_runtime_dir(setup_root_path: Path) -> Path:
    return setup_root_path / "backend-api"


def default_backend_api_pid_path(setup_root_path: Path) -> Path:
    return default_backend_api_runtime_dir(setup_root_path) / "backend.pid"


def default_backend_api_log_path(setup_root_path: Path) -> Path:
    return default_backend_api_runtime_dir(setup_root_path) / "backend.log"


def default_backend_api_url() -> str:
    return f"http://{BACKEND_API_HOST}:{BACKEND_API_PORT}"


def sqlite_url_for_file(path: Path) -> str:
    raw_rendered = str(path.expanduser()).replace("\\", "/")
    if raw_rendered.startswith("//"):
        return f"sqlite+aiosqlite://///{raw_rendered.lstrip('/')}"
    if len(raw_rendered) >= 3 and raw_rendered[1] == ":" and raw_rendered[2] == "/":
        return f"sqlite+aiosqlite:///{raw_rendered}"
    resolved = path.expanduser().resolve()
    rendered = str(resolved).replace("\\", "/")
    if rendered.startswith("//"):
        return f"sqlite+aiosqlite://///{rendered.lstrip('/')}"
    if len(rendered) >= 3 and rendered[1] == ":" and rendered[2] == "/":
        return f"sqlite+aiosqlite:///{rendered}"
    return f"sqlite+aiosqlite:////{rendered.lstrip('/')}"


def bool_to_env(value: bool) -> str:
    return "true" if value else "false"


def normalize_base_url(value: str | None) -> str:
    return str(value or "").strip().rstrip("/")


def normalize_embedding_api_base(value: str | None) -> str:
    normalized = normalize_base_url(value)
    if normalized.endswith("/embeddings"):
        return normalized[: -len("/embeddings")]
    return normalized


def normalize_reranker_api_base(value: str | None) -> str:
    normalized = normalize_base_url(value)
    if normalized.endswith("/rerank"):
        return normalized[: -len("/rerank")]
    return normalized


def normalize_chat_api_base(value: str | None) -> str:
    normalized = normalize_base_url(value)
    lowered = normalized.lower()
    for suffix in ("/chat/completions", "/responses"):
        if lowered.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def first_non_blank(*values: str | int | None) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


# ---------------------------------------------------------------------------
# Legacy / short env-var alias mapping
# ---------------------------------------------------------------------------
# Users coming from older docs or blog posts may use these short names.
# We accept them as fallbacks but emit a one-time stderr warning so the
# user knows to switch to the canonical name.

_ENV_ALIAS_WARNED: set[str] = set()

_ENV_LEGACY_ALIASES: dict[str, str] = {
    # Embedding short names → canonical
    "EMBEDDING_API_KEY": "RETRIEVAL_EMBEDDING_API_KEY",
    "EMBEDDING_API_BASE": "RETRIEVAL_EMBEDDING_API_BASE",
    "EMBEDDING_MODEL": "RETRIEVAL_EMBEDDING_MODEL",
    "EMBEDDINGS_API_KEY": "RETRIEVAL_EMBEDDING_API_KEY",
    "EMBEDDINGS_BASE_URL": "RETRIEVAL_EMBEDDING_API_BASE",
    "EMBEDDINGS_MODEL": "RETRIEVAL_EMBEDDING_MODEL",
    # Reranker short names → canonical
    "RERANK_API_KEY": "RETRIEVAL_RERANKER_API_KEY",
    "RERANK_BASE_URL": "RETRIEVAL_RERANKER_API_BASE",
    "RERANK_MODEL": "RETRIEVAL_RERANKER_MODEL",
    "RERANKER_API_KEY": "RETRIEVAL_RERANKER_API_KEY",
    "RERANKER_BASE_URL": "RETRIEVAL_RERANKER_API_BASE",
    "RERANKER_MODEL": "RETRIEVAL_RERANKER_MODEL",
    "RERANKER_API_BASE": "RETRIEVAL_RERANKER_API_BASE",
}


def env_value_with_aliases(
    env_source: Mapping[str, str] | None,
    canonical_key: str,
) -> str | None:
    """Read *canonical_key* from *env_source*, falling back to any known
    legacy aliases.  Emits a one-time stderr warning when an alias is used."""
    value = env_value(env_source, canonical_key)
    if value:
        return value
    for alias, target in _ENV_LEGACY_ALIASES.items():
        if target != canonical_key:
            continue
        alias_value = env_value(env_source, alias)
        if alias_value:
            if alias not in _ENV_ALIAS_WARNED:
                _ENV_ALIAS_WARNED.add(alias)
                print(
                    f"[memory-palace] env alias: {alias} → {canonical_key}  "
                    f"(please update to {canonical_key})",
                    file=sys.stderr,
                )
            return alias_value
    return None


def _metadata_key(name: str) -> str:
    return f"OPENCLAW_MEMORY_PALACE_{name}"


def render_env_prefixed_command(command: str, *, config_path: Path, host_platform: str | None = None) -> str:
    platform_name = host_platform_name(host_platform)
    if platform_name == "windows":
        return f"$env:OPENCLAW_CONFIG_PATH={json.dumps(str(config_path))}; {command}"
    return f"OPENCLAW_CONFIG_PATH={shlex.quote(str(config_path))} {command}"


def repo_python_command(script_and_args: str, *, host_platform: str | None = None) -> str:
    platform_name = host_platform_name(host_platform)
    if platform_name == "windows":
        return f"py -3 {script_and_args}"
    return f"python3 {script_and_args}"


def render_host_command(args: Sequence[str], *, host_platform: str | None = None) -> str:
    rendered = [str(item) for item in args]
    if host_platform_name(host_platform) == "windows":
        return subprocess.list2cmdline(rendered)
    return shlex.join(rendered)


def _path_exists(path_value: str | None) -> bool:
    candidate = str(path_value or "").strip()
    return bool(candidate) and Path(candidate).is_file()


def resolve_posix_stdio_shell() -> tuple[str, list[str]] | None:
    wrapper_command = str(stdio_wrapper())
    shell_env = first_non_blank(os.getenv("SHELL"))
    bash_path = first_non_blank(shell_env if str(shell_env or "").lower().endswith("bash") else None, shutil.which("bash"), "/bin/bash")
    zsh_path = first_non_blank(shell_env if str(shell_env or "").lower().endswith("zsh") else None, shutil.which("zsh"), "/bin/zsh")
    if _path_exists(zsh_path) and _path_exists(bash_path):
        launch_command = f"{shlex.quote(str(bash_path))} {shlex.quote(wrapper_command)}"
        return str(zsh_path), ["-lc", launch_command]
    if _path_exists(bash_path):
        return str(bash_path), [wrapper_command]
    return None


def build_default_stdio_launch(
    *,
    runtime_python_path: Path | None = None,
    host_platform: str | None = None,
) -> tuple[str, list[str], str]:
    platform_name = host_platform_name(host_platform)
    if platform_name == "windows":
        python_path = runtime_python_path or default_runtime_python_path(
            default_setup_root(),
            host_platform=platform_name,
        )
        wrapper_path = windows_stdio_wrapper()
        return (
            python_path.as_posix(),
            [portable_path_string(wrapper_path) or str(wrapper_path)],
            portable_path_string(backend_root()) or str(backend_root()),
        )
    resolved_shell = resolve_posix_stdio_shell()
    if resolved_shell is not None:
        shell_command, shell_args = resolved_shell
        return shell_command, shell_args, str(project_root())
    python_path = runtime_python_path or default_runtime_python_path(
        default_setup_root(),
        host_platform=platform_name,
    )
    wrapper_path = windows_stdio_wrapper()
    return (
        python_path.as_posix(),
        [portable_path_string(wrapper_path) or str(wrapper_path)],
        portable_path_string(backend_root()) or str(backend_root()),
    )


def _normalize_port(value: str | int | None, *, default: int, label: str) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        port = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"Invalid {label} port: {value}") from exc
    if port < 1 or port > 65535:
        raise ValueError(f"Invalid {label} port: {value}")
    return port


def apply_stack_runtime_overrides(
    data: dict[str, str],
    *,
    dashboard_host: str | None = None,
    dashboard_port: str | int | None = None,
    backend_api_host: str | None = None,
    backend_api_port: str | int | None = None,
) -> None:
    resolved_dashboard_host = str(
        dashboard_host or data.get(_metadata_key("DASHBOARD_HOST")) or DASHBOARD_HOST
    ).strip() or DASHBOARD_HOST
    resolved_dashboard_port = _normalize_port(
        dashboard_port if dashboard_port is not None else data.get(_metadata_key("DASHBOARD_PORT")),
        default=DASHBOARD_PORT,
        label="dashboard",
    )
    resolved_backend_host = str(
        backend_api_host or data.get(_metadata_key("BACKEND_API_HOST")) or BACKEND_API_HOST
    ).strip() or BACKEND_API_HOST
    resolved_backend_port = _normalize_port(
        backend_api_port if backend_api_port is not None else data.get(_metadata_key("BACKEND_API_PORT")),
        default=BACKEND_API_PORT,
        label="backend API",
    )

    data[_metadata_key("DASHBOARD_HOST")] = resolved_dashboard_host
    data[_metadata_key("DASHBOARD_PORT")] = str(resolved_dashboard_port)
    data[_metadata_key("BACKEND_API_HOST")] = resolved_backend_host
    data[_metadata_key("BACKEND_API_PORT")] = str(resolved_backend_port)


def resolve_stack_runtime_settings(
    *,
    setup_root_path: Path,
    env_values: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    values = dict(env_values or load_env_file(default_runtime_env_path(setup_root_path)))
    dashboard_host = str(values.get(_metadata_key("DASHBOARD_HOST")) or DASHBOARD_HOST).strip() or DASHBOARD_HOST
    dashboard_port = _normalize_port(
        values.get(_metadata_key("DASHBOARD_PORT")),
        default=DASHBOARD_PORT,
        label="dashboard",
    )
    backend_api_host = str(values.get(_metadata_key("BACKEND_API_HOST")) or BACKEND_API_HOST).strip() or BACKEND_API_HOST
    backend_api_port = _normalize_port(
        values.get(_metadata_key("BACKEND_API_PORT")),
        default=BACKEND_API_PORT,
        label="backend API",
    )
    return {
        "dashboard": {
            "host": dashboard_host,
            "port": dashboard_port,
            "url": f"http://{dashboard_host}:{dashboard_port}",
        },
        "backendApi": {
            "host": backend_api_host,
            "port": backend_api_port,
            "url": f"http://{backend_api_host}:{backend_api_port}",
        },
    }


def _find_available_loopback_port(start_port: int) -> int | None:
    for candidate in range(max(1024, start_port + 1), min(start_port + 101, 65535)):
        if not _port_open("127.0.0.1", candidate):
            return candidate
    return None


def is_loopback_host(host: str | None) -> bool:
    normalized = str(host or "").strip().lower()
    return normalized in LOOPBACK_HOSTS


def is_loopback_sse_url(url: str | None) -> bool:
    normalized = str(url or "").strip()
    if not normalized:
        return False
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return False
    return is_loopback_host(parsed.hostname)


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid_file(path: Path) -> int | None:
    record = _read_pid_file_record(path)
    if record is None:
        return None
    return int(record.get("pid") or 0) or None


def _read_pid_file_record(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        if raw.isdigit():
            return {"pid": int(raw), "legacy": True}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        pid = int(payload.get("pid") or 0)
        if pid <= 0:
            return None
        payload["pid"] = pid
        return payload
    except (OSError, ValueError):
        return None


def _read_process_start_marker(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                'if ($null -eq $p) { exit 1 }; '
                '$p.StartTime.ToUniversalTime().ToString("o")'
            ),
        ]
    else:
        command = ["ps", "-p", str(pid), "-o", "lstart="]
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    marker = str(completed.stdout or "").strip()
    return marker or None


def _read_process_command_line(pid: int) -> str | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                f"$p = Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\"; "
                'if ($null -eq $p) { exit 1 }; '
                "$p.CommandLine"
            ),
        ]
    else:
        command = ["ps", "-p", str(pid), "-o", "command="]
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    command_line = str(completed.stdout or "").strip()
    return command_line or None


def _normalize_pid_command(command: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if command is None:
        return []
    if isinstance(command, str):
        rendered = command.strip()
        return [rendered] if rendered else []
    normalized: list[str] = []
    for item in command:
        rendered = str(item).strip()
        if rendered:
            normalized.append(rendered)
    return normalized


def _read_optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _build_pid_file_record(
    *,
    pid: int,
    component: str,
    command: str | list[str] | tuple[str, ...] | None = None,
    cwd: Path | str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "pid": pid,
        "component": str(component).strip() or "unknown",
    }
    normalized_command = _normalize_pid_command(command)
    if normalized_command:
        record["command"] = normalized_command
    if cwd is not None:
        rendered_cwd = str(cwd).strip()
        if rendered_cwd:
            record["cwd"] = rendered_cwd
    start_marker = _read_process_start_marker(pid)
    if start_marker:
        record["start_marker"] = start_marker
    if os.name != "nt":
        try:
            record["process_group_id"] = os.getpgid(pid)
        except OSError:
            pass
    return record


def _pid_file_record_matches_running_process(record: Mapping[str, Any]) -> bool:
    pid = int(record.get("pid") or 0)
    if pid <= 0 or not _is_process_alive(pid):
        return False
    start_marker = str(record.get("start_marker") or "").strip()
    if start_marker:
        current_marker = _read_process_start_marker(pid)
        if not current_marker or current_marker != start_marker:
            return False
    expected_command = _normalize_pid_command(record.get("command"))
    if expected_command:
        running_command = _read_process_command_line(pid)
        if not running_command:
            return False
        lowered_running = running_command.lower()
        required_markers = [
            marker.lower()
            for marker in (
                expected_command[0],
                *[part for part in expected_command[1:] if part.startswith("--") or "/" in part or "\\" in part],
            )
            if str(marker).strip()
        ]
        if not all(marker in lowered_running for marker in required_markers):
            return False
    return True


def _write_pid_file(
    path: Path,
    pid: int,
    *,
    dry_run: bool,
    record: Mapping[str, Any] | None = None,
) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record or {"pid": pid})
    payload["pid"] = pid
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _remove_file_if_exists(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _wait_for_process_exit(pid: int, *, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0.25)
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            return True
        time.sleep(0.2)
    return not _is_process_alive(pid)


def _wait_for_process_group_exit(pgid: int, *, timeout_seconds: float = 5.0) -> bool:
    if pgid <= 0 or os.name == "nt":
        return True
    deadline = time.monotonic() + max(timeout_seconds, 0.25)
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    try:
        os.killpg(pgid, 0)
    except OSError:
        return True
    return False


def _kill_process_tree_windows(pid: int, *, force: bool) -> bool:
    command = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        command.append("/F")
    try:
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode == 0:
        return True
    detail = str(completed.stderr or completed.stdout or "").lower()
    if any(
        marker in detail
        for marker in (
            "not found",
            "no running instance",
            "no instance",
            "cannot find the process",
            "does not exist",
        )
    ):
        return not _is_process_alive(pid)
    return False


def _terminate_process(pid: int, *, process_group_id: int | None = None) -> bool:
    if not _is_process_alive(pid):
        return False
    if os.name == "nt":
        terminated = _kill_process_tree_windows(pid, force=False)
        if terminated and _wait_for_process_exit(pid):
            return True
        if _is_process_alive(pid):
            return _kill_process_tree_windows(pid, force=True) and _wait_for_process_exit(
                pid, timeout_seconds=5.0
            )
        return terminated
    pgid = process_group_id
    if pgid is None:
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = None
    if pgid:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            return False
        if _wait_for_process_group_exit(pgid, timeout_seconds=5.0):
            return True
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            return False
        return _wait_for_process_group_exit(pgid, timeout_seconds=5.0)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return _wait_for_process_exit(pid)


def _cleanup_timed_out_process(
    *,
    pid: int,
    pid_path: Path,
    component_label: str,
    actions: list[str],
    warnings: list[str],
) -> None:
    terminated = _terminate_process(pid)
    if not _is_process_alive(pid):
        _remove_file_if_exists(pid_path, dry_run=False)
        if terminated:
            actions.append(f"stopped timed-out {component_label} process")
        else:
            actions.append(f"cleared stale pid file for timed-out {component_label} process")
        return
    warnings.append(
        f"{component_label} 启动超时，且自动停止失败；请手动结束 PID {pid} 后再重试。"
    )


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_for_port_closed(host: str, port: int, *, timeout_seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0.25)
    while time.monotonic() < deadline:
        if not _port_open(host, port):
            return True
        time.sleep(0.2)
    return not _port_open(host, port)


def _dashboard_service_ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=1.0) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            body = response.read(4096).decode("utf-8", errors="ignore")
    except OSError:
        return False
    if "text/html" not in content_type:
        return False
    lowered = body.lower()
    return "memory palace" in lowered or "/src/main" in lowered or "vite/client" in lowered


def background_process_popen_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "start_new_session": True,
        "close_fds": True,
    }
    if os.name == "nt":
        creationflags = 0
        for name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS", "CREATE_NO_WINDOW"):
            creationflags |= int(getattr(subprocess, name, 0))
        if creationflags:
            kwargs["creationflags"] = creationflags
    return kwargs


def _backend_api_service_ready(url: str) -> bool:
    probe_url = f"{url.rstrip('/')}/openapi.json"
    try:
        with urlopen(probe_url, timeout=1.0) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            body = response.read(4096).decode("utf-8", errors="ignore")
    except OSError:
        return False
    if "json" not in content_type:
        return False
    lowered = body.lower()
    return "openapi" in lowered and "memory palace api" in lowered


def wait_for_dashboard_ready(host: str, port: int, url: str, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0.5)
    while time.monotonic() < deadline:
        if _port_open(host, port) and _dashboard_service_ready(url):
            return True
        time.sleep(0.25)
    return _port_open(host, port) and _dashboard_service_ready(url)


def wait_for_backend_api_ready(host: str, port: int, url: str, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + max(timeout_seconds, 0.5)
    while time.monotonic() < deadline:
        if _port_open(host, port) and _backend_api_service_ready(url):
            return True
        time.sleep(0.25)
    return _port_open(host, port) and _backend_api_service_ready(url)


def inspect_dashboard_state(
    *,
    setup_root_path: Path,
    env_values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    settings = resolve_stack_runtime_settings(setup_root_path=setup_root_path, env_values=env_values)
    dashboard_settings = settings["dashboard"]
    pid_path = default_dashboard_pid_path(setup_root_path)
    pid = _read_pid_file(pid_path)
    reachable = _port_open(dashboard_settings["host"], dashboard_settings["port"])
    service_ready = _dashboard_service_ready(dashboard_settings["url"]) if reachable else False
    running = bool(pid and _is_process_alive(pid) and service_ready)
    state = {
        "enabled": False,
        "frontendRoot": str(frontend_root()),
        "url": dashboard_settings["url"],
        "host": dashboard_settings["host"],
        "port": dashboard_settings["port"],
        "pidFile": str(pid_path),
        "logFile": str(default_dashboard_log_path(setup_root_path)),
        "running": running,
        "reachable": reachable,
        "serviceReady": service_ready,
        "status": "running" if running else ("running_external" if service_ready else ("port_in_use" if reachable else "stopped")),
    }
    if pid:
        state["pid"] = pid
    return state


def build_backend_api_command(
    *,
    runtime_python_path: Path,
    host: str,
    port: int,
) -> list[str]:
    return [
        str(runtime_python_path),
        "-m",
        "uvicorn",
        "main:app",
        "--app-dir",
        str(backend_root()),
        "--host",
        host,
        "--port",
        str(port),
    ]


def inspect_backend_api_state(
    *,
    setup_root_path: Path,
    env_values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    settings = resolve_stack_runtime_settings(setup_root_path=setup_root_path, env_values=env_values)
    backend_settings = settings["backendApi"]
    pid_path = default_backend_api_pid_path(setup_root_path)
    pid = _read_pid_file(pid_path)
    reachable = _port_open(backend_settings["host"], backend_settings["port"])
    service_ready = _backend_api_service_ready(backend_settings["url"]) if reachable else False
    running = bool(pid and _is_process_alive(pid) and service_ready)
    state = {
        "enabled": False,
        "backendRoot": str(backend_root()),
        "url": backend_settings["url"],
        "host": backend_settings["host"],
        "port": backend_settings["port"],
        "pidFile": str(pid_path),
        "logFile": str(default_backend_api_log_path(setup_root_path)),
        "running": running,
        "reachable": reachable,
        "serviceReady": service_ready,
        "status": "running" if running else ("running_external" if service_ready else ("port_in_use" if reachable else "stopped")),
    }
    if pid:
        state["pid"] = pid
    return state


def profile_template_path(profile: str, platform: str | None = None) -> Path:
    normalized_profile = str(profile or "").strip().lower()
    if normalized_profile not in PROFILE_VALUES:
        raise ValueError(f"Unsupported profile: {profile}")
    candidate = deploy_root() / "profiles" / host_platform_name(platform) / f"profile-{normalized_profile}.env"
    if not candidate.is_file():
        raise FileNotFoundError(f"Missing profile template: {candidate}")
    return candidate


def portable_path_string(path: Path | None) -> str | None:
    if path is None:
        return None
    return path.expanduser().as_posix()


def path_compare_variants(path: Path | None) -> set[str]:
    if path is None:
        return set()
    path_obj = path.expanduser()
    variants = {
        str(path_obj).replace("\\", "/"),
        path_obj.as_posix(),
    }
    try:
        resolved = path_obj.resolve()
    except OSError:
        resolved = None
    if resolved is not None:
        variants.add(str(resolved).replace("\\", "/"))
        variants.add(resolved.as_posix())
    return {item for item in variants if item}
