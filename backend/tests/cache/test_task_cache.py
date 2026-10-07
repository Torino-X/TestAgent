"""Tests for ``app.cache.domains.task_cache``.

Coverage (per prompt §22 / 设计文档 §13):
  * Spec invariants: TTL active vs terminal / negative / domain
  * TERMINAL_STATUSES membership
  * Key namespace format
  * TaskStatusCache get_or_load hit / miss / write_through (active → 180s, terminal → 6h)
  * TaskStatusCache invalidate
  * TaskDetailCache (terminal only) get_or_load / write_through / invalidate
  * Domain disabled → bypass
  * Backend disabled → bypass
  * Multi-worker consistency (worker A writes / worker B reads)
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.domains.task_cache import (
    TASK_STATUS_SPEC,
    TERMINAL_STATUSES,
    TaskDetailCache,
    TaskDetailDTO,
    TaskStatusCache,
    TaskStatusDTO,
    TaskStatusSpec,
    get_task_detail_cache,
    get_task_status_cache,
    set_task_detail_cache,
    set_task_status_cache,
    task_detail_key,
    task_status_key,
)
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


# ── helpers ────────────────────────────────────────────────────────


def _status_dto(status: str = "running", **overrides) -> TaskStatusDTO:
    base = dict(
        status=status,
        updated_at=datetime(2026, 9, 6, 12, 0, 0).isoformat(),
        active_run_id="run_xyz",
        terminal=status in TERMINAL_STATUSES,
    )
    base.update(overrides)
    return TaskStatusDTO(**base)


def _detail_dto(task_public_id: str = "task_test") -> TaskDetailDTO:
    return TaskDetailDTO(
        task_public_id=task_public_id,
        status="completed",
        user_id=42,
        conversation_id=7,
        task_type="test_plan_generation",
        title="Test task",
        error_code=None,
        error_message=None,
        started_at="2026-09-06T11:00:00",
        completed_at="2026-09-06T12:00:00",
        updated_at="2026-09-06T12:00:00",
        active_run_id="run_xyz",
    )


def _make_manager(fake_redis) -> CacheManager:
    backend = CacheBackend(redis_client=fake_redis)
    backend._healthy = True
    return CacheManager(
        backend=backend,
        breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=0.05)),
        bulkhead=DBBulkhead(max_concurrency=2),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,
    )


@pytest.fixture(autouse=True)
def _reset_singletons(monkeypatch) -> None:
    CacheBackend._instance = None
    cache_metrics.reset()
    from app.core.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "cache_task_enabled", True)
    yield
    CacheBackend._instance = None


# ── Spec invariants ────────────────────────────────────────────────


class TestSpecInvariants:
    def test_active_ttl_180s(self) -> None:
        """Per 设计文档 §13.1 — active TTL = 180s."""
        assert TASK_STATUS_SPEC.active_ttl_seconds == 180

    def test_terminal_ttl_6h(self) -> None:
        """Per 设计文档 §13.1 — terminal TTL = 6h."""
        assert TASK_STATUS_SPEC.terminal_ttl_seconds == 6 * 3600

    def test_negative_ttl_short(self) -> None:
        """Negative envelope must be short to allow heartbeat to re-check."""
        assert TASK_STATUS_SPEC.negative_ttl_seconds <= 30

    def test_domain_is_task(self) -> None:
        assert TASK_STATUS_SPEC.domain == "task"


class TestTerminalSet:
    def test_terminal_statuses_contains_expected(self) -> None:
        """Per 设计文档 §13.4 — terminal = completed / failed / cancelled."""
        assert "completed" in TERMINAL_STATUSES
        assert "failed" in TERMINAL_STATUSES
        assert "cancelled" in TERMINAL_STATUSES
        # Non-terminal status must NOT be in the set.
        assert "running" not in TERMINAL_STATUSES
        assert "waiting_user_confirm" not in TERMINAL_STATUSES
        assert "format_loss_review" not in TERMINAL_STATUSES


class TestKeyNamespace:
    def test_task_status_key(self) -> None:
        k = task_status_key("task_xyz")
        assert k.endswith(":task:status:task_xyz"), k

    def test_task_detail_key(self) -> None:
        k = task_detail_key("task_xyz")
        assert k.endswith(":task:detail:task_xyz"), k


# ── TaskStatusCache ───────────────────────────────────────────────


class TestTaskStatusCache:
    async def test_get_or_load_miss_then_hit(self, fake_redis) -> None:
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _status_dto(status="running")

        r1 = await cache.get_or_load_status("task_1", loader)
        r2 = await cache.get_or_load_status("task_1", loader)
        assert r1.status == "running"
        assert r2.status == "running"
        assert loader_calls["n"] == 1, "second call must hit cache"

    async def test_write_through_active_uses_short_ttl(self, fake_redis) -> None:
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        ok = await cache.write_through(
            task_public_id="task_a",
            status="running",
            active_run_id="run_a",
        )
        assert ok is True
        # TTL ~ 180s ± jitter. We forced jitter=1.0 → ~180s.
        ttl = await fake_redis.ttl(f"ta:{_env()}:cache:v1:task:status:task_a")
        assert 170 <= ttl <= 190, ttl

    async def test_write_through_terminal_uses_long_ttl(self, fake_redis) -> None:
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        ok = await cache.write_through(
            task_public_id="task_t",
            status="completed",
            active_run_id=None,
        )
        assert ok is True
        # 6h = 21600s ± jitter.
        ttl = await fake_redis.ttl(f"ta:{_env()}:cache:v1:task:status:task_t")
        assert 21500 <= ttl <= 21700, ttl

    async def test_write_through_failed_is_terminal(self, fake_redis) -> None:
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        ok = await cache.write_through(
            task_public_id="task_f",
            status="failed",
            active_run_id=None,
        )
        assert ok is True
        ttl = await fake_redis.ttl(f"ta:{_env()}:cache:v1:task:status:task_f")
        # terminal → 6h
        assert 21500 <= ttl <= 21700, ttl

    async def test_write_through_then_get_or_load_hit(self, fake_redis) -> None:
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        await cache.write_through("task_x", "running", "run_x")
        # Next read returns the write-through value (without invoking loader).
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _status_dto(status="stale")

        r = await cache.get_or_load_status("task_x", loader)
        assert r.status == "running"
        assert loader_calls["n"] == 0

    async def test_invalidate_removes_entry(self, fake_redis) -> None:
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        await cache.write_through("task_x", "running", "run_x")
        ok = await cache.invalidate("task_x")
        assert ok is True
        # Next read invokes loader.
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _status_dto(status="running")

        await cache.get_or_load_status("task_x", loader)
        assert loader_calls["n"] == 1

    async def test_loader_returning_none_writes_negative(self, fake_redis) -> None:
        cache = TaskStatusCache(manager=_make_manager(fake_redis))

        async def loader():
            return None

        r = await cache.get_or_load_status("task_missing", loader)
        assert r is None
        # Second call short-circuits (negative cache).
        loader_calls = {"n": 0}

        async def counting_loader():
            loader_calls["n"] += 1
            return None

        r2 = await cache.get_or_load_status("task_missing", counting_loader)
        assert r2 is None
        assert loader_calls["n"] == 0


def _env() -> str:
    """Match key_builder._env_segment."""
    from app.core.config import get_settings

    raw = get_settings().app_env or "dev"
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in raw)[:32] or "dev"


# ── TaskDetailCache (terminal only) ────────────────────────────────


class TestTaskDetailCache:
    async def test_get_or_load_hit(self, fake_redis) -> None:
        cache = TaskDetailCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _detail_dto("task_term")

        r1 = await cache.get_or_load("task_term", loader)
        r2 = await cache.get_or_load("task_term", loader)
        assert r1.task_public_id == "task_term"
        assert r2.task_public_id == "task_term"
        assert loader_calls["n"] == 1

    async def test_write_through(self, fake_redis) -> None:
        cache = TaskDetailCache(manager=_make_manager(fake_redis))
        ok = await cache.write_through("task_t", _detail_dto())
        assert ok is True
        # 30m TTL ± jitter.
        ttl = await fake_redis.ttl(f"ta:{_env()}:cache:v1:task:detail:task_t")
        assert 1700 <= ttl <= 1810, ttl  # 1800s ± jitter

    async def test_invalidate(self, fake_redis) -> None:
        cache = TaskDetailCache(manager=_make_manager(fake_redis))
        await cache.write_through("task_t", _detail_dto())
        ok = await cache.invalidate("task_t")
        assert ok is True


# ── Bypass paths ───────────────────────────────────────────────────


class TestBypass:
    async def test_domain_disabled_bypasses(self, fake_redis, monkeypatch) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_task_enabled", False)
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _status_dto()

        await cache.get_or_load_status("task_x", loader)
        await cache.get_or_load_status("task_x", loader)
        assert loader_calls["n"] == 2

    async def test_backend_disabled_bypasses(self) -> None:
        cache = TaskStatusCache(
            manager=CacheManager(backend=CacheBackend(redis_client=None)),
        )
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _status_dto()

        await cache.get_or_load_status("task_x", loader)
        await cache.get_or_load_status("task_x", loader)
        assert loader_calls["n"] == 2

    async def test_write_through_returns_false_when_bypassed(
        self, fake_redis, monkeypatch
    ) -> None:
        from app.core.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "cache_task_enabled", False)
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        ok = await cache.write_through("task_x", "running", "run_x")
        assert ok is False


# ── Singleton helpers ─────────────────────────────────────────────


class TestSingletons:
    def test_get_task_status_cache_lazy_default(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        CacheBackend._instance = backend
        try:
            a = get_task_status_cache()
            b = get_task_status_cache()
            assert a is b
        finally:
            CacheBackend._instance = None

    def test_set_resets_singleton(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        mgr = CacheManager(backend=backend)
        custom = TaskStatusCache(manager=mgr)
        set_task_status_cache(custom)
        try:
            assert get_task_status_cache() is custom
        finally:
            set_task_status_cache(None)

    def test_detail_singleton_round_trip(self, fake_redis) -> None:
        backend = CacheBackend(redis_client=fake_redis)
        backend._healthy = True
        mgr = CacheManager(backend=backend)
        custom = TaskDetailCache(manager=mgr)
        set_task_detail_cache(custom)
        try:
            assert get_task_detail_cache() is custom
        finally:
            set_task_detail_cache(None)


# ── Multi-worker consistency ──────────────────────────────────────


class TestMultiWorker:
    async def test_worker_a_writes_worker_b_reads(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = TaskStatusCache(manager=_make_manager(worker_a_redis))
        cache_b = TaskStatusCache(manager=_make_manager(worker_b_redis))

        # Worker A: write through "running".
        ok = await cache_a.write_through(
            task_public_id="task_shared",
            status="running",
            active_run_id="run_a",
        )
        assert ok is True

        # Worker B: read returns Worker A's write.
        async def stale_loader():
            return _status_dto(status="stale")

        r = await cache_b.get_or_load_status("task_shared", stale_loader)
        assert r.status == "running"
        assert r.active_run_id == "run_a"

    async def test_worker_a_terminal_writes_worker_b_terminal_reads(
        self, fake_redis_pair
    ) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = TaskStatusCache(manager=_make_manager(worker_a_redis))
        cache_b = TaskStatusCache(manager=_make_manager(worker_b_redis))

        ok = await cache_a.write_through(
            task_public_id="task_done",
            status="completed",
            active_run_id=None,
        )
        assert ok is True

        async def loader():
            return _status_dto(status="should-not-be-called")

        r = await cache_b.get_or_load_status("task_done", loader)
        assert r.status == "completed"
        assert r.terminal is True

    async def test_invalidations_cross_worker(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair
        cache_a = TaskStatusCache(manager=_make_manager(worker_a_redis))
        cache_b = TaskStatusCache(manager=_make_manager(worker_b_redis))

        await cache_a.write_through("task_x", "running", "run_x")

        async def stale_loader():
            return _status_dto(status="STALE")

        r = await cache_b.get_or_load_status("task_x", stale_loader)
        assert r.status == "running"

        # Worker A invalidates (e.g. admin force-killed).
        ok = await cache_a.invalidate("task_x")
        assert ok is True

        # Worker B must re-load.
        r2 = await cache_b.get_or_load_status("task_x", stale_loader)
        assert r2.status == "STALE"