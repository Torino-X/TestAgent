"""CE-04 整改 §六：Runtime Reliability 门禁（CE-04 特有部分）。

命令 §六 清单中，Schema/Context-Length Retry 由 test_context_engine_invoker 覆盖；
Artifact/AgentEvent Idempotency 由 test_artifact_event_idempotency 覆盖；
Cross-worker 由 test_dispatch 覆盖。本文件集中验证 CE-04 特有项：

- Compaction at-least-once：同 idempotency key 重放 no-op（provider 返回后重放）
- CompactionRun/ConversationSummary idempotency key 确定性
- ToolCall public_id UNIQUE（幂等载体，v3 经 checkpointer 天然保证）
- Late Worker Reject（CAS：completed 后不再 finalize）
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.context_engine.compression.conversation_compactor import (
    ConversationCompactor,
    _idempotency_key,
)
from app.context_engine.compression.models import ContextCompactionRequest
from app.context_engine.models.enums import (
    CompactionTriggerType,
    CompactionType,
    RecoveryMode,
)
from app.models.context_engine import ContextCompactionRun
from app.models.conversation_summary import ConversationSummary


class FakeInvoker:
    def __init__(self, *, summary_text: str | None = "压缩后的摘要", error: Exception | None = None):
        self.summary_text = summary_text
        self.error = error
        self.calls = 0

    async def generate_with_profile(self, profile, user_content: str, **kw):
        self.calls += 1
        if self.error:
            raise self.error
        if self.summary_text is None:
            return None

        class _R:
            parsed = self.summary_text
            success = True

        return _R()

    async def invoke(self, *, request=None, llm_task_profile=None, runtime_context=None):
        raw = await self.generate_with_profile(llm_task_profile, request.current_user_message or "")
        if raw is None:
            return None

        class _Result:
            value = self.summary_text

        return _Result()


class _FakeRT:
    def __init__(self, session_factory, invoker):
        self.session_factory = session_factory
        self.context_llm_invoker = invoker


def _req(*, user_id=1, conversation_id=100, digest="d" * 64) -> ContextCompactionRequest:
    return ContextCompactionRequest(
        request_id="req_1",
        user_id=user_id,
        conversation_id=conversation_id,
        call_site="compression.conversation",
        compaction_type=CompactionType.CONVERSATION,
        trigger=CompactionTriggerType.PREFLIGHT,
        policy_key="compression.conversation:v1",
        policy_version="v1",
        source_digest=digest,
        tokens_before=200,
        target_tokens=80,
        recovery_mode=RecoveryMode.SUMMARY_WITH_REFS,
        require_recovery_payload=True,
        source_payload={"conversation": "long conversation text" * 20},
    )


def _rt(session_factory, invoker):
    return _FakeRT(session_factory, invoker)


def _run_async(coro):
    return asyncio.run(coro)


def test_compaction_at_least_once_same_key_replay_noop(sqlite_session_factory):
    """at-least-once：同 idempotency key 重放（provider 返回后 checkpoint 前崩溃重跑）→ no-op。"""
    sf = sqlite_session_factory
    compactor = ConversationCompactor(session_factory=sf)
    req = _req()

    r1 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要"))))
    assert r1 is not None

    # 模拟重放：同一 req（同 source_digest）再次 compact → 幂等，不重复写
    r2 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要"))))

    async def _check():
        async with sf() as s:
            summaries = (await s.execute(select(ConversationSummary))).scalars().all()
            runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
            assert len(summaries) == 1
            assert len(runs) == 1
    _run_async(_check())


def test_compaction_idempotency_key_deterministic_and_unique(sqlite_session_factory):
    """CompactionRun idempotency key：确定性 + 区分不同 source_digest。"""
    req1 = _req(digest="a" * 64)
    req2 = _req(digest="b" * 64)
    k1a = _idempotency_key(req1)
    k1b = _idempotency_key(req1)
    k2 = _idempotency_key(req2)
    assert k1a == k1b  # 确定性
    assert k1a != k2   # 不同 digest → 不同 key


def test_toolcall_public_id_unique_constraint():
    """ToolCall.public_id 为 UNIQUE（幂等载体；v3 经 LangGraph checkpointer 幂等）。"""
    from app.models.tool_call import ToolCall

    assert ToolCall.__table__.columns["public_id"].unique is True


def test_late_worker_reject_via_cas(sqlite_session_factory):
    """Late Worker Reject：run 已 completed 后，同 key 再次提交被 CAS 拒绝。"""
    sf = sqlite_session_factory
    compactor = ConversationCompactor(session_factory=sf)
    req = _req()

    _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要A"))))
    # 第二次同 key（late worker）→ 幂等 no-op；不产生第二个 run/summary
    _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要B"))))

    async def _check():
        async with sf() as s:
            runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == "completed"
    _run_async(_check())


def test_provider_at_least_once_failure_no_duplicate_summary(sqlite_session_factory):
    """Provider at-least-once：provider 失败后重试不产生重复 active summary。"""
    sf = sqlite_session_factory
    compactor = ConversationCompactor(session_factory=sf)
    req = _req()

    # 第一次 provider 失败
    r1 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(error=RuntimeError("down")))))
    assert r1 is None
    # 第二次成功（同 key 重试）→ 应成功
    r2 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要"))))
    assert r2 is not None

    async def _check():
        async with sf() as s:
            summaries = (await s.execute(select(ConversationSummary))).scalars().all()
            # 只有 1 个 active summary（失败那次没产生 summary）
            assert len(summaries) == 1
            assert summaries[0].status == "active"
    _run_async(_check())
