"""Context Engine ORM models — 新增的 10 张上下文基础表。

表名与 Migration（ce_002/ce_003/ce_004）保持一致。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    JSON,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContextPayload(Base):
    __tablename__ = "context_payloads"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    workspace_key: Mapped[str | None] = mapped_column(String(191))
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_public_id: Mapped[str | None] = mapped_column(String(64))
    payload_type: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    storage_key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    content_encoding: Mapped[str | None] = mapped_column(String(32))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    char_count: Mapped[int | None] = mapped_column(BigInteger)
    estimated_tokens: Mapped[int | None] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    encryption_key_ref: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active")
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class ConversationContextLedger(Base):
    """Durable, content-free manifest of a conversation's logical working set.

    The ledger is intentionally not another prompt store.  Source data remains
    in messages, summaries, project documents, tasks and memories; this row
    records the current *materialized working-set* references and its stable
    five-category token accounting.  Individual LLM profiles may project a
    subset of those references, but the conversation usage card always reads
    this canonical view rather than an arbitrary internal LLM snapshot.
    """

    __tablename__ = "conversation_context_ledgers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id"), nullable=False, unique=True
    )
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    context_window_tokens: Mapped[int | None] = mapped_column(BigInteger)
    conversation_history_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    project_documents_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    task_context_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    user_memory_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    system_instructions_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    manifest_json: Mapped[dict | None] = mapped_column(JSON)
    materialized_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ConversationEvidenceCache(Base):
    """Bounded, traceable project evidence working set for one conversation.

    This is deliberately a cache of *selected evidence chunks*, not a second
    document store.  The source document/chunk keeps the authoritative body;
    this row records the small reusable projection, its source version and
    whether a user explicitly pinned it.
    """

    __tablename__ = "conversation_evidence_cache"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False)
    index_document_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("context_index_documents.id"), nullable=False)
    index_chunk_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("context_index_chunks.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False)
    source_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    section_path: Mapped[str | None] = mapped_column(String(1000))
    content_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    relevance_score: Mapped[float | None] = mapped_column(Numeric(12, 8))
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    selected_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    locator_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    evicted_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("conversation_id", "index_chunk_id", name="uq_conversation_evidence_cache_chunk"),
        Index("ix_conversation_evidence_cache_active", "user_id", "conversation_id", "status", "pinned", "last_used_at"),
    )


class ConversationEvidenceAudit(Base):
    """Append-only audit receipt for evidence cache materialization changes."""

    __tablename__ = "conversation_evidence_audits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False)
    evidence_cache_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversation_evidence_cache.id"))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(128))
    details_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_conversation_evidence_audits_conversation_created", "conversation_id", "created_at"),
    )


class ContextMemory(Base):
    __tablename__ = "context_memories"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False)
    workspace_key: Mapped[str | None] = mapped_column(String(191))
    agent_type: Mapped[str | None] = mapped_column(String(64))
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_content: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    activation_source: Mapped[str] = mapped_column(String(32), default="auto")
    confidence: Mapped[float] = mapped_column(Numeric(6, 5), default=0.5)
    importance: Mapped[int] = mapped_column(SmallInteger, default=3)
    quality_score: Mapped[float | None] = mapped_column(Numeric(6, 5))
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(191))
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_memory_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("context_memories.id"))
    consolidated_into_memory_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("context_memories.id"))
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_by_type: Mapped[str] = mapped_column(String(32), default="system")
    created_by_user_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class ContextMemorySource(Base):
    __tablename__ = "context_memory_sources"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    memory_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("context_memories.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_public_id: Mapped[str | None] = mapped_column(String(64))
    source_version: Mapped[str | None] = mapped_column(String(64))
    relation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_digest: Mapped[str | None] = mapped_column(CHAR(64))
    evidence_excerpt: Mapped[str | None] = mapped_column(String(1000))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class ContextWorkspaceInstruction(Base):
    __tablename__ = "context_workspace_instructions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    workspace_key: Mapped[str] = mapped_column(String(191), nullable=False)
    instruction_key: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, default=5)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    source_type: Mapped[str] = mapped_column(String(32), default="manual")
    source_public_id: Mapped[str | None] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_instruction_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("context_workspace_instructions.id")
    )
    effective_from: Mapped[datetime | None] = mapped_column(DateTime)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime)
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        CheckConstraint("priority BETWEEN 1 AND 10", name="ck_workspace_instructions_priority"),
    )


class ContextIndexDocument(Base):
    __tablename__ = "context_index_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    workspace_key: Mapped[str | None] = mapped_column(String(191))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), default="1")
    source_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(500))
    language: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    chunk_policy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    lexical_index_status: Mapped[str] = mapped_column(String(32), default="pending")
    vector_index_status: Mapped[str] = mapped_column(String(32), default="pending")
    embedding_provider: Mapped[str | None] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    embedding_dimension: Mapped[int | None] = mapped_column(Integer)
    external_index_namespace: Mapped[str | None] = mapped_column(String(191))
    external_document_ref: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class ContextIndexChunk(Base):
    __tablename__ = "context_index_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("context_index_documents.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[str | None] = mapped_column(String(1000))
    source_locator_json: Mapped[dict | None] = mapped_column(JSON)
    content: Mapped[str] = mapped_column(Text(length=16777215), nullable=False)
    normalized_content: Mapped[str | None] = mapped_column(Text(length=16777215))
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_tokens: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(16))
    external_vector_ref: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_index_chunks_document_index"),
        UniqueConstraint("document_id", "content_hash", name="uq_index_chunks_document_hash"),
    )


class ContextIndexJob(Base):
    __tablename__ = "context_index_jobs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    document_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("context_index_documents.id"), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    claimed_by: Mapped[str | None] = mapped_column(String(128))
    claimed_until: Mapped[datetime | None] = mapped_column(DateTime)
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(2000))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class ContextRetrievalRun(Base):
    __tablename__ = "context_retrieval_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    workspace_key: Mapped[str | None] = mapped_column(String(191))
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"))
    agent_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_runs.id"))
    context_snapshot_public_id: Mapped[str | None] = mapped_column(String(64))
    call_site: Mapped[str] = mapped_column(String(128), nullable=False)
    retrieval_channel: Mapped[str] = mapped_column(String(32), nullable=False)
    retrieval_policy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieval_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    query_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    query_excerpt: Mapped[str | None] = mapped_column(String(1000))
    query_metadata_json: Mapped[dict | None] = mapped_column(JSON)
    requested_candidate_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_final_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    lexical_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    vector_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rerank_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    reranker_model: Mapped[str | None] = mapped_column(String(128))
    reranker_version: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="running")
    fallback_code: Mapped[str | None] = mapped_column(String(64))
    fallback_detail: Mapped[str | None] = mapped_column(String(1000))
    recalled_count: Mapped[int] = mapped_column(Integer, default=0)
    reranked_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    recall_latency_ms: Mapped[int | None] = mapped_column(Integer)
    rerank_latency_ms: Mapped[int | None] = mapped_column(Integer)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class ContextRetrievalCandidate(Base):
    __tablename__ = "context_retrieval_candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    retrieval_run_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("context_retrieval_runs.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_public_id: Mapped[str] = mapped_column(String(64), nullable=False)
    index_document_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("context_index_documents.id"))
    index_chunk_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("context_index_chunks.id"))
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    content_excerpt: Mapped[str | None] = mapped_column(String(1000))
    estimated_tokens: Mapped[int | None] = mapped_column(Integer)
    raw_rank: Mapped[int | None] = mapped_column(Integer)
    raw_score: Mapped[float | None] = mapped_column(Numeric(12, 8))
    normalized_score: Mapped[float | None] = mapped_column(Numeric(12, 8))
    rerank_score: Mapped[float | None] = mapped_column(Numeric(12, 8))
    final_rank: Mapped[int | None] = mapped_column(Integer)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    drop_reason: Mapped[str | None] = mapped_column(String(64))
    source_quota_key: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class ContextCompactionRun(Base):
    __tablename__ = "context_compaction_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    workspace_key: Mapped[str | None] = mapped_column(String(191))
    conversation_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("conversations.id"))
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"))
    agent_run_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_runs.id"))
    context_snapshot_public_id: Mapped[str | None] = mapped_column(String(64))
    output_summary_public_id: Mapped[str | None] = mapped_column(String(64))
    recovery_payload_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("context_payloads.id"))
    call_site: Mapped[str] = mapped_column(String(128), nullable=False)
    compaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    trigger_type: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_config_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("model_configs.id"))
    model_name_snapshot: Mapped[str | None] = mapped_column(String(128))
    tokens_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tokens_after: Mapped[int | None] = mapped_column(BigInteger)
    target_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    compression_ratio: Mapped[float | None] = mapped_column(Numeric(10, 6))
    protected_anchors_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_refs_json: Mapped[dict | None] = mapped_column(JSON)
    dropped_refs_json: Mapped[dict | None] = mapped_column(JSON)
    recovery_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running")
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(2000))
    sampling_latency_ms: Mapped[int | None] = mapped_column(Integer)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


# 模块定位:Context Engine ORM 模型(10 张基础表)
#
# 表名与 Migration (alembic/versions/ce_002 / ce_003 / ce_004) 保持一致。
#
# 主要实体:
#   - ContextIndexDocument / ContextIndexJob  ——
#     Knowledge Base 上传的文档与索引任务;
#   - ContextPayload ——
#     抓取后的 raw payload(供 retrieve 用);
#   - ContextWorkspace / ContextWorkspaceInstruction ——
#     用户 workspace 与 instructions;
#   - ContextUserMemory / ContextProjectRule ——
#     学习到的记忆(用户级 / 项目级 scope);
#   - ContextIndexJob / ContextIndexChunk / ContextIndexEmbedding ——
#     chunk & embedding 持久化;
#   - ContextTraceRow ——
#     装配审计行(可选);
#
# 关键约束:
#   - 表名与 migration 100% 一致(否则 alembic 报错);
#   - index 列必须显式 INDEX(避免生产 scan);
#   - 不要让 ContextEngine 表与核心业务表 cross-schema foreign key;
#   - CE 升级路径在 ce_002 / ce_003 / ce_004 等专用 migration。
