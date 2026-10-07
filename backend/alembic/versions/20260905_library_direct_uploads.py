"""Allow user library uploads without a backing conversation."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260905_lib_upload"
down_revision: Union[str, None] = "20260810_rur_km"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "uploaded_files",
        "conversation_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.create_index(
        "ix_artifacts_library_active_order",
        "artifacts",
        ["user_id", "status", "deleted_at", "updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_artifacts_library_deleted_order",
        "artifacts",
        ["user_id", "deleted_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_uploaded_files_library_active_order",
        "uploaded_files",
        ["user_id", "deleted_at", "updated_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_uploaded_files_library_deleted_order",
        "uploaded_files",
        ["user_id", "deleted_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_uploaded_files_library_deleted_order", table_name="uploaded_files")
    op.drop_index("ix_uploaded_files_library_active_order", table_name="uploaded_files")
    op.drop_index("ix_artifacts_library_deleted_order", table_name="artifacts")
    op.drop_index("ix_artifacts_library_active_order", table_name="artifacts")
    op.alter_column(
        "uploaded_files",
        "conversation_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
