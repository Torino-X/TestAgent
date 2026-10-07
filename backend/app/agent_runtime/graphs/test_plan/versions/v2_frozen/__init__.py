"""v2 子包导出。"""
from __future__ import annotations

from .graph import build_compiled_v2_graph, build_test_plan_v2_graph

__all__ = ["build_compiled_v2_graph", "build_test_plan_v2_graph"]


# ════════════════════════════════════════════════════════════════════════════════
# v2_frozen 历史快照(Phase 2.2)。
# 与同目录的 v2/ 字节级镜像;目录独立是为了 Phase 2.2.x 升级不被绑死,
# v2 可演进,v2_frozen 永远冻结(供回滚 / 测试稳定快照用)。
#
# 读代码时:v2_frozen 内容与 v2 几乎一致,优先看 v2 的注释(v2 改动先行)。
# 唯一差异:LangGraph 编译时 graph_name 不同
# (`${GRAPH_NAME_TEST_PLAN}_v2_frozen` vs `${GRAPH_NAME_TEST_PLAN}_v2`),
# 不影响代码组织。
#
# ⚠️ 不要让 v2_frozen 与 v3 直接对照读 ——
#   v2_frozen 用 sentinel pause_marker,v3 走真 LangGraph interrupt,差异大;
#   想知道中断怎么跑,读 v3/nodes_interrupts.py(v3 已加注释)。
# ════════════════════════════════════════════════════════════════════════════════
