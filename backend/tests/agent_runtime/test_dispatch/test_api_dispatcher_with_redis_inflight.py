"""LangGraph dispatcher + Redis in-flight guard tests."""
from __future__ import annotations

import dataclasses

import pytest

try:
    from fakeredis import aioredis as fakeredis_aioredis
    HAS_FAKEREDIS = True
except ImportError:  # pragma: no cover
    HAS_FAKEREDIS = False

from app.agent_runtime.api_dispatcher import ApiDispatcher
from app.agent_runtime.feature_flags import get_feature_flags
from app.agent_runtime.persistence import ProbeReport
from app.agent_runtime.persistence.redis_inflight_registry import RedisInFlightRegistry

pytestmark = pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis not installed")


class _Coordinator:
    def __init__(self): self.calls = 0
    async def run_pre_confirm(self, payload):
        self.calls += 1
        return payload


def _dispatcher(redis_inflight=None):
    coordinator = _Coordinator()
    flags = dataclasses.replace(
        get_feature_flags(), production_dispatch_enabled=True, langgraph_enabled=True
    )
    probe = ProbeReport(
        postgres_ok=True,
        postgres_url_echo="postgresql://user:***@host/db",
        postgres_latency_ms=1,
        eventbus_kind="InMemory",
        eventbus_url_echo="",
        redis_ok=True,
        ready=True,
        production_dispatch_forced_off=False,
        langgraph_readiness=True,
        checkpointer_type="AsyncPostgresSaver",
    )
    return ApiDispatcher(
        coordinator=coordinator,
        probe_report=probe,
        feature_flags=flags,
        redis_inflight=redis_inflight,
    ), coordinator


@pytest.fixture
async def fake_redis():
    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    yield redis
    await redis.flushall()
    await redis.aclose()


@pytest.mark.asyncio
async def test_langgraph_dispatch_acquires_and_releases_redis_guard(fake_redis) -> None:
    registry = RedisInFlightRegistry(redis_client=fake_redis, worker_id="worker-a")
    dispatcher, coordinator = _dispatcher(registry)
    outcome = await dispatcher.dispatch_new_task(
        task_public_id="task-1",
        task_engine_type="langgraph",
        context={"value": 1},
    )
    assert outcome.engine == "langgraph"
    assert coordinator.calls == 1
    assert await fake_redis.get(registry._key("task-1")) is None


@pytest.mark.asyncio
async def test_redis_outage_degrades_to_process_guard() -> None:
    class BrokenRedis:
        async def set(self, *_args, **_kwargs): raise ConnectionError("offline")

    registry = RedisInFlightRegistry(redis_client=BrokenRedis(), worker_id="worker-a")
    dispatcher, coordinator = _dispatcher(registry)
    outcome = await dispatcher.dispatch_new_task(
        task_public_id="task-2",
        task_engine_type="langgraph",
        context={},
    )
    assert outcome.engine == "langgraph"
    assert coordinator.calls == 1
