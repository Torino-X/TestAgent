"""Phase 2.9B.1 — dynamic subgraph dependency injection regression tests.

Covers the production defect where ``prep_subgraph_node`` fell back because
``RuntimeContext`` (a frozen + slots dataclass) had no ``llm_client`` field,
so ``getattr(ctx, "llm_client", None)`` was always None.  These tests assert:

* ``RuntimeContext`` / ``RuntimeContextFactory`` expose ``llm_client``;
* ``ProductionRuntimeContextFactory`` builds a real LLMClient (or returns
  None when the provider cannot be resolved, without crashing);
* ``prep_subgraph_node`` receives a non-None ``llm_client`` when the
  context carries one;
* missing dependencies produce a structured fallback (single owner of the
  deterministic KnowledgeSearch execution), never a fabricated success;
* the narrative emitter chain reaches the event sink.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
    prep_subgraph_node,
)
from app.agent_runtime.feature_flags import AgentRuntimeFeatureFlags
from app.agent_runtime.runtime_context import RuntimeContext, RuntimeContextFactory


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
    """Minimal tool adapter stub; tracks KnowledgeSearchTool invocations."""

    def __init__(self):
        self.kb_calls = 0

    async def execute(self, *, tool_name, inputs=None, ctx_runtime=None, **kw):
        if tool_name == "KnowledgeSearchTool":
            self.kb_calls += 1
            return {"success": True, "data": {}, "summary": "ok"}
        raise AssertionError(f"unexpected tool {tool_name}")


class _StubLLM:
    """Minimal decision LLM stub for the preparation loop."""

    def __init__(self):
        self.calls = 0

    async def generate(self, _prompt, **_kw):
        self.calls += 1
        return '{"action": "finish", "reason": "信息已足够"}'


def _make_runtime_context(*, tool_adapter=None, llm_client=None, sink=None):
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


def _base_state():
    return {
        "user_prompt": "请生成测试方案",
        "requirement_analysis": {"text_content": "需求"},
        "template_structure": {"sections": []},
        "knowledge_search_result": None,
        "kb_skip_reason": None,
        "completed_nodes": [],
        "preparation_agent_enabled": True,
    }


class TestRuntimeContextCarriesLlmClient:
    """The frozen+slots RuntimeContext must expose llm_client as a real field."""

    def test_runtime_context_has_llm_client_field(self):
        ctx = _make_runtime_context(llm_client=_StubLLM())
        assert ctx.llm_client is not None

    def test_runtime_context_defaults_to_none(self):
        ctx = _make_runtime_context()
        assert ctx.llm_client is None

    def test_runtime_context_factory_passes_llm_client(self):
        factory = RuntimeContextFactory(
            user_internal_id=1,
            task_internal_id=200,
            conversation_internal_id=20,
            session_factory=_null_session_factory,
            settings_service=None,
            event_sink=_MemorySink(),
            cancellation_service=_StubCancel(),
            tool_adapter=_StubAdapter(),
            llm_client=_StubLLM(),
        )
        ctx = factory.build()
        assert ctx.llm_client is not None
        assert ctx.tool_adapter is not None


class TestPrepSubgraphNodeDependencies:
    """prep_subgraph_node must enter the dynamic subgraph when deps are present."""

    @pytest.mark.asyncio
    async def test_prep_with_complete_dependencies_enters_subgraph(self, monkeypatch):
        adapter = _StubAdapter()
        llm = _StubLLM()
        sink = _MemorySink()
        ctx = _make_runtime_context(
            tool_adapter=adapter, llm_client=llm, sink=sink,
        )

        captured = {}

        async def _fake_run_subgraph(state_snapshot, *, llm_client, tool_adapter, ctx, **_kw):
            captured["llm_client"] = llm_client
            captured["tool_adapter"] = tool_adapter

            class _BudgetNS:
                def model_dump(self):
                    return {"steps": 1, "tool_calls": 0}

            class _ResultNS:
                information_sufficient = True
                fallback_reason = None
                evidence = []
                budget_state = _BudgetNS()

                def model_dump(self):
                    return {
                        "information_sufficient": True,
                        "fallback_reason": None,
                        "evidence": [],
                        "budget_state": {"steps": 1, "tool_calls": 0},
                    }

            return _ResultNS()

        monkeypatch.setattr(
            "app.agent_runtime.preparation.subgraph.run_preparation_subgraph",
            _fake_run_subgraph,
        )
        result = await prep_subgraph_node(_base_state(), ctx=ctx)
        # llm_client/tool_adapter must reach the subgraph (not None).
        assert captured["llm_client"] is llm
        assert captured["tool_adapter"] is adapter
        # No fallback marker; real subgraph result surfaced.
        assert "preparation_fallback_reason" not in result
        assert result["preparation_result"] is not None

    @pytest.mark.asyncio
    async def test_prep_missing_llm_client_returns_structured_fallback(self, monkeypatch):
        adapter = _StubAdapter()
        sink = _MemorySink()
        ctx = _make_runtime_context(tool_adapter=adapter, llm_client=None, sink=sink)

        result = await prep_subgraph_node(_base_state(), ctx=ctx)
        # Structured fallback reason, no fabricated success.
        assert result["preparation_result"] is None
        assert result["preparation_fallback_reason"].startswith(
            "missing_runtime_dependencies:"
        )
        assert "llm_client" in result["preparation_fallback_reason"]
        # Fallback itself is NOT executed inside the node (single-owner fix):
        # the main graph's prep_legacy_fallback_node owns deterministic KB.
        assert "knowledge_search_result" not in result

    @pytest.mark.asyncio
    async def test_prep_missing_both_dependencies_lists_both(self, monkeypatch):
        ctx = _make_runtime_context(tool_adapter=None, llm_client=None)
        result = await prep_subgraph_node(_base_state(), ctx=ctx)
        reason = result["preparation_fallback_reason"]
        assert "tool_adapter" in reason
        assert "llm_client" in reason


class TestNarrativeEmitterReachesSink:
    """Phase 2.9B decision/observation events must persist through the sink."""

    @pytest.mark.asyncio
    async def test_preparation_emitter_writes_decision_and_observation(self, monkeypatch):
        from app.agent_runtime._shared.public_narrative import AgentObservation
        from app.agent_runtime.preparation.event_emitter import PreparationEventEmitter

        sink = _MemorySink()
        ctx = _make_runtime_context(
            tool_adapter=_StubAdapter(), llm_client=_StubLLM(), sink=sink,
        )
        emitter = PreparationEventEmitter(ctx)
        # Force the 9B narrative flag on so events actually emit.
        monkeypatch.setattr(
            "app.agent_runtime.preparation.event_emitter.get_feature_flags",
            lambda: SimpleNamespace(
                phase29b_narrative_enabled_for=lambda _name: True,
            ),
        )
        # Phase 2.9B.2: emit 是 async 并直接 await event_sink.emit —
        # 事件在方法返回前已写入 sink,无需 sleep / 驱动 loop。
        await emitter.emit_decision_update(
            decision_id="t:preparation:0",
            step_index=0,
            action="call_tool",
            tool_name="KnowledgeSearchTool",
            public_update={
                "headline": "决定检索知识库",
                "summary": "模板字段不明确，需要检索背景知识",
                "impact": "影响后续生成",
                "next_action": "调用 KnowledgeSearchTool",
            },
        )
        await emitter.emit_observation_update(
            decision_id="t:preparation:1",
            step_index=1,
            observation=AgentObservation(
                tool_name="KnowledgeSearchTool",
                success=True,
                error_code=None,
                result_excerpt="检索到 2 条知识",
            ),
            public_update={
                "headline": "知识库检索完成",
                "summary": "检索到 2 条相关知识",
                "impact": "可继续生成章节建议",
                "next_action": "生成章节建议",
            },
        )
        event_types = {e.get("event_type") for e in sink.events}
        assert "agent_decision_update" in event_types
        assert "agent_observation_update" in event_types
        # Each narrative event must carry a public_update with the contract fields.
        decision_payload = next(
            e["payload"] for e in sink.events if e.get("event_type") == "agent_decision_update"
        )
        assert decision_payload.get("public_update", {}).get("headline")
        assert decision_payload.get("public_update", {}).get("summary")
        obs_payload = next(
            e["payload"] for e in sink.events if e.get("event_type") == "agent_observation_update"
        )
        assert obs_payload.get("public_update", {}).get("headline")

    @pytest.mark.asyncio
    async def test_preparation_tool_narrative_anchors_dynamic_tool_call(self):
        from app.agent_runtime.preparation.agent_loop import (
            _compose_dynamic_tool_narrative,
        )

        class _TaggedNarrativeLLM:
            async def stream_with_system(self, _system_prompt, _user_content, **_kw):
                yield (
                    "<HEADLINE>Knowledge search unavailable</HEADLINE>"
                    "<SUMMARY>Knowledge base is not configured for this task.</SUMMARY>"
                    "<IMPACT>The task can continue using local context.</IMPACT>"
                    "<NEXT_ACTION>Continue with section suggestions.</NEXT_ACTION>"
                    "<DETAIL>Error code KNOWLEDGE_NOT_CONFIGURED.</DETAIL>"
                    "<DETAIL>Tool execution took 3000 milliseconds.</DETAIL>"
                )

        class _AdapterWithTerminal:
            def last_terminal_event_id(self):
                return "event-terminal-1"

        sink = _MemorySink()
        ctx = _make_runtime_context(sink=sink, llm_client=_TaggedNarrativeLLM())
        envelope = {
            "success": False,
            "summary": "knowledge not configured",
            "tool_call_id": "KnowledgeSearchTool-real-call",
            "attempt": 1,
            "duration_ms": 3000,
            "error": {"code": "KNOWLEDGE_NOT_CONFIGURED", "recoverable": False},
        }

        await _compose_dynamic_tool_narrative(
            state=_base_state(),
            tool_name="KnowledgeSearchTool",
            tool_inputs={"query": "reservation sign in rules"},
            envelope=envelope,
            tool_adapter=_AdapterWithTerminal(),
            llm_client=_TaggedNarrativeLLM(),
            ctx=ctx,
        )

        update = next(
            e for e in sink.events if e.get("event_type") == "tool_narrative_update"
        )
        payload = update["payload"]
        assert payload["source_tool_call_id"] == "KnowledgeSearchTool-real-call"
        assert payload["source_event_id"] == "event-terminal-1"
        assert payload["attempt"] == 1
        assert payload["narrative_source"] == "llm"
        assert payload["fallback_used"] is False
        assert payload["public_update"]["headline"] == "Knowledge search unavailable"


class TestEventDurability:
    """Phase 2.9B.2 — durable events must be awaited before the producer
    returns (no fire-and-forget / no create_task without a reference)."""

    @pytest.mark.asyncio
    async def test_emitter_awaits_sink_before_returning(self):
        """A sink that only records events AFTER the emit coroutine finishes
        must still see them by the time emit_* returns — proving the emit is
        awaited, not scheduled."""
        from app.agent_runtime.preparation.event_emitter import PreparationEventEmitter

        class _DelayedSink:
            """Writes the event synchronously during the await (the real
            LiveAgentEventSink writes the DB row inside emit)."""

            def __init__(self):
                self.events = []

            async def emit(self, **kw):
                self.events.append(kw)
                return {"ok": True}

        sink = _DelayedSink()
        ctx = _make_runtime_context(sink=sink)
        emitter = PreparationEventEmitter(ctx)
        # Force narrative on.
        import app.agent_runtime.preparation.event_emitter as prep_em

        prep_em.get_feature_flags = lambda: SimpleNamespace(
            phase29b_narrative_enabled_for=lambda _n: True,
        )
        await emitter.emit_decision_update(
            decision_id="t:durability:0",
            step_index=0,
            action="finish",
            tool_name=None,
            public_update={"headline": "决策已持久化", "summary": "s", "impact": "i"},
        )
        # No sleep, no event-loop flush — the event is already in the sink.
        assert len(sink.events) == 1
        assert sink.events[0]["event_type"] == "agent_decision_update"

    @pytest.mark.asyncio
    async def test_control_event_started_is_persisted(self):
        """preparation_started (the event that was lost in task 89) must be
        persisted when the producer returns."""
        from app.agent_runtime.preparation.event_emitter import PreparationEventEmitter

        sink = _MemorySink()
        ctx = _make_runtime_context(sink=sink)
        emitter = PreparationEventEmitter(ctx)
        await emitter.emit_preparation_started("cap=summary")
        assert len(sink.events) == 1
        assert sink.events[0]["event_type"] == "preparation_started"

    @pytest.mark.asyncio
    async def test_sink_failure_does_not_crash_producer(self):
        """A failing sink must not make the main task fail (log-and-continue
        per the existing EventSink strategy)."""
        from app.agent_runtime.preparation.event_emitter import PreparationEventEmitter

        class _FailingSink:
            async def emit(self, **_kw):
                raise RuntimeError("sink down")

        ctx = _make_runtime_context(sink=_FailingSink())
        emitter = PreparationEventEmitter(ctx)
        # Must not raise.
        await emitter.emit_preparation_started("cap")
        await emitter.emit_decision_update(
            decision_id="t:fail:0",
            step_index=0,
            action="finish",
            tool_name=None,
            public_update={"headline": "h", "summary": "s"},
        )


class TestPhase29BNarrativeFeatureFlags:
    """18.6 — narrative gating: agents can run while narrative is off."""

    @pytest.mark.asyncio
    async def test_dynamic_agent_runs_but_narrative_off_no_public_update(self, monkeypatch):
        """Narrative flag OFF: the agent loop still executes (real LLM call)
        but no agent_decision_update / agent_observation_update events emit."""
        from app.agent_runtime.preparation.event_emitter import PreparationEventEmitter

        sink = _MemorySink()
        ctx = _make_runtime_context(sink=sink)
        emitter = PreparationEventEmitter(ctx)
        # narrative flag OFF (default in AgentRuntimeFeatureFlags).
        import app.agent_runtime.preparation.event_emitter as prep_em

        prep_em.get_feature_flags = lambda: AgentRuntimeFeatureFlags()
        await emitter.emit_decision_update(
            decision_id="t:off:0",
            step_index=0,
            action="finish",
            tool_name=None,
            public_update={"headline": "h", "summary": "s"},
        )
        # Agent decision narrative suppressed; no event emitted.
        assert sink.events == []

    def test_phase29b_requires_global_and_agent_flag(self):
        from app.agent_runtime.feature_flags import AgentRuntimeFeatureFlags

        flags = AgentRuntimeFeatureFlags(
            phase29b_narrative_enabled=True,
            phase29b_preparation_narrative_enabled=True,
        )
        assert flags.phase29b_narrative_enabled_for("PreparationAgent") is True
        assert flags.phase29b_narrative_enabled_for("RepairAgent") is False

        global_off = AgentRuntimeFeatureFlags(
            phase29b_preparation_narrative_enabled=True,
        )
        assert global_off.phase29b_narrative_enabled_for("PreparationAgent") is False

    def test_dynamic_agent_switch_off_skips_subgraph(self, monkeypatch):
        """preparation_agent_enabled=False must not enter the dynamic subgraph
        (node routes elsewhere)."""
        from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
            route_after_parse_template,
        )

        state = _base_state()
        state["preparation_agent_enabled"] = False
        result = route_after_parse_template(state)
        # Must NOT route to the prep subgraph node.
        assert result != "prep_subgraph"
