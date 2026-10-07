"""make_to_fail_decision — 工厂:把被拦截的 call_tool 转为 action=fail (Phase 2.4 shared)。

Phase 2.3 实现为 ``preparation/tool_filter.py:107-115 _to_fail_decision(decision, reason)``。
Phase 2.4 改为工厂函数 ``make_to_fail_decision(decision_cls)``,返回一个绑定到具体决策类的
helper 函数,以便 Repair Agent 复用到 ``RepairDecision`` (字段集不同)。
"""

from __future__ import annotations

from typing import Any, Callable, Type


def make_to_fail_decision(decision_cls: Type[Any]) -> Callable[..., Any]:
    """返回 ``_to_fail_decision(decision, *, reason)`` 工厂函数。

    Usage:
        _to_fail_decision = make_to_fail_decision(AgentDecision)
        fail = _to_fail_decision(decision, reason="...")

    Requirements on ``decision_cls``:
        * pydantic BaseModel (or any model with ``model_construct``)
        * 字段名: action, decision_summary, public_update,
          expected_result, confidence (与 Phase 2.3 AgentDecision 一致)

    Returns:
        bound helper that constructs a new ``action="fail"`` decision with reason
        in summary and a public update explaining the block.
    """

    def _to_fail_decision(decision, *, reason: str):
        # Phase 2.9A.X: LLM 的 decision_summary 可能超过 Pydantic max_length=500，
        # 拼接 "BLOCKED: ..." 后更长。必须截断，否则 Pydantic 校验失败 →
        # exception 被 agent_loop 兜底 → repair_fallback → 跳过 TestPlanRegenTool。
        raw_summary = f"BLOCKED: {reason}; original summary: {decision.decision_summary}"
        return decision_cls(
            action="fail",
            decision_summary=raw_summary[:500],
            public_update="动态 Agent 拦截到不合规工具调用,已中止。",
            expected_result="下轮应避免相同请求或 finish",
            confidence=decision.confidence,
        )

    return _to_fail_decision


__all__ = ["make_to_fail_decision"]

# 模块定位:make_to_fail_decision 工厂(action=fail decision 适配)
#
# Phase 2.3 时代(preparation/tool_filter.py):手写 inline _to_fail_decision(...)
# Phase 2.4 改造:工厂方法 make_to_fail_decision(decision_cls),返回一个
# closure 绑定到具体 decision 类型(PreparationDecision / RepairDecision
# 字段不一样)。
#
# 链路:
#   ToolPermissionGuard 或 BudgetTracker 触发 reject
#     → filter 调 make_to_fail_decision(PrepDecision)(decision, reason)
#     → 返回 PrepDecision(action='fail', reason=reason)
#   decision_filter 在子图循环里消费这个 fail 行,跳过实际工具调用
#
# 关键约束:
#   - 工厂方法返回的是纯函数,无外部副作用;
#   - decision_cls 必须实现 .model_validate({...}) Pydantic 模型;
#   - reason 必须是稳定的字符串 token("permission_denied" / "budget_exceeded"
#     / "arg_replay"),不要用动态 f-string(便于审计聚合);
#   - 子图测试不直接调工厂,而是模拟 Permission / Budget 来触发。
