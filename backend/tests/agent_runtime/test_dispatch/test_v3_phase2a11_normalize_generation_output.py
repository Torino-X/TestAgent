"""Phase 2.9A.11 生成结果真实 Schema 与规范化契约测试。

覆盖:

A. 规范化函数支持多种 schema 变体(section_package / 顶层 generated_sections / legacy)
B. content 可以是 str / list[dict] / dict 三种类型都被识别
C. chapter_count=16 + total_word_count=11147 的真实场景通过校验
D. content 为空(全部空字符串)→ GENERATION_OUTPUT_INVALID
E. tool_failed 事件含 tool_name=TestPlanGeneratorTool
F. ResultReviewTool 读相同规范化结构(支持 str/list/dict)
G. normalize 函数单一入口 — 节点不重复字段兼容
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_any_unsuccessful_generator_envelope_preserves_the_original_failure():
    """A recoverable tool error is still a failed generation attempt.

    The node must not normalize it as an empty successful result, otherwise the
    real CE/error code is replaced by GENERATION_OUTPUT_INVALID.
    """
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        _core_failed,
    )

    assert _core_failed({
        "success": False,
        "error": {
            "code": "CONTEXT_ENGINE_AGENT_DISABLED",
            "recoverable": True,
        },
    }) is True


# ── A. 规范化函数支持 schema 变体 ─────────────────────────────────────


def test_normalize_handles_section_package_v1():
    """F019 标准结构(data.section_package.generated_sections)。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    envelope = {
        "success": True,
        "data": {
            "generated_sections": 2,
            "kept_sections": 0,
            "manual_sections": 0,
            "total_word_count": 11147,
            "tables_generated": 0,
            "section_package": {
                "schema_version": 1,
                "generated_sections": [
                    {
                        "field": "section_001",
                        "section_id": "sec_001",
                        "title": "项目概述",
                        "content": "本项目是一个测试方案生成器。" * 10,
                    },
                    {
                        "field": "section_002",
                        "section_id": "sec_002",
                        "title": "测试目标",
                        "content": "本项目测试目标包括功能完整性。" * 10,
                    },
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        },
    }

    out = normalize_test_plan_generation_output(envelope)
    assert out["schema_variant"] == "section_package_v1"
    assert out["normalized_section_count"] == 2
    assert out["non_empty_section_count"] == 2
    assert out["normalized_content_length"] > 0
    assert out["total_word_count"] == 11147


def test_normalize_handles_top_level_v1():
    """顶层 generated_sections 路径(无 section_package)。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    envelope = {
        "success": True,
        "data": {
            "generated_sections": [
                {"section_id": "s1", "title": "S1", "content": "Real content " * 20},
            ],
            "kept_sections": [],
            "manual_sections": [],
            "total_word_count": 200,
        },
    }

    out = normalize_test_plan_generation_output(envelope)
    assert out["schema_variant"] == "top_level_v1"
    assert out["normalized_section_count"] == 1
    assert out["non_empty_section_count"] == 1


def test_normalize_handles_content_as_list_of_dicts():
    """content 是 list[dict](多表格行)→ 累积每个 dict 的 value 字符串长度。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    envelope = {
        "success": True,
        "data": {
            "section_package": {
                "generated_sections": [
                    {
                        "section_id": "s1",
                        "title": "测试用例",
                        "content": [
                            {"用例编号": "TC001", "用例标题": "登录测试"},
                            {"用例编号": "TC002", "用例标题": "登出测试"},
                            {"用例编号": "TC003", "用例标题": "权限验证"},
                        ],
                    },
                ],
            },
        },
    }

    out = normalize_test_plan_generation_output(envelope)
    assert out["normalized_section_count"] == 1
    # 3 行 × 多个字段,剥除空白后的总长度应 > 0
    assert out["non_empty_section_count"] == 1
    assert out["normalized_content_length"] > 0


def test_normalize_handles_content_as_list_of_lists_of_dicts():
    """content 是 list[list[dict]](多表格,outer per table)→ 递归累计。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    envelope = {
        "success": True,
        "data": {
            "section_package": {
                "generated_sections": [
                    {
                        "section_id": "s1",
                        "title": "Multi Tables",
                        "content": [
                            [
                                {"k1": "row1_table1", "k2": "val"},
                                {"k1": "row2_table1", "k2": "val2"},
                            ],
                            [
                                {"k1": "row1_table2", "k2": "val3"},
                            ],
                        ],
                    },
                ],
            },
        },
    }

    out = normalize_test_plan_generation_output(envelope)
    assert out["non_empty_section_count"] == 1
    assert out["normalized_content_length"] > 0


# ── C. 真实场景(chapter_count=16, total_word_count=11147) ───────────


def test_normalize_passes_with_real_16_chapter_scenario():
    """用户真实场景:chapter_count=16, total_word_count=11147 — 通过校验。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    # 16 章,每章 50 字文本 + 部分章是表格(列表行)
    generated = []
    for i in range(16):
        sec_id = f"sec_{i+1:03d}"
        title = f"章节{i+1}"
        if i % 3 == 0:
            # 表格章 — list[dict]
            content = [
                {"字段1": f"值1_{i}", "字段2": f"值2_{i}"},
                {"字段1": f"值1_{i}_b", "字段2": f"值2_{i}_b"},
            ]
        else:
            # 文本章
            content = f"章节{i+1}的内容包含详细的描述信息。" * 5
        generated.append({
            "field": f"section_{i+1:03d}",
            "section_id": sec_id,
            "title": title,
            "content": content,
        })

    envelope = {
        "success": True,
        "data": {
            "generated_sections": 16,
            "kept_sections": 0,
            "manual_sections": 0,
            "total_word_count": 11147,
            "tables_generated": 5,
            "section_package": {
                "schema_version": 1,
                "generated_sections": generated,
                "keep_sections": [],
                "manual_sections": [],
            },
        },
    }

    out = normalize_test_plan_generation_output(envelope)
    assert out["schema_variant"] == "section_package_v1"
    assert out["normalized_section_count"] == 16
    assert out["non_empty_section_count"] == 16
    assert out["normalized_content_length"] > 0
    # total_word_count 保留为辅助指标
    assert out["total_word_count"] == 11147
    assert out["tables_generated"] == 5


# ── D. 内容为空仍报 GENERATION_OUTPUT_INVALID ────────────────────────────


def test_normalize_all_empty_string_content_marks_non_empty_zero():
    """所有 content 是空字符串 → non_empty_section_count=0。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    envelope = {
        "success": True,
        "data": {
            "section_package": {
                "generated_sections": [
                    {"section_id": "s1", "title": "S1", "content": ""},
                    {"section_id": "s2", "title": "S2", "content": "   "},
                ],
            },
        },
    }

    out = normalize_test_plan_generation_output(envelope)
    assert out["normalized_section_count"] == 2
    assert out["non_empty_section_count"] == 0
    assert out["normalized_content_length"] == 0


def test_normalize_section_with_no_title_and_no_id_excluded():
    """章节既无 title 也无 section_id → 不计入 normalized_section_count。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    envelope = {
        "success": True,
        "data": {
            "section_package": {
                "generated_sections": [
                    {"section_id": "s1", "title": "Valid", "content": "Real"},
                    {"content": "No title no id"},  # 应被排除
                ],
            },
        },
    }

    out = normalize_test_plan_generation_output(envelope)
    assert out["normalized_section_count"] == 1
    assert out["non_empty_section_count"] == 1


# ── E. tool_failed 事件含 tool_name=TestPlanGeneratorTool ──────────────


@pytest.mark.asyncio
async def test_tool_failed_event_contains_test_plan_generator_tool_name():
    """GENERATION_OUTPUT_INVALID 事件必须含 tool_name=TestPlanGeneratorTool。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        generate_test_plan_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "requirement_analysis": {"text_content": "ok"},
        "template_structure": {"sections": [{"title": "S1"}]},
        "section_suggestions": {"sections": [{"section_id": "s1"}]},
        "section_confirm_config": {"sections": [{"section_id": "s1"}]},
        "confirmed_sections": [{"section_id": "s1"}],
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {
            "section_package": {"generated_sections": [{"content": ""}]},
            "total_word_count": 100,
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    await generate_test_plan_node(state, ctx=mock_ctx)

    stage_failed_calls = [
        c for c in mock_ctx.event_sink.emit.call_args_list
        if c.kwargs.get("event_type") == "stage_failed"
    ]
    assert len(stage_failed_calls) >= 1
    payload = stage_failed_calls[0].kwargs.get("payload", {})
    assert payload.get("tool_name") == "TestPlanGeneratorTool"
    assert payload.get("failed_stage") == "generate_test_plan"


@pytest.mark.asyncio
async def test_generate_all_empty_with_schema_issues_routes_to_review():
    """JSON 严重截断时，空 section_package + schema_issues 仍必须进入审查。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        generate_test_plan_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "requirement_analysis": {"text_content": "ok"},
        "template_structure": {"sections": [{"title": "S1"}]},
        "section_suggestions": {"sections": [{"section_id": "section_1"}]},
        "section_confirm_config": {"sections": [{"section_id": "section_1"}]},
        "confirmed_sections": [{"section_id": "section_1"}],
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "tool_call_id": "tc-gen",
        "data": {
            "section_package": {
                "generated_sections": [],
                "keep_sections": [],
                "manual_sections": [],
            },
            "schema_issues": [
                {
                    "kind": "missing_field",
                    "field": "section_1",
                    "severity": "block",
                    "message": "JSON 截断导致章节缺失",
                },
            ],
            "generated_sections": 0,
            "kept_sections": 0,
            "manual_sections": 0,
            "total_word_count": 4716,
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    assert result.get("task_status") != "failed"
    assert result["next_node"] == "review_step"
    assert result["test_plan_content"]["schema_issues"][0]["field"] == "section_1"
    assert result["test_plan_content"]["generated_sections"] == []


@pytest.mark.asyncio
async def test_review_tool_failed_event_contains_result_review_tool_name():
    """REVIEW_CONTENT_MISSING 事件必须含 tool_name=ResultReviewTool。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        review_result_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    await review_result_node(state, ctx=mock_ctx)

    stage_failed_calls = [
        c for c in mock_ctx.event_sink.emit.call_args_list
        if c.kwargs.get("event_type") == "stage_failed"
    ]
    assert len(stage_failed_calls) >= 1
    payload = stage_failed_calls[0].kwargs.get("payload", {})
    assert payload.get("tool_name") == "ResultReviewTool"


@pytest.mark.asyncio
async def test_review_accepts_empty_generated_sections_when_schema_issues_exist():
    """ResultReviewTool 边界校验不应拦截可修复的 schema_issues 输入。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        review_result_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "test_plan_content": {
            "schema_variant": "section_package_v1",
            "section_package": {
                "generated_sections": [],
                "keep_sections": [],
                "manual_sections": [],
            },
            "generated_sections": [],
            "kept_sections": [],
            "manual_sections": [],
            "schema_issues": [
                {
                    "kind": "missing_field",
                    "field": "section_1",
                    "severity": "block",
                    "message": "JSON 截断导致章节缺失",
                },
            ],
        },
        "template_structure": {
            "generation_config": {
                "ai_fields": [
                    {
                        "field": "section_1",
                        "section_id": "body_10_level_1",
                        "title": "章节一",
                        "clean_title": "章节一",
                        "level": 1,
                        "order": 1,
                        "path": ["章节一"],
                        "paragraph_index": 8,
                        "body_start_index": 10,
                        "body_end_index": 12,
                        "table_indexes": [],
                        "table_schemas": [],
                        "source": "outline",
                        "target_kind": "leaf_section",
                    },
                ],
            },
        },
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "tool_call_id": "tc-review",
        "data": {
            "level": "failed",
            "passed": False,
            "review_issues": [
                {
                    "severity": "block",
                    "section_id": "section_1",
                    "message": "JSON 截断导致章节缺失",
                },
            ],
            "block_issues": [
                {
                    "section_id": "section_1",
                    "message": "JSON 截断导致章节缺失",
                },
            ],
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await review_result_node(state, ctx=mock_ctx)

    assert result.get("task_status") != "failed"
    mock_adapter.execute.assert_awaited_once()
    inputs = mock_adapter.execute.await_args.kwargs["inputs"]
    generated = inputs["test_plan_content"]["generated_sections"]
    assert isinstance(generated, list)
    assert generated[0]["field"] == "section_1"
    assert generated[0]["section_id"] == "body_10_level_1"
    assert generated[0]["body_start_index"] == 10
    assert generated[0]["body_end_index"] == 12
    patched_content = result["test_plan_content"]
    patched_section = patched_content["section_package"]["generated_sections"][0]
    assert patched_section["field"] == "section_1"
    assert patched_section["section_id"] == "body_10_level_1"
    assert patched_section["body_start_index"] == 10
    assert patched_section["body_end_index"] == 12


def test_missing_section_placeholders_dedupe_field_and_section_id_aliases():
    """同一缺失章节的 field/section_id 双别名不应制造两个 placeholder。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        _inject_missing_section_placeholders,
    )

    content = {
        "section_package": {
            "generated_sections": [],
            "keep_sections": [],
            "manual_sections": [],
        },
        "generated_sections": [],
    }
    template_structure = {
        "generation_config": {
            "ai_fields": [
                {
                    "field": "section_14",
                    "section_id": "body_67_level_1",
                    "title": "14 缺陷管理流程",
                    "body_start_index": 67,
                    "body_end_index": 72,
                },
            ],
        },
    }
    schema_issues = [
        {"kind": "missing_field", "field": "body_67_level_1"},
        {"kind": "missing_field", "field": "section_14"},
    ]

    out = _inject_missing_section_placeholders(
        content,
        schema_issues,
        template_structure,
    )

    generated = out["section_package"]["generated_sections"]
    assert len(generated) == 1
    assert generated[0]["field"] == "section_14"
    assert generated[0]["section_id"] == "body_67_level_1"
    assert generated[0]["body_start_index"] == 67


# ── G. 生成成功路径 → test_plan_content 写入规范化结构 ──────────────────


@pytest.mark.asyncio
async def test_generate_success_writes_normalized_test_plan_content():
    """生成成功 → test_plan_content 含规范化结构(非 envelope.data 原始 dict)。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        generate_test_plan_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "requirement_analysis": {"text_content": "ok"},
        "template_structure": {"sections": [{"title": "S1"}]},
        "section_suggestions": {"sections": [{"section_id": "s1"}]},
        "section_confirm_config": {"sections": [{"section_id": "s1"}]},
        "confirmed_sections": [{"section_id": "s1"}],
        "completed_nodes": [],
    }

    real_section_package = {
        "schema_version": 1,
        "generated_sections": [
            {"section_id": "s1", "title": "S1", "content": "Real content " * 50},
            {"section_id": "s2", "title": "S2", "content": "More content " * 50},
        ],
        "keep_sections": [],
        "manual_sections": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {
            "generated_sections": 2,
            "kept_sections": 0,
            "manual_sections": 0,
            "total_word_count": 1200,
            "tables_generated": 0,
            "section_package": real_section_package,
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    tpc = result["test_plan_content"]
    # Phase 2.9A.11:Graph State 含规范化结构
    assert tpc["schema_variant"] == "section_package_v1"
    assert tpc["section_package"] == real_section_package
    assert tpc["generated_sections"] == real_section_package["generated_sections"]
    assert tpc["normalized_section_count"] == 2
    assert tpc["non_empty_section_count"] == 2
    assert tpc["normalized_content_length"] > 0


# ── F. ResultReviewTool 读相同规范化结构 ────────────────────────────────


@pytest.mark.asyncio
async def test_review_reads_normalized_structure_with_list_content():
    """Review 节点读规范化结构,且 content 是 list[dict] 时仍识别非空。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        review_result_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "test_plan_content": {
            "schema_variant": "section_package_v1",
            "section_package": {
                "generated_sections": [
                    {
                        "section_id": "s1",
                        "title": "测试用例",
                        "content": [
                            {"k1": "val1", "k2": "val2"},
                            {"k1": "val3", "k2": "val4"},
                        ],
                    },
                ],
            },
            "generated_sections": [
                {
                    "section_id": "s1",
                    "title": "测试用例",
                    "content": [
                        {"k1": "val1", "k2": "val2"},
                        {"k1": "val3", "k2": "val4"},
                    ],
                },
            ],
            "kept_sections": [],
            "manual_sections": [],
            "total_word_count": 100,
            "tables_generated": 0,
            "normalized_section_count": 1,
            "non_empty_section_count": 1,
            "normalized_content_length": 50,
        },
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {"level": "passed", "passed": True, "issues": []},
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await review_result_node(state, ctx=mock_ctx)

    # 不应 fail — list[dict] content 被正确识别为非空
    assert result.get("task_status") != "failed"


# ── 2.9A.X: schema_issues 透传 — 防止 ResultReviewTool 审查永远通过 ──


def test_normalize_propagates_schema_issues_section_package_v1():
    """Phase 2.9A.X bug fix: TestPlanGeneratorTool 在 envelope.data.schema_issues
    写入表头不符 / 截断字段。修复前 normalize 函数丢了 schema_issues，
    ResultReviewTool 7c 段永远读 None，审查永远通过 — RepairAgent 永远
    不被触发。

    修复后：schema_issues 必须出现在 normalized output 顶层。
    """
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    envelope = {
        "success": True,
        "data": {
            "generated_sections": 1,
            "kept_sections": 0,
            "manual_sections": 0,
            "total_word_count": 100,
            "tables_generated": 1,
            "section_package": {
                "schema_version": 1,
                "generated_sections": [
                    {
                        "field": "section_001",
                        "section_id": "section_001",
                        "title": "项目概述",
                        "content": "内容正常",
                    },
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
            "schema_issues": [
                {
                    "kind": "header_mismatch",
                    "field": "section_001",
                    "expected": "作者",
                    "actual": "TEST_FAULT_作者",
                    "severity": "block",
                    "message": "字段「section_001」表头键名不符",
                },
            ],
        },
    }

    out = normalize_test_plan_generation_output(envelope)

    # 关键断言：schema_issues 必须在 normalized 顶层
    assert "schema_issues" in out, (
        "schema_issues 字段丢失！ResultReviewTool 7c 段将无法触发，"
        "审查永远通过，RepairAgent 永远不执行"
    )
    assert len(out["schema_issues"]) == 1
    assert out["schema_issues"][0]["kind"] == "header_mismatch"
    assert out["schema_issues"][0]["field"] == "section_001"


def test_normalize_propagates_generation_recovery_for_json_truncation():
    """JSON 截断恢复元数据必须穿过 normalize 进入 Graph State。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    recovery = {
        "kind": "json_truncated",
        "mode": "complete",
        "missing_fields": ["body_14_level_1", "body_18_level_1"],
        "missing_section_ids": ["body_14_level_1", "body_18_level_1"],
        "missing_count": 2,
        "requires_bulk_repair": True,
    }
    envelope = {
        "success": True,
        "data": {
            "generated_sections": 0,
            "kept_sections": 0,
            "manual_sections": 0,
            "total_word_count": 4658,
            "tables_generated": 0,
            "section_package": {
                "schema_version": 1,
                "generated_sections": [],
                "keep_sections": [],
                "manual_sections": [],
            },
            "schema_issues": [
                {
                    "kind": "missing_field",
                    "field": "body_14_level_1",
                    "section_id": "body_14_level_1",
                    "severity": "block",
                    "source_error": "json_truncated",
                },
            ],
            "generation_recovery": recovery,
        },
    }

    out = normalize_test_plan_generation_output(envelope)

    assert out["generation_recovery"] == recovery


def test_normalize_omits_schema_issues_when_none():
    """无 schema_issues 时不应在 normalized output 留 None / 空 list — 避免下游 if check 触发。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    envelope = {
        "success": True,
        "data": {
            "generated_sections": 1,
            "kept_sections": 0,
            "manual_sections": 0,
            "total_word_count": 50,
            "tables_generated": 0,
            "section_package": {
                "schema_version": 1,
                "generated_sections": [
                    {
                        "field": "section_001",
                        "section_id": "section_001",
                        "title": "项目概述",
                        "content": "正常内容",
                    },
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
            # 无 schema_issues
        },
    }

    out = normalize_test_plan_generation_output(envelope)
    # 无 schema_issues 时 normalize 不应塞 None / 空 list — 避免下游
    # `schema_issues or []` 多余空转。但空 list 也无所谓，下游 `if isinstance(...) and schema_issues` 会跳过。
    # 这里只要求：键不存在 OR 为空 list
    assert out.get("schema_issues") in (None, [], ) or not out.get("schema_issues")


def test_normalize_propagates_schema_issues_top_level_v1():
    """schema_issues 透传对顶层 generated_sections 路径也生效。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        normalize_test_plan_generation_output,
    )

    envelope = {
        "success": True,
        "data": {
            "generated_sections": [
                {"section_id": "s1", "title": "S1", "content": "内容" * 5},
            ],
            "kept_sections": [],
            "manual_sections": [],
            "schema_issues": [
                {
                    "kind": "missing_section",
                    "field": "section_002",
                    "severity": "block",
                    "message": "章节 section_002 缺失",
                },
            ],
        },
    }

    out = normalize_test_plan_generation_output(envelope)
    assert out["schema_variant"] == "top_level_v1"
    assert out.get("schema_issues") and len(out["schema_issues"]) == 1
    assert out["schema_issues"][0]["field"] == "section_002"
