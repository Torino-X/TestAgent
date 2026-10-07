"""BUG FIX 2026-08-18 (keep_sections):

旧版 ``_build_searchable_text`` 对 ``section_package.keep_sections`` 用
``isinstance(section, dict)`` 过滤,而 ``keep_sections`` 实际是
``list[str]``(来自 ``template_prompt_preview_service.build_generation_config``
line 62),导致 mode="keep" 的章节标题**永远不进 searchable**,被
``_check_completeness`` 误判为"缺失必含章节"。

修复后 ``_build_searchable_text`` 兼容 ``list[str]`` 形态。
"""

from __future__ import annotations

import pytest

from app.agent.context import AgentContext
from app.tools.result_review_tool import ResultReviewTool


@pytest.fixture
def tool():
    return ResultReviewTool()


@pytest.fixture
def base_context():
    return AgentContext(
        task_id="task_keep_test",
        conversation_id="conv_keep_test",
        user_id="user_keep_test",
    )


def _ctx_with_keep(
    ctx: AgentContext,
    keep_titles: list[str],
    generated_titles: list[str],
) -> AgentContext:
    """构造 ``test_plan_content``:keep_sections 是 list[str](真实形态)。"""
    ctx.test_plan_content = {
        "section_package": {
            "generated_sections": [
                {"section_id": f"g{i}", "title": t, "content": "正文" * 30}
                for i, t in enumerate(generated_titles)
            ],
            "keep_sections": list(keep_titles),
            "manual_sections": [],
        },
    }
    return ctx


class TestKeepSectionsSearchable:
    """``_build_searchable_text`` 必须接受 ``list[str]`` 形态的 keep_sections。"""

    def test_list_str_keep_sections_do_not_show_as_missing(self, tool, base_context):
        """用户选择"保留原文"的章节不应被 _check_completeness 误判为缺失。"""
        _ctx_with_keep(
            base_context,
            keep_titles=["测试交付物", "修订记录", "风险分析"],
            generated_titles=["项目概述", "测试目标", "测试范围"],
        )
        result = tool._build_searchable_text(base_context.test_plan_content)
        assert "测试交付物" in result
        assert "修订记录" in result
        assert "风险分析" in result

    @pytest.mark.asyncio
    async def test_kept_titles_not_reported_as_missing(self, tool, base_context):
        """端到端:keep_sections 标题在,result["missing_sections"] 不含它们。"""
        _ctx_with_keep(
            base_context,
            keep_titles=["测试交付物", "修订记录", "风险分析"],
            generated_titles=["项目概述", "测试目标"],
        )
        # 不设 requirement_analysis,只测 DEFAULT_SECTIONS 不在 generated
        # 的情况,确保 keep 章节补全 searchable 后 _check_completeness
        # 不会把它们列入 missing_sections。
        base_context.requirement_analysis = None
        result = await tool.run({}, base_context)
        data = result["data"]
        missing = data.get("missing_sections", [])
        # 关键断言:3 个 keep 章节不应被列入缺失
        assert "测试交付物" not in missing
        assert "修订记录" not in missing
        assert "风险分析" not in missing

    def test_dict_keep_sections_still_work(self, tool, base_context):
        """backward-compat:keep_sections 为 list[dict] 时仍能正确处理。"""
        base_context.test_plan_content = {
            "section_package": {
                "generated_sections": [],
                "keep_sections": [
                    {"title": "测试交付物"},
                    {"title": "修订记录"},
                ],
                "manual_sections": [],
            },
        }
        result = tool._build_searchable_text(base_context.test_plan_content)
        assert "测试交付物" in result
        assert "修订记录" in result

    def test_empty_keep_sections_returns_empty_string(self, tool, base_context):
        """keep_sections 为空时,函数不报错。"""
        base_context.test_plan_content = {
            "section_package": {
                "generated_sections": [
                    {"title": "A", "content": "正文" * 30},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        }
        result = tool._build_searchable_text(base_context.test_plan_content)
        # 至少包含 generated 的标题
        assert "A" in result

    def test_mixed_dict_and_string_keep_sections(self, tool, base_context):
        """防御性测试:list 中 dict 和 str 混合时两者都进 searchable。"""
        base_context.test_plan_content = {
            "section_package": {
                "generated_sections": [],
                "keep_sections": [
                    "测试交付物",  # str
                    {"title": "修订记录"},  # dict
                ],
                "manual_sections": [],
            },
        }
        result = tool._build_searchable_text(base_context.test_plan_content)
        assert "测试交付物" in result
        assert "修订记录" in result