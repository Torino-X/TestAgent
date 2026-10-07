"""Tests for ``app.cache.singleflight.SingleFlight``.

Coverage (per prompt §11):
  * 100 concurrent tasks for the same key → loader called **exactly once**
  * leader exception propagates to all waiters
  * different keys run independently
  * inflight cleanup after success / failure
  * sequential calls run fresh loaders
"""

from __future__ import annotations

import asyncio
import contextlib
import gc

import pytest

from app.cache.singleflight import SingleFlight


@pytest.mark.asyncio
async def test_100_concurrent_one_loader() -> None:
    """The headline scenario: 100 concurrent callers coalesce to 1 loader."""
    sf = SingleFlight()
    counter = {"calls": 0}

    async def loader() -> str:
        counter["calls"] += 1
        # Tiny sleep to ensure the first call is in-flight when others arrive.
        await asyncio.sleep(0.01)
        return "value"

    results = await asyncio.gather(*[sf.do("key", loader) for _ in range(100)])
    assert counter["calls"] == 1, f"expected 1 loader call, got {counter['calls']}"
    assert results == ["value"] * 100


@pytest.mark.asyncio
async def test_exception_propagates_to_all_waiters() -> None:
    sf = SingleFlight()

    async def loader() -> str:
        await asyncio.sleep(0.01)
        raise RuntimeError("leader failure")

    with pytest.raises(RuntimeError, match="leader failure"):
        await asyncio.gather(*[sf.do("k", loader) for _ in range(10)])


@pytest.mark.asyncio
async def test_different_keys_run_independently() -> None:
    sf = SingleFlight()
    counter = {"calls": 0}

    async def loader(v: str) -> str:
        counter["calls"] += 1
        await asyncio.sleep(0.01)
        return v

    results = await asyncio.gather(
        sf.do("k1", lambda: loader("a")),
        sf.do("k2", lambda: loader("b")),
        sf.do("k3", lambda: loader("c")),
    )
    assert counter["calls"] == 3, counter
    assert set(results) == {"a", "b", "c"}


@pytest.mark.asyncio
async def test_sequential_calls_run_fresh_loaders() -> None:
    """After the first call finishes, the next call must run a fresh loader."""
    sf = SingleFlight()
    counter = {"calls": 0}

    async def loader() -> int:
        counter["calls"] += 1
        return counter["calls"]

    a = await sf.do("k", loader)
    b = await sf.do("k", loader)
    assert a == 1
    assert b == 2


@pytest.mark.asyncio
async def test_inflight_cleanup_on_success() -> None:
    sf = SingleFlight()

    async def loader() -> str:
        return "v"

    await sf.do("k", loader)
    assert sf.inflight_count() == 0
    assert "k" not in sf.keys()


@pytest.mark.asyncio
async def test_inflight_cleanup_on_failure() -> None:
    sf = SingleFlight()

    async def loader() -> str:
        raise RuntimeError("boom")

    with contextlib.suppress(RuntimeError):
        await sf.do("k", loader)
    assert sf.inflight_count() == 0, sf.keys()


@pytest.mark.asyncio
async def test_solo_leader_failure_does_not_emit_unretrieved_future_warning() -> None:
    """A failed request without coalesced waiters must not leak a Future error."""
    sf = SingleFlight()
    loop = asyncio.get_running_loop()
    contexts: list[dict] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))

    async def loader() -> str:
        raise RuntimeError("solo failure")

    try:
        with pytest.raises(RuntimeError, match="solo failure"):
            await sf.do("solo", loader)
        gc.collect()
        await asyncio.sleep(0)
        assert not any(
            context.get("message") == "Future exception was never retrieved"
            for context in contexts
        ), contexts
    finally:
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_no_future_leak_under_concurrent_load() -> None:
    """Run many rounds; verify the inflight map stays empty after each."""
    sf = SingleFlight()

    async def loader(i: int) -> int:
        return i

    for i in range(50):
        await sf.do(f"k-{i % 5}", lambda v=i: loader(v))
        assert sf.inflight_count() == 0, (i, sf.keys())


@pytest.mark.asyncio
async def test_waiter_observes_same_value_as_leader() -> None:
    """Leader returns a mutable; waiters must see the same reference."""
    sf = SingleFlight()
    sentinel = {"mut": 0}

    async def loader() -> dict:
        await asyncio.sleep(0.01)
        return sentinel

    results = await asyncio.gather(*[sf.do("k", loader) for _ in range(20)])
    assert all(r is sentinel for r in results), results
