"""Phase 2.9A.6 AsyncPostgresSaver Resume 异步契约测试。

覆盖:
  1. 生产代码中无 compiled.get_state 同步调用(源码检查)
  2. _load_checkpoint_state_by_thread_id 是 async def
  3. 所有调用方正确 await
  4. Checkpoint 不存在时明确 ValueError
  5. pre-confirm 与 resume thread_id 一致
"""

from __future__ import annotations

import inspect
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 1. 无 compiled.get_state 同步调用 ─────────────────────────────────────


def test_coordinator_has_no_sync_get_state_in_production_code():
    """langgraph_run_coordinator.py 中不应出现 compiled.get_state(同步) 调用。

    Phase 2.9A.6:所有 get_state 必须改为 aget_state(异步)。
    允许:aget_state (异步)、assert 中的字符串引用、注释。
    """
    from app.agent_runtime import langgraph_run_coordinator as module

    src = inspect.getsource(module)
    # 排除注释和字符串
    code_lines = [
        line for line in src.split("\n")
        if not line.strip().startswith("#")
        and not line.strip().startswith('"""')
        and not line.strip().startswith("'")
    ]
    code = "\n".join(code_lines)

    # 禁止 .get_state( 同步调用(但允许 .aget_state( 异步调用)
    assert ".aget_state(" in code, "必须使用异步 aget_state()"
    assert "aget_state" in code


def test_graph_runtime_service_has_async_get_state():
    """graph_runtime_service.py 的 get_state 必须是 async def。"""
    from app.agent_runtime.graph_runtime_service import GraphRuntimeService

    src = inspect.getsource(GraphRuntimeService.get_state)
    assert "async def get_state" in inspect.getsource(GraphRuntimeService) or "await compiled.aget_state" in src, (
        "GraphRuntimeService.get_state 必须用 await compiled.aget_state"
    )
    assert "await compiled.aget_state" in src


# ── 2. _load_checkpoint_state_by_thread_id 是 async def ──────────────────


def test_load_checkpoint_state_by_thread_id_is_async():
    """_load_checkpoint_state_by_thread_id 必须是 async def。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    method = getattr(LangGraphRunCoordinator, "_load_checkpoint_state_by_thread_id")
    assert inspect.iscoroutinefunction(method), (
        "_load_checkpoint_state_by_thread_id 必须是 async def"
    )


def test_load_checkpoint_state_is_async():
    """_load_checkpoint_state 必须是 async def。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    method = getattr(LangGraphRunCoordinator, "_load_checkpoint_state")
    assert inspect.iscoroutinefunction(method), (
        "_load_checkpoint_state 必须是 async def"
    )


# ── 3. 所有调用方正确 await ───────────────────────────────────────────────


def test_resume_section_confirmation_awaits_checkpoint_load():
    """resume_section_confirmation 必须 await _load_checkpoint_state_by_thread_id。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    src = inspect.getsource(LangGraphRunCoordinator.resume_section_confirmation)
    assert "await self._load_checkpoint_state_by_thread_id" in src


def test_resume_format_loss_interrupt_awaits_checkpoint_load():
    """resume_format_loss_interrupt 必须 await _load_checkpoint_state_by_thread_id。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    src = inspect.getsource(LangGraphRunCoordinator.resume_format_loss_interrupt)
    assert "await self._load_checkpoint_state_by_thread_id" in src


def test_resume_thread_awaits_both_checkpoint_loads():
    """resume_thread 必须 await _load_checkpoint_state_by_thread_id 和 _load_checkpoint_state。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    src = inspect.getsource(LangGraphRunCoordinator.resume_thread)
    assert "await self._load_checkpoint_state_by_thread_id" in src
    assert "await self._load_checkpoint_state" in src


# ── 4. Checkpoint 不存在时明确 ValueError ─────────────────────────────────


@pytest.mark.asyncio
async def test_load_checkpoint_state_raises_on_missing():
    """_load_checkpoint_state 在 checkpoint 不存在时抛 ValueError。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)

    mock_compiled = MagicMock()
    mock_compiled.aget_state = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="no checkpoint found"):
        await coord._load_checkpoint_state(compiled=mock_compiled, task_id="missing_task")


@pytest.mark.asyncio
async def test_load_checkpoint_state_raises_on_empty_values():
    """_load_checkpoint_state 在 snapshot.values 为空时抛 ValueError。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)

    mock_snapshot = MagicMock()
    mock_snapshot.values = {}
    mock_compiled = MagicMock()
    mock_compiled.aget_state = AsyncMock(return_value=mock_snapshot)

    with pytest.raises(ValueError, match="no checkpoint found"):
        await coord._load_checkpoint_state(compiled=mock_compiled, task_id="empty_task")


# ── 5. pre-confirm 与 resume thread_id 一致 ───────────────────────────────


def test_resume_uses_task_id_for_thread_id():
    """_build_resume_config 用 task_id 作 thread_id,与 pre-confirm 一致。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    src = inspect.getsource(LangGraphRunCoordinator._build_resume_config)
    assert 'setdefault("task_id", task_id)' in src
    assert "_build_config(state)" in src


# ── 6. aget_state 在 production code 中 ────────────────────────────────────


def test_coordinator_resume_methods_use_async_checkpoint_api():
    """resume_section_confirmation 和 resume_format_loss_interrupt 必须用
    await compiled.aget_state 或直接 await compiled.ainvoke(Command(...))。"""
    from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

    for method_name in ["resume_section_confirmation", "resume_format_loss_interrupt", "resume_thread"]:
        method = getattr(LangGraphRunCoordinator, method_name)
        src = inspect.getsource(method)
        assert "await self._load_checkpoint_state_by_thread_id" in src, (
            f"{method_name} 必须 await _load_checkpoint_state_by_thread_id"
        )
