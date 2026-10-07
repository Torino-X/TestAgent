"""Persist the canonical conversation context working-set ledger.

Revision ID: 20260928_context_ledger
Revises: 20260928_tool_call_truncated
Create Date: 2026-09-28
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260928_context_ledger"
down_revision: Union[str, None] = "20260928_tool_call_truncated"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "conversation_context_ledgers",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("context_window_tokens", sa.BigInteger(), nullable=True),
        sa.Column("conversation_history_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("project_documents_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("task_context_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("user_memory_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("system_instructions_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("manifest_json", sa.JSON(), nullable=True),
        sa.Column("materialized_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.UniqueConstraint("public_id", name="uq_conversation_context_ledgers_public_id"),
        sa.UniqueConstraint("conversation_id", name="uq_conversation_context_ledgers_conversation"),
    )
    op.create_index(
        "ix_conversation_context_ledgers_user_conversation",
        "conversation_context_ledgers",
        ["user_id", "conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_context_ledgers_user_conversation", table_name="conversation_context_ledgers")
    op.drop_table("conversation_context_ledgers")
