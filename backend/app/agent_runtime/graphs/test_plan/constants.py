"""图相关的全局常量。"""

from __future__ import annotations

GRAPH_NAME_TEST_PLAN: str = "test_plan_generation"
GRAPH_VERSION_V1: str = "v1"
GRAPH_VERSION_V2: str = "v2"
# Phase 2.8R-D: v3 独立版本 — 与 v2_frozen 完全隔离
# v3 显式独立注册;不再 fallback 到 v2 latest
GRAPH_VERSION_V3: str = "v3"
# v1 stub 仍用 1;v2 真图升到 2(Phase 2.1);v3 加 Preparation Agent 字段 (Phase 2.3)
STATE_SCHEMA_VERSION_V1: int = 1
STATE_SCHEMA_VERSION_V2: int = 2
STATE_SCHEMA_VERSION_V3: int = 3
STATE_SCHEMA_VERSION_V4: int = 4
STATE_SCHEMA_VERSION_V5: int = 5  # Phase 2.5: 增量任务 Agent
STATE_SCHEMA_VERSION_V6: int = 6  # Phase 2.8D: Retry + Summary 闭环
                                     # 新增 attempt/last_retry_decision/summary 字段
# Phase 2.8R-D: v3 默认 schema_version 也用 V6(retry + summary 字段)
STATE_SCHEMA_VERSION_V7: int = 7  # 预留 v3 专用 schema 版本(目前与 V6 等价)

# Phase 2.4: Repair Agent scope guard upper bound on
# ``target_section_ids`` length(防 LLM 扩大修复范围)。
MAX_REPAIR_SCOPE_SECTIONS: int = 3

# Phase 2.5: Incremental Agent scope guard upper bound on
# ``target_section_ids`` length(略大于 Repair,因含整段 export)。
MAX_INCREMENTAL_TARGET_SECTIONS: int = 8

# Phase 2.1: review-regen 和 format-check 循环的有界上限
MAX_REVIEW_LOOPS: int = 3
MAX_FORMAT_LOOPS: int = 2

# Phase 2.1: plan announce 5 个事件之间的间隔(从 Legacy orchestrator
# 的 PLAN_STEP_VISIBLE_SECONDS=0.8 镜像过来)。
PLAN_STEP_VISIBLE_SECONDS: float = 0.8

# Phase 2.1: 工具 min_visible 兜底 + 帧间过渡。
MIN_TOOL_VISIBLE_SECONDS: float = 0.8
TOOL_TRANSITION_SECONDS: float = 0.12


def state_schema_version_for(version: str) -> int:
    return {
        GRAPH_VERSION_V1: STATE_SCHEMA_VERSION_V1,
        GRAPH_VERSION_V2: STATE_SCHEMA_VERSION_V2,
        GRAPH_VERSION_V3: STATE_SCHEMA_VERSION_V7,
    }.get(version, STATE_SCHEMA_VERSION_V2)


# 向后兼容:旧导入 ``STATE_SCHEMA_VERSION`` 仍解析到 v2 默认值
STATE_SCHEMA_VERSION: int = STATE_SCHEMA_VERSION_V2

__all__ = [
    "GRAPH_NAME_TEST_PLAN",
    "GRAPH_VERSION_V1",
    "GRAPH_VERSION_V2",
    "GRAPH_VERSION_V3",
    "STATE_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION_V1",
    "STATE_SCHEMA_VERSION_V2",
    "STATE_SCHEMA_VERSION_V3",
    "STATE_SCHEMA_VERSION_V4",
    "STATE_SCHEMA_VERSION_V5",
    "STATE_SCHEMA_VERSION_V6",
    "STATE_SCHEMA_VERSION_V7",
    "state_schema_version_for",
    "MAX_REVIEW_LOOPS",
    "MAX_FORMAT_LOOPS",
    "PLAN_STEP_VISIBLE_SECONDS",
    "MIN_TOOL_VISIBLE_SECONDS",
    "TOOL_TRANSITION_SECONDS",
]
