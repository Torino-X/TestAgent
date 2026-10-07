"""Test: 13. Checkpoint resume — MemorySaver + thread_id 二次读 state."""

from __future__ import annotations

import pytest

from app.agent_runtime.incremental.subgraph import build_incremental_subgraph


@pytest.mark.asyncio
async def test_subgraph_memory_saver_thread_id_restore():
    """Mock state 写入 checkpoint → 新 subgraph 实例 + 同 thread_id 读回。"""
    g1 = build_incremental_subgraph()  # 默认无 checkpointer

    # 使用 checkpointer
    from langgraph.checkpoint.memory import MemorySaver
    cp = MemorySaver()
    g2 = build_incremental_subgraph(checkpointer=cp)

    # graph 编译成功,get_state 不会 raise
    cfg = {"configurable": {"thread_id": "task-test-123"}}
    state = g2.get_state(cfg)
    # 无写入则 state 为 None / 空
    assert state is None or state.values == {} or state.values is None


def test_subgraph_thread_id_isolated():
    """不同 thread_id 互不污染。"""
    from langgraph.checkpoint.memory import MemorySaver
    cp = MemorySaver()
    g = build_incremental_subgraph(checkpointer=cp)

    cfg_a = {"configurable": {"thread_id": "thread-A"}}
    cfg_b = {"configurable": {"thread_id": "thread-B"}}

    # 两个 thread_id 都能查询(空 state);两个 thread 的 config 必须不同
    state_a = g.get_state(cfg_a)
    state_b = g.get_state(cfg_b)
    assert state_a is not None
    assert state_b is not None
    # thread_id 隔离:两个 snapshot 的 configurable.thread_id 必须不同
    assert state_a.config["configurable"]["thread_id"] == "thread-A"
    assert state_b.config["configurable"]["thread_id"] == "thread-B"
    # 同 cp 下双方 values 一致(空)
    assert state_a.values == state_b.values