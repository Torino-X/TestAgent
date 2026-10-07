"""Message endpoints — list, send, feedback, and regenerate.

Phase 2.9A.26+: ``PUT /api/messages/{public_id}/feedback`` lets a user
like/dislike an assistant message; ``GET /api/conversations/{id}/messages``
returns the user's existing feedback inline as ``my_feedback``.

Phase 2.9A.26+: ``POST /api/messages/{public_id}/regenerate`` re-runs the
LLM chat reply for the original user prompt that produced the assistant
message, returning an SSE stream for the front-end to subscribe to.
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
import json

from app.api.deps import get_current_user
from app.core.feedback_cache import FeedbackCache
from app.core.response import success
from app.db.session import get_db
from app.integrations.llm_client import LLMClient
from app.schemas.auth import UserProfile
from app.schemas.message import FeedbackRequest, FeedbackResponse, SendMessageRequest
from app.services.message_feedback_service import MessageFeedbackService
from app.services.message_regeneration_service import MessageRegenerationService
from app.services.message_service import MessageService
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)

router = APIRouter()


def _encode_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _agent_runtime_readiness(request: Request | None) -> tuple[bool | None, str | None]:
    """Return production readiness while keeping isolated service tests injectable."""
    state = getattr(getattr(request, "app", None), "state", None)
    probe = getattr(state, "probe_report", None)
    if state is None or probe is None:
        return None, None
    if not bool(getattr(probe, "langgraph_readiness", False)):
        return False, "langgraph_readiness_false"
    if getattr(state, "api_dispatcher", None) is None:
        return False, "api_dispatcher_not_mounted"
    if not bool(getattr(state, "worker_started", False)):
        return False, "execution_worker_not_started"
    return True, None


def _build_feedback_cache_for_message(
    message_internal_id: int,
) -> FeedbackCache:
    """Step 9 迁移: 现在用 Business Cache Redis (port 6380),走标准 CacheManager.

    与原实现的差异:
      - 走 ``get_cache_manager()`` 而不是直接构造 redis client (复用 breaker /
        singleflight / bulkhead / negative cache)
      - 24h TTL 不变, negative TTL 60s
      - Master toggle ``cache_redis_enabled`` + domain flag ``cache_fb_enabled``
        关闭 → 退化为 no-op (与旧行为一致)
      - Fail-soft: Redis 异常 → 退化为 MySQL-only,不 raise

    参数 ``message_internal_id`` 在新实现里已经不需要 (route 把消息 id 传给
    cache.set 时才用),保留参数仅为 backward-compat 调用方。
    """
    from app.cache.domains.feedback_cache import get_feedback_cache

    return get_feedback_cache()


async def _apply_feedback_to_message(
    detail: dict,
    *,
    feedback_by_message_internal: dict[int, str],
) -> dict:
    """Embed ``my_feedback`` on a message detail dict.

    Phase 2.9A.26+: only agent_text rows surface this field so the UI
    doesn't render like/dislike buttons on tool/system messages.  The
    serializer is intentionally a single pass — N+1 prevention happens
    in ``MessageService.list_messages`` which calls ``list_feedbacks_for_conversation``
    once per conversation.
    """
    if detail.get("role") != "agent" or detail.get("message_type") != "agent_text":
        return detail
    return detail


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List all messages in a conversation.

    Phase 2.9A.26+: each assistant ``agent_text`` row carries the
    caller's existing feedback inline as ``my_feedback`` so the UI does
    not need a second round-trip per message.  The implementation
    batches a single SQL query keyed on the conversation (no N+1).
    """
    service = MessageService(session)
    feedback_service = MessageFeedbackService(session)
    messages = await service.list_messages(current_user.internal_id, conversation_id)

    # Phase 2.9A.26: inline ``my_feedback`` per agent_text row.
    # Use a single joined query to map ``message_public_id → feedback_type``
    # so there is no N+1 (one query per conversation, not one per message).
    from sqlalchemy import select
    from app.models.conversation import Conversation

    conv = await session.execute(
        select(Conversation.id).where(
            Conversation.public_id == conversation_id,
            Conversation.user_id == current_user.internal_id,
            Conversation.deleted_at.is_(None),
        )
    )
    conv_row = conv.first()
    feedback_by_msg_pubid: dict[str, str] = {}
    if conv_row is not None:
        feedback_by_msg_pubid = (
            await feedback_service.list_feedbacks_for_conversation_by_public_id(
                user_id=current_user.internal_id,
                conversation_id=int(conv_row[0]),
            )
        )

    enriched: list[dict] = []
    for msg in messages:
        if msg.get("role") == "agent" and msg.get("message_type") == "agent_text":
            msg_id = msg.get("message_id", "")
            msg["my_feedback"] = feedback_by_msg_pubid.get(msg_id)
        enriched.append(msg)

    return success({"messages": enriched, "total": len(enriched)})


async def _build_llm_client(session: AsyncSession, user_internal_id: int) -> LLMClient:
    """Build an LLMClient wired to the user's configured model.

    F015 — passes the integer ``users.id`` (NOT the public_id) to
    ``build_llm_config_provider``.  If the user has no model_configs
    row the provider returned will be an ``LLMNotConfiguredMarker``;
    ``LLMClient`` detects this and surfaces a friendly
    ``LLMClientError`` to the caller.
    """
    provider = await SettingsService(session).build_llm_config_provider(
        user_id=user_internal_id
    )
    return LLMClient(config_provider=provider)


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    body: SendMessageRequest,
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
):
    """Send a user message.

    F013: the message is first classified by an LLM-backed intent
    router; based on the resulting route we either:

      - create an ``agent_task`` (only when the user asked to generate
        a test plan **and** all required files are present),
      - ask the user to upload the missing files,
      - surface an "unsupported capability" notice for capabilities
        not yet open in this version, or
      - return a friendly chat reply for general questions.

    The response includes both the legacy ``agent_task``/``agent_reply``
    fields and the new ``route``/``intent``/``requires_sse``/``task_id``
    routing fields.
    """
    logger.info(
        "收到发送消息请求 | 会话=%s | 用户=%s | 内容长度=%d",
        conversation_id, current_user.username, len(body.content or ""),
    )
    llm = await _build_llm_client(session, current_user.internal_id)
    # CE-04 MIG_CHAT：把 context_llm_bridge 注入 MessageService → IntentRouter。
    # app.state 可能未挂（测试），getattr 兜底为 None（degraded 不阻塞）。
    _bridge = getattr(request.app.state, "context_llm_bridge", None)
    _context_engine_invoker = getattr(request.app.state, "context_llm_invoker", None)
    _runtime_ready, _runtime_reason = _agent_runtime_readiness(request)
    service = MessageService(
        session,
        llm_client=llm,
        context_llm_invoker=_bridge,
        context_engine_invoker=_context_engine_invoker,
        agent_runtime_ready=_runtime_ready,
        agent_runtime_readiness_reason=_runtime_reason,
    )
    result = await service.send_message(
        conv_public_id=conversation_id,
        content=body.content,
        attached_file_ids=body.attached_file_ids,
        user_internal_id=current_user.internal_id,
        knowledge_mode_snapshot=body.knowledge_mode_snapshot,
    )
    route_value = result.get("route", "n/a") if isinstance(result, dict) else "n/a"
    logger.info(
        "消息发送完成 | 会话=%s | 用户=%s | route=%s",
        conversation_id, current_user.username, route_value,
    )
    return success(result)


@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(
    body: SendMessageRequest,
    conversation_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
):
    logger.info(
        "开始SSE流式消息 | 会话=%s | 用户=%s | 附件数=%d",
        conversation_id, current_user.username, len(body.attached_file_ids or []),
    )
    llm = await _build_llm_client(session, current_user.internal_id)
    # CE-04 MIG_CHAT：注入 context_llm_bridge（缺失/未挂 → None，degraded）
    _bridge = getattr(request.app.state, "context_llm_bridge", None)
    _context_engine_invoker = getattr(request.app.state, "context_llm_invoker", None)
    _runtime_ready, _runtime_reason = _agent_runtime_readiness(request)
    service = MessageService(
        session,
        llm_client=llm,
        context_llm_invoker=_bridge,
        context_engine_invoker=_context_engine_invoker,
        agent_runtime_ready=_runtime_ready,
        agent_runtime_readiness_reason=_runtime_reason,
    )

    async def event_stream():
        try:
            async for event in service.stream_message(
                conv_public_id=conversation_id,
                content=body.content,
                attached_file_ids=body.attached_file_ids,
                user_internal_id=current_user.internal_id,
                knowledge_mode_snapshot=body.knowledge_mode_snapshot,
            ):
                yield _encode_sse(event["event"], event["data"])
        except Exception as exc:  # noqa: BLE001
            from app.core.exceptions import AppError

            logger.exception(
                "SSE消息流异常 | 会话=%s | 用户=%s | err=%s",
                conversation_id, current_user.username, type(exc).__name__,
            )
            yield _encode_sse(
                "error",
                {
                    "message": str(exc) or "消息发送失败",
                    "code": exc.code if isinstance(exc, AppError) else 50000,
                    "detail": exc.detail if isinstance(exc, AppError) else None,
                },
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.put("/messages/{message_public_id}/feedback")
async def set_message_feedback(
    body: FeedbackRequest,
    message_public_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Phase 2.9A.26+: set / clear per-user feedback on an assistant message.

    Body:
      ``{"feedback": "like" | "dislike" | null}``

    * ``like`` / ``dislike`` — exclusive UPSERT in MySQL (one row per
      ``(user, message)``); cache write is best-effort.
    * ``null`` — DELETE the row (cancels the reaction).
    * Anything else — 400 ValidationError.
    """
    if body.feedback not in {"like", "dislike", None}:
        from app.core.exceptions import ValidationError
        raise ValidationError("feedback 必须是 like / dislike / null 之一")

    # We need the conversation_public_id to check ownership.  Look it up
    # from the message row itself (cheap SELECT) before delegating.
    from sqlalchemy import select
    from app.models.message import Message as _Message

    msg_lookup = await session.execute(
        select(_Message.conversation_id).where(
            _Message.public_id == message_public_id,
            _Message.user_id == current_user.internal_id,
            _Message.deleted_at.is_(None),
        )
    )
    msg_row = msg_lookup.first()
    if msg_row is None:
        from app.core.exceptions import NotFoundError
        raise NotFoundError("消息")

    conversation_id_internal = int(msg_row[0])

    # Resolve conversation_public_id for the service.
    from app.models.conversation import Conversation

    conv_lookup = await session.execute(
        select(Conversation.public_id).where(
            Conversation.id == conversation_id_internal,
            Conversation.user_id == current_user.internal_id,
            Conversation.deleted_at.is_(None),
        )
    )
    conv_row = conv_lookup.first()
    conversation_public_id = str(conv_row[0]) if conv_row is not None else ""

    feedback_service = MessageFeedbackService(session)
    new_value = await feedback_service.set_feedback(
        user_id=current_user.internal_id,
        conversation_public_id=conversation_public_id,
        message_public_id=message_public_id,
        feedback_type=body.feedback,
    )

    # Best-effort cache write; failure here is non-fatal.
    cache = _build_feedback_cache_for_message(conversation_id_internal)
    try:
        msg_internal_lookup = await session.execute(
            select(_Message.id).where(
                _Message.public_id == message_public_id,
                _Message.deleted_at.is_(None),
            )
        )
        msg_internal_row = msg_internal_lookup.first()
        if msg_internal_row is not None:
            await cache.set(
                current_user.internal_id,
                int(msg_internal_row[0]),
                new_value,
            )
    except Exception:
        # Fall through — service call already mutated MySQL.
        pass

    return success(
        FeedbackResponse(message_id=message_public_id, feedback=new_value).model_dump()
    )


# ── Phase 2.9A.26+: Regeneration ──────────────────────────────────────


class RegenerateRequest(BaseModel):
    """Request body for ``POST /api/messages/{public_id}/regenerate``.

    ``idempotency_key`` is optional but strongly recommended; if two
    requests with the same key arrive concurrently, the second one
    receives the same ``generation_id`` from the first, preventing
    duplicate generation rows.
    """

    idempotency_key: str | None = Field(default=None)


@router.post("/messages/{message_public_id}/regenerate")
async def regenerate_message(
    body: RegenerateRequest = RegenerateRequest(),
    message_public_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    request: Request = None,  # type: ignore[assignment]
    session: AsyncSession = Depends(get_db),
):
    """Phase 2.9A.26+: regenerate the last assistant text reply.

    The endpoint validates that:
      1. The target message is the latest assistant_text in the conversation
      2. No running regeneration already exists
      3. The original user prompt is available for re-submission

    On validation failure, the appropriate AppError subclass is raised
    (50911 / 50912 / 50913), which the ``AppError`` handler converts to
    a JSON error response.

    On success, an SSE ``StreamingResponse`` is returned so the front-end
    can subscribe to ``agent_text_delta`` / ``agent_text_done`` events
    targeting the same ``message_id``.  The new content replaces the old
    one in the ``messages`` table upon completion.
    """
    from fastapi import HTTPException

    regenerate_service = MessageRegenerationService(session)
    try:
        (
            msg_internal_id,
            _conv_id_str,
            original_user_prompt,
            generation_public_id,
            target_msg_public_id,
        ) = await regenerate_service.validate_and_prepare(
            user_id=current_user.internal_id,
            conversation_public_id="",  # resolved inside via message lookup
            message_public_id=message_public_id,
        )
    except Exception:
        # Let the AppError handler catch the specific exception type.
        # MessageRegeneration* exceptions inherit from AppError and will
        # be handled by the global ``app_error_handler`` in main.py.
        raise

    await session.commit()

    # Build LLM client for the streaming reply
    llm = await _build_llm_client(session, current_user.internal_id)
    conversation_id = _conv_id_str
    target_message_id = target_msg_public_id

    async def _event_stream():
        """Stream the regenerated chat reply as SSE events.

        Uses the same ``stream_message`` mechanism as the original send,
        but with the original user prompt re-submitted.  The ``agent_text_delta``
        and ``agent_text_done`` events carry the same ``message_id`` as the
        target, so the front-end can update in-place.
        """
        # Use a fresh session for the streaming service call
        from app.db.session import AsyncSessionLocal
        _bridge = getattr(request.app.state, "context_llm_bridge", None)
        _context_engine_invoker = getattr(request.app.state, "context_llm_invoker", None)
        _runtime_ready, _runtime_reason = _agent_runtime_readiness(request)
        async with AsyncSessionLocal() as stream_session:
            stream_service = MessageService(
                stream_session,
                llm_client=llm,
                context_llm_invoker=_bridge,
                context_engine_invoker=_context_engine_invoker,
                agent_runtime_ready=_runtime_ready,
                agent_runtime_readiness_reason=_runtime_reason,
            )
            gen_service = MessageRegenerationService(
                stream_session,
                generation_repo=None,
            )

            final_content = ""
            try:
                async for event in stream_service.stream_message(
                    conv_public_id=conversation_id,
                    content=original_user_prompt,
                    attached_file_ids=None,
                    user_internal_id=current_user.internal_id,
                ):
                    event_type = event.get("event", "message")
                    data = event.get("data", {})

                    # Intercept agent_text_done to capture the final content
                    # and update the generation record
                    if event_type == "agent_text_done":
                        agent_reply = data.get("agent_reply", {})
                        final_content = agent_reply.get("content", "")

                        # Update generation record + messages table
                        from app.repositories.message_generation_repository import (
                            MessageGenerationRepository,
                        )
                        gen_repo = MessageGenerationRepository(stream_session)
                        gen_record = await gen_repo.get_by_public_id(generation_public_id)
                        if gen_record is not None:
                            await gen_repo.deactivate_all_for_message(gen_record.message_id)
                            await gen_repo.activate_generation(gen_record.id)
                            await gen_repo.update_status(
                                gen_record.id,
                                status="completed",
                                content_markdown=final_content,
                            )
                            # Update the messages table content
                            from sqlalchemy import text as _sa_text
                            await stream_session.execute(
                                _sa_text(
                                    "UPDATE messages SET content = :content, updated_at = NOW() "
                                    "WHERE id = :mid"
                                ),
                                {"content": final_content, "mid": gen_record.message_id},
                            )
                            await stream_session.commit()

                    # Re-map the message_id in delta events to the target
                    if event_type == "agent_text_delta":
                        data = {**data, "message_id": target_message_id}
                        event = {"event": event_type, "data": data}

                    # Re-map agent_reply_created to carry the target message_id
                    if event_type == "agent_reply_created":
                        msg = data.get("message", {})
                        msg = {**msg, "message_id": target_message_id}
                        data = {**data, "message": msg}
                        event = {"event": event_type, "data": data}

                    # Re-map agent_text_done similarly
                    if event_type == "agent_text_done":
                        agent_reply = data.get("agent_reply", {})
                        agent_reply = {**agent_reply, "message_id": target_message_id}
                        data = {**data, "agent_reply": agent_reply}
                        event = {"event": event_type, "data": data}

                    yield f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

            except Exception as exc:
                logger.exception(
                    "regenerate_message SSE failed | msg=%s | err=%s",
                    message_public_id,
                    exc,
                )
                # Mark generation as failed
                from app.repositories.message_generation_repository import (
                    MessageGenerationRepository,
                )
                async with AsyncSessionLocal() as fail_session:
                    gen_repo = MessageGenerationRepository(fail_session)
                    gen_record = await gen_repo.get_by_public_id(generation_public_id)
                    if gen_record is not None:
                        await gen_repo.update_status(
                            gen_record.id, status="failed", error_code="REGEN_FAILED"
                        )
                        await fail_session.commit()

                yield _encode_sse("error", {"message": "重新生成失败，请稍后重试"})

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ════════════════════════════════════════════════════════════════════════════════
# Message API (列表 / 发送 / 反馈 / 重新生成):
#
#   路由清单:
#     GET    /api/v1/conversations/{id}/messages          → 列出会话消息(含 my_feedback)
#     POST   /api/v1/messages                              → 发送(触发 message_service.send_message → intent_router → 测试方案/普通聊天)
#     POST   /api/v1/messages/{public_id}/regenerate      → 重新生成 assistant 回复
#     PUT    /api/v1/messages/{public_id}/feedback        → 👍/👎 (MessageFeedbackService.record)
#     POST   /api/v1/messages/{public_id}/stream          → SSE 流式聊天(进 ChatLLMService.stream_reply)
#
#   链路(发送消息主路径):
#     POST /messages → MessageService.send_message
#       → IntentRouter.dispatch(5 routes):
#           1. chat_reply     → ChatLLMService + 写库普通 message
#           2. ask_for_files  → static default(文件缺失列表)
#           3. unsupported    → static default
#           4. clarify        → router reply_message
#           5. agent_task     → AgentTaskService + AgentExecutionRequest Outbox
#       → 落库 user message + assistant message(顺序固定)
#
# 关键约束(供开发者速查):
#   - send_message 失败也要先写 user message(失败的 assistant 占位)以保持时间线;
#   - regen / feedback 严格校验 owner(user_internal_id == message.user_id);
#   - SSE 流式聊天失败不能让数据库写一半,必须事务边界;
#   - messages 列表含 my_feedback 字段(已加 Phase 2.9A.26)。
