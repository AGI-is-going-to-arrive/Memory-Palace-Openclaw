"""Typed exceptions for SQLite client layer."""


class MemoryStoreError(Exception):
    """Base exception for memory store operations."""
    pass


class MemoryContentError(MemoryStoreError):
    """Raised when memory content validation fails."""
    pass


class MemoryPathError(MemoryStoreError):
    """Raised when memory path validation fails."""
    pass


class EmbeddingProviderError(MemoryStoreError):
    """Raised when embedding provider chain is blocked or fails."""
    pass


class VectorEngineNotReadyError(MemoryStoreError):
    """Raised when sqlite-vec KNN is not ready."""
    pass


class ImportGuardStateError(MemoryStoreError):
    """Raised when import guard detects invalid state."""

    def __init__(self, kind: str, detail: str = ""):
        self.kind = kind
        self.detail = detail
        super().__init__(f"{kind}: {detail}" if detail else kind)
