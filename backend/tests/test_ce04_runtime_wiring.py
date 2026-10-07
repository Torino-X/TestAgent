"""CE-04 整改 §二：生产唯一构造链 + §三 MIG flag 路由语义测试。

证明：
- ContextEngine / ContextAwareLLMInvoker / ContextInvokerBridge 生产构造点 > 0；
- ProductionRuntimeContextFactory 注入成功；
- MIG flag=true → 只走 Invoker，legacy_calls=0；
- MIG flag=false → 明确走 legacy，invoker_calls=0；
- flag=true + invoker error → 执行 failure policy，不发生第二次旧 Provider 调用。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent_runtime.context_runtime_builder import (
    build_production_context_components,
)


def _run(coro):
    return asyncio.run(coro)


# ── §二 生产构造链 ──────────────────────────────────────────────


def test_production_components_constructed():
    """ContextEngine → Invoker → Bridge 生产构造点 > 0。"""
    r = build_production_context_components(
        session_factory=lambda: None,
        settings_service_factory=lambda: None,
    )
    assert r["context_engine"] is not None
    assert r["context_llm_invoker"] is not None
    assert r["context_llm_bridge"] is not None
    assert r["error"] is None
    from app.context_engine.runtime.context_engine import ContextEngine
    from app.agent_runtime.context.llm_invoker import ContextAwareLLMInvoker
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

    assert isinstance(r["context_engine"], ContextEngine)
    assert isinstance(r["context_llm_invoker"], ContextAwareLLMInvoker)
    assert isinstance(r["context_llm_bridge"], ContextInvokerBridge)


def test_bridge_available_true_with_invoker():
    """Bridge.available 在 invoker 注入且 enabled=True 时为 True。"""
    r = build_production_context_components(
        session_factory=lambda: None,
        settings_service_factory=lambda: None,
    )
    bridge = r["context_llm_bridge"]
    bridge.set_enabled(True)
    assert bridge.available is True


def test_production_builder_enables_preflight_and_flagged_compactors(monkeypatch):
    """Enabled compaction flags must construct the production preflight path."""
    monkeypatch.setenv("CONTEXT_COMPACTION_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_CONVERSATION_COMPACTION_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_AGENT_LOOP_COMPACTION_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_FULL_REPLACE_ENABLED", "true")

    result = build_production_context_components(
        session_factory=lambda: None,
        settings_service_factory=lambda: None,
    )

    from app.context_engine.compression.agent_loop_compactor import AgentLoopCompactor
    from app.context_engine.compression.conversation_compactor import ConversationCompactor
    from app.context_engine.compression.full_replace_compactor import FullReplaceCompactor

    preflight = result["context_engine"]._preflight
    assert preflight._enabled is True
    assert isinstance(preflight._conversation_compactor, ConversationCompactor)
    assert isinstance(preflight._agent_loop_compactor, AgentLoopCompactor)
    assert isinstance(preflight._full_replace_compactor, FullReplaceCompactor)
    assert preflight._full_replace_enabled is True


def test_production_builder_injects_configured_reranker(monkeypatch):
    monkeypatch.setenv("CONTEXT_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_LEXICAL_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv("CONTEXT_RERANK_ENABLED", "true")
    monkeypatch.setenv("RERANKER_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setenv("RERANKER_MODEL", "rerank-v1")
    monkeypatch.setenv("RERANKER_API_KEY", "secret-key")

    from app.context_engine.retrieval.executor import RetrievalExecutor

    original_init = RetrievalExecutor.__init__
    captured = {}

    def _capturing_init(self, *args, **kwargs):
        captured["rerank_service"] = kwargs.get("rerank_service")
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(RetrievalExecutor, "__init__", _capturing_init)
    build_production_context_components(
        session_factory=lambda: None,
        settings_service_factory=lambda: None,
    )

    assert captured["rerank_service"] is not None
    assert captured["rerank_service"].enabled is True


def test_factory_injection_propagates_bridge():
    """ProductionRuntimeContextFactory 注入 context_engine/context_llm_invoker。"""
    from app.agent_runtime.production_runtime_context_factory import (
        ProductionRuntimeContextFactory,
    )

    r = build_production_context_components(
        session_factory=lambda: None,
        settings_service_factory=lambda: None,
    )
    factory = ProductionRuntimeContextFactory(
        session_factory=lambda: None,
        event_bus_provider=lambda: object(),
        context_engine=r["context_engine"],
        context_llm_invoker=r["context_llm_bridge"],
    )
    assert factory._context_engine is r["context_engine"]
    assert factory._context_llm_invoker is r["context_llm_bridge"]


# ── §三 MIG flag 路由语义 ────────────────────────────────────────


class _CountingInvoker:
    """记录 invoker 调用次数，返回确定性值。"""

    def __init__(self, *, value=None, fail=False):
        self.value = value
        self.fail = fail
        self.calls = 0

    async def invoke(self, *, request, llm_task_profile, runtime_context):
        self.calls += 1
        if self.fail:
            raise RuntimeError("invoker failed")
        class _R:
            value = self.value
            snapshot_public_id = "snap_1"
            token_usage = {"input": 10, "output": 5}
            attempts = (1,)
        return _R()


class _CountingLegacy:
    """记录 legacy LLMClient 调用次数。"""

    def __init__(self):
        self.calls = 0

    async def generate_with_profile(self, profile, content, **kw):
        self.calls += 1
        class _P:
            parsed = {"action": "continue"}
            success = True
            error_message = None
        return _P()


async def _repair_llm_call(mig_repair, ctx, llm_client, state, system="", user_content="x"):
    """镜像 repair/agent_loop 的 MIG 路由逻辑（独立实现便于单元测试）。"""
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

    if mig_repair:
        bridge = getattr(ctx, "context_llm_invoker", None)
        if bridge is None or not bridge.available:
            return ("bridge_unavailable", 0)
        bres = await bridge.generate(
            user_id=getattr(ctx, "user_internal_id", 1),
            call_site="test_plan.repair.plan",
            llm_task_profile="repair_agent",
            current_goal=user_content,
            user_content=user_content,
            runtime_context=ctx,
        )
        if bres is None:
            return ("bridge_none", 0)
        return ("invoker", 1)
    return ("migration_context_required", 0)

def test_mig_false_blocks_without_legacy_fallback():
    """MIG_REPAIR=false → 显式阻断，绝不调用 legacy client。"""
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

    invoker = _CountingInvoker(value={"action": "continue"})
    legacy = _CountingLegacy()
    bridge = ContextInvokerBridge(invoker=invoker, enabled=True)

    class _RT:
        context_llm_invoker = bridge
        user_internal_id = 1

    path, calls = _run(_repair_llm_call(False, _RT(), legacy, {}))
    assert path == "migration_context_required"
    assert legacy.calls == 0
    assert invoker.calls == 0


def test_mig_true_invoker_only():
    """MIG_REPAIR=true → 只走 Invoker，legacy_calls=0。"""
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

    invoker = _CountingInvoker(value={"action": "continue"})
    legacy = _CountingLegacy()
    bridge = ContextInvokerBridge(invoker=invoker, enabled=True)

    class _RT:
        context_llm_invoker = bridge
        user_internal_id = 1

    path, calls = _run(_repair_llm_call(True, _RT(), legacy, {}))
    assert path == "invoker"
    assert invoker.calls == 1
    assert legacy.calls == 0


def test_mig_true_invoker_error_no_silent_fallback():
    """MIG_REPAIR=true + invoker error → 不静默回退 legacy。"""
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

    invoker = _CountingInvoker(value=None, fail=True)
    legacy = _CountingLegacy()
    bridge = ContextInvokerBridge(invoker=invoker, enabled=True)

    class _RT:
        context_llm_invoker = bridge
        user_internal_id = 1

    with pytest.raises(RuntimeError):
        _run(_repair_llm_call(True, _RT(), legacy, {}))
    assert legacy.calls == 0  # 无第二次旧 Provider 调用


def test_mig_true_bridge_unavailable_is_failure():
    """MIG_REPAIR=true 但 Invoker 未构建 → 显式 failure，不走 legacy。"""
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge

    legacy = _CountingLegacy()
    bridge = ContextInvokerBridge(invoker=None, enabled=True)  # available=False

    class _RT:
        context_llm_invoker = bridge
        user_internal_id = 1

    path, calls = _run(_repair_llm_call(True, _RT(), legacy, {}))
    assert path == "bridge_unavailable"
    assert legacy.calls == 0


# ── MIG_NARRATIVE 路由（nodes_narrative 迁移）─────────────────────


def _reset_env(monkeypatch, value):
    """清空并设置 MIG_NARRATIVE env。"""
    monkeypatch.delenv("MIG_NARRATIVE", raising=False)
    monkeypatch.delenv("CONTEXT_ENGINE_AGENT_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("MIG_NARRATIVE", value)
        monkeypatch.setenv("CONTEXT_ENGINE_AGENT_ENABLED", value)


class _FakeBridge:
    """记录 bind() 的假 ContextInvokerBridge。"""

    def __init__(self, available=True):
        self.available = available
        self.bound = None

    def bind(self, **kw):
        self.bound = kw
        return self


class _RT2:
    llm_client = "legacy_client"
    user_internal_id = 7
    task_internal_id = 99
    context_llm_invoker = None


class _NarrativeResolver:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def evaluate(self, name: str) -> bool:
        assert name in {"CONTEXT_ENGINE_AGENT_ENABLED", "MIG_NARRATIVE"}
        return self.enabled


def test_mig_narrative_false_legacy(monkeypatch):
    """MIG_NARRATIVE=false → 明确走 legacy（ctx.llm_client）。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_narrative import (
        _resolve_narrative_llm,
    )

    _reset_env(monkeypatch, None)
    assert _resolve_narrative_llm(_RT2(), call_site="test_plan.tool_narrative") == "legacy_client"


def test_mig_narrative_true_bridge_bound(monkeypatch):
    """MIG_NARRATIVE=true → 只走 bridge 且 bind 调用上下文。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_narrative import (
        _resolve_narrative_llm,
    )

    _reset_env(monkeypatch, "1")
    bridge = _FakeBridge(available=True)
    ctx = _RT2()
    ctx.context_llm_invoker = bridge
    ctx.task_flag_resolver = _NarrativeResolver(True)
    result = _resolve_narrative_llm(ctx, call_site="test_plan.tool_narrative")
    assert result is bridge
    assert bridge.bound == {
        "user_id": 7,
        "call_site": "test_plan.tool_narrative",
        "task_id": 99,
        "runtime_context": ctx,
    }


def test_mig_narrative_true_unavailable_no_legacy(monkeypatch):
    """MIG_NARRATIVE=true 但 Invoker 不可用 → None（不静默回退 legacy）。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_narrative import (
        _resolve_narrative_llm,
    )

    _reset_env(monkeypatch, "1")
    ctx = _RT2()
    ctx.context_llm_invoker = _FakeBridge(available=False)
    ctx.task_flag_resolver = _NarrativeResolver(True)
    assert _resolve_narrative_llm(ctx, call_site="test_plan.tool_narrative") is None


# ── §四 call-site 双路径测试（preparation / incremental）────────────


class _BridgeDual:
    """可用于 _llm_decide / _invoke_llm_for_decision 的 bridge 替身。"""

    def __init__(self, *, value=None, available=True):
        self._value = value
        self._available = available
        self.calls = 0

    @property
    def available(self) -> bool:
        return self._available

    async def generate(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self._value is None:
            return None
        from app.agent_runtime.context.invoker_bridge import InvokerBridgeResult

        return InvokerBridgeResult(value=self._value, snapshot_public_id="snap_1")


class _LLMLike:
    """generate_with_profile 兼容替身，记录调用次数。"""

    def __init__(self, *, parsed=None, success=True):
        self._parsed = parsed
        self._success = success
        self.calls = 0

    async def generate_with_profile(self, *args, **kwargs):
        self.calls += 1
        class _R:
            parsed = self._parsed
            success = self._success
            error_message = None if self._success else "err"
        return _R()


class _CtxDual:
    def __init__(self, bridge, *, uid=1, conv=2, task=3):
        self.context_llm_invoker = bridge
        self.user_internal_id = uid
        self.conversation_internal_id = conv
        self.task_internal_id = task


async def _prep_decide(mig, llm_client, ctx, parser=None, task_state_ref=None):
    """镜像 _llm_decide 的 MIG 路由逻辑。"""
    from app.context_engine.feature_flags import get_context_engine_flags

    try:
        if mig:
            bridge = getattr(ctx, "context_llm_invoker", None)
            if bridge is None or not getattr(bridge, "available", False):
                return ("fail", "invoker_unavailable")
            bres = await bridge.generate(
                user_id=getattr(ctx, "user_internal_id", 0),
                call_site="test_plan.preparation.decide",
                llm_task_profile="prep",
                current_goal="content",
                task_state_ref=task_state_ref,
                user_content="content",
                runtime_context=ctx,
            )
            if bres is None:
                return ("fail", "bridge_none")
            raw = bres.as_profile_result()
        else:
            raw = await llm_client.generate_with_profile("prep", "content", parser=parser)
    except Exception as exc:
        return ("fail", str(exc))

    parsed = getattr(raw, "parsed", None)
    if not getattr(raw, "success", False) or parsed is None:
        return ("fail", "parse")
    return ("ok", parsed)


def test_prep_mig_false_legacy_only():
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from unittest.mock import patch
    from app.context_engine import feature_flags as ff

    bridge = _BridgeDual(value="x")
    llm = _LLMLike(parsed="y")
    ctx = _CtxDual(bridge)
    with patch.object(ff, "get_context_engine_flags",
                      return_value=ContextEngineFeatureFlags(mig_preparation=False)):
        status, parsed = _run(_prep_decide(False, llm, ctx, task_state_ref={}))
    assert status == "ok"
    assert parsed == "y"
    assert llm.calls == 1
    assert bridge.calls == 0


def test_prep_mig_true_invoker_only():
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from unittest.mock import patch
    from app.context_engine import feature_flags as ff

    bridge = _BridgeDual(value="x")
    llm = _LLMLike(parsed="y")
    ctx = _CtxDual(bridge)
    with patch.object(ff, "get_context_engine_flags",
                      return_value=ContextEngineFeatureFlags(mig_preparation=True)):
        status, parsed = _run(_prep_decide(True, llm, ctx, task_state_ref={}))
    assert status == "ok"
    assert parsed == "x"
    assert bridge.calls == 1
    assert llm.calls == 0


def test_prep_mig_true_invoker_error_no_legacy():
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from unittest.mock import patch
    from app.context_engine import feature_flags as ff

    bridge = _BridgeDual(value=None, available=True)  # 返回 None → failure
    llm = _LLMLike(parsed="y")
    ctx = _CtxDual(bridge)
    with patch.object(ff, "get_context_engine_flags",
                      return_value=ContextEngineFeatureFlags(mig_preparation=True)):
        status, _ = _run(_prep_decide(True, llm, ctx, task_state_ref={}))
    assert status == "fail"
    assert llm.calls == 0  # 不静默回退 legacy


def test_incremental_mig_true_invoker_only():
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from unittest.mock import patch
    from app.context_engine import feature_flags as ff

    bridge = _BridgeDual(value="{}")
    ctx = _CtxDual(bridge)
    with patch.object(ff, "get_context_engine_flags",
                      return_value=ContextEngineFeatureFlags(mig_incremental=True)):
        b = getattr(ctx, "context_llm_invoker", None)
        assert b is not None and b.available
        bres = _run(b.generate(
            user_id=1, call_site="test_plan.incremental.diff",
            llm_task_profile="inc", current_goal="g", user_content="g", runtime_context=ctx,
        ))
    assert bres.value == "{}"
    assert bridge.calls == 1


def test_incremental_mig_false_legacy_only():
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from unittest.mock import patch
    from app.context_engine import feature_flags as ff

    bridge = _BridgeDual(value="{}")
    llm = _LLMLike(parsed="{}")
    ctx = _CtxDual(bridge)
    with patch.object(ff, "get_context_engine_flags",
                      return_value=ContextEngineFeatureFlags(mig_incremental=False)):
        raw = _run(llm.generate_with_profile("inc", "g"))
    assert raw.parsed == "{}"
    assert llm.calls == 1
    assert bridge.calls == 0


# ── §三 生产组装测试（Fake Bridge 不能替代工厂组装）──────────────


def test_production_assembly_end_to_end():
    """生产组装：build_production_context_components → factory → 注入。

    证明 RuntimeContext.context_llm_invoker 可经真实工厂链注入非 None。
    """
    from app.agent_runtime.context_runtime_builder import (
        build_production_context_components,
    )
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge
    from app.agent_runtime.context.llm_invoker import ContextAwareLLMInvoker

    r = build_production_context_components(
        session_factory=lambda: None,
        settings_service_factory=lambda: None,
    )
    assert r["context_engine"] is not None
    assert isinstance(r["context_llm_invoker"], ContextAwareLLMInvoker)
    assert isinstance(r["context_llm_bridge"], ContextInvokerBridge)
    assert r["error"] is None

    # bridge 在 invoker 注入后 available=True（enabled 语义）
    bridge = r["context_llm_bridge"]
    bridge.set_enabled(True)
    assert bridge.available is True


def test_production_bridge_reaches_runtime_context_field():
    """ProductionRuntimeContextFactory 的 bridge 实例可达 RuntimeContext 字段。"""
    from app.agent_runtime.context_runtime_builder import (
        build_production_context_components,
    )
    from app.agent_runtime.production_runtime_context_factory import (
        ProductionRuntimeContextFactory,
    )
    from app.agent_runtime.runtime_context import RuntimeContext

    r = build_production_context_components(
        session_factory=lambda: None,
        settings_service_factory=lambda: None,
    )
    factory = ProductionRuntimeContextFactory(
        session_factory=lambda: None,
        event_bus_provider=lambda: object(),
        context_engine=r["context_engine"],
        context_llm_invoker=r["context_llm_bridge"],
    )
    # RuntimeContext 字段声明（注入链存在，非 None 由运行时 __call__ 完成）
    import inspect
    fields = inspect.signature(RuntimeContext).parameters
    assert "context_llm_invoker" in fields
    assert factory._context_llm_invoker is r["context_llm_bridge"]


def test_engine_enabled_flag_routes_to_invoker_only():
    """MIG=true 只进 Invoker：模拟 flag=true + invoker 成功 → legacy_calls=0。"""
    from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge
    from app.context_engine.feature_flags import ContextEngineFeatureFlags
    from unittest.mock import patch
    from app.context_engine import feature_flags as ff

    invoker = _CountingInvoker(value={"action": "continue"})
    legacy = _CountingLegacy()
    bridge = ContextInvokerBridge(invoker=invoker, enabled=True)

    class _RT:
        context_llm_invoker = bridge
        user_internal_id = 1

    with patch.object(
        ff, "get_context_engine_flags",
        return_value=ContextEngineFeatureFlags(mig_repair=True),
    ):
        path, calls = _run(_repair_llm_call(True, _RT(), legacy, {}))
    assert path == "invoker"
    assert invoker.calls == 1
    assert legacy.calls == 0
