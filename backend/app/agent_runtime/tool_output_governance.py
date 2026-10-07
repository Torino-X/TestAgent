"""Production bridge from Agent tool envelopes to Context tool-output governance."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable

from app.context_engine.models.tool_output import ToolOutputPolicy
from app.context_engine.tool_output import ToolOutputManager, TypedToolOutput
from app.models.tool_call import ToolCall
from app.repositories.tool_call_repository import ToolCallRepository


def serialize_tool_result(envelope: dict[str, Any]) -> str:
    """Serialize a copy deterministically without mutating business Graph State."""
    return json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


class ProductionToolOutputRecorder:
    """Persist a safe tool-call row, then attach the governed output metadata."""

    def __init__(
        self,
        *,
        session_factory,
        manager: ToolOutputManager,
        task_flag_resolver,
        user_internal_id: int,
        conversation_internal_id: int,
        task_internal_id: int,
        task_public_id: str,
        clock: Callable[[], datetime] = datetime.utcnow,
        policy: ToolOutputPolicy | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._manager = manager
        self._resolver = task_flag_resolver
        self._user_id = user_internal_id
        self._conversation_id = conversation_internal_id
        self._task_id = task_internal_id
        self._task_public_id = task_public_id
        self._clock = clock
        self._policy = policy or ToolOutputPolicy()

    async def __call__(self, record: dict[str, Any]) -> None:
        if not self._resolver.evaluate("CONTEXT_TOOL_OUTPUT_GOVERNANCE_ENABLED"):
            return

        envelope = dict(record.get("result") or {})
        tool_call_public_id = str(envelope.get("tool_call_id") or "").strip()
        if not tool_call_public_id:
            raise RuntimeError("governed tool output requires tool_call_id")

        now = self._clock()
        success = bool(envelope.get("success"))
        inputs = record.get("inputs")
        input_keys = sorted(inputs.keys()) if isinstance(inputs, dict) else []
        row = ToolCall(
            public_id=tool_call_public_id,
            user_id=self._user_id,
            conversation_id=self._conversation_id,
            task_id=self._task_id,
            tool_name=str(record.get("tool_name") or "unknown")[:128],
            status="success" if success else "failed",
            input_summary_json={"keys": input_keys},
            output_summary_json={"success": success},
            started_at=now,
            finished_at=now,
            duration_ms=int(envelope.get("duration_ms") or 0),
            created_at=now,
            updated_at=now,
        )
        async with self._session_factory() as session:
            await ToolCallRepository(session).create(row)
            await session.commit()

        await self._manager.manage(
            self._user_id,
            self._task_public_id,
            tool_call_public_id,
            raw_output=TypedToolOutput(
                text=serialize_tool_result(envelope),
                media_type="application/json",
            ),
            policy=self._policy,
            session_factory=self._session_factory,
        )


__all__ = ["ProductionToolOutputRecorder", "serialize_tool_result"]
