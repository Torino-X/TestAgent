"""Base tool class that all Agent tools inherit."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from app.agent.context import AgentContext
from app.agent.retry_policy import RetryContext


class BaseTool(ABC):
    """Abstract base for all Agent tools.

    Every tool has a unique name, a description, and must implement `run`.

    The optional ``retry_context`` parameter lets a tool observe and
    react to retry attempts (e.g. append corrective feedback to the
    prompt on retry #2).  Tools that don't care about retries simply
    don't accept the parameter (the abstract signature uses kwargs).

    See :class:`app.agent.retry_policy.RetryContext` for the shape.
    """

    name: str
    description: str

    @abstractmethod
    async def run(
        self,
        inputs: dict,
        context: AgentContext,
        retry_context: Optional[RetryContext] = None,
    ) -> dict:
        """Execute the tool. Returns a standardised result dict."""
        ...

    def _success(
        self,
        data: dict,
        summary: str = "",
        warnings: list[str] | None = None,
    ) -> dict:
        return {
            "success": True,
            "tool_name": self.name,
            "task_id": None,  # filled by executor
            "data": data,
            "summary": summary or f"{self.name} 执行完成",
            "warnings": warnings or [],
            "error": None,
        }

    def _error(
        self,
        code: str,
        message: str,
        recoverable: bool = True,
        warnings: list[str] | None = None,
        details: Optional[dict] = None,
    ) -> dict:
        """Build a standard tool-error dict.

        ``recoverable`` defaults to True so the orchestrator's
        :class:`RetryPolicy` will retry it (subject to the hard-coded
        unrecoverable list).  Pass ``recoverable=False`` for the rare
        tool errors that genuinely cannot be retried.

        ``details`` is optional metadata for the orchestrator (e.g.
        which JSON field was missing).  Stored under
        ``error.details``; ``RetryPolicy`` ignores it today but
        future strategies (schema_feedback) can read it.
        """
        error_dict: dict[str, Any] = {
            "code": code,
            "message": message,
            "recoverable": recoverable,
        }
        if details:
            error_dict["details"] = details
        return {
            "success": False,
            "tool_name": self.name,
            "task_id": None,
            "data": None,
            "summary": f"{self.name} 执行失败",
            "warnings": warnings or [],
            "error": error_dict,
        }

    async def _progress(self, context: AgentContext, message: str, **metadata: Any) -> None:
        """Emit optional user-facing runtime progress without affecting tool results."""
        emit = getattr(context, "emit_tool_progress", None)
        if not callable(emit):
            return
        await emit(message, **metadata)
