"""LangGraph 图模块根。"""

from __future__ import annotations

from . import test_plan

__all__ = ["test_plan"]

# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (LangGraph 图模块根):
#
#   Graph 模块根入口。test_plan 子包内部按 versions/v2、versions/v2_frozen、
#   versions/v3 三个版本组织,默认业务图 v3。
#
#   关于 v3(默认生产):
#     由 graph_registry 在 startup 注册 'test_plan_generation_v3',
#     feature_flags 默认指向 v3。phase 灰度时切到 v2_frozen 需用
#     GRAPH_VERSION_FEATURE_FLAGS。
#
#   关于 v2 / v2_frozen(历史):
#     默认不启用,仅回滚或特殊测试场景显式装载。
# ════════════════════════════════════════════════════════════════════════════════
