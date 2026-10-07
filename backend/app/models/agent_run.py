"""AgentRun model — run-level monitoring record (Phase 2.6).

每个 LangGraph graph run(``agent_tasks.active_run_id`` 改变一次算一次)对应
一行;legacy path 不写。token_usage_json 按 profile 拆分(例如
``{"by_profile": {"TEST_PLAN": {...}, "REPAIR": {...}}, "total": {...},
"cost_estimate_usd": 0.0012}``)。error_json 是结构化失败原因,任一字段
不可信外露。

字段与文档 §15.2 完全对齐;Phase 2.6 范围不引入 ``run_attempts`` /
``parent_run_id`` 等 Phase 2.7+ 字段。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    task_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agent_tasks.id"), nullable=False, index=True
    )
    engine_type: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="langgraph"
    )
    graph_name: Mapped[str | None] = mapped_column(String(64))
    graph_version: Mapped[str | None] = mapped_column(String(32))
    thread_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="created")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    model_provider: Mapped[str | None] = mapped_column(String(64))
    model_name: Mapped[str | None] = mapped_column(String(64))
    token_usage_json: Mapped[dict | None] = mapped_column(JSON)
    error_json: Mapped[dict | None] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Mirror task.back_populates relationship agent_tasks.runs.
    # NOTE: agent_task.py may not define 'runs' relation yet. We don't add a
    # backref here to keep Phase 2.6 surgical — AgentTask.runs is optional.
    task = relationship("AgentTask")

    __table_args__ = (
        Index("idx_agent_runs_task_started", "task_id", "started_at"),
        Index("idx_agent_runs_status", "status"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debug
        return (
            f"<AgentRun id={self.id} public_id={self.public_id!s} "
            f"task_id={self.task_id} status={self.status}>"
        )


# 模块定位:AgentRun 模型(run-level 监控记录,Phase 2.6)
#
# 每个 LangGraph graph run(``agent_tasks.active_run_id`` 改变一次)对应一行;
# legacy path 不写。token_usage_json 按 profile 拆分。
#
# 字段:
#   - id / task_id(FK agent_tasks)
#   - run_index / started_at / ended_at / status
#   - token_usage_json({by_profile: {CHAT:..., TEST_PLAN:..., ...}, total})
#   - final_artifacts(JSON list)
#
# 链路:
#   LangGraphRunCoordinator 启动期 → bind_agent_run(task_id, run_id)
#   LangGraphRunLifecycle → 终态 / fail 时 update
#
# 关键约束:
#   - token_usage 按 profile 累计,跨 run 累加;
#   - final_artifacts 必须含 public_id,不含 storage_path;
#   - run_index 在 task_id 内单调增。
