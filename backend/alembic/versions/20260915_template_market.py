"""Add canonical template marketplace and per-user template library."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260915_template_market"
down_revision: Union[str, None] = "20260913_doc_preview"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "template_assets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category_code", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags_json", sa.JSON(), nullable=True),
        sa.Column("visibility", sa.String(32), nullable=False, server_default="private"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("current_version_id", sa.BigInteger(), nullable=True),
        sa.Column("save_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.UniqueConstraint("public_id", name="uq_template_assets_public_id"),
    )
    op.create_index("ix_template_assets_owner_deleted", "template_assets", ["owner_user_id", "deleted_at"])
    op.create_index("ix_template_assets_market_updated", "template_assets", ["visibility", "status", "deleted_at", "updated_at"])
    op.create_index("ix_template_assets_category_market", "template_assets", ["category_code", "visibility", "status"])
    op.create_index("ix_template_assets_updated", "template_assets", ["updated_at"])

    op.create_table(
        "template_versions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("file_ext", sa.String(32), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("file_hash", sa.String(128), nullable=False),
        sa.Column("storage_path", sa.String(1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["template_id"], ["template_assets.id"]),
        sa.UniqueConstraint("public_id", name="uq_template_versions_public_id"),
        sa.UniqueConstraint("template_id", "version_no", name="uq_template_versions_template_version"),
    )
    op.create_index("ix_template_versions_template_created", "template_versions", ["template_id", "created_at"])
    op.create_index("ix_template_versions_file_hash", "template_versions", ["file_hash"])

    op.create_table(
        "user_templates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column("version_id", sa.BigInteger(), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["template_id"], ["template_assets.id"]),
        sa.ForeignKeyConstraint(["version_id"], ["template_versions.id"]),
        sa.UniqueConstraint("public_id", name="uq_user_templates_public_id"),
        sa.UniqueConstraint("user_id", "template_id", name="uq_user_templates_user_template"),
    )
    op.create_index("ix_user_templates_user_deleted_updated", "user_templates", ["user_id", "deleted_at", "updated_at"])
    op.create_index("ix_user_templates_user_source", "user_templates", ["user_id", "source_type"])


def downgrade() -> None:
    op.drop_index("ix_user_templates_user_source", table_name="user_templates")
    op.drop_index("ix_user_templates_user_deleted_updated", table_name="user_templates")
    op.drop_table("user_templates")
    op.drop_index("ix_template_versions_file_hash", table_name="template_versions")
    op.drop_index("ix_template_versions_template_created", table_name="template_versions")
    op.drop_table("template_versions")
    op.drop_index("ix_template_assets_updated", table_name="template_assets")
    op.drop_index("ix_template_assets_category_market", table_name="template_assets")
    op.drop_index("ix_template_assets_market_updated", table_name="template_assets")
    op.drop_index("ix_template_assets_owner_deleted", table_name="template_assets")
    op.drop_table("template_assets")
