"""Model config repository — per-user CRUD for the ``model_configs`` table.

F015 — each user has their own configuration.  There is no longer a
"global default" row in the application sense; rows with ``user_id
IS NULL`` are treated as system-shared visibility only (visible in
``list_all`` for the user who owns them, but never used to satisfy
``build_llm_config_provider`` for a different user).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import ModelConfig
from app.repositories.base import BaseRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id


class ModelConfigRepository(BaseRepository[ModelConfig]):
    model = ModelConfig

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session)

    async def get_active_for_user(self, user_id: int) -> ModelConfig | None:
        """Return the active config for the given user, or None.

        Filters strictly by ``user_id``; rows with ``user_id IS NULL``
        (system defaults) are NOT considered a fallback — those rows
        are only visible via ``list_all``.
        """
        result = await self.session.execute(
            select(ModelConfig)
            .where(
                ModelConfig.user_id == user_id,
                ModelConfig.status == "active",
            )
            .order_by(ModelConfig.is_default.desc(), ModelConfig.updated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_public_id(self, public_id: str) -> ModelConfig | None:
        result = await self.session.execute(
            select(ModelConfig).where(
                ModelConfig.public_id == public_id,
                ModelConfig.status == "active",
            )
        )
        return result.scalar_one_or_none()

    async def list_all(self, user_internal_id: int | None = None) -> list[ModelConfig]:
        """List active configs visible to the given user.

        Visibility:
          - rows where ``user_id == user_internal_id`` (the user's own configs)
          - rows where ``user_id IS NULL`` (system-shared, visible to all
            users for read-only display in the Settings UI; they can
            still create their own row alongside these)

        Rows belonging to OTHER users are never returned.
        """
        stmt = select(ModelConfig).where(ModelConfig.status == "active")
        if user_internal_id is not None:
            stmt = stmt.where(
                (ModelConfig.user_id == user_internal_id) | (ModelConfig.user_id.is_(None))
            )
        else:
            stmt = stmt.where(ModelConfig.user_id.is_(None))
        stmt = stmt.order_by(ModelConfig.is_default.desc(), ModelConfig.updated_at.desc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_by_capability(
        self,
        capability_type: str,
        user_internal_id: int | None = None,
    ) -> list[ModelConfig]:
        """按能力类型列出用户可见的 active 配置。

        CE-01: Embedding / Reranker / Compression 等能力配置查询。可见性与
        ``list_all`` 一致（用户自身 + user_id IS NULL 系统共享）。
        """
        stmt = (
            select(ModelConfig)
            .where(
                ModelConfig.status == "active",
                ModelConfig.capability_type == capability_type,
            )
            .order_by(ModelConfig.is_default.desc(), ModelConfig.updated_at.desc())
        )
        if user_internal_id is not None:
            stmt = stmt.where(
                (ModelConfig.user_id == user_internal_id) | (ModelConfig.user_id.is_(None))
            )
        else:
            stmt = stmt.where(ModelConfig.user_id.is_(None))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_default_by_capability(
        self,
        capability_type: str,
        user_internal_id: int | None = None,
    ) -> ModelConfig | None:
        """按能力返回默认配置（is_default 优先），无则返回最近更新的一条。

        CE-01: Embedding / Reranker 无独立 UI 时，用系统共享或用户默认行。
        """
        rows = await self.list_by_capability(capability_type, user_internal_id)
        if not rows:
            return None
        for row in rows:
            if row.is_default:
                return row
        return rows[0]

    async def get_active_for_capability(
        self, capability_type: str, user_id: int
    ) -> ModelConfig | None:
        """按能力返回用户 active 配置（默认优先）。

        CE-01 多能力管理：每种 capability 一个用户可有一个 default。
        """
        result = await self.session.execute(
            select(ModelConfig)
            .where(
                ModelConfig.user_id == user_id,
                ModelConfig.capability_type == capability_type,
                ModelConfig.status == "active",
                ModelConfig.enabled.is_(True),
            )
            .order_by(ModelConfig.is_default.desc(), ModelConfig.updated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def upsert_for_capability(
        self,
        *,
        user_id: int,
        capability_type: str,
        public_id: str | None,
        config_name: str,
        provider: str,
        api_base_url: str,
        api_key_encrypted: str,
        model_name: str,
        timeout_seconds: int,
        enable_thinking: bool,
        supports_vision: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        context_window_tokens: int | None = None,
        context_window_tokens_set: bool | None = None,
        default_max_output_tokens: int | None = None,
        embedding_dimension: int | None = None,
        normalize_embeddings: bool | None = None,
        rerank_instruction: str | None = None,
        pre_rerank_limit: int | None = None,
        score_type: str | None = None,
        enabled: bool = True,
        is_default: bool = True,
    ) -> ModelConfig:
        """按能力 upsert 用户配置。

        Per-user invariant: 每种 capability 最多一个 default=1 行。
        存在该能力现有 default 时更新之；否则新建。
        """
        existing = await self.get_active_for_capability(capability_type, user_id)

        if existing is not None:
            existing.config_name = config_name or existing.config_name
            existing.provider = provider
            existing.api_base_url = api_base_url
            if api_key_encrypted:
                existing.api_key_encrypted = api_key_encrypted
            existing.model_name = model_name
            existing.timeout_seconds = timeout_seconds or existing.timeout_seconds
            existing.enable_thinking = enable_thinking
            existing.supports_vision = supports_vision
            existing.capability_type = capability_type
            if temperature is not None:
                existing.temperature = temperature
            if max_tokens is not None:
                existing.max_tokens = max_tokens
            if context_window_tokens_set is None:
                context_window_tokens_set = context_window_tokens is not None
            if context_window_tokens_set:
                existing.context_window_tokens = context_window_tokens
            if default_max_output_tokens is not None:
                existing.default_max_output_tokens = default_max_output_tokens
            if embedding_dimension is not None:
                existing.embedding_dimension = embedding_dimension
            if normalize_embeddings is not None:
                existing.normalize_embeddings = normalize_embeddings
            if rerank_instruction is not None:
                existing.rerank_instruction = rerank_instruction
            if pre_rerank_limit is not None:
                existing.pre_rerank_limit = pre_rerank_limit
            if score_type is not None:
                existing.score_type = score_type
            existing.enabled = enabled
            existing.is_default = is_default
            await self.session.flush()
            return existing

        now = utcnow()
        new_cfg = ModelConfig(
            public_id=public_id or generate_public_id("model_config"),
            user_id=user_id,
            config_name=config_name or f"{capability_type} 模型配置",
            provider=provider,
            api_base_url=api_base_url,
            api_key_encrypted=api_key_encrypted or "",
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds or 120,
            enable_thinking=bool(enable_thinking),
            supports_vision=bool(supports_vision),
            capability_type=capability_type,
            context_window_tokens=context_window_tokens,
            default_max_output_tokens=default_max_output_tokens,
            embedding_dimension=embedding_dimension,
            normalize_embeddings=normalize_embeddings,
            rerank_instruction=rerank_instruction,
            pre_rerank_limit=pre_rerank_limit,
            score_type=score_type,
            enabled=bool(enabled),
            is_default=bool(is_default),
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.session.add(new_cfg)
        await self.session.flush()
        return new_cfg

    async def upsert_for_user(
        self,
        *,
        user_id: int,
        public_id: str | None,
        config_name: str,
        provider: str,
        api_base_url: str,
        api_key_encrypted: str,
        model_name: str,
        timeout_seconds: int,
        enable_thinking: bool,
        supports_vision: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        capability_type: str = "chat",
        context_window_tokens: int | None = None,
        context_window_tokens_set: bool | None = None,
        default_max_output_tokens: int | None = None,
        embedding_dimension: int | None = None,
        normalize_embeddings: bool | None = None,
    ) -> ModelConfig:
        """Insert or update this user's only ``is_default=1`` row.

        Per-user invariant: at most one row with ``user_id=`` ``user_id``
        and ``is_default=1``.  Unlike the legacy ``upsert_default``,
        this does NOT demote other users' defaults.
        """
        existing = await self.get_active_for_user(user_id)

        if existing is not None:
            existing.config_name = config_name or existing.config_name
            existing.provider = provider
            existing.api_base_url = api_base_url
            # Only overwrite the encrypted key when a non-empty value was passed
            if api_key_encrypted:
                existing.api_key_encrypted = api_key_encrypted
            existing.model_name = model_name
            existing.timeout_seconds = timeout_seconds or existing.timeout_seconds
            existing.enable_thinking = enable_thinking
            existing.supports_vision = supports_vision
            if temperature is not None:
                existing.temperature = temperature
            if max_tokens is not None:
                existing.max_tokens = max_tokens
            existing.capability_type = capability_type or existing.capability_type
            if context_window_tokens_set is None:
                context_window_tokens_set = context_window_tokens is not None
            if context_window_tokens_set:
                existing.context_window_tokens = context_window_tokens
            if default_max_output_tokens is not None:
                existing.default_max_output_tokens = default_max_output_tokens
            if embedding_dimension is not None:
                existing.embedding_dimension = embedding_dimension
            if normalize_embeddings is not None:
                existing.normalize_embeddings = normalize_embeddings
            await self.session.flush()
            return existing

        now = utcnow()
        new_cfg = ModelConfig(
            public_id=public_id or generate_public_id("model_config"),
            user_id=user_id,
            config_name=config_name or "默认模型配置",
            provider=provider,
            api_base_url=api_base_url,
            api_key_encrypted=api_key_encrypted or "",
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds or 120,
            enable_thinking=bool(enable_thinking),
            supports_vision=bool(supports_vision),
            capability_type=capability_type or "chat",
            context_window_tokens=context_window_tokens,
            default_max_output_tokens=default_max_output_tokens,
            embedding_dimension=embedding_dimension,
            normalize_embeddings=normalize_embeddings,
            is_default=True,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.session.add(new_cfg)
        await self.session.flush()
        return new_cfg


# 模块定位:ModelConfig 仓储(F015 per-user 模型配置,DB-only)
#
# 链路:
#   api/v1/settings/models → SettingsService.upsert / get_for_user
#   LLM 上游通过 settings_service.build_llm_config_provider 读
#
# 关键约束:
#   - user_internal_id 唯一索引(1 user 1 row);
#   - 数据库无 env fallback(F015 后);
#   - api_key 必须经 crypto;
#   - model 切要 reset cache。
