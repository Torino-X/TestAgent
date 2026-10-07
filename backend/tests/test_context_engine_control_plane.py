"""CE-01 控制平面组件单元测试。

覆盖：Profile Registry、Planner、Budget Calculator、Scope Resolver、
ModelCapabilityResolver、TokenCounter、Feature Flags。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.context_engine.adapters import ModelCapability
from app.context_engine.errors import ContextEngineError, ContextEngineFailure
from app.context_engine.feature_flags import (
    ContextEngineFeatureFlags,
    ContextFeatureFlag,
)
from app.context_engine.planning import (
    ContextBudgetCalculator,
    ContextPlanner,
    ModelCapabilityResolver,
    TokenCounter,
)
from app.common.token_estimator import estimate_tokens as _heuristic_estimate
from app.context_engine.planning.budget_calculator import _UNKNOWN_WINDOW_FLOOR
from app.context_engine.profiles import (
    ContextProfileRegistry,
    get_default_profile_registry,
)
from app.context_engine.scope import ContextScopeResolver, ScopeResolutionError
from app.context_engine.models import ContextRequest


# ── Profile Registry ──────────────────────────────────────────────


def test_default_registry_has_builtins():
    from app.context_engine.profiles.registry import CALL_SITE_TO_PROFILE_KEY

    registry = get_default_profile_registry()
    keys = registry.keys()
    assert "intent.recognize.v1" in keys
    assert "chat.reply.v1" in keys
    assert "test_plan.review.v1" in keys
    assert "test_plan.incremental.diff.v1" in keys
    assert "compression.evidence.v1" in keys
    assert "ce.pilot.summarize.v1" in keys  # CE-02 Pilot Profile
    # Profiles represent context-preparation strategies.  Multiple call
    # sites may intentionally share one strategy; full replacement adds the
    # one additional semantic profile beyond the historic base set.
    assert len(keys) == 21


def test_registry_get_missing_raises_context_error():
    registry = ContextProfileRegistry([])
    with pytest.raises(ContextEngineFailure) as exc_info:
        registry.get("unknown.profile")
    assert exc_info.value.error.code == "context.profile.not_found"


def test_registry_duplicate_registration_rejected():
    from app.context_engine.models import ContextProfile

    registry = ContextProfileRegistry([])
    p = ContextProfile(key="dup", version="v1")
    registry.register(p)
    with pytest.raises(ValueError):
        registry.register(p)


def test_default_registry_maps_conversation_title_to_chat_reply_profile():
    """WP-BE-09 回归：conversation.title 是 MIG_SUMMARY=true 时 MessageService
    实际调用的 call_site，必须注册到 CALL_SITE_TO_PROFILE_KEY（缺失 → compose
    fail-fast with context.profile.no_call_site_mapping，标题生成退化）。
    """
    registry = get_default_profile_registry()
    profile = registry.get_for_call_site("conversation.title")
    assert profile.key == "chat.reply.v1"


# ── Budget Calculator ─────────────────────────────────────────────


def test_default_registry_maps_dynamic_agent_planner_to_context_profile():
    registry = get_default_profile_registry()
    profile = registry.get_for_call_site("dynamic_agent.planner")
    assert profile.key == "ce.pilot.summarize.v1"


def test_budget_known_window():
    calc = ContextBudgetCalculator()
    budget = calc.calculate(model_context_window=200000)
    assert budget.usable_window == 200000 - int(200000 * 0.075)
    assert budget.target_input < budget.usable_window
    assert budget.valid is True


def test_budget_unknown_window_conservative():
    calc = ContextBudgetCalculator()
    budget = calc.calculate(model_context_window=None)
    assert budget.model_context_window == _UNKNOWN_WINDOW_FLOOR
    assert budget.valid is True


def test_budget_invalid_window():
    calc = ContextBudgetCalculator()
    with pytest.raises(ContextEngineFailure) as exc_info:
        calc.calculate(model_context_window=0)
    assert exc_info.value.error.code == "context.budget.invalid_window"


def test_budget_reserves_exceed_window():
    from app.context_engine.models import BudgetPolicy

    calc = ContextBudgetCalculator()
    with pytest.raises(ContextEngineFailure) as exc_info:
        calc.calculate(
            model_context_window=10000,
            policy=BudgetPolicy(output_reserve_tokens=50000),
        )
    assert exc_info.value.error.code == "context.budget.no_usable_window"


# ── Planner ───────────────────────────────────────────────────────


def test_planner_resolves_profile_by_call_site():
    planner = ContextPlanner()
    plan = planner.plan(ContextRequest(user_id="usr_1", call_site="intent.recognize", model_context_window=200000))
    assert plan.profile_key == "intent.recognize.v1"
    assert plan.input_budget > 0
    assert "current_goal" in plan.section_plans
    assert plan.section_plans["current_goal"].required is True


def test_chat_reply_current_goal_reaches_preflight_without_section_cap():
    """A long ordinary message must not fail selection before preflight."""
    planner = ContextPlanner()
    plan = planner.plan(
        ContextRequest(
            user_id="usr_1",
            call_site="chat.reply",
            current_user_message="x" * 12_013,
            model_context_window=128_000,
        )
    )

    current_goal = plan.section_plans["current_goal"]
    assert current_goal.required is True
    assert current_goal.budget_tokens == 0


def test_planner_respects_spec_required_false_for_intent_recognize_task_state():
    """WP-BE-09 回归：INTENT_RECOGNIZE_PROFILE 在 required_sections 中列出 TASK_STATE
    但 spec.required=False；Planner 必须尊重 spec.required 而非硬编码 True。

    背景：旧 Planner 对 required_sections 中所有 spec 都强制 required=True，导致
    普通 Chat 路径 task_id=None 时 TaskStateSourceAdapter 返回 no_task，
    compose 在 required_failure 处抛错，整条 Intent 链路 fail-fast。
    """
    planner = ContextPlanner()
    plan = planner.plan(
        ContextRequest(
            user_id="usr_1",
            call_site="intent.recognize",
            current_user_message="你好",
            model_context_window=200000,
        )
    )
    # TASK_STATE 在 INTENT_RECOGNIZE_PROFILE 的 required_sections 列表中
    # 但 spec.required=False → Planner 必须按 False 处理
    assert "task_state" in plan.section_plans
    assert plan.section_plans["task_state"].required is False, (
        "spec.required=False 必须被尊重（普通 Chat 无 task 是合法状态）"
    )
    # CURRENT_GOAL 仍应 required=True
    assert plan.section_plans["current_goal"].required is True


def test_intent_recognize_current_goal_is_not_section_capped_before_preflight():
    """Long normal-chat routing input must reach real preflight policy.

    Recent conversation context can make the compact routing representation
    exceed 1,500 tokens.  Because CURRENT_GOAL is required, a per-section cap
    would discard it and fail selection before intent routing can run.
    """
    from app.context_engine.models import ContextKind
    from app.context_engine.profiles.registry import INTENT_RECOGNIZE_PROFILE

    current_goal_spec = next(
        spec
        for spec in INTENT_RECOGNIZE_PROFILE.required_sections
        if spec.kind == ContextKind.CURRENT_GOAL
    )

    assert current_goal_spec.max_budget_tokens == 0


def test_intent_recognize_conversation_is_bounded_independently_from_chat_reply():
    """Routing must not inherit the full high-pressure chat working set.

    ``IntentRouter`` already embeds a bounded summary and recent-turn excerpt
    in CURRENT_GOAL.  A second unlimited Conversation section duplicates that
    history and can trigger Absolute compaction before routing has happened.
    """
    from app.context_engine.models import ContextKind
    from app.context_engine.profiles.registry import INTENT_RECOGNIZE_PROFILE

    conversation_spec = next(
        spec
        for spec in INTENT_RECOGNIZE_PROFILE.optional_sections
        if spec.kind == ContextKind.CONVERSATION
    )

    assert 0 < conversation_spec.max_budget_tokens <= 4_000


def test_deduplicator_resets_between_compose_calls():
    """WP-BE-09 回归：ContextDeduplicator 实例被 facade 复用，_seen_* 集合是
    进程级状态。若不重置，跨 compose 调用的相同 source_ref（如
    CURRENT_GOAL adapter 永远使用 ``current_user_message``）会被误判为
    duplicate，导致第二次及之后的 compose() 中 Required Section 变空 →
    selector 抛 ``context.selection.required_unmet``。

    测试验证 reset() 后，相同 identity_key 的 item 不再被视为重复。
    """
    from app.context_engine.selection.dedup import ContextDeduplicator
    from app.context_engine.models.context import ContextItem
    from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType

    dedup = ContextDeduplicator()
    items = [
        ContextItem(
            item_id="goal",
            kind=ContextKind.CURRENT_GOAL,
            source_type=SourceType.CONVERSATION,
            source_ref="current_user_message",
            content="用户消息：你好",
            authority=100,
            priority=100,
            estimated_tokens=3,
            trust=ContextTrust.TRUSTED_INSTRUCTION,
        )
    ]
    # 第一次：included
    inc1, _drop1 = dedup.dedup(items)
    assert len(inc1) == 1
    # 第二次：不重置 → 被误判为重复 → included=0
    inc2, _drop2 = dedup.dedup(items)
    assert len(inc2) == 0, "未 reset 的 dedup 会跨调用误判相同 source_ref"
    # 第三次：reset 后 → 重新 included
    dedup.reset()
    inc3, _drop3 = dedup.dedup(items)
    assert len(inc3) == 1, "reset() 后 dedup 恢复初始状态"


def test_mysql_nulls_last_does_not_emit_unsupported_syntax():
    """WP-BE-09 回归：MySQL 不支持 SQL 标准 ``NULLS LAST/FIRST`` 语法
    （仅 PostgreSQL/Oracle + MySQL 8.0.16+ window function）。

    旧实现 ``Column.desc().nulls_last()`` 在 MySQL 上生成
    ``ORDER BY ... DESC NULLS LAST`` → pymysql 1064 语法错误。
    ContextMemoryRepository.list_authoritative_active 整条路径直接报错 →
    MemorySourceAdapter 永远拿不到任何 user memory → RAG 失败。

    修复：用 ``_mysql_nulls_last`` 等价（``col IS NULL`` 标记列 + 主排序键）。
    验证：编译出的 SQL 不含 ``NULLS LAST/FIRST`` 关键字。
    """
    from sqlalchemy.dialects import mysql as mysql_dialect
    from app.models.context_engine import ContextMemory
    from app.repositories.context_engine_repositories import (
        ContextMemoryRepository,
        _mysql_nulls_last,
    )

    stmt = (
        select(ContextMemory)
        .order_by(
            *_mysql_nulls_last(ContextMemory.quality_score, descending=True)
        )
    )
    compiled_sql = str(stmt.compile(dialect=mysql_dialect.dialect(), compile_kwargs={"literal_binds": True}))
    assert "NULLS LAST" not in compiled_sql.upper(), (
        f"修复后 SQL 不应包含 NULLS LAST：{compiled_sql[:300]}"
    )
    assert "NULLS FIRST" not in compiled_sql.upper()
    assert "IS NULL" in compiled_sql.upper(), "等价实现必须使用 IS NULL 标记列"


def test_invoker_uses_parsed_fallback_when_result_success_false():
    """WP-BE-09 回归：LLMClient 在 parse 失败时按 ``FALLBACK_DEFAULT`` 政策
    解析 ``profile.fallback_text``，生成 ``LLMProfileResult(success=False,
    parsed=<fallback_dict>)``。

    Invoker 之前的实现直接走 error path（``if not result.success:``），
    把这个对象当 Provider 失败处理（``value=None``）→ bridge 返回 None →
    IntentRouter 永远降级为 ``route=clarify``，连 ``profile.fallback_text``
    提供的语义降级（unknown/clarify 字典）都没消费到。

    修复：当 ``parsed`` 已设置时直接消费 fallback 值，跳过 error path。
    本测试验证 Invoker 在该场景下返回 ``value=fallback_dict``（而非 None）。
    """
    from unittest.mock import AsyncMock, MagicMock
    from app.agent_runtime.context.llm_invoker import ContextAwareLLMInvoker
    from app.context_engine.models.context import ContextRequest
    from app.llm.errors import LLMProfileParseError
    from app.llm.task_profiles import LLMTaskProfile

    async def _run():
        # Mocks
        request = ContextRequest(user_id="1", call_site="intent.recognize", current_user_message="你好")
        runtime_context = MagicMock()
        snapshot_writer = MagicMock()
        snapshot_writer.mark_sent = AsyncMock()
        snapshot_writer.complete = AsyncMock()
        snapshot_writer.begin_build = AsyncMock(return_value=MagicMock(public_id="snap_test"))

        engine = MagicMock()
        engine.compose = AsyncMock(return_value=MagicMock(
            snapshot_public_id="snap_test",
            messages=[],
            prompt_text="",
            prompt_digest="",
            estimated_input_tokens=0,
        ))

        llm_client = MagicMock()
        # 模拟 LLM 返回非 JSON，LLMClient 按 FALLBACK_DEFAULT 应用 fallback_text
        fallback_dict = {
            "intent": "unknown",
            "route": "clarify",
            "supported": False,
            "confidence": 0.0,
            "reason": "intent_parse_failed",
        }
        llm_client.generate_with_profile = AsyncMock(return_value=MagicMock(
            success=False,            # parse 失败 → success=False
            parsed=fallback_dict,     # 但 fallback_text 已成功解析为 dict
            error_type="parse_error",
            error_message="strict-JSON parse failed",
            latency_ms=100,
            usage={"input_tokens": 10, "output_tokens": 5},
        ))

        invoker = ContextAwareLLMInvoker(
            engine=engine,
            snapshot_writer=snapshot_writer,
            llm_client_factory=lambda _rt: llm_client,
        )

        profile = LLMTaskProfile(
            name="intent_recognition",
            system_prompt="",
            parser="json_strict",
            fallback_text='{"intent":"unknown","route":"clarify"}',
            on_parse_failure="fallback_default",
        )

        result = await invoker.invoke(
            request=request,
            llm_task_profile=profile,
            runtime_context=runtime_context,
        )
        return result

    result = asyncio.run(_run())
    # 验证：value 不是 None，且是 fallback_dict
    assert result.value is not None, (
        "FALLBACK_DEFAULT 时 Invoker 必须消费 fallback_text 派生的 parsed，"
        "而非返回 None（否则 IntentRouter 永远降级为 clarify）"
    )
    assert result.value.get("intent") == "unknown"
    assert result.value.get("route") == "clarify"
    # Snapshot 应标记为 completed（不是 failed）—— 因为 fallback 是合法降级
    # mark_sent / complete 都被调用过，fail 没有被调用
    # 注：实际调用链太长，本测试只验证 value 字段。


def test_planner_unknown_call_site_fails_fast():
    planner = ContextPlanner()
    with pytest.raises(ContextEngineFailure) as exc_info:
        planner.plan(ContextRequest(user_id="usr_1", call_site="some.unknown.site", model_context_window=100000))
    assert exc_info.value.error.code == "context.profile.no_call_site_mapping"


def test_planner_requires_call_site():
    planner = ContextPlanner()
    with pytest.raises(ContextEngineFailure) as exc_info:
        planner.plan(ContextRequest(user_id="usr_1", call_site=""))
    assert exc_info.value.error.code == "context.plan.no_call_site"


def test_planner_retrieval_query_generation():
    planner = ContextPlanner()
    plan = planner.plan(
        ContextRequest(
            user_id="usr_1",
            call_site="intent.recognize",
            current_user_message="生成登录模块测试方案",
            model_context_window=100000,
        )
    )
    assert len(plan.retrieval_queries) == 1
    assert plan.retrieval_queries[0].query_text == "生成登录模块测试方案"


def test_planner_unknown_window_uses_floor():
    planner = ContextPlanner()
    plan = planner.plan(ContextRequest(user_id="usr_1", call_site="chat.reply"))
    assert plan.model_context_window == _UNKNOWN_WINDOW_FLOOR


# ── Scope Resolver ───────────────────────────────────────────────


def test_scope_resolver_thread_id_equals_task():
    resolver = ContextScopeResolver()
    scope = resolver.resolve(ContextRequest(user_id="usr_1", call_site="x", task_id="task_pub_1"))
    assert scope.thread_id == "task_pub_1"


def test_scope_resolver_thread_id_mismatch_rejected():
    resolver = ContextScopeResolver()
    with pytest.raises(ScopeResolutionError):
        resolver.resolve(
            ContextRequest(user_id="usr_1", call_site="x", task_id="task_pub_1", thread_id="other")
        )


def test_scope_resolver_invalid_user():
    resolver = ContextScopeResolver()
    with pytest.raises(ScopeResolutionError):
        resolver.resolve(ContextRequest(user_id="BAD!", call_site="x"))


def test_scope_resolver_error_maps_to_context_error():
    resolver = ContextScopeResolver()
    err = resolver.to_error(ScopeResolutionError("bad thread", code="context.scope.thread_id_mismatch"))
    assert err.stage.value == "scope"
    assert err.code == "context.scope.thread_id_mismatch"


# ── ModelCapabilityResolver ──────────────────────────────────────


def test_capability_resolver_known_window():
    cap = ModelCapability(
        public_id="mc_1", provider="o", api_base_url="https://x", model_name="m",
        capability_type="chat", context_window_tokens=200000,
    )
    resolved = ModelCapabilityResolver().resolve(cap)
    assert resolved.context_window == 200000
    assert resolved.known_model is True


def test_capability_resolver_unknown_window_not_200k():
    cap = ModelCapability(
        public_id="mc_1", provider="o", api_base_url="https://x", model_name="m",
        capability_type="chat",
    )
    resolved = ModelCapabilityResolver().resolve(cap)
    assert resolved.context_window is None
    assert resolved.known_model is False


def test_capability_resolver_rejects_unsupported():
    cap = ModelCapability(
        public_id="mc_1", provider="o", api_base_url="https://x", model_name="m",
        capability_type="quantum",
    )
    with pytest.raises(ContextEngineFailure) as exc_info:
        ModelCapabilityResolver().resolve(cap)
    assert exc_info.value.error.code == "context.model.unsupported_capability"


def test_capability_resolver_embedding_dimension():
    cap = ModelCapability(
        public_id="mc_1", provider="o", api_base_url="https://x", model_name="e",
        capability_type="embedding", embedding_dimension=1536,
    )
    resolved = ModelCapabilityResolver().resolve(cap)
    assert resolved.embedding_dimension == 1536


# ── TokenCounter ─────────────────────────────────────────────────


def test_token_counter_heuristic():
    counter = TokenCounter()
    result = counter.estimate("你好世界")
    assert result.tokens > 0
    assert counter.uses_heuristic is True
    assert result.count_mode == "heuristic"
    assert result.fallback_reason == "no_real_tokenizer"
    assert result.safety_multiplier == 1.0
    assert result.tokenizer_name == "heuristic"
    assert result.estimated is True


def test_token_counter_empty():
    counter = TokenCounter()
    assert counter.estimate(None).tokens == 0
    assert counter.estimate("").tokens == 0


# ── TokenCounter 整改 v2 + WP-BE-04：无真实 tokenizer 不得伪装 exact ──


def test_token_counter_tokenizer_name_metadata_is_heuristic_not_exact():
    """WP-BE-04：tokenizer_name 只是元数据；未真实调用 tokenizer → heuristic。"""
    counter = TokenCounter(tokenizer_name="cl100k_base", model_known=True)
    result = counter.estimate("hello world")
    assert result.count_mode == "heuristic"
    assert result.estimated is True
    assert result.fallback_reason == "no_real_tokenizer"
    assert result.tokenizer_name == "heuristic"
    assert result.safety_multiplier == 1.0


def test_token_counter_exact_only_via_estimate_exact():
    """WP-BE-04：只有真实 tokenizer 输出才能走 estimate_exact → exact。"""
    counter = TokenCounter(tokenizer_name="cl100k_base", model_known=True)
    result = counter.estimate_exact("hello world", count=42)
    assert result.count_mode == "exact"
    assert result.estimated is False
    assert result.estimated_tokens == 42
    assert result.tokenizer_name == "cl100k_base"


def test_token_counter_unknown_model_conservative_multiplier():
    counter = TokenCounter(model_known=False)
    result = counter.estimate("这是一段测试文本")
    assert result.count_mode == "unknown"
    assert result.safety_multiplier == 1.5
    assert result.fallback_reason == "model_unknown"
    assert result.estimated_tokens >= _heuristic_estimate("这是一段测试文本")


def test_token_counter_unknown_empty_is_exact():
    counter = TokenCounter(model_known=False)
    result = counter.estimate(None)
    assert result.count_mode == "exact"
    assert result.estimated_tokens == 0


def test_token_counter_fallback_reason_carried():
    counter = TokenCounter()
    result = counter.estimate("hello")
    assert result.fallback_reason == "no_real_tokenizer"
    assert result.uses_heuristic is True


# ── Feature Flags ────────────────────────────────────────────────


def test_feature_flags_default_all_off():
    flags = ContextEngineFeatureFlags()
    assert flags.context_engine_enabled is False
    assert flags.context_engine_agent_enabled is False
    assert flags.context_tool_output_governance_enabled is False
    assert flags.context_memory_read_enabled is False
    assert flags.context_memory_write_enabled is False
    assert flags.context_memory_auto_extract_enabled is False
    assert flags.context_memory_auto_activate_enabled is False
    assert flags.context_dense_retrieval_enabled is False
    assert flags.context_hybrid_fusion_enabled is False
    assert flags.context_rerank_enabled is False
    assert flags.context_compaction_enabled is False
    assert flags.context_conversation_compaction_enabled is False
    assert flags.context_agent_loop_compaction_enabled is False
    assert flags.context_full_replace_enabled is False
    assert flags.context_full_prompt_debug_enabled is False
    assert flags.context_debug_api_enabled is False


def test_feature_flags_evaluate():
    flags = ContextEngineFeatureFlags()
    assert flags.evaluate(ContextFeatureFlag.CONTEXT_ENGINE_ENABLED) is False
    assert flags.evaluate(ContextFeatureFlag.CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED) is False
    assert flags.evaluate(ContextFeatureFlag.CONTEXT_RERANK_ENABLED) is False
