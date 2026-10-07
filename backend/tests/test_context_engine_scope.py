"""CE-01 整改 / CE-05 收口：ContextScopeResolver 验证测试。

覆盖（CPS-04 Conversation Project Space 收口后）：
- Task Frozen Workspace 优先；
- Conversation Explicit Workspace；
- Conversation Derived Workspace（CPS-04 新 source，前身是 conversation_fallback）；
- 跨用户拒绝；
- Task/Conversation ownership 不一致拒绝；
- 运行中 Task 不随 Conversation 改绑；
- 旧 Task 兼容；
- 文件名不推断；
- 前端 workspace_key 伪造拒绝；
- thread_id = task_public_id 约束（单独保留）。
"""

from __future__ import annotations

import pytest

from app.context_engine.models import ContextRequest
from app.context_engine.scope import ContextScopeResolver, ScopeResolutionError


def test_thread_id_equals_task():
    resolver = ContextScopeResolver()
    scope = resolver.resolve(ContextRequest(user_id="usr_1", call_site="x", task_id="task_pub_1"))
    assert scope.thread_id == "task_pub_1"


def test_thread_id_mismatch_rejected():
    resolver = ContextScopeResolver()
    with pytest.raises(ScopeResolutionError):
        resolver.resolve(
            ContextRequest(user_id="usr_1", call_site="x", task_id="task_pub_1", thread_id="other")
        )


# ── workspace 解析优先级 ───────────────────────────────────────────


def test_task_frozen_workspace_takes_priority():
    resolver = ContextScopeResolver()
    res = resolver.resolve_workspace(
        user_id="usr_1",
        task_workspace="ws_task_frozen",
        conversation_workspace="ws_conv",
        conversation_public_id="conv_1",
    )
    assert res.workspace_key == "ws_task_frozen"
    assert res.source == "task_frozen"


def test_conversation_explicit_workspace():
    resolver = ContextScopeResolver()
    res = resolver.resolve_workspace(
        user_id="usr_1",
        conversation_workspace="ws_conv_explicit",
        conversation_public_id="conv_1",
    )
    assert res.workspace_key == "ws_conv_explicit"
    assert res.source == "conversation_explicit"


def test_conversation_derived_workspace():
    """CPS-04：当 Conversation 表 context_workspace_key 缺失时，
    resolver 由 conversation_public_id 派生 workspace_key=conversation:{public_id}，
    source 标记为 conversation_derived（替代旧 conversation_fallback）。"""
    resolver = ContextScopeResolver()
    res = resolver.resolve_workspace(
        user_id="usr_1",
        conversation_public_id="conv_1",
    )
    assert res.workspace_key == "conversation:conv_1"
    assert res.source == "conversation_derived"


def test_no_workspace_returns_none():
    resolver = ContextScopeResolver()
    res = resolver.resolve_workspace(user_id="usr_1")
    assert res.workspace_key is None
    assert res.source == "none"


def test_running_task_does_not_follow_conversation_rebind():
    """运行中 Task 冻结的 workspace 优先于会话改绑后的新 workspace。"""
    resolver = ContextScopeResolver()
    # Task 创建时冻结 ws_old；会话后来改绑 ws_new
    res = resolver.resolve_workspace(
        user_id="usr_1",
        task_workspace="ws_old",
        conversation_workspace="ws_new",
    )
    assert res.workspace_key == "ws_old"  # 不随会话改绑


def test_legacy_task_compatible():
    """旧 Task 无冻结 workspace → 回落会话 workspace。"""
    resolver = ContextScopeResolver()
    res = resolver.resolve_workspace(
        user_id="usr_1",
        task_workspace=None,
        conversation_workspace="ws_conv",
    )
    assert res.workspace_key == "ws_conv"
    assert res.source == "conversation_explicit"


# ── 所有权校验 ─────────────────────────────────────────────────────


def test_cross_user_denied():
    resolver = ContextScopeResolver()
    with pytest.raises(ScopeResolutionError) as exc_info:
        resolver.validate_ownership(user_id="usr_1", owner_user_id="usr_2")
    assert exc_info.value.code == "context.scope.cross_user_denied"


def test_task_conversation_ownership_mismatch_denied():
    resolver = ContextScopeResolver()
    with pytest.raises(ScopeResolutionError):
        resolver.validate_ownership(
            user_id="usr_1",
            owner_user_id="usr_1",
            workspace_key="ws_task_owned",
            owner_workspace_key="ws_task_other",
        )


def test_ownership_ok():
    resolver = ContextScopeResolver()
    # 不抛异常即通过
    resolver.validate_ownership(
        user_id="usr_1",
        owner_user_id="usr_1",
        workspace_key="ws_a",
        owner_workspace_key="ws_a",
    )


# ── 文件名不推断 / 前端伪造拒绝 ───────────────────────────────────


def test_workspace_not_inferred_from_filename():
    """resolver 不接收文件名；只接受显式 workspace_key。"""
    resolver = ContextScopeResolver()
    # 显式 workspace_key 为 conversation:{public_id}，与文件名无关
    res = resolver.resolve_workspace(
        user_id="usr_1",
        conversation_workspace="conversation:conv_1",
    )
    assert res.workspace_key == "conversation:conv_1"
    # 若前端传文件名作为 workspace，所有权校验会在 scope 层拦截
    with pytest.raises(ScopeResolutionError):
        resolver.validate_ownership(
            user_id="usr_1",
            owner_user_id="usr_1",
            workspace_key="requirement.docx",  # 伪造 workspace_key
            owner_workspace_key="ws_real",
        )


def test_frontend_workspace_key_forgery_rejected():
    """前端伪造 workspace_key 通过所有权校验拒绝。"""
    resolver = ContextScopeResolver()
    with pytest.raises(ScopeResolutionError) as exc_info:
        resolver.validate_ownership(
            user_id="usr_1",
            owner_user_id="usr_1",
            workspace_key="forged_workspace",
            owner_workspace_key="real_workspace",
        )
    assert exc_info.value.code == "context.scope.workspace_mismatch"


def test_scope_resolver_error_maps_to_context_error():
    resolver = ContextScopeResolver()
    err = resolver.to_error(
        ScopeResolutionError("cross-user denied", code="context.scope.cross_user_denied")
    )
    assert err.stage.value == "scope"
    assert err.code == "context.scope.cross_user_denied"


@pytest.mark.asyncio
async def test_task_scope_validator_backfills_null_workspace_for_owned_conversation(sqlite_session_factory):
    """A task with a conversation but NULL workspace is a recoverable legacy row."""
    from sqlalchemy import select

    from app.context_engine.scope.task_scope_validator import validate_task_conversation_scope
    from app.models.agent_task import AgentTask
    from app.models.conversation import Conversation
    from app.models.user import User
    from app.utils.datetime import utcnow

    now = utcnow()
    async with sqlite_session_factory() as session:
        session.add(User(id=501, public_id="usr_scope", username="scope", password_hash="x", created_at=now, updated_at=now))
        session.add(Conversation(id=601, public_id="conv_scope", user_id=501, title="scope", status="active", created_at=now, updated_at=now))
        session.add(AgentTask(
            id=701,
            public_id="task_scope_null",
            user_id=501,
            conversation_id=601,
            task_type="test_plan_generation",
            status="waiting_user_confirm",
            context_workspace_key=None,
            created_at=now,
            updated_at=now,
        ))
        await session.commit()

    async with sqlite_session_factory() as session:
        await validate_task_conversation_scope(
            session,
            task_id=701,
            task_public_id="task_scope_null",
            task_context_workspace_key=None,
            task_conversation_id=601,
            task_user_id=501,
            current_user_id=501,
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        task = (await session.execute(select(AgentTask).where(AgentTask.id == 701))).scalar_one()
        assert task.context_workspace_key == "conversation:conv_scope"
