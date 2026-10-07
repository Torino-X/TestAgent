"""report.py — render prep_eval.json as a markdown table."""

from __future__ import annotations

from typing import Any, Dict, List


def render_markdown(artifact: Dict[str, Any]) -> str:
    """Render the eval artifact as a markdown summary."""
    summary = artifact["summary"]
    scenarios: List[Dict[str, Any]] = artifact["scenarios"]

    lines: List[str] = []
    lines.append("# TestAgent Phase 2.3 — Preparation Agent 评测报告")
    lines.append("")
    lines.append("## 总览")
    lines.append("")
    lines.append(f"- **场景总数**: {summary['total']}")
    lines.append(f"- **通过**: {summary['passed']}")
    lines.append(f"- **失败**: {summary['failed']}")
    lines.append(f"- **准确率**: {summary['accuracy']:.0%}")
    lines.append(f"- **Fallback 率**: {summary['fallback_rate']:.0%}")
    lines.append(f"- **总工具调用**: {summary['tool_calls_total']}")
    lines.append(f"- **总步数**: {summary['steps_total']}")
    lines.append(f"- **平均步数**: {summary['avg_steps']:.2f}")
    lines.append(f"- **平均工具调用**: {summary['avg_tool_calls']:.2f}")
    lines.append("")

    lines.append("## 场景明细")
    lines.append("")
    lines.append("| # | 场景 | 预期 action | 实际 action | 工具调用 | 步数 | 用时 | 结果 |")
    lines.append("|---|------|-------------|-------------|----------|------|------|------|")
    for i, s in enumerate(scenarios, 1):
        actual = s["actual"]
        status = "✅ PASS" if s["passed"] else "❌ FAIL"
        lines.append(
            f"| {i} | {s['scenario']} | "
            f"{s['expected']['final_action']} | "
            f"{actual['final_action']} | "
            f"{actual['tool_calls_count']} | "
            f"{s['steps']} | "
            f"{s['elapsed_ms']} ms | "
            f"{status} |"
        )
    lines.append("")

    fails = [s for s in scenarios if not s["passed"]]
    if fails:
        lines.append("## 失败明细")
        lines.append("")
        for f in fails:
            lines.append(f"### {f['scenario']}")
            lines.append("")
            for key in f["expected"]:
                exp = f["expected"][key]
                act = f["actual"].get(key)
                mark = "✅" if f["matches"][key] else "❌"
                lines.append(f"- {key}: 期望 `{exp}`, 实际 `{act}` {mark}")
            lines.append("")

    lines.append("## 观察")
    lines.append("")
    if summary["accuracy"] == 1.0:
        lines.append("- 全部 6 个场景通过,准备 agent 主循环行为符合预期。")
    else:
        lines.append(
            f"- {summary['failed']} 个场景未达预期,需要回归到 agent_loop.py 调优。"
        )
    if summary["fallback_rate"] > 0.3:
        lines.append("- Fallback 率偏高,可能 LLM 决策边界需要收窄或 guard 更激进。")
    if summary["avg_tool_calls"] > 2.5:
        lines.append("- 平均工具调用偏高,query 精炼策略可优化(避免不必要的二次检索)。")
    lines.append("")
    return "\n".join(lines)