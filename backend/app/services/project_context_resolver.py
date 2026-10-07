"""Resolve owner-isolated Project context without reading sibling chat history."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, TypedDict

from sqlalchemy import or_, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_task import AgentTask
from app.models.artifact import Artifact
from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
from app.models.conversation import Conversation
from app.models.project import Project
from app.repositories.context_engine_repositories import (
    ContextMemoryRepository,
    WorkspaceInstructionRepository,
)
from app.repositories.project_source_repository import ProjectSourceRepository


logger = logging.getLogger(__name__)

AUTHORITY_POLICY = {
    "project_source": 100,
    "project_memory": 90,
    "enterprise_rag": 70,
    "generated_artifact": 30,
}

_EVIDENCE_CACHE_DEADLOCK_RETRY_ATTEMPTS = 3


class ProjectContextPackage(TypedDict):
    project_id: str | None
    project_name: str | None
    workspace_key: str | None
    instructions: list[dict[str, Any]]
    memories: list[dict[str, Any]]
    source_hits: list[dict[str, Any]]
    source_refs: list[dict[str, Any]]
    recent_progress: list[dict[str, Any]]
    context_policy: dict[str, Any]


def empty_project_context() -> ProjectContextPackage:
    return {
        "project_id": None,
        "project_name": None,
        "workspace_key": None,
        "instructions": [],
        "memories": [],
        "source_hits": [],
        "source_refs": [],
        "recent_progress": [],
        "context_policy": {
            "project_sources_authority": "primary",
            "company_rag_authority": "reference",
            "generated_artifacts_authority": "derived",
            "authority": dict(AUTHORITY_POLICY),
        },
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _query_terms(value: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", value or "")
        if token.strip()
    }


def _default_evidence_cache_session_factory():
    """Open a short-lived transaction for optional evidence-cache writes."""
    from app.db.session import AsyncSessionLocal

    return AsyncSessionLocal()


class ProjectContextResolver:
    """Build a bounded Project context package from authoritative stores only."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        cache=None,
        evidence_cache_session_factory=None,
    ) -> None:
        self._session = session
        if cache is None:
            from app.cache.domains.project_cache import get_project_cache

            cache = get_project_cache()
        self._cache = cache
        self._evidence_cache_session_factory = (
            evidence_cache_session_factory or _default_evidence_cache_session_factory
        )

    async def resolve(
        self,
        *,
        user_id: int,
        conversation_id: int | str,
        query: str,
        task_id: int | str | None = None,
    ) -> ProjectContextPackage:
        conversation = await self._owned_conversation(user_id, conversation_id)
        if conversation is None or conversation.project_id is None:
            return empty_project_context()
        project = (await self._session.execute(
            select(Project).where(
                Project.id == conversation.project_id,
                Project.user_id == user_id,
                Project.status == "active",
                Project.deleted_at.is_(None),
            )
        )).scalar_one_or_none()
        if project is None:
            return empty_project_context()

        async def load() -> ProjectContextPackage:
            return await self._build_package(
                user_id=user_id,
                conversation_id=int(conversation.id),
                project=project,
                query=query,
                task_id=task_id,
            )

        try:
            result = await self._cache.get_or_load_context(
                user_id,
                project.public_id,
                query=query,
                loader=load,
            )
            return result or empty_project_context()
        except Exception as exc:  # noqa: BLE001 — cache/retrieval is fail-open
            logger.warning(
                "ProjectContextResolver cache degraded | project=%s | err=%s",
                project.public_id,
                type(exc).__name__,
            )
            return await load()

    async def _owned_conversation(self, user_id: int, conversation_id: int | str):
        predicates = [Conversation.user_id == user_id, Conversation.deleted_at.is_(None)]
        if isinstance(conversation_id, int) or str(conversation_id).isdigit():
            identity = int(conversation_id)
            predicates.append(or_(Conversation.id == identity, Conversation.public_id == str(conversation_id)))
        else:
            predicates.append(Conversation.public_id == str(conversation_id))
        return (await self._session.execute(select(Conversation).where(*predicates))).scalar_one_or_none()

    async def _build_package(
        self,
        *,
        user_id: int,
        conversation_id: int,
        project: Project,
        query: str,
        task_id: int | str | None,
    ) -> ProjectContextPackage:
        now = _utcnow()
        instructions = await WorkspaceInstructionRepository(
            self._session
        ).list_effective_for_workspace(
            user_id,
            project.context_workspace_key,
            now=now,
            limit=20,
        )
        memory_rows = []
        if project.memory_mode != "off":
            memory_rows = await ContextMemoryRepository(
                self._session
            ).list_authoritative_active(
                user_id,
                scope_type="workspace",
                workspace_key=project.context_workspace_key,
                now=now,
                limit=50,
            )
        sources = await ProjectSourceRepository(self._session).list_with_files(project.id)
        fresh_source_hits = await self._source_hits(
            user_id=user_id,
            workspace_key=project.context_workspace_key,
            query=query,
        )
        # The conversation evidence cache is an additional candidate layer;
        # it never suppresses fresh project retrieval.  This produces a stable,
        # bounded reusable working set without treating stale cache entries as
        # authoritative project knowledge.
        from app.services.conversation_evidence_cache_service import (
            ConversationEvidenceCacheService,
        )

        source_hits = await self._merge_with_evidence_cache(
            user_id=user_id,
            conversation_id=conversation_id,
            workspace_key=project.context_workspace_key,
            retrieval_hits=fresh_source_hits,
        )
        progress = await self._recent_progress(project.id, task_id=task_id)
        terms = _query_terms(query)
        ranked_memories = sorted(
            memory_rows,
            key=lambda item: (
                -len(terms & _query_terms(item.content)),
                -int(item.importance or 0),
                str(item.public_id),
            ),
        )[:10]
        package = empty_project_context()
        package.update({
            "project_id": project.public_id,
            "project_name": project.name,
            "workspace_key": project.context_workspace_key,
            "instructions": [
                {
                    "id": item.public_id,
                    "key": item.instruction_key,
                    "content": item.content,
                    "priority": int(item.priority or 5),
                    "authority": AUTHORITY_POLICY["project_source"],
                }
                for item in instructions
            ],
            "memories": [
                {
                    "id": item.public_id,
                    "title": item.title,
                    "content": item.content,
                    "memory_type": item.memory_type,
                    "authority": AUTHORITY_POLICY["project_memory"],
                }
                for item in ranked_memories
            ],
            "source_hits": source_hits,
            "source_refs": [
                {
                    "id": source.public_id,
                    "file_id": uploaded.public_id,
                    "title": uploaded.original_name,
                    "source_role": source.source_role,
                    "is_current": bool(source.is_current),
                }
                for source, uploaded in sources
            ],
            "recent_progress": progress,
        })
        return package

    async def _merge_with_evidence_cache(
        self,
        *,
        user_id: int,
        conversation_id: int,
        workspace_key: str,
        retrieval_hits: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Persist optional reusable evidence without sharing the chat transaction.

        A cache audit is observational.  In MySQL, a deadlock rolls back the
        whole transaction, so performing this write on the request session can
        make later context reads fail with ``PendingRollbackError``.  Give the
        cache a short independent transaction and retain fresh retrieval hits
        when all bounded retries encounter a lock conflict.
        """
        from app.services.conversation_evidence_cache_service import (
            ConversationEvidenceCacheService,
        )

        for attempt in range(1, _EVIDENCE_CACHE_DEADLOCK_RETRY_ATTEMPTS + 1):
            try:
                async with self._evidence_cache_session_factory() as cache_session:
                    async with cache_session.begin():
                        return await ConversationEvidenceCacheService(
                            cache_session
                        ).merge_with_retrieval(
                            user_id=user_id,
                            conversation_id=conversation_id,
                            workspace_key=workspace_key,
                            retrieval_hits=retrieval_hits,
                        )
            except OperationalError as exc:
                if self._is_retryable_cache_lock(exc) and attempt < _EVIDENCE_CACHE_DEADLOCK_RETRY_ATTEMPTS:
                    logger.warning(
                        "Conversation evidence cache deadlocked; retrying | "
                        "conversation_id=%s | attempt=%s/%s",
                        conversation_id,
                        attempt,
                        _EVIDENCE_CACHE_DEADLOCK_RETRY_ATTEMPTS,
                    )
                    await asyncio.sleep(0.05 * attempt)
                    continue
                logger.warning(
                    "Conversation evidence cache unavailable; using fresh retrieval "
                    "hits | conversation_id=%s | error=%s",
                    conversation_id,
                    type(exc).__name__,
                )
                return list(retrieval_hits)

        return list(retrieval_hits)

    @staticmethod
    def _is_retryable_cache_lock(exc: OperationalError) -> bool:
        original = getattr(exc, "orig", None)
        args = getattr(original, "args", ()) or ()
        code = args[0] if args else None
        message = str(original or exc).casefold()
        return code in {1205, 1213} or "deadlock" in message or "lock wait timeout" in message

    async def _source_hits(
        self,
        *,
        user_id: int,
        workspace_key: str,
        query: str,
    ) -> list[dict[str, Any]]:
        rows = (await self._session.execute(
            select(ContextIndexChunk, ContextIndexDocument)
            .join(ContextIndexDocument, ContextIndexDocument.id == ContextIndexChunk.document_id)
            .where(
                ContextIndexChunk.user_id == user_id,
                ContextIndexChunk.status == "active",
                ContextIndexChunk.deleted_at.is_(None),
                ContextIndexDocument.user_id == user_id,
                ContextIndexDocument.workspace_key == workspace_key,
                ContextIndexDocument.status == "indexed",
                ContextIndexDocument.deleted_at.is_(None),
            )
            .limit(100)
        )).all()
        terms = _query_terms(query)
        ranked: list[tuple[int, int, str, ContextIndexChunk, ContextIndexDocument]] = []
        for chunk, document in rows:
            metadata = dict(document.metadata_json or {})
            overlap = len(terms & _query_terms(chunk.normalized_content or chunk.content))
            current = bool(metadata.get("is_current", True))
            role = str(metadata.get("source_role") or "other")
            authority = AUTHORITY_POLICY["project_source"] if current else 80
            role_boost = 2 if role in {"requirement", "api_spec", "design", "technical_spec"} else 0
            ranked.append((overlap, authority + role_boost, chunk.public_id, chunk, document))
        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [
            {
                "chunk_id": chunk.public_id,
                "document_id": document.public_id,
                "source_id": document.source_public_id,
                "title": document.title,
                "section": chunk.section_path,
                "content": chunk.content[:1600],
                "score": overlap,
                "source_role": (document.metadata_json or {}).get("source_role", "other"),
                "source_version": document.source_version,
                "updated_at": document.updated_at.isoformat() if document.updated_at else None,
                "is_current": bool((document.metadata_json or {}).get("is_current", True)),
                "authority": (
                    AUTHORITY_POLICY["project_source"]
                    if bool((document.metadata_json or {}).get("is_current", True))
                    else 80
                ),
            }
            for overlap, authority, _public_id, chunk, document in ranked[:5]
            if overlap > 0 or not terms
        ]

    async def _recent_progress(
        self,
        project_id: int,
        *,
        task_id: int | str | None,
    ) -> list[dict[str, Any]]:
        stmt = select(AgentTask).where(
            AgentTask.project_id == project_id,
            AgentTask.status == "completed",
            AgentTask.deleted_at.is_(None),
        )
        if task_id is not None:
            if isinstance(task_id, int) or str(task_id).isdigit():
                stmt = stmt.where(AgentTask.id != int(task_id))
            else:
                stmt = stmt.where(AgentTask.public_id != str(task_id))
        tasks = list((await self._session.execute(
            stmt.order_by(AgentTask.completed_at.desc(), AgentTask.updated_at.desc()).limit(5)
        )).scalars())
        if not tasks:
            return []
        artifacts = list((await self._session.execute(
            select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.task_id.in_([task.id for task in tasks]),
                Artifact.deleted_at.is_(None),
            )
        )).scalars())
        artifacts_by_task: dict[int, list[dict[str, Any]]] = {}
        for artifact in artifacts:
            artifacts_by_task.setdefault(artifact.task_id, []).append({
                "id": artifact.public_id,
                "file_name": artifact.file_name,
                "artifact_type": artifact.artifact_type,
                "version_no": artifact.version_no,
            })
        return [
            {
                "task_id": task.public_id,
                "title": task.title,
                "summary": ((task.task_context_json or {}).get("completion_summary") if isinstance(task.task_context_json, dict) else None),
                "completed_at": (task.completed_at or task.updated_at).isoformat() if (task.completed_at or task.updated_at) else None,
                "artifacts": artifacts_by_task.get(task.id, []),
            }
            for task in tasks
        ]


__all__ = [
    "AUTHORITY_POLICY",
    "ProjectContextPackage",
    "ProjectContextResolver",
    "empty_project_context",
]
