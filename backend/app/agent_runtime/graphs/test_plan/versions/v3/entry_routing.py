"""v2 入口条件路由。

本图采用 ADR-2.1-3 的"单编译图 + 入口条件路由":START 之后的第一个节点不是
固定节点,而是 ``entry_router``(cond edge 路由函数)。它读:

* ``state["current_phase"]``  — None / pre_confirm / paused / post_confirm / completed / failed / cancelled
* ``state["pause_marker"]``    — need_user_confirm / format_loss_review / None
* ``state["format_loss_confirmation"]`` — 用户决策;若存在则直接进 record_loss_decision

并把 START 后的执行路由到:
* ``initialize_task`` — 全新派发 (current_phase in {None, "pre_confirm"})
* ``resume_task``     — 重新进入 post_confirm (paused + pause_marker=need_user_confirm)
* ``record_loss_decision`` — 用户已提交 format-loss 决策,直接落库
* ``fail_task``       — 任何其它 paused 状态(防御性兜底)
"""

from __future__ import annotations

from typing import Literal

from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState


EntryTarget = Literal[
    "initialize_task",
    "resume_task",
    "record_loss_decision",
    "fail_task",
]

NODE_INITIALIZE_TASK = "initialize_task"
NODE_RESUME_TASK = "resume_task"
NODE_RECORD_LOSS_DECISION = "record_loss_decision"
NODE_FAIL_TASK = "fail_task"


def entry_router(state: TestPlanGraphState) -> EntryTarget:
    """START 后的条件边。

    LangGraph 在 invoke / resume 时都会走该路由一次,根据 state 决定下一个节点。
    状态字段说明:
      * ``current_phase``: 由节点在写入时主动设置(None / pre_confirm / paused / completed / failed / cancelled)。
      * ``pause_marker``: 中断 / 暂停的类型,目前支持 ``need_user_confirm``(章节确认等待)
        与 ``format_loss_review``(导出格式丢失等待)。
      * ``format_loss_confirmation``: 仅在 format_loss_review 路径有效,为用户在前端提交的决策 dict。
    """
    phase = state.get("current_phase")
    marker = state.get("pause_marker")
    confirmation = state.get("format_loss_confirmation")

    # 1) 全新派发:首次调用(无 checkpoint / 无 phase 标记) → 进 initialize_task 创建 AgentRun。
    if phase in (None, "pre_confirm") and marker is None:
        return NODE_INITIALIZE_TASK

    # 2) 章节确认等待:用户在 section_confirmation_interrupt 阻塞后 resume → 走 resume_task 接续生成。
    if phase == "paused" and marker == "need_user_confirm":
        return NODE_RESUME_TASK

    # 3) 格式丢失确认:用户在 format_loss_interrupt 已选 accept/retry/reject,
    #    决策 dict 写入 state.format_loss_confirmation → 落库并按决策推进。
    if (
        phase == "paused"
        and marker == "format_loss_review"
        and isinstance(confirmation, dict)
    ):
        return NODE_RECORD_LOSS_DECISION

    # 4) 防御性 fail:任何 paused 但 marker 不识别的状态(例如用户点取消前留下的 sentinel
    #    而 resume 用 thread_id 但内部状态被并发覆盖),直接终态失败而不是重启主图。
    if phase == "paused":
        return NODE_FAIL_TASK

    # 5) 已完成的任务再被 resume → 不要重启,直接 fail(测试 / 异常路径双重保险)。
    if phase in ("completed", "failed", "cancelled"):
        return NODE_FAIL_TASK

    # 兜底:无法识别的相位走重新初始化,保证 LangGraph 不会因 router 返回值意外抛错。
    return NODE_INITIALIZE_TASK


__all__ = [
    "EntryTarget",
    "NODE_INITIALIZE_TASK",
    "NODE_RESUME_TASK",
    "NODE_RECORD_LOSS_DECISION",
    "NODE_FAIL_TASK",
    "entry_router",
]
