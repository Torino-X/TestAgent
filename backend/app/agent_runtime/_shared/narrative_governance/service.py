"""Phase 2.9C narrative governance service — single public entry.

The service is the only object that should be invoked by emitters
on either side (Phase 2.9A Builder / Phase 2.9B Dynamic Envelope).

Pipeline (Phase 2.9C §6.1):

    policy → sanitizer → fact → signature → dedup → projection →
        deterministic compression → optional LLM compression →
        quality gate → cost record → result

Failure paths:

* The pipeline NEVER raises. Any exception is captured, recorded in
  ``metadata['error']``, and the service returns a ``fallback``
  result.
* Sanitization is always run (even when other gates are disabled).
* Setting persistence is the caller's responsibility (out of scope
  here — uses ``NarrativeSettingsService``).
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent_runtime._shared.narrative_governance.cache import (
    Budget,
    NarrativeCompressionCache,
    stub_compress,
)
from app.agent_runtime._shared.narrative_governance.compressor import (
    compress as compress_payload,
    measure_chars,
)
from app.agent_runtime._shared.narrative_governance.dedup import decide as dedup_decide
from app.agent_runtime._shared.narrative_governance.policy import (
    project as project_payload,
    resolve_level,
)
from app.agent_runtime._shared.narrative_governance.quality_validator import (
    validate_and_repair,
)
from app.agent_runtime._shared.narrative_governance.signature import build_signature
from app.agent_runtime._shared.narrative_governance.schemas import (
    NarrativeContentBudget,
    NarrativeGovernanceContext,
    NarrativeGovernanceResult,
    NarrativeQualityReport,
    NarrativeRepetitionState,
    NarrativeCostRecord,
)


logger = logging.getLogger(__name__)


class NarrativeGovernanceService:
    """Phase 2.9C unified governance entry.

    Stateless apart from the injected ``cache`` and ``budget``. ``cache``
    defaults to an in-process LRU; tests inject a mock to assert hits.
    The caller is expected to provide:

    * ``context`` — :class:`NarrativeGovernanceContext`
    * ``candidate`` — public-update payload (dict or ``None``)
    * ``repetition`` — current :class:`NarrativeRepetitionState` for
      the signature, or ``None`` for the first occurrence.

    The service decides ``emit / suppress / aggregate / fallback``
    and returns the sanitised payload ready for the existing emitter
    boundaries.
    """

    def __init__(
        self,
        *,
        cache: NarrativeCompressionCache | None = None,
        budget: Budget | None = None,
        compress_threshold_chars: int = 480,
    ) -> None:
        self._cache = cache or NarrativeCompressionCache()
        self._budget = budget or Budget()
        self._compress_threshold_chars = compress_threshold_chars

    @property
    def cache(self) -> NarrativeCompressionCache:
        return self._cache

    @property
    def budget(self) -> Budget:
        return self._budget

    # ── Public entry ──────────────────────────────────────────────

    def govern(
        self,
        *,
        candidate: dict[str, Any] | None,
        context: NarrativeGovernanceContext,
        repetition: NarrativeRepetitionState | None = None,
    ) -> NarrativeGovernanceResult:
        """Run the governance pipeline.

        Returns a :class:`NarrativeGovernanceResult`; never raises.
        """
        metadata: dict[str, Any] = {
            "governance_version": "2.9C-v1",
            "governance_applied": True,
        }
        quality = NarrativeQualityReport(passed=True)
        cost = NarrativeCostRecord()
        if repetition is None:
            repetition = NarrativeRepetitionState(signature_hash="")

        try:
            return self._govern_impl(
                candidate=candidate,
                context=context,
                repetition=repetition,
                quality=quality,
                cost=cost,
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001 — Phase 2.9C §5.6
            logger.warning("narrative governance failed: %s", exc)
            return NarrativeGovernanceResult(
                action="fallback",
                public_update=candidate if isinstance(candidate, dict) else None,
                detail_level=resolve_level(context),
                signature=None,
                repetition_count=repetition.occurrence,
                quality_report=NarrativeQualityReport(
                    passed=False,
                    fallback_used=True,
                    violations=[],
                ),
                cost=cost,
                metadata={**metadata, "error": repr(exc)},
            )

    # ── Implementation ────────────────────────────────────────────

    def _govern_impl(
        self,
        *,
        candidate: dict[str, Any] | None,
        context: NarrativeGovernanceContext,
        repetition: NarrativeRepetitionState,
        quality: NarrativeQualityReport,
        cost: NarrativeCostRecord,
        metadata: dict[str, Any],
    ) -> NarrativeGovernanceResult:
        signature = build_signature(candidate, context)
        occurrence = max(repetition.occurrence, 1)
        previous_outcome_code = repetition.last_outcome_code

        decision = dedup_decide(
            context=context,
            occurrence=occurrence,
            previous_outcome_code=previous_outcome_code,
            candidate=candidate,
        )

        # Always sanitise at the gate, regardless of decision.
        sanitised_candidate, quality_report = validate_and_repair(
            candidate, context,
        )
        # Merge existing (default) report for compat with callers that
        # provided one.
        quality = quality_report
        if quality.violations:
            metadata["quality_violation_codes"] = sorted(
                {v.code for v in quality.violations},
            )

        # 1. Suppress / aggregate decisions — emit agg payload.
        if decision.action == "suppress":
            return NarrativeGovernanceResult(
                action="suppress",
                public_update=None,
                detail_level=resolve_level(context),
                signature=signature,
                repetition_count=occurrence,
                quality_report=quality,
                cost=cost,
                metadata={**metadata, "decision_reason": decision.reason},
            )

        if decision.action == "aggregate":
            template = _AGGREGATE_TEMPLATES.get(decision.aggregate_template)
            aggregate_payload = dict(template) if template else {}
            aggregate_payload.update(
                {
                    "level": "warning",
                    "kind": str((sanitised_candidate or {}).get("kind") or "rep"),
                }
            )
            # Project aggregate payload through the same projection
            # rules so detail budget still applies.
            projected = project_payload(aggregate_payload, context=context)
            return NarrativeGovernanceResult(
                action="aggregate",
                public_update=projected,
                detail_level=resolve_level(context),
                signature=signature,
                repetition_count=occurrence,
                quality_report=quality,
                cost=cost,
                metadata={
                    **metadata,
                    "decision_reason": decision.reason,
                    "aggregate_template": decision.aggregate_template,
                },
            )

        # 2. Emit path — project + deterministic compress.
        level = resolve_level(context)
        budget = NarrativeContentBudget.for_level(level)
        projected = project_payload(sanitised_candidate, context=context)
        if projected is None and decision.action == "fallback":
            projected = dict(_FALLBACK_TEMPLATE)

        compressed = compress_payload(
            projected or {},
            budget=budget,
            allowlist=context.allowed_detail_keys,
        )
        cost.deterministic_chars_in = measure_chars(sanitised_candidate or {})
        cost.deterministic_chars_out = measure_chars(compressed)

        # 3. Optional LLM compression — only fires when deterministic
        # output is still large AND stub/LLM is enabled.
        if (
            cost.deterministic_chars_out > self._compress_threshold_chars
            and self._budget.can_call()
        ):
            stub_result = stub_compress(
                compressed,
                level=level.value,
                budget=self._budget,
                cache=self._cache,
                schema_version="v2.9C-v1",
            )
            if stub_result is not None and isinstance(stub_result.payload, dict):
                compressed = stub_result.payload
                cost.llm_compression_called = True
                cost.llm_model = "stub"
                cost.prompt_tokens = stub_result.prompt_tokens
                cost.completion_tokens = stub_result.completion_tokens
                cost.latency_ms = stub_result.latency_ms
                cost.cache_hit = stub_result.cache_hit

        final_payload = dict(compressed)
        final_payload["governance_metadata"] = {
            "version": "2.9C-v1",
            "detail_level": level.value,
            "signature_hash": signature.hash,
            "repetition_count": occurrence,
            "action": "emit",
            "applied": True,
        }

        return NarrativeGovernanceResult(
            action="emit",
            public_update=final_payload,
            detail_level=level,
            signature=signature,
            repetition_count=occurrence,
            quality_report=quality,
            cost=cost,
            metadata=metadata,
        )


_AGGREGATE_TEMPLATES: dict[str, dict[str, Any]] = {
    "repeat": {
        "headline": "类似问题再次出现",
        "summary": "上一次执行情况已记录。本次结果与之前相同,系统正在改变策略。",
        "impact": "用户可观察到行为已收敛但未解决。",
        "next_action": "等待系统切换修复路径或重新生成。",
        "details": ["已重新整理本次出现的事实摘要"],
    },
    "stable": {
        "headline": "相同问题已稳定出现",
        "summary": "连续多次出现相同结构问题,已不再重试相同路径。",
        "impact": "建议切换修复路径或请求人工确认。",
        "next_action": "等待用户决策或更换模板。",
        "details": ["自动重试已暂停"],
    },
}

_FALLBACK_TEMPLATE: dict[str, Any] = {
    "headline": "执行说明已生成",
    "summary": "本次执行结果已记录。",
    "impact": "",
    "next_action": "",
    "details": [],
    "level": "info",
    "kind": "fallback",
}


__all__ = ["NarrativeGovernanceService"]
