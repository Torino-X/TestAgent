"""allow long normal-chat message content on MySQL

Revision ID: 20260924_msg_mediumtext
Revises: 20260916_template_cover
Create Date: 2026-09-24

``TEXT`` is capped at 64 KiB on MySQL.  A real Context Engine waterline
scenario can legitimately submit a 50k-character multilingual turn, and that
turn must persist before normal-chat routing and Context Engine preflight.
``MEDIUMTEXT`` supplies a 16 MiB byte capacity without affecting the portable
SQLite/PostgreSQL ``Text`` representation.
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy.dialects import mysql


revision: str = "20260924_msg_mediumtext"
down_revision: Union[str, None] = "20260916_template_cover"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        op.alter_column(
            "messages",
            "content",
            existing_type=mysql.TEXT(),
            type_=mysql.MEDIUMTEXT(),
            existing_nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "mysql":
        # MySQL rejects this change if any existing row exceeds TEXT capacity;
        # that protects long user messages from silent truncation.
        op.alter_column(
            "messages",
            "content",
            existing_type=mysql.MEDIUMTEXT(),
            type_=mysql.TEXT(),
            existing_nullable=True,
        )
