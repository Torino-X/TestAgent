"""Phase 2.4 Review Repair Agent 评测 harness.

镜像 ``test_plan_prep_eval/conftest.py`` 但适配 Repair Agent:

* 复用 ``test_plan_repair_agent/conftest.py`` 的 FakeLLMClient / StubToolAdapter
  / runtime_ctx / base_state
* ``load_scenario(path) -> dict`` 读 JSON
* ``build_scripted_llm(scenario) -> FakeLLMClient``  按 scripted_llm_responses
  顺序压栈
* ``build_envelope_map(scenario) -> dict``          取 stub_tool_adapter 注入
* ``run_scenario(scenario, sink, adapter, ctx) -> RepairResult``    触发 repair
"""

from __future__ import annotations

import json as _json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# 复用 Repair 的 conftest helper
from tests.agent_runtime.test_plan_repair_agent.conftest import (  # noqa: F401
    FakeLLMClient,
    StubToolAdapter,
    _null_session_factory,
    _StubCancel,
    _make_review_result,
    in_memory_sink,
    runtime_ctx,
    base_state,
    call_regen_decision,
    call_review_decision,
    call_kb_decision,
    finish_decision,
    fail_decision,
    regen_envelope_ok,
    review_envelope_passed,
    review_envelope_failed,
    kb_envelope_ok,
)


SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def load_scenario(name: str) -> Dict[str, Any]:
    """Load a scenario JSON by file name (with or without .json)."""
    if not name.endswith(".json"):
        name = f"{name}.json"
    path = SCENARIOS_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return _json.load(f)


def list_scenarios() -> List[str]:
    """List all scenario file basenames (without .json)."""
    return sorted(
        p.stem for p in SCENARIOS_DIR.glob("*.json")
    )


def build_scripted_llm(responses: Optional[List[str]] = None) -> FakeLLMClient:
    """Build a FakeLLMClient with the given scripted JSON responses."""
    return FakeLLMClient(responses=list(responses or []))


def build_envelope_map(env_cfg: Dict[str, List[Dict[str, Any]]]) -> Dict[str, List[Dict[str, Any]]]:
    """Pass-through envelope map for StubToolAdapter."""
    return dict(env_cfg or {})


def build_base_state_from_scenario(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """Construct a base_state from scenario.setup dict."""
    setup = scenario.get("setup") or {}
    review_issues_cfg = setup.get("review_issues") or {}
    return {
        "task_id": setup.get("task_id", "repair-eval"),
        "graph_run_id": setup.get("graph_run_id", "run-repair-eval"),
        "review_result": _make_review_result(
            issue_ids=review_issues_cfg.get("issue_ids", ["iss-1"]),
            section_ids=review_issues_cfg.get("section_ids", ["sec-A"]),
            level=review_issues_cfg.get("level", "failed"),
            kind=review_issues_cfg.get("kind", "forbidden_pattern"),
            severity=review_issues_cfg.get("severity", "block"),
        ),
        "test_plan_content": setup.get(
            "test_plan_content",
            {"sections": [{"section_id": "sec-A", "content": "原始"}]},
        ),
        "template_structure": setup.get(
            "template_structure",
            {"generation_config": {"constraints": {"max_chars_per_section": 5000}}},
        ),
        "review_loop_count": 0,
        "repair_loop_count": 0,
        "repair_agent_enabled": scenario.get("repair_agent_enabled", True),
        "locked_section_ids": setup.get("locked_section_ids", []),
        "completed_nodes": [],
        "user_prompt": "",
    }


# ── Optional pytest fixtures (used by test_eval.py) ──────────────────────


@pytest.fixture
def scenarios_dir() -> Path:
    return SCENARIOS_DIR


@pytest.fixture
def current_artifact_dir(tmp_path_factory) -> Path:
    """默认写到 backend/_artifacts/repair_eval/ (commit-ignore)."""
    artifacts = Path(__file__).parent.parent.parent.parent / "_artifacts" / "repair_eval"
    artifacts.mkdir(parents=True, exist_ok=True)
    return artifacts
