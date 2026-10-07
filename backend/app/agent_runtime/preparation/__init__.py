"""Preparation Agent 动态子图 — Phase 2.3。

对外公共 surface (本阶段范围):
* Schemas: AgentDecision / PreparationResult / KnowledgeEvidence /
  RequirementGap / UserQuestion / BudgetState / PublicSummary
* Capabilities: ModelCapabilities + resolve_capabilities
* Prompt: build_preparation_prompt (在 prompt.py)
* Budget: BudgetTracker + BudgetExceeded (在 budget.py)
* Permission: ToolPermissionGuard + PREPARATION_TOOL_WHITELIST (在 permission.py)
* Tool filter: filter_decision_tool_calls (在 tool_filter.py)
* Loop: run_preparation (在 agent_loop.py)
* Fallback: run_legacy_kb_fallback (在 fallback.py)
* Subgraph: build_preparation_subgraph (在 subgraph.py)
* Events: PreparationEventEmitter (在 event_emitter.py)

不在本 __init__ 中 import 具体实现 — 让上层按需 lazy import,
避免 LangGraph 节点 import 链拉长 (mirror adapter pattern)。
"""

from __future__ import annotations

from .schemas import (
    Action,
    AgentDecision,
    BudgetState,
    KnowledgeEvidence,
    PreparationResult,
    PublicSummary,
    RequirementGap,
    UserQuestion,
)

__all__ = [
    "Action",
    "AgentDecision",
    "BudgetState",
    "KnowledgeEvidence",
    "PreparationResult",
    "PublicSummary",
    "RequirementGap",
    "UserQuestion",
]