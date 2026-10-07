"""P1-6 真实多进程 Redis 测试(localhost:6380,非 fakeredis).

启动方式:
  # pytest 模式(默认 skip, 需要 RUN_REAL_REDIS_TESTS=1)
  RUN_REAL_REDIS_TESTS=1 CACHE_REDIS_ENABLED=true \\
    CACHE_REDIS_URL=redis://:xiaoliu123@49.235.42.163:6379/8 \\
    python -m pytest tests/perf/test_real_multiworker_redis.py -v -s

  # 独立脚本模式
  CACHE_REDIS_ENABLED=true CACHE_REDIS_URL=redis://:xiaoliu123@49.235.42.163:6379/8 \\
    python tests/perf/test_real_multiworker_redis.py --mode=cross_worker

设计要点:
  * 启动 2 个 subprocess(Popen + 独立 Python 进程)
  * 各自直接连 redis (独立连接池,验证跨进程可见性)
  * Writer 写 → Reader 读 → 验证读到的 model_name 是新值,loader 未被调用
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
USER_ID = 99001
CAPABILITY = "chat"
MODEL_NAME = "real-multiworker-model"


def _build_backend():
    """Build a real CacheBackend connected to the configured Redis."""
    import redis.asyncio as aioredis
    from app.cache.backend import CacheBackend

    client = aioredis.from_url(REDIS_URL, decode_responses=True)
    backend = CacheBackend(redis_client=client)
    backend._healthy = True
    return backend, client


def _worker_writer(payload_str: str, ready_event_path: str) -> None:
    """Worker A: 写一个新配置到 cache."""
    import asyncio

    async def main():
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
        backend, _ = _build_backend()
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=1)),
            bulkhead=DBBulkhead(max_concurrency=5),
            singleflight=SingleFlight(),
            jitter_fn=lambda ratio: 1.0,
        )
        cache = ModelConfigCache(manager=mgr)
        dto = ModelConfigDTO(**json.loads(payload_str))
        ok = await cache.write_through(USER_ID, CAPABILITY, dto)
        Path(ready_event_path).write_text(
            json.dumps({"ok": ok, "ts": time.time()})
        )

    asyncio.run(main())


def _worker_reader(ready_event_path: str, result_event_path: str) -> None:
    """Worker B: 等 writer 完成, 然后读 cache. 验证读到的 model_name 是新值."""
    import asyncio

    async def main():
        from app.cache.bulkhead import DBBulkhead
        from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
        from app.cache.domains.config_cache import (
            ModelConfigCache,
            ModelConfigDTO,
        )
        from app.cache.manager import CacheManager
        from app.cache.singleflight import SingleFlight
        from app.cache.metrics import cache_metrics

        for _ in range(50):
            if Path(ready_event_path).exists():
                break
            await asyncio.sleep(0.05)

        cache_metrics.reset()
        backend, _ = _build_backend()
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=1)),
            bulkhead=DBBulkhead(max_concurrency=5),
            singleflight=SingleFlight(),
            jitter_fn=lambda ratio: 1.0,
        )
        cache = ModelConfigCache(manager=mgr)

        loader_called_holder = {"called": False}

        async def stale_loader():
            loader_called_holder["called"] = True
            return ModelConfigDTO(
                public_id="mc_stale",
                user_id=USER_ID,
                capability_type=CAPABILITY,
                config_name="stale",
                provider="openai",
                api_base_url="https://stale",
                api_key_encrypted="gAAAAA==",
                model_name="STALE",
                timeout_seconds=120,
                enable_thinking=False,
                supports_vision=False,
                is_default=True,
                enabled=True,
            )

        dto = await cache.get_or_load(USER_ID, CAPABILITY, stale_loader)
        Path(result_event_path).write_text(
            json.dumps({
                "loader_called": loader_called_holder["called"],
                "model_name": dto.model_name if dto else None,
                "ts": time.time(),
            })
        )

    asyncio.run(main())


def _make_model_dto() -> str:
    return json.dumps({
        "public_id": "mc_real_test",
        "user_id": USER_ID,
        "capability_type": CAPABILITY,
        "config_name": "real-multiworker",
        "provider": "openai-compatible",
        "api_base_url": "https://real.example.com/v1",
        "api_key_encrypted": "gAAAAAfakeFernet==",
        "model_name": MODEL_NAME,
        "timeout_seconds": 120,
        "enable_thinking": False,
        "supports_vision": False,
        "is_default": True,
        "enabled": True,
    })


def _cross_worker_impl(tmp_dir: str) -> dict:
    """Worker A 写, Worker B 立即读到. 0 DB loader 调用."""
    ready_event = os.path.join(tmp_dir, "writer_ready.json")
    result_event = os.path.join(tmp_dir, "reader_result.json")

    if Path(ready_event).exists():
        Path(ready_event).unlink()
    if Path(result_event).exists():
        Path(result_event).unlink()

    writer = subprocess.Popen([
        sys.executable, "-c",
        f"import sys; sys.path.insert(0, r'{BACKEND_ROOT}'); "
        f"from tests.perf.test_real_multiworker_redis import _worker_writer, _make_model_dto; "
        f"import os; "
        f"_worker_writer(_make_model_dto(), r'{ready_event}')"
    ])
    writer.wait(timeout=30)

    reader = subprocess.Popen([
        sys.executable, "-c",
        f"import sys; sys.path.insert(0, r'{BACKEND_ROOT}'); "
        f"from tests.perf.test_real_multiworker_redis import _worker_reader; "
        f"_worker_reader(r'{ready_event}', r'{result_event}')"
    ])
    reader.wait(timeout=30)

    assert Path(ready_event).exists(), "Writer didn't write ready event"
    assert Path(result_event).exists(), "Reader didn't write result event"

    writer_data = json.loads(Path(ready_event).read_text())
    reader_data = json.loads(Path(result_event).read_text())

    return {
        "writer_ok": writer_data["ok"],
        "reader_model_name": reader_data["model_name"],
        "loader_called": reader_data["loader_called"],
    }


def test_cross_worker_immediate_consistency() -> None:
    """Real cross-worker test.

    Requires real Redis at CACHE_REDIS_TEST_URL (default shared dev redis db=8).
    Skip by default — set ``RUN_REAL_REDIS_TESTS=1`` to enable.
    """
    if not os.environ.get("RUN_REAL_REDIS_TESTS"):
        import pytest
        pytest.skip(
            "Real Redis subprocess test — set RUN_REAL_REDIS_TESTS=1 to enable"
        )

    tmp_dir = BACKEND_ROOT / ".zcode" / "tmp_real_redis_test"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    result = _cross_worker_impl(str(tmp_dir))

    assert result["writer_ok"] is True, f"Writer failed: {result}"
    assert result["reader_model_name"] == MODEL_NAME, (
        f"Reader got {result['reader_model_name']}, expected {MODEL_NAME}"
    )
    assert result["loader_called"] is False, (
        "Loader was called — cross-worker write did NOT propagate to cache"
    )


def main_runner():
    """Entry point for the standalone test script."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="cross_worker",
                        choices=["cross_worker"])
    args = parser.parse_args()

    tmp_dir = BACKEND_ROOT / ".zcode" / "tmp_real_redis_test"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "cross_worker":
        result = _cross_worker_impl(str(tmp_dir))
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main_runner()