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
    """START 后的条件边。"""
    phase = state.get("current_phase")
    marker = state.get("pause_marker")
    confirmation = state.get("format_loss_confirmation")

    # 1) 全新派发
    if phase in (None, "pre_confirm") and marker is None:
        return NODE_INITIALIZE_TASK

    # 2) 章节确认等待 → 重新进入 post_confirm
    if phase == "paused" and marker == "need_user_confirm":
        return NODE_RESUME_TASK

    # 3) 格式丢失确认 → 若已提交决策就落库
    if (
        phase == "paused"
        and marker == "format_loss_review"
        and isinstance(confirmation, dict)
    ):
        return NODE_RECORD_LOSS_DECISION

    # 4) 其它 paused(用户点取消前留下的 sentinel)→ 防御性 fail
    if phase == "paused":
        return NODE_FAIL_TASK

    # 5) 兜底:任何非常规状态都进 initialize_task 重新开始(测试场景)
    if phase in ("completed", "failed", "cancelled"):
        return NODE_FAIL_TASK

    return NODE_INITIALIZE_TASK


__all__ = [
    "EntryTarget",
    "NODE_INITIALIZE_TASK",
    "NODE_RESUME_TASK",
    "NODE_RECORD_LOSS_DECISION",
    "NODE_FAIL_TASK",
    "entry_router",
]


# ════════════════════════════════════════════════════════════════════════════════
# v2_frozen 历史快照(Phase 2.2)。
# 与同目录的 v2/ 字节级镜像;目录独立是为了 Phase 2.2.x 升级不被绑死,
# v2 可演进,v2_frozen 永远冻结(供回滚 / 测试稳定快照用)。
#
# 读代码时:v2_frozen 内容与 v2 几乎一致,优先看 v2 的注释(v2 改动先行)。
# 唯一差异:LangGraph 编译时 graph_name 不同
# (`${GRAPH_NAME_TEST_PLAN}_v2_frozen` vs `${GRAPH_NAME_TEST_PLAN}_v2`),
# 不影响代码组织。
#
# ⚠️ 不要让 v2_frozen 与 v3 直接对照读 ——
#   v2_frozen 用 sentinel pause_marker,v3 走真 LangGraph interrupt,差异大;
#   想知道中断怎么跑,读 v3/nodes_interrupts.py(v3 已加注释)。
# ════════════════════════════════════════════════════════════════════════════════
