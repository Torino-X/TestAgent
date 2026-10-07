"""Phase 2.8D — ``_emit_with_public_update`` helper 单元测试。

对应 docs/32 §12 测试矩阵 — 5 cases:
* TOOL_FINISHED 触发 chunk 流
* TOOL_FAILED 触发 chunk 流
* RETRYING 触发 chunk 流
* 其他事件类型(PLAN_CREATED 等)直传
* chunk_index / chunk_total 索引正确
"""

from __future__ import annotations

import pytest

from app.agent.enums import AgentEventType
from app.agent_runtime.events.sink import InMemoryEventSink
from app.agent_runtime.graphs.nodes_emit_helper import _emit_with_public_update


class _StubCtx:
    """Phase 2.8D:helper 只读 ctx.event_sink + graph_run_id。"""

    def __init__(self, sink: InMemoryEventSink) -> None:
        self.event_sink = sink
        self.graph_run_id = "run-test"


@pytest.mark.asyncio
class TestEmitWithPublicUpdate:
    async def test_tool_finished_embeds_chunk_in_payload(self) -> None:
        """TOOL_FINISHED → build_for_tool_result → chunk 嵌入 payload.public_execution_update。"""
        sink = InMemoryEventSink()
        ctx = _StubCtx(sink)
        await _emit_with_public_update(
            task_id="task-1",
            node_name="search_knowledge",
            event_type=AgentEventType.TOOL_FINISHED.value,
            payload={"data": {"hits": 5}},
            ctx=ctx,
            tool_name="KnowledgeSearchTool",
            success=True,
            elapsed_ms=120,
        )
        events = sink.collect()
        assert len(events) >= 1
        last = events[-1]
        assert last["event_type"] == "tool_finished"
        assert "public_execution_update" in last["payload"]
        # 关键 payload 字段透传
        assert last["payload"].get("data", {}).get("hits") == 5

    async def test_tool_failed_embeds_error_chunk(self) -> None:
        """TOOL_FAILED → chunk 包含 failed 标记 + error 数据。"""
        sink = InMemoryEventSink()
        ctx = _StubCtx(sink)
        await _emit_with_public_update(
            task_id="task-2",
            node_name="parse_requirement",
            event_type=AgentEventType.TOOL_FAILED.value,
            payload={"data": {}},
            ctx=ctx,
            tool_name="RequirementParserTool",
            success=False,
            error={"code": "REQUIREMENT_PARSE_FAILED", "message": "missing"},
            elapsed_ms=200,
        )
        events = sink.collect()
        assert events, "no events emitted"
        e = events[-1]
        assert e["event_type"] == "tool_failed"
        chunk = e["payload"].get("public_execution_update")
        assert chunk is not None

    async def test_retrying_embeds_retry_chunk(self) -> None:
        """RETRYING → build_for_retry → chunk 含 attempt/strategy。"""
        sink = InMemoryEventSink()
        ctx = _StubCtx(sink)
        await _emit_with_public_update(
            task_id="task-3",
            node_name="generate_test_plan",
            event_type=AgentEventType.RETRYING.value,
            payload={"retry": "context"},
            ctx=ctx,
            tool_name="TestPlanGeneratorTool",
            retry_strategy="degrade",
            retry_attempt=2,
            retry_last_error="schema_mismatch",
            retry_next_attempt_in_seconds=5.0,
        )
        events = sink.collect()
        assert events
        e = events[-1]
        assert e["event_type"] == "retrying"
        assert "public_execution_update" in e["payload"]

    async def test_non_tool_event_passes_through(self) -> None:
        """非 TOOL/RETRYING 事件(PLAN_CREATED 等)走直传,不带 chunk。"""
        sink = InMemoryEventSink()
        ctx = _StubCtx(sink)
        await _emit_with_public_update(
            task_id="task-4",
            node_name="initialize_task",
            event_type=AgentEventType.PLAN_CREATED.value,
            payload={"plan": {"steps": ["a", "b"]}},
            ctx=ctx,
        )
        events = sink.collect()
        assert len(events) == 1, "non-tool event should emit exactly once"
        e = events[0]
        assert e["event_type"] == "plan_created"
        assert "public_execution_update" not in e["payload"], (
            "non-tool event should not carry public_execution_update chunk"
        )

    async def test_chunked_payload_has_index_and_total(self) -> None:
        """多帧 chunk 流 → 每帧带 chunk_index/total 索引(2.5/2.6 协议)。"""
        sink = InMemoryEventSink()
        ctx = _StubCtx(sink)
        # 调用一次 TOOL_FINISHED(可能产生 1+ 帧)
        await _emit_with_public_update(
            task_id="task-5",
            node_name="export_word",
            event_type=AgentEventType.TOOL_FINISHED.value,
            payload={"data": {"size_kb": 250}},
            ctx=ctx,
            tool_name="WordExportTool",
            success=True,
            elapsed_ms=2000,
        )
        events = sink.collect()
        assert events
        total = len(events)
        for idx, e in enumerate(events):
            chunk = e["payload"].get("public_execution_update")
            assert chunk is not None, f"frame {idx} missing chunk"
        # 若 total > 1,每帧带 chunk_index/chunk_total
        if total > 1:
            for idx, e in enumerate(events):
                assert e["payload"].get("public_execution_update_chunk_index") == idx
                assert e["payload"].get("public_execution_update_chunk_total") == total
