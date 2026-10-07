"""v2 节点工具方法。

每节点函数签名::

    async def node_xxx(state: TestPlanGraphState, *, ctx: RuntimeContext) -> dict

LangGraph 把 *state* 作为位置参数,其余关键字参数由 ``RunnableConfig``
中的 ``configurable`` 解析。``get_ctx`` helper 在节点入口从 configurable
取出 ``RuntimeContext`` —— 我们约定它通过 ``"runtime_context"`` key 注入。
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
    if not config:
        return _stub_context()
    configurable = config.get("configurable") or {}
    ctx = configurable.get(_RUNTIME_CONTEXT_KEY)
    if ctx is None:
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
    """Append ``node_name`` to the completed_nodes list, dedup-safe."""
    completed = list(state.get("completed_nodes") or [])
    if node_name not in completed:
        completed.append(node_name)
    return completed


def bump_attempts(state: dict, node_name: str, *, default: int = 1) -> dict[str, int]:
    """Bump ``node_attempts[node_name]`` returning the new map."""
    attempts = dict(state.get("node_attempts") or {})
    attempts[node_name] = int(attempts.get(node_name, 0)) + default
    return attempts


__all__ = ["get_ctx", "mark_completed", "bump_attempts"]

