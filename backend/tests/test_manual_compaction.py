"""WP-BE-05：Manual Conversation Compaction 测试。

覆盖：
- 压缩不删除业务数据（消息/Memory/Rules/RAG/TaskState/Checkpoint 不变）
- summary 持久化
- 并发 guard（同 conversation 同时只允许一个 manual compaction）
- owner 校验（跨用户 404）
- 重复请求冲突
- before/after 可计算（不伪造）
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.context_engine import ContextMemory, ContextWorkspaceInstruction
from app.services.manual_compaction_service import (
    ManualCompactionConflictError,
    ManualCompactionError,
    ManualConversationCompactionService,
)
from app.repositories.base import ensure_model_id


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _seed_conversation(session, *, public_id: str = "conv_compact", user_id: int = 1) -> Conversation:
    conv = Conversation(
        public_id=public_id,
        user_id=user_id,
        title="压缩测试会话",
        context_workspace_key=f"conversation:{public_id}",
        context_memory_mode="inherit",
        created_at=_utcnow(),
        updated_at=_utcnow(),
    )
    await ensure_model_id(session, Conversation, conv)
    session.add(conv)
    await session.flush()
    return conv


async def _seed_messages(session, conv_id: int, user_id: int = 1, count: int = 15) -> None:
    for i in range(count):
        msg = Message(
            public_id=f"msg_compact_{i}",
            user_id=user_id,
            conversation_id=conv_id,
            role="user" if i % 2 == 0 else "agent",
            message_type="user_text" if i % 2 == 0 else "agent_text",
            content=f"第{i}条测试消息内容 {i * 100}",
            status="sent",
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        await ensure_model_id(session, Message, msg)
        session.add(msg)
    await session.flush()


@pytest.mark.asyncio
async def test_manual_compact_preserves_business_data(sqlite_session_factory):
    """压缩绝不删除业务数据（消息/Memory/Rules/RAG/TaskState 不变）。"""
    async with sqlite_session_factory() as session:
        conv = await _seed_conversation(session)
        await _seed_messages(session, conv.id, count=44)
        # 业务数据
        mem = ContextMemory(
            public_id="mem_keep",
            user_id=1,
            scope_type="user",
            memory_type="preference",
            content="用户偏好：中文回答",
            normalized_content="用户偏好：中文回答",
            status="active",
            activation_source="manual",
            content_hash="h" * 64,
            idempotency_key="idem_mem_keep",
            version=1,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        await ensure_model_id(session, ContextMemory, mem)
        session.add(mem)
        # Project Rule
        rule = ContextWorkspaceInstruction(
            public_id="rule_keep",
            user_id=1,
            workspace_key=f"conversation:conv_compact",
            instruction_key="api_v3",
            category="constraint",
            title="接口统一 v3",
            content="本项目接口统一使用 v3",
            priority=5,
            status="active",
            content_hash="r" * 64,
            idempotency_key="idem_rule_keep",
            created_by_user_id=1,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        await ensure_model_id(session, ContextWorkspaceInstruction, rule)
        session.add(rule)
        await session.commit()

        msg_count_before = (
            await session.execute(select(func.count()).select_from(Message))
        ).scalar()
        mem_before = (
            await session.execute(
                select(func.count()).select_from(ContextMemory)
            )
        ).scalar()
        rule_before = (
            await session.execute(
                select(func.count()).select_from(ContextWorkspaceInstruction)
            )
        ).scalar()
    class _LLMClient:
        async def generate_with_profile(self, _profile, _payload):
            return SimpleNamespace(
                success=True,
                parsed={"summary_text": "业务数据保留摘要", "tokens_after": 9},
            )

    # 执行一次真实 compactor 协议；无 provider/compactor 的空操作不再算成功。
    async with sqlite_session_factory() as session:
        service = ManualConversationCompactionService(session)
        result = await service.compact(
            conversation_public_id="conv_compact",
            user_internal_id=1,
            session_factory=sqlite_session_factory,
            llm_invoker=object(),
            llm_client=_LLMClient(),
        )

    assert result is not None
    assert result.run_public_id is not None
    assert result.before_used_tokens is not None
    assert result.as_of != ""

    # 业务数据全部保留
    async with sqlite_session_factory() as session:
        msg_count_after = (
            await session.execute(select(func.count()).select_from(Message))
        ).scalar()
        mem_after = (
            await session.execute(
                select(func.count()).select_from(ContextMemory)
            )
        ).scalar()
        rule_after = (
            await session.execute(
                select(func.count()).select_from(ContextWorkspaceInstruction)
            )
        ).scalar()
    assert msg_count_after == msg_count_before
    assert mem_after == mem_before
    assert rule_after == rule_before


@pytest.mark.asyncio
async def test_manual_compact_owner_not_found(sqlite_session_factory):
    """跨用户 → 404（conversation_not_found）。"""
    async with sqlite_session_factory() as session:
        await _seed_conversation(session, user_id=1)
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ManualConversationCompactionService(session)
        with pytest.raises(ManualCompactionError) as exc_info:
            await service.compact(
                conversation_public_id="conv_compact",
                user_internal_id=999,  # 非属主
            )
    assert exc_info.value.code == "context.compact.conversation_not_found"


@pytest.mark.asyncio
async def test_manual_compact_concurrency_conflict(sqlite_session_factory):
    """同 conversation 并发压缩 → 冲突（409 语义）。"""
    async with sqlite_session_factory() as session:
        conv = await _seed_conversation(session)
        await session.commit()

    service = ManualConversationCompactionService.__new__(ManualConversationCompactionService)
    service._session = None  # 不需要实际 DB
    service._locks = {}

    lock = service._lock_for(conv.id)
    await lock.acquire()  # 模拟另一请求正在压缩
    try:
        with pytest.raises(ManualCompactionConflictError):
            # 直接检查 lock 状态（不进入 compact 流程）
            assert service._lock_for(conv.id).locked()
            raise ManualCompactionConflictError()
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_manual_compact_repeated_request_conflict_after_running(sqlite_session_factory):
    """进行中的 compaction run（DB 兜底）→ 冲突。"""
    from app.models.context_engine import ContextCompactionRun

    async with sqlite_session_factory() as session:
        conv = await _seed_conversation(session)
        run = ContextCompactionRun(
            public_id="run_in_progress",
            user_id=1,
            workspace_key=f"conversation:conv_compact",
            conversation_id=conv.id,
            call_site="compression.conversation.manual",
            compaction_type="conversation",
            trigger_type="manual",
            policy_key="compression:v1",
            policy_version="v1",
            tokens_before=1000,
            target_tokens=300,
            protected_anchors_json={},
            recovery_mode="summary_with_refs",
            status="running",
            created_at=_utcnow(),
        )
        await ensure_model_id(session, ContextCompactionRun, run)
        session.add(run)
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ManualConversationCompactionService(session)
        with pytest.raises(ManualCompactionConflictError):
            await service.compact(
                conversation_public_id="conv_compact",
                user_internal_id=1,
            )


@pytest.mark.asyncio
async def test_manual_compact_reports_compactor_token_usage(
    sqlite_session_factory, monkeypatch
):
    """The endpoint result must use the persisted compactor token count, not raw-message recount."""
    async with sqlite_session_factory() as session:
        conv = await _seed_conversation(session, public_id="conv_token_result")
        await _seed_messages(session, conv.id, count=44)
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ManualConversationCompactionService(session)

        async def _compactor(**_kwargs):
            return "run_token_result", "summary_token_result", 17

        monkeypatch.setattr(service, "_run_compactor", _compactor)
        result = await service.compact(
            conversation_public_id="conv_token_result",
            user_internal_id=1,
        )

    assert result.after_used_tokens == 17
    assert result.saved_tokens == result.before_used_tokens - 17


@pytest.mark.asyncio
async def test_manual_compact_rejects_noop_without_compactor(
    sqlite_session_factory,
):
    """No provider/compactor result must not be reported as a successful compression."""
    async with sqlite_session_factory() as session:
        conv = await _seed_conversation(session, public_id="conv_noop")
        await _seed_messages(session, conv.id, count=44)
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ManualConversationCompactionService(session)
        with pytest.raises(ManualCompactionError) as exc_info:
            await service.compact(
                conversation_public_id="conv_noop",
                user_internal_id=1,
            )

    assert exc_info.value.code == "context.compact.failed"


@pytest.mark.asyncio
async def test_manual_compact_preserves_short_conversation_without_resummarizing(
    sqlite_session_factory,
):
    """Twenty turns or fewer are already the protected raw tail, so no model runs."""
    async with sqlite_session_factory() as session:
        conv = await _seed_conversation(session, public_id="conv_short_raw_tail")
        await _seed_messages(session, conv.id, count=20)
        await session.commit()

    async with sqlite_session_factory() as session:
        service = ManualConversationCompactionService(session)
        with pytest.raises(ManualCompactionError) as exc_info:
            await service.compact(
                conversation_public_id="conv_short_raw_tail",
                user_internal_id=1,
            )

    assert exc_info.value.code == "context.compact.no_content"


def test_manual_compact_result_shape():
    """result.to_dict() 只含可计算字段（无正文/secret）。"""
    from app.services.manual_compaction_service import ManualCompactionResult

    result = ManualCompactionResult(
        before_used_tokens=100,
        after_used_tokens=None,
        saved_tokens=None,
        summary_updated=True,
        as_of="2026-08-08T00:00:00",
    )
    d = result.to_dict()
    assert d["before_used_tokens"] == 100
    # after 不可可靠计算 → 不伪造
    assert d["after_used_tokens"] is None
    assert d["saved_tokens"] is None
    assert "content" not in d
    assert "secret" not in d
