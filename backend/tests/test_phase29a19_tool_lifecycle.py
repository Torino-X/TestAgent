"""Phase 2.9A.19 Tool 生命周期事件单一出口测试。

覆盖:
  1. Adapter 是 tool_started/tool_finished/tool_failed 的唯一点
  2. Graph Node 不再发 TOOL_FAILED,改用 STAGE_FAILED(业务失败)
  3. fail_task 用 TASK_FAILED(由 Adapter / node / fail_task 唯一责任划分)
  4. 同一工具失败时不会双发 tool_failed(Adapter 已 dedup last frame)
  5. STAGE_FAILED 是新 enum,前端可识别
"""

from __future__ import annotations

from app.agent.enums import AgentEventType


class TestAgentEventTypeStageFailed:
    def test_stage_failed_enum_present(self):
        """Phase 2.9A.19:STAGE_FAILED 必须存在,作为 Graph Node 业务领域失败事件"""
        assert AgentEventType.STAGE_FAILED.value == "stage_failed"

    def test_tool_failed_still_exists_for_adapter(self):
        """Adapter 仍是 tool_failed 唯一拥有者"""
        assert AgentEventType.TOOL_FAILED.value == "tool_failed"

    def test_task_failed_for_fail_task(self):
        """fail_task 节点用 TASK_FAILED"""
        assert AgentEventType.TASK_FAILED.value == "task_failed"


# ── 测试 Graph Node 不再直接发 TOOL_FAILED ────────────────────────────────


class TestGraphNodeNoLongerEmitsToolFailed:
    """Phase 2.9A.19 §5.2 责任划分:Graph Node 不发 TOOL_FAILED,改用 STAGE_FAILED."""

    def test_post_confirm_v3_uses_stage_failed(self):
        """v3 nodes_post_confirm 与 nodes_review_format 应不再硬编码 TOOL_FAILED。

        Audit by source: 通过 grep 模拟 — 当前所有 v3 节点都用 STAGE_FAILED
        而非 TOOL_FAILED 表达业务失败。
        """
        import os

        # 静态扫描 v3 节点源文件
        v3_dir = (
            os.path.dirname(os.path.abspath(__file__)).replace("\\tests", "")
            + "\\app\\agent_runtime\\graphs\\test_plan\\versions\\v3"
        )
        offenders = []
        for root, _, files in os.walk(v3_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                # 跳过 terminal 节点(它们发 TASK_FAILED)与 routing
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
                if "event_type=AgentEventType.TOOL_FAILED.value" in content:
                    offenders.append(fpath)

        assert (
            offenders == []
        ), f"v3 节点仍在发 TOOL_FAILED,应当改用 STAGE_FAILED: {offenders}"


# ── Adapter 去重保证 ──────────────────────────────────────────────────────


class TestAdapterToolFailedDedup:
    """Phase 2.9A.13 + 2.9A.19:Adapter 仅对失败分支发最后一帧,不重复 tool_failed。"""

    def test_adapter_dedups_last_frame_for_failure(self):
        """源码验证 — `_emit_tool_chunks` 必须对 tool_failed 路径 frames = [frames[-1]]"""
        import os

        adapter_path = (
            os.path.dirname(os.path.abspath(__file__)).replace("\\tests", "")
            + "\\app\\agent_runtime\\adapters\\test_agent_tool_adapter.py"
        )
        with open(adapter_path, encoding="utf-8") as f:
            content = f.read()

        assert "event_type in (\"tool_failed\", \"tool_retry\")" in content
        assert "frames = [frames[-1]]" in content

    def test_adapter_passes_graph_state_to_tool_chunk_emitter(self):
        """Regression: dynamic-agent tool chunks need graph_state for public-update overrides."""
        import inspect

        from app.agent_runtime.adapters.test_agent_tool_adapter import (
            TestAgentToolAdapter,
        )

        execute_src = inspect.getsource(TestAgentToolAdapter.execute)
        chunks_signature = inspect.signature(TestAgentToolAdapter._emit_tool_chunks)

        assert "graph_state=graph_state" in execute_src
        assert "graph_state" in chunks_signature.parameters


# ── fail_task 用 TASK_FAILED(责任划分) ────────────────────────────────────


class TestFailTaskEmitsTaskFailed:
    """Phase 2.9A.19 §5.2:fail_task 节点不写 tool_failed,只写 task_failed。"""

    def test_fail_task_emits_task_failed(self):
        import os

        v3_dir = (
            os.path.dirname(os.path.abspath(__file__)).replace("\\tests", "")
            + "\\app\\agent_runtime\\graphs\\test_plan\\versions\\v3"
        )
        nodes_terminal_path = os.path.join(v3_dir, "nodes_terminal.py")
        with open(nodes_terminal_path, encoding="utf-8") as f:
            content = f.read()

        assert "event_type=AgentEventType.TASK_FAILED.value" in content
        # fail_task 不应碰 TOOL_FAILED
        assert "event_type=AgentEventType.TOOL_FAILED.value" not in content
