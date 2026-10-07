"""Conversation summary model — stores LLM-compressed conversation summaries."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CHAR, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ConversationSummary(Base):
    __tablename__ = "conversation_summaries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    summary_text: Mapped[str] = mapped_column(Text, nullable=False)
    covered_message_start_id: Mapped[int | None] = mapped_column(BigInteger)
    covered_message_end_id: Mapped[int | None] = mapped_column(BigInteger)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0)
    summary_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="active")

    # ── CE-01: 结构化压缩/恢复/版本扩展字段（对应 ce_004_compression）──
    summary_type: Mapped[str] = mapped_column(String(32), default="conversation")
    schema_version: Mapped[str] = mapped_column(String(32), default="v1")
    protected_anchors_json: Mapped[dict | None] = mapped_column(JSON)
    source_refs_json: Mapped[dict | None] = mapped_column(JSON)
    recovery_mode: Mapped[str] = mapped_column(String(32), default="summary_only")
    recovery_payload_id: Mapped[int | None] = mapped_column(BigInteger)
    model_config_id: Mapped[int | None] = mapped_column(BigInteger)
    model_name_snapshot: Mapped[str | None] = mapped_column(String(128))
    tokens_before: Mapped[int | None] = mapped_column(BigInteger)
    tokens_after: Mapped[int | None] = mapped_column(BigInteger)
    compression_ratio: Mapped[float | None] = mapped_column(Numeric(10, 6))
    source_digest: Mapped[str | None] = mapped_column(CHAR(64))
    summary_digest: Mapped[str | None] = mapped_column(CHAR(64))
    supersedes_summary_id: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


# 模块定位:ConversationSummary 模型(F016 长会话压缩产物)
#
# 字段:
#   - id / conversation_id / kind(summary / extraction / rewrite)
#   - summary_text(Markdown,token 上限由 token_estimator 估算)
#   - source_message_count / token_count / created_at
#   - is_current(Boolean,只 1 条 is_current=true / conversation)
#
# 链路:
#   ConversationSummaryService.generate(conversation_id) → upsert
#   ConversationContextService.build_chat_context → 读 is_current=true 行
#
# 关键约束:
#   - 同一 conversation 可有多条 summary,但 is_current 仅 1 条;
#   - is_current 翻转必须事务边界内完成;
#   - summary_text 上限(约 2K 字符);
#   - 不要把 message_id 列表塞进来(N+1 风险)。
