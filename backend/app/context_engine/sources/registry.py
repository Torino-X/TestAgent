"""SourceAdapterRegistry — 注册表：source_kind → adapter 列表。

CE-02 WP-2：支持同一 ContextKind 多个 adapter（如 conversation + summary
都覆盖 CONVERSATION）。缺失 adapter fail-fast
``context.source.adapter_not_found``。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.context_engine.errors import ContextEngineStage, raise_engine_error
from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind
from app.context_engine.models.source import SourceCollectResult


@runtime_checkable
class ContextSourceAdapterProtocol(Protocol):
    source_kind: ContextKind

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult: ...


class SourceAdapterRegistry:
    """每个 ContextKind 可注册多个 adapter（按注册顺序返回）。"""

    def __init__(self) -> None:
        self._adapters: dict[ContextKind, list[ContextSourceAdapterProtocol]] = {}

    def register(self, adapter: ContextSourceAdapterProtocol) -> None:
        kind = adapter.source_kind
        if kind not in self._adapters:
            self._adapters[kind] = []
        if adapter in self._adapters[kind]:
            raise ValueError(f"SourceAdapter 重复注册: {adapter!r} for {kind}")
        self._adapters[kind].append(adapter)

    def has_kind(self, kind: ContextKind) -> bool:
        return kind in self._adapters and bool(self._adapters[kind])

    def all_for_kind(self, kind: ContextKind) -> list[ContextSourceAdapterProtocol]:
        """返回该 kind 的全部 adapter（按注册顺序）。"""
        return list(self._adapters.get(kind, []))

    def get_for_kind(self, kind: ContextKind) -> ContextSourceAdapterProtocol:
        """kind → 单一 adapter；缺失 fail-fast。

        同一 kind 多 adapter 时，返回第一个（Orchestrator 应使用
        ``all_for_kind`` 显式收集）。
        """
        adapters = self._adapters.get(kind)
        if not adapters:
            raise_engine_error(
                code="context.source.adapter_not_found",
                detail=f"未注册 SourceAdapter: {kind.value}",
                stage=ContextEngineStage.SOURCE,
                retryable=False,
                recoverable=True,
                safe_metadata={"source_kind": kind.value},
            )
        return adapters[0]
# auto-appended module-level note: source 注册表。
