"""Phase 2.8B — ``ProbeReport`` 新字段 + ``probe_at_startup`` Redis InFlight 子探测。

设计要点(对应 docs/30 §4 + ADR-2.8B-6):

* ``redis_inflight_ok`` / ``redis_inflight_url_echo`` / ``in_flight_lock_backend``
  / ``in_flight_lock_ttl_seconds`` 4 个新字段默认 False / "" / "InMemory" / 1800
* 空 URL 时 backend = "InMemory"(设计意图),ok = True
* fakeredis URL → 探测 OK,backend = "Redis"
* Postgres fail → production_dispatch_forced_off=True(沿用 2.8A)
* Redis InFlight fail → graceful degrade,加 WARN,不强制 off production
"""

from __future__ import annotations

import os
from typing import Any

import pytest

try:
    from fakeredis import aioredis as fakeredis_aioredis
    HAS_FAKEREDIS = True
except ImportError:  # pragma: no cover
    HAS_FAKEREDIS = False

from app.agent_runtime.persistence import ProbeReport, probe_at_startup


def test_probe_report_default_fields_phase_2_8b() -> None:
    """ProbeReport 默认字段 — Phase 2.8B 新增 4 字段都在,默认值正确。"""
    report = ProbeReport(
        postgres_ok=False,
        postgres_url_echo="",
        postgres_latency_ms=0,
        eventbus_kind="InMemory",
        eventbus_url_echo="",
        redis_ok=False,
        ready=False,
        production_dispatch_forced_off=True,
    )
    assert report.redis_inflight_ok is False
    assert report.redis_inflight_url_echo == ""
    assert report.in_flight_lock_backend == "InMemory"
    assert report.in_flight_lock_ttl_seconds == 1800


async def test_probe_at_startup_empty_redis_inflight_url_inmemory_backend() -> None:
    """空 redis_inflight_url → backend = "InMemory",ok = True(设计意图)。"""
    report = await probe_at_startup(
        postgres_url="",
        redis_url="",
        redis_inflight_url="",
        redis_inflight_ttl_seconds=600,
    )
    assert report.redis_inflight_ok is True
    assert report.redis_inflight_url_echo == ""
    assert report.in_flight_lock_backend == "InMemory"
    assert report.in_flight_lock_ttl_seconds == 600
    # 空 url 也算 ok(设计意图)
    assert "redis_inflight_lock" not in " ".join(report.warnings)


async def test_probe_at_startup_invalid_redis_inflight_url_degrade() -> None:
    """无效 redis URL → backend = "InMemoryDegraded",ok = False,WARN。"""
    # 使用一个不存在的主机 + 极短 connect timeout,触发失败
    report = await probe_at_startup(
        postgres_url="",
        redis_url="",
        # RFC 5737 TEST-NET-1 — 不可路由
        redis_inflight_url="redis://192.0.2.1:6379/0",
        redis_inflight_ttl_seconds=1800,
        postgres_timeout=0.5,
        overall_timeout=4.0,
    )
    assert report.redis_inflight_ok is False
    assert report.in_flight_lock_backend == "InMemoryDegraded"
    # WARN 触发
    assert any("Redis InFlight" in w for w in report.warnings)


async def test_probe_at_startup_postgres_fail_forces_prod_off() -> None:
    """Postgres fail → production_dispatch_forced_off=True(沿用 2.8A 守禁令 #18)。"""
    report = await probe_at_startup(
        postgres_url="postgresql://user:pw@192.0.2.1:5432/db",
        redis_url="",
        postgres_timeout=0.5,
        overall_timeout=3.0,
    )
    assert report.postgres_ok is False
    assert report.production_dispatch_forced_off is True
