"""Tests for ``app.cache.domains.config_cache``.

Coverage (per prompt §17-20 / 设计文档 §11):
  * Spec invariants: TTL / negative TTL / domain / fill_lock flag
  * Key namespace format
  * DTO never includes plaintext API key (only api_key_encrypted)
  * ModelConfigCache get_or_load hit / miss / 100 concurrent → 1 loader
  * ModelConfigCache write_through updates cache (encrypted DTO)
  * ModelConfigCache invalidate
  * KnowledgeConfigCache user → system fallback (dual-layer)
  * ImageUnderstandingConfigCache write_through (encrypted DTO)
  * SystemConfigCache get_or_load / write_through / invalidate
  * Domain disabled → bypass
"""

from __future__ import annotations

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.domains.config_cache import (
    IMAGE_UNDERSTANDING_SPEC,
    KNOWLEDGE_CONFIG_SYSTEM_SPEC,
    KNOWLEDGE_CONFIG_USER_SPEC,
    MODEL_CONFIG_SPEC,
    SYSTEM_CONFIG_SPEC,
    ImageUnderstandingConfigCache,
    ImageUnderstandingConfigDTO,
    KnowledgeConfigCache,
    KnowledgeConfigDTO,
    ModelConfigCache,
    ModelConfigDTO,
    SystemConfigCache,
    SystemConfigDTO,
    image_understanding_key,
    knowledge_system_key,
    knowledge_user_key,
    model_config_key,
    system_config_key,
)
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


# ── helpers ────────────────────────────────────────────────────────


def _model_dto(uid: int = 42, cap: str = "chat", **overrides) -> ModelConfigDTO:
    base = dict(
        public_id="mc_test",
        user_id=uid,
        capability_type=cap,
        config_name="primary",
        provider="openai-compatible",
        api_base_url="https://example.com/v1",
        api_key_encrypted="gAAAAABlFernetCiphertext==",
        model_name="gpt-4",
        timeout_seconds=120,
        enable_thinking=False,
        supports_vision=False,
        is_default=True,
        enabled=True,
    )
    base.update(overrides)
    return ModelConfigDTO(**base)


def _kb_dto(uid: int | None = 42, **overrides) -> KnowledgeConfigDTO:
    base = dict(
        public_id="kb_test",
        user_id=uid,
        scope="user" if uid else "system",
        api_base_url="https://kb.example.com",
        api_key_encrypted="gAAAAABlFernetCiphertext==",
        api_key_masked="****",
        default_knowledge_ids=["kb1", "kb2"],
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
    base.update(overrides)
    return KnowledgeConfigDTO(**base)


def _iu_dto(uid: int = 42, **overrides) -> ImageUnderstandingConfigDTO:
    base = dict(
        public_id="iu_test",
        user_id=uid,
        api_base_url="https://vl.example.com",
        api_key_encrypted="gAAAAABlFernetCiphertext==",
        api_key_masked="****",
        model_name="qwen-vl-plus",
        timeout_seconds=60,
        max_tokens=2048,
        enable_in_doc_parsing=True,
        status="active",
    )
    base.update(overrides)
    return ImageUnderstandingConfigDTO(**base)


def _system_dto(**overrides) -> SystemConfigDTO:
    base = dict(
        config_key="upload.max_file_size_mb",
        config_value="50",
        value_type="int",
        description="Max upload size",
        editable=True,
    )
    base.update(overrides)
    return SystemConfigDTO(**base)


def _async_loader(dto):
    async def _l():
        return dto

    return _l


def _make_manager(fake_redis) -> CacheManager:
    backend = CacheBackend(redis_client=fake_redis)
    backend._healthy = True
    return CacheManager(
        backend=backend,
        breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=0.05)),
        bulkhead=DBBulkhead(max_concurrency=2),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,
    )


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch) -> None:
    CacheBackend._instance = None
    cache_metrics.reset()
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "cache_cfg_enabled", True)
    yield
    CacheBackend._instance = None


# ── Spec invariants ────────────────────────────────────────────────


class TestSpecInvariants:
    def test_model_spec_ttl(self) -> None:
        """30m per 提示词 §17."""
        assert MODEL_CONFIG_SPEC.ttl_seconds == 30 * 60
        assert MODEL_CONFIG_SPEC.negative_ttl_seconds == 60
        assert MODEL_CONFIG_SPEC.enable_distributed_fill_lock is True
        assert MODEL_CONFIG_SPEC.domain == "cfg"

    def test_kb_user_spec_ttl(self) -> None:
        """user 30m per 提示词 §18."""
        assert KNOWLEDGE_CONFIG_USER_SPEC.ttl_seconds == 30 * 60
        assert KNOWLEDGE_CONFIG_USER_SPEC.negative_ttl_seconds == 60

    def test_kb_system_spec_ttl(self) -> None:
        """system 10m per 提示词 §18."""
        assert KNOWLEDGE_CONFIG_SYSTEM_SPEC.ttl_seconds == 10 * 60

    def test_image_understanding_spec_ttl(self) -> None:
        """30m per 提示词 §19."""
        assert IMAGE_UNDERSTANDING_SPEC.ttl_seconds == 30 * 60

    def test_system_config_spec_ttl(self) -> None:
        """10m per 提示词 §20."""
        assert SYSTEM_CONFIG_SPEC.ttl_seconds == 10 * 60
        assert SYSTEM_CONFIG_SPEC.enable_distributed_fill_lock is False


class TestKeyNamespace:
    def test_model_config_key(self) -> None:
        k = model_config_key(42, "chat")
        assert k.endswith(":cfg:model:user:42:cap:chat"), k

    def test_kb_user_key(self) -> None:
        k = knowledge_user_key(42)
        assert k.endswith(":cfg:knowledge:user:42"), k

    def test_kb_system_key(self) -> None:
        k = knowledge_system_key()
        assert k.endswith(":cfg:knowledge:system"), k

    def test_iu_key(self) -> None:
        k = image_understanding_key(42)
        assert k.endswith(":cfg:image:user:42"), k

    def test_system_config_key(self) -> None:
        k = system_config_key("upload.max_file_size_mb")
        assert k.endswith(":cfg:system:upload.max_file_size_mb"), k


class TestDTOEncrypted:
    def test_model_dto_has_no_plaintext_key(self) -> None:
        d = _model_dto().to_dict()
        # Only ciphertext, never plaintext.
        assert "api_key_encrypted" in d
        assert "api_key" not in d
        assert "api_key_plain" not in d

    def test_kb_dto_has_no_plaintext_key(self) -> None:
        d = _kb_dto().to_dict()
        assert "api_key_encrypted" in d
        assert "api_key_masked" in d  # masked is OK; plaintext is not.
        assert "api_key" not in d

    def test_iu_dto_has_no_plaintext_key(self) -> None:
        d = _iu_dto().to_dict()
        assert "api_key_encrypted" in d
        assert "api_key_masked" in d
        assert "api_key" not in d


# ── ModelConfigCache ────────────────────────────────────────────────


class TestModelConfigCache:
    async def test_get_or_load_miss_then_hit(self, fake_redis) -> None:
        cache = ModelConfigCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _model_dto()

        r1 = await cache.get_or_load(42, "chat", loader)
        r2 = await cache.get_or_load(42, "chat", loader)
        assert r1.public_id == "mc_test"
        assert r2.public_id == "mc_test"
        assert loader_calls["n"] == 1

    async def test_100_concurrent_misses_coalesce_to_one_loader(
        self, fake_redis
    ) -> None:
        import asyncio

        cache = ModelConfigCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            await asyncio.sleep(0.01)
            return _model_dto()

        results = await asyncio.gather(
            *[cache.get_or_load(42, "chat", loader) for _ in range(100)]
        )
        assert loader_calls["n"] == 1
        assert all(r.public_id == "mc_test" for r in results)

    async def test_different_capability_keys_independent(
        self, fake_redis
    ) -> None:
        cache = ModelConfigCache(manager=_make_manager(fake_redis))
        await cache.get_or_load(42, "chat", _async_loader(_model_dto(cap="chat")))
        await cache.get_or_load(42, "embedding", _async_loader(_model_dto(cap="embedding")))
        # 2 different keys must exist in Redis.
        keys = await fake_redis.keys("*cap:chat*")
        embedding_keys = await fake_redis.keys("*cap:embedding*")
        assert len(keys) >= 1
        assert len(embedding_keys) >= 1

    async def test_write_through_updates_cache(self, fake_redis) -> None:
        cache = ModelConfigCache(manager=_make_manager(fake_redis))
        # Prime cache with chat-capability default.
        await cache.get_or_load(42, "chat", _async_loader(_model_dto(model_name="old")))
        # Write through with new model.
        new_dto = _model_dto(model_name="new-model")
        ok = await cache.write_through(42, "chat", new_dto)
        assert ok is True
        # Next read returns updated.
        result = await cache.get_or_load(42, "chat", _async_loader(_model_dto(model_name="fresh")))
        assert result.model_name == "new-model"

    async def test_write_through_none_invalidates(self, fake_redis) -> None:
        cache = ModelConfigCache(manager=_make_manager(fake_redis))
        await cache.get_or_load(42, "chat", _async_loader(_model_dto()))
        ok = await cache.write_through(42, "chat", None)
        assert ok is True
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _model_dto()

        await cache.get_or_load(42, "chat", loader)
        assert loader_calls["n"] == 1

    async def test_invalidate_removes_entry(self, fake_redis) -> None:
        cache = ModelConfigCache(manager=_make_manager(fake_redis))
        await cache.get_or_load(42, "chat", _async_loader(_model_dto()))
        ok = await cache.invalidate(42, "chat")
        assert ok is True


# ── KnowledgeConfigCache (dual-layer) ───────────────────────────────


class TestKnowledgeConfigCache:
    async def test_user_cache_hit(self, fake_redis) -> None:
        cache = KnowledgeConfigCache(manager=_make_manager(fake_redis))
        loader_user_calls = {"n": 0}
        loader_system_calls = {"n": 0}

        async def loader_user():
            loader_user_calls["n"] += 1
            return _kb_dto(uid=42)

        async def loader_system():
            loader_system_calls["n"] += 1
            return _kb_dto(uid=None)

        r = await cache.get_or_load(42, loader_user, loader_system)
        assert r.user_id == 42
        assert loader_user_calls["n"] == 1
        assert loader_system_calls["n"] == 0, "system loader must not run on user hit"

    async def test_user_miss_then_system_hit(self, fake_redis) -> None:
        cache = KnowledgeConfigCache(manager=_make_manager(fake_redis))
        loader_user_calls = {"n": 0}
        loader_system_calls = {"n": 0}

        async def loader_user():
            loader_user_calls["n"] += 1
            return None  # user has no KB config

        async def loader_system():
            loader_system_calls["n"] += 1
            return _kb_dto(uid=None)

        r = await cache.get_or_load(42, loader_user, loader_system)
        assert r.user_id is None, "system fallback DTO"
        assert loader_user_calls["n"] == 1
        assert loader_system_calls["n"] == 1

    async def test_user_negative_then_system_negative_returns_none(
        self, fake_redis
    ) -> None:
        cache = KnowledgeConfigCache(manager=_make_manager(fake_redis))
        loader_user_calls = {"n": 0}
        loader_system_calls = {"n": 0}

        async def loader_user():
            loader_user_calls["n"] += 1
            return None

        async def loader_system():
            loader_system_calls["n"] += 1
            return None

        r1 = await cache.get_or_load(42, loader_user, loader_system)
        assert r1 is None
        # Second call must short-circuit (negative caches on both layers).
        r2 = await cache.get_or_load(42, loader_user, loader_system)
        assert r2 is None
        assert loader_user_calls["n"] == 1
        assert loader_system_calls["n"] == 1

    async def test_user_cache_persists_across_calls(self, fake_redis) -> None:
        cache = KnowledgeConfigCache(manager=_make_manager(fake_redis))
        loader_user_calls = {"n": 0}

        async def loader_user():
            loader_user_calls["n"] += 1
            return _kb_dto(uid=42)

        async def loader_system():
            return _kb_dto(uid=None)

        await cache.get_or_load(42, loader_user, loader_system)
        await cache.get_or_load(42, loader_user, loader_system)
        assert loader_user_calls["n"] == 1

    async def test_invalidate_user_then_system_serves(self, fake_redis) -> None:
        cache = KnowledgeConfigCache(manager=_make_manager(fake_redis))
        loader_user_calls = {"n": 0}

        async def loader_user():
            loader_user_calls["n"] += 1
            return _kb_dto(uid=42) if loader_user_calls["n"] == 1 else None

        async def loader_system():
            return _kb_dto(uid=None)

        # First call: user row exists.
        await cache.get_or_load(42, loader_user, loader_system)
        # Invalidate user cache.
        ok = await cache.invalidate_user(42)
        assert ok is True
        # Next call: user loader returns None → fall through to system.
        r = await cache.get_or_load(42, loader_user, loader_system)
        assert r.user_id is None

    async def test_invalidate_system(self, fake_redis) -> None:
        cache = KnowledgeConfigCache(manager=_make_manager(fake_redis))

        async def loader_user():
            return None

        async def loader_system():
            return _kb_dto(uid=None)

        await cache.get_or_load(42, loader_user, loader_system)
        ok = await cache.invalidate_system()
        assert ok is True


# ── ImageUnderstandingConfigCache ─────────────────────────────────


class TestImageUnderstandingCache:
    async def test_get_or_load_hit_returns_dto(self, fake_redis) -> None:
        cache = ImageUnderstandingConfigCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _iu_dto()

        r = await cache.get_or_load(42, loader)
        assert r.model_name == "qwen-vl-plus"
        assert loader_calls["n"] == 1
        # Second call is a hit.
        r2 = await cache.get_or_load(42, loader)
        assert r2.model_name == "qwen-vl-plus"
        assert loader_calls["n"] == 1

    async def test_write_through_updates_cache(self, fake_redis) -> None:
        cache = ImageUnderstandingConfigCache(manager=_make_manager(fake_redis))
        await cache.get_or_load(42, _async_loader(_iu_dto(model_name="old")))
        new_dto = _iu_dto(model_name="new-vl")
        ok = await cache.write_through(42, new_dto)
        assert ok is True
        r = await cache.get_or_load(42, _async_loader(_iu_dto(model_name="fresh")))
        assert r.model_name == "new-vl"

    async def test_invalidate_removes_entry(self, fake_redis) -> None:
        cache = ImageUnderstandingConfigCache(manager=_make_manager(fake_redis))
        await cache.get_or_load(42, _async_loader(_iu_dto()))
        ok = await cache.invalidate(42)
        assert ok is True


# ── SystemConfigCache ──────────────────────────────────────────────


class TestSystemConfigCache:
    async def test_get_or_load_then_hit(self, fake_redis) -> None:
        cache = SystemConfigCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _system_dto()

        r1 = await cache.get_or_load("upload.max_file_size_mb", loader)
        r2 = await cache.get_or_load("upload.max_file_size_mb", loader)
        assert r1.config_value == "50"
        assert r2.config_value == "50"
        assert loader_calls["n"] == 1

    async def test_different_keys_cached_independently(self, fake_redis) -> None:
        cache = SystemConfigCache(manager=_make_manager(fake_redis))
        await cache.get_or_load(
            "upload.max_file_size_mb", _async_loader(_system_dto(config_key="upload.max_file_size_mb")),
        )
        await cache.get_or_load(
            "upload.max_files_per_conversation",
            _async_loader(_system_dto(config_key="upload.max_files_per_conversation")),
        )
        # Both keys must exist independently.
        keys = await fake_redis.keys("*cfg:system:upload.*")
        assert len(keys) == 2

    async def test_write_through_then_invalidate(self, fake_redis) -> None:
        cache = SystemConfigCache(manager=_make_manager(fake_redis))
        await cache.get_or_load(
            "upload.max_file_size_mb", _async_loader(_system_dto(config_value="50")),
        )
        new_dto = _system_dto(config_value="100")
        ok = await cache.write_through("upload.max_file_size_mb", new_dto)
        assert ok is True
        r = await cache.get_or_load(
            "upload.max_file_size_mb", _async_loader(_system_dto(config_value="fresh")),
        )
        assert r.config_value == "100"
        # Invalidate.
        ok = await cache.invalidate("upload.max_file_size_mb")
        assert ok is True


# ── Domain disabled / Bypass ──────────────────────────────────────


class TestBypass:
    async def test_model_domain_disabled_bypasses_cache(
        self, fake_redis, monkeypatch
    ) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_cfg_enabled", False)
        cache = ModelConfigCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _model_dto()

        await cache.get_or_load(42, "chat", loader)
        await cache.get_or_load(42, "chat", loader)
        # Both calls reach the loader when domain is disabled.
        assert loader_calls["n"] == 2

    async def test_kb_domain_disabled_bypasses_cache(
        self, fake_redis, monkeypatch
    ) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_cfg_enabled", False)
        cache = KnowledgeConfigCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader_user():
            loader_calls["n"] += 1
            return _kb_dto(uid=42)

        async def loader_system():
            return _kb_dto(uid=None)

        await cache.get_or_load(42, loader_user, loader_system)
        await cache.get_or_load(42, loader_user, loader_system)
        assert loader_calls["n"] == 2

    async def test_backend_disabled_bypasses_cache(self) -> None:
        """No redis client → backend disabled → loader runs every time."""
        cache = ModelConfigCache(manager=CacheManager(backend=CacheBackend(redis_client=None)))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _model_dto()

        await cache.get_or_load(42, "chat", loader)
        await cache.get_or_load(42, "chat", loader)
        assert loader_calls["n"] == 2