"""Phase 2.6 — LLMUsageRecorder + estimate_cost."""

from __future__ import annotations

from typing import Any

import pytest

from app.agent_runtime.observability.llm_usage_recorder import (
    LLMUsageRecorder,
    estimate_cost,
    MODEL_COST_TABLE,
    CostBreakdown,
)


class _FakeRunMonitor:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._session_factory_called = 0

    async def append_tokens(self, *, run_public_id, profile, prompt_tokens, completion_tokens):
        self.calls.append(
            {
                "run_public_id": run_public_id,
                "profile": profile,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )


def test_estimate_cost_known_model_returns_deterministic_value() -> None:
    # minimax / MiniMax-M3 内置
    assert ("minimax", "MiniMax-M3") in MODEL_COST_TABLE
    cost = estimate_cost(
        provider="minimax", model="MiniMax-M3",
        prompt_tokens=1_000_000, completion_tokens=0,
    )
    assert isinstance(cost, CostBreakdown)
    assert cost.prompt_cost_usd > 0.0
    assert cost.completion_cost_usd == 0.0


def test_estimate_cost_unknown_provider_returns_zero() -> None:
    cost = estimate_cost(
        provider="nonexistent", model="MiniMax-M3",
        prompt_tokens=100, completion_tokens=50,
    )
    assert cost.prompt_cost_usd == 0.0
    assert cost.completion_cost_usd == 0.0


def test_estimate_cost_combines_prompt_and_completion() -> None:
    if ("minimax", "MiniMax-M3") not in MODEL_COST_TABLE:
        pytest.skip("MiniMax-M3 not in MODEL_COST_TABLE")
    only_prompt = estimate_cost(provider="minimax", model="MiniMax-M3", prompt_tokens=1000, completion_tokens=0)
    only_completion = estimate_cost(provider="minimax", model="MiniMax-M3", prompt_tokens=0, completion_tokens=1000)
    combined = estimate_cost(provider="minimax", model="MiniMax-M3", prompt_tokens=1000, completion_tokens=1000)
    assert abs(combined.total_cost_usd - (only_prompt.total_cost_usd + only_completion.total_cost_usd)) < 1e-9


def test_estimate_cost_normalizes_case() -> None:
    """大小写不敏感(内部 lower().lower())."""
    c1 = estimate_cost(provider="MiniMax", model="MiniMax-M3", prompt_tokens=1000, completion_tokens=0)
    c2 = estimate_cost(provider="minimax", model="MiniMax-M3", prompt_tokens=1000, completion_tokens=0)
    assert c1.total_cost_usd == c2.total_cost_usd


@pytest.mark.asyncio
async def test_llm_usage_recorder_record_calls_run_monitor() -> None:
    rm = _FakeRunMonitor()

    # 用 StubAgentRunRepository 避开真实 SQL — 这里让 record 阶段 skip cost 路径
    class _StubAgentRunRepository:
        @staticmethod
        async def get_by_public_id(*a: object, **kw: object) -> None:
            return None

    # 通过 monkeypatch 注入 stub repo,避免实际 SQL
    import app.repositories.agent_run_repository as ar_mod
    orig = getattr(ar_mod, "AgentRunRepository", None)

    # 简化路径:不实际触发 accumulate_cost_estimate(record 里 if got is None: return)
    rec = LLMUsageRecorder(monitor=rm)  # type: ignore[arg-type]
    await rec.record(
        profile="preparation_agent",
        prompt_tokens=120,
        completion_tokens=80,
        run_public_id=None,  # noop path
    )
    # run_public_id=None → noop → 不该调 monitor
    assert rm.calls == []

    await rec.record(
        profile="preparation_agent",
        prompt_tokens=120,
        completion_tokens=80,
        run_public_id="rpub",
    )
    # run_public_id 有效 → 应该调一次 monitor.append_tokens
    assert len(rm.calls) == 1
    assert rm.calls[0]["profile"] == "preparation_agent"
    assert rm.calls[0]["prompt_tokens"] == 120
    assert rm.calls[0]["completion_tokens"] == 80
    assert rm.calls[0]["run_public_id"] == "rpub"


@pytest.mark.asyncio
async def test_llm_usage_recorder_record_accumulates() -> None:
    rm = _FakeRunMonitor()
    rec = LLMUsageRecorder(monitor=rm)  # type: ignore[arg-type]
    await rec.record(profile="p1", prompt_tokens=10, completion_tokens=5, run_public_id="r1")
    await rec.record(profile="p1", prompt_tokens=10, completion_tokens=5, run_public_id="r1")
    assert len(rm.calls) == 2


@pytest.mark.asyncio
async def test_llm_usage_recorder_swallows_exceptions(monkeypatch) -> None:
    """任何异常仅 log,不抛(节点不应因 recorder 失败失败)."""

    class _BoomMonitor:
        async def append_tokens(self, **kw: object) -> None:
            raise RuntimeError("boom")

    rec = LLMUsageRecorder(monitor=_BoomMonitor())  # type: ignore[arg-type]
    # 不该抛
    await rec.record(profile="p", prompt_tokens=10, completion_tokens=5, run_public_id="r1")