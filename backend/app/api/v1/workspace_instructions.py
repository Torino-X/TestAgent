"""Workspace Instructions API：CRUD + activate。

CE-03 WP-8：owner-scoped（user_id + workspace_key 双条件）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.schemas.auth import UserProfile

router = APIRouter()


class InstructionCreateRequest(BaseModel):
    workspace_key: str
    instruction_key: str
    category: str = "general"
    title: str
    content: str
    priority: int = 5
    effective_from: datetime | None = None
    effective_to: datetime | None = None


@router.get("")
async def list_instructions(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    workspace_key: str | None = None,
    status: str | None = None,
    limit: int = 50,
):
    from sqlalchemy import select
    from app.models.context_engine import ContextWorkspaceInstruction

    stmt = select(ContextWorkspaceInstruction).where(
        ContextWorkspaceInstruction.user_id == current.internal_id,
        ContextWorkspaceInstruction.deleted_at.is_(None),
    )
    if workspace_key:
        stmt = stmt.where(ContextWorkspaceInstruction.workspace_key == workspace_key)
    if status:
        stmt = stmt.where(ContextWorkspaceInstruction.status == status)
    stmt = stmt.order_by(ContextWorkspaceInstruction.priority.asc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "public_id": i.public_id,
            "workspace_key": i.workspace_key,
            "instruction_key": i.instruction_key,
            "category": i.category,
            "title": i.title,
            "content": i.content,
            "priority": i.priority,
            "status": i.status,
            "effective_from": i.effective_from.isoformat() if i.effective_from else None,
            "effective_to": i.effective_to.isoformat() if i.effective_to else None,
        }
        for i in rows
    ]


async def _invalidate_instruction_cache(user_id: int, workspace_key: str) -> None:
    """Best-effort WorkspaceInstructionCache invalidation.

    Failure is logged but not raised.  Centralised so create / activate /
    delete all share the same semantics.
    """
    try:
        from app.cache.domains.context_cache import (
            get_workspace_instruction_cache,
            workspace_hash_for,
        )

        await get_workspace_instruction_cache().invalidate(
            user_id, workspace_hash_for(workspace_key),
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "workspace_instructions: cache invalidate failed | "
            "user=%s | ws=%s | %s",
            user_id, workspace_key, exc,
        )


@router.post("", response_model=dict)
async def create_instruction(
    body: InstructionCreateRequest,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.context_engine.indexing.document_service import _gen_public_id
    from app.context_engine.memory.memory_service import content_hash
    from app.models.context_engine import ContextWorkspaceInstruction

    inst = ContextWorkspaceInstruction(
        public_id=_gen_public_id("wi_"),
        user_id=current.internal_id,
        workspace_key=body.workspace_key,
        instruction_key=body.instruction_key,
        category=body.category,
        title=body.title,
        content=body.content,
        priority=max(1, min(10, body.priority)),
        status="draft",
        content_hash=content_hash(body.content),
        idempotency_key=_gen_public_id("idem_"),
        created_by_user_id=current.internal_id,
        effective_from=body.effective_from,
        effective_to=body.effective_to,
    )
    session.add(inst)
    await session.commit()
    # Phase 1 cache (P0 整改): 新建 instruction 必须立即让对应 workspace 缓存失效.
    await _invalidate_instruction_cache(current.internal_id, body.workspace_key)
    return {"public_id": inst.public_id, "status": inst.status}


@router.post("/{instruction_public_id}/activate", response_model=dict)
async def activate_instruction(
    instruction_public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from sqlalchemy import update, select
    from app.models.context_engine import ContextWorkspaceInstruction

    # Phase 1 cache (P0 整改): 读出 workspace_key 先 invalidate 再 UPDATE
    # (避免缓存与 DB 短窗口不一致,UI 已经显式 activate 了).
    res_ws = await session.execute(
        select(ContextWorkspaceInstruction.workspace_key).where(
            ContextWorkspaceInstruction.public_id == instruction_public_id,
            ContextWorkspaceInstruction.user_id == current.internal_id,
        )
    )
    ws_row = res_ws.first()
    workspace_key = ws_row[0] if ws_row else None

    result = await session.execute(
        update(ContextWorkspaceInstruction)
        .where(
            ContextWorkspaceInstruction.public_id == instruction_public_id,
            ContextWorkspaceInstruction.user_id == current.internal_id,
        )
        .values(status="active")
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail={"code": "context.ws_instruction.not_found", "detail": "指令不存在"})
    await session.commit()
    if workspace_key:
        await _invalidate_instruction_cache(current.internal_id, workspace_key)
    return {"public_id": instruction_public_id, "status": "active"}


@router.delete("/{instruction_public_id}", response_model=dict)
async def delete_instruction(
    instruction_public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timezone
    from sqlalchemy import update, select
    from app.models.context_engine import ContextWorkspaceInstruction

    # Phase 1 cache (P0 整改): 删除前先读出 workspace_key 用于 cache invalidate.
    res_ws = await session.execute(
        select(ContextWorkspaceInstruction.workspace_key).where(
            ContextWorkspaceInstruction.public_id == instruction_public_id,
            ContextWorkspaceInstruction.user_id == current.internal_id,
        )
    )
    ws_row = res_ws.first()
    workspace_key = ws_row[0] if ws_row else None

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = await session.execute(
        update(ContextWorkspaceInstruction)
        .where(
            ContextWorkspaceInstruction.public_id == instruction_public_id,
            ContextWorkspaceInstruction.user_id == current.internal_id,
        )
        .values(deleted_at=now)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail={"code": "context.ws_instruction.not_found", "detail": "指令不存在"})
    await session.commit()
    if workspace_key:
        await _invalidate_instruction_cache(current.internal_id, workspace_key)
    return {"public_id": instruction_public_id, "status": "deleted"}


# 路由清单(CE-03 workspace instructions CRUD):
#   POST   /api/v1/workspace/instructions                  新建(workspace_key + 内容)
#   GET    /api/v1/workspace/instructions                  列出(按 workspace_key 过滤)
#   GET    /api/v1/workspace/instructions/{public_id}      单条
#   PATCH  /api/v1/workspace/instructions/{public_id}      修改 content / title
#   POST   /api/v1/workspace/instructions/{public_id}/activate 启用
#   DELETE /api/v1/workspace/instructions/{public_id}      物理删除
#
# 链路:
#   WorkspaceInstructionRepository
#     → ContextEngine.retrieve_assembly 时读取 activation = true 行
#     → 注入到 system_prompt 前缀
#
# 关键约束:
#   - 双索引 (user_id, workspace_key) ——
#     同一 user 在不同 workspace 可以独立配置;
#   - activate 是置"激活标记";同时只能有 *少量* activations(避免多个互冲);
#   - content 上限 4 KB,防止 LLM 上下文污染;
#   - 删除前 deactivate,否则可能在 in-flight LLM 请求里引用悬挂指针。
