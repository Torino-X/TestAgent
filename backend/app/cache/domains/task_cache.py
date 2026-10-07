"""Task Status Cache (设计文档 §13 + 提示词 §22-24).

解决 ``SSE heartbeat`` 每 30s 一次的 ``SELECT status FROM agent_tasks`` 高频读.
运行中 task 字段变化频繁,因此:

  - 不缓存完整 task detail (Step 6 仅 status shadow);
  - 后续 task detail cache 仅对 terminal (completed / failed / cancelled)
    任务启用 (设计文档 §13.4).

Key:    ta:{env}:cache:v1:task:status:{task_public_id}
TTL:    active 180s / terminal 6h
Value:  {status, updated_at, active_run_id, terminal}

写穿策略（设计文档 §13.2）:

    DB commit
       ↓
    Redis SET status shadow

不能:
    先 SET Redis → 再 commit DB
    (rollback 会留下错误状态)

SSE Heartbeat（设计文档 §13.3 + 提示词 §23）:

    Redis GET task:status
       ├─ hit → heartbeat
       └─ miss
            ├─ breaker closed → SELECT DB → SET Redis → heartbeat
            └─ breaker open / Redis down → fallback DB (走 DB fallback limiter)

不得改变:
    - SSE event 格式
    - Last-Event-ID
    - LiveEventBus 行为
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from app.cache.distributed_lock import CacheFillLock
from app.cache.key_builder import build_key
from app.cache.manager import CacheManager, get_cache_manager
from app.cache.metrics import cache_metrics
from app.cache.specs import CacheSpec

logger = logging.getLogger(__name__)


# ── Spec ────────────────────────────────────────────────────────────


# Terminal statuses per 设计文档 §13.4.
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass(frozen=True)
class TaskStatusSpec:
    """Per 设计文档 §13.1 — TTL different for active vs terminal.

    Encoded as a single spec with a TTL-chosen-at-write helper.  Active
    tasks refresh the short 180s TTL each time; terminal tasks settle
    into the 6h TTL because their status never changes again.
    """

    active_ttl_seconds: int = 180
    terminal_ttl_seconds: int = 6 * 3600
    jitter_ratio: float = 0.10
    enable_singleflight: bool = True
    enable_distributed_fill_lock: bool = False  # TTL 短,本地 dedupe 已够
    domain: str = "task"

    @property
    def negative_ttl_seconds(self) -> int:
        # Negative TTL short — heartbeat must re-check frequently to avoid
        # serving stale "missing" 缓存.
        return 30

    @property
    def max_value_bytes(self) -> int:
        return 1024  # DTO is small


TASK_STATUS_SPEC = TaskStatusSpec()


def _cache_spec_for_dto(dto: "TaskStatusDTO") -> CacheSpec:
    """Build the right CacheSpec for the DTO's terminal flag."""
    return CacheSpec(
        domain=TASK_STATUS_SPEC.domain,
        ttl_seconds=(
            TASK_STATUS_SPEC.terminal_ttl_seconds
            if dto.terminal
            else TASK_STATUS_SPEC.active_ttl_seconds
        ),
        negative_ttl_seconds=TASK_STATUS_SPEC.negative_ttl_seconds,
        jitter_ratio=TASK_STATUS_SPEC.jitter_ratio,
        enable_singleflight=TASK_STATUS_SPEC.enable_singleflight,
        enable_distributed_fill_lock=TASK_STATUS_SPEC.enable_distributed_fill_lock,
        max_value_bytes=TASK_STATUS_SPEC.max_value_bytes,
    )


# ── DTO ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TaskStatusDTO:
    """Status shadow of an AgentTask row.

    Only the fields relevant to heartbeat / status display.  Full task
    detail is intentionally NOT cached (设计文档 §13.1).
    """

    status: str
    updated_at: str  # ISO-8601 string for JSON-friendliness
    active_run_id: str | None
    terminal: bool  # computed: status in TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskStatusDTO":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class TaskDetailDTO:
    """Full task detail DTO (terminal only, 设计文档 §13.4).

    Captured when a task enters a terminal status.  Subsequent
    mutations to related state (artifact rename / late event) must
    invalidate the corresponding Key.
    """

    task_public_id: str
    status: str
    user_id: int
    conversation_id: int
    task_type: str
    title: str | None
    error_code: str | None
    error_message: str | None
    started_at: str | None
    completed_at: str | None
    updated_at: str
    active_run_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskDetailDTO":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ── Key helpers ────────────────────────────────────────────────────


def task_status_key(task_public_id: str) -> str:
    return build_key("task", "status", task_public_id)


def task_detail_key(task_public_id: str) -> str:
    return build_key("task", "detail", task_public_id)


# ── Cache service ─────────────────────────────────────────────────


class _BaseTaskCache:
    """Common scaffolding (mirrors config_cache._BaseConfigCache)."""

    def __init__(
        self,
        manager: CacheManager | None = None,
        *,
        fill_lock: CacheFillLock | None = None,
    ) -> None:
        self._mgr = manager if manager is not None else get_cache_manager()
        self._fill_lock = fill_lock

    @property
    def spec_domain(self) -> str:
        return "task"

    def _fill_lock_runtime(self) -> CacheFillLock | None:
        if self._fill_lock is not None:
            return self._fill_lock
        if not self._mgr.is_enabled():
            return None
        try:
            client = self._mgr._backend.client  # type: ignore[attr-defined]
        except RuntimeError:
            return None
        from app.core.config import get_settings

        ttl_ms = int(get_settings().cache_lock_ttl_ms)
        self._fill_lock = CacheFillLock(client, ttl_ms=ttl_ms)
        return self._fill_lock


class TaskStatusCache(_BaseTaskCache):
    """Per-task status shadow cache (设计文档 §13).

    Used by SSE heartbeat as the read path, and by every ``agent_tasks
    .status`` write-through point as the post-commit set.

    ``get_or_load_status(task_public_id, loader)`` is the canonical
    read path used by SSE — it short-circuits to the cache when the
    status shadow is fresh, falling through to ``loader()`` (a short
    DB SELECT) when the shadow is missing or expired.
    """

    @property
    def spec(self) -> TaskStatusSpec:
        return TASK_STATUS_SPEC

    def key(self, task_public_id: str) -> str:
        return task_status_key(task_public_id)

    async def get_or_load_status(
        self,
        task_public_id: str,
        loader,
    ) -> TaskStatusDTO | None:
        """Cache-aside read with TTL chosen by status.

        ``loader()`` is called on miss; it must return a
        :class:`TaskStatusDTO` (or ``None`` for not-found).
        """
        from app.cache.manager import _CacheMiss as _CM

        cached_raw = await self._mgr.get(
            self._spec_obj(terminal=False),
            self.key(task_public_id),
        )
        if isinstance(cached_raw, dict):
            return TaskStatusDTO.from_dict(cached_raw)
        if isinstance(cached_raw, _CM):
            if cached_raw.reason == "negative":
                # Negative-cache hit → short-circuit (do NOT call loader).
                return None
            # True miss (key absent) → fall through to loader.
        elif cached_raw is not None:
            # Some other cached value (defensive).
            return cached_raw
        # miss → load + write with the right TTL.
        dto = await loader()
        if dto is None:
            await self._mgr.set_negative(
                self._spec_obj(terminal=False),
                self.key(task_public_id),
            )
            return None
        # Pick the correct spec for THIS status (terminal → 6h).
        await self._mgr.set(
            self._spec_obj(terminal=dto.terminal),
            self.key(task_public_id),
            dto.to_dict(),
        )
        return dto

    async def write_through(
        self,
        task_public_id: str,
        status: str,
        active_run_id: str | None,
        updated_at: datetime | str | None = None,
    ) -> bool:
        """Build a DTO from the new status and write it.

        Call AFTER DB commit (设计文档 §13.2).  If the status is one
        of ``completed`` / ``failed`` / ``cancelled``, the terminal TTL
        (6h) is applied; otherwise active (180s).
        """
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            cache_metrics.record(
                domain=self.spec_domain, operation="set", result="bypass",
            )
            return False
        is_terminal = status in TERMINAL_STATUSES
        if updated_at is None:
            updated_at = datetime.now(timezone.utc)
        if isinstance(updated_at, datetime):
            updated_at_str = updated_at.isoformat()
        else:
            updated_at_str = str(updated_at)
        dto = TaskStatusDTO(
            status=status,
            updated_at=updated_at_str,
            active_run_id=active_run_id,
            terminal=is_terminal,
        )
        return await self._mgr.set(
            self._spec_obj(terminal=is_terminal),
            self.key(task_public_id),
            dto.to_dict(),
        )

    async def invalidate(self, task_public_id: str) -> bool:
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            return False
        return await self._mgr.delete(
            self._spec_obj(terminal=False),
            self.key(task_public_id),
        )

    @staticmethod
    def _spec_obj(terminal: bool) -> CacheSpec:
        return _cache_spec_for_dto(
            TaskStatusDTO(
                status="completed" if terminal else "running",
                updated_at="",
                active_run_id=None,
                terminal=terminal,
            )
        )


class TaskDetailCache(_BaseTaskCache):
    """Terminal-only full task detail cache (设计文档 §13.4)."""

    DETAIL_TTL_SECONDS = 30 * 60  # 30m
    DETAIL_NEGATIVE_TTL_SECONDS = 60

    def key(self, task_public_id: str) -> str:
        return task_detail_key(task_public_id)

    @property
    def spec(self) -> CacheSpec:
        return CacheSpec(
            domain=self.spec_domain,
            ttl_seconds=self.DETAIL_TTL_SECONDS,
            negative_ttl_seconds=self.DETAIL_NEGATIVE_TTL_SECONDS,
            jitter_ratio=0.10,
            enable_singleflight=True,
            enable_distributed_fill_lock=False,
        )

    async def get_or_load(
        self,
        task_public_id: str,
        loader,
    ) -> TaskDetailDTO | None:
        cached_raw = await self._mgr.get(self.spec, self.key(task_public_id))
        from app.cache.manager import _CacheMiss as _CM

        if isinstance(cached_raw, dict):
            return TaskDetailDTO.from_dict(cached_raw)
        if cached_raw is not None and not isinstance(cached_raw, _CM):
            return cached_raw
        dto = await loader()
        if dto is None:
            await self._mgr.set_negative(self.spec, self.key(task_public_id))
            return None
        await self._mgr.set(self.spec, self.key(task_public_id), dto.to_dict())
        return dto

    async def write_through(
        self,
        task_public_id: str,
        dto: TaskDetailDTO | None,
    ) -> bool:
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            return False
        if dto is None:
            return await self._mgr.delete(self.spec, self.key(task_public_id))
        return await self._mgr.set(self.spec, self.key(task_public_id), dto.to_dict())

    async def invalidate(self, task_public_id: str) -> bool:
        return await self._mgr.delete(self.spec, self.key(task_public_id))


# ── Module-level singletons ─────────────────────────────────────────


_status_cache: TaskStatusCache | None = None
_detail_cache: TaskDetailCache | None = None


def get_task_status_cache() -> TaskStatusCache:
    global _status_cache
    if _status_cache is None:
        _status_cache = TaskStatusCache()
    return _status_cache


def set_task_status_cache(c: TaskStatusCache | None) -> None:
    global _status_cache
    _status_cache = c


def get_task_detail_cache() -> TaskDetailCache:
    global _detail_cache
    if _detail_cache is None:
        _detail_cache = TaskDetailCache()
    return _detail_cache


def set_task_detail_cache(c: TaskDetailCache | None) -> None:
    global _detail_cache
    _detail_cache = c


__all__ = [
    "TASK_STATUS_SPEC",
    "TERMINAL_STATUSES",
    "TaskDetailCache",
    "TaskDetailDTO",
    "TaskStatusCache",
    "TaskStatusDTO",
    "TaskStatusSpec",
    "get_task_detail_cache",
    "get_task_status_cache",
    "set_task_detail_cache",
    "set_task_status_cache",
    "task_detail_key",
    "task_status_key",
]