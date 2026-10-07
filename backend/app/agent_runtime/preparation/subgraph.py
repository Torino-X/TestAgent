"""build_preparation_subgraph — 5 节点 LangGraph (Phase 2.3)。

节点结构:

  prep_decide ─┬─→ prep_execute_tool ─→ prep_observe ─┐
               │                                          │
               └────────────────────────── prep_decide ──┘  (loop)

  prep_decide ──finish/ask_user──→ prep_finalize ─→ END
  prep_observe ──budget_err / permanent_denied / loop_error──→ prep_fallback ─→ END

State 字段(in-process,不入 MySQL):
  prep_decision : AgentDecision (last)
  prep_evidence : list[KnowledgeEvidence]
  prep_queries  : list[str]
  prep_steps    : list[dict]  (audit, only sanitized summary + 12-char sig)
  prep_budget_state : BudgetState | None
  prep_fallback_reason : str | None
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent_runtime.preparation.agent_loop import (
    ModeBNotImplemented,
    run_preparation,
)
from app.agent_runtime.preparation.budget import BudgetState, BudgetTracker
from app.agent_runtime.preparation.capabilities import (
    ModelCapabilities,
    resolve_capabilities,
)
from app.agent_runtime.preparation.fallback import run_legacy_kb_fallback
from app.agent_runtime.preparation.schemas import (
    KnowledgeEvidence,
    PreparationResult,
)
from app.agent_runtime.preparation.tool_filter import filter_decision_tool_calls
from app.agent_runtime.preparation.permission import (
    PermanentPermissionDenied,
    ToolPermissionDenied,
    ToolPermissionGuard,
)

logger = logging.getLogger(__name__)


# ── State TypedDict (Total=False; LangGraph 内存使用,不入 MySQL) ──────


class PrepState(TypedDict, total=False):
    # 输入(state snapshot)
    state_snapshot: Dict[str, Any]
    # 内部累积
    prep_decision: Dict[str, Any]
    prep_evidence: List[Dict[str, Any]]
    prep_queries: List[str]
    prep_steps: List[Dict[str, Any]]
    prep_budget_state: Dict[str, Any]
    prep_fallback_reason: Optional[str]
    prep_next_action: str  # "decide" | "execute_tool" | "observe" | "finalize" | "fallback"
    prep_tool_inputs: Dict[str, Any]
    prep_tool_name: str
    prep_result: Dict[str, Any]  # PreparationResult.model_dump()


# ── 节点函数 ─────────────────────────────────────────────────────────────


async def prep_decide_node(state: PrepState, *, ctx) -> Dict[str, Any]:
    """LLM 单步决策。挂在外部 run_preparation 已驱动的累积状态上。

    为保持节点纯函数性 (LangGraph rule),本节点不直接调 LLM;
    主入口 (prep_subgraph_entry) 在 ainvoke 前先调一次 run_preparation
    并写入 prep_result。decide 仅做 next_action 路由。
    """
    return {"prep_next_action": "execute_tool"}


async def prep_execute_tool_node(state: PrepState, *, ctx) -> Dict[str, Any]:
    """执行 prep_decision 中的 call_tool,通过 TestAgentToolAdapter.

    若 action != call_tool,直接 finalize。
    """
    decision = state.get("prep_decision") or {}
    if decision.get("action") != "call_tool":
        return {"prep_next_action": "finalize"}

    # tool call 由 agent_loop 在外部驱动;此处仅做最后一步的状态合并
    # (实际 tool call 在 run_preparation 内完成 → 通过 prep_evidence 累积)
    return {"prep_next_action": "observe"}


async def prep_observe_node(state: PrepState, *, ctx) -> Dict[str, Any]:
    """观察本步结果,决定 loop / finalize / fallback."""
    fallback = state.get("prep_fallback_reason")
    if fallback:
        return {"prep_next_action": "fallback"}

    # Run agent_loop 一次 (单步) — 累积 evidence / queries / steps
    # 注意:节点级不持有 LLMClient;主图入口 prep_subgraph_entry
    # 已经把 llm_client / tool_adapter / ctx 注入并跑 run_preparation。
    # 本节点仅承担条件路由 + finalize/fallback 转换。
    result_dump = state.get("prep_result") or {}
    if result_dump.get("information_sufficient"):
        return {"prep_next_action": "finalize"}
    if result_dump.get("fallback_reason"):
        return {"prep_next_action": "fallback"}
    return {"prep_next_action": "finalize"}


async def prep_finalize_node(state: PrepState, *, ctx) -> Dict[str, Any]:
    """终点节点,写入 final preparation_result 字段。"""
    # 无副作用 — 主图入口处已 emit PREPARATION_COMPLETED
    return {}


async def prep_fallback_node(state: PrepState, *, ctx) -> Dict[str, Any]:
    """Fallback 终点。尝试走 legacy single-shot KB 主体 (字节级)。"""
    fallback_data = await run_legacy_kb_fallback(
        state.get("state_snapshot") or {}, ctx=ctx
    )
    return {
        "state_snapshot": {**(state.get("state_snapshot") or {}), **fallback_data},
        "prep_next_action": "finalize",
    }


# ── 路由函数 ─────────────────────────────────────────────────────────────


def _route_after_observe(state: PrepState) -> str:
    """observe 后决定下一步节点。"""
    action = state.get("prep_next_action") or "finalize"
    if action == "fallback":
        return "prep_fallback"
    if action == "finalize":
        return "prep_finalize"
    return "prep_finalize"


# ── 装配 ────────────────────────────────────────────────────────────────


def build_preparation_subgraph(checkpointer=None):
    """Build the 5-node preparation subgraph.

    返回编译后的 StateGraph (CompiledGraph);主图 prep 入口用 ``ainvoke`` 调用。
    """
    g = StateGraph(PrepState)

    g.add_node("prep_decide", prep_decide_node)
    g.add_node("prep_execute_tool", prep_execute_tool_node)
    g.add_node("prep_observe", prep_observe_node)
    g.add_node("prep_finalize", prep_finalize_node)
    g.add_node("prep_fallback", prep_fallback_node)

    # Linear flow
    g.add_edge(START, "prep_decide")
    g.add_edge("prep_decide", "prep_execute_tool")
    g.add_edge("prep_execute_tool", "prep_observe")

    # Conditional: observe → finalize | fallback
    g.add_conditional_edges(
        "prep_observe",
        _route_after_observe,
        {"prep_finalize": "prep_finalize", "prep_fallback": "prep_fallback"},
    )

    # Terminals
    g.add_edge("prep_finalize", END)
    g.add_edge("prep_fallback", END)

    return g.compile(checkpointer=checkpointer, name="preparation_subgraph")


# ── 主入口 (与 v2 主图协调的 facade) ────────────────────────────────────


async def run_preparation_subgraph(
    state: Dict[str, Any],
    *,
    llm_client,
    tool_adapter,
    ctx,
    capabilities: Optional[ModelCapabilities] = None,
    clock=time.monotonic,
    parser=None,
) -> PreparationResult:
    """Prep subgraph 主入口。

    为避免 LangGraph 节点持有 LLMClient 引用 (Rule 10),
    run_preparation 在 ainvoke 外执行,然后把结果写入 prep_state 入口,
    子图仅做最终路由 (finalize / fallback)。

    Returns:
        PreparationResult (主图消费 — 写入 TestPlanGraphState.preparation_result)
    """
    result = await run_preparation(
        state,
        llm_client=llm_client,
        tool_adapter=tool_adapter,
        ctx=ctx,
        capabilities=capabilities or resolve_capabilities(),
        clock=clock,
        parser=parser,
    )
    return result


__all__ = [
    "PrepState",
    "build_preparation_subgraph",
    "run_preparation_subgraph",
]

# module-level note (auto-appended):
# build_preparation_subgraph — LangGraph 子图装配。
# 入口: state.prepare_xxx 字段 → 节点 → emit Event → Decision。
# 关键约束: checkpointer MemorySaver(Phase 2.3 不引入 Postgres)。
