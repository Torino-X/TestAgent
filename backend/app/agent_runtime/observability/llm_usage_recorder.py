"""LLMUsageRecorder — Phase 2.6 token 成本累加。

Wraps an LLMClient-like 对象,在每次 generate 调用后异步累加 token_usage_json
to ``agent_runs``:

  token_usage_json = {
    "by_profile": {
      "<profile_name>": {
        "prompt_tokens": N,
        "completion_tokens": M,
        "total_tokens": N + M
      },
      ...
    },
    "total": {
      "prompt_tokens": Σ,
      "completion_tokens": Σ,
      "total_tokens": Σ
    },
    "cost_estimate_usd": float   # 由 MODEL_COST_TABLE 计算
  }

MODEL_COST_TABLE 提供常见 provider/model 的每 1K token 单价;未知模型 fallback = $0.
不是 Prometheus 替代;只承接 RunMonitor.

实现注意:
  * 不读 LLMClient;仅拦截结果(字典协议);不会破坏 prompt payload
  * 异步 fire-and-forget;网络抖动不阻塞主路径
  * 失败仅 log,不抛
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from .run_monitor import RunMonitor

logger = logging.getLogger(__name__)


# ─── 内置成本表(USD per 1K tokens) ─────────────────────────────────────────
# 真实成本需要 provider 官方账单;此处提供保守默认供 dev/test 用。
# 任何未知 (provider, model) 组合 → fallback $0,不报错。

MODEL_COST_TABLE: dict[tuple[str, str], tuple[float, float]] = {
    ("openai", "gpt-4"): (0.03, 0.06),
    ("openai", "gpt-4-turbo"): (0.01, 0.03),
    ("openai", "gpt-4o"): (0.005, 0.015),
    ("openai", "gpt-4o-mini"): (0.00015, 0.0006),
    ("openai", "gpt-3.5-turbo"): (0.0005, 0.0015),
    ("anthropic", "claude-opus-4-8"): (0.015, 0.075),
    ("anthropic", "claude-sonnet-5"): (0.003, 0.015),
    ("anthropic", "claude-haiku-4-5"): (0.0008, 0.004),
    ("dashscope", "qwen-plus"): (0.0008, 0.002),
    ("dashscope", "qwen-turbo"): (0.0003, 0.0006),
    ("minimax", "minimax-m3"): (0.001, 0.002),
    # 提供外部"原始拼写" key,方便 user 在 record 时直接传未 normalize 的 model 名
    ("minimax", "MiniMax-M3"): (0.001, 0.002),
}


@dataclass
class CostBreakdown:
    prompt_cost_usd: float
    completion_cost_usd: float

    @property
    def total_cost_usd(self) -> float:
        return float(self.prompt_cost_usd + self.completion_cost_usd)


def estimate_cost(
    provider: str, model: str, prompt_tokens: int, completion_tokens: int
) -> CostBreakdown:
    """根据 MODEL_COST_TABLE 计算 USD 成本;未知模型 fallback $0."""
    table = MODEL_COST_TABLE.get(
        (str(provider or "").lower(), str(model or "").lower())
    )
    if not table:
        return CostBreakdown(0.0, 0.0)
    pp, cc = table
    return CostBreakdown(
        prompt_cost_usd=float(int(prompt_tokens)) / 1000.0 * pp,
        completion_cost_usd=float(int(completion_tokens)) / 1000.0 * cc,
    )


@dataclass
class RecorderHandle:
    """LLMUsageRecorder 返回的轻量句柄;caller 写入 _run_public_id 后即可。"""

    recorder: "LLMUsageRecorder"
    run_public_id: Optional[str] = None
    profile: str = "default"


class LLMUsageRecorder:
    """拦截 generate_* 调用的 token 计数 + 成本累加。

    用法:
        monitor = RunMonitor(session_factory=...)
        recorder = LLMUsageRecorder(monitor=monitor)
        handle = RecorderHandle(recorder=recorder, run_public_id="run_xxx", profile="chat")

        # 在 generator 之前:
        result = await original_generate(profile, content, ...)
        await recorder.record(
            profile="chat",
            prompt_tokens=result["usage"]["prompt_tokens"],
            completion_tokens=result["usage"]["completion_tokens"],
            provider="openai",
            model="gpt-4o",
            run_public_id="run_xxx",
        )
    """

    def __init__(
        self,
        *,
        monitor: RunMonitor,
        default_provider: str = "minimax",
        default_model: str = "MiniMax-M3",
    ) -> None:
        self._monitor = monitor
        self._default_provider = default_provider
        self._default_model = default_model

    async def record(
        self,
        *,
        profile: str,
        prompt_tokens: int,
        completion_tokens: int,
        run_public_id: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """累加 token + 成本到 ``agent_runs``;fire-and-forget。"""
        if not run_public_id:
            return
        try:
            await self._monitor.append_tokens(
                run_public_id=run_public_id,
                profile=str(profile),
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
            )
            cost = estimate_cost(
                provider=provider or self._default_provider,
                model=model or self._default_model,
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
            )
            from app.repositories.agent_run_repository import AgentRunRepository

            async with self._monitor._session_factory() as session:
                repo = AgentRunRepository(session)
                got = await repo.get_by_public_id(run_public_id)
                if got is None:
                    return
                await repo.accumulate_cost_estimate(
                    run_internal_id=int(got.id), delta_usd=cost.total_cost_usd
                )
        except Exception:
            logger.warning(
                "LLMUsageRecorder.record failed (swallowed)", exc_info=True
            )


__all__ = [
    "LLMUsageRecorder",
    "RecorderHandle",
    "MODEL_COST_TABLE",
    "estimate_cost",
    "CostBreakdown",
]
