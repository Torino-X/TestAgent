"""add_image_understanding_configs_and_supports_vision

Revision ID: f020a1b2c3d4
Revises: 1a5ca1cbf787
Create Date: 2026-07-01 10:00:00.000000

F020 — Image understanding pipeline.

This migration:
  1. Creates ``image_understanding_configs`` mirroring the
     ``knowledge_configs`` schema (per-user row, Fernet-encrypted
     api_key, masked form, last_test_* columns).
  2. Adds ``supports_vision TINYINT(1) NOT NULL DEFAULT 0`` to
     ``model_configs`` so the UI can flag the primary LLM as
     multimodal-capable.  Default is False (legacy text-only configs).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "f020a1b2c3d4"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("SET SESSION sql_mode = 'NO_ENGINE_SUBSTITUTION'")

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS image_understanding_configs (
            id BIGINT NOT NULL AUTO_INCREMENT,
            public_id VARCHAR(64) NOT NULL,
            user_id BIGINT NULL,
            api_base_url VARCHAR(1024) NOT NULL,
            api_key_encrypted TEXT NULL,
            api_key_masked VARCHAR(64) NULL,
            model_name VARCHAR(255) NOT NULL DEFAULT 'qwen-vl-plus',
            timeout_seconds INT NOT NULL DEFAULT 60,
            max_tokens INT NULL,
            enable_in_doc_parsing TINYINT(1) NOT NULL DEFAULT 0,
            status VARCHAR(32) NOT NULL DEFAULT 'active',
            last_test_status VARCHAR(32) NULL,
            last_test_message TEXT NULL,
            last_test_at DATETIME NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (id),
            UNIQUE KEY uq_image_understanding_configs_public_id (public_id),
            KEY idx_image_understanding_configs_user_status_updated (user_id, status, updated_at),
            CONSTRAINT fk_image_understanding_configs_user
                FOREIGN KEY (user_id) REFERENCES users(id)
                ON DELETE SET NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """
    )

    # supports_vision — added to model_configs.  Default 0 keeps every
    # existing row as "text-only LLM"; UI opt-in flips to 1.
    op.execute(
        """
        ALTER TABLE model_configs
            ADD COLUMN supports_vision TINYINT(1) NOT NULL DEFAULT 0
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE model_configs DROP COLUMN IF EXISTS supports_vision")
    op.execute("DROP TABLE IF EXISTS image_understanding_configs")