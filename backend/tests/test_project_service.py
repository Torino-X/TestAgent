"""Phase 3 Project workspace persistence and ownership contracts."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.core.exceptions import NotFoundError
from app.models.agent_task import AgentTask
from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.project import Project, ProjectSource
from app.models.uploaded_file import UploadedFile
from app.models.user import User
from app.repositories.conversation_repository import ConversationRepository
from app.services.project_knowledge_connection_service import ProjectKnowledgeConnectionService
from app.services.project_service import ProjectService
from app.services.project_source_service import ProjectSourceService


@pytest.mark.asyncio
async def test_sidebar_conversation_query_includes_owning_project_identity(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "user_owner")
        service = ProjectService(session)
        project = await service.create(1, name="Long-lived project")
        conversation = await service.create_conversation(project["id"], 1, "Project chat")
        await session.flush()

        rows = await ConversationRepository(session).list_by_user(1)
        row = next(item for item in rows if item.public_id == conversation["id"])

        assert row._project_public_id == project["id"]
        assert row._project_name == "Long-lived project"


async def _seed_user(session, user_id: int, public_id: str) -> None:
    now = datetime(2026, 9, 8, 8, 0, 0)
    session.add(User(
        id=user_id,
        public_id=public_id,
        username=public_id,
        password_hash="test",
        role="user",
        status="active",
        created_at=now,
        updated_at=now,
    ))
    await session.flush()


@pytest.mark.asyncio
async def test_project_crud_search_pin_instructions_and_ownership(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "user_owner")
        await _seed_user(session, 2, "user_other")
        service = ProjectService(session)

        created = await service.create(1, name=" TestAgent ", description=" workspace ")
        await session.commit()
        assert created["name"] == "TestAgent"
        assert created["memoryMode"] == "project_memory"
        assert created["createdByCurrentUser"] is True

        listed = await service.list_projects(1, query="agent", scope="created")
        assert listed["total"] == 1
        assert listed["items"][0]["id"] == created["id"]
        assert await service.list_projects(1, scope="shared") == {"items": [], "total": 0}

        updated = await service.update(
            created["id"],
            1,
            name="TestAgent 2",
            memory_mode="none",
            instructions="优先使用项目资料。",
        )
        await session.commit()
        assert updated["name"] == "TestAgent 2"
        assert updated["memoryMode"] == "none"
        assert await service.get_instructions(created["id"], 1) == "优先使用项目资料。"

        pinned = await service.set_pinned(created["id"], 1, True)
        assert pinned["pinned"] is True
        with pytest.raises(NotFoundError):
            await service.get_detail(created["id"], 2)


@pytest.mark.asyncio
async def test_project_conversation_move_and_future_task_keeps_project_identity(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "user_owner")
        service = ProjectService(session)
        project_a = await service.create(1, name="A")
        project_b = await service.create(1, name="B")
        conversation = await service.create_conversation(project_a["id"], 1, "项目会话")
        await session.commit()

        moved = await service.move_conversation(conversation["id"], project_b["id"], 1)
        await session.flush()
        project_b_row = (await session.execute(select(Project).where(Project.public_id == project_b["id"]))).scalar_one()
        conv_row = (await session.execute(select(Conversation).where(Conversation.public_id == moved["id"]))).scalar_one()
        assert conv_row.project_id == project_b_row.id

        task = AgentTask(
            id=100,
            public_id="task_project",
            user_id=1,
            conversation_id=conv_row.id,
            project_id=conv_row.project_id,
            task_type="test_plan_generation",
            status="created",
            created_at=datetime(2026, 9, 8, 8, 1, 0),
            updated_at=datetime(2026, 9, 8, 8, 1, 0),
        )
        session.add(task)
        await session.flush()
        assert task.project_id == project_b_row.id

        await service.remove_conversation(conversation["id"], 1)
        await session.flush()
        assert conv_row.project_id is None
        assert task.project_id == project_b_row.id


@pytest.mark.asyncio
async def test_project_source_attach_enforces_file_owner_and_unbind_preserves_library_file(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "user_owner")
        await _seed_user(session, 2, "user_other")
        project = await ProjectService(session).create(1, name="Sources")
        now = datetime(2026, 9, 8, 8, 2, 0)
        session.add_all([
            UploadedFile(
                id=10, public_id="file_owner", user_id=1, conversation_id=None,
                original_name="需求.docx", stored_name="a.docx", file_ext="docx",
                file_size=100, file_type="document", upload_status="uploaded",
                storage_type="local", storage_path="safe/a.docx", created_at=now, updated_at=now,
            ),
            UploadedFile(
                id=11, public_id="file_other", user_id=2, conversation_id=None,
                original_name="越权.docx", stored_name="b.docx", file_ext="docx",
                file_size=100, file_type="document", upload_status="uploaded",
                storage_type="local", storage_path="safe/b.docx", created_at=now, updated_at=now,
            ),
        ])
        await session.flush()
        sources = ProjectSourceService(session)

        attached = await sources.attach(project["id"], 1, "file_owner", source_role="requirement")
        assert attached["fileId"] == "file_owner"
        with pytest.raises(NotFoundError):
            await sources.attach(project["id"], 1, "file_other")

        await sources.remove(project["id"], attached["id"], 1)
        await session.flush()
        relation = (await session.execute(select(ProjectSource))).scalar_one()
        upload = (await session.execute(select(UploadedFile).where(UploadedFile.public_id == "file_owner"))).scalar_one()
        assert relation.deleted_at is not None
        assert upload.deleted_at is None

        reattached = await sources.attach(project["id"], 1, "file_owner", source_role="design")
        assert reattached["id"] == attached["id"]
        assert reattached["sourceRole"] == "design"
        assert relation.deleted_at is None


@pytest.mark.asyncio
async def test_project_delete_detaches_conversations_but_preserves_tasks_artifacts_and_files(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "user_owner")
        service = ProjectService(session)
        project = await service.create(1, name="Delete semantics")
        conversation = await service.create_conversation(project["id"], 1, "保留资产")
        project_row = (await session.execute(select(Project).where(Project.public_id == project["id"]))).scalar_one()
        conv_row = (await session.execute(select(Conversation).where(Conversation.public_id == conversation["id"]))).scalar_one()
        now = datetime(2026, 9, 8, 8, 3, 0)
        task = AgentTask(
            id=200, public_id="task_keep", user_id=1, conversation_id=conv_row.id,
            project_id=project_row.id, task_type="test_plan_generation", status="completed",
            created_at=now, updated_at=now,
        )
        session.add(task)
        await session.flush()
        artifact = Artifact(
            id=300, public_id="art_keep", user_id=1, conversation_id=conv_row.id,
            task_id=task.id, project_id=project_row.id, artifact_type="test_plan_word",
            file_name="方案.docx", file_ext="docx", storage_type="local",
            storage_path="safe/result.docx", status="available", version_no=1,
            created_at=now, updated_at=now,
        )
        session.add(artifact)
        await session.flush()

        artifacts = (await service.get_detail(project["id"], 1))["artifacts"]
        assert [item["id"] for item in artifacts] == ["art_keep"]
        await service.delete(project["id"], 1)
        await session.flush()

        assert project_row.deleted_at is not None
        assert conv_row.project_id is None
        assert task.project_id == project_row.id
        assert artifact.project_id == project_row.id
        assert artifact.deleted_at is None


@pytest.mark.asyncio
async def test_project_knowledge_connections_replace_isolated_by_project(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "user_owner")
        projects = ProjectService(session)
        project_a = await projects.create(1, name="A")
        project_b = await projects.create(1, name="B")
        service = ProjectKnowledgeConnectionService(session)

        assert await service.replace(project_a["id"], 1, ["kb_1", "kb_1", "kb_2"]) == ["kb_1", "kb_2"]
        assert await service.list_ids(project_a["id"], 1) == ["kb_1", "kb_2"]
        assert await service.list_ids(project_b["id"], 1) == []
        assert await service.replace(project_a["id"], 1, ["kb_2"]) == ["kb_2"]
        assert await service.list_ids(project_a["id"], 1) == ["kb_2"]
