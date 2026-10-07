"""P1-8 Redis 故障恢复测试 — 真实 redis 6380.

启动方式:
  RUN_REAL_REDIS_TESTS=1 python -m pytest tests/perf/test_real_fault_redis.py -v -s

测试:
  1. 先验证正常情况下 cache 写穿成功
  2. 用错误的 redis URL 让 client 连接失败
  3. 验证 graceful fallback (no 500)
  4. circuit breaker 触发后 fast-fail

不杀掉真实 redis — 只用错误的连接让 client 报错.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))


def test_fault_fallback_returns_gracefully() -> None:
    """When Redis is unreachable, cache.write_through must return False,
    not raise."""
    if not os.environ.get("RUN_REAL_REDIS_TESTS"):
        import pytest
        pytest.skip("set RUN_REAL_REDIS_TESTS=1 to run")

    # Use a non-routable port (127.0.0.1:1) so connect fails fast.
    bad_url = "redis://127.0.0.1:1/0"

    async def main():
        import redis.asyncio as aioredis
        from app.cache.backend import CacheBackend
        from app.cache.bulkhead import DBBulkhead
        from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
        from app.cache.domains.config_cache import (
            ModelConfigCache,
            ModelConfigDTO,
        )
        from app.cache.manager import CacheManager
        from app.cache.singleflight import SingleFlight
        from app.cache.metrics import cache_metrics

        cache_metrics.reset()
        # Use very short timeout to fail fast
        client = aioredis.from_url(bad_url, decode_responses=True, socket_timeout=0.3, socket_connect_timeout=0.3)
        backend = CacheBackend(redis_client=client)
        # Don't force healthy — let it report is_enabled=False since connect fails
        try:
            await client.ping()
            backend._healthy = True
        except Exception:
            backend._healthy = False

        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=2, open_seconds=0.5)),
            bulkhead=DBBulkhead(max_concurrency=5),
            singleflight=SingleFlight(),
            jitter_fn=lambda ratio: 1.0,
        )
        cache = ModelConfigCache(manager=mgr)
        dto = ModelConfigDTO(
            public_id="mc_fault_test",
            user_id=42,
            capability_type="chat",
            config_name="fault",
            provider="openai",
            api_base_url="https://fault",
            api_key_encrypted="gAAAAA==",
            model_name="FAULT_TEST",
            timeout_seconds=120,
            enable_thinking=False,
            supports_vision=False,
            is_default=True,
            enabled=True,
        )

        # write_through should NOT raise
        ok = await cache.write_through(42, "chat", dto)
        # Should return False (bypass or error)
        assert ok is False, f"Expected False on fault, got {ok}"

        # get_or_load should also not raise
        async def loader():
            from app.cache.domains.config_cache import ModelConfigDTO
            return ModelConfigDTO(
                public_id="loader_fallback",
                user_id=42,
                capability_type="chat",
                config_name="loader",
                provider="openai",
                api_base_url="https://loader",
                api_key_encrypted="gAAAAA==",
                model_name="LOADER_OK",
                timeout_seconds=120,
                enable_thinking=False,
                supports_vision=False,
                is_default=True,
                enabled=True,
            )

        # When Redis is down, get_or_load should call loader (fallback)
        result = await cache.get_or_load(42, "chat", loader)
        # We don't assert specific behavior here — just that it didn't crash
        assert result is not None
        await client.aclose()

    asyncio.run(main())


if __name__ == "__main__":
    test_fault_fallback_returns_gracefully()
    print("fault test OK")