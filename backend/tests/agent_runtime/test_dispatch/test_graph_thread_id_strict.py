"""Phase 2.8R-C — Thread ID 严格唯一测试(5)。

设计目标(对应 docs/35 §3 验收三/四):
  * ``require_graph_thread_id`` — task_public_id 正常取值
  * 缺失 task_public_id → InvalidGraphThreadIdError,reason=task_id_missing
  * 空字符串 → reason=task_id_empty
  * None state → reason=state_is_none
  * placeholder ``stub-thread`` / ``incremental-default`` → 拒绝
  * 兼容 dict / dataclass
"""

from __future__ import annotations

import dataclasses

import pytest

from app.agent_runtime.graph_thread_id import (
    legacy_task_id_thread_id,
    require_graph_thread_id,
)
from app.core.exceptions import InvalidGraphThreadIdError


class TestRequireGraphThreadId:
    """5 tests — 覆盖所有错误路径。"""

    def test_valid_task_public_id_dict(self):
        """正常 dict state with task_public_id."""
        tid = require_graph_thread_id(
            {"task_public_id": "abc-123"}, context="test"
        )
        assert tid == "abc-123"

    def test_valid_task_public_id_dataclass(self):
        """dataclass state with task_public_id field."""

        @dataclasses.dataclass
        class S:
            task_public_id: str = ""

        s = S(task_public_id="p1")
        assert require_graph_thread_id(s, context="t") == "p1"

    def test_missing_raises_invalid(self):
        """缺失 task_public_id AND task_id → InvalidGraphThreadIdError。"""
        with pytest.raises(InvalidGraphThreadIdError) as exc_info:
            require_graph_thread_id({"other": "x"}, context="t")
        assert exc_info.value.detail["reason"] == "task_id_missing"
        assert exc_info.value.detail["context"] == "t"

    def test_empty_string_raises_invalid(self):
        """空字符串 → reason=task_public_id_empty(因为显式声明了)。"""
        with pytest.raises(InvalidGraphThreadIdError) as exc_info:
            require_graph_thread_id(
                {"task_public_id": "   "}, context="t"
            )
        assert exc_info.value.detail["reason"] == "task_public_id_empty"

    def test_none_state_raises(self):
        """None state → reason=state_is_none。"""
        with pytest.raises(InvalidGraphThreadIdError) as exc_info:
            require_graph_thread_id(None, context="t")
        assert exc_info.value.detail["reason"] == "state_is_none"

    def test_stub_thread_rejected(self):
        """placeholder 'stub-thread' → 不允许(防止历史 bug 复活)。"""
        with pytest.raises(InvalidGraphThreadIdError) as exc_info:
            require_graph_thread_id(
                {"task_public_id": "stub-thread"}, context="t"
            )
        assert exc_info.value.detail["reason"] == "placeholder_thread_id_rejected"

    def test_incremental_default_rejected(self):
        """placeholder 'incremental-default' → 不允许。"""
        with pytest.raises(InvalidGraphThreadIdError):
            require_graph_thread_id(
                {"task_public_id": "incremental-default"}, context="t"
            )

    def test_falls_back_to_task_id_int(self):
        """task_id(int 内部 id)fallback 接受 — 测试 fixture 兼容。"""
        tid = require_graph_thread_id({"task_id": 100}, context="t")
        assert tid == "100"

    def test_dual_key_dict(self):
        """dual-key dict:有 task_public_id 优先。"""
        tid = require_graph_thread_id(
            {"task_id": 100, "task_public_id": "p-real"}, context="t"
        )
        assert tid == "p-real"

    def test_dual_key_stub_thread_via_task_id_rejected(self):
        """task_id 兜底时若等于 stub-thread 仍要拒绝。"""
        with pytest.raises(InvalidGraphThreadIdError) as exc_info:
            require_graph_thread_id(
                {"task_id": "stub-thread"}, context="t"
            )
        assert exc_info.value.detail["reason"] == "placeholder_thread_id_rejected"
        assert exc_info.value.detail["source"] == "task_id_fallback"


class TestLegacyTaskIdThreadId:
    """1 测试 — helper 不抛错(只用于测试迁移期)。"""

    def test_legacy_returns_int_as_string(self):
        assert legacy_task_id_thread_id({"task_id": 99}) == "99"
        assert legacy_task_id_thread_id({"task_public_id": "pid"}) == "pid"
        assert legacy_task_id_thread_id({}) == ""
        assert legacy_task_id_thread_id(None) == ""


__all__ = [
    "TestRequireGraphThreadId",
    "TestLegacyTaskIdThreadId",
]
