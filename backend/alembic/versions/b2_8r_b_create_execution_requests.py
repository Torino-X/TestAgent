"""create_agent_execution_requests_table

Phase 2.8R-B — 任务与 SSE 解耦。

新增 ``agent_execution_requests`` 表(Outbox 队列)。任务创建事务
写一行 ``status=queued``,``AgentExecutionWorker`` 独立线程按
``SELECT ... FOR UPDATE SKIP LOCKED`` 领取 → ``status=running`` →
``ApiDispatcher.dispatch_new_task`` → ``status=completed`` /
``status=failed``。

核心不变式:
  * ``idempotency_key`` UNIQUE — 同一 task 同 request_type 不重复入队
  * ``task_id`` FK → ``agent_tasks.id``,跟随 CASCADE 删除
  * 任务运行不依赖 SSE 客户端连接(SSE 只观察 status + 历史 events)

新增列(由 ORM ``AgentExecutionRequest`` 镜像):
  public_id / request_type / engine_type / graph_name / graph_version /
  payload_json / status / attempt_count / available_at /
  lease_owner / lease_expires_at / started_at / finished_at /
  last_error_code / last_error_message / idempotency_key /
  created_at / updated_at

新增索引:
  UNIQUE (idempotency_key) — at-most-once 入队
  INDEX (status, available_at) — Worker 扫描热点
  INDEX (lease_owner, lease_expires_at) — lease 到期回收
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2_8r_b_create_execution_requests"
down_revision: Union[str, None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 2.8R-B — 新增 agent_execution_requests 表 + 3 索引。"""
    # NO_ZERO_DATE workaround: 与 a8b9c0d1e2f3 沿用相同 pattern
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )

    # ── 新表 ——
    op.create_table(
        "agent_execution_requests",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(), "mysql"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("public_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column(
            "task_id",
            sa.BigInteger().with_variant(sa.dialects.mysql.BIGINT(), "mysql"),
            sa.ForeignKey("agent_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "request_type",
            sa.String(length=32),
            nullable=False,
            server_default="new_task",
        ),
        sa.Column(
            "engine_type",
            sa.String(length=32),
            nullable=False,
            server_default="langgraph",
        ),
        sa.Column("graph_name", sa.String(length=64), nullable=True),
        sa.Column("graph_version", sa.String(length=32), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "available_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("last_error_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
            server_onupdate=sa.func.current_timestamp(),
        ),
    )

    # ── 索引 ——
    # Worker 扫描热路径:(status=queued AND available_at <= NOW())
    op.create_index(
        "idx_agent_execution_request_status_available",
        "agent_execution_requests",
        ["status", "available_at"],
    )

    # Lease 回收扫描
    op.create_index(
        "idx_agent_execution_request_lease",
        "agent_execution_requests",
        ["lease_owner", "lease_expires_at"],
    )

    # idempotency_key 单独 INDEX(UNIQUE 单独约束,不作为复合)
    op.create_index(
        "idx_agent_execution_request_idempotency",
        "agent_execution_requests",
        ["idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    """Reverse: drop 3 indexes + table.

    注意:downgrade 后所有未派发任务丢失。Worker 重启后,业务上
    AgentTask.status 会回退到 "created" 等待手动重新入队。
    """
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )

    op.drop_index(
        "idx_agent_execution_request_idempotency",
        table_name="agent_execution_requests",
    )
    op.drop_index(
        "idx_agent_execution_request_lease",
        table_name="agent_execution_requests",
    )
    op.drop_index(
        "idx_agent_execution_request_status_available",
        table_name="agent_execution_requests",
    )
    op.drop_table("agent_execution_requests")
