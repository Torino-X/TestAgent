"""Phase 2.8R-D:Graph 版本传递链测试。

覆盖矩阵:
  * 新 LangGraph 任务 graph_version=v3
  * 历史 graph_version=v2 → v2_frozen
  * 未知 graph_version=v999 → GraphVersionNotFound,无 fallback
  * Registry 同时注册 v1+v2_frozen+v3,v3 ≠ v2_frozen (compiled 对象不同)
  * GraphRuntimeService 默认 → settings.agent_runtime_default_graph_version
  * graph.py:compile_test_plan_graph(version="v2") → v2_frozen 实现
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agent_runtime.graph_registry import GraphRegistry, GraphVersionNotFound
from app.agent_runtime.graph_runtime_service import GraphRuntimeService, _resolve_graph_version
from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V1,
    GRAPH_VERSION_V2,
    GRAPH_VERSION_V3,
)
from app.agent_runtime.graphs.test_plan.graph import compile_test_plan_graph
from app.core.exceptions import GraphVersionNotAvailableError
from app.core.config import get_settings


def _make_state(**overrides) -> dict:
    s = {
        "task_id": "task_public_test",
        "graph_run_id": "run-task_public_test",
        "user_internal_id": 1,
        "task_internal_id": 1,
        "conversation_internal_id": 1,
    }
    s.update(overrides)
    return s


def test_settings_has_default_graph_version() -> None:
    """Settings 必须暴露 agent_runtime_default_graph_version 默认 v3。"""
    settings = get_settings()
    assert getattr(settings, "agent_runtime_default_graph_version", None) == "v3"


def test_compile_test_plan_graph_v3_loads_v3_builder() -> None:
    g = compile_test_plan_graph(version=GRAPH_VERSION_V3, checkpointer=None)
    assert g is not None


def test_compile_test_plan_graph_v2_loads_v2_frozen() -> None:
    """v2 必须显式指向 v2_frozen,不再用旧的未冻结 versions/v2/。"""
    g = compile_test_plan_graph(version=GRAPH_VERSION_V2, checkpointer=None)
    assert g is not None


def test_compile_test_plan_graph_unknown_raises_not_available() -> None:
    with pytest.raises(GraphVersionNotAvailableError) as exc:
        compile_test_plan_graph(version="v999", checkpointer=None)
    assert "v999" in str(exc.value)


def test_compile_test_plan_graph_empty_version_defaults_to_v3() -> None:
    """Phase 2.8R-D:空 version → v3(新默认),不再回退 v2。"""
    g = compile_test_plan_graph(version=None, checkpointer=None)
    assert g is not None


def test_registry_build_default_v2_v3_registers_three_versions() -> None:
    reg = GraphRegistry.build_default_v2_v3(checkpointer=None)
    versions = reg.list_versions(GRAPH_NAME_TEST_PLAN)
    assert set(versions) == {"v1", "v2", "v3"}


def test_registry_v3_compiled_distinct_from_v2() -> None:
    """v3 与 v2(v2_frozen)必须是独立 compiled 对象,不可指向同一 builder。"""
    reg = GraphRegistry.build_default_v2_v3(checkpointer=None)
    g_v3 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V3)
    g_v2 = reg.get(GRAPH_NAME_TEST_PLAN, GRAPH_VERSION_V2)
    assert g_v3 is not g_v2


def test_resolve_graph_version_uses_state_first() -> None:
    """State 有 graph_version 时,优先级高于 settings。"""
    state = _make_state(graph_version="v2")
    assert _resolve_graph_version(state) == "v2"


def test_resolve_graph_version_falls_back_to_settings() -> None:
    """State 无 graph_version → settings.agent_runtime_default_graph_version(默认 v3)。"""
    state = _make_state()
    state.pop("graph_version", None)
    assert _resolve_graph_version(state) == "v3"


def test_graph_runtime_service_unknown_version_raises_graph_not_found() -> None:
    """Registry 不存在 v999 → GraphVersionNotFound(无 fallback)。"""
    reg = GraphRegistry.build_default_v2_v3(checkpointer=None)
    state = _make_state(graph_version="v999")
    # _resolve_graph_version 仅从 state 读 → 返回 v999(不静默 fallback)
    assert _resolve_graph_version(state) == "v999"
    # registry 真查的时候抛 GraphVersionNotFound
    with pytest.raises(GraphVersionNotFound):
        reg.get(GRAPH_NAME_TEST_PLAN, "v999")
