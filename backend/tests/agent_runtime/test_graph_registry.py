"""GraphRegistry 基本行为。"""

from __future__ import annotations

import pytest

from app.agent_runtime.graph_registry import GraphRegistry, GraphVersionNotFound
from app.agent_runtime.graphs.dynamic_agent import (
    GRAPH_NAME_DYNAMIC_AGENT,
    GRAPH_VERSION_DYNAMIC_AGENT_V1,
    GRAPH_VERSION_DYNAMIC_AGENT_V3,
)
from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V1,
)


class _FakeCompiled:
    def __init__(self, name: str) -> None:
        self.name = name


def test_register_then_get() -> None:
    reg = GraphRegistry(name="t")
    compiled = _FakeCompiled("v1")
    reg.register("alpha", "v1", compiled, schema_version=2)
    assert reg.get("alpha", "v1") is compiled
    assert reg.schema_version("alpha", "v1") == 2


def test_unknown_version_raises() -> None:
    reg = GraphRegistry(name="t")
    with pytest.raises(GraphVersionNotFound):
        reg.get("missing", "v9")


def test_double_register_raises() -> None:
    reg = GraphRegistry(name="t")
    reg.register("alpha", "v1", _FakeCompiled("v1"))
    with pytest.raises(ValueError):
        reg.register("alpha", "v1", _FakeCompiled("v1"))


def test_list_versions_filters_by_name() -> None:
    reg = GraphRegistry(name="t")
    reg.register("alpha", "v1", _FakeCompiled("a1"))
    reg.register("alpha", "v2", _FakeCompiled("a2"))
    reg.register("beta", "v1", _FakeCompiled("b1"))
    assert reg.list_versions("alpha") == ["v1", "v2"]
    assert reg.list_versions("beta") == ["v1"]
    assert reg.list_versions("missing") == []


def test_default_v2_v3_registers_dynamic_agent_v1_and_v3() -> None:
    reg = GraphRegistry.build_default_v2_v3()

    assert GRAPH_VERSION_DYNAMIC_AGENT_V1 in reg.list_versions(GRAPH_NAME_DYNAMIC_AGENT)
    assert GRAPH_VERSION_DYNAMIC_AGENT_V3 in reg.list_versions(GRAPH_NAME_DYNAMIC_AGENT)
    assert reg.schema_version(GRAPH_NAME_DYNAMIC_AGENT, GRAPH_VERSION_DYNAMIC_AGENT_V1) == 1
    assert reg.schema_version(GRAPH_NAME_DYNAMIC_AGENT, GRAPH_VERSION_DYNAMIC_AGENT_V3) == 1
