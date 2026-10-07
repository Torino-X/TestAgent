"""F020 — image_understanding_configs repository.

Per-user CRUD for the dedicated image-understanding (vision) LLM
endpoint.  Mirrors :mod:`app.repositories.knowledge_config_repository`
so the lifecycle (upsert / update_test_status) matches F017.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.image_understanding_config import ImageUnderstandingConfig
from app.repositories.base import BaseRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)


class ImageUnderstandingConfigRepository(BaseRepository[ImageUnderstandingConfig]):
    model = ImageUnderstandingConfig

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_for_user(self, user_id: int) -> Optional[ImageUnderstandingConfig]:
        """Return the most recent active row for this user (user-only — no system fallback in F020)."""
        result = await self.session.execute(
            select(ImageUnderstandingConfig)
            .where(
                ImageUnderstandingConfig.user_id == user_id,
                ImageUnderstandingConfig.status == "active",
            )
            .order_by(ImageUnderstandingConfig.updated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def upsert_for_user(
        self,
        *,
        user_id: int,
        public_id: str | None,
        api_base_url: str,
        api_key_encrypted: str | None,
        api_key_masked: str | None,
        model_name: str,
        timeout_seconds: int,
        max_tokens: int | None,
        enable_in_doc_parsing: bool,
    ) -> ImageUnderstandingConfig:
        existing = await self.session.execute(
            select(ImageUnderstandingConfig)
            .where(
                ImageUnderstandingConfig.user_id == user_id,
                ImageUnderstandingConfig.status == "active",
            )
            .order_by(ImageUnderstandingConfig.updated_at.desc())
            .limit(1)
        )
        existing_row = existing.scalar_one_or_none()

        if existing_row is not None:
            existing_row.api_base_url = api_base_url
            if api_key_encrypted:
                # Empty / masked values mean "leave existing ciphertext".
                existing_row.api_key_encrypted = api_key_encrypted
                existing_row.api_key_masked = api_key_masked
            existing_row.model_name = model_name
            existing_row.timeout_seconds = timeout_seconds
            existing_row.max_tokens = max_tokens
            existing_row.enable_in_doc_parsing = enable_in_doc_parsing
            await self.session.flush()
            logger.info(
                "ImageUnderstandingConfigRepository.upsert_for_user: 更新配置 | "
                "user_id=%d | model=%s | masked=%s | enable=%s",
                user_id, model_name, api_key_masked or "", enable_in_doc_parsing,
            )
            return existing_row

        now = utcnow()
        new_cfg = ImageUnderstandingConfig(
            public_id=public_id or generate_public_id("image_understanding"),
            user_id=user_id,
            api_base_url=api_base_url,
            api_key_encrypted=api_key_encrypted or None,
            api_key_masked=api_key_masked,
            model_name=model_name,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
            enable_in_doc_parsing=enable_in_doc_parsing,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.session.add(new_cfg)
        await self.session.flush()
        logger.info(
            "ImageUnderstandingConfigRepository.upsert_for_user: 新建配置 | "
            "user_id=%d | model=%s | base=%s",
            user_id, model_name, api_base_url[:60],
        )
        return new_cfg

    async def update_test_status(
        self,
        *,
        user_id: int,
        status: str,
        message: str | None,
    ) -> None:
        """Update ``last_test_*`` columns on the user's most recent active row."""
        result = await self.session.execute(
            select(ImageUnderstandingConfig)
            .where(
                ImageUnderstandingConfig.user_id == user_id,
                ImageUnderstandingConfig.status == "active",
            )
            .order_by(ImageUnderstandingConfig.updated_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.last_test_status = status
        row.last_test_message = message
        row.last_test_at = utcnow()
        await self.session.flush()

# 模块定位:ImageUnderstandingConfig 仓储
#
# 链路:
#   api/v1/image-understanding PUT → 写 per-user config
#   ImageUnderstandingOrchestrator → 读 user_id 取
#
# 关键约束:
#   - user_internal_id 唯一索引(per-user 只 1 行);
#   - provider 切换必须 invalidate settings cache。
