from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from db import get_sqlite_client


class SearchConsoleRequest(BaseModel):
    query: str = Field(min_length=1)
    mode: str = Field(default="hybrid")
    max_results: int = Field(default=8, ge=1, le=50)
    candidate_multiplier: int = Field(default=4, ge=1, le=20)
    include_session: bool = True
    session_id: Optional[str] = None
    filters: Dict[str, Any] = Field(default_factory=dict)
    scope_hint: Optional[str] = None


class VitalityCleanupQueryRequest(BaseModel):
    threshold: float = Field(default=0.35, ge=0.0)
    inactive_days: float = Field(default=14.0, ge=0.0)
    limit: int = Field(default=50, ge=1, le=500)
    domain: Optional[str] = None
    path_prefix: Optional[str] = None


class CleanupSelectionItem(BaseModel):
    memory_id: int = Field(ge=1)
    state_hash: str = Field(min_length=16, max_length=128)


class VitalityCleanupPrepareRequest(BaseModel):
    action: str = Field(default="delete")
    selections: List[CleanupSelectionItem] = Field(min_length=1, max_length=100)
    reviewer: Optional[str] = None
    ttl_seconds: int = Field(default=900, ge=60, le=3600)


class VitalityCleanupConfirmRequest(BaseModel):
    review_id: str = Field(min_length=8)
    token: str = Field(min_length=16)
    confirmation_phrase: str = Field(min_length=8)


class IndexJobCancelRequest(BaseModel):
    reason: str = Field(default="api_cancel", min_length=1, max_length=120)


class IndexJobRetryRequest(BaseModel):
    reason: str = Field(default="", max_length=120)


class ImportPrepareRequest(BaseModel):
    file_paths: List[str] = Field(min_length=1, max_length=200)
    actor_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    source: str = Field(default="external_import", min_length=1, max_length=128)
    reason: str = Field(default="manual_import", min_length=1, max_length=240)
    domain: str = Field(default="notes", min_length=1, max_length=32)
    parent_path: str = Field(default="", max_length=512)
    priority: int = Field(default=2, ge=0, le=9)


class ImportExecuteRequest(BaseModel):
    job_id: str = Field(min_length=8, max_length=64)


class ImportRollbackRequest(BaseModel):
    reason: str = Field(default="manual_rollback", min_length=1, max_length=240)


class LearnTriggerRequest(BaseModel):
    content: str = Field(min_length=1)
    source: str = Field(default="manual_review", min_length=1, max_length=128)
    reason: str = Field(default="", max_length=240)
    session_id: str = Field(min_length=1, max_length=128)
    actor_id: Optional[str] = Field(default=None, max_length=128)
    domain: str = Field(default="notes", min_length=1, max_length=32)
    path_prefix: str = Field(default="corrections", min_length=1, max_length=256)
    execute: bool = True


class _LazySQLiteClientProxy:
    def __init__(self, factory=get_sqlite_client):
        self._factory = factory
        self._client = None

    def _resolve(self):
        if self._client is None:
            self._client = self._factory()
        return self._client

    def __getattr__(self, name: str):
        return getattr(self._resolve(), name)
