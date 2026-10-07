"""Repair the server default for ``tool_calls.output_truncated``.

Revision ID: 20260928_tool_call_truncated
Revises: 20260924_msg_mediumtext
Create Date: 2026-09-28

Some deployed databases contain the non-nullable column without the server
default declared by CE-001.  Raw audit INSERTs then fail before output
governance can populate the extended fields.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260928_tool_call_truncated"
down_revision: Union[str, None] = "20260924_msg_mediumtext"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "tool_calls",
        "output_truncated",
        existing_type=sa.Boolean(),
        existing_nullable=False,
        server_default=sa.text("0"),
    )


def downgrade() -> None:
    # CE-001 already intended this server default.  Keep it when rolling back
    # this drift-repair revision rather than restoring the broken deployment.
    pass
