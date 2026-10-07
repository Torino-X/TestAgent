"""add_agent_event_idempotency_unique

Phase 2.8R-E 验收六:agent_events.idempotency_key 由 INDEX 升级为 UNIQUE。

约束名:``uq_agent_events_idempotency_key``
  - 历史行 NULL NULL → MySQL InnoDB 视为不冲突(允许多行 NULL)
  - 新写必须保证 idempotency_key 唯一(由 LiveAgentEventSink 调用方负责)

不直接 DROP idx + ADD UNIQUE;Alembic 路径:drop index 后 add unique
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c2_8r_e_event_idemp_uniq"
down_revision: Union[str, None] = "b2_8r_e_idempotency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Phase 2.8R-E: idempotency_key UNIQUE 升级。"""
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )

    # Drop existing INDEX
    op.execute(
        "ALTER TABLE agent_events DROP INDEX idx_agent_events_idemp, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )

    # Add UNIQUE INDEX with explicit name
    op.execute(
        "ALTER TABLE agent_events "
        "ADD UNIQUE INDEX uq_agent_events_idempotency_key (idempotency_key), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )


def downgrade() -> None:
    """Reverse: drop UNIQUE → 还原 INDEX。"""
    op.execute(
        "SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')"
    )
    op.execute(
        "ALTER TABLE agent_events DROP INDEX uq_agent_events_idempotency_key, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
    op.execute(
        "ALTER TABLE agent_events "
        "ADD INDEX idx_agent_events_idemp (idempotency_key), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
