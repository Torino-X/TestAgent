"""Human confirmation repository."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.human_confirmation import HumanConfirmation
from app.repositories.base import BaseRepository


class ConfirmationRepository(BaseRepository[HumanConfirmation]):
    model = HumanConfirmation

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_pending_by_task(self, task_internal_id: int) -> HumanConfirmation | None:
        result = await self.session.execute(
            select(HumanConfirmation).where(
                HumanConfirmation.task_id == task_internal_id,
                HumanConfirmation.status == "pending",
            )
        )
        return result.scalar_one_or_none()

    async def get_latest_by_task(self, task_internal_id: int) -> HumanConfirmation | None:
        """Get the most recent confirmation for a task (any status)."""
        result = await self.session.execute(
            select(HumanConfirmation)
            .where(HumanConfirmation.task_id == task_internal_id)
            .order_by(HumanConfirmation.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_confirmed_by_task(self, task_internal_id: int) -> list[HumanConfirmation]:
        """Return the task's confirmed checkpoints in the order the user submitted them."""
        result = await self.session.execute(
            select(HumanConfirmation)
            .where(
                HumanConfirmation.task_id == task_internal_id,
                HumanConfirmation.status == "confirmed",
            )
            .order_by(
                HumanConfirmation.confirmed_at.asc(),
                HumanConfirmation.created_at.asc(),
            )
        )
        return list(result.scalars().all())

    async def create(self, confirm: HumanConfirmation) -> HumanConfirmation:
        # aiomysql driver chokes on dict params for JSON columns —
        # use raw INSERT with explicit json.dumps.
        import json as _json
        from sqlalchemy import text

        req_str = None
        if confirm.request_json is not None:
            req_str = _json.dumps(
                confirm.request_json, ensure_ascii=False, default=str
            )

        await self.session.execute(
            text(
                "INSERT INTO human_confirmations "
                "(public_id, user_id, conversation_id, task_id, "
                " confirmation_type, status, prompt_text, request_json, "
                " requested_at, created_at, updated_at) "
                "VALUES "
                "(:pid, :uid, :cid, :tid, :ct, :st, :pt, :rj, "
                " :ra, :ca, :ua)"
            ),
            {
                "pid": confirm.public_id,
                "uid": confirm.user_id,
                "cid": confirm.conversation_id,
                "tid": confirm.task_id,
                "ct": confirm.confirmation_type,
                "st": confirm.status,
                "pt": confirm.prompt_text,
                "rj": req_str,
                "ra": confirm.requested_at,
                "ca": confirm.created_at,
                "ua": confirm.updated_at,
            },
        )
        await self.session.flush()
        return confirm

    async def confirm(self, confirm_id: int, response_json: dict, now: object) -> None:
        import json as _json
        from sqlalchemy import text

        resp_str = _json.dumps(response_json, ensure_ascii=False, default=str)
        await self.session.execute(
            text(
                "UPDATE human_confirmations "
                "SET status = 'confirmed', response_json = :rj, confirmed_at = :ca, updated_at = :ca "
                "WHERE id = :cid"
            ),
            {"rj": resp_str, "ca": now, "cid": confirm_id},
        )
        await self.session.flush()


# 模块定位:HumanConfirmation 仓储
#
# 链路:
#   v3 nodes_interrupt.prepare_section_confirmation → upsert
#   ConfirmationTimeoutService → list pending + update status
#
# 关键约束:
#   - public_id 是 LangGraph interrupt() 传给前端的句柄 ——
#     创建后必须 commit,前端才能 subscribe;
#   - status 翻转需要 StateManager 校验的状态机;
#   - 与 message + task 强耦合但不级联 FK(允许删除时清理)。
