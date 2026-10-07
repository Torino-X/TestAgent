"""create_agent_runs_table

Phase 2.6 — 多 Worker 和生产运行时。

新建 ``agent_runs`` 表,用于 run 级别监控:
  * status / started_at / finished_at — run 生命周期
  * model_provider / model_name — 记录 LLM 配置来源
  * token_usage_json — Token 成本跟踪 (按 profile 拆分)
  * error_json — 失败原因结构化存储
  * graph_name / graph_version / thread_id — 与 agent_tasks.graph_*
    字段冗余,便于不 JOIN 直接出监控报表

约束:
  * 不动 ``agent_tasks`` 既有 39 列(``active_run_id`` 仍为 VARCHAR 64)
  * FK ``(task_id) REFERENCES agent_tasks(id)``;ON DELETE CASCADE 不加,
    保留历史
  * 索引 ``(task_id, started_at)`` 与 ``(status)`` 给监控 dashboard 用
  * 不引入外键到 users/conversations — agent_tasks 已有

镜像文档 §15.2 spec。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: Union[str, None] = "a8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 2.6 — 新建 agent_runs 表 + 2 索引。"""
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )

    op.create_table(
        "agent_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False, unique=True),
        sa.Column("task_id", sa.BigInteger, nullable=False),
        sa.Column("engine_type", sa.String(32), nullable=False, server_default="langgraph"),
        sa.Column("graph_name", sa.String(64), nullable=True),
        sa.Column("graph_version", sa.String(32), nullable=True),
        sa.Column("thread_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="created"),
        sa.Column("started_at", sa.DateTime, nullable=False),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("model_provider", sa.String(64), nullable=True),
        sa.Column("model_name", sa.String(64), nullable=True),
        sa.Column("token_usage_json", sa.JSON, nullable=True),
        sa.Column("error_json", sa.JSON, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], name="fk_agent_runs_task_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    # task_id 单列索引(FK 自动;但复合索引另加)
    op.execute(
        "ALTER TABLE agent_runs "
        "ADD INDEX idx_agent_runs_task_started (task_id, started_at), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
    op.execute(
        "ALTER TABLE agent_runs "
        "ADD INDEX idx_agent_runs_status (status), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )


def downgrade() -> None:
    """Reverse: drop table + indexes.

    注意:agent_runs 数据全部丢失;再次 upgrade 不会 backfill 历史 run。
    agent_tasks.active_run_id 仍存的是旧 public_id 字符串,但表已不存在
    → 业务层访问 agent_runs_public_id_to_run 时应容忍 not-found。
    """
    op.execute(
        "ALTER TABLE agent_runs DROP INDEX idx_agent_runs_status, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
    op.execute(
        "ALTER TABLE agent_runs DROP INDEX idx_agent_runs_task_started, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
    op.drop_table("agent_runs")
