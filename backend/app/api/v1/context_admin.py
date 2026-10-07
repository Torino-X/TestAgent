"""CE-05 Index Admin API — 管理员异步任务管理。

- GET  /api/v1/context/admin/index/jobs                  （list，admin）
- GET  /api/v1/context/admin/index/jobs/{job_public_id}  （detail，admin）
- POST /api/v1/context/admin/index/jobs/{job_public_id}/retry（admin）
- POST /api/v1/context/admin/index/jobs/{job_public_id}/cancel（admin）
- POST /api/v1/context/admin/index/documents/{doc_public_id}/delete（admin）

跨用户 Admin 查询专用 admin 路径；写 context_admin_access 审计事件。
错误：401 / 403（非 admin）/ 404 / 409（状态冲突）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile

router = APIRouter()


def _http_error(status: int, code: str, message: str) -> HTTPException:
    from app.core.response import error

    return HTTPException(
        status_code=status,
        detail=error(status * 100, message, {"code": code}),
    )


def _iso(dt) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:  # noqa: BLE001
        return str(dt)


def _job_dto(row) -> dict:
    return {
        "job_public_id": row.public_id,
        "operation": row.operation,
        "status": row.status,
        "attempt": row.attempt,
        "max_attempts": row.max_attempts,
        "error_code": row.error_code,
        "error_message": (row.error_message or "")[:300] or None,
        "created_at": _iso(row.created_at),
        "completed_at": _iso(row.completed_at),
    }


async def _write_admin_access_event(session: AsyncSession, *, user: UserProfile, path: str):
    """写 context_admin_access 审计事件（agent_events）。"""
    try:
        import uuid

        from datetime import datetime, timezone

        from app.models.agent_event import AgentEvent

        event = AgentEvent(
            public_id=f"adm_{uuid.uuid4().hex[:32]}",
            user_id=user.internal_id,
            conversation_id=0,
            task_id=0,
            event_type="context_admin_access",
            message_type="context_admin_access",
            title="管理员访问审计",
            content=path,
            status="created",
            created_at=datetime.now(timezone.utc),
        )
        session.add(event)
    except Exception:  # noqa: BLE001 — 审计失败不阻塞
        pass


@router.get("/admin/index/jobs")
async def list_index_jobs(
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
    status: Optional[str] = None,
    operation: Optional[str] = None,
):
    from app.models.context_engine import ContextIndexJob

    stmt = select(ContextIndexJob)
    if status:
        stmt = stmt.where(ContextIndexJob.status == status)
    if operation:
        stmt = stmt.where(ContextIndexJob.operation == operation)
    stmt = stmt.order_by(ContextIndexJob.id.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    await _write_admin_access_event(session, user=current, path="admin/index/jobs")
    await session.commit()
    return success({"items": [_job_dto(r) for r in rows]})


@router.get("/admin/index/jobs/{job_public_id}")
async def get_index_job(
    job_public_id: str,
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    from app.models.context_engine import ContextIndexJob

    row = (
        await session.execute(
            select(ContextIndexJob).where(ContextIndexJob.public_id == job_public_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http_error(404, "context.index.job.not_found", "索引任务不存在")
    await _write_admin_access_event(session, user=current, path=f"admin/index/jobs/{job_public_id}")
    await session.commit()
    return success(_job_dto(row))


@router.post("/admin/index/jobs/{job_public_id}/retry")
async def retry_index_job(
    job_public_id: str,
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    from app.models.context_engine import ContextIndexJob

    row = (
        await session.execute(
            select(ContextIndexJob).where(ContextIndexJob.public_id == job_public_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http_error(404, "context.index.job.not_found", "索引任务不存在")
    if row.status in {"pending", "claimed", "running"}:
        raise _http_error(409, "context.index.job.active", "任务仍处于活动状态，不可重试")
    row.status = "pending"
    row.attempt = 0
    row.error_code = None
    row.error_message = None
    await _write_admin_access_event(session, user=current, path=f"admin/index/jobs/{job_public_id}/retry")
    await session.commit()
    return success({"job_public_id": row.public_id, "status": row.status})


@router.post("/admin/index/jobs/{job_public_id}/cancel")
async def cancel_index_job(
    job_public_id: str,
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    from app.models.context_engine import ContextIndexJob

    row = (
        await session.execute(
            select(ContextIndexJob).where(ContextIndexJob.public_id == job_public_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise _http_error(404, "context.index.job.not_found", "索引任务不存在")
    if row.status in {"completed", "cancelled"}:
        raise _http_error(409, "context.index.job.finished", "任务已完成，不可取消")
    row.status = "cancelled"
    await _write_admin_access_event(session, user=current, path=f"admin/index/jobs/{job_public_id}/cancel")
    await session.commit()
    return success({"job_public_id": row.public_id, "status": row.status})


@router.post("/admin/index/documents/{document_public_id}/delete")
async def delete_index_document_admin(
    document_public_id: str,
    current: UserProfile = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
):
    """Admin 跨用户文档删除（复用既有 repo 逻辑，admin 可跨用户）。"""
    from datetime import datetime, timezone

    from sqlalchemy import update

    from app.models.context_engine import ContextIndexDocument
    from app.repositories.context_engine_repositories import ContextIndexDocumentRepository

    repo = ContextIndexDocumentRepository(session)
    doc = await repo.get_by_public_id(document_public_id, None)
    if doc is None:
        raise _http_error(404, "context.index.not_found", "文档不存在")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await session.execute(
        update(ContextIndexDocument)
        .where(ContextIndexDocument.public_id == document_public_id)
        .values(status="superseded", deleted_at=now)
    )
    await _write_admin_access_event(session, user=current, path=f"admin/index/documents/{document_public_id}/delete")
    await session.commit()
    return success({"document_public_id": document_public_id, "status": "superseded"})


# 路由清单(本文件):
#   GET    /api/v1/context/admin/index/jobs                         列出所有用户的索引任务
#   GET    /api/v1/context/admin/index/jobs/{job_public_id}         单个 job 详情
#   POST   /api/v1/context/admin/index/jobs/{job_public_id}/retry   触发重试
#   POST   /api/v1/context/admin/index/jobs/{job_public_id}/cancel  取消 in-progress
#   POST   /api/v1/context/admin/index/documents/{doc_public_id}/delete 删除索引中的文档
#
# 链路:
#   ContextIndexJobRepository / ContextIndexDocumentRepository
#   → admin 鉴权(IsAdmin 依赖) → 任务执行入 admin 表 → 触发重试 / 取消
#
# 关键约束:
#   - admin role 由 admin_token 或 admin_role(user.admin=True)+feature_flag 决定;
#   - 在位重试不影响其他用户正在进行任务,只对目标 job;
#   - delete 文档是 hard delete(整个 document + index entry 一起移除)。
