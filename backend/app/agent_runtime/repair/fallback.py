"""Repair Fallback — 字节级复用 Phase 2.1 ``regenerate_sections_node`` (Phase 2.4).

镜像 ``preparation.fallback`` 的设计 (ADR-2.4-15):
* Repair Agent 任意失败 / 永久拒绝 / budget 耗尽时,
  ``run_legacy_review_repair_fallback`` 直接调
  ``nodes_review_format.regenerate_sections_node`` 主体
* 保证 Phase 2.1 4 个 routing 测试 + Phase 2.2 16 个 checkpoint 测试
  零回归 (Test 13/test_dynamic_failure_fallback 测试强制)
* 不重写 regen-loop,只是 delegate
"""

from __future__ import annotations

from typing import Any, Dict

from app.agent_runtime.graphs.test_plan.state import TestPlanGraphState
from app.agent_runtime.runtime_context import RuntimeContext


async def run_legacy_review_repair_fallback(
    state: TestPlanGraphState,
    *,
    ctx: RuntimeContext,
    reason: str = "fallback_to_legacy_regen",
) -> Dict[str, Any]:
    """字节级 fallback:delegate 到 ``nodes_review_format.regenerate_sections_node``。

    Args:
        state: TestPlanGraphState(当前轮 review 后)
        ctx: RuntimeContext
        reason: fallback_reason 字符串,会被写入新 state

    Returns:
        state delta dict (与 regenerate_sections_node 语义一致)
        + ``repair_fallback_reason`` 写入
    """
    # Local import to avoid circular dep with v2.nodes_review_format
    from app.agent_runtime.graphs.test_plan.versions.v2.nodes_review_format import (
        regenerate_sections_node,
    )

    result = await regenerate_sections_node(state, ctx=ctx)
    if isinstance(result, dict):
        result["repair_fallback_reason"] = reason
        result["current_node"] = "regenerate_sections_step"  # 标注走的是兜底
    return result


__all__ = ["run_legacy_review_repair_fallback"]


# module-level note (auto-appended):
# Repair fallback — 失败兜底(字节级复用 regenerate_sections_node)。
# 关键约束: fallback 必须保留 Phase 2.1 字节级契约。
