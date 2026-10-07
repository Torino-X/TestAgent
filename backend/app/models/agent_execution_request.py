"""AgentExecutionRequest model — Outbox queue for task execution (Phase 2.8R-B).

任务创建事务 → 写一行 ``status=queued``。
``AgentExecutionWorker`` 按 ``SELECT ... FOR UPDATE SKIP LOCKED`` 领取 →
``status=running`` → ``ApiDispatcher.dispatch_new_task`` → 写
``status=completed`` 或 ``status=failed``。

核心不变式:
  * ``idempotency_key`` UNIQUE:同一 task 同 request_type 不重复入队
  * ``task_id`` FK → ``agent_tasks.id``;CASCADE 跟随 task 删除
  * 任务运行不依赖 SSE 客户端连接(SSE 只观察 status + 历史 events)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AgentExecutionRequest(Base):
    __tablename__ = "agent_execution_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_tasks.id"), nullable=False, index=True
    )
    request_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="new_task"
    )
    engine_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="langgraph"
    )
    graph_name: Mapped[str | None] = mapped_column(String(64))
    graph_version: Mapped[str | None] = mapped_column(String(32))
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="queued", index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64), index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(
        String(160), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    task = relationship("AgentTask")

    __table_args__ = (
        UniqueConstraint(
            "idempotency_key",
            name="uq_agent_execution_request_idempotency",
        ),
        Index(
            "idx_agent_execution_request_status_available",
            "status",
            "available_at",
        ),
        Index(
            "idx_agent_execution_request_lease",
            "lease_owner",
            "lease_expires_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug
        return (
            f"<AgentExecutionRequest id={self.id} public_id={self.public_id!s} "
            f"task_id={self.task_id} status={self.status} request_type={self.request_type}>"
        )
