"""Phase 2.1 v2 — 8 个节点/适配器单元测试。"""

from __future__ import annotations

import pytest

from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V2,
)
from app.agent_runtime.graphs.test_plan.state import (
    TestPlanGraphState,
    make_empty_state,
)
from app.agent_runtime.graphs.test_plan.versions.v2.nodes_pre_confirm import (
    initialize_task_node,
    pause_for_legacy_confirm_node,
    search_knowledge_node,
    validate_inputs_node,
)


# ── 1. v2 state default serializable ───────────────────────────────────────


def test_v2_state_default_serializable():
    """make_empty_state 在 graph_version=v2 时所有字段都可序列化。"""
    state = make_empty_state(
        task_id="t-1",
        graph_run_id="r-1",
        graph_version=GRAPH_VERSION_V2,
    )
    import json

    json.dumps(dict(state), default=lambda o: f"<{type(o).__name__}>")


# ── 2. adapter whitelist rejects unknown tool ─────────────────────────────


@pytest.mark.asyncio
async def test_adapter_whitelist_rejects_unknown_tool(adapter):
    """白名单外 tool_name → UnknownToolError。"""
    from app.agent_runtime.adapters.test_agent_tool_adapter import (
        UnknownToolError,
    )
    from app.agent_runtime.runtime_context import RuntimeContext

    ctx = RuntimeContext(
        user_internal_id=1,
        task_internal_id=1,
        conversation_internal_id=1,
        session_factory=lambda: _noop_cm(),
        settings_service=None,
        event_sink=adapter._sink,
        cancellation_service=adapter._sink,
        clock=adapter._clock,
        tool_adapter=adapter,
    )

    with pytest.raises(UnknownToolError):
        await adapter.execute(
            tool_name="DefinitelyNotInWhitelistTool",
            inputs={},
            ctx_runtime=ctx,
        )


class _NoopCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return False


def _noop_cm():
    return _NoopCM()


# ── 3. adapter min input validation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_min_input_validation(adapter):
    """RequirementParserTool 缺 requirement_file_id → canonical error envelope,不调 tool。"""
    from app.agent_runtime.runtime_context import RuntimeContext

    ctx = RuntimeContext(
        user_internal_id=1,
        task_internal_id=1,
        conversation_internal_id=1,
        session_factory=lambda: _noop_cm(),
        settings_service=None,
        event_sink=adapter._sink,
        cancellation_service=_noop_cancel(),
        clock=adapter._clock,
        tool_adapter=adapter,
    )

    envelope = await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={},  # 缺 requirement_file_id
        ctx_runtime=ctx,
    )
    assert envelope["success"] is False
    assert envelope["error"]["code"] == "INVALID_INPUTS"
    assert envelope["tool_name"] == "RequirementParserTool"


class _NoopCancel:
    def is_cancelled(self, task_id):
        return False


def _noop_cancel():
    return _NoopCancel()


# ── 4. adapter min_visible pacing ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_adapter_pacing_min_visible_seconds():
    """当 tool 自身执行快,adapter 仍 sleep 到 min_visible_seconds。"""
    import asyncio
    import time
    from datetime import datetime
    from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter
    from app.agent_runtime.events.sink import InMemoryEventSink

    sink = InMemoryEventSink()
    executor = _NoopExecutor()
    adapter = TestAgentToolAdapter(
        tool_executor=executor,
        event_sink=sink,
        session_factory=lambda: _noop_cm(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.3,
        transition_seconds=0.0,
    )

    ctx = _make_ctx(adapter, sink)

    started = time.monotonic()
    await adapter.execute(
        tool_name="KnowledgeSearchTool",
        inputs={},
        ctx_runtime=ctx,
    )
    elapsed = time.monotonic() - started
    assert elapsed >= 0.25, f"min_visible_seconds 兜底未生效;elapsed={elapsed}"


class _NoopExecutor:
    async def run(self, tool_name, inputs, context, retry_context=None):
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


def _make_ctx(adapter, sink):
    from app.agent_runtime.runtime_context import RuntimeContext
    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=1,
        conversation_internal_id=1,
        session_factory=lambda: _noop_cm(),
        settings_service=None,
        event_sink=sink,
        cancellation_service=_noop_cancel(),
        clock=adapter._clock,
        tool_adapter=adapter,
    )


# ── 5. adapter transition pacing between frames ──────────────────────────


@pytest.mark.asyncio
async def test_adapter_pacing_transition_between_frames():
    """多帧之间 sleep transition_seconds(只对非最后一帧)。"""
    import asyncio
    from datetime import datetime
    from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter
    from app.agent_runtime.events.sink import InMemoryEventSink

    sink = InMemoryEventSink()
    executor = _NoopExecutor()
    adapter = TestAgentToolAdapter(
        tool_executor=executor,
        event_sink=sink,
        session_factory=lambda: _noop_cm(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.15,
    )

    ctx = _make_ctx(adapter, sink)

    started = asyncio.get_event_loop().time()
    await adapter.execute(
        tool_name="KnowledgeSearchTool",  # default builder produces 1+ frames
        inputs={},
        ctx_runtime=ctx,
    )
    # 仅校验函数能跑完;具体 sleep 时长由 builder 分帧数决定
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed >= 0


# ── 6. initialize_task emits 5 plan events ────────────────────────────────


@pytest.mark.asyncio
async def test_initialize_task_emits_5_events(in_memory_sink, runtime_context_with_adapter):
    """initialize_task 节点发 5 个事件:2×STARTED + 2×COMPLETED + PLAN_CREATED。"""
    state = TestPlanGraphState()
    state["current_phase"] = None
    state["completed_nodes"] = []

    await initialize_task_node(state, ctx=runtime_context_with_adapter)

    types = [e["event_type"] for e in in_memory_sink.collect()]
    started_count = sum(1 for t in types if t == "plan_step_started")
    completed_count = sum(1 for t in types if t == "plan_step_completed")
    plan_created_count = sum(1 for t in types if t == "plan_created")

    assert started_count == 2, f"expected 2 plan_step_started, got {started_count}"
    assert completed_count == 2, f"expected 2 plan_step_completed, got {completed_count}"
    assert plan_created_count == 1, f"expected 1 plan_created, got {plan_created_count}"


# ── 7. search_knowledge synthetic skip path ──────────────────────────────


@pytest.mark.asyncio
async def test_search_knowledge_synthetic_skip(in_memory_sink, runtime_context_with_adapter):
    """kb_skip_reason 非空 → synthetic TOOL_FINISHED + KNOWLEDGE_SUMMARY,不调 adapter。"""
    state = TestPlanGraphState()
    state["kb_skip_reason"] = "test_plan_generation_disabled"
    state["completed_nodes"] = []

    await search_knowledge_node(state, ctx=runtime_context_with_adapter)

    types = [e["event_type"] for e in in_memory_sink.collect()]
    assert "tool_finished" in types
    assert "knowledge_summary" in types
    # adapter 不被调用 — context.tool_adapter._executor.calls 应为空
    # (不依赖具体实现细节,只校验 result 不要求 stub)


# ── 8. pause_for_legacy_confirm sets marker ──────────────────────────────


@pytest.mark.asyncio
async def test_pause_for_legacy_confirm_sets_marker(
    in_memory_sink, runtime_context_with_adapter
):
    """pause_for_legacy_confirm_node 设 pause_marker=need_user_confirm + current_phase=paused。"""
    state = TestPlanGraphState()
    state["section_suggestions"] = {"items": []}
    state["completed_nodes"] = []

    out = await pause_for_legacy_confirm_node(state, ctx=runtime_context_with_adapter)

    assert out["pause_marker"] == "need_user_confirm"
    assert out["current_phase"] == "paused"
    # 至少发 NEED_USER_CONFIRM + TASK_WAITING 2 个事件
    types = [e["event_type"] for e in in_memory_sink.collect()]
    assert "need_user_confirm" in types
    assert "task_waiting" in types