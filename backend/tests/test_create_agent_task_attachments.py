"""Phase 2.8R-K 第三处修复 — _create_agent_task 关联附件单测。

验证 _create_agent_task 把 conversation 里已 confirm-type 的
requirement_doc / test_plan_template 关联到 task 的
requirement_file_id / template_file_id 字段。
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_msg_service():
    """构造最小 MessageService 实例,跳过 __init__。"""
    from app.services.message_service import MessageService
    return MessageService.__new__(MessageService)


def _make_file(public_id: str, internal_id: int, file_type: str):
    f = MagicMock()
    f.public_id = public_id
    f.id = internal_id
    f.file_type = file_type
    return f


def _make_intent_result():
    from app.agent.enums import IntentType, MessageRoute
    from app.agent.intent_router import IntentResult
    return IntentResult(
        intent=IntentType.TEST_PLAN_GENERATION,
        route=MessageRoute.AGENT_TASK,
        supported=True,
        confidence=1.0,
        reason="",
    )


@pytest.mark.asyncio
async def test_create_agent_task_links_requirement_doc_and_template():
    """两个文件都 attached → 都被关联到 task。"""
    ms = _make_msg_service()
    ms._task_repo = MagicMock()
    ms._task_repo.create = AsyncMock(side_effect=lambda t: t)
    ms._event_repo = MagicMock()
    ms._event_repo.create = AsyncMock()
    ms._exec_repo = MagicMock()
    ms._exec_repo.enqueue_new_task = AsyncMock(return_value=SimpleNamespace())
    ms._context_svc = MagicMock()
    ms._context_svc.build_task_trigger_context = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda mode=None: {})
    )

    req = _make_file("file_req_001", internal_id=100, file_type="requirement_doc")
    tpl = _make_file("file_tpl_001", internal_id=200, file_type="test_plan_template")
    other = _make_file("file_other", internal_id=300, file_type="other")
    files = [req, tpl, other]
    attached = {"file_req_001", "file_tpl_001"}

    conv = MagicMock()
    conv.id = 5
    conv.project_id = 77
    conv.title = "测试方案生成"

    intent_result = _make_intent_result()

    captured = {}
    async def _capture(t):
        captured["task"] = t
        return t
    ms._task_repo.create = _capture

    await ms._create_agent_task(
        conv=conv,
        user_internal_id=1,
        content="test",
        now=None,
        user_msg_dict={},
        intent_result=intent_result,
        files=files,
        attached_id_set=attached,
    )

    task = captured["task"]
    assert task.requirement_file_id == 100
    assert task.template_file_id == 200
    assert task.project_id == 77
    assert task.engine_type == "langgraph"
    assert task.graph_name == "test_plan_generation"
    assert task.graph_version == "v3"


@pytest.mark.asyncio
async def test_create_agent_task_no_files_leaves_ids_none():
    """没传 files → 字段保持 None(向后兼容)。"""
    ms = _make_msg_service()
    ms._task_repo = MagicMock()
    ms._event_repo = MagicMock()
    ms._event_repo.create = AsyncMock()
    ms._exec_repo = MagicMock()
    ms._exec_repo.enqueue_new_task = AsyncMock(return_value=SimpleNamespace())
    ms._context_svc = MagicMock()
    ms._context_svc.build_task_trigger_context = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda mode=None: {})
    )

    conv = MagicMock()
    conv.id = 5
    conv.title = "测试方案生成"

    captured = {}
    async def _capture(t):
        captured["task"] = t
        return t
    ms._task_repo.create = _capture

    await ms._create_agent_task(
        conv=conv,
        user_internal_id=1,
        content="test",
        now=None,
        user_msg_dict={},
        intent_result=_make_intent_result(),
        # 显式不传 files / attached_id_set
    )

    task = captured["task"]
    assert task.requirement_file_id is None
    assert task.template_file_id is None


@pytest.mark.asyncio
async def test_create_dynamic_agent_task_uses_langgraph_v3():
    """Every newly persisted dynamic-agent task uses the v3 contract."""
    from app.agent.enums import IntentType, MessageRoute
    from app.agent.intent_router import IntentResult
    from app.agent_runtime.graphs.dynamic_agent import (
        GRAPH_NAME_DYNAMIC_AGENT,
        GRAPH_VERSION_DYNAMIC_AGENT_V3,
    )

    ms = _make_msg_service()
    ms._task_repo = MagicMock()
    ms._event_repo = MagicMock()
    ms._event_repo.create = AsyncMock()
    ms._exec_repo = MagicMock()
    ms._exec_repo.enqueue_new_task = AsyncMock(return_value=SimpleNamespace())
    ms._context_svc = MagicMock()
    ms._context_svc.build_task_trigger_context = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda mode=None: {})
    )

    captured = {}

    async def _capture(task):
        captured["task"] = task
        return task

    ms._task_repo.create = _capture
    conv = SimpleNamespace(id=5, public_id="conv_dynamic", project_id=None, title="")
    intent_result = IntentResult(
        intent=IntentType.DOCUMENT_QUESTION,
        route=MessageRoute.AGENT_TASK,
        supported=True,
        confidence=1.0,
        extra_payload={
            "capability_routing": {
                "route": "dynamic_agent",
                "execution_mode": "dynamic_agent",
                "target_capability": "document_question",
            }
        },
    )

    await ms._create_agent_task(
        conv=conv,
        user_internal_id=1,
        content="summarize the evidence",
        now=None,
        user_msg_dict={"message_id": "msg_dynamic"},
        intent_result=intent_result,
    )

    task = captured["task"]
    assert task.task_type == "dynamic_agent"
    assert task.engine_type == "langgraph"
    assert task.graph_name == GRAPH_NAME_DYNAMIC_AGENT
    assert task.graph_version == GRAPH_VERSION_DYNAMIC_AGENT_V3
    enqueue_kwargs = ms._exec_repo.enqueue_new_task.await_args.kwargs
    assert enqueue_kwargs["graph_version"] == GRAPH_VERSION_DYNAMIC_AGENT_V3
    assert enqueue_kwargs["payload"]["graph_version"] == GRAPH_VERSION_DYNAMIC_AGENT_V3


@pytest.mark.asyncio
async def test_create_agent_task_fallback_to_all_conversation_files_when_attached_empty():
    """attached_id_set 为空集 → fallback 用本 conversation 内所有匹配文件。"""
    ms = _make_msg_service()
    ms._task_repo = MagicMock()
    ms._event_repo = MagicMock()
    ms._event_repo.create = AsyncMock()
    ms._exec_repo = MagicMock()
    ms._exec_repo.enqueue_new_task = AsyncMock(return_value=SimpleNamespace())
    ms._context_svc = MagicMock()
    ms._context_svc.build_task_trigger_context = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda mode=None: {})
    )

    req = _make_file("file_req_001", internal_id=100, file_type="requirement_doc")
    tpl = _make_file("file_tpl_001", internal_id=200, file_type="test_plan_template")
    files = [req, tpl]
    attached = set()  # 空 attached(前端没传 attached_file_ids)

    conv = MagicMock()
    conv.id = 5
    conv.title = "测试方案生成"

    captured = {}
    async def _capture(t):
        captured["task"] = t
        return t
    ms._task_repo.create = _capture

    await ms._create_agent_task(
        conv=conv,
        user_internal_id=1,
        content="test",
        now=None,
        user_msg_dict={},
        intent_result=_make_intent_result(),
        files=files,
        attached_id_set=attached,
    )

    task = captured["task"]
    assert task.requirement_file_id == 100
    assert task.template_file_id == 200


@pytest.mark.asyncio
async def test_create_agent_task_builds_incremental_task_from_latest_artifact():
    """result_modification 不再退回主图,而是创建 incremental_test_plan 任务。"""
    from app.agent.enums import IntentType, MessageRoute
    from app.agent.intent_router import IntentResult
    from app.agent_runtime.incremental.subgraph import (
        GRAPH_NAME_INCREMENTAL,
        GRAPH_VERSION_INCREMENTAL_V3,
    )

    ms = _make_msg_service()
    ms._session = MagicMock()
    ms._task_repo = MagicMock()
    ms._event_repo = MagicMock()
    ms._event_repo.create = AsyncMock()
    ms._exec_repo = MagicMock()
    ms._exec_repo.enqueue_new_task = AsyncMock(return_value=SimpleNamespace())
    ms._context_svc = MagicMock()
    ms._context_svc.build_task_trigger_context = AsyncMock(
        return_value=SimpleNamespace(model_dump=lambda mode=None: {})
    )

    source_task = SimpleNamespace(
        id=11,
        public_id="task_source",
        requirement_file_id=100,
        template_file_id=200,
        task_context_json={},
        plan_json={},
        review_result_json={},
    )
    ms._session.execute = AsyncMock(
        return_value=SimpleNamespace(
            scalar_one_or_none=MagicMock(return_value=source_task)
        )
    )
    artifact = SimpleNamespace(
        public_id="art_source",
        task_id=11,
        version_no=1,
        source_artifact_id=None,
        metadata_json={
            "test_plan_content": {
                "section_package": {
                    "generated_sections": [
                        {"section_id": "strategy", "title": "4 测试策略"}
                    ]
                }
            },
            "template_structure": {
                "generation_config": {
                    "ai_fields": [
                        {"section_id": "strategy", "section_title": "4 测试策略"}
                    ]
                }
            },
        },
    )

    conv = SimpleNamespace(id=5, public_id="conv_001", title="")
    intent_result = IntentResult(
        intent=IntentType.RESULT_MODIFICATION,
        route=MessageRoute.AGENT_TASK,
        supported=True,
        confidence=0.91,
        reason="modify existing result",
    )
    captured = {}

    async def _capture_task(task):
        captured["task"] = task
        return task

    ms._task_repo.create = _capture_task

    with patch(
        "app.services.message_service.ArtifactRepository.get_latest_available_by_conversation",
        new=AsyncMock(return_value=artifact),
    ):
        await ms._create_agent_task(
            conv=conv,
            user_internal_id=1,
            content="把测试策略章节补充更多边界场景",
            now=None,
            user_msg_dict={"message_id": "msg_1"},
            intent_result=intent_result,
        )

    task = captured["task"]
    assert task.task_type == "incremental_test_plan"
    assert task.engine_type == "langgraph"
    assert task.graph_name == GRAPH_NAME_INCREMENTAL
    assert task.graph_version == GRAPH_VERSION_INCREMENTAL_V3
    assert task.requirement_file_id == 100
    assert task.template_file_id == 200

    enqueue_kwargs = ms._exec_repo.enqueue_new_task.await_args.kwargs
    assert enqueue_kwargs["request_type"] == "incremental_task"
    assert enqueue_kwargs["graph_name"] == GRAPH_NAME_INCREMENTAL
    assert enqueue_kwargs["payload"]["execution_mode"] == "incremental_agent"
    assert enqueue_kwargs["payload"]["incremental_intent"]["scope"]["target_section_ids"] == [
        "strategy"
    ]


__all__ = [
    "test_create_agent_task_links_requirement_doc_and_template",
    "test_create_agent_task_no_files_leaves_ids_none",
    "test_create_agent_task_fallback_to_all_conversation_files_when_attached_empty",
]
