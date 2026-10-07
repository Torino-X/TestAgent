"""Phase 2.9A.26+: Assistant message feedback model.

A user can leave at most one feedback per assistant message.  The
``feedback_type`` is mutually exclusive (``like`` xor ``dislike`` xor
``null``); switching types performs an UPSERT in the same row, not a new
row, so the unique index on ``(user_id, message_id)`` enforces the
"at most one feedback per user/message" rule.

Generation versions are NOT modelled here yet.  When Phase 3 introduces
``assistant_message_generations`` we add ``message_generation_id`` and
relax the unique constraint to ``(user_id, message_id, generation_id)``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AssistantMessageFeedback(Base):
    __tablename__ = "assistant_message_feedbacks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    # public_id is exposed for completeness but the front-end only ever
    # uses (user_id, message_id) as the natural key — feedback rows are
    # not directly fetched by ID.
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False, index=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id"), nullable=False, index=True
    )
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id"), nullable=False, index=True
    )
    feedback_type: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# 模块定位:MessageFeedback 模型(👍 / 👎 + 反馈文本)
#
# 字段:
#   - id / message_id(FK messages) / user_internal_id
#   - feedback_type(thumb_up / thumb_down)
#   - text(可选反馈文本)
#   - UNIQUE(message_id, user_internal_id)
#
# 链路:
#   MessageFeedbackService.record(message_id, user_id, type, text?)
#     → upsert(DB 唯一约束保证幂等)
#
# 关键约束:
#   - 严格 UNIQUE(message_id, user_internal_id) ——
#     1 user × 1 message 只允许一条;
#   - 跨用户访问返回 404,不暴露存在性;
#   - text 上限 1024 字符。
