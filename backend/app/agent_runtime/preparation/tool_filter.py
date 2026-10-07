"""filter_decision_tool_calls — Pydantic schema validate model args (Phase 2.4).

Rule 13: 不允许未经 Schema 校验直接使用模型生成的工具参数。

Phase 2.4 changes (ADR-2.4-1):
* ``_to_fail_decision`` 改用 ``app.agent_runtime._shared.to_fail_decision`` 工厂
* ``args_signature`` 改 import 自 ``app.agent_runtime._shared.args_signature``
* 其他逻辑字节级保留(Phase 2.3 tests 不回归)
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.agent_runtime._shared.args_signature import args_signature
from app.agent_runtime._shared.to_fail_decision import make_to_fail_decision

from .permission import PREPARATION_TOOL_WHITELIST, ToolPermissionGuard
from .schemas import AgentDecision


# ── per-tool args schema ───────────────────────────────────────────────


class KnowledgeSearchArgs(BaseModel):
    """KnowledgeSearchTool — 现阶段唯一允许工具。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    top_k: int | None = Field(default=None, ge=1, le=20)
    labels: List[str] | None = None


# name → schema class
_PER_TOOL_SCHEMA: Dict[str, type[BaseModel]] = {
    "KnowledgeSearchTool": KnowledgeSearchArgs,
}


# ── factory-bound helper ────────────────────────────────────────────────


_to_fail_decision = make_to_fail_decision(AgentDecision)


# ── public API ──────────────────────────────────────────────────────────


def filter_decision_tool_calls(
    decision: AgentDecision,
    guard: ToolPermissionGuard,
) -> Tuple[AgentDecision, List[Dict[str, Any]]]:
    """校验 LLM 单步决策的工具调用合法性。

    Returns:
        (clean_decision, blocked_list)
        * clean_decision — 通过校验的决策(若无违规,等价于原 decision)
        * blocked_list   — 每条被拦截的违规描述 {tool, args, reason}
          当 call_tool 但 tool_name/args 校验失败时,clean_decision 转为
          action="fail" 且 decision_summary 写明拦截原因 (供下轮 LLM 修正)

    Raises:
        不抛 — 拦截转为 clean_decision.action=fail 或 blocked_list, 由调用方
        决定是否走 fallback / ask LLM 重新规划。
    """
    blocked: List[Dict[str, Any]] = []

    if decision.action != "call_tool":
        return decision, blocked

    tool_name = decision.tool_name or ""
    raw_args = decision.tool_arguments or {}

    # 1. 白名单 (ToolPermissionGuard 也会再做,此处先快速路径)
    if tool_name not in _PER_TOOL_SCHEMA or tool_name not in PREPARATION_TOOL_WHITELIST:
        blocked.append(
            {"tool": tool_name, "args": raw_args, "reason": "unknown_tool"}
        )
        return _to_fail_decision(
            decision, reason=f"工具 {tool_name!r} 不在 PREPARATION 允许范围"
        ), blocked

    # 2. schema 校验
    schema_cls = _PER_TOOL_SCHEMA[tool_name]
    try:
        validated = schema_cls.model_validate(raw_args)
    except Exception as exc:
        blocked.append(
            {"tool": tool_name, "args": raw_args, "reason": f"schema_invalid: {exc}"}
        )
        return _to_fail_decision(
            decision,
            reason=f"工具 {tool_name!r} 参数 schema 校验失败: {exc}",
        ), blocked

    # 3. permission guard (含 repeats 计数 + fail-fast)
    sig = args_signature(validated.model_dump())
    try:
        guard.authorize(tool_name, sig)
    except Exception as exc:
        blocked.append(
            {
                "tool": tool_name,
                "args": raw_args,
                "args_signature": sig,
                "reason": f"permission_denied: {exc}",
            }
        )
        return _to_fail_decision(
            decision,
            reason=f"权限/重复次数受限: {exc}",
        ), blocked

    # All checks passed — clean decision with normalized args
    cleaned = decision.model_copy(update={"tool_arguments": validated.model_dump()})
    return cleaned, blocked


__all__ = ["filter_decision_tool_calls", "KnowledgeSearchArgs"]

# module-level note (auto-appended):
# filter_decision_tool_calls — 拒绝可疑调用。
# 同 args 重复 N 次转 reject。
# 关键约束: 不静默 pass,失败必 emit Decision 行。
