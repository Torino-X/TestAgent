"""Conversation endpoints — create, list, detail, update, delete."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.messages import _build_llm_client
from app.core.response import success
from app.db.session import get_db
from app.schemas.auth import UserProfile
from app.schemas.conversation import ConversationCreate, ConversationUpdate
from app.services.conversation_service import ConversationService

logger = logging.getLogger(__name__)
router = APIRouter()


class _ContextSettingsRequest(BaseModel):
    knowledge_mode: str | None = None


class _EvidencePinRequest(BaseModel):
    pinned: bool


@router.post("")
async def create_conversation(
    body: ConversationCreate,
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info(
        "创建会话 | 用户=%s | 标题长度=%d",
        current_user.username, len(body.title or ""),
    )
    service = ConversationService(session)
    result = await service.create(body.title, current_user.internal_id)
    return success(result)


@router.get("")
async def list_conversations(
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = ConversationService(session)
    conversations, total = await service.list_conversations(current_user.internal_id)
    return success({"conversations": conversations, "total": total})


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = ConversationService(session)
    result = await service.get_detail(conversation_id, current_user.internal_id)
    # Phase 2.9A.26+: include all tasks for this conversation, not just
    # the latest.  Front-end uses the trigger_message_id to anchor each
    # task run block in the timeline.  Detail endpoint stays backward
    # compatible: ``latestTask`` is preserved when present.
    from app.services.agent_task_service import AgentTaskService
    task_service = AgentTaskService(session)
    tasks = await task_service.list_tasks_for_conversation(
        current_user.internal_id, conversation_id
    )
    if isinstance(result, dict):
        result.setdefault("tasks", tasks)
    return success(result)


@router.get("/{conversation_id}/context-settings")
async def get_conversation_context_settings(
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """CE-05 WP-5: Conversation Context Settings 只读。

    返回 memory_mode / context_workspace_key / context_engine_version /
    policy_version。仅允许 memory_mode 可写（PATCH /memory-mode）。
    """
    from sqlalchemy import select

    from app.models.conversation import Conversation
    from app.context_engine.security.policy_version import CURRENT_SECURITY_POLICY_VERSION

    row = (
        await session.execute(
            select(Conversation).where(
                Conversation.public_id == conversation_id,
                Conversation.user_id == current_user.internal_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        from app.core.response import error

        raise HTTPException(
            status_code=404,
            detail=error(40401, "会话不存在或不属于当前用户"),
        )
    return success({
        "memory_mode": getattr(row, "context_memory_mode", "inherit"),
        "knowledge_mode": getattr(row, "knowledge_mode", "AUTO") or "AUTO",
        "knowledge_mode_persistence": "server",
        "knowledge_mode_migration_required": False,
        "context_workspace_key": getattr(row, "context_workspace_key", None),
        "context_engine_version": getattr(row, "context_engine_version", None),
        "policy_version": CURRENT_SECURITY_POLICY_VERSION,
    })


@router.patch("/{conversation_id}/context-settings")
async def update_conversation_context_settings(
    body: _ContextSettingsRequest,
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Persist conversation-scoped knowledge mode."""
    from sqlalchemy import select

    from app.context_engine.security.policy_version import CURRENT_SECURITY_POLICY_VERSION
    from app.models.conversation import Conversation

    row = (
        await session.execute(
            select(Conversation).where(
                Conversation.public_id == conversation_id,
                Conversation.user_id == current_user.internal_id,
                Conversation.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        from app.core.response import error

        raise HTTPException(
            status_code=404,
            detail=error(40401, "会话不存在或不属于当前用户"),
        )
    mode = "MAAS_STRICT" if body.knowledge_mode == "MAAS_STRICT" else "AUTO"
    row.knowledge_mode = mode
    await session.commit()
    return success({
        "memory_mode": getattr(row, "context_memory_mode", "inherit"),
        "knowledge_mode": mode,
        "knowledge_mode_persistence": "server",
        "knowledge_mode_migration_required": False,
        "context_workspace_key": getattr(row, "context_workspace_key", None),
        "context_engine_version": getattr(row, "context_engine_version", None),
        "policy_version": CURRENT_SECURITY_POLICY_VERSION,
    })


@router.get("/{conversation_id}/context-evidence")
async def list_conversation_context_evidence(
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List the bounded, traceable project-evidence working set."""
    from sqlalchemy import select
    from app.models.context_engine import ConversationEvidenceCache
    from app.models.conversation import Conversation

    conversation = (await session.execute(select(Conversation).where(
        Conversation.public_id == conversation_id,
        Conversation.user_id == current_user.internal_id,
        Conversation.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前用户")
    rows = list((await session.execute(
        select(ConversationEvidenceCache)
        .where(
            ConversationEvidenceCache.user_id == current_user.internal_id,
            ConversationEvidenceCache.conversation_id == conversation.id,
            ConversationEvidenceCache.status == "active",
        )
        .order_by(ConversationEvidenceCache.pinned.desc(), ConversationEvidenceCache.last_used_at.desc())
    )).scalars())
    return success({"items": [{
        "id": row.public_id,
        "title": row.title,
        "section": row.section_path,
        "source_type": row.source_type,
        "source_id": row.source_public_id,
        "source_version": row.source_version,
        "tokens": row.estimated_tokens,
        "pinned": row.pinned,
        "locator": row.locator_json,
    } for row in rows]})


@router.patch("/{conversation_id}/context-evidence/{evidence_id}")
async def pin_conversation_context_evidence(
    body: _EvidencePinRequest,
    conversation_id: str = Path(...),
    evidence_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Pin/unpin a selected evidence chunk; pinned rows survive normal eviction."""
    from sqlalchemy import select
    from app.models.conversation import Conversation
    from app.services.conversation_evidence_cache_service import ConversationEvidenceCacheService

    conversation = (await session.execute(select(Conversation).where(
        Conversation.public_id == conversation_id,
        Conversation.user_id == current_user.internal_id,
        Conversation.deleted_at.is_(None),
    ))).scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="会话不存在或不属于当前用户")
    row = await ConversationEvidenceCacheService(session).set_pinned(
        user_id=current_user.internal_id,
        conversation_id=int(conversation.id),
        evidence_public_id=evidence_id,
        pinned=body.pinned,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="上下文证据不存在")
    await session.commit()
    return success({"id": row.public_id, "pinned": row.pinned})


@router.get("/{conversation_id}/tasks")
async def list_conversation_tasks(
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Phase 2.9A.26+: list all tasks for a conversation.

    Used by the conversation timeline to render multiple task run blocks
    instead of only the latest one.  Each entry includes
    ``trigger_message_id`` so the front-end can anchor the run block
    to the user message that created it.
    """
    from app.services.agent_task_service import AgentTaskService
    task_service = AgentTaskService(session)
    tasks = await task_service.list_tasks_for_conversation(
        current_user.internal_id, conversation_id
    )
    return success({"tasks": tasks, "total": len(tasks)})


@router.patch("/{conversation_id}")
async def update_conversation(
    body: ConversationUpdate,
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info(
        "更新会话标题 | 会话=%s | 用户=%s",
        conversation_id, current_user.username,
    )
    service = ConversationService(session)
    result = await service.update_title(conversation_id, body.title or "")
    return success(result)


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.warning(
        "删除会话 | 会话=%s | 用户=%s",
        conversation_id, current_user.username,
    )
    service = ConversationService(session)
    await service.delete(conversation_id)
    return success(None, "会话已删除")


class _MemoryModeRequest(BaseModel):
    memory_mode: str


class _ContextUsagePreviewRequest(BaseModel):
    """Draft-only request; it is not stored as a conversation message."""

    content: str = Field(default="", max_length=200_000)
    attached_file_ids: list[str] = Field(default_factory=list, max_length=50)


@router.get("/{conversation_id}/context-usage")
async def get_conversation_context_usage(
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
):
    """WP-BE-02: Conversation Context Usage（owner-scope）。

    返回最近一次成功 Context Assembly 的 usage + 5 分类 breakdown。
    无 snapshot → available=false（不伪造 0%）。
    unknown window → percent=null。
    仅 Conversation Owner 可访问；非属主 404。
    """
    from app.services.context_usage_service import (
        ContextUsageService,
        ConversationNotFound,
    )

    service = ContextUsageService(session)
    try:
        data = await service.get_context_usage(
            conversation_public_id=conversation_id,
            user_internal_id=current_user.internal_id,
            context_engine=getattr(request.app.state, "context_engine", None),
        )
    except ConversationNotFound:
        from app.core.response import error

        # 项目统一 envelope 的 code 必须为 int（error()/ApiResponse.code 校验 int）。
        # 会话不存在或非属主 → 404 + 40401（防枚举；internal identifier:
        # context.compact.conversation_not_found / conversation.not_found）。
        raise HTTPException(
            status_code=404,
            detail=error(40401, "会话不存在或不属于当前用户"),
        )
    return success(data.to_dict())


@router.post("/{conversation_id}/context-usage/preview")
async def preview_conversation_context_usage(
    body: _ContextUsagePreviewRequest,
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
):
    """Read-only, draft-aware next ``chat.reply`` context estimate."""
    from app.services.context_usage_service import (
        ContextUsageService,
        ConversationNotFound,
    )

    service = ContextUsageService(session)
    try:
        data = await service.get_context_usage(
            conversation_public_id=conversation_id,
            user_internal_id=current_user.internal_id,
            context_engine=getattr(request.app.state, "context_engine", None),
            preview_message=body.content,
            preview_attached_file_ids=body.attached_file_ids,
            preview_next_request=True,
        )
    except ConversationNotFound:
        from app.core.response import error

        raise HTTPException(
            status_code=404,
            detail=error(40401, "会话不存在或不属于当前用户"),
        )
    return success(data.to_dict())


@router.post("/{conversation_id}/context/compact")
async def compact_conversation_context(
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
):
    """WP-BE-05: Manual Conversation Compaction（owner-scope）。

    Context View Compression：生成/更新 Conversation Summary，不删除业务数据。
    同一 Conversation 同一时间只允许一个 manual compaction（并发 → 409）。
    """
    from app.services.manual_compaction_service import (
        ManualCompactionConflictError,
        ManualCompactionError,
        ManualConversationCompactionService,
    )

    service = ManualConversationCompactionService(session)
    from app.db.session import AsyncSessionLocal

    llm_invoker = getattr(request.app.state, "context_llm_bridge", None)
    llm_client = await _build_llm_client(session, current_user.internal_id)

    try:
        result = await service.compact(
            conversation_public_id=conversation_id,
            user_internal_id=current_user.internal_id,
            session_factory=AsyncSessionLocal,
            llm_invoker=llm_invoker,
            llm_client=llm_client,
        )
    except ManualCompactionConflictError as exc:
        from app.core.response import error

        raise HTTPException(status_code=409, detail=error(40901, exc.detail))
    except ManualCompactionError as exc:
        from app.core.response import error

        code_num = 40401 if "not_found" in exc.code else 40001
        raise HTTPException(status_code=404 if code_num == 40401 else 400, detail=error(code_num, exc.detail))
    return success(result.to_dict(), "上下文已压缩")



@router.patch("/{conversation_id}/memory-mode")
async def update_conversation_memory_mode(
    body: _MemoryModeRequest,
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Conversation Memory Mode（off / inherit / on）。CE-03 WP-8。"""
    from sqlalchemy import update
    from app.models.conversation import Conversation

    valid = {"off", "inherit", "on"}
    if body.memory_mode not in valid:
        return success(None, "memory_mode 必须为 off/inherit/on")

    result = await session.execute(
        update(Conversation)
        .where(
            Conversation.public_id == conversation_id,
            Conversation.user_id == current_user.internal_id,
        )
        .values(context_memory_mode=body.memory_mode)
    )
    if result.rowcount == 0:
        return success(None, "会话不存在或不属于当前用户")
    await session.commit()
    return success({"memory_mode": body.memory_mode}, "记忆模式已更新")


# ════════════════════════════════════════════════════════════════════════════════
# Conversation API (创建 / 列表 / 详情 / 更新 / 删除):
#
#   路由清单(快速对照):
#     POST   /api/v1/conversations            → 创建新空会话 + 可选初标题
#     GET    /api/v1/conversations            → 列出当前用户所有会话(分页)
#     GET    /api/v1/conversations/{id}       → 单会话详情 + 文件 + 时间线
#     PATCH  /api/v1/conversations/{id}       → 修改标题 / 归档状态
#     DELETE /api/v1/conversations/{id}       → 软删除(归档,可恢复)
#     POST   /api/v1/conversations/{id}/compact → 触发手动压缩(调 ManualConversationCompactionService)
#
#   链路:
#     FastAPI Depends get_current_user → 拿到 user_internal_id
#     → ConversationService.{create / list / get_detail / update / delete / compact}
#       → ConversationRepository 操作 conversation 表
#       → 时间线由前端 reducer 拼(本接口只给原子数据)
#
# 关键约束(供开发者速查):
#   - 所有列表接口用复合索引 (user_id, updated_at DESC),page size 上限 100;
#   - PATCH 字段白名单限制(只允许 title / archived / metadata);
#   - DELETE 是 soft_delete(不物理删除),后台审计可恢复;
#   - "/compact" 触发 ManualConversationCompactionService,失败仅日志,前端
#     可重试。
