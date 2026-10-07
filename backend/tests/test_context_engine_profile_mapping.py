"""CE-01 整改：ContextProfile Registry 全量 call-site 映射测试。

覆盖：
- 19 个内置 profile 全部注册且带 version（CE-01 18 + CE-02 pilot）；
- 19 个 call-site 全部映射到已注册 profile；
- 每个已知 LLMTaskProfile 有显式映射；
- 缺失映射 fail-fast；
- 重复注册拒绝；
- planner 委托 registry 解析。
"""

from __future__ import annotations

import pytest

from app.context_engine.errors import ContextEngineFailure
from app.context_engine.planning import ContextPlanner
from app.context_engine.profiles import (
    CALL_SITE_TO_PROFILE_KEY,
    LLMTASK_PROFILE_TO_CONTEXT_PROFILE,
    ContextProfileRegistry,
    get_default_profile_registry,
)
from app.context_engine.models import ContextRequest

REQUIRED_CALL_SITES = [
    "chat.reply",
    "document.qa",
    "intent.recognize",
    "test_plan.preparation.decide",
    "test_plan.section_suggest",
    "test_plan.requirement.extract",
    "test_plan.generate.outline",
    "test_plan.generate.batch",
    "test_plan.review",
    "test_plan.repair.plan",
    "test_plan.repair.regenerate",
    "test_plan.repair.review",
    "test_plan.incremental.diff",
    "test_plan.incremental.regenerate",
    "memory.extract.user",
    "memory.extract.workspace",
    "memory.extract.playbook",
    "compression.conversation",
    "compression.conversation.manual",
    "compression.agent_loop",
    "compression.evidence",
    "compression.full_replace",
    "ce.pilot.summarize",
    # WP-BE-09 fix 2026-08-18: word_export.project_title 走 chat.reply.v1
    # (轻量纯文本生成,与 conversation.title 同类)
    "word_export.project_title",
    "incremental.task_summary_narrative",
]


def test_all_required_call_sites_mapped():
    """所有必需 call-site 全部存在。"""
    missing = [cs for cs in REQUIRED_CALL_SITES if cs not in CALL_SITE_TO_PROFILE_KEY]
    assert not missing, f"缺失 call-site: {missing}"


def test_all_call_sites_resolve_to_registered_profiles():
    """每个 call-site 映射到的 profile 都已注册且带 version。"""
    registry = get_default_profile_registry()
    for call_site, key in CALL_SITE_TO_PROFILE_KEY.items():
        profile = registry.get(key)  # 缺失即抛
        assert profile.version, f"{key} 缺 version"


def test_registry_has_semantic_profile_set():
    registry = get_default_profile_registry()
    # The count is not the contract; the manual compaction path must remain
    # independently registered rather than silently sharing the automatic
    # compaction profile.
    assert "compression.conversation.manual.v1" in registry.keys()
    assert len(registry.keys()) >= 34


def test_registry_call_site_mappings_count():
    """call-site 映射总数(含 conversation.title / dynamic_agent.planner /
    word_export.project_title 等扩展点)。不做硬编码数字,断言 ≥ REQUIRED
    列表长度,允许后续新增 call-site 不破坏本测试。
    """
    registry = get_default_profile_registry()
    assert len(registry.call_site_keys()) >= len(REQUIRED_CALL_SITES)


def test_word_export_project_title_has_its_own_profile_contract():
    """Project-title inference must not inherit general chat context."""
    registry = get_default_profile_registry()
    profile = registry.get_for_call_site("word_export.project_title")
    assert profile.key == "word_export.project_title.v1"


def test_document_qa_has_a_conversation_evidence_only_contract():
    from app.context_engine.models.enums import ContextKind

    profile = get_default_profile_registry().get_for_call_site("document.qa")
    evidence = next(
        section for section in profile.required_sections
        if section.kind is ContextKind.EVIDENCE
    )
    assert profile.key == "document.qa.v1"
    assert evidence.required is True
    assert evidence.allow_retrieval is False
    assert evidence.max_budget_tokens == 0
    assert set(evidence.source_types) == {"parsed_document", "artifact"}
    assert profile.retrieval_policy == "none"


def test_repair_profiles_reserve_full_repair_instruction_budget():
    """A repair instruction must not be dropped before recovery can start."""
    from app.context_engine.models.enums import ContextKind

    registry = get_default_profile_registry()
    for call_site in (
        "test_plan.repair.plan",
        "test_plan.repair.regenerate",
        "test_plan.repair.review",
    ):
        profile = registry.get_for_call_site(call_site)
        goal = next(
            section for section in profile.required_sections
            if section.kind is ContextKind.CURRENT_GOAL
        )
        assert goal.max_budget_tokens >= 2500


def test_repair_regenerate_accepts_review_evidence_when_generation_is_truncated():
    """JSON truncation leaves review findings but no generated section body.

    The regenerate profile must therefore be able to compose its required
    Evidence section from ``review_result`` alone; otherwise selection aborts
    before the recovery tool can ask the model for the missing fields.
    """
    from app.context_engine.models.enums import ContextKind

    profile = get_default_profile_registry().get_for_call_site(
        "test_plan.repair.regenerate"
    )
    evidence = next(
        section for section in profile.required_sections
        if section.kind is ContextKind.EVIDENCE
    )

    assert "review_result" in set(evidence.source_types)


def test_every_llmtask_profile_has_mapping():
    """已知 LLMTaskProfile 全部有显式映射。"""
    known = {
        "CHAT", "INTENT", "TITLE", "TEST_PLAN", "SUMMARY", "PREPARATION",
        "REPAIR", "INCREMENTAL", "REQUIREMENT_EVIDENCE_EXTRACT", "TOOL_NARRATIVE_COMPOSER",
        "TASK_SUMMARY_NARRATIVE_COMPOSER", "NARRATIVE_SCHEMA_REPAIR",
    }
    missing = known - set(LLMTASK_PROFILE_TO_CONTEXT_PROFILE.keys())
    assert not missing, f"缺失 LLMTaskProfile 映射: {missing}"
    # TITLE 是标题生成，映射到 chat.reply（轻量）
    assert "TITLE" in LLMTASK_PROFILE_TO_CONTEXT_PROFILE


def test_registry_incomplete_mapping_fails_fast():
    """构造缺 profile 的映射 → 启动 validate 抛错。"""
    from app.context_engine.models import ContextProfile

    p = ContextProfile(key="only.one.v1", version="v1")
    with pytest.raises(ContextEngineFailure) as exc_info:
        ContextProfileRegistry(
            profiles=[p],
            call_site_map={"chat.reply": "chat.reply.v1", "missing.cs": "not.registered.v1"},
        )
    assert exc_info.value.error.code == "context.profile.incomplete_mapping"


def test_registry_duplicate_registration_rejected():
    from app.context_engine.models import ContextProfile

    registry = ContextProfileRegistry(profiles=[])
    registry.register(ContextProfile(key="dup", version="v1"))
    with pytest.raises(ValueError):
        registry.register(ContextProfile(key="dup", version="v1"))


def test_get_for_call_site_resolves():
    registry = get_default_profile_registry()
    profile = registry.get_for_call_site("test_plan.repair.review")
    assert profile.key == "test_plan.repair.review.v1"


def test_get_for_call_site_unknown_fails_fast():
    registry = get_default_profile_registry()
    with pytest.raises(ContextEngineFailure) as exc_info:
        registry.get_for_call_site("unknown.call.site")
    assert exc_info.value.error.code == "context.profile.no_call_site_mapping"


@pytest.mark.parametrize("call_site", REQUIRED_CALL_SITES)
def test_planner_resolves_each_call_site(call_site):
    """planner 对每个 call-site 都能解析（委托 registry）。"""
    planner = ContextPlanner()
    plan = planner.plan(ContextRequest(user_id="usr_1", call_site=call_site, model_context_window=100000))
    assert plan.profile_key == CALL_SITE_TO_PROFILE_KEY[call_site]
    expected_version = "v2" if call_site == "document.qa" else "v1"
    assert plan.profile_version == expected_version


def test_profile_keys_are_unique_per_llm_call_site():
    """Each production LLM call site owns its explicit context contract."""
    keys = set(CALL_SITE_TO_PROFILE_KEY.values())
    assert len(keys) == len(CALL_SITE_TO_PROFILE_KEY)
    assert all(
        profile_key == f"{call_site}.v1"
        for call_site, profile_key in CALL_SITE_TO_PROFILE_KEY.items()
    )

    # Multiple semantic profiles may share a budget policy.
    from app.context_engine.profiles.registry import (
        BASE_POLICY_STANDARD_GENERATION,
    )

    # 验证共享 policy 被多个 profile 复用
    import app.context_engine.profiles.registry as reg

    standard_profiles = [
        reg.TEST_PLAN_GENERATE_BATCH_PROFILE,
        reg.TEST_PLAN_GENERATE_OUTLINE_PROFILE,
        reg.TEST_PLAN_REVIEW_PROFILE,
    ]
    for p in standard_profiles:
        assert p.budget_policy is BASE_POLICY_STANDARD_GENERATION


def test_distinct_llm_request_contracts_have_distinct_context_profiles():
    """Non-chat model calls must not silently inherit chat.reply context.

    These call sites each carry a different output contract, source scope, or
    privacy boundary.  Sharing a budget-policy object is fine; sharing the
    generic chat input contract is not.
    """
    dedicated_call_sites = (
        "conversation.title",
        "file.understanding",
        "vision.image_analysis",
        "chat.image_reply",
        "vision.connection_probe",
        "completion.summary",
        "test_plan.tool_narrative",
        "test_plan.task_summary_narrative",
        "dynamic_agent.planner",
        "dynamic_agent.tool_narrative",
        "incremental.task_summary_narrative",
        "word_export.project_title",
    )
    for call_site in dedicated_call_sites:
        assert CALL_SITE_TO_PROFILE_KEY[call_site] == f"{call_site}.v1"


def test_test_plan_profiles_explicitly_select_relevance_ranked_memory():
    from app.context_engine.models.enums import ContextKind

    registry = get_default_profile_registry()
    for call_site in (
        "test_plan.generate.outline",
        "test_plan.generate.batch",
        "test_plan.review",
        "test_plan.repair.plan",
        "test_plan.repair.regenerate",
        "test_plan.repair.review",
    ):
        profile = registry.get_for_call_site(call_site)
        memory = next(
            section for section in profile.optional_sections
            if section.kind is ContextKind.MEMORY
        )
        assert set(memory.source_types) == {
            "user_memory", "workspace_memory", "agent_playbook"
        }
        assert memory.max_budget_tokens > 0
