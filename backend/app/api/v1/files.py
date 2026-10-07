"""File endpoints — upload, list, confirm type, delete."""

import logging

from fastapi import APIRouter, Depends, Form, Path, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile
from app.schemas.file import FileConfirmRequest
from app.services.file_service import FileService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/files/upload")
async def upload_file(
    file: UploadFile,
    request: Request,
    conversation_id: str = Form(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    content = await file.read()
    file_name = file.filename or "unknown"
    logger.info(
        "文件上传开始 | 用户=%s | 会话=%s | 文件名=%s | 大小=%d字节",
        current_user.username, conversation_id, file_name, len(content),
    )
    service = FileService(
        session,
        context_llm_invoker=getattr(request.app.state, "context_llm_bridge", None),
    )
    result = await service.upload(
        content=content,
        file_name=file_name,
        user_internal_id=current_user.internal_id,
        conv_public_id=conversation_id,
        content_type=file.content_type,
    )
    await session.commit()
    logger.info(
        "文件上传完成 | 用户=%s | 会话=%s | 文件名=%s",
        current_user.username, conversation_id, file_name,
    )
    return success(result)


@router.get("/conversations/{conversation_id}/files")
async def list_files(
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = FileService(session)
    files, total = await service.list_files(current_user.internal_id, conversation_id)
    return success({"files": files, "total": total})


@router.post("/files/{file_id}/confirm-type")
async def confirm_file_type(
    body: FileConfirmRequest,
    file_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info(
        "确认文件类型 | 文件=%s | 用户=%s | 类型=%s",
        file_id, current_user.username, body.file_type,
    )
    service = FileService(session)
    result = await service.confirm_type(
        file_id,
        body.file_type,
        user_internal_id=current_user.internal_id,
    )
    return success(result)


@router.delete("/files/{file_id}")
async def delete_file(
    file_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.warning(
        "删除文件 | 文件=%s | 用户=%s",
        file_id, current_user.username,
    )
    service = FileService(session)
    await service.delete(file_id, user_internal_id=current_user.internal_id)
    return success(None, "文件已删除")


# 路由清单(File API):
#   POST   /api/v1/files                上传(.docx/.pdf/.md/.txt/.png/...)
#   GET    /api/v1/files                列出当前 user 的文件
#   GET    /api/v1/files/{public_id}   单条元数据
#   POST   /api/v1/files/{public_id}/type 确认文件类型(触发 file_understanding)
#   DELETE /api/v1/files/{public_id}   删除(同时清理 message_attachment 引用)
#
# 链路:
#   上传:UploadFile → FileService.save_upload(...) → local_storage.save_bytes +
#   UploadedFileRepository.upsert
#   类型确认:FileService.confirm_type(...) → FileUnderstandingService.understand_uploaded_file(...)
#
# 关键约束:
#   - 上传文件先写到 tmp,落库后才 move 到正式路径(AtomicFileWriter);
#   - 删除必须 owner 校验,跨用户返回 404;
#   - 文件能力必须 file_capability_registry 已知(image / docx / ...),
#     否则不能用作 task input。
