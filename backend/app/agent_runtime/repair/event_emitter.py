"""Repair Event Emitter (Phase 2.4).

6 个 SSE 事件 (Rule 7/11):
* ``REPAIR_STARTED``
* ``REPAIR_COMPLETED``
* ``REPAIR_FALLBACK``
* ``REPAIR_BUDGET_EXHAUSTED``
* ``REPAIR_REMEDIATION_APPLIED`` (可选)
* ``REPAIR_SKIPPED`` (可选, cancel 时)

载荷 sanitization:不包含 raw LLM text / CoT / 完整 args,只暴露
sanitized PublicSummary 与审计相关 metadata。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.agent.enums import AgentEventType
from app.agent_runtime._shared.public_narrative import (
    AgentObservation,
    build_narrative_envelope,
    deterministic_repair_fallback,
    observation_to_public_update,
)
from app.agent_runtime.feature_flags import get_feature_flags
from app.agent_runtime.runtime_context import RuntimeContext


logger = logging.getLogger(__name__)


class RepairEventEmitter:
    """Repair subgraph 用的 SSE 事件发射器;镜像 ``PreparationEventEmitter``。

    所有持久化事件都是 async 并直接 ``await event_sink.emit``(Phase 2.9B.2:
    不再 fire-and-forget,事件写完后才返回,保证 SSE 重连 / event-list / Hydrate
    历史恢复不丢事件)。异常仅 log 不抛,遵循项目现有 EventSink 策略。
    """

    def __init__(self, ctx: RuntimeContext, *, node_name: str = "repair_subgraph_step") -> None:
        self._ctx = ctx
        self._node_name = node_name

    @property
    def task_id(self) -> str:
        return str(getattr(self._ctx, "task_internal_id", ""))

    @property
    def graph_run_id(self) -> str:
        return f"run-{getattr(self._ctx, 'task_internal_id', '')}"

    async def emit_repair_started(self, *, issue_count: int, loop_count: int) -> None:
        """REPAIR_STARTED — Repair subgraph 入口。"""
        await self._ctx.event_sink.emit(
            task_id=self.task_id,
            graph_run_id=self.graph_run_id,
            node_name=self._node_name,
            event_type=AgentEventType.REPAIR_STARTED.value,
            title="修复阶段开始",
            content="",
            payload={
                "issue_count": issue_count,
                "loop_count": loop_count,
            },
        )

    async def emit_repair_completed(
        self,
        *,
        issues_resolved: int,
        issues_remaining: int,
        rounds_used: int,
        tool_calls_used: int,
        public_headline: str,
    ) -> None:
        """REPAIR_COMPLETED — Repair 主循环正常终止。"""
        await self._ctx.event_sink.emit(
            task_id=self.task_id,
            graph_run_id=self.graph_run_id,
            node_name=self._node_name,
            event_type=AgentEventType.REPAIR_COMPLETED.value,
            title="修复阶段完成",
            content="",
            payload={
                "issues_resolved": issues_resolved,
                "issues_remaining": issues_remaining,
                "rounds_used": rounds_used,
                "tool_calls_used": tool_calls_used,
                "public_headline": public_headline[:120],
            },
        )

    async def emit_repair_fallback(
        self,
        *,
        reason: str,
        fallback_target: str = "regenerate_sections_node",
    ) -> None:
        """REPAIR_FALLBACK — 失败兜底(永不抛, Rule 14)。"""
        await self._ctx.event_sink.emit(
            task_id=self.task_id,
            graph_run_id=self.graph_run_id,
            node_name=self._node_name,
            event_type=AgentEventType.REPAIR_FALLBACK.value,
            title="修复阶段降级",
            content="",
            payload={
                "reason": str(reason)[:240],
                "fallback_target": fallback_target,
            },
        )

    async def emit_repair_budget_exhausted(
        self,
        *,
        code: str,
        steps: int,
        tool_calls: int,
        wall_seconds: float,
    ) -> None:
        """REPAIR_BUDGET_EXHAUSTED — BudgetTracker 超限。"""
        await self._ctx.event_sink.emit(
            task_id=self.task_id,
            graph_run_id=self.graph_run_id,
            node_name=self._node_name,
            event_type=AgentEventType.REPAIR_BUDGET_EXHAUSTED.value,
            title="修复预算耗尽",
            content="",
            payload={
                "code": str(code)[:80],
                "steps": steps,
                "tool_calls": tool_calls,
                "wall_seconds": wall_seconds,
            },
        )

    async def emit_repair_remediation_applied(
        self,
        *,
        section_id: Optional[str],
        tool_name: str,
    ) -> None:
        """REPAIR_REMEDIATION_APPLIED — 单次修复操作施加成功。"""
        await self._ctx.event_sink.emit(
            task_id=self.task_id,
            graph_run_id=self.graph_run_id,
            node_name=self._node_name,
            event_type=AgentEventType.REPAIR_REMEDIATION_APPLIED.value,
            title="修复动作已施加",
            content="",
            payload={
                "section_id": section_id,
                "tool_name": tool_name,
            },
        )

    async def emit_repair_skipped(self, *, reason: str) -> None:
        """REPAIR_SKIPPED — Repair Agent 被取消 / 跳过(Rule 11 sanitized)。"""
        await self._ctx.event_sink.emit(
            task_id=self.task_id,
            graph_run_id=self.graph_run_id,
            node_name=self._node_name,
            event_type=AgentEventType.REPAIR_SKIPPED.value,
            title="修复阶段跳过",
            content="",
            payload={"reason": str(reason)[:240]},
        )

    async def emit_decision_update(self, *, step_index: int, decision) -> None:
        if not get_feature_flags().phase29b_narrative_enabled_for("RepairAgent"):
            return
        public_update = getattr(decision, "decision_update", None) or getattr(
            decision, "observation_update", None,
        )
        payload = build_narrative_envelope(
            update_kind="agent_decision",
            agent_name="RepairAgent",
            decision_id=f"{self.task_id}:repair:{step_index}",
            step_index=step_index,
            action=decision.action,
            tool_name=decision.tool_name,
            route="repair",
            public_update=public_update,
        )
        if not payload:
            # Phase 2.9B.3: 模型叙事缺失/不完整 → 确定性公开回退,不泄漏内部错误。
            payload = build_narrative_envelope(
                update_kind="agent_decision",
                agent_name="RepairAgent",
                decision_id=f"{self.task_id}:repair:{step_index}",
                step_index=step_index,
                action=decision.action,
                tool_name=decision.tool_name,
                route="repair",
                public_update=deterministic_repair_fallback(),
            )
            if payload:
                payload["fallback_used"] = True
                payload["narrative_source"] = "deterministic"
                payload["failure_category"] = (
                    "invalid_model_narrative"
                    if getattr(decision, "action", "") != "fail"
                    else "schema_validation_failed"
                )
        if payload:
            await self._ctx.event_sink.emit(
                task_id=self.task_id,
                graph_run_id=self.graph_run_id,
                node_name=self._node_name,
                event_type=AgentEventType.AGENT_DECISION_UPDATE.value,
                title=payload["public_update"]["headline"],
                content=payload["public_update"]["summary"],
                payload=payload,
            )

    async def emit_observation_update(
        self,
        *,
        step_index: int,
        tool_name: str,
        success: bool,
        summary: str,
        error_code: str | None = None,
    ) -> None:
        if not get_feature_flags().phase29b_narrative_enabled_for("RepairAgent"):
            return
        observation = AgentObservation(
            tool_name=tool_name,
            success=success,
            error_code=error_code,
            result_excerpt=summary,
        )
        payload = build_narrative_envelope(
            update_kind="agent_observation",
            agent_name="RepairAgent",
            decision_id=f"{self.task_id}:repair:observation:{step_index}",
            step_index=step_index,
            action="observe_tool_result",
            tool_name=tool_name,
            route="repair",
            public_update=observation_to_public_update(observation),
        )
        if payload:
            await self._ctx.event_sink.emit(
                task_id=self.task_id,
                graph_run_id=self.graph_run_id,
                node_name=self._node_name,
                event_type=AgentEventType.AGENT_OBSERVATION_UPDATE.value,
                title=payload["public_update"]["headline"],
                content=payload["public_update"]["summary"],
                payload=payload,
            )


__all__ = ["RepairEventEmitter"]


# module-level note (auto-appended):
# RepairEventEmitter — 修复事件输出。
# emit 链路: agent emit → event_sink + Decision + audit。
# 关键约束: 不允许 raw LLM 输出流进 event。
