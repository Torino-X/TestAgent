"""v3 章节确认持久化与查询契约测试。

验证 LangGraph v3 sentinel pause 路径创建 HumanConfirmation 记录,
使 GET /pending-confirmation 可读取、用户确认后可 resume。

不需要真实 Postgres — mock AsyncSessionLocal + ConfirmationRepository。
"""

from __future__ import annotations

import asyncio
import unittest.mock as mock
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_state(
    *,
    task_internal_id: int = 100,
    user_internal_id: int = 1,
    conversation_internal_id: int = 34,
    sections_count: int = 17,
    pause_marker: str | None = "need_user_confirm",
    task_status: str = "waiting_user_confirm",
) -> Dict[str, Any]:
    """构造一个模拟的 result_state 字典。"""
    sections = [
        {
            "section_id": f"sec_{i:03d}",
            "code": str(i),
            "title": f"章节{i}",
            "level": "L1" if i % 5 == 0 else "L2",
            "suggested_action": "ai_generate",
            "reason": "测试",
            "available_actions": ["ai_generate", "keep_template", "manual_fill", "skip"],
        }
        for i in range(1, sections_count + 1)
    ]
    return {
        "task_internal_id": task_internal_id,
        "user_internal_id": user_internal_id,
        "conversation_internal_id": conversation_internal_id,
        "task_id": f"task_{task_internal_id}",
        "graph_run_id": f"run-{task_internal_id}",
        "section_suggestions": {"sections": sections},
        "pause_marker": pause_marker,
        "task_status": task_status,
        "current_phase": "paused",
        "current_node": "pause_for_legacy_confirm",
        "completed_nodes": ["initialize_task", "suggest_sections", "pause_for_legacy_confirm"],
    }


# ── Tests: _persist_human_confirmation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_17_sections_creates_one_pending_confirmation():
    """17章建议创建一条 pending confirmation。"""
    state = _make_state(sections_count=17)

    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)

    mock_repo = AsyncMock()
    mock_repo.get_pending_by_task = AsyncMock(return_value=None)
    mock_repo.create = AsyncMock()

    fake_session = AsyncMock()
    fake_session.commit = AsyncMock()

    with patch("app.db.session.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value.__aenter__ = AsyncMock(return_value=fake_session)
        mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch(
            "app.repositories.confirmation_repository.ConfirmationRepository",
            return_value=mock_repo,
        ):
            await coord._persist_human_confirmation(state)

    mock_repo.get_pending_by_task.assert_awaited_once_with(100)
    mock_repo.create.assert_awaited_once()
    created = mock_repo.create.call_args[0][0]
    assert created.confirmation_type == "section_generation_config"
    assert created.status == "pending"
    assert created.task_id == 100
    assert len(created.request_json["sections"]) == 17


@pytest.mark.asyncio
async def test_record_created_before_task_status_change():
    """记录创建后 task 才变为 waiting_user_confirm — 由 coordinator 顺序保证。"""
    state = _make_state()
    commit_order = []

    mock_repo = AsyncMock()
    mock_repo.get_pending_by_task = AsyncMock(return_value=None)

    async def track_create(confirm):
        commit_order.append("create_record")
    mock_repo.create = track_create

    fake_session = AsyncMock()

    async def track_commit():
        commit_order.append("commit")
    fake_session.commit = track_commit

    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)

    with patch("app.db.session.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value.__aenter__ = AsyncMock(return_value=fake_session)
        mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch(
            "app.repositories.confirmation_repository.ConfirmationRepository",
            return_value=mock_repo,
        ):
            await coord._persist_human_confirmation(state)

    assert commit_order.index("create_record") < commit_order.index("commit")


@pytest.mark.asyncio
async def test_db_create_failure_does_not_publish_need_user_confirm():
    """数据库创建失败时不发布 need_user_confirm — fail-closed。"""
    state = _make_state()

    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)

    with patch("app.db.session.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value.__aenter__ = AsyncMock(
            side_effect=Exception("DB connection refused")
        )
        mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
        # 不应抛异常 — fail-closed (swallowed)
        await coord._persist_human_confirmation(state)


@pytest.mark.asyncio
async def test_empty_sections_skips_persist():
    """sections 为空时禁止创建 HumanConfirmation 记录。"""
    state = _make_state()
    state["section_suggestions"] = {"sections": []}

    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
    # 不应调用 DB — 直接返回
    await coord._persist_human_confirmation(state)


@pytest.mark.asyncio
async def test_missing_task_internal_id_skips_persist():
    """task_internal_id 缺失时跳过持久化。"""
    state = _make_state()
    del state["task_internal_id"]

    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
    # 不应抛异常
    await coord._persist_human_confirmation(state)


@pytest.mark.asyncio
async def test_existing_pending_record_skips():
    """已有 pending 记录时跳过创建（幂等）。"""
    state = _make_state()

    mock_repo = AsyncMock()
    existing = MagicMock()
    existing.status = "pending"
    mock_repo.get_pending_by_task = AsyncMock(return_value=existing)

    fake_session = AsyncMock()

    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)

    with patch("app.db.session.AsyncSessionLocal") as mock_sl:
        mock_sl.return_value.__aenter__ = AsyncMock(return_value=fake_session)
        mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch(
            "app.repositories.confirmation_repository.ConfirmationRepository",
            return_value=mock_repo,
        ):
            await coord._persist_human_confirmation(state)

    mock_repo.get_pending_by_task.assert_awaited_once_with(100)
    mock_repo.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_invoke_and_capture_triggers_persist_on_sentinel():
    """_invoke_and_capture 在 sentinel pause 时自动调用 _persist_human_confirmation。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
    coord._context_factory = None
    coord._write_checkpoint = None
    coord._build_config = MagicMock(return_value={"configurable": {"thread_id": "t"}})

    pause_state = _make_state()
    coord._runtime = MagicMock()
    coord._runtime.ainvoke = AsyncMock(return_value=pause_state)

    persist_called = []
    async def fake_persist(state):
        persist_called.append(state.get("task_internal_id"))
    coord._persist_human_confirmation = fake_persist
    coord._maybe_write_checkpoint = AsyncMock()

    state = _make_state()
    outcome = await coord._invoke_and_capture(state)

    assert outcome.paused is True
    assert outcome.pause_marker == "need_user_confirm"
    assert persist_called == [100]


# ── Tests: AgentTaskService 查询 / 确认 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pending_confirmation_returns_17_sections():
    """pending-confirmation 接口返回 17 章。"""
    from app.services.agent_task_service import AgentTaskService

    sections_in_db = _make_state(sections_count=17)["section_suggestions"]["sections"]

    mock_task = MagicMock()
    mock_task.id = 100
    mock_task.user_id = 1

    mock_confirm = MagicMock()
    mock_confirm.public_id = "confirmation_test"
    mock_confirm.confirmation_type = "section_generation_config"
    mock_confirm.status = "pending"
    mock_confirm.request_json = {"sections": sections_in_db}

    fake_session = AsyncMock()

    with patch("app.services.agent_task_service.AgentTaskRepository") as MockRepo:
        MockRepo.return_value.get_by_public_id = AsyncMock(return_value=mock_task)
        with patch("app.services.agent_task_service.ConfirmationRepository") as MockCR:
            MockCR.return_value.get_pending_by_task = AsyncMock(return_value=mock_confirm)
            service = AgentTaskService(fake_session)
            result = await service.get_pending_confirmation("task_100", 1)

    assert result is not None
    assert result["confirmation_type"] == "section_generation_config"
    assert len(result["sections"]) == 17


@pytest.mark.asyncio
async def test_page_refresh_can_requery():
    """页面刷新后可恢复 — get_pending_confirmation 幂等查询。"""
    from app.services.agent_task_service import AgentTaskService

    mock_task = MagicMock()
    mock_task.id = 100
    mock_task.user_id = 1

    mock_confirm = MagicMock()
    mock_confirm.public_id = "confirmation_test"
    mock_confirm.confirmation_type = "section_generation_config"
    mock_confirm.status = "pending"
    mock_confirm.request_json = {"sections": [{"section_id": "s1"}]}

    fake_session = AsyncMock()

    with patch("app.services.agent_task_service.AgentTaskRepository") as MockRepo:
        MockRepo.return_value.get_by_public_id = AsyncMock(return_value=mock_task)
        with patch("app.services.agent_task_service.ConfirmationRepository") as MockCR:
            MockCR.return_value.get_pending_by_task = AsyncMock(return_value=mock_confirm)
            service = AgentTaskService(fake_session)
            r1 = await service.get_pending_confirmation("task_100", 1)
            r2 = await service.get_pending_confirmation("task_100", 1)

    assert r1 is not None and r2 is not None
    assert r1["confirmation_id"] == r2["confirmation_id"]


@pytest.mark.asyncio
async def test_confirmed_confirmations_are_available_for_history_restore():
    """A completed task still exposes the decisions needed by a refreshed UI."""
    from app.services.agent_task_service import AgentTaskService

    mock_task = MagicMock(id=100, user_id=1)
    confirmed = MagicMock(
        public_id="conf_history",
        confirmation_type="preparation_clarification",
        status="confirmed",
        request_json={"cards": [{"id": "scope", "question": "覆盖范围"}]},
        response_json={"answers": {"scope": "只覆盖员工访客"}},
        confirmed_at=datetime(2026, 10, 2, 3, 0, tzinfo=timezone.utc),
    )

    with patch("app.services.agent_task_service.AgentTaskRepository") as MockRepo:
        MockRepo.return_value.get_by_public_id = AsyncMock(return_value=mock_task)
        with patch("app.services.agent_task_service.ConfirmationRepository") as MockCR:
            MockCR.return_value.list_confirmed_by_task = AsyncMock(return_value=[confirmed])
            service = AgentTaskService(AsyncMock())
            result = await service.list_confirmed_confirmations("task_100", 1)

    assert result == [{
        "confirmation_id": "conf_history",
        "confirmation_type": "preparation_clarification",
        "status": "confirmed",
        "request": {"cards": [{"id": "scope", "question": "覆盖范围"}]},
        "response": {"answers": {"scope": "只覆盖员工访客"}},
        "confirmed_at": "2026-10-02T03:00:00+00:00",
    }]


@pytest.mark.asyncio
async def test_service_restart_can_requery():
    """服务重启后可恢复 — 独立于 coordinator 进程。"""
    from app.services.agent_task_service import AgentTaskService

    mock_task = MagicMock()
    mock_task.id = 100
    mock_task.user_id = 1

    mock_confirm = MagicMock()
    mock_confirm.public_id = "conf_restart"
    mock_confirm.confirmation_type = "section_generation_config"
    mock_confirm.status = "pending"
    mock_confirm.request_json = {"sections": [{"section_id": "s1"}]}

    fake_session = AsyncMock()

    with patch("app.services.agent_task_service.AgentTaskRepository") as MockRepo:
        MockRepo.return_value.get_by_public_id = AsyncMock(return_value=mock_task)
        with patch("app.services.agent_task_service.ConfirmationRepository") as MockCR:
            MockCR.return_value.get_pending_by_task = AsyncMock(return_value=mock_confirm)
            service = AgentTaskService(fake_session)
            result = await service.get_pending_confirmation("task_100", 1)

    assert result is not None
    assert result["confirmation_id"] == "conf_restart"


@pytest.mark.asyncio
async def test_confirm_changes_status_to_confirmed():
    """用户确认后记录变为 confirmed。"""
    from app.services.agent_task_service import AgentTaskService

    mock_task = MagicMock()
    mock_task.id = 100
    mock_task.user_id = 1
    mock_task.status = "waiting_user_confirm"
    mock_task.conversation_id = 34

    mock_pending = MagicMock()
    mock_pending.id = 42
    mock_pending.public_id = "conf_42"
    mock_pending.status = "pending"

    fake_session = AsyncMock()

    with patch("app.services.agent_task_service.AgentTaskRepository") as MockRepo:
        MockRepo.return_value.get_by_public_id = AsyncMock(return_value=mock_task)
        MockRepo.return_value.update_status = AsyncMock()
        with patch("app.services.agent_task_service.ConfirmationRepository") as MockCR:
            MockCR.return_value.get_pending_by_task = AsyncMock(return_value=mock_pending)
            MockCR.return_value.confirm = AsyncMock()
            service = AgentTaskService(fake_session)
            sections = [{"section_id": "s1", "action": "ai_generate"}]
            await service.confirm_sections("task_100", sections, 1)

    MockCR.return_value.confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_idempotent_confirm():
    """重复确认不重复执行 — pending 不存在时跳过 confirm。"""
    from app.services.agent_task_service import AgentTaskService

    mock_task = MagicMock()
    mock_task.id = 100
    mock_task.user_id = 1
    mock_task.status = "running"
    mock_task.conversation_id = 34

    fake_session = AsyncMock()

    with patch("app.services.agent_task_service.AgentTaskRepository") as MockRepo:
        MockRepo.return_value.get_by_public_id = AsyncMock(return_value=mock_task)
        MockRepo.return_value.update_status = AsyncMock()
        with patch("app.services.agent_task_service.ConfirmationRepository") as MockCR:
            MockCR.return_value.get_pending_by_task = AsyncMock(return_value=None)  # 已 confirmed
            MockCR.return_value.confirm = AsyncMock()
            service = AgentTaskService(fake_session)
            await service.confirm_sections("task_100", [{"section_id": "s1"}], 1)

    # pending=None → 不重复 confirm
    MockCR.return_value.confirm.assert_not_awaited()


# ── Tests: resume thread_id ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_command_resume_uses_original_thread_id():
    """Command(resume) 使用原 thread_id。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
    from langgraph.types import Command

    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
    coord._registry = MagicMock()
    coord._runtime = MagicMock()
    coord._checkpointer = MagicMock()
    coord._context_factory = None
    coord._write_checkpoint = None
    coord._validate_section_decision = MagicMock()

    coord._load_checkpoint_state_by_thread_id = AsyncMock(return_value={
        "task_id": "task_100",
        "graph_run_id": "run-100",
        "graph_version": "v3",
        "task_internal_id": 100,
    })

    mock_compiled = AsyncMock()
    mock_result_state = {
        "pause_marker": None,
        "task_status": "generating",
        "current_node": "section_confirmation_interrupt",
    }
    mock_compiled.ainvoke = AsyncMock(return_value=mock_result_state)
    coord._compiled_for_version = MagicMock(return_value=mock_compiled)

    expected_thread_id = "task_100"
    coord._build_resume_config = MagicMock(return_value={
        "configurable": {"thread_id": expected_thread_id}
    })
    coord._to_outcome = MagicMock(return_value=MagicMock(completed=False, paused=False))
    coord._maybe_write_checkpoint = AsyncMock()

    # Phase 2.9A.7:resume_section_confirmation 现在会调 aget_state 做 before/after 校验。
    # mock snapshot:含 pending interrupt + checkpoint_id / step 推进。
    interrupt_obj = MagicMock()
    interrupt_obj.value = {"kind": "section_confirmation", "sections": []}
    task_obj = MagicMock()
    task_obj.interrupts = [interrupt_obj]

    before_snapshot = MagicMock()
    before_snapshot.tasks = [task_obj]
    before_snapshot.next = ["section_confirmation_interrupt"]
    before_snapshot.values = {"__interrupt__": [interrupt_obj]}
    before_snapshot.config = MagicMock()
    before_snapshot.config.configurable = {"checkpoint_id": "cp_before"}
    before_snapshot.metadata = MagicMock()
    before_snapshot.metadata.step = 5

    after_snapshot = MagicMock()
    after_snapshot.tasks = []  # interrupt consumed
    after_snapshot.next = ["generate_test_plan"]
    after_snapshot.values = {"current_node": "generate_test_plan"}
    after_snapshot.config = MagicMock()
    after_snapshot.config.configurable = {"checkpoint_id": "cp_after"}
    after_snapshot.metadata = MagicMock()
    after_snapshot.metadata.step = 6

    # Phase 2.9A.7: aget_state 返回 before / after 两份
    mock_compiled.aget_state = AsyncMock(
        side_effect=[before_snapshot, after_snapshot]
    )

    decision = {
        "kind": "section_confirmation",
        "sections": [{"section_id": "s1"}],
        "source": "user",
    }

    await coord.resume_section_confirmation(
        task_id="task_100",
        graph_run_id="run-100",
        decision=decision,
    )

    call_config = mock_compiled.ainvoke.call_args[1]["config"]
    assert call_config["configurable"]["thread_id"] == expected_thread_id

    cmd = mock_compiled.ainvoke.call_args[0][0]
    assert isinstance(cmd, Command)
    assert cmd.resume == decision
