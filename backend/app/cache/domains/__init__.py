"""Domain cache services — 每个业务域一个模块。

每个模块提供一个 ``*Cache`` 类，封装该域的所有 cache key / TTL / 写穿
策略；上层 service 只调 Domain Cache 的方法，不直接接触 Redis。

实现状态（按交接文档 Step 顺序）:
  - auth_cache.py          (Step 4) ✅ → AuthPrincipalCache
  - config_cache.py        (Step 5) ✅ → ModelConfigCache / KnowledgeConfigCache /
                                       ImageUnderstandingConfigCache /
                                       SystemConfigCache
  - task_cache.py          (Step 6) ✅ → TaskStatusCache + Terminal TaskDetailCache
  - conversation_cache.py  (Step 7) ✅ → ConversationCache (List + Detail) +
                                       bump_generation
  - library_cache.py       (Step 8) ✅ → LibraryMetadataCache (list + filter hash)
  - context_cache.py       (Step 8) ✅ → ContextMemoryCache + WorkspaceInstructionCache
  - semantic_profile_cache.py (Step 8) ✅ → FileSemanticProfileCache
                                       (batched MGET, status-specific TTL)
  - feedback_cache.py       (Step 9) ✅ → FeedbackCache
                                       (Business Cache Redis 迁移, 24h TTL)

设计参考 docs/prompt/TestAgent_Redis缓存系统详细技术设计文档.md §10-16
+ docs/prompt/TestAgent_Redis缓存系统_ZCode开发实施提示词.md §16-27。
"""

from __future__ import annotations

from app.cache.domains.auth_cache import (
    AUTH_PRINCIPAL_SPEC,
    AuthPrincipalCache,
    AuthPrincipalDTO,
    auth_principal_key,
    get_auth_principal_cache,
    set_auth_principal_cache,
)
from app.cache.domains.config_cache import (
    IMAGE_UNDERSTANDING_SPEC,
    KNOWLEDGE_CONFIG_SYSTEM_SPEC,
    KNOWLEDGE_CONFIG_USER_SPEC,
    MODEL_CONFIG_SPEC,
    SYSTEM_CONFIG_SPEC,
    ImageUnderstandingConfigCache,
    ImageUnderstandingConfigDTO,
    KnowledgeConfigCache,
    KnowledgeConfigDTO,
    ModelConfigCache,
    ModelConfigDTO,
    SystemConfigCache,
    SystemConfigDTO,
    get_image_understanding_config_cache,
    get_knowledge_config_cache,
    get_model_config_cache,
    get_system_config_cache,
    image_understanding_key,
    knowledge_system_key,
    knowledge_user_key,
    model_config_key,
    set_image_understanding_config_cache,
    set_knowledge_config_cache,
    set_model_config_cache,
    set_system_config_cache,
    system_config_key,
)
from app.cache.domains.task_cache import (
    TASK_STATUS_SPEC,
    TERMINAL_STATUSES,
    TaskDetailCache,
    TaskDetailDTO,
    TaskStatusCache,
    TaskStatusDTO,
    TaskStatusSpec,
    get_task_detail_cache,
    get_task_status_cache,
    set_task_detail_cache,
    set_task_status_cache,
    task_detail_key,
    task_status_key,
)
from app.cache.domains.conversation_cache import (
    CONVERSATION_DETAIL_SPEC,
    CONVERSATION_LIST_SPEC,
    GENERATION_SPEC as CONVERSATION_GENERATION_SPEC,
    ConversationCache,
    ConversationDetailDTO,
    ConversationListDTO,
    conv_detail_key,
    conv_generation_key,
    conv_list_key,
    get_conversation_cache,
    set_conversation_cache,
)
from app.cache.domains.library_cache import (
    GENERATION_SPEC as LIBRARY_GENERATION_SPEC,
    LIBRARY_LIST_SPEC,
    LibraryCache,
    LibraryListDTO,
    filter_hash_for,
    get_library_cache,
    library_generation_key,
    library_list_key,
    set_library_cache,
)
from app.cache.domains.context_cache import (
    CONTEXT_SPEC,
    ContextMemoryCache,
    ContextMemoryDTO,
    WorkspaceInstructionCache,
    WorkspaceInstructionDTO,
    context_memory_key,
    get_context_memory_cache,
    get_workspace_instruction_cache,
    set_context_memory_cache,
    set_workspace_instruction_cache,
    workspace_hash_for,
    workspace_instruction_key,
)
from app.cache.domains.semantic_profile_cache import (
    SEMANTIC_PROFILE_SPEC,
    FileSemanticProfileCache,
    SemanticProfileDTO,
    SemanticProfileSpec,
    get_semantic_profile_cache,
    semantic_profile_key,
    set_semantic_profile_cache,
)
from app.cache.domains.feedback_cache import (
    FEEDBACK_SPEC,
    FeedbackCache,
    FeedbackCacheDTO,
    feedback_key,
    get_feedback_cache,
    set_feedback_cache,
)
from app.cache.domains.project_cache import (
    PROJECT_DETAIL_SPEC,
    PROJECT_LIST_SPEC,
    ProjectCache,
    get_project_cache,
    project_detail_key,
    project_list_filter_hash,
    project_list_key,
    set_project_cache,
)

__all__ = [
    # Auth (Step 4)
    "AUTH_PRINCIPAL_SPEC",
    "AuthPrincipalCache",
    "AuthPrincipalDTO",
    "auth_principal_key",
    "get_auth_principal_cache",
    "set_auth_principal_cache",
    # Config (Step 5)
    "IMAGE_UNDERSTANDING_SPEC",
    "KNOWLEDGE_CONFIG_SYSTEM_SPEC",
    "KNOWLEDGE_CONFIG_USER_SPEC",
    "MODEL_CONFIG_SPEC",
    "SYSTEM_CONFIG_SPEC",
    "ImageUnderstandingConfigCache",
    "ImageUnderstandingConfigDTO",
    "KnowledgeConfigCache",
    "KnowledgeConfigDTO",
    "ModelConfigCache",
    "ModelConfigDTO",
    "SystemConfigCache",
    "SystemConfigDTO",
    "get_image_understanding_config_cache",
    "get_knowledge_config_cache",
    "get_model_config_cache",
    "get_system_config_cache",
    "image_understanding_key",
    "knowledge_system_key",
    "knowledge_user_key",
    "model_config_key",
    "set_image_understanding_config_cache",
    "set_knowledge_config_cache",
    "set_model_config_cache",
    "set_system_config_cache",
    "system_config_key",
    # Task (Step 6)
    "TASK_STATUS_SPEC",
    "TERMINAL_STATUSES",
    "TaskDetailCache",
    "TaskDetailDTO",
    "TaskStatusCache",
    "TaskStatusDTO",
    "TaskStatusSpec",
    "get_task_detail_cache",
    "get_task_status_cache",
    "set_task_detail_cache",
    "set_task_status_cache",
    "task_detail_key",
    "task_status_key",
    # Conversation (Step 7)
    "CONVERSATION_DETAIL_SPEC",
    "CONVERSATION_GENERATION_SPEC",
    "CONVERSATION_LIST_SPEC",
    "ConversationCache",
    "ConversationDetailDTO",
    "ConversationListDTO",
    "conv_detail_key",
    "conv_generation_key",
    "conv_list_key",
    "get_conversation_cache",
    "set_conversation_cache",
    # Library (Step 8)
    "LIBRARY_GENERATION_SPEC",
    "LIBRARY_LIST_SPEC",
    "LibraryCache",
    "LibraryListDTO",
    "filter_hash_for",
    "get_library_cache",
    "library_generation_key",
    "library_list_key",
    "set_library_cache",
    # Context (Step 8)
    "CONTEXT_SPEC",
    "ContextMemoryCache",
    "ContextMemoryDTO",
    "WorkspaceInstructionCache",
    "WorkspaceInstructionDTO",
    "context_memory_key",
    "get_context_memory_cache",
    "get_workspace_instruction_cache",
    "set_context_memory_cache",
    "set_workspace_instruction_cache",
    "workspace_hash_for",
    "workspace_instruction_key",
    # Semantic Profile (Step 8)
    "SEMANTIC_PROFILE_SPEC",
    "FileSemanticProfileCache",
    "SemanticProfileDTO",
    "SemanticProfileSpec",
    "get_semantic_profile_cache",
    "semantic_profile_key",
    "set_semantic_profile_cache",
    # Feedback (Step 9)
    "FEEDBACK_SPEC",
    "FeedbackCache",
    "FeedbackCacheDTO",
    "feedback_key",
    "get_feedback_cache",
    "set_feedback_cache",
    # Project workspace
    "PROJECT_DETAIL_SPEC",
    "PROJECT_LIST_SPEC",
    "ProjectCache",
    "get_project_cache",
    "project_detail_key",
    "project_list_filter_hash",
    "project_list_key",
    "set_project_cache",
]
