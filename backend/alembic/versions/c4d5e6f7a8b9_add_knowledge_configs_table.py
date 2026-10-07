"""add_knowledge_configs_table

Revision ID: c4d5e6f7a8b9
Revises: b3c2e4f5a1d0
Create Date: 2026-06-28 11:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b3c2e4f5a1d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS knowledge_configs (
            id BIGINT NOT NULL AUTO_INCREMENT,
            public_id VARCHAR(64) NOT NULL,
            user_id BIGINT NULL,
            scope VARCHAR(32) NOT NULL DEFAULT 'system',
            api_base_url VARCHAR(1024) NOT NULL,
            api_key_encrypted TEXT NULL,
            api_key_masked VARCHAR(64) NULL,
            default_knowledge_ids JSON NULL,
            top_k INT NOT NULL DEFAULT 5,
            similarity_threshold FLOAT NOT NULL DEFAULT 0.35,
            retrieve_strategy INT NOT NULL DEFAULT 3,
            enable_rerank_model TINYINT(1) NOT NULL DEFAULT 1,
            rerank_model VARCHAR(128) NULL DEFAULT 'bge-reranker-v2-m3',
            knowledge_graph TINYINT(1) NOT NULL DEFAULT 0,
            direct_answer_enabled TINYINT(1) NOT NULL DEFAULT 1,
            test_plan_generation_enabled TINYINT(1) NOT NULL DEFAULT 1,
            test_case_generation_enabled TINYINT(1) NOT NULL DEFAULT 1,
            timeout_seconds INT NOT NULL DEFAULT 30,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            last_test_status VARCHAR(32) NULL,
            last_test_message TEXT NULL,
            last_test_at DATETIME NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_knowledge_configs_public_id (public_id),
            KEY idx_knowledge_configs_user_status_updated (user_id, status, updated_at),
            CONSTRAINT fk_knowledge_configs_user
                FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE SET NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS knowledge_configs")