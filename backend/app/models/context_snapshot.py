"""LLM context snapshot model — lightweight audit records for context construction."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CHAR, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ContextSnapshot(Base):
    __tablename__ = "llm_context_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    agent_task_id: Mapped[int | None] = mapped_column(BigInteger)
    llm_task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    context_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    included_message_ids: Mapped[list | None] = mapped_column(JSON)
    included_file_ids: Mapped[list | None] = mapped_column(JSON)
    included_task_ids: Mapped[list | None] = mapped_column(JSON)
    included_artifact_ids: Mapped[list | None] = mapped_column(JSON)
    included_knowledge_ids: Mapped[list | None] = mapped_column(JSON)
    summary_id: Mapped[int | None] = mapped_column(BigInteger)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0)
    context_digest: Mapped[str | None] = mapped_column(String(128))
    context_preview: Mapped[str | None] = mapped_column(Text)

    # ── CE-01: Context Engine 审计扩展字段（对应 ce_001_foundation）──
    engine_version: Mapped[str | None] = mapped_column(String(32))
    call_site: Mapped[str | None] = mapped_column(String(128))
    context_profile_key: Mapped[str | None] = mapped_column(String(128))
    context_profile_version: Mapped[str | None] = mapped_column(String(32))
    context_policy_version: Mapped[str | None] = mapped_column(String(32))
    model_config_id: Mapped[int | None] = mapped_column(BigInteger)
    model_name_snapshot: Mapped[str | None] = mapped_column(String(128))
    context_window_tokens: Mapped[int | None] = mapped_column(BigInteger)
    input_budget_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_reserve_tokens: Mapped[int | None] = mapped_column(BigInteger)
    runtime_reserve_tokens: Mapped[int | None] = mapped_column(BigInteger)
    safety_margin_tokens: Mapped[int | None] = mapped_column(BigInteger)
    target_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    estimated_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    actual_input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    actual_output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    section_stats_json: Mapped[dict | None] = mapped_column(JSON)
    included_refs_json: Mapped[dict | None] = mapped_column(JSON)
    dropped_refs_json: Mapped[dict | None] = mapped_column(JSON)
    retrieval_run_ids_json: Mapped[dict | None] = mapped_column(JSON)
    compaction_json: Mapped[dict | None] = mapped_column(JSON)
    tool_output_json: Mapped[dict | None] = mapped_column(JSON)
    fallback_json: Mapped[dict | None] = mapped_column(JSON)
    latency_json: Mapped[dict | None] = mapped_column(JSON)
    prompt_digest: Mapped[str | None] = mapped_column(CHAR(64))
    prompt_excerpt: Mapped[str | None] = mapped_column(String(2000))
    full_prompt_payload_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(32), default="building")
    error_code: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


# 模块定位:ContextSnapshot 模型(上下文装配审计,F016)
#
# 字段:
#   - id / conversation_id / llm_call_id
#   - kind(active / repair / interrupt ...)
#   - status(completed / partial)
#   - included_ids(JSON list of selected docs/messages/memories)
#   - short_preview(text 头部 ~200 字符)
#   - estimated_tokens
#
# 链路:
#   ContextSnapshotService.save_snapshot(...)
#   ContextUsageService.get_current_usage → 读最近一条 status=completed & kind=active
#
# 关键约束:
#   - **不**存完整 prompt(只存头部 + 选择的 IDs);
#   - status='partial' 表示装配中途出错(告警用);
#   - estimated_tokens 用 token_estimator 估算,非真值。
