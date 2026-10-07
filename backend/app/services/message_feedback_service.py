"""Phase 2.9A.26+: Business logic for assistant message feedback.

Service contract:

  * **MySQL is source of truth.** Redis is a best-effort cache; if the
    cache call fails, the caller keeps going and we just emit a WARN log.
  * **One feedback per (user, message).** Switch like<->dislike is an
    UPSERT; None deletes the row.  The unique index
    ``uq_assistant_feedback_user_message`` is the actual contract enforcer.
  * **Access control.** We refuse feedback on messages that the user
    doesn't own — the lookup verifies (user_id, conversation_id) match
    first, throwing ``MessageNotFeedbackableError`` when the row can't
    be served.
  * **Audit logging.** We log ``feedback_change`` per request, including
    message public_id + the resulting value, but never the message body.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, MessageNotFeedbackableError
from app.models.message import Message
from app.repositories.message_feedback_repository import (
    MessageFeedbackRepository,
    fetch_message_internal_id,
)

logger = logging.getLogger(__name__)

_VALID_TYPES = {"like", "dislike", None}


class MessageFeedbackService:
    """Stateless service; bundles the feedback repository helpers."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        feedback_repo: Optional[MessageFeedbackRepository] = None,
    ) -> None:
        self._session = session
        self._repo = feedback_repo or MessageFeedbackRepository(session)

    async def set_feedback(
        self,
        *,
        user_id: int,
        conversation_public_id: str,
        message_public_id: str,
        feedback_type: Optional[str],
    ) -> Optional[str]:
        """Set (or cancel) feedback on a message.  Returns the new value.

        Raises ``MessageNotFeedbackableError`` if the message is missing,
        role-wrong, or owned by another user; ``ForbiddenError`` for
        cross-conversation access; ``ValueError`` for invalid
        ``feedback_type``.
        """
        if feedback_type not in _VALID_TYPES and feedback_type not in ("like", "dislike", None):
            raise ValueError(f"invalid feedback_type: {feedback_type!r}")

        # Phase 2.9A.26: explicit role/ownership check via two-step lookup
        # so we never leak the integer Message.id across users.
        message = await self._fetch_owned_message(
            user_id=user_id,
            message_public_id=message_public_id,
            conversation_public_id=conversation_public_id,
        )
        if message is None:
            raise MessageNotFeedbackableError("消息不存在或不属于当前会话")

        # Only agent_text (普通 chat reply) supports feedback.  Tool /
        # system / task-card messages deliberately reject.
        if message.role != "agent" or message.message_type != "agent_text":
            raise MessageNotFeedbackableError(
                "仅普通 Agent 文本回复支持点赞或点踩"
            )

        # Resolve the conversation's internal id (FK column for the row).
        from app.models.conversation import Conversation

        result = await self._session.execute(
            select(Conversation.id).where(
                Conversation.public_id == conversation_public_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        conv_row = result.first()
        if conv_row is None:
            raise MessageNotFeedbackableError("会话不存在")
        conversation_internal_id = int(conv_row[0])

        new_value = await self._repo.upsert(
            user_id=user_id,
            conversation_id=conversation_internal_id,
            message_id=message.id,
            feedback_type=feedback_type,
        )

        logger.info(
            "feedback_change | user=%d | conv=%s | msg=%s | "
            "feedback=%s",
            user_id,
            conversation_public_id,
            message_public_id,
            new_value or "none",
        )
        return new_value

    async def get_feedback(
        self,
        *,
        user_id: int,
        message_internal_id: int,
    ) -> Optional[str]:
        """Read-only fetch — used by ``list_messages`` to inline
        ``my_feedback`` per message without an N+1."""
        return await self._repo.get_for_user_message(
            user_id=user_id,
            message_internal_id=message_internal_id,
        )

    async def list_feedbacks_for_conversation(
        self,
        *,
        user_id: int,
        conversation_id: int,
    ) -> dict[int, str]:
        return await self._repo.list_for_conversation(
            user_id=user_id,
            conversation_id=conversation_id,
        )

    async def list_feedbacks_for_conversation_by_public_id(
        self,
        *,
        user_id: int,
        conversation_id: int,
    ) -> dict[str, str]:
        """Returns ``{message_public_id -> feedback_type}`` for the
        conversation, joined in a single query (no N+1)."""
        return await self._repo.list_for_conversation_by_public_id(
            user_id=user_id,
            conversation_id=conversation_id,
        )

    async def _fetch_owned_message(
        self,
        *,
        user_id: int,
        message_public_id: str,
        conversation_public_id: str,
    ) -> Optional[Message]:
        result = await self._session.execute(
            select(Message).where(
                Message.public_id == message_public_id,
                Message.user_id == user_id,
                Message.deleted_at.is_(None),
            )
        )
        message = result.scalar_one_or_none()
        if message is None:
            return None

        # Cross-check conversation ownership.
        from app.models.conversation import Conversation

        result = await self._session.execute(
            select(Conversation).where(
                Conversation.id == message.conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
                Conversation.public_id == conversation_public_id,
            )
        )
        conv = result.scalar_one_or_none()
        if conv is None:
            raise ForbiddenError("无权访问该消息")
        return message


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (F018 消息点赞/点踩):
#
#   链路:
#     用户点 assistant 消息的 👍 / 👎 按钮
#       → api/v1/messages.py POST /messages/{id}/feedback
#         → MessageFeedbackService.record(message_id, user_id, feedback_type, text?)
#           → MessageFeedbackRepository.upsert(...)
#           → 已存在同 message_id+user_id → 更新;不存在 → 新增
#
#   与 message_regeneration 配合:
#     feedback == "down" + regenerable → 触发 regenerate(可选)
#
# 关键约束(供开发者速查):
#   - 1 user × 1 message 只能有 1 条 feedback (DB 唯一约束);
#   - user_id 必须等于 message.user_id(否则 403);
#   - text 字段可选(thumbs_down 时用户可填具体原因);
#   - 不发任何 SSE 通知(纯内部记录)。
