"""ToolPermissionGuard — shared whitelist + repeat-guard (Phase 2.4).

Shared by Preparation (Phase 2.3) and Review Repair (Phase 2.4) agents.

Phase 2.3 行为字节级保留:
* 构造接受任意 whitelist (默认 PREPARATION 的单工具集)
* max_repeat=2, fail_fast_after=2
* ``authorize(tool_name, args_signature)`` 抛:
  - ``ToolPermissionDenied`` (单次拦截)
  - ``PermanentPermissionDenied`` (fail-fast 触发)

Rule 12: 不允许未经白名单校验直接执行模型选择的工具 — 由本模块强制。
``TestAgentToolAdapter.execute()`` 内部 9 工具白名单是第二道防线。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, FrozenSet


# ── 默认白名单 (Phase 2.3 PREPARATION 单工具;Phase 2.4 可注入 REPAIR 三工具集) ──

DEFAULT_PREPARATION_WHITELIST: FrozenSet[str] = frozenset({
    "KnowledgeSearchTool",
})


# ── 异常 ─────────────────────────────────────────────────────────────────


class ToolPermissionDenied(Exception):
    """tool_name 不在白名单,或参数 schema 校验失败。"""


class PermanentPermissionDenied(Exception):
    """同一工具同参数重复次数 ≥ MAX_SAME_TOOL_SAME_ARGS,触发 fail-fast。"""


# ── Guard ────────────────────────────────────────────────────────────────


class ToolPermissionGuard:
    """维护 (tool_name, args_signature) → call_count 表。

    同 (tool, sig) 重复 ≥ max_repeat 次 → PermanentPermissionDenied
    总违规次数 ≥ fail_fast_after → PermanentPermissionDenied
    """

    def __init__(
        self,
        *,
        whitelist: FrozenSet[str] = DEFAULT_PREPARATION_WHITELIST,
        max_repeat: int = 2,
        fail_fast_after: int = 2,
    ):
        # 白名单是 kw-only,默认 Phase 2.3 PREPARATION 单工具集(向后兼容)
        self._whitelist = frozenset(whitelist)
        self._max_repeat = max_repeat
        self._fail_fast_after = fail_fast_after
        self._call_counts: Dict[tuple, int] = defaultdict(int)
        self._violation_count = 0

    @property
    def violation_count(self) -> int:
        return self._violation_count

    def is_permanently_denied(self) -> bool:
        return self._violation_count >= self._fail_fast_after

    def authorize(self, tool_name: str, args_signature: str) -> None:
        """校验 (tool, sig) 是否允许再调一次。失败抛对应异常。

        Args:
            tool_name: 模型请求的工具名
            args_signature: 12-char SHA prefix (shared.args_signature)
        """
        if tool_name not in self._whitelist:
            self._violation_count += 1
            if self.is_permanently_denied():
                raise PermanentPermissionDenied(
                    f"tool {tool_name!r} not in whitelist "
                    f"({sorted(self._whitelist)}); "
                    f"violation_count={self._violation_count}, "
                    f"fail-fast after {self._fail_fast_after}"
                )
            raise ToolPermissionDenied(
                f"tool {tool_name!r} not in whitelist "
                f"({sorted(self._whitelist)})"
            )

        key = (tool_name, args_signature)
        self._call_counts[key] += 1
        if self._call_counts[key] >= self._max_repeat + 1:
            # 第 3 次及以后被拒
            self._violation_count += 1
            if self.is_permanently_denied():
                raise PermanentPermissionDenied(
                    f"tool {tool_name!r} with args_sig {args_signature!r} "
                    f"called {self._call_counts[key]} times; "
                    f"max_repeat={self._max_repeat}, fail-fast after "
                    f"{self._fail_fast_after} violations"
                )
            raise ToolPermissionDenied(
                f"tool {tool_name!r} with args_sig {args_signature!r} "
                f"called {self._call_counts[key]} times (max {self._max_repeat})"
            )


__all__ = [
    "DEFAULT_PREPARATION_WHITELIST",
    "ToolPermissionDenied",
    "PermanentPermissionDenied",
    "ToolPermissionGuard",
]

# 模块定位:ToolPermissionGuard — 子图共享的白名单 + 重复 guard
#
# Shared by Preparation (Phase 2.3) and Review Repair (Phase 2.4) agents.
#
# 链路:
#   ToolPermissionGuard.check(tool_name, args, args_signature, same_args_counter)
#     → Allow / Reject(too-many-call) / Reject(arg-replay)
#   Reject 决策由 make_to_fail_decision 工厂转 action=fail 的 Decision 行,
#   decision_filter 在循环里立刻消费。
#
# 关键约束:
#   - 不在 allow 路径写日志(避免日志爆炸);只在 reject 路径 warn;
#   - 同 args_signature 重复 N 次直接 reject(阻止 prompt injection 触发的
#     无限循环);
#   - 工具白名单是 per-agent(Preparation 白名单 ≠ Repair 白名单);
#   - 与 budget.shared / narrative_governance.cache 互不冲突。
