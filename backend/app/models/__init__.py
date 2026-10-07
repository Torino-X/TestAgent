"""ORM models — all database tables for TestAgent Phase 1."""

from app.models.user import User
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.message_feedback import AssistantMessageFeedback
from app.models.message_generation import AssistantMessageGeneration
from app.models.uploaded_file import UploadedFile
from app.models.template_asset import TemplateAsset
from app.models.template_version import TemplateVersion
from app.models.user_template import UserTemplate
from app.models.agent_task import AgentTask
from app.models.agent_event import AgentEvent
from app.models.agent_run import AgentRun
from app.models.agent_context_snapshot import AgentContextSnapshot
from app.models.agent_execution_request import AgentExecutionRequest
from app.models.tool_call import ToolCall
from app.models.human_confirmation import HumanConfirmation
from app.models.artifact import Artifact
from app.models.document_preview import DocumentPreview
from app.models.project import Project, ProjectKnowledgeBinding, ProjectSource
from app.models.config import ModelConfig, KnowledgeBaseConfig, SystemConfig
from app.models.conversation_summary import ConversationSummary
from app.models.context_snapshot import ContextSnapshot
from app.models.knowledge_config import KnowledgeConfig
from app.models.image_understanding_config import ImageUnderstandingConfig
from app.models.context_engine import (
    ContextCompactionRun,
    ConversationEvidenceAudit,
    ConversationEvidenceCache,
    ConversationContextLedger,
    ContextIndexChunk,
    ContextIndexDocument,
    ContextIndexJob,
    ContextMemory,
    ContextMemorySource,
    ContextPayload,
    ContextRetrievalCandidate,
    ContextRetrievalRun,
    ContextWorkspaceInstruction,
)
from app.models.attachment_understanding import (
    FileSemanticProfile,
    MessageAttachment,
    TaskFileBinding,
)

__all__ = [
    "User",
    "Conversation",
    "Message",
    "AssistantMessageFeedback",
    "AssistantMessageGeneration",
    "UploadedFile",
    "TemplateAsset",
    "TemplateVersion",
    "UserTemplate",
    "AgentTask",
    "AgentEvent",
    "AgentRun",
    "AgentContextSnapshot",
    "AgentExecutionRequest",
    "ToolCall",
    "HumanConfirmation",
    "Artifact",
    "DocumentPreview",
    "Project",
    "ProjectSource",
    "ProjectKnowledgeBinding",
    "ModelConfig",
    "KnowledgeBaseConfig",
    "KnowledgeConfig",
    "ImageUnderstandingConfig",
    "SystemConfig",
    "ConversationSummary",
    "ContextSnapshot",
    "ContextCompactionRun",
    "ConversationEvidenceAudit",
    "ConversationEvidenceCache",
    "ConversationContextLedger",
    "ContextIndexChunk",
    "ContextIndexDocument",
    "ContextIndexJob",
    "ContextMemory",
    "ContextMemorySource",
    "ContextPayload",
    "ContextRetrievalCandidate",
    "ContextRetrievalRun",
    "ContextWorkspaceInstruction",
    "FileSemanticProfile",
    "MessageAttachment",
    "TaskFileBinding",
]


# 模块定位:ORM 模型总目录(Phase 1 全表导出)
#
# 列出所有实体模型,SQLAlchemy metadata 自动收集;
# 上层 services 通过对应 repositories 操作这些表。
#
# 各表语义(从命名分):
#   - 业务核心:AgentTask / AgentRun / AgentEvent / AgentExecutionRequest
#   - 内容:Message / Conversation / Artifact / Attachment / UploadedFile
#   - 配置:User / KnowledgeConfig / ImageUnderstandingConfig / Config
#   - 上下文引擎:ContextEngine(10 张基础表)
#   - 反馈 / 生成:MessageFeedback / MessageGeneration / ConversationSummary
#   - 人为介入:HumanConfirmation / ToolCall / AgentContextSnapshot
#
# 关键约束:
#   - 表名与 Migration (alembic/versions) 必须保持一致;
#   - 不加 cross-table foreign key 误连(每个 repo 自己 join);
#   - 阅读建议:按 chat 主路径 → Agent Task → 配置,逐层读。
