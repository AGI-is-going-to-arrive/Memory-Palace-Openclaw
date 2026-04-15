import json
import math
import os
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from .maintenance_common import _parse_iso_ts


def _coerce_trace_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return round(parsed, 6)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TRANSPORT_DIAGNOSTICS_PATH_ENV = "OPENCLAW_TRANSPORT_DIAGNOSTICS_PATH"
_DEFAULT_TRANSPORT_DIAGNOSTICS_PATH = (
    _PROJECT_ROOT
    / ".tmp"
    / "observability"
    / "openclaw_transport_diagnostics.json"
)
_TRANSPORT_REDACTION_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(authorization\s*[:=]\s*bearer\s+)[^\s,;]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"\b(x-mcp-api-key\s*[:=]\s*)[^\s,;]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"\b(api[-_ ]?key\s*[:=]\s*)[^\s,;]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"\b(token\s*[:=]\s*)[^\s,;]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"([?&](?:api[-_]?key|token|key)=)[^&\s]+", re.IGNORECASE), r"\1[REDACTED]"),
]

def _redact_transport_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    for pattern, replacement in _TRANSPORT_REDACTION_PATTERNS:
        normalized = pattern.sub(replacement, normalized)
    return normalized or None


def _coerce_transport_int(value: Any) -> Optional[int]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _coerce_transport_float(value: Any) -> Optional[float]:
    parsed = _coerce_trace_number(value)
    if parsed is None or parsed < 0:
        return None
    return round(float(parsed), 3)


def _empty_transport_latency_summary() -> Dict[str, Any]:
    return {
        "last": None,
        "avg": None,
        "p95": None,
        "max": None,
        "samples": 0,
    }


def _normalize_transport_latency_summary(raw: Any) -> Dict[str, Any]:
    payload = _empty_transport_latency_summary()
    if not isinstance(raw, dict):
        return payload
    payload["last"] = _coerce_transport_float(raw.get("last"))
    payload["avg"] = _coerce_transport_float(raw.get("avg"))
    payload["p95"] = _coerce_transport_float(raw.get("p95"))
    payload["max"] = _coerce_transport_float(raw.get("max"))
    payload["samples"] = _coerce_transport_int(raw.get("samples")) or 0
    return payload


def _summarize_transport_latency_samples(values: List[Any]) -> Dict[str, Any]:
    normalized = [
        value
        for value in (_coerce_transport_float(item) for item in values)
        if value is not None
    ]
    if not normalized:
        return _empty_transport_latency_summary()
    sorted_values = sorted(normalized)
    p95_index = min(
        len(sorted_values) - 1,
        max(0, math.ceil(len(sorted_values) * 0.95) - 1),
    )
    average = round(sum(normalized) / len(normalized), 3)
    return {
        "last": normalized[-1],
        "avg": average,
        "p95": sorted_values[p95_index],
        "max": sorted_values[-1],
        "samples": len(normalized),
    }


def _merge_transport_latency_summaries(
    snapshots: List[Dict[str, Any]], recent_events: List[Dict[str, Any]]
) -> Dict[str, Any]:
    aggregate = _empty_transport_latency_summary()
    total_samples = 0
    weighted_sum = 0.0
    max_latency: Optional[float] = None
    fallback_p95: Optional[float] = None
    for item in snapshots:
        diagnostics = item.get("diagnostics") if isinstance(item.get("diagnostics"), dict) else {}
        summary = (
            diagnostics.get("connect_latency_ms")
            if isinstance(diagnostics.get("connect_latency_ms"), dict)
            else {}
        )
        sample_count = _coerce_transport_int(summary.get("samples")) or 0
        avg_latency = _coerce_transport_float(summary.get("avg"))
        max_candidate = _coerce_transport_float(summary.get("max"))
        p95_candidate = _coerce_transport_float(summary.get("p95"))
        if sample_count > 0 and avg_latency is not None:
            total_samples += sample_count
            weighted_sum += avg_latency * sample_count
        if max_candidate is not None:
            max_latency = (
                max_candidate
                if max_latency is None
                else max(max_latency, max_candidate)
            )
        if p95_candidate is not None:
            fallback_p95 = (
                p95_candidate
                if fallback_p95 is None
                else max(fallback_p95, p95_candidate)
            )

    event_connect_latencies = [
        event.get("latency_ms")
        for event in recent_events
        if isinstance(event, dict)
        and event.get("category") == "connect"
        and event.get("status") in {"pass", "warn"}
    ]
    event_summary = _summarize_transport_latency_samples(event_connect_latencies)
    last_event_connect_latency = next(
        (
            _coerce_transport_float(event.get("latency_ms"))
            for event in recent_events
            if isinstance(event, dict)
            and event.get("category") == "connect"
            and event.get("status") in {"pass", "warn"}
            and _coerce_transport_float(event.get("latency_ms")) is not None
        ),
        None,
    )

    aggregate["last"] = (
        _coerce_transport_float(
            (
                snapshots[0].get("diagnostics", {})
                if snapshots and isinstance(snapshots[0].get("diagnostics"), dict)
                else {}
            )
            .get("connect_latency_ms", {})
            .get("last")
        )
        if snapshots
        else None
    )
    if aggregate["last"] is None:
        aggregate["last"] = last_event_connect_latency
    aggregate["avg"] = (
        round(weighted_sum / total_samples, 3) if total_samples > 0 else event_summary["avg"]
    )
    aggregate["p95"] = event_summary["p95"] if event_summary["samples"] > 0 else fallback_p95
    aggregate["max"] = (
        max_latency if max_latency is not None else event_summary["max"]
    )
    aggregate["samples"] = total_samples if total_samples > 0 else event_summary["samples"]
    return aggregate


def _sanitize_transport_event(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    at = _redact_transport_text(raw.get("at"))
    category = _redact_transport_text(raw.get("category"))
    status = _redact_transport_text(raw.get("status"))
    transport = _redact_transport_text(raw.get("transport"))
    if not at or not category or not status:
        return None
    payload: Dict[str, Any] = {
        "at": at,
        "category": category,
        "status": status,
        "transport": transport,
    }
    tool = _redact_transport_text(raw.get("tool"))
    if tool:
        payload["tool"] = tool
    attempt = _coerce_transport_int(raw.get("attempt"))
    if attempt is not None:
        payload["attempt"] = attempt
    for boolean_key in ("fallback", "retry", "reused"):
        if isinstance(raw.get(boolean_key), bool):
            payload[boolean_key] = raw.get(boolean_key)
    latency_ms = _coerce_transport_float(raw.get("latency_ms"))
    if latency_ms is not None:
        payload["latency_ms"] = latency_ms
    message = _redact_transport_text(raw.get("message"))
    if message:
        payload["message"] = message
    return payload


def _transport_snapshot_path() -> Path:
    raw = str(os.getenv(_TRANSPORT_DIAGNOSTICS_PATH_ENV) or "").strip()
    return Path(raw) if raw else _DEFAULT_TRANSPORT_DIAGNOSTICS_PATH


def _empty_transport_observability(
    reason: str, *, available: bool, degraded: bool, status: str
) -> Dict[str, Any]:
    return {
        "available": available,
        "degraded": degraded,
        "reason": reason,
        "updated_at": None,
        "status": status,
        "active_transport": None,
        "configured_transport": None,
        "configured_transports": [],
        "fallback_order": [],
        "connection_model": "persistent-client",
        "snapshot_count": 0,
        "sources": [],
        "instances": [],
        "diagnostics": {
            "connect_attempts": 0,
            "connect_retry_count": 0,
            "call_retry_count": 0,
            "request_retries": 0,
            "fallback_count": 0,
            "reuse_count": 0,
            "last_connected_at": None,
            "connect_latency_ms": _empty_transport_latency_summary(),
            "last_error": None,
            "last_health_check_at": None,
            "last_health_check_error": None,
            "healthcheck_tool": None,
            "healthcheck_ttl_ms": None,
            "recent_events": [],
            "exception_breakdown": {
                "total": 0,
                "status_counts": {},
                "source_counts": {},
                "category_counts": {},
                "tool_counts": {},
                "check_id_counts": {},
                "last_exception_at": None,
                "items": [],
                "signature_breakdown": {
                    "total": 0,
                    "signature_counts": {},
                    "items": [],
                },
                "incident_breakdown": {
                    "incident_count": 0,
                    "canonical_cause_counts": {},
                    "items": [],
                },
            },
        },
        "last_report": None,
    }


def _transport_snapshot_instance_dir(snapshot_path: Path) -> Path:
    return snapshot_path.with_name(f"{snapshot_path.stem}.instances")


def _normalize_transport_snapshot(
    raw: Dict[str, Any], source_path: Path
) -> Optional[Dict[str, Any]]:
    diagnostics_raw = raw.get("diagnostics") if isinstance(raw.get("diagnostics"), dict) else {}
    recent_events_raw = diagnostics_raw.get("recent_events")
    recent_events: List[Dict[str, Any]] = []
    if isinstance(recent_events_raw, list):
        for entry in recent_events_raw[-24:]:
            normalized = _sanitize_transport_event(entry)
            if normalized:
                recent_events.append(normalized)
    connect_latency_summary = _normalize_transport_latency_summary(
        diagnostics_raw.get("connect_latency_ms")
    )
    if connect_latency_summary["samples"] == 0:
        connect_event_summary = _summarize_transport_latency_samples(
            [
                event.get("latency_ms")
                for event in recent_events
                if event.get("category") == "connect"
                and event.get("status") in {"pass", "warn"}
            ]
        )
        if connect_event_summary["samples"] > 0:
            connect_latency_summary = connect_event_summary

    last_report_raw = raw.get("last_report") if isinstance(raw.get("last_report"), dict) else {}
    checks: List[Dict[str, Any]] = []
    if isinstance(last_report_raw.get("checks"), list):
        for entry in last_report_raw["checks"]:
            if not isinstance(entry, dict):
                continue
            check_id = _redact_transport_text(entry.get("id"))
            check_status = _redact_transport_text(entry.get("status"))
            check_message = _redact_transport_text(entry.get("message"))
            if not check_id or not check_status or not check_message:
                continue
            payload: Dict[str, Any] = {
                "id": check_id,
                "status": check_status,
                "message": check_message,
            }
            action = _redact_transport_text(entry.get("action"))
            if action:
                payload["action"] = action
            checks.append(payload)

    normalized_payload: Dict[str, Any] = {
        "source_path": str(source_path),
        "instance_id": _redact_transport_text(raw.get("instance_id"))
        or source_path.stem,
        "process_id": _coerce_transport_int(raw.get("process_id")),
        "updated_at": _redact_transport_text(raw.get("updated_at")),
        "status": _redact_transport_text(raw.get("status")) or "pass",
        "active_transport": _redact_transport_text(raw.get("active_transport")),
        "configured_transport": _redact_transport_text(raw.get("configured_transport")),
        "fallback_order": [
            item.strip()
            for item in raw.get("fallback_order", [])
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(raw.get("fallback_order"), list)
        else [],
        "connection_model": _redact_transport_text(raw.get("connection_model"))
        or "persistent-client",
        "diagnostics": {
            "connect_attempts": _coerce_transport_int(
                diagnostics_raw.get("connect_attempts")
            )
            or 0,
            "connect_retry_count": _coerce_transport_int(
                diagnostics_raw.get("connect_retry_count")
            )
            or 0,
            "call_retry_count": _coerce_transport_int(
                diagnostics_raw.get("call_retry_count")
            )
            or 0,
            "request_retries": _coerce_transport_int(diagnostics_raw.get("request_retries"))
            or 0,
            "fallback_count": _coerce_transport_int(diagnostics_raw.get("fallback_count"))
            or 0,
            "reuse_count": _coerce_transport_int(diagnostics_raw.get("reuse_count"))
            or 0,
            "last_connected_at": _redact_transport_text(
                diagnostics_raw.get("last_connected_at")
            ),
            "connect_latency_ms": connect_latency_summary,
            "last_error": _redact_transport_text(diagnostics_raw.get("last_error")),
            "last_health_check_at": _redact_transport_text(
                diagnostics_raw.get("last_health_check_at")
            ),
            "last_health_check_error": _redact_transport_text(
                diagnostics_raw.get("last_health_check_error")
            ),
            "healthcheck_tool": _redact_transport_text(
                diagnostics_raw.get("healthcheck_tool")
            ),
            "healthcheck_ttl_ms": _coerce_transport_int(
                diagnostics_raw.get("healthcheck_ttl_ms")
            ),
            "recent_events": recent_events,
        },
        "last_report": (
            {
                "command": _redact_transport_text(last_report_raw.get("command")),
                "ok": bool(last_report_raw.get("ok")),
                "status": _redact_transport_text(last_report_raw.get("status")),
                "summary": _redact_transport_text(last_report_raw.get("summary")),
                "active_transport": _redact_transport_text(
                    last_report_raw.get("active_transport")
                ),
                "checks": checks,
            }
            if last_report_raw
            else None
        ),
    }
    last_report = (
        normalized_payload.get("last_report")
        if isinstance(normalized_payload.get("last_report"), dict)
        else {}
    )
    report_status = str(last_report.get("status") or "").strip().lower()
    recent_event_degraded = any(
        str(event.get("status") or "").strip().lower() in {"warn", "fail"}
        for event in recent_events
        if isinstance(event, dict)
    )
    normalized_payload["degraded"] = bool(normalized_payload["status"] != "pass") or bool(
        normalized_payload["diagnostics"]["last_error"]
        or normalized_payload["diagnostics"]["last_health_check_error"]
        or last_report.get("ok") is False
        or report_status in {"warn", "fail"}
        or recent_event_degraded
    )
    return normalized_payload


def _transport_status_rank(status: Optional[str]) -> int:
    normalized = str(status or "").strip().lower()
    if normalized == "fail":
        return 3
    if normalized == "warn":
        return 2
    if normalized == "pass":
        return 1
    return 0


def _normalize_transport_exception_message(value: Any) -> Optional[str]:
    text = _redact_transport_text(value)
    if not text:
        return None
    normalized = text.strip()
    if not normalized:
        return None
    first_line = normalized.splitlines()[0].strip()
    return first_line[:240] if first_line else None


def _transport_incident_cause_family(canonical_cause: str) -> str:
    cause = str(canonical_cause or "").strip().lower()
    if cause == "healthcheck_auth_failure":
        return "auth"
    if cause in {"transport_timeout", "transport_connect_fallback"}:
        return "latency"
    if cause in {
        "transport_connection_refused",
        "transport_network_unreachable",
        "transport_connection_reset",
        "transport_dns_failure",
    }:
        return "network"
    if cause == "transport_tls_failure":
        return "tls"
    if cause in {
        "transport_rate_limited",
        "transport_payload_too_large",
        "transport_upstream_unavailable",
        "transport_protocol_error",
    }:
        return "upstream"
    if cause == "sqlite_database_locked":
        return "storage"
    if cause == "transport_snapshot_load_failed":
        return "observability"
    if cause.startswith("report_check |"):
        return "healthcheck"
    return "other"


def _canonicalize_transport_exception_cause(
    *,
    category: str,
    tool: str,
    check_id: str,
    transport: str,
    message: str,
    fallback_signature: str,
) -> str:
    normalized_message = str(message or "").strip().lower()
    normalized_category = str(category or "").strip().lower()
    normalized_tool = str(tool or "").strip().lower()
    normalized_check_id = str(check_id or "").strip().lower()
    normalized_transport = str(transport or "").strip().lower()
    normalized_context = " | ".join(
        part
        for part in (
            normalized_category,
            normalized_tool,
            normalized_check_id,
            normalized_transport,
            normalized_message,
        )
        if part
    )
    if "database is locked" in normalized_message:
        return "sqlite_database_locked"
    if normalized_category == "snapshot_load":
        return "transport_snapshot_load_failed"
    if (
        normalized_category in {"connect", "healthcheck", "report_check", "transport"}
        and (
            "token=[redacted]" in normalized_message
            or "x-mcp-api-key: [redacted]" in normalized_message
            or "authorization: bearer [redacted]" in normalized_message
            or "unauthorized" in normalized_message
            or "forbidden" in normalized_message
            or "invalid api key" in normalized_message
            or "api key missing" in normalized_message
            or "api key invalid" in normalized_message
            or "invalid token" in normalized_message
            or "missing token" in normalized_message
            or "401" in normalized_message
            or "403" in normalized_message
        )
    ):
        return "healthcheck_auth_failure"
    if normalized_category == "connect" and "connected after fallback" in normalized_message:
        return "transport_connect_fallback"
    if any(
        marker in normalized_context
        for marker in (
            "timed out",
            "timeout",
            "deadline exceeded",
            "read timeout",
            "connect timeout",
        )
    ):
        return "transport_timeout"
    if any(
        marker in normalized_context
        for marker in (
            "connection refused",
            "econnrefused",
            "actively refused",
        )
    ):
        return "transport_connection_refused"
    if any(
        marker in normalized_context
        for marker in (
            "network is unreachable",
            "no route to host",
            "host is unreachable",
            "enetunreach",
            "ehostunreach",
        )
    ):
        return "transport_network_unreachable"
    if any(
        marker in normalized_context
        for marker in (
            "connection reset",
            "connection reset by peer",
            "econnreset",
            "socket hang up",
            "broken pipe",
            "connection aborted",
        )
    ):
        return "transport_connection_reset"
    if any(
        marker in normalized_context
        for marker in (
            "temporary failure in name resolution",
            "name or service not known",
            "nodename nor servname provided",
            "getaddrinfo",
            "enotfound",
            "eai_again",
            "dns",
        )
    ):
        return "transport_dns_failure"
    if any(
        marker in normalized_context
        for marker in (
            "certificate verify failed",
            "certificate has expired",
            "self signed certificate",
            "ssl:",
            "tls",
            "handshake",
        )
    ):
        return "transport_tls_failure"
    if any(
        marker in normalized_context
        for marker in (
            "429",
            "rate limit",
            "rate-limit",
            "too many requests",
        )
    ):
        return "transport_rate_limited"
    if any(
        marker in normalized_context
        for marker in (
            "413",
            "payload too large",
            "request entity too large",
            "content too large",
        )
    ):
        return "transport_payload_too_large"
    if any(
        marker in normalized_context
        for marker in (
            "500",
            "502",
            "503",
            "504",
            "bad gateway",
            "service unavailable",
            "gateway timeout",
            "internal server error",
            "upstream connect error",
            "upstream request failed",
        )
    ):
        return "transport_upstream_unavailable"
    if any(
        marker in normalized_context
        for marker in (
            "protocol error",
            "bad status line",
            "invalid content-type",
            "unexpected content-type",
            "invalid json",
            "unexpected token <",
            "malformed response",
        )
    ):
        return "transport_protocol_error"
    return fallback_signature


def _resolve_transport_report_check_signal(
    snapshot: Dict[str, Any],
    check: Dict[str, Any],
) -> Dict[str, str]:
    check_id = str(check.get("id") or "").strip().lower()
    message = _normalize_transport_exception_message(check.get("message")) or ""
    if check_id != "transport-health" or message.lower() != "transport health check failed.":
        return {
            "category": "report_check",
            "tool": "",
            "transport": "",
            "message": message,
        }

    diagnostics = (
        snapshot.get("diagnostics") if isinstance(snapshot.get("diagnostics"), dict) else {}
    )
    healthcheck_message = _normalize_transport_exception_message(
        diagnostics.get("last_health_check_error")
    )
    if healthcheck_message:
        return {
            "category": "healthcheck",
            "tool": str(diagnostics.get("healthcheck_tool") or "").strip().lower(),
            "transport": str(snapshot.get("active_transport") or "").strip().lower(),
            "message": healthcheck_message,
        }

    last_error_message = _normalize_transport_exception_message(diagnostics.get("last_error"))
    if last_error_message:
        return {
            "category": "transport",
            "tool": "",
            "transport": str(snapshot.get("active_transport") or "").strip().lower(),
            "message": last_error_message,
        }

    return {
        "category": "report_check",
        "tool": "",
        "transport": "",
        "message": message,
    }


def _build_transport_exception_breakdown(
    *,
    snapshots: List[Dict[str, Any]],
    merged_events: List[Dict[str, Any]],
    load_errors: List[str],
    focus: Dict[str, Any],
) -> Dict[str, Any]:
    status_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    check_id_counts: Counter[str] = Counter()
    deduped_items: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]] = {}
    signature_items: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    incident_items: Dict[str, Dict[str, Any]] = {}
    last_exception_at: Optional[str] = None

    def _record(
        *,
        source: str,
        status: str,
        category: Optional[str] = None,
        tool: Optional[str] = None,
        transport: Optional[str] = None,
        check_id: Optional[str] = None,
        message: Optional[str] = None,
        at: Optional[str] = None,
    ) -> None:
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in {"warn", "fail"}:
            return
        normalized_source = str(source or "").strip().lower() or "unknown"
        normalized_category = str(category or "").strip().lower()
        normalized_tool = str(tool or "").strip().lower()
        normalized_transport = str(transport or "").strip().lower()
        normalized_check_id = str(check_id or "").strip().lower()
        normalized_message = _normalize_transport_exception_message(message)
        if not normalized_message:
            return

        status_counts[normalized_status] += 1
        source_counts[normalized_source] += 1
        if normalized_category:
            category_counts[normalized_category] += 1
        if normalized_tool:
            tool_counts[normalized_tool] += 1
        if normalized_check_id:
            check_id_counts[normalized_check_id] += 1

        signature_subject = normalized_tool or normalized_check_id or normalized_transport
        fallback_cause = " | ".join(
            part
            for part in (
                normalized_category,
                signature_subject,
                normalized_message,
            )
            if part
        )
        signature_key = (normalized_category, signature_subject, normalized_message)
        signature_payload = signature_items.get(signature_key)
        if signature_payload is None:
            signature_value = " | ".join(
                part
                for part in (
                    normalized_status,
                    normalized_category,
                    signature_subject,
                    normalized_message,
                )
                if part
            )
            signature_payload = {
                "signature": signature_value,
                "status": normalized_status,
                "signal_count": 1,
                "message": normalized_message,
                "sources": [normalized_source],
            }
            if normalized_category:
                signature_payload["category"] = normalized_category
            if normalized_tool:
                signature_payload["tool"] = normalized_tool
            if normalized_transport:
                signature_payload["transport"] = normalized_transport
            if normalized_check_id:
                signature_payload["check_id"] = normalized_check_id
            signature_items[signature_key] = signature_payload
        else:
            current_status = str(signature_payload.get("status") or "").strip().lower()
            if _transport_status_rank(normalized_status) > _transport_status_rank(
                current_status
            ):
                signature_payload["status"] = normalized_status
            signature_payload["signal_count"] = int(
                signature_payload.get("signal_count") or 0
            ) + 1
            sources = signature_payload.get("sources")
            if isinstance(sources, list) and normalized_source not in sources:
                sources.append(normalized_source)
            if normalized_check_id and not signature_payload.get("check_id"):
                signature_payload["check_id"] = normalized_check_id
            effective_subject = (
                str(signature_payload.get("tool") or "").strip().lower()
                or str(signature_payload.get("check_id") or "").strip().lower()
                or str(signature_payload.get("transport") or "").strip().lower()
            )
            signature_payload["signature"] = " | ".join(
                part
                for part in (
                    str(signature_payload.get("status") or "").strip().lower(),
                    str(signature_payload.get("category") or "").strip().lower(),
                    effective_subject,
                    normalized_message,
                )
                if part
            )

        canonical_cause = _canonicalize_transport_exception_cause(
            category=normalized_category,
            tool=normalized_tool,
            check_id=normalized_check_id,
            transport=normalized_transport,
            message=normalized_message,
            fallback_signature=fallback_cause,
        )
        incident_payload = incident_items.get(canonical_cause)
        if incident_payload is None:
            incident_payload = {
                "canonical_cause": canonical_cause,
                "cause_family": _transport_incident_cause_family(canonical_cause),
                "signal_count": 1,
                "highest_status": normalized_status,
                "sample_message": normalized_message,
                "sources": [normalized_source],
                "last_seen_at": at if at else None,
            }
            if normalized_category:
                incident_payload["category"] = normalized_category
            if normalized_tool:
                incident_payload["tool"] = normalized_tool
            if normalized_transport:
                incident_payload["transport"] = normalized_transport
            if normalized_check_id:
                incident_payload["check_id"] = normalized_check_id
            incident_items[canonical_cause] = incident_payload
        else:
            if _transport_status_rank(normalized_status) > _transport_status_rank(
                str(incident_payload.get("highest_status") or "")
            ):
                incident_payload["highest_status"] = normalized_status
            incident_payload["signal_count"] = int(
                incident_payload.get("signal_count") or 0
            ) + 1
            sources = incident_payload.get("sources")
            if isinstance(sources, list) and normalized_source not in sources:
                sources.append(normalized_source)
            if at and (
                incident_payload.get("last_seen_at") is None
                or str(at) > str(incident_payload.get("last_seen_at"))
            ):
                incident_payload["last_seen_at"] = at
            if normalized_tool and not incident_payload.get("tool"):
                incident_payload["tool"] = normalized_tool
            if normalized_transport and not incident_payload.get("transport"):
                incident_payload["transport"] = normalized_transport
            if normalized_check_id and not incident_payload.get("check_id"):
                incident_payload["check_id"] = normalized_check_id
            if not incident_payload.get("cause_family"):
                incident_payload["cause_family"] = _transport_incident_cause_family(
                    canonical_cause
                )

        dedupe_key = (
            normalized_source,
            normalized_status,
            normalized_category,
            normalized_tool,
            normalized_transport,
            normalized_message,
        )
        existing = deduped_items.get(dedupe_key)
        if existing is None:
            payload: Dict[str, Any] = {
                "source": normalized_source,
                "status": normalized_status,
                "count": 1,
                "message": normalized_message,
            }
            if normalized_category:
                payload["category"] = normalized_category
            if normalized_tool:
                payload["tool"] = normalized_tool
            if normalized_transport:
                payload["transport"] = normalized_transport
            if normalized_check_id:
                payload["check_id"] = normalized_check_id
            deduped_items[dedupe_key] = payload
        else:
            existing["count"] = int(existing.get("count") or 0) + 1

    for load_error in load_errors:
        _record(
            source="snapshot_load",
            status="fail",
            category="snapshot_load",
            message=load_error,
            at=cast(Optional[str], focus.get("updated_at")),
        )

    for event in merged_events:
        if not isinstance(event, dict):
            continue
        event_status = str(event.get("status") or "").strip().lower()
        event_at: Optional[str] = None
        if event_status in {"warn", "fail"}:
            event_at = _redact_transport_text(event.get("at"))
            if event_at and (last_exception_at is None or event_at > last_exception_at):
                last_exception_at = event_at
        _record(
            source="recent_events",
            status=event_status,
            category=str(event.get("category") or ""),
            tool=str(event.get("tool") or ""),
            transport=str(event.get("transport") or ""),
            message=event.get("message"),
            at=event_at,
        )

    for item in snapshots:
        report = item.get("last_report") if isinstance(item.get("last_report"), dict) else {}
        checks = report.get("checks") if isinstance(report.get("checks"), list) else []
        for check in checks:
            if not isinstance(check, dict):
                continue
            check_id = str(check.get("id") or "").strip().lower()
            signal = _resolve_transport_report_check_signal(item, check)
            if not signal["message"]:
                continue
            _record(
                source="last_report_checks",
                status=str(check.get("status") or ""),
                category=signal["category"],
                tool=signal["tool"],
                transport=signal["transport"],
                check_id=check_id,
                message=signal["message"],
                at=cast(Optional[str], item.get("updated_at")),
            )

    for item in snapshots:
        diagnostics = (
            item.get("diagnostics") if isinstance(item.get("diagnostics"), dict) else {}
        )
        item_transport = str(item.get("active_transport") or "").strip().lower()
        item_updated_at = cast(Optional[str], item.get("updated_at"))
        if diagnostics.get("last_error"):
            _record(
                source="last_error",
                status="fail",
                category="transport",
                transport=item_transport,
                message=diagnostics.get("last_error"),
                at=item_updated_at,
            )
        if diagnostics.get("last_health_check_error"):
            _record(
                source="last_health_check_error",
                status="warn",
                category="healthcheck",
                tool=str(diagnostics.get("healthcheck_tool") or ""),
                transport=item_transport,
                message=diagnostics.get("last_health_check_error"),
                at=item_updated_at,
            )

    items = sorted(
        deduped_items.values(),
        key=lambda item: (
            -int(item.get("count") or 0),
            -_transport_status_rank(str(item.get("status") or "")),
            str(item.get("category") or item.get("source") or ""),
            str(item.get("tool") or item.get("check_id") or ""),
            str(item.get("message") or ""),
        ),
    )
    signature_breakdown_items = sorted(
        signature_items.values(),
        key=lambda item: (
            -int(item.get("signal_count") or 0),
            -_transport_status_rank(str(item.get("status") or "")),
            str(item.get("category") or ""),
            str(item.get("tool") or item.get("check_id") or ""),
            str(item.get("message") or ""),
        ),
    )
    signature_counts = {
        str(item.get("signature") or f"signature_{index}"): int(
            item.get("signal_count") or 0
        )
        for index, item in enumerate(signature_breakdown_items)
    }
    incident_breakdown_items = sorted(
        incident_items.values(),
        key=lambda item: (
            -int(item.get("signal_count") or 0),
            -_transport_status_rank(str(item.get("highest_status") or "")),
            str(item.get("canonical_cause") or ""),
        ),
    )
    canonical_cause_counts = {
        str(item.get("canonical_cause") or f"incident_{index}"): int(
            item.get("signal_count") or 0
        )
        for index, item in enumerate(incident_breakdown_items)
    }
    return {
        "total": sum(status_counts.values()),
        "status_counts": dict(status_counts),
        "source_counts": dict(source_counts),
        "category_counts": dict(category_counts),
        "tool_counts": dict(tool_counts),
        "check_id_counts": dict(check_id_counts),
        "last_exception_at": last_exception_at
        or (cast(Optional[str], focus.get("updated_at")) if sum(status_counts.values()) > 0 else None),
        "items": items,
        "signature_breakdown": {
            "total": len(signature_breakdown_items),
            "signature_counts": signature_counts,
            "items": signature_breakdown_items,
        },
        "incident_breakdown": {
            "incident_count": len(incident_breakdown_items),
            "canonical_cause_counts": canonical_cause_counts,
            "items": incident_breakdown_items,
        },
    }


def _transport_snapshot_focus_key(item: Dict[str, Any]) -> Tuple[int, int, datetime]:
    diagnostics = item.get("diagnostics") if isinstance(item.get("diagnostics"), dict) else {}
    report = item.get("last_report") if isinstance(item.get("last_report"), dict) else {}
    degraded_signal = bool(
        item.get("degraded")
        or diagnostics.get("last_error")
        or diagnostics.get("last_health_check_error")
        or report.get("ok") is False
    )
    return (
        _transport_status_rank(cast(Optional[str], item.get("status"))),
        1 if degraded_signal else 0,
        _parse_iso_ts(cast(Optional[str], item.get("updated_at")))
        or datetime.min.replace(tzinfo=timezone.utc),
    )


def _load_single_transport_observability(snapshot_path: Path) -> Dict[str, Any]:
    instance_dir = _transport_snapshot_instance_dir(snapshot_path)
    candidate_paths: List[Path] = []
    if instance_dir.exists():
        candidate_paths.extend(sorted(instance_dir.glob("*.json")))
    if snapshot_path.exists() and snapshot_path not in candidate_paths:
        candidate_paths.append(snapshot_path)
    if not candidate_paths:
        return _empty_transport_observability(
            "transport_trace_unavailable",
            available=False,
            degraded=False,
            status="unavailable",
        )

    snapshots: List[Dict[str, Any]] = []
    load_errors: List[str] = []
    for candidate_path in candidate_paths:
        try:
            raw = json.loads(candidate_path.read_text(encoding="utf-8"))
        except Exception as exc:
            load_errors.append(
                f"{candidate_path.name}: {_redact_transport_text(str(exc)) or 'load_failed'}"
            )
            continue
        if not isinstance(raw, dict):
            load_errors.append(f"{candidate_path.name}: invalid_payload")
            continue
        normalized = _normalize_transport_snapshot(raw, candidate_path)
        if normalized:
            snapshots.append(normalized)

    deduped_snapshots: Dict[str, Dict[str, Any]] = {}
    for item in snapshots:
        dedupe_key = str(item.get("instance_id") or item.get("source_path") or uuid.uuid4())
        existing = deduped_snapshots.get(dedupe_key)
        if existing is None:
            deduped_snapshots[dedupe_key] = item
            continue
        existing_ts = _parse_iso_ts(cast(Optional[str], existing.get("updated_at")))
        current_ts = _parse_iso_ts(cast(Optional[str], item.get("updated_at")))
        if current_ts and (existing_ts is None or current_ts >= existing_ts):
            deduped_snapshots[dedupe_key] = item

    snapshots = list(deduped_snapshots.values())

    if not snapshots:
        payload = _empty_transport_observability(
            "transport_trace_invalid_payload",
            available=True,
            degraded=True,
            status="fail",
        )
        payload["diagnostics"]["last_error"] = "; ".join(load_errors) or "transport_trace_invalid_payload"
        payload["sources"] = [str(path) for path in candidate_paths]
        return payload

    snapshots.sort(
        key=lambda item: _parse_iso_ts(cast(Optional[str], item.get("updated_at")))
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    configured_transports = list(
        dict.fromkeys(
            item.get("configured_transport")
            for item in snapshots
            if item.get("configured_transport")
        )
    )
    fallback_order: List[str] = []
    for item in snapshots:
        for entry in item.get("fallback_order", []):
            if entry not in fallback_order:
                fallback_order.append(entry)
    merged_events: List[Dict[str, Any]] = []
    for item in snapshots:
        merged_events.extend(
            event
            for event in item["diagnostics"].get("recent_events", [])
            if isinstance(event, dict)
        )
    merged_events.sort(key=lambda event: str(event.get("at") or ""), reverse=True)

    worst_status = max(
        (str(item.get("status") or "pass") for item in snapshots),
        key=_transport_status_rank,
    )
    if worst_status == "pass" and any(bool(item.get("degraded")) for item in snapshots):
        worst_status = "warn"
    focus = max(snapshots, key=_transport_snapshot_focus_key)
    payload = {
        "available": True,
        "degraded": bool(
            load_errors
            or any(bool(item.get("degraded")) for item in snapshots)
            or worst_status != "pass"
        ),
        "reason": "; ".join(load_errors) if load_errors else focus.get("reason"),
        "updated_at": focus.get("updated_at"),
        "status": worst_status,
        "active_transport": focus.get("active_transport"),
        "configured_transport": focus.get("configured_transport"),
        "configured_transports": configured_transports,
        "fallback_order": fallback_order,
        "connection_model": focus.get("connection_model") or "persistent-client",
        "snapshot_count": len(snapshots),
        "sources": [str(item.get("source_path")) for item in snapshots],
        "instances": [
            {
                "instance_id": item.get("instance_id"),
                "process_id": item.get("process_id"),
                "updated_at": item.get("updated_at"),
                "status": item.get("status"),
                "active_transport": item.get("active_transport"),
                "source_path": item.get("source_path"),
            }
            for item in snapshots
        ],
        "diagnostics": {
            "connect_attempts": sum(
                int(item["diagnostics"].get("connect_attempts") or 0)
                for item in snapshots
            ),
            "connect_retry_count": sum(
                int(item["diagnostics"].get("connect_retry_count") or 0)
                for item in snapshots
            ),
            "call_retry_count": sum(
                int(item["diagnostics"].get("call_retry_count") or 0)
                for item in snapshots
            ),
            "request_retries": max(
                int(item["diagnostics"].get("request_retries") or 0)
                for item in snapshots
            ),
            "fallback_count": sum(
                int(item["diagnostics"].get("fallback_count") or 0)
                for item in snapshots
            ),
            "reuse_count": sum(
                int(item["diagnostics"].get("reuse_count") or 0)
                for item in snapshots
            ),
            "last_connected_at": focus["diagnostics"].get("last_connected_at"),
            "connect_latency_ms": _merge_transport_latency_summaries(
                snapshots, merged_events
            ),
            "last_error": focus["diagnostics"].get("last_error"),
            "last_health_check_at": focus["diagnostics"].get("last_health_check_at"),
            "last_health_check_error": focus["diagnostics"].get(
                "last_health_check_error"
            ),
            "healthcheck_tool": focus["diagnostics"].get("healthcheck_tool"),
            "healthcheck_ttl_ms": focus["diagnostics"].get("healthcheck_ttl_ms"),
            "recent_events": merged_events[:24],
            "exception_breakdown": _build_transport_exception_breakdown(
                snapshots=snapshots,
                merged_events=merged_events[:24],
                load_errors=load_errors,
                focus=focus,
            ),
        },
        "last_report": focus.get("last_report"),
    }
    return payload


def _load_transport_observability() -> Dict[str, Any]:
    return _load_single_transport_observability(_transport_snapshot_path())
