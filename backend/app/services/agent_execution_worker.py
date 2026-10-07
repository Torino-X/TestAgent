"""AgentExecutionWorker — Phase 2.8R-B Outbox Worker。

主要职责:
  1. 后台循环:每 ``poll_interval_seconds`` 调
     ``AgentExecutionRequestRepository.claim_next(lease_owner=...)`` 拉 next。
  2. 调 ``ApiDispatcher.dispatch_new_task``(或 ``dispatch_resume``、
     ``dispatch_confirm``,取决于 ``request_type``)实际执行任务。
  3. 显式 mark_completed / mark_failed / release_lease_for_retry 收尾。

设计决策(对应 docs/35 §2.3):
  * Worker 主循环 = asyncio.Task;启动后无外部驱动,**只在 lifespan 启动期挂**。
  * SSE 端点不再触发 dispatch — Worker 完全负责启动 task execution。
  * Worker 自身不 commit 事务;每次循环用 ephemeral ``AsyncSessionLocal()``,
    ``claim_next`` 后立刻处理,处理完 commit。
  * 多 Worker E2E 测试基建在 Phase 2.8R-G 阶段(需要真实 PG + Redis)。
  * Phase 2.8R-B 单进程 worker 与 lifespan 启动:进入 main.py 时挂背景
    asyncio 任务即可。

不在范围:
  * ``incremental_task`` 与 ``new_task`` 共用 outbox row 派发入口,
    由 dispatcher 根据 graph_name / payload 选择增量子图。
  ❌ ``repair_task`` — 走相同路径但单独 request_type=repair_task,
    留给后续扩展。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.repositories.agent_execution_request_repository import (
    AgentExecutionRequestRepository,
)

logger = logging.getLogger(__name__)

_MYSQL_CONNECTION_ERROR_CODES = frozenset({2003, 2006, 2013})
_MYSQL_CONNECTION_BACKOFF_MAX_SECONDS = 30.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_lease_owner() -> str:
    """默认 lease owner: ``pid@hostname``。

    多 worker 可识别同一进程;跨进程 lease 冲突通过 SKIP LOCKED 自然错开。
    """
    return f"{os.getpid()}@{socket.gethostname()}"


def database_retry_delay_seconds(
    exc: BaseException,
    *,
    failure_count: int,
    poll_interval: float,
) -> float | None:
    """Return bounded backoff only for transient MySQL connection failures."""
    if not isinstance(exc, OperationalError):
        return None
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ()) or ()
    code = args[0] if args else None
    text = str(exc).lower()
    if code not in _MYSQL_CONNECTION_ERROR_CODES and not any(
        marker in text
        for marker in (
            "can't connect to mysql server",
            "connection refused",
            "connection reset",
        )
    ):
        return None
    exponent = max(int(failure_count) - 1, 0)
    base_delay = max(float(poll_interval), 1.0)
    return min(base_delay * (2 ** exponent), _MYSQL_CONNECTION_BACKOFF_MAX_SECONDS)


class AgentExecutionWorker:
    """Phase 2.8R-B Outbox Worker。

    工作循环:
      ```
      while not stop_event.is_set():
          rows = await self._poll_once()
          if rows is None:  # 空队列
              await asyncio.sleep(poll_interval)
          else:
              # process each
      ```
    """

    def __init__(
        self,
        *,
        api_dispatcher: Any | None = None,
        poll_interval_seconds: float = 1.0,
        lease_owner: str | None = None,
        max_runtime_seconds_per_task: int = 600,
        cache_maintenance_service: Any | None = None,
    ) -> None:
        self._dispatcher = api_dispatcher
        self._poll_interval = float(poll_interval_seconds)
        self._lease_owner = lease_owner or _default_lease_owner()
        self._max_runtime = int(max_runtime_seconds_per_task)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._leases_reaped_total = 0
        self._database_connection_failures = 0
        # Phase 2.9A.8:Worker 在 dispatcher=None 时不再无限 release_lease
        # + 暂挂任务 — 这属于启动配置错误,应一次性退出,不重复制造日志。
        self._dispatcher_missing_logged = False
        # Phase 0 — opportunistic cache maintenance trigger.  When the
        # outbox is empty the worker has slack; let it run a single
        # tick of ``CacheMaintenanceService.run_once`` to keep the
        # library purge sweep making forward progress without spawning
        # a separate asyncio task.  Failures are swallowed inside the
        # service so the worker loop is never blocked.
        self._cache_maintenance_service = cache_maintenance_service
        self._last_cache_maintenance_at: float | None = None

    # ── 生命周期 ────────────────────────────────────────────

    def attach_dispatcher(self, dispatcher: Any) -> None:
        """Lifespan 启动时挂上;测试可注入 spy。"""
        self._dispatcher = dispatcher

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_forever(), name="agent-exec-worker")
        logger.info(
            "AgentExecutionWorker start | lease_owner=%s | poll=%.1fs",
            self._lease_owner, self._poll_interval,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        logger.info("AgentExecutionWorker stopped")

    # ── 主循环 ──────────────────────────────────────────────

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = await self._poll_once()
                if self._database_connection_failures:
                    logger.info(
                        "AgentExecutionWorker database connectivity recovered | consecutive_failures=%d",
                        self._database_connection_failures,
                    )
                    self._database_connection_failures = 0
                if processed == 0:
                    # Phase 0 — opportunistic cache maintenance tick on
                    # every idle poll.  The service is fail-soft so we
                    # never block the worker loop on errors here.
                    if self._cache_maintenance_service is not None:
                        try:
                            await self._cache_maintenance_service.run_once()
                        except Exception as maint_exc:  # noqa: BLE001
                            logger.debug(
                                "AgentExecutionWorker opportunistic "
                                "cache maintenance skipped: %s", maint_exc,
                            )
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait(), timeout=self._poll_interval
                        )
                    except asyncio.TimeoutError:
                        pass
                else:
                    # 至少处理了一行 → 立即进下一轮,避免长尾延迟
                    pass
            except Exception as exc:  # noqa: BLE001 — 防止单次异常杀掉循环
                next_failure_count = self._database_connection_failures + 1
                delay = database_retry_delay_seconds(
                    exc,
                    failure_count=next_failure_count,
                    poll_interval=self._poll_interval,
                )
                if delay is not None:
                    self._database_connection_failures = next_failure_count
                    logger.warning(
                        "AgentExecutionWorker database connection unavailable | "
                        "consecutive_failures=%d | retry_in=%.1fs | error=%s",
                        next_failure_count,
                        delay,
                        exc,
                    )
                else:
                    logger.exception(
                        "AgentExecutionWorker loop iteration failed: %s", exc
                    )
                    delay = self._poll_interval
                # back-off on unexpected errors
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=delay
                    )
                except asyncio.TimeoutError:
                    pass

    async def _poll_once(self) -> int:
        """单次轮询:领 1 条任务 → 委派 → 标记状态。Returns 处理条数。"""
        async with AsyncSessionLocal() as session:
            repo = AgentExecutionRequestRepository(session)
            row = await repo.claim_next(lease_owner=self._lease_owner)
            if row is None:
                # 顺手 reap 过期 lease(可在没有任务时一并扫)
                try:
                    reaped = await repo.reap_expired_leases()
                    if reaped:
                        self._leases_reaped_total += reaped
                        await session.commit()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Worker reap_expired_leases skipped: %s", exc)
                return 0

            # ── Phase 2.8R-K fix: capture ORM attributes to primitives ──
            # row 是 claim_next() 返回的 ORM 对象;一旦后续 commit 或 expire
            # (Orchestrator.run 内部会 commit checkpoint),row 上的字符串属性
            # 再次访问会触发 lazy reload → MissingGreenlet(worker loop 上下文
            # 没有 active greenlet)。提前把后续路径要用的字段 capture 成
            # primitive(str/int/dict),后续 _poll_once 内只用 primitive。
            # 用 getattr 兜底(None)避免 ORM 字段缺失触发 AttributeError —
            # 真实 AgentExecutionRequest 行所有字段都 NOT NULL,这里只是
            # 防止测试/异常路径的边界 case。
            row_id: int = int(row.id)
            row_task_id: int = int(row.task_id)
            row_request_type: str = str(row.request_type or "new_task")
            row_engine_type: str = str(row.engine_type or "")
            row_public_id: str = str(getattr(row, "public_id", "") or "")
            row_payload_json: dict = dict(getattr(row, "payload_json", None) or {})
            row_graph_name: str | None = getattr(row, "graph_name", None)
            row_graph_version: str | None = getattr(row, "graph_version", None)
            dispatch_row = SimpleNamespace(
                id=row_id,
                task_id=row_task_id,
                public_id=row_public_id,
                request_type=row_request_type,
                engine_type=row_engine_type,
                payload_json=row_payload_json,
                graph_name=row_graph_name,
                graph_version=row_graph_version,
            )

            # row 委派到 dispatcher
            if self._dispatcher is None:
                # Phase 2.9A.8:启动配置错误,不是暂时性任务错误。
                # 第一次发现时记录 + 标失败,后续 silent skip(避免无限 release_lease)。
                if not self._dispatcher_missing_logged:
                    logger.warning(
                        "AgentExecutionWorker: dispatcher 未挂载,不再轮询; "
                        "ready=False | 不增加 attempt | 不暂挂任务。 "
                        "LangGraph 入口不可用,需运维介入 | "
                        "execution_id=%d | task_id=%d | type=%s",
                        row_id, row_task_id, row_request_type,
                    )
                    self._dispatcher_missing_logged = True
                # Phase 2.9A.8:行 mark_failed + WORKER_DISPATCHER_NOT_READY,
                # 不再 release_lease_for_retry(避免下一轮再次领取、再次失败)。
                try:
                    await repo.mark_failed(
                        execution_id=row_id,
                        error_code="WORKER_DISPATCHER_NOT_READY",
                        error_message=(
                            "AgentExecutionWorker has no dispatcher mounted; "
                            "langgraph entry is unavailable"
                        ),
                    )
                    await session.commit()
                except Exception as fail_exc:  # noqa: BLE001
                    logger.warning(
                        "AgentExecutionWorker: mark_failed(no dispatcher) 失败 | "
                        "execution_id=%d | err=%s",
                        row_id, fail_exc,
                    )
                return 1

            # 我们直接把 row.id 当 execution_id;task public_id 需要反查
            task_public_id = await self._resolve_task_public_id(session, row_task_id)
            if task_public_id is None:
                # 找不到(被删?),直接 mark_failed
                await repo.mark_failed(
                    execution_id=row_id,
                    error_code="TASK_NOT_FOUND",
                    error_message="agent_tasks row not found",
                )
                await session.commit()
                return 1

            if row_engine_type.strip().lower() != "langgraph":
                from sqlalchemy import text as _sa_text

                error_message = (
                    "migration-required: historical Legacy task execution is unsupported"
                )
                logger.error(
                    "UNSUPPORTED_LEGACY_TASK | migration-required | execution_id=%d | "
                    "task=%s | engine_type=%s",
                    row_id,
                    task_public_id,
                    row_engine_type or "missing",
                )
                await repo.mark_failed(
                    execution_id=row_id,
                    error_code="UNSUPPORTED_LEGACY_TASK",
                    error_message=error_message,
                )
                await session.execute(
                    _sa_text(
                        "UPDATE agent_tasks SET runtime_status=:status "
                        "WHERE id=:task_id"
                    ),
                    {"status": "migration_required", "task_id": row_task_id},
                )
                await session.commit()
                await self._emit_resume_failed_event(
                    task_public_id=task_public_id,
                    error_code="UNSUPPORTED_LEGACY_TASK",
                    error_message=error_message,
                )
                return 1

            # 构造 LangGraph context；historical Legacy rows were rejected above.
            # Phase 2.9A.2:把 payload 的关键字段(sections/source)提升到顶层,
            # 让 dispatch_resume 可以扁平化读取。
            payload_dict = row_payload_json if isinstance(row_payload_json, dict) else {}
            trace_carrier = payload_dict.get("_observability") if isinstance(payload_dict.get("_observability"), dict) else {}
            try:
                from app.core.observability import extract_trace_context
                worker_parent_trace = extract_trace_context(trace_carrier.get("traceparent"), trace_carrier.get("tracestate"))
            except Exception:
                worker_parent_trace = None
            ctx: Any = {
                "task_public_id": task_public_id,
                "task_internal_id": row_task_id,
                "execution_id": row_id,
                "request_type": row_request_type,
                "engine_type": row_engine_type,
                "graph_name": row_graph_name,
                "graph_version": row_graph_version,
                "payload": payload_dict,
                "sections": payload_dict.get("sections"),
                "source": payload_dict.get("source"),
                "parent_trace_id": getattr(worker_parent_trace, "trace_id", None),
            }

            try:
                # Release the claim_next row lock before long-running dispatch work.
                await session.commit()

                if row_request_type in {"new_task", "incremental_task"}:
                    # Phase 2.8R-K:走 dispatch_from_outbox_row 修 Worker → dict → ctx 缺失 bug
                    # 增量任务也必须走这里,再由 dispatcher 按 graph_name
                    # 路由到 incremental 子图。
                    from app.core.observability import start_span
                    with start_span("agent.worker", {"testagent.task_id": task_public_id, "agent.request_type": row_request_type}, parent=worker_parent_trace) as worker_span:
                        async with AsyncSessionLocal() as dispatch_session:
                            outcome = await self._dispatcher.dispatch_from_outbox_row(
                                row=dispatch_row,
                                session=dispatch_session,
                            )
                elif row_request_type == "resume":
                    from app.core.observability import start_span
                    with start_span("agent.worker", {"testagent.task_id": task_public_id, "agent.request_type": row_request_type}, parent=worker_parent_trace):
                        outcome = await self._dispatcher.dispatch_resume(
                            task_public_id=task_public_id,
                            task_engine_type=row_engine_type,
                            context=ctx,
                        )
                elif row_request_type == "preparation_clarification":
                    from app.core.observability import start_span
                    with start_span("agent.worker", {"testagent.task_id": task_public_id, "agent.request_type": row_request_type}, parent=worker_parent_trace):
                        outcome = await self._dispatcher.dispatch_preparation_clarification(
                            task_public_id=task_public_id,
                            task_engine_type=row_engine_type,
                            payload={
                                "task_id": task_public_id,
                                "graph_run_id": payload_dict.get("graph_run_id"),
                                "decision": {
                                    "kind": "preparation_clarification",
                                    "answers": payload_dict.get("answers") or {},
                                    "conservative_gap_ids": payload_dict.get("conservative_gap_ids") or [],
                                    "source": payload_dict.get("source") or "user",
                                },
                            },
                        )
                elif row_request_type == "confirm":
                    from app.core.observability import start_span
                    with start_span("agent.worker", {"testagent.task_id": task_public_id, "agent.request_type": row_request_type}, parent=worker_parent_trace):
                        outcome = await self._dispatcher.dispatch_confirm(
                            task_public_id=task_public_id,
                            task_engine_type=row_engine_type,
                            context=ctx,
                        )
                else:
                    # 未知 request_type:标记失败,避免毒丸
                    await repo.mark_failed(
                        execution_id=row_id,
                        error_code="UNKNOWN_REQUEST_TYPE",
                        error_message=f"unknown request_type={row_request_type!r}",
                    )
                    await session.commit()
                    return 1

                # 成功 dispatch:API dispatcher 内部"已派"不等于"已完成",
                # 真实完成时由上层调用 complete_task;此处保守只标终态。
                # 2.8R-B 范围内:outcome 非异常即视为派发成功,
                # 真实完成事件由 SSE / event_publisher 处理。
                await self._mark_completed_with_retry(execution_id=row_id)
                logger.info(
                    "AgentExecutionWorker: dispatched | execution_id=%d | task=%s | "
                    "engine=%s",
                    row_id, task_public_id,
                    getattr(outcome, "engine", row_engine_type),
                )
                return 1

            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "AgentExecutionWorker dispatch failed | execution_id=%d | "
                    "task=%s | err=%s",
                    row_id, task_public_id, exc,
                )
                # Phase 2.8R-D:区分可重试 vs 不可重试异常。
                # 编程错误(TypeError/AttributeError/NotImplementedError/KeyError
                # 等)和接口契约错误永远不可重试 — mark_failed 立即终止。
                # 只有暂时性基础设施异常(IOError/OperationalError/Timeout等)
                # 释放 lease 让 worker 重新领取,但有最大尝试次数上限。
                # Phase 2.9A.5:ValueError 也是不可重试的业务契约错误。
                # 包括:
                # - section resume source 非法(user_confirm 不允许)
                # - sections 格式错误
                # - confirmation_id 缺失
                # - thread_id 契约错误
                # 这些都是确定性错误,重试只消耗 max_attempts,不解决问题。
                _NON_RETRYABLE = (
                    TypeError, AttributeError, NotImplementedError, KeyError,
                    ValueError,
                )
                is_programming_error = isinstance(exc, _NON_RETRYABLE)
                if is_programming_error:
                    # Phase 2.9A.7:Resume 类错误继承 ValueError,需要精确
                    # 区分错误码,让前端看到明确语义。
                    error_code = self._classify_dispatch_error(exc)
                    logger.error(
                        "AgentExecutionWorker: 不可重试的编程错误 | "
                        "execution_id=%d | task=%s | type=%s | error_code=%s | mark failed",
                        row_id, task_public_id, type(exc).__name__, error_code,
                    )
                    await session.rollback()
                    try:
                        async with AsyncSessionLocal() as fail_session:
                            fail_repo = AgentExecutionRequestRepository(fail_session)
                            await fail_repo.mark_failed(
                                execution_id=row_id,
                                error_code=error_code,
                                error_message=f"{type(exc).__name__}: {str(exc)[:500]}",
                            )
                            await fail_session.commit()
                    except Exception:
                        logger.warning(
                            "AgentExecutionWorker: mark_failed 失败 | execution_id=%d",
                            row_id, exc_info=True,
                        )
                    # 发布用户可见的失败事件(Phase 2.9A.7)— Worker 已知 task_public_id,
                    # 即使 worker 拿不到 ctx,这里也能定位失败任务。
                    try:
                        await self._emit_resume_failed_event(
                            task_public_id=task_public_id,
                            error_code=error_code,
                            error_message=str(exc)[:500],
                        )
                    except Exception:
                        logger.warning(
                            "AgentExecutionWorker: emit resume_failed 事件失败 | "
                            "execution_id=%d",
                            row_id, exc_info=True,
                        )
                    return 1

                # 可重试暂时性异常:检查 attempt_count 上限(max_attempts=3)
                max_attempts = 3
                current_attempt = int(getattr(row, "attempt_count", 0) or 0) + 1
                if current_attempt >= max_attempts:
                    logger.error(
                        "AgentExecutionWorker: 超过最大重试次数 %d | "
                        "execution_id=%d | task=%s | mark failed",
                        max_attempts, row_id, task_public_id,
                    )
                    await session.rollback()
                    try:
                        async with AsyncSessionLocal() as fail_session:
                            fail_repo = AgentExecutionRequestRepository(fail_session)
                            await fail_repo.mark_failed(
                                execution_id=row_id,
                                error_code="EXCEED_MAX_RETRIES",
                                error_message=f"{type(exc).__name__}: {str(exc)[:500]}",
                            )
                            await fail_session.commit()
                    except Exception:
                        logger.warning(
                            "AgentExecutionWorker: mark_failed(retries exceeded) 失败 | "
                            "execution_id=%d",
                            row_id, exc_info=True,
                        )
                    return 1

                # 未超限:释放 lease,backoff 退避
                backoff_seconds = min(5 * (2 ** (current_attempt - 1)), 30)
                await session.rollback()
                await repo.release_lease_for_retry(
                    execution_id=row_id, retry_in_seconds=backoff_seconds,
                )
                await session.commit()
                return 1

    async def _mark_completed_with_retry(self, *, execution_id: int) -> None:
        """Persist completion in a fresh, short-lived transaction."""
        attempts = 3
        for attempt in range(attempts):
            try:
                async with AsyncSessionLocal() as completion_session:
                    completion_repo = AgentExecutionRequestRepository(completion_session)
                    await completion_repo.mark_completed(execution_id=execution_id)
                    await completion_session.commit()
                return
            except OperationalError as exc:
                is_lock_timeout = "1205" in str(exc) or "Lock wait timeout" in str(exc)
                if not is_lock_timeout or attempt == attempts - 1:
                    raise
                logger.warning(
                    "AgentExecutionWorker completion lock timeout | execution_id=%d | retry=%d/%d",
                    execution_id,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(0.15 * (attempt + 1))

    def _classify_dispatch_error(self, exc: Exception) -> str:
        """Phase 2.9A.7: 把 dispatcher / coordinator 抛出的异常映射到 error_code。

        让前端能区分 "Resume 没找到挂起点" / "Resume 后无推进" /
        "Graph 版本找不到" 等具体语义,而不是统一的
        ``DISPATCH_CONTRACT_ERROR``。
        """
        # 局部 import 避免循环
        try:
            from app.agent_runtime.langgraph_run_coordinator import (
                ResumeNoPendingInterruptError,
                ResumeNoProgressError,
                ResumeGraphVersionNotFoundError,
            )
        except ImportError:
            return "DISPATCH_CONTRACT_ERROR"

        if isinstance(exc, ResumeNoPendingInterruptError):
            return "RESUME_NO_PENDING_INTERRUPT"
        if isinstance(exc, ResumeNoProgressError):
            return "RESUME_NO_PROGRESS"
        if isinstance(exc, ResumeGraphVersionNotFoundError):
            return "RESUME_GRAPH_VERSION_NOT_FOUND"
        return "DISPATCH_CONTRACT_ERROR"

    async def _emit_resume_failed_event(
        self,
        *,
        task_public_id: str,
        error_code: str,
        error_message: str,
    ) -> None:
        """Phase 2.9A.7: 发布 RESUME_FAILED 事件,前端立即收到。

        用 SSE 端点都在用的 ``LiveAgentEventSink`` 接口;若 sink 未挂载,
        fallback 到 ``agent_events`` 表直接 INSERT(供 SSE history replay 兜底)。
        """
        try:
            from app.agent_runtime.live_event_sink import get_live_event_sink
            sink = get_live_event_sink()
            if sink is not None:
                # 取 task_internal_id:走短 Session 查询
                async with AsyncSessionLocal() as s:
                    from sqlalchemy import text as _sa_text
                    res = await s.execute(
                        _sa_text("SELECT id FROM agent_tasks WHERE public_id=:p"),
                        {"p": task_public_id},
                    )
                    row = res.first()
                    task_internal_id = int(row[0]) if row else 0
                await sink.emit(
                    task_id=str(task_internal_id),
                    graph_run_id=f"run-{task_internal_id}",
                    node_name="agent_execution_worker",
                    event_type="RESUME_FAILED",
                    title="章节确认恢复失败",
                    content="",
                    payload={
                        "task_public_id": task_public_id,
                        "error_code": error_code,
                        "error_message": error_message[:500],
                    },
                )
                return
        except Exception:
            logger.debug("LiveAgentEventSink emit failed, fallback to direct insert")

        # Fallback:直接 INSERT agent_events
        try:
            from app.agent.enums import AgentEventType
            from sqlalchemy import text as _sa_text
            async with AsyncSessionLocal() as s:
                await s.execute(
                    _sa_text(
                        "INSERT INTO agent_events "
                        "(task_id, event_type, title, content, payload_json, "
                        "sequence_no, created_at) "
                        "VALUES (:tid, :et, :t, :c, :p, "
                        "(SELECT COALESCE(MAX(sequence_no),0)+1 FROM agent_events "
                        "WHERE task_id=:tid), :now)"
                    ),
                    {
                        "tid": task_public_id,  # may be public_id fallback
                        "et": "RESUME_FAILED",
                        "t": "章节确认恢复失败",
                        "c": "",
                        "p": f'{{"error_code":"{error_code}","message":"{error_message[:200]}"}}',
                        "now": _utcnow().isoformat(),
                    },
                )
                await s.commit()
        except Exception as exc:
            logger.debug("Direct insert resume_failed failed: %s", exc)

    async def _resolve_task_public_id(
        self, session: AsyncSession, task_internal_id: int
    ) -> str | None:
        from sqlalchemy import text

        result = await session.execute(
            text("SELECT public_id FROM agent_tasks WHERE id = :tid"),
            {"tid": int(task_internal_id)},
        )
        row = result.first()
        return str(row[0]) if row else None


# ── 进程级 singleton helper ──────────────────────────────────


_worker_singleton: AgentExecutionWorker | None = None


def get_execution_worker() -> AgentExecutionWorker | None:
    """Lifespan 启动时挂 worker;测试可注入自己的实例。

    Returns ``None`` 在 lifespan 没启动 / 没挂载时。
    """
    return _worker_singleton


def set_execution_worker(worker: AgentExecutionWorker | None) -> None:
    """Lifespan + 测试 setter。"""
    global _worker_singleton
    _worker_singleton = worker


__all__ = [
    "AgentExecutionWorker",
    "get_execution_worker",
    "set_execution_worker",
]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Outbox 模式 Worker,Phase 2.8R-B 起正式启用):
#
#   Worker 启动:
#     app/main.py FastAPI lifespan → AgentExecutionWorker.start()
#       → 后台 asyncio.create_task(self._run_loop())
#       → _run_loop 循环:
#           await sleep(poll_interval_seconds)
#           → session = AsyncSessionLocal() (ephemeral, 不跨循环)
#           → AgentExecutionRequestRepository.claim_next(lease_owner=worker_id)
#             → 行级锁;若已被其他 worker 抢走 → 跳过
#             → 命中 → mark_running() 进入 in_progress
#
#   任务派发 (按 request_type 分支):
#     NEW_TASK:
#         → ApiDispatcher.dispatch_new_task(payload)
#           → LangGraphRunCoordinator.ainvoke(v3_graph, ...)
#           → 写 AgentTask / AgentRun / AgentEvent
#     RESUME:
#         → ApiDispatcher.dispatch_resume(task_public_id, payload)
#           → LangGraphRunCoordinator.resume_section_confirmation(...)
#     CONFIRM:
#         → 写 HumanConfirmation 决策
#         → LangGraph 重新拉起 section_confirmation_interrupt
#
#   任务收尾 (Worker 端,不在 Dispatch 端):
#     成功 → mark_completed(commit)
#     失败 → mark_failed(error_code=...)
#     重试 → release_lease_for_retry(下次轮询重新 claim)
#     超时 → lease 自动过期,下次轮询新 worker 接管
#
# 关键约束(供开发者速查):
#   - Worker 是 asyncio single-instance;多进程 worker 模式在 Phase 2.8R-G;
#   - claim_next 必须用 lease_owner(本机 hostname)避免重复跑同一行;
#   - 任何 dispatch 异常不能 raise 进 main loop;只 release_lease + 错误日志;
#   - SSE 推送不在 Worker 范围内,只走 EventRepository.save → LiveEventBus。
