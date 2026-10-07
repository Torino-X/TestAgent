"""Repair Agent ModelCapabilities — re-export shared stub (Phase 2.4).

镜像 preparation.capabilities:Repair Agent 同样走 Mode A (Structured Action
JSON),不需要 native tool calling;未来 Mode B 接通时只需 resolve_capabilities
返回 ``native_tool_calling=True``。

Phase 2.4 范围内只重新暴露 ``resolve_capabilities`` 与 ``ModelCapabilities``,
避免在 preparation/ 与 repair/ 之间循环引用。
"""

from __future__ import annotations

from app.agent_runtime.preparation.capabilities import (
    ModelCapabilities,
    resolve_capabilities,
)


__all__ = ["ModelCapabilities", "resolve_capabilities"]


# module-level note (auto-appended):
# ModelCapabilities + resolve_capabilities(repair)。
# 关键约束: 同 prep capabilities,但是按 repair context 解析。
