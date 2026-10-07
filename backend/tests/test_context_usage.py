"""WP-BE-02/03：Context Usage 聚合 + Model Window Resolver 测试。

覆盖：
- 5 分类聚合（conversation_history / project_documents / task_context /
  user_memory / system_instructions）；
- 未知 ContextKind → 显式抛错（不允许 silent drop）；
- model window 解析优先级（user config > snapshot > unknown）；
- ContextUsageService：latest successful snapshot / unknown window / no snapshot。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.context_engine.models.enums import ContextKind
from app.services.context_usage_service import (
    ContextUsageData,
    ContextUsageService,
    UI_CATEGORIES,
    UnknownContextKindError,
    _bounded_preview_query,
    aggregate_section_stats,
    build_evidence_receipt,
    map_kind_to_ui_category,
    normalize_breakdown_to_used_tokens,
)


@pytest.mark.asyncio
async def test_ledger_usage_degrades_when_materialization_expires_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional usage card must not re-read an expired ORM conversation."""
    from app.services.conversation_context_ledger_service import (
        ConversationContextLedgerService,
    )

    class _ExpiringConversation:
        def __init__(self) -> None:
            self.read_count = 0

        @property
        def id(self) -> int:
            self.read_count += 1
            if self.read_count > 1:
                raise RuntimeError("conversation instance expired")
            return 475

    async def _failing_materialize(self, **kwargs):
        # Accessing the already-expired instance is what previously made the
        # error handler itself fail with an unhandled 500.
        _ = kwargs["conversation"].id
        raise RuntimeError("ledger flush failed")

    monkeypatch.setattr(
        ConversationContextLedgerService,
        "materialize",
        _failing_materialize,
    )
    conversation = _ExpiringConversation()

    result = await ContextUsageService(None)._ledger_usage(
        conv=conversation,
        conversation_public_id="conv_ledger",
        user_internal_id=1,
        model={"context_window_tokens": 200_000},
    )

    assert result is None
    assert conversation.read_count == 2


def test_preview_retrieval_query_is_bounded_but_keeps_head_and_tail() -> None:
    query = "HEAD" + ("x" * 3000) + "TAIL"

    bounded = _bounded_preview_query(query)

    assert len(bounded) < len(query)
    assert len(bounded) <= 2000
    assert bounded.startswith("HEAD")
    assert bounded.endswith("TAIL")
    assert "retrieval focus shortened" in bounded


# ── 5 分类聚合（纯函数）─────────────────────────────────────────────


def test_map_kind_to_ui_all_nine_kinds():
    """9 个 ContextKind 全部映射到 5 个 UI 分类。"""
    mapping = {
        ContextKind.CONVERSATION: "conversation_history",
        ContextKind.KNOWLEDGE: "project_documents",
        ContextKind.CURRENT_GOAL: "task_context",
        ContextKind.TASK_STATE: "task_context",
        ContextKind.EVIDENCE: "task_context",
        ContextKind.MEMORY: "user_memory",
        ContextKind.SYSTEM_RULES: "system_instructions",
        ContextKind.PROJECT_INSTRUCTIONS: "system_instructions",
        ContextKind.CALL_CONTRACT: "system_instructions",
    }
    for kind, expected in mapping.items():
        assert map_kind_to_ui_category(kind) == expected


def test_map_kind_unknown_raises():
    """未映射 kind → 显式抛错，不允许 silent drop。"""
    with pytest.raises(UnknownContextKindError):
        map_kind_to_ui_category("future_kind")


def test_aggregate_section_stats_merges_into_5_categories():
    stats = {
        "conversation": {"kind": "conversation", "estimated_tokens": 32000},
        "knowledge": {"kind": "knowledge", "estimated_tokens": 27000},
        "current_goal": {"kind": "current_goal", "estimated_tokens": 4000},
        "task_state": {"kind": "task_state", "estimated_tokens": 6000},
        "evidence": {"kind": "evidence", "estimated_tokens": 4000},
        "memory": {"kind": "memory", "estimated_tokens": 3000},
        "system_rules": {"kind": "system_rules", "estimated_tokens": 6000},
        "project_instructions": {"kind": "project_instructions", "estimated_tokens": 3000},
        "call_contract": {"kind": "call_contract", "estimated_tokens": 1000},
    }
    result = aggregate_section_stats(stats)
    assert set(result.keys()) == set(UI_CATEGORIES)
    assert result["conversation_history"] == 32000
    assert result["project_documents"] == 27000
    assert result["task_context"] == 4000 + 6000 + 4000
    assert result["user_memory"] == 3000
    assert result["system_instructions"] == 6000 + 3000 + 1000


def test_aggregate_empty_stats():
    result = aggregate_section_stats(None)
    assert set(result.keys()) == set(UI_CATEGORIES)
    assert all(v == 0 for v in result.values())


def test_evidence_receipt_exposes_safe_source_references_only():
    snapshot = SimpleNamespace(
        public_id="ctxsnap_123",
        call_site="test_plan.generate.outline",
        context_profile_key="test_plan.generator.v1",
        context_profile_version="1",
        included_refs_json=[
            {
                "kind": "evidence",
                "source_type": "task_document_evidence",
                "source_ref": "file_requirement_123",
                "item_id": "ignored_when_source_ref_exists",
            },
            {
                "kind": "conversation",
                "source_type": "conversation_message",
                "item_id": "msg_456",
            },
            {
                "kind": "evidence",
                "source_type": "task_document_evidence",
                "source_ref": "file_requirement_123",
            },
        ],
        dropped_refs_json=[{"reason": "budget"}],
    )

    receipt = build_evidence_receipt(snapshot)

    assert receipt["call_site"] == "test_plan.generate.outline"
    assert receipt["included_source_count"] == 2
    assert receipt["dropped_source_count"] == 1
    assert receipt["included_sources"] == [
        {
            "kind": "evidence",
            "source_type": "task_document_evidence",
            "reference": "file_requirement_123",
        },
        {
            "kind": "conversation",
            "source_type": "conversation_message",
            "reference": "msg_456",
        },
    ]


def test_normalize_breakdown_never_exceeds_used_tokens():
    breakdown = {
        "conversation_history": 2400,
        "project_documents": 0,
        "task_context": 6,
        "user_memory": 0,
        "system_instructions": 16,
    }

    result = normalize_breakdown_to_used_tokens(breakdown, 2300)

    assert sum(result.values()) == 2300
    assert result["conversation_history"] <= 2300
    assert result["task_context"] > 0
    assert result["system_instructions"] > 0


def test_normalize_breakdown_keeps_already_consistent_values():
    breakdown = {
        "conversation_history": 1800,
        "project_documents": 0,
        "task_context": 6,
        "user_memory": 0,
        "system_instructions": 16,
    }

    assert normalize_breakdown_to_used_tokens(breakdown, 2300) == breakdown


def test_aggregate_unknown_kind_raises():
    with pytest.raises(UnknownContextKindError):
        aggregate_section_stats(
            {"future": {"kind": "future_kind", "estimated_tokens": 100}}
        )


def test_context_usage_model_prefers_snapshot_window_when_config_has_no_window():
    service = ContextUsageService(session=None)
    model_cfg = SimpleNamespace(
        provider="unknown-provider",
        model_name="custom-compatible-model",
        context_window_tokens=None,
    )
    snapshot = SimpleNamespace(
        model_name_snapshot="qwen3.7-max",
        context_window_tokens=32_000,
    )

    model = service._resolve_model(model_cfg, snapshot=snapshot)

    assert model == {
        "name": "custom-compatible-model",
        "context_window_tokens": 32_000,
        "window_source": "snapshot",
    }


def test_context_usage_model_does_not_guess_registry_window():
    service = ContextUsageService(session=None)
    model_cfg = SimpleNamespace(
        provider="weird-provider",
        model_name="qwen3.7-max",
        context_window_tokens=None,
    )

    model = service._resolve_model(model_cfg, snapshot=None)

    assert model == {
        "name": "qwen3.7-max",
        "context_window_tokens": None,
        "window_source": "unknown",
    }


async def test_context_usage_no_snapshot_fallback_uses_conversation_estimate(monkeypatch):
    import app.services.context_usage_service as context_usage_module

    class _FakeConversationContextService:
        def __init__(self, session):
            self.session = session

        async def build_chat_context(self, **kwargs):
            return SimpleNamespace(estimated_tokens=2120)

    monkeypatch.setattr(
        context_usage_module,
        "ConversationContextService",
        _FakeConversationContextService,
    )
    service = ContextUsageService(session=object())

    data = await service._estimate_without_snapshot(
        conv=SimpleNamespace(id=189),
        conversation_public_id="conv_64422249",
        user_internal_id=1,
        model={
            "name": "qwen3.7-max",
            "context_window_tokens": 32_000,
            "window_source": "model_config",
        },
    )

    assert data is not None
    assert data.available is False
    assert data.usage["used_tokens"] == 2120
    assert data.usage["available_tokens"] == 32_000 - 2120
    assert data.usage["count_mode"] == "heuristic"
    assert data.breakdown["conversation_history"] == 2120


def test_context_usage_response_exposes_snapshot_provenance():
    data = ContextUsageData(
        conversation_public_id="conv_64422249",
        model={"name": "model", "context_window_tokens": 32_000, "window_source": "snapshot"},
        usage={"used_tokens": 100, "count_mode": "estimated"},
        breakdown={category: 0 for category in UI_CATEGORIES},
        compaction={"available": False, "recommended": False, "in_progress": False},
        as_of="2026-09-19T00:00:00+00:00",
        available=True,
        snapshot_public_id="ctxsnap_123",
    )

    assert data.to_dict()["available"] is True
    assert data.to_dict()["snapshot_public_id"] == "ctxsnap_123"


def test_context_usage_hides_receipts_without_server_debug_flag():
    data = ContextUsageData(
        conversation_public_id="conv_1",
        model={"name": "model", "context_window_tokens": 32_000, "window_source": "snapshot"},
        usage={"used_tokens": 100, "count_mode": "heuristic"},
        breakdown={category: 0 for category in UI_CATEGORIES},
        compaction={"available": False, "recommended": False, "in_progress": False},
        evidence_receipt={"snapshot_public_id": "cs_1"},
        recent_evidence_receipts=[{"snapshot_public_id": "cs_1"}],
    )

    payload = data.to_dict()
    assert payload["debug_details_enabled"] is False
    assert payload["evidence_receipt"] is None
    assert payload["recent_evidence_receipts"] is None


def test_context_usage_exposes_receipts_only_with_server_debug_flag():
    data = ContextUsageData(
        conversation_public_id="conv_1",
        model={"name": "model", "context_window_tokens": 32_000, "window_source": "snapshot"},
        usage={"used_tokens": 100, "count_mode": "heuristic"},
        breakdown={category: 0 for category in UI_CATEGORIES},
        compaction={"available": False, "recommended": False, "in_progress": False},
        debug_details_enabled=True,
        evidence_receipt={"snapshot_public_id": "cs_1"},
        recent_evidence_receipts=[{"snapshot_public_id": "cs_1"}],
    )

    payload = data.to_dict()
    assert payload["debug_details_enabled"] is True
    assert payload["evidence_receipt"] == {"snapshot_public_id": "cs_1"}


@pytest.mark.asyncio
async def test_context_usage_prefers_provider_actual_input_tokens(monkeypatch):
    service = ContextUsageService(session=object())
    snapshot = SimpleNamespace(
        public_id="cs_actual",
        estimated_input_tokens=180,
        actual_input_tokens=250,
        context_window_tokens=200_000,
        model_name_snapshot="qwen",
        section_stats_json={},
        target_input_tokens=None,
        input_budget_tokens=None,
        completed_at=None,
        created_at=None,
        included_refs_json=[],
        dropped_refs_json=[],
        id=1,
        user_id=1,
    )
    monkeypatch.setattr(service, "_load_conversation", lambda *_: _async_value(SimpleNamespace(id=3)))
    monkeypatch.setattr(service, "_load_latest_success_snapshot", lambda **_: _async_value(snapshot))
    monkeypatch.setattr(service, "_load_model_config", lambda *_: _async_value(None))
    monkeypatch.setattr(service, "_build_compaction_signal", lambda **_: _async_value({"available": False, "recommended": False, "in_progress": False}))
    monkeypatch.setattr(service, "_load_recent_task_success_snapshots", lambda *_: _async_value([]))

    data = await service.get_context_usage(conversation_public_id="conv_1", user_internal_id=1)

    assert data.usage["used_tokens"] == 250
    assert data.usage["count_mode"] == "exact"
    assert data.usage["estimated"] is False


@pytest.mark.asyncio
async def test_next_request_preview_uses_read_only_chat_assembly(monkeypatch):
    """The card must describe the next chat request, not the last snapshot."""
    service = ContextUsageService(session=object())
    observed = {}

    class _PreviewEngine:
        async def preview(self, request, *, runtime_context):
            observed["request"] = request
            observed["runtime_context"] = runtime_context
            return SimpleNamespace(
                estimated_input_tokens=3210,
                section_stats={
                    "recent_turns": {
                        "kind": "conversation",
                        "estimated_tokens": 2800,
                    },
                    "current_goal": {
                        "kind": "current_goal",
                        "estimated_tokens": 250,
                    },
                },
                preflight={"waterline": "soft", "status": "pruned"},
            )

    monkeypatch.setattr(service, "_load_model_config", lambda *_: _async_value(
        SimpleNamespace(model_name="qwen", context_window_tokens=200_000)
    ))
    monkeypatch.setattr(service, "_compaction_enabled", lambda: _async_value(True))
    monkeypatch.setattr(
        service,
        "_resolve_preview_focus",
        lambda **_: _async_value(("", None, None)),
    )

    data = await service._preview_next_request_usage(
        conv=SimpleNamespace(id=19, project_id=None),
        conversation_public_id="conv_preview",
        user_internal_id=7,
        context_engine=_PreviewEngine(),
        preview_message="请继续分析退款项目的上线风险",
        preview_attached_file_ids=["file_1"],
    )

    assert observed["request"].call_site == "chat.reply"
    assert observed["request"].current_user_message == "请继续分析退款项目的上线风险"
    assert observed["request"].attached_file_ids == ["file_1"]
    assert data.source == "next_request_preview"
    assert data.snapshot_public_id is None
    assert data.usage["used_tokens"] == 3210
    assert data.breakdown["conversation_history"] == 2800
    assert data.compaction["recommended"] is True


@pytest.mark.asyncio
async def test_idle_preview_reuses_the_persisted_focus_exactly_once(monkeypatch):
    service = ContextUsageService(session=object())

    monkeypatch.setattr(
        service,
        "_resolve_preview_focus",
        lambda **_: _async_value(("继续核对退款发布门禁与回滚责任人", 903, "task_latest")),
    )

    async def _project_context(**kwargs):
        assert kwargs["query"] == "继续核对退款发布门禁与回滚责任人"
        assert kwargs["task_id"] == "task_latest"
        return None

    monkeypatch.setattr(service, "_resolve_preview_project_context", _project_context)

    class _PreviewEngine:
        async def preview(self, request, *, runtime_context):
            assert request.context_usage_baseline is True
            assert request.current_user_message == "继续核对退款发布门禁与回滚责任人"
            assert request.current_user_message_id == 903
            assert request.retrieval_query == "继续核对退款发布门禁与回滚责任人"
            assert request.task_id == "task_latest"
            return SimpleNamespace(estimated_input_tokens=80, section_stats={}, preflight=None)

    monkeypatch.setattr(service, "_load_model_config", lambda *_: _async_value(None))
    monkeypatch.setattr(service, "_compaction_enabled", lambda: _async_value(False))

    data = await service._preview_next_request_usage(
        conv=SimpleNamespace(id=20, project_id=None),
        conversation_public_id="conv_idle",
        user_internal_id=7,
        context_engine=_PreviewEngine(),
        preview_message="",
        preview_attached_file_ids=[],
    )

    assert data.source == "next_request_preview"
    assert data.usage["used_tokens"] == 80


@pytest.mark.asyncio
async def test_latest_usage_snapshot_ignores_background_memory_extraction(
    sqlite_session_factory,
):
    """The user-facing card must not be replaced by post-turn extraction."""
    from app.models.context_snapshot import ContextSnapshot

    session = sqlite_session_factory()
    session.add_all([
        ContextSnapshot(
            id=1,
            public_id="cs_chat",
            user_id=1,
            conversation_id=9,
            llm_task_type="chat",
            context_kind="active",
            call_site="chat.reply",
            status="completed",
        ),
        ContextSnapshot(
            id=2,
            public_id="cs_memory_extract",
            user_id=1,
            conversation_id=9,
            llm_task_type="memory",
            context_kind="active",
            call_site="memory.extract.user",
            status="completed",
        ),
    ])
    await session.commit()

    snapshot = await ContextUsageService(session)._load_latest_success_snapshot(
        conversation_id=9,
        user_internal_id=1,
    )

    assert snapshot is not None
    assert snapshot.public_id == "cs_chat"
    await session.close()


async def _async_value(value):
    return value
