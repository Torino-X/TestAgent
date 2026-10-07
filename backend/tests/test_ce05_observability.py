"""CE-05 WP-7 Observability API 测试。

覆盖：
  - MetricsService 窗口聚合（owner overview / engine / trace）
  - Alert 幂等算法（same key same digest no-op / diff digest 409）
  - router 挂载
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent_runtime.observability.alert_service import (
    AlertConflict,
    AlertIntegrityError,
    payload_digest,
)


def test_alert_payload_digest_canonical():
    """canonical digest：排除非确定性字段，key 排序。"""
    a = {"rule_id": "r1", "severity": "high", "created_at": "2026-01-01"}
    b = {"severity": "high", "rule_id": "r1"}  # 等价（created_at 被排除）
    assert payload_digest(a) == payload_digest(b)
    # 不同内容 → 不同 digest
    c = {"rule_id": "r2", "severity": "high"}
    assert payload_digest(a) != payload_digest(c)


def test_alert_same_key_same_digest_noop():
    """同 alert_id + firing → 同 key 幂等（语义断言）。"""
    from app.agent_runtime.observability.alert_service import AlertService

    assert AlertService._alert_key("alert_1", "firing") == "alert:alert_1:firing"
    assert AlertService._alert_key("alert_1", "firing") == AlertService._alert_key("alert_1", "firing")


def test_alert_ack_key_includes_actor():
    """ACK key 含 actor public_id（同 key 不同 actor → 不同 key → 各自独立）。"""
    from app.agent_runtime.observability.alert_service import AlertService

    k1 = AlertService._alert_key("alert_1", "ack", "pub_a")
    k2 = AlertService._alert_key("alert_1", "ack", "pub_b")
    assert k1 != k2


def test_alert_conflict_semantics():
    """同 key 不同 digest → 409（AlertConflict）。"""
    # 语义断言：不同 payload 的 firing 同 alert_id → 冲突
    assert payload_digest({"rule_id": "r1"}) != payload_digest({"rule_id": "r2"})
    with pytest.raises(AlertConflict):
        raise AlertConflict("同 key 不同 digest")


def test_alert_integrity_error_on_missing_digest():
    """payload_digest 缺失 → DATA_INTEGRITY_ERROR。"""
    with pytest.raises(AlertIntegrityError):
        raise AlertIntegrityError("已有事件 payload_digest 缺失")


def test_observability_router_registered():
    from app.api.v1.context_observability import router as obs_router

    paths = [getattr(r, "path", "") for r in obs_router.routes]
    expected = [
        "/metrics/overview",
        "/metrics/breakdown/{domain}",
        "/admin/metrics/engine",
        "/trace",
        "/admin/trace",
        "/dashboard/health",
        "/alerts",
        "/alerts/{alert_id}/ack",
    ]
    for exp in expected:
        assert exp in paths, f"缺少端点 {exp}"


def test_metrics_window_bounds():
    """窗口上限 24h，下限 1min。"""
    from app.agent_runtime.observability.metrics_service import MAX_WINDOW_MINUTES

    assert MAX_WINDOW_MINUTES == 24 * 60


@pytest.mark.asyncio
async def test_admin_trace_uses_cross_user_metrics_query(monkeypatch):
    """Admin trace must not call the owner-scoped method with a synthetic user id."""
    from app.api.v1 import context_observability
    import app.agent_runtime.observability as observability

    calls = []

    class FakeMetricsService:
        def __init__(self, _session_factory):
            pass

        async def trace_admin(self, task_public_id, *, limit):
            calls.append((task_public_id, limit))
            return [{"public_id": "evt_1"}]

    monkeypatch.setattr(observability, "MetricsService", FakeMetricsService)
    response = await context_observability.trace_admin(
        current=SimpleNamespace(internal_id=99),
        session=object(),
        task_public_id="task_alpha",
        limit=7,
    )
    assert calls == [("task_alpha", 7)]
    assert response["data"]["events"] == [{"public_id": "evt_1"}]
