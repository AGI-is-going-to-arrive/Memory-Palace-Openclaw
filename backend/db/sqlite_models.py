from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import declarative_base, relationship

from .sqlite_paths import _utc_now_naive

Base = declarative_base()


class Memory(Base):
    """A single memory unit with content and metadata."""

    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    content = Column(Text, nullable=False)
    deprecated = Column(Boolean, default=False)
    migrated_to = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=_utc_now_naive)
    vitality_score = Column(
        Float, default=1.0, server_default=text("1.0"), nullable=False
    )
    last_accessed_at = Column(DateTime, nullable=True)
    access_count = Column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    paths = relationship("Path", back_populates="memory")
    gists = relationship("MemoryGist", back_populates="memory")
    tags = relationship("MemoryTag", back_populates="memory")


class Path(Base):
    """A path pointing to a memory. Multiple paths can point to the same memory."""

    __tablename__ = "paths"

    domain = Column(String(64), primary_key=True, default="core")
    path = Column(String(512), primary_key=True)
    memory_id = Column(Integer, ForeignKey("memories.id"), nullable=False)
    created_at = Column(DateTime, default=_utc_now_naive)
    priority = Column(Integer, default=0)
    disclosure = Column(Text, nullable=True)

    memory = relationship("Memory", back_populates="paths")


class AutoPathCounter(Base):
    """Monotonic numeric path allocator per domain/parent path."""

    __tablename__ = "auto_path_counters"

    domain = Column(String(64), primary_key=True, default="core")
    parent_path = Column(String(512), primary_key=True, default="")
    next_id = Column(Integer, nullable=False, default=1, server_default=text("1"))
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive)


class MemoryChunk(Base):
    """Chunked text slices for memory-level retrieval."""

    __tablename__ = "memory_chunks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    memory_id = Column(Integer, ForeignKey("memories.id"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    chunk_text = Column(Text, nullable=False)
    char_start = Column(Integer, nullable=False, default=0)
    char_end = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_utc_now_naive)


class MemoryChunkVec(Base):
    """Persisted vectors for memory chunks (fallback pure-SQLite storage)."""

    __tablename__ = "memory_chunks_vec"

    chunk_id = Column(Integer, ForeignKey("memory_chunks.id"), primary_key=True)
    memory_id = Column(Integer, ForeignKey("memories.id"), nullable=False, index=True)
    vector = Column(Text, nullable=False)
    model = Column(String(64), nullable=False, default="hash-v1")
    dim = Column(Integer, nullable=False, default=64)
    created_at = Column(DateTime, default=_utc_now_naive)


class EmbeddingCache(Base):
    """Cache embeddings by deterministic text hash."""

    __tablename__ = "embedding_cache"

    cache_key = Column(String(128), primary_key=True)
    text_hash = Column(String(128), nullable=False, index=True)
    model = Column(String(64), nullable=False, default="hash-v1")
    embedding = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive)


class IndexMeta(Base):
    """Index runtime metadata and capability flags."""

    __tablename__ = "index_meta"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=_utc_now_naive, onupdate=_utc_now_naive)


class SchemaMigration(Base):
    """Applied schema migration records."""

    __tablename__ = "schema_migrations"

    version = Column(String(32), primary_key=True)
    applied_at = Column(DateTime, default=_utc_now_naive, nullable=False)
    checksum = Column(String(128), nullable=False)


class MemoryGist(Base):
    """Compact gist materialized from a memory body."""

    __tablename__ = "memory_gists"
    __table_args__ = (
        Index("idx_memory_gists_memory_id", "memory_id"),
        Index(
            "idx_memory_gists_memory_source_hash_unique",
            "memory_id",
            "source_content_hash",
            unique=True,
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    memory_id = Column(Integer, ForeignKey("memories.id"), nullable=False)
    gist_text = Column(Text, nullable=False)
    source_content_hash = Column(String(128), nullable=False)
    gist_method = Column(String(64), nullable=False, default="fallback")
    quality_score = Column(Float, nullable=True)
    created_at = Column(DateTime, default=_utc_now_naive)

    memory = relationship("Memory", back_populates="gists")


class MemoryTag(Base):
    """Structured tag extraction output for memories."""

    __tablename__ = "memory_tags"
    __table_args__ = (
        Index("idx_tags_value", "tag_value"),
        Index("idx_memory_tags_memory_id", "memory_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    memory_id = Column(Integer, ForeignKey("memories.id"), nullable=False)
    tag_type = Column(String(64), nullable=False)
    tag_value = Column(String(255), nullable=False)
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime, default=_utc_now_naive)

    memory = relationship("Memory", back_populates="tags")


class FlushQuarantine(Base):
    """Quarantined flush events preserved before mark_flushed() destroys them."""

    __tablename__ = "flush_quarantine"
    __table_args__ = (
        Index("idx_flush_quarantine_session", "session_id"),
        Index("idx_flush_quarantine_status", "status"),
        Index("idx_flush_quarantine_expires", "expires_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    summary = Column(Text, nullable=False)
    gist_text = Column(Text, nullable=True)
    trace_text = Column(Text, nullable=True)
    guard_action = Column(Text, nullable=False)
    guard_method = Column(Text, nullable=True)
    guard_reason = Column(Text, nullable=True)
    guard_target_uri = Column(Text, nullable=True)
    content_hash = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_utc_now_naive)
    expires_at = Column(Text, nullable=False)
    replayed_at = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="pending")


class GistAuditResult(Base):
    """LLM judge evaluation of gist quality (P3-2 feedback loop)."""

    __tablename__ = "gist_audit_results"
    __table_args__ = (
        Index("idx_gist_audit_gist_id", "gist_id"),
        Index("idx_gist_audit_memory_id", "memory_id"),
        Index("idx_gist_audit_created", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    gist_id = Column(Integer, nullable=False)
    memory_id = Column(Integer, nullable=False)
    gist_method = Column(Text, nullable=False)
    coverage_score = Column(Float, nullable=True)
    factual_preservation_score = Column(Float, nullable=True)
    actionability_score = Column(Float, nullable=True)
    missing_anchors = Column(Text, nullable=True)
    hallucination_flags = Column(Text, nullable=True)
    judge_model = Column(Text, nullable=True)
    judge_raw_response = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False, default=_utc_now_naive)
    source_content_hash = Column(Text, nullable=True)
