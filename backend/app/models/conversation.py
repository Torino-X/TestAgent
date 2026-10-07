"""Conversation model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    project_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("projects.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), default="新会话")
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="active")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime)

    # ── CE-01: Context Engine 扩展字段 ──────────────────────────────
    context_workspace_key: Mapped[str | None] = mapped_column(String(191))
    context_memory_mode: Mapped[str] = mapped_column(String(32), default="inherit")
    knowledge_mode: Mapped[str] = mapped_column(String(32), default="AUTO")
    context_engine_version: Mapped[str | None] = mapped_column(String(32))

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    owner = relationship("User", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", order_by="Message.created_at")
    files = relationship("UploadedFile", back_populates="conversation")
    agent_tasks = relationship("AgentTask", back_populates="conversation")

    __table_args__ = (
        Index("ix_conversations_user_project_updated", "user_id", "project_id", "updated_at"),
    )


# 模块定位:Conversation 模型(用户会话容器)
#
# 字段:
#   - id / public_id / user_internal_id(FK users)
#   - title / subtitle / state(empty / running / closed)
#   - message_count / file_count(冗余字段,触发器维护)
#   - latest_task_id / latest_message_id(快速定位)
#
# 链路:
#   ConversationService.create / list / get / update / delete
#     → Conversation ORM 行读写
#
# 关键约束:
#   - user_internal_id 必须 + 复合索引(user_id, updated_at DESC) 用于历史侧栏;
#   - soft_delete(archived_at 时间戳),不物理删除;
#   - state 字段直接对应前端 UI 显示(空 / 进行中 / 已完成);
#   - 不要在这表存消息体(message_count 是冗余但实用)。
