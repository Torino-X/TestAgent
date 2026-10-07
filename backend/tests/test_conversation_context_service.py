"""Unit tests for ConversationContextService (F016).

Tests build_chat_context, build_intent_context, build_task_trigger_context,
and exception degradation using mock repositories.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.conversation_context_service import ConversationContextService


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def service(mock_session):
    with patch("app.services.conversation_context_service.MessageRepository"), \
         patch("app.services.conversation_context_service.FileRepository"), \
         patch("app.services.conversation_context_service.AgentTaskRepository"), \
         patch("app.services.conversation_context_service.ConversationSummaryRepository"), \
         patch("app.services.conversation_context_service.ContextSnapshotRepository"):
        return ConversationContextService(mock_session)


def _make_message(role: str, content: str, mid: int = 1, offset_minutes: int = 0):
    return SimpleNamespace(
        id=mid,
        role=role,
        content=content,
        message_type="user_text" if role == "user" else "agent_text",
        created_at=datetime(2026, 1, 1, 12, offset_minutes, tzinfo=timezone.utc),
    )


class TestBuildContext:
    @pytest.mark.asyncio
    async def test_build_intent_context_degrades_on_exception(self, service):
        """When repo raises, returns empty IntentContext (never propagates)."""
        service._msg_repo.list_recent_by_conversation = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        ctx = await service.build_intent_context(
            user_id=1, conversation_id=100
        )
        assert ctx.conversation_id == "100"
        assert ctx.recent_turns == []

    @pytest.mark.asyncio
    async def test_build_intent_context_with_messages(self, service):
        msgs = [
            _make_message("user", "hello", mid=1, offset_minutes=0),
            _make_message("agent", "hi", mid=2, offset_minutes=1),
        ]
        service._msg_repo.list_recent_by_conversation = AsyncMock(return_value=msgs)
        service._summary_repo.get_latest_active = AsyncMock(return_value=None)
        service._file_repo.list_by_conversation = AsyncMock(return_value=[])
        service._task_repo.list_by_conversation = AsyncMock(return_value=[])

        ctx = await service.build_intent_context(user_id=1, conversation_id=100)
        assert len(ctx.recent_turns) == 2
        # Reducer maps "agent" → "assistant"
        assert ctx.recent_turns[0].role == "user"
        assert ctx.recent_turns[1].role == "assistant"

    @pytest.mark.asyncio
    async def test_build_chat_context_degrades(self, service):
        service._msg_repo.list_recent_by_conversation = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        ctx = await service.build_chat_context(user_id=1, conversation_id=100)
        assert ctx.conversation_id == "100"
        assert ctx.recent_messages == []

    @pytest.mark.asyncio
    async def test_build_task_trigger_context(self, service):
        service._summary_repo.get_latest_active = AsyncMock(return_value=None)
        service._task_repo.list_by_conversation = AsyncMock(return_value=[])

        ctx = await service.build_task_trigger_context(
            user_id=1,
            conversation_id=100,
            trigger_message_id=42,
            user_goal="生成测试方案",
            selected_file_ids=["f1"],
            intent="test_plan_generation",
            route="agent_task",
        )
        assert ctx.user_goal == "生成测试方案"
        assert ctx.intent == "test_plan_generation"
        assert ctx.route == "agent_task"
        assert ctx.selected_file_ids == ["f1"]

    @pytest.mark.asyncio
    async def test_build_file_summaries_excludes_paths(self, service):
        file_obj = SimpleNamespace(
            public_id="f1",
            original_name="需求文档.docx",
            file_type="requirement_doc",
            upload_status="parsed",
            storage_path="/secret/path",
            blob_path="blob://secret",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        service._file_repo.list_by_conversation = AsyncMock(return_value=[file_obj])

        summaries = await service.build_file_summaries(user_id=1, conversation_id=100)
        assert len(summaries) == 1
        s = summaries[0]
        assert s.file_id == "f1"
        assert s.file_name == "需求文档.docx"
        # Verify no path attributes leak through
        assert not hasattr(s, "storage_path")
        assert not hasattr(s, "blob_path")
