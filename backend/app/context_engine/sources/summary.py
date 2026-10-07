"""Conversation Summary Source Adapter：最新 active conversation_summaries。

CE-02 WP-2：仅摘要来源，不整载消息。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.source import ContextWarning, SourceCollectResult
from app.context_engine.sources.registry import ContextSourceAdapterProtocol


class ConversationSummarySourceAdapter:
    """对话摘要来源：latest active conversation summary。

    owner-scope：按 user_id + conversation_id 查询。
    """

    source_kind = ContextKind.CONVERSATION

    def __init__(self, *, token_counter=None) -> None:
        self._token_counter = token_counter

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        started = _now_ms()
        conversation_id = request.conversation_id
        user_id = request.user_id
        if not conversation_id:
            return SourceCollectResult(
                adapter_key="conversation_summary",
                kind=self.source_kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.no_conversation",
                warnings=[
                    ContextWarning(
                        code="context.source.no_conversation",
                        detail="conversation_id 缺失，跳过对话摘要",
                        adapter_key="conversation_summary",
                    )
                ],
                latency_ms=_now_ms() - started,
            )

        items: list[ContextItem] = []
        warnings: list[ContextWarning] = []
        try:
            async with runtime_context.session_factory() as session:
                from app.context_engine.sources._helpers import user_internal_id
                from app.repositories.conversation_summary_repository import ConversationSummaryRepository

                repo = ConversationSummaryRepository(session)
                summary = await repo.get_latest_active_by_type(
                    int(conversation_id), user_internal_id(runtime_context, request), "conversation"
                )
                if summary is not None and summary.summary_text:
                    items.append(
                        ContextItem(
                            item_id=f"summary:{summary.public_id}",
                            kind=ContextKind.CONVERSATION,
                            source_type=SourceType.CONVERSATION_SUMMARY,
                            source_ref=summary.public_id,
                            title="conversation_summary",
                            content=summary.summary_text,
                            authority=80,
                            priority=10,
                            estimated_tokens=self._estimate(summary.summary_text),
                            trust=ContextTrust.BUSINESS_EVIDENCE,
                            metadata={
                                "summary_id": summary.public_id,
                                "summary_type": summary.summary_type,
                                "message_count": summary.message_count,
                            },
                        )
                    )
        except Exception as exc:  # noqa: BLE001 — adapter 捕获后降级
            warnings.append(
                ContextWarning(
                    code="context.source.summary_error",
                    detail="对话摘要收集失败，已降级",
                    adapter_key="conversation_summary",
                )
            )
            return SourceCollectResult(
                adapter_key="conversation_summary",
                kind=self.source_kind,
                items=[],
                warnings=warnings,
                attempted=True,
                degraded=True,
                failure_code="context.source.summary_error",
                latency_ms=_now_ms() - started,
            )

        return SourceCollectResult(
            adapter_key="conversation_summary",
            kind=self.source_kind,
            items=items,
            warnings=warnings,
            attempted=True,
            degraded=False,
            latency_ms=_now_ms() - started,
        )

    def _estimate(self, text: str) -> int:
        if self._token_counter is not None:
            return self._token_counter.estimate(text).tokens
        return max(1, len(text) // 3)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
# auto-appended module-level note: Summary 类型 source adapter。
