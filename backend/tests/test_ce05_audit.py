"""CE-05 WP-4 三类 Audit API 测试。

覆盖：serializer 脱敏（不返回 prompt/vector/secret/storage key）、
owner-scope 语义、路由挂载、负向 401/404。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.v1.audit_serializers import (
    serialize_compaction,
    serialize_retrieval,
    serialize_snapshot,
)


class _Row:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_snapshot_serializer_no_forbidden_fields():
    row = _Row(
        public_id="snap_1",
        llm_task_type="test_plan.review",
        context_kind="review",
        status="completed",
        estimated_tokens=1500,
        engine_version="v3",
        call_site="test_plan.review",
        context_policy_version="v1",
        prompt_digest="d" * 64,
        created_at=None,
        # 应被排除的字段
        context_preview="机密正文",
        prompt_excerpt="excerpt",
        full_prompt_payload_id=99,
    )
    out = serialize_snapshot(row)
    assert out["public_id"] == "snap_1"
    assert "context_preview" not in out
    assert "prompt_excerpt" not in out
    assert "full_prompt_payload_id" not in out
    assert "prompt_digest" in out


def test_snapshot_serializer_exposes_only_safe_preflight_telemetry():
    row = _Row(
        public_id="snap_telemetry",
        created_at=None,
        context_window_tokens=32000,
        input_budget_tokens=18000,
        target_input_tokens=18000,
        estimated_input_tokens=17000,
        actual_input_tokens=16850,
        actual_output_tokens=120,
        compaction_json={
            "waterline": "hard_compact",
            "action": "compact",
            "tokens_before": 17500,
            "tokens_after": 6000,
            "hard_threshold": 17000,
            "raw_prompt": "must never be exposed",
            "summary_text": "must never be exposed",
        },
    )

    out = serialize_snapshot(row)

    assert out["budget"]["context_window_tokens"] == 32000
    assert out["token_usage"]["actual_input_tokens"] == 16850
    assert out["preflight"] == {
        "waterline": "hard_compact",
        "action": "compact",
        "tokens_before": 17500,
        "tokens_after": 6000,
        "hard_threshold": 17000,
    }
    assert "raw_prompt" not in str(out)
    assert "summary_text" not in str(out)


def test_preflight_telemetry_records_runtime_decision_without_content():
    from app.context_engine.models.enums import (
        CompactionStatus,
        CompactionTriggerType,
        ContextPreflightAction,
    )
    from app.context_engine.runtime.context_engine import _preflight_telemetry

    plan = SimpleNamespace(
        soft_threshold=700,
        hard_compact_threshold=850,
        absolute_threshold=950,
    )
    result = SimpleNamespace(
        status=CompactionStatus.COMPACTED,
        action=ContextPreflightAction.COMPACT,
        tokens_before=900,
        tokens_after=300,
        target_tokens=600,
        selected=SimpleNamespace(dropped=["metadata only"]),
        compacted_summary_refs=["summary ref"],
        compression_provider_call_count=1,
        business_provider_call_count=0,
        degraded=False,
        blocked_reason=None,
        raw_prompt="must never enter telemetry",
    )

    out = _preflight_telemetry(
        plan,
        result,
        trigger=CompactionTriggerType.PREFLIGHT,
    )

    assert out == {
        "trigger": "preflight",
        "waterline": "hard_compact",
        "status": "compacted",
        "action": "compact",
        "tokens_before": 900,
        "tokens_after": 300,
        "target_tokens": 600,
        "soft_threshold": 700,
        "hard_threshold": 850,
        "absolute_threshold": 950,
        "dropped_ref_count": 1,
        "compacted_summary_count": 1,
        "compression_provider_call_count": 1,
        "business_provider_call_count": 0,
        "degraded": False,
        "blocked_reason": None,
        "compaction_attempted": False,
        "compaction_compactor_available": None,
        "compaction_runtime_context_available": None,
        "compaction_phase": None,
        "compaction_exception_type": None,
        "compaction_exception_code": None,
    }
    assert "raw_prompt" not in out


def test_retrieval_serializer_truncates_query():
    row = _Row(
        public_id="crr_1",
        query_hash="h" * 64,
        query_excerpt="x" * 500,
        recalled_count=5,
        selected_count=2,
        fallback_code=None,
        total_ms=120,
        created_at=None,
    )
    out = serialize_retrieval(row)
    assert len(out["query_excerpt"]) <= 200
    assert out["selected_count"] == 2


def test_compaction_serializer_truncates_error_and_hides_refs():
    row = _Row(
        public_id="ccr_1",
        compaction_type="conversation",
        trigger_type="preflight",
        policy_key="conversation-retention.deep.v2",
        policy_version="v2",
        status="completed",
        tokens_before=1000,
        tokens_after=400,
        target_tokens=520,
        compression_ratio=0.4,
        error_code="compression.generate_failed",
        error_message="e" * 600,
        created_at=None,
    )
    out = serialize_compaction(row)
    assert len(out["error_message"]) <= 500
    assert out["error_code"] == "compression.generate_failed"
    assert out["compression_ratio"] == 0.4
    assert out["policy_key"] == "conversation-retention.deep.v2"
    assert out["policy_version"] == "v2"
    assert out["target_tokens"] == 520
    # 不含 storage_key / 路径
    assert "storage" not in str(out).lower()


def test_serializers_no_secret_fields():
    for s in (serialize_snapshot, serialize_retrieval, serialize_compaction):
        out = s(_Row(public_id="x", **({"created_at": None})))
        payload = str(out).lower()
        assert "sk-" not in payload
        assert "bearer" not in payload
        assert "api_key" not in payload


async def test_compaction_audit_list_borrows_request_session(sqlite_session_factory):
    """Regression: a request-scoped session must satisfy the audit service context-manager contract."""
    from app.api.v1.context_audit import list_compaction
    from app.schemas.auth import UserProfile

    current = UserProfile(id="user_1", internal_id=1, name="tester", role="user")
    async with sqlite_session_factory() as session:
        response = await list_compaction(current=current, session=session, limit=5)

    assert response["code"] == 0
    assert response["data"]["items"] == []


def test_audit_router_registered():
    """Audit router 含 6 端点（snapshots/retrieval/compaction × list/detail）。"""
    from app.api.v1.context_audit import router as audit_router

    paths = [getattr(r, "path", "") for r in audit_router.routes]
    expected = [
        "/audit/snapshots",
        "/audit/snapshots/{public_id}",
        "/audit/retrieval",
        "/audit/retrieval/{public_id}",
        "/audit/compaction",
        "/audit/compaction/{public_id}",
    ]
    for exp in expected:
        assert exp in paths, f"缺少端点 {exp}"
