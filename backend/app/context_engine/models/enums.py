"""Context Engine 枚举：Context / Memory / Retrieval / Snapshot / Compaction / Payload / Index。

只依赖标准库 Enum，可被 ORM / REST / State 共享。
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """字符串枚举基类：序列化为自身值，便于与 DB 字符串列兼容。"""

    def __str__(self) -> str:
        return self.value


class ContextKind(StrEnum):
    """九类上下文内容（设计文档 §6.1）。"""

    SYSTEM_RULES = "system_rules"
    CALL_CONTRACT = "call_contract"
    CURRENT_GOAL = "current_goal"
    PROJECT_INSTRUCTIONS = "project_instructions"
    TASK_STATE = "task_state"
    EVIDENCE = "evidence"
    KNOWLEDGE = "knowledge"
    MEMORY = "memory"
    CONVERSATION = "conversation"


class ContextTrust(StrEnum):
    """可信度分层（设计文档 §18.1）。"""

    TRUSTED_INSTRUCTION = "trusted_instruction"
    BUSINESS_EVIDENCE = "business_evidence"
    UNTRUSTED_REFERENCE = "untrusted_reference"


class SourceType(StrEnum):
    """ContextItem.source_type 来源分类。"""

    SYSTEM = "system"
    CONVERSATION = "conversation"
    CONVERSATION_SUMMARY = "conversation_summary"
    TASK_STATE = "task_state"
    TASK_TRIGGER = "task_trigger"
    FILE_SUMMARY = "file_summary"
    PARSED_DOCUMENT = "parsed_document"
    TEMPLATE_SECTION = "template_section"
    ARTIFACT = "artifact"
    PROJECT_INSTRUCTION = "project_instruction"
    KNOWLEDGE = "knowledge"
    USER_MEMORY = "user_memory"
    WORKSPACE_MEMORY = "workspace_memory"
    AGENT_PLAYBOOK = "agent_playbook"
    TOOL_OUTPUT = "tool_output"
    GENERATED_CONTENT = "generated_content"
    REVIEW_RESULT = "review_result"


class RetrievalStrategy(StrEnum):
    """检索策略（Planned / On-demand）。"""

    PLANNED = "planned"
    ON_DEMAND = "on_demand"
    NONE = "none"


class RerankStrategy(StrEnum):
    """重排策略。"""

    RERANKER = "reranker"
    WEIGHTED_RRF = "weighted_rrf"
    NONE = "none"


class SnapshotStatus(StrEnum):
    """ContextSnapshot 生命周期状态。"""

    PENDING = "pending"
    SNAPSHOTTED = "snapshotted"
    USED = "used"


class CompressionLevel(StrEnum):
    """Compaction 四级水位（设计文档 §8.3.2）。"""

    TARGET = "target"
    SOFT = "soft"
    HARD_COMPACT = "hard_compact"
    ABSOLUTE = "absolute"


class CompactionStatus(StrEnum):
    """Compaction 结果状态。"""

    PASS = "pass"
    PRUNED = "pruned"
    COMPACTED = "compacted"
    BLOCKED = "blocked"


class CompactionType(StrEnum):
    """Compaction 类型（context_compaction_runs.compaction_type）。"""

    ITEM_PRUNING = "item_pruning"
    CONVERSATION = "conversation"
    AGENT_LOOP = "agent_loop"
    FULL_REPLACE = "full_replace"


class CompactionTriggerType(StrEnum):
    """Compaction 触发类型（context_compaction_runs.trigger_type）。"""

    PREFLIGHT = "preflight"
    SOFT_THRESHOLD = "soft_threshold"
    HARD_THRESHOLD = "hard_threshold"
    ABSOLUTE_THRESHOLD = "absolute_threshold"
    PROVIDER_CONTEXT_ERROR = "provider_context_error"
    MANUAL = "manual"
    RETRY_RECOVERY = "retry_recovery"


class RecoveryMode(StrEnum):
    """恢复模式（conversation_summaries.recovery_mode / context_compaction_runs.recovery_mode）。"""

    SUMMARY_ONLY = "summary_only"
    SUMMARY_WITH_REFS = "summary_with_refs"
    EVIDENCE_SEGMENTS = "evidence_segments"
    FULL_REHYDRATE = "full_rehydrate"


class ContextPreflightAction(StrEnum):
    """Preflight 决策动作。"""

    PASS = "pass"
    PRUNE = "prune"
    COMPACT = "compact"
    BLOCK = "block"


class PayloadMode(StrEnum):
    """Tool Output 策略（设计文档 §12.2）。"""

    INLINE = "inline"
    REFERENCE = "reference"
    EXCLUDE = "exclude"


class IndexSourceFamily(StrEnum):
    """Index 来源族（检索 / 记忆）。"""

    KNOWLEDGE = "knowledge"
    MEMORY = "memory"
    ARTIFACT = "artifact"


class MemoryScope(StrEnum):
    """长期记忆作用域（设计文档 §10.1）。"""

    USER = "user"
    WORKSPACE = "workspace"
    AGENT_PLAYBOOK = "agent_playbook"


class MemoryStatus(StrEnum):
    """记忆生命周期。"""

    CANDIDATE = "candidate"
    ACTIVE = "active"
    REJECTED = "rejected"
    FORGOTTEN = "forgotten"


class ContextExecutionMode(StrEnum):
    """Context 执行模式（设计文档 §49）。

    由 ContextEngine 生命周期统一处理，**不是** Snapshot 表字段
    （``llm_context_snapshots`` 无此列）；Shadow 通过
    ``context_kind='shadow'`` 复用快照行（见 WP-11）。
    """

    ACTIVE = "active"
    SHADOW = "shadow"
    PILOT = "pilot"
# auto-appended module-level note: ContextEngine 全局枚举: SourceKind / Scope / QuotaExceededReason 等。
