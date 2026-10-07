"""add_idx_agent_events_task_created

Revision ID: 4d22c4955510
Revises: f020a1b2c3d4
Create Date: 2026-07-16 21:09:36.150574
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4d22c4955510'
down_revision: Union[str, None] = 'f020a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # F024-ext follow-up: ``EventRepository.list_by_task`` runs
    # ``WHERE task_id=? ORDER BY created_at ASC LIMIT N`` on every
    # SSE reconnect / history replay.  With the chunked-streaming
    # rollout, each tool emits 2-5 rows instead of 1, and payload_json
    # carries the full public_execution_update body.  MySQL's default
    # sort_buffer_size (256 KB) overflows once a single task crosses
    # ~50 rows — surfaces as pymysql err 1038.
    #
    # Add a composite index so the query becomes an index range scan
    # and never hits filesort.
    #
    # Workaround for MySQL 8.0 + NO_ZERO_DATE bug: ALTER TABLE on a
    # table that has ``DEFAULT (NOW())`` columns fails with 1067
    # "Invalid default value for 'created_at'" even when no row uses
    # a zero date.  We temporarily drop NO_ZERO_DATE for this
    # migration session only — server sql_mode is unaffected.
    op.execute("SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')")
    op.execute(
        "ALTER TABLE agent_events "
        "ADD INDEX idx_agent_events_task_created (task_id, created_at), "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )


def downgrade() -> None:
    op.execute("SET SESSION sql_mode = REPLACE(@@SESSION.sql_mode, 'NO_ZERO_DATE', '')")
    op.execute(
        "ALTER TABLE agent_events "
        "DROP INDEX idx_agent_events_task_created, "
        "ALGORITHM=INPLACE, LOCK=NONE"
    )
