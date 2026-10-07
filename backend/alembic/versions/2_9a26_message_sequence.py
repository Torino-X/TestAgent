"""add_conversation_sequence_and_trigger_message_id

Phase 2.9A.26+: Stable timeline ordering for hybrid conversations.

Adds three new columns that establish the canonical ordering for
messages, replies, and task triggers:

  * ``messages.conversation_sequence BIGINT NULL`` — monotonically
    increasing per-conversation sequence used by the front-end timeline
    to disambiguate same-second messages.

  * ``messages.reply_to_message_id BIGINT NULL`` — FK to the user message
    that an agent_text message replies to.  Replaces fragile "the most
    recent user message" inference.

  * ``agent_tasks.trigger_message_id BIGINT NULL`` — FK to the user
    message that triggered this task.  Replaces inference from
    ``user_instruction``.

Backfill (data migration) runs in this same migration:
  * ``conversation_sequence`` is assigned via a window function
    ``ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY
    created_at, id)``.  The same ordering is used by the front-end
    fallback path when the new column is missing.
  * ``reply_to_message_id`` and ``trigger_message_id`` are best-effort
    inferred for historical rows:
      * For agent_text: the closest preceding user_text in the same
        conversation becomes ``reply_to_message_id``.
      * For agent_tasks: the closest preceding user_text whose
        ``content`` matches ``agent_tasks.user_instruction`` becomes
        ``trigger_message_id``.

Both indexes are added to keep the new lookups off the table-scan path.

Revision ID: 2_9a26_message_sequence
Revises: 2_9a26_assistant_generations
Create Date: 2026-07-29 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2_9a26_message_sequence"
down_revision: Union[str, None] = "2_9a26_assistant_generations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Step 1: Add new columns (NULL allowed during backfill) ──
    op.add_column(
        "messages",
        sa.Column("conversation_sequence", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "messages",
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "agent_tasks",
        sa.Column("trigger_message_id", sa.BigInteger(), nullable=True),
    )

    # ── Step 2: Backfill conversation_sequence ──
    # ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY created_at, id)
    # gives the same canonical order the front-end fallback uses.
    op.execute(
        """
        UPDATE messages m
        JOIN (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY conversation_id
                       ORDER BY created_at ASC, id ASC
                   ) AS rn
            FROM messages
        ) r ON m.id = r.id
        SET m.conversation_sequence = r.rn
        """
    )

    # ── Step 3: Backfill reply_to_message_id for agent_text messages ──
    # Pick the user_text immediately preceding (by sequence) the
    # agent_text in the same conversation.
    op.execute(
        """
        UPDATE messages agent_msg
        JOIN (
            SELECT m.id AS agent_id,
                   (
                       SELECT user_msg.id
                       FROM messages user_msg
                       WHERE user_msg.conversation_id = m.conversation_id
                         AND user_msg.role = 'user'
                         AND user_msg.message_type = 'user_text'
                         AND user_msg.conversation_sequence < m.conversation_sequence
                       ORDER BY user_msg.conversation_sequence DESC
                       LIMIT 1
                   ) AS reply_to_id
            FROM messages m
            WHERE m.role = 'agent'
              AND m.message_type = 'agent_text'
              AND m.conversation_sequence IS NOT NULL
        ) picked ON agent_msg.id = picked.agent_id
        SET agent_msg.reply_to_message_id = picked.reply_to_id
        """
    )

    # ── Step 4: Backfill agent_tasks.trigger_message_id ──
    # Match the closest user_text whose content matches the task's
    # user_instruction in the same conversation.
    op.execute(
        """
        UPDATE agent_tasks t
        JOIN (
            SELECT t2.id AS task_id,
                   (
                       SELECT m.id
                       FROM messages m
                       WHERE m.conversation_id = t2.conversation_id
                         AND m.role = 'user'
                         AND m.message_type = 'user_text'
                         AND m.content = t2.user_instruction
                       ORDER BY m.created_at DESC
                       LIMIT 1
                   ) AS trigger_id
            FROM agent_tasks t2
        ) picked ON t.id = picked.task_id
        SET t.trigger_message_id = picked.trigger_id
        """
    )

    # ── Step 5: Add indexes / constraints ──
    op.create_index(
        "ix_messages_conversation_sequence",
        "messages",
        ["conversation_id", "conversation_sequence"],
    )
    op.create_index(
        "ix_messages_reply_to_message_id",
        "messages",
        ["reply_to_message_id"],
    )
    op.create_unique_constraint(
        "uq_messages_conversation_sequence",
        "messages",
        ["conversation_id", "conversation_sequence"],
    )
    op.create_foreign_key(
        "fk_messages_reply_to_message",
        "messages",
        "messages",
        ["reply_to_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_agent_tasks_trigger_message_id",
        "agent_tasks",
        ["trigger_message_id"],
    )
    op.create_foreign_key(
        "fk_agent_tasks_trigger_message",
        "agent_tasks",
        "messages",
        ["trigger_message_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_agent_tasks_trigger_message", "agent_tasks", type_="foreignkey")
    op.drop_index("ix_agent_tasks_trigger_message_id", table_name="agent_tasks")
    op.drop_column("agent_tasks", "trigger_message_id")

    op.drop_constraint("fk_messages_reply_to_message", "messages", type_="foreignkey")
    op.drop_constraint(
        "uq_messages_conversation_sequence", "messages", type_="unique"
    )
    op.drop_index("ix_messages_reply_to_message_id", table_name="messages")
    op.drop_index(
        "ix_messages_conversation_sequence", table_name="messages"
    )
    op.drop_column("messages", "reply_to_message_id")
    op.drop_column("messages", "conversation_sequence")