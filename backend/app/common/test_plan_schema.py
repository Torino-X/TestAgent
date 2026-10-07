"""Test plan schema — adapted from legacy test_plan_schema.py.

Defines the default 10 test-plan sections and a completeness check result.

Equivalent migration: all capabilities preserved.
"""

from dataclasses import dataclass, field
from typing import List

DEFAULT_SECTIONS = [
    "项目概述",
    "测试目标",
    "测试范围",
    "测试策略",
    "测试环境",
    "测试资源",
    "测试进度",
    "准入准出标准",
    "风险分析",
    "测试交付物",
]


@dataclass
class CompletenessResult:
    """Result of a completeness check against the default section set."""

    required_sections: List[str] = field(
        default_factory=lambda: DEFAULT_SECTIONS.copy()
    )
    missing_sections: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.missing_sections


# 模块定位:Test plan schema 默认值(从 legacy test_plan_schema.py 迁移)
#
# 定义:
#   - DEFAULT_SECTIONS(10 个标准测试方案章节)
#   - CompletenessCheckResult(Pydantic 模型)
#
# 链路:
#   ResultReviewTool 一开始读 DEFAULT_SECTIONS 校验生成的章节集合;
#   完整字段名 / schema 注释由本模块给出。
#
# 关键约束:
#   - DEFAULT_SECTIONS 是 10 元素定长顺序,**不能改** ——
#     改了会破坏向后兼容的字段名 + 字段顺序假设;
#   - 不要往本模块加 LLM 逻辑(纯常量);
#   - 新增字段请走 build_review_standard(template_section_service)。
