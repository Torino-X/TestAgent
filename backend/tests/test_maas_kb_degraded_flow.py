from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent_runtime.dynamic_agent.schemas import DynamicPlanStep
from app.agent_runtime.dynamic_agent.tool_handlers import KnowledgeSearchHandler
from app.agent_runtime.graphs.dynamic_agent.graph import _understanding_summary
from app.common.prompt_builder import PromptBuilder


def test_prompt_builder_injects_maas_knowledge_context():
    prompt = PromptBuilder().build(
        user_prompt="生成测试方案",
        requirement_text="支付接口需要幂等。",
        template_structure={"headings": ["1 测试目标"], "table_fields": ["场景|预期"]},
        template_generation_config={
            "ai_fields": [{"field": "目标", "title": "测试目标", "level": 1}],
            "keep_sections": [],
            "manual_sections": [],
        },
        knowledge_context={
            "query": "支付接口幂等测试规范",
            "hit_count": 1,
            "degraded": False,
            "similar_projects": [
                {"name": "接口测试规范", "snippet": "幂等接口需要覆盖重复提交和并发请求。"}
            ],
        },
    )

    assert "公司知识库检索结果" in prompt
    assert "支付接口幂等测试规范" in prompt
    assert "幂等接口需要覆盖重复提交和并发请求" in prompt


@pytest.mark.asyncio
async def test_dynamic_knowledge_search_failure_degrades_and_continues():
    class _Adapter:
        async def execute(self, **_kwargs):
            return {
                "success": False,
                "summary": "KnowledgeSearchTool failed.",
                "warnings": ["not configured"],
                "error": {
                    "code": "KNOWLEDGE_NOT_CONFIGURED",
                    "message": "knowledge base not configured",
                },
                "data": None,
            }

    ctx = SimpleNamespace(task_internal_id=1, tool_adapter=_Adapter())
    handler = KnowledgeSearchHandler(ctx_runtime=ctx)
    step = DynamicPlanStep(
        step_id="kb",
        title="检索公司知识库",
        action_type="tool",
        capability_key="knowledge_search",
        success_criteria=["retrieval_attempted"],
    )
    result = await handler(
        step,
        {
            "goal": "根据需求文档和模板生成测试方案",
            "retrieval_plan_snapshot": {"maas": "auto", "query": "预约系统测试规范"},
        },
    )

    assert result["success"] is True
    assert result["error"] is None
    assert result["data"]["degraded"] is True
    assert result["data"]["error_code"] == "KNOWLEDGE_NOT_CONFIGURED"


def test_understanding_summary_recovers_test_plan_goal_from_user_instruction():
    summary = _understanding_summary(
        {
            "user_instruction": "帮我根据这份PRD需求文档和模板生成测试方案",
            "target_capability": "test_plan_generation",
            "attachment_refs": [{"public_id": "file_1"}, {"public_id": "file_2"}],
        }
    )

    assert summary == "根据上传的需求文档和测试方案模板生成测试方案。"
