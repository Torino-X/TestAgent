"""Context Memory API：list / activate / reject / forget / delete。

CE-03 WP-8：owner-scoped（get_current_user + get_db）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.schemas.auth import UserProfile

router = APIRouter()


class MemoryCreateRequest(BaseModel):
    scope_type: str
    workspace_key: str | None = None
    agent_type: str | None = None
    memory_type: str = "fact"
    content: str
    title: str | None = None
    dedupe_key: str | None = None


@router.get("")
async def list_memories(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    scope_type: str | None = None,
    status: str | None = None,
    limit: int = 50,
):
    from sqlalchemy import select
    from app.models.context_engine import ContextMemory

    stmt = select(ContextMemory).where(
        ContextMemory.user_id == current.internal_id,
        ContextMemory.deleted_at.is_(None),
    )
    if scope_type:
        stmt = stmt.where(ContextMemory.scope_type == scope_type)
    if status:
        stmt = stmt.where(ContextMemory.status == status)
    stmt = stmt.order_by(ContextMemory.updated_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "memory_public_id": m.public_id,
            "scope_type": m.scope_type,
            "workspace_key": m.workspace_key,
            "agent_type": m.agent_type,
            "memory_type": m.memory_type,
            "title": m.title,
            "status": m.status,
            "importance": m.importance,
            "confidence": float(m.confidence) if m.confidence is not None else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in rows
    ]


@router.post("", response_model=dict)
async def create_memory(
    body: MemoryCreateRequest,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.context_engine.memory import MemoryService, MemoryServiceError

    # CPS-06: workspace_key External Input 收口
    # 1. User Memory (scope_type='user'): 强制 workspace_key=NULL
    #    不允许客户端指定任意 workspace_key
    # 2. Workspace Memory (scope_type='workspace'): 服务端校验格式
    #    必须匹配 conversation:{conversation_public_id} 格式
    workspace_key = body.workspace_key
    if body.scope_type == "user":
        # User Memory 属于 user_id，不随 Conversation 变化
        # 强制覆盖客户端可能传入的任意值
        workspace_key = None
    elif body.scope_type == "workspace" and workspace_key:
        # workspace memory 必须是 conversation:{conversation_public_id} 格式
        import re
        if not re.match(r"^conversation:[a-zA-Z0-9_-]+$", workspace_key):
            raise HTTPException(
                status_code=400,
                detail={"code": "invalid_workspace_key",
                        "detail": "workspace_key 必须为 conversation:{conversation_public_id} 格式"},
            )

    try:
        result = await MemoryService(session).create_candidate(
            user_id=current.internal_id,
            scope_type=body.scope_type,
            workspace_key=workspace_key,
            agent_type=body.agent_type,
            memory_type=body.memory_type,
            content=body.content,
            title=body.title,
            dedupe_key=body.dedupe_key,
        )
        await session.commit()
        # Phase 1 cache (P0 整改): memory create 影响 list 视图. 立即 invalidate
        # 对应 workspace 的 ContextMemoryCache. 失败仅 log,主流程不受影响.
        try:
            from app.cache.domains.context_cache import (
                get_context_memory_cache,
                workspace_hash_for,
            )

            await get_context_memory_cache().invalidate(
                current.internal_id,
                workspace_hash_for(workspace_key),
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "context_memory.create_memory: cache invalidate failed | "
                "user=%s | %s",
                current.internal_id, exc,
            )
        return result
    except MemoryServiceError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "detail": exc.detail})


async def _invalidate_memory_cache_for_public_id(
    session: AsyncSession, user_id: int, memory_public_id: str
) -> None:
    """Look up the memory row's workspace_key and invalidate that cache entry.

    Best-effort: failure is logged but not raised.  Centralised here so all
    mutation endpoints (activate / reject / forget / delete) share the same
    cache-invalidation semantics.  If the row is not found the caller already
    raises a 404, so we just no-op.
    """
    try:
        from sqlalchemy import select
        from app.models.context_engine import ContextMemory
        from app.cache.domains.context_cache import (
            get_context_memory_cache,
            workspace_hash_for,
        )

        res = await session.execute(
            select(ContextMemory.workspace_key).where(
                ContextMemory.public_id == memory_public_id,
                ContextMemory.user_id == user_id,
            )
        )
        row = res.first()
        if row is None:
            return
        workspace_key = row[0]
        await get_context_memory_cache().invalidate(
            user_id, workspace_hash_for(workspace_key),
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "context_memory: cache invalidate failed | user=%s | mem=%s | %s",
            user_id, memory_public_id, exc,
        )


async def _invalidate_memory_cache(user_id: int, workspace_key: str | None) -> None:
    """Invalidate ContextMemoryCache for (user, workspace).

    Best-effort: failure is logged but not raised.
    """
    try:
        from app.cache.domains.context_cache import (
            get_context_memory_cache,
            workspace_hash_for,
        )

        await get_context_memory_cache().invalidate(
            user_id, workspace_hash_for(workspace_key),
        )
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "context_memory: cache invalidate failed | user=%s | %s",
            user_id, exc,
        )


@router.post("/{memory_public_id}/activate", response_model=dict)
async def activate_memory(
    memory_public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.context_engine.memory import MemoryService, MemoryServiceError

    try:
        result = await MemoryService(session).activate(
            user_id=current.internal_id, memory_public_id=memory_public_id
        )
        await session.commit()
        # Phase 1 cache (P0 整改): activation 改变 list 视图. invalidate.
        await _invalidate_memory_cache_for_public_id(
            session, current.internal_id, memory_public_id
        )
        return result
    except MemoryServiceError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "detail": exc.detail})


@router.post("/{memory_public_id}/reject", response_model=dict)
async def reject_memory(
    memory_public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.context_engine.memory import MemoryService, MemoryServiceError

    try:
        result = await MemoryService(session).reject(
            user_id=current.internal_id, memory_public_id=memory_public_id
        )
        await session.commit()
        # Phase 1 cache (P0 整改): reject 改变 list 视图.
        await _invalidate_memory_cache_for_public_id(
            session, current.internal_id, memory_public_id
        )
        return result
    except MemoryServiceError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "detail": exc.detail})


@router.post("/{memory_public_id}/forget", response_model=dict)
async def forget_memory(
    memory_public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.context_engine.memory import MemoryService, MemoryServiceError

    try:
        result = await MemoryService(session).forget(
            user_id=current.internal_id, memory_public_id=memory_public_id
        )
        await session.commit()
        # Phase 1 cache (P0 整改): forget 删除 memory, list 视图必须立即失效.
        await _invalidate_memory_cache_for_public_id(
            session, current.internal_id, memory_public_id
        )
        return result
    except MemoryServiceError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "detail": exc.detail})


@router.delete("/{memory_public_id}", response_model=dict)
async def delete_memory(
    memory_public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.context_engine.memory import MemoryService, MemoryServiceError

    try:
        result = await MemoryService(session).delete(
            user_id=current.internal_id, memory_public_id=memory_public_id
        )
        await session.commit()
        # Phase 1 cache (P0 整改): hard delete 同样使 list 视图失效.
        await _invalidate_memory_cache_for_public_id(
            session, current.internal_id, memory_public_id
        )
        return result
    except MemoryServiceError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "detail": exc.detail})


# 路由清单(CE-03 Memory,user-scoped):
#   GET    /api/v1/context/memories                       列出当前 user 的 memories
#   GET    /api/v1/context/memories/{public_id}           单条
#   POST   /api/v1/context/memories/{public_id}/activate  启用
#   POST   /api/v1/context/memories/{public_id}/reject    拒绝(下次不读入 context)
#   POST   /api/v1/context/memories/{public_id}/forget   忘记(soft delete)
#   DELETE /api/v1/context/memories/{public_id}           hard delete(物理)
#
# 链路:
#   UserMemoryRepository / ProjectRuleRepository
#   → ContextLearningService.learn_from_turn() 异步写入
#   → 本路由暴露 CRUD 让用户可以管理写入的记忆
#
# 关键约束:
#   - scope=user 与 scope=project(conversation_id 二级)严格隔离;
#   - reject 不删行,只设 `is_rejected=true`,方便未来回滚;
#   - forget 是 soft delete,审计可恢复;hard delete 不可逆;
#   - 写入不通过本路由(由 ContextLearningService 异步)。
