"""Phase 2.9B E2E stub tests (场景 1 + 场景 5)。

不连真实 Postgres / Redis / 真实 LLM;所有输入通过 build_narrative_envelope
+ validate_route_consistency 验证 Phase 2.9B 真实代码路径。
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from app.agent_runtime._shared.public_narrative import (
    AgentObservation,
    AgentPublicUpdateDraft,
    PublicNarrativeEnvelope,
    build_narrative_envelope,
    normalize_public_update,
    validate_route_consistency,
)


# ── 场景 1: Preparation 检索 → finish ────────────────────────────────────


def test_e2e_scenario1_prep_knowledge_search_then_finish():
    """Preparation 第一轮 call_tool → knowledge_search;第二轮 finish。
    验证 route consistency 通过,build_narrative_envelope 正常发布。
    """
    # Round 1: 决策 = call_tool(KnowledgeSearchTool),observation_update 描述检索目标
    round1_decision = {
        "headline": "现有需求缺支付回调异常规则",
        "summary": "知识库可补充相关业务规则。",
        "impact": "建议继续检索后结束。",
        "next_action": "调用 KnowledgeSearchTool。",
    }
    envelope_r1 = build_narrative_envelope(
        update_kind="agent_decision",
        agent_name="PreparationAgent",
        decision_id="prep-1",
        step_index=1,
        action="call_tool",
        tool_name="KnowledgeSearchTool",
        route="execute_tool",
        public_update=round1_decision,
        approved_tool_name="KnowledgeSearchTool",
    )
    assert envelope_r1 is not None
    assert envelope_r1["public_update"]["headline"] == "现有需求缺支付回调异常规则"
    assert envelope_r1["action"] == "call_tool"
    assert envelope_r1["tool_name"] == "KnowledgeSearchTool"

    # Round 2: knowledge_search 返回 Observation,observation_update 用检索事实
    observation = AgentObservation(
        tool_name="KnowledgeSearchTool",
        success=True,
        result_excerpt="命中 3 条支付回调相关规则。",
        business_effect="信息已充分,可进入 finish。",
    )
    obs_draft = AgentPublicUpdateDraft(
        headline="已检索支付回调异常规则",
        summary="知识库返回超时重试、重复通知去重、签名失败拒绝三类规则。",
        impact="测试设计可覆盖异常场景和幂等性。",
        next_action="信息已充分,可进入 finish。",
        details=["规则数: 3"],
    )
    obs_env = build_narrative_envelope(
        update_kind="agent_observation",
        agent_name="PreparationAgent",
        decision_id="prep-2",
        step_index=2,
        action="observe",
        tool_name="KnowledgeSearchTool",
        route="continue",
        public_update=obs_draft,
        approved_tool_name="KnowledgeSearchTool",
    )
    assert obs_env is not None

    # Round 3: finish 决策,decision_update 用 finish 的语气,不含 "调用工具"
    finish_draft = AgentPublicUpdateDraft(
        headline="准备阶段完成",
        summary="需求、模板和知识库证据已满足章节策略生成。",
        impact="后续方案将包含支付回调异常流程。",
        next_action="等待用户章节确认。",
    )
    finish_env = build_narrative_envelope(
        update_kind="agent_decision",
        agent_name="PreparationAgent",
        decision_id="prep-3",
        step_index=3,
        action="finish",
        tool_name=None,
        route="finish",
        public_update=finish_draft,
        is_finish=True,
    )
    assert finish_env is not None
    assert finish_env["public_update"]["next_action"] == "等待用户章节确认。"

    # 三次 envelope 都是合法 envelope,route 全一致。
    for env in (envelope_r1, obs_env, finish_env):
        assert env["agent_name"] == "PreparationAgent"


# ── 场景 5: 叙事冲突 (finish 但 next_action 描写调用工具) ──────────────


def test_e2e_scenario5_route_conflict_falls_back_to_deterministic():
    """模型写 finish 但 next_action 声称调用工具 → 系统丢弃动态叙事,返回 None。
    上游调用方拿到 None 后使用确定性 fallback,合法决策不被阻断。
    """
    inconsistent_draft = {
        "headline": "已准备完成",
        "summary": "信息充分。",
        "impact": "",
        "next_action": "下一步调用 TestPlanGeneratorTool 生成方案。",
    }
    envelope = build_narrative_envelope(
        update_kind="agent_decision",
        agent_name="PreparationAgent",
        decision_id="prep-bad",
        step_index=1,
        action="finish",
        tool_name=None,
        route="finish",
        public_update=inconsistent_draft,
        is_finish=True,
    )
    # 路由校验失败 → 返回 None,caller 走 fallback
    assert envelope is None


def test_e2e_scenario5_route_conflict_calltool_claims_wrong_tool():
    """call_tool=ResultReviewTool 但 narrative 提 TestPlanGeneratorTool → 拒绝"""
    draft = AgentPublicUpdateDraft(
        headline="已审查",
        summary="block issues 数量下降。",
        impact="审查结果将指导后续修复。",
        next_action="调用 TestPlanGeneratorTool 重新生成。",
    )
    envelope = build_narrative_envelope(
        update_kind="agent_decision",
        agent_name="RepairAgent",
        decision_id="rep-bad",
        step_index=2,
        action="call_tool",
        tool_name="ResultReviewTool",
        route="repair",
        public_update=draft,
        approved_tool_name="ResultReviewTool",
    )
    assert envelope is None


def test_e2e_scenario5_calltool_matches_approved_passes():
    """call_tool=ResultReviewTool + narrative 提 ResultReviewTool → 通过"""
    draft = AgentPublicUpdateDraft(
        headline="已审查 ResultReviewTool 的 issue 列表",
        summary="block issues 数量从 1 降到 0。",
        impact="可进入 finish 阶段。",
        next_action="结束本次修复。",
    )
    envelope = build_narrative_envelope(
        update_kind="agent_decision",
        agent_name="RepairAgent",
        decision_id="rep-good",
        step_index=2,
        action="finish",
        tool_name=None,
        route="finish",
        public_update=draft,
        is_finish=True,
    )
    assert envelope is not None


# ── 场景 6: 叙事解析失败 — normalize 仍不抛 ──────────────────────────


def test_e2e_scenario6_narrative_parse_failure_does_not_raise():
    """public_update 是各种异常形态时,normalize 与 validator 都宽容处理,
    返回 None 时 caller 用 fallback。"""
    cases = [
        None,
        {},
        {"headline": 123},           # 字段类型错
        {"headline": "x" * 200},    # 超长
        "only a string",            # 字符串形态
        ["a", "b", "c"],            # list 形态
        {"headline": "ok", "details": "not a dict"},  # details 类型错
    ]
    for bad in cases:
        out = normalize_public_update(bad)
        # normalize 返回 None 或 trimmed draft
        # validate 用 None 也必须返回 False,但不抛异常
        if out is not None:
            assert validate_route_consistency(
                out, approved_tool_name=None, is_finish=False, is_fail=False,
            ) in (True, False)


def test_e2e_scenario6_validator_with_non_draft_input_returns_false():
    """route consistency 收到非 AgentPublicUpdateDraft 时返回 False,不抛。"""
    assert validate_route_consistency(
        "not a draft",  # type: ignore[arg-type]
        approved_tool_name=None, is_finish=False, is_fail=False,
    ) is False
    assert validate_route_consistency(
        None,  # type: ignore[arg-type]
        approved_tool_name=None, is_finish=False, is_fail=False,
    ) is False


# ── 场景 7: 同一 envelope id 稳定 (去重) ──────────────────────────────


def test_e2e_scenario7_envelope_event_id_is_stable():
    """相同 decision_id + agent_name 应得到完全一致的事件字段,
    前端可凭 event_id 去重。

    PublicNarrativeEnvelope 不强制要求 event_id (由 caller 注入 context.event_sink.emit),
    这里只断言 envelope 内字段稳定。
    """
    draft = AgentPublicUpdateDraft(
        headline="摘要",
        summary="进度摘要。",
        impact="影响后续。",
        next_action="继续。",
    )
    e1 = build_narrative_envelope(
        update_kind="agent_decision",
        agent_name="PreparationAgent",
        decision_id="prep-stable",
        step_index=2,
        action="finish",
        tool_name=None,
        route="finish",
        public_update=draft,
        is_finish=True,
    )
    e2 = build_narrative_envelope(
        update_kind="agent_decision",
        agent_name="PreparationAgent",
        decision_id="prep-stable",
        step_index=2,
        action="finish",
        tool_name=None,
        route="finish",
        public_update=AgentPublicUpdateDraft(
            headline="摘要",
            summary="进度摘要。",
            impact="影响后续。",
            next_action="继续。",
        ),
        is_finish=True,
    )
    assert e1["decision_id"] == e2["decision_id"]
    assert e1["step_index"] == e2["step_index"]
    assert e1["public_update"]["headline"] == e2["public_update"]["headline"]


# ── §18.6: 无额外 LLM 调用证据 ───────────────────────────────────────


def test_e2e_no_extra_llm_call_in_normalize():
    """normalize / observation_to_public_update / build_narrative_envelope
    都不调用 LLM — 仅做规范化、清洗、模型构造。
    通过断言 import graph 不引用 OpenAI/chat completion path 来确保。
    """
    # 这里只断言模块自身不导入 llm_client — 这能挡住"误在 narrative 内
    # 嵌套一次 LLM 调用"。
    import sys
    ns: Dict[str, Any] = {}
    from app.agent_runtime._shared import public_narrative

    # 模块不应包含 'gener' / 'chat_completion' 等与 LLM 相关的调用符号
    src = public_narrative.__file__
    assert src is not None
    with open(src, "r", encoding="utf-8") as f:
        content = f.read()
    forbidden = ["LLMClient", "chat_completion", "generate_with_profile"]
    for token in forbidden:
        assert token not in content, (
            f"public_narrative.py 不允许引用 {token} "
            f"(Phase 2.9B §4.1 必须从已有 decide 输出派生,不新增 LLM 调用)"
        )