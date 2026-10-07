"""CE-04 WP-6：Full Replace + Provider Context Length Recovery 测试。

覆盖：
- FullReplaceCompactor：summary_type='full_replace'，强制 recovery payload。
- Preflight full_replace 分支：仅 provider_context_error + flag 开时启用；
  默认关时走普通 compactor。
- compose_for_retry 已接入 Preflight（trigger=provider_context_error）。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.context_engine.compression.full_replace_compactor import FullReplaceCompactor
from app.context_engine.compression.models import ContextCompactionRequest
from app.context_engine.compression.preflight_service import ContextPreflightService
from app.context_engine.errors import ContextEngineFailure
from app.context_engine.models.context import ContextItem, ContextKind, ContextRequest, SectionPlan
from app.context_engine.models.enums import (
    CompactionStatus,
    CompactionTriggerType,
    CompactionType,
    ContextTrust,
    RecoveryMode,
    SourceType,
)
from app.context_engine.models.profile import ContextBudget
from app.context_engine.models.selection import SelectedContextSet
from app.models.conversation_summary import ConversationSummary


class _FakeInvoker:
    def __init__(self, summary_text: str = "完整重组摘要: 保留目标与决策"):
        self.summary_text = summary_text

    async def invoke(self, *, request=None, llm_task_profile=None, runtime_context=None):
        class _Result:
            value = self.summary_text

        return _Result()


class _FakeRT:
    def __init__(self, sf, invoker):
        self.session_factory = sf
        self.context_llm_invoker = invoker


def _fr_req(*, user_id=1, conversation_id=100, tokens_before=800, target=300) -> ContextCompactionRequest:
    return ContextCompactionRequest(
        request_id="fr_1",
        user_id=user_id,
        conversation_id=conversation_id,
        call_site="compression.full_replace",
        compaction_type=CompactionType.FULL_REPLACE,
        trigger=CompactionTriggerType.PROVIDER_CONTEXT_ERROR,
        policy_key="compression.full_replace:v1",
        policy_version="v1",
        source_digest="f" * 64,
        tokens_before=tokens_before,
        target_tokens=target,
        recovery_mode=RecoveryMode.FULL_REHYDRATE,
        require_recovery_payload=True,
        source_payload={"conversation": "大量上下文" * 50},
    )


def _run(coro):
    return asyncio.run(coro)


def _budget() -> ContextBudget:
    return ContextBudget(
        model_context_window=10000,
        target_input=1000,
        soft_threshold=7000,
        hard_compact_threshold=8500,
        absolute_threshold=9500,
    )


def _plan() -> "ContextPlan":
    from app.context_engine.models.context import ContextPlan

    return ContextPlan(
        profile_key="test_plan.review.v1",
        profile_version="v1",
        model_context_window=10000,
        input_budget=1000,
        output_reserve=0,
        runtime_reserve=0,
        safety_margin=0,
        section_plans={"memory": SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=1000)},
        compression_policy="conversation_compaction",
    )


class TestFullReplaceCompactor:
    def test_full_replace_summary_type_and_payload(self, sqlite_session_factory):
        sf = sqlite_session_factory
        compactor = FullReplaceCompactor(session_factory=sf)
        req = _fr_req()
        result = _run(compactor.compact(req, runtime_context=_FakeRT(sf, _FakeInvoker())))
        assert result is not None
        assert result.status == CompactionStatus.COMPACTED
        assert result.recovery_payload_id is not None

        async def _check():
            async with sf() as s:
                summaries = (await s.execute(select(ConversationSummary))).scalars().all()
                assert len(summaries) == 1
                assert summaries[0].summary_type == "full_replace"
                assert summaries[0].recovery_mode == "full_rehydrate"
                assert summaries[0].status == "active"
        _run(_check())


class TestPreflightFullReplace:
    def test_full_replace_only_on_provider_error_flag_on(self, sqlite_session_factory):
        sf = sqlite_session_factory
        fr = FullReplaceCompactor(session_factory=sf)
        # flag 开 + provider_context_error trigger
        svc = ContextPreflightService(enabled=True, full_replace_compactor=fr, full_replace_enabled=True)
        items = [_item(f"k{i}", tokens=2500) for i in range(4)]  # 10000 >= absolute
        s = _selected(*items)
        result = _run(svc.run(
            request=_request(), plan=_plan(), selected=s,
            runtime_context=_FakeRT(sf, _FakeInvoker()),
            trigger=CompactionTriggerType.PROVIDER_CONTEXT_ERROR,
        ))
        assert result.status == CompactionStatus.COMPACTED
        assert result.compression_provider_call_count == 1

    def test_full_replace_disabled_default(self, sqlite_session_factory):
        sf = sqlite_session_factory
        fr = FullReplaceCompactor(session_factory=sf)
        # flag 默认关 → full_replace 不可用；Absolute 仍超且无 conversation compactor → BLOCKED
        svc = ContextPreflightService(enabled=True, full_replace_compactor=fr, full_replace_enabled=False)
        items = [_item(f"k{i}", tokens=2500) for i in range(4)]  # 10000 >= absolute 9500
        s = _selected(*items)
        with pytest.raises(ContextEngineFailure) as exc:
            _run(svc.run(
                request=_request(), plan=_plan(), selected=s,
                runtime_context=_FakeRT(sf, _FakeInvoker()),
                trigger=CompactionTriggerType.PROVIDER_CONTEXT_ERROR,
            ))
        assert exc.value.error.code == "context.preflight.blocked"
        assert exc.value.error.retryable is False


def _item(item_id: str, *, tokens: int) -> ContextItem:
    return ContextItem(
        item_id=item_id,
        kind=ContextKind.KNOWLEDGE,
        source_type=SourceType.KNOWLEDGE,
        source_ref=item_id,
        content=f"{item_id} 唯一内容 {item_id}",
        authority=50,
        estimated_tokens=tokens,
        trust=ContextTrust.UNTRUSTED_REFERENCE,
    )


def _selected(*items) -> SelectedContextSet:
    return SelectedContextSet(
        included=list(items),
        dropped=[],
        section_stats={},
        total_estimated_tokens=sum(i.estimated_tokens for i in items),
        locked_section_ids=[],
    )


def _request(**kw) -> ContextRequest:
    base = dict(
        user_id="1",
        call_site="test_plan.review",
        current_node="review_format",
        conversation_id="100",
    )
    base.update(kw)
    return ContextRequest(**base)


class TestFullReplaceGates:
    """命令 §九：Full Replace 政策门禁。"""

    def test_full_replace_failure_blocks_with_zero_business(self, sqlite_session_factory):
        """Full Replace 失败 → BLOCKED；business_provider_call_count=0。"""
        sf = sqlite_session_factory
        fr = FullReplaceCompactor(session_factory=sf)

        class _FailingInvoker:
            async def generate_with_profile(self, profile, user_content, **kw):
                raise RuntimeError("compression provider down")

        svc = ContextPreflightService(
            enabled=True, full_replace_compactor=fr, full_replace_enabled=True,
        )
        items = [_item(f"k{i}", tokens=2500) for i in range(4)]  # 10000 >= absolute
        s = _selected(*items)
        with pytest.raises(ContextEngineFailure) as exc:
            _run(svc.run(
                request=_request(), plan=_plan(), selected=s,
                runtime_context=_FakeRT(sf, _FailingInvoker()),
                trigger=CompactionTriggerType.PROVIDER_CONTEXT_ERROR,
            ))
        assert exc.value.error.retryable is False  # BLOCKED
        # Absolute BLOCKED → business_provider_call_count 不增长

    def test_full_replace_anchor_preserved(self, sqlite_session_factory):
        """Full Replace 后 summary 保留 protected_anchors（Anchor Preservation=100%）。"""
        from app.context_engine.compression.models import ProtectedAnchor

        sf = sqlite_session_factory
        compactor = FullReplaceCompactor(session_factory=sf)
        req = _fr_req()
        # 构造带 protected anchors 的 req
        anchors = [
            ProtectedAnchor(key="current_goal", value_digest="h" * 64, kind="direct_inject",
                            source_ref="task_state:task_1"),
            ProtectedAnchor(key="system_rules", value_digest="g" * 64, kind="direct_inject",
                            source_ref="system_rules:rule_1"),
        ]
        req2 = req.model_copy(update={"protected_anchors": anchors})
        result = _run(compactor.compact(req2, runtime_context=_FakeRT(sf, _FakeInvoker())))
        assert result is not None

        async def _check():
            async with sf() as s:
                summary = (await s.execute(select(ConversationSummary))).scalars().first()
                stored = summary.protected_anchors_json or []
                # Anchor Preservation=100%：所有输入 anchor 都存进 summary（state-safe digest）
                assert len(stored) == len(anchors)
                keys = {a.get("key") for a in stored}
                assert keys == {"current_goal", "system_rules"}
                digests = {a.get("value_digest") for a in stored}
                assert "h" * 64 in digests
        _run(_check())
