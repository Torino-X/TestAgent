from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.enums import IntentType, MessageRoute
from app.agent import intent_router
from app.agent.intent_router import IntentResult
from app.llm.task_profiles import INTENT_FALLBACK_JSON
from app.schemas.message import SendMessageRequest
from app.services.message_service import MessageService


def test_send_message_request_accepts_knowledge_mode_snapshot() -> None:
    body = SendMessageRequest(
        content="公司知识库里 Redis 规范是什么？",
        attached_file_ids=[],
        knowledge_mode_snapshot="MAAS_STRICT",
    )

    assert body.knowledge_mode_snapshot == "MAAS_STRICT"


def test_user_payload_builder_includes_knowledge_mode_snapshot() -> None:
    payload = MessageService._user_message_payload(["file_1"], "MAAS_STRICT")

    assert payload["attached_file_ids"] == ["file_1"]
    assert payload["knowledge_mode_snapshot"] == "MAAS_STRICT"


def test_task_context_includes_knowledge_mode_snapshot() -> None:
    context = {"intent": "test_plan_generation", "route": "agent_task"}
    out = MessageService._task_context_with_knowledge_mode(context, "MAAS_STRICT")

    assert out["knowledge_mode_snapshot"] == "MAAS_STRICT"
    assert context == {"intent": "test_plan_generation", "route": "agent_task"}


def test_intent_router_contract_does_not_generate_chat_body() -> None:
    prompt = intent_router._INTENT_SYSTEM_PROMPT

    assert "必须同时在 reply 字段中生成回复内容" not in prompt
    assert "reply 字段必须设为 null" in prompt
    assert '"reply":null' in INTENT_FALLBACK_JSON


@pytest.mark.asyncio
async def test_strict_maas_unavailable_returns_exact_text() -> None:
    svc = MessageService(MagicMock(), intent_router=MagicMock(), chat_service=MagicMock())
    svc._try_kb_direct_reply = AsyncMock(
        return_value=(
            None,
            {
                "attempted": True,
                "hit": False,
                "confidence": "low",
                "reason": "kb_unavailable:COMPANY_KB_UNAVAILABLE",
                "error_code": "COMPANY_KB_UNAVAILABLE",
            },
            SimpleNamespace(error_code="COMPANY_KB_UNAVAILABLE", context={}),
        )
    )
    intent = IntentResult(
        intent=IntentType.KNOWLEDGE_QUESTION,
        route=MessageRoute.CHAT_REPLY,
        supported=True,
        confidence=0.9,
        reason="stub",
    )

    reply, meta, _ = await svc._strict_maas_reply(
        user_internal_id=1,
        content="公司规范是什么？",
        intent_result=intent,
    )

    assert reply == "公司知识库当前不可用，请稍后重试。"
    assert meta["strict"] is True


@pytest.mark.asyncio
async def test_strict_maas_no_hit_returns_exact_text() -> None:
    svc = MessageService(MagicMock(), intent_router=MagicMock(), chat_service=MagicMock())
    svc._try_kb_direct_reply = AsyncMock(
        return_value=(
            None,
            {
                "attempted": True,
                "hit": False,
                "confidence": "low",
                "reason": "kb_low_fallback_llm",
                "error_code": None,
            },
            SimpleNamespace(error_code=None, context={}),
        )
    )
    intent = IntentResult(
        intent=IntentType.KNOWLEDGE_QUESTION,
        route=MessageRoute.CHAT_REPLY,
        supported=True,
        confidence=0.9,
        reason="stub",
    )

    reply, meta, _ = await svc._strict_maas_reply(
        user_internal_id=1,
        content="公司规范是什么？",
        intent_result=intent,
    )

    assert reply == "公司知识库中未检索到相关内容。"
    assert meta["strict"] is True
