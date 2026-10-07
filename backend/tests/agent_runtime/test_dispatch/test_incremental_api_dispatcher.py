"""Phase 2.8R — incremental / repair engine_type 二元化。

Phase 2.8R 重新语义:
* engine_type 只接受 legacy / langgraph
* incremental / repair 通过 task_engine_type="langgraph" 表达
* 历史值(incremental/repair) 抛 InvalidEngineTypeError
* legacy 是无效的 incremental 入口,显式抛 LangGraphNotReadyError
"""

from __future__ import annotations

import dataclasses
from typing import Any, Dict, Optional

import pytest

try:
    from fakeredis import aioredis as fakeredis_aioredis
    HAS_FAKEREDIS = True
except ImportError:  # pragma: no cover
    HAS_FAKEREDIS = False

from app.agent_runtime.api_dispatcher import ApiDispatcher
from app.agent_runtime.dispatch_errors import (
    IncrementalPayloadInvalidError,
    RepairPayloadInvalidError,
)
from app.agent_runtime.feature_flags import get_feature_flags
from app.agent_runtime.persistence import ProbeReport
from app.agent_runtime.persistence.redis_inflight_registry import RedisInFlightRegistry
from app.core.exceptions import (
    EngineDispatcherUnavailableError,
    InvalidEngineTypeError,
    LangGraphNotReadyError,
    UnsupportedLegacyTaskError,
)


pytestmark = pytest.mark.skipif(
    not HAS_FAKEREDIS,
    reason="fakeredis not installed; skip incremental api dispatcher tests",
)


def _make_probe() -> ProbeReport:
    return ProbeReport(
        postgres_ok=True,
        postgres_url_echo="postgresql://user:***@host:5432/db",
        postgres_latency_ms=120,
        eventbus_kind="InMemory",
        eventbus_url_echo="redis://localhost:6379/0",
        redis_ok=True,
        ready=True,
        production_dispatch_forced_off=False,
    )


class _StubOrchestrator:
    async def run(self, ctx): return None
    async def resume_after_confirm(self, ctx): return None


class _SpyCoordinator:
    def __init__(self) -> None:
        self.incremental_calls = []
        self.repair_calls = []
        self.pre_confirm_calls = []

    async def run_pre_confirm(self, payload):
        self.pre_confirm_calls.append(payload)
        return None
    async def run_post_confirm(self, payload): return None
    async def run_resume_section_confirmation(self, payload): return None

    async def run_incremental(self, payload):
        self.incremental_calls.append(payload)
        return {"engine": "langgraph", "graph_name": "incremental_test_plan", "result": "ok"}

    async def run_repair(self, payload):
        self.repair_calls.append(payload)
        return {"engine": "langgraph", "graph_name": "repair_agent", "result": "ok"}


def _make_dispatcher(*, redis_inflight=None, dynamic_api=True) -> ApiDispatcher:
    flags = dataclasses.replace(
        get_feature_flags(),
        production_dispatch_enabled=True,
        langgraph_enabled=True,
        dynamic_agent_api_enabled=dynamic_api,
    )
    return ApiDispatcher(
        coordinator=_SpyCoordinator(),
        probe_report=_make_probe(),
        feature_flags=flags,
        redis_inflight=redis_inflight,
    )


# ── 1. 接受 task_engine_type="langgraph"(incremental / repair) ─────


@pytest.mark.asyncio
async def test_dispatch_incremental_task_full_unlocked() -> None:
    """Phase 2.8R:task_engine_type="langgraph" + graph_name="incremental_test_plan" → LangGraph 路径。"""
    coord_spy = _SpyCoordinator()
    d = ApiDispatcher(
        coordinator=coord_spy,
        probe_report=_make_probe(),
        feature_flags=dataclasses.replace(
            get_feature_flags(),
            production_dispatch_enabled=True,
            langgraph_enabled=True,
            dynamic_agent_api_enabled=True,
        ),
    )
    outcome = await d.dispatch_incremental_task(
        task_public_id="t-inc-001",
        task_engine_type="langgraph",
        payload={
            "task_id": "t-inc-001",
            "incremental_intent": {"kind": "extend", "section_id": "s3"},
            "source_artifact_public_id": "art-001",
            "modification_idempotency_key": "idem-001",
        },
    )
    assert outcome.engine == "langgraph"
    assert outcome.fallback_used is False
    assert len(coord_spy.incremental_calls) == 1
    assert coord_spy.incremental_calls[0]["modification_idempotency_key"] == "idem-001"


@pytest.mark.asyncio
async def test_dispatch_from_outbox_row_routes_incremental_graph(monkeypatch) -> None:
    """Outbox 中 graph_name=incremental_test_plan 时必须走 run_incremental。"""
    from app.agent_runtime.incremental.subgraph import GRAPH_NAME_INCREMENTAL

    coord_spy = _SpyCoordinator()
    d = ApiDispatcher(
        coordinator=coord_spy,
        probe_report=_make_probe(),
        feature_flags=dataclasses.replace(
            get_feature_flags(),
            production_dispatch_enabled=True,
            langgraph_enabled=True,
            dynamic_agent_api_enabled=True,
            incremental_agent_enabled=True,
        ),
    )

    async def _fake_context(*, session, task_internal_id):
        return type(
            "Ctx",
            (),
            {
                    "task_id": "task_inc_001",
                    "task_internal_id": int(task_internal_id),
                    "conversation_id": "conv_inc_001",
                    "conversation_internal_id": 5,
                "user_internal_id": 1,
                "user_id": "1",
                "user_prompt": "补充测试策略",
                "requirement_file_id": "file_req",
                "template_file_id": "file_tpl",
                "task_context_json": {
                    "incremental_intent": {"scope": {"kind": "modify_section"}},
                    "source_artifact_public_id": "art_001",
                    "modification_idempotency_key": "idem_001",
                },
            },
        )()

    monkeypatch.setattr(
        "app.services.agent_context_factory.build_agent_context_from_task_internal_id",
        _fake_context,
    )
    row = type(
        "Row",
        (),
        {
            "task_id": 11,
            "engine_type": "langgraph",
            "graph_name": GRAPH_NAME_INCREMENTAL,
            "graph_version": "v1",
            "payload_json": {
                "request_type": "incremental_task",
                "incremental_intent": {"scope": {"kind": "modify_section"}},
                "source_artifact_public_id": "art_001",
                "modification_idempotency_key": "idem_001",
            },
        },
    )()

    outcome = await d.dispatch_from_outbox_row(row=row, session=object())

    assert outcome.engine == "langgraph"
    assert len(coord_spy.incremental_calls) == 1
    assert coord_spy.pre_confirm_calls == []
    assert coord_spy.incremental_calls[0]["graph_name"] == GRAPH_NAME_INCREMENTAL


@pytest.mark.asyncio
async def test_dispatch_incremental_resume_with_decision() -> None:
    """Phase 2.8R:incremental_resume 走 langgraph 路径。"""
    coord_spy = _SpyCoordinator()
    d = ApiDispatcher(
        coordinator=coord_spy,
        probe_report=_make_probe(),
        feature_flags=dataclasses.replace(
            get_feature_flags(),
            production_dispatch_enabled=True,
            langgraph_enabled=True,
            dynamic_agent_api_enabled=True,
        ),
    )
    outcome = await d.dispatch_incremental_resume(
        task_public_id="t-inc-resume-001",
        task_engine_type="langgraph",
        payload={
            "task_id": "t-inc-resume-001",
            "kind": "incremental_resume",
            "decision": {"action": "apply", "section_id": "s3"},
        },
    )
    assert outcome.engine == "langgraph"
    assert coord_spy.incremental_calls[0]["decision"]["action"] == "apply"


@pytest.mark.asyncio
async def test_dispatch_repair_task_full_unlocked() -> None:
    """Phase 2.8R:repair 走 langgraph 路径,触发 run_repair。"""
    coord_spy = _SpyCoordinator()
    d = ApiDispatcher(
        coordinator=coord_spy,
        probe_report=_make_probe(),
        feature_flags=dataclasses.replace(
            get_feature_flags(),
            production_dispatch_enabled=True,
            langgraph_enabled=True,
            dynamic_agent_api_enabled=True,
        ),
    )
    outcome = await d.dispatch_repair_task(
        task_public_id="t-rep-001",
        task_engine_type="langgraph",
        payload={
            "task_id": "t-rep-001",
            "review_issues": [{"rule_id": "R1", "severity": "block"}],
            "block_issues": [{"section_id": "s3"}],
            "source": "route_after_review",
        },
    )
    assert outcome.engine == "langgraph"
    assert coord_spy.repair_calls[0]["source"] == "route_after_review"


# ── 2. 历史 engine_type(incremental/repair/preparation_agent)抛错 ─────


@pytest.mark.asyncio
async def test_dispatch_incremental_task_rejects_legacy_engine_type() -> None:
    """Phase 2.8R:incremental 任务 engine_type=legacy 显式抛错,不静默走 legacy。"""
    d = _make_dispatcher()
    with pytest.raises(UnsupportedLegacyTaskError):
        await d.dispatch_incremental_task(
            task_public_id="t-inc-legacy",
            task_engine_type="legacy",
            payload={
                "task_id": "t-inc-legacy",
                "incremental_intent": {"kind": "extend"},
                "source_artifact_public_id": "art",
                "modification_idempotency_key": "idem",
            },
        )


@pytest.mark.asyncio
async def test_dispatch_incremental_task_rejects_historical_engine_type() -> None:
    """Phase 2.8R:历史 engine_type "incremental"/"repair" 不再被接受。"""
    d = _make_dispatcher()
    for bad in ("incremental", "repair", "preparation_agent"):
        with pytest.raises(UnsupportedLegacyTaskError):
            await d.dispatch_incremental_task(
                task_public_id=f"t-{bad}",
                task_engine_type=bad,
                payload={
                    "task_id": f"t-{bad}",
                    "incremental_intent": {"kind": "extend"},
                    "source_artifact_public_id": "art",
                    "modification_idempotency_key": "idem",
                },
            )


# ── 3. Payload 验证仍工作 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_incremental_payload_missing_required_field() -> None:
    """incremental payload 缺 modification_idempotency_key → IncrementalPayloadInvalidError。"""
    d = _make_dispatcher()
    with pytest.raises(IncrementalPayloadInvalidError) as exc_info:
        await d.dispatch_incremental_task(
            task_public_id="t-inc-miss",
            task_engine_type="langgraph",
            payload={
                "task_id": "t-inc-miss",
                "incremental_intent": {"kind": "extend"},
                "source_artifact_public_id": "art-miss",
            },
        )
    assert exc_info.value.missing_field == "modification_idempotency_key"


@pytest.mark.asyncio
async def test_incremental_resume_payload_invalid_kind() -> None:
    """incremental_resume payload.kind != 'incremental_resume' → InvalidError。"""
    d = _make_dispatcher()
    with pytest.raises(IncrementalPayloadInvalidError) as exc_info:
        await d.dispatch_incremental_resume(
            task_public_id="t-resume-miss",
            task_engine_type="langgraph",
            payload={
                "task_id": "t-resume-miss",
                "kind": "wrong_kind",
                "decision": {"action": "apply"},
            },
        )
    assert exc_info.value.missing_field == "kind=incremental_resume"


@pytest.mark.asyncio
async def test_repair_payload_missing_task_id() -> None:
    """repair payload 缺 task_id → RepairPayloadInvalidError。"""
    d = _make_dispatcher()
    with pytest.raises(RepairPayloadInvalidError) as exc_info:
        await d.dispatch_repair_task(
            task_public_id="t-rep-miss",
            task_engine_type="langgraph",
            payload={"review_issues": []},
        )
    assert exc_info.value.missing_field == "task_id"


# ── 4. Redis InFlight finally 释放(沿用) ─────────────────────────


@pytest.mark.asyncio
async def test_incremental_dispatch_releases_redis_owner_on_finally() -> None:
    """incremental dispatch finally 释放 Redis 锁(Phase 2.8C ADR-11)。"""
    if not HAS_FAKEREDIS:
        pytest.skip("fakeredis required")
    redis_client = fakeredis_aioredis.FakeRedis()
    reg = RedisInFlightRegistry(redis_client=redis_client, ttl_seconds=60)
    try:
        d = _make_dispatcher(redis_inflight=reg)
        await d.dispatch_incremental_task(
            task_public_id="t-redis-rel",
            task_engine_type="langgraph",
            payload={
                "task_id": "t-redis-rel",
                "incremental_intent": {"kind": "extend"},
                "source_artifact_public_id": "art-rel",
                "modification_idempotency_key": "idem-rel",
            },
        )
        # Redis 锁应已被 finally 释放 → 新一次 try_acquire 应直接拿到锁
        reacquired = await reg.try_acquire(task_public_id="t-redis-rel", engine="other")
        assert reacquired is not None
        await reg.release(task_public_id="t-redis-rel", owner=reacquired)
    finally:
        await reg.aclose()
