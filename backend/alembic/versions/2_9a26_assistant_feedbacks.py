"""add_assistant_message_feedbacks

Phase 2.9A.26+: Per-user feedback (like / dislike) on assistant messages.

Schema:
  * ``assistant_message_feedbacks``: one row per (user, assistant message).
  * Feedback is exclusive — switching like<->dislike is an UPSERT not an
    insert.  Enforced by the unique index ``uq_assistant_feedback_user_message``.
  * MySQL is the system of record; Redis is optional cache.
  * Phase 3 will add a generation column — for now the natural key is
    ``(user_id, message_id)``.

Indexes:
  * PK on ``id``.
  * UNIQUE on ``(user_id, message_id)`` to enforce the "one feedback per
    user/message" contract and accelerate the common fetch path.
  * INDEX on ``message_id`` for "all feedback on a given message"
    lookups used by analytics/admin reviews (not exercised in the UI).

Revision ID: 2_9a26_assistant_feedbacks
Revises: 2_8r_merge_heads
Create Date: 2026-07-28 22:50:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2_9a26_assistant_feedbacks"
down_revision: Union[str, None] = "2_8r_merge_heads"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "assistant_message_feedbacks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("feedback_type", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            server_onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKey("users.id", name="fk_assistant_feedback_user"),
        sa.ForeignKey("conversations.id", name="fk_assistant_feedback_conversation"),
        sa.ForeignKey("messages.id", name="fk_assistant_feedback_message"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_assistant_feedback_public_id"),
        sa.UniqueConstraint(
            "user_id",
            "message_id",
            name="uq_assistant_feedback_user_message",
        ),
    )
    op.create_index(
        "ix_assistant_feedback_user_id",
        "assistant_message_feedbacks",
        ["user_id"],
    )
    op.create_index(
        "ix_assistant_feedback_conversation_id",
        "assistant_message_feedbacks",
        ["conversation_id"],
    )
    op.create_index(
        "ix_assistant_feedback_message_id",
        "assistant_message_feedbacks",
        ["message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_feedback_message_id", table_name="assistant_message_feedbacks")
    op.drop_index("ix_assistant_feedback_conversation_id", table_name="assistant_message_feedbacks")
    op.drop_index("ix_assistant_feedback_user_id", table_name="assistant_message_feedbacks")
    op.drop_table("assistant_message_feedbacks")
