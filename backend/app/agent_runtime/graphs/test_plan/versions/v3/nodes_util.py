"""v2 节点工具方法。

每节点函数签名::

    async def node_xxx(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict

LangGraph 把 *state* 作为位置参数,其余关键字参数由 ``RunnableConfig``
中的 ``configurable`` 解析。``get_ctx`` helper 在节点入口从 configurable
取出 ``RuntimeContext`` —— 我们约定它通过 ``"runtime_context"`` key 注入。

本模块是节点函数的"胶水层":
  - ``get_ctx`` 是节点签名适配器(让 async ``(state, *, ctx)`` 业务签名
    能在 LangGraph 的 ``(state, config)`` 入口下被调用)。
  - ``mark_completed`` / ``bump_attempts`` 是 state 片段更新 helper,
    让节点函数能不直接写 list.append + dict.update 那种重复 boilerplate。

测试 stub 在 _stub_context() 注入 NullEventSink + 空 session,
让节点函数在没真实事件订阅 / DB 连接时也能跑通 (测试场景)。
"""

from __future__ import annotations

from typing import Any, Dict

from app.agent_runtime.runtime_context import RuntimeContext


_RUNTIME_CONTEXT_KEY = "runtime_context"


def get_ctx(config: Dict[str, Any] | None) -> RuntimeContext:
    """从 RunnableConfig.configurable 抽取 RuntimeContext。

    测试可以显式传入:config={"configurable": {"runtime_context": ctx}}
    生产协调器``LangGraphRunCoordinator.ainvoke`` 在调图前注入。

    当 graph 被直接 ``dispatcher.dispatch`` 调用而未注入 context 时(测试 stub),
    返回一个最小可用的 ``RuntimeContext`` 让节点仍能跑通。无 session / 无 adapter,
    节点走 fallback 分支(stub)。
    """
    # 防御:空 config / 缺 configurable 都是合法情况(测试或 stub dispatcher 直接调用)。
    if not config:
        return _stub_context()
    configurable = config.get("configurable") or {}
    ctx = configurable.get(_RUNTIME_CONTEXT_KEY)
    if ctx is None:
        # 节点仍想跑通(测试场景或 disabled coordinator),就给它一个空 stub
        # 让节点函数依然能 emit 事件(走 NullEventSink)。
        return _stub_context()
    return ctx


def _stub_context() -> RuntimeContext:
    """最小 stub RuntimeContext —— 没有 session / adapter / 真实 event_sink。

    仅给 dispatcher direct call / Phase 2.0 范围使用。
    """
    from app.agent_runtime.events.sink import NullEventSink
    from datetime import datetime

    class _StubCancel:
        def is_cancelled(self, task_id):  # pragma: no cover - trivial
            return False

    return RuntimeContext(
        user_internal_id=0,
        task_internal_id=0,
        conversation_internal_id=0,
        session_factory=lambda: _null_session(),
        settings_service=None,
        event_sink=NullEventSink(),
        cancellation_service=_StubCancel(),
        clock=datetime.utcnow,
        tool_adapter=None,
    )


class _NullSessionCM:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *args):
        return False


def _null_session():
    return _NullSessionCM()


def mark_completed(state: dict, node_name: str) -> list[str]:
    """Append ``node_name`` to the completed_nodes list, dedup-safe。

    LangGraph state 是只读 partial-update 语义:节点函数返回 dict,
    LangGraph 把它 merge 到 state 上。这里把当前节点的 name 追加到
    completed_nodes,LangGraph merge 后下次节点路由时能看见历史。
    """
    completed = list(state.get("completed_nodes") or [])
    # 不重复添加(同一节点 LangGraph 内部可能因 retry 重复执行,但同一 task 内
    # completed_nodes 应当保持稳定 — 用于审计 / lock_in 之类检查)。
    if node_name not in completed:
        completed.append(node_name)
    return completed


def bump_attempts(state: dict, node_name: str, *, default: int = 1) -> dict[str, int]:
    """Bump ``node_attempts[node_name]`` returning the new map。

    节点重试次数追踪:LangGraph 在 retry 时会重跑节点函数,
    把 attempt 计数写到 state 里供路由/审计使用。
    与 retry_policy 的 attempt 配合使用。
    """
    attempts = dict(state.get("node_attempts") or {})
    # 缺省 default=1 表示每次节点函数被调用就 +1;部分节点可能传 default=2
    # 表示一次节点内含两个子动作。
    attempts[node_name] = int(attempts.get(node_name, 0)) + default
    return attempts


__all__ = ["get_ctx", "mark_completed", "bump_attempts"]
