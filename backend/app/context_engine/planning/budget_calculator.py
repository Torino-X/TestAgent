"""ContextBudget Calculator — Model-Aware Budget Manager（设计文档 §8.3）。

基本公式：
    usable_window = model_context_window - output_reserve - provider_overhead
                    - runtime_tool_reserve - safety_margin
    target_input_budget = usable_window × target_input_ratio

四级阈值（Target / Soft / Hard Compact / Absolute）全部来自 ``BudgetPolicy``
（代码 Policy），按模型窗口和 call_site 计算，不硬编码单一 85%。
窗口未知时提高 Safety Margin，不得假设 200K。
"""

from __future__ import annotations

from app.context_engine.errors import (
    ContextEngineError,
    ContextEngineStage,
    raise_engine_error,
)
from app.context_engine.models.profile import BudgetPolicy, ContextBudget

# 窗口未知时的保守策略：不假设 200K，按可用上限计算并抬高安全余量
_UNKNOWN_WINDOW_FLOOR = 32_000
_UNKNOWN_WINDOW_SAFETY_MARGIN = 0.15


class ContextBudgetCalculator:
    """根据模型窗口 + BudgetPolicy 计算 ContextBudget。纯函数，无 I/O。"""

    def calculate(
        self,
        *,
        model_context_window: int | None,
        policy: BudgetPolicy | None = None,
    ) -> ContextBudget:
        policy = policy or BudgetPolicy()

        if model_context_window is None:
            return self._calculate_unknown_window(policy)

        if model_context_window <= 0:
            raise_engine_error(
                code="context.budget.invalid_window",
                detail="model_context_window 必须为正数",
                stage=ContextEngineStage.PLANNING,
                retryable=False,
                recoverable=True,
            )

        safety_margin = int(model_context_window * policy.safety_margin_ratio)
        usable = (
            model_context_window
            - int(policy.output_reserve_tokens)
            - int(policy.provider_overhead_tokens)
            - int(policy.runtime_tool_reserve_tokens)
            - safety_margin
        )
        if usable <= 0:
            raise_engine_error(
                code="context.budget.no_usable_window",
                detail="预算计算后可用窗口为负（预留超过模型窗口）",
                stage=ContextEngineStage.PLANNING,
                retryable=False,
                recoverable=True,
                safe_metadata={
                    "window": model_context_window,
                    "reserves": int(policy.output_reserve_tokens)
                    + int(policy.provider_overhead_tokens)
                    + int(policy.runtime_tool_reserve_tokens),
                },
            )

        return ContextBudget(
            model_context_window=model_context_window,
            output_reserve=int(policy.output_reserve_tokens),
            runtime_reserve=int(policy.runtime_tool_reserve_tokens),
            provider_overhead=int(policy.provider_overhead_tokens),
            safety_margin=safety_margin,
            target_input=int(usable * policy.target_input_ratio),
            soft_threshold=int(usable * policy.soft_ratio),
            hard_compact_threshold=int(usable * policy.hard_compact_ratio),
            absolute_threshold=int(usable * policy.absolute_ratio),
            conversation_compact_threshold=(
                # The retention line is a user-visible percentage of the
                # advertised model window (the same denominator used by the
                # Context Usage card), not of the reserve-adjusted input
                # budget used by Hard/Absolute safety controls.
                int(model_context_window * policy.conversation_compact_ratio)
                if policy.conversation_compact_ratio is not None
                else 0
            ),
        )

    @staticmethod
    def _calculate_unknown_window(policy: BudgetPolicy) -> ContextBudget:
        """窗口未知：按保守下限计算并抬高 Safety Margin，禁止假设 200K。"""
        safety_margin = int(_UNKNOWN_WINDOW_FLOOR * _UNKNOWN_WINDOW_SAFETY_MARGIN)
        usable = (
            _UNKNOWN_WINDOW_FLOOR
            - int(policy.output_reserve_tokens)
            - int(policy.provider_overhead_tokens)
            - int(policy.runtime_tool_reserve_tokens)
            - safety_margin
        )
        usable = max(usable, 0)
        return ContextBudget(
            model_context_window=_UNKNOWN_WINDOW_FLOOR,
            output_reserve=int(policy.output_reserve_tokens),
            runtime_reserve=int(policy.runtime_tool_reserve_tokens),
            provider_overhead=int(policy.provider_overhead_tokens),
            safety_margin=safety_margin,
            target_input=int(usable * policy.target_input_ratio),
            soft_threshold=int(usable * policy.soft_ratio),
            hard_compact_threshold=int(usable * policy.hard_compact_ratio),
            absolute_threshold=int(usable * policy.absolute_ratio),
            conversation_compact_threshold=(
                int(_UNKNOWN_WINDOW_FLOOR * policy.conversation_compact_ratio)
                if policy.conversation_compact_ratio is not None
                else 0
            ),
        )
# auto-appended module-level note: 预算计算器: 由 token_count + model_capabilities 计算可用 token 分配。
