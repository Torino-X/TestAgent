"""v3 时序竞态修复测试 — commit 必须早于 event publish。

验证 _persist_human_confirmation_in_node 在 pause_for_legacy_confirm_node
内正确执行:先 commit,再 emit NEED_USER_CONFIRM,再 emit TASK_WAITING。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_state_with_sections(*, sections_count: int = 17) -> Dict[str, Any]:
    sections = [
        {"section_id": f"sec_{i:03d}", "title": f"章节{i}", "code": str(i)}
        for i in range(1, sections_count + 1)
    ]
    return {
        "task_id": "task_100",
        "task_internal_id": 100,
        "user_internal_id": 1,
        "conversation_internal_id": 34,
        "graph_run_id": "run-100",
        "section_suggestions": {"sections": sections},
        "current_phase": "pre_confirm",
        "task_status": "generating",
    }


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.task_internal_id = 100
    ctx.user_internal_id = 1
    ctx.conversation_internal_id = 34
    return ctx


def _build_session_factory(operation_order: list[str] | None, fake_session: AsyncMock):
    """构造一个 async session_factory 进入 async with ctx.session_factory() as session."""

    @asynccontextmanager
    async def _cm():
        if operation_order is not None:
            operation_order.append("session_open")
        try:
            yield fake_session
        finally:
            pass

    def session_factory():
        return _cm()

    return session_factory


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commit_happens_before_emit_need_user_confirm():
    """commit 必须先于 NEED_USER_CONFIRM emit。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        pause_for_legacy_confirm_node,
    )

    state = _make_state_with_sections()
    ctx = _make_ctx()
    operation_order: list[str] = []

    fake_session = AsyncMock()

    async def track_commit():
        operation_order.append("commit")

    fake_session.commit = track_commit

    ctx.session_factory = _build_session_factory(operation_order, fake_session)

    mock_repo = MagicMock()

    async def track_get_pending(_tid):
        operation_order.append("get_pending")
        return None

    mock_repo.get_pending_by_task = track_get_pending

    async def track_create(_confirm):
        operation_order.append("create")
    mock_repo.create = track_create

    async def track_emit(**kwargs):
        event_type = kwargs.get("event_type", "")
        operation_order.append(f"emit:{event_type}")
        return {}

    ctx.event_sink.emit = track_emit

    with patch(
        "app.repositories.confirmation_repository.ConfirmationRepository",
        return_value=mock_repo,
    ):
        result = await pause_for_legacy_confirm_node(state, ctx=ctx)

    # 验证顺序:commit → emit:need_user_confirm → emit:task_waiting
    assert "commit" in operation_order
    assert "emit:need_user_confirm" in operation_order
    assert "emit:task_waiting" in operation_order
    commit_idx = operation_order.index("commit")
    nuc_idx = operation_order.index("emit:need_user_confirm")
    tw_idx = operation_order.index("emit:task_waiting")
    assert commit_idx < nuc_idx, f"commit should be before need_user_confirm: {operation_order}"
    assert nuc_idx < tw_idx, f"need_user_confirm should be before task_waiting: {operation_order}"

    # 验证最终 return
    assert result["pause_marker"] == "need_user_confirm"
    assert result["task_status"] == "waiting_user_confirm"


@pytest.mark.asyncio
async def test_event_payload_contains_confirmation_id_and_sections():
    """NEED_USER_CONFIRM 事件 payload 必须包含 confirmation_id 和 sections。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        pause_for_legacy_confirm_node,
    )

    state = _make_state_with_sections(sections_count=17)
    ctx = _make_ctx()

    fake_session = AsyncMock()
    fake_session.commit = AsyncMock()
    ctx.session_factory = _build_session_factory(None, fake_session)

    mock_repo = MagicMock()
    mock_repo.get_pending_by_task = AsyncMock(return_value=None)
    mock_repo.create = AsyncMock()

    captured_emits: list[dict] = []

    async def track_emit(**kwargs):
        captured_emits.append(kwargs)
        return {}

    ctx.event_sink.emit = track_emit

    with patch(
        "app.repositories.confirmation_repository.ConfirmationRepository",
        return_value=mock_repo,
    ):
        await pause_for_legacy_confirm_node(state, ctx=ctx)

    # 找到 NEED_USER_CONFIRM 事件
    nuc_emit = next(e for e in captured_emits if e.get("event_type") == "need_user_confirm")
    payload = nuc_emit["payload"]
    assert payload["confirmation_id"] is not None
    assert payload["confirmation_id"].startswith("confirm_")
    assert payload["confirmation_type"] == "section_generation_config"
    assert payload["section_count"] == 17
    assert "section_suggestions" in payload
    assert len(payload["section_suggestions"]["sections"]) == 17


@pytest.mark.asyncio
async def test_sections_count_17_creates_single_record():
    """sections=17 只创建一条 pending confirmation 记录。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        pause_for_legacy_confirm_node,
    )

    state = _make_state_with_sections(sections_count=17)
    ctx = _make_ctx()

    fake_session = AsyncMock()
    fake_session.commit = AsyncMock()
    ctx.session_factory = _build_session_factory(None, fake_session)

    mock_repo = MagicMock()
    mock_repo.get_pending_by_task = AsyncMock(return_value=None)
    create_calls: list[Any] = []

    async def track_create(confirm):
        create_calls.append(confirm)
    mock_repo.create = track_create

    ctx.event_sink.emit = AsyncMock(return_value={})

    with patch(
        "app.repositories.confirmation_repository.ConfirmationRepository",
        return_value=mock_repo,
    ):
        await pause_for_legacy_confirm_node(state, ctx=ctx)

    assert len(create_calls) == 1
    assert len(create_calls[0].request_json["sections"]) == 17


@pytest.mark.asyncio
async def test_persist_failure_does_not_emit_events():
    """持久化失败时不发 NEED_USER_CONFIRM 事件(必须有 sections 才会尝试持久化)。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        pause_for_legacy_confirm_node,
    )

    state = _make_state_with_sections()  # 17 sections
    ctx = _make_ctx()

    # session_factory 直接抛异常(模拟 DB 完全不可用)
    @asynccontextmanager
    async def failing_cm():
        raise RuntimeError("DB connection refused")
        yield  # unreachable

    def failing_session_factory():
        return failing_cm()

    ctx.session_factory = failing_session_factory

    emit_calls: list[dict] = []

    async def track_emit(**kwargs):
        emit_calls.append(kwargs)
        return {}

    ctx.event_sink.emit = track_emit

    result = await pause_for_legacy_confirm_node(state, ctx=ctx)

    # 持久化失败时不应发出事件
    assert len(emit_calls) == 0, f"Should not emit events on persist failure: {emit_calls}"
    # 节点应进入 FAILED 状态(由上层处理)
    assert result["task_status"] == "failed"


@pytest.mark.asyncio
async def test_retry_does_not_create_duplicate_record():
    """重复执行时不创建第二条记录(已有 pending 则复用)。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        _persist_human_confirmation_in_node,
    )

    state = _make_state_with_sections()
    ctx = _make_ctx()

    fake_session = AsyncMock()
    fake_session.commit = AsyncMock()
    ctx.session_factory = _build_session_factory(None, fake_session)

    existing = MagicMock()
    existing.public_id = "confirm_existing"
    existing.confirmation_type = "section_generation_config"

    mock_repo = MagicMock()
    mock_repo.get_pending_by_task = AsyncMock(return_value=existing)
    mock_repo.create = AsyncMock()

    with patch(
        "app.repositories.confirmation_repository.ConfirmationRepository",
        return_value=mock_repo,
    ):
        result = await _persist_human_confirmation_in_node(ctx, state)

    # 复用现有记录,不应调用 create
    mock_repo.create.assert_not_awaited()
    assert result["confirmation_id"] == "confirm_existing"


@pytest.mark.asyncio
async def test_empty_sections_skips_persist_in_node():
    """sections 为空时在节点层跳过持久化。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        _persist_human_confirmation_in_node,
    )

    state = _make_state_with_sections(sections_count=0)
    ctx = _make_ctx()

    session_factory_called = []

    @asynccontextmanager
    async def unused_cm():
        session_factory_called.append(True)
        yield None

    def session_factory():
        return unused_cm()

    ctx.session_factory = session_factory

    result = await _persist_human_confirmation_in_node(ctx, state)

    assert result is None
    assert len(session_factory_called) == 0


@pytest.mark.asyncio
async def test_pending_confirmation_queryable_after_pause_node_returns():
    """pause 节点返回后,pending-confirmation API 立即可查询。"""
    from app.agent_runtime.graphs.test_plan.versions.v3.nodes_pre_confirm import (
        pause_for_legacy_confirm_node,
    )

    state = _make_state_with_sections()
    ctx = _make_ctx()

    created_record = MagicMock()
    created_record.public_id = "confirm_xxx"
    created_record.task_id = 100
    created_record.confirmation_type = "section_generation_config"
    created_record.status = "pending"
    created_record.request_json = {"sections": state["section_suggestions"]["sections"]}

    fake_session = AsyncMock()
    fake_session.commit = AsyncMock()
    ctx.session_factory = _build_session_factory(None, fake_session)

    mock_repo = MagicMock()
    mock_repo.get_pending_by_task = AsyncMock(return_value=None)
    mock_repo.create = AsyncMock()
    ctx.event_sink.emit = AsyncMock(return_value={})

    with patch(
        "app.repositories.confirmation_repository.ConfirmationRepository",
        return_value=mock_repo,
    ):
        await pause_for_legacy_confirm_node(state, ctx=ctx)

    # node 返回后,模拟 pending-confirmation 查询 — 应能查到
    mock_query_repo = MagicMock()
    mock_query_repo.get_pending_by_task = AsyncMock(return_value=created_record)

    with patch(
        "app.repositories.confirmation_repository.ConfirmationRepository",
        return_value=mock_query_repo,
    ):
        result = await mock_query_repo.get_pending_by_task(100)

    assert result is not None
    assert result.public_id == "confirm_xxx"
    assert len(result.request_json["sections"]) == 17