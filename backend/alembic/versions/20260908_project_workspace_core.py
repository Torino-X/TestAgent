"""Add Project workspace core tables and nullable asset relations."""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "20260908_project_core"
down_revision: Union[str, None] = "20260906_phase0_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("memory_mode", sa.String(32), nullable=False, server_default="project_only"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("context_workspace_key", sa.String(191), nullable=False),
        sa.Column("pinned_at", sa.DateTime(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.UniqueConstraint("public_id", name="uq_projects_public_id"),
        sa.UniqueConstraint("context_workspace_key", name="uq_projects_context_workspace_key"),
    )
    op.create_index("ix_projects_user_deleted_updated", "projects", ["user_id", "deleted_at", "updated_at"])
    op.create_index("ix_projects_user_pinned", "projects", ["user_id", "pinned_at"])

    op.add_column("conversations", sa.Column("project_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("fk_conversations_project_id", "conversations", "projects", ["project_id"], ["id"])
    op.create_index("ix_conversations_user_project_updated", "conversations", ["user_id", "project_id", "updated_at"])

    op.add_column("agent_tasks", sa.Column("project_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("fk_agent_tasks_project_id", "agent_tasks", "projects", ["project_id"], ["id"])
    op.create_index("ix_agent_tasks_user_project_created", "agent_tasks", ["user_id", "project_id", "created_at"])

    op.add_column("artifacts", sa.Column("project_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key("fk_artifacts_project_id", "artifacts", "projects", ["project_id"], ["id"])
    op.create_index("ix_artifacts_user_project_created", "artifacts", ["user_id", "project_id", "created_at"])

    op.create_table(
        "project_sources",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_file_id", sa.BigInteger(), nullable=False),
        sa.Column("added_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_role", sa.String(32), nullable=False, server_default="other"),
        sa.Column("source_version", sa.String(64), nullable=True),
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("added_from", sa.String(32), nullable=False, server_default="library"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["uploaded_file_id"], ["uploaded_files.id"]),
        sa.ForeignKeyConstraint(["added_by_user_id"], ["users.id"]),
        sa.UniqueConstraint("public_id", name="uq_project_sources_public_id"),
        sa.UniqueConstraint("project_id", "uploaded_file_id", name="uq_project_sources_project_file"),
    )
    op.create_index("ix_project_sources_project_deleted", "project_sources", ["project_id", "deleted_at"])

    op.create_table(
        "project_knowledge_bindings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("knowledge_id", sa.String(191), nullable=False),
        sa.Column("knowledge_name_snapshot", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.UniqueConstraint("public_id", name="uq_project_knowledge_public_id"),
        sa.UniqueConstraint("project_id", "knowledge_id", name="uq_project_knowledge_project_knowledge"),
    )
    op.create_index("ix_project_knowledge_project_enabled", "project_knowledge_bindings", ["project_id", "enabled"])


def downgrade() -> None:
    op.drop_index("ix_project_knowledge_project_enabled", table_name="project_knowledge_bindings")
    op.drop_table("project_knowledge_bindings")
    op.drop_index("ix_project_sources_project_deleted", table_name="project_sources")
    op.drop_table("project_sources")
    op.drop_index("ix_artifacts_user_project_created", table_name="artifacts")
    op.drop_constraint("fk_artifacts_project_id", "artifacts", type_="foreignkey")
    op.drop_column("artifacts", "project_id")
    op.drop_index("ix_agent_tasks_user_project_created", table_name="agent_tasks")
    op.drop_constraint("fk_agent_tasks_project_id", "agent_tasks", type_="foreignkey")
    op.drop_column("agent_tasks", "project_id")
    op.drop_index("ix_conversations_user_project_updated", table_name="conversations")
    op.drop_constraint("fk_conversations_project_id", "conversations", type_="foreignkey")
    op.drop_column("conversations", "project_id")
    op.drop_index("ix_projects_user_pinned", table_name="projects")
    op.drop_index("ix_projects_user_deleted_updated", table_name="projects")
    op.drop_table("projects")
