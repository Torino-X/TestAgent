"""AgentContext factory — Phase 2.8R-K 复用 Outbox / SSE 端点构造 ctx 的逻辑。

之前 2.8R-B 实现时,SSE 端点直接构造 ``AgentContext``(~20 行),Outbox Worker 端
只传 dict(导致 ``'dict' object has no attribute 'task_id'`` 错误)。

本模块抽出 helper:
  * ``build_agent_context_from_task_public_id(...)`` — 从 task_public_id 拉 task
    + user + files,构造 AgentContext
  * SSE 端点 / Worker 共用

Outbox 入口见 ``dispatch_from_outbox_row`` in api_dispatcher.py。
"""

from __future__ import annotations

import json
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import AgentContext
from app.models.agent_task import AgentTask
from app.models.conversation import Conversation
from app.models.uploaded_file import UploadedFile
from app.services.settings_service import SettingsService


async def build_agent_context_from_task_public_id(
    *,
    session: AsyncSession,
    task_public_id: str,
) -> AgentContext:
    """从 task_public_id 拉 task + user / files → 构造 AgentContext。

    Phase 2.8R-K 第一版用此签名;但 Outbox row 只有 task_internal_id,
    误把 row.public_id(exq-...) 当 task_public_id 传会找不到 task。
    保留此签名供 SSE 端点继续用;Outbox 入口走 :func:`build_agent_context_from_task_internal_id`。

    Raises:
        ValueError: task 不存在或已删除
    """
    task_row = (
        await session.execute(
            select(AgentTask).where(AgentTask.public_id == task_public_id)
        )
    ).scalar_one_or_none()
    if task_row is None:
        raise ValueError(f"AgentTask not found: public_id={task_public_id!r}")

    return await _build_from_task_row(session, task_row, task_public_id)


async def build_agent_context_from_task_internal_id(
    *,
    session: AsyncSession,
    task_internal_id: int,
) -> AgentContext:
    """Phase 2.8R-K 修正版 — Outbox Worker 入口用 task_internal_id。

    原因:AgentExecutionRequest row 没有 task_public_id 字段,只有 task_id(FK → agent_tasks.id);
    row.public_id 形如 ``exq-{task_public_id}-{request_type}``,不能直接当 public_id 用。

    Args:
        session: SQLAlchemy async session(Outbox Worker _poll_once 持有)
        task_internal_id: agent_tasks.id(Outbox row.task_id)

    Returns:
        AgentContext,task_id 字段塞的是 agent_tasks.public_id(给 orchestrator / SSE 用)。

    Raises:
        ValueError: task 不存在或已删除
    """
    task_row = (
        await session.execute(
            select(AgentTask).where(AgentTask.id == int(task_internal_id))
        )
    ).scalar_one_or_none()
    if task_row is None:
        raise ValueError(
            f"AgentTask not found: task_internal_id={task_internal_id!r}"
        )

    return await _build_from_task_row(session, task_row, task_row.public_id)


async def _build_from_task_row(
    session: AsyncSession,
    task_row: AgentTask,
    task_public_id: str,
) -> AgentContext:
    """共享构造逻辑。"""
    # 查 requirement_file / template_file 公开 id
    req_file_id: Optional[str] = None
    tpl_file_id: Optional[str] = None
    if task_row.requirement_file_id:
        req_row = (
            await session.execute(
                select(UploadedFile.public_id).where(
                    UploadedFile.id == task_row.requirement_file_id
                )
            )
        ).scalar_one_or_none()
        req_file_id = req_row
    if task_row.template_file_id:
        tpl_row = (
            await session.execute(
                select(UploadedFile.public_id).where(
                    UploadedFile.id == task_row.template_file_id
                )
            )
        ).scalar_one_or_none()
        tpl_file_id = tpl_row

    conversation_public_id = (
        await session.execute(
            select(Conversation.public_id).where(
                Conversation.id == task_row.conversation_id,
                Conversation.user_id == task_row.user_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if not conversation_public_id:
        raise ValueError(
            f"Conversation not found for AgentTask: conversation_id={task_row.conversation_id!r}"
        )

    return AgentContext(
        task_id=task_public_id,
        conversation_id=str(conversation_public_id),
        user_id=str(task_row.user_id),
        session=session,
        task_internal_id=task_row.id,
        conversation_internal_id=task_row.conversation_id,
        user_internal_id=task_row.user_id,
        requirement_file_id=req_file_id,
        template_file_id=tpl_file_id,
        settings_service=SettingsService(session),
        user_prompt=task_row.user_instruction or "",
        task_context_json=_coerce_task_context_json(task_row.task_context_json),
    )


def _coerce_task_context_json(value: object) -> dict | None:
    """Normalize the TEXT/JSON variants returned by different DB drivers.

    MySQL deployments store ``task_context_json`` in a TEXT column, while
    some test drivers and newer schemas return an already-decoded dict. The
    outbox dispatcher needs the same context in both cases; silently dropping
    the text form leaves incremental graph state with only empty defaults.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None
    return None


__all__ = [
    "build_agent_context_from_task_public_id",
    "build_agent_context_from_task_internal_id",
]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Phase 2 Legacy AgentContext 工厂):
#
#   链路:
#     LangGraph 节点进入时(V3 nodes_*.py):
#       → ctx.agent_context = AgentContextFactory.build(
#             conversation_internal_id, user_internal_id, session,
#             settings_service, ...)
#         → 返回 AgentContext,挂到 RuntimeContext;
#
#   关键:Legacy AgentContext 与 LangGraphTestPlanGraphState 是平行的两套
#     数据通道 — 节点函数需要把 LangGraph state 镜像到 AgentContext 才能
#     调老工具(因为老工具是 AgentContext 输入)。
#
# 关键约束(供开发者速查):
#   - 工厂不变性:每节点调用 → 同一 conversation 的所有节点应共享同一 agent_context;
#   - 工厂不持久化:它是 *构造* 函数,不缓存;
#   - 升级 Phase 3 时,本服务会被工具的 Session/Runtime 直接替掉(legacy 路径)。
