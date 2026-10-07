from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

import pytest

from app.agent_runtime.adapters.test_agent_tool_adapter import TestAgentToolAdapter
from app.agent_runtime.cancellation import InMemoryCancellationService
from app.agent_runtime.events.sink import InMemoryEventSink
from app.agent_runtime.runtime_context import RuntimeContext


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _CapturingExecutor:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def run(self, _tool_name, _inputs, context, _retry_context=None):
        assert context.session is self._session
        return {"success": True, "data": {"ok": True}}


class _ProgressExecutor:
    async def run(self, _tool_name, _inputs, context, _retry_context=None):
        await context.emit_tool_progress(
            "正在处理图片：《系统架构图.png》",
            business_action="解析需求文档",
            business_subject_type="需求文档",
            business_subject_name="智慧校园需求说明书.docx",
        )
        return {"success": True, "data": {"ok": True}}


class _ExplodingExecutor:
    async def run(self, _tool_name, _inputs, _context, _retry_context=None):
        raise RuntimeError("Traceback C:\\secret\\tool.py api_key=abc123")


@pytest.mark.asyncio
async def test_tool_adapter_passes_a_scoped_session_to_legacy_tools() -> None:
    """Legacy tools that persist artifacts receive a live session per invocation."""
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory():
        yield session

    runtime = RuntimeContext(
        user_internal_id=3,
        task_internal_id=2,
        conversation_internal_id=1,
        session_factory=session_factory,
        settings_service=object(),
        event_sink=InMemoryEventSink(),
        cancellation_service=InMemoryCancellationService(),
    )
    adapter = TestAgentToolAdapter(
        tool_executor=_CapturingExecutor(session),
        event_sink=runtime.event_sink,
        session_factory=session_factory,
        clock=datetime.utcnow,
        min_visible_seconds=0,
        transition_seconds=0,
    )

    result = await adapter.execute(
        tool_name="KnowledgeSearchTool",
        inputs={},
        ctx_runtime=runtime,
    )

    assert result["success"] is True
    assert session.commits == 1


@pytest.mark.asyncio
async def test_tool_adapter_emits_a_stable_tool_call_id_from_start_to_finish() -> None:
    """One physical tool invocation must keep a single public event identity."""
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory():
        yield session

    sink = InMemoryEventSink()
    runtime = RuntimeContext(
        user_internal_id=3,
        task_internal_id=2,
        conversation_internal_id=1,
        session_factory=session_factory,
        settings_service=object(),
        event_sink=sink,
        cancellation_service=InMemoryCancellationService(),
    )
    adapter = TestAgentToolAdapter(
        tool_executor=_CapturingExecutor(session),
        event_sink=sink,
        session_factory=session_factory,
        clock=datetime.utcnow,
        min_visible_seconds=0,
        transition_seconds=0,
    )

    await adapter.execute(
        tool_name="KnowledgeSearchTool",
        inputs={},
        ctx_runtime=runtime,
    )

    events = sink.collect()
    assert events[0]["event_type"] == "tool_started"
    started_payload = events[0]["payload"]
    tool_call_id = started_payload["tool_call_id"]
    assert started_payload["tool_name"] == "KnowledgeSearchTool"
    assert tool_call_id

    terminal_events = [event for event in events[1:] if event["event_type"] == "tool_finished"]
    assert terminal_events
    assert {event["payload"]["tool_call_id"] for event in terminal_events} == {tool_call_id}


@pytest.mark.asyncio
async def test_tool_adapter_exposes_the_public_summary_as_tool_output() -> None:
    """The client can render a semantic tool result without reading raw tool data."""
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory():
        yield session

    sink = InMemoryEventSink()
    runtime = RuntimeContext(
        user_internal_id=3,
        task_internal_id=2,
        conversation_internal_id=1,
        session_factory=session_factory,
        settings_service=object(),
        event_sink=sink,
        cancellation_service=InMemoryCancellationService(),
    )
    adapter = TestAgentToolAdapter(
        tool_executor=_CapturingExecutor(session),
        event_sink=sink,
        session_factory=session_factory,
        clock=datetime.utcnow,
        min_visible_seconds=0,
        transition_seconds=0,
    )

    await adapter.execute(
        tool_name="KnowledgeSearchTool",
        inputs={},
        ctx_runtime=runtime,
    )

    terminal_events = [
        event for event in sink.collect() if event["event_type"] == "tool_finished"
    ]
    final_payload = terminal_events[-1]["payload"]

    assert final_payload["summary"]
    assert final_payload["output"] == final_payload["summary"]
    assert final_payload["output_summary"] == final_payload["summary"]


@pytest.mark.asyncio
async def test_tool_adapter_emits_safe_display_fields_for_tool_logs() -> None:
    """The client must never render raw tool input metadata as a user-facing log."""
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory():
        yield session

    sink = InMemoryEventSink()
    runtime = RuntimeContext(
        user_internal_id=3,
        task_internal_id=2,
        conversation_internal_id=1,
        session_factory=session_factory,
        settings_service=object(),
        event_sink=sink,
        cancellation_service=InMemoryCancellationService(),
    )
    adapter = TestAgentToolAdapter(
        tool_executor=_CapturingExecutor(session),
        event_sink=sink,
        session_factory=session_factory,
        clock=datetime.utcnow,
        min_visible_seconds=0,
        transition_seconds=0,
    )

    await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={"requirement_file_id": "file-secret-token"},
        ctx_runtime=runtime,
    )

    events = sink.collect()
    started_payload = events[0]["payload"]
    terminal_payload = [
        event["payload"] for event in events if event["event_type"] == "tool_finished"
    ][-1]

    assert started_payload["display_input"] == "读取需求文档并提取结构"
    assert "file-secret-token" not in started_payload["display_input"]
    assert started_payload["input"] == started_payload["display_input"]
    assert terminal_payload["display_output"] == terminal_payload["summary"]


@pytest.mark.asyncio
async def test_tool_adapter_emits_runtime_progress_with_same_tool_call_id() -> None:
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory():
        yield session

    sink = InMemoryEventSink()
    runtime = RuntimeContext(
        user_internal_id=3,
        task_internal_id=2,
        conversation_internal_id=1,
        session_factory=session_factory,
        settings_service=object(),
        event_sink=sink,
        cancellation_service=InMemoryCancellationService(),
    )
    adapter = TestAgentToolAdapter(
        tool_executor=_ProgressExecutor(),
        event_sink=sink,
        session_factory=session_factory,
        clock=datetime.utcnow,
        min_visible_seconds=0,
        transition_seconds=0,
    )

    await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={"requirement_file_id": "file-1"},
        ctx_runtime=runtime,
    )

    events = sink.collect()
    started = next(event for event in events if event["event_type"] == "tool_started")
    progress = next(event for event in events if event["event_type"] == "tool_progress")
    finished = next(event for event in events if event["event_type"] == "tool_finished")

    assert progress["payload"]["tool_call_id"] == started["payload"]["tool_call_id"]
    assert finished["payload"]["tool_call_id"] == started["payload"]["tool_call_id"]
    assert progress["payload"]["progress_message"] == "正在处理图片：《系统架构图.png》"
    assert progress["payload"]["display_tool_name"] == "调用Word文档解析工具"
    assert progress["payload"]["business_subject_type"] == "需求文档"
    assert progress["payload"]["business_subject_name"] == "智慧校园需求说明书.docx"


@pytest.mark.asyncio
async def test_tool_adapter_sends_safe_error_summary_without_internal_exception_text() -> None:
    session = _FakeSession()

    @asynccontextmanager
    async def session_factory():
        yield session

    sink = InMemoryEventSink()
    runtime = RuntimeContext(
        user_internal_id=3,
        task_internal_id=2,
        conversation_internal_id=1,
        session_factory=session_factory,
        settings_service=object(),
        event_sink=sink,
        cancellation_service=InMemoryCancellationService(),
    )
    adapter = TestAgentToolAdapter(
        tool_executor=_ExplodingExecutor(),
        event_sink=sink,
        session_factory=session_factory,
        clock=datetime.utcnow,
        min_visible_seconds=0,
        transition_seconds=0,
    )

    await adapter.execute(
        tool_name="RequirementParserTool",
        inputs={"requirement_file_id": "file-1"},
        ctx_runtime=runtime,
    )

    failed_payload = next(
        event["payload"] for event in sink.collect() if event["event_type"] == "tool_failed"
    )

    assert failed_payload["error_summary"] == "工具执行失败，请稍后重试或检查输入文件"
    assert "Traceback" not in failed_payload["error_summary"]
    assert "C:\\" not in failed_payload["error_summary"]
    assert "api_key" not in failed_payload["error_summary"]
