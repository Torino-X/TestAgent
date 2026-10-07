"""AgentEvent model — fine-grained execution events for SSE and recovery.

Phase 2.8R-E: ``idempotency_key`` 由 INDEX 升级为 UNIQUE 约束,阻止双进程并发 emit。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AgentEvent(Base):
    __tablename__ = "agent_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("conversations.id"), nullable=False)
    task_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("agent_tasks.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message_type: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(255))
    content: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="created")

    # Phase 2.6 — sequence_no / 多 worker 实时事件维度
    sequence_no: Mapped[int | None] = mapped_column(BigInteger)
    graph_run_id: Mapped[str | None] = mapped_column(String(64))
    graph_version: Mapped[str | None] = mapped_column(String(32))
    node_name: Mapped[str | None] = mapped_column(String(64))
    event_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str | None] = mapped_column(String(160))

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    task = relationship("AgentTask", back_populates="events")

    # F024-ext follow-up: composite index keeps the SSE-reconnect /
    # history-replay query (``WHERE task_id=? ORDER BY created_at``)
    # out of MySQL's filesort path.  With chunked streaming each tool
    # emits 2-5 rows instead of 1, so a single task can easily blow
    # past the default sort_buffer_size (256 KB).
    __table_args__ = (
        Index("idx_agent_events_task_created", "task_id", "created_at"),
        # Phase 2.6: 单任务 sequence_no 单调 + 不重复
        UniqueConstraint("task_id", "sequence_no", name="uq_agent_events_task_seq"),
        # Phase 2.8R-E: idempotency_key 升级 UNIQUE(原 INDEX 仅去重,未阻止双写)
        # 双进程并发 emit 同一事件时只能 1 行成功;其余 EventIdempotencyConflictError。
        UniqueConstraint(
            "idempotency_key",
            name="uq_agent_events_idempotency_key",
        ),
    )


# 模块定位:AgentEvent 模型(细粒度执行事件 for SSE + recovery)
#
# 字段:
#   - id / public_id / task_id / sequence(单 task 单调递增)
#   - event_type(AgentEventType 枚举)
#   - node_name / execution_id
#   - title / content
#   - payload(JSON;白名单过滤)
#   - idempotency_key **UNIQUE**(Phase 2.8R-E)
#
# 链路:
#   LangGraph 节点 emit → EventRepository.save → SSE 推
#     → 前端 LiveAgentEventBus 订阅 → reducer 归一
#
# 关键约束:
#   - idempotency_key UNIQUE:同 task 双进程并发 emit 自动 fail-fast;
#   - sequence 必须单调增(按 (task_id, sequence) 复合索引);
#   - payload 字段白名单(no api_key / storage_key);
#   - 任何 SSE 必走 EventRepository(直接 INSERT 是反模式)。
