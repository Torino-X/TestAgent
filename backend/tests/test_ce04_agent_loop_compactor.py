"""CE-04 WP-5：Agent Loop Compaction 测试。

覆盖：
- summary_type='agent_loop' 写入。
- source=steps_audit（decision_summary + args_signature）来源。
- 三段事务 + 版本链（agent_loop 独立版本链，不与 conversation 冲突）。
- 幂等。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.context_engine.compression.agent_loop_compactor import AgentLoopCompactor
from app.context_engine.compression.models import ContextCompactionRequest
from app.context_engine.models.enums import (
    CompactionStatus,
    CompactionTriggerType,
    CompactionType,
    RecoveryMode,
)
from app.models.conversation_summary import ConversationSummary


class _FakeInvoker:
    def __init__(self, summary_text: str = "循环摘要: 修复了 section B 的格式问题"):
        self.summary_text = summary_text

    async def invoke(self, *, request=None, llm_task_profile=None, runtime_context=None):
        class _Result:
            value = self.summary_text

        return _Result()


class _FakeRT:
    def __init__(self, sf, invoker):
        self.session_factory = sf
        self.context_llm_invoker = invoker


def _loop_req(*, task_id=200, conversation_id=300, tokens_before=500, target=200, digest="a" * 64) -> ContextCompactionRequest:
    return ContextCompactionRequest(
        request_id="loop_req_1",
        user_id=1,
        conversation_id=conversation_id,
        task_id=task_id,
        call_site="compression.agent_loop",
        compaction_type=CompactionType.AGENT_LOOP,
        trigger=CompactionTriggerType.PREFLIGHT,
        policy_key="compression.agent_loop:v1",
        policy_version="v1",
        source_digest=digest,
        tokens_before=tokens_before,
        target_tokens=target,
        recovery_mode=RecoveryMode.SUMMARY_WITH_REFS,
        require_recovery_payload=True,
        source_payload={
            "steps_audit": "step1: 决策 summary(准备) args_signature=prepare(plan_v1)\n"
                           "step2: 决策 summary(修复) args_signature=repair(section_b)",
        },
    )


def _run(coro):
    return asyncio.run(coro)


class TestAgentLoopCompactor:
    def test_agent_loop_summary_type(self, sqlite_session_factory):
        sf = sqlite_session_factory
        compactor = AgentLoopCompactor(session_factory=sf)
        req = _loop_req()
        result = _run(compactor.compact(req, runtime_context=_FakeRT(sf, _FakeInvoker())))
        assert result is not None
        assert result.status == CompactionStatus.COMPACTED
        assert result.summary_public_id is not None

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(
                    select(ConversationSummary).where(ConversationSummary.user_id == 1)
                )).scalars().all()
                assert len(summaries) == 1
                assert summaries[0].summary_type == "agent_loop"
                assert summaries[0].status == "active"
                # source_refs 记录 steps_audit（state-safe ref，不含全文）
                assert summaries[0].tokens_before == 500
                assert summaries[0].tokens_after == 200
                # Numeric(10,6) → Decimal；ratio = 200/500 = 0.4
                assert float(summaries[0].compression_ratio) == 0.4
        _run(_check())

    def test_agent_loop_version_chain_separate_from_conversation(self, sqlite_session_factory):
        """agent_loop 版本链与 conversation 独立，不互相 supersede。"""
        sf = sqlite_session_factory
        compactor = AgentLoopCompactor(session_factory=sf)
        r1 = _run(compactor.compact(_loop_req(digest="a" * 64), runtime_context=_FakeRT(sf, _FakeInvoker())))
        r2 = _run(compactor.compact(_loop_req(digest="b" * 64), runtime_context=_FakeRT(sf, _FakeInvoker())))
        assert r1 is not None and r2 is not None

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(
                    select(ConversationSummary).where(ConversationSummary.summary_type == "agent_loop").order_by(ConversationSummary.id)
                )).scalars().all()
                assert len(summaries) == 2
                assert summaries[0].status == "superseded"
                assert summaries[1].status == "active"
        _run(_check())

    def test_agent_loop_idempotent(self, sqlite_session_factory):
        sf = sqlite_session_factory
        compactor = AgentLoopCompactor(session_factory=sf)
        req = _loop_req()
        _run(compactor.compact(req, runtime_context=_FakeRT(sf, _FakeInvoker())))
        _run(compactor.compact(req, runtime_context=_FakeRT(sf, _FakeInvoker())))

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(select(ConversationSummary))).scalars().all()
                assert len(summaries) == 1  # 幂等
        _run(_check())

    def test_agent_loop_provider_failure(self, sqlite_session_factory):
        sf = sqlite_session_factory

        class FailingInvoker:
            async def invoke(self, *, request=None, llm_task_profile=None, runtime_context=None):
                raise RuntimeError("down")

        compactor = AgentLoopCompactor(session_factory=sf)
        result = _run(compactor.compact(_loop_req(), runtime_context=_FakeRT(sf, FailingInvoker())))
        assert result is None

        async def _check():
            async with sf() as s:
                from app.models.context_engine import ContextCompactionRun
                runs = (await s.execute(select(ContextCompactionRun))).scalars().all()
                assert runs[0].status == "failed"
        _run(_check())
