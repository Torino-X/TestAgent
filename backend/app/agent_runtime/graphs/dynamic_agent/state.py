"""Dynamic Agent graph state contract."""

from __future__ import annotations

from typing import Any, TypedDict


class DynamicAgentState(TypedDict, total=False):
    task_id: str
    task_public_id: str
    graph_run_id: str
    user_id: int
    user_internal_id: int
    task_internal_id: int
    conversation_internal_id: int
    conversation_public_id: str
    trigger_message_id: int
    graph_name: str
    graph_version: str
    goal: str
    dynamic_goal: str
    target_capability: str
    operation: str
    request_understanding_snapshot: dict[str, Any]
    retrieval_plan_snapshot: dict[str, Any]
    knowledge_mode_snapshot: str
    attachment_refs: list[dict[str, Any]]
    plan: dict[str, Any] | None
    plan_revision: int
    current_step_id: str | None
    tool_results: dict[str, Any]
    observations: list[dict[str, Any]]
    analysis_results: dict[str, Any]
    verification_status: str | None
    verification_gaps: list[str]
    replan_count: int
    tool_call_count: int
    awaiting_user: bool
    clarification: dict[str, Any] | None
    pause_marker: str | None
    final_answer: str | None
    failure: dict[str, Any] | None
    current_node: str
    current_phase: str
    task_status: str
    completed_nodes: list[str]


__all__ = ["DynamicAgentState"]


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Dynamic Agent v1 State Contract):
#
#   一个 TypedDict 描述 DynamicAgentState 的全部字段。
#   Phase-1 阶段只有骨架,后续 Phase 加 Planner/Executor/Verifier 时递增字段。
#
# 关键约束(供开发者速查):
#   - TypedDict total=False:节点函数只 partial-update 字段;
#   - 不包含 system_prompt / api_key / 内部文件路径等敏感字段;
#   - thread_id / task_public_id 必填,与 graph_thread_id 模块共享格式;
#   - planner_output / executor_output / verifier_output 是预留占位(后续 phase)。
