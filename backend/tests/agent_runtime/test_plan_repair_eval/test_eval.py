"""Phase 2.4 Repair Agent eval harness — runs 6 scenarios, writes artifact.

For each scenario:

1. Load JSON
2. Build FakeLLMClient with scripted responses, parsing ``__USE_*__``
   directives into actual RepairDecision JSON
3. Build StubToolAdapter with envelope map (also via ``__USE_*__`` directives)
4. Call ``run_repair``
5. Compare against ``assertions``
6. Accumulate into ``artifact`` dict; write
   ``backend/_artifacts/repair_eval/repair_eval.json`` and
   ``repair_eval_report.md`` at the end
"""

from __future__ import annotations

import json as _json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from app.agent_runtime.events.sink import InMemoryEventSink
from app.agent_runtime.repair.agent_loop import run_repair
from app.agent_runtime.runtime_context import RuntimeContext

from tests.agent_runtime.test_plan_repair_agent.conftest import (
    FakeLLMClient,
    StubToolAdapter,
    call_regen_decision,
    call_review_decision,
    call_kb_decision,
    finish_decision,
    regen_envelope_ok,
    review_envelope_passed,
    review_envelope_failed,
    kb_envelope_ok,
)

from .conftest import (
    SCENARIOS_DIR,
    load_scenario,
    list_scenarios,
    build_base_state_from_scenario,
)


# ── "__USE_*__" directive expanders ────────────────────────────────────────


def _expand_llm_directive(raw: str, *, iteration: Optional[int] = None) -> str:
    """Expand ``__USE_*__|...`` placeholder strings into real scripted JSON."""
    raw = raw.strip()
    if raw.startswith("__USE_CALL_REGEN__|"):
        rest = raw[len("__USE_CALL_REGEN__|"):]
        parts = rest.split("|", 2)
        sections = parts[0].split(",") if parts[0] else []
        issues = parts[1].split(",") if parts[1] else []
        summary = parts[2] if len(parts) > 2 else "regen"
        return call_regen_decision(
            sections, issues, summary=summary, iteration=iteration,
        )
    if raw.startswith("__USE_CALL_REVIEW__|"):
        rest = raw[len("__USE_CALL_REVIEW__|"):]
        summary = rest or "re-review"
        return call_review_decision(summary=summary)
    if raw.startswith("__USE_CALL_KB__|"):
        rest = raw[len("__USE_CALL_KB__|"):]
        parts = rest.split("|", 1)
        query = parts[0]
        summary = parts[1] if len(parts) > 1 else "kb"
        return call_kb_decision(query, summary=summary)
    if raw.startswith("__FINISH__|"):
        ids = raw[len("__FINISH__|"):].split(",")
        return finish_decision([i for i in ids if i])
    return raw


def _expand_envelope_directive(raw: Any) -> Dict[str, Any]:
    """Expand a ``__USE_*__`` envelope directive into a real envelope dict.

    raw is expected to be a dict with one key starting with ``__USE_``.
    """
    if not isinstance(raw, dict):
        return raw
    if "__USE_OK__" in raw:
        return regen_envelope_ok(raw["__USE_OK__"])
    if "__USE_PASSED__" in raw:
        return review_envelope_passed()
    if "__USE_FAILED__" in raw:
        spec = raw["__USE_FAILED__"]
        return review_envelope_failed(spec.get("issue_ids", []), spec.get("section_ids", []))
    if "__USE_KB_OK__" in raw:
        return kb_envelope_ok(raw["__USE_KB_OK__"])
    return raw


# ── Scenario runner ───────────────────────────────────────────────────────


async def _run_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Run a single scenario and return a result summary dict."""
    # LLM scripted responses
    scripted: List[str] = []
    loop_count = scenario.get("loop_count") or 0
    raw_responses = list(scenario.get("scripted_llm_responses") or [])
    if loop_count > 0:
        # expand per-iteration template __ITER__ in summary, AND pass iteration
        # to call_regen_decision so args_signature varies per call (avoid
        # ToolPermissionGuard fail-fast).
        expanded: List[str] = []
        per_call = len(raw_responses)
        for i in range(loop_count):
            base = raw_responses[i % per_call]
            expanded.append(base.replace("__ITER__", str(i)))
        raw_responses = expanded
    for idx, r in enumerate(raw_responses):
        scripted.append(_expand_llm_directive(r, iteration=idx))
    llm = FakeLLMClient(responses=scripted)

    # Stub adapter
    env_cfg = scenario.get("envelope_map") or {}
    envelopes_per_tool: Dict[str, List[Dict[str, Any]]] = {}
    for tool, envs in env_cfg.items():
        envelopes_per_tool[tool] = [_expand_envelope_directive(e) for e in envs]
    adapter = StubToolAdapter(envelopes_per_tool=envelopes_per_tool)

    # Build base state
    state = build_base_state_from_scenario(scenario)

    # RuntimeContext: import locally to avoid heavy chain
    from tests.agent_runtime.test_plan_repair_agent.conftest import (
        _null_session_factory,
        _StubCancel,
    )

    sink = InMemoryEventSink()
    ctx = RuntimeContext(
        user_internal_id=1,
        task_internal_id=400,
        conversation_internal_id=40,
        session_factory=_null_session_factory,
        settings_service=None,
        event_sink=sink,
        cancellation_service=_StubCancel(),
        clock=lambda: datetime.utcnow(),
        tool_adapter=adapter,
    )

    repair_agent_enabled = scenario.get("repair_agent_enabled", True)
    state["repair_agent_enabled"] = repair_agent_enabled

    if not repair_agent_enabled:
        # Non-repair path: run_repair still completes, just with no real action.
        # For eval consistency, we still call run_repair (which will emit
        # mode_b_deferred fallback) and accept whatever it returns.
        pass

    result = await run_repair(
        state,
        llm_client=llm,
        tool_adapter=adapter,
        ctx=ctx,
    )

    # Assertions
    assertions = scenario.get("assertions") or {}
    failures: List[str] = []
    if "review_passed" in assertions:
        if result.review_passed != assertions["review_passed"]:
            failures.append(
                f"review_passed={result.review_passed} != expected={assertions['review_passed']}"
            )
    if "fallback_reason" in assertions:
        if result.fallback_reason != assertions["fallback_reason"]:
            failures.append(
                f"fallback_reason={result.fallback_reason!r} != expected={assertions['fallback_reason']!r}"
            )
    if "fallback_reason_contains" in assertions:
        needle = str(assertions["fallback_reason_contains"])
        if not needle in (result.fallback_reason or ""):
            failures.append(
                f"fallback_reason={result.fallback_reason!r} should contain {needle!r}"
            )
    if "issues_resolved_min" in assertions:
        got = len(result.issues_resolved)
        need = int(assertions["issues_resolved_min"])
        if got < need:
            failures.append(f"issues_resolved={got} < min={need}")
    if "tool_calls_min" in assertions:
        got = result.tool_calls_used
        need = int(assertions["tool_calls_min"])
        if got < need:
            failures.append(f"tool_calls={got} < min={need}")
    if "evidence_count_min" in assertions:
        got = len(result.knowledge_evidence)
        need = int(assertions["evidence_count_min"])
        if got < need:
            failures.append(f"evidence_count={got} < min={need}")
    if "steps_min" in assertions:
        got = result.budget_state.steps
        need = int(assertions["steps_min"])
        if got < need:
            failures.append(f"steps={got} < min={need}")
    if "modified_sections_must_not_contain" in assertions:
        for forbidden in assertions["modified_sections_must_not_contain"]:
            if forbidden in result.modified_section_ids:
                failures.append(
                    f"modified_sections={result.modified_section_ids} must not contain {forbidden!r}"
                )

    return {
        "index": 0,
        "name": scenario.get("name"),
        "description": scenario.get("description"),
        "passed": len(failures) == 0,
        "failures": failures,
        "review_passed": result.review_passed,
        "fallback_reason": result.fallback_reason,
        "steps": result.budget_state.steps,
        "tool_calls": result.tool_calls_used,
        "rounds": result.rounds_used,
        "issues_resolved": list(result.issues_resolved),
        "evidence_count": len(result.knowledge_evidence),
        "modified_section_ids": list(result.modified_section_ids),
        "irrelevant_section_modification": (
            len([
                s for s in result.modified_section_ids
                if s not in (state.get("test_plan_content", {}).get(
                    "sections", []
                ) and [
                    x.get("section_id") for x in
                    state["test_plan_content"].get("sections", [])
                    if isinstance(x, dict)
                ] or [])
            ])
        ),
    }


# ── Pytest tests (one per scenario) ──────────────────────────────────────


def _parametrize_id(val):
    """Pytest id function — accepts scalar (str) and returns string label."""
    if isinstance(val, str):
        return val
    return str(val)


@pytest.mark.parametrize(
    "scenario_name",
    list_scenarios(),
    ids=_parametrize_id,
)
@pytest.mark.asyncio
async def test_repair_eval_scenario(scenario_name: str):
    scenario = load_scenario(scenario_name)
    result = await _run_scenario(scenario)
    assert result["passed"], (
        f"Scenario {scenario_name} failed: {result['failures']}; "
        f"got {result!r}"
    )


# ── Aggregate artifact writer ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repair_eval_artifact_write(current_artifact_dir: Path):
    """Run all scenarios, write aggregate artifact and markdown report."""
    scenarios_results: List[Dict[str, Any]] = []
    for idx, name in enumerate(list_scenarios()):
        scenario = load_scenario(name)
        r = await _run_scenario(scenario)
        r["index"] = idx + 1
        scenarios_results.append(r)

    passed = sum(1 for s in scenarios_results if s["passed"])
    total = len(scenarios_results)
    fallback_count = sum(
        1 for s in scenarios_results if s["fallback_reason"]
    )
    avg_steps = (
        sum(s["steps"] for s in scenarios_results) / total if total else 0.0
    )
    avg_tool_calls = (
        sum(s["tool_calls"] for s in scenarios_results) / total if total else 0.0
    )
    irrelevant_total = sum(
        s["irrelevant_section_modification"] for s in scenarios_results
    )
    artifact = {
        "phase": "2.4",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "accuracy": passed / total if total else 0.0,
            "fallback_rate": fallback_count / total if total else 0.0,
            "avg_repair_steps": round(avg_steps, 2),
            "avg_tool_calls": round(avg_tool_calls, 2),
            "irrelevant_section_modification_count": irrelevant_total,
        },
        "scenarios": scenarios_results,
    }

    out_json = current_artifact_dir / "repair_eval.json"
    with open(out_json, "w", encoding="utf-8") as f:
        _json.dump(artifact, f, ensure_ascii=False, indent=2)

    from .report import render_markdown
    md = render_markdown(artifact)
    out_md = current_artifact_dir / "repair_eval_report.md"
    with open(out_md, "w", encoding="utf-8") as f:
        f.write(md)

    assert passed == total, (
        f"Repair eval: {passed}/{total} passed. failures={[
            s for s in scenarios_results if not s['passed']
        ]}"
    )
