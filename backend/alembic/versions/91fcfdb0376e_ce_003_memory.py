"""ce_003_memory

Memory：创建 context_memories / context_memory_sources /
context_workspace_instructions。

长期记忆与人工项目指令。人工指令独立表，保持高权威与自动提取经验的边界。

使用 alembic op.create_table 操作，确保 alembic_version 正确推进。

Revision ID: 91fcfdb0376e
Revises: cd2c083cdad6
Create Date: 2026-08-04 22:53:44.735829
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '91fcfdb0376e'
down_revision: Union[str, None] = 'cd2c083cdad6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ascii_bin(length: int) -> sa.String:
    return sa.String(length=length, collation="ascii_bin")


def upgrade() -> None:
    # ── 1. context_memories ─────────────────────────────────────────
    op.create_table(
        "context_memories",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("workspace_key", _ascii_bin(191), nullable=True),
        sa.Column("agent_type", sa.String(length=64), nullable=True),
        sa.Column("memory_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'candidate'")),
        sa.Column("activation_source", sa.String(length=32), nullable=False, server_default=sa.text("'auto'")),
        sa.Column("confidence", sa.Numeric(6, 5), nullable=False, server_default=sa.text("0.5")),
        sa.Column("importance", sa.SmallInteger(), nullable=False, server_default=sa.text("3")),
        sa.Column("quality_score", sa.Numeric(6, 5), nullable=True),
        sa.Column("content_hash", sa.CHAR(length=64, collation="ascii_bin"), nullable=False),
        sa.Column("dedupe_key", sa.String(length=191), nullable=True),
        sa.Column("idempotency_key", _ascii_bin(191), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("supersedes_memory_id", sa.BigInteger(), nullable=True),
        sa.Column("consolidated_into_memory_id", sa.BigInteger(), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_accessed_at", sa.DateTime(), nullable=True),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_by_type", sa.String(length=32), nullable=False, server_default=sa.text("'system'")),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("public_id", name="uq_context_memories_public_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_context_memories_idempotency"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_context_memories_user"),
        sa.ForeignKeyConstraint(["supersedes_memory_id"], ["context_memories.id"], name="fk_context_memories_supersedes"),
        sa.ForeignKeyConstraint(["consolidated_into_memory_id"], ["context_memories.id"], name="fk_context_memories_consolidated"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_context_memories_scope", "context_memories", ["user_id", "scope_type", "workspace_key", "status"])
    op.create_index("idx_context_memories_dedupe", "context_memories", ["user_id", "dedupe_key"])
    op.create_index("idx_context_memories_type", "context_memories", ["user_id", "memory_type", "status"])

    # ── 2. context_memory_sources ───────────────────────────────────
    op.create_table(
        "context_memory_sources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("memory_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_public_id", sa.String(length=64), nullable=True),
        sa.Column("source_version", sa.String(length=64), nullable=True),
        sa.Column("relation_type", sa.String(length=32), nullable=False),
        sa.Column("source_digest", sa.CHAR(length=64, collation="ascii_bin"), nullable=True),
        sa.Column("evidence_excerpt", sa.String(length=1000), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "memory_id", "source_type", "source_public_id", "relation_type",
            name="uq_memory_source_relation",
        ),
        sa.ForeignKeyConstraint(["memory_id"], ["context_memories.id"], name="fk_memory_sources_memory"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_memory_sources_user"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_memory_sources_user_source", "context_memory_sources", ["user_id", "source_type", "source_public_id"])

    # ── 3. context_workspace_instructions ───────────────────────────
    op.create_table(
        "context_workspace_instructions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_key", _ascii_bin(191), nullable=False),
        sa.Column("instruction_key", _ascii_bin(128), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default=sa.text("5")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("source_public_id", sa.String(length=64), nullable=True),
        sa.Column("content_hash", sa.CHAR(length=64, collation="ascii_bin"), nullable=False),
        sa.Column("idempotency_key", _ascii_bin(191), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("supersedes_instruction_id", sa.BigInteger(), nullable=True),
        sa.Column("effective_from", sa.DateTime(), nullable=True),
        sa.Column("effective_to", sa.DateTime(), nullable=True),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("archived_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("public_id", name="uq_workspace_instructions_public_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_workspace_instructions_idempotency"),
        sa.UniqueConstraint(
            "user_id", "workspace_key", "instruction_key", "version",
            name="uq_workspace_instruction_version",
        ),
        sa.CheckConstraint("priority BETWEEN 1 AND 10", name="ck_workspace_instructions_priority"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_workspace_instructions_user"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], name="fk_workspace_instructions_creator"),
        sa.ForeignKeyConstraint(
            ["supersedes_instruction_id"], ["context_workspace_instructions.id"],
            name="fk_workspace_instructions_supersedes",
        ),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_workspace_instructions_active", "context_workspace_instructions", ["user_id", "workspace_key", "status", "priority"])
    op.create_index("idx_workspace_instructions_category", "context_workspace_instructions", ["user_id", "workspace_key", "category"])


def downgrade() -> None:
    op.drop_table("context_workspace_instructions")
    op.drop_table("context_memory_sources")
    op.drop_table("context_memories")
