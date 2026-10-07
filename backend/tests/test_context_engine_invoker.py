"""CE-02 WP-9：ContextAwareLLMInvoker 测试（mock engine + snapshot writer）。

覆盖：Provider 成功+Parser 失败→原 Snapshot completed、Schema Retry 创建新
Snapshot、Provider 失败→Snapshot failed、context-length retry 基础、
begin 前取消无 Snapshot、begin 后取消 abandoned、sent 后取消按真实结果。

注：CE-02 整改一后，ContextAwareLLMInvoker 位于 Agent Runtime 层
（app/agent_runtime/context/llm_invoker.py）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent_runtime.context import ContextAwareLLMInvoker, RetryPolicy
from app.context_engine.errors import ContextEngineFailure
from app.context_engine.models.context import ContextRequest
from app.context_engine.models.compose import ContextComposeResult, ComposeValidation
from app.context_engine.models.snapshot_models import ContextSnapshotRef
from app.context_engine.models.retry_models import SafeLLMError
from app.agent_runtime.context.invoker_bridge import ContextInvokerBridge


class _FakeResult:
    def __init__(self, *, success=True, error_type=None, error_message="", parsed=None, usage=None):
        self.success = success
        self.error_type = error_type
        self.error_message = error_message
        self.parsed = parsed
        self.usage = usage
        self.raw_text = "raw"
        self.provider_request_id = None


class _FakeLLMClient:
    """可编排的假 LLM 客户端。"""

    def __init__(self):
        self.sequence = []
        self.calls = 0
        self.last_images = None

    def enqueue(self, result):
        self.sequence.append(result)

    async def generate_with_profile(self, profile, user_content, *, system_prompt_override=None, images=None):
        self.calls += 1
        self.last_images = images
        if self.sequence:
            return self.sequence.pop(0)
        return _FakeResult(parsed={"summary": "ok"})

    async def stream_with_profile(self, profile, user_content, *, system_prompt_override=None, on_usage=None):
        self.calls += 1
        self.last_stream_profile = profile
        self.last_stream_user_content = user_content
        self.last_stream_system_prompt_override = system_prompt_override
        yield "stream "
        yield "reply"
        if on_usage is not None:
            on_usage({"input": 123, "output": 45})


class _FakeEngine:
    """假 engine：记录 compose 调用次数与 snapshot 公共 id。"""

    def __init__(self):
        self.compose_calls = 0
        self.retry_calls = 0
        self.snapshot_ids = []

    async def compose(self, request, *, runtime_context, execution_mode="active"):
        self.compose_calls += 1
        sid = f"cs_snap_{self.compose_calls}"
        self.snapshot_ids.append(sid)
        return ContextComposeResult(
            messages=[],
            prompt_text="prompt",
            prompt_digest=None,
            estimated_input_tokens=10,
            snapshot_public_id=sid,
        )

    async def compose_for_retry(self, request, *, runtime_context, previous_snapshot_ref):
        self.retry_calls += 1
        sid = f"cs_retry_{self.retry_calls}"
        self.snapshot_ids.append(sid)
        return ContextComposeResult(
            messages=[],
            prompt_text="retry prompt",
            prompt_digest=None,
            estimated_input_tokens=5,
            snapshot_public_id=sid,
        )


class _FakeWriter:
    """假 snapshot writer：追踪状态转换。"""

    def __init__(self):
        self.transitions = []
        self.completed = []
        self.completed_commands = []
        self.failed = []
        self.abandoned = []
        self.shadow = []

    async def mark_sent(self, *, session, public_id, user_id):
        self.transitions.append(("sent", public_id))

    async def complete(self, command, *, session, user_id):
        self.completed.append(command.snapshot_public_id)
        self.completed_commands.append(command)
        self.transitions.append(("completed", command.snapshot_public_id))

    async def fail(self, command, *, session, user_id):
        self.failed.append(command.snapshot_public_id)
        self.transitions.append(("failed", command.snapshot_public_id))

    async def abandon(self, *, session, public_id, user_id):
        self.abandoned.append(public_id)
        self.transitions.append(("abandoned", public_id))


class _FakeSession:
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def commit(self):
        pass


class _NoCancel:
    def is_cancelled(self, task_id):
        return False


class _Cancelled:
    def is_cancelled(self, task_id):
        return True


def _ctx(cancel=None):
    class _RT:
        def __init__(self, c):
            self.cancellation_service = c or _NoCancel()
            self.user_internal_id = 1
        def session_factory(self):
            return _FakeSession()
    return _RT(cancel)


def _request(**kw):
    return ContextRequest(user_id="usr_1", call_site="chat.reply", task_id="task_1", **kw)


async def test_multimodal_request_forwards_request_local_images():
    engine = _FakeEngine()
    writer = _FakeWriter()
    llm = _FakeLLMClient()
    llm.enqueue(_FakeResult(success=True, parsed={"summary": "ok"}))
    invoker = _make_invoker(engine, writer, llm)

    await invoker.invoke(
        request=_request(image_paths=["C:/staged/current-message.png"]),
        llm_task_profile=None,
        runtime_context=_ctx(),
    )

    assert llm.last_images == ["C:/staged/current-message.png"]


def _make_invoker(engine, writer, llm_client):
    return ContextAwareLLMInvoker(
        engine=engine,
        snapshot_writer=writer,
        llm_client_factory=lambda rc: llm_client,
        retry_policy=RetryPolicy(max_provider_retries=0, max_schema_retries=1, max_context_length_retries=1),
    )


async def test_provider_success_parser_failure_completed():
    """Provider 成功 + Parser 失败 → 原 Snapshot completed，不标 failed；
    Schema Retry 创建新 Snapshot。"""
    engine = _FakeEngine()
    writer = _FakeWriter()
    llm = _FakeLLMClient()
    # 第一次 Provider 成功（usage 有值），但 parsed=None → parser 失败
    llm.enqueue(_FakeResult(success=True, usage={"input_tokens": 10, "output_tokens": 20}, parsed=None))
    # Schema Retry 第二次 Provider 成功且 parsed 正常
    llm.enqueue(_FakeResult(success=True, usage={"input_tokens": 5, "output_tokens": 2}, parsed={"summary": "fixed"}))
    invoker = _make_invoker(engine, writer, llm)

    result = await invoker.invoke(request=_request(), llm_task_profile=None, runtime_context=_ctx())

    # 原 Snapshot 保持 completed（Provider 成功即 complete，与 parser 结果无关）
    assert len(writer.completed) >= 1
    assert len(writer.failed) == 0  # parser 失败不标 failed
    # Schema Retry 创建新 Snapshot（compose 两次）
    assert engine.compose_calls >= 2
    assert len(engine.snapshot_ids) == 2
    assert result.snapshot_public_id == engine.snapshot_ids[-1]
    assert result.value == {"summary": "fixed"}


async def test_provider_failure_snapshot_failed():
    engine = _FakeEngine()
    writer = _FakeWriter()
    llm = _FakeLLMClient()
    llm.enqueue(_FakeResult(success=False, error_type="llm_error", error_message="403 permission"))
    invoker = _make_invoker(engine, writer, llm)
    result = await invoker.invoke(request=_request(), llm_task_profile=None, runtime_context=_ctx())
    assert result.value is None
    assert writer.failed  # provider 失败 → snapshot failed


async def test_provider_retry_uses_fresh_snapshot_after_connection_failure():
    """A failed snapshot must never be transitioned to sent for a retry."""
    engine = _FakeEngine()
    writer = _FakeWriter()
    llm = _FakeLLMClient()
    llm.enqueue(
        _FakeResult(
            success=False,
            error_type="llm_error",
            error_message="Connection error.",
        )
    )
    llm.enqueue(
        _FakeResult(
            success=True,
            parsed={"summary": "retried"},
            usage={"input_tokens": 5, "output_tokens": 2},
        )
    )
    invoker = ContextAwareLLMInvoker(
        engine=engine,
        snapshot_writer=writer,
        llm_client_factory=lambda rc: llm,
        retry_policy=RetryPolicy(
            max_provider_retries=1,
            max_schema_retries=0,
            max_context_length_retries=0,
        ),
    )

    result = await invoker.invoke(
        request=_request(), llm_task_profile=None, runtime_context=_ctx()
    )

    assert result.value == {"summary": "retried"}
    assert result.snapshot_public_id == "cs_snap_2"
    assert llm.calls == 2
    assert writer.transitions == [
        ("sent", "cs_snap_1"),
        ("failed", "cs_snap_1"),
        ("sent", "cs_snap_2"),
        ("completed", "cs_snap_2"),
    ]
    assert [item.kind for item in result.attempts] == ["initial", "provider_retry"]
    assert result.attempts[-1].snapshot_public_id == "cs_snap_2"


async def test_provider_retry_failure_returns_provider_result_not_snapshot_error():
    """A failed retry remains an ordinary provider failure with its own snapshot."""
    engine = _FakeEngine()
    writer = _FakeWriter()
    llm = _FakeLLMClient()
    for _ in range(2):
        llm.enqueue(
            _FakeResult(
                success=False,
                error_type="llm_error",
                error_message="Connection error.",
            )
        )
    invoker = ContextAwareLLMInvoker(
        engine=engine,
        snapshot_writer=writer,
        llm_client_factory=lambda rc: llm,
        retry_policy=RetryPolicy(
            max_provider_retries=1,
            max_schema_retries=0,
            max_context_length_retries=0,
        ),
    )

    result = await invoker.invoke(
        request=_request(), llm_task_profile=None, runtime_context=_ctx()
    )

    assert result.value is None
    assert result.snapshot_public_id == "cs_snap_2"
    assert writer.transitions == [
        ("sent", "cs_snap_1"),
        ("failed", "cs_snap_1"),
        ("sent", "cs_snap_2"),
        ("failed", "cs_snap_2"),
    ]


async def test_context_length_retry_creates_new_snapshot():
    engine = _FakeEngine()
    writer = _FakeWriter()
    llm = _FakeLLMClient()
    # 第一次 context_length error → 触发 compose_for_retry → 第二次成功
    llm.enqueue(_FakeResult(success=False, error_type="llm_error", error_message="exceeds context length"))
    llm.enqueue(_FakeResult(success=True, parsed={"summary": "ok"}, usage={"input_tokens": 5, "output_tokens": 2}))
    invoker = _make_invoker(engine, writer, llm)
    result = await invoker.invoke(request=_request(), llm_task_profile=None, runtime_context=_ctx())
    assert result.value == {"summary": "ok"}
    assert engine.retry_calls == 1  # compose_for_retry 被调用
    assert len(engine.snapshot_ids) == 2  # 新 snapshot
    assert result.snapshot_public_id == engine.snapshot_ids[-1]


async def test_cancelled_before_begin_no_snapshot():
    """begin 前取消 → 无 Snapshot，不调 LLM。"""
    engine = _FakeEngine()
    writer = _FakeWriter()
    llm = _FakeLLMClient()
    invoker = _make_invoker(engine, writer, llm)
    with pytest.raises(ContextEngineFailure) as exc_info:
        await invoker.invoke(request=_request(), llm_task_profile=None, runtime_context=_ctx(cancel=_Cancelled()))
    assert exc_info.value.error.code == "context.invoker.cancelled_before_begin"
    assert engine.compose_calls == 0  # 未 compose，无 snapshot
    assert llm.calls == 0  # 未调 LLM


async def test_cancelled_before_sent_abandon():
    """begin 后、sent 前取消 → snapshot abandoned，不调 LLM。"""
    engine = _FakeEngine()

    class _SentCancel:
        """第一次 is_cancelled 返回 False（begin 前检查），第二次 True（sent 前）。"""
        def __init__(self):
            self.calls = 0
        def is_cancelled(self, task_id):
            self.calls += 1
            return self.calls >= 2

    writer = _FakeWriter()
    llm = _FakeLLMClient()
    invoker = _make_invoker(engine, writer, llm)
    with pytest.raises(ContextEngineFailure) as exc_info:
        await invoker.invoke(request=_request(), llm_task_profile=None, runtime_context=_ctx(cancel=_SentCancel()))
    assert exc_info.value.error.code == "context.invoker.cancelled_before_sent"
    assert writer.abandoned  # snapshot abandoned
    assert llm.calls == 0  # 未调 LLM


async def test_schema_retry_creates_new_snapshot():
    """Parser 失败（parsed=None 无 parser_factory）→ Schema Retry 新 snapshot → 第二次成功。"""
    engine = _FakeEngine()
    writer = _FakeWriter()
    llm = _FakeLLMClient()
    # 第一次 parsed=None → parse_result 抛错 → schema_retry
    llm.enqueue(_FakeResult(success=True, parsed=None))
    llm.enqueue(_FakeResult(success=True, parsed={"summary": "ok"}, usage={"input_tokens": 5}))
    invoker = _make_invoker(engine, writer, llm)
    result = await invoker.invoke(request=_request(), llm_task_profile=None, runtime_context=_ctx())
    assert result.value == {"summary": "ok"}
    # schema_retry 创建了第 2 个 snapshot
    assert len(engine.snapshot_ids) == 2
    assert result.snapshot_public_id == engine.snapshot_ids[-1]
    # attempts 含 schema_retry kind
    kinds = [a.kind for a in result.attempts]
    assert "schema_retry" in kinds


async def test_stream_completes_snapshot_and_yields_chunks():
    engine = _FakeEngine()
    writer = _FakeWriter()
    llm = _FakeLLMClient()
    invoker = _make_invoker(engine, writer, llm)

    chunks = [
        chunk
        async for chunk in invoker.stream(
            request=_request(),
            llm_task_profile="chat_profile",
            runtime_context=_ctx(),
        )
    ]

    assert chunks == ["stream ", "reply"]
    assert writer.transitions == [("sent", "cs_snap_1"), ("completed", "cs_snap_1")]
    assert writer.completed_commands[-1].actual_input_tokens == 123
    assert writer.completed_commands[-1].actual_output_tokens == 45
    assert llm.last_stream_profile == "chat_profile"
    assert llm.last_stream_user_content == "prompt"


async def test_invoker_bridge_generate_with_profile_passes_runtime_conversation_id():
    class _CaptureInvoker:
        def __init__(self):
            self.request = None

        async def invoke(self, *, request, llm_task_profile, runtime_context):
            self.request = request
            return type(
                "_Result",
                (),
                {
                    "value": {"ok": True},
                    "snapshot_public_id": "cs_1",
                    "attempts": (),
                    "token_usage": None,
                },
            )()

    invoker = _CaptureInvoker()
    bridge = ContextInvokerBridge(invoker=invoker, enabled=True).bind(
        user_id=1,
        call_site="chat.reply",
        task_id=456,
        runtime_context=type(
            "_RuntimeContext",
            (),
            {"conversation_internal_id": 189, "task_internal_id": 456},
        )(),
    )

    result = await bridge.generate_with_profile(None, "hello")

    assert result.success is True
    assert invoker.request.conversation_id == "189"
    assert invoker.request.task_id == "456"
