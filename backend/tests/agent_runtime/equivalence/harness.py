"""Phase 2.1 — Legacy ↔ LangGraph 等价测试 harness。

**Phase 2.1 范围**:只覆盖 LangGraph 端的事件序列,并以 ``expected_prefix``
断言节点按预期顺序发射事件。Legacy 端真对比留 Phase 2.2 接 AgentOrchestrator
时补全(legacy 直接走真实 orchestrator 改动太大,本阶段不动 Legacy 代码)。

harness 暴露:
* ``run_langgraph_pre_confirm``   — 跑 LangGraph v2 走到 pause_for_legacy_confirm
* ``run_langgraph_resume``        — 把决策合并到 state,继续跑

两个方法都返回 ``(events, final_state)``。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from app.agent_runtime.graph_registry import GraphRegistry
from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V2,
)
from app.agent_runtime.graphs.test_plan.state import make_empty_state
from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator


@dataclass
class HarnessResult:
    events: List[Dict[str, Any]]
    final_state: Dict[str, Any]
    pause_marker: str | None
    task_status: str | None


def _build_runtime_and_registry():
    """Phase 2.8R-D:Equivalence 测的是 v2 行为(合同测试),但要兼容 settings
    默认 v3 — registry 注册 v1+v2+v3,但 coordinator 的 state 仍然
    graph_version="v2"(被调用方透传)。
    """
    registry = GraphRegistry.build_default_v2_v3(checkpointer=None)
    return registry, GraphRuntimeService(registry=registry)


async def run_langgraph_pre_confirm(
    *,
    task_id: str,
    user_prompt: str,
    requirement_file_id: str | None,
    template_file_id: str | None,
    kb_skip_reason: str | None = None,
    graph_version: str | None = None,
) -> HarnessResult:
    """走 v2 图从 initialize_task 到 pause_for_legacy_confirm。

    返回 events(从 ``InMemoryEventSink`` 收集)+ final_state。

    Args:
        graph_version: Phase 2.9A.7 新增 — 默认 None(走 settings 默认 v3);
            部分合同测试(如 happy_path 期望 ``need_user_confirm`` 事件)
            需要显式传 ``GRAPH_VERSION_V2`` 让 v2 sentinel pause 路径生效。
    """
    from tests.agent_runtime.test_plan_v2.conftest import (
        StubToolExecutor,
        _null_session_cm,
        _StubCancel,
    )
    from app.agent_runtime.adapters.test_agent_tool_adapter import (
        TestAgentToolAdapter,
    )
    from app.agent_runtime.events.sink import InMemoryEventSink
    from app.agent_runtime.runtime_context import RuntimeContext
    from datetime import datetime

    sink = InMemoryEventSink()
    executor = StubToolExecutor()
    adapter = TestAgentToolAdapter(
        tool_executor=executor,
        event_sink=sink,
        session_factory=lambda: _null_session_cm(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )
    ctx = RuntimeContext(
        user_internal_id=1,
        task_internal_id=int(task_id.split("-")[-1]) if task_id.split("-")[-1].isdigit() else 100,
        conversation_internal_id=10,
        session_factory=lambda: _null_session_cm(),
        settings_service=None,
        event_sink=sink,
        cancellation_service=_StubCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=adapter,
    )

    registry, runtime = _build_runtime_and_registry()
    coord = LangGraphRunCoordinator(
        registry=registry,
        runtime=runtime,
        checkpointer=None,
        context_factory=lambda state: ctx,
    )

    outcome = await coord.run_pre_confirm(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={
            "user_prompt": user_prompt,
            "requirement_file_id": requirement_file_id,
            "template_file_id": template_file_id,
            "kb_skip_reason": kb_skip_reason,
            # Phase 2.9A.7: 若 caller 显式传 graph_version,跟随;
            # 否则走 settings 默认(通常 v3 + interrupt 路径)。
            "graph_version": graph_version,
        },
    )
    return HarnessResult(
        events=list(sink.collect()),
        final_state=dict(outcome.final_state),
        pause_marker=outcome.pause_marker,
        task_status=outcome.task_status,
    )


async def run_langgraph_post_confirm(
    *,
    task_id: str,
    restored_state: Dict[str, Any],
    section_confirm_config: Dict[str, Any] | None = None,
) -> HarnessResult:
    """把 section_confirm_config merge 到 restored_state,继续跑 post-confirm。"""
    from tests.agent_runtime.test_plan_v2.conftest import (
        _null_session_cm,
        _StubCancel,
    )
    from app.agent_runtime.adapters.test_agent_tool_adapter import (
        TestAgentToolAdapter,
    )
    from app.agent_runtime.events.sink import InMemoryEventSink
    from app.agent_runtime.runtime_context import RuntimeContext
    from datetime import datetime
    from tests.agent_runtime.test_plan_v2.conftest import StubToolExecutor

    sink = InMemoryEventSink()
    executor = StubToolExecutor()
    adapter = TestAgentToolAdapter(
        tool_executor=executor,
        event_sink=sink,
        session_factory=lambda: _null_session_cm(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )
    ctx = RuntimeContext(
        user_internal_id=1,
        task_internal_id=100,
        conversation_internal_id=10,
        session_factory=lambda: _null_session_cm(),
        settings_service=None,
        event_sink=sink,
        cancellation_service=_StubCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=adapter,
    )

    state = dict(restored_state)
    state["section_confirm_config"] = section_confirm_config or {"accepted": True}
    state["pause_marker"] = None
    # Phase 2.9A.7: 若 restored_state 没显式 graph_version,跟随
    # caller 的预_confirm 调用;否则保留 caller 传的值。
    state.setdefault("graph_version", GRAPH_VERSION_V2)

    registry, runtime = _build_runtime_and_registry()
    coord = LangGraphRunCoordinator(
        registry=registry,
        runtime=runtime,
        checkpointer=None,
        context_factory=lambda st: ctx,
    )
    outcome = await coord.run_post_confirm(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        restored_state=state,
    )
    return HarnessResult(
        events=list(sink.collect()),
        final_state=dict(outcome.final_state),
        pause_marker=outcome.pause_marker,
        task_status=outcome.task_status,
    )


__all__ = [
    "HarnessResult",
    "run_langgraph_pre_confirm",
    "run_langgraph_post_confirm",
]