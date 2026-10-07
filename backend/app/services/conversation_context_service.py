"""Conversation context service — builds structured context for LLM calls.

F016: unified context construction layer.  Builds ChatContext,
IntentContext, and TaskTriggerContext by querying recent messages,
summaries, files, and task state.  All methods fail-safe: internal
exceptions are logged and a minimal empty context is returned so that
the caller can degrade to single-turn chat without crashing.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.context_reducer import ContextReducer
from app.models.agent_event import AgentEvent
from app.models.artifact import Artifact
from app.models.human_confirmation import HumanConfirmation
from app.repositories.agent_task_repository import AgentTaskRepository
from app.repositories.conversation_summary_repository import (
    ConversationSummaryRepository,
)
from app.repositories.context_snapshot_repository import ContextSnapshotRepository
from app.repositories.event_repository import EventRepository
from app.repositories.file_repository import FileRepository
from app.repositories.message_repository import MessageRepository
from app.schemas.context import (
    ChatContext,
    FileContextSummary,
    IntentContext,
    TaskContextSummary,
    TaskTriggerContext,
)
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)


class ConversationContextService:
    """Builds structured context objects for chat, intent, and task triggering."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._msg_repo = MessageRepository(session)
        self._file_repo = FileRepository(session)
        self._task_repo = AgentTaskRepository(session)
        self._summary_repo = ConversationSummaryRepository(session)
        self._snap_repo = ContextSnapshotRepository(session)
        self._event_repo = EventRepository(session)
        self._reducer = ContextReducer()
        # Phase 0 — request-scoped dedupe cache for cross-builder context
        # sharing within a single ``send_message`` invocation.  Populated
        # by ``_load_context`` and consumed by ``build_chat_context`` /
        # ``build_intent_context`` so the same query (e.g. summary, files,
        # task summary, recent messages) is not re-issued twice in one
        # request lifecycle.  Reset on every new ``send_message`` call by
        # instantiating a fresh ``ConversationContextService`` per request.
        self._shared: dict[str, Any] = {}

    # ── Chat context ─────────────────────────────────────────────

    async def build_chat_context(
        self,
        *,
        user_id: int,
        conversation_id: int,
        current_message_id: int | None = None,
        max_turns: int = 10,
        project_context: dict[str, Any] | None = None,
    ) -> ChatContext:
        """Build a bounded ChatContext for a chat-reply call.

        Phase 0: shares a per-request memoized context (messages,
        summary, files, task_summary) with ``build_intent_context`` so
        the same Conversation is not queried twice within a single
        ``send_message`` lifecycle.  Falls back to independent queries
        if ``_load_context`` was not called.
        """
        try:
            await self._load_context(
                user_id=user_id,
                conversation_id=conversation_id,
                current_message_id=current_message_id,
                intent_max_turns=3,  # upper-bound intent needs are smaller
                chat_max_turns=max_turns,
            )

            messages = self._shared["chat_messages"]
            summary_text = self._shared["summary_text"]
            file_sums = self._shared["file_sums"]
            task_sum = self._shared["task_sum"]

            ctx = self._reducer.reduce_chat_context(
                messages,
                summary_text,
                file_sums,
                task_sum,
                conversation_id=str(conversation_id),
                current_message_id=str(current_message_id) if current_message_id else None,
                exclude_id=current_message_id,
            )
            if project_context and project_context.get("project_id"):
                ctx = ctx.model_copy(update={"project_context": dict(project_context)})
            logger.info(
                "ConversationContextService.build_chat_context: 完成 | conv=%d | msgs=%d | tokens=%d",
                conversation_id, len(ctx.recent_messages), ctx.estimated_tokens,
            )
            return ctx
        except Exception as exc:
            logger.warning(
                "ConversationContextService.build_chat_context: 异常降级 | conv=%d | error=%s",
                conversation_id, exc,
            )
            return ChatContext(
                conversation_id=str(conversation_id),
                current_message_id=str(current_message_id) if current_message_id else None,
                project_context=(
                    dict(project_context)
                    if project_context and project_context.get("project_id")
                    else None
                ),
            )

    # ── Intent context ───────────────────────────────────────────

    async def build_intent_context(
        self,
        *,
        user_id: int,
        conversation_id: int,
        current_message_id: int | None = None,
        attached_file_ids: list[str] | None = None,
        max_turns: int = 3,
    ) -> IntentContext:
        """Build a bounded IntentContext for intent recognition.

        Phase 0: shares a per-request memoized context with
        ``build_chat_context``.  Re-issues queries only if
        ``_load_context`` was not previously called in the same lifecycle.
        """
        try:
            await self._load_context(
                user_id=user_id,
                conversation_id=conversation_id,
                current_message_id=current_message_id,
                intent_max_turns=max_turns,
                chat_max_turns=10,
            )

            messages = self._shared["intent_messages"]
            summary_text = self._shared["summary_text"]
            file_sums = self._shared["file_sums"]
            task_sum = self._shared["task_sum"]

            ctx = self._reducer.reduce_intent_context(
                messages,
                summary_text,
                file_sums,
                task_sum,
                conversation_id=str(conversation_id),
                current_message_id=str(current_message_id) if current_message_id else None,
                attached_file_ids=attached_file_ids or [],
                exclude_id=current_message_id,
            )
            logger.info(
                "ConversationContextService.build_intent_context: 完成 | conv=%d | turns=%d | tokens=%d",
                conversation_id, len(ctx.recent_turns), ctx.estimated_tokens,
            )
            return ctx
        except Exception as exc:
            logger.warning(
                "ConversationContextService.build_intent_context: 异常降级 | conv=%d | error=%s",
                conversation_id, exc,
            )
            return IntentContext(
                conversation_id=str(conversation_id),
                current_message_id=str(current_message_id) if current_message_id else None,
                attached_file_ids=list(attached_file_ids or []),
            )

    # ── Task trigger context ─────────────────────────────────────

    async def build_task_trigger_context(
        self,
        *,
        user_id: int,
        conversation_id: int,
        trigger_message_id: int,
        user_goal: str,
        selected_file_ids: list[str],
        intent: str,
        route: str,
    ) -> TaskTriggerContext:
        """Build a TaskTriggerContext to record in AgentTask.input_payload."""
        try:
            summary_text = await self._get_latest_summary(conversation_id)
            tasks = await self._task_repo.list_by_conversation(user_id, conversation_id)
            latest_task_id = tasks[0].public_id if tasks else None

            return TaskTriggerContext(
                conversation_id=str(conversation_id),
                trigger_message_id=str(trigger_message_id),
                user_goal=user_goal,
                recent_conversation_summary=summary_text,
                selected_file_ids=selected_file_ids,
                latest_task_id=latest_task_id,
                intent=intent,
                route=route,
            )
        except Exception as exc:
            logger.warning(
                "ConversationContextService.build_task_trigger_context: 异常降级 | conv=%d | error=%s",
                conversation_id, exc,
            )
            return TaskTriggerContext(
                conversation_id=str(conversation_id),
                trigger_message_id=str(trigger_message_id),
                user_goal=user_goal,
                selected_file_ids=selected_file_ids,
                intent=intent,
                route=route,
            )

    # ── File summaries ───────────────────────────────────────────

    async def build_file_summaries(
        self, *, user_id: int, conversation_id: int
    ) -> list[FileContextSummary]:
        """Build lightweight file metadata summaries (no storage_path)."""
        try:
            files = await self._file_repo.list_by_conversation(
                user_id, conversation_id
            )
            # Most recent 10
            files_sorted = sorted(
                files,
                key=lambda f: getattr(f, "created_at", ""),
                reverse=True,
            )[:10]

            return [
                FileContextSummary(
                    file_id=f.public_id,
                    file_name=f.original_name,
                    file_type=f.file_type,
                    upload_status=f.upload_status,
                )
                for f in files_sorted
            ]
        except Exception as exc:
            logger.warning(
                "ConversationContextService.build_file_summaries: 异常 | conv=%d | error=%s",
                conversation_id, exc,
            )
            return []

    # ── Latest task summary ──────────────────────────────────────

    async def build_latest_task_summary(
        self, *, user_id: int, conversation_id: int
    ) -> TaskContextSummary | None:
        """Build a summary of the most recent AgentTask."""
        try:
            tasks = await self._task_repo.list_by_conversation(
                user_id, conversation_id
            )
            if not tasks:
                return None

            task = tasks[0]  # most recent (DESC order)

            # Count pending confirmations
            pending_count = await self._count_pending_confirmations(task.id)

            # Count artifacts
            artifact_count = await self._count_artifacts(task.id)
            latest_artifact = await self._get_latest_artifact(task.id)

            # Get latest event type
            latest_event_type = await self._get_latest_event_type(task.id)

            # Get failed tool name if task failed
            failed_tool_name = None
            if task.status == "failed":
                failed_tool_name = await self._get_failed_tool_name(task.id)

            # Generate summary text
            summary_text = self._format_task_summary_text(
                task.status, task.task_type, pending_count, artifact_count
            )

            return TaskContextSummary(
                task_id=task.public_id,
                task_type=task.task_type,
                status=task.status,
                current_node=task.current_node,
                resume_node=task.resume_node,
                latest_event_type=latest_event_type,
                pending_confirmation_count=pending_count,
                artifact_count=artifact_count,
                latest_artifact_public_id=getattr(latest_artifact, "public_id", None),
                latest_artifact_file_name=getattr(latest_artifact, "file_name", None),
                latest_artifact_version_no=getattr(latest_artifact, "version_no", None),
                failed_tool_name=failed_tool_name,
                summary_text=summary_text,
            )
        except Exception as exc:
            logger.warning(
                "ConversationContextService.build_latest_task_summary: 异常 | conv=%d | error=%s",
                conversation_id, exc,
            )
            return None

    # ── Private helpers ──────────────────────────────────────────

    async def _load_context(
        self,
        *,
        user_id: int,
        conversation_id: int,
        current_message_id: int | None,
        intent_max_turns: int,
        chat_max_turns: int,
    ) -> None:
        """Load shared cross-builder context once per request lifecycle.

        Populates ``self._shared`` with:

        - ``intent_messages``: list_recent_by_conversation for intent (limit=intent*2)
        - ``chat_messages``:   list_recent_by_conversation for chat (limit=chat*2)
        - ``summary_text``:    latest active summary text (single row)
        - ``file_sums``:       file summaries (single SQL via build_file_summaries)
        - ``task_sum``:        latest task summary (multiple SQL via build_latest_task_summary)

        Subsequent calls within the same service instance are a no-op
        so back-to-back ``build_intent_context`` +
        ``build_chat_context`` calls in a single ``send_message`` request
        only query the database once per resource.
        """
        if self._shared.get("loaded_for") == (user_id, conversation_id, current_message_id):
            return
        self._shared.clear()

        # Messages are queried at the higher of the two turn limits so
        # both consumers (intent + chat) can slice locally without
        # re-querying.
        limit = max(intent_max_turns, chat_max_turns) * 2

        intent_messages = await self._msg_repo.list_recent_by_conversation(
            user_id, conversation_id,
            limit=limit,
            exclude_id=current_message_id,
        )
        # Chat and intent share the same row set; ``chat_messages`` is
        # the same chronological list as ``intent_messages`` because the
        # message_repository always returns chronological order
        # (see ``list_recent_by_conversation`` post-`.reverse()`).
        chat_messages = intent_messages

        summary_text = await self._get_latest_summary(conversation_id)
        file_sums = await self.build_file_summaries(
            user_id=user_id, conversation_id=conversation_id
        )
        task_sum = await self.build_latest_task_summary(
            user_id=user_id, conversation_id=conversation_id
        )

        self._shared.update(
            {
                "loaded_for": (user_id, conversation_id, current_message_id),
                "intent_messages": intent_messages,
                "chat_messages": chat_messages,
                "summary_text": summary_text,
                "file_sums": file_sums,
                "task_sum": task_sum,
            }
        )

    async def _get_latest_summary(self, conversation_id: int) -> str | None:
        """Get the text of the latest active conversation summary."""
        summary = await self._summary_repo.get_latest_active(conversation_id)
        if summary:
            return summary.summary_text
        return None

    async def _count_pending_confirmations(self, task_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(HumanConfirmation)
            .where(
                HumanConfirmation.task_id == task_id,
                HumanConfirmation.status == "pending",
            )
        )
        return result.scalar() or 0

    async def _count_artifacts(self, task_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(Artifact)
            .where(
                Artifact.task_id == task_id,
                Artifact.deleted_at.is_(None),
            )
        )
        return result.scalar() or 0

    async def _get_latest_artifact(self, task_id: int) -> Artifact | None:
        result = await self._session.execute(
            select(Artifact)
            .where(
                Artifact.task_id == task_id,
                Artifact.status == "available",
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_latest_event_type(self, task_id: int) -> str | None:
        result = await self._session.execute(
            select(AgentEvent.event_type)
            .where(AgentEvent.task_id == task_id)
            .order_by(AgentEvent.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row if row else None

    async def _get_failed_tool_name(self, task_id: int) -> str | None:
        from app.models.tool_call import ToolCall

        result = await self._session.execute(
            select(ToolCall.tool_name)
            .where(
                ToolCall.task_id == task_id,
                ToolCall.status == "failed",
            )
            .order_by(ToolCall.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    def _format_task_summary_text(
        status: str,
        task_type: str | None,
        pending_count: int,
        artifact_count: int,
    ) -> str:
        parts = [f"任务类型：{task_type or 'test_plan_generation'}"]
        parts.append(f"状态：{status}")
        if pending_count > 0:
            parts.append(f"待确认：{pending_count} 项")
        if artifact_count > 0:
            parts.append(f"已生成产物：{artifact_count} 个")
        return "；".join(parts)


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (F016 统一 Context Builder):
#
#   链路:
#     任意 ContextEngine.assemble 调用 → 内部调此服务构造 3 种 context:
#       - ChatContext: 普通聊天 LLM 输入(sections: summary / recent / files / task_state)
#       - IntentContext: intent_router 用(轻量,无 summary)
#       - TaskTriggerContext: 用户消息触发测试任务时附加的任务上下文
#
#   与 ChatLLMService.generate_reply 配合:
#     generate_reply(prompt, chat_context=self.build_chat_context(conversation, user))
#
# 关键约束(供开发者速查):
#   - 三个 build_* 方法都是 fail-safe:任何内部失败返回空 context 而不是抛;
#   - summary 字段读 conversation_summaries is_current=True 行;
#   - files 字段由 task_attachment_resolver 解析后的 binding list 派生;
#   - 本服务只 assemble data,不做 prompt 拼接(prompt 由 ChatLLMService 拼)。
