"""Context State Update — 把 Invoker 结果转换为 State 更新。

CE-02 整改一：Invoker 迁移到 Agent Runtime 层（app/agent_runtime/context/）。
CE-02 WP-9（设计 06 §42）：返回 ``{"context_state": ref.model_dump,
"active_context_plan": ...}`` 替换语义。
"""

from __future__ import annotations

from typing import Any

from app.context_engine.models.snapshot_models import ContextStateRef


def build_context_state_update(result) -> dict[str, Any]:
    """由 ContextualLLMResult 构建 State 更新（替换语义）。"""
    ref = result.context_state_ref
    if ref is not None and isinstance(ref, ContextStateRef):
        return {
            "context_state": ref.to_state_dict(),
            "active_context_plan": None,
        }
    return {
        "context_state": {
            "latest_snapshot_public_id": result.snapshot_public_id,
            "stats": {},
            "source_refs": [],
        },
        "active_context_plan": None,
    }


def build_loop_context_state_update(result, retain_plan: bool = False) -> dict[str, Any]:
    """Agent Loop 场景：可选保留 active_context_plan。"""
    update = build_context_state_update(result)
    if not retain_plan:
        update["active_context_plan"] = None
    return update
