"""CE-04 WP-4：Conversation Compaction 测试（三段事务 + 版本链 + payload）。

使用 Fake Invoker（generate_with_profile 协议）+ SQLite session。
覆盖：
- Prepare → Provider → Finalize 三段事务，run→completed。
- 新 summary active + 旧 active→superseded（版本链）。
- recovery payload 先写（Payload First），recovery_payload_id 落 run。
- idempotency key：同 key 已完成 → 幂等 no-op。
- 失败：provider error → run failed，旧 active 保持。
- Anchor 校验失败 → summary 不激活。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.context_engine.compression.anchor import ProtectedAnchorBuilder
from app.context_engine.compression.conversation_compactor import ConversationCompactor, _idempotency_key
from app.context_engine.compression.models import ContextCompactionRequest
from app.context_engine.models.context import ContextRequest
from app.context_engine.models.enums import (
    CompactionStatus,
    CompactionTriggerType,
    CompactionType,
    RecoveryMode,
)
from app.models.context_engine import ContextCompactionRun, ContextPayload
from app.models.conversation_summary import ConversationSummary


class FakeInvoker:
    """Fake 压缩 Provider：支持 generate_with_profile 协议，可配置失败/摘要。"""

    def __init__(self, *, summary_text: str | None = "压缩后的摘要", tokens_after: int = 30, error: Exception | None = None):
        self.summary_text = summary_text
        self.tokens_after = tokens_after
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

    # 兼容 invoke 协议（ContextAwareLLMInvoker 返回 ContextualLLMResult 含 value）
    async def invoke(self, *, request=None, llm_task_profile=None, runtime_context=None):
        raw = await self.generate_with_profile(llm_task_profile, request.current_user_message or "")
        if raw is None:
            return None

        class _Result:
            value = self.summary_text

        return _Result()


class FlakyProfileInvoker:
    """Production-style profile client that succeeds after one safe failure."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate_with_profile(self, profile, user_content: str, **kw):
        self.calls += 1
        if self.calls == 1:
            class _Failed:
                success = False
                parsed = None
                error_type = "llm_error"
                error_message = "sensitive provider detail must not be persisted"

            return _Failed()

        class _Succeeded:
            success = True
            parsed = {"summary_text": "重试后摘要", "tokens_after": 25}
            error_type = None
            error_message = None

        return _Succeeded()


class FailedProfileInvoker:
    """Provider-result failure used to verify safe persisted diagnostics."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate_with_profile(self, profile, user_content: str, **kw):
        self.calls += 1

        class _Failed:
            success = False
            parsed = None
            error_type = "parse_error"
            error_message = "sensitive provider response must not be persisted"

        return _Failed()


class _FakeRT:
    """带 session_factory + context_llm_invoker 的假 RuntimeContext。"""

    def __init__(self, session_factory, invoker):
        self.session_factory = session_factory
        self.context_llm_invoker = invoker


def _req(*, user_id=1, conversation_id=100, tokens_before=200, target=80, require_payload=True, digest="d" * 64) -> ContextCompactionRequest:
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
        tokens_before=tokens_before,
        target_tokens=target,
        recovery_mode=RecoveryMode.SUMMARY_WITH_REFS,
        require_recovery_payload=require_payload,
        source_payload={"conversation": "long conversation text" * 20},
    )

def _rt(session_factory, invoker):
    return _FakeRT(session_factory, invoker)


def _run_async(coro):
    return asyncio.run(coro)


class TestCompactorThreePhase:
    async def _count_summaries(self, sf, conv_id):
        async with sf() as s:
            r = await s.execute(
                select(ConversationSummary).where(ConversationSummary.conversation_id == conv_id)
            )
            return list(r.scalars().all())

    async def _count_runs(self, sf, user_id):
        async with sf() as s:
            r = await s.execute(select(ContextCompactionRun).where(ContextCompactionRun.user_id == user_id))
            return list(r.scalars().all())

    def test_payload_first_and_version_chain(self, sqlite_session_factory):
        sf = sqlite_session_factory
        invoker = FakeInvoker()
        compactor = ConversationCompactor(session_factory=sf)
        req = _req()

        result = _run_async(compactor.compact(req, runtime_context=_rt(sf, invoker)))
        assert result is not None
        assert result.status == CompactionStatus.COMPACTED
        assert result.summary_public_id is not None
        assert result.recovery_payload_id is not None
        # 最终 ratio = tokens_after / tokens_before = 30/200 = 0.15
        assert result.compression_ratio < 0.5

        # payload 先写（recovery_manifest）
        async def _check():
            async with sf() as s:
                payloads = (await s.execute(select(ContextPayload))).scalars().all()
                assert len(payloads) == 1
                assert payloads[0].payload_type == "recovery_manifest"
                assert payloads[0].user_id == 1
                # summary 版本链：1 条 active
                summaries = (await s.execute(select(ConversationSummary))).scalars().all()
                assert len(summaries) == 1
                assert summaries[0].status == "active"
                assert summaries[0].summary_type == "conversation"
                # run completed
                runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
                assert len(runs) == 1
                assert runs[0].status == "completed"
                assert runs[0].recovery_payload_id == payloads[0].id
        _run_async(_check())

    def test_version_chain_supersedes_old(self, sqlite_session_factory):
        sf = sqlite_session_factory
        compactor = ConversationCompactor(session_factory=sf)
        req1 = _req(digest="aaa" + "b" * 61)
        req2 = _req(digest="ccc" + "d" * 61)  # 不同 digest → 新版本链

        r1 = _run_async(compactor.compact(req1, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要1"))))
        r2 = _run_async(compactor.compact(req2, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要2"))))
        assert r1 is not None and r2 is not None
        assert r1.summary_public_id != r2.summary_public_id

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(
                    select(ConversationSummary).where(ConversationSummary.conversation_id == 100).order_by(ConversationSummary.id)
                )).scalars().all()
                # 旧 active → superseded（旧行指向新行）
                assert summaries[0].status == "superseded"
                assert summaries[1].status == "active"
                assert summaries[0].supersedes_summary_id == summaries[1].id
        _run_async(_check())

    def test_idempotency_same_key_noop(self, sqlite_session_factory):
        sf = sqlite_session_factory
        compactor = ConversationCompactor(session_factory=sf)
        req = _req()
        r1 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker())))
        # 同 req（同 source_digest/idempotency key）重跑 → 幂等 no-op（不重复写 summary）
        r2 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker())))

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(select(ConversationSummary))).scalars().all()
                runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
                # 只有 1 summary + 1 run（幂等）
                assert len(summaries) == 1
                assert len(runs) == 1
        _run_async(_check())

    def test_provider_failure_run_failed(self, sqlite_session_factory):
        sf = sqlite_session_factory
        invoker = FakeInvoker(error=RuntimeError("provider down"))
        compactor = ConversationCompactor(session_factory=sf)
        req = _req()
        result = _run_async(compactor.compact(req, runtime_context=_rt(sf, invoker)))
        assert result is None

        async def _check():
            async with sf() as s:
                runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
                assert len(runs) == 1
                assert runs[0].status == "failed"
                assert runs[0].error_code == "compression.provider_error"
                assert runs[0].error_message == "provider_exception:RuntimeError"
                # 无 summary 被激活
                summaries = (await s.execute(select(ConversationSummary))).scalars().all()
                assert len(summaries) == 0
        _run_async(_check())

    def test_profile_generation_failure_retries_before_failing_compaction(self, sqlite_session_factory):
        sf = sqlite_session_factory
        provider = FlakyProfileInvoker()
        runtime = _FakeRT(sf, provider)
        runtime.llm_client = provider
        compactor = ConversationCompactor(session_factory=sf, max_attempts=2)

        result = _run_async(compactor.compact(_req(), runtime_context=runtime))

        assert result is not None
        assert result.status == CompactionStatus.COMPACTED
        assert result.summary_text == "重试后摘要"
        assert provider.calls == 2

    def test_profile_generation_failure_persists_safe_reason_after_retries(self, sqlite_session_factory):
        sf = sqlite_session_factory
        provider = FailedProfileInvoker()
        runtime = _FakeRT(sf, provider)
        runtime.llm_client = provider
        compactor = ConversationCompactor(session_factory=sf, max_attempts=2)

        result = _run_async(compactor.compact(_req(), runtime_context=runtime))

        assert result is None
        assert provider.calls == 2

        async def _check():
            async with sf() as session:
                runs = (await session.execute(select(ContextCompactionRun))).scalars().all()
                assert len(runs) == 1
                assert runs[0].error_code == "compression.generate_failed"
                assert runs[0].error_message == "provider_result:profile_parse_error"
                assert "sensitive provider response" not in (runs[0].error_message or "")

        _run_async(_check())

    def test_anchor_validation_failure_no_activate(self, sqlite_session_factory):
        sf = sqlite_session_factory
        # summary 不含 required anchor（"目标"/"goal" 都不出现）→ 校验失败
        invoker = FakeInvoker(summary_text="今日天气晴朗适合出行")
        compactor = ConversationCompactor(session_factory=sf)
        # 构造带 required anchor 的 req
        req = _req(digest="e" * 64)
        req = req.model_copy(update={
            "protected_anchors": [
                ProtectedAnchorBuilder().build(
                    ContextRequest(user_id="1", call_site="compression.conversation", state_ref={"task_goal": "生成测试计划"})
                )[0]
            ]
        })
        result = _run_async(compactor.compact(req, runtime_context=_rt(sf, invoker)))
        assert result is None

        async def _check():
            async with sf() as s:
                runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
                assert runs[0].status == "failed"
                assert "anchor" in (runs[0].error_code or "")
                summaries = (await s.execute(select(ConversationSummary))).scalars().all()
                assert len(summaries) == 0
        _run_async(_check())

    def test_idempotency_key_deterministic(self):
        req = _req()
        k1 = _idempotency_key(req)
        k2 = _idempotency_key(req)
        assert k1 == k2
        assert k1.startswith("compaction_")
        assert len(k1) == len("compaction_") + 24
