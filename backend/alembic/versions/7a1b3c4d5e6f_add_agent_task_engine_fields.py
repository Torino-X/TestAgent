"""add_agent_task_engine_fields

Revision ID: 7a1b3c4d5e6f
Revises: 4d22c4955510
Create Date: 2026-07-16 23:45:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7a1b3c4d5e6f"
down_revision: Union[str, None] = "4d22c4955510"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 2.0 — LangGraph 基础设施。

    在 ``agent_tasks`` 上新增 6 个字段,支持双引擎路由。所有字段均可空,
    默认 ``engine_type='legacy'``,确保历史记录 (50+ 行) 兼容:
      * ``engine_type IS NULL`` 由业务层 ``as_legacy_engine()`` 解释为 legacy
      * ``graph_name / graph_version / thread_id / active_run_id`` 留空直至
        Phase 2.1 真实 graph run 出现
      * ``runtime_status`` 区分 LangGraph run 内部生命周期 (``invoking`` /
        ``awaiting_resume``),不影响 ``status`` 字段语义

    决策记录:
      * ``active_run_id`` Phase 2.0 留为 ``VARCHAR(64) NULL``;Phase 2.1
        引入 ``agent_runs`` 表 + 外键时再升级。
      * 不加 UNIQUE,不引入外键,避免锁表风险。每列单独 ALTER,
        MySQL 8 在 nullable 列上使用 INPLACE + INSTANT 路径。
      * ``engine_type`` 写入策略:仓库层 ``create()`` 在缺失时填 'legacy',
        业务层读取 ``as_legacy_engine()`` 统一 fallback。
      * 同 4d22c4955510: ``agent_tasks`` 表上 ``NO_ZERO_DATE`` 模式下
        ALTER 会触发 1067 错误;这里临时清掉 NO_ZERO_DATE,server sql_mode
        不受影响。
    """
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )
    nullable_adds = [
        ("engine_type", "VARCHAR(32) NULL"),
        ("graph_name", "VARCHAR(64) NULL"),
        ("graph_version", "VARCHAR(32) NULL"),
        ("thread_id", "VARCHAR(128) NULL"),
        ("active_run_id", "VARCHAR(64) NULL"),
        ("runtime_status", "VARCHAR(32) NULL"),
    ]
    for col, decl in nullable_adds:
        op.execute(
            f"ALTER TABLE agent_tasks ADD COLUMN {col} {decl}, ALGORITHM=INSTANT"
        )

    # Backfill: 历史记录的 engine_type 标记为 'legacy',避免后续业务层
    # 反复依赖 NULL 解释。
    op.execute(
        "UPDATE agent_tasks "
        "SET engine_type = 'legacy' "
        "WHERE engine_type IS NULL"
    )


def downgrade() -> None:
    """Reverse: drop the 6 Phase 2.0 columns.

    注意:downgrade 后 engine_type 数据全部丢失。再次 upgrade 会重新 backfill
    为 'legacy',语义一致。
    """
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )
    drop_cols = [
        "runtime_status",
        "active_run_id",
        "thread_id",
        "graph_version",
        "graph_name",
        "engine_type",
    ]
    for col in drop_cols:
        # DROP COLUMN does not support ALGORITHM=INSTANT on MySQL 8;
        # INPLACE is the right non-blocking choice for nullable columns.
        op.execute(
            f"ALTER TABLE agent_tasks DROP COLUMN {col}, ALGORITHM=INPLACE, LOCK=NONE"
        )