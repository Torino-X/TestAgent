"""Shared fixtures for the Business Cache test suite.

All fakeredis-backed tests acquire an isolated ``FakeRedis`` instance
so they can run concurrently under ``pytest -n auto`` and don't
interfere with the live ``CacheBackend`` singleton.

The pattern matches the project's existing fakeredis fixtures in
``tests/agent_runtime/test_dispatch/test_*redis_inflight*.py``: a
plain ``@pytest.fixture async def`` that yields the fake client and
flushes on teardown.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest
from fakeredis import aioredis as fakeredis_aioredis


def make_fake_redis() -> "fakeredis_aioredis.FakeRedis":
    """Construct a fresh in-process ``FakeRedis`` for a single test.

    Tests that need to share state across instances should pass the
    same ``server`` argument (see ``shared_fake_redis_factory``).
    """
    return fakeredis_aioredis.FakeRedis(decode_responses=False)


@pytest.fixture
async def fake_redis() -> AsyncIterator["fakeredis_aioredis.FakeRedis"]:
    """Fresh in-process FakeRedis — fully isolated, flushed on teardown."""
    redis = make_fake_redis()
    try:
        yield redis
    finally:
        try:
            await redis.flushall()
        finally:
            await redis.aclose()


@pytest.fixture
async def fake_redis_pair() -> AsyncIterator[
    tuple["fakeredis_aioredis.FakeRedis", "fakeredis_aioredis.FakeRedis"]
]:
    """Two FakeRedis instances bound to the **same** server.

    Useful for "Worker A modifies config; Worker B sees it on next read"
    multi-worker consistency scenarios (Step 5).
    """
    server = fakeredis_aioredis.FakeServer()
    a = fakeredis_aioredis.FakeRedis(server=server, decode_responses=False)
    b = fakeredis_aioredis.FakeRedis(server=server, decode_responses=False)
    try:
        yield a, b
    finally:
        try:
            await a.flushall()
            await b.flushall()
        finally:
            await a.aclose()
            await b.aclose()