"""Phase 2.8C ADR-2.8C-11 — Redis InFlight finally 释放修复验证。"""

from __future__ import annotations

import dataclasses
import inspect

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


pytestmark = pytest.mark.skipif(
    not HAS_FAKEREDIS,
    reason="fakeredis not installed; skip Redis finally release tests",
)


def _probe() -> ProbeReport:
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


class _StubCoordinator:
    async def run_pre_confirm(self, payload): return None
    async def run_post_confirm(self, payload): return None
    async def resume_section_confirmation(self, payload): return None
    async def run_incremental(self, payload): return None
    async def run_repair(self, payload): return None


def _all_six_dispatch_methods_release_redis_in_finally() -> None:
    """每个 dispatch_* 方法 finally 块必须包含 ``_release_redis_owner`` 调用(ADR-2.8C-11)。"""
    for method_name in (
        "dispatch_new_task",
        "dispatch_resume",
        "dispatch_confirm",
        "dispatch_incremental_task",
        "dispatch_incremental_resume",
        "dispatch_repair_task",
    ):
        method = getattr(ApiDispatcher, method_name)
        src = inspect.getsource(method)
        # finally: 块必须包含 _release_redis_owner
        assert "finally:" in src, f"{method_name} 缺 finally 块"
        # finally 之后的代码段含 _release_redis_owner
        finally_idx = src.find("finally:")
        assert finally_idx > 0
        post_finally = src[finally_idx:]
        assert "_release_redis_owner" in post_finally, (
            f"{method_name} finally 块必须调 _release_redis_owner "
            "(phase 2.8C ADR-2.8C-11 修复 2.8B 遗留 bug)"
        )


@pytest.mark.asyncio
async def test_dispatch_new_task_releases_redis_owner_on_success() -> None:
    """dispatch_new_task 成功后 Redis 锁被 finally 释放(不再等 TTL)。"""
    redis_client = fakeredis_aioredis.FakeRedis()
    reg = RedisInFlightRegistry(redis_client=redis_client, ttl_seconds=60)
    try:
        d = ApiDispatcher(
            coordinator=_StubCoordinator(),
            probe_report=_probe(),
            feature_flags=dataclasses.replace(
                get_feature_flags(),
                production_dispatch_enabled=True,
                langgraph_enabled=True,
                dynamic_agent_api_enabled=True,
            ),
            redis_inflight=reg,
        )
        await d.dispatch_new_task(
            task_public_id="t-rel-001",
            task_engine_type="langgraph",
            context={"task_id": "t-rel-001"},
        )
        # 完成后 Redis 锁已释放 → 新一次 try_acquire 应直接拿到锁
        reacquired = await reg.try_acquire(task_public_id="t-rel-001", engine="other")
        assert reacquired is not None
        # 清理
        await reg.release(task_public_id="t-rel-001", owner=reacquired)
    finally:
        await reg.aclose()


@pytest.mark.asyncio
async def test_dispatch_incremental_task_releases_redis_on_exception() -> None:
    """dispatch_incremental_task 抛异常时 Redis 锁也由 finally 释放(防御性)。"""
    redis_client = fakeredis_aioredis.FakeRedis()
    reg = RedisInFlightRegistry(redis_client=redis_client, ttl_seconds=60)
    try:
        class _RaisingCoordinator(_StubCoordinator):
            async def run_incremental(self, payload):
                raise RuntimeError("simulated incremental crash")

        d = ApiDispatcher(
            coordinator=_RaisingCoordinator(),
            probe_report=_probe(),
            feature_flags=dataclasses.replace(
                get_feature_flags(),
                production_dispatch_enabled=True,
                langgraph_enabled=True,
                dynamic_agent_api_enabled=True,
            ),
            redis_inflight=reg,
        )
        with pytest.raises(RuntimeError, match="simulated incremental crash"):
            await d.dispatch_incremental_task(
                task_public_id="t-rel-002",
                task_engine_type="langgraph",
                payload={
                    "task_id": "t-rel-002",
                    "incremental_intent": {"kind": "extend"},
                    "source_artifact_public_id": "art-002",
                    "modification_idempotency_key": "idem-002",
                },
            )
        # 即使抛异常,finally 块释放 Redis 锁 → 新一次 try_acquire 应直接拿到锁
        reacquired = await reg.try_acquire(task_public_id="t-rel-002", engine="other")
        assert reacquired is not None
        await reg.release(task_public_id="t-rel-002", owner=reacquired)
    finally:
        await reg.aclose()
