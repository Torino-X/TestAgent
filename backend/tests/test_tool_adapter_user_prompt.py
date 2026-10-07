"""Regression tests for ToolAdapter user_prompt propagation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_build_proxy_reads_user_prompt_from_graph_state():
    """SectionSuggestionTool must see the original user prompt from graph_state."""
    from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter
    from app.agent_runtime.runtime_context import RuntimeContext

    @asynccontextmanager
    async def _null_session():
        yield None

    adapter = TestAgentToolAdapter(
        tool_executor=MagicMock(),
        event_sink=MagicMock(),
        session_factory=lambda: _null_session(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )

    ctx = MagicMock(spec=RuntimeContext)
    ctx.task_internal_id = 1
    ctx.conversation_internal_id = 1
    ctx.user_internal_id = 1
    ctx.settings_service = None
    ctx._intermediate_state = {}

    graph_state = {
        "user_prompt": "其中的“项目概述”，“测试目标”章节保留原文",
        "template_structure": {"sections": []},
    }

    proxy = adapter._build_proxy(
        ctx_runtime=ctx,
        inputs={},
        session=None,
        graph_state=graph_state,
        tool_name="SectionSuggestionTool",
        attempt=1,
        tool_call_id="tc1",
    )

    assert proxy.user_prompt == "其中的“项目概述”，“测试目标”章节保留原文"

