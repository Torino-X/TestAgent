"""AgentTask model — one task per test-plan generation run."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, JSON, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("projects.id"), nullable=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default="created")
    title: Mapped[str | None] = mapped_column(String(255))
    user_instruction: Mapped[str | None] = mapped_column(Text)
    requirement_file_id: Mapped[int | None] = mapped_column(BigInteger)
    template_file_id: Mapped[int | None] = mapped_column(BigInteger)
    plan_json: Mapped[dict | None] = mapped_column(JSON)
    task_context_json: Mapped[dict | None] = mapped_column(JSON)
    review_result_json: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)

    # Checkpoint fields
    latest_snapshot_id: Mapped[int | None] = mapped_column(BigInteger)
    context_version: Mapped[int] = mapped_column(BigInteger, default=0)
    current_node: Mapped[str | None] = mapped_column(String(64))
    resume_node: Mapped[str | None] = mapped_column(String(64))
    checkpoint_updated_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ── Phase 2.0 LangGraph runtime fields ───────────────────────────
    # Historical rows may contain NULL/legacy; all newly constructed rows default to LangGraph.
    engine_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, default="langgraph"
    )
    graph_name: Mapped[str | None] = mapped_column(String(64))
    graph_version: Mapped[str | None] = mapped_column(String(32))
    thread_id: Mapped[str | None] = mapped_column(String(128))
    # active_run_id is reserved for Phase 2.1 agent_runs table (foreign key target).
    active_run_id: Mapped[str | None] = mapped_column(String(64))
    runtime_status: Mapped[str | None] = mapped_column(String(32))

    # ── CE-01: Context Engine 冻结字段 ──────────────────────────────
    # Task 创建时复制 Conversation 的 Workspace；历史 Task 不因会话改绑而改变。
    context_workspace_key: Mapped[str | None] = mapped_column(String(191))
    context_engine_version: Mapped[str | None] = mapped_column(String(32))

    # Phase 2.9A.26+: FK to the user_text message that triggered
    # this task.  Replaces inference from user_instruction; the
    # front-end timeline uses this to anchor the task run block to
    # the user message that caused it.
    trigger_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="SET NULL")
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    conversation = relationship("Conversation", back_populates="agent_tasks")
    events = relationship("AgentEvent", back_populates="task", order_by="AgentEvent.created_at")
    tool_calls = relationship("ToolCall", back_populates="task")
    human_confirmations = relationship("HumanConfirmation", back_populates="task")
    artifacts = relationship("Artifact", back_populates="task")

    __table_args__ = (
        Index("ix_agent_tasks_user_project_created", "user_id", "project_id", "created_at"),
    )


# 模块定位:AgentTask 模型(测试方案任务主表)
#
# 字段:
#   - id / public_id / conversation_id / user_internal_id
#   - task_type(test_plan_generation)
#   - status(pending / running / waiting_user_confirm / completed / failed / cancelled / format_loss_review)
#   - active_run_id(指向当前 AgentRun)
#   - trigger_message_id(触发本任务的 user message)
#   - started_at / completed_at / ended_at / duration_ms
#   - task_status / current_phase / current_node
#   - last_error(JSON)
#   - graph_version
#
# 链路:
#   AgentTaskService.create / dispatch / resume / cancel / finalize
#   → AgentTask ORM 读写 + 事务 commit
#
# 关键约束:
#   - 状态机必须由 StateManager 校验,不允许直接 ORM 改写;
#   - trigger_message_id 必须能回溯(用于时间线展示);
#   - last_error 是 dict(error_code, message, detail);
#   - duration_ms 由 ended_at - started_at 算出,**不**业务字段。
