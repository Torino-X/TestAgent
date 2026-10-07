"""Phase 0 critical indexes for Redis cache project.

Adds indexes that protect the highest-frequency read paths surfaced
by the cache audit:

  - ``conversations(user_id, deleted_at, updated_at, id)`` for the
    sidebar list query (``ConversationService.list_conversations``)
    which previously relied on no dedicated index.
  - ``model_configs(user_id, status, is_default, updated_at)`` for
    per-user active config lookup on every LLM call.
  - ``agent_tasks(status, created_at)`` for backend / SSE polling and
    admin task scanning.
  - ``agent_tasks(user_id, status)`` for per-user task list filtering.
  - ``human_confirmations(status, expired_at)`` for confirmation
    timeout sweeps.
  - ``human_confirmations(task_id, status)`` for per-task
    confirmation state lookup.
  - ``artifacts(task_id, status)`` for ``ArtifactService.list_by_task``
    style queries.

All indexes are non-unique and additive — none conflict with existing
``__table_args__`` / migration indexes (``idx_conversations_workspace``,
``ix_model_configs_capability``, ``idx_tasks_conversation_created``,
``ix_artifacts_library_active_order``, etc.).  The migration is
fully reversible via ``downgrade``.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260906_phase0_indexes"
down_revision: Union[str, None] = "20260905_lib_upload"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # conversations — sidebar list
    op.create_index(
        "ix_conversations_user_deleted_updated",
        "conversations",
        ["user_id", "deleted_at", "updated_at", "id"],
        unique=False,
    )
    # model_configs — per-user active lookup (every LLM call)
    op.create_index(
        "ix_model_configs_user_status_default_updated",
        "model_configs",
        ["user_id", "status", "is_default", "updated_at"],
        unique=False,
    )
    # agent_tasks — status scan + user task list
    op.create_index(
        "ix_agent_tasks_status_created",
        "agent_tasks",
        ["status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_agent_tasks_user_status",
        "agent_tasks",
        ["user_id", "status"],
        unique=False,
    )
    # human_confirmations — timeout sweep + per-task lookup
    op.create_index(
        "ix_human_confirmations_status_expired_at",
        "human_confirmations",
        ["status", "expired_at"],
        unique=False,
    )
    op.create_index(
        "ix_human_confirmations_task_status",
        "human_confirmations",
        ["task_id", "status"],
        unique=False,
    )
    # artifacts — per-task artifact lookup
    op.create_index(
        "ix_artifacts_task_status",
        "artifacts",
        ["task_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_task_status", table_name="artifacts")
    op.drop_index(
        "ix_human_confirmations_task_status", table_name="human_confirmations"
    )
    op.drop_index(
        "ix_human_confirmations_status_expired_at",
        table_name="human_confirmations",
    )
    op.drop_index("ix_agent_tasks_user_status", table_name="agent_tasks")
    op.drop_index("ix_agent_tasks_status_created", table_name="agent_tasks")
    op.drop_index(
        "ix_model_configs_user_status_default_updated",
        table_name="model_configs",
    )
    op.drop_index(
        "ix_conversations_user_deleted_updated", table_name="conversations"
    )
