"""v3 graph package — Phase 2.8R-D 独立版本。

设计目标(对应 docs/35 §4 + 验收五):
  * 与 v2_frozen 完全隔离(目录独立,无交叉 import)
  * 显式声明 ``GRAPH_VERSION_V3``;不向后兼容 v2 logic
  * 未知 version / 不在 registry → ``GraphVersionNotAvailableError``
    (不静默回退 latest — 守 #18)
  * v3 默认状态 schema_version = V7(包含 V6 retry/summary 字段);
    v2_frozen checkpoints 仍按 V6 schema_version 读

不在范围(2.8R-D 设计 §4.4):
  ❌ 不修改 v2_frozen(只读)
  ❌ 不引入新增 retry 边(R2 retry 改写已在 2.8D Retry 阶段实施,本阶段只独立)
  ❌ 不重新 Summary 节点(2.8D 已实现,v3 复用 nodes_post_confirm.generate_completion_summary)
"""

from __future__ import annotations

from .graph import build_compiled_v3_graph, build_test_plan_v3_graph

__all__ = [
    "build_compiled_v3_graph",
    "build_test_plan_v3_graph",
]
