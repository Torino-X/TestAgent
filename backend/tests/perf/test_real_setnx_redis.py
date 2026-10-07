"""P1-6 真实 SET NX 跨进程 Fill Lock 测试(localhost:6380 或共享 redis).

启动方式:
  cd backend && CACHE_REDIS_ENABLED=true CACHE_REDIS_URL=redis://:xiaoliu123@49.235.42.163:6379/8 \\
    python tests/perf/test_real_setnx_redis.py --mode=set_nx

测试:
  启动 4 个独立进程, 全部同时调 ``get_or_load`` 同一个 cache key.
  理想: 只有 1 个进程跑 loader (因为 SET NX 锁); 其它 3 个进程在锁外等待.
  实际(无 SET NX): 4 个进程都跑 loader.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND_ROOT))

REDIS_URL = os.environ.get("CACHE_REDIS_TEST_URL", "redis://:xiaoliu123@49.235.42.163:6379/8")
USER_ID = 99002
CAPABILITY = "chat"
N_WORKERS = 4


async def _single_worker(idx: int, barrier_path: str, output_path: str) -> None:
    """One process — waits on barrier, then races for the same cache key."""
    from app.cache.bulkhead import DBBulkhead
    from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
    from app.cache.domains.config_cache import (
        ModelConfigCache,
        ModelConfigDTO,
    )
    from app.cache.manager import CacheManager
    from app.cache.singleflight import SingleFlight
    from app.cache.metrics import cache_metrics
    import redis.asyncio as aioredis

    cache_metrics.reset()
    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    from app.cache.backend import CacheBackend
    backend = CacheBackend(redis_client=client)
    backend._healthy = True
    mgr = CacheManager(
        backend=backend,
        breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=1)),
        bulkhead=DBBulkhead(max_concurrency=5),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,
    )
    cache = ModelConfigCache(manager=mgr)

    # Make sure key starts empty
    await client.delete(
        f"ta:{os.environ.get('CACHE_KEY_PREFIX', 'ta')}:cache:v1:cfg:model:user:{USER_ID}:cap:{CAPABILITY}"
    )

    # Wait at the barrier
    while not Path(barrier_path).exists():
        await asyncio.sleep(0.01)

    started_at = time.time()
    loader_calls = {"count": 0}

    async def loader():
        loader_calls["count"] += 1
        # Sleep to simulate slow loader; gives other workers a chance to SET NX fail
        await asyncio.sleep(0.5)
        return ModelConfigDTO(
            public_id=f"mc_worker_{idx}",
            user_id=USER_ID,
            capability_type=CAPABILITY,
            config_name=f"worker-{idx}",
            provider="openai",
            api_base_url=f"https://worker-{idx}",
            api_key_encrypted="gAAAAA==",
            model_name=f"FROM_WORKER_{idx}",
            timeout_seconds=120,
            enable_thinking=False,
            supports_vision=False,
            is_default=True,
            enabled=True,
        )

    dto = await cache.get_or_load(USER_ID, CAPABILITY, loader)
    ended_at = time.time()
    Path(output_path).write_text(json.dumps({
        "idx": idx,
        "loader_called": loader_calls["count"],
        "model_name": dto.model_name if dto else None,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": (ended_at - started_at) * 1000,
    }))
    await client.aclose()


def _subprocess_runner(idx: int, barrier: str, output: str) -> None:
    asyncio.run(_single_worker(idx, barrier, output))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    args = parser.parse_args()

    tmp_dir = BACKEND_ROOT / ".zcode" / "tmp_real_setnx_test"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    barrier = tmp_dir / "barrier.json"
    barrier.unlink(missing_ok=True)

    outputs = []
    processes = []
    for i in range(N_WORKERS):
        output = tmp_dir / f"worker_{i}_result.json"
        output.unlink(missing_ok=True)
        outputs.append(output)
        p = subprocess.Popen([
            sys.executable, "-c",
            f"import sys; sys.path.insert(0, r'{BACKEND_ROOT}'); "
            f"from tests.perf.test_real_setnx_redis import _subprocess_runner; "
            f"_subprocess_runner({i}, r'{barrier}', r'{output}')"
        ])
        processes.append(p)
        # Give each process time to import and reach the barrier
        time.sleep(0.5)

    # Wait for all to reach the barrier
    time.sleep(1)
    # Release barrier
    barrier.write_text(json.dumps({"ts": time.time()}))

    # Wait for all to complete
    for p in processes:
        p.wait(timeout=30)

    # Read results
    results = []
    for i, output in enumerate(outputs):
        if output.exists():
            data = json.loads(output.read_text())
            results.append(data)
        else:
            results.append({"idx": i, "loader_called": -1, "model_name": None})

    print(json.dumps({
        "n_workers": N_WORKERS,
        "results": results,
        "loader_called_count": sum(r["loader_called"] for r in results),
        "unique_models_seen": len(set(r["model_name"] for r in results if r["model_name"])),
    }, indent=2))


if __name__ == "__main__":
    main()