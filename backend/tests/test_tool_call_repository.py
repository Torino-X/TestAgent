"""Regression tests for durable ToolCall audit writes."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.models.tool_call import ToolCall
from app.repositories.tool_call_repository import ToolCallRepository


class _RecordingSession:
    def __init__(self) -> None:
        self.statement = None
        self.params = None
        self.flushed = False

    async def execute(self, statement, params=None):
        self.statement = statement
        self.params = params

    async def flush(self) -> None:
        self.flushed = True


@pytest.mark.asyncio
async def test_create_initializes_non_nullable_output_truncated_column():
    session = _RecordingSession()
    repo = ToolCallRepository(session)  # type: ignore[arg-type]
    now = datetime(2026, 9, 28, 12, 0, 0)
    tool_call = ToolCall(
        public_id="tc_regression",
        user_id=1,
        conversation_id=2,
        task_id=3,
        tool_name="ResultReviewTool",
        tool_stage="review",
        status="running",
        created_at=now,
        updated_at=now,
    )

    await repo.create(tool_call)

    assert "output_truncated" in str(session.statement)
    assert session.params["otr"] is False
    assert session.flushed is True
