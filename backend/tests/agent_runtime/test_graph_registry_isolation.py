"""测试用 registry 隔离 - build 多个实例互不影响。"""

from __future__ import annotations

from app.agent_runtime.graph_registry import GraphRegistry


class _FakeCompiled:
    def __init__(self, name: str) -> None:
        self.name = name


def test_two_registries_are_isolated() -> None:
    reg_a = GraphRegistry(name="A")
    reg_b = GraphRegistry(name="B")
    reg_a.register("g", "v1", _FakeCompiled("A"))
    assert reg_b.list_versions("g") == []


def test_isolated_registry_does_not_see_default() -> None:
    """显式 build 的空 registry 不应包含 v1。"""
    reg = GraphRegistry(name="empty")
    assert reg.list_versions("test_plan_generation") == []