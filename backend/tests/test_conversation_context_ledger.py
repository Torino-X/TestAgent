"""Unit coverage for the canonical conversation working-set ledger."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.conversation_context_ledger_service import (
    ConversationContextLedgerService,
    RECENT_TURNS_AFTER_COMPACTION,
)


def _message(identifier: int, role: str, text: str):
    return SimpleNamespace(
        id=identifier,
        public_id=f"msg_{identifier}",
        role=role,
        message_type="user_text" if role == "user" else "agent_text",
        content=text,
        conversation_sequence=identifier,
    )


def test_ledger_keeps_all_verbatim_messages_before_durable_compaction() -> None:
    messages = [_message(1, "user", "需求一"), _message(2, "agent", "答复一")]

    texts, refs = ConversationContextLedgerService._conversation_working_set(
        messages, summary=None
    )

    assert texts == ["需求一", "答复一"]
    assert [ref["id"] for ref in refs] == ["msg_1", "msg_2"]


def test_ledger_uses_summary_and_retains_twenty_complete_turns_after_compaction() -> None:
    messages = []
    for turn in range(24):
        messages.extend(
            [
                _message(turn * 2 + 1, "user", f"user-{turn}"),
                _message(turn * 2 + 2, "agent", f"agent-{turn}"),
            ]
        )
    summary = SimpleNamespace(
        public_id="sum_1",
        summary_text="older dialogue summary",
        covered_message_end_id=8,
        source_refs_json={"coverage": "durable"},
    )

    texts, refs = ConversationContextLedgerService._conversation_working_set(messages, summary)

    assert texts[0] == "older dialogue summary"
    message_refs = [ref for ref in refs if ref["type"] == "message"]
    # 24 turns, summary covers first 4; the 20-turn raw tail is preserved.
    assert len(message_refs) == RECENT_TURNS_AFTER_COMPACTION * 2
    assert message_refs[0]["id"] == "msg_9"
    assert message_refs[-1]["id"] == "msg_48"


def test_ledger_honours_the_durable_deep_summary_tail() -> None:
    messages = []
    for turn in range(24):
        messages.extend(
            [
                _message(turn * 2 + 1, "user", f"user-{turn}"),
                _message(turn * 2 + 2, "agent", f"agent-{turn}"),
            ]
        )
    summary = SimpleNamespace(
        public_id="sum_deep",
        summary_text="older dialogue summary",
        covered_message_end_id=48,
        source_refs_json={"coverage": "durable"},
        schema_version="v2-retention-deep",
    )

    _texts, refs = ConversationContextLedgerService._conversation_working_set(messages, summary)

    message_refs = [ref for ref in refs if ref["type"] == "message"]
    assert len(message_refs) == 24
    assert message_refs[0]["id"] == "msg_25"
    assert message_refs[-1]["id"] == "msg_48"


def test_task_working_set_keeps_task_summary_but_not_generated_plan_payload() -> None:
    task = SimpleNamespace(
        public_id="task_1",
        title="退款发布评审",
        status="completed",
        current_node="done",
        task_type="test_plan_generation",
        user_instruction="根据需求生成测试方案",
        task_context_json={
            "completion_summary": "已生成方案，待验收",
            "test_plan_content": "MUST NOT be copied into ordinary chat ledger",
        },
    )

    texts, refs = ConversationContextLedgerService._task_working_set(task)

    assert "已生成方案，待验收" in texts[0]
    assert "MUST NOT" not in texts[0]
    assert refs == [{"type": "agent_task", "id": "task_1", "status": "completed"}]


@pytest.mark.asyncio
async def test_existing_ledger_does_not_lock_parent_conversation() -> None:
    """Normal chat updates must not contend with the current message write."""
    service = ConversationContextLedgerService(SimpleNamespace())
    existing = SimpleNamespace(public_id="ctxledger_existing")
    service._load_for_update = AsyncMock(return_value=existing)  # type: ignore[method-assign]
    service._lock_conversation_for_ledger = AsyncMock()  # type: ignore[method-assign]

    result = await service._load_or_lock_for_creation(42, 7)

    assert result is existing
    service._lock_conversation_for_ledger.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_missing_ledger_locks_parent_only_for_creation() -> None:
    service = ConversationContextLedgerService(SimpleNamespace())
    created_by_peer = SimpleNamespace(public_id="ctxledger_created")
    service._load_for_update = AsyncMock(side_effect=[None, created_by_peer])  # type: ignore[method-assign]
    service._lock_conversation_for_ledger = AsyncMock()  # type: ignore[method-assign]

    result = await service._load_or_lock_for_creation(42, 7)

    assert result is created_by_peer
    service._lock_conversation_for_ledger.assert_awaited_once_with(42)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_materialize_persists_a_single_conversation_ledger(sqlite_session_factory) -> None:
    from datetime import datetime

    from sqlalchemy import select

    from app.models.context_engine import ConversationContextLedger
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.repositories.base import ensure_model_id

    async with sqlite_session_factory() as session:
        conversation = Conversation(
            public_id="conv_ledger",
            user_id=1,
            title="ledger",
            context_workspace_key="conversation:conv_ledger",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        await ensure_model_id(session, Conversation, conversation)
        session.add(conversation)
        await session.flush()
        for index, (role, text) in enumerate((("user", "请分析退款风险"), ("agent", "已分析风险")), start=1):
            now = datetime.now()
            message = Message(
                public_id=f"msg_ledger_{index}",
                user_id=1,
                conversation_id=conversation.id,
                role=role,
                message_type="user_text" if role == "user" else "agent_text",
                content=text,
                status="sent",
                conversation_sequence=index,
                created_at=now,
                updated_at=now,
            )
            await ensure_model_id(session, Message, message)
            session.add(message)
        await session.flush()

        materialized = await ConversationContextLedgerService(session).materialize(
            conversation=conversation,
            user_id=1,
            context_window_tokens=200_000,
        )
        await session.commit()

        row = (await session.execute(select(ConversationContextLedger))).scalar_one()
        assert row.public_id == materialized.public_id
        assert row.total_tokens == materialized.total_tokens
        assert row.conversation_history_tokens > 0
        assert row.manifest_json["conversation"]
