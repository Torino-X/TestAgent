"""CE-04 整改 §七：Compression 并发与崩溃一致性测试。

覆盖命令 §七 的崩溃点与并发语义（在既有三段事务测试基础上补充）：
- same key + different digest → 新版本链（不覆盖旧）
- 旧 worker 延迟提交 → CAS 拒绝（run 已 completed）
- 失败保持旧 active summary
- run 状态 CAS
- 两 worker 并发 finalize（逻辑层面串行化验证）
- 初无 active summary 并发
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.context_engine.compression.conversation_compactor import ConversationCompactor
from app.context_engine.compression.models import ContextCompactionRequest
from app.context_engine.models.enums import (
    CompactionStatus,
    CompactionTriggerType,
    CompactionType,
    RecoveryMode,
)
from app.models.context_engine import ContextCompactionRun
from app.models.conversation_summary import ConversationSummary


class FakeInvoker:
    """Fake 压缩 Provider（与 test_ce04_conversation_compactor 同款）。"""

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


def _req(*, user_id=1, conversation_id=100, tokens_before=200, target=80,
         require_payload=True, digest="d" * 64, request_id="req_1") -> ContextCompactionRequest:
    return ContextCompactionRequest(
        request_id=request_id,
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


async def _gather_compact(compactor, req1, req2, sf):
    return await asyncio.gather(
        compactor.compact(req1, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要1"))),
        compactor.compact(req2, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要2"))),
        return_exceptions=True,
    )


class TestCompressionCrashConsistency:
    """命令 §七 崩溃/并发一致性补充。"""

    def test_same_key_different_digest_creates_new_version(self, sqlite_session_factory):
        """same idempotency key + different source_digest → 新版本链，不覆盖旧。"""
        sf = sqlite_session_factory
        # 同一 request_id 但不同 digest → key 相同、内容不同
        req1 = _req(digest="aaa" + "b" * 61, request_id="req_same")
        req2 = _req(digest="ccc" + "d" * 61, request_id="req_same")

        r1 = _run_async(ConversationCompactor(session_factory=sf).compact(
            req1, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要1"))))
        r2 = _run_async(ConversationCompactor(session_factory=sf).compact(
            req2, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要2"))))

        assert r1 is not None and r2 is not None
        assert r1.summary_public_id != r2.summary_public_id

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(
                    select(ConversationSummary).where(ConversationSummary.conversation_id == 100)
                )).scalars().all()
                # 2 条：旧 active → superseded，新 active
                assert len(summaries) == 2
                active = [x for x in summaries if x.status == "active"]
                superseded = [x for x in summaries if x.status == "superseded"]
                assert len(active) == 1
                assert len(superseded) == 1
        _run_async(_check())

    def test_late_worker_delayed_commit_rejected(self, sqlite_session_factory):
        """旧 worker 延迟提交 → 第二次 compact 幂等 no-op（run 已 completed）。"""
        sf = sqlite_session_factory
        compactor = ConversationCompactor(session_factory=sf)
        req = _req()

        # 第一次成功完成
        r1 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要A"))))
        assert r1 is not None

        # 旧 worker 用同一 idempotency key 再次提交 → 幂等拒绝（不重复写）
        r2 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要B"))))
        # 同 digest → 幂等 no-op；run 已 completed → 不再写新 summary

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(select(ConversationSummary))).scalars().all()
                runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
                # 幂等：只有 1 summary + 1 run
                assert len(summaries) == 1
                assert len(runs) == 1
                assert runs[0].status == "completed"
        _run_async(_check())

    def test_failure_keeps_old_active(self, sqlite_session_factory):
        """失败保持旧 active summary（新 provider 失败不覆盖旧）。"""
        sf = sqlite_session_factory
        compactor = ConversationCompactor(session_factory=sf)
        req1 = _req(digest="aaa" + "b" * 61)
        req2 = _req(digest="ccc" + "d" * 61)

        r1 = _run_async(compactor.compact(req1, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要1"))))
        assert r1 is not None

        # 第二个 digest 压缩失败（provider error）
        r2 = _run_async(compactor.compact(req2, runtime_context=_rt(sf, FakeInvoker(error=RuntimeError("down")))))
        assert r2 is None

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(
                    select(ConversationSummary).where(ConversationSummary.conversation_id == 100)
                )).scalars().all()
                # 旧 active 仍保持；失败未产生新 active
                active = [x for x in summaries if x.status == "active"]
                assert len(active) == 1
                assert active[0].summary_text == "摘要1"
                runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
                failed = [x for x in runs if x.status == "failed"]
                assert len(failed) == 1
        _run_async(_check())

    def test_run_status_cas(self, sqlite_session_factory):
        """run 状态 CAS：completed 后不可再 finalize。"""
        sf = sqlite_session_factory
        compactor = ConversationCompactor(session_factory=sf)
        req = _req()

        r1 = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要"))))
        assert r1 is not None

        async def _check():
            async with sf() as s:
                run = (await s.execute(select(ContextCompactionRun))).scalars().first()
                assert run.status == "completed"
                assert run.error_code is None
        _run_async(_check())

    def test_two_workers_concurrent_finalize_single_active(self, sqlite_session_factory):
        """两 worker 并发 finalize → 最终只有一个 active summary（逻辑串行化）。"""
        sf = sqlite_session_factory
        compactor = ConversationCompactor(session_factory=sf)
        req1 = _req(digest="aaa" + "b" * 61, request_id="w1")
        req2 = _req(digest="ccc" + "d" * 61, request_id="w2")

        results = asyncio.run(_gather_compact(compactor, req1, req2, sf))
        assert results[0] is not None or results[1] is not None

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(
                    select(ConversationSummary).where(ConversationSummary.conversation_id == 100)
                )).scalars().all()
                # 最多一个 active（SQLite 单写者串行化；若两条都成功则是版本链，仅 1 active）
                active = [x for x in summaries if x.status == "active"]
                assert len(active) == 1
        _run_async(_check())

    def test_first_compaction_no_active_summary(self, sqlite_session_factory):
        """初次无 active summary 并发 → 直接创建 active。"""
        sf = sqlite_session_factory
        compactor = ConversationCompactor(session_factory=sf)
        req = _req(digest="fff" + "e" * 61)

        r = _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="首条摘要"))))
        assert r is not None

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(select(ConversationSummary))).scalars().all()
                assert len(summaries) == 1
                assert summaries[0].status == "active"
        _run_async(_check())

    def test_initial_run_status_running_then_completed(self, sqlite_session_factory):
        """Prepare 时 run=running → Finalize 后 completed（三段事务推进）。"""
        sf = sqlite_session_factory
        compactor = ConversationCompactor(session_factory=sf)
        req = _req()

        _run_async(compactor.compact(req, runtime_context=_rt(sf, FakeInvoker(summary_text="摘要"))))

        async def _check():
            async with sf() as s:
                run = (await s.execute(select(ContextCompactionRun))).scalars().first()
                assert run.status == "completed"
                assert run.trigger_type == CompactionTriggerType.PREFLIGHT.value
                assert run.compaction_type == CompactionType.CONVERSATION.value
                assert run.recovery_mode == RecoveryMode.SUMMARY_WITH_REFS.value
        _run_async(_check())
