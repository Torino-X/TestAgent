"""Incremental, best-effort summaries for long conversations.

The current active summary is a durable memory layer: it must carry decisions
that have aged out of the small raw-message context window.  Summary generation
therefore uses the configured provider directly with the prior summary plus the
uncovered message delta.  Sending the transcript through normal context
composition would reapply the raw-message window and can silently drop exactly
the facts that this service is responsible for preserving.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.llm_client import LLMClient
from app.llm.task_profiles import SUMMARY_PROFILE
from app.models.conversation_summary import ConversationSummary
from app.repositories.conversation_summary_repository import ConversationSummaryRepository
from app.repositories.message_repository import MessageRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)

_MIN_MESSAGES_FOR_SUMMARY = 6
_STALENESS_THRESHOLD = 8
_SUMMARY_BATCH_LIMIT = 50


class ConversationSummaryService:
    """Maintain one active, cursor-based summary per conversation."""

    def __init__(
        self,
        session: AsyncSession,
        llm_client: LLMClient | None = None,
        *,
        context_llm_invoker=None,
        task_flag_resolver=None,
        session_factory=None,
    ) -> None:
        self._session = session
        self._summary_repo = ConversationSummaryRepository(session)
        self._msg_repo = MessageRepository(session)
        self._llm = llm_client
        # Kept as injection-compatible constructor parameters.  Summaries do
        # not use the normal invoker because it intentionally limits raw
        # conversation sources and cannot receive the full summary delta.
        self._context_llm_invoker = context_llm_invoker
        self._task_flag_resolver = task_flag_resolver
        self._session_factory = session_factory

    async def get_latest_summary(self, conversation_id: int) -> str | None:
        """Return the latest active summary text, if any."""
        try:
            summary = await self._summary_repo.get_latest_active(conversation_id)
            return summary.summary_text if summary else None
        except Exception as exc:  # noqa: BLE001 - summary reads are non-blocking
            logger.warning(
                "ConversationSummaryService.get_latest_summary failed | conv=%d | error=%s",
                conversation_id,
                exc,
            )
            return None

    async def should_update_summary(self, conversation_id: int) -> bool:
        """Return whether the active summary lags the persisted timeline."""
        try:
            total = await self._msg_repo.count_by_conversation(conversation_id)
            if total < _MIN_MESSAGES_FOR_SUMMARY:
                return False
            latest = await self._summary_repo.get_latest_active(conversation_id)
            return latest is None or latest.message_count < total - _STALENESS_THRESHOLD
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ConversationSummaryService.should_update_summary failed | conv=%d | error=%s",
                conversation_id,
                exc,
            )
            return False

    async def maybe_update_summary(self, conversation_id: int, user_id: int) -> None:
        """Advance the summary cursor without blocking the chat request."""
        try:
            if not await self.should_update_summary(conversation_id):
                return

            active_summary = await self._summary_repo.get_latest_active_by_type(
                conversation_id, user_id, "conversation"
            )
            cursor = getattr(active_summary, "covered_message_end_id", None)
            if isinstance(cursor, int) and cursor > 0:
                messages = await self._msg_repo.list_after_id(
                    user_id,
                    conversation_id,
                    cursor,
                    limit=_SUMMARY_BATCH_LIMIT,
                )
            else:
                messages = await self._msg_repo.list_by_conversation(
                    user_id,
                    conversation_id,
                    limit=_SUMMARY_BATCH_LIMIT,
                )
            if not messages:
                return

            delta_text = self._format_for_summary(messages)
            if not delta_text.strip():
                return
            conversation_text = self._summary_input(
                getattr(active_summary, "summary_text", None), delta_text
            )
            summary_text = await self._generate_summary(
                conversation_text, user_id, conversation_id
            )
            if not summary_text:
                return

            now = utcnow()
            latest_message = messages[-1]
            prior_count = int(getattr(active_summary, "message_count", 0) or 0)
            start_id = getattr(active_summary, "covered_message_start_id", None)
            summary = ConversationSummary(
                public_id=generate_public_id("summary"),
                user_id=user_id,
                conversation_id=conversation_id,
                summary_text=summary_text,
                covered_message_start_id=start_id or messages[0].id,
                covered_message_end_id=latest_message.id,
                message_count=prior_count + len(messages),
                estimated_tokens=0,
                summary_version=(getattr(active_summary, "summary_version", 0) or 0) + 1,
                status="active",
                summary_type="conversation",
                created_at=now,
                updated_at=now,
            )
            created = await self._summary_repo.create(summary)
            if created.id is not None:
                await self._summary_repo.supersede_active_by_type(
                    conversation_id,
                    user_id,
                    "conversation",
                    created.id,
                )
            logger.info(
                "ConversationSummaryService.maybe_update_summary saved | conv=%d | through_message=%s | count=%d",
                conversation_id,
                latest_message.id,
                summary.message_count,
            )
        except Exception as exc:  # noqa: BLE001 - never fail the user turn
            logger.warning(
                "ConversationSummaryService.maybe_update_summary failed | conv=%d | error=%s",
                conversation_id,
                exc,
            )

    async def _generate_summary(
        self, conversation_text: str, user_id: int, conversation_id: int | None = None
    ) -> str | None:
        """Generate a summary from the exact incremental input payload."""
        bridge = self._context_llm_invoker
        if bridge is None or not getattr(bridge, "available", False):
            logger.warning(
                "ConversationSummaryService._generate_summary skipped | conv=%s | reason=context_bridge_unavailable",
                conversation_id,
            )
            return None
        try:
            from types import SimpleNamespace

            runtime_context = SimpleNamespace(
                session_factory=self._session_factory,
                llm_client=self._llm,
                context_llm_invoker=bridge,
                user_internal_id=int(user_id),
                conversation_internal_id=conversation_id,
            )
            result = await bridge.generate(
                user_id=user_id,
                conversation_id=conversation_id,
                call_site="compression.conversation",
                llm_task_profile=SUMMARY_PROFILE,
                current_goal=conversation_text,
                user_content=conversation_text,
                output_contract="text",
                runtime_context=runtime_context,
            )
            if result is not None and isinstance(getattr(result, "value", None), str):
                summary = result.value.strip()
                if summary:
                    return summary
            logger.warning(
                "ConversationSummaryService._generate_summary empty result | conv=%s | success=%s",
                conversation_id,
                getattr(result, "success", False),
            )
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ConversationSummaryService._generate_summary failed | conv=%s | error=%s",
                conversation_id,
                exc,
            )
            return None

    @staticmethod
    def _summary_input(prior_summary: str | None, delta_text: str) -> str:
        if not prior_summary:
            return (
                "Create a durable summary. Explicitly retain named people and owners, "
                "final decisions, scope inclusions and exclusions, numbers and deadlines, "
                "and unresolved items from these conversation turns:\n"
                f"{delta_text}"
            )
        return (
            "Existing durable conversation summary. Preserve every still-valid "
            "specific fact, final decision, inclusion/exclusion, owner, deadline, "
            "and unresolved item unless the new turns explicitly supersede it:\n"
            f"{prior_summary.strip()}\n\n"
            "New uncovered conversation turns to merge into that summary:\n"
            f"{delta_text}"
        )

    @staticmethod
    def _format_for_summary(messages: list[Any]) -> str:
        """Format persisted user/assistant text as a compact transcript."""
        lines: list[str] = []
        for message in messages:
            role = getattr(message, "role", "")
            content = (getattr(message, "content", "") or "").strip()
            message_type = getattr(message, "message_type", "")
            if not content:
                continue
            if role == "user" and message_type == "user_text":
                lines.append(f"User: {content}")
            elif role == "agent" and message_type == "agent_text":
                lines.append(f"Assistant: {content}")
        return "\n".join(lines)
