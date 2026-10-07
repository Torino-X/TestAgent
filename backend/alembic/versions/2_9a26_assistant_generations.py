"""add_assistant_message_generations

Phase 2.9A.26+: Track each regeneration of an assistant message.

Schema:
  * ``assistant_message_generations``: one row per generation attempt.
  * ``is_active`` flag: exactly one generation per message is active.
  * UNIQUE ``(message_id, generation_no)``: enforces monotonic versioning.

Revision ID: 2_9a26_assistant_generations
Revises: 2_9a26_assistant_feedbacks
Create Date: 2026-07-28 23:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2_9a26_assistant_generations"
down_revision: Union[str, None] = "2_9a26_assistant_feedbacks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_message_generations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("generation_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("content_markdown", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            server_onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKey("messages.id", name="fk_generation_message"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_generation_public_id"),
        sa.UniqueConstraint(
            "message_id",
            "generation_no",
            name="uq_generation_message_no",
        ),
    )
    op.create_index(
        "ix_generation_message_id",
        "assistant_message_generations",
        ["message_id"],
    )
    op.create_index(
        "ix_generation_status",
        "assistant_message_generations",
        ["status"],
    )


def downgrade() -> None:
    op.drop_index("ix_generation_status", table_name="assistant_message_generations")
    op.drop_index("ix_generation_message_id", table_name="assistant_message_generations")
    op.drop_table("assistant_message_generations")
