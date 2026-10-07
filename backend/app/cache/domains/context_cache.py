"""Context Memory + Workspace Instructions Cache (设计文档 §15 + 提示词 §26).

Context Memory + Workspace Instructions 是 LLM 高频注入 / 低频修改
的典型缓存场景 — TTL 5m 覆盖大多数 RAG 上下文调用.

Key 格式 (设计文档 §15):
  Memory Key:       ta:{env}:cache:v1:ctx:memory:user:{uid}:ws:{workspace_hash}
  Instructions Key:  ta:{env}:cache:v1:ctx:instruction:user:{uid}:ws:{workspace_hash}

TTL: 5m / negative 30s

写穿触发 (设计文档 §15 + 提示词 §26):
  - memory create_candidate
  - memory activate
  - memory reject
  - memory forget
  - memory delete
  - project rule create / activate / delete

workspace_hash: 由 caller 提供 (e.g. SHA256(user_internal_id + workspace_key) 截断).
不直接用 workspace_key 字符串是为了保持 Key 长度可控 + 避免 PII 进 Key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.cache.distributed_lock import CacheFillLock
from app.cache.key_builder import build_key, hash_filter
from app.cache.manager import CacheManager, get_cache_manager
from app.cache.metrics import cache_metrics
from app.cache.specs import CacheSpec

logger = logging.getLogger(__name__)


# ── Spec ────────────────────────────────────────────────────────────


# Per 设计文档 §15: TTL 5m, negative 30s
CONTEXT_SPEC = CacheSpec(
    domain="ctx",
    ttl_seconds=5 * 60,
    negative_ttl_seconds=30,
    jitter_ratio=0.10,
    enable_singleflight=True,
    enable_distributed_fill_lock=False,  # instructions load 不太并发
)


# ── DTO ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContextMemoryDTO:
    """Cached memory-list response shape."""

    memories: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"memories": list(self.memories)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContextMemoryDTO":
        return cls(memories=list(data.get("memories", [])))


@dataclass(frozen=True)
class WorkspaceInstructionDTO:
    """Cached workspace-instructions response shape."""

    instructions: dict[str, Any]  # arbitrary JSON-serialisable structure

    def to_dict(self) -> dict[str, Any]:
        return {"instructions": dict(self.instructions)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkspaceInstructionDTO":
        return cls(instructions=dict(data.get("instructions", {})))


# ── Key helpers ────────────────────────────────────────────────────


def context_memory_key(user_id: int | str, workspace_hash: str) -> str:
    return build_key("ctx", "memory", "user", user_id, "ws", workspace_hash)


def workspace_instruction_key(user_id: int | str, workspace_hash: str) -> str:
    return build_key("ctx", "instruction", "user", user_id, "ws", workspace_hash)


def workspace_hash_for(workspace_key: str | None) -> str:
    """Compute the workspace_hash used in cache Key.

    Caller passes the workspace_key (or None for the user-default scope);
    we SHA-256 + truncate to keep the cache Key under design §7's 200-byte
    limit while avoiding PII (the original workspace_key may be user-chosen).
    """
    return hash_filter(workspace_key or "")


# ── Cache services ─────────────────────────────────────────────────


class _BaseContextCache:
    """Common scaffolding (mirror _BaseConfigCache / _BaseTaskCache)."""

    def __init__(
        self,
        manager: CacheManager | None = None,
    ) -> None:
        self._mgr = manager if manager is not None else get_cache_manager()

    @property
    def spec_domain(self) -> str:
        return "ctx"

    async def _invalidate(self, spec: CacheSpec, key: str) -> bool:
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(spec.domain):
            return False
        return await self._mgr.delete(spec, key)

    async def _read_through(
        self,
        spec: CacheSpec,
        key: str,
        loader,
        is_dto_type,
    ) -> Any:
        """Generic cache-aside read.

        ``loader()`` must return a DTO instance of ``is_dto_type`` (or ``None``
        for negative cache).
        """
        if not self._mgr.is_enabled() or not self._mgr.domain_enabled(spec.domain):
            return await loader()

        async def _adapt():
            dto = await loader()
            if dto is None:
                return None
            return dto.to_dict()

        async def _adapt_cached(raw):
            from app.cache.manager import _CacheMiss as _CM

            if isinstance(raw, _CM):
                return None
            if isinstance(raw, dict):
                return is_dto_type.from_dict(raw)
            return raw

        return await self._mgr.get_or_load(
            spec,
            key,
            _adapt,
            fill_lock=None,
            cached_adapter=_adapt_cached,
        )


class ContextMemoryCache(_BaseContextCache):
    """Per-user per-workspace memory cache (设计文档 §15).

    Used by LLM prompt injection path.  Low write frequency (user must
    actively create/activate/forget memory candidates) but high read
    frequency (every chat turn).
    """

    def key(self, user_id: int | str, workspace_hash: str) -> str:
        return context_memory_key(user_id, workspace_hash)

    async def get_or_load(
        self,
        user_id: int | str,
        workspace_hash: str,
        loader,
    ) -> ContextMemoryDTO | None:
        return await self._read_through(
            CONTEXT_SPEC,
            self.key(user_id, workspace_hash),
            loader,
            ContextMemoryDTO,
        )

    async def invalidate(
        self,
        user_id: int | str,
        workspace_hash: str,
    ) -> bool:
        return await self._invalidate(
            CONTEXT_SPEC, self.key(user_id, workspace_hash),
        )


class WorkspaceInstructionCache(_BaseContextCache):
    """Per-user per-workspace instructions cache (设计文档 §15).

    Project rules / user preferences injected into LLM prompts.
    """

    def key(self, user_id: int | str, workspace_hash: str) -> str:
        return workspace_instruction_key(user_id, workspace_hash)

    async def get_or_load(
        self,
        user_id: int | str,
        workspace_hash: str,
        loader,
    ) -> WorkspaceInstructionDTO | None:
        return await self._read_through(
            CONTEXT_SPEC,
            self.key(user_id, workspace_hash),
            loader,
            WorkspaceInstructionDTO,
        )

    async def invalidate(
        self,
        user_id: int | str,
        workspace_hash: str,
    ) -> bool:
        return await self._invalidate(
            CONTEXT_SPEC, self.key(user_id, workspace_hash),
        )


# ── Module-level singletons ─────────────────────────────────────────


_memory_cache: ContextMemoryCache | None = None
_instruction_cache: WorkspaceInstructionCache | None = None


def get_context_memory_cache() -> ContextMemoryCache:
    global _memory_cache
    if _memory_cache is None:
        _memory_cache = ContextMemoryCache()
    return _memory_cache


def set_context_memory_cache(c: ContextMemoryCache | None) -> None:
    global _memory_cache
    _memory_cache = c


def get_workspace_instruction_cache() -> WorkspaceInstructionCache:
    global _instruction_cache
    if _instruction_cache is None:
        _instruction_cache = WorkspaceInstructionCache()
    return _instruction_cache


def set_workspace_instruction_cache(c: WorkspaceInstructionCache | None) -> None:
    global _instruction_cache
    _instruction_cache = c


__all__ = [
    "CONTEXT_SPEC",
    "ContextMemoryCache",
    "ContextMemoryDTO",
    "WorkspaceInstructionCache",
    "WorkspaceInstructionDTO",
    "context_memory_key",
    "get_context_memory_cache",
    "get_workspace_instruction_cache",
    "set_context_memory_cache",
    "set_workspace_instruction_cache",
    "workspace_hash_for",
    "workspace_instruction_key",
]