"""F017 — knowledge_configs repository.

Per-user (or system-shared) CRUD for company knowledge base
configuration.  API key is stored encrypted (Fernet) and only
returned in masked form.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge_config import KnowledgeConfig
from app.repositories.base import BaseRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)


class KnowledgeConfigRepository(BaseRepository[KnowledgeConfig]):
    model = KnowledgeConfig

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_for_user(self, user_id: int) -> KnowledgeConfig | None:
        """Per-user row first, fall back to a system-shared row."""
        result = await self.session.execute(
            select(KnowledgeConfig)
            .where(
                KnowledgeConfig.user_id == user_id,
                KnowledgeConfig.status == "active",
            )
            .order_by(KnowledgeConfig.updated_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row

        result = await self.session.execute(
            select(KnowledgeConfig)
            .where(
                KnowledgeConfig.user_id.is_(None),
                KnowledgeConfig.status == "active",
            )
            .order_by(KnowledgeConfig.updated_at.desc())
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
        default_knowledge_ids: list[str] | None,
        top_k: int,
        similarity_threshold: float,
        retrieve_strategy: int,
        enable_rerank_model: bool,
        rerank_model: str | None,
        knowledge_graph: bool,
        direct_answer_enabled: bool,
        test_plan_generation_enabled: bool,
        test_case_generation_enabled: bool,
        timeout_seconds: int,
    ) -> KnowledgeConfig:
        existing = await self.session.execute(
            select(KnowledgeConfig)
            .where(
                KnowledgeConfig.user_id == user_id,
                KnowledgeConfig.status == "active",
            )
            .order_by(KnowledgeConfig.updated_at.desc())
            .limit(1)
        )
        existing_row = existing.scalar_one_or_none()

        if existing_row is not None:
            existing_row.api_base_url = api_base_url
            if api_key_encrypted:
                # Only overwrite the ciphertext when the caller supplied one
                existing_row.api_key_encrypted = api_key_encrypted
                existing_row.api_key_masked = api_key_masked
            existing_row.default_knowledge_ids = default_knowledge_ids or []
            existing_row.top_k = top_k
            existing_row.similarity_threshold = similarity_threshold
            existing_row.retrieve_strategy = retrieve_strategy
            existing_row.enable_rerank_model = enable_rerank_model
            existing_row.rerank_model = rerank_model
            existing_row.knowledge_graph = knowledge_graph
            existing_row.direct_answer_enabled = direct_answer_enabled
            existing_row.test_plan_generation_enabled = (
                test_plan_generation_enabled
            )
            existing_row.test_case_generation_enabled = (
                test_case_generation_enabled
            )
            existing_row.timeout_seconds = timeout_seconds
            await self.session.flush()
            logger.info(
                "KnowledgeConfigRepository.upsert_for_user: 更新配置 | user_id=%d | base=%s | masked=%s",
                user_id, api_base_url[:60], api_key_masked or "",
            )
            return existing_row

        now = utcnow()
        new_cfg = KnowledgeConfig(
            public_id=public_id or generate_public_id("knowledge_config"),
            user_id=user_id,
            scope="user",
            api_base_url=api_base_url,
            api_key_encrypted=api_key_encrypted or None,
            api_key_masked=api_key_masked,
            default_knowledge_ids=default_knowledge_ids or [],
            top_k=top_k,
            similarity_threshold=similarity_threshold,
            retrieve_strategy=retrieve_strategy,
            enable_rerank_model=enable_rerank_model,
            rerank_model=rerank_model,
            knowledge_graph=knowledge_graph,
            direct_answer_enabled=direct_answer_enabled,
            test_plan_generation_enabled=test_plan_generation_enabled,
            test_case_generation_enabled=test_case_generation_enabled,
            timeout_seconds=timeout_seconds,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.session.add(new_cfg)
        await self.session.flush()
        logger.info(
            "KnowledgeConfigRepository.upsert_for_user: 新建配置 | user_id=%d | base=%s",
            user_id, api_base_url[:60],
        )
        return new_cfg

    async def update_test_status(
        self,
        *,
        user_id: int,
        status: str,
        message: str | None,
    ) -> None:
        """Update the latest connection test status without mutating config."""
        result = await self.session.execute(
            select(KnowledgeConfig)
            .where(
                KnowledgeConfig.user_id == user_id,
                KnowledgeConfig.status == "active",
            )
            .order_by(KnowledgeConfig.updated_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.last_test_status = status
        row.last_test_message = message
        row.last_test_at = utcnow()
        await self.session.flush()

# 模块定位:KnowledgeConfig 仓储
#
# 链路:
#   KnowledgeConfigService.get_for_user(user_id) → per-user 优先 / shared 兜底
#   api/v1/settings PUT → encrypt_api_key + write
#
# 关键约束:
#   - api_key 走 crypto.encrypt_api_key(...);
#   - user_internal_id 可空表示 system-shared;
#   - 测试时 kms decrypt key 必须有 fixture。
