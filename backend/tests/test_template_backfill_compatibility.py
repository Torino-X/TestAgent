"""BUG FIX 2026-08-18 (template_backfill):
F025 rule kind ``template_backfill`` 的端到端测试。

覆盖:
  * ``build_review_standard`` 规则合成:mode=ai + table_indexes=[] + leaf
    section 才生成 rule;mode=keep / table_indexes 非空 / 非 leaf 不生成。
  * ``ResultReviewTool._check_template_backfill`` handler:LLM 返回 tables
    非空 → block issue;未返回 tables → 无 issue。
  * 端到端 ``ResultReviewTool.run`` 集成:block issue 透传到 rule_issues
    / review_issues / block_issues,level=failed,经 fix_instruction 给 RepairAgent。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.context import AgentContext
from app.common.template_section import TemplateSection
from app.common.template_section_service import (
    TemplateSectionService,
    build_review_standard,
)
from app.tools.result_review_tool import ResultReviewTool


# ── helper:构造 TemplateSection ───────────────────────────────────────


def _make_section(
    *,
    title: str = "1 测试策略",
    level: int = 1,
    mode: str = "ai",
    body_start_index: int | None = 25,
    table_indexes: list[int] | None = None,
    children: list | None = None,
) -> TemplateSection:
    """构造一个最小可用的 TemplateSection。"""
    return TemplateSection(
        title=title,
        level=level,
        mode=mode,
        body_start_index=body_start_index,
        table_indexes=list(table_indexes) if table_indexes is not None else [],
        children=children or [],
    )


# ── build_review_standard rule 合成 ────────────────────────────────────


class TestBuildReviewStandardTemplateBackfill:
    """``build_review_standard`` 应为 mode=ai + 无 tables 的 leaf section
    合成 template_backfill rule;keep / 非 leaf / 有 tables 不合成。"""

    def _standard_for(self, *sections: TemplateSection) -> dict:
        return build_review_standard(
            template_id="tpl_test",
            sections=list(sections),
            generation_config=None,
        )

    def _rules_by_kind(self, standard: dict) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in standard.get("rules", []):
            if isinstance(r, dict):
                out.setdefault(r.get("kind", ""), []).append(r)
        return out

    def test_emits_rule_for_ai_leaf_with_no_tables(self):
        sec = _make_section(title="1 测试策略", mode="ai", table_indexes=[])
        standard = self._standard_for(sec)
        rules = self._rules_by_kind(standard).get("template_backfill", [])
        assert len(rules) == 1
        rule = rules[0]
        assert rule["section_id"] == "body_25_level_1"
        assert rule["expected_no_tables"] is True
        assert rule["severity"] == "block"
        assert rule["section_title"] == "1 测试策略"
        assert "fix_instruction" in rule

    def test_no_rule_for_keep_section(self):
        """mode=keep 章节不应发 template_backfill(后续可扩展 keep 规则)。"""
        sec = _make_section(title="5 测试环境", mode="keep", table_indexes=[])
        standard = self._standard_for(sec)
        rules = self._rules_by_kind(standard).get("template_backfill", [])
        assert rules == []

    def test_no_rule_for_section_with_tables(self):
        """模板本身有 tables(table_indexes 非空)由 table_header_whitelist 接管。"""
        sec = _make_section(title="2 测试目标", mode="ai", table_indexes=[3, 4])
        standard = self._standard_for(sec)
        rules = self._rules_by_kind(standard).get("template_backfill", [])
        assert rules == []

    def test_no_rule_for_non_leaf_section(self):
        """非 leaf(有 children)section 不发 rule,子章节各自校验。"""
        child = _make_section(title="1.1 子项", mode="ai", table_indexes=[])
        parent = _make_section(title="1 父章节", mode="ai", table_indexes=[], children=[child])
        standard = self._standard_for(parent)
        rules = self._rules_by_kind(standard).get("template_backfill", [])
        # parent 不发(非 leaf),child 也不发(非顶层,只在传进 sections 时遍历)。
        # 注:当前 build_review_standard 只遍历顶层 sections,不递归 children。
        # 因此 child 也不在结果里。
        assert rules == []

    def test_rule_id_is_stable(self):
        """rule_id 基于 section_id 派生,同一 section 多次调用得同一 id。"""
        sec = _make_section(title="1 测试策略", mode="ai", body_start_index=25)
        s1 = self._standard_for(sec)
        s2 = self._standard_for(sec)
        r1 = self._rules_by_kind(s1)["template_backfill"][0]
        r2 = self._rules_by_kind(s2)["template_backfill"][0]
        assert r1["id"] == r2["id"]
        assert r1["id"] == "no_tables_for_body_25_level_1"


# ── _check_template_backfill handler ──────────────────────────────────


class TestCheckTemplateBackfillHandler:
    """handler 单测:LLM 返回 tables 非空 → block issue。"""

    @staticmethod
    def _rule(section_id: str = "body_25_level_1") -> dict:
        return {
            "id": f"no_tables_for_{section_id}",
            "kind": "template_backfill",
            "section_id": section_id,
            "expected_no_tables": True,
            "severity": "block",
        }

    def test_emits_block_when_llm_returned_tables(self):
        sections = [
            {
                "section_id": "body_25_level_1",
                "title": "1 测试策略",
                "content": "正文",
                "tables": [[{"col1": "v1", "col2": "v2"}]],  # 违规:模板无 tables
            },
        ]
        issues = ResultReviewTool._check_template_backfill(sections, self._rule())
        assert len(issues) == 1
        issue = issues[0]
        assert issue["kind"] == "template_backfill"
        assert issue["severity"] == "block"
        assert issue["section_id"] == "body_25_level_1"
        assert "tables" in issue["message"]
        assert issue["evidence"]["table_count"] == 1
        assert issue["span"] is None

    def test_emits_block_when_content_is_table_array(self):
        sections = [
            {
                "section_id": "body_25_level_1",
                "title": "1 测试策略",
                "content": [{"col1": "v1", "col2": "v2"}],
            },
        ]
        issues = ResultReviewTool._check_template_backfill(sections, self._rule())
        assert len(issues) == 1
        assert issues[0]["severity"] == "block"
        assert issues[0]["evidence"]["table_count"] == 1

    def test_emits_block_when_content_contains_tables_key(self):
        sections = [
            {
                "section_id": "body_25_level_1",
                "title": "1 测试策略",
                "content": {
                    "summary": "正文",
                    "tables": [[{"col1": "v1"}], [{"col2": "v2"}]],
                },
            },
        ]
        issues = ResultReviewTool._check_template_backfill(sections, self._rule())
        assert len(issues) == 1
        assert issues[0]["severity"] == "block"
        assert issues[0]["evidence"]["table_count"] == 2

    def test_no_issue_when_llm_did_not_return_tables(self):
        sections = [
            {
                "section_id": "body_25_level_1",
                "title": "1 测试策略",
                "content": "正文",
            },
        ]
        issues = ResultReviewTool._check_template_backfill(sections, self._rule())
        assert issues == []

    def test_no_issue_when_target_section_absent(self):
        """generated_sections 缺该 section_id → 不发 issue(与其他 handler 一致)。"""
        sections = [{"section_id": "body_99_level_1", "content": "x"}]
        issues = ResultReviewTool._check_template_backfill(sections, self._rule())
        assert issues == []

    def test_no_issue_when_rule_missing_expected_no_tables(self):
        sections = [{"section_id": "body_25_level_1", "tables": [[{}]]}]
        rule = {"id": "x", "kind": "template_backfill", "section_id": "body_25_level_1"}
        # 缺 expected_no_tables → 早返
        issues = ResultReviewTool._check_template_backfill(sections, rule)
        assert issues == []

    def test_no_issue_when_tables_empty_list(self):
        sections = [{"section_id": "body_25_level_1", "tables": []}]
        issues = ResultReviewTool._check_template_backfill(sections, self._rule())
        assert issues == []

    def test_idempotent_on_repeated_calls(self):
        """同一 (sections, rule) 多次调用,issues 数稳定。"""
        sections = [
            {
                "section_id": "body_25_level_1",
                "tables": [[{"k": "v"}]],
            },
        ]
        issues1 = ResultReviewTool._check_template_backfill(sections, self._rule())
        issues2 = ResultReviewTool._check_template_backfill(sections, self._rule())
        assert len(issues1) == 1
        assert len(issues2) == 1
        assert issues1[0]["section_id"] == issues2[0]["section_id"]


# ── 端到端 ResultReviewTool.run 集成 ─────────────────────────────────


class TestEndToEndTemplateBackfill:
    """完整链路:rule 进 ctx.review_standard → ResultReviewTool.run →
    rule_issues / review_issues / block_issues / level=failed。"""

    @staticmethod
    def _ctx(
        *,
        section_id: str = "body_25_level_1",
        tables=None,
        with_rule: bool = True,
    ) -> AgentContext:
        ctx = AgentContext(
            task_id="t", conversation_id="c", user_id="u",
        )
        section: dict[str, Any] = {
            "section_id": section_id,
            "title": "1 测试策略",
            "content": (
                "围绕核心业务流程开展功能、接口、性能、安全与兼容性测试，"
                "确保本章节样例聚焦模板表格回填契约。"
            ),
        }
        if tables is not None:
            section["tables"] = tables
        ctx.test_plan_content = {
            "section_package": {
                "generated_sections": [section],
                "keep_sections": [],
                "manual_sections": [],
            },
        }
        ctx.section_confirm_config = {
            "sections": [
                {
                    "section_id": section_id,
                    "title": "1 测试策略",
                    "suggested_action": "ai_generate",
                }
            ]
        }
        if with_rule:
            ctx.review_standard = {
                "version": 1,
                "rules": [
                    {
                        "id": f"no_tables_for_{section_id}",
                        "kind": "template_backfill",
                        "section_id": section_id,
                        "expected_no_tables": True,
                        "severity": "block",
                        "fix_instruction": "请去掉 tables 字段",
                    },
                ],
                "pass_policy": "any_block_fails",
            }
        return ctx

    @pytest.mark.asyncio
    async def test_block_issue_flows_to_review_issues_and_block_issues(self):
        ctx = self._ctx(
            tables=[[{"col1": "v"}]],
        )
        result = await ResultReviewTool().run({}, ctx)
        assert result["success"] is True
        data = result["data"]
        assert data["level"] == "failed"
        assert data["passed"] is False

        rule_issues = data["rule_issues"]
        assert any(
            i.get("kind") == "template_backfill" and i.get("severity") == "block"
            for i in rule_issues
        )

        # rule_issues 与 review_issues 是同一引用(同源 issue 列表)
        assert data["review_issues"] == data["rule_issues"]

        # block_issues 是 rule_issues 中 severity=block 的子集
        # (block_issues 字段只有 rule_id/section_id/message,不含 kind —
        # 这是 ResultReviewTool pre-existing 设计,见 line 301-309。)
        block_section_ids = [
            i.get("section_id") for i in data["block_issues"]
        ]
        assert "body_25_level_1" in block_section_ids

        # fix_instruction 透传到 issue
        tb_issue = next(i for i in rule_issues if i.get("kind") == "template_backfill")
        assert tb_issue.get("fix_instruction") == "请去掉 tables 字段"
        assert tb_issue.get("section_id") == "body_25_level_1"
        assert tb_issue.get("suggested_strategy") == "regenerate_section"
        assert tb_issue.get("expected_rule") == "section_matches_template"
        assert tb_issue.get("repairable") is True

    @pytest.mark.asyncio
    async def test_no_block_when_llm_followed_template(self):
        """LLM 未返回 tables → level=passed,无 template_backfill issue。"""
        ctx = self._ctx(tables=[])
        result = await ResultReviewTool().run({}, ctx)
        assert result["success"] is True
        data = result["data"]
        kinds = [i.get("kind") for i in data["rule_issues"]]
        assert "template_backfill" not in kinds
        assert data["level"] in ("passed", "warning")

    @pytest.mark.asyncio
    async def test_no_evaluation_when_review_standard_absent(self):
        """backward-compat:review_standard=None 时不进 F025 引擎。"""
        ctx = self._ctx(tables=[[{"col1": "v"}]], with_rule=False)
        result = await ResultReviewTool().run({}, ctx)
        data = result["data"]
        assert not any(
            i.get("kind") == "template_backfill" for i in data["rule_issues"]
        )


# ── handler dispatch 注册 ─────────────────────────────────────────────


class TestHandlerRegistration:
    """_evaluate_rules dispatch dict 必须包含 template_backfill 映射。"""

    def test_template_backfill_in_dispatch(self):
        """handler dict 必须含 template_backfill key(防止漏注册)。"""
        # _evaluate_rules 的 handler dict 在 closure 内,这里通过 mock
        # rule_kind 触发并验证 dispatch。
        from unittest.mock import patch

        captured: list[str] = []

        def fake_handler(sections, rule):
            captured.append(rule.get("kind"))
            return []

        with patch.object(ResultReviewTool, "_check_template_backfill", staticmethod(fake_handler)):
            # 构造一个 minimal rule,走 dispatch
            rule = {
                "id": "x", "kind": "template_backfill",
                "section_id": "s1", "expected_no_tables": True, "severity": "block",
            }
            sections: list[dict] = []
            ResultReviewTool()._evaluate_rules(sections, {
                "version": 1, "rules": [rule], "pass_policy": "any_block_fails",
            })
        assert captured == ["template_backfill"]
