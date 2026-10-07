"""CPS-05: Task Conversation Scope Integrity Check.

验证 task.context_workspace_key 与所属 Conversation 派生的 workspace_key 一致。
在 resume / confirm 入口调用；不匹配时抛 ScopeIntegrityError → BLOCKED。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ScopeIntegrityError(Exception):
    """Task Conversation Scope 不一致（数据完整性错误）"""

    def __init__(self, *, task_public_id: str, detail: str) -> None:
        self.task_public_id = task_public_id
        self.detail = detail
        super().__init__(detail)


async def validate_task_conversation_scope(
    session: AsyncSession,
    *,
    task_id: int,
    task_public_id: str,
    task_context_workspace_key: str | None,
    task_conversation_id: int | None,
    task_user_id: int | None,
    current_user_id: int | None,
) -> None:
    """CPS-05: 校验 Task 的 Conversation Scope 一致性。

    校验项:
    1. task.user_id == current_user（防止越权）
    2. task.conversation_id 存在且指向有效 Conversation
    3. task.context_workspace_key == conversation:{conversation.public_id}
    4. 不匹配时抛 ScopeIntegrityError → BLOCKED

    Legacy 兼容:
    - task_context_workspace_key is None → 允许（Legacy task 无 workspace_key）
    - task_conversation_id is None → 允许（Legacy task）
    - 两者都为 None → Legacy task，跳过 scope 校验
    """
    # 1) 用户隔离校验
    if current_user_id is not None and task_user_id is not None:
        if task_user_id != current_user_id:
            raise ScopeIntegrityError(
                task_public_id=task_public_id,
                detail=f"跨用户 scope 校验失败: task.user_id={task_user_id} != current_user={current_user_id}",
            )

    # 2) Legacy Task 兼容：无 workspace_key 且无 conversation_id → 跳过
    if task_context_workspace_key is None and task_conversation_id is None:
        # Legacy historical task → 允许恢复，不做 scope 校验
        logger.debug("CPS-05: Legacy task scope 校验跳过 | task=%s", task_public_id)
        return

    # 3) Task 必须有 conversation_id（3.0 正式模型下 Task 必须属于 Conversation）
    if task_conversation_id is None:
        raise ScopeIntegrityError(
            task_public_id=task_public_id,
            detail="3.0 Task 缺少 conversation_id，数据完整性错误",
        )

    # 4) 加载 Conversation，推导 expected workspace_key
    from app.models.agent_task import AgentTask
    from app.models.conversation import Conversation

    conv = (await session.execute(
        select(Conversation).where(Conversation.id == task_conversation_id)
    )).scalar_one_or_none()

    if conv is None:
        raise ScopeIntegrityError(
            task_public_id=task_public_id,
            detail=f"conversation_id={task_conversation_id} 指向不存在的 Conversation",
        )

    # 5) 校验 task_context_workspace_key == conversation:{conversation.public_id}
    expected_key = f"conversation:{conv.public_id}"
    if task_context_workspace_key is None:
        await session.execute(
            update(AgentTask)
            .where(AgentTask.id == task_id)
            .where(AgentTask.context_workspace_key.is_(None))
            .values(
                context_workspace_key=expected_key,
                context_engine_version="v3",
            )
        )
        await session.flush()
        logger.info(
            "CPS-05: backfilled task context workspace | task=%s | workspace=%s",
            task_public_id,
            expected_key,
        )
        return

    if task_context_workspace_key != expected_key:
        raise ScopeIntegrityError(
            task_public_id=task_public_id,
            detail=(
                f"Context Workspace Key 不匹配: "
                f"task.context_workspace_key={task_context_workspace_key!r} "
                f"!= expected={expected_key!r} "
                f"(conversation.public_id={conv.public_id!r})"
            ),
        )
# auto-appended module-level note: task scope 校验: 阻止跨任务 / 跨 session 引用。
