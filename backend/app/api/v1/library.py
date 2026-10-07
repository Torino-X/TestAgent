"""Library endpoints for visible items, recycle bin, and item actions."""

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, Path, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile
from app.schemas.library import LibraryRenameRequest
from app.services.library_service import LibraryService
from app.services.document_preview_service import DocumentPreviewService

router = APIRouter()
LibraryCategory = Literal["all", "image", "file"]
LibraryScope = Literal["active", "deleted"]
LibrarySourceFilter = Literal["all", "upload", "generated"]
LibraryFileType = Literal["all", "image", "document", "spreadsheet", "presentation", "pdf"]


async def _schedule_document_preview_after_commit(
    session: AsyncSession, background_tasks: BackgroundTasks, item_id: str, user_id: int
) -> None:
    """Commit a queue transition before a fresh-session worker can claim it.

    Response background tasks run before yielded request dependencies are torn
    down.  Scheduling first would make the worker contend with this request's
    uncommitted ``document_previews`` row.
    """
    await session.commit()
    background_tasks.add_task(DocumentPreviewService.build_preview_job, item_id, user_id)


def _content_disposition(filename: str, disposition: str = "attachment") -> str:
    try:
        filename.encode("latin-1")
        return f'{disposition}; filename="{filename}"'
    except UnicodeEncodeError:
        return f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}"


@router.post("/upload")
async def upload_library_file(
    background_tasks: BackgroundTasks,
    file: UploadFile,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    result = await LibraryService(session).upload_file(
        await file.read(), file.filename or "unknown", current_user.internal_id, file.content_type
    )
    if str(result.get("extension") or "").lower() == ".docx":
        preview_service = DocumentPreviewService(session)
        await preview_service.get_preview(str(result["id"]), current_user.internal_id)
        if preview_service.preview_job_required:
            await _schedule_document_preview_after_commit(
                session, background_tasks, str(result["id"]), current_user.internal_id
            )
    return success(result)


@router.get("/items")
async def list_library_items(
    category: LibraryCategory = Query("all"),
    q: str = Query("", max_length=120),
    scope: LibraryScope = Query("active"),
    source: LibrarySourceFilter = Query("all"),
    file_type: LibraryFileType = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    items, total = await LibraryService(session).list_items(
        current_user.internal_id,
        category=category,
        query=q,
        scope=scope,
        source=source,
        file_type=file_type,
        page=page,
        page_size=page_size,
    )
    return success({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": page * page_size < total,
    })


@router.get("/items/{item_id}")
async def get_library_item(
    item_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    result = await LibraryService(session).get_item(item_id, current_user.internal_id)
    return success(result)


@router.get("/items/{item_id}/download")
async def download_library_item(
    item_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    stream, filename, media_type = await LibraryService(session).get_download_stream(
        item_id, current_user.internal_id
    )
    return StreamingResponse(
        stream(), media_type=media_type, headers={"Content-Disposition": _content_disposition(filename)}
    )


@router.get("/items/{item_id}/preview")
async def get_library_item_preview(
    background_tasks: BackgroundTasks,
    item_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = DocumentPreviewService(session)
    result = await service.get_preview(item_id, current_user.internal_id)
    if service.preview_job_required:
        await _schedule_document_preview_after_commit(session, background_tasks, item_id, current_user.internal_id)
    return success(result)


@router.get("/items/{item_id}/preview/pdf")
async def get_library_item_preview_pdf(
    item_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    stream, filename = await DocumentPreviewService(session).get_pdf_stream(item_id, current_user.internal_id)
    return StreamingResponse(
        stream,
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(filename, "inline")},
    )


@router.patch("/items/{item_id}")
async def rename_library_item(
    body: LibraryRenameRequest,
    item_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    result = await LibraryService(session).rename(item_id, body.name, current_user.internal_id)
    return success(result)


@router.delete("/items/{item_id}")
async def delete_library_item(
    item_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    await LibraryService(session).soft_delete(item_id, current_user.internal_id)
    return success(None, "文件已移至最近删除")


@router.post("/items/{item_id}/restore")
async def restore_library_item(
    item_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    result = await LibraryService(session).restore(item_id, current_user.internal_id)
    return success(result)


@router.delete("/items/{item_id}/permanent")
async def permanently_delete_library_item(
    item_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    await LibraryService(session).permanent_delete(item_id, current_user.internal_id)
    return success(None, "文件已永久删除")
