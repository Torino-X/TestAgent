"""Shared infrastructure for Preparation / Repair dynamic agents (Phase 2.4).

ADR-2.4-1: ``_shared/`` 是 ``preparation/`` 与 ``repair/`` 的公共信源,
包含 BudgetTracker / ToolPermissionGuard / args_signature / ``_to_fail_decision`` 工厂。

Migration notes:
* Phase 2.3 的 ``preparation/budget.py`` / ``preparation/permission.py`` /
  ``preparation/prompt.py::args_signature`` / ``preparation/tool_filter.py::_to_fail_decision``
  在 Phase 2.4 改为 re-export 自本包,行为字节级一致。
* Phase 2.4 的 ``repair/`` 子包直接消费本包,不复用 preparation。
"""

from __future__ import annotations

from app.agent_runtime._shared.args_signature import args_signature
from app.agent_runtime._shared.budget import BudgetExceeded, BudgetTracker
from app.agent_runtime._shared.permission import (
    PermanentPermissionDenied,
    ToolPermissionDenied,
    ToolPermissionGuard,
)
from app.agent_runtime._shared.to_fail_decision import make_to_fail_decision

__all__ = [
    "BudgetTracker",
    "BudgetExceeded",
    "ToolPermissionGuard",
    "ToolPermissionDenied",
    "PermanentPermissionDenied",
    "args_signature",
    "make_to_fail_decision",
]

# ════════════════════════════════════════════════════════════════════════════════
# Shared 公共契约包说明 (Phase 2.4 ADR-2.4-1):
#
# 本目录集中以下公共基础设施给 Preparation / Repair 子图复用,避免两个子图
# 各自重复实现同一逻辑:
#
#   - BudgetTracker(budget.py): 动态 Agent 调用循环的硬上限(步数 / 工具次数 /
#     wall 时钟 / token 数 / 重复 args),超限 → 直接终止子图并返回 fail;
#   - ToolPermissionGuard(permission.py): 工具白名单 + 重复调用 guard,
#     不允许的 call 被拒绝,重复 args 直接归并到 decision;
#   - args_signature(args_signature.py): tool args 的稳定 12 字符 SHA 前缀,
#     用于审计而非 raw LLM 文本(Rule 11);
#   - make_to_fail_decision(to_fail_decision.py): 工厂方法,
#     把 Permission / Budget 拒绝统一映射到 action=fail 的 decision 类;
#   - public_narrative(public_narrative.py): 公开叙事合同(Phase 2.9B),
#     不带模型客户端 / 隐蔽推理,只翻译"Agent 决定要暴露的事实"为小的事件 envelope;
#   - summary_facts(summary_facts.py): 摘要字段的唯一事实构造器,
#     Generation / Review / Repair 都用它拿一致数字;
#   - artifact_contract(子包): artifact {public_id, ...} 的标准化 + 校验;
#   - narrative_governance(子包): narrative 治理(compressor / dedup / quality
#     / signature / cache / emitter_adapter)。
#
# 读 _shared/ 时的心智:
#   - preparation/ 与 repair/ 都从这层 import ——
#     修改这里 1 行就同时影响两个子图;
#   - 这里任何"可改"的字段都是公共契约,要慎重(会让两个子图一起改)。
