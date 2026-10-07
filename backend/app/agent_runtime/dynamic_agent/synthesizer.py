"""Final answer synthesis for Dynamic Agent."""

from __future__ import annotations

_INTERNAL_STATUS_LEAK_MARKERS = (
    "当前任务状态",
    "若需自动流转",
    "自动流转至下一节点",
    "task_status",
    "current_node",
    "节点 `unknown`",
    "node `unknown`",
)


def _sanitize_user_visible_answer(text: str) -> str:
    lines = text.splitlines()
    filtered = [
        line
        for line in lines
        if not any(marker in line for marker in _INTERNAL_STATUS_LEAK_MARKERS)
    ]
    sanitized = "\n".join(filtered).strip()
    return sanitized or text.strip()


class DynamicSynthesizer:
    def synthesize(self, state: dict) -> str:
        analysis = state.get("analysis_results")
        if isinstance(analysis, dict):
            for value in analysis.values():
                text = str(value or "").strip()
                if text:
                    return _sanitize_user_visible_answer(text)
        observations = [
            str(item.get("summary") or "").strip()
            for item in list(state.get("observations") or [])
            if isinstance(item, dict) and item.get("summary")
        ]
        if observations:
            return _sanitize_user_visible_answer("\n".join(observations))
        return "已完成当前动态任务，但没有形成可展示的分析结果。"


# module-level note (auto-appended):
# DynamicSynthesizer — 多步结果合成。
# 关键约束: 与 verifier 配合,失败转 replanner。
