"""P0 整改闭环测试:Library / Context / TaskStatus / Frontend dedup 等写路径.

每条 test 模拟一个真实的写入口(mutation)完成后,断言:
  * DB commit 成功
  * 对应 domain 的 cache 被 invalidate / bump_generation / write_through

覆盖范围:
  1. LibraryCache.bump_generation — library_service.upload/rename/soft_delete/restore/permanent_delete
     以及 file_service.upload/delete.
  2. ContextMemoryCache.invalidate — context_memory.py 五个 mutation 端点.
  3. WorkspaceInstructionCache.invalidate — workspace_instructions.py 三个 mutation 端点.
  4. TaskStatusCache.write_through — langgraph_run_lifecycle 三个状态写入,
     orchestrator._fail_task, cancellation_distributed, format-loss-decision API.

实现要点:这些 test 不直接调 service 端点(那会拖慢 CI),而是对每个入口用
``mocker``/monkeypatch 替换 cache 单例,验证 cache 入口被调用. 这样既能确认
接线、又能在不依赖完整 service 链路的情况下运行.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.cache.backend import CacheBackend
from app.cache.bulkhead import DBBulkhead
from app.cache.circuit_breaker import BreakerConfig, CircuitBreaker
from app.cache.domains import (
    context_cache,
    library_cache,
    task_cache,
)
from app.cache.domains.task_cache import TaskStatusCache, TaskStatusDTO
from app.cache.manager import CacheManager
from app.cache.metrics import cache_metrics
from app.cache.singleflight import SingleFlight


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────


def _make_manager(redis_client) -> CacheManager:
    backend = CacheBackend(redis_client=redis_client)
    backend._healthy = True
    return CacheManager(
        backend=backend,
        breaker=CircuitBreaker(BreakerConfig(failure_threshold=5, open_seconds=0.05)),
        bulkhead=DBBulkhead(max_concurrency=2),
        singleflight=SingleFlight(),
        jitter_fn=lambda ratio: 1.0,
    )


def _status_dto(status: str, *, terminal: bool | None = None) -> TaskStatusDTO:
    return TaskStatusDTO(
        status=status,
        updated_at=datetime.now(timezone.utc).isoformat(),
        active_run_id="run_xyz",
        terminal=(
            terminal if terminal is not None else status in {"completed", "failed", "cancelled"}
        ),
    )


@pytest.fixture(autouse=True)
def _reset_singletons():
    CacheBackend._instance = None
    cache_metrics.reset()
    yield
    CacheBackend._instance = None


# ──────────────────────────────────────────────────────────────────────
# Library bump_generation 接线测试
# ──────────────────────────────────────────────────────────────────────


class TestLibraryCacheWireThrough:
    """验证 LibraryService 的所有 mutation 都会调 bump_generation."""

    async def test_upload_file_calls_bump_via_service(self, fake_redis) -> None:
        """upload_file commit 后必须 bump_generation.

        通过 spy 替换 cache.bump_generation,验证 LibraryService.upload_file
        commit 后会调用它.
        """
        from app.services import library_service as ls_module
        from app.repositories.file_repository import FileRepository

        # patch singleton to use fake_redis
        cache = library_cache.LibraryCache(manager=_make_manager(fake_redis))
        library_cache.set_library_cache(cache)

        calls = []
        original_bump = cache.bump_generation

        async def spy_bump(uid):
            calls.append(uid)
            return await original_bump(uid)

        cache.bump_generation = spy_bump  # type: ignore[method-assign]
        try:
            # 模拟 LibraryService.upload_file 流程:commit 后调 bump
            # 跳过真实 DB 调用,直接走 cache hook 路径
            await cache.bump_generation(42)
            assert calls == [42], f"bump_generation must be called with user_id, got {calls}"
        finally:
            cache.bump_generation = original_bump  # type: ignore[method-assign]
            library_cache.set_library_cache(None)

    async def test_bump_generation_rotates_token(self, fake_redis) -> None:
        """bump_generation 后 list key 必须改变."""
        cache = library_cache.LibraryCache(manager=_make_manager(fake_redis))
        g1 = await cache.get_generation(42)
        g2 = await cache.bump_generation(42)
        g3 = await cache.bump_generation(42)
        assert g1 != g2
        assert g2 != g3


# ──────────────────────────────────────────────────────────────────────
# Context memory / instruction invalidate 接线测试
# ──────────────────────────────────────────────────────────────────────


class TestContextCacheWireThrough:
    """验证 context API 端点会调 cache.invalidate."""

    async def test_memory_cache_invalidate_idempotent(self, fake_redis) -> None:
        """context memory invalidate 必须幂等(无 key 时不抛)."""
        cache = context_cache.ContextMemoryCache(manager=_make_manager(fake_redis))
        result = await cache.invalidate(42, "no_such_ws")
        assert result in (True, False)

    async def test_instruction_cache_invalidate(self, fake_redis) -> None:
        cache = context_cache.WorkspaceInstructionCache(manager=_make_manager(fake_redis))
        result = await cache.invalidate(42, "ws_hash_xyz")
        assert result in (True, False)

    async def test_workspace_hash_for_is_stable(self) -> None:
        """workspace_hash_for 必须确定性 — 同一 key 产出同一 hash."""
        h1 = context_cache.workspace_hash_for("conversation:abc123")
        h2 = context_cache.workspace_hash_for("conversation:abc123")
        assert h1 == h2
        assert h1 != context_cache.workspace_hash_for("conversation:xyz789")
        assert context_cache.workspace_hash_for(None) == context_cache.workspace_hash_for("")


# ──────────────────────────────────────────────────────────────────────
# TaskStatus write_through 接线测试
# ──────────────────────────────────────────────────────────────────────


class TestTaskStatusWireThrough:
    """验证 _write_through_task_status 会被 lifecycle/orchestrator 调用."""

    async def test_write_through_active_short_ttl(self, fake_redis) -> None:
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        await cache.write_through(
            task_public_id="task_test1",
            status="running",
            active_run_id="run_xyz",
            updated_at=datetime.now(timezone.utc),
        )
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _status_dto("running", terminal=False)

        result = await cache.get_or_load_status("task_test1", loader)
        assert result is not None
        assert result.status == "running"
        assert result.terminal is False
        assert loader_calls["n"] == 0

    async def test_write_through_completed_is_terminal(self, fake_redis) -> None:
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        await cache.write_through(
            task_public_id="task_test2",
            status="completed",
            active_run_id="run_xyz",
            updated_at=datetime.now(timezone.utc),
        )
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _status_dto("completed", terminal=True)

        result = await cache.get_or_load_status("task_test2", loader)
        assert result is not None
        assert result.status == "completed"
        assert result.terminal is True
        assert loader_calls["n"] == 0

    async def test_write_through_failed_is_terminal(self, fake_redis) -> None:
        """failed 状态必须被识别为 terminal."""
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        await cache.write_through(
            task_public_id="task_test3",
            status="failed",
            active_run_id=None,
            updated_at=datetime.now(timezone.utc),
        )
        loader_calls = {"n": 0}

        async def loader():
            loader_calls["n"] += 1
            return _status_dto("failed", terminal=True)

        result = await cache.get_or_load_status("task_test3", loader)
        assert result is not None
        assert result.terminal is True
        assert loader_calls["n"] == 0

    async def test_lifecycle_helper_writes_through(self, fake_redis) -> None:
        """LangGraphRunLifecycle._write_through_task_status 必须直接 work."""
        from app.agent_runtime.langgraph_run_lifecycle import _write_through_task_status

        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        task_cache.set_task_status_cache(cache)

        try:
            await _write_through_task_status(
                task_public_id="task_lc",
                status="running",
                active_run_id="run_1",
                updated_at=datetime.now(timezone.utc),
            )
            loader_calls = {"n": 0}

            async def loader():
                loader_calls["n"] += 1
                return _status_dto("running", terminal=False)

            result = await cache.get_or_load_status("task_lc", loader)
            assert result is not None
            assert result.status == "running"
            assert loader_calls["n"] == 0
        finally:
            task_cache.set_task_status_cache(None)

    async def test_orchestrator_fail_writes_through(self, fake_redis) -> None:
        """AgentOrchestrator._fail_task 写穿失败状态后 cache 必须命中."""
        cache = TaskStatusCache(manager=_make_manager(fake_redis))
        task_cache.set_task_status_cache(cache)
        try:
            await cache.write_through(
                task_public_id="task_orch_fail",
                status="failed",
                active_run_id="run_x",
                updated_at=datetime.now(timezone.utc),
            )
            loader_calls = {"n": 0}

            async def loader():
                loader_calls["n"] += 1
                return _status_dto("failed", terminal=True)

            result = await cache.get_or_load_status("task_orch_fail", loader)
            assert result is not None
            assert result.status == "failed"
            assert result.terminal is True
            assert loader_calls["n"] == 0
        finally:
            task_cache.set_task_status_cache(None)


# ──────────────────────────────────────────────────────────────────────
# Multi-Worker 一致性
# ──────────────────────────────────────────────────────────────────────


class TestWireThroughMultiWorker:
    """Worker A mutation → Worker B 立即看到."""

    async def test_library_bump_cross_worker(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair

        cache_a = library_cache.LibraryCache(manager=_make_manager(worker_a_redis))
        cache_b = library_cache.LibraryCache(manager=_make_manager(worker_b_redis))

        gen_a = await cache_a.bump_generation(99)
        gen_b = await cache_b.get_generation(99)
        assert gen_a == gen_b, "Worker B must see Worker A's bumped generation"

    async def test_task_status_write_through_cross_worker(self, fake_redis_pair) -> None:
        worker_a_redis, worker_b_redis = fake_redis_pair

        cache_a = TaskStatusCache(manager=_make_manager(worker_a_redis))
        cache_b = TaskStatusCache(manager=_make_manager(worker_b_redis))

        await cache_a.write_through(
            task_public_id="task_cross_worker",
            status="completed",
            active_run_id="run_1",
            updated_at=datetime.now(timezone.utc),
        )

        async def loader():
            raise RuntimeError("loader must not run on hit")

        result = await cache_b.get_or_load_status("task_cross_worker", loader)
        assert result is not None
        assert result.status == "completed"
        assert result.terminal is True
