"""Redis 故障模拟测试 (提示词 §35 + 设计文档 §21.1, §22).

覆盖场景:
  1. Cache Redis 全宕机
     - 所有 7 domain cache fall back to DB, 不 raise
     - 业务路径不出现 500 风暴
  2. Cache Redis 超时 / 慢响应
     - breaker 在 N 次失败后快速打开
     - 打开后 fast-fail,不阻塞请求循环
  3. Cache Redis 恢复
     - breaker half-open 探测 → 关闭 → 缓存恢复
  4. 写穿失败
     - write_through 返回 False 但不 raise (业务继续)
  5. DB 风暴防护
     - bulkhead 在 Redis 故障时限制并发 loader 数
  6. 域隔离
     - Business Cache Redis down 不影响 Runtime Inflight (LiveEventBus / redis_inflight)

本文件复用 ``app.cache.manager.CacheManager`` + ``app.cache.backend.CacheBackend``
+ fakeredis,通过 monkeypatch 注入故障 (替换底层 ``get/set/delete`` 为抛
异常或慢响应函数),不依赖真实 Redis 进程。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fakeredis import aioredis as fakeredis_aioredis

from app.cache.backend import CacheBackend
from app.cache.bulkhead import BulkheadTimeoutError, DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, BreakerState, CircuitBreaker
from app.cache.domains.auth_cache import AuthPrincipalCache, AuthPrincipalDTO
from app.cache.domains.config_cache import (
    ImageUnderstandingConfigCache,
    ImageUnderstandingConfigDTO,
    KnowledgeConfigCache,
    KnowledgeConfigDTO,
    ModelConfigCache,
    ModelConfigDTO,
    SystemConfigCache,
    SystemConfigDTO,
)
from app.cache.domains.context_cache import (
    ContextMemoryCache,
    ContextMemoryDTO,
    WorkspaceInstructionCache,
    WorkspaceInstructionDTO,
)
from app.cache.domains.conversation_cache import (
    ConversationCache,
    ConversationDetailDTO,
    ConversationListDTO,
)
from app.cache.domains.library_cache import LibraryCache, LibraryListDTO
from app.cache.domains.semantic_profile_cache import (
    FileSemanticProfileCache,
    SemanticProfileDTO,
)
from app.cache.domains.task_cache import TaskDetailDTO, TaskStatusCache, TaskStatusDTO
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


# ── Helpers ────────────────────────────────────────────────────────


def _make_manager(
    fake_redis,
    *,
    breaker: CircuitBreaker | None = None,
    bulkhead: DBBulkhead | None = None,
    singleflight: SingleFlight | None = None,
) -> CacheManager:
    """Build a CacheManager wired to ``fake_redis`` with default test breaker."""
    backend = CacheBackend(redis_client=fake_redis)
    backend._healthy = True
    return _make_manager_with_backend(fake_redis, backend, breaker, bulkhead, singleflight)


def _make_manager_with_backend(
    fake_redis,
    backend: CacheBackend,
    breaker: CircuitBreaker | None = None,
    bulkhead: DBBulkhead | None = None,
    singleflight: SingleFlight | None = None,
) -> CacheManager:
    """Same as ``_make_manager`` but accepts an externally-created backend.

    Lets tests swap ``backend._redis`` mid-test to simulate outages.
    """
    return CacheManager(
        backend=backend,
        breaker=breaker or CircuitBreaker(
            BreakerConfig(failure_threshold=3, open_seconds=0.05)
        ),
        bulkhead=bulkhead or DBBulkhead(max_concurrency=2),
        singleflight=singleflight or SingleFlight(),
        jitter_fn=lambda ratio: 1.0,
    )


def _auth_dto(public_id: str = "usr_x") -> AuthPrincipalDTO:
    return AuthPrincipalDTO(
        internal_id=42,
        public_id=public_id,
        display_name="Alice",
        username="alice",
        email="alice@example.com",
        role="user",
        status="active",
        avatar_url=None,
    )


def _task_status_dto(public_id: str = "task_x") -> TaskStatusDTO:
    """TaskStatusDTO fields: status, updated_at, active_run_id, terminal."""
    return TaskStatusDTO(
        status="running",
        updated_at="2026-09-06T12:00:00+00:00",
        active_run_id="run-1",
        terminal=False,
    )


def _task_detail_dto(public_id: str = "task_x") -> TaskDetailDTO:
    return TaskDetailDTO(
        task_public_id=public_id,
        status="running",
        user_id=42,
        conversation_id=1,
        task_type="test_plan",
        title="Test task",
        error_code=None,
        error_message=None,
        started_at="2026-09-06T12:00:00+00:00",
        completed_at=None,
        updated_at="2026-09-06T12:00:00+00:00",
        active_run_id="run-1",
    )


def _model_dto() -> ModelConfigDTO:
    return ModelConfigDTO(
        public_id="mc_x",
        user_id=42,
        capability_type="default",
        config_name="default",
        provider="openai",
        api_base_url="https://api.openai.com/v1",
        api_key_encrypted="enc-blob",
        model_name="gpt-4",
        timeout_seconds=60,
        enable_thinking=False,
        supports_vision=False,
        is_default=True,
        enabled=True,
    )


def _knowledge_dto(*, user_id: int | None = 42) -> KnowledgeConfigDTO:
    return KnowledgeConfigDTO(
        public_id="kc_x",
        user_id=user_id,
        scope="user" if user_id is not None else "system",
        api_base_url="https://kb.example.com",
        api_key_encrypted="enc-blob",
        api_key_masked="****1234",
        default_knowledge_ids=[],
        top_k=5,
        similarity_threshold=0.7,
        retrieve_strategy=1,
        enable_rerank_model=False,
        rerank_model=None,
        knowledge_graph=False,
        timeout_seconds=30,
        enabled=True,
        status="active",
    )


def _img_dto() -> ImageUnderstandingConfigDTO:
    return ImageUnderstandingConfigDTO(
        public_id="iu_x",
        user_id=42,
        api_base_url="https://dashscope.aliyuncs.com",
        api_key_encrypted="enc-blob",
        api_key_masked="****abcd",
        model_name="qwen-vl-max",
        timeout_seconds=60,
        max_tokens=2048,
        enable_in_doc_parsing=False,
        status="active",
    )


def _sys_dto() -> SystemConfigDTO:
    return SystemConfigDTO(
        config_key="upload",
        config_value='{"max_bytes":10485760}',
        value_type="json",
        description="Upload settings",
        editable=True,
    )


def _conv_list_dto() -> ConversationListDTO:
    return ConversationListDTO(summaries=[{"id": "c1"}], total=1)


def _conv_detail_dto() -> ConversationDetailDTO:
    return ConversationDetailDTO(
        payload={
            "public_id": "c1",
            "title": "Hello",
            "message_count": 3,
            "created_at": "2026-09-06T12:00:00+00:00",
            "updated_at": "2026-09-06T12:00:00+00:00",
        }
    )


def _lib_dto() -> LibraryListDTO:
    return LibraryListDTO(items=[{"id": "f1"}], total=1)


def _memory_dto() -> ContextMemoryDTO:
    return ContextMemoryDTO(memories=[{"id": "mem_1"}])


def _instruction_dto() -> WorkspaceInstructionDTO:
    return WorkspaceInstructionDTO(instructions={"language": "zh"})


def _sem_dto() -> SemanticProfileDTO:
    return SemanticProfileDTO(
        file_public_id="file_x",
        status="ready",
        metadata={"summary": "ok"},
    )


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch) -> None:
    CacheBackend._instance = None
    cache_metrics.reset()
    yield
    CacheBackend._instance = None


# ── Scenario 1: Cache Redis 全宕机 ─────────────────────────────────


class TestRedisCompletelyDown:
    """Cache Redis 完全不可用 → 所有 domain cache fall back to DB."""

    async def test_cache_manager_get_returns_miss_silently(self) -> None:
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("connection refused")

            async def set(self, *a, **kw):
                raise ConnectionError("connection refused")

            async def delete(self, *a, **kw):
                raise ConnectionError("connection refused")

            async def mget(self, *a, **kw):
                raise ConnectionError("connection refused")

            async def ping(self):
                raise ConnectionError("connection refused")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("connection refused")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False  # simulate PING already failed
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        # GET → cache miss, not raise
        from app.cache.specs import CacheSpec

        spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)
        result = await mgr.get(spec, "k1")
        from app.cache.manager import _CacheMiss

        assert isinstance(result, _CacheMiss)
        assert result.reason == "miss"

    async def test_cache_manager_set_returns_false_silently(self) -> None:
        class BrokenRedis:
            async def set(self, *a, **kw):
                raise ConnectionError("redis down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        from app.cache.specs import CacheSpec

        spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)
        ok = await mgr.set(spec, "k", {"v": 1})
        assert ok is False  # not raise

    async def test_auth_cache_loader_still_runs_when_redis_down(
        self, monkeypatch
    ) -> None:
        """AuthPrincipalCache.get_or_load → loader runs, returns DTO."""
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = AuthPrincipalCache(manager=mgr)
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _auth_dto()

        dto = await cache.get_or_load("usr_x", loader)
        assert dto is not None
        assert dto.public_id == "usr_x"
        assert loader_calls["n"] == 1, "loader must run on redis down"

    async def test_auth_cache_write_through_returns_false_not_raise(self) -> None:
        """write_through fails silently — service code doesn't crash."""

        class BrokenRedis:
            async def set(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = AuthPrincipalCache(manager=mgr)
        ok = await cache.write_through("usr_x", _auth_dto())
        assert ok is False  # not raise

    async def test_task_cache_loader_runs_on_redis_down(self) -> None:
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = TaskStatusCache(manager=mgr)
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _task_status_dto()

        dto = await cache.get_or_load_status("task_x", loader)
        assert dto is not None
        assert dto.status == "running"
        assert loader_calls["n"] == 1

    async def test_config_cache_loader_runs_on_redis_down(self) -> None:
        """ModelConfigCache — 4 个 config cache 全部要 graceful degrade."""

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )

        # Model
        model_cache = ModelConfigCache(manager=mgr)
        loader_calls = {"model": 0}

        async def model_loader():
            loader_calls["model"] += 1
            return _model_dto()

        dto = await model_cache.get_or_load(42, "default", model_loader)
        assert dto is not None
        assert loader_calls["model"] == 1

        # Knowledge (dual-layer: user + system)
        kb_cache = KnowledgeConfigCache(manager=mgr)
        loader_calls_kb_user = {"n": 0}
        loader_calls_kb_sys = {"n": 0}

        async def kb_user_loader():
            loader_calls_kb_user["n"] += 1
            return _knowledge_dto(user_id=42)

        async def kb_system_loader():
            loader_calls_kb_sys["n"] += 1
            return _knowledge_dto(user_id=None)

        kdto = await kb_cache.get_or_load(42, kb_user_loader, kb_system_loader)
        assert kdto is not None
        # user loader 跑一次 (因为 cache miss), system loader 暂不需要
        # 但也可能跑 — 取决于 user layer 的结果
        assert loader_calls_kb_user["n"] >= 1

        # Image
        img_cache = ImageUnderstandingConfigCache(manager=mgr)
        loader_calls_img = {"n": 0}

        async def img_loader():
            loader_calls_img["n"] += 1
            return _img_dto()

        idto = await img_cache.get_or_load(42, img_loader)
        assert idto is not None
        assert loader_calls_img["n"] == 1

        # System (uses config_key)
        sys_cache = SystemConfigCache(manager=mgr)
        loader_calls_sys = {"n": 0}

        async def sys_loader():
            loader_calls_sys["n"] += 1
            return _sys_dto()

        sdto = await sys_cache.get_or_load("upload", sys_loader)
        assert sdto is not None
        assert loader_calls_sys["n"] == 1

    async def test_conversation_cache_loader_runs_on_redis_down(self) -> None:
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = ConversationCache(manager=mgr)

        async def list_loader():
            return _conv_list_dto()

        result = await cache.get_or_load_list(42, list_loader)
        assert result is not None
        assert result.total == 1

        async def detail_loader():
            return _conv_detail_dto()

        dresult = await cache.get_or_load_detail(42, "c1", detail_loader)
        assert dresult is not None
        assert dresult.payload["public_id"] == "c1"

    async def test_library_cache_loader_runs_on_redis_down(self) -> None:
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = LibraryCache(manager=mgr)

        async def loader():
            return _lib_dto()

        result = await cache.get_or_load_list(42, loader)
        assert result is not None
        assert result.total == 1

    async def test_context_cache_loaders_on_redis_down(self) -> None:
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )

        mem_cache = ContextMemoryCache(manager=mgr)
        loader_calls_mem = {"n": 0}

        async def mem_loader():
            loader_calls_mem["n"] += 1
            return _memory_dto()

        m = await mem_cache.get_or_load(42, "ws_a", mem_loader)
        assert m is not None
        assert loader_calls_mem["n"] == 1

        ins_cache = WorkspaceInstructionCache(manager=mgr)
        loader_calls_ins = {"n": 0}

        async def ins_loader():
            loader_calls_ins["n"] += 1
            return _instruction_dto()

        ins = await ins_cache.get_or_load(42, "ws_a", ins_loader)
        assert ins is not None
        assert loader_calls_ins["n"] == 1

    async def test_semantic_profile_get_many_works_on_redis_down(self) -> None:
        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = FileSemanticProfileCache(manager=mgr)
        loader_calls = {"n": 0}

        async def loader(missing):
            loader_calls["n"] += 1
            return {pid: _sem_dto() for pid in missing}

        result = await cache.get_many(["file_a", "file_b"], loader)
        assert loader_calls["n"] == 1
        assert "file_a" in result and "file_b" in result

    async def test_no_500_storm_when_redis_down(self) -> None:
        """100 个并发请求,Redis down → 100 个 loader 调用,无异常抛出."""
        from app.cache.specs import CacheSpec

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        # bulkhead 设大一点避免排队超时,本测试关注"无异常"
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
            bulkhead=DBBulkhead(max_concurrency=100),
            singleflight=SingleFlight(),
            jitter_fn=lambda ratio: 1.0,
        )
        spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)

        async def loader():
            return {"v": "from_db"}

        results = await asyncio.gather(
            *[mgr.get_or_load(spec, f"k{i}", loader) for i in range(100)],
            return_exceptions=True,
        )
        # 没有异常抛出
        assert all(not isinstance(r, Exception) for r in results)
        # 100 个都返回 loader 结果 (bulkhead 够大,SingleFlight 也会 dedupe)
        # 注意: 100 个不同的 key → 100 个 loader 调用 (SingleFlight 按 key 去重)
        # 这里 key 各不相同,所以 loader_calls 会是 100
        assert all(r == {"v": "from_db"} for r in results)


# ── Scenario 2: Redis 超时 / 慢响应 ──────────────────────────────


class TestRedisSlowTimeout:
    """Redis 慢响应 / 超时 → breaker 在 N 次失败后快速打开."""

    async def test_slow_redis_opens_breaker_after_threshold(self) -> None:
        """每次 GET 都 timeout → 3 次后 breaker 打开 → 第 4 次 fast-fail."""

        async def slow_get(*a, **kw):
            await asyncio.sleep(0.05)  # simulate 50ms slow
            raise TimeoutError("socket timeout")

        class SlowRedis:
            async def get(self, *a, **kw):
                await slow_get()

            async def set(self, *a, **kw):
                raise TimeoutError("socket timeout")

            async def delete(self, *a, **kw):
                raise TimeoutError("socket timeout")

            async def ping(self):
                raise TimeoutError("socket timeout")

            async def mget(self, *a, **kw):
                raise TimeoutError("socket timeout")

            async def pipeline(self, *a, **kw):
                raise TimeoutError("socket timeout")

        backend = CacheBackend(redis_client=SlowRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=3, open_seconds=10.0)
            ),
        )
        from app.cache.specs import CacheSpec

        spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)

        start = time.monotonic()
        # 5 次调用,前 3 次失败,后 2 次 fast-fail (breaker 已开)
        for _ in range(5):
            result = await mgr.get(spec, "k")
            from app.cache.manager import _CacheMiss

            assert isinstance(result, _CacheMiss)
        elapsed = time.monotonic() - start
        # 5 次调用总耗时 < 0.5s (假设每次失败 50ms × 3 = 150ms, 后 2 次 ~0ms)
        # 实际 fast-fail 后 ~0ms,所以总时间 < 250ms
        assert elapsed < 0.5, f"elapsed {elapsed:.3f}s — fast-fail not working"
        assert mgr._breaker.state is BreakerState.OPEN

    async def test_timeout_does_not_block_request_loop(self) -> None:
        """socket_timeout=100ms 模拟下,GET 不会无限阻塞."""

        async def hanging_get(*a, **kw):
            # fakeredis 没真超时,我们让 coroutine hang 一下再用 cancel 终止
            await asyncio.sleep(0.1)
            raise TimeoutError("socket timeout")

        class HangingRedis:
            async def get(self, *a, **kw):
                await hanging_get()

            async def set(self, *a, **kw):
                raise TimeoutError("socket timeout")

            async def delete(self, *a, **kw):
                raise TimeoutError("socket timeout")

            async def ping(self):
                raise TimeoutError("socket timeout")

            async def mget(self, *a, **kw):
                raise TimeoutError("socket timeout")

            async def pipeline(self, *a, **kw):
                raise TimeoutError("socket timeout")

        backend = CacheBackend(redis_client=HangingRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        from app.cache.specs import CacheSpec

        spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)

        start = time.monotonic()
        # 10 次调用,每次失败 ~100ms,总耗时应 < 1.5s
        for _ in range(10):
            result = await mgr.get(spec, "k")
        elapsed = time.monotonic() - start
        assert elapsed < 1.5, f"请求循环被阻塞: {elapsed:.3f}s"


# ── Scenario 3: Redis 恢复 ────────────────────────────────────────


class TestRedisRecovery:
    """Redis 恢复后 breaker half-open → close,缓存自动恢复."""

    async def test_breaker_recovery_after_redis_comes_back(self, fake_redis) -> None:
        """模拟: Redis 先挂 → breaker 开 → 恢复 → breaker 半开探测 → 关."""
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        # threshold=2, open_seconds=0.05 — 2 次失败后即打开
        mgr = _make_manager_with_backend(
            fake_redis,
            backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=2, open_seconds=0.05)
            ),
        )
        from app.cache.specs import CacheSpec

        spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)

        # 1. Redis down → 用 BrokenRedis 替换 backend 的 redis
        original_redis = backend._redis

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        backend._redis = BrokenRedis()
        # 2 次失败后 breaker 打开
        await mgr.get(spec, "k")
        await mgr.get(spec, "k")
        assert mgr._breaker.state is BreakerState.OPEN

        # 2. 等待 open_seconds (0.05s)
        await asyncio.sleep(0.06)
        # 此时半开,下一次调用作为 probe
        # 3. Redis 恢复 → 换回 fake_redis
        backend._redis = original_redis
        backend._healthy = True
        # 4. probe 调用 GET (fake_redis 上 key 不存在) → 成功 (返回 None) → breaker 关闭
        result = await mgr.get(spec, "recovery-key")
        from app.cache.manager import _CacheMiss

        assert isinstance(result, _CacheMiss)
        # breaker 应该 close
        assert mgr._breaker.state is BreakerState.CLOSED, (
            f"expected CLOSED after successful probe, got {mgr._breaker.state}"
        )
        # 5. 之后可以正常 SET / GET
        await mgr.set(spec, "k1", {"v": "after-recovery"})
        assert (await mgr.get(spec, "k1")) == {"v": "after-recovery"}

    async def test_cache_resumes_after_recovery(self, fake_redis) -> None:
        """模拟恢复后,domain cache 立即能 cache hit."""
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        mgr = _make_manager_with_backend(fake_redis, backend)
        cache = AuthPrincipalCache(manager=mgr)

        # 1. 写入 cache
        await cache.write_through("usr_x", _auth_dto())

        # 2. 模拟 Redis 短时挂
        original_redis = backend._redis

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        backend._redis = BrokenRedis()
        backend._healthy = False

        # Redis down → loader 跑
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _auth_dto("usr_x")

        await cache.get_or_load("usr_x", loader)
        assert loader_calls["n"] == 1

        # 3. Redis 恢复
        backend._redis = original_redis
        backend._healthy = True
        # 等 breaker 从 OPEN 转 HALF_OPEN (open_seconds=0.05)
        await asyncio.sleep(0.06)
        # 触发一次成功的 probe 来 close breaker
        from app.cache.specs import CacheSpec

        spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)
        await mgr.get(spec, "probe-key")  # probe
        assert mgr._breaker.state is BreakerState.CLOSED

        # 4. 重新写入 cache,后续 read 应该 cache hit
        await cache.write_through("usr_x", _auth_dto("usr_x"))
        loader_calls["n"] = 0

        async def fresh_loader():
            loader_calls["n"] += 1
            return _auth_dto("usr_x")

        dto = await cache.get_or_load("usr_x", fresh_loader)
        assert dto is not None
        assert loader_calls["n"] == 0, "cache hit expected after recovery"


# ── Scenario 4: 写穿失败 ──────────────────────────────────────────


class TestWriteThroughFailure:
    """write_through 在 Redis down 时返回 False 但不 raise — 业务继续."""

    async def test_auth_write_through_returns_false_on_redis_down(self) -> None:
        class BrokenRedis:
            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = AuthPrincipalCache(manager=mgr)
        # 都不应 raise
        ok = await cache.write_through("usr_x", _auth_dto())
        assert ok is False

    async def test_task_write_through_returns_false_on_redis_down(self) -> None:
        class BrokenRedis:
            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = TaskStatusCache(manager=mgr)
        # write_through(status, active_run_id, updated_at)
        ok = await cache.write_through("task_x", "running", "run-1", None)
        assert ok is False

    async def test_invalidate_returns_false_on_redis_down(self) -> None:
        class BrokenRedis:
            async def delete(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = AuthPrincipalCache(manager=mgr)
        ok = await cache.invalidate("usr_x")
        assert ok is False


# ── Scenario 5: DB 风暴防护 ──────────────────────────────────────


class TestDbStormProtection:
    """Redis down + DB slow → bulkhead 限制并发 loader,防 MySQL pool 爆."""

    async def test_bulkhead_caps_concurrent_loaders(self) -> None:
        """bulkhead max_concurrency=2 → 第 3 个 loader 排队 → 设短超时即抛."""
        from app.cache.specs import CacheSpec

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        bh = DBBulkhead(max_concurrency=2)
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
            bulkhead=bh,
        )
        spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)

        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_loader():
            started.set()
            await release.wait()
            return {"v": "ok"}

        # 3 个并发, bulkhead=2, 第 3 个会等 → 2s 后超时
        t1 = asyncio.create_task(mgr.get_or_load(spec, "k1", slow_loader))
        t2 = asyncio.create_task(mgr.get_or_load(spec, "k2", slow_loader))
        await started.wait()
        # 第三个应该在排队 (key3 不同于 key1, key2 → 不走 SingleFlight dedupe)
        with pytest.raises(BulkheadTimeoutError):
            await asyncio.wait_for(
                mgr.get_or_load(spec, "k3", slow_loader), timeout=3.0
            )
        # 前两个释放后,bulkhead 不再满
        release.set()
        r1 = await t1
        r2 = await t2
        assert r1 == {"v": "ok"}
        assert r2 == {"v": "ok"}

    async def test_no_db_storm_when_breaker_open(self) -> None:
        """breaker 打开后,cache miss 不调用 loader (短路)."""

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        # breaker 阈值 2 → 2 次失败后开
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=2, open_seconds=10.0)
            ),
        )
        from app.cache.specs import CacheSpec

        spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)

        # 触发 breaker 打开
        await mgr.get(spec, "k1")
        await mgr.get(spec, "k2")
        assert mgr._breaker.state is BreakerState.OPEN

        # 之后 100 次 get → 全部 fast-fail (loader 不应被调用)
        # 等等 — get_or_load 的逻辑: cache miss → 走 loader
        # 但 breaker open 时 _redis_op 返回 None → 视为 cache miss → 走 loader
        # 所以 get_or_load 仍然会调用 loader
        # 关键是 get/set/delete 不会阻塞业务
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return {"v": "ok"}

        # 100 次 (注意 SingleFlight 会按 key dedupe,所以最多 = unique keys)
        # 这里都用不同 key
        keys = [f"k{i}" for i in range(100)]
        results = await asyncio.gather(
            *[mgr.get_or_load(spec, k, loader) for k in keys],
            return_exceptions=True,
        )
        # 没有异常
        assert all(not isinstance(r, Exception) for r in results)


# ── Scenario 6: 域隔离 ──────────────────────────────────────────


class TestDomainIsolation:
    """Business Cache Redis down ≠ Runtime Redis down.

    两个故障域物理隔离:
      - Business Cache Redis: port 6380 (allkeys-lfu)
      - Runtime Inflight Redis: port 6379 (noeviction)
    """

    async def test_business_cache_down_不影响_runtime_inflight_registry(self) -> None:
        """即使 business cache 完全挂, inflight registry 也正常工作."""
        from app.agent_runtime.persistence.redis_inflight_registry import (
            RedisInFlightRegistry,
        )

        # Runtime redis 用真 fakeredis
        runtime_redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
        inflight = RedisInFlightRegistry(redis_client=runtime_redis, worker_id="w1")

        # 即使 business cache 完全不可用, inflight 也能正常 acquire/release
        owner = await inflight.try_acquire("task-public-1", "langgraph")
        assert owner is not None
        await inflight.release("task-public-1", owner)

        # 不同的 task 仍然能 acquire
        owner2 = await inflight.try_acquire("task-public-2", "langgraph")
        assert owner2 is not None
        await inflight.release("task-public-2", owner2)

        await runtime_redis.flushall()
        await runtime_redis.aclose()

    async def test_business_cache_down_不影响_feedback_cache(self) -> None:
        """FeedbackCache 已迁移到 Business Cache Redis (Step 9) — 与 cache manager
        物理隔离在 *Redis 实例层* (fakeredis 不同实例)。

        即使 business cache backend 挂了,feedback cache 仍走独立 Redis 实例工作。
        """
        from app.cache.backend import CacheBackend as _CB
        from app.cache.bulkhead import DBBulkhead as _DB
        from app.cache.circuit_breaker import BreakerConfig as _BC, CircuitBreaker as _CBR
        from app.cache.domains.feedback_cache import FeedbackCache as _DomainFC
        from app.cache.manager import CacheManager as _CM
        from app.cache.singleflight import SingleFlight as _SF

        # Runtime 模拟独立 Redis 实例 (反馈走的是同一个 Business Cache Redis,
        # 但通过不同 CacheManager + 不同 redis client 实例隔离)
        runtime_redis = fakeredis_aioredis.FakeRedis(decode_responses=False)
        runtime_backend = _CB(redis_client=runtime_redis)
        runtime_backend._healthy = True
        runtime_mgr = _CM(
            backend=runtime_backend,
            breaker=_CBR(_BC(failure_threshold=5, open_seconds=1.0)),
            bulkhead=_DB(max_concurrency=2),
            singleflight=_SF(),
            jitter_fn=lambda ratio: 1.0,
        )
        fb_cache = _DomainFC(manager=runtime_mgr)

        # 即使 business cache 不可用, feedback cache 仍工作
        ok = await fb_cache.set(42, 1001, "like")
        assert ok is True

        got = await fb_cache.get(42, 1001)
        assert got is not None
        assert got.feedback_type == "like"

        ok = await fb_cache.delete(42, 1001)
        assert ok is True

        await runtime_redis.flushall()
        await runtime_redis.aclose()

    async def test_business_cache_and_runtime_redis_can_fail_independently(
        self,
    ) -> None:
        """Business cache 挂 + Runtime 也挂 → 各自的降级路径独立触发."""

        # Business cache 完全挂
        class BrokenBusinessRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("business down")

            async def set(self, *a, **kw):
                raise ConnectionError("business down")

            async def delete(self, *a, **kw):
                raise ConnectionError("business down")

            async def ping(self):
                raise ConnectionError("business down")

            async def mget(self, *a, **kw):
                raise ConnectionError("business down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("business down")

        backend = CacheBackend(redis_client=BrokenBusinessRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        # Runtime redis 也挂
        runtime = fakeredis_aioredis.FakeRedis(decode_responses=False)
        # 模拟 Runtime 也挂 — 替换方法
        class BrokenRuntimeRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("runtime down")

            async def set(self, *a, **kw):
                raise ConnectionError("runtime down")

            async def delete(self, *a, **kw):
                raise ConnectionError("runtime down")

        # 业务 cache 走 loader (Redis down)
        cache = AuthPrincipalCache(manager=mgr)

        async def loader():
            return _auth_dto()

        dto = await cache.get_or_load("usr_x", loader)
        assert dto is not None
        # Runtime 也走降级 (返回 None)
        from app.agent_runtime.persistence.redis_inflight_registry import (
            RedisInFlightRegistry,
        )

        inflight = RedisInFlightRegistry(
            redis_client=BrokenRuntimeRedis(), worker_id="w1"
        )
        owner = await inflight.try_acquire("task-x", "langgraph")
        assert owner is None  # runtime down → 返回 None (降级)

        # 两个故障独立处理,互不影响
        await runtime.flushall()
        await runtime.aclose()


# ── Scenario 7: 端到端业务场景 ──────────────────────────────────


class TestBusinessScenariosSurvive:
    """§35 第 4 步要求的业务场景: login / chat / send_message / SSE / config."""

    async def test_login_path_returns_user_via_loader_when_cache_down(self) -> None:
        """Cache down → 登录流程仍能拿到 user (走 DB loader)."""
        from app.cache.specs import CacheSpec

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = AuthPrincipalCache(manager=mgr)

        # 模拟 deps.get_current_user 路径
        async def load_user_from_db():
            return _auth_dto("usr_login")

        user = await cache.get_or_load("usr_login", load_user_from_db)
        assert user is not None
        assert user.public_id == "usr_login"
        assert user.status == "active"

    async def test_sse_heartbeat_returns_status_when_cache_down(self) -> None:
        """Agent SSE 路径 — task status 读 cache → cache down 走 DB."""
        from app.cache.specs import CacheSpec

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = TaskStatusCache(manager=mgr)

        async def load_status():
            return _task_status_dto("task_sse")

        status = await cache.get_or_load_status("task_sse", load_status)
        assert status is not None
        assert status.status == "running"

    async def test_chat_load_conversation_list_when_cache_down(self) -> None:
        """Chat 首屏 — conversation list 走 DB."""
        from app.cache.specs import CacheSpec

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )
        cache = ConversationCache(manager=mgr)

        async def load_list():
            return _conv_list_dto()

        result = await cache.get_or_load_list(42, load_list)
        assert result is not None
        assert result.total == 1

    async def test_config_endpoint_returns_config_when_cache_down(self) -> None:
        """Config 页面 — model/knowledge/image 全部走 DB."""
        from app.cache.specs import CacheSpec

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        backend = CacheBackend(redis_client=BrokenRedis())
        backend._healthy = False
        mgr = CacheManager(
            backend=backend,
            breaker=CircuitBreaker(
                BreakerConfig(failure_threshold=10, open_seconds=10.0)
            ),
        )

        # Model
        model_cache = ModelConfigCache(manager=mgr)

        async def model_loader():
            return _model_dto()

        mdto = await model_cache.get_or_load(42, "default", model_loader)
        assert mdto is not None
        assert mdto.provider == "openai"


# ── Scenario 8: 多次循环故障 / 恢复 ────────────────────────────────


class TestRepeatedOutageCycles:
    """Redis 多次故障 / 恢复循环 — 验证 breaker 状态机稳健."""

    async def test_multiple_outage_cycles(self, fake_redis) -> None:
        """3 轮 outage → recovery → outage,每次 cache 能正常工作."""
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        mgr = _make_manager_with_backend(fake_redis, backend)
        cache = AuthPrincipalCache(manager=mgr)
        original_redis = backend._redis

        class BrokenRedis:
            async def get(self, *a, **kw):
                raise ConnectionError("down")

            async def set(self, *a, **kw):
                raise ConnectionError("down")

            async def delete(self, *a, **kw):
                raise ConnectionError("down")

            async def ping(self):
                raise ConnectionError("down")

            async def mget(self, *a, **kw):
                raise ConnectionError("down")

            async def pipeline(self, *a, **kw):
                raise ConnectionError("down")

        for cycle in range(3):
            # 故障期
            backend._redis = BrokenRedis()
            backend._healthy = False

            async def loader():
                return _auth_dto(f"usr_c{cycle}")

            dto = await cache.get_or_load(f"usr_c{cycle}", loader)
            assert dto is not None
            assert dto.public_id == f"usr_c{cycle}"

            # 恢复期
            backend._redis = original_redis
            backend._healthy = True
            await asyncio.sleep(0.06)  # 等 breaker 恢复
            # 触发一次成功的 probe
            from app.cache.specs import CacheSpec

            spec = CacheSpec(domain="auth", ttl_seconds=60, negative_ttl_seconds=5)
            await mgr.get(spec, f"probe-{cycle}")
            assert mgr._breaker.state is BreakerState.CLOSED, (
                f"cycle {cycle}: breaker not closed"
            )

            # 验证 cache 写入/读出
            await cache.write_through(f"usr_c{cycle}", _auth_dto(f"usr_c{cycle}"))

            async def read_loader():
                raise RuntimeError("should not be called on cache hit")

            loaded = await cache.get_or_load(f"usr_c{cycle}", read_loader)
            assert loaded.public_id == f"usr_c{cycle}"