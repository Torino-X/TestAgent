from app.agent_runtime._shared.public_narrative import (
    AgentObservation,
    AgentPublicUpdateDraft,
    PublicNarrativeEnvelope,
    observation_to_public_update,
    normalize_public_update,
)
from app.agent_runtime.feature_flags import AgentRuntimeFeatureFlags
from app.agent_runtime.preparation.schemas import AgentDecision
from app.agent_runtime.repair.schemas import RepairDecision
from app.agent_runtime.incremental.schemas import IncrementalDecision


def test_phase29b_narrative_is_disabled_by_default():
    assert AgentRuntimeFeatureFlags().phase29b_narrative_enabled is False


def test_public_update_has_bounded_fields_and_normalizes_to_envelope():
    draft = AgentPublicUpdateDraft(
        headline="解析需求文档完成",
        summary="识别出章节和表格结构。",
        impact="后续会按章节策略继续生成。",
        next_action="进入模板解析。",
        details=["章节数: 17", "表格数: 3"],
    )
    envelope = PublicNarrativeEnvelope(
        update_kind="agent_observation",
        agent_name="PreparationAgent",
        decision_id="prep-1",
        step_index=1,
        action="observe",
        tool_name="RequirementParserTool",
        route="continue",
        public_update=draft,
    )

    assert envelope.public_update.details == ["章节数: 17", "表格数: 3"]
    assert "tool_arguments" not in envelope.model_dump()


def test_invalid_or_oversized_public_update_is_dropped_without_retry_payload():
    assert normalize_public_update({"headline": 123}) is None
    result = normalize_public_update({
        "headline": "x" * 200,
        "summary": "s",
        "impact": "i",
        "next_action": "n",
    })
    assert result is not None
    assert len(result.headline) <= 80


def test_bare_headline_without_body_is_not_valid_narrative():
    # Phase 2.9B.3: 只有 headline、缺 summary/impact/next_action 的对象
    # 不构成合法叙事(否则前端只剩标题)。
    assert normalize_public_update({"headline": "只有标题"}) is None
    assert normalize_public_update({"headline": "只有标题", "summary": "s"}) is None
    assert normalize_public_update({
        "headline": "完整",
        "summary": "s",
        "impact": "i",
        "next_action": "n",
    }) is not None


def test_observation_contains_only_public_facts():
    observation = AgentObservation(
        tool_name="KnowledgeSearchTool",
        success=False,
        error_code="KB_EMPTY",
        summary_facts={"hit_count": 0},
        result_excerpt="未检索到匹配内容。",
        business_effect="将跳过知识库结果并继续。",
        retryable=False,
        source_event_id="evt-1",
    )
    assert observation.summary_facts["hit_count"] == 0
    assert "tool_arguments" not in observation.model_dump()


def test_all_dynamic_agent_decisions_accept_structured_public_updates():
    update = {
        "headline": "正在解析需求文档",
        "summary": "识别章节结构。",
        "impact": "影响后续生成。",
        "next_action": "进入下一步。",
        "details": ["章节数: 17"],
    }
    assert AgentDecision(
        action="call_tool",
        tool_name="RequirementParserTool",
        decision_summary="parse",
        action_reason="需要解析需求",
        observation_update=update,
        decision_update=update,
    ).decision_update.headline == "正在解析需求文档"
    assert RepairDecision(
        action="finish",
        decision_summary="review",
        observation_update=update,
        decision_update=update,
    ).observation_update.summary == "识别章节结构。"
    assert IncrementalDecision(
        action="ask_user",
        decision_summary="clarify",
        observation_update=update,
        decision_update=update,
    ).observation_update.headline == "正在解析需求文档"


def test_agent_decision_accepts_action_reason():
    d = AgentDecision(
        action="finish",
        decision_summary="done",
        action_reason="PRD与模板结构清晰,信息充分。",
    )
    assert d.action_reason == "PRD与模板结构清晰,信息充分。"
    assert d.action == "finish"


def test_agent_decision_rejects_too_long_action_reason():
    import pytest as _pytest

    with _pytest.raises(Exception):
        AgentDecision(
            action="finish",
            decision_summary="done",
            action_reason="x" * 201,
        )


def test_agent_decision_rejects_unknown_field():
    import pytest as _pytest

    with _pytest.raises(Exception):
        AgentDecision(
            action="finish",
            decision_summary="done",
            unknown_field="should reject",
        )


def test_observation_to_public_update_is_bounded_and_does_not_expose_private_fields():
    draft = observation_to_public_update(
        AgentObservation(
            tool_name="RequirementParserTool",
            success=True,
            result_excerpt="识别出 17 个章节。",
            business_effect="后续进入章节策略确认。",
            summary_facts={"chapter_count": 17, "secret": "sk-not-public"},
        )
    )
    assert draft.headline == "RequirementParserTool 完成"
    assert draft.summary == "识别出 17 个章节。"
    assert draft.impact == "后续进入章节策略确认。"
    assert "tool_arguments" not in draft.model_dump()


def test_observation_to_public_update_bounds_240_char_excerpt_to_public_summary():
    draft = observation_to_public_update(
        AgentObservation(
            tool_name="RepairAgent",
            success=False,
            result_excerpt="x" * 240,
            business_effect="后续继续导出。",
        )
    )

    assert len(draft.summary) <= 200
    assert len(draft.impact) <= 160
    assert draft.summary == "x" * 200


# ── Phase 2.9B §14 sanitizer (_clean) 测试 ──────────────────────────


from app.agent_runtime._shared.public_narrative import (  # noqa: E402
    _clean,
    validate_route_consistency,
)


def test_clean_redacts_bearer_token():
    src = "Use Bearer abcDEF1234567890xyz in header"
    out = _clean(src, 200)
    assert "abcDEF1234567890xyz" not in out
    assert "Bearer [redacted]" in out or "[redacted]" in out


def test_clean_redacts_sk_key():
    fake_key = "sk-" + "abcdef1234567890XYZ_secret"
    src = f"API key {fake_key}"
    out = _clean(src, 200)
    assert fake_key not in out
    assert "[redacted]" in out


def test_clean_redacts_absolute_path():
    src = "log at /home/user/secrets/x.txt and C:\\Users\\admin\\y.txt"
    out = _clean(src, 200)
    assert "/home/user/secrets" not in out
    assert "[path]" in out


def test_clean_preserves_normal_text():
    src = "测试方案生成完成,共 17 章,工具: TestPlanGeneratorTool"
    out = _clean(src, 200)
    assert "测试方案生成完成" in out
    assert "TestPlanGeneratorTool" in out


# ── Phase 2.9B §10.3 route_consistency 测试 ───────────────────────


def _draft(next_action: str = "") -> AgentPublicUpdateDraft:
    return AgentPublicUpdateDraft(
        headline="更新",
        summary="本轮进度摘要。",
        impact="影响后续步骤。",
        next_action=next_action,
    )


def test_validate_route_consistency_finish_must_not_call_tool():
    draft = _draft(next_action="下一步调用 TestPlanGeneratorTool")
    assert validate_route_consistency(
        draft, approved_tool_name=None, is_finish=True, is_fail=False,
    ) is False


def test_validate_route_consistency_finish_pure_text_passes():
    draft = _draft(next_action="进入准备阶段总结。")
    assert validate_route_consistency(
        draft, approved_tool_name=None, is_finish=True, is_fail=False,
    ) is True


def test_validate_route_consistency_fail_must_hide_stacktrace():
    draft = _draft(next_action="报错 Traceback (most recent call last)")
    assert validate_route_consistency(
        draft, approved_tool_name=None, is_finish=False, is_fail=True,
    ) is False


def test_validate_route_consistency_fail_user_facing_passes():
    draft = _draft(next_action="模型调用超时,资源不足。")
    assert validate_route_consistency(
        draft, approved_tool_name=None, is_finish=False, is_fail=True,
    ) is True


def test_validate_route_consistency_calltool_must_match_approved():
    draft = _draft(next_action="下一步调用 WordExportTool 生成文件。")
    assert validate_route_consistency(
        draft,
        approved_tool_name="ResultReviewTool",
        is_finish=False,
        is_fail=False,
    ) is False


def test_validate_route_consistency_calltool_target_text_passes():
    draft = _draft(next_action="本次审查基于 ResultReviewTool 的 issue 列表。")
    assert validate_route_consistency(
        draft,
        approved_tool_name="ResultReviewTool",
        is_finish=False,
        is_fail=False,
    ) is True


def test_build_narrative_envelope_drops_inconsistent_finish_draft():
    from app.agent_runtime._shared.public_narrative import build_narrative_envelope

    inconsistent = {
        "headline": "完成",
        "next_action": "下一步调用 TestPlanGeneratorTool",
    }
    out = build_narrative_envelope(
        update_kind="agent_decision",
        agent_name="PreparationAgent",
        decision_id="prep-2",
        step_index=2,
        action="finish",
        tool_name=None,
        route="finish",
        public_update=inconsistent,
        is_finish=True,
    )
    assert out is None
