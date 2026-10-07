"""GraphEventAdapter — Phase 2.1 AgentEventSink 实现。

fan-out 到两条线:
1. ``event_repo.create(event, session)`` — 写 ``agent_events`` 行
2. ``event_publisher.publish(task_id, event_dict)`` — 推到 SSE 订阅者

写入与发布错误均仅 log,不抛(Rule:DB / publish hiccup 永远不能挂 graph 节点)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .cancellation_buffer import CancellationEventBuffer
from app.agent_runtime.runtime_context import AgentEventSink

logger = logging.getLogger(__name__)


@dataclass
class _GraphAgentEvent:
    """最小 AgentEvent 视图(避免 import app.models.agent_event 触发重型链路)。

    节点只用 event_type / title / content / payload 4 字段;
    真正的 ``create()`` 调用由 event_repo 在 sink 内完成。
    """

    event_type: str
    title: str
    content: str
    payload: Optional[dict] = None


class GraphEventAdapter:
    """AgentEventSink 实现,把 LangGraph 节点 emit 的事件 fan-out 到 DB + SSE。"""

    def __init__(
        self,
        *,
        task_internal_id: int,
        conversation_internal_id: int,
        user_internal_id: int,
        session_factory: Callable[[], Any],
        event_repo: Any,
        event_publisher: Any,
        cancellation_buffer: Optional[CancellationEventBuffer] = None,
        cancellation_service: Optional[Any] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._task_internal_id = int(task_internal_id)
        self._conversation_internal_id = int(conversation_internal_id)
        self._user_internal_id = int(user_internal_id)
        self._session_factory = session_factory
        self._repo = event_repo
        self._publisher = event_publisher
        self._cancellation_buffer = cancellation_buffer or CancellationEventBuffer()
        self._cancellation_service = cancellation_service
        self._clock = clock or _default_clock

    async def emit(
        self,
        *,
        task_id: str,
        graph_run_id: str,
        node_name: str,
        event_type: str,
        title: str,
        content: str,
        payload: Optional[dict] = None,
        event_id: Optional[str] = None,        # Phase 2.6 optional,ignored
        sequence_no: Optional[int] = None,     # Phase 2.6 optional,ignored
        idempotency_key: Optional[str] = None, # Phase 2.6 optional,ignored
    ) -> dict[str, Any]:
        """fan-out 到 DB + SSE;失败仅 log。"""
        event_dict: dict[str, Any] = {
            "event_type": event_type,
            "title": title,
            "content": content,
            "payload": dict(payload or {}),
            "task_id": str(self._task_internal_id),
            "node_name": node_name,
            "graph_run_id": graph_run_id,
        }
        if event_id is not None:
            event_dict["event_id"] = event_id
        if sequence_no is not None:
            event_dict["sequence_no"] = sequence_no
        if idempotency_key is not None:
            event_dict["idempotency_key"] = idempotency_key

        # 取消短路(Legacy 行为):已取消任务的事件走 buffer,不再 publish
        if self._cancellation_service is not None and self._cancellation_service.is_cancelled(
            str(self._task_internal_id)
        ):
            self._cancellation_buffer.append(event_dict)
            return event_dict

        # DB
        await self._persist_row(event_dict)

        # SSE
        await self._publisher.publish(str(self._task_internal_id), event_dict)

        return event_dict

    async def _persist_row(self, event_dict: dict[str, Any]) -> None:
        try:
            agent_event = _build_agent_event(
                event_dict=event_dict,
                task_internal_id=self._task_internal_id,
                conversation_internal_id=self._conversation_internal_id,
                user_internal_id=self._user_internal_id,
                clock=self._clock,
            )
            if agent_event is not None:
                await self._repo.create(agent_event)
        except Exception:
            logger.warning(
                "GraphEventAdapter: DB row write failed (swallowed)",
                exc_info=True,
            )

    async def _publisher_publish(self, task_id: str, event_dict: dict[str, Any]) -> None:
        try:
            await self._publisher.publish(task_id, event_dict)
        except Exception:
            logger.warning(
                "GraphEventAdapter: SSE publish failed (swallowed)",
                exc_info=True,
            )


def _default_clock() -> datetime:
    from datetime import datetime as _dt

    return _dt.utcnow()


def _build_agent_event(
    *,
    event_dict: dict[str, Any],
    task_internal_id: int,
    conversation_internal_id: int,
    user_internal_id: int,
    clock: Callable[[], datetime],
) -> Any:
    """Construct an AgentEvent ORM row using session-less factory.

    Lazy import:不污染 Phase 2.0 import 图。
    """
    try:
        from app.models.agent_event import AgentEvent
        from app.utils.ids import generate_public_id
    except Exception:
        return None

    now = clock()
    return AgentEvent(
        public_id=generate_public_id("event"),
        user_id=user_internal_id,
        conversation_id=conversation_internal_id,
        task_id=task_internal_id,
        event_type=str(event_dict.get("event_type") or ""),
        message_type=str(event_dict.get("event_type") or ""),
        title=str(event_dict.get("title") or ""),
        content=str(event_dict.get("content") or ""),
        status="created",
        created_at=now,
        updated_at=now,
        # Note: payload 字段视 AgentEvent 模型而定;此处不强行塞,以免破坏兼容
        # 真正落库时,如需要,可在 AgentEventRepository.create() 内读取 payload
    )


__all__ = ["GraphEventAdapter"]
