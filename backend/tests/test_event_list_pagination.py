"""Phase 2.9A.27 — event-list 分页 + 真实总数后端测试。

覆盖:
  1. ``EventRepository.list_by_task`` 不传 limit 时仍返回 list[dict](legacy 兼容)
  2. ``return_total=True`` 返回 (events, next_cursor, total) 三元组
  3. cursor 翻页正确(事件不被丢失 / 重复)
  4. summary_facts.payload 在 task_completed 事件持久化路径被检出
  5. trigger_message_id legacy fallback(conversation 内 nearest user_text)
  6. list_tasks_for_conversation 走 fallback 覆盖所有 task

不依赖真实 MySQL,全部 mock。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _RowLike:
    """SQLAlchemy Row 的最小替身 — 支持 ``r[i]`` 索引访问。"""

    def __init__(self, values: tuple):
        self._values = values

    def __getitem__(self, key):
        return self._values[key]


def _row(*values):
    return _RowLike(values)


# ──────────────────────────────────────────────────────────────────────
# 1. EventRepository.list_by_task — 默认签名兼容性
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_by_task_default_return_dict_list_legacy_compat() -> None:
    """向后兼容:不传 return_total 时返回 list[dict](Phase 2.9A.26 旧 API)。"""
    from app.repositories.event_repository import EventRepository

    # repository 按 r[0..15] 索引取值
    fake_row = _row(
        "event_001",          # r[0]  public_id
        "tool_finished",      # r[1]  event_type
        "tool_call",          # r[2]  message_type
        "x",                  # r[3]  title
        "y",                  # r[4]  content
        {"tool_call_id": "c1"},  # r[5]  payload_json
        "completed",          # r[6]  status
        10,                   # r[7]  sequence_no
        "run_1",              # r[8]  graph_run_id
        "v3",                 # r[9]  graph_version
        "parse",              # r[10] node_name
        1,                    # r[11] event_schema_version
        "idem_1",             # r[12] idempotency_key
        None,                 # r[13] created_at
        99,                   # r[14] id
        10,                   # r[15] canonical_order
    )

    fake_result = SimpleNamespace(fetchall=lambda: [fake_row])
    fake_session = SimpleNamespace(execute=AsyncMock(return_value=fake_result))
    repo = EventRepository(fake_session)

    out = await repo.list_by_task(task_internal_id=42)

    assert isinstance(out, list)
    assert out[0]["event_type"] == "tool_finished"


# ──────────────────────────────────────────────────────────────────────
# 2. return_total=True 返回三元组
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_by_task_return_total_returns_triple() -> None:
    from app.repositories.event_repository import EventRepository

    fake_row = _row(
        "event_002", "plan_created", None, "plan", None, None,
        "created", 5, "run_1", "v3", None, 1, None, None, 42, 5,
    )

    # repo 顺序是 先 SELECT (line 72) 再 COUNT (line 128)
    def route(*args, **_kwargs):
        if "COUNT(*)" in str(args[0]):
            return SimpleNamespace(scalar=lambda: 1)
        return SimpleNamespace(fetchall=lambda: [fake_row])

    fake_session = SimpleNamespace(execute=AsyncMock(side_effect=route))
    repo = EventRepository(fake_session)

    events, next_cursor, total = await repo.list_by_task(
        task_internal_id=42,
        limit=50,
        return_total=True,
    )

    assert total == 1
    # next_cursor 始终 = 本批最后一条 canonical_order;caller 用 events 为空作终止
    assert next_cursor == 5
    assert len(events) == 1
    assert events[0]["event_type"] == "plan_created"


# ──────────────────────────────────────────────────────────────────────
# 3. cursor 翻页 — 多页时不丢不重
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_by_task_cursor_pagination_no_drop_or_duplicate() -> None:
    """模拟 60 条事件按 limit=20 翻页,确认无丢无重。Phase 2.9A.27 设计:
    repo 永远返回 next_cursor = 本批最后一条 canonical_order;caller 用
    "events 为空" 或 "累计 == total" 作为终止信号。
    """
    from app.repositories.event_repository import EventRepository

    def make_row(idx: int):
        return _row(
            f"event_{idx}", "tool_finished", "tool_call", f"step {idx}", None,
            {"tool_call_id": f"c{idx}"}, "completed", idx, "run_1", "v3", None, 1,
            None, None, idx, idx,
        )

    page1 = [make_row(i) for i in range(20)]
    page2 = [make_row(i) for i in range(20, 40)]
    page3 = [make_row(i) for i in range(40, 60)]
    empty = []

    batch = {"n": 0}

    def route(*args, **_kwargs):
        if "COUNT(*)" in str(args[0]):
            return SimpleNamespace(scalar=lambda: 60)
        batch["n"] += 1
        rows = {1: page1, 2: page2, 3: page3, 4: empty}.get(batch["n"], [])
        return SimpleNamespace(fetchall=lambda: rows)

    fake_session = SimpleNamespace(execute=AsyncMock(side_effect=route))
    repo = EventRepository(fake_session)

    # 第 1 批
    e1, c1, t1 = await repo.list_by_task(42, limit=20, cursor=None, return_total=True)
    assert t1 == 60
    assert len(e1) == 20
    assert c1 == 19  # 本批最后一条 canonical_order

    # 第 2 批
    e2, c2, _ = await repo.list_by_task(42, limit=20, cursor=c1, return_total=True)
    assert len(e2) == 20
    assert c2 == 39
    assert e1[0]["public_id"] != e2[0]["public_id"]

    # 第 3 批
    e3, c3, _ = await repo.list_by_task(42, limit=20, cursor=c2, return_total=True)
    assert len(e3) == 20
    assert c3 == 59  # 仍然返回 cursor,caller 拿空页终止

    # 第 4 批 — cursor > 59,SQL 返回空,caller 终止
    e4, c4, _ = await repo.list_by_task(42, limit=20, cursor=c3, return_total=True)
    assert len(e4) == 0
    assert c4 is None  # 空批 → next_cursor=None

    # 累计无丢无重
    assert sum(len(x) for x in (e1, e2, e3, e4)) == 60


# ──────────────────────────────────────────────────────────────────────
# 4. limit 超过 500 时被截断(防 OOM)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_by_task_limit_clamped_to_500() -> None:
    from app.repositories.event_repository import EventRepository

    def route(*args, **_kwargs):
        if "COUNT(*)" in str(args[0]):
            return SimpleNamespace(scalar=lambda: 10000)
        return SimpleNamespace(fetchall=lambda: [])

    fake_session = SimpleNamespace(execute=AsyncMock(side_effect=route))
    repo = EventRepository(fake_session)

    e, c, total = await repo.list_by_task(1, limit=10000, return_total=True)
    assert total == 10000
    assert e == []
    assert c is None

    select_call = None
    for call in fake_session.execute.await_args_list:
        if "COUNT(*)" not in str(call.args[0]):
            select_call = call
            break
    assert select_call is not None
    # repo 传 params["lim"](name-based dict);AsyncMock 既可能在 args[1] 也可能在 kwargs
    passed_params = select_call.args[1] if len(select_call.args) > 1 else {}
    passed_params = {**passed_params, **select_call.kwargs}
    assert passed_params.get("lim") == 500


# ──────────────────────────────────────────────────────────────────────
# 5. AgentTaskService.list_events 透传 cursor / limit
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_events_forwards_cursor_and_limit_to_repository() -> None:
    from app.services.agent_task_service import AgentTaskService

    service = AgentTaskService.__new__(AgentTaskService)
    service._task_repo = SimpleNamespace(
        get_by_public_id=AsyncMock(
            return_value=SimpleNamespace(id=7, user_id=11, public_id="task_001")
        )
    )
    service._event_repo = SimpleNamespace(
        list_by_task=AsyncMock(return_value=([], None, 0))
    )

    payload = await service.list_events(
        "task_001",
        11,
        cursor=42,
        limit=100,
    )

    service._event_repo.list_by_task.assert_awaited_once_with(
        7, limit=100, cursor=42, return_total=True
    )
    assert payload == {"events": [], "next_cursor": None, "total": 0}


# ──────────────────────────────────────────────────────────────────────
# 6. trigger_message_id legacy fallback
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_trigger_message_id_legacy_fallback_finds_nearest_user_text() -> None:
    """旧任务 ``trigger_message_id`` 为 NULL 时,fallback 取同一 conversation
    内 created_at <= task.created_at 的最近一条 user_text 消息。"""
    from app.services.agent_task_service import AgentTaskService

    fake_task = SimpleNamespace(
        id=70,
        conversation_id=91,
        trigger_message_id=None,
        created_at=None,
    )

    fake_row = _row(94, "msg_trigger_001")  # (id, public_id)
    fake_result = SimpleNamespace(first=lambda: fake_row)
    fake_session = SimpleNamespace(execute=AsyncMock(return_value=fake_result))

    service = AgentTaskService.__new__(AgentTaskService)
    service._session = fake_session

    result = await service.resolve_trigger_message_id(fake_task)

    assert result == (94, "msg_trigger_001")  # Phase 2.9A.30: returns (internal_id, public_id)
    fake_session.execute.assert_awaited_once()
    call_args = fake_session.execute.await_args
    sql_text = str(call_args.args[0])
    assert "messages" in sql_text.lower()
    assert "role" in sql_text.lower()
    assert "user_text" in sql_text.lower()
    assert "LIMIT 1" in sql_text.upper()


@pytest.mark.asyncio
async def test_resolve_trigger_message_id_returns_existing_when_set() -> None:
    from app.services.agent_task_service import AgentTaskService

    fake_task = SimpleNamespace(
        id=70,
        conversation_id=91,
        trigger_message_id=123,
        created_at=None,
    )

    # Phase 2.9A.30: when trigger_message_id is set, one query to get public_id
    fake_row = _row("msg_trigger_123")
    fake_result = SimpleNamespace(first=lambda: fake_row)
    fake_session = SimpleNamespace(execute=AsyncMock(return_value=fake_result))
    service = AgentTaskService.__new__(AgentTaskService)
    service._session = fake_session

    out = await service.resolve_trigger_message_id(fake_task)

    assert out == (123, "msg_trigger_123")


@pytest.mark.asyncio
async def test_resolve_trigger_message_id_returns_none_when_no_user_text() -> None:
    from app.services.agent_task_service import AgentTaskService

    fake_task = SimpleNamespace(
        id=70,
        conversation_id=91,
        trigger_message_id=None,
        created_at=None,
    )

    fake_result = SimpleNamespace(first=lambda: None)
    fake_session = SimpleNamespace(execute=AsyncMock(return_value=fake_result))
    service = AgentTaskService.__new__(AgentTaskService)
    service._session = fake_session

    out = await service.resolve_trigger_message_id(fake_task)

    assert out == (None, None)


@pytest.mark.asyncio
async def test_resolve_trigger_message_id_handles_db_exception() -> None:
    from app.services.agent_task_service import AgentTaskService

    fake_task = SimpleNamespace(
        id=70,
        conversation_id=91,
        trigger_message_id=None,
        created_at=None,
    )

    fake_session = SimpleNamespace(
        execute=AsyncMock(side_effect=Exception("connection reset"))
    )
    service = AgentTaskService.__new__(AgentTaskService)
    service._session = fake_session

    out = await service.resolve_trigger_message_id(fake_task)
    assert out == (None, None)
# ──────────────────────────────────────────────────────────────────────


def test_to_detail_uses_explicit_trigger_message_id_arg() -> None:
    """_to_detail 显式 trigger_message_id 参数优先级高于 ORM 字段。"""
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        public_id="task_001",
        task_type="test_plan_generation",
        status="completed",
        plan_json=None,
        task_context_json=None,
        review_result_json=None,
        active_run_id=None,
        runtime_status="completed",
        started_at=None,
        completed_at=None,
        trigger_message_id=10,
    )
    out = AgentTaskService._to_detail(
        task,
        run=None,
        artifact_info=None,
        format_check_info=None,
        trigger_message_id="msg_trigger_999",  # Phase 2.9A.30: string, not int
    )

    assert out["trigger_message_id"] == "msg_trigger_999"


def test_to_detail_no_orm_fallback_requires_explicit_arg() -> None:
    """Phase 2.9A.30: _to_detail no longer falls back to ORM field.
    Caller must pass the resolved public_id explicitly."""
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        public_id="task_001",
        task_type="test_plan_generation",
        status="completed",
        plan_json=None,
        task_context_json=None,
        review_result_json=None,
        active_run_id=None,
        runtime_status="completed",
        started_at=None,
        completed_at=None,
        trigger_message_id=42,  # ORM field is set, but _to_detail ignores it
    )
    out = AgentTaskService._to_detail(task, run=None, artifact_info=None, format_check_info=None)
    # Without explicit trigger_message_id, the field is None
    assert out["trigger_message_id"] is None


def test_to_detail_returns_none_when_no_trigger_anywhere() -> None:
    from app.services.agent_task_service import AgentTaskService

    task = SimpleNamespace(
        public_id="task_001",
        task_type="test_plan_generation",
        status="completed",
        plan_json=None,
        task_context_json=None,
        review_result_json=None,
        active_run_id=None,
        runtime_status="completed",
        started_at=None,
        completed_at=None,
        trigger_message_id=None,
    )
    out = AgentTaskService._to_detail(task, run=None, artifact_info=None, format_check_info=None)
    assert out["trigger_message_id"] is None


# ──────────────────────────────────────────────────────────────────────
# 8. list_tasks_for_conversation 走每个 task 的 legacy fallback
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_tasks_for_conversation_invokes_fallback_per_task() -> None:
    from app.services.agent_task_service import AgentTaskService

    fake_task_a = SimpleNamespace(
        id=70,
        public_id="task_a",
        task_type="test_plan_generation",
        status="completed",
        engine_type="langgraph",
        trigger_message_id=None,
        created_at=None,
        started_at=None,
        completed_at=None,
        conversation_id=91,
        plan_json=[],
        task_context_json=None,
        review_result_json=None,
        active_run_id=None,
        runtime_status=None,
    )
    fake_task_b = SimpleNamespace(
        id=71,
        public_id="task_b",
        task_type="test_plan_generation",
        status="running",
        engine_type="langgraph",
        trigger_message_id=200,
        created_at=None,
        started_at=None,
        completed_at=None,
        conversation_id=91,
        plan_json=[],
        task_context_json=None,
        review_result_json=None,
        active_run_id=None,
        runtime_status=None,
    )

    service = AgentTaskService.__new__(AgentTaskService)
    service._conv_repo = SimpleNamespace(
        get_by_public_id=AsyncMock(
            return_value=SimpleNamespace(id=91, user_id=11, public_id="conv_001")
        )
    )
    service._task_repo = SimpleNamespace(
        list_by_conversation=AsyncMock(return_value=[fake_task_a, fake_task_b])
    )
    service._run_repo = SimpleNamespace(
        list_by_task=AsyncMock(return_value=[])
    )

    async def fake_fallback(task):
        if task.public_id == "task_a":
            return (999, "msg_trigger_999")  # (internal_id, public_id)
        return (int(task.trigger_message_id), f"msg_trigger_{task.trigger_message_id}")

    service.resolve_trigger_message_id = fake_fallback

    out = await service.list_tasks_for_conversation(11, "conv_001")

    assert out[0]["trigger_message_id"] == "msg_trigger_999"  # public_id
    assert out[1]["trigger_message_id"] == "msg_trigger_200"  # public_id


@pytest.mark.asyncio
async def test_list_tasks_for_conversation_returns_run_timing_and_duration() -> None:
    from app.services.agent_task_service import AgentTaskService

    fake_task = SimpleNamespace(
        id=70,
        public_id="task_7e15d3a4",
        task_type="test_plan_generation",
        status="completed",
        engine_type="langgraph",
        trigger_message_id=123,
        created_at=datetime(2026, 8, 1, 4, 58, 40, tzinfo=timezone.utc),
        started_at=None,
        completed_at=None,
        conversation_id=91,
        plan_json=[],
        task_context_json=None,
        review_result_json=None,
        active_run_id="run_001",
        runtime_status="idle",
    )
    fake_run = SimpleNamespace(
        public_id="run_001",
        status="completed",
        started_at=datetime(2026, 8, 1, 4, 58, 45, tzinfo=timezone.utc),
        finished_at=datetime(2026, 8, 1, 5, 1, 34, tzinfo=timezone.utc),
    )
    service = AgentTaskService.__new__(AgentTaskService)
    service._conv_repo = SimpleNamespace(
        get_by_public_id=AsyncMock(
            return_value=SimpleNamespace(id=91, user_id=11, public_id="conv_001")
        )
    )
    service._task_repo = SimpleNamespace(
        list_by_conversation=AsyncMock(return_value=[fake_task])
    )
    service._run_repo = SimpleNamespace(
        get_by_public_id=AsyncMock(return_value=fake_run),
        list_by_task=AsyncMock(return_value=[fake_run]),
    )
    service.resolve_trigger_message_id = AsyncMock(return_value=(123, "msg_trigger_123"))

    out = await service.list_tasks_for_conversation(11, "conv_001")

    assert len(out) == 1
    expected_fields = {
        "task_id": "task_7e15d3a4",
        "task_type": "test_plan_generation",
        "status": "completed",
        "events_url": "/api/agent/tasks/task_7e15d3a4/events",
        "engine_type": "langgraph",
        "active_run_id": "run_001",
        "runtime_status": "idle",
        "started_at": "2026-08-01T04:58:45+00:00",
        "completed_at": "2026-08-01T05:01:34+00:00",
        "duration_ms": 169000,
        "trigger_message_id": "msg_trigger_123",
    }
    for key, expected in expected_fields.items():
        assert out[0][key] == expected
    assert out[0]["run"] == {
        "run_id": "run_001",
        "status": "completed",
        "started_at": "2026-08-01T04:58:45+00:00",
        "finished_at": "2026-08-01T05:01:34+00:00",
        "duration_ms": 169000,
    }
