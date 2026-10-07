"""Phase 2.2 — interrupt 节点前后的条件路由。

interrupt 节点返回 dict 时已经把决定映射到 ``state.task_status`` /
``state.current_phase``,但 LangGraph 的边是固定的,需要条件路由器
重新读 state 决定下一节点。

设计:
* ``route_after_format_check_for_interrupt``:
    * passed / warning → ``finalize_task``
    * failed AND format_loop_count < MAX_FORMAT_LOOPS → ``prepare_export``(下一保真度重试)
    * loss_detected → ``format_loss_interrupt``(interrupt 节点)
    * failed AND format_loop_count >= MAX_FORMAT_LOOPS → ``format_loss_interrupt``
      (合成 losses 后等用户决策)

* ``route_after_format_interrupt``:
    * ``format_loss_confirmation.decision == "retry"`` → ``prepare_export``
    * ``format_loss_confirmation.decision == "accept"`` → ``finalize_task``
    * ``format_loss_confirmation.decision == "reject"`` → ``fail_task``
    * 兜底(没决策 / None)→ ``fail_task``(防御性)

* ``route_after_section_interrupt``:章节 interrupt 永远只有一个出口
  ``resume_task``,无需条件路由。
"""

from __future__ import annotations

from app.agent_runtime.graphs.test_plan.constants import MAX_FORMAT_LOOPS
from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState


NODE_FINALIZE_TASK = "finalize_task"
NODE_PREPARE_EXPORT = "prepare_export"
NODE_FORMAT_LOSS_INTERRUPT = "format_loss_interrupt"
NODE_FAIL_TASK = "fail_task"
# Phase 2.8D:Summary 节点插在 format_check 通过 / interrupt resume "accept" 之后
NODE_GENERATE_COMPLETION_SUMMARY = "generate_completion_summary"


def route_after_format_check_for_interrupt(state: TestPlanGraphState) -> str:
    """interrupt 路径下,``check_docx_format`` 后的条件边。"""
    result = state.get("format_check_result") or {}
    level = str(result.get("level") or "")
    loops = int(state.get("format_loop_count") or 0)

    if level in ("passed", "warning"):
        # Phase 2.8D:format_check 通过 → 先经 Summary 节点再 finalize
        return NODE_GENERATE_COMPLETION_SUMMARY

    if level == "loss_detected" and loops <= MAX_FORMAT_LOOPS:
        return NODE_FORMAT_LOSS_INTERRUPT

    if level == "failed":
        if loops < MAX_FORMAT_LOOPS:
            return NODE_PREPARE_EXPORT
        return NODE_FORMAT_LOSS_INTERRUPT

    # 兜底:未识别 level 一律走 interrupt(防御性)
    return NODE_FORMAT_LOSS_INTERRUPT


def route_after_format_interrupt(state: TestPlanGraphState) -> str:
    """``format_loss_interrupt`` 节点后的条件边。

    期望 ``state.format_loss_confirmation.decision`` 已被 interrupt 节点写入。
    兜底:未识别 / 缺失 → ``fail_task``,绝不直接吞掉。
    """
    confirmation = state.get("format_loss_confirmation") or {}
    decision = confirmation.get("decision") if isinstance(confirmation, dict) else None

    if decision == "retry":
        return NODE_PREPARE_EXPORT
    if decision == "accept":
        # Phase 2.8D:accept 决策也要先经 Summary 再 finalize
        return NODE_GENERATE_COMPLETION_SUMMARY
    if decision == "reject":
        return NODE_FAIL_TASK

    # 兜底:防御性,绝不静默
    return NODE_FAIL_TASK


__all__ = [
    "NODE_FINALIZE_TASK",
    "NODE_PREPARE_EXPORT",
    "NODE_FORMAT_LOSS_INTERRUPT",
    "NODE_FAIL_TASK",
    "route_after_format_check_for_interrupt",
    "route_after_format_interrupt",
]
