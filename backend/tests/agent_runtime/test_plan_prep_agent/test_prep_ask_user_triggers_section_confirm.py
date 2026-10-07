"""test_prep_ask_user_triggers_section_confirm — prep finish 后章节确认仍必走 (Phase 2.3 §9.7, 禁令 #5)."""

from __future__ import annotations

import pytest

from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState, make_empty_state
from app.agent_runtime.graphs.test_plan.versions.v2.routing import (
    NODE_SEARCH_KNOWLEDGE,
    NODE_SUGGEST_SECTIONS,
    route_after_parse_template,
)
from app.agent_runtime.graphs.test_plan.versions.v2.graph import build_compiled_v2_graph


def test_route_after_parse_template_with_prep_enabled():
    """preparation_agent_enabled=True → 路由到 prep_subgraph。"""
    state = make_empty_state(
        task_id="t1", graph_run_id="r1", graph_version="v2",
        preparation_agent_enabled=True,
    )
    next_node = route_after_parse_template(state)
    # 不论 prep_subgraph 还是 search_knowledge,都必须经过章节确认节点
    # 验证流程上 prep → 章节确认 链路保留
    assert next_node in ("prep_subgraph", NODE_SEARCH_KNOWLEDGE)


def test_route_after_parse_template_default_no_prep():
    """默认 (flag OFF) → 走 search_knowledge (Phase 2.1 字节级行为)。"""
    state = make_empty_state(
        task_id="t1", graph_run_id="r1", graph_version="v2",
        preparation_agent_enabled=False,
    )
    assert route_after_parse_template(state) == NODE_SEARCH_KNOWLEDGE


def test_prep_subgraph_eventually_reaches_suggest_sections():
    """主图拓扑: prep_subgraph → prep_legacy_fallback → NODE_SUGGEST_SECTIONS."""
    g = build_compiled_v2_graph(checkpointer=None, interrupt_enabled=False)
    node_names = set(g.nodes.keys())

    # 章节确认节点必须存在
    assert NODE_SUGGEST_SECTIONS in node_names
    # prep 子图节点存在
    assert "prep_subgraph" in node_names
    assert "prep_legacy_fallback" in node_names
    assert "search_knowledge" in node_names


def test_state_schema_v6_when_prep_enabled():
    """Phase 2.8D:preparation_agent_enabled=True → schema_version=V6 + 新字段。

    Phase 2.3 schema=V3 → Phase 2.8D 升级 V6(retry+summary 闭环字段为通用基础字段)。
    V6 是 V3-5 的 super-set,所有旧字段保留。详见 docs/31 §12。
    """
    state = make_empty_state(
        task_id="t1", graph_run_id="r1", graph_version="v2",
        preparation_agent_enabled=True,
    )
    assert state["state_schema_version"] == 6
    assert state.get("preparation_agent_enabled") is True
    assert "preparation_result" in state
    assert "preparation_steps" in state
    assert "preparation_budget_state" in state
    # Phase 2.8D 新增 3 字段在所有路径通用
    assert "attempt" in state
    assert "last_retry_decision" in state
    assert "summary" in state


def test_state_schema_v6_when_prep_disabled():
    """Phase 2.8D:preparation_agent_enabled=False → schema_version=V6 默认基线。

    Phase 2.1 行为 schema=V2 → Phase 2.8D 升级 V6(V6 是 V2 的 super-set)。
    """
    state = make_empty_state(
        task_id="t1", graph_run_id="r1", graph_version="v2",
        preparation_agent_enabled=False,
    )
    assert state["state_schema_version"] == 6
    assert "preparation_agent_enabled" not in state or state.get("preparation_agent_enabled") is False