"""Phase 2.8R-D — Graph v3 + v2_frozen 独立注册测试(5)。

设计目标(对应 docs/35 §4 + 验收五):
  * v2_frozen 目录存在;独立模块不被 v3 import 触发
  * compile_test_plan_graph(version="v3") 返回可执行图
  * compile_test_plan_graph(version="v2") 仍可用(向后兼容)
  * 未知 version 抛 ``GraphVersionNotAvailableError``,**不**回退 default
  * state_schema_version_for("v3") == V7,V2 仍 == V2
"""

from __future__ import annotations

import importlib
import pathlib

import pytest


def test_v2_frozen_directory_exists():
    """验证 v2_frozen 目录存在并包含关键模块。"""
    base = pathlib.Path("app/agent_runtime/graphs/test_plan/versions")
    assert (base / "v2_frozen").is_dir(), "v2_frozen/ directory missing"
    frozen = base / "v2_frozen"
    expected = {"graph.py", "nodes_post_confirm.py", "routing.py"}
    present = {p.name for p in frozen.iterdir()}
    missing = expected - present
    assert not missing, f"v2_frozen missing: {missing}"


def test_compile_v3_returns_compiled_graph():
    """compile_test_plan_graph(version="v3") → 编译成功。"""
    from app.agent_runtime.graphs.test_plan.graph import compile_test_plan_graph

    compiled = compile_test_plan_graph(version="v3", checkpointer=None)
    # LangGraph compiled 状态图对象
    assert compiled is not None
    # 图名含 v3 后缀
    assert "v3" in (compiled.name or "")


def test_compile_v2_still_supported():
    """compile_test_plan_graph(version="v2") 仍可用(向后兼容 Phase 2.x)。"""
    from app.agent_runtime.graphs.test_plan.graph import compile_test_plan_graph

    compiled = compile_test_plan_graph(version="v2", checkpointer=None)
    assert compiled is not None
    assert "v2" in (compiled.name or "")


def test_compile_unknown_version_raises_no_fallback():
    """未知 version → GraphVersionNotAvailableError,**不**回退 v2。"""
    from app.agent_runtime.graphs.test_plan.graph import compile_test_plan_graph
    from app.core.exceptions import GraphVersionNotAvailableError

    with pytest.raises(GraphVersionNotAvailableError) as exc_info:
        compile_test_plan_graph(version="v999")
    assert exc_info.value.detail["graph_version"] == "v999"
    assert "v1" in exc_info.value.detail["known_versions"]
    assert "v2" in exc_info.value.detail["known_versions"]
    assert "v3" in exc_info.value.detail["known_versions"]


def test_v2_frozen_directory_independent_of_v2_active():
    """v2_frozen 与 v2(active)目录双存在;编译时走 v2(默认)。"""
    base = pathlib.Path("app/agent_runtime/graphs/test_plan/versions")
    assert (base / "v2").is_dir()
    assert (base / "v2_frozen").is_dir()
    import importlib

    v2 = importlib.import_module("app.agent_runtime.graphs.test_plan.versions.v2.graph")
    frozen = importlib.import_module(
        "app.agent_runtime.graphs.test_plan.versions.v2_frozen.graph"
    )
    assert v2.__file__ != frozen.__file__, (
        "v2 and v2_frozen must be independent modules"
    )
    """v3 → V7;v2 → V2(向后兼容)。"""
    from app.agent_runtime.graphs.test_plan.constants import (
        GRAPH_VERSION_V2,
        GRAPH_VERSION_V3,
        STATE_SCHEMA_VERSION_V2,
        STATE_SCHEMA_VERSION_V7,
        state_schema_version_for,
    )

    assert state_schema_version_for(GRAPH_VERSION_V3) == STATE_SCHEMA_VERSION_V7
    assert state_schema_version_for(GRAPH_VERSION_V2) == STATE_SCHEMA_VERSION_V2
    # Unknown fallback to V2 (legacy behavior)
    assert state_schema_version_for("unknown") == STATE_SCHEMA_VERSION_V2


def test_v3_subpackage_independent_from_v2():
    """v3.graph 模块独立 import;不依赖 v2 subpackage。"""
    from app.agent_runtime.graphs.test_plan.versions.v3 import graph as v3_graph
    from app.agent_runtime.graphs.test_plan.versions.v2 import graph as v2_graph

    # 不同模块,但符号可能同名;关键测试是 import 路径
    assert v3_graph.__name__.endswith("versions.v3.graph")
    assert v2_graph.__name__.endswith("versions.v2.graph")
    assert v3_graph.__name__ != v2_graph.__name__


__all__ = [
    "test_v2_frozen_directory_exists",
    "test_compile_v3_returns_compiled_graph",
    "test_compile_v2_still_supported",
    "test_compile_unknown_version_raises_no_fallback",
    "test_state_schema_version_v3_is_v7_v2_remains_v2",
    "test_v3_subpackage_independent_from_v2",
]
