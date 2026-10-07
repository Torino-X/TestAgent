"""Source Adapter 协议：ContextSourceAdapterProtocol。

CE-02 WP-2：adapter 只做 fetch/ownership/normalize/token-estimate。
``runtime_context`` 提供 session_factory / cancellation / event_sink。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind
from app.context_engine.models.source import SourceCollectResult


@runtime_checkable
class ContextSourceAdapterProtocol(Protocol):
    """Source Adapter 统一契约。

    - ``source_kind``：本 adapter 覆盖的 ContextKind；
    - ``collect``：按 section_plan 收集，返回 SourceCollectResult。
    """

    source_kind: ContextKind

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult: ...
# auto-appended module-level note: source adapter 协议接口。
