from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.chat_llm_service import ChatLLMService


@pytest.mark.asyncio
async def test_stream_reply_uses_context_bridge_when_mig_chat_enabled():
    async def _bridge_stream(**kwargs):
        _bridge_stream.kwargs = kwargs
        yield "bridge "
        yield "reply"

    llm = MagicMock()
    llm.stream_with_profile = MagicMock()
    bridge = SimpleNamespace(
        available=True,
        stream=_bridge_stream,
        generate=AsyncMock(return_value=SimpleNamespace(value="not used")),
    )
    resolver = SimpleNamespace(evaluate=MagicMock(return_value=True))
    session_factory = object()
    context_engine_invoker = object()
    chat_context = SimpleNamespace(conversation_id="123", current_message_id="456")

    service = ChatLLMService(
        llm,
        context_llm_invoker=bridge,
        context_engine_invoker=context_engine_invoker,
        task_flag_resolver=resolver,
        session_factory=session_factory,
    )

    chunks = [
        chunk
        async for chunk in service.stream_reply(
            "how many questions?", chat_context=chat_context, user_id=7
        )
    ]

    assert chunks == ["bridge ", "reply"]
    llm.stream_with_profile.assert_not_called()
    bridge.generate.assert_not_called()
    kwargs = _bridge_stream.kwargs
    assert kwargs["call_site"] == "chat.reply"
    assert kwargs["current_goal"] == "how many questions?"
    assert kwargs["user_content"] == "how many questions?"
    assert kwargs["current_user_message_id"] == 456
    assert kwargs["runtime_context"].conversation_internal_id == 123
    assert kwargs["runtime_context"].session_factory is session_factory
    assert kwargs["runtime_context"].context_llm_invoker is context_engine_invoker


@pytest.mark.asyncio
async def test_stream_reply_falls_back_to_context_bridge_generate_for_old_bridge():
    llm = MagicMock()
    bridge = SimpleNamespace(
        available=True,
        generate=AsyncMock(return_value=SimpleNamespace(value="ok")),
    )
    resolver = SimpleNamespace(evaluate=MagicMock(return_value=True))
    session_factory = object()
    chat_context = SimpleNamespace(
        conversation_id="123",
        current_message_id="456",
        conversation_summary="old summary",
        recent_messages=[
            SimpleNamespace(role="user", content="old question"),
            SimpleNamespace(role="assistant", content="old answer"),
        ],
        file_summaries=[],
        latest_task_summary=None,
    )

    service = ChatLLMService(
        llm,
        context_llm_invoker=bridge,
        task_flag_resolver=resolver,
        session_factory=session_factory,
    )

    chunks = [
        chunk
        async for chunk in service.stream_reply(
            "current question", chat_context=chat_context, user_id=7
        )
    ]

    assert chunks == ["ok"]
    kwargs = bridge.generate.await_args.kwargs
    assert kwargs["current_goal"] == "current question"
    assert kwargs["current_user_message_id"] == 456
    assert "old question" not in kwargs["current_goal"]
    assert "old answer" not in kwargs["user_content"]


@pytest.mark.asyncio
async def test_stream_reply_does_not_send_legacy_context_as_current_goal():
    async def _bridge_stream(**kwargs):
        _bridge_stream.kwargs = kwargs
        yield "ok"

    llm = MagicMock()
    bridge = SimpleNamespace(
        available=True,
        stream=_bridge_stream,
        generate=AsyncMock(return_value=SimpleNamespace(value="unused")),
    )
    resolver = SimpleNamespace(evaluate=MagicMock(return_value=True))
    session_factory = object()
    chat_context = SimpleNamespace(
        conversation_id="123",
        current_message_id="456",
        conversation_summary="old summary",
        recent_messages=[
            SimpleNamespace(role="user", content="old question"),
            SimpleNamespace(role="assistant", content="old answer"),
        ],
        file_summaries=[],
        latest_task_summary=None,
    )

    service = ChatLLMService(
        llm,
        context_llm_invoker=bridge,
        task_flag_resolver=resolver,
        session_factory=session_factory,
    )

    chunks = [
        chunk
        async for chunk in service.stream_reply(
            "current question", chat_context=chat_context, user_id=7
        )
    ]

    assert chunks == ["ok"]
    bridge.generate.assert_not_called()
    kwargs = _bridge_stream.kwargs
    assert kwargs["current_goal"] == "current question"
    assert kwargs["current_user_message_id"] == 456
    assert "old question" not in kwargs["current_goal"]
    assert "old answer" not in kwargs["user_content"]


@pytest.mark.asyncio
async def test_generate_reply_reports_safe_bridge_empty_result_diagnostic():
    llm = MagicMock()
    bridge = SimpleNamespace(
        available=True,
        generate=AsyncMock(return_value=None),
    )
    resolver = SimpleNamespace(evaluate=MagicMock(return_value=True))
    service = ChatLLMService(
        llm,
        context_llm_invoker=bridge,
        task_flag_resolver=resolver,
        session_factory=object(),
    )
    diagnostics: dict = {}

    reply = await service.generate_reply("ordinary chat", diagnostics=diagnostics)

    assert reply
    assert diagnostics["chat_context_engine"] == {
        "path": "context_bridge",
        "outcome": "bridge_empty_result",
    }


@pytest.mark.asyncio
async def test_generate_reply_reports_safe_context_engine_error_code():
    from app.context_engine.errors import ContextEngineFailure, raise_engine_error

    async def _raise_context_failure(**kwargs):
        raise_engine_error(
            code="context.profile.no_call_site_mapping",
            detail="safe internal test detail",
            stage="profile",
            retryable=False,
        )

    llm = MagicMock()
    bridge = SimpleNamespace(available=True, generate=_raise_context_failure)
    resolver = SimpleNamespace(evaluate=MagicMock(return_value=True))
    service = ChatLLMService(
        llm,
        context_llm_invoker=bridge,
        task_flag_resolver=resolver,
        session_factory=object(),
    )
    diagnostics: dict = {}

    reply = await service.generate_reply("ordinary chat", diagnostics=diagnostics)

    assert reply
    assert diagnostics["chat_context_engine"] == {
        "path": "context_bridge",
        "outcome": "bridge_exception",
        "exception_type": ContextEngineFailure.__name__,
        "error_code": "context.profile.no_call_site_mapping",
        "stage": "profile",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_document_qa_does_not_call_model_when_evidence_is_not_ready():
    from app.context_engine.errors import raise_engine_error

    async def _raise_document_indexing(**kwargs):
        raise_engine_error(
            code="context.source.document_indexing",
            detail="document index is not ready",
            stage="source",
            retryable=True,
        )

    llm = MagicMock()
    bridge = SimpleNamespace(available=True, generate=_raise_document_indexing)
    service = ChatLLMService(llm, context_llm_invoker=bridge, session_factory=object())
    diagnostics: dict = {}

    reply = await service.generate_reply(
        "刚才上传的需求文档讲了什么？",
        diagnostics=diagnostics,
        call_site="document.qa",
    )

    assert "正在解析" in reply
    assert diagnostics["document_grounding"] == {
        "status": "indexing",
        "error_code": "context.source.document_indexing",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "expected_status"),
    [
        ("context.source.document_no_evidence", "no_evidence"),
        ("context.source.document_ambiguous", "ambiguous"),
        ("context.source.document_full_content_too_large", "full_content_too_large"),
        ("context.source.document_retrieval_error", "retrieval_degraded"),
    ],
)
async def test_document_qa_blocks_provider_for_unanswered_or_degraded_evidence(
    error_code: str,
    expected_status: str,
):
    from app.context_engine.errors import raise_engine_error

    async def _raise_document_grounding_failure(**kwargs):
        raise_engine_error(
            code=error_code,
            detail="safe test failure",
            stage="source",
            retryable=error_code.endswith("retrieval_error"),
        )

    llm = MagicMock()
    bridge = SimpleNamespace(available=True, generate=_raise_document_grounding_failure)
    service = ChatLLMService(llm, context_llm_invoker=bridge, session_factory=object())
    diagnostics: dict = {}

    reply = await service.generate_reply(
        "question about an uploaded document",
        diagnostics=diagnostics,
        call_site="document.qa",
    )

    assert reply
    assert diagnostics["document_grounding"] == {
        "status": expected_status,
        "error_code": error_code,
    }
    assert llm.mock_calls == []
