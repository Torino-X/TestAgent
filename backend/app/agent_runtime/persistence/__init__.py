"""LangGraph checkpointer 工厂 — Phase 2.0 内存 + Phase 2.8A Postgres + Phase 2.8R-C 严格化。"""

from __future__ import annotations

# Phase 2.0 + 2.8R-C: 内存 + Postgres 双模式
from .checkpointer_factory import (
    aclose_checkpointer,
    build_checkpointer,
    build_inmemory_checkpointer,
    build_postgres_checkpointer,
)

# Phase 2.8A: Postgres Checkpointer 工厂 + 探测
from .postgres_checkpointer import (  # noqa: E402
    aclose_postgres_checkpointer,
    probe_postgres_checkpointer,
)
# Phase 2.8A: lifespan 启动期统一探测(Postgres + LiveEventBus)
from .probe_router import (  # noqa: E402
    ProbeReport,
    probe_at_startup,
)

# Phase 2.8B: 跨 worker InFlight Redis lock(独立模块,延迟 import)
from .redis_inflight_registry import (  # noqa: E402
    InflightOwner,
    REDIS_INFLIGHT_KEY_PREFIX,
    RedisInFlightRegistry,
)

# Phase 2.8B: Postgres 实证 helpers(verify 4 表 + round-trip + count)
from .postgres_integration import (  # noqa: E402
    EXPECTED_CORE_TABLES,
    count_checkpoints,
    roundtrip_checkpoint,
    verify_postgres_4_tables,
)

# Phase 2.8B: 跨 worker checkpoint 恢复辅助(运维 opt-in)
from .cross_worker_recovery import (  # noqa: E402
    RecoveryReport,
    release_orphaned_lock,
    scan_recoverable_checkpoints,
)

__all__ = [
    # 2.0 + 2.8R-C
    "build_inmemory_checkpointer",
    "build_postgres_checkpointer",
    "build_checkpointer",
    "aclose_checkpointer",
    # 2.8A
    "probe_postgres_checkpointer",
    "aclose_postgres_checkpointer",
    "ProbeReport",
    "probe_at_startup",
    # 2.8B
    "InflightOwner",
    "REDIS_INFLIGHT_KEY_PREFIX",
    "RedisInFlightRegistry",
    "EXPECTED_CORE_TABLES",
    "count_checkpoints",
    "roundtrip_checkpoint",
    "verify_postgres_4_tables",
    "RecoveryReport",
    "release_orphaned_lock",
    "scan_recoverable_checkpoints",
]
