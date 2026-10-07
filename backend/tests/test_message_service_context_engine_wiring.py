from __future__ import annotations

from unittest.mock import MagicMock

from app.services import message_service


def test_message_service_wires_raw_invoker_only_to_chat_runtime(monkeypatch):
    """Normal-chat construction must preserve both Context Engine roles.

    The bridge is used by chat/intent business calls.  The raw invoker is
    reserved for preflight compaction inside ChatLLMService.runtime_context;
    IntentRouter does not accept or need that extra constructor argument.
    """

    router_class = MagicMock(return_value=MagicMock())
    chat_class = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(message_service, "IntentRouter", router_class)
    monkeypatch.setattr(message_service, "ChatLLMService", chat_class)

    bridge = object()
    raw_context_engine_invoker = object()
    llm = MagicMock()

    message_service.MessageService(
        MagicMock(),
        llm_client=llm,
        context_llm_invoker=bridge,
        context_engine_invoker=raw_context_engine_invoker,
    )

    assert router_class.call_args.kwargs["context_llm_invoker"] is bridge
    assert "context_engine_invoker" not in router_class.call_args.kwargs
    assert chat_class.call_args.kwargs["context_llm_invoker"] is bridge
    assert (
        chat_class.call_args.kwargs["context_engine_invoker"]
        is raw_context_engine_invoker
    )
