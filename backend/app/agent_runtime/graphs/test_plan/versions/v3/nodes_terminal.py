"""v2 终止节点 (cancel / fail)。

两个节点都是 LangGraph 的终态 (END) ——
不再进入后续节点,由 Worker 等待结束。
所有 LangGraph 路径上出现的失败都会被 fail_task_node 兜底,
而 cancel_task_node 仅在用户主动触发任务取消时进入。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.agent.enums import AgentEventType, TaskStatus

from app.agent_runtime.runtime_context import RuntimeContext

from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from .nodes_util import mark_completed

logger = logging.getLogger(__name__)


NODE_FAIL_TASK = "fail_task"
NODE_CANCEL_TASK = "cancel_task"


# ── fail_task ────────────────────────────────────────────────────────────────


async def fail_task_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """统一失败出口:发出 TASK_FAILED 并把当前 last_error 写入 event payload。

    任何业务节点把 state.last_error 写成一个 dict(error_code / message / ...)
    后,LangGraph router 就会路到 fail_task_node,这里负责把这条因果
    通过 event_sink 推给前端,然后写终态字段让 Worker 关闭任务。
    """
    completed = mark_completed(state, NODE_FAIL_TASK)
    # state.last_error 形如 {"error_code": "EXPORT_ARTIFACT_MISSING", "message": "..."}
    # 这里原样透传给事件 payload,前端能直接展示给用户。
    last_error = state.get("last_error") or {}

    # 通过 event_sink 发 SSE TASK_FAILED 事件,前端 reducer 收到后会
    # 切到 error 状态 + 显示错误卡片。
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_FAIL_TASK,
        event_type=AgentEventType.TASK_FAILED.value,
        title="任务失败",
        content="",
        payload={"error": last_error},
    )

    return {
        "task_status": TaskStatus.FAILED.value,
        "current_phase": "failed",
        "current_node": NODE_FAIL_TASK,
        "completed_nodes": completed,
    }


# ── cancel_task ──────────────────────────────────────────────────────────────


async def cancel_task_node(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict:
    """取消出口。

    Legacy orchestrator 不直接 emit TASK_CANCELLED ——
    取消链路上的状态写库由 agent_task_service.cancel_task 负责。
    但 LangGraph 节点为了 SSE 流自洽(让前端 reducer 能切到 cancelled
    状态),需要在这里发一次 TASK_CANCELLED。
    """
    completed = mark_completed(state, NODE_CANCEL_TASK)

    # 取消任务没有 last_error,只发空白 payload 让前端 reducer 识别事件类型即可。
    await ctx.event_sink.emit(
        task_id=str(ctx.task_internal_id),
        graph_run_id=f"run-{ctx.task_internal_id}",
        node_name=NODE_CANCEL_TASK,
        event_type=AgentEventType.TASK_CANCELLED.value,
        title="任务已取消",
        content="",
        payload={},
    )

    return {
        "task_status": TaskStatus.CANCELLED.value,
        "current_phase": "cancelled",
        "current_node": NODE_CANCEL_TASK,
        "completed_nodes": completed,
    }


__all__ = [
    "NODE_FAIL_TASK",
    "NODE_CANCEL_TASK",
    "fail_task_node",
    "cancel_task_node",
]
