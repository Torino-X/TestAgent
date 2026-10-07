"""Phase 2.1 — 测试场景类型化输入。

每个场景都是 ``Scenario`` dataclass,描述:
- 输入字段(user_prompt / requirement_file_id / template_file_id)
- 用户决策(章节确认 / format-loss 决策)
- 期望关键事件序列(prefix,不一定全跑完)

Phase 2.1 的等价测试仅 2 个:happy_path / format_loss_review。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Scenario:
    name: str
    user_prompt: str = "请生成测试方案"
    requirement_file_id: str = "req-1"
    template_file_id: str = "tpl-1"
    kb_skip_reason: Optional[str] = None
    section_confirm_config: Optional[Dict[str, Any]] = None
    review_issues: Optional[List[Dict[str, Any]]] = None
    format_losses: Optional[List[Dict[str, Any]]] = None
    format_loss_decision: str = "accept"  # accept / retry / reject
    expected_prefix: List[str] = field(default_factory=list)


# ── 场景列表 ───────────────────────────────────────────────────────────────


HAPPY_PATH = Scenario(
    name="happy_path",
    user_prompt="请生成测试方案",
    requirement_file_id="req-1",
    template_file_id="tpl-1",
    kb_skip_reason=None,
    section_confirm_config={"items": [], "accepted": True},
    review_issues=None,
    format_losses=None,
    format_loss_decision="accept",
    # pre-confirm 阶段应至少到达 NEED_USER_CONFIRM
    # adapter 给每个工具发 5 个 tool_finished 帧(PublicExecutionUpdate split_into_chunks)
    expected_prefix=[
        "plan_step_started",  # 5 events in initialize_task
        "plan_step_completed",
        "plan_step_started",
        "plan_step_completed",
        "plan_created",
        # parse_requirement:5 帧 tool_finished + requirement_summary
        "tool_finished",
        "requirement_summary",
        # parse_template:5 帧 tool_finished + template_summary
        "tool_finished",
        "template_summary",
        # search_knowledge:5 帧 tool_finished + knowledge_summary
        "tool_finished",
        "knowledge_summary",
        # suggest_sections:5 帧 tool_finished
        "tool_finished",
        "need_user_confirm",
        "task_waiting",
    ],
)


FORMAT_LOSS_REVIEW = Scenario(
    name="format_loss_review",
    user_prompt="请生成测试方案",
    requirement_file_id="req-1",
    template_file_id="tpl-1",
    kb_skip_reason=None,
    section_confirm_config={"items": [], "accepted": True},
    review_issues=None,
    format_losses=[{"component": "table", "severity": "high"}],
    format_loss_decision="accept",
    expected_prefix=[
        "plan_step_started",
        "plan_step_completed",
        "plan_step_started",
        "plan_step_completed",
        "plan_created",
        "need_user_confirm",
        "task_waiting",
    ],
)


ALL_SCENARIOS = [HAPPY_PATH, FORMAT_LOSS_REVIEW]


def get_scenario(name: str) -> Scenario:
    for s in ALL_SCENARIOS:
        if s.name == name:
            return s
    raise KeyError(f"unknown scenario: {name}")


__all__ = [
    "Scenario",
    "HAPPY_PATH",
    "FORMAT_LOSS_REVIEW",
    "ALL_SCENARIOS",
    "get_scenario",
]