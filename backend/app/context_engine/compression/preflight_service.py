"""ContextPreflightService — 四级水位状态机（doc09 §8 / §7）。

位置：select 之后、compose 之前。只处理 SelectedContextSet。

状态机：
  flag 关 → PASS（直通零行为）
  TARGET → PASS
  SOFT → pruner.try_prune()；有删除 → PRUNED；无删除 → PASS
  HARD_COMPACT → 先 prune；仍超 hard 但 < absolute → compactor；成功 COMPACTED；
                 失败 → degraded PRUNED；仍超 absolute → BLOCKED
  ABSOLUTE → 强制 compact（可调 Compression Provider）；成功 COMPACTED；
             失败或仍超 → BLOCKED（retryable=false, business_provider_call_count=0）

Absolute 语义：compression_provider_call_count 与 business_provider_call_count
分离。成功压缩并通过最终校验前 business_provider_call_count=0。
"""

from __future__ import annotations

from typing import Any

from app.context_engine.conversation_retention import (
    choose_retention_decision,
    count_complete_turns,
)
from app.context_engine.compression.anchor import ProtectedAnchorBuilder
from app.context_engine.compression.models import (
    ContextCompactionRequest,
    ContextPreflightRequest,
    ContextPreflightResult,
    ProtectedAnchor,
)
from app.context_engine.compression.pruner import ItemPruner
from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.context import (
    ContextPlan,
    ContextRef,
    ContextRequest,
    ContextScope,
    SectionPlan,
)
from app.context_engine.models.enums import (
    CompactionStatus,
    CompactionTriggerType,
    CompactionType,
    ContextKind,
    ContextPreflightAction,
    ContextTrust,
    CompressionLevel,
    RecoveryMode,
    SourceType,
)
from app.context_engine.models.selection import DroppedContextRef, SelectedContextSet


class ContextPreflightService:
    """Preflight 状态机。compaction 依赖可注入（WP-4/5 提供实现）。"""

    def __init__(
        self,
        *,
        pruner: ItemPruner | None = None,
        anchor_builder: ProtectedAnchorBuilder | None = None,
        conversation_compactor=None,
        agent_loop_compactor=None,
        full_replace_compactor=None,
        compaction_repo=None,
        enabled: bool = True,
        full_replace_enabled: bool = False,
    ) -> None:
        self._pruner = pruner or ItemPruner()
        self._anchor_builder = anchor_builder or ProtectedAnchorBuilder()
        self._conversation_compactor = conversation_compactor
        self._agent_loop_compactor = agent_loop_compactor
        self._full_replace_compactor = full_replace_compactor
        self._compaction_repo = compaction_repo
        self._enabled = enabled
        self._full_replace_enabled = full_replace_enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    async def run(
        self,
        *,
        request: ContextRequest,
        plan: ContextPlan,
        selected: SelectedContextSet,
        profile: Any | None = None,
        runtime_context: Any | None = None,
        trigger: CompactionTriggerType = CompactionTriggerType.PREFLIGHT,
        preview: bool = False,
    ) -> ContextPreflightResult:
        """执行 Preflight。flag 关 → PASS 直通。"""
        if not self._enabled:
            return self._pass_through(selected)

        budget = getattr(plan, "_budget", None)
        if budget is None:
            # 从 plan 预算重建 ContextBudget（target/soft/hard/absolute）
            budget = _rebuild_budget(plan)
        tokens_before = selected.total_estimated_tokens
        if tokens_before <= 0:
            return self._pass_through(selected)

        # Chat retains the full verbatim timeline while there is inexpensive
        # room in the model window.  At the configured 60% waterline, fold
        # only the old conversational prefix and keep the newest complete
        # turns exact.  This is deliberately earlier than the generic Hard
        # guard: Hard/Absolute still protect every source type, whereas this
        # transition is solely a conversation-retention policy.
        proactive_threshold = int(
            getattr(budget, "conversation_compact_threshold", 0) or 0
        )
        # Normal chat owns a durable conversation ledger.  Its total is the
        # retention waterline, while ``tokens_before`` remains the exact
        # selected prompt size used for all hard/absolute safety decisions.
        retention_tokens = max(
            tokens_before,
            int(getattr(request, "conversation_ledger_tokens", 0) or 0),
        )
        if (
            proactive_threshold
            and retention_tokens >= proactive_threshold
            and _uses_proactive_conversation_retention(request, plan)
        ):
            proactive = await self._proactive_conversation_compact(
                request,
                plan,
                selected,
                budget,
                profile,
                runtime_context,
                trigger,
                preview,
            )
            if proactive is not None:
                return proactive.model_copy(
                    update={"conversation_retention_eligible": True}
                )

        level = budget.level_for(tokens_before)
        anchors = self._anchor_builder.build(request, profile, selected)

        if level == CompressionLevel.TARGET:
            return self._pass_through(selected, anchors=anchors)

        if level == CompressionLevel.SOFT:
            # Soft is an advisory warning band.  Applying the generic quota
            # pruner here can collapse a coherent 20-turn working set to five
            # items long before the configured compression line.  Destructive
            # pruning/compaction belongs to Hard and Absolute.
            return self._pass_through(selected, anchors=anchors)

        if level == CompressionLevel.HARD_COMPACT:
            return await self._hard_compact(
                request, plan, selected, budget, anchors, runtime_context, trigger, preview
            )

        if level == CompressionLevel.ABSOLUTE:
            return await self._absolute(
                request, plan, selected, budget, anchors, runtime_context, trigger, preview
            )

        return self._pass_through(selected, anchors=anchors)

    async def _proactive_conversation_compact(
        self,
        request: ContextRequest,
        plan: ContextPlan,
        selected: SelectedContextSet,
        budget: Any,
        profile: Any | None,
        runtime_context: Any,
        trigger: CompactionTriggerType,
        preview: bool,
    ) -> ContextPreflightResult | None:
        """Compact the old conversation prefix while preserving a raw tail."""
        # Select the tier from durable context, not elapsed wall-clock time.
        # The first pass retains twenty turns; a quick regrowth after a prior
        # summary retains twelve, creating useful hysteresis below the 60%
        # waterline.
        working_selected = selected
        rehydrated = False
        candidate_count = 0
        light_older_items, _recent_items = _split_old_conversation_prefix(
            working_selected, keep_turns=20
        )
        # Source quota and semantic dedup protect regular prompt construction,
        # but a 60% retention transition needs the durable old prefix itself.
        # Rehydrate it only when the bounded selected set has no old turn to
        # summarize.  The result is still reduced to summary + raw tail before
        # it reaches the business model.
        if not light_older_items:
            durable_items = await _load_durable_conversation_candidates(
                request, runtime_context
            )
            candidate_count = len(durable_items)
            if durable_items:
                working_selected = _with_conversation_candidates(
                    selected, durable_items
                )
                rehydrated = True
                light_older_items, _recent_items = _split_old_conversation_prefix(
                    working_selected, keep_turns=20
                )
        summary_items = _conversation_summary_items(working_selected)
        decision = choose_retention_decision(
            has_prior_summary=bool(summary_items),
            newly_compactable_turns=count_complete_turns(light_older_items),
        )
        older_items, _recent_items = _split_old_conversation_prefix(
            working_selected, keep_turns=decision.keep_turns
        )
        if not older_items:
            # A single oversized recent turn cannot safely be summarized as an
            # "old" conversation.  Record this explicitly instead of falling
            # through as an indistinguishable ordinary TARGET pass: once the
            # durable ledger is above the retention waterline it is an
            # actionable observability signal, not normal behaviour.
            return ContextPreflightResult(
                status=CompactionStatus.PASS,
                action=ContextPreflightAction.PASS,
                selected=selected,
                tokens_before=selected.total_estimated_tokens,
                tokens_after=selected.total_estimated_tokens,
                target_tokens=selected.total_estimated_tokens,
                degraded=True,
                blocked_reason="conversation_retention_no_old_prefix",
                conversation_retention_rehydrated=rehydrated,
                conversation_retention_candidate_count=candidate_count,
            )

        tokens_before = working_selected.total_estimated_tokens
        source_items = [*summary_items, *older_items]
        source_tokens = sum(item.estimated_tokens for item in source_items)
        compactable = _selected_subset(working_selected, source_items)
        target_tokens = _proactive_summary_target(
            total_tokens=tokens_before,
            source_tokens=source_tokens,
            target_total_tokens=int(budget.model_context_window * decision.target_ratio),
        )
        compactor = self._conversation_compactor
        diagnostics = _compaction_diagnostics(compactor, runtime_context)

        # Preview must remain side-effect free.  It deliberately keeps the
        # current raw selection so the displayed estimate is truthful; the
        # next real send performs the durable summary transition.
        if preview:
            return ContextPreflightResult(
                status=CompactionStatus.PASS,
                action=ContextPreflightAction.PASS,
                selected=selected,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                target_tokens=target_tokens,
                degraded=True,
                blocked_reason="preview_requires_compaction",
                conversation_retention_rehydrated=rehydrated,
                conversation_retention_candidate_count=candidate_count,
                **diagnostics,
            )

        if compactor is None or runtime_context is None:
            return ContextPreflightResult(
                status=CompactionStatus.PASS,
                action=ContextPreflightAction.PASS,
                selected=selected,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                target_tokens=target_tokens,
                degraded=True,
                blocked_reason="compaction_unavailable_or_failed",
                conversation_retention_rehydrated=rehydrated,
                conversation_retention_candidate_count=candidate_count,
                **diagnostics,
            )

        diagnostics["compaction_attempted"] = True
        anchors = self._anchor_builder.build(request, profile, working_selected)
        try:
            compacted, summary_refs = await self._run_compaction(
                request,
                plan,
                working_selected,
                budget,
                anchors,
                runtime_context,
                compactor,
                trigger,
                compaction_type=CompactionType.CONVERSATION,
                source_selected=compactable,
                replace_item_ids={item.item_id for item in source_items},
                target_tokens=target_tokens,
                policy_key=decision.policy_key,
                covered_message_start_id=_summary_covered_start(summary_items)
                or _message_id(older_items[0]),
                covered_message_end_id=_message_id(older_items[-1]),
                covered_message_count=(
                    sum(_summary_message_count(item) for item in summary_items)
                    + len(older_items)
                ),
            )
        except Exception as exc:  # noqa: BLE001
            _record_compaction_failure(diagnostics, exc)
            return ContextPreflightResult(
                status=CompactionStatus.PASS,
                action=ContextPreflightAction.PASS,
                selected=selected,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                target_tokens=target_tokens,
                degraded=True,
                blocked_reason="compaction_unavailable_or_failed",
                conversation_retention_rehydrated=rehydrated,
                conversation_retention_candidate_count=candidate_count,
                **diagnostics,
            )

        if compacted.total_estimated_tokens > budget.absolute_threshold:
            return self._block(
                tokens_before,
                target_tokens,
                "proactive_compaction_over_absolute",
                safe_metadata=diagnostics,
            )
        return ContextPreflightResult(
            status=CompactionStatus.COMPACTED,
            action=ContextPreflightAction.COMPACT,
            selected=compacted,
            tokens_before=tokens_before,
            tokens_after=compacted.total_estimated_tokens,
            target_tokens=target_tokens,
            compacted_summary_refs=summary_refs,
            compression_provider_call_count=1,
            business_provider_call_count=0,
            conversation_retention_rehydrated=rehydrated,
            conversation_retention_candidate_count=candidate_count,
            **_completed_compaction_diagnostics(diagnostics),
        )

    # ── HARD_COMPACT ──────────────────────────────────────────────────

    async def _hard_compact(
        self,
        request: ContextRequest,
        plan: ContextPlan,
        selected: SelectedContextSet,
        budget: Any,
        anchors: tuple[ProtectedAnchor, ...],
        runtime_context: Any,
        trigger: CompactionTriggerType,
        preview: bool,
    ) -> ContextPreflightResult:
        tokens_before = selected.total_estimated_tokens

        # 1. 先 prune
        pruned = self._pruner.prune(
            selected, section_plans=plan.section_plans, target_tokens=budget.hard_compact_threshold
        )
        after_prune = pruned.total_estimated_tokens

        # 2. prune 后低于 hard → PRUNED
        if after_prune <= budget.hard_compact_threshold:
            return ContextPreflightResult(
                status=CompactionStatus.PRUNED,
                action=ContextPreflightAction.PRUNE,
                selected=pruned,
                tokens_before=tokens_before,
                tokens_after=after_prune,
                target_tokens=budget.target_input,
                degraded=False,
            )

        # A typing-time preview must never call a provider or write a durable
        # summary. It still returns the exact deterministic prune result and
        # marks that a real send needs compaction.
        if preview:
            return ContextPreflightResult(
                status=CompactionStatus.PRUNED,
                action=ContextPreflightAction.PRUNE,
                selected=pruned,
                tokens_before=tokens_before,
                tokens_after=after_prune,
                target_tokens=budget.target_input,
                degraded=True,
                blocked_reason="preview_requires_compaction",
                **_compaction_diagnostics(
                    self._select_compactor(plan, trigger=trigger), runtime_context
                ),
            )

        # 3. prune 后仍超 hard → compact（低于 absolute 才允许 degraded 继续）
        compactor = self._select_compactor(plan, trigger=trigger)
        diagnostics = _compaction_diagnostics(compactor, runtime_context)
        if compactor is not None and runtime_context is not None:
            diagnostics["compaction_attempted"] = True
            try:
                compacted, summary_refs = await self._run_compaction(
                    request, plan, pruned, budget, anchors, runtime_context, compactor, trigger,
                    compaction_type=compactor.compaction_type,
                )
                if compacted.total_estimated_tokens <= budget.absolute_threshold:
                    return ContextPreflightResult(
                        status=CompactionStatus.COMPACTED,
                        action=ContextPreflightAction.COMPACT,
                        selected=compacted,
                        tokens_before=tokens_before,
                        tokens_after=compacted.total_estimated_tokens,
                        target_tokens=budget.target_input,
                        compacted_summary_refs=summary_refs,
                        compression_provider_call_count=1,
                        business_provider_call_count=0,
                        **_completed_compaction_diagnostics(diagnostics),
                    )
                # 压缩后仍超 absolute → BLOCKED
                return self._block(tokens_before, budget.target_input, "hard_compact_after_compaction_over_absolute")
            except Exception as exc:  # noqa: BLE001 — retain safe failure telemetry
                _record_compaction_failure(diagnostics, exc)

        # 压缩不可用或失败：低于 absolute → degraded PRUNED
        if after_prune <= budget.absolute_threshold:
            return ContextPreflightResult(
                status=CompactionStatus.PRUNED,
                action=ContextPreflightAction.PRUNE,
                selected=pruned,
                tokens_before=tokens_before,
                tokens_after=after_prune,
                target_tokens=budget.target_input,
                degraded=True,
                blocked_reason="compaction_unavailable_or_failed",
                **diagnostics,
            )
        # 仍超 absolute → BLOCKED
        return self._block(
            tokens_before,
            budget.target_input,
            "hard_compact_over_absolute",
            safe_metadata=diagnostics,
        )

    # ── ABSOLUTE ──────────────────────────────────────────────────────

    async def _absolute(
        self,
        request: ContextRequest,
        plan: ContextPlan,
        selected: SelectedContextSet,
        budget: Any,
        anchors: tuple[ProtectedAnchor, ...],
        runtime_context: Any,
        trigger: CompactionTriggerType,
        preview: bool,
    ) -> ContextPreflightResult:
        tokens_before = selected.total_estimated_tokens

        # 先 prune（低成本）
        pruned = self._pruner.prune(
            selected, section_plans=plan.section_plans, target_tokens=budget.absolute_threshold
        )
        if preview:
            return ContextPreflightResult(
                status=CompactionStatus.PRUNED,
                action=ContextPreflightAction.PRUNE,
                selected=pruned,
                tokens_before=tokens_before,
                tokens_after=pruned.total_estimated_tokens,
                target_tokens=budget.target_input,
                degraded=True,
                blocked_reason="preview_requires_compaction",
                **_compaction_diagnostics(
                    self._select_compactor(plan, trigger=trigger), runtime_context
                ),
            )
        # Absolute is the last preflight guard before a business-model call.
        # Pruning may reduce the source set, but it must not turn this branch
        # into a silent PRUNED success: the configured compactor still has to
        # create the durable compacted representation.  This keeps the runtime
        # behaviour consistent with the documented Absolute contract and makes
        # the resulting summary available to later Compose calls.
        #
        # Use the pruned set as the compactor input so the deterministic,
        # low-value removals still lower the provider payload.
        compactor = self._select_compactor(plan, trigger=trigger)
        diagnostics = _compaction_diagnostics(compactor, runtime_context)
        if compactor is not None and runtime_context is not None:
            diagnostics["compaction_attempted"] = True
            try:
                compacted, summary_refs = await self._run_compaction(
                    request, plan, pruned, budget, anchors, runtime_context, compactor, trigger,
                    compaction_type=compactor.compaction_type,
                )
                if compacted.total_estimated_tokens <= budget.absolute_threshold:
                    return ContextPreflightResult(
                        status=CompactionStatus.COMPACTED,
                        action=ContextPreflightAction.COMPACT,
                        selected=compacted,
                        tokens_before=tokens_before,
                        tokens_after=compacted.total_estimated_tokens,
                        target_tokens=budget.target_input,
                        compacted_summary_refs=summary_refs,
                        compression_provider_call_count=1,
                        business_provider_call_count=0,
                        **_completed_compaction_diagnostics(diagnostics),
                    )
            except Exception as exc:  # noqa: BLE001 — retain safe failure telemetry
                _record_compaction_failure(diagnostics, exc)

        # 压缩失败或仍超 → BLOCKED（retryable=false, business_provider_call_count=0）
        return self._block(
            tokens_before,
            budget.target_input,
            "absolute_after_compaction",
            safe_metadata=diagnostics,
        )

    # ── 内部 ──────────────────────────────────────────────────────────

    async def _run_compaction(
        self,
        request: ContextRequest,
        plan: ContextPlan,
        selected: SelectedContextSet,
        budget: Any,
        anchors: tuple[ProtectedAnchor, ...],
        runtime_context: Any,
        compactor: Any,
        trigger: CompactionTriggerType,
        *,
        compaction_type: Any,
        source_selected: SelectedContextSet | None = None,
        replace_item_ids: set[str] | None = None,
        target_tokens: int | None = None,
        policy_key: str | None = None,
        covered_message_start_id: int | None = None,
        covered_message_end_id: int | None = None,
        covered_message_count: int | None = None,
    ) -> tuple[SelectedContextSet, list]:
        """执行 compactor 并返回 (更新后的 selected, summary_refs)。

        最终校验：压缩后 token 必须 <= absolute_threshold，否则视为失败。
        """
        try:
            source_selected = source_selected or selected
            req = ContextCompactionRequest(
                request_id=request.call_site,
                user_id=_required_internal_identity(
                    runtime_context, "user_internal_id", request.user_id
                ),
                conversation_id=_optional_internal_identity(
                    runtime_context, "conversation_internal_id", request.conversation_id
                ),
                task_id=_optional_internal_identity(
                    runtime_context, "task_internal_id", request.task_id
                ),
                workspace_key=request.workspace_key,
                call_site=request.call_site,
                compaction_type=compaction_type,
                trigger=trigger,
                policy_key=policy_key or plan.compression_policy or "compression:v1",
                policy_version="v2" if policy_key else "v1",
                source_digest=_source_digest(source_selected),
                tokens_before=source_selected.total_estimated_tokens,
                target_tokens=target_tokens or budget.target_input,
                protected_anchors=list(anchors),
                source_refs=source_selected.refs(),
                recovery_mode=RecoveryMode.SUMMARY_WITH_REFS,
                source_payload=_build_source_payload(
                    source_selected,
                    compaction_type,
                    compression_policy=plan.compression_policy,
                ),
                covered_message_start_id=covered_message_start_id,
                covered_message_end_id=covered_message_end_id,
                covered_message_count=covered_message_count,
            )
        except Exception as exc:  # noqa: BLE001
            raise _CompactionAttemptFailure("request_build", exc) from None
        try:
            result = await compactor.compact(req, runtime_context=runtime_context)
        except Exception as exc:  # noqa: BLE001
            raise _CompactionAttemptFailure("compactor_call", exc) from None
        if result is None:
            raise _CompactionAttemptFailure("compactor_result", RuntimeError())
        try:
            summary_refs = []
            if result.summary_public_id:
                summary_refs.append(
                    ContextRef(
                        item_id=result.summary_public_id,
                        kind=ContextKind.CONVERSATION,
                        source_type="conversation_summary",
                        source_ref=result.summary_public_id,
                    )
                )
            compacted = _replace_compacted_items(
                selected,
                result,
                compaction_type=compaction_type,
                compression_policy=plan.compression_policy,
                replace_item_ids=replace_item_ids,
            )
        except Exception as exc:  # noqa: BLE001
            raise _CompactionAttemptFailure("result_materialization", exc) from None
        return compacted, summary_refs

    def _select_compactor(self, plan: ContextPlan, *, trigger: CompactionTriggerType | None = None):
        """按 compression_policy + trigger 选择 compactor。"""
        # Full Replace：仅 provider_context_error 且 flag 开时启用
        if (
            trigger == CompactionTriggerType.PROVIDER_CONTEXT_ERROR
            and self._full_replace_enabled
            and self._full_replace_compactor is not None
        ):
            return self._full_replace_compactor
        policy = (plan.compression_policy or "").lower()
        if "agent_loop" in policy or "loop" in policy:
            return self._agent_loop_compactor
        if "conversation" in policy:
            return self._conversation_compactor
        # 默认 conversation（无 policy 时走 conversation 语义）
        return self._conversation_compactor

    def _pass_through(
        self,
        selected: SelectedContextSet,
        *,
        anchors: tuple[ProtectedAnchor, ...] = (),
    ) -> ContextPreflightResult:
        return ContextPreflightResult(
            status=CompactionStatus.PASS,
            action=ContextPreflightAction.PASS,
            selected=selected,
            tokens_before=selected.total_estimated_tokens,
            tokens_after=selected.total_estimated_tokens,
            target_tokens=selected.total_estimated_tokens,
            degraded=False,
        )

    def _block(
        self,
        tokens_before: int,
        target_tokens: int,
        reason: str,
        *,
        safe_metadata: dict[str, str | bool | int | None] | None = None,
    ) -> ContextPreflightResult:
        metadata = {"reason": reason, "tokens_before": tokens_before}
        if safe_metadata:
            metadata.update({key: value for key, value in safe_metadata.items() if value is not None})
        raise_engine_error(
            code="context.preflight.blocked",
            detail="上下文 Absolute 超限且压缩失败，阻止业务 LLM 调用",
            stage=ContextEngineStage.PREFLIGHT,
            retryable=False,
            recoverable=True,
            safe_metadata=metadata,
        )


class _CompactionAttemptFailure(RuntimeError):
    """Internal wrapper retaining only a phase and exception class for telemetry."""

    def __init__(self, phase: str, cause: Exception) -> None:
        self.phase = phase
        self.exception_type = type(cause).__name__[:64]
        self.exception_code = _safe_exception_code(cause)
        super().__init__(phase)


def _compaction_diagnostics(compactor: Any, runtime_context: Any) -> dict[str, str | bool | None]:
    compactor_available = compactor is not None
    runtime_available = runtime_context is not None
    phase = None
    if not compactor_available:
        phase = "compactor_unavailable"
    elif not runtime_available:
        phase = "runtime_context_unavailable"
    return {
        "compaction_attempted": False,
        "compaction_compactor_available": compactor_available,
        "compaction_runtime_context_available": runtime_available,
        "compaction_phase": phase,
        "compaction_exception_type": None,
        "compaction_exception_code": None,
    }


def _record_compaction_failure(diagnostics: dict[str, str | bool | None], exc: Exception) -> None:
    diagnostics["compaction_phase"] = getattr(exc, "phase", "compactor_call")
    diagnostics["compaction_exception_type"] = getattr(
        exc, "exception_type", type(exc).__name__[:64]
    )
    diagnostics["compaction_exception_code"] = getattr(exc, "exception_code", None)


def _completed_compaction_diagnostics(
    diagnostics: dict[str, str | bool | None],
) -> dict[str, str | bool | None]:
    completed = dict(diagnostics)
    completed["compaction_phase"] = "completed"
    completed["compaction_exception_type"] = None
    completed["compaction_exception_code"] = None
    return completed


def _safe_exception_code(exc: Exception) -> str | None:
    """Expose only a numeric DB/provider error code, never its message/body."""
    original = getattr(exc, "orig", exc)
    args = getattr(original, "args", ())
    candidate = args[0] if isinstance(args, tuple) and args else None
    if isinstance(candidate, int):
        return f"db_{candidate}"
    if isinstance(candidate, str) and candidate.isdigit():
        return f"db_{candidate}"
    return None


def _required_internal_identity(runtime_context: Any, attribute: str, fallback: Any) -> int:
    value = getattr(runtime_context, attribute, None)
    if value is None:
        value = fallback
    try:
        identity = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"missing internal identity: {attribute}") from exc
    if identity <= 0:
        raise RuntimeError(f"invalid internal identity: {attribute}")
    return identity


def _optional_internal_identity(
    runtime_context: Any, attribute: str, fallback: Any
) -> int | None:
    value = getattr(runtime_context, attribute, None)
    if value is None:
        value = fallback
    if value in (None, ""):
        return None
    try:
        identity = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"missing internal identity: {attribute}") from exc
    if identity <= 0:
        raise RuntimeError(f"invalid internal identity: {attribute}")
    return identity


def _rebuild_budget(plan: ContextPlan):
    """从 plan 的冻结预算快照精确重建 ContextBudget。"""
    from app.context_engine.models.profile import ContextBudget

    window = plan.model_context_window or 0
    target = int(plan.input_budget)
    output = int(plan.output_reserve)
    runtime = int(plan.runtime_reserve)
    safety = int(plan.safety_margin)
    usable = window - output - runtime - safety
    if usable <= 0:
        usable = max(target * 2, 1)
    return ContextBudget(
        model_context_window=window or target * 2,
        output_reserve=output,
        runtime_reserve=runtime,
        safety_margin=safety,
        target_input=target,
        soft_threshold=int(plan.soft_threshold or int(usable * 0.7)),
        hard_compact_threshold=int(
            plan.hard_compact_threshold or int(usable * 0.85)
        ),
        absolute_threshold=int(plan.absolute_threshold or int(usable * 0.95)),
        conversation_compact_threshold=int(
            plan.conversation_compact_threshold or 0
        ),
    )


def _source_digest(selected: SelectedContextSet) -> str:
    """selected 的稳定摘要（幂等 key 输入）。"""
    import hashlib
    import json

    items = sorted(
        (i.item_id, i.content[:100]) for i in selected.included
    )
    return hashlib.sha256(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest()


def _uses_proactive_conversation_retention(
    request: ContextRequest,
    plan: ContextPlan,
) -> bool:
    """Return whether this request owns the durable chat-history transition.

    ``chat.reply`` is the production identity of normal conversation and is
    therefore authoritative.  Some callers can supply a profile override for
    a single request; relying only on the resolved ``compression_policy`` made
    that override accidentally bypass the durable-ledger waterline even though
    the request had already crossed it.  Keep the policy check as a backwards
    compatible opt-in for non-chat callers used by existing integrations.
    """
    return (
        request.call_site == "chat.reply"
        or (plan.compression_policy or "").lower() == "preserve_goal"
    )


def _split_old_conversation_prefix(
    selected: SelectedContextSet, *, keep_turns: int = 20
) -> tuple[list[Any], list[Any]]:
    """Split raw conversation items before the requested complete-turn tail."""
    raw = [
        item
        for item in selected.included
        if item.kind == ContextKind.CONVERSATION
        and item.source_type == SourceType.CONVERSATION
    ]
    ordered = sorted(
        enumerate(raw),
        key=lambda pair: _conversation_sort_key(pair[1], pair[0]),
    )
    items = [item for _index, item in ordered]
    if not items:
        return [], []

    keep_turns = max(0, int(keep_turns))
    user_indexes = [
        index
        for index, item in enumerate(items)
        if item.metadata.get("role") == "user"
    ]
    if len(user_indexes) > keep_turns:
        boundary = user_indexes[-keep_turns]
    elif not user_indexes:
        boundary = max(0, len(items) - keep_turns * 2)
    else:
        boundary = 0
    return items[:boundary], items[boundary:]


def _conversation_sort_key(item: Any, fallback_index: int) -> tuple[int, int]:
    value = item.metadata.get("sequence")
    try:
        return (0, int(value))
    except (TypeError, ValueError):
        return (1, fallback_index)


def _selected_subset(selected: SelectedContextSet, items: list[Any]) -> SelectedContextSet:
    ids = {item.item_id for item in items}
    return SelectedContextSet(
        included=list(items),
        dropped=[],
        section_stats={
            "conversation": {
                "included_count": len(items),
                "estimated_tokens": sum(item.estimated_tokens for item in items),
            }
        },
        total_estimated_tokens=sum(item.estimated_tokens for item in items),
        locked_section_ids=[
            section_id
            for section_id in selected.locked_section_ids
            if section_id in ids
        ],
    )


def _message_id(item: Any) -> int | None:
    value = item.metadata.get("message_id")
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _conversation_summary_items(selected: SelectedContextSet) -> list[Any]:
    """Return active conversation summaries so a new summary remains continuous."""
    return [
        item
        for item in selected.included
        if item.kind == ContextKind.CONVERSATION
        and str(getattr(item.source_type, "value", item.source_type))
        == str(SourceType.CONVERSATION_SUMMARY.value)
    ]


async def _load_durable_conversation_candidates(
    request: ContextRequest,
    runtime_context: Any,
) -> list[Any]:
    """Load the complete durable chat candidate set for a retention crossing.

    This fallback is intentionally narrow: it runs only after the normal
    selected context proved unable to supply an old prefix.  Reusing the
    production conversation source keeps ownership, message-type filtering and
    current-message exclusion identical to ordinary context construction.
    """
    if runtime_context is None or not request.conversation_id:
        return []
    try:
        from app.context_engine.sources.conversation import ConversationSourceAdapter

        # Chat-facing requests carry the public ``conv_*`` identifier, while
        # the source repository intentionally accepts the internal numeric
        # identity.  Normal composition receives that translation from its
        # bridge; retention rehydration must make the same translation rather
        # than silently degrading on ``int('conv_...')``.
        conversation_id = getattr(runtime_context, "conversation_internal_id", None)
        if not conversation_id or not str(conversation_id).isdigit():
            conversation_id = request.conversation_id
        if not str(conversation_id).isdigit():
            return []
        source_request = request.model_copy(
            update={"conversation_id": str(conversation_id)}
        )
        scope = ContextScope(
            user_id=str(request.user_id),
            workspace_key=request.workspace_key,
            conversation_id=str(conversation_id),
            task_id=str(request.task_id) if request.task_id else None,
            thread_id=str(request.thread_id) if request.thread_id else None,
        )
        section = SectionPlan(
            kind=ContextKind.CONVERSATION,
            required=False,
            budget_tokens=0,
            source_types=["conversation", "conversation_summary"],
        )
        outcome = await ConversationSourceAdapter().collect(
            source_request,
            section,
            scope,
            runtime_context=runtime_context,
        )
        return list(outcome.items) if outcome.attempted and not outcome.degraded else []
    except Exception:  # noqa: BLE001 - retention falls back to normal safety path
        return []


def _with_conversation_candidates(
    selected: SelectedContextSet,
    candidates: list[Any],
) -> SelectedContextSet:
    """Replace only the selected conversation layer with durable candidates."""
    non_conversation = [
        item for item in selected.included if item.kind != ContextKind.CONVERSATION
    ]
    included = [*non_conversation, *candidates]
    return SelectedContextSet(
        included=included,
        dropped=list(selected.dropped),
        section_stats=dict(selected.section_stats),
        total_estimated_tokens=sum(item.estimated_tokens for item in included),
        locked_section_ids=list(selected.locked_section_ids),
    )


def _summary_covered_start(items: list[Any]) -> int | None:
    values = [
        value
        for item in items
        if (value := _safe_positive_int(item.metadata.get("covered_start"))) is not None
    ]
    return min(values) if values else None


def _summary_message_count(item: Any) -> int:
    return max(0, _safe_positive_int(item.metadata.get("message_count")) or 0)


def _safe_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _proactive_summary_target(
    *, total_tokens: int, source_tokens: int, target_total_tokens: int
) -> int:
    """Target a global low-water mark while never summarizing the raw tail."""
    non_source_tokens = max(0, total_tokens - source_tokens)
    available = max(512, target_total_tokens - non_source_tokens)
    return max(512, min(max(512, source_tokens // 8), available))


def _build_source_payload(
    selected: SelectedContextSet,
    compaction_type: CompactionType,
    *,
    compression_policy: str | None = None,
) -> dict[str, Any]:
    """为 compactor 提供本次 selected 的真实待压缩文本。"""
    compactable = [
        item for item in selected.included
        if _is_compactable_item(
            item,
            compaction_type,
            compression_policy=compression_policy,
        )
    ]
    rendered = "\n\n".join(
        f"[{item.kind.value if hasattr(item.kind, 'value') else item.kind}] "
        f"{item.title or item.item_id} ({item.source_ref or item.item_id})\n{item.content}"
        for item in compactable
    )
    if compaction_type == CompactionType.AGENT_LOOP:
        return {"steps_audit": rendered}
    return {"conversation": rendered, "selected_item_count": len(compactable)}


def _replace_compacted_items(
    selected: SelectedContextSet,
    result: Any,
    *,
    compaction_type: CompactionType,
    compression_policy: str | None = None,
    replace_item_ids: set[str] | None = None,
) -> SelectedContextSet:
    """把 compactor 输出真实回灌到 SelectedContextSet.included。"""
    replacement_items = list(getattr(result, "replacement_items", None) or [])
    if not replacement_items:
        replacement_items = _build_replacement_items(result, compaction_type=compaction_type)
    if not replacement_items:
        raise RuntimeError("compaction did not provide summary content for prompt replacement")

    compacted_source_ids: set[str] = set()
    kept = []
    for item in selected.included:
        if (
            (replace_item_ids is None or item.item_id in replace_item_ids)
            and _is_compactable_item(
                item,
                compaction_type,
                compression_policy=compression_policy,
            )
        ):
            compacted_source_ids.add(item.item_id)
            continue
        kept.append(item)

    if not compacted_source_ids:
        raise RuntimeError("compaction had no replaceable source items")

    existing_ids = {item.item_id for item in kept}
    for item in replacement_items:
        if item.item_id not in existing_ids:
            kept.append(item)
            existing_ids.add(item.item_id)

    dropped = list(selected.dropped)
    dropped.extend(
        DroppedContextRef(
            item_id=item_id,
            reason="superseded",
            detail="已被 preflight compaction summary 替换",
        )
        for item_id in sorted(compacted_source_ids)
    )
    total_tokens = sum(item.estimated_tokens for item in kept)
    return SelectedContextSet(
        included=kept,
        dropped=dropped,
        section_stats=_rebuild_section_stats(selected, kept),
        total_estimated_tokens=total_tokens,
        locked_section_ids=selected.locked_section_ids,
    )


def _build_replacement_items(result: Any, *, compaction_type: CompactionType) -> list[Any]:
    from app.context_engine.models.context import ContextItem

    summary_text = getattr(result, "summary_text", None)
    summary_public_id = getattr(result, "summary_public_id", None)
    if not summary_text or not summary_public_id:
        return []

    summary_type = getattr(result, "summary_type", None) or compaction_type.value
    source_type = SourceType.CONVERSATION_SUMMARY
    title = "conversation_summary"
    metadata = {
        "section_id": "conversation_summary",
        "summary_id": summary_public_id,
        "summary_type": summary_type,
        "compaction_run_id": getattr(result, "run_public_id", None),
        "compaction_type": compaction_type.value,
    }
    if compaction_type == CompactionType.AGENT_LOOP:
        metadata["section_id"] = "task_state"
        source_type = SourceType.TASK_STATE
        title = "agent_loop_summary"
    elif compaction_type == CompactionType.FULL_REPLACE:
        title = "conversation_summary"
        metadata["section_id"] = "conversation_summary"

    tokens = int(getattr(result, "tokens_after", None) or 0)
    if tokens <= 0:
        tokens = max(1, len(summary_text) // 3)

    return [
        ContextItem(
            item_id=f"summary:{summary_public_id}",
            kind=ContextKind.CONVERSATION,
            source_type=source_type,
            source_ref=summary_public_id,
            title=title,
            content=str(summary_text),
            authority=80,
            priority=10,
            estimated_tokens=tokens,
            trust=ContextTrust.BUSINESS_EVIDENCE,
            metadata=metadata,
        )
    ]


def _is_compactable_item(
    item: Any,
    compaction_type: CompactionType,
    *,
    compression_policy: str | None = None,
) -> bool:
    protected_recent_conversation = (
        item.kind == ContextKind.CONVERSATION
        and item.metadata.get("retention_policy") == "recent_complete_turns"
    )
    if item.metadata.get("locked") and not protected_recent_conversation:
        return False
    if item.kind in {ContextKind.SYSTEM_RULES, ContextKind.CALL_CONTRACT, ContextKind.CURRENT_GOAL}:
        return False
    if compaction_type == CompactionType.AGENT_LOOP:
        return item.kind == ContextKind.TASK_STATE
    if compaction_type == CompactionType.FULL_REPLACE:
        return item.kind not in {ContextKind.SYSTEM_RULES, ContextKind.CALL_CONTRACT, ContextKind.CURRENT_GOAL}
    policy = (compression_policy or "").lower()
    if "preserve_evidence" in policy or "preserve_generated" in policy:
        # Test-plan generation/review first reduces the conversational layer.
        # Evidence, Knowledge, Memory and Task State are independent durable
        # layers and must never be fed into the conversation summarizer.
        return item.kind == ContextKind.CONVERSATION
    return True


def _rebuild_section_stats(
    selected: SelectedContextSet,
    included: list[Any],
) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for item in included:
        sid = item.metadata.get("section_id") or item.kind.value
        current = stats.setdefault(
            str(sid),
            {
                "included_count": 0,
                "estimated_tokens": 0,
            },
        )
        current["included_count"] += 1
        current["estimated_tokens"] += item.estimated_tokens

    for sid, old in selected.section_stats.items():
        if sid in stats:
            stats[sid] = {**old, **stats[sid]}
        else:
            stats[sid] = {**old, "included_count": 0, "estimated_tokens": 0}
    return stats


__all__ = ["ContextPreflightService", "_rebuild_budget", "_source_digest"]
# auto-appended module-level note: preflight 服务: 压缩前 sanity check(token / quota / lock)。
