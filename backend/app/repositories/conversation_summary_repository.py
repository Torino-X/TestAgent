"""Conversation summary repository."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation_summary import ConversationSummary
from app.repositories.base import BaseRepository


class ConversationSummaryRepository(BaseRepository[ConversationSummary]):
    model = ConversationSummary

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def create(self, summary: ConversationSummary) -> ConversationSummary:
        self.session.add(summary)
        await self.session.flush()
        return summary

    async def get_latest_active(
        self, conversation_id: int
    ) -> ConversationSummary | None:
        result = await self.session.execute(
            select(ConversationSummary)
            .where(
                ConversationSummary.conversation_id == conversation_id,
                ConversationSummary.status == "active",
            )
            .order_by(ConversationSummary.updated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def count_by_conversation(self, conversation_id: int) -> int:
        result = await self.session.execute(
            select(func.count())
            .select_from(ConversationSummary)
            .where(
                ConversationSummary.conversation_id == conversation_id,
                ConversationSummary.status == "active",
            )
        )
        return result.scalar() or 0

    # ── CE-01: owner-scoped + summary_type 支持 ─────────────────────

    async def get_latest_active_by_type(
        self, conversation_id: int, user_id: int, summary_type: str
    ) -> ConversationSummary | None:
        result = await self.session.execute(
            select(ConversationSummary)
            .where(
                ConversationSummary.conversation_id == conversation_id,
                ConversationSummary.user_id == user_id,
                ConversationSummary.summary_type == summary_type,
                ConversationSummary.status == "active",
            )
            .order_by(ConversationSummary.updated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_public_id(
        self, public_id: str, user_id: int
    ) -> ConversationSummary | None:
        result = await self.session.execute(
            select(ConversationSummary).where(
                ConversationSummary.public_id == public_id,
                ConversationSummary.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def supersede(
        self, summary_id: int, user_id: int, superseded_by: int
    ) -> None:
        """将旧摘要标记为 superseded（不删除）。"""
        result = await self.session.execute(
            select(ConversationSummary).where(
                ConversationSummary.id == summary_id,
                ConversationSummary.user_id == user_id,
            )
        )
        summary = result.scalar_one_or_none()
        if summary is not None:
            summary.status = "superseded"
            summary.supersedes_summary_id = superseded_by

    async def supersede_active_by_type(
        self,
        conversation_id: int,
        user_id: int,
        summary_type: str,
        superseded_by: int,
    ) -> None:
        """Atomically retire every prior active summary in one lineage.

        The caller creates and flushes the replacement first, then calls this
        method in the same transaction.  Excluding the replacement leaves
        exactly that new row active, including when historical data already
        contains more than one incorrectly-active row.
        """
        await self.session.execute(
            update(ConversationSummary)
            .where(
                ConversationSummary.conversation_id == conversation_id,
                ConversationSummary.user_id == user_id,
                ConversationSummary.summary_type == summary_type,
                ConversationSummary.status == "active",
                ConversationSummary.id != superseded_by,
            )
            .values(
                status="superseded",
                supersedes_summary_id=superseded_by,
            )
        )


# 模块定位:ConversationSummary 仓储
#
# 链路:
#   ConversationSummaryService.generate → summary 行 upsert
#   build_chat_context → 读 is_current=True 行
#
# 关键约束:
#   - 同一 conversation 同时只有 1 行 is_current=True;
#   - 切换 is_current 必须事务边界。
