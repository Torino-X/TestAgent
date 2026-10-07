"""Unit tests for ChatLLMService with F016 ChatContext integration."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.context import (
    ChatContext,
    ChatHistoryMessage,
    FileContextSummary,
    TaskContextSummary,
)
from app.services.chat_llm_service import ChatLLMService


class TestBuildContextContent:
    """Test ChatLLMService._build_context_content static method."""

    def test_basic_sections(self):
        ctx = ChatContext(
            conversation_id="100",
            conversation_summary="之前讨论了需求文档的解析",
            recent_messages=[
                ChatHistoryMessage(role="user", content="帮我生成测试方案"),
                ChatHistoryMessage(role="assistant", content="好的，请上传文件"),
            ],
            file_summaries=[
                FileContextSummary(
                    file_id="f1",
                    file_name="需求文档.docx",
                    file_type="requirement_doc",
                    upload_status="parsed",
                ),
            ],
            latest_task_summary=TaskContextSummary(
                task_id="t1",
                task_type="test_plan_generation",
                status="created",
            ),
        )
        result = ChatLLMService._build_context_content("现在上传好了", ctx)

        assert "【会话摘要】" in result
        assert "之前讨论了需求文档的解析" in result
        assert "【最近对话】" in result
        assert "用户：帮我生成测试方案" in result
        assert "助手：好的，请上传文件" in result
        assert "【当前会话文件】" in result
        assert "需求文档.docx" in result
        assert "【最近任务状态】" in result
        assert "【当前用户问题】" in result
        assert "现在上传好了" in result

    def test_no_summary(self):
        ctx = ChatContext(
            conversation_id="100",
            recent_messages=[],
        )
        result = ChatLLMService._build_context_content("你好", ctx)
        assert "【会话摘要】" not in result
        assert "【最近对话】" not in result
        assert "【当前用户问题】" in result
        assert "你好" in result

    def test_empty_context_just_question(self):
        ctx = ChatContext(conversation_id="100")
        result = ChatLLMService._build_context_content("hello", ctx)
        assert result.strip().startswith("【当前用户问题】")
        assert "hello" in result

    def test_task_with_pending_confirmations(self):
        ctx = ChatContext(
            conversation_id="100",
            latest_task_summary=TaskContextSummary(
                task_id="t1",
                task_type="test_plan_generation",
                status="waiting_user_confirm",
                pending_confirmation_count=3,
            ),
        )
        result = ChatLLMService._build_context_content("继续", ctx)
        assert "待确认：3 个" in result


class TestGenerateReplyWithContext:
    """Test ChatLLMService.generate_reply with chat_context parameter."""

    @pytest.mark.asyncio
    async def test_accepts_chat_context_kwarg(self):
        """generate_reply should accept chat_context without error."""
        mock_llm = AsyncMock()
        mock_llm.generate_with_profile = AsyncMock(
            return_value=SimpleNamespace(success=True, parsed="回复内容")
        )
        svc = ChatLLMService(mock_llm)
        ctx = ChatContext(conversation_id="100")
        result = await svc.generate_reply("你好", chat_context=ctx)
        assert result == "回复内容"

    @pytest.mark.asyncio
    async def test_legacy_path_without_context(self):
        """Without chat_context, falls back to legacy single-turn path."""
        mock_llm = AsyncMock()
        mock_llm.generate_with_profile = AsyncMock(
            return_value=SimpleNamespace(success=True, parsed="legacy reply")
        )
        svc = ChatLLMService(mock_llm)
        result = await svc.generate_reply("hello")
        assert result == "legacy reply"
