"""Unit tests for ConversationSummaryService (F016)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.conversation_summary import ConversationSummary
from app.repositories.conversation_summary_repository import (
    ConversationSummaryRepository,
)
from app.services.conversation_summary_service import ConversationSummaryService
from app.utils.datetime import utcnow


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def mock_llm():
    return MagicMock()


@pytest.fixture
def service(mock_session, mock_llm):
    bridge = SimpleNamespace(
        available=True,
        is_context_engine_bridge=True,
        generate=AsyncMock(return_value=SimpleNamespace(value="summary text")),
    )
    with patch("app.services.conversation_summary_service.ConversationSummaryRepository"), \
         patch("app.services.conversation_summary_service.MessageRepository"):
        return ConversationSummaryService(
            mock_session,
            llm_client=mock_llm,
            context_llm_invoker=bridge,
            session_factory=object(),
        )


class TestShouldUpdateSummary:
    @pytest.mark.asyncio
    async def test_below_min_messages(self, service):
        service._msg_repo.count_by_conversation = AsyncMock(return_value=10)
        assert await service.should_update_summary(100) is False

    @pytest.mark.asyncio
    async def test_no_existing_summary(self, service):
        service._msg_repo.count_by_conversation = AsyncMock(return_value=25)
        service._summary_repo.get_latest_active = AsyncMock(return_value=None)
        assert await service.should_update_summary(100) is True

    @pytest.mark.asyncio
    async def test_summary_stale(self, service):
        service._msg_repo.count_by_conversation = AsyncMock(return_value=30)
        summary = SimpleNamespace(message_count=15)  # 15 < (30 - 10) = 20
        service._summary_repo.get_latest_active = AsyncMock(return_value=summary)
        assert await service.should_update_summary(100) is True

    @pytest.mark.asyncio
    async def test_summary_fresh(self, service):
        service._msg_repo.count_by_conversation = AsyncMock(return_value=25)
        summary = SimpleNamespace(message_count=24)  # 24 >= (25 - 10) = 15
        service._summary_repo.get_latest_active = AsyncMock(return_value=summary)
        assert await service.should_update_summary(100) is False

    @pytest.mark.asyncio
    async def test_db_error_returns_false(self, service):
        service._msg_repo.count_by_conversation = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        assert await service.should_update_summary(100) is False


class TestMaybeUpdateSummary:
    @pytest.mark.asyncio
    async def test_skips_when_not_needed(self, service):
        service.should_update_summary = AsyncMock(return_value=False)
        await service.maybe_update_summary(100, 1)
        service._msg_repo.list_by_conversation.assert_not_called()

    @pytest.mark.asyncio
    async def test_llm_failure_does_not_raise(self, service):
        service.should_update_summary = AsyncMock(return_value=True)
        service._msg_repo.list_by_conversation = AsyncMock(
            return_value=[
                SimpleNamespace(id=1, role="user", content="hi", message_type="user_text"),
                SimpleNamespace(id=2, role="agent", content="hello", message_type="agent_text"),
            ]
        )
        service._context_llm_invoker.generate = AsyncMock(
            side_effect=RuntimeError("llm down")
        )
        # Must not raise
        await service.maybe_update_summary(100, 1)

    @pytest.mark.asyncio
    async def test_saves_summary_on_success(self, service):
        service.should_update_summary = AsyncMock(return_value=True)
        msgs = [
            SimpleNamespace(id=i, role="user" if i % 2 == 0 else "agent",
                          content=f"msg{i}", message_type="user_text" if i % 2 == 0 else "agent_text")
            for i in range(20)
        ]
        service._msg_repo.list_by_conversation = AsyncMock(return_value=msgs)

        service._context_llm_invoker.generate = AsyncMock(
            return_value=SimpleNamespace(value="这是一个摘要")
        )
        prior = SimpleNamespace(id=10, summary_version=3)
        service._summary_repo.get_latest_active_by_type = AsyncMock(
            return_value=prior
        )

        async def create_with_id(summary):
            summary.id = 11
            return summary

        service._summary_repo.create = AsyncMock(side_effect=create_with_id)
        service._summary_repo.supersede_active_by_type = AsyncMock()

        await service.maybe_update_summary(100, 1)
        service._summary_repo.create.assert_called_once()
        created = service._summary_repo.create.await_args.args[0]
        assert created.summary_version == 4
        service._summary_repo.supersede_active_by_type.assert_awaited_once_with(
            100, 1, "conversation", 11
        )

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="Superseded by transcript-grounded summary maintenance coverage.")
    async def test_compaction_bridge_receives_conversation_runtime(self, mock_session, mock_llm):
        bridge = SimpleNamespace(available=True, generate=AsyncMock(
            return_value=SimpleNamespace(value="summary text")
        ))
        resolver = SimpleNamespace(evaluate=MagicMock(return_value=True))
        session_factory = object()
        with patch("app.services.conversation_summary_service.ConversationSummaryRepository"), \
             patch("app.services.conversation_summary_service.MessageRepository"):
            svc = ConversationSummaryService(
                mock_session,
                llm_client=mock_llm,
                context_llm_invoker=bridge,
                task_flag_resolver=resolver,
                session_factory=session_factory,
            )

        text = await svc._generate_summary("user: hello", 7, 100)

        assert text == "summary text"
        kwargs = bridge.generate.await_args.kwargs
        assert kwargs["call_site"] == "compression.conversation"
        assert kwargs["conversation_id"] == 100
        assert kwargs["llm_task_profile"].name == "conversation_summary"
        assert getattr(kwargs["llm_task_profile"].parser, "value", None) == "plain_text"
        assert kwargs["current_goal"] != "user: hello"
        assert "总结当前会话" in kwargs["current_goal"]
        assert kwargs["runtime_context"].conversation_internal_id == 100
        assert kwargs["runtime_context"].session_factory is session_factory

    @pytest.mark.asyncio
    async def test_enabled_compaction_summary_sends_the_real_transcript_to_context_engine(self, mock_session, mock_llm):
        bridge = SimpleNamespace(
            available=True,
            is_context_engine_bridge=True,
            generate=AsyncMock(return_value=SimpleNamespace(value="summary text")),
        )
        resolver = SimpleNamespace(evaluate=MagicMock(return_value=True))
        with patch("app.services.conversation_summary_service.ConversationSummaryRepository"), \
             patch("app.services.conversation_summary_service.MessageRepository"):
            svc = ConversationSummaryService(
                mock_session,
                llm_client=mock_llm,
                context_llm_invoker=bridge,
                task_flag_resolver=resolver,
                session_factory=object(),
            )
        text = await svc._generate_summary("user: hello", 7, 100)

        assert text == "summary text"
        kwargs = bridge.generate.await_args.kwargs
        assert kwargs["call_site"] == "compression.conversation"
        assert kwargs["llm_task_profile"].name == "conversation_summary"
        assert kwargs["user_content"] == "user: hello"

    @pytest.mark.asyncio
    async def test_incremental_summary_merges_prior_summary_with_uncovered_turns(self, service):
        """A decision older than the raw-source window must remain summary input."""
        service.should_update_summary = AsyncMock(return_value=True)
        prior = SimpleNamespace(
            id=10,
            summary_version=2,
            summary_text="acceptance owner: Wang Chen; manual review timing excluded from P0.",
            covered_message_start_id=1,
            covered_message_end_id=20,
            message_count=20,
        )
        service._summary_repo.get_latest_active_by_type = AsyncMock(return_value=prior)
        uncovered = [
            SimpleNamespace(id=21, role="user", content="Continue discussing payment-failure refunds.", message_type="user_text"),
            SimpleNamespace(id=22, role="agent", content="Recorded.", message_type="agent_text"),
        ]
        service._msg_repo.list_after_id = AsyncMock(return_value=uncovered)
        service._msg_repo.list_by_conversation = AsyncMock()
        service._context_llm_invoker.generate = AsyncMock(
            return_value=SimpleNamespace(value="merged summary")
        )

        async def create_with_id(summary):
            summary.id = 11
            return summary

        service._summary_repo.create = AsyncMock(side_effect=create_with_id)
        service._summary_repo.supersede_active_by_type = AsyncMock()

        await service.maybe_update_summary(100, 1)

        service._msg_repo.list_after_id.assert_awaited_once_with(1, 100, 20, limit=50)
        service._msg_repo.list_by_conversation.assert_not_called()
        transcript = service._context_llm_invoker.generate.await_args.kwargs["user_content"]
        assert "acceptance owner: Wang Chen" in transcript
        assert "Continue discussing payment-failure refunds" in transcript
        created = service._summary_repo.create.await_args.args[0]
        assert created.message_count == 22
        assert created.covered_message_start_id == 1
        assert created.covered_message_end_id == 22


class TestGetLatestSummary:
    @pytest.mark.asyncio
    async def test_returns_text(self, service):
        summary = SimpleNamespace(summary_text="摘要内容")
        service._summary_repo.get_latest_active = AsyncMock(return_value=summary)
        assert await service.get_latest_summary(100) == "摘要内容"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_summary(self, service):
        service._summary_repo.get_latest_active = AsyncMock(return_value=None)
        assert await service.get_latest_summary(100) is None

    @pytest.mark.asyncio
    async def test_returns_none_on_error(self, service):
        service._summary_repo.get_latest_active = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        assert await service.get_latest_summary(100) is None


@pytest.mark.asyncio
async def test_replacement_summary_retires_every_prior_active_version(
    sqlite_session_factory,
):
    """The LCT-02 lineage invariant is one active conversation summary."""
    now = utcnow()
    async with sqlite_session_factory() as session:
        session.add_all(
            [
                ConversationSummary(
                    id=101,
                    public_id="summary_old_one",
                    user_id=1,
                    conversation_id=100,
                    summary_text="old one",
                    summary_type="conversation",
                    status="active",
                    created_at=now,
                    updated_at=now,
                ),
                ConversationSummary(
                    id=102,
                    public_id="summary_old_two",
                    user_id=1,
                    conversation_id=100,
                    summary_text="old two",
                    summary_type="conversation",
                    status="active",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        await session.flush()

        replacement = ConversationSummary(
            id=103,
            public_id="summary_replacement",
            user_id=1,
            conversation_id=100,
            summary_text="replacement",
            summary_type="conversation",
            status="active",
            created_at=now,
            updated_at=now,
        )
        repository = ConversationSummaryRepository(session)
        await repository.create(replacement)
        await repository.supersede_active_by_type(100, 1, "conversation", 103)
        await session.commit()

    async with sqlite_session_factory() as session:
        summaries = list(
            (
                await session.execute(
                    select(ConversationSummary)
                    .where(ConversationSummary.conversation_id == 100)
                    .order_by(ConversationSummary.id)
                )
            ).scalars()
        )

    assert [summary.status for summary in summaries] == [
        "superseded",
        "superseded",
        "active",
    ]
    assert [summary.supersedes_summary_id for summary in summaries] == [103, 103, None]
