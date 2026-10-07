"""Phase 2.8C — ``LangGraphDispatchAdapter`` Protocol 扩展(`run_incremental` + `run_repair`)。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.agent_runtime.api_dispatcher import LangGraphCoordinatorProtocol
from app.agent_runtime.langgraph_dispatch_adapter import LangGraphDispatchAdapter


@dataclass
class _Ctx:
    task_id: str = "t-adapter-001"


class _FullCoordinator:
    """实现所有 5 个 Protocol 方法的 fake coordinator。"""

    def __init__(self) -> None:
        self.calls: list = []

    async def run_pre_confirm(self, *, task_id, graph_run_id, initial_payload):
        self.calls.append(("run_pre_confirm", task_id, graph_run_id))
        return {"result": "pre_confirm"}

    async def run_post_confirm(self, *, task_id, graph_run_id, initial_payload):
        self.calls.append(("run_post_confirm", task_id, graph_run_id))
        return {"result": "post_confirm"}

    async def resume_section_confirmation(self, *, task_id, graph_run_id, decision):
        self.calls.append(("resume_section_confirmation", task_id, graph_run_id))
        return {"result": "resume"}

    async def run_incremental(self, *, task_id, graph_run_id, initial_payload):
        self.calls.append(("run_incremental", task_id, graph_run_id))
        return {"result": "incremental"}

    async def run_repair(self, *, task_id, graph_run_id, initial_payload):
        self.calls.append(("run_repair", task_id, graph_run_id))
        return {"result": "repair"}


def test_langgraph_dispatch_adapter_satisfies_extended_protocol() -> None:
    """LangGraphDispatchAdapter 必须满足 5 方法 Protocol(2.8C 扩展)。"""
    coord = _FullCoordinator()
    adapter = LangGraphDispatchAdapter(coord)
    assert isinstance(adapter, LangGraphCoordinatorProtocol)


@pytest.mark.asyncio
async def test_run_incremental_invokes_coordinator_with_dict_payload() -> None:
    """run_incremental:dict payload → coordinator.run_incremental(task_id, graph_run_id, initial_payload)。"""
    coord = _FullCoordinator()
    adapter = LangGraphDispatchAdapter(coord)
    payload = {
        "task_id": "t-inc-100",
        "incremental_intent": {"kind": "extend_section", "section_id": "s3"},
        "source_artifact_public_id": "art-200",
        "modification_idempotency_key": "idem-abc",
    }
    result = await adapter.run_incremental(payload)
    assert result == {"result": "incremental"}
    assert coord.calls[0][0] == "run_incremental"
    assert coord.calls[0][1] == "t-inc-100"
    assert coord.calls[0][2].startswith("run-t-inc-100-")


@pytest.mark.asyncio
async def test_run_repair_invokes_coordinator_with_dict_payload() -> None:
    """run_repair:dict payload → coordinator.run_repair(task_id, graph_run_id, initial_payload)。"""
    coord = _FullCoordinator()
    adapter = LangGraphDispatchAdapter(coord)
    payload = {
        "task_id": "t-rep-100",
        "review_issues": [{"rule_id": "R1", "severity": "block"}],
        "block_issues": [{"section_id": "s3", "rule_id": "R1"}],
    }
    result = await adapter.run_repair(payload)
    assert result == {"result": "repair"}
    assert coord.calls[0][0] == "run_repair"
    assert coord.calls[0][1] == "t-rep-100"


@pytest.mark.asyncio
async def test_run_incremental_rejects_non_dict_payload() -> None:
    """run_incremental 非 dict payload → ValueError(防御性)。"""
    coord = _FullCoordinator()
    adapter = LangGraphDispatchAdapter(coord)
    with pytest.raises(ValueError, match="expects dict payload"):
        await adapter.run_incremental("not-a-dict")


@pytest.mark.asyncio
async def test_run_repair_rejects_payload_without_task_id() -> None:
    """run_repair payload 缺 task_id → ValueError。"""
    coord = _FullCoordinator()
    adapter = LangGraphDispatchAdapter(coord)
    with pytest.raises(ValueError, match="requires 'task_id'"):
        await adapter.run_repair({"foo": "bar"})