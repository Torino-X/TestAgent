"""Tests for batch 3 common modules.

Tests ResultParser (16 capabilities), TestPlanGeneratorTool (single-pass),
and SectionSuggestionTool.
"""

import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.common.test_plan_schema import DEFAULT_SECTIONS, CompletenessResult


# ═══════════════════════════════════════════════════════════════════
# Test plan schema
# ═══════════════════════════════════════════════════════════════════


class TestTestPlanSchema:
    def test_default_sections_count(self):
        assert len(DEFAULT_SECTIONS) == 10

    def test_default_sections_has_key_items(self):
        assert "项目概述" in DEFAULT_SECTIONS
        assert "测试目标" in DEFAULT_SECTIONS
        assert "风险分析" in DEFAULT_SECTIONS

    def test_completeness_passed_empty(self):
        cr = CompletenessResult()
        assert cr.passed is True

    def test_completeness_failed_with_missing(self):
        cr = CompletenessResult(missing_sections=["测试目标"])
        assert cr.passed is False


# ═══════════════════════════════════════════════════════════════════
# ResultParser — JSON extraction
# ═══════════════════════════════════════════════════════════════════


class TestResultParserParseJson:
    @pytest.fixture
    def parser(self):
        from app.common.result_parser import ResultParser
        return ResultParser()

    def test_pure_json(self, parser):
        result = parser.parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_markdown_fence(self, parser):
        result = parser.parse_json('```json\n{"a": 1}\n```')
        assert result == {"a": 1}

    def test_markdown_fence_no_lang(self, parser):
        result = parser.parse_json('```\n{"b": 2}\n```')
        assert result == {"b": 2}

    def test_bracket_extraction(self, parser):
        result = parser.parse_json('prefix {"c": 3} suffix')
        assert result == {"c": 3}

    def test_nested_braces(self, parser):
        result = parser.parse_json('{"outer": {"inner": [1,2,3]}}')
        assert result == {"outer": {"inner": [1, 2, 3]}}

    def test_empty_content_raises(self, parser):
        from app.common.result_parser import ResultParseError
        with pytest.raises(ResultParseError, match="空"):
            parser.parse_json("")

    def test_no_json_raises(self, parser):
        from app.common.result_parser import ResultParseError
        with pytest.raises(ResultParseError, match="完整 JSON"):
            parser.parse_json("no json here")

    def test_invalid_json_with_position(self, parser):
        from app.common.result_parser import ResultParseError
        # Create a JSON that is almost valid but has a trailing error
        with pytest.raises(ResultParseError, match="不合法"):
            parser.parse_json('{"a": 1,}')

    def test_not_a_dict_raises(self, parser):
        from app.common.result_parser import ResultParseError
        with pytest.raises(ResultParseError, match="顶层必须是对象"):
            parser.parse_json('["array", "not", "dict"]')

    def test_truncated_json_detected(self, parser):
        result = parser.parse_json('{"a": {"b": 1}}')
        assert not parser.is_json_truncated(json.dumps(result))

    def test_is_truncated_true(self, parser):
        assert parser.is_json_truncated('{"a": "hello') is True

    def test_is_truncated_false_complete(self, parser):
        assert parser.is_json_truncated('{"a": "hello"}') is False

    def test_is_truncated_no_brace(self, parser):
        assert parser.is_json_truncated("no braces at all") is False

    # ── 2026-07 中文密集响应解析（防回归）───────────────────────────
    # 旧实现有一行 `"\s+..."` 正则"归一化"键名空白，在中文密集响应里
    # 会把 `"value",\n  "next_key"` 的右引号和左引号一起吃掉，导致
    # `json.loads` 在第 9 行第 5 列报 "Expecting value"。这些用例确保
    # 删除正则后中文 key + 中文 value 的解析仍然稳定。
    def test_chinese_keys_and_values_parse_clean(self, parser):
        payload = (
            '{\n'
            '  "审批角色": "（待审批人签字）",\n'
            '  "审批意见": "对测试方案整体范围与里程碑安排进行审核。",\n'
            '  "审批日期": "（审批时填写）",\n'
            '  "签名": "（签字）"\n'
            '}'
        )
        result = parser.parse_json(payload)
        assert result == {
            "审批角色": "（待审批人签字）",
            "审批意见": "对测试方案整体范围与里程碑安排进行审核。",
            "审批日期": "（审批时填写）",
            "签名": "（签字）",
        }

    def test_chinese_nested_array_parses_clean(self, parser):
        """复现用户日志的尾片段：对象数组嵌套 + 中文键值。"""
        payload = (
            '{\n'
            '  "审批流程": [\n'
            '    {\n'
            '      "审批角色": "（待审批人签字）",\n'
            '      "审批意见": "对测试方案整体范围与里程碑安排进行审核，确认覆盖核心业务流程与高风险异常场景。",\n'
            '      "审批日期": "（审批时填写）",\n'
            '      "签名": "（签字）"\n'
            '    }\n'
            '  ]\n'
            '}'
        )
        result = parser.parse_json(payload)
        assert "审批流程" in result
        assert len(result["审批流程"]) == 1
        assert result["审批流程"][0]["审批意见"].startswith("对测试方案")

    def test_chinese_realistic_23k_chars_parses_clean(self, parser):
        """模拟用户实际收到的 ~23480 字符级响应：30 个章节、每章多字段。"""
        sections = []
        for i in range(30):
            sections.append(
                f'    {{\n'
                f'      "审批角色": "（待审批人签字{i}）",\n'
                f'      "审批意见": "对测试方案整体范围与里程碑安排进行审核确认{i}。",\n'
                f'      "审批日期": "（审批时填写{i}）",\n'
                f'      "签名": "（签字{i}）"\n'
                f'    }}'
            )
        payload = '{\n  "审批流程": [\n' + ',\n'.join(sections) + '\n  ]\n}'
        result = parser.parse_json(payload)
        assert len(result["审批流程"]) == 30
        assert result["审批流程"][29]["签名"] == "（签字29）"

    def test_chinese_value_with_quotes_inside_parses_clean(self, parser):
        """中文 value 里出现英文双引号不应破坏解析。"""
        # JSON 中表示 value 内的双引号需要反斜杠转义；Python 字符串
        # 里反斜杠又要再转义一次，所以是 \\\" 的 4 字符序列。
        payload = '{"备注": "他说\\"好的\\"就继续"}'
        result = parser.parse_json(payload)
        assert result == {"备注": '他说"好的"就继续'}

    def test_error_message_no_longer_mislabels_as_truncated(self, parser):
        """旧错误信息在 pos > 0.8*len 时贴"截断"标签——对结构破坏是误导。
        修复后调用 is_json_truncated：响应闭合时应说"结构异常"。"""
        from app.common.result_parser import ResultParseError
        # 故意构造结构破坏的 JSON（缺右花括号但内容完整）—— 实际是 truncate，
        # 这里测的是截断检测路径能正确贴"截断"标签。
        truncated_payload = '{"a": 1, "b": [1, 2, 3'
        with pytest.raises(ResultParseError) as exc_info:
            parser.parse_json(truncated_payload)
        msg = str(exc_info.value)
        # 截断确实发生时，应提示截断
        assert "可能因输出长度限制导致截断" in msg or "截断" in msg

    def test_error_message_marks_structural_corruption(self, parser):
        """真正结构破坏（非截断）时，新错误信息应明确"结构异常"。"""
        from app.common.result_parser import ResultParseError
        # 响应闭合，但中间多了一个逗号（结构错误）
        corrupted = '{"a": 1,, "b": 2}'
        with pytest.raises(ResultParseError) as exc_info:
            parser.parse_json(corrupted)
        msg = str(exc_info.value)
        assert "结构异常" in msg or "非截断" in msg


# ═══════════════════════════════════════════════════════════════════
# ResultParser — validation
# ═══════════════════════════════════════════════════════════════════


class TestResultParserValidation:
    @pytest.fixture
    def parser(self):
        from app.common.result_parser import ResultParser
        return ResultParser()

    @pytest.fixture
    def config(self):
        return {
            "ai_fields": [
                {"field": "overview", "title": "项目概述"},
                {"field": "objectives", "title": "测试目标"},
            ]
        }

    def test_required_fields(self, parser, config):
        fields = parser.required_fields(config)
        assert fields == ["overview", "objectives"]

    def test_field_bindings_from_ai_fields(self, parser, config):
        bindings = parser.field_bindings(config)
        assert "overview" in bindings
        assert bindings["overview"]["title"] == "项目概述"

    def test_field_bindings_from_explicit(self, parser):
        config = {
            "field_bindings": {
                "overview": {"field": "overview", "title": "概述", "section_id": "1"}
            }
        }
        bindings = parser.field_bindings(config)
        assert bindings["overview"]["section_id"] == "1"

    def test_validate_passes(self, parser, config):
        payload = {"overview": "text", "objectives": "text"}
        result = parser.validate_json_payload(payload, config)
        assert result.passed is True

    def test_validate_missing_field(self, parser, config):
        payload = {"overview": "text"}
        result = parser.validate_json_payload(payload, config)
        assert result.passed is False
        assert "objectives" in result.missing_fields

    def test_validate_extra_field(self, parser, config):
        payload = {"overview": "text", "objectives": "text", "extra": "bad"}
        result = parser.validate_json_payload(payload, config)
        assert result.passed is False
        assert "extra" in result.extra_fields

    def test_validate_empty_value(self, parser, config):
        payload = {"overview": "text", "objectives": ""}
        result = parser.validate_json_payload(payload, config)
        assert result.passed is False
        assert "objectives" in result.empty_fields

    def test_validate_empty_placeholder(self, parser, config):
        payload = {"overview": "text", "objectives": "待补充"}
        result = parser.validate_json_payload(payload, config)
        assert result.passed is False
        assert "objectives" in result.empty_fields

    def test_is_empty_value_none(self, parser):
        assert parser._is_empty_value(None) is True

    def test_is_empty_value_placeholder(self, parser):
        for v in ("...", "…", "待补充", "暂无", "无", "N/A", "n/a"):
            assert parser._is_empty_value(v) is True, f"'{v}' should be empty"

    def test_is_empty_value_non_empty(self, parser):
        assert parser._is_empty_value("实际内容") is False

    def test_parse_and_validate_success(self, parser, config):
        payload = parser.parse_and_validate_json(
            '{"overview": "概述内容", "objectives": "目标内容"}', config
        )
        assert payload == {"overview": "概述内容", "objectives": "目标内容"}

    def test_parse_and_validate_failure_raises(self, parser, config):
        from app.common.result_parser import ResultParseError
        with pytest.raises(ResultParseError, match="校验失败"):
            parser.parse_and_validate_json(
                '{"overview": "text"}', config
            )


# ═══════════════════════════════════════════════════════════════════
# ResultParser — table schema validation
# ═══════════════════════════════════════════════════════════════════


class TestResultParserTableValidation:
    @pytest.fixture
    def parser(self):
        from app.common.result_parser import ResultParser
        return ResultParser()

    def test_single_table_headers_match(self, parser):
        config = {
            "ai_fields": [
                {
                    "field": "env",
                    "title": "测试环境",
                    "table_schemas": [{"headers": ["环境", "配置"]}],
                }
            ]
        }
        payload = {"env": [{"环境": "服务器", "配置": "4C8G"}]}
        result = parser.validate_json_payload(payload, config)
        assert result.passed is True

    def test_single_table_missing_header(self, parser):
        config = {
            "ai_fields": [
                {
                    "field": "env",
                    "title": "测试环境",
                    "table_schemas": [{"headers": ["环境", "配置"]}],
                }
            ]
        }
        payload = {"env": [{"环境": "服务器"}]}  # missing 配置
        result = parser.validate_json_payload(payload, config)
        assert result.passed is False
        assert len(result.schema_mismatch_fields) > 0

    def test_single_table_extra_header(self, parser):
        config = {
            "ai_fields": [
                {
                    "field": "env",
                    "title": "测试环境",
                    "table_schemas": [{"headers": ["环境", "配置"]}],
                }
            ]
        }
        payload = {"env": [{"环境": "服务器", "配置": "4C8G", "备注": "多余"}]}
        result = parser.validate_json_payload(payload, config)
        assert result.passed is False

    def test_multi_table_format(self, parser):
        config = {
            "ai_fields": [
                {
                    "field": "resources",
                    "title": "测试资源",
                    "table_schemas": [
                        {"headers": ["角色", "职责"]},
                        {"headers": ["设备", "数量"]},
                    ],
                }
            ]
        }
        payload = {
            "resources": [
                [{"角色": "测试", "职责": "测试执行"}],
                [{"设备": "服务器", "数量": "2"}],
            ]
        }
        result = parser.validate_json_payload(payload, config)
        assert result.passed is True

    def test_multi_table_wrong_count(self, parser):
        config = {
            "ai_fields": [
                {
                    "field": "resources",
                    "title": "测试资源",
                    "table_schemas": [
                        {"headers": ["角色", "职责"]},
                        {"headers": ["设备", "数量"]},
                    ],
                }
            ]
        }
        payload = {
            "resources": [
                [{"角色": "测试", "职责": "测试执行"}],
                # only 1 table data for 2 schemas
            ]
        }
        result = parser.validate_json_payload(payload, config)
        assert result.passed is False

    def test_legacy_single_table_format(self, parser):
        """Legacy format: one-dimensional array auto-converts to [[...]]."""
        config = {
            "ai_fields": [
                {
                    "field": "env",
                    "title": "测试环境",
                    "table_schemas": [{"headers": ["环境", "配置"]}],
                }
            ]
        }
        # Legacy format — flat array of dicts
        payload = {"env": [{"环境": "服务器", "配置": "4C8G"}]}
        result = parser.validate_json_payload(payload, config)
        assert result.passed is True

    def test_no_table_schemas_skips_validation(self, parser):
        config = {
            "ai_fields": [
                {"field": "overview", "title": "项目概述", "table_schemas": []}
            ]
        }
        payload = {"overview": "plain text"}
        result = parser.validate_json_payload(payload, config)
        assert result.passed is True


# ═══════════════════════════════════════════════════════════════════
# ResultParser — sanitisation
# ═══════════════════════════════════════════════════════════════════


class TestResultParserSanitise:
    @pytest.fixture
    def parser(self):
        from app.common.result_parser import ResultParser
        return ResultParser()

    def test_remove_ai_label_suffix(self, parser):
        result = parser.sanitize_payload({"text": "测试内容【AI生成】"})
        assert result["text"] == "测试内容"

    def test_remove_keep_label_suffix(self, parser):
        result = parser.sanitize_payload({"text": "环境配置【保留原文】"})
        assert result["text"] == "环境配置"

    def test_remove_manual_label_prefix(self, parser):
        result = parser.sanitize_payload({"text": "手动编辑: 人员信息"})
        assert result["text"] == "人员信息"

    def test_remove_ai_generate_prefix(self, parser):
        result = parser.sanitize_payload({"text": "AI 生成：概述内容"})
        assert result["text"] == "概述内容"

    def test_nested_sanitise(self, parser):
        payload = {
            "text": "概述【AI生成】",
            "table": [{"col": "值【保留原文】"}],
            "nested": {"inner": "手动编辑: 内容"},
        }
        result = parser.sanitize_payload(payload)
        assert "【AI生成】" not in str(result)
        assert "【保留原文】" not in str(result)
        assert "手动编辑" not in str(result)

    def test_sanitise_text_strips_whitespace(self, parser):
        result = parser.sanitize_payload({"text": "  内容【AI生成】  "})
        assert result["text"] == "内容"


# ═══════════════════════════════════════════════════════════════════
# ResultParser — section package & merge
# ═══════════════════════════════════════════════════════════════════


class TestResultParserSectionPackage:
    @pytest.fixture
    def parser(self):
        from app.common.result_parser import ResultParser
        return ResultParser()

    def test_build_section_package(self, parser):
        config = {
            "ai_fields": [
                {
                    "field": "overview",
                    "title": "项目概述",
                    "section_id": "sec_001",
                    "level": 1,
                    "body_start_index": 5,
                    "table_schemas": [],
                }
            ],
            "keep_sections": ["测试环境"],
            "manual_sections": [],
            "section_bindings": [
                {"field": "overview", "title": "项目概述", "section_id": "sec_001"},
            ],
        }
        payload = {"overview": "这是项目概述内容"}
        package = parser.build_section_package(payload, config)

        assert package["schema_version"] == 1
        assert len(package["generated_sections"]) == 1
        assert package["generated_sections"][0]["field"] == "overview"
        assert package["generated_sections"][0]["content"] == "这是项目概述内容"
        assert package["generated_sections"][0]["section_id"] == "sec_001"
        assert "测试环境" in package["keep_sections"]

    def test_merge_batch_payloads(self, parser):
        config = {
            "ai_fields": [
                {"field": "overview", "title": "项目概述"},
                {"field": "objectives", "title": "测试目标"},
            ]
        }
        batch1 = {"overview": "概述内容"}
        batch2 = {"objectives": "目标内容"}
        merged = parser.merge_batch_payloads([batch1, batch2], config)
        assert merged == {"overview": "概述内容", "objectives": "目标内容"}

    def test_merge_duplicate_key_raises(self, parser):
        from app.common.result_parser import ResultParseError
        config = {
            "ai_fields": [
                {"field": "overview", "title": "项目概述"},
                {"field": "objectives", "title": "测试目标"},
            ]
        }
        batch1 = {"overview": "a"}
        batch2 = {"overview": "b"}  # duplicate
        with pytest.raises(ResultParseError, match="重复"):
            parser.merge_batch_payloads([batch1, batch2], config)

    def test_merge_missing_field_raises(self, parser):
        from app.common.result_parser import ResultParseError
        config = {
            "ai_fields": [
                {"field": "overview", "title": "项目概述"},
                {"field": "objectives", "title": "测试目标"},
            ]
        }
        batch1 = {"overview": "a"}  # missing objectives
        with pytest.raises(ResultParseError, match="缺少"):
            parser.merge_batch_payloads([batch1], config)

    def test_merge_empty_list_raises(self, parser):
        from app.common.result_parser import ResultParseError
        with pytest.raises(ResultParseError, match="没有可合并"):
            parser.merge_batch_payloads([], {})

    def test_format_json(self, parser):
        result = parser.format_json({"a": 1, "b": [2, 3]})
        assert "a" in result
        assert "1" in result
        assert "\n" in result  # indented

    def test_check_completeness_passes(self, parser):
        # Content containing all default sections
        content = " ".join(DEFAULT_SECTIONS)
        result = parser.check_completeness(content)
        assert result.passed is True

    def test_check_completeness_fails(self, parser):
        result = parser.check_completeness("only overview")
        assert result.passed is False
        assert len(result.missing_sections) > 0


# ═══════════════════════════════════════════════════════════════════
# SectionSuggestionTool
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
# SectionSuggestionTool
# ═══════════════════════════════════════════════════════════════════


class TestSectionSuggestionTool:
    @pytest.mark.asyncio
    async def test_returns_real_sections_from_context(self):
        from app.tools.section_suggestion_tool import SectionSuggestionTool
        from app.agent.context import AgentContext

        ctx = AgentContext(
            task_id="task_test",
            conversation_id="conv_test",
            user_id="user_test",
        )
        ctx.template_structure = {
            "sections": [
                {
                    "section_id": "sec_001",
                    "title": "1 项目概述",
                    "level": 1,
                    "mode": "ai",
                    "children": [],
                },
                {
                    "section_id": "sec_002",
                    "title": "2 测试环境",
                    "level": 1,
                    "mode": "keep",
                    "children": [
                        {
                            "section_id": "sec_003",
                            "title": "2.1 硬件环境",
                            "level": 2,
                            "mode": "keep",
                            "children": [],
                        }
                    ],
                },
                {
                    "section_id": "sec_004",
                    "title": "3 审批信息",
                    "level": 1,
                    "mode": "manual",
                    "children": [],
                },
            ],
            "generation_config": {},
        }

        tool = SectionSuggestionTool()
        result = await tool.run({}, ctx)

        assert result["success"] is True
        sections = result["data"]["sections"]
        assert len(sections) == 4  # 3 top-level + 1 child

        # Check ai_generate
        ai_sections = [s for s in sections if s["suggested_action"] == "ai_generate"]
        assert len(ai_sections) == 1
        assert ai_sections[0]["title"] == "1 项目概述"

        # Check keep_template
        keep_sections = [s for s in sections if s["suggested_action"] == "keep_template"]
        assert len(keep_sections) == 2  # parent + child

        # Check manual_fill
        manual = [s for s in sections if s["suggested_action"] == "manual_fill"]
        assert len(manual) == 1
        assert manual[0]["title"] == "3 审批信息"

        # Check available_actions
        for s in sections:
            assert "available_actions" in s
            assert len(s["available_actions"]) == 4

    @pytest.mark.asyncio
    async def test_no_template_returns_empty_with_warning(self):
        from app.tools.section_suggestion_tool import SectionSuggestionTool
        from app.agent.context import AgentContext

        ctx = AgentContext(
            task_id="task_test",
            conversation_id="conv_test",
            user_id="user_test",
        )
        # No template_structure set

        tool = SectionSuggestionTool()
        result = await tool.run({}, ctx)

        assert result["success"] is True
        assert result["data"]["sections"] == []
        assert len(result["warnings"]) > 0

    @pytest.mark.asyncio
    async def test_section_code_from_title_numbering(self):
        from app.tools.section_suggestion_tool import _section_code

        assert _section_code("", "1 项目概述") == "1"
        assert _section_code("", "1.2 测试背景") == "1.2"
        assert _section_code("", "2.3.1 详细设计") == "2.3.1"


# ═══════════════════════════════════════════════════════════════════
# TestPlanGeneratorTool
# ═══════════════════════════════════════════════════════════════════


class TestTestPlanGeneratorTool:
    @pytest.fixture
    def ctx_with_data(self):
        from app.agent.context import AgentContext
        ctx = AgentContext(
            task_id="task_test",
            conversation_id="conv_test",
            user_id="user_test",
        )
        ctx.requirement_analysis = {
            "text_content": "这是一个测试系统的需求文档，包含用户管理和订单管理功能。",
        }
        ctx.template_structure = {
            "sections": [
                {
                    "section_id": "sec_001",
                    "title": "1 项目概述",
                    "level": 1,
                    "mode": "ai",
                    "children": [],
                },
                {
                    "section_id": "sec_002",
                    "title": "2 测试环境",
                    "level": 1,
                    "mode": "keep",
                    "children": [],
                },
            ],
            "generation_config": {
                "ai_fields": [
                    {"field": "overview", "title": "项目概述", "section_id": "sec_001", "level": 1},
                ],
                "keep_sections": ["测试环境"],
                "manual_sections": [],
                "section_bindings": [],
            },
        }
        return ctx

    @pytest.mark.asyncio
    async def test_missing_requirement(self):
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool
        from app.agent.context import AgentContext

        ctx = AgentContext(task_id="t", conversation_id="c", user_id="u")
        tool = TestPlanGeneratorTool()
        result = await tool.run({}, ctx)
        assert result["success"] is False
        assert result["error"]["code"] == "MISSING_REQUIREMENT"

    @pytest.mark.asyncio
    async def test_missing_template(self):
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool
        from app.agent.context import AgentContext

        ctx = AgentContext(task_id="t", conversation_id="c", user_id="u")
        ctx.requirement_analysis = {"text_content": "test"}
        tool = TestPlanGeneratorTool()
        result = await tool.run({}, ctx)
        assert result["success"] is False
        assert result["error"]["code"] == "MISSING_TEMPLATE"

    @pytest.mark.asyncio
    async def test_no_ai_fields(self):
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool
        from app.agent.context import AgentContext

        ctx = AgentContext(task_id="t", conversation_id="c", user_id="u")
        ctx.requirement_analysis = {"text_content": "test"}
        ctx.template_structure = {
            "sections": [{"title": "1 Overview", "level": 1, "mode": "ai"}],
            "generation_config": {"ai_fields": []},
        }
        tool = TestPlanGeneratorTool()
        result = await tool.run({}, ctx)
        assert result["success"] is False
        assert result["error"]["code"] == "NO_AI_FIELDS"

    @pytest.mark.asyncio
    async def test_mock_generation(self, ctx_with_data):
        """Test generation pipeline with a custom JSON-returning mock."""
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool
        from unittest.mock import patch, AsyncMock

        # We need the mock to return valid JSON, not markdown.
        # Patch MockLLMClient.generate to return JSON payload.
        async def json_gen(self, prompt, images=None):
            return '{"overview": "项目概述内容"}'

        with patch(
            "app.integrations.llm_client.MockLLMClient.generate", json_gen
        ):
            tool = TestPlanGeneratorTool()
            result = await tool.run({"use_mock": True}, ctx_with_data)

        assert result["success"] is True, f"Expected success but got error: {result.get('error')}"
        assert "generated_sections" in result["data"]
        assert "section_package" in result["data"]
        assert ctx_with_data.test_plan_content is not None

    def test_extract_headings(self):
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool

        sections = [
            {
                "title": "1 项目概述",
                "children": [
                    {"title": "1.1 背景"},
                    {"title": "1.2 目标"},
                ],
            },
            {"title": "2 测试策略", "children": []},
        ]
        headings = TestPlanGeneratorTool._extract_headings(sections)
        assert "1 项目概述" in headings
        assert "1.1 背景" in str(headings)
        assert "2 测试策略" in headings

    def test_extract_table_fields(self):
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool

        sections = [
            {
                "title": "测试环境",
                "table_schemas": [{"headers": ["环境", "配置"]}],
            }
        ]
        fields = TestPlanGeneratorTool._extract_table_fields(sections)
        assert "环境" in fields[0]
        assert "配置" in fields[0]

    def test_classify_model_error(self):
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool

        assert TestPlanGeneratorTool._classify_model_error("超时错误") == "MODEL_TIMEOUT"
        assert TestPlanGeneratorTool._classify_model_error("connection refused") == "MODEL_CONNECTION_ERROR"
        assert TestPlanGeneratorTool._classify_model_error("HTTP 500错误") == "MODEL_STATUS_ERROR"
        assert TestPlanGeneratorTool._classify_model_error("未知问题") == "MODEL_UNKNOWN_ERROR"

    @pytest.mark.asyncio
    async def test_resolve_llm_config(self):
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool
        from app.agent.context import AgentContext

        ctx = AgentContext(task_id="t", conversation_id="c", user_id="u")
        # ctx has no settings_service → falls back to _EnvConfig.
        config = await TestPlanGeneratorTool._resolve_llm_config(ctx)
        assert hasattr(config, "api_url")
        assert hasattr(config, "api_key")
        assert hasattr(config, "model_name")
        assert hasattr(config, "timeout")

    @pytest.mark.asyncio
    async def test_tool_standard_output_format(self, ctx_with_data):
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool

        tool = TestPlanGeneratorTool()
        result = await tool.run({"use_mock": True}, ctx_with_data)

        # Standard Tool output fields
        assert "success" in result
        assert "tool_name" in result
        assert "task_id" in result
        assert "data" in result
        assert "summary" in result
        assert "warnings" in result
        assert "error" in result

    @pytest.mark.asyncio
    async def test_model_error_returns_standard_error(self, ctx_with_data):
        from app.tools.test_plan_generator_tool import TestPlanGeneratorTool
        from app.integrations.llm_client import MockLLMClient

        # Patch MockLLMClient to fail
        original_gen = MockLLMClient.generate

        async def failing_gen(*args, **kwargs):
            from app.integrations.llm_client import LLMClientError
            raise LLMClientError("模型调用超时（配置超时 120s）")

        with patch.object(MockLLMClient, "generate", failing_gen):
            tool = TestPlanGeneratorTool()
            result = await tool.run({"use_mock": True}, ctx_with_data)

        assert result["success"] is False
        assert result["error"]["code"] == "MODEL_TIMEOUT"
        assert "超时" in result["error"]["message"]
