"""Incremental Task Agent 动态子图 — Phase 2.5。

对已生成的测试方案做局部增量修改(改章节 / 扩范围 / 调表 / 重审 /
重导出);**只跑必要节点**,不重做整个 pre-confirm → post-confirm 流程。

镜像 ``app.agent_runtime.preparation`` 与 ``app.agent_runtime.repair`` 的
子包布局;共用 ``app.agent_runtime._shared`` 的 BudgetTracker /
ToolPermissionGuard / args_signature / filter_decision。

Phase 2.5 增量点:

* graph_name = ``incremental_test_plan``(区别于 Phase 2.4 ``test_plan_generation``)
* state schema_version 升到 5(incremental=True 时)
* 新增 6 个 SSE 事件(INCREMENTAL_STARTED / COMPLETED / FALLBACK /
  BUDGET_EXHAUSTED / SCOPE_CONFIRM_REQUESTED / SCOPE_DECISION_RECORDED)
* IntentRouter RESULT_MODIFICATION 路由从 clarify 改为 agent_task +
  条件 gate(基于 ``intent_context.last_completed_artifact_public_id``)
* Artifact 链走 ``task_context_json.superseded_artifact_ids``(无 Alembic;
  已存在的 ``Artifact.version_no + source_artifact_id`` 列继续沿用)
* 所有写入幂等;不修改原任务历史 / ``agent_tasks.context_version``

当前生产约束：仅由 LangGraph dispatcher 调用；历史非 LangGraph 任务不得
进入本子图。`incremental_agent_enabled` 仍是独立能力门禁。
"""

from __future__ import annotations

from .schemas import (
    Action,
    BudgetState,
    ExistingArtifactRef,
    IncrementalDecision,
    IncrementalIntent,
    IncrementalResult,
    ModificationScope,
    PublicSummary,
)

__all__ = [
    "Action",
    "BudgetState",
    "ExistingArtifactRef",
    "IncrementalDecision",
    "IncrementalIntent",
    "IncrementalResult",
    "ModificationScope",
    "PublicSummary",
]

# 子包说明 (Phase 2.5 Incremental Task Agent):
# 入口:incremental.subgraph.run_incremental_subgraph
# 仅跑必要节点(不重做 pre-confirm → post-confirm 全流程)
# 关键约束:不动已完成 section,artifact 链式 version_no +1,不开生产默认
