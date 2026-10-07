"""agent_events_add_production_fields

Phase 2.6 — 多 Worker 和生产运行时。

在 ``agent_events`` 上加 6 列 + 2 索引,支持 sequence_no 单调分配 / 分布式
事件去重 / 节点级监控 / Last-Event-ID replay。所有字段都是 nullable=True
(``event_schema_version`` 除外),保证历史 50+ 万行零侵入。

新增列:
  * ``sequence_no`` — 单任务单调递增,Redis INCR 快速分配 / DB 兜底
  * ``graph_run_id`` — LangGraph 单次 run 的 public_id(UUID7 字符串)
  * ``graph_version`` — 镜像 ``agent_tasks.graph_version``;事件可追溯
  * ``node_name`` — 节点级监控维度;``GROUP BY node_name`` 直接出报表
  * ``event_schema_version`` — 协议版本,默认 1,NOT NULL
  * ``idempotency_key`` — ``task_id|graph_run_id|node_name|event_type|seq``
    哈希,dedup 入口

新增索引:
  * ``UNIQUE (task_id, sequence_no)`` — 序号不重复,online DDL
  * ``INDEX (idempotency_key)`` — replay 时按 key 查 dup

MySQL 8 + NO_ZERO_DATE 兼容:复用 Phase 2.0 / Phase 2.5 模式,临时清掉
NO_ZERO_DATE,只影响 session sql_mode,server 全局不变。

注意:不引入新表,不动 ``agent_tasks``;事件维度列在本表。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: Union[str, None] = "7a1b3c4d5e6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 2.6 — agent_events 增加 6 列 + 2 索引。"""
    # NO_ZERO_DATE workaround:同 4d22c4955510 / 7a1b3c4d5e6f 模式
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )

    # ── 加 6 列(nullable=True;event_schema_version NOT NULL DEFAULT 1) ──
    # 单独 ALTER 是为了让 MySQL 8 走 INPLACE + INSTANT;一次 ADD 多列在 MySQL
    # 8.0.27+ 才完全 INSTANT,老版本会退化为 INPLACE。
    nullable_adds = [
        ("sequence_no", "BIGINT NULL"),
        ("graph_run_id", "VARCHAR(64) NULL"),
        ("graph_version", "VARCHAR(32) NULL"),
        ("node_name", "VARCHAR(64) NULL"),
        ("idempotency_key", "VARCHAR(160) NULL"),
    ]
    for col, decl in nullable_adds:
        op.execute(
            f"ALTER TABLE agent_events ADD COLUMN {col} {decl}, "
            f"ALGORITHM=INSTANT"
        )

    # event_schema_version 是 NOT NULL DEFAULT 1,NULL → 1 backfill 不需要
    op.execute(
        "ALTER TABLE agent_events "
        "ADD COLUMN event_schema_version INT NOT NULL DEFAULT 1, "
        "ALGORITHM=INSTANT"
    )

    # ── 加 2 索引 ──
    # UNIQUE (task_id, sequence_no):序号单调 + 同任务序号不重复;online DDL
    op.execute(
        "ALTER TABLE agent_events "
        "ADD UNIQUE INDEX uq_agent_events_task_seq (task_id, sequence_no), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )

    # INDEX (idempotency_key):dedup 入口
    op.execute(
        "ALTER TABLE agent_events "
        "ADD INDEX idx_agent_events_idemp (idempotency_key), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )


def downgrade() -> None:
    """Reverse: drop 2 indexes + 6 columns.

    注意:downgrade 后所有 sequence_no / graph_run_id / node_name 数据丢失;
    再次 upgrade 不会 backfill 历史事件的序号,业务层需要容忍 NULL seq
    (Last-Event-ID replay 改用 public_id + created_at)。
    """
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )

    # Drop indexes first (FK drops before tables;indexes don't have FK, but
    # dropping index before column is canonical)
    op.execute(
        "ALTER TABLE agent_events "
        "DROP INDEX idx_agent_events_idemp, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
    op.execute(
        "ALTER TABLE agent_events "
        "DROP INDEX uq_agent_events_task_seq, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )

    # Drop columns in reverse order;event_schema_version NOT NULL → 改 DEFAULT 0
    # 才能安全 DROP;MySQL 8 DROP NOT NULL 限制不强,但保留 DEFAULT 1 → 0 便于回退
    op.execute(
        "ALTER TABLE agent_events "
        "MODIFY COLUMN event_schema_version INT NOT NULL DEFAULT 0, "
        "ALGORITHM=INSTANT"
    )
    op.execute(
        "ALTER TABLE agent_events DROP COLUMN event_schema_version, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )

    drop_cols = [
        "idempotency_key",
        "node_name",
        "graph_version",
        "graph_run_id",
        "sequence_no",
    ]
    for col in drop_cols:
        op.execute(
            f"ALTER TABLE agent_events DROP COLUMN {col}, "
            f"ALGORITHM=INPLACE, LOCK=NONE"
        )
