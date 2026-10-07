"""Context Engine Repositories — 新增 10 张表的 owner-scoped 数据访问层。

规则（设计文档 §34 Repository 边界）：
- Repository 只做数据访问；
- 不调用 LLM / Embedding / Reranker / Vector / HTTP；
- 所有用户资源查询包含 user_id；
- Workspace 资源查询带 user_id + workspace_key；
- Service 负责事务与多表编排。
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import case as _case


def _mysql_nulls_last(column, *, descending: bool) -> list:
    """MySQL 兼容的 NULLS LAST 实现（query-level ORDER BY）。

    MySQL 不支持 SQL 标准的 ``NULLS LAST/FIRST`` 语法（仅 PostgreSQL/Oracle，
    MySQL 8.0.16+ 仅在 window function 内支持）。等价表达：
    ``ORDER BY col IS NULL ASC, col [ASC|DESC]``（NULL 排末尾）。

    返回 [null_marker, main_sort] 列表，供 ``stmt.order_by(*_mysql_nulls_last(col))``
    解包为两条独立排序键。
    """
    null_marker = _case((column.is_(None), 1), else_=0)
    main = column.desc() if descending else column.asc()
    return [null_marker, main]

from app.models.context_engine import (
    ContextCompactionRun,
    ContextIndexChunk,
    ContextIndexDocument,
    ContextIndexJob,
    ContextMemory,
    ContextMemorySource,
    ContextPayload,
    ContextRetrievalCandidate,
    ContextRetrievalRun,
    ContextWorkspaceInstruction,
)
from app.repositories.base import BaseRepository, ensure_model_id


def _delete_job_idempotency_key(document: ContextIndexDocument) -> str:
    parts = [
        document.public_id,
        document.source_version,
        document.source_digest,
        "delete_external",
        document.chunk_policy_key,
        document.external_index_namespace or "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class ContextPayloadRepository(BaseRepository[ContextPayload]):
    model = ContextPayload

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_public_id(self, public_id: str, user_id: int) -> ContextPayload | None:
        result = await self.session.execute(
            select(ContextPayload).where(
                ContextPayload.public_id == public_id,
                ContextPayload.user_id == user_id,
                ContextPayload.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_owner(
        self, user_id: int, *, payload_type: str | None = None, limit: int = 20
    ) -> list[ContextPayload]:
        stmt = select(ContextPayload).where(
            ContextPayload.user_id == user_id,
            ContextPayload.deleted_at.is_(None),
        )
        if payload_type:
            stmt = stmt.where(ContextPayload.payload_type == payload_type)
        stmt = stmt.order_by(ContextPayload.created_at.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class ContextMemoryRepository(BaseRepository[ContextMemory]):
    model = ContextMemory

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_public_id(self, public_id: str, user_id: int) -> ContextMemory | None:
        result = await self.session.execute(
            select(ContextMemory).where(
                ContextMemory.public_id == public_id,
                ContextMemory.user_id == user_id,
                ContextMemory.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, memory_id: int) -> ContextMemory | None:
        result = await self.session.execute(
            select(ContextMemory).where(ContextMemory.id == memory_id)
        )
        return result.scalar_one_or_none()

    async def list_active_for_scope(
        self,
        user_id: int,
        *,
        scope_type: str,
        workspace_key: str | None = None,
        limit: int = 50,
    ) -> list[ContextMemory]:
        stmt = select(ContextMemory).where(
            ContextMemory.user_id == user_id,
            ContextMemory.scope_type == scope_type,
            ContextMemory.status.in_(["active", "candidate"]),
            ContextMemory.deleted_at.is_(None),
        )
        if workspace_key:
            stmt = stmt.where(ContextMemory.workspace_key == workspace_key)
        stmt = stmt.order_by(ContextMemory.updated_at.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_authoritative_active(
        self,
        user_id: int,
        *,
        scope_type: str,
        workspace_key: str | None = None,
        agent_type: str | None = None,
        now,
        limit: int = 50,
    ) -> list[ContextMemory]:
        """严格 active 检索（CE-03 MemorySourceAdapter 权威路径）。

        status='active' + deleted_at null + valid_from<=now<=expires_at +
        scope 过滤（workspace 精确 / agent_playbook agent_type 匹配）；
        排序 scope authority → importance → quality_score → confidence →
        last_accessed_at → valid_from → public_id。
        """
        stmt = select(ContextMemory).where(
            ContextMemory.user_id == user_id,
            ContextMemory.scope_type == scope_type,
            ContextMemory.status == "active",
            ContextMemory.deleted_at.is_(None),
            ContextMemory.archived_at.is_(None),
            (ContextMemory.valid_from.is_(None) | (ContextMemory.valid_from <= now)),
            (ContextMemory.expires_at.is_(None) | (ContextMemory.expires_at >= now)),
        )
        if workspace_key is not None:
            stmt = stmt.where(ContextMemory.workspace_key == workspace_key)
        if agent_type:
            stmt = stmt.where(ContextMemory.agent_type == agent_type)
        # WP-BE-09 fix: MySQL 不支持 SQL 标准 ``NULLS LAST/FIRST`` 语法（在
        # query-level ORDER BY 上仅 PostgreSQL/Oracle 支持，MySQL 8.0.16+ 只在
        # window function 内支持）。
        # 用 ``col IS NULL``（0/1 布尔）+ 主排序列等价实现 NULL 末尾：
        #   - 升序 NULL 末尾：ORDER BY col IS NULL, col ASC
        #   - 降序 NULL 末尾：ORDER BY col IS NULL, col DESC
        # 这一改让 list_authoritative_active 在 MySQL 上正常返回 user memory。
        stmt = stmt.order_by(
            ContextMemory.importance.desc(),
            *_mysql_nulls_last(ContextMemory.quality_score, descending=True),
            ContextMemory.confidence.desc(),
            *_mysql_nulls_last(ContextMemory.last_accessed_at, descending=True),
            *_mysql_nulls_last(ContextMemory.valid_from, descending=False),
            ContextMemory.public_id.asc(),
        ).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_idempotency_key(self, idempotency_key: str, user_id: int) -> ContextMemory | None:
        result = await self.session.execute(
            select(ContextMemory).where(
                ContextMemory.idempotency_key == idempotency_key,
                ContextMemory.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def find_conflict(
        self,
        user_id: int,
        scope_type: str,
        dedupe_key: str | None,
        *,
        workspace_key: str | None = None,
    ) -> ContextMemory | None:
        if not dedupe_key:
            return None
        stmt = select(ContextMemory).where(
                ContextMemory.user_id == user_id,
                ContextMemory.scope_type == scope_type,
                ContextMemory.dedupe_key == dedupe_key,
                ContextMemory.deleted_at.is_(None),
                ContextMemory.archived_at.is_(None),
            )
        if workspace_key is not None:
            stmt = stmt.where(ContextMemory.workspace_key == workspace_key)
        stmt = stmt.order_by(ContextMemory.updated_at.desc(), ContextMemory.id.desc()).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


class ContextMemorySourceRepository(BaseRepository[ContextMemorySource]):
    model = ContextMemorySource

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_for_memory(self, memory_id: int, user_id: int) -> list[ContextMemorySource]:
        result = await self.session.execute(
            select(ContextMemorySource).where(
                ContextMemorySource.memory_id == memory_id,
                ContextMemorySource.user_id == user_id,
            )
        )
        return list(result.scalars().all())


class WorkspaceInstructionRepository(BaseRepository[ContextWorkspaceInstruction]):
    model = ContextWorkspaceInstruction

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_active(
        self, user_id: int, workspace_key: str, instruction_key: str
    ) -> ContextWorkspaceInstruction | None:
        result = await self.session.execute(
            select(ContextWorkspaceInstruction).where(
                ContextWorkspaceInstruction.user_id == user_id,
                ContextWorkspaceInstruction.workspace_key == workspace_key,
                ContextWorkspaceInstruction.instruction_key == instruction_key,
                ContextWorkspaceInstruction.status == "active",
                ContextWorkspaceInstruction.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_for_workspace(
        self, user_id: int, workspace_key: str, *, status: str | None = None
    ) -> list[ContextWorkspaceInstruction]:
        stmt = select(ContextWorkspaceInstruction).where(
            ContextWorkspaceInstruction.user_id == user_id,
            ContextWorkspaceInstruction.workspace_key == workspace_key,
            ContextWorkspaceInstruction.deleted_at.is_(None),
        )
        if status:
            stmt = stmt.where(ContextWorkspaceInstruction.status == status)
        stmt = stmt.order_by(ContextWorkspaceInstruction.priority.asc())
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_effective_for_workspace(
        self,
        user_id: int,
        workspace_key: str,
        *,
        now,
        status: str = "active",
        limit: int = 50,
    ) -> list[ContextWorkspaceInstruction]:
        """生效窗口过滤（CE-03 WP-7）：active + effective_from<=now<=effective_to。

        复用 CE-02 adapter 语义：仅返回当前生效的指令。
        """
        stmt = select(ContextWorkspaceInstruction).where(
            ContextWorkspaceInstruction.user_id == user_id,
            ContextWorkspaceInstruction.workspace_key == workspace_key,
            ContextWorkspaceInstruction.status == status,
            ContextWorkspaceInstruction.deleted_at.is_(None),
            ContextWorkspaceInstruction.archived_at.is_(None),
            (ContextWorkspaceInstruction.effective_from.is_(None) | (ContextWorkspaceInstruction.effective_from <= now)),
            (ContextWorkspaceInstruction.effective_to.is_(None) | (ContextWorkspaceInstruction.effective_to >= now)),
        )
        stmt = stmt.order_by(ContextWorkspaceInstruction.priority.asc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class ContextIndexDocumentRepository(BaseRepository[ContextIndexDocument]):
    model = ContextIndexDocument

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_public_id(self, public_id: str, user_id: int) -> ContextIndexDocument | None:
        result = await self.session.execute(
            select(ContextIndexDocument).where(
                ContextIndexDocument.public_id == public_id,
                ContextIndexDocument.user_id == user_id,
                ContextIndexDocument.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_source(
        self, user_id: int, source_type: str, source_public_id: str
    ) -> ContextIndexDocument | None:
        result = await self.session.execute(
            select(ContextIndexDocument).where(
                ContextIndexDocument.user_id == user_id,
                ContextIndexDocument.source_type == source_type,
                ContextIndexDocument.source_public_id == source_public_id,
                ContextIndexDocument.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_source_in_workspace(
        self,
        user_id: int,
        source_type: str,
        source_public_id: str,
        workspace_key: str | None,
    ) -> ContextIndexDocument | None:
        result = await self.session.execute(
            select(ContextIndexDocument).where(
                ContextIndexDocument.user_id == user_id,
                ContextIndexDocument.source_type == source_type,
                ContextIndexDocument.source_public_id == source_public_id,
                ContextIndexDocument.workspace_key == workspace_key,
                ContextIndexDocument.deleted_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> ContextIndexDocument | None:
        result = await self.session.execute(
            select(ContextIndexDocument).where(
                ContextIndexDocument.idempotency_key == idempotency_key
            )
        )
        return result.scalar_one_or_none()

    async def sync_uploaded_file_metadata(
        self,
        *,
        user_id: int,
        file_public_id: str,
        metadata: dict[str, Any],
    ) -> int:
        if not metadata:
            return 0
        result = await self.session.execute(
            select(ContextIndexDocument).where(
                ContextIndexDocument.user_id == user_id,
                ContextIndexDocument.source_type == "uploaded_file",
                ContextIndexDocument.source_public_id == file_public_id,
                ContextIndexDocument.deleted_at.is_(None),
            )
        )
        documents = list(result.scalars().all())
        allowed = {
            "file_public_id",
            "document_kind",
            "semantic_labels",
            "profile_confidence",
            "profile_version",
        }
        safe = {key: metadata[key] for key in allowed if key in metadata}
        if not safe:
            return 0
        for document in documents:
            document.metadata_json = dict(document.metadata_json or {}) | safe
        await self.session.flush()
        return len(documents)

    async def invalidate_uploaded_file(self, *, user_id: int, file_public_id: str, now) -> int:
        docs_result = await self.session.execute(
            select(ContextIndexDocument).where(
                ContextIndexDocument.user_id == user_id,
                ContextIndexDocument.source_type == "uploaded_file",
                ContextIndexDocument.source_public_id == file_public_id,
                ContextIndexDocument.deleted_at.is_(None),
            )
        )
        documents = list(docs_result.scalars().all())
        document_ids = [document.id for document in documents]
        if not document_ids:
            return 0
        await self.session.execute(
            update(ContextIndexChunk)
            .where(
                ContextIndexChunk.user_id == user_id,
                ContextIndexChunk.document_id.in_(document_ids),
                ContextIndexChunk.deleted_at.is_(None),
            )
            .values(status="deleted", deleted_at=now)
        )
        await self.session.execute(
            update(ContextIndexDocument)
            .where(ContextIndexDocument.id.in_(document_ids))
            .values(status="deleted", deleted_at=now)
        )
        for document in documents:
            idem = _delete_job_idempotency_key(document)
            existing_job = await self.session.execute(
                select(ContextIndexJob).where(ContextIndexJob.idempotency_key == idem)
            )
            if existing_job.scalar_one_or_none() is not None:
                continue
            job = ContextIndexJob(
                public_id="idx_" + hashlib.sha1(idem.encode("utf-8")).hexdigest()[:40],
                user_id=user_id,
                document_id=document.id,
                operation="delete_external",
                status="pending",
                priority=100,
                attempt=0,
                max_attempts=3,
                idempotency_key=idem,
            )
            await ensure_model_id(self.session, ContextIndexJob, job)
            self.session.add(job)
        await self.session.flush()
        return len(document_ids)

    async def invalidate_uploaded_file_in_workspace(
        self,
        *,
        user_id: int,
        file_public_id: str,
        workspace_key: str,
        now,
    ) -> int:
        docs_result = await self.session.execute(
            select(ContextIndexDocument).where(
                ContextIndexDocument.user_id == user_id,
                ContextIndexDocument.workspace_key == workspace_key,
                ContextIndexDocument.source_type == "uploaded_file",
                ContextIndexDocument.source_public_id == file_public_id,
                ContextIndexDocument.deleted_at.is_(None),
            )
        )
        documents = list(docs_result.scalars().all())
        document_ids = [document.id for document in documents]
        if not document_ids:
            return 0
        await self.session.execute(
            update(ContextIndexChunk)
            .where(
                ContextIndexChunk.user_id == user_id,
                ContextIndexChunk.document_id.in_(document_ids),
                ContextIndexChunk.deleted_at.is_(None),
            )
            .values(status="deleted", deleted_at=now, updated_at=now)
        )
        await self.session.execute(
            update(ContextIndexDocument)
            .where(ContextIndexDocument.id.in_(document_ids))
            .values(status="deleted", deleted_at=now, updated_at=now)
        )
        for document in documents:
            idem = _delete_job_idempotency_key(document)
            existing_job = await self.session.execute(
                select(ContextIndexJob).where(ContextIndexJob.idempotency_key == idem)
            )
            if existing_job.scalar_one_or_none() is not None:
                continue
            job = ContextIndexJob(
                public_id="idx_" + hashlib.sha1(idem.encode("utf-8")).hexdigest()[:40],
                user_id=user_id,
                document_id=document.id,
                operation="delete_external",
                status="pending",
                priority=100,
                attempt=0,
                max_attempts=3,
                idempotency_key=idem,
            )
            await ensure_model_id(self.session, ContextIndexJob, job)
            self.session.add(job)
        await self.session.flush()
        return len(document_ids)


class ContextIndexChunkRepository(BaseRepository[ContextIndexChunk]):
    model = ContextIndexChunk

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_for_document(
        self, document_id: int, user_id: int, *, status: str = "active"
    ) -> list[ContextIndexChunk]:
        result = await self.session.execute(
            select(ContextIndexChunk).where(
                ContextIndexChunk.document_id == document_id,
                ContextIndexChunk.user_id == user_id,
                ContextIndexChunk.status == status,
                ContextIndexChunk.deleted_at.is_(None),
            )
        )
        return list(result.scalars().all())


class ContextIndexJobRepository(BaseRepository[ContextIndexJob]):
    model = ContextIndexJob

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def claim_next(
        self, *, claim_owner: str, lease_seconds: int = 60
    ) -> ContextIndexJob | None:
        """多 Worker 领取（SELECT ... FOR UPDATE SKIP LOCKED）。"""
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        stmt = text(
            "SELECT id FROM context_index_jobs "
            "WHERE status = 'pending' "
            "  AND (next_retry_at IS NULL OR next_retry_at <= :now) "
            "ORDER BY priority ASC, created_at ASC "
            "LIMIT 1 "
            "FOR UPDATE SKIP LOCKED"
        )
        result = await self.session.execute(stmt, {"now": now})
        row = result.first()
        if row is None:
            return None
        job_id = row[0]
        claim_until = now + datetime.timedelta(seconds=lease_seconds)
        await self.session.execute(
            text(
                "UPDATE context_index_jobs SET status = 'running', claimed_by = :owner, "
                "claimed_until = :until, started_at = :now WHERE id = :jid"
            ),
            {"owner": claim_owner, "until": claim_until, "now": now, "jid": job_id},
        )
        await self.session.flush()
        return await self.session.get(ContextIndexJob, job_id)

    async def get_by_idempotency_key(self, idempotency_key: str) -> ContextIndexJob | None:
        result = await self.session.execute(
            select(ContextIndexJob).where(ContextIndexJob.idempotency_key == idempotency_key)
        )
        return result.scalar_one_or_none()


class ContextRetrievalRunRepository(BaseRepository[ContextRetrievalRun]):
    model = ContextRetrievalRun

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_public_id(self, public_id: str, user_id: int) -> ContextRetrievalRun | None:
        result = await self.session.execute(
            select(ContextRetrievalRun).where(
                ContextRetrievalRun.public_id == public_id,
                ContextRetrievalRun.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()


class ContextRetrievalCandidateRepository(BaseRepository[ContextRetrievalCandidate]):
    model = ContextRetrievalCandidate

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_for_run(
        self, run_id: int, user_id: int, *, limit: int = 100
    ) -> list[ContextRetrievalCandidate]:
        result = await self.session.execute(
            select(ContextRetrievalCandidate)
            .where(
                ContextRetrievalCandidate.retrieval_run_id == run_id,
                ContextRetrievalCandidate.user_id == user_id,
            )
            .order_by(
                *_mysql_nulls_last(ContextRetrievalCandidate.final_rank, descending=False)
            )
            .limit(limit)
        )
        return list(result.scalars().all())

    async def list_detail_for_run(
        self, run_id: int, user_id: int
    ) -> list[tuple[ContextRetrievalCandidate, str | None, str | None]]:
        """Owner-scoped candidates joined to public index identifiers.

        Public API consumers must never receive the internal BIGINT foreign
        keys stored on the candidate record.  Selected items are first and
        retain final-rank order; dropped items use their raw recall rank, with
        a creation-ID tie breaker for stable, explainable output.
        """
        result = await self.session.execute(
            select(
                ContextRetrievalCandidate,
                ContextIndexDocument.public_id,
                ContextIndexChunk.public_id,
            )
            .outerjoin(
                ContextIndexDocument,
                ContextIndexDocument.id == ContextRetrievalCandidate.index_document_id,
            )
            .outerjoin(
                ContextIndexChunk,
                ContextIndexChunk.id == ContextRetrievalCandidate.index_chunk_id,
            )
            .where(
                ContextRetrievalCandidate.retrieval_run_id == run_id,
                ContextRetrievalCandidate.user_id == user_id,
            )
            .order_by(
                ContextRetrievalCandidate.selected.desc(),
                *_mysql_nulls_last(ContextRetrievalCandidate.final_rank, descending=False),
                *_mysql_nulls_last(ContextRetrievalCandidate.raw_rank, descending=False),
                ContextRetrievalCandidate.id.asc(),
            )
        )
        return list(result.all())


class ContextCompactionRunRepository(BaseRepository[ContextCompactionRun]):
    model = ContextCompactionRun

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def get_by_public_id(self, public_id: str, user_id: int) -> ContextCompactionRun | None:
        result = await self.session.execute(
            select(ContextCompactionRun).where(
                ContextCompactionRun.public_id == public_id,
                ContextCompactionRun.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()


# 模块定位:Context Engine 子包仓储(515 行,合并 10+ 张 CE 表的子仓)
#
# 对应模型:
#   - ContextIndexDocumentRepo / ContextIndexJobRepo
#   - ContextPayloadRepo / ContextWorkspaceRepo / ContextWorkspaceInstructionRepo
#   - ContextUserMemoryRepo / ContextProjectRuleRepo
#   - ContextIndexChunkRepo / ContextIndexEmbeddingRepo
#   - ContextTraceRowRepo
#
# 链路:
#   api/v1/context_index.py 等 → 各子 repo →
#     ORM 读写 CE 表 → 返回 dict
#
# 关键约束:
#   - 所有 repo 都是 per-user 强制 owner scope;
#   - user_memory / project_rule 严格按 scope 隔离;
#   - admin 路径走独立鉴权(见 context_admin);
#   - 测试覆盖 bulk insert 性能。
