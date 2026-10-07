"""WP-BE-08 Supersede Closure：Project Rule Supersede Lifecycle 测试。

覆盖：
TEST-01 same rule duplicate（v3 → v3）→ 只有一条 active rule（DEDUPE）
TEST-02 explicit supersede（v3 → v4）→ old superseded + new active
TEST-03 supersede link（new.supersedes_instruction_id = old.id）
TEST-04 unrelated rules（API version + no IE）→ both active（不误 supersede）
TEST-05 conversation isolation（Conversation A supersede 不影响 B）
TEST-06 Context Assembly（supersede 后 Context 只含 v4，不含 v3）
TEST-07 transaction rollback（新规则创建失败 → 不留半完成 supersede）
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.models.context_engine import ContextWorkspaceInstruction
from app.repositories.base import ensure_model_id
from app.services.context_learning_service import (
    ContextLearningService,
    deterministic_rule_key,
)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class _FakeInvoker:
    """可注入的 bridge 假实现（返回分类结果）。"""

    def __init__(self, items: list[dict]):
        self._items = items
        self.available = True

    async def generate(self, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(value={"items": self._items})


async def _list_active(session, conv: str) -> list[ContextWorkspaceInstruction]:
    result = await session.execute(
        select(ContextWorkspaceInstruction).where(
            ContextWorkspaceInstruction.workspace_key == f"conversation:{conv}",
            ContextWorkspaceInstruction.status == "active",
            ContextWorkspaceInstruction.deleted_at.is_(None),
        )
    )
    return list(result.scalars().all())


async def _learn(session, *, user_message: str, conv: str, key: str, content: str):
    """经 learning service 持久化一条 PROJECT_RULE。"""
    learning = ContextLearningService(
        session,
        llm_invoker=_FakeInvoker(
            items=[{"category": "PROJECT_RULE", "key": key, "content": content, "reason": "x"}]
        ),
    )
    result = await learning.learn_from_user_message(
        user_message=user_message,
        user_internal_id=1,
        conversation_public_id=conv,
    )
    return result


@pytest.mark.asyncio
async def test_same_rule_duplicate_dedupe(sqlite_session_factory):
    """TEST-01：v3 → v3 重复 → 只有一条 active rule（DEDUPE 非 SUPERSEDE）。"""
    async with sqlite_session_factory() as session:
        await _learn(session, user_message="接口统一使用 v3", conv="conv_t1", key="api_version", content="接口统一使用 v3")
        await _learn(session, user_message="接口还是使用 v3", conv="conv_t1", key="api_version", content="接口统一使用 v3")
        await session.commit()

    async with sqlite_session_factory() as session:
        active = await _list_active(session, "conv_t1")
        assert len(active) == 1
        assert active[0].status == "active"
        all_rules = (
            await session.execute(select(ContextWorkspaceInstruction).where(
                ContextWorkspaceInstruction.workspace_key == "conversation:conv_t1"
            ))
        ).scalars().all()
        # 重复 → 不新增行（幂等）
        assert len(all_rules) == 1


@pytest.mark.asyncio
async def test_explicit_supersede_v3_to_v4(sqlite_session_factory):
    """TEST-02：v3 → v4 → old superseded + new active。"""
    async with sqlite_session_factory() as session:
        await _learn(session, user_message="接口统一使用 v3", conv="conv_t2", key="api_version", content="接口统一使用 v3")
        await _learn(session, user_message="v3 作废以 v4 为准", conv="conv_t2", key="api_version", content="接口统一使用 v4")
        await session.commit()

    async with sqlite_session_factory() as session:
        active = await _list_active(session, "conv_t2")
        assert len(active) == 1
        assert active[0].content == "接口统一使用 v4"
        all_rules = (
            await session.execute(select(ContextWorkspaceInstruction).where(
                ContextWorkspaceInstruction.workspace_key == "conversation:conv_t2"
            ))
        ).scalars().all()
        statuses = sorted(r.status for r in all_rules)
        assert statuses == ["active", "superseded"]
        # 旧 v3 是 superseded
        old = next(r for r in all_rules if r.status == "superseded")
        assert old.content == "接口统一使用 v3"


@pytest.mark.asyncio
async def test_supersede_link(sqlite_session_factory):
    """TEST-03：new.supersedes_instruction_id = old.id。"""
    async with sqlite_session_factory() as session:
        await _learn(session, user_message="接口统一使用 v3", conv="conv_t3", key="api_version", content="接口统一使用 v3")
        await _learn(session, user_message="v4 为准", conv="conv_t3", key="api_version", content="接口统一使用 v4")
        await session.commit()

    async with sqlite_session_factory() as session:
        all_rules = (
            await session.execute(select(ContextWorkspaceInstruction).where(
                ContextWorkspaceInstruction.workspace_key == "conversation:conv_t3"
            ))
        ).scalars().all()
        old = next(r for r in all_rules if r.status == "superseded")
        new = next(r for r in all_rules if r.status == "active")
        assert new.supersedes_instruction_id == old.id
        assert new.version == old.version + 1


@pytest.mark.asyncio
async def test_unrelated_rules_both_active(sqlite_session_factory):
    """TEST-04：API version + no IE → both active（不同 identity 不误 supersede）。"""
    async with sqlite_session_factory() as session:
        await _learn(session, user_message="接口统一使用 v3", conv="conv_t4", key="api_version", content="接口统一使用 v3")
        await _learn(session, user_message="本项目不测试 IE", conv="conv_t4", key="browser_ie", content="本项目不测试 IE")
        await session.commit()

    async with sqlite_session_factory() as session:
        active = await _list_active(session, "conv_t4")
        assert len(active) == 2
        keys = sorted(r.instruction_key for r in active)
        assert keys == ["api_version", "browser_ie"]
        assert all(r.status == "active" for r in active)


@pytest.mark.asyncio
async def test_conversation_isolation(sqlite_session_factory):
    """TEST-05：Conversation A supersede 不影响 B。"""
    async with sqlite_session_factory() as session:
        # A：v3 → v4（supersede）
        await _learn(session, user_message="接口统一使用 v3", conv="conv_a", key="api_version", content="接口统一使用 v3")
        await _learn(session, user_message="v4 为准", conv="conv_a", key="api_version", content="接口统一使用 v4")
        # B：仍是 v3
        await _learn(session, user_message="接口统一使用 v3", conv="conv_b", key="api_version", content="接口统一使用 v3")
        await session.commit()

    async with sqlite_session_factory() as session:
        active_a = await _list_active(session, "conv_a")
        active_b = await _list_active(session, "conv_b")
        # A 只有 v4
        assert len(active_a) == 1
        assert active_a[0].content == "接口统一使用 v4"
        # B 不受影响：仍是 v3 active
        assert len(active_b) == 1
        assert active_b[0].content == "接口统一使用 v3"
        assert active_b[0].status == "active"


@pytest.mark.asyncio
async def test_context_assembly_excludes_superseded(sqlite_session_factory):
    """TEST-06：supersede 后 Context 只含 v4，不含 v3。"""
    async with sqlite_session_factory() as session:
        await _learn(session, user_message="接口统一使用 v3", conv="conv_t6", key="api_version", content="接口统一使用 v3")
        await _learn(session, user_message="v4 为准", conv="conv_t6", key="api_version", content="接口统一使用 v4")
        await session.commit()

    async with sqlite_session_factory() as session:
        # 用 adapter 的过滤语义（list_effective_for_workspace status=active）
        from app.repositories.context_engine_repositories import WorkspaceInstructionRepository

        repo = WorkspaceInstructionRepository(session)
        effective = await repo.list_effective_for_workspace(
            1, "conversation:conv_t6", now=_utcnow(), status="active"
        )
        assert len(effective) == 1
        assert effective[0].content == "接口统一使用 v4"
        # superseded v3 不在 effective 列表
        assert all("v3" not in (r.content or "") for r in effective)


@pytest.mark.asyncio
async def test_transaction_rollback_supersede(sqlite_session_factory):
    """TEST-07：新规则创建失败 → 旧规则不被错误 supersede（不留半完成状态）。

    模拟：新规则 ensure_model_id/flush 失败（如 content 非法）→ 抛错；
    验证旧规则仍是 active（单事务回滚）。
    """
    import asyncio

    # 正常建 v3
    async with sqlite_session_factory() as session:
        await _learn(session, user_message="接口统一使用 v3", conv="conv_t7", key="api_version", content="接口统一使用 v3")
        await session.commit()

    # 模拟 supersede 过程中新规则创建抛错：注入会失败的 repo
    from unittest.mock import patch

    async with sqlite_session_factory() as session:
        # 先构造一条会让 supersede 走到旧规则标记，但新规则创建失败的环境。
        # 这里直接验证：若 _persist_project_rule 抛异常，调用方 rollback 后旧规则仍 active。
        learning = ContextLearningService(
            session,
            llm_invoker=_FakeInvoker(
                items=[{"category": "PROJECT_RULE", "key": "api_version", "content": "接口统一使用 v4", "reason": "x"}]
            ),
        )
        # 让 ensure_model_id 失败：monkeypatch _session.add 抛错
        original_add = session.add

        def _fail_add(obj):
            raise RuntimeError("simulated insert failure")

        session.add = _fail_add  # type: ignore[method-assign]
        try:
            result = await learning.learn_from_user_message(
                user_message="v4 为准", user_internal_id=1, conversation_public_id="conv_t7"
            )
        finally:
            session.add = original_add  # type: ignore[method-assign]

    async with sqlite_session_factory() as session:
        # 旧 v3 仍是 active（未半 supersede）
        active = await _list_active(session, "conv_t7")
        assert len(active) == 1
        assert active[0].content == "接口统一使用 v3"
        assert active[0].status == "active"


def test_deterministic_rule_key():
    """deterministic key：明确主题 → stable 键；不匹配 → 空（不误 supersede）。"""
    assert deterministic_rule_key("接口统一使用 v3") == "api"
    assert deterministic_rule_key("接口统一使用 v4") == "api"
    assert deterministic_rule_key("本项目不测试 IE") == "browser_ie"
    # 无明确主题 → 空串（调用方回落 content-hash 键，保守不 supersede）
    assert deterministic_rule_key("今天天气不错") == ""
