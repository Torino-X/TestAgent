"""CE-02 整改七：用户 / Workspace 隔离测试。

覆盖（owner-scope + workspace 双条件）：
- ConversationSource 跨用户拒绝；
- SummarySource 跨用户拒绝；
- TaskStateSource owner 不一致拒绝；
- FileSource 跨用户拒绝；
- ArtifactSource 跨用户拒绝；
- WorkspaceInstruction user_id + workspace_key 双条件；
- 伪造 workspace_key 拒绝；
- Task/Conversation owner 不一致拒绝。

最终输出：Cross-user Leakage = 0 / Cross-workspace Leakage = 0。
"""

from __future__ import annotations

import pytest

from app.context_engine.models.context import (
    ContextRequest,
    ContextScope,
    SectionPlan,
)
from app.context_engine.models.enums import ContextKind
from app.context_engine.sources.artifact import ArtifactSourceAdapter
from app.context_engine.sources.conversation import ConversationSourceAdapter
from app.context_engine.sources.file_document import FileDocumentSourceAdapter
from app.context_engine.sources.summary import ConversationSummarySourceAdapter
from app.context_engine.sources.task_state import TaskStateSourceAdapter
from app.context_engine.sources.workspace_instruction import WorkspaceInstructionSourceAdapter


def _section(kind=ContextKind.CONVERSATION, **kw):
    return SectionPlan(kind=kind, required=False, budget_tokens=1000, **kw)


def _scope(user_id="usr_1", **kw):
    return ContextScope(user_id=user_id, **kw)


class _RT:
    """fake runtime_context：session_factory 提供 SQLite session + 内部 user id。

    ``user_internal_id`` 由请求的 user_id 派生（usr_2 → 2），供 owner-scope 查询。
    """

    def __init__(self, sf, user_internal_id=1):
        self._sf = sf
        self.user_internal_id = user_internal_id

    def session_factory(self):
        return self._sf()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _seed_message(sf, *, user_id=1, conversation_id=1, content="hello"):
    from datetime import datetime
    from app.models.message import Message

    now = datetime.now()
    async with sf() as session:
        session.add(
            Message(
                id=1,
                public_id=f"msg_{user_id}_{conversation_id}",
                user_id=user_id,
                conversation_id=conversation_id,
                role="user",
                message_type="user_text",
                content=content,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


async def _seed_summary(sf):
    from datetime import datetime
    from app.models.conversation_summary import ConversationSummary

    now = datetime.now()
    async with sf() as session:
        session.add(
            ConversationSummary(
                id=1, public_id="sum_1", user_id=1, conversation_id=10,
                summary_text="user1 secret summary",
                created_at=now, updated_at=now,
            )
        )
        await session.commit()


async def _seed_task(sf):
    from datetime import datetime
    from app.models.agent_task import AgentTask

    now = datetime.now()
    async with sf() as session:
        session.add(
            AgentTask(
                id=1, public_id="task_pub_1", user_id=1, conversation_id=1,
                task_type="test_plan", status="created", title="secret task",
                created_at=now, updated_at=now,
            )
        )
        await session.commit()


async def _seed_file(sf):
    from datetime import datetime
    from app.models.uploaded_file import UploadedFile

    now = datetime.now()
    async with sf() as session:
        session.add(
            UploadedFile(
                id=1, public_id="file_1", user_id=1, conversation_id=10,
                original_name="secret.docx", stored_name="s.docx",
                file_ext="docx", file_size=100, file_type="document",
                storage_type="local", storage_path="/x",
                created_at=now, updated_at=now,
            )
        )
        await session.commit()


async def _seed_artifact(sf):
    from datetime import datetime
    from app.models.artifact import Artifact

    now = datetime.now()
    async with sf() as session:
        session.add(
            Artifact(
                id=1, public_id="art_1", user_id=1, conversation_id=1, task_id=1,
                artifact_type="test_plan", file_name="secret_plan.docx",
                file_ext="docx", storage_type="local", storage_path="/x",
                status="available", version_no=1,
                created_at=now, updated_at=now,
            )
        )
        await session.commit()


async def _seed_workspace_instruction(sf, *, wid, user_id, ws, text):
    from datetime import datetime
    from app.models.context_engine import ContextWorkspaceInstruction
    from app.context_engine.models.value_objects import Digest

    now = datetime.now()
    async with sf() as session:
        session.add(
            ContextWorkspaceInstruction(
                id=1, public_id=wid, user_id=user_id, workspace_key=ws,
                instruction_key=f"rule_{wid}", category="general",
                title=f"rule {wid}", content=text,
                status="active", content_hash=str(Digest.of(text)),
                idempotency_key=f"idem_{wid}",
                created_by_user_id=user_id,
                created_at=now, updated_at=now,
            )
        )
        await session.commit()


# ── ConversationSource 跨用户 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_conversation_source_cross_user_rejected(sqlite_session_factory):
    """用户 A 请求用户 B 的 conversation → 查不到任何消息（无泄漏）。"""
    await _seed_message(sqlite_session_factory, user_id=1, conversation_id=10, content="user 1 message")
    adapter = ConversationSourceAdapter()
    rt = _RT(sqlite_session_factory, user_internal_id=2)
    request = ContextRequest(user_id="usr_2", conversation_id="10", call_site="x")
    scope = _scope(user_id="usr_2", conversation_id="10", thread_id="task_1")
    result = await adapter.collect(request, _section(), scope, runtime_context=rt)
    assert result.item_count == 0
    assert all("user 1 message" not in it.content for it in result.items)


@pytest.mark.asyncio
async def test_conversation_source_keeps_full_raw_history_until_summary_exists(sqlite_session_factory):
    """The chat source must not silently cap an uncompacted session at 20 turns."""
    from datetime import datetime
    from app.models.message import Message

    now = datetime.now()
    async with sqlite_session_factory() as session:
        for index in range(1, 49):
            session.add(
                Message(
                    id=index,
                    public_id=f"msg_full_{index}",
                    user_id=1,
                    conversation_id=77,
                    role="user" if index % 2 else "agent",
                    message_type="user_text" if index % 2 else "agent_text",
                    content=f"turn-content-{index}",
                    conversation_sequence=index,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.commit()

    adapter = ConversationSourceAdapter()
    rt = _RT(sqlite_session_factory)
    request = ContextRequest(user_id="usr_1", conversation_id="77", call_site="x")
    scope = _scope(user_id="usr_1", conversation_id="77", thread_id="task_1")
    result = await adapter.collect(
        request,
        SectionPlan(kind=ContextKind.CONVERSATION, required=False, budget_tokens=100_000),
        scope,
        runtime_context=rt,
    )

    assert result.item_count == 48
    assert result.items[0].content == "turn-content-1"
    assert result.items[-1].content == "turn-content-48"


# ── SummarySource 跨用户 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_source_cross_user_rejected(sqlite_session_factory):
    """Summary 跨用户拒绝：conversation_summaries 按 user_id 过滤。"""
    await _seed_summary(sqlite_session_factory)
    adapter = ConversationSummarySourceAdapter()
    rt = _RT(sqlite_session_factory, user_internal_id=2)
    request = ContextRequest(user_id="usr_2", conversation_id="10", call_site="x")
    scope = _scope(user_id="usr_2", conversation_id="10", thread_id="task_1")
    result = await adapter.collect(request, _section(), scope, runtime_context=rt)
    assert result.item_count == 0


# ── TaskStateSource owner 不一致 ───────────────────────────────────


@pytest.mark.asyncio
async def test_task_state_owner_mismatch_rejected(sqlite_session_factory):
    """TaskState owner 不一致：task 属于用户 1，用户 2 请求 → 拒绝。"""
    await _seed_task(sqlite_session_factory)
    adapter = TaskStateSourceAdapter()
    rt = _RT(sqlite_session_factory, user_internal_id=2)
    request = ContextRequest(user_id="usr_2", task_id="task_pub_1", call_site="x")
    scope = _scope(user_id="usr_2", task_id="task_pub_1", thread_id="task_pub_1")
    result = await adapter.collect(request, _section(ContextKind.TASK_STATE), scope, runtime_context=rt)
    assert result.item_count == 0
    assert result.failure_code == "context.source.task_not_found"


# ── FileSource 跨用户 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_file_source_cross_user_rejected(sqlite_session_factory):
    """FileSource 跨用户拒绝：uploaded_files 按 user_id 过滤。"""
    await _seed_file(sqlite_session_factory)
    adapter = FileDocumentSourceAdapter()
    rt = _RT(sqlite_session_factory, user_internal_id=2)
    request = ContextRequest(user_id="usr_2", conversation_id="10", call_site="x")
    scope = _scope(user_id="usr_2", conversation_id="10", thread_id="task_1")
    result = await adapter.collect(request, _section(ContextKind.EVIDENCE), scope, runtime_context=rt)
    assert result.item_count == 0
    assert all("secret.docx" not in it.content for it in result.items)


# ── ArtifactSource 跨用户 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_artifact_source_cross_user_rejected(sqlite_session_factory):
    """ArtifactSource 跨用户拒绝：artifacts 按 user_id 过滤。"""
    await _seed_artifact(sqlite_session_factory)
    adapter = ArtifactSourceAdapter()
    rt = _RT(sqlite_session_factory, user_internal_id=2)
    request = ContextRequest(user_id="usr_2", task_id="task_pub_1", call_site="x")
    scope = _scope(user_id="usr_2", task_id="task_pub_1", thread_id="task_pub_1")
    result = await adapter.collect(request, _section(ContextKind.EVIDENCE), scope, runtime_context=rt)
    assert result.item_count == 0
    assert all("secret_plan.docx" not in it.content for it in result.items)


# ── WorkspaceInstruction user_id + workspace_key 双条件 ────────────


@pytest.mark.asyncio
async def test_workspace_instruction_user_and_workspace_dual_condition(sqlite_session_factory):
    """WorkspaceInstruction 必须 user_id + workspace_key 同时匹配。"""
    await _seed_workspace_instruction(sqlite_session_factory, wid="wi_1", user_id=1, ws="ws_secret", text="secret instruction")
    adapter = WorkspaceInstructionSourceAdapter()
    rt = _RT(sqlite_session_factory, user_internal_id=2)
    # 用户 2 + 同一 workspace → 不应拿到用户 1 的指令
    request = ContextRequest(user_id="usr_2", workspace_key="ws_secret", call_site="x")
    scope = _scope(user_id="usr_2", workspace_key="ws_secret", thread_id="task_1")
    result = await adapter.collect(request, _section(ContextKind.PROJECT_INSTRUCTIONS), scope, runtime_context=rt)
    assert result.item_count == 0


@pytest.mark.asyncio
async def test_workspace_instruction_forged_workspace_key_rejected(sqlite_session_factory):
    """伪造 workspace_key：用户 1 的指令不会被其他 workspace 拿到。"""
    await _seed_workspace_instruction(sqlite_session_factory, wid="wi_2", user_id=1, ws="ws_real", text="real instruction")
    adapter = WorkspaceInstructionSourceAdapter()
    rt = _RT(sqlite_session_factory)
    request = ContextRequest(user_id="usr_1", workspace_key="forged_ws", call_site="x")
    scope = _scope(user_id="usr_1", workspace_key="forged_ws", thread_id="task_1")
    result = await adapter.collect(request, _section(ContextKind.PROJECT_INSTRUCTIONS), scope, runtime_context=rt)
    assert result.item_count == 0


# ── Task/Conversation owner 不一致 ────────────────────────────────


def test_task_conversation_owner_mismatch_scope_rejected():
    """Task/Conversation owner 不一致 → scope 校验拒绝。"""
    from app.context_engine.scope.resolver import ContextScopeResolver, ScopeResolutionError

    resolver = ContextScopeResolver()
    with pytest.raises(ScopeResolutionError) as exc_info:
        resolver.validate_ownership(
            user_id="usr_1", owner_user_id="usr_2",
            workspace_key="ws_task", owner_workspace_key="ws_task",
        )
    assert exc_info.value.code == "context.scope.cross_user_denied"


def test_conversation_owner_mismatch_scope_rejected():
    from app.context_engine.scope.resolver import ContextScopeResolver, ScopeResolutionError

    resolver = ContextScopeResolver()
    with pytest.raises(ScopeResolutionError) as exc_info:
        resolver.validate_ownership(
            user_id="usr_1", owner_user_id="usr_1",
            workspace_key="ws_a", owner_workspace_key="ws_b",
        )
    assert exc_info.value.code == "context.scope.workspace_mismatch"


@pytest.mark.asyncio
async def test_artifact_source_public_task_id_resolves_internal_task(sqlite_session_factory):
    await _seed_task(sqlite_session_factory)
    await _seed_artifact(sqlite_session_factory)
    adapter = ArtifactSourceAdapter()
    rt = _RT(sqlite_session_factory, user_internal_id=1)
    request = ContextRequest(user_id="usr_1", task_id="task_pub_1", call_site="x")
    scope = _scope(user_id="usr_1", task_id="task_pub_1", thread_id="task_pub_1")

    result = await adapter.collect(request, _section(ContextKind.EVIDENCE), scope, runtime_context=rt)

    assert result.failure_code is None
    assert result.degraded is False
    assert result.item_count == 1
    assert result.items[0].source_ref == "art_1"
