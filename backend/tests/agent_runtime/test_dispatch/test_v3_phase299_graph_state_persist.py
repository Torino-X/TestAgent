"""Phase 2.9A.9 业务 State 持久化与 Fail-Fast 路由契约测试。

覆盖:

A. 预确认节点显式返回业务字段
B. generate_test_plan 启动前校验 requirement/template/sections
C. ToolAdapter._build_proxy 优先从 graph_state 读业务字段
D. 新 RuntimeContext (空 _intermediate_state) 不影响 _build_proxy
E. Fail-Fast:generate 失败 → 不进 review;export 失败 → 不进 format_check
F. resume_task_node 派生 confirmed_sections
G. State schema 含 confirmed_sections 字段
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── A. 预确认节点显式返回业务字段 ────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_requirement_node_returns_requirement_analysis():
    """parse_requirement_node 必须返回 ``requirement_analysis`` 字段。

    之前节点只把数据存到 ``ctx._intermediate_state``,Graph State 空,
    Resume 后新 RuntimeContext 也读不到 — 这是真实任务的根因。
    """
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        parse_requirement_node,
    )

    # 构建最小 State
    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "requirement_file_id": "req-file-1",
        "completed_nodes": [],
    }

    # Mock adapter 工具返回 envelope
    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {"summary": "Req summary", "sections": []},
    })

    # Mock ctx
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.user_internal_id = 1
    mock_ctx.conversation_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await parse_requirement_node(state, ctx=mock_ctx)

    # 必须显式返回 requirement_analysis
    assert "requirement_analysis" in result
    assert result["requirement_analysis"] == {"summary": "Req summary", "sections": []}
    assert result["current_node"] == "parse_requirement"


@pytest.mark.asyncio
async def test_parse_template_node_returns_template_structure_and_review_standard():
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        parse_template_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "template_file_id": "tpl-file-1",
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {
            "template_structure": {"sections": ["A", "B"]},
            "review_standard": {"checks": ["c1"]},
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await parse_template_node(state, ctx=mock_ctx)

    assert "template_structure" in result
    assert "review_standard" in result
    assert result["template_structure"] == {"sections": ["A", "B"]}
    assert result["review_standard"] == {"checks": ["c1"]}


@pytest.mark.asyncio
async def test_search_knowledge_node_returns_knowledge_search_result():
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        search_knowledge_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {"hits": [{"doc_id": "d1"}]},
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await search_knowledge_node(state, ctx=mock_ctx)

    assert "knowledge_search_result" in result
    assert result["knowledge_search_result"] == {"hits": [{"doc_id": "d1"}]}


@pytest.mark.asyncio
async def test_suggest_sections_node_returns_section_suggestions():
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        suggest_sections_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {"sections": [{"section_id": "s1"}]},
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter

    result = await suggest_sections_node(state, ctx=mock_ctx)

    assert "section_suggestions" in result
    assert result["section_suggestions"] == {"sections": [{"section_id": "s1"}]}


# ── B. generate_test_plan 启动前校验 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_test_plan_fails_when_requirement_missing():
    """Graph State 缺 requirement_analysis → GENERATION_STATE_MISSING。

    不再调 TestPlanGeneratorTool 触发 MISSING_REQUIREMENT;节点直接
    返回失败状态,Fail-Fast 路由走 fail_task。
    """
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        generate_test_plan_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        # requirement_analysis 缺失!
        "template_structure": {"x": 1},
        "section_suggestions": {"sections": []},
        "section_confirm_config": {"sections": [{"section_id": "s1"}]},
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock()  # 不应被调用
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    assert result["task_status"] == "failed"
    assert result["last_error"]["code"] == "GENERATION_STATE_MISSING"
    assert "requirement_analysis" in result["last_error"]["message"]
    # adapter 不应被调用
    mock_adapter.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_generate_test_plan_fails_when_confirmed_sections_empty():
    """confirmed_sections 为 None(从未写入)/ section_confirm_config.sections 为空
    → 视为缺失 → 失败。

    Phase 2.9A.9 严格语义:missing = 字段不存在 或 字段为 None;
    空 dict {} 视为"已写入但 stub 没产出",允许继续(测试兼容)。
    """
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        generate_test_plan_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "requirement_analysis": {"summary": "ok"},
        "template_structure": {"x": 1},
        "section_suggestions": {"sections": [{"section_id": "s1"}]},
        # 顶层 confirmed_sections 缺失
        "section_confirm_config": {"sections": []},  # 空 → 顶层也 None
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    assert result["task_status"] == "failed"
    assert result["last_error"]["code"] == "GENERATION_STATE_MISSING"
    assert "confirmed_sections" in result["last_error"]["message"]


@pytest.mark.asyncio
async def test_generate_test_plan_allows_empty_dict_when_field_was_written():
    """空 dict {} 但键存在(代表节点已写入,stub 没产出数据) — 允许继续。

    这是与 Phase 2.1 测试兼容的关键:stub executor 不产出数据,但节点
    仍写入空 dict 占位。严格 missing 校验必须区分两种场景。
    """
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        generate_test_plan_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "requirement_analysis": {},  # 空 dict 但存在
        "template_structure": {},  # 空 dict 但存在
        "section_suggestions": {},  # 空 dict 但存在
        "section_confirm_config": {"sections": [{"section_id": "s1"}]},
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    # Phase 2.9A.10:stub 数据必须有真实 section_package 才能通过内容校验
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {
            "generated_sections": 1,
            "kept_sections": 0,
            "manual_sections": 0,
            "total_word_count": 100,
            "tables_generated": 0,
            "section_package": {
                "generated_sections": [
                    {"section_id": "s1", "title": "S1", "content": "Content " * 50},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    # 不应 fail — 空 dict 视为已写入
    assert result.get("task_status") != "failed"
    mock_adapter.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_test_plan_uses_confirmed_sections_top_level():
    """顶层 confirmed_sections 字段优先于 section_confirm_config.sections。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        generate_test_plan_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "requirement_analysis": {"summary": "ok"},
        "template_structure": {"x": 1},
        "section_suggestions": {"sections": [{"section_id": "s1"}]},
        "confirmed_sections": [{"section_id": "s1"}, {"section_id": "s2"}],
        "section_confirm_config": {"sections": []},  # 空,但顶层有
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    # Phase 2.9A.10:必须返回真实生成的 section_package,否则内容校验失败
    mock_adapter.execute = AsyncMock(return_value={
        "success": True,
        "data": {
            "generated_sections": 2,
            "kept_sections": 0,
            "manual_sections": 0,
            "total_word_count": 100,
            "tables_generated": 0,
            "section_package": {
                "generated_sections": [
                    {"section_id": "s1", "title": "S1", "content": "Content 1 " * 50},
                    {"section_id": "s2", "title": "S2", "content": "Content 2 " * 50},
                ],
                "keep_sections": [],
                "manual_sections": [],
            },
        },
    })
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    # 不应失败;confirmed_sections 从顶层读 + section_package 非空
    assert result.get("task_status") != "failed"
    mock_adapter.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_test_plan_emits_tool_failed_when_state_missing():
    """缺字段时必须发 TOOL_FAILED 事件(给前端可见反馈)。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        generate_test_plan_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    await generate_test_plan_node(state, ctx=mock_ctx)

    emit_calls = mock_ctx.event_sink.emit.call_args_list
    assert any(
        call.kwargs.get("event_type") == "tool_failed"
        for call in emit_calls
    ), "必须发 TOOL_FAILED 事件"


# ── C. ToolAdapter._build_proxy 从 graph_state 读 ─────────────────────────


@pytest.mark.asyncio
async def test_build_proxy_prefers_graph_state_over_intermediate_state():
    """graph_state 优先于 ctx._intermediate_state。"""
    from app.agent_runtime.adapters.test_agent_tool_adapter import (
        TestAgentToolAdapter,
    )
    from app.agent_runtime.runtime_context import RuntimeContext
    from datetime import datetime
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _null_session():
        yield None

    mock_executor = MagicMock()
    adapter = TestAgentToolAdapter(
        tool_executor=mock_executor,
        event_sink=MagicMock(),
        session_factory=lambda: _null_session(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )

    # ctx 的 _intermediate_state 是旧值(空)
    ctx = MagicMock(spec=RuntimeContext)
    ctx.task_internal_id = 1
    ctx.conversation_internal_id = 1
    ctx.user_internal_id = 1
    ctx.settings_service = None
    ctx._intermediate_state = {}

    # graph_state 是新值(Resume 后从 Postgres 恢复的)
    graph_state = {
        "requirement_analysis": {"summary": "from_graph_state"},
        "template_structure": {"sections": ["A"]},
    }

    proxy = adapter._build_proxy(
        ctx_runtime=ctx,
        inputs={},
        session=None,
        graph_state=graph_state,
    )

    # 优先从 graph_state 读
    assert proxy.requirement_analysis == {"summary": "from_graph_state"}
    assert proxy.template_structure == {"sections": ["A"]}


@pytest.mark.asyncio
async def test_build_proxy_with_empty_intermediate_state_does_not_lose_data():
    """新 RuntimeContext (空 _intermediate_state) 时仍能从 graph_state 读。"""
    from app.agent_runtime.adapters.test_agent_tool_adapter import (
        TestAgentToolAdapter,
    )
    from app.agent_runtime.runtime_context import RuntimeContext
    from datetime import datetime
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _null_session():
        yield None

    mock_executor = MagicMock()
    adapter = TestAgentToolAdapter(
        tool_executor=mock_executor,
        event_sink=MagicMock(),
        session_factory=lambda: _null_session(),
        clock=lambda: datetime.utcnow(),
        min_visible_seconds=0.0,
        transition_seconds=0.0,
    )

    # 模拟 Resume 后:ctx._intermediate_state 不存在(新 ctx)
    ctx = MagicMock(spec=RuntimeContext)
    ctx.task_internal_id = 1
    ctx.conversation_internal_id = 1
    ctx.user_internal_id = 1
    ctx.settings_service = None
    # 关键:没有 _intermediate_state 属性
    del ctx._intermediate_state

    graph_state = {
        "requirement_analysis": {"summary": "resumed_from_postgres"},
        "template_structure": {"x": 1},
        "knowledge_search_result": {"hits": []},
        "section_suggestions": {"sections": [{"section_id": "s1"}]},
        "section_confirm_config": {"sections": [{"section_id": "s1"}]},
    }

    proxy = adapter._build_proxy(
        ctx_runtime=ctx,
        inputs={},
        session=None,
        graph_state=graph_state,
    )

    # 全部业务字段从 graph_state 读出
    assert proxy.requirement_analysis == {"summary": "resumed_from_postgres"}
    assert proxy.template_structure == {"x": 1}
    assert proxy.knowledge_search_result == {"hits": []}
    assert proxy.section_suggestions == {"sections": [{"section_id": "s1"}]}
    assert proxy.section_confirm_config == {"sections": [{"section_id": "s1"}]}


# ── D. resume_task_node 派生 confirmed_sections ──────────────────────────


@pytest.mark.asyncio
async def test_resume_task_derives_confirmed_sections():
    """resume_task_node 从 section_confirm_config.sections 派生 confirmed_sections。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        resume_task_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        "section_confirm_config": {
            "sections": [{"section_id": "s1"}, {"section_id": "s2"}],
        },
        "completed_nodes": [],
    }

    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await resume_task_node(state, ctx=mock_ctx)

    assert result.get("confirmed_sections") == [
        {"section_id": "s1"},
        {"section_id": "s2"},
    ]


# ── E. Fail-Fast 路由 ────────────────────────────────────────────────────


def test_route_after_generate_test_plan_routes_failure_to_fail_task():
    from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
        route_after_generate_test_plan,
        NODE_FAIL_TASK,
        NODE_REVIEW_STEP,
    )

    # 失败 → fail_task
    state_failed = {"task_status": "failed"}
    assert route_after_generate_test_plan(state_failed) == NODE_FAIL_TASK

    # 成功 → review_step
    state_ok = {"task_status": "running", "last_error": None}
    assert route_after_generate_test_plan(state_ok) == NODE_REVIEW_STEP


def test_route_after_export_word_routes_failure_to_fail_task():
    from app.agent_runtime.graphs.test_plan.versions.v3.routing import (
        route_after_export_word,
        NODE_FAIL_TASK,
    )

    state_failed = {"task_status": "failed"}
    assert route_after_export_word(state_failed) == NODE_FAIL_TASK

    state_ok = {"task_status": "running"}
    assert route_after_export_word(state_ok) == "check_docx_format_step"


# ── F. State schema 含 confirmed_sections ──────────────────────────────────


def test_state_schema_has_confirmed_sections_field():
    from app.agent_runtime.graphs.test_plan.state import (
        TestPlanGraphState,
        make_empty_state,
    )

    state = make_empty_state(
        task_id="t",
        graph_run_id="r",
        graph_version="v3",
    )
    # Phase 2.9A.9: 新增字段
    assert "confirmed_sections" in state
    assert state["confirmed_sections"] is None


def test_make_empty_state_initializes_confirmed_sections():
    from app.agent_runtime.graphs.test_plan.state import make_empty_state

    state = make_empty_state(task_id="t", graph_run_id="r")
    assert state["confirmed_sections"] is None


# ── G. 集成测试:节点完整 partial update ────────────────────────────────────


@pytest.mark.asyncio
async def test_post_confirm_gen_node_emits_tool_failed_with_missing_fields():
    """generate_test_plan 缺字段时,emit TOOL_FAILED + last_error 含缺失字段名。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_post_confirm import (
        generate_test_plan_node,
    )

    state: Dict[str, Any] = {
        "task_internal_id": 1,
        # 全部缺失
        "completed_nodes": [],
    }

    mock_adapter = MagicMock()
    mock_adapter.execute = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.task_internal_id = 1
    mock_ctx.tool_adapter = mock_adapter
    mock_ctx.event_sink = MagicMock()
    mock_ctx.event_sink.emit = AsyncMock()

    result = await generate_test_plan_node(state, ctx=mock_ctx)

    # 4 个字段都应被列为缺失
    last_error_msg = result["last_error"]["message"]
    for field in ("requirement_analysis", "template_structure",
                  "section_suggestions", "confirmed_sections"):
        assert field in last_error_msg

    # emit TOOL_FAILED + payload 含 missing_fields
    tool_failed_call = next(
        call for call in mock_ctx.event_sink.emit.call_args_list
        if call.kwargs.get("event_type") == "tool_failed"
    )
    payload = tool_failed_call.kwargs.get("payload", {})
    assert payload.get("code") == "GENERATION_STATE_MISSING"
    assert set(payload.get("missing_fields", [])) == {
        "requirement_analysis", "template_structure",
        "section_suggestions", "confirmed_sections",
    }