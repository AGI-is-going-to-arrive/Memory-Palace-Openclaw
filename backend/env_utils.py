import logging
import math
import os
from datetime import datetime, timezone
from typing import Iterable, Optional

_DEFAULT_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on", "enabled"})
_DEFAULT_FALSEY_ENV_VALUES = frozenset({"0", "false", "no", "off", "disabled"})
logger = logging.getLogger(__name__)


def env_bool(
    name: str,
    default: bool,
    *,
    truthy_values: Iterable[str] = _DEFAULT_TRUTHY_ENV_VALUES,
    falsey_values: Iterable[str] = _DEFAULT_FALSEY_ENV_VALUES,
) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized_truthy = {str(value).strip().lower() for value in truthy_values}
    normalized_falsey = {str(value).strip().lower() for value in falsey_values}
    normalized = raw.strip().lower()
    if normalized in normalized_truthy:
        return True
    if normalized in normalized_falsey:
        return False
    logger.warning(
        "Ignoring invalid boolean environment value for %s: %r; using default %s.",
        name,
        raw,
        default,
    )
    return default


def _warn_invalid_numeric_env(
    name: str,
    raw: str,
    *,
    default: object,
    value_type: str,
) -> None:
    logger.warning(
        "Ignoring invalid %s environment value for %s: %r; using default %s.",
        value_type,
        name,
        raw,
        default,
    )


def _warn_clamped_numeric_env(
    name: str,
    raw: str,
    *,
    minimum: object,
    value_type: str,
) -> None:
    logger.warning(
        "Clamping %s environment value for %s: %r is below minimum %s; using %s.",
        value_type,
        name,
        raw,
        minimum,
        minimum,
    )


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    if not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        _warn_invalid_numeric_env(
            name,
            raw,
            default=default,
            value_type="integer",
        )
        return default
    if value < minimum:
        _warn_clamped_numeric_env(
            name,
            raw,
            minimum=minimum,
            value_type="integer",
        )
        return minimum
    return value


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return max(minimum, float(default))
    fallback = max(minimum, float(default))
    if not raw.strip():
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _warn_invalid_numeric_env(
            name,
            raw,
            default=fallback,
            value_type="float",
        )
        return fallback
    if not math.isfinite(value):
        _warn_invalid_numeric_env(
            name,
            raw,
            default=fallback,
            value_type="float",
        )
        return fallback
    if value < minimum:
        _warn_clamped_numeric_env(
            name,
            raw,
            minimum=minimum,
            value_type="float",
        )
        return minimum
    return value


def env_csv(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    values: list[str] = []
    for part in str(raw or "").split(","):
        value = part.strip()
        if value:
            values.append(value)
    return values


def utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    return parse_iso_datetime_with_options(value)


def parse_iso_datetime_with_options(
    value: Optional[str],
    *,
    normalize_utc: bool = False,
    naive_utc: bool = False,
    assume_utc_for_naive: bool = False,
    raise_on_error: bool = False,
    error_message: Optional[str] = None,
) -> Optional[datetime]:
    if not value:
        return None
    candidate = str(value).strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        if raise_on_error:
            raise ValueError(
                error_message
                or f"Invalid datetime '{value}'. Use ISO-8601 like '2026-01-31T12:00:00Z'."
            ) from exc
        return None

    if assume_utc_for_naive and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is not None and (normalize_utc or naive_utc):
        parsed = parsed.astimezone(timezone.utc)
    if naive_utc and parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed
