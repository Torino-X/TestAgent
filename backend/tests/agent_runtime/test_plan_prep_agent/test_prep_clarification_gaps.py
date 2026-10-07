"""LLM-owned clarification gaps in the PreparationAgent result and narration."""

from __future__ import annotations

import json

import pytest

from app.agent_runtime.preparation.agent_loop import _decision_public_update, run_preparation
from app.agent_runtime.preparation.prompt import build_preparation_prompt
from app.agent_runtime.preparation.schemas import AgentDecision
from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
    _build_clarification_cards,
)


def _ask_user_with_dynamic_gaps() -> str:
    return json.dumps(
        {
            "action": "ask_user",
            "tool_name": None,
            "tool_arguments": None,
            "action_reason": "检索后仍缺少会影响测试结论的项目约束。",
            "decision_summary": "需要用户补充两个动态识别出的需求缺口。",
            "public_update": "需要补充关键测试约束。",
            "clarification_gaps": [
                {
                    "field": "acceptance_criteria",
                    "description": "请补充准入准出的具体判据，例如通过率和遗留缺陷门槛。",
                    "severity": "high",
                },
                {
                    "field": "defect_workflow",
                    "description": "请补充缺陷等级定义、责任人和修复时限。",
                    "severity": "medium",
                },
            ],
            "decision_update": {
                "headline": "需要补充测试方案关键约束",
                "narrative_text": "检索后仍有两项会影响测试结论的信息需要补充。",
                "summary": "需要补充准入准出判据和缺陷流转规则。",
                "impact": "补充后才能确定测试结论的判定口径。",
                "next_action": "展示补充卡片并等待你的填写。",
                "details": [],
            },
            "expected_result": "记录待补充项，交由主图继续检索或展示补充卡片。",
            "confidence": 0.86,
        },
        ensure_ascii=False,
    )


def test_text_only_public_update_remains_model_authored_decision_narrative() -> None:
    decision = AgentDecision(
        action="finish",
        decision_summary="需求与模板已可用于下一步。",
        public_update="需求范围和模板结构已经核对完成，可以进入章节策略确认。",
        confidence=0.9,
    )

    update = _decision_public_update(decision)

    assert update["narrative_text"] == decision.public_update
    assert update["next_action"] == "将进入章节处理策略确认。"


def test_call_tool_allows_null_clarification_gaps_as_empty_list() -> None:
    decision = AgentDecision.model_validate(
        {
            "action": "call_tool",
            "tool_name": "KnowledgeSearchTool",
            "tool_arguments": {"query": "支付回调异常规则"},
            "decision_summary": "需要检索项目资料和公司规则。",
            "clarification_gaps": None,
            "confidence": 0.7,
        }
    )

    assert decision.clarification_gaps == []


def test_prompt_requires_explicit_user_choice_for_unresolved_business_decisions() -> None:
    system_prompt, _ = build_preparation_prompt()

    assert "“待确认”只能用于标记风险，不能替代用户补充" in system_prompt
    assert "“保守范围”只能在用户已明确选择时使用" in system_prompt
    assert "### 观察" in system_prompt
    assert "完整问题只写入 clarification_gaps 和补充卡" in system_prompt


@pytest.mark.asyncio
async def test_explicit_unresolved_critical_requirements_cannot_silently_finish(
    monkeypatch, fake_llm, stub_adapter, runtime_ctx, base_state,
) -> None:
    async def _silent_finish(**_kwargs) -> AgentDecision:
        return AgentDecision(
            action="finish",
            decision_summary="可在方案中用待确认描述未定规则。",
            public_update="未决规则可按保守范围继续。",
            confidence=0.8,
        )

    monkeypatch.setattr(
        "app.agent_runtime.preparation.agent_loop._llm_decide", _silent_finish,
    )
    state = {
        **base_state,
        "requirement_summary": (
            "园区访客预约系统：适用范围尚未确定；审批规则和超时处理未明确；"
            "预约容量待确认；身份核验方式未确定；数据隐私规则未明确；"
            "验收与准出标准待确认。"
        ),
        "retrieval_evidence_bundle": {
            "company_rag": {"status": "skipped", "hits": []},
            "project_rag": {"status": "skipped", "hits": []},
        },
    }

    result = await run_preparation(
        state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.information_sufficient is False
    assert [gap.field for gap in result.requirement_gaps] == [
        "scope_and_capacity",
        "approval_and_verification",
        "privacy_and_acceptance",
    ]
    assert all(gap.severity == "high" for gap in result.requirement_gaps)


def test_only_scope_card_offers_explicit_conservative_scope_choice() -> None:
    cards = _build_clarification_cards(
        {
            "preparation_result": {
                "requirement_gaps": [
                    {
                        "field": "scope_and_capacity",
                        "description": "请确认适用对象与预约容量。",
                        "severity": "high",
                    },
                    {
                        "field": "privacy_and_acceptance",
                        "description": "请确认隐私规则与验收标准。",
                        "severity": "high",
                    },
                ]
            }
        }
    )

    assert [card["allow_conservative_scope"] for card in cards] == [True, False]


def test_clarification_cards_preserve_model_choice_options() -> None:
    cards = _build_clarification_cards(
        {
            "preparation_result": {
                "requirement_gaps": [
                    {
                        "field": "approval_rule",
                        "description": "请确认预约审批规则。",
                        "severity": "high",
                        "selection_mode": "single",
                        "options": [
                            {
                                "id": "front_desk",
                                "label": "前台审批",
                                "description": "由前台在预约到访前完成审批。",
                            },
                            {
                                "id": "security",
                                "label": "安保审批",
                                "description": "由安保人员确认访客到访资格。",
                            },
                        ],
                    }
                ]
            }
        }
    )

    assert cards[0]["selection_mode"] == "single"
    assert cards[0]["options"] == [
        {
            "id": "front_desk",
            "label": "前台审批",
            "description": "由前台在预约到访前完成审批。",
        },
        {
            "id": "security",
            "label": "安保审批",
            "description": "由安保人员确认访客到访资格。",
        },
    ]


@pytest.mark.asyncio
async def test_ask_user_preserves_llm_owned_gap_list_without_repeating_card_questions(
    monkeypatch, fake_llm, stub_adapter, runtime_ctx, base_state, in_memory_sink,
) -> None:
    captured_decisions = []

    async def _llm_decide_with_dynamic_gaps(**_kwargs) -> AgentDecision:
        return AgentDecision.model_validate_json(_ask_user_with_dynamic_gaps())

    async def _capture_decision_update(_self, **kwargs) -> None:
        captured_decisions.append(kwargs)

    monkeypatch.setattr(
        "app.agent_runtime.preparation.agent_loop._llm_decide",
        _llm_decide_with_dynamic_gaps,
    )
    monkeypatch.setattr(
        "app.agent_runtime.preparation.event_emitter.PreparationEventEmitter.emit_decision_update",
        _capture_decision_update,
    )

    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.information_sufficient is False
    assert [(gap.field, gap.severity) for gap in result.requirement_gaps] == [
        ("acceptance_criteria", "high"),
        ("defect_workflow", "medium"),
    ]
    assert [question.field for question in result.user_questions] == [
        "acceptance_criteria",
        "defect_workflow",
    ]

    assert captured_decisions
    update = captured_decisions[-1]["public_update"]
    assert update["details"] == []
    assert "### 观察" in update["narrative_text"]
    assert "### 下一步：需要你确认" in update["narrative_text"]
    assert "\n- " not in update["narrative_text"]
    assert "1. 已识别 2 项会影响测试结论的未决规则。" in update["narrative_text"]
    assert "请在下方补充卡中完成 2 项关键决策：" in update["narrative_text"]
    assert "1. acceptance criteria" in update["narrative_text"]
    assert "请补充准入准出的具体判据" not in update["narrative_text"]


@pytest.mark.asyncio
async def test_submitted_clarification_is_authoritative_for_current_task(
    monkeypatch, fake_llm, stub_adapter, runtime_ctx, base_state,
) -> None:
    """A submitted clarification cannot immediately open another card round."""

    async def _ask_again(**_kwargs) -> AgentDecision:
        return AgentDecision.model_validate_json(_ask_user_with_dynamic_gaps())

    monkeypatch.setattr(
        "app.agent_runtime.preparation.agent_loop._llm_decide", _ask_again,
    )
    state = {
        **base_state,
        "clarification_answers": {
            "answers": {
                "scope_and_capacity": "仅覆盖单人访客；每日上限 200 人。",
                "approval_and_verification": "前台审批，超时挂起并提醒。",
                "privacy_and_acceptance": "保留 30 天；成功率不低于 99%。",
            },
            "conservative_gap_ids": [],
            "source": "user",
        },
    }

    result = await run_preparation(
        state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=runtime_ctx,
    )

    assert result.information_sufficient is True
    assert result.requirement_gaps == []
    assert result.user_questions == []
