"""Artifact endpoints — detail, download."""

import logging
import traceback

from fastapi import APIRouter, Depends, Path
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from urllib.parse import quote

from app.api.deps import get_current_user
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile
from app.services.artifact_service import ArtifactService

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_content_disposition(filename: str) -> str:
    """Build an RFC 6266/8187 Content-Disposition header safe for non-ASCII filenames."""
    try:
        filename.encode("latin-1")
        return f'attachment; filename="{filename}"'
    except UnicodeEncodeError:
        encoded = quote(filename, safe="")
        return f"attachment; filename*=UTF-8''{encoded}"


@router.get("/{artifact_id}")
async def get_artifact(
    artifact_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = ArtifactService(session)
    result = await service.get_artifact(artifact_id, current_user.internal_id)
    return success(result)


@router.get("/{artifact_id}/download")
async def download_artifact(
    artifact_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info("下载制品 | 制品=%s | 用户=%s", artifact_id, current_user.username)
    try:
        service = ArtifactService(session)
        stream, filename, media_type = await service.get_download_stream(
            artifact_id, current_user.internal_id
        )
        return StreamingResponse(
            stream(),
            media_type=media_type,
            headers={
                "Content-Disposition": _safe_content_disposition(filename),
            },
        )
    except Exception:
        logger.exception("下载制品失败 | 制品=%s", artifact_id)
        raise


# 路由清单(Artifact API):
#   GET    /api/v1/artifacts/{public_id}            详情(脱敏 storage_path)
#   GET    /api/v1/artifacts/{public_id}/download   流式下载
#
# 链路:
#   → ArtifactService.get_for_download(public_id, user_id)
#     → owner ACL 校验 → local_storage.open_bytes
#     → StreamingResponse 流式返回
#
# 关键约束:
#   - 详情绝不返回 storage_path(白名单字段);
#   - 下载需 owner 校验,跨用户 404;
#   - 大文件必须 StreamingResponse,不要一次性 read;
#   - WordExportTool 落库后的 artifact 才允许前端下载(中途 draft 不能)。
