"""Serializer — Business Cache Redis payload 统一 JSON envelope。

格式（设计文档 §6 + 提示词 §9）::

    {
      "v": 1,
      "negative": false,
      "created_at": 1788620000,
      "payload": {...}
    }

负缓存::

    {
      "v": 1,
      "negative": true,
      "created_at": 1788620000,
      "payload": null
    }

规则:
  - 仅 JSON UTF-8；禁止 pickle / cloudpickle / marshal
  - serializer 版本检查：发现旧版本/损坏 payload → 当 miss 处理并返回 None
  - 默认最大 256 KiB；超过最大值 → 上层跳过缓存
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CacheEnvelope:
    v: int
    negative: bool
    created_at: int
    payload: Any

    def to_json(self) -> str:
        return json.dumps(
            {
                "v": self.v,
                "negative": self.negative,
                "created_at": self.created_at,
                "payload": self.payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "CacheEnvelope | None":
        """Decode + validate envelope; return None when corrupt / old version."""
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("v") != SCHEMA_VERSION:
            return None
        if "negative" not in data or "created_at" not in data:
            return None
        return cls(
            v=int(data["v"]),
            negative=bool(data["negative"]),
            created_at=int(data["created_at"]),
            payload=data.get("payload"),
        )


def encode_positive(payload: Any) -> str:
    """Wrap a non-empty payload as a positive cache envelope (JSON str)."""
    env = CacheEnvelope(
        v=SCHEMA_VERSION,
        negative=False,
        created_at=int(time.time()),
        payload=payload,
    )
    return env.to_json()


def encode_negative() -> str:
    """Return a negative-cache envelope (no payload)."""
    env = CacheEnvelope(
        v=SCHEMA_VERSION,
        negative=True,
        created_at=int(time.time()),
        payload=None,
    )
    return env.to_json()


def decode(raw: str | bytes) -> CacheEnvelope | None:
    """Decode + validate; None on any failure (treat as miss)."""
    return CacheEnvelope.from_json(raw)


__all__ = [
    "CacheEnvelope",
    "SCHEMA_VERSION",
    "decode",
    "encode_negative",
    "encode_positive",
]