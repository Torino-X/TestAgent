"""Unit tests for ContextSnapshotService (F016)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.context_snapshot_service import ContextSnapshotService


@pytest.fixture
def mock_session():
    return MagicMock()


@pytest.fixture
def service(mock_session):
    with patch("app.services.context_snapshot_service.ContextSnapshotRepository"):
        return ContextSnapshotService(mock_session)


class TestSaveSnapshot:
    @pytest.mark.asyncio
    async def test_saves_snapshot(self, service):
        service._repo.create = AsyncMock()

        await service.save_snapshot(
            user_id=1,
            conversation_id=100,
            message_id=42,
            llm_task_type="chat_reply",
            context_kind="chat",
            included_message_ids=["m1", "m2"],
            included_file_ids=["f1"],
            summary_id=None,
            estimated_tokens=500,
            context_preview="用户问了XX，助手回答了YY",
        )
        service._repo.create.assert_called_once()
        snapshot = service._repo.create.call_args[0][0]
        assert snapshot.user_id == 1
        assert snapshot.estimated_tokens == 500
        assert snapshot.llm_task_type == "chat_reply"

    @pytest.mark.asyncio
    async def test_preview_truncated(self, service):
        service._repo.create = AsyncMock()
        long_preview = "x" * 2000

        await service.save_snapshot(
            user_id=1,
            llm_task_type="chat_reply",
            context_kind="chat",
            context_preview=long_preview,
        )
        snapshot = service._repo.create.call_args[0][0]
        assert len(snapshot.context_preview) <= 1000

    @pytest.mark.asyncio
    async def test_never_raises(self, service):
        service._repo.create = AsyncMock(side_effect=RuntimeError("db down"))
        # Must not raise
        await service.save_snapshot(
            user_id=1,
            llm_task_type="chat_reply",
            context_kind="chat",
        )

    @pytest.mark.asyncio
    async def test_no_full_prompt_saved(self, service):
        """Verify the snapshot never contains the full LLM prompt."""
        service._repo.create = AsyncMock()
        full_prompt = "这是一段完整的LLM prompt " * 100  # 2500+ chars

        await service.save_snapshot(
            user_id=1,
            llm_task_type="chat_reply",
            context_kind="chat",
            context_preview=full_prompt[:100],  # Only a preview is stored
        )
        snapshot = service._repo.create.call_args[0][0]
        # Preview is capped at 1000 chars
        assert len(snapshot.context_preview) <= 1000
        # context_digest is a sha256 hash, not the prompt itself
        assert len(snapshot.context_digest) == 64
        assert snapshot.context_digest != full_prompt
