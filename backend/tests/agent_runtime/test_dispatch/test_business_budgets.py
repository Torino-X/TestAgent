"""Phase 2.8R-F — BusinessBudgets + recursion_limit 测试(5)。

设计目标(对应 docs/35 §6 验收八):
  * BusinessBudgets.max_legal_observed 返回正确值
  * compute_recursion_limit = max_legal + safe_margin
  * safe_margin 计算正确(20% or floor 5)
  * validate_recursion_limit 拒绝 too-low / 非正整数
  * resolve_recursion_limit 从 env 读 OK
"""

from __future__ import annotations

import pytest

from app.agent_runtime.business_budgets import (
    PATH_MEASUREMENTS,
    BusinessBudgets,
    get_default_budgets,
    set_default_budgets,
    validate_recursion_limit,
)


def test_business_budgets_default_compute_recursion_limit():
    """默认 budgets.compute_recursion_limit > max_legal_observed。"""
    budgets = BusinessBudgets()
    legal = budgets.max_legal_observed()
    computed = budgets.compute_recursion_limit()

    assert legal > 25, f"PATH_MEASUREMENTS max unexpected: {legal}"
    assert computed > legal, f"recursion_limit {computed} must be > max_legal {legal}"
    assert computed == legal + budgets.safe_margin()


def test_safe_margin_is_floor_or_percent():
    """safe_margin = max(5, max_legal * 20% // 100)。"""
    budgets = BusinessBudgets()
    legal = budgets.max_legal_observed()

    expected = max(5, legal * 20 // 100)
    assert budgets.safe_margin() == expected


def test_path_measurements_match_design_section_6_4():
    """PATH_MEASUREMENTS 包含 §6.4 矩阵所有 11 路径。"""
    expected = {
        "A_shortest_success",
        "B_prep_once",
        "C_prep_max",
        "D_review_fail_repair",
        "E_repair_max",
        "F_format_loss",
        "G_llm_retry",
        "H_tool_retry",
        "I_incremental",
        "J_interrupt_resume",
        "K_resume_continue",
    }
    actual = set(PATH_MEASUREMENTS.keys())
    assert expected.issubset(actual), f"missing: {expected - actual}"


def test_validate_recursion_limit_accepts_valid_value():
    """合法值通过校验。"""
    budgets = BusinessBudgets()
    computed = budgets.compute_recursion_limit()

    # 应该返回相同值
    assert validate_recursion_limit(computed) == computed


def test_validate_recursion_limit_rejects_below_min(monkeypatch):
    """低于 min_safe → RecursionLimitConfigurationInvalidError。"""
    from app.core.exceptions import RecursionLimitConfigurationInvalidError

    budgets = BusinessBudgets()
    legal = budgets.max_legal_observed()

    with pytest.raises(RecursionLimitConfigurationInvalidError) as exc_info:
        validate_recursion_limit(legal - 1)

    # configured / min_required 在 detail,reason + configured 字段必含
    assert exc_info.value.detail["reason"] == "below_min_safe"
    assert exc_info.value.detail["configured"] == legal - 1
    assert exc_info.value.detail["min_required"] == legal


def test_validate_recursion_limit_rejects_zero_or_negative():
    """非正整数(0 / 负) → RecursionLimitConfigurationInvalidError。"""
    from app.core.exceptions import RecursionLimitConfigurationInvalidError

    for bad in (0, -1, -100):
        with pytest.raises(RecursionLimitConfigurationInvalidError) as exc_info:
            validate_recursion_limit(bad)
        assert exc_info.value.detail["reason"] in {
            "must_be_positive_int",
            "below_min_safe",
        }


def test_validate_recursion_limit_none_returns_default(monkeypatch):
    """None → 默认 budgets 计算。"""
    result = validate_recursion_limit(None)
    budgets = BusinessBudgets()
    assert result == budgets.compute_recursion_limit()


def test_resolve_recursion_limit_env_override(monkeypatch):
    """env 覆盖 → 校验通过。"""
    from app.agent_runtime.recursion_limit_config import resolve_recursion_limit

    monkeypatch.setenv("AGENT_RUNTIME_RECURSION_LIMIT", "100")
    assert resolve_recursion_limit() == 100


def test_resolve_recursion_limit_env_invalid_raises(monkeypatch):
    """env 含非数字 → fail-fast。"""
    from app.agent_runtime.recursion_limit_config import resolve_recursion_limit
    from app.core.exceptions import RecursionLimitConfigurationInvalidError

    monkeypatch.setenv("AGENT_RUNTIME_RECURSION_LIMIT", "not-a-number")
    with pytest.raises(RecursionLimitConfigurationInvalidError) as exc_info:
        resolve_recursion_limit()
    assert exc_info.value.detail["reason"] == "env_value_not_int"


def test_resolve_recursion_limit_no_env_uses_default(monkeypatch):
    """无 env → budgets.compute_recursion_limit。"""
    from app.agent_runtime.recursion_limit_config import resolve_recursion_limit

    monkeypatch.delenv("AGENT_RUNTIME_RECURSION_LIMIT", raising=False)
    budgets = BusinessBudgets()
    assert resolve_recursion_limit() == budgets.compute_recursion_limit()


def test_singleton_setter_roundtrip():
    """set_default_budgets / get_default_budgets 双向存读。"""
    custom = BusinessBudgets(review_max=99, format_max=88)
    set_default_budgets(custom)
    assert get_default_budgets() is custom

    # 还原
    set_default_budgets(BusinessBudgets())


__all__ = [
    "test_business_budgets_default_compute_recursion_limit",
    "test_safe_margin_is_floor_or_percent",
    "test_path_measurements_match_design_section_6_4",
    "test_validate_recursion_limit_accepts_valid_value",
    "test_validate_recursion_limit_rejects_below_min",
    "test_validate_recursion_limit_rejects_zero_or_negative",
    "test_validate_recursion_limit_none_returns_default",
    "test_resolve_recursion_limit_env_override",
    "test_resolve_recursion_limit_env_invalid_raises",
    "test_resolve_recursion_limit_no_env_uses_default",
    "test_singleton_setter_roundtrip",
]
