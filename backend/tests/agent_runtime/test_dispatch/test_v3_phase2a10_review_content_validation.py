"""Phase 2.9A.10 生成结果写入 Graph State 与 Review 失败路由契约测试。

覆盖:

A. ToolAdapter._build_proxy 复制 test_plan_content / review_result 等 post-confirm 字段
B. generate_test_plan_node 校验生成内容非空 → GENERATION_OUTPUT_INVALID
C. generate_test_plan_node 写出真实生成数据到 Graph State
D. review_result_node 启动前校验 test_plan_content → REVIEW_CONTENT_MISSING
E. review_result_node 工具执行失败 → fail_task(区分于 review level=warning/failed)
F. route_after_result_review 失败路由
G. Generation 失败 → 不进 review
H. Review 失败 → 不进 export
I. Review 成功 → 进入 export
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── A. ToolAdapter._build_proxy 复制 post-confirm 字段 ────────────────


@pytest.mark.asyncio
async def test_build_proxy_propagates_test_plan_content():
    """graph_state.test_plan_content 必须复制到 proxy.test_plan_content。"""
    from app.agent_runtime.adapters.test_agent_tool_adapter import (
        TestAgentToolAdapter,
    )
    from datetime import datetime
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _null_session():
        yield None

    adapter = TestAgentToolAdapter(
        tool_executor=MagicMock(),
        event_sink=MagicMock(),
        session_factory=lambda: _null_session(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )

    ctx = MagicMock()
    ctx.task_internal_id = 1
    ctx.conversation_internal_id = 1
    ctx.user_internal_id = 1
    ctx.settings_service = None

    # Phase 2.9A.10: graph_state 含 test_plan_content(前序 generation 写入)
    graph_state = {
        "test_plan_content": {
            "section_package": {
                "generated_sections": [
                    {"section_id": "s1", "content": "long content here " * 100}
                ],
            },
        },
    }

    proxy = adapter._build_proxy(
        ctx_runtime=ctx,
        inputs={},
        session=None,
        graph_state=graph_state,
    )

    # proxy 必须拿到 test_plan_content,ResultReviewTool 才不会 REVIEW_CONTENT_MISSING
    assert proxy.test_plan_content == graph_state["test_plan_content"]


@pytest.mark.asyncio
async def test_build_proxy_propagates_review_standard():
    """graph_state.review_standard 必须复制到 proxy.review_standard。"""
    from app.agent_runtime.adapters.test_agent_tool_adapter import (
        TestAgentToolAdapter,
    )
    from datetime import datetime
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _null_session():
        yield None

    adapter = TestAgentToolAdapter(
        tool_executor=MagicMock(),
        event_sink=MagicMock(),
        session_factory=lambda: _null_session(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )

    ctx = MagicMock()
    ctx.task_internal_id = 1
    ctx.conversation_internal_id = 1
    ctx.user_internal_id = 1
    ctx.settings_service = None

    graph_state = {
        "review_standard": {"rules": [{"kind": "forbidden_pattern"}]},
    }

    proxy = adapter._build_proxy(
        ctx_runtime=ctx, inputs={}, session=None, graph_state=graph_state,
    )

    assert proxy.review_standard == {"rules": [{"kind": "forbidden_pattern"}]}


# ── B. generate_test_plan_node 内容校验 ────────────────────────────────


@pytest.mark.asyncio
async def test_generate_test_plan_returns_invalid_when_section_package_empty():
    """生成的 section_package 为空 → GENERATION_OUTPUT_INVALID。"""
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
    # 生成器返回 success 但 section_package 为空
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {
            "generated_sections": 0,
            "section_package": {},
            "total_word_count": 0,
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    assert result["task_status"] == "failed"
    assert result["last_error"]["code"] == "GENERATION_OUTPUT_INVALID"
    # 必须 emit TOOL_FAILED
    emit_calls = mock_ctx.event_sink.emit.call_args_list
    assert any(c.kwargs.get("event_type") == "tool_failed" for c in emit_calls)


@pytest.mark.asyncio
async def test_generate_test_plan_returns_invalid_when_chapter_count_zero():
    """生成的 generated_sections 为空列表 → 失败。"""
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
            "generated_sections": 0,
            "section_package": {"generated_sections": [], "keep_sections": []},
            "total_word_count": 0,
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    assert result["task_status"] == "failed"
    assert result["last_error"]["code"] == "GENERATION_OUTPUT_INVALID"
    # Phase 2.9A.11:字段名从 chapter_count 改为 normalized_section_count
    assert "normalized_section_count=0" in result["last_error"]["message"]


@pytest.mark.asyncio
async def test_generate_test_plan_returns_invalid_when_content_empty_string():
    """所有 generated_sections[i].content 是空字符串 → 失败。"""
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
            "generated_sections": 3,
            "section_package": {
                "generated_sections": [
                    {"section_id": "s1", "content": ""},
                    {"section_id": "s2", "content": "  "},
                    {"section_id": "s3", "content": ""},
                ],
            },
            "total_word_count": 11343,
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    assert result["task_status"] == "failed"
    assert result["last_error"]["code"] == "GENERATION_OUTPUT_INVALID"
    # Phase 2.9A.11:stripped_content_length 改为 normalized_content_length
    assert "normalized_content_length=0" in result["last_error"]["message"]
    assert "declared_total_word_count=11343" in result["last_error"]["message"]


# ── C. generate_test_plan_node 写出真实数据 ────────────────────────────


@pytest.mark.asyncio
async def test_generate_test_plan_returns_real_test_plan_content():
    """生成成功 → state.test_plan_content 含真实 section_package.generated_sections。"""
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
        "generated_sections": [
            {
                "section_id": "s1",
                "title": "项目概述",
                "content": "本项目是一个测试方案生成器。" * 50,
            },
            {
                "section_id": "s2",
                "title": "测试目标",
                "content": "目标是确保所有功能正确。" * 50,
            },
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

    assert result.get("task_status") != "failed"
    assert "test_plan_content" in result
    # Phase 2.9A.11:normalized 结构 — 6 字段 + 3 统计字段
    tpc = result["test_plan_content"]
    assert tpc["schema_variant"] == "section_package_v1"
    assert tpc["section_package"] == real_section_package
    assert tpc["generated_sections"] == real_section_package["generated_sections"]
    assert tpc["kept_sections"] == []
    assert tpc["manual_sections"] == []
    assert tpc["total_word_count"] == 1200
    assert tpc["tables_generated"] == 0
    assert tpc["normalized_section_count"] == 2
    assert tpc["non_empty_section_count"] == 2  # both sections have content
    assert tpc["normalized_content_length"] > 0


# ── D. review_result_node 启动前校验 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_review_result_fails_when_test_plan_content_missing():
    """Graph State 缺 test_plan_content → REVIEW_CONTENT_MISSING。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        review_result_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock()  # 不应被调用
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await review_result_node(state, ctx=mock_ctx)

    assert result["task_status"] == "failed"
    assert result["last_error"]["code"] == "REVIEW_CONTENT_MISSING"
    assert "test_plan_content" in result["last_error"]["message"]
    # adapter 不应被调用
    mock_adapter.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_result_fails_when_section_package_empty():
    """test_plan_content.section_package 为空 → REVIEW_CONTENT_MISSING。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        review_result_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "test_plan_content": {"section_package": {}},
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await review_result_node(state, ctx=mock_ctx)

    assert result["task_status"] == "failed"
    assert result["last_error"]["code"] == "REVIEW_CONTENT_MISSING"


@pytest.mark.asyncio
async def test_review_result_passes_boundary_check_with_real_content():
    """test_plan_content.section_package.generated_sections 非空 → 通过校验 + 调工具。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        review_result_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "test_plan_content": {
            "section_package": {
                "generated_sections": [
                    {"section_id": "s1", "content": "Real content " * 50},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        },
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {
            "level": "passed",
            "passed": True,
            "issues": [],
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await review_result_node(state, ctx=mock_ctx)

    assert result.get("task_status") != "failed"
    assert result.get("review_result", {}).get("level") == "passed"
    mock_adapter.execute.assert_awaited_once()


# ── E. review_result_node 工具执行失败 ────────────────────────────────────


@pytest.mark.asyncio
async def test_review_result_fails_when_tool_execution_fails():
    """ResultReviewTool 工具执行失败(envelope.success=False)→ fail_task。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_review_format import (
        review_result_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "test_plan_content": {
            "section_package": {
                "generated_sections": [
                    {"section_id": "s1", "content": "Real " * 50},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        },
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": False,
        "data": None,
        "error": {"code": "REVIEW_CONTENT_MISSING", "message": "..."},
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await review_result_node(state, ctx=mock_ctx)

    assert result["task_status"] == "failed"
    assert result["last_error"]["code"] == "REVIEW_CONTENT_MISSING"


# ── F. route_after_result_review 失败路由 ───────────────────────────────


def test_route_after_result_review_routes_failure_to_fail_task():
    """task_status=failed → fail_task(Phase 2.9A.10 严格 Fail-Fast)。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
        route_after_result_review,
        NODE_FAIL_TASK,
    )

    state_failed = {
        "task_status": "failed",
        "last_error": {"code": "REVIEW_CONTENT_MISSING"},
    }
    assert route_after_result_review(state_failed) == NODE_FAIL_TASK


def test_route_after_result_review_passes_to_export_on_pass():
    """review_result.level=passed → prepare_export。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
        route_after_result_review,
        NODE_PREPARE_EXPORT,
    )

    state_ok = {
        "task_status": "running",
        "review_result": {"level": "passed"},
    }
    assert route_after_result_review(state_ok) == NODE_PREPARE_EXPORT


def test_route_after_result_review_regen_on_block_issues():
    """review_result.level=failed + 有 block issues + repair 未启用 → regenerate。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
        route_after_result_review,
        NODE_REGENERATE_SECTIONS_STEP,
    )

    state_failed = {
        "task_status": "running",
        "review_result": {
            "level": "failed",
            "block_issues": [{"rule_id": "r1"}],
        },
        "review_loop_count": 0,
        "repair_agent_enabled": False,
        "repair_loop_count": 0,
    }
    assert route_after_result_review(state_failed) == NODE_REGENERATE_SECTIONS_STEP


def test_route_after_result_review_distinguishes_tool_fail_vs_review_level():
    """tool fail(task_status=failed)与 level=failed 是不同语义,前者 fail_task,后者 regen/export。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
        route_after_result_review,
        NODE_FAIL_TASK,
        NODE_REGENERATE_SECTIONS_STEP,
    )

    # 工具执行失败 → 立刻 fail_task
    state_tool_fail = {
        "task_status": "failed",
        "review_result": {"level": "failed", "block_issues": [{"rule_id": "r1"}]},
    }
    assert route_after_result_review(state_tool_fail) == NODE_FAIL_TASK

    # review level=failed(数据问题)→ 走 regen(可修复)
    state_review_fail = {
        "task_status": "running",
        "review_result": {"level": "failed", "block_issues": [{"rule_id": "r1"}]},
        "review_loop_count": 0,
        "repair_agent_enabled": False,
        "repair_loop_count": 0,
    }
    assert route_after_result_review(state_review_fail) == NODE_REGENERATE_SECTIONS_STEP


# ── G/H/I. Generation → Review → Export Fail-Fast 链 ─────────────────────


def test_route_after_generate_test_plan_blocks_failed_gen():
    """生成失败 → 不进 review。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
        route_after_generate_test_plan,
        NODE_FAIL_TASK,
    )

    state_failed = {"task_status": "failed", "last_error": {"code": "GENERATION_OUTPUT_INVALID"}}
    assert route_after_generate_test_plan(state_failed) == NODE_FAIL_TASK


def test_route_after_export_word_blocks_failed_export():
    """导出失败 → 不进 format_check。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
        route_after_export_word,
        NODE_FAIL_TASK,
    )

    state_failed = {"task_status": "failed", "last_error": {"code": "WORD_EXPORT_FAILED"}}
    assert route_after_export_word(state_failed) == NODE_FAIL_TASK