"""Regression tests for restoring persisted incremental task context."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.services.agent_context_factory import _build_from_task_row


@pytest.mark.asyncio
async def test_build_context_parses_json_text_task_context_for_incremental_tasks():
    task_context = {
        "execution_mode": "incremental_agent",
        "source_artifact_public_id": "art_source",
        "test_plan_content": {"section_package": {"generated_sections": []}},
        "template_structure": {"generation_config": {"ai_fields": []}},
        "review_result": {"level": "failed", "review_issues": []},
    }
    task = SimpleNamespace(
        id=101,
        public_id="task_incremental",
        user_id=7,
        conversation_id=8,
        requirement_file_id=None,
        template_file_id=None,
        user_instruction="扩充项目概述",
        task_context_json=json.dumps(task_context, ensure_ascii=False),
    )

    class _ConversationResult:
        @staticmethod
        def scalar_one_or_none():
            return "conv_incremental"

    class _Session:
        async def execute(self, _statement):
            return _ConversationResult()

    context = await _build_from_task_row(_Session(), task, task.public_id)

    assert context.task_context_json == task_context
    assert context.conversation_id == "conv_incremental"
    assert context.task_context_json["source_artifact_public_id"] == "art_source"
    assert "test_plan_content" in context.task_context_json
