"""Context Engine 协议：ContextEngineProtocol。

CE-02 整改一：ContextAwareLLMInvokerProtocol 已迁移到
app/agent_runtime/context/protocols.py（Agent Runtime 层）。
本模块只保留 Context Engine Core 契约。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.context_engine.models.compose import ContextComposeResult
from app.context_engine.models.context import ContextRequest
from app.context_engine.models.snapshot_models import ContextSnapshotRef


@runtime_checkable
class ContextEngineProtocol(Protocol):
    """ContextEngine Facade 契约。"""

    async def compose(
        self,
        request: ContextRequest,
        *,
        runtime_context,
        execution_mode: str = "active",
    ) -> ContextComposeResult: ...

    async def compose_for_retry(
        self,
        request: ContextRequest,
        *,
        runtime_context,
        previous_snapshot_ref: ContextSnapshotRef,
    ) -> ContextComposeResult: ...
# auto-appended module-level note: ContextEngine 协议: ContextEngine 的对外接口 (retrieve / select / compose)。
