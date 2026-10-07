"""F026 — KB direct-answer for KNOWLEDGE_QUESTION.

Tests the new gate behaviour in
``MessageService._kb_trigger_eligible`` and the wider
``KnowledgeAnswerService.decide`` route-eligibility check.

The tests use a stubbed ``MessageService`` and patch
``KnowledgeAnswerService.decide`` so the underlying KB retrieval /
threshold logic is not exercised — only the wiring at the chat /
intent boundary is.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.enums import IntentType, MessageRoute
from app.agent.intent_router import IntentResult
from app.services.chat_llm_service import ChatLLMService
from app.services.knowledge_answer_service import (
    KnowledgeAnswerDecision,
    KnowledgeAnswerService,
)
from app.services.message_service import MessageService


def _intent_result(intent: IntentType, route: MessageRoute) -> IntentResult:
    return IntentResult(
        intent=intent,
        route=route,
        supported=True,
        confidence=0.9,
        reason="stub",
    )


def _make_session() -> MagicMock:
    session = MagicMock(spec=AsyncSession)
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    return session


def _make_service(intent_result: IntentResult) -> MessageService:
    session = _make_session()
    svc = MessageService(
        session,
        intent_router=MagicMock(),
        chat_service=MagicMock(spec=ChatLLMService),
    )
    return svc


@pytest.mark.parametrize(
    "intent,route,expected",
    [
        (IntentType.GENERAL_CHAT, MessageRoute.CHAT_REPLY, True),
        (IntentType.KNOWLEDGE_QUESTION, MessageRoute.UNSUPPORTED, True),
        (
            IntentType.KNOWLEDGE_QUESTION,
            MessageRoute.CHAT_REPLY,
            True,
        ),  # also fine if router chose chat_reply
        (IntentType.TEST_PLAN_GENERATION, MessageRoute.AGENT_TASK, False),
        (IntentType.UNKNOWN, MessageRoute.CLARIFY, False),
        (IntentType.UNKNOWN, MessageRoute.ASK_FOR_FILES, False),
    ],
)
def test_kb_trigger_eligible(intent, route, expected):
    svc = _make_service(_intent_result(intent, route))
    result = svc._kb_trigger_eligible(_intent_result(intent, route))
    assert result is expected


@pytest.mark.asyncio
async def test_try_kb_direct_reply_returns_decision_for_chat_reply():
    """``_try_kb_direct_reply`` now exposes the decision object so the
    caller can reuse snippets for LLM polish."""
    intent = _intent_result(IntentType.GENERAL_CHAT, MessageRoute.CHAT_REPLY)
    svc = _make_service(intent)
    decision = KnowledgeAnswerDecision(
        should_call_llm=True,
        confidence="medium",
        context={"knowledge_snippets": [{"doc_name": "doc1", "content": "片段A"}]},
        source_attribution=[{"doc_name": "doc1", "score": 0.42}],
        reason="medium_confidence",
    )

    fake_svc = MagicMock(spec=KnowledgeAnswerService)
    fake_svc.decide = AsyncMock(return_value=decision)
    with patch(
        "app.services.message_service.KnowledgeAnswerService",
        return_value=fake_svc,
    ):
        reply, meta, out_decision = await svc._try_kb_direct_reply(
            user_internal_id=1,
            content="公司测试用例评审制度是什么？",
            intent_result=intent,
        )

    assert reply is None  # medium → still call LLM
    assert meta["attempted"] is True
    assert meta["hit"] is False
    assert meta["confidence"] == "medium"
    assert out_decision is decision


@pytest.mark.asyncio
async def test_try_kb_direct_reply_returns_direct_answer_for_high_confidence():
    intent = _intent_result(IntentType.KNOWLEDGE_QUESTION, MessageRoute.UNSUPPORTED)
    svc = _make_service(intent)
    decision = KnowledgeAnswerDecision(
        should_call_llm=False,
        confidence="high",
        direct_answer="公司测试用例评审制度：每周三下午 4 点召开...",
        context={"knowledge_snippets": []},
        source_attribution=[{"doc_name": "评审制度V3.docx", "score": 0.88}],
        reason="kb_high_confidence",
    )

    fake_svc = MagicMock(spec=KnowledgeAnswerService)
    fake_svc.decide = AsyncMock(return_value=decision)
    with patch(
        "app.services.message_service.KnowledgeAnswerService",
        return_value=fake_svc,
    ):
        reply, meta, out_decision = await svc._try_kb_direct_reply(
            user_internal_id=1,
            content="公司测试用例评审制度是什么？",
            intent_result=intent,
        )

    assert reply is not None
    assert "评审制度" in reply
    assert meta["hit"] is True
    assert meta["confidence"] == "high"
    assert out_decision is decision


@pytest.mark.asyncio
async def test_try_kb_direct_reply_skips_for_unsupported_intent():
    """Non-KB-intent + non-chat_reply must NOT consult the KB."""
    intent = _intent_result(IntentType.TEST_PLAN_GENERATION, MessageRoute.AGENT_TASK)
    svc = _make_service(intent)
    reply, meta, decision = await svc._try_kb_direct_reply(
        user_internal_id=1,
        content="帮我生成测试方案",
        intent_result=intent,
    )
    assert reply is None
    assert meta is None
    assert decision is None


@pytest.mark.asyncio
async def test_maybe_inject_kb_context_copies_snippets():
    """``_maybe_inject_kb_context`` must shallow-copy snippets onto a new
    ChatContext so the LLM polish path sees them."""
    from app.schemas.context import ChatContext

    ctx = ChatContext(conversation_id="conv_001")
    decision = SimpleNamespace(
        context={
            "knowledge_snippets": [
                {"doc_name": "docA", "content": "片段X", "score": 0.6},
                {"doc_name": "docB", "content": "片段Y", "score": 0.5},
            ]
        }
    )
    svc = _make_service(_intent_result(IntentType.GENERAL_CHAT, MessageRoute.CHAT_REPLY))
    new_ctx = svc._maybe_inject_kb_context(ctx, decision)
    assert new_ctx is not ctx
    assert new_ctx.knowledge_snippets is not decision.context["knowledge_snippets"]
    assert new_ctx.knowledge_source_count == 2
    assert new_ctx.knowledge_snippets[0]["doc_name"] == "docA"


@pytest.mark.asyncio
async def test_maybe_inject_kb_context_returns_original_when_no_snippets():
    from app.schemas.context import ChatContext

    ctx = ChatContext(conversation_id="conv_001")
    decision = SimpleNamespace(context={})
    svc = _make_service(_intent_result(IntentType.GENERAL_CHAT, MessageRoute.CHAT_REPLY))
    new_ctx = svc._maybe_inject_kb_context(ctx, decision)
    assert new_ctx is ctx


@pytest.mark.asyncio
async def test_knowledge_answer_service_decide_accepts_unsupported_route():
    """F026 widening: KNOWLEDGE_QUESTION uses route=UNSUPPORTED, so the
    early-eligibility check must NOT short-circuit that route."""
    svc = KnowledgeAnswerService.__new__(KnowledgeAnswerService)
    svc._session = MagicMock(spec=AsyncSession)
    svc._retrieval = MagicMock()
    # Stub the per-user toggle check so we get past it without a real repo
    with patch(
        "app.services.knowledge_answer_service.KnowledgeConfigRepository"
    ) as repo_cls:
        repo = repo_cls.return_value
        repo.get_for_user = AsyncMock(return_value=None)
        decision = await svc.decide(
            user_internal_id=1,
            query="公司测试用例评审制度是什么？",
            route=MessageRoute.UNSUPPORTED,
        )
    # route=UNSUPPORTED now reaches the retrieval step; the decision
    # should not be the "不做 KB 直返" placeholder.
    assert "不做 KB 直返" not in decision.reason