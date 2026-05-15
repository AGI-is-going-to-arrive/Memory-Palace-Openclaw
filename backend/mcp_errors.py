"""Typed exceptions for MCP tool layer — replaces string-marker error codes."""

from enum import Enum


class GuardAction(str, Enum):
    """Valid guard actions."""
    ADD = "ADD"
    UPDATE = "UPDATE"
    NOOP = "NOOP"
    DELETE = "DELETE"
    IGNORE = "IGNORE"


class GuardOutcomeReason(str, Enum):
    """Structured guard outcome reasons — replaces inline string markers."""
    INVALID_GUARD_ACTION = "invalid_guard_action"
    WRITE_GUARD_UNAVAILABLE = "write_guard_unavailable"
    WRITE_GUARD_SEMANTIC_FAILED = "write_guard_semantic_failed"
    WRITE_GUARD_KEYWORD_FAILED = "write_guard_keyword_failed"
    GUARD_LLM_TIMEOUT = "guard_llm_timeout"
    GUARD_LLM_ERROR = "guard_llm_error"


class MCPError(Exception):
    """Base exception for MCP tool errors."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}:{detail}" if detail else reason)

    def to_payload(self) -> dict:
        """Generate backward-compatible error payload."""
        return {"ok": False, "reason": str(self)}


class InvalidGuardActionError(MCPError):
    """Raised when guard action is invalid or missing."""

    def __init__(self, marker_value: str = ""):
        super().__init__(GuardOutcomeReason.INVALID_GUARD_ACTION.value, marker_value)


class WriteGuardUnavailableError(MCPError):
    """Raised when write guard service is unavailable."""

    def __init__(self, detail: str = ""):
        super().__init__(GuardOutcomeReason.WRITE_GUARD_UNAVAILABLE.value, detail)


class InvalidPayloadError(MCPError):
    """Raised when a tool receives invalid payload."""

    def __init__(self, payload_type: str, detail: str = ""):
        super().__init__(f"invalid_{payload_type}_payload", detail)


class InvalidApiKeyError(MCPError):
    """Raised when API key is invalid or missing."""

    def __init__(self):
        super().__init__("invalid_or_missing_api_key")


class InvalidReviewTokenError(MCPError):
    """Raised when review token is invalid."""

    def __init__(self):
        super().__init__("invalid_review_token")
