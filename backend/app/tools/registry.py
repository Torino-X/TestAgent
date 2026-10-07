"""Tool registry — stores all registered Agent tools."""

from __future__ import annotations

from app.tools.base import BaseTool


class ToolRegistry:
    """In-memory registry of available Agent tools.

    All tools must be registered here before the Agent can use them.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def all(self) -> dict[str, BaseTool]:
        return dict(self._tools)


# Module-level singleton
tool_registry = ToolRegistry()
# tools.registry:全局 ToolRegistry(单例);内存 dict[tool_name -> BaseTool];启动期 register_all_tools 一次性填充。
