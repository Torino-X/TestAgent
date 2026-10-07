"""Schema-level safety contract for the Project migration."""

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.agent_task import AgentTask
from app.models.artifact import Artifact
from app.models.conversation import Conversation
from app.models.project import Project, ProjectKnowledgeBinding, ProjectSource
from app.models.uploaded_file import UploadedFile


def test_project_migration_is_single_head():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["20260908_project_core"]


def test_legacy_asset_project_relations_are_nullable_without_backfill_defaults():
    for model in (Conversation, AgentTask, Artifact):
        column = model.__table__.c.project_id
        assert column.nullable is True
        assert column.default is None
        assert column.server_default is None
        assert next(iter(column.foreign_keys)).target_fullname == "projects.id"


def test_project_relation_tables_keep_files_and_knowledge_separate():
    assert Project.__tablename__ == "projects"
    assert ProjectSource.__table__.c.uploaded_file_id.nullable is False
    assert ProjectKnowledgeBinding.__table__.c.knowledge_id.nullable is False
    assert "project_id" not in {column.name for column in UploadedFile.__table__.columns}
