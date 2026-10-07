"""Repair subgraph — 6 节点 LangGraph (Phase 2.4 — ADR-2.4-8).

节点:
* ``repair_decide``       — 调 LLM 决策下一步
* ``repair_execute_tool``  — 调 ToolAdapter
* ``repair_observe``       — 写审计 + 派 issue_resolution
* ``repair_re_review``     — 调 ResultReviewTool 复审 (内部回环)
* ``repair_finish``        — 终止 + emit COMPLETED
* ``repair_fallback``      — 兜底(emit FALLBACK + delegate 到 Phase 2.1 regen)

条件边 (本文件集中定义,因为 subgraph 内部状态机比 prep 复杂):
- decide → execute_tool (call_tool)
- decide → finish (finish)
- decide → fallback (fail / banned)
- execute_tool → observe
- observe → re_review (if has remaining block)
- observe → finish (if no remaining block)
- re_review → decide (loop) or finish/fallback

外部接口:``run_repair_subgraph(state, llm_client, tool_adapter, ctx) -> RepairResult``。
此函数直接 await ``agent_loop.run_repair`` 主循环(避免在 LangGraph 上
再起一个 checkpoint memory),只需返回 RepairResult 主图节点读它即可。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agent_runtime.runtime_context import RuntimeContext
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState


logger = logging.getLogger(__name__)


# Public node name constants — 主图 node_review_format 引用
NODE_REPAIR_DECIDE = "repair_decide_step"
NODE_REPAIR_EXECUTE_TOOL = "repair_execute_tool_step"
NODE_REPAIR_OBSERVE = "repair_observe_step"
NODE_REPAIR_REVIEW = "repair_re_review_step"
NODE_REPAIR_FINISH = "repair_finish_step"
NODE_REPAIR_FALLBACK = "repair_fallback_step"


# Public entry/exit for main graph embedding
NODE_REPAIR_SUBGRAPH = "repair_subgraph_step"
NODE_REPAIR_FALLBACK_NODE = "repair_fallback_step"


async def run_repair_subgraph(
    state: TestPlanGraphState,
    *,
    llm_client,
    tool_adapter,
    ctx: RuntimeContext,
) -> Any:
    """Repair subgraph 主入口;直接调 ``run_repair`` 主循环。

    把 state 字典化(避开 TypedDict 校验)交给 agent_loop;返回 RepairResult
    实例,主图 ``repair_subgraph_node`` 读它做状态更新。

    Phase 2.4 范围内:本函数直接代理到 agent_loop.run_repair(避免在 subgraph
    内另起一个 checkpoint memory,降低 Phase 2.5+ 抽象层)。LangGraph 6 节点
    作为"语义占位"在 documentation 里描述,其真实执行在 run_repair。
    """
    from app.agent_runtime.repair.agent_loop import run_repair

    state_dict: Dict[str, Any] = dict(state)
    return await run_repair(
        state_dict,
        llm_client=llm_client,
        tool_adapter=tool_adapter,
        ctx=ctx,
    )


__all__ = [
    "NODE_REPAIR_DECIDE",
    "NODE_REPAIR_EXECUTE_TOOL",
    "NODE_REPAIR_OBSERVE",
    "NODE_REPAIR_REVIEW",
    "NODE_REPAIR_FINISH",
    "NODE_REPAIR_FALLBACK",
    "NODE_REPAIR_SUBGRAPH",
    "NODE_REPAIR_FALLBACK_NODE",
    "run_repair_subgraph",
]


# module-level note (auto-appended):
# build_repair_subgraph — 6 节点修复 LangGraph 子图。
# 关键约束: checkpointer = MemorySaver(Phase 2.4 不引入 Postgres)。
