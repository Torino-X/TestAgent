"""Phase 4 Project context, memory, and runtime contracts."""

from __future__ import annotations

from datetime import datetime
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.agent_runtime.graphs.test_plan.state import make_empty_state
from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
    _knowledge_search_inputs_from_state,
    search_knowledge_node,
)
from app.agent_runtime.runtime_context import RuntimeContextFactory
from app.models.agent_task import AgentTask
from app.models.artifact import Artifact
from app.models.context_engine import (
    ContextIndexChunk,
    ContextIndexDocument,
    ContextMemory,
    ContextWorkspaceInstruction,
)
from app.models.project import Project
from app.models.uploaded_file import UploadedFile
from app.models.user import User
from app.services.chat_llm_service import ChatLLMService
from app.services.project_context_resolver import ProjectContextResolver
from app.services.project_memory_service import ProjectMemoryService
from app.services.message_service import MessageService
from app.services.project_service import ProjectService
from app.services.project_source_service import ProjectSourceService


NOW = datetime(2026, 9, 8, 10, 0, 0)


async def _seed_user(session, user_id: int, public_id: str) -> None:
    session.add(User(
        id=user_id,
        public_id=public_id,
        username=public_id,
        password_hash="test",
        role="user",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    ))
    await session.flush()


async def _seed_context(session, *, user_id: int, project, marker: str) -> None:
    session.add(ContextWorkspaceInstruction(
        id=100 + project.id,
        public_id=f"ins_{marker}",
        user_id=user_id,
        workspace_key=project.context_workspace_key,
        instruction_key="project_instructions",
        category="project",
        title=f"{marker} instruction",
        content=f"只使用 {marker} 规则",
        priority=1,
        status="active",
        source_type="manual",
        content_hash=marker * 64,
        idempotency_key=f"idem_ins_{marker}",
        version=1,
        created_by_user_id=user_id,
        created_at=NOW,
        updated_at=NOW,
    ))
    session.add(ContextMemory(
        id=200 + project.id,
        public_id=f"mem_{marker}",
        user_id=user_id,
        scope_type="workspace",
        workspace_key=project.context_workspace_key,
        memory_type="decision",
        title=f"{marker} decision",
        content=f"{marker} 已确认采用 OAuth2",
        normalized_content=f"{marker} oauth2",
        status="active",
        activation_source="manual",
        confidence=1,
        importance=5,
        content_hash=(marker.lower() * 64)[:64],
        dedupe_key=f"decision_{marker}",
        idempotency_key=f"idem_mem_{marker}",
        version=1,
        created_by_type="user",
        created_by_user_id=user_id,
        created_at=NOW,
        updated_at=NOW,
    ))
    document = ContextIndexDocument(
        id=300 + project.id,
        public_id=f"idoc_{marker}",
        user_id=user_id,
        workspace_key=project.context_workspace_key,
        source_type="uploaded_file",
        source_public_id=f"file_{marker}",
        source_version="1",
        source_digest=(marker.upper() * 64)[:64],
        title=f"{marker} spec",
        status="indexed",
        chunk_policy_key="paragraph_v1",
        chunk_policy_version="v1",
        lexical_index_status="ready",
        vector_index_status="pending",
        metadata_json={"source_role": "requirement", "is_current": True},
        idempotency_key=f"idem_doc_{marker}",
        created_at=NOW,
        updated_at=NOW,
    )
    session.add(document)
    await session.flush()
    session.add(ContextIndexChunk(
        id=400 + project.id,
        public_id=f"ichunk_{marker}",
        document_id=document.id,
        user_id=user_id,
        chunk_index=0,
        section_path="认证",
        content=f"{marker} OAuth2 回调必须校验 state",
        normalized_content=f"{marker.lower()} oauth2 state",
        content_hash=(marker * 32)[:64],
        char_count=24,
        status="active",
        created_at=NOW,
        updated_at=NOW,
    ))
    await session.flush()


@pytest.mark.asyncio
async def test_resolver_is_owner_scoped_and_project_isolated(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "owner")
        await _seed_user(session, 2, "other")
        projects = ProjectService(session)
        pa = await projects.create(1, name="Alpha")
        pb = await projects.create(1, name="Beta")
        ca = await projects.create_conversation(pa["id"], 1, "A chat")
        cb = await projects.create_conversation(pb["id"], 1, "B chat")
        standalone = await projects.create_conversation(pa["id"], 1, "standalone")
        await projects.remove_conversation(standalone["id"], 1)
        project_rows = {
            row.public_id: row
            for row in (await session.execute(select(Project))).scalars()
        }
        await _seed_context(session, user_id=1, project=project_rows[pa["id"]], marker="A")
        await _seed_context(session, user_id=1, project=project_rows[pb["id"]], marker="B")
        await session.commit()

        resolver = ProjectContextResolver(
            session,
            evidence_cache_session_factory=sqlite_session_factory,
        )
        conv_a = await projects._conversations.get_owned_by_public_id(ca["id"], 1)
        conv_b = await projects._conversations.get_owned_by_public_id(cb["id"], 1)
        conv_none = await projects._conversations.get_owned_by_public_id(standalone["id"], 1)
        ctx_a = await resolver.resolve(user_id=1, conversation_id=conv_a.id, query="OAuth2 state")
        ctx_b = await resolver.resolve(user_id=1, conversation_id=conv_b.id, query="OAuth2 state")

        assert ctx_a["project_id"] == pa["id"]
        assert "knowledge_ids" not in ctx_a
        assert ctx_a["source_hits"] and all(
            hit["authority"] == 100 for hit in ctx_a["source_hits"]
        )
        a_payload = " ".join(
            str(item.get("content") or "")
            for key in ("instructions", "memories", "source_hits")
            for item in ctx_a[key]
        )
        assert "A" in a_payload and "B" not in a_payload
        assert ctx_b["project_id"] == pb["id"]
        b_payload = " ".join(
            str(item.get("content") or "")
            for key in ("instructions", "memories", "source_hits")
            for item in ctx_b[key]
        )
        assert "B" in b_payload and "A 已确认" not in b_payload and "A 规则" not in b_payload
        assert (await resolver.resolve(user_id=1, conversation_id=conv_none.id, query="x"))["project_id"] is None
        assert (await resolver.resolve(user_id=2, conversation_id=conv_a.id, query="x"))["project_id"] is None


@pytest.mark.asyncio
async def test_project_memory_completion_is_active_idempotent_and_workspace_scoped(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "owner")
        projects = ProjectService(session)
        pa = await projects.create(1, name="Alpha")
        pb = await projects.create(1, name="Beta")
        ca = await projects.create_conversation(pa["id"], 1, "A chat")
        project_a = (await session.execute(select(Project).where(
            Project.public_id == pa["id"]
        ))).scalar_one()
        conv_a = await projects._conversations.get_owned_by_public_id(ca["id"], 1)
        task = AgentTask(
            id=901, public_id="task_phase4", user_id=1, conversation_id=conv_a.id,
            project_id=project_a.id, task_type="test_plan_generation", status="completed",
            title="认证测试", created_at=NOW, updated_at=NOW,
        )
        session.add(task)
        await session.flush()
        session.add(Artifact(
            id=902, public_id="art_phase4", user_id=1, conversation_id=conv_a.id,
            task_id=task.id, project_id=project_a.id, artifact_type="test_plan_word",
            file_name="认证测试方案.docx", file_ext="docx", storage_type="local",
            storage_path="safe/result.docx", status="available", version_no=1,
            created_at=NOW, updated_at=NOW,
        ))
        await session.flush()

        writer = ProjectMemoryService(session)
        first = await writer.record_task_completion(
            user_id=1,
            project_public_id=pa["id"],
            task_public_id=task.public_id,
            summary="已完成认证模块测试方案",
            artifact={"public_id": "art_phase4", "file_name": "认证测试方案.docx"},
            confirmed_sections=[{"title": "OAuth2 登录"}],
        )
        second = await writer.record_task_completion(
            user_id=1,
            project_public_id=pa["id"],
            task_public_id=task.public_id,
            summary="重复回放不应新增",
        )
        await session.flush()

        rows = list((await session.execute(select(ContextMemory).where(
            ContextMemory.dedupe_key == "project_progress:task_phase4"
        ))).scalars())
        assert first["created"] is True
        assert second["created"] is False
        assert len(rows) == 1 and rows[0].status == "active"
        assert rows[0].workspace_key == f"project:{pa['id']}"
        assert pb["id"] not in rows[0].content


@pytest.mark.asyncio
async def test_project_chat_fact_update_supersedes_only_current_workspace_fact(sqlite_session_factory):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "owner")
        project = await ProjectService(session).create(1, name="Alpha")
        writer = ProjectMemoryService(session)

        await writer.record_chat_facts(
            user_id=1,
            project_public_id=project["id"],
            conversation_public_id="conv_alpha",
            facts=[{"key": "auth_strategy", "content": "Use OAuth2"}],
        )
        await writer.record_chat_facts(
            user_id=1,
            project_public_id=project["id"],
            conversation_public_id="conv_alpha",
            facts=[{"key": "auth_strategy", "content": "Use OIDC"}],
        )
        await session.flush()

        rows = list((await session.execute(select(ContextMemory).where(
            ContextMemory.user_id == 1,
            ContextMemory.workspace_key == f"project:{project['id']}",
            ContextMemory.dedupe_key == "project_fact:auth_strategy",
        ).order_by(ContextMemory.id))).scalars())
        assert [row.status for row in rows] == ["archived", "active"]
        assert rows[1].supersedes_memory_id == rows[0].id


@pytest.mark.asyncio
async def test_move_out_archives_only_memories_sourced_exclusively_from_conversation(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "owner")
        projects = ProjectService(session)
        project = await projects.create(1, name="Alpha")
        conversation = await projects.create_conversation(project["id"], 1, "Alpha chat")
        writer = ProjectMemoryService(session)
        await writer.record_chat_facts(
            user_id=1,
            project_public_id=project["id"],
            conversation_public_id=conversation["id"],
            facts=[{"key": "auth", "content": "Use OAuth2"}],
        )
        await writer.record_task_completion(
            user_id=1,
            project_public_id=project["id"],
            task_public_id="task_keep",
            summary="Keep task progress",
        )

        await projects.remove_conversation(conversation["id"], 1)
        await session.flush()
        rows = list((await session.execute(select(ContextMemory).where(
            ContextMemory.workspace_key == f"project:{project['id']}"
        ))).scalars())
        status_by_key = {row.dedupe_key: row.status for row in rows}
        assert status_by_key["project_fact:auth"] == "archived"
        assert status_by_key["project_progress:task_keep"] == "active"


@pytest.mark.asyncio
async def test_one_library_file_has_isolated_project_indexes_and_unbind_is_scoped(
    sqlite_session_factory,
    monkeypatch,
):
    from app.context_engine.indexing.document_service import IndexDocumentService

    monkeypatch.setattr(
        IndexDocumentService,
        "_read_file_bytes",
        staticmethod(lambda _path, _storage_type="local": b"oauth2 state requirement"),
    )
    async with sqlite_session_factory() as session:
        await _seed_user(session, 1, "owner")
        projects = ProjectService(session)
        pa = await projects.create(1, name="Alpha")
        pb = await projects.create(1, name="Beta")
        session.add(UploadedFile(
            id=950,
            public_id="file_shared",
            user_id=1,
            conversation_id=None,
            original_name="shared.md",
            stored_name="shared.md",
            file_ext="md",
            file_size=24,
            file_type="document",
            upload_status="uploaded",
            storage_type="local",
            storage_path="not-read-by-test.md",
            created_at=NOW,
            updated_at=NOW,
        ))
        await session.flush()
        sources = ProjectSourceService(session)
        source_a = await sources.attach(pa["id"], 1, "file_shared", source_role="requirement")
        await sources.attach(pb["id"], 1, "file_shared", source_role="technical_spec")
        documents = list((await session.execute(
            select(ContextIndexDocument).where(
                ContextIndexDocument.source_public_id == "file_shared",
                ContextIndexDocument.deleted_at.is_(None),
            )
        )).scalars())
        assert {item.workspace_key for item in documents} == {
            f"project:{pa['id']}",
            f"project:{pb['id']}",
        }

        await sources.remove(pa["id"], source_a["id"], 1)
        remaining = list((await session.execute(
            select(ContextIndexDocument).where(
                ContextIndexDocument.source_public_id == "file_shared",
                ContextIndexDocument.deleted_at.is_(None),
            )
        )).scalars())
        assert [item.workspace_key for item in remaining] == [f"project:{pb['id']}"]
        uploaded = (await session.execute(select(UploadedFile).where(
            UploadedFile.public_id == "file_shared"
        ))).scalar_one()
        assert uploaded.deleted_at is None


@pytest.mark.asyncio
async def test_v3_knowledge_fusion_and_optional_runtime_fields_preserve_standalone():
    project_context = {
        "project_id": "project_alpha",
        "instructions": [{"content": "follow alpha"}],
        "memories": [{"content": "confirmed alpha"}],
        "source_hits": [{"content": "alpha source"}],
    }
    inputs = _knowledge_search_inputs_from_state({"user_prompt": "query", "project_context": project_context})
    assert "knowledge_ids" not in inputs
    assert "knowledge_ids" not in _knowledge_search_inputs_from_state({"user_prompt": "query"})

    state = make_empty_state(task_id="task_1", graph_run_id="run_1", graph_version="v3")
    assert state["project_id"] is None
    assert state["project_context"] is None
    state["project_context"] = project_context
    patch = await search_knowledge_node(state, ctx=SimpleNamespace(tool_adapter=None))
    fused = patch["knowledge_search_result"]
    bundle = fused["retrieval_evidence_bundle"]
    assert bundle["company_rag"]["status"] == "skipped"
    assert bundle["project_rag"] == {
        "status": "skipped",
        "reason": "no_project",
        "queries": [],
        "hits": [],
    }

    factory = RuntimeContextFactory(
        user_internal_id=1,
        task_internal_id=2,
        conversation_internal_id=3,
        session_factory=lambda: None,
        settings_service=object(),
        event_sink=object(),
        cancellation_service=object(),
        project_context=project_context,
    )
    assert factory.build().project_context == project_context

    coordinator = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
    initial = coordinator._initial_state(
        task_id="task_1",
        graph_run_id="run_1",
        payload={"project_id": "project_alpha", "project_context": project_context},
    )
    assert initial["project_id"] == "project_alpha"
    assert initial["project_context"] == project_context


@pytest.mark.asyncio
async def test_agent_task_freezes_project_context_into_task_and_outbox_payload(monkeypatch):
    from app.agent.enums import IntentType, MessageRoute
    from app.agent.intent_router import IntentResult

    service = MessageService.__new__(MessageService)
    service._task_repo = MagicMock()
    created_tasks = []

    async def create_task(task):
        task.id = 777
        created_tasks.append(task)
        return task

    service._task_repo.create = create_task
    service._task_repo.write_frozen_manifest = MagicMock(return_value=1)
    service._task_repo.update_context_json = AsyncMock()
    service._event_repo = SimpleNamespace(create=AsyncMock())
    service._exec_repo = SimpleNamespace(enqueue_new_task=AsyncMock(return_value=object()))
    service._context_svc = SimpleNamespace(
        build_task_trigger_context=AsyncMock(
            return_value=SimpleNamespace(model_dump=lambda mode=None: {})
        )
    )
    service._ensure_context_services = MagicMock()
    project_context = {
        "project_id": "project_alpha",
        "project_name": "Alpha",
        "knowledge_ids": ["kb_alpha"],
    }
    service._resolve_project_context = AsyncMock(return_value=project_context)
    monkeypatch.setenv("CONTEXT_ENGINE_AGENT_ENABLED", "1")
    monkeypatch.setenv("MIG_GENERATE", "1")
    conv = SimpleNamespace(
        id=11,
        public_id="conversation_alpha",
        project_id=22,
        title="Alpha task",
    )
    intent = IntentResult(
        intent=IntentType.TEST_PLAN_GENERATION,
        route=MessageRoute.AGENT_TASK,
        supported=True,
        confidence=1,
        reason="test",
    )
    await service._create_agent_task(
        conv=conv,
        user_internal_id=1,
        content="生成认证测试方案",
        now=NOW,
        user_msg_dict={"message_id": "msg_1"},
        intent_result=intent,
        user_msg_internal_id=10,
    )

    stored_json = service._task_repo.update_context_json.await_args.args[1]
    assert json.loads(stored_json)["project_context"] == project_context
    manifest = created_tasks[0].task_context_json["frozen_flags_manifest"]
    persisted_manifest = json.loads(stored_json)["frozen_flags_manifest"]
    assert persisted_manifest["flags_digest"] == manifest["flags_digest"]
    assert (
        service._task_repo.update_context_json.await_args.kwargs[
            "allow_missing_manifest_from_patch"
        ]
        is True
    )
    assert manifest["task_semantic_flags"]["CONTEXT_ENGINE_AGENT_ENABLED"] is True
    assert manifest["task_semantic_flags"]["MIG_GENERATE"] is True
    outbox = service._exec_repo.enqueue_new_task.await_args.kwargs["payload"]
    assert outbox["project_id"] == "project_alpha"
    assert outbox["project_context"] == project_context


@pytest.mark.asyncio
async def test_chat_memory_extraction_is_scheduled_and_does_not_block_reply():
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class FreshSession:
        async def get(self, _model, _identity):
            return None

    @asynccontextmanager
    async def session_factory():
        yield FreshSession()

    async def learn(_self, **_kwargs):
        started.set()
        await release.wait()
        finished.set()
        return {"persisted": 0}

    service = MessageService.__new__(MessageService)
    service._session_factory = session_factory
    service._context_llm_invoker = None
    service._llm = None
    conv = SimpleNamespace(id=12, public_id="conv_async", project_id=None)
    flags = SimpleNamespace(
        context_memory_auto_extract_enabled=True,
        context_memory_write_enabled=True,
        context_engine_enabled=False,
    )
    with patch(
        "app.context_engine.feature_flags.get_context_engine_flags",
        return_value=flags,
    ), patch(
        "app.services.context_learning_service.ContextLearningService.learn_from_user_message",
        new=learn,
    ):
        await service._maybe_context_learn(
            conversation_public_id=conv.public_id,
            conversation_internal_id=conv.id,
            project_internal_id=conv.project_id,
            user_message="remember this",
            user_internal_id=1,
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert finished.is_set() is False
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=1)


def test_project_context_keeps_v3_graph_wiring_explicit():
    from app.agent_runtime.graphs.test_plan.graph import compile_test_plan_graph

    graph = compile_test_plan_graph(version="v3", checkpointer=None).get_graph()
    expected_nodes = {
        "__start__", "__end__", "initialize_task", "validate_inputs",
        "parse_requirement", "parse_template", "search_knowledge",
        "prep_subgraph", "prep_legacy_fallback", "suggest_sections",
        "prepare_preparation_clarification", "preparation_clarification_interrupt",
        "prepare_section_confirmation", "section_confirmation_interrupt",
        "pause_for_legacy_confirm", "resume_task", "generate_test_plan",
        "review_step", "repair_subgraph_step", "repair_fallback_step",
        "regenerate_sections_step", "prepare_export", "export_word",
        "check_docx_format_step", "format_loss_interrupt",
        "pause_for_legacy_format_decision", "record_loss_decision",
        "tool_narrative_barrier", "task_summary_narrative", "finalize_task",
        "cancel_task", "fail_task",
    }
    expected_edges = {
        ("__start__", "fail_task", True),
        ("__start__", "initialize_task", True),
        ("__start__", "record_loss_decision", True),
        ("__start__", "resume_task", True),
        ("initialize_task", "validate_inputs", False),
        ("validate_inputs", "fail_task", True),
        ("validate_inputs", "parse_requirement", True),
        ("parse_requirement", "tool_narrative_barrier", False),
        ("parse_template", "tool_narrative_barrier", False),
        ("search_knowledge", "tool_narrative_barrier", False),
        ("prep_subgraph", "__end__", True),
        ("prep_subgraph", "prep_legacy_fallback", True),
        ("prep_subgraph", "prepare_preparation_clarification", True),
        ("prep_subgraph", "search_knowledge", True),
        ("prep_subgraph", "suggest_sections", True),
        ("prep_legacy_fallback", "suggest_sections", False),
        ("prepare_preparation_clarification", "preparation_clarification_interrupt", False),
        ("preparation_clarification_interrupt", "prep_subgraph", False),
        ("suggest_sections", "tool_narrative_barrier", False),
        ("prepare_section_confirmation", "fail_task", True),
        ("prepare_section_confirmation", "section_confirmation_interrupt", True),
        ("section_confirmation_interrupt", "generate_test_plan", False),
        ("resume_task", "generate_test_plan", False),
        ("generate_test_plan", "tool_narrative_barrier", False),
        ("review_step", "tool_narrative_barrier", False),
        ("repair_subgraph_step", "fail_task", True),
        ("repair_subgraph_step", "prepare_export", True),
        ("regenerate_sections_step", "review_step", False),
        ("prepare_export", "export_word", False),
        ("export_word", "tool_narrative_barrier", False),
        ("check_docx_format_step", "tool_narrative_barrier", False),
        ("format_loss_interrupt", "fail_task", True),
        ("format_loss_interrupt", "prepare_export", True),
        ("format_loss_interrupt", "task_summary_narrative", True),
        ("record_loss_decision", "__end__", False),
        ("task_summary_narrative", "finalize_task", False),
        ("finalize_task", "__end__", False),
        ("fail_task", "__end__", False),
        ("tool_narrative_barrier", "__end__", True),
        ("tool_narrative_barrier", "check_docx_format_step", True),
        ("tool_narrative_barrier", "fail_task", True),
        ("tool_narrative_barrier", "format_loss_interrupt", True),
        ("tool_narrative_barrier", "parse_template", True),
        ("tool_narrative_barrier", "prep_subgraph", True),
        ("tool_narrative_barrier", "prepare_export", True),
        ("tool_narrative_barrier", "prepare_preparation_clarification", True),
        ("tool_narrative_barrier", "prepare_section_confirmation", True),
        ("tool_narrative_barrier", "regenerate_sections_step", True),
        ("tool_narrative_barrier", "repair_subgraph_step", True),
        ("tool_narrative_barrier", "review_step", True),
        ("tool_narrative_barrier", "search_knowledge", True),
        ("tool_narrative_barrier", "suggest_sections", True),
        ("tool_narrative_barrier", "task_summary_narrative", True),
    }
    actual_edges = {
        (edge.source, edge.target, bool(edge.conditional)) for edge in graph.edges
    }
    assert set(graph.nodes) == expected_nodes
    assert actual_edges == expected_edges


def test_chat_prompt_puts_project_context_before_conversation_and_current_input_last():
    ctx = SimpleNamespace(
        project_context={
            "project_id": "project_alpha",
            "project_name": "Alpha",
            "instructions": [{"content": "项目规则"}],
            "memories": [{"content": "项目记忆"}],
            "source_hits": [{"title": "需求", "content": "项目资料"}],
            "recent_progress": [{"summary": "项目进度"}],
        },
        conversation_summary="会话摘要",
        recent_messages=[],
        file_summaries=[],
        latest_task_summary=None,
        knowledge_snippets=None,
    )
    prompt = ChatLLMService._build_context_content("本轮明确要求", ctx)
    assert prompt.index("【项目上下文：Alpha】") < prompt.index("【会话摘要】")
    assert prompt.index("项目规则") < prompt.index("项目记忆") < prompt.index("项目资料")
    assert prompt.endswith("【当前用户问题】\n本轮明确要求")
