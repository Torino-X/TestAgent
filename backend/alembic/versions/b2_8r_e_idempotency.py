"""add_artifact_idempotency_fields

Phase 2.8R-E 验收六(Artifact / Event 真幂等):
  * ``artifacts`` 加 4 列(idempotency_key / input_hash / graph_run_id / graph_version)
  * ``artifacts.idempotency_key`` 加 UNIQUE 约束(uq_artifacts_idempotency_key)
  * 历史行 ``idempotency_key IS NULL`` 允许(双 NULL 不冲突 InnoDB 行为)
  * 索引:idx_artifacts_idempotency (若 UNIQUE 已隐含则跳过)

向 MySQL 8 + ``sql_mode`` 兼容:复用 NO_ZERO_DATE workaround。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2_8r_e_idempotency"
down_revision: Union[str, None] = "b2_8r_b_create_execution_requests"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 2.8R-E: artifacts 表加 4 列 + UNIQUE 约束 + 索引。"""
    # NO_ZERO_DATE workaround
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )

    # ── 加 4 列(nullable=True;历史行允许 NULL) ──
    nullable_adds = [
        ("idempotency_key", "VARCHAR(160) NULL"),
        ("input_hash", "VARCHAR(64) NULL"),
        ("graph_run_id", "VARCHAR(64) NULL"),
        ("graph_version", "VARCHAR(32) NULL"),
    ]
    for col, decl in nullable_adds:
        op.execute(
            f"ALTER TABLE artifacts ADD COLUMN {col} {decl}, "
            f"ALGORITHM=INSTANT"
        )

    # ── UNIQUE 约束 ──
    op.execute(
        "ALTER TABLE artifacts "
        "ADD UNIQUE INDEX uq_artifacts_idempotency_key (idempotency_key), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )

    # ── idx_artifacts_input_hash (input_hash 单独 INDEX,用于快速 lookup) ──
    op.execute(
        "ALTER TABLE artifacts "
        "ADD INDEX idx_artifacts_input_hash (input_hash), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )


def downgrade() -> None:
    """Reverse: drop UNIQUE + index + 4 columns."""
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )

    op.execute(
        "ALTER TABLE artifacts DROP INDEX idx_artifacts_input_hash, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
    op.execute(
        "ALTER TABLE artifacts DROP INDEX uq_artifacts_idempotency_key, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )

    drop_cols = [
        "graph_version",
        "graph_run_id",
        "input_hash",
        "idempotency_key",
    ]
    for col in drop_cols:
        op.execute(
            f"ALTER TABLE artifacts DROP COLUMN {col}, "
            f"ALGORITHM=INPLACE, LOCK=NONE"
        )
