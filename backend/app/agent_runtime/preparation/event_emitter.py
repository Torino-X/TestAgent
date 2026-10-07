"""PreparationEventEmitter — 6 SSE 事件 emit (Phase 2.3)。

所有 emit 走 ctx.event_sink.emit(), payload **必须 sanitized**:
* 不包含 raw LLM 文本 / CoT / 内部 reasoning
* 仅 headline / decision_summary (≤500) / tool_name / evidence_count /
  public_summary (PublicSummary Pydantic 校验后) / reason codes

对应 6 个 AgentEventType:
* PREPARATION_STARTED          — 子图入口
* PREPARATION_COMPLETED        — 子图正常完成
* PREPARATION_FALLBACK         — 异常回退到 legacy single-shot KB
* PREPARATION_BUDGET_EXHAUSTED — 预算触达上限
* KNOWLEDGE_INSUFFICIENT       — KB 检索证据不足以支撑决策
* KNOWLEDGE_SKIPPED            — KB 未配置 / 跳过
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.agent.enums import AgentEventType
from app.agent_runtime._shared.public_narrative import (
    AgentObservation,
    build_narrative_envelope,
    deterministic_preparation_fallback,
    observation_to_public_update,
)
from app.agent_runtime.feature_flags import get_feature_flags
from app.agent_runtime.runtime_context import RuntimeContext

from .schemas import PreparationResult

logger = logging.getLogger(__name__)


class PreparationEventEmitter:
    """封装 6 个 Preparation Agent SSE 事件。"""

    def __init__(
        self,
        ctx: RuntimeContext,
        *,
        node_name: str = "preparation_subgraph",
    ):
        self._ctx = ctx
        self._node_name = node_name

    async def _emit(self, event_type: AgentEventType, payload: Dict[str, Any]) -> None:
        """统一 emit 入口(直接 await event_sink.emit,不 fire-and-forget)。

        Phase 2.9B.2: 持久化语义 — 事件必须写完后才返回,否则 SSE 重连 /
        event-list / Hydrate 历史恢复会丢事件(preparation_started 曾因
        loop.create_task 竞态未持久化)。失败仅 log 不抛,遵循项目现有
        EventSink 策略(不改变主任务错误等级)。
        """
        try:
            await self._ctx.event_sink.emit(
                task_id=str(self._ctx.task_internal_id),
                graph_run_id=f"run-{self._ctx.task_internal_id}",
                node_name=self._node_name,
                event_type=event_type.value,
                title=payload.get("headline", event_type.value),
                content="",
                payload=payload,
            )
        except Exception as exc:  # never let SSE failure cascade
            logger.warning("preparation event emit failed: %s", exc)

    # ── 6 events ──────────────────────────────────────────────────────

    async def emit_preparation_started(self, capability_summary: str) -> None:
        await self._emit(
            AgentEventType.PREPARATION_STARTED,
            {
                "headline": "准备阶段开始",
                "capability_summary": capability_summary[:200],
            },
        )

    async def emit_preparation_completed(self, result: PreparationResult) -> None:
        """载荷严格来自 Pydantic 模型 — 没有 raw LLM text / CoT."""
        # Strip raw decision_summary / CoT; only emit sanitized summary
        await self._emit(
            AgentEventType.PREPARATION_COMPLETED,
            {
                "headline": result.public_summary.headline,
                "detail": result.public_summary.detail,
                "information_sufficient": result.information_sufficient,
                "knowledge_search_used": result.knowledge_search_used,
                "queries_count": len(result.queries),
                "evidence_count": len(result.evidence),
                "requirement_gaps_count": len(result.requirement_gaps),
                "user_questions_count": len(result.user_questions),
                "confidence": result.confidence,
            },
        )

    async def emit_knowledge_insufficient(self, queries_attempted: int) -> None:
        await self._emit(
            AgentEventType.KNOWLEDGE_INSUFFICIENT,
            {
                "headline": "知识库证据不足",
                "queries_attempted": queries_attempted,
            },
        )

    async def emit_knowledge_skipped(self, reason: str) -> None:
        await self._emit(
            AgentEventType.KNOWLEDGE_SKIPPED,
            {
                "headline": "知识库检索跳过",
                "reason": (reason or "unspecified")[:200],
            },
        )

    async def emit_preparation_fallback(self, reason: str) -> None:
        await self._emit(
            AgentEventType.PREPARATION_FALLBACK,
            {
                "headline": "准备阶段回退到 legacy 单次检索",
                "reason": (reason or "unspecified")[:200],
            },
        )

    async def emit_preparation_budget_exhausted(self, code: str) -> None:
        await self._emit(
            AgentEventType.PREPARATION_BUDGET_EXHAUSTED,
            {
                "headline": "准备阶段预算耗尽",
                "code": code,
            },
        )

    async def emit_decision_update(
        self,
        *,
        decision_id: str,
        step_index: int,
        action: str,
        tool_name: str | None,
        public_update: Any,
        failure_category: str | None = None,
    ) -> None:
        """Emit the optional Phase 2.9B public decision envelope.

        Phase 2.9B.3: 只允许把模型生成的合法 decision_update/observation_update
        作为动态叙事。若模型叙事缺失或校验不完整(build_narrative_envelope 返回
        None),改发**确定性公开回退**,绝不把 decision_summary 里的内部错误
        (Schema error / Pydantic / action_reason / Traceback)泄漏给用户。
        """
        if not get_feature_flags().phase29b_narrative_enabled_for("PreparationAgent"):
            return
        payload = build_narrative_envelope(
            update_kind="agent_decision",
            agent_name="PreparationAgent",
            decision_id=decision_id,
            step_index=step_index,
            action=action,
            tool_name=tool_name,
            route="execute_tool" if action == "call_tool" else action,
            public_update=public_update,
        )
        if not payload:
            # 模型叙事缺失 / 不完整 → 确定性公开回退(含完整五字段)。
            payload = build_narrative_envelope(
                update_kind="agent_decision",
                agent_name="PreparationAgent",
                decision_id=decision_id,
                step_index=step_index,
                action=action,
                tool_name=tool_name,
                route="execute_tool" if action == "call_tool" else action,
                public_update=deterministic_preparation_fallback(),
            )
            if payload:
                payload["fallback_used"] = True
                payload["narrative_source"] = "deterministic"
                payload["failure_category"] = failure_category or "invalid_model_narrative"
        if payload:
            await self._emit(AgentEventType.AGENT_DECISION_UPDATE, payload)

    async def emit_observation_update(
        self,
        *,
        decision_id: str,
        step_index: int,
        observation: AgentObservation | dict[str, Any],
        public_update: Any = None,
    ) -> None:
        """Emit bounded facts after a tool call, never raw tool output."""
        if not get_feature_flags().phase29b_narrative_enabled_for("PreparationAgent"):
            return
        public_update = public_update or observation_to_public_update(observation)
        item = observation if isinstance(observation, AgentObservation) else AgentObservation.model_validate(observation)
        payload = build_narrative_envelope(
            update_kind="agent_observation",
            agent_name="PreparationAgent",
            decision_id=decision_id,
            step_index=step_index,
            action="observe",
            tool_name=item.tool_name,
            route="continue" if item.success else "retry_or_fallback",
            public_update=public_update,
        )
        if payload:
            await self._emit(AgentEventType.AGENT_OBSERVATION_UPDATE, payload)


__all__ = ["PreparationEventEmitter"]


# module-level note (auto-appended):
# PreparationEventEmitter — 事件输出层。
# emit 链路: agent emit → event_sink + Decision + audit。
# 关键约束: 不允许 raw LLM 输出流进 event。
