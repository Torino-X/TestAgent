"""Add bounded conversation evidence cache and audit receipts.

Revision ID: 20260928_evidence_cache
Revises: 20260928_context_ledger
Create Date: 2026-09-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260928_evidence_cache"
down_revision: Union[str, None] = "20260928_context_ledger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_evidence_cache",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("index_document_id", sa.BigInteger(), nullable=False),
        sa.Column("index_chunk_id", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_public_id", sa.String(64), nullable=False),
        sa.Column("source_version", sa.String(64), nullable=False),
        sa.Column("source_digest", sa.CHAR(64), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("section_path", sa.String(1000), nullable=True),
        sa.Column("content_excerpt", sa.Text(), nullable=False),
        sa.Column("estimated_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("relevance_score", sa.Numeric(12, 8), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("selected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("locator_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("evicted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["index_document_id"], ["context_index_documents.id"]),
        sa.ForeignKeyConstraint(["index_chunk_id"], ["context_index_chunks.id"]),
        sa.UniqueConstraint("public_id", name="uq_conversation_evidence_cache_public_id"),
        sa.UniqueConstraint("conversation_id", "index_chunk_id", name="uq_conversation_evidence_cache_chunk"),
    )
    op.create_index(
        "ix_conversation_evidence_cache_active",
        "conversation_evidence_cache",
        ["user_id", "conversation_id", "status", "pinned", "last_used_at"],
    )
    op.create_table(
        "conversation_evidence_audits",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("evidence_cache_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(128), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["evidence_cache_id"], ["conversation_evidence_cache.id"]),
        sa.UniqueConstraint("public_id", name="uq_conversation_evidence_audits_public_id"),
    )
    op.create_index(
        "ix_conversation_evidence_audits_conversation_created",
        "conversation_evidence_audits",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_evidence_audits_conversation_created", table_name="conversation_evidence_audits")
    op.drop_table("conversation_evidence_audits")
    op.drop_index("ix_conversation_evidence_cache_active", table_name="conversation_evidence_cache")
    op.drop_table("conversation_evidence_cache")
