"""Phase 2.8D — GraphState v2 → v6 状态迁移。

ADR-2.8D-2 / 守 #22 不破坏现有 checkpoint。

设计要点:
* 当 LangGraph Checkpointer 读到的 state 是旧 schema_version(v2/v3/v4/v5)时,
  调用 ``migrate_to_v6`` 把 state 升级到 v6,补 3 个新字段默认值
* 缺失关键字段(current_node / resume_node 等)→ 抛 MigrationRequired 让调用方 reset 重跑
* 现有 checkpoint 不删除,迁移是 in-memory 操作(写回由 Checkpointer 自动序列化)
* 仅一个迁移函数 migrate_to_v6;后续 v7+ 在此基础上叠加
"""

from __future__ import annotations

from typing import Any

V6_NEW_FIELDS: tuple[str, ...] = ("attempt", "last_retry_decision", "summary")
V6_REQUIRED_FROM_V2: frozenset[str] = frozenset(
    {
        "state_schema_version",
        "current_node",
        "graph_run_id",
        "task_id",
    }
)


class MigrationRequired(Exception):
    """state 缺关键字段,需要 reset 重跑。

    抛出后 LangGraphRunCoordinator 应:
    1. 记录 warning 日志
    2. 跳过旧 checkpoint,初始化一个全新 state(task_id 保持)
    3. 标记 task 为 fresh start
    """


def migrate_to_v6(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 2.8D:把任意旧版 GraphState(2-5)迁移到 v6。

    Args:
        state: LangGraph Checkpointer 读到的原始 state(dict)。

    Returns:
        迁移后的 state dict(就地修改并返回)。

    Raises:
        MigrationRequired: state 缺关键字段,无法安全迁移,需 reset。
    """
    if not isinstance(state, dict):
        raise MigrationRequired(f"state must be dict; got {type(state).__name__}")

    missing_required = V6_REQUIRED_FROM_V2 - set(state.keys())
    if missing_required:
        raise MigrationRequired(
            f"GraphState v2 → v6: missing required fields {sorted(missing_required)}; "
            "需要 reset 状态重跑"
        )

    # 升级 schema_version;缺则补 2(2.1 默认值)
    current_version = state.get("state_schema_version") or 2
    if current_version < 6:
        state["state_schema_version"] = 6

    # 补 3 个新字段默认值(向后兼容)
    state.setdefault("attempt", {})
    state.setdefault("last_retry_decision", {})
    state.setdefault("summary", None)
    return state


__all__ = [
    "MigrationRequired",
    "migrate_to_v6",
    "V6_NEW_FIELDS",
    "V6_REQUIRED_FROM_V2",
]

# 模块定位:GraphState schema 迁移(v2/v3/v4/v5 → v6,Phase 2.8D ADR-2.8D-2)
#
# 守 #22:不破坏既有 checkpoint ——
# 当 LangGraph Checkpointer 读到的 state 是旧 schema_version 时,调用
# migrate_to_v6(state) 把 state 升级到 v6,补 3 个新字段默认值。
#
# 链路:
#   LangGraphRunCoordinator 启动时 → graph_thread_id.require_graph_thread_id
#     → state.schema_version < 6 → 调 migrate_to_v6(state)
#       → 字段补全 + atomic / attempt / last_retry_decision
#     → resume / restart 业务
#
# 关键约束:
#   - 单向迁移(v2/v3/v4/v5 → v6),不要做反向;
#   - 永不抛错让 LangGraph 卡死(失败 → 报错到 log + 让上层重试);
#   - 测试覆盖 v2/v3/v4/v5 全部 schema 形状(各种字段缺省)。
