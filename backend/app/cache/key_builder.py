"""KeyBuilder — Business Cache Redis Key 统一构造器。

格式（设计文档 §7 + 提示词 §10）::

    {prefix}:{env}:cache:{schema_version}:{domain}:{resource}:{identity...}

例::

    ta:dev:cache:v1:auth:principal:usr_xxx
    ta:dev:cache:v1:cfg:model:user:10001:cap:chat
    ta:dev:cache:v1:conv:list:user:10001:g:abc12345
    ta:dev:cache:v1:task:status:task_xxx
    ta:dev:cache:v1:lib:list:user:10001:g:abc12345:f:<filter_hash>

原则:
  - Key 不携带 email / username / display_name（仅 public_id / 整数 internal_id）
  - Key 不携带 API Key / password / token / JWT
  - 过滤条件使用稳定 hash（SHA-256 截断 16 hex），不放原文
  - Key 长度尽量 < 200 bytes

Step 2.3 仅放接口契约；具体 domain key 模板在 Step 3 CacheManager 之前完成。
"""

from __future__ import annotations

import hashlib
import os
from typing import Sequence

from app.core.config import get_settings


def _env_segment() -> str:
    """Return the environment segment from settings (default ``dev``).

    Keeps the prefix stable per environment so dev / staging / prod caches
    never collide.
    """
    settings = get_settings()
    env = settings.app_env or "dev"
    # Slug-safe — replace any non-alnum with underscore.
    return "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in env)[:32] or "dev"


def _prefix_segment() -> str:
    return get_settings().cache_key_prefix or "ta"


def _schema_segment() -> str:
    return get_settings().cache_schema_version or "v1"


def build_key(*parts: str | int) -> str:
    """Join ``prefix:env:cache:schema:parts...`` into a single Redis Key.

    Empty parts are dropped; ``int`` parts are stringified; whitespace is
    not stripped (callers must pass clean values).
    """
    segs: list[str] = []
    for p in parts:
        if p is None:
            continue
        s = str(p).strip()
        if s:
            segs.append(s)
    return ":".join([_prefix_segment(), _env_segment(), "cache", _schema_segment(), *segs])


def hash_filter(*values: object) -> str:
    """Stable, short hash of a filter tuple for use as a Key segment.

    SHA-256 over canonical JSON-ish ``repr(values)``; first 16 hex chars.
    Empty input → ``"0"`` (consistent with ``int(..., 16)`` shorthand).
    """
    if not values:
        return "0"
    encoded = repr(tuple(values)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


__all__ = ["build_key", "hash_filter"]