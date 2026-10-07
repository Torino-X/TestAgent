"""State-safe 与 Runtime-only 边界标记。

设计文档：``ContextStateRef 保持轻量``，State 不保存 Prompt、ContextItem、
Candidate、Vector、Raw Tool Output、Payload bytes。

- ``StateSafe``：可安全放入 LangGraph State / Snapshot / JSON 的轻量标记。
- ``RuntimeOnly``：只在进程内存在，禁止持久化（Session / Client / Vector 等）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StateSafe(Protocol):
    """可安全序列化进 LangGraph State / Snapshot 的对象。"""

    def to_state_dict(self) -> dict[str, Any]: ...


@runtime_checkable
class RuntimeOnly(Protocol):
    """运行时对象（Session / Client / 大对象），禁止进入 State / Snapshot。"""
# auto-appended module-level note: boundary 模型: ContextEngine 各子模块(检索/选择/压缩)的边界契约。
