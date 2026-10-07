"""Phase 2.8R-D:RuntimeContext._intermediate_state 行为保护。

不重复 `_tool_state` 历史 bug 的故事 — frozen+slots 不允许 setattr 新字段,
必须显式声明 `_intermediate_state: dict` 才能复用 dict 内容做工具间传递。

不可妥协的不变式:
  * 不同 RuntimeContext 之间状态隔离(同一 Run 内可连续读)
  * frozen=True / slots=True 仍然生效
  * _intermediate_state 不进入 LangGraph GraphState(否则会被 checkpoint 序列化)
  * 不允许动态新增字段(防止代码再误用 `object.__setattr__`)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

import pytest

from app.agent_runtime.runtime_context import RuntimeContext


def _make_ctx(**overrides) -> RuntimeContext:
    base = dict(
        user_internal_id=1,
        task_internal_id=1,
        conversation_internal_id=1,
        session_factory=lambda: None,
        settings_service=None,
        event_sink=None,
        cancellation_service=None,
        clock=datetime.utcnow,
    )
    base.update(overrides)
    return RuntimeContext(**base)


def test_intermediate_state_default_is_empty_dict() -> None:
    ctx = _make_ctx()
    assert isinstance(ctx._intermediate_state, dict)
    assert ctx._intermediate_state == {}


def test_intermediate_state_mutable_dict_content() -> None:
    """frozen=True 不阻止 dict mutation;只能阻止 setattr 增加 attribute。"""
    ctx = _make_ctx()
    ctx._intermediate_state["template_structure"] = {"sections": ["x"]}
    assert ctx._intermediate_state["template_structure"]["sections"] == ["x"]


def test_intermediate_state_isolated_between_contexts() -> None:
    """两个 RuntimeContext 实例的 _intermediate_state 必须互不影响。"""
    ctx_a = _make_ctx(task_internal_id=11)
    ctx_b = _make_ctx(task_internal_id=22)
    ctx_a._intermediate_state["k"] = "from-a"
    ctx_b._intermediate_state["k"] = "from-b"
    assert ctx_a._intermediate_state["k"] == "from-a"
    assert ctx_b._intermediate_state["k"] == "from-b"
    assert ctx_a._intermediate_state is not ctx_b._intermediate_state


def test_runtime_context_remains_frozen() -> None:
    """frozen=True 必须仍然生效 — 不能 setattr 现有字段。"""
    ctx = _make_ctx()
    with pytest.raises(Exception):  # FrozenInstanceError
        ctx.user_internal_id = 999  # type: ignore[misc]


def test_runtime_context_slots_prevent_dynamic_attrs() -> None:
    """slots=True 不允许动态新增字段(防止 ``_tool_state`` 故事再发生)。"""
    ctx = _make_ctx()
    with pytest.raises(Exception):
        ctx.foobar_new_attr = 1  # type: ignore[attr-defined]


def test_intermediate_state_not_in_slots_when_frozen_copy_made() -> None:
    """dataclasses.replace 必须保留 _intermediate_state(不像 slots 字段)。"""
    ctx = _make_ctx()
    ctx._intermediate_state["requirement_analysis"] = {"a": 1}
    # replace 会复制 dataclass fields,_intermediate_state 是 field(default_factory=dict)
    new_ctx = dataclasses_replace(ctx, user_internal_id=99)
    # 同一个 dict 引用(因为 field 是 mutable,replace 不深拷贝)
    assert new_ctx._intermediate_state["requirement_analysis"] == {"a": 1}


# Local helper — dataclasses.replace 不在 dataclass 自身 import 默认开
def dataclasses_replace(instance: Any, **changes: Any) -> Any:
    import dataclasses

    return dataclasses.replace(instance, **changes)
