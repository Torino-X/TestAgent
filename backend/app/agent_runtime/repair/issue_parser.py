"""Repair Issue Parser — ResultReviewTool → list[ReviewIssue] (Phase 2.4).

ADR-2.4-3 + Risk #5: 解析 review_result 中的 ``review_issues`` (新,标准化)
优先, ``block_issues`` (旧, Phase 2.1) 兜底:

* 过滤 ``severity != "block"``
* 过滤 ``section_id ∈ locked_section_ids``
* 过滤 ``repairable=False``
* 保留 issue_id / rule_id / kind / section_id / field_path / message / evidence /
  expected_rule / suggested_strategy (后续 scope_guard / decision_filter 使用)
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List

from app.agent_runtime.repair.schemas import ReviewIssue


def _coerce_issue_id(
    rule_id: str, section_id: str, idx: int, raw_issue_id: str | None,
) -> str:
    """生成确定性 issue_id —— Phase 2.4 用 (rule, section, idx) 避免 LLM 凭空捏造。"""
    if raw_issue_id:
        return str(raw_issue_id)[:200]
    return f"{rule_id}:{section_id or 'global'}:{idx}"


def coerce_evidence_text(value: Any, *, max_length: int = 400) -> str | None:
    """Normalize structured review evidence into bounded prompt-safe text.

    ResultReviewTool may keep rich dict/list evidence for diagnostics, while
    RepairIssue is intentionally compact text for LLM routing and prompt
    construction. This function is the boundary between those contracts.
    """

    if value is None:
        return None
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            text = str(value)
    return text[:max_length]


def parse_review_issues(
    review_result: Dict[str, Any],
    *,
    locked_section_ids: Iterable[str] = (),
) -> List[ReviewIssue]:
    """Parse ResultReviewTool output → 过滤后的 list[ReviewIssue]。

    Args:
        review_result: ``state.review_result`` 字典(由 review_result_node 写入)
        locked_section_ids: 用户锁定的章节 ID;命中将被剔除

    Returns:
        List of ``ReviewIssue`` (severity=block, repairable=True, 不在 locked)

    兼容:
    * 新字段 ``review_issues`` 优先(Phase 2.4 standardized)
    * 旧字段 ``block_issues`` 兜底(Phase 2.1 / Phase 2.3 testing)
    """
    locked = set(locked_section_ids or ())

    raw_issues: List[Dict[str, Any]] = []
    new_field = review_result.get("review_issues")
    if isinstance(new_field, list):
        for it in new_field:
            if isinstance(it, dict) and it.get("severity") == "block":
                raw_issues.append(it)
    else:
        # 旧路径 — block_issues 已经是 severity=block 的子集
        legacy = review_result.get("block_issues") or review_result.get("issues") or []
        if isinstance(legacy, list):
            raw_issues = [it for it in legacy if isinstance(it, dict)]

    out: List[ReviewIssue] = []
    for idx, raw in enumerate(raw_issues):
        section_id = raw.get("section_id") or ""
        if section_id in locked:
            continue
        # repairable=False 跳过(留 Phase 2.5+ 精细化工具)
        if raw.get("repairable") is False:
            continue
        rule_id = str(raw.get("rule_id") or "unknown_rule")
        kind = str(raw.get("kind") or "unknown")
        issue_id = _coerce_issue_id(
            rule_id, section_id or "", idx, raw.get("issue_id"),
        )
        out.append(
            ReviewIssue(
                issue_id=issue_id,
                rule_id=rule_id,
                kind=kind,
                severity="block",
                section_id=section_id or None,
                field_path=raw.get("field_path"),
                message=str(raw.get("message") or "")[:480],
                evidence=coerce_evidence_text(raw.get("evidence")),
                expected_rule=raw.get("expected_rule"),
                repairable=True,
                suggested_strategy=raw.get("suggested_strategy") or "regenerate_section",
                # Phase 2.9A.X：透传 fix_instruction — LLM-actionable 修正指令，
                # TestPlanRegenTool._build_prompt 优先用它替代 message。
                fix_instruction=raw.get("fix_instruction"),
            )
        )
    return out


__all__ = ["coerce_evidence_text", "parse_review_issues"]


# module-level note (auto-appended):
# ReviewIssue 解析 — review_issues → RepairDecision 输入。
# 关键约束: 解析只读,不改 state。
