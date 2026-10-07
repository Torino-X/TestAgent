"""v2 终止节点 (cancel / fail)。"""

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
    """统一失败出口:发出 TASK_FAILED 并把当前 last_error 写入 event payload。"""
    completed = mark_completed(state, NODE_FAIL_TASK)
    last_error = state.get("last_error") or {}

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
    """取消出口。Legacy orchestrator 不直接 emit TASK_CANCELLED (由 agent_task_service 写),
    但 LangGraph 节点为了 SSE 流自洽,这里发一次。"""
    completed = mark_completed(state, NODE_CANCEL_TASK)

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


# ════════════════════════════════════════════════════════════════════════════════
# v2_frozen 历史快照(Phase 2.2)。
# 与同目录的 v2/ 字节级镜像;目录独立是为了 Phase 2.2.x 升级不被绑死,
# v2 可演进,v2_frozen 永远冻结(供回滚 / 测试稳定快照用)。
#
# 读代码时:v2_frozen 内容与 v2 几乎一致,优先看 v2 的注释(v2 改动先行)。
# 唯一差异:LangGraph 编译时 graph_name 不同
# (`${GRAPH_NAME_TEST_PLAN}_v2_frozen` vs `${GRAPH_NAME_TEST_PLAN}_v2`),
# 不影响代码组织。
#
# ⚠️ 不要让 v2_frozen 与 v3 直接对照读 ——
#   v2_frozen 用 sentinel pause_marker,v3 走真 LangGraph interrupt,差异大;
#   想知道中断怎么跑,读 v3/nodes_interrupts.py(v3 已加注释)。
# ════════════════════════════════════════════════════════════════════════════════
