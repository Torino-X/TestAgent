"""Public, bounded narrative contracts for Phase 2.9B.

This module deliberately contains no model client and no hidden reasoning. It
only validates facts that an Agent has already decided to expose and turns them
into a small event envelope that legacy consumers can safely ignore.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"(?:Bearer\s+|sk-[A-Za-z0-9_-]{8,})\S*", "[redacted]", value)
    value = re.sub(r"(?:[A-Za-z]:\\|/home/|/Users/|/workspace/)[^\s]+", "[path]", value)
    return value.strip()[:limit]


# Tool-name verbs used to detect "calling another tool" mentions in
# next_action text. Subset of allow-listed tools — if next_action mentions
# any of these, the narrative claims to invoke a specific tool.
_TOOL_VERB_KEYWORDS = (
    "SearchTool",
    "ParserTool",
    "SuggestionTool",
    "GeneratorTool",
    "RegenTool",
    "ReviewTool",
    "ExportTool",
    "FormatCheckTool",
)


# Stack-trace / path markers — fail/path narratives must hide these.
_FAIL_LEAK_MARKERS = (
    "Traceback ",
    "Traceback (",
    " at 0x",
    'File "',
    "raise ",
)


def validate_route_consistency(
    draft: AgentPublicUpdateDraft,
    *,
    approved_tool_name: str | None,
    is_finish: bool,
    is_fail: bool,
) -> bool:
    """校验 narrative draft 与真实决策/路由的一致性(Phase 2.9B §10.3)。

    返回 ``True`` 表示 narrative 与 approved decision 一致,
    可以作为公开说明发布;
    返回 ``False`` 表示 draft 与真实路由冲突,应丢弃并使用
    确定性 fallback。

    Rules:
      * ``is_finish=True`` 时,``next_action`` 不得描述调用任何工具。
      * ``is_fail=True`` 时,``next_action`` 不得包含 stack trace / 路径
        泄漏标记。
      * ``approved_tool_name`` 已确定(``call_tool`` 路径)时,
        ``next_action`` 描述的工具名必须与 ``approved_tool_name`` 一致 —
        或者直接不含工具动词(只描述目标而非工具名)。
    """
    if not isinstance(draft, AgentPublicUpdateDraft):
        return False
    next_action = (draft.next_action or "").strip()

    if is_finish:
        if next_action and any(kw in next_action for kw in _TOOL_VERB_KEYWORDS):
            return False

    if is_fail:
        lowered = next_action
        if any(marker in lowered for marker in _FAIL_LEAK_MARKERS):
            return False

    if not is_finish and approved_tool_name:
        if next_action:
            if not _action_targets_approved_tool(next_action, approved_tool_name):
                return False
    return True


def _action_targets_approved_tool(text: str, approved: str) -> bool:
    """轻量判定:next_action 描述的工具与 approved 是否一致。

    不做 NLP 解析,只看:
      * 若一句话明确提到 approved 工具名,视为对齐。
      * 若提到任何其他列出的工具动词,且不属于 approved 工具,视为冲突。
      * 若一句话不出现任何工具动词,默认通过(描述目标即可)。
    """
    if not text or not approved:
        return True
    if approved in text:
        return True
    for kw in _TOOL_VERB_KEYWORDS:
        if kw in text and kw != approved and not approved.startswith(kw):
            return False
    return True


class AgentPublicUpdateDraft(BaseModel):
    """The only narrative text allowed into a public Agent update.

    Phase 2.9B.3 contract — 正式合同统一为:

        {
          "headline": "string",
          "summary": "string",
          "impact": "string",
          "next_action": "string",
          "details": ["string"]
        }

    * ``headline`` 必填,trim 后非空,max_length=80;
    * ``summary`` / ``impact`` / ``next_action`` 必填,trim 后非空
      (当 public_update 对象存在时),长度上限 200/160/160;
    * ``details`` 为 ``list[str]``,每项 trim、删除空项、最多 5 项;
      不允许 dict 作为正式输出(历史 dict 事件由前端规范化兼容)。
    """

    model_config = ConfigDict(extra="forbid")

    headline: str = Field(max_length=80)
    summary: str = Field(max_length=200)
    impact: str = Field(max_length=160)
    next_action: str = Field(max_length=160)
    details: list[str] = Field(default_factory=list, max_length=5)
    narrative_text: str = Field(default="", max_length=800)


class AgentObservation(BaseModel):
    """Public facts produced after a tool call; no raw arguments are stored."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=80)
    success: bool
    error_code: str | None = Field(default=None, max_length=80)
    summary_facts: dict[str, Any] = Field(default_factory=dict)
    result_excerpt: str = Field(default="", max_length=240)
    business_effect: str = Field(default="", max_length=160)
    retryable: bool | None = None
    source_event_id: str | None = Field(default=None, max_length=120)


class PublicNarrativeEnvelope(BaseModel):
    """Stable event payload shared by Preparation, Repair and Incremental."""

    model_config = ConfigDict(extra="forbid")

    update_kind: Literal["agent_decision", "agent_observation"]
    agent_name: str = Field(min_length=1, max_length=80)
    decision_id: str = Field(min_length=1, max_length=120)
    step_index: int = Field(ge=0)
    action: str = Field(min_length=1, max_length=80)
    tool_name: str | None = Field(default=None, max_length=80)
    route: str | None = Field(default=None, max_length=80)
    public_update: AgentPublicUpdateDraft


def _normalize_details(value: Any) -> list[str]:
    """Normalize ``details`` to a bounded list of trimmed non-empty strings.

    Phase 2.9B.3 正式合同为 ``list[str]``。历史事件里可能出现 dict / null /
    非法值,这里统一转换为 list:
      - list[str] → 原样清洗(trim、删空、最多 5 项);
      - dict     → 每个键值对转 ``"键:值"`` 条目(历史兼容);
      - null / 非法 → []。
    """
    if isinstance(value, list):
        items = []
        for item in value:
            text = _clean(item, 160)
            if text:
                items.append(text)
        return items[:5]
    if isinstance(value, dict):
        items = []
        for key, val in value.items():
            k = _clean(key, 80)
            v = _clean(val, 120)
            if k:
                items.append(f"{k}:{(' ' + v) if v else ''}")
        return items[:5]
    return []


def normalize_public_update(value: Any) -> AgentPublicUpdateDraft | None:
    """Normalize LLM-shaped public text without retrying the model call.

    Wrong field types are dropped. Oversized strings are bounded before model
    validation. This is intentionally tolerant because narrative is optional
    and must never make the core Agent decision fail.

    Phase 2.9B.3: 只有当 public_update 对象存在且 headline/summary/impact/
    next_action 均 trim 后非空时才视为「合法完整」叙事;否则返回 ``None``
    (调用方改用确定性公开回退,不再把空正文或内部错误泄漏给用户)。
    """

    if isinstance(value, AgentPublicUpdateDraft):
        value = value.model_dump(mode="python")
    if isinstance(value, str):
        # 单字符串叙事只能退化为 headline;没有 summary/impact/next_action,
        # 按新合同视为不完整 → None,由调用方走确定性 fallback。
        headline = _clean(value, 80)
        if not headline:
            return None
        return None
    if not isinstance(value, dict):
        return None
    narrative_text = _clean(value.get("narrative_text") or value.get("narrativeText"), 800)
    headline = _clean(value.get("headline"), 80) or _clean(narrative_text, 80)
    if not headline:
        return None
    summary = _clean(value.get("summary"), 200)
    impact = _clean(value.get("impact"), 160)
    next_action = _clean(value.get("next_action"), 160)
    # Natural narratives carry user-visible prose in narrative_text. Legacy
    # structured updates still need the three body fields.
    if not narrative_text and (not summary or not impact or not next_action):
        return None
    return AgentPublicUpdateDraft(
        headline=headline,
        summary=summary or _clean(narrative_text, 200),
        impact=impact,
        next_action=next_action,
        details=_normalize_details(value.get("details")),
        narrative_text=narrative_text,
    )


def observation_to_public_update(
    observation: AgentObservation | dict[str, Any],
) -> AgentPublicUpdateDraft:
    """Build a bounded narrative from already-public tool facts.

    Phase 2.9B.3: details 统一为 ``list[str]``(每项 "key: value"),
    summary/impact/next_action 保证非空,满足新正式合同。
    """

    item = (
        observation
        if isinstance(observation, AgentObservation)
        else AgentObservation.model_validate(observation)
    )
    summary = _clean(item.result_excerpt, 200) or (
        f"{item.tool_name} completed" if item.success else f"{item.tool_name} failed"
    )
    details = [
        f"{key}:{(' ' + _clean(val, 120)) if _clean(val, 120) else ''}"
        for key, val in item.summary_facts.items()
        if isinstance(key, str)
        and key not in {"tool_arguments", "arguments", "secrets"}
    ][:5]
    impact = _clean(item.business_effect, 160) or "已生成工具执行结果。"
    next_action = (
        f"{item.tool_name} {'已完成' if item.success else '失败'},继续当前流程。"
    )
    return AgentPublicUpdateDraft(
        headline=f"{item.tool_name} {'完成' if item.success else '失败'}",
        summary=summary,
        impact=impact,
        next_action=next_action,
        details=details,
        narrative_text=(
            f"{item.tool_name} {'执行完成' if item.success else '执行失败'}。"
            f"{summary} 后续流程会基于这次结果继续处理。"
        ),
    )


def deterministic_preparation_fallback() -> AgentPublicUpdateDraft:
    """安全的确定性公开回退叙事(Preparation Agent 决策失败时使用)。

    Phase 2.9B.3: 内部 Schema 错误 / 解析错误 / 模型原始文本**绝不**泄漏给用户。
    当模型未生成合法的 decision_update/observation_update 时,用固定文案说明
    系统已切换到确定性准备流程。字段完整(headline/summary/impact/next_action/
    details),满足新正式合同。
    """
    return AgentPublicUpdateDraft(
        headline="准备阶段已使用默认策略",
        summary="动态准备决策未能生成有效的结构化结果,系统已自动切换到确定性准备流程。",
        impact="任务将继续执行,不影响后续需求分析、模板处理和测试方案生成。",
        next_action="接下来将按照默认准备策略继续处理当前任务。",
        details=[],
        narrative_text=(
            "准备阶段没有拿到可直接展示的模型叙事,我会按已经确定的准备流程继续往下走。"
            "这不会中断需求解析、模板处理和测试方案生成。"
        ),
    )


def deterministic_repair_fallback() -> AgentPublicUpdateDraft:
    """安全的确定性公开回退叙事(Repair Agent 决策失败时使用)。"""
    return AgentPublicUpdateDraft(
        headline="修复阶段已使用默认策略",
        summary="动态修复决策未能生成有效的结构化结果,系统已自动切换到确定性修复流程。",
        impact="任务将继续执行,不影响后续审查与导出。",
        next_action="接下来将按照默认修复策略继续处理当前问题。",
        details=[],
    )


def deterministic_incremental_fallback() -> AgentPublicUpdateDraft:
    """安全的确定性公开回退叙事(Incremental Agent 决策失败时使用)。"""
    return AgentPublicUpdateDraft(
        headline="增量任务已使用默认策略",
        summary="动态增量决策未能生成有效的结构化结果,系统已自动切换到确定性增量流程。",
        impact="任务将继续执行,不影响后续生成与导出。",
        next_action="接下来将按照默认增量策略继续处理当前任务。",
        details=[],
    )


def build_narrative_envelope(
    *,
    update_kind: Literal["agent_decision", "agent_observation"],
    agent_name: str,
    decision_id: str,
    step_index: int,
    action: str,
    tool_name: str | None,
    route: str | None,
    public_update: Any,
    approved_tool_name: str | None = None,
    is_finish: bool = False,
    is_fail: bool = False,
) -> dict[str, Any] | None:
    """构造 narrative envelope;Phase 2.9B §10.3 路由一致性校验内置。

    若 draft 与 approved tool/finish/fail 不一致 → 返回 ``None``
    (caller 改用确定性 fallback,合法决策不被阻断)。
    """
    draft = normalize_public_update(public_update)
    if draft is None:
        return None
    if not validate_route_consistency(
        draft,
        approved_tool_name=approved_tool_name,
        is_finish=is_finish,
        is_fail=is_fail,
    ):
        return None
    return PublicNarrativeEnvelope(
        update_kind=update_kind,
        agent_name=_clean(agent_name, 80),
        decision_id=_clean(decision_id, 120),
        step_index=max(0, step_index),
        action=_clean(action, 80),
        tool_name=_clean(tool_name, 80) or None,
        route=_clean(route, 80) or None,
        public_update=draft,
    ).model_dump(mode="json")


__all__ = [
    "AgentObservation",
    "AgentPublicUpdateDraft",
    "PublicNarrativeEnvelope",
    "build_narrative_envelope",
    "deterministic_incremental_fallback",
    "deterministic_preparation_fallback",
    "deterministic_repair_fallback",
    "normalize_public_update",
    "observation_to_public_update",
    "validate_route_consistency",
]


# 模块定位:Phase 2.9B 公开叙事合同(Public / Bounded Narrative)
#
# 本模块刻意不引用任何 LLM 客户端,也不在源码里隐藏推理:
#   - 输入:Agent 已经决定要公开的事实(whitelist);
#   - 输出:小的事件 envelope,供 SSE / 持久化;
#   - 不暴露:CoT / model 原始输出 / 内部路径 / API key。
#
# 字段限定(AgentPublicUpdateDraft):
#   - headline(<= 80 字符)
#   - summary(<= 200)
#   - impact(<= 160)
#   - next_action(<= 160)
#   - details(2~5 条,各 <= 160)
#   - narrative_text(<= 800)
#
# 关键约束:
#   - 所有字段在落库 / 入 SSE 之前都要过本模块的 model_validate(...);
#   - 不允许传任意外部 schema(防止前端 reconstruct 不可控字段);
#   - 与 narrative_composer 的对接点是 AllowedFactConstraints;
#   - 任何破坏该合同的提交直接 fail PR review(已有 lint 提醒)。
