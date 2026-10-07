"""v2 interrupt 节点(Phase 2.2)。

ADR-010 §12 把章节确认与格式损失确认从 sentinel 字段
(``pause_marker`` + return-to-END) 升级为 LangGraph ``interrupt()`` +
``Command(resume=...)``。

**关键约束**:LangGraph 强制要求调用 ``interrupt()`` 的节点必须是 **同步**
(非 async)函数 —— async 节点内部调 interrupt 会抛 ``TypeError``。
本文件中两个节点函数都是 ``def``,不带 ``async``。

行为约束(规范 §12.4 + §17.3):
* ``interrupt()`` payload 与 Legacy ``need_user_confirm`` /
  ``format_loss_confirm_requested`` 业务数据保持一致,旧 SSE 消费者无感。
* ``resume`` 接受 dict,允许 ``source="user"`` 或 ``source="timeout"``;
  coordinator 强制校验。
* 超时由 ``ConfirmationTimeoutService`` 驱动(管理命令),不依赖
  Worker 内存中的 asyncio.sleep;恢复时通过持久化 ``agent_tasks`` /
  checkpointer 的 thread_id 即可拿到上次中断上下文。
* 旧任务继续走 Legacy;LangGraph v2 interrupt 路径由
  ``AgentRuntimeFeatureFlags.interrupt_v2_enabled`` 控制,默认关闭。

事件发送策略(Phase 2.2):
* interrupt 节点本身不能 await,因此 ``event_sink.emit`` 通过
  ``asyncio.get_running_loop().create_task(...)`` 投递到主循环。
* 同步路径(测试 stub / 无 loop)→ 仅记 logger,不阻塞 interrupt。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from langgraph.types import interrupt

from app.agent.enums import AgentEventType, TaskStatus

from app.agent_runtime.runtime_context import RuntimeContext

from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from .nodes_util import get_ctx

logger = logging.getLogger(__name__)


NODE_SECTION_CONFIRM_INTERRUPT = "section_confirmation_interrupt"
NODE_FORMAT_LOSS_INTERRUPT = "format_loss_interrupt"


# ── 通用 helper ───────────────────────────────────────────────────────────


def _schedule_emit(ctx: RuntimeContext, event_type: str, title: str, payload: Dict[str, Any]) -> None:
    """``event_sink.emit`` 在无 await 上下文里通过 ``loop.create_task`` 投递。

    interrupt 节点不能 await,但仍要让用户在前端看到 ``TASK_RESUMED``。
    失败仅 log,不阻塞 interrupt 返回。
    """
    task_id = str(ctx.task_internal_id)
    graph_run_id = f"run-{ctx.task_internal_id}"

    async def _do() -> None:
        try:
            await ctx.event_sink.emit(
                task_id=task_id,
                graph_run_id=graph_run_id,
                node_name="interrupt",
                event_type=event_type,
                title=title,
                content="",
                payload=payload,
            )
        except Exception:
            logger.warning(
                "interrupt: event_sink.emit failed (swallowed) event=%s",
                event_type,
                exc_info=True,
            )

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_do())
    except RuntimeError:
        logger.debug("interrupt: no running loop, skip emit %s", event_type)


def _mark_completed(state: TestPlanGraphState, node: str) -> list:
    """append 节点名到 completed_nodes(同步版本,不调 mark_completed helper)。"""
    completed = list(state.get("completed_nodes") or [])
    if node not in completed:
        completed.append(node)
    return completed


# ── 章节确认 interrupt ─────────────────────────────────────────────────────


def section_confirmation_interrupt_node(
    state: TestPlanGraphState, config: Any
) -> dict:
    """章节策略确认 interrupt(同步节点)。

    ``interrupt()`` payload::

        {
          "kind": "section_confirmation",
          "task_id": <str>,
          "sections": [...],   # 镜像 state.section_suggestions
          "timeout_at": <iso8601 str> | None,
        }

    Resume payload(由 LangGraphRunCoordinator.resume_section_confirmation 校验)::

        {
          "kind": "section_confirmation",
          "sections": [{...}, ...],
          "source": "user" | "timeout",
        }
    """
    ctx: RuntimeContext = get_ctx(config)
    completed = _mark_completed(state, NODE_SECTION_CONFIRM_INTERRUPT)

    sections = state.get("section_suggestions") or {}
    payload = {
        "kind": "section_confirmation",
        "task_id": str(ctx.task_internal_id),
        "sections": sections.get("sections") if isinstance(sections, dict) else sections,
        "timeout_at": None,
    }

    decision = interrupt(payload)

    if not isinstance(decision, dict):
        raise ValueError(
            f"section_confirmation resume payload must be dict, got {type(decision).__name__}"
        )
    if decision.get("kind") != "section_confirmation":
        raise ValueError(
            f"section_confirmation resume kind mismatch: {decision.get('kind')!r}"
        )

    new_section_confirm = {
        "sections": decision.get("sections") or [],
        "source": decision.get("source") or "user",
    }

    _schedule_emit(
        ctx,
        AgentEventType.TASK_RESUMED.value,
        "任务已恢复",
        {"source": new_section_confirm["source"]},
    )

    return {
        "section_confirm_config": new_section_confirm,
        "pause_marker": None,
        "current_phase": "post_confirm",
        "task_status": TaskStatus.GENERATING.value,
        "current_node": NODE_SECTION_CONFIRM_INTERRUPT,
        "completed_nodes": completed,
    }


# ── 格式损失 interrupt ────────────────────────────────────────────────────


def format_loss_interrupt_node(
    state: TestPlanGraphState, config: Any
) -> dict:
    """格式损失确认 interrupt(同步节点)。

    ``interrupt()`` payload::

        {
          "kind": "format_loss",
          "task_id": <str>,
          "loss_count": int,
          "loss_details_for_user": [...],
          "losses": [...],
          "choices": ["accept", "retry", "reject"],
          "timeout_seconds": 300,
        }

    Resume payload::

        {
          "kind": "format_loss",
          "decision": "accept" | "retry" | "reject",
          "source": "user" | "timeout",
        }
    """
    ctx: RuntimeContext = get_ctx(config)
    completed = _mark_completed(state, NODE_FORMAT_LOSS_INTERRUPT)

    losses = state.get("pending_format_losses") or []
    timeout_seconds = int(state.get("format_loss_timeout_seconds") or 300)

    payload = {
        "kind": "format_loss",
        "task_id": str(ctx.task_internal_id),
        "loss_count": len(losses) if isinstance(losses, list) else 0,
        "loss_details_for_user": losses,
        "losses": losses,
        "choices": ["accept", "retry", "reject"],
        "timeout_seconds": timeout_seconds,
    }

    decision = interrupt(payload)

    if not isinstance(decision, dict):
        raise ValueError(
            f"format_loss resume payload must be dict, got {type(decision).__name__}"
        )
    if decision.get("kind") != "format_loss":
        raise ValueError(
            f"format_loss resume kind mismatch: {decision.get('kind')!r}"
        )
    chosen = decision.get("decision")
    if chosen not in ("accept", "retry", "reject"):
        raise ValueError(f"format_loss decision must be accept/retry/reject, got {chosen!r}")

    confirmation = {
        "decision": chosen,
        "source": decision.get("source") or "user",
        "losses": losses,
    }

    _schedule_emit(
        ctx,
        AgentEventType.FORMAT_LOSS_DECISION_RECORDED.value,
        f"用户决策:{chosen}",
        {"decision": chosen, "source": confirmation["source"]},
    )

    return {
        "format_loss_confirmation": confirmation,
        "pause_marker": None,
        "current_phase": "post_confirm" if chosen == "retry" else "completed",
        "task_status": (
            TaskStatus.EXPORTING.value if chosen == "retry" else TaskStatus.COMPLETED.value
        ),
        "current_node": NODE_FORMAT_LOSS_INTERRUPT,
        "completed_nodes": completed,
    }


__all__ = [
    "NODE_SECTION_CONFIRM_INTERRUPT",
    "NODE_FORMAT_LOSS_INTERRUPT",
    "section_confirmation_interrupt_node",
    "format_loss_interrupt_node",
]
