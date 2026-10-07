"""Phase 2.9A.18 路由闭环测试 — 格式损失路由与循环限制。

覆盖:
  1. passed → finalize_task
  2. warning → finalize_task
  3. failed + loops < MAX → prepare_export(再试)
  4. failed + loops >= MAX → pause_for_legacy_format_decision
  5. blocked(loss_detected 同义)+ loops ≤ MAX → pause_for_legacy_format_decision
  6. 未识别 level → pause(防御性)
  7. status 字段与 level 字段等价(routing 优先 status)
  8. loss_detected 字面值被识别为 blocked 向后兼容
  9. interrupt 路径下相同逻辑(route_after_format_check_for_interrupt)
 10. check_docx_format_node 写入 status 字段
"""

from __future__ import annotations

import importlib

import pytest

from app.agent_runtime.graphs.test_plan.constants import MAX_FORMAT_LOOPS
from app.agent_runtime.graphs.test_plan.versions.v3 import (
    routing,
    routing_after_interrupt,
)


# ── 单元: route_after_format_check ────────────────────────────────────────


class TestRouteAfterFormatCheck:
    def _state(self, status=None, level=None, loops=0):
        result = {}
        if status is not None:
            result["status"] = status
        if level is not None:
            result["level"] = level
        return {
            "format_check_result": result,
            "format_loop_count": loops,
        }

    def test_passed_goes_to_finalize(self):
        s = self._state(status="passed", loops=0)
        assert routing.route_after_format_check(s) == "finalize_task"

    def test_warning_goes_to_finalize(self):
        s = self._state(status="warning", loops=2)
        assert routing.route_after_format_check(s) == "finalize_task"

    def test_failed_with_budget_prepares_retry(self):
        s = self._state(status="failed", loops=0)
        assert routing.route_after_format_check(s) == "prepare_export"

    def test_failed_exhausted_budget_pauses(self):
        # MAX_FORMAT_LOOPS 是允许的最大循环次数;loops == MAX 已耗尽
        s = self._state(status="failed", loops=MAX_FORMAT_LOOPS)
        assert routing.route_after_format_check(s) == "pause_for_legacy_format_decision"

    def test_blocked_within_budget_pauses(self):
        s = self._state(status="blocked", loops=0)
        assert routing.route_after_format_check(s) == "pause_for_legacy_format_decision"

    def test_blocked_at_boundary_pauses(self):
        """Phase 2.9A.18: blocked + loops ≤ MAX_FORMAT_LOOPS 仍 pause"""
        s = self._state(status="blocked", loops=MAX_FORMAT_LOOPS)
        assert routing.route_after_format_check(s) == "pause_for_legacy_format_decision"

    def test_unknown_level_falls_back_to_pause(self):
        s = self._state(status="garbage_value")
        assert routing.route_after_format_check(s) == "pause_for_legacy_format_decision"

    def test_legacy_loss_detected_level_treated_as_blocked(self):
        """向后兼容 — 老 envelope 使用 ``level=loss_detected``,本路由识别为 blocked"""
        s = self._state(level="loss_detected", loops=0)
        assert routing.route_after_format_check(s) == "pause_for_legacy_format_decision"

    def test_status_takes_precedence_over_level(self):
        """status 与 level 同时存在时,status 优先(Phase 2.9A.18 规范)"""
        s = self._state(status="passed", level="failed", loops=5)
        # status=passed 应导向 finalize(而非按 level=failed 再试)
        assert routing.route_after_format_check(s) == "finalize_task"


# ── 单元: route_after_format_check_for_interrupt ──────────────────────────


class TestRouteAfterFormatCheckForInterrupt:
    def _state(self, status=None, level=None, loops=0):
        result = {}
        if status is not None:
            result["status"] = status
        if level is not None:
            result["level"] = level
        return {"format_check_result": result, "format_loop_count": loops}

    def test_passed_goes_directly_to_finalize(self):
        """Phase 2.9A.24: format passed → finalize_task(跳过 generate_completion_summary)。"""
        s = self._state(status="passed")
        assert (
            routing_after_interrupt.route_after_format_check_for_interrupt(s)
            == "finalize_task"
        )

    def test_warning_goes_directly_to_finalize(self):
        """Phase 2.9A.24: format warning → finalize_task(跳过 generate_completion_summary)。"""
        s = self._state(status="warning")
        assert (
            routing_after_interrupt.route_after_format_check_for_interrupt(s)
            == "finalize_task"
        )

    def test_blocked_within_budget_pauses(self):
        s = self._state(status="blocked", loops=0)
        assert (
            routing_after_interrupt.route_after_format_check_for_interrupt(s)
            == "format_loss_interrupt"
        )

    def test_blocked_at_boundary_pauses(self):
        s = self._state(status="blocked", loops=MAX_FORMAT_LOOPS)
        assert (
            routing_after_interrupt.route_after_format_check_for_interrupt(s)
            == "format_loss_interrupt"
        )

    def test_failed_with_budget_prepares_retry(self):
        s = self._state(status="failed", loops=0)
        assert (
            routing_after_interrupt.route_after_format_check_for_interrupt(s)
            == "prepare_export"
        )

    def test_failed_exhausted_budget_pauses(self):
        s = self._state(status="failed", loops=MAX_FORMAT_LOOPS)
        assert (
            routing_after_interrupt.route_after_format_check_for_interrupt(s)
            == "format_loss_interrupt"
        )

    def test_legacy_loss_detected_maps_to_blocked(self):
        s = self._state(level="loss_detected", loops=0)
        assert (
            routing_after_interrupt.route_after_format_check_for_interrupt(s)
            == "format_loss_interrupt"
        )

    def test_unknown_level_falls_back_to_interrupt(self):
        s = self._state(status="weird")
        assert (
            routing_after_interrupt.route_after_format_check_for_interrupt(s)
            == "format_loss_interrupt"
        )


# ── 节点层: check_docx_format_node 写 status 字段 ─────────────────────────


class TestCheckDocxFormatNodeStatusField:
    """Phase 2.9A.18 §4.2 format_check_result 同时含 status 与 level,
    routing 优先读 status,旧 envelope 只含 level 时仍可识别."""

    @pytest.mark.asyncio
    async def test_node_writes_status_alias_for_blocked(self):
        """Tool 返回 loss_detected → 节点 status 字段映射为 blocked"""
        from unittest.mock import AsyncMock, MagicMock

        from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
            check_docx_format_node,
        )
        from app.common.format_checker import FormatReport, HeadingCounts

        ctx = MagicMock()
        ctx.task_internal_id = 1
        ctx.conversation_internal_id = 1
        ctx.user_internal_id = 1
        sink = MagicMock()
        sink.emit = AsyncMock()
        ctx.event_sink = sink

        ok_report = FormatReport(
            headings=HeadingCounts(),
            table_count=0,
            table_column_counts=(),
            bookmark_names=(),
            field_count=0,
            hyperlink_count=0,
            header_text_count=0,
            footer_text_count=0,
            drifts=[],
        )
        data = ok_report.to_dict()
        # 模拟 DocxFormatCheckTool 返回 level=loss_detected(旧字面值)
        data["level"] = "loss_detected"
        data["losses"] = [{"element": "bookmark_X", "loss_class": "loss", "severity": "block"}]

        adapter = AsyncMock()
        adapter.execute = AsyncMock(
            return_value={"success": True, "data": data, "duration_ms": 10}
        )
        ctx.tool_adapter = adapter

        def _good_artifact():
            return {
                "public_id": "art_abc",
                "artifact_id": "art_abc",
                "artifact_type": "test_plan_word",
                "file_name": "x.docx",
                "file_ext": "docx",
                "mime_type": "x",
                "file_size": 1,
                "storage_path": "artifacts/1/x.docx",
            }

        state = {
            "artifact": _good_artifact(),
            "format_loop_count": 0,
            "completed_nodes": [],
        }

        result = await check_docx_format_node(state, ctx=ctx)
        # Phase 2.9A.18:status 字段映射 blocked,level 保留原值
        assert result["format_check_result"]["status"] == "blocked"
        assert result["format_check_result"]["level"] == "loss_detected"
        assert result["pending_format_losses"]  # losses 列表已透传
        assert len(result["pending_format_losses"]) >= 1

    @pytest.mark.asyncio
    async def test_node_writes_status_for_passed(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
            check_docx_format_node,
        )
        from app.common.format_checker import FormatReport, HeadingCounts

        ctx = MagicMock()
        ctx.task_internal_id = 2
        ctx.conversation_internal_id = 2
        ctx.user_internal_id = 2
        sink = MagicMock()
        sink.emit = AsyncMock()
        ctx.event_sink = sink

        ok_report = FormatReport(
            headings=HeadingCounts(),
            table_count=0,
            table_column_counts=(),
            bookmark_names=(),
            field_count=0,
            hyperlink_count=0,
            header_text_count=0,
            footer_text_count=0,
            drifts=[],
        )

        adapter = AsyncMock()
        adapter.execute = AsyncMock(
            return_value={"success": True, "data": ok_report.to_dict(), "duration_ms": 10}
        )
        ctx.tool_adapter = adapter

        def _good_artifact():
            return {
                "public_id": "art_abc",
                "artifact_id": "art_abc",
                "artifact_type": "test_plan_word",
                "file_name": "x.docx",
                "file_ext": "docx",
                "mime_type": "x",
                "file_size": 1,
                "storage_path": "artifacts/2/x.docx",
            }

        state = {
            "artifact": _good_artifact(),
            "format_loop_count": 0,
            "completed_nodes": [],
        }

        result = await check_docx_format_node(state, ctx=ctx)
        # 成功路径:status=passed,level=passed(向后兼容双字段)
        assert result["format_check_result"]["status"] == "passed"
        assert result["format_check_result"]["level"] == "passed"
        # 节点返回顶层 checked_artifact_public_id — DocxFormatCheckTool
        # envelope.data 也带这个字段(Phase 2.9A.17 §4.6)
        assert result["checked_artifact_public_id"] == "art_abc"
        # Tool payload 可能不写(若 artifact_struct 不在 inputs.artifact),
        # 但节点顶层 state["checked_artifact_public_id"] 一定有
        envelope_checked = result["format_check_result"].get("checked_artifact_public_id")
        assert envelope_checked in (None, "art_abc")  # 双轨并存


# ── Phase 2.9A.X: format_loss 入口 B 补 emit ─────────────────────────────


class TestCheckDocxFormatNodeEmitsFormatLossConfirm:
    """BUG FIX 2026-08-18 (A):DocxFormatCheckTool 检测到 ``loss_detected``
    时,barrier_path_map 在 nodes_interrupts.format_loss_interrupt_node
    interrupt() 之前**不**发 FORMAT_LOSS_CONFIRM_REQUESTED(发事件的是
    pause_for_legacy_format_decision_node,interrupt 模式下不被走到),
    导致前端收不到"需要确认"提示,任务静默挂起。

    本测试验证入口 B(check_docx_format_node)自身会发
    FORMAT_LOSS_CONFIRM_REQUESTED 事件,与入口 A(export_word_node)对称。
    """

    @pytest.mark.asyncio
    async def test_emits_format_loss_confirm_on_loss_detected(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
            check_docx_format_node,
        )
        from app.common.format_checker import FormatReport, HeadingCounts
        from app.agent.enums import AgentEventType

        ctx = MagicMock()
        ctx.task_internal_id = 7
        ctx.conversation_internal_id = 7
        ctx.user_internal_id = 7
        sink = MagicMock()
        sink.emit = AsyncMock()
        ctx.event_sink = sink

        ok_report = FormatReport(
            headings=HeadingCounts(),
            table_count=0,
            table_column_counts=(),
            bookmark_names=(),
            field_count=0,
            hyperlink_count=0,
            header_text_count=0,
            footer_text_count=0,
            drifts=[],
        )
        data = ok_report.to_dict()
        data["level"] = "loss_detected"
        data["losses"] = [{"element": "bookmark_X", "loss_class": "loss", "severity": "block"}]

        adapter = AsyncMock()
        adapter.execute = AsyncMock(
            return_value={"success": True, "data": data, "duration_ms": 10}
        )
        ctx.tool_adapter = adapter

        def _good_artifact():
            return {
                "public_id": "art_b",
                "artifact_id": "art_b",
                "artifact_type": "test_plan_word",
                "file_name": "x.docx",
                "file_ext": "docx",
                "mime_type": "x",
                "file_size": 1,
                "storage_path": "artifacts/7/x.docx",
            }

        state = {
            "artifact": _good_artifact(),
            "format_loop_count": 0,
            "completed_nodes": [],
        }
        await check_docx_format_node(state, ctx=ctx)

        # 验证 emit 至少被调用两次:DOCX_FORMAT_CHECKED + FORMAT_LOSS_CONFIRM_REQUESTED
        emit_calls = sink.emit.await_args_list
        event_types = [c.kwargs.get("event_type") or c.args[3] for c in emit_calls]
        assert AgentEventType.DOCX_FORMAT_CHECKED.value in event_types
        assert AgentEventType.FORMAT_LOSS_CONFIRM_REQUESTED.value in event_types

        # 验证 FORMAT_LOSS_CONFIRM_REQUESTED 的 payload 含 losses
        confirm_call = next(
            c for c in emit_calls
            if (c.kwargs.get("event_type") or c.args[3]) == AgentEventType.FORMAT_LOSS_CONFIRM_REQUESTED.value
        )
        payload = confirm_call.kwargs.get("payload") or confirm_call.args[5]
        assert "losses" in payload
        assert len(payload["losses"]) >= 1

    @pytest.mark.asyncio
    async def test_no_format_loss_confirm_on_passed(self):
        """passed 路径不应发 FORMAT_LOSS_CONFIRM_REQUESTED,只发 DOCX_FORMAT_CHECKED。"""
        from unittest.mock import AsyncMock, MagicMock

        from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
            check_docx_format_node,
        )
        from app.common.format_checker import FormatReport, HeadingCounts
        from app.agent.enums import AgentEventType

        ctx = MagicMock()
        ctx.task_internal_id = 8
        ctx.conversation_internal_id = 8
        ctx.user_internal_id = 8
        sink = MagicMock()
        sink.emit = AsyncMock()
        ctx.event_sink = sink

        ok_report = FormatReport(
            headings=HeadingCounts(),
            table_count=0,
            table_column_counts=(),
            bookmark_names=(),
            field_count=0,
            hyperlink_count=0,
            header_text_count=0,
            footer_text_count=0,
            drifts=[],
        )

        adapter = AsyncMock()
        adapter.execute = AsyncMock(
            return_value={"success": True, "data": ok_report.to_dict(), "duration_ms": 10}
        )
        ctx.tool_adapter = adapter

        def _good_artifact():
            return {
                "public_id": "art_p",
                "artifact_id": "art_p",
                "artifact_type": "test_plan_word",
                "file_name": "x.docx",
                "file_ext": "docx",
                "mime_type": "x",
                "file_size": 1,
                "storage_path": "artifacts/8/x.docx",
            }

        state = {
            "artifact": _good_artifact(),
            "format_loop_count": 0,
            "completed_nodes": [],
        }
        await check_docx_format_node(state, ctx=ctx)

        emit_calls = sink.emit.await_args_list
        event_types = [c.kwargs.get("event_type") or c.args[3] for c in emit_calls]
        assert AgentEventType.DOCX_FORMAT_CHECKED.value in event_types
        assert AgentEventType.FORMAT_LOSS_CONFIRM_REQUESTED.value not in event_types


# ── Phase 2.9A.18 §4.5 checked_artifact_public_id Artifact 版本绑定 ──────


class TestArtifactVersionBinding:
    """每次 format_check 必须记录 checked_artifact_public_id,新导出后不能继续读旧 Artifact。"""

    @pytest.mark.asyncio
    async def test_checked_artifact_public_id_persists_in_state(self):
        from unittest.mock import AsyncMock, MagicMock

        from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
            check_docx_format_node,
        )
        from app.common.format_checker import FormatReport, HeadingCounts

        ctx = MagicMock()
        ctx.task_internal_id = 5
        ctx.conversation_internal_id = 5
        ctx.user_internal_id = 5
        sink = MagicMock()
        sink.emit = AsyncMock()
        ctx.event_sink = sink

        ok_report = FormatReport(
            headings=HeadingCounts(),
            table_count=0,
            table_column_counts=(),
            bookmark_names=(),
            field_count=0,
            hyperlink_count=0,
            header_text_count=0,
            footer_text_count=0,
            drifts=[],
        )
        adapter = AsyncMock()
        adapter.execute = AsyncMock(
            return_value={"success": True, "data": ok_report.to_dict(), "duration_ms": 10}
        )
        ctx.tool_adapter = adapter

        # 模拟 re-export 后,新 Artifact public_id 不同
        new_artifact = {
            "public_id": "art_new_v2",
            "artifact_id": "art_new_v2",
            "artifact_type": "test_plan_word",
            "file_name": "x.docx",
            "file_ext": "docx",
            "mime_type": "x",
            "file_size": 1,
            "storage_path": "artifacts/5/v2/x.docx",
        }
        state = {
            "artifact": new_artifact,
            "format_loop_count": 1,  # 第二次循环
            "checked_artifact_public_id": "art_v1",  # 上一次的值
            "completed_nodes": [],
        }

        result = await check_docx_format_node(state, ctx=ctx)
        # 节点返回后,checked_artifact_public_id 必须更新为新 Artifact
        assert result["checked_artifact_public_id"] == "art_new_v2"
        # 节点级写入(graph_state 顶层)总是成功;envelope 级由 DocxFormatCheckTool
        # 在 inputs.artifact 缺失时不强写,不强求
