"""CE-01 SettingsService 能力字段持久化测试。

覆盖：ModelSettingsRequest 能力字段、SettingsService 校验非法能力类型、
repository capability 查询（用显式 id 绕过 SQLite BIGINT autoincrement）、
ORM 新列。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.config import ModelConfig
from app.repositories.model_config_repository import ModelConfigRepository
from app.schemas.settings import ModelSettingsRequest
from app.services.settings_service import SettingsService
from app.core.exceptions import ValidationError


def _make_user(session_factory, public_id="usr_ce01", username="ce01", user_id=9001):
    from sqlalchemy import select
    from app.models.user import User
    from app.utils.datetime import utcnow

    async def _do():
        async with session_factory() as session:
            result = await session.execute(select(User))
            existing = result.scalars().all()
            if existing:
                return existing[0].id
            now = utcnow()
            user = User(
                id=user_id,
                public_id=public_id,
                username=username,
                password_hash="x",
                display_name=username,
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user.id

    return _do()


def _model_config_orm(**overrides) -> ModelConfig:
    """构造带显式 id 的 ModelConfig ORM（SQLite 不会自动递增 BIGINT PK）。"""
    base = dict(
        id=100,
        public_id="mc_ce01",
        user_id=9001,
        config_name="我的模型配置",
        provider="openai-compatible",
        api_base_url="https://x",
        api_key_encrypted="enc",
        model_name="model",
        timeout_seconds=120,
        enable_thinking=False,
        supports_vision=False,
        is_default=True,
        status="active",
        capability_type="chat",
    )
    base.update(overrides)
    return ModelConfig(**base)


def _make_service(model_config: ModelConfig | None = None):
    """构造带 mock repository 的 SettingsService（复用现有测试模式）。"""
    session = MagicMock()
    session.flush = AsyncMock()
    session.refresh = AsyncMock()
    session.add = MagicMock()
    svc = SettingsService(session)
    mock_repo = MagicMock()
    mock_repo.get_active_for_user = AsyncMock(return_value=model_config)
    mock_repo.upsert_for_user = AsyncMock(return_value=model_config)
    svc._model_repo = mock_repo
    return svc, session, mock_repo


# ── Schema ───────────────────────────────────────────────────────


def test_model_settings_request_capability_fields():
    req = ModelSettingsRequest(
        api_base_url="https://x",
        model_name="m",
        capability_type="embedding",
        embedding_dimension=1536,
        context_window_tokens=200000,
        context_window_k=200,
    )
    assert req.capability_type == "embedding"
    assert req.embedding_dimension == 1536
    assert req.context_window_tokens == 200000
    assert req.context_window_k == 200


def test_model_settings_request_default_capability():
    req = ModelSettingsRequest(api_base_url="https://x", model_name="m")
    assert req.capability_type == "chat"


# ── ORM 新列 ─────────────────────────────────────────────────────


def test_model_config_orm_has_capability_columns(sqlite_engine):
    columns = {c.name for c in ModelConfig.__table__.columns}
    assert "capability_type" in columns
    assert "context_window_tokens" in columns
    assert "embedding_dimension" in columns
    assert "normalize_embeddings" in columns


@pytest.mark.asyncio
async def test_repository_capability_query(sqlite_session_factory):
    uid = await _make_user(sqlite_session_factory)
    async with sqlite_session_factory() as session:
        # SQLite 不会自动递增 BIGINT PK，必须显式给 id
        session.add_all(
            [
                _model_config_orm(
                    id=101,
                    public_id="mc_ce01_chat",
                    user_id=uid,
                    model_name="chat-model",
                    capability_type="chat",
                    context_window_tokens=200000,
                ),
                _model_config_orm(
                    id=102,
                    public_id="mc_ce01_emb",
                    user_id=uid,
                    model_name="embed-model",
                    capability_type="embedding",
                    embedding_dimension=1536,
                ),
            ]
        )
        await session.commit()

        repo = ModelConfigRepository(session)
        emb_rows = await repo.list_by_capability("embedding", uid)
        assert len(emb_rows) == 1
        assert emb_rows[0].embedding_dimension == 1536

        default_emb = await repo.get_default_by_capability("embedding", uid)
        assert default_emb is not None
        assert default_emb.capability_type == "embedding"

        # 系统共享行(user_id IS NULL)对用户可见
        session.add(
            _model_config_orm(
                id=103,
                public_id="mc_sys_emb",
                user_id=None,
                model_name="sys-embed",
                capability_type="embedding",
                embedding_dimension=768,
            )
        )
        await session.commit()
        sys_rows = await repo.list_by_capability("embedding", uid)
        assert len(sys_rows) == 2


@pytest.mark.asyncio
async def test_settings_service_invalid_capability_rejected(sqlite_session_factory):
    uid = await _make_user(sqlite_session_factory)
    async with sqlite_session_factory() as session:
        service = SettingsService(session)
        with pytest.raises(ValidationError):
            await service.update_model_settings(
                {
                    "api_base_url": "https://x",
                    "model_name": "m",
                    "capability_type": "quantum",
                },
                uid,
            )


@pytest.mark.asyncio
async def test_settings_service_persists_capability():
    # 使用 mock repository（现有测试模式，避免 SQLite BIGINT 限制）
    cfg = _model_config_orm(id=100, user_id=9001, capability_type="chat")
    svc, _, mock_repo = _make_service(model_config=cfg)

    async def _fake_upsert(**kwargs):
        return _model_config_orm(
            id=100,
            user_id=9001,
            capability_type=kwargs.get("capability_type", "chat"),
            embedding_dimension=kwargs.get("embedding_dimension"),
            context_window_tokens=kwargs.get("context_window_tokens"),
            model_name=kwargs.get("model_name", "m"),
        )

    mock_repo.upsert_for_user = AsyncMock(side_effect=_fake_upsert)

    result = await svc.update_model_settings(
        {
            "api_base_url": "https://x",
            "model_name": "embed-model",
            "capability_type": "embedding",
            "embedding_dimension": 1536,
            "context_window_tokens": 200000,
            "api_key": "sk-test-key",
        },
        9001,
    )
    assert result["configured"] is True
    assert result["capability_type"] == "embedding"
    assert result["embedding_dimension"] == 1536
    assert result["context_window_tokens"] == 200000
    assert result["context_window_k"] == 200
    assert result["api_key_masked"] != "sk-test-key"


@pytest.mark.asyncio
async def test_settings_service_persists_context_window_k_as_tokens():
    cfg = _model_config_orm(id=100, user_id=9001, capability_type="chat")
    svc, _, mock_repo = _make_service(model_config=cfg)

    async def _fake_upsert(**kwargs):
        return _model_config_orm(
            id=100,
            user_id=9001,
            context_window_tokens=kwargs.get("context_window_tokens"),
            model_name=kwargs.get("model_name", "m"),
        )

    mock_repo.upsert_for_user = AsyncMock(side_effect=_fake_upsert)

    result = await svc.update_model_settings(
        {
            "api_base_url": "https://x",
            "model_name": "qwen3.7-max",
            "context_window_k": 32,
            "api_key": "sk-test-key",
        },
        9001,
    )

    call_kwargs = mock_repo.upsert_for_user.call_args.kwargs
    assert call_kwargs["context_window_tokens"] == 32_000
    assert call_kwargs["context_window_tokens_set"] is True
    assert result["context_window_tokens"] == 32_000
    assert result["context_window_k"] == 32


@pytest.mark.asyncio
async def test_settings_service_can_clear_context_window_k():
    cfg = _model_config_orm(id=100, user_id=9001, context_window_tokens=32_000)
    svc, _, mock_repo = _make_service(model_config=cfg)

    async def _fake_upsert(**kwargs):
        return _model_config_orm(
            id=100,
            user_id=9001,
            context_window_tokens=kwargs.get("context_window_tokens"),
            model_name=kwargs.get("model_name", "m"),
        )

    mock_repo.upsert_for_user = AsyncMock(side_effect=_fake_upsert)

    result = await svc.update_model_settings(
        {
            "api_base_url": "https://x",
            "model_name": "qwen3.7-max",
            "context_window_k": None,
            "api_key": "sk-test-key",
        },
        9001,
    )

    call_kwargs = mock_repo.upsert_for_user.call_args.kwargs
    assert call_kwargs["context_window_tokens"] is None
    assert call_kwargs["context_window_tokens_set"] is True
    assert result["context_window_tokens"] is None
    assert result["context_window_k"] is None
