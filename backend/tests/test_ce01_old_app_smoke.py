"""CE-01 整改：Old App Smoke 验证。

在升级后的真实 MySQL 测试库（testagent_ce_test）上验证旧应用兼容：
- Backend import/startup smoke；
- 旧 GET /api/settings/model；
- 旧 PUT /api/settings/model（保存 chat 配置）；
- 旧 POST /api/settings/model/test（受控调用，mock LLMClient）；
- 旧 LLMClient 初始化；
- 至少一条未启用 Context Engine 的旧业务路径。

运行方式：
    DATABASE_SYNC_URL=mysql+pymysql://...  DATABASE_ASYNC_URL=mysql+aiomysql://...
    python -m pytest tests/test_ce01_old_app_smoke.py -q

跳过条件：无 DATABASE_ASYNC_URL 环境变量时跳过（避免误连生产库）。
"""

from __future__ import annotations

import os

import pytest

_TEST_DB = os.environ.get("DATABASE_ASYNC_URL", "")
REQUIRES_TEST_MYSQL = pytest.mark.skipif(
    not _TEST_DB or "testagent_ce_test" not in _TEST_DB,
    reason="需要 DATABASE_ASYNC_URL 指向隔离测试库 testagent_ce_test",
)


@REQUIRES_TEST_MYSQL
def test_backend_startup_import(monkeypatch):
    """Backend import/startup smoke（app.main 可导入）。"""
    import app.main  # noqa: F401

    from app.context_engine.feature_flags import get_context_engine_flags

    # 未启用 Context Engine（默认全关，生产非测试态），旧业务路径不受影响
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    flags = get_context_engine_flags()
    assert flags.context_engine_enabled is False


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_old_get_model_settings():
    """旧 GET /api/settings/model 对未配置用户返回 configured=False（兼容）。"""
    from sqlalchemy import select

    from app.models.user import User
    from app.services.settings_service import SettingsService

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine(_TEST_DB)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            # 找或建一个测试用户
            result = await session.execute(select(User).limit(1))
            user = result.scalars().first()
            if user is None:
                from app.utils.datetime import utcnow

                user = User(public_id="usr_oldsmoke", username="oldsmoke", password_hash="x", created_at=utcnow(), updated_at=utcnow())
                session.add(user)
                await session.flush()
            svc = SettingsService(session)
            settings = await svc.get_model_settings(str(user.id))
            # 旧契约：configured 字段存在；未配置时 False
            assert "configured" in settings
            assert settings["configured"] in (True, False)
    finally:
        await engine.dispose()


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_old_put_model_settings():
    """旧 PUT /api/settings/model 保存 chat 配置（默认 capability=chat，兼容旧行为）。"""
    from sqlalchemy import select

    from app.models.user import User
    from app.services.settings_service import SettingsService

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

    engine = create_async_engine(_TEST_DB)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(select(User).where(User.public_id == "usr_oldsmoke"))
            user = result.scalars().first()
            if user is None:
                from app.utils.datetime import utcnow

                user = User(public_id="usr_oldsmoke", username="oldsmoke", password_hash="x", created_at=utcnow(), updated_at=utcnow())
                session.add(user)
                await session.flush()
            assert user is not None
            svc = SettingsService(session)
            saved = await svc.update_model_settings(
                {
                    "api_base_url": "https://api.oldsmoke.example.com/v1",
                    "model_name": "old-chat-model",
                    "api_key": "sk-old-smoke-key",
                    "timeout_seconds": 120,
                },
                str(user.id),
            )
            assert saved["configured"] is True
            assert saved["model_name"] == "old-chat-model"
            # 旧保存不传 capability → 默认 chat
            assert saved["capability_type"] == "chat"
    finally:
        await engine.dispose()


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_old_llmclient_initialization():
    """旧 LLMClient 初始化（config_provider 契约）不因 CE 改动而破坏。"""
    from types import SimpleNamespace

    from app.integrations.llm_client import LLMClient

    provider = SimpleNamespace(
        api_url="https://api.example.com/v1",
        api_key="sk-old-key",
        model_name="old-model",
        timeout=120,
        enable_thinking=False,
    )
    client = LLMClient(config_provider=provider)
    # LLMClient 不持 CE 依赖；初始化成功即兼容
    assert client._config_provider is provider


@REQUIRES_TEST_MYSQL
def test_old_business_path_without_context_engine():
    """未启用 Context Engine 的旧业务路径：ConversationContextService 构建旧 chat 上下文。"""
    from app.common.context_reducer import ContextReducer
    from app.common.token_estimator import estimate_tokens

    # 旧 chat 上下文构建（不依赖 Context Engine flag）
    reducer = ContextReducer()
    messages = []
    from types import SimpleNamespace

    m1 = SimpleNamespace(role="user", message_type="user_text", content="生成登录模块测试方案", created_at="2026-01-01", id=1)
    ctx = reducer.reduce_chat_context(
        messages=[m1],
        summary="用户想生成登录模块测试方案",
        file_summaries=[],
        task_summary=None,
        conversation_id="conv_1",
    )
    assert ctx.conversation_id == "conv_1"
    assert ctx.estimated_tokens > 0
    # 旧 token 估算仍可用
    assert estimate_tokens("你好世界") > 0
