import asyncio
import inspect
import importlib
import hmac
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from api import review_router, browse_router, maintenance_router
from api.maintenance import (
    _allow_insecure_local_without_api_key,
    _extract_bearer_token,
    _get_configured_mcp_api_key,
    _is_loopback_request,
)
from db import get_sqlite_client, close_sqlite_client
from env_utils import (
    env_bool as _env_bool,
    env_float as _env_float,
    utc_iso_now as _utc_iso_now,
)
from run_sse import create_embedded_sse_apps
from runtime_bootstrap import (
    _try_restore_legacy_sqlite_file,
    initialize_backend_runtime,
)
from runtime_state import runtime_state
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


_DEFAULT_CORS_ALLOW_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)
_BOOTSTRAP_INSTALLER = None
_LOOPBACK_RESTART_HOSTS = {"127.0.0.1", "::1", "localhost"}
_BOOTSTRAP_UNPROTECTED_WARNING_EMITTED = False
_RESTART_WAIT_TIMEOUT_ENV = "BOOTSTRAP_RESTART_WAIT_TIMEOUT_SEC"
_RESTART_POLL_INTERVAL_ENV = "BOOTSTRAP_RESTART_POLL_INTERVAL_SEC"
_RESTART_HELPER_WAIT_TIMEOUT_ENV = "MEMORY_PALACE_RESTART_WAIT_TIMEOUT_SEC"
_RESTART_HELPER_POLL_INTERVAL_ENV = "MEMORY_PALACE_RESTART_POLL_INTERVAL_SEC"
_RESTART_HELPER_LOG_PATH_ENV = "MEMORY_PALACE_RESTART_LOG_PATH"
_BOOTSTRAP_VALIDATE_STEP_TIMEOUT_ENV = "BOOTSTRAP_VALIDATE_STEP_TIMEOUT_SEC"
_RESTART_COOLDOWN_SECONDS = 30.0
_last_restart_ts: float = 0.0
_DEFAULT_BACKEND_BIND_HOST = "127.0.0.1"
_DEFAULT_BACKEND_BIND_PORT = 8000


def _resolve_bootstrap_installer():
    global _BOOTSTRAP_INSTALLER
    if _BOOTSTRAP_INSTALLER is not None:
        return _BOOTSTRAP_INSTALLER

    current_file = Path(__file__).resolve()
    candidate_dirs = (
        current_file.parents[1] / "scripts",
        current_file.parents[1] / "release" / "scripts",
        current_file.parents[2] / "release" / "scripts"
        if len(current_file.parents) >= 3
        else None,
    )
    for candidate in candidate_dirs:
        if candidate is None or not candidate.is_dir():
            continue
        candidate_text = str(candidate)
        if candidate_text not in sys.path:
            sys.path.insert(0, candidate_text)
        try:
            _BOOTSTRAP_INSTALLER = importlib.import_module(
                "openclaw_memory_palace_installer"
            )
            return _BOOTSTRAP_INSTALLER
        except ModuleNotFoundError:
            continue
    raise RuntimeError("openclaw_memory_palace_installer module is unavailable")


def _resolve_cors_config() -> tuple[list[str], bool]:
    raw_origins = str(os.getenv("CORS_ALLOW_ORIGINS", "") or "")
    origins = [item.strip() for item in raw_origins.split(",") if item.strip()]
    if not origins:
        origins = list(_DEFAULT_CORS_ALLOW_ORIGINS)

    allow_credentials = _env_bool("CORS_ALLOW_CREDENTIALS", True)
    if "*" in origins and allow_credentials:
        # Browsers reject '*' + credentials. Fall back to credential-less CORS.
        logger.warning(
            "CORS_ALLOW_CREDENTIALS=true ignored because CORS_ALLOW_ORIGINS contains '*'; "
            "falling back to allow_credentials=False."
        )
        allow_credentials = False
    return origins, allow_credentials


def _health_request_allows_details(
    request: Request,
    *,
    x_mcp_api_key: Optional[str] = None,
    authorization: Optional[str] = None,
) -> bool:
    if _is_loopback_request(request):
        return True

    configured = _get_configured_mcp_api_key()
    if not configured:
        return False

    provided = str(x_mcp_api_key or "").strip() or _extract_bearer_token(authorization)
    return bool(provided) and hmac.compare_digest(provided, configured)


def _mount_embedded_sse_apps(app: FastAPI) -> None:
    if getattr(app.state, "embedded_sse_mounted", False):
        return

    embedded_sse_stream_app, embedded_sse_message_app = create_embedded_sse_apps()
    app.mount("/sse/messages", embedded_sse_message_app)
    app.mount("/messages", embedded_sse_message_app)
    app.mount("/sse", embedded_sse_stream_app)
    app.state.embedded_sse_mounted = True


class BootstrapApplyRequest(BaseModel):
    mode: str = Field(default="basic")
    profile: str = Field(default="b")
    transport: str = Field(default="stdio")
    validateAfterApply: bool = Field(default=False, alias="validate")
    reconfigure: bool = False
    databasePath: Optional[str] = None
    sseUrl: Optional[str] = None
    mcpApiKey: Optional[str] = None
    allowInsecureLocal: bool = False
    embeddingApiBase: Optional[str] = None
    embeddingApiKey: Optional[str] = None
    embeddingModel: Optional[str] = None
    embeddingDim: Optional[str] = None
    rerankerApiBase: Optional[str] = None
    rerankerApiKey: Optional[str] = None
    rerankerModel: Optional[str] = None
    llmApiBase: Optional[str] = None
    llmApiKey: Optional[str] = None
    llmModel: Optional[str] = None
    writeGuardLlmApiBase: Optional[str] = None
    writeGuardLlmApiKey: Optional[str] = None
    writeGuardLlmModel: Optional[str] = None
    compactGistLlmApiBase: Optional[str] = None
    compactGistLlmApiKey: Optional[str] = None
    compactGistLlmModel: Optional[str] = None


class BootstrapProviderProbeRequest(BaseModel):
    mode: str = Field(default="basic")
    profile: str = Field(default="b")
    transport: str = Field(default="stdio")
    sseUrl: Optional[str] = None
    mcpApiKey: Optional[str] = None
    allowInsecureLocal: bool = False
    embeddingApiBase: Optional[str] = None
    embeddingApiKey: Optional[str] = None
    embeddingModel: Optional[str] = None
    embeddingDim: Optional[str] = None
    rerankerApiBase: Optional[str] = None
    rerankerApiKey: Optional[str] = None
    rerankerModel: Optional[str] = None
    llmApiBase: Optional[str] = None
    llmApiKey: Optional[str] = None
    llmModel: Optional[str] = None
    writeGuardLlmApiBase: Optional[str] = None
    writeGuardLlmApiKey: Optional[str] = None
    writeGuardLlmModel: Optional[str] = None
    compactGistLlmApiBase: Optional[str] = None
    compactGistLlmApiKey: Optional[str] = None
    compactGistLlmModel: Optional[str] = None


async def require_bootstrap_access(
    request: Request,
    x_mcp_api_key: Optional[str] = Header(default=None, alias="X-MCP-API-Key"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> None:
    global _BOOTSTRAP_UNPROTECTED_WARNING_EMITTED
    if not _is_loopback_request(request):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "bootstrap_access_denied",
                "reason": "loopback_required",
            },
        )
    if str(request.method or "").upper() in {"GET", "HEAD", "OPTIONS"}:
        return

    configured = _get_configured_mcp_api_key()
    if not configured:
        if not _BOOTSTRAP_UNPROTECTED_WARNING_EMITTED:
            logger.warning(
                "Bootstrap mutating endpoints are running without MCP_API_KEY protection; "
                "loopback requests remain allowed until MCP_API_KEY is configured."
            )
            _BOOTSTRAP_UNPROTECTED_WARNING_EMITTED = True
        return

    provided = str(x_mcp_api_key or "").strip() or _extract_bearer_token(authorization)
    if not provided or not hmac.compare_digest(provided, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "bootstrap_auth_failed",
                "reason": "invalid_or_missing_api_key",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


def _bootstrap_subprocess_failure_message(exc: subprocess.CalledProcessError) -> str:
    return_code = getattr(exc, "returncode", None)
    if isinstance(return_code, int):
        return f"bootstrap installer failed with exit code {return_code}. See server logs for details."
    return "bootstrap installer failed. See server logs for details."


def _parse_openclaw_json_payload(installer_module: Any, stdout: str, stderr: str) -> Dict[str, Any]:
    for candidate in (stdout, stderr):
        try:
            parsed = installer_module.parse_jsonish_stdout(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {
        "ok": False,
        "summary": "openclaw command produced no JSON payload. See server logs for details.",
    }


def _run_post_setup_validation(installer_module: Any, *, config_path: Path) -> Dict[str, Any]:
    openclaw_bin = None
    resolve_binary = getattr(installer_module, "resolve_openclaw_binary", None)
    if callable(resolve_binary):
        openclaw_bin = resolve_binary()
    openclaw_bin = str(openclaw_bin or "").strip() or "openclaw"

    steps = []
    failed_step = None
    env = os.environ.copy()
    env["OPENCLAW_CONFIG_PATH"] = str(config_path)
    timeout_seconds = _env_float(
        _BOOTSTRAP_VALIDATE_STEP_TIMEOUT_ENV,
        120.0,
        minimum=1.0,
    )
    for step_name in ("verify", "doctor", "smoke"):
        try:
            completed = subprocess.run(
                [openclaw_bin, "memory-palace", step_name, "--json"],
                cwd=str(Path(__file__).resolve().parents[1]),
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            steps.append(
                {
                    "name": step_name,
                    "ok": False,
                    "exit_code": None,
                    "summary": f"{step_name} timed out after {timeout_seconds:g}s",
                    "status": "timeout",
                    "code": "timeout",
                }
            )
            failed_step = step_name
            break
        payload = _parse_openclaw_json_payload(installer_module, completed.stdout, completed.stderr)
        step_ok = bool(completed.returncode == 0 and payload.get("ok"))
        steps.append(
            {
                "name": step_name,
                "ok": step_ok,
                "exit_code": completed.returncode,
                "summary": str(payload.get("summary") or ""),
                "status": str(payload.get("status") or ""),
                "code": str(payload.get("code") or ""),
            }
        )
        if not step_ok:
            failed_step = step_name
            break
    return {
        "ok": failed_step is None,
        "failed_step": failed_step,
        "steps": steps,
    }


async def _run_post_setup_validation_async(
    installer_module: Any, *, config_path: Path
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _run_post_setup_validation,
        installer_module,
        config_path=config_path,
    )


def _restart_supported_for_request(request: Request) -> bool:
    server = request.scope.get("server")
    host = ""
    if isinstance(server, (tuple, list)) and server:
        host = str(server[0] or "").strip().lower()
    if not host:
        host = str(request.url.hostname or "").strip().lower()
    return _is_loopback_request(request) and host in _LOOPBACK_RESTART_HOSTS


def _build_local_restart_command(request: Request) -> list[str]:
    backend_dir = Path(__file__).resolve().parent
    server = request.scope.get("server")
    host = "127.0.0.1"
    port = 8000
    if isinstance(server, (tuple, list)) and len(server) >= 2:
        host = str(server[0] or host).strip() or host
        try:
            port = int(server[1] or port)
        except (TypeError, ValueError):
            port = 8000
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "main:app",
        "--app-dir",
        str(backend_dir),
        "--host",
        host,
        "--port",
        str(port),
    ]


def _current_bootstrap_context() -> dict[str, str]:
    context: dict[str, str] = {}
    config_value = str(os.getenv("OPENCLAW_CONFIG_PATH") or "").strip()
    env_file_value = str(os.getenv("OPENCLAW_MEMORY_PALACE_ENV_FILE") or "").strip()
    setup_root_value = str(os.getenv("OPENCLAW_MEMORY_PALACE_SETUP_ROOT") or "").strip()
    if not setup_root_value and env_file_value:
        try:
            setup_root_value = str(Path(env_file_value).expanduser().resolve().parent)
        except OSError:
            setup_root_value = ""
    if config_value:
        context["config"] = config_value
    if setup_root_value:
        context["setup_root_value"] = setup_root_value
    if env_file_value:
        context["env_file_value"] = env_file_value
    return context


def _resolve_backend_bind_host() -> str:
    rendered = str(os.getenv("HOST") or _DEFAULT_BACKEND_BIND_HOST).strip()
    return rendered or _DEFAULT_BACKEND_BIND_HOST


def _resolve_backend_bind_port() -> int:
    rendered = str(os.getenv("PORT") or str(_DEFAULT_BACKEND_BIND_PORT)).strip()
    try:
        return int(rendered)
    except ValueError:
        return _DEFAULT_BACKEND_BIND_PORT


def _build_restart_env(installer_module: Any) -> tuple[dict[str, str], Optional[str]]:
    restart_env = dict(os.environ)
    status_payload = installer_module.bootstrap_status(**_current_bootstrap_context())
    setup_payload = status_payload.get("setup") if isinstance(status_payload, dict) else {}
    env_file = None
    if isinstance(setup_payload, dict):
        env_file = str(setup_payload.get("envFile") or "").strip() or None
    refresh_keys = {
        "DATABASE_URL",
        "MCP_API_KEY",
        "MCP_API_KEY_ALLOW_INSECURE_LOCAL",
        "OPENCLAW_CONFIG_PATH",
        "OPENCLAW_MEMORY_PALACE_ENV_FILE",
        "OPENCLAW_MEMORY_PALACE_SETUP_ROOT",
    }
    provider_keys = getattr(installer_module, "RETRIEVAL_PROVIDER_RUNTIME_ENV_KEYS", None)
    if isinstance(provider_keys, (list, tuple, set)):
        refresh_keys.update(str(key) for key in provider_keys if str(key).strip())
    for key in list(restart_env):
        if (
            key in refresh_keys
            or key.startswith("ROUTER_")
            or key.startswith("RETRIEVAL_")
            or key.startswith("LLM_")
            or key.startswith("WRITE_GUARD_")
            or key.startswith("COMPACT_GIST_")
            or key.startswith("INTENT_")
            or key.startswith("OPENAI_")
        ):
            restart_env.pop(key, None)
    if env_file:
        env_path = Path(env_file).expanduser().resolve()
        if env_path.is_file():
            restart_env.update(installer_module.load_env_file(env_path))
            restart_env["OPENCLAW_MEMORY_PALACE_ENV_FILE"] = str(env_path)
            restart_env.setdefault("OPENCLAW_MEMORY_PALACE_SETUP_ROOT", str(env_path.parent))
    config_path = ""
    if isinstance(setup_payload, dict):
        config_path = str(setup_payload.get("configPath") or "").strip()
    if config_path:
        restart_env["OPENCLAW_CONFIG_PATH"] = config_path
    return restart_env, env_file


def _build_restart_supervisor_log_path(launch_cwd: str) -> str:
    backend_dir = Path(launch_cwd).resolve()
    log_path = backend_dir.parent / ".tmp" / "bootstrap-restart-supervisor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return str(log_path)


def _build_restart_supervisor_env(
    *,
    launch_command: list[str],
    launch_env: dict[str, str],
    launch_cwd: str,
) -> dict[str, str]:
    host = "127.0.0.1"
    port = 8000
    try:
        host_index = launch_command.index("--host")
        host = str(launch_command[host_index + 1] or host)
    except (ValueError, IndexError, TypeError):
        host = "127.0.0.1"
    try:
        port_index = launch_command.index("--port")
        port = int(launch_command[port_index + 1] or port)
    except (ValueError, IndexError, TypeError):
        port = 8000

    helper_env = dict(launch_env)
    helper_env["MEMORY_PALACE_RESTART_CWD"] = launch_cwd
    helper_env["MEMORY_PALACE_RESTART_HOST"] = host
    helper_env["MEMORY_PALACE_RESTART_PORT"] = str(port)
    helper_env[_RESTART_HELPER_WAIT_TIMEOUT_ENV] = str(
        _env_float(_RESTART_WAIT_TIMEOUT_ENV, 30.0, minimum=1.0)
    )
    helper_env[_RESTART_HELPER_POLL_INTERVAL_ENV] = str(
        _env_float(_RESTART_POLL_INTERVAL_ENV, 0.25, minimum=0.05)
    )
    helper_env[_RESTART_HELPER_LOG_PATH_ENV] = _build_restart_supervisor_log_path(
        launch_cwd
    )
    return helper_env


def _schedule_restart_supervisor(
    *,
    launch_command: list[str],
    launch_env: dict[str, str],
    launch_cwd: str,
) -> None:
    helper_code = (
        "import os, socket, subprocess, sys, time\n"
        "def _log(message):\n"
        "    path = str(os.environ.get('MEMORY_PALACE_RESTART_LOG_PATH') or '').strip()\n"
        "    if not path:\n"
        "        return\n"
        "    try:\n"
        "        with open(path, 'a', encoding='utf-8') as fh:\n"
        "            fh.write(message + '\\n')\n"
        "    except Exception:\n"
        "        pass\n"
        "command = sys.argv[1:]\n"
        "cwd = os.environ['MEMORY_PALACE_RESTART_CWD']\n"
        "host = os.environ['MEMORY_PALACE_RESTART_HOST']\n"
        "port = int(os.environ['MEMORY_PALACE_RESTART_PORT'])\n"
        "wait_timeout = max(1.0, float(os.environ.get('MEMORY_PALACE_RESTART_WAIT_TIMEOUT_SEC', '30')))\n"
        "poll_interval = max(0.05, float(os.environ.get('MEMORY_PALACE_RESTART_POLL_INTERVAL_SEC', '0.25')))\n"
        "family = socket.AF_INET6 if ':' in host and host != 'localhost' else socket.AF_INET\n"
        "address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)\n"
        "time.sleep(0.5)\n"
        "deadline = time.monotonic() + wait_timeout\n"
        "while time.monotonic() < deadline:\n"
        "    sock = socket.socket(family, socket.SOCK_STREAM)\n"
        "    sock.settimeout(0.5)\n"
        "    try:\n"
        "        if sock.connect_ex(address) != 0:\n"
        "            break\n"
        "    finally:\n"
        "        sock.close()\n"
        "    time.sleep(poll_interval)\n"
        "else:\n"
        "    _log(f'timed out waiting for backend port to close: {host}:{port}')\n"
        "    raise SystemExit(1)\n"
        "try:\n"
        "    subprocess.Popen(command, cwd=cwd, env=dict(os.environ), start_new_session=True)\n"
        "except Exception as exc:\n"
        "    _log(f'failed to restart backend: {exc!r}')\n"
        "    raise\n"
    )
    helper_env = _build_restart_supervisor_env(
        launch_command=launch_command,
        launch_env=launch_env,
        launch_cwd=launch_cwd,
    )
    with open(os.devnull, "wb") as devnull:
        subprocess.Popen(
            [sys.executable, "-c", helper_code, *launch_command],
            cwd=launch_cwd,
            env=helper_env,
            stdout=devnull,
            stderr=devnull,
            start_new_session=True,
            close_fds=True,
        )


def _restart_local_backend_background(
    *,
    launch_command: list[str],
    launch_env: dict[str, str],
    launch_cwd: str,
) -> None:
    _schedule_restart_supervisor(
        launch_command=launch_command,
        launch_env=launch_env,
        launch_cwd=launch_cwd,
    )
    _terminate_self_for_restart()


def _terminate_self_for_restart() -> None:
    if sys.platform == "win32":
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break is not None:
            try:
                os.kill(os.getpid(), ctrl_break)
                return
            except OSError:
                pass
    os.kill(os.getpid(), signal.SIGTERM)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    logger.info("Memory API starting...")

    # Initialize SQLite
    try:
        await initialize_backend_runtime()
        _mount_embedded_sse_apps(app)
        logger.info("SQLite database initialized.")
    except Exception as e:
        logger.error("Failed to initialize SQLite: %s", e)
        raise RuntimeError("Failed to initialize SQLite during startup") from e

    if _allow_insecure_local_without_api_key() and not _get_configured_mcp_api_key():
        logger.warning(
            "SECURITY: MCP_API_KEY_ALLOW_INSECURE_LOCAL is enabled but no API key "
            "is configured. All loopback requests bypass authentication. "
            "Set MCP_API_KEY for production use."
        )

    yield

    # 关闭时
    logger.info("Closing database connections...")
    try:
        from mcp_server import drain_pending_flush_summaries

        await drain_pending_flush_summaries(reason="runtime.shutdown")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Best-effort flush drain skipped: %s", type(exc).__name__)
    await runtime_state.shutdown()
    await close_sqlite_client()


app = FastAPI(
    title="Memory Palace API",
    description="AI Agent 长期记忆系统后端",
    version="1.0.1",
    lifespan=lifespan
)

# CORS设置
_cors_origins, _cors_allow_credentials = _resolve_cors_config()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(review_router)
app.include_router(browse_router)
app.include_router(maintenance_router)


@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "Memory Palace API",
        "version": "1.0.1",
        "docs": "/docs"
    }


@app.get("/health")
async def health(
    request: Request,
    x_mcp_api_key: Optional[str] = Header(default=None, alias="X-MCP-API-Key"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
):
    """健康检查"""
    payload: Dict[str, Any] = {
        "status": "ok",
        "timestamp": _utc_iso_now(),
    }
    if not _health_request_allows_details(
        request,
        x_mcp_api_key=x_mcp_api_key,
        authorization=authorization,
    ):
        return payload

    try:
        sqlite_client = get_sqlite_client()
        index_payload: Optional[Dict[str, Any]] = None

        for method_name in (
            "get_index_status",
            "index_status",
            "get_retrieval_status",
            "get_search_index_status",
        ):
            method = getattr(sqlite_client, method_name, None)
            if not callable(method):
                continue
            try:
                result = method()
                if inspect.isawaitable(result):
                    result = await result
            except TypeError as exc:
                message = str(exc)
                if (
                    "unexpected keyword argument" in message
                    or "required positional argument" in message
                ):
                    continue
                raise

            index_payload = result if isinstance(result, dict) else {"raw_status": result}
            index_payload.setdefault("index_available", True)
            index_payload.setdefault("degraded", False)
            index_payload["source"] = f"sqlite_client.{method_name}"
            break

        if index_payload is None:
            # Lightweight fallback: count rows per domain instead of loading every path object.
            from sqlalchemy import func as sa_func, select as sa_select
            from db.sqlite_client import Path as PathModel, Memory as MemoryModel

            domain_counts: Dict[str, int] = {}
            total_paths = 0
            async with sqlite_client.session() as session:
                rows = (
                    await session.execute(
                        sa_select(PathModel.domain, sa_func.count())
                        .join(MemoryModel, PathModel.memory_id == MemoryModel.id)
                        .where(MemoryModel.deprecated == False)
                        .group_by(PathModel.domain)
                    )
                ).all()
                for domain_val, cnt in rows:
                    domain_counts[str(domain_val)] = int(cnt)
                    total_paths += int(cnt)

            index_payload = {
                "index_available": False,
                "degraded": True,
                "reason": "sqlite_client index status API unavailable; fallback stats only.",
                "source": "api.health.fallback",
                "stats": {
                    "total_paths": total_paths,
                    "domain_counts": domain_counts,
                },
            }

        payload["index"] = index_payload
        payload["runtime"] = {
            "write_lanes": await runtime_state.write_lanes.status(),
            "index_worker": await runtime_state.index_worker.status(),
        }
        if index_payload.get("degraded"):
            payload["status"] = "degraded"

    except Exception as exc:
        error_type = type(exc).__name__
        payload["status"] = "degraded"
        payload["index"] = {
            "index_available": False,
            "degraded": True,
            "reason": "internal_error",
            "error_type": error_type,
            "source": "api.health.exception",
        }
        payload["runtime"] = {
            "write_lanes": {"degraded": True, "reason": "internal_error"},
            "index_worker": {"degraded": True, "reason": "internal_error"},
        }

    if payload.get("status") == "degraded":
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/bootstrap/status")
async def bootstrap_status(
    request: Request,
    _auth: None = Depends(require_bootstrap_access),
):
    try:
        installer = _resolve_bootstrap_installer()
        payload = installer.bootstrap_status(**_current_bootstrap_context())
        if isinstance(payload, dict):
            setup_payload = payload.get("setup")
            if isinstance(setup_payload, dict):
                setup_payload["restartSupported"] = _restart_supported_for_request(request)
        return payload
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "bootstrap_unavailable",
                "reason": "installer_missing",
                "message": str(exc),
            },
        ) from exc


@app.post("/bootstrap/apply")
async def bootstrap_apply(
    payload: BootstrapApplyRequest,
    request: Request,
    _auth: None = Depends(require_bootstrap_access),
):
    try:
        installer = _resolve_bootstrap_installer()
        report = installer.perform_setup(
            **_current_bootstrap_context(),
            mode=payload.mode,
            profile=payload.profile,
            transport=payload.transport,
            reconfigure=payload.reconfigure,
            database_path=payload.databasePath,
            sse_url=payload.sseUrl,
            mcp_api_key=payload.mcpApiKey,
            allow_insecure_local=payload.allowInsecureLocal,
            embedding_api_base=payload.embeddingApiBase,
            embedding_api_key=payload.embeddingApiKey,
            embedding_model=payload.embeddingModel,
            embedding_dim=payload.embeddingDim,
            reranker_api_base=payload.rerankerApiBase,
            reranker_api_key=payload.rerankerApiKey,
            reranker_model=payload.rerankerModel,
            llm_api_base=payload.llmApiBase,
            llm_api_key=payload.llmApiKey,
            llm_model=payload.llmModel,
            write_guard_llm_api_base=payload.writeGuardLlmApiBase,
            write_guard_llm_api_key=payload.writeGuardLlmApiKey,
            write_guard_llm_model=payload.writeGuardLlmModel,
            compact_gist_llm_api_base=payload.compactGistLlmApiBase,
            compact_gist_llm_api_key=payload.compactGistLlmApiKey,
            compact_gist_llm_model=payload.compactGistLlmModel,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "bootstrap_invalid_request",
                "reason": "validation_failed",
                "message": str(exc),
            },
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "bootstrap_runtime_unavailable",
                "reason": "runtime_bootstrap_failed",
                "message": str(exc),
            },
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "bootstrap_apply_failed",
                "reason": "subprocess_failed",
                "message": _bootstrap_subprocess_failure_message(exc),
            },
        ) from exc

    def _mask_api_key(key: str) -> str | None:
        if not key:
            return None
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "*" * (len(key) - 4)

    restart_supported = _restart_supported_for_request(request)
    setup_payload = report.get("setup") if isinstance(report.get("setup"), dict) else {}
    if isinstance(setup_payload, dict):
        setup_payload["restartSupported"] = restart_supported
    validation = None
    if payload.validateAfterApply:
        validation = await _run_post_setup_validation_async(
            installer,
            config_path=Path(str(report.get("config_path") or "")).expanduser().resolve(),
        )
    env_values = {}
    env_file = str(report.get("env_file") or "").strip()
    if env_file:
        try:
            env_values = installer.load_env_file(Path(env_file).expanduser().resolve())
        except Exception:
            env_values = {}
    reindex_gate = report.get("reindexGate")
    if not isinstance(reindex_gate, dict):
        reindex_gate = {
            "required": bool(report.get("reindex_required")),
            "reasonKeys": report.get("reindex_reason_keys") if isinstance(report.get("reindex_reason_keys"), list) else [],
            "recommendedAction": "reindex_all" if report.get("reindex_required") else None,
        }
    response = {
        "ok": bool(report.get("ok")),
        "summary": str(report.get("summary") or ""),
        "effectiveProfile": str(report.get("effective_profile") or "b"),
        "fallbackApplied": bool(report.get("fallback_applied")),
        "restartRequired": bool(report.get("restart_required")),
        "restartSupported": restart_supported,
        "reindexGate": reindex_gate,
        "maintenanceApiKey": _mask_api_key(str(env_values.get("MCP_API_KEY") or "").strip()),
        "maintenanceApiKeySet": bool(str(env_values.get("MCP_API_KEY") or "").strip()),
        "maintenanceApiKeyMode": "header",
        "warnings": report.get("warnings") if isinstance(report.get("warnings"), list) else [],
        "actions": report.get("actions") if isinstance(report.get("actions"), list) else [],
        "nextSteps": report.get("next_steps") if isinstance(report.get("next_steps"), list) else [],
        "setup": setup_payload,
    }
    if validation is not None:
        response["validation"] = validation
    return response


@app.post("/bootstrap/restart")
async def bootstrap_restart(
    request: Request,
    background_tasks: BackgroundTasks,
    _auth: None = Depends(require_bootstrap_access),
):
    global _last_restart_ts
    if not _restart_supported_for_request(request):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "bootstrap_restart_unsupported",
                "reason": "loopback_only_local_backend",
            },
        )

    now = time.monotonic()
    elapsed = now - _last_restart_ts
    if _last_restart_ts > 0 and elapsed < _RESTART_COOLDOWN_SECONDS:
        remaining = int(_RESTART_COOLDOWN_SECONDS - elapsed) + 1
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "bootstrap_restart_cooldown",
                "reason": "too_soon",
                "message": f"Restart cooldown active. Try again in {remaining}s.",
                "retry_after_seconds": remaining,
            },
        )
    try:
        installer = _resolve_bootstrap_installer()
        launch_env, env_file = _build_restart_env(installer)
        launch_command = _build_local_restart_command(request)
        launch_cwd = str(Path(__file__).resolve().parent)
        background_tasks.add_task(
            _restart_local_backend_background,
            launch_command=launch_command,
            launch_env=launch_env,
            launch_cwd=launch_cwd,
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "bootstrap_restart_unavailable",
                "reason": "installer_missing",
                "message": str(exc),
            },
        ) from exc

    # Only record cooldown timestamp AFTER restart was successfully scheduled.
    _last_restart_ts = now

    return {
        "ok": True,
        "restartAccepted": True,
        "message": "Local backend restart scheduled.",
        "envFile": env_file,
        "restartSupported": True,
    }


@app.post("/bootstrap/provider-probe")
async def bootstrap_provider_probe(
    payload: BootstrapProviderProbeRequest,
    _auth: None = Depends(require_bootstrap_access),
):
    try:
        installer = _resolve_bootstrap_installer()
        provider_probe = installer.preview_provider_probe_status(
            **_current_bootstrap_context(),
            mode=payload.mode,
            profile=payload.profile,
            transport=payload.transport,
            sse_url=payload.sseUrl,
            mcp_api_key=payload.mcpApiKey,
            allow_insecure_local=payload.allowInsecureLocal,
            embedding_api_base=payload.embeddingApiBase,
            embedding_api_key=payload.embeddingApiKey,
            embedding_model=payload.embeddingModel,
            embedding_dim=payload.embeddingDim,
            reranker_api_base=payload.rerankerApiBase,
            reranker_api_key=payload.rerankerApiKey,
            reranker_model=payload.rerankerModel,
            llm_api_base=payload.llmApiBase,
            llm_api_key=payload.llmApiKey,
            llm_model=payload.llmModel,
            write_guard_llm_api_base=payload.writeGuardLlmApiBase,
            write_guard_llm_api_key=payload.writeGuardLlmApiKey,
            write_guard_llm_model=payload.writeGuardLlmModel,
            compact_gist_llm_api_base=payload.compactGistLlmApiBase,
            compact_gist_llm_api_key=payload.compactGistLlmApiKey,
            compact_gist_llm_model=payload.compactGistLlmModel,
            persist=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "bootstrap_provider_probe_invalid_request",
                "reason": "validation_failed",
                "message": str(exc),
            },
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "bootstrap_provider_probe_unavailable",
                "reason": "runtime_unavailable",
                "message": str(exc),
            },
        ) from exc

    probe_summary_status = str(provider_probe.get("summaryStatus") or "").strip().lower()
    probe_ok = probe_summary_status == "pass" if probe_summary_status else bool(provider_probe.get("ok", True))

    return {
        "ok": probe_ok,
        "providerProbe": provider_probe,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=_resolve_backend_bind_host(),
        port=_resolve_backend_bind_port(),
    )
