"""CE-04 最终复验 —— 真实 Cross-worker Resume 测试。

证明真正的 Cross-worker Resume:

  Worker A 用 task_public_id 启动任务 → 执行到 section_confirmation_interrupt
  → checkpoint 持久化 → Worker A 销毁 → Worker B 接管**同一 thread_id**
  → 从 checkpointer 读 checkpoint identity → 重建 RuntimeContext(新实例)
  → 用 Command(resume=...) 恢复 → 任务继续到 completed。

关键断言(能力范围内):
  * resume 使用与启动相同的 thread_id(task_public_id);
  * 两个 RuntimeContext 实例不同(object identity);
  * checkpoint 在 Worker A 时已写(snapshot 含 task_id/graph_run_id/task_internal_id);
  * Worker B resume 成功完成,最终 artifact 是新 Snapshot(public_id 非空)。

checkpointer 策略:
  * 持久化目标与生产一致:``AGENT_RUNTIME_POSTGRES_URL``(生产 AsyncPostgresSaver
    实际使用的共享存储)或 ``POSTGRES_TEST_URL``。两者任一可用时用真实
    ``AsyncPostgresSaver`` 做真实 checkpoint 落库,Worker A / Worker B 各用
    **独立连接池**(真正跨连接恢复),测试结束删除测试 thread(checkpoint 零残留);
  * 两者都不可用时用 ``InMemorySaver``,但跨**两个独立编译 graph + 两个
    RuntimeContext 实例**恢复,证明 checkpoint 身份不依赖 Worker A 实例。

约束:不修改 v2_frozen / v3 拓扑 / TestPlanGraphState;无外部 DB 则 InMemory 兜底。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from langgraph.checkpoint.memory import InMemorySaver

from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter

# 阻止 pytest 把同名测试辅助类当作测试收集(仅 import 需要)
TestAgentToolAdapter.__test__ = False  # type: ignore[attr-defined]

from app.agent_runtime.events.sink import InMemoryEventSink
from app.agent_runtime.graph_registry import GraphRegistry
from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V3,
)
from app.agent_runtime.graphs.test_plan.versions.v3 import build_test_plan_v3_graph
from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
from app.agent_runtime.runtime_context import RuntimeContext


def _load_persistent_pg_url() -> str:
    """持久化 Checkpointer URL:与生产一致(AGENT_RUNTIME_POSTGRES_URL 优先)。"""
    for var in ("POSTGRES_TEST_URL", "AGENT_RUNTIME_POSTGRES_URL"):
        val = os.getenv(var, "").strip()
        if val:
            return val
    env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("AGENT_RUNTIME_POSTGRES_URL="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


POSTGRES_TEST_URL = os.getenv("POSTGRES_TEST_URL", "")
_PERSISTENT_PG_URL = _load_persistent_pg_url()
_HAS_PERSISTENT_PG = bool(_PERSISTENT_PG_URL.strip())

_has_persistent_ckpt = pytest.mark.skipif(
    not _HAS_PERSISTENT_PG,
    reason=(
        "POSTGRES_TEST_URL / AGENT_RUNTIME_POSTGRES_URL 均未设置;"
        "持久化 cross-worker 测试降级为 InMemory 兜底"
    ),
)


# ── Tool stub ────────────────────────────────────────────────────────────────


class _StubToolExecutor:
    """所有 9 个工具都返回成功信封;关键工具返回满足下游校验的结构。

    * TestPlanGeneratorTool → section_package(generated_sections 非空正文),
      使 Phase 2.9A.11 内容校验通过;
    * ResultReviewTool → level=passed(→ prepare_export,不触发 regen/repair);
    * WordExportTool → canonical artifact(public_id + storage_path + file_name),
      使 export_word_node 的 normalize_export_artifact 通过;
    * DocxFormatCheckTool → level=passed(→ finalize_task,不触发 format_loss
      interrupt),保证 resume 后任务一路走到 completed。
    """

    def __init__(self) -> None:
        self.calls: List[str] = []
        self.export_count: int = 0

    async def run(
        self, tool_name: str, inputs: Dict[str, Any], context: Any,
        retry_context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        self.calls.append(tool_name)
        if tool_name == "TestPlanGeneratorTool":
            return {
                "success": True, "tool_name": tool_name, "task_id": "stub",
                "data": {
                    "total_word_count": 120,
                    "tables_generated": 0,
                    "section_package": {
                        "generated_sections": [
                            {
                                "section_id": "gs_1",
                                "title": "测试方案章节1",
                                "content": "根据需求文档生成的测试用例说明正文。" * 10,
                            }
                        ],
                        "keep_sections": [],
                        "manual_sections": [],
                    },
                },
                "summary": "", "warnings": [], "error": None,
                "duration_ms": 1, "attempt": 1,
            }
        if tool_name == "WordExportTool":
            self.export_count += 1
            pid = f"art_cross_worker_{self.export_count}"
            return {
                "success": True, "tool_name": tool_name, "task_id": "stub",
                "data": {
                    "public_id": pid,
                    "artifact_id": pid,
                    "artifact_type": "test_plan_word",
                    "file_name": "测试方案.docx",
                    "file_ext": "docx",
                    "mime_type": (
                        "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"
                    ),
                    "file_size": 2048,
                    "storage_path": "/tmp/stub.docx",
                },
                "summary": "", "warnings": [], "error": None,
                "duration_ms": 1, "attempt": 1,
            }
        if tool_name == "DocxFormatCheckTool":
            return {
                "success": True, "tool_name": tool_name, "task_id": "stub",
                "data": {"level": "passed", "status": "passed", "losses": []},
                "summary": "", "warnings": [], "error": None,
                "duration_ms": 1, "attempt": 1,
            }
        if tool_name == "ResultReviewTool":
            return {
                "success": True, "tool_name": tool_name, "task_id": "stub",
                "data": {
                    "level": "passed", "issues": [], "block_issues": [],
                    "suggestions": [],
                },
                "summary": "", "warnings": [], "error": None,
                "duration_ms": 1, "attempt": 1,
            }
        # RequirementParserTool / TemplateParserTool / KnowledgeSearchTool /
        # SectionSuggestionTool — 均返回成功空 data(测试 stub 模式)。
        return {
            "success": True, "tool_name": tool_name, "task_id": "stub",
            "data": {}, "summary": "", "warnings": [], "error": None,
            "duration_ms": 1, "attempt": 1,
        }


# ── RuntimeContext helpers ──────────────────────────────────────────────────


class _NullCM:
    async def __aenter__(self_):  # noqa: N805 - mirror existing test fixture
        return None

    async def __aexit__(self_, *a):  # noqa: N805
        return False


class _NC:
    def is_cancelled(self, x):  # noqa: N805
        return False


def _make_runtime_context(
    *,
    task_internal_id: int,
    sink: InMemoryEventSink,
    executor: _StubToolExecutor,
) -> RuntimeContext:
    adapter = TestAgentToolAdapter(
        tool_executor=executor,
        event_sink=sink,
        session_factory=lambda: _NullCM(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )
    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=task_internal_id,
        conversation_internal_id=10,
        session_factory=lambda: _NullCM(),
        settings_service=None,
        event_sink=sink,
        cancellation_service=_NC(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=adapter,
    )


def _make_worker(
    *,
    checkpointer: Any,
    worker_tag: str,
    sink: InMemoryEventSink,
    executor: _StubToolExecutor,
) -> Tuple[LangGraphRunCoordinator, Any, List[RuntimeContext]]:
    """构造一个独立 Worker(独立 registry + runtime + context_factory)。

    每次调用都会:
      * 重新 compile v3 graph(绑定传入的 checkpointer);
      * 新建 context_factory → 每次注入全新的 RuntimeContext 实例;
      * ``ctxs`` 记录所有被创建的 RuntimeContext,用于断言跨 Worker 实例不同。

    这正是生产形态:每个 Worker 是独立进程/连接,只有 checkpointer 是共享的
    (InMemorySaver 单实例或 Postgres 真实持久化)。
    """
    registry = GraphRegistry(name=f"ce04_{worker_tag}")
    compiled = build_test_plan_v3_graph(
        checkpointer=checkpointer, interrupt_enabled=True
    )
    registry.register(
        GRAPH_NAME_TEST_PLAN,
        GRAPH_VERSION_V3,
        compiled,
        schema_version=7,
    )
    runtime = GraphRuntimeService(registry=registry)
    ctxs: List[RuntimeContext] = []

    def context_factory(state: Dict[str, Any]) -> RuntimeContext:
        # 每次注入都用全新实例 —— 不从任何共享缓存复用。
        ctx = _make_runtime_context(
            task_internal_id=int(state.get("task_internal_id") or 0),
            sink=sink,
            executor=executor,
        )
        ctxs.append(ctx)
        return ctx

    coord = LangGraphRunCoordinator(
        registry=registry,
        runtime=runtime,
        checkpointer=checkpointer,
        context_factory=context_factory,
    )
    return coord, compiled, ctxs


def _section_resume_decision() -> Dict[str, Any]:
    return {
        "kind": "section_confirmation",
        "sections": [
            {"section_id": "s1", "title": "第1节", "accepted": True},
            {"section_id": "s2", "title": "第2节", "accepted": True},
        ],
        "source": "user",
    }


def _initial_payload() -> Dict[str, Any]:
    return {
        "user_prompt": "生成测试方案",
        "requirement_file_id": "req_1",
        "template_file_id": "tpl_1",
        "task_internal_id": 701,
        "conversation_internal_id": 702,
        "user_internal_id": 703,
    }


# ════════════════════════════════════════════════════════════════════════════
# 核心:InMemorySaver 共享单实例,跨两个 RuntimeContext 实例恢复
# ════════════════════════════════════════════════════════════════════════════


async def test_cross_worker_resume_new_snapshot() -> None:
    """Worker A 中断 → checkpoint 落库 → Worker A 销毁 → Worker B 接管同一
    thread_id → 重建 RuntimeContext → Command(resume=...) → 任务完成。

    用 InMemorySaver(单实例)但跨两个**独立编译 graph + 独立 context_factory**,
    证明 checkpoint 身份不依赖 Worker A 的 RuntimeContext 实例。
    """
    ck = InMemorySaver()
    task_id = "task_pub_1"  # task_public_id,即 thread_id(代码库约定)

    # ── Worker A:启动任务到 interrupt ────────────────────────────────
    coord_a, compiled_a, ctxs_a = _make_worker(
        checkpointer=ck, worker_tag="A",
        sink=InMemoryEventSink(), executor=_StubToolExecutor(),
    )
    o1 = await coord_a.run_pre_confirm_interrupted(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        initial_payload=_initial_payload(),
    )
    # 走到 section_confirmation_interrupt 挂起
    assert o1.paused is True
    assert o1.pause_marker == "section_confirmation_interrupt"
    assert o1.pending_interrupt in (None, "section_confirmation")

    # ── checkpoint 在 Worker A 时已写 ────────────────────────────────
    snap_a = await compiled_a.aget_state(
        {"configurable": {"thread_id": task_id}}
    )
    assert snap_a is not None
    values_a = dict(snap_a.values or {})
    # checkpoint 保存了 task_public_id(=task_id)与 graph_run_id、DB identity
    assert values_a.get("task_id") == task_id
    assert values_a.get("graph_run_id") == f"run-{task_id}"
    assert values_a.get("task_internal_id") == 701
    # thread_id(configurable) == task_public_id
    assert snap_a.config["configurable"]["thread_id"] == task_id
    # pending interrupt 在 tasks 上
    assert list(getattr(snap_a, "tasks", None) or ()), (
        "Worker A 的 checkpoint 必须含 pending interrupt task"
    )

    # ── 销毁 Worker A(丢弃引用;真实场景为进程退出/连接池关闭) ───────
    del coord_a
    del compiled_a

    # ── Worker B:全新实例,同一 thread_id 接管 ───────────────────────
    coord_b, compiled_b, ctxs_b = _make_worker(
        checkpointer=ck, worker_tag="B",
        sink=InMemoryEventSink(), executor=_StubToolExecutor(),
    )
    o2 = await coord_b.resume_section_confirmation(
        task_id=task_id,
        graph_run_id=f"run-{task_id}",
        decision=_section_resume_decision(),
    )
    # Worker B 用 Command(resume=...) 恢复并跑完
    assert o2.paused is False
    assert o2.completed is True
    assert o2.task_status == "completed"
    assert o2.final_state.get("section_confirm_config", {}).get("source") == "user"

    # ── resume 用同一 thread_id(task_public_id):最终 checkpoint 仍在该 thread
    final_snap = await compiled_b.aget_state(
        {"configurable": {"thread_id": task_id}}
    )
    final_values = dict(final_snap.values or {})
    assert final_values.get("task_id") == task_id
    assert final_values.get("task_internal_id") == 701
    assert final_values.get("graph_run_id") == f"run-{task_id}"
    assert final_values.get("task_status") == "completed"
    # 新 Snapshot(artifact public_id)在 Worker B resume 时创建,非空
    artifact = final_values.get("artifact") or {}
    assert artifact.get("public_id"), "Worker B 必须创建新的 artifact Snapshot"

    # ── 两个 RuntimeContext 实例不同 ────────────────────────────────
    assert len(ctxs_a) == 1 and len(ctxs_b) == 1, (
        f"预期 A/B 各注入一次 RuntimeContext;got A={len(ctxs_a)} B={len(ctxs_b)}"
    )
    assert ctxs_a[0] is not ctxs_b[0], (
        "Worker B 必须重建全新的 RuntimeContext,不能复用 Worker A 实例"
    )
    # Worker B 的 RuntimeContext 从 checkpoint identity 重建(同一 DB identity)
    assert ctxs_b[0].task_internal_id == 701
    assert ctxs_b[0].task_internal_id == ctxs_a[0].task_internal_id


# ════════════════════════════════════════════════════════════════════════════
# 真实 Postgres:两个独立 AsyncPostgresSaver 连接,证明 checkpoint 真持久化
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(
    not _HAS_PERSISTENT_PG,
    reason="POSTGRES_TEST_URL / AGENT_RUNTIME_POSTGRES_URL 未设置;持久化测试降级",
)
async def test_cross_worker_resume_persistent_postgres() -> None:
    """Worker A 用 AsyncPostgresSaver 写 checkpoint → 关闭连接池(进程销毁)
    → Worker B 用全新 AsyncPostgresSaver 从 Postgres 读回同一 thread_id 恢复。

    这是最真实的 Cross-worker Resume:checkpoint 跨两个独立 saver 实例落库。
    使用与生产一致的持久化目标(AGENT_RUNTIME_POSTGRES_URL);线程唯一 + 结束清理。
    """
    from app.agent_runtime.persistence.postgres_checkpointer import (
        aclose_postgres_checkpointer,
        build_postgres_checkpointer,
    )

    url = _PERSISTENT_PG_URL
    task_id = f"ce04_pg_cross_{uuid.uuid4().hex[:8]}"  # 唯一,避免污染共享库

    # ── Worker A:独立连接池,启动到 interrupt ────────────────────────
    executor_a = _StubToolExecutor()
    cp_a = await build_postgres_checkpointer(url, setup=True)
    assert cp_a is not None, (
        "POSTGRES_TEST_URL / AGENT_RUNTIME_POSTGRES_URL 已设置但 Postgres 不可用,"
        "应 fail(不伪造)"
    )
    try:
        coord_a, compiled_a, ctxs_a = _make_worker(
            checkpointer=cp_a, worker_tag="PG-A",
            sink=InMemoryEventSink(), executor=executor_a,
        )
        o1 = await coord_a.run_pre_confirm_interrupted(
            task_id=task_id,
            graph_run_id=f"run-{task_id}",
            initial_payload=_initial_payload(),
        )
        assert o1.paused is True
        assert o1.pause_marker == "section_confirmation_interrupt"

        snap_a = await compiled_a.aget_state(
            {"configurable": {"thread_id": task_id}}
        )
        values_a = dict(snap_a.values or {})
        assert values_a.get("task_id") == task_id
        assert values_a.get("task_internal_id") == 701
        assert snap_a.config["configurable"]["thread_id"] == task_id
        assert list(getattr(snap_a, "tasks", None) or ()), (
            "Worker A 的 Postgres checkpoint 必须含 pending interrupt task"
        )
    finally:
        # 模拟 Worker A 销毁:关闭连接池,checkpoint 已落 Postgres
        await aclose_postgres_checkpointer(cp_a)

    # ── Worker B:全新连接池,从 Postgres 恢复同一 thread_id ──────────
    executor_b = _StubToolExecutor()
    cp_b = await build_postgres_checkpointer(url, setup=False)
    assert cp_b is not None
    try:
        coord_b, compiled_b, ctxs_b = _make_worker(
            checkpointer=cp_b, worker_tag="PG-B",
            sink=InMemoryEventSink(), executor=executor_b,
        )
        o2 = await coord_b.resume_section_confirmation(
            task_id=task_id,
            graph_run_id=f"run-{task_id}",
            decision=_section_resume_decision(),
        )
        assert o2.paused is False
        assert o2.completed is True
        assert o2.task_status == "completed"

        final_snap = await compiled_b.aget_state(
            {"configurable": {"thread_id": task_id}}
        )
        final_values = dict(final_snap.values or {})
        # checkpoint identity 跨独立 saver 持久化:DB identity 与 thread 不变
        assert final_values.get("task_id") == task_id
        assert final_values.get("task_internal_id") == 701
        assert final_values.get("task_status") == "completed"
        # thread_id 全程保持 = task_public_id
        assert final_snap.config["configurable"]["thread_id"] == task_id

        # 两个 RuntimeContext 实例不同(跨连接重建)
        assert len(ctxs_a) == 1 and len(ctxs_b) == 1
        assert ctxs_a[0] is not ctxs_b[0]
        assert ctxs_b[0].task_internal_id == 701
        assert ctxs_b[0].task_internal_id == ctxs_a[0].task_internal_id

        # 新 Snapshot(artifact public_id)在 Worker B resume 时创建,非空
        artifact = final_values.get("artifact") or {}
        assert artifact.get("public_id"), "Worker B 必须创建新的 artifact Snapshot"
        # 不产生重复 AgentEvent(export 只发生一次)
        assert executor_b.export_count == 1, (
            f"Worker B resume 不得重复导出 artifact;export_count={executor_b.export_count}"
        )
    finally:
        await aclose_postgres_checkpointer(cp_b)
        # 清理测试 thread,保证共享库零残留
        cp_cleanup = await build_postgres_checkpointer(url, setup=False)
        if cp_cleanup is not None:
            try:
                await cp_cleanup.adelete_thread(task_id)
            finally:
                await aclose_postgres_checkpointer(cp_cleanup)
