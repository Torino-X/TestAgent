"""Project workspace core business service."""

from __future__ import annotations

import hashlib

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError, ValidationError
from app.models.artifact import Artifact
from app.models.context_engine import (
    ContextIndexChunk,
    ContextIndexDocument,
    ContextMemory,
    ContextMemorySource,
    ContextWorkspaceInstruction,
)
from app.models.conversation import Conversation
from app.models.project import Project
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.base import ensure_model_id
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.project_source_repository import ProjectSourceRepository
from app.services.project_source_service import ProjectSourceService
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id


_MEMORY_TO_DB = {"project_memory": "project_only", "none": "off", "project_only": "project_only", "off": "off"}
_MEMORY_TO_API = {"project_only": "project_memory", "off": "none"}


class ProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._conversations = ConversationRepository(session)
        self._sources = ProjectSourceRepository(session)
        self._artifacts = ArtifactRepository(session)

    async def create(self, user_id: int, *, name: str, description: str = "", memory_mode: str = "project_memory") -> dict:
        clean_name = name.strip()
        if not clean_name:
            raise ValidationError("请填写项目名称")
        public_id = generate_public_id("project")
        now = utcnow()
        project = await self._projects.create(Project(
            public_id=public_id,
            user_id=user_id,
            name=clean_name,
            description=description.strip(),
            memory_mode=self._normalize_memory_mode(memory_mode),
            status="active",
            context_workspace_key=f"project:{public_id}",
            last_activity_at=now,
            created_at=now,
            updated_at=now,
        ))
        self._register_cache_invalidation(user_id, public_id)
        return await self._to_detail(project)

    async def list_projects(self, user_id: int, *, query: str = "", scope: str = "all", page: int = 1, page_size: int = 20) -> dict:
        if scope not in {"all", "created", "shared"}:
            raise ValidationError("scope 必须为 all/created/shared")
        page = max(1, page)
        page_size = max(1, min(100, page_size))

        async def load():
            if scope == "shared":
                return {"items": [], "total": 0}
            projects = await self._projects.list_owned(
                user_id,
                query=query.strip(),
                offset=(page - 1) * page_size,
                limit=page_size,
            )
            total = await self._projects.count_owned(user_id, query=query.strip())
            return {"items": [self._to_summary(project) for project in projects], "total": total}

        from app.cache.domains.project_cache import get_project_cache
        return await get_project_cache().get_or_load_list(
            user_id,
            query=query,
            scope=scope,
            page=page,
            page_size=page_size,
            loader=load,
        )

    async def get_detail(self, project_public_id: str, user_id: int) -> dict:
        async def load():
            project = await self._require_project(project_public_id, user_id)
            return await self._to_detail(project)

        from app.cache.domains.project_cache import get_project_cache
        result = await get_project_cache().get_or_load_detail(user_id, project_public_id, load)
        if result is None:
            raise NotFoundError("项目")
        return result

    async def update(
        self,
        project_public_id: str,
        user_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        memory_mode: str | None = None,
        instructions: str | None = None,
    ) -> dict:
        project = await self._require_project(project_public_id, user_id)
        if name is not None:
            if not name.strip():
                raise ValidationError("请填写项目名称")
            project.name = name.strip()
        if description is not None:
            project.description = description.strip()
        if memory_mode is not None:
            project.memory_mode = self._normalize_memory_mode(memory_mode)
        now = utcnow()
        project.updated_at = now
        project.last_activity_at = now
        if instructions is not None:
            await self.update_instructions(project_public_id, user_id, instructions)
        self._register_cache_invalidation(user_id, project_public_id)
        if name is not None:
            self._register_conversation_cache_invalidation(user_id)
        return await self._to_detail(project)

    async def set_pinned(self, project_public_id: str, user_id: int, pinned: bool) -> dict:
        project = await self._require_project(project_public_id, user_id)
        now = utcnow()
        project.pinned_at = now if pinned else None
        project.updated_at = now
        self._register_cache_invalidation(user_id, project_public_id)
        return self._to_summary(project)

    async def delete(self, project_public_id: str, user_id: int) -> None:
        project = await self._require_project(project_public_id, user_id)
        now = utcnow()
        project.status = "deleted"
        project.deleted_at = now
        project.updated_at = now
        await self._conversations.detach_project(project.id, now)
        await self._sources.soft_delete_all(project.id, now)
        await self._session.execute(
            update(ContextMemory)
            .where(
                ContextMemory.user_id == user_id,
                ContextMemory.workspace_key == project.context_workspace_key,
                ContextMemory.deleted_at.is_(None),
            )
            .values(status="archived", archived_at=now, updated_at=now)
        )
        await self._session.execute(
            update(ContextWorkspaceInstruction)
            .where(
                ContextWorkspaceInstruction.user_id == user_id,
                ContextWorkspaceInstruction.workspace_key == project.context_workspace_key,
                ContextWorkspaceInstruction.deleted_at.is_(None),
            )
            .values(status="archived", archived_at=now, updated_at=now)
        )
        document_ids = select(ContextIndexDocument.id).where(
            ContextIndexDocument.user_id == user_id,
            ContextIndexDocument.workspace_key == project.context_workspace_key,
        )
        await self._session.execute(
            update(ContextIndexChunk)
            .where(ContextIndexChunk.document_id.in_(document_ids), ContextIndexChunk.deleted_at.is_(None))
            .values(deleted_at=now, updated_at=now)
        )
        await self._session.execute(
            update(ContextIndexDocument)
            .where(
                ContextIndexDocument.user_id == user_id,
                ContextIndexDocument.workspace_key == project.context_workspace_key,
                ContextIndexDocument.deleted_at.is_(None),
            )
            .values(deleted_at=now, updated_at=now)
        )
        self._register_cache_invalidation(user_id, project_public_id)
        self._register_conversation_cache_invalidation(user_id)

    async def list_conversations(self, project_public_id: str, user_id: int) -> list[dict]:
        project = await self._require_project(project_public_id, user_id)
        rows = await self._conversations.list_by_project(user_id, project.id)
        return [self._conversation_preview(row) for row in rows]

    async def create_conversation(self, project_public_id: str, user_id: int, title: str = "新对话") -> dict:
        project = await self._require_project(project_public_id, user_id)
        now = utcnow()
        conversation = await self._conversations.create(Conversation(
            public_id=generate_public_id("conversation"),
            user_id=user_id,
            project_id=project.id,
            title=title.strip() or "新对话",
            status="active",
            context_workspace_key=None,
            context_memory_mode="inherit",
            knowledge_mode="AUTO",
            created_at=now,
            updated_at=now,
        ))
        await self._projects.touch(project.id, now)
        self._register_cache_invalidation(user_id, project_public_id)
        self._register_conversation_cache_invalidation(user_id)
        return self._conversation_preview(conversation)

    async def move_conversation(self, conversation_public_id: str, project_public_id: str, user_id: int) -> dict:
        project = await self._require_project(project_public_id, user_id)
        conversation = await self._conversations.get_owned_by_public_id(conversation_public_id, user_id)
        if conversation is None:
            raise NotFoundError("会话")
        now = utcnow()
        old_project_id = conversation.project_id
        if old_project_id is not None and old_project_id != project.id:
            old_project = await self._session.get(Project, old_project_id)
            if old_project is not None and old_project.user_id == user_id:
                await self._archive_conversation_only_memories(
                    user_id=user_id,
                    workspace_key=old_project.context_workspace_key,
                    conversation_public_id=conversation.public_id,
                    now=now,
                )
                self._register_cache_invalidation(user_id, old_project.public_id)
        conversation.project_id = project.id
        conversation.updated_at = now
        await self._projects.touch(project.id, now)
        self._register_cache_invalidation(user_id, project_public_id)
        self._register_conversation_cache_invalidation(user_id, conversation_public_id)
        return self._conversation_preview(conversation)

    async def remove_conversation(self, conversation_public_id: str, user_id: int) -> None:
        conversation = await self._conversations.get_owned_by_public_id(conversation_public_id, user_id)
        if conversation is None:
            raise NotFoundError("会话")
        old_project_id = conversation.project_id
        now = utcnow()
        if old_project_id is not None:
            old_project = await self._session.get(Project, old_project_id)
            if old_project is not None and old_project.user_id == user_id:
                await self._archive_conversation_only_memories(
                    user_id=user_id,
                    workspace_key=old_project.context_workspace_key,
                    conversation_public_id=conversation.public_id,
                    now=now,
                )
                self._register_cache_invalidation(user_id, old_project.public_id)
        conversation.project_id = None
        conversation.updated_at = now
        self._register_conversation_cache_invalidation(user_id, conversation_public_id)

    async def _archive_conversation_only_memories(
        self,
        *,
        user_id: int,
        workspace_key: str,
        conversation_public_id: str,
        now,
    ) -> None:
        rows = (await self._session.execute(
            select(ContextMemory, ContextMemorySource)
            .join(ContextMemorySource, ContextMemorySource.memory_id == ContextMemory.id)
            .where(
                ContextMemory.user_id == user_id,
                ContextMemory.workspace_key == workspace_key,
                ContextMemory.status.in_(["active", "candidate"]),
                ContextMemory.deleted_at.is_(None),
                ContextMemory.archived_at.is_(None),
            )
        )).all()
        sources_by_memory: dict[int, tuple[ContextMemory, list[ContextMemorySource]]] = {}
        for memory, source in rows:
            entry = sources_by_memory.setdefault(memory.id, (memory, []))
            entry[1].append(source)
        for memory, sources in sources_by_memory.values():
            if sources and all(
                source.source_type == "conversation"
                and source.source_public_id == conversation_public_id
                for source in sources
            ):
                memory.status = "archived"
                memory.archived_at = now
                memory.updated_at = now

    async def get_instructions(self, project_public_id: str, user_id: int) -> str:
        project = await self._require_project(project_public_id, user_id)
        row = (await self._session.execute(
            select(ContextWorkspaceInstruction)
            .where(
                ContextWorkspaceInstruction.user_id == user_id,
                ContextWorkspaceInstruction.workspace_key == project.context_workspace_key,
                ContextWorkspaceInstruction.instruction_key == "project_instructions",
                ContextWorkspaceInstruction.status == "active",
                ContextWorkspaceInstruction.deleted_at.is_(None),
            )
            .order_by(ContextWorkspaceInstruction.version.desc(), ContextWorkspaceInstruction.id.desc())
            .limit(1)
        )).scalar_one_or_none()
        return row.content if row is not None else ""

    async def update_instructions(self, project_public_id: str, user_id: int, content: str) -> str:
        project = await self._require_project(project_public_id, user_id)
        now = utcnow()
        rows = list((await self._session.execute(
            select(ContextWorkspaceInstruction).where(
                ContextWorkspaceInstruction.user_id == user_id,
                ContextWorkspaceInstruction.workspace_key == project.context_workspace_key,
                ContextWorkspaceInstruction.instruction_key == "project_instructions",
                ContextWorkspaceInstruction.deleted_at.is_(None),
            )
        )).scalars().all())
        max_version = max((row.version for row in rows), default=0)
        for row in rows:
            if row.status == "active":
                row.status = "archived"
                row.archived_at = now
                row.updated_at = now
        clean_content = content.strip()
        if clean_content:
            digest = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()
            instruction = ContextWorkspaceInstruction(
                public_id=generate_public_id("wi"),
                user_id=user_id,
                workspace_key=project.context_workspace_key,
                instruction_key="project_instructions",
                category="project",
                title="项目指令",
                content=clean_content,
                priority=1,
                status="active",
                source_type="manual",
                content_hash=digest,
                idempotency_key=f"{project.public_id}:{digest}",
                version=max_version + 1,
                created_by_user_id=user_id,
                effective_from=now,
                created_at=now,
                updated_at=now,
            )
            await ensure_model_id(self._session, ContextWorkspaceInstruction, instruction)
            self._session.add(instruction)
            await self._session.flush()
        await self._projects.touch(project.id, now)
        self._register_cache_invalidation(user_id, project_public_id)
        self._register_instruction_cache_invalidation(user_id, project.context_workspace_key)
        return clean_content

    async def _require_project(self, public_id: str, user_id: int) -> Project:
        project = await self._projects.get_owned(public_id, user_id)
        if project is None:
            raise NotFoundError("项目")
        return project

    async def _to_detail(self, project: Project) -> dict:
        conversations = await self._conversations.list_by_project(project.user_id, project.id)
        sources = await ProjectSourceService(self._session).list_sources(project.public_id, project.user_id)
        artifacts = await self._artifacts.list_by_project(project.user_id, project.id)
        instructions = await self.get_instructions(project.public_id, project.user_id)
        return {
            **self._to_summary(project),
            "instructions": instructions,
            "conversations": [self._conversation_preview(row) for row in conversations],
            "sources": sources,
            "artifacts": [self._artifact_preview(row) for row in artifacts],
        }

    @staticmethod
    def _to_summary(project: Project) -> dict:
        return {
            "id": project.public_id,
            "name": project.name,
            "description": project.description or "",
            "memoryMode": _MEMORY_TO_API.get(project.memory_mode, "project_memory"),
            "pinned": project.pinned_at is not None,
            "updatedAt": project.updated_at.isoformat() if project.updated_at else "",
            "createdAt": project.created_at.isoformat() if project.created_at else "",
            "createdByCurrentUser": True,
        }

    @staticmethod
    def _conversation_preview(conversation: Conversation) -> dict:
        return {
            "id": conversation.public_id,
            "title": conversation.title,
            "summary": conversation.summary or "",
            "updatedAt": conversation.updated_at.isoformat() if conversation.updated_at else "",
        }

    @staticmethod
    def _artifact_preview(artifact: Artifact) -> dict:
        type_map = {
            "test_plan_word": "测试方案",
            "test_case": "测试用例",
            "review_report": "审查报告",
        }
        return {
            "id": artifact.public_id,
            "name": artifact.file_name,
            "type": type_map.get(artifact.artifact_type, "其他资产"),
            "updatedAt": artifact.updated_at.isoformat() if artifact.updated_at else "",
        }

    @staticmethod
    def _normalize_memory_mode(memory_mode: str) -> str:
        try:
            return _MEMORY_TO_DB[memory_mode]
        except KeyError as exc:
            raise ValidationError("memoryMode 必须为 project_memory/none") from exc

    def _register_cache_invalidation(self, user_id: int, project_public_id: str) -> None:
        from app.cache.domains.project_cache import get_project_cache
        from app.db.sync import register_after_commit

        async def invalidate(_session):
            await get_project_cache().invalidate(user_id, project_public_id)

        register_after_commit(self._session, invalidate)

    def _register_conversation_cache_invalidation(self, user_id: int, conversation_public_id: str | None = None) -> None:
        from app.cache.domains.conversation_cache import get_conversation_cache
        from app.db.sync import register_after_commit

        async def invalidate(_session):
            cache = get_conversation_cache()
            await cache.bump_generation(user_id)
            if conversation_public_id:
                await cache.invalidate_detail(user_id, conversation_public_id)

        register_after_commit(self._session, invalidate)

    def _register_instruction_cache_invalidation(self, user_id: int, workspace_key: str) -> None:
        from app.cache.domains.context_cache import get_workspace_instruction_cache, workspace_hash_for
        from app.db.sync import register_after_commit

        async def invalidate(_session):
            await get_workspace_instruction_cache().invalidate(user_id, workspace_hash_for(workspace_key))

        register_after_commit(self._session, invalidate)
