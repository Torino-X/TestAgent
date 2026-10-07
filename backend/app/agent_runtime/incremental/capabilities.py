"""Incremental Agent ModelCapabilities — re-export shared stub (Phase 2.5).

镜像 preparation.capabilities / repair.capabilities。Incremental Agent 同样
走 Mode A (Structured Action JSON),不需要 native tool calling;未来 Mode B
接通时只需 ``resolve_capabilities`` 返回 ``native_tool_calling=True``。
"""

from __future__ import annotations

from app.agent_runtime.preparation.capabilities import (
    ModelCapabilities,
    resolve_capabilities,
)


__all__ = ["ModelCapabilities", "resolve_capabilities"]

# module-level note (auto-appended):
# ModelCapabilities + resolve_capabilities(incremental)。
# 关键约束: 沿用 prep 解析,本模块占位。
