"""ManualConversationCompactionService — 用户主动触发 Conversation Compaction。

WP-BE-05：Manual Compaction = Context View Compression（不是业务数据删除）。
复用已有 ConversationCompactor / CompactionAudit，**不新造第二套 compressor**。

语义：
- 保护当前目标 / anchors / recent turns → 生成或更新 Conversation Summary →
  持久化 → 记录 Compaction Audit → 下一轮 compose 用 Summary + recent messages；
- **绝不删除**：原始聊天消息 / User Memory / Project Rules / RAG Documents /
  Index Chunks / Task State / Checkpoint / Artifact；
- 同一个 Conversation 同一时间只允许一个 manual compaction：
  进程内 per-conversation lock + DB 检查进行中的 compaction run；
  并发请求 → 409 Conflict。

返回值：
- before_used_tokens / after_used_tokens / saved_tokens / summary_updated /
  as_of。
- 若真实实现无法可靠给出 after_used_tokens → 不伪造，返回可计算字段。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine.conversation_retention import (
    ConversationRetentionDecision,
    NORMAL_RECENT_TURNS,
    choose_retention_decision,
    count_complete_turns,
    split_before_recent_turns,
)

logger = logging.getLogger(__name__)

class ManualCompactionError(Exception):
    """手动压缩失败（安全 detail）。"""

    def __init__(self, detail: str, code: str = "context.compact.error") -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


class ManualCompactionConflictError(ManualCompactionError):
    """并发冲突：同一 Conversation 已有进行中的 manual compaction。"""

    def __init__(self, detail: str = "该会话正在执行上下文压缩，请稍后再试") -> None:
        super().__init__(detail, code="context.compact.in_progress")


@dataclass
class ManualCompactionResult:
    """手动压缩结果（不包含正文）。"""

    run_public_id: str | None = None
    summary_public_id: str | None = None
    before_used_tokens: int | None = None
    after_used_tokens: int | None = None
    saved_tokens: int | None = None
    summary_updated: bool = False
    as_of: str = ""
    in_progress: bool = False
    strategy: str | None = None
    preserved_complete_turns: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_public_id": self.run_public_id,
            "summary_public_id": self.summary_public_id,
            "before_used_tokens": self.before_used_tokens,
            "after_used_tokens": self.after_used_tokens,
            "saved_tokens": self.saved_tokens,
            "summary_updated": self.summary_updated,
            "as_of": self.as_of,
            "in_progress": self.in_progress,
            "strategy": self.strategy,
            "preserved_complete_turns": self.preserved_complete_turns,
        }


@dataclass(frozen=True)
class _ManualCompactionSource:
    """Real conversation payload and its durable coverage boundary."""

    text: str
    source_refs: tuple[Any, ...]
    source_digest: str
    tokens_before: int
    covered_message_start_id: int | None
    covered_message_end_id: int | None
    covered_message_count: int
    retention: ConversationRetentionDecision


class ManualConversationCompactionService:
    """手动 Conversation 压缩服务（owner-scoped，进程内并发锁）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        # 进程内 per-conversation lock（同一进程不并发；跨进程由 DB run 检查兜底）
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, conversation_internal_id: int) -> asyncio.Lock:
        return self._locks.setdefault(conversation_internal_id, asyncio.Lock())

    # ── 公开入口 ──────────────────────────────────────────────────

    async def compact(
        self,
        *,
        conversation_public_id: str,
        user_internal_id: int,
        session_factory=None,
        llm_invoker=None,
        llm_client=None,
    ) -> ManualCompactionResult:
        """执行一次手动压缩。

        - owner 校验：conversation 必须属于 user，否则 404。
        - 并发 guard：同 conversation 同时只允许一个 manual compaction。
        - 使用 ConversationCompactor 对真实会话 payload 生成单一权威摘要。
        """
        conv = await self._load_conversation(conversation_public_id, user_internal_id)
        if conv is None:
            raise ManualCompactionError(
                "会话不存在或不属于当前用户", code="context.compact.conversation_not_found"
            )

        # 并发 guard（进程内锁）
        lock = self._lock_for(conv.id)
        if lock.locked():
            raise ManualCompactionConflictError()
        async with lock:
            # 检查是否有进行中的 compaction run（DB 兜底）
            if await self._has_in_progress_run(conv.id, user_internal_id):
                raise ManualCompactionConflictError()

            source = await self._build_compaction_source(
                conversation_id=conv.id,
                user_internal_id=user_internal_id,
            )
            if not source.text.strip() or not source.source_refs:
                raise ManualCompactionError(
                    "当前会话没有可压缩的上下文",
                    code="context.compact.no_content",
                )

            # Manual compaction has one authoritative provider call.  The old
            # path first created an incremental summary and then invoked the
            # compactor with an empty payload, allowing an empty summary to
            # supersede the useful one.
            run_public_id, summary_public_id, compacted_tokens_after = await self._run_compactor(
                conversation_id=conv.id,
                user_internal_id=user_internal_id,
                conversation_public_id=conversation_public_id,
                source=source,
                session_factory=session_factory,
                llm_invoker=llm_invoker,
                llm_client=llm_client,
            )
            if (
                run_public_id is None
                or summary_public_id is None
                or compacted_tokens_after is None
            ):
                raise ManualCompactionError(
                    "上下文压缩未完成，请稍后重试",
                    code="context.compact.failed",
                )

            before_used = source.tokens_before
            after_used = compacted_tokens_after
            saved = max(before_used - after_used, 0)

            return ManualCompactionResult(
                run_public_id=run_public_id,
                summary_public_id=summary_public_id,
                before_used_tokens=before_used,
                after_used_tokens=after_used,
                saved_tokens=saved,
                summary_updated=True,
                as_of=_utcnow_iso(),
                strategy=source.retention.level,
                preserved_complete_turns=source.retention.keep_turns,
            )

    # ── 内部 ──────────────────────────────────────────────────────

    async def _load_conversation(self, public_id: str, user_internal_id: int):
        from app.models.conversation import Conversation

        result = await self._session.execute(
            select(Conversation).where(
                Conversation.public_id == public_id,
                Conversation.user_id == user_internal_id,
                Conversation.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def _has_in_progress_run(
        self, conversation_id: int, user_internal_id: int
    ) -> bool:
        """是否有进行中的 manual compaction run（DB 兜底并发检查）。"""
        from app.models.context_engine import ContextCompactionRun

        result = await self._session.execute(
            select(ContextCompactionRun.id).where(
                ContextCompactionRun.conversation_id == conversation_id,
                ContextCompactionRun.user_id == user_internal_id,
                ContextCompactionRun.status.in_(["running", "pending"]),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def _build_compaction_source(
        self,
        *,
        conversation_id: int,
        user_internal_id: int,
    ) -> _ManualCompactionSource:
        """Build the real payload currently represented in chat context.

        The payload merges the prior durable summary with only the older half
        of the normal 20-turn raw window.  The latest 10 turns are deliberately
        excluded from summarization and remain verbatim in future contexts.
        """
        from app.context_engine.models.context import ContextRef
        from app.context_engine.models.enums import ContextKind, SourceType
        from app.repositories.conversation_summary_repository import (
            ConversationSummaryRepository,
        )
        from app.repositories.message_repository import MessageRepository

        # Manual compaction follows the automatic two-tier contract.  First
        # measure what would age out under light retention; a prior summary
        # plus only a few newly-aged turns is a quick re-growth and uses the
        # deeper twelve-turn tail.
        messages = await MessageRepository(self._session).list_context_messages_by_conversation(
            user_id=user_internal_id,
            conversation_id=conversation_id,
        )
        summary = await ConversationSummaryRepository(
            self._session
        ).get_latest_active_by_type(
            conversation_id,
            user_internal_id,
            "conversation",
        )

        light_compactable, _preserved_messages = split_before_recent_turns(
            messages,
            keep_turns=NORMAL_RECENT_TURNS,
        )
        prior_cursor = int(getattr(summary, "covered_message_end_id", 0) or 0)
        newly_aged_light = [
            message
            for message in light_compactable
            if int(getattr(message, "id", 0) or 0) > prior_cursor
        ]
        retention = choose_retention_decision(
            has_prior_summary=summary is not None,
            newly_compactable_turns=count_complete_turns(newly_aged_light),
            manual=True,
        )
        compactable_candidates, _preserved_messages = split_before_recent_turns(
            messages,
            keep_turns=retention.keep_turns,
        )
        # A prior summary already represents everything through its cursor.
        # Re-sending covered messages would duplicate evidence and gradually
        # make a replacement summary less faithful on every manual click.
        compactable_messages = [
            message
            for message in compactable_candidates
            if int(getattr(message, "id", 0) or 0) > prior_cursor
        ]
        if not compactable_messages:
            return _ManualCompactionSource(
                text="",
                source_refs=(),
                source_digest=hashlib.sha256(b"").hexdigest(),
                tokens_before=0,
                covered_message_start_id=None,
                covered_message_end_id=None,
                covered_message_count=0,
                retention=retention,
            )

        rendered: list[str] = []
        refs: list[ContextRef] = []
        if summary is not None and (summary.summary_text or "").strip():
            rendered.append(f"[conversation_summary]\n{summary.summary_text.strip()}")
            refs.append(
                ContextRef(
                    item_id=f"summary:{summary.public_id}",
                    kind=ContextKind.CONVERSATION,
                    source_type=SourceType.CONVERSATION_SUMMARY,
                    source_ref=summary.public_id,
                )
            )

        for message in compactable_messages:
            content = (getattr(message, "content", "") or "").strip()
            if not content:
                continue
            role = "User" if getattr(message, "role", "") == "user" else "Assistant"
            rendered.append(f"[{role}]\n{content}")
            public_id = getattr(message, "public_id", None) or str(message.id)
            refs.append(
                ContextRef(
                    item_id=f"msg:{public_id}",
                    kind=ContextKind.CONVERSATION,
                    source_type=SourceType.CONVERSATION,
                    source_ref=public_id,
                )
            )

        text = "\n\n".join(rendered)
        covered_end = int(getattr(compactable_messages[-1], "id", 0) or 0)
        covered_start = getattr(summary, "covered_message_start_id", None)
        if covered_start is None:
            covered_start = compactable_messages[0].id
        prior_count = int(getattr(summary, "message_count", 0) or 0)
        covered_count = prior_count + len(compactable_messages)

        return _ManualCompactionSource(
            text=text,
            source_refs=tuple(refs),
            source_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            tokens_before=max(1, len(text) // 3) if text else 0,
            covered_message_start_id=covered_start,
            covered_message_end_id=covered_end,
            covered_message_count=covered_count,
            retention=retention,
        )

    async def _run_compactor(
        self,
        *,
        conversation_id: int,
        user_internal_id: int,
        conversation_public_id: str,
        source: _ManualCompactionSource,
        session_factory,
        llm_invoker,
        llm_client,
    ) -> tuple[str | None, str | None, int | None]:
        """复用 ConversationCompactor（若可装配）。

        需要 session_factory + context_llm_invoker 才能驱动 compactor；
        否则跳过（仅生成摘要，仍有效降低 usage）。
        """
        if session_factory is None or llm_invoker is None:
            return None, None, None
        try:
            from app.context_engine.compression.conversation_compactor import (
                ConversationCompactor,
            )
            from app.context_engine.models.context import ContextRequest
            from app.context_engine.models.enums import (
                CompactionTriggerType,
                CompactionType,
                RecoveryMode,
            )
            from app.context_engine.compression.anchor import ProtectedAnchorBuilder

            compactor = ConversationCompactor(session_factory=session_factory)
            request = ContextRequest(
                user_id=str(user_internal_id),
                conversation_id=str(conversation_public_id),
                call_site="compression.conversation.manual",
                current_user_message=None,
            )
            protected = ProtectedAnchorBuilder().build(
                request,
                None,
                _empty_selected(),
            )
            from app.context_engine.compression.models import ContextCompactionRequest

            req = ContextCompactionRequest(
                request_id=f"manual_compact_{conversation_public_id}",
                user_id=user_internal_id,
                conversation_id=conversation_id,
                workspace_key=f"conversation:{conversation_public_id}",
                call_site="compression.conversation.manual",
                compaction_type=CompactionType.CONVERSATION,
                trigger=CompactionTriggerType.MANUAL,
                policy_key=source.retention.policy_key,
                policy_version="v2",
                source_digest=source.source_digest,
                tokens_before=max(source.tokens_before, 1),
                target_tokens=max(int(source.tokens_before * source.retention.target_ratio), 1),
                protected_anchors=list(protected),
                source_refs=list(source.source_refs),
                recovery_mode=RecoveryMode.SUMMARY_WITH_REFS,
                source_payload={
                    "conversation": source.text,
                    "selected_item_count": len(source.source_refs),
                },
                covered_message_start_id=source.covered_message_start_id,
                covered_message_end_id=source.covered_message_end_id,
                covered_message_count=source.covered_message_count,
            )
            # runtime_context 需要挂 context_llm_invoker 供 compactor 生成摘要
            runtime = _build_runtime_context(
                llm_invoker,
                session_factory,
                llm_client=llm_client,
                user_internal_id=user_internal_id,
                conversation_internal_id=conversation_id,
                conversation_public_id=conversation_public_id,
            )
            result = await compactor.compact(req, runtime_context=runtime)
            if result is None:
                return None, None, None
            return result.run_public_id, result.summary_public_id, result.tokens_after
        except Exception as exc:  # noqa: BLE001 — compactor 失败不影响已生成的摘要
            logger.warning(
                "manual compact compactor failed | conv=%d | err=%s",
                conversation_id, type(exc).__name__,
            )
            return None, None, None


def _empty_selected():
    from app.context_engine.models.selection import SelectedContextSet

    return SelectedContextSet(included=[], dropped=[], section_stats={}, total_estimated_tokens=0)


def _build_runtime_context(
    llm_invoker,
    session_factory,
    *,
    llm_client=None,
    user_internal_id: int | None = None,
    conversation_internal_id: int | None = None,
    conversation_public_id: str | None = None,
):
    """构造 compactor 需要的 runtime_context（含 context_llm_invoker）。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        session_factory=session_factory,
        context_llm_invoker=llm_invoker,
        llm_client=llm_client,
        user_internal_id=user_internal_id,
        conversation_internal_id=conversation_internal_id,
        conversation_public_id=conversation_public_id,
        context_workspace_key=(
            f"conversation:{conversation_public_id}"
            if conversation_public_id
            else None
        ),
    )


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ManualConversationCompactionService",
    "ManualCompactionResult",
    "ManualCompactionError",
    "ManualCompactionConflictError",
]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (WP-BE-05 用户主动压缩):
#
#   链路:
#     ChatView "压缩会话" 按钮 (或 SettingsView)
#       → api/v1/conversations.py POST /conversations/{id}/compact
#         → ManualConversationCompactionService.compact(conversation_id, user_id)
#           → 构造真实 conversation payload + ConversationCompactor
#             → CompactionAudit 落库(who/when/before/after token 数)
#           → 不删除原消息,仅用压缩后的 summary 作为下游 LLM 输入
#
# 关键约束(供开发者速查):
#   - 压缩 = Context View Compression,**不是**业务数据删除;
#   - 必须复用 Compactor / Audit,**不新造第二套**;
#   - 同一会话可重复触发,CompactionAudit 表记录历史;
#   - UI 上"压缩"按钮对已完成 / 失败任务都允许,但有 60s 冷却防止误触。
