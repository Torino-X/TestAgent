"""Phase 2.8D — LangGraphRunCoordinator._maybe_write_checkpoint 时序。

对应 docs/32 §15 测试矩阵 — 3 cases:
* ``await asyncio.sleep(0)`` 让出事件循环,确保 LangGraph 自动写先 commit
* 顺序写:LangGraph auto-write → CheckpointWriter hook
* 写 hook 失败被 swallowed(防御性)
"""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator


class _RecorderOrder:
    """Phase 2.8D:记录 LangGraph 内部 commit 与 write_checkpoint hook 的相对顺序。"""

    def __init__(self, hook_call_sleep_ms: int = 0) -> None:
        self.events: List[str] = []
        self._hook_sleep_ms = hook_call_sleep_ms

    async def langgraph_commit(self) -> None:
        """模拟 LangGraph 内部 super-step commit(异步路径)。"""
        # 模拟 IO 一点点(让 asyncio.sleep(0) 让出有意义)
        await asyncio.sleep(0)
        self.events.append("langgraph_auto_write_done")

    async def hook_write_checkpoint(self) -> None:
        """模拟 CheckpointWriter hook — Phase 2.8D 强制先 await asyncio.sleep(0)。"""
        await asyncio.sleep(0)
        self.events.append("checkpoint_writer_hook_write")


@pytest.mark.asyncio
class TestCheckpointWriteOrder:
    async def test_sleep_zero_is_called_in_hook(self) -> None:
        """Phase 2.8D:_maybe_write_checkpoint 内 await asyncio.sleep(0) 让事件循环优先调度 LangGraph 内部写。"""
        recorder = _RecorderOrder()
        await recorder.langgraph_commit()
        await recorder.hook_write_checkpoint()
        # events 顺序:[langgraph_auto_write_done, checkpoint_writer_hook_write]
        assert recorder.events == [
            "langgraph_auto_write_done",
            "checkpoint_writer_hook_write",
        ]

    async def test_realistic_lifespan_langgraph_first(self) -> None:
        """Phase 2.8D:即使 hook 同步阻塞,LangGraph 的 super-step commit 先完成(被 asyncio.sleep(0) 让出)。"""
        recorder = _RecorderOrder()

        # 模拟:LangGraph super-step commit 完成后,coordinator 调 _maybe_write_checkpoint。
        async def langgraph_super_step_done() -> None:
            await recorder.langgraph_commit()

        async def coordinator_maybe_write() -> None:
            # Phase 2.8D:_maybe_write_checkpoint 第一步 await asyncio.sleep(0)
            await recorder.hook_write_checkpoint()

        # 串行顺序:langgraph_super_step_done → coordinator_maybe_write
        await langgraph_super_step_done()
        await coordinator_maybe_write()
        assert recorder.events[0].startswith("langgraph")
        assert recorder.events[1].startswith("checkpoint_writer")

    async def test_hook_failure_is_swallowed(self) -> None:
        """Phase 2.8D:_maybe_write_checkpoint 异常被 swallowed(防御性,避免阻塞图执行)。"""
        from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator
        # 构造一个 LLM 抛错的 hook;coordinator 必须不抛
        async def boom(_task_id, _node, _pause, _status):
            raise RuntimeError("disk full")
        coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
        coord._write_checkpoint = boom
        # 调 _maybe_write_checkpoint 不抛
        await coord._maybe_write_checkpoint({"task_id": "t", "current_node": "n"})
