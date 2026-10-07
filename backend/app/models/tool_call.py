"""ToolCall model — records each tool invocation by the Agent."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, CHAR, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_stage: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    input_summary_json: Mapped[dict | None] = mapped_column(JSON)
    output_summary_json: Mapped[dict | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)

    # ── CE-01: Tool Output 治理字段 ─────────────────────────────────
    context_payload_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("context_payloads.id"))
    output_preview: Mapped[str | None] = mapped_column(Text(length=16777215))
    output_char_count: Mapped[int | None] = mapped_column(BigInteger)
    output_estimated_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    output_truncated: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="0"
    )
    output_policy_key: Mapped[str | None] = mapped_column(String(64))
    output_policy_version: Mapped[str | None] = mapped_column(String(32))
    truncation_metadata_json: Mapped[dict | None] = mapped_column(JSON)

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    task = relationship("AgentTask", back_populates="tool_calls")


# 模块定位:ToolCall 模型(单次工具调用的审计记录)
#
# 字段:
#   - id / task_id / tool_name / tool_call_id(LangGraph 给的)
#   - input(JSON 摘要) / output(摘要,长度受限)
#   - status(running / success / failed / skipped)
#   - attempt / duration_ms
#
# 链路:
#   TestAgentToolAdapter.execute → ToolCallRepository.create
#     → (run 完成) → update status
#
# 关键约束:
#   - input / output 必经过白名单脱敏(no api_key / no storage_key);
#   - status 不允许外部直接写,只许 adapter 内部;
#   - duration_ms 仅在终态写,中间态不写。
