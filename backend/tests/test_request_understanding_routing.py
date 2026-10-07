from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.capability_registry import CapabilityRegistry
from app.agent.capability_router import CapabilityDecisionRoute, CapabilityRouter
from app.agent.enums import IntentType, MessageRoute
from app.agent.intent_router import IntentResult
from app.agent.request_understanding import RequestClass, build_request_understanding
from app.services.message_service import MessageService


def _intent(intent: IntentType, route: MessageRoute) -> IntentResult:
    return IntentResult(
        intent=intent,
        route=route,
        supported=True,
        confidence=0.9,
        reason="stub",
    )


def test_open_domain_question_stays_general_chat() -> None:
    result = build_request_understanding("什么是 Redis？")
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.request_class == RequestClass.CHAT
    assert result.target_capability == "general_chat"
    assert decision.route == CapabilityDecisionRoute.CHAT
    assert decision.legacy_route == MessageRoute.CHAT_REPLY


def test_document_question_with_attachment_routes_to_document_qa_chat() -> None:
    result = build_request_understanding(
        "帮我总结这份需求文档",
        attached_file_ids=["file_req"],
    )
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.request_class == RequestClass.CHAT
    assert result.target_capability == "document_qa"
    assert result.attachment_context is True
    assert decision.route == CapabilityDecisionRoute.CHAT


def test_document_question_with_negated_test_case_generation_stays_document_qa() -> None:
    intent = _intent(IntentType.DOCUMENT_QUESTION, MessageRoute.UNSUPPORTED)

    result = build_request_understanding(
        "请基于当前项目上下文，列出已确认事实。先不要生成测试用例。",
        intent_result=intent,
    )
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.target_capability == "document_qa"
    assert result.operation == "qa"
    assert decision.route == CapabilityDecisionRoute.CHAT
    assert decision.legacy_route == MessageRoute.CHAT_REPLY


def test_document_analyze_with_docx_routes_to_dynamic_agent() -> None:
    file = SimpleNamespace(
        public_id="file_req",
        file_ext=".docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    result = build_request_understanding(
        "分析这个文档说了什么",
        files=[file],
        attached_file_ids=["file_req"],
    )
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.request_class == RequestClass.CHAT
    assert result.operation == "analyze"
    assert result.target_capability == "document_qa"
    assert result.attachment_file_exts == [".docx"]
    assert decision.route == CapabilityDecisionRoute.DYNAMIC_AGENT
    assert decision.execution_mode == "dynamic_agent"
    assert decision.legacy_route == MessageRoute.AGENT_TASK
    assert decision.capability is not None
    assert decision.capability.allow_dynamic_fallback is True


def test_document_summary_with_uploaded_docx_ext_without_dot_routes_dynamic_agent() -> None:
    file = SimpleNamespace(
        public_id="file_req",
        original_name="requirement.docx",
        file_ext="docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    intent = _intent(IntentType.DOCUMENT_QUESTION, MessageRoute.UNSUPPORTED)

    result = build_request_understanding(
        "summarize this document",
        intent_result=intent,
        files=[file],
        attached_file_ids=["file_req"],
    )
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.attachment_file_exts == [".docx"]
    assert result.operation == "summarize"
    assert decision.route == CapabilityDecisionRoute.DYNAMIC_AGENT
    assert decision.legacy_route == MessageRoute.AGENT_TASK


def test_enterprise_knowledge_question_routes_to_chat_not_unsupported() -> None:
    result = build_request_understanding("公司知识库里 Redis 连接池规范是什么？")
    decision = CapabilityRouter(CapabilityRegistry.default(maas_enabled=True)).route(result)

    assert result.target_capability == "enterprise_knowledge_qa"
    assert result.knowledge_scope_hint == "enterprise"
    assert decision.route == CapabilityDecisionRoute.CHAT
    assert decision.legacy_route == MessageRoute.CHAT_REPLY


def test_test_plan_generation_remains_agent_task() -> None:
    result = build_request_understanding(
        "根据这份 PRD 和模板生成测试方案",
        intent_result=_intent(IntentType.TEST_PLAN_GENERATION, MessageRoute.AGENT_TASK),
        attached_file_ids=["file_req", "file_tpl"],
    )
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.request_class == RequestClass.AGENT_TASK
    assert result.target_capability == "test_plan_generation"
    assert decision.route == CapabilityDecisionRoute.AGENT_TASK
    assert decision.execution_mode == "fixed_workflow"
    assert decision.legacy_route == MessageRoute.AGENT_TASK
    assert decision.capability is not None
    assert decision.capability.fixed_workflow == "test_plan/v3"
    assert decision.capability.allow_dynamic_fallback is False


def test_result_modification_routes_to_incremental_agent_task() -> None:
    result = build_request_understanding(
        "项目概述章节的内容太少了，需要更加丰富，起码达到100字才可以",
        intent_result=_intent(IntentType.RESULT_MODIFICATION, MessageRoute.AGENT_TASK),
    )
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.request_class == RequestClass.AGENT_TASK
    assert result.operation == "modify"
    assert result.target_capability == "test_plan_incremental"
    assert result.task_type == "incremental_test_plan"
    assert decision.route == CapabilityDecisionRoute.AGENT_TASK
    assert decision.legacy_route == MessageRoute.AGENT_TASK


def test_generation_capability_disabled_returns_capability_unavailable() -> None:
    result = build_request_understanding(
        "请生成测试用例",
        intent_result=_intent(IntentType.TEST_CASE_GENERATION, MessageRoute.UNSUPPORTED),
    )
    decision = CapabilityRouter(CapabilityRegistry.default(test_case_enabled=False)).route(result)

    assert result.request_class == RequestClass.AGENT_TASK
    assert result.target_capability == "test_case_generation"
    assert decision.route == CapabilityDecisionRoute.CAPABILITY_UNAVAILABLE
    assert decision.legacy_route == MessageRoute.UNSUPPORTED


def test_concept_question_about_test_case_is_not_generation() -> None:
    result = build_request_understanding("测试用例是什么？")
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.request_class == RequestClass.CHAT
    assert result.target_capability == "general_chat"
    assert decision.legacy_route == MessageRoute.CHAT_REPLY


def test_existing_task_action_is_preserved() -> None:
    ctx = SimpleNamespace(latest_task_summary=SimpleNamespace(status="waiting_user_confirm"))
    result = build_request_understanding(
        "确认章节策略",
        intent_result=_intent(IntentType.RESULT_MODIFICATION, MessageRoute.EXISTING_TASK_ACTION),
        intent_context=ctx,
    )
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.request_class == RequestClass.TASK_ACTION
    assert decision.route == CapabilityDecisionRoute.TASK_ACTION
    assert decision.legacy_route == MessageRoute.EXISTING_TASK_ACTION


def test_degraded_understanding_falls_back_to_general_chat() -> None:
    result = build_request_understanding("", degraded=True, reason="parser_error")
    decision = CapabilityRouter(CapabilityRegistry.default()).route(result)

    assert result.degraded is True
    assert result.request_class == RequestClass.CHAT
    assert result.target_capability == "general_chat"
    assert decision.legacy_route == MessageRoute.CHAT_REPLY


def test_message_service_request_understanding_recovers_document_summary_from_clarify(
    caplog,
) -> None:
    caplog.set_level(logging.WARNING, logger="app.services.message_service")
    legacy_clarify = IntentResult(
        intent=IntentType.UNKNOWN,
        route=MessageRoute.CLARIFY,
        supported=False,
        confidence=0.0,
        reason="intent_router_llm_error",
        reply_message="clarify",
    )
    result = MessageService._apply_request_understanding_routing(
        MessageService.__new__(MessageService),
        "帮我总结这个文档",
        legacy_clarify,
        files=[],
        attached_ids=["file_req"],
    )

    assert result.intent == IntentType.DOCUMENT_QUESTION
    assert result.route == MessageRoute.CHAT_REPLY
    assert result.supported is True
    assert result.reply_message is None
    assert result.extra_payload["request_understanding"]["target_capability"] == "document_qa"
    assert any(
        "ROUTE_OVERRIDE | component=message_service.request_understanding"
        in record.message
        and "from_route=clarify" in record.message
        and "to_route=chat_reply" in record.message
        for record in caplog.records
    )


def test_message_service_routes_project_document_question_with_sources_to_chat() -> None:
    legacy_unsupported = _intent(IntentType.DOCUMENT_QUESTION, MessageRoute.UNSUPPORTED)

    result = MessageService._apply_request_understanding_routing(
        MessageService.__new__(MessageService),
        "请基于当前项目上下文，列出已确认事实。先不要生成测试用例。",
        legacy_unsupported,
        files=[],
        attached_ids=[],
        project_source_available=True,
    )

    assert result.intent == IntentType.DOCUMENT_QUESTION
    assert result.route == MessageRoute.CHAT_REPLY
    assert result.supported is True
    assert result.extra_payload["request_understanding"]["target_capability"] == "document_qa"


def test_message_service_keeps_standalone_document_question_unsupported() -> None:
    legacy_unsupported = _intent(IntentType.DOCUMENT_QUESTION, MessageRoute.UNSUPPORTED)

    result = MessageService._apply_request_understanding_routing(
        MessageService.__new__(MessageService),
        "请基于当前项目上下文，列出已确认事实。先不要生成测试用例。",
        legacy_unsupported,
        files=[],
        attached_ids=[],
        project_source_available=False,
    )

    assert result.route == MessageRoute.UNSUPPORTED
    assert result.intent == IntentType.DOCUMENT_QUESTION


@pytest.mark.asyncio
async def test_message_service_project_source_gate_requires_a_current_source(monkeypatch) -> None:
    from app.repositories.project_source_repository import ProjectSourceRepository

    service = MessageService.__new__(MessageService)
    service._session = object()
    conv = SimpleNamespace(project_id=9, public_id="conv_project")
    intent = _intent(IntentType.DOCUMENT_QUESTION, MessageRoute.UNSUPPORTED)
    list_sources = AsyncMock(return_value=[(SimpleNamespace(is_current=False), object())])
    monkeypatch.setattr(ProjectSourceRepository, "list_with_files", list_sources)

    assert await service._project_has_sources(
        conv=conv,
        user_internal_id=1,
        intent_result=intent,
    ) is False

    list_sources.return_value = [(SimpleNamespace(is_current=True), object())]
    assert await service._project_has_sources(
        conv=conv,
        user_internal_id=1,
        intent_result=intent,
    ) is True

def test_message_service_preserves_result_modification_agent_task_route() -> None:
    routed = MessageService._apply_request_understanding_routing(
        MessageService.__new__(MessageService),
        "项目概述章节的内容太少了，需要更加丰富，起码达到100字才可以",
        _intent(IntentType.RESULT_MODIFICATION, MessageRoute.AGENT_TASK),
        files=[],
        attached_ids=[],
    )

    assert routed.intent == IntentType.RESULT_MODIFICATION
    assert routed.route == MessageRoute.AGENT_TASK
    assert routed.supported is True
    assert routed.extra_payload["request_understanding"]["target_capability"] == (
        "test_plan_incremental"
    )
    assert routed.extra_payload["capability_routing"]["legacy_route"] == (
        MessageRoute.AGENT_TASK.value
    )


def test_memory_rule_statement_cannot_be_upgraded_to_agent_task() -> None:
    """An explicit remember/rule statement is chat, not a test-plan request."""
    content = (
        "我们需要定义下规则：以后在生成测试方案时，不允许使用捏造数据；"
        "每章不超过 50 字。记住这些规则。"
    )
    routed = MessageService._apply_request_understanding_routing(
        MessageService.__new__(MessageService),
        content,
        _intent(IntentType.GENERAL_CHAT, MessageRoute.CHAT_REPLY),
        files=[],
        attached_ids=[],
    )

    assert routed.intent == IntentType.GENERAL_CHAT
    assert routed.route == MessageRoute.CHAT_REPLY
