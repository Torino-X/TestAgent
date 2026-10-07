"""Config Cache (设计文档 §11 + 提示词 §17-20).

覆盖 4 类配置：

  1. ``ModelConfigCache`` — Per-user 模型配置（含 capability 维度）
     Key:   ta:{env}:cache:v1:cfg:model:user:{uid}:cap:{capability}
     TTL:   30m / negative 60s

  2. ``KnowledgeConfigCache`` — Per-user + system fallback 双层
     Key (user):   ta:{env}:cache:v1:cfg:knowledge:user:{uid}
     Key (system): ta:{env}:cache:v1:cfg:knowledge:system
     TTL:   user 30m / system 10m / negative 60s

  3. ``ImageUnderstandingConfigCache`` — Per-user 视觉模型配置
     Key:   ta:{env}:cache:v1:cfg:image:user:{uid}
     TTL:   30m / negative 60s

  4. ``SystemConfigCache`` — Global system 配置 (upload limits, narrative ...)
     Key:   ta:{env}:cache:v1:cfg:system:{config_key}
     TTL:   10m / negative 60s

**安全约束（设计文档 §11.1）**:

  Redis 中只允许缓存 ``api_key_encrypted``（Fernet 密文）;禁止缓存解密后的 API Key.
  每个 worker 拿到 encrypted DTO 后本机 Fernet decrypt,构造 Provider. 这样:
    - Redis 泄漏风险降低（密文而非明文）
    - 多 worker 配置一致（都从 Redis 取同一密文 + 本机解密 key）
    - 取消"无 TTL 的解密 Provider" 永久 in-process 缓存

**Adapter 兼容**:

  现有 ``LLMConfigCache`` / ``ImageUnderstandingConfigCache`` (in-process
  dict 缓存 decrypted Provider) 保留为 Adapter — ``get_or_load`` 内部改为
  先调本 CacheManager 取密文 DTO,再本机 decrypt. 业务代码不需要改.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from app.cache.distributed_lock import CacheFillLock
from app.cache.key_builder import build_key
from app.cache.manager import CacheManager, get_cache_manager
from app.cache.metrics import cache_metrics
from app.cache.specs import CacheSpec

logger = logging.getLogger(__name__)


# ── DTOs ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelConfigDTO:
    """Cache-safe projection of a ``ModelConfig`` row.

    Only carries what's needed to construct an LLMConfigProvider AFTER
    local Fernet decrypt.  Contains ``api_key_encrypted`` (Fernet
    ciphertext) — NEVER the plaintext key (设计文档 §11.1).
    """

    public_id: str
    user_id: int
    capability_type: str
    config_name: str
    provider: str
    api_base_url: str
    api_key_encrypted: str
    model_name: str
    timeout_seconds: int
    enable_thinking: bool
    supports_vision: bool
    is_default: bool
    enabled: bool
    # Optional CE-01 fields — surfaced only when set.
    temperature: float | None = None
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    default_max_output_tokens: int | None = None
    embedding_dimension: int | None = None
    normalize_embeddings: bool | None = None
    rerank_instruction: str | None = None
    pre_rerank_limit: int | None = None
    score_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelConfigDTO":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class KnowledgeConfigDTO:
    """Cache-safe projection of a ``KnowledgeConfig`` row.

    ``user_id=None`` 表示 system-shared fallback row.
    """

    public_id: str
    user_id: int | None  # None → system fallback
    scope: str
    api_base_url: str
    api_key_encrypted: str | None
    api_key_masked: str | None
    default_knowledge_ids: list | None
    top_k: int
    similarity_threshold: float
    retrieve_strategy: int
    enable_rerank_model: bool
    rerank_model: str | None
    knowledge_graph: bool
    timeout_seconds: int
    enabled: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "KnowledgeConfigDTO":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ImageUnderstandingConfigDTO:
    """Cache-safe projection of an ``ImageUnderstandingConfig`` row."""

    public_id: str
    user_id: int
    api_base_url: str
    api_key_encrypted: str | None
    api_key_masked: str | None
    model_name: str
    timeout_seconds: int
    max_tokens: int | None
    enable_in_doc_parsing: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImageUnderstandingConfigDTO":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class SystemConfigDTO:
    """Cache-safe projection of a single ``SystemConfig`` row.

    ``SystemConfig`` is keyed by ``config_key`` (string).  Each key is
    cached independently so that a write to one key does not invalidate
    the others.
    """

    config_key: str
    config_value: str | None
    value_type: str
    description: str | None
    editable: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SystemConfigDTO":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ── CacheSpecs ──────────────────────────────────────────────────────


# Per 设计文档 §9 + §11.2 + 提示词 §17:
#   TTL 30m — config 变更频次低,但 role/status 不在 model DTO 里所以
#   不必 ≤ 60s. 30m 是合理的 stale window.
#   negative 60s — 防穿透.
#   enable_distributed_fill_lock=True — 每 LLM/KB 调用都查,热点 spec.
MODEL_CONFIG_SPEC = CacheSpec(
    domain="cfg",
    ttl_seconds=30 * 60,
    negative_ttl_seconds=60,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=True,
)


# Per 提示词 §18:
#   user 30m / system 10m / user negative 60s
#   双层 Key (user_id = None → system fallback)
KNOWLEDGE_CONFIG_USER_SPEC = CacheSpec(
    domain="cfg",
    ttl_seconds=30 * 60,
    negative_ttl_seconds=60,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=True,
)

KNOWLEDGE_CONFIG_SYSTEM_SPEC = CacheSpec(
    domain="cfg",
    ttl_seconds=10 * 60,
    negative_ttl_seconds=60,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=True,
)

# Per 提示词 §19: 镜像 KnowledgeConfig
IMAGE_UNDERSTANDING_SPEC = CacheSpec(
    domain="cfg",
    ttl_seconds=30 * 60,
    negative_ttl_seconds=60,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=True,
)

# Per 提示词 §20: 系统级 (global),TTL 10m
SYSTEM_CONFIG_SPEC = CacheSpec(
    domain="cfg",
    ttl_seconds=10 * 60,
    negative_ttl_seconds=60,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=False,  # 系统配置低频,无需分布式锁
)


# ── Key helpers ────────────────────────────────────────────────────


def model_config_key(user_id: int, capability: str) -> str:
    """Per 设计文档 §7 — cfg:model:user:{uid}:cap:{capability}."""
    return build_key("cfg", "model", "user", user_id, "cap", capability)


def knowledge_user_key(user_id: int) -> str:
    """Per 提示词 §18 — cfg:knowledge:user:{uid}."""
    return build_key("cfg", "knowledge", "user", user_id)


def knowledge_system_key() -> str:
    """Per 提示词 §18 — cfg:knowledge:system."""
    return build_key("cfg", "knowledge", "system")


def image_understanding_key(user_id: int) -> str:
    return build_key("cfg", "image", "user", user_id)


def system_config_key(config_key: str) -> str:
    return build_key("cfg", "system", config_key)


# ── Cache services ─────────────────────────────────────────────────


class _BaseConfigCache:
    """Common scaffolding: holds the CacheManager, builds fill_lock on demand."""

    def __init__(
        self,
        manager: CacheManager | None = None,
        *,
        fill_lock: CacheFillLock | None = None,
    ) -> None:
        self._mgr = manager if manager is not None else get_cache_manager()
        self._fill_lock = fill_lock

    @property
    def spec_domain(self) -> str:
        return "cfg"

    def _fill_lock_runtime(self) -> CacheFillLock | None:
        if self._fill_lock is not None:
            return self._fill_lock
        if not self._mgr.is_enabled():
            return None
        try:
            client = self._mgr._backend.client  # type: ignore[attr-defined]
        except RuntimeError:
            return None
        from app.core.config import get_settings

        ttl_ms = int(get_settings().cache_lock_ttl_ms)
        self._fill_lock = CacheFillLock(client, ttl_ms=ttl_ms)
        return self._fill_lock

    # ── Adapter pattern used by ALL concrete services ─────────────

    async def _get_or_load_dto(
        self,
        spec: CacheSpec,
        key: str,
        loader,
    ) -> Any | None:
        """Read-through / write-through DTO via CacheManager.

        ``loader()`` is called only on cache miss; it must return a
        DTO (or None for negative).  Negative returns write a short
        negative envelope.
        """

        async def _adapt_loader():
            result = await loader()
            if result is None:
                return None
            return result.to_dict() if hasattr(result, "to_dict") else result

        async def _adapt_cached(raw):
            from app.cache.manager import _CacheMiss as _CM

            if isinstance(raw, _CM):
                return None
            return raw  # 已为 dict — caller 负责 deserialize

        return await self._mgr.get_or_load(
            spec,
            key,
            _adapt_loader,
            fill_lock=self._fill_lock_runtime(),
            cached_adapter=_adapt_cached,
        )

    async def _write_through_dto(
        self,
        spec: CacheSpec,
        key: str,
        dto: Any | None,
    ) -> bool:
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(spec.domain):
            cache_metrics.record(
                domain=spec.domain, operation="set", result="bypass",
            )
            return False
        if dto is None:
            return await self._mgr.delete(spec, key)
        return await self._mgr.set(spec, key, dto.to_dict())

    async def _invalidate(self, spec: CacheSpec, key: str) -> bool:
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(spec.domain):
            return False
        return await self._mgr.delete(spec, key)


class ModelConfigCache(_BaseConfigCache):
    """Per-user per-capability ModelConfig cache."""

    @property
    def spec(self) -> CacheSpec:
        return MODEL_CONFIG_SPEC

    def key(self, user_id: int, capability: str) -> str:
        return model_config_key(user_id, capability)

    async def get_or_load(
        self,
        user_id: int,
        capability: str,
        loader,
    ) -> ModelConfigDTO | None:
        """Cache-aside read.

        ``loader()`` is invoked on miss; it must return
        :class:`ModelConfigDTO` (or ``None`` for negative).
        """
        key = self.key(user_id, capability)
        cached = await self._get_or_load_dto(self.spec, key, loader)
        if cached is None:
            return None
        if isinstance(cached, dict):
            return ModelConfigDTO.from_dict(cached)
        return cached

    async def write_through(
        self,
        user_id: int,
        capability: str,
        dto: ModelConfigDTO | None,
    ) -> bool:
        return await self._write_through_dto(
            self.spec, self.key(user_id, capability), dto,
        )

    async def invalidate(self, user_id: int, capability: str) -> bool:
        return await self._invalidate(self.spec, self.key(user_id, capability))


class KnowledgeConfigCache(_BaseConfigCache):
    """Per-user + system fallback dual-layer KnowledgeConfig cache."""

    @property
    def user_spec(self) -> CacheSpec:
        return KNOWLEDGE_CONFIG_USER_SPEC

    @property
    def system_spec(self) -> CacheSpec:
        return KNOWLEDGE_CONFIG_SYSTEM_SPEC

    def user_key(self, user_id: int) -> str:
        return knowledge_user_key(user_id)

    def system_key(self) -> str:
        return knowledge_system_key()

    async def get_or_load(
        self,
        user_id: int,
        loader_user,
        loader_system,
    ) -> KnowledgeConfigDTO | None:
        """Two-layer cache-aside.

        1. user cache hit  → return
        2. user negative   → fall through to system cache
        3. user cache miss → ``loader_user()`` invoked, write-through;
                              on its None, fall through to system.
        4. system cache hit  → return
        5. system negative / miss → ``loader_system()`` invoked, write-through
        """
        from app.cache.manager import _CacheMiss as _CM

        # Layer 1: user cache.
        user_cached = await self._mgr.get(
            self.user_spec, self.user_key(user_id),
        )
        if isinstance(user_cached, _CM):
            if user_cached.reason == "negative":
                # User has no row → fall through to system layer.
                pass
            else:
                # True miss → call loader_user.
                dto = await loader_user()
                if dto is not None:
                    await self._mgr.set(
                        self.user_spec,
                        self.user_key(user_id),
                        dto.to_dict(),
                    )
                    return dto
                else:
                    await self._mgr.set_negative(
                        self.user_spec, self.user_key(user_id),
                    )
                    # Fall through to system.
        elif isinstance(user_cached, dict):
            return KnowledgeConfigDTO.from_dict(user_cached)
        # Fall through (user negative OR loader_user returned None)

        # Layer 2: system cache.
        sys_cached = await self._mgr.get(self.system_spec, self.system_key())
        if isinstance(sys_cached, _CM):
            if sys_cached.reason == "negative":
                return None
            # True miss → loader_system.
            sys_dto = await loader_system()
            if sys_dto is None:
                await self._mgr.set_negative(self.system_spec, self.system_key())
                return None
            await self._mgr.set(
                self.system_spec, self.system_key(), sys_dto.to_dict(),
            )
            return sys_dto
        elif isinstance(sys_cached, dict):
            return KnowledgeConfigDTO.from_dict(sys_cached)
        return None

    async def invalidate_user(self, user_id: int) -> bool:
        return await self._invalidate(
            self.user_spec, self.user_key(user_id),
        )

    async def invalidate_system(self) -> bool:
        return await self._invalidate(self.system_spec, self.system_key())


class ImageUnderstandingConfigCache(_BaseConfigCache):
    """Per-user ImageUnderstandingConfig cache."""

    @property
    def spec(self) -> CacheSpec:
        return IMAGE_UNDERSTANDING_SPEC

    def key(self, user_id: int) -> str:
        return image_understanding_key(user_id)

    async def get_or_load(
        self,
        user_id: int,
        loader,
    ) -> ImageUnderstandingConfigDTO | None:
        key = self.key(user_id)
        cached = await self._get_or_load_dto(self.spec, key, loader)
        if cached is None:
            return None
        if isinstance(cached, dict):
            return ImageUnderstandingConfigDTO.from_dict(cached)
        return cached

    async def write_through(
        self,
        user_id: int,
        dto: ImageUnderstandingConfigDTO | None,
    ) -> bool:
        return await self._write_through_dto(self.spec, self.key(user_id), dto)

    async def invalidate(self, user_id: int) -> bool:
        return await self._invalidate(self.spec, self.key(user_id))


class SystemConfigCache(_BaseConfigCache):
    """Global SystemConfig cache (keyed by ``config_key``)."""

    @property
    def spec(self) -> CacheSpec:
        return SYSTEM_CONFIG_SPEC

    def key(self, config_key: str) -> str:
        return system_config_key(config_key)

    async def get_or_load(
        self,
        config_key: str,
        loader,
    ) -> SystemConfigDTO | None:
        key = self.key(config_key)
        cached = await self._get_or_load_dto(self.spec, key, loader)
        if cached is None:
            return None
        if isinstance(cached, dict):
            return SystemConfigDTO.from_dict(cached)
        return cached

    async def write_through(
        self,
        config_key: str,
        dto: SystemConfigDTO | None,
    ) -> bool:
        return await self._write_through_dto(self.spec, self.key(config_key), dto)

    async def invalidate(self, config_key: str) -> bool:
        return await self._invalidate(self.spec, self.key(config_key))


# ── Module-level singletons ─────────────────────────────────────────


_model_cache: ModelConfigCache | None = None
_kb_cache: KnowledgeConfigCache | None = None
_iu_cache: ImageUnderstandingConfigCache | None = None
_system_cache: SystemConfigCache | None = None


def get_model_config_cache() -> ModelConfigCache:
    global _model_cache
    if _model_cache is None:
        _model_cache = ModelConfigCache()
    return _model_cache


def set_model_config_cache(c: ModelConfigCache | None) -> None:
    global _model_cache
    _model_cache = c


def get_knowledge_config_cache() -> KnowledgeConfigCache:
    global _kb_cache
    if _kb_cache is None:
        _kb_cache = KnowledgeConfigCache()
    return _kb_cache


def set_knowledge_config_cache(c: KnowledgeConfigCache | None) -> None:
    global _kb_cache
    _kb_cache = c


def get_image_understanding_config_cache() -> ImageUnderstandingConfigCache:
    global _iu_cache
    if _iu_cache is None:
        _iu_cache = ImageUnderstandingConfigCache()
    return _iu_cache


def set_image_understanding_config_cache(c: ImageUnderstandingConfigCache | None) -> None:
    global _iu_cache
    _iu_cache = c


def get_system_config_cache() -> SystemConfigCache:
    global _system_cache
    if _system_cache is None:
        _system_cache = SystemConfigCache()
    return _system_cache


def set_system_config_cache(c: SystemConfigCache | None) -> None:
    global _system_cache
    _system_cache = c


__all__ = [
    "IMAGE_UNDERSTANDING_SPEC",
    "KNOWLEDGE_CONFIG_SYSTEM_SPEC",
    "KNOWLEDGE_CONFIG_USER_SPEC",
    "MODEL_CONFIG_SPEC",
    "SYSTEM_CONFIG_SPEC",
    "ImageUnderstandingConfigCache",
    "ImageUnderstandingConfigDTO",
    "KnowledgeConfigCache",
    "KnowledgeConfigDTO",
    "ModelConfigCache",
    "ModelConfigDTO",
    "SystemConfigCache",
    "SystemConfigDTO",
    "get_image_understanding_config_cache",
    "get_knowledge_config_cache",
    "get_model_config_cache",
    "get_system_config_cache",
    "image_understanding_key",
    "knowledge_system_key",
    "knowledge_user_key",
    "model_config_key",
    "set_image_understanding_config_cache",
    "set_knowledge_config_cache",
    "set_model_config_cache",
    "set_system_config_cache",
    "system_config_key",
]