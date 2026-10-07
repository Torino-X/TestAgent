"""Agent task endpoints — task detail, SSE events, confirm, cancel, task artifacts."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.context import AgentContext
from app.agent.event_publisher import event_publisher
from app.api.deps import get_current_user
from app.core.response import error, success
from app.core.logging import LogEvent, log_event
from app.db.session import AsyncSessionLocal, get_db
from app.repositories.agent_execution_request_repository import (
    AgentExecutionRequestRepository,
)
from app.repositories.agent_task_repository import AgentTaskRepository
from app.repositories.file_repository import FileRepository
from app.schemas.agent import (
    ConfirmSectionsRequest,
    FormatLossDecisionRequest,
    FormatLossDecisionResponse,
    PreparationClarificationRequest,
    RetryTaskRequest,
)
from app.schemas.auth import UserProfile
from app.services.agent_execution_worker import (
    get_execution_worker,
)
from app.services.agent_task_service import AgentTaskService
from app.services.artifact_service import ArtifactService
from app.services.settings_service import SettingsService

logger = logging.getLogger(__name__)
router = APIRouter()


_PRE_CONFIRM_STREAM_BOUNDARY_STATUSES = frozenset(
    {
        "waiting_user_confirm",
        "format_loss_review",
        "completed",
        "failed",
        "cancelled",
    }
)


def merge_section_confirm_config(
    task_context_json: object,
    sections: list[dict],
) -> dict:
    """Add a confirmation payload without discarding immutable task context.

    ORM JSON columns are returned as dictionaries, while some deployments
    expose the same column as JSON text. Treating a dictionary as text loses
    the frozen Context Engine manifest and breaks the resumed task.
    """
    if isinstance(task_context_json, dict):
        context = dict(task_context_json)
    elif isinstance(task_context_json, str) and task_context_json.strip():
        try:
            parsed = json.loads(task_context_json)
        except (TypeError, ValueError):
            parsed = {}
        context = dict(parsed) if isinstance(parsed, dict) else {}
    else:
        context = {}
    context["section_confirm_config"] = {"sections": sections}
    return context


# ── Phase 1 (Step 6): SSE heartbeat status helper ──────────────────
# 30s heartbeat originally ran ``SELECT status FROM agent_tasks`` 4
# times per stream (pre-confirm + incremental streams).  Replaced with
# cache-first read (设计文档 §13.3):
#
#   Redis GET task:status:{public_id}
#     ├─ hit → status
#     └─ miss
#          ├─ breaker closed → SELECT agent_tasks → SET Redis → status
#          └─ breaker open / Redis down → fallback DB (走 DB bulkhead limiter)
#
# The DB SELECT still runs in fallback path so SSE is not blocked when
# Business Cache Redis is unhealthy.
async def _read_task_status_for_sse(
    task_public_id: str | None,
    task_internal_id: int,
) -> str | None:
    """Cache-first task status read for SSE heartbeat.

    ``task_public_id`` is preferred (cache key); ``task_internal_id`` is
    the fallback for the DB SELECT when the cache misses.
    """
    # 1) Try cache.
    try:
        from app.cache.domains.task_cache import (
            TaskStatusDTO,
            get_task_status_cache,
        )

        cache = get_task_status_cache()
        cached = await cache.get_or_load_status(
            task_public_id=task_public_id or f"id:{task_internal_id}",
            loader=lambda: _load_task_status_dto_from_db(
                task_internal_id, task_public_id,
            ),
        )
        if cached is not None and isinstance(cached, TaskStatusDTO):
            return cached.status
    except Exception as exc:  # noqa: BLE001 — never let SSE die from cache
        logger.debug(
            "SSE status: cache read failed; falling back to DB | "
            "task_internal_id=%s | %s",
            task_internal_id, exc,
        )

    # 2) Cache miss / disabled / broken → fallback DB.
    async with AsyncSessionLocal() as session:
        from sqlalchemy import text as _sa_text

        row = (
            await session.execute(
                _sa_text(
                    "SELECT status FROM agent_tasks "
                    "WHERE id = :iid AND deleted_at IS NULL"
                ),
                {"iid": task_internal_id},
            )
        ).first()
    return row[0] if row else None


async def _load_task_status_dto_from_db(
    task_internal_id: int,
    task_public_id: str | None,
) -> "TaskStatusDTO | None":
    """DB loader for the TaskStatusCache; builds the DTO + populates cache.

    Called only on cache miss; falls through to ``None`` when the row
    has been deleted.
    """
    from sqlalchemy import text as _sa_text
    from app.cache.domains.task_cache import (
        TaskStatusDTO,
        get_task_status_cache,
    )
    from app.utils.datetime import utcnow

    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                _sa_text(
                    "SELECT status, active_run_id, updated_at "
                    "FROM agent_tasks WHERE id = :iid AND deleted_at IS NULL"
                ),
                {"iid": task_internal_id},
            )
        ).first()
    if row is None:
        return None
    status_val, active_run_id, updated_at = row
    return TaskStatusDTO(
        status=str(status_val),
        updated_at=updated_at.isoformat() if updated_at else utcnow().isoformat(),
        active_run_id=active_run_id,
        terminal=str(status_val) in (
            "completed", "failed", "cancelled",
        ),
    )


async def _has_pending_execution_retry(task_internal_id: int) -> bool:
    """Return whether a worker will still retry this task.

    A task can briefly retain ``failed`` after a transient checkpoint failure
    while its outbox request has already been released back to ``queued``.
    That state is not terminal for an observer: closing SSE would make the
    browser miss the retry's progress events.
    """
    try:
        async with AsyncSessionLocal() as session:
            from sqlalchemy import text as _sa_text

            row = (
                await session.execute(
                    _sa_text(
                        "SELECT 1 FROM agent_execution_requests "
                        "WHERE task_id = :task_id "
                        "AND status IN ('queued', 'running') LIMIT 1"
                    ),
                    {"task_id": task_internal_id},
                )
            ).first()
            return row is not None
    except Exception as exc:  # noqa: BLE001 - observation must not kill SSE
        logger.warning(
            "SSE retry-state check failed; falling back to task status | "
            "task_internal_id=%s | err=%s",
            task_internal_id,
            exc,
        )
        return False


def _should_close_pre_confirm_stream(
    *,
    has_local_dispatch_task: bool,
    local_dispatch_done: bool,
    task_status: str | None,
    has_pending_execution_retry: bool = False,
) -> bool:
    """Keep worker-backed SSE alive until the persisted task reaches a boundary.

    In worker mode there is intentionally no local asyncio task.  Treating that
    absence as completion made a quiet LLM/tool call close the SSE response after
    30 seconds, even though the worker was still progressing the task.
    """
    if has_local_dispatch_task:
        return local_dispatch_done
    if has_pending_execution_retry:
        return False
    return str(task_status or "").lower() in _PRE_CONFIRM_STREAM_BOUNDARY_STATUSES


def _resolve_api_dispatcher_from_request(request) -> "Any | None":
    """读 ``request.app.state.api_dispatcher``(Phase 2.8A lifespan 挂的 singleton)。

    Phase 2.8A 范围内:lifespan 在 main.py 启动时构造并挂上。
    测试 / 非 lifespan 场景下可能为 None — 调用方应走 Legacy fallback。

    显式 None 返回值便于 FastAPI SSE handler 在 None 时跳过 dispatch 层,
    完全不影响 Phase 1 既有行为(守禁令 #1 不删除 Legacy)。
    """
    try:
        return getattr(request.app.state, "api_dispatcher", None)
    except Exception:  # noqa: BLE001
        return None


async def _subscribe_task_events(*, request: Request | None, task_id: str):
    """Use the runtime event bus; retain legacy publishing as a fallback."""
    live_event_bus = None
    try:
        live_event_bus = getattr(request.app.state, "live_event_bus", None)
    except Exception:  # noqa: BLE001
        pass

    if live_event_bus is not None:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        await live_event_bus.subscribe(task_id=task_id, queue=queue)

        async def unsubscribe() -> None:
            await live_event_bus.unsubscribe(task_id=task_id, queue=queue)

        return queue, unsubscribe

    queue = event_publisher.subscribe(task_id)

    async def unsubscribe() -> None:
        event_publisher.unsubscribe(task_id, queue)

    return queue, unsubscribe


async def _resolve_engine_with_fallback(
    *,
    api_dispatcher: "Any | None",
    task_id: str,
    explicit_engine_type: "str | None",
) -> "tuple[str, bool, str | None]":
    """通过 ``ApiDispatcher._resolve_engine`` 选引擎;无 dispatcher 时 fail-closed。

    Phase 2.9A.8:api_dispatcher=None 不再静默回退 legacy;按用户要求
    启动失败时 Agent 任务接口返回明确 503(不再走 Legacy,不再制造
    ``engine=legacy fallback_used=False`` 假象)。

    Returns:
        (engine, fallback_used, fallback_reason) — ApiDispatcher 决策签名。

    Raises:
        HTTPException 503 — api_dispatcher=None 时(启动失败)
    """
    if api_dispatcher is None:
        from fastapi import HTTPException as _HTTPException
        # Phase 2.9A.8:不再吞掉这个故障。SSE 端点返 503,前端可识别
        # 这是启动配置错误,而非普通业务错误。
        logger.error(
            "Phase 2.9A.8 api_dispatcher 未挂载 | task_id=%s | "
            "返 503(不再静默回退 legacy)",
            task_id,
        )
        raise _HTTPException(
            status_code=503,
            detail={
                "code": 50301,
                "message": (
                    "Agent runtime is not ready (api_dispatcher not mounted). "
                    "Production entry broken; cannot dispatch task."
                ),
            },
        )
    engine, fallback, reason = api_dispatcher._resolve_engine(
        task_public_id=task_id, task_engine_type=explicit_engine_type
    )
    return engine, fallback, reason


async def _register_inflight(api_dispatcher: "Any | None", task_id: str, engine: str) -> None:
    """登记任务进入 in-flight;无 dispatcher 时 no-op(Phase 1 等价)。

    Phase 2.8C ADR-2.8C-10:agent_tasks.py 已**移除**显式 _register_inflight 调用;
    单一登记点 = ApiDispatcher.dispatch_* 内部 begin()。本函数保留供
    其他可能需要的边界调用,但默认 SSE 端点不再调。
    """
    if api_dispatcher is None:
        return
    try:
        api_dispatcher.inflight.begin(task_id, engine)
    except Exception:  # noqa: BLE001
        # ParallelDispatchGuardError 等:让上层 SSE 路径决定如何响应
        # 当前 SSE 接口对并发重连无 409 响应语义,先 log 后继续。
        logger.warning(
            "Step11: inflight.begin failed | 任务=%s | 引擎=%s",
            task_id, engine,
        )


def _complete_inflight(api_dispatcher: "Any | None", task_id: str) -> None:
    """结束 in-flight 登记表;无 dispatcher 时 no-op。"""
    if api_dispatcher is None:
        return
    try:
        api_dispatcher.complete_task(task_id)
    except Exception:  # noqa: BLE001
        logger.debug("Step11: inflight.end failed | 任务=%s", task_id)


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = AgentTaskService(session)
    result = await service.get_task(task_id, current_user.internal_id)
    return success(result)


@router.get("/tasks/{task_id}/event-list")
async def list_events(
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    cursor: int | None = None,
    limit: int = 50,
):
    """Phase 2.9A.27: 支持 cursor 分页与真实 total。

    前端 Hydration 路径用 ``?cursor=&limit=100`` 循环拉完,直到
    ``next_cursor`` 为 None;普通 live SSE/SSE retry 仍可省略参数,
    保留旧 50 条默认(向后兼容)。

    Query:
        cursor: 上次分页最后一条事件的 canonical_order;None 表示从头
        limit: 每次返回的最大事件数(默认 50,前端显式 100)

    Returns:
        ``{"events": [...], "next_cursor": int|None, "total": int}``
    """
    service = AgentTaskService(session)
    payload = await service.list_events(
        task_id,
        current_user.internal_id,
        cursor=cursor,
        limit=limit,
    )
    return success(payload)


@router.get("/tasks/{task_id}/events")
async def task_events_sse(
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    request: Request = None,  # type: ignore[assignment]
):
    """SSE endpoint — streams Agent execution events for a task.

    This endpoint is observation-only. Task execution is exclusively owned by
    the outbox worker; opening or reconnecting an SSE stream never dispatches work.

    Phase 2.9A.7 SSE Session 泄漏修复:
      * 鉴权 + 任务快照 + 文件解析 — 全部在 endpoint body(generator 外)
        用独立短 Session 完成,generator 闭包不再捕获 ORM/Repository 对象;
      * generator 只接收不可变标量(task_internal_id / task_conversation_id
        / engine_type / req_file_id / tpl_file_id / resolved_engine /
        fallback_used / fallback_reason);
      * generator 内部需要 DB 时(轮询 task.status)用独立短 Session,
        查询完立即退出 ``async with``;
      * 客户端断开时 generator 的 finally 只 unsubscribe bus,不再
        触碰 session — 避免 ``Cancelled during execution`` 把连接
        切到 invalidate 状态,触发 GC "non-checked-in connection"。
    """

    from sqlalchemy import text as _sa_text

    # ── 阶段 1:鉴权 + 任务快照 + 文件解析(独立短 Session,立刻 close) ──
    async with AsyncSessionLocal() as setup_session:
        task_row = (
            await setup_session.execute(
                _sa_text(
                    "SELECT id, conversation_id, user_instruction, engine_type, status "
                    "FROM agent_tasks "
                    "WHERE public_id = :pid AND user_id = :uid AND deleted_at IS NULL"
                ),
                {"pid": task_id, "uid": current_user.internal_id},
            )
        ).first()
        if not task_row:
            raise HTTPException(status_code=404, detail="Task not found")

        (
            task_internal_id,
            task_conversation_id,
            task_user_instruction,
            task_engine_type_raw,
            task_status,
        ) = task_row
        task_engine_type = (
            str(task_engine_type_raw).strip().lower()
            if task_engine_type_raw is not None
            else None
        )

        from app.repositories.file_repository import FileRepository

        file_repo = FileRepository(setup_session)
        conv_files = await file_repo.list_by_conversation(
            current_user.internal_id, task_conversation_id
        )

        req_file_id = ""
        tpl_file_id = ""
        for f in conv_files:
            if f.file_type == "requirement_doc" and not req_file_id:
                req_file_id = f.public_id
            elif f.file_type == "test_plan_template" and not tpl_file_id:
                tpl_file_id = f.public_id

        logger.info(
            "SSE预确认阶段开始 | 任务=%s | 用户=%s | 需求文件=%s | 模板文件=%s",
            task_id, current_user.username, bool(req_file_id), bool(tpl_file_id),
        )

    if task_engine_type != "langgraph" and task_status not in {
        "completed",
        "failed",
        "cancelled",
    }:
        raise HTTPException(
            status_code=409,
            detail={
                "code": 40910,
                "error_code": "UNSUPPORTED_LEGACY_TASK",
                "message": "MIGRATION_REQUIRED: historical Legacy task cannot execute",
                "migration_status": "migration-required",
                "task_id": task_id,
            },
        )

    # ── 阶段 2:event_stream generator 闭包不持有 Session/Repository ──
    async def event_stream():
        # 把需要 DB 的状态查询 / dispatch 都在 generator 内用独立短 Session
        # 完成;generator 闭包只捕获不可变标量。
        queue, unsubscribe = await _subscribe_task_events(
            request=request,
            task_id=task_id,
        )
        stream_started = time.monotonic()
        events_sent = 0
        log_event(
            logging.getLogger("testagent.agent"),
            logging.INFO,
            LogEvent.SSE_CONNECTED,
            "Agent SSE stream connected",
            task_id=task_id,
        )

        # Phase 2.9A.28: pre-confirm 也支持 Last-Event-ID 补发断线缺口。
        # 原设计只在 events-post-confirm 有 DB replay;pre-confirm 断线后
        # 事件永久丢失。现在统一:订阅 bus 后先从 DB 补发 cursor 之后的
        # 持久化事件,然后进入实时循环。顺序是"先 bus 再 DB"还是"先 DB 再
        # bus"有竞态窗口,所以用单次"先 DB 快照再 bus 实时"保证不丢。
        #
        # 注意:必须在 subscribe 之后立即读 DB,减少"subscribe → DB 读"
        # 窗口内事件丢失风险。LiveEventBus 有 maxsize=200 的环形缓冲,
        # 读 DB 期间的新事件会堆积在 buffer 中不丢。
        after_seq = 0
        if request is not None:
            try:
                last_event_id = (
                    request.headers.get("last-event-id")
                    if hasattr(request, "headers") else None
                )
            except Exception:  # noqa: BLE001
                last_event_id = None
            if last_event_id and str(last_event_id).isdigit():
                after_seq = int(last_event_id)

        if after_seq > 0:
            try:
                async with AsyncSessionLocal() as replay_session:
                    replay_result = await replay_session.execute(
                        _sa_text(
                            "SELECT event_type, sequence_no, payload_json, "
                            "title, content, public_id, created_at "
                            "FROM agent_events "
                            "WHERE task_id = :iid AND sequence_no IS NOT NULL "
                            "AND sequence_no > :after_seq "
                            "ORDER BY sequence_no ASC LIMIT 200"
                        ),
                        {"iid": task_internal_id, "after_seq": after_seq},
                    )
                    replay_rows = replay_result.fetchall()
                for r in replay_rows:
                    evt = {
                        "event_type": r[0],
                        "sequence_no": r[1],
                        "payload": json.loads(r[2]) if r[2] else {},
                        "title": r[3],
                        "content": r[4],
                        "event_id": r[5],
                        "created_at": str(r[6]) if r[6] else None,
                        "task_id": task_id,
                    }
                    seq_no = evt.get("sequence_no")
                    id_line = f"id: {int(seq_no)}\n" if seq_no else ""
                    yield (
                        f"{id_line}"
                        f"event: {evt['event_type']}\n"
                        f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"
                    )
                    events_sent += 1
                logger.info(
                    "SSE预确认 | 断线补发 | task=%s | after_seq=%d | 补发=%d 条",
                    task_id, after_seq, len(replay_rows),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SSE预确认 | 断线补发失败(degraded) | task=%s | err=%s",
                    task_id, exc,
                )

        logger.info("SSE observation stream attached | task=%s", task_id)

        # Stream events as they are published
        pre_done = False
        try:
            while not pre_done:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    data_str = json.dumps(event, ensure_ascii=False)
                    # Phase 2.9A.28: emit SSE id: line for frontend lastEventId tracking
                    seq_no = event.get("sequence_no")
                    id_line = f"id: {int(seq_no)}\n" if seq_no else ""
                    yield (
                        f"{id_line}"
                        f"event: {event.get('event_type', 'message')}\n"
                        f"data: {data_str}\n\n"
                    )
                    events_sent += 1
                except asyncio.TimeoutError:
                    # Phase 1 (Step 6): heartbeat reads task status via
                    # TaskStatusCache — see ``_read_task_status_for_sse``.
                    current_status = await _read_task_status_for_sse(
                        task_public_id=task_id,
                        task_internal_id=task_internal_id,
                    )

                    has_pending_execution_retry = (
                        await _has_pending_execution_retry(task_internal_id)
                        if str(current_status or "").lower()
                        in _PRE_CONFIRM_STREAM_BOUNDARY_STATUSES
                        else False
                    )
                    if _should_close_pre_confirm_stream(
                        has_local_dispatch_task=False,
                        local_dispatch_done=False,
                        task_status=current_status,
                        has_pending_execution_retry=has_pending_execution_retry,
                    ):
                        pre_done = True
                    else:
                        yield ": keep-alive\n\n"

            # Drain any remaining events
            while not queue.empty():
                event = queue.get_nowait()
                data_str = json.dumps(event, ensure_ascii=False)
                yield f"event: {event.get('event_type', 'message')}\ndata: {data_str}\n\n"

            # Phase 1 (Step 6): final-status read via TaskStatusCache.
            current_status = await _read_task_status_for_sse(
                task_public_id=task_id,
                task_internal_id=task_internal_id,
            )

            phase_status = (
                "failed" if current_status in {"failed", "cancelled"}
                else "waiting_user_confirm"
            )
            yield (
                f"event: stream_phase_done\ndata: "
                f"{json.dumps({'phase': 'pre_confirm', 'task_id': task_id, 'status': phase_status}, ensure_ascii=False)}\n\n"
            )
        except (asyncio.CancelledError, GeneratorExit):
            # Phase 2.9A.7: 客户端断开 → generator 闭包只 unsubscribe,
            # 不再触碰 session(generator 闭包不持有 session)。
            raise
        finally:
            await unsubscribe()
            log_event(
                logging.getLogger("testagent.agent"),
                logging.INFO,
                LogEvent.SSE_DISCONNECTED,
                "Agent SSE stream disconnected",
                task_id=task_id,
                events_sent_count=events_sent,
                duration_ms=round((time.monotonic() - stream_started) * 1000, 2),
            )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/tasks/{task_id}/events-post-confirm")
async def task_events_post_confirm_sse(
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    request: Request = None,  # type: ignore[assignment]
):
    """SSE endpoint for the post-confirmation phase (Phase 2.9A.3: STRICT LIFECYCLE).

    Phase 2.9A.3 invariant:
      * 鉴权 + 任务快照查询必须在 StreamingResponse 创建前完成;
      * 查询失败 → HTTPException 4xx/5xx → FastAPI 立刻返回响应头;
      * 绝不返回 SSE 200 后再抛数据库错误。
      * event_stream generator 不接收 AsyncSession;只接收不可变标量
        (task_internal_id / engine_type / engine_type 等);
      * generator 内部需要 DB 时,只能用 ``async with AsyncSessionLocal() as s:``
        独立短 Session,查询结束立即 close。

    业务 Resume 由 confirm 接口入队的 agent_execution_requests
    (request_type='resume') 行驱动 Worker 领取后执行 v3 Command(resume)。
    """
    from fastapi import HTTPException

    # ── 阶段 1:在 endpoint 内完成查询 / 鉴权(StreamingResponse 创建前) ──
    async with AsyncSessionLocal() as session:
        task_repo = AgentTaskRepository(session)
        task = await task_repo.get_owned_task(
            public_id=task_id,
            user_id=current_user.internal_id,
        )
        if task is None:
            # 查询失败 → FastAPI 立即返回 404,绝不带 SSE 头
            logger.info(
                "events-post-confirm: 任务不存在或无权访问 | task=%s | user=%s",
                task_id, current_user.username,
            )
            raise HTTPException(status_code=404, detail="任务不存在或无权访问")
        # 提取不可变标量(不再触碰 ORM 对象,后续 generator 也不依赖)
        task_internal_id = task.id
        task_engine_type = str(task.engine_type or "historical").strip().lower()
        ctx_raw = task.task_context_json
        if ctx_raw:
            try:
                ctx_dict = json.loads(ctx_raw)
            except (ValueError, TypeError):
                ctx_dict = {}
        else:
            ctx_dict = {}
        confirm_config = ctx_dict.get("section_confirm_config") or {}
    # Session 已 close;不可变标量已提取

    logger.info(
        "events-post-confirm start | task=%s | user=%s | engine=%s | tid=%d",
        task_id, current_user.username, task_engine_type, task_internal_id,
    )

    # ── 阶段 2:event_stream generator 不持有 AsyncSession ──
    async def event_stream():
        # ── 阶段 2.1:订阅 LiveEventBus(无 DB) ──
        try:
            queue, unsubscribe = await _subscribe_task_events(
                request=request,
                task_id=task_id,
            )
        except Exception as exc:
            logger.warning(
                "events-post-confirm: 订阅 LiveEventBus 失败 | task=%s | err=%s",
                task_id, exc,
            )
            yield (
                "event: error\n"
                "data: {\"message\": \"LiveEventBus unavailable\"}\n\n"
            )
            return

        # ── 阶段 2.2:历史事件回放(短 Session,查询完立即关) ──
        from sqlalchemy import text as _sa_text

        # Phase 2.9A.5:读取 Last-Event-ID 作为历史回放的起点 cursor。
        # SSE 协议约定:客户端在重连时通过 Last-Event-ID header 携带
        # 最后消费的 event id(我们用 sequence_no 作为 id)。
        after_seq = 0
        if request is not None:
            try:
                last_event_id = request.headers.get("last-event-id") if hasattr(request, "headers") else None
            except Exception:
                last_event_id = None
            if last_event_id and last_event_id.isdigit():
                after_seq = int(last_event_id)

        async def _replay_history():
            async with AsyncSessionLocal() as s:
                # Phase 2.9A.4:正确拆分 await + Result.fetchall
                # (coroutine 没有 fetchall;await 必须紧贴 execute)
                # 同时修正列名:agent_events 用 payload_json 与 public_id,
                # 不是 payload / event_id。
                #
                # Phase 2.9A.5:cursor 过滤 — 只返回 sequence_no > :after_seq
                # 防止 pre-confirm 已消费的事件在 post-confirm 重放时
                # 被重复 push(导致前端 tool_started 重复 append)。
                result = await s.execute(
                    _sa_text(
                        "SELECT event_type, sequence_no, payload_json, "
                        "title, content, public_id, created_at "
                        "FROM agent_events "
                        "WHERE task_id = :iid AND sequence_no IS NOT NULL "
                        "AND sequence_no > :after_seq "
                        "ORDER BY sequence_no ASC LIMIT 200"
                    ),
                    {"iid": task_internal_id, "after_seq": after_seq},
                )
                return result.fetchall()

        try:
            history_rows = await _replay_history()
        except asyncio.CancelledError:
            # 必须 re-raise,让 ASGI 优雅关闭
            raise
        except Exception as exc:
            # 历史回放失败 → 推 SSE error event 并返回
            # 不让 generator 异常逃逸成未处理 ASGI 异常
            logger.exception(
                "events-post-confirm: history replay failed",
                extra={"task_id": task_id, "err": str(exc)[:200]},
            )
            yield (
                "event: error\n"
                "data: {\"code\": \"EVENT_HISTORY_REPLAY_FAILED\", "
                "\"message\": \"历史执行事件加载失败\"}\n\n"
            )
            return

        # 输出 phase_start(包含 engine_type 与 section_confirm_config 给前端)
        yield "event: stream_phase_start\ndata: " + json.dumps(
            {
                "phase": "post_confirm",
                "task_id": task_id,
                "engine_type": task_engine_type,
                "section_confirm_config": confirm_config,
            },
            ensure_ascii=False,
        ) + "\n\n"

        for r in history_rows:
            evt = {
                "event_id": r[5],
                "event_type": r[0],
                "sequence_no": r[1],
                "title": r[3],
                "content": r[4],
                "payload": json.loads(r[2]) if r[2] else {},
                "created_at": str(r[6]) if r[6] else None,
            }
            # Phase 2.9A.5:emit SSE `id:` 行,前端 useSse 会更新 lastEventId
            # 用于下次重连 Last-Event-ID header。
            seq_no = evt["sequence_no"] or 0
            yield (
                f"id: {seq_no}\n"
                f"event: {evt['event_type']}\n"
                f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"
            )

        # ── 阶段 2.3:实时事件订阅循环,直到 terminal event ──
        try:
            while True:
                if request is not None and await request.is_disconnected():
                    logger.info(
                        "events-post-confirm: 客户端断开 | task=%s",
                        task_id,
                    )
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    data_str = json.dumps(event, ensure_ascii=False, default=str)
                    # Phase 2.9A.5:emit SSE `id:` 行(基于 sequence_no),
                    # 让前端 useSse 更新 lastEventId 用于 Last-Event-ID header。
                    seq_no = event.get("sequence_no")
                    id_line = f"id: {int(seq_no)}\n" if seq_no else ""
                    yield (
                        f"{id_line}"
                        f"event: {event.get('event_type', 'message')}\n"
                        f"data: {data_str}\n\n"
                    )
                    etype = event.get("event_type")
                    if etype in {"task_completed", "task_failed", "task_cancelled"}:
                        yield "event: stream_phase_done\ndata: " + json.dumps(
                            {
                                "phase": "post_confirm",
                                "task_id": task_id,
                                "status": etype.replace("task_", ""),
                            },
                            ensure_ascii=False,
                        ) + "\n\n"
                        return
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
        except (asyncio.CancelledError, GeneratorExit):
            logger.info(
                "events-post-confirm: SSE cancelled | task=%s",
                task_id,
            )
            raise
        finally:
            try:
                await unsubscribe()
            except Exception:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/tasks/{task_id}/confirm")
async def confirm_task(
    body: ConfirmSectionsRequest,
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info(
        "确认任务 | 任务=%s | 用户=%s | 分区数=%d",
        task_id, current_user.username, len(body.sections),
    )
    service = AgentTaskService(session)
    sections = [s.model_dump() for s in body.sections]

    # ── Tightened idempotency checks ──
    task_repo = AgentTaskRepository(session)
    task = await task_repo.get_by_public_id(task_id)
    if not task:
        return error(40401, "任务不存在")

    # 提前把后续会用到的 ORM 字段读出来缓存到本地变量；
    # service.confirm_sections 内部会 flush，理论上不会 expire task，
    # 但保险起见解耦 ORM 访问，方便后续 commit 后不再触碰 ORM。
    task_internal_id = task.id
    task_status = task.status
    task_ctx_json_raw = task.task_context_json
    if task.user_id != current_user.internal_id:
        return error(40301, "FORBIDDEN: 无权访问该任务")
    engine_type = str(getattr(task, "engine_type", None) or "").strip().lower()
    if engine_type != "langgraph":
        return error(
            40910,
            "MIGRATION_REQUIRED: historical Legacy task execution is unsupported",
        )

    from app.repositories.confirmation_repository import ConfirmationRepository
    confirm_repo = ConfirmationRepository(session)
    latest_confirm = await confirm_repo.get_latest_by_task(task_internal_id)

    if latest_confirm and latest_confirm.status == "confirmed":
        # Already completed — check if data matches
        existing_sections = (latest_confirm.response_json or {}).get("sections")
        if existing_sections == sections:
            # Same data: idempotent success
            return success({"task_id": task_id, "status": task_status, "message": "已确认（幂等）"})
        else:
            # Different data: conflict
            logger.warning(
                "任务确认冲突(幂等但数据不一致) | 任务=%s | 用户=%s",
                task_id, current_user.username,
            )
            return error(40901, "CONFIRMATION_ALREADY_COMPLETED: 确认已完成，数据不一致")

    if task_status == "failed":
        return error(40001, "TASK_FAILED: 任务已失败，无法确认")
    if task_status == "cancelled":
        return error(40002, "TASK_CANCELLED: 任务已取消，无法确认")

    if task_status != "waiting_user_confirm":
        if not latest_confirm or latest_confirm.status != "confirmed":
            import logging
            logging.getLogger("confirm").warning(
                "Task %s not in waiting_user_confirm, actual status=%s, confirm_status=%s",
                task_id, task_status, latest_confirm.status if latest_confirm else "None",
            )
            return error(40003, "TASK_NOT_WAITING_CONFIRMATION: 任务不在等待确认状态")

    # CPS-05: Task Resume Scope Integrity Check
    # 校验 task.context_workspace_key 与所属 Conversation 派生值一致
    # Legacy Task（context_workspace_key=None 且 conversation_id=None）允许跳过
    from app.context_engine.scope.task_scope_validator import (
        ScopeIntegrityError,
        validate_task_conversation_scope,
    )
    try:
        await validate_task_conversation_scope(
            session,
            task_id=task_internal_id,
            task_public_id=task_id,
            task_context_workspace_key=getattr(task, "context_workspace_key", None),
            task_conversation_id=getattr(task, "conversation_id", None),
            task_user_id=getattr(task, "user_id", None),
            current_user_id=current_user.internal_id,
        )
    except ScopeIntegrityError as exc:
        logger.warning(
            "CPS-05 Scope Integrity 声明失败 | task=%s | detail=%s",
            task_id, exc.detail,
        )
        return error(50003, f"SCOPE_INTEGRITY_ERROR: {exc.detail}")

    result = await service.confirm_sections(task_id, sections, current_user.internal_id)

    # Store section_confirm_config in the task's context_json for
    # the post-confirmation SSE endpoint to read
    if task_ctx_json_raw or True:  # 走过流程就必然要写
        ctx_dict = merge_section_confirm_config(task_ctx_json_raw, sections)
        ctx_str = json.dumps(ctx_dict, ensure_ascii=False, default=str)
        from sqlalchemy import text
        await session.execute(
            text("UPDATE agent_tasks SET task_context_json = :cj WHERE id = :tid"),
            {"cj": ctx_str, "tid": task_internal_id},
        )
        await session.flush()

    # Phase 2.9A.2: confirm → 入队 Resume ExecutionRequest
    # Worker 领取后走 v3 Command(resume)。幂等键防双击重复入队。
    from app.repositories.agent_execution_request_repository import (
        AgentExecutionRequestRepository,
    )
    graph_name = getattr(task, "graph_name", None)
    graph_version = getattr(task, "graph_version", None)
    outbox_repo = AgentExecutionRequestRepository(session)
    await outbox_repo.enqueue_new_task(
        task_public_id=task_id,
        task_internal_id=task_internal_id,
        engine_type=engine_type,
        request_type="resume",
        graph_name=graph_name,
        graph_version=graph_version,
        payload={
            # Phase 2.9A.5:source 唯一规范值是 "user"(与
            # LangGraphRunCoordinator._validate_section_decision 兼容)。
            # "user_confirm" 只作为 producer provenance 字段保留,
            # 不进入 Coordinator decision.source。
            "source": "user",
            "confirm_source_detail": "user_confirm",
            "sections": sections,
            "confirmation_id": str(latest_confirm.public_id) if latest_confirm else None,
        },
        idempotency_key=f"{task_id}|resume|user",
    )

    result["events_url"] = f"/api/agent/tasks/{task_id}/events-post-confirm"
    return success(result)


@router.post("/tasks/{task_id}/preparation-clarification")
async def submit_preparation_clarification(
    body: PreparationClarificationRequest,
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Persist task-only gap answers and queue the matching graph resume."""
    if len(body.answers) > 3 or len(body.conservative_gap_ids) > 3:
        return error(40001, "CLARIFICATION_TOO_LARGE: 最多提交 3 条补充信息")
    if any(len(key) > 80 or len(value.strip()) > 2000 for key, value in body.answers.items()):
        return error(40001, "CLARIFICATION_INVALID: 补充信息长度不合法")

    task_repo = AgentTaskRepository(session)
    task = await task_repo.get_by_public_id(task_id)
    if task is None:
        return error(40401, "任务不存在")
    if task.user_id != current_user.internal_id:
        return error(40301, "FORBIDDEN: 无权访问该任务")
    if str(getattr(task, "engine_type", "") or "").lower() != "langgraph":
        return error(40910, "MIGRATION_REQUIRED: historical Legacy task execution is unsupported")
    if task.status != "waiting_user_confirm":
        return error(40003, "TASK_NOT_WAITING_CONFIRMATION: 任务不在等待补充状态")

    from app.context_engine.scope.task_scope_validator import (
        ScopeIntegrityError,
        validate_task_conversation_scope,
    )
    try:
        await validate_task_conversation_scope(
            session,
            task_id=task.id,
            task_public_id=task_id,
            task_context_workspace_key=getattr(task, "context_workspace_key", None),
            task_conversation_id=getattr(task, "conversation_id", None),
            task_user_id=getattr(task, "user_id", None),
            current_user_id=current_user.internal_id,
        )
    except ScopeIntegrityError as exc:
        return error(50003, f"SCOPE_INTEGRITY_ERROR: {exc.detail}")

    from app.repositories.confirmation_repository import ConfirmationRepository
    from app.utils.datetime import utcnow

    confirm_repo = ConfirmationRepository(session)
    pending = await confirm_repo.get_pending_by_task(task.id)
    response = {
        "answers": {key: value.strip() for key, value in body.answers.items() if value.strip()},
        "conservative_gap_ids": list(dict.fromkeys(body.conservative_gap_ids)),
        "source": "user",
    }
    if pending is None or pending.confirmation_type != "preparation_clarification":
        return error(40902, "CLARIFICATION_NOT_PENDING: 当前任务没有待处理的补充卡片")
    await confirm_repo.confirm(pending.id, response, utcnow())

    outbox_repo = AgentExecutionRequestRepository(session)
    await outbox_repo.enqueue_new_task(
        task_public_id=task_id,
        task_internal_id=task.id,
        engine_type="langgraph",
        request_type="preparation_clarification",
        graph_name=getattr(task, "graph_name", None),
        graph_version=getattr(task, "graph_version", None),
        payload={
            "source": "user",
            "answers": response["answers"],
            "conservative_gap_ids": response["conservative_gap_ids"],
            "confirmation_id": pending.public_id,
        },
        idempotency_key=f"{task_id}|preparation_clarification|{pending.public_id}",
    )
    await session.commit()
    return success({
        "task_id": task_id,
        "status": "resuming",
        "events_url": f"/api/agent/tasks/{task_id}/events",
    })


@router.get("/tasks/{task_id}/pending-confirmation")
async def get_pending_confirmation(
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = AgentTaskService(session)
    result = await service.get_pending_confirmation(task_id, current_user.internal_id)
    if result is None:
        return error(40401, "没有待处理的确认")
    return success(result)


@router.get("/tasks/{task_id}/confirmed-confirmations")
async def list_confirmed_confirmations(
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Expose completed confirmation decisions for a restored task timeline."""
    service = AgentTaskService(session)
    confirmations = await service.list_confirmed_confirmations(
        task_id, current_user.internal_id
    )
    if confirmations is None:
        return error(40401, "任务不存在")
    return success({"confirmations": confirmations})


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    logger.info("取消任务 | 任务=%s | 用户=%s", task_id, current_user.username)
    service = AgentTaskService(session)
    result = await service.cancel(task_id, current_user.internal_id)
    return success(result)


@router.post("/tasks/{task_id}/retry")
async def retry_task(
    body: RetryTaskRequest,
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Retry a failed/cancelled/completed task.

    Creates a new agent_task copying the old one's context (plan_json,
    task_context_json, conversation_id, task_type, user_instruction).
    When ``retry_mode='from_failed_step'`` (default) the latest
    snapshot_id is also inherited so the SSE pre-confirm phase can
    skip already-completed tools.  Optional ``user_instruction`` lets
    the caller supply a new prompt on retry.
    """
    logger.info(
        "重试任务 | 任务=%s | 用户=%s | 模式=%s | 新prompt=%s",
        task_id, current_user.username, body.retry_mode,
        bool(body.user_instruction),
    )
    service = AgentTaskService(session)
    result = await service.retry(
        task_id,
        body.retry_mode,
        current_user.internal_id,
        user_instruction=body.user_instruction,
    )
    return success(result)


@router.get("/tasks/{task_id}/artifacts")
async def list_task_artifacts(
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    service = ArtifactService(session)
    artifacts, total = await service.list_artifacts(task_id, current_user.internal_id)
    return success({"artifacts": artifacts, "total": total})


# ── Phase 2.8C: 动态 Agent API 入口 — incremental 端点 ──────────────
# 用户授权决策 #3:独立 URL 前缀 /incremental + /events-incremental;
# 由 ``ApiDispatcher.dispatch_incremental_task`` / ``dispatch_incremental_resume``
# 调度;agent_tasks.py 仅做 payload 校验 + 引擎决策透传 + SSE 事件流订阅。

from pydantic import BaseModel as _BaseModel  # 局部导入避免污染顶层
from typing import Optional as _Optional


class IncrementalTaskRequest(_BaseModel):
    """Phase 2.8C 增量任务请求体。

    必含:incremental_intent / source_artifact_public_id / modification_idempotency_key。
    ApiDispatcher._validate_incremental_payload 二次校验,本 schema 提供 422 兜底。
    """

    incremental_intent: dict
    source_artifact_public_id: str
    modification_idempotency_key: str
    user_id: _Optional[int] = None
    extra: _Optional[dict] = None


class IncrementalResumeRequest(_BaseModel):
    """Phase 2.8C 增量任务恢复请求体(用户提交 decision)。"""

    kind: str = "incremental_resume"
    decision: dict
    extra: _Optional[dict] = None


@router.post("/tasks/{task_id}/incremental")
async def submit_incremental_task(
    body: IncrementalTaskRequest,
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
):
    """Phase 2.8C — 提交增量任务(动态 Agent)。

    body: 见 ``IncrementalTaskRequest``;task_id 来自 path。
    调用 ``ApiDispatcher.dispatch_incremental_task`` 走 LangGraph 增量路径;
    返回 dispatch_outcome + 事件订阅 URL ``/tasks/{id}/events-incremental``。
    """
    api_dispatcher = _resolve_api_dispatcher_from_request(request)
    if api_dispatcher is None:
        return error(50301, "DISPATCHER_UNAVAILABLE: 增量任务调度器未就绪")
    task = await AgentTaskRepository(session).get_owned_task(
        task_id, current_user.internal_id
    )
    if task is None:
        return error(40404, "TASK_NOT_FOUND: 任务不存在或无权限")
    payload = {
        "task_id": task_id,
        "incremental_intent": body.incremental_intent,
        "source_artifact_public_id": body.source_artifact_public_id,
        "modification_idempotency_key": body.modification_idempotency_key,
        "user_id": body.user_id or current_user.internal_id,
        "extra": body.extra or {},
    }
    outcome = await api_dispatcher.dispatch_incremental_task(
        task_public_id=task_id,
        task_engine_type=task.engine_type or "langgraph",
        payload=payload,
    )
    return success({
        "task_id": task_id,
        "status": "submitted",
        "events_url": f"/api/agent/tasks/{task_id}/events-incremental",
        "dispatch_outcome": {
            "engine": outcome.engine,
            "fallback_used": outcome.fallback_used,
            "fallback_reason": outcome.fallback_reason,
        },
    })


@router.post("/tasks/{task_id}/incremental-resume")
async def submit_incremental_resume(
    body: IncrementalResumeRequest,
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
    request: Request = None,  # type: ignore[assignment]
):
    """Phase 2.8C — 增量任务恢复(用户提交 decision)。"""
    if body.kind != "incremental_resume":
        return error(40001, "INVALID_KIND: kind 必须为 'incremental_resume'")
    if not isinstance(body.decision, dict):
        return error(40002, "INVALID_DECISION: decision 必须为 dict")
    api_dispatcher = _resolve_api_dispatcher_from_request(request)
    if api_dispatcher is None:
        return error(50301, "DISPATCHER_UNAVAILABLE: 增量任务调度器未就绪")
    task = await AgentTaskRepository(session).get_owned_task(
        task_id, current_user.internal_id
    )
    if task is None:
        return error(40404, "TASK_NOT_FOUND: 任务不存在或无权限")
    payload = {
        "task_id": task_id,
        "kind": "incremental_resume",
        "decision": body.decision,
        "user_id": current_user.internal_id,
        "extra": body.extra or {},
    }
    outcome = await api_dispatcher.dispatch_incremental_resume(
        task_public_id=task_id,
        task_engine_type=task.engine_type or "langgraph",
        payload=payload,
    )
    return success({
        "task_id": task_id,
        "status": "resumed",
        "events_url": f"/api/agent/tasks/{task_id}/events-incremental",
        "dispatch_outcome": {
            "engine": outcome.engine,
            "fallback_used": outcome.fallback_used,
            "fallback_reason": outcome.fallback_reason,
        },
    })


@router.get("/tasks/{task_id}/events-incremental")
async def task_events_incremental_sse(
    task_id: str = Path(...),
    current_user: UserProfile = Depends(get_current_user),
    request: Request = None,  # type: ignore[assignment]
):
    """Phase 2.8C — 增量任务事件流 SSE(镜像 /events 结构)。

    与 pre-confirm /events 端点的差异:
    * 引擎决策固定走 incremental → ``adapter.run_incremental``;
    * 事件名复用现有 ``tool_call_*`` / ``node_*`` / ``task_*`` 命名(守禁令 #7);
    * LiveEventBus 已支持多订阅(2.6),增量事件流独立 channel 不冲突。

    R3 修复:
    * 订阅 ``LiveEventBus``(由 ``LiveAgentEventSink`` 写入)而不是 legacy
      ``event_publisher``,确保 ``incremental_started`` / ``incremental_decision_made``
      / ``incremental_completed`` / ``incremental_failed`` 等事件能到达前端。
    * 加 while 循环持续消费 queue,直到 ``task.status`` 落到 boundary。
    * generator 不持有 ORM session,Last-Event-ID 补发 + status 轮询都走独立短 Session。
    """

    from sqlalchemy import text as _sa_text

    # ── 阶段 1:鉴权 + 取 task_internal_id(独立短 Session,立刻 close) ──
    async with AsyncSessionLocal() as setup_session:
        task_row = (
            await setup_session.execute(
                _sa_text(
                    "SELECT id, conversation_id "
                    "FROM agent_tasks "
                    "WHERE public_id = :pid AND deleted_at IS NULL"
                ),
                {"pid": task_id},
            )
        ).first()
        if not task_row:
            from fastapi import HTTPException as _HTTPException
            raise _HTTPException(status_code=404, detail="Task not found")

        (
            task_internal_id,
            task_conversation_id,
        ) = task_row

        user_internal_id_primitive = current_user.internal_id

    # ── 阶段 2:event_stream generator ──
    async def event_stream():
        # 订阅 LiveEventBus(不是 legacy event_publisher)
        queue, unsubscribe = await _subscribe_task_events(
            request=request,
            task_id=task_id,
        )

        # 推送 stream_phase_done 信号(标记订阅成功)
        yield (
            f"event: stream_phase_done\n"
            f"data: {json.dumps({'phase': 'incremental', 'task_id': task_id, 'status': 'subscribed'}, ensure_ascii=False)}\n\n"
        )

        # Last-Event-ID 断线补发
        after_seq = 0
        if request is not None:
            try:
                last_event_id = (
                    request.headers.get("last-event-id")
                    if hasattr(request, "headers") else None
                )
            except Exception:  # noqa: BLE001
                last_event_id = None
            if last_event_id and str(last_event_id).isdigit():
                after_seq = int(last_event_id)

        if after_seq > 0:
            try:
                async with AsyncSessionLocal() as replay_session:
                    replay_result = await replay_session.execute(
                        _sa_text(
                            "SELECT event_type, sequence_no, payload_json, "
                            "title, content, public_id, created_at "
                            "FROM agent_events "
                            "WHERE task_id = :iid AND sequence_no IS NOT NULL "
                            "AND sequence_no > :after_seq "
                            "ORDER BY sequence_no ASC LIMIT 200"
                        ),
                        {"iid": task_internal_id, "after_seq": after_seq},
                    )
                    replay_rows = replay_result.fetchall()
                for r in replay_rows:
                    evt = {
                        "event_type": r[0],
                        "sequence_no": r[1],
                        "payload": json.loads(r[2]) if r[2] else {},
                        "title": r[3],
                        "content": r[4],
                        "event_id": r[5],
                        "created_at": str(r[6]) if r[6] else None,
                        "task_id": task_id,
                    }
                    seq_no = evt.get("sequence_no")
                    id_line = f"id: {int(seq_no)}\n" if seq_no else ""
                    yield (
                        f"{id_line}"
                        f"event: {evt['event_type']}\n"
                        f"data: {json.dumps(evt, ensure_ascii=False, default=str)}\n\n"
                    )
                logger.info(
                    "SSE增量 | 断线补发 | task=%s | after_seq=%d | 补发=%d 条",
                    task_id, after_seq, len(replay_rows),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SSE增量 | 断线补发失败(degraded) | task=%s | err=%s",
                    task_id, exc,
                )

        # 流式主循环:从 queue 持续读事件,直到 task.status 到 boundary
        incremental_done = False
        try:
            while not incremental_done:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    data_str = json.dumps(event, ensure_ascii=False, default=str)
                    seq_no = event.get("sequence_no")
                    id_line = f"id: {int(seq_no)}\n" if seq_no else ""
                    yield (
                        f"{id_line}"
                        f"event: {event.get('event_type', 'message')}\n"
                        f"data: {data_str}\n\n"
                    )

                    # 终态事件到达 → 标记流结束
                    evt_type = str(event.get("event_type") or "")
                    if evt_type in ("task_completed", "task_failed",
                                     "task_cancelled", "incremental_completed",
                                     "incremental_failed"):
                        incremental_done = True
                except asyncio.TimeoutError:
                    # Phase 1 (Step 6): heartbeat reads task status via
                    # TaskStatusCache — see ``_read_task_status_for_sse``.
                    current_status = await _read_task_status_for_sse(
                        task_public_id=task_id,
                        task_internal_id=task_internal_id,
                    )

                    if str(current_status or "").lower() in _PRE_CONFIRM_STREAM_BOUNDARY_STATUSES:
                        incremental_done = True
                    else:
                        yield ": keep-alive\n\n"

            # Drain any remaining events
            while not queue.empty():
                event = queue.get_nowait()
                data_str = json.dumps(event, ensure_ascii=False, default=str)
                yield (
                    f"event: {event.get('event_type', 'message')}\n"
                    f"data: {data_str}\n\n"
                )

            # Phase 1 (Step 6): final-status read via TaskStatusCache.
            final_status = await _read_task_status_for_sse(
                task_public_id=task_id,
                task_internal_id=task_internal_id,
            )

            phase_status = (
                "failed" if str(final_status or "").lower() in {"failed", "cancelled"}
                else "completed"
            )
            yield (
                f"event: stream_phase_done\ndata: "
                f"{json.dumps({'phase': 'incremental', 'task_id': task_id, 'status': phase_status}, ensure_ascii=False)}\n\n"
            )
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            try:
                await unsubscribe()
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── F025-ext: format-loss confirmation ──────────────────────────────


@router.post("/tasks/{task_id}/format-loss-decision")
async def submit_format_loss_decision(
    body: FormatLossDecisionRequest,
    task_id: str = Path(...),
    request: Request = None,  # type: ignore[assignment]
    current_user: UserProfile = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Submit the user's accept/retry decision for an outstanding
    format-loss confirm dialog.

    The task must belong to ``current_user`` and must be a LangGraph task.
    The decision is resumed through the persisted graph interrupt; historical
    tasks from the retired engine return a migration-required response.
    """
    if body.decision not in ("accept", "retry"):
        return error(
            40001,
            f"INVALID_DECISION: 未知决策：{body.decision!r}（仅接受 accept / retry）",
        )

    # 1. Load task + verify ownership
    task_repo = AgentTaskRepository(session)
    task = await task_repo.get_by_public_id(task_id)
    if not task or task.deleted_at is not None:
        return error(40401, "TASK_NOT_FOUND: 任务不存在")
    if task.user_id != current_user.internal_id:
        return error(40301, "FORBIDDEN: 无权访问该任务")

    # CPS-05: Task Resume Scope Integrity Check
    from app.context_engine.scope.task_scope_validator import (
        ScopeIntegrityError,
        validate_task_conversation_scope,
    )
    try:
        await validate_task_conversation_scope(
            session,
            task_id=task.id,
            task_public_id=task_id,
            task_context_workspace_key=getattr(task, "context_workspace_key", None),
            task_conversation_id=getattr(task, "conversation_id", None),
            task_user_id=task.user_id,
            current_user_id=current_user.internal_id,
        )
    except ScopeIntegrityError as exc:
        logger.warning(
            "CPS-05 Scope Integrity 声明失败 | task=%s | detail=%s",
            task_id, exc.detail,
        )
        return error(50003, f"SCOPE_INTEGRITY_ERROR: {exc.detail}")

    task_engine_type = (getattr(task, "engine_type", None) or "").strip().lower()
    if task_engine_type != "langgraph":
        return error(
            40910,
            "MIGRATION_REQUIRED: historical Legacy task execution is unsupported",
        )
    if task.status in {"completed", "failed", "cancelled"}:
        return error(
            40003,
            f"TASK_NOT_AWAITING_DECISION: 任务当前状态为 {task.status}，无需决策",
        )
    api_dispatcher = _resolve_api_dispatcher_from_request(request)
    if api_dispatcher is None:
        return error(
            50301,
            "AGENT_RUNTIME_NOT_READY: Agent runtime is not ready (api_dispatcher not mounted)",
        )

    async def _resume_langgraph_format_loss() -> None:
        try:
            await api_dispatcher.dispatch_format_loss_decision(
                task_public_id=task_id,
                task_engine_type=task_engine_type,
                payload={
                    "task_id": task_id,
                    "graph_run_id": getattr(task, "graph_run_id", None),
                    "decision": {
                        "kind": "format_loss",
                        "decision": body.decision,
                        "source": "user",
                        "note": body.note,
                    },
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "format-loss-decision: LangGraph resume failed | task=%s",
                task_id,
            )

    asyncio.create_task(_resume_langgraph_format_loss())
    next_action = "re_export" if body.decision == "retry" else "complete"
    return success(
        FormatLossDecisionResponse(
            task_id=task_id,
            decision=body.decision,
            next_action=next_action,
            fallback_to_accept=False,
        ).model_dump()
    )


# ════════════════════════════════════════════════════════════════════════════════
# Agent Task API (任务详情 / SSE 事件流 / 章节确认 / 取消):
#
#   路由清单:
#     GET  /api/v1/agent/tasks/{task_public_id}              → 任务详情(GraphState 快照)
#     GET  /api/v1/agent/tasks/{task_public_id}/events       → SSE 实时事件流(LiveEventBus 订阅)
#     GET  /api/v1/agent/tasks/{task_public_id}/event-list   → 历史事件(分页)
#     POST /api/v1/agent/tasks/{task_public_id}/confirm-section → 章节策略确认
#     POST /api/v1/agent/tasks/{task_public_id}/confirm-format-loss → 格式丢失确认
#     POST /api/v1/agent/tasks/{task_public_id}/cancel       → 取消任务
#     GET  /api/v1/agent/tasks/{task_public_id}/artifacts   → 列出该任务的产物(.docx 等)
#
#   链路:
#     SSE 流:
#       LiveEventBus.subscribe(task_id) → yield event 一帧帧 →
#       前端 useTaskEvents composable 通过 SSE 收到,
#       reducer 把 raw_event 归一化为 message / tool_call / artifact_download。
#
#     Confirm 路径:
#       POST /confirm-section →
#         AgentTaskService.persist_human_confirmation + emit NEED_USER_CONFIRM
#         → LangGraphRunCoordinator.resume_section_confirmation(decision)
#
# 关键约束(供开发者速查):
#   - SSE 必须 client disconnect handling,断开不能 rollback 阻塞域;
#   - checkpointer 决定 thread_id=task_public_id,不是内部 int id;
#   - cancel 必须 idempotent:已 cancelled / failed 状态再 cancel → 200 幂等返回;
#   - artifacts list 必须仅返回 owner 可见的(强 ACL)。
