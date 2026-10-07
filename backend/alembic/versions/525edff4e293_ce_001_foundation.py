"""ce_001_foundation

Foundation：创建 context_payloads，扩展 conversations / agent_tasks /
tool_calls / llm_context_snapshots / model_configs。

全部新列默认 nullable 或安全默认值（设计文档 §32 CE-001）。
单一主题、不修改已发布 Revision、不调用网络、不导入应用 Service。

使用 alembic op.create_table / op.add_column / op.create_foreign_key 等
SQLAlchemy 级操作（而非裸 op.execute），确保 alembic_version 正确推进。

Revision ID: 525edff4e293
Revises: 2_9a26_message_sequence
Create Date: 2026-08-04 22:53:42.154368
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '525edff4e293'
down_revision: Union[str, None] = '2_9a26_message_sequence'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _ascii_bin(length: int) -> sa.String:
    return sa.String(length=length, collation="ascii_bin")


def upgrade() -> None:
    # ── 1. context_payloads ────────────────────────────────────────
    op.create_table(
        "context_payloads",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("public_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("workspace_key", _ascii_bin(191), nullable=True),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_public_id", sa.String(length=64), nullable=True),
        sa.Column("payload_type", sa.String(length=32), nullable=False),
        sa.Column("storage_backend", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("storage_key_hash", sa.CHAR(length=64, collation="ascii_bin"), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("content_encoding", sa.String(length=32), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("char_count", sa.BigInteger(), nullable=True),
        sa.Column("estimated_tokens", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.CHAR(length=64, collation="ascii_bin"), nullable=False),
        sa.Column("encrypted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("encryption_key_ref", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'active'")),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("public_id", name="uq_context_payloads_public_id"),
        sa.UniqueConstraint("storage_backend", "storage_key_hash", name="uq_context_payloads_storage"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_context_payloads_user"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_context_payloads_conversation"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], name="fk_context_payloads_task"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    op.create_index("idx_context_payloads_owner_type", "context_payloads", ["user_id", "payload_type", "created_at"])
    op.create_index("idx_context_payloads_task", "context_payloads", ["task_id", "payload_type"])
    op.create_index("idx_context_payloads_expiry", "context_payloads", ["status", "expires_at"])

    # ── 2. 扩展 conversations ───────────────────────────────────────
    op.add_column("conversations", sa.Column("context_workspace_key", _ascii_bin(191), nullable=True))
    op.add_column("conversations", sa.Column("context_memory_mode", sa.String(length=32), nullable=False, server_default=sa.text("'inherit'")))
    op.add_column("conversations", sa.Column("context_engine_version", sa.String(length=32), nullable=True))
    op.create_index("idx_conversations_workspace", "conversations", ["user_id", "context_workspace_key"])

    # ── 3. 扩展 agent_tasks ─────────────────────────────────────────
    op.add_column("agent_tasks", sa.Column("context_workspace_key", _ascii_bin(191), nullable=True))
    op.add_column("agent_tasks", sa.Column("context_engine_version", sa.String(length=32), nullable=True))
    op.create_index("idx_agent_tasks_workspace", "agent_tasks", ["user_id", "context_workspace_key"])

    # ── 4. 扩展 tool_calls ──────────────────────────────────────────
    op.add_column("tool_calls", sa.Column("context_payload_id", sa.BigInteger(), nullable=True))
    op.add_column("tool_calls", sa.Column("output_preview", sa.Text(length=16777215).with_variant(sa.Text(), "mysql"), nullable=True))
    op.add_column("tool_calls", sa.Column("output_char_count", sa.BigInteger(), nullable=True))
    op.add_column("tool_calls", sa.Column("output_estimated_tokens", sa.BigInteger(), nullable=True))
    op.add_column("tool_calls", sa.Column("output_sha256", sa.CHAR(length=64, collation="ascii_bin"), nullable=True))
    op.add_column("tool_calls", sa.Column("output_truncated", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.add_column("tool_calls", sa.Column("output_policy_key", sa.String(length=64), nullable=True))
    op.add_column("tool_calls", sa.Column("output_policy_version", sa.String(length=32), nullable=True))
    op.add_column("tool_calls", sa.Column("truncation_metadata_json", sa.JSON(), nullable=True))
    op.create_index("idx_tool_calls_context_payload", "tool_calls", ["context_payload_id"])
    op.create_foreign_key(
        "fk_tool_calls_context_payload", "tool_calls", "context_payloads",
        ["context_payload_id"], ["id"],
    )

    # ── 5. 扩展 llm_context_snapshots ───────────────────────────────
    op.add_column("llm_context_snapshots", sa.Column("engine_version", sa.String(length=32), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("call_site", _ascii_bin(128), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("context_profile_key", _ascii_bin(128), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("context_profile_version", sa.String(length=32), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("context_policy_version", sa.String(length=32), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("model_config_id", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("model_name_snapshot", sa.String(length=128), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("context_window_tokens", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("input_budget_tokens", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("output_reserve_tokens", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("runtime_reserve_tokens", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("safety_margin_tokens", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("target_input_tokens", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("estimated_input_tokens", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("actual_input_tokens", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("actual_output_tokens", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("section_stats_json", sa.JSON(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("included_refs_json", sa.JSON(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("dropped_refs_json", sa.JSON(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("retrieval_run_ids_json", sa.JSON(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("compaction_json", sa.JSON(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("tool_output_json", sa.JSON(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("fallback_json", sa.JSON(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("latency_json", sa.JSON(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("prompt_digest", sa.CHAR(length=64, collation="ascii_bin"), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("prompt_excerpt", sa.String(length=2000), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("full_prompt_payload_id", sa.BigInteger(), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'building'")))
    op.add_column("llm_context_snapshots", sa.Column("error_code", sa.String(length=64), nullable=True))
    op.add_column("llm_context_snapshots", sa.Column("completed_at", sa.DateTime(), nullable=True))
    op.create_index("idx_context_snapshot_call_site", "llm_context_snapshots", ["call_site", "created_at"])
    op.create_index("idx_context_snapshot_profile", "llm_context_snapshots", ["context_profile_key", "created_at"])
    op.create_index("idx_context_snapshot_model", "llm_context_snapshots", ["model_config_id", "created_at"])
    op.create_index("idx_context_snapshot_status", "llm_context_snapshots", ["status", "created_at"])

    # ── 6. 扩展 model_configs ───────────────────────────────────────
    op.add_column("model_configs", sa.Column("capability_type", sa.String(length=32), nullable=False, server_default=sa.text("'chat'")))
    op.add_column("model_configs", sa.Column("context_window_tokens", sa.BigInteger(), nullable=True))
    op.add_column("model_configs", sa.Column("default_max_output_tokens", sa.BigInteger(), nullable=True))
    op.add_column("model_configs", sa.Column("tokenizer_name", sa.String(length=128), nullable=True))
    op.add_column("model_configs", sa.Column("tokenizer_revision", sa.String(length=64), nullable=True))
    op.add_column("model_configs", sa.Column("provider_overhead_tokens", sa.Integer(), nullable=True))
    op.add_column("model_configs", sa.Column("embedding_dimension", sa.Integer(), nullable=True))
    op.add_column("model_configs", sa.Column("normalize_embeddings", sa.Boolean(), nullable=True))
    op.add_column("model_configs", sa.Column("rerank_instruction", sa.String(length=512), nullable=True))
    op.add_column("model_configs", sa.Column("pre_rerank_limit", sa.Integer(), nullable=True))
    op.add_column("model_configs", sa.Column("score_type", sa.String(length=32), nullable=True))
    op.add_column("model_configs", sa.Column("capability_source", sa.String(length=32), nullable=True))
    op.add_column("model_configs", sa.Column("capability_verified_at", sa.DateTime(), nullable=True))
    op.add_column("model_configs", sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")))
    op.create_index("ix_model_configs_capability", "model_configs", ["capability_type", "status"])


def downgrade() -> None:
    op.drop_index("ix_model_configs_capability", table_name="model_configs")
    op.drop_column("model_configs", "enabled")
    op.drop_column("model_configs", "capability_verified_at")
    op.drop_column("model_configs", "capability_source")
    op.drop_column("model_configs", "score_type")
    op.drop_column("model_configs", "pre_rerank_limit")
    op.drop_column("model_configs", "rerank_instruction")
    op.drop_column("model_configs", "normalize_embeddings")
    op.drop_column("model_configs", "embedding_dimension")
    op.drop_column("model_configs", "provider_overhead_tokens")
    op.drop_column("model_configs", "tokenizer_revision")
    op.drop_column("model_configs", "tokenizer_name")
    op.drop_column("model_configs", "default_max_output_tokens")
    op.drop_column("model_configs", "context_window_tokens")
    op.drop_column("model_configs", "capability_type")

    op.drop_index("idx_context_snapshot_status", table_name="llm_context_snapshots")
    op.drop_index("idx_context_snapshot_model", table_name="llm_context_snapshots")
    op.drop_index("idx_context_snapshot_profile", table_name="llm_context_snapshots")
    op.drop_index("idx_context_snapshot_call_site", table_name="llm_context_snapshots")
    op.drop_column("llm_context_snapshots", "completed_at")
    op.drop_column("llm_context_snapshots", "error_code")
    op.drop_column("llm_context_snapshots", "status")
    op.drop_column("llm_context_snapshots", "full_prompt_payload_id")
    op.drop_column("llm_context_snapshots", "prompt_excerpt")
    op.drop_column("llm_context_snapshots", "prompt_digest")
    op.drop_column("llm_context_snapshots", "latency_json")
    op.drop_column("llm_context_snapshots", "fallback_json")
    op.drop_column("llm_context_snapshots", "tool_output_json")
    op.drop_column("llm_context_snapshots", "compaction_json")
    op.drop_column("llm_context_snapshots", "retrieval_run_ids_json")
    op.drop_column("llm_context_snapshots", "dropped_refs_json")
    op.drop_column("llm_context_snapshots", "included_refs_json")
    op.drop_column("llm_context_snapshots", "section_stats_json")
    op.drop_column("llm_context_snapshots", "actual_output_tokens")
    op.drop_column("llm_context_snapshots", "actual_input_tokens")
    op.drop_column("llm_context_snapshots", "estimated_input_tokens")
    op.drop_column("llm_context_snapshots", "target_input_tokens")
    op.drop_column("llm_context_snapshots", "safety_margin_tokens")
    op.drop_column("llm_context_snapshots", "runtime_reserve_tokens")
    op.drop_column("llm_context_snapshots", "output_reserve_tokens")
    op.drop_column("llm_context_snapshots", "input_budget_tokens")
    op.drop_column("llm_context_snapshots", "context_window_tokens")
    op.drop_column("llm_context_snapshots", "model_name_snapshot")
    op.drop_column("llm_context_snapshots", "model_config_id")
    op.drop_column("llm_context_snapshots", "context_policy_version")
    op.drop_column("llm_context_snapshots", "context_profile_version")
    op.drop_column("llm_context_snapshots", "context_profile_key")
    op.drop_column("llm_context_snapshots", "call_site")
    op.drop_column("llm_context_snapshots", "engine_version")

    op.drop_constraint("fk_tool_calls_context_payload", "tool_calls", type_="foreignkey")
    op.drop_index("idx_tool_calls_context_payload", table_name="tool_calls")
    op.drop_column("tool_calls", "truncation_metadata_json")
    op.drop_column("tool_calls", "output_policy_version")
    op.drop_column("tool_calls", "output_policy_key")
    op.drop_column("tool_calls", "output_truncated")
    op.drop_column("tool_calls", "output_sha256")
    op.drop_column("tool_calls", "output_estimated_tokens")
    op.drop_column("tool_calls", "output_char_count")
    op.drop_column("tool_calls", "output_preview")
    op.drop_column("tool_calls", "context_payload_id")

    op.drop_index("idx_agent_tasks_workspace", table_name="agent_tasks")
    op.drop_column("agent_tasks", "context_engine_version")
    op.drop_column("agent_tasks", "context_workspace_key")

    op.drop_index("idx_conversations_workspace", table_name="conversations")
    op.drop_column("conversations", "context_engine_version")
    op.drop_column("conversations", "context_memory_mode")
    op.drop_column("conversations", "context_workspace_key")

    op.drop_table("context_payloads")
