"""v2 真实业务图装配 — Phase 2.1 + Phase 2.2 interrupt 双路径。

LangGraph 0.2.x 节点签名::

    def node(state: TestPlanGraphState) -> dict | Command
    # 或
    async def node(state: TestPlanGraphState, config: RunnableConfig) -> dict

我们走第二条路,通过 ``get_ctx(config)`` 从 ``configurable.runtime_context``
抽出 ``RuntimeContext``。LangGraphRunCoordinator 调 ``ainvoke`` 前注入。

Phase 2.2 增量:
* 新增 ``section_confirmation_interrupt`` / ``format_loss_interrupt`` 节点,
  使用 LangGraph ``interrupt()`` + ``Command(resume=...)``。
* 旧 ``pause_for_legacy_confirm`` / ``pause_for_legacy_format_decision``
  (sentinel return-to-END) 保留,作为默认路径。
* ``interrupt_enabled=True`` 时,边切换为指向 interrupt 节点;
  ``False`` 时仍走 sentinel(等价 Phase 2.1 行为,行为等价测试基线)。
"""

from __future__ import annotations

from typing import Any, Optional

from langgraph.graph import END, START, StateGraph

from app.agent_runtime.adapters.test_agent_tool_adapter import DEFAULT_TOOL_WHITELIST
from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V2,
)
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from .entry_routing import (
    NODE_FAIL_TASK,
    NODE_INITIALIZE_TASK,
    NODE_RECORD_LOSS_DECISION as ENTRY_NODE_RLD,
    NODE_RESUME_TASK,
    entry_router,
)
from .nodes_interrupts import (
    NODE_FORMAT_LOSS_INTERRUPT,
    NODE_SECTION_CONFIRM_INTERRUPT,
)
from .nodes_post_confirm import (
    NODE_EXPORT_WORD,
    NODE_FINALIZE_TASK,
    NODE_GENERATE_COMPLETION_SUMMARY,
    NODE_GENERATE_TEST_PLAN,
    NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION,
    NODE_PREPARE_EXPORT,
    NODE_RECORD_LOSS_DECISION,
    export_word_node,
    finalize_task_node,
    generate_completion_summary_node,
    generate_test_plan_node,
    pause_for_legacy_format_decision_node,
    prepare_export_node,
    record_loss_decision_node,
    resume_task_node,
)
from .nodes_pre_confirm import (
    NODE_PARSE_REQUIREMENT,
    NODE_PARSE_TEMPLATE,
    NODE_PAUSE_FOR_LEGACY_CONFIRM,
    NODE_PREP_LEGACY_FALLBACK,
    NODE_PREP_SUBGRAPH,
    NODE_SEARCH_KNOWLEDGE,
    NODE_SUGGEST_SECTIONS,
    NODE_VALIDATE_INPUTS,
    initialize_task_node,
    parse_requirement_node,
    parse_template_node,
    pause_for_legacy_confirm_node,
    prep_legacy_fallback_node,
    prep_subgraph_node,
    search_knowledge_node,
    suggest_sections_node,
    validate_inputs_node,
)
from .nodes_review_format import (
    NODE_CHECK_FORMAT_STEP,
    NODE_REGENERATE_SECTIONS_STEP,
    NODE_REPAIR_SUBGRAPH,
    NODE_REPAIR_FALLBACK,
    NODE_REVIEW_STEP,
    check_docx_format_node,
    regenerate_sections_node,
    repair_subgraph_node,
    repair_fallback_node,
    review_result_node,
)
from .nodes_terminal import NODE_CANCEL_TASK, cancel_task_node, fail_task_node
from .nodes_util import get_ctx
from .routing import (
    NODE_FAIL_TASK as ROUTE_NODE_FAIL,
    NODE_FINALIZE_TASK as ROUTE_NODE_FIN,
    NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION as ROUTE_NODE_PAUSE_FMT,
    NODE_PREPARE_EXPORT as ROUTE_NODE_PREP,
    NODE_PREP_LEGACY_FALLBACK as ROUTE_NODE_PREP_FALLBACK,
    NODE_PREP_SUBGRAPH as ROUTE_NODE_PREP_SUB,
    NODE_REGENERATE_SECTIONS_STEP as ROUTE_NODE_REG,
    NODE_REPAIR_SUBGRAPH as ROUTE_NODE_REPAIR,
    NODE_SEARCH_KNOWLEDGE as ROUTE_NODE_KB,
    route_after_format_check,
    route_after_parse_template,
    route_after_review,
    route_after_validate,
)


# ── 节点包装 ───────────────────────────────────────────────────────────────


def _bind_async(node_fn):
    """把 ``async def f(state, *, ctx)`` 包成 LangGraph 节点 ``async def f(state, config)``。"""

    async def _wrapped(state: TestPlanGraphState, config: Any) -> dict:
        ctx = get_ctx(config)
        return await node_fn(state, ctx=ctx)

    _wrapped.__name__ = node_fn.__name__
    return _wrapped


def _bind_sync(node_fn):
    """把 ``def f(state, config) -> dict`` 包成 LangGraph 节点(原样转发)。

    interrupt 节点必须是 sync,所以它们不通过 _bind_async;RuntimeContext 仍
    从 ``config.configurable.runtime_context`` 取。
    """

    def _wrapped(state: TestPlanGraphState, config: Any) -> dict:
        return node_fn(state, config)

    _wrapped.__name__ = node_fn.__name__
    return _wrapped


# ── 名称常量 ───────────────────────────────────────────────────────────────


NODE_INIT = NODE_INITIALIZE_TASK
NODE_VALID = NODE_VALIDATE_INPUTS
NODE_PARSE_REQ = NODE_PARSE_REQUIREMENT
NODE_PARSE_TPL = NODE_PARSE_TEMPLATE
NODE_KB = NODE_SEARCH_KNOWLEDGE
NODE_SEC = NODE_SUGGEST_SECTIONS
NODE_PREP_SUB = NODE_PREP_SUBGRAPH
NODE_PREP_FALLBACK = NODE_PREP_LEGACY_FALLBACK
NODE_PAUSE_CONF = NODE_PAUSE_FOR_LEGACY_CONFIRM
NODE_SECTION_INTERRUPT = NODE_SECTION_CONFIRM_INTERRUPT
NODE_RESUME = NODE_RESUME_TASK
NODE_GEN = NODE_GENERATE_TEST_PLAN
NODE_REVIEW = NODE_REVIEW_STEP
NODE_REG = NODE_REGENERATE_SECTIONS_STEP
NODE_REPAIR = NODE_REPAIR_SUBGRAPH
NODE_REPAIR_FB = NODE_REPAIR_FALLBACK
NODE_PREP = NODE_PREPARE_EXPORT
NODE_WORD = NODE_EXPORT_WORD
NODE_FMT = NODE_CHECK_FORMAT_STEP
NODE_PAUSE_FMT_DEC = NODE_PAUSE_FOR_LEGACY_FORMAT_DECISION
NODE_FORMAT_INTERRUPT = NODE_FORMAT_LOSS_INTERRUPT
NODE_RLD = NODE_RECORD_LOSS_DECISION
# Phase 2.8D:插入 Summary 节点在 finalize_task 前
NODE_SUMMARY = NODE_GENERATE_COMPLETION_SUMMARY
NODE_FIN = NODE_FINALIZE_TASK
NODE_FAIL = NODE_FAIL_TASK
NODE_CANCEL = NODE_CANCEL_TASK


def build_compiled_v2_graph(
    checkpointer: Optional[Any] = None,
    *,
    interrupt_enabled: bool = False,
):
    """组装 v2 业务图并编译。

    :param checkpointer: ``MemorySaver`` / ``AsyncSqliteSaver`` / ``AsyncPostgresSaver``。
                        Phase 2.2 范围仅 ``MemorySaver`` 实测;SQLite/Postgres
                        见完成报告「生产 Checkpointer 部署说明」。
    :param interrupt_enabled: ``True`` 时把 sentinel ``pause_for_legacy_*`` 节点
                             替换为 ``interrupt()`` 节点(Phase 2.2 默认 ``False``)。
    """
    g = StateGraph(TestPlanGraphState)

    # ── 节点 ───────────────────────────────────────────────────────────
    g.add_node(NODE_INIT, _bind_async(initialize_task_node))
    g.add_node(NODE_VALID, _bind_async(validate_inputs_node))
    g.add_node(NODE_PARSE_REQ, _bind_async(parse_requirement_node))
    g.add_node(NODE_PARSE_TPL, _bind_async(parse_template_node))
    g.add_node(NODE_KB, _bind_async(search_knowledge_node))
    # Phase 2.3: prep subgraph (preparation_agent_enabled=True 时被选中)
    g.add_node(NODE_PREP_SUB, _bind_async(prep_subgraph_node))
    g.add_node(NODE_PREP_FALLBACK, _bind_async(prep_legacy_fallback_node))
    g.add_node(NODE_SEC, _bind_async(suggest_sections_node))

    # 章节确认:interrupt 节点(sync)或 sentinel(原 pause_for_legacy_confirm,async)
    if interrupt_enabled:
        from .nodes_interrupts import section_confirmation_interrupt_node
        g.add_node(NODE_SECTION_INTERRUPT, _bind_sync(section_confirmation_interrupt_node))
    g.add_node(NODE_PAUSE_CONF, _bind_async(pause_for_legacy_confirm_node))

    g.add_node(NODE_RESUME, _bind_async(resume_task_node))
    g.add_node(NODE_GEN, _bind_async(generate_test_plan_node))
    g.add_node(NODE_REVIEW, _bind_async(review_result_node))
    g.add_node(NODE_REG, _bind_async(regenerate_sections_node))
    # Phase 2.4: Repair Agent dynamic subgraph
    g.add_node(NODE_REPAIR, _bind_async(repair_subgraph_node))
    g.add_node(NODE_REPAIR_FB, _bind_async(repair_fallback_node))
    g.add_node(NODE_PREP, _bind_async(prepare_export_node))
    g.add_node(NODE_WORD, _bind_async(export_word_node))
    g.add_node(NODE_FMT, _bind_async(check_docx_format_node))

    # 格式损失:interrupt 或 sentinel
    if interrupt_enabled:
        from .nodes_interrupts import format_loss_interrupt_node
        g.add_node(NODE_FORMAT_INTERRUPT, _bind_sync(format_loss_interrupt_node))
    g.add_node(NODE_PAUSE_FMT_DEC, _bind_async(pause_for_legacy_format_decision_node))

    g.add_node(NODE_RLD, _bind_async(record_loss_decision_node))
    # Phase 2.8D:Summary 节点在 finalize_task 前(复用 Legacy 逻辑)
    g.add_node(NODE_SUMMARY, _bind_async(generate_completion_summary_node))
    g.add_node(NODE_FIN, _bind_async(finalize_task_node))

    g.add_node(NODE_FAIL, _bind_async(fail_task_node))
    g.add_node(NODE_CANCEL, _bind_async(cancel_task_node))

    # ── 入口条件路由(START → 4 种入口之一) ──────────────────────────────
    g.add_conditional_edges(
        START,
        entry_router,
        path_map={
            NODE_INIT: NODE_INIT,
            NODE_RESUME: NODE_RESUME,
            NODE_RLD: NODE_RLD,
            NODE_FAIL: NODE_FAIL,
        },
    )

    # ── Pre-confirm 线性 ───────────────────────────────────────────────
    g.add_edge(NODE_INIT, NODE_VALID)
    g.add_conditional_edges(
        NODE_VALID,
        route_after_validate,
        path_map={
            NODE_PARSE_REQ: NODE_PARSE_REQ,
            NODE_FAIL: NODE_FAIL,
        },
    )
    g.add_edge(NODE_PARSE_REQ, NODE_PARSE_TPL)
    # Phase 2.3: parse_template → prep_subgraph (如果 flag 开) 否则 search_knowledge
    g.add_conditional_edges(
        NODE_PARSE_TPL,
        route_after_parse_template,
        path_map={
            ROUTE_NODE_PREP_SUB: NODE_PREP_SUB,
            ROUTE_NODE_KB: NODE_KB,
        },
    )
    g.add_edge(NODE_PREP_SUB, NODE_PREP_FALLBACK)  # 防御:prep 异常可走 fallback
    g.add_edge(NODE_PREP_FALLBACK, NODE_SEC)
    g.add_edge(NODE_KB, NODE_SEC)

    # 章节确认 → 路径分流
    if interrupt_enabled:
        # interrupt 节点 resume 后直接跳 resume_task(无需 sentinel END)
        g.add_edge(NODE_SEC, NODE_SECTION_INTERRUPT)
        g.add_edge(NODE_SECTION_INTERRUPT, NODE_RESUME)
    else:
        g.add_edge(NODE_SEC, NODE_PAUSE_CONF)
        g.add_edge(NODE_PAUSE_CONF, END)

    # ── Post-confirm ───────────────────────────────────────────────────
    g.add_edge(NODE_RESUME, NODE_GEN)
    g.add_edge(NODE_GEN, NODE_REVIEW)
    # Phase 2.4: route_after_review 加入 NODE_REPAIR 路径
    g.add_conditional_edges(
        NODE_REVIEW,
        route_after_review,
        path_map={
            NODE_REG: NODE_REG,
            NODE_REPAIR: NODE_REPAIR,
            NODE_PREP: NODE_PREP,
        },
    )
    g.add_edge(NODE_REG, NODE_REVIEW)  # regenerate → re-review
    # Phase 2.4: repair 完成后走到 prepare_export(主流程不变)
    g.add_edge(NODE_REPAIR, NODE_PREP)
    g.add_edge(NODE_REPAIR_FB, NODE_PREP)  # 兜底导出
    g.add_edge(NODE_PREP, NODE_WORD)
    g.add_edge(NODE_WORD, NODE_FMT)

    # ── Format-check 后路径分流 ──────────────────────────────────────────
    if interrupt_enabled:
        # interrupt 路径下,format_check 节点仍然写 format_check_result;
        # 但 loss_detected 直接走 interrupt 节点(替换 sentinel)。
        from .routing_after_interrupt import route_after_format_check_for_interrupt
        g.add_conditional_edges(
            NODE_FMT,
            route_after_format_check_for_interrupt,
            path_map={
                # Phase 2.8D:format_check 通过也要先经 Summary 再 finalize
                NODE_SUMMARY: NODE_SUMMARY,
                NODE_PREP: NODE_PREP,
                NODE_FORMAT_INTERRUPT: NODE_FORMAT_INTERRUPT,
            },
        )
        from .routing_after_interrupt import route_after_format_interrupt
        g.add_conditional_edges(
            NODE_FORMAT_INTERRUPT,
            route_after_format_interrupt,
            path_map={
                # Phase 2.8D:即便 interrupt 路径,finalize 也要先经 Summary
                NODE_SUMMARY: NODE_SUMMARY,
                NODE_PREP: NODE_PREP,
                NODE_FAIL: NODE_FAIL,
            },
        )
    else:
        g.add_conditional_edges(
            NODE_FMT,
            route_after_format_check,
            path_map={
                NODE_FIN: NODE_FIN,
                NODE_PREP: NODE_PREP,
                NODE_PAUSE_FMT_DEC: NODE_PAUSE_FMT_DEC,
            },
        )
        g.add_edge(NODE_PAUSE_FMT_DEC, END)
        # Phase 2.8D:record_loss_decision → Summary → finalize_task
        g.add_edge(NODE_RLD, NODE_SUMMARY)
        g.add_edge(NODE_SUMMARY, NODE_FIN)

    g.add_edge(NODE_FIN, END)
    g.add_edge(NODE_FAIL, END)
    g.add_edge(NODE_CANCEL, END)

    # Phase 2.8D ADR-3:LangGraph recursion_limit 兜底 防 RetryPolicy while 错改时无限递归。
    # LangGraph 0.3.x compile() 不支持 recursion_limit kwarg,改在调用方 ainvoke 时通过
    # config={"recursion_limit": 10} 传入。
    # 不在 graph 静态签名上 rewrite,改为通过 LangGraphRunCoordinator 注入(见 Step 9)。
    return g.compile(
        checkpointer=checkpointer,
        name=f"{GRAPH_NAME_TEST_PLAN}_{GRAPH_VERSION_V2}",
    )


__all__ = [
    "build_compiled_v2_graph",
    "build_test_plan_v2_graph",
    "NODE_SECTION_INTERRUPT",
    "NODE_FORMAT_INTERRUPT",
    "DEFAULT_TOOL_WHITELIST",
    "GraphRuntimeNotBoundError",
]


def build_test_plan_v2_graph(
    checkpointer: Optional[Any] = None,
    *,
    interrupt_enabled: bool = False,
):
    """Phase 2.1 兼容入口 — 默认 ``interrupt_enabled=False``(sentinel 路径)。

    新代码请显式用 ``build_compiled_v2_graph(..., interrupt_enabled=...)``。
    """
    return build_compiled_v2_graph(
        checkpointer=checkpointer,
        interrupt_enabled=interrupt_enabled,
    )


class GraphRuntimeNotBoundError(RuntimeError):
    """节点被调用时 RuntimeContext 未注入到 config.configurable.runtime_context。"""
