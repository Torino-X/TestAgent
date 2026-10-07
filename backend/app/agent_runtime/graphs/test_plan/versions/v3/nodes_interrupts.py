"""v2 interrupt 节点(Phase 2.2)。

ADR-010 §12 把章节确认与格式损失确认从 sentinel 字段
(``pause_marker`` + return-to-END) 升级为 LangGraph ``interrupt()`` +
``Command(resume=...)``。

**关键约束**:LangGraph 强制要求调用 ``interrupt()`` 的节点必须是 **同步**
(非 async)函数 —— async 节点内部调 interrupt 会抛 ``TypeError``。
本文件中两个节点函数都是 ``def``,不带 ``async``。

行为约束(规范 §12.4 + §17.3):
* ``interrupt()`` payload 与 Legacy ``need_user_confirm`` /
  ``format_loss_confirm_requested`` 业务数据保持一致,旧 SSE 消费者无感。
* ``resume`` 接受 dict,允许 ``source="user"`` 或 ``source="timeout"``;
  coordinator 强制校验。
* 超时由 ``ConfirmationTimeoutService`` 驱动(管理命令),不依赖
  Worker 内存中的 asyncio.sleep;恢复时通过持久化 ``agent_tasks`` /
  checkpointer 的 thread_id 即可拿到上次中断上下文。
* 旧任务继续走 Legacy;LangGraph v2 interrupt 路径由
  ``AgentRuntimeFeatureFlags.interrupt_v2_enabled`` 控制,默认关闭。

事件发送策略(Phase 2.2):
* interrupt 节点本身不能 await,因此 ``event_sink.emit`` 通过
  ``asyncio.get_running_loop().create_task(...)`` 投递到主循环。
* 同步路径(测试 stub / 无 loop)→ 仅记 logger,不阻塞 interrupt。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from langgraph.types import interrupt

from app.agent.enums import AgentEventType, TaskStatus

from app.agent_runtime.runtime_context import RuntimeContext

from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from .nodes_util import get_ctx

logger = logging.getLogger(__name__)


NODE_SECTION_CONFIRM_INTERRUPT = "section_confirmation_interrupt"
NODE_FORMAT_LOSS_INTERRUPT = "format_loss_interrupt"
NODE_PREPARATION_CLARIFICATION_INTERRUPT = "preparation_clarification_interrupt"


# ── 通用 helper ───────────────────────────────────────────────────────────


def _schedule_emit(ctx: RuntimeContext, event_type: str, title: str, payload: Dict[str, Any]) -> None:
    """``event_sink.emit`` 在无 await 上下文里通过 ``loop.create_task`` 投递。

    interrupt 节点不能 await,但仍要让用户在前端看到 ``TASK_RESUMED``。
    失败仅 log,不阻塞 interrupt 返回。
    """
    task_id = str(ctx.task_internal_id)
    graph_run_id = f"run-{ctx.task_internal_id}"

    async def _do() -> None:
        try:
            # 真正的事件发布:落库 + 推 SSE 推送给前端订阅者。
            # 这里是异步,但 interrupt 节点本身不能 await,所以需要先 schedule 再返回。
            await ctx.event_sink.emit(
                task_id=task_id,
                graph_run_id=graph_run_id,
                node_name="interrupt",
                event_type=event_type,
                title=title,
                content="",
                payload=payload,
            )
        except Exception:
            # 失败仅 log,不阻塞 interrupt 行为 ——
            # 即使发不出事件,interrupt 也要继续等待用户决策。
            logger.warning(
                "interrupt: event_sink.emit failed (swallowed) event=%s",
                event_type,
                exc_info=True,
            )

    try:
        # 取当前正在运行的事件循环(同步节点因为不 await,而是被 LangGraph
        # 在事件循环里调度,所以这里一定存在)。
        loop = asyncio.get_running_loop()
        # fire-and-forget:事件发出后主路径不等它完成。
        loop.create_task(_do())
    except RuntimeError:
        # 无 loop(测试 stub / 同步 fake 环境)→ 不报错,只 debug log。
        logger.debug("interrupt: no running loop, skip emit %s", event_type)


def _mark_completed(state: TestPlanGraphState, node: str) -> list:
    """append 节点名到 completed_nodes(同步版本,不调 mark_completed helper)。

    这里与 nodes_util.mark_completed 等价但为同步,避免 interrupt 节点
    (也是同步)再 import 时混淆。
    """
    completed = list(state.get("completed_nodes") or [])
    if node not in completed:
        completed.append(node)
    return completed


# ── 章节确认 interrupt ─────────────────────────────────────────────────────


def section_confirmation_interrupt_node(
    state: TestPlanGraphState, config: Any
) -> dict:
    """章节策略确认 interrupt(同步节点)。

    阻塞时把``section_suggestions``序列化进 interrupt payload,
    前端 reducer 看到后弹出"确认章节处理策略"卡片;用户提交后
    LangGraph 把 decision 通过 ``Command(resume=...)`` 传回,本函数继续执行。
    """
    ctx: RuntimeContext = get_ctx(config)
    # 审计: 记录该节点已执行(供后续审计/重入检查使用)
    completed = _mark_completed(state, NODE_SECTION_CONFIRM_INTERRUPT)

    # interrupt() 会让 worker 暂停在该节点,直到用户从前端做出决策并触发 resume。
    # payload 是发给前端的展示数据(章节列表 + 超时截止时间)。
    sections = state.get("section_suggestions") or {}
    payload = {
        "kind": "section_confirmation",
        "task_id": str(ctx.task_internal_id),
        "sections": sections.get("sections") if isinstance(sections, dict) else sections,
        "timeout_at": None,
    }

    # 核心: 调 LangGraph interrupt() 抛一个特殊的 control flow 异常,
    # 由 LangGraph 持久化当前 state 并等待 Command(resume=...)。
    # 这里阻塞,直到 LangGraphRunCoordinator 通过 ainvoke(Command(resume=...)) 把决策传回。
    decision = interrupt(payload)

    # Resume 后: 严格校验 resume payload。
    if not isinstance(decision, dict):
        # kind 错配或非 dict payload 直接 raise,让 worker 写入 last_error,
        # 路由到 fail_task_node。
        raise ValueError(
            f"section_confirmation resume payload must be dict, got {type(decision).__name__}"
        )
    if decision.get("kind") != "section_confirmation":
        raise ValueError(
            f"section_confirmation resume kind mismatch: {decision.get('kind')!r}"
        )

    # 拿到用户的章节策略(sections 数组就是用户选择 AI 生成/保留模板 的列表)
    new_section_confirm = {
        "sections": decision.get("sections") or [],
        "source": decision.get("source") or "user",  # "user" 或 "timeout"(超时服务驱动)
    }

    # Resume 后异步通知 SSE 端"任务已恢复"
    _schedule_emit(
        ctx,
        AgentEventType.TASK_RESUMED.value,
        "任务已恢复",
        {"source": new_section_confirm["source"]},
    )

    # 返回 partial state 字典,LangGraph merge 后下一节点(generate_test_plan)即可读到。
    # 注意 task_status 切到 GENERATING,因为接下来要让 TestPlanGeneratorTool 真正产出。
    return {
        "section_confirm_config": new_section_confirm,
        "pause_marker": None,                           # 清掉 interrupt 标记
        "current_phase": "post_confirm",                # 进入后确认阶段
        "task_status": TaskStatus.GENERATING.value,     # 状态标记:正在生成
        "current_node": NODE_SECTION_CONFIRM_INTERRUPT, # 审计
        "completed_nodes": completed,
    }


# ── 格式损失 interrupt ────────────────────────────────────────────────────


def format_loss_interrupt_node(
    state: TestPlanGraphState, config: Any
) -> dict:
    """格式损失确认 interrupt(同步节点)。

    DocxFormatCheckTool 在模板回填过程中检测到 secondary 结构丢失
    (书签/批注/脚注/尾注),会把 loss 列表写入 state.pending_format_losses,
    本节点负责把这些信息推给前端,等用户决策:
      - accept:接受当前产物(带损失),走 finalize
      - retry: 重新出文档(降低 fidelity / 调整模板)
      - reject: 拒绝任务,走 fail_task
    """
    ctx: RuntimeContext = get_ctx(config)
    completed = _mark_completed(state, NODE_FORMAT_LOSS_INTERRUPT)

    losses = state.get("pending_format_losses") or []
    timeout_seconds = int(state.get("format_loss_timeout_seconds") or 300)

    # 中断 payload 包含 3 个用户可见的 choices;timeout 提示前端倒计时。
    payload = {
        "kind": "format_loss",
        "task_id": str(ctx.task_internal_id),
        "loss_count": len(losses) if isinstance(losses, list) else 0,
        "loss_details_for_user": losses,
        "losses": losses,
        "choices": ["accept", "retry", "reject"],
        "timeout_seconds": timeout_seconds,
    }

    decision = interrupt(payload)

    # 校验 resume payload。
    if not isinstance(decision, dict):
        raise ValueError(
            f"format_loss resume payload must be dict, got {type(decision).__name__}"
        )
    if decision.get("kind") != "format_loss":
        raise ValueError(
            f"format_loss resume kind mismatch: {decision.get('kind')!r}"
        )
    chosen = decision.get("decision")
    # 防御: 只接受白名单内的决策值,否则 raise → fail_task_node。
    if chosen not in ("accept", "retry", "reject"):
        raise ValueError(f"format_loss decision must be accept/retry/reject, got {chosen!r}")

    # 把用户决策结构化保存,供下游 routing + 后续审计使用。
    confirmation = {
        "decision": chosen,
        "source": decision.get("source") or "user",
        "losses": losses,
    }

    # 发一个用户决策的 SSE 事件,前端 reducer 收到后关掉 banner,
    # 切回正常收尾 / 重试 / 终止状态。
    _schedule_emit(
        ctx,
        AgentEventType.FORMAT_LOSS_DECISION_RECORDED.value,
        f"用户决策:{chosen}",
        {
            "decision": chosen,
            "source": confirmation["source"],
            # The interrupt route does not pass through record_loss_decision;
            # preserve the checked losses in the durable receipt event so a
            # reloaded conversation can show the user's actual selection.
            "losses": losses,
        },
    )

    # 返回 partial state;路由函数 route_after_format_interrupt 根据
    # confirmation.decision 决定下一节点(prepare_export / finalize_task / fail_task)。
    return {
        "format_loss_confirmation": confirmation,
        "pause_marker": None,
        # retry 时回到导出流程 / accept 时进入完成收尾。
        "current_phase": "post_confirm" if chosen == "retry" else "completed",
        "task_status": (
            TaskStatus.EXPORTING.value if chosen == "retry" else TaskStatus.COMPLETED.value
        ),
        "current_node": NODE_FORMAT_LOSS_INTERRUPT,
        "completed_nodes": completed,
    }


def preparation_clarification_interrupt_node(
    state: TestPlanGraphState, config: Any
) -> dict:
    """Pause after the second retrieval round and validate structured input."""
    ctx: RuntimeContext = get_ctx(config)
    completed = _mark_completed(state, NODE_PREPARATION_CLARIFICATION_INTERRUPT)
    cards = state.get("clarification_cards") or []
    payload = {
        "kind": "preparation_clarification",
        "task_id": str(ctx.task_internal_id),
        "cards": cards,
        "retrieval_round": int(state.get("retrieval_round") or 0),
    }
    decision = interrupt(payload)
    if not isinstance(decision, dict):
        raise ValueError("preparation clarification resume payload must be dict")
    if decision.get("kind") != "preparation_clarification":
        raise ValueError(
            f"preparation clarification resume kind mismatch: {decision.get('kind')!r}"
        )
    answers = decision.get("answers")
    conservative_ids = decision.get("conservative_gap_ids") or []
    if not isinstance(answers, dict) or not isinstance(conservative_ids, list):
        raise ValueError("preparation clarification answers and conservative_gap_ids are required")
    card_by_id = {
        str(card.get("id")): card for card in cards if isinstance(card, dict) and card.get("id")
    }
    if not card_by_id:
        raise ValueError("preparation clarification has no pending cards")
    conservative = {str(item) for item in conservative_ids}
    if not conservative.issubset(set(card_by_id)):
        raise ValueError("preparation clarification references an unknown gap")
    normalized_answers: dict[str, str] = {}
    for gap_id, card in card_by_id.items():
        answer = str(answers.get(gap_id) or "").strip()[:2000]
        can_choose_conservative = bool(card.get("allow_conservative_scope"))
        if not answer and not (can_choose_conservative and gap_id in conservative):
            raise ValueError(f"preparation clarification requires an answer for {gap_id!r}")
        if answer:
            normalized_answers[gap_id] = answer

    response = {
        "answers": normalized_answers,
        "conservative_gap_ids": sorted(conservative),
        "source": decision.get("source") or "user",
    }
    _schedule_emit(
        ctx,
        AgentEventType.TASK_RESUMED.value,
        "已收到补充信息，正在重新评估需求",
        {"source": response["source"], "clarification_count": len(card_by_id)},
    )
    return {
        "clarification_answers": response,
        "pause_marker": None,
        "current_phase": "pre_confirm",
        "task_status": TaskStatus.RUNNING.value,
        "current_node": NODE_PREPARATION_CLARIFICATION_INTERRUPT,
        "completed_nodes": completed,
    }


__all__ = [
    "NODE_SECTION_CONFIRM_INTERRUPT",
    "NODE_FORMAT_LOSS_INTERRUPT",
    "NODE_PREPARATION_CLARIFICATION_INTERRUPT",
    "section_confirmation_interrupt_node",
    "format_loss_interrupt_node",
    "preparation_clarification_interrupt_node",
]
