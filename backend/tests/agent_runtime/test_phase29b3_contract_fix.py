"""Phase 2.9B.3 — Prompt/Schema 合同 + details 类型统一 + 内部错误隔离测试。

覆盖 docs/94 报告 §15 的后端测试清单:
  - AgentDecision 接受 action_reason;
  - action_reason 超长被拒;
  - 未知字段仍被 extra=forbid 拒绝;
  - Prompt 字段集合与 Schema 一致;
  - 完整 decision_update / observation_update 通过校验;
  - 空 summary/impact/next_action 不能伪装成成功动态叙事;
  - details 接受 string[]、不生成 dict;
  - EventEmitter 不公开 decision_summary 内部错误;
  - Schema 失败时生成安全 fallback(含完整五字段);
  - DB payload / event-list / SSE 保存完整五字段。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agent_runtime._shared.public_narrative import (
    AgentPublicUpdateDraft,
    build_narrative_envelope,
    deterministic_preparation_fallback,
    normalize_public_update,
    observation_to_public_update,
)
from app.agent_runtime.preparation.schemas import AgentDecision


def _full_update() -> dict:
    return {
        "headline": "正在检索知识库",
        "summary": "模板字段需要知识库确认。",
        "impact": "检索结果决定章节策略。",
        "next_action": "调用 KnowledgeSearchTool。",
        "details": ["字段: payment_callback"],
    }


# ── 1. action_reason 合同 ────────────────────────────────────────────────


def test_agent_decision_accepts_action_reason():
    d = AgentDecision(
        action="finish",
        decision_summary="done",
        action_reason="PRD与模板结构清晰,信息充分。",
    )
    assert d.action_reason == "PRD与模板结构清晰,信息充分。"
    assert d.action == "finish"


def test_agent_decision_rejects_too_long_action_reason():
    with pytest.raises(Exception):
        AgentDecision(
            action="finish",
            decision_summary="done",
            action_reason="x" * 201,
        )


def test_agent_decision_rejects_unknown_field():
    with pytest.raises(Exception):
        AgentDecision(
            action="finish",
            decision_summary="done",
            unknown_field="should reject",
        )


def test_action_reason_never_leaks_to_public_update():
    """action_reason 是内部字段,不应出现在 public narrative 输出中。"""
    d = AgentDecision(
        action="finish",
        decision_summary="done",
        action_reason="内部短句",
        decision_update=AgentPublicUpdateDraft(
            headline="准备阶段完成",
            summary="已评估信息。",
            impact="可进入下一步。",
            next_action="进入章节确认。",
        ),
    )
    env = build_narrative_envelope(
        update_kind="agent_decision",
        agent_name="PreparationAgent",
        decision_id="prep-1",
        step_index=0,
        action="finish",
        tool_name=None,
        route="finish",
        public_update=d.decision_update,
        is_finish=True,
    )
    assert env is not None
    assert "action_reason" not in json.dumps(env, ensure_ascii=False)
    assert "内部短句" not in json.dumps(env, ensure_ascii=False)


# ── 2. Prompt 字段集合与 Schema 一致 ────────────────────────────────────


def test_prompt_field_set_matches_schema_field_set():
    """Prompt 的输出契约字段列表必须与 AgentDecision schema 字段集合一致:
    Prompt 要求的字段 Schema 必须支持;Schema 正式字段不得遗漏于 Prompt。"""
    from app.agent_runtime.preparation.prompt import PREPARATION_SYSTEM_PROMPT
    from app.agent_runtime.preparation.schemas import AgentDecision as AD

    schema_fields = set(AD.model_fields.keys())
    # 必须包含 action_reason(生产 Prompt 明确要求)。
    assert "action_reason" in schema_fields

    prompt = PREPARATION_SYSTEM_PROMPT
    # Prompt 明确列出的字段都应被 schema 支持。
    for field in ("action", "action_reason", "tool_name", "tool_arguments",
                  "decision_summary", "public_update", "observation_update",
                  "decision_update", "expected_result", "confidence"):
        assert field in prompt, f"Prompt 缺失字段 {field}"
        assert field in schema_fields, f"Prompt 要求但 Schema 不支持: {field}"
    # Schema 正式字段不得遗漏于 Prompt 合同。
    for field in schema_fields:
        assert field in prompt, f"Schema 字段 {field} 遗漏于 Prompt 合同"


# ── 3. 完整 decision_update / observation_update 通过校验 ───────────────


def test_full_decision_update_passes_validation():
    d = AgentDecision(
        action="call_tool",
        tool_name="KnowledgeSearchTool",
        decision_summary="retrieve",
        action_reason="需要检索",
        decision_update=_full_update(),
    )
    assert d.decision_update is not None
    assert d.decision_update.headline == "正在检索知识库"
    assert d.decision_update.details == ["字段: payment_callback"]


def test_full_observation_update_passes_validation():
    d = AgentDecision(
        action="call_tool",
        tool_name="KnowledgeSearchTool",
        decision_summary="retrieve",
        action_reason="需要检索",
        observation_update=_full_update(),
    )
    assert d.observation_update is not None
    assert d.observation_update.summary == "模板字段需要知识库确认。"


# ── 4. 空正文不能伪装成成功动态叙事 ────────────────────────────────────


@pytest.mark.parametrize("missing_field", ["summary", "impact", "next_action"])
def test_missing_body_field_is_not_valid_narrative(missing_field):
    payload = _full_update()
    del payload[missing_field]
    assert normalize_public_update(payload) is None


def test_bare_headline_is_not_valid_narrative():
    assert normalize_public_update({"headline": "只有标题"}) is None
    assert normalize_public_update("只有标题的字符串") is None


# ── 5. details 类型: 接受 string[],不生成 dict ─────────────────────────


def test_details_accepts_string_list():
    draft = AgentPublicUpdateDraft(
        headline="h",
        summary="s",
        impact="i",
        next_action="n",
        details=["a", "  ", "b"],  # 空项应被清洗
    )
    assert draft.details == ["a", "  ", "b"]  # Pydantic 不自动清洗,清洗在 normalize


def test_normalize_cleans_details_string_list():
    out = normalize_public_update({**_full_update(), "details": ["a", "  ", "", "b"]})
    assert out is not None
    assert out.details == ["a", "b"]


def test_normalize_details_dict_to_string_list():
    """历史 dict details → 字符串数组。"""
    out = normalize_public_update({**_full_update(), "details": {"章节数": 43}})
    assert out is not None
    assert isinstance(out.details, list)
    assert any("章节数" in item for item in out.details)


def test_normalize_details_invalid_to_empty_list():
    out = normalize_public_update({**_full_update(), "details": None})
    assert out is not None
    assert out.details == []


def test_normalize_details_max_five_items():
    out = normalize_public_update({**_full_update(), "details": [str(i) for i in range(10)]})
    assert out is not None
    assert len(out.details) == 5


# ── 6. EventEmitter 不公开内部错误 + 安全 fallback ─────────────────────


class _Sink:
    def __init__(self):
        self.events = []

    async def emit(self, **kw):
        self.events.append(kw)
        return {"ok": True}


class _Ctx:
    def __init__(self, sink):
        self.task_internal_id = 90
        self.event_sink = sink


@pytest.mark.asyncio
async def test_emitter_does_not_leak_decision_summary(monkeypatch):
    """fail 决策且无 decision_update/observation_update 时,不得把
    decision_summary(可能含 Schema 错误)作为 headline。"""
    from app.agent_runtime.preparation.event_emitter import PreparationEventEmitter

    sink = _Sink()
    emitter = PreparationEventEmitter(_Ctx(sink))
    monkeypatch.setattr(
        "app.agent_runtime.preparation.event_emitter.get_feature_flags",
        lambda: SimpleNamespace(phase29b_narrative_enabled_for=lambda _n: True),
    )
    error_text = "AgentDecision schema 校验失败: 1 validation error ... action_reason ..."
    await emitter.emit_decision_update(
        decision_id="90:preparation:0",
        step_index=0,
        action="fail",
        tool_name=None,
        public_update=None,  # 无模型叙事
        failure_category="schema_validation_failed",
    )
    assert len(sink.events) == 1
    payload = sink.events[0]["payload"]
    # 不得泄漏内部错误文本。
    assert "schema 校验失败" not in json.dumps(payload, ensure_ascii=False)
    assert "action_reason" not in json.dumps(payload, ensure_ascii=False)
    assert "Traceback" not in json.dumps(payload, ensure_ascii=False)
    # 安全 fallback: 完整五字段 + 标记。
    pu = payload["public_update"]
    assert pu["headline"]
    assert pu["summary"]
    assert pu["impact"]
    assert pu["next_action"]
    assert isinstance(pu["details"], list)
    assert payload.get("fallback_used") is True
    assert payload.get("narrative_source") == "deterministic"
    assert payload.get("failure_category") == "schema_validation_failed"


@pytest.mark.asyncio
async def test_emitter_uses_model_narrative_when_valid(monkeypatch):
    """模型生成完整 decision_update 时,emitter 原样发布,不 fallback。"""
    from app.agent_runtime.preparation.event_emitter import PreparationEventEmitter

    sink = _Sink()
    emitter = PreparationEventEmitter(_Ctx(sink))
    monkeypatch.setattr(
        "app.agent_runtime.preparation.event_emitter.get_feature_flags",
        lambda: SimpleNamespace(phase29b_narrative_enabled_for=lambda _n: True),
    )
    await emitter.emit_decision_update(
        decision_id="90:preparation:0",
        step_index=0,
        action="finish",
        tool_name=None,
        public_update=_full_update(),
    )
    assert len(sink.events) == 1
    payload = sink.events[0]["payload"]
    assert payload["public_update"]["headline"] == "正在检索知识库"
    assert "fallback_used" not in payload


def test_deterministic_preparation_fallback_is_complete_and_safe():
    draft = deterministic_preparation_fallback()
    assert draft.headline == "准备阶段已使用默认策略"
    assert draft.summary
    assert draft.impact
    assert draft.next_action
    assert isinstance(draft.details, list)
    leaked = json.dumps(draft.model_dump(), ensure_ascii=False)
    for marker in ("schema", "Pydantic", "action_reason", "Traceback", "validation"):
        assert marker.lower() not in leaked.lower()


# ── 7. observation_to_public_update 产出完整五字段 ─────────────────────


def test_observation_to_public_update_full_contract():
    from app.agent_runtime._shared.public_narrative import AgentObservation

    draft = observation_to_public_update(
        AgentObservation(
            tool_name="KnowledgeSearchTool",
            success=True,
            result_excerpt="命中 3 条规则。",
            business_effect="信息充分。",
            summary_facts={"chapter_count": 17, "secret": "sk-not-public"},
        )
    )
    assert draft.headline == "KnowledgeSearchTool 完成"
    assert draft.summary == "命中 3 条规则。"
    assert draft.impact == "信息充分。"
    assert draft.next_action
    assert isinstance(draft.details, list)
    assert any("chapter_count" in item for item in draft.details)
    assert not any("sk-not-public" in item for item in draft.details)


def test_preparation_prompt_prefers_natural_narrative_text():
    """PreparationAgent public narrative should be natural text, not a fixed template."""
    from app.agent_runtime.preparation.prompt import PREPARATION_SYSTEM_PROMPT

    prompt = PREPARATION_SYSTEM_PROMPT
    assert "narrative_text" in prompt
    assert "Markdown" in prompt
    assert "不要输出 Markdown" not in prompt
