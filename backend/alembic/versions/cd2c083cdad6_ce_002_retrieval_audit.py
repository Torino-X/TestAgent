"""ce_002_retrieval_audit

Retrieval Audit：创建 context_index_documents / context_index_chunks /
context_index_jobs / context_retrieval_runs / context_retrieval_candidates。

索引检索审计表。向量数组不进入 MySQL（只存外部引用与 metadata）。

使用 alembic op.create_table 操作，确保 alembic_version 正确推进。

Revision ID: cd2c083cdad6
Revises: 525edff4e293
Create Date: 2026-08-04 22:53:43.479325
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cd2c083cdad6'
down_revision: Union[str, None] = '525edff4e293'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ascii_bin(length: int) -> sa.String:
    return sa.String(length=length, collation="ascii_bin")


def upgrade() -> None:
    # ── 1. context_index_documents ──────────────────────────────────
    op.create_table(
        "context_index_documents",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_key", _ascii_bin(191), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_public_id", sa.String(length=64), nullable=False),
        sa.Column("source_version", sa.String(length=64), nullable=False, server_default=sa.text("'1'")),
        sa.Column("source_digest", sa.CHAR(length=64, collation="ascii_bin"), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("chunk_policy_key", sa.String(length=64), nullable=False),
        sa.Column("chunk_policy_version", sa.String(length=32), nullable=False),
        sa.Column("lexical_index_status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("vector_index_status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("embedding_provider", sa.String(length=64), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=True),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("external_index_namespace", sa.String(length=191), nullable=True),
        sa.Column("external_document_ref", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("idempotency_key", _ascii_bin(191), nullable=False),
        sa.Column("indexed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("public_id", name="uq_index_documents_public_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_index_documents_idempotency"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_index_documents_user"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_index_documents_scope_time", "context_index_documents", ["user_id", "workspace_key", "created_at"])
    op.create_index("idx_index_documents_source", "context_index_documents", ["user_id", "source_type", "source_public_id"])
    op.create_index("idx_index_documents_status", "context_index_documents", ["status", "created_at"])

    # ── 2. context_index_chunks ─────────────────────────────────────
    op.create_table(
        "context_index_chunks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("section_path", sa.String(length=1000), nullable=True),
        sa.Column("source_locator_json", sa.JSON(), nullable=True),
        sa.Column("content", sa.Text(length=16777215).with_variant(sa.Text(), "mysql"), nullable=False),
        sa.Column("normalized_content", sa.Text(length=16777215).with_variant(sa.Text(), "mysql"), nullable=True),
        sa.Column("content_hash", sa.CHAR(length=64, collation="ascii_bin"), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(length=16), nullable=True),
        sa.Column("external_vector_ref", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("public_id", name="uq_index_chunks_public_id"),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_index_chunks_document_index"),
        sa.UniqueConstraint("document_id", "content_hash", name="uq_index_chunks_document_hash"),
        sa.ForeignKeyConstraint(["document_id"], ["context_index_documents.id"], name="fk_index_chunks_document"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_index_chunks_user"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_index_chunks_user_status", "context_index_chunks", ["user_id", "status"])
    op.create_index("idx_index_chunks_document_status", "context_index_chunks", ["document_id", "status"])

    # ── 3. context_index_jobs ───────────────────────────────────────
    op.create_table(
        "context_index_jobs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("claimed_until", sa.DateTime(), nullable=True),
        sa.Column("idempotency_key", _ascii_bin(191), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("public_id", name="uq_index_jobs_public_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_index_jobs_idempotency"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_index_jobs_user"),
        sa.ForeignKeyConstraint(["document_id"], ["context_index_documents.id"], name="fk_index_jobs_document"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_index_jobs_claim", "context_index_jobs", ["status", "next_retry_at", "priority", "created_at"])
    op.create_index("idx_index_jobs_lease", "context_index_jobs", ["status", "claimed_until"])
    op.create_index("idx_index_jobs_document", "context_index_jobs", ["document_id", "status"])

    # ── 4. context_retrieval_runs ───────────────────────────────────
    op.create_table(
        "context_retrieval_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_key", _ascii_bin(191), nullable=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column("context_snapshot_public_id", sa.String(length=64), nullable=True),
        sa.Column("call_site", _ascii_bin(128), nullable=False),
        sa.Column("retrieval_channel", sa.String(length=32), nullable=False),
        sa.Column("retrieval_policy_key", sa.String(length=64), nullable=False),
        sa.Column("retrieval_policy_version", sa.String(length=32), nullable=False),
        sa.Column("query_hash", sa.CHAR(length=64, collation="ascii_bin"), nullable=False),
        sa.Column("query_excerpt", sa.String(length=1000), nullable=True),
        sa.Column("query_metadata_json", sa.JSON(), nullable=True),
        sa.Column("requested_candidate_limit", sa.Integer(), nullable=False),
        sa.Column("requested_final_limit", sa.Integer(), nullable=False),
        sa.Column("lexical_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("vector_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("rerank_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("reranker_model", sa.String(length=128), nullable=True),
        sa.Column("reranker_version", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'running'")),
        sa.Column("fallback_code", sa.String(length=64), nullable=True),
        sa.Column("fallback_detail", sa.String(length=1000), nullable=True),
        sa.Column("recalled_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("reranked_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("selected_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("recall_latency_ms", sa.Integer(), nullable=True),
        sa.Column("rerank_latency_ms", sa.Integer(), nullable=True),
        sa.Column("total_latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("public_id", name="uq_retrieval_runs_public_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_retrieval_runs_user"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_retrieval_runs_conversation"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], name="fk_retrieval_runs_task"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], name="fk_retrieval_runs_agent_run"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_retrieval_runs_scope_time", "context_retrieval_runs", ["user_id", "workspace_key", "created_at"])
    op.create_index("idx_retrieval_runs_task", "context_retrieval_runs", ["task_id", "created_at"])
    op.create_index("idx_retrieval_runs_call_site", "context_retrieval_runs", ["call_site", "created_at"])
    op.create_index("idx_retrieval_runs_snapshot", "context_retrieval_runs", ["context_snapshot_public_id"])

    # ── 5. context_retrieval_candidates ─────────────────────────────
    op.create_table(
        "context_retrieval_candidates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("retrieval_run_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_public_id", sa.String(length=64), nullable=False),
        sa.Column("index_document_id", sa.BigInteger(), nullable=True),
        sa.Column("index_chunk_id", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.CHAR(length=64, collation="ascii_bin"), nullable=False),
        sa.Column("content_excerpt", sa.String(length=1000), nullable=True),
        sa.Column("estimated_tokens", sa.Integer(), nullable=True),
        sa.Column("raw_rank", sa.Integer(), nullable=True),
        sa.Column("raw_score", sa.Numeric(12, 8), nullable=True),
        sa.Column("normalized_score", sa.Numeric(12, 8), nullable=True),
        sa.Column("rerank_score", sa.Numeric(12, 8), nullable=True),
        sa.Column("final_rank", sa.Integer(), nullable=True),
        sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("drop_reason", sa.String(length=64), nullable=True),
        sa.Column("source_quota_key", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "retrieval_run_id", "source_type", "source_public_id", "content_hash",
            name="uq_retrieval_candidate",
        ),
        sa.ForeignKeyConstraint(["retrieval_run_id"], ["context_retrieval_runs.id"], name="fk_retrieval_candidates_run"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_retrieval_candidates_user"),
        sa.ForeignKeyConstraint(["index_document_id"], ["context_index_documents.id"], name="fk_retrieval_candidates_document"),
        sa.ForeignKeyConstraint(["index_chunk_id"], ["context_index_chunks.id"], name="fk_retrieval_candidates_chunk"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_retrieval_candidates_run_selected", "context_retrieval_candidates", ["retrieval_run_id", "selected", "final_rank"])
    op.create_index("idx_retrieval_candidates_source", "context_retrieval_candidates", ["user_id", "source_type", "source_public_id"])
    op.create_index("idx_retrieval_candidates_chunk", "context_retrieval_candidates", ["index_chunk_id"])


def downgrade() -> None:
    op.drop_table("context_retrieval_candidates")
    op.drop_table("context_retrieval_runs")
    op.drop_table("context_index_jobs")
    op.drop_table("context_index_chunks")
    op.drop_table("context_index_documents")
