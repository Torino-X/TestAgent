"""Context Index API：文档索引提交 / 状态 / job / reindex / delete。

CE-03 WP-8：owner-scoped（get_current_user + get_db）。API ACL 由
user 内部 id 强制；跨用户访问返回 404（不泄露存在性）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.schemas.auth import UserProfile

router = APIRouter()


class IndexSubmitRequest(BaseModel):
    file_public_id: str


class IndexStatusResponse(BaseModel):
    document_public_id: str
    status: str
    lexical_index_status: str
    vector_index_status: str
    source_public_id: str | None = None
    source_version: str | None = None
    indexed_at: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@router.post("/documents/submit", response_model=dict)
async def submit_document(
    body: IndexSubmitRequest,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.context_engine.indexing.document_service import DocumentIndexError, IndexDocumentService

    try:
        result = await IndexDocumentService(session).submit_uploaded_file(
            user_id=current.internal_id,
            file_public_id=body.file_public_id,
        )
        return result
    except DocumentIndexError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "detail": exc.detail})


@router.get("/documents/{document_public_id}", response_model=IndexStatusResponse)
async def get_document_status(
    document_public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from app.repositories.context_engine_repositories import ContextIndexDocumentRepository

    repo = ContextIndexDocumentRepository(session)
    doc = await repo.get_by_public_id(document_public_id, current.internal_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={"code": "context.index.not_found", "detail": "文档不存在"})
    return IndexStatusResponse(
        document_public_id=doc.public_id,
        status=doc.status,
        lexical_index_status=doc.lexical_index_status,
        vector_index_status=doc.vector_index_status,
        source_public_id=doc.source_public_id,
        source_version=doc.source_version,
        indexed_at=doc.indexed_at.isoformat() if doc.indexed_at else None,
        error_code=doc.error_code if hasattr(doc, "error_code") else None,
        error_message=None,
    )


@router.get("/documents")
async def list_documents(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    limit: int = 50,
):
    from sqlalchemy import select
    from app.models.context_engine import ContextIndexDocument

    stmt = (
        select(ContextIndexDocument)
        .where(
            ContextIndexDocument.user_id == current.internal_id,
            ContextIndexDocument.deleted_at.is_(None),
        )
        .order_by(ContextIndexDocument.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "document_public_id": d.public_id,
            "status": d.status,
            "lexical_index_status": d.lexical_index_status,
            "vector_index_status": d.vector_index_status,
            "source_public_id": d.source_public_id,
            "source_version": d.source_version,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in rows
    ]


@router.get("/jobs")
async def list_jobs(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    limit: int = 50,
):
    from sqlalchemy import select
    from app.models.context_engine import ContextIndexJob

    stmt = (
        select(ContextIndexJob)
        .where(ContextIndexJob.user_id == current.internal_id)
        .order_by(ContextIndexJob.created_at.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "job_public_id": j.public_id,
            "operation": j.operation,
            "status": j.status,
            "attempt": j.attempt,
            "error_code": j.error_code,
            "error_message": j.error_message,
        }
        for j in rows
    ]


@router.post("/documents/{document_public_id}/delete", response_model=dict)
async def delete_document(
    document_public_id: str,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timezone
    from sqlalchemy import update
    from app.models.context_engine import ContextIndexDocument
    from app.repositories.context_engine_repositories import ContextIndexDocumentRepository

    repo = ContextIndexDocumentRepository(session)
    doc = await repo.get_by_public_id(document_public_id, current.internal_id)
    if doc is None:
        raise HTTPException(status_code=404, detail={"code": "context.index.not_found", "detail": "文档不存在"})
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await session.execute(
        update(ContextIndexDocument)
        .where(ContextIndexDocument.public_id == document_public_id, ContextIndexDocument.user_id == current.internal_id)
        .values(status="superseded", deleted_at=now)
    )
    await session.commit()
    return {"document_public_id": document_public_id, "status": "superseded"}


# 路由清单(CE-03 / CE-05 owner-scoped 索引 CRUD):
#   POST /api/v1/context/index/documents                上传 + 索引入库
#   GET  /api/v1/context/index/documents                列出
#   GET  /api/v1/context/index/documents/{public_id}   单条
#   POST /api/v1/context/index/documents/{public_id}/reindex
#   POST /api/v1/context/index/documents/{public_id}/delete
#   GET  /api/v1/context/index/jobs/{job_public_id}    任务状态
#
# 链路:
#   ContextIndexService.submit_document(...)
#     → DocumentService 落 disk → IndexWorker 排队 →
#   → ContextIndexJobRepository.upsert(job row, status='queued') →
#   → Worker 后台 chunk + embedding → status='completed'
#
# 关键约束:
#   - 跨用户访问返回 404(不泄露存在性),按 CE-03 强 owner-scope;
#   - 上传 docx / pdf 走通用 DocumentReader,然后 chunking by IndexWorker;
#   - job 状态:`queued` → `running` → `completed` / `failed`,前端可轮询;
#   - 删除是 hard delete:document row + 所有 chunks + embedding + index entries。
