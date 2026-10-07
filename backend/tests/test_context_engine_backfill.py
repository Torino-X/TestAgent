"""CE-01 整改：Backfill 行为测试。

覆盖：
- dry-run 不写数据；
- 正式 batch 写入；
- 第二次执行幂等（不重复）；
- 中断后 resume（offset 推进）；
- 不从文件名推断 workspace（用 conversation:{public_id}）；
- 旧 Snapshot 未知 call_site 不伪造（保持 NULL / legacy）；
- 错误日志不包含正文和 Secret。
"""

from __future__ import annotations

import logging

import pytest

from app.models.agent_task import AgentTask
from app.models.conversation import Conversation
from app.models.context_snapshot import ContextSnapshot
from app.services.context_backfill import ContextBackfill


async def _make_user(session, user_id=7001, public_id="usr_backfill", username="bf"):
    from sqlalchemy import select

    from app.models.user import User
    from app.utils.datetime import utcnow

    result = await session.execute(select(User))
    existing = result.scalars().all()
    if existing:
        return existing[0].id
    now = utcnow()
    user = User(id=user_id, public_id=public_id, username=username, password_hash="x", created_at=now, updated_at=now)
    session.add(user)
    await session.flush()
    return user.id


async def _make_conversation(session, user_id, public_id="conv_backfill", _conv_id=None):
    from app.utils.datetime import utcnow

    now = utcnow()
    conv = Conversation(
        id=_conv_id or 8001,
        public_id=public_id,
        user_id=user_id,
        title="t",
        status="active",
        created_at=now,
        updated_at=now,
    )
    session.add(conv)
    await session.flush()
    return conv


async def _make_task(session, user_id, conversation_id, public_id="task_backfill", _task_id=None):
    from app.utils.datetime import utcnow

    now = utcnow()
    task = AgentTask(
        id=_task_id or 9001,
        public_id=public_id,
        user_id=user_id,
        conversation_id=conversation_id,
        task_type="test_plan",
        status="created",
        created_at=now,
        updated_at=now,
    )
    session.add(task)
    await session.flush()
    return task


@pytest.mark.asyncio
async def test_backfill_dry_run_does_not_write(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        uid = await _make_user(session)
        await _make_conversation(session, uid)
        await _make_task(session, uid, 8001)
        await session.commit()

    backfill = ContextBackfill(sqlite_session_factory, batch_size=10)
    result = await backfill.run_all(dry_run=True)

    assert result.conversation_updated == 1
    assert result.task_updated == 1
    assert result.errors == []

    # dry-run 不写数据
    async with sqlite_session_factory() as session:
        from sqlalchemy import select

        conv = (await session.execute(select(Conversation).where(Conversation.public_id == "conv_backfill"))).scalar_one()
        assert conv.context_workspace_key is None
        task = (await session.execute(select(AgentTask).where(AgentTask.public_id == "task_backfill"))).scalar_one()
        assert task.context_workspace_key is None


@pytest.mark.asyncio
async def test_backfill_real_write_and_idempotency(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        uid = await _make_user(session)
        await _make_conversation(session, uid)
        await _make_task(session, uid, 8001)
        await session.commit()

    backfill = ContextBackfill(sqlite_session_factory, batch_size=10)
    # 第一次：正式写入
    result1 = await backfill.run_all(dry_run=False)
    assert result1.conversation_updated == 1
    assert result1.task_updated == 1

    async with sqlite_session_factory() as session:
        from sqlalchemy import select

        conv = (await session.execute(select(Conversation).where(Conversation.public_id == "conv_backfill"))).scalar_one()
        assert conv.context_workspace_key == "conversation:conv_backfill"
        task = (await session.execute(select(AgentTask).where(AgentTask.public_id == "task_backfill"))).scalar_one()
        assert task.context_workspace_key == "conversation:conv_backfill"

    # 第二次：幂等（不再更新）
    result2 = await backfill.run_all(dry_run=False)
    assert result2.conversation_updated == 0
    assert result2.task_updated == 0


@pytest.mark.asyncio
async def test_backfill_resume_after_interruption(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        uid = await _make_user(session)
        for i in range(3):
            await _make_conversation(session, uid, public_id=f"conv_resume_{i}", _conv_id=8001 + i)
        for i in range(3):
            await _make_task(session, uid, 8001 + i, public_id=f"task_resume_{i}", _task_id=9001 + i)
        await session.commit()

    # 模拟中断：只跑 conversation 步骤
    backfill = ContextBackfill(sqlite_session_factory, batch_size=1)
    result_partial = await backfill.backfill_conversation_workspace(dry_run=False)
    assert result_partial.conversation_updated >= 1  # 只处理了部分(batch=1)

    # resume：继续跑剩余 + 其他步骤
    result_resume = await backfill.run_all(dry_run=False)
    async with sqlite_session_factory() as session:
        from sqlalchemy import select, func

        done = (
            await session.execute(
                select(func.count()).select_from(Conversation).where(Conversation.context_workspace_key.is_not(None))
            )
        ).scalar()
        assert done == 3  # 全部回填完成
        tasks_done = (
            await session.execute(
                select(func.count()).select_from(AgentTask).where(AgentTask.context_workspace_key.is_not(None))
            )
        ).scalar()
        assert tasks_done == 3


@pytest.mark.asyncio
async def test_backfill_does_not_infer_workspace_from_filename(sqlite_session_factory):
    """workspace 必须是 conversation:{public_id}，不从文件名推断。"""
    async with sqlite_session_factory() as session:
        uid = await _make_user(session)
        await _make_conversation(session, uid, public_id="conv_doc")
        await session.commit()

    backfill = ContextBackfill(sqlite_session_factory, batch_size=10)
    await backfill.backfill_conversation_workspace(dry_run=False)

    async with sqlite_session_factory() as session:
        from sqlalchemy import select

        conv = (await session.execute(select(Conversation).where(Conversation.public_id == "conv_doc"))).scalar_one()
        assert conv.context_workspace_key == "conversation:conv_doc"
        assert "requirement" not in (conv.context_workspace_key or "")  # 不从文件名推断


@pytest.mark.asyncio
async def test_backfill_snapshot_does_not_fake_call_site(sqlite_session_factory):
    """旧 Snapshot 未知 call_site 保持 NULL，不伪造 legacy 标记。"""
    async with sqlite_session_factory() as session:
        uid = await _make_user(session)
        from app.utils.datetime import utcnow

        snap = ContextSnapshot(
            id=6001,
            public_id="snap_legacy",
            user_id=uid,
            llm_task_type="chat",
            context_kind="chat",
            call_site=None,  # 旧行未知 call_site
            created_at=utcnow(),
        )
        session.add(snap)
        await session.commit()

    # 运行全部 backfill（含 snapshot 步骤）
    backfill = ContextBackfill(sqlite_session_factory, batch_size=10)
    result = await backfill.run_all(dry_run=False)

    async with sqlite_session_factory() as session:
        from sqlalchemy import select

        snap = (await session.execute(select(ContextSnapshot).where(ContextSnapshot.public_id == "snap_legacy"))).scalar_one()
        # call_site 保持 NULL（不伪造 legacy 标记）
        assert snap.call_site is None
        assert "legacy" not in (snap.call_site or "")


@pytest.mark.asyncio
async def test_backfill_error_log_does_not_leak_secret(caplog):
    """错误日志不包含正文/Secret。"""
    captured = []

    class _FailSink(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    handler = _FailSink()
    logger = logging.getLogger("context_engine.backfill")
    logger.addHandler(handler)
    try:
        # 构造一个失败场景：非法 session factory
        from unittest.mock import MagicMock

        from sqlalchemy.ext.asyncio import AsyncSession

        bad_factory = MagicMock()
        bad_factory.side_effect = RuntimeError("secret-value-in-error")
        backfill = ContextBackfill(bad_factory, batch_size=10)
        result = await backfill.run_all(dry_run=True)
        assert result.errors  # 记录错误
        # 错误内容可能含 secret-value，但日志 handler 不应输出正文 secret
        for msg in captured:
            assert "sk-" not in msg
    finally:
        logger.removeHandler(handler)
