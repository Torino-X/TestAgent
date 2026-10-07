"""Phase 2.4 eval report renderer.

``render_markdown(artifact: dict) -> str`` 生成可读 markdown 报告;输出写到
``backend/_artifacts/repair_eval/repair_eval_report.md``。
"""

from __future__ import annotations

from typing import Any, Dict, List


def render_markdown(artifact: Dict[str, Any]) -> str:
    """Render an eval artifact as markdown."""
    summary = artifact.get("summary") or {}
    scenarios = artifact.get("scenarios") or []

    lines: List[str] = []
    lines.append("# TestAgent Phase 2.4 Review Repair Agent Eval Report")
    lines.append("")
    lines.append(
        f"phase: `{artifact.get('phase', '?')}` · "
        f"completed_at: `{artifact.get('completed_at', '?')}`"
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    for k, v in summary.items():
        lines.append(f"| `{k}` | `{v}` |")
    lines.append("")
    lines.append("## Per-scenario Results")
    lines.append("")
    lines.append("| # | name | passed | fallback | steps | tool_calls | rounds |")
    lines.append("|---|---|---|---|---|---|---|")
    for s in scenarios:
        lines.append(
            f"| `{s.get('index')}` | `{s.get('name')}` | "
            f"`{s.get('passed')}` | `{s.get('fallback') or '-'}` | "
            f"`{s.get('steps')}` | `{s.get('tool_calls')}` | "
            f"`{s.get('rounds')}` |"
        )
    lines.append("")
    return "\n".join(lines)


__all__ = ["render_markdown"]
