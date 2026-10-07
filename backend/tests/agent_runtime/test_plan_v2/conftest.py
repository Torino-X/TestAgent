"""Phase 2.1 v2 测试套件 — fixtures。

提供:
* ``compiled_v2_graph``         — 编译好的 v2 graph,无 checkpointer(可重入跑)
* ``in_memory_sink``            — 事件收集器
* ``stub_tool_executor``        — 工具 stub,白名单 9 工具集返回固定 envelope
* ``runtime_context_with_adapter`` — RuntimeContext 注入 adapter
* ``run_pre_confirm``           — 走 coordinator.run_pre_confirm 的便捷方法
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import pytest

from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter
from app.agent_runtime.events.sink import InMemoryEventSink
from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graph_registry import GraphRegistry
from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V2,
)
from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
from app.agent_runtime.runtime_context import RuntimeContext


# ── compiled v2 graph ─────────────────────────────────────────────────────


@pytest.fixture
def compiled_v2_graph():
    """Phase 2.1 v2 编译图;无 checkpointer(单跑场景)。"""
    from app.agent_runtime.graphs.test_plan.versions.v2 import build_test_plan_v2_graph

    return build_test_plan_v2_graph(checkpointer=None)


@pytest.fixture
def runtime_v2():
    """GraphRuntimeService + v2 registry;tests 直接调 ainvoke。"""
    registry = GraphRegistry.build_default_v2(checkpointer=None)
    return GraphRuntimeService(registry=registry)


@pytest.fixture
def registry_v2_no_ckpt():
    return GraphRegistry.build_default_v2(checkpointer=None)


# ── event sink + stub tool executor ───────────────────────────────────────


@pytest.fixture
def in_memory_sink() -> InMemoryEventSink:
    return InMemoryEventSink()


class StubToolExecutor:
    """工具 stub:按 tool_name 返回预设 envelope。"""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.envelopes: Dict[str, Dict[str, Any]] = {}

    def register(self, tool_name: str, envelope: Dict[str, Any]) -> None:
        self.envelopes[tool_name] = envelope

    async def run(
        self,
        tool_name: str,
        inputs: Dict[str, Any],
        context: Any,
        retry_context: Any = None,
    ) -> Dict[str, Any]:
        self.calls.append({
            "tool_name": tool_name,
            "inputs": dict(inputs),
        })
        if tool_name in self.envelopes:
            return dict(self.envelopes[tool_name])
        return {
            "success": True,
            "tool_name": tool_name,
            "task_id": "stub",
            "data": {},
            "summary": "",
            "warnings": [],
            "error": None,
            "duration_ms": 1,
            "attempt": 1,
        }


@pytest.fixture
def stub_executor() -> StubToolExecutor:
    return StubToolExecutor()


# ── adapter + runtime_context ─────────────────────────────────────────────


def _null_session_cm():
    class _N:
        async def __aenter__(self_inner):
            return None

        async def __aexit__(self_inner, *args):
            return False

    return _N()


class _StubCancel:
    def is_cancelled(self, task_id: str) -> bool:  # pragma: no cover
        return False


@pytest.fixture
def stub_session_factory():
    return lambda: _null_session_cm()


@pytest.fixture
def adapter(
    stub_executor: StubToolExecutor,
    in_memory_sink: InMemoryEventSink,
    stub_session_factory,
):
    return TestAgentToolAdapter(
        tool_executor=stub_executor,
        event_sink=in_memory_sink,
        session_factory=stub_session_factory,
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,  # 测试跳过 pacing
        transition_seconds=0.0,
    )


@pytest.fixture
def runtime_context_with_adapter(
    adapter, in_memory_sink, stub_session_factory
):
    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=100,
        conversation_internal_id=10,
        session_factory=stub_session_factory,
        settings_service=None,
        event_sink=in_memory_sink,
        cancellation_service=_StubCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=adapter,
    )


# ── coordinator ───────────────────────────────────────────────────────────


@pytest.fixture
def coordinator(runtime_v2, registry_v2_no_ckpt):
    """LangGraphRunCoordinator — 每个测试用新实例。"""
    from app.agent_runtime.langgraph_run_coordinator import (
        LangGraphRunCoordinator,
    )

    return LangGraphRunCoordinator(
        registry=registry_v2_no_ckpt,
        runtime=runtime_v2,
        checkpointer=None,
    )


# ── 初始 state helpers ────────────────────────────────────────────────────


def make_pre_confirm_payload(
    *,
    task_id: str = "t-test",
    user_prompt: str = "请生成测试方案",
    requirement_file_id: Optional[str] = "req-1",
    template_file_id: Optional[str] = "tpl-1",
    kb_skip_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "task_id": task_id,
        "graph_run_id": f"run-{task_id}",
        "graph_name": GRAPH_NAME_TEST_PLAN,
        "graph_version": GRAPH_VERSION_V2,
        "user_prompt": user_prompt,
        "requirement_file_id": requirement_file_id,
        "template_file_id": template_file_id,
        "kb_skip_reason": kb_skip_reason,
    }