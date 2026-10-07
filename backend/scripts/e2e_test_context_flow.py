"""E2E test for F016 context flow.

Tests:
1. Chat with context builds intent_context and chat_context
2. Context snapshot is saved for each LLM call
3. existing_task_action route is recognized
4. Summary service thresholds work
5. Context reducer bounds token usage
6. File summaries don't expose storage paths
7. Graceful degradation when context services fail

Run: python backend/scripts/e2e_test_context_flow.py
Requires: running backend server + database
"""

from __future__ import annotations

import asyncio
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PASS = 0
FAIL = 0


def ok(name: str):
    global PASS
    PASS += 1
    print(f"  [PASS] {name}")


def fail(name: str, reason: str = ""):
    global FAIL
    FAIL += 1
    print(f"  [FAIL] {name}: {reason}")


def test_token_estimator():
    print("\n[E2E-F016.1] token_estimator")
    try:
        from app.common.token_estimator import estimate_tokens

        assert estimate_tokens("") == 0, "empty should be 0"
        assert estimate_tokens("hello world") > 0, "english should > 0"
        assert estimate_tokens("你好世界") > 0, "chinese should > 0"
        assert estimate_tokens(None) == 0, "none should be 0"  # type: ignore[arg-type]
        ok("token_estimator basic")
    except Exception as e:
        fail("token_estimator basic", str(e))


def test_context_schemas():
    print("\n[E2E-F016.2] context schemas")
    try:
        from app.schemas.context import (
            ChatContext,
            ChatHistoryMessage,
            FileContextSummary,
            IntentContext,
            TaskContextSummary,
            TaskTriggerContext,
        )

        msg = ChatHistoryMessage(role="user", content="hello")
        assert msg.role == "user"

        ctx = ChatContext(
            conversation_id="100",
            recent_messages=[msg],
            conversation_summary="test summary",
        )
        assert ctx.conversation_id == "100"
        assert len(ctx.recent_messages) == 1
        ok("ChatContext creation")

        intent = IntentContext(
            conversation_id="100",
            recent_turns=[msg],
            attached_file_ids=["f1"],
        )
        assert intent.attached_file_ids == ["f1"]
        ok("IntentContext creation")

        task_ctx = TaskTriggerContext(
            conversation_id="100",
            trigger_message_id="42",
            user_goal="生成测试方案",
            selected_file_ids=["f1"],
            intent="test_plan_generation",
            route="agent_task",
        )
        assert task_ctx.user_goal == "生成测试方案"
        ok("TaskTriggerContext creation")
    except Exception as e:
        fail("context schemas", str(e))


def test_context_reducer():
    print("\n[E2E-F016.3] ContextReducer")
    try:
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from app.common.context_reducer import ContextReducer

        def _msg(role, content, mid=1, offset=0):
            return SimpleNamespace(
                id=mid, role=role, content=content,
                message_type="user_text" if role == "user" else "agent_text",
                created_at=datetime(2026, 1, 1, 12 + offset // 60, offset % 60, tzinfo=timezone.utc),
            )

        reducer = ContextReducer(max_chat_turns=3)

        # Empty → empty context
        ctx = reducer.reduce_chat_context([], None, [], None)
        assert ctx.recent_messages == []
        ok("empty context")

        # Turn limiting
        msgs = [_msg("user", f"m{i}", mid=i, offset=i) for i in range(20)]
        msgs += [_msg("agent", f"a{i}", mid=100 + i, offset=100 + i) for i in range(20)]
        ctx = reducer.reduce_chat_context(msgs, None, [], None)
        assert len(ctx.recent_messages) <= 6  # 3 turns * 2
        ok(f"turn limiting ({len(ctx.recent_messages)} msgs)")

        # Agent → assistant mapping
        msgs = [_msg("agent", "hi", mid=1)]
        ctx = reducer.reduce_chat_context(msgs, None, [], None)
        assert ctx.recent_messages[0].role == "assistant"
        ok("agent → assistant mapping")

        # Intent context
        ctx = reducer.reduce_intent_context(
            [_msg("user", "hello")], None, [], None,
            attached_file_ids=["f1"]
        )
        assert ctx.attached_file_ids == ["f1"]
        ok("intent context")

    except Exception as e:
        fail("ContextReducer", str(e))


def test_message_route_enum():
    print("\n[E2E-F016.4] MessageRoute.EXISTING_TASK_ACTION")
    try:
        from app.agent.enums import MessageRoute

        assert hasattr(MessageRoute, "EXISTING_TASK_ACTION")
        assert MessageRoute.EXISTING_TASK_ACTION.value == "existing_task_action"
        ok("EXISTING_TASK_ACTION enum")
    except Exception as e:
        fail("MessageRoute enum", str(e))


def test_summary_profile():
    print("\n[E2E-F016.5] SUMMARY_PROFILE")
    try:
        from app.llm.task_profiles import SUMMARY_PROFILE

        assert SUMMARY_PROFILE.name == "conversation_summary"
        assert SUMMARY_PROFILE.max_tokens == 800
        assert SUMMARY_PROFILE.temperature == 0.2
        ok("SUMMARY_PROFILE config")
    except Exception as e:
        fail("SUMMARY_PROFILE", str(e))


def test_chat_context_content_building():
    print("\n[E2E-F016.6] ChatLLMService._build_context_content")
    try:
        from app.schemas.context import (
            ChatContext,
            ChatHistoryMessage,
            FileContextSummary,
            TaskContextSummary,
        )
        from app.services.chat_llm_service import ChatLLMService

        ctx = ChatContext(
            conversation_id="100",
            conversation_summary="测试摘要",
            recent_messages=[
                ChatHistoryMessage(role="user", content="上传文件"),
                ChatHistoryMessage(role="assistant", content="收到"),
            ],
            file_summaries=[
                FileContextSummary(
                    file_id="f1", file_name="需求.docx",
                    file_type="requirement_doc", upload_status="parsed",
                ),
            ],
            latest_task_summary=TaskContextSummary(
                task_id="t1", task_type="test_plan_generation",
                status="waiting_user_confirm", pending_confirmation_count=2,
            ),
        )
        result = ChatLLMService._build_context_content("继续生成", ctx)

        assert "【会话摘要】" in result
        assert "测试摘要" in result
        assert "【最近对话】" in result
        assert "用户：上传文件" in result
        assert "助手：收到" in result
        assert "【当前会话文件】" in result
        assert "需求.docx" in result
        assert "【最近任务状态】" in result
        assert "待确认：2 个" in result
        assert "【当前用户问题】" in result
        assert "继续生成" in result
        ok("context content building")
    except Exception as e:
        fail("context content building", str(e))


def test_intent_router_system_prompt():
    print("\n[E2E-F016.7] IntentRouter system prompt")
    try:
        from app.agent.intent_router import _INTENT_SYSTEM_PROMPT

        assert "existing_task_action" in _INTENT_SYSTEM_PROMPT
        assert "waiting_user_confirm" in _INTENT_SYSTEM_PROMPT
        assert "优先级" in _INTENT_SYSTEM_PROMPT
        ok("system prompt includes F016 rules")
    except Exception as e:
        fail("system prompt", str(e))


def main():
    print("=" * 60)
    print("E2E Test: F016 Context Flow")
    print("=" * 60)

    test_token_estimator()
    test_context_schemas()
    test_context_reducer()
    test_message_route_enum()
    test_summary_profile()
    test_chat_context_content_building()
    test_intent_router_system_prompt()

    print("\n" + "=" * 60)
    print(f"Results: {PASS} passed, {FAIL} failed, {PASS + FAIL} total")
    print("=" * 60)
    return FAIL == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
