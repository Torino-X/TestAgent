"""Multi-worker consistency tests (提示词 §17).

Worker A modifies config → Worker B 立即读到新值.  These mirror the
real production scenario where ``LLMConfigCache`` previously had
in-process dict that disagreed across workers.

Uses :func:`fake_redis_pair` from the cache conftest — two
``FakeRedis`` instances bound to the **same** ``FakeServer`` so writes
on one are visible to the other.
"""

from __future__ import annotations

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
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
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


def _model_dto(**overrides) -> ModelConfigDTO:
    base = dict(
        public_id="mc_test",
        user_id=42,
        capability_type="chat",
        config_name="primary",
        provider="openai-compatible",
        api_base_url="https://example.com/v1",
        api_key_encrypted="gAAAAABlFernet==",
        model_name="gpt-4",
        timeout_seconds=120,
        enable_thinking=False,
        supports_vision=False,
        is_default=True,
        enabled=True,
    )
    base.update(overrides)
    return ModelConfigDTO(**base)


def _make_manager(redis_client) -> CacheManager:
    backend = CacheBackend(redis_client=redis_client)
    backend._healthy = True
    return CacheManager(
        backend=backend,
        breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=0.05)),
        bulkhead=DBBulkhead(max_concurrency=2),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,
    )


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    CacheBackend._instance = None
    cache_metrics.reset()
    yield
    CacheBackend._instance = None


class TestModelConfigMultiWorker:
    async def test_worker_a_writes_worker_b_reads(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair

        cache_a = ModelConfigCache(manager=_make_manager(worker_a_redis))
        cache_b = ModelConfigCache(manager=_make_manager(worker_b_redis))

        # Worker A: write through new config.
        new_dto = _model_dto(model_name="new-shared-model")
        ok = await cache_a.write_through(42, "chat", new_dto)
        assert ok is True

        # Worker B (different process, different in-process state):
        # next get_or_load sees the write-through.
        result = await cache_b.get_or_load(
            42,
            "chat",
            lambda: _async_return_dto(_model_dto(model_name="stale")),
        )
        assert result.model_name == "new-shared-model"

    async def test_invalidations_cross_worker(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = ModelConfigCache(manager=_make_manager(worker_a_redis))
        cache_b = ModelConfigCache(manager=_make_manager(worker_b_redis))

        # Worker A writes; Worker B reads (cache hit).
        await cache_a.write_through(42, "chat", _model_dto(model_name="v1"))

        async def stale_loader():
            return _model_dto(model_name="STALE")

        r = await cache_b.get_or_load(42, "chat", stale_loader)
        assert r.model_name == "v1", "Worker B should see Worker A's write"

        # Worker A invalidates (e.g. admin disabled user).
        ok = await cache_a.invalidate(42, "chat")
        assert ok is True

        # Worker B's next read: stale loader runs (cache empty).
        r2 = await cache_b.get_or_load(42, "chat", stale_loader)
        assert r2.model_name == "STALE", (
            "Worker B should observe invalidation and re-load"
        )


class TestKnowledgeConfigMultiWorker:
    async def test_system_change_invalidates_user_lookups(
        self, fake_redis_pair
    ) -> None:
        """If Worker A invalidates the system KB cache, Worker B's
        user-layer read still uses user row (cached), but a fresh read
        with no user row now sees the new system config."""
        worker_a_redis, worker_b_redis = fake_redis_pair

        cache_a = KnowledgeConfigCache(manager=_make_manager(worker_a_redis))
        cache_b = KnowledgeConfigCache(manager=_make_manager(worker_b_redis))

        # Worker B reads: user has no row → falls through to system.
        sys_dto = KnowledgeConfigDTO(
            public_id="kb_sys",
            user_id=None,
            scope="system",
            api_base_url="https://kb.example.com",
            api_key_encrypted="gAAAAABlFernet==",
            api_key_masked="****",
            default_knowledge_ids=["kb_v1"],
            top_k=5,
            similarity_threshold=0.35,
            retrieve_strategy=3,
            enable_rerank_model=True,
            rerank_model="bge-reranker-v2-m3",
            knowledge_graph=False,
            timeout_seconds=30,
            enabled=True,
            status="active",
        )

        async def loader_user():
            return None

        async def loader_system():
            return sys_dto

        r1 = await cache_b.get_or_load(42, loader_user, loader_system)
        assert r1.user_id is None

        # Worker A updates system config + invalidates system cache.
        ok = await cache_a.invalidate_system()
        assert ok is True

        # Worker B's next read must re-load system (cache was invalidated).
        sys_dto_v2 = KnowledgeConfigDTO(
            public_id="kb_sys",
            user_id=None,
            scope="system",
            api_base_url="https://kb.example.com",
            api_key_encrypted="gAAAAABlFernet==",
            api_key_masked="****",
            default_knowledge_ids=["kb_v2"],
            top_k=5,
            similarity_threshold=0.35,
            retrieve_strategy=3,
            enable_rerank_model=True,
            rerank_model="bge-reranker-v2-m3",
            knowledge_graph=False,
            timeout_seconds=30,
            enabled=True,
            status="active",
        )

        async def loader_system_v2():
            return sys_dto_v2

        r2 = await cache_b.get_or_load(42, loader_user, loader_system_v2)
        assert r2.user_id is None
        assert r2.default_knowledge_ids == ["kb_v2"], (
            "Worker B must observe the system update via the shared cache"
        )


class TestImageUnderstandingMultiWorker:
    async def test_encrypted_dto_shared(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = ImageUnderstandingConfigCache(manager=_make_manager(worker_a_redis))
        cache_b = ImageUnderstandingConfigCache(manager=_make_manager(worker_b_redis))

        dto = ImageUnderstandingConfigDTO(
            public_id="iu_test",
            user_id=42,
            api_base_url="https://vl.example.com",
            api_key_encrypted="gAAAAABlFernetCiphertext==",
            api_key_masked="****",
            model_name="qwen-vl-plus",
            timeout_seconds=60,
            max_tokens=2048,
            enable_in_doc_parsing=True,
            status="active",
        )
        ok = await cache_a.write_through(42, dto)
        assert ok is True

        # Worker B sees the ciphertext (NOT plaintext — that's the whole point).
        r = await cache_b.get_or_load(
            42, lambda: _async_return_dto(
                ImageUnderstandingConfigDTO(
                    public_id="WRONG",
                    user_id=42,
                    api_base_url="",
                    api_key_encrypted="",
                    api_key_masked="",
                    model_name="WRONG",
                    timeout_seconds=0,
                    max_tokens=None,
                    enable_in_doc_parsing=False,
                    status="",
                ),
            ),
        )
        assert r.public_id == "iu_test"
        assert r.model_name == "qwen-vl-plus"


class TestSystemConfigMultiWorker:
    async def test_global_key_shared(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = SystemConfigCache(manager=_make_manager(worker_a_redis))
        cache_b = SystemConfigCache(manager=_make_manager(worker_b_redis))

        # Worker A writes upload.max_file_size_mb=100.
        dto = SystemConfigDTO(
            config_key="upload.max_file_size_mb",
            config_value="100",
            value_type="int",
            description="test",
            editable=True,
        )
        await cache_a.write_through("upload.max_file_size_mb", dto)

        # Worker B reads.
        async def loader():
            return SystemConfigDTO(
                config_key="upload.max_file_size_mb",
                config_value="50",  # would be returned if loader ran
                value_type="int",
                description="",
                editable=True,
            )

        r = await cache_b.get_or_load("upload.max_file_size_mb", loader)
        assert r.config_value == "100", (
            "Worker B must observe the global upload limit update"
        )

    async def test_independent_keys_dont_cross_invalidate(
        self, fake_redis_pair
    ) -> None:
        """Worker A invalidates key X; Worker B's key Y remains cached."""
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = SystemConfigCache(manager=_make_manager(worker_a_redis))
        cache_b = SystemConfigCache(manager=_make_manager(worker_b_redis))

        await cache_a.write_through(
            "upload.max_file_size_mb",
            SystemConfigDTO(
                config_key="upload.max_file_size_mb",
                config_value="100",
                value_type="int",
                description="",
                editable=True,
            ),
        )
        await cache_a.write_through(
            "upload.max_files_per_conversation",
            SystemConfigDTO(
                config_key="upload.max_files_per_conversation",
                config_value="20",
                value_type="int",
                description="",
                editable=True,
            ),
        )

        # Invalidate only one key.
        await cache_a.invalidate("upload.max_file_size_mb")

        # Worker B's read of the other key must still hit cache.
        async def fresh_loader():
            raise RuntimeError("loader must not run on hit")

        r = await cache_b.get_or_load(
            "upload.max_files_per_conversation", fresh_loader,
        )
        assert r.config_value == "20"


async def _async_return_dto(dto):
    return dto