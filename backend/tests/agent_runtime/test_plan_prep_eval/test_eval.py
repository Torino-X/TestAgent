"""test_eval.py — drives scenarios through run_preparation + asserts expectations.

Writes tests/_artifacts/prep_eval.json with full per-scenario results.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import pytest

from app.agent_runtime.preparation.agent_loop import run_preparation
from tests.agent_runtime.test_plan_prep_agent.conftest import (
    RuntimeContext,
    FakeLLMClient,
    StubToolAdapter,
)

from .conftest import build_fake_llm, build_stub_adapter, load_scenarios


ARTIFACTS_DIR = Path(__file__).resolve().parents[3] / "_artifacts"
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACTS_FILE = ARTIFACTS_DIR / "prep_eval.json"


class _NullSessionCM:
    async def __aenter__(self_inner):
        return None

    async def __aexit__(self_inner, *args):
        return False


def _null_session_factory():
    return _NullSessionCM()


class _StubCancel:
    def is_cancelled(self, task_id: str) -> bool:  # pragma: no cover
        return False


def _make_runtime_ctx(stub_adapter: StubToolAdapter) -> RuntimeContext:
    """Build a minimal RuntimeContext-compatible object for run_preparation."""
    from app.agent_runtime.events.sink import InMemoryEventSink
    sink = InMemoryEventSink()
    return RuntimeContext(
        user_internal_id=1,
        task_internal_id=1,
        conversation_internal_id=1,
        session_factory=_null_session_factory,
        settings_service=None,
        event_sink=sink,
        cancellation_service=_StubCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=stub_adapter,
    )


def _make_base_state(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Build a base state dict from scenario input."""
    return {
        "user_prompt": scenario["input"].get("user_prompt", ""),
        "requirement_summary": scenario["input"].get("requirement_summary", ""),
        "template_summary": scenario["input"].get("template_summary", ""),
        "knowledge_search_result": None,
    }


def _scenario_result(result, scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Compute scenario outcome vs expected."""
    expected = scenario["expected"]
    actual = {
        "tool_calls_count": result.budget_state.tool_calls,
        "final_action": (
            "fallback" if result.fallback_reason else
            "finish" if result.information_sufficient else
            "ask_user" if result.user_questions else
            "unknown"
        ),
        "information_sufficient": result.information_sufficient,
        "fallback_reason": result.fallback_reason,
        "knowledge_search_used": result.knowledge_search_used,
    }

    matches = {
        k: actual.get(k) == expected.get(k)
        for k in expected
    }
    return {
        "scenario": scenario["name"],
        "expected": expected,
        "actual": actual,
        "matches": matches,
        "passed": all(matches.values()),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", load_scenarios(), ids=[s["name"] for s in load_scenarios()]
)
async def test_eval_scenario(scenario):
    """Run one scenario end-to-end through run_preparation."""
    fake_llm = build_fake_llm(scenario["scripted_llm_responses"])
    stub_adapter = build_stub_adapter(scenario.get("kb_envelopes") or [])

    ctx = _make_runtime_ctx(stub_adapter)
    base_state = _make_base_state(scenario)

    t0 = time.monotonic()
    result = await run_preparation(
        base_state,
        llm_client=fake_llm,
        tool_adapter=stub_adapter,
        ctx=ctx,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    outcome = _scenario_result(result, scenario)
    outcome["elapsed_ms"] = elapsed_ms
    outcome["steps"] = result.budget_state.steps

    assert outcome["passed"], (
        f"scenario '{scenario['name']}' mismatches:\n"
        f"  expected: {outcome['expected']}\n"
        f"  actual:   {outcome['actual']}\n"
        f"  matches:  {outcome['matches']}"
    )


@pytest.mark.asyncio
async def test_eval_full_run_writes_artifact(all_scenarios):
    """Run all scenarios, write prep_eval.json, assert all pass."""
    from .report import render_markdown

    results: List[Dict[str, Any]] = []
    for scenario in all_scenarios:
        fake_llm = build_fake_llm(scenario["scripted_llm_responses"])
        stub_adapter = build_stub_adapter(scenario.get("kb_envelopes") or [])

        ctx = _make_runtime_ctx(stub_adapter)
        base_state = _make_base_state(scenario)

        t0 = time.monotonic()
        result = await run_preparation(
            base_state,
            llm_client=fake_llm,
            tool_adapter=stub_adapter,
            ctx=ctx,
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        outcome = _scenario_result(result, scenario)
        outcome["elapsed_ms"] = elapsed_ms
        outcome["steps"] = result.budget_state.steps
        results.append(outcome)

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    tool_calls_total = sum(r["actual"]["tool_calls_count"] for r in results)
    steps_total = sum(r["steps"] for r in results)
    fallback_count = sum(
        1 for r in results if r["actual"]["final_action"] == "fallback"
    )

    summary = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": passed / total if total else 0.0,
        "fallback_rate": fallback_count / total if total else 0.0,
        "tool_calls_total": tool_calls_total,
        "steps_total": steps_total,
        "avg_steps": steps_total / total if total else 0.0,
        "avg_tool_calls": tool_calls_total / total if total else 0.0,
    }

    artifact = {
        "phase": "2.3",
        "summary": summary,
        "scenarios": results,
    }
    ARTIFACTS_FILE.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Also write markdown report
    (ARTIFACTS_DIR / "prep_eval_report.md").write_text(
        render_markdown(artifact),
        encoding="utf-8",
    )

    assert passed == total, (
        f"{total - passed} of {total} scenarios failed; see "
        f"{ARTIFACTS_FILE}"
    )