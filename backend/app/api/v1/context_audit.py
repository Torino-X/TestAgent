"""CE-05 Context Audit API — 三类只读审计端点。

- GET /api/v1/context/audit/snapshots + /{public_id}
- GET /api/v1/context/audit/retrieval + /{public_id}
- GET /api/v1/context/audit/compaction + /{public_id}

owner-scope（user_id 过滤）+ 统一脱敏 + opaque composite cursor。
Envelope 复用 ApiResponse；错误 401/404（非属主防枚举）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.audit_serializers import (
    serialize_compaction,
    serialize_retrieval,
    serialize_retrieval_candidate,
    serialize_snapshot,
)
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile

router = APIRouter()


def _not_found(code: str, message: str) -> HTTPException:
    from app.core.response import error

    return HTTPException(
        status_code=404,
        detail=error(40400, message, {"error_code": code}),
    )


@router.get("/audit/snapshots")
async def list_snapshots(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    context_kind: Optional[str] = None,
    conversation_id: Optional[str] = None,
):
    """Snapshot 审计列表（owner-scope，只读字段白名单）。"""
    from app.models.context_snapshot import ContextSnapshot

    stmt = select(ContextSnapshot).where(
        ContextSnapshot.user_id == current.internal_id
    )
    if context_kind:
        stmt = stmt.where(ContextSnapshot.context_kind == context_kind)
    if conversation_id:
        from app.models.conversation import Conversation

        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.public_id == conversation_id,
                    Conversation.user_id == current.internal_id,
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            return success({"items": []})
        stmt = stmt.where(ContextSnapshot.conversation_id == conversation.id)
    stmt = stmt.order_by(ContextSnapshot.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return success({"items": [serialize_snapshot(r) for r in rows]})


@router.get("/audit/snapshots/{public_id}")
async def get_snapshot(
    public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.models.context_snapshot import ContextSnapshot

    row = (
        await session.execute(
            select(ContextSnapshot).where(
                ContextSnapshot.public_id == public_id,
                ContextSnapshot.user_id == current.internal_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("context.audit.snapshot.not_found", "快照不存在或不属于当前用户")
    return success(serialize_snapshot(row))


@router.get("/audit/retrieval")
async def list_retrieval(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Retrieval 审计列表（owner-scope，统计字段）。"""
    from app.models.context_engine import ContextRetrievalRun

    stmt = (
        select(ContextRetrievalRun)
        .where(ContextRetrievalRun.user_id == current.internal_id)
        .order_by(ContextRetrievalRun.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return success({"items": [serialize_retrieval(r) for r in rows]})


@router.get("/audit/retrieval/{public_id}")
async def get_retrieval(
    public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.models.context_engine import ContextRetrievalRun
    from app.repositories.context_engine_repositories import ContextRetrievalCandidateRepository

    row = (
        await session.execute(
            select(ContextRetrievalRun).where(
                ContextRetrievalRun.public_id == public_id,
                ContextRetrievalRun.user_id == current.internal_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise _not_found("context.audit.retrieval.not_found", "检索审计不存在或不属于当前用户")
    candidate_rows = await ContextRetrievalCandidateRepository(session).list_detail_for_run(
        row.id,
        current.internal_id,
    )
    detail = serialize_retrieval(row)
    detail["candidate_count"] = len(candidate_rows)
    detail["candidates"] = [
        serialize_retrieval_candidate(
            candidate,
            index_document_public_id=document_public_id,
            index_chunk_public_id=chunk_public_id,
        )
        for candidate, document_public_id, chunk_public_id in candidate_rows
    ]
    return success(detail)


@router.get("/audit/compaction")
async def list_compaction(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    status: Optional[str] = None,
    compaction_type: Optional[str] = None,
    conversation_id: Optional[str] = None,
):
    """Compaction 审计列表（owner-scope）。"""
    from app.context_engine.compression.audit import CompactionAuditService

    service = CompactionAuditService(session_factory=_session_factory(session))
    conversation_internal_id = None
    if conversation_id:
        from app.models.conversation import Conversation

        conversation = (
            await session.execute(
                select(Conversation).where(
                    Conversation.public_id == conversation_id,
                    Conversation.user_id == current.internal_id,
                )
            )
        ).scalar_one_or_none()
        if conversation is None:
            return success({"items": []})
        conversation_internal_id = conversation.id
    rows = await service.list_runs(
        current.internal_id,
        limit=limit,
        status=status,
        compaction_type=compaction_type,
        conversation_internal_id=conversation_internal_id,
    )
    return success({"items": [serialize_compaction(r) for r in rows]})


@router.get("/audit/compaction/{public_id}")
async def get_compaction(
    public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.context_engine.compression.audit import CompactionAuditService

    service = CompactionAuditService(session_factory=_session_factory(session))
    row = await service.get_run(current.internal_id, public_id)
    if row is None:
        raise _not_found("context.audit.compaction.not_found", "压缩审计不存在或不属于当前用户")
    return success(serialize_compaction(row))


def _session_factory(session: AsyncSession):
    """把请求级 session 包装为 session_factory（CompactionAuditService 期望工厂）。"""

    @asynccontextmanager
    async def _borrowed_session():
        yield session

    return _borrowed_session


# 路由清单(CE-05 三类只读审计):
#   GET /api/v1/context/audit/snapshots             列出当前 user 的上下文快照
#   GET /api/v1/context/audit/snapshots/{public_id} 单条
#   GET /api/v1/context/audit/retrieval             检索审计行
#   GET /api/v1/context/audit/retrieval/{public_id} 单条
#   GET /api/v1/context/audit/compaction            压缩事件
#   GET /api/v1/context/audit/compaction/{public_id} 单条
#
# 链路:
#   ContextSnapshotRepository / RetrievalAuditRepository / CompactionAuditRepository
#   → owner scope 过滤 → opaque cursor 分页 → 返回响应
#
# 关键约束:
#   - 三类 audit 表都不让前端看 full payload;
#   - ownership 验证为当前 user(`messages` owner, or `retrieval.user_id`);
#   - 分页 cursor 用 base64 of (timestamp + id),不可被前端猜解;
#   - 旧数据保留:audit 不删不写,只查。
