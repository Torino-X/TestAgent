"""CE-04 WP-9：ContextInvokerBridge + 生产注入测试。

覆盖：
- Bridge 不修改业务 State：返回 patch，Node 显式应用。
- 显式传 call_site/llm_task_profile/current_node/current_goal/TaskStateRef。
- flag 关 → None（走既有路径）。
- ProductionRuntimeContextFactory 注入 context_engine/context_llm_invoker。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge, InvokerBridgeResult
from app.agent_runtime.production_runtime_context_factory import ProductionRuntimeContextFactory
from app.agent_runtime.runtime_context import RuntimeContext


def _run(coro):
    return asyncio.run(coro)


class _FakeInvoker:
    """模拟 ContextAwareLLMInvoker.invoke()。"""

    def __init__(self, *, value="generated", snapshot="snap_1"):
        self.value = value
        self.snapshot = snapshot
        self.calls = 0

    async def invoke(self, *, request, llm_task_profile, runtime_context):
        self.calls += 1
        self.last_request = request
        self.last_profile = llm_task_profile

        class _Result:
            value = self.value
            snapshot_public_id = self.snapshot
            token_usage = {"input": 100, "output": 50}
            attempts = (1,)

        return _Result()


class _FakeStreamInvoker(_FakeInvoker):
    """模拟 ContextAwareLLMInvoker.stream()。"""

    async def stream(self, *, request, llm_task_profile, runtime_context):
        self.calls += 1
        self.last_request = request
        self.last_profile = llm_task_profile
        yield "hello "
        yield "world"


class _FakeStateRefBuilder:
    def __call__(self, *, snapshot_public_id, profile_key, token_usage):
        class _Ref:
            def to_state_dict(self):
                return {"latest_snapshot_public_id": snapshot_public_id, "profile_key": profile_key}

        return _Ref()


class TestToolAdapterBridgeContract:
    def test_proxy_uses_public_task_id_and_exposes_session_factory(self):
        from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter

        session_factory = object()
        adapter = TestAgentToolAdapter(
            tool_executor=SimpleNamespace(),
            event_sink=SimpleNamespace(),
            session_factory=session_factory,
            clock=datetime.utcnow,
        )
        ctx_runtime = SimpleNamespace(
            task_internal_id=108,
            conversation_internal_id=10,
            user_internal_id=1,
            settings_service=None,
            context_llm_invoker=None,
            llm_client="provider",
            _intermediate_state={},
        )

        proxy = adapter._build_proxy(
            tool_name="TestPlanGeneratorTool",
            attempt=1,
            tool_call_id="call_1",
            ctx_runtime=ctx_runtime,
            inputs={},
            session=object(),
            graph_state={"task_id": "task_9abb8c99"},
        )

        assert proxy.task_id == "task_9abb8c99"
        assert proxy.session_factory is session_factory
        assert proxy.llm_client == "provider"

    def test_mig_helper_passes_public_task_id_to_bridge(self):
        from app.tools._mig_routing import invoke_via_bridge_or_none

        class _Bridge:
            available = True

            async def generate(self, **kwargs):
                self.kwargs = kwargs
                return SimpleNamespace(value="raw-json")

        bridge = _Bridge()
        context = SimpleNamespace(
            context_llm_invoker=bridge,
            user_internal_id=1,
            conversation_internal_id=10,
            task_id="task_9abb8c99",
            task_internal_id=108,
        )

        err, raw = _run(
            invoke_via_bridge_or_none(
                context=context,
                call_site="test_plan.generate.outline",
                llm_task_profile="profile",
                current_goal="generate",
            )
        )

        assert err is None
        assert raw == "raw-json"
        assert bridge.kwargs["task_id"] == "task_9abb8c99"
        assert bridge.kwargs["runtime_context"] is context


class TestInvokerBridge:
    def test_bridge_returns_patch_not_mutating_state(self):
        invoker = _FakeInvoker()
        bridge = ContextInvokerBridge(invoker=invoker, state_ref_builder=_FakeStateRefBuilder(), enabled=True)

        class _RT:
            pass

        result = _run(bridge.generate(
            user_id=1,
            call_site="test_plan.review",
            llm_task_profile="review_profile",
            current_node="review_format",
            current_goal="请评审测试计划",
            current_user_message_id=456,
            task_state_ref={"task_goal": "生成测试计划"},
            output_contract="json",
            conversation_id=100,
            runtime_context=_RT(),
        ))
        assert isinstance(result, InvokerBridgeResult)
        assert result.value == "generated"
        assert result.snapshot_public_id == "snap_1"
        # patch 含 context_state，但业务 State 未被直接修改
        assert "context_state" in result.context_state_patch
        assert invoker.calls == 1
        # 显式参数透传
        assert invoker.last_request.call_site == "test_plan.review"
        assert invoker.last_request.current_node == "review_format"
        assert invoker.last_request.current_user_message == "请评审测试计划"
        assert invoker.last_request.current_user_message_id == 456
        assert invoker.last_request.state_ref == {"task_goal": "生成测试计划"}

    def test_bridge_disabled_returns_none(self):
        invoker = _FakeInvoker()
        bridge = ContextInvokerBridge(invoker=invoker, enabled=False)
        result = _run(bridge.generate(user_id=1, call_site="x", llm_task_profile="p"))
        assert result is None
        assert invoker.calls == 0

    def test_bridge_no_invoker_returns_none(self):
        bridge = ContextInvokerBridge(enabled=True)
        result = _run(bridge.generate(user_id=1, call_site="x", llm_task_profile="p"))
        assert result is None

    def test_bridge_available_property(self):
        assert ContextInvokerBridge(enabled=True).available is False  # 无 invoker
        assert ContextInvokerBridge(invoker=_FakeInvoker(), enabled=False).available is False
        assert ContextInvokerBridge(invoker=_FakeInvoker(), enabled=True).available is True

    def test_bridge_stream_preserves_chunk_boundaries(self):
        invoker = _FakeStreamInvoker()
        bridge = ContextInvokerBridge(invoker=invoker, enabled=True)

        async def _collect():
            return [
                chunk
                async for chunk in bridge.stream(
                    user_id=1,
                    call_site="chat.reply",
                    llm_task_profile="chat_profile",
                    current_goal="普通提问",
                    output_contract="text",
                    conversation_id=100,
                    task_id="task_1",
                    runtime_context=object(),
                )
            ]

        chunks = _run(_collect())

        assert chunks == ["hello ", "world"]
        assert invoker.calls == 1
        assert invoker.last_request.call_site == "chat.reply"
        assert invoker.last_request.current_user_message == "普通提问"


class TestFactoryInjection:
    def test_factory_passes_context_invoker_to_runtime(self):
        """ProductionRuntimeContextFactory 注入 context_engine/context_llm_invoker。"""
        factory = ProductionRuntimeContextFactory(
            session_factory=lambda: None,
            event_bus_provider=lambda: object(),
            context_engine="fake_engine",
            context_llm_invoker="fake_invoker",
        )
        assert factory._context_engine == "fake_engine"
        assert factory._context_llm_invoker == "fake_invoker"

    def test_factory_default_none(self):
        factory = ProductionRuntimeContextFactory(
            session_factory=lambda: None,
            event_bus_provider=lambda: object(),
        )
        assert factory._context_engine is None
        assert factory._context_llm_invoker is None

    def test_runtime_context_has_invoker_field(self):
        """RuntimeContext 已有 context_engine/context_llm_invoker 可选字段。"""
        import inspect

        fields = inspect.signature(RuntimeContext).parameters
        assert "context_engine" in fields
        assert "context_llm_invoker" in fields


class TestBridgeProfileCompat:
    def test_as_profile_result_compat(self):
        from app.agent_runtime.context.invoker_bridge import InvokerBridgeResult

        r = InvokerBridgeResult(value={"decision": "regenerate"}, snapshot_public_id="s1")
        compat = r.as_profile_result()
        assert compat.parsed == {"decision": "regenerate"}
        assert compat.success is True
        assert compat.error_message is None

        r2 = InvokerBridgeResult(value=None)
        compat2 = r2.as_profile_result()
        assert compat2.success is False
        assert compat2.error_message == "bridge_value_none"


class TestBridgeLLMCompat:
    def test_generate_with_profile_compat(self):
        from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

        class FakeInv:
            async def invoke(self, *, request, llm_task_profile, runtime_context):
                class R:
                    value = {"action": "continue"}
                    snapshot_public_id = "s1"
                    token_usage = {}
                    attempts = ()

                return R()

        bridge = ContextInvokerBridge(invoker=FakeInv(), enabled=True)
        bridge.bind(user_id=1, call_site="test_plan.preparation.decide", task_id=5, runtime_context=object())
        result = _run(bridge.generate_with_profile("prep_profile", "content", parser="json_strict"))
        assert result.success is True
        assert result.parsed == {"action": "continue"}

    def test_bound_clients_keep_identity_when_bindings_interleave(self):
        """A lifespan-scoped bridge must not let a later bind overwrite an earlier caller."""

        class RecordingInvoker:
            def __init__(self):
                self.requests = []

            async def invoke(self, *, request, llm_task_profile, runtime_context):
                self.requests.append(request)
                return SimpleNamespace(
                    value={"action": "continue"},
                    snapshot_public_id=None,
                    token_usage={},
                    attempts=(),
                )

        invoker = RecordingInvoker()
        bridge = ContextInvokerBridge(invoker=invoker, enabled=True)
        first = bridge.bind(
            user_id=101,
            call_site="test_plan.narrative.first",
            task_id=1001,
            runtime_context=SimpleNamespace(conversation_internal_id=501),
        )
        second = bridge.bind(
            user_id=202,
            call_site="test_plan.narrative.second",
            task_id=2002,
            runtime_context=SimpleNamespace(conversation_internal_id=502),
        )

        _run(first.generate_with_profile("profile", "first content"))
        _run(second.generate_with_profile("profile", "second content"))

        assert [(r.user_id, r.call_site, r.task_id, r.conversation_id) for r in invoker.requests] == [
            ("101", "test_plan.narrative.first", "1001", "501"),
            ("202", "test_plan.narrative.second", "2002", "502"),
        ]

    def test_generate_with_profile_invoker_missing_is_real_failure(self):
        """§三：bridge 不承担 flag 路由。invoker 缺失 → 真实失败（非"flag 关闭"信号）。"""
        from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

        bridge = ContextInvokerBridge(enabled=True)  # enabled=True 但无 invoker
        result = _run(bridge.generate_with_profile("p", "c"))
        assert result.success is False
        assert result.error_message == "bridge_unavailable"

        # flag=false 由调用方在调用前决定走 legacy，不调用 bridge —— 这属于路由判断，
        # 不应由 bridge 的 success=False 表示。
        from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge as CIB

        bridge2 = CIB(invoker=None, enabled=True)
        assert bridge2.available is False
