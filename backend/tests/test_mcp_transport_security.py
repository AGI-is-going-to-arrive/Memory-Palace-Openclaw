from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
import socket
import subprocess
import sys
import time


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from mcp_transport import _is_loopback_host, _resolve_transport_security


def _allocate_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_is_loopback_host_accepts_ipv4_ipv6_and_port_variants() -> None:
    assert _is_loopback_host("127.0.0.2")
    assert _is_loopback_host("[::1]:8080")
    assert _is_loopback_host("127.0.0.1:3000")
    assert _is_loopback_host("localhost:5173")


def test_is_loopback_host_rejects_non_loopback_hosts() -> None:
    assert not _is_loopback_host("192.168.1.10:3000")
    assert not _is_loopback_host("example.com")


def test_resolve_transport_security_keeps_loopback_protection_enabled() -> None:
    settings = _resolve_transport_security("127.0.0.1")

    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.1" in settings.allowed_hosts
    assert "http://127.0.0.1" in settings.allowed_origins


def test_resolve_transport_security_applies_explicit_allowlists_on_loopback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "example.com:*")
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "http://example.com:*")

    settings = _resolve_transport_security("127.0.0.1")

    assert "example.com:*" in settings.allowed_hosts
    assert "http://example.com:*" in settings.allowed_origins


def _wait_until_listening(port: int, server: subprocess.Popen[str]) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        if server.poll() is not None:
            raise AssertionError("uvicorn exited before the transport-security test could connect")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("timed out waiting for transport-security test server to start")


def _send_sse_request(
    port: int,
    host_header: str,
    extra_headers: Mapping[str, str] | None = None,
) -> str:
    header_lines = [
        "GET /sse HTTP/1.1",
        f"Host: {host_header}:{port}",
        "Accept: text/event-stream",
        "X-MCP-API-Key: transport-secret",
        "Connection: close",
    ]
    for header_name, header_value in (extra_headers or {}).items():
        header_lines.append(f"{header_name}: {header_value}")
    request = ("\r\n".join(header_lines) + "\r\n\r\n").encode("utf-8")

    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(request)
        chunks: list[str] = []
        deadline = time.time() + 5
        while time.time() < deadline:
            chunk = client.recv(4096).decode("utf-8", errors="ignore")
            if not chunk:
                break
            chunks.append(chunk)
            combined = "".join(chunks)
            if (
                "event: endpoint" in combined
                or "Invalid Host header" in combined
                or "Invalid Origin header" in combined
            ):
                break
    return "".join(chunks)


def _run_sse_server(
    tmp_path: Path,
    host: str,
    *,
    extra_env: Mapping[str, str] | None = None,
) -> tuple[subprocess.Popen[str], int]:
    port = _allocate_port()
    db_path = tmp_path / f"{host.replace(':', '_')}.db"
    env = dict(os.environ)
    env["MCP_API_KEY"] = "transport-secret"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    env["HOST"] = host
    env["PORT"] = str(port)
    env.update(extra_env or {})
    server = subprocess.Popen(
        [sys.executable, "run_sse.py"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    _wait_until_listening(port, server)
    return server, port


def _stop_server(server: subprocess.Popen[str]) -> str:
    try:
        server.terminate()
        return server.communicate(timeout=5)[0]
    except subprocess.TimeoutExpired:
        server.kill()
        return server.communicate(timeout=5)[0]


def test_sse_remote_host_rejects_custom_host_header_by_default(tmp_path: Path) -> None:
    server, port = _run_sse_server(tmp_path, "0.0.0.0")
    try:
        response = _send_sse_request(port, "example.com")
    finally:
        output = _stop_server(server)

    assert "421 Misdirected Request" in response
    assert "Invalid Host header" in response
    assert "RuntimeError:" not in output


def test_sse_remote_host_allows_explicit_allowlisted_host_and_origin(tmp_path: Path) -> None:
    server, port = _run_sse_server(
        tmp_path,
        "0.0.0.0",
        extra_env={
            "MCP_ALLOWED_HOSTS": "example.com:*",
            "MCP_ALLOWED_ORIGINS": "http://example.com,http://example.com:*",
        },
    )
    try:
        response = _send_sse_request(
            port,
            "example.com",
            extra_headers={"Origin": "http://example.com"},
        )
    finally:
        output = _stop_server(server)

    assert "200 OK" in response
    assert "event: endpoint" in response
    assert "Invalid Host header" not in response
    assert "Invalid Origin header" not in response
    assert "RuntimeError:" not in output


def test_sse_loopback_host_rejects_non_loopback_host_header(tmp_path: Path) -> None:
    server, port = _run_sse_server(tmp_path, "127.0.0.1")
    try:
        response = _send_sse_request(port, "example.com")
    finally:
        output = _stop_server(server)

    assert "421 Misdirected Request" in response
    assert "Invalid Host header" in response
    assert "RuntimeError:" not in output
    assert "ValueError: Request validation failed" not in output


def test_sse_loopback_host_rejects_invalid_origin_without_value_error_noise(tmp_path: Path) -> None:
    server, port = _run_sse_server(tmp_path, "127.0.0.1")
    try:
        response = _send_sse_request(
            port,
            "127.0.0.1",
            extra_headers={"Origin": "http://evil.example"},
        )
    finally:
        output = _stop_server(server)

    assert "403 Forbidden" in response
    assert "Invalid Origin header" in response
    assert "RuntimeError:" not in output
    assert "ValueError: Request validation failed" not in output
