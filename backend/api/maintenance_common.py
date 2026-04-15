import hmac
import logging
import math
import os
from typing import Any, List, Optional

from fastapi import Header, HTTPException, Request, status
from env_utils import (
    env_bool as _env_bool,
    env_float as _env_float,
    parse_iso_datetime as _parse_iso_ts,
    utc_iso_now as _utc_iso_now,
)

logger = logging.getLogger(__name__)

_MCP_API_KEY_ENV = "MCP_API_KEY"
_MCP_API_KEY_HEADER = "X-MCP-API-Key"
_MCP_API_KEY_ALLOW_INSECURE_LOCAL_ENV = "MCP_API_KEY_ALLOW_INSECURE_LOCAL"
_TRUTHY_ENV_VALUES = {"1", "true", "yes", "on", "enabled"}
_LOOPBACK_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost"}
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


def _get_configured_mcp_api_key() -> str:
    return str(os.getenv(_MCP_API_KEY_ENV) or "").strip()


def _allow_insecure_local_without_api_key() -> bool:
    value = str(os.getenv(_MCP_API_KEY_ALLOW_INSECURE_LOCAL_ENV) or "").strip().lower()
    return value in _TRUTHY_ENV_VALUES


def _is_loopback_request(request: Request) -> bool:
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "").strip().lower()
    if host not in _LOOPBACK_CLIENT_HOSTS:
        return False
    headers = getattr(request, "headers", None)
    if headers is None:
        return True
    for header_name in _FORWARDED_HEADER_NAMES:
        header_value = headers.get(header_name)
        if isinstance(header_value, str) and header_value.strip():
            return False
    return True


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


async def require_maintenance_api_key(
    request: Request,
    x_mcp_api_key: Optional[str] = Header(default=None, alias=_MCP_API_KEY_HEADER),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> None:
    configured = _get_configured_mcp_api_key()
    if not configured:
        if _allow_insecure_local_without_api_key() and _is_loopback_request(request):
            logger.warning(
                "Insecure local auth bypass: MCP_API_KEY not configured, "
                "loopback request allowed without authentication."
            )
            return
        reason = (
            "insecure_local_override_requires_loopback"
            if _allow_insecure_local_without_api_key()
            else "api_key_not_configured"
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "maintenance_auth_failed",
                "reason": reason,
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    provided = str(x_mcp_api_key or "").strip() or _extract_bearer_token(authorization)
    if not provided or not hmac.compare_digest(provided, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "maintenance_auth_failed",
                "reason": "invalid_or_missing_api_key",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )


def _safe_percentile(values: List[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * ratio) - 1))
    return float(ordered[index])


def _safe_non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
