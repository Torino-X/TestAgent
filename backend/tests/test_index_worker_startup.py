"""WP-BE-01：IndexWorker 启动 / 生命周期 / 幂等 / shutdown 测试。

覆盖：
- worker start/stop（graceful shutdown，无 leaked task）
- pending → claimed → indexed（_process_job 路径，SQLite 可跑）
- parse → channel jobs → embed/lexical skipped（无外部 store）
- failed → retry（RetryableIndexError → pending + next_retry_at）
- duplicate job idempotent（同 idempotency key 不重复创建）
- restart 后 pending job 可继续消费
- 生产工厂可构建（缺外部依赖 → channel skipped/degraded，不伪造）
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.context_engine.indexing.document_service import compute_job_idempotency_key
from app.context_engine.indexing.index_worker import (
    IndexWorker,
    RetryableIndexError,
)
from app.context_engine.indexing.production_handler import ProductionIndexJobHandler
from app.models.context_engine import (
    ContextIndexChunk,
    ContextIndexDocument,
    ContextIndexJob,
)
from app.repositories.base import ensure_model_id


async def _seed_document_and_job(
    session,
    *,
    doc_public_id: str = "idoc_seed",
    job_public_id: str = "idx_seed",
    operation: str = "parse_chunk",
    user_id: int = 1,
) -> tuple[ContextIndexDocument, ContextIndexJob]:
    doc = ContextIndexDocument(
        public_id=doc_public_id,
        user_id=user_id,
        workspace_key=f"conversation:conv_seed",
        source_type="uploaded_file",
        source_public_id="file_seed",
        source_version="1",
        source_digest="a" * 64,
        title="seed.txt",
        status="pending",
        chunk_policy_key="recursive_char:v1",
        chunk_policy_version="v1",
        lexical_index_status="pending",
        vector_index_status="pending",
        idempotency_key="idem_seed",
    )
    await ensure_model_id(session, ContextIndexDocument, doc)
    session.add(doc)
    await session.flush()

    job = ContextIndexJob(
        public_id=job_public_id,
        user_id=user_id,
        document_id=doc.id,
        operation=operation,
        status="pending",
        priority=100,
        attempt=0,
        max_attempts=3,
        idempotency_key=compute_job_idempotency_key(
            document_public_id=doc.public_id,
            source_version="1",
            source_digest="a" * 64,
            operation=operation,
            chunk_policy_key="recursive_char:v1",
            external_index_namespace=None,
        ),
    )
    await ensure_model_id(session, ContextIndexJob, job)
    session.add(job)
    await session.flush()
    return doc, job


@pytest.mark.asyncio
async def test_worker_start_stop_no_leaked_task(sqlite_session_factory):
    """worker.start → task 创建；stop → task 取消且清空引用。"""
    worker = IndexWorker(
        session_factory=sqlite_session_factory,
        poll_interval_seconds=0.01,
    )
    await worker.start()
    assert worker._task is not None
    assert not worker._task.done()
    await worker.stop()
    assert worker._task is None


@pytest.mark.asyncio
async def test_pending_job_processed_parse_creates_channel_jobs(sqlite_session_factory):
    """pending → claimed → terminal failure when no channel is queryable.

    无外部 store：parse_chunk 成功 → chunk 落库 → 创建 embed/lexical channel
    jobs（后续被 skipped）。无 ready 通道不得伪装成 indexed。
    """
    import tempfile
    import os

    tmp = tempfile.NamedTemporaryFile(
        suffix=".txt", delete=False, mode="w", encoding="utf-8"
    )
    tmp.write("hello world index content for rag retrieval")
    tmp.close()

    try:
        from app.models.uploaded_file import UploadedFile
        from datetime import datetime

        async with sqlite_session_factory() as session:
            # 建 uploaded_files 指向临时文件（parse 需要源文件）
            from app.repositories.base import ensure_model_id as _em

            uf = UploadedFile(
                public_id="file_seed",
                user_id=1,
                conversation_id=1,
                original_name="seed.txt",
                stored_name="seed.txt",
                file_ext="txt",
                mime_type="text/plain",
                file_size=os.path.getsize(tmp.name),
                storage_type="local",
                storage_path=tmp.name,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            await _em(session, UploadedFile, uf)
            session.add(uf)
            await session.flush()
            await session.commit()

            doc, job = await _seed_document_and_job(session)
            await session.commit()
            job_id = job.id
            doc_id = doc.id

        worker = IndexWorker(
            session_factory=sqlite_session_factory,
            handler=ProductionIndexJobHandler(),  # 无 store → skipped
            poll_interval_seconds=0.01,
        )
        async with sqlite_session_factory() as session:
            job = await session.get(ContextIndexJob, job_id)
            doc = await session.get(ContextIndexDocument, doc_id)
            await worker._process_job(session, job)
            await session.commit()

        # 处理 channel jobs（embed/lexical）：无外部 store → skipped → indexed
        async with sqlite_session_factory() as session:
            channel_jobs = (
                await session.execute(
                    select(ContextIndexJob).where(
                        ContextIndexJob.document_id == doc_id,
                        ContextIndexJob.operation.in_(["embed_document", "index_lexical"]),
                    )
                )
            ).scalars().all()
            for cj in channel_jobs:
                await worker._process_job(session, cj)
            await session.commit()

        async with sqlite_session_factory() as session:
            job = await session.get(ContextIndexJob, job_id)
            doc = await session.get(ContextIndexDocument, doc_id)
            assert job.status == "completed"
            assert doc.status == "failed"
            # chunk 落库
            chunk_count = (
                await session.execute(
                    select(func.count()).select_from(ContextIndexChunk).where(
                        ContextIndexChunk.document_id == doc_id
                    )
                )
            ).scalar()
            assert chunk_count >= 1
            # channel jobs 已创建
            ops = (
                await session.execute(
                    select(ContextIndexJob.operation).where(
                        ContextIndexJob.document_id == doc_id
                    )
                )
            ).scalars().all()
            assert "embed_document" in ops
            assert "index_lexical" in ops
    finally:
        os.unlink(tmp.name)


@pytest.mark.asyncio
async def test_failed_job_retries_not_fails_until_max(sqlite_session_factory):
    """RetryableIndexError → pending + next_retry_at（attempt 递增）。"""

    class _FailingHandler(ProductionIndexJobHandler):
        async def embed_document(self, **kwargs):
            raise RetryableIndexError("context.index.test_fail", "simulated failure")

    async with sqlite_session_factory() as session:
        doc, job = await _seed_document_and_job(
            session, doc_public_id="idoc_retry", job_public_id="idx_retry"
        )
        await session.commit()
        doc_id = doc.id

    # 直接创建 embed job（引用已存在的 document）
    async with sqlite_session_factory() as session:
        from app.context_engine.indexing.document_service import EMBED_OPERATION

        embed_job = ContextIndexJob(
            public_id="idx_embed",
            user_id=1,
            document_id=doc_id,
            operation=EMBED_OPERATION,
            status="pending",
            priority=100,
            attempt=0,
            max_attempts=3,
            idempotency_key="idem_embed_test",
        )
        await ensure_model_id(session, ContextIndexJob, embed_job)
        session.add(embed_job)
        await session.commit()

    worker = IndexWorker(
        session_factory=sqlite_session_factory,
        handler=_FailingHandler(),
        embedding_namespace="test_vec_ns",
        lexical_namespace="test_lex_ns",
        poll_interval_seconds=0.01,
    )
    async with sqlite_session_factory() as session:
        embed_job = (
            await session.execute(
                select(ContextIndexJob).where(
                    ContextIndexJob.public_id == "idx_embed"
                )
            )
        ).scalar_one()
        await worker._process_job(session, embed_job)
        await session.commit()

    async with sqlite_session_factory() as session:
        embed_job = (
            await session.execute(
                select(ContextIndexJob).where(
                    ContextIndexJob.public_id == "idx_embed"
                )
            )
        ).scalar_one()
        assert embed_job.status == "pending"  # attempt<max → pending（重试）
        assert embed_job.attempt == 1
        assert embed_job.next_retry_at is not None


@pytest.mark.asyncio
async def test_duplicate_job_idempotent(sqlite_session_factory):
    """同 idempotency key 不重复创建 job。"""
    from app.repositories.context_engine_repositories import ContextIndexJobRepository

    idem = compute_job_idempotency_key(
        document_public_id="idoc_idem",
        source_version="1",
        source_digest="b" * 64,
        operation="parse_chunk",
        chunk_policy_key="recursive_char:v1",
        external_index_namespace=None,
    )
    async with sqlite_session_factory() as session:
        repo = ContextIndexJobRepository(session)
        assert await repo.get_by_idempotency_key(idem) is None
        # 创建一次
        async with sqlite_session_factory() as s2:
            from app.context_engine.indexing.index_worker import _enqueue_checked

            await _seed_document_and_job(
                s2, doc_public_id="idoc_idem", job_public_id="idx_idem"
            )
            doc = (
                await s2.execute(
                    select(ContextIndexDocument).where(
                        ContextIndexDocument.public_id == "idoc_idem"
                    )
                )
            ).scalar_one()
            await _enqueue_checked(
                s2,
                user_id=1,
                document=doc,
                operation="parse_chunk",
                chunk_policy_key="recursive_char:v1",
            )
            await s2.commit()
        # 再次 enqueue 同一 idem → 不新增
        async with sqlite_session_factory() as s3:
            doc = (
                await s3.execute(
                    select(ContextIndexDocument).where(
                        ContextIndexDocument.public_id == "idoc_idem"
                    )
                )
            ).scalar_one()
            await _enqueue_checked(
                s3,
                user_id=1,
                document=doc,
                operation="parse_chunk",
                chunk_policy_key="recursive_char:v1",
            )
            await s3.commit()
        # 只有最初 seed 的 1 个 parse job（幂等无重复）
        jobs = (
            await session.execute(
                select(ContextIndexJob).where(
                    ContextIndexJob.operation == "parse_chunk",
                    ContextIndexJob.document_id == doc.id,
                )
            )
        ).scalars().all()
        assert len(jobs) == 1


@pytest.mark.asyncio
async def test_restart_consumes_existing_pending_job(sqlite_session_factory):
    """重启后 pending job 可被新 worker 消费（幂等语义）。"""
    # 模拟旧进程留下的 pending job
    async with sqlite_session_factory() as session:
        doc, job = await _seed_document_and_job(
            session,
            doc_public_id="idoc_restart",
            job_public_id="idx_restart_pending",
            operation="parse_chunk",
        )
        await session.commit()

    # 新进程 worker 处理同一 pending job（SKIP LOCKED 在 SQLite 不可用，
    # 直接调用 _process_job 验证 pending 可消费）
    worker = IndexWorker(
        session_factory=sqlite_session_factory,
        poll_interval_seconds=0.01,
    )
    async with sqlite_session_factory() as session:
        job = (
            await session.execute(
                select(ContextIndexJob).where(
                    ContextIndexJob.public_id == "idx_restart_pending"
                )
            )
        ).scalar_one()
        assert job.status == "pending"
        # parse 需要源文件不存在 → RetryableIndexError → 重试语义
        await worker._process_job(session, job)
        await session.commit()

    async with sqlite_session_factory() as session:
        job = (
            await session.execute(
                select(ContextIndexJob).where(
                    ContextIndexJob.public_id == "idx_restart_pending"
                )
            )
        ).scalar_one()
        # 源文件缺失 → 重试 pending（not failed until max_attempts）
        assert job.status == "pending"
        assert job.attempt >= 1


@pytest.mark.asyncio
async def test_terminal_embed_failure_settles_document_channel_status(sqlite_session_factory):
    """An exhausted channel retry must not leave a document permanently indexing."""
    from app.context_engine.indexing.document_service import EMBED_OPERATION

    async with sqlite_session_factory() as session:
        document, job = await _seed_document_and_job(
            session,
            doc_public_id="idoc_terminal_embed",
            job_public_id="idx_terminal_embed",
        )
        document.lexical_index_status = "skipped"
        document.vector_index_status = "indexing"
        job.operation = EMBED_OPERATION
        job.attempt = job.max_attempts - 1
        await session.commit()

        worker = IndexWorker(session_factory=sqlite_session_factory)
        await worker._retry_or_fail(
            session,
            job,
            "context.index.embed_failed",
            "embedding provider exhausted retries",
        )
        await session.commit()
        await session.refresh(document)

        assert job.status == "failed"
        assert document.vector_index_status == "failed"
        assert document.status == "failed"


def test_build_production_index_worker_returns_worker():
    """生产工厂返回 IndexWorker；缺外部依赖不崩溃。"""
    import os

    # 清掉 embedding/env 外部依赖（保证不依赖真实服务）
    saved = {
        k: os.environ.get(k)
        for k in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "QDRANT_HOST", "ES_HOST")
    }
    try:
        for k in saved:
            os.environ.pop(k, None)
        from app.context_engine.indexing.worker_factory import (
            build_production_index_worker,
        )
        from sqlalchemy.ext.asyncio import async_sessionmaker

        worker = build_production_index_worker(
            session_factory=lambda: None,
        )
        assert isinstance(worker, IndexWorker)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
