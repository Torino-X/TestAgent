"""未注册的 graph version 抛 ``GraphVersionNotFound``。"""

from __future__ import annotations

import pytest

from app.agent_runtime.graph_registry import GraphRegistry, GraphVersionNotFound


def test_unknown_graph_raises() -> None:
    reg = GraphRegistry(name="t")
    with pytest.raises(GraphVersionNotFound):
        reg.get("ghost", "v9")


def test_unknown_schema_version_raises() -> None:
    reg = GraphRegistry(name="t")
    with pytest.raises(GraphVersionNotFound):
        reg.schema_version("ghost", "v9")