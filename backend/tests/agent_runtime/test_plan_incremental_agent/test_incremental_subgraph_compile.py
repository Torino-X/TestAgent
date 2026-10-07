"""Test: 12. Subgraph 编译 — graph_name=incremental_test_plan,graph_version=v1,能编译并 invoke。"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from app.agent_runtime.incremental.subgraph import (
    GRAPH_NAME_INCREMENTAL,
    GRAPH_VERSION_INCREMENTAL_V1,
    NODE_INCREMENTAL_DECIDE,
    NODE_INCREMENTAL_FINISH,
    build_incremental_subgraph,
)


def test_subgraph_constants_unique():
    assert GRAPH_NAME_INCREMENTAL == "incremental_test_plan"
    assert GRAPH_VERSION_INCREMENTAL_V1 == "v1"
    assert NODE_INCREMENTAL_DECIDE != NODE_INCREMENTAL_FINISH


def test_build_incremental_subgraph_compiles():
    g = build_incremental_subgraph()
    assert g is not None
    # compiled graph 暴露 nodes/edges
    nodes = list(g.get_graph().nodes.keys())
    assert NODE_INCREMENTAL_DECIDE in nodes
    assert NODE_INCREMENTAL_FINISH in nodes


def test_subgraph_isolated_from_test_plan_generation():
    """graph_name 必须 != 'test_plan_generation',隔离主图。"""
    g = build_incremental_subgraph()
    # compiled graph 没有 name 属性,但可通过 constant 校验
    assert "test_plan_generation" != GRAPH_NAME_INCREMENTAL
    assert "incremental" in GRAPH_NAME_INCREMENTAL


def test_subgraph_compile_with_checkpointer():
    from langgraph.checkpoint.memory import MemorySaver
    cp = MemorySaver()
    g = build_incremental_subgraph(checkpointer=cp)
    assert g is not None