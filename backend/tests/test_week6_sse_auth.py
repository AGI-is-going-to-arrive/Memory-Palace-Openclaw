import asyncio
from anyio import ClosedResourceError
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pathlib import Path
import json
import logging
import os
import pytest
import signal
import socket
import subprocess
import sys
import time
from starlette.applications import Starlette
from starlette.routing import Mount, Route

import run_sse
from run_sse import apply_mcp_api_key_middleware


_WINDOWS_NEW_PROCESS_GROUP = int(
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
) if os.name == "nt" else 0


def _build_client(*, client=("testclient", 50000)) -> TestClient:
    app = FastAPI()

    @app.api_route("/ping", methods=["GET", "POST"])
    async def ping(request: Request):
        if request.method == "POST":
            payload = await request.body()
            return {"ok": True, "size": len(payload)}
        return {"ok": True}

    wrapped_app = apply_mcp_api_key_middleware(app)
    return TestClient(wrapped_app, client=client)


def test_sse_auth_rejects_when_api_key_not_configured_by_default(monkeypatch) -> None:
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.delenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", raising=False)
    with _build_client() as client:
        response = client.get("/ping")
    assert response.status_code == 401
    payload = response.json()
    assert payload.get("error") == "mcp_sse_auth_failed"
    assert payload.get("reason") == "api_key_not_configured"


@pytest.mark.parametrize("override_value", ["true", "enabled"])
def test_sse_auth_allows_when_explicit_insecure_local_override_is_enabled(
    monkeypatch, override_value: str
) -> None:
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", override_value)
    with _build_client(client=("127.0.0.1", 50000)) as client:
        response = client.get("/ping")
    assert response.status_code == 200
    assert response.json().get("ok") is True


def test_sse_auth_rejects_insecure_local_override_for_non_loopback_client(monkeypatch) -> None:
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", "true")
    with _build_client(client=("203.0.113.10", 50000)) as client:
        response = client.get("/ping")
    assert response.status_code == 401
    payload = response.json()
    assert payload.get("error") == "mcp_sse_auth_failed"
    assert payload.get("reason") == "insecure_local_override_requires_loopback"


def test_sse_auth_rejects_insecure_local_override_when_forwarded_headers_present(monkeypatch) -> None:
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", "true")
    headers = {"X-Forwarded-For": "198.51.100.8"}
    with _build_client(client=("127.0.0.1", 50000)) as client:
        response = client.get("/ping", headers=headers)
    assert response.status_code == 401
    payload = response.json()
    assert payload.get("error") == "mcp_sse_auth_failed"
    assert payload.get("reason") == "insecure_local_override_requires_loopback"


def test_sse_auth_rejects_when_api_key_missing(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    with _build_client() as client:
        response = client.get("/ping")
    assert response.status_code == 401
    payload = response.json()
    assert payload.get("error") == "mcp_sse_auth_failed"
    assert payload.get("reason") == "invalid_or_missing_api_key"


def test_sse_auth_accepts_x_mcp_api_key_header(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    headers = {"X-MCP-API-Key": "week6-sse-secret"}
    with _build_client() as client:
        response = client.get("/ping", headers=headers)
    assert response.status_code == 200
    assert response.json().get("ok") is True


def test_sse_auth_accepts_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    headers = {"Authorization": "Bearer week6-sse-secret"}
    with _build_client() as client:
        response = client.get("/ping", headers=headers)
    assert response.status_code == 200
    assert response.json().get("ok") is True


def test_sse_auth_rate_limits_requests(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    monkeypatch.setenv("SSE_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("SSE_RATE_LIMIT_MAX_REQUESTS", "1")

    with _build_client(client=("127.0.0.1", 50000)) as client:
        first = client.get("/ping", headers={"X-MCP-API-Key": "week6-sse-secret"})
        second = client.get("/ping", headers={"X-MCP-API-Key": "week6-sse-secret"})

    assert first.status_code == 200
    assert second.status_code == 429
    payload = second.json()
    assert payload.get("error") == "mcp_sse_rate_limited"
    assert payload.get("reason") == "rate_limited"
    assert int(second.headers.get("Retry-After") or "0") >= 1


def test_sse_auth_rate_limits_by_authenticated_identity_even_when_session_ids_change(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    monkeypatch.setenv("SSE_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("SSE_RATE_LIMIT_MAX_REQUESTS", "1")

    with _build_client(client=("127.0.0.1", 50000)) as client:
        first = client.get(
            "/ping?session_id=alpha",
            headers={"X-MCP-API-Key": "week6-sse-secret"},
        )
        second = client.get(
            "/ping?session_id=beta",
            headers={"X-MCP-API-Key": "week6-sse-secret"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    payload = second.json()
    assert payload.get("error") == "mcp_sse_rate_limited"
    assert payload.get("reason") == "rate_limited"
    assert int(second.headers.get("Retry-After") or "0") >= 1


def test_sse_auth_rejects_requests_above_body_size_limit(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    monkeypatch.setenv("SSE_MESSAGE_MAX_BODY_BYTES", "16")

    with _build_client(client=("127.0.0.1", 50000)) as client:
        response = client.post(
            "/ping",
            headers={"X-MCP-API-Key": "week6-sse-secret"},
            content=b"x" * 17,
        )

    assert response.status_code == 413
    payload = response.json()
    assert payload.get("error") == "mcp_sse_request_too_large"
    assert payload.get("reason") == "body_too_large"
    assert payload.get("max_body_bytes") == 16


@pytest.mark.asyncio
async def test_sse_auth_rejects_chunked_requests_above_body_size_limit(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    monkeypatch.setenv("SSE_MESSAGE_MAX_BODY_BYTES", "5")

    async def _body_reader_app(scope, receive, send):
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                continue
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{\"ok\":true}'})

    wrapped_app = apply_mcp_api_key_middleware(_body_reader_app)
    sent_messages = []
    incoming = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )

    async def _receive():
        return next(incoming)

    async def _send(message):
        sent_messages.append(message)

    await wrapped_app(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/messages",
            "raw_path": b"/messages",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"x-mcp-api-key", b"week6-sse-secret"),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        },
        _receive,
        _send,
    )

    start = next(item for item in sent_messages if item.get("type") == "http.response.start")
    body = next(item for item in sent_messages if item.get("type") == "http.response.body")
    payload = json.loads((body.get("body") or b"{}").decode("utf-8"))

    assert start["status"] == 413
    assert payload.get("error") == "mcp_sse_request_too_large"
    assert payload.get("received_body_bytes") == 6


@pytest.mark.asyncio
async def test_sse_auth_rejects_chunked_requests_above_body_size_limit_for_insecure_local_override(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", "true")
    monkeypatch.setenv("SSE_MESSAGE_MAX_BODY_BYTES", "5")

    async def _body_reader_app(scope, receive, send):
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                continue
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    wrapped_app = apply_mcp_api_key_middleware(_body_reader_app)
    sent_messages = []
    incoming = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"456", "more_body": False},
        ]
    )

    async def _receive():
        return next(incoming)

    async def _send(message):
        sent_messages.append(message)

    await wrapped_app(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/messages",
            "raw_path": b"/messages",
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1"),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        },
        _receive,
        _send,
    )

    start = next(item for item in sent_messages if item.get("type") == "http.response.start")
    body = next(item for item in sent_messages if item.get("type") == "http.response.body")
    payload = json.loads((body.get("body") or b"{}").decode("utf-8"))

    assert start["status"] == 413
    assert payload.get("error") == "mcp_sse_request_too_large"
    assert payload.get("received_body_bytes") == 6


def test_sse_auth_rate_limits_insecure_local_override_requests(monkeypatch) -> None:
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.setenv("MCP_API_KEY_ALLOW_INSECURE_LOCAL", "true")
    monkeypatch.setenv("SSE_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("SSE_RATE_LIMIT_MAX_REQUESTS", "1")

    with _build_client(client=("127.0.0.1", 50000)) as client:
        first = client.get("/ping")
        second = client.get("/ping")

    assert first.status_code == 200
    assert second.status_code == 429
    payload = second.json()
    assert payload.get("error") == "mcp_sse_rate_limited"
    assert payload.get("reason") == "rate_limited"


def test_sse_rate_limiter_shares_state_file_across_instances(tmp_path: Path) -> None:
    state_file = tmp_path / "sse-rate-limit.json"
    limiter_a = run_sse._SseSlidingWindowRateLimiter(
        window_seconds=60,
        max_requests=1,
        state_file=state_file,
        state_lock_timeout_seconds=1.0,
    )
    limiter_b = run_sse._SseSlidingWindowRateLimiter(
        window_seconds=60,
        max_requests=1,
        state_file=state_file,
        state_lock_timeout_seconds=1.0,
    )

    first = limiter_a.check_and_record("session:alpha")
    second = limiter_b.check_and_record("session:alpha")
    third = limiter_b.check_and_record("session:beta")

    assert first["allowed"] is True
    assert first["storage"] == "state_file"
    assert second["allowed"] is False
    assert third["allowed"] is True


def test_sse_rate_limiter_persists_epoch_timestamps_for_state_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(run_sse.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(run_sse.time, "monotonic", lambda: 42.0)
    state_file = tmp_path / "sse-rate-limit.json"

    limiter = run_sse._SseSlidingWindowRateLimiter(
        window_seconds=60,
        max_requests=1,
        state_file=state_file,
        state_lock_timeout_seconds=1.0,
    )
    result = limiter.check_and_record("session:alpha")
    payload = json.loads(state_file.read_text(encoding="utf-8"))

    assert result["allowed"] is True
    assert payload["session:alpha"] == [1_700_000_000.0]


def test_sse_rate_limiter_prunes_future_state_file_timestamps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(run_sse.time, "time", lambda: 1_700_000_000.0)
    state_file = tmp_path / "sse-rate-limit.json"
    state_file.write_text(
        json.dumps({"session:alpha": [1_700_000_600.0]}),
        encoding="utf-8",
    )

    limiter = run_sse._SseSlidingWindowRateLimiter(
        window_seconds=60,
        max_requests=1,
        state_file=state_file,
        state_lock_timeout_seconds=1.0,
    )
    result = limiter.check_and_record("session:alpha")
    payload = json.loads(state_file.read_text(encoding="utf-8"))

    assert result["allowed"] is True
    assert payload["session:alpha"] == [1_700_000_000.0]


def test_sse_rate_limiter_prunes_legacy_monotonic_state_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(run_sse.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(run_sse.time, "monotonic", lambda: 5050.0)
    state_file = tmp_path / "sse-rate-limit.json"
    state_file.write_text(
        json.dumps({"session:alpha": [5000.0]}),
        encoding="utf-8",
    )

    limiter = run_sse._SseSlidingWindowRateLimiter(
        window_seconds=60,
        max_requests=1,
        state_file=state_file,
        state_lock_timeout_seconds=1.0,
    )
    result = limiter.check_and_record("session:alpha")
    payload = json.loads(state_file.read_text(encoding="utf-8"))

    assert result["allowed"] is True
    assert payload["session:alpha"] == [1_700_000_000.0]


def test_sse_rate_limiter_recovers_when_state_file_disappears_mid_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_file = tmp_path / "sse-rate-limit.json"
    load_calls = {"count": 0}
    original_load = run_sse._SseSlidingWindowRateLimiter._load_state_payload

    def flaky_load(path: Path) -> dict[str, list[float]]:
        load_calls["count"] += 1
        if load_calls["count"] == 1:
            raise FileNotFoundError(path)
        return original_load(path)

    monkeypatch.setattr(
        run_sse._SseSlidingWindowRateLimiter,
        "_load_state_payload",
        staticmethod(flaky_load),
    )
    limiter = run_sse._SseSlidingWindowRateLimiter(
        window_seconds=60,
        max_requests=1,
        state_file=state_file,
        state_lock_timeout_seconds=1.0,
    )

    with caplog.at_level(logging.WARNING):
        result = limiter.check_and_record("session:alpha")

    assert result["allowed"] is True
    assert result["storage"] == "state_file"
    assert json.loads(state_file.read_text(encoding="utf-8"))["session:alpha"]
    assert "disappeared during locked read" in caplog.text


def test_sse_rate_limiter_keeps_monotonic_clock_for_process_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_sse.time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(run_sse.time, "monotonic", lambda: 42.0)

    limiter = run_sse._SseSlidingWindowRateLimiter(
        window_seconds=60,
        max_requests=2,
        state_file=None,
        state_lock_timeout_seconds=1.0,
    )
    result = limiter.check_and_record("session:alpha")

    assert result["allowed"] is True
    assert list(limiter._buckets["session:alpha"]) == [42.0]


def test_sse_rate_limiter_degrades_network_state_file_to_process_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        run_sse,
        "warn_if_unreliable_file_lock_path",
        lambda path, *, label, log=None: (True, "nfs4"),
    )
    state_file = tmp_path / "sse-rate-limit.json"

    limiter = run_sse._SseSlidingWindowRateLimiter(
        window_seconds=60,
        max_requests=1,
        state_file=state_file,
        state_lock_timeout_seconds=1.0,
    )
    result = limiter.check_and_record("session:alpha")

    assert limiter._state_file is None
    assert result["allowed"] is True
    assert result["storage"] == "process_memory"


def test_sse_rate_limiter_falls_back_when_state_file_disappears_mid_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(run_sse.time, "time", lambda: 1_700_000_000.0)
    state_file = tmp_path / "sse-rate-limit.json"
    state_file.write_text(json.dumps({"session:alpha": []}), encoding="utf-8")

    limiter = run_sse._SseSlidingWindowRateLimiter(
        window_seconds=60,
        max_requests=2,
        state_file=state_file,
        state_lock_timeout_seconds=1.0,
    )

    def flaky_write(path: Path, payload: dict[str, list[float]]) -> None:
        _ = payload
        path.unlink(missing_ok=True)
        raise FileNotFoundError(str(path))

    monkeypatch.setattr(limiter, "_write_state_payload", flaky_write)

    with caplog.at_level(logging.WARNING):
        result = limiter.check_and_record("session:alpha")

    assert result["allowed"] is True
    assert result["storage"] == "process_memory"
    assert list(limiter._buckets["session:alpha"]) == [1_700_000_000.0]
    assert "state file disappeared during locked update" in caplog.text


def test_sse_rate_limit_prefers_authenticated_identity_over_session_id(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    monkeypatch.setenv("SSE_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("SSE_RATE_LIMIT_MAX_REQUESTS", "1")

    with _build_client(client=("203.0.113.10", 50000)) as client:
        first = client.post(
            "/ping?session_id=session-a",
            headers={"X-MCP-API-Key": "week6-sse-secret"},
            content=b"ok",
        )
        second = client.post(
            "/ping?session_id=session-b",
            headers={"X-MCP-API-Key": "week6-sse-secret"},
            content=b"ok",
        )
        third = client.post(
            "/ping?session_id=session-a",
            headers={"X-MCP-API-Key": "week6-sse-secret"},
            content=b"ok",
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert third.status_code == 429


def test_sse_auth_preserves_streaming_response(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")

    app = FastAPI()

    @app.get("/stream")
    async def stream():
        async def _events():
            yield "event: endpoint\n\n"
            yield "data: ok\n\n"

        return StreamingResponse(_events(), media_type="text/event-stream")

    wrapped_app = apply_mcp_api_key_middleware(app)
    with TestClient(wrapped_app) as client:
        with client.stream("GET", "/stream", headers={"X-MCP-API-Key": "week6-sse-secret"}) as response:
            assert response.status_code == 200
            lines = list(response.iter_lines())
    assert "event: endpoint" in lines
    assert "data: ok" in lines


def test_sse_auth_does_not_suppress_validation_error_before_response_starts(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")

    async def _broken_transport(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        raise ValueError("Request validation failed")

    wrapped_app = apply_mcp_api_key_middleware(_broken_transport)
    with TestClient(wrapped_app) as client:
        with pytest.raises(ValueError, match="Request validation failed"):
            client.get("/sse", headers={"X-MCP-API-Key": "week6-sse-secret"})


def test_sse_auth_does_not_suppress_validation_error_after_response_starts_for_valid_host(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")

    async def _broken_transport(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise ValueError("Request validation failed")

    wrapped_app = apply_mcp_api_key_middleware(_broken_transport)
    with TestClient(wrapped_app, client=("127.0.0.1", 50000)) as client:
        with pytest.raises(ValueError, match="Request validation failed"):
            client.get(
                "/sse",
                headers={
                    "X-MCP-API-Key": "week6-sse-secret",
                    "Host": "127.0.0.1",
                },
            )


def test_sse_auth_skips_rate_limit_for_loopback_sse_probes(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    monkeypatch.setenv("SSE_RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("SSE_RATE_LIMIT_MAX_REQUESTS", "1")

    app = FastAPI()

    @app.get("/sse")
    async def stream():
        async def _events():
            yield "event: endpoint\n\n"
            yield "data: ok\n\n"

        return StreamingResponse(_events(), media_type="text/event-stream")

    wrapped_app = apply_mcp_api_key_middleware(app)
    with TestClient(wrapped_app, client=("127.0.0.1", 50000)) as client:
        first = client.get("/sse", headers={"X-MCP-API-Key": "week6-sse-secret"})
        second = client.get("/sse", headers={"X-MCP-API-Key": "week6-sse-secret"})

    assert first.status_code == 200
    assert second.status_code == 200


@pytest.mark.asyncio
async def test_sse_auth_injects_heartbeat_for_idle_event_stream(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    monkeypatch.setenv("SSE_HEARTBEAT_PING_SECONDS", "0.01")

    async def _idle_sse(scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b"event: endpoint\n\n",
                "more_body": True,
            }
        )
        await asyncio.sleep(0.03)
        await send(
            {
                "type": "http.response.body",
                "body": b"data: done\n\n",
                "more_body": False,
            }
        )

    wrapped_app = apply_mcp_api_key_middleware(_idle_sse)
    sent_messages = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent_messages.append(message)

    await wrapped_app(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/sse",
            "raw_path": b"/sse",
            "query_string": b"",
            "headers": [
                (b"host", b"example.com"),
                (b"x-mcp-api-key", b"week6-sse-secret"),
            ],
            "client": ("203.0.113.10", 50000),
            "server": ("203.0.113.20", 8000),
        },
        _receive,
        _send,
    )

    heartbeat_frames = [
        item
        for item in sent_messages
        if item.get("type") == "http.response.body" and item.get("body") == b": ping\n\n"
    ]
    assert heartbeat_frames


def test_sse_auth_does_not_raise_on_streaming_disconnect(tmp_path) -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = dict(**os.environ)
    env["MCP_API_KEY"] = "week6-sse-secret"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'streaming_disconnect.db'}"
    env["HOST"] = "127.0.0.1"
    env["PORT"] = str(port)
    server = subprocess.Popen(
        [
            sys.executable,
            "run_sse.py",
        ],
        cwd=str(backend_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=_WINDOWS_NEW_PROCESS_GROUP,
    )

    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if server.poll() is not None:
                pytest.fail("uvicorn exited before the streaming test could connect")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("timed out waiting for streaming test server to start")

        request = (
            "GET /sse HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Accept: text/event-stream\r\n"
            "X-MCP-API-Key: week6-sse-secret\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("utf-8")

        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.sendall(request)
            chunks = []
            deadline = time.time() + 5
            while time.time() < deadline:
                chunk = client.recv(4096).decode("utf-8", errors="ignore")
                if not chunk:
                    break
                chunks.append(chunk)
                if "event: endpoint" in "".join(chunks):
                    break
            received = "".join(chunks)
            assert "200 OK" in received
            assert "event: endpoint" in received

        time.sleep(0.5)
    finally:
        server.terminate()
        try:
            output, _ = server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            output, _ = server.communicate(timeout=5)

    assert "AssertionError: Unexpected message" not in output


def test_sse_auth_does_not_raise_on_streaming_shutdown(tmp_path) -> None:
    backend_dir = Path(__file__).resolve().parents[1]
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    env = dict(**os.environ)
    env["MCP_API_KEY"] = "week6-sse-secret"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{tmp_path / 'streaming_shutdown.db'}"
    env["HOST"] = "127.0.0.1"
    env["PORT"] = str(port)
    server = subprocess.Popen(
        [
            sys.executable,
            "run_sse.py",
        ],
        cwd=str(backend_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=_WINDOWS_NEW_PROCESS_GROUP,
    )

    client = None
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if server.poll() is not None:
                pytest.fail("uvicorn exited before the shutdown test could connect")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("timed out waiting for shutdown test server to start")

        request = (
            "GET /sse HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Accept: text/event-stream\r\n"
            "X-MCP-API-Key: week6-sse-secret\r\n"
            "\r\n"
        ).encode("utf-8")

        client = socket.create_connection(("127.0.0.1", port), timeout=5)
        client.sendall(request)
        received = ""
        deadline = time.time() + 5
        while time.time() < deadline:
            chunk = client.recv(4096).decode("utf-8", errors="ignore")
            if not chunk:
                break
            received += chunk
            if "event: endpoint" in received:
                break
        assert "200 OK" in received
        assert "event: endpoint" in received

        if os.name == "nt":
            server.terminate()
        else:
            server.send_signal(signal.SIGINT)
        if client is not None:
            client.close()
            client = None
        output, _ = server.communicate(timeout=10)
    finally:
        if client is not None:
            client.close()
        if server.poll() is None:
            server.terminate()
            try:
                output, _ = server.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                output, _ = server.communicate(timeout=5)

    assert "Expected ASGI message 'http.response.body'" not in output
    assert "RuntimeError:" not in output


def test_resolve_sse_port_falls_back_for_busy_loopback(monkeypatch, caplog) -> None:
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(run_sse, "_is_loopback_port_available", lambda _port: False)

    with caplog.at_level(logging.WARNING, logger="run_sse"):
        resolved = run_sse._resolve_sse_port("127.0.0.1")

    assert resolved == 8010
    assert "falling back to 8010" in caplog.text


def test_resolve_sse_port_falls_back_for_default_host_when_port_is_busy(
    monkeypatch, caplog
) -> None:
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(run_sse, "_is_loopback_port_available", lambda _port: False)

    with caplog.at_level(logging.WARNING, logger="run_sse"):
        resolved = run_sse._resolve_sse_port("0.0.0.0")

    assert resolved == 8010
    assert "falling back to 8010" in caplog.text


def test_should_suppress_closed_resource_error_only_for_sse_paths() -> None:
    assert run_sse._should_suppress_closed_resource_error(
        {"type": "http", "path": "/sse"}
    )
    assert not run_sse._should_suppress_closed_resource_error(
        {"type": "http", "path": "/messages"}
    )


def test_create_sse_server_app_runs_mcp_lifespan_hooks(monkeypatch) -> None:
    call_order = []

    async def _fake_startup() -> None:
        call_order.append("startup")

    async def _fake_shutdown() -> None:
        call_order.append("shutdown")

    async def _ping(_request):
        return StreamingResponse(iter(["event: ping\n\n"]), media_type="text/event-stream")

    monkeypatch.setattr(run_sse, "mcp_startup", _fake_startup)
    monkeypatch.setattr(run_sse, "mcp_shutdown", _fake_shutdown)
    monkeypatch.setattr(
        run_sse,
        "create_sse_app",
        lambda: Starlette(routes=[Route("/ping", endpoint=_ping, methods=["GET"])]),
    )

    with TestClient(run_sse.create_sse_server_app()) as client:
        response = client.get("/ping")

    assert response.status_code == 200
    assert call_order == ["startup", "shutdown"]


def test_sse_main_builds_server_app_before_uvicorn(monkeypatch) -> None:
    call_order = []
    fake_server_app = object()

    async def _fake_startup() -> None:
        call_order.append("startup")

    def _fake_create_server_app():
        call_order.append("create_server_app")
        return fake_server_app

    def _fake_uvicorn_run(app, host, port):
        call_order.append(("uvicorn", host, port, app))

    monkeypatch.setattr(run_sse, "mcp_startup", _fake_startup)
    monkeypatch.setattr(run_sse, "create_sse_server_app", _fake_create_server_app)
    monkeypatch.setattr(run_sse.uvicorn, "run", _fake_uvicorn_run)
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "8010")

    run_sse.main()

    assert call_order == [
        "create_server_app",
        ("uvicorn", "127.0.0.1", 8010, fake_server_app),
    ]


def test_sse_main_defaults_to_loopback_host_when_host_missing(monkeypatch) -> None:
    call_order = []

    def _fake_create_server_app():
        call_order.append("create_server_app")
        return {"app": "fake"}

    def _fake_uvicorn_run(app, host, port):
        call_order.append(("uvicorn", host, port, app))

    monkeypatch.setattr(run_sse, "create_sse_server_app", _fake_create_server_app)
    monkeypatch.setattr(run_sse.uvicorn, "run", _fake_uvicorn_run)
    monkeypatch.setattr(run_sse, "_is_loopback_port_available", lambda _port: True)
    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)

    run_sse.main()

    assert call_order == [
        "create_server_app",
        ("uvicorn", "127.0.0.1", 8000, {"app": "fake"}),
    ]


def test_sse_main_uses_loopback_fallback_port_when_default_is_busy(monkeypatch) -> None:
    call_order = []

    def _fake_create_server_app():
        call_order.append("create_server_app")
        return {"app": "fake"}

    def _fake_uvicorn_run(app, host, port):
        call_order.append(("uvicorn", host, port, app))

    monkeypatch.setattr(run_sse, "create_sse_server_app", _fake_create_server_app)
    monkeypatch.setattr(run_sse.uvicorn, "run", _fake_uvicorn_run)
    monkeypatch.setattr(run_sse, "_is_loopback_port_available", lambda _port: False)
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.delenv("PORT", raising=False)

    run_sse.main()

    assert call_order == [
        "create_server_app",
        ("uvicorn", "0.0.0.0", 8010, {"app": "fake"}),
    ]


def test_sse_heartbeat_closed_resource_error_is_suppressed(monkeypatch) -> None:
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")
    monkeypatch.setenv("SSE_HEARTBEAT_PING_SECONDS", "0.001")

    original_sleep = run_sse.asyncio.sleep

    async def _fast_sleep(delay: float) -> None:
        await original_sleep(min(delay, 0.001))

    async def _fake_app(_scope, _receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream; charset=utf-8")],
            }
        )
        await original_sleep(0.02)

    sent = {"count": 0}

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent["count"] += 1
        if message.get("type") == "http.response.body":
            raise ClosedResourceError()

    monkeypatch.setattr(run_sse.asyncio, "sleep", _fast_sleep)
    wrapped = apply_mcp_api_key_middleware(_fake_app)

    asyncio.run(
        wrapped(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/sse",
                "raw_path": b"/sse",
                "query_string": b"",
                "headers": [
                    (b"host", b"127.0.0.1"),
                    (b"x-mcp-api-key", b"week6-sse-secret"),
                ],
                "client": ("127.0.0.1", 50000),
                "server": ("127.0.0.1", 8000),
            },
            _receive,
            _send,
        )
    )

    assert sent["count"] >= 2


def test_create_embedded_sse_apps_extracts_stream_and_message_routes(monkeypatch) -> None:
    async def _stream(_request):
        return StreamingResponse(iter(["event: endpoint\n\n"]), media_type="text/event-stream")

    async def _message_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    fake_base_app = Starlette(
        routes=[
            Route("/sse", endpoint=_stream, methods=["GET"]),
            Mount("/messages", app=_message_app),
        ]
    )

    class _FakeMcp:
        def sse_app(self):
            return fake_base_app

    monkeypatch.setattr(run_sse, "mcp", _FakeMcp())
    monkeypatch.setenv("MCP_API_KEY", "week6-sse-secret")

    stream_app, message_app = run_sse.create_embedded_sse_apps()

    with TestClient(stream_app, client=("127.0.0.1", 50000)) as client:
        response = client.get("/", headers={"X-MCP-API-Key": "week6-sse-secret"})
        assert response.status_code == 200

    sent_messages = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        sent_messages.append(message)

    asyncio.run(
        message_app(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [
                    (b"host", b"127.0.0.1"),
                    (b"x-mcp-api-key", b"week6-sse-secret"),
                ],
                "client": ("127.0.0.1", 50000),
                "server": ("127.0.0.1", 8000),
            },
            _receive,
            _send,
        )
    )

    assert any(item.get("type") == "http.response.start" for item in sent_messages)


def test_create_embedded_sse_apps_raises_clear_error_when_message_mount_missing(
    monkeypatch,
) -> None:
    async def _stream(_request):
        return StreamingResponse(iter(["event: endpoint\n\n"]), media_type="text/event-stream")

    fake_base_app = Starlette(
        routes=[Route("/sse", endpoint=_stream, methods=["GET"])]
    )

    class _FakeMcp:
        def sse_app(self):
            return fake_base_app

    monkeypatch.setattr(run_sse, "mcp", _FakeMcp())

    with pytest.raises(RuntimeError, match="Missing embedded SSE routes"):
        run_sse.create_embedded_sse_apps()
