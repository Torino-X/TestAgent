"""Agent context — shared state container passed between orchestration steps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    """Mutable context bag for a single Agent task run.

    Holds references to the task identity, current status, and
    intermediate tool outputs so that tools can pass data downstream.
    When ``session`` is provided, orchestrator methods persist events,
    tool calls, and artifacts to the database automatically.
    """

    task_id: str
    conversation_id: str
    user_id: str
    status: str = "created"

    # DB session for persisting events  (optional — mock runs without it)
    session: Any = None
    task_internal_id: int = 0
    conversation_internal_id: int = 0
    user_internal_id: int = 0

    # SettingsService instance (DB-backed).  Optional; when present,
    # tools resolve LLM config from it instead of from env vars.
    # Not persisted to checkpoint.
    settings_service: Any = None

    # F022: the user's original prompt text for this task.  Populated
    # by the SSE entry point from ``agent_tasks.user_instruction``.
    # ``SectionSuggestionTool`` uses it to extract per-section handling
    # constraints via ``UserConstraintExtractor``.  Empty string means
    # "no user constraint" — the tool falls back to template defaults.
    # Not persisted to checkpoint (it's already in ``agent_tasks.user_instruction``).
    user_prompt: str = ""

    # Task-level persisted trigger context. Dynamic Agent dispatch reads
    # this to restore goal/capability/attachment metadata from the outbox
    # worker path without querying the task row again.
    task_context_json: dict | None = None

    # Optional event sink used by shared tools to publish scoped progress.
    # Graph nodes receive it from RuntimeContext; historical task reads do not
    # require a live sink.
    sink: Any = None

    # CE-05 WP-2: 任务级 Flag Resolver（TaskScopedFeatureFlagResolver）。
    # 任务路径工具经它读 Task-semantic/MIG Flag（冻结 Manifest）；None → 进程级。
    task_flag_resolver: Any = None

    # File references
    requirement_file_id: str | None = None
    template_file_id: str | None = None

    # Intermediate data (populated by tools)
    requirement_analysis: dict | None = None
    template_structure: dict | None = None
    knowledge_search_result: dict | None = None
    section_suggestions: dict | None = None
    section_confirm_config: dict | None = None

    # Checkpoint metadata restored by the active runtime.
    current_node: str | None = None
    resume_node: str | None = None

    # ── Generation output (written by TestPlanGeneratorTool) ─────────
    # ``test_plan_content`` is the canonical container for the generator's
    # complete structured output.  Downstream tools read sub-keys:
    #
    #   test_plan_content["payload"]          — sanitised JSON payload
    #   test_plan_content["section_package"]  — section fill-package (WordExportTool)
    #   test_plan_content["generated_sections"] / ["kept_sections"] / …
    #                                           — summary counts
    #
    # The name ``generated_test_plan`` was proposed in early design but is
    # NOT used — do NOT introduce it as a synonym; it would break consumers
    # that already depend on ``test_plan_content``.
    test_plan_content: dict | None = None
    review_result: dict | None = None
    artifact: dict | None = None

    # F025 — per-task review standard.  Built by the template parser
    # (``template_section_service.build_review_standard``) from the
    # parsed template_structure, then copied to ctx here.  The
    # orchestrator reads this to decide whether to run the review /
    # regen loop, and ``ResultReviewTool`` reads this to evaluate
    # the rule list.  ``None`` means "no standard configured" — the
    # tool falls back to its hardcoded checks and the orchestrator
    # skips the loop.
    review_standard: dict | None = None

    # F025 — last result from ``DocxFormatCheckTool``.  Populated
    # after the docx format-check loop in ``resume_after_confirm``.
    # Not consumed by the orchestrator (it just informs SSE/UI).
    format_check_result: dict | None = None

    # CE-04 §四：ContextInvokerBridge 可选引用（v3 路径经 adapter 透传）。
    # legacy 编排不注入 → None；工具内 MIG flag=true 时若无此引用视为明确错误，
    # 不静默回退 legacy LLMClient。
    context_llm_invoker: Any = None

    # F025-ext — when ``DocxFormatCheckTool`` reports ``level ==
    # "loss_detected"``, the orchestrator pauses and stores the
    # affected losses here so the front-end can render a confirm
    # dialog.  Cleared by ``_resume_after_loss_decision``.
    pending_format_losses: list[dict] | None = None

    # F025-ext — the user's decision for the most recent loss
    # confirmation round.  Shape:
    #   {"decision": "accept" | "retry" | "timeout",
    #    "decided_at": ISO-8601,
    #    "source":    "user" | "timeout_default",
    #    "losses":    [...]}            # the losses the decision applies to
    format_loss_confirmation: dict | None = None

    # F025-ext — how long (seconds) the orchestrator waits for a
    # user response to ``format_loss_confirm_requested`` before
    # defaulting to "accept".  Default 300s (5 minutes).
    format_loss_timeout_seconds: int = 300

    # Events emitted during this run
    events: list[dict] = field(default_factory=list)

    def add_event(self, event_type: str, title: str = "", content: str = "", payload: dict | None = None) -> dict:
        event = {
            "event_type": event_type,
            "title": title,
            "content": content,
            "payload": payload or {},
        }
        self.events.append(event)
        return event

    # F024 — list of tools whose result field has already been populated
    # (e.g. from a snapshot restore on retry).  Used by the orchestrator
    # to skip re-running completed steps.
    #
    # Field names mirror ``context_store._TOOL_FIELD_MAP`` so the result
    # is interchangeable with the snapshot's ``completed_tools`` array.
    def completed_tools_marker(self) -> list[str]:
        marker: list[str] = []
        if self.requirement_analysis is not None:
            marker.append("RequirementParserTool")
        if self.template_structure is not None:
            marker.append("TemplateParserTool")
        if self.knowledge_search_result is not None:
            marker.append("KnowledgeSearchTool")
        if self.section_suggestions is not None:
            marker.append("SectionSuggestionTool")
        return marker


# 模块定位:AgentContext — orchestration 各步骤之间共享的状态容器
#
# 字段样例:
#   user_prompt / requirement_file_id / template_file_id / current_phase
#   / conversation_context / user_constraints / 各 Tool 的输出缓存
#
# 链路:
#   orchestrator.run() 实例化 AgentContext
#   → 各 step 读写 context
#   → 最终落库 conversation_summaries 等
#
# 关键约束:
#   - 不放 AsyncSession / LLMClient(规则 10,序列化失败);
#   - 不放不可序列化对象(asyncio.Task / 文件句柄);
#   - 升级到 LangGraph 时:AgentContext 是过渡,主图用 TestPlanGraphState。
