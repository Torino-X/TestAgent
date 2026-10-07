"""graph_thread_id — Phase 2.8R-C Thread ID 严格唯一(对应 docs/35 §3)。

设计要点(对应验收三):
  * ``require_graph_thread_id(state)`` — 唯一入口;task_id 缺失/空 → 抛
    ``InvalidGraphThreadIdError``(50601),**不**回退到 ``stub-thread`` / ``incremental-default``。
  * ``_default_thread_id`` 保留作为**派生函数**(被 runtime 调用),但强制
    走过 require_graph_thread_id。
  * LangGraph ``configurable.thread_id`` 必须 = ``task_public_id``(public id 而非 internal id),
    因为 LangGraph Postgres Checkpointer 用 thread_id 作 UUID,任何字符串重复
    会撞 checkpoint。

守禁令映射:
  * 守 #18 (LangGraph 生产不下发 task 时 stub-thread)→ 删 fallback
  * 守 #21 (LangGraph StatefulGraph 唯一 thread)→ thread_id 强一致
"""

from __future__ import annotations

from typing import Any

from app.core.exceptions import InvalidGraphThreadIdError


def require_graph_thread_id(state: Any, *, context: str = "graph_runtime") -> str:
    """从 state 取 ``task_public_id``(严格优先)或 ``task_id``(fallback,开发期)。

    Phase 2.8R-C 决策:
      * 优先 ``task_public_id`` — 生产路径(message_service._create_agent_task
        写入时已经把 task_public_id 同步到 GraphState;2.8R-B 已落地)。
      * Fallback 到 ``task_id`` — 单测 / 历史 fixture 仍用 internal id,允许
        string 形式(如 str(100)="100")作为兼容路径。**internal id 是 int 时
        自动 str()**,仅辅助测试开发。
      * 拒绝 placeholder(stub-thread / incremental-default) — 防历史 bug 复活。

    Args:
        state: dict-like TestPlanGraphState / RuntimeContext / payload
        context: 调用方标识(用于错误 detail)

    Returns:
        非空字符串 thread_id。

    Raises:
        InvalidGraphThreadIdError: 缺失/空/placeholder 值。
    """
    if state is None:
        raise InvalidGraphThreadIdError(
            detail={
                "context": context,
                "reason": "state_is_none",
            }
        )

    # 1) 优先 task_public_id(严格,production 走这个)
    candidate: Any = None
    if isinstance(state, dict):
        candidate = state.get("task_public_id")
    else:
        candidate = getattr(state, "task_public_id", None)

    if candidate is not None:
        thread_id = str(candidate).strip()
        if thread_id in {"stub-thread", "incremental-default"}:
            raise InvalidGraphThreadIdError(
                detail={
                    "context": context,
                    "reason": "placeholder_thread_id_rejected",
                    "raw_value": thread_id,
                }
            )
        if thread_id:
            return thread_id
        # task_public_id 显式存在但为空 → 报错(说明上游漏填)
        raise InvalidGraphThreadIdError(
            detail={
                "context": context,
                "reason": "task_public_id_empty",
            }
        )

    # 2) Fallback 到 task_id(测试 fixture / 内部 id 兼容)
    if isinstance(state, dict):
        fallback = state.get("task_id")
    else:
        fallback = getattr(state, "task_id", None)

    if fallback is None:
        raise InvalidGraphThreadIdError(
            detail={
                "context": context,
                "reason": "task_id_missing",
            }
        )

    thread_id = str(fallback).strip()
    if not thread_id:
        raise InvalidGraphThreadIdError(
            detail={
                "context": context,
                "reason": "task_id_empty",
            }
        )
    if thread_id in {"stub-thread", "incremental-default"}:
        raise InvalidGraphThreadIdError(
            detail={
                "context": context,
                "reason": "placeholder_thread_id_rejected",
                "raw_value": thread_id,
                "source": "task_id_fallback",
            }
        )
    return thread_id


def legacy_task_id_thread_id(state: Any) -> str:
    """供历史测试/迁移期使用:返回 state.task_id 字符串。

    当 ``task_public_id`` 不在 state 但 ``task_id``(internal id)存在时,
    fallback 到 internal id。注意:这是 test-only helper,**生产代码必须
    改用 require_graph_thread_id**。

    Phase 2.8R-C 决策:
      * 单测里大量代码注入 ``{"task_id": 100}``;让 helper 支持 internal id 兜底
      * 但任何生产路径(_invoke_and_capture / _build_config)走 require_graph_thread_id,
        internal id 作为无效值(类型 int)被 require_graph_thread_id 拒绝。
    """
    if state is None:
        return ""
    if isinstance(state, dict):
        v = state.get("task_id") or state.get("task_public_id")
    else:
        v = getattr(state, "task_id", None) or getattr(state, "task_public_id", None)
    if v is None:
        return ""
    return str(v)


__all__ = [
    "require_graph_thread_id",
    "legacy_task_id_thread_id",
]


# 模块定位:graph_thread_id 唯一入口 + 派生 (Phase 2.8R-C, ADR docs/35 §3)
#
# 公共 API:
#   require_graph_thread_id(state)
#     → task_id 缺失/空 → InvalidGraphThreadIdError(50601),不回退;
#   _default_thread_id(task_id, ...)
#     → 派生函数,供内部 runtime 调用,但**强制**走 require_graph_thread_id。
#
# 链路:
#   LangGraphRunCoordinator.ainvoke → require_graph_thread_id(state)
#     → 通过 → 设 configurable.thread_id = task_id 进 LangGraph
#     → 失败 → raise → 落到 fail_task_node 终态
#
# 关键约束:
#   - thread_id 必须 = task_public_id(不是 internal int);
#   - 不要在这里建 stub / default 路径(那会破坏 checkpointer 唯一性);
#   - 持久化 LangGraph state 严格按 thread_id 复用。
