"""Phase 2.2 interrupt / resume 测试 — 15 类场景。

**目录**:tests/agent_runtime/test_plan_v2/test_v2_interrupts.py
**目标**:覆盖 §五 "interrupt 路径正确性" + §六 "旧接口兼容" + §七 "15 类测试"。

每类场景独立函数。fixture 来自 ``conftest.py``(registry / runtime / coordinator
/ in-memory checkpointer)。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.enums import TaskStatus
from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter
from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V2,
    GRAPH_VERSION_V3,
)
from app.agent_runtime.confirmation_timeout_service import (
    DEFAULT_TIMEOUT_SECONDS,
    ConfirmationTimeoutService,
)
from app.agent_runtime.events.sink import InMemoryEventSink
from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graph_registry import GraphRegistry
from app.agent_runtime.graphs.test_plan.state import make_empty_state
from app.agent_runtime.graphs.test_plan.versions.v2 import build_compiled_v2_graph
from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
from app.agent_runtime.runtime_context import RuntimeContext
from app.models.human_confirmation import HumanConfirmation


# ── Fixtures ──────────────────────────────────────────────────────────────


def _null_cm():
    class _N:
        async def __aenter__(self_):
            return None

        async def __aexit__(self_, *a):
            return False

    return _N()


class _NC:
    def is_cancelled(self, x):
        return False


class _Ex:
    """通用 stub tool executor;默认 success;可通过 register 覆盖返回。"""

    def __init__(self):
        self.calls: List[str] = []
        self.envelopes: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, envelope: Dict[str, Any]) -> None:
        self.envelopes[name] = envelope

    async def run(self, tool_name, inputs, context, retry_context=None):
        self.calls.append(tool_name)
        if tool_name in self.envelopes:
            return dict(self.envelopes[tool_name])
        # Phase 2.9A.10:TestPlanGeneratorTool 默认必须返回带
        # section_package 的成功响应,否则 ``generate_test_plan_node``
        # 的内容校验会触发 GENERATION_OUTPUT_INVALID,v2_interrupts
        # 集成测试无法跑完。
        if tool_name == "TestPlanGeneratorTool":
            return {
                "success": True,
                "tool_name": tool_name,
                "task_id": "stub",
                "data": {
                    "generated_sections": 1,
                    "kept_sections": 0,
                    "manual_sections": 0,
                    "total_word_count": 100,
                    "tables_generated": 0,
                    "section_package": {
                        "generated_sections": [
                            {
                                "section_id": "stub_s1",
                                "title": "Stub",
                                "content": "Generated stub content " * 20,
                            }
                        ],
                        "keep_sections": [],
                        "manual_sections": [],
                    },
                },
                "summary": "",
                "warnings": [],
                "error": None,
                "duration_ms": 1,
                "attempt": 1,
            }
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


def _make_runtime_context(
    *, task_internal_id: int = 100, sink: InMemoryEventSink
) -> RuntimeContext:
    exec_ = _Ex()
    adapter = TestAgentToolAdapter(
        tool_executor=exec_,
        event_sink=sink,
        session_factory=lambda: _null_cm(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )
    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=task_internal_id,
        conversation_internal_id=10,
        session_factory=lambda: _null_cm(),
        settings_service=None,
        event_sink=sink,
        cancellation_service=_NC(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=adapter,
    )


def _make_coordinator(
    *,
    with_checkpointer: bool = True,
    interrupt_enabled: bool = True,
) -> tuple[LangGraphRunCoordinator, _Ex, InMemoryEventSink]:
    ck = InMemorySaver() if with_checkpointer else None
    registry = GraphRegistry(name="t_v2_interrupts")
    # Phase 2.8R-D:测试 fixture 注册 v2(指向 v2_frozen) + v3,确保
    # settings 默认 v3 不会触发 GraphVersionNotFound。tests 设 state
    # graph_version=v2 验证 v2 interrupt 行为。
    registry.register(
        GRAPH_NAME_TEST_PLAN,
        GRAPH_VERSION_V2,
        build_compiled_v2_graph(checkpointer=ck, interrupt_enabled=interrupt_enabled),
        schema_version=2,
    )
    from app.agent_runtime.graphs.test_plan.versions.v3 import build_test_plan_v3_graph

    registry.register(
        GRAPH_NAME_TEST_PLAN,
        GRAPH_VERSION_V3,
        build_test_plan_v3_graph(checkpointer=ck, interrupt_enabled=interrupt_enabled),
        schema_version=7,
    )
    runtime = GraphRuntimeService(registry=registry)
    sink = InMemoryEventSink()
    exec_ = _Ex()
    adapter = TestAgentToolAdapter(
        tool_executor=exec_,
        event_sink=sink,
        session_factory=lambda: _null_cm(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )
    ctx = RuntimeContext(
        user_internal_id=1,
        task_internal_id=100,
        conversation_internal_id=10,
        session_factory=lambda: _null_cm(),
        settings_service=None,
        event_sink=sink,
        cancellation_service=_NC(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=adapter,
    )
    coord = LangGraphRunCoordinator(
        registry=registry,
        runtime=runtime,
        checkpointer=ck,
        context_factory=lambda s: ctx,
    )
    return coord, exec_, sink


@pytest.mark.asyncio
async def test_resume_rebuilds_runtime_context_from_checkpoint_identity() -> None:
    """The resumed graph must retain the database identities from its checkpoint."""
    ck = InMemorySaver()
    registry = GraphRegistry(name="t_v2_resume_identity")
    registry.register(
        GRAPH_NAME_TEST_PLAN,
        GRAPH_VERSION_V2,
        build_compiled_v2_graph(checkpointer=ck, interrupt_enabled=True),
        schema_version=2,
    )
    from app.agent_runtime.graphs.test_plan.versions.v3 import build_test_plan_v3_graph

    registry.register(
        GRAPH_NAME_TEST_PLAN,
        GRAPH_VERSION_V3,
        build_test_plan_v3_graph(checkpointer=ck, interrupt_enabled=True),
        schema_version=7,
    )
    runtime = GraphRuntimeService(registry=registry)
    sink = InMemoryEventSink()
    captured_states: list[dict[str, Any]] = []

    def context_factory(state: dict[str, Any]) -> RuntimeContext:
        captured_states.append(dict(state))
        return _make_runtime_context(
            task_internal_id=int(state.get("task_internal_id") or 0),
            sink=sink,
        )

    coordinator = LangGraphRunCoordinator(
        registry=registry,
        runtime=runtime,
        checkpointer=ck,
        context_factory=context_factory,
    )
    task_id = "t-int-runtime-identity"

    await coordinator.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={
            "user_prompt": "test",
            "requirement_file_id": "r1",
            "template_file_id": "t1",
            "conversation_id": "conv-public",
            "user_id": "user-public",
            "task_internal_id": 701,
            "conversation_internal_id": 702,
            "user_internal_id": 703,
        },
    )
    assert captured_states[-1]["task_internal_id"] == 701
    assert captured_states[-1]["conversation_internal_id"] == 702
    assert captured_states[-1]["user_internal_id"] == 703

    captured_states.clear()
    await coordinator.resume_section_confirmation(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={
            "kind": "section_confirmation",
            "sections": [{"id": "s1", "accepted": True}],
            "source": "user",
        },
    )

    assert captured_states[-1]["task_internal_id"] == 701
    assert captured_states[-1]["conversation_internal_id"] == 702
    assert captured_states[-1]["user_internal_id"] == 703


# ── 1) interrupt 触发 + Command(resume) 恢复完整链路 ──────────────────


@pytest.mark.asyncio
async def test_01_interrupt_triggers_then_resume_completes() -> None:
    """全新任务触发 section interrupt,Command(resume=user) 走完到 completed。"""
    coord, _, _ = _make_coordinator()
    task_id = "t-int-1"

    o1 = await coord.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={
            "user_prompt": "测试",
            "requirement_file_id": "r1",
            "template_file_id": "t1",
        },
    )
    assert o1.paused is True
    assert o1.pause_marker == "section_confirmation_interrupt"

    o2 = await coord.resume_section_confirmation(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={
            "kind": "section_confirmation",
            "sections": [{"id": "s1", "title": "第1节", "accepted": True}],
            "source": "user",
        },
    )
    assert o2.paused is False
    assert o2.task_status in ("exporting", "completed")
    assert o2.final_state.get("section_confirm_config")["source"] == "user"


# ── 2) 重复 resume 防御(第二次 resume 应在 graph 已走完情况下无害) ────


@pytest.mark.asyncio
async def test_02_double_resume_section_then_format_loss_promotes() -> None:
    """section resume 完成后,format_check 仍可能触发 format_loss interrupt
    — 这是合法流程;不是"重复 resume"问题。
    这里验证:section resume 一次,再 resume_format_loss_interrupt,流程不抛。
    """
    coord, exec_, _ = _make_coordinator()
    exec_.register(
        "DocxFormatCheckTool",
        {
            "success": True,
            "tool_name": "DocxFormatCheckTool",
            "task_id": "stub",
            "data": {"level": "loss_detected", "losses": []},
            "summary": "loss_detected",
            "warnings": [],
            "error": None,
            "duration_ms": 1,
            "attempt": 1,
        },
    )
    task_id = "t-int-2"
    await coord.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={"user_prompt": "x", "requirement_file_id": "r", "template_file_id": "t"},
    )
    await coord.resume_section_confirmation(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={"kind": "section_confirmation", "sections": [{"id": "s", "accepted": True}], "source": "user"},
    )
    out = await coord.resume_format_loss_interrupt(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={"kind": "format_loss", "decision": "accept", "source": "user"},
    )
    assert out.task_status == "completed"


# ── 3) source=timeout resume 也通 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_03_resume_with_timeout_source() -> None:
    """timeout-driven Command(resume=..., source=timeout) 走通。"""
    coord, _, _ = _make_coordinator()
    task_id = "t-int-3"
    await coord.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={"user_prompt": "x", "requirement_file_id": "r", "template_file_id": "t"},
    )
    out = await coord.resume_section_confirmation(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={
            "kind": "section_confirmation",
            "sections": [{"id": "s", "accepted": True}],
            "source": "timeout",
        },
    )
    scc = out.final_state.get("section_confirm_config") or {}
    assert scc.get("source") == "timeout"


# ── 4) accept 决策通(format loss) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_04_format_loss_resume_accept() -> None:
    """format_loss interrupt + accept 决策 → completed。"""
    coord, exec_, _ = _make_coordinator()
    exec_.register(
        "DocxFormatCheckTool",
        {
            "success": True,
            "tool_name": "DocxFormatCheckTool",
            "task_id": "stub",
            "data": {
                "level": "loss_detected",
                "losses": [{"category": "image", "description": "图片压损"}],
            },
            "summary": "loss_detected",
            "warnings": [],
            "error": None,
            "duration_ms": 1,
            "attempt": 1,
        },
    )
    task_id = "t-int-4"
    await coord.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={"user_prompt": "x", "requirement_file_id": "r", "template_file_id": "t"},
    )
    await coord.resume_section_confirmation(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={"kind": "section_confirmation", "sections": [{"id": "s", "accepted": True}], "source": "user"},
    )
    out = await coord.resume_format_loss_interrupt(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={"kind": "format_loss", "decision": "accept", "source": "user"},
    )
    assert out.task_status == "completed"
    conf = out.final_state.get("format_loss_confirmation") or {}
    assert conf.get("decision") == "accept"


# ── 5) retry 决策 → prepare_export 重试 ────────────────────────────────


@pytest.mark.asyncio
async def test_05_format_loss_resume_retry() -> None:
    coord, exec_, _ = _make_coordinator()
    exec_.register(
        "DocxFormatCheckTool",
        {
            "success": True,
            "tool_name": "DocxFormatCheckTool",
            "task_id": "stub",
            "data": {"level": "loss_detected", "losses": []},
            "summary": "loss_detected",
            "warnings": [],
            "error": None,
            "duration_ms": 1,
            "attempt": 1,
        },
    )
    task_id = "t-int-5"
    await coord.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={"user_prompt": "x", "requirement_file_id": "r", "template_file_id": "t"},
    )
    await coord.resume_section_confirmation(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={"kind": "section_confirmation", "sections": [{"id": "s", "accepted": True}], "source": "user"},
    )
    out = await coord.resume_format_loss_interrupt(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={"kind": "format_loss", "decision": "retry", "source": "user"},
    )
    # retry 后状态变 exporting(进入下一保真度重试);pause_marker 应该清空
    assert out.paused is False or out.pause_marker is None
    assert out.task_status in ("exporting", "completed")


# ── 6) reject 决策 → fail_task ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_06_format_loss_resume_reject() -> None:
    coord, exec_, _ = _make_coordinator()
    exec_.register(
        "DocxFormatCheckTool",
        {
            "success": True,
            "tool_name": "DocxFormatCheckTool",
            "task_id": "stub",
            "data": {"level": "loss_detected", "losses": []},
            "summary": "loss_detected",
            "warnings": [],
            "error": None,
            "duration_ms": 1,
            "attempt": 1,
        },
    )
    task_id = "t-int-6"
    await coord.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={"user_prompt": "x", "requirement_file_id": "r", "template_file_id": "t"},
    )
    await coord.resume_section_confirmation(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={"kind": "section_confirmation", "sections": [{"id": "s", "accepted": True}], "source": "user"},
    )
    out = await coord.resume_format_loss_interrupt(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={"kind": "format_loss", "decision": "reject", "source": "user"},
    )
    assert out.task_status == "failed"


# ── 7) resume validation: payload 错误 kind 抛 ValueError ─────────────


@pytest.mark.asyncio
async def test_07_resume_validation_rejects_wrong_kind() -> None:
    coord, _, _ = _make_coordinator()
    task_id = "t-int-7"
    await coord.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={"user_prompt": "x", "requirement_file_id": "r", "template_file_id": "t"},
    )
    with pytest.raises(ValueError, match="kind"):
        await coord.resume_section_confirmation(
            task_id=task_id,
            graph_run_id=f"run-{task_id}",
            decision={"kind": "format_loss", "sections": [], "source": "user"},
        )


# ── 8) resume validation: sections 不是 list 抛 ValueError ────────────


@pytest.mark.asyncio
async def test_08_resume_validation_rejects_bad_sections() -> None:
    coord, _, _ = _make_coordinator()
    task_id = "t-int-8"
    await coord.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={"user_prompt": "x", "requirement_file_id": "r", "template_file_id": "t"},
    )
    with pytest.raises(ValueError, match="sections"):
        await coord.resume_section_confirmation(
            task_id=task_id,
            graph_run_id=f"run-{task_id}",
            decision={"kind": "section_confirmation", "sections": "not-list", "source": "user"},
        )


# ── 9) format_loss validation: decision 非法值抛 ValueError ────────────


@pytest.mark.asyncio
async def test_09_format_loss_validation_rejects_bad_decision() -> None:
    coord, _, _ = _make_coordinator()
    with pytest.raises(ValueError, match="decision"):
        await coord.resume_format_loss_interrupt(
            task_id="t-int-9",
            graph_run_id="run-t-int-9",
            decision={"kind": "format_loss", "decision": "sabotage", "source": "user"},
        )


# ── 10) 服务重启恢复:paused 中间态能 resume_thread 跑完 ───────────────


@pytest.mark.asyncio
async def test_10_resume_thread_after_simulated_restart() -> None:
    """第一次 ainvoke 触发 interrupt 后,模拟进程重启 → 新 coordinator 用同一
    checkpointer → resume_thread 应能从 checkpoint 恢复到当前状态。
    """
    ck = InMemorySaver()
    registry1 = GraphRegistry(name="restart_a")
    registry1.register(
        GRAPH_NAME_TEST_PLAN,
        GRAPH_VERSION_V2,
        build_compiled_v2_graph(checkpointer=ck, interrupt_enabled=True),
        schema_version=2,
    )
    runtime1 = GraphRuntimeService(registry=registry1)
    sink = InMemoryEventSink()
    ctx = _make_runtime_context(task_internal_id=100, sink=sink)
    coord_a = LangGraphRunCoordinator(
        registry=registry1,
        runtime=runtime1,
        checkpointer=ck,
        context_factory=lambda s: ctx,
    )

    task_id = "t-int-10"
    await coord_a.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={
            "user_prompt": "x",
            "requirement_file_id": "r",
            "template_file_id": "t",
            "graph_name": GRAPH_NAME_TEST_PLAN,
            "graph_version": GRAPH_VERSION_V2,
        },
    )

    # 模拟"重启":造一个全新 coordinator,但复用同一 checkpointer
    registry2 = GraphRegistry(name="restart_b")
    registry2.register(
        GRAPH_NAME_TEST_PLAN,
        GRAPH_VERSION_V2,
        build_compiled_v2_graph(checkpointer=ck, interrupt_enabled=True),
        schema_version=2,
    )
    from app.agent_runtime.graphs.test_plan.versions.v3 import build_test_plan_v3_graph

    registry2.register(
        GRAPH_NAME_TEST_PLAN,
        GRAPH_VERSION_V3,
        build_test_plan_v3_graph(checkpointer=ck, interrupt_enabled=True),
        schema_version=7,
    )
    runtime2 = GraphRuntimeService(registry=registry2)
    ctx2 = _make_runtime_context(task_internal_id=200, sink=sink)
    coord_b = LangGraphRunCoordinator(
        registry=registry2,
        runtime=runtime2,
        checkpointer=ck,
        context_factory=lambda s: ctx2,
    )

    # resume_section_confirmation 在新 coordinator 仍能命中老 thread
    out = await coord_b.resume_section_confirmation(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision={
            "kind": "section_confirmation",
            "sections": [{"id": "s", "accepted": True}],
            "source": "user",
        },
    )
    assert out.final_state.get("section_confirm_config")["source"] == "user"


# ── 11) 已 completed 不被 resume_thread 重新跑 ──────────────────────────


@pytest.mark.asyncio
async def test_11_resume_thread_noop_on_missing_checkpoint() -> None:
    """resume_thread 对不存在的 thread_id 抛 ValueError(防御性)。"""
    coord, _, _ = _make_coordinator()
    with pytest.raises(ValueError, match="no checkpoint"):
        await coord.resume_thread(task_id="never-existed", graph_run_id="x")


@pytest.mark.asyncio
async def test_11b_resume_thread_returns_outcome_for_existing_thread() -> None:
    """resume_thread 对已 paused 的 thread 调用,不抛错且返回 outcome。"""
    coord, _, _ = _make_coordinator()
    task_id = "t-int-11b"
    await coord.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload={"user_prompt": "x", "requirement_file_id": "r", "template_file_id": "t"},
    )
    rt = await coord.resume_thread(task_id=task_id, graph_run_id=f"run-{task_id}")
    # 当前还 paused → resume_thread 后仍然 paused(invoke None 不带 decision)
    assert rt is not None


# ── 12) thread 隔离:两个 task 互不干扰 ─────────────────────────────────


@pytest.mark.asyncio
async def test_12_thread_isolation_two_tasks() -> None:
    """不同 task_id(不同 thread_id)的 state 互不串扰。"""
    coord, _, _ = _make_coordinator()

    await coord.run_pre_confirm_interrupted(
        task_id="t-A",
        graph_run_id="run-A",
        initial_payload={"user_prompt": "A", "requirement_file_id": "rA", "template_file_id": "tA"},
    )
    await coord.run_pre_confirm_interrupted(
        task_id="t-B",
        graph_run_id="run-B",
        initial_payload={"user_prompt": "B", "requirement_file_id": "rB", "template_file_id": "tB"},
    )

    await coord.resume_section_confirmation(
        task_id="t-A",
        graph_run_id="run-A",
        decision={"kind": "section_confirmation", "sections": [{"id": "sA", "accepted": True}], "source": "user"},
    )
    # t-A 走完,t-B 仍 paused
    snap_b = coord._compiled_for_version(None).get_state(
        {"configurable": {"thread_id": "t-B"}}
    )
    assert snap_b is not None
    next_nodes = list(getattr(snap_b, "next", None) or ())
    assert next_nodes, "t-B should still be paused at interrupt"


# ── 13) confirm API 兼容(Legacy 接口不变) ─────────────────────────────


@pytest.mark.asyncio
async def test_13_legacy_sentinel_path_still_works() -> None:
    """Phase 2.1 sentinel 路径不因 interrupt_enabled 默认 False 被破坏。"""
    coord, _, _ = _make_coordinator(interrupt_enabled=False)
    out = await coord.run_pre_confirm(
        task_id="t-sentinel",
        graph_run_id="run-t-sentinel",
        initial_payload={
            "user_prompt": "x",
            "requirement_file_id": "r",
            "template_file_id": "t",
        },
    )
    # sentinel 路径停在 pause_for_legacy_confirm → END
    assert out.paused is True
    assert out.pause_marker == "need_user_confirm"


# ── 14) Feature flag default off + 显式 on 工作 ───────────────────────


def test_14_feature_flag_default_off_and_pytest_on(monkeypatch) -> None:
    """interrupt_v2_enabled 默认 False;PYTEST_CURRENT_TEST 强制 True。"""
    import os

    from app.agent_runtime.feature_flags import get_feature_flags

    monkeypatch.delenv("AGENT_RUNTIME_INTERRUPT_V2_ENABLED", raising=False)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/agent_runtime/test_plan_v2/test_v2_interrupts.py::test_14")
    flags = get_feature_flags()
    assert flags.interrupt_v2_enabled is True

    # 再造一个非 pytest 环境模拟
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("AGENT_RUNTIME_LANGGRAPH_ENABLED", "0")
    flags2 = get_feature_flags()
    assert flags2.interrupt_v2_enabled is False


# ── 15) ConfirmationTimeoutService 扫描 + CAS + decision 合成 ──────────


@pytest.mark.asyncio
async def test_15_timeout_service_marks_and_decides(monkeypatch) -> None:
    """mock session:插入 1 条 pending 超时记录,扫一次后 status=timeout 且
    response_json 含 source=timeout decision。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy import select

    # 用 sqlite 内存数据库快速 round-trip
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        from app.db.base import Base
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)

    # 插一条超时的 pending
    async def _seed() -> None:
        async with Session() as s:
            # 写一个 user / conversation / task placeholder 仅满足外键
            from app.models.user import User
            from app.models.conversation import Conversation
            from app.models.agent_task import AgentTask

            user = User(id=1, public_id="u1", username="u1", email="u@x", password_hash="x", display_name="u")
            conv = Conversation(id=1, public_id="c1", user_id=1, title="t")
            task = AgentTask(id=1, public_id="tk1", user_id=1, conversation_id=1, task_type="test_plan", title="t")
            s.add_all([user, conv, task])
            await s.flush()

            now = datetime.utcnow()
            long_ago = now - timedelta(seconds=DEFAULT_TIMEOUT_SECONDS + 60)
            hc = HumanConfirmation(
                id=1,
                public_id="hc1",
                user_id=1,
                conversation_id=1,
                task_id=1,
                confirmation_type="section_confirmation",
                status="pending",
                request_json={
                    "sections": [{"id": "s1", "title": "第1节", "accepted": True}]
                },
                requested_at=long_ago,
                created_at=long_ago,
                updated_at=long_ago,
            )
            s.add(hc)
            await s.commit()

    await _seed()

    resume_calls: List[Dict[str, Any]] = []

    async def _resume_cb(*, task_public_id: str, task_id: str, kind: str, decision: Dict[str, Any]) -> bool:
        resume_calls.append({"task_public_id": task_public_id, "kind": kind, "decision": decision})
        return True

    async def _session_factory():
        return Session()

    service = ConfirmationTimeoutService(
        session_factory=_session_factory,
        resume_callback=_resume_cb,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )
    result = await service.scan_and_drive_once()

    assert result.scanned >= 1
    assert result.timed_out >= 1
    assert len(resume_calls) == result.driven
    if resume_calls:
        call = resume_calls[0]
        assert call["decision"]["source"] == "timeout"
        assert call["decision"]["kind"] == "section_confirmation"
        assert isinstance(call["decision"]["sections"], list)

    # 二次扫描:CAS 之后,不应再 timed out
    result2 = await service.scan_and_drive_once()
    assert result2.scanned == 0 or result2.timed_out == 0
