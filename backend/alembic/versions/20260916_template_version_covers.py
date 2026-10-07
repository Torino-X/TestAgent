"""Persist version-scoped template card covers."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260916_template_cover"
down_revision: Union[str, None] = "20260915_template_market"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "template_versions",
        sa.Column("cover_status", sa.String(32), nullable=False, server_default="missing"),
    )
    op.add_column("template_versions", sa.Column("cover_kind", sa.String(32), nullable=True))
    op.add_column("template_versions", sa.Column("cover_storage_path", sa.String(1024), nullable=True))
    op.add_column("template_versions", sa.Column("cover_size", sa.BigInteger(), nullable=True))
    op.add_column("template_versions", sa.Column("cover_payload_json", sa.JSON(), nullable=True))
    op.add_column("template_versions", sa.Column("cover_error", sa.Text(), nullable=True))
    op.add_column("template_versions", sa.Column("cover_generated_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_template_versions_cover_status",
        "template_versions",
        ["cover_status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_template_versions_cover_status", table_name="template_versions")
    op.drop_column("template_versions", "cover_generated_at")
    op.drop_column("template_versions", "cover_error")
    op.drop_column("template_versions", "cover_payload_json")
    op.drop_column("template_versions", "cover_size")
    op.drop_column("template_versions", "cover_storage_path")
    op.drop_column("template_versions", "cover_kind")
    op.drop_column("template_versions", "cover_status")
