"""Incremental Agent EventEmitter (Phase 2.5).

增量事件(纯 additive,不破坏现有事件):

1. ``INCREMENTAL_STARTED`` — subgraph 入口
2. ``INCREMENTAL_DECISION_MADE`` — 单步 LLM 决策(载荷 sanitized)
3. ``INCREMENTAL_TOOL_FINISHED`` — 单工具调用成功
4. ``INCREMENTAL_TOOL_BLOCKED`` — 拦截(banned / scope / schema)
5. ``INCREMENTAL_COMPLETED`` — 成功完成,带 version_no+1 artifact 信息
6. ``INCREMENTAL_FALLBACK`` — 降级到 legacy regen + re-export
7. ``PLAN_CREATED`` — 增量任务的可见执行计划
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from app.agent_runtime.runtime_context import RuntimeContext

from app.agent_runtime._shared.public_narrative import (
    AgentObservation,
    build_narrative_envelope,
    deterministic_incremental_fallback,
    observation_to_public_update,
)
from app.agent_runtime.feature_flags import get_feature_flags


# EventType 常量(增量 SSE 事件;不入 AgentEventType 枚举以保持纯 additive;
# 与 preparation/repair 一致地通过 event_sink.emit 走 enum 表)
_INCREMENTAL_EVENT_STARTED = "incremental_started"
_INCREMENTAL_EVENT_DECISION = "incremental_decision_made"
_INCREMENTAL_EVENT_TOOL_FINISHED = "incremental_tool_finished"
_INCREMENTAL_EVENT_TOOL_BLOCKED = "incremental_tool_blocked"
_INCREMENTAL_EVENT_COMPLETED = "incremental_completed"
_INCREMENTAL_EVENT_FALLBACK = "incremental_fallback"
_INCREMENTAL_EVENT_FAILED = "incremental_failed"


class IncrementalEventEmitter:
    """包装 ``event_sink.emit`` 的便捷类(载荷 sanitized)。

    用法:
        emitter = IncrementalEventEmitter(ctx, node_name=NODE_INCREMENTAL_SUBGRAPH)
        await emitter.emit_started(task_id=..., headline="...")
    """

    def __init__(
        self,
        ctx: "RuntimeContext",
        *,
        node_name: str,
    ) -> None:
        self._ctx = ctx
        self._node_name = node_name

    async def emit_started(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        intent,
    ) -> Optional[Dict[str, Any]]:
        started = await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type=_INCREMENTAL_EVENT_STARTED,
            title="Incremental Agent 已启动",
            content=(
                f"kind={intent.scope.kind}; "
                f"target_sections={intent.scope.target_section_ids}; "
                f"source_artifact={intent.existing_artifact.artifact_public_id};"
                f" version_no={intent.existing_artifact.version_no}"
            ),
            payload={
                "intent_kind": intent.scope.kind,
                "target_section_ids": intent.scope.target_section_ids,
                "source_artifact_public_id": intent.existing_artifact.artifact_public_id,
                "source_version_no": intent.existing_artifact.version_no,
                "confidence": intent.confidence,
            },
        )
        # Incremental execution does not run the normal preparation graph,
        # so it must publish its own plan for the task card.  Keep this a
        # separate event so the existing started event remains compatible.
        target_section_ids = [str(section_id) for section_id in (intent.scope.target_section_ids or [])]
        target_sections = ", ".join(target_section_ids) or "用户指定章节"
        await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type="plan_created",
            title="增量执行计划",
            content="",
            payload={
                "plan": {
                    "steps": [
                        {
                            "step_id": "generate",
                            "name": "局部重写目标章节",
                            "detail": f"仅修改 {target_sections}，保持模板结构不变。",
                            "status": "pending",
                        },
                        {
                            "step_id": "review",
                            "name": "复审增量修改结果",
                            "detail": "检查目标章节内容是否满足用户的字数与语义要求。",
                            "status": "pending",
                        },
                        {
                            "step_id": "export",
                            "name": "导出新版本 Word",
                            "detail": "通过格式自检后生成新的 Word 产物。",
                            "status": "pending",
                        },
                    ]
                }
            },
        )
        return started

    async def emit_decision_made(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        decision,
    ) -> Optional[Dict[str, Any]]:
        return await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type=_INCREMENTAL_EVENT_DECISION,
            title=f"Incremental decision: {decision.action}",
            content=decision.public_update or decision.decision_summary,
            payload={
                "action": decision.action,
                "tool_name": decision.tool_name,
                "scope_kind": decision.scope_kind,
                "target_section_ids": decision.target_section_ids,
                "decision_summary": decision.decision_summary[:500],
                "confidence": decision.confidence,
            },
        )

    async def emit_tool_finished(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        tool_name: str,
        success: bool,
        summary: str,
    ) -> Optional[Dict[str, Any]]:
        return await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type=_INCREMENTAL_EVENT_TOOL_FINISHED,
            title=f"Tool finished: {tool_name}",
            content=summary,
            payload={
                "tool_name": tool_name,
                "success": success,
                "summary": summary[:480],
            },
        )

    async def emit_tool_blocked(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        tool_name: str,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        return await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type=_INCREMENTAL_EVENT_TOOL_BLOCKED,
            title=f"Tool blocked: {tool_name}",
            content=reason[:240],
            payload={
                "tool_name": tool_name,
                "reason": reason[:240],
            },
        )

    async def emit_completed(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        result,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        payload = {
            "success": result.success,
            "new_artifact_public_id": result.new_artifact_public_id,
            "new_artifact_version_no": result.new_artifact_version_no,
            "superseded_artifact_public_ids": result.superseded_artifact_public_ids,
            "modified_section_ids": result.modified_section_ids,
            "tool_calls_used": result.tool_calls_used,
            "rounds_used": result.rounds_used,
            "headline": result.public_summary.headline,
            "detail": result.public_summary.detail,
        }
        # The incremental event remains additive, but terminal presentation
        # metadata must travel with it so a refresh/replay can render the same
        # summary and artifact cards as the ordinary flow.
        if extra:
            payload.update(extra)
        return await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type=_INCREMENTAL_EVENT_COMPLETED,
            title=result.public_summary.headline,
            content=result.public_summary.detail,
            payload=payload,
        )

    async def emit_fallback(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        reason: str,
    ) -> Optional[Dict[str, Any]]:
        return await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type=_INCREMENTAL_EVENT_FALLBACK,
            title="Incremental Agent 已降级到 legacy regen+re-export",
            content=reason[:240],
            payload={
                "reason": reason[:240],
            },
        )

    async def emit_failed(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        reason: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        return await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type=_INCREMENTAL_EVENT_FAILED,
            title="增量修改任务失败",
            content=reason[:240],
            payload={
                "reason": reason[:240],
                "headline": "增量修改任务失败",
                "detail": reason[:240],
                "extra": extra or {},
            },
        )

    async def emit_public_decision_update(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        step_index: int,
        decision,
    ) -> Optional[Dict[str, Any]]:
        if not get_feature_flags().phase29b_narrative_enabled_for("IncrementalAgent"):
            return None
        public_update = getattr(decision, "decision_update", None) or getattr(
            decision, "observation_update", None,
        )
        payload = build_narrative_envelope(
            update_kind="agent_decision",
            agent_name="IncrementalAgent",
            decision_id=f"{task_id}:incremental:{step_index}",
            step_index=step_index,
            action=decision.action,
            tool_name=decision.tool_name,
            route="incremental",
            public_update=public_update,
        )
        if not payload:
            # Phase 2.9B.3: 模型叙事缺失/不完整 → 确定性公开回退,不泄漏内部错误。
            payload = build_narrative_envelope(
                update_kind="agent_decision",
                agent_name="IncrementalAgent",
                decision_id=f"{task_id}:incremental:{step_index}",
                step_index=step_index,
                action=decision.action,
                tool_name=decision.tool_name,
                route="incremental",
                public_update=deterministic_incremental_fallback(),
            )
            if payload:
                payload["fallback_used"] = True
                payload["narrative_source"] = "deterministic"
                payload["failure_category"] = (
                    "invalid_model_narrative"
                    if getattr(decision, "action", "") != "fail"
                    else "schema_validation_failed"
                )
        if not payload:
            return None
        return await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type="agent_decision_update",
            title=payload["public_update"]["headline"],
            content=payload["public_update"]["summary"],
            payload=payload,
        )

    async def emit_public_observation_update(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        step_index: int,
        tool_name: str,
        success: bool,
        summary: str,
        error_code: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        if not get_feature_flags().phase29b_narrative_enabled_for("IncrementalAgent"):
            return None
        observation = AgentObservation(
            tool_name=tool_name,
            success=success,
            error_code=error_code,
            result_excerpt=summary,
        )
        payload = build_narrative_envelope(
            update_kind="agent_observation",
            agent_name="IncrementalAgent",
            decision_id=f"{task_id}:incremental:observation:{step_index}",
            step_index=step_index,
            action="observe_tool_result",
            tool_name=tool_name,
            route="incremental",
            public_update=observation_to_public_update(observation),
        )
        if not payload:
            return None
        return await self._emit(
            task_id=task_id,
            graph_run_id=graph_run_id,
            event_type="agent_observation_update",
            title=payload["public_update"]["headline"],
            content=payload["public_update"]["summary"],
            payload=payload,
        )

    async def _emit(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        event_type: str,
        title: str,
        content: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """委托给 ``event_sink.emit``;失败仅 log,不抛(rule 14)。"""
        try:
            return await self._ctx.event_sink.emit(
                task_id=task_id,
                graph_run_id=graph_run_id,
                node_name=self._node_name,
                event_type=event_type,
                title=title,
                content=content,
                payload=payload or {},
            )
        except Exception:
            # 镜像 preparation / repair emitter:失败仅 log,绝不抛
            import logging
            logging.getLogger(__name__).warning(
                "incremental_event_emit_failed event_type=%s", event_type,
                exc_info=True,
            )
            return None


__all__ = ["IncrementalEventEmitter"]


# module-level note (auto-appended):
# IncrementalEventEmitter — 增量事件输出。
# 新增 6 个 SSE 事件(INCREMENTAL_STARTED / COMPLETED / FALLBACK / BUDGET_EXHAUSTED / SCOPE_CONFIRM_REQUESTED / SCOPE_DECISION_RECORDED)。
