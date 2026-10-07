"""F020 — ImageUnderstandingConfigService.

Per-user service responsible for:

* CRUD on ``image_understanding_configs`` (DB).
* API key encryption + masking (Fernet).
* Connection test via a lightweight LLMClient probe (1x1 PNG).
* Cache warming (so the orchestrator never touches the DB on the
  hot path of requirement parsing).

All methods accept the integer ``user_internal_id``; the JWT
subject lookup is the caller's responsibility.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import CryptoError, decrypt_api_key, encrypt_api_key, mask_api_key
from app.core.image_understanding_config_cache import image_understanding_config_cache
from app.integrations.llm_client import LLMClient, LLMClientError
from app.models.image_understanding_config import ImageUnderstandingConfig
from app.repositories.image_understanding_config_repository import (
    ImageUnderstandingConfigRepository,
)
from app.utils.datetime import utcnow

logger = logging.getLogger(__name__)


# 4x4 solid red PNG — used to test image-understanding endpoints
# without exercising the real image pipeline.  Solid-coloured squares
# stay clear of dashscope's safety classifier; a 1x1 transparent PNG
# gets flagged as "sensitive" (1026).  Constant; do not edit.
_HEALTH_PROBE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAEElEQVR42mP4z8AARwzEcQCukw/xOF6MEQAAAABJRU5ErkJggg=="
)


@dataclass
class ImageUnderstandingConfigProvider:
    """Adapter exposing the attributes ``VisionService`` needs.

    Returned by :meth:`ImageUnderstandingConfigService.build_provider` so
    ``VisionService`` can be constructed with
    ``LLMClient(config_provider=provider)`` directly.  Mirrors
    :class:`app.services.settings_service.LLMConfigProvider` but
    without ``enable_thinking`` (vision models do not consume that
    knob — minimaxi/qwen-vl ignore it).
    """

    api_url: str
    api_key: str
    model_name: str
    timeout: int
    enable_in_doc_parsing: bool
    public_id: str | None = None

    def get_effective_api_key(self) -> str:
        return self.api_key


class ImageUnderstandingConfigService:
    """User-scoped image-understanding configuration service."""

    DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DEFAULT_MODEL = "qwen-vl-plus"

    def __init__(self, session: AsyncSession, *, context_llm_invoker: Any | None = None) -> None:
        self._session = session
        self._context_llm_invoker = context_llm_invoker
        self._repo = ImageUnderstandingConfigRepository(session)

    # ── Public API ────────────────────────────────────────────────

    async def get_config(self, user_internal_id: int) -> dict:
        row = await self._repo.get_for_user(user_internal_id)
        if row is None:
            return self._unconfigured_public()
        return self._to_public(row)

    async def save_config(
        self,
        user_internal_id: int,
        data: dict,
    ) -> dict:
        api_base_url = (data.get("api_base_url") or "").strip() or self.DEFAULT_BASE_URL
        model_name = (data.get("model_name") or "").strip() or self.DEFAULT_MODEL
        plain_api_key = (data.get("api_key") or "").strip()

        try:
            timeout_seconds = int(data.get("timeout_seconds") or 60)
        except (TypeError, ValueError):
            timeout_seconds = 60
        raw_max_tokens = data.get("max_tokens")
        try:
            max_tokens: int | None = (
                int(raw_max_tokens) if raw_max_tokens not in (None, "") else None
            )
        except (TypeError, ValueError):
            max_tokens = None

        if not api_base_url:
            from app.core.exceptions import ValidationError
            raise ValidationError("api_base_url 不能为空")

        encrypted = ""
        masked = ""
        if plain_api_key:
            encrypted = encrypt_api_key(plain_api_key)
            masked = mask_api_key(plain_api_key)

        existing = await self._repo.get_for_user(user_internal_id)
        # When the user is updating without supplying a new key, retain
        # the stored ciphertext — matches the KnowledgeConfigService
        # convention.  Initial save without a key is rejected.
        if not encrypted and existing is not None:
            encrypted = existing.api_key_encrypted or ""
            masked = existing.api_key_masked or ""
        elif not encrypted:
            from app.core.exceptions import ValidationError
            raise ValidationError("api_key 不能为空")

        saved = await self._repo.upsert_for_user(
            user_id=user_internal_id,
            public_id=existing.public_id if existing else None,
            api_base_url=api_base_url,
            api_key_encrypted=encrypted,
            api_key_masked=masked,
            model_name=model_name,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
            enable_in_doc_parsing=bool(data.get("enable_in_doc_parsing", False)),
        )
        await self._session.flush()
        await self._session.refresh(saved)

        # Warm the cache so the next requirement-parsing call does not
        # have to decrypt again.  CryptoError here is non-fatal — the
        # orchestrator will re-derive on first use.
        try:
            plain_key = decrypt_api_key(saved.api_key_encrypted)
        except CryptoError:
            plain_key = ""
        if plain_key:
            provider = ImageUnderstandingConfigProvider(
                api_url=saved.api_base_url,
                api_key=plain_key,
                model_name=saved.model_name,
                timeout=int(saved.timeout_seconds or 60),
                enable_in_doc_parsing=bool(saved.enable_in_doc_parsing),
                public_id=saved.public_id,
            )
            await image_understanding_config_cache.set(user_internal_id, provider)

        # Phase 1 (Step 5): write encrypted DTO into Redis so the next
        # ``build_provider`` hits the cache instead of going through the
        # in-process Adapter's loader.  Redis stores Fernet ciphertext
        # only — never plaintext (设计文档 §11.1).
        # P0 收口:用 ``register_after_commit`` 挂 hook;rollback 不触发.
        try:
            from app.cache.domains.config_cache import (
                ImageUnderstandingConfigDTO,
                get_image_understanding_config_cache,
            )
            from app.db.sync import register_after_commit

            _dto = ImageUnderstandingConfigDTO(
                public_id=saved.public_id,
                user_id=user_internal_id,
                api_base_url=saved.api_base_url,
                api_key_encrypted=saved.api_key_encrypted,
                api_key_masked=saved.api_key_masked,
                model_name=saved.model_name,
                timeout_seconds=int(saved.timeout_seconds or 60),
                max_tokens=saved.max_tokens,
                enable_in_doc_parsing=bool(saved.enable_in_doc_parsing),
                status=saved.status,
            )
            _uid = user_internal_id

            async def _do_write_through(_session):
                try:
                    await get_image_understanding_config_cache().write_through(
                        user_id=_uid, dto=_dto,
                    )
                except Exception as cache_exc:  # noqa: BLE001
                    logger.warning(
                        "ImageUnderstandingConfigService.save_config: cache "
                        "write-through failed for user_id=%s: %s",
                        _uid, cache_exc,
                    )

            register_after_commit(self._session, _do_write_through)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ImageUnderstandingConfigService.save_config: cache hook register "
                "failed for user_id=%s: %s",
                user_internal_id, exc,
            )

        now_iso = (saved.updated_at or utcnow()).isoformat()
        return {
            "config_id": saved.public_id,
            "saved": True,
            "api_key_set": bool(saved.api_key_encrypted),
            "api_key_masked": saved.api_key_masked or "",
            "updated_at": now_iso,
        }

    async def test_connection(
        self,
        user_internal_id: int,
        override: dict | None = None,
    ) -> dict:
        """Probe the configured vision endpoint with a 1x1 PNG.

        ``override`` (optional) — when supplied it represents the form
        values from the request body so the user can "test before
        save".  When the override omits ``api_key`` we transparently
        fall back to the DB-saved key, matching the main-model
        ``test_model_connection`` semantics.
        """
        # ── Body-override path: build a transient provider ────────
        provider: ImageUnderstandingConfigProvider | None = None
        using_override = bool(override and any(
            (override.get(k) or "").strip() for k in ("api_base_url", "model_name")
        ))
        if using_override:
            api_key = (override.get("api_key") or "").strip()
            if not api_key:
                # Fall back to the DB-saved key.
                saved = await self._repo.get_for_user(user_internal_id)
                if saved is not None:
                    api_key = self._decrypt_for_use(saved) or ""
            provider = ImageUnderstandingConfigProvider(
                api_url=(override.get("api_base_url") or "").strip(),
                api_key=api_key,
                model_name=(override.get("model_name") or "").strip(),
                timeout=int(override.get("timeout_seconds") or 60),
                enable_in_doc_parsing=bool(
                    override.get("enable_in_doc_parsing", False)
                ),
            )
        # ── DB-only path (legacy "saved → test") ───────────────────
        if provider is None:
            row = await self._repo.get_for_user(user_internal_id)
            if row is None:
                return self._test_failure(
                    41201,
                    "尚未配置图片理解模型，请先保存连接配置",
                    latency_ms=0,
                )
            plain_key = self._decrypt_for_use(row)
            if not plain_key:
                return self._test_failure(
                    41202,
                    "API Key 未配置或解密失败",
                    latency_ms=0,
                )
            provider = ImageUnderstandingConfigProvider(
                api_url=row.api_base_url,
                api_key=plain_key,
                model_name=row.model_name,
                timeout=int(row.timeout_seconds or 60),
                enable_in_doc_parsing=bool(row.enable_in_doc_parsing),
                public_id=row.public_id,
            )

        if not provider.api_url or not provider.api_key or not provider.model_name:
            return self._test_failure(
                41203,
                "图片理解模型未配置完整（API 地址 / Key / 模型名缺失）",
                latency_ms=0,
            )

        client = LLMClient(config_provider=provider)
        try:
            start = time.monotonic()
            # Drive a real chat-completion with the probe PNG.  We do
            # NOT call health_check() because that path sends only a
            # text message and does not exercise the image attachment
            # code path that the orchestrator actually uses.
            try:
                from app.db.session import AsyncSessionLocal
                from app.services.vision_service import VISION_ANALYSIS_PROFILE
                from types import SimpleNamespace

                bridge = self._context_llm_invoker
                if bridge is None or not getattr(bridge, "available", False):
                    raise RuntimeError("context_engine_unavailable")
                result = await bridge.generate(
                    user_id=user_internal_id,
                    call_site="vision.connection_probe",
                    llm_task_profile=VISION_ANALYSIS_PROFILE,
                    current_goal="Validate the vision provider can inspect this probe image and return JSON.",
                    user_content="Validate the vision provider can inspect this probe image and return JSON.",
                    output_contract="json",
                    image_paths=[_png_b64_to_tempfile()],
                    runtime_context=SimpleNamespace(
                        session_factory=AsyncSessionLocal,
                        llm_client=client,
                        user_internal_id=user_internal_id,
                    ),
                )
                if result is None or getattr(result, "value", None) is None:
                    raise RuntimeError("vision_probe_failed")
            except Exception as exc:  # noqa: BLE001 - probe failures are user-visible data
                elapsed_ms = int((time.monotonic() - start) * 1000)
                await self._repo.update_test_status(
                    user_id=user_internal_id,
                    status="failed",
                    message=str(exc)[:200],
                )
                return {
                    "success": False,
                    "latency_ms": elapsed_ms,
                    "status": "failed",
                    "message": str(exc),
                    "error_code": "VISION_TEST_FAILED",
                    "tested_at": utcnow().isoformat(),
                }
            elapsed_ms = int((time.monotonic() - start) * 1000)
        finally:
            # LLMClient does not hold a long-lived client, no aclose.
            pass

        await self._repo.update_test_status(
            user_id=user_internal_id,
            status="success",
            message="连接成功",
        )
        return {
            "success": True,
            "latency_ms": elapsed_ms,
            "status": "success",
            "message": "连接成功",
            "tested_at": utcnow().isoformat(),
        }

    async def build_provider(
        self, user_internal_id: int
    ) -> Optional[ImageUnderstandingConfigProvider]:
        """Resolve a decrypted :class:`ImageUnderstandingConfigProvider`.

        Returns ``None`` when the user has not configured image
        understanding — the orchestrator treats this as "OCR-only".
        """

        async def _loader(uid: int) -> Optional[ImageUnderstandingConfigProvider]:
            cfg = await self._repo.get_for_user(uid)
            if cfg is None:
                return None
            try:
                plain_key = decrypt_api_key(cfg.api_key_encrypted or "")
            except CryptoError:
                logger.warning(
                    "ImageUnderstandingConfigService.build_provider: "
                    "stored ciphertext cannot decrypt for user_id=%s", uid,
                )
                return None
            return ImageUnderstandingConfigProvider(
                api_url=cfg.api_base_url,
                api_key=plain_key,
                model_name=cfg.model_name,
                timeout=int(cfg.timeout_seconds or 60),
                enable_in_doc_parsing=bool(cfg.enable_in_doc_parsing),
                public_id=cfg.public_id,
            )

        return await image_understanding_config_cache.get_or_load(
            user_internal_id, _loader
        )

    # ── Internal helpers ──────────────────────────────────────────

    @staticmethod
    def _decrypt_for_use(row: ImageUnderstandingConfig) -> str:
        if not row.api_key_encrypted:
            return ""
        try:
            return decrypt_api_key(row.api_key_encrypted)
        except CryptoError:
            logger.warning(
                "ImageUnderstandingConfigService: 存储的API Key无法解密 | user_id=%s",
                row.user_id,
            )
            return ""

    def _to_public(self, row: ImageUnderstandingConfig) -> dict:
        return {
            "config_id": row.public_id,
            "api_base_url": row.api_base_url,
            "api_key_masked": row.api_key_masked or "",
            "api_key_set": bool(row.api_key_encrypted),
            "model_name": row.model_name,
            "timeout_seconds": int(row.timeout_seconds or 60),
            "max_tokens": row.max_tokens,
            "enable_in_doc_parsing": bool(row.enable_in_doc_parsing),
            "last_test_status": row.last_test_status,
            "last_test_message": row.last_test_message,
            "last_test_at": row.last_test_at.isoformat() if row.last_test_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _unconfigured_public() -> dict:
        return {
            "config_id": None,
            "api_base_url": ImageUnderstandingConfigService.DEFAULT_BASE_URL,
            "api_key_masked": "",
            "api_key_set": False,
            "model_name": ImageUnderstandingConfigService.DEFAULT_MODEL,
            "timeout_seconds": 60,
            "max_tokens": None,
            "enable_in_doc_parsing": False,
            "last_test_status": None,
            "last_test_message": None,
            "last_test_at": None,
            "updated_at": None,
        }

    @staticmethod
    def _test_failure(code: int, message: str, *, latency_ms: int) -> dict:
        return {
            "success": False,
            "latency_ms": latency_ms,
            "status": "failed",
            "message": message,
            "error_code": f"IMAGE_UNDERSTANDING_{code}",
            "tested_at": utcnow().isoformat(),
        }


def _png_b64_to_tempfile() -> str:
    """Decode the inline PNG probe and persist to a temporary file path.

    LLMClient's ``generate_with_system`` reads images via file path so
    that ``_encode_image_base64`` can base64-encode the bytes.  This
    helper materialises the probe PNG to a temp file and returns the
    path; the file is owned by the test call and lives only for the
    duration of the request.
    """
    import tempfile

    raw = base64.b64decode(_HEALTH_PROBE_PNG_B64)
    tmp = tempfile.NamedTemporaryFile(prefix="img_probe_", suffix=".png", delete=False)
    try:
        tmp.write(raw)
    finally:
        tmp.close()
    return tmp.name

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (ImageUnderstanding per-user 配置):
#
#   链路:
#     SettingsView "图片理解" 页面:
#       → api/v1/settings.py
#         → ImageUnderstandingConfigService.get_for_user(user_id)
#         → update(provider, model, prompt_template, max_tokens, ...)
#     VisionService / ImageUnderstandingOrchestrator 读此配置
#
# 关键约束(供开发者速查):
#   - 与 KnowledgeConfigService 平行,API 完全不同;
#   - prompt_template 留空 → 走系统默认模板;
#   - max_tokens / temperature / top_p 都在这里覆盖;
#   - provider 切换时旧缓存必须 invalidate。
