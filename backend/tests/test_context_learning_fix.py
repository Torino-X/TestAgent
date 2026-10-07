"""ContextLearning classify failure 修复 — targeted tests。

覆盖：
- TEST-01 普通"你好"分类 EPHEMERAL，不写 Memory/Rule，不报错
- TEST-02 "以后都用中文回答" → USER_MEMORY，scope_type='user', workspace_key=NULL
- TEST-03 "这个项目所有接口统一使用 v4" → PROJECT_RULE，conversation scope
- TEST-04 Secret 内容拒绝写入 Memory/Rule
- TEST-05 LLM classify failure → chat 主流程不受影响（soft-async）
- TEST-06 _classify 透传 conversation_id + runtime_context（根因修复验证）
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.context_learning_service import (
    ContextLearningService,
    detect_secret,
    is_explicit_remember,
)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _StubBridge:
    """最小 ContextInvokerBridge 替身：记录 generate 参数，可注入返回值/异常。"""

    def __init__(self, *, result=None, raise_exc=None):
        self.available = True
        self._result = result
        self._raise_exc = raise_exc
        self.calls: list[dict] = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


class _StubResult:
    def __init__(self, value):
        self.value = value


@pytest.mark.asyncio
async def test_ephemeral_not_persisted(sqlite_session_factory):
    """TEST-01: '你好' → EPHEMERAL，不写 Memory/Rule，不报错。"""
    session = sqlite_session_factory()
    bridge = _StubBridge(
        result=_StubResult({'items': [{'category': 'EPHEMERAL', 'content': '你好', 'reason': 'greeting'}]})
    )
    service = ContextLearningService(session, llm_invoker=bridge)
    result = await service.learn_from_user_message(
        user_message='你好',
        user_internal_id=1,
        conversation_public_id='conv_ephemeral',
        conversation_internal_id=1,
    )
    assert result["error"] is None, f"应无错误，got {result}"
    assert result["persisted"] == 0
    await session.close()


@pytest.mark.asyncio
async def test_user_memory_persisted_user_scope(sqlite_session_factory):
    """TEST-02: '以后都用中文回答' → USER_MEMORY，scope_type='user', workspace_key=NULL。"""
    session = sqlite_session_factory()
    bridge = _StubBridge(
        result=_StubResult({'items': [{'category': 'USER_MEMORY', 'content': '以后都用中文回答', 'reason': 'preference'}]})
    )
    service = ContextLearningService(session, llm_invoker=bridge)
    result = await service.learn_from_user_message(
        user_message='以后都用中文回答',
        user_internal_id=1,
        conversation_public_id='conv_mem',
        conversation_internal_id=2,
    )
    assert result["error"] is None
    # 默认 auto_activate 关 → 写入 candidate，scope 必须正确
    from app.models.context_engine import ContextMemory
    from sqlalchemy import select

    rows = (await session.execute(select(ContextMemory))).scalars().all()
    assert rows, "应写入 User Memory candidate"
    assert rows[0].scope_type == "user"
    assert rows[0].workspace_key is None
    assert rows[0].created_at is not None
    assert rows[0].updated_at is not None
    await session.close()


@pytest.mark.asyncio
async def test_explicit_remember_bypasses_llm_project_rule_and_becomes_active_user_memory(
    sqlite_session_factory, monkeypatch,
):
    """Explicit consent must become cross-conversation User Memory."""
    monkeypatch.setenv("CONTEXT_MEMORY_WRITE_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED", "1")
    session = sqlite_session_factory()
    user_message = "以后生成测试方案时不得编造数据，每章不超过 50 字。记住这些规则。"
    bridge = _StubBridge(
        result=_StubResult({
            "items": [{
                "category": "PROJECT_RULE",
                "key": "no_fabricated_data",
                "content": "不得编造数据",
                "reason": "constraint",
            }]
        })
    )
    service = ContextLearningService(session, llm_invoker=bridge)

    result = await service.learn_from_user_message(
        user_message=user_message,
        user_internal_id=1,
        conversation_public_id="conv_explicit_memory",
        conversation_internal_id=8,
    )

    from app.models.context_engine import ContextMemory, ContextWorkspaceInstruction
    from sqlalchemy import select

    memories = (await session.execute(select(ContextMemory))).scalars().all()
    rules = (await session.execute(select(ContextWorkspaceInstruction))).scalars().all()
    assert result == {"persisted": 1, "error": None}
    assert bridge.calls == []
    assert len(memories) == 1
    assert memories[0].content == user_message
    assert memories[0].status == "active"
    assert rules == []
    await session.close()


@pytest.mark.asyncio
async def test_new_explicit_preference_supersedes_active_memory_with_same_topic(
    sqlite_session_factory, monkeypatch,
):
    """A revised durable preference must replace, not compete with, its prior value."""
    monkeypatch.setenv("CONTEXT_MEMORY_WRITE_ENABLED", "1")
    monkeypatch.setenv("CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED", "1")
    session = sqlite_session_factory()
    service = ContextLearningService(session)

    first = "请记住：生成测试方案时，每个章节字数至少达到100个。"
    revised = "请记住：生成测试方案时，每个章节字数至少达到500个。"
    await service.learn_from_user_message(
        user_message=first,
        user_internal_id=1,
        conversation_public_id="conv_preference_revision",
        conversation_internal_id=18,
    )
    await service.learn_from_user_message(
        user_message=revised,
        user_internal_id=1,
        conversation_public_id="conv_preference_revision",
        conversation_internal_id=18,
    )

    from app.models.context_engine import ContextMemory
    from sqlalchemy import select

    rows = list((await session.execute(
        select(ContextMemory).order_by(ContextMemory.version.asc())
    )).scalars().all())
    assert len(rows) == 2
    old, current = rows
    assert old.status == "superseded"
    assert old.archived_at is not None
    assert current.status == "active"
    assert current.content == revised
    assert current.dedupe_key == "test_plan_chapter_min_words"
    assert current.supersedes_memory_id == old.id
    assert current.version == old.version + 1
    await session.close()


@pytest.mark.asyncio
async def test_project_rule_conversation_scope(sqlite_session_factory):
    """TEST-03: '这个项目所有接口统一使用 v4' → PROJECT_RULE，conversation scope。"""
    session = sqlite_session_factory()
    bridge = _StubBridge(
        result=_StubResult({
            'items': [{
                'category': 'PROJECT_RULE', 'key': 'api_version',
                'content': '这个项目所有接口统一使用 v4', 'reason': 'constraint',
            }]
        })
    )
    service = ContextLearningService(session, llm_invoker=bridge)
    result = await service.learn_from_user_message(
        user_message='这个项目所有接口统一使用 v4',
        user_internal_id=1,
        conversation_public_id='conv_rule',
        conversation_internal_id=3,
        allow_project_rule=True,
    )
    assert result["error"] is None
    from app.models.context_engine import ContextWorkspaceInstruction
    from sqlalchemy import select

    rows = (await session.execute(select(ContextWorkspaceInstruction))).scalars().all()
    assert rows, "应写入 Project Rule"
    assert rows[0].workspace_key == "conversation:conv_rule"
    assert rows[0].status == "active"
    await session.close()


@pytest.mark.asyncio
async def test_secret_denied(sqlite_session_factory):
    """TEST-04: Secret 内容不得写入 Memory/Rule。"""
    assert detect_secret("我的密码是 abc123") is True
    session = sqlite_session_factory()
    bridge = _StubBridge(
        result=_StubResult({
            'items': [{'category': 'USER_MEMORY', 'content': '我的密码是 abc123', 'reason': 'credential'}]
        })
    )
    service = ContextLearningService(session, llm_invoker=bridge)
    result = await service.learn_from_user_message(
        user_message='我的密码是 abc123',
        user_internal_id=1,
        conversation_public_id='conv_secret',
        conversation_internal_id=4,
    )
    assert result["persisted"] == 0
    from app.models.context_engine import ContextMemory
    from sqlalchemy import select

    rows = (await session.execute(select(ContextMemory))).scalars().all()
    assert len(rows) == 0, "Secret 不得写入 Memory"
    await session.close()

@pytest.mark.asyncio
async def test_llm_failure_soft_async(sqlite_session_factory, caplog):
    """TEST-05: LLM classify 抛异常 → 走确定性兜底，不向外抛，不破坏主流程。"""
    from app.context_engine.errors import ContextEngineError, ContextEngineFailure, ContextEngineStage

    session = sqlite_session_factory()
    exc = ContextEngineFailure(
        ContextEngineError(
            code="context.source.no_conversation",
            detail="conversation_id 缺失",
            stage=ContextEngineStage.SOURCE,
        )
    )
    bridge = _StubBridge(raise_exc=exc)
    service = ContextLearningService(session, llm_invoker=bridge)
    # learn_from_user_message 绝不抛异常
    result = await service.learn_from_user_message(
        user_message='以后都用中文回答',
        user_internal_id=1,
        conversation_public_id='conv_soft',
        conversation_internal_id=5,
    )
    assert result is not None
    assert "error" in result
    await session.close()


@pytest.mark.asyncio
async def test_classify_passes_conversation_id_and_runtime_context(sqlite_session_factory):
    """TEST-06: 根因修复 — _classify 把 conversation_id + runtime_context 透传给 bridge。

    bridge.generate 收到的 conversation_id 必须等于传入值；runtime_context 非 None
    （携带 session_factory），从而避免 context.source.no_conversation。
    """
    session = sqlite_session_factory()

    async def _sf():
        return session

    bridge = _StubBridge(result=_StubResult({'items': []}))
    service = ContextLearningService(
        session, llm_invoker=bridge,
        session_factory=_sf,
    )
    await service._classify(
        "你好",
        "old context " * 200,
        user_internal_id=42,
        conversation_internal_id=154,
    )
    assert bridge.calls, "bridge.generate 应被调用"
    call = bridge.calls[0]
    assert call["conversation_id"] == 154
    assert call["user_id"] == 42
    assert call["current_goal"] == "你好"
    assert call["user_content"] == "你好"
    assert call["runtime_context"] is not None
    assert getattr(call["runtime_context"], "user_internal_id", None) == 42
    assert getattr(call["runtime_context"], "conversation_internal_id", None) == 154
    assert getattr(call["runtime_context"], "session_factory", None) is not None
    await session.close()


@pytest.mark.asyncio
async def test_no_conversation_failure_logged_with_code_stage(sqlite_session_factory, caplog):
    """TEST-06b: ContextEngineFailure 日志应包含 code/stage（增强定位能力，不泄露 detail）。"""
    from app.context_engine.errors import ContextEngineError, ContextEngineFailure, ContextEngineStage

    session = sqlite_session_factory()
    exc = ContextEngineFailure(
        ContextEngineError(
            code="context.source.no_conversation",
            detail="conversation_id 缺失",
            stage=ContextEngineStage.SOURCE,
        )
    )
    bridge = _StubBridge(raise_exc=exc)
    service = ContextLearningService(session, llm_invoker=bridge)
    await service._classify("你好", None, user_internal_id=1, conversation_internal_id=None)
    # _classify 内部日志包含 stage + code，不包含原始 detail（不泄露）
    logs = [r.message for r in caplog.records]
    joined = "\n".join(logs)
    assert "stage=source" in joined
    assert "code=context.source.no_conversation" in joined
    assert "type=ContextEngineFailure" in joined
    await session.close()
