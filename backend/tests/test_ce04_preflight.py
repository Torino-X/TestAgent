"""CE-04 WP-3：ContextPreflightService 状态机 + engine 集成测试。

覆盖：
- flag 关 → PASS 直通零行为。
- TARGET → PASS。
- SOFT：有删除 → PRUNED；无删除 → PASS。
- HARD_COMPACT：prune 后低 → PRUNED；compactor 成功 → COMPACTED；失败 degraded。
- ABSOLUTE：compactor 成功 → COMPACTED；失败 → BLOCKED（retryable=false）。
- engine.compose 集成（preflight 默认关 → 输出不变）。
- business_provider_call_count=0 语义。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from datetime import datetime

import pytest

from app.context_engine.composer.composer import ContextComposer
from app.context_engine.compression.models import ContextCompactionResult
from app.context_engine.compression.preflight_service import (
    ContextPreflightService,
    _load_durable_conversation_candidates,
)
from app.context_engine.errors import ContextEngineFailure
from app.context_engine.models.context import ContextItem, ContextKind, ContextPlan, ContextRequest, SectionPlan
from app.context_engine.models.enums import (
    CompactionStatus,
    CompactionTriggerType,
    CompactionType,
    ContextPreflightAction,
    ContextTrust,
    SourceType,
)
from app.context_engine.models.profile import ContextBudget
from app.context_engine.models.selection import SelectedContextSet
from app.context_engine.planning.budget_calculator import ContextBudgetCalculator
from app.context_engine.profiles.registry import BASE_POLICY_LIGHT_DIALOG


def _item(item_id: str, kind: ContextKind, *, authority: int = 50, tokens: int = 100, section_id: str | None = None) -> ContextItem:
    md = {"section_id": section_id} if section_id else {}
    return ContextItem(
        item_id=item_id,
        kind=kind,
        source_type=SourceType.CONVERSATION,
        source_ref=item_id,
        content=f"{item_id} unique content {item_id}",
        authority=authority,
        estimated_tokens=tokens,
        trust=ContextTrust.UNTRUSTED_REFERENCE,
        metadata=md,
    )


def _budget(window: int = 10000, target: int = 1000) -> ContextBudget:
    usable = window
    return ContextBudget(
        model_context_window=window,
        target_input=target,
        soft_threshold=int(usable * 0.7),       # 7000
        hard_compact_threshold=int(usable * 0.85),  # 8500
        absolute_threshold=int(usable * 0.95),      # 9500
    )


def test_chat_waterlines_map_to_the_visible_warning_and_compression_bands() -> None:
    budget = ContextBudgetCalculator().calculate(
        model_context_window=200_000,
        policy=BASE_POLICY_LIGHT_DIALOG,
    )

    assert 129_000 <= budget.soft_threshold <= 131_000
    assert 159_000 <= budget.hard_compact_threshold <= 161_000
    assert budget.absolute_threshold > budget.hard_compact_threshold
    assert budget.conversation_compact_threshold == 120_000


def _plan(compression_policy: str = "conversation_compaction", budget: ContextBudget | None = None) -> ContextPlan:
    b = budget or _budget()
    return ContextPlan(
        profile_key="test_plan.review.v1",
        profile_version="v1",
        model_context_window=b.model_context_window,
        input_budget=b.target_input,
        output_reserve=b.output_reserve,
        runtime_reserve=b.runtime_reserve,
        safety_margin=b.safety_margin,
        conversation_compact_threshold=b.conversation_compact_threshold,
        section_plans={
            "memory": SectionPlan(kind=ContextKind.MEMORY, required=False, budget_tokens=1000),
        },
        compression_policy=compression_policy,
    )


def _request(**kw) -> ContextRequest:
    base = dict(user_id="1", call_site="test_plan.review", current_node="review_format")
    base.update(kw)
    return ContextRequest(**base)


class FakeCompactor:
    """测试 compactor：返回固定 tokens_after / summary_public_id，或抛错。"""

    compaction_type = CompactionType.CONVERSATION

    def __init__(
        self,
        *,
        tokens_after: int = 50,
        summary_id: str | None = "sum_1",
        summary_text: str | None = "compressed summary",
        error: Exception | None = None,
    ):
        self._tokens_after = tokens_after
        self._summary_id = summary_id
        self._summary_text = summary_text
        self._error = error
        self.calls = 0
        self.last_req = None

    async def compact(self, req, *, runtime_context=None) -> ContextCompactionResult:
        self.calls += 1
        self.last_req = req
        if self._error:
            raise self._error
        return ContextCompactionResult(
            run_public_id="run_1",
            summary_public_id=self._summary_id,
            summary_text=self._summary_text,
            summary_type="conversation",
            tokens_after=self._tokens_after,
            compression_ratio=self._tokens_after / req.tokens_before,
        )


def _selected(*items: ContextItem) -> SelectedContextSet:
    return SelectedContextSet(
        included=list(items),
        dropped=[],
        section_stats={},
        total_estimated_tokens=sum(i.estimated_tokens for i in items),
        locked_section_ids=[],
    )


def test_durable_rehydration_translates_public_conversation_id(monkeypatch):
    """The fallback must use the bridge's numeric identity for repository reads."""
    from app.context_engine.sources.conversation import ConversationSourceAdapter

    captured: dict[str, object] = {}

    async def collect(_self, request, _section, scope, *, runtime_context):
        captured["request_id"] = request.conversation_id
        captured["scope_id"] = scope.conversation_id
        captured["runtime"] = runtime_context
        return SimpleNamespace(attempted=True, degraded=False, items=[_item("durable", ContextKind.CONVERSATION)])

    monkeypatch.setattr(ConversationSourceAdapter, "collect", collect)
    runtime = SimpleNamespace(conversation_internal_id=901)
    items = pytest_sync_run(
        _load_durable_conversation_candidates,
        request=_request(conversation_id="conv_public_901"),
        runtime_context=runtime,
    )

    assert [item.item_id for item in items] == ["durable"]
    assert captured["request_id"] == "901"
    assert captured["scope_id"] == "901"
    assert captured["runtime"] is runtime


class TestPreflightDisabled:
    def test_disabled_passes_through(self):
        svc = ContextPreflightService(enabled=False)
        s = _selected(_item("m1", ContextKind.MEMORY, tokens=500))
        result = pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=s)
        assert result.status == CompactionStatus.PASS
        assert result.selected is s  # 直通，无副作用
        assert result.business_provider_call_count == 0


class TestPreflightStateMachine:
    def test_target_pass(self):
        svc = ContextPreflightService(enabled=True)
        s = _selected(_item("m1", ContextKind.MEMORY, tokens=100))
        result = pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=s)
        assert result.status == CompactionStatus.PASS

    def test_soft_no_delete_pass(self):
        svc = ContextPreflightService(enabled=True)
        # 单条 memory(7500>soft 7000) 无法 prune 更多 → PASS
        s = _selected(_item("m1", ContextKind.MEMORY, tokens=7500))
        result = pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=s)
        assert result.status == CompactionStatus.PASS

    def test_soft_is_advisory_and_preserves_the_selected_working_set(self):
        svc = ContextPreflightService(enabled=True)
        # Soft is the warning band. Destructive quota pruning here would
        # collapse a coherent working set before the 80% compression line.
        items = [_item(f"m{i}", ContextKind.MEMORY, tokens=1000, authority=i) for i in range(8)]
        s = _selected(*items)
        result = pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=s)
        assert result.status == CompactionStatus.PASS
        assert result.action == ContextPreflightAction.PASS
        assert result.selected is s
        assert result.tokens_after == result.tokens_before == 8000

    def test_hard_compact_compactor_success(self):
        compactor = FakeCompactor(tokens_after=200, summary_id="sum_1")
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        # 4 条 KNOWLEDGE ×2200=8800 ∈ [hard 8500, abs 9500)；prune 不降(4<5) → compactor → COMPACTED
        items = [_item(f"k{i}", ContextKind.KNOWLEDGE, tokens=2200, authority=i) for i in range(4)]
        s = _selected(*items)
        result = pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=s)
        assert result.status == CompactionStatus.COMPACTED
        assert result.compression_provider_call_count == 1
        assert result.business_provider_call_count == 0
        assert compactor.calls == 1

    def test_preserve_evidence_policy_compacts_conversation_but_keeps_evidence_verbatim(self):
        compactor = FakeCompactor(tokens_after=150, summary_id="sum_conversation")
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        evidence = _item(
            "requirement-evidence", ContextKind.EVIDENCE, tokens=5000
        ).model_copy(update={"metadata": {"locked": True}})
        conversation = _item(
            "old-conversation", ContextKind.CONVERSATION, tokens=4000
        ).model_copy(
            update={
                "metadata": {
                    "locked": True,
                    "retention_policy": "recent_complete_turns",
                }
            }
        )
        plan = _plan(compression_policy="preserve_evidence_and_sections")

        result = pytest_sync_run(
            svc.run,
            request=_request(),
            plan=plan,
            selected=_selected(evidence, conversation),
        )

        payload = compactor.last_req.source_payload["conversation"]
        assert "old-conversation" in payload
        assert "requirement-evidence" not in payload
        assert any(item.item_id == evidence.item_id for item in result.selected.included)
        assert any(item.source_type == SourceType.CONVERSATION_SUMMARY for item in result.selected.included)

    def test_proactive_chat_compaction_keeps_newest_twenty_complete_turns(self):
        compactor = FakeCompactor(tokens_after=300, summary_id="sum_proactive")
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        budget = _budget().model_copy(
            update={"conversation_compact_threshold": 10_000}
        )
        plan = _plan(compression_policy="preserve_goal", budget=budget)
        conversation = []
        for turn in range(24):
            conversation.extend(
                [
                    _item(
                        f"u{turn}",
                        ContextKind.CONVERSATION,
                        tokens=200,
                    ).model_copy(
                        update={
                            "metadata": {
                                "role": "user",
                                "sequence": turn * 2 + 1,
                                "message_id": turn * 2 + 1,
                            }
                        }
                    ),
                    _item(
                        f"a{turn}",
                        ContextKind.CONVERSATION,
                        tokens=200,
                    ).model_copy(
                        update={
                            "metadata": {
                                "role": "agent",
                                "sequence": turn * 2 + 2,
                                "message_id": turn * 2 + 2,
                            }
                        }
                    ),
                ]
            )
        evidence = _item("requirements", ContextKind.EVIDENCE, tokens=500)

        result = pytest_sync_run(
            svc.run,
            request=_request(),
            plan=plan,
            selected=_selected(*conversation, evidence),
        )

        assert result.status == CompactionStatus.COMPACTED
        assert compactor.calls == 1
        assert compactor.last_req.covered_message_start_id == 1
        assert compactor.last_req.covered_message_end_id == 8
        assert compactor.last_req.covered_message_count == 8
        assert "u0" in compactor.last_req.source_payload["conversation"]
        assert "u4" not in compactor.last_req.source_payload["conversation"]
        assert "requirements" not in compactor.last_req.source_payload["conversation"]
        raw_after = [
            item
            for item in result.selected.included
            if item.kind == ContextKind.CONVERSATION
            and item.source_type == SourceType.CONVERSATION
        ]
        assert len(raw_after) == 40
        assert {item.item_id for item in raw_after} == {
            f"{role}{turn}"
            for turn in range(4, 24)
            for role in ("u", "a")
        }
        assert any(item.item_id == "requirements" for item in result.selected.included)
        assert any(
            item.source_type == SourceType.CONVERSATION_SUMMARY
            for item in result.selected.included
        )

    def test_proactive_retention_rehydrates_durable_prefix_when_selection_is_tail_only(
        self, monkeypatch
    ):
        """A bounded normal selection must not prevent the 60% transition."""
        compactor = FakeCompactor(tokens_after=300, summary_id="sum_rehydrated")
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        budget = _budget().model_copy(update={"conversation_compact_threshold": 10_000})
        plan = _plan(compression_policy="preserve_goal", budget=budget)

        def turns(start: int, end: int) -> list[ContextItem]:
            items: list[ContextItem] = []
            for turn in range(start, end):
                items.extend([
                    _item(f"u{turn}", ContextKind.CONVERSATION, tokens=200).model_copy(
                        update={"metadata": {"role": "user", "message_id": turn * 2 + 1}}
                    ),
                    _item(f"a{turn}", ContextKind.CONVERSATION, tokens=200).model_copy(
                        update={"metadata": {"role": "agent", "message_id": turn * 2 + 2}}
                    ),
                ])
            return items

        durable = turns(0, 24)
        selected_tail = turns(4, 24)

        async def load_durable(*_args, **_kwargs):
            return durable

        monkeypatch.setattr(
            "app.context_engine.compression.preflight_service._load_durable_conversation_candidates",
            load_durable,
        )
        result = pytest_sync_run(
            svc.run,
            request=_request(conversation_id="1", conversation_ledger_tokens=10_000),
            plan=plan,
            selected=_selected(*selected_tail),
            runtime_context=object(),
        )

        assert result.status == CompactionStatus.COMPACTED
        assert compactor.calls == 1
        assert compactor.last_req.covered_message_start_id == 1
        assert compactor.last_req.covered_message_end_id == 8
        raw_after = [
            item.item_id
            for item in result.selected.included
            if item.source_type == SourceType.CONVERSATION
        ]
        assert raw_after == [
            f"{role}{turn}"
            for turn in range(4, 24)
            for role in ("u", "a")
        ]

    def test_proactive_quick_regrowth_uses_deep_tail_and_carries_prior_summary(self):
        """A second 60% crossing keeps twelve turns and re-summarizes history."""
        compactor = FakeCompactor(tokens_after=300, summary_id="sum_deep")
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        budget = _budget().model_copy(update={"conversation_compact_threshold": 10_000})
        plan = _plan(compression_policy="preserve_goal", budget=budget)
        prior_summary = _item("prior-summary", ContextKind.CONVERSATION, tokens=300).model_copy(
            update={
                "source_type": SourceType.CONVERSATION_SUMMARY,
                "content": "R001-R004 durable summary",
                "metadata": {
                    "covered_start": 1,
                    "covered_end": 8,
                    "message_count": 8,
                },
            }
        )
        conversation = []
        for turn in range(27):
            conversation.extend([
                _item(f"u{turn}", ContextKind.CONVERSATION, tokens=200).model_copy(
                    update={"metadata": {"role": "user", "message_id": turn * 2 + 1}}
                ),
                _item(f"a{turn}", ContextKind.CONVERSATION, tokens=200).model_copy(
                    update={"metadata": {"role": "agent", "message_id": turn * 2 + 2}}
                ),
            ])

        result = pytest_sync_run(
            svc.run,
            request=_request(),
            plan=plan,
            selected=_selected(prior_summary, *conversation),
        )

        assert result.status == CompactionStatus.COMPACTED
        assert compactor.last_req.policy_key == "conversation-retention.deep.v2"
        assert compactor.last_req.policy_version == "v2"
        assert compactor.last_req.covered_message_start_id == 1
        assert compactor.last_req.covered_message_end_id == 30
        assert compactor.last_req.covered_message_count == 38
        payload = compactor.last_req.source_payload["conversation"]
        assert "R001-R004 durable summary" in payload
        assert "u14" in payload
        assert "u15" not in payload
        raw_after = [
            item.item_id
            for item in result.selected.included
            if item.source_type == SourceType.CONVERSATION
        ]
        assert raw_after == [
            f"{role}{turn}"
            for turn in range(15, 27)
            for role in ("u", "a")
        ]

    def test_conversation_ledger_reaches_waterline_before_narrow_chat_projection(self):
        """The durable ledger, rather than a task-local snapshot, owns 50%."""
        compactor = FakeCompactor(tokens_after=20, summary_id="sum_ledger")
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        budget = _budget().model_copy(update={"conversation_compact_threshold": 10_000})
        plan = _plan(compression_policy="preserve_goal", budget=budget)
        turns = [
            _item(f"u{index}", ContextKind.CONVERSATION, tokens=10).model_copy(
                update={"metadata": {"role": "user", "message_id": index + 1}}
            )
            for index in range(21)
        ]

        result = pytest_sync_run(
            svc.run,
            request=_request(conversation_ledger_tokens=10_000),
            plan=plan,
            selected=_selected(*turns),
        )

        assert compactor.calls == 1
        assert result.status == CompactionStatus.COMPACTED

    def test_chat_reply_uses_durable_ledger_even_when_profile_is_overridden(self):
        """A chat call site must not skip 60% retention due to profile override."""
        compactor = FakeCompactor(tokens_after=20, summary_id="sum_chat_reply")
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        budget = _budget().model_copy(update={"conversation_compact_threshold": 10_000})
        plan = _plan(compression_policy="preserve_evidence_and_sections", budget=budget)
        turns = [
            _item(f"u{index}", ContextKind.CONVERSATION, tokens=10).model_copy(
                update={"metadata": {"role": "user", "message_id": index + 1}}
            )
            for index in range(21)
        ]

        result = pytest_sync_run(
            svc.run,
            request=_request(call_site="chat.reply", conversation_ledger_tokens=10_000),
            plan=plan,
            selected=_selected(*turns),
        )

        assert compactor.calls == 1
        assert result.status == CompactionStatus.COMPACTED

    def test_hard_compact_no_compactor_degraded_pruned(self):
        # 无 compactor：4×2300=9200 ∈ [8500,9500)；prune 不降且<abs → degraded PRUNED
        svc = ContextPreflightService(enabled=True, conversation_compactor=None)
        items = [_item(f"k{i}", ContextKind.KNOWLEDGE, tokens=2300, authority=i) for i in range(4)]
        s = _selected(*items)
        result = pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=s)
        assert result.status == CompactionStatus.PRUNED
        assert result.degraded is True

    def test_hard_compactor_failure_emits_safe_failure_diagnostics(self):
        compactor = FakeCompactor(error=RuntimeError("provider down"))
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        items = [_item(f"k{i}", ContextKind.KNOWLEDGE, tokens=2200, authority=i) for i in range(4)]
        result = pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=_selected(*items))

        assert result.status == CompactionStatus.PRUNED
        assert result.degraded is True
        assert result.compaction_attempted is True
        assert result.compaction_phase == "compactor_call"
        assert result.compaction_exception_type == "RuntimeError"

    def test_absolute_compactor_success(self):
        compactor = FakeCompactor(tokens_after=100, summary_id="sum_1")
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        # 4×2500=10000 >= absolute 9500；prune 不降 → compactor 压到 100 → COMPACTED
        items = [_item(f"k{i}", ContextKind.KNOWLEDGE, tokens=2500, authority=i) for i in range(4)]
        s = _selected(*items)
        result = pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=s)
        assert result.status == CompactionStatus.COMPACTED
        assert result.compression_provider_call_count == 1

    def test_absolute_compacts_after_prune_reduces_below_absolute(self):
        """Absolute must persist a compacted representation, not stop at prune."""
        compactor = FakeCompactor(tokens_after=100, summary_id="sum_after_prune")
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        # 6×1900=11400 >= absolute; the deterministic quota keeps five items
        # (9500), exactly at Absolute.  The Absolute contract still requires
        # the compression provider and a durable summary.
        items = [_item(f"k{i}", ContextKind.KNOWLEDGE, tokens=1900, authority=i) for i in range(6)]
        s = _selected(*items)
        result = pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=s)
        assert result.status == CompactionStatus.COMPACTED
        assert result.action == ContextPreflightAction.COMPACT
        assert result.tokens_before == 11400
        assert result.compression_provider_call_count == 1
        assert compactor.calls == 1

    def test_compaction_replaces_included_and_composed_prompt(self):
        compactor = FakeCompactor(
            tokens_after=120,
            summary_id="sum_real",
            summary_text="真实压缩后的对话摘要，保留关键事实和未决事项。",
        )
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        old_text = "OLD_LONG_CONVERSATION_SHOULD_NOT_REACH_PROMPT"
        items = [_item("sys", ContextKind.SYSTEM_RULES, tokens=100)] + [
            ContextItem(
                item_id=f"turn{i}",
                kind=ContextKind.CONVERSATION,
                source_type=SourceType.CONVERSATION,
                source_ref=f"msg_{i}",
                title="recent_turn",
                content=f"{old_text}_{i}",
                authority=60,
                priority=5,
                estimated_tokens=2500,
                trust=ContextTrust.UNTRUSTED_REFERENCE,
            )
            for i in range(4)
        ]
        s = _selected(*items)
        request = _request(current_user_message="继续回答当前问题")

        result = pytest_sync_run(svc.run, request=request, plan=_plan(), selected=s)

        assert result.status == CompactionStatus.COMPACTED
        assert compactor.last_req.source_payload["conversation"]
        assert old_text in compactor.last_req.source_payload["conversation"]
        assert all(old_text not in item.content for item in result.selected.included)
        assert any(
            item.source_type == SourceType.CONVERSATION_SUMMARY
            and item.content == "真实压缩后的对话摘要，保留关键事实和未决事项。"
            for item in result.selected.included
        )
        assert result.selected.total_estimated_tokens == sum(
            item.estimated_tokens for item in result.selected.included
        )

        composed = ContextComposer().compose(request, result.selected)
        assert old_text not in composed.prompt_text
        assert "真实压缩后的对话摘要" in composed.prompt_text

    def test_absolute_compactor_fail_blocks(self):
        compactor = FakeCompactor(error=RuntimeError("provider down"))
        svc = ContextPreflightService(enabled=True, conversation_compactor=compactor)
        items = [_item(f"k{i}", ContextKind.KNOWLEDGE, tokens=2500, authority=i) for i in range(4)]
        s = _selected(*items)
        with pytest.raises(ContextEngineFailure) as exc:
            pytest_sync_run(svc.run, request=_request(), plan=_plan(), selected=s)
        assert exc.value.error.code == "context.preflight.blocked"
        assert exc.value.error.retryable is False
        assert exc.value.error.safe_metadata["compaction_attempted"] is True
        assert exc.value.error.safe_metadata["compaction_phase"] == "compactor_call"
        assert exc.value.error.safe_metadata["compaction_exception_type"] == "RuntimeError"


_DUMMY_RT = object()


def pytest_sync_run(fn, **kwargs):
    """同步运行 async 测试辅助；未传 runtime_context 时用 dummy。"""
    import asyncio

    kwargs.setdefault("runtime_context", _DUMMY_RT)

    async def _run():
        return await fn(**kwargs)

    return asyncio.run(_run())


class TestEngineIntegration:
    def test_engine_without_preflight_unchanged(self):
        """preflight 默认关 → engine 行为不变（compose 正常）。"""
        from app.context_engine.feature_flags import ContextEngineFeatureFlags
        from app.context_engine.runtime import engine_factory

        # Verify factory defaults rather than the developer's active .env.
        with patch.object(
            engine_factory,
            "get_context_engine_flags",
            return_value=ContextEngineFeatureFlags(),
        ):
            engine = engine_factory.build_context_engine()
        assert engine._preflight is not None
        assert engine._preflight._enabled is False


def test_conversation_compactor_uses_explicit_bridge_request_contract():
    """RuntimeContext holds a Bridge, so automatic compaction must not use unbound profile calls."""
    import asyncio
    from types import SimpleNamespace

    from app.context_engine.compression.conversation_compactor import ConversationCompactor

    class _Bridge:
        def __init__(self):
            self.kwargs = None

        async def generate(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(value={"summary_text": "summary", "tokens_after": 12})

    bridge = _Bridge()
    request = SimpleNamespace(
        user_id=7,
        conversation_id=70,
        task_id=700,
        request_id="compact-1",
        source_payload={"conversation": "long conversation"},
        target_tokens=12,
    )
    runtime_context = SimpleNamespace(context_llm_invoker=bridge)

    result = asyncio.run(
        ConversationCompactor(session_factory=lambda: None)._generate_summary(
            request, runtime_context
        )
    )

    assert result == {"summary_text": "summary", "tokens_after": 12}
    assert bridge.kwargs["user_id"] == 7
    assert bridge.kwargs["conversation_id"] == 70
    assert bridge.kwargs["task_id"] == 700
    assert bridge.kwargs["call_site"] == "compression.conversation"


def test_conversation_compactor_bypasses_normal_context_invoker_for_full_source_payload():
    """Compaction must not recursively compose the same over-limit context it repairs."""
    import asyncio
    from types import SimpleNamespace

    from app.context_engine.compression.conversation_compactor import ConversationCompactor

    class _ProviderClient:
        def __init__(self):
            self.calls = []

        async def generate_with_profile(self, profile, user_content, **kwargs):
            self.calls.append((profile, user_content, kwargs))
            return SimpleNamespace(
                success=True,
                parsed={"summary_text": "compressed", "tokens_after": 12},
            )

    class _NormalContextInvoker:
        async def invoke(self, **kwargs):
            raise AssertionError("automatic compaction must not recursively call ContextEngine.invoke")

    provider = _ProviderClient()
    request = SimpleNamespace(
        user_id=7,
        conversation_id=70,
        task_id=None,
        request_id="compact-provider-only",
        source_payload={"conversation": "long conversation" * 20_000},
        target_tokens=12,
    )
    runtime_context = SimpleNamespace(
        llm_client=provider,
        context_llm_invoker=_NormalContextInvoker(),
    )

    result = asyncio.run(
        ConversationCompactor(session_factory=lambda: None)._generate_summary(
            request, runtime_context
        )
    )

    assert result == {"summary_text": "compressed", "tokens_after": 12}
    assert len(provider.calls) == 1


def test_compaction_failure_diagnostics_expose_only_numeric_database_code():
    from app.context_engine.compression.preflight_service import _safe_exception_code

    class _OriginalDatabaseError(Exception):
        def __init__(self):
            super().__init__(2003, "connection string or server detail must not be exposed")

    class _OperationalError(Exception):
        def __init__(self):
            self.orig = _OriginalDatabaseError()

    code = _safe_exception_code(_OperationalError())

    assert code == "db_2003"
    assert "connection" not in code
