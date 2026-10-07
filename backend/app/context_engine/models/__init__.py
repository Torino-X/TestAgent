"""Context Engine 领域模型（Pydantic v2，不依赖框架）。"""

from app.context_engine.models.enums import (
    CompactionStatus,
    CompressionLevel,
    ContextExecutionMode,
    ContextKind,
    ContextTrust,
    IndexSourceFamily,
    MemoryScope,
    MemoryStatus,
    PayloadMode,
    RerankStrategy,
    RetrievalStrategy,
    SnapshotStatus,
    SourceType,
)
from app.context_engine.models.value_objects import (
    Digest,
    PublicId,
    TokenCount,
    VersionString,
    WorkspaceKey,
    canonical_json,
)
from app.context_engine.models.context import (
    ContextItem,
    ContextPlan,
    ContextRef,
    ContextRequest,
    ContextScope,
    RetrievalQuery,
    SectionPlan,
)
from app.context_engine.models.profile import (
    BudgetPolicy,
    ContextBudget,
    ContextProfile,
    ProfileSectionSpec,
)
from app.context_engine.models.source import (
    ContextWarning,
    LockedSection,
    SourceCollectResult,
)
from app.context_engine.models.selection import (
    DroppedContextRef,
    SelectedContextSet,
)
from app.context_engine.models.compose import (
    ComposeValidation,
    ContextComposeResult,
    ContextMessage,
)
from app.context_engine.models.snapshot_models import (
    ContextBuildLatency,
    ContextSnapshotBeginCommand,
    ContextSnapshotCompleteCommand,
    ContextSnapshotFailCommand,
    ContextSnapshotRef,
    ContextStateRef,
    ContextStateStats,
)
from app.context_engine.models.retry_models import (
    LLMAttemptRef,
    SafeLLMError,
)
from app.context_engine.models.tool_output import (
    ManagedToolOutput,
    ToolOutputPolicy,
    ToolOutputTruncation,
)
from app.context_engine.models.retrieval import (
    RetrievalRequest,
    RetrievalScopeFilter,
)
from app.context_engine.models.payload import (
    ContextPayloadRef,
    PayloadStoreCommand,
)

__all__ = [
    "CompactionStatus",
    "CompressionLevel",
    "ContextExecutionMode",
    "ContextKind",
    "ContextTrust",
    "IndexSourceFamily",
    "MemoryScope",
    "MemoryStatus",
    "PayloadMode",
    "RerankStrategy",
    "RetrievalStrategy",
    "SnapshotStatus",
    "SourceType",
    "Digest",
    "PublicId",
    "TokenCount",
    "VersionString",
    "WorkspaceKey",
    "canonical_json",
    "ContextItem",
    "ContextPlan",
    "ContextRef",
    "ContextRequest",
    "ContextScope",
    "RetrievalQuery",
    "SectionPlan",
    "BudgetPolicy",
    "ContextBudget",
    "ContextProfile",
    "ProfileSectionSpec",
    "ContextWarning",
    "LockedSection",
    "SourceCollectResult",
    "DroppedContextRef",
    "SelectedContextSet",
    "ComposeValidation",
    "ContextComposeResult",
    "ContextMessage",
    "ContextBuildLatency",
    "ContextSnapshotBeginCommand",
    "ContextSnapshotCompleteCommand",
    "ContextSnapshotFailCommand",
    "ContextSnapshotRef",
    "ContextStateRef",
    "ContextStateStats",
    "LLMAttemptRef",
    "SafeLLMError",
    "ManagedToolOutput",
    "ToolOutputPolicy",
    "ToolOutputTruncation",
    "ContextPayloadRef",
    "PayloadStoreCommand",
    "RetrievalRequest",
    "RetrievalScopeFilter",
]
# auto-appended module-level note: context_engine models 子包入口: 跨子包共享 Pydantic 数据模型。
