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


def route_after_format_check_for_interrupt(state: TestPlanGraphState) -> str:
    """interrupt 路径下,``check_docx_format`` 后的条件边。

    与 legacy ``route_after_format_check`` 的差别只是 blocked 分支:这里
    阻塞不去 legacy pause,而是去 ``format_loss_interrupt``(真 interrupt),
    让用户在前端选 accept/retry/reject。

    Phase 2.9A.18: 字面值统一为 ``passed / warning / blocked / failed``。
    兼容旧 ``loss_detected``。
    """
    result = state.get("format_check_result") or {}
    # Phase 2.9A.18:status 字段是首选;level 仅做向后兼容
    level = (
        str(result.get("status") or result.get("level") or "")
        .strip()
        .lower()
    )
    # 历史 task 的 level 字段可能仍是 "loss_detected",对齐到 "blocked"。
    if level == "loss_detected":
        level = "blocked"
    loops = int(state.get("format_loop_count") or 0)

    if level in ("passed", "warning"):
        # Phase 2.9A.24: format_check 通过 → 直接 finalize(跳过 generate_completion_summary)
        return NODE_FINALIZE_TASK

    if level == "blocked" and loops <= MAX_FORMAT_LOOPS:
        # 阻塞(次要结构丢失)→ 真 interrupt 节点,等用户决策。
        return NODE_FORMAT_LOSS_INTERRUPT

    if level == "failed":
        if loops < MAX_FORMAT_LOOPS:
            return NODE_PREPARE_EXPORT  # 重试导出(降保真度或换模板)
        return NODE_FORMAT_LOSS_INTERRUPT  # 重试次数耗尽,也让用户决定

    # 兜底:未识别 level 一律走 interrupt(防御性) ——
    # 与 legacy 不同:这里 strict 升级为 interrupt,因为这条路径本身就是真 interrupt 模式。
    return NODE_FORMAT_LOSS_INTERRUPT


def route_after_format_interrupt(state: TestPlanGraphState) -> str:
    """``format_loss_interrupt`` 节点后的条件边。

    期望 ``state.format_loss_confirmation.decision`` 已被 interrupt 节点写入。
    兜底:未识别 / 缺失 → ``fail_task``,绝不直接吞掉。
    """
    confirmation = state.get("format_loss_confirmation") or {}
    # 用户选择:
    #   accept → 接受当前产物(尽管有损失),进入收尾 finalize;
    #   retry  → 重新走一遍 PREP/导出;
    #   reject → 直接终态失败(用户放弃)。
    decision = confirmation.get("decision") if isinstance(confirmation, dict) else None

    if decision == "retry":
        return NODE_PREPARE_EXPORT
    if decision == "accept":
        # Phase 2.9A.24: accept → 直接 finalize(跳过 generate_completion_summary)
        return NODE_FINALIZE_TASK
    if decision == "reject":
        return NODE_FAIL_TASK

    # 兜底:防御性,绝不静默 — 决策缺失 / 值不在枚举内一律 fail,
    # 否则会被 endless loop 卡住或被错误推进 finalize。
    return NODE_FAIL_TASK


__all__ = [
    "NODE_FINALIZE_TASK",
    "NODE_PREPARE_EXPORT",
    "NODE_FORMAT_LOSS_INTERRUPT",
    "NODE_FAIL_TASK",
    "route_after_format_check_for_interrupt",
    "route_after_format_interrupt",
]