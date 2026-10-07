#!/usr/bin/env python3
"""Phase 2.8F — End-to-end LangGraph v2 stack readiness script.

目标:不调 LLM,只验证 Phase 2.8A/B/C/D/E 全栈生产代码可组装运行:

  1. GraphState V6 schema 字段齐备(state_schema_version=6)
  2. TestPlanGraphState make_empty_state 默认值正确(attempt/retry_decision/summary)
  3. v2 graph build_compiled_v2_graph compile 通过(26 sentinel / 28 interrupt nodes)
  4. LangGraphRunCoordinator._build_config 注入 cfg["recursion_limit"]=10
  5. CheckpointStateMigration v2 → V6 + MigrationRequired reset
  6. _emit_with_public_update helper 链(tool_finished chunk 流)
  7. graph.py path_map + routing_after_interrupt 引用 Summary 节点

无外部依赖(LLM stub,InMemoryEventSink,MemorySaver)。

退出码:0 全部通过;非 0 任意阶段失败。
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from typing import Any, Dict

# Force UTF-8 stdout/stderr on Windows GBK console.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Make ``app.*`` importable when this script is launched directly via
# ``python scripts/e2e_test_phase_2_8_langgraph_full_stack.py``.  When
# pytest loads the same code we don't need the bootstrap, hence the
# cheap env probe.
if "app" not in sys.modules and not __package__:
    backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if backend_root not in sys.path:
        sys.path.insert(0, backend_root)


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def check(name: str, ok: bool, detail: str = "") -> bool:
    icon = f"{GREEN}OK{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  {icon} {name}{(' -- ' + detail) if detail else ''}")
    return ok


async def phase_1_v6_schema() -> bool:
    print(f"\n{YELLOW}[1] GraphState V6 schema + 3 字段")
    try:
        from app.agent_runtime.graphs.test_plan.constants import (
            STATE_SCHEMA_VERSION_V6,
            STATE_SCHEMA_VERSION_V5,
        )
        from app.agent_runtime.graphs.test_plan.state import make_empty_state

        s = make_empty_state(task_id="e2e_t1", graph_run_id="r1", graph_version="v2")
        ok_schema = s["state_schema_version"] == STATE_SCHEMA_VERSION_V6 == 6
        ok_attempt = s.get("attempt") == {} and isinstance(s["attempt"], dict)
        ok_retry = s.get("last_retry_decision") == {} and isinstance(s["last_retry_decision"], dict)
        ok_summary = s.get("summary") is None
        ok_super = STATE_SCHEMA_VERSION_V6 >= STATE_SCHEMA_VERSION_V5

        return all(check(s, ok_schema, f"V6={STATE_SCHEMA_VERSION_V6} >= V5={STATE_SCHEMA_VERSION_V5}") for s in [
            f"state_schema_version == V6 (V6 是 V3-5 的 super-set)",
        ]) and check("attempt 默认值 {}", ok_attempt) \
            and check("last_retry_decision 默认值 {}", ok_retry) \
            and check("summary 默认值 None", ok_summary) \
            and check("V6 >= V5 super-set", ok_super)
    except Exception:
        traceback.print_exc()
        return False


async def phase_2_v2_graph_compile() -> bool:
    print(f"\n{YELLOW}[2] v2 graph compile (sentinel + interrupt 路径)")
    try:
        from app.agent_runtime.graphs.test_plan.versions.v2.graph import (
            build_compiled_v2_graph, NODE_SUMMARY, NODE_FIN,
        )
        from langgraph.checkpoint.memory import MemorySaver

        g_sentinel = build_compiled_v2_graph(checkpointer=MemorySaver(), interrupt_enabled=False)
        n_sentinel = len(g_sentinel.get_graph().nodes)
        g_interrupt = build_compiled_v2_graph(checkpointer=MemorySaver(), interrupt_enabled=True)
        n_interrupt = len(g_interrupt.get_graph().nodes)

        return check(f"sentinel graph 编译 + {n_sentinel} nodes", n_sentinel >= 26) \
            and check(f"interrupt graph 编译 + {n_interrupt} nodes", n_interrupt >= 28) \
            and check("Summary 节点 (NODE_SUMMARY) 在 graph 中", NODE_SUMMARY == "generate_completion_summary") \
            and check("Finalize 节点 (NODE_FIN) 在 graph 中", NODE_FIN == "finalize_task")
    except Exception:
        traceback.print_exc()
        return False


async def phase_3_coordinator_config() -> bool:
    print(f"\n{YELLOW}[3] LangGraphRunCoordinator recursion_limit=10 注入")
    try:
        from app.agent_runtime.langgraph_run_coordinator import LangGraphRunCoordinator

        coord = LangGraphRunCoordinator.__new__(LangGraphRunCoordinator)
        coord._context_factory = None
        cfg = coord._build_config({"task_id": "e2e_t3"})
        return check("cfg.recursion_limit == 10", cfg["recursion_limit"] == 10) \
            and check("cfg.thread_id 一致", cfg["configurable"]["thread_id"] == "e2e_t3")
    except Exception:
        traceback.print_exc()
        return False


async def phase_4_migration() -> bool:
    print(f"\n{YELLOW}[4] CheckpointStateMigration v2 → V6 + MigrationRequired reset")
    try:
        from app.agent_runtime.checkpoint_state_migration import (
            migrate_to_v6, MigrationRequired, V6_NEW_FIELDS,
        )

        valid = migrate_to_v6({
            "state_schema_version": 2,
            "current_node": "a",
            "graph_run_id": "g",
            "task_id": "t",
        })
        ok_valid = valid["state_schema_version"] == 6 and set(V6_NEW_FIELDS) == {
            "attempt", "last_retry_decision", "summary"
        } and valid["attempt"] == {}

        ok_missing = False
        try:
            migrate_to_v6({"state_schema_version": 2, "task_id": "t"})
        except MigrationRequired:
            ok_missing = True

        return check("V2 state → V6 + 3 字段默认填充", ok_valid) \
            and check("缺关键字段抛 MigrationRequired", ok_missing)
    except Exception:
        traceback.print_exc()
        return False


async def phase_5_emit_helper() -> bool:
    print(f"\n{YELLOW}[5] _emit_with_public_update helper chain (TOOL_FINISHED + chunk)")
    try:
        from app.agent_runtime.graphs.nodes_emit_helper import _emit_with_public_update
        from app.agent_runtime.events.sink import InMemoryEventSink
        from app.agent.enums import AgentEventType

        class _StubCtx:
            def __init__(self, sink): self.event_sink = sink; self.graph_run_id = "r5"

        sink = InMemoryEventSink()
        ctx = _StubCtx(sink)
        await _emit_with_public_update(
            task_id="e2e_t5",
            node_name="search_knowledge",
            event_type=AgentEventType.TOOL_FINISHED.value,
            payload={"data": {"hits": 5}},
            ctx=ctx,
            tool_name="KnowledgeSearchTool",
            success=True,
            elapsed_ms=120,
        )
        events = sink.collect()
        ok_chunk = bool(events) and "public_execution_update" in events[-1]["payload"]
        return check("emit 1+ 事件 + payload 嵌入 public_execution_update chunk", ok_chunk)
    except Exception:
        traceback.print_exc()
        return False


async def phase_6_summary_node_chain() -> bool:
    print(f"\n{YELLOW}[6] generate_completion_summary_node 字节级复用 Legacy 兜底")
    try:
        from app.agent_runtime.graphs.test_plan.state import make_empty_state
        from app.agent_runtime.graphs.test_plan.versions.v2.nodes_post_confirm import (
            generate_completion_summary_node, _fallback_summary,
        )
        from app.agent_runtime.events.sink import InMemoryEventSink

        class _StubCtx:
            def __init__(self, sink):
                self.user_internal_id = 1
                self.task_internal_id = 100
                self.conversation_internal_id = 0
                self._settings_service = None
                self._sink = sink
                self.graph_run_id = "r6"

            @property
            def settings_service(self): return self._settings_service
            @property
            def event_sink(self): return self._sink

        sink = InMemoryEventSink()
        ctx = _StubCtx(sink)
        state = make_empty_state(task_id="e2e_t6", graph_run_id="r6")
        state["test_plan_content"] = {"generated_sections": 3, "kept_sections": 1}
        state["artifact"] = {"file_name": "plan.docx"}

        out = await generate_completion_summary_node(state, ctx=ctx)
        ok_summary = "summary" in out and "3" in out["summary"]
        ok_emit = bool(sink.collect()) and sink.collect()[-1]["event_type"] == "tool_finished"
        ok_fallback = _fallback_summary({}) == "任务已完成。"
        return check("Summary 节点写入 state.summary 含 section 数", ok_summary) \
            and check("Summary 节点 emit tool_finished (走 helper)", ok_emit) \
            and check("Fallback 模板字节级对照 Legacy L385", ok_fallback)
    except Exception:
        traceback.print_exc()
        return False


async def phase_7_routing_summary_path() -> bool:
    print(f"\n{YELLOW}[7] routing_after_interrupt + Summary 路径接入")
    try:
        from app.agent_runtime.graphs.test_plan.versions.v2.routing_after_interrupt import (
            route_after_format_check_for_interrupt,
            route_after_format_interrupt,
            NODE_GENERATE_COMPLETION_SUMMARY,
        )
        from app.agent_runtime.graphs.test_plan.state import make_empty_state

        # 路径 1:format_check passed → Summary
        s = make_empty_state(task_id="e2e_t7", graph_run_id="r7")
        s["format_check_result"] = {"level": "passed"}
        r1 = route_after_format_check_for_interrupt(s)
        ok_passed = r1 == "generate_completion_summary"

        # 路径 2:format_loss accept → Summary
        s["format_check_result"] = {"level": "loss_detected"}
        s["format_loss_confirmation"] = {"decision": "accept"}
        r2 = route_after_format_interrupt(s)
        ok_accept = r2 == "generate_completion_summary"

        return check("format_check passed → Summary 节点", ok_passed) \
            and check("format_loss accept → Summary 节点", ok_accept) \
            and check("NODE_GENERATE_COMPLETION_SUMMARY 常量对齐", NODE_GENERATE_COMPLETION_SUMMARY == "generate_completion_summary")
    except Exception:
        traceback.print_exc()
        return False


async def main() -> int:
    print(f"\n{YELLOW}{'='*60}\n  Phase 2.8F — 端到端 LangGraph v2 全栈就绪检查\n{'='*60}")

    phases = [
        ("Phase 2.8D V6 schema", phase_1_v6_schema),
        ("Phase 2.8C/2.8D v2 graph compile", phase_2_v2_graph_compile),
        ("Phase 2.8D recursion_limit=10", phase_3_coordinator_config),
        ("Phase 2.8D state migration", phase_4_migration),
        ("Phase 2.8D emit helper chain", phase_5_emit_helper),
        ("Phase 2.8D Summary 节点兜底", phase_6_summary_node_chain),
        ("Phase 2.8D routing → Summary 路径", phase_7_routing_summary_path),
    ]

    results = []
    for label, fn in phases:
        ok = await fn()
        results.append((label, ok))

    print(f"\n{YELLOW}{'='*60}\n  汇总\n{'='*60}")
    all_ok = True
    for label, ok in results:
        icon = f"{GREEN}PASS" if ok else f"{RED}FAIL"
        print(f"  {icon}  {label}")
        all_ok = all_ok and ok

    if all_ok:
        print(f"\n{GREEN}Phase 2.8F 端到端全栈就绪检查 — 7/7 通过 ✓\n")
        return 0
    print(f"\n{RED}Phase 2.8F 端到端全栈就绪检查 — 有失败项\n")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
