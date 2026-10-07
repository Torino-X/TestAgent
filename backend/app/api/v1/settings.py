"""Settings endpoints — model, knowledge base, upload config.

F015 — model endpoints now operate on the integer ``users.id`` (the
``UserProfile.internal_id`` already exposed by ``deps.get_current_user``)
rather than the JWT public_id.  Each user gets their own model config;
there is no longer a global default that all users share.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.agent_runtime._shared.narrative_governance.settings_service import (
    NarrativeSettingsService,
)
from app.core.response import success
from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import UserProfile
from app.schemas.settings import (
    CapabilityModelConfigRequest,
    KnowledgeBaseSettingsRequest,
    ModelSettingsRequest,
    NarrativeSettingsRequest,
    NarrativeSettingsResponse,
    UploadSettingsRequest,
)
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)
router = APIRouter()


async def _resolve_internal_id(session: AsyncSession, current_user: UserProfile) -> int:
    """Look up the integer users.id for the current JWT subject.

    ``UserProfile.id`` is the public_id (string); settings services
    now require the integer DB primary key.
    """
    result = await session.execute(
        select(User).where(User.public_id == current_user.id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        # Token references a deleted user — fall back to whatever
        # internal_id was injected by get_current_user (may be 0).
        return current_user.internal_id
    return user.id


# ── Model config ──────────────────────────────────────────────────────

@router.get("/model")
async def get_model_settings(
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.get_model_settings(internal_id)
    return success(result)


@router.put("/model")
async def update_model_settings(
    body: ModelSettingsRequest,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info(
        "更新模型配置 | 用户=%s | 模型=%s",
        current_user.username, body.model_name,
    )
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.update_model_settings(body.model_dump(), internal_id)
    return success(result)


@router.post("/model/test")
async def test_model_connection(
    body: ModelSettingsRequest | None = None,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Test LLM connectivity.

    F015 + UX: prefer the form values posted in ``body`` (so the user
    can "test before save").  When ``body`` is empty/absent we fall
    back to the user's saved config in the DB (existing behaviour).

    ``body`` is optional so older callers that POST without a body
    still get the DB-path semantics they expect.
    """
    logger.info(
        "测试模型连接 | 用户=%s | 来自body=%s",
        current_user.username, body is not None,
    )
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    logger.info(
        "测试模型连接 | internal_id=%s | username=%s",
        internal_id, current_user.username,
    )
    result = await service.test_model_connection(
        user_id=internal_id,
        override=body.model_dump() if body else None,
    )
    return success(result)


# ── CE-01: 多能力模型配置管理 ─────────────────────────────────────

@router.get("/model/capabilities")
async def list_model_capabilities(
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """返回各能力（chat/embedding/reranker/...）的用户配置。"""
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.list_capabilities(internal_id)
    return success(result)


@router.get("/model/capability/{capability_type}")
async def get_model_capability(
    capability_type: str,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """返回指定能力的用户 default 配置。"""
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.get_capability_config(capability_type, internal_id)
    return success(result)


@router.put("/model/capability/{capability_type}")
async def update_model_capability(
    capability_type: str,
    body: CapabilityModelConfigRequest,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """按能力 upsert 模型配置（多能力管理）。"""
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.update_capability_config(
        capability_type, body.model_dump(), internal_id
    )
    return success(result)


# ── Knowledge base config ─────────────────────────────────────────────

@router.get("/knowledge-base")
async def get_knowledge_base_settings(
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.get_knowledge_base_settings(internal_id)
    return success(result)


@router.put("/knowledge-base")
async def update_knowledge_base_settings(
    body: KnowledgeBaseSettingsRequest,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.update_knowledge_base_settings(body.model_dump(), internal_id)
    return success(result)


@router.post("/knowledge-base/test")
async def test_knowledge_base_connection(
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info("测试知识库连接 | 用户=%s", current_user.username)
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.test_knowledge_base_connection(internal_id)
    return success(result)


# ── Upload config ─────────────────────────────────────────────────────

@router.get("/upload")
async def get_upload_settings(
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.get_upload_settings(internal_id)
    return success(result)


@router.put("/upload")
async def update_upload_settings(
    body: UploadSettingsRequest,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = SettingsService(session)
    internal_id = await _resolve_internal_id(session, current_user)
    result = await service.update_upload_settings(body.model_dump(), internal_id)
    return success(result)


# ── Phase 2.9C narrative detail level ─────────────────────────────────────


@router.get("/narrative")
async def get_narrative_settings(
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Return current tool-card narrative display setting."""
    internal_id = await _resolve_internal_id(session, current_user)
    svc = NarrativeSettingsService(session)
    settings = await svc.get_user_settings(internal_id)
    payload = NarrativeSettingsResponse(
        enabled=settings.enabled,
        detail_level=settings.detail_level.value,
        detail_level_source=settings.detail_level_source,
    ).model_dump()
    return success(payload)


@router.patch("/narrative")
async def patch_narrative_settings(
    body: NarrativeSettingsRequest,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update tool-card narrative display setting."""
    internal_id = await _resolve_internal_id(session, current_user)
    svc = NarrativeSettingsService(session)
    settings = await svc.set_user_enabled(internal_id, body.enabled)
    payload = NarrativeSettingsResponse(
        enabled=settings.enabled,
        detail_level=settings.detail_level.value,
        detail_level_source=settings.detail_level_source,
    ).model_dump()
    return success(payload)


# 路由清单(Settings 路由,F015 per-user 配置):
#   GET  /api/v1/settings/models/{user_id}             读 model config
#   PUT  /api/v1/settings/models/{user_id}             写 model config
#   POST /api/v1/settings/models/{user_id}/test        探测 model 端点
#   GET  /api/v1/settings/knowledge                    读 KB config
#   PUT  /api/v1/settings/knowledge                    写 KB config
#   GET  /api/v1/settings/upload                        读上传参数
#   PUT  /api/v1/settings/upload                        写上传参数
#   GET  /api/v1/settings/image-understanding          读图片理解 config
#   PUT  /api/v1/settings/image-understanding          写图片理解 config
#
# 链路:
#   各路由 → 对应 ConfigService → 落库 user_per_type_config 表
#
# 关键约束:
#   - user_id 必须是当前 user(否则 403);
#   - F015 后 model config 是 per-user DB-only,无 env fallback;
#   - api_key 走 crypto.encrypt_api_key(...),绝不写日志;
#   - test 路由会真正调用上游 API,慢请求允许,加超时。
