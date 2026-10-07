"""attachment_understanding_phase1

Revision ID: 20260809au01
Revises: 1b640574c715
Create Date: 2026-08-09 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260809au01"
down_revision: Union[str, None] = "1b640574c715"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index["name"] == index_name for index in inspector.get_indexes(table_name))


def _create_index_if_missing(index_name: str, table_name: str, columns: list[str]) -> None:
    if _table_exists(table_name) and not _index_exists(table_name, index_name):
        op.create_index(index_name, table_name, columns)


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    if _table_exists(table_name) and _index_exists(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    if not _table_exists("file_semantic_profiles"):
        op.create_table(
            "file_semantic_profiles",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("public_id", sa.String(length=64), nullable=False),
            sa.Column("file_id", sa.BigInteger(), nullable=False),
            sa.Column("user_id", sa.BigInteger(), nullable=False),
            sa.Column("conversation_id", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("document_kind", sa.String(length=64), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("semantic_labels_json", sa.JSON(), nullable=True),
            sa.Column("possible_usages_json", sa.JSON(), nullable=True),
            sa.Column("characteristics_json", sa.JSON(), nullable=True),
            sa.Column("confidence", sa.Numeric(6, 5), nullable=True),
            sa.Column("classifier_version", sa.String(length=64), nullable=True),
            sa.Column("source_hash", sa.String(length=128), nullable=True),
            sa.Column("error_code", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
            sa.ForeignKeyConstraint(["file_id"], ["uploaded_files.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("file_id", name="uq_file_semantic_profiles_file_id"),
            sa.UniqueConstraint("public_id", name="uq_file_semantic_profiles_public_id"),
        )
    _create_index_if_missing(
        "ix_file_semantic_profiles_user_conversation",
        "file_semantic_profiles",
        ["user_id", "conversation_id"],
    )

    if not _table_exists("message_attachments"):
        op.create_table(
            "message_attachments",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("message_id", sa.BigInteger(), nullable=False),
            sa.Column("file_id", sa.BigInteger(), nullable=False),
            sa.Column("position", sa.BigInteger(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["file_id"], ["uploaded_files.id"]),
            sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("message_id", "file_id", name="uq_message_attachments_message_file"),
            sa.UniqueConstraint("message_id", "position", name="uq_message_attachments_message_position"),
        )
    _create_index_if_missing("ix_message_attachments_file_id", "message_attachments", ["file_id"])

    if not _table_exists("task_file_bindings"):
        op.create_table(
            "task_file_bindings",
            sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
            sa.Column("task_id", sa.BigInteger(), nullable=False),
            sa.Column("file_id", sa.BigInteger(), nullable=False),
            sa.Column("binding_role", sa.String(length=64), nullable=False),
            sa.Column("position", sa.BigInteger(), nullable=False),
            sa.Column("is_primary", sa.Boolean(), nullable=False),
            sa.Column("binding_source", sa.String(length=64), nullable=False),
            sa.Column("confidence", sa.Numeric(6, 5), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
            sa.ForeignKeyConstraint(["file_id"], ["uploaded_files.id"]),
            sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("task_id", "file_id", "binding_role", name="uq_task_file_bindings_task_file_role"),
        )
    _create_index_if_missing("ix_task_file_bindings_file_id", "task_file_bindings", ["file_id"])


def downgrade() -> None:
    _drop_index_if_exists("ix_task_file_bindings_file_id", "task_file_bindings")
    if _table_exists("task_file_bindings"):
        op.drop_table("task_file_bindings")
    _drop_index_if_exists("ix_message_attachments_file_id", "message_attachments")
    if _table_exists("message_attachments"):
        op.drop_table("message_attachments")
    _drop_index_if_exists("ix_file_semantic_profiles_user_conversation", "file_semantic_profiles")
    if _table_exists("file_semantic_profiles"):
        op.drop_table("file_semantic_profiles")
