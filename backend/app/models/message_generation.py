"""Phase 2.9A.26+: Assistant message generation version model.

Tracks each regeneration of an assistant message.  One message can have
many generations; the ``is_active`` boolean marks the currently displayed
version.

Schema (Phase 3 — minimal):
  * ``id``            — PK
  * ``public_id``     — ``gen_xxx``  public identifier
  * ``message_id``    — FK → ``messages.id``
  * ``generation_no`` — monotonically increasing per message (1, 2, 3 …)
  * ``status``        — ``pending | running | completed | failed``
  * ``content_markdown`` — the final markdown content on completion
  * ``is_active``     — exactly one generation per message is active
  * ``error_code``    — optional; populated on failure

The UNIQUE constraint on ``(message_id, generation_no)`` enforces
monotonicity.  A ``SELECT FOR UPDATE`` on ``message_id`` serializes
concurrent regenerations on the same message without cross-row locks.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AssistantMessageGeneration(Base):
    __tablename__ = "assistant_message_generations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id"), nullable=False, index=True
    )
    generation_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    content_markdown: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    error_code: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


# 模块定位:MessageGeneration 模型(消息生成审计,Phase 2.8R-C)
#
# 字段:
#   - id / message_id / prompt_hash / response_hash
#   - model_profile(CHAT / TEST_PLAN / SUMMARY 等)
#   - input_tokens / output_tokens / latency_ms
#   - created_at
#
# 链路:
#   LLM 调用后 → MessageGenerationService.record → 写 message_generations
#   audit 用:回看某个 message 是哪个 prompt + 哪个 model + 多少次重试。
#
# 关键约束:
#   - 一对一映射 message_id;
#   - prompt_hash 用稳定 hash(便于跨实例聚合);
#   - 不存 raw prompt / raw response(由 prompt_dump 工具在开发期负责)。
