"""Tool executor — calls a tool by name with given inputs."""

from __future__ import annotations

import logging
from typing import Optional

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext
from app.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Invokes registered tools safely."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def run(
        self,
        tool_name: str,
        inputs: dict,
        context: AgentContext,
        retry_context: Optional[RetryContext] = None,
    ) -> dict:
        """Run a tool by name.

        The optional ``retry_context`` is forwarded to the tool's
        ``run()`` so it can observe retry attempts.  A tool that does
        not accept this parameter simply gets the default ``None``
        and ignores it (Python's *args / **kwargs convention).
        """
        tool = self.registry.get(tool_name)
        if tool is None:
            return {
                "success": False,
                "tool_name": tool_name,
                "task_id": context.task_id,
                "data": None,
                "summary": f"未注册的工具: {tool_name}",
                "warnings": [],
                "error": {
                    "code": "TOOL_NOT_FOUND",
                    "message": f"Tool '{tool_name}' is not registered.",
                    "recoverable": False,
                },
            }

        try:
            # Tools that don't yet accept retry_context will raise
            # TypeError if we passed it positionally.  We try the
            # 3-arg call first (new signature); if the tool signature
            # is the older 2-arg form, fall back.
            try:
                result = await tool.run(inputs, context, retry_context)
            except TypeError as exc:
                if "retry_context" not in str(exc):
                    raise
                # Legacy tool signature: drop retry_context.
                result = await tool.run(inputs, context)
            result["task_id"] = context.task_id
            return result
        except Exception as exc:
            logger.exception("Tool %s failed", tool_name)
            return {
                "success": False,
                "tool_name": tool_name,
                "task_id": context.task_id,
                "data": None,
                "summary": f"{tool_name} 执行异常",
                "warnings": [],
                "error": {
                    "code": "TOOL_EXECUTION_ERROR",
                    "message": str(exc),
                    "recoverable": False,
                },
            }