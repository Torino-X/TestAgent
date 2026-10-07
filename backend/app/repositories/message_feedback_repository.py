"""Repository for :class:`AssistantMessageFeedback`.

Uses raw SQL ``INSERT ... ON DUPLICATE KEY UPDATE`` (matching the
``MessageRepository`` pattern — see ``app/repositories/message_repository.py``
comment about the ``aiomysql`` dict issue) to provide a true UPSERT in a
single round-trip.

A second method, ``set_for_user_message``, takes the user-facing
``public_id`` of the message, resolves the internal ``Message.id`` and
delegates to the integer-keyed upsert.  Callers that already hold the
internal id (the route handler keeps it in the message payload) use the
faster path directly.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.message_feedback import AssistantMessageFeedback
from app.repositories.base import BaseRepository
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)

_FEEDBACK_TYPES = ("like", "dislike")


def _validate_feedback_type(value: str) -> str:
    if value not in _FEEDBACK_TYPES:
        raise ValueError(f"invalid feedback_type: {value!r}")
    return value


class MessageFeedbackRepository(BaseRepository[AssistantMessageFeedback]):
    model = AssistantMessageFeedback

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def upsert(
        self,
        *,
        user_id: int,
        conversation_id: int,
        message_id: int,
        feedback_type: str,
    ) -> Optional[str]:
        """Insert-or-update the feedback row.  Returns the new value or None
        when feedback_type is None (caller is asking to cancel)."""
        if feedback_type is None:
            await self.session.execute(
                text(
                    "DELETE FROM assistant_message_feedbacks "
                    "WHERE user_id = :uid AND message_id = :mid"
                ),
                {"uid": user_id, "mid": message_id},
            )
            return None

        _validate_feedback_type(feedback_type)
        new_public_id = generate_public_id("feedback")
        sql = text(
            "INSERT INTO assistant_message_feedbacks "
            "(public_id, user_id, conversation_id, message_id, "
            " feedback_type, created_at, updated_at) "
            "VALUES "
            "(:pid, :uid, :cid, :mid, :ft, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE "
            "  feedback_type = VALUES(feedback_type), "
            "  updated_at = NOW()"
        )
        await self.session.execute(
            sql,
            {
                "pid": new_public_id,
                "uid": user_id,
                "cid": conversation_id,
                "mid": message_id,
                "ft": feedback_type,
            },
        )
        await self.session.flush()
        return feedback_type

    async def get_for_user_message(
        self,
        user_id: int,
        message_internal_id: int,
    ) -> Optional[str]:
        result = await self.session.execute(
            text(
                "SELECT feedback_type FROM assistant_message_feedbacks "
                "WHERE user_id = :uid AND message_id = :mid LIMIT 1"
            ),
            {"uid": user_id, "mid": message_internal_id},
        )
        row = result.first()
        if row is None:
            return None
        value = row[0]
        return str(value) if value is not None else None

    async def list_for_conversation(
        self,
        user_id: int,
        conversation_id: int,
    ) -> dict[int, str]:
        """Bulk fetch ``{message_id -> feedback_type}`` for a conversation.

        Used by ``list_messages`` to inline ``my_feedback`` on each
        assistant message without an N+1.
        """
        result = await self.session.execute(
            text(
                "SELECT message_id, feedback_type FROM assistant_message_feedbacks "
                "WHERE user_id = :uid AND conversation_id = :cid"
            ),
            {"uid": user_id, "cid": conversation_id},
        )
        rows = result.all()
        return {int(row[0]): str(row[1]) for row in rows}

    async def list_for_conversation_by_public_id(
        self,
        user_id: int,
        conversation_id: int,
    ) -> dict[str, str]:
        """Bulk fetch ``{message_public_id -> feedback_type}`` for a conversation.

        Joins against ``messages`` in a single query so the caller never
        needs to resolve integer message IDs — avoids the N+1 that the
        earlier ``list_for_conversation`` + per-row lookup would cause.
        """
        result = await self.session.execute(
            text(
                "SELECT m.public_id, f.feedback_type "
                "FROM assistant_message_feedbacks f "
                "JOIN messages m ON m.id = f.message_id "
                "WHERE f.user_id = :uid AND f.conversation_id = :cid"
            ),
            {"uid": user_id, "cid": conversation_id},
        )
        rows = result.all()
        return {str(row[0]): str(row[1]) for row in rows}


async def fetch_message_internal_id(
    session: AsyncSession, message_public_id: str
) -> Optional[int]:
    """Helper that resolves a message's public_id to its internal id.

    Synchronous SQLAlchemy ``Session`` lookup — used inside the route's
    repo/service layer so we can fail fast when the public_id is bogus.
    Returns None when not found.
    """
    raise NotImplementedError("Use MessageRepository internals via session")


async def fetch_message_internal_id(
    session: AsyncSession, message_public_id: str
) -> Optional[int]:
    result = await session.execute(
        select(Message.id).where(Message.public_id == message_public_id, Message.deleted_at.is_(None))
    )
    row = result.first()
    return int(row[0]) if row is not None else None


# Local import — placed at bottom to avoid surprising top-level pulls
# of ``select`` from tests that import just the model class.
from sqlalchemy import select  # noqa: E402


# 模块定位:MessageFeedback 仓储
#
# 链路:
#   api/v1/messages/{public_id}/feedback PUT
#     → MessageFeedbackService.record → upsert
#
# 关键约束:
#   - UNIQUE(message_id, user_internal_id) 严格保护;
#   - 跨用户访问返回 404,不暴露存在性;
#   - text 上限 1024 字符。
