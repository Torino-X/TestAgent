"""Authenticated REST API for the template marketplace and My Templates."""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Path, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.exceptions import ValidationError
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile
from app.schemas.template import TemplateUseRequest
from app.services.template_market_service import TemplateMarketService
from app.services.template_cover_service import TemplateCoverService, page_needs_cover_backfill
from app.services.template_preview_service import TemplatePreviewService
from app.services.template_service import TemplateService
from app.services.template_use_service import TemplateUseService

router = APIRouter()


def _content_disposition(filename: str, disposition: str = "attachment") -> str:
    try:
        filename.encode("latin-1")
        return f'{disposition}; filename="{filename}"'
    except UnicodeEncodeError:
        return f"{disposition}; filename*=UTF-8''{quote(filename, safe='')}"


def _parse_tags(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValidationError("模板标签必须是 JSON 字符串数组") from exc
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError("模板标签必须是 JSON 字符串数组")
    return value


@router.get("/market")
async def list_market_templates(
    background_tasks: BackgroundTasks,
    q: str = Query(default="", max_length=120),
    category: str = Query(default="all", max_length=64),
    sort: str = Query(default="newest", max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await TemplateMarketService(session).list_market(
        current.internal_id,
        query=q,
        category=category,
        sort=sort,
        page=page,
        page_size=page_size,
    )
    if page_needs_cover_backfill(data):
        background_tasks.add_task(TemplateCoverService.backfill_missing)
    return success(data)


@router.get("/mine")
async def list_my_templates(
    background_tasks: BackgroundTasks,
    q: str = Query(default="", max_length=120),
    category: str = Query(default="all", max_length=64),
    source_type: str = Query(default="all", max_length=32),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await TemplateMarketService(session).list_mine(
        current.internal_id,
        query=q,
        category=category,
        source_type=source_type,
        page=page,
        page_size=page_size,
    )
    if page_needs_cover_backfill(data):
        background_tasks.add_task(TemplateCoverService.backfill_missing)
    return success(data)


@router.post("/mine")
async def upload_template(
    file: UploadFile = File(...),
    name: str = Form(...),
    category_code: str = Form(...),
    description: str = Form(default=""),
    tags_json: str = Form(default="[]"),
    publish: bool = Form(default=False),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await TemplateService(session).create_template(
        content=await file.read(),
        file_name=file.filename or "template",
        user_id=current.internal_id,
        name=name,
        category_code=category_code,
        description=description,
        tags=_parse_tags(tags_json),
        publish=publish,
        content_type=file.content_type,
    )
    await session.commit()
    return success(data, "模板已上传")


@router.get("/market/{template_id}/preview")
async def preview_market_template(
    background_tasks: BackgroundTasks,
    template_id: str = Path(...),
    _current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await TemplatePreviewService(session).get_preview(template_id)
    if data.get("preview_status") == "queued":
        background_tasks.add_task(TemplateCoverService.backfill_missing)
    return success(data)


@router.get("/market/{template_id}/preview/pdf")
async def preview_market_template_pdf(
    template_id: str = Path(...),
    _current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    stream, filename = await TemplatePreviewService(session).get_pdf_stream(template_id)
    return StreamingResponse(
        stream,
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(filename, "inline")},
    )


@router.post("/market/{template_id}/save")
async def save_market_template(
    template_id: str = Path(...),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await TemplateMarketService(session).save(template_id, current.internal_id)
    await session.commit()
    return success(data, "模板已保存")


@router.post("/mine/{user_template_id}/publish")
async def publish_template(
    user_template_id: str = Path(...),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await TemplateService(session).publish(user_template_id, current.internal_id)
    await session.commit()
    return success(data, "模板已发布")


@router.post("/mine/{user_template_id}/unpublish")
async def unpublish_template(
    user_template_id: str = Path(...),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await TemplateService(session).unpublish(user_template_id, current.internal_id)
    await session.commit()
    return success(data, "模板已取消发布")


@router.delete("/mine/{user_template_id}")
async def remove_template(
    user_template_id: str = Path(...),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    await TemplateService(session).remove(user_template_id, current.internal_id)
    await session.commit()
    return success(None, "模板已移除")


@router.get("/mine/{user_template_id}/download")
async def download_template(
    user_template_id: str = Path(...),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    stream, filename, media_type = await TemplateService(session).get_download_stream(
        user_template_id, current.internal_id
    )
    return StreamingResponse(
        stream,
        media_type=media_type,
        headers={"Content-Disposition": _content_disposition(filename)},
    )


@router.post("/mine/{user_template_id}/use")
async def use_template(
    body: TemplateUseRequest,
    user_template_id: str = Path(...),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await TemplateUseService(session).use(
        user_id=current.internal_id,
        user_template_public_id=user_template_id,
        conversation_public_id=body.conversation_id,
    )
    await session.commit()
    return success(data, "模板已加入会话")


@router.get("/mine/{user_template_id}/preview")
async def preview_my_template(
    background_tasks: BackgroundTasks,
    user_template_id: str = Path(...),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await TemplatePreviewService(session).get_user_preview(
        user_template_id, current.internal_id
    )
    if data.get("preview_status") == "queued":
        background_tasks.add_task(TemplateCoverService.backfill_missing)
    return success(data)


@router.get("/mine/{user_template_id}/preview/pdf")
async def preview_my_template_pdf(
    user_template_id: str = Path(...),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    stream, filename = await TemplatePreviewService(session).get_user_pdf_stream(
        user_template_id, current.internal_id
    )
    return StreamingResponse(
        stream,
        media_type="application/pdf",
        headers={"Content-Disposition": _content_disposition(filename, "inline")},
    )
