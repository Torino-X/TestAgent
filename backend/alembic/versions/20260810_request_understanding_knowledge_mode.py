"""add conversation knowledge mode

Revision ID: 20260810_rur_km
Revises: 20260809au01
Create Date: 2026-08-10 09:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260810_rur_km"
down_revision: Union[str, None] = "20260809au01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def upgrade() -> None:
    if not _column_exists("conversations", "knowledge_mode"):
        op.add_column(
            "conversations",
            sa.Column(
                "knowledge_mode",
                sa.String(length=32),
                nullable=False,
                server_default="AUTO",
            ),
        )


def downgrade() -> None:
    if _column_exists("conversations", "knowledge_mode"):
        op.drop_column("conversations", "knowledge_mode")
