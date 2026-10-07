"""Phase 2.6 — SequenceNumberAllocator Redis path / DB fallback / idempotency_key."""

from __future__ import annotations

import pytest

from app.agent_runtime.events.sequence_allocator import (
    SequenceNumberAllocator,
    compute_idempotency_key,
)


class _FakeRedis:
    """Minimal aioredis-compatible fake for SequenceNumberAllocator tests."""

    def __init__(self) -> None:
        self._counter: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self._counter[key] = self._counter.get(key, 0) + 1
        return self._counter[key]

    def pipeline(self) -> "_FakePipeline":
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, parent: _FakeRedis) -> None:
        self._parent = parent
        self._ops: list[str] = []

    def incr(self, key: str) -> "_FakePipeline":
        self._ops.append(key)
        return self

    async def execute(self) -> list[int]:
        return [await self._parent.incr(k) for k in self._ops]


@pytest.mark.asyncio
async def test_allocator_redis_path_returns_monotonic_ints() -> None:
    r = _FakeRedis()
    alloc = SequenceNumberAllocator(redis_client=r)
    n1 = await alloc.next(task_internal_id=42)
    n2 = await alloc.next(task_internal_id=42)
    n3 = await alloc.next(task_internal_id=99)
    assert n1 == 1
    assert n2 == 2
    assert n3 == 1  # per-task monotonic, not global


@pytest.mark.asyncio
async def test_allocator_next_batch_returns_consecutive_ints() -> None:
    r = _FakeRedis()
    alloc = SequenceNumberAllocator(redis_client=r)
    batch = await alloc.next_batch(task_internal_id=7, count=5)
    assert batch == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_allocator_next_batch_zero_returns_empty() -> None:
    r = _FakeRedis()
    alloc = SequenceNumberAllocator(redis_client=r)
    batch = await alloc.next_batch(task_internal_id=7, count=0)
    assert batch == []


def test_allocator_requires_at_least_one_backend() -> None:
    with pytest.raises(ValueError):
        SequenceNumberAllocator()


def test_compute_idempotency_key_format() -> None:
    k = compute_idempotency_key(
        task_id="t1",
        graph_run_id="gr1",
        node_name="n1",
        event_type="tool_finished",
        sequence_no=7,
    )
    assert k == "t1|gr1|n1|tool_finished|7"


def test_compute_idempotency_key_truncates_to_160_chars() -> None:
    long_str = "x" * 100
    k = compute_idempotency_key(
        task_id=long_str,
        graph_run_id=long_str,
        node_name=long_str,
        event_type=long_str,
        sequence_no=1,
    )
    assert len(k) <= 160


def test_compute_idempotency_key_handles_missing_fields() -> None:
    k = compute_idempotency_key(
        task_id=None,  # type: ignore[arg-type]
        graph_run_id="",
        node_name=None,  # type: ignore[arg-type]
        event_type="x",
        sequence_no=1,
    )
    assert k.startswith("|||x|")