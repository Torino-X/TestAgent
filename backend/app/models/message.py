"""Message model."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False)
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"))
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    message_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # MySQL TEXT is limited to 64 KiB *bytes*, which is insufficient for a
    # legitimate long multilingual user turn before Context Engine gets a
    # chance to compact its prompt.  Keep portable Text elsewhere while using
    # MEDIUMTEXT (16 MiB) for the production MySQL schema.
    content: Mapped[str | None] = mapped_column(Text().with_variant(MEDIUMTEXT(), "mysql"))
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="sent")

    # Phase 2.9A.26+: per-conversation monotonic ordering for stable
    # timeline rendering.  Assigned by the repository at insert time
    # (MAX(sequence)+1 within the same conversation).
    conversation_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Phase 2.9A.26+: FK to the user_text message this agent_text
    # message replies to.  Replaces fragile "most recent user message"
    # inference.
    reply_to_message_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)

    conversation = relationship("Conversation", back_populates="messages")

    __table_args__ = (
        # Stable ordering: list_messages MUST use conversation_sequence
        # ASC; this index keeps that path off the table-scan.
        Index(
            "ix_messages_conversation_sequence",
            "conversation_id",
            "conversation_sequence",
        ),
        # Hard contract: one sequence per conversation.
        UniqueConstraint(
            "conversation_id",
            "conversation_sequence",
            name="uq_messages_conversation_sequence",
        ),
        Index(
            "ix_messages_reply_to_message_id",
            "reply_to_message_id",
        ),
    )


# 模块定位:Message 模型(聊天消息 + 测试方案产物消息)
#
# 字段:
#   - id / public_id / conversation_id / user_internal_id
#   - role(user / assistant / tool_call / system / error / agent_text)
#   - content(message text,可能含 markdown)
#   - message_type / metadata(图片理解 / tool call / 工具产物)
#   - regen_of_message_id(重新生成的原始消息引用)
#
# 链路:
#   MessageService.send_message → 写 user + assistant pair;
#   MessageFeedbackService.record → 写 message_feedback;
#   MessageRegenerationService → 写新 message + regen_of 链
#
# 关键约束:
#   - conversation 必须 + user 复合索引(快速按会话查);
#   - 1 user message + 1 assistant message 配对写入(前端时间线锚定);
#   - regen 不删旧消息,**软替换**(用 hidden=true);
#   - content 上限 64KB 文本(Markdown)。
