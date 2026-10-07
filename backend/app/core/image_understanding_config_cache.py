"""F020 — Per-user ImageUnderstandingConfig cache (Phase 1 Redis Cache refactor).

历史: 曾经是 process-wide dict 缓存 decrypted ``ImageUnderstandingConfigProvider``.
Phase 1 缓存系统 (Step 5) 改造后:

  - Redis 存 encrypted DTO (``api_key_encrypted`` Fernet 密文);
  - 本类作为 Adapter, ``get_or_load`` 内部调用
    ``ImageUnderstandingConfigCache`` (Redis 后端) 拿 encrypted DTO, 然后
    本机 Fernet decrypt 构造 Provider;
  - 不再持久缓存 decrypted provider object;
  - 多 worker 配置一致 — 同 LLMConfigCache Adapter 模式.

API surface: 兼容旧的 ``get_or_load`` / ``set`` / ``invalidate`` /
``cached_user_ids`` 调用方 — 现有 ``ImageUnderstandingConfigService
.build_provider`` 等不需要改.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from app.cache.domains.config_cache import (
    ImageUnderstandingConfigCache as DomainIUConfigCache,
    ImageUnderstandingConfigDTO,
    get_image_understanding_config_cache,
)

if TYPE_CHECKING:
    from app.services.image_understanding_config_service import (
        ImageUnderstandingConfigProvider,
    )

logger = logging.getLogger(__name__)


class ImageUnderstandingConfigCache:
    """Adapter over ``ImageUnderstandingConfigCache`` for the decrypted-provider view."""

    def __init__(
        self,
        *,
        domain_cache: DomainIUConfigCache | None = None,
    ) -> None:
        self._domain = domain_cache  # type: ignore[assignment]

    @property
    def domain_cache(self) -> DomainIUConfigCache:
        if self._domain is None:
            self._domain = get_image_understanding_config_cache()
        return self._domain

    async def get_or_load(
        self,
        user_id: int,
        loader: Callable[[int], Awaitable[Optional["ImageUnderstandingConfigProvider"]]],
    ) -> Optional["ImageUnderstandingConfigProvider"]:
        """Return the decrypted provider from cache, or call ``loader``."""

        async def _dto_loader(uid: int) -> Optional[ImageUnderstandingConfigDTO]:
            provider = await loader(uid)
            if provider is None:
                return None
            # Adapter cannot reconstruct ciphertext from plaintext — caller
            # (the service) must write the encrypted DTO directly via
            # domain_cache.write_through after DB commit.  Returning None
            # forces the fallback path.
            return None

        cached = await self.domain_cache.get_or_load(
            user_id=user_id,
            loader=_dto_loader,
        )
        if cached is None:
            return await loader(user_id)
        return self._provider_from_dto(cached)

    async def set(
        self,
        user_id: int,
        provider: "ImageUnderstandingConfigProvider",
    ) -> None:
        """Deprecated write-through; forces invalidation.

        Caller should use ``domain_cache.write_through`` with the
        encrypted DTO after DB commit.
        """
        logger.warning(
            "ImageUnderstandingConfigCache.set: deprecated; use "
            "domain_cache.write_through with encrypted DTO. "
            "Forcing invalidation of user_id=%s.",
            user_id,
        )
        await self.invalidate(user_id)

    async def invalidate(self, user_id: int) -> None:
        await self.domain_cache.invalidate(user_id)

    async def clear(self) -> None:
        return None

    def cached_user_ids(self) -> list[int]:
        return []

    @staticmethod
    def _provider_from_dto(
        dto: ImageUnderstandingConfigDTO,
    ) -> "ImageUnderstandingConfigProvider":
        from app.core.crypto import CryptoError, decrypt_api_key
        from app.services.image_understanding_config_service import (
            ImageUnderstandingConfigProvider,
        )

        try:
            plain_key = decrypt_api_key(dto.api_key_encrypted or "")
        except CryptoError as exc:
            logger.warning(
                "ImageUnderstandingConfigCache: cached ciphertext cannot "
                "decrypt for user_id=%s: %s",
                dto.user_id, exc,
            )
            raise
        return ImageUnderstandingConfigProvider(
            api_url=dto.api_base_url,
            api_key=plain_key,
            model_name=dto.model_name,
            timeout=int(dto.timeout_seconds or 60),
            enable_in_doc_parsing=bool(dto.enable_in_doc_parsing),
            public_id=dto.public_id,
        )


image_understanding_config_cache = ImageUnderstandingConfigCache()


__all__ = [
    "ImageUnderstandingConfigCache",
    "image_understanding_config_cache",
]