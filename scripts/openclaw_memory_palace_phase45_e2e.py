#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import openclaw_assistant_derived_e2e as assistant_e2e
import openclaw_memory_palace_installer as installer
import openclaw_memory_palace_profile_smoke as smoke

FORCE_KILL_SIGNAL = smoke._force_kill_signal()

PLUGIN_ROOT = REPO_ROOT / "extensions" / "memory-palace"
DEFAULT_REPORT_PATH = REPO_ROOT / ".tmp" / "openclaw_memory_palace_phase45_e2e.json"
LOCAL_RERANKER_ROOT_ENV = "OPENCLAW_LOCAL_RERANKER_ROOT"
LOCAL_RERANKER_DEFAULT_ROOT_CANDIDATES = (
    REPO_ROOT.parent / "reranker",
    Path.home() / "Desktop" / "reranker",
    Path.home() / "reranker",
)
LOCAL_RERANKER_PORT = 8080
LOCAL_RERANKER_HOST = "127.0.0.1"
LOCAL_RERANKER_API_BASE = f"http://{LOCAL_RERANKER_HOST}:{LOCAL_RERANKER_PORT}"
LOCAL_RERANKER_ALIAS = "Qwen3-Reranker-8B"
LOCAL_RERANKER_CTX_SIZE = 8192
LOCAL_OLLAMA_EMBED_BASE_MODEL = "qwen3-embedding:8b-q8_0"
LOCAL_OLLAMA_EMBED_ALIAS = "qwen3-embedding:8b-q8_0-ctx8192"
LOCAL_OLLAMA_EMBED_CTX_SIZE = 8192
LOCAL_OLLAMA_EMBED_API_BASE = "http://127.0.0.1:11434/v1"
LOCAL_OLLAMA_EMBED_API_KEY = "ollama"
GATEWAY_TERMINATE_WAIT_SECONDS = 1.5
GATEWAY_FORCE_KILL_WAIT_SECONDS = 1.5
PHASE45_GATEWAY_HEALTH_TIMEOUT_SECONDS = 75.0
DEFAULT_PROFILE_EMBEDDING_DIM = 1024
PHASE45_EVENT_LIMIT = 48

REQUIRED_PHASE45_VERIFY_IDS = {
    "smart-extraction",
    "reconcile-mode",
    "last-capture-path",
    "last-fallback-path",
}
REQUIRED_PHASE45_DOCTOR_IDS = {
    "capture-layer-distribution",
    "smart-extraction",
    "reconcile-mode",
}
REQUIRED_PHASE45_SMOKE_IDS = {
    "search-probe",
    "read-probe",
}
ALLOWED_PHASE45_WARN_IDS = {
    "visual-auto-harvest",
    "auto-capture",
    "host-bridge",
    "assistant-derived",
    "last-rule-capture-decision",
    "profile-memory-state",
    "sleep-consolidation",
}
PHASE45_DIAGNOSTIC_IGNORED_WARN_IDS = tuple(sorted(ALLOWED_PHASE45_WARN_IDS | {"last-fallback-path"}))


def openclaw_command(openclaw_bin: str, *args: str) -> list[str]:
    return smoke.openclaw_command(*args, explicit_bin=openclaw_bin)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a real OpenClaw phase4/5 e2e probe.",
    )
    parser.add_argument("--openclaw-bin", default=shutil.which("openclaw") or "openclaw")
    parser.add_argument("--model-env", default=str(smoke.DEFAULT_MODEL_ENV or ""))
    parser.add_argument("--profile", choices=["c", "d"], default="c")
    parser.add_argument("--report", default=str(DEFAULT_REPORT_PATH))
    parser.add_argument("--base-config", default="")
    parser.add_argument("--check-supported", action="store_true")
    parser.add_argument("--cleanup-on-failure", action="store_true")
    return parser.parse_args()


def load_model_env(path_value: str) -> dict[str, str]:
    target = str(path_value or "").strip()
    if not target:
        return {}
    return smoke.load_env_file(Path(target).expanduser().resolve())


def resolve_openclaw_bin_path(path_value: str) -> str:
    rendered = str(path_value or "").strip()
    if not rendered:
        return rendered
    has_path_hint = (
        "/" in rendered
        or "\\" in rendered
        or rendered.startswith(".")
        or (len(rendered) >= 2 and rendered[1] == ":")
    )
    if not has_path_hint:
        return rendered
    return str(Path(rendered).expanduser().resolve())


def _command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _port_is_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def _run_text(command: list[str], *, input_text: str | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _normalize_api_base(raw: str | None) -> str:
    return str(raw or "").strip().rstrip("/")


def _is_local_reranker_base(raw: str | None) -> bool:
    normalized = _normalize_api_base(raw)
    if not normalized:
        return False
    parsed = urllib_parse.urlparse(normalized)
    host = str(parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost"}:
        return False
    if parsed.port is not None:
        return parsed.port == LOCAL_RERANKER_PORT
    return parsed.scheme in {"http", ""} and LOCAL_RERANKER_PORT == 80


def _post_json(
    base_url: str,
    payload: dict[str, Any],
    *,
    api_key: str | None = None,
    endpoint: str = "/embeddings",
    timeout_seconds: float = 30.0,
) -> Any:
    target = f"{str(base_url or '').strip().rstrip('/')}{endpoint}"
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if str(api_key or "").strip():
        headers["Authorization"] = f"Bearer {str(api_key).strip()}"
    request = urllib_request.Request(target, data=body, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except (urllib_error.URLError, urllib_error.HTTPError, TimeoutError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc
    return json.loads(raw.decode("utf-8"))


def probe_reranker_service(
    base_url: str,
    model: str,
    *,
    api_key: str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    parsed = _post_json(
        base_url,
        {
            "model": model,
            "query": "memory palace phase45 reranker readiness probe",
            "documents": [
                "phase45 reranker probe alpha",
                "phase45 reranker probe beta",
            ],
        },
        api_key=api_key,
        endpoint="/rerank",
        timeout_seconds=timeout_seconds,
    )
    if not isinstance(parsed, dict):
        raise RuntimeError(f"reranker probe returned a non-object payload: {parsed!r}")
    results = parsed.get("results")
    if not isinstance(results, list):
        results = parsed.get("data")
    if not isinstance(results, list) or not results:
        raise RuntimeError(
            f"reranker probe returned no results: {json.dumps(parsed, ensure_ascii=False)}"
        )
    first = results[0]
    if not isinstance(first, dict):
        raise RuntimeError(
            f"reranker probe returned an invalid result payload: {json.dumps(parsed, ensure_ascii=False)}"
        )
    if "index" not in first and "score" not in first and "relevance_score" not in first:
        raise RuntimeError(
            "reranker probe payload does not look like a rerank response: "
            f"{json.dumps(parsed, ensure_ascii=False)}"
        )
    return parsed


def probe_embedding_dimension(
    base_url: str,
    model: str,
    *,
    api_key: str | None = None,
    dimensions: int | None = None,
    timeout_seconds: float = 30.0,
) -> int:
    payload = {
        "model": model,
        "input": "memory palace phase45 embedding dimension probe",
    }
    if isinstance(dimensions, int) and dimensions > 0:
        payload["dimensions"] = dimensions
    parsed = _post_json(
        base_url,
        payload,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    data = parsed.get("data") if isinstance(parsed, dict) else None
    first = data[0] if isinstance(data, list) and data else None
    embedding = first.get("embedding") if isinstance(first, dict) else None
    if isinstance(embedding, list) and embedding:
        return len(embedding)
    embeddings = parsed.get("embeddings") if isinstance(parsed, dict) else None
    first_list = embeddings[0] if isinstance(embeddings, list) and embeddings else None
    if isinstance(first_list, list) and first_list:
        return len(first_list)
    if str(base_url or "").strip().rstrip("/") == LOCAL_OLLAMA_EMBED_API_BASE:
        parsed = _post_json(
            "http://127.0.0.1:11434",
            payload,
            endpoint="/api/embed",
            timeout_seconds=timeout_seconds,
        )
        embeddings = parsed.get("embeddings") if isinstance(parsed, dict) else None
        first_list = embeddings[0] if isinstance(embeddings, list) and embeddings else None
        if isinstance(first_list, list) and first_list:
            return len(first_list)
        embedding = parsed.get("embedding") if isinstance(parsed, dict) else None
        if isinstance(embedding, list) and embedding:
            return len(embedding)
    raise RuntimeError(f"embedding probe returned no embedding payload: {json.dumps(parsed, ensure_ascii=False)}")


def probe_llm_service(
    base_url: str,
    model: str,
    *,
    api_key: str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "Reply with JSON only."},
            {"role": "user", "content": "Return {\"ok\":true}."},
        ],
    }
    candidate_bases = [base_url]
    normalized_base = str(base_url or "").strip().rstrip("/")
    if normalized_base == "http://127.0.0.1:8318/v1":
        candidate_bases.append("http://127.0.0.1:8317/v1")
    last_exc: Exception | None = None
    for candidate_base in candidate_bases:
        for attempt in range(1, 4):
            try:
                parsed = _post_json(
                    candidate_base,
                    payload,
                    api_key=api_key,
                    endpoint="/chat/completions",
                    timeout_seconds=timeout_seconds,
                )
                break
            except Exception as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(min(1.0 * attempt, 2.0))
                    continue
        else:
            continue
        break
    else:
        assert last_exc is not None
        raise last_exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"llm probe returned a non-object payload: {parsed!r}")
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(
            f"llm probe returned no choices: {json.dumps(parsed, ensure_ascii=False)}"
        )
    first = choices[0]
    if not isinstance(first, dict):
        raise RuntimeError(
            f"llm probe returned an invalid choice payload: {json.dumps(parsed, ensure_ascii=False)}"
        )
    return parsed


def apply_local_embedding_fallback(model_env: dict[str, str], *, target_dim: int = 1024) -> dict[str, str]:
    resolved = dict(model_env)
    embed_model = ensure_local_ollama_embedding_alias()
    local_dim = probe_embedding_dimension(
        LOCAL_OLLAMA_EMBED_API_BASE,
        embed_model,
        api_key=LOCAL_OLLAMA_EMBED_API_KEY,
        dimensions=target_dim,
        timeout_seconds=30.0,
    )
    resolved["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
    resolved["RETRIEVAL_EMBEDDING_API_BASE"] = LOCAL_OLLAMA_EMBED_API_BASE
    resolved["RETRIEVAL_EMBEDDING_API_KEY"] = LOCAL_OLLAMA_EMBED_API_KEY
    resolved["RETRIEVAL_EMBEDDING_MODEL"] = embed_model
    resolved["RETRIEVAL_EMBEDDING_DIM"] = str(local_dim)
    return resolved


def clear_embedding_provider_chain(model_env: dict[str, str]) -> dict[str, str]:
    resolved = dict(model_env)
    for key in (
        "EMBEDDING_PROVIDER_CHAIN_ENABLED",
        "EMBEDDING_PROVIDER_FAIL_OPEN",
        "EMBEDDING_PROVIDER_FALLBACK",
        "ROUTER_API_BASE",
        "ROUTER_API_KEY",
        "ROUTER_EMBEDDING_MODEL",
    ):
        resolved.pop(key, None)
    return resolved


def ensure_local_ollama_embedding_alias() -> str:
    if not _command_exists("ollama"):
        raise RuntimeError("ollama is not installed; cannot switch embedding to local Ollama")
    listed = _run_text(["ollama", "list"], timeout=60)
    if listed.returncode != 0:
        raise RuntimeError(f"ollama list failed:\n{listed.stderr or listed.stdout}")
    if LOCAL_OLLAMA_EMBED_ALIAS in (listed.stdout or ""):
        return LOCAL_OLLAMA_EMBED_ALIAS
    if LOCAL_OLLAMA_EMBED_BASE_MODEL not in (listed.stdout or ""):
        raise RuntimeError(
            f"ollama model {LOCAL_OLLAMA_EMBED_BASE_MODEL} is missing; cannot create ctx-limited alias"
        )
    modelfile = "\n".join(
        [
            f"FROM {LOCAL_OLLAMA_EMBED_BASE_MODEL}",
            f"PARAMETER num_ctx {LOCAL_OLLAMA_EMBED_CTX_SIZE}",
            "",
        ]
    )
    created = _run_text(
        ["ollama", "create", LOCAL_OLLAMA_EMBED_ALIAS, "-f", "-"],
        input_text=modelfile,
        timeout=300,
    )
    if created.returncode != 0:
        raise RuntimeError(f"ollama create failed:\n{created.stderr or created.stdout}")
    return LOCAL_OLLAMA_EMBED_ALIAS


def resolve_local_reranker_root() -> Path | None:
    explicit = str(os.environ.get(LOCAL_RERANKER_ROOT_ENV) or "").strip()
    if explicit:
        path = Path(explicit).expanduser().resolve()
        return path if path.exists() else None
    for candidate in LOCAL_RERANKER_DEFAULT_ROOT_CANDIDATES:
        resolved = candidate.expanduser().resolve()
        if resolved.exists():
            return resolved
    return None


def resolve_local_reranker_server_executable(root: Path) -> Path | None:
    candidates = [
        root / "llama-bin" / "llama-server.exe",
        root / "llama-bin" / "llama-server",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    discovered = shutil.which("llama-server")
    if discovered:
        resolved = Path(discovered).expanduser().resolve()
        if resolved.is_file():
            return resolved
    return None


@contextlib.contextmanager
def managed_local_reranker(*, model_name: str, api_key: str | None = None) -> Any:
    if _port_is_open(LOCAL_RERANKER_HOST, LOCAL_RERANKER_PORT):
        probe_reranker_service(
            LOCAL_RERANKER_API_BASE,
            model_name,
            api_key=api_key,
            timeout_seconds=30.0,
        )
        yield {
            "managed": True,
            "started": False,
            "pid": None,
            "root": str(resolve_local_reranker_root() or ""),
            "base_url": LOCAL_RERANKER_API_BASE,
            "model": model_name,
            "validated": True,
        }
        return

    root = resolve_local_reranker_root()
    if root is None:
        raise RuntimeError("local reranker root not found; cannot autostart reranker")
    exe = resolve_local_reranker_server_executable(root)
    model = root / "Qwen3-Reranker-8B-Q8_0.gguf"
    if exe is None or not model.is_file():
        raise RuntimeError("llama-server or reranker gguf is missing under the local reranker root")
    logs_dir = root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / "llama-reranker.stdout.log"
    stderr_path = logs_dir / "llama-reranker.stderr.log"
    stdout_handle = stdout_path.open("a", encoding="utf-8")
    stderr_handle = stderr_path.open("a", encoding="utf-8")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [
            str(exe),
            "--model",
            str(model),
            "--reranking",
            "--host",
            LOCAL_RERANKER_HOST,
            "--port",
            str(LOCAL_RERANKER_PORT),
            "--ctx-size",
            str(LOCAL_RERANKER_CTX_SIZE),
            "--n-gpu-layers",
            "all",
            "--alias",
            LOCAL_RERANKER_ALIAS,
        ],
        cwd=str(exe.parent),
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
        creationflags=creationflags if os.name == "nt" else 0,
    )
    try:
        deadline = time.monotonic() + 120.0
        last_probe_error = ""
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"local reranker exited early with code {process.returncode}; see {stderr_path}"
                )
            if _port_is_open(LOCAL_RERANKER_HOST, LOCAL_RERANKER_PORT):
                try:
                    probe_reranker_service(
                        LOCAL_RERANKER_API_BASE,
                        model_name,
                        api_key=api_key,
                        timeout_seconds=5.0,
                    )
                    break
                except RuntimeError as exc:
                    last_probe_error = str(exc)
            time.sleep(0.5)
        else:
            details = f"; last probe error: {last_probe_error}" if last_probe_error else ""
            raise RuntimeError(
                f"local reranker did not become ready on port {LOCAL_RERANKER_PORT} in time{details}"
            )
        yield {
            "managed": True,
            "started": True,
            "pid": process.pid,
            "root": str(root),
            "base_url": LOCAL_RERANKER_API_BASE,
            "model": model_name,
            "validated": True,
        }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=20)
        stdout_handle.close()
        stderr_handle.close()


def model_env_has_usable_reranker(model_env: dict[str, str]) -> bool:
    base = smoke.normalize_reranker_base(model_env.get("RETRIEVAL_RERANKER_API_BASE"))
    model = str(model_env.get("RETRIEVAL_RERANKER_MODEL") or "").strip()
    return bool(base and model and not smoke.is_placeholder_runtime_env_value(base))


def apply_local_reranker_fallback(model_env: dict[str, str]) -> dict[str, str]:
    resolved = dict(model_env)
    resolved["RETRIEVAL_RERANKER_API_BASE"] = LOCAL_RERANKER_API_BASE
    resolved["RETRIEVAL_RERANKER_API_KEY"] = str(
        resolved.get("RETRIEVAL_RERANKER_API_KEY") or "ollama"
    ).strip()
    resolved["RETRIEVAL_RERANKER_MODEL"] = LOCAL_RERANKER_ALIAS
    return resolved


def managed_reranker_runtime(model_env: dict[str, str]) -> Any:
    if model_env_has_usable_reranker(model_env):
        base = smoke.normalize_reranker_base(model_env.get("RETRIEVAL_RERANKER_API_BASE"))
        model = str(model_env.get("RETRIEVAL_RERANKER_MODEL") or "").strip()
        api_key = str(model_env.get("RETRIEVAL_RERANKER_API_KEY") or "").strip()
        if not _is_local_reranker_base(base):
            probe_reranker_service(
                base or "",
                model,
                api_key=api_key or None,
                timeout_seconds=30.0,
            )
            return contextlib.nullcontext(
                {
                    "managed": False,
                    "started": False,
                    "pid": None,
                    "root": "",
                    "base_url": base or "",
                    "model": model,
                    "validated": True,
                }
            )
        return managed_local_reranker(model_name=model, api_key=api_key or None)
    return managed_local_reranker(model_name=LOCAL_RERANKER_ALIAS)


def prewarm_result_for_component(
    prewarm_results: list[dict[str, str]], component: str
) -> dict[str, str] | None:
    normalized_component = str(component or "").strip().lower()
    return next(
        (
            item
            for item in prewarm_results
            if str(item.get("component") or "").strip().lower() == normalized_component
        ),
        None,
    )


def ensure_successful_prewarm(
    prewarm_results: list[dict[str, str]], component: str
) -> None:
    item = prewarm_result_for_component(prewarm_results, component)
    if not isinstance(item, dict):
        raise RuntimeError(f"{component} prewarm did not run")
    if str(item.get("status") or "").strip().lower() == "fail":
        detail = str(item.get("detail") or item.get("error") or "").strip()
        raise RuntimeError(
            f"{component} prewarm failed{(': ' + detail) if detail else ''}"
        )


def apply_local_model_env_overrides(model_env: dict[str, str]) -> dict[str, str]:
    resolved = dict(model_env)
    requested_dim_raw = str(resolved.get("RETRIEVAL_EMBEDDING_DIM") or "").strip()
    try:
        requested_dim = int(requested_dim_raw) if requested_dim_raw else DEFAULT_PROFILE_EMBEDDING_DIM
    except ValueError:
        requested_dim = DEFAULT_PROFILE_EMBEDDING_DIM
    if requested_dim <= 0:
        requested_dim = DEFAULT_PROFILE_EMBEDDING_DIM
    resolved["RETRIEVAL_EMBEDDING_DIM"] = str(requested_dim)
    remote_base = smoke.normalize_embedding_base(
        resolved.get("RETRIEVAL_EMBEDDING_API_BASE") or resolved.get("EMBEDDINGS_BASE_URL")
    )
    remote_model = smoke.resolve_embedding_model_for_profile(resolved)
    remote_api_key = str(
        resolved.get("RETRIEVAL_EMBEDDING_API_KEY")
        or resolved.get("EMBEDDINGS_API_KEY")
        or ""
    ).strip()
    if (
        remote_base
        and remote_model
        and remote_base.rstrip("/") == LOCAL_OLLAMA_EMBED_API_BASE
        and not smoke.is_placeholder_runtime_env_value(remote_base)
    ):
        local_dim = probe_embedding_dimension(
            remote_base,
            remote_model,
            api_key=remote_api_key or LOCAL_OLLAMA_EMBED_API_KEY,
            dimensions=requested_dim,
            timeout_seconds=30.0,
        )
        resolved["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
        resolved["RETRIEVAL_EMBEDDING_API_BASE"] = remote_base
        resolved["RETRIEVAL_EMBEDDING_API_KEY"] = remote_api_key or LOCAL_OLLAMA_EMBED_API_KEY
        resolved["RETRIEVAL_EMBEDDING_MODEL"] = remote_model
        resolved["RETRIEVAL_EMBEDDING_DIM"] = str(local_dim)
        resolved = clear_embedding_provider_chain(resolved)
    elif remote_base and remote_model and not smoke.is_placeholder_runtime_env_value(remote_base):
        try:
            remote_dim = probe_embedding_dimension(
                remote_base,
                remote_model,
                api_key=remote_api_key or None,
                dimensions=requested_dim,
                timeout_seconds=30.0,
            )
            resolved["RETRIEVAL_EMBEDDING_BACKEND"] = "api"
            resolved["RETRIEVAL_EMBEDDING_API_BASE"] = remote_base
            resolved["RETRIEVAL_EMBEDDING_API_KEY"] = remote_api_key
            resolved["RETRIEVAL_EMBEDDING_MODEL"] = remote_model
            resolved["RETRIEVAL_EMBEDDING_DIM"] = str(remote_dim)
            try:
                local_embed_model = ensure_local_ollama_embedding_alias()
                local_dim = probe_embedding_dimension(
                    LOCAL_OLLAMA_EMBED_API_BASE,
                    local_embed_model,
                    api_key=LOCAL_OLLAMA_EMBED_API_KEY,
                    dimensions=remote_dim,
                    timeout_seconds=30.0,
                )
            except Exception:  # noqa: BLE001
                resolved = clear_embedding_provider_chain(resolved)
            else:
                if local_dim != remote_dim:
                    resolved = clear_embedding_provider_chain(resolved)
                else:
                    resolved["EMBEDDING_PROVIDER_CHAIN_ENABLED"] = "true"
                    resolved["EMBEDDING_PROVIDER_FAIL_OPEN"] = "false"
                    resolved["EMBEDDING_PROVIDER_FALLBACK"] = "router"
                    resolved["ROUTER_API_BASE"] = LOCAL_OLLAMA_EMBED_API_BASE
                    resolved["ROUTER_API_KEY"] = LOCAL_OLLAMA_EMBED_API_KEY
                    resolved["ROUTER_EMBEDDING_MODEL"] = local_embed_model
        except Exception:  # noqa: BLE001
            # Keep the explicitly configured remote embedding as the primary path.
            # A transient probe failure during support checks should not force the
            # run onto a local fallback that may be unavailable on this machine.
            resolved = clear_embedding_provider_chain(resolved)
    else:
        resolved = apply_local_embedding_fallback(resolved, target_dim=1024)
    if model_env_has_usable_reranker(resolved):
        resolved["RETRIEVAL_RERANKER_API_BASE"] = (
            smoke.normalize_reranker_base(resolved.get("RETRIEVAL_RERANKER_API_BASE")) or ""
        )
        resolved["RETRIEVAL_RERANKER_API_KEY"] = str(
            resolved.get("RETRIEVAL_RERANKER_API_KEY") or ""
        ).strip()
        resolved["RETRIEVAL_RERANKER_MODEL"] = str(
            resolved.get("RETRIEVAL_RERANKER_MODEL") or ""
        ).strip()
        return resolved
    return apply_local_reranker_fallback(resolved)


def config_has_real_models(base_config_path: Path) -> bool:
    try:
        payload = installer.read_json_file(base_config_path)
    except Exception:  # noqa: BLE001
        return False
    if not isinstance(payload, dict):
        return False
    models = payload.get("models")
    providers = models.get("providers") if isinstance(models, dict) else None
    if isinstance(providers, dict) and providers:
        return True
    agents = payload.get("agents")
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    model = defaults.get("model") if isinstance(defaults, dict) else None
    return isinstance(model, dict) and str(model.get("primary") or "").strip() != ""


def model_env_supports_phase45(model_env: dict[str, str]) -> bool:
    resolved_llm = smoke.resolve_compatible_llm_env(model_env)
    base_url = str(resolved_llm.get("api_base") or "").strip()
    model = str(resolved_llm.get("model") or "").strip()
    return bool(base_url and model)


def collect_phase45_support_report(
    base_config_path: Path,
    model_env: dict[str, str],
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    supported = True
    reason = ""

    if not base_config_path.is_file():
        supported = False
        reason = f"base config missing: {base_config_path}"
        checks.append(
            {
                "id": "base-config",
                "status": "fail",
                "message": reason,
            }
        )
        return {
            "supported": supported,
            "reason": reason,
            "base_config_path": str(base_config_path),
            "model_env_keys": sorted(model_env.keys()),
            "checks": checks,
        }

    if not config_has_real_models(base_config_path):
        supported = False
        reason = (
            f"phase45 requires a base OpenClaw config with real model providers: "
            f"{base_config_path}"
        )
        checks.append(
            {
                "id": "base-config",
                "status": "fail",
                "message": reason,
            }
        )
        return {
            "supported": supported,
            "reason": reason,
            "base_config_path": str(base_config_path),
            "model_env_keys": sorted(model_env.keys()),
            "checks": checks,
        }

    checks.append(
        {
            "id": "base-config",
            "status": "pass",
            "message": f"Usable base OpenClaw config found in {base_config_path}.",
        }
    )

    resolved_llm = smoke.resolve_compatible_llm_env(model_env)
    llm_base = str(resolved_llm.get("api_base") or "").strip()
    llm_model = str(resolved_llm.get("model") or "").strip()
    llm_key = str(resolved_llm.get("api_key") or "").strip()
    if not llm_base or not llm_model:
        supported = False
        reason = (
            "phase45 requires SMART_EXTRACTION_LLM_* or compatible "
            "WRITE_GUARD/INTENT/LLM_RESPONSES/OPENAI model env values"
        )
        checks.append(
            {
                "id": "llm-provider",
                "status": "fail",
                "message": reason,
            }
        )
    else:
        try:
            probe_llm_service(
                llm_base,
                llm_model,
                api_key=llm_key or None,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            supported = False
            reason = f"llm provider preflight failed: {exc}"
            checks.append(
                {
                    "id": "llm-provider",
                    "status": "fail",
                    "message": f"LLM provider preflight failed: {exc}",
                    "details": {
                        "base_url": llm_base,
                        "model": llm_model,
                    },
                }
            )
        else:
            checks.append(
                {
                    "id": "llm-provider",
                    "status": "pass",
                    "message": "LLM provider preflight passed.",
                    "details": {
                        "base_url": llm_base,
                        "model": llm_model,
                    },
                }
            )

    embedding_base = smoke.normalize_embedding_base(
        model_env.get("RETRIEVAL_EMBEDDING_API_BASE") or model_env.get("EMBEDDINGS_BASE_URL")
    )
    embedding_model = smoke.resolve_embedding_model_for_profile(model_env)
    embedding_key = str(
        model_env.get("RETRIEVAL_EMBEDDING_API_KEY")
        or model_env.get("EMBEDDINGS_API_KEY")
        or ""
    ).strip()
    embedding_dim_raw = str(model_env.get("RETRIEVAL_EMBEDDING_DIM") or "").strip()
    embedding_dim: int | None = None
    if embedding_dim_raw:
        try:
            embedding_dim = int(embedding_dim_raw)
        except ValueError:
            embedding_dim = None
    if embedding_base and embedding_model and not smoke.is_placeholder_runtime_env_value(embedding_base):
        try:
            detected_dim = probe_embedding_dimension(
                embedding_base,
                embedding_model,
                api_key=embedding_key or None,
                dimensions=embedding_dim,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            supported = False
            if not reason:
                reason = f"embedding provider preflight failed: {exc}"
            checks.append(
                {
                    "id": "embedding-provider",
                    "status": "fail",
                    "message": f"Embedding provider preflight failed: {exc}",
                    "details": {
                        "base_url": embedding_base,
                        "model": embedding_model,
                        "requested_dim": embedding_dim,
                    },
                }
            )
        else:
            checks.append(
                {
                    "id": "embedding-provider",
                    "status": "pass",
                    "message": "Embedding provider preflight passed.",
                    "details": {
                        "base_url": embedding_base,
                        "model": embedding_model,
                        "requested_dim": embedding_dim,
                        "detected_dim": detected_dim,
                    },
                }
            )
    else:
        checks.append(
            {
                "id": "embedding-provider",
                "status": "warn",
                "message": "No explicit embedding provider preflight was requested; runtime may rely on fallback behavior.",
            }
        )

    if model_env_has_usable_reranker(model_env):
        reranker_base = smoke.normalize_reranker_base(model_env.get("RETRIEVAL_RERANKER_API_BASE")) or ""
        reranker_model = str(model_env.get("RETRIEVAL_RERANKER_MODEL") or "").strip()
        reranker_key = str(model_env.get("RETRIEVAL_RERANKER_API_KEY") or "").strip()
        try:
            probe_reranker_service(
                reranker_base,
                reranker_model,
                api_key=reranker_key or None,
                timeout_seconds=timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            supported = False
            if not reason:
                reason = f"reranker provider preflight failed: {exc}"
            checks.append(
                {
                    "id": "reranker-provider",
                    "status": "fail",
                    "message": f"Reranker provider preflight failed: {exc}",
                    "details": {
                        "base_url": reranker_base,
                        "model": reranker_model,
                    },
                }
            )
        else:
            checks.append(
                {
                    "id": "reranker-provider",
                    "status": "pass",
                    "message": "Reranker provider preflight passed.",
                    "details": {
                        "base_url": reranker_base,
                        "model": reranker_model,
                    },
                }
            )
    else:
        checks.append(
            {
                "id": "reranker-provider",
                "status": "warn",
                "message": "No explicit reranker provider preflight was requested; runtime may rely on fallback behavior.",
            }
        )

    if supported and not reason:
        reason = f"usable base config and provider preflight checks passed for {base_config_path}"

    return {
        "supported": supported,
        "reason": reason,
        "base_config_path": str(base_config_path),
        "model_env_keys": sorted(model_env.keys()),
        "checks": checks,
    }


def resolve_base_config(openclaw_bin: str, base_config: str | Path | None) -> Path:
    explicit = str(base_config or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    for env_name in ("OPENCLAW_CONFIG_PATH", "OPENCLAW_CONFIG"):
        configured = str(os.environ.get(env_name) or "").strip()
        if not configured:
            continue
        candidate = Path(configured).expanduser().resolve()
        if candidate.is_file() and config_has_real_models(candidate):
            return candidate
    home_candidate = Path.home() / ".openclaw" / "openclaw.json"
    if home_candidate.is_file() and config_has_real_models(home_candidate):
        return home_candidate
    return assistant_e2e.resolve_current_openclaw_config(openclaw_bin)


def phase45_e2e_supported(base_config_path: Path, model_env: dict[str, str]) -> tuple[bool, str]:
    payload = collect_phase45_support_report(base_config_path, model_env)
    return bool(payload.get("supported")), str(payload.get("reason") or "")


def build_temp_openclaw_config(
    base_config_path: Path,
    runtime_env_path: Path,
    workspace_dir: Path,
    runtime_python_path: Path | None = None,
    profile: str = "c",
) -> dict[str, Any]:
    payload = installer.read_json_file(base_config_path)
    runtime_env_values = smoke.load_env_file(runtime_env_path)

    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise RuntimeError("hooks must be an object")
    internal_hooks = hooks.setdefault("internal", {})
    if not isinstance(internal_hooks, dict):
        raise RuntimeError("hooks.internal must be an object")
    # Phase45 validates hook-driven capture/recall, so internal hooks must stay on.
    internal_hooks["enabled"] = True

    gateway = payload.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        raise RuntimeError("gateway must be an object")
    auth = gateway.setdefault("auth", {})
    if not isinstance(auth, dict):
        raise RuntimeError("gateway.auth must be an object")
    auth.setdefault("mode", "none")
    auth["token"] = str(auth.get("token") or secrets.token_hex(16))

    agents = payload.setdefault("agents", {})
    if isinstance(agents, dict):
        defaults = agents.setdefault("defaults", {})
        if isinstance(defaults, dict):
            defaults["workspace"] = str(workspace_dir)
            defaults["skipBootstrap"] = True
            compatible_llm = smoke.resolve_compatible_llm_env(runtime_env_values)
            provider_base = str(compatible_llm.get("api_base") or "").strip()
            provider_key = str(compatible_llm.get("api_key") or "").strip()
            provider_model = str(compatible_llm.get("model") or "").strip()
            if provider_base and provider_model:
                models = payload.setdefault("models", {})
                if not isinstance(models, dict):
                    raise RuntimeError("models must be an object")
                providers = models.setdefault("providers", {})
                if not isinstance(providers, dict):
                    raise RuntimeError("models.providers must be an object")
                provider_id = "phase45-openai"
                providers[provider_id] = {
                    "baseUrl": provider_base,
                    "api": "openai-completions",
                    "models": [
                        {
                            "id": provider_model,
                            "name": provider_model,
                            "contextWindow": 256000,
                        }
                    ],
                    **({"apiKey": provider_key} if provider_key else {}),
                }
                defaults["model"] = {"primary": f"{provider_id}/{provider_model}"}
                default_models = defaults.setdefault("models", {})
                if isinstance(default_models, dict):
                    default_models[f"{provider_id}/{provider_model}"] = {"alias": provider_model}

    plugins = payload.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise RuntimeError("plugins must be an object")
    allow = plugins.setdefault("allow", [])
    if isinstance(allow, list) and "memory-palace" not in allow:
        allow.append("memory-palace")
    load = plugins.setdefault("load", {})
    if not isinstance(load, dict):
        raise RuntimeError("plugins.load must be an object")
    load_paths = load.setdefault("paths", [])
    if isinstance(load_paths, list) and str(PLUGIN_ROOT) not in load_paths:
        load_paths.append(str(PLUGIN_ROOT))
    slots = plugins.setdefault("slots", {})
    if not isinstance(slots, dict):
        raise RuntimeError("plugins.slots must be an object")
    slots["memory"] = "memory-palace"
    entries = plugins.setdefault("entries", {})
    if not isinstance(entries, dict):
        raise RuntimeError("plugins.entries must be an object")
    # Phase45 runs inside an isolated test workspace and only needs the
    # memory-palace entry. Carrying unrelated host plugin configs into the
    # temp config can make gateway startup fail on host-specific schema drift.
    for entry_name in list(entries.keys()):
        if entry_name != "memory-palace":
            entries.pop(entry_name, None)
    memory_entry = entries.setdefault("memory-palace", {})
    if not isinstance(memory_entry, dict):
        raise RuntimeError("plugins.entries.memory-palace must be an object")
    memory_entry["enabled"] = True
    config = memory_entry.setdefault("config", {})
    if not isinstance(config, dict):
        raise RuntimeError("plugins.entries.memory-palace.config must be an object")

    config["transport"] = "stdio"
    config["timeoutMs"] = 120000
    config["observability"] = {
        "enabled": True,
        "transportDiagnosticsPath": str(runtime_env_path.parent / "transport-diagnostics.json"),
        "maxRecentTransportEvents": 12,
    }
    config["autoRecall"] = {"enabled": True, "traceEnabled": True}
    # Phase45 validates smart-extracted durable captures, so turn on
    # automatic capture for this isolated test config.
    config["autoCapture"] = {"enabled": True, "traceEnabled": True}
    config["visualMemory"] = {"enabled": False}
    config["reflection"] = {"enabled": False}
    config["profileMemory"] = {
        "enabled": True,
        "injectBeforeAgentStart": True,
        "maxCharsPerBlock": 320,
        "blocks": ["identity", "preferences", "workflow"],
    }
    config["hostBridge"] = {"enabled": False}
    config["capturePipeline"] = {
        "mode": "v2",
        "captureAssistantDerived": False,
        "maxAssistantDerivedPerRun": 2,
        "pendingOnFailure": True,
        "minConfidence": 0.72,
        "pendingConfidence": 0.55,
    }
    mode = "remote" if str(profile or "").strip().lower() == "d" else "local"
    config["smartExtraction"] = {
        "enabled": True,
        "mode": mode,
        "traceEnabled": True,
        "minConversationMessages": 2,
        "maxTranscriptChars": 8000,
        "timeoutMs": 30000,
        "retryAttempts": 2,
        "circuitBreakerFailures": 3,
        "circuitBreakerCooldownMs": 300000,
        "categories": [
            "profile",
            "preferences",
            "workflow",
            "entities",
            "events",
            "cases",
            "patterns",
            "reminders",
        ],
    }
    config["reconcile"] = {
        "enabled": True,
        "profileMergePolicy": "always_merge",
        "eventMergePolicy": "append_only",
        "similarityThreshold": 0.70,
        "pendingOnConflict": True,
        "maxSearchResults": 6,
        "actions": ["ADD", "UPDATE", "NONE"],
    }

    stdio = config.setdefault("stdio", {})
    if not isinstance(stdio, dict):
        raise RuntimeError("plugins.entries.memory-palace.config.stdio must be an object")
    env_block = stdio.setdefault("env", {})
    if not isinstance(env_block, dict):
        raise RuntimeError("plugins.entries.memory-palace.config.stdio.env must be an object")
    for key in (
        "DATABASE_URL",
        "OPENCLAW_MEMORY_PALACE_ENV_FILE",
        "OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON",
        "OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT",
        "OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH",
        "OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR",
    ):
        env_block.pop(key, None)
    env_block["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(runtime_env_path)
    env_block["OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR"] = str(workspace_dir)
    env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_ROOT"] = str(runtime_env_path.parent)
    env_block["OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH"] = str(
        runtime_env_path.parent / "transport-diagnostics.json"
    )
    resolved_runtime_python_path = runtime_python_path
    if resolved_runtime_python_path is None:
        runtime_python = str(os.environ.get("OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON") or "").strip()
        if runtime_python:
            resolved_runtime_python_path = Path(runtime_python).expanduser().resolve()
    if resolved_runtime_python_path is not None:
        env_block["OPENCLAW_MEMORY_PALACE_RUNTIME_PYTHON"] = str(resolved_runtime_python_path)
        env_block.setdefault("PYTHONIOENCODING", "utf-8")
        env_block.setdefault("PYTHONUTF8", "1")
        stdio_command, stdio_args, stdio_cwd = installer.build_default_stdio_launch(
            runtime_python_path=resolved_runtime_python_path,
            host_platform="windows" if os.name == "nt" else None,
        )
        stdio["command"] = stdio_command
        stdio["args"] = stdio_args
        stdio["cwd"] = stdio_cwd
    config.pop("sse", None)
    return payload


def build_runtime_env_file(target: Path, model_env: dict[str, str], profile: str = "c") -> Path:
    runtime_model_env = dict(model_env)
    resolved_llm = smoke.resolve_compatible_llm_env(runtime_model_env)
    # Phase45 always enables smart extraction in the temporary runtime config.
    # Profile C normally does not carry memory-LLM fields, so explicitly seed
    # them here to keep runtime.env aligned with the already-passing provider
    # preflight checks.
    if str(profile or "").strip().lower() == "c":
        if str(resolved_llm.get("api_base") or "").strip():
            runtime_model_env.setdefault("WRITE_GUARD_LLM_API_BASE", resolved_llm["api_base"])
        if str(resolved_llm.get("api_key") or "").strip():
            runtime_model_env.setdefault("WRITE_GUARD_LLM_API_KEY", resolved_llm["api_key"])
        if str(resolved_llm.get("model") or "").strip():
            runtime_model_env.setdefault("WRITE_GUARD_LLM_MODEL", resolved_llm["model"])
    smoke.build_profile_env(smoke.local_native_platform_name(), profile, target, runtime_model_env)
    return target


def build_phase_env(config_payload: dict[str, Any], config_path: Path, state_dir: Path) -> dict[str, str]:
    smoke.assert_isolated_test_runtime_paths(
        context="build_phase_env",
        config_path=config_path,
        state_dir=state_dir,
    )
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    env["OPENCLAW_STATE_DIR"] = str(state_dir)
    gateway = config_payload.get("gateway")
    if isinstance(gateway, dict):
        auth = gateway.get("auth")
        if isinstance(auth, dict):
            token = str(auth.get("token") or "").strip()
            if token:
                env["OPENCLAW_GATEWAY_TOKEN"] = token
    return env


def cleanup_temp_root(tmp_root: Path | None) -> None:
    if tmp_root is None:
        return
    shutil.rmtree(tmp_root, ignore_errors=True)


def check_map(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    checks = payload.get("checks")
    if not isinstance(checks, list):
        raise RuntimeError(f"diagnostic payload missing checks: {json.dumps(payload, ensure_ascii=False)}")
    result: dict[str, dict[str, Any]] = {}
    for item in checks:
        if not isinstance(item, dict):
            continue
        check_id = str(item.get("id") or "").strip()
        if check_id:
            result[check_id] = item
    return result


def ensure_required_check_ids(payload: dict[str, Any], expected: set[str], *, context: str) -> dict[str, dict[str, Any]]:
    mapping = check_map(payload)
    missing = sorted(expected - set(mapping))
    if missing:
        raise RuntimeError(f"{context} missing checks: {missing}\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
    return mapping


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PhaseRecorder:
    def __init__(self, *, event_limit: int = PHASE45_EVENT_LIMIT) -> None:
        self._event_limit = max(1, int(event_limit))
        self.phase_events: list[dict[str, Any]] = []
        self.phase_timings: dict[str, float] = {}
        self.failed_step: str | None = None

    def _normalize_details(self, details: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in details.items():
            if value is None:
                continue
            if isinstance(value, (str, int, float, bool)):
                normalized[key] = value
                continue
            if isinstance(value, Path):
                normalized[key] = str(value)
                continue
            if isinstance(value, (list, tuple)):
                normalized[key] = [
                    item
                    if isinstance(item, (str, int, float, bool)) or item is None
                    else str(item)
                    for item in value
                ]
                continue
            normalized[key] = str(value)
        return normalized

    def _append_event(self, event: dict[str, Any]) -> None:
        self.phase_events.append(event)
        if len(self.phase_events) > self._event_limit:
            self.phase_events = self.phase_events[-self._event_limit :]

    def _log(self, event: dict[str, Any]) -> None:
        parts = [f"[phase45] {event['status']} {event['step']}"]
        elapsed = event.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)):
            parts.append(f"elapsed={float(elapsed):.1f}s")
        details = event.get("details")
        if isinstance(details, dict) and details:
            preview = ", ".join(
                f"{key}={details[key]}"
                for key in sorted(details)
                if details[key] is not None
            )
            if preview:
                parts.append(preview)
        error = str(event.get("error") or "").strip()
        if error:
            parts.append(f"error={error}")
        print(" ".join(parts), file=sys.stderr, flush=True)

    def start(self, step: str, **details: Any) -> float:
        started_at = time.monotonic()
        event: dict[str, Any] = {
            "step": step,
            "status": "start",
            "at": utc_now_iso(),
        }
        normalized_details = self._normalize_details(details)
        if normalized_details:
            event["details"] = normalized_details
        self._append_event(event)
        self._log(event)
        return started_at

    def succeed(self, step: str, started_at: float, **details: Any) -> None:
        elapsed_seconds = round(max(0.0, time.monotonic() - started_at), 3)
        self.phase_timings[step] = elapsed_seconds
        event: dict[str, Any] = {
            "step": step,
            "status": "pass",
            "at": utc_now_iso(),
            "elapsed_seconds": elapsed_seconds,
        }
        normalized_details = self._normalize_details(details)
        if normalized_details:
            event["details"] = normalized_details
        self._append_event(event)
        self._log(event)

    def fail(self, step: str, started_at: float, error: Exception | str, **details: Any) -> None:
        elapsed_seconds = round(max(0.0, time.monotonic() - started_at), 3)
        self.phase_timings[step] = elapsed_seconds
        self.failed_step = step
        event: dict[str, Any] = {
            "step": step,
            "status": "fail",
            "at": utc_now_iso(),
            "elapsed_seconds": elapsed_seconds,
            "error": str(error),
        }
        normalized_details = self._normalize_details(details)
        if normalized_details:
            event["details"] = normalized_details
        self._append_event(event)
        self._log(event)

    def warn(self, step: str, **details: Any) -> None:
        event: dict[str, Any] = {
            "step": step,
            "status": "warn",
            "at": utc_now_iso(),
        }
        normalized_details = self._normalize_details(details)
        if normalized_details:
            event["details"] = normalized_details
        self._append_event(event)
        self._log(event)

    @contextlib.contextmanager
    def span(self, step: str, **details: Any) -> Any:
        started_at = self.start(step, **details)
        try:
            yield
        except Exception as exc:
            self.fail(step, started_at, exc, **details)
            raise
        else:
            self.succeed(step, started_at, **details)

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase_timings": {key: value for key, value in self.phase_timings.items()},
            "phase_events": [dict(event) for event in self.phase_events],
            "failed_step": self.failed_step,
        }


def parse_iso_timestamp(raw: str | None) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_preexisting_phase45_fallback(check: dict[str, Any], run_started_at: str | None) -> bool:
    if str(check.get("id") or "").strip() != "last-fallback-path":
        return False
    started_at = parse_iso_timestamp(run_started_at)
    details = check.get("details")
    if started_at is None or not isinstance(details, dict):
        return False
    fallback_at = parse_iso_timestamp(str(details.get("at") or ""))
    if fallback_at is None:
        return False
    return fallback_at < started_at


def wait_for_llm_extracted_current(
    openclaw_bin: str,
    *,
    env: dict[str, str],
    cwd: Path,
    target_uri: str = "core://agents/main/captured/llm-extracted/workflow/current",
    timeout_seconds: float = 120.0,
    ready_check: Callable[[str], bool] | None = None,
    fallback_targets: tuple[str, ...] = (),
    fallback_ready_check: Callable[[str], bool] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    deadline = time.monotonic() + max(timeout_seconds, 1.0)
    last_error = ""
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            break
        command_timeout_seconds = max(1, min(20, int(remaining)))
        subprocess_timeout = max(10, command_timeout_seconds + 5)
        index_result = assistant_e2e.parse_json_output(
            assistant_e2e.run(
                openclaw_command(
                    openclaw_bin,
                    "memory-palace",
                    "index",
                    "--wait",
                    "--timeout-seconds",
                    str(command_timeout_seconds),
                    "--json",
                ),
                env=env,
                cwd=cwd,
                timeout=subprocess_timeout,
            ),
            context="openclaw memory-palace index",
        )
        get_result = assistant_e2e.run(
            openclaw_command(openclaw_bin, "memory-palace", "get", target_uri, "--json"),
            env=env,
            cwd=cwd,
            timeout=subprocess_timeout,
        )
        fallback_checker = fallback_ready_check or ready_check

        def maybe_read_fallback() -> tuple[dict[str, Any], str] | None:
            nonlocal last_error
            for fallback_target in fallback_targets:
                fallback_payload = try_optional_phase45_get(
                    openclaw_bin,
                    fallback_target,
                    env=env,
                    cwd=cwd,
                    timeout=subprocess_timeout,
                )
                if fallback_payload is None:
                    continue
                fallback_text = str(fallback_payload.get("text") or "")
                if not fallback_text.strip():
                    last_error = json.dumps(fallback_payload, ensure_ascii=False)
                    continue
                if fallback_checker is not None and not fallback_checker(fallback_text):
                    last_error = fallback_text
                    continue
                return fallback_payload, fallback_target
            return None

        if get_result.returncode != 0:
            last_error = (get_result.stderr or get_result.stdout or "").strip()
            fallback_result = maybe_read_fallback()
            if fallback_result is not None:
                fallback_payload, fallback_target = fallback_result
                return index_result, fallback_payload, fallback_target
            time.sleep(0.75)
            continue
        get_payload = assistant_e2e.parse_json_output(
            get_result,
            context="openclaw memory-palace get phase45 fixed llm-extracted current",
        )
        text = str(get_payload.get("text") or "")
        if not text.strip():
            last_error = json.dumps(get_payload, ensure_ascii=False)
            fallback_result = maybe_read_fallback()
            if fallback_result is not None:
                fallback_payload, fallback_target = fallback_result
                return index_result, fallback_payload, fallback_target
            time.sleep(0.75)
            continue
        if ready_check is not None and not ready_check(text):
            last_error = text
            fallback_result = maybe_read_fallback()
            if fallback_result is not None:
                fallback_payload, fallback_target = fallback_result
                return index_result, fallback_payload, fallback_target
            time.sleep(0.75)
            continue

        return index_result, get_payload, target_uri
    raise RuntimeError(
        "phase45 fixed llm-extracted current never became readable:\n"
        f"{last_error}"
    )


def try_optional_phase45_get(
    openclaw_bin: str,
    target_path: str,
    *,
    env: dict[str, str],
    cwd: Path,
    timeout: int = 600,
) -> dict[str, Any] | None:
    try:
        return assistant_e2e.parse_json_output(
            assistant_e2e.run(
                openclaw_command(
                    openclaw_bin,
                    "memory-palace",
                    "get",
                    target_path,
                    "--json",
                ),
                env=env,
                cwd=cwd,
                timeout=timeout,
            ),
            context=f"openclaw memory-palace get {target_path}",
        )
    except RuntimeError as exc:
        message = str(exc).lower()
        if "not found" in message:
            return None
        raise


def ensure_phase45_diagnostics(
    verify_payload: dict[str, Any],
    doctor_payload: dict[str, Any],
    smoke_payload: dict[str, Any],
    *,
    expected_capture_path: str,
    run_started_at: str | None = None,
    allow_manual_learn_distribution: bool = False,
) -> None:
    verify_checks = ensure_required_check_ids(verify_payload, REQUIRED_PHASE45_VERIFY_IDS, context="verify")
    doctor_checks = ensure_required_check_ids(doctor_payload, REQUIRED_PHASE45_DOCTOR_IDS, context="doctor")
    smoke_checks = ensure_required_check_ids(smoke_payload, REQUIRED_PHASE45_SMOKE_IDS, context="smoke")
    verify_capture_message = str(verify_checks["last-capture-path"].get("message") or "")
    has_expected_capture = expected_capture_path in verify_capture_message
    smoke_read_status = str(smoke_checks["read-probe"].get("status") or "").strip().lower()
    capture_verified = smoke_read_status == "pass" or has_expected_capture

    for context, payload in (
        ("verify", verify_payload),
        ("doctor", doctor_payload),
        ("smoke", smoke_payload),
    ):
        for check_id, check in check_map(payload).items():
            status = str(check.get("status") or "").strip().lower()
            if status == "pass":
                continue
            if status == "warn":
                if check_id in ALLOWED_PHASE45_WARN_IDS:
                    continue
                if check_id == "search-probe" and _search_probe_warn_is_allowed(check):
                    continue
                if check_id == "last-capture-path" and capture_verified:
                    continue
                if check_id == "last-fallback-path":
                    if is_preexisting_phase45_fallback(check, run_started_at):
                        continue
                    if capture_verified:
                        continue
                    details = check.get("details")
                    if (
                        isinstance(details, dict)
                        and str(details.get("reason") or "").strip() == "smart_extraction_candidates_empty"
                    ):
                        continue
                raise RuntimeError(
                    f"{context} exposed an unexpected warning for {check_id}:\n"
                    f"{json.dumps(check, ensure_ascii=False, indent=2)}"
                )
            raise RuntimeError(
                f"{context} exposed a non-pass check for {check_id}:\n"
                f"{json.dumps(check, ensure_ascii=False, indent=2)}"
            )

    if not capture_verified:
        raise RuntimeError(f"verify did not expose the expected last capture path: {verify_capture_message}")

    doctor_capture_check = doctor_checks["capture-layer-distribution"]
    doctor_capture_status = str(
        doctor_capture_check.get("status") or ""
    ).strip().lower()
    doctor_capture_message = str(doctor_capture_check.get("message") or "").lower()
    if doctor_capture_status != "pass":
        if allow_manual_learn_distribution and "manual_learn" in doctor_capture_message:
            return
        raise RuntimeError(
            "doctor capture-layer-distribution did not pass:\n"
            f"{json.dumps(doctor_capture_check, ensure_ascii=False, indent=2)}"
        )

    smoke_search_check = smoke_checks["search-probe"]
    smoke_search_status = str(smoke_search_check.get("status") or "").strip().lower()
    smoke_search_ok = smoke_search_status == "pass" or (
        smoke_search_status == "warn" and _search_probe_warn_is_allowed(smoke_search_check)
    )
    if not smoke_search_ok or smoke_read_status != "pass":
        raise RuntimeError(f"smoke probe statuses look wrong: {json.dumps(smoke_payload, ensure_ascii=False, indent=2)}")


def _phase45_required_ids(context: str) -> set[str]:
    if context == "verify":
        return REQUIRED_PHASE45_VERIFY_IDS
    if context == "doctor":
        return REQUIRED_PHASE45_DOCTOR_IDS
    if context == "smoke":
        return REQUIRED_PHASE45_SMOKE_IDS
    raise ValueError(f"unsupported phase45 diagnostics context: {context}")


def _search_probe_warn_is_allowed(check: dict[str, Any]) -> bool:
    details = check.get("details")
    if not isinstance(details, dict):
        return False
    results = details.get("results")
    if not isinstance(results, list) or not results:
        return False
    degrade_reasons = details.get("degrade_reasons")
    if not isinstance(degrade_reasons, list) or not degrade_reasons:
        raw = details.get("raw")
        if isinstance(raw, dict):
            degrade_reasons = raw.get("degrade_reasons")
            if not isinstance(degrade_reasons, list) or not degrade_reasons:
                intent_profile = raw.get("intent_profile")
                if isinstance(intent_profile, dict):
                    degrade_reasons = intent_profile.get("degrade_reasons")
    if not isinstance(degrade_reasons, list) or not degrade_reasons:
        singular_reason = str(details.get("degrade_reason") or "").strip()
        if singular_reason:
            degrade_reasons = [singular_reason]
    normalized_reasons = {
        str(reason or "").strip().lower()
        for reason in (degrade_reasons or [])
        if str(reason or "").strip()
    }
    return bool(normalized_reasons) and normalized_reasons <= {"intent_llm_request_failed"}


def _phase45_warn_is_allowed(
    check_id: str,
    check: dict[str, Any],
    *,
    has_expected_capture: bool,
    capture_verified: bool = False,
    run_started_at: str | None = None,
) -> bool:
    if check_id in ALLOWED_PHASE45_WARN_IDS:
        return True
    if check_id == "search-probe" and _search_probe_warn_is_allowed(check):
        return True
    if check_id == "last-capture-path" and capture_verified:
        return True
    if check_id != "last-fallback-path":
        return False
    if is_preexisting_phase45_fallback(check, run_started_at):
        return True
    if capture_verified:
        return True
    details = check.get("details")
    return (
        isinstance(details, dict)
        and str(details.get("reason") or "").strip() == "smart_extraction_candidates_empty"
    )


def normalize_phase45_report_status(
    context: str,
    payload: dict[str, Any],
    *,
    expected_capture_path: str,
    run_started_at: str | None = None,
) -> str:
    checks = ensure_required_check_ids(payload, _phase45_required_ids(context), context=context)
    capture_message = str(checks.get("last-capture-path", {}).get("message") or "")
    has_expected_capture = expected_capture_path in capture_message
    read_probe_status = str(checks.get("read-probe", {}).get("status") or "").strip().lower()
    capture_verified = read_probe_status == "pass" or has_expected_capture
    for check_id, check in check_map(payload).items():
        status = str(check.get("status") or "").strip().lower()
        if status == "fail":
            return "fail"
        if status == "warn" and not _phase45_warn_is_allowed(
            check_id,
            check,
            has_expected_capture=has_expected_capture,
            capture_verified=capture_verified,
            run_started_at=run_started_at,
        ):
            return "warn"
    return "pass"


def phase45_llm_record_ready(text: str, *, marker: str) -> bool:
    lowered = str(text or "").lower()
    return (
        marker in text
        and "source_mode: llm_extracted" in text
        and "capture_layer: smart_extraction" in text
        and "test" in lowered
        and "doc" in lowered
    )


def phase45_profile_record_ready(text: str, *, marker: str) -> bool:
    lowered = str(text or "").lower()
    return marker in text and "test" in lowered and "doc" in lowered


def model_env_has_explicit_remote_embedding(model_env: dict[str, str]) -> bool:
    base = smoke.normalize_embedding_base(
        model_env.get("RETRIEVAL_EMBEDDING_API_BASE") or model_env.get("EMBEDDINGS_BASE_URL")
    )
    model = str(
        model_env.get("RETRIEVAL_EMBEDDING_MODEL")
        or model_env.get("EMBEDDINGS_MODEL")
        or ""
    ).strip()
    normalized_base = str(base or "").strip().rstrip("/")
    return bool(
        normalized_base
        and normalized_base != LOCAL_OLLAMA_EMBED_API_BASE
        and model
        and not smoke.is_placeholder_runtime_env_value(normalized_base)
    )


def stop_gateway_process(gateway: subprocess.Popen[str]) -> None:
    if gateway.poll() is not None:
        return
    smoke.kill_process_group(gateway.pid, signal.SIGTERM)
    try:
        gateway.terminate()
    except Exception:
        pass
    try:
        gateway.wait(timeout=GATEWAY_TERMINATE_WAIT_SECONDS)
        return
    except subprocess.TimeoutExpired:
        smoke.kill_process_group(gateway.pid, FORCE_KILL_SIGNAL, force=True)
        try:
            gateway.kill()
        except Exception:
            pass
    try:
        gateway.wait(timeout=GATEWAY_FORCE_KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            gateway.kill()
        except Exception:
            pass


@contextlib.contextmanager
def managed_phase45_gateway(
    openclaw_bin: str,
    *,
    env: dict[str, str],
    workspace_dir: Path,
    gateway_log_path: Path,
) -> Any:
    gateway_run_args = [
        "gateway",
        "run",
        "--allow-unconfigured",
    ]
    # Lean Linux containers may not ship lsof/fuser, and OpenClaw's --force
    # currently relies on one of them to evict a stale listener.
    if os.name == "nt" or shutil.which("lsof") or shutil.which("fuser"):
        gateway_run_args.append("--force")
    gateway_port = int(smoke.find_free_port())
    gateway_url = f"ws://127.0.0.1:{gateway_port}"
    previous_gateway_url = env.get("OPENCLAW_GATEWAY_URL")
    had_previous_gateway_url = "OPENCLAW_GATEWAY_URL" in env
    env["OPENCLAW_GATEWAY_URL"] = gateway_url
    gateway_env = dict(env)
    gateway_env.pop("OPENCLAW_GATEWAY_URL", None)
    with gateway_log_path.open("a", encoding="utf-8") as gateway_log:
        gateway = subprocess.Popen(
            [
                *openclaw_command(
                    openclaw_bin,
                    *gateway_run_args,
                    "--port",
                    str(gateway_port),
                ),
            ],
            cwd=str(workspace_dir),
            env=gateway_env,
            stdout=gateway_log,
            stderr=gateway_log,
            text=True,
            start_new_session=True,
        )
    try:
        assistant_e2e.wait_for_gateway(
            openclaw_bin,
            gateway_url,
            env=dict(env),
            cwd=workspace_dir,
            timeout_seconds=PHASE45_GATEWAY_HEALTH_TIMEOUT_SECONDS,
        )
        yield gateway_url
    finally:
        if had_previous_gateway_url:
            env["OPENCLAW_GATEWAY_URL"] = str(previous_gateway_url or "")
        else:
            env.pop("OPENCLAW_GATEWAY_URL", None)
        stop_gateway_process(gateway)


def should_fallback_embedding_after_prewarm(prewarm_results: list[dict[str, str]]) -> bool:
    return any(
        str(item.get("component") or "").strip().lower() == "embedding"
        and str(item.get("status") or "").strip().lower() == "fail"
        for item in prewarm_results
    )


def main() -> int:
    args = parse_args()
    args.openclaw_bin = resolve_openclaw_bin_path(args.openclaw_bin)
    report_path = Path(args.report).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    phase_recorder = PhaseRecorder()
    model_env = apply_local_model_env_overrides(load_model_env(args.model_env))
    base_config_path = resolve_base_config(args.openclaw_bin, args.base_config)
    tmp_root: Path | None = None
    gateway_log_path: Path | None = None
    preserve_tmp_root = False
    support_payload = collect_phase45_support_report(base_config_path, model_env)
    supported = bool(support_payload.get("supported"))
    support_reason = str(support_payload.get("reason") or "")

    if args.check_supported:
        print(json.dumps(support_payload, ensure_ascii=False, indent=2))
        return 0 if supported else 1

    if not supported:
        payload = {
            "ok": False,
            "skipped": True,
            **support_payload,
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    reranker_manager = None
    reranker_runtime = {
        "managed": False,
        "started": False,
        "pid": None,
        "root": "",
        "base_url": "",
        "model": "",
    }
    try:
        run_started_at = utc_now_iso()
        with phase_recorder.span("setup.runtime", profile=args.profile):
            tmp_root = Path(tempfile.mkdtemp(prefix="mp-phase45-e2e-"))
            reranker_manager = managed_reranker_runtime(model_env)
            reranker_runtime = reranker_manager.__enter__()
            workspace_dir = tmp_root / "workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            setup_root = tmp_root / "memory-palace"
            setup_root.mkdir(parents=True, exist_ok=True)
            runtime_env_path = build_runtime_env_file(
                setup_root / "runtime.env",
                model_env,
                args.profile,
            )
            runtime_python_path, _ = installer.ensure_runtime_venv(
                setup_root_path=setup_root,
                dry_run=False,
            )
            runtime_env_values = smoke.load_env_file(runtime_env_path)
        with phase_recorder.span("prewarm.backends", profile=args.profile):
            prewarm_results = smoke.prewarm_profile_model_backends(
                args.profile,
                runtime_env_values,
                timeout_seconds=30.0,
            )
            explicit_remote_embedding = model_env_has_explicit_remote_embedding(model_env)
            embedding_prewarm = prewarm_result_for_component(prewarm_results, "embedding")
            if (
                explicit_remote_embedding
                and isinstance(embedding_prewarm, dict)
                and str(embedding_prewarm.get("status") or "").strip().lower() == "fail"
            ):
                prewarm_results = smoke.prewarm_profile_model_backends(
                    args.profile,
                    runtime_env_values,
                    timeout_seconds=60.0,
                )
                embedding_prewarm = prewarm_result_for_component(prewarm_results, "embedding")
            if (
                isinstance(embedding_prewarm, dict)
                and str(embedding_prewarm.get("status") or "").strip().lower() == "fail"
                and not explicit_remote_embedding
                and str(runtime_env_values.get("RETRIEVAL_EMBEDDING_API_BASE") or "").strip() != LOCAL_OLLAMA_EMBED_API_BASE
            ):
                model_env = apply_local_embedding_fallback(model_env)
                runtime_env_path = build_runtime_env_file(
                    setup_root / "runtime.env",
                    model_env,
                    args.profile,
                )
                runtime_env_values = smoke.load_env_file(runtime_env_path)
                prewarm_results = smoke.prewarm_profile_model_backends(
                    args.profile,
                    runtime_env_values,
                    timeout_seconds=30.0,
                )
                if (
                    should_fallback_embedding_after_prewarm(prewarm_results)
                    and str(runtime_env_values.get("RETRIEVAL_EMBEDDING_API_BASE") or "").strip().rstrip("/")
                    != LOCAL_OLLAMA_EMBED_API_BASE
                ):
                    model_env = apply_local_embedding_fallback(model_env)
                    runtime_env_path = build_runtime_env_file(
                        setup_root / "runtime.env",
                        model_env,
                        args.profile,
                    )
                    runtime_env_values = smoke.load_env_file(runtime_env_path)
                    prewarm_results = smoke.prewarm_profile_model_backends(
                        args.profile,
                        runtime_env_values,
                        timeout_seconds=30.0,
                    )

            ensure_successful_prewarm(prewarm_results, "embedding")
            ensure_successful_prewarm(prewarm_results, "reranker")

        with phase_recorder.span("setup.phase_env", profile=args.profile):
            config_payload = build_temp_openclaw_config(
                base_config_path,
                runtime_env_path,
                workspace_dir,
                runtime_python_path,
                args.profile,
            )
            config_path = tmp_root / "openclaw.json"
            config_path.write_text(
                json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            state_dir = tmp_root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            env = build_phase_env(config_payload, config_path, state_dir)
            env["OPENCLAW_MEMORY_PALACE_WORKSPACE_DIR"] = str(workspace_dir)
            env["OPENCLAW_MEMORY_PALACE_DIAGNOSTIC_IGNORE_WARN_IDS"] = ",".join(
                PHASE45_DIAGNOSTIC_IGNORED_WARN_IDS
            )

        marker = f"phase45-{secrets.token_hex(4)}"
        first_message = (
            f"For future sessions, remember this as my stable long-term workflow preference for {marker}: "
            "1. make code changes first; "
            "2. run the tests immediately after the code changes; "
            "3. keep docs last. "
            "This is a durable preference, not a one-off task. "
            "Reply in one short English sentence only."
        )
        second_message = (
            f"Then for {marker}, run the tests immediately after the code changes. "
            "Reply in one short English sentence only."
        )
        third_message = (
            f"Docs should come at the end for {marker}. "
            "Reply in one short English sentence only."
        )
        recall_message = (
            f"What is the workflow order for {marker}? "
            "Answer in one short English sentence using only the current conversation context. "
            "Do not use tools, do not read files, and do not search memory."
        )

        gateway_log_path = tmp_root / "gateway.log"
        gateway_log_path.write_text("", encoding="utf-8")
        gateway_phase1_started = phase_recorder.start(
            "gateway.capture_phase",
            profile=args.profile,
            workspace_dir=workspace_dir,
        )
        try:
            with managed_phase45_gateway(
                args.openclaw_bin,
                env=env,
                workspace_dir=workspace_dir,
                gateway_log_path=gateway_log_path,
            ):
                phase_recorder.succeed(
                    "gateway.capture_phase",
                    gateway_phase1_started,
                    gateway_log_path=gateway_log_path,
                )
                with phase_recorder.span("agent_message.initial_preference", message_index=1):
                    assistant_e2e.run_agent_message(
                        args.openclaw_bin,
                        first_message,
                        env=env,
                        cwd=workspace_dir,
                        timeout=900,
                    )
                with phase_recorder.span("agent_message.reinforce_tests", message_index=2):
                    assistant_e2e.run_agent_message(
                        args.openclaw_bin,
                        second_message,
                        env=env,
                        cwd=workspace_dir,
                        timeout=900,
                    )
                with phase_recorder.span("agent_message.reinforce_docs", message_index=3):
                    assistant_e2e.run_agent_message(
                        args.openclaw_bin,
                        third_message,
                        env=env,
                        cwd=workspace_dir,
                        timeout=900,
                    )
                with phase_recorder.span("wait_for_llm_extracted_current", marker=marker):
                    index_result, llm_get_payload, llm_path = wait_for_llm_extracted_current(
                        args.openclaw_bin,
                        env=env,
                        cwd=workspace_dir,
                        ready_check=lambda text: phase45_llm_record_ready(text, marker=marker),
                        fallback_targets=(
                            "memory-palace/core/agents/main/profile/workflow.md",
                            "memory-palace/core/agents/main/profile/preferences.md",
                        ),
                        fallback_ready_check=lambda text: phase45_profile_record_ready(
                            text,
                            marker=marker,
                        ),
                    )
                    if (
                        llm_path.endswith("/profile/workflow.md")
                        or llm_path.endswith("/profile/preferences.md")
                    ):
                        phase_recorder.warn(
                            "wait_for_llm_extracted_current",
                            fallback_path=llm_path,
                            reason="smart_extraction_current_missing_but_profile_record_present",
                        )
                llm_text = str(llm_get_payload.get("text") or "")
                summary_text = llm_text
                used_profile_capture_fallback = (
                    llm_path.endswith("/profile/workflow.md")
                    or llm_path.endswith("/profile/preferences.md")
                )
                lowered_llm_text = llm_text.lower()
                if (
                    "source_mode: llm_extracted" not in llm_text
                    and not used_profile_capture_fallback
                ):
                    raise RuntimeError(f"phase45 llm-extracted record is missing expected provenance:\n{llm_text}")
                if "test" not in lowered_llm_text or "doc" not in lowered_llm_text:
                    raise RuntimeError(f"phase45 llm-extracted record is missing the expected workflow steps:\n{llm_text}")
        except Exception as exc:
            if "gateway.capture_phase" not in phase_recorder.phase_timings:
                phase_recorder.fail(
                    "gateway.capture_phase",
                    gateway_phase1_started,
                    exc,
                    gateway_log_path=gateway_log_path,
                )
            raise

        with phase_recorder.span("workflow_profile_probe"):
            workflow_get = try_optional_phase45_get(
                args.openclaw_bin,
                "memory-palace/core/agents/main/profile/workflow.md",
                env=env,
                cwd=workspace_dir,
            )
        preferences_get = try_optional_phase45_get(
            args.openclaw_bin,
            "memory-palace/core/agents/main/profile/preferences.md",
            env=env,
            cwd=workspace_dir,
        )
        profile_probe_payload = workflow_get or preferences_get
        if profile_probe_payload is not None:
            workflow_text = str(profile_probe_payload.get("text") or "")
            lowered_workflow_text = workflow_text.lower()
            if (
                marker not in workflow_text
                or "test" not in lowered_workflow_text
                or "doc" not in lowered_workflow_text
            ):
                raise RuntimeError(
                    "phase45 workflow profile block is missing the expected merged fact:\n"
                    f"{workflow_text}"
                )

        with phase_recorder.span("verify", query=marker):
            verify_payload = assistant_e2e.parse_json_output(
                assistant_e2e.run(
                    openclaw_command(args.openclaw_bin, "memory-palace", "verify", "--json"),
                    env=env,
                    cwd=workspace_dir,
                    timeout=600,
                ),
                context="openclaw memory-palace verify",
            )
        with phase_recorder.span("doctor", query=marker):
            doctor_payload = assistant_e2e.parse_json_output(
                assistant_e2e.run(
                    openclaw_command(
                        args.openclaw_bin,
                        "memory-palace",
                        "doctor",
                        "--query",
                        marker,
                        "--json",
                    ),
                    env=env,
                    cwd=workspace_dir,
                    timeout=600,
                ),
                context="openclaw memory-palace doctor",
            )
        with phase_recorder.span("smoke", query="tests immediately after"):
            smoke_payload = assistant_e2e.parse_json_output(
                assistant_e2e.run(
                    openclaw_command(
                        args.openclaw_bin,
                        "memory-palace",
                        "smoke",
                        "--query",
                        "tests immediately after",
                        "--path-or-uri",
                        llm_path,
                        "--expect-hit",
                        "--json",
                    ),
                    env=env,
                    cwd=workspace_dir,
                    timeout=600,
                ),
                context="openclaw memory-palace smoke",
            )
        ensure_phase45_diagnostics(
            verify_payload,
            doctor_payload,
            smoke_payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at=run_started_at,
            allow_manual_learn_distribution=used_profile_capture_fallback,
        )
        verify_effective_status = normalize_phase45_report_status(
            "verify",
            verify_payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at=run_started_at,
        )
        doctor_effective_status = normalize_phase45_report_status(
            "doctor",
            doctor_payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at=run_started_at,
        )
        smoke_effective_status = normalize_phase45_report_status(
            "smoke",
            smoke_payload,
            expected_capture_path="core://agents/main/captured/llm-extracted/workflow/current",
            run_started_at=run_started_at,
        )

        gateway_phase2_started = phase_recorder.start(
            "gateway.recall_phase",
            profile=args.profile,
            workspace_dir=workspace_dir,
        )
        try:
            with managed_phase45_gateway(
                args.openclaw_bin,
                env=env,
                workspace_dir=workspace_dir,
                gateway_log_path=gateway_log_path,
            ):
                phase_recorder.succeed(
                    "gateway.recall_phase",
                    gateway_phase2_started,
                    gateway_log_path=gateway_log_path,
                )
                with phase_recorder.span("agent_command.new"):
                    assistant_e2e.parse_json_output(
                        assistant_e2e.run(
                            openclaw_command(
                                args.openclaw_bin,
                                "agent",
                                "--agent",
                                "main",
                                "--message",
                                "/new",
                                "--json",
                            ),
                            env=env,
                            cwd=workspace_dir,
                            timeout=600,
                        ),
                        context="openclaw agent /new phase45",
                    )
                with phase_recorder.span("recall", marker=marker):
                    recall_result = assistant_e2e.run_agent_message(
                        args.openclaw_bin,
                        recall_message,
                        env=env,
                        cwd=workspace_dir,
                        timeout=900,
                    )
                recall_text = "\n".join(assistant_e2e.extract_text_fragments(recall_result))
                lowered_recall_text = recall_text.lower()
                if "code" not in lowered_recall_text or "test" not in lowered_recall_text or "doc" not in lowered_recall_text:
                    raise RuntimeError(
                        "phase45 recall did not reflect the plugin-only smart-extracted memory:\n"
                        + json.dumps(recall_result, ensure_ascii=False, indent=2)
                    )
        except Exception as exc:
            if "gateway.recall_phase" not in phase_recorder.phase_timings:
                phase_recorder.fail(
                    "gateway.recall_phase",
                    gateway_phase2_started,
                    exc,
                    gateway_log_path=gateway_log_path,
                )
            raise

        payload = {
            "ok": True,
            "tmp_root": str(tmp_root),
            "gateway_log_path": str(gateway_log_path) if gateway_log_path is not None else "",
            "profile": args.profile,
            "workspace_dir": str(workspace_dir),
            "config_path": str(config_path),
            "state_dir": str(state_dir),
            "runtime_env_path": str(runtime_env_path),
            "base_config_path": str(base_config_path),
            "index_ok": smoke.extract_index_command_ok(index_result),
            "llm_path": llm_path,
            "summary_text": summary_text,
            "recall_text": recall_text,
            "verify_status": verify_effective_status,
            "doctor_status": doctor_effective_status,
            "smoke_status": smoke_effective_status,
            "verify_raw_status": verify_payload.get("status"),
            "doctor_raw_status": doctor_payload.get("status"),
            "smoke_raw_status": smoke_payload.get("status"),
            "managed_local_reranker": reranker_runtime,
            "resolved_embedding_model": model_env.get("RETRIEVAL_EMBEDDING_MODEL"),
            "resolved_embedding_dim": model_env.get("RETRIEVAL_EMBEDDING_DIM"),
            **phase_recorder.snapshot(),
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001
        preserve_tmp_root = not args.cleanup_on_failure
        payload = {
            "ok": False,
            "tmp_root": str(tmp_root) if tmp_root is not None else "",
            "gateway_log_path": str(gateway_log_path) if gateway_log_path is not None else "",
            "artifacts_preserved": preserve_tmp_root,
            "error": str(exc),
            **support_payload,
            **phase_recorder.snapshot(),
        }
        report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    finally:
        if reranker_manager is not None:
            reranker_manager.__exit__(None, None, None)
        if not preserve_tmp_root:
            cleanup_temp_root(tmp_root)


if __name__ == "__main__":
    raise SystemExit(main())
