"""ce_004_compression

Compression：扩展 conversation_summaries，创建 context_compaction_runs。

conversation_summaries 新增结构化压缩、恢复和版本信息（保留现有字段）。
注意: status 列已存在(默认 active)，不重复新增。

使用 alembic op 操作，确保 alembic_version 正确推进。

Revision ID: 1b640574c715
Revises: 91fcfdb0376e
Create Date: 2026-08-04 22:53:45.860135
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1b640574c715'
down_revision: Union[str, None] = '91fcfdb0376e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ascii_bin(length: int) -> sa.String:
    return sa.String(length=length, collation="ascii_bin")


def upgrade() -> None:
    # ── 1. 扩展 conversation_summaries ──────────────────────────────
    op.add_column("conversation_summaries", sa.Column("summary_type", sa.String(length=32), nullable=False, server_default=sa.text("'conversation'")))
    op.add_column("conversation_summaries", sa.Column("schema_version", sa.String(length=32), nullable=False, server_default=sa.text("'v1'")))
    op.add_column("conversation_summaries", sa.Column("protected_anchors_json", sa.JSON(), nullable=True))
    op.add_column("conversation_summaries", sa.Column("source_refs_json", sa.JSON(), nullable=True))
    op.add_column("conversation_summaries", sa.Column("recovery_mode", sa.String(length=32), nullable=False, server_default=sa.text("'summary_only'")))
    op.add_column("conversation_summaries", sa.Column("recovery_payload_id", sa.BigInteger(), nullable=True))
    op.add_column("conversation_summaries", sa.Column("model_config_id", sa.BigInteger(), nullable=True))
    op.add_column("conversation_summaries", sa.Column("model_name_snapshot", sa.String(length=128), nullable=True))
    op.add_column("conversation_summaries", sa.Column("tokens_before", sa.BigInteger(), nullable=True))
    op.add_column("conversation_summaries", sa.Column("tokens_after", sa.BigInteger(), nullable=True))
    op.add_column("conversation_summaries", sa.Column("compression_ratio", sa.Numeric(10, 6), nullable=True))
    op.add_column("conversation_summaries", sa.Column("source_digest", sa.CHAR(length=64, collation="ascii_bin"), nullable=True))
    op.add_column("conversation_summaries", sa.Column("summary_digest", sa.CHAR(length=64, collation="ascii_bin"), nullable=True))
    op.add_column("conversation_summaries", sa.Column("supersedes_summary_id", sa.BigInteger(), nullable=True))
    op.add_column("conversation_summaries", sa.Column("error_code", sa.String(length=64), nullable=True))
    op.create_index("idx_conversation_summaries_type_status", "conversation_summaries", ["conversation_id", "summary_type", "status"])
    op.create_index("idx_conversation_summaries_recovery", "conversation_summaries", ["recovery_payload_id"])
    op.create_foreign_key("fk_conversation_summary_recovery", "conversation_summaries", "context_payloads", ["recovery_payload_id"], ["id"])
    op.create_foreign_key("fk_conversation_summary_model", "conversation_summaries", "model_configs", ["model_config_id"], ["id"])
    op.create_foreign_key("fk_conversation_summary_supersedes", "conversation_summaries", "conversation_summaries", ["supersedes_summary_id"], ["id"])

    # ── 2. context_compaction_runs ──────────────────────────────────
    op.create_table(
        "context_compaction_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_key", _ascii_bin(191), nullable=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("agent_run_id", sa.BigInteger(), nullable=True),
        sa.Column("context_snapshot_public_id", sa.String(length=64), nullable=True),
        sa.Column("output_summary_public_id", sa.String(length=64), nullable=True),
        sa.Column("recovery_payload_id", sa.BigInteger(), nullable=True),
        sa.Column("call_site", _ascii_bin(128), nullable=False),
        sa.Column("compaction_type", sa.String(length=32), nullable=False),
        sa.Column("trigger_type", sa.String(length=32), nullable=False),
        sa.Column("policy_key", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("model_config_id", sa.BigInteger(), nullable=True),
        sa.Column("model_name_snapshot", sa.String(length=128), nullable=True),
        sa.Column("tokens_before", sa.BigInteger(), nullable=False),
        sa.Column("tokens_after", sa.BigInteger(), nullable=True),
        sa.Column("target_tokens", sa.BigInteger(), nullable=False),
        sa.Column("compression_ratio", sa.Numeric(10, 6), nullable=True),
        sa.Column("protected_anchors_json", sa.JSON(), nullable=False),
        sa.Column("source_refs_json", sa.JSON(), nullable=True),
        sa.Column("dropped_refs_json", sa.JSON(), nullable=True),
        sa.Column("recovery_mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'running'")),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=2000), nullable=True),
        sa.Column("sampling_latency_ms", sa.Integer(), nullable=True),
        sa.Column("total_latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("public_id", name="uq_compaction_runs_public_id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_compaction_runs_user"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_compaction_runs_conversation"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], name="fk_compaction_runs_task"),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_runs.id"], name="fk_compaction_runs_agent_run"),
        sa.ForeignKeyConstraint(["model_config_id"], ["model_configs.id"], name="fk_compaction_runs_model"),
        sa.ForeignKeyConstraint(["recovery_payload_id"], ["context_payloads.id"], name="fk_compaction_runs_payload"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_compaction_runs_task_time", "context_compaction_runs", ["task_id", "created_at"])
    op.create_index("idx_compaction_runs_conversation", "context_compaction_runs", ["conversation_id", "created_at"])
    op.create_index("idx_compaction_runs_call_site", "context_compaction_runs", ["call_site", "created_at"])
    op.create_index("idx_compaction_runs_snapshot", "context_compaction_runs", ["context_snapshot_public_id"])
    op.create_index("idx_compaction_runs_status", "context_compaction_runs", ["status", "created_at"])


def downgrade() -> None:
    op.drop_table("context_compaction_runs")
    op.drop_constraint("fk_conversation_summary_supersedes", "conversation_summaries", type_="foreignkey")
    op.drop_constraint("fk_conversation_summary_model", "conversation_summaries", type_="foreignkey")
    op.drop_constraint("fk_conversation_summary_recovery", "conversation_summaries", type_="foreignkey")
    op.drop_index("idx_conversation_summaries_recovery", table_name="conversation_summaries")
    op.drop_index("idx_conversation_summaries_type_status", table_name="conversation_summaries")
    op.drop_column("conversation_summaries", "error_code")
    op.drop_column("conversation_summaries", "supersedes_summary_id")
    op.drop_column("conversation_summaries", "summary_digest")
    op.drop_column("conversation_summaries", "source_digest")
    op.drop_column("conversation_summaries", "compression_ratio")
    op.drop_column("conversation_summaries", "tokens_after")
    op.drop_column("conversation_summaries", "tokens_before")
    op.drop_column("conversation_summaries", "model_name_snapshot")
    op.drop_column("conversation_summaries", "model_config_id")
    op.drop_column("conversation_summaries", "recovery_payload_id")
    op.drop_column("conversation_summaries", "recovery_mode")
    op.drop_column("conversation_summaries", "source_refs_json")
    op.drop_column("conversation_summaries", "protected_anchors_json")
    op.drop_column("conversation_summaries", "schema_version")
    op.drop_column("conversation_summaries", "summary_type")
