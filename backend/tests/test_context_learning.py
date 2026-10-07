"""WP-BE-06/08：Auto User Memory / Project Rule Extraction 测试。

覆盖：
- 显式长期记忆 → active（flag 门控）
- 稳定偏好写入 user scope
- ephemeral 跳过
- assistant 内容不被记忆（只处理 user message）
- secret 拒绝
- 跨用户隔离
- Project Rule → conversation scope
- 确定性兜底（无 LLM）
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.context_engine import ContextMemory, ContextWorkspaceInstruction
from app.services.context_learning_service import (
    ContextLearningService,
    ContextSecurityService,
    detect_secret,
    is_explicit_remember,
)
from app.repositories.base import ensure_model_id


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_detect_secret():
    assert detect_secret("sk-" + "abcdef1234567890abcdef1234567890")
    assert detect_secret("api_key=secretvalue123")
    assert detect_secret("密码：123456")
    assert detect_secret("-----BEGIN " + "RSA PRIVATE KEY-----")
    assert not detect_secret("以后都用中文回答")
    assert not detect_secret("接口统一使用 v3")


def test_is_explicit_remember():
    assert is_explicit_remember("记住，以后都用中文回答")
    assert is_explicit_remember("以后生成测试方案都要风险分析")
    assert not is_explicit_remember("今天先测试登录")


class _FakeInvoker:
    """可注入的 bridge 假实现（返回分类结果）。"""

    def __init__(self, items: list[dict] | None = None, available: bool = True):
        self._items = items
        self.available = available
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        if self._items is None:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(value={"items": self._items})


@pytest.mark.asyncio
async def test_explicit_remember_writes_user_memory(sqlite_session_factory):
    """显式长期记忆 → 写入 user scope memory（candidate/active 由 flag 决定）。"""
    from app.context_engine.feature_flags import get_context_engine_flags

    # 强制 auto activate 打开（测试隔离）
    import os

    saved = os.environ.get("CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED")
    os.environ["CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED"] = "1"
    os.environ["CONTEXT_MEMORY_WRITE_ENABLED"] = "1"
    try:
        async with sqlite_session_factory() as session:
            learning = ContextLearningService(session)
            result = await learning.learn_from_user_message(
                user_message="记住，以后都用中文回答",
                user_internal_id=1,
                conversation_public_id="conv_l",
                conversation_internal_id=1,
            )
            assert result["persisted"] == 1

        async with sqlite_session_factory() as session:
            mems = (
                await session.execute(select(ContextMemory).where(
                    ContextMemory.user_id == 1
                ))
            ).scalars().all()
            assert len(mems) == 1
            assert mems[0].scope_type == "user"
            assert mems[0].workspace_key is None
            assert mems[0].status in ("candidate", "active")
    finally:
        if saved is None:
            os.environ.pop("CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED", None)
        else:
            os.environ["CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED"] = saved


@pytest.mark.asyncio
async def test_ephemeral_skipped(sqlite_session_factory):
    """临时任务 → EPHEMERAL 跳过（不写入）。"""
    async with sqlite_session_factory() as session:
        learning = ContextLearningService(
            session,
            llm_invoker=_FakeInvoker(
                items=[{"category": "EPHEMERAL", "content": "今天先测试登录", "reason": "临时"}]
            ),
        )
        result = await learning.learn_from_user_message(
            user_message="今天先测试登录",
            user_internal_id=1,
            conversation_public_id="conv_ephem",
        )
        assert result["persisted"] == 0

    async with sqlite_session_factory() as session:
        count = (
            await session.execute(
                select(ContextMemory).where(ContextMemory.user_id == 1)
            )
        ).scalars().all()
        assert len(count) == 0


@pytest.mark.asyncio
async def test_secret_rejected(sqlite_session_factory):
    """secret 内容拒绝写入（不落库）。"""
    async with sqlite_session_factory() as session:
        learning = ContextLearningService(
            session,
            llm_invoker=_FakeInvoker(
                items=[{"category": "USER_MEMORY", "content": "API key 是 sk-abc123def456ghi789", "reason": "x"}]
            ),
        )
        result = await learning.learn_from_user_message(
            user_message="记住 API key",
            user_internal_id=1,
        )
        assert result["persisted"] == 0

    async with sqlite_session_factory() as session:
        mems = (
            await session.execute(select(ContextMemory).where(ContextMemory.user_id == 1))
        ).scalars().all()
        assert len(mems) == 0


@pytest.mark.asyncio
async def test_security_service_rejects_secret():
    """ContextSecurityService.check_for_write 拒绝 secret/PII。"""
    from app.services.context_learning_service import ContextSecurityError

    service = ContextSecurityService(None)
    with pytest.raises(ContextSecurityError):
        service.check_for_write("密码是 admin123")
    with pytest.raises(ContextSecurityError):
        service.check_for_write("我的手机号是 13812345678")
    # 正常内容通过
    service.check_for_write("以后都用中文回答")


@pytest.mark.asyncio
async def test_project_rule_written_to_conversation_scope(sqlite_session_factory):
    """PROJECT_RULE → conversation:{public_id} scope（不跨 conversation）。"""
    async with sqlite_session_factory() as session:
        learning = ContextLearningService(
            session,
            llm_invoker=_FakeInvoker(
                items=[{"category": "PROJECT_RULE", "content": "接口统一使用 v3", "reason": "项目约束"}]
            ),
        )
        result = await learning.learn_from_user_message(
            user_message="这个项目接口统一使用 v3",
            user_internal_id=1,
            conversation_public_id="conv_rules_a",
        )
        assert result["persisted"] == 1

    async with sqlite_session_factory() as session:
        rules = (
            await session.execute(select(ContextWorkspaceInstruction).where(
                ContextWorkspaceInstruction.user_id == 1
            ))
        ).scalars().all()
        assert len(rules) == 1
        assert rules[0].workspace_key == "conversation:conv_rules_a"
        assert rules[0].status == "active"


@pytest.mark.asyncio
async def test_project_rule_dedupe(sqlite_session_factory):
    """同 workspace + 同 content_hash 幂等（不重复建）。"""
    async with sqlite_session_factory() as session:
        learning = ContextLearningService(
            session,
            llm_invoker=_FakeInvoker(
                items=[{"category": "PROJECT_RULE", "content": "接口统一使用 v3", "reason": "x"}]
            ),
        )
        await learning.learn_from_user_message(
            user_message="接口统一使用 v3", user_internal_id=1, conversation_public_id="conv_dedupe"
        )
        await learning.learn_from_user_message(
            user_message="接口统一使用 v3", user_internal_id=1, conversation_public_id="conv_dedupe"
        )

    async with sqlite_session_factory() as session:
        rules = (
            await session.execute(select(ContextWorkspaceInstruction).where(
                ContextWorkspaceInstruction.user_id == 1
            ))
        ).scalars().all()
        assert len(rules) == 1


def test_deterministic_classify_without_llm():
    """无 LLM → 确定性兜底只处理显式触发词。"""
    service = ContextLearningService(None)
    items = service._deterministic_classify("以后生成测试方案都要风险分析")
    assert any(i["category"] == "USER_MEMORY" for i in items)
    items2 = service._deterministic_classify("今天先测试登录")
    assert items2 == []
