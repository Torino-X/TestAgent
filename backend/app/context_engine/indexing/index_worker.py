"""Index Worker：asyncio poll 领取 context_index_jobs 并处理。

CE-03 WP-3：
- 复刻 AgentExecutionWorker 模式：asyncio.Task poll + claim_next(SKIP LOCKED)；
- parse_chunk 本地处理（读文件 → 解析 → chunk 落库 → 创建 channel jobs）；
- embed_document / index_lexical / delete_external 委托给注入的 handler
  （WP-4 接 Qdrant / ES；此处仅编排状态机）；
- lease heartbeat / expired recovery / claim_owner 校验 / 优雅停止；
- 状态聚合：parse failed→document failed；channel 状态聚合见 _aggregate_document_status。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.context_engine.indexing.chunker import CHUNK_POLICY_KEY, RecursiveCharChunker
from app.context_engine.indexing.document_service import (
    DELETE_EXTERNAL_OPERATION,
    EMBED_OPERATION,
    LEXICAL_OPERATION,
    compute_job_idempotency_key,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _default_lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _gen_public_id(prefix: str) -> str:
    import uuid

    return prefix + uuid.uuid4().hex[:40]


class IndexJobHandler:
    """channel job 处理器接口（WP-4 由 Vector/Lexical store 实现注入）。

    parse_chunk 在 Worker 内部处理；embed/lexical/delete_external 委托实现。
    """

    async def embed_document(self, *, session, document, chunks, embedding_namespace: str | None) -> None:
        raise NotImplementedError

    async def index_lexical(self, *, session, document, chunks, lexical_namespace: str | None) -> None:
        raise NotImplementedError

    async def delete_external(self, *, session, document, namespaces: list[str]) -> None:
        raise NotImplementedError


class IndexWorker:
    """Index Worker：领取 context_index_jobs 并执行。

    构造参数：
    - session_factory：async_sessionmaker（新建独立 session，ORM→primitives 先落地）；
    - handler：IndexJobHandler（channel job 实现，WP-4 注入）；
    - embedding_namespace / lexical_namespace：由模型配置解析（可为 None）。
    """

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        handler: IndexJobHandler | None = None,
        embedding_namespace: str | None = None,
        lexical_namespace: str | None = None,
        chunk_policy_key: str = CHUNK_POLICY_KEY,
        poll_interval_seconds: float = 1.0,
        lease_seconds: int = 60,
        lease_owner: str | None = None,
    ) -> None:
        self._sf = session_factory
        self._handler = handler or IndexJobHandler()
        self._embedding_namespace = embedding_namespace
        self._lexical_namespace = lexical_namespace
        self._chunk_policy_key = chunk_policy_key
        self._poll_interval = float(poll_interval_seconds)
        self._lease_seconds = int(lease_seconds)
        self._lease_owner = lease_owner or _default_lease_owner()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._started_at: datetime | None = None
        self._last_poll_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._last_error_code: str | None = None
        self._processed_total = 0
        self._poll_errors_total = 0

    # ── 生命周期 ──────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._started_at = _utcnow()
        self._task = asyncio.create_task(self._run_forever(), name="context-index-worker")
        logger.info("IndexWorker start | lease_owner=%s | poll=%.1fs", self._lease_owner, self._poll_interval)

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
        logger.info("IndexWorker stopped")

    def health_snapshot(self) -> dict[str, Any]:
        """Return non-sensitive liveness and progress indicators for readiness."""
        return {
            "running": self._task is not None and not self._task.done(),
            "lease_owner": self._lease_owner,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "last_poll_at": self._last_poll_at.isoformat() if self._last_poll_at else None,
            "last_success_at": self._last_success_at.isoformat() if self._last_success_at else None,
            "last_error_at": self._last_error_at.isoformat() if self._last_error_at else None,
            "last_error_code": self._last_error_code,
            "processed_total": self._processed_total,
            "poll_errors_total": self._poll_errors_total,
            "vector_index_configured": self._embedding_namespace is not None,
            "lexical_index_configured": self._lexical_namespace is not None,
        }

    async def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._last_poll_at = _utcnow()
                processed = await self._poll_once()
                self._last_success_at = _utcnow()
                self._last_error_code = None
                self._processed_total += processed
                if processed == 0:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
                    except asyncio.TimeoutError:
                        pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("IndexWorker poll error: %s", type(exc).__name__)
                self._last_error_at = _utcnow()
                self._last_error_code = f"context.index.poll.{type(exc).__name__}"
                self._poll_errors_total += 1
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
                except asyncio.TimeoutError:
                    pass

    # ── 主循环 ────────────────────────────────────────────────────

    async def _poll_once(self) -> int:
        from app.repositories.context_engine_repositories import ContextIndexJobRepository

        async with self._sf() as session:
            job_repo = ContextIndexJobRepository(session)
            job = await job_repo.claim_next(claim_owner=self._lease_owner, lease_seconds=self._lease_seconds)
            if job is None:
                return 0
            await self._process_job(session, job)
            return 1

    async def _process_job(self, session: AsyncSession, job) -> None:
        from app.models.context_engine import ContextIndexDocument

        document = await session.get(ContextIndexDocument, job.document_id)
        if document is None:
            await self._fail_job(session, job, "context.index.document_missing")
            await session.commit()
            return

        try:
            if job.operation == "parse_chunk":
                await self._handle_parse(session, document, job)
            elif job.operation == EMBED_OPERATION:
                await self._handle_embed(session, document, job)
            elif job.operation == LEXICAL_OPERATION:
                await self._handle_lexical(session, document, job)
            elif job.operation == DELETE_EXTERNAL_OPERATION:
                await self._handle_delete_external(session, document, job)
            else:
                await self._fail_job(session, job, "context.index.unknown_operation")
            await session.commit()
        except RetryableIndexError as exc:
            await self._retry_or_fail(session, job, exc.code, exc.detail)
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            safe_error = getattr(exc, "error", None)
            safe_code = str(getattr(safe_error, "code", "") or "context.index.job_error")
            safe_stage = str(getattr(getattr(safe_error, "stage", None), "value", "") or "unknown")
            logger.warning(
                "IndexWorker job %s failed | error_type=%s | code=%s | stage=%s",
                job.public_id,
                type(exc).__name__,
                safe_code,
                safe_stage,
            )
            await self._retry_or_fail(session, job, safe_code, type(exc).__name__)
            await session.commit()

    # ── parse_chunk ───────────────────────────────────────────────

    async def _handle_parse(self, session, document, job) -> None:
        from app.context_engine.indexing.document_service import (
            EMBED_OPERATION,
            LEXICAL_OPERATION,
            SUPPORTED_EXTENSIONS,
        )

        document.status = "processing"
        await session.flush()

        storage_path, storage_type, ext = await _resolve_index_source(session, document)
        if ext not in SUPPORTED_EXTENSIONS:
            raise RetryableIndexError("context.index.unsupported_type", f"不支持类型 {ext}")

        raw = _read_storage(storage_path, storage_type)
        text = _parse_text(raw, ext)

        chunker = RecursiveCharChunker(section_path=document.title or None)
        chunks = chunker.chunk(text)
        from sqlalchemy import select
        from app.models.context_engine import ContextIndexChunk

        for c in chunks:
            existing = await session.execute(
                select(ContextIndexChunk).where(
                    ContextIndexChunk.document_id == document.id,
                    ContextIndexChunk.content_hash == c.content_hash,
                )
            )
            if existing.scalar_one_or_none() is not None:
                continue
            from app.repositories.base import ensure_model_id

            chunk_row = ContextIndexChunk(
                public_id=_gen_public_id("ick_"),
                document_id=document.id,
                user_id=document.user_id,
                chunk_index=c.chunk_index,
                section_path=c.section_path,
                content=c.content,
                normalized_content=c.normalized_content,
                content_hash=c.content_hash,
                char_count=c.char_count,
                estimated_tokens=c.estimated_tokens,
                status="active",
            )
            await ensure_model_id(session, ContextIndexChunk, chunk_row)
            session.add(chunk_row)
        await session.flush()

        # 创建 channel jobs（parse 成功后）
        for operation in (EMBED_OPERATION, LEXICAL_OPERATION):
            await _enqueue_checked(
                session,
                user_id=document.user_id,
                document=document,
                operation=operation,
                chunk_policy_key=self._chunk_policy_key,
            )

        # WP-BE-01：parse_chunk job 本身也标记完成（此前停留 running 永不消费，
        # 导致 parse job 长期堆积）。channel jobs 由后续 claim 消费。
        await self._complete_job(session, job)

    async def _handle_embed(self, session, document, job) -> None:
        from app.repositories.context_engine_repositories import ContextIndexChunkRepository

        if self._embedding_namespace is None:
            document.vector_index_status = "skipped"
            await self._aggregate_document_status(session, document)
            await self._complete_job(session, job)
            return
        document.vector_index_status = "indexing"
        await session.flush()
        chunks = await ContextIndexChunkRepository(session).list_for_document(document.id, document.user_id)
        await self._handler.embed_document(
            session=session,
            document=document,
            chunks=chunks,
            embedding_namespace=self._embedding_namespace,
        )
        document.vector_index_status = "ready"
        document.embedding_provider = document.embedding_provider or None
        await self._aggregate_document_status(session, document)
        await self._complete_job(session, job)

    async def _handle_lexical(self, session, document, job) -> None:
        from app.repositories.context_engine_repositories import ContextIndexChunkRepository

        if self._lexical_namespace is None:
            document.lexical_index_status = "skipped"
            await self._aggregate_document_status(session, document)
            await self._complete_job(session, job)
            return
        document.lexical_index_status = "indexing"
        await session.flush()
        chunks = await ContextIndexChunkRepository(session).list_for_document(document.id, document.user_id)
        await self._handler.index_lexical(
            session=session,
            document=document,
            chunks=chunks,
            lexical_namespace=self._lexical_namespace,
        )
        document.lexical_index_status = "ready"
        await self._aggregate_document_status(session, document)
        await self._complete_job(session, job)

    async def _handle_delete_external(self, session, document, job) -> None:
        await self._handler.delete_external(
            session=session,
            document=document,
            namespaces=[self._embedding_namespace, self._lexical_namespace],
        )
        await self._complete_job(session, job)

    # ── 状态聚合 ──────────────────────────────────────────────────

    async def _aggregate_document_status(self, session, document) -> None:
        """统一规则（第 6 项）：
        parse failed → failed（在 _retry_or_fail 置位）；
        ≥1 channel ready → indexed（仅查询 ready channel，其他记 fallback）；
        无 ready 且仍有 pending/indexing → processing；
        无 ready 的终态组合 → failed（禁止把不可检索文档标成 indexed）。
        """
        lex = document.lexical_index_status
        vec = document.vector_index_status
        ready = [c for c in (lex, vec) if c == "ready"]
        pending = [c for c in (lex, vec) if c in ("pending", "indexing")]

        if ready:
            document.status = "indexed"
            document.indexed_at = _utcnow()
        elif pending:
            document.status = "processing"
        else:
            document.status = "failed"
            document.indexed_at = None
        await session.flush()

    # ── job 完成 / 失败 ───────────────────────────────────────────

    async def _complete_job(self, session, job) -> None:
        job.status = "completed"
        job.completed_at = _utcnow()
        job.attempt += 1
        job.claimed_by = None
        job.claimed_until = None
        job.error_code = None
        job.error_message = None
        job.next_retry_at = None
        await session.flush()

    async def _retry_or_fail(self, session, job, code: str, detail: str) -> None:
        from datetime import timedelta

        job.attempt += 1
        if job.attempt >= job.max_attempts:
            job.status = "failed"
            job.error_code = code
            job.error_message = detail[:1900]
            # parse failed → document failed
            from app.models.context_engine import ContextIndexDocument

            doc = await session.get(ContextIndexDocument, job.document_id)
            if doc and job.operation == "parse_chunk":
                doc.status = "failed"
            elif doc and job.operation == EMBED_OPERATION:
                doc.vector_index_status = "failed"
                await self._aggregate_document_status(session, doc)
            elif doc and job.operation == LEXICAL_OPERATION:
                doc.lexical_index_status = "failed"
                await self._aggregate_document_status(session, doc)
        else:
            backoff = min(5 * (2 ** (job.attempt - 1)), 60)
            job.status = "pending"
            job.error_code = code
            job.error_message = detail[:1900]
            job.next_retry_at = _utcnow() + timedelta(seconds=backoff)
        await session.flush()

    async def _fail_job(self, session, job, code: str) -> None:
        job.status = "failed"
        job.error_code = code
        job.error_message = code
        await session.flush()


class RetryableIndexError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


async def _resolve_index_source(session, document) -> tuple[str, str, str | None]:
    """Resolve supported durable source rows for the common index worker."""
    if document.source_type == "uploaded_file":
        from app.repositories.file_repository import FileRepository

        uploaded = await FileRepository(session).get_by_public_id(
            document.user_id, document.source_public_id
        )
        if uploaded is None:
            raise RetryableIndexError("context.index.file_not_found", "源文件缺失")
        ext = ("." + uploaded.file_ext).lower() if uploaded.file_ext else None
        return uploaded.storage_path, uploaded.storage_type, ext
    if document.source_type == "artifact":
        from app.models.artifact import Artifact
        from sqlalchemy import select

        artifact = (await session.execute(
            select(Artifact).where(
                Artifact.public_id == document.source_public_id,
                Artifact.user_id == document.user_id,
                Artifact.status == "available",
                Artifact.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if artifact is None:
            raise RetryableIndexError("context.index.artifact_not_found", "产物不存在或不可用")
        ext = ("." + artifact.file_ext).lower() if artifact.file_ext else None
        return artifact.storage_path, artifact.storage_type, ext
    raise RetryableIndexError("context.index.source_not_supported", document.source_type)


async def _enqueue_checked(session, *, user_id: int, document, operation: str, chunk_policy_key: str) -> None:
    from app.repositories.context_engine_repositories import ContextIndexJobRepository

    idem = compute_job_idempotency_key(
        document_public_id=document.public_id,
        source_version=document.source_version,
        source_digest=document.source_digest,
        operation=operation,
        chunk_policy_key=chunk_policy_key,
        external_index_namespace=document.external_index_namespace,
    )
    job_repo = ContextIndexJobRepository(session)
    if await job_repo.get_by_idempotency_key(idem):
        return
    from app.models.context_engine import ContextIndexJob
    from app.repositories.base import ensure_model_id

    job = ContextIndexJob(
        public_id=_gen_public_id("idx_"),
        user_id=user_id,
        document_id=document.id,
        operation=operation,
        status="pending",
        priority=100,
        attempt=0,
        max_attempts=3,
        idempotency_key=idem,
    )
    await ensure_model_id(session, ContextIndexJob, job)
    session.add(job)


def _read_storage(storage_path: str, storage_type: str = "local") -> bytes:
    if storage_type == "oss":
        from app.storage.oss_storage import object_storage

        try:
            return object_storage.read_bytes_sync(storage_path)
        except FileNotFoundError as exc:
            raise RetryableIndexError("context.index.file_missing", storage_path) from exc

    from app.storage.local_storage import local_storage

    path = Path(storage_path)
    if path.is_absolute() and not str(storage_path).startswith(("/", "\\")):
        full_path = path
    else:
        full_path = local_storage._base / storage_path  # noqa: SLF001
        try:
            full_path.resolve().relative_to(local_storage._base.resolve())  # noqa: SLF001
        except ValueError as exc:
            raise RetryableIndexError(
                "context.index.path_traversal",
                "storage_path escapes local storage base",
            ) from exc
    if not full_path.exists():
        raise RetryableIndexError("context.index.file_missing", storage_path)
    with open(full_path, "rb") as fh:
        return fh.read()


def _parse_text(raw: bytes, ext: str) -> str:
    if ext == ".txt" or ext == ".md" or ext == ".json":
        return raw.decode("utf-8", errors="replace")
    if ext == ".docx":
        from docx import Document

        doc = Document(__import__("io").BytesIO(raw))
        return "\n".join(p.text for p in doc.paragraphs if p.text)
    return ""
# auto-appended module-level note: index worker: 后台消费任务, 走 chunker + embedding 落库。
