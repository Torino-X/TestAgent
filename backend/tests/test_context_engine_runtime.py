"""CE-02 WP-8：RuntimeContext 注入 + ContextEngine Facade 测试。

覆盖：RuntimeContext 新 Protocol 类型字段、engine facade 时序、
compose_for_retry 确定性降容、StateSafe/RuntimeOnly 边界。
"""

from __future__ import annotations

import pytest

from app.agent_runtime.context import ContextAwareLLMInvoker
from app.agent_runtime.context.protocols import ContextAwareLLMInvokerProtocol
from app.agent_runtime.runtime_context import RuntimeContext, RuntimeContextFactory
from app.context_engine.runtime import ContextEngine, ContextEngineProtocol, build_context_engine
from app.context_engine.models.snapshot_models import ContextSnapshotRef


def test_runtime_context_new_fields_default_none():
    rc = RuntimeContext(
        user_internal_id=1, task_internal_id=2, conversation_internal_id=3,
        session_factory=lambda: None, settings_service=None, event_sink=None,
        cancellation_service=None,
    )
    assert rc.context_engine is None
    assert rc.context_llm_invoker is None


def test_runtime_context_factory_passes_through():
    factory = RuntimeContextFactory(
        user_internal_id=1, task_internal_id=2, conversation_internal_id=3,
        session_factory=lambda: None, settings_service=None, event_sink=None,
        cancellation_service=None,
    )
    rc = factory.build()
    assert rc.context_engine is None
    assert rc.context_llm_invoker is None


def test_engine_is_protocol():
    engine = build_context_engine()
    assert isinstance(engine, ContextEngine)
    # Protocol 结构匹配
    assert isinstance(engine, ContextEngineProtocol)


def test_invoker_is_protocol():
    invoker = ContextAwareLLMInvoker(
        engine=build_context_engine(),
        snapshot_writer=None,
        llm_client_factory=lambda rc: None,
    )
    assert isinstance(invoker, ContextAwareLLMInvokerProtocol)


def test_state_safe_boundary():
    """StateSafe 协议：ContextSnapshotRef 有 to_state_dict。"""
    from app.context_engine.models.boundaries import StateSafe
    from app.context_engine.models.snapshot_models import ContextSnapshotRef

    ref = ContextSnapshotRef(public_id="cs_1", status="building")
    assert isinstance(ref, StateSafe)
    sd = ref.to_state_dict()
    assert "public_id" in sd


async def test_compose_for_retry_reduces_plan():
    """compose_for_retry 确定性降容：Optional budget 减半。"""
    from app.context_engine.runtime.context_engine import _reduce_plan_for_retry
    from app.context_engine.models.context import ContextPlan, SectionPlan
    from app.context_engine.models.enums import ContextKind

    plan = ContextPlan(
        profile_key="p", profile_version="v1", model_context_window=100000,
        input_budget=10000, output_reserve=1000, runtime_reserve=0, safety_margin=0,
        section_plans={
            "evidence": SectionPlan(kind=ContextKind.EVIDENCE, required=True, budget_tokens=1000, source_types=["artifact"]),
            "conversation": SectionPlan(kind=ContextKind.CONVERSATION, required=False, budget_tokens=800, source_types=["conversation"]),
        },
    )
    reduced = _reduce_plan_for_retry(plan)
    # Required 保留，Optional 减半
    assert reduced.section_plans["evidence"].budget_tokens == 1000
    assert reduced.section_plans["conversation"].budget_tokens == 400


@pytest.mark.asyncio
async def test_engine_resolves_model_window_from_user_config(monkeypatch):
    from types import SimpleNamespace
    from app.context_engine.models.context import ContextRequest

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

    class _FakeRepo:
        def __init__(self, session):
            self.session = session

        async def get_active_for_user(self, user_id):
            assert user_id == 42
            return SimpleNamespace(context_window_tokens=128_000)

    monkeypatch.setattr(
        "app.repositories.model_config_repository.ModelConfigRepository",
        _FakeRepo,
    )

    engine = build_context_engine()
    request = ContextRequest(user_id="42", call_site="intent.recognize")
    runtime_context = SimpleNamespace(
        user_internal_id=42,
        session_factory=lambda: _FakeSession(),
    )

    window = await engine._resolve_runtime_model_window(request, runtime_context)

    assert window == 128_000


def test_runtime_only_boundary():
    """RuntimeOnly 协议：RuntimeContext 不实现 to_state_dict。"""
    from app.context_engine.models.boundaries import RuntimeOnly

    rc = RuntimeContext(
        user_internal_id=1, task_internal_id=2, conversation_internal_id=3,
        session_factory=lambda: None, settings_service=None, event_sink=None,
        cancellation_service=None,
    )
    # RuntimeContext 不应被视为 StateSafe（无 to_state_dict）
    assert not hasattr(rc, "to_state_dict")
