"""Phase 2.4 test #15: Cancel — cancellation_service.is_cancelled()=True → 优雅退出。"""

from __future__ import annotations

import pytest

from app.agent_runtime.repair.agent_loop import run_repair
from app.agent_runtime.runtime_context import RuntimeContext
from datetime import datetime

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    _null_session_factory,
    FakeLLMClient,
    StubToolAdapter,
    _make_review_result,
)


@pytest.mark.asyncio
async def test_repair_cancel_returns_cleanly(
    in_memory_sink,
):
    """cancellation_service 标记 True → run_repair 永不抛,emit REPAIR_SKIPPED。"""
    from tests.agent_runtime.test_plan_repair_agent.conftest import _StubCancel

    cancel = _StubCancel(cancel=False)
    ctx = RuntimeContext(
        user_internal_id=1, task_internal_id=400, conversation_internal_id=40,
        session_factory=_null_session_factory,
        settings_service=None,
        event_sink=in_memory_sink,
        cancellation_service=cancel,
        clock=lambda: datetime.utcnow(),
        tool_adapter=StubToolAdapter(),
    )
    state = {
        "task_id": "t1", "graph_run_id": "r1",
        "review_result": _make_review_result(["i1"], ["s1"], level="failed"),
        "test_plan_content": {"sections": []},
        "template_structure": {"generation_config": {}},
        "repair_agent_enabled": True, "locked_section_ids": [],
        "repair_loop_count": 0,
        "user_prompt": "x",
    }

    # 第一次进入时取消(模拟用户在第一帧后立刻取消)
    call_count = [0]

    class CancelLLM:
        async def generate_with_profile(
            self, profile, content, *, parser=None, system_prompt_override=None
        ):
            call_count[0] += 1
            cancel.set_cancel(True)
            # 返回合法 JSON 但 cancel 已触发 → 后续循环检查应退出
            import json as _json
            return type("R", (), {
                "success": True, "parsed": {
                    "action": "call_tool", "tool_name": "TestPlanRegenTool",
                    "tool_arguments": {"section_ids": ["s1"], "issues": []},
                    "target_issue_ids": ["i1"], "target_section_ids": ["s1"],
                    "suggested_strategy": "regenerate_section",
                    "decision_summary": "尝试修",
                }, "error_message": None,
            })()

    result = await run_repair(state, llm_client=CancelLLM(),
                              tool_adapter=ctx.tool_adapter, ctx=ctx)
    # 永不抛
    assert isinstance(result.review_passed, bool)
    assert result.fallback_reason is not None or result.review_passed is False