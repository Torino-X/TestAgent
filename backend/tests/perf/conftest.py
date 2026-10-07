"""Perf test conftest — 复用 tests/cache 的 fake_redis fixtures."""

from tests.cache.conftest import fake_redis, fake_redis_pair  # noqa: F401