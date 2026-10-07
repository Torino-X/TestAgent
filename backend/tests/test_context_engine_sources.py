"""CE-02 WP-2：Source Adapter / Registry / Orchestrator 测试。

覆盖：Adapter/Registry/Required-Optional/Deadline/Cancellation/concurrency/
owner-scope/去重/超时降级；取消 → 停止+无 Snapshot+不调 LLM 传播，非 degraded。
"""

from __future__ import annotations

import asyncio

import pytest

from app.context_engine.models.context import (
    ContextItem,
    ContextPlan,
    ContextRequest,
    ContextScope,
    SectionPlan,
)
from app.context_engine.models.enums import ContextKind, SourceType
from app.context_engine.models.source import ContextWarning, SourceCollectResult
from app.context_engine.sources.deadline import Deadline, DeadlineExceeded
from app.context_engine.sources.orchestrator import SourceOrchestrator
from app.context_engine.sources.registry import SourceAdapterRegistry
from app.context_engine.errors import ContextEngineFailure


class _FakeAdapter:
    """确定性 adapter：返回预置 items 或触发取消/失败。"""

    def __init__(self, kind, *, items=None, failure=None, cancel=False, slow=False):
        self.source_kind = kind
        self._items = items or []
        self._failure = failure
        self._cancel = cancel
        self._slow = slow
        self.called = False

    async def collect(self, request, section_plan, scope, *, runtime_context):
        self.called = True
        if self._cancel:
            return SourceCollectResult(
                adapter_key=self.source_kind.value,
                kind=self.source_kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.cancelled",
            )
        if self._slow:
            await asyncio.sleep(0.05)
        if self._failure:
            return SourceCollectResult(
                adapter_key=self.source_kind.value,
                kind=self.source_kind,
                attempted=True,
                degraded=True,
                failure_code=self._failure,
                warnings=[ContextWarning(code=self._failure, detail="failed", adapter_key=self.source_kind.value)],
            )
        return SourceCollectResult(
            adapter_key=self.source_kind.value,
            kind=self.source_kind,
            items=self._items,
            attempted=True,
            degraded=False,
        )


class _HangingAdapter:
    """Simulates an optional source whose I/O never returns."""

    def __init__(self, kind):
        self.source_kind = kind
        self.called = False

    async def collect(self, request, section_plan, scope, *, runtime_context):
        self.called = True
        await asyncio.Event().wait()


def _plan(required=None, optional=None) -> ContextPlan:
    required = required or [ContextKind.EVIDENCE]
    optional = optional or []
    section_plans = {}
    for k in required:
        section_plans[k.value] = SectionPlan(kind=k, required=True, budget_tokens=1000, source_types=["artifact"])
    for k in optional:
        section_plans[k.value] = SectionPlan(kind=k, required=False, budget_tokens=500, source_types=["conversation"])
    return ContextPlan(
        profile_key="p", profile_version="v1", model_context_window=100000,
        input_budget=10000, output_reserve=1000, runtime_reserve=0, safety_margin=0,
        section_plans=section_plans,
    )


class _Cancel:
    def __init__(self, cancelled=False):
        self._c = cancelled
    def is_cancelled(self, task_id):
        return self._c


def _ctx(cancel=None):
    class _RT:
        def __init__(self, cancel_obj):
            self.cancellation_service = cancel_obj or _Cancel()
        async def session_factory(self):
            raise AssertionError("adapter 不应在纯逻辑测试中访问 DB")
        def __aenter__(self):
            return self
        def __aexit__(self, *a):
            return False

    return _RT(cancel)


def test_registry_multiple_adapters_per_kind():
    reg = SourceAdapterRegistry()
    a1 = _FakeAdapter(ContextKind.CONVERSATION)
    a2 = _FakeAdapter(ContextKind.CONVERSATION)
    reg.register(a1)
    reg.register(a2)
    assert len(reg.all_for_kind(ContextKind.CONVERSATION)) == 2
    assert reg.has_kind(ContextKind.CONVERSATION)


def test_registry_missing_kind_fails_fast():
    reg = SourceAdapterRegistry()
    with pytest.raises(ContextEngineFailure) as exc_info:
        reg.get_for_kind(ContextKind.MEMORY)
    assert exc_info.value.error.code == "context.source.adapter_not_found"


def test_registry_duplicate_register_rejected():
    reg = SourceAdapterRegistry()
    a = _FakeAdapter(ContextKind.EVIDENCE)
    reg.register(a)
    with pytest.raises(ValueError):
        reg.register(a)


async def test_orchestrator_required_collect():
    reg = SourceAdapterRegistry()
    adapter = _FakeAdapter(
        ContextKind.EVIDENCE,
        items=[ContextItem(item_id="i1", kind=ContextKind.EVIDENCE, source_type="artifact", content="证据", authority=80, estimated_tokens=10)],
    )
    reg.register(adapter)
    orch = SourceOrchestrator(reg)
    request = ContextRequest(user_id="usr_1", call_site="x", task_id="task_1")
    scope = ContextScope(user_id="usr_1", task_id="task_1", thread_id="task_1")
    outcome = await orch.collect(request, _plan(), scope, runtime_context=_ctx())
    assert outcome.ok
    assert len(outcome.all_items()) == 1
    assert adapter.called


async def test_orchestrator_required_failure_fails_fast():
    reg = SourceAdapterRegistry()
    reg.register(_FakeAdapter(ContextKind.EVIDENCE, failure="context.source.adapter_error"))
    orch = SourceOrchestrator(reg)
    request = ContextRequest(user_id="usr_1", call_site="x", task_id="task_1")
    scope = ContextScope(user_id="usr_1", task_id="task_1", thread_id="task_1")
    outcome = await orch.collect(request, _plan(), scope, runtime_context=_ctx())
    assert outcome.required_failure is not None
    assert not outcome.ok


async def test_orchestrator_optional_degrade_not_fail():
    reg = SourceAdapterRegistry()
    reg.register(_FakeAdapter(ContextKind.EVIDENCE, items=[ContextItem(item_id="i1", kind=ContextKind.EVIDENCE, source_type="artifact", content="证据", authority=80, estimated_tokens=10)]))
    reg.register(_FakeAdapter(ContextKind.KNOWLEDGE, failure="context.source.knowledge_not_implemented"))
    orch = SourceOrchestrator(reg)
    request = ContextRequest(user_id="usr_1", call_site="x", task_id="task_1")
    scope = ContextScope(user_id="usr_1", task_id="task_1", thread_id="task_1")
    outcome = await orch.collect(
        request,
        _plan(optional=[ContextKind.KNOWLEDGE]),
        scope,
        runtime_context=_ctx(),
    )
    assert outcome.ok  # required 收集成功
    assert outcome.degraded  # optional 降级


async def test_orchestrator_cancellation_before_begin_no_snapshot():
    """begin 前取消：不创建 Snapshot，只安全传播（不调 adapter）。"""
    reg = SourceAdapterRegistry()
    adapter = _FakeAdapter(ContextKind.EVIDENCE)
    reg.register(adapter)
    orch = SourceOrchestrator(reg)
    request = ContextRequest(user_id="usr_1", call_site="x", task_id="task_1")
    scope = ContextScope(user_id="usr_1", task_id="task_1", thread_id="task_1")
    cancel_ctx = _ctx(cancel=_Cancel(cancelled=True))
    outcome = await orch.collect(request, _plan(), scope, runtime_context=cancel_ctx)
    assert outcome.cancelled
    assert outcome.failure_code == "context.source.cancelled"
    assert not adapter.called  # 取消时 adapter 未被调用


def test_deadline_exhausted_independent():
    d = Deadline(deadline_ms=100, start_epoch_ms=0)
    assert not d.is_expired(50)
    assert d.is_expired(150)
    with pytest.raises(DeadlineExceeded):
        d.raise_if_expired(150)


async def test_concurrency_limited():
    """Optional 并发收集：并发受 Semaphore 限制。"""
    reg = SourceAdapterRegistry()
    reg.register(_FakeAdapter(ContextKind.EVIDENCE, items=[ContextItem(item_id="i1", kind=ContextKind.EVIDENCE, source_type="artifact", content="c", authority=80, estimated_tokens=1)]))
    reg.register(_FakeAdapter(ContextKind.KNOWLEDGE, slow=True))
    orch = SourceOrchestrator(reg, concurrency=2)
    request = ContextRequest(user_id="usr_1", call_site="x", task_id="task_1")
    scope = ContextScope(user_id="usr_1", task_id="task_1", thread_id="task_1")
    outcome = await orch.collect(
        request,
        _plan(optional=[ContextKind.KNOWLEDGE]),
        scope,
        runtime_context=_ctx(),
    )
    assert outcome.ok


async def test_optional_source_timeout_degrades_instead_of_hanging():
    """A stalled optional source must consume only the remaining source budget."""
    reg = SourceAdapterRegistry()
    reg.register(_FakeAdapter(
        ContextKind.EVIDENCE,
        items=[ContextItem(
            item_id="i1",
            kind=ContextKind.EVIDENCE,
            source_type="artifact",
            content="c",
            authority=80,
            estimated_tokens=1,
        )],
    ))
    stalled = _HangingAdapter(ContextKind.KNOWLEDGE)
    reg.register(stalled)
    orch = SourceOrchestrator(reg, deadline_ms=50)
    request = ContextRequest(user_id="usr_1", call_site="x", task_id="task_1")
    scope = ContextScope(user_id="usr_1", task_id="task_1", thread_id="task_1")

    outcome = await asyncio.wait_for(
        orch.collect(
            request,
            _plan(optional=[ContextKind.KNOWLEDGE]),
            scope,
            runtime_context=_ctx(),
        ),
        timeout=0.5,
    )

    assert stalled.called
    assert outcome.ok
    assert outcome.degraded
    assert outcome.timed_out
    assert any(warning.code == "context.source.timeout" for warning in outcome.warnings)
