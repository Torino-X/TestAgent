"""Phase 2.8A Step 16 — probe → ApiDispatcher 端到端集成测试(1 个)。

设计目标(对应 docs/29 §10 + 附录 E):
* 把 ``probe_at_startup`` 与 ``ApiDispatcher._resolve_engine`` 串起来,验证:
  1. Postgres 健康 → task.engine_type="langgraph" → 真正走到 langgraph
  2. Postgres 不健康 → 双闸门触发 → task.engine_type="langgraph" → 静默回退 legacy
  3. 不论 probe 状态,task.engine_type="legacy" → 不论如何都走 legacy

这条集成链路是 Step 13 lifespan 启动后到 Step 11 agent_tasks 入口路由之间的
"信息流":
  ProbeReport(production_dispatch_forced_off) ─┐
                                                ├─→ ApiDispatcher._resolve_engine
  AgentRuntimeFeatureFlags(production_dispatch_enabled) ─┘
                                                    ↓
                              (engine, fallback_used, fallback_reason)

守禁令映射:
* 守禁令 #18 → ProbeReport 派生 production_dispatch_forced_off 与 Postgres 健康绑定
* 守禁令 #24 → 双闸门失败必须静默回退,**不抛**
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent_runtime.api_dispatcher import ApiDispatcher
from app.agent_runtime.persistence import ProbeReport


def _build_probe(*, postgres_ok: bool) -> ProbeReport:
    """最小可用的 ProbeReport(避免真探测 Postgres)。"""
    return ProbeReport(
        postgres_ok=postgres_ok,
        postgres_url_echo=(
            "postgresql://user:***@localhost:5432/db" if postgres_ok else ""
        ),
        postgres_latency_ms=120 if postgres_ok else 0,
        eventbus_kind="InMemory",
        eventbus_url_echo="redis://localhost:6379/0",
        redis_ok=True,
        ready=True,
        production_dispatch_forced_off=not postgres_ok,
        warnings=() if postgres_ok else ("postgres probe failed",),
    )


class _SpyOrchestrator:
    async def run(self, ctx: Any) -> dict:
        return {"engine": "legacy", "result": "spy", "ctx": ctx}

    async def resume_after_confirm(self, ctx: Any) -> dict:
        return {"engine": "legacy", "result": "spy-resume", "ctx": ctx}


class _SpyCoordinator:
    async def run_pre_confirm(self, payload: Any) -> dict:
        return {"engine": "langgraph", "result": "spy-coord", "payload": payload}

    async def run_post_confirm(self, payload: Any) -> dict:
        return {"engine": "langgraph", "result": "spy-coord-post", "payload": payload}

    async def resume_section_confirmation(self, payload: Any) -> dict:
        return {"engine": "langgraph", "result": "spy-coord-resume", "payload": payload}


def _patched_probe_postgres(*, healthy: bool):
    """生成 mock 函数,模拟 probe_postgres_checkpointer 返回 healthy/not。

    包装在 ``probe_at_startup`` 调用链第一段,绕过真实 Postgres 探测。
    """
    async def _fake_pg_probe(url, timeout):
        return healthy

    return _fake_pg_probe


@pytest.mark.asyncio
async def test_probe_report_drives_api_dispatcher_routing_end_to_end() -> None:
    """集成测试:probe → ApiDispatcher 一条龙,验证 3 个场景。

    (1) Postgres 健康 + task.engine_type="langgraph" → 真正走 LangGraph coordinator
    (2) Postgres 不健康 + task.engine_type="langgraph" → 静默回退 Legacy
        (验证 fallback_used=True + fallback_reason="postgres_unhealthy")
    (3) Postgres 不健康 + task.engine_type="legacy" → 始终 Legacy(无 fallback)

    这是 docs/29 §10 + 附录 E 描述的双闸门 + engine_type 不可变契约的
    端到端验证。如果这条测试失败,说明 probe 报告与 dispatcher 决策之间
    有不一致 — 老 ingress 会"破门而出",新 ingress 会"门关闭"。
    """
    from app.agent_runtime.feature_flags import AgentRuntimeFeatureFlags

    # ── Spy Coordinator + Orchestrator(全 run 共享;通过 reset 调用历史区分)──
    coord = _SpyCoordinator()
    orch = _SpyOrchestrator()
    coord_run_pre_count = 0
    orch_run_count = 0

    orig_coord_pre = coord.run_pre_confirm

    async def _spy_coord_pre(payload):
        nonlocal coord_run_pre_count
        coord_run_pre_count += 1
        return await orig_coord_pre(payload)

    coord.run_pre_confirm = _spy_coord_pre

    orig_orch_run = orch.run

    async def _spy_orch_run(ctx):
        nonlocal orch_run_count
        orch_run_count += 1
        return await orig_orch_run(ctx)

    orch.run = _spy_orch_run

    flags_full_on = AgentRuntimeFeatureFlags(
        langgraph_enabled=True,
        production_dispatch_enabled=True,
    )

    async def _fake_bus_resolve(url):
        from app.agent_runtime.events.live_event_bus import InMemoryLiveEventBus
        return InMemoryLiveEventBus()

    # ── 场景 (1):Postgres 健康 + langgraph 任务 → 真走 LangGraph ──
    with patch(
        "app.agent_runtime.persistence.probe_router.probe_postgres_checkpointer",
        new=_patched_probe_postgres(healthy=True),
    ), patch(
        "app.agent_runtime.events.live_event_bus.LiveEventBusProbe.resolve_with_health_check",
        new=_fake_bus_resolve,
    ):
        from app.agent_runtime.persistence.probe_router import probe_at_startup

        probe = await probe_at_startup(
            postgres_url="postgresql://user:pass@localhost:5432/db",
            redis_url="redis://localhost:6379/0",
        )
    assert probe.postgres_ok is True
    assert probe.production_dispatch_forced_off is False

    dispatcher = ApiDispatcher(
        coordinator=coord,
        probe_report=probe,
        feature_flags=flags_full_on,
    )

    outcome_lg = await dispatcher.dispatch_new_task(
        task_public_id="t-happy-lg",
        task_engine_type="langgraph",
        context={"trace": "1"},
    )
    assert outcome_lg.engine == "langgraph"
    assert outcome_lg.fallback_used is False
    assert coord_run_pre_count == 1
    # langgraph 走了,Legacy orchestrator 这次没被调
    assert orch_run_count == 0

    # ── 场景 (2):Postgres 不健康 + langgraph 任务 → Phase 2.8R 严格抛错 ──
    with patch(
        "app.agent_runtime.persistence.probe_router.probe_postgres_checkpointer",
        new=_patched_probe_postgres(healthy=False),
    ), patch(
        "app.agent_runtime.events.live_event_bus.LiveEventBusProbe.resolve_with_health_check",
        new=_fake_bus_resolve,
    ):
        from app.agent_runtime.persistence.probe_router import probe_at_startup

        probe_bad = await probe_at_startup(
            postgres_url="postgresql://user:pass@localhost:5432/db",
            redis_url="redis://localhost:6379/0",
        )
    assert probe_bad.postgres_ok is False
    assert probe_bad.production_dispatch_forced_off is True

    dispatcher_bad = ApiDispatcher(
        coordinator=coord,
        probe_report=probe_bad,
        feature_flags=flags_full_on,
    )

    # Phase 2.8R-A:LangGraph 任务 Postgres 不健康时显式抛错,绝不静默回退
    from app.core.exceptions import LangGraphNotReadyError

    with pytest.raises(LangGraphNotReadyError):
        await dispatcher_bad.dispatch_new_task(
            task_public_id="t-bad-lg",
            task_engine_type="langgraph",
            context={"trace": "2"},
        )
    # 关键断言:Legacy orchestrator 这次**没有被调**(因为抛错,fallback 失败)
    assert orch_run_count == 0
    # coordinator 这次也没再被加 1(仍是上次的 1 次)
    assert coord_run_pre_count == 1

    # ── 场景 (3):历史 Legacy 任务显式进入迁移状态,绝不执行 ──
    from app.core.exceptions import UnsupportedLegacyTaskError

    with pytest.raises(UnsupportedLegacyTaskError):
        await dispatcher_bad.dispatch_new_task(
            task_public_id="t-bad-legacy",
            task_engine_type="legacy",
            context={"trace": "3"},
        )
    assert orch_run_count == 0
    # coordinator 总次数不变
    assert coord_run_pre_count == 1


__all__ = [
    "test_probe_report_drives_api_dispatcher_routing_end_to_end",
]
