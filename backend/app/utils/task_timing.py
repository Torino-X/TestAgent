"""Shared task/run timing helpers for API serialization."""

from __future__ import annotations

from datetime import datetime, timezone


def normalize_utc(value: datetime | None) -> datetime | None:
    """Return a timezone-aware UTC datetime without changing the instant."""
    if value is None:
        return None
    if value.tzinfo is None:
        # Legacy database rows may contain naive UTC timestamps.
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_rfc3339(value: datetime | None) -> str | None:
    normalized = normalize_utc(value)
    return normalized.isoformat() if normalized else None


def duration_ms(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    start = normalize_utc(started_at)
    finish = normalize_utc(finished_at)
    if start is None or finish is None:
        return None
    return max(0, int((finish - start).total_seconds() * 1000))
# task_timing:任务级耗时度量(装饰器 + 上下文管理器);用于慢查询 / 慢任务监控,metrics 上报到 observability。
