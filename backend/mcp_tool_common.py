import re
from typing import Any, Callable, Dict


def event_preview_impl(text: str, max_chars: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars] + "..."


def normalize_guard_decision_impl(
    decision: Any, *, allow_bypass: bool = False
) -> Dict[str, Any]:
    if not isinstance(decision, dict):
        decision = {}
    reason = str(decision.get("reason") or "").strip()
    has_action = "action" in decision
    raw_action = str(decision.get("action") or "").strip().upper() if has_action else ""
    action = raw_action
    valid_actions = {"ADD", "UPDATE", "NOOP", "DELETE"}
    if allow_bypass:
        valid_actions.add("BYPASS")
    if action not in valid_actions:
        action = "NOOP"
        marker_value = raw_action or ("EMPTY" if has_action else "MISSING")
        marker = f"invalid_guard_action:{marker_value}"
        reason = marker if not reason else f"{marker}; {reason}"
    method = str(decision.get("method") or "none").strip().lower() or "none"
    target_id = decision.get("target_id")
    if not isinstance(target_id, int) or target_id <= 0:
        target_id = None
    target_uri = decision.get("target_uri")
    if not isinstance(target_uri, str) or not target_uri.strip():
        target_uri = None

    degrade_reasons = decision.get("degrade_reasons")
    if not isinstance(degrade_reasons, list):
        degrade_reasons = []
    degrade_reasons = [item for item in degrade_reasons if isinstance(item, str) and item]

    return {
        "action": action,
        "method": method,
        "reason": reason,
        "target_id": target_id,
        "target_uri": target_uri,
        "degraded": bool(decision.get("degraded")),
        "degrade_reasons": degrade_reasons,
    }


def guard_fields_impl(decision: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "guard_action": decision.get("action"),
        "guard_reason": decision.get("reason"),
        "guard_method": decision.get("method"),
        "guard_target_id": decision.get("target_id"),
        "guard_target_uri": decision.get("target_uri"),
    }


def tool_response_impl(
    *, to_json: Callable[[Dict[str, Any]], str], ok: bool, message: str, **extra: Any
) -> str:
    payload: Dict[str, Any] = {"ok": bool(ok), "message": message}
    payload.update(extra)
    return to_json(payload)


def trim_sentence_impl(text: str, limit: int = 90) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if limit <= 0:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    if limit <= 3:
        return cleaned[:limit]
    return cleaned[: limit - 3].rstrip() + "..."
