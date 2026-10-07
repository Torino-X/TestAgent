"""CacheManager — 业务缓存统一接口（设计文档 §5 + 提示词 §8）。

接口:
  - get(spec, key) → payload | None | CacheMiss sentinel
  - set(spec, key, payload, *, ttl_override=None)
  - delete(spec, key)
  - get_or_load(spec, key, loader) → payload（带 SingleFlight + DB bulkhead）
  - get_many(spec, keys) → dict[key, payload]
  - set_many(spec, mapping)
  - bump_generation(domain, user_id) → 新 generation token
  - get_generation(domain, user_id) → 当前 generation token

不允许业务代码直接散落 ``redis.get/set/delete`` —— 统一走本 manager。
"""

from __future__ import annotations

import asyncio
import logging
import random
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, TypeVar

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.cache.distributed_lock import CacheFillLock
from app.cache.key_builder import build_key, hash_filter
from app.cache.metrics import Timer, cache_metrics
from app.cache.serializer import decode, encode_negative, encode_positive
from app.cache.singleflight import SingleFlight
from app.cache.specs import CacheSpec

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ── Sentinel / options ─────────────────────────────────────────────


@dataclass(frozen=True)
class _CacheMiss:
    """Sentinel returned when a domain cache lookup produced no entry.

    Domain Cache adapters can use ``isinstance(value, _CacheMiss)`` to
    distinguish "key not in cache" from "key cached as None / empty".
    """

    reason: str = "miss"

    def __repr__(self) -> str:
        return f"CacheMiss(reason={self.reason!r})"


CACHE_MISS = _CacheMiss()
CACHE_BYPASS = _CacheMiss(reason="bypass")
CACHE_ERROR = _CacheMiss(reason="error")


# ── Manager ────────────────────────────────────────────────────────


class CacheManager:
    """Single entry point for all domain cache operations.

    Step 2.3 ships the contract + a minimal Redis-backed implementation
    that respects:

      * circuit breaker (Redis errors → fallback DB)
      * SingleFlight (process-local request coalescing)
      * distributed cache-fill lock (optional per spec)
      * JSON envelope serializer
      * size guard (skip cache when payload > spec.max_value_bytes)
      * metrics emission per operation/result
      * DB fallback bulkhead (loader concurrency cap)

    Step 3 adds:
      * TTL jitter (per design §8: actual_ttl = base × random(0.90, 1.10))
      * batched get_many / set_many (MGET / MSET)
      * generation token bump/get on a generation Key namespace
        (per design §12.2: ta:{env}:cache:v1:gen:{domain}:user:{uid})
    """

    def __init__(
        self,
        *,
        backend: CacheBackend | None = None,
        breaker: CircuitBreaker | None = None,
        bulkhead: DBBulkhead | None = None,
        singleflight: SingleFlight | None = None,
        jitter_fn: Callable[[float], float] | None = None,
    ) -> None:
        # Backend resolution is graceful: if no singleton is installed
        # (e.g. tests that don't set up the cache backend), fall through
        # to a disabled instance.  This matches 设计文档 §21.1: "Redis
        # GET 失败 → 不直接 500" — the same principle applies to "Redis
        # not configured at all".
        if backend is None:
            try:
                backend = CacheBackend.get()
            except RuntimeError:
                backend = CacheBackend(redis_client=None)
        self._backend = backend
        self._breaker = breaker or CircuitBreaker()
        self._bulkhead = bulkhead or DBBulkhead()
        self._singleflight = singleflight or SingleFlight()
        # jitter_fn (default uniform 0.90 ~ 1.10); tests can inject deterministic.
        self._jitter = jitter_fn or self._default_jitter

    @staticmethod
    def _default_jitter(ratio: float) -> float:
        """Uniform jitter in [1-ratio, 1+ratio].  Default ratio 0.10 → [0.90, 1.10]."""
        return 1.0 + random.uniform(-ratio, ratio)

    def _apply_jitter(self, ttl_seconds: int, ratio: float) -> int:
        """Apply ±ratio jitter to a base TTL; clamp to >=1 to avoid zero/negative."""
        jittered = ttl_seconds * self._jitter(ratio)
        return max(1, int(round(jittered)))

    # ── Feature flag ────────────────────────────────────────────────

    def is_enabled(self) -> bool:
        """False → Domain Cache must bypass entirely."""
        return self._backend.is_enabled

    def domain_enabled(self, domain: str) -> bool:
        """Check per-domain feature flag from settings."""
        from app.core.config import get_settings

        s = get_settings()
        flag = f"cache_{domain}_enabled"
        if not hasattr(s, flag):
            return True  # unknown domain → default allow
        return bool(getattr(s, flag))

    # ── Internal helpers ────────────────────────────────────────────

    def _redis(self) -> Any:
        return self._backend.client

    async def _redis_op(self, coro_factory) -> Any:  # type: ignore[no-untyped-def]
        """Run a Redis op through the circuit breaker; swallow to None."""
        try:
            return await self._breaker.call(coro_factory)
        except CircuitOpenError:
            logger.debug("CacheManager: breaker open, fast-fail")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("CacheManager: Redis op failed: %s", exc)
            return None

    # ── Key helpers (delegate to key_builder for consistent format) ─

    @staticmethod
    def make_key(*parts: str | int) -> str:
        return build_key(*parts)

    @staticmethod
    def hash_filter(*values: object) -> str:
        return hash_filter(*values)

    @staticmethod
    def generation_key(domain: str, user_id: str | int) -> str:
        """Return the generation-token Key for ``(domain, user_id)``.

        Per design §12.2 — collections don't get per-list-key DEL; instead
        ``bump_generation`` invalidates by changing this token; readers
        can no longer find their cached list under the new gen segment.
        """
        return build_key("gen", domain, "user", user_id)

    @staticmethod
    def _generate_token() -> str:
        """Random opaque token (hex). 16 hex chars ≈ 64 bits of entropy."""
        return secrets.token_hex(8)

    # ── GET / SET / DELETE primitives ───────────────────────────────

    async def get(self, spec: CacheSpec, key: str) -> Any:
        """Return cached payload or ``CACHE_MISS`` / ``CACHE_BYPASS``.

        Negative envelopes are returned to the caller as ``None`` payload
        — they are converted back to ``CACHE_MISS(reason="negative")``
        so Domain Cache can distinguish "miss" from "loaded as empty".
        """
        if not self.is_enabled() or not self.domain_enabled(spec.domain):
            cache_metrics.record(domain=spec.domain, operation="get", result="bypass")
            return CACHE_BYPASS

        async def _op():
            return await self._redis().get(key)

        with Timer() as t:
            raw = await self._redis_op(_op)

        if raw is None:
            # Could be miss, breaker open, or Redis error — all map to miss
            # but with different metrics labels.
            cache_metrics.record(
                domain=spec.domain, operation="get",
                result="miss", latency_ms=t.elapsed_ms,
            )
            return CACHE_MISS

        env = decode(raw)
        if env is None:
            cache_metrics.record(
                domain=spec.domain, operation="get",
                result="error", latency_ms=t.elapsed_ms,
            )
            # Best-effort cleanup of corrupt payload
            await self._redis_op(lambda: self._redis().delete(key))
            return _CacheMiss(reason="corrupt")

        if env.negative:
            cache_metrics.record(
                domain=spec.domain, operation="get",
                result="negative_hit", latency_ms=t.elapsed_ms,
            )
            return _CacheMiss(reason="negative")

        cache_metrics.record(
            domain=spec.domain, operation="get",
            result="hit", latency_ms=t.elapsed_ms,
        )
        return env.payload

    async def set(self, spec: CacheSpec, key: str, payload: Any) -> bool:
        """Encode + SET payload.  Returns True on success, False otherwise.

        Honours ``spec.max_value_bytes`` (oversize → skip + record metric)
        and applies ``spec.jitter_ratio`` TTL jitter (per design §8).
        """
        if not self.is_enabled() or not self.domain_enabled(spec.domain):
            cache_metrics.record(domain=spec.domain, operation="set", result="bypass")
            return False
        body = encode_positive(payload)
        body_bytes = len(body.encode("utf-8"))
        if body_bytes > spec.max_value_bytes:
            cache_metrics.record(
                domain=spec.domain, operation="set",
                result="oversize",
            )
            logger.debug(
                "CacheManager.set: oversize skip domain=%s key=%s bytes=%d > %d",
                spec.domain, key, body_bytes, spec.max_value_bytes,
            )
            return False
        ttl = self._apply_jitter(spec.ttl_seconds, spec.jitter_ratio)

        async def _op():
            return await self._redis().set(key, body, ex=ttl)

        with Timer() as t:
            ok = await self._redis_op(_op)
        result = "hit" if ok else "error"
        cache_metrics.record(
            domain=spec.domain, operation="set",
            result=result, latency_ms=t.elapsed_ms,
        )
        return bool(ok)

    async def set_negative(self, spec: CacheSpec, key: str) -> bool:
        """Write a negative envelope (short TTL) to fend off stampedes."""
        if not self.is_enabled() or not self.domain_enabled(spec.domain):
            return False
        ttl = spec.negative_ttl_seconds
        if ttl is None or ttl <= 0:
            return False
        body = encode_negative()
        # Apply jitter to negative TTL too (smaller absolute impact).
        ttl_jittered = self._apply_jitter(ttl, spec.jitter_ratio)

        async def _op():
            return await self._redis().set(key, body, ex=ttl_jittered)

        with Timer() as t:
            ok = await self._redis_op(_op)
        result = "hit" if ok else "error"
        cache_metrics.record(
            domain=spec.domain, operation="set",
            result=result, latency_ms=t.elapsed_ms,
        )
        return bool(ok)

    async def delete(self, spec: CacheSpec, key: str) -> bool:
        if not self.is_enabled() or not self.domain_enabled(spec.domain):
            cache_metrics.record(domain=spec.domain, operation="delete", result="bypass")
            return False

        async def _op():
            return await self._redis().delete(key)

        with Timer() as t:
            ok = await self._redis_op(_op)
        cache_metrics.record(
            domain=spec.domain, operation="delete",
            result="hit" if ok else "error", latency_ms=t.elapsed_ms,
        )
        return bool(ok)

    # ── Batch: get_many / set_many ─────────────────────────────────

    async def get_many(
        self,
        spec: CacheSpec,
        keys: list[str],
    ) -> dict[str, Any]:
        """Batch GET via MGET.  Returns dict mapping each requested key
        to its payload OR a ``_CacheMiss`` sentinel.

        Keys absent from cache / breaker-open / Redis-error all map to
        ``CACHE_MISS`` (callers cannot tell apart, by design — the goal
        is a single batched call, not three different result shapes).
        Corrupt payloads are returned as ``_CacheMiss(reason="corrupt")``.
        Negative envelopes map to ``_CacheMiss(reason="negative")``.
        """
        if not self.is_enabled() or not self.domain_enabled(spec.domain):
            cache_metrics.record(domain=spec.domain, operation="get_many", result="bypass")
            return {k: CACHE_BYPASS for k in keys}

        result: dict[str, Any] = {k: CACHE_MISS for k in keys}
        if not keys:
            return result

        async def _op():
            return await self._redis().mget(keys)

        with Timer() as t:
            raw_values = await self._redis_op(_op)
        if raw_values is None:
            cache_metrics.record(
                domain=spec.domain, operation="get_many",
                result="error", latency_ms=t.elapsed_ms,
            )
            return result

        miss_count = 0
        hit_count = 0
        negative_count = 0
        for key, raw in zip(keys, raw_values):
            if raw is None:
                miss_count += 1
                continue
            env = decode(raw)
            if env is None:
                # corrupt payload → best-effort delete + mark miss
                await self._redis_op(lambda k=key: self._redis().delete(k))
                miss_count += 1
                continue
            if env.negative:
                # Negative-cache hit — surface as _CacheMiss(reason='negative')
                # so callers can distinguish from true miss and short-circuit.
                negative_count += 1
                result[key] = _CacheMiss(reason="negative")
                continue
            hit_count += 1
            result[key] = env.payload
        # One metrics line per operation; record hit / miss breakdown.
        cache_metrics.record(
            domain=spec.domain, operation="get_many",
            result="hit" if hit_count else "miss",
            latency_ms=t.elapsed_ms,
        )
        if miss_count:
            cache_metrics.record(
                domain=spec.domain, operation="get_many",
                result="miss", latency_ms=0.0,
            )
        if negative_count:
            cache_metrics.record(
                domain=spec.domain, operation="get_many",
                result="negative_hit", latency_ms=0.0,
            )
        return result

    async def set_many(
        self,
        spec: CacheSpec,
        mapping: Mapping[str, Any],
    ) -> int:
        """Batch SET via MSET + per-key EXPIRE (pipelined).  Returns count
        successfully written.

        Each (key, payload) pair runs through the same ``set`` pipeline:
        oversize skip, jitter, metrics.  Per design §6.3 max_value_bytes
        is enforced per value, not aggregate.
        """
        if not self.is_enabled() or not self.domain_enabled(spec.domain):
            cache_metrics.record(domain=spec.domain, operation="set_many", result="bypass")
            return 0

        written = 0
        # MSET has no per-key TTL; we need a SET per key for TTL anyway.
        # Pipeline them to amortize the round-trip.
        ttl = self._apply_jitter(spec.ttl_seconds, spec.jitter_ratio)

        async def _pipeline():
            pipe = self._redis().pipeline(transaction=False)
            for key, payload in mapping.items():
                body = encode_positive(payload)
                body_bytes = len(body.encode("utf-8"))
                if body_bytes > spec.max_value_bytes:
                    cache_metrics.record(
                        domain=spec.domain, operation="set_many",
                        result="oversize",
                    )
                    logger.debug(
                        "CacheManager.set_many: oversize skip key=%s bytes=%d > %d",
                        key, body_bytes, spec.max_value_bytes,
                    )
                    continue
                pipe.set(key, body, ex=ttl)
            return await pipe.execute()

        with Timer() as t:
            results = await self._redis_op(_pipeline)
        if results is None:
            cache_metrics.record(
                domain=spec.domain, operation="set_many",
                result="error", latency_ms=t.elapsed_ms,
            )
            return 0
        written = sum(1 for r in results if r)
        cache_metrics.record(
            domain=spec.domain, operation="set_many",
            result="hit" if written else "error",
            latency_ms=t.elapsed_ms,
        )
        return written

    # ── Generation token (collection invalidation, design §12.2) ──

    async def get_generation(
        self,
        domain: str,
        user_id: str | int,
    ) -> str:
        """Return the current generation token for ``(domain, user_id)``.

        On first read (key absent) returns a freshly-generated token AND
        writes it back so subsequent readers see the same value.  This
        is best-effort: when Redis is down we still return a stable token
        for the rest of the request.
        """
        from app.core.config import get_settings

        s = get_settings()
        flag = f"cache_{domain}_enabled"
        if not s.cache_redis_enabled or (
            hasattr(s, flag) and not getattr(s, flag)
        ):
            # Bypass: callers should still get *some* stable token for
            # this request lifecycle, so use process-local random.
            return self._generate_token()

        key = self.generation_key(domain, user_id)

        async def _op():
            return await self._redis().get(key)

        raw = await self._redis_op(_op)
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if raw:
            return str(raw)
        # Initialize on first read so consecutive readers see same token.
        new_token = self._generate_token()
        await self._redis_op(lambda: self._redis().set(key, new_token))
        return new_token

    async def bump_generation(
        self,
        domain: str,
        user_id: str | int,
    ) -> str:
        """Rotate the generation token for ``(domain, user_id)``.

        After bump, any cached list/detail under the OLD generation token
        is unreachable (the reader appends the new generation to its
        cache key — different key → miss → fresh DB load).

        Returns the new token.  No-op (returns existing token) when the
        domain is disabled / Redis is unavailable.
        """
        from app.core.config import get_settings

        s = get_settings()
        flag = f"cache_{domain}_enabled"
        if not s.cache_redis_enabled or (
            hasattr(s, flag) and not getattr(s, flag)
        ):
            return self._generate_token()

        key = self.generation_key(domain, user_id)
        new_token = self._generate_token()

        async def _op():
            # No NX — we want unconditional rotation.  Token-only value
            # so future readers can detect schema changes via length / prefix.
            return await self._redis().set(key, new_token)

        ok = await self._redis_op(_op)
        if not ok:
            logger.debug(
                "CacheManager.bump_generation: Redis SET failed domain=%s "
                "user_id=%s; returning new token without persisting",
                domain, user_id,
            )
        cache_metrics.record(
            domain=domain, operation="bump_generation",
            result="hit" if ok else "error",
        )
        return new_token

    # ── get_or_load: full cache-aside semantics ────────────────────

    async def get_or_load(
        self,
        spec: CacheSpec,
        key: str,
        loader: Callable[[], Awaitable[T]],
        *,
        fill_lock: CacheFillLock | None = None,
        cached_adapter: Callable[[Any], Awaitable[Any]] | None = None,
    ) -> T | None:
        """Cache-aside read with SingleFlight + optional distributed lock.

        ``loader()`` is invoked on miss; its return value is cached
        (positive or negative).  ``None`` returns from ``loader`` write a
        negative envelope so the next caller short-circuits.

        The DB bulkhead caps the number of concurrent loaders inside
        this worker to ``cache_db_fallback_max_concurrency`` so a Redis
        outage can't drown the MySQL pool.

        ``cached_adapter`` (optional) transforms the raw cached payload
        into the caller's preferred type on hit.  Useful when the cache
        stores a plain dict but the caller wants a DTO / dataclass
        instance.  Signature: ``async (raw) -> T``.  On miss the loader
        is responsible for whatever conversion is needed before writing.
        """
        if not self.is_enabled() or not self.domain_enabled(spec.domain):
            return await loader()

        cached = await self.get(spec, key)
        if isinstance(cached, _CacheMiss):
            # Distinguish negative-cache-hit from true miss.
            #
            # negative_hit: a previous loader returned None and we wrote
            # a negative envelope.  Subsequent callers should short-circuit,
            # not re-invoke the loader (that defeats the negative cache's
            # purpose of stopping penetration/stampede).
            #
            # miss / corrupt: key absent or unreadable — load.
            if cached.reason == "negative":
                if cached_adapter is not None:
                    return await cached_adapter(cached)
                return None  # type: ignore[return-value]
        elif not isinstance(cached, _CacheMiss):
            # True hit (positive payload).
            if cached_adapter is not None:
                return await cached_adapter(cached)  # type: ignore[no-any-return]
            return cached  # type: ignore[no-any-return]
        # else: miss / corrupt → fall through to loader path.

        # P0 收口:Distributed fill lock 真正集成.
        # 如果 spec 启用 distributed fill lock 且 fill_lock 实例可用,4 个进程同时
        # 抢锁 → 只 1 个进程拿到锁进入 _load;其它 3 个进程在锁外等待短时间后重新
        # 检查 cache (锁持有者会 SET cache key),看到 hit 后直接返回.
        # 之前 fill_lock 参数被接受但从未真正使用;现在接入 acquire-then-retry.
        _lock = None
        if fill_lock is not None and spec.enable_distributed_fill_lock:
            _lock = await fill_lock.acquire(key)
            if _lock is None:
                # Lock held: 等待 holder 写完 cache,然后重读 cache (小概率仍 miss 走 _load)
                for _ in range(20):
                    await asyncio.sleep(0.05)
                    cached2 = await self.get(spec, key)
                    if not isinstance(cached2, _CacheMiss):
                        if cached_adapter is not None:
                            return await cached_adapter(cached2)
                        return cached2  # type: ignore[no-any-return]
                # 20 次 retry 都没拿到:锁可能 stale / holder 挂掉. 进入 _load 本地
                # SingleFlight 路径,fallback 单进程合并.

        # SingleFlight + bulkhead gate.
        async def _load():
            try:
                async with self._bulkhead.acquire(timeout=2.0):
                    # Re-check inside the gate: another coroutine may have just
                    # populated the cache while we were waiting for the bulkhead.
                    cached2 = await self.get(spec, key)
                    if isinstance(cached2, _CacheMiss):
                        if cached2.reason == "negative":
                            # Another coroutine wrote a negative envelope while we
                            # waited for the bulkhead.  Honour it.
                            if cached_adapter is not None:
                                return await cached_adapter(cached2)
                            return None  # type: ignore[return-value]
                    elif not isinstance(cached2, _CacheMiss):
                        if cached_adapter is not None:
                            return await cached_adapter(cached2)
                        return cached2  # type: ignore[no-any-return]
                    payload = await loader()
                    if payload is None:
                        await self.set_negative(spec, key)
                    else:
                        await self.set(spec, key, payload)
                    # Apply cached_adapter on miss too: loader returns whatever
                    # the domain considers "raw" (often a dict for cache storage);
                    # the caller expects the domain's preferred type (DTO).
                    if cached_adapter is not None:
                        return await cached_adapter(payload)
                    return payload
            finally:
                # Release distributed fill lock if we own it
                if _lock is not None:
                    try:
                        await _lock.__aexit__(None, None, None)
                    except Exception:
                        pass

        if spec.enable_singleflight:
            return await self._singleflight.do(key, _load)

        return await _load()


# ── Module-level singleton ─────────────────────────────────────────

_manager: CacheManager | None = None


def get_cache_manager() -> CacheManager:
    """Return the lifespan-installed CacheManager; lazy-construct on first use.

    Test fixtures (and the Step 3 unit tests) override this via
    ``set_cache_manager(...)``.  Production code should always go
    through ``get_cache_manager()`` — never instantiate ``CacheManager``
    directly.
    """
    global _manager
    if _manager is None:
        _manager = CacheManager()
    return _manager


def set_cache_manager(mgr: CacheManager | None) -> None:
    """Install/reset the singleton (mainly for tests)."""
    global _manager
    _manager = mgr


__all__ = [
    "CACHE_BYPASS",
    "CACHE_ERROR",
    "CACHE_MISS",
    "CacheManager",
    "get_cache_manager",
    "set_cache_manager",
]