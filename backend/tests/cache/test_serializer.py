"""Tests for ``app.cache.serializer``.

Coverage (per prompt §33 / 设计文档 §6):
  * positive envelope round-trip
  * negative envelope round-trip
  * corrupt JSON returns None (treated as miss)
  * missing required fields returns None
  * old schema version returns None
  * unicode payload safe
"""

from __future__ import annotations

import json

import pytest

from app.cache.serializer import (
    SCHEMA_VERSION,
    decode,
    encode_negative,
    encode_positive,
)


class TestPositiveEnvelope:
    def test_round_trip_simple(self) -> None:
        raw = encode_positive({"role": "user", "name": "alice"})
        env = decode(raw)
        assert env is not None
        assert env.v == SCHEMA_VERSION
        assert env.negative is False
        assert env.payload == {"role": "user", "name": "alice"}

    def test_round_trip_nested(self) -> None:
        payload = {
            "items": [1, 2, 3],
            "nested": {"k": "v", "n": None},
            "flag": True,
        }
        raw = encode_positive(payload)
        env = decode(raw)
        assert env is not None
        assert env.payload == payload

    def test_unicode_payload_safe(self) -> None:
        payload = {"name": "测试用户 🎉", "role": "админ"}
        raw = encode_positive(payload)
        # Body must be valid UTF-8 (redis stores bytes).
        assert isinstance(raw, str)
        env = decode(raw)
        assert env is not None
        assert env.payload == payload

    def test_bytes_payload_accepted(self) -> None:
        """decode() must accept bytes (fakeredis returns bytes by default)."""
        raw_bytes = encode_positive({"k": 1}).encode("utf-8")
        env = decode(raw_bytes)
        assert env is not None
        assert env.payload == {"k": 1}


class TestNegativeEnvelope:
    def test_round_trip(self) -> None:
        raw = encode_negative()
        env = decode(raw)
        assert env is not None
        assert env.negative is True
        assert env.payload is None

    def test_created_at_is_recent(self) -> None:
        import time

        before = int(time.time()) - 1
        env = decode(encode_negative())
        after = int(time.time()) + 1
        assert before <= env.created_at <= after, env.created_at


class TestCorruptPayloads:
    def test_invalid_json_returns_none(self) -> None:
        assert decode(b"{not-json") is None
        assert decode("not-json-at-all") is None

    def test_missing_v_returns_none(self) -> None:
        bad = json.dumps({"negative": False, "created_at": 0, "payload": {}})
        assert decode(bad) is None

    def test_missing_negative_returns_none(self) -> None:
        bad = json.dumps({"v": 1, "created_at": 0, "payload": {}})
        assert decode(bad) is None

    def test_missing_created_at_returns_none(self) -> None:
        bad = json.dumps({"v": 1, "negative": False, "payload": {}})
        assert decode(bad) is None

    def test_old_version_returns_none(self) -> None:
        bad = json.dumps({"v": 99, "negative": False, "created_at": 0, "payload": {}})
        assert decode(bad) is None

    def test_empty_object_returns_none(self) -> None:
        assert decode(b"{}") is None

    def test_array_returns_none(self) -> None:
        assert decode(b"[]") is None

    def test_null_bytes_returns_none(self) -> None:
        assert decode(b"\x00\x00\x00") is None


class TestSchemaVersion:
    def test_schema_version_constant(self) -> None:
        # Bump this only with a planned migration path; bumping the
        # constant is how we force-rotate stale payloads.
        assert SCHEMA_VERSION == 1, SCHEMA_VERSION