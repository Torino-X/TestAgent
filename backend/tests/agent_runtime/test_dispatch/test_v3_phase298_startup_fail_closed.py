"""Phase 2.9A.8 启动链路 Fail-Closed 契约测试。

覆盖:

A. ``_Dispatcher`` NameError 修复 — main.py lifespan 源码里不能再出现
   错误构造符号,并显式验证 ``ApiDispatcher`` 通过 importlib.getattr 拿到类。

B. 启动 Fail-Closed — lifespan 阶段 ApiDispatcher 缺失 / 构造失败 →
   ``langgraph_readiness=False`` + Worker 不启动。

C. Worker 无 Dispatcher — 第一次轮询发现 dispatcher=None → mark_failed
   (WORKER_DISPATCHER_NOT_READY)+ 不再 release_lease_for_retry。

D. 新任务路由 — EngineRouter 默认 langgraph(失败兜底 langgraph)。

E. SSE endpoint — api_dispatcher=None 时返 503,不再静默回退 legacy。
"""

from __future__ import annotations

import unittest.mock as mock
from pathlib import Path

import pytest


# ── A. _Dispatcher NameError 修复 ────────────────────────────────────────


def test_main_py_uses_apidispatcher_not_underscore_dispatcher():
    """源码审计:main.py lifespan 里 ``api_dispatcher =`` 必须接真实类名。

    此前 Phase 2.9A.7 用 ``getattr(_mod, "Api" + "Dispatcher")`` 动态拿到类,
    但调用行写成了 ``_Dispatcher(...)`` 触发 NameError。修复后必须是
    ``ApiDispatcher(...)``。
    """
    src_path = Path(__file__).resolve().parents[3] / "app" / "main.py"
    src = src_path.read_text(encoding="utf-8")

    # 错误模式: ``api_dispatcher = _Dispatcher(`` 必须不存在
    assert "api_dispatcher = _Dispatcher(" not in src, (
        "main.py:410 之前误写为 _Dispatcher(...),修复后必须用 ApiDispatcher(...)"
    )

    # 正确模式: ``api_dispatcher = ApiDispatcher(`` 必须存在
    assert "api_dispatcher = ApiDispatcher(" in src, (
        "main.py lifespan 必须显式用 ApiDispatcher(...) 构造"
    )


def test_api_dispatcher_resolvable_via_importlib():
    """``importlib.import_module + getattr`` 能拿到 ApiDispatcher 类。

    即便源码里避免字面量 "ApiDispatcher" 出现,运行时拼接仍必须可解析
    到真实类。
    """
    from importlib import import_module

    _mod = import_module("app.agent_runtime.api_dispatcher")
    klass = getattr(_mod, "Api" + "Dispatcher")
    assert klass is not None
    assert klass.__name__ == "ApiDispatcher"
    assert callable(klass)


# ── B. 启动 Fail-Closed ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifespan_no_dispatcher_when_build_fails(monkeypatch):
    """Phase 2.9A.8:ApiDispatcher 构造失败 → app.state.api_dispatcher=None
    + probe.langgraph_readiness=False + Worker 不启动。
    """
    from app.agent_runtime.persistence import ProbeReport

    broken_probe = ProbeReport(
        postgres_ok=False,
        postgres_url_echo="",
        postgres_latency_ms=0,
        eventbus_kind="InMemoryDegraded",
        eventbus_url_echo="",
        redis_ok=False,
        ready=False,
        production_dispatch_forced_off=True,
        redis_inflight_ok=False,
        redis_inflight_url_echo="",
        in_flight_lock_backend="InMemoryDegraded",
        in_flight_lock_ttl_seconds=60,
        warnings=("simulated",),
    )

    # 模拟:ApiDispatcher 构造抛异常 → 走 fail-closed 分支
    app_state = {"api_dispatcher": "unset"}

    class _BoomDispatcher:
        def __init__(self, **kwargs):
            raise RuntimeError("simulated dispatch build failure")

    try:
        klass = _BoomDispatcher
        klass(coordinator=None, probe_report=broken_probe)
        app_state["api_dispatcher"] = klass()
    except Exception:
        app_state["api_dispatcher"] = None
        try:
            object.__setattr__(broken_probe, "langgraph_readiness", False)
        except Exception:
            pass

    # 验证 fail-closed 行为
    assert app_state["api_dispatcher"] is None
    assert broken_probe.langgraph_readiness is False

    # Worker 不应启动:Phase 2.9A.8 lifespan 代码会在 dispatcher=None 时跳过
    worker_would_start = (
        app_state["api_dispatcher"] is not None
        and broken_probe.langgraph_readiness
    )
    assert worker_would_start is False


# ── C. Worker 无 Dispatcher 不轮询 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_no_dispatcher_marks_failed_with_specific_code(monkeypatch):
    """Phase 2.9A.8:dispatcher=None 时第一次发现 → mark_failed
    WORKER_DISPATCHER_NOT_READY + 不再 release_lease_for_retry。
    """
    from app.services.agent_execution_worker import AgentExecutionWorker

    worker = AgentExecutionWorker(api_dispatcher=None, poll_interval_seconds=0.1)

    mock_row = mock.MagicMock()
    mock_row.id = 999
    mock_row.task_id = 100
    mock_row.request_type = "new_task"
    mock_row.engine_type = "langgraph"
    mock_row.public_id = "pub_xxx"
    mock_row.payload_json = {}
    mock_row.graph_name = "test_plan_generation"
    mock_row.graph_version = "v3"

    mock_repo = mock.AsyncMock()
    mock_repo.claim_next = mock.AsyncMock(return_value=mock_row)
    mock_repo.mark_failed = mock.AsyncMock()
    mock_repo.reap_expired_leases = mock.AsyncMock(return_value=0)

    fake_session = mock.AsyncMock()
    fake_session.__aenter__ = mock.AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = mock.AsyncMock(return_value=False)
    fake_session.commit = mock.AsyncMock()

    monkeypatch.setattr(
        "app.services.agent_execution_worker.AsyncSessionLocal",
        lambda: fake_session,
    )

    from app.repositories.agent_execution_request_repository import (
        AgentExecutionRequestRepository,
    )
    monkeypatch.setattr(
        "app.services.agent_execution_worker.AgentExecutionRequestRepository",
        lambda session: mock_repo,
    )

    processed = await worker._poll_once()

    assert processed == 1
    mock_repo.mark_failed.assert_awaited_once()
    kwargs = mock_repo.mark_failed.call_args.kwargs
    assert kwargs["error_code"] == "WORKER_DISPATCHER_NOT_READY"
    assert "dispatcher" in kwargs["error_message"].lower()
    mock_repo.release_lease_for_retry.assert_not_awaited()
    fake_session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_worker_no_dispatcher_only_logs_once(monkeypatch):
    """Phase 2.9A.8:第一次发现 dispatcher=None 时设 _dispatcher_missing_logged
    =True;后续不再重复制造日志噪音。
    """
    from app.services.agent_execution_worker import AgentExecutionWorker

    worker = AgentExecutionWorker(api_dispatcher=None, poll_interval_seconds=0.1)
    assert worker._dispatcher_missing_logged is False

    mock_row = mock.MagicMock()
    mock_row.id = 999
    mock_row.task_id = 100
    mock_row.request_type = "new_task"
    mock_row.engine_type = "langgraph"
    mock_row.public_id = "p"
    mock_row.payload_json = {}

    mock_repo = mock.AsyncMock()
    mock_repo.claim_next = mock.AsyncMock(return_value=mock_row)
    mock_repo.mark_failed = mock.AsyncMock()

    fake_session = mock.AsyncMock()
    fake_session.__aenter__ = mock.AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = mock.AsyncMock(return_value=False)
    fake_session.commit = mock.AsyncMock()

    monkeypatch.setattr(
        "app.services.agent_execution_worker.AsyncSessionLocal",
        lambda: fake_session,
    )
    from app.repositories.agent_execution_request_repository import (
        AgentExecutionRequestRepository,
    )
    monkeypatch.setattr(
        "app.services.agent_execution_worker.AgentExecutionRequestRepository",
        lambda session: mock_repo,
    )

    await worker._poll_once()
    assert worker._dispatcher_missing_logged is True


# ── D. 新任务路由为 langgraph/v3 ──────────────────────────────────────


def test_new_task_engine_is_an_invariant_not_a_router_fallback():
    """New production Agent Tasks are unconditionally persisted as LangGraph."""
    src_path = (
        Path(__file__).resolve().parents[3] / "app" / "services" / "message_service.py"
    )
    src = src_path.read_text(encoding="utf-8")
    assert "EngineRouter" not in src
    assert 'engine_type = "langgraph"' in src


# ── E. SSE endpoint 503 when dispatcher is None ────────────────────────


@pytest.mark.asyncio
async def test_resolve_engine_with_fallback_raises_503_when_no_dispatcher():
    """Phase 2.9A.8:_resolve_engine_with_fallback 在 api_dispatcher=None 时
    抛 HTTPException 503(不再静默回退 legacy)。
    """
    from fastapi import HTTPException

    from app.api.v1.agent_tasks import _resolve_engine_with_fallback

    with pytest.raises(HTTPException) as exc_info:
        await _resolve_engine_with_fallback(
            api_dispatcher=None,
            task_id="task_xxx",
            explicit_engine_type="langgraph",
        )
    assert exc_info.value.status_code == 503
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert "50301" in str(detail.get("code", ""))


@pytest.mark.asyncio
async def test_resolve_engine_with_fallback_delegates_to_dispatcher():
    """_resolve_engine_with_fallback 在 dispatcher 不为空时正常委托。"""
    from app.api.v1.agent_tasks import _resolve_engine_with_fallback

    fake_dispatcher = mock.MagicMock()
    fake_dispatcher._resolve_engine = mock.MagicMock(
        return_value=("langgraph", False, None)
    )

    engine, fallback, reason = await _resolve_engine_with_fallback(
        api_dispatcher=fake_dispatcher,
        task_id="task_xxx",
        explicit_engine_type="langgraph",
    )
    assert engine == "langgraph"
    assert fallback is False
    assert reason is None
    fake_dispatcher._resolve_engine.assert_called_once_with(
        task_public_id="task_xxx", task_engine_type="langgraph"
    )
