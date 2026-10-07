"""Attachment understanding foundation models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, JSON, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FileSemanticProfile(Base):
    __tablename__ = "file_semantic_profiles"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("uploaded_files.id"), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    document_kind: Mapped[str] = mapped_column(String(64), default="unknown", nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    semantic_labels_json: Mapped[list | None] = mapped_column(JSON)
    possible_usages_json: Mapped[list | None] = mapped_column(JSON)
    characteristics_json: Mapped[dict | None] = mapped_column(JSON)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 5))
    classifier_version: Mapped[str | None] = mapped_column(String(64))
    source_hash: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class MessageAttachment(Base):
    __tablename__ = "message_attachments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    message_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("messages.id"), nullable=False)
    file_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("uploaded_files.id"), nullable=False)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("message_id", "file_id", name="uq_message_attachments_message_file"),
        UniqueConstraint("message_id", "position", name="uq_message_attachments_message_position"),
    )


class TaskFileBinding(Base):
    __tablename__ = "task_file_bindings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"), nullable=False)
    file_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("uploaded_files.id"), nullable=False)
    binding_role: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    binding_source: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Numeric(6, 5))
    metadata_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("task_id", "file_id", "binding_role", name="uq_task_file_bindings_task_file_role"),
    )



# 模块定位:AttachmentUnderstanding 模型(PHASE-1 图片理解结果)
#
# 字段:
#   - id / attachment_id(FK uploaded_files)
#   - provider(vision model 名) / model
#   - caption / tags(JSON list) / ocr_overlay(text)
#   - status(completed / partial / failed)
#   - attempt_count / last_attempt_at
#   - error_message(失败原因)
#
# 链路:
#   FileUnderstandingService.understand_uploaded_file
#     → ImageUnderstandingOrchestrator.understand_one → upsert
#
# 关键约束:
#   - attachment_id 唯一(UNIQUE);
#   - 上限 caption ≈ 2KB / tags ≤ 16 / ocr ≤ 8KB;
#   - 失败 status='failed' + error_message,**不**写 caption(可能误导)。
