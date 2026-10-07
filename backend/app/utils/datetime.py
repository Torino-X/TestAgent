"""Datetime helpers for consistent timestamp handling."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current UTC datetime with timezone info."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Return current UTC datetime as ISO 8601 string."""
    return utcnow().isoformat()
# datetime:统一 UTC 时区处理 + ISO 序列化 + 字符串解析;全模块用 timezone-aware 对象,禁用 naive datetime。
