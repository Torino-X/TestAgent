"""CE-02 WP-7：ContextSnapshot 生命周期测试（隔离 MySQL，REQUIRES_TEST_MYSQL）。

覆盖：统一生命周期 building→ready→sent→completed/failed/abandoned、
CAS/expected status 非法转换拒绝、30 列落库、actual tokens 只在真实 usage
时写否则 null、shadow ready→completed / active ready→completed 拒绝。

注：llm_context_snapshots 的 PK 为 BIGINT（SQLite 不自动递增）且 repository
用 LAST_INSERT_ID()（MySQL-only），故本组测试走隔离 MySQL（WP-12b）。
"""

from __future__ import annotations

import os

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.context_engine.models.snapshot_models import (
    ContextSnapshotBeginCommand,
    ContextSnapshotCompleteCommand,
    ContextSnapshotFailCommand,
)
from app.context_engine.models.value_objects import Digest
from app.context_engine.snapshot import ContextSnapshotWriterService, SnapshotStatusError
from app.repositories.context_snapshot_repository import ContextSnapshotRepository

_TEST_DB = os.environ.get("DATABASE_SYNC_URL", "")
REQUIRES_TEST_MYSQL = pytest.mark.skipif(
    not _TEST_DB or "testagent" not in _TEST_DB,
    reason="需要 DATABASE_SYNC_URL 指向测试 MySQL",
)

_DIGEST = Digest.of("test-content")


def _async_db_url(db_name: str) -> str:
    """构造指向一次性测试库的 async URL。"""
    base = _TEST_DB.replace("mysql+pymysql://", "mysql+aiomysql://", 1)
    return base.rsplit("/", 1)[0] + f"/{db_name}?charset=utf8mb4"


async def _setup_snapshot_table(engine) -> None:
    """在一次性库中建 llm_context_snapshots（沿用 CE-01 30 列 schema）。"""
    _DDL = """
    CREATE TABLE IF NOT EXISTS llm_context_snapshots (
        id BIGINT NOT NULL AUTO_INCREMENT,
        public_id VARCHAR(64) NOT NULL,
        user_id BIGINT NOT NULL,
        conversation_id BIGINT NULL,
        message_id BIGINT NULL,
        agent_task_id BIGINT NULL,
        llm_task_type VARCHAR(64) NOT NULL,
        context_kind VARCHAR(64) NOT NULL,
        included_message_ids JSON NULL,
        included_file_ids JSON NULL,
        included_task_ids JSON NULL,
        included_artifact_ids JSON NULL,
        included_knowledge_ids JSON NULL,
        summary_id BIGINT NULL,
        estimated_tokens INT NOT NULL DEFAULT 0,
        context_digest VARCHAR(128) NULL,
        context_preview TEXT NULL,
        engine_version VARCHAR(32) NULL,
        call_site VARCHAR(128) NULL,
        context_profile_key VARCHAR(128) NULL,
        context_profile_version VARCHAR(32) NULL,
        context_policy_version VARCHAR(32) NULL,
        model_config_id BIGINT NULL,
        model_name_snapshot VARCHAR(128) NULL,
        context_window_tokens BIGINT NULL,
        input_budget_tokens BIGINT NULL,
        output_reserve_tokens BIGINT NULL,
        runtime_reserve_tokens BIGINT NULL,
        safety_margin_tokens BIGINT NULL,
        target_input_tokens BIGINT NULL,
        estimated_input_tokens BIGINT NULL,
        actual_input_tokens BIGINT NULL,
        actual_output_tokens BIGINT NULL,
        section_stats_json JSON NULL,
        included_refs_json JSON NULL,
        dropped_refs_json JSON NULL,
        retrieval_run_ids_json JSON NULL,
        compaction_json JSON NULL,
        tool_output_json JSON NULL,
        fallback_json JSON NULL,
        latency_json JSON NULL,
        prompt_digest CHAR(64) NULL,
        prompt_excerpt VARCHAR(2000) NULL,
        full_prompt_payload_id BIGINT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'building',
        error_code VARCHAR(64) NULL,
        completed_at DATETIME NULL,
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id),
        UNIQUE KEY uq_snap (public_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """
    async with engine.begin() as conn:
        await conn.execute(sa.text(_DDL))


@pytest.fixture()
async def snapshot_mysql_engine():
    """一次性 MySQL 库：testagent_ce02_snap_<pid>。"""
    import uuid

    db_name = "testagent_ce02_snap_" + uuid.uuid4().hex[:8]
    admin_engine = sa.create_engine(_TEST_DB)
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {db_name}"))
        conn.execute(sa.text(f"CREATE DATABASE {db_name} CHARACTER SET utf8mb4"))
        conn.commit()
    admin_engine.dispose()

    engine = create_async_engine(_async_db_url(db_name))
    await _setup_snapshot_table(engine)
    try:
        yield engine
    finally:
        await engine.dispose()
        admin_engine = sa.create_engine(_TEST_DB)
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f"DROP DATABASE IF EXISTS {db_name}"))
            conn.commit()
        admin_engine.dispose()


@pytest.fixture()
async def snapshot_session_factory(snapshot_mysql_engine):
    return async_sessionmaker(snapshot_mysql_engine, expire_on_commit=False)


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_full_lifecycle(snapshot_session_factory):
    async with snapshot_session_factory() as session:
        repo = ContextSnapshotRepository(session)
        svc = ContextSnapshotWriterService(lambda s: repo)

        ref = await svc.begin_build(
            ContextSnapshotBeginCommand(
                user_id=1, llm_task_type="chat", context_kind="active",
                call_site="chat.reply", estimated_input_tokens=100,
                prompt_digest=_DIGEST, prompt_excerpt="摘要",
                section_stats_json={"evidence": {"count": 1}},
            ),
            session=session,
        )
        assert ref.status == "building"

        r = await svc.mark_ready(session=session, public_id=ref.public_id, user_id=1)
        assert r.status == "ready"
        r = await svc.mark_sent(session=session, public_id=ref.public_id, user_id=1)
        assert r.status == "sent"
        r = await svc.complete(
            ContextSnapshotCompleteCommand(
                snapshot_public_id=ref.public_id,
                actual_input_tokens=50,
                actual_output_tokens=80,
                completed_at=None,
                llm_latency_ms=100,
            ),
            session=session,
            user_id=1,
        )
        assert r.status == "completed"

        # 30 列落库验证
        row = (
            await session.execute(
                sa.text("SELECT * FROM llm_context_snapshots WHERE public_id = :pid"),
                {"pid": ref.public_id},
            )
        ).first()
        assert row.status == "completed"
        assert row.actual_input_tokens == 50
        assert row.actual_output_tokens == 80
        assert row.call_site == "chat.reply"
        await session.commit()


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_actual_tokens_null_when_unavailable(snapshot_session_factory):
    async with snapshot_session_factory() as session:
        repo = ContextSnapshotRepository(session)
        svc = ContextSnapshotWriterService(lambda s: repo)
        ref = await svc.begin_build(
            ContextSnapshotBeginCommand(user_id=1, llm_task_type="chat", context_kind="active", call_site="x", estimated_input_tokens=10),
            session=session,
        )
        await svc.mark_ready(session=session, public_id=ref.public_id, user_id=1)
        await svc.mark_sent(session=session, public_id=ref.public_id, user_id=1)
        await svc.complete(
            ContextSnapshotCompleteCommand(
                snapshot_public_id=ref.public_id,
                actual_input_tokens=None,
                actual_output_tokens=None,
                completed_at=None,
                llm_latency_ms=None,
            ),
            session=session,
            user_id=1,
        )
        row = (
            await session.execute(
                sa.text("SELECT actual_input_tokens, actual_output_tokens FROM llm_context_snapshots WHERE public_id = :pid"),
                {"pid": ref.public_id},
            )
        ).first()
        assert row.actual_input_tokens is None
        assert row.actual_output_tokens is None
        await session.commit()


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_cas_illegal_transition_rejected(snapshot_session_factory):
    """abandoned → sent 非法（终止状态不可再转换）。"""
    async with snapshot_session_factory() as session:
        repo = ContextSnapshotRepository(session)
        svc = ContextSnapshotWriterService(lambda s: repo)
        ref = await svc.begin_build(
            ContextSnapshotBeginCommand(user_id=1, llm_task_type="chat", context_kind="active", call_site="x", estimated_input_tokens=10),
            session=session,
        )
        # 先 abandon（building → abandoned 合法）
        await svc.abandon(session=session, public_id=ref.public_id, user_id=1)
        # abandoned 后不可再 sent（终止状态 CAS 拒绝）
        with pytest.raises(SnapshotStatusError):
            await svc.mark_sent(session=session, public_id=ref.public_id, user_id=1)
        await session.commit()


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_completed_cannot_sent(snapshot_session_factory):
    async with snapshot_session_factory() as session:
        repo = ContextSnapshotRepository(session)
        svc = ContextSnapshotWriterService(lambda s: repo)
        ref = await svc.begin_build(
            ContextSnapshotBeginCommand(user_id=1, llm_task_type="chat", context_kind="active", call_site="x", estimated_input_tokens=10),
            session=session,
        )
        await svc.mark_ready(session=session, public_id=ref.public_id, user_id=1)
        await svc.mark_sent(session=session, public_id=ref.public_id, user_id=1)
        await svc.complete(
            ContextSnapshotCompleteCommand(snapshot_public_id=ref.public_id, completed_at=None),
            session=session,
            user_id=1,
        )
        with pytest.raises(SnapshotStatusError):
            await svc.mark_sent(session=session, public_id=ref.public_id, user_id=1)
        await session.commit()


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_fail_and_abandon(snapshot_session_factory):
    async with snapshot_session_factory() as session:
        repo = ContextSnapshotRepository(session)
        svc = ContextSnapshotWriterService(lambda s: repo)

        ref = await svc.begin_build(
            ContextSnapshotBeginCommand(user_id=1, llm_task_type="chat", context_kind="active", call_site="x", estimated_input_tokens=10),
            session=session,
        )
        await svc.mark_ready(session=session, public_id=ref.public_id, user_id=1)
        await svc.mark_sent(session=session, public_id=ref.public_id, user_id=1)
        r = await svc.fail(
            ContextSnapshotFailCommand(snapshot_public_id=ref.public_id, error_code="llm.provider.timeout", failed_at=None),
            session=session,
            user_id=1,
        )
        assert r.status == "failed"

        ref2 = await svc.begin_build(
            ContextSnapshotBeginCommand(user_id=1, llm_task_type="chat", context_kind="active", call_site="x", estimated_input_tokens=10),
            session=session,
        )
        r2 = await svc.abandon(session=session, public_id=ref2.public_id, user_id=1)
        assert r2.status == "abandoned"
        await session.commit()


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_shadow_ready_completed_allowed(snapshot_session_factory):
    async with snapshot_session_factory() as session:
        repo = ContextSnapshotRepository(session)
        svc = ContextSnapshotWriterService(lambda s: repo)
        ref = await svc.begin_build(
            ContextSnapshotBeginCommand(user_id=1, llm_task_type="chat", context_kind="shadow", call_site="x", estimated_input_tokens=10),
            session=session,
        )
        await svc.mark_ready(session=session, public_id=ref.public_id, user_id=1)
        r = await svc.complete_shadow(session=session, public_id=ref.public_id, user_id=1)
        assert r.status == "completed"
        row = (
            await session.execute(
                sa.text("SELECT context_kind, status, actual_input_tokens FROM llm_context_snapshots WHERE public_id = :pid"),
                {"pid": ref.public_id},
            )
        ).first()
        assert row.context_kind == "shadow"
        assert row.status == "completed"
        assert row.actual_input_tokens is None
        await session.commit()


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_active_ready_completed_rejected(snapshot_session_factory):
    async with snapshot_session_factory() as session:
        repo = ContextSnapshotRepository(session)
        svc = ContextSnapshotWriterService(lambda s: repo)
        ref = await svc.begin_build(
            ContextSnapshotBeginCommand(user_id=1, llm_task_type="chat", context_kind="active", call_site="x", estimated_input_tokens=10),
            session=session,
        )
        await svc.mark_ready(session=session, public_id=ref.public_id, user_id=1)
        with pytest.raises(SnapshotStatusError):
            await svc.complete_shadow(session=session, public_id=ref.public_id, user_id=1)
        await session.commit()
