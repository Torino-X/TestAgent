"""Stage 4 HA contracts: composition constraints and tool-output governance."""

from __future__ import annotations

import json

import pytest

from app.context_engine.capabilities import CONTEXT_ENGINE_PRODUCT_CAPABILITIES
from app.context_engine.composer.composer import ContextComposer
from app.context_engine.composer.validator import ComposeValidator
from app.context_engine.errors import ContextEngineFailure
from app.context_engine.models.context import (
    ContextItem,
    ContextPlan,
    ContextRequest,
    SectionPlan,
)
from app.context_engine.models.enums import ContextKind, SourceType
from app.context_engine.models.selection import SelectedContextSet
from app.context_engine.models.source import LockedSection
from app.context_engine.models.tool_output import ToolOutputPolicy
from app.context_engine.planning.planner import ContextPlanner
from app.context_engine.runtime.context_engine import _apply_source_contracts
from app.context_engine.selection.quota import SourceQuotaEnforcer, SourceQuotaPolicy
from app.context_engine.tool_output import ToolOutputManager, TypedToolOutput


def _item(
    item_id: str,
    *,
    source_type: str,
    metadata: dict | None = None,
) -> ContextItem:
    return ContextItem(
        item_id=item_id,
        kind=ContextKind.EVIDENCE,
        source_type=source_type,
        content=f"content-{item_id}",
        authority=50,
        estimated_tokens=5,
        metadata=metadata or {},
    )


def _plan(*, absolute_threshold: int = 100) -> ContextPlan:
    return ContextPlan(
        profile_key="stage4",
        profile_version="v1",
        model_context_window=1000,
        input_budget=80,
        output_reserve=10,
        runtime_reserve=10,
        safety_margin=10,
        soft_threshold=60,
        hard_compact_threshold=80,
        absolute_threshold=absolute_threshold,
        section_plans={
            "evidence": SectionPlan(
                kind=ContextKind.EVIDENCE,
                required=False,
                budget_tokens=100,
                source_types=[SourceType.ARTIFACT],
            )
        },
    )


def test_source_types_and_global_quota_are_applied_before_selection():
    plan = _plan()
    by_section = {
        "evidence": [
            _item("allowed-1", source_type="artifact"),
            _item("wrong-source", source_type="memory"),
            _item("allowed-2", source_type="artifact"),
        ]
    }
    constrained, dropped = _apply_source_contracts(
        plan,
        by_section,
        quota_enforcer=SourceQuotaEnforcer(
            SourceQuotaPolicy(per_source_type_limit={"artifact": 1})
        ),
        locked_sections=[],
    )

    assert [item.item_id for item in constrained["evidence"]] == ["allowed-1"]
    assert {drop.item_id: drop.reason for drop in dropped} == {
        "wrong-source": "source_type_not_allowed",
        "allowed-2": "source_quota",
    }


def test_locked_business_section_id_still_routes_by_context_kind():
    request = ContextRequest(
        user_id="usr_1", call_site="stage4", current_user_message="hello"
    )
    locked = LockedSection(
        section_id="business-section-42", locked=True, authority="artifact", version="v1"
    )
    selected = SelectedContextSet(
        included=[
            _item(
                "locked",
                source_type="artifact",
                metadata={"section_id": "business-section-42"},
            )
        ],
        section_stats={"evidence": {"required": False, "included_count": 1}},
        total_estimated_tokens=5,
    )

    result = ContextComposer().compose(request, selected, locked_sections=[locked])

    assert any(message.section_id == "business-section-42" for message in result.messages)
    assert "不可修改" in result.prompt_text


def test_planner_carries_exact_budget_thresholds_into_plan():
    plan = ContextPlanner().plan(
        ContextRequest(user_id="usr_1", call_site="intent.recognize"),
        model_context_window=10_000,
    )

    assert plan.soft_threshold > 0
    assert plan.hard_compact_threshold > plan.soft_threshold
    assert plan.absolute_threshold > plan.hard_compact_threshold


def test_final_composed_prompt_is_checked_against_plan_absolute_threshold():
    selected = SelectedContextSet(
        included=[], section_stats={}, total_estimated_tokens=0
    )
    result = ContextComposer(output_reminder="x" * 600).compose(
        ContextRequest(user_id="usr_1", call_site="stage4"), selected
    )

    validation = ComposeValidator().validate(result, absolute_threshold=20)

    assert validation.failure_code == "context.preflight.absolute_exceeded"


@pytest.mark.asyncio
async def test_large_tool_output_without_durable_payload_fails_closed():
    manager = ToolOutputManager(None)
    policy = ToolOutputPolicy(inline_char_limit=4, head_chars=2, tail_chars=0)

    with pytest.raises(ContextEngineFailure) as exc_info:
        await manager.manage(
            1,
            "task_1",
            "tool_1",
            raw_output=TypedToolOutput(text="x" * 100),
            policy=policy,
        )

    assert exc_info.value.error.code == "context.tool_output.payload_unavailable"


@pytest.mark.asyncio
async def test_governed_prompt_text_never_exceeds_the_governed_preview():
    manager = ToolOutputManager(None)
    policy = ToolOutputPolicy(inline_char_limit=4, head_chars=3, tail_chars=2)
    output = await manager.manage(
        1,
        "task_1",
        "tool_1",
        raw_output=TypedToolOutput(text="abcdefgh"),
        policy=policy,
    )

    assert output.prompt_text == output.preview
    assert len(output.prompt_text or "") < output.output_char_count


def test_shadow_and_rehydrate_are_explicitly_not_production_exposed():
    assert CONTEXT_ENGINE_PRODUCT_CAPABILITIES["shadow"] == "internal_only"
    assert CONTEXT_ENGINE_PRODUCT_CAPABILITIES["rehydrate"] == "internal_only"


def test_tool_result_serialization_is_deterministic_and_non_mutating():
    from app.agent_runtime.tool_output_governance import serialize_tool_result

    envelope = {"success": True, "data": {"b": 2, "a": 1}}
    before = json.loads(json.dumps(envelope))
    serialized = serialize_tool_result(envelope)

    assert serialized == '{"data":{"a":1,"b":2},"success":true}'
    assert envelope == before


@pytest.mark.asyncio
async def test_production_tool_recorder_persists_safe_row_then_governs_output(monkeypatch):
    from datetime import datetime

    from app.agent_runtime.tool_output_governance import ProductionToolOutputRecorder

    captured: dict = {}

    class _Resolver:
        def evaluate(self, name):
            assert name == "CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED"
            return True

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def commit(self):
            captured["committed"] = True

    class _Repo:
        def __init__(self, session):
            self.session = session

        async def create(self, row):
            captured["row"] = row

    class _Manager:
        async def manage(self, user_id, task_public_id, tool_call_id, **kwargs):
            captured["managed"] = (
                user_id,
                task_public_id,
                tool_call_id,
                kwargs["raw_output"].text,
            )

    monkeypatch.setattr(
        "app.agent_runtime.tool_output_governance.ToolCallRepository", _Repo
    )
    recorder = ProductionToolOutputRecorder(
        session_factory=lambda: _Session(),
        manager=_Manager(),
        task_flag_resolver=_Resolver(),
        user_internal_id=1,
        conversation_internal_id=2,
        task_internal_id=3,
        task_public_id="task_public",
        clock=lambda: datetime(2026, 1, 1),
    )
    envelope = {
        "success": True,
        "tool_call_id": "tool-call-1",
        "duration_ms": 12,
        "data": {"secret_body": "kept only in governed output"},
    }
    await recorder(
        {
            "tool_name": "KnowledgeSearchTool",
            "inputs": {"query": "private", "api_key": "never persist"},
            "result": envelope,
        }
    )

    assert captured["committed"] is True
    assert captured["row"].input_summary_json == {"keys": ["api_key", "query"]}
    assert captured["row"].output_summary_json == {"success": True}
    assert captured["managed"][:3] == (1, "task_public", "tool-call-1")
    assert json.loads(captured["managed"][3]) == envelope


def test_production_runtime_factory_constructs_tool_governance_manager():
    from app.agent_runtime.production_runtime_context_factory import (
        ProductionRuntimeContextFactory,
    )

    factory = ProductionRuntimeContextFactory(
        session_factory=lambda: None,
        event_bus_provider=lambda: None,
    )

    assert isinstance(factory._tool_output_manager, ToolOutputManager)
