"""Agent Runtime Context 协议：ContextAwareLLMInvokerProtocol。

CE-02 整改一：Invoker 迁移到 Agent Runtime 层（app/agent_runtime/context/）。
Agent Runtime 可以依赖 ContextEngineProtocol（来自 context_engine/runtime/protocols.py）。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.context_engine.models.context import ContextRequest


@runtime_checkable
class ContextAwareLLMInvokerProtocol(Protocol):
    """ContextAwareLLMInvoker 契约。"""

    async def invoke(
        self,
        *,
        request: ContextRequest,
        llm_task_profile,
        runtime_context,
        retry_policy=None,
    ): ...
