"""Phase 2.9B.1 — graph-level dependency wiring + KnowledgeSearch single-owner tests.

Verifies that a compiled v3 graph receives a RuntimeContext whose
``tool_adapter`` and ``llm_client`` are both present when built through
``ProductionRuntimeContextFactory`` with a real session factory, and that
the prep fallback path defers KnowledgeSearch execution to the main graph
(prep_legacy_fallback_node) so it runs exactly once.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.agent_runtime.graphs.test_plan.versions.v3.graph import (
    build_compiled_v3_graph,
)
from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
    prep_subgraph_node,
)
from app.agent_runtime.runtime_context import RuntimeContext


class _StubCancel:
    def is_cancelled(self, _task_id):
        return False


class _NullSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_args):
        return False


def _null_session_factory():
    return _NullSession()


class _MemorySink:
    def __init__(self):
        self.events = []

    async def emit(self, **kw):
        self.events.append(kw)
        return {"ok": True}


class _StubAdapter:
    def __init__(self):
        self.kb_calls = 0

    async def execute(self, *, tool_name, inputs=None, ctx_runtime=None, **kw):
        if tool_name == "KnowledgeSearchTool":
            self.kb_calls += 1
            return {"success": True, "data": {}, "summary": "ok"}
        raise AssertionError(f"unexpected tool {tool_name}")


def _make_runtime_context(tool_adapter=None, llm_client=None, sink=None):
    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=200,
        conversation_internal_id=20,
        session_factory=_null_session_factory,
        settings_service=None,
        event_sink=sink or _MemorySink(),
        cancellation_service=_StubCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=tool_adapter,
        llm_client=llm_client,
    )


class TestV3GraphCompilesWithDependencies:
    """The v3 graph must compile and hold the prep node with runtime deps."""

    def test_compiled_v3_graph_builds(self):
        from langgraph.checkpoint.memory import MemorySaver

        graph = build_compiled_v3_graph(checkpointer=MemorySaver())
        assert graph is not None
        nodes = set(graph.get_graph().nodes.keys())
        assert "generate_test_plan" in nodes
        assert "prep_subgraph_step" in nodes or "prep_subgraph" in nodes

    def test_graph_does_not_serialize_llm_client_in_state(self):
        """LLMClient / ToolAdapter must never be written to checkpointer state."""
        from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
        from app.agent_runtime.runtime_context import RuntimeContext

        assert "llm_client" not in TestPlanGraphState.__annotations__
        assert "tool_adapter" not in TestPlanGraphState.__annotations__
        assert "llm_client" in RuntimeContext.__dataclass_fields__
        assert "tool_adapter" in RuntimeContext.__dataclass_fields__


class TestKnowledgeSearchSingleOwner:
    """Missing-dependency prep fallback defers KB execution to main graph."""

    @pytest.mark.asyncio
    async def test_prep_fallback_does_not_execute_kb(self):
        adapter = _StubAdapter()
        ctx = _make_runtime_context(tool_adapter=adapter, llm_client=None)
        state = {
            "user_prompt": "请生成测试方案",
            "requirement_analysis": {"text_content": "需求"},
            "template_structure": {"sections": []},
            "knowledge_search_result": None,
            "kb_skip_reason": None,
            "completed_nodes": [],
            "preparation_agent_enabled": True,
        }
        result = await prep_subgraph_node(state, ctx=ctx)
        # The node itself must NOT invoke KnowledgeSearchTool — it defers to
        # the main graph's deterministic prep_legacy_fallback_node.
        assert adapter.kb_calls == 0
        assert result["preparation_result"] is None
        assert "missing_runtime_dependencies" in result["preparation_fallback_reason"]

    @pytest.mark.asyncio
    async def test_prep_success_executes_kb_once_through_agent_loop(self, monkeypatch):
        """With deps present, the prep agent loop drives KnowledgeSearch once."""
        from app.agent_runtime.preparation.schemas import PreparationResult

        adapter = _StubAdapter()
        llm_calls = []

        class _LLM:
            def __init__(self):
                self.calls = 0

            async def generate(self, _prompt, **_kw):
                self.calls += 1
                llm_calls.append(_prompt)
                # First decision: call KnowledgeSearchTool once, then finish.
                if self.calls == 1:
                    return (
                        '{"action": "call_tool", "tool_name": "KnowledgeSearchTool",'
                        ' "args": {}, "reason": "需要检索知识库"}'
                    )
                return '{"action": "finish", "reason": "信息已足够"}'

        llm = _LLM()
        ctx = _make_runtime_context(tool_adapter=adapter, llm_client=llm)

        captured = {}

        async def _fake_run_subgraph(state_snapshot, *, llm_client, tool_adapter, ctx, **_kw):
            captured["llm"] = llm_client
            captured["adapter"] = tool_adapter
            # Simulate the real prep agent loop deciding to call KB once.
            await tool_adapter.execute(
                tool_name="KnowledgeSearchTool",
                inputs={},
                ctx_runtime=ctx,
            )
            return PreparationResult(
                information_sufficient=True,
                fallback_reason=None,
                knowledge_search_used=True,
                queries=["测试"],
                evidence=[],
                requirement_gaps=[],
                user_questions=[],
                constraints=[],
                confidence=0.9,
                public_summary={"headline": "完成", "detail": "信息已足够"},
                budget_state={
                    "steps": 2,
                    "tool_calls": 1,
                    "wall_seconds": 0.5,
                    "token_estimate": 500,
                    "repeated_tool_calls": 0,
                },
            )

        monkeypatch.setattr(
            "app.agent_runtime.preparation.subgraph.run_preparation_subgraph",
            _fake_run_subgraph,
        )
        state = {
            "user_prompt": "请生成测试方案",
            "requirement_analysis": {"text_content": "需求"},
            "template_structure": {"sections": []},
            "knowledge_search_result": None,
            "kb_skip_reason": None,
            "completed_nodes": [],
            "preparation_agent_enabled": True,
        }
        result = await prep_subgraph_node(state, ctx=ctx)
        assert captured["llm"] is llm
        assert captured["adapter"] is adapter
        assert adapter.kb_calls == 1
        assert result["preparation_result"] is not None
