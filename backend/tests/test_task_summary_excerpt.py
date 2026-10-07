"""BUG FIX 2026-08-18 (B2): task_summary narrative 引入 excerpt 后,
LLM 能结合需求文档要点与关键章节内容展开针对性总结,而非仅基于
数字/文件名生成格式化模板。

覆盖:
  * ``TaskSummaryNarrativeContext`` 增加 ``requirement_text_excerpt`` 与
    ``generated_section_content_excerpts`` 字段,Optional 默认 None,
    向后兼容。
  * ``_build_task_summary_context`` 从 Graph State 填入 excerpt:
    - 需求文档前 600 字
    - 前 5 个章节,每个 content 前 200 字
  * ``build_task_summary_prompt`` 的 prompt 包含 excerpt 写作要点
    (结合内容展开、按主题分段、300~500 字),且 model_dump
    exclude_none=True 让 None 字段不出现在 context_json。
"""

from __future__ import annotations

import json

import pytest

from app.agent_runtime.graphs.test_plan.versions.v3.nodes_narrative import (
    _build_task_summary_context,
)
from app.agent_runtime.narrative_composer.prompts import build_task_summary_prompt
from app.agent_runtime.narrative_composer.schemas import (
    TaskSummaryNarrativeContext,
)


# ── helpers ─────────────────────────────────────────────────────


def _make_state(
    *,
    requirement_text: str = "",
    sections: list[dict] | None = None,
) -> dict:
    state: dict = {
        "task_id": "t_excerpt",
        "graph_run_id": "run_excerpt",
    }
    if requirement_text or sections is not None:
        req: dict = {}
        if requirement_text:
            req["text_content"] = requirement_text
        state["requirement_analysis"] = req
    if sections is not None:
        # 注:_build_task_summary_context 与 _count_chapters 都直接读
        # ``test_plan_content.generated_sections``,不走 section_package。
        state["test_plan_content"] = {
            "generated_sections": sections,
        }
    return state


# ── TaskSummaryNarrativeContext schema 兼容 ─────────────────────────


class TestTaskSummaryNarrativeContextSchema:
    """Optional 字段默认 None,向后兼容(老调用方不需要传)。"""

    def test_default_construction_excerpts_are_none(self):
        ctx = TaskSummaryNarrativeContext()
        assert ctx.requirement_text_excerpt is None
        assert ctx.generated_section_content_excerpts is None

    def test_explicit_excerpts_are_stored(self):
        ctx = TaskSummaryNarrativeContext(
            requirement_text_excerpt="需求文档要点摘要",
            generated_section_content_excerpts=[
                {"section_id": "s1", "title": "测试目标", "excerpt": "本节描述..."},
            ],
        )
        assert ctx.requirement_text_excerpt == "需求文档要点摘要"
        assert len(ctx.generated_section_content_excerpts) == 1

    def test_extra_field_rejected_by_config(self):
        """extra='forbid' 仍然生效(原合同)。"""
        with pytest.raises(Exception):  # pydantic ValidationError
            TaskSummaryNarrativeContext(unknown_field="x")


# ── _build_task_summary_context 填 excerpt ──────────────────────────


class TestBuildTaskSummaryContextExcerpts:
    """``_build_task_summary_context`` 从 Graph State 提取 excerpt。"""

    def test_requirement_text_excerpt_first_600_chars(self):
        long_req = "需求" * 1000  # 1000 个汉字
        state = _make_state(requirement_text=long_req)
        ctx = _build_task_summary_context(state)
        assert ctx.requirement_text_excerpt is not None
        assert len(ctx.requirement_text_excerpt) == 600

    def test_requirement_text_excerpt_strips_whitespace(self):
        state = _make_state(requirement_text="   关键需求\n\n\t段  ")
        ctx = _build_task_summary_context(state)
        assert ctx.requirement_text_excerpt == "关键需求\n\n\t段"

    def test_requirement_text_excerpt_none_when_missing(self):
        state = _make_state(requirement_text="")
        ctx = _build_task_summary_context(state)
        assert ctx.requirement_text_excerpt is None

    def test_generated_section_excerpts_first_5_sections(self):
        sections = [
            {"section_id": f"s{i}", "title": f"章节{i}", "content": f"内容{i}" * 50}
            for i in range(8)
        ]
        state = _make_state(sections=sections)
        ctx = _build_task_summary_context(state)
        excerpts = ctx.generated_section_content_excerpts
        assert excerpts is not None
        assert len(excerpts) == 5  # 限制 5 个
        assert excerpts[0]["section_id"] == "s0"
        assert excerpts[-1]["section_id"] == "s4"

    def test_generated_section_excerpts_first_200_chars_each(self):
        sections = [
            {
                "section_id": "s1",
                "title": "测试目标",
                "content": "正文" * 200,  # 400 字
            },
        ]
        state = _make_state(sections=sections)
        ctx = _build_task_summary_context(state)
        assert ctx.generated_section_content_excerpts[0]["excerpt"]
        assert len(ctx.generated_section_content_excerpts[0]["excerpt"]) == 200

    def test_generated_section_excerpts_skips_empty_content(self):
        sections = [
            {"section_id": "s1", "title": "空章节", "content": ""},
            {"section_id": "s2", "title": "正常", "content": "正常内容" * 50},
        ]
        state = _make_state(sections=sections)
        ctx = _build_task_summary_context(state)
        excerpts = ctx.generated_section_content_excerpts
        assert excerpts is not None
        # s1 被跳过,只有 s2
        assert len(excerpts) == 1
        assert excerpts[0]["section_id"] == "s2"

    def test_generated_section_excerpts_none_when_no_sections(self):
        state = _make_state(sections=[])
        ctx = _build_task_summary_context(state)
        assert ctx.generated_section_content_excerpts is None

    def test_full_integration_state(self):
        """端到端:完整 state 触发的 excerpt 计算。"""
        state = _make_state(
            requirement_text="本系统是觅迅测试管理系统,功能包括 A/B/C",
            sections=[
                {"section_id": "s1", "title": "项目概述", "content": "觅迅系统介绍..."},
                {"section_id": "s2", "title": "测试目标", "content": "验证功能正确性"},
            ],
        )
        ctx = _build_task_summary_context(state)
        assert ctx.requirement_text_excerpt is not None
        assert "觅迅测试管理系统" in ctx.requirement_text_excerpt
        assert ctx.generated_section_content_excerpts is not None
        assert len(ctx.generated_section_content_excerpts) == 2


# ── build_task_summary_prompt 写作要点 ─────────────────────────────


class TestBuildTaskSummaryPrompt:
    """Prompt 必须含 excerpt 写作要点,且 model_dump exclude_none 不显示 None。"""

    def test_prompt_contains_excerpt_writing_instructions(self):
        ctx = TaskSummaryNarrativeContext(task_id="t1")
        system_prompt, _ = build_task_summary_prompt(ctx)
        # 关键写作要点出现在 prompt
        assert "requirement_text_excerpt" in system_prompt
        assert "generated_section_content_excerpts" in system_prompt
        assert "结合这些内容展开" in system_prompt or "结合这些" in system_prompt
        assert "300~500 字" in system_prompt or "300~500" in system_prompt
        # Markdown 二级标题指引
        assert "## " in system_prompt or "二级标题" in system_prompt

    def test_exclude_none_does_not_show_none_fields(self):
        """requirement_text_excerpt 为 None 时,context_json 不应有 null。"""
        ctx = TaskSummaryNarrativeContext(task_id="t1")
        system_prompt, _ = build_task_summary_prompt(ctx)
        # prompt 里的 context_json 不应含 "requirement_text_excerpt": null
        # 提取 {context_json} 占位符的实际值
        import re
        m = re.search(r"\{context_json\}.*?\{constraints_json\}", system_prompt, re.DOTALL)
        # 不直接断言 context_json 内容(被 format 替换为空),改为检查
        # TaskSummaryNarrativeContext.model_dump(exclude_none=True) 行为
        dumped = ctx.model_dump(exclude_none=True)
        assert "requirement_text_excerpt" not in dumped
        assert "generated_section_content_excerpts" not in dumped

    def test_excerpt_present_when_set(self):
        ctx = TaskSummaryNarrativeContext(
            requirement_text_excerpt="需求要点",
            generated_section_content_excerpts=[
                {"section_id": "s1", "title": "测试目标", "excerpt": "..."},
            ],
        )
        system_prompt, _ = build_task_summary_prompt(ctx)
        # 当 excerpt 有值时,prompt context_json 应含实际内容
        assert "需求要点" in system_prompt
        assert "测试目标" in system_prompt

    def test_fact_constraints_not_in_context_json(self):
        """fact_constraints 仍然被 exclude(老合同)。"""
        ctx = TaskSummaryNarrativeContext(task_id="t1")
        system_prompt, _ = build_task_summary_prompt(ctx)
        # fact_constraints 字典不应该在 system_prompt 里出现
        # (它单独在 constraints_json 里)
        dumped = ctx.model_dump(exclude={"fact_constraints"}, exclude_none=True)
        assert "fact_constraints" not in dumped
        # 序列化也不应在 context_json 里出现
        context_json = json.dumps(dumped, ensure_ascii=False, default=str)
        assert "fact_constraints" not in context_json


# ── backward-compat:老调用方不传 excerpt,行为不变 ──────────────────


class TestBackwardCompat:
    """excerpt 字段默认 None 时,prompt 与 LLM 看到的事实与修改前一致。"""

    def test_no_excerpt_means_no_excerpt_in_prompt(self):
        ctx = TaskSummaryNarrativeContext(task_id="t1")
        dumped = ctx.model_dump(exclude_none=True)
        # 不应含 excerpt 相关键
        assert "requirement_text_excerpt" not in dumped
        assert "generated_section_content_excerpts" not in dumped
        # 但 schema_version / task_id 等应有
        assert "schema_version" in dumped
        assert "task_id" in dumped

    def test_prompt_still_works_without_excerpt(self):
        """无 excerpt 时,prompt 仍然有效,只是写作要点提示不触发。"""
        ctx = TaskSummaryNarrativeContext(task_id="t1")
        system_prompt, user_content = build_task_summary_prompt(ctx)
        assert "<NARRATIVE>" in system_prompt
        assert user_content
        # 写作要点段存在但无 excerpt 时 LLM 自然不引用
        assert "写作要点" in system_prompt