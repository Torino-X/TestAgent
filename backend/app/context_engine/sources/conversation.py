"""Conversation Source Adapter：最近消息 + 摘要 + 当前消息去重。

CE-02 WP-2：只做 fetch/ownership/normalize/token-estimate。
旧 Tool Result 只给 Ref/Preview（不整载全文）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.context_engine.conversation_retention import (
    EXPECTED_MESSAGES_PER_TURN,
    NORMAL_RECENT_TURNS,
    recent_turn_tail,
    recent_turns_for_summary_schema,
)
from app.context_engine.models.context import ContextItem, ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, ContextTrust, SourceType
from app.context_engine.models.source import ContextWarning, SourceCollectResult
from app.context_engine.sources.registry import ContextSourceAdapterProtocol


class ConversationSourceAdapter:
    """对话来源：最近 user/agent text 消息（排除当前用户消息避免重复）。

    owner-scope：按 user_id + conversation_id 查询；不 dump 全部 State。
    """

    source_kind = ContextKind.CONVERSATION

    def __init__(
        self,
        *,
        recent_turn_limit: int = NORMAL_RECENT_TURNS,
        recent_limit: int | None = None,
        exclude_current: bool = True,
        token_counter=None,
    ) -> None:
        # Repository limits are message rows, while the product contract is
        # expressed in complete user/assistant turns.  Keep ``recent_limit``
        # as a compatibility override for isolated callers.
        self._recent_limit = (
            max(0, int(recent_limit))
            if recent_limit is not None
            else max(0, int(recent_turn_limit)) * EXPECTED_MESSAGES_PER_TURN
        )
        self._exclude_current = exclude_current
        self._token_counter = token_counter

    async def collect(
        self,
        request: ContextRequest,
        section_plan: SectionPlan,
        scope: ContextScope,
        *,
        runtime_context,
    ) -> SourceCollectResult:
        warnings: list[ContextWarning] = []
        started = _now_ms()

        conversation_id = request.conversation_id
        user_id = request.user_id
        if not conversation_id:
            return SourceCollectResult(
                adapter_key="conversation",
                kind=self.source_kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.no_conversation",
                warnings=[
                    ContextWarning(
                        code="context.source.no_conversation",
                        detail="conversation_id 缺失，跳过对话来源",
                        adapter_key="conversation",
                    )
                ],
                latency_ms=_now_ms() - started,
            )

        # scope 校验：conversation 必须匹配 scope
        if scope.conversation_id and str(scope.conversation_id) != str(conversation_id):
            return SourceCollectResult(
                adapter_key="conversation",
                kind=self.source_kind,
                attempted=False,
                degraded=True,
                failure_code="context.source.scope_mismatch",
                warnings=[
                    ContextWarning(
                        code="context.source.scope_mismatch",
                        detail="conversation 与 scope 不匹配",
                        adapter_key="conversation",
                    )
                ],
                latency_ms=_now_ms() - started,
            )

        items: list[ContextItem] = []
        try:
            async with runtime_context.session_factory() as session:
                from app.context_engine.sources._helpers import user_internal_id
                from app.repositories.message_repository import MessageRepository

                repo = MessageRepository(session)
                exclude_id = None
                if self._exclude_current and getattr(request, "current_user_message_id", None):
                    exclude_id = request.current_user_message_id
                from app.repositories.conversation_summary_repository import ConversationSummaryRepository

                summary_repo = ConversationSummaryRepository(session)
                summary = await summary_repo.get_latest_active_by_type(
                    int(conversation_id), user_internal_id(runtime_context, request), "conversation"
                )
                conversation_policy = _conversation_policy(request)
                requested_recent_turns = conversation_policy.get("recent_complete_turns")
                covered_end = getattr(summary, "covered_message_end_id", None)
                has_compaction_manifest = bool(
                    getattr(summary, "source_refs_json", None)
                )
                if isinstance(requested_recent_turns, int) and requested_recent_turns > 0:
                    messages = await repo.list_recent_by_conversation(
                        user_id=user_internal_id(runtime_context, request),
                        conversation_id=int(conversation_id),
                        limit=max(
                            self._recent_limit,
                            requested_recent_turns * EXPECTED_MESSAGES_PER_TURN,
                        ),
                        exclude_id=exclude_id,
                    )
                    messages = recent_turn_tail(
                        messages,
                        keep_turns=requested_recent_turns,
                    )
                elif covered_end and has_compaction_manifest:
                    # The durable summary represents exactly the covered
                    # prefix.  Keep every subsequent raw message until the
                    # next 50% preflight folds its older prefix as well;
                    # fetching only a fixed tail here would silently discard
                    # post-summary turns that have never been summarized.
                    messages = await repo.list_context_messages_by_conversation(
                        user_id=user_internal_id(runtime_context, request),
                        conversation_id=int(conversation_id),
                        exclude_id=exclude_id,
                    )
                    retained_turns = recent_turns_for_summary_schema(
                        getattr(summary, "schema_version", None)
                    )
                    minimum_recent = recent_turn_tail(
                        messages,
                        keep_turns=retained_turns,
                    )
                    minimum_recent_ids = {
                        int(getattr(message, "id", 0) or 0)
                        for message in minimum_recent
                    }
                    messages = [
                        message
                        for message in messages
                        if (
                            int(getattr(message, "id", 0) or 0) > int(covered_end)
                            or int(getattr(message, "id", 0) or 0)
                            in minimum_recent_ids
                        )
                    ]
                else:
                    # Until the proactive 50% waterline is reached we retain
                    # the full verbatim conversation.  This makes available
                    # model context useful instead of silently fixing every
                    # conversation to the last twenty turns.
                    messages = await repo.list_context_messages_by_conversation(
                        user_id=user_internal_id(runtime_context, request),
                        conversation_id=int(conversation_id),
                        exclude_id=exclude_id,
                    )
                if (
                    summary is not None
                    and summary.summary_text
                    and conversation_policy.get("include_summary", True)
                ):
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
                                "covered_start": summary.covered_message_start_id,
                                "covered_end": summary.covered_message_end_id,
                                "compacted_source": has_compaction_manifest,
                                "retained_complete_turns": recent_turns_for_summary_schema(
                                    getattr(summary, "schema_version", None)
                                ),
                            },
                        )
                    )

                for m in messages:
                    item_id = f"msg:{getattr(m, 'public_id', None) or m.id}"
                    items.append(
                        ContextItem(
                            item_id=item_id,
                            kind=ContextKind.CONVERSATION,
                            source_type=SourceType.CONVERSATION,
                            source_ref=getattr(m, "public_id", None) or str(m.id),
                            title="recent_turn",
                            content=m.content or "",
                            authority=60,
                            priority=5,
                            estimated_tokens=self._estimate(m.content or ""),
                            trust=ContextTrust.UNTRUSTED_REFERENCE,
                            freshness_at=m.created_at,
                            metadata={
                                "role": m.role,
                                "message_id": getattr(m, "id", None),
                                "sequence": getattr(m, "conversation_sequence", None),
                                "locked": bool(
                                    conversation_policy.get("protect_recent_turns", False)
                                ),
                                "retention_policy": (
                                    "recent_complete_turns"
                                    if requested_recent_turns
                                    else "normal_recent_turns"
                                ),
                            },
                        )
                    )
        except Exception as exc:  # noqa: BLE001 — adapter 捕获后降级
            warnings.append(
                ContextWarning(
                    code="context.source.conversation_error",
                    detail="对话来源收集失败，已降级",
                    adapter_key="conversation",
                )
            )
            return SourceCollectResult(
                adapter_key="conversation",
                kind=self.source_kind,
                items=[],
                warnings=warnings,
                attempted=True,
                degraded=True,
                failure_code="context.source.conversation_error",
                latency_ms=_now_ms() - started,
            )

        return SourceCollectResult(
            adapter_key="conversation",
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
        # 简单启发式（中文 ~1/1.5 chars）兜底
        return max(1, len(text) // 3)


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _conversation_policy(request: ContextRequest) -> dict:
    state_ref = request.state_ref
    if not isinstance(state_ref, dict):
        return {}
    value = state_ref.get("conversation_policy")
    return value if isinstance(value, dict) else {}
# auto-appended module-level note: Conversation source adapter。
