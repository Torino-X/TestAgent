"""add_context_snapshots_table_and_checkpoint_fields

Revision ID: 1a5ca1cbf787
Revises: 70a7b52174b2
Create Date: 2026-06-21 18:13:37.823535
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a5ca1cbf787'
down_revision: Union[str, None] = '70a7b52174b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Relax sql_mode to avoid STRICT_TRANS_TABLES rejecting existing
    # DEFAULT (now()) expressions during ALTER TABLE.
    op.execute("SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")

    # Create agent_context_snapshots table
    op.execute("""
        CREATE TABLE IF NOT EXISTS agent_context_snapshots (
            id BIGINT NOT NULL AUTO_INCREMENT,
            public_id VARCHAR(64) NOT NULL,
            task_id BIGINT NOT NULL,
            snapshot_version BIGINT NOT NULL,
            node_name VARCHAR(64) NOT NULL,
            resume_node VARCHAR(64) NULL,
            storage_mode VARCHAR(32) NOT NULL,
            snapshot_json JSON NULL,
            blob_path VARCHAR(512) NULL,
            size_bytes BIGINT NOT NULL DEFAULT 0,
            checksum VARCHAR(128) NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_agent_context_snapshots_public_id (public_id),
            KEY ix_agent_context_snapshots_task_id (task_id),
            CONSTRAINT fk_agent_context_snapshots_task_id
                FOREIGN KEY (task_id) REFERENCES agent_tasks (id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # Add checkpoint columns to agent_tasks
    op.execute("ALTER TABLE agent_tasks ADD COLUMN latest_snapshot_id BIGINT NULL")
    op.execute("ALTER TABLE agent_tasks ADD COLUMN context_version BIGINT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE agent_tasks ADD COLUMN current_node VARCHAR(64) NULL")
    op.execute("ALTER TABLE agent_tasks ADD COLUMN resume_node VARCHAR(64) NULL")
    op.execute("ALTER TABLE agent_tasks ADD COLUMN checkpoint_updated_at DATETIME NULL")


def downgrade() -> None:
    op.execute("SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")
    op.execute("ALTER TABLE agent_tasks DROP COLUMN IF EXISTS checkpoint_updated_at")
    op.execute("ALTER TABLE agent_tasks DROP COLUMN IF EXISTS resume_node")
    op.execute("ALTER TABLE agent_tasks DROP COLUMN IF EXISTS current_node")
    op.execute("ALTER TABLE agent_tasks DROP COLUMN IF EXISTS context_version")
    op.execute("ALTER TABLE agent_tasks DROP COLUMN IF EXISTS latest_snapshot_id")
    op.execute("DROP TABLE IF EXISTS agent_context_snapshots")
