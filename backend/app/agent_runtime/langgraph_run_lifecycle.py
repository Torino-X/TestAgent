"""Durable lifecycle bridge for LangGraph task executions.

The LangGraph coordinator owns graph execution, while this module owns the
database-visible identity and terminal state of an execution.  Keeping that
boundary explicit lets live SSE, task detail APIs, and restored conversations
refer to the same ``AgentRun`` record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V2,
)
from app.models.agent_run import AgentRun
from app.repositories.agent_run_repository import AgentRunRepository
from app.repositories.agent_task_repository import AgentTaskRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id


async def _write_through_task_status(
    task_public_id: str,
    status: str,
    active_run_id: str | None,
    updated_at,
) -> None:
    """Best-effort TaskStatusCache write-through.

    Called AFTER session.commit() for every place that mutates ``agent_tasks
    .status``.  Failure is logged but never raised — the DB is the SoT and
    TTL misses will heal within 30-180s.  Per 设计文档 §13.2.
    """
    try:
        from app.cache.domains.task_cache import get_task_status_cache

        await get_task_status_cache().write_through(
            task_public_id=task_public_id,
            status=status,
            active_run_id=active_run_id,
            updated_at=updated_at,
        )
    except Exception as exc:  # noqa: BLE001 — cache must never break DB success
        import logging
        logging.getLogger(__name__).warning(
            "LangGraphRunLifecycle: task status cache write-through failed | "
            "task=%s | status=%s | %s",
            task_public_id, status, exc,
        )


@dataclass(frozen=True)
class PreparedGraphRun:
    """The durable run identity passed into one graph invocation."""

    graph_run_id: str


class LangGraphRunLifecycle:
    """Create, reuse, pause, finish, or fail the durable run for a task."""

    def __init__(self, *, session_factory: Callable[..., Any]) -> None:
        self._session_factory = session_factory

    async def prepare(
        self,
        task_public_id: str,
        *,
        preferred_graph_run_id: str | None = None,
    ) -> PreparedGraphRun:
        """Return the task's active durable run, creating one only when needed."""
        async with self._session_factory() as session:
            task_repo = AgentTaskRepository(session)
            run_repo = AgentRunRepository(session)
            task = await task_repo.get_by_public_id(task_public_id)
            if task is None:
                raise ValueError(f"Agent task not found: {task_public_id}")

            run = None
            for public_id in (preferred_graph_run_id, task.active_run_id):
                if not public_id:
                    continue
                candidate = await run_repo.get_by_public_id(public_id)
                if candidate is None:
                    continue
                if candidate.task_id != task.id:
                    raise ValueError(
                        f"Agent run {public_id} does not belong to task {task_public_id}"
                    )
                if candidate.status not in {"completed", "failed", "cancelled"}:
                    run = candidate
                    break

            now = utcnow()
            if run is None:
                run = AgentRun(
                    public_id=generate_public_id("run"),
                    task_id=task.id,
                    engine_type="langgraph",
                    graph_name=task.graph_name or GRAPH_NAME_TEST_PLAN,
                    graph_version=task.graph_version or GRAPH_VERSION_V2,
                    thread_id=task.thread_id or task.public_id,
                    status="running",
                    started_at=now,
                    finished_at=None,
                    model_provider=None,
                    model_name=None,
                    token_usage_json=None,
                    error_json=None,
                    created_at=now,
                    updated_at=now,
                )
                await run_repo.create(run)
            else:
                run.status = "running"
                run.finished_at = None

            task.engine_type = "langgraph"
            task.graph_name = task.graph_name or run.graph_name or GRAPH_NAME_TEST_PLAN
            task.graph_version = task.graph_version or run.graph_version or GRAPH_VERSION_V2
            task.thread_id = task.thread_id or run.thread_id or task.public_id
            task.active_run_id = run.public_id
            task.runtime_status = "running"
            task.status = "running"
            task.started_at = task.started_at or now
            task.completed_at = None
            await session.commit()
            # Phase 1 cache (P0 整改): task status 写穿. 失败仅 log,
            # TTL 180s 内下次 SSE heartbeat miss 后会从 DB 重新加载.
            await _write_through_task_status(
                task.public_id, "running", task.active_run_id, now
            )
            return PreparedGraphRun(graph_run_id=run.public_id)

    async def apply_outcome(
        self,
        task_public_id: str,
        graph_run_id: str,
        outcome: Any,
    ) -> None:
        """Persist the graph outcome without treating a pause as a terminal run."""
        async with self._session_factory() as session:
            task, run = await self._load_task_and_run(session, task_public_id, graph_run_id)
            now = utcnow()
            status = resolve_outcome_task_status(outcome)
            paused = bool(getattr(outcome, "paused", False))
            completed = bool(getattr(outcome, "completed", False))

            if paused:
                task.status = status
                task.runtime_status = "awaiting_resume"
                task.completed_at = None
                run.status = "waiting_user_confirm"
                run.finished_at = None
            elif completed:
                task.status = status
                task.runtime_status = status
                task.completed_at = now
                run.status = status
                run.finished_at = now
            else:
                task.status = status
                task.runtime_status = "running"
                task.completed_at = None
                run.status = "running"
                run.finished_at = None

            task.active_run_id = run.public_id
            await session.commit()
            # Phase 1 cache (P0 整改): task status 写穿. terminal 状态
            # (completed) TTL 自动切到 6h;active (running) 180s.
            await _write_through_task_status(
                task.public_id, status, run.public_id, now
            )

    async def mark_failed(
        self,
        task_public_id: str,
        graph_run_id: str,
        exc: Exception,
    ) -> None:
        """Store an adapter/coordinator exception as a terminal failed run."""
        async with self._session_factory() as session:
            task, run = await self._load_task_and_run(session, task_public_id, graph_run_id)
            now = utcnow()
            message = str(exc) or type(exc).__name__
            error = {
                "code": "langgraph_execution_failed",
                "message": message,
                "exception_type": type(exc).__name__,
            }
            task.status = "failed"
            task.runtime_status = "failed"
            task.error_code = error["code"]
            task.error_message = message
            task.completed_at = now
            task.active_run_id = run.public_id
            run.status = "failed"
            run.finished_at = now
            run.error_json = error
            await session.commit()
            # Phase 1 cache (P0 整改): failed terminal 状态写穿 → TTL 6h.
            await _write_through_task_status(
                task.public_id, "failed", run.public_id, now
            )

    async def _load_task_and_run(self, session: Any, task_public_id: str, graph_run_id: str):
        task = await AgentTaskRepository(session).get_by_public_id(task_public_id)
        if task is None:
            raise ValueError(f"Agent task not found: {task_public_id}")
        run = await AgentRunRepository(session).get_by_public_id(graph_run_id)
        if run is None:
            raise ValueError(f"Agent run not found: {graph_run_id}")
        if run.task_id != task.id:
            raise ValueError(
                f"Agent run {graph_run_id} does not belong to task {task_public_id}"
            )
        return task, run


__all__ = ["LangGraphRunLifecycle", "PreparedGraphRun"]


def resolve_outcome_task_status(outcome: Any) -> str:
    """Use lifecycle-safe defaults when a graph outcome omits ``task_status``."""
    explicit_status = getattr(outcome, "task_status", None)
    if explicit_status:
        return str(explicit_status)
    if bool(getattr(outcome, "paused", False)):
        return "waiting_user_confirm"
    if bool(getattr(outcome, "completed", False)):
        return "completed"
    return "running"


# 模块定位:LangGraph 任务执行的可持久化生命周期(Phase 2.8R-B)
#
# 模块边界:
#   - LangGraphRunCoordinator 拥有图执行 + SSE 推送;
#   - 本模块拥有 AgentRun 数据库身份与终态(开始 / 暂停 / 完成 / 失败 / 取消)。
# 这样划分让 SSE / 任务详情 / 历史复显都引用同一 AgentRun 行。
#
# 关键约束:
#   - 增删 AgentRun 行只在本模块里发生(不要在节点里 INSERT);
#   - 失败时 update terminal_status 后必须 commit,不能 leak 事务;
#   - 启动期 acquire lease / 终态 release lease 不在这(由 Outbox Worker 负责)。
