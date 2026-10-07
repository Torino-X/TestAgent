"""Repair Agent package — Review Repair 动态 Agent (Phase 2.4).

镜像 ``app.agent_runtime.preparation`` 子包布局;共用
``app.agent_runtime._shared`` 的 BudgetTracker / ToolPermissionGuard /
args_signature / filter_decision。Repair 自己的扩展点:

* 3 工具 result review / regen / kb-search(不是 prep 的单工具)
* 6 节点修复 subgraph(decide / execute_tool / observe / re_review /
  finish / fallback),比 prep 的 5 节点多一个 re_review
* scope_guard.enforce_minimal_scope 强制修复范围不超出 review_issues
  涉及 section 且不触碰 locked_section_ids

ADR-2.4 范围(强约束):
* ❌ 不开 production 默认 repair_agent_enabled (Feature Flag 默认关)
* ❌ 不删除历史任务数据
* ❌ 不动 ``app/api/v1/agent_tasks.py``
* ❌ 不引入 PostgreSQL/SQLite checkpointer(仍 MemorySaver)
"""

from app.agent_runtime.repair.budget import BudgetExceeded, BudgetTracker
from app.agent_runtime.repair.capabilities import (
    ModelCapabilities,
    resolve_capabilities,
)
from app.agent_runtime.repair.permission import (
    REPAIR_TOOL_WHITELIST,
    PermanentPermissionDenied,
    ToolPermissionDenied,
    ToolPermissionGuard,
)
from app.agent_runtime.repair.schemas import (
    Action,
    BudgetState,
    KnowledgeEvidence,
    PublicSummary,
    RepairDecision,
    RepairResult,
    ReviewIssue,
)


__all__ = [
    "BudgetTracker",
    "BudgetExceeded",
    "BudgetState",
    "ModelCapabilities",
    "resolve_capabilities",
    "REPAIR_TOOL_WHITELIST",
    "ToolPermissionDenied",
    "PermanentPermissionDenied",
    "ToolPermissionGuard",
    "Action",
    "ReviewIssue",
    "RepairDecision",
    "KnowledgeEvidence",
    "PublicSummary",
    "RepairResult",
]


# 子包说明 (Phase 2.4 Review Repair Agent):
# 入口:repair.subgraph.run_repair_subgraph
# 扩展点:RepairDecision / scope_guard / 6 节点修复 subgraph
# 关键约束:scope_guard 不能被绕过,失败 emit RepairStep(success=False),不开生产默认
