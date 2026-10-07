"""CePilotState — Pilot 图的 LangGraph State（JSON 可序列化）。

CE-02 WP-10：thread_id 必须 = task_public_id（复用 require_graph_thread_id）。
所有字段可被 json.dumps 序列化，过 assert_state_serializable。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, TypedDict


class CePilotState(TypedDict, total=False):
    # ── 协议与版本 ────────────────────────────────────────────────────
    state_schema_version: int
    graph_name: str
    graph_version: str

    # ── 身份（均为 public_id / 字符串）────────────────────────────────
    task_public_id: str
    task_id: str
    conversation_id: str
    user_id: str
    graph_run_id: str
    task_internal_id: int
    conversation_internal_id: int
    user_internal_id: int

    # ── 输入 ─────────────────────────────────────────────────────────
    user_prompt: str
    call_site: str

    # ── Context Engine 输出 ───────────────────────────────────────────
    context_state: Optional[Dict[str, Any]]
    active_context_plan: Optional[Dict[str, Any]]
    snapshot_public_id: Optional[str]

    # ── Pilot 业务输出 ────────────────────────────────────────────────
    pilot_summary: Optional[str]
    pilot_token_usage: Optional[Dict[str, int]]
    last_error: Optional[Dict[str, Any]]

    # ── 幂等追踪 ─────────────────────────────────────────────────────
    completed_nodes: list[str]
    node_attempts: Dict[str, int]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (CE-02 WP-10 ContextEngine Pilot 图状态):
#
#   链路:
#     ── 这是一个独立可编译 Pilot 图,与 test_plan (v2/v3) 完全隔离 ──
#
#     GraphRegistry 启动期读 feature_flags,
#       若 pilot_graph_enabled=True → 注册到 GRAPH_REGISTRY (graph_name='ce_pilot');
#       否则不注册(用户切换到这图时 GraphVersionNotAvailableError)。
#
#     任务走 Pilot 图:
#       AgentTaskService.dispatch_new_task(task_type='ce_pilot')
#         → LangGraphRunCoordinator.ainvoke(graph_name='ce_pilot', thread_id=task_id)
#           → ce_pilot.graph: build_pilot_graph(ctx_runtime)
#             → init_node 校验输入 → pilot_invoke_node 走 ContextAwareLLMInvoker
#
# 关键约束(供开发者速查):
#   - thread_id 必须 = task_public_id(由 graph_thread_id 模块强制);
#   - state 字段只承担该图的 stage 信息,**不要**和 test_plan 主图混淆;
#   - Pilot 是隔离实验,不在生产默认图里出现。
