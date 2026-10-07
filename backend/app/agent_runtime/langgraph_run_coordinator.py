"""LangGraphRunCoordinator —— Phase 2.1 + 2.2 业务执行入口。

**不挂 API**(ADR-2.1-7);仅供内部测试与 Phase 2.2 显式调用。

三种入口路径:

* **sentinel 路径(Phase 2.1,默认)**:用 ``pause_marker`` 字段 + return-to-END,
  ``run_pre_confirm`` / ``run_post_confirm`` / ``resume_format_loss`` 三方法。
* **interrupt 路径(Phase 2.2,``interrupt_v2_enabled=True`` 时切换)**:
  用 LangGraph ``interrupt()`` + ``Command(resume=...)``,新方法
  ``run_pre_confirm_interrupted`` / ``resume_section_confirmation`` /
  ``resume_format_loss_interrupt``。

Coordinator 自身**不**根据 feature flag 自动分流 — 入口层(dispatcher /
Phase 2.3 API 接入)根据 ``AgentRuntimeFeatureFlags.interrupt_v2_enabled``
决定调哪一组方法。这样:
* 单测可显式选任一路径,不依赖全局 flag。
* sentinel 路径与 interrupt 路径互不污染,sentinel 路径用作"行为等价
  baseline"测试,interrupt 路径是新覆盖。

**持久化**:每个方法完成后通过 ``write_checkpoint`` 同步任务投影
(可选,测试可传 None;MemorySaver 已经够测试用)。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from langgraph.types import Command

from app.core.logging import LogEvent, log_event

from .feature_flags import get_feature_flags
from .graph_runtime_service import GraphRuntimeService
from .graph_registry import GraphRegistry, GraphVersionNotFound
from .graphs.test_plan.constants import (
    GRAPH_NAME_TEST_PLAN,
    GRAPH_VERSION_V2,
    GRAPH_VERSION_V3,
)
from .graphs.test_plan.state import (
    TestPlanGraphState,
    assert_state_serializable,
    make_empty_state,
)
# Phase 2.5: incremental subgraph 是独立 graph_name + version
from .incremental.subgraph import (
    GRAPH_NAME_INCREMENTAL,
    GRAPH_VERSION_INCREMENTAL_V3,
)

logger = logging.getLogger(__name__)


def _resolve_default_graph_version() -> str:
    """从 settings 读默认 graph_version;settings 未配置 → v3。

    Phase 2.8R-D:Coordinator 不再硬编码 v2,而是:
      1. state 已含 ``graph_version`` → 用 state 的(不可变)
      2. settings 有 ``agent_runtime_default_graph_version`` → 用 settings
      3. 兜底 v3
    """
    try:
        from app.core.config import get_settings
        value = getattr(get_settings(), "agent_runtime_default_graph_version", None)
        if value:
            return str(value)
    except Exception:  # noqa: BLE001 — import failure shouldn't crash coordinator
        pass
    return GRAPH_VERSION_V3


def _derive_locked_section_ids(payload):
    """从 payload.section_confirm_config 派生 locked_section_ids。

    镜像 ADR-2.4-4:用户已确认保留模板的 sections 即"锁定"。
    """
    cfg = payload.get("section_confirm_config") or {}
    sections = cfg.get("sections") if isinstance(cfg, dict) else None
    if not isinstance(sections, list):
        return []
    out = []
    for s in sections:
        if not isinstance(s, dict):
            continue
        action = (s.get("suggested_action") or s.get("action") or "").lower()
        if action == "keep_template":
            sid = s.get("section_id") or s.get("id") or s.get("title")
            if sid and sid not in out:
                out.append(str(sid))
    return out


@dataclass
class RunOutcome:
    """LangGraphRunCoordinator 三方法的统一返回。

    Phase 2.9A.7: 新增 ``pending_interrupt`` 字段区分:
      * ``pending_interrupt=None`` → 终态(completed / paused / failed),
        由 ``completed`` + ``paused`` 两个 flag 组合表达
      * ``pending_interrupt=<name>`` → 走到 interrupt 节点挂起,
        LangGraph 等待 ``Command(resume=...)`` 解挂
    """

    completed: bool
    paused: bool
    pause_marker: Optional[str]
    task_status: Optional[str]
    final_state: Dict[str, Any]
    current_node: Optional[str]
    pending_interrupt: Optional[str] = None


class CoordinatorNotWiredError(RuntimeError):
    """coordinator 在不完整配置下被调用。"""


class ResumeNoPendingInterruptError(ValueError):
    """Phase 2.9A.7: Resume 时 LangGraph checkpoint 中没有 pending interrupt。

    触发场景:
      * 已 completed/failed/cancelled 的 task 又被 resume;
      * pre-confirm 走的是 sentinel pause_marker 路径而非真 interrupt,
        Resume 时 LangGraph 找不到 pending interrupt;
      * thread_id 错配(checkpoint 不存在)。

    行为:Resume 必须立即失败,Worker 标 mark_failed(error_code=
    "RESUME_NO_PENDING_INTERRUPT"),不允许 silently no-op。

    继承 ``ValueError`` 是为兼容 Worker 的 ``_NON_RETRYABLE`` 异常列表
    (Phase 2.9A.5)。Resume 错误是确定性契约错误,重试无意义。
    """


class ResumeNoProgressError(ValueError):
    """Phase 2.9A.7: Resume 调用 ``Command(resume=...)`` 后 checkpoint
    没有推进。

    触发场景:
      * checkpointer 没生效(missing / fallback MemorySaver);
      * graph version 错配(resume 用 default,但 pre-confirm 用 v3);
      * 决策 payload 非法导致 interrupt 节点拒绝,LangGraph 抛
        ``GraphInterrupt`` 但被 swallow。

    行为:Resume 必须立即失败,Worker 标 mark_failed(error_code=
    "RESUME_NO_PROGRESS"),不允许 silently no-op。
    """


class ResumeGraphVersionNotFoundError(ValueError):
    """Phase 2.9A.7: Resume 解析 graph_version 失败。

    触发场景:
      * state["graph_version"] 写入了非法值(例如 ``""`` / ``"v9"``);
      * registry 注册了 v1+v2+v3 但 call 时需要 v4,GraphVersionNotFound
        上抛,coordinator 转译成此异常。

    行为:Resume 必须立即失败,Worker 标 mark_failed(error_code=
    "RESUME_GRAPH_VERSION_NOT_FOUND"),不允许 silently fallback 到
    default graph(那样会让旧 v3 checkpoint 用新 default graph 重跑,
    拓扑不兼容)。
    """


class LangGraphRunCoordinator:
    """业务执行入口。Phase 2.1 内部使用,生产热路径不挂。

    :param registry: ``GraphRegistry`` 实例;coordinator 从它取 v2 compiled
                     graph。必须 register 过 v2(``build_default_v2`` 或
                     ``include_v2=True``)。
    """

    def __init__(
        self,
        *,
        registry: GraphRegistry,
        runtime: GraphRuntimeService,
        checkpointer: Any,
        context_factory: Optional[Callable[[Dict[str, Any]], Any]] = None,
        write_checkpoint: Optional[Callable[[str, str, Optional[str], Optional[str]], Any]] = None,
    ) -> None:
        self._registry = registry
        self._runtime = runtime
        self._checkpointer = checkpointer
        self._context_factory = context_factory
        self._write_checkpoint = write_checkpoint

    # ── Sentinel 路径(Phase 2.1,默认) ───────────────────────────────────

    async def run_pre_confirm(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        initial_payload: Dict[str, Any],
    ) -> RunOutcome:
        """全新任务 → pre-confirm 节点链 → 暂停于 need_user_confirm。"""
        state = self._initial_state(
            task_id=task_id,
            graph_run_id=graph_run_id,
            payload=initial_payload,
        )
        return await self._invoke_and_capture(state)

    # ── Phase 2.4: Review Repair Agent 入口 ───────────────────────────────

    async def run_repair(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        initial_payload: Dict[str, Any],
    ) -> RunOutcome:
        """Phase 2.4: 走 v2 图主路径,通过 route_after_review 触发 Repair subgraph。

        与 ``run_pre_confirm`` 共享同一 v2 graph;区别在于
        ``_initial_state`` 注入 ``repair_agent_enabled`` 标志与
        ``locked_section_ids``,使 ``route_after_review`` 在检测到
        block issues 时把路由切到 ``NODE_REPAIR_SUBGRAPH``。

        Phase 2.4 范围内此方法仅供内部测试显式调用(镜像
        ``run_preparation`` Phase 2.3 模式),不挂 ``app/api/v1/agent_tasks.py``。
        """
        state = self._initial_state(
            task_id=task_id,
            graph_run_id=graph_run_id,
            payload=initial_payload,
        )
        return await self._invoke_and_capture(state)

    # ── Phase 2.5: Incremental Task Agent 入口 ──────────────────────────

    async def run_incremental(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        initial_payload: Dict[str, Any],
    ) -> RunOutcome:
        """Phase 2.5: 走 ``incremental_test_plan`` 独立图。

        与 Phase 2.1 / 2.4 主图(``test_plan_generation`` v2)**隔离**:
        - 独立 graph_name("incremental_test_plan"),Thread_id 共享 task_id;
        - 独立 schema_version=5;
        - payload 必含 ``incremental_intent``(由上层 MessageService 从
          ``IntentContext.last_completed_artifact_public_id`` 派生并序列化);
        - payload 必含 ``source_artifact_public_id``(同上,显式传入)。

        Phase 2.5 范围内此方法仅供内部测试显式调用(镜像
        ``run_preparation`` / ``run_repair`` 模式),不挂 API 接缝。
        """
        state = self._initial_state(
            task_id=task_id,
            graph_run_id=graph_run_id,
            payload=initial_payload,
        )
        # 强制覆盖 graph_name → 走 incremental subgraph
        state["graph_name"] = GRAPH_NAME_INCREMENTAL
        requested_version = str(initial_payload.get("graph_version") or "").strip()
        state["graph_version"] = requested_version or GRAPH_VERSION_INCREMENTAL_V3
        # Incremental 特有 payload 字段
        if "incremental_intent" in initial_payload:
            state["incremental_intent"] = initial_payload["incremental_intent"]
        if "source_artifact_public_id" in initial_payload:
            state["source_artifact_public_id"] = (
                initial_payload["source_artifact_public_id"]
            )
        if "modification_idempotency_key" in initial_payload:
            state["modification_idempotency_key"] = (
                initial_payload["modification_idempotency_key"]
            )
        # 走 incremental subgraph(独立 compile)
        return await self._invoke_incremental_subgraph(state)

    async def _invoke_incremental_subgraph(
        self,
        state: TestPlanGraphState,
    ) -> RunOutcome:
        cfg = await self._build_config(state)
        assert_state_serializable(dict(state))

        from app.agent_runtime.incremental.subgraph import run_incremental_subgraph

        ctx = (cfg.get("configurable") or {}).get("runtime_context")
        if ctx is None:
            raise CoordinatorNotWiredError(
                "incremental runtime_context missing; "
                "LangGraphRunCoordinator requires context_factory for "
                "incremental tasks"
            )
        result_state = await run_incremental_subgraph(
            dict(state), ctx=ctx,
            config=cfg,
            checkpointer=None,
        )
        outcome = self._to_outcome(result_state)
        await self._maybe_write_checkpoint(result_state)
        return outcome

    async def run_post_confirm(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        restored_state: Dict[str, Any],
    ) -> RunOutcome:
        """post-confirm 入口:用户已确认 section。"""
        restored_state.setdefault("current_phase", "paused")
        restored_state.setdefault("pause_marker", "need_user_confirm")
        restored_state.setdefault("task_id", task_id)
        restored_state.setdefault("graph_run_id", graph_run_id)
        # Phase 2.8R-D:graph_version 不可变。若 restored_state 没有,沿用默认。
        restored_state.setdefault("graph_name", GRAPH_NAME_TEST_PLAN)
        if not restored_state.get("graph_version"):
            restored_state["graph_version"] = _resolve_default_graph_version()
        return await self._invoke_and_capture(dict(restored_state))

    async def resume_format_loss(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        restored_state: Dict[str, Any],
    ) -> RunOutcome:
        """format_loss_review 暂停后,提交用户决策 → record_loss_decision。"""
        restored_state.setdefault("current_phase", "paused")
        restored_state.setdefault("pause_marker", "format_loss_review")
        restored_state.setdefault("task_id", task_id)
        restored_state.setdefault("graph_run_id", graph_run_id)
        # Phase 2.8R-D:graph_version 不可变。若 restored_state 没有,沿用默认。
        restored_state.setdefault("graph_name", GRAPH_NAME_TEST_PLAN)
        if not restored_state.get("graph_version"):
            restored_state["graph_version"] = _resolve_default_graph_version()
        return await self._invoke_and_capture(dict(restored_state))

    # ── Interrupt 路径(Phase 2.2) ────────────────────────────────────────

    async def run_pre_confirm_interrupted(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        initial_payload: Dict[str, Any],
    ) -> RunOutcome:
        """interrupt 路径全新任务 — 跑到 ``section_confirmation_interrupt`` 节点
        暂停。

        实现细节:
        * 走 ``compiled.ainvoke`` 直接调 LangGraph;首次 ainvoke 触发
          ``interrupt()`` 后 LangGraph 把任务挂起,``get_state(cfg).tasks[0].interrupts``
          含 interrupt payload。
        * ``pause_marker`` 由 coordinator 合成(便于 caller 判断),
          ``current_node`` 指向触发 interrupt 的节点。
        """
        state = self._initial_state(
            task_id=task_id,
            graph_run_id=graph_run_id,
            payload=initial_payload,
        )
        cfg = await self._build_config(state)
        assert_state_serializable(dict(state))
        compiled = self._compiled_for_state(state)
        # 首次 ainvoke:走到 section_confirmation_interrupt 节点,LangGraph
        # 触发 interrupt() 后挂起,AINVOKE 正常返回(不抛)。
        await compiled.ainvoke(dict(state), config=cfg)
        return await self._capture_interrupted_state(
            compiled=compiled, cfg=cfg, task_id=task_id,
            marker="section_confirmation_interrupt",
        )

    async def _capture_interrupted_state(
        self,
        *,
        compiled: Any,
        cfg: Dict[str, Any],
        task_id: str,
        marker: str,
    ) -> RunOutcome:
        """从 checkpointer 读 pending interrupt 状态并组装 outcome。"""
        snap = await compiled.aget_state(cfg)
        values = dict(getattr(snap, "values", None) or {})
        tasks = list(getattr(snap, "tasks", None) or [])
        next_nodes = list(getattr(snap, "next", None) or ())
        # 防御:没识别到 interrupt → 抛错提醒 caller
        if not tasks or not next_nodes:
            raise ValueError(
                f"_capture_interrupted_state: no pending interrupt for task_id={task_id!r}; "
                f"snap.values keys={list(values.keys())[:5]}, next={next_nodes}"
            )
        # 注入 marker 让 outcome.pause_marker 不为 None
        values["pause_marker"] = marker
        values["current_phase"] = "paused"
        values["task_status"] = values.get("task_status") or "waiting_user_confirm"
        outcome = self._to_outcome(values)
        await self._maybe_write_checkpoint(values)
        return outcome

    async def resume_section_confirmation(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        decision: Dict[str, Any],
    ) -> RunOutcome:
        """章节确认 interrupt 后,通过 ``Command(resume=...)`` 恢复。

        ``decision`` 必须是 dict::

            {
              "kind": "section_confirmation",
              "sections": [...],
              "source": "user" | "timeout",
            }

        由 coordinator 强制校验,防御模型/上游错误调用。
        """
        self._validate_section_decision(decision)
        # Phase 2.8R-D:先拿 checkpoint state,再决定 graph_version → 拿 compiled
        restored_state = await self._load_checkpoint_state_by_thread_id(task_id=task_id)
        compiled = self._compiled_for_state(restored_state)
        cfg = self._build_resume_config(task_id=task_id, restored_state=restored_state)
        if inspect.isawaitable(cfg):
            cfg = await cfg
        assert_state_serializable(restored_state)

        # Phase 2.9A.7: Resume 前置验证 — checkpoint 必须含 pending interrupt。
        # 任何 mismatch → ResumeNoPendingInterruptError,Worker 立即 mark_failed。
        before_snapshot = await compiled.aget_state(cfg)
        self._validate_pending_interrupt_before_resume(
            before_snapshot,
            expected_kind="section_confirmation",
            task_id=task_id,
        )
        before_checkpoint_id = getattr(before_snapshot, "config", None) and getattr(
            before_snapshot.config, "configurable", {}
        ).get("checkpoint_id")
        before_step = (
            int(getattr(before_snapshot.metadata, "step", 0))
            if getattr(before_snapshot, "metadata", None)
            else 0
        )

        logger.info(
            "resume_section_confirmation: before | task_id=%s | "
            "checkpoint_id=%s | step=%s",
            task_id, before_checkpoint_id, before_step,
        )

        result_state = await compiled.ainvoke(Command(resume=decision), config=cfg)
        outcome = self._to_outcome(result_state)

        # Phase 2.9A.7: Resume 后置验证 — checkpoint 必须推进(interrupt 被消费)。
        after_snapshot = await compiled.aget_state(cfg)
        self._validate_progress_after_resume(
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            before_checkpoint_id=before_checkpoint_id,
            before_step=before_step,
            task_id=task_id,
        )

        await self._maybe_write_checkpoint(result_state)
        return outcome

    async def resume_preparation_clarification(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        decision: Dict[str, Any],
    ) -> RunOutcome:
        """Resume the durable post-retrieval clarification interrupt."""
        self._validate_preparation_clarification_decision(decision)
        restored_state = await self._load_checkpoint_state_by_thread_id(task_id=task_id)
        compiled = self._compiled_for_state(restored_state)
        cfg = self._build_resume_config(task_id=task_id, restored_state=restored_state)
        if inspect.isawaitable(cfg):
            cfg = await cfg
        assert_state_serializable(restored_state)
        before_snapshot = await compiled.aget_state(cfg)
        self._validate_pending_interrupt_before_resume(
            before_snapshot,
            expected_kind="preparation_clarification",
            task_id=task_id,
        )
        before_checkpoint_id = (
            getattr(before_snapshot.config, "configurable", {}).get("checkpoint_id")
            if getattr(before_snapshot, "config", None)
            else None
        )
        before_step = (
            int(getattr(before_snapshot.metadata, "step", 0))
            if getattr(before_snapshot, "metadata", None)
            else 0
        )
        result_state = await compiled.ainvoke(Command(resume=decision), config=cfg)
        outcome = self._to_outcome(result_state)
        after_snapshot = await compiled.aget_state(cfg)
        self._validate_progress_after_resume(
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            before_checkpoint_id=before_checkpoint_id,
            before_step=before_step,
            task_id=task_id,
        )
        await self._maybe_write_checkpoint(result_state)
        return outcome

    async def resume_format_loss_interrupt(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        decision: Dict[str, Any],
    ) -> RunOutcome:
        """格式损失 interrupt 后,通过 ``Command(resume=...)`` 恢复。

        ``decision`` 必须是 dict::

            {
              "kind": "format_loss",
              "decision": "accept" | "retry" | "reject",
              "source": "user" | "timeout",
            }
        """
        self._validate_format_loss_decision(decision)
        restored_state = await self._load_checkpoint_state_by_thread_id(task_id=task_id)
        compiled = self._compiled_for_state(restored_state)
        cfg = self._build_resume_config(task_id=task_id, restored_state=restored_state)
        if inspect.isawaitable(cfg):
            cfg = await cfg
        assert_state_serializable(restored_state)

        # Phase 2.9A.7: 同 resume_section_confirmation 的 before/after 验证。
        before_snapshot = await compiled.aget_state(cfg)
        self._validate_pending_interrupt_before_resume(
            before_snapshot,
            expected_kind="format_loss",
            task_id=task_id,
        )
        before_checkpoint_id = (
            getattr(before_snapshot.config, "configurable", {}).get("checkpoint_id")
            if getattr(before_snapshot, "config", None)
            else None
        )
        before_step = (
            int(getattr(before_snapshot.metadata, "step", 0))
            if getattr(before_snapshot, "metadata", None)
            else 0
        )

        result_state = await compiled.ainvoke(Command(resume=decision), config=cfg)
        outcome = self._to_outcome(result_state)

        after_snapshot = await compiled.aget_state(cfg)
        self._validate_progress_after_resume(
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            before_checkpoint_id=before_checkpoint_id,
            before_step=before_step,
            task_id=task_id,
        )

        await self._maybe_write_checkpoint(result_state)
        return outcome

    # ── Thread 恢复(service 重启恢复) ────────────────────────────────────

    async def resume_thread(
        self,
        *,
        task_id: str,
        graph_run_id: str,
    ) -> RunOutcome:
        """服务重启后,从 checkpointer 拿上次 state 重新跑。

        Phase 2.2 范围:此方法仅恢复已 paused(interrupt 触发的暂停,
        checkpointer 已存 task_id 对应的 thread);已 completed / failed /
        cancelled 的 task **不**重新跑(空 invoke,会直接走 entry_router
        拒入)。

        注:LangGraph 在 MemorySaver 模式下,``ainvoke(None)`` 等价
        ``ainvoke(Command(resume=None))``,会从 pending interrupt
        节点恢复 — 但只对最后一次被 interrupt 的 thread 有效。如果
        上次终态是 completed (marker=None),此调用等价无害 pass-through。
        """
        restored_state = await self._load_checkpoint_state_by_thread_id(task_id=task_id)
        compiled = self._compiled_for_state(restored_state)
        # 取上一次持久化的 state;如果不存在 → 抛 ValueError 提醒调用方
        values = await self._load_checkpoint_state(compiled=compiled, task_id=task_id)
        prev_status = str(values.get("task_status") or "")
        if prev_status in ("completed", "failed", "cancelled"):
            # 已结束的 task 不再跑,直接返回持久化终态
            return self._to_outcome(values)
        # paused / running 等 → 用 None 作为 Command.resume 让 graph 从中断点继续
        cfg = self._build_resume_config(task_id=task_id, restored_state=values)
        if inspect.isawaitable(cfg):
            cfg = await cfg
        result_state = await compiled.ainvoke(None, config=cfg)
        outcome = self._to_outcome(result_state)
        await self._maybe_write_checkpoint(result_state)
        return outcome

    # ── Internals ─────────────────────────────────────────────────────────

    def _initial_state(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        payload: Dict[str, Any],
    ) -> TestPlanGraphState:
        flags = get_feature_flags()
        state = make_empty_state(
            task_id=task_id,
            graph_run_id=graph_run_id,
            preparation_agent_enabled=flags.preparation_agent_enabled,
            repair_agent_enabled=flags.repair_agent_enabled,
            # Phase 2.5: incremental 是独立路径(独立 graph_name);
            # _initial_state 注入只是为 schema_version/字段默认值兜底,
            # 真正的 graph 路由由 run_incremental() 强制覆盖 graph_name。
            incremental_agent_enabled=flags.incremental_agent_enabled,
            locked_section_ids=_derive_locked_section_ids(payload),
        )
        # Phase 2.8R-D:graph_version 不可变。优先级:
        #   1. payload 显式传 graph_version(上层已经决定)
        #   2. settings.agent_runtime_default_graph_version(默认 v3)
        state["graph_name"] = payload.get("graph_name") or GRAPH_NAME_TEST_PLAN
        state["graph_version"] = (
            payload.get("graph_version")
            or _resolve_default_graph_version()
        )
        for k in (
            "user_prompt",
            "requirement_file_id",
            "template_file_id",
            "conversation_id",
            "user_id",
            "task_internal_id",
            "conversation_internal_id",
            "user_internal_id",
            "project_id",
            "project_context",
            "goal",
            "target_capability",
            "operation",
            "attachment_refs",
            "plan",
            "observations",
            "analysis_results",
            "awaiting_user",
            "clarification",
            "final_answer",
            "request_understanding_snapshot",
            "retrieval_plan_snapshot",
            "knowledge_mode_snapshot",
            "kb_skip_reason",
            "format_loss_timeout_seconds",
        ):
            if k in payload and payload[k] is not None:
                state[k] = payload[k]
        # Incremental tasks are dispatched from the outbox with their source
        # artifact context restored from task_context_json. Preserve those
        # fields in the graph state; otherwise the subgraph starts with the
        # schema keys but empty values and TestPlanRegenTool cannot target the
        # existing document.
        for k in (
            "requirement_analysis",
            "template_structure",
            "knowledge_search_result",
            "section_suggestions",
            "section_confirm_config",
            "confirmed_sections",
            "test_plan_content",
            "review_result",
            "review_standard",
            "artifact",
            "format_check_result",
            "pending_format_losses",
            "format_loss_confirmation",
        ):
            if k in payload and payload[k] is not None:
                state[k] = payload[k]
        state["current_phase"] = None
        state["pause_marker"] = None
        return state

    async def _invoke_and_capture(self, state: TestPlanGraphState) -> RunOutcome:
        cfg = self._build_config(state)
        if inspect.isawaitable(cfg):
            cfg = await cfg
        assert_state_serializable(dict(state))
        task_id = state.get("task_id") or state.get("task_public_id")
        thread_id = cfg.get("configurable", {}).get("thread_id")
        started = time.monotonic()
        log_event(
            logging.getLogger("testagent.agent"),
            logging.INFO,
            LogEvent.AGENT_GRAPH_STARTED,
            "Agent graph started",
            task_id=task_id,
            thread_id=thread_id,
        )
        logger.info(
            "LangGraphRunCoordinator._invoke_and_capture: 开始 ainvoke | task_id=%s | thread_id=%s",
            state.get("task_id") or state.get("task_public_id"),
            cfg.get("configurable", {}).get("thread_id"),
        )
        try:
            from app.core.observability import start_span
            with start_span(
                "agent.graph",
                {"testagent.task_id": task_id, "testagent.thread_id": thread_id},
            ):
                result_state = await self._runtime.ainvoke(state, config=cfg)
        except Exception as exc:
            log_event(
                logging.getLogger("testagent.agent"),
                logging.ERROR,
                LogEvent.AGENT_GRAPH_FAILED,
                "Agent graph failed",
                task_id=task_id,
                thread_id=thread_id,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                error_type=type(exc).__name__,
            )
            logger.exception(
                "LangGraphRunCoordinator._invoke_and_capture: ainvoke 失败 | "
                "task_id=%s | err=%s",
                state.get("task_id") or state.get("task_public_id"),
                exc,
            )
            raise
        logger.info(
            "LangGraphRunCoordinator._invoke_and_capture: ainvoke 完成 | task_id=%s",
            state.get("task_id") or state.get("task_public_id"),
        )
        log_event(
            logging.getLogger("testagent.agent"),
            logging.INFO,
            LogEvent.AGENT_GRAPH_COMPLETED,
            "Agent graph completed",
            task_id=task_id,
            thread_id=thread_id,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
        )
        outcome = self._to_outcome(result_state)
        await self._maybe_write_checkpoint(result_state)
        # Phase 2.8R-D: sentinel pause (need_user_confirm) → 兜底持久化
        # v3 节点 ``pause_for_legacy_confirm_node`` 已经在 emit 事件前
        # 创建并 commit 了 HumanConfirmation;此处只作为兜底:
        # 若节点层未创建(失败/legacy path/降级),由 coordinator 补上。
        # 内部已有 get_pending_by_task 幂等检查,不会重复创建。
        if outcome.paused and outcome.pause_marker == "need_user_confirm":
            await self._persist_human_confirmation(dict(result_state))
        return outcome

    async def _persist_human_confirmation(self, state: Dict[str, Any]) -> None:
        """sentinel pause 后创建 HumanConfirmation 记录。

        ``pause_for_legacy_confirm_node`` 是同步节点,不能 await DB 操作;
        在此 async coordinator 层统一创建。幂等:已有 pending 记录则跳过。
        """
        from app.db.session import AsyncSessionLocal
        from app.models.human_confirmation import HumanConfirmation
        from app.repositories.confirmation_repository import ConfirmationRepository
        from app.utils.datetime import utcnow
        from app.utils.ids import generate_public_id

        task_internal_id = state.get("task_internal_id")
        if not task_internal_id:
            logger.warning(
                "_persist_human_confirmation: task_internal_id missing, skip"
            )
            return

        sections = state.get("section_suggestions") or {}
        sections_list = (
            sections.get("sections") if isinstance(sections, dict) else sections
        )
        if not isinstance(sections_list, list) or not sections_list:
            logger.warning(
                "_persist_human_confirmation: no sections, skip | task_internal_id=%s",
                task_internal_id,
            )
            return

        try:
            async with AsyncSessionLocal() as session:
                confirm_repo = ConfirmationRepository(session)
                existing = await confirm_repo.get_pending_by_task(task_internal_id)
                if existing is not None:
                    logger.info(
                        "_persist_human_confirmation: pending record exists, skip | "
                        "task_internal_id=%s",
                        task_internal_id,
                    )
                    return

                now = utcnow()
                pending = HumanConfirmation(
                    public_id=generate_public_id("confirmation"),
                    user_id=state.get("user_internal_id"),
                    conversation_id=state.get("conversation_internal_id"),
                    task_id=task_internal_id,
                    confirmation_type="section_generation_config",
                    status="pending",
                    prompt_text="请确认各章节的处理方式",
                    request_json={"sections": sections_list},
                    requested_at=now,
                    created_at=now,
                    updated_at=now,
                )
                await confirm_repo.create(pending)
                await session.commit()
                logger.info(
                    "_persist_human_confirmation: created | task_internal_id=%s | "
                    "confirmation_id=%s | sections=%d",
                    task_internal_id,
                    pending.public_id,
                    len(sections_list),
                )
        except Exception as exc:
            logger.warning(
                "_persist_human_confirmation failed (swallowed) | "
                "task_internal_id=%s | err=%s",
                task_internal_id,
                exc,
            )

    async def _build_config(self, state: TestPlanGraphState) -> Dict[str, Any]:
        # Phase 2.8R-C:严格 thread_id,缺失/空/stub-thread 直接抛错,
        # **不静默 fallback**(对应 docs/35 §3 验收三)。
        from app.agent_runtime.graph_thread_id import require_graph_thread_id
        thread_id = require_graph_thread_id(state, context="langgraph_coordinator")
        cfg: Dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        if self._context_factory is not None:
            ctx = self._context_factory(dict(state))
            # Phase 2.9B.1: 生产 context_factory(ProductionRuntimeContextFactory)是
            # async(需按用户构造 LLMClient);测试可注入 sync factory。兼容两者。
            if inspect.isawaitable(ctx):
                ctx = await ctx
            cfg["configurable"]["runtime_context"] = ctx
        # Phase 2.8D ADR-3 → Phase 2.8R-F 升级:
        # recursion_limit 不再硬编码 10,改由 BusinessBudgets 静态计算
        # (max_legal_observed + 安全余量)。实测 11 路径 max=30,默认 30+6=36。
        # 任何低于 max_legal 的配置会在 validate_recursion_limit 抛错
        # (启动期 fail-fast,不进入运行循环)。
        from app.agent_runtime.business_budgets import (
            get_default_budgets,
            validate_recursion_limit,
        )

        budgets = get_default_budgets()
        cfg["recursion_limit"] = validate_recursion_limit(
            budgets.compute_recursion_limit()
        )
        return cfg

    async def _load_checkpoint_state(self, *, compiled: Any, task_id: str) -> Dict[str, Any]:
        """异步读取 checkpoint,验证 checkpoint 存在。

        Phase 2.9A.6:必须用 ``compiled.aget_state()`` 替代同步 ``get_state()``。
        AsyncPostgresSaver 在 async 上下文中不允许同步调用。
        """
        checkpoint_config = {"configurable": {"thread_id": str(task_id)}}
        snapshot = await compiled.aget_state(checkpoint_config)
        values = getattr(snapshot, "values", None) if snapshot is not None else None
        if not values:
            raise ValueError(
                f"resume_thread: no checkpoint found for thread_id={task_id!r}"
            )
        return dict(values)

    async def _load_checkpoint_state_by_thread_id(self, *, task_id: str) -> Dict[str, Any]:
        """Phase 2.8R-D:从 default compiled 异步读取一次 snapshot,得到 graph_version
        后再取真正的 compiled(graph_version 不可变)。

        Resume 路径 caller 不知道原任务的 graph_version;必须先 peek
        checkpoint 才能正确加载 compiled。

        Phase 2.9A.6:必须用 ``compiled.aget_state()`` 替代同步 ``get_state()``。
        AsyncPostgresSaver 在 async 上下文中不允许同步调用,否则抛出
        ``InvalidStateError: Synchronous calls to AsyncPostgresSaver``。
        """
        default_compiled = self._compiled_for_version(None)
        snapshot = await default_compiled.aget_state(
            {"configurable": {"thread_id": str(task_id)}}
        )
        values = getattr(snapshot, "values", None) if snapshot is not None else None
        if values:
            return dict(values)
        # 没有 snapshot(新任务或 MemorySaver 未持有)→ 走 default
        # graph_version。caller 在 _load_checkpoint_state 里会再次校验。
        return {"graph_version": _resolve_default_graph_version()}

    async def _build_resume_config(
        self,
        *,
        task_id: str,
        restored_state: Dict[str, Any],
    ) -> Dict[str, Any]:
        """interrupt 路径专用:不需要重新塞 runtime_context,因为 ``Command(resume=...)``
        沿用 thread 上一轮的 runtime_context(checkpointer 持久化 nodes 内部状态,
        但 runtime_context 必须每次注入)。

        这里仍然从 context_factory 拿一份,模拟入口层逻辑。
        """
        state = dict(restored_state)
        state.setdefault("task_id", task_id)
        state.setdefault("graph_run_id", f"run-{task_id}")
        return await self._build_config(state)

    def _compiled_for_version(self, version: str | None) -> Any:
        """从 registry 取对应 graph_version 的 compiled graph。

        Phase 2.8R-D: 不写死 v2。version=None 时从 default 拿。
        Phase 2.9A.7: ``GraphVersionNotFound`` → ``ResumeGraphVersionNotFoundError``
        (resume 路径不允许 silently fallback)。其它路径保持原行为。
        """
        return self._compiled_for_graph(GRAPH_NAME_TEST_PLAN, version)

    def _compiled_for_state(self, state: Dict[str, Any]) -> Any:
        graph_name = state.get("graph_name") or GRAPH_NAME_TEST_PLAN
        return self._compiled_for_graph(graph_name, state.get("graph_version"))

    def _compiled_for_graph(self, graph_name: str, version: str | None) -> Any:
        version = version or _resolve_default_graph_version()
        try:
            return self._registry.get(graph_name, version)
        except GraphVersionNotFound as exc:
            raise ResumeGraphVersionNotFoundError(
                f"graph {graph_name!r}/{version!r} not registered in registry "
                f"{getattr(self._registry, 'name', '?')!r}; "
                "resume cannot proceed without exact graph match"
            ) from exc

    def _validate_section_decision(self, decision: Dict[str, Any]) -> None:
        if not isinstance(decision, dict):
            raise ValueError(
                f"section resume decision must be dict, got {type(decision).__name__}"
            )
        if decision.get("kind") != "section_confirmation":
            raise ValueError(
                f"section resume kind mismatch: {decision.get('kind')!r}"
            )
        if not isinstance(decision.get("sections"), list):
            raise ValueError("section resume decision.sections must be list")
        src = decision.get("source") or "user"
        if src not in ("user", "timeout"):
            raise ValueError(f"section resume source invalid: {src!r}")

    def _validate_format_loss_decision(self, decision: Dict[str, Any]) -> None:
        if not isinstance(decision, dict):
            raise ValueError(
                f"format_loss resume decision must be dict, got {type(decision).__name__}"
            )
        if decision.get("kind") != "format_loss":
            raise ValueError(
                f"format_loss resume kind mismatch: {decision.get('kind')!r}"
            )
        chosen = decision.get("decision")
        if chosen not in ("accept", "retry", "reject"):
            raise ValueError(
                f"format_loss decision must be accept/retry/reject, got {chosen!r}"
            )
        src = decision.get("source") or "user"
        if src not in ("user", "timeout"):
            raise ValueError(f"format_loss source invalid: {src!r}")

    def _validate_preparation_clarification_decision(self, decision: Dict[str, Any]) -> None:
        if not isinstance(decision, dict):
            raise ValueError("preparation clarification decision must be dict")
        if decision.get("kind") != "preparation_clarification":
            raise ValueError("preparation clarification resume kind mismatch")
        answers = decision.get("answers")
        conservative = decision.get("conservative_gap_ids")
        if not isinstance(answers, dict) or not isinstance(conservative, list):
            raise ValueError("preparation clarification answers and conservative_gap_ids are required")
        if len(answers) > 3 or len(conservative) > 3:
            raise ValueError("preparation clarification response exceeds maximum gap count")
        if any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 2000 for k, v in answers.items()):
            raise ValueError("preparation clarification answers are invalid")
        if any(not isinstance(item, str) or len(item) > 80 for item in conservative):
            raise ValueError("preparation clarification conservative_gap_ids are invalid")
        src = decision.get("source") or "user"
        if src not in ("user", "timeout"):
            raise ValueError(f"preparation clarification source invalid: {src!r}")

    def _to_outcome(self, final_state: Dict[str, Any]) -> RunOutcome:
        """从终态 state 派生 RunOutcome。

        Phase 2.9A.7:
          * 优先判断 ``pending_interrupt`` — LangGraph 走到 interrupt
            节点挂起时,snapshot 会含 tasks[0].interrupts,需识别为
            "pending_interrupt=section_confirmation",而非误判为 completed。
          * 其次 ``pause_marker`` → paused。
          * 否则 completed 仅当 status in completed/failed/cancelled。
        """
        pending_interrupt = self._detect_pending_interrupt(final_state)
        marker = final_state.get("pause_marker")
        status = final_state.get("task_status")

        if pending_interrupt:
            # 走到 interrupt 节点挂起 → pending_interrupt 状态
            paused = False
            completed = False
            # 覆盖 current_node 为触发 interrupt 的节点
            current_node = pending_interrupt
        else:
            paused = bool(marker)
            completed = (
                not paused
                and status in {"completed", "failed", "cancelled"}
            )
            current_node = final_state.get("current_node")

        return RunOutcome(
            completed=completed,
            paused=paused,
            pause_marker=marker,
            task_status=status,
            final_state=dict(final_state),
            current_node=current_node,
            pending_interrupt=pending_interrupt,
        )

    def _detect_pending_interrupt(
        self, final_state: Dict[str, Any]
    ) -> Optional[str]:
        """检测 state 中是否含 LangGraph 待解挂的 interrupt。

        Phase 2.9A.7: 终态 dict 可能含 ``__interrupt__``(LangGraph 注入)
        或者 ``pending_interrupts``;后者是 coordinator 之前自己设的字段。
        返回:
          * ``"section_confirmation"`` — 章节确认挂起
          * ``"format_loss"`` — 格式损失挂起
          * ``None`` — 无挂起 interrupt
        """
        # LangGraph 注入的 __interrupt__ 字段(列表形式,每项含 value.id / value.value)
        raw = final_state.get("__interrupt__")
        if isinstance(raw, (list, tuple)) and raw:
            first = raw[0]
            # Interrupt 对象有 .value 属性;或在 dict 内
            value = getattr(first, "value", None)
            if value is None and isinstance(first, dict):
                value = first
            kind = None
            if isinstance(value, dict):
                kind = value.get("kind")
            elif value is not None:
                kind = getattr(value, "kind", None)
            if kind in ("section_confirmation", "format_loss", "preparation_clarification"):
                return kind
        return None

    def _validate_pending_interrupt_before_resume(
        self,
        snapshot: Any,
        *,
        expected_kind: str,
        task_id: str,
    ) -> None:
        """Resume 前断言 snapshot 含匹配的 pending interrupt。

        Phase 2.9A.7: 防止 Resume silent no-op — 必须显式拒绝在
        无 pending interrupt 时调用 ``Command(resume=...)``。

        Args:
            snapshot: ``compiled.aget_state(config)`` 返回值。
            expected_kind: ``"section_confirmation"`` 或 ``"format_loss"``。
            task_id: 当前 resume 的 task_id(仅用于错误信息)。

        Raises:
            ResumeNoPendingInterruptError: 找不到匹配的 pending interrupt。
        """
        if snapshot is None:
            raise ResumeNoPendingInterruptError(
                f"resume: no checkpoint found for task_id={task_id!r}; "
                "LangGraph snapshot is None"
            )

        # LangGraph StateSnapshot 含 ``tasks`` 字段(每个 task 含 ``interrupts``)
        tasks = list(getattr(snapshot, "tasks", None) or [])
        next_nodes = list(getattr(snapshot, "next", None) or ())

        # 兼容:某些版本 snapshot.values 含 __interrupt__
        values = dict(getattr(snapshot, "values", None) or {})
        if not tasks and "__interrupt__" in values:
            interrupt_list = values["__interrupt__"]
            if isinstance(interrupt_list, (list, tuple)) and interrupt_list:
                # 抽 kind
                first = interrupt_list[0]
                value = getattr(first, "value", None) or (
                    first if isinstance(first, dict) else None
                )
                kind = (
                    value.get("kind") if isinstance(value, dict) else None
                )
                if kind == expected_kind:
                    return
                raise ResumeNoPendingInterruptError(
                    f"resume: expected pending interrupt={expected_kind!r} but got "
                    f"kind={kind!r} for task_id={task_id!r}"
                )

        # 主路径:遍历 tasks 找匹配 kind 的 interrupt
        for task in tasks:
            interrupts = list(getattr(task, "interrupts", None) or [])
            for intr in interrupts:
                # intr 是 Interrupt 对象,含 .value (dict)
                value = getattr(intr, "value", None)
                if value is None and isinstance(intr, dict):
                    value = intr
                kind = (
                    value.get("kind") if isinstance(value, dict) else None
                )
                if kind == expected_kind:
                    return

        # 任何路径都没找到匹配 → 拒绝
        raise ResumeNoPendingInterruptError(
            f"resume: no pending interrupt of kind={expected_kind!r} for "
            f"task_id={task_id!r}; tasks={len(tasks)}, next={next_nodes}, "
            "graph_version_or_thread_id mismatch likely. "
            "Refusing to silently no-op — caller must investigate "
            "pre-confirm path or checkpointer state."
        )

    def _validate_progress_after_resume(
        self,
        *,
        before_snapshot: Any,
        after_snapshot: Any,
        before_checkpoint_id: Optional[str],
        before_step: int,
        task_id: str,
    ) -> None:
        """Resume 后断言 checkpoint 真的推进了。

        Phase 2.9A.7: 防止 "Resume silent no-op" — ``Command(resume=...)``
        表面上成功返回,但 checkpoint 没变、interrupt 没被消费、graph 没
        推进到下一节点。Worker 看到 dispatched 就标成功,前端永远停在
        "已确认章节策略"。

        至少一项成立:
          1. ``checkpoint_id`` 变化;
          2. ``metadata.step`` 增加;
          3. pending interrupt 被消费(after tasks[].interrupts 为空);
          4. ``next`` 节点发生变化(从等待节点转到下游);
          5. ``current_node`` 变化到下游;
          6. ``task_status`` 变化(从 paused/waiting → generating/completed 等);
          7. ``pause_marker`` 从非 None 变 None(走完 interrupt 进入 post_confirm)。

        Args:
            before_snapshot: Resume 前的 StateSnapshot。
            after_snapshot: Resume 后的 StateSnapshot。
            before_checkpoint_id: Resume 前 checkpoint_id(可能 None,InMemorySaver)。
            before_step: Resume 前 step(int)。
            task_id: 当前 task_id(仅用于错误信息)。

        Raises:
            ResumeNoProgressError: 任何进度指标都没变化。
        """
        if after_snapshot is None:
            raise ResumeNoProgressError(
                f"resume: after_snapshot is None for task_id={task_id!r}; "
                "checkpoint may have been invalidated"
            )

        after_checkpoint_id = (
            getattr(after_snapshot.config, "configurable", {}).get("checkpoint_id")
            if getattr(after_snapshot, "config", None)
            else None
        )
        after_step = (
            int(getattr(after_snapshot.metadata, "step", 0))
            if getattr(after_snapshot, "metadata", None)
            else 0
        )

        # 条件 1+2: checkpoint_id 变 或 step 增加
        # InMemorySaver 不写 config / metadata,所以这两个值都可能是 None / 0;
        # 这里只在两边都非 None 时才比对,InMemorySaver 场景下条件 1+2 不参与判断。
        if (
            before_checkpoint_id is not None
            and after_checkpoint_id is not None
            and after_checkpoint_id != before_checkpoint_id
        ):
            return
        if before_step > 0 and after_step > before_step:
            return

        # 条件 3: pending interrupt 被消费
        after_tasks = list(getattr(after_snapshot, "tasks", None) or [])
        after_has_pending_interrupt = False
        for task in after_tasks:
            interrupts = list(getattr(task, "interrupts", None) or [])
            if interrupts:
                after_has_pending_interrupt = True
                break
        before_tasks = list(getattr(before_snapshot, "tasks", None) or [])
        before_has_pending_interrupt = False
        for task in before_tasks:
            interrupts = list(getattr(task, "interrupts", None) or [])
            if interrupts:
                before_has_pending_interrupt = True
                break
        if before_has_pending_interrupt and not after_has_pending_interrupt:
            return

        # 条件 4: next 节点发生变化
        before_next = list(getattr(before_snapshot, "next", None) or ())
        after_next = list(getattr(after_snapshot, "next", None) or ())
        if before_next != after_next:
            return

        # 条件 5: current_node 变化
        before_values = dict(getattr(before_snapshot, "values", None) or {})
        after_values = dict(getattr(after_snapshot, "values", None) or {})
        before_cur = before_values.get("current_node")
        after_cur = after_values.get("current_node")
        if (
            before_cur
            and after_cur
            and before_cur != after_cur
            and after_cur not in (None, "section_confirmation_interrupt", "format_loss_interrupt")
        ):
            return

        # 条件 6: task_status 变化
        before_status = before_values.get("task_status")
        after_status = after_values.get("task_status")
        if (
            before_status
            and after_status
            and before_status != after_status
            and after_status
            in {"generating", "completed", "exporting", "reviewing"}
        ):
            return

        # 条件 7: pause_marker 从非 None 变 None(interrupt 走完 → post_confirm)
        before_marker = before_values.get("pause_marker")
        after_marker = after_values.get("pause_marker")
        if before_marker and after_marker is None:
            return

        # 条件 8: 决策字段被写入(``section_confirm_config`` / ``format_loss_confirmation``)
        before_section_cfg = before_values.get("section_confirm_config")
        after_section_cfg = after_values.get("section_confirm_config")
        if (
            before_section_cfg is None
            and after_section_cfg is not None
            and isinstance(after_section_cfg, dict)
            and after_section_cfg.get("sections") is not None
        ):
            return

        before_fmt_loss = before_values.get("format_loss_confirmation")
        after_fmt_loss = after_values.get("format_loss_confirmation")
        if (
            before_fmt_loss is None
            and after_fmt_loss is not None
            and isinstance(after_fmt_loss, dict)
            and after_fmt_loss.get("decision") in {"retry", "accept", "reject"}
        ):
            return

        # Some in-memory checkpointers omit checkpoint_id/step and a resumed
        # task may temporarily return to the same waiting node.  The durable
        # clarification response itself is still a real, auditable transition.
        before_clarification = before_values.get("clarification_answers")
        after_clarification = after_values.get("clarification_answers")
        if before_clarification is None and isinstance(after_clarification, dict):
            response_map = after_clarification.get("answers")
            conservative_ids = after_clarification.get("conservative_gap_ids")
            if (
                isinstance(response_map, dict)
                and any(str(value).strip() for value in response_map.values())
            ) or (isinstance(conservative_ids, list) and bool(conservative_ids)):
                return

        # 全部未变化 → ResumeNoProgressError
        raise ResumeNoProgressError(
            f"resume: no checkpoint progress detected for task_id={task_id!r}; "
            f"before_checkpoint_id={before_checkpoint_id}, "
            f"after_checkpoint_id={after_checkpoint_id}, "
            f"before_step={before_step}, after_step={after_step}, "
            f"before_next={before_next}, after_next={after_next}, "
            f"before_current_node={before_cur}, after_current_node={after_cur}. "
            "Refusing to silently no-op — checkpointer / graph_version / "
            "interrupt_node wiring likely broken."
        )

    async def _maybe_write_checkpoint(self, final_state: Dict[str, Any]) -> None:
        if self._write_checkpoint is None:
            return
        # Phase 2.8D ADR-9:让出事件循环,确保 LangGraph 自动写先 commit,CheckpointWriter 后写入
        # (LangGraph 默认同步写 checkpointer.put,但 Postgres 路径需要 SQLAlchemy 异步 session;
        # 若时序错位,API 任务投影会拿到旧 current_node 但 LangGraph checkpoint 已是新 node)
        await asyncio.sleep(0)
        try:
            await self._write_checkpoint(
                str(final_state.get("task_id") or ""),
                str(final_state.get("current_node") or ""),
                final_state.get("pause_marker"),
                final_state.get("task_status"),
            )
        except Exception:
            logger.warning(
                "LangGraphRunCoordinator: write_checkpoint hook failed (swallowed)",
                exc_info=True,
            )


__all__ = [
    "LangGraphRunCoordinator",
    "RunOutcome",
    "CoordinatorNotWiredError",
]
