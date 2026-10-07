"""HumanConfirmation model — user confirmation checkpoints."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class HumanConfirmation(Base):
    __tablename__ = "human_confirmations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"), nullable=False)
    confirmation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    prompt_text: Mapped[str | None] = mapped_column(Text)
    request_json: Mapped[dict | None] = mapped_column(JSON)
    response_json: Mapped[dict | None] = mapped_column(JSON)

    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    task = relationship("AgentTask", back_populates="human_confirmations")


# 模块定位:HumanConfirmation 模型(章节 / 格式损失 二次决策)
#
# 字段:
#   - id / public_id / task_id / kind(section_confirmation / format_loss)
#   - status(pending / confirmed / timeout / cancelled)
#   - sections(JSON:用户决定的章节处理策略)
#   - decision(accept / retry / reject)
#   - timeout_at(超时截止)
#   - created_at / confirmed_at
#
# 链路:
#   LangGraph node → HumanConfirmationRepository.upsert(...)
#   → interrupt → user 决策 → resume() → 写回状态
#
# 关键约束:
#   - sections / decision 字段都是 JSON 列;
#   - 与 SectionConfirmationCard / FormatLossBanner 1:1;
#   - timeout 触发由 ConfirmationTimeoutService 周期扫描;
#   - 不允许跨任务引用 public_id(否则隐私泄露)。
