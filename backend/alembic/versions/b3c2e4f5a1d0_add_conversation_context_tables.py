"""add_conversation_context_tables_and_indexes

Revision ID: b3c2e4f5a1d0
Revises: 1a5ca1cbf787
Create Date: 2026-06-27 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b3c2e4f5a1d0'
down_revision: Union[str, None] = '1a5ca1cbf787'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")

    # ── 1. conversation_summaries table ──────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS conversation_summaries (
            id BIGINT NOT NULL AUTO_INCREMENT,
            public_id VARCHAR(64) NOT NULL,
            user_id BIGINT NOT NULL,
            conversation_id BIGINT NOT NULL,
            summary_text TEXT NOT NULL,
            covered_message_start_id BIGINT NULL,
            covered_message_end_id BIGINT NULL,
            message_count INT NOT NULL DEFAULT 0,
            estimated_tokens INT NOT NULL DEFAULT 0,
            summary_version INT NOT NULL DEFAULT 1,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_conversation_summaries_public_id (public_id),
            KEY idx_summaries_conversation_status_updated (conversation_id, status, updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── 2. llm_context_snapshots table ───────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS llm_context_snapshots (
            id BIGINT NOT NULL AUTO_INCREMENT,
            public_id VARCHAR(64) NOT NULL,
            user_id BIGINT NOT NULL,
            conversation_id BIGINT NULL,
            message_id BIGINT NULL,
            agent_task_id BIGINT NULL,
            llm_task_type VARCHAR(64) NOT NULL,
            context_kind VARCHAR(64) NOT NULL,
            included_message_ids JSON NULL,
            included_file_ids JSON NULL,
            included_task_ids JSON NULL,
            included_artifact_ids JSON NULL,
            included_knowledge_ids JSON NULL,
            summary_id BIGINT NULL,
            estimated_tokens INT NOT NULL DEFAULT 0,
            context_digest VARCHAR(128) NULL,
            context_preview TEXT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_llm_context_snapshots_public_id (public_id),
            KEY idx_context_snapshots_conversation_created (conversation_id, created_at),
            KEY idx_context_snapshots_message (message_id),
            KEY idx_context_snapshots_task (agent_task_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)

    # ── 3. Performance indexes on existing tables ────────────────
    op.execute("""
        CREATE INDEX idx_messages_conversation_role_created
        ON messages (conversation_id, role, created_at)
    """)
    op.execute("""
        CREATE INDEX idx_files_conversation_created
        ON uploaded_files (conversation_id, created_at)
    """)
    op.execute("""
        CREATE INDEX idx_tasks_conversation_created
        ON agent_tasks (conversation_id, created_at)
    """)


def downgrade() -> None:
    op.execute("SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")

    # Drop indexes on existing tables
    op.execute("DROP INDEX idx_tasks_conversation_created ON agent_tasks")
    op.execute("DROP INDEX idx_files_conversation_created ON uploaded_files")
    op.execute("DROP INDEX idx_messages_conversation_role_created ON messages")

    # Drop new tables
    op.execute("DROP TABLE IF EXISTS llm_context_snapshots")
    op.execute("DROP TABLE IF EXISTS conversation_summaries")
