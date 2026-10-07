"""Phase 2.9A.20 Summary Facts 与状态投影测试。

覆盖:
  1. build_summary_facts 单一事实源
  2. 章节数从 generated_sections(list) 与 int 字面值
  3. 业务模块从 requirement_analysis.modules 真字段读取
  4. 审查分级: block / warning / suggestion 三档
  5. Artifact 状态: present + format_check_result.status 二元合成
  6. fallback_summary_text 实时与历史共用同一字符串
  7. 空 State / None State 安全回退
  8. 部分成功状态(任务完成但 format_check failed)业务事实不丢
  9. finalize_task_node payload 含 summary_facts + checked_artifact_public_id
 10. 旧 envelope.data 形态(generated_sections=int)兼容
"""

from __future__ import annotations

import pytest


# ── 单元: build_summary_facts ────────────────────────────────────────────


class TestBuildSummaryFacts:
    def test_empty_state_yields_zeros(self):
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        facts = build_summary_facts({})
        assert facts["generated_sections"] == 0
        assert facts["kept_sections"] == 0
        assert facts["business_modules"] == 0
        assert facts["review"]["block_count"] == 0
        assert facts["review"]["warning_count"] == 0
        assert facts["artifact"]["present"] is False

    def test_none_state_yields_zeros(self):
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        facts = build_summary_facts(None)  # type: ignore[arg-type]
        assert facts["generated_sections"] == 0
        assert facts["artifact"]["present"] is False

    def test_chapter_count_from_list(self):
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {
            "test_plan_content": {
                "generated_sections": [
                    {"section_id": "1"},
                    {"section_id": "2"},
                    {"section_id": "3"},
                ],
                "kept_sections": [{"section_id": "K1"}],
            }
        }
        facts = build_summary_facts(state)
        assert facts["generated_sections"] == 3
        assert facts["kept_sections"] == 1

    def test_chapter_count_from_int_compat(self):
        """旧 envelope.data 形态 — generated_sections 是 int"""
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {"test_plan_content": {"generated_sections": 16, "kept_sections": 4}}
        facts = build_summary_facts(state)
        assert facts["generated_sections"] == 16
        assert facts["kept_sections"] == 4

    def test_business_modules_from_real_field(self):
        """Phase 2.9A.20 §6.2:业务模块数 = requirement_analysis.modules 真字段,不靠章节标题"""
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {
            "requirement_analysis": {
                "modules": [
                    {"name": "用户管理"},
                    {"name": "订单管理"},
                    {"name": "支付管理"},
                ]
            }
        }
        facts = build_summary_facts(state)
        assert facts["business_modules"] == 3

    def test_business_modules_legacy_field(self):
        """向后兼容 — 历史字段 business_modules"""
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {"requirement_analysis": {"business_modules": 5}}
        facts = build_summary_facts(state)
        assert facts["business_modules"] == 5

    def test_review_severity_breakdown(self):
        """Phase 2.9A.20 §6.2:审查分 block / warning / suggestion 三档"""
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {
            "review_result": {
                "level": "failed",
                "review_issues": [
                    {"severity": "block", "message": "b1"},
                    {"severity": "block", "message": "b2"},
                    {"severity": "warn", "message": "w1"},
                    {"severity": "suggestion", "message": "s1"},
                    {"severity": "info", "message": "i1"},  # info 不计入
                ],
            }
        }
        facts = build_summary_facts(state)
        assert facts["review"]["block_count"] == 2
        assert facts["review"]["warning_count"] == 1
        assert facts["review"]["suggestion_count"] == 1

    def test_review_legacy_issues_compat(self):
        """旧 envelope 形态 — review_result.issues[]"""
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {"review_result": {"level": "warning", "issues": [{}, {}, {}]}}
        facts = build_summary_facts(state)
        assert facts["review"]["block_count"] == 3

    def test_review_passed_ignores_stale_block_issue_fields(self):
        """复审通过后残留的旧 block 字段不能污染最终任务总结。"""
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {
            "review_result": {
                "level": "passed",
                "passed": True,
                "review_issues": [
                    {
                        "severity": "block",
                        "section_id": "body_67_level_1",
                        "message": "用户选择 AI 生成的章节缺失或内容为空",
                    }
                ],
                "block_issues": [
                    {"section_id": "section_14", "message": "生成内容为空"}
                ],
                "repair_unresolved_issue_details": [
                    {"section_id": "section_14", "message": "生成内容为空"}
                ],
                "suggestions": [{"message": "请检查生成内容"}],
            }
        }
        facts = build_summary_facts(state)
        assert facts["review"]["block_count"] == 0
        assert facts["review"]["block_details"] == []
        assert facts["review"]["suggestion_count"] == 1

    def test_review_suggestions_count(self):
        state = {
            "review_result": {
                "level": "passed",
                "suggestions": [{"s": 1}, {"s": 2}],
                "warnings": [{"w": 1}],
            }
        }
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        facts = build_summary_facts(state)
        assert facts["review"]["suggestion_count"] >= 2
        assert facts["review"]["warning_count"] >= 1

    def test_artifact_status_dual_signal(self):
        """Artifact present + format_check_result.status 二元合成"""
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {
            "artifact": {
                "public_id": "art_abc",
                "file_name": "方案.docx",
                "storage_path": "artifacts/1/x.docx",
            },
            "format_check_result": {"status": "blocked"},
        }
        facts = build_summary_facts(state)
        assert facts["artifact"]["present"] is True
        assert facts["artifact"]["public_id"] == "art_abc"
        assert facts["artifact"]["format_status"] == "blocked"
        assert facts["artifact"]["format_blocked"] is True

    def test_artifact_page_count_passthrough(self):
        """文档页数来自导出的 Word artifact 元数据。"""
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        facts = build_summary_facts(
            {
                "artifact": {
                    "public_id": "art_abc",
                    "file_name": "项目_测试方案.docx",
                    "storage_path": "artifacts/1/x.docx",
                    "page_count": 12,
                }
            }
        )
        assert facts["page_count"] == 12
        assert facts["artifact"]["page_count"] == 12

    def test_artifact_legacy_level_compat(self):
        """旧 envelope 形态 — format_check_result.level"""
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        state = {
            "artifact": {"public_id": "art_abc"},
            "format_check_result": {"level": "loss_detected"},
        }
        facts = build_summary_facts(state)
        # 不强行 mapping loss_detected → blocked;让它原样透传
        assert facts["artifact"]["format_status"] == "loss_detected"

    def test_task_status_passthrough(self):
        from app.agent_runtime._shared.summary_facts import build_summary_facts
        facts = build_summary_facts({"task_status": "running"})
        assert facts["task_status"] == "running"


# ── 单元: fallback_summary_text ──────────────────────────────────────────


class TestFallbackSummaryText:
    def test_empty_state_basic_message(self):
        from app.agent_runtime._shared.summary_facts import fallback_summary_text

        text = fallback_summary_text({})
        assert "任务已完成" in text

    def test_with_sections_and_modules(self):
        from app.agent_runtime._shared.summary_facts import fallback_summary_text

        text = fallback_summary_text(
            {
                "test_plan_content": {
                    "generated_sections": [{"section_id": "1"}, {"section_id": "2"}],
                    "kept_sections": [],
                },
                "requirement_analysis": {"modules": ["m1", "m2", "m3"]},
            }
        )
        assert "2" in text  # 章节
        assert "3" in text  # 业务模块

    def test_with_artifact_and_format_blocked(self):
        from app.agent_runtime._shared.summary_facts import fallback_summary_text

        text = fallback_summary_text(
            {
                "artifact": {"public_id": "art_abc", "file_name": "x.docx"},
                "format_check_result": {"status": "blocked"},
            }
        )
        assert "x.docx" in text
        assert "格式检查" in text or "格式" in text

    def test_review_block_and_warning(self):
        from app.agent_runtime._shared.summary_facts import fallback_summary_text

        text = fallback_summary_text(
            {
                "review_result": {
                    "review_issues": [
                        {"severity": "block"},
                        {"severity": "warn"},
                    ]
                }
            }
        )
        assert "1 个阻断" in text or "1个阻断" in text

    def test_empty_standard_review_issues_do_not_count_legacy_issues_as_blocks(self):
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        facts = build_summary_facts(
            {
                "review_result": {
                    "level": "passed",
                    "review_issues": [],
                    "issues": [
                        {"message": "缺少必含章节：风险分析"},
                        {"message": "缺少必含章节：测试交付物"},
                    ],
                    "block_issues": [
                        {"message": "旧审查遗留阻断"},
                    ],
                }
            }
        )

        assert facts["review"]["block_count"] == 0
        assert facts["review"]["block_details"] == []


# ── 节点层: finalize_task_node payload 含 summary_facts ────────────────────


class TestFinalizeTaskPayload:
    @pytest.mark.asyncio
    async def test_finalize_payload_contains_summary_facts(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
            finalize_task_node,
        )

        ctx = MagicMock()
        ctx.task_internal_id = 1
        ctx.conversation_internal_id = 1
        ctx.user_internal_id = 1
        sink = MagicMock()
        sink.emit = AsyncMock()
        ctx.event_sink = sink

        # Part 成功场景 — task 走到 finalize 但 format_check_failed
        state = {
            "artifact": {
                "public_id": "art_abc",
                "file_name": "test.docx",
                "page_count": 9,
            },
            "format_check_result": {"status": "failed"},
            "test_plan_content": {
                "generated_sections": [{"section_id": "x"} for _ in range(5)]
            },
            "review_result": {
                "level": "warning",
                "review_issues": [{"severity": "warn"} for _ in range(2)],
            },
            "summary": "测试生成成功并通过。",
            "completed_nodes": [],
            "checked_artifact_public_id": "art_abc",
        }

        await finalize_task_node(state, ctx=ctx)

        # 找到 TASK_COMPLETED 的 emit
        task_completed_emits = [
            call for call in sink.emit.call_args_list
            if call.kwargs.get("event_type") == "task_completed"
            or (call.args and len(call.args) >= 4 and call.args[3] == "task_completed")
        ]
        assert len(task_completed_emits) >= 1

        # 检查 payload 同时含 summary + summary_facts + checked_artifact_public_id
        # 由于 emit 签名,我们看 kwargs / args:
        task_completed_payload = None
        for call in sink.emit.call_args_list:
            kwargs = call.kwargs
            if "payload" in kwargs and "summary_facts" in kwargs.get("payload", {}):
                task_completed_payload = kwargs["payload"]
                break

        assert task_completed_payload is not None
        assert "summary" in task_completed_payload
        assert "summary_facts" in task_completed_payload
        assert "checked_artifact_public_id" in task_completed_payload
        # 业务事实:章节数 5 不应是 0
        assert task_completed_payload["summary_facts"]["generated_sections"] == 5
        assert task_completed_payload["summary_facts"]["page_count"] == 9
        assert task_completed_payload["summary_facts"]["artifact"]["page_count"] == 9
        # 部分成功场景不丢审查数
        assert task_completed_payload["summary_facts"]["review"]["warning_count"] == 2

    @pytest.mark.asyncio
    async def test_partial_success_preserves_business_facts(self):
        """Phase 2.9A.20 §6.3 部分成功 — task 进入 finalize 但其他阶段部分失败

        关键:业务事实在 finalize 阶段不应被清零。
        """
        from unittest.mock import AsyncMock, MagicMock

        from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
            finalize_task_node,
        )
        from app.agent_runtime._shared.summary_facts import build_summary_facts

        ctx = MagicMock()
        ctx.task_internal_id = 1
        ctx.conversation_internal_id = 1
        ctx.user_internal_id = 1
        sink = MagicMock()
        sink.emit = AsyncMock()
        ctx.event_sink = sink

        # 即使没有 artifact(导出失败),章节/审查事实仍保存
        state = {
            "test_plan_content": {
                "generated_sections": [{"section_id": str(i)} for i in range(8)]
            },
            "review_result": {
                "level": "passed",
                "review_issues": [],
            },
            "summary": "生成+审查通过，导出失败。",
            "completed_nodes": [],
        }

        canonical_facts = build_summary_facts(state)
        assert canonical_facts["generated_sections"] == 8
        assert canonical_facts["artifact"]["present"] is False
        # 即便导出失败,业务事实仍是 8 章
        # 这是 §6.3 关键验收点
