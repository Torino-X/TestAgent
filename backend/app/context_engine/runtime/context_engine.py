"""ContextEngine Facade — 统一管线入口。

CE-02 WP-8：时序严格按设计 05 §49 + WP-7 统一生命周期：
validate flags → scope resolve → profile resolve → model capability resolve →
plan → collect/select/dedup/quota → composer → validator → begin_build（写
完整 compose metadata）→ mark_ready。**不调业务 LLM**。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.context_engine.debug.prompt_dumper import ContextPromptDumper
from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.compose import ContextComposeResult
from app.context_engine.models.context import ContextPlan, ContextRequest, ContextScope
from app.context_engine.models.selection import DroppedContextRef
from app.context_engine.models.snapshot_models import (
    ContextBuildLatency,
    ContextSnapshotBeginCommand,
    ContextSnapshotRef,
)
from app.context_engine.models.source import LockedSection
from app.context_engine.planning.planner import ContextPlanner
from app.context_engine.scope.resolver import ContextScopeResolver
from app.context_engine.selection.dedup import ContextDeduplicator
from app.context_engine.selection.injection_filter import tag_prompt_injection
from app.context_engine.selection.quota import SourceQuotaEnforcer, SourceQuotaPolicy
from app.context_engine.selection.selector import ContextSelector
from app.context_engine.sources.orchestrator import SourceCollectionOutcome, SourceOrchestrator
from app.context_engine.sources.registry import SourceAdapterRegistry
from app.context_engine.composer.composer import ContextComposer
from app.context_engine.composer.validator import ComposeValidator
from app.context_engine.compression.preflight_service import ContextPreflightService
from app.context_engine.models.enums import CompactionTriggerType
from app.core.logging import LogEvent, log_event


logger = logging.getLogger(__name__)


class ContextEngine:
    """Context 构建 Facade：scope → profile → plan → collect → select → preflight → compose → snapshot。"""

    def __init__(
        self,
        *,
        planner: ContextPlanner,
        scope_resolver: ContextScopeResolver,
        source_orchestrator: SourceOrchestrator,
        selector: ContextSelector,
        deduplicator: ContextDeduplicator,
        quota_enforcer: SourceQuotaEnforcer,
        composer: ContextComposer,
        validator: ComposeValidator,
        snapshot_writer,
        token_counter=None,
        preflight: ContextPreflightService | None = None,
        prompt_dumper: ContextPromptDumper | None = None,
    ) -> None:
        self._planner = planner
        self._scope_resolver = scope_resolver
        self._source_orchestrator = source_orchestrator
        self._selector = selector
        self._deduplicator = deduplicator
        self._quota_enforcer = quota_enforcer
        self._composer = composer
        self._validator = validator
        self._snapshot_writer = snapshot_writer
        self._token_counter = token_counter
        self._preflight = preflight
        self._prompt_dumper = prompt_dumper

    async def compose(
        self,
        request: ContextRequest,
        *,
        runtime_context,
        execution_mode: str = "active",
    ) -> ContextComposeResult:
        try:
            from app.core.observability import start_span
            with start_span(
                "context.compose",
                {"context.call_site": request.call_site, "context.execution_mode": execution_mode},
            ):
                return await self._compose(
                    request,
                    runtime_context=runtime_context,
                    execution_mode=execution_mode,
                )
        except Exception as exc:
            log_event(
                logging.getLogger("testagent.context"),
                logging.ERROR,
                LogEvent.CONTEXT_COMPOSE_FAILED,
                "Context composition failed",
                call_site=request.call_site,
                error_type=type(exc).__name__,
            )
            raise

    async def preview(
        self,
        request: ContextRequest,
        *,
        runtime_context,
    ) -> ContextComposeResult:
        """Build a read-only estimate for the next model request.

        The preview reuses production source collection, retrieval, selection
        and deterministic preflight, while deliberately avoiding snapshots,
        provider calls and any durable compaction mutation.
        """
        return await self.compose(
            request,
            runtime_context=runtime_context,
            execution_mode="preview",
        )

    async def _compose(
        self,
        request: ContextRequest,
        *,
        runtime_context,
        execution_mode: str = "active",
    ) -> ContextComposeResult:
        started = _now_ms()

        # 1. scope resolve
        scope = self._scope_resolver.resolve(request)

        # 2. profile resolve + plan
        planning_started = _now_ms()
        model_context_window = await self._resolve_runtime_model_window(
            request,
            runtime_context,
        )
        # Source adapters run after planning and may need the effective model
        # capability for admission decisions.  Keep that runtime-resolved
        # value on this request only; it is never persisted as source data.
        if request.model_context_window != model_context_window:
            request = request.model_copy(
                update={"model_context_window": model_context_window}
            )
        plan = self._planner.plan(
            request,
            model_context_window=model_context_window,
        )

        # Materialize the canonical conversation working set once for a real
        # normal-chat request.  Task-internal profiles deliberately do not get
        # this field: they continue to use their own compact projections.
        if execution_mode == "active" and request.call_site == "chat.reply":
            request = await self._attach_conversation_ledger_tokens(
                request,
                runtime_context=runtime_context,
                context_window_tokens=plan.model_context_window,
            )
        log_event(
            logging.getLogger("testagent.context"),
            logging.INFO,
            LogEvent.CONTEXT_COMPOSE_STARTED,
            "Context composition started",
            call_site=request.call_site,
            profile_key=getattr(plan, "profile_key", None),
        )
        planning_ms = _now_ms() - planning_started

        # 3. collect
        source_started = _now_ms()
        outcome: SourceCollectionOutcome = await self._source_orchestrator.collect(
            request, plan, scope, runtime_context=runtime_context
        )
        source_gathering_ms = _now_ms() - source_started
        if outcome.required_failure:
            raise_engine_error(
                code=outcome.required_failure,
                detail="Required Source 收集失败",
                stage=ContextEngineStage.SOURCE,
                retryable=False,
                recoverable=True,
            )
        if outcome.cancelled:
            raise_engine_error(
                code="context.source.cancelled",
                detail="Source 收集阶段已取消（begin 前无 Snapshot）",
                stage=ContextEngineStage.SOURCE,
                retryable=False,
                recoverable=True,
            )

        # 4. dedup + injection tag
        # WP-BE-09 fix: ContextDeduplicator 实例由 facade 复用，其 _seen_* 集合是
        # 进程级状态。若不在每次 compose 入口重置，跨 compose 调用的相同 source_ref
        # （如 CURRENT_GOAL adapter 永远使用 ``current_user_message``）会被
        # 误判为 duplicate，导致 Required Section 在第二次及之后的调用中
        # 变成空 → selector 抛 ``context.selection.required_unmet``。
        selection_started = _now_ms()
        self._deduplicator.reset()
        by_section_dedup: dict[str, list] = {}
        preselection_dropped: list[DroppedContextRef] = []
        locked_sections: list[LockedSection] = list(outcome.locked_sections)
        for section_id, items in outcome.by_section.items():
            tagged = tag_prompt_injection(items)
            included, dedup_dropped = self._deduplicator.dedup(tagged)
            by_section_dedup[section_id] = included
            preselection_dropped.extend(dedup_dropped)

        # 5. quota + select
        # The normal prompt stays source-quota bounded until its durable
        # conversation ledger crosses the retention waterline.  On that one
        # transition request, preflight must see the complete raw prefix in
        # order to summarize it; after compaction only the summary and raw
        # tail remain in the composed prompt.
        retention_due = _conversation_retention_due(request, plan, execution_mode)
        by_section_dedup, contract_dropped = _apply_source_contracts(
            plan,
            by_section_dedup,
            quota_enforcer=self._quota_enforcer,
            locked_sections=locked_sections,
            quota_exempt_source_types=(
                {"conversation", "conversation_summary"} if retention_due else None
            ),
        )
        preselection_dropped.extend(contract_dropped)
        selected = self._selector.select(
            plan,
            by_section_dedup,
            locked_sections=locked_sections,
        )
        if preselection_dropped:
            selected = selected.model_copy(
                update={"dropped": [*preselection_dropped, *selected.dropped]}
            )
        selection_ms = _now_ms() - selection_started

        # 5a. Preflight（Compression，CE-04）：select 后、compose 前
        preflight_started = _now_ms()
        preflight_result = None
        if self._preflight is not None and not request.call_site.startswith("compression."):
            preflight_kwargs = {
                "request": request,
                "plan": plan,
                "selected": selected,
                "runtime_context": runtime_context,
                "trigger": CompactionTriggerType.PREFLIGHT,
            }
            if execution_mode == "preview":
                preflight_kwargs["preview"] = True
            preflight_result = await self._preflight.run(**preflight_kwargs)
            selected = preflight_result.selected
        preflight_ms = _now_ms() - preflight_started

        # 6. compose + validate
        compose_started = _now_ms()
        composed = self._composer.compose(
            request,
            selected,
            locked_sections=locked_sections,
        )
        validation = self._validator.validate(
            composed,
            absolute_threshold=int(plan.absolute_threshold or 0) or None,
        )
        preview_requires_compaction = bool(
            execution_mode == "preview"
            and getattr(preflight_result, "blocked_reason", None)
            == "preview_requires_compaction"
        )
        if validation.failure_code and not preview_requires_compaction:
            raise_engine_error(
                code=validation.failure_code,
                detail="Compose 验证未通过（Absolute 超限等）",
                stage=ContextEngineStage.PREFLIGHT,
                retryable=True,
                recoverable=True,
            )
        compose_ms = _now_ms() - compose_started
        latency = ContextBuildLatency(
            planning_ms=planning_ms,
            source_gathering_ms=source_gathering_ms,
            selection_ms=selection_ms,
            preflight_ms=preflight_ms,
            compose_ms=compose_ms,
        )
        composed = composed.model_copy(
            update={
                "build_latency_ms": max(latency.total_ms, _now_ms() - started),
                "retrieval_run_ids": list(outcome.retrieval_run_ids),
                "degraded": bool(outcome.degraded or selected.dropped),
                "execution_mode": execution_mode,
                "preflight": _preflight_telemetry(
                    plan, preflight_result, request=request,
                    trigger=CompactionTriggerType.PREFLIGHT,
                ),
            }
        )

        if execution_mode == "preview":
            return composed

        # 7. begin_build（写完整 compose metadata）→ mark_ready
        snapshot_ref = await self._begin_and_ready(
            request,
            scope,
            plan,
            composed,
            selected,
            locked_sections,
            execution_mode,
            runtime_context,
            latency=latency,
            preflight_telemetry=_preflight_telemetry(
                plan, preflight_result, request=request,
                trigger=CompactionTriggerType.PREFLIGHT,
            ),
        )

        composed = composed.model_copy(update={"snapshot_public_id": snapshot_ref.public_id})
        self._dump_prompt(
            request=request,
            plan=plan,
            composed=composed,
            snapshot_public_id=snapshot_ref.public_id,
            execution_mode=execution_mode,
        )
        log_event(
            logging.getLogger("testagent.context"),
            logging.INFO,
            LogEvent.CONTEXT_COMPOSE_COMPLETED,
            "Context composition completed",
            call_site=request.call_site,
            profile_key=getattr(plan, "profile_key", None),
            snapshot_id=snapshot_ref.public_id,
            sources_selected_count=len(selected.included),
            sources_dropped_count=len(selected.dropped),
            estimated_tokens=getattr(composed, "estimated_tokens", None),
            duration_ms=_now_ms() - started,
        )
        return composed

    async def _attach_conversation_ledger_tokens(
        self,
        request: ContextRequest,
        *,
        runtime_context,
        context_window_tokens: int,
    ) -> ContextRequest:
        """Fail-open bridge from the persistent ledger to chat preflight."""
        conversation_id = request.conversation_id
        if not conversation_id or not str(conversation_id).isdigit():
            # Chat bridges created by older/alternate call sites may only put
            # the internal identity on runtime_context.  Never let a public
            # ``conv_*`` identifier silently disable the durable ledger.
            conversation_id = getattr(runtime_context, "conversation_internal_id", None)
        session_factory = getattr(runtime_context, "session_factory", None)
        user_id = getattr(runtime_context, "user_internal_id", None)
        if not conversation_id or not str(conversation_id).isdigit() or session_factory is None or not user_id:
            return request
        try:
            from app.models.conversation import Conversation
            from app.services.conversation_context_ledger_service import (
                ConversationContextLedgerService,
            )

            async with session_factory() as session:
                conversation = (await session.execute(
                    select(Conversation).where(
                        Conversation.id == int(conversation_id),
                        Conversation.user_id == int(user_id),
                        Conversation.deleted_at.is_(None),
                    )
                )).scalar_one_or_none()
                if conversation is None:
                    return request
                ledger = await ConversationContextLedgerService(session).materialize(
                    conversation=conversation,
                    user_id=int(user_id),
                    context_window_tokens=int(context_window_tokens),
                )
                await session.commit()
            return request.model_copy(
                update={"conversation_ledger_tokens": int(ledger.total_tokens)}
            )
        except Exception as exc:  # noqa: BLE001 - never block the real request
            logger.warning(
                "CONVERSATION_CONTEXT_LEDGER_ATTACH_FAILED | call_site=%s | error=%s",
                request.call_site, type(exc).__name__,
            )
            return request

    async def compose_for_retry(
        self,
        request: ContextRequest,
        *,
        runtime_context,
        previous_snapshot_ref: ContextSnapshotRef,
    ) -> ContextComposeResult:
        """Context Length Retry：确定性降容后重 compose → 新 Snapshot。

        复用已有 Summary / 删 Optional / 降 quota / 减 recent turns；
        **不复制旧 Prompt 全文，无 compress/summarize 占位**。
        """
        started = _now_ms()
        scope = self._scope_resolver.resolve(request)
        planning_started = _now_ms()
        model_context_window = await self._resolve_runtime_model_window(
            request,
            runtime_context,
        )
        if request.model_context_window != model_context_window:
            request = request.model_copy(
                update={"model_context_window": model_context_window}
            )
        plan = self._planner.plan(
            request,
            model_context_window=model_context_window,
        )

        # 降容：Optional 全部降级（budget 减半），recent turns 减半
        reduced_plan = _reduce_plan_for_retry(plan)
        planning_ms = _now_ms() - planning_started

        source_started = _now_ms()
        outcome = await self._source_orchestrator.collect(
            request, reduced_plan, scope, runtime_context=runtime_context
        )
        source_gathering_ms = _now_ms() - source_started
        if outcome.required_failure:
            raise_engine_error(
                code=outcome.required_failure,
                detail="Retry 的 Required Source 收集失败",
                stage=ContextEngineStage.SOURCE,
                retryable=False,
                recoverable=True,
            )

        selection_started = _now_ms()
        by_section_dedup: dict[str, list] = {}
        preselection_dropped: list[DroppedContextRef] = []
        locked_sections = list(outcome.locked_sections)
        # WP-BE-09 fix: 同 compose()，compose_for_retry 入口也重置 dedup 状态，
        # 避免原 compose() 的 seen 污染 retry 路径。
        self._deduplicator.reset()
        for section_id, items in outcome.by_section.items():
            tagged = tag_prompt_injection(items)
            included, dedup_dropped = self._deduplicator.dedup(tagged)
            by_section_dedup[section_id] = included
            preselection_dropped.extend(dedup_dropped)

        by_section_dedup, contract_dropped = _apply_source_contracts(
            reduced_plan,
            by_section_dedup,
            quota_enforcer=self._quota_enforcer,
            locked_sections=locked_sections,
        )
        preselection_dropped.extend(contract_dropped)
        selected = self._selector.select(
            reduced_plan,
            by_section_dedup,
            locked_sections=locked_sections,
        )
        if preselection_dropped:
            selected = selected.model_copy(
                update={"dropped": [*preselection_dropped, *selected.dropped]}
            )
        selection_ms = _now_ms() - selection_started

        # 5a. Preflight（Provider Context Length Recovery）：select 后、compose 前
        preflight_started = _now_ms()
        preflight_result = None
        if self._preflight is not None:
            preflight_result = await self._preflight.run(
                request=request,
                plan=reduced_plan,
                selected=selected,
                runtime_context=runtime_context,
                trigger=CompactionTriggerType.PROVIDER_CONTEXT_ERROR,
            )
            selected = preflight_result.selected
        preflight_ms = _now_ms() - preflight_started

        compose_started = _now_ms()
        composed = self._composer.compose(
            request,
            selected,
            locked_sections=locked_sections,
        )
        validation = self._validator.validate(
            composed,
            absolute_threshold=int(reduced_plan.absolute_threshold or 0) or None,
        )
        if validation.failure_code:
            raise_engine_error(
                code=validation.failure_code,
                detail="Retry Compose 验证未通过",
                stage=ContextEngineStage.PREFLIGHT,
                retryable=True,
                recoverable=True,
            )
        compose_ms = _now_ms() - compose_started
        latency = ContextBuildLatency(
            planning_ms=planning_ms,
            source_gathering_ms=source_gathering_ms,
            selection_ms=selection_ms,
            preflight_ms=preflight_ms,
            compose_ms=compose_ms,
        )
        composed = composed.model_copy(
            update={
                "build_latency_ms": max(latency.total_ms, _now_ms() - started),
                "retrieval_run_ids": list(outcome.retrieval_run_ids),
                "degraded": bool(outcome.degraded or selected.dropped),
            }
        )

        snapshot_ref = await self._begin_and_ready(
            request,
            scope,
            reduced_plan,
            composed,
            selected,
            locked_sections,
            "active",
            runtime_context,
            previous_ref=previous_snapshot_ref,
            latency=latency,
            preflight_telemetry=_preflight_telemetry(
                reduced_plan,
                preflight_result,
                trigger=CompactionTriggerType.PROVIDER_CONTEXT_ERROR,
            ),
        )
        composed = composed.model_copy(update={"snapshot_public_id": snapshot_ref.public_id})
        self._dump_prompt(
            request=request,
            plan=reduced_plan,
            composed=composed,
            snapshot_public_id=snapshot_ref.public_id,
            execution_mode="retry",
        )
        return composed

    # ── 内部：begin_build + mark_ready ────────────────────────────────

    def _dump_prompt(
        self,
        *,
        request: ContextRequest,
        plan: ContextPlan,
        composed: ContextComposeResult,
        snapshot_public_id: str,
        execution_mode: str,
    ) -> None:
        """dev-only prompt 全量落盘。失败永不抛 — 仅 logger.warning。"""
        if self._prompt_dumper is None or not self._prompt_dumper.enabled:
            return
        try:
            self._prompt_dumper.dump(
                request=request,
                plan=plan,
                composed=composed,
                snapshot_public_id=snapshot_public_id,
                execution_mode=execution_mode,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ContextEngine._dump_prompt failed: %s", exc)

    async def _resolve_runtime_model_window(
        self,
        request: ContextRequest,
        runtime_context,
    ) -> int | None:
        """Resolve the user's configured context window for planning.

        Unknown remains ``None`` so the budget calculator uses its conservative
        unknown-window policy; the value is never guessed from the model name.
        """
        if request.model_context_window is not None:
            return int(request.model_context_window)
        session_factory = getattr(runtime_context, "session_factory", None)
        user_internal_id = getattr(runtime_context, "user_internal_id", None)
        if session_factory is None or not user_internal_id:
            logger.warning(
                "FALLBACK_USED | component=context_engine | "
                "from=model_config_window | to=unknown_window_budget | "
                "reason=missing_runtime_context | call_site=%s",
                request.call_site,
            )
            return None
        try:
            from app.repositories.model_config_repository import ModelConfigRepository

            async with session_factory() as session:
                cfg = await ModelConfigRepository(session).get_active_for_user(
                    int(user_internal_id)
                )
            window = getattr(cfg, "context_window_tokens", None) if cfg else None
            return int(window) if window else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "FALLBACK_USED | component=context_engine | "
                "from=model_config_window | to=unknown_window_budget | "
                "reason=model_config_load_failed | call_site=%s | error=%s",
                request.call_site,
                type(exc).__name__,
            )
            return None

    async def _begin_and_ready(
        self,
        request: ContextRequest,
        scope: ContextScope,
        plan: ContextPlan,
        composed: ContextComposeResult,
        selected,
        locked_sections: list[LockedSection],
        execution_mode: str,
        runtime_context,
        *,
        previous_ref: ContextSnapshotRef | None = None,
        latency: ContextBuildLatency | None = None,
        preflight_telemetry: dict[str, Any] | None = None,
    ) -> ContextSnapshotRef:
        command = ContextSnapshotBeginCommand(
            user_id=_snapshot_user_id(request, runtime_context),
            conversation_id=_snapshot_conversation_id(request, runtime_context),
            agent_task_id=_snapshot_task_id(request, runtime_context),
            llm_task_type=_llm_task_type(request),
            context_kind="shadow" if execution_mode == "shadow" else "active",
            call_site=request.call_site,
            context_profile_key=plan.profile_key,
            context_profile_version=str(plan.profile_version),
            context_policy_version=None,
            model_name_snapshot=await _snapshot_model_name(runtime_context),
            context_window_tokens=plan.model_context_window,
            input_budget_tokens=int(plan.input_budget),
            output_reserve_tokens=int(plan.output_reserve),
            runtime_reserve_tokens=int(plan.runtime_reserve),
            safety_margin_tokens=int(plan.safety_margin),
            target_input_tokens=int(plan.input_budget),
            estimated_input_tokens=composed.estimated_input_tokens,
            section_stats_json=selected.section_stats,
            included_refs_json=[r.to_state_dict() for r in selected.refs()],
            dropped_refs_json=[d.model_dump(mode="json") for d in selected.dropped],
            prompt_digest=composed.prompt_digest,
            prompt_excerpt=composed.prompt_excerpt,
            latency_json=(latency or ContextBuildLatency()).model_dump(mode="json"),
            retrieval_run_ids=list(composed.retrieval_run_ids),
            preflight_json=preflight_telemetry,
        )

        async with runtime_context.session_factory() as session:
            ref = await self._snapshot_writer.begin_build(command, session=session)
            await self._snapshot_writer.mark_ready(
                session=session, public_id=ref.public_id, user_id=command.user_id
            )
            if execution_mode == "shadow":
                # Shadow：begin → ready → complete_shadow（成功路径，非 abandoned）
                await self._snapshot_writer.complete_shadow(
                    session=session, public_id=ref.public_id, user_id=command.user_id
                )
            await session.commit()
        return ref


def _preflight_telemetry(
    plan: ContextPlan,
    result: Any | None,
    *,
    request: ContextRequest | None = None,
    trigger: CompactionTriggerType,
) -> dict[str, Any] | None:
    """Return owner-safe preflight evidence for the context snapshot.

    The telemetry is intentionally limited to tokens, thresholds and decision
    enums.  It must never contain source content, prompt excerpts or provider
    response text.
    """
    if result is None:
        return None
    tokens_before = int(result.tokens_before)
    absolute = int(plan.absolute_threshold)
    hard = int(plan.hard_compact_threshold)
    soft = int(plan.soft_threshold)
    waterline = "target"
    if absolute and tokens_before >= absolute:
        waterline = "absolute"
    elif hard and tokens_before >= hard:
        waterline = "hard_compact"
    elif soft and tokens_before >= soft:
        waterline = "soft"
    return {
        "trigger": getattr(trigger, "value", str(trigger)),
        "waterline": waterline,
        "status": getattr(result.status, "value", str(result.status)),
        "action": getattr(result.action, "value", str(result.action)),
        "tokens_before": tokens_before,
        "tokens_after": int(result.tokens_after),
        "target_tokens": int(result.target_tokens),
        # Numeric-only evidence for the conversation-retention bridge.  This
        # lets operators distinguish a policy decision from a fail-open ledger
        # attach without exposing any source text or identities.
        "conversation_ledger_tokens": int(
            getattr(request, "conversation_ledger_tokens", 0) or 0
        ),
        "conversation_compact_threshold": int(
            getattr(plan, "conversation_compact_threshold", 0) or 0
        ),
        "soft_threshold": soft,
        "hard_threshold": hard,
        "absolute_threshold": absolute,
        "dropped_ref_count": len(result.selected.dropped),
        "compacted_summary_count": len(result.compacted_summary_refs),
          "compression_provider_call_count": int(result.compression_provider_call_count),
          "business_provider_call_count": int(result.business_provider_call_count),
          "degraded": bool(result.degraded),
          "blocked_reason": result.blocked_reason,
          "compaction_attempted": bool(getattr(result, "compaction_attempted", False)),
          "compaction_compactor_available": getattr(result, "compaction_compactor_available", None),
          "compaction_runtime_context_available": getattr(result, "compaction_runtime_context_available", None),
          "compaction_phase": getattr(result, "compaction_phase", None),
          "compaction_exception_type": getattr(result, "compaction_exception_type", None),
          "compaction_exception_code": getattr(result, "compaction_exception_code", None),
          "conversation_retention_eligible": bool(
              getattr(result, "conversation_retention_eligible", False)
          ),
          "conversation_retention_rehydrated": bool(
              getattr(result, "conversation_retention_rehydrated", False)
          ),
          "conversation_retention_candidate_count": int(
              getattr(result, "conversation_retention_candidate_count", 0) or 0
          ),
      }


def _llm_task_type(request: ContextRequest) -> str:
    """call_site → llm_task_type（简化映射）。"""
    return (request.call_site or "context").split(".")[-1][:64] or "context"


def _snapshot_user_id(request: ContextRequest, runtime_context) -> int:
    """snapshot.user_id 使用内部 id（RuntimeContext.user_internal_id）。

    与 llm_invoker._user_internal_id 同语义：生产 bridge 传 str(内部 id)（如
    "1"），scope resolver 要求 PublicId 格式 —— 内部 id 不能直接 int(request.user_id)。
    """
    internal = getattr(runtime_context, "user_internal_id", None)
    if internal is not None:
        return int(internal)
    return int(request.user_id)


def _snapshot_conversation_id(request: ContextRequest, runtime_context) -> int | None:
    """snapshot.conversation_id 使用内部 id（RuntimeContext.conversation_internal_id）。

    request.conversation_id 是 public_id 字符串，不能 int()。
    """
    internal = getattr(runtime_context, "conversation_internal_id", None)
    if internal is not None:
        return int(internal)
    if request.conversation_id and str(request.conversation_id).isdigit():
        return int(request.conversation_id)
    return None


def _snapshot_task_id(request: ContextRequest, runtime_context) -> int | None:
    """snapshot.agent_task_id 使用内部 id（RuntimeContext.task_internal_id）。"""
    internal = getattr(runtime_context, "task_internal_id", None)
    if internal is not None:
        return int(internal)
    if request.task_id and str(request.task_id).isdigit():
        return int(request.task_id)
    return None


async def _snapshot_model_name(runtime_context) -> str | None:
    """Best-effort model name for snapshot metadata."""
    settings_service = getattr(runtime_context, "settings_service", None)
    if settings_service is None:
        return None
    try:
        provider = await settings_service.llm_config_provider()
        return getattr(provider, "model_name", None)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ContextEngine snapshot model_name resolve failed | error=%s",
            exc,
        )
        return None


def _reduce_plan_for_retry(plan: ContextPlan) -> ContextPlan:
    """确定性降容：Optional section budget 减半；Required 保留。"""
    new_section_plans: dict[str, Any] = {}
    for section_id, sp in plan.section_plans.items():
        if not sp.required:
            new_section_plans[section_id] = sp.model_copy(
                update={"budget_tokens": max(1, int(sp.budget_tokens // 2))}
            )
        else:
            new_section_plans[section_id] = sp
    return plan.model_copy(update={"section_plans": new_section_plans})


def _apply_source_contracts(
    plan: ContextPlan,
    by_section: dict[str, list],
    *,
    quota_enforcer: SourceQuotaEnforcer,
    locked_sections: list[LockedSection],
    quota_exempt_source_types: set[str] | None = None,
) -> tuple[dict[str, list], list[DroppedContextRef]]:
    """Apply section source allow-lists, then one global source quota pass."""
    filtered: dict[str, list] = {}
    dropped: list[DroppedContextRef] = []
    flattened: list = []

    for section_id, section_plan in plan.section_plans.items():
        allowed = {
            value.value if hasattr(value, "value") else str(value)
            for value in section_plan.source_types
        }
        section_items = []
        for item in by_section.get(section_id, []):
            source_type = (
                item.source_type.value
                if hasattr(item.source_type, "value")
                else str(item.source_type)
            )
            if allowed and source_type not in allowed:
                dropped.append(
                    DroppedContextRef(
                        item_id=item.item_id,
                        reason="source_type_not_allowed",
                        detail=(
                            f"section {section_id} does not allow source_type "
                            f"{source_type}"
                        ),
                    )
                )
                continue
            section_items.append(item)
            flattened.append(item)
        filtered[section_id] = section_items

    locked_ids = {section.section_id for section in locked_sections}
    required_kinds = {
        section.kind.value
        for section in plan.section_plans.values()
        if section.required
    }
    included, quota_dropped = quota_enforcer.enforce(
        flattened,
        locked_ids=locked_ids,
        required_kinds=required_kinds,
        exempt_source_types=quota_exempt_source_types,
    )
    included_object_ids = {id(item) for item in included}
    constrained = {
        section_id: [item for item in items if id(item) in included_object_ids]
        for section_id, items in filtered.items()
    }
    return constrained, [*dropped, *quota_dropped]


def _conversation_retention_due(
    request: ContextRequest,
    plan: ContextPlan,
    execution_mode: str,
) -> bool:
    """Whether this compose needs its full raw prefix for chat retention.

    The durable ledger is separate from the selected prompt estimate.  Its
    crossing must be evaluated before source quota removes older messages,
    otherwise the conversation compactor has no prefix to summarize and
    silently falls through to normal prompt composition.
    """
    threshold = int(getattr(plan, "conversation_compact_threshold", 0) or 0)
    ledger_tokens = int(getattr(request, "conversation_ledger_tokens", 0) or 0)
    # ``conversation_ledger_tokens`` is populated exclusively by the normal
    # chat bridge.  It is therefore a more reliable production capability
    # signal than a call-site label that can be wrapped by an invoker.
    # Preview remains safe: preflight does not mutate in preview mode.
    del execution_mode
    return threshold > 0 and ledger_tokens >= threshold


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# auto-appended module-level note: ContextEngine 主类: build_context(preflight + retrieve + select + compose + audit)。
