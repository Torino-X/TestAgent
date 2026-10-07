"""Project workspace API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Path, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile
from app.schemas.project import (
    ConversationProjectMove,
    ProjectConversationCreate,
    ProjectCreate,
    ProjectInstructionsUpdate,
    ProjectSourceAttach,
    ProjectSourceUpdate,
    ProjectUpdate,
)
from app.services.project_service import ProjectService
from app.services.project_source_service import ProjectSourceService


router = APIRouter()
conversation_router = APIRouter()


@router.post("")
async def create_project(body: ProjectCreate, current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    data = await ProjectService(session).create(
        current.internal_id,
        name=body.name,
        description=body.description,
        memory_mode=body.memory_mode,
    )
    return success(data, "项目已创建")


@router.get("")
async def list_projects(
    q: str = Query(default="", max_length=255),
    scope: str = Query(default="all"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    page_size_camel: int | None = Query(default=None, alias="pageSize", ge=1, le=100),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    return success(await ProjectService(session).list_projects(
        current.internal_id,
        query=q,
        scope=scope,
        page=page,
        page_size=page_size_camel or page_size,
    ))


@router.get("/{project_id}")
async def get_project(project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    return success(await ProjectService(session).get_detail(project_id, current.internal_id))


@router.patch("/{project_id}")
async def update_project(body: ProjectUpdate, project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    values = body.model_dump(exclude_unset=True, by_alias=False)
    return success(await ProjectService(session).update(project_id, current.internal_id, **values), "项目已更新")


@router.delete("/{project_id}")
async def delete_project(project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    await ProjectService(session).delete(project_id, current.internal_id)
    return success(None, "项目已删除")


@router.post("/{project_id}/pin")
async def pin_project(project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    return success(await ProjectService(session).set_pinned(project_id, current.internal_id, True))


@router.delete("/{project_id}/pin")
async def unpin_project(project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    return success(await ProjectService(session).set_pinned(project_id, current.internal_id, False))


@router.get("/{project_id}/conversations")
async def list_project_conversations(project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    items = await ProjectService(session).list_conversations(project_id, current.internal_id)
    return success({"items": items, "total": len(items)})


@router.post("/{project_id}/conversations")
async def create_project_conversation(body: ProjectConversationCreate, project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    return success(await ProjectService(session).create_conversation(project_id, current.internal_id, body.title), "项目对话已创建")


@conversation_router.post("/conversations/{conversation_id}/project")
async def move_conversation_to_project(body: ConversationProjectMove, conversation_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    return success(await ProjectService(session).move_conversation(conversation_id, body.project_id, current.internal_id), "会话已移入项目")


@conversation_router.delete("/conversations/{conversation_id}/project")
async def remove_conversation_from_project(conversation_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    await ProjectService(session).remove_conversation(conversation_id, current.internal_id)
    return success(None, "会话已移出项目")


@router.get("/{project_id}/sources")
async def list_project_sources(project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    items = await ProjectSourceService(session).list_sources(project_id, current.internal_id)
    return success({"items": items, "total": len(items)})


@router.post("/{project_id}/sources/upload")
async def upload_project_source(
    project_id: str = Path(...),
    file: UploadFile = File(...),
    source_role: str = Form(default="other"),
    source_role_camel: str | None = Form(default=None, alias="sourceRole"),
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    data = await ProjectSourceService(session).upload(
        project_id,
        current.internal_id,
        await file.read(),
        file.filename or "upload.bin",
        file.content_type,
        source_role_camel or source_role,
    )
    return success(data, "项目来源已上传")


@router.post("/{project_id}/sources/attach")
async def attach_project_source(body: ProjectSourceAttach, project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    return success(await ProjectSourceService(session).attach(
        project_id, current.internal_id, body.file_id, source_role=body.source_role,
    ), "项目来源已关联")


@router.patch("/{project_id}/sources/{source_id}")
async def update_project_source(body: ProjectSourceUpdate, project_id: str = Path(...), source_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    return success(await ProjectSourceService(session).update(
        project_id,
        source_id,
        current.internal_id,
        source_role=body.source_role,
        is_current=body.is_current,
    ))


@router.delete("/{project_id}/sources/{source_id}")
async def remove_project_source(project_id: str = Path(...), source_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    await ProjectSourceService(session).remove(project_id, source_id, current.internal_id)
    return success(None, "项目来源已移除")


@router.get("/{project_id}/artifacts")
async def list_project_artifacts(project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    detail = await ProjectService(session).get_detail(project_id, current.internal_id)
    items = detail["artifacts"]
    return success({"items": items, "total": len(items)})


@router.get("/{project_id}/instructions")
async def get_project_instructions(project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    instructions = await ProjectService(session).get_instructions(project_id, current.internal_id)
    return success({"instructions": instructions})


@router.put("/{project_id}/instructions")
async def update_project_instructions(body: ProjectInstructionsUpdate, project_id: str = Path(...), current: UserProfile = Depends(get_current_user), session: AsyncSession = Depends(get_db)):
    instructions = await ProjectService(session).update_instructions(project_id, current.internal_id, body.instructions)
    return success({"instructions": instructions}, "项目指令已更新")
