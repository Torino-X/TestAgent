from __future__ import annotations

from app.agent.request_understanding import RequestClass, RequestUnderstandingResult
from app.agent.retrieval_planner import KnowledgeMode, RetrievalPlanner


def _understanding(**updates) -> RequestUnderstandingResult:
    data = {
        "request_class": RequestClass.CHAT,
        "target_capability": "general_chat",
        "operation": "qa",
        "knowledge_scope_hint": "general",
        "enterprise_knowledge_likelihood": 0.0,
    }
    data.update(updates)
    return RequestUnderstandingResult(**data)


def test_auto_generic_question_keeps_maas_off() -> None:
    plan = RetrievalPlanner().plan(
        "什么是 Redis？",
        _understanding(),
        knowledge_mode=KnowledgeMode.AUTO,
    )

    assert plan.maas == "off"
    assert plan.current_attachments == "off"
    assert plan.factual_source_policy == "general_model_allowed"


def test_auto_enterprise_question_uses_maas_auto() -> None:
    plan = RetrievalPlanner().plan(
        "公司知识库里的 Redis 连接池规范是什么？",
        _understanding(
            target_capability="enterprise_knowledge_qa",
            knowledge_scope_hint="enterprise",
            enterprise_knowledge_likelihood=0.9,
        ),
        knowledge_mode=KnowledgeMode.AUTO,
    )

    assert plan.maas == "auto"
    assert plan.factual_source_policy == "prefer_maas_when_relevant"


def test_strict_mode_requires_maas() -> None:
    plan = RetrievalPlanner().plan(
        "Redis 连接池规范是什么？",
        _understanding(),
        knowledge_mode=KnowledgeMode.MAAS_STRICT,
    )

    assert plan.maas == "required"
    assert plan.factual_source_policy == "maas_only"


def test_strict_mode_allows_explicit_current_attachments() -> None:
    plan = RetrievalPlanner().plan(
        "结合我刚上传的文档和公司规范说明测试范围",
        _understanding(attachment_context=True),
        knowledge_mode=KnowledgeMode.MAAS_STRICT,
        attached_file_ids=["file_req"],
    )

    assert plan.maas == "required"
    assert plan.current_attachments == "explicit_only"
    assert plan.factual_source_policy == "maas_and_explicit_attachment"
