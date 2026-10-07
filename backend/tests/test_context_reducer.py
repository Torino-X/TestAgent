"""Unit tests for ContextReducer (F016)."""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.common.context_reducer import ContextReducer


def _msg(role: str, content: str, *, mid: int = 1, offset_minutes: int = 0):
    """Create a minimal message-like object."""
    return SimpleNamespace(
        id=mid,
        role=role,
        content=content,
        message_type="user_text" if role == "user" else "agent_text",
        created_at=datetime(2026, 1, 1, 12, offset_minutes, tzinfo=timezone.utc),
    )


def _file_summary(file_id: str, file_name: str):
    from app.schemas.context import FileContextSummary
    return FileContextSummary(
        file_id=file_id,
        file_name=file_name,
        file_type="requirement_doc",
        upload_status="parsed",
    )


def _task_summary(status: str = "created"):
    from app.schemas.context import TaskContextSummary
    return TaskContextSummary(
        task_id="t1",
        task_type="test_plan_generation",
        status=status,
        summary_text=f"任务状态：{status}",
    )


class TestReduceChatContext:
    def test_empty_messages(self):
        reducer = ContextReducer()
        ctx = reducer.reduce_chat_context([], None, [], None)
        assert ctx.recent_messages == []
        assert ctx.estimated_tokens == 0

    def test_limits_turns(self):
        reducer = ContextReducer(max_chat_turns=2)
        msgs = [_msg("user", f"msg{i}", mid=i, offset_minutes=i) for i in range(10)]
        msgs += [_msg("agent", f"reply{i}", mid=10 + i, offset_minutes=10 + i) for i in range(10)]
        ctx = reducer.reduce_chat_context(msgs, None, [], None)
        # max_chat_turns=2 → max 4 messages (2 user + 2 agent)
        assert len(ctx.recent_messages) <= 4

    def test_filters_non_user_agent(self):
        reducer = ContextReducer()
        msgs = [
            _msg("user", "hello", mid=1),
            SimpleNamespace(id=2, role="system", content="sys", message_type="system", created_at=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc)),
            _msg("agent", "hi", mid=3, offset_minutes=2),
        ]
        ctx = reducer.reduce_chat_context(msgs, None, [], None)
        assert len(ctx.recent_messages) == 2
        assert all(m.role in ("user", "assistant") for m in ctx.recent_messages)

    def test_agent_mapped_to_assistant(self):
        reducer = ContextReducer()
        msgs = [_msg("agent", "hi", mid=1)]
        ctx = reducer.reduce_chat_context(msgs, None, [], None)
        assert len(ctx.recent_messages) == 1
        assert ctx.recent_messages[0].role == "assistant"

    def test_truncates_long_message(self):
        reducer = ContextReducer(max_single_message_chars=20, max_chat_turns=6, max_chat_tokens=6000)
        long_text = "x" * 200
        msgs = [_msg("user", long_text, mid=1)]
        ctx = reducer.reduce_chat_context(msgs, None, [], None)
        assert len(ctx.recent_messages) == 1
        assert len(ctx.recent_messages[0].content) <= 20 + 20  # truncated + suffix

    def test_summary_truncated(self):
        reducer = ContextReducer(max_summary_chars=10)
        summary = "a" * 100
        ctx = reducer.reduce_chat_context([], summary, [], None)
        assert ctx.conversation_summary is not None
        assert len(ctx.conversation_summary) <= 15  # 10 + "..." suffix

    def test_file_summaries_limit(self):
        reducer = ContextReducer(max_file_summaries=3)
        files = [_file_summary(f"f{i}", f"file{i}.doc") for i in range(10)]
        ctx = reducer.reduce_chat_context([], None, files, None)
        assert len(ctx.file_summaries) == 3

    def test_task_summary_passthrough(self):
        reducer = ContextReducer()
        task = _task_summary("running")
        ctx = reducer.reduce_chat_context([], None, [], task)
        assert ctx.latest_task_summary is not None
        assert ctx.latest_task_summary.status == "running"

    def test_orders_asc(self):
        reducer = ContextReducer()
        msgs = [
            _msg("user", "first", mid=1, offset_minutes=1),
            _msg("user", "second", mid=2, offset_minutes=2),
            _msg("agent", "third", mid=3, offset_minutes=3),
        ]
        ctx = reducer.reduce_chat_context(msgs, None, [], None)
        contents = [m.content for m in ctx.recent_messages]
        assert contents == ["first", "second", "third"]


class TestReduceIntentContext:
    def test_basic_structure(self):
        reducer = ContextReducer()
        ctx = reducer.reduce_intent_context(
            [], None, [], None, attached_file_ids=["f1"]
        )
        assert ctx.attached_file_ids == ["f1"]
        assert ctx.recent_turns == []

    def test_limits_turns(self):
        reducer = ContextReducer(max_intent_turns=1)
        msgs = [
            _msg("user", "a", mid=1),
            _msg("agent", "b", mid=2, offset_minutes=1),
            _msg("user", "c", mid=3, offset_minutes=2),
            _msg("agent", "d", mid=4, offset_minutes=3),
        ]
        ctx = reducer.reduce_intent_context(msgs, None, [], None)
        assert len(ctx.recent_turns) <= 2  # 1 turn = user + agent

    def test_exclude_id(self):
        reducer = ContextReducer()
        msgs = [_msg("user", "hello", mid=5)]
        ctx = reducer.reduce_intent_context(msgs, None, [], None, exclude_id=5)
        assert len(ctx.recent_turns) == 0
