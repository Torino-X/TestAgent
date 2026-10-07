"""Tests for ResultReviewTool — migrated from legacy check_completeness + new review capabilities."""

from __future__ import annotations

import pytest

from app.agent.context import AgentContext
from app.tools.result_review_tool import ResultReviewTool, DEFAULT_SECTIONS


@pytest.fixture
def tool():
    return ResultReviewTool()


@pytest.fixture
def base_context():
    """Create a minimal AgentContext for testing."""
    ctx = AgentContext(
        task_id="task_test",
        conversation_id="conv_test",
        user_id="user_test",
    )
    return ctx


@pytest.fixture
def full_test_plan_content():
    """Create a complete test_plan_content with section_package."""
    return {
        "generated_sections": 7,
        "kept_sections": 3,
        "manual_sections": 2,
        "total_word_count": 5000,
        "tables_generated": 4,
        "section_package": {
            "generated_sections": [
                {
                    "section_id": "sec_001",
                    "title": "项目概述",
                    "content": "本项目为智慧校园综合服务平台，主要面向学校师生和管理人员提供统一服务入口，包括用户管理、校园服务、消息通知等核心功能模块。",
                    "level": 1,
                },
                {
                    "section_id": "sec_002",
                    "title": "测试目标",
                    "content": "验证系统功能正确性、性能稳定性和安全可靠性，确保系统满足需求规格说明书中的各项要求。",
                    "level": 1,
                },
                {
                    "section_id": "sec_003",
                    "title": "测试范围",
                    "content": "本次测试覆盖用户管理、校园服务、消息通知、权限管理、数据统计等核心业务模块。",
                    "level": 1,
                },
                {
                    "section_id": "sec_004",
                    "title": "测试策略",
                    "content": "采用黑盒测试为主，结合白盒测试进行代码覆盖率分析。测试阶段包括单元测试、集成测试、系统测试和验收测试。",
                    "level": 1,
                },
                {
                    "section_id": "sec_007",
                    "title": "测试进度",
                    "content": "【待补充】请根据项目计划补充测试进度安排",
                    "level": 1,
                },
                {
                    "section_id": "sec_008",
                    "title": "准入准出标准",
                    "content": "准入标准：所有开发任务完成、代码评审通过、测试环境部署完成。准出标准：所有高优先级缺陷修复、测试用例执行率≥95%。",
                    "level": 1,
                },
                {
                    "section_id": "sec_009",
                    "title": "风险分析",
                    "content": "主要风险包括：1）需求变更频繁；2）第三方接口不稳定；3）测试环境资源不足。",
                    "level": 1,
                },
            ],
            "keep_sections": [
                {"section_id": "sec_005", "title": "测试环境", "level": 1},
                {"section_id": "sec_006", "title": "测试资源", "level": 1},
                {"section_id": "sec_010", "title": "测试交付物", "level": 1},
            ],
            "manual_sections": [
                {"section_id": "sec_007", "title": "测试进度", "level": 1},
            ],
        },
    }


@pytest.fixture
def requirement_analysis():
    """Create a requirement_analysis with modules."""
    return {
        "project_name": "智慧校园综合服务平台",
        "modules": [
            {"module_id": "m_001", "module_name": "用户管理"},
            {"module_id": "m_002", "module_name": "校园服务"},
            {"module_id": "m_003", "module_name": "消息通知"},
        ],
    }


@pytest.fixture
def template_structure():
    """Create a template_structure with sections."""
    return {
        "sections": [
            {"section_id": "sec_001", "title": "项目概述", "level": 1},
            {"section_id": "sec_002", "title": "测试目标", "level": 1},
            {"section_id": "sec_003", "title": "测试范围", "level": 1},
            {"section_id": "sec_004", "title": "测试策略", "level": 1},
            {"section_id": "sec_005", "title": "测试环境", "level": 1},
            {"section_id": "sec_006", "title": "测试资源", "level": 1},
            {"section_id": "sec_007", "title": "测试进度", "level": 1},
            {"section_id": "sec_008", "title": "准入准出标准", "level": 1},
            {"section_id": "sec_009", "title": "风险分析", "level": 1},
            {"section_id": "sec_010", "title": "测试交付物", "level": 1},
        ],
    }


def _complete_generated_sections(extra_sections=None):
    base = [
        {
            "section_id": f"default_{idx}",
            "title": title,
            "content": (
                f"{title}内容覆盖充分，用于满足完整性检查和基础审查逻辑。"
                "这里补充足够的测试背景、范围说明、执行关注点和质量目标。"
            ),
            "level": 1,
        }
        for idx, title in enumerate(DEFAULT_SECTIONS)
    ]
    return base + list(extra_sections or [])


def _approval_template_structure():
    return {
        "generation_config": {
            "ai_fields": [
                {
                    "field": "field_body_14_level_1",
                    "section_id": "body_14_level_1",
                    "title": "审批信息",
                    "type": "array",
                    "table_schemas": [
                        {
                            "headers": [
                                "角色",
                                "姓名",
                                "审批意见",
                                "审批日期",
                                "签名",
                            ]
                        }
                    ],
                }
            ]
        }
    }


# ── DEFAULT_SECTIONS tests ─────────────────────────────────────────────


class TestDefaultSections:
    """Verify DEFAULT_SECTIONS is preserved from legacy."""

    def test_default_sections_count(self):
        assert len(DEFAULT_SECTIONS) == 10

    def test_default_sections_content(self):
        expected = [
            "项目概述", "测试目标", "测试范围", "测试策略", "测试环境",
            "测试资源", "测试进度", "准入准出标准", "风险分析", "测试交付物",
        ]
        assert DEFAULT_SECTIONS == expected

    def test_default_sections_order(self):
        """Order must be preserved for consistency."""
        assert DEFAULT_SECTIONS[0] == "项目概述"
        assert DEFAULT_SECTIONS[-1] == "测试交付物"


class TestGeneratorSchemaIssueReReview:
    """Generator schema_issues are historical; current content is authoritative."""

    @pytest.mark.asyncio
    async def test_resolved_schema_issue_does_not_block_review(
        self, tool, base_context,
    ):
        approval_section = {
            "section_id": "body_14_level_1",
            "field": "field_body_14_level_1",
            "title": "审批信息",
            "content": [
                {
                    "角色": "编写人",
                    "姓名": "张三",
                    "审批意见": "同意",
                    "审批日期": "2026-08-21",
                    "签名": "张三",
                }
            ],
            "level": 1,
        }
        base_context.template_structure = _approval_template_structure()
        base_context.test_plan_content = {
            "section_package": {
                "generated_sections": _complete_generated_sections([
                    approval_section
                ]),
                "keep_sections": [],
                "manual_sections": [],
            },
            "schema_issues": [
                {
                    "kind": "schema_mismatch",
                    "field": "field_body_14_level_1",
                    "severity": "block",
                    "message": "表头缺少签名且包含 TEST_FAULT_签名",
                }
            ],
        }

        result = await tool.run({}, base_context)

        assert result["success"] is True
        assert result["data"]["level"] != "failed"
        assert result["data"]["block_issues"] == []
        assert "schema_issues" not in base_context.test_plan_content

    @pytest.mark.asyncio
    async def test_unresolved_schema_issue_still_blocks_review(
        self, tool, base_context,
    ):
        approval_section = {
            "section_id": "body_14_level_1",
            "field": "field_body_14_level_1",
            "title": "审批信息",
            "content": [
                {
                    "角色": "编写人",
                    "姓名": "张三",
                    "审批意见": "同意",
                    "审批日期": "2026-08-21",
                    "TEST_FAULT_签名": "张三",
                }
            ],
            "level": 1,
        }
        base_context.template_structure = _approval_template_structure()
        base_context.test_plan_content = {
            "section_package": {
                "generated_sections": _complete_generated_sections([
                    approval_section
                ]),
                "keep_sections": [],
                "manual_sections": [],
            },
            "schema_issues": [
                {
                    "kind": "schema_mismatch",
                    "field": "field_body_14_level_1",
                    "severity": "block",
                    "message": "表头缺少签名且包含 TEST_FAULT_签名",
                }
            ],
        }

        result = await tool.run({}, base_context)

        assert result["success"] is True
        assert result["data"]["level"] == "failed"
        assert result["data"]["block_issues"][0]["section_id"] == (
            "field_body_14_level_1"
        )
        assert base_context.test_plan_content["schema_issues"]


# ── Completeness check tests (from legacy) ─────────────────────────────


class TestCompletenessCheck:
    """Test check_completeness migrated from legacy result_parser.py."""

    @pytest.mark.asyncio
    async def test_all_sections_present(self, tool, base_context, full_test_plan_content, template_structure):
        """All required sections present → no missing."""
        base_context.test_plan_content = full_test_plan_content
        base_context.template_structure = template_structure

        result = await tool.run({}, base_context)
        assert result["success"] is True
        data = result["data"]
        # All 10 DEFAULT_SECTIONS should be covered (7 generated + 3 kept)
        assert len(data["missing_sections"]) == 0

    @pytest.mark.asyncio
    async def test_missing_sections_detected(self, tool, base_context):
        """Missing sections should be detected."""
        base_context.test_plan_content = {
            "generated_sections": 1,
            "kept_sections": 0,
            "manual_sections": 0,
            "section_package": {
                "generated_sections": [
                    {"section_id": "sec_001", "title": "项目概述", "content": "项目背景和概述内容"},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        }

        result = await tool.run({}, base_context)
        assert result["success"] is True
        data = result["data"]
        assert len(data["missing_sections"]) > 0
        assert "测试目标" in data["missing_sections"]
        assert "测试范围" in data["missing_sections"]

    @pytest.mark.asyncio
    async def test_keep_template_sections_do_not_count_as_missing_required(
        self, tool, base_context,
    ):
        """Only AI-generated sections are required for LLM JSON completeness."""
        base_context.section_confirm_config = {
            "sections": [
                {
                    "section_id": "risk",
                    "title": "风险分析",
                    "suggested_action": "keep_template",
                },
                {
                    "section_id": "deliverables",
                    "title": "测试交付物",
                    "suggested_action": "keep_template",
                },
                {
                    "section_id": "strategy",
                    "field": "strategy",
                    "title": "测试策略",
                    "suggested_action": "ai_generate",
                },
            ]
        }
        base_context.template_structure = {
            "generation_config": {
                "ai_fields": [
                    {
                        "field": "strategy",
                        "section_id": "strategy",
                        "title": "测试策略",
                    }
                ]
            }
        }
        base_context.test_plan_content = {
            "section_package": {
                "generated_sections": [
                    {
                        "field": "strategy",
                        "section_id": "strategy",
                        "title": "测试策略",
                        "content": (
                            "围绕核心业务流程开展功能、接口、性能、安全与兼容性测试，"
                            "结合预约、签到、通知、权限和异常处理等场景设计分层验证策略。"
                        ),
                    }
                ],
                "keep_sections": [
                    {"section_id": "risk", "title": "风险分析"},
                    {"section_id": "deliverables", "title": "测试交付物"},
                ],
                "manual_sections": [],
            }
        }

        result = await tool.run({}, base_context)

        assert result["success"] is True
        data = result["data"]
        assert "风险分析" not in data["missing_sections"]
        assert "测试交付物" not in data["missing_sections"]
        assert data["level"] == "passed"
        assert data["review_issues"] == []

    @pytest.mark.asyncio
    async def test_ai_generated_empty_section_becomes_block_review_issue(
        self, tool, base_context,
    ):
        base_context.section_confirm_config = {
            "sections": [
                {
                    "section_id": "strategy",
                    "field": "strategy",
                    "title": "测试策略",
                    "suggested_action": "ai_generate",
                }
            ]
        }
        base_context.test_plan_content = {
            "section_package": {
                "generated_sections": [
                    {
                        "field": "strategy",
                        "section_id": "strategy",
                        "title": "测试策略",
                        "content": "",
                    }
                ],
                "keep_sections": [],
                "manual_sections": [],
            }
        }

        result = await tool.run({}, base_context)

        assert result["success"] is True
        data = result["data"]
        assert data["level"] == "failed"
        assert "测试策略" in data["missing_sections"]
        assert any(
            issue.get("severity") == "block"
            and issue.get("section_id") == "strategy"
            for issue in data["review_issues"]
        )

    @pytest.mark.asyncio
    async def test_missing_ai_section_uses_generation_config_canonical_id(
        self, tool, base_context,
    ):
        base_context.section_confirm_config = {
            "sections": [
                {
                    "id": "section_1",
                    "title": "1 项目概述",
                    "suggested_action": "ai_generate",
                }
            ]
        }
        base_context.template_structure = {
            "generation_config": {
                "ai_fields": [
                    {
                        "field": "overview",
                        "section_id": "body_18_level_1",
                        "title": "1 项目概述",
                    }
                ]
            }
        }
        base_context.test_plan_content = {
            "section_package": {
                "generated_sections": [],
                "keep_sections": [],
                "manual_sections": [],
            }
        }

        result = await tool.run({}, base_context)

        assert result["success"] is True
        data = result["data"]
        issue = next(
            item
            for item in data["review_issues"]
            if item.get("rule_id") == "missing_required_ai_section"
        )
        assert issue["section_id"] == "body_18_level_1"
        assert issue["field_path"] == "overview"
        assert issue["evidence"]["field"] == "overview"
        assert "section_1" in issue["evidence"]["aliases"]

    @pytest.mark.asyncio
    async def test_completeness_with_template_sections(self, tool, base_context, full_test_plan_content, template_structure):
        """Template structure sections should be included in completeness check."""
        base_context.test_plan_content = full_test_plan_content
        base_context.template_structure = template_structure

        result = await tool.run({}, base_context)
        data = result["data"]
        # With template_structure, all sections should be found
        assert len(data["missing_sections"]) == 0


# ── Module coverage tests ──────────────────────────────────────────────


class TestModuleCoverage:
    """Test module coverage check."""

    @pytest.mark.asyncio
    async def test_full_module_coverage(self, tool, base_context, full_test_plan_content, requirement_analysis):
        """All modules covered → covered_module_count == total_module_count."""
        base_context.test_plan_content = full_test_plan_content
        base_context.requirement_analysis = requirement_analysis

        result = await tool.run({}, base_context)
        assert result["success"] is True
        data = result["data"]
        assert data["total_module_count"] == 3
        assert data["covered_module_count"] == 3

    @pytest.mark.asyncio
    async def test_partial_module_coverage(self, tool, base_context, requirement_analysis):
        """Some modules not covered → warning."""
        base_context.test_plan_content = {
            "generated_sections": 1,
            "kept_sections": 0,
            "manual_sections": 0,
            "section_package": {
                "generated_sections": [
                    {
                        "section_id": "sec_001",
                        "title": "项目概述",
                        "content": "本项目为智慧校园综合服务平台，主要面向用户管理模块。",
                    },
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        }
        base_context.requirement_analysis = requirement_analysis

        result = await tool.run({}, base_context)
        data = result["data"]
        assert data["covered_module_count"] < data["total_module_count"]
        assert any("业务模块" in issue["message"] for issue in data["issues"])

    @pytest.mark.asyncio
    async def test_no_requirement_analysis(self, tool, base_context, full_test_plan_content):
        """No requirement_analysis → skip module coverage check."""
        base_context.test_plan_content = full_test_plan_content
        base_context.requirement_analysis = None

        result = await tool.run({}, base_context)
        data = result["data"]
        assert data["total_module_count"] == 0
        assert data["covered_module_count"] == 0


# ── Empty sections tests ───────────────────────────────────────────────


class TestEmptySections:
    """Test empty section detection."""

    @pytest.mark.asyncio
    async def test_empty_content_detected(self, tool, base_context):
        """Empty content should be detected."""
        base_context.test_plan_content = {
            "generated_sections": 2,
            "kept_sections": 0,
            "manual_sections": 0,
            "section_package": {
                "generated_sections": [
                    {"section_id": "sec_001", "title": "项目概述", "content": ""},
                    {"section_id": "sec_002", "title": "测试目标", "content": "有内容的章节"},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        }

        result = await tool.run({}, base_context)
        data = result["data"]
        assert len(data["empty_sections"]) == 1
        assert data["empty_sections"][0]["section_title"] == "项目概述"

    @pytest.mark.asyncio
    async def test_no_empty_sections(self, tool, base_context, full_test_plan_content):
        """No empty sections → empty_sections is empty."""
        base_context.test_plan_content = full_test_plan_content

        result = await tool.run({}, base_context)
        data = result["data"]
        assert len(data["empty_sections"]) == 0


# ── Placeholder detection tests ────────────────────────────────────────


class TestPlaceholderDetection:
    """Test placeholder pattern detection."""

    @pytest.mark.asyncio
    async def test_placeholder_detected(self, tool, base_context):
        """Placeholder patterns should be detected."""
        base_context.test_plan_content = {
            "generated_sections": 2,
            "kept_sections": 0,
            "manual_sections": 0,
            "section_package": {
                "generated_sections": [
                    {"section_id": "sec_001", "title": "项目概述", "content": "正常内容"},
                    {"section_id": "sec_007", "title": "测试进度", "content": "【待补充】请根据项目计划补充"},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        }

        result = await tool.run({}, base_context)
        data = result["data"]
        assert len(data["placeholder_sections"]) == 1
        assert data["placeholder_sections"][0]["section_title"] == "测试进度"

    @pytest.mark.asyncio
    async def test_no_placeholders(self, tool, base_context, full_test_plan_content):
        """Content without placeholders → placeholder_sections is empty."""
        # Remove the placeholder from full_test_plan_content
        for section in full_test_plan_content["section_package"]["generated_sections"]:
            if section["title"] == "测试进度":
                section["content"] = "测试进度安排如下：第一阶段..."

        base_context.test_plan_content = full_test_plan_content

        result = await tool.run({}, base_context)
        data = result["data"]
        assert len(data["placeholder_sections"]) == 0

    def test_strategy_text_does_not_match_standalone_omitted_marker(self):
        assert ResultReviewTool._detect_placeholder(
            "本章节采用分层验证策略，覆盖功能、接口、安全和性能。"
        ) is None

    def test_standalone_omitted_marker_is_placeholder(self):
        assert ResultReviewTool._detect_placeholder("略。") is not None


# ── Too-short content tests ────────────────────────────────────────────


class TestTooShortContent:
    """Test too-short content detection."""

    @pytest.mark.asyncio
    async def test_short_content_detected(self, tool, base_context):
        """Short content should generate a warning."""
        base_context.test_plan_content = {
            "generated_sections": 1,
            "kept_sections": 0,
            "manual_sections": 0,
            "section_package": {
                "generated_sections": [
                    {"section_id": "sec_001", "title": "项目概述", "content": "略"},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        }

        result = await tool.run({}, base_context)
        data = result["data"]
        assert any("过短" in issue["message"] for issue in data["issues"])


# ── Review level tests ─────────────────────────────────────────────────


class TestReviewLevel:
    """Test review level determination (passed/warning/failed)."""

    @pytest.mark.asyncio
    async def test_passed_level(self, tool, base_context, full_test_plan_content, template_structure, requirement_analysis):
        """Complete content without placeholders → passed."""
        # Replace all sections with longer content to pass the too-short check
        full_test_plan_content["section_package"]["generated_sections"] = [
            {"section_id": "sec_001", "title": "项目概述", "content": "本项目为智慧校园综合服务平台，主要面向学校师生和管理人员提供统一服务入口。系统采用微服务架构，包括用户管理、校园服务、消息通知等核心功能模块，支持多终端访问。"},
            {"section_id": "sec_002", "title": "测试目标", "content": "验证系统功能正确性、性能稳定性和安全可靠性，确保系统满足需求规格说明书中的各项要求。通过系统测试发现潜在缺陷，保证系统上线质量。"},
            {"section_id": "sec_003", "title": "测试范围", "content": "本次测试覆盖用户管理、校园服务、消息通知、权限管理、数据统计等核心业务模块。包括功能测试、性能测试、安全测试和兼容性测试。"},
            {"section_id": "sec_004", "title": "测试策略", "content": "采用黑盒测试为主，结合白盒测试进行代码覆盖率分析。测试阶段包括单元测试、集成测试、系统测试和验收测试，确保全面覆盖。"},
            {"section_id": "sec_007", "title": "测试进度", "content": "测试进度安排如下：第一阶段需求分析与测试计划制定，第二阶段测试用例设计与评审，第三阶段测试执行与缺陷跟踪，第四阶段回归测试与验收。"},
            {"section_id": "sec_008", "title": "准入准出标准", "content": "准入标准：所有开发任务完成、代码评审通过、测试环境部署完成、测试数据准备就绪。准出标准：所有高优先级缺陷修复、测试用例执行率≥95%、通过率≥90%。"},
            {"section_id": "sec_009", "title": "风险分析", "content": "主要风险包括：1）需求变更频繁导致测试范围扩大；2）第三方接口不稳定影响集成测试；3）测试环境资源不足影响测试进度；4）人员变动影响项目连续性。"},
        ]

        base_context.test_plan_content = full_test_plan_content
        base_context.template_structure = template_structure
        base_context.requirement_analysis = requirement_analysis

        result = await tool.run({}, base_context)
        data = result["data"]
        assert data["level"] == "passed"
        assert data["passed"] is True

    @pytest.mark.asyncio
    async def test_warning_level_with_placeholder(self, tool, base_context):
        """AI-generated placeholder content blocks export and enters repair."""
        base_context.test_plan_content = {
            "generated_sections": 2,
            "kept_sections": 0,
            "manual_sections": 0,
            "section_package": {
                "generated_sections": [
                    {"section_id": "sec_001", "title": "项目概述", "content": "本项目为智慧校园综合服务平台，提供用户管理、校园服务等核心功能。"},
                    {"section_id": "sec_007", "title": "测试进度", "content": "【待补充】请根据项目计划补充测试进度安排"},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        }

        result = await tool.run({}, base_context)
        data = result["data"]
        assert data["level"] == "failed"
        assert data["passed"] is False
        assert any(
            issue.get("kind") == "placeholder"
            and issue.get("severity") == "block"
            for issue in data["review_issues"]
        )

    @pytest.mark.asyncio
    async def test_failed_level_with_empty_and_missing(self, tool, base_context):
        """Empty sections + many missing sections → failed."""
        base_context.test_plan_content = {
            "generated_sections": 1,
            "kept_sections": 0,
            "manual_sections": 0,
            "section_package": {
                "generated_sections": [
                    {"section_id": "sec_001", "title": "项目概述", "content": ""},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        }

        result = await tool.run({}, base_context)
        data = result["data"]
        assert data["level"] == "failed"
        assert data["passed"] is False


# ── Input validation tests ─────────────────────────────────────────────


class TestInputValidation:
    """Test input validation and error handling."""

    @pytest.mark.asyncio
    async def test_missing_test_plan_content(self, tool, base_context):
        """Missing test_plan_content → error."""
        base_context.test_plan_content = None

        result = await tool.run({}, base_context)
        assert result["success"] is False
        assert result["error"]["code"] == "REVIEW_CONTENT_MISSING"

    @pytest.mark.asyncio
    async def test_empty_section_package(self, tool, base_context):
        """Empty section_package → should handle gracefully."""
        base_context.test_plan_content = {
            "generated_sections": 0,
            "kept_sections": 0,
            "manual_sections": 0,
            "section_package": {},
        }

        result = await tool.run({}, base_context)
        assert result["success"] is True
        data = result["data"]
        assert data["generated_section_count"] == 0


# ── Output format tests ────────────────────────────────────────────────


class TestOutputFormat:
    """Test output format compliance with Agent tool definition."""

    @pytest.mark.asyncio
    async def test_output_has_required_fields(self, tool, base_context, full_test_plan_content):
        """Output must have all required fields per Agent tool definition 13.6."""
        base_context.test_plan_content = full_test_plan_content

        result = await tool.run({}, base_context)
        assert result["success"] is True
        data = result["data"]

        # Required fields per Agent tool definition
        assert "passed" in data
        assert "level" in data
        assert "covered_module_count" in data
        assert "total_module_count" in data
        assert "generated_section_count" in data
        assert "kept_section_count" in data
        assert "manual_section_count" in data
        assert "missing_sections" in data
        assert "empty_sections" in data
        assert "placeholder_sections" in data
        assert "issues" in data
        assert "suggestions" in data

    @pytest.mark.asyncio
    async def test_level_enum_values(self, tool, base_context, full_test_plan_content):
        """Level must be one of: passed, warning, failed."""
        base_context.test_plan_content = full_test_plan_content

        result = await tool.run({}, base_context)
        data = result["data"]
        assert data["level"] in ("passed", "warning", "failed")

    @pytest.mark.asyncio
    async def test_issues_structure(self, tool, base_context):
        """Issues must have level and message fields."""
        base_context.test_plan_content = {
            "generated_sections": 1,
            "kept_sections": 0,
            "manual_sections": 0,
            "section_package": {
                "generated_sections": [
                    {"section_id": "sec_001", "title": "项目概述", "content": ""},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        }

        result = await tool.run({}, base_context)
        data = result["data"]
        for issue in data["issues"]:
            assert "level" in issue
            assert "message" in issue
            assert issue["level"] in ("warning", "error")

    @pytest.mark.asyncio
    async def test_result_written_to_context(self, tool, base_context, full_test_plan_content):
        """Review result must be written to context.review_result."""
        base_context.test_plan_content = full_test_plan_content

        await tool.run({}, base_context)
        assert base_context.review_result is not None
        assert "passed" in base_context.review_result
        assert "level" in base_context.review_result


# ── BUG FIX 2026-08-19: template_backfill 应读 content 而不是 tables ─────


class TestTemplateBackfill:
    """回归测试: template_backfill 规则必须读取 section["content"]
    而不是不存在的 section["tables"]。"""

    @staticmethod
    def _rule(section_id="body_61_level_2"):
        return {
            "id": f"no_tables_for_{section_id}",
            "kind": "template_backfill",
            "section_id": section_id,
            "expected_no_tables": True,
            "severity": "block",
        }

    def test_single_table_in_content_triggers_issue(self):
        rule = self._rule()
        sections = [
            {
                "section_id": "body_61_level_2",
                "content": [{"col1": "v1", "col2": "v2"}, {"col1": "v3", "col2": "v4"}],
            }
        ]
        issues = ResultReviewTool._check_template_backfill(sections, rule)
        assert len(issues) == 1
        assert issues[0]["kind"] == "template_backfill"
        assert issues[0]["section_id"] == "body_61_level_2"
        assert issues[0]["evidence"]["table_count"] == 1

    def test_multi_table_in_content_triggers_issue(self):
        rule = self._rule()
        sections = [
            {
                "section_id": "body_61_level_2",
                "content": [
                    [{"a": 1, "b": 2}],
                    [{"c": 3, "d": 4}],
                ],
            }
        ]
        issues = ResultReviewTool._check_template_backfill(sections, rule)
        assert len(issues) == 1
        assert issues[0]["evidence"]["table_count"] == 2

    def test_plain_text_content_does_not_trigger(self):
        rule = self._rule()
        sections = [
            {
                "section_id": "body_61_level_2",
                "content": "这是纯文本策略说明，无表格。",
            }
        ]
        issues = ResultReviewTool._check_template_backfill(sections, rule)
        assert issues == []


class TestBackfillContract:
    """ResultReviewTool must block JSON that cannot be backfilled to Word."""

    @staticmethod
    def _rule(section_id="body_61_level_2"):
        return TestTemplateBackfill._rule(section_id)

    def test_no_table_placeholder_blocks_top_level_tables(self):
        issues = ResultReviewTool._check_backfill_contract(
            generated_sections=[
                {
                    "section_id": "strategy_2",
                    "table_indexes": [],
                    "table_schemas": [],
                    "content": "正文",
                    "tables": [[{"col1": "v"}]],
                }
            ]
        )

        assert len(issues) == 1
        assert issues[0]["kind"] == "backfill_contract"
        assert issues[0]["severity"] == "block"
        assert issues[0]["section_id"] == "strategy_2"

    def test_single_table_template_blocks_multiple_table_payloads(self):
        issues = ResultReviewTool._check_backfill_contract(
            generated_sections=[
                {
                    "section_id": "strategy_2",
                    "table_indexes": [0],
                    "table_schemas": [{"headers": ["模块", "策略"]}],
                    "content": [
                        [{"模块": "登录", "策略": "功能测试"}],
                        [{"模块": "权限", "策略": "安全测试"}],
                    ],
                }
            ]
        )

        assert len(issues) == 1
        assert "模板声明了 1 个表格占位" in issues[0]["message"]
        assert issues[0]["evidence"]["actual_table_count"] == 2

    def test_multi_table_template_blocks_missing_table_payloads(self):
        issues = ResultReviewTool._check_backfill_contract(
            generated_sections=[
                {
                    "section_id": "strategy_2",
                    "table_indexes": [0, 1],
                    "table_schemas": [
                        {"headers": ["模块", "策略"]},
                        {"headers": ["风险", "措施"]},
                    ],
                    "content": [[{"模块": "登录", "策略": "功能测试"}]],
                }
            ]
        )

        assert len(issues) == 1
        assert "模板声明了 2 个表格占位" in issues[0]["message"]
        assert issues[0]["evidence"]["actual_table_count"] == 1

    def test_single_table_template_blocks_missing_table_payload(self):
        issues = ResultReviewTool._check_backfill_contract(
            generated_sections=[
                {
                    "section_id": "strategy_2",
                    "table_indexes": [0],
                    "table_schemas": [{"headers": ["模块", "策略"]}],
                    "content": "TEST_FAULT_missing_table_data",
                }
            ]
        )

        assert len(issues) == 1
        assert issues[0]["kind"] == "backfill_contract"
        assert "模板声明了 1 个表格占位" in issues[0]["message"]
        assert issues[0]["evidence"]["actual_table_count"] == 0

    def test_single_table_template_blocks_invalid_row_shape(self):
        issues = ResultReviewTool._check_backfill_contract(
            generated_sections=[
                {
                    "section_id": "strategy_2",
                    "table_indexes": [0],
                    "table_schemas": [{"headers": ["模块", "策略"]}],
                    "content": [["TEST_FAULT_row_is_not_object"]],
                }
            ]
        )

        assert len(issues) == 1
        assert issues[0]["kind"] == "backfill_contract"
        assert "模板声明了 1 个表格占位" in issues[0]["message"]

    def test_table_header_mismatch_blocks_export(self):
        issues = ResultReviewTool._check_backfill_contract(
            generated_sections=[
                {
                    "section_id": "strategy_2",
                    "table_indexes": [0],
                    "table_schemas": [{"headers": ["模块", "策略"]}],
                    "content": [{"模块": "登录", "错误列": "功能测试"}],
                }
            ]
        )

        assert len(issues) == 1
        assert "字段与模板表头不一致" in issues[0]["message"]
        assert issues[0]["evidence"]["expected_headers"] == ["模块", "策略"]

    def test_matching_table_payload_has_no_backfill_issue(self):
        issues = ResultReviewTool._check_backfill_contract(
            generated_sections=[
                {
                    "section_id": "strategy_2",
                    "table_indexes": [0, 1],
                    "table_schemas": [
                        {"headers": ["模块", "策略"]},
                        {"headers": ["风险", "措施"]},
                    ],
                    "content": [
                        [{"模块": "登录", "策略": "功能测试"}],
                        [{"风险": "数据泄露", "措施": "权限校验"}],
                    ],
                }
            ]
        )

        assert issues == []

    @pytest.mark.asyncio
    async def test_backfill_contract_runs_without_review_standard(self):
        ctx = AgentContext(task_id="t", conversation_id="c", user_id="u")
        ctx.review_standard = None
        ctx.test_plan_content = {
            "section_package": {
                "generated_sections": [
                    {
                        "section_id": "strategy_2",
                        "title": "测试策略",
                        "table_indexes": [],
                        "table_schemas": [],
                        "content": [{"模块": "登录", "策略": "功能测试"}],
                    }
                ],
                "keep_sections": [],
                "manual_sections": [],
            }
        }

        result = await ResultReviewTool().run({}, ctx)

        assert result["success"] is True
        data = result["data"]
        assert data["level"] == "failed"
        assert any(
            issue.get("kind") == "backfill_contract"
            for issue in data["review_issues"]
        )

    def test_plain_text_list_does_not_trigger(self):
        rule = self._rule()
        sections = [
            {
                "section_id": "body_61_level_2",
                "content": ["行一", "行二", "行三"],
            }
        ]
        issues = ResultReviewTool._check_template_backfill(sections, rule)
        assert issues == []

    def test_section_id_mismatch_does_not_trigger(self):
        rule = self._rule(section_id="body_99_level_2")
        sections = [
            {
                "section_id": "body_61_level_2",
                "content": [{"col1": "v1"}],
            }
        ]
        issues = ResultReviewTool._check_template_backfill(sections, rule)
        assert issues == []


class TestContentIsTableHelper:
    """_content_is_table / _extract_table_rows 形态判定回归。"""

    def test_list_of_dict_is_table(self):
        assert ResultReviewTool._content_is_table([{"a": 1}, {"a": 2}]) is True

    def test_list_of_list_of_dict_is_table(self):
        assert ResultReviewTool._content_is_table([[{"a": 1}], [{"b": 2}]]) is True

    def test_plain_string_is_not_table(self):
        assert ResultReviewTool._content_is_table("plain text") is False

    def test_list_of_string_is_not_table(self):
        assert ResultReviewTool._content_is_table(["line1", "line2"]) is False

    def test_none_is_not_table(self):
        assert ResultReviewTool._content_is_table(None) is False

    def test_empty_list_is_not_table(self):
        assert ResultReviewTool._content_is_table([]) is False


class TestTableHeaderWhitelistContent:
    """_check_table_header_whitelist 必须走 content 路径。"""

    @staticmethod
    def _rule():
        return {
            "id": "whitelist_v1",
            "kind": "table_header_whitelist",
            "section_id": "body_70_level_2",
            "whitelist": ["用例编号", "用例名称"],
            "severity": "block",
        }

    def test_extra_header_in_content_is_blocked(self):
        rule = self._rule()
        sections = [
            {
                "section_id": "body_70_level_2",
                "content": [
                    {"用例编号": "1", "用例名称": "登录", "非法键": "x"}
                ],
            }
        ]
        issues = ResultReviewTool._check_table_header_whitelist(sections, rule)
        assert len(issues) == 1
        assert "非法键" in issues[0]["message"]

    def test_whitelist_only_header_passes(self):
        rule = self._rule()
        sections = [
            {
                "section_id": "body_70_level_2",
                "content": [{"用例编号": "1", "用例名称": "登录"}],
            }
        ]
        issues = ResultReviewTool._check_table_header_whitelist(sections, rule)
        assert issues == []

    def test_section_id_mismatch_skips_check(self):
        rule = self._rule()
        sections = [
            {
                "section_id": "body_99_level_2",
                "content": [{"用例编号": "1", "非法键": "x"}],
            }
        ]
        issues = ResultReviewTool._check_table_header_whitelist(sections, rule)
        assert issues == []
