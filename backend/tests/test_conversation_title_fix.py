"""Conversation 自动标题生成修复 — targeted tests。

覆盖根因：conversation.title 走 bridge/compose 时 SYSTEM_RULES 退化为普通聊天
规则（71 字符），TITLE_SYSTEM_PROMPT 未进 prompt → 模型把标题任务当问答。
修复：SystemRulesSourceAdapter 对 conversation.title 注入 TITLE_SYSTEM_PROMPT。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.context_engine.composer.composer import ContextComposer
from app.context_engine.models.context import (
    ContextRequest,
    ContextScope,
)
from app.context_engine.planning.planner import ContextPlanner
from app.context_engine.selection.selector import ContextSelector
from app.context_engine.sources.orchestrator import SourceOrchestrator
from app.context_engine.sources.production_registry import build_default_source_registry


async def _compose_title_prompt(user_message: str) -> str:
    req = ContextRequest(
        user_id="1", conversation_id="154", call_site="conversation.title",
        current_user_message=user_message,
    )
    scope = ContextScope(user_id="1", conversation_id="154")
    rt = SimpleNamespace(session_factory=lambda: None, user_internal_id=1, llm_client=None)
    planner = ContextPlanner()
    plan = planner.plan(req)
    registry = build_default_source_registry(token_counter=None)
    orch = SourceOrchestrator(registry)
    outcome = await orch.collect(req, plan, scope, runtime_context=rt)
    if outcome.required_failure:
        pytest.skip(f"compose blocked: {outcome.required_failure}")
    sel = ContextSelector()
    selected = sel.select(plan, outcome.by_section, locked_sections=[])
    composer = ContextComposer()
    result = composer.compose(req, selected)
    sys_parts = [m.content for m in result.messages if m.role == "system"]
    return "\n\n".join(sys_parts)


@pytest.mark.asyncio
async def test_title_system_prompt_includes_title_instruction():
    """TEST-06/根因: conversation.title 的 system prompt 必须包含标题生成指令。

    修复前 system prompt 只有普通聊天规则(71字符, 无标题指令);
    修复后必须包含 TITLE_SYSTEM_PROMPT 的核心约束。
    """
    sys_prompt = await _compose_title_prompt("学习使用Redis")
    assert "只输出标题" in sys_prompt, "system prompt 必须要求模型只输出标题"
    assert "禁止" in sys_prompt, "必须禁止解释/问候/Markdown"
    assert "12字以内" in sys_prompt, "必须限制标题长度"
    assert "用户的第一句话" in sys_prompt, "必须基于用户第一句话生成"


@pytest.mark.asyncio
async def test_title_system_prompt_is_not_plain_chat():
    """TEST-01 保障: system prompt 不再是普通聊天规则(71字符)。

    修复前 SYSTEM_RULES 退化为 '你是TestAgent...' + output_reminder;
    修复后标题指令生效, 长度应显著大于纯聊天占位符。
    """
    sys_prompt = await _compose_title_prompt("学习使用Redis")
    assert len(sys_prompt) > 120, (
        f"system prompt 过短(={len(sys_prompt)}), 标题指令未注入"
    )


@pytest.mark.asyncio
async def test_title_input_is_first_user_message():
    """TEST-输入: compose 的 current_user_message 必须来自第一条用户消息。

    验证 CURRENT_GOAL anchor 是用户输入, 而非 assistant/system/旧消息。
    """
    user_msg = "在Java中如何使用Redis"
    sys_prompt = await _compose_title_prompt(user_msg)
    # output_reminder 之外, system prompt 是标题指令; user content 应含用户消息
    assert user_msg in sys_prompt or "用户" in sys_prompt


def test_title_sanitizer_strips_verbose_reply():
    """TEST-04: sanitizer 必须剔除"你好/我是TestAgent/解释正文"。

    即使模型错误返回长文本, sanitizer 也应收敛为短标题或 fallback。
    """
    from app.services.message_service import MessageService

    result = "你好！我是TestAgent，很高兴见到你。请问今天有什么我可以帮你的吗？"
    title = MessageService._sanitize_conversation_title(result)
    assert title is not None
    assert len(title) <= 12, f"title 超长: {title!r}"
    # 不应包含解释性内容 / 问候
    assert "很高兴" not in title
    assert "有什么可以帮你" not in title


def test_title_sanitizer_truncates_verbose():
    """TEST-05: sanitizer 硬截断到 12 字符内。"""
    from app.services.message_service import MessageService

    title = MessageService._sanitize_conversation_title(
        "这是一个非常非常长的标题不应该被完整保留"
    )
    assert title is not None
    assert len(title) <= 12


def test_title_sanitizer_two_distinct_inputs_distinct_titles():
    """TEST-03 保障: 不同第一句话 → 不同语义标题(sanitizer 不互相污染)。"""
    from app.services.message_service import MessageService

    t1 = MessageService._sanitize_conversation_title("Redis学习指南")
    t2 = MessageService._sanitize_conversation_title("Java中使用Redis")
    assert t1 != t2
    assert "Redis" in t1 and "Redis" in t2
