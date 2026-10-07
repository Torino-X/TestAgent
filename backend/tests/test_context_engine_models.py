"""CE-01 领域模型与值对象单元测试。

覆盖：值对象校验、枚举序列化、ContextItem/Scope/Ref、Profile/Budget、
Request/Plan、错误体系映射。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineFailure,
    ContextEngineStage,
    to_app_error,
)
from app.context_engine.models import (
    BudgetPolicy,
    ContextBudget,
    ContextItem,
    ContextKind,
    ContextPlan,
    ContextProfile,
    ContextRef,
    ContextRequest,
    ContextScope,
    ContextTrust,
    Digest,
    ProfileSectionSpec,
    PublicId,
    RetrievalQuery,
    RetrievalStrategy,
    RerankStrategy,
    SourceType,
    TokenCount,
    VersionString,
    WorkspaceKey,
    canonical_json,
)
from app.context_engine.models.enums import (
    CompressionLevel,
    ContextKind as CK,
)


# ── 值对象 ─────────────────────────────────────────────────────────


def test_public_id_accepts_valid():
    assert PublicId("usr_abc123") == "usr_abc123"


def test_public_id_rejects_invalid():
    with pytest.raises(ValueError):
        PublicId("Uppercase")
    with pytest.raises(ValueError):
        PublicId("")
    with pytest.raises(ValueError):
        PublicId("ab")  # 太短


def test_workspace_key_reserved_rejected():
    with pytest.raises(ValueError):
        WorkspaceKey("default")
    with pytest.raises(ValueError):
        WorkspaceKey("system")


def test_workspace_key_none_allowed():
    assert WorkspaceKey(None) is None


def test_digest_stable_and_hex():
    a = Digest.of("hello")
    b = Digest.of("hello")
    c = Digest.of("world")
    assert a == b
    assert a != c
    assert len(a) == 64
    int(a, 16)  # 校验 hex


def test_token_count_rejects_negative():
    with pytest.raises(ValueError):
        TokenCount(-1)


def test_version_string_valid():
    assert VersionString("v1") == "v1"
    assert VersionString("v1.2.3") == "v1.2.3"


def test_canonical_json_stable():
    assert canonical_json({"b": 1, "a": [3, 1]}) == canonical_json({"a": [3, 1], "b": 1})


# ── ContextItem / Scope / Ref ──────────────────────────────────────


def test_context_item_defaults():
    item = ContextItem(
        item_id="i1",
        kind=ContextKind.TASK_STATE,
        source_type=SourceType.TASK_STATE,
        content="x",
        authority=80,
    )
    assert item.trust == ContextTrust.UNTRUSTED_REFERENCE
    assert item.estimated_tokens == 0


def test_context_item_authority_bounds():
    with pytest.raises(ValidationError):
        ContextItem(item_id="i1", kind=CK.CURRENT_GOAL, source_type="conv", content="x", authority=101)


def test_context_item_extra_forbid():
    with pytest.raises(ValidationError):
        ContextItem(item_id="i1", kind=CK.CURRENT_GOAL, source_type="conv", content="x", authority=50, unknown_field=1)


def test_context_scope_thread_id_matches_task():
    scope = ContextScope(user_id="usr_1", task_id="task_pub_1", thread_id="task_pub_1")
    assert scope.thread_id == "task_pub_1"
    assert scope.is_workspace_scoped is False


def test_context_scope_ownership():
    scope = ContextScope(user_id="usr_1", workspace_key="ws_a")
    assert scope.validate_ownership("usr_1", "ws_a") is True
    assert scope.validate_ownership("usr_2", "ws_a") is False
    assert scope.validate_ownership("usr_1", "ws_b") is False


def test_context_ref_state_safe():
    ref = ContextRef(item_id="i1", kind=ContextKind.TASK_STATE, source_type=SourceType.TASK_STATE, version="v1")
    state = ref.to_state_dict()
    assert state["kind"] == "task_state"
    assert state["item_id"] == "i1"


# ── Profile / Budget ──────────────────────────────────────────────


def test_profile_required_sections_unique():
    with pytest.raises(ValidationError):
        ContextProfile(
            key="dup",
            required_sections=[
                ProfileSectionSpec(kind=CK.CURRENT_GOAL, required=True),
                ProfileSectionSpec(kind=CK.CURRENT_GOAL, required=True),
            ],
        )


def test_profile_required_kind_set():
    profile = ContextProfile(
        key="p",
        required_sections=[ProfileSectionSpec(kind=CK.TASK_STATE, required=True)],
    )
    assert CK.TASK_STATE in profile.required_kind_set()


def test_budget_policy_monotonic():
    with pytest.raises(ValidationError):
        BudgetPolicy(target_input_ratio=0.7, soft_ratio=0.5)


def test_context_budget_levels():
    budget = ContextBudget(
        model_context_window=200000,
        output_reserve=24000,
        runtime_reserve=16000,
        provider_overhead=5000,
        safety_margin=15000,
        target_input=120000,
        soft_threshold=100000,
        hard_compact_threshold=160000,
        absolute_threshold=185000,
    )
    assert budget.usable_window == 140000
    assert budget.level_for(50000) is CompressionLevel.TARGET
    assert budget.level_for(120000) is CompressionLevel.SOFT
    assert budget.level_for(170000) is CompressionLevel.HARD_COMPACT
    assert budget.level_for(190000) is CompressionLevel.ABSOLUTE
    assert budget.valid is True


# ── Request / Plan ────────────────────────────────────────────────


def test_context_request_has_target():
    req = ContextRequest(user_id="usr_1", call_site="chat.reply")
    assert req.has_target is True
    req2 = ContextRequest(user_id="usr_1", call_site="")
    assert req2.has_target is False


def test_context_plan_retrieval_query():
    plan = ContextPlan(
        profile_key="p",
        profile_version="v1",
        model_context_window=200000,
        input_budget=100000,
        output_reserve=10000,
        runtime_reserve=5000,
        safety_margin=5000,
        retrieval_queries=[
            RetrievalQuery(query_text="q", strategy=RetrievalStrategy.PLANNED, rerank_strategy=RerankStrategy.WEIGHTED_RRF)
        ],
    )
    assert plan.retrieval_queries[0].top_k == 5
    assert plan.retrieval_queries[0].rerank_strategy is RerankStrategy.WEIGHTED_RRF


# ── 错误体系 ──────────────────────────────────────────────────────


def test_context_engine_error_serializable():
    err = ContextEngineError(
        code="context.profile.not_found",
        detail="未注册的 profile",
        stage=ContextEngineStage.PROFILE,
        retryable=False,
    )
    d = err.model_dump(mode="json")
    assert d["code"] == "context.profile.not_found"
    assert d["stage"] == "profile"
    assert d["retryable"] is False
    # state 只保存轻量字段
    s = err.to_state_dict()
    assert "detail" not in s
    assert s["stage"] == "profile"


def test_context_engine_error_validation():
    with pytest.raises(ValueError):
        ContextEngineError(code="", detail="x", stage=ContextEngineStage.PROFILE)
    with pytest.raises(ValueError):
        ContextEngineError(code="c", detail="x" * 2000, stage=ContextEngineStage.PROFILE)


def test_context_engine_failure_wraps_error():
    err = ContextEngineError(code="c", detail="d", stage=ContextEngineStage.SCOPE)
    failure = ContextEngineFailure(err)
    assert failure.error is err


def test_to_app_error_maps_known():
    err = ContextEngineError(code="c", detail="d", stage=ContextEngineStage.SOURCE)
    code, message, detail = to_app_error(err)
    assert code == 51001
    assert message == "d"
    assert detail["stage"] == "source"


def test_to_app_error_maps_retrieval():
    err = ContextEngineError(code="c", detail="d", stage=ContextEngineStage.RETRIEVAL)
    code, _, _ = to_app_error(err)
    assert code == 51101


def test_to_app_error_fallback_unknown():
    code, message, detail = to_app_error(ValueError("boom"))
    assert code == 51001
    assert "boom" not in message
    assert detail == {}
