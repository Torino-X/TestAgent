"""Settings service — model, knowledge base, upload config.

F015 — model configuration is now per-user and DB-only.  The legacy
``_env_config_provider`` fallback has been removed: if a user has no
``model_configs`` row, ``build_llm_config_provider`` returns an
``LLMNotConfiguredMarker`` (a sentinel provider) so the LLM call
produces a clear, user-actionable error directing them to Settings.

The decrypted provider is cached in :class:`LLMConfigCache` keyed by
``users.id`` (integer).  Login calls ``bootstrap_user_config`` to warm
the cache; logout calls ``llm_config_cache.invalidate``; LLM calls
fall through ``get_or_load`` which lazily populates on miss.

API keys in ``model_configs.api_key_encrypted`` are encrypted with Fernet
(keyed off ``settings.SECRET_KEY``); only the masked form is returned to
clients.  Connection tests exercise the real LLMClient against the
resolved provider.

F021-revised — knowledge base and image-understanding configs share the
same per-user contract (DB-backed, Fernet-encrypted, masked-on-the-wire,
in-process cache, bootstrap-on-login, invalidate-on-logout).  KB and
image-understanding services delegate to this service for their
read / save / test / bootstrap / cache-invalidate paths so the three
configs move in lock-step — login warms all three, logout clears all
three.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, decrypt_api_key, encrypt_api_key, mask_api_key
from app.core.llm_config_cache import llm_config_cache
from app.core.llm_not_configured import LLMNotConfiguredMarker
from app.models.config import ModelConfig, SystemConfig
from app.repositories.model_config_repository import ModelConfigRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)


def _context_window_tokens_from_payload(data: dict[str, Any]) -> int | None:
    """Resolve Settings payload window fields into DB tokens.

    The product-facing field is ``context_window_k``; ``context_window_tokens``
    remains accepted for older clients and internal capability forms.
    """
    if "context_window_k" in data:
        raw_k = data.get("context_window_k")
        if raw_k in (None, ""):
            return None
        return int(raw_k) * 1000
    raw_tokens = data.get("context_window_tokens")
    if raw_tokens in (None, ""):
        return None
    return int(raw_tokens)


def _context_window_k_from_tokens(tokens: int | None) -> int | None:
    if tokens is None:
        return None
    return max(int(round(int(tokens) / 1000)), 1)


@dataclass
class LLMConfigProvider:
    """Adapter exposing the attributes LLMClient expects.

    Returned by ``SettingsService.build_llm_config_provider`` so that
    LLMClient can be constructed with ``LLMClient(config_provider=...)``.
    """

    api_url: str
    api_key: str
    model_name: str
    timeout: int
    enable_thinking: bool = False
    supports_vision: bool = False

    def get_effective_api_key(self) -> str:
        return self.api_key


class SettingsService:
    """Read/write system settings — model config is real (DB + Fernet)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._model_repo = ModelConfigRepository(session)

    # ── System config helpers (unchanged — real DB) ──────────────────

    async def _get_config(self, key: str, default: str = "") -> str:
        from sqlalchemy import select as _select

        result = await self._session.execute(
            _select(SystemConfig).where(SystemConfig.config_key == key)
        )
        row = result.scalar_one_or_none()
        return row.config_value if row else default

    async def _upsert_config(
        self,
        key: str,
        value: str,
        value_type: str,
        description: str,
        editable: bool = True,
    ) -> None:
        from sqlalchemy import select as _select

        result = await self._session.execute(
            _select(SystemConfig).where(SystemConfig.config_key == key)
        )
        row = result.scalar_one_or_none()
        if row:
            row.config_value = value
            row.updated_at = utcnow()
        else:
            now = utcnow()
            self._session.add(
                SystemConfig(
                    config_key=key,
                    config_value=value,
                    value_type=value_type,
                    description=description,
                    editable=bool(editable),
                    created_at=now,
                    updated_at=now,
                )
            )

    # ── Model config (PER-USER, DB + Fernet) ─────────────────────────

    async def get_model_settings(self, user_id: str) -> dict:
        """Return the current user's model config for the Settings page.

        ``user_id`` is the JWT subject (public_id string); we resolve
        it to the integer DB id internally.
        """
        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            return self._unconfigured_public()
        cfg = await self._model_repo.get_active_for_user(internal_id)
        if cfg is None:
            return self._unconfigured_public()
        return self._model_to_public(cfg)

    async def update_model_settings(self, data: dict, user_id: str) -> dict:
        """Persist the current user's model config. Empty ``api_key`` keeps existing."""
        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            from app.core.exceptions import ValidationError
            raise ValidationError("无法识别当前用户，请重新登录")

        api_base_url = (data.get("api_base_url") or "").strip()
        model_name = (data.get("model_name") or "").strip()
        provider = (data.get("provider") or "openai-compatible").strip()
        timeout_seconds = int(data.get("timeout_seconds") or 120)
        enable_thinking = bool(data.get("enable_thinking", False))
        supports_vision = bool(data.get("supports_vision", False))
        temperature = data.get("temperature")
        max_tokens = data.get("max_tokens")
        # CE-01: 模型能力类型与能力元数据
        capability_type = (data.get("capability_type") or "chat").strip()
        context_window_tokens = _context_window_tokens_from_payload(data)
        default_max_output_tokens = data.get("default_max_output_tokens")
        embedding_dimension = data.get("embedding_dimension")
        normalize_embeddings = data.get("normalize_embeddings")

        if not api_base_url:
            from app.core.exceptions import ValidationError
            raise ValidationError("API Base URL 不能为空")
        if not model_name:
            from app.core.exceptions import ValidationError
            raise ValidationError("模型名称不能为空")
        if capability_type not in (
            "chat",
            "reasoning",
            "embedding",
            "reranker",
            "compression",
            "memory_extraction",
        ):
            from app.core.exceptions import ValidationError
            raise ValidationError(f"不支持的模型能力类型: {capability_type}")

        plain_api_key = (data.get("api_key") or "").strip()
        encrypted_api_key = encrypt_api_key(plain_api_key) if plain_api_key else ""

        # Reuse existing public_id when possible to keep stable references.
        existing = await self._model_repo.get_active_for_user(internal_id)
        public_id = existing.public_id if existing else generate_public_id("model_config")

        saved = await self._model_repo.upsert_for_user(
            user_id=internal_id,
            public_id=public_id,
            config_name=data.get("config_name") or "我的模型配置",
            provider=provider,
            api_base_url=api_base_url,
            api_key_encrypted=encrypted_api_key,
            model_name=model_name,
            timeout_seconds=timeout_seconds,
            enable_thinking=enable_thinking,
            supports_vision=supports_vision,
            temperature=float(temperature) if temperature is not None else None,
            max_tokens=int(max_tokens) if max_tokens is not None else None,
            capability_type=capability_type,
            context_window_tokens=context_window_tokens,
            context_window_tokens_set=(
                "context_window_k" in data or "context_window_tokens" in data
            ),
            default_max_output_tokens=int(default_max_output_tokens) if default_max_output_tokens is not None else None,
            embedding_dimension=int(embedding_dimension) if embedding_dimension is not None else None,
            normalize_embeddings=bool(normalize_embeddings) if normalize_embeddings is not None else None,
        )
        await self._session.flush()
        await self._session.refresh(saved)

        # Phase 1 (Step 5): write the encrypted DTO into Redis so the
        # next LLM call hits the cache instead of the DB.  Redis stores
        # the Fernet ciphertext (never plaintext); each worker decrypts
        # locally when constructing the LLMConfigProvider.
        # P0 收口:用 ``register_after_commit`` 挂 hook;rollback 不触发.
        try:
            from app.cache.domains.config_cache import (
                ModelConfigDTO,
                get_model_config_cache,
            )
            from app.db.sync import register_after_commit

            _dto = ModelConfigDTO(
                public_id=saved.public_id,
                user_id=saved.user_id,
                capability_type=getattr(saved, "capability_type", None) or "chat",
                config_name=saved.config_name,
                provider=saved.provider,
                api_base_url=saved.api_base_url,
                api_key_encrypted=saved.api_key_encrypted,
                model_name=saved.model_name,
                timeout_seconds=int(saved.timeout_seconds or 120),
                enable_thinking=bool(saved.enable_thinking),
                supports_vision=bool(saved.supports_vision),
                is_default=bool(saved.is_default),
                enabled=bool(saved.enabled),
                temperature=saved.temperature,
                max_tokens=saved.max_tokens,
                context_window_tokens=getattr(saved, "context_window_tokens", None),
                default_max_output_tokens=getattr(
                    saved, "default_max_output_tokens", None
                ),
                embedding_dimension=getattr(saved, "embedding_dimension", None),
                normalize_embeddings=getattr(saved, "normalize_embeddings", None),
            )
            _user_id_for_cache = internal_id

            async def _do_write_through(_session):
                try:
                    ok = await get_model_config_cache().write_through(
                        user_id=_user_id_for_cache,
                        capability=_dto.capability_type,
                        dto=_dto,
                    )
                    logger.info(
                        "SettingsService.update_model_settings: 保存配置 | user_id=%d "
                        "| capability=%s | model=%s | base=%s | "
                        "timeout_seconds=%d | cache_write=%s",
                        _user_id_for_cache, _dto.capability_type, saved.model_name,
                        (saved.api_base_url or "")[:40],
                        int(saved.timeout_seconds or 120),
                        "ok" if ok else "bypass",
                    )
                except Exception as cache_exc:  # noqa: BLE001
                    logger.warning(
                        "SettingsService.update_model_settings: model config cache "
                        "write-through failed | user_id=%d | err=%s",
                        _user_id_for_cache, cache_exc,
                    )

            register_after_commit(self._session, _do_write_through)
        except Exception as exc:  # noqa: BLE001 — cache failure must not fail save
            logger.warning(
                "update_model_settings: cache write-through failed for "
                "user_id=%s capability=%s: %s",
                internal_id, saved.capability_type, exc,
            )

        return self._model_to_public(saved)

    async def test_model_connection(
        self,
        user_id: str,
        override: dict | None = None,
    ) -> dict:
        """Run LLMClient.health_check against the resolved provider.

        ``override`` (when supplied) is the raw dict from the request
        body — typically ``ModelSettingsRequest.model_dump()``.  When
        it carries an ``api_base_url`` or ``model_name`` we build a
        transient :class:`LLMConfigProvider` from those values and
        **bypass the DB** — this implements the "test before save"
        UX.  When ``override`` is ``None`` (or empty) we fall back to
        the user's saved row in ``model_configs`` so old callers and
        the "saved → test" path continue to work unchanged.
        """
        logger.info(
            "SettingsService.test_model_connection: 入口 | user_id=%r | override_keys=%s",
            user_id,
            list(override.keys()) if isinstance(override, dict) else None,
        )
        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            logger.warning(
                "SettingsService.test_model_connection: 无法识别用户 | user_id=%r",
                user_id,
            )
            return {
                "success": False,
                "message": "无法识别当前用户，请重新登录",
                "latency_ms": None,
            }

        provider: LLMConfigProvider | None = None
        # ── 1) Body override path: build provider from request body ─
        # When the override carries api_base_url or model_name we honour
        # those values. If api_key is empty/missing in the override we
        # transparently fall back to the user's saved key in the DB —
        # otherwise "test connection" without retyping the key would
        # always fail with "API Key missing", which the frontend masks
        # its value so it has no way to distinguish "empty" from
        # "intentionally kept the saved one".
        if override and any(
            (override.get(k) or "").strip() for k in ("api_base_url", "model_name")
        ):
            api_key = (override.get("api_key") or "").strip()
            if not api_key:
                # Pull saved key from DB to merge with the override.
                saved_provider = await self.build_llm_config_provider(
                    user_id=internal_id,
                )
                if isinstance(saved_provider, LLMNotConfiguredMarker):
                    saved_provider = None
                api_key = getattr(saved_provider, "api_key", "") or ""
            provider = LLMConfigProvider(
                api_url=(override.get("api_base_url") or "").strip(),
                api_key=api_key,
                model_name=(override.get("model_name") or "").strip(),
                timeout=int(override.get("timeout_seconds") or 120),
                enable_thinking=False,
            )
            logger.info(
                "SettingsService.test_model_connection: 使用body覆盖配置 | user_id=%d | model=%s",
                internal_id, provider.model_name,
            )
        else:
            # ── 2) DB fallback path (legacy + "saved → test") ──────
            provider = await self.build_llm_config_provider(user_id=internal_id)
            if isinstance(provider, LLMNotConfiguredMarker):
                logger.info(
                    "SettingsService.test_model_connection: 用户无配置 | user_id=%d",
                    internal_id,
                )
                return {
                    "success": False,
                    "message": "尚未配置模型，请先在'设置'页面配置 API 地址 / Key / 模型名",
                    "latency_ms": None,
                }
            logger.info(
                "SettingsService.test_model_connection: 使用DB配置 | user_id=%d | model=%s",
                internal_id, getattr(provider, "model_name", "?"),
            )

        if not provider.api_url or not provider.api_key or not provider.model_name:
            return {
                "success": False,
                "message": "模型未配置完整（API 地址 / Key / 模型名缺失）",
                "latency_ms": None,
            }

        from app.integrations.llm_client import LLMClient

        client = LLMClient(config_provider=provider)
        start = time.monotonic()
        try:
            ok = await client.health_check()
        except Exception as exc:
            return {
                "success": False,
                "message": f"连接测试异常: {exc}",
                "latency_ms": int((time.monotonic() - start) * 1000),
            }
        elapsed_ms = int((time.monotonic() - start) * 1000)
        detail = getattr(client, "_last_health_error", None)
        return {
            "success": bool(ok),
            "message": "连接测试成功" if ok else f"连接测试失败: {detail or '未知错误'}",
            "latency_ms": elapsed_ms,
        }

    async def bootstrap_user_config(self, user_id: int) -> Optional[LLMConfigProvider]:
        """Called from ``AuthService.login`` to warm the per-user cache.

        Returns the populated provider, or ``None`` if the user has no
        config (caller will not raise — the next LLM call will surface
        the "not configured" error to the frontend, which will route
        the user to Settings).
        """
        async def _loader(uid: int) -> Optional[LLMConfigProvider]:
            cfg = await self._model_repo.get_active_for_user(uid)
            if cfg is None:
                return None
            try:
                api_key = decrypt_api_key(cfg.api_key_encrypted)
            except CryptoError:
                logger.warning(
                    "bootstrap_user_config: stored ciphertext cannot "
                    "be decrypted for user_id=%s — leaving cache cold",
                    uid,
                )
                return None
            return LLMConfigProvider(
                api_url=cfg.api_base_url,
                api_key=api_key,
                model_name=cfg.model_name,
                timeout=int(cfg.timeout_seconds or 120),
                enable_thinking=bool(cfg.enable_thinking),
                supports_vision=bool(getattr(cfg, "supports_vision", False)),
            )

        return await llm_config_cache.get_or_load(user_id, _loader)

    async def build_llm_config_provider(self, user_id: int) -> LLMConfigProvider | LLMNotConfiguredMarker:
        """Build an LLMClient-compatible provider for the given user.

        ``user_id`` is the integer ``users.id`` (internal_id).  F015
        removed the ``_env_config_provider`` fallback: when the user
        has no row, this returns an :class:`LLMNotConfiguredMarker` so
        the LLM call site can produce a clear, friendly error.
        """
        if not isinstance(user_id, int) or user_id <= 0:
            logger.warning(
                "build_llm_config_provider: invalid user_id=%r — "
                "returning unconfigured marker", user_id,
            )
            return LLMNotConfiguredMarker()

        async def _loader(uid: int) -> Optional[LLMConfigProvider]:
            cfg = await self._model_repo.get_active_for_user(uid)
            if cfg is None:
                return None
            try:
                api_key = decrypt_api_key(cfg.api_key_encrypted)
            except CryptoError:
                logger.warning(
                    "build_llm_config_provider: stored ciphertext "
                    "cannot be decrypted for user_id=%s", uid,
                )
                return None
            return LLMConfigProvider(
                api_url=cfg.api_base_url,
                api_key=api_key,
                model_name=cfg.model_name,
                timeout=int(cfg.timeout_seconds or 120),
                enable_thinking=bool(cfg.enable_thinking),
            )

        provider = await llm_config_cache.get_or_load(user_id, _loader)
        if provider is None:
            logger.info(
                "SettingsService.build_llm_config_provider: 用户无模型配置 | user_id=%d",
                user_id,
            )
            return LLMNotConfiguredMarker()
        return provider

    # ── Knowledge base config (Phase 2 — neutral placeholders) ───────

    async def get_knowledge_base_settings(self, user_id: str) -> dict:
        return {
            "configured": False,
            "api_base_url": "",
            "api_key_masked": "",
            "knowledge_base_id": "",
            "default_top_k": 5,
            "timeout_seconds": 30,
            "enabled": False,
        }

    async def update_knowledge_base_settings(self, data: dict, user_id: str) -> dict:
        return {
            "configured": False,
            "api_base_url": data.get("api_base_url", "") or "",
            "api_key_masked": "****" if data.get("api_key") else "",
            "knowledge_base_id": data.get("knowledge_base_id", "") or "",
            "default_top_k": int(data.get("default_top_k") or 5),
            "timeout_seconds": int(data.get("timeout_seconds") or 30),
            "enabled": bool(data.get("enabled", False)),
        }

    async def test_knowledge_base_connection(self, user_id: str) -> dict:
        return {"success": False, "message": "知识库连接测试尚未接入（Phase 2）", "latency_ms": None}

    # ── F021-revised: cross-kind read / save / test / cache / invalidate ──

    async def get_knowledge_base_settings(self, user_id: str | int) -> dict:
        """KB get — mirrors the model-card ``get_model_settings`` contract.

        Same return shape as :meth:`KnowledgeConfigService.get_config`
        but routed through SettingsService so the three configs share
        one read path.  The KB-specific fields live in the response
        exactly as the legacy KB service returned them.
        """
        from app.services.knowledge_config_service import KnowledgeConfigService

        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            return {"configured": False}
        kb_svc = KnowledgeConfigService(self._session)
        return await kb_svc.get_config(internal_id)

    async def get_image_understanding_settings(self, user_id: str | int) -> dict:
        """Image-understanding get — mirrors the model-card contract."""
        from app.services.image_understanding_config_service import (
            ImageUnderstandingConfigService,
        )

        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            return {"configured": False}
        iu_svc = ImageUnderstandingConfigService(self._session)
        return await iu_svc.get_config(internal_id)

    async def save_knowledge_base_settings(
        self, data: dict, user_id: str | int
    ) -> dict:
        """KB save — delegates to KnowledgeConfigService for the per-field handling."""
        from app.services.knowledge_config_service import KnowledgeConfigService

        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            from app.core.exceptions import ValidationError
            raise ValidationError("无法识别当前用户，请重新登录")
        kb_svc = KnowledgeConfigService(self._session)
        return await kb_svc.save_config(internal_id, data)

    async def save_image_understanding_settings(
        self, data: dict, user_id: str | int
    ) -> dict:
        """Image-understanding save — delegates to ImageUnderstandingConfigService."""
        from app.services.image_understanding_config_service import (
            ImageUnderstandingConfigService,
        )

        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            from app.core.exceptions import ValidationError
            raise ValidationError("无法识别当前用户，请重新登录")
        iu_svc = ImageUnderstandingConfigService(self._session)
        return await iu_svc.save_config(internal_id, data)

    async def test_knowledge_base_connection(
        self,
        user_id: str | int,
        page_start: int = 1,
        page_size: int = 1,
    ) -> dict:
        """KB connection probe — same shape as the model ``test_model_connection``."""
        from app.services.knowledge_config_service import KnowledgeConfigService

        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            return {"success": False, "message": "无法识别当前用户，请重新登录", "latency_ms": None}
        kb_svc = KnowledgeConfigService(self._session)
        return await kb_svc.test_connection(internal_id, page_start=page_start, page_size=page_size)

    async def test_image_understanding_connection(self, user_id: str | int) -> dict:
        """Vision probe — same shape as the model ``test_model_connection``."""
        from app.services.image_understanding_config_service import (
            ImageUnderstandingConfigService,
        )

        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            return {"success": False, "message": "无法识别当前用户，请重新登录", "latency_ms": None}
        iu_svc = ImageUnderstandingConfigService(self._session)
        return await iu_svc.test_connection(internal_id)

    async def bootstrap_knowledge_base_config(self, user_id: int) -> None:
        """Warm the KB service's cache on login.  KB has no provider dataclass."""
        from app.services.knowledge_config_service import KnowledgeConfigService

        kb_svc = KnowledgeConfigService(self._session)
        # KB doesn't currently use a process cache (config is read on
        # demand), so the bootstrap is a no-op that still verifies the
        # row is loadable.  We call get_config here as a presence check.
        try:
            await kb_svc.get_config(user_id)
            logger.debug(
                "SettingsService.bootstrap_knowledge_base_config: row check ok | user_id=%d",
                user_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SettingsService.bootstrap_knowledge_base_config: row check failed | user_id=%d | err=%s",
                user_id, exc,
            )

    async def bootstrap_image_understanding_config(
        self, user_id: int
    ) -> Optional["LLMConfigProvider"]:
        """Warm the image-understanding cache on login.  Returns the provider."""
        from app.services.image_understanding_config_service import (
            ImageUnderstandingConfigService,
        )

        iu_svc = ImageUnderstandingConfigService(self._session)
        return await iu_svc.build_provider(user_id)

    async def build_image_understanding_provider(
        self, user_id: int
    ) -> Optional["LLMConfigProvider"]:
        """Resolve the user's image-understanding provider, cache-aware.

        Mirrors :meth:`build_llm_config_provider` so the two
        configurations go through the same code shape.  Returns
        ``None`` when the user has not configured vision (orchestrator
        treats this as OCR-only).
        """
        from app.services.image_understanding_config_service import (
            ImageUnderstandingConfigService,
        )

        iu_svc = ImageUnderstandingConfigService(self._session)
        return await iu_svc.build_provider(user_id)

    async def build_knowledge_base_provider(
        self, user_id: int
    ) -> Optional[Any]:
        """Resolve the user's knowledge-base row, cache-aware.

        KB has no provider dataclass today — the row is read on demand
        by the KB services.  This helper just returns the latest
        configuration dict so callers (e.g. RequirementParserTool) can
        access it through SettingsService like the other two configs.
        """
        from app.services.knowledge_config_service import KnowledgeConfigService

        kb_svc = KnowledgeConfigService(self._session)
        return await kb_svc.get_config(user_id)

    async def bootstrap_all_user_caches(self, user_id: int) -> dict:
        """Login hook — warms primary + KB + image-understanding.

        ``AuthService.login`` calls this so all three configs are
        ready before the first user-visible request.
        """
        primary = await self.bootstrap_user_config(user_id)
        await self.bootstrap_knowledge_base_config(user_id)
        image = await self.bootstrap_image_understanding_config(user_id)
        warmed = {
            "primary_model": bool(primary),
            "knowledge_base": "row_checked",
            "image_understanding": bool(image),
        }
        logger.info(
            "SettingsService.bootstrap_all_user_caches: login warm | user_id=%d | warmed=%s",
            user_id, warmed,
        )
        return warmed

    async def invalidate_all_user_caches(self, user_id: int) -> None:
        """Logout hook — clears primary + image-understanding caches.

        Replaces the two manual ``invalidate`` calls previously
        hard-coded in ``auth.py``.  KB has no in-process cache, so
        nothing to do for it.
        """
        from app.core.image_understanding_config_cache import image_understanding_config_cache
        from app.core.llm_config_cache import llm_config_cache

        await llm_config_cache.invalidate(user_id)
        await image_understanding_config_cache.invalidate(user_id)
        logger.info(
            "SettingsService.invalidate_all_user_caches: logout clear | user_id=%d",
            user_id,
        )

    # ── Upload config (REAL — system_configs) ────────────────────────

    async def get_upload_settings(self, user_id: str) -> dict:
        max_mb = await self._get_config("upload.max_file_size_mb", "50")
        extensions_raw = await self._get_config(
            "upload.allowed_extensions", '[".docx",".txt",".md"]'
        )
        max_files = await self._get_config("upload.max_files_per_conversation", "10")

        try:
            extensions = json.loads(extensions_raw)
        except (json.JSONDecodeError, TypeError):
            extensions = [".docx", ".txt", ".md"]

        return {
            "max_file_size_mb": int(max_mb),
            "max_files_per_conversation": int(max_files),
            "allowed_extensions": extensions,
        }

    async def update_upload_settings(self, data: dict, user_id: str) -> dict:
        max_mb = str(data.get("max_file_size_mb", 50))
        max_files = str(data.get("max_files_per_conversation", 10))
        extensions = data.get("allowed_extensions", [".docx", ".txt", ".md"])
        extensions_json = json.dumps(extensions, ensure_ascii=False)

        await self._upsert_config(
            "upload.max_file_size_mb", max_mb, "int", "Single upload max file size (MB)"
        )
        await self._upsert_config(
            "upload.allowed_extensions", extensions_json, "json", "Allowed upload file extensions"
        )
        await self._upsert_config(
            "upload.max_files_per_conversation", max_files, "int", "Max files per conversation"
        )

        # Phase 1 (Step 5): invalidate the SystemConfig cache for the 3
        # upload keys so the next read picks up the new value.  Each
        # SystemConfig key is cached independently (设计文档 §20), so
        # the cache bypass on the next read is bounded to ~30s (jittered).
        # P0 收口:用 ``register_after_commit`` 挂 hook;rollback 不触发.
        try:
            from app.cache.domains.config_cache import get_system_config_cache
            from app.db.sync import register_after_commit

            async def _do_invalidate(_session):
                try:
                    sys_cache = get_system_config_cache()
                    for key in (
                        "upload.max_file_size_mb",
                        "upload.allowed_extensions",
                        "upload.max_files_per_conversation",
                    ):
                        await sys_cache.invalidate(key)
                except Exception as cache_exc:  # noqa: BLE001
                    logger.warning(
                        "update_upload_settings: SystemConfig cache invalidate "
                        "failed: %s", cache_exc,
                    )

            register_after_commit(self._session, _do_invalidate)
        except Exception as exc:  # noqa: BLE001 — cache fail must not fail save
            logger.warning(
                "update_upload_settings: cache hook register failed: %s", exc,
            )

        return {
            "max_file_size_mb": int(max_mb),
            "max_files_per_conversation": int(max_files),
            "allowed_extensions": extensions,
        }

    # ── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _model_to_public(cfg: ModelConfig) -> dict:
        try:
            plain = decrypt_api_key(cfg.api_key_encrypted)
            masked = mask_api_key(plain)
        except CryptoError:
            masked = "****"
        return {
            "configured": True,
            "public_id": cfg.public_id,
            "config_name": cfg.config_name,
            "provider": cfg.provider,
            "api_base_url": cfg.api_base_url,
            "api_key_masked": masked,
            "model_name": cfg.model_name,
            "timeout_seconds": int(cfg.timeout_seconds or 120),
            "enable_thinking": bool(cfg.enable_thinking),
            "supports_vision": bool(cfg.supports_vision),
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "is_default": bool(cfg.is_default),
            "capability_type": getattr(cfg, "capability_type", None) or "chat",
            "context_window_tokens": getattr(cfg, "context_window_tokens", None),
            "context_window_k": _context_window_k_from_tokens(
                getattr(cfg, "context_window_tokens", None)
            ),
            "default_max_output_tokens": getattr(cfg, "default_max_output_tokens", None),
            "embedding_dimension": getattr(cfg, "embedding_dimension", None),
            "normalize_embeddings": getattr(cfg, "normalize_embeddings", None),
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @staticmethod
    def _unconfigured_public() -> dict:
        """Default response when the current user has no model_configs row."""
        return {
            "configured": False,
            "public_id": None,
            "config_name": "",
            "provider": "",
            "api_base_url": "",
            "api_key_masked": "",
            "model_name": "",
            "timeout_seconds": 120,
            "enable_thinking": False,
            "supports_vision": False,
            "temperature": None,
            "max_tokens": None,
            "is_default": False,
            "capability_type": "chat",
            "context_window_tokens": None,
            "context_window_k": None,
            "default_max_output_tokens": None,
            "embedding_dimension": None,
            "normalize_embeddings": None,
            "updated_at": None,
        }

    # ── CE-01: 多能力模型配置管理 ──────────────────────────────────

    async def list_capabilities(self, user_id: str | int) -> dict:
        """返回各能力的用户配置（能力 → 配置列表），含系统共享行可见性。"""
        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            return {"capabilities": []}
        capabilities: dict[str, list] = {}
        for capability in (
            "chat",
            "reasoning",
            "embedding",
            "reranker",
            "compression",
            "memory_extraction",
        ):
            rows = await self._model_repo.list_by_capability(capability, internal_id)
            capabilities[capability] = [self._model_to_public(r) for r in rows]
        return {"capabilities": capabilities}

    async def get_capability_config(self, capability_type: str, user_id: str | int) -> dict:
        """返回指定能力的用户 default 配置。"""
        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            return self._unconfigured_public()
        cfg = await self._model_repo.get_active_for_capability(capability_type, internal_id)
        if cfg is None:
            return self._unconfigured_public()
        return self._model_to_public(cfg)

    async def update_capability_config(
        self, capability_type: str, data: dict, user_id: str | int
    ) -> dict:
        """按能力 upsert 用户模型配置。

        - capability_type 必须合法；
        - masked api_key 更新不覆盖原值（api_key 为空或含掩码时不更新）；
        - 每种 capability 维持一个 default 行。
        """
        internal_id = self._resolve_internal_id(user_id)
        if internal_id is None:
            from app.core.exceptions import ValidationError
            raise ValidationError("无法识别当前用户，请重新登录")

        if capability_type not in (
            "chat",
            "reasoning",
            "embedding",
            "reranker",
            "compression",
            "memory_extraction",
        ):
            from app.core.exceptions import ValidationError
            raise ValidationError(f"不支持的模型能力类型: {capability_type}")

        api_base_url = (data.get("api_base_url") or "").strip()
        model_name = (data.get("model_name") or "").strip()
        if not api_base_url:
            from app.core.exceptions import ValidationError
            raise ValidationError("API Base URL 不能为空")
        if not model_name:
            from app.core.exceptions import ValidationError
            raise ValidationError("模型名称不能为空")

        plain_api_key = (data.get("api_key") or "").strip()
        encrypted_api_key = ""
        if plain_api_key and "*" not in plain_api_key:
            encrypted_api_key = encrypt_api_key(plain_api_key)

        existing = await self._model_repo.get_active_for_capability(capability_type, internal_id)
        public_id = existing.public_id if existing else generate_public_id("model_config")

        saved = await self._model_repo.upsert_for_capability(
            user_id=internal_id,
            capability_type=capability_type,
            public_id=public_id,
            config_name=data.get("config_name") or f"{capability_type} 模型配置",
            provider=(data.get("provider") or "openai-compatible").strip(),
            api_base_url=api_base_url,
            api_key_encrypted=encrypted_api_key,
            model_name=model_name,
            timeout_seconds=int(data.get("timeout_seconds") or 120),
            enable_thinking=bool(data.get("enable_thinking", False)),
            supports_vision=bool(data.get("supports_vision", False)),
            temperature=float(data["temperature"]) if data.get("temperature") is not None else None,
            max_tokens=int(data["max_tokens"]) if data.get("max_tokens") is not None else None,
            context_window_tokens=_context_window_tokens_from_payload(data),
            context_window_tokens_set=(
                "context_window_k" in data or "context_window_tokens" in data
            ),
            default_max_output_tokens=int(data["default_max_output_tokens"]) if data.get("default_max_output_tokens") is not None else None,
            embedding_dimension=int(data["embedding_dimension"]) if data.get("embedding_dimension") is not None else None,
            normalize_embeddings=bool(data["normalize_embeddings"]) if data.get("normalize_embeddings") is not None else None,
            rerank_instruction=data.get("rerank_instruction"),
            pre_rerank_limit=int(data["pre_rerank_limit"]) if data.get("pre_rerank_limit") is not None else None,
            score_type=data.get("score_type"),
            enabled=bool(data.get("enabled", True)),
            is_default=bool(data.get("is_default", True)),
        )
        await self._session.flush()
        await self._session.refresh(saved)

        # Phase 1 (Step 5): write encrypted DTO into Redis so the next
        # LLM call (or Capability-specific call) hits the cache.  Per
        # 设计文档 §11.1: Redis stores ciphertext only.
        # P0 收口:用 ``register_after_commit`` 挂 hook;rollback 不触发.
        try:
            from app.cache.domains.config_cache import (
                ModelConfigDTO,
                get_model_config_cache,
            )
            from app.db.sync import register_after_commit

            _dto = ModelConfigDTO(
                public_id=saved.public_id,
                user_id=saved.user_id,
                capability_type=capability_type,
                config_name=saved.config_name,
                provider=saved.provider,
                api_base_url=saved.api_base_url,
                api_key_encrypted=saved.api_key_encrypted,
                model_name=saved.model_name,
                timeout_seconds=int(saved.timeout_seconds or 120),
                enable_thinking=bool(saved.enable_thinking),
                supports_vision=bool(saved.supports_vision),
                is_default=bool(saved.is_default),
                enabled=bool(saved.enabled),
                temperature=saved.temperature,
                max_tokens=saved.max_tokens,
                context_window_tokens=getattr(saved, "context_window_tokens", None),
                default_max_output_tokens=getattr(
                    saved, "default_max_output_tokens", None
                ),
                embedding_dimension=getattr(saved, "embedding_dimension", None),
                normalize_embeddings=getattr(saved, "normalize_embeddings", None),
                rerank_instruction=getattr(saved, "rerank_instruction", None),
                pre_rerank_limit=getattr(saved, "pre_rerank_limit", None),
                score_type=getattr(saved, "score_type", None),
            )
            _user_id = saved.user_id
            _cap = capability_type

            async def _do_write_through(_session):
                try:
                    await get_model_config_cache().write_through(
                        user_id=_user_id,
                        capability=_cap,
                        dto=_dto,
                    )
                except Exception as cache_exc:  # noqa: BLE001
                    logger.warning(
                        "update_capability_config: cache write-through failed for "
                        "user_id=%s capability=%s: %s",
                        _user_id, _cap, cache_exc,
                    )

            register_after_commit(self._session, _do_write_through)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "update_capability_config: cache hook register failed for "
                "user_id=%s capability=%s: %s",
                saved.user_id, capability_type, exc,
            )

        return self._model_to_public(saved)

    def _resolve_internal_id(self, user_id: str | int | None) -> int | None:
        """Translate a public_id (str) or already-internal id (int) to int.

        ``user_id`` may be:
          - the JWT subject (public_id, e.g. ``"usr_abc123"``) — most common
          - already the integer internal_id (defensive)
          - ``None`` / empty / unparseable — returns None

        Returns ``None`` if the value is a non-integer string that does
        not match a known user (caller should treat as "unauthenticated"
        and 401).
        """
        if user_id is None or user_id == "":
            return None
        if isinstance(user_id, int):
            return user_id if user_id > 0 else None
        try:
            return int(user_id)
        except (TypeError, ValueError):
            pass
        return None  # public_id lookups must be done by caller with await


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Phase 2.9A / 2.9B 起作为各 Agent 节点 LLM 配置来源):
#
#   链路 (TestPlan 任务触发):
#     api/v1/messages.py → MessageService.send_message(...) 检测到 prompt 触发
#       → AgentTaskService.create_task(...)
#         → SettingsService.build_llm_config_provider(user_id)
#           → 若 model_configs 有用户行 → 构造 UserAware LLMConfigProvider
#           → 若 model_configs 没有 → 构造 LLMNotConfiguredMarker(sentinel)
#             → 任务直接 fail_task 出 LLM_NOT_CONFIGURED 错误
#       → Outbox: AgentExecutionRequest 行(供 Worker claim)
#     → AgentExecutionWorker 拉起
#       → ApiDispatcher.dispatch_new_task(...)
#         → LangGraphRunCoordinator(graph_runtime_context)
#           → ctx.llm_config_provider = build_llm_config_provider(user_id)
#           → 注入到所有节点(由 _shared/runtime_context 流转)
#
#   链路 (普通聊天):
#     api/v1/messages.py → MessageService.send_chat_message(...)
#       → ChatLLMService.generate_reply(prompt, chat_context, user_id)
#         → LLMClient.generate_with_profile(CHAT_PROFILE, prompt)
#
#   链路 (Settings UI):
#     api/v1/settings.py → SettingsService.get_user_settings / set_user_settings
#       → 读写 model_configs / knowledge_configs / image_understanding_configs 行
#
# 关键约束(供开发者速查):
#   - 模型配置:per-user,DB-only;无 DB 行 → LLMNotConfiguredMarker(sentinel 错
#     误清晰提示用户去 Settings 配);
#   - 知识库配置:per-user,有 default_knowledge_ids / top_k / similarity_threshold /
#     retrieve_strategy / enable_rerank_model / rerank_model 等;
#   - ImageUnderstanding 配置:per-user,模型 + prompt template。
#   - 所有配置变更需要 invalidate_settings_cache(user_id)(如有缓存)。
