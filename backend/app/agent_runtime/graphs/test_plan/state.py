"""TestPlanGraphState - LangGraph 节点间传递的可序列化 dict。

设计约束 (规则 10 + ADR-2.0-6 / ADR-2.1-1):
* ``TypedDict, total=False`` —— 允许节点部分更新
* 所有字段必须可被 ``json.dumps`` 序列化,不允许 Session / Client / Task
* 节点若要 emit 不可序列化副作用,请通过 ``RuntimeContext`` 取得

Phase 2.1 扩展:
* 镜像 ``AgentContext``(Legacy)的关键字段,保持行为等价
* 新增 LangGraph 运行控制字段(current_phase / pause_marker / loop counters)
* 状态 schema_version 从 1 升到 2(v1 stub 仍为 1)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict


class TestPlanGraphState(TypedDict, total=False):
    # ── 协议与版本 ────────────────────────────────────────────────────
    state_schema_version: int        # 2 (Phase 2.1);v1 stub 仍为 1
    graph_name: str                  # "test_plan_generation"
    graph_version: str               # "v2"
    engine_type: str                 # "langgraph"

    # ── 身份 (均为 public_id / 字符串) ────────────────────────────────
    task_id: str
    conversation_id: str
    user_id: str
    graph_run_id: str
    # Internal database identities rebuild RuntimeContext after an interrupt.
    task_internal_id: int
    conversation_internal_id: int
    user_internal_id: int
    # Phase 4: optional Project identity/context snapshot. Both remain JSON-safe.
    project_id: Optional[str]
    project_context: Optional[Dict[str, Any]]

    # ── 输入 ─────────────────────────────────────────────────────────
    user_prompt: str
    requirement_file_id: Optional[str]
    template_file_id: Optional[str]

    # ── 运行状态 ─────────────────────────────────────────────────────
    task_status: str
    current_node: Optional[str]
    current_phase: Optional[str]
    last_error: Optional[Dict[str, Any]]

    # ── 幂等追踪 ─────────────────────────────────────────────────────
    completed_nodes: List[str]
    node_attempts: Dict[str, int]

    # ── Phase 2.1: 业务字段 (镜像 AgentContext) ─────────────────────
    requirement_analysis: Optional[Dict[str, Any]]
    template_structure: Optional[Dict[str, Any]]
    knowledge_search_result: Optional[Dict[str, Any]]
    # Frozen, source-labelled evidence gathered for this task before section
    # suggestion / JSON generation. Never stores runtime clients or sessions.
    retrieval_evidence_bundle: Optional[Dict[str, Any]]
    retrieval_plan_snapshot: Optional[Dict[str, Any]]
    retrieval_round: int
    # Canonical queries already sent to the dual-source retrieval stage.
    # This makes a repeated PreparationAgent request terminal instead of
    # issuing the same unavailable-source lookup again.
    retrieval_query_signatures: List[str]
    clarification_cards: Optional[List[Dict[str, Any]]]
    clarification_answers: Optional[Dict[str, Any]]
    clarification_confirmation_id: Optional[str]
    section_suggestions: Optional[Dict[str, Any]]
    section_confirm_config: Optional[Dict[str, Any]]
    # Phase 2.9A.9: 用户确认后的章节清单(从 section_confirm_config.sections 派生),
    # TestPlanGeneratorTool 启动前校验非空;缺失时抛 GENERATION_STATE_MISSING。
    confirmed_sections: Optional[List[Dict[str, Any]]]
    test_plan_content: Optional[Dict[str, Any]]
    review_result: Optional[Dict[str, Any]]
    review_standard: Optional[Dict[str, Any]]
    artifact: Optional[Dict[str, Any]]
    # Phase 2.9A.17: 当前 / 最新一次 format_check 校验的 Artifact public_id,
    # 让 format_check_result 与 artifact 解耦时也能定位校验目标。
    checked_artifact_public_id: Optional[str]
    format_check_result: Optional[Dict[str, Any]]
    pending_format_losses: Optional[List[Dict[str, Any]]]
    format_loss_confirmation: Optional[Dict[str, Any]]
    format_loss_timeout_seconds: int

    # ── Phase 2.1: LangGraph 控制字段 ────────────────────────────────
    pause_marker: Optional[str]          # need_user_confirm / format_loss_review
    review_loop_count: int              # <= MAX_REVIEW_LOOPS
    format_loop_count: int              # <= MAX_FORMAT_LOOPS
    last_retry_strategy: Optional[str]
    kb_skip_reason: Optional[str]

    # ── Phase 2.3: Preparation Agent 动态子图字段 ──────────────────
    preparation_result: Optional[Dict[str, Any]]      # PreparationResult.model_dump()
    preparation_steps: List[Dict[str, Any]]           # 审计(≤500 summary + 12 sig)
    preparation_budget_state: Optional[Dict[str, Any]]  # BudgetState.model_dump()
    preparation_agent_enabled: bool                  # derived from feature flag

    # ── Phase 2.4: Review Repair Agent 动态子图字段 ──────────────────
    repair_result: Optional[Dict[str, Any]]          # RepairResult.model_dump()
    repair_steps: List[Dict[str, Any]]               # 审计(同 prep_steps 结构)
    repair_budget_state: Optional[Dict[str, Any]]    # BudgetState.model_dump()
    repair_agent_enabled: bool                       # derived from feature flag
    locked_section_ids: List[str]                    # 从 section_confirm_config 派生
    repair_loop_count: int                           # repair subgraph 内部循环计数
    repair_fallback_reason: Optional[str]            # 失败原因

    # ── Phase 2.5: Incremental Agent 动态子图字段 ────────────────────
    incremental_intent: Optional[Dict[str, Any]]     # IncrementalIntent.model_dump()
    incremental_result: Optional[Dict[str, Any]]     # IncrementalResult.model_dump()
    incremental_steps: List[Dict[str, Any]]          # 审计(≤500 summary + 12 sig)
    incremental_budget_state: Optional[Dict[str, Any]]  # BudgetState.model_dump()
    incremental_agent_enabled: bool                 # derived from feature flag
    source_artifact_public_id: Optional[str]         # 用户指定的 source artifact
    modification_idempotency_key: Optional[str]      # 客户端生成的幂等 key
    new_artifact_public_id: Optional[str]            # 增量产出的 artifact
    new_artifact_version_no: Optional[int]

    # ── Phase 2.8D: Retry + Summary 闭环字段 ─────────────────────────
    attempt: Dict[str, int]                          # tool_name → attempt_count (2.8D ADR-2)
    last_retry_decision: Dict[str, str]              # tool_name → strategy (2.8D ADR-13/14)
    summary: Optional[str]                           # generate_completion_summary_node 写入 (2.8D ADR-8)

    # ── Phase 2.9B.4: LLM-first Tool 叙事屏障字段 ───────────────────
    pending_narrative: Optional[Dict[str, Any]]      # PendingNarrative.model_dump()
    next_node: Optional[str]                         # tool_narrative_barrier continuation route
    narrative_tool_call_id: Optional[str]            # 已生成叙事的最新 tool_call_id(幂等)
    narrative_completed_logical_keys: List[str]      # {task}:{graph}:{tool_call_id}:{attempt}:v1 已完成
    task_summary_narrative_done: Optional[bool]      # 最终总结是否已完成(幂等)
    task_summary_narrative_result: Optional[Dict[str, Any]]  # LLM/兜底来源审计


__all__ = ["TestPlanGraphState"]


def make_empty_state(
    *, task_id: str, graph_run_id: str, graph_version: str | None = None,
    preparation_agent_enabled: bool = False,
    repair_agent_enabled: bool = False,
    incremental_agent_enabled: bool = False,
    locked_section_ids: Optional[List[str]] = None,
) -> TestPlanGraphState:
    """构造一个最小可用的初始 state(Phase 2.1 全字段默认值)。

    ``graph_version`` 缺省走 ``GRAPH_VERSION_V1`` (Phase 2.0 兼容);Phase 2.1 调用方
    应显式传 ``GRAPH_VERSION_V2`` 进入真业务图。

    Phase 2.3 增量:当 ``preparation_agent_enabled=True`` 时:
    * ``state_schema_version`` 升到 3
    * 新增 ``preparation_result`` / ``preparation_steps`` / ``preparation_budget_state`` 字段
    * ``preparation_agent_enabled`` 字段写入 True

    Phase 2.4 增量:当 ``repair_agent_enabled=True`` 时(必须先有 prep=True):
    * ``state_schema_version`` 升到 4
    * 新增 7 个 repair 字段(repair_result/steps/budget_state/agent_enabled/
      locked_section_ids/repair_loop_count/repair_fallback_reason)

    Phase 2.5 增量:当 ``incremental_agent_enabled=True`` 时(独立路径,不依赖 prep/repair):
    * ``state_schema_version`` 升到 5
    * 新增 9 个 incremental 字段(incremental_intent/result/steps/budget_state/
      agent_enabled/source_artifact_public_id/modification_idempotency_key/
      new_artifact_public_id/new_artifact_version_no)
    """
    from .constants import (
        GRAPH_VERSION_V1, GRAPH_VERSION_V2,
        STATE_SCHEMA_VERSION_V3, STATE_SCHEMA_VERSION_V4, STATE_SCHEMA_VERSION_V5,
        STATE_SCHEMA_VERSION_V6,
    )

    version = graph_version or GRAPH_VERSION_V1
    # Phase 2.5: incremental 是独立路径(用户已有 artifact,不需要 prep/repair 上下文)
    # Phase 2.8D: 所有路径升 V6(retry + summary 字段为通用基础字段)
    if incremental_agent_enabled:
        schema_version = STATE_SCHEMA_VERSION_V6  # 2.8D:incremental 路径升 V6
    elif repair_agent_enabled:
        schema_version = STATE_SCHEMA_VERSION_V6  # 2.8D:repair 路径升 V6
    elif preparation_agent_enabled:
        schema_version = STATE_SCHEMA_VERSION_V6  # 2.8D:prep 路径升 V6
    else:
        schema_version = STATE_SCHEMA_VERSION_V6  # 2.8D:默认基线 V6
    state = TestPlanGraphState(
        state_schema_version=schema_version,
        graph_name="test_plan_generation",
        graph_version=version,
        engine_type="langgraph",
        task_id=task_id,
        conversation_id="",
        user_id="",
        graph_run_id=graph_run_id,
        task_internal_id=0,
        conversation_internal_id=0,
        user_internal_id=0,
        project_id=None,
        project_context=None,
        user_prompt="",
        requirement_file_id=None,
        template_file_id=None,
        task_status="pending",
        current_node=None,
        current_phase=None,
        last_error=None,
        completed_nodes=[],
        node_attempts={},
        requirement_analysis=None,
        template_structure=None,
        knowledge_search_result=None,
        retrieval_evidence_bundle=None,
        retrieval_plan_snapshot=None,
        retrieval_round=0,
        retrieval_query_signatures=[],
        clarification_cards=None,
        clarification_answers=None,
        clarification_confirmation_id=None,
        section_suggestions=None,
        section_confirm_config=None,
        # Phase 2.9A.9: confirmed_sections 初始 None,resume 后由
        # resume_section_confirmation 写入 section_confirm_config.sections
        confirmed_sections=None,
        test_plan_content=None,
        review_result=None,
        review_standard=None,
        artifact=None,
        format_check_result=None,
        pending_format_losses=None,
        format_loss_confirmation=None,
        format_loss_timeout_seconds=300,
        pause_marker=None,
        review_loop_count=0,
        format_loop_count=0,
        last_retry_strategy=None,
        kb_skip_reason=None,
    )
    if preparation_agent_enabled:
        state["preparation_result"] = None
        state["preparation_steps"] = []
        state["preparation_budget_state"] = None
        state["preparation_agent_enabled"] = True
    if repair_agent_enabled:
        state["repair_result"] = None
        state["repair_steps"] = []
        state["repair_budget_state"] = None
        state["repair_agent_enabled"] = True
        state["locked_section_ids"] = list(locked_section_ids or [])
        state["repair_loop_count"] = 0
        state["repair_fallback_reason"] = None
    if incremental_agent_enabled:
        # Phase 2.5: 独立字段集;不强制要求 prep/repair 已开
        state["incremental_intent"] = None
        state["incremental_result"] = None
        state["incremental_steps"] = []
        state["incremental_budget_state"] = None
        state["incremental_agent_enabled"] = True
        state["source_artifact_public_id"] = None
        state["modification_idempotency_key"] = None
        state["new_artifact_public_id"] = None
        state["new_artifact_version_no"] = None
    # Phase 2.8D: retry + summary 通用基础字段,所有路径必含
    state["attempt"] = {}
    state["last_retry_decision"] = {}
    state["summary"] = None
    # Phase 2.9B.4: Tool 叙事屏障字段
    state["pending_narrative"] = None
    state["next_node"] = None
    state["narrative_tool_call_id"] = None
    state["narrative_completed_logical_keys"] = []
    state["task_summary_narrative_done"] = False
    state["task_summary_narrative_result"] = None
    return state


def assert_state_serializable(state: dict, *, path: str = "") -> None:
    """递归校验 state 中所有字段可被 json 序列化。

    用于 ``GraphRuntimeService.ainvoke`` 入口;抛 ``RuntimeContextLeakedIntoState``
    时说明状态污染。
    """
    import json

    def _raise(_obj: object) -> object:
        raise TypeError("not json serializable")

    try:
        json.dumps(state, default=_raise)
    except (TypeError, ValueError) as exc:
        raise RuntimeContextLeakedIntoState(
            f"state contains non-serializable value at {path or '<root>'}: {exc}"
        ) from exc


class RuntimeContextLeakedIntoState(TypeError):
    """RuntimeContext 字段被错误塞进 state。"""
