"""RecursionLimitConfig — Phase 2.8R-F env-driven override。

设计要点(对应 docs/35 §6.3):
  * 默认值由 ``BusinessBudgets.compute_recursion_limit()`` 计算(37 步)。
  * 环境变量 ``AGENT_RUNTIME_RECURSION_LIMIT`` 可覆盖;非正整数 / 低于
    min_safe → 启动期 fail-fast(``RecursionLimitConfigurationInvalidError``)。
  * ``from_env_or_default()`` 单调用入口,生产代码应走这个。

不在范围:BusinessBudgets 各字段(preparation_max / repair_max 等)也允许
env 覆盖;但是这些字段改动是细粒度的,不在 2.8R-F 直击范围,留后续阶段。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from app.agent_runtime.business_budgets import (
    BusinessBudgets,
    get_default_budgets,
    validate_recursion_limit,
)

logger = logging.getLogger(__name__)


def resolve_recursion_limit(
    budgets: BusinessBudgets | None = None,
    *,
    env_var: str = "AGENT_RUNTIME_RECURSION_LIMIT",
) -> int:
    """从 env 读覆盖值,否则用 budgets 计算的默认值。

    Args:
        budgets: 业务预算(默认 singleton)
        env_var: env 变量名

    Returns:
        校验后的正整数。

    Raises:
        RecursionLimitConfigurationInvalidError: env 值无效
    """
    raw = os.environ.get(env_var)
    b = budgets or get_default_budgets()

    if raw is None:
        computed = b.compute_recursion_limit()
        logger.info(
            "RecursionLimitConfig: %s=auto → recursion_limit=%d",
            env_var, computed,
        )
        return computed

    try:
        parsed = int(raw.strip())
    except (TypeError, ValueError) as exc:
        from app.core.exceptions import RecursionLimitConfigurationInvalidError

        raise RecursionLimitConfigurationInvalidError(
            configured=-1,
            min_required=b.compute_recursion_limit(),
            detail={
                "reason": "env_value_not_int",
                "env_var": env_var,
                "raw_value": raw[:64],
                "recommended": b.compute_recursion_limit(),
            },
        ) from exc

    validated = validate_recursion_limit(parsed)
    logger.info(
        "RecursionLimitConfig: %s=%d → recursion_limit=%d",
        env_var, parsed, validated,
    )
    return validated


__all__ = [
    "resolve_recursion_limit",
]


# 模块定位:Recursion Limit 配置加载(Phase 2.8R-F)
#
# 默认值由 BusinessBudgets.compute_recursion_limit() 计算 ≈ 37 步。
# 环境变量 AGENT_RUNTIME_RECURSION_LIMIT 可覆盖;非法值启动期 fail-fast。
#
# 链路:
#   startup → from_env_or_default() → 校验 → 注入 LangGraphRunCoordinator
#   → LangGraphCompiledGraph 编译时设 recursion_limit 参数。
#
# 关键约束:
#   - 不是为了卡住主图,而是防 Python while 误改死循环;
#   - 真实业务 budget 用 BusinessBudgets 单独跟踪;
#   - 任何 sub_min(<=10) → 启动 fail,不接受隐式默认。
