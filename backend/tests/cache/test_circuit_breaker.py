"""Tests for ``app.cache.circuit_breaker.CircuitBreaker``.

Coverage (per prompt §13):
  * closed → open after N failures
  * open → fast-fail
  * open → half-open after timeout
  * half-open → closed on probe success
  * half-open → open on probe failure
  * success in closed state resets failure counter
"""

from __future__ import annotations

import asyncio

import pytest

from app.cache.circuit_breaker import (
    BreakerConfig,
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
)


def _ok_coro(value: str = "ok") -> "asyncio.Future[str]":
    async def _f() -> str:
        return value

    return _f()


async def _ok(value: str = "ok") -> str:
    return value


def _fail_coro(exc: Exception | None = None) -> "asyncio.Future[str]":
    async def _f() -> str:
        raise exc or RuntimeError("boom")

    return _f()


async def _fail() -> str:
    raise RuntimeError("boom")


class TestClosedState:
    async def test_passes_through_on_success(self) -> None:
        cb = CircuitBreaker(BreakerConfig(failure_threshold=3, open_seconds=0.05))
        assert cb.state is BreakerState.CLOSED
        result = await cb.call(lambda: _ok("x"))
        assert result == "x"
        assert cb.state is BreakerState.CLOSED

    async def test_resets_failure_count_on_success(self) -> None:
        cb = CircuitBreaker(BreakerConfig(failure_threshold=3, open_seconds=0.05))
        # Two failures then a success; counter resets so 3 more failures needed.
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state is BreakerState.CLOSED
        await cb.call(_ok)
        # Two more failures → still closed (counter restarted from 0).
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state is BreakerState.CLOSED


class TestOpensAfterN:
    async def test_opens_after_threshold_failures(self) -> None:
        cb = CircuitBreaker(BreakerConfig(failure_threshold=3, open_seconds=0.05))
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state is BreakerState.OPEN

    async def test_open_state_fast_fails(self) -> None:
        cb = CircuitBreaker(BreakerConfig(failure_threshold=2, open_seconds=10.0))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state is BreakerState.OPEN
        with pytest.raises(CircuitOpenError):
            await cb.call(_ok)


class TestHalfOpen:
    async def test_transitions_open_to_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker(BreakerConfig(failure_threshold=2, open_seconds=0.05))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state is BreakerState.OPEN
        await asyncio.sleep(0.06)
        # First call after the open period enters HALF_OPEN as probe.
        result = await cb.call(_ok)
        assert result == "ok"
        assert cb.state is BreakerState.CLOSED, "probe success must close breaker"

    async def test_half_open_failure_reopens(self) -> None:
        cb = CircuitBreaker(BreakerConfig(failure_threshold=2, open_seconds=0.05))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state is BreakerState.OPEN
        await asyncio.sleep(0.06)
        with pytest.raises(RuntimeError):
            await cb.call(_fail)
        assert cb.state is BreakerState.OPEN, "probe failure must reopen breaker"

    async def test_half_open_full_probe_queue_blocks_extras(self) -> None:
        """Only ``half_open_max_probes`` (default 1) probes run at once.

        Second concurrent call must fast-fail.
        """
        cb = CircuitBreaker(BreakerConfig(failure_threshold=2, open_seconds=0.05))
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        await asyncio.sleep(0.06)

        async def slow_probe() -> str:
            await asyncio.sleep(0.05)
            return "ok"

        # First call enters half-open and holds the probe slot.
        probe_task = asyncio.create_task(cb.call(slow_probe))
        await asyncio.sleep(0.005)  # let probe start
        # Second call must be rejected (probe slot full).
        with pytest.raises(CircuitOpenError):
            await cb.call(_ok)
        result = await probe_task
        assert result == "ok"
        assert cb.state is BreakerState.CLOSED


class TestRecoverAfterMultipleCycles:
    async def test_recovers_then_reopens_then_recovers_again(self) -> None:
        cb = CircuitBreaker(BreakerConfig(failure_threshold=2, open_seconds=0.02))
        # Cycle 1: open
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state is BreakerState.OPEN
        # recover
        await asyncio.sleep(0.03)
        assert (await cb.call(_ok)) == "ok"
        assert cb.state is BreakerState.CLOSED
        # Cycle 2: open again
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)
        assert cb.state is BreakerState.OPEN
        # recover again
        await asyncio.sleep(0.03)
        assert (await cb.call(_ok)) == "ok"
        assert cb.state is BreakerState.CLOSED


class TestExceptionTypes:
    async def test_any_exception_counts_as_failure(self) -> None:
        cb = CircuitBreaker(BreakerConfig(failure_threshold=2, open_seconds=0.05))

        async def value_error() -> str:
            raise ValueError("typed")

        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.call(value_error)
        assert cb.state is BreakerState.OPEN