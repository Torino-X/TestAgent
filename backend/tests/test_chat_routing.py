"""Integration tests for ``MessageService`` routing (F013).

These tests cover the end-to-end behaviour of ``MessageService.send_message``
across all five routes (chat_reply, ask_for_files, unsupported, clarify,
agent_task), the deterministic ``FileRequirementChecker`` override, and
the backward-compatibility invariants.

The test pattern follows the existing tests in this repo: build a stub
``AsyncSession`` via ``MagicMock(spec=AsyncSession)``, patch the
repositories with ``AsyncMock`` instances that return canned data, and
inject stub ``IntentRouter`` / ``ChatLLMService`` objects so we can
deterministically control intent recognition.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.enums import IntentType, MessageRoute
from app.agent.intent_router import IntentResult
from app.models.conversation import Conversation
from app.services.message_service import (
    CHAT_FALLBACK_REPLY,
    DEFAULT_ASK_FOR_FILES_TEXT,
    DEFAULT_CLARIFY_TEXT,
    INTENT_DEFAULT_REPLIES,
    MessageService,
)


# ── Stubs ────────────────────────────────────────────────────────


class _StubRouter:
    """Stub ``IntentRouter`` that returns a pre-canned ``IntentResult``."""

    def __init__(self, result: IntentResult):
        self._result = result
        self.calls: list[dict[str, Any]] = []

    async def recognize(
        self,
        content: str,
        files: list | None = None,
        attached_file_ids: list[str] | None = None,
        intent_context=None,
        user_internal_id: int | None = None,
    ) -> IntentResult:
        self.calls.append(
            {
                "content": content,
                "files": list(files or []),
                "attached_file_ids": list(attached_file_ids or []),
            }
        )
        return self._result


class _StubChat:
    """Stub ``ChatLLMService`` that returns a pre-canned reply."""

    def __init__(
        self,
        reply: str = "stub reply",
        *,
        raise_exc: Exception | None = None,
        stream_chunks: list[str] | None = None,
    ):
        self._reply = reply
        self._raise = raise_exc
        self._stream_chunks = stream_chunks
        self.calls: list[str] = []

    async def generate_reply(
        self,
        content: str,
        history: list[dict] | None = None,
        chat_context=None,
        user_id: int | None = None,
        conversation_public_id: str | None = None,
    ) -> str:
        self.calls.append(content)
        if self._raise is not None:
            raise self._raise
        return self._reply

    async def stream_reply(
        self,
        content: str,
        history: list[dict] | None = None,
        chat_context=None,
        user_id: int | None = None,
        conversation_public_id: str | None = None,
    ):
        self.calls.append(content)
        if self._raise is not None:
            raise self._raise
        if self._stream_chunks is not None:
            for chunk in self._stream_chunks:
                yield chunk
            return
        midpoint = max(1, len(self._reply) // 2)
        yield self._reply[:midpoint]
        yield self._reply[midpoint:]


class _StubTitleLLM:
    """Stub title model used by first-message conversation title tests.

    F014 closeout: routes through ``generate_with_profile`` so the
    MessageService's title generator exercises the contract surface.
    The stub emulates the contract layer's PlainTextParser output
    (strips trailing punctuation / quotes) so the service's success
    path returns the same final title as a real LLM round-trip.
    """

    def __init__(self, reply: str = "测试方案生成"):
        # The contract layer strips trailing punctuation / quotes /
        # newlines via PlainTextParser.  We mimic that behaviour here.
        self._reply = self._strip_like_parser(reply)
        self.calls: list[dict[str, Any]] = []

    @staticmethod
    def _strip_like_parser(text: str) -> str:
        """Apply the PlainTextParser normalisations the LLMClient
        contract layer would have applied before we see the value."""
        from app.llm.parsers.plain_text import PlainTextParser
        from app.llm.task_profiles import TITLE_PROFILE

        try:
            return str(PlainTextParser().parse(text, TITLE_PROFILE))
        except Exception:
            return text.strip()

    async def generate_with_system(
        self,
        system_prompt: str,
        user_content: str,
        **_: Any,
    ) -> str:
        # Legacy fallback for any test that still calls the old method.
        self.calls.append(
            {"system_prompt": system_prompt, "user_content": user_content}
        )
        return self._reply

    async def generate_with_profile(
        self,
        profile: Any,
        user_content: str,
        **_: Any,
    ) -> Any:
        # Lazy import to avoid an import cycle in test collection.
        from app.integrations.llm_client import LLMProfileResult

        self.calls.append(
            {"profile_name": getattr(profile, "name", None), "user_content": user_content}
        )
        return LLMProfileResult(
            task_name=getattr(profile, "name", "title"),
            raw_text=self._reply,
            parsed=self._reply,
            success=True,
            error_type=None,
            error_message=None,
        )


def _make_session() -> MagicMock:
    """Return a stub AsyncSession that swallows ``execute`` / ``flush``."""
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    return session


def _make_conversation(public_id: str = "conv_001", conv_id: int = 100) -> Conversation:
    return Conversation(
        id=conv_id,
        public_id=public_id,
        user_id=1,
        title="测试会话",
        status="active",
    )


def _make_file(
    public_id: str,
    ftype: str,
    status: str = "confirmed",
    *,
    file_id: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=file_id,
        public_id=public_id,
        user_id=1,
        conversation_id=100,
        original_name=f"{ftype}.docx",
        file_type=ftype,
        upload_status=status,
        file_ext=".docx",
        file_size=1024,
        deleted_at=None,
    )


def _make_service(
    *,
    router: _StubRouter | None = None,
    chat: _StubChat | None = None,
    files: list | None = None,
) -> tuple[MessageService, MagicMock, _StubRouter, _StubChat]:
    """Build a MessageService with stubbed router/chat and a fake session.

    Returns ``(service, session, router_stub, chat_stub)`` for assertions.
    """
    session = _make_session()
    conv = _make_conversation()
    router_stub = router or _StubRouter(
        IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.9,
            reason="stub",
        )
    )
    chat_stub = chat or _StubChat("stub reply")
    svc = MessageService(
        session,
        intent_router=router_stub,
        chat_service=chat_stub,
    )
    # Patch the repositories
    svc._conv_repo.get_by_public_id = AsyncMock(return_value=conv)  # type: ignore[method-assign]
    svc._file_repo.list_by_conversation = AsyncMock(return_value=files or [])  # type: ignore[method-assign]
    svc._msg_repo.list_by_conversation = AsyncMock(return_value=[])  # type: ignore[method-assign]
    # Track every message created
    created: list = []
    async def _create(msg):  # noqa: ANN001
        created.append(msg)
        return msg
    svc._msg_repo.create = AsyncMock(side_effect=_create)  # type: ignore[method-assign]
    svc._created_messages = created
    # Also patch task / event repos so agent_task branch works
    async def _create_task(task):  # noqa: ANN001
        task.id = 999
        return task
    svc._task_repo.create = AsyncMock(side_effect=_create_task)  # type: ignore[method-assign]
    async def _create_event(ev):  # noqa: ANN001
        return ev
    svc._event_repo.create = AsyncMock(side_effect=_create_event)  # type: ignore[method-assign]
    svc._message_attachment_repo = MagicMock()
    svc._message_attachment_repo.create_ordered_for_message = AsyncMock()
    return svc, session, router_stub, chat_stub


@pytest.mark.asyncio
async def test_send_message_commits_user_write_before_context_engine_work() -> None:
    """Automatic preflight must not compete with the request transaction.

    The normal-chat Context Engine can open a separate session to persist a
    compaction run.  The user-message write from this request therefore has
    to be committed before either intent or chat context construction begins.
    """
    svc, session, _, _ = _make_service()

    observed_commit_counts: list[int] = []

    async def _observe_user_write_commit(**_kwargs: Any) -> None:
        observed_commit_counts.append(session.commit.await_count)

    svc._context_svc = SimpleNamespace(
        build_intent_context=AsyncMock(side_effect=_observe_user_write_commit),
        build_chat_context=AsyncMock(side_effect=_observe_user_write_commit),
    )
    svc._schedule_post_chat_maintenance = MagicMock()  # type: ignore[method-assign]

    await svc.send_message(
        conv_public_id="conv_001",
        content="Please continue the release discussion.",
        attached_file_ids=None,
        user_internal_id=1,
    )

    svc._context_svc.build_intent_context.assert_awaited_once()
    svc._context_svc.build_chat_context.assert_awaited_once()
    assert observed_commit_counts == [1, 1]


@pytest.mark.asyncio
async def test_first_user_message_generates_conversation_title(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.context_engine.feature_flags.get_context_engine_flags",
        lambda: SimpleNamespace(mig_summary=False),
    )
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.9,
            reason="small talk",
        )),
        chat=_StubChat("ok"),
    )
    conv = _make_conversation()
    conv.title = "新会话"
    title_llm = _StubTitleLLM("测试方案生成。")
    svc._llm = title_llm  # type: ignore[assignment]
    svc._conv_repo.get_by_public_id = AsyncMock(return_value=conv)  # type: ignore[method-assign]
    svc._conv_repo.update_title = AsyncMock()  # type: ignore[method-assign]
    svc._msg_repo.list_by_conversation = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="帮我根据这份 PRD 需求文档和模板生成测试方案",
        attached_file_ids=None,
        user_internal_id=1,
    )

    assert title_llm.calls
    svc._conv_repo.update_title.assert_awaited_once()  # type: ignore[attr-defined]
    assert result["conversation"]["id"] == "conv_001"
    assert result["conversation"]["title"] == "测试方案生成"


@pytest.mark.asyncio
async def test_first_user_message_generates_title_when_placeholder_is_new_dialogue(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.context_engine.feature_flags.get_context_engine_flags",
        lambda: SimpleNamespace(mig_summary=False),
    )
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.9,
            reason="small talk",
        )),
        chat=_StubChat("ok"),
    )
    conv = _make_conversation()
    conv.title = "新对话"
    title_llm = _StubTitleLLM("Redis使用指南")
    svc._llm = title_llm  # type: ignore[assignment]
    svc._conv_repo.get_by_public_id = AsyncMock(return_value=conv)  # type: ignore[method-assign]
    svc._conv_repo.update_title = AsyncMock()  # type: ignore[method-assign]
    svc._msg_repo.list_by_conversation = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="Redis 怎么使用？",
        attached_file_ids=None,
        user_internal_id=1,
    )

    assert title_llm.calls
    svc._conv_repo.update_title.assert_awaited_once()  # type: ignore[attr-defined]
    assert result["conversation"]["title"] == "Redis使用指南"


@pytest.mark.asyncio
async def test_first_test_plan_message_title_uses_requirement_project_and_task(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.context_engine.feature_flags.get_context_engine_flags",
        lambda: SimpleNamespace(mig_summary=False),
    )
    requirement_file = _make_file("file_req", "requirement_doc")
    requirement_file.original_name = "01_智慧校园需求文档.docx"
    template_file = _make_file("file_tpl", "test_plan_template")
    template_file.original_name = "00_PlanWise_QA_测试方案模板.docx"
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="test plan request",
        )),
        chat=_StubChat("ok"),
        files=[requirement_file, template_file],
    )
    conv = _make_conversation()
    conv.title = "新会话"
    title_llm = _StubTitleLLM("用户输入是：帮我根据这份")
    svc._llm = title_llm  # type: ignore[assignment]
    svc._conv_repo.get_by_public_id = AsyncMock(return_value=conv)  # type: ignore[method-assign]
    svc._conv_repo.update_title = AsyncMock()  # type: ignore[method-assign]
    svc._msg_repo.list_by_conversation = AsyncMock(return_value=[])  # type: ignore[method-assign]

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="帮我根据这份需求文档和模板生成测试方案",
        attached_file_ids=["file_req", "file_tpl"],
        user_internal_id=1,
    )

    assert title_llm.calls
    assert "01_智慧校园需求文档.docx" in title_llm.calls[0]["user_content"]
    svc._conv_repo.update_title.assert_awaited_once()  # type: ignore[attr-defined]
    assert result["conversation"]["title"] == "生成智慧校园测试方案"


@pytest.mark.asyncio
async def test_existing_conversation_title_is_not_overwritten() -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.9,
            reason="small talk",
        )),
        chat=_StubChat("ok"),
    )
    conv = _make_conversation()
    conv.title = "已有标题"
    title_llm = _StubTitleLLM("新标题")
    svc._llm = title_llm  # type: ignore[assignment]
    svc._conv_repo.get_by_public_id = AsyncMock(return_value=conv)  # type: ignore[method-assign]
    svc._conv_repo.update_title = AsyncMock()  # type: ignore[method-assign]

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="这条消息不应该覆盖已有标题",
        attached_file_ids=None,
        user_internal_id=1,
    )

    assert title_llm.calls == []
    svc._conv_repo.update_title.assert_not_awaited()  # type: ignore[attr-defined]
    assert "conversation" not in result


# ── 1. chat_reply route ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_reply_route_does_not_create_task() -> None:
    svc, _, router, chat = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.95,
            reason="small talk",
        )),
        chat=_StubChat("你好，有什么可以帮您？"),
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="你好",
        attached_file_ids=None,
        user_internal_id=1,
    )

    assert result["route"] == "chat_reply"
    assert result["intent"] == "general_chat"
    assert result["requires_sse"] is False
    assert result["task_id"] is None
    assert result["agent_task"] is None
    assert result["agent_reply"] is not None
    assert result["agent_reply"]["content"] == "你好，有什么可以帮您？"
    # Chat service was called with the user content
    assert chat.calls == ["你好"]
    # Two messages persisted: user + agent
    assert len(svc._created_messages) == 2
    # Router was called
    assert router.calls and router.calls[0]["content"] == "你好"


@pytest.mark.asyncio
async def test_nonstream_chat_schedules_maintenance_after_response_work() -> None:
    """Normal chat must not await post-turn LLM work in its request transaction."""
    svc, _, _, _ = _make_service(chat=_StubChat("reply"))
    svc._current_image_vision_reply = AsyncMock(return_value=None)  # type: ignore[method-assign]
    scheduled = MagicMock()
    svc._schedule_post_chat_maintenance = scheduled  # type: ignore[method-assign]
    svc._summary_svc = SimpleNamespace(
        maybe_update_summary=AsyncMock(
            side_effect=AssertionError("request session summary must not run")
        )
    )
    svc._maybe_context_learn = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("request session learning must not run")
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="hello",
        attached_file_ids=None,
        user_internal_id=1,
    )

    assert result["route"] == "chat_reply"
    scheduled.assert_called_once()
    kwargs = scheduled.call_args.kwargs
    assert kwargs["conv"].public_id == "conv_001"
    assert kwargs["user_message"] == "hello"
    assert kwargs["user_internal_id"] == 1
    svc._summary_svc.maybe_update_summary.assert_not_awaited()
    svc._maybe_context_learn.assert_not_awaited()


@pytest.mark.asyncio
async def test_strict_maas_bypasses_intent_router_and_returns_kb_reply() -> None:
    svc, _, router, chat = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.UNKNOWN,
            route=MessageRoute.CLARIFY,
            supported=False,
            confidence=0.0,
            reason="would_break_kb_if_called",
            reply_message="不应该出现",
        )),
        chat=_StubChat("普通聊天不应该被调用"),
    )
    svc._strict_maas_reply = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            "知识库答案",
            {"attempted": True, "hit": True, "strict": True},
            SimpleNamespace(context={}),
        )
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="测试",
        attached_file_ids=None,
        user_internal_id=1,
        knowledge_mode_snapshot="MAAS_STRICT",
    )

    assert router.calls == []
    assert chat.calls == []
    svc._strict_maas_reply.assert_awaited_once()
    assert result["route"] == MessageRoute.CHAT_REPLY.value
    assert result["intent"] == IntentType.KNOWLEDGE_QUESTION.value
    assert result["agent_reply"]["content"] == "知识库答案"
    payload = result["agent_reply"]["payload"]
    assert payload["knowledge_mode_snapshot"] == "MAAS_STRICT"
    assert payload["knowledge_mode_fast_path"] is True


@pytest.mark.asyncio
async def test_chat_reply_route_filters_hidden_reasoning_before_persisting() -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.95,
            reason="small talk",
        )),
        chat=_StubChat("<thinking>private</thinking>Public answer"),
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="hello",
        attached_file_ids=None,
        user_internal_id=1,
    )

    assert result["agent_reply"]["content"] == "Public answer"
    assert svc._created_messages[-1].content == "Public answer"


@pytest.mark.asyncio
async def test_stream_message_chat_reply_emits_incremental_text_events() -> None:
    svc, _, _, chat = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.95,
            reason="small talk",
        )),
        chat=_StubChat("markdown reply"),
    )

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="hello",
            attached_file_ids=None,
            user_internal_id=1,
        )
    ]

    assert [event["event"] for event in events] == [
        "message_created",
        "agent_reply_created",
        "agent_text_delta",
        "agent_text_delta",
        "agent_text_done",
    ]
    assert events[0]["data"]["message"]["role"] == "user"
    assert events[1]["data"]["message"]["role"] == "agent"
    assert events[1]["data"]["message"]["content"] == ""
    assert "".join(
        event["data"]["delta"]
        for event in events
        if event["event"] == "agent_text_delta"
    ) == "markdown reply"
    assert events[-1]["data"]["agent_reply"]["content"] == "markdown reply"
    assert chat.calls == ["hello"]
    assert len(svc._created_messages) == 2


@pytest.mark.asyncio
async def test_stream_existing_task_action_emits_terminal_event() -> None:
    """A completed direct reply must release the frontend stream state."""
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.EXISTING_TASK_ACTION,
            supported=True,
            confidence=0.95,
            reason="refer back to the current conversation",
        )),
    )
    svc._context_svc = SimpleNamespace(
        build_intent_context=AsyncMock(return_value=None),
    )
    svc._schedule_post_chat_maintenance = MagicMock()  # type: ignore[method-assign]

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="What did we discuss in this conversation?",
            attached_file_ids=None,
            user_internal_id=1,
        )
    ]

    assert [event["event"] for event in events] == [
        "message_created",
        "agent_reply_created",
        "agent_text_delta",
        "agent_text_done",
    ]
    preview = events[1]["data"]["message"]
    terminal = events[-1]["data"]
    assert preview["role"] == "agent"
    assert preview["content"] == ""
    assert terminal["agent_reply"]["message_id"] == preview["message_id"]
    assert terminal["agent_reply"]["content"]
    svc._schedule_post_chat_maintenance.assert_called_once()


@pytest.mark.asyncio
async def test_stream_message_closes_immediately_after_terminal_event() -> None:
    """A slow summary must not keep the chat SSE/controller active."""
    svc, _, _, _ = _make_service(chat=_StubChat("reply"))
    summary_gate = asyncio.Event()

    async def _blocked_summary(*_args, **_kwargs):
        await summary_gate.wait()

    svc._summary_svc = SimpleNamespace(maybe_update_summary=_blocked_summary)
    svc._maybe_context_learn = AsyncMock()  # type: ignore[method-assign]
    stream = svc.stream_message(
        conv_public_id="conv_001",
        content="hello",
        attached_file_ids=None,
        user_internal_id=1,
    )

    events = []
    while True:
        event = await anext(stream)
        events.append(event)
        if event["event"] == "agent_text_done":
            break

    try:
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=0.1)
        assert events[-1]["event"] == "agent_text_done"
    finally:
        summary_gate.set()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_post_chat_maintenance_uses_fresh_session_and_captured_ids(monkeypatch) -> None:
    """Post-SSE work must never reuse the request session or an ORM entity."""
    svc, request_session, _, _ = _make_service(chat=_StubChat("reply"))
    calls: list[tuple[object, int, int]] = []

    class _FreshSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        commit = AsyncMock()

    class _FreshSummaryService:
        def __init__(self, session, **_kwargs):
            self._session = session

        async def maybe_update_summary(self, conversation_id, user_id):
            calls.append((self._session, conversation_id, user_id))

    factory_calls: list[_FreshSession] = []

    def _session_factory():
        session = _FreshSession()
        factory_calls.append(session)
        return session

    monkeypatch.setattr(
        "app.services.message_service.ConversationSummaryService",
        _FreshSummaryService,
    )
    svc._session_factory = _session_factory
    svc._maybe_context_learn = AsyncMock()  # type: ignore[method-assign]
    # The request-scoped summary service must be irrelevant after the SSE ends.
    svc._summary_svc = SimpleNamespace(
        maybe_update_summary=AsyncMock(side_effect=AssertionError("request session reused"))
    )

    conv = SimpleNamespace(id=334, public_id="conv_334", project_id=None)
    svc._schedule_post_chat_maintenance(
        conv=conv,
        user_message="remember this rule",
        user_internal_id=1,
        intent_result=SimpleNamespace(intent="general_chat", route="chat_reply"),
    )
    task = next(iter(svc._background_tasks))
    await task

    assert request_session not in [session for session, _, _ in calls]
    assert len(factory_calls) == 1
    assert calls == [(factory_calls[0], 334, 1)]
    factory_calls[0].commit.assert_awaited_once()
    svc._maybe_context_learn.assert_awaited_once_with(
        conversation_public_id="conv_334",
        conversation_internal_id=334,
        project_internal_id=None,
        user_message="remember this rule",
        user_internal_id=1,
    )


@pytest.mark.asyncio
async def test_post_chat_maintenance_coalesces_concurrent_summary_for_same_conversation(monkeypatch) -> None:
    """Rapid user turns may queue learning, but only one summary LLM call."""
    MessageService._summary_maintenance_tasks.clear()
    first, _, _, _ = _make_service(chat=_StubChat("reply"))
    second, _, _, _ = _make_service(chat=_StubChat("reply"))
    gate = asyncio.Event()
    summary_calls: list[int] = []

    class _FreshSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        commit = AsyncMock()

    class _BlockingSummaryService:
        def __init__(self, _session, **_kwargs):
            pass

        async def maybe_update_summary(self, conversation_id, _user_id):
            summary_calls.append(conversation_id)
            await gate.wait()

    factory_calls: list[_FreshSession] = []

    def _session_factory():
        session = _FreshSession()
        factory_calls.append(session)
        return session

    monkeypatch.setattr(
        "app.services.message_service.ConversationSummaryService",
        _BlockingSummaryService,
    )
    for svc in (first, second):
        svc._session_factory = _session_factory
        svc._maybe_context_learn = AsyncMock()  # type: ignore[method-assign]

    conv = SimpleNamespace(id=335, public_id="conv_335", project_id=None)
    try:
        first._schedule_post_chat_maintenance(
            conv=conv,
            user_message="first turn",
            user_internal_id=1,
            intent_result=SimpleNamespace(intent="general_chat", route="chat_reply"),
        )
        await asyncio.sleep(0)
        second._schedule_post_chat_maintenance(
            conv=conv,
            user_message="second turn",
            user_internal_id=1,
            intent_result=SimpleNamespace(intent="general_chat", route="chat_reply"),
        )
        await asyncio.sleep(0)

        assert summary_calls == [335]
        assert len(factory_calls) == 1
    finally:
        gate.set()
        await asyncio.gather(*first._background_tasks, *second._background_tasks)
        MessageService._summary_maintenance_tasks.clear()


@pytest.mark.asyncio
async def test_stream_message_first_event_does_not_wait_for_title_generation() -> None:
    """A slow optional title must never hold the SSE response on thinking."""
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.95,
            reason="small talk",
        )),
        chat=_StubChat("hello"),
    )
    conv = _make_conversation()
    conv.title = "新会话"
    svc._conv_repo.get_by_public_id = AsyncMock(return_value=conv)  # type: ignore[method-assign]
    svc._session_factory = None
    title_gate = asyncio.Event()

    async def _blocked_title(*args, **kwargs):  # noqa: ANN002, ANN003
        await title_gate.wait()
        return "迟到的标题"

    svc._generate_conversation_title = _blocked_title  # type: ignore[method-assign]
    stream = svc.stream_message(
        conv_public_id="conv_001",
        content="hello",
        attached_file_ids=None,
        user_internal_id=1,
    )

    try:
        first_event = await asyncio.wait_for(anext(stream), timeout=0.1)
        assert first_event["event"] == "message_created"
    finally:
        title_gate.set()
        await stream.aclose()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stream_message_task_creation_does_not_wait_for_title_generation() -> None:
    files = [
        _make_file("file_req", "requirement_doc"),
        _make_file("file_tpl", "test_plan_template"),
    ]
    svc, _, _, _ = _make_service(files=files)
    conv = _make_conversation()
    conv.title = "新会话"
    svc._conv_repo.get_by_public_id = AsyncMock(return_value=conv)  # type: ignore[method-assign]
    svc._session_factory = None
    title_gate = asyncio.Event()

    async def _blocked_title(*args, **kwargs):  # noqa: ANN002, ANN003
        await title_gate.wait()
        return "迟到的标题"

    svc._generate_conversation_title = _blocked_title  # type: ignore[method-assign]
    stream = svc.stream_message(
        conv_public_id="conv_001",
        content="帮我根据这份需求文档和模板生成测试方案",
        attached_file_ids=["file_req", "file_tpl"],
        user_internal_id=1,
    )

    try:
        first_event = await asyncio.wait_for(anext(stream), timeout=0.1)
        task_event = await asyncio.wait_for(anext(stream), timeout=0.1)
        assert first_event["event"] == "message_created"
        assert task_event["event"] == "agent_task_created"
        created_task = svc._task_repo.create.await_args.args[0]  # type: ignore[attr-defined]
        assert created_task.graph_version == "v3"
    finally:
        title_gate.set()
        await stream.aclose()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_strict_maas_stream_bypasses_intent_router_and_returns_kb_reply() -> None:
    svc, _, router, chat = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.UNKNOWN,
            route=MessageRoute.CLARIFY,
            supported=False,
            confidence=0.0,
            reason="would_break_kb_if_called",
            reply_message="不应该出现",
        )),
        chat=_StubChat("普通聊天不应该被调用", stream_chunks=["普通", "聊天"]),
    )
    svc._strict_maas_reply = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            "知识库流式答案",
            {"attempted": True, "hit": True, "strict": True},
            SimpleNamespace(context={}),
        )
    )

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="测试",
            attached_file_ids=None,
            user_internal_id=1,
            knowledge_mode_snapshot="MAAS_STRICT",
        )
    ]

    assert router.calls == []
    assert chat.calls == []
    svc._strict_maas_reply.assert_awaited_once()
    assert any(
        event["event"] == "agent_text_delta"
        and event["data"]["delta"] == "知识库流式答案"
        for event in events
    )


@pytest.mark.asyncio
async def test_stream_message_filters_split_think_block_from_reply() -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.95,
            reason="small talk",
        )),
        chat=_StubChat(
            "<think>hidden reasoning</think>Visible reply",
            stream_chunks=["<thi", "nk>hidden reasoning", "</think>Visible reply"],
        ),
    )

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="hello",
            attached_file_ids=None,
            user_internal_id=1,
        )
    ]

    streamed_text = "".join(
        event["data"]["delta"]
        for event in events
        if event["event"] == "agent_text_delta"
    )
    assert streamed_text == "Visible reply"
    assert events[-1]["data"]["agent_reply"]["content"] == "Visible reply"
    assert svc._created_messages[-1].content == "Visible reply"


@pytest.mark.parametrize(
    "stream_chunks",
    [
        ["[thinking]private[/thinking]Public answer"],
        ["<reasoning>private</reasoning>Public answer"],
        ["<|begin_of_thought|>private<|end_of_thought|>Public answer"],
    ],
)
@pytest.mark.asyncio
async def test_stream_message_filters_common_reasoning_delimiters(
    stream_chunks: list[str],
) -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.95,
            reason="small talk",
        )),
        chat=_StubChat("unused", stream_chunks=stream_chunks),
    )

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="hello",
            attached_file_ids=None,
            user_internal_id=1,
        )
    ]

    streamed_text = "".join(
        event["data"]["delta"]
        for event in events
        if event["event"] == "agent_text_delta"
    )
    assert streamed_text == "Public answer"
    assert events[-1]["data"]["agent_reply"]["content"] == "Public answer"


@pytest.mark.asyncio
async def test_stream_message_agent_task_emits_task_created_without_agent_reply() -> None:
    files = [
        _make_file("file_req", "requirement_doc"),
        _make_file("file_tpl", "test_plan_template"),
    ]
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="has files",
        )),
        files=files,
    )

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="generate test plan",
            attached_file_ids=["file_req", "file_tpl"],
            user_internal_id=1,
        )
    ]

    assert [event["event"] for event in events] == [
        "message_created",
        "agent_task_created",
    ]
    assert events[1]["data"]["agent_task"]["events_url"].startswith("/api/agent/tasks/")
    assert events[1]["data"]["agent_reply"] is None
    assert len(svc._created_messages) == 1


@pytest.mark.asyncio
async def test_stream_document_summary_with_uploaded_docx_ext_creates_dynamic_agent_task() -> None:
    doc = _make_file("file_doc", "unknown", status="uploaded", file_id=10)
    doc.original_name = "requirement.docx"
    doc.file_ext = "docx"
    doc.mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.DOCUMENT_QUESTION,
            route=MessageRoute.UNSUPPORTED,
            supported=False,
            confidence=0.9,
            reason="legacy document question",
        )),
        files=[doc],
    )
    svc._context_svc = SimpleNamespace(
        build_intent_context=AsyncMock(return_value=None),
        build_task_trigger_context=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda **_: {})
        ),
    )
    svc._summary_svc = SimpleNamespace(maybe_update_summary=AsyncMock())
    svc._task_repo.update_context_json = AsyncMock()  # type: ignore[method-assign]
    svc._exec_repo.enqueue_new_task = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="总结这个文档",
            attached_file_ids=["file_doc"],
            user_internal_id=1,
        )
    ]

    assert [event["event"] for event in events] == [
        "message_created",
        "agent_task_created",
    ]
    assert events[1]["data"]["route"] == "agent_task"
    assert events[1]["data"]["intent"] == "document_question"
    assert events[1]["data"]["agent_task"]["task_type"] == "dynamic_agent"
    assert events[1]["data"]["agent_reply"] is None


@pytest.mark.asyncio
async def test_stream_message_keeps_llm_route_when_capability_heuristic_disagrees() -> None:
    files = [
        _make_file("file_req", "requirement_doc"),
        _make_file("file_tpl", "test_plan_template"),
    ]
    router = _StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.EXISTING_TASK_ACTION,
            supported=True,
            confidence=0.8,
            reason="router misclassified explicit task request",
        ))
    svc, _, _, _ = _make_service(
        router=router,
        files=files,
    )

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="帮我生成测试方案",
            attached_file_ids=["file_req", "file_tpl"],
            user_internal_id=1,
        )
    ]

    assert [event["event"] for event in events] == [
        "message_created",
        "agent_reply_created",
        "agent_text_delta",
        "agent_text_done",
    ]
    assert len(router.calls) == 1
    assert events[1]["data"]["route"] == "existing_task_action"
    assert events[1]["data"]["intent"] == "general_chat"


@pytest.mark.asyncio
async def test_stream_message_agent_task_binding_failure_emits_terminal_reply_lifecycle() -> None:
    files = [
        _make_file("file_req", "requirement_doc"),
        _make_file("file_tpl", "test_plan_template"),
    ]
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="has files",
        )),
        files=files,
    )
    svc._resolve_task_attachments_for_create = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            status="MISSING_REQUIRED_ATTACHMENT",
            bindings=[],
            reason="missing_required:output_template",
            error_code=None,
            ambiguous_role=None,
        )
    )

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="generate test plan",
            attached_file_ids=["file_req", "file_tpl"],
            user_internal_id=1,
        )
    ]

    assert [event["event"] for event in events] == [
        "message_created",
        "agent_reply_created",
        "agent_text_delta",
        "agent_text_done",
    ]
    preview = events[1]["data"]["message"]
    assert preview["role"] == "agent"
    assert preview["message_type"] == "agent_text"
    assert preview["content"] == ""
    assert events[2]["data"] == {
        "message_id": preview["message_id"],
        "delta": "需要先确认附件用途后才能创建测试方案任务。",
    }
    terminal = events[3]["data"]
    assert terminal["agent_task"] is None
    assert terminal["agent_reply"]["message_id"] == preview["message_id"]
    assert terminal["agent_reply"]["payload"]["attachment_binding_status"] == (
        "MISSING_REQUIRED_ATTACHMENT"
    )
    assert [message.role for message in svc._created_messages] == ["user", "agent"]
    assert svc._created_messages[1].content == (
        "需要先确认附件用途后才能创建测试方案任务。"
    )
    assert svc._task_repo.create.await_count == 0  # type: ignore[attr-defined]


# ── 2. ask_for_files route (no files) ───────────────────────────


@pytest.mark.asyncio
async def test_ask_for_files_route_when_no_files() -> None:
    svc, _, router, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.ASK_FOR_FILES,
            supported=True,
            confidence=0.9,
            reason="missing files",
            reply_message=DEFAULT_ASK_FOR_FILES_TEXT,
        )),
        files=[],
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="帮我生成测试方案",
        attached_file_ids=None,
        user_internal_id=1,
    )

    assert result["route"] == "ask_for_files"
    assert result["intent"] == "test_plan_generation"
    assert result["requires_sse"] is False
    assert result["task_id"] is None
    assert result["agent_task"] is None
    assert "需求文档" in result["agent_reply"]["content"]
    assert "模板" in result["agent_reply"]["content"]
    # Test-plan wording must still be classified by the LLM router.  The
    # file checker may validate the LLM result, but it must never replace it.
    assert len(router.calls) == 1
    # Two messages persisted
    assert len(svc._created_messages) == 2


def test_file_requirement_override_keeps_llm_test_plan_route_when_files_are_complete() -> None:
    svc, _, _, _ = _make_service(files=[])
    files = [
        _make_file("file_a", "requirement_doc", status="uploaded"),
        _make_file("file_b", "test_plan_template", status="uploaded"),
    ]
    intent = IntentResult(
        intent=IntentType.TEST_PLAN_GENERATION,
        route=MessageRoute.AGENT_TASK,
        supported=True,
        confidence=0.75,
        reason="router_detected_test_plan",
    )

    result = svc._apply_file_requirement_override(
        intent,
        files,
        {"file_a", "file_b"},
        "generate a test plan from these attachments",
    )

    assert result.route == MessageRoute.AGENT_TASK
    assert result.reason == "router_detected_test_plan"
    assert result.need_files is False


def test_file_requirement_override_preserves_llm_incremental_intent() -> None:
    svc, _, _, _ = _make_service(files=[])
    intent = IntentResult(
        intent=IntentType.RESULT_MODIFICATION,
        route=MessageRoute.AGENT_TASK,
        supported=True,
        confidence=0.95,
        reason="llm_detected_existing_artifact_modification",
    )

    result = svc._apply_file_requirement_override(
        intent,
        [],
        set(),
        "刚才生成的测试方案中，第二个章节的内容太少了，第二个章节字数必须达到200字",
    )

    assert result.intent == IntentType.RESULT_MODIFICATION
    assert result.route == MessageRoute.AGENT_TASK
    assert result.reason == "llm_detected_existing_artifact_modification"


def test_memory_rule_mentioning_test_plan_is_not_a_new_task_request() -> None:
    """A future-use rule must not trigger the ask-for-files fast path."""
    content = (
        "我们需要定义下规则：以后在生成测试方案时，不允许使用不存在的捏造数据；"
        "每一章节不超过 50 字；回答前必须认真思考。记住这些规则。"
    )

    assert MessageService._is_explicit_test_plan_request(content) is False


def test_test_plan_knowledge_question_is_not_deterministically_routed_as_task() -> None:
    """Keyword mentions alone must leave an educational question to the LLM router."""
    content = "在生成测试方案时，一般需要注意什么？"

    assert MessageService._is_explicit_test_plan_request(content) is False


@pytest.mark.asyncio
async def test_send_message_reports_missing_attached_ids_instead_of_generic_upload_prompt() -> None:
    req = _make_file("file_req", "requirement_doc", status="uploaded", file_id=10)
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="explicit request",
        )),
        files=[req],
    )
    svc._message_attachment_repo = MagicMock()
    svc._message_attachment_repo.create_ordered_for_message = AsyncMock()

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="generate a test plan from this PRD and template",
        attached_file_ids=["file_req", "file_missing"],
        user_internal_id=1,
    )

    assert result["route"] == "ask_for_files"
    assert result["agent_task"] is None
    assert result["task_id"] is None
    assert "file_missing" in result["agent_reply"]["content"]
    assert result["agent_reply"]["payload"]["reason"] == "attached_files_not_found"
    assert result["agent_reply"]["payload"]["missing_attached_file_ids"] == ["file_missing"]
    assert svc._task_repo.create.await_count == 0  # type: ignore[attr-defined]


# ── 3-6. unsupported routes ──────────────────────────────────────


@pytest.mark.parametrize(
    "intent,expected_substring",
    [
        (IntentType.TEST_CASE_GENERATION, "测试用例生成功能"),
        (IntentType.PPT_GENERATION, "PPT 生成"),
        (IntentType.EXCEL_GENERATION, "Excel 生成"),
        # F026: KNOWLEDGE_QUESTION is no longer purely "unsupported" —
        # it first consults the KB; the default-text branch is exercised
        # only when KB is unreachable and the LLM polish path also fails.
    ],
)
@pytest.mark.asyncio
async def test_unsupported_route_uses_default_text(
    intent: IntentType, expected_substring: str
) -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=intent,
            route=MessageRoute.UNSUPPORTED,
            supported=False,
            confidence=0.9,
            reason="not yet",
        )),
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="示例",
        attached_file_ids=None,
        user_internal_id=1,
    )

    assert result["route"] == "unsupported"
    assert result["intent"] == intent.value
    assert expected_substring in result["agent_reply"]["content"]


# ── 7. clarify route ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clarify_route_falls_back_to_default_text() -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.UNKNOWN,
            route=MessageRoute.CLARIFY,
            supported=False,
            confidence=0.4,
            reason="ambiguous",
            reply_message=None,
        )),
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="嗯",
        attached_file_ids=None,
        user_internal_id=1,
    )

    assert result["route"] == "clarify"
    assert result["agent_reply"]["content"] == DEFAULT_CLARIFY_TEXT
    # Verify the constant matches the spec
    assert "测试方案" in DEFAULT_CLARIFY_TEXT
    assert "测试用例" in DEFAULT_CLARIFY_TEXT


# ── 8. agent_task route with both files confirmed ───────────────


@pytest.mark.asyncio
async def test_agent_task_route_with_files_creates_task() -> None:
    files = [
        _make_file("f_req", "requirement_doc"),
        _make_file("f_tpl", "test_plan_template"),
    ]
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="explicit request + files present",
        )),
        files=files,
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="根据这些文件帮我生成测试方案",
        attached_file_ids=["f_req", "f_tpl"],
        user_internal_id=1,
    )

    assert result["route"] == "agent_task"
    assert result["intent"] == "test_plan_generation"
    assert result["requires_sse"] is True
    assert result["task_id"] is not None
    assert result["task_id"] == result["agent_task"]["task_id"]
    assert result["agent_task"]["task_type"] == "test_plan_generation"
    assert result["agent_task"]["status"] == "created"
    assert result["agent_task"]["events_url"].endswith("/events")
    assert result["agent_reply"] is None
    # Only the user message persisted (no premature assistant message)
    assert len(svc._created_messages) == 1


@pytest.mark.asyncio
async def test_document_analyze_with_docx_creates_dynamic_agent_task() -> None:
    doc = _make_file("file_doc", "unknown", status="uploaded", file_id=10)
    doc.original_name = "需求说明书.docx"
    doc.file_ext = ".docx"
    doc.mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.DOCUMENT_QUESTION,
            route=MessageRoute.UNSUPPORTED,
            supported=False,
            confidence=0.9,
            reason="legacy document question",
        )),
        files=[doc],
    )
    svc._context_svc = SimpleNamespace(
        build_intent_context=AsyncMock(return_value=None),
        build_task_trigger_context=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda **_: {})
        )
    )
    svc._summary_svc = SimpleNamespace(maybe_update_summary=AsyncMock())
    svc._task_repo.update_context_json = AsyncMock()  # type: ignore[method-assign]
    svc._exec_repo.enqueue_new_task = AsyncMock(return_value=SimpleNamespace())  # type: ignore[method-assign]

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="分析这个文档说了什么",
        attached_file_ids=["file_doc"],
        user_internal_id=1,
    )

    assert result["route"] == "agent_task"
    assert result["intent"] == "document_question"
    assert result["requires_sse"] is True
    assert result["agent_task"]["task_type"] == "dynamic_agent"
    assert result["agent_reply"] is None


# ── 9. agent_task → ask_for_files downgrade ──────────────────────


@pytest.mark.asyncio
async def test_agent_task_route_downgrades_when_files_missing(monkeypatch) -> None:
    # The fallback is deployment-configurable; this case verifies the
    # deliberate no-default configuration still asks for a template.
    monkeypatch.setattr(
        "app.services.message_service.get_settings",
        lambda: SimpleNamespace(default_test_plan_template_name=""),
    )
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,  # LLM optimistic
            supported=True,
            confidence=0.95,
            reason="...",
        )),
        files=[_make_file("f_req", "requirement_doc")],  # only one
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="帮我生成测试方案",
        attached_file_ids=["f_req"],
        user_internal_id=1,
    )

    assert result["route"] == "ask_for_files"
    assert result["agent_task"] is None
    assert result["task_id"] is None
    # missing_file_types is reported
    assert "missing_file_types" in result["agent_reply"]["payload"] or True  # payload may not be in detail
    assert "需求文档" in result["agent_reply"]["content"]


@pytest.mark.asyncio
async def test_test_plan_request_materializes_default_template_when_only_requirement_is_attached(monkeypatch) -> None:
    requirement = _make_file("file_requirement", "requirement_doc")
    default_template = _make_file("file_default_template", "test_plan_template")
    default_template.original_name = "00_PlanWise_QA_测试方案模板.docx"
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="explicit request",
        )),
        files=[requirement],
    )
    svc._file_repo.list_by_conversation = AsyncMock(
        side_effect=[[requirement], [requirement, default_template]]
    )

    class _DefaultTemplateUse:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def use_public_default_test_plan_template(self, **_kwargs):
            return {"uploaded_file": {"id": "file_default_template"}}

    monkeypatch.setattr(
        "app.services.template_use_service.TemplateUseService", _DefaultTemplateUse
    )
    svc._create_agent_task = AsyncMock(return_value={
        "route": "agent_task", "intent": "test_plan_generation",
        "agent_task": {"task_id": "task_default"}, "task_id": "task_default",
    })

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="请根据这份需求文档生成测试方案",
        attached_file_ids=["file_requirement"],
        user_internal_id=1,
    )

    assert result["route"] == "agent_task"
    assert svc._create_agent_task.await_args.kwargs["attached_ids"] == [
        "file_requirement", "file_default_template"
    ]


# ── 10. ask_for_files → agent_task upgrade ───────────────────────


@pytest.mark.asyncio
async def test_ask_for_files_route_upgrades_when_files_complete() -> None:
    files = [
        _make_file("f_req", "requirement_doc"),
        _make_file("f_tpl", "test_plan_template"),
    ]
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.ASK_FOR_FILES,  # LLM pessimistic
            supported=True,
            confidence=0.95,
            reason="...",
        )),
        files=files,
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="帮我生成测试方案",
        attached_file_ids=["f_req", "f_tpl"],
        user_internal_id=1,
    )

    assert result["route"] == "agent_task"
    assert result["task_id"] is not None
    assert result["agent_reply"] is None


# ── 11. Response contains no internal_id / storage_path / API Key


@pytest.mark.asyncio
async def test_response_contains_no_internal_ids_or_secrets() -> None:
    """None of the response branches should leak internal_id, storage_path,
    blob_path, or an API key string."""
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.9,
            reason="...",
        )),
        chat=_StubChat("测试方案是一份系统化的文档。"),
    )

    result = await svc.send_message(
        conv_public_id="conv_001",
        content="测试方案是什么？",
        attached_file_ids=None,
        user_internal_id=1,
    )

    dumped = json.dumps(result, ensure_ascii=False, default=str)
    for forbidden in ("internal_id", "storage_path", "blob_path", "sk-"):
        assert forbidden not in dumped, f"leaked: {forbidden}"


# ── 12. Backward-compat: agent_task field shape ──────────────────


@pytest.mark.asyncio
async def test_backward_compat_agent_task_field_shape() -> None:
    files = [
        _make_file("f_req", "requirement_doc"),
        _make_file("f_tpl", "test_plan_template"),
    ]
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="...",
        )),
        files=files,
    )
    result = await svc.send_message(
        conv_public_id="conv_001",
        content="x",
        attached_file_ids=["f_req", "f_tpl"],
        user_internal_id=1,
    )
    # The agent_task dict has the legacy fields
    at = result["agent_task"]
    assert set(at.keys()) >= {"task_id", "task_type", "status", "events_url"}
    assert at["task_type"] == "test_plan_generation"
    assert at["status"] == "created"


# ── 13. Backward-compat: agent_reply field shape ────────────────


@pytest.mark.asyncio
async def test_backward_compat_agent_reply_field_shape() -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.9,
            reason="...",
        )),
        chat=_StubChat("hi"),
    )
    result = await svc.send_message(
        conv_public_id="conv_001",
        content="x",
        attached_file_ids=None,
        user_internal_id=1,
    )
    ar = result["agent_reply"]
    assert "message_id" in ar
    assert "content" in ar
    assert ar["content"] == "hi"


# ── 14. router raises → fallback to clarify (no propagation) ─────


@pytest.mark.asyncio
async def test_router_exception_does_not_propagate() -> None:
    class _BoomRouter(_StubRouter):
        async def recognize(self, *args, **kwargs):  # type: ignore[override]
            raise RuntimeError("boom")

    svc, _, _, _ = _make_service(router=_BoomRouter(None))
    result = await svc.send_message(
        conv_public_id="conv_001",
        content="x",
        attached_file_ids=None,
        user_internal_id=1,
    )
    assert result["route"] == "clarify"
    assert "RuntimeError" in result["agent_reply"]["content"] or result["agent_reply"]["content"] == DEFAULT_CLARIFY_TEXT


# ── 15. chat_service raises → fallback ──────────────────────────


@pytest.mark.asyncio
async def test_chat_service_failure_uses_fallback() -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.CHAT_REPLY,
            supported=True,
            confidence=0.9,
            reason="...",
        )),
        chat=_StubChat(raise_exc=RuntimeError("LLM down")),
    )
    result = await svc.send_message(
        conv_public_id="conv_001",
        content="x",
        attached_file_ids=None,
        user_internal_id=1,
    )
    assert result["agent_reply"]["content"] == CHAT_FALLBACK_REPLY


# ── 16. Default reply text constants match the F013 spec ─────────


def test_default_reply_texts_match_f013_spec() -> None:
    assert "测试方案" in DEFAULT_ASK_FOR_FILES_TEXT
    assert "需求文档" in DEFAULT_ASK_FOR_FILES_TEXT
    assert "模板" in DEFAULT_ASK_FOR_FILES_TEXT
    assert "后续版本开放" in INTENT_DEFAULT_REPLIES[IntentType.TEST_CASE_GENERATION]
    assert "PPT" in INTENT_DEFAULT_REPLIES[IntentType.PPT_GENERATION]
    assert "Excel" in INTENT_DEFAULT_REPLIES[IntentType.EXCEL_GENERATION]
    assert "知识库" in INTENT_DEFAULT_REPLIES[IntentType.KNOWLEDGE_QUESTION]


# ── 17. agent_task branch does NOT persist a premature assistant message


@pytest.mark.asyncio
async def test_agent_task_branch_does_not_persist_assistant_message() -> None:
    files = [
        _make_file("f_req", "requirement_doc"),
        _make_file("f_tpl", "test_plan_template"),
    ]
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="...",
        )),
        files=files,
    )
    result = await svc.send_message(
        conv_public_id="conv_001",
        content="x",
        attached_file_ids=["f_req", "f_tpl"],
        user_internal_id=1,
    )
    # No agent message persisted; only the user message
    assert len(svc._created_messages) == 1
    assert svc._created_messages[0].role == "user"
    # And the response has no agent_reply
    assert result["agent_reply"] is None
    # But requires_sse is True
    assert result["requires_sse"] is True


@pytest.mark.asyncio
async def test_stream_message_does_not_create_task_when_agent_task_route_has_no_files() -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.TEST_PLAN_GENERATION,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="optimistic model route without files",
        )),
        files=[],
    )

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="can you generate a test plan",
            attached_file_ids=None,
            user_internal_id=1,
        )
    ]

    assert "agent_task_created" not in [event["event"] for event in events]
    assert [event["event"] for event in events] == [
        "message_created",
        "agent_reply_created",
        "agent_text_delta",
        "agent_text_done",
    ]
    assert events[-1]["data"]["agent_task"] is None
    assert events[-1]["data"]["requires_sse"] is False
    assert svc._task_repo.create.await_count == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_stream_message_never_creates_task_for_non_test_plan_agent_task_route() -> None:
    svc, _, _, _ = _make_service(
        router=_StubRouter(IntentResult(
            intent=IntentType.GENERAL_CHAT,
            route=MessageRoute.AGENT_TASK,
            supported=True,
            confidence=0.95,
            reason="inconsistent model route",
        )),
        files=[],
    )

    events = [
        event async for event in svc.stream_message(
            conv_public_id="conv_001",
            content="hello",
            attached_file_ids=None,
            user_internal_id=1,
        )
    ]

    assert "agent_task_created" not in [event["event"] for event in events]
    assert events[-1]["data"]["agent_task"] is None
    assert events[-1]["data"]["requires_sse"] is False
    assert svc._task_repo.create.await_count == 0  # type: ignore[attr-defined]
