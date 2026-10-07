"""Eval harness — runs 6 JSON scenarios through run_preparation (Phase 2.3 §10)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def load_scenarios() -> List[Dict[str, Any]]:
    """Load all *.json scenario files sorted by filename."""
    out: List[Dict[str, Any]] = []
    for path in sorted(SCENARIOS_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as f:
            out.append(json.load(f))
    return out


@pytest.fixture(scope="session")
def all_scenarios() -> List[Dict[str, Any]]:
    return load_scenarios()


def build_fake_llm(responses: List[str]):
    """Build a FakeLLMClient from a list of scripted JSON strings."""
    from tests.agent_runtime.test_plan_prep_agent.conftest import FakeLLMClient
    c = FakeLLMClient()
    for r in responses:
        c.push(r)
    return c


def build_stub_adapter(envelopes: List[Dict[str, Any]]):
    """Build a StubToolAdapter from a list of KB envelopes."""
    from tests.agent_runtime.test_plan_prep_agent.conftest import StubToolAdapter
    a = StubToolAdapter()
    a.envelopes_kb = envelopes or [
        {"success": True, "data": {"chunks": []}, "summary": "", "warnings": [],
         "error": None, "duration_ms": 1, "attempt": 1}
    ]
    return a