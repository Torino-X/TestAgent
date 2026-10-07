"""F020 — image-understanding configuration endpoints.

Routes mounted under ``/api/v1/image-understanding``:

  * ``GET  /config``            — return the current user's masked config
  * ``POST /config``            — save (upsert) the current user's config
  * ``POST /config/test``       — probe the configured vision endpoint

Frontend never receives the plaintext API key; we mirror the
KnowledgeConfig endpoints' shape and conventions.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile
from app.schemas.image_understanding import (
    ImageUnderstandingConfigPayload,
    ImageUnderstandingConfigPublic,
    ImageUnderstandingConfigSaveResult,
    ImageUnderstandingTestConnectionResult,
)
from app.services.image_understanding_config_service import (
    ImageUnderstandingConfigService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/config", summary="获取图片理解配置")
async def get_config(
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info("读取图片理解配置 | 用户=%s", current.username)
    svc = ImageUnderstandingConfigService(session)
    data = await svc.get_config(current.internal_id)
    return success(ImageUnderstandingConfigPublic(**data).model_dump())


@router.post("/config", summary="保存图片理解配置")
async def save_config(
    payload: ImageUnderstandingConfigPayload,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info(
        "保存图片理解配置 | 用户=%s | model=%s | enable=%s",
        current.username, payload.model_name, payload.enable_in_doc_parsing,
    )
    svc = ImageUnderstandingConfigService(session)
    result = await svc.save_config(
        current.internal_id, payload.model_dump(exclude_none=True)
    )
    return success(ImageUnderstandingConfigSaveResult(**result).model_dump())


@router.post("/config/test", summary="测试图片理解连接")
async def test_connection(
    request: Request,
    payload: ImageUnderstandingConfigPayload | None = None,
    current: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Probe the configured vision endpoint with a 1x1 PNG.

    Mirrors the main-model ``/settings/model/test`` semantics so the
    Settings page can offer the same "test before save" UX:
      * If ``payload`` carries ``api_base_url`` or ``model_name`` we
        build a transient provider from those values, falling back to
        the DB-saved key when ``api_key`` is empty (the frontend masks
        the value and cannot tell those two cases apart).
      * When ``payload`` is empty/absent we use the saved config in
        the DB exclusively (legacy path).
    """
    logger.info(
        "测试图片理解连接 | 用户=%s | from_body=%s",
        current.username, payload is not None,
    )
    svc = ImageUnderstandingConfigService(
        session,
        context_llm_invoker=getattr(request.app.state, "context_llm_bridge", None),
    )
    override = payload.model_dump(exclude_none=True) if payload else None
    result = await svc.test_connection(
        current.internal_id, override=override
    )
    return success(
        ImageUnderstandingTestConnectionResult(
            success=bool(result.get("success")),
            latency_ms=int(result.get("latency_ms") or 0),
            status=result.get("status") or "failed",
            message=result.get("message") or "",
            error_code=result.get("error_code"),
            tested_at=result.get("tested_at") or "",
        ).model_dump()
    )

# 路由清单(F020 图片理解配置):
#   GET    /api/v1/image-understanding/config             返回当前 user 脱敏配置
#   POST   /api/v1/image-understanding/config             upsert
#   POST   /api/v1/image-understanding/config/test        探测配置好的 vision 端点
#
# 链路:
#   → ImageUnderstandingConfigService.get_for_user / upsert
#   → test → VisionService.understand(...) 用 1x1 dummy 测连通
#
# 关键约束:
#   - config 永远返回脱敏(api_key masked);
#   - test 路由超时 30s;
#   - 当前 user 修改自己的配置,跨用户 404。
