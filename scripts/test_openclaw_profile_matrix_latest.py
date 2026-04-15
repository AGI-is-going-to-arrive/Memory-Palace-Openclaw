#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import openclaw_memory_palace_installer as installer
import openclaw_memory_palace_profile_smoke as smoke
from openclaw_json_output import extract_json_from_streams

DEFAULT_OPENCLAW_BIN = shutil.which("openclaw") or "openclaw"
DEFAULT_REPORT = REPO_ROOT / ".tmp" / "openclaw-profile-matrix-latest" / "profile-matrix-latest.json"
DEFAULT_MARKDOWN = REPO_ROOT / ".tmp" / "openclaw-profile-matrix-latest" / "profile-matrix-latest.md"
DEFAULT_WORKDIR = Path(tempfile.gettempdir()) / "openclaw-profile-matrix-latest" / "runs"


@dataclass(frozen=True)
class ProfileCase:
    name: str
    requested_profile: str
    require_retrieval: bool
    require_llm_suite: bool


PROFILE_CASES: tuple[ProfileCase, ...] = (
    ProfileCase("profile-a", "a", False, False),
    ProfileCase("profile-b", "b", False, False),
    ProfileCase("profile-c-default", "c", True, False),
    ProfileCase("profile-c-llm-opt-in", "c", True, True),
    ProfileCase("profile-d-default", "d", True, True),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--openclaw-bin", default=os.getenv("OPENCLAW_BIN") or DEFAULT_OPENCLAW_BIN)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--markdown", default=str(DEFAULT_MARKDOWN))
    parser.add_argument("--workdir", default=str(DEFAULT_WORKDIR))
    parser.add_argument("--profiles", default="a,b,c-default,c-llm,d-default")
    parser.add_argument("--skip-acl", action="store_true")
    return parser.parse_args()


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def resolve_openclaw_bin(path_value: str) -> str:
    candidate = Path(path_value).expanduser()
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(path_value)
    if not resolved:
        raise RuntimeError(f"OpenClaw binary not found: {path_value}")
    return str(Path(resolved).resolve())


def run(
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    cwd: Path = REPO_ROOT,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        input=input_text if input_text is not None else "",
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def read_json_payload(stdout: str, stderr: str) -> dict[str, Any]:
    payload = extract_json_from_streams(stdout, stderr)
    return payload if isinstance(payload, dict) else {"value": payload}


def host_env(*, openclaw_bin: str, home_dir: Path, state_dir: Path, config_path: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCLAW_BIN"] = openclaw_bin
    env["PATH"] = os.pathsep.join(
        [str(Path(openclaw_bin).expanduser().resolve().parent), env.get("PATH", "")]
    ).strip(os.pathsep)
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    if config_path is not None:
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    return env


def real_host_env(openclaw_bin: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCLAW_BIN"] = openclaw_bin
    env["PATH"] = os.pathsep.join(
        [str(Path(openclaw_bin).expanduser().resolve().parent), env.get("PATH", "")]
    ).strip(os.pathsep)
    return env


def host_version(openclaw_bin: str, *, env: dict[str, str]) -> str:
    result = run([openclaw_bin, "--version"], env=env, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "openclaw --version failed")
    return (result.stdout or result.stderr).strip().splitlines()[0]


def load_host_config(openclaw_bin: str, *, env: dict[str, str]) -> dict[str, Any]:
    result = run([openclaw_bin, "config", "file"], env=env, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or "openclaw config file failed")
    config_path = Path(str(result.stdout or "").splitlines()[0].strip()).expanduser().resolve()
    return json.loads(config_path.read_text(encoding="utf-8"))


def minimal_host_config(host_config: dict[str, Any], *, workspace_dir: Path, include_acl_agents: bool) -> dict[str, Any]:
    agents = host_config.get("agents") if isinstance(host_config.get("agents"), dict) else {}
    defaults = agents.get("defaults") if isinstance(agents.get("defaults"), dict) else {}
    default_model = defaults.get("model") if isinstance(defaults.get("model"), dict) else None
    default_models = defaults.get("models") if isinstance(defaults.get("models"), dict) else None
    agent_list = [{"id": "main", "default": True, "workspace": str(workspace_dir)}]
    if include_acl_agents:
        agent_list.extend(
            [
                {"id": "alpha", "workspace": str(workspace_dir)},
                {"id": "beta", "workspace": str(workspace_dir)},
            ]
        )
    payload: dict[str, Any] = {
        "agents": {
            "defaults": {
                **({"model": default_model} if default_model else {}),
                **({"models": default_models} if default_models else {}),
                "workspace": str(workspace_dir),
                "skipBootstrap": True,
            },
            "list": agent_list,
        },
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "auth": {
                "mode": "token",
                "token": "profile-matrix-local-token",
            },
            "controlUi": {
                "enabled": True,
            },
        },
        "commands": {
            "native": "auto",
            "nativeSkills": "auto",
            "restart": True,
            "ownerDisplay": "raw",
        },
    }
    if isinstance(host_config.get("models"), dict):
        payload["models"] = host_config["models"]
    return payload


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def first_non_blank(*values: str | None) -> str:
    for value in values:
        rendered = str(value or "").strip()
        if rendered:
            return rendered
    return ""


def normalize_chat_base(base_url: str | None) -> str:
    rendered = str(base_url or "").strip()
    if not rendered:
        return ""
    if rendered.endswith("/chat/completions"):
        return rendered[: -len("/chat/completions")]
    if rendered.endswith("/responses"):
        return rendered[: -len("/responses")]
    return rendered.rstrip("/")


def http_json_ok(
    *,
    base_url: str,
    endpoint: str,
    payload: dict[str, Any],
    api_key: str | None,
    timeout_seconds: float = 30.0,
) -> tuple[bool, str]:
    normalized = str(base_url or "").strip().rstrip("/")
    if not normalized:
        return False, "empty base url"
    request = Request(
        urljoin(f"{normalized}/", endpoint.lstrip("/")),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if str(api_key or "").strip() else {}),
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            return (200 <= response.status < 300), body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code}: {body}"
    except URLError as exc:
        return False, str(exc.reason)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


@dataclass(frozen=True)
class ProviderConfig:
    embedding_base: str
    embedding_key: str
    embedding_model: str
    embedding_dim: str
    reranker_base: str
    reranker_key: str
    reranker_model: str
    llm_primary: str
    llm_secondary: str
    llm_key: str
    llm_model: str


def load_provider_config() -> ProviderConfig:
    return ProviderConfig(
        embedding_base=first_non_blank(
            os.getenv("OPENCLAW_TEST_EMBEDDING_API_BASE"),
            os.getenv("RETRIEVAL_EMBEDDING_API_BASE"),
        ),
        embedding_key=first_non_blank(
            os.getenv("OPENCLAW_TEST_EMBEDDING_API_KEY"),
            os.getenv("RETRIEVAL_EMBEDDING_API_KEY"),
        ),
        embedding_model=first_non_blank(
            os.getenv("OPENCLAW_TEST_EMBEDDING_MODEL"),
            os.getenv("RETRIEVAL_EMBEDDING_MODEL"),
        ),
        embedding_dim=first_non_blank(
            os.getenv("OPENCLAW_TEST_EMBEDDING_DIM"),
            os.getenv("RETRIEVAL_EMBEDDING_DIM"),
            "1024",
        ),
        reranker_base=first_non_blank(
            os.getenv("OPENCLAW_TEST_RERANKER_API_BASE"),
            os.getenv("RETRIEVAL_RERANKER_API_BASE"),
        ),
        reranker_key=first_non_blank(
            os.getenv("OPENCLAW_TEST_RERANKER_API_KEY"),
            os.getenv("RETRIEVAL_RERANKER_API_KEY"),
        ),
        reranker_model=first_non_blank(
            os.getenv("OPENCLAW_TEST_RERANKER_MODEL"),
            os.getenv("RETRIEVAL_RERANKER_MODEL"),
        ),
        llm_primary=normalize_chat_base(
            first_non_blank(
                os.getenv("OPENCLAW_TEST_LLM_API_BASE_PRIMARY"),
                os.getenv("OPENCLAW_TEST_LLM_API_BASE"),
                os.getenv("LLM_API_BASE"),
                os.getenv("OPENAI_API_BASE"),
                os.getenv("OPENAI_BASE_URL"),
            )
        ),
        llm_secondary=normalize_chat_base(
            first_non_blank(
                os.getenv("OPENCLAW_TEST_LLM_API_BASE_SECONDARY"),
                os.getenv("OPENCLAW_TEST_LLM_FALLBACK_API_BASE"),
            )
        ),
        llm_key=first_non_blank(
            os.getenv("OPENCLAW_TEST_LLM_API_KEY"),
            os.getenv("LLM_API_KEY"),
            os.getenv("OPENAI_API_KEY"),
        ),
        llm_model=first_non_blank(
            os.getenv("OPENCLAW_TEST_LLM_MODEL"),
            os.getenv("LLM_MODEL_NAME"),
            os.getenv("OPENAI_MODEL"),
        ),
    )


def resolve_provider_flags(case: ProfileCase, provider: ProviderConfig) -> tuple[dict[str, str], dict[str, Any]]:
    flags: dict[str, str] = {}
    trace: dict[str, Any] = {"embedding": None, "reranker": None, "llm": None}
    if case.require_retrieval:
        if not provider.embedding_base or not provider.embedding_model:
            raise RuntimeError("Embedding provider configuration is incomplete.")
        ok, detail = http_json_ok(
            base_url=provider.embedding_base,
            endpoint="/embeddings",
            payload={
                "model": provider.embedding_model,
                "input": "openclaw profile matrix probe",
                "dimensions": int(provider.embedding_dim),
            },
            api_key=provider.embedding_key,
        )
        if ok:
            flags.update(
                {
                    "embedding_api_base": provider.embedding_base,
                    "embedding_api_key": provider.embedding_key,
                    "embedding_model": provider.embedding_model,
                    "embedding_dim": provider.embedding_dim,
                }
            )
            trace["embedding"] = {"selected": "primary", "detail": detail[:400]}
        else:
            fallback = smoke.apply_local_embedding_fallback(
                {
                    "RETRIEVAL_EMBEDDING_API_BASE": provider.embedding_base,
                    "RETRIEVAL_EMBEDDING_API_KEY": provider.embedding_key,
                    "RETRIEVAL_EMBEDDING_MODEL": provider.embedding_model,
                    "RETRIEVAL_EMBEDDING_DIM": provider.embedding_dim,
                },
                platform="local",
                target_dim=provider.embedding_dim,
            )
            flags.update(
                {
                    "embedding_api_base": str(fallback["RETRIEVAL_EMBEDDING_API_BASE"]),
                    "embedding_api_key": str(fallback["RETRIEVAL_EMBEDDING_API_KEY"]),
                    "embedding_model": str(fallback["RETRIEVAL_EMBEDDING_MODEL"]),
                    "embedding_dim": str(fallback["RETRIEVAL_EMBEDDING_DIM"]),
                }
            )
            trace["embedding"] = {
                "selected": "ollama-fallback",
                "detail": detail[:400],
            }

        if not provider.reranker_base or not provider.reranker_model:
            raise RuntimeError("Reranker provider configuration is incomplete.")
        ok, detail = http_json_ok(
            base_url=provider.reranker_base,
            endpoint="/rerank",
            payload={
                "model": provider.reranker_model,
                "query": "openclaw profile matrix probe",
                "documents": ["alpha", "beta"],
                "top_n": 1,
            },
            api_key=provider.reranker_key,
        )
        if not ok:
            raise RuntimeError(f"Reranker probe failed: {detail}")
        flags.update(
            {
                "reranker_api_base": provider.reranker_base,
                "reranker_api_key": provider.reranker_key,
                "reranker_model": provider.reranker_model,
            }
        )
        trace["reranker"] = {"selected": "primary", "detail": detail[:400]}

    if case.require_llm_suite:
        if not provider.llm_model:
            raise RuntimeError("LLM model is missing.")
        for label, base_url in (("primary", provider.llm_primary), ("secondary", provider.llm_secondary)):
            if not base_url:
                continue
            ok, detail = http_json_ok(
                base_url=base_url,
                endpoint="/chat/completions",
                payload={
                    "model": provider.llm_model,
                    "messages": [{"role": "user", "content": "Reply with OK."}],
                    "max_tokens": 8,
                },
                api_key=provider.llm_key,
            )
            if ok:
                flags.update(
                    {
                        "llm_api_base": base_url,
                        "llm_api_key": provider.llm_key,
                        "llm_model": provider.llm_model,
                    }
                )
                trace["llm"] = {"selected": label, "detail": detail[:400]}
                break
        if trace["llm"] is None:
            raise RuntimeError("LLM probe failed for all configured endpoints.")

    return flags, trace


def command_ok(payload: dict[str, Any]) -> bool:
    if payload.get("ok") is True:
        return True
    result = payload.get("result")
    return isinstance(result, dict) and result.get("ok") is True


def sqlite_path_from_url(database_url: str | None) -> Path | None:
    rendered = str(database_url or "").strip()
    if not rendered or ":memory:" in rendered:
        return None
    if rendered.startswith("sqlite+aiosqlite:////"):
        return Path(rendered[len("sqlite+aiosqlite:////") - 1 :]).expanduser()
    if rendered.startswith("sqlite+aiosqlite:///"):
        return Path(rendered[len("sqlite+aiosqlite:///") :]).expanduser()
    parsed = urlsplit(rendered)
    if parsed.scheme.startswith("sqlite"):
        return Path(parsed.path).expanduser()
    return None


def build_case_env_file(case_root: Path) -> tuple[Path, Path]:
    env_file = case_root / "runtime.env"
    database_path = case_root / "data" / "memory-palace.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return env_file, database_path


def run_setup_case(
    *,
    case: ProfileCase,
    openclaw_bin: str,
    host_version: str,
    host_config: dict[str, Any],
    provider: ProviderConfig,
    workdir: Path,
) -> dict[str, Any]:
    case_root = Path(tempfile.mkdtemp(prefix=f"{case.name}-", dir=str(workdir)))
    home_dir = case_root / "home"
    state_dir = case_root / "state"
    workspace_dir = case_root / "workspace"
    setup_root = case_root / "memory-palace"
    config_path = case_root / "openclaw.json"
    env_file, database_path = build_case_env_file(case_root)
    for directory in (home_dir, state_dir, workspace_dir, setup_root):
        directory.mkdir(parents=True, exist_ok=True)
    try:
        state_dir.chmod(0o700)
    except OSError:
        pass
    credentials_dir = state_dir / "credentials"
    credentials_dir.mkdir(parents=True, exist_ok=True)
    try:
        credentials_dir.chmod(0o700)
    except OSError:
        pass

    write_json(
        config_path,
        minimal_host_config(host_config, workspace_dir=workspace_dir, include_acl_agents=False),
    )
    env = host_env(openclaw_bin=openclaw_bin, home_dir=home_dir, state_dir=state_dir, config_path=config_path)
    provider_flags, provider_trace = resolve_provider_flags(case, provider)
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "openclaw_memory_palace.py"),
        "setup",
        "--config",
        str(config_path),
        "--setup-root",
        str(setup_root),
        "--env-file",
        str(env_file),
        "--database-path",
        str(database_path),
        "--mode",
        "basic",
        "--profile",
        case.requested_profile,
        "--transport",
        "stdio",
        "--strict-profile",
        "--json",
    ]
    for flag_name, value in provider_flags.items():
        command.extend([f"--{flag_name.replace('_', '-')}", value])
    started = time.time()
    setup_run = run(command, env=env, timeout=900)
    if setup_run.returncode != 0:
        raise RuntimeError(
            f"{case.name} setup failed:\nSTDOUT:\n{setup_run.stdout}\nSTDERR:\n{setup_run.stderr}"
        )
    setup_payload = read_json_payload(setup_run.stdout, setup_run.stderr)
    env_values = installer.load_env_file(env_file)
    sqlite_path = sqlite_path_from_url(env_values.get("DATABASE_URL"))
    if sqlite_path is not None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    smoke.seed_local_memory(env_values["DATABASE_URL"], env_values=env_values)
    command_env = smoke.build_openclaw_env(config_path=config_path, state_dir=state_dir)
    plugins_info = smoke.run_openclaw_json_command(
        smoke.openclaw_command("plugins", "inspect", "memory-palace", "--json", explicit_bin=openclaw_bin),
        config_path=config_path,
        state_dir=state_dir,
        timeout=120,
    )
    verify = smoke.run_openclaw_json_command(
        smoke.openclaw_command("memory-palace", "verify", "--json", explicit_bin=openclaw_bin),
        config_path=config_path,
        state_dir=state_dir,
        timeout=180,
    )
    doctor = smoke.run_openclaw_json_command(
        smoke.openclaw_command("memory-palace", "doctor", "--json", explicit_bin=openclaw_bin),
        config_path=config_path,
        state_dir=state_dir,
        timeout=180,
    )
    smoke_report = smoke.run_openclaw_json_command(
        smoke.openclaw_command("memory-palace", "smoke", "--json", explicit_bin=openclaw_bin),
        config_path=config_path,
        state_dir=state_dir,
        timeout=240,
    )
    root_doctor = run(
        [openclaw_bin, "doctor", "--non-interactive", "--no-workspace-suggestions"],
        env=command_env,
        timeout=180,
    )
    merged_text = "\n".join(
        [
            json.dumps(setup_payload, ensure_ascii=False),
            json.dumps(plugins_info, ensure_ascii=False),
            json.dumps(verify, ensure_ascii=False),
            json.dumps(doctor, ensure_ascii=False),
            json.dumps(smoke_report, ensure_ascii=False),
            root_doctor.stdout,
            root_doctor.stderr,
        ]
    )
    report = {
        "profile": case.name,
        "requestedProfile": case.requested_profile,
        "effectiveProfile": str(
            setup_payload.get("effective_profile")
            or setup_payload.get("setup", {}).get("effectiveProfile")
            or case.requested_profile
        ),
        "hostVersion": host_version,
        "setupOk": bool(setup_run.returncode == 0 and setup_payload),
        "pluginsInfoOk": bool(plugins_info),
        "verifyOk": command_ok(verify),
        "doctorOk": command_ok(doctor),
        "smokeOk": command_ok(smoke_report),
        "rootDoctorOk": root_doctor.returncode == 0,
        "containsMemoryCoreRuntimeApi": "memory-core/runtime-api.js" in merged_text,
        "containsNoActiveMemoryPlugin": "No active memory plugin is registered" in merged_text,
        "envFlags": {
            "SEARCH_DEFAULT_MODE": env_values.get("SEARCH_DEFAULT_MODE"),
            "RETRIEVAL_EMBEDDING_BACKEND": env_values.get("RETRIEVAL_EMBEDDING_BACKEND"),
            "RETRIEVAL_EMBEDDING_DIM": env_values.get("RETRIEVAL_EMBEDDING_DIM"),
            "RETRIEVAL_RERANKER_ENABLED": env_values.get("RETRIEVAL_RERANKER_ENABLED"),
            "WRITE_GUARD_LLM_ENABLED": env_values.get("WRITE_GUARD_LLM_ENABLED"),
            "COMPACT_GIST_LLM_ENABLED": env_values.get("COMPACT_GIST_LLM_ENABLED"),
            "INTENT_LLM_ENABLED": env_values.get("INTENT_LLM_ENABLED"),
        },
        "configChecks": {
            "pluginsAllowContainsMemoryPalace": "memory-palace"
            in (installer.read_json_file(config_path).get("plugins", {}).get("allow", []) or []),
            "pluginsAllowContainsMemoryCore": "memory-core"
            in (installer.read_json_file(config_path).get("plugins", {}).get("allow", []) or []),
            "pluginsSlotsMemory": installer.read_json_file(config_path).get("plugins", {}).get("slots", {}).get("memory"),
            "pluginsEntriesMemoryCoreEnabled": bool(
                installer.read_json_file(config_path)
                .get("plugins", {})
                .get("entries", {})
                .get("memory-core", {})
                .get("enabled")
            ),
        },
        "providerTrace": provider_trace,
        "artifacts": {
            "caseRoot": str(case_root),
            "configPath": str(config_path),
            "envFile": str(env_file),
        },
        "elapsedSeconds": round(time.time() - started, 3),
        "allChecksPassed": all(
            [
                command_ok(verify),
                command_ok(doctor),
                command_ok(smoke_report),
                root_doctor.returncode == 0,
            ]
        ),
    }
    write_json(case_root / "case-report.json", report)
    print(f"[profile-matrix] {case.name} done pass={report['allChecksPassed']}", flush=True)
    return report


def build_acl_config(path: Path, *, runtime_env: dict[str, str], host_config: dict[str, Any], workspace_dir: Path) -> None:
    payload = minimal_host_config(host_config, workspace_dir=workspace_dir, include_acl_agents=True)
    payload["plugins"] = {
        "allow": ["memory-palace"],
        "load": {"paths": [str((REPO_ROOT / "extensions" / "memory-palace").resolve())]},
        "slots": {"memory": "memory-palace"},
        "entries": {
            "memory-palace": {
                "enabled": True,
                "config": {
                    "transport": "stdio",
                    "timeoutMs": 120000,
                    "stdio": {"env": runtime_env},
                    "acl": {
                        "enabled": True,
                        "sharedUriPrefixes": [],
                        "sharedWriteUriPrefixes": [],
                        "defaultPrivateRootTemplate": "core://agents/{agentId}",
                        "allowIncludeAncestors": False,
                        "defaultDisclosure": "Agent-scoped durable memory.",
                        "agents": {
                            "main": {
                                "allowedUriPrefixes": ["core://agents/main"],
                                "writeRoots": ["core://agents/main"],
                                "allowIncludeAncestors": False,
                            },
                            "alpha": {
                                "allowedUriPrefixes": ["core://agents/alpha"],
                                "writeRoots": ["core://agents/alpha"],
                                "allowIncludeAncestors": False,
                            },
                            "beta": {
                                "allowedUriPrefixes": ["core://agents/beta"],
                                "writeRoots": ["core://agents/beta"],
                                "allowIncludeAncestors": False,
                            },
                        },
                    },
                },
            }
        },
    }
    write_json(path, payload)


def extract_agent_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    texts = result.get("payloads") if isinstance(result, dict) else None
    if isinstance(texts, list):
        return "\n".join(
            str(item.get("text") or "").strip() for item in texts if isinstance(item, dict)
        ).strip()
    return json.dumps(payload, ensure_ascii=False)


def run_local_agent(
    *,
    openclaw_bin: str,
    config_path: Path,
    state_dir: Path,
    agent_id: str,
    session_id: str,
    message: str,
) -> dict[str, Any]:
    env = smoke.build_openclaw_env(config_path=config_path, state_dir=state_dir)
    result = run(
        [
            openclaw_bin,
            "agent",
            "--local",
            "--agent",
            agent_id,
            "--session-id",
            session_id,
            "--message",
            message,
            "--json",
        ],
        env=env,
        timeout=300,
    )
    payload = read_json_payload(result.stdout, result.stderr)
    return {
        "exitCode": result.returncode,
        "payload": payload,
        "text": extract_agent_text(payload),
    }


def looks_isolated_reply(text: str, secret: str) -> bool:
    rendered = str(text or "").strip()
    if not rendered:
        return False
    lowered = rendered.lower()
    secret_lower = secret.lower()
    return "unknown" in lowered and secret_lower not in lowered


def run_acl_cli(openclaw_bin: str, *, host_config: dict[str, Any], workdir: Path) -> dict[str, Any]:
    case_root = Path(tempfile.mkdtemp(prefix="acl-cli-", dir=str(workdir)))
    config_path = case_root / "openclaw.json"
    env_file, _database_path = build_case_env_file(case_root)
    state_dir = case_root / "state"
    workspace_dir = case_root / "workspace"
    for directory in (state_dir, workspace_dir):
        directory.mkdir(parents=True, exist_ok=True)
    try:
        state_dir.chmod(0o700)
    except OSError:
        pass
    credentials_dir = state_dir / "credentials"
    credentials_dir.mkdir(parents=True, exist_ok=True)
    try:
        credentials_dir.chmod(0o700)
    except OSError:
        pass
    runtime_env = smoke.build_profile_env(smoke.local_native_platform_name(), "b", env_file, {})
    sqlite_path = sqlite_path_from_url(runtime_env.get("DATABASE_URL"))
    if sqlite_path is not None:
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    smoke.seed_local_memory(runtime_env["DATABASE_URL"], env_values=runtime_env)
    build_acl_config(config_path, runtime_env=runtime_env, host_config=host_config, workspace_dir=workspace_dir)

    workflow_text = "list first, implement next, tests last"
    alpha_write = run_local_agent(
        openclaw_bin=openclaw_bin,
        config_path=config_path,
        state_dir=state_dir,
        agent_id="alpha",
        session_id="acl-alpha-write",
        message=(
            "Please remember this workflow for future alpha runs only: "
            f"alpha default workflow is {workflow_text}. "
            "Reply with STORED only."
        ),
    )
    alpha_recall = run_local_agent(
        openclaw_bin=openclaw_bin,
        config_path=config_path,
        state_dir=state_dir,
        agent_id="alpha",
        session_id="acl-alpha-recall",
        message="What is alpha's default workflow? Reply with the workflow sentence only.",
    )
    beta_recall = run_local_agent(
        openclaw_bin=openclaw_bin,
        config_path=config_path,
        state_dir=state_dir,
        agent_id="beta",
        session_id="acl-beta-recall",
        message="What is alpha's default workflow? If you cannot know it, reply UNKNOWN.",
    )
    main_recall = run_local_agent(
        openclaw_bin=openclaw_bin,
        config_path=config_path,
        state_dir=state_dir,
        agent_id="main",
        session_id="acl-main-recall",
        message="What is alpha's default workflow? If you cannot know it, reply UNKNOWN.",
    )
    verify = smoke.run_openclaw_json_command(
        smoke.openclaw_command("memory-palace", "verify", "--json", explicit_bin=openclaw_bin),
        config_path=config_path,
        state_dir=state_dir,
        timeout=120,
    )
    result = {
        "workflowText": workflow_text,
        "verifyOk": command_ok(verify),
        "alphaWrite": alpha_write,
        "alphaRecall": alpha_recall,
        "betaRecall": beta_recall,
        "mainRecall": main_recall,
        "alphaDurableRecallOk": workflow_text.lower() in alpha_recall["text"].lower(),
        "betaIsolationOk": looks_isolated_reply(beta_recall["text"], workflow_text),
        "mainIsolationOk": looks_isolated_reply(main_recall["text"], workflow_text),
    }
    result["allChecksPassed"] = bool(
        result["verifyOk"]
        and result["alphaDurableRecallOk"]
        and result["betaIsolationOk"]
        and result["mainIsolationOk"]
    )
    write_json(case_root / "acl-cli-report.json", result)
    print(f"[profile-matrix] acl-cli done pass={result['allChecksPassed']}", flush=True)
    return result


def normalize_variant(value: str) -> str:
    rendered = str(value or "").strip().lower()
    mapping = {
        "a": "profile-a",
        "b": "profile-b",
        "c-default": "profile-c-default",
        "c-llm": "profile-c-llm-opt-in",
        "d-default": "profile-d-default",
    }
    if rendered not in mapping:
        raise RuntimeError(f"Unsupported profile selector: {value}")
    return mapping[rendered]


def build_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# OpenClaw 2026.4.5 Profile Matrix",
        "",
        f"- OpenClaw binary: `{report['openclawBin']}`",
        f"- Host version: `{report['hostVersion']}`",
        "",
        "| Profile | Requested | Effective | Verify | Doctor | Smoke | Root doctor | memory-core/runtime-api.js | No active memory plugin |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in report["profiles"]:
        lines.append(
            f"| {item['profile']} | {item['requestedProfile'].upper()} | {str(item.get('effectiveProfile') or '').upper()} | "
            f"{'PASS' if item.get('verifyOk') else 'FAIL'} | "
            f"{'PASS' if item.get('doctorOk') else 'FAIL'} | "
            f"{'PASS' if item.get('smokeOk') else 'FAIL'} | "
            f"{'PASS' if item.get('rootDoctorOk') else 'FAIL'} | "
            f"{'YES' if item.get('containsMemoryCoreRuntimeApi') else 'NO'} | "
            f"{'YES' if item.get('containsNoActiveMemoryPlugin') else 'NO'} |"
        )
    if report.get("aclCli") is not None:
        acl = report["aclCli"]
        lines.extend(
            [
                "",
                "## ACL CLI",
                "",
                f"- alpha durable recall ok: `{'yes' if acl.get('alphaDurableRecallOk') else 'no'}`",
                f"- beta isolation ok: `{'yes' if acl.get('betaIsolationOk') else 'no'}`",
                f"- main isolation ok: `{'yes' if acl.get('mainIsolationOk') else 'no'}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    openclaw_bin = resolve_openclaw_bin(args.openclaw_bin)
    report_path = Path(args.report).expanduser().resolve()
    markdown_path = Path(args.markdown).expanduser().resolve()
    workdir = Path(args.workdir).expanduser().resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    ensure_parent(report_path)
    ensure_parent(markdown_path)

    host_base_env = real_host_env(openclaw_bin)
    version = host_version(openclaw_bin, env=host_base_env)
    host_config = load_host_config(openclaw_bin, env=host_base_env)
    provider = load_provider_config()

    selected = {normalize_variant(item) for item in str(args.profiles or "").split(",") if item.strip()}
    profiles: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for case in PROFILE_CASES:
        if case.name not in selected:
            continue
        print(f"[profile-matrix] {case.name} start", flush=True)
        try:
            profiles.append(
                run_setup_case(
                    case=case,
                    openclaw_bin=openclaw_bin,
                    host_version=version,
                    host_config=host_config,
                    provider=provider,
                    workdir=workdir,
                )
            )
        except Exception as exc:  # noqa: BLE001
            failures.append({"scope": case.name, "error": str(exc)})
            profiles.append(
                {
                    "profile": case.name,
                    "requestedProfile": case.requested_profile,
                    "effectiveProfile": None,
                    "hostVersion": version,
                    "allChecksPassed": False,
                    "error": str(exc),
                }
            )
            print(f"[profile-matrix] {case.name} done pass=False error={exc}", flush=True)

    acl_cli = None
    if not args.skip_acl:
        print("[profile-matrix] acl-cli start", flush=True)
        try:
            acl_cli = run_acl_cli(openclaw_bin, host_config=host_config, workdir=workdir)
        except Exception as exc:  # noqa: BLE001
            acl_cli = {"allChecksPassed": False, "error": str(exc)}
            failures.append({"scope": "acl-cli", "error": str(exc)})
            print(f"[profile-matrix] acl-cli done pass=False error={exc}", flush=True)

    report = {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "openclawBin": openclaw_bin,
        "hostVersion": version,
        "profiles": profiles,
        "aclCli": acl_cli,
        "allChecksPassed": all(item.get("allChecksPassed") for item in profiles)
        and (acl_cli is None or bool(acl_cli.get("allChecksPassed"))),
        "failures": failures,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(build_markdown(report), encoding="utf-8")
    print(json.dumps({"report": str(report_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0 if report["allChecksPassed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
