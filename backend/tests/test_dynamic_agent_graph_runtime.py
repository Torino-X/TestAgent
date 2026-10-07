from __future__ import annotations

import pytest

from app.agent_runtime.graph_registry import GraphRegistry
from app.agent_runtime.graph_runtime_service import GraphRuntimeService
from app.agent_runtime.graphs.dynamic_agent import (
    GRAPH_NAME_DYNAMIC_AGENT,
    GRAPH_VERSION_DYNAMIC_AGENT_V1,
)


@pytest.mark.asyncio
async def test_dynamic_agent_graph_runs_to_final_answer() -> None:
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    result = await runtime.ainvoke(
        {
            "task_public_id": "task_dyn_001",
            "task_id": "task_dyn_001",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "分析已知证据",
            "target_capability": "general_chat",
            "operation": "analyze",
            "observations": [{"status": "success", "summary": "已有证据"}],
        }
    )

    assert result["task_status"] == "completed"
    assert result["plan"]["revision"] == 1
    assert result["verification_status"] == "COMPLETE"
    assert result["final_answer"]
    assert "completed_nodes" in result
    assert "persist_final_answer" in result["completed_nodes"]


@pytest.mark.asyncio
async def test_dynamic_agent_graph_fails_closed_when_tool_handler_missing() -> None:
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    result = await runtime.ainvoke(
        {
            "task_public_id": "task_dyn_missing_tool",
            "task_id": "task_dyn_missing_tool",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "分析当前上传的 Word 文档",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_doc",
                    "file_ext": ".docx",
                }
            ],
        }
    )

    assert result["task_status"] == "failed"
    assert result["verification_status"] == "FAIL"
    assert result["verification_gaps"] == ["capability_handler_not_configured"]


@pytest.mark.asyncio
async def test_dynamic_agent_graph_fails_closed_for_unknown_capability_plan() -> None:
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    result = await runtime.ainvoke(
        {
            "task_public_id": "task_dyn_002",
            "task_id": "task_dyn_002",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "bad",
            "plan": {
                "goal": "bad",
                "revision": 1,
                "status": "active",
                "steps": [
                    {
                        "step_id": "step_1",
                        "title": "bad",
                        "action_type": "tool",
                        "capability_key": "unknown",
                        "input_refs": [],
                        "depends_on": [],
                        "success_criteria": ["ok"],
                        "status": "pending",
                    }
                ],
            },
            "plan_revision": 1,
        }
    )

    assert result["task_status"] == "failed"
    assert result["failure"]["code"] == "PLAN_VALIDATION_ERROR"
    assert "unknown_capability:unknown" in result["failure"]["errors"]


@pytest.mark.asyncio
async def test_dynamic_agent_graph_pauses_when_user_input_required() -> None:
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    result = await runtime.ainvoke(
        {
            "task_public_id": "task_dyn_need_user",
            "task_id": "task_dyn_need_user",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "Choose the correct source document",
            "target_capability": "general_chat",
            "operation": "analyze",
            "awaiting_user": True,
            "clarification": {
                "question": "Which document should I analyze?",
                "required_input": "attachment_selection",
            },
            "plan": {
                "goal": "Choose the correct source document",
                "revision": 1,
                "status": "active",
                "steps": [
                    {
                        "step_id": "step_1",
                        "title": "Wait for user selection",
                        "action_type": "analysis",
                        "capability_key": "evidence_analysis",
                        "input_refs": [],
                        "depends_on": [],
                        "success_criteria": ["user_selection_received"],
                        "status": "completed",
                    }
                ],
            },
            "observations": [{"step_id": "step_1", "status": "success"}],
        }
    )

    assert result["task_status"] == "waiting_user"
    assert result["pause_marker"] == "dynamic_agent_need_user"
    assert result["current_node"] == "need_user"
    assert result["verification_status"] == "NEED_USER"


@pytest.mark.asyncio
async def test_dynamic_agent_graph_requests_user_for_ambiguous_document_reference() -> None:
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    result = await runtime.ainvoke(
        {
            "task_public_id": "task_dyn_ambiguous_doc",
            "task_id": "task_dyn_ambiguous_doc",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "分析这个文件",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_a",
                    "file_ext": ".docx",
                    "position": 1,
                },
                {
                    "ref": "attachment:1",
                    "file_public_id": "file_b",
                    "file_ext": ".docx",
                    "position": 2,
                },
            ],
        }
    )

    assert result["task_status"] == "waiting_user"
    assert result["current_node"] == "need_user"
    assert result["pause_marker"] == "dynamic_agent_need_user"
    assert result["clarification"]["required_input"] == "attachment_selection"


@pytest.mark.asyncio
async def test_dynamic_agent_graph_treats_uploaded_docx_ext_without_dot_as_docx() -> None:
    registry = GraphRegistry.build_default_v2_v3()
    runtime = GraphRuntimeService(registry)

    result = await runtime.ainvoke(
        {
            "task_public_id": "task_dyn_ambiguous_doc_no_dot",
            "task_id": "task_dyn_ambiguous_doc_no_dot",
            "graph_name": GRAPH_NAME_DYNAMIC_AGENT,
            "graph_version": GRAPH_VERSION_DYNAMIC_AGENT_V1,
            "goal": "Analyze this file",
            "target_capability": "document_qa",
            "operation": "analyze",
            "attachment_refs": [
                {
                    "ref": "attachment:0",
                    "file_public_id": "file_a",
                    "file_ext": "docx",
                    "position": 1,
                },
                {
                    "ref": "attachment:1",
                    "file_public_id": "file_b",
                    "file_ext": "docx",
                    "position": 2,
                },
            ],
        }
    )

    assert result["task_status"] == "waiting_user"
    assert result["clarification"]["required_input"] == "attachment_selection"
