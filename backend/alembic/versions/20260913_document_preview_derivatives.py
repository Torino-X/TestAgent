"""Add durable private PDF-preview metadata."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260913_doc_preview"
down_revision: Union[str, None] = "20260908_project_core"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_previews",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("item_public_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_version", sa.String(191), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("preview_storage_path", sa.String(1024), nullable=True),
        sa.Column("preview_size", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("item_public_id", name="uq_document_previews_item_public_id"),
    )
    op.create_index(
        "ix_document_previews_user_status_updated",
        "document_previews",
        ["user_id", "status", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_previews_user_status_updated", table_name="document_previews")
    op.drop_table("document_previews")
