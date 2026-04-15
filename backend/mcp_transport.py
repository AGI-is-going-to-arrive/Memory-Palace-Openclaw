import os
import ipaddress
from typing import List

from env_utils import env_csv
from mcp.server.transport_security import TransportSecuritySettings


def _resolve_mcp_host() -> str:
    """Keep FastMCP transport security aligned with the actual server bind host."""
    host = str(os.getenv("HOST", "0.0.0.0") or "").strip()
    return host or "0.0.0.0"


def _is_loopback_host(host: str) -> bool:
    normalized = _normalize_transport_bind_host(host)
    if not normalized:
        return False
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _append_unique_pattern(target: List[str], value: str) -> None:
    rendered = value.strip()
    if rendered and rendered not in target:
        target.append(rendered)


def _normalize_transport_bind_host(host: str) -> str:
    normalized = str(host or "").strip().lower()
    if normalized.startswith("["):
        closing = normalized.find("]")
        if closing != -1:
            return normalized[1:closing]
    if normalized.count(":") == 1:
        candidate, port = normalized.rsplit(":", 1)
        if port.isdigit():
            normalized = candidate
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return normalized


def _render_transport_host_literal(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _transport_env_csv(name: str) -> List[str]:
    values: List[str] = []
    for item in env_csv(name):
        value = item.strip().lower()
        if value and value not in values:
            values.append(value)
    return values


def _transport_allowed_hosts(host: str) -> List[str]:
    patterns = _transport_env_csv("MCP_ALLOWED_HOSTS")
    for default_host in (
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
    ):
        _append_unique_pattern(patterns, default_host)

    normalized_host = _normalize_transport_bind_host(host)
    if normalized_host and normalized_host not in {"0.0.0.0", "::"} and not _is_loopback_host(normalized_host):
        rendered_host = _render_transport_host_literal(normalized_host)
        _append_unique_pattern(patterns, rendered_host)
        _append_unique_pattern(patterns, f"{rendered_host}:*")
    return patterns


def _transport_allowed_origins(host: str) -> List[str]:
    patterns = _transport_env_csv("MCP_ALLOWED_ORIGINS")
    for default_origin in (
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        "https://127.0.0.1",
        "https://127.0.0.1:*",
        "http://localhost",
        "http://localhost:*",
        "https://localhost",
        "https://localhost:*",
        "http://[::1]",
        "http://[::1]:*",
        "https://[::1]",
        "https://[::1]:*",
    ):
        _append_unique_pattern(patterns, default_origin)

    normalized_host = _normalize_transport_bind_host(host)
    if normalized_host and normalized_host not in {"0.0.0.0", "::"} and not _is_loopback_host(normalized_host):
        rendered_host = _render_transport_host_literal(normalized_host)
        for scheme in ("http", "https"):
            _append_unique_pattern(patterns, f"{scheme}://{rendered_host}")
            _append_unique_pattern(patterns, f"{scheme}://{rendered_host}:*")
    return patterns


def _resolve_transport_security(host: str) -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_transport_allowed_hosts(host),
        allowed_origins=_transport_allowed_origins(host),
    )
