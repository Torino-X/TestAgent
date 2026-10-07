"""File Semantic Profile Cache (设计文档 §16 + 提示词 §27).

解决 ``_wait_for_profiles_ready`` 100ms 短轮询的 SELECT storm (设计文档 §16):

  - 每 100ms 轮询一次 → 改成 Redis MGET + 短 TTL negative cache
  - ready profile 缓存 10m (语义稳定)
  - pending profile 缓存 500ms (短 TTL 让 poll 收敛)
  - failed profile 缓存 30s (终态, 不再变)
  - 缺失 profile 缓存 250ms negative (防穿透)

Key:    ta:{env}:cache:v1:sem:profile:{file_public_id}
Value:  {"status": "pending"/"ready"/"failed", "metadata": {...}}

写穿 (设计文档 §16):
  Profile Worker 写入 DB 后 → SET ready (TTL 10m).
  Profile Worker 失败 → SET failed (TTL 30s).
  Profile Worker 创建 → 删缓存 (避免 pending→ready 期间返回 stale).

读 (设计文档 §16 + 提示词 §27):
  1. Redis MGET (多个 file_public_id 一次)
  2. miss 才批量查 DB
  3. 禁止每个 file 单独查一次
  4. 轮询必须有 jitter + 最大截止时间
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.cache.distributed_lock import CacheFillLock
from app.cache.key_builder import build_key
from app.cache.manager import CacheManager, get_cache_manager
from app.cache.metrics import cache_metrics
from app.cache.specs import CacheSpec

logger = logging.getLogger(__name__)


# ── Spec ────────────────────────────────────────────────────────────


# Per 设计文档 §16:
#   ready 10m / pending 500ms / negative 250ms / failed 30s
# Encoded as multiple specs; pick by status at write/read time.
@dataclass(frozen=True)
class SemanticProfileSpec:
    ready_ttl_seconds: int = 10 * 60
    pending_ttl_seconds: int = 500 // 1000  # 0 seconds — we'll use ms below
    failed_ttl_seconds: int = 30
    negative_ttl_seconds: int = 250 // 1000  # 0 — use ms below

    # Use ms precision since pending/negative are sub-second.
    pending_ttl_ms: int = 500
    negative_ttl_ms: int = 250
    jitter_ratio: float = 0.05
    domain: str = "sem"
    enable_singleflight: bool = True
    enable_distributed_fill_lock: bool = False  # poll path is high-frequency


SEMANTIC_PROFILE_SPEC = SemanticProfileSpec()


def _spec_for_status(status: str, *, negative: bool = False) -> CacheSpec:
    """Build the right CacheSpec for the status / hit-mode."""
    if negative:
        return CacheSpec(
            domain=SEMANTIC_PROFILE_SPEC.domain,
            ttl_seconds=max(1, SEMANTIC_PROFILE_SPEC.negative_ttl_ms // 1000),
            negative_ttl_seconds=max(1, SEMANTIC_PROFILE_SPEC.negative_ttl_ms // 1000),
            jitter_ratio=SEMANTIC_PROFILE_SPEC.jitter_ratio,
            enable_singleflight=True,
            enable_distributed_fill_lock=False,
            max_value_bytes=2048,
        )
    if status == "ready":
        ttl = SEMANTIC_PROFILE_SPEC.ready_ttl_seconds
    elif status == "failed":
        ttl = SEMANTIC_PROFILE_SPEC.failed_ttl_seconds
    else:
        # pending / processing — sub-second TTL via seconds rounding
        ttl = max(1, SEMANTIC_PROFILE_SPEC.pending_ttl_ms // 1000)
    return CacheSpec(
        domain=SEMANTIC_PROFILE_SPEC.domain,
        ttl_seconds=ttl,
        negative_ttl_seconds=max(1, SEMANTIC_PROFILE_SPEC.negative_ttl_ms // 1000),
        jitter_ratio=SEMANTIC_PROFILE_SPEC.jitter_ratio,
        enable_singleflight=True,
        enable_distributed_fill_lock=False,
        max_value_bytes=2048,
    )


# ── DTO ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SemanticProfileDTO:
    """Cached semantic-profile shadow.

    status ∈ {"pending", "processing", "ready", "failed", "unsupported"}.
    ``metadata`` only carries the fields the orchestrator hot path needs
    (summary / document_kind / confidence); the full row stays in MySQL.
    """

    file_public_id: str
    status: str
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_public_id": self.file_public_id,
            "status": self.status,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SemanticProfileDTO":
        return cls(
            file_public_id=str(data.get("file_public_id", "")),
            status=str(data.get("status", "pending")),
            metadata=dict(data.get("metadata", {})),
        )


# ── Key helpers ────────────────────────────────────────────────────


def semantic_profile_key(file_public_id: str) -> str:
    return build_key("sem", "profile", file_public_id)


# ── Cache service ─────────────────────────────────────────────────


class FileSemanticProfileCache:
    """Per-file semantic profile cache (设计文档 §16).

    Used by ``_wait_for_profiles_ready`` to short-circuit the 100ms poll
    loop.  Reads go through ``get_many`` (MGET) to fetch all pending
    files in one round-trip; misses batch-fetch from MySQL.

    SingleFlight coalesces concurrent ``get_many`` calls with the same
    set of file_public_ids — preventing the 100ms poll loop from
    multiplying DB load when N SSE clients all subscribe to the same
    task (设计文档 §16).
    """

    def __init__(
        self,
        manager: CacheManager | None = None,
    ) -> None:
        self._mgr = manager if manager is not None else get_cache_manager()
        # Process-local SingleFlight keyed by sorted (file_public_ids,).
        from app.cache.singleflight import SingleFlight

        self._flight = SingleFlight()

    @property
    def spec_domain(self) -> str:
        return "sem"

    def key(self, file_public_id: str) -> str:
        return semantic_profile_key(file_public_id)

    # ── Read path: batched MGET ──────────────────────────────────────

    async def get_many(
        self,
        file_public_ids: list[str],
        loader,
    ) -> dict[str, SemanticProfileDTO | None]:
        """Cache-aside batch read (MGET + fallback DB).

        ``loader(missing_ids)`` is called only for keys whose cache hit
        returned None / negative; it must return a ``dict[file_public_id,
        SemanticProfileDTO | None]`` covering those missing IDs (including
        entries explicitly ``None`` for "no row in DB").

        Concurrent callers with the same ``file_public_ids`` set coalesce
        through process-local SingleFlight to avoid the 100ms poll loop
        multiplying DB load (设计文档 §16).
        """
        if not file_public_ids:
            return {}
        # SingleFlight coalesces concurrent reads of the same set.
        flight_key = "sem:" + ",".join(sorted(file_public_ids))
        return await self._flight.do(
            flight_key,
            lambda: self._get_many_uncached(file_public_ids, loader),
        )

    async def _get_many_uncached(
        self,
        file_public_ids: list[str],
        loader,
    ) -> dict[str, SemanticProfileDTO | None]:

        spec = _spec_for_status("ready")  # probe spec; ttl only matters on write
        keys = [self.key(pid) for pid in file_public_ids]
        # get_many returns {key: payload_or_CacheMiss}; map back to file ids.
        raws_by_key = await self._mgr.get_many(spec, keys)
        from app.cache.manager import _CacheMiss as _CM

        result: dict[str, SemanticProfileDTO | None] = {}
        missing: list[str] = []
        negative_hit: list[str] = []
        for pid, key in zip(file_public_ids, keys):
            raw = raws_by_key.get(key)
            if isinstance(raw, _CM):
                if raw.reason == "negative":
                    # Negative cache hit → short-circuit (file not in DB)
                    negative_hit.append(pid)
                else:
                    # miss / corrupt → fetch from loader
                    missing.append(pid)
            elif raw is None:
                missing.append(pid)
            elif isinstance(raw, dict):
                result[pid] = SemanticProfileDTO.from_dict(raw)
            else:
                # Defensive — corrupt type, treat as miss.
                missing.append(pid)

        # Short-circuit negative hits (file doesn't exist in DB).
        for pid in negative_hit:
            result[pid] = None

        if missing:
            loaded = await loader(missing)
            for pid in missing:
                loaded_dto = loaded.get(pid) if loaded else None
                result[pid] = loaded_dto
                if loaded_dto is None:
                    # Negative cache (TTL 250ms) so concurrent misses
                    # converge — anti-stampede.
                    await self._mgr.set_negative(
                        _spec_for_status("ready", negative=True),
                        self.key(pid),
                    )
                else:
                    # Write-through with status-specific TTL.
                    await self._mgr.set(
                        _spec_for_status(loaded_dto.status),
                        self.key(pid),
                        loaded_dto.to_dict(),
                    )
        return result

    # ── Write-through (called by Profile Worker) ──────────────────────

    async def write_through(self, dto: SemanticProfileDTO) -> bool:
        """Cache SET with status-specific TTL (设计文档 §16).

        Called by ``FileSemanticProfileWorker`` after DB commit:
          - status='ready'    → 10m TTL
          - status='failed'   → 30s TTL
          - status='pending'  → 500ms TTL
          - status='processing' → 500ms TTL
        """
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            cache_metrics.record(
                domain=self.spec_domain, operation="set", result="bypass",
            )
            return False
        spec = _spec_for_status(dto.status)
        return await self._mgr.set(spec, self.key(dto.file_public_id), dto.to_dict())

    async def invalidate(self, file_public_id: str) -> bool:
        """Drop cached entry — e.g. when starting fresh processing."""
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(self.spec_domain):
            return False
        return await self._mgr.delete(
            _spec_for_status("ready"),
            self.key(file_public_id),
        )


# ── Module-level singleton ─────────────────────────────────────────


_cache: FileSemanticProfileCache | None = None


def get_semantic_profile_cache() -> FileSemanticProfileCache:
    global _cache
    if _cache is None:
        _cache = FileSemanticProfileCache()
    return _cache


def set_semantic_profile_cache(c: FileSemanticProfileCache | None) -> None:
    global _cache
    _cache = c


__all__ = [
    "SEMANTIC_PROFILE_SPEC",
    "SemanticProfileSpec",
    "FileSemanticProfileCache",
    "SemanticProfileDTO",
    "get_semantic_profile_cache",
    "semantic_profile_key",
    "set_semantic_profile_cache",
]