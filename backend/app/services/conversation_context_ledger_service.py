"""Canonical conversation-level context ledger.

This module deliberately separates a conversation's logical working set from
the context snapshot of any particular LLM call.  A test-plan flow can make
many narrow calls; none of those calls is allowed to make the main conversation
usage card suddenly look empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.token_estimator import estimate_tokens
from app.context_engine.conversation_retention import (
    recent_turn_tail,
    recent_turns_for_summary_schema,
)
from app.context_engine.sources.memory import select_authoritative_memory_records
from app.models.agent_task import AgentTask
from app.models.conversation import Conversation
from app.models.context_engine import ConversationContextLedger
from app.repositories.base import ensure_model_id
from app.repositories.conversation_summary_repository import ConversationSummaryRepository
from app.repositories.message_repository import MessageRepository
from app.services.project_context_resolver import ProjectContextResolver
from app.context_engine.system_instruction_catalog import (
    render_system_instructions,
    system_instruction_manifest,
)
from app.utils.ids import generate_public_id


LEDGER_POLICY_VERSION = "conversation_working_set_v1"
RECENT_TURNS_AFTER_COMPACTION = 20
_BASE_CALL_CONTRACT = "输出必须符合调用合同：结构化、可解析、不包含敏感信息。"


@dataclass(frozen=True)
class ConversationLedgerMaterialization:
    public_id: str
    breakdown: dict[str, int]
    total_tokens: int
    manifest: dict[str, Any]
    materialized_at: datetime


class ConversationContextLedgerService:
    """Materialize one stable working-set ledger for an owned conversation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def materialize(
        self,
        *,
        conversation,
        user_id: int,
        context_window_tokens: int | None,
    ) -> ConversationLedgerMaterialization:
        conversation_id = int(conversation.id)
        messages = await MessageRepository(self._session).list_context_messages_by_conversation(
            user_id=user_id, conversation_id=conversation_id
        )
        summary = await ConversationSummaryRepository(self._session).get_latest_active_by_type(
            conversation_id, user_id, "conversation"
        )
        conversation_texts, conversation_refs = self._conversation_working_set(messages, summary)

        latest_user_text = next(
            (
                str(message.content or "")
                for message in reversed(messages)
                if getattr(message, "role", "") == "user" and getattr(message, "message_type", "") == "user_text"
            ),
            "",
        )
        latest_task = await self._latest_task(conversation_id, user_id)
        project_package = await self._project_package(
            conversation=conversation,
            user_id=user_id,
            query=latest_user_text,
            task_id=getattr(latest_task, "public_id", None),
        )
        project_texts, project_refs = self._project_document_working_set(project_package)
        memory_texts, memory_refs = await self._memory_working_set(
            user_id=user_id,
            project_package=project_package,
            query=latest_user_text,
            task=latest_task,
        )
        system_texts, system_refs = self._system_working_set(project_package)
        task_texts, task_refs = self._task_working_set(latest_task)

        breakdown = {
            "conversation_history": self._count(conversation_texts),
            "project_documents": self._count(project_texts),
            "task_context": self._count(task_texts),
            "user_memory": self._count(memory_texts),
            "system_instructions": self._count(system_texts),
        }
        total_tokens = sum(breakdown.values())
        manifest = {
            "policy_version": LEDGER_POLICY_VERSION,
            "conversation": conversation_refs,
            "project_documents": project_refs,
            "task_context": task_refs,
            "user_memory": memory_refs,
            "system_instructions": system_refs,
            "note": "references only; canonical content remains in its authoritative source",
        }
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # A context-usage refresh can be requested concurrently (for example,
        # by the conversation view and the input dock).  Only the *first*
        # ledger creation needs the parent-conversation lock.  Taking that
        # lock for every materialization races with the message-write request
        # that is simultaneously building an active chat context; the chat
        # bridge then fail-opens without the ledger and the 60% retention
        # waterline is never reached.  Existing ledger rows have their own
        # one-row lock, which is sufficient for a refresh/update.
        ledger = await self._load_or_lock_for_creation(conversation_id, user_id)
        created_ledger = ledger is None
        if ledger is None:
            ledger = ConversationContextLedger(
                public_id=generate_public_id("ctxledger"),
                user_id=user_id,
                conversation_id=conversation_id,
            )

        try:
            # A savepoint keeps a rare duplicate-key conflict from poisoning
            # the request session.  This is still needed as a final guard for
            # deployments where another process bypasses the row-lock path.
            async with self._session.begin_nested():
                if created_ledger:
                    await ensure_model_id(self._session, ConversationContextLedger, ledger)
                    self._session.add(ledger)
                self._apply_materialization(
                    ledger=ledger,
                    context_window_tokens=context_window_tokens,
                    breakdown=breakdown,
                    total_tokens=total_tokens,
                    manifest=manifest,
                    materialized_at=now,
                )
                await self._session.flush()
        except IntegrityError as exc:
            if not created_ledger or not self._is_ledger_duplicate_error(exc):
                raise
            # The competing writer committed the ledger while this request was
            # waiting on the unique index.  Reload its canonical row and apply
            # this request's fresh accounting rather than failing the card.
            if ledger in self._session:
                self._session.expunge(ledger)
            ledger = await self._load_for_update(conversation_id, user_id)
            if ledger is None:
                raise
            self._apply_materialization(
                ledger=ledger,
                context_window_tokens=context_window_tokens,
                breakdown=breakdown,
                total_tokens=total_tokens,
                manifest=manifest,
                materialized_at=now,
            )
            await self._session.flush()
        return ConversationLedgerMaterialization(
            public_id=ledger.public_id,
            breakdown=breakdown,
            total_tokens=total_tokens,
            manifest=manifest,
            materialized_at=now,
        )

    async def _load(self, conversation_id: int, user_id: int) -> ConversationContextLedger | None:
        result = await self._session.execute(
            select(ConversationContextLedger).where(
                ConversationContextLedger.conversation_id == conversation_id,
                ConversationContextLedger.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    def _supports_row_lock(self) -> bool:
        bind = self._session.get_bind()
        return getattr(getattr(bind, "dialect", None), "name", "") in {"mysql", "postgresql"}

    async def _lock_conversation_for_ledger(self, conversation_id: int) -> None:
        """Serialize first-ledger creation where the database supports row locks."""
        if not self._supports_row_lock():
            return
        await self._session.execute(
            select(Conversation.id)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )

    async def _load_for_update(
        self, conversation_id: int, user_id: int
    ) -> ConversationContextLedger | None:
        if not self._supports_row_lock():
            return await self._load(conversation_id, user_id)
        result = await self._session.execute(
            select(ConversationContextLedger)
            .where(
                ConversationContextLedger.conversation_id == conversation_id,
                ConversationContextLedger.user_id == user_id,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def _load_or_lock_for_creation(
        self, conversation_id: int, user_id: int
    ) -> ConversationContextLedger | None:
        """Load an existing ledger without locking its parent conversation.

        If it is absent, serialize just the creation path on the parent row
        and re-read under the ledger lock.  The second read covers a competing
        creator that committed while this request waited for the parent lock.
        """
        ledger = await self._load_for_update(conversation_id, user_id)
        if ledger is not None:
            return ledger
        await self._lock_conversation_for_ledger(conversation_id)
        return await self._load_for_update(conversation_id, user_id)

    @staticmethod
    def _is_ledger_duplicate_error(exc: IntegrityError) -> bool:
        message = str(exc.orig or exc)
        return (
            "uq_conversation_context_ledgers_conversation" in message
            or "conversation_context_ledgers.conversation_id" in message
            or "Duplicate entry" in message
        )

    @staticmethod
    def _apply_materialization(
        *,
        ledger: ConversationContextLedger,
        context_window_tokens: int | None,
        breakdown: dict[str, int],
        total_tokens: int,
        manifest: dict[str, Any],
        materialized_at: datetime,
    ) -> None:
        ledger.policy_version = LEDGER_POLICY_VERSION
        ledger.context_window_tokens = context_window_tokens
        ledger.conversation_history_tokens = breakdown["conversation_history"]
        ledger.project_documents_tokens = breakdown["project_documents"]
        ledger.task_context_tokens = breakdown["task_context"]
        ledger.user_memory_tokens = breakdown["user_memory"]
        ledger.system_instructions_tokens = breakdown["system_instructions"]
        ledger.total_tokens = total_tokens
        ledger.manifest_json = manifest
        ledger.materialized_at = materialized_at

    async def _latest_task(self, conversation_id: int, user_id: int) -> AgentTask | None:
        result = await self._session.execute(
            select(AgentTask)
            .where(
                AgentTask.conversation_id == conversation_id,
                AgentTask.user_id == user_id,
                AgentTask.deleted_at.is_(None),
            )
            .order_by(AgentTask.updated_at.desc(), AgentTask.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _project_package(self, *, conversation, user_id: int, query: str, task_id: str | None) -> dict[str, Any]:
        if getattr(conversation, "project_id", None) is None:
            return {}
        package = await ProjectContextResolver(self._session).resolve(
            user_id=user_id,
            conversation_id=int(conversation.id),
            query=query[:2000],
            task_id=task_id,
        )
        return dict(package or {})

    @staticmethod
    def _conversation_working_set(messages, summary) -> tuple[list[str], list[dict[str, Any]]]:
        refs: list[dict[str, Any]] = []
        texts: list[str] = []
        covered_end = getattr(summary, "covered_message_end_id", None)
        has_manifest = bool(getattr(summary, "source_refs_json", None))
        if summary is not None and getattr(summary, "summary_text", None):
            texts.append(str(summary.summary_text))
            refs.append({
                "type": "conversation_summary",
                "id": summary.public_id,
                "covered_end_message_id": covered_end,
            })
        active_messages = list(messages)
        if covered_end and has_manifest:
            tail = recent_turn_tail(
                active_messages,
                keep_turns=recent_turns_for_summary_schema(
                    getattr(summary, "schema_version", None)
                ),
            )
            tail_ids = {int(getattr(item, "id", 0) or 0) for item in tail}
            active_messages = [
                item for item in active_messages
                if int(getattr(item, "id", 0) or 0) > int(covered_end)
                or int(getattr(item, "id", 0) or 0) in tail_ids
            ]
        for message in active_messages:
            text = str(getattr(message, "content", "") or "")
            if not text:
                continue
            texts.append(text)
            refs.append({"type": "message", "id": getattr(message, "public_id", None) or str(message.id)})
        return texts, refs

    @staticmethod
    def _project_document_working_set(package: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
        texts: list[str] = []
        refs: list[dict[str, Any]] = []
        for hit in list(package.get("source_hits") or [])[:5]:
            if not isinstance(hit, dict):
                continue
            content = str(hit.get("content") or "")[:1600]
            if content:
                texts.append(content)
            refs.append({"type": "project_chunk", "id": hit.get("chunk_id"), "document_id": hit.get("document_id")})
        return texts, refs

    async def _memory_working_set(
        self,
        *,
        user_id: int,
        project_package: dict[str, Any],
        query: str,
        task: AgentTask | None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Materialize precisely the ranked memory set Context Engine uses."""
        texts: list[str] = []
        refs: list[dict[str, Any]] = []
        from app.repositories.context_engine_repositories import ContextMemoryRepository

        try:
            records = await select_authoritative_memory_records(
                ContextMemoryRepository(self._session),
                internal_uid=user_id,
                workspace_key=project_package.get("workspace_key"),
                agent_type=(
                    "test_plan"
                    if task is not None and str(task.task_type or "").endswith("test_plan_generation")
                    else None
                ),
                query=query,
            )
        except Exception:  # noqa: BLE001 - usage telemetry must degrade safely
            records = []
        for memory in records:
            content = str(getattr(memory, "content", "") or "")[:2000]
            if content:
                texts.append(content)
            refs.append({
                "type": "memory",
                "id": getattr(memory, "public_id", None),
                "scope_type": getattr(memory, "scope_type", None),
            })
        return texts, refs

    @staticmethod
    def _system_working_set(package: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
        texts = [render_system_instructions("chat.reply"), _BASE_CALL_CONTRACT]
        refs: list[dict[str, Any]] = [
            *system_instruction_manifest("chat.reply"),
            {"type": "call_contract", "id": "text"},
        ]
        for instruction in list(package.get("instructions") or [])[:20]:
            if not isinstance(instruction, dict):
                continue
            content = str(instruction.get("content") or "")[:4000]
            if content:
                texts.append(content)
            refs.append({"type": "workspace_instruction", "id": instruction.get("id")})
        return texts, refs

    @staticmethod
    def _task_working_set(task: AgentTask | None) -> tuple[list[str], list[dict[str, Any]]]:
        if task is None:
            return [], []
        parts = [
            f"任务标题: {task.title or ''}",
            f"任务状态: {task.status}; 当前节点: {task.current_node or 'unknown'}",
            f"任务类型: {task.task_type}",
        ]
        if task.user_instruction:
            parts.append(f"任务指令: {str(task.user_instruction)[:4000]}")
        context = task.task_context_json if isinstance(task.task_context_json, dict) else {}
        completion_summary = str(context.get("completion_summary") or "")[:4000]
        if completion_summary:
            parts.append(f"任务摘要: {completion_summary}")
        return ["\n".join(parts)], [{"type": "agent_task", "id": task.public_id, "status": task.status}]

    @staticmethod
    def _count(texts: list[str]) -> int:
        return sum(max(0, estimate_tokens(text)) for text in texts)


__all__ = [
    "ConversationContextLedgerService",
    "ConversationLedgerMaterialization",
    "LEDGER_POLICY_VERSION",
    "RECENT_TURNS_AFTER_COMPACTION",
]
