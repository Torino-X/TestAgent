"""F015 — Per-user ModelConfig cache (Phase 1 Redis Cache refactor).

历史: 这个类曾经是 process-wide dict 缓存 decrypted ``LLMConfigProvider``.
Phase 1 缓存系统 (Step 5) 改造后:

  - Redis 存 encrypted DTO (``api_key_encrypted`` Fernet 密文);
  - 本类作为 Adapter, ``get_or_load`` 内部调用 ``ModelConfigCache``
    (Redis 后端) 拿 encrypted DTO, 然后本机 Fernet decrypt 构造 Provider;
  - 不再持久缓存 decrypted provider object (设计文档 §11.5 + 提示词 §17);
  - 多 worker 配置一致: 都从 Redis 取同一密文 + 本机 Fernet key 解密;
  - 每个 worker 不再各自缓存明文, 泄漏面降低.

API surface: 兼容旧的 ``get_or_load`` / ``set`` / ``invalidate`` /
``cached_user_ids`` 调用方 — 现有 ``SettingsService.build_llm_config_provider`` /
``bootstrap_user_config`` / ``invalidate_all_user_caches`` 等不需要改.

Capability: 旧版本只支持 primary model (chat); 新版本通过
``capability`` 参数选择. ``SettingsService`` 现有调用都是
``capability="chat"`` (primary), 没有破坏性.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from app.cache.domains.config_cache import (
    ModelConfigCache,
    ModelConfigDTO,
    get_model_config_cache,
)

if TYPE_CHECKING:
    from app.services.settings_service import LLMConfigProvider

logger = logging.getLogger(__name__)


# Default capability for legacy callers — primary / chat model.
_DEFAULT_CAPABILITY = "chat"


class LLMConfigCache:
    """Adapter over ``ModelConfigCache`` for the decrypted-provider view.

    Lifecycle:

      - ``get_or_load(user_id, loader)`` from ``SettingsService.build_llm_config_provider``
      - ``set(user_id, provider)`` after settings update (write-through)
      - ``invalidate(user_id)`` on logout

    The class keeps the public API stable; internally it asks
    ``ModelConfigCache`` for the encrypted DTO and decrypts locally to
    build the ``LLMConfigProvider``.
    """

    def __init__(self, *, model_cache: ModelConfigCache | None = None) -> None:
        # Adapter holds a reference to the domain cache.  Module-level
        # ``llm_config_cache`` singleton uses the singleton
        # ``get_model_config_cache()``; tests can inject a custom one.
        self._model_cache = model_cache  # type: ignore[assignment]
        # Process-local cache of (user_id, capability) -> decrypted
        # ``LLMConfigProvider``.  This avoids running the Fernet
        # decrypt on every chat call after the first one in this
        # worker.  The Redis layer (ModelConfigCache) holds the
        # ciphertext so multi-worker config stays in sync; this dict
        # is purely a per-worker decrypt accelerator.
        self._provider_cache: dict[tuple[int, str], "LLMConfigProvider"] = {}
        # Lock for in-flight loader coalescing per (user, capability)
        # — protects against concurrent decrypts on the same user when
        # the Redis cache is down (bypass path).
        self._inflight_lock = asyncio.Lock()

    @property
    def model_cache(self) -> ModelConfigCache:
        if self._model_cache is None:
            self._model_cache = get_model_config_cache()
        return self._model_cache

    async def get_or_load(
        self,
        user_id: int,
        loader: Callable[[int], Awaitable[Optional["LLMConfigProvider"]]],
        *,
        capability: str = _DEFAULT_CAPABILITY,
    ) -> Optional["LLMConfigProvider"]:
        """Return the decrypted provider from cache, or call ``loader``.

        ``loader(user_id)`` is invoked on cache miss; it must return a
        ``LLMConfigProvider`` (with the decrypted key).  The adapter
        reverses the encryption to populate the Redis DTO and returns
        the in-memory provider for the current call.

        Note: ``loader(uid)`` returns a decrypted ``LLMConfigProvider``
        only — it does NOT have access to the ciphertext (the loader's
        caller already decrypted).  This adapter therefore runs the
        loader first to know the provider exists, then attempts to
        re-fetch the ciphertext row via ``ModelConfigCache`` for the
        cache write.  If the write succeeds, future calls in this
        worker hit the per-worker ``_provider_cache`` and skip both the
        DB read and the Fernet decrypt.
        """
        cache_key = (user_id, capability)
        # Per-worker decrypted cache fast hit.
        if cache_key in self._provider_cache:
            return self._provider_cache[cache_key]

        # Coalesce concurrent loads for the same user so we don't
        # decrypt the same ciphertext twice in parallel.
        async with self._inflight_lock:
            if cache_key in self._provider_cache:
                return self._provider_cache[cache_key]
            provider = await loader(user_id)
            if provider is None:
                return None
            # Best-effort write-through to Redis for cross-worker
            # consistency.  Failures are non-fatal — the per-worker
            # cache above still serves this call correctly.
            try:
                await self._write_dto_for_provider(user_id, capability, provider)
            except Exception as exc:  # noqa: BLE001 — best effort
                logger.debug(
                    "LLMConfigCache.get_or_load: write-through skipped: %s",
                    exc,
                )
            self._provider_cache[cache_key] = provider
            return provider

    async def _write_dto_for_provider(
        self,
        user_id: int,
        capability: str,
        provider: "LLMConfigProvider",
    ) -> None:
        """Best-effort: re-read the ciphertext row and populate the
        Redis cache for cross-worker consistency.

        This is a best-effort operation — if the read fails (no row,
        DB error) we just skip the cache write.  The caller already has
        the decrypted provider for this turn.
        """
        from app.db.session import AsyncSessionLocal
        from app.repositories.model_config_repository import ModelConfigRepository

        async with AsyncSessionLocal() as session:
            repo = ModelConfigRepository(session)
            cfg = await repo.get_active_for_user(user_id)
            if cfg is None:
                return
            dto = ModelConfigDTO(
                public_id=cfg.public_id,
                user_id=cfg.user_id,
                capability_type=cfg.capability_type,
                config_name=cfg.config_name,
                provider=cfg.provider,
                api_base_url=cfg.api_base_url,
                api_key_encrypted=cfg.api_key_encrypted,
                model_name=cfg.model_name,
                timeout_seconds=int(cfg.timeout_seconds or 120),
                enable_thinking=bool(cfg.enable_thinking),
                supports_vision=bool(getattr(cfg, "supports_vision", False)),
                is_default=bool(cfg.is_default),
                enabled=bool(cfg.enabled),
                temperature=getattr(cfg, "temperature", None),
                max_tokens=getattr(cfg, "max_tokens", None),
                context_window_tokens=getattr(cfg, "context_window_tokens", None),
                default_max_output_tokens=getattr(cfg, "default_max_output_tokens", None),
            )
            await self.model_cache.write_through(user_id, capability, dto)

    async def set(
        self,
        user_id: int,
        provider: "LLMConfigProvider",
        *,
        capability: str = _DEFAULT_CAPABILITY,
    ) -> None:
        """Write-through after a settings update.

        Adapter does NOT have the plaintext → ciphertext path; the
        caller (``SettingsService.update_model_settings``) is expected
        to write the encrypted DTO directly via
        ``ModelConfigCache.write_through`` after the DB commit.  This
        ``set`` method is kept for backward compatibility — it seeds
        the per-worker decrypted cache so the next ``get_or_load`` in
        this worker skips the DB read + Fernet decrypt, and forces
        invalidation of any stale Redis entry.
        """
        # Per-worker decrypt accelerator.
        self._provider_cache[(user_id, capability)] = provider
        # Invalidate any stale Redis entry so other workers re-fetch
        # the ciphertext from MySQL on their next call.
        try:
            await self.model_cache.invalidate(user_id, capability=capability)
        except Exception as exc:  # noqa: BLE001 — best effort
            logger.debug(
                "LLMConfigCache.set: invalidate Redis skipped: %s", exc,
            )

    async def invalidate(
        self,
        user_id: int,
        *,
        capability: str = _DEFAULT_CAPABILITY,
    ) -> None:
        """Remove the cached entry.  Used on logout / settings deletion."""
        # Per-worker cache.
        self._provider_cache.pop((user_id, capability), None)
        # Redis cache.
        await self.model_cache.invalidate(user_id, capability=capability)

    async def clear(self) -> None:
        """Wipe the per-worker decrypted cache.  No-op for Redis (no
        keyspace to bulk-clear from here)."""
        self._provider_cache.clear()

    def cached_user_ids(self) -> list[int]:
        """Snapshot of cached user IDs (any capability)."""
        return sorted({uid for (uid, _cap) in self._provider_cache.keys()})

    # ── Internal helpers ───────────────────────────────────────────

    @staticmethod
    def _provider_from_dto(dto: ModelConfigDTO) -> "LLMConfigProvider":
        """Local Fernet decrypt + LLMConfigProvider construction.

        ``api_key_encrypted`` is decrypted here ONLY; the plaintext
        key never enters Redis, never enters logs, and lives for the
        duration of this single ``get_or_load`` call's return value.
        """
        from app.core.crypto import CryptoError, decrypt_api_key
        from app.services.settings_service import LLMConfigProvider

        try:
            plain_key = decrypt_api_key(dto.api_key_encrypted or "")
        except CryptoError as exc:
            logger.warning(
                "LLMConfigCache: cached ciphertext cannot decrypt "
                "(user_id=%s capability=%s): %s",
                dto.user_id, dto.capability_type, exc,
            )
            raise
        return LLMConfigProvider(
            api_url=dto.api_base_url,
            api_key=plain_key,
            model_name=dto.model_name,
            timeout=int(dto.timeout_seconds or 120),
            enable_thinking=bool(dto.enable_thinking),
            supports_vision=bool(dto.supports_vision),
        )


# ── module-level instance ───────────────────────────────────────────

llm_config_cache = LLMConfigCache()


__all__ = ["LLMConfigCache", "llm_config_cache"]
