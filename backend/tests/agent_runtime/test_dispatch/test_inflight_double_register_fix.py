"""Phase 2.8C ADR-2.8C-10 — ApiDispatcher + agent_tasks.py 双重登记修复验证。"""

from __future__ import annotations

import inspect


def test_agent_tasks_no_longer_calls_register_inflight() -> None:
    """agent_tasks.py SSE 端点必须**不再**显式调 ``_register_inflight``。

    单一登记点 = ApiDispatcher.dispatch_* 内部 begin()(ADR-2.8C-10)。
    这避免同任务二次 begin → ParallelDispatchGuardError。
    """
    from app.api.v1 import agent_tasks

    src = inspect.getsource(agent_tasks)
    # 显式 await _register_inflight(...) 调用应已移除
    assert "await _register_inflight(" not in src, (
        "agent_tasks.py SSE 端点不应再调 _register_inflight; "
        "单一登记点 = ApiDispatcher.dispatch_*(phase 2.8C ADR-2.8C-10)"
    )


def test_register_inflight_helper_still_defined_for_back_compat() -> None:
    """``_register_inflight`` helper 函数仍保留(供边界调用),但默认 SSE 端点不调。"""
    from app.api.v1 import agent_tasks

    assert hasattr(agent_tasks, "_register_inflight"), (
        "保留 _register_inflight helper(向后兼容边界调用)"
    )
    assert inspect.iscoroutinefunction(agent_tasks._register_inflight)


def test_api_dispatcher_is_single_registration_point() -> None:
    """All execution paths share one in-flight registration boundary."""
    from app.agent_runtime.api_dispatcher import ApiDispatcher

    guarded_src = inspect.getsource(ApiDispatcher._run_guarded)
    assert guarded_src.count("self._inflight.begin(") == 1
    assert "finally:" in guarded_src
    assert "self._inflight.end(" in guarded_src
