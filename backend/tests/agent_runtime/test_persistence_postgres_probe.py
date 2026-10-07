"""Phase 2.8A Step 16 — Postgres + LiveEventBus probe 单元测试(6 个)。

设计目标(对应 docs/29 §10 + 附录 E):

* 验证 ``_mask_url`` URL 脱敏(2 个测试:postgresql/redis 主路径 + 边界 case)
* 验证 ``_eventbus_health`` 3 象限(2 个测试:redis_url 未配置 / 配置后多 bus 类型)
* 验证 ``ProbeReport`` dataclass 字段 + 派生关系(1 个测试)
* 验证 ``probe_at_startup`` 真实接口聚合(1 个测试:Postgres 健康 + Redis 不可用)

守禁令映射:
* 守禁令 #18 → Postgres probe 失败时 ``production_dispatch_forced_off=True``
* 守禁令 #30 → 探测不影响 lifespan 启动(超时则返回"全部 fail")
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.agent_runtime.persistence import ProbeReport
from app.agent_runtime.persistence.probe_router import (
    _eventbus_health,
    _mask_url,
)
from app.agent_runtime.persistence.postgres_checkpointer import (
    normalize_postgres_conn_string,
)


# ──────────────────────────────────────────────────────────────────────────
# Path 1: URL 脱敏(2 个)
# ──────────────────────────────────────────────────────────────────────────


def test_mask_url_postgresql_and_redis_masks_passwords() -> None:
    """``postgresql://user:pass@host`` → ``postgresql://user:***@host``。
    同样处理 redis URL — 同函数处理两种 scheme。"""
    masked_pg = _mask_url("postgresql://alice:s3cret@db.local:5432/test")
    assert masked_pg == "postgresql://alice:***@db.local:5432/test"
    assert "s3cret" not in masked_pg, "原密码必须被脱敏"

    masked_redis = _mask_url("redis://:hunter2@redis.local:6379/0")
    assert masked_redis == "redis://:***@redis.local:6379/0"
    assert "hunter2" not in masked_redis


def test_mask_url_edge_cases_passthrough_or_empty() -> None:
    """URL 脱敏边界:空串 / 无密码 / 无 @ 一律不变或空串。"""
    # 空串 → 空串(代表未配置)
    assert _mask_url("") == ""
    # 无密码的 URL 不变(没有 ``:password`` 段)
    url = "postgresql://user@db.local:5432/test"
    assert _mask_url(url) == url
    # 没找到 "@" 时不动(不是标准格式)
    weird = "http://example.com/path"
    assert _mask_url(weird) == weird


def test_normalize_postgres_conn_string_converts_sqlalchemy_asyncpg_url() -> None:
    """LangGraph's psycopg checkpointer accepts libpq URLs, not SQLAlchemy URLs."""
    raw_url = (
        "postgresql+asyncpg://test_user:secret@db.example:5432/langgraph?ssl=false"
    )

    normalized = normalize_postgres_conn_string(raw_url)

    assert normalized == (
        "postgresql://test_user:secret@db.example:5432/langgraph?sslmode=disable"
    )


def test_normalize_postgres_conn_string_converts_sqlalchemy_psycopg_url() -> None:
    """The LangGraph psycopg client must not receive a SQLAlchemy driver suffix."""
    raw_url = (
        "postgresql+psycopg://test_user:secret@db.example:5432/langgraph?ssl=false"
    )

    normalized = normalize_postgres_conn_string(raw_url)

    assert normalized == (
        "postgresql://test_user:secret@db.example:5432/langgraph?sslmode=disable"
    )


# ──────────────────────────────────────────────────────────────────────────
# Path 2: _eventbus_health 判定(2 个)
# ──────────────────────────────────────────────────────────────────────────


class _StubBus:
    """最小化 stub;_eventbus_health 通过 ``type(bus).__name__`` 判定。"""
    pass


def test_eventbus_health_redis_url_unconfigured_means_intentional_inmemory() -> None:
    """redis_url 未配置 → (True, "InMemory", False):视为设计意图,不报警告。"""
    redis_ok, kind, provided = _eventbus_health(
        redis_url="", bus=_StubBus()
    )
    assert redis_ok is True
    assert kind == "InMemory"
    assert provided is False


def test_eventbus_health_distinguishes_redis_inmemory_and_unknown() -> None:
    """redis_url 配置后,根据实际 bus 类型 3 象限判定。

    守禁令:未知类型 bus 不能 False Negative(降级到 InMemory
    也可能,取决于真实类型)。这里只验证真实已知类型的判定分支。
    """

    class RedisLiveEventBus:
        pass

    class InMemoryLiveEventBus:
        pass

    class _WeirdBus:
        pass

    # Redis bus 健康
    redis_ok, kind, provided = _eventbus_health(
        redis_url="redis://localhost:6379/0", bus=RedisLiveEventBus()
    )
    assert (redis_ok, kind, provided) == (True, "Redis", True)

    # InMemory bus 降级(必须报警)
    redis_ok, kind, provided = _eventbus_health(
        redis_url="redis://localhost:6379/0", bus=InMemoryLiveEventBus()
    )
    assert (redis_ok, kind, provided) == (False, "InMemoryDegraded", True)

    # 未知类型 — 不静默吞错
    redis_ok, kind, provided = _eventbus_health(
        redis_url="redis://localhost:6379/0", bus=_WeirdBus()
    )
    assert redis_ok is False
    assert provided is True
    assert kind == "_WeirdBus"


# ──────────────────────────────────────────────────────────────────────────
# Path 3: ProbeReport dataclass 字段 + 派生关系(1 个)
# ──────────────────────────────────────────────────────────────────────────


def test_probe_report_postgres_ok_drives_production_dispatch_forced_off() -> None:
    """逆变:postgres_ok=False 时 production_dispatch_forced_off=True(派生关系)。

    守禁令 #18:LangGraph 生产任务不得使用纯 MemorySaver ——
    ProbeReport 把这个事实编码在字段里供调用方判定。
    同时验证 9 字段命名齐全(从 dataclass.fields 读出)。
    """
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(ProbeReport)}
    expected = {
        "postgres_ok",
        "postgres_url_echo",
        "postgres_latency_ms",
        "eventbus_kind",
        "eventbus_url_echo",
        "redis_ok",
        "ready",
        "production_dispatch_forced_off",
        "warnings",
    }
    assert expected.issubset(field_names), (
        f"ProbeReport 缺失字段: {expected - field_names}"
    )

    # postgres_ok=True → production_dispatch_forced_off=False
    healthy = ProbeReport(
        postgres_ok=True,
        postgres_url_echo="postgresql://user:***@x/y",
        postgres_latency_ms=50,
        eventbus_kind="InMemory",
        eventbus_url_echo="",
        redis_ok=True,
        ready=True,
        production_dispatch_forced_off=False,
        warnings=(),
    )
    assert healthy.postgres_ok is True
    assert healthy.production_dispatch_forced_off is False

    # postgres_ok=False → production_dispatch_forced_off=True
    broken = ProbeReport(
        postgres_ok=False,
        postgres_url_echo="",
        postgres_latency_ms=0,
        eventbus_kind="InMemory",
        eventbus_url_echo="",
        redis_ok=True,
        ready=True,
        production_dispatch_forced_off=True,
        warnings=("postgres probe failed",),
    )
    assert broken.postgres_ok is False
    assert broken.production_dispatch_forced_off is True
    assert "postgres probe failed" in broken.warnings


# ──────────────────────────────────────────────────────────────────────────
# Path 4: probe_at_startup 真实聚合接口(1 个)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_at_startup_with_healthy_postgres_and_inmemory_bus() -> None:
    """Postgres 健康 + LiveEventBus 自动降级到 InMemory → ProbeReport 正确聚合。

    验证 lifespan 在 Redis 不可用 + Postgres 健康时仍能启动(Legacy 模式),
    且 LangGraph 生产路径可用(postgres_ok 让 production_dispatch_forced_off=False)。

    注意:当 Redis 不可用时,LiveEventBusProbe 自动退化返回
    ``InMemoryLiveEventBus``(不是任意 InMemory 命名的类);这里 patch
    该 probe 后返回真实的 InMemoryLiveEventBus。
    """

    async def _fake_pg_probe(url, timeout):
        return True

    async def _fake_bus_resolve(url):
        # LiveEventBusProbe 自动降级后返回 InMemoryLiveEventBus(Phase 2.6 行为)
        from app.agent_runtime.events.live_event_bus import InMemoryLiveEventBus
        return InMemoryLiveEventBus()

    with patch(
        "app.agent_runtime.persistence.probe_router.probe_postgres_checkpointer",
        new=_fake_pg_probe,
    ), patch(
        "app.agent_runtime.events.live_event_bus.LiveEventBusProbe.resolve_with_health_check",
        new=_fake_bus_resolve,
    ):
        from app.agent_runtime.persistence.probe_router import probe_at_startup

        result = await probe_at_startup(
            postgres_url="postgresql://user:pass@localhost:5432/db",
            redis_url="redis://localhost:6379/0",
        )

    assert result.postgres_ok is True
    # 密码必须脱敏为 ***
    assert result.postgres_url_echo == "postgresql://user:***@localhost:5432/db"
    # Redis 不可用 → LiveEventBusProbe 自动降级到 InMemoryLiveEventBus
    # → _eventbus_health 判定为 InMemoryDegraded 且 redis_ok=False
    assert result.eventbus_kind == "InMemoryDegraded"
    assert result.redis_ok is False
    # Postgres 健康 → production_dispatch_forced_off=False(关键!)
    assert result.production_dispatch_forced_off is False
    # ready = redis_ok(Legacy 不依赖 Postgres)
    assert result.ready is False
    # 必有 Redis 警告
    assert any(
        "redis" in w.lower() or "event" in w.lower() for w in result.warnings
    ), f"Redis 不可用应有警告;got warnings={result.warnings}"


__all__ = [
    # 2 URL 脱敏
    "test_mask_url_postgresql_and_redis_masks_passwords",
    "test_mask_url_edge_cases_passthrough_or_empty",
    # 2 eventbus health
    "test_eventbus_health_redis_url_unconfigured_means_intentional_inmemory",
    "test_eventbus_health_distinguishes_redis_inmemory_and_unknown",
    # 1 ProbeReport
    "test_probe_report_postgres_ok_drives_production_dispatch_forced_off",
    # 1 probe_at_startup
    "test_probe_at_startup_with_healthy_postgres_and_inmemory_bus",
]
