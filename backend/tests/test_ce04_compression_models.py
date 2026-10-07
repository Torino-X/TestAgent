"""CE-04 WP-1：Compression 核心模型 + ProtectedAnchor 测试。

覆盖：
- 枚举（CompactionType/Trigger/RecoveryMode/Action）序列化与 DB 兼容。
- 核心 DTO FrozenModel（extra=forbid、不可变）。
- ProtectedAnchorBuilder 从 ContextRequest/TaskStateRef/Profile 构建。
- AnchorValidator 校验（digest/summary presence/直接注入）。
"""

from __future__ import annotations

import pytest

from app.context_engine.compression.anchor import AnchorValidator, ProtectedAnchorBuilder
from app.context_engine.compression.models import (
    ContextCompactionRequest,
    ContextCompactionResult,
    ContextPreflightRequest,
    ContextPreflightResult,
    ContextRehydrateRequest,
    ProtectedAnchor,
    RehydratedContext,
)
from app.context_engine.models.context import ContextRequest
from app.context_engine.models.enums import (
    CompactionStatus,
    CompactionTriggerType,
    CompactionType,
    CompressionLevel,
    ContextPreflightAction,
    RecoveryMode,
)


class TestCompressionEnums:
    """枚举与 DB String(32) 兼容（零 Migration 验证）。"""

    def test_compaction_type_values(self):
        assert CompactionType.ITEM_PRUNING.value == "item_pruning"
        assert CompactionType.CONVERSATION.value == "conversation"
        assert CompactionType.AGENT_LOOP.value == "agent_loop"
        assert CompactionType.FULL_REPLACE.value == "full_replace"
        # 全部 ≤32 字符（String(32) 列约束）
        for t in CompactionType:
            assert len(t.value) <= 32

    def test_trigger_type_values(self):
        assert CompactionTriggerType.PREFLIGHT.value == "preflight"
        assert CompactionTriggerType.PROVIDER_CONTEXT_ERROR.value == "provider_context_error"
        assert CompactionTriggerType.ABSOLUTE_THRESHOLD.value == "absolute_threshold"
        for t in CompactionTriggerType:
            assert len(t.value) <= 32

    def test_recovery_mode_values(self):
        assert RecoveryMode.SUMMARY_ONLY.value == "summary_only"
        assert RecoveryMode.SUMMARY_WITH_REFS.value == "summary_with_refs"
        assert RecoveryMode.EVIDENCE_SEGMENTS.value == "evidence_segments"
        assert RecoveryMode.FULL_REHYDRATE.value == "full_rehydrate"
        for m in RecoveryMode:
            assert len(m.value) <= 32

    def test_action_and_status_compat(self):
        assert ContextPreflightAction.PASS.value == "pass"
        assert CompactionStatus.PRUNED.value == "pruned"
        assert CompressionLevel.HARD_COMPACT.value == "hard_compact"


class TestCompressionModels:
    """核心 DTO FrozenModel 语义。"""

    def test_protected_anchor_frozen(self):
        a = ProtectedAnchor(
            key="task_goal",
            value_digest="d" * 64,
            source_ref="task_state.task_goal",
            required_in_summary=True,
            kind="summary_required",
        )
        assert a.to_state_dict()["key"] == "task_goal"
        with pytest.raises(Exception):
            a.key = "changed"  # frozen 不可变

    def test_models_extra_forbidden(self):
        with pytest.raises(Exception):
            ContextCompactionRequest(
                request_id="r1",
                user_id=1,
                call_site="compression.conversation",
                compaction_type=CompactionType.CONVERSATION,
                trigger=CompactionTriggerType.PREFLIGHT,
                policy_key="k",
                policy_version="v1",
                source_digest="d" * 64,
                tokens_before=100,
                target_tokens=50,
                unexpected_field=1,  # extra=forbid
            )

    def test_preflight_result_state_dict(self):
        from app.context_engine.models.selection import SelectedContextSet

        r = ContextPreflightResult(
            status=CompactionStatus.PASS,
            action=ContextPreflightAction.PASS,
            selected=SelectedContextSet(),
            tokens_before=100,
            tokens_after=100,
            target_tokens=200,
        )
        sd = r.to_state_dict()
        assert sd["status"] == "pass"
        assert sd["business_provider_call_count"] == 0

    def test_compaction_result_ratio(self):
        r = ContextCompactionResult(
            run_public_id="run_1",
            tokens_after=30,
            compression_ratio=0.3,
        )
        assert r.compression_ratio == 0.3
        assert r.status == CompactionStatus.COMPACTED

    def test_rehydrate_models(self):
        req = ContextRehydrateRequest(request_id="r", user_id=1)
        out = RehydratedContext(request_id="r", recovery_mode=RecoveryMode.SUMMARY_ONLY)
        assert req.recovery_mode == RecoveryMode.SUMMARY_ONLY
        assert out.summary_text is None


class TestProtectedAnchorBuilder:
    """从 ContextRequest/TaskStateRef/Profile 构建锚点。"""

    def _request(self, **kw):
        base = dict(
            user_id="1",
            call_site="test_plan.review",
            current_node="review_format",
            current_user_message="请评审测试计划格式",
        )
        base.update(kw)
        return ContextRequest(**base)

    def test_builds_from_request_and_state(self):
        req = self._request(
            state_ref={
                "task_goal": "生成登录模块测试计划",
                "locked_sections": ["s1", "s2"],
            }
        )
        anchors = ProtectedAnchorBuilder().build(req)
        keys = {a.key for a in anchors}
        assert "current_user_message" in keys
        assert "task_goal" in keys
        assert "locked_sections" in keys
        assert "current_node" in keys

    def test_direct_inject_vs_summary_required(self):
        req = self._request(
            state_ref={
                "task_goal": "生成测试计划",
                "task_type": "test_plan",
                "unresolved_decisions": ["是否用 mock"],
            }
        )
        anchors = ProtectedAnchorBuilder().build(req)
        by_key = {a.key: a for a in anchors}
        # current_user_message → direct_inject
        assert by_key["current_user_message"].kind == "direct_inject"
        assert by_key["current_user_message"].required_in_summary is False
        # task_goal → summary_required
        assert by_key["task_goal"].kind == "summary_required"
        assert by_key["task_goal"].required_in_summary is True

    def test_digest_stable_and_deterministic(self):
        req = self._request(state_ref={"task_goal": "生成测试计划"})
        builder = ProtectedAnchorBuilder()
        a1 = builder.build(req)
        a2 = builder.build(req)
        assert {x.key: x.value_digest for x in a1} == {x.key: x.value_digest for x in a2}

    def test_dedup_same_key(self):
        req = self._request(
            state_ref={"task_goal": "g", "selected_files": ["a.py"]},
            attached_file_ids=["a.py"],
        )
        anchors = ProtectedAnchorBuilder().build(req)
        keys = [a.key for a in anchors]
        assert len(keys) == len(set(keys))


class TestAnchorValidator:
    """服务端确定性校验。"""

    def test_empty_anchors_pass(self):
        ok, failures = AnchorValidator().validate([])
        assert ok and not failures

    def test_required_in_summary_present(self):
        anchors = [
            ProtectedAnchor(
                key="task_goal",
                value_digest="x" * 64,
                source_ref="task_state.task_goal",
                required_in_summary=True,
                kind="summary_required",
            )
        ]
        ok, failures = AnchorValidator().validate(anchors, {"summary_text": "目标: 生成登录模块测试计划"})
        assert ok, failures

    def test_required_in_summary_missing_fails(self):
        anchors = [
            ProtectedAnchor(
                key="task_goal",
                value_digest="x" * 64,
                source_ref="task_state.task_goal",
                required_in_summary=True,
                kind="summary_required",
            )
        ]
        ok, failures = AnchorValidator().validate(anchors, {"summary_text": "无关内容"})
        assert not ok
        assert any("task_goal" in f for f in failures)

    def test_digest_mismatch_fails(self):
        anchors = [
            ProtectedAnchor(
                key="task_goal",
                value_digest="a" * 64,
                source_ref="task_state.task_goal",
                required_in_summary=True,
                kind="summary_required",
            )
        ]
        ok, failures = AnchorValidator().validate(anchors, {"anchors": {"task_goal": "different"}})
        assert not ok
        assert any("digest_mismatch" in f for f in failures)

    def test_direct_inject_not_required_in_summary(self):
        anchors = [
            ProtectedAnchor(
                key="current_user_message",
                value_digest="b" * 64,
                source_ref="context_request.current_user_message",
                required_in_summary=False,
                kind="direct_inject",
            )
        ]
        ok, failures = AnchorValidator().validate(anchors, {"summary_text": "无此消息"})
        assert ok  # 直接注入不要求 summary 包含
