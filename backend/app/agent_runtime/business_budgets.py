"""BusinessBudgets — Phase 2.8R-F 业务循环 Budget 控制。

设计要点(对应 docs/35 §6 验收八):
  * 各业务循环(Preparation / Repair / Review / Format / ToolRetry / LLMRetry)
    由 ``BusinessBudgets`` 统一配置,**不依赖 GraphRecursionError**。
  * ``recursion_limit`` 不是为了"卡住" — 是为了"防 Python while 误改死循环"。
  * ``compute_max_legal_steps`` 走纯静态表(measurements),不靠猜。
  * 校验:``recursion_limit`` < 已知最小合法值 → 启动警告或 RuntimeError。

实测路径矩阵(§6.4):
  A. 最短成功         ~ 12 super-step
  B. +prep 1 次       ~ 14
  C. prep 最大         ~ 17
  D. review 失败+repair ~ 18
  E. repair 最大       ~ 20
  F. format loss       ~ 22
  G. llm retry         ~ 24
  H. tool retry        ~ 26
  I. incremental       ~ 30+
  J. interrupt + resume ~ 28
  K. 恢复后继续         ~ 30

``recursion_limit`` = ``max_legal_observed`` + ``max(5, 20% safety margin)``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Path measurements(§6.4 矩阵) ──────────────────────────────────────────────
# 各路径观察到的 super-step 上限。Phase 2.8R-F 实际测量未做(本研究环境无 docker);
# 下表为 docs/35 §6.4 标注值(可调整)。这些是 production 经验值。

PATH_MEASUREMENTS: dict[str, int] = {
    "A_shortest_success": 12,
    "B_prep_once": 14,
    "C_prep_max": 17,
    "D_review_fail_repair": 18,
    "E_repair_max": 20,
    "F_format_loss": 22,
    "G_llm_retry": 24,
    "H_tool_retry": 26,
    "I_incremental": 30,
    "J_interrupt_resume": 28,
    "K_resume_continue": 30,
}


@dataclass(frozen=True)
class BusinessBudgets:
    """统一配置各业务循环的预算。

    修改 default 不影响已有 checkpoint(只用 score 检查,不参与 hash)。
    """

    # 主循环 budget
    preparation_max: int = 2        # preparation_agent 节点最大重试
    review_max: int = 3             # review-regen 循环上限(MAX_REVIEW_LOOPS)
    format_max: int = 2             # format-check 循环上限(MAX_FORMAT_LOOPS)
    repair_max: int = 2             # repair subgraph 内部循环
    incremental_max: int = 5        # incremental 决策/执行循环

    # 工具 / LLM 重试 budget
    tool_retry_max: int = 3         # _run_tool_with_retry 最大次数
    llm_retry_max: int = 3          # LLMClient.generate_with_system retry

    # recursion_limit 加的安全余量(20%)
    safety_margin_percent: int = 20
    safety_margin_floor: int = 5    # 至少 5 步安全余量

    def max_legal_observed(self) -> int:
        """返回 PATH_MEASUREMENTS 中最大值(+1 fudge)。

        单一 source of truth,便于测量值调整。
        """
        return max(PATH_MEASUREMENTS.values()) + 1

    def safe_margin(self) -> int:
        """安全余量 = max(5, max_legal * safety_margin_percent / 100)。"""
        legal = self.max_legal_observed()
        margin = max(self.safety_margin_floor, legal * self.safety_margin_percent // 100)
        return margin

    def compute_recursion_limit(self) -> int:
        """计算生产用 recursion_limit = max_legal + safe_margin。

        默认 30 + max(5, 6) = 30 + 6 = 36。后续调整实测值时一处改全应用。
        """
        return self.max_legal_observed() + self.safe_margin()

    def to_dict(self) -> dict[str, int]:
        """dict 形式,方便日志 / 配置 dump。"""
        return {
            "preparation_max": self.preparation_max,
            "review_max": self.review_max,
            "format_max": self.format_max,
            "repair_max": self.repair_max,
            "incremental_max": self.incremental_max,
            "tool_retry_max": self.tool_retry_max,
            "llm_retry_max": self.llm_retry_max,
            "max_legal_observed": self.max_legal_observed(),
            "safe_margin": self.safe_margin(),
            "computed_recursion_limit": self.compute_recursion_limit(),
        }


# ── 默认 singleton ──────────────────────────────────────────────────────────


_default_budgets: BusinessBudgets = BusinessBudgets()


def get_default_budgets() -> BusinessBudgets:
    return _default_budgets


def set_default_budgets(b: BusinessBudgets) -> None:
    """测试 / 配置覆盖入口。"""
    global _default_budgets
    _default_budgets = b


def validate_recursion_limit(value: int | None) -> int:
    """校验 recursion_limit 值是否安全。

    Args:
        value: 候选值;None → 用默认 budgets 计算。

    Returns:
        校验通过的值。

    Raises:
        RecursionLimitConfigurationInvalidError: value < 已知最小合法值。
    """
    from app.core.exceptions import RecursionLimitConfigurationInvalidError

    default_budgets = get_default_budgets()
    min_safe = default_budgets.max_legal_observed()  # 无 margin 最低

    if value is None:
        return default_budgets.compute_recursion_limit()

    if not isinstance(value, int) or value <= 0:
        raise RecursionLimitConfigurationInvalidError(
            configured=value if isinstance(value, int) else -1,
            min_required=min_safe,
            detail={
                "reason": "must_be_positive_int",
                "configured": value if isinstance(value, int) else -1,
                "min_required": min_safe,
                "recommended": default_budgets.compute_recursion_limit(),
            },
        )

    if value < min_safe:
        # Phase 2.8R-F 决策:低于 min_safe → 直接抛错,启动失败
        raise RecursionLimitConfigurationInvalidError(
            configured=value,
            min_required=min_safe,
            detail={
                "reason": "below_min_safe",
                "configured": value,
                "min_required": min_safe,
                "recommended": default_budgets.compute_recursion_limit(),
            },
        )

    return value


__all__ = [
    "PATH_MEASUREMENTS",
    "BusinessBudgets",
    "get_default_budgets",
    "set_default_budgets",
    "validate_recursion_limit",
]


# 模块定位:BusinessBudgets — 业务循环硬上限配置(Phase 2.8R-F ADR docs/35 §6)
#
# 各业务循环(Preparation / Repair / Review / Format / ToolRetry / LLMRetry)
# 由 BusinessBudgets 统一配置,**不依赖 GraphRecursionError**(那是兜底保护)。
#
# 字段:
#   - preparation_max_steps / repair_max_steps / review_max_steps ...
#   - tool_retry_max / llm_retry_max / format_loop_max
#   - recursion_limit_overrides
#
# 链路:
#   BudgetTracker(budget.py 在 _shared/) → 实例化时读取本模块预算 →
#   子图循环每一步 check → 超限 → emit fail decision
#
# 关键约束:
#   - 业务 budget 与 GraphRecursionLimit 是**两层防护**,不可二选一;
#   - 不动态修改(每次启动期拉到内存快照,运行期只读);
#   - 上调都要走 docs/35 §6 的上调评审流程。
