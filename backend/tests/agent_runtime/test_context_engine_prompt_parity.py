"""Regression tests for server-owned rules surviving CE migration."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


class _Bridge:
    available = True

    def __init__(self, parsed: dict):
        self.parsed = parsed
        self.calls: list[dict] = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        result = SimpleNamespace(success=True, parsed=self.parsed, error_message=None)
        return SimpleNamespace(as_profile_result=lambda: result)


@pytest.mark.asyncio
async def test_preparation_ce_call_keeps_dynamic_system_policy():
    from app.agent_runtime.preparation.agent_loop import _llm_decide

    bridge = _Bridge({
        "action": "finish",
        "decision_summary": "requirements are sufficient",
        "public_update": "preparation complete",
        "confidence": 0.9,
    })
    ctx = SimpleNamespace(
        user_internal_id=1,
        conversation_internal_id=2,
        task_internal_id=3,
        context_llm_invoker=bridge,
    )

    decision = await _llm_decide(
        llm_client=object(),
        system_prompt="PREPARATION_POLICY_MARKER: allowed tool is KnowledgeSearchTool",
        user_content="untrusted task data",
        parser=None,
        ctx=ctx,
        task_state_ref={},
    )

    assert decision.action == "finish"
    assert bridge.calls[0]["system_prompt"].startswith("PREPARATION_POLICY_MARKER")


@pytest.mark.asyncio
async def test_incremental_ce_call_keeps_dynamic_system_policy():
    from app.agent_runtime.incremental.agent_loop import _invoke_llm_for_decision

    bridge = _Bridge({
        "action": "finish",
        "decision_summary": "incremental task complete",
        "public_update": "incremental task complete",
        "confidence": 0.8,
    })
    ctx = SimpleNamespace(
        user_internal_id=1,
        conversation_internal_id=2,
        task_internal_id=3,
        context_llm_invoker=bridge,
    )

    decision = await _invoke_llm_for_decision(
        state={},
        ctx=ctx,
        system_prompt="INCREMENTAL_POLICY_MARKER: locked sections must not change",
        user_content="untrusted task data",
    )

    assert decision.action == "finish"
    assert bridge.calls[0]["system_prompt"].startswith("INCREMENTAL_POLICY_MARKER")
