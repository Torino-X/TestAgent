"""LangGraph 适配器 — 把 Phase 2.8A ApiDispatcher 契约映射到真实 ``LangGraphRunCoordinator``。

设计要点(对应 docs/29 §10 + ADR-2.8A-3):

* **Adapter 模式** — ``ApiDispatcher`` 通过
  ``LangGraphCoordinatorProtocol``(``run_pre_confirm(payload)`` /
  ``run_post_confirm(payload)`` / ``resume_section_confirmation(payload)``)
  与 LangGraph 层解耦。本模块提供该契约的**真实实现**,把
  ``payload: Any`` 转成 LangGraph 内部 ``(task_id, graph_run_id, initial_payload)``
  三元组,然后调 ``LangGraphRunCoordinator`` 对应方法。

* **graph_run_id 派生** — Phase 2.8A 范围内 ``graph_run_id`` 没有跨重启
  持久化诉求,默认 ``f"run-{task_public_id}-{uuid4().hex[:8]}"``。Step 12
  持久化 Checkpointer 落地后会换成 ``agent_runs.graph_run_id`` 字段。

* **payload 形态** — ``payload`` 来自 ApiDispatcher.dispatch_new_task 的
  ``context`` 参数,实际是 ``AgentContext`` 实例。Adapter 内部调用
  ``ctx.task_id`` + ``ctx.to_dict()``(若存在)/``vars(ctx)`` 派生
  ``initial_payload``。AgentContext 没有 ``to_dict``,所以用
  ``dataclasses.asdict`` 兼容(遇到不可序列化字段如 SQLAlchemy session
  会被 dataclasses 跳过 — 但不应出现在 initial_payload 中)。

* **resume_* 决策** — payload 必须含 ``decision`` 字段。章节确认使用
  ``{"kind": "section_confirmation", ...}``，格式损失使用
  ``{"kind": "format_loss", "decision": "accept"|"retry"|"reject", ...}``。
  Adapter 强制校验,避免上层错传整个 ctx。

* **Protocol 校验** — Adapter 自身满足 ``LangGraphCoordinatorProtocol`` 契约;
  ApiDispatcher 构造时显式 ``isinstance(coordinator, LangGraphCoordinatorProtocol)``
  跑一遍(开发期 fail-fast)。

守禁令映射:
* 守禁令 #21 → ApiDispatcher 在 dispatch_* 入口统一调 InFlightTaskRegistry;
  Adapter 不感知并发,单一职责。
* 守禁令 #24 → Adapter 内部不感知双闸门(由 ApiDispatcher 决定 engine);
  双闸门关时根本不会到 Adapter。
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


def _coerce_initial_payload(payload: Any) -> dict:
    """把 AgentContext/dataclass/dict 统一转成 dict(initial_payload 必须是 JSON 序列化)。"""
    if isinstance(payload, dict):
        return dict(payload)
    if dataclasses.is_dataclass(payload):
        # asdict 会递归;遇到 session / settings_service 等不可序列化对象会抛 TypeError。
        # Phase 2.8A 约定 initial_payload 只放数据字段,session / sink 走 ctx 参数侧。
        try:
            return dataclasses.asdict(payload)
        except TypeError as exc:
            logger.warning(
                "LangGraphDispatchAdapter: payload=%s 含不可序列化字段,fallback to vars(); err=%s",
                type(payload).__name__, exc,
            )
            # 兜底:逐字段尝试
            out: dict = {}
            for f in dataclasses.fields(payload):
                v = getattr(payload, f.name, None)
                if isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    out[f.name] = v
            return out
    # 未知类型 — 当字符串 / None 处理
    return {"payload": payload}


def _build_graph_run_id(task_public_id: str) -> str:
    """派生 graph_run_id。

    格式:``run-{task_public_id}-{uuid4_short}``。
    Phase 2.8A 范围内不需要持久化(Step 12 接 agent_runs.graph_run_id)。
    """
    suffix = uuid.uuid4().hex[:8]
    return f"run-{task_public_id}-{suffix}"


class LangGraphDispatchAdapter:
    """把 ApiDispatcher Protocol 签名映射到真实 LangGraphRunCoordinator 方法。

    用法:
        coordinator = LangGraphRunCoordinator.from_app_state(app.state)
        adapter = LangGraphDispatchAdapter(coordinator)
        api_dispatcher = ApiDispatcher(
            coordinator=adapter,
            probe_report=probe_report,
        )

    Phase 2.8A Step 11 由 agent_tasks.py / main lifespan 显式构造。
    """

    def __init__(self, coordinator: Any, lifecycle: Any | None = None) -> None:
        # 弱类型 = 真实 coordinator 是 LangGraphRunCoordinator;
        # Phase 2.8A 暂不引入循环 import 风险,直接用 duck typing。
        self._coordinator = coordinator
        self._lifecycle = lifecycle

    async def _dispatch(
        self,
        *,
        task_public_id: str,
        preferred_graph_run_id: str | None,
        operation: Callable[[str], Awaitable[Any]],
    ) -> Any:
        """Run a coordinator operation and mirror its lifecycle when configured."""
        graph_run_id = preferred_graph_run_id or _build_graph_run_id(task_public_id)
        if self._lifecycle is not None:
            prepared = await self._lifecycle.prepare(
                task_public_id,
                preferred_graph_run_id=preferred_graph_run_id,
            )
            graph_run_id = prepared.graph_run_id

        try:
            outcome = await operation(graph_run_id)
        except Exception as exc:
            if self._lifecycle is not None:
                await self._lifecycle.mark_failed(task_public_id, graph_run_id, exc)
            raise

        if self._lifecycle is not None:
            await self._lifecycle.apply_outcome(task_public_id, graph_run_id, outcome)
        return outcome

    # ── ApiDispatcher Protocol 三个方法 ──

    async def run_pre_confirm(self, payload: Any) -> Any:
        """新任务 → coordinator.run_pre_confirm(task_id, graph_run_id, initial_payload)。

        Phase 2.8R-D:payload 可能是 AgentContext(dataclass) 或 dict。
        dict 场景是 dispatch_from_outbox_row 透传:包含 task_id、graph_run_id、
        graph_name、graph_version 等字段。
        """
        ctx = payload
        # 兼容 dict / dataclass 两种 payload 形态
        if isinstance(ctx, dict):
            task_public_id = ctx.get("task_id") or str(payload)
            preferred_gid = ctx.get("graph_run_id")
        else:
            task_public_id = getattr(ctx, "task_id", None) or str(payload)
            preferred_gid = getattr(ctx, "graph_run_id", None)
        initial_payload = _coerce_initial_payload(ctx)
        return await self._dispatch(
            task_public_id=task_public_id,
            preferred_graph_run_id=preferred_gid,
            operation=lambda graph_run_id: self._coordinator.run_pre_confirm(
                task_id=task_public_id,
                graph_run_id=graph_run_id,
                initial_payload=initial_payload,
            ),
        )

    async def run_post_confirm(self, payload: Any) -> Any:
        """post-confirm 入口 → coordinator.run_post_confirm(task_id, graph_run_id, restored_state)。"""
        ctx = payload
        task_public_id = getattr(ctx, "task_id", None) or str(payload)
        restored_state = _coerce_initial_payload(ctx)
        # run_post_confirm 期望 restored_state;Adapter 视 ctx 字段为完整 state 兜底
        return await self._dispatch(
            task_public_id=task_public_id,
            preferred_graph_run_id=getattr(ctx, "graph_run_id", None),
            operation=lambda graph_run_id: self._coordinator.run_post_confirm(
                task_id=task_public_id,
                graph_run_id=graph_run_id,
                restored_state=restored_state,
            ),
        )

    async def resume_section_confirmation(self, payload: Any) -> Any:
        """恢复入口 → coordinator.resume_section_confirmation(task_id, graph_run_id, decision)。

        ``payload`` 必须是 dict 且包含 ``decision`` 字段::

            {
              "kind": "section_confirmation",
              "sections": [...],
              "source": "user" | "timeout",
              "task_id": "<public_id>"  # 必须,用于 task_id 路由
            }
        """
        if not isinstance(payload, dict):
            raise ValueError(
                "LangGraphDispatchAdapter.resume_section_confirmation expects "
                f"dict payload, got {type(payload).__name__}"
            )
        decision = payload.get("decision")
        if not isinstance(decision, dict):
            raise ValueError(
                "LangGraphDispatchAdapter.resume_section_confirmation requires "
                "'decision' dict in payload; missing or wrong type"
            )
        task_public_id = payload.get("task_id") or ""
        if not task_public_id:
            raise ValueError(
                "LangGraphDispatchAdapter.resume_section_confirmation requires "
                "'task_id' in payload"
            )
        return await self._dispatch(
            task_public_id=task_public_id,
            preferred_graph_run_id=payload.get("graph_run_id"),
            operation=lambda graph_run_id: self._coordinator.resume_section_confirmation(
                task_id=task_public_id,
                graph_run_id=graph_run_id,
                decision=decision,
            ),
        )

    async def resume_format_loss_interrupt(self, payload: Any) -> Any:
        """恢复格式损失 interrupt → coordinator.resume_format_loss_interrupt。

        ``payload`` 必须是 dict 且包含 ``task_id`` / ``decision``。真实的
        pending interrupt 校验由 ``LangGraphRunCoordinator`` 完成。
        """
        if not isinstance(payload, dict):
            raise ValueError(
                "LangGraphDispatchAdapter.resume_format_loss_interrupt expects "
                f"dict payload, got {type(payload).__name__}"
            )
        decision = payload.get("decision")
        if not isinstance(decision, dict):
            raise ValueError(
                "LangGraphDispatchAdapter.resume_format_loss_interrupt requires "
                "'decision' dict in payload; missing or wrong type"
            )
        task_public_id = payload.get("task_id") or ""
        if not task_public_id:
            raise ValueError(
                "LangGraphDispatchAdapter.resume_format_loss_interrupt requires "
                "'task_id' in payload"
            )
        return await self._dispatch(
            task_public_id=task_public_id,
            preferred_graph_run_id=payload.get("graph_run_id"),
            operation=lambda graph_run_id: self._coordinator.resume_format_loss_interrupt(
                task_id=task_public_id,
                graph_run_id=graph_run_id,
                decision=decision,
            ),
        )

    # ── Phase 2.8C: 动态 Agent API 入口真实化 ──

    async def resume_preparation_clarification(self, payload: Any) -> Any:
        """Resume the structured post-retrieval clarification interrupt."""
        if not isinstance(payload, dict):
            raise ValueError("preparation clarification payload must be a dict")
        decision = payload.get("decision")
        task_public_id = payload.get("task_id") or ""
        if not isinstance(decision, dict) or not task_public_id:
            raise ValueError("preparation clarification requires task_id and decision")
        return await self._dispatch(
            task_public_id=task_public_id,
            preferred_graph_run_id=payload.get("graph_run_id"),
            operation=lambda graph_run_id: self._coordinator.resume_preparation_clarification(
                task_id=task_public_id,
                graph_run_id=graph_run_id,
                decision=decision,
            ),
        )

    async def run_incremental(self, payload: Any) -> Any:
        """增量任务入口 → coordinator.run_incremental(task_id, graph_run_id, initial_payload)。

        Phase 2.8C:coordinator.run_incremental(Phase 2.5 已有真实实现)
        通过本 Adapter 暴露到 ApiDispatcher 路径。payload 必含
        ``incremental_intent`` + ``source_artifact_public_id`` +
        ``modification_idempotency_key`` 三个字段(由
        ``ApiDispatcher.dispatch_incremental_task`` 强制校验)。

        payload 形态(必须是 dict)::
            {
              "task_id": "<public_id>",
              "incremental_intent": {...},
              "source_artifact_public_id": "...",
              "modification_idempotency_key": "...",
              "user_id": <int>,
              ...其他 initial_payload 字段
            }
        """
        if not isinstance(payload, dict):
            raise ValueError(
                "LangGraphDispatchAdapter.run_incremental expects dict payload, "
                f"got {type(payload).__name__}"
            )
        task_public_id = payload.get("task_id") or ""
        if not task_public_id:
            raise ValueError(
                "LangGraphDispatchAdapter.run_incremental requires 'task_id' in payload"
            )
        initial_payload = dict(payload)
        return await self._dispatch(
            task_public_id=task_public_id,
            preferred_graph_run_id=payload.get("graph_run_id"),
            operation=lambda graph_run_id: self._coordinator.run_incremental(
                task_id=task_public_id,
                graph_run_id=graph_run_id,
                initial_payload=initial_payload,
            ),
        )

    async def run_repair(self, payload: Any) -> Any:
        """Repair Agent 入口 → coordinator.run_repair(task_id, graph_run_id, initial_payload)。

        Phase 2.8C:coordinator.run_repair(Phase 2.4 已有真实实现)
        通过本 Adapter 暴露到 ApiDispatcher 路径。由
        ``nodes_review_format.route_after_review`` 在 review failed 时
        调 ``ApiDispatcher.dispatch_repair_task``(fire-and-forget)。

        payload 形态(必须是 dict)::
            {
              "task_id": "<public_id>",
              ...其他 initial_payload 字段(review_issues / lock context 等)
            }
        """
        if not isinstance(payload, dict):
            raise ValueError(
                "LangGraphDispatchAdapter.run_repair expects dict payload, "
                f"got {type(payload).__name__}"
            )
        task_public_id = payload.get("task_id") or ""
        if not task_public_id:
            raise ValueError(
                "LangGraphDispatchAdapter.run_repair requires 'task_id' in payload"
            )
        initial_payload = dict(payload)
        return await self._dispatch(
            task_public_id=task_public_id,
            preferred_graph_run_id=payload.get("graph_run_id"),
            operation=lambda graph_run_id: self._coordinator.run_repair(
                task_id=task_public_id,
                graph_run_id=graph_run_id,
                initial_payload=initial_payload,
            ),
        )


__all__ = ["LangGraphDispatchAdapter"]
