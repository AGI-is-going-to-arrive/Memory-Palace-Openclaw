import os
import sys
import hmac
import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import math
import socket
import threading
import time
import uvicorn
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple
from urllib.parse import parse_qsl

from anyio import ClosedResourceError
from filelock import FileLock, Timeout as FileLockTimeout
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

# Ensure we can import from backend dir
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from env_utils import env_float as _env_float, env_int as _env_int
from mcp_server import mcp, shutdown as mcp_shutdown, startup as mcp_startup
from db.sqlite_paths import extract_sqlite_file_path
from filesystem_utils import warn_if_unreliable_file_lock_path

_MCP_API_KEY_ENV = "MCP_API_KEY"
_MCP_API_KEY_HEADER = "X-MCP-API-Key"
_MCP_API_KEY_ALLOW_INSECURE_LOCAL_ENV = "MCP_API_KEY_ALLOW_INSECURE_LOCAL"
_SSE_RATE_LIMIT_WINDOW_SECONDS_ENV = "SSE_RATE_LIMIT_WINDOW_SECONDS"
_SSE_RATE_LIMIT_MAX_REQUESTS_ENV = "SSE_RATE_LIMIT_MAX_REQUESTS"
_SSE_RATE_LIMIT_STATE_FILE_ENV = "SSE_RATE_LIMIT_STATE_FILE"
_SSE_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS_ENV = "SSE_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS"
_SSE_MESSAGE_MAX_BODY_BYTES_ENV = "SSE_MESSAGE_MAX_BODY_BYTES"
_SSE_HEARTBEAT_PING_SECONDS_ENV = "SSE_HEARTBEAT_PING_SECONDS"
_DEFAULT_SSE_PORT = 8000
_LOOPBACK_FALLBACK_SSE_PORT = 8010
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on", "enabled"}
_LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost"}
_TRANSPORT_SECURITY_PATHS = {
    "/sse",
    "/sse/",
    "/messages",
    "/messages/",
    "/sse/messages",
    "/sse/messages/",
}
_FORWARDED_HEADER_NAMES = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-port",
    "x-real-ip",
    "x-client-ip",
    "true-client-ip",
    "cf-connecting-ip",
}
logger = logging.getLogger(__name__)


class _SseRequestBodyTooLargeError(RuntimeError):
    def __init__(self, *, limit_bytes: int, received_bytes: int) -> None:
        super().__init__("SSE request body exceeded the configured limit.")
        self.limit_bytes = max(1, int(limit_bytes))
        self.received_bytes = max(0, int(received_bytes))


class _SseSlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        window_seconds: int,
        max_requests: int,
        state_file: Optional[Path] = None,
        state_lock_timeout_seconds: float = 1.0,
        max_state_keys: int = 4096,
    ) -> None:
        self._window_seconds = max(1, int(window_seconds))
        self._max_requests = max(1, int(max_requests))
        resolved_state_file = Path(state_file).expanduser().resolve() if state_file else None
        if resolved_state_file is not None:
            is_network_filesystem, _ = warn_if_unreliable_file_lock_path(
                resolved_state_file,
                label="SSE rate limit state file",
                log=logger,
            )
            if is_network_filesystem:
                resolved_state_file = None
        self._state_file = resolved_state_file
        self._state_lock_timeout_seconds = max(0.1, float(state_lock_timeout_seconds))
        self._max_state_keys = max(32, int(max_state_keys))
        self._guard = threading.Lock()
        self._buckets: Dict[str, Deque[float]] = {}

    def _clock_now(self) -> float:
        # Persisted buckets must survive process and host restarts, so they use
        # wall-clock timestamps instead of a monotonic counter.
        if self._state_file is not None:
            return time.time()
        return time.monotonic()

    def check_and_record(self, identifier: str) -> Dict[str, int | bool]:
        now = self._clock_now()
        key = str(identifier or "unknown").strip() or "unknown"

        if self._state_file is not None:
            state_result = self._check_and_record_with_state_file(key, now)
            if state_result is not None:
                return state_result

        with self._guard:
            bucket = self._buckets.setdefault(key, deque())
            result = self._evaluate_and_record_bucket(bucket=bucket, key=key, now=now)
            result["storage"] = "process_memory"
            return result

    def _evaluate_and_record_bucket(
        self,
        *,
        bucket: Deque[float],
        key: str,
        now: float,
    ) -> Dict[str, int | bool | str]:
        future_cutoff = now + float(self._window_seconds)
        cutoff = now - float(self._window_seconds)
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        while bucket and bucket[-1] > future_cutoff:
            bucket.pop()

        if len(bucket) >= self._max_requests:
            retry_after_seconds = max(
                1,
                int(math.ceil(bucket[0] + float(self._window_seconds) - now)),
            )
            return {
                "allowed": False,
                "retry_after_seconds": retry_after_seconds,
                "limit": self._max_requests,
                "window_seconds": self._window_seconds,
                "storage": "process_memory" if self._state_file is None else "state_file",
                "key": key,
            }

        bucket.append(now)
        return {
            "allowed": True,
            "remaining": max(0, self._max_requests - len(bucket)),
            "limit": self._max_requests,
            "window_seconds": self._window_seconds,
            "storage": "process_memory" if self._state_file is None else "state_file",
            "key": key,
        }

    def _check_and_record_with_state_file(
        self,
        key: str,
        now: float,
    ) -> Optional[Dict[str, int | bool | str]]:
        state_file = self._state_file
        if state_file is None:
            return None

        lock_file = Path(f"{state_file}.lock")
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(lock_file), timeout=self._state_lock_timeout_seconds):
                try:
                    payload = self._load_state_payload(state_file)
                except FileNotFoundError:
                    logger.warning(
                        "SSE rate limit state file disappeared during locked read; "
                        "recreating it from an empty payload: %s",
                        state_file,
                    )
                    payload = {}
                bucket = deque(
                    float(item)
                    for item in payload.get(key, [])
                    if isinstance(item, (int, float)) and math.isfinite(float(item))
                )
                result = self._evaluate_and_record_bucket(bucket=bucket, key=key, now=now)
                payload[key] = list(bucket)
                self._prune_state_payload(payload, now=now, protected_keys={key})
                self._write_state_payload(state_file, payload)
                with self._guard:
                    self._buckets[key] = deque(bucket)
                result["storage"] = "state_file"
                return result
        except FileNotFoundError:
            logger.warning(
                "SSE rate limit state file disappeared during locked update; "
                "falling back to process memory for this request: %s",
                state_file,
            )
            return None
        except (FileLockTimeout, OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _load_state_payload(state_file: Path) -> Dict[str, list[float]]:
        if not state_file.exists():
            return {}
        raw = json.loads(state_file.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _write_state_payload(state_file: Path, payload: Dict[str, list[float]]) -> None:
        tmp_path = state_file.with_suffix(f"{state_file.suffix}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp_path, state_file)

    def _prune_state_payload(
        self,
        payload: Dict[str, list[float]],
        *,
        now: float,
        protected_keys: set[str],
    ) -> None:
        future_cutoff = now + float(self._window_seconds)
        cutoff = now - float(self._window_seconds)
        stale_keys: list[str] = []
        latest_ts_by_key: Dict[str, float] = {}
        for key, raw_values in list(payload.items()):
            if not isinstance(raw_values, list):
                stale_keys.append(key)
                continue
            cleaned = [
                float(item)
                for item in raw_values
                if (
                    isinstance(item, (int, float))
                    and math.isfinite(float(item))
                    and cutoff < float(item) <= future_cutoff
                )
            ]
            if cleaned:
                payload[key] = cleaned
                latest_ts_by_key[key] = cleaned[-1]
            elif key not in protected_keys:
                stale_keys.append(key)
        for key in stale_keys:
            payload.pop(key, None)
        while len(payload) > self._max_state_keys:
            candidates = [
                (candidate_key, latest_ts_by_key.get(candidate_key, float("-inf")))
                for candidate_key in payload.keys()
                if candidate_key not in protected_keys
            ]
            if not candidates:
                break
            oldest_key = min(candidates, key=lambda item: item[1])[0]
            payload.pop(oldest_key, None)


def _is_loopback_port_available(port: int) -> bool:
    attempted_probe = False
    for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                attempted_probe = True
                if family == socket.AF_INET6 and hasattr(socket, "IPV6_V6ONLY"):
                    try:
                        probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    except OSError:
                        pass
                probe.bind((host, port))
        except OSError as exc:
            if exc.errno in {
                errno.EADDRNOTAVAIL,
                errno.EAFNOSUPPORT,
                errno.EPROTONOSUPPORT,
            }:
                continue
            return False
    if not attempted_probe:
        return True
    return True


def _format_http_host_for_display(host: str) -> str:
    normalized = str(host or "").strip() or "127.0.0.1"
    if ":" in normalized and not normalized.startswith("[") and not normalized.endswith("]"):
        return f"[{normalized}]"
    return normalized


def _resolve_sse_port(host: str) -> int:
    raw_port = str(os.getenv("PORT") or "").strip()
    if raw_port:
        return _env_int("PORT", _DEFAULT_SSE_PORT, minimum=1)

    normalized_host = str(host or "").strip().lower()
    if (
        normalized_host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
        and not _is_loopback_port_available(_DEFAULT_SSE_PORT)
    ):
        display_host = _format_http_host_for_display(normalized_host or host)
        logger.warning(
            "Loopback port %s is already in use; falling back to %s. "
            "Update MCP client config to http://%s:%s/sse or set PORT explicitly.",
            _DEFAULT_SSE_PORT, _LOOPBACK_FALLBACK_SSE_PORT,
            display_host, _LOOPBACK_FALLBACK_SSE_PORT,
        )
        return _LOOPBACK_FALLBACK_SSE_PORT

    return _DEFAULT_SSE_PORT


def _get_configured_mcp_api_key() -> str:
    return str(os.getenv(_MCP_API_KEY_ENV) or "").strip()


def _allow_insecure_local_without_api_key() -> bool:
    value = str(os.getenv(_MCP_API_KEY_ALLOW_INSECURE_LOCAL_ENV) or "").strip().lower()
    return value in _TRUTHY_ENV_VALUES


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not isinstance(authorization, str):
        return None
    value = authorization.strip()
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token if token else None


def _is_loopback_scope(scope: Scope) -> bool:
    client = scope.get("client")
    host = ""
    if isinstance(client, tuple) and client:
        host = str(client[0] or "").strip().lower()
    elif client is not None:
        host = str(getattr(client, "host", "") or "").strip().lower()
    if host not in _LOOPBACK_CLIENT_HOSTS:
        return False

    headers = Headers(scope=scope)
    for header_name in _FORWARDED_HEADER_NAMES:
        header_value = headers.get(header_name)
        if isinstance(header_value, str) and header_value.strip():
            return False
    return True


def _should_suppress_stream_shutdown_runtime_error(scope: Scope, exc: RuntimeError) -> bool:
    if scope.get("type") != "http":
        return False
    path = str(scope.get("path") or "")
    if path not in _TRANSPORT_SECURITY_PATHS:
        return False
    message = str(exc)
    return (
        "Expected ASGI message 'http.response.body'" in message
        and "'http.response.start'" in message
    )


def _should_suppress_closed_resource_error(scope: Scope) -> bool:
    if scope.get("type") != "http":
        return False
    path = str(scope.get("path") or "")
    return path in {"/sse", "/sse/"}


def _matches_transport_security_pattern(value: str | None, patterns: list[str]) -> bool:
    if not value:
        return False
    if value in patterns:
        return True
    for pattern in patterns:
        if pattern.endswith(":*") and value.startswith(pattern[:-2] + ":"):
            return True
    return False


def _should_suppress_transport_security_validation_error(
    scope: Scope,
    exc: ValueError,
    *,
    response_started: bool,
) -> bool:
    if not response_started or scope.get("type") != "http":
        return False
    if str(exc) != "Request validation failed":
        return False

    path = str(scope.get("path") or "")
    if path not in _TRANSPORT_SECURITY_PATHS:
        return False

    settings = getattr(mcp.settings, "transport_security", None)
    if settings is None or not settings.enable_dns_rebinding_protection:
        return False

    headers = Headers(scope=scope)
    host = headers.get("host")
    if not _matches_transport_security_pattern(host, settings.allowed_hosts):
        return True

    origin = headers.get("origin")
    if origin and not _matches_transport_security_pattern(origin, settings.allowed_origins):
        return True

    return False


def _resolve_rate_limit_identifier(scope: Scope) -> str:
    headers = Headers(scope=scope)
    token = str(headers.get(_MCP_API_KEY_HEADER, "")).strip() or _extract_bearer_token(
        headers.get("Authorization")
    )
    if token:
        digest = hashlib.sha256(token.encode("utf-8", errors="ignore")).hexdigest()
        return f"auth:{digest}"

    for header_name in (
        "x-forwarded-for",
        "x-real-ip",
        "x-client-ip",
        "true-client-ip",
        "cf-connecting-ip",
    ):
        header_value = str(headers.get(header_name) or "").strip()
        if not header_value:
            continue
        if header_name == "x-forwarded-for":
            first = str(header_value.split(",", 1)[0]).strip()
            if first:
                return first.lower()
            continue
        return header_value.lower()

    client = scope.get("client")
    host = ""
    if isinstance(client, tuple) and client:
        host = str(client[0] or "").strip().lower()
    elif client is not None:
        host = str(getattr(client, "host", "") or "").strip().lower()
    if host:
        return host

    query_string = scope.get("query_string")
    if isinstance(query_string, bytes) and query_string:
        try:
            for key, value in parse_qsl(query_string.decode("utf-8", errors="ignore")):
                if str(key).strip().lower() == "session_id":
                    rendered = str(value or "").strip()
                    if rendered:
                        return f"session:{rendered}"
        except ValueError:
            pass
    return "unknown"


def _resolve_rate_limit_state_file() -> Optional[Path]:
    explicit = str(os.getenv(_SSE_RATE_LIMIT_STATE_FILE_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    database_path = extract_sqlite_file_path(os.getenv("DATABASE_URL"))
    if database_path is None:
        return None
    return database_path.with_name(f"{database_path.name}.sse_rate_limit_state.json")


def _should_apply_rate_limit(scope: Scope) -> bool:
    method = str(scope.get("method") or "").strip().upper()
    path = str(scope.get("path") or "")
    if method in {"HEAD", "OPTIONS"}:
        return False
    if method == "GET" and path in {"/sse", "/sse/"} and _is_loopback_scope(scope):
        return False
    return True


def _wrap_receive_with_body_limit(
    receive: Receive,
    *,
    max_body_bytes: int,
) -> Receive:
    total_bytes = 0

    async def _limited_receive():
        nonlocal total_bytes
        message = await receive()
        if message.get("type") != "http.request":
            return message
        body = message.get("body", b"") or b""
        total_bytes += len(body)
        if total_bytes > max_body_bytes:
            raise _SseRequestBodyTooLargeError(
                limit_bytes=max_body_bytes,
                received_bytes=total_bytes,
            )
        return message

    return _limited_receive


def apply_mcp_api_key_middleware(app: ASGIApp) -> ASGIApp:
    rate_limiter = _SseSlidingWindowRateLimiter(
        window_seconds=_env_int(_SSE_RATE_LIMIT_WINDOW_SECONDS_ENV, 60, minimum=1),
        max_requests=_env_int(_SSE_RATE_LIMIT_MAX_REQUESTS_ENV, 60, minimum=1),
        state_file=_resolve_rate_limit_state_file(),
        state_lock_timeout_seconds=_env_float(
            _SSE_RATE_LIMIT_STATE_LOCK_TIMEOUT_SECONDS_ENV,
            1.0,
            minimum=0.1,
        ),
    )
    max_body_bytes = _env_int(
        _SSE_MESSAGE_MAX_BODY_BYTES_ENV,
        1_048_576,
        minimum=1,
    )
    heartbeat_interval_seconds = _env_float(
        _SSE_HEARTBEAT_PING_SECONDS_ENV,
        25.0,
        minimum=0.0,
    )

    async def _auth_middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await app(scope, receive, send)
            return

        response_started = False
        response_completed = asyncio.Event()
        send_guard = asyncio.Lock()
        heartbeat_task: Optional[asyncio.Task[None]] = None
        last_body_sent_at = time.monotonic()
        limited_receive = _wrap_receive_with_body_limit(
            receive,
            max_body_bytes=max_body_bytes,
        )

        async def _tracking_send(message) -> None:
            nonlocal response_started, heartbeat_task, last_body_sent_at
            if message.get("type") == "http.response.start":
                response_started = True
                async with send_guard:
                    await send(message)
                if (
                    heartbeat_interval_seconds > 0
                    and _is_sse_response_start(message)
                    and heartbeat_task is None
                ):
                    heartbeat_task = asyncio.create_task(_heartbeat_sender())
                return
            if message.get("type") == "http.response.body":
                last_body_sent_at = time.monotonic()
                if not message.get("more_body", False):
                    response_completed.set()
                async with send_guard:
                    await send(message)
                return
            async with send_guard:
                await send(message)

        async def _heartbeat_sender() -> None:
            nonlocal last_body_sent_at
            try:
                while not response_completed.is_set():
                    await asyncio.sleep(heartbeat_interval_seconds)
                    if response_completed.is_set():
                        return
                    if (time.monotonic() - last_body_sent_at) < heartbeat_interval_seconds:
                        continue
                    async with send_guard:
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b": ping\n\n",
                                "more_body": True,
                            }
                        )
                    last_body_sent_at = time.monotonic()
            except asyncio.CancelledError:
                return
            except ClosedResourceError:
                if _should_suppress_closed_resource_error(scope):
                    return
                raise
            except RuntimeError as exc:
                if _should_suppress_stream_shutdown_runtime_error(scope, exc):
                    return
                raise

        async def _forward_request() -> None:
            try:
                await app(scope, limited_receive, _tracking_send)
            except _SseRequestBodyTooLargeError as exc:
                if response_started:
                    raise
                response = JSONResponse(
                    status_code=413,
                    content={
                        "error": "mcp_sse_request_too_large",
                        "reason": "body_too_large",
                        "max_body_bytes": exc.limit_bytes,
                        "received_body_bytes": exc.received_bytes,
                    },
                )
                await response(scope, receive, send)
                return
            except ClosedResourceError:
                if _should_suppress_closed_resource_error(scope):
                    return
                raise
            except RuntimeError as exc:
                if _should_suppress_stream_shutdown_runtime_error(scope, exc):
                    return
                raise
            except ValueError as exc:
                if _should_suppress_transport_security_validation_error(
                    scope,
                    exc,
                    response_started=response_started,
                ):
                    return
                raise
            finally:
                response_completed.set()
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, ClosedResourceError):
                        await heartbeat_task

        configured = _get_configured_mcp_api_key()
        headers = Headers(scope=scope)
        content_length = str(headers.get("content-length") or "").strip()
        if content_length:
            try:
                if int(content_length) > max_body_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={
                            "error": "mcp_sse_request_too_large",
                            "reason": "body_too_large",
                            "max_body_bytes": max_body_bytes,
                        },
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass

        if not configured:
            if _allow_insecure_local_without_api_key() and _is_loopback_scope(scope):
                if _should_apply_rate_limit(scope):
                    limit_result = rate_limiter.check_and_record(
                        _resolve_rate_limit_identifier(scope)
                    )
                    if not bool(limit_result.get("allowed")):
                        retry_after_seconds = int(limit_result.get("retry_after_seconds") or 1)
                        response = JSONResponse(
                            status_code=429,
                            content={
                                "error": "mcp_sse_rate_limited",
                                "reason": "rate_limited",
                                "retry_after_seconds": retry_after_seconds,
                            },
                            headers={"Retry-After": str(retry_after_seconds)},
                        )
                        await response(scope, receive, send)
                        return
                await _forward_request()
                return
            reason = (
                "insecure_local_override_requires_loopback"
                if _allow_insecure_local_without_api_key()
                else "api_key_not_configured"
            )
            response = JSONResponse(
                status_code=401,
                content={
                    "error": "mcp_sse_auth_failed",
                    "reason": reason,
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        provided = (
            str(headers.get(_MCP_API_KEY_HEADER, "")).strip()
            or _extract_bearer_token(headers.get("Authorization"))
        )
        if not provided or not hmac.compare_digest(provided, configured):
            response = JSONResponse(
                status_code=401,
                content={
                    "error": "mcp_sse_auth_failed",
                    "reason": "invalid_or_missing_api_key",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        if _should_apply_rate_limit(scope):
            limit_result = rate_limiter.check_and_record(_resolve_rate_limit_identifier(scope))
            if not bool(limit_result.get("allowed")):
                retry_after_seconds = int(limit_result.get("retry_after_seconds") or 1)
                response = JSONResponse(
                    status_code=429,
                    content={
                        "error": "mcp_sse_rate_limited",
                        "reason": "rate_limited",
                        "retry_after_seconds": retry_after_seconds,
                    },
                    headers={"Retry-After": str(retry_after_seconds)},
                )
                await response(scope, receive, send)
                return
        await _forward_request()

    return _auth_middleware


def _is_sse_response_start(message: dict) -> bool:
    if message.get("type") != "http.response.start":
        return False
    for raw_name, raw_value in message.get("headers", []) or []:
        if bytes(raw_name).lower() != b"content-type":
            continue
        rendered = bytes(raw_value).decode("latin-1", errors="ignore").lower()
        if rendered.startswith("text/event-stream"):
            return True
    return False


def create_sse_app() -> ASGIApp:
    app = mcp.sse_app()
    return apply_mcp_api_key_middleware(app)


@contextlib.asynccontextmanager
async def _sse_server_lifespan(_app):
    await mcp_startup()
    try:
        yield
    finally:
        await mcp_shutdown()


def create_sse_server_app() -> ASGIApp:
    return Starlette(
        lifespan=_sse_server_lifespan,
        routes=[Mount("/", app=create_sse_app())],
    )


def _resolve_embedded_sse_route_components(base_app: ASGIApp) -> tuple[object, ASGIApp]:
    routes = tuple(getattr(base_app, "routes", []) or [])
    stream_route = None
    message_app = None
    for route in routes:
        path = str(getattr(route, "path", "") or "").rstrip("/") or "/"
        if stream_route is None and path == "/sse" and hasattr(route, "endpoint"):
            stream_route = route
        if message_app is None and path in {"/messages", "/sse/messages"} and hasattr(route, "app"):
            message_app = route.app

    if stream_route is not None and message_app is not None:
        return stream_route, message_app

    missing = []
    if stream_route is None:
        missing.append("/sse endpoint")
    if message_app is None:
        missing.append("/messages mount")
    available_paths = [
        str(getattr(route, "path", "") or "")
        for route in routes
        if str(getattr(route, "path", "") or "")
    ]
    available_rendered = ", ".join(available_paths) or "<none>"
    missing_rendered = ", ".join(missing)
    raise RuntimeError(
        "Missing embedded SSE routes from FastMCP sse_app(): "
        f"{missing_rendered}. Available routes: {available_rendered}"
    )


def create_embedded_sse_apps() -> Tuple[ASGIApp, ASGIApp]:
    base_app = mcp.sse_app()
    stream_route, message_app = _resolve_embedded_sse_route_components(base_app)
    stream_app = Starlette(
        debug=getattr(base_app, "debug", False),
        routes=[
            Route(
                "/",
                endpoint=stream_route.endpoint,
                methods=sorted(stream_route.methods or {"GET"}),
            )
        ],
    )
    return (
        apply_mcp_api_key_middleware(stream_app),
        apply_mcp_api_key_middleware(message_app),
    )


def main():
    """
    Run the Memory Palace MCP server using SSE (Server-Sent Events) transport.
    This is required for clients that don't support stdio (like some web-based tools).
    """
    host = os.getenv("HOST", "127.0.0.1")
    port = _resolve_sse_port(host)

    logger.info("Initializing Memory Palace SSE Server...")
    app = create_sse_server_app()

    logger.info("Starting SSE Server on http://%s:%s", host, port)
    logger.info("SSE Endpoint: http://%s:%s/sse", host, port)
    
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    main()
