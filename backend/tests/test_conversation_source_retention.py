"""Conversation Context Engine retention and manual-compaction regressions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.context_engine.models.context import ContextRequest, ContextScope, SectionPlan
from app.context_engine.models.enums import ContextKind, SourceType
from app.context_engine.sources.conversation import ConversationSourceAdapter
from app.models.conversation import Conversation
from app.models.message import Message
from app.repositories.base import ensure_model_id
from app.repositories.conversation_summary_repository import ConversationSummaryRepository
from app.services.manual_compaction_service import ManualConversationCompactionService


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _seed_conversation(session, *, public_id: str) -> Conversation:
    conversation = Conversation(
        public_id=public_id,
        user_id=1,
        title="context retention regression",
        context_workspace_key=f"conversation:{public_id}",
        context_memory_mode="inherit",
        created_at=_now(),
        updated_at=_now(),
    )
    await ensure_model_id(session, Conversation, conversation)
    session.add(conversation)
    await session.flush()
    return conversation


async def _seed_messages(
    session,
    conversation_id: int,
    *,
    start_sequence: int,
    count: int,
) -> list[Message]:
    seeded: list[Message] = []
    base = _now()
    for offset in range(count):
        sequence = start_sequence + offset
        role = "user" if sequence % 2 == 1 else "agent"
        message = Message(
            public_id=f"msg_retention_{conversation_id}_{sequence}",
            user_id=1,
            conversation_id=conversation_id,
            role=role,
            message_type="user_text" if role == "user" else "agent_text",
            content=f"retention message {sequence}",
            status="sent",
            conversation_sequence=sequence,
            created_at=base + timedelta(seconds=sequence),
            updated_at=base + timedelta(seconds=sequence),
        )
        await ensure_model_id(session, Message, message)
        session.add(message)
        seeded.append(message)
    await session.flush()
    return seeded


def _request(conversation_id: int) -> ContextRequest:
    return ContextRequest(
        user_id="usr_1",
        conversation_id=str(conversation_id),
        call_site="chat.reply",
    )


def _test_plan_request(conversation_id: int) -> ContextRequest:
    return ContextRequest(
        user_id="usr_1",
        conversation_id=str(conversation_id),
        call_site="test_plan.generate.outline",
        state_ref={
            "conversation_policy": {
                "recent_complete_turns": 6,
                "include_summary": False,
                "protect_recent_turns": True,
            }
        },
    )


def _scope(conversation_id: int) -> ContextScope:
    return ContextScope(
        user_id="usr_1",
        conversation_id=str(conversation_id),
        thread_id=str(conversation_id),
    )


def _section() -> SectionPlan:
    return SectionPlan(
        kind=ContextKind.CONVERSATION,
        required=False,
        budget_tokens=200_000,
    )


@pytest.mark.asyncio
async def test_conversation_source_retains_full_verbatim_history_before_compaction(
    sqlite_session_factory,
):
    """Normal chat keeps its full timeline until the 50% compaction waterline."""
    async with sqlite_session_factory() as session:
        conversation = await _seed_conversation(session, public_id="conv_twenty_turns")
        messages = await _seed_messages(
            session,
            conversation.id,
            start_sequence=1,
            count=44,
        )
        conversation_id = conversation.id
        await session.commit()

    runtime = SimpleNamespace(
        session_factory=sqlite_session_factory,
        user_internal_id=1,
    )
    result = await ConversationSourceAdapter().collect(
        _request(conversation_id),
        _section(),
        _scope(conversation_id),
        runtime_context=runtime,
    )

    raw_messages = [
        item for item in result.items if item.source_type == SourceType.CONVERSATION
    ]
    assert len(raw_messages) == 44
    assert raw_messages[0].source_ref == messages[0].public_id
    assert raw_messages[-1].source_ref == messages[-1].public_id
    assert raw_messages[0].metadata["role"] == "user"
    assert raw_messages[-1].metadata["role"] == "agent"


@pytest.mark.asyncio
async def test_test_plan_conversation_source_keeps_exactly_six_complete_turns(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        conversation = await _seed_conversation(session, public_id="conv_test_plan_six")
        messages = await _seed_messages(
            session,
            conversation.id,
            start_sequence=1,
            count=30,
        )
        conversation_id = conversation.id
        await session.commit()

    result = await ConversationSourceAdapter().collect(
        _test_plan_request(conversation_id),
        _section(),
        _scope(conversation_id),
        runtime_context=SimpleNamespace(
            session_factory=sqlite_session_factory,
            user_internal_id=1,
        ),
    )

    raw_messages = [
        item for item in result.items if item.source_type == SourceType.CONVERSATION
    ]
    assert [item.source_ref for item in raw_messages] == [
        message.public_id for message in messages[-12:]
    ]
    assert all(item.metadata["locked"] is True for item in raw_messages)
    assert not any(item.source_type == SourceType.CONVERSATION_SUMMARY for item in result.items)


@pytest.mark.asyncio
async def test_manual_compaction_summarizes_older_half_and_keeps_ten_turns_raw(
    sqlite_session_factory,
):
    """Manual compaction folds only the prefix and preserves 20 turns verbatim."""
    async with sqlite_session_factory() as session:
        conversation = await _seed_conversation(session, public_id="conv_manual_payload")
        messages = await _seed_messages(
            session,
            conversation.id,
            start_sequence=1,
            count=44,
        )
        conversation_id = conversation.id
        await session.commit()

    observed: dict[str, str] = {}

    class _LLMClient:
        async def generate_with_profile(self, profile, payload):
            observed["profile"] = profile.name
            observed["payload"] = payload
            return SimpleNamespace(
                success=True,
                parsed={
                    "summary_text": "manual compacted summary",
                    "tokens_after": 8,
                },
            )

    async with sqlite_session_factory() as session:
        result = await ManualConversationCompactionService(session).compact(
            conversation_public_id="conv_manual_payload",
            user_internal_id=1,
            session_factory=sqlite_session_factory,
            llm_invoker=object(),
            llm_client=_LLMClient(),
        )

    assert result.run_public_id is not None
    assert result.summary_public_id is not None
    assert result.summary_updated is True
    assert observed["profile"] == "compression.conversation.v1"
    assert messages[0].content in observed["payload"]
    assert messages[3].content in observed["payload"]
    assert messages[4].content not in observed["payload"]
    assert messages[-1].content not in observed["payload"]

    async with sqlite_session_factory() as session:
        summary = await ConversationSummaryRepository(session).get_latest_active_by_type(
            conversation_id,
            1,
            "conversation",
        )
        assert summary is not None
        assert summary.covered_message_start_id == messages[0].id
        assert summary.covered_message_end_id == messages[3].id
        assert summary.message_count == 4
        assert len(summary.source_refs_json or []) == 4

    runtime = SimpleNamespace(
        session_factory=sqlite_session_factory,
        user_internal_id=1,
    )
    adapter = ConversationSourceAdapter()
    immediately_after = await adapter.collect(
        _request(conversation_id),
        _section(),
        _scope(conversation_id),
        runtime_context=runtime,
    )
    raw_immediately_after = [
        item
        for item in immediately_after.items
        if item.source_type == SourceType.CONVERSATION
    ]
    assert [item.source_ref for item in raw_immediately_after] == [
        message.public_id for message in messages[-40:]
    ]

    async with sqlite_session_factory() as session:
        new_messages = await _seed_messages(
            session,
            conversation_id,
            start_sequence=45,
            count=2,
        )
        await session.commit()

    after_new_turn = await adapter.collect(
        _request(conversation_id),
        _section(),
        _scope(conversation_id),
        runtime_context=runtime,
    )
    raw_after_new_turn = [
        item for item in after_new_turn.items if item.source_type == SourceType.CONVERSATION
    ]
    assert [item.source_ref for item in raw_after_new_turn] == [
        message.public_id for message in [*messages[4:], *new_messages]
    ]


@pytest.mark.asyncio
async def test_manual_quick_regrowth_deepens_tail_without_repeating_covered_history(
    sqlite_session_factory,
):
    """A second close manual compaction keeps twelve turns and extends the summary."""
    async with sqlite_session_factory() as session:
        conversation = await _seed_conversation(session, public_id="conv_manual_deep")
        messages = await _seed_messages(
            session,
            conversation.id,
            start_sequence=1,
            count=44,
        )
        conversation_id = conversation.id
        await session.commit()

    observed_payloads: list[str] = []

    class _LLMClient:
        async def generate_with_profile(self, _profile, payload):
            observed_payloads.append(payload)
            return SimpleNamespace(
                success=True,
                parsed={"summary_text": "retention summary", "tokens_after": 8},
            )

    async with sqlite_session_factory() as session:
        service = ManualConversationCompactionService(session)
        first = await service.compact(
            conversation_public_id="conv_manual_deep",
            user_internal_id=1,
            session_factory=sqlite_session_factory,
            llm_invoker=object(),
            llm_client=_LLMClient(),
        )
    assert first.strategy == "light"
    assert first.preserved_complete_turns == 20

    async with sqlite_session_factory() as session:
        later_messages = await _seed_messages(
            session,
            conversation_id,
            start_sequence=45,
            count=12,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        second = await ManualConversationCompactionService(session).compact(
            conversation_public_id="conv_manual_deep",
            user_internal_id=1,
            session_factory=sqlite_session_factory,
            llm_invoker=object(),
            llm_client=_LLMClient(),
        )

    assert second.strategy == "deep"
    assert second.preserved_complete_turns == 12
    assert f"[User]\\n{messages[0].content}\\n\\n" not in observed_payloads[-1]
    assert f"[User]\\n{messages[4].content}\\n\\n" in observed_payloads[-1]

    async with sqlite_session_factory() as session:
        summary = await ConversationSummaryRepository(session).get_latest_active_by_type(
            conversation_id,
            1,
            "conversation",
        )
        assert summary is not None
        assert summary.schema_version == "v2-retention-deep"
        assert summary.covered_message_start_id == messages[0].id
        assert summary.covered_message_end_id == messages[31].id
        assert summary.message_count == 32

    collected = await ConversationSourceAdapter().collect(
        _request(conversation_id),
        _section(),
        _scope(conversation_id),
        runtime_context=SimpleNamespace(
            session_factory=sqlite_session_factory,
            user_internal_id=1,
        ),
    )
    raw_messages = [
        item.source_ref for item in collected.items if item.source_type == SourceType.CONVERSATION
    ]
    assert raw_messages == [message.public_id for message in [*messages[32:], *later_messages]]


@pytest.mark.asyncio
async def test_legacy_over_compaction_still_keeps_twenty_turns_raw(
    sqlite_session_factory,
):
    """Already-created summary cursors at the tail must not blank recent dialogue."""
    async with sqlite_session_factory() as session:
        conversation = await _seed_conversation(session, public_id="conv_legacy_tail")
        messages = await _seed_messages(
            session,
            conversation.id,
            start_sequence=1,
            count=44,
        )
        conversation_id = conversation.id
        await session.commit()

    class _LLMClient:
        async def generate_with_profile(self, _profile, _payload):
            return SimpleNamespace(
                success=True,
                parsed={"summary_text": "legacy tail summary", "tokens_after": 8},
            )

    async with sqlite_session_factory() as session:
        await ManualConversationCompactionService(session).compact(
            conversation_public_id="conv_legacy_tail",
            user_internal_id=1,
            session_factory=sqlite_session_factory,
            llm_invoker=object(),
            llm_client=_LLMClient(),
        )

    async with sqlite_session_factory() as session:
        summary = await ConversationSummaryRepository(session).get_latest_active_by_type(
            conversation_id,
            1,
            "conversation",
        )
        assert summary is not None
        summary.covered_message_end_id = messages[-1].id
        summary.message_count = len(messages)
        await session.commit()

    runtime = SimpleNamespace(
        session_factory=sqlite_session_factory,
        user_internal_id=1,
    )
    collected = await ConversationSourceAdapter().collect(
        _request(conversation_id),
        _section(),
        _scope(conversation_id),
        runtime_context=runtime,
    )
    raw_messages = [
        item for item in collected.items if item.source_type == SourceType.CONVERSATION
    ]
    assert [item.source_ref for item in raw_messages] == [
        message.public_id for message in messages[-40:]
    ]
