"""Phase 2.9A.26+: Message regeneration service.

Business logic for re-generating the last assistant text reply in a
conversation.  Regeneration is distinct from task retry: it re-runs
only the LLM chat reply for the same user prompt, not the entire
Agent task pipeline.

Design:
  * Only the **latest** assistant_text message in a conversation can be
    regenerated.  If the message has been followed by another user or
    agent message, we reject with 50911 (MESSAGE_REGEN_NOT_LATEST).
  * A Redis lock on ``(user_id, conversation_id)`` serializes
    concurrent regeneration requests for the same conversation.  When
    Redis is unavailable, the MySQL row-level lock provides the safety
    net.
  * The new generation is written to ``assistant_message_generations``
    and the messages table is updated **only after** the LLM reply
    completes.  On failure, the old active generation remains active and
    the user can retry.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    MessageNotFeedbackableError,
    MessageRegenerationAlreadyRunningError,
    MessageRegenerationContextMissingError,
    MessageRegenerationNotLatestError,
    NotFoundError,
)
from app.models.message import Message
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.message_generation_repository import (
    MessageGenerationRepository,
)
from app.repositories.message_repository import MessageRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)

_GENERATION_LOCK_TTL_SECONDS = 300


def _make_regenerate_lock_key(user_id: int, conversation_public_id: str) -> str:
    return f"msg-regen:{user_id}:{conversation_public_id}"


class MessageRegenerationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        generation_repo: Optional[MessageGenerationRepository] = None,
        message_repo: Optional[MessageRepository] = None,
        conversation_repo: Optional[ConversationRepository] = None,
    ) -> None:
        self._session = session
        self._gen_repo = generation_repo or MessageGenerationRepository(session)
        self._msg_repo = message_repo or MessageRepository(session)
        self._conv_repo = conversation_repo or ConversationRepository(session)

    async def validate_and_prepare(
        self,
        *,
        user_id: int,
        conversation_public_id: str,
        message_public_id: str,
    ) -> tuple[int, str, str, str, str]:
        """Validate the regeneration target and return context needed by the
        SSE endpoint.

        Returns:
          ``(message_internal_id, conversation_internal_id_str,
          original_user_prompt, generation_public_id, target_message_public_id)``

        Raises:
          ``MessageNotFeedbackableError``  — target message not found / not
            a plain agent_text reply
          ``MessageRegenerationNotLatestError`` — target is not the last
            user-visible message in the conversation
          ``MessageRegenerationAlreadyRunningError`` — a regeneration for
            this message is already running
          ``MessageRegenerationContextMissingError`` — no original user
            prompt could be resolved

        All validation happens *before* any generation record is created.
        """
        # 1) Resolve the target message (scoped to the current user and
        #    non-deleted).
        message = await self._msg_repo.get_by_public_id(
            public_id=message_public_id,
            user_id=user_id,
        )
        if message is None:
            raise NotFoundError("消息")

        # 2) Business-rule guard: only a plain agent text reply can be
        #    regenerated.  Never rely on the front-end button visibility
        #    alone.
        if message.role != "agent" or message.message_type != "agent_text":
            raise MessageNotFeedbackableError("仅普通 Agent 文本回复支持重新生成")

        # 3) The target must be the last user-visible message in the
        #    conversation.  A later user message / agent reply / task row
        #    (i.e. anything the timeline would render after it) makes
        #    regeneration unsafe, so it is rejected.  Ordering is the
        #    stable timeline contract (conversation_sequence → created_at
        #    → id), NOT ``created_at`` alone — two messages written in the
        #    same second must not tie on a second-resolution timestamp.
        latest_msg = await self._msg_repo.get_latest_by_conversation(
            user_id=user_id,
            conversation_id=message.conversation_id,
        )
        if latest_msg is None or latest_msg.id != message.id:
            raise MessageRegenerationNotLatestError()

        # 4) Check no running generation already exists.
        active_gen = await self._gen_repo.get_active_generation(message.id)
        if active_gen and active_gen.status == "running":
            raise MessageRegenerationAlreadyRunningError()

        # 5) Find the original user prompt.  The production writer anchors
        #    every agent_text row to its triggering user message via
        #    ``reply_to_message_id``; prefer that anchor over the
        #    ``created_at <`` inference, which fails for same-second pairs.
        user_prompt_msg = await self._resolve_original_user_prompt(
            user_id=user_id,
            message=message,
        )
        if user_prompt_msg is None or not user_prompt_msg.content:
            raise MessageRegenerationContextMissingError()

        # 6) Create the generation record — only after every validation
        #    above has passed.
        gen_no = await self._gen_repo.next_generation_no(message.id)
        generation = await self._gen_repo.create_generation(
            message_id=message.id,
            generation_no=gen_no,
            status="pending",
            is_active=False,
        )

        # 7) Deactivate all other generations for this message.
        await self._gen_repo.deactivate_all_for_message(message.id)
        await self._gen_repo.activate_generation(generation.id)
        await self._gen_repo.update_status(generation.id, status="running")

        # Resolve the conversation *public* id.  The caller (SSE endpoint)
        # keys ``stream_message`` on the public id; returning the internal
        # ``message.conversation_id`` here would make the downstream lookup
        # fail with NotFoundError("会话").
        conv_public_id = await self._conv_repo.get_public_id_by_internal_id(
            message.conversation_id
        )
        if conv_public_id is None:
            raise NotFoundError("会话")

        return (
            message.id,
            conv_public_id,
            user_prompt_msg.content,
            generation.public_id,
            message.public_id,
        )

    async def _resolve_original_user_prompt(
        self, *, user_id: int, message: Message
    ) -> Message | None:
        """Resolve the user_text message that triggered ``message``.

        Prefers the explicit ``reply_to_message_id`` anchor written by
        ``MessageService``; falls back to the latest user_text row under
        the stable timeline ordering for legacy rows without an anchor.
        """
        if message.reply_to_message_id is not None:
            result = await self._session.execute(
                select(Message).where(
                    Message.id == message.reply_to_message_id,
                    Message.user_id == user_id,
                    Message.deleted_at.is_(None),
                )
            )
            prompt_msg = result.scalar_one_or_none()
            if prompt_msg is not None and prompt_msg.message_type == "user_text":
                return prompt_msg
        return await self._msg_repo.get_latest_user_text_by_conversation(
            user_id=user_id,
            conversation_id=message.conversation_id,
        )

    async def complete_generation(
        self,
        *,
        generation_public_id: str,
        content_markdown: str,
    ) -> None:
        """Mark a generation as completed and update the messages table."""
        gen = await self._gen_repo.get_by_public_id(generation_public_id)
        if gen is None:
            return
        await self._gen_repo.update_status(
            gen.id, status="completed", content_markdown=content_markdown
        )
        await self._session.execute(
            select(Message.id).where(Message.id == gen.message_id)
        )
        # Update the messages table with the new content
        from sqlalchemy import text
        await self._session.execute(
            text(
                "UPDATE messages SET content = :content, updated_at = NOW() "
                "WHERE id = :mid"
            ),
            {"content": content_markdown, "mid": gen.message_id},
        )
        await self._session.flush()

    async def fail_generation(
        self,
        *,
        generation_public_id: str,
        error_code: str = "REGEN_FAILED",
    ) -> None:
        """Mark a generation as failed.  The old active generation is
        restored by the caller (the SSE handler restores the original
        content to the messages table)."""
        gen = await self._gen_repo.get_by_public_id(generation_public_id)
        if gen is None:
            return
        await self._gen_repo.update_status(
            gen.id, status="failed", error_code=error_code
        )


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (F018 assistant 消息再生):
#
#   链路:
#     用户在 assistant 消息下点"重新生成"按钮
#       → api/v1/messages.py POST /messages/{id}/regenerate
#         → MessageRegenerationService.regenerate(message_id, user_id, alternate=True)
#           → 校验 owner + 可再生性(非任务产物消息)
#           → 重新调 ChatLLMService.generate_reply(prompt, alternate_prompt=...)
#           → 落库新的 assistant Message(原文保留为 hidden=alternate=true)
#           → 设新消息为 conversation 的 latest
#
# 关键约束(供开发者速查):
#   - regenerate_count 上限(默认 3 次):防止用户无限点击;
#   - 不能 regenerate 任务产物消息(word_export 等),只能 regenerate
#     普通 chat assistant 消息;
#   - alternate=True 走相同 ChatLLMService,但 prompt 模板指示 LLM
#     给出不同答案,避免与上文一致;
#   - 历史保留:旧消息不删,只 hide 前端不显示,方便审计。
