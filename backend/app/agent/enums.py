"""Agent enums — event types, task status, tool stages."""

from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_USER_CONFIRM = "waiting_user_confirm"
    GENERATING = "generating"
    REVIEWING = "reviewing"
    EXPORTING = "exporting"
    # F025-ext: orchestrator is paused waiting for the user to decide
    # whether to accept a docx format-loss (bookmarks / fields / etc).
    FORMAT_LOSS_REVIEW = "format_loss_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AgentEventType(StrEnum):
    TASK_CREATED = "task_created"
    PLAN_CREATED = "plan_created"
    # Streaming-stage signals that break ``PLAN_CREATED`` into ordered
    # beats: the UI flips the relevant ``basePlanSteps`` entry to
    # ``running`` on _STARTED and to ``done`` on _COMPLETED.  Lets the
    # "理解任务" → "生成执行计划" → "开始执行" sequence play out one
    # step at a time instead of all three appearing at once.
    PLAN_STEP_STARTED = "plan_step_started"
    PLAN_STEP_COMPLETED = "plan_step_completed"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    TOOL_FAILED = "tool_failed"
    # F023: emitted when RetryPolicy decides to retry a failed tool.
    RETRYING = "retrying"
    REQUIREMENT_SUMMARY = "requirement_summary"
    TEMPLATE_SUMMARY = "template_summary"
    KNOWLEDGE_SUMMARY = "knowledge_summary"
    NEED_USER_CONFIRM = "need_user_confirm"
    TASK_WAITING = "task_waiting"
    TASK_RESUMED = "task_resumed"
    GENERATING_STARTED = "generating_started"
    REVIEW_COMPLETED = "review_completed"
    ARTIFACT_CREATED = "artifact_created"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    # F025: review-regen loop and docx format-check loop.
    SECTION_REGENERATING = "section_regenerating"
    DOCX_FORMAT_CHECKED = "docx_format_checked"
    DOCX_FORMAT_REMEDIATING = "docx_format_remediating"
    # F025-ext: emitted when DocxFormatCheckTool detects user-visible
    # losses (bookmarks, fields, hyperlinks, headers, footers).  The
    # orchestrator pauses until the user submits a decision via
    # ``POST /agent/tasks/{task_id}/format-loss-decision``.
    FORMAT_LOSS_CONFIRM_REQUESTED = "format_loss_confirm_requested"
    FORMAT_LOSS_DECISION_RECORDED = "format_loss_decision_recorded"
    FORMAT_LOSS_RESUMING = "format_loss_resuming"
    # Phase 2.3: Preparation Agent dynamic subgraph events. Additive
    # only; existing 26 event names unchanged. Legacy FE ignores
    # unknown events. These events mark the start/end of the dynamic
    # pre-confirm phase, knowledge-base-specific outcomes, fallback
    # to legacy single-shot KB path, and budget exhaustion.
    PREPARATION_STARTED = "preparation_started"
    PREPARATION_COMPLETED = "preparation_completed"
    PREPARATION_FALLBACK = "preparation_fallback"
    PREPARATION_BUDGET_EXHAUSTED = "preparation_budget_exhausted"
    KNOWLEDGE_INSUFFICIENT = "knowledge_insufficient"
    KNOWLEDGE_SKIPPED = "knowledge_skipped"
    # Phase 2.4: Review Repair Agent dynamic subgraph events. Additive
    # only; cumulative event names (32 historical) unchanged.
    REPAIR_STARTED = "repair_started"
    REPAIR_COMPLETED = "repair_completed"
    REPAIR_FALLBACK = "repair_fallback"
    REPAIR_BUDGET_EXHAUSTED = "repair_budget_exhausted"
    REPAIR_REMEDIATION_APPLIED = "repair_remediation_applied"
    REPAIR_SKIPPED = "repair_skipped"
    # Phase 2.5: Incremental Task Agent dynamic subgraph events.
    # Additive only; cumulative event names (38 historical) unchanged.
    INCREMENTAL_STARTED = "incremental_started"
    INCREMENTAL_DECISION_MADE = "incremental_decision_made"
    INCREMENTAL_TOOL_FINISHED = "incremental_tool_finished"
    INCREMENTAL_TOOL_BLOCKED = "incremental_tool_blocked"
    INCREMENTAL_COMPLETED = "incremental_completed"
    INCREMENTAL_FALLBACK = "incremental_fallback"
    # Phase 2.9B: bounded public decision/observation updates.  Additive and
    # feature-gated; older clients can safely ignore these event names.
    AGENT_DECISION_UPDATE = "agent_decision_update"
    AGENT_OBSERVATION_UPDATE = "agent_observation_update"
    # Phase 2.9B.4: LLM-first Tool narrative streaming events. Additive only.
    TOOL_NARRATIVE_STARTED = "tool_narrative_started"
    TOOL_NARRATIVE_DELTA = "tool_narrative_delta"
    TOOL_NARRATIVE_UPDATE = "tool_narrative_update"
    TOOL_NARRATIVE_FAILED = "tool_narrative_failed"
    TOOL_NARRATIVE_FALLBACK = "tool_narrative_fallback"
    # Phase 2.9B.4: final task summary streaming events.
    TASK_SUMMARY_NARRATIVE_STARTED = "task_summary_narrative_started"
    TASK_SUMMARY_NARRATIVE_DELTA = "task_summary_narrative_delta"
    TASK_SUMMARY_NARRATIVE_UPDATE = "task_summary_narrative_update"
    TASK_SUMMARY_NARRATIVE_FAILED = "task_summary_narrative_failed"
    TASK_SUMMARY_NARRATIVE_FALLBACK = "task_summary_narrative_fallback"
    # Phase 2.9A.19: Graph Node 业务领域失败(状态校验/内容空/数据不可用)。
    # Adapter 仍是 tool_failed 的唯一入口;节点级业务失败用 STAGE_FAILED
    # 区分,避免双发同一终态事件。
    STAGE_FAILED = "stage_failed"
    # Phase 2.6: SSE control frames for multi-worker / Last-Event-ID replay.
    # Additive only; cumulative event names (44 historical) unchanged.
    # - SLOW_CONSUMER_DISCONNECTED: SSE consumer too slow → 断开订阅
    # - CANCEL_OBSERVED: 跨 worker 取消到达后由 DistributedCancellationService 广播
    # - TASK_REPLAY_COMPLETE: HistoryDrainer 历史回放结束,准备进入 live
    SLOW_CONSUMER_DISCONNECTED = "sse_slow_consumer_disconnected"
    CANCEL_OBSERVED = "cancel_observed"
    TASK_REPLAY_COMPLETE = "task_replay_complete"


# ── F013: Intent routing ──────────────────────────────────────────


class IntentType(StrEnum):
    """User-intent classification (LLM-driven)."""

    GENERAL_CHAT = "general_chat"
    TEST_PLAN_GENERATION = "test_plan_generation"
    TEST_CASE_GENERATION = "test_case_generation"
    DOCUMENT_QUESTION = "document_question"
    KNOWLEDGE_QUESTION = "knowledge_question"
    RESULT_MODIFICATION = "result_modification"
    PPT_GENERATION = "ppt_generation"
    EXCEL_GENERATION = "excel_generation"
    UNKNOWN = "unknown"


class MessageRoute(StrEnum):
    """How message_service dispatches the user message."""

    CHAT_REPLY = "chat_reply"
    AGENT_TASK = "agent_task"
    ASK_FOR_FILES = "ask_for_files"
    UNSUPPORTED = "unsupported"
    CLARIFY = "clarify"
    EXISTING_TASK_ACTION = "existing_task_action"


# 模块定位:Agent 全局枚举(EventType / TaskStatus / ToolStage 等)
#
# 全项目唯一的 enum 来源:
#   - AgentEventType(TOOL_STARTED / TOOL_FINISHED / TASK_RESUMED / ...)
#   - TaskStatus(pending / running / waiting_user_confirm / completed / ...)
#   - ToolStage(running / success / failed / skipped / timeout)
#   - IntentType(chat_reply / ask_for_files / unsupported / clarify / agent_task)
#
# 链路:
#   - orchestrator / LangGraph 节点 → emit(event_type=AgentEventType.X)
#   - message_service → classify intent by IntentType
#   - IntentRouter → emit ToolStage
#
# 关键约束:
#   - 任何新增 enum 值必须同步更新:
#     * 前端 reducer 期望的字符串;
#     * event_id / task_status 路由白名单;
#   - 不要删 enum 值(只能加 deprecated 注释);
#   - 与 schemas/agent.py Pydantic enum 同步。
