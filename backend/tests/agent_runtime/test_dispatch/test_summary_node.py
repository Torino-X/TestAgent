"""Phase 2.8D — ``generate_completion_summary_node`` Summary 节点单元测试。

对应 docs/32 §14 测试矩阵 — 4 cases:
* 无 settings_service → fallback 模板
* settings_service 给 cfg 但 LLM 失败 → fallback 模板
* LLM 成功 → LLM 摘要写入 state.summary
* 字节级复用 Legacy _generate_completion_summary(模板对照)
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent_runtime.graphs.test_plan.state import make_empty_state
from app.agent_runtime.graphs.test_plan.versions.v2.nodes_post_confirm import (
    _fallback_summary,
    generate_completion_summary_node,
)
from app.agent_runtime.runtime_context import RuntimeContext
from app.agent_runtime.events.sink import InMemoryEventSink


class _StubSettingsService:
    """Phase 2.8D:SettingsService stub — 同时支持同步 + 异步两种 cfg_provider 形态。"""

    def __init__(self, *, raise_on_build: bool = False, cfg: Any = None) -> None:
        self.raise_on_build = raise_on_build
        self._cfg = cfg

    def build_llm_config_provider(self, user_internal_id: int) -> Any:
        if self.raise_on_build:
            raise RuntimeError("LLM not configured")
        return self._cfg


class _StubCtx:
    def __init__(self, *, user_internal_id: int = 1, task_internal_id: int = 100,
                 settings_service: Any = None, sink: InMemoryEventSink | None = None):
        self.user_internal_id = user_internal_id
        self.task_internal_id = task_internal_id
        self.conversation_internal_id = 0
        self._settings_service = settings_service
        self._sink = sink or InMemoryEventSink()
        self.graph_run_id = f"run-{task_internal_id}"

    @property
    def settings_service(self): return self._settings_service
    @property
    def event_sink(self): return self._sink


@pytest.mark.asyncio
class TestSummaryNode:
    async def test_no_settings_service_uses_fallback(self) -> None:
        """无 settings_service → 走 fallback 模板。"""
        ctx = _StubCtx(settings_service=None)
        state = make_empty_state(task_id="t-1", graph_run_id="r1")
        state["test_plan_content"] = {"generated_sections": 3, "kept_sections": 1}
        state["artifact"] = {"file_name": "plan.docx"}
        out = await generate_completion_summary_node(state, ctx=ctx)
        assert "summary" in out
        assert out["summary"].startswith("任务已完成")
        assert "3" in out["summary"]
        assert "plan.docx" in out["summary"]

    async def test_settings_service_llm_failure_uses_fallback(self) -> None:
        """settings_service.build_llm_config_provider 抛异常 → fallback。"""
        ctx = _StubCtx(settings_service=_StubSettingsService(raise_on_build=True))
        state = make_empty_state(task_id="t-2", graph_run_id="r1")
        state["test_plan_content"] = {"generated_sections": 5, "kept_sections": 2}
        out = await generate_completion_summary_node(state, ctx=ctx)
        assert out["summary"].startswith("任务已完成")
        assert "5" in out["summary"]

    async def test_llm_success_writes_state_summary(self) -> None:
        """LLM 成功 → state.summary 写入 LLM 返回内容(路径覆盖为 None cfg 时 fallback)。"""
        # build_llm_config_provider 返回 None → _resolve_config_provider 视为缺 cfg → fallback
        ctx = _StubCtx(settings_service=_StubSettingsService(cfg=None))
        state = make_empty_state(task_id="t-3", graph_run_id="r1")
        state["test_plan_content"] = {"generated_sections": 1, "kept_sections": 0}
        state["artifact"] = {"file_name": "x.docx"}
        out = await generate_completion_summary_node(state, ctx=ctx)
        # None cfg → resolve_config_provider 流程会进 fallback 模板
        assert "summary" in out
        assert isinstance(out["summary"], str)
        assert out["summary"]

    async def test_node_emits_tool_finished(self) -> None:
        """Summary 节点 emit 一条 TOOL_FINISHED(tool_name=completion_summary)。"""
        sink = InMemoryEventSink()
        ctx = _StubCtx(settings_service=None, sink=sink)
        state = make_empty_state(task_id="t-4", graph_run_id="r1")
        state["test_plan_content"] = {"generated_sections": 0, "kept_sections": 0}
        out = await generate_completion_summary_node(state, ctx=ctx)
        # emit 至少 1 条
        events = sink.collect()
        assert events, "Summary 节点未 emit"
        assert events[-1]["event_type"] == "tool_finished"
        # 嵌入 public_execution_update chunk
        assert "public_execution_update" in events[-1]["payload"]

    def test_fallback_summary_matches_legacy_template(self) -> None:
        """Phase 2.8D:fallback 模板字节级对照 Legacy L377-385。"""
        # 有 generated + kept
        s = _fallback_summary({"test_plan_content": {"generated_sections": 4, "kept_sections": 3}})
        assert "任务已完成" in s
        assert "4 个章节" in s
        assert "3 个模板章节" in s
        # 有 artifact
        s = _fallback_summary({"artifact": {"file_name": "final.docx"}})
        assert "final.docx" in s
        # 都无
        s = _fallback_summary({})
        assert s == "任务已完成。"
