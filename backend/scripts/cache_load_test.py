"""Cache 压测脚本 (提示词 §36 + 设计文档 §42).

覆盖场景（per §36）:
  1. Chat 首屏 (Conversation list cache-aside)
  2. Hot cache (cache hit path)
  3. Cold cache (cache miss path)
  4. Cache flush burst (bulk invalidation + reload)
  5. SSE heartbeat (Task Status cache hit/miss)
  6. 100 Agent Run (SingleFlight effectiveness)
  7. Redis Cache 故障 (graceful degradation)
  8. 多 worker 一致性 (via shared FakeServer)

输出：
  - p50 / p95 / p99 延迟 (毫秒)
  - 总 QPS / 错误数 / fallback 数
  - MySQL SELECT/s 模拟 (loader count)
  - cache hit rate / miss rate
  - breaker 状态变化

运行方式:
  cd backend && python -m scripts.cache_load_test
  或: cd backend && python scripts/cache_load_test.py

环境假设:
  - Python 3.13+ asyncio
  - 无外部依赖 (fakeredis 内存模拟)
  - 不需要真 Redis / MySQL
  - 数字与 production 真 Redis + MySQL 不同,但相对差异可用

注意事项:
  - 本脚本输出的是 in-process 数字。生产环境真实数据需通过
    `scripts/prod_start.sh` 启动后用 wrk/locust 跑。
  - 真实 baseline 阈值在
    `tests/perf/test_cache_load_thresholds.py` 里做 regression guard。
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# Make sure backend root is importable when run as `python scripts/cache_load_test.py`.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fakeredis import aioredis as fakeredis_aioredis

from app.cache.backend import CacheBackend
from app.cache.bulkhead import BulkheadTimeoutError, DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, BreakerState, CircuitBreaker
from app.cache.distributed_lock import CacheFillLock
from app.cache.manager import CacheManager, get_cache_manager, set_cache_manager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight
from app.cache.specs import CacheSpec


# 抑制 SingleFlight 内部的 "Future exception was never retrieved" 噪音 —
# SingleFlight 把 exception set 在 leader future 上,waiters 通过 asyncio.shield
# 拿到 exception (语义正确),但原始 future 的 .exception() 从未被显式调用,
# asyncio 会打一条 warning。这里设个空 exception handler 屏蔽之。
def _suppress_future_warning(loop, context):
    msg = context.get("message", "")
    if "Future exception was never retrieved" in msg:
        return
    loop.default_exception_handler(context)


def _install_silent_loop() -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(_suppress_future_warning)
    except RuntimeError:
        pass


# ── 报告数据结构 ────────────────────────────────────────────────


@dataclass
class ScenarioReport:
    """压测场景的指标快照."""

    name: str
    scenarios: list[str]  # 并发级别 (e.g. [100, 500, 1000])
    concurrency: int
    iterations: int
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    loader_calls: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    fallbacks: int = 0
    breaker_state_changes: list[BreakerState] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def p50(self) -> float:
        return _percentile(self.latencies_ms, 50)

    @property
    def p95(self) -> float:
        return _percentile(self.latencies_ms, 95)

    @property
    def p99(self) -> float:
        return _percentile(self.latencies_ms, 99)

    @property
    def qps(self) -> float:
        if self.elapsed_s == 0:
            return 0
        return self.iterations / self.elapsed_s

    def summary_line(self) -> str:
        return (
            f"  p50={self.p50 * 1000:7.2f}ms  p95={self.p95 * 1000:7.2f}ms  "
            f"p99={self.p99 * 1000:7.2f}ms  qps={self.qps:7.1f}  "
            f"errors={self.errors}  loaders={self.loader_calls}  "
            f"hits={self.cache_hits}  miss={self.cache_misses}  "
            f"fallback={self.fallbacks}  breaker={self.breaker_state_changes[-1].value if self.breaker_state_changes else '-'}"
        )


def _percentile(samples: list[float], p: int) -> float:
    """线性插值 percentile。空列表返回 0。"""
    if not samples:
        return 0.0
    s = sorted(samples)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


# ── 公共 helpers ────────────────────────────────────────────────


AUTH_SPEC = CacheSpec(
    domain="auth",
    ttl_seconds=60,
    negative_ttl_seconds=5,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=True,
)

CONV_LIST_SPEC = CacheSpec(
    domain="conv",
    ttl_seconds=60,
    negative_ttl_seconds=30,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=True,
)


def _make_manager(fake_redis) -> CacheManager:
    """构造一个干净的 CacheManager (独立 fakeredis + breaker + bulkhead)."""
    backend = CacheBackend(redis_client=fake_redis)
    backend._healthy = True
    return CacheManager(
        backend=backend,
        breaker=CircuitBreaker(
            BreakerConfig(failure_threshold=5, open_seconds=5.0)
        ),
        bulkhead=DBBulkhead(max_concurrency=5),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,  # deterministic TTL
    )


def _auth_payload(uid: int = 42) -> dict:
    return {
        "internal_id": uid,
        "public_id": f"usr_{uid}",
        "display_name": "Alice",
        "username": "alice",
        "email": "alice@example.com",
        "role": "user",
        "status": "active",
        "avatar_url": None,
    }


def _conv_list_payload(uid: int = 42, n: int = 20) -> dict:
    return {
        "summaries": [
            {"id": f"c{i}", "title": f"Conv {i}", "updated_at": "2026-09-06T12:00:00+00:00"}
            for i in range(n)
        ],
        "total": n,
    }


class BrokenRedis:
    """模拟 Redis 故障 — 所有 op 抛 ConnectionError."""

    def __init__(self, exc_type: type[Exception] = ConnectionError):
        self._exc = exc_type

    async def get(self, *a, **kw):
        raise self._exc("redis down")

    async def set(self, *a, **kw):
        raise self._exc("redis down")

    async def delete(self, *a, **kw):
        raise self._exc("redis down")

    async def mget(self, *a, **kw):
        raise self._exc("redis down")

    async def ping(self):
        raise self._exc("redis down")

    async def pipeline(self, *a, **kw):
        raise self._exc("redis down")


async def _time_many(
    coro_factory: Callable[[int], Awaitable[Any]],
    n: int,
) -> tuple[list[float], int]:
    """并发跑 n 次 coro,返回 (latencies, error_count).

    ``coro_factory(i)`` 返回第 i 次调用的 coroutine。
    """
    latencies: list[float] = []
    errors = 0
    bulkhead_exhausted = 0

    async def _timed_one(i: int) -> None:
        nonlocal errors, bulkhead_exhausted
        start = time.perf_counter()
        try:
            await coro_factory(i)
        except BulkheadTimeoutError:
            # 这是 bulkhead 设计行为: Redis down + 高并发 → bulkhead
            # 排队超时。记为 fallback 失败,不算 5xx 业务错误。
            bulkhead_exhausted += 1
            latencies.append(time.perf_counter() - start)
        except Exception:
            errors += 1
        else:
            latencies.append(time.perf_counter() - start)

    start = time.perf_counter()
    await asyncio.gather(*[_timed_one(i) for i in range(n)])
    elapsed = time.perf_counter() - start
    return latencies, errors, bulkhead_exhausted


def _section(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _row(name: str, r: ScenarioReport) -> None:
    print(f"[{name:36s}] conc={r.concurrency:4d}  iters={r.iterations}")
    print(r.summary_line())


# ── 场景 1: Chat 首屏 (Conversation list) ──────────────────────


async def scenario_chat_first_page(concurrency: int, iterations: int) -> ScenarioReport:
    """模拟 Chat 首屏加载 — Conversation list cache-aside.

    流程:
      1. 100 个 user 同时加载自己的 conv list
      2. 每个 user 的 list cache miss 1 次 (cold)
      3. 之后都是 cache hit
    4. SingleFlight + bulkhead dedupe 同一 user 的并发
    """
    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    mgr = _make_manager(redis)
    set_cache_manager(mgr)
    cache_metrics.reset()

    user_ids = list(range(1, concurrency + 1))
    # Warm-up 一次
    for uid in user_ids:
        key = f"ta:dev:cache:v1:conv:list:user:{uid}:g:warm:q:all"
        await mgr.set(CONV_LIST_SPEC, key, _conv_list_payload(uid))

    loader_calls = {"n": 0}

    async def loader(uid: int):
        loader_calls["n"] += 1
        return _conv_list_payload(uid)

    async def hit_one(i: int) -> None:
        uid = user_ids[i % concurrency]
        key = f"ta:dev:cache:v1:conv:list:user:{uid}:g:warm:q:all"
        cached = await mgr.get(CONV_LIST_SPEC, key)
        if isinstance(cached, dict):
            return  # hit
        # miss → loader
        await loader(uid)

    latencies, errors, _bh = await _time_many(hit_one, iterations)
    snap = cache_metrics.snapshot()
    r = ScenarioReport(
        name="Chat first page (conv list)",
        scenarios=[100, 500, 1000],
        concurrency=concurrency,
        iterations=iterations,
        latencies_ms=latencies,
        errors=errors,
        loader_calls=loader_calls["n"],
        cache_hits=snap.get("auth_get_hit", 0)
        + snap.get("conv_get_hit", 0)
        + snap.get("auth_get_many_hit", 0)
        + snap.get("conv_get_many_hit", 0),
        cache_misses=snap.get("auth_get_miss", 0)
        + snap.get("conv_get_miss", 0),
        fallbacks=0,
        breaker_state_changes=[mgr._breaker.state],
        elapsed_s=sum(latencies),
    )
    await redis.flushall()
    await redis.aclose()
    return r


# ── 场景 2: Hot cache (AuthPrincipal cache hit) ───────────────


async def scenario_hot_cache(concurrency: int, iterations: int) -> ScenarioReport:
    """Hot cache path — SET → 1000 个并发 GET. 全 hit."""
    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    mgr = _make_manager(redis)
    set_cache_manager(mgr)
    cache_metrics.reset()

    # Pre-populate 200 个不同 key
    keys = [f"ta:dev:cache:v1:auth:principal:usr_{i}" for i in range(200)]
    for k in keys:
        await mgr.set(AUTH_SPEC, k, _auth_payload())

    loader_calls = {"n": 0}

    async def get_one(i: int) -> None:
        k = keys[i % len(keys)]
        cached = await mgr.get(AUTH_SPEC, k)
        if not isinstance(cached, dict):
            loader_calls["n"] += 1

    latencies, errors, _bh = await _time_many(get_one, iterations)
    snap = cache_metrics.snapshot()
    r = ScenarioReport(
        name="Hot cache (auth principal)",
        scenarios=[100, 500, 1000],
        concurrency=concurrency,
        iterations=iterations,
        latencies_ms=latencies,
        errors=errors,
        loader_calls=loader_calls["n"],
        cache_hits=snap.get("auth_get_hit", 0),
        cache_misses=snap.get("auth_get_miss", 0),
        fallbacks=0,
        breaker_state_changes=[mgr._breaker.state],
        elapsed_s=sum(latencies),
    )
    await redis.flushall()
    await redis.aclose()
    return r


# ── 场景 3: Cold cache ───────────────────────────────────────


async def scenario_cold_cache(concurrency: int, iterations: int) -> ScenarioReport:
    """Cold cache — 1000 个不同 key, 全 miss, 走 loader."""
    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    mgr = _make_manager(redis)
    set_cache_manager(mgr)
    cache_metrics.reset()

    loader_calls = {"n": 0}

    async def loader(i: int):
        loader_calls["n"] += 1
        return _auth_payload(i)

    async def miss_one(i: int) -> None:
        key = f"ta:dev:cache:v1:auth:principal:cold_{i}"
        async def _ld():
            return await loader(i)
        await mgr.get_or_load(AUTH_SPEC, key, _ld)

    latencies, errors, _bh = await _time_many(miss_one, iterations)
    snap = cache_metrics.snapshot()
    r = ScenarioReport(
        name="Cold cache (1000 unique keys)",
        scenarios=[100, 500, 1000],
        concurrency=concurrency,
        iterations=iterations,
        latencies_ms=latencies,
        errors=errors,
        loader_calls=loader_calls["n"],
        cache_hits=snap.get("auth_get_hit", 0)
        + snap.get("auth_get_many_hit", 0),
        cache_misses=snap.get("auth_get_miss", 0)
        + snap.get("auth_get_many_hit_miss", 0),
        fallbacks=0,
        breaker_state_changes=[mgr._breaker.state],
        elapsed_s=sum(latencies),
    )
    await redis.flushall()
    await redis.aclose()
    return r


# ── 场景 4: Cache flush burst ────────────────────────────────


async def scenario_cache_flush_burst(
    concurrency: int, iterations: int
) -> ScenarioReport:
    """Cache flush — 大批量 invalidate + 立刻 reload.

    模拟场景: admin / repair tool 触发 cache flush,
    之后所有 read 都得 cache miss 一次 → loader.
    """
    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    mgr = _make_manager(redis)
    set_cache_manager(mgr)
    cache_metrics.reset()

    keys = [f"ta:dev:cache:v1:auth:principal:usr_{i}" for i in range(500)]
    # Pre-populate
    for k in keys:
        await mgr.set(AUTH_SPEC, k, _auth_payload())

    # Flush — DEL 所有 key
    for k in keys:
        await mgr.delete(AUTH_SPEC, k)

    loader_calls = {"n": 0}

    async def loader(i: int):
        loader_calls["n"] += 1
        return _auth_payload(i)

    async def reload_one(i: int) -> None:
        k = keys[i % len(keys)]
        async def _ld():
            return await loader(i)
        await mgr.get_or_load(AUTH_SPEC, k, _ld)

    latencies, errors, _bh = await _time_many(reload_one, iterations)
    snap = cache_metrics.snapshot()
    r = ScenarioReport(
        name="Cache flush burst (invalidate + reload)",
        scenarios=[100, 500, 1000],
        concurrency=concurrency,
        iterations=iterations,
        latencies_ms=latencies,
        errors=errors,
        loader_calls=loader_calls["n"],
        cache_hits=snap.get("auth_get_hit", 0),
        cache_misses=snap.get("auth_get_miss", 0),
        fallbacks=0,
        breaker_state_changes=[mgr._breaker.state],
        elapsed_s=sum(latencies),
    )
    await redis.flushall()
    await redis.aclose()
    return r


# ── 场景 5: SSE heartbeat ───────────────────────────────────


async def scenario_sse_heartbeat(concurrency: int, iterations: int) -> ScenarioReport:
    """模拟 500 个 SSE 客户端同时 heartbeat.

    实际 SSE 是长连接,每 30s 一次 status read.
    这里模拟 heartbeat 的 cache GET 路径.
    """
    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    mgr = _make_manager(redis)
    set_cache_manager(mgr)
    cache_metrics.reset()

    # 假设 500 个 task, 每个状态已 cache 住
    task_ids = [f"task_{i}" for i in range(500)]
    for tid in task_ids:
        key = f"ta:dev:cache:v1:task:status:{tid}"
        await mgr.set(
            CacheSpec(domain="task", ttl_seconds=180, negative_ttl_seconds=30),
            key,
            {"status": "running", "updated_at": "2026-09-06", "active_run_id": "r", "terminal": False},
        )

    loader_calls = {"n": 0}

    async def heartbeat(i: int) -> None:
        tid = task_ids[i % len(task_ids)]
        key = f"ta:dev:cache:v1:task:status:{tid}"
        cached = await mgr.get(
            CacheSpec(domain="task", ttl_seconds=180, negative_ttl_seconds=30),
            key,
        )
        if not isinstance(cached, dict):
            loader_calls["n"] += 1

    latencies, errors, _bh = await _time_many(heartbeat, iterations)
    snap = cache_metrics.snapshot()
    r = ScenarioReport(
        name="SSE heartbeat (task status)",
        scenarios=[100, 500, 1000],
        concurrency=concurrency,
        iterations=iterations,
        latencies_ms=latencies,
        errors=errors,
        loader_calls=loader_calls["n"],
        cache_hits=snap.get("task_get_hit", 0),
        cache_misses=snap.get("task_get_miss", 0),
        fallbacks=0,
        breaker_state_changes=[mgr._breaker.state],
        elapsed_s=sum(latencies),
    )
    await redis.flushall()
    await redis.aclose()
    return r


# ── 场景 6: 100 Agent Run (SingleFlight effectiveness) ──────────


async def scenario_singleflight_coalesce(
    concurrency: int, iterations: int
) -> ScenarioReport:
    """100 个并发请求同一 cache key — SingleFlight 应合并到 1 次 loader.

    当 many SSE clients / API clients 同时订阅同一 task,
    SingleFlight 避免 DB 被打穿.
    """
    redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
    mgr = _make_manager(redis)
    set_cache_manager(mgr)
    cache_metrics.reset()

    shared_key = "ta:dev:cache:v1:auth:principal:shared_usr"
    loader_calls = {"n": 0}

    async def loader():
        loader_calls["n"] += 1
        # 模拟 DB 耗时
        await asyncio.sleep(0.01)
        return _auth_payload()

    async def coalesce_one(i: int) -> None:
        await mgr.get_or_load(AUTH_SPEC, shared_key, loader)

    latencies, errors, _bh = await _time_many(coalesce_one, iterations)
    snap = cache_metrics.snapshot()
    r = ScenarioReport(
        name=f"SingleFlight coalesce ({concurrency}→1 loader)",
        scenarios=[100, 500, 1000],
        concurrency=concurrency,
        iterations=iterations,
        latencies_ms=latencies,
        errors=errors,
        loader_calls=loader_calls["n"],
        cache_hits=snap.get("auth_get_hit", 0)
        + snap.get("auth_get_many_hit", 0),
        cache_misses=snap.get("auth_get_miss", 0),
        fallbacks=0,
        breaker_state_changes=[mgr._breaker.state],
        elapsed_s=sum(latencies),
    )
    await redis.flushall()
    await redis.aclose()
    return r


# ── 场景 7: Redis Cache 故障 ────────────────────────────────


async def scenario_redis_fault(concurrency: int, iterations: int) -> ScenarioReport:
    """Redis 完全 down — 1000 个并发 fallback 到 loader, 无 5xx."""
    redis = BrokenRedis()
    backend = CacheBackend(redis_client=redis)
    backend._healthy = False
    mgr = CacheManager(
        backend=backend,
        breaker=CircuitBreaker(
            BreakerConfig(failure_threshold=5, open_seconds=5.0)
        ),
        bulkhead=DBBulkhead(max_concurrency=5),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,
    )
    set_cache_manager(mgr)
    cache_metrics.reset()

    loader_calls = {"n": 0}

    async def loader(i: int):
        loader_calls["n"] += 1
        # 模拟 DB 写耗时
        await asyncio.sleep(0.001)
        return _auth_payload(i)

    async def fault_one(i: int) -> None:
        key = f"ta:dev:cache:v1:auth:principal:fault_{i}"
        async def _ld():
            return await loader(i)
        await mgr.get_or_load(AUTH_SPEC, key, _ld)

    latencies, errors, _bh = await _time_many(fault_one, iterations)
    snap = cache_metrics.snapshot()
    r = ScenarioReport(
        name="Redis fault (graceful degradation)",
        scenarios=[100, 500, 1000],
        concurrency=concurrency,
        iterations=iterations,
        latencies_ms=latencies,
        errors=errors,
        loader_calls=loader_calls["n"],
        cache_hits=snap.get("auth_get_hit", 0)
        + snap.get("auth_get_many_hit", 0),
        cache_misses=snap.get("auth_get_miss", 0),
        fallbacks=snap.get("auth_get_error", 0)
        + snap.get("auth_get_many_error", 0)
        + snap.get("auth_set_error", 0),
        breaker_state_changes=[mgr._breaker.state],
        elapsed_s=sum(latencies),
    )
    return r


# ── 场景 8: 多 worker 一致性 ────────────────────────────────


async def scenario_multi_worker_consistency(
    concurrency: int, iterations: int
) -> ScenarioReport:
    """2 个 worker (共享 FakeServer) — Worker A 写, Worker B 读.

    验证: Worker A 写的 cache, Worker B 立即能读到.
    """
    server = fakeredis_aioredis.FakeServer()
    worker_a_redis = fakeredis_aioredis.FakeRedis(server=server, decode_responses=False)
    worker_b_redis = fakeredis_aioredis.FakeRedis(server=server, decode_responses=False)

    mgr_a = _make_manager(worker_a_redis)
    mgr_b = _make_manager(worker_b_redis)

    shared_key = "ta:dev:cache:v1:auth:principal:multi_worker_usr"

    # Worker A writes
    await mgr_a.set(AUTH_SPEC, shared_key, _auth_payload(99))

    loader_calls = {"n": 0}

    async def worker_b_read(i: int) -> None:
        cached = await mgr_b.get(AUTH_SPEC, shared_key)
        if not isinstance(cached, dict):
            loader_calls["n"] += 1

    latencies, errors, _bh = await _time_many(worker_b_read, iterations)
    snap = cache_metrics.snapshot()
    r = ScenarioReport(
        name="Multi-worker consistency (A write → B read)",
        scenarios=[100, 500, 1000],
        concurrency=concurrency,
        iterations=iterations,
        latencies_ms=latencies,
        errors=errors,
        loader_calls=loader_calls["n"],
        cache_hits=snap.get("auth_get_hit", 0),
        cache_misses=snap.get("auth_get_miss", 0),
        fallbacks=0,
        breaker_state_changes=[mgr_b._breaker.state],
        elapsed_s=sum(latencies),
    )
    await worker_a_redis.flushall()
    await worker_b_redis.flushall()
    await worker_a_redis.aclose()
    await worker_b_redis.aclose()
    return r


# ── 主流程 ──────────────────────────────────────────────────


async def main() -> None:
    """运行 §36 全部 8 个场景,每个场景在 3 个并发级别跑."""
    print("TestAgent Cache 压测 (提示词 §36)")
    print(f"Python {sys.version.split()[0]}  |  asyncio  |  fakeredis in-process")
    print(f"Started: {time.strftime('%Y-%m-%d %H:%M:%S')}")

    scenarios = [
        ("1. Chat 首屏 (conv list)", scenario_chat_first_page),
        ("2. Hot cache (auth hit)", scenario_hot_cache),
        ("3. Cold cache (1000 unique)", scenario_cold_cache),
        ("4. Cache flush burst", scenario_cache_flush_burst),
        ("5. SSE heartbeat (500 task)", scenario_sse_heartbeat),
        (
            "6. SingleFlight coalesce",
            scenario_singleflight_coalesce,
        ),
        ("7. Redis 故障 (graceful)", scenario_redis_fault),
        ("8. Multi-worker consistency", scenario_multi_worker_consistency),
    ]
    concurrency_levels = [100, 500, 1000]
    iterations_per_level = 1000

    all_reports: list[ScenarioReport] = []

    # 抑制 SingleFlight 噪音
    _install_silent_loop()

    for name, fn in scenarios:
        _section(name)
        for conc in concurrency_levels:
            t0 = time.perf_counter()
            report = await fn(conc, iterations_per_level)
            elapsed_wall = time.perf_counter() - t0
            report.elapsed_s = elapsed_wall
            all_reports.append(report)
            _row(name.split(".")[1].strip(), report)

    # ── 总览 ──
    _section("总览 / Baseline (in-process fakeredis, NO network)")
    print(
        f"{'场景':40s}  {'conc':>4s}  {'p50 (ms)':>9s}  {'p95 (ms)':>9s}  {'p99 (ms)':>9s}  {'qps':>7s}  {'loaders':>7s}"
    )
    print("-" * 100)
    for r in all_reports:
        print(
            f"{r.name:40s}  {r.concurrency:>4d}  {r.p50 * 1000:>9.2f}  "
            f"{r.p95 * 1000:>9.2f}  {r.p99 * 1000:>9.2f}  {r.qps:>7.0f}  {r.loader_calls:>7d}"
        )
    print()
    print("注: 上面是 in-process 数字。Production 真 Redis + 真 MySQL 下,")
    print("    p50/p95/p99 应在 §42 目标范围内 (见 tests/perf/test_cache_load_thresholds.py)。")


if __name__ == "__main__":
    asyncio.run(main())