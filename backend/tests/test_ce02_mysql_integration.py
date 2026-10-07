"""CE-02 WP-12b：隔离 MySQL 集成测试（REQUIRES_TEST_MYSQL）。

一次性库 testagent_ce02_*（自动建/删），不触碰生产 testagent 库。
覆盖：
- 并发 complete/fail（两并发事务争用同一 snapshot → 一个成功一个 CAS 拒绝）；
- Payload owner scope（跨用户读取/删除拒绝）；
- ToolCalls 扩展列（context_payload_id/output_preview/char_count/estimated_tokens/
  sha256/truncated/policy/truncation_metadata 落库）；
- 事务 rollback（payload 文件已写但 DB 失败 → 补偿清理 / 无孤儿文件残留）；
- 孤儿文件补偿（DB 指向不存在文件 / 文件无 DB 行 → cleanup）。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.context_engine.models.payload import PayloadStoreCommand
from app.context_engine.models.snapshot_models import (
    ContextSnapshotBeginCommand,
    ContextSnapshotCompleteCommand,
    ContextSnapshotFailCommand,
)
from app.context_engine.payload import FileSystemPayloadBackend, PayloadStorageService
from app.context_engine.snapshot import ContextSnapshotWriterService, SnapshotStatusError
from app.context_engine.tool_output import ToolOutputManager, TypedToolOutput
from app.models.tool_call import ToolCall
from app.repositories.context_engine_repositories import ContextIndexJobRepository
from app.repositories.context_snapshot_repository import ContextSnapshotRepository
from app.repositories.tool_call_repository import ToolCallRepository

# 从 backend/.env 读取 DATABASE_SYNC_URL（若环境未注入）
def _load_db_url() -> str:
    env = os.environ.get("DATABASE_SYNC_URL", "")
    if env:
        return env
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_SYNC_URL="):
                return line.strip().split("=", 1)[1]
    return ""


_TEST_DB = _load_db_url()
REQUIRES_TEST_MYSQL = pytest.mark.skipif(
    not _TEST_DB or "testagent" not in _TEST_DB,
    reason="需要 DATABASE_SYNC_URL 指向测试 MySQL",
)


def _async_db_url(db_name: str) -> str:
    base = _TEST_DB.replace("mysql+pymysql://", "mysql+aiomysql://", 1)
    return base.rsplit("/", 1)[0] + f"/{db_name}?charset=utf8mb4"


_SNAPSHOT_DDL = """
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

_PAYLOAD_DDL = """
CREATE TABLE IF NOT EXISTS context_payloads (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id VARCHAR(64) NOT NULL,
    user_id BIGINT NOT NULL,
    workspace_key VARCHAR(191) NULL,
    conversation_id BIGINT NULL,
    task_id BIGINT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_public_id VARCHAR(64) NULL,
    payload_type VARCHAR(32) NOT NULL,
    storage_backend VARCHAR(32) NOT NULL,
    storage_key VARCHAR(1000) NOT NULL,
    storage_key_hash CHAR(64) NOT NULL,
    mime_type VARCHAR(128) NULL,
    content_encoding VARCHAR(32) NULL,
    size_bytes BIGINT NOT NULL,
    char_count BIGINT NULL,
    estimated_tokens BIGINT NULL,
    sha256 CHAR(64) NOT NULL,
    encrypted TINYINT(1) NOT NULL DEFAULT 0,
    encryption_key_ref VARCHAR(255) NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    metadata_json JSON NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NULL,
    deleted_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_payload (public_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_TOOLCALLS_DDL = """
CREATE TABLE IF NOT EXISTS tool_calls (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id VARCHAR(64) NOT NULL,
    user_id BIGINT NOT NULL,
    conversation_id BIGINT NOT NULL,
    task_id BIGINT NOT NULL,
    tool_name VARCHAR(128) NOT NULL,
    tool_stage VARCHAR(128) NULL,
    status VARCHAR(32) DEFAULT 'pending',
    input_summary_json JSON NULL,
    output_summary_json JSON NULL,
    error_code VARCHAR(128) NULL,
    error_message TEXT NULL,
    context_payload_id BIGINT NULL,
    output_preview TEXT NULL,
    output_char_count BIGINT NULL,
    output_estimated_tokens BIGINT NULL,
    output_sha256 CHAR(64) NULL,
    output_truncated TINYINT(1) NOT NULL DEFAULT 0,
    output_policy_key VARCHAR(64) NULL,
    output_policy_version VARCHAR(32) NULL,
    truncation_metadata_json JSON NULL,
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    duration_ms BIGINT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_tool (public_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

_INDEX_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS context_index_jobs (
    id BIGINT NOT NULL AUTO_INCREMENT,
    public_id VARCHAR(64) NOT NULL,
    user_id BIGINT NOT NULL,
    document_id BIGINT NOT NULL,
    operation VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    priority INT NOT NULL DEFAULT 100,
    attempt INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    claimed_by VARCHAR(128) NULL,
    claimed_until DATETIME NULL,
    idempotency_key VARCHAR(191) NOT NULL,
    payload_json JSON NULL,
    error_code VARCHAR(64) NULL,
    error_message VARCHAR(2000) NULL,
    next_retry_at DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at DATETIME NULL,
    completed_at DATETIME NULL,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_index_jobs_public_id (public_id),
    UNIQUE KEY uq_index_jobs_idempotency (idempotency_key),
    KEY idx_index_jobs_claim (status, next_retry_at, priority, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


@pytest.fixture()
def mysql_db_name() -> str:
    return "testagent_ce02_mysql_" + uuid.uuid4().hex[:8]


@pytest.fixture()
def admin_engine():
    engine = sa.create_engine(_TEST_DB)
    yield engine
    engine.dispose()


def _create_db(admin_engine, db_name: str) -> None:
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {db_name}"))
        conn.execute(sa.text(f"CREATE DATABASE {db_name} CHARACTER SET utf8mb4"))
        conn.commit()


def _drop_db(admin_engine, db_name: str) -> None:
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f"DROP DATABASE IF EXISTS {db_name}"))
        conn.commit()


@pytest.fixture()
async def mysql_engine(admin_engine, mysql_db_name):
    _create_db(admin_engine, mysql_db_name)
    engine = create_async_engine(_async_db_url(mysql_db_name))
    async with engine.begin() as conn:
        for ddl in (_SNAPSHOT_DDL, _PAYLOAD_DDL, _TOOLCALLS_DDL, _INDEX_JOBS_DDL):
            await conn.execute(sa.text(ddl))
    try:
        yield engine
    finally:
        await engine.dispose()
        _drop_db(admin_engine, mysql_db_name)


@pytest.fixture()
def session_factory(mysql_engine):
    return async_sessionmaker(mysql_engine, expire_on_commit=False)


# ── 并发 complete/fail：一个成功一个 CAS 拒绝 ─────────────────────────


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_concurrent_complete_fail_cas(session_factory):
    """两并发事务争用同一 snapshot → 一个成功一个 CAS 拒绝，无脏写。"""
    # 先建 building snapshot
    async with session_factory() as session:
        repo = ContextSnapshotRepository(session)
        svc = ContextSnapshotWriterService(lambda s: repo)
        ref = await svc.begin_build(
            ContextSnapshotBeginCommand(user_id=1, llm_task_type="chat", context_kind="active", call_site="x", estimated_input_tokens=10),
            session=session,
        )
        await svc.mark_ready(session=session, public_id=ref.public_id, user_id=1)
        await svc.mark_sent(session=session, public_id=ref.public_id, user_id=1)
        await session.commit()
        pid = ref.public_id

    # 并发 complete vs fail（两个独立事务）
    import asyncio
    from datetime import datetime

    async def _complete():
        async with session_factory() as session:
            repo = ContextSnapshotRepository(session)
            svc = ContextSnapshotWriterService(lambda s: repo)
            await svc.complete(
                ContextSnapshotCompleteCommand(snapshot_public_id=pid, actual_input_tokens=5, completed_at=datetime.now()),
                session=session,
                user_id=1,
            )
            await session.commit()
            return "completed"

    async def _fail():
        async with session_factory() as session:
            repo = ContextSnapshotRepository(session)
            svc = ContextSnapshotWriterService(lambda s: repo)
            await svc.fail(
                ContextSnapshotFailCommand(snapshot_public_id=pid, error_code="llm.error", failed_at=datetime.now()),
                session=session,
                user_id=1,
            )
            await session.commit()
            return "failed"

    results = await asyncio.gather(_complete(), _fail(), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, BaseException)]
    rejected = [r for r in results if isinstance(r, SnapshotStatusError)]
    # 一个成功，一个 CAS 拒绝（MySQL 行锁 + status IN 原子更新）
    assert len(ok) == 1
    assert len(rejected) == 1

    # 无脏写：最终状态唯一
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text("SELECT status FROM llm_context_snapshots WHERE public_id = :pid"),
                {"pid": pid},
            )
        ).first()
    assert row.status in ("completed", "failed")


# ── Payload owner scope ─────────────────────────────────────────────


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_payload_owner_scope_cross_user_rejected(session_factory):
    """跨用户读取/删除 Payload 拒绝。"""
    from app.repositories.context_engine_repositories import ContextPayloadRepository

    backend_root = Path(__file__).resolve().parent.parent / "data" / "ce_payloads_test"
    # 测试残留不应写入仓库；用系统临时目录
    import tempfile

    backend_root = Path(tempfile.mkdtemp(prefix="ce_payloads_test_"))
    backend = FileSystemPayloadBackend(backend_root)

    # 直接插入 payload 行（模拟已存在）
    async with session_factory() as session:
        await session.execute(
            sa.text(
                "INSERT INTO context_payloads "
                "(public_id, user_id, source_type, payload_type, storage_backend, storage_key, "
                " storage_key_hash, size_bytes, sha256, status) "
                "VALUES (:pid, :uid, 'test', 'test', 'fs', :key, :kh, 5, :sha, 'active')"
            ),
            {
                "pid": "pay_owner_1", "uid": 1, "key": "key1",
                "kh": "abc", "sha": "a" * 64,
            },
        )
        await session.commit()

    # 写文件（owner 1 可读）— sha256 必须匹配内容
    from app.context_engine.models.value_objects import Digest

    content = b"hello"
    real_sha = str(Digest.of(content))
    backend.write(1, "key1", content)

    # 更新 DB 行的 sha256 为真实值
    async with session_factory() as session:
        await session.execute(
            sa.text("UPDATE context_payloads SET sha256 = :sha WHERE public_id = 'pay_owner_1'"),
            {"sha": real_sha},
        )
        await session.commit()

    # 用真实 repo 工厂（每次 session 绑定）
    svc = PayloadStorageService(
        lambda session: ContextPayloadRepository(session),
        backend=backend,
    )

    # owner 1 读取成功
    chunks = []
    async for c in svc.open(1, "pay_owner_1", session_factory=session_factory):
        chunks.append(c)
    assert b"".join(chunks) == content

    # 跨用户（user 2）读取拒绝（repo 查不到行 → not_found）
    from app.context_engine.errors import ContextEngineFailure

    with pytest.raises(ContextEngineFailure) as exc_info:
        async for _ in svc.open(2, "pay_owner_1", session_factory=session_factory):
            pass
    assert exc_info.value.error.code == "context.payload.not_found"

    # 跨用户删除 → False
    assert await svc.delete(2, "pay_owner_1", session_factory=session_factory) is False


# ── ToolCalls 扩展列落库 ─────────────────────────────────────────────


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_tool_calls_extended_columns(session_factory):
    """ToolCalls 扩展列（context_payload_id/preview/char_count/tokens/sha256/truncated/policy）落库。"""
    async with session_factory() as session:
        # 插入 tool_call 基础行
        await session.execute(
            sa.text(
                "INSERT INTO tool_calls (public_id, user_id, conversation_id, task_id, tool_name, status) "
                "VALUES ('tc_ext_1', 1, 1, 1, 'test_tool', 'completed')"
            ),
        )
        # 插入 payload 行供关联
        await session.execute(
            sa.text(
                "INSERT INTO context_payloads "
                "(public_id, user_id, source_type, payload_type, storage_backend, storage_key, "
                " storage_key_hash, size_bytes, sha256, status) "
                "VALUES ('pay_ext_1', 1, 'tool_output', 'tool_output', 'fs', 'k', 'h', 10, :sha, 'active')"
            ),
            {"sha": "b" * 64},
        )
        await session.commit()

    # 用 update_extended_columns 写扩展列
    async with session_factory() as session:
        repo = ToolCallRepository(session)
        await repo.update_extended_columns(
            "tc_ext_1", 1,
            payload_ref="pay_ext_1",
            preview="preview text",
            char_count=12,
            estimated_tokens=6,
            sha256="c" * 64,
            truncated=True,
            policy_key="medium",
            policy_version="v1",
            truncation_metadata={"mode": "head_tail", "included": 8, "omitted": 4},
        )
        await session.commit()

    # 验证落库
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text("SELECT * FROM tool_calls WHERE public_id = 'tc_ext_1'")
            )
        ).first()
    assert row.output_preview == "preview text"
    assert row.output_char_count == 12
    assert row.output_estimated_tokens == 6
    assert row.output_sha256 == "c" * 64
    assert row.output_truncated == 1
    assert row.output_policy_key == "medium"
    assert row.output_policy_version == "v1"
    assert row.truncation_metadata_json is not None
    # context_payload_id 关联成功（FK 到 context_payloads.id）
    assert row.context_payload_id is not None


# ── 事务 rollback / 孤儿文件补偿 ─────────────────────────────────────


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_payload_orphan_cleanup(mysql_engine, session_factory):
    """孤儿文件补偿：DB 指向不存在文件 / 文件无 DB 行 → cleanup。"""
    import tempfile

    backend_root = Path(tempfile.mkdtemp(prefix="ce_payloads_orphan_"))
    backend = FileSystemPayloadBackend(backend_root)

    # 1. DB 指向不存在文件
    async with session_factory() as session:
        await session.execute(
            sa.text(
                "INSERT INTO context_payloads "
                "(public_id, user_id, source_type, payload_type, storage_backend, storage_key, "
                " storage_key_hash, size_bytes, sha256, status) "
                "VALUES ('pay_orphan_db', 1, 'test', 'test', 'fs', 'missing_key', :kh, 5, :sha, 'active')"
            ),
            {"kh": "d" * 64, "sha": "e" * 64},
        )
        await session.commit()

    # 2. 文件无 DB 行
    backend.write(99, "orphan_file_key", b"orphan data")

    svc = PayloadStorageService(None, backend=backend)
    db_orphans, file_orphans = await svc.cleanup_orphans(session_factory=session_factory)

    # DB 孤儿（missing file）标记 deleted
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text("SELECT status FROM context_payloads WHERE public_id = 'pay_orphan_db'")
            )
        ).first()
    assert row.status == "missing_file"
    # 文件孤儿被删除
    assert not backend.exists(99, "orphan_file_key")
    # 至少清理了一个 DB 孤儿 + 一个文件孤儿
    assert db_orphans >= 1
    assert file_orphans >= 1


# ── SKIP LOCKED：并发 Worker 领取互斥 ────────────────────────────────


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_skip_locked_concurrent_claim_no_dup(session_factory):
    """两个并发 Session/Worker 同时 claim → 不领取同一 Job，无重复 claim。"""
    from app.repositories.context_engine_repositories import ContextIndexJobRepository

    async with session_factory() as session:
        await session.execute(
            sa.text(
                "INSERT INTO context_index_jobs "
                "(public_id, user_id, document_id, operation, status, priority, idempotency_key, created_at, updated_at) "
                "VALUES ('job_1', 1, 1, 'embed', 'pending', 100, 'idem_1', NOW(), NOW())"
            ),
        )
        await session.commit()

    import asyncio

    async def _claim(owner):
        async with session_factory() as session:
            repo = ContextIndexJobRepository(session)
            job = await repo.claim_next(claim_owner=owner)
            await session.commit()
            return job.public_id if job else None

    results = await asyncio.gather(_claim("worker_a"), _claim("worker_b"))
    claimed = [r for r in results if r is not None]
    # 只有一个 worker 领到 job（SKIP LOCKED 互斥）
    assert len(claimed) == 1
    assert claimed[0] == "job_1"

    async with session_factory() as session:
        row = (
            await session.execute(sa.text("SELECT status, claimed_by FROM context_index_jobs WHERE public_id = 'job_1'"))
        ).first()
    assert row.status == "running"
    assert row.claimed_by in ("worker_a", "worker_b")


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_skip_locked_rollback_reclaimable(session_factory):
    """rollback 后 Job 可重新领取（未提交的 claim 不占用）。"""
    from app.repositories.context_engine_repositories import ContextIndexJobRepository

    async with session_factory() as session:
        await session.execute(
            sa.text(
                "INSERT INTO context_index_jobs "
                "(public_id, user_id, document_id, operation, status, priority, idempotency_key, created_at, updated_at) "
                "VALUES ('job_rb', 1, 1, 'embed', 'pending', 100, 'idem_rb', NOW(), NOW())"
            ),
        )
        await session.commit()

    # Worker A claim 但 rollback（不 commit）
    async with session_factory() as session:
        repo = ContextIndexJobRepository(session)
        job = await repo.claim_next(claim_owner="worker_a")
        assert job is not None
        # 不 commit → 自动 rollback
    # Worker B 可重新领取
    async with session_factory() as session:
        repo = ContextIndexJobRepository(session)
        job = await repo.claim_next(claim_owner="worker_b")
        await session.commit()
    assert job is not None
    assert job.public_id == "job_rb"


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_skip_locked_no_double_claim(session_factory):
    """已 running 的 Job 不会被二次 claim。"""
    from app.repositories.context_engine_repositories import ContextIndexJobRepository

    async with session_factory() as session:
        await session.execute(
            sa.text(
                "INSERT INTO context_index_jobs "
                "(public_id, user_id, document_id, operation, status, priority, idempotency_key, created_at, updated_at) "
                "VALUES ('job_claimed', 1, 1, 'embed', 'running', 100, 'idem_claimed', NOW(), NOW())"
            ),
        )
        await session.commit()

    async with session_factory() as session:
        repo = ContextIndexJobRepository(session)
        job = await repo.claim_next(claim_owner="worker_c")
        await session.commit()
    assert job is None  # running job 不可再 claim


# ── Payload 事务补偿：文件写成功 DB 失败 → 清理 ───────────────────────


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_payload_transaction_compensation(session_factory):
    """文件写入成功、DB insert 失败 → 临时/最终文件清理，无 ContextPayload 行。"""
    import tempfile
    from sqlalchemy.exc import IntegrityError

    backend_root = Path(tempfile.mkdtemp(prefix="ce_payload_comp_"))
    backend = FileSystemPayloadBackend(backend_root)

    from app.repositories.context_engine_repositories import ContextPayloadRepository

    svc = PayloadStorageService(
        lambda session: ContextPayloadRepository(session),
        backend=backend,
    )
    command = PayloadStoreCommand(user_id=1, content="transaction payload")

    # 让 DB 写入失败：monkeypatch session_factory 使 flush/commit 抛 IntegrityError
    original_sf = session_factory

    class _FailingCM:
        def __init__(self):
            self._session = original_sf()

        async def __aenter__(self):
            await self._session.__aenter__()
            return self._session

        async def __aexit__(self, *a):
            return await self._session.__aexit__(*a)

    def _failing_factory():
        cm = _FailingCM()

        async def _fail(*args, **kwargs):
            raise IntegrityError("INSERT", {}, Exception("simulated db failure"))

        # patch flush/commit on the underlying session
        cm._session.flush = _fail
        cm._session.commit = _fail
        return cm

    try:
        await svc.put(command, session_factory=_failing_factory)
        assert False, "DB 失败应导致 put 抛错"
    except IntegrityError:
        pass

    # 补偿验证：无 ContextPayload 行 + 无残留文件
    async with session_factory() as session:
        count = (
            await session.execute(sa.text("SELECT COUNT(*) FROM context_payloads"))
        ).scalar()
    assert count == 0, "DB 失败后不应有 ContextPayload 行"
    owner_dir = backend_root / "1"
    if owner_dir.exists():
        files = [p.name for p in owner_dir.iterdir()]
        assert files == [], f"应无残留文件: {files}"


# ── 双向 Orphan Cleanup + owner 目录外不删 ───────────────────────────


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_orphan_cleanup_bidirectional(session_factory):
    """双向 Orphan Cleanup：DB 行无文件 + 文件无 DB 行。"""
    import tempfile

    backend_root = Path(tempfile.mkdtemp(prefix="ce_payload_orphan2_"))
    backend = FileSystemPayloadBackend(backend_root)

    # DB 行存在但文件不存在
    async with session_factory() as session:
        await session.execute(
            sa.text(
                "INSERT INTO context_payloads "
                "(public_id, user_id, source_type, payload_type, storage_backend, storage_key, "
                " storage_key_hash, size_bytes, sha256, status) "
                "VALUES ('pay_db_only', 1, 'test', 'test', 'fs', 'missing_key', :kh, 5, :sha, 'active')"
            ),
            {"kh": "x" * 64, "sha": "y" * 64},
        )
        await session.commit()

    # 文件存在但 DB 行不存在
    backend.write(2, "file_only_key", b"orphan")

    svc = PayloadStorageService(None, backend=backend)
    db_orphans, file_orphans = await svc.cleanup_orphans(session_factory=session_factory)

    async with session_factory() as session:
        row = (
            await session.execute(sa.text("SELECT status FROM context_payloads WHERE public_id = 'pay_db_only'"))
        ).first()
    assert row.status == "missing_file"
    assert not backend.exists(2, "file_only_key")
    assert db_orphans >= 1
    assert file_orphans >= 1


@REQUIRES_TEST_MYSQL
@pytest.mark.asyncio
async def test_orphan_cleanup_does_not_delete_owner_dir_outside(session_factory):
    """owner 目录外文件绝不删除（cleanup 只扫 owner 子目录内）。"""
    import tempfile

    backend_root = Path(tempfile.mkdtemp(prefix="ce_payload_orphan3_"))
    backend = FileSystemPayloadBackend(backend_root)

    outside = backend_root / "outside_file.txt"
    outside.write_text("should not be touched")

    svc = PayloadStorageService(None, backend=backend)
    await svc.cleanup_orphans(session_factory=session_factory)

    # 目录外文件仍在（iterate_orphans 只扫 owner 子目录）
    assert outside.exists()
