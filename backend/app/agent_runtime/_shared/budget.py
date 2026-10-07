"""BudgetTracker — hard caps for dynamic-agent loops (Phase 2.4 shared).

Shared by:
* Preparation Agent (Phase 2.3): MAX_AGENT_STEPS=6, MAX_TOOL_CALLS=4, WALL=120,
  TOKEN=6000, MAX_SAME_ARGS=2
* Review Repair Agent (Phase 2.4): MAX_AGENT_STEPS=8, MAX_TOOL_CALLS=6, WALL=120,
  TOKEN=8000, MAX_SAME_ARGS=2

本模块是 Phase 2.3 ``preparation/budget.py:1-114`` 的字节级迁移;
所有上限触达立刻 raise ``BudgetExceeded``, agent_loop 必须 catch 并走 fallback。

防 Risk #1 (LLM 循环烧 token)、Risk #4 (checkpoint 膨胀)。
"""

from __future__ import annotations

import time
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field


# ── Budget snapshot (Pydantic, JSON serializable) ────────────────────────


class BudgetState(BaseModel):
    """预算快照,append 到 *_steps 与最终 *result。"""

    model_config = ConfigDict(extra="forbid")

    steps: int
    tool_calls: int
    wall_seconds: float
    token_estimate: int
    repeated_tool_calls: int


# ── Exception ─────────────────────────────────────────────────────────────


class BudgetExceeded(Exception):
    """任意上限触达时抛出。code 标识具体超限项。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


# ── Tracker ───────────────────────────────────────────────────────────────


class BudgetTracker:
    """单次 dynamic-agent 运行的预算追踪。

    通过 ``wall_clock_fn`` 注入 wall time (默认 time.monotonic)。
    ``token_estimate`` 由调用方按 LLM 返回的 usage 字段累计。

    上限由构造参数注入;类属性只声明默认 (与 Phase 2.3 字节级一致)。
    """

    # Defaults: Phase 2.3 preparation shape
    MAX_AGENT_STEPS: int = 6
    MAX_TOOL_CALLS: int = 4
    MAX_WALL_TIME_SECONDS: float = 120.0
    MAX_TOKEN_ESTIMATE: int = 6000
    MAX_SAME_TOOL_SAME_ARGS: int = 2

    def __init__(
        self,
        *,
        wall_clock_fn: Callable[[], float] = time.monotonic,
        max_steps: int = MAX_AGENT_STEPS,
        max_tool_calls: int = MAX_TOOL_CALLS,
        max_wall_seconds: float = MAX_WALL_TIME_SECONDS,
        max_token_estimate: int = MAX_TOKEN_ESTIMATE,
    ):
        self._now = wall_clock_fn
        self._t0 = self._now()
        self.steps = 0
        self.tool_calls = 0
        self.token_estimate = 0
        self.repeated_tool_calls = 0
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.max_wall_seconds = max_wall_seconds
        self.max_token_estimate = max_token_estimate

    # ── counters ──────────────────────────────────────────────────────

    def on_step(self) -> None:
        self.steps += 1
        self.check()

    def on_tool_call(self) -> None:
        self.tool_calls += 1
        self.check()

    def on_token_estimate(self, n: int) -> None:
        self.token_estimate += max(0, int(n))
        self.check()

    def record_repeat(self) -> None:
        self.repeated_tool_calls += 1

    # ── checks ────────────────────────────────────────────────────────

    def check(self) -> None:
        if self.steps > self.max_steps:
            raise BudgetExceeded("max_steps", f"steps={self.steps} > {self.max_steps}")
        if self.tool_calls > self.max_tool_calls:
            raise BudgetExceeded(
                "max_tool_calls",
                f"tool_calls={self.tool_calls} > {self.max_tool_calls}",
            )
        wall = self._now() - self._t0
        if wall > self.max_wall_seconds:
            raise BudgetExceeded(
                "max_wall_time",
                f"wall={wall:.1f}s > {self.max_wall_seconds}s",
            )
        if self.token_estimate > self.max_token_estimate:
            raise BudgetExceeded(
                "max_token_estimate",
                f"tokens={self.token_estimate} > {self.max_token_estimate}",
            )

    # ── snapshot ──────────────────────────────────────────────────────

    def snapshot(self) -> BudgetState:
        wall = self._now() - self._t0
        return BudgetState(
            steps=self.steps,
            tool_calls=self.tool_calls,
            wall_seconds=round(wall, 3),
            token_estimate=self.token_estimate,
            repeated_tool_calls=self.repeated_tool_calls,
        )


__all__ = ["BudgetTracker", "BudgetExceeded", "BudgetState"]

# 模块定位:BudgetTracker — 动态 Agent 硬上限
#
# Shared by:
#   * Preparation Agent  (Phase 2.3): MAX_AGENT_STEPS=6 / MAX_TOOL_CALLS=4 /
#     WALL=120s / TOKEN=6000 / MAX_SAME_ARGS=2
#   * Review Repair     (Phase 2.4): 同上(让 Repair 与 Prep 一致)
#
# 链路:
#   BudgetTracker.step_counter()  / .register_tool_call() / .check_wall() /
#   .check_token() / .bump_same_args()
#   → 任意超限 raise BudgetExceeded → 子图 catch → emit + 落库 fail decision
#
# 关键约束:
#   - BudgetExceeded 必须是 soft-fail(recoverable, 不会把任务推到 fail_task),
#     上层 agent_loop 必须把这个当"模型 + retry 边界"用,而非"硬错误";
#   - 单子图局部 budget,跨子图不共享;
#   - wall 时钟由 budget.clock() 提供,不依赖 datetime.now()(便于测试)。
