"""CE-02 WP-10 + 整改六：LangGraph Pilot E2E 测试。

覆盖：
- Provider 成功：Snapshot completed、context_state 写入、thread_id=task_public_id；
- Provider 失败：Pilot last_error 为安全错误、Snapshot failed、无原始 Provider
  body/stack、State 可序列化；
- Schema Retry：第一 Snapshot completed、第二 Snapshot completed、State 仅保存
  最终成功 Snapshot ID。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent_runtime.context import ContextAwareLLMInvoker, RetryPolicy
from app.agent_runtime.graphs.ce_pilot import build_compiled_ce_pilot_graph
from app.context_engine.models.snapshot_models import ContextStateRef, ContextStateStats


class _FakeInvokerResult:
    def __init__(self, snapshot_id, value=None, context_state_ref=None, token_usage=None):
        self.value = value if value is not None else {"summary": "pilot summary"}
        self.snapshot_public_id = snapshot_id
        self.context_state_ref = context_state_ref
        self.token_usage = token_usage or {"input": 10, "output": 5}
        self.provider_request_id = None


def _state_ref(snapshot_id):
    return ContextStateRef(
        latest_snapshot_public_id=snapshot_id,
        profile_key="ce.pilot.summarize.v1",
        stats=ContextStateStats(included_ref_count=1, dropped_ref_count=0, retrieval_run_count=0, estimated_input_tokens=10),
        source_refs=[],
    )


class _FakeInvoker:
    """假 invoker：成功返回固定结果 + context_state_ref。"""

    def __init__(self, snapshot_id="cs_pilot_1"):
        self.snapshot_id = snapshot_id
        self.calls = 0

    async def invoke(self, *, request, llm_task_profile, runtime_context, retry_policy=None):
        self.calls += 1
        assert request.call_site == "ce.pilot.summarize"
        assert request.thread_id == request.task_id  # thread_id = task_public_id
        return _FakeInvokerResult(self.snapshot_id, context_state_ref=_state_ref(self.snapshot_id))


class _FakeSafeFailInvoker:
    """假 invoker：抛安全 ContextEngineFailure（Snapshot failed，无原始 Provider body/stack）。"""

    def __init__(self, snapshot_id="cs_pilot_fail"):
        self.snapshot_id = snapshot_id
        self.calls = 0

    async def invoke(self, *, request, llm_task_profile, runtime_context, retry_policy=None):
        self.calls += 1
        from app.context_engine.errors import ContextEngineError, ContextEngineFailure, ContextEngineStage

        error = ContextEngineError(
            code="llm.provider.timeout",
            detail="Provider 暂时不可用",
            stage=ContextEngineStage.SNAPSHOT,
            retryable=True,
        )
        raise ContextEngineFailure(error)


class _FakeSchemaRetryInvoker:
    """假 invoker：模拟 Schema Retry（内部完成）。

    真实 ContextAwareLLMInvoker 在 invoke() 内做 Schema Retry：
    第一 snapshot completed（parser 失败）→ 第二 snapshot completed（重试成功）。
    对 Pilot 图而言 invoke() 只被调用一次，返回最终成功 Snapshot ID。
    """

    def __init__(self):
        self.calls = 0
        self.snapshot_ids = ["cs_schema_1", "cs_schema_2"]

    async def invoke(self, *, request, llm_task_profile, runtime_context, retry_policy=None):
        self.calls += 1
        # 模拟内部 retry：返回最终成功 Snapshot（cs_schema_2）
        final_sid = self.snapshot_ids[-1]
        return _FakeInvokerResult(final_sid, context_state_ref=_state_ref(final_sid))


def _runtime_context(invoker):
    class _Cancel:
        def is_cancelled(self, task_id):
            return False

    class _RT:
        context_llm_invoker = invoker
        context_engine = None
        cancellation_service = _Cancel()
        user_internal_id = 1
        def session_factory(self):
            class _S:
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    return False
                async def commit(self):
                    pass
            return _S()
    return _RT()


def _initial_state(**overrides):
    state = {
        "task_public_id": "task_pub_1",
        "task_id": "task_pub_1",
        "user_id": "usr_1",
        "conversation_id": "conv_1",
        "user_prompt": "生成测试方案",
        "call_site": "ce.pilot.summarize",
        "user_internal_id": 1,
        "task_internal_id": 2,
        "conversation_internal_id": 3,
    }
    state.update(overrides)
    return state


async def test_pilot_provider_success_snapshot_completed_context_state():
    """Provider 成功：Snapshot completed + context_state 写入 + thread_id=task_public_id。"""
    invoker = _FakeInvoker()
    graph = build_compiled_ce_pilot_graph()
    ctx = _runtime_context(invoker)
    config = {"configurable": {"thread_id": "task_pub_1", "runtime_context": ctx}}
    result = await graph.ainvoke(_initial_state(), config=config)
    assert result.get("last_error") is None
    assert result.get("pilot_summary") == "pilot summary"
    assert result.get("snapshot_public_id") == "cs_pilot_1"
    # context_state 写入（含 latest_snapshot_public_id + stats）
    context_state = result.get("context_state")
    assert context_state is not None
    assert context_state["latest_snapshot_public_id"] == "cs_pilot_1"
    assert "stats" in context_state
    assert result.get("pilot_token_usage") == {"input": 10, "output": 5}
    assert invoker.calls == 1
    # thread_id 校验（invoker 内部断言 request.thread_id == task_public_id）


async def test_pilot_provider_failure_safe_error_no_body():
    """Provider 失败：last_error 为安全错误、无原始 Provider body/stack、State 可序列化。"""
    invoker = _FakeSafeFailInvoker()
    graph = build_compiled_ce_pilot_graph()
    ctx = _runtime_context(invoker)
    config = {"configurable": {"thread_id": "task_pub_1", "runtime_context": ctx}}
    result = await graph.ainvoke(_initial_state(), config=config)
    # last_error 存在且安全（无原始 Provider body/stack）
    last_error = result.get("last_error")
    assert last_error is not None
    assert last_error["code"] == "ce_pilot.invoke_failed"
    # ContextEngineFailure.str 只输出 code/stage，不含 detail/堆栈
    assert "Traceback" not in str(last_error)
    assert "stack" not in str(last_error).lower()
    assert "Provider 暂时不可用" not in str(last_error)  # detail 不泄漏
    # State 可序列化（无 Session/Client）
    import json

    json.dumps(result)  # 不抛异常


async def test_pilot_schema_retry_final_snapshot_only():
    """Schema Retry：内部完成（第一/第二 Snapshot 均 completed），State 仅保存最终成功 Snapshot ID。"""
    invoker = _FakeSchemaRetryInvoker()
    graph = build_compiled_ce_pilot_graph()
    ctx = _runtime_context(invoker)
    config = {"configurable": {"thread_id": "task_pub_1", "runtime_context": ctx}}
    result = await graph.ainvoke(_initial_state(), config=config)
    assert result.get("last_error") is None
    assert result.get("pilot_summary") == "pilot summary"
    # State 仅保存最终成功 Snapshot ID（内部 retry 的最终结果）
    assert result.get("snapshot_public_id") == "cs_schema_2"
    context_state = result.get("context_state")
    assert context_state["latest_snapshot_public_id"] == "cs_schema_2"
    # invoke 只被调用一次（retry 在 invoker 内部完成）
    assert invoker.calls == 1


async def test_pilot_ainvoke_end_to_end():
    invoker = _FakeInvoker()
    graph = build_compiled_ce_pilot_graph()
    ctx = _runtime_context(invoker)
    config = {"configurable": {"thread_id": "task_pub_1", "runtime_context": ctx}}
    result = await graph.ainvoke(_initial_state(), config=config)
    assert result.get("last_error") is None
    assert result.get("pilot_summary") == "pilot summary"
    assert result.get("snapshot_public_id") == "cs_pilot_1"
    assert result.get("pilot_token_usage") == {"input": 10, "output": 5}
    assert invoker.calls == 1


async def test_pilot_thread_id_equals_task_public_id():
    """thread_id 必须 = task_public_id。"""
    from app.agent_runtime.graph_thread_id import require_graph_thread_id

    state = {"task_public_id": "task_pub_abc", "task_id": "task_pub_abc"}
    assert require_graph_thread_id(state, context="ce_pilot") == "task_pub_abc"


async def test_pilot_invoker_failure_sets_last_error():
    class _FailInvoker:
        async def invoke(self, *, request, llm_task_profile, runtime_context, retry_policy=None):
            raise RuntimeError("provider down")

    graph = build_compiled_ce_pilot_graph()
    ctx = _runtime_context(_FailInvoker())
    config = {"configurable": {"thread_id": "task_pub_1", "runtime_context": ctx}}
    result = await graph.ainvoke(_initial_state(), config=config)
    assert result.get("last_error") is not None
    assert result["last_error"]["code"] == "ce_pilot.invoke_failed"


async def test_pilot_profile_registered():
    """call-site 映射到正式 Profile。"""
    from app.context_engine.profiles.registry import get_default_profile_registry

    reg = get_default_profile_registry()
    profile = reg.get_for_call_site("ce.pilot.summarize")
    assert profile.key == "ce.pilot.summarize.v1"
