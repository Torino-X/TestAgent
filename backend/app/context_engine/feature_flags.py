"""Context Engine Feature Flag（设计文档 12 §45）。

遵循规范：服务端控制、有默认值、有回滚、不依赖前端传参、
不在函数中散落直接读环境变量。默认全部关闭（context engine 不默认启用）。

读取顺序：global → environment → user cohort → agent type → call-site →
task frozen version（本基础层实现 global + environment 两级，其余留给
后续阶段扩展）。

使用方式（推荐，不在函数中散落读 env）：
    decision = get_context_engine_flags().evaluate(ContextFeatureFlag.RERANK_ENABLED)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values


# Full-chain mode is an operational deployment contract, not a task semantic
# flag.  It deliberately remains outside the Frozen Manifest: operators may
# not start a release in this mode with a partially configured migration, but
# an already-created task must keep its frozen behaviour.
FULL_CHAIN_REQUIRED_ENV = "CONTEXT_ENGINE_FULL_CHAIN_REQUIRED"

# These are the task-scoped switches which make an Agent LLM call travel
# through ContextInvokerBridge.  In full-chain mode they must all be present
# and enabled; otherwise a task could silently create a valid all-false or
# partly-false manifest and bypass snapshot creation.
_FULL_CHAIN_REQUIRED_TRUE_ENV_NAMES = frozenset(
    {
        "CONTEXT_ENGINE_AGENT_ENABLED",
        "MIG_REVIEW",
        "MIG_REPAIR",
        "MIG_GENERATE",
        "MIG_PREPARATION",
        "MIG_INCREMENTAL",
        "MIG_CHAT",
        "MIG_SUMMARY",
        "MIG_NARRATIVE",
    }
)


class ContextFeatureFlag(str, Enum):
    """Context Engine 功能开关枚举（19 项）。"""

    # ── 总开关 ──────────────────────────────────────────────────────
    # Context Engine 主链是否参与生产调用
    CONTEXT_ENGINE_ENABLED = "context_engine_enabled"
    # 动态 Agent（Preparation/Repair/Incremental）是否启用 Context Engine
    CONTEXT_ENGINE_AGENT_ENABLED = "context_engine_agent_enabled"
    # Tool Output 治理（截断/外置 Payload）是否启用
    CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED = "context_tool_output_governance_enabled"

    # ── Memory ──────────────────────────────────────────────────────
    CONTEXT_MEMORY_READ_ENABLED = "context_memory_read_enabled"
    CONTEXT_MEMORY_WRITE_ENABLED = "context_memory_write_enabled"
    CONTEXT_MEMORY_AUTO_EXTRACT_ENABLED = "context_memory_auto_extract_enabled"
    # Auto Activate 默认关闭（自动记忆默认 Candidate，守禁令 21/22）
    CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED = "context_memory_auto_activate_enabled"

    # ── RAG / 检索 ──────────────────────────────────────────────────
    CONTEXT_RETRIEVAL_ENABLED = "context_retrieval_enabled"
    CONTEXT_LEXICAL_RETRIEVAL_ENABLED = "context_lexical_retrieval_enabled"
    CONTEXT_DENSE_RETRIEVAL_ENABLED = "context_dense_retrieval_enabled"
    CONTEXT_HYBRID_FUSION_ENABLED = "context_hybrid_fusion_enabled"
    CONTEXT_RERANK_ENABLED = "context_rerank_enabled"

    # ── Index Worker / 后台任务 ─────────────────────────────────────
    CONTEXT_INDEX_WORKER_ENABLED = "context_index_worker_enabled"

    # ── Compression ─────────────────────────────────────────────────
    CONTEXT_COMPACTION_ENABLED = "context_compaction_enabled"
    CONTEXT_CONVERSATION_COMPACTION_ENABLED = "context_conversation_compaction_enabled"
    CONTEXT_AGENT_LOOP_COMPACTION_ENABLED = "context_agent_loop_compaction_enabled"
    # Full Replace 默认关闭（守禁令 25）
    CONTEXT_FULL_REPLACE_ENABLED = "context_full_replace_enabled"

    # ── Debug / 审计 ────────────────────────────────────────────────
    # 默认不记录 Full Prompt（守禁令 26）
    CONTEXT_FULL_PROMPT_DEBUG_ENABLED = "context_full_prompt_debug_enabled"
    CONTEXT_DEBUG_API_ENABLED = "context_debug_api_enabled"
    # Safe, user-facing Context Usage diagnostics.  This is independent of
    # the admin debug API and defaults off for production releases.
    CONTEXT_USAGE_DEBUG_DETAILS_ENABLED = "context_usage_debug_details_enabled"
    # 开发态 Prompt 全量落盘（.md + index.jsonl，仅本地调试用）
    CONTEXT_PROMPT_DUMP_ENABLED = "context_prompt_dump_enabled"

    # ── LangGraph call-site 迁移（MIG flags）────────────────────────
    # 复验 §五：LangGraph 迁移必须逐 Story 可关。MIG flag=false → 走 legacy path；
    # MIG flag=true → 只走 Context Invoker，失败不静默回退 legacy LLMClient。
    MIG_REVIEW = "mig_review"
    MIG_REPAIR = "mig_repair"
    MIG_GENERATE = "mig_generate"
    MIG_PREPARATION = "mig_preparation"
    MIG_INCREMENTAL = "mig_incremental"
    MIG_CHAT = "mig_chat"
    MIG_SUMMARY = "mig_summary"
    MIG_NARRATIVE = "mig_narrative"

@dataclass(frozen=True)
class ContextEngineFeatureFlags:
    """进程级 Context Engine feature flags。全部默认关闭。"""

    # 总开关
    context_engine_enabled: bool = False
    context_engine_agent_enabled: bool = False
    context_tool_output_governance_enabled: bool = False
    # Memory
    context_memory_read_enabled: bool = False
    context_memory_write_enabled: bool = False
    context_memory_auto_extract_enabled: bool = False
    context_memory_auto_activate_enabled: bool = False
    # RAG
    context_retrieval_enabled: bool = False
    context_lexical_retrieval_enabled: bool = False
    context_dense_retrieval_enabled: bool = False
    context_hybrid_fusion_enabled: bool = False
    context_rerank_enabled: bool = False
    # Index Worker
    context_index_worker_enabled: bool = False
    # Compression
    context_compaction_enabled: bool = False
    context_conversation_compaction_enabled: bool = False
    context_agent_loop_compaction_enabled: bool = False
    context_full_replace_enabled: bool = False
    # Debug
    context_full_prompt_debug_enabled: bool = False
    context_debug_api_enabled: bool = False
    context_usage_debug_details_enabled: bool = False
    context_prompt_dump_enabled: bool = False
    # LangGraph call-site 迁移（复验 §五；均默认 False → legacy path）
    mig_review: bool = False
    mig_repair: bool = False
    mig_generate: bool = False
    mig_preparation: bool = False
    mig_incremental: bool = False
    mig_chat: bool = False
    mig_summary: bool = False
    mig_narrative: bool = False
    # 派生/互斥约束（依赖矩阵）
    @property
    def memory_write_implies_extract(self) -> bool:
        """自动提取只在实际可写时生效。"""
        return self.context_memory_write_enabled and self.context_memory_auto_extract_enabled

    @property
    def memory_auto_activate_requires_write(self) -> bool:
        """Auto Activate 依赖 Memory Write；默认关闭（守禁令 22）。"""
        return (
            self.context_memory_write_enabled
            and self.context_memory_auto_activate_enabled
        )

    @property
    def retrieval_implies_engine(self) -> bool:
        """检索管线依赖总开关。"""
        return self.context_engine_enabled and self.context_retrieval_enabled

    @property
    def lexical_implies_retrieval(self) -> bool:
        """词法通道依赖检索总开关。"""
        return self.retrieval_implies_engine and self.context_lexical_retrieval_enabled

    @property
    def dense_requires_retrieval(self) -> bool:
        """dense 通道依赖检索总开关（provider 有无另行判断）。"""
        return self.retrieval_implies_engine and self.context_dense_retrieval_enabled

    @property
    def memory_read_implies_engine(self) -> bool:
        """Memory 读取依赖总开关。"""
        return self.context_engine_enabled and self.context_memory_read_enabled

    @property
    def full_prompt_debug_requires_engine(self) -> bool:
        return (
            self.context_engine_enabled
            and self.context_full_prompt_debug_enabled
        )

    def evaluate(self, flag: ContextFeatureFlag) -> bool:
        """评估单个 flag（global + environment 级）。"""
        values = {
            ContextFeatureFlag.CONTEXT_ENGINE_ENABLED: self.context_engine_enabled,
            ContextFeatureFlag.CONTEXT_ENGINE_AGENT_ENABLED: self.context_engine_agent_enabled,
            ContextFeatureFlag.CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED: self.context_tool_output_governance_enabled,
            ContextFeatureFlag.CONTEXT_MEMORY_READ_ENABLED: self.context_memory_read_enabled,
            ContextFeatureFlag.CONTEXT_MEMORY_WRITE_ENABLED: self.context_memory_write_enabled,
            ContextFeatureFlag.CONTEXT_MEMORY_AUTO_EXTRACT_ENABLED: self.context_memory_auto_extract_enabled,
            ContextFeatureFlag.CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED: self.context_memory_auto_activate_enabled,
            ContextFeatureFlag.CONTEXT_RETRIEVAL_ENABLED: self.context_retrieval_enabled,
            ContextFeatureFlag.CONTEXT_LEXICAL_RETRIEVAL_ENABLED: self.context_lexical_retrieval_enabled,
            ContextFeatureFlag.CONTEXT_DENSE_RETRIEVAL_ENABLED: self.context_dense_retrieval_enabled,
            ContextFeatureFlag.CONTEXT_HYBRID_FUSION_ENABLED: self.context_hybrid_fusion_enabled,
            ContextFeatureFlag.CONTEXT_RERANK_ENABLED: self.context_rerank_enabled,
            ContextFeatureFlag.CONTEXT_INDEX_WORKER_ENABLED: self.context_index_worker_enabled,
            ContextFeatureFlag.CONTEXT_COMPACTION_ENABLED: self.context_compaction_enabled,
            ContextFeatureFlag.CONTEXT_CONVERSATION_COMPACTION_ENABLED: self.context_conversation_compaction_enabled,
            ContextFeatureFlag.CONTEXT_AGENT_LOOP_COMPACTION_ENABLED: self.context_agent_loop_compaction_enabled,
            ContextFeatureFlag.CONTEXT_FULL_REPLACE_ENABLED: self.context_full_replace_enabled,
            ContextFeatureFlag.CONTEXT_FULL_PROMPT_DEBUG_ENABLED: self.context_full_prompt_debug_enabled,
            ContextFeatureFlag.CONTEXT_DEBUG_API_ENABLED: self.context_debug_api_enabled,
            ContextFeatureFlag.CONTEXT_USAGE_DEBUG_DETAILS_ENABLED: self.context_usage_debug_details_enabled,
            ContextFeatureFlag.CONTEXT_PROMPT_DUMP_ENABLED: self.context_prompt_dump_enabled,
            ContextFeatureFlag.MIG_REVIEW: self.mig_review,
            ContextFeatureFlag.MIG_REPAIR: self.mig_repair,
            ContextFeatureFlag.MIG_GENERATE: self.mig_generate,
            ContextFeatureFlag.MIG_PREPARATION: self.mig_preparation,
            ContextFeatureFlag.MIG_INCREMENTAL: self.mig_incremental,
            ContextFeatureFlag.MIG_CHAT: self.mig_chat,
            ContextFeatureFlag.MIG_SUMMARY: self.mig_summary,
            ContextFeatureFlag.MIG_NARRATIVE: self.mig_narrative,
        }
        return values[flag]


_DEFAULT_FLAGS = ContextEngineFeatureFlags()
_DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# 所有 flag 的 env 变量名（服务端控制，集中在模块级常量）
_ENV_VAR_NAMES: dict[ContextFeatureFlag, str] = {
    ContextFeatureFlag.CONTEXT_ENGINE_ENABLED: "CONTEXT_ENGINE_ENABLED",
    ContextFeatureFlag.CONTEXT_ENGINE_AGENT_ENABLED: "CONTEXT_ENGINE_AGENT_ENABLED",
    ContextFeatureFlag.CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED: "CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED",
    ContextFeatureFlag.CONTEXT_MEMORY_READ_ENABLED: "CONTEXT_MEMORY_READ_ENABLED",
    ContextFeatureFlag.CONTEXT_MEMORY_WRITE_ENABLED: "CONTEXT_MEMORY_WRITE_ENABLED",
    ContextFeatureFlag.CONTEXT_MEMORY_AUTO_EXTRACT_ENABLED: "CONTEXT_MEMORY_AUTO_EXTRACT_ENABLED",
    ContextFeatureFlag.CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED: "CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED",
    ContextFeatureFlag.CONTEXT_RETRIEVAL_ENABLED: "CONTEXT_RETRIEVAL_ENABLED",
    ContextFeatureFlag.CONTEXT_LEXICAL_RETRIEVAL_ENABLED: "CONTEXT_LEXICAL_RETRIEVAL_ENABLED",
    ContextFeatureFlag.CONTEXT_DENSE_RETRIEVAL_ENABLED: "CONTEXT_DENSE_RETRIEVAL_ENABLED",
    ContextFeatureFlag.CONTEXT_HYBRID_FUSION_ENABLED: "CONTEXT_HYBRID_FUSION_ENABLED",
    ContextFeatureFlag.CONTEXT_RERANK_ENABLED: "CONTEXT_RERANK_ENABLED",
    ContextFeatureFlag.CONTEXT_INDEX_WORKER_ENABLED: "CONTEXT_INDEX_WORKER_ENABLED",
    ContextFeatureFlag.CONTEXT_COMPACTION_ENABLED: "CONTEXT_COMPACTION_ENABLED",
    ContextFeatureFlag.CONTEXT_CONVERSATION_COMPACTION_ENABLED: "CONTEXT_CONVERSATION_COMPACTION_ENABLED",
    ContextFeatureFlag.CONTEXT_AGENT_LOOP_COMPACTION_ENABLED: "CONTEXT_AGENT_LOOP_COMPACTION_ENABLED",
    ContextFeatureFlag.CONTEXT_FULL_REPLACE_ENABLED: "CONTEXT_FULL_REPLACE_ENABLED",
    ContextFeatureFlag.CONTEXT_FULL_PROMPT_DEBUG_ENABLED: "CONTEXT_FULL_PROMPT_DEBUG_ENABLED",
    ContextFeatureFlag.CONTEXT_DEBUG_API_ENABLED: "CONTEXT_DEBUG_API_ENABLED",
    ContextFeatureFlag.CONTEXT_USAGE_DEBUG_DETAILS_ENABLED: "CONTEXT_USAGE_DEBUG_DETAILS_ENABLED",
    ContextFeatureFlag.CONTEXT_PROMPT_DUMP_ENABLED: "CONTEXT_PROMPT_DUMP_ENABLED",
    ContextFeatureFlag.MIG_REVIEW: "MIG_REVIEW",
    ContextFeatureFlag.MIG_REPAIR: "MIG_REPAIR",
    ContextFeatureFlag.MIG_GENERATE: "MIG_GENERATE",
    ContextFeatureFlag.MIG_PREPARATION: "MIG_PREPARATION",
    ContextFeatureFlag.MIG_INCREMENTAL: "MIG_INCREMENTAL",
    ContextFeatureFlag.MIG_CHAT: "MIG_CHAT",
    ContextFeatureFlag.MIG_SUMMARY: "MIG_SUMMARY",
    ContextFeatureFlag.MIG_NARRATIVE: "MIG_NARRATIVE",
}


@lru_cache
def _read_dotenv_flags() -> dict[str, str]:
    """Read deployment flags from the backend `.env` without mutating os.environ."""
    try:
        raw_values = dotenv_values(_DOTENV_PATH)
    except OSError:
        return {}
    return {
        str(name): str(value)
        for name, value in raw_values.items()
        if name is not None and value is not None
    }


def get_context_engine_flags() -> ContextEngineFeatureFlags:
    """读取进程级 Context Engine flags。

    只支持 env opt-in（服务端控制）。默认全部关闭，保证生产零行为变化。
    测试 sandbox（PYTEST_CURRENT_TEST）下只开总开关，其余子开关仍独立默认关闭。
    """
    in_pytest = bool(os.environ.get("PYTEST_CURRENT_TEST"))

    def _env(flag: ContextFeatureFlag) -> bool:
        name = _ENV_VAR_NAMES[flag]
        # A real process environment is the operator override.  Pydantic
        # reads `.env` for Settings but does not export undeclared keys into
        # os.environ, so feature flags must explicitly read the same file.
        value = os.environ.get(name)
        if value is None:
            value = _read_dotenv_flags().get(name, "")
        value = value.strip().lower()
        if value in {"1", "true", "yes"}:
            return True
        return False

    # 测试 sandbox 只开总开关（子开关独立评估，不在测试中隐式全开）
    engine_enabled = _env(ContextFeatureFlag.CONTEXT_ENGINE_ENABLED) or in_pytest

    return ContextEngineFeatureFlags(
        context_engine_enabled=engine_enabled,
        context_engine_agent_enabled=_env(ContextFeatureFlag.CONTEXT_ENGINE_AGENT_ENABLED),
        context_tool_output_governance_enabled=_env(ContextFeatureFlag.CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED),
        context_memory_read_enabled=_env(ContextFeatureFlag.CONTEXT_MEMORY_READ_ENABLED),
        context_memory_write_enabled=_env(ContextFeatureFlag.CONTEXT_MEMORY_WRITE_ENABLED),
        context_memory_auto_extract_enabled=_env(ContextFeatureFlag.CONTEXT_MEMORY_AUTO_EXTRACT_ENABLED),
        context_memory_auto_activate_enabled=_env(ContextFeatureFlag.CONTEXT_MEMORY_AUTO_ACTIVATE_ENABLED),
        context_retrieval_enabled=_env(ContextFeatureFlag.CONTEXT_RETRIEVAL_ENABLED),
        context_lexical_retrieval_enabled=_env(ContextFeatureFlag.CONTEXT_LEXICAL_RETRIEVAL_ENABLED),
        context_dense_retrieval_enabled=_env(ContextFeatureFlag.CONTEXT_DENSE_RETRIEVAL_ENABLED),
        context_hybrid_fusion_enabled=_env(ContextFeatureFlag.CONTEXT_HYBRID_FUSION_ENABLED),
        context_rerank_enabled=_env(ContextFeatureFlag.CONTEXT_RERANK_ENABLED),
        context_index_worker_enabled=_env(ContextFeatureFlag.CONTEXT_INDEX_WORKER_ENABLED),
        context_compaction_enabled=_env(ContextFeatureFlag.CONTEXT_COMPACTION_ENABLED),
        context_conversation_compaction_enabled=_env(ContextFeatureFlag.CONTEXT_CONVERSATION_COMPACTION_ENABLED),
        context_agent_loop_compaction_enabled=_env(ContextFeatureFlag.CONTEXT_AGENT_LOOP_COMPACTION_ENABLED),
        context_full_replace_enabled=_env(ContextFeatureFlag.CONTEXT_FULL_REPLACE_ENABLED),
        context_full_prompt_debug_enabled=_env(ContextFeatureFlag.CONTEXT_FULL_PROMPT_DEBUG_ENABLED),
        context_debug_api_enabled=_env(ContextFeatureFlag.CONTEXT_DEBUG_API_ENABLED),
        context_usage_debug_details_enabled=_env(ContextFeatureFlag.CONTEXT_USAGE_DEBUG_DETAILS_ENABLED),
        context_prompt_dump_enabled=_env(ContextFeatureFlag.CONTEXT_PROMPT_DUMP_ENABLED),
        # LangGraph call-site 迁移 flags（MIG；默认 False → legacy path）
        mig_review=_env(ContextFeatureFlag.MIG_REVIEW),
        mig_repair=_env(ContextFeatureFlag.MIG_REPAIR),
        mig_generate=_env(ContextFeatureFlag.MIG_GENERATE),
        mig_preparation=_env(ContextFeatureFlag.MIG_PREPARATION),
        mig_incremental=_env(ContextFeatureFlag.MIG_INCREMENTAL),
        mig_chat=_env(ContextFeatureFlag.MIG_CHAT),
        mig_summary=_env(ContextFeatureFlag.MIG_SUMMARY),
        mig_narrative=_env(ContextFeatureFlag.MIG_NARRATIVE),
    )


class ContextEngineReleaseConfigurationError(RuntimeError):
    """Raised before work starts when full-chain deployment is incomplete."""


def validate_full_chain_configuration() -> None:
    """Reject a partial Context Engine rollout when full-chain mode is enabled.

    An explicit ``0`` remains valid for optional task-semantic features such
    as memory auto-activation.  Every task-semantic key must nevertheless be
    declared, and every migration switch required to prevent a legacy LLM
    route must be ``true``.
    """

    def _raw_value(name: str) -> str | None:
        value = os.environ.get(name)
        if value is None:
            value = _read_dotenv_flags().get(name)
        return value

    required = _raw_value(FULL_CHAIN_REQUIRED_ENV)
    if str(required or "").strip().lower() not in {"1", "true", "yes"}:
        return

    # Import lazily to avoid feature-flags <-> freeze module import coupling.
    from app.context_engine.freeze.profiles import FROZEN_FLAG_KEYS

    missing = sorted(name for name in FROZEN_FLAG_KEYS if _raw_value(name) is None)
    disabled = sorted(
        name
        for name in _FULL_CHAIN_REQUIRED_TRUE_ENV_NAMES
        if str(_raw_value(name) or "").strip().lower() not in {"1", "true", "yes"}
    )
    if missing or disabled:
        parts: list[str] = []
        if missing:
            parts.append(f"missing={','.join(missing)}")
        if disabled:
            parts.append(f"disabled={','.join(disabled)}")
        raise ContextEngineReleaseConfigurationError(
            "Context Engine full-chain configuration is invalid: " + "; ".join(parts)
        )


def is_agent_context_migration_enabled(
    resolver: object | None,
    migration_flag: str,
) -> bool:
    """Resolve an Agent MIG flag together with its task-scoped master gate.

    Agent task paths must provide a Frozen Manifest resolver. Missing or
    invalid resolver evaluation fails closed. Ordinary non-task Chat does not
    use this helper and keeps its independent process-level migration gate.
    """
    if migration_flag not in {
        "MIG_REVIEW",
        "MIG_REPAIR",
        "MIG_GENERATE",
        "MIG_PREPARATION",
        "MIG_INCREMENTAL",
        "MIG_SUMMARY",
        "MIG_NARRATIVE",
    }:
        raise ValueError(f"unsupported Agent migration flag: {migration_flag}")
    if resolver is None:
        return False
    try:
        evaluate = getattr(resolver, "evaluate")
        return bool(
            evaluate("CONTEXT_ENGINE_AGENT_ENABLED")
            and evaluate(migration_flag)
        )
    except Exception:  # noqa: BLE001 — migration authority fails closed
        return False


def require_agent_context_migration(
    resolver: object | None,
    migration_flag: str,
) -> str | None:
    """Return a stable failure code unless a task is frozen for CE routing.

    Agent task model calls must never use the process configuration or a
    legacy prompt path when their frozen manifest is absent or disabled.
    Callers can surface this code directly as a recoverable task failure.
    """
    if resolver is None:
        return "MIGRATION_CONTEXT_REQUIRED"
    try:
        evaluate = getattr(resolver, "evaluate")
        if not bool(evaluate("CONTEXT_ENGINE_AGENT_ENABLED")):
            return "CONTEXT_ENGINE_AGENT_DISABLED"
        if not bool(evaluate(migration_flag)):
            return "MIGRATION_CONTEXT_REQUIRED"
    except Exception:  # noqa: BLE001 — migration authority fails closed
        return "MIGRATION_CONTEXT_REQUIRED"
    return None


def reset_context_engine_flags_for_test() -> None:
    """仅供测试使用 — 无全局可变 cache，no-op 保留兼容。"""
    return None
# auto-appended module-level note: context_engine feature flags。
