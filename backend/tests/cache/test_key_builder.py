"""Tests for ``app.cache.key_builder``.

Coverage:
  * build_key format (prefix:env:cache:schema:parts)
  * hash_filter determinism + length + collision resistance
  * empty / None parts are skipped
"""

from __future__ import annotations

import pytest

from app.cache.key_builder import build_key, hash_filter


class TestBuildKey:
    def test_format_matches_design(self) -> None:
        """Sample keys from design §7."""
        k = build_key("auth", "principal", "usr_xxx")
        # Expected: ta:{env}:cache:v1:auth:principal:usr_xxx
        assert k.endswith(":auth:principal:usr_xxx"), k
        assert ":cache:v1:" in k, k
        assert k.startswith("ta:"), k

    def test_capability_key(self) -> None:
        k = build_key("cfg", "model", "user", 10001, "cap", "chat")
        assert k.endswith(":cfg:model:user:10001:cap:chat"), k

    def test_int_parts_stringified(self) -> None:
        a = build_key("conv", "detail", "user", 1, "conv", 42)
        b = build_key("conv", "detail", "user", "1", "conv", "42")
        assert a == b, (a, b)

    def test_empty_parts_dropped(self) -> None:
        a = build_key("a", "", "b")
        b = build_key("a", "b")
        assert a == b, (a, b)

    def test_none_parts_dropped(self) -> None:
        a = build_key("a", None, "b")
        b = build_key("a", "b")
        assert a == b, (a, b)

    def test_whitespace_only_dropped(self) -> None:
        a = build_key("a", "   ", "b")
        b = build_key("a", "b")
        assert a == b, (a, b)


class TestHashFilter:
    def test_deterministic(self) -> None:
        a = hash_filter("user", "active", 1)
        b = hash_filter("user", "active", 1)
        assert a == b, (a, b)

    def test_length_is_16(self) -> None:
        # 设计文档 §7: filter hash 截断 16 hex chars
        h = hash_filter("foo")
        assert len(h) == 16, h
        assert all(c in "0123456789abcdef" for c in h), h

    def test_different_inputs_different_outputs(self) -> None:
        a = hash_filter("foo")
        b = hash_filter("bar")
        assert a != b, (a, b)

    def test_order_matters(self) -> None:
        a = hash_filter("foo", "bar")
        b = hash_filter("bar", "foo")
        assert a != b, (a, b)

    def test_empty_returns_zero(self) -> None:
        assert hash_filter() == "0", hash_filter()

    def test_same_inputs_collide(self) -> None:
        """Identical inputs must hash identically (the whole point)."""
        assert hash_filter("a", "b", "c") == hash_filter("a", "b", "c")


@pytest.mark.parametrize(
    "parts",
    [
        ("auth", "principal", "usr_abc"),
        ("cfg", "knowledge", "user", 1),
        ("task", "status", "task_xyz"),
        ("conv", "list", "user", 99, "g", "abc", "q", "deadbeef"),
    ],
)
def test_build_key_param(parts) -> None:
    k = build_key(*parts)
    expected_tail = ":".join(str(p) for p in parts)
    assert k.endswith(":" + expected_tail), k