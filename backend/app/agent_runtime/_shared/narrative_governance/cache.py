"""Phase 2.9C cache + budget for the optional LLM compression path.

The compression path is **default off** (Phase 2.9C §5.5). This
module provides:

* A simple in-memory LRU cache used when no Redis is available.
* A budget tracker that records token usage, latency, cache hit ratio
  per task.
* A stub LLM compressor that produces the same shape as the future
  real profile-based compressor (`narrative_summary`), but stays
  local-only so tests can run in any environment without LLM keys.

The cache key (Phase 2.9C §10.6) is intentionally NOT a function of
the full input — it depends on a normalized fact hash the caller has
already computed, so secrets in the payload cannot leak through the
key.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BudgetStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    budget_blocked: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "latency_ms": self.latency_ms,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "budget_blocked": self.budget_blocked,
        }


@dataclass
class Budget:
    """Per-task budget for the optional LLM compressor."""

    max_calls_per_task: int = 2
    max_prompt_tokens: int = 4000
    max_latency_ms: int = 12000
    stats: BudgetStats = field(default_factory=BudgetStats)

    def can_call(self, prompt_tokens_est: int = 0) -> bool:
        if self.stats.calls >= self.max_calls_per_task:
            self.stats.budget_blocked += 1
            return False
        if prompt_tokens_est and self.stats.prompt_tokens + prompt_tokens_est > self.max_prompt_tokens:
            self.stats.budget_blocked += 1
            return False
        return True

    def record_call(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        cache_hit: bool,
    ) -> None:
        self.stats.calls += 1
        self.stats.prompt_tokens += prompt_tokens
        self.stats.completion_tokens += completion_tokens
        self.stats.latency_ms += latency_ms
        if cache_hit:
            self.stats.cache_hits += 1
        else:
            self.stats.cache_misses += 1


class NarrativeCompressionCache:
    """Minimal in-process LRU cache.

    Phase 2.9C §10.6 calls for content-addressable caching but warns
    that the cache must not carry secrets. The cache ``set`` operation
    stores a pre-sanitized, structured copy; the key is hash-derived
    from parameters that don't carry secrets.
    """

    def __init__(self, *, max_entries: int = 64) -> None:
        self._items: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._max = max_entries

    def build_key(
        self,
        *,
        version: str,
        detail_level: str,
        normalized_fact_hash: str,
        output_schema_version: str = "v1",
    ) -> str:
        return f"{version}|{detail_level}|{normalized_fact_hash}|{output_schema_version}"

    def get(self, key: str) -> dict[str, Any] | None:
        return self._items.get(key)

    def set(self, key: str, value: dict[str, Any]) -> None:
        if key in self._items:
            self._items.move_to_end(key)
        self._items[key] = value
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self._items),
            "max": self._max,
        }


def hash_facts(facts: dict[str, Any] | None) -> str:
    """Stable, secret-free hash of normalized facts."""
    if not isinstance(facts, dict):
        return hashlib.sha256(b"{}").hexdigest()[:16]
    canonical_items = sorted(
        (str(k), str(v)) for k, v in facts.items() if k and v is not None
    )
    payload = "\n".join(f"{k}={v}" for k, v in canonical_items)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class StubCompressionResult:
    """Result returned by the stub compressor; the real LLM-backed
    compressor (Phase 2.9C+ profile `narrative_summary`) returns the
    same shape.
    """

    payload: dict[str, Any]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cache_hit: bool = False


def stub_compress(
    candidate: dict[str, Any] | None,
    *,
    level: str,
    budget: Budget,
    cache: NarrativeCompressionCache | None = None,
    fact_hash: str | None = None,
    schema_version: str = "v1",
) -> StubCompressionResult | None:
    """Deterministic stub — returns a trimmed payload when budget allows.

    Used in tests when LLM credentials are unavailable. The real LLM
    implementation plugs in here later and should preserve the same
    contract: ``None`` when budget/cache says "fall back to rule".
    """
    if not isinstance(candidate, dict):
        return None
    fact_hash = fact_hash or hash_facts(candidate)
    version = "stub-v1"
    cache_key = (
        cache.build_key(
            version=version,
            detail_level=level,
            normalized_fact_hash=fact_hash,
            output_schema_version=schema_version,
        )
        if cache is not None
        else None
    )

    started = time.monotonic()
    if cache and cache_key:
        cached = cache.get(cache_key)
        if cached is not None:
            cache_hit_result = StubCompressionResult(
                payload=cached,
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=int((time.monotonic() - started) * 1000),
                cache_hit=True,
            )
            budget.record_call(
                prompt_tokens=0,
                completion_tokens=0,
                latency_ms=cache_hit_result.latency_ms,
                cache_hit=True,
            )
            return cache_hit_result

    if not budget.can_call():
        return None

    payload = {
        "headline": str(candidate.get("headline") or "").strip()[:40],
        "summary": str(candidate.get("summary") or "").strip()[:160],
        "impact": str(candidate.get("impact") or "").strip()[:120],
        "next_action": str(candidate.get("next_action") or "").strip()[:120],
        "level": str(candidate.get("level") or "info"),
        "kind": str(candidate.get("kind") or ""),
        "details": [],
    }
    if cache and cache_key:
        cache.set(cache_key, payload)
    latency = int((time.monotonic() - started) * 1000)
    budget.record_call(
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=latency,
        cache_hit=False,
    )
    return StubCompressionResult(
        payload=payload,
        prompt_tokens=0,
        completion_tokens=0,
        latency_ms=latency,
        cache_hit=False,
    )


__all__ = [
    "Budget",
    "BudgetStats",
    "NarrativeCompressionCache",
    "StubCompressionResult",
    "hash_facts",
    "stub_compress",
]
# narrative_governance.cache:Phase 2.9C 压缩路径 LRU 缓存 + token/latency/hit ratio 预算记录器;默认 off。
