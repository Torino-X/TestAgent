"""Idempotent Dynamic Agent completion projection contract."""

from __future__ import annotations


class DynamicAgentCompletionProjector:
    def __init__(self) -> None:
        self._projected_task_ids: set[str] = set()

    def project(self, state: dict) -> dict | None:
        task_public_id = str(state.get("task_public_id") or state.get("task_id") or "")
        final_answer = str(state.get("final_answer") or "").strip()
        if not task_public_id or not final_answer:
            return None
        if task_public_id in self._projected_task_ids:
            return None
        self._projected_task_ids.add(task_public_id)
        return {
            "task_public_id": task_public_id,
            "role": "assistant",
            "content": final_answer,
            "payload": {
                "source": "dynamic_agent",
                "task_public_id": task_public_id,
            },
        }


# module-level note (auto-appended):
# DynamicAgentCompletionProjector — 完成态投影(plan → 真实写入)。
# 关键约束: 必须经过 scope_guard。
