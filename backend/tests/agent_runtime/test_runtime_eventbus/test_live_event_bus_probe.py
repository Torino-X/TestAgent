"""Phase 2.6 — LiveEventBusProbe resolve / resolve_with_health_check."""

from __future__ import annotations

import pytest

from app.agent_runtime.events.live_event_bus import (
    InMemoryLiveEventBus,
    LiveEventBusProbe,
    RedisLiveEventBus,
)


def test_probe_resolve_returns_inmemory_when_no_redis_url() -> None:
    bus = LiveEventBusProbe.resolve(redis_url=None)
    assert isinstance(bus, InMemoryLiveEventBus)


def test_probe_resolve_returns_inmemory_when_redis_url_empty() -> None:
    bus = LiveEventBusProbe.resolve(redis_url="")
    assert isinstance(bus, InMemoryLiveEventBus)


def test_probe_resolve_returns_bus_with_redis_url() -> None:
    """Redis 库未装时 RuntimeError 退化 InMemory;装了就返回 RedisLiveEventBus。
    测试接受任一结果;只要不是异常即可。"""

    bus = LiveEventBusProbe.resolve(redis_url="redis://example:6379/0")
    assert isinstance(bus, (InMemoryLiveEventBus, RedisLiveEventBus))


@pytest.mark.asyncio
async def test_probe_with_health_check_returns_inmemory_when_no_url() -> None:
    bus = await LiveEventBusProbe.resolve_with_health_check(redis_url=None)
    assert isinstance(bus, InMemoryLiveEventBus)
    assert await bus.health() is True


@pytest.mark.asyncio
async def test_probe_with_health_check_degrades_when_redis_unreachable() -> None:
    """Redis 不通 → 自动退化 InMemory(走 fallback 路径:Redis 库未装或 ping 失败)。"""

    bus = await LiveEventBusProbe.resolve_with_health_check(
        redis_url="redis://nope:9999/0"
    )
    # 退化路径返回 InMemoryLiveEventBus
    assert isinstance(bus, InMemoryLiveEventBus)


@pytest.mark.asyncio
async def test_probe_with_health_check_degrades_when_ping_never_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A black-holed Redis PING must not block the FastAPI lifespan."""
    import asyncio

    async def never_returns(_self: RedisLiveEventBus) -> bool:
        await asyncio.Future()
        return True

    monkeypatch.setattr(RedisLiveEventBus, "health", never_returns)

    bus = await LiveEventBusProbe.resolve_with_health_check(
        redis_url="redis://127.0.0.1:1/0",
        health_timeout_seconds=0.01,
    )

    assert isinstance(bus, InMemoryLiveEventBus)
