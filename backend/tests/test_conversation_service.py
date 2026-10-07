"""Conversation detail contracts used by historical conversation restore."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.conversation_service import ConversationService


@pytest.mark.asyncio
async def test_detail_includes_latest_task_summary_for_history_restore():
    session = MagicMock()
    conversation = SimpleNamespace(
        id=42,
        public_id="conv_history",
        user_id=7,
        title="Historical task",
        status="active",
        updated_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )
    task = SimpleNamespace(
        public_id="task_history",
        task_type="test_plan_generation",
        status="completed",
    )

    with patch("app.services.conversation_service.ConversationRepository"), patch(
        "app.services.conversation_service.AgentTaskRepository"
    ), patch("app.services.conversation_service.MessageRepository"), patch(
        "app.services.conversation_service.FileRepository"
    ):
        service = ConversationService(session)

    service._repo.get_by_public_id = AsyncMock(return_value=conversation)
    service._task_repo.get_latest_by_conversation = AsyncMock(return_value=task)
    service._msg_repo.count_by_conversation = AsyncMock(return_value=3)
    service._file_repo.count_by_conversation = AsyncMock(return_value=1)

    detail = await service.get_detail("conv_history", 7)

    assert detail["latest_task"] == {
        "task_id": "task_history",
        "task_type": "test_plan_generation",
        "status": "completed",
        "events_url": "/api/agent/tasks/task_history/events",
    }
    assert detail["message_count"] == 3
    assert detail["file_count"] == 1


@pytest.mark.asyncio
async def test_list_conversations_includes_message_count_for_empty_chat_reuse():
    session = MagicMock()
    empty_conversation = SimpleNamespace(
        id=41,
        public_id="conv_empty",
        user_id=7,
        title="New chat",
        status="active",
        updated_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    busy_conversation = SimpleNamespace(
        id=42,
        public_id="conv_busy",
        user_id=7,
        title="Greeting",
        status="active",
        updated_at=datetime(2026, 8, 1, 0, 5, tzinfo=timezone.utc),
    )

    with patch("app.services.conversation_service.ConversationRepository"), patch(
        "app.services.conversation_service.AgentTaskRepository"
    ), patch("app.services.conversation_service.MessageRepository"), patch(
        "app.services.conversation_service.FileRepository"
    ):
        service = ConversationService(session)

    service._repo.list_by_user = AsyncMock(return_value=[empty_conversation, busy_conversation])
    service._repo.count_by_user = AsyncMock(return_value=2)
    # Service code calls count_group_by_conversation(user_id, conv_ids) → dict[conv_id, count]
    service._msg_repo.count_group_by_conversation = AsyncMock(
        return_value={41: 0, 42: 2}
    )
    service._file_repo.count_group_by_conversation = AsyncMock(
        return_value={41: 0, 42: 1}
    )

    conversations, total = await service.list_conversations(7)

    assert total == 2
    assert [(item["id"], item["message_count"], item["file_count"]) for item in conversations] == [
        ("conv_empty", 0, 0),
        ("conv_busy", 2, 1),
    ]


@pytest.mark.asyncio
async def test_list_conversations_includes_project_identity_for_sidebar():
    session = MagicMock()
    project_conversation = SimpleNamespace(
        id=43,
        public_id="conv_project",
        user_id=7,
        title="Project chat",
        status="active",
        updated_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
        _project_public_id="prj_alpha",
        _project_name="Alpha Project",
    )

    with patch("app.services.conversation_service.ConversationRepository"), patch(
        "app.services.conversation_service.AgentTaskRepository"
    ), patch("app.services.conversation_service.MessageRepository"), patch(
        "app.services.conversation_service.FileRepository"
    ):
        service = ConversationService(session)

    service._repo.list_by_user = AsyncMock(return_value=[project_conversation])
    service._repo.count_by_user = AsyncMock(return_value=1)
    service._msg_repo.count_group_by_conversation = AsyncMock(return_value={43: 1})
    service._file_repo.count_group_by_conversation = AsyncMock(return_value={43: 0})

    conversations, _ = await service.list_conversations(7)

    assert conversations[0]["project_id"] == "prj_alpha"
    assert conversations[0]["project_name"] == "Alpha Project"
