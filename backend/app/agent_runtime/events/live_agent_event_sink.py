"""LiveAgentEventSink — Phase 2.6 多 Worker 生产型 AgentEventSink。

Source-of-truth 写入顺序:
  1. SequenceNumberAllocator.next()      (Redis INCR → DB FOR UPDATE fallback)
  2. EventRepository.create_with_idempotency() — INSERT IGNORE;UNIQUE (task_id, sequence_no) 命中 → 静默吞
  3. LiveEventBus.publish(task_id, event_dict) — Redis pub/sub → 多 worker fan-out

Sequence + DB 写入 + publish 三者解耦:
  * 步骤 1-2 必须在 MySQL 落库后,才能把 sequence_no 真正"颁发"给订阅者
  * 步骤 3 是 best-effort;失败仅 log — 历史已在 MySQL
  * sequence_no = None 时(双轨过渡期),skip step 2 的 dedup 但写 DB

Snapshot 由 event_id (UUID7) 主导;sequence_no 是单调标量,event_id 是唯一事件 id。
"""

from __future__ import annotations

import json as _json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .sequence_allocator import SequenceNumberAllocator, compute_idempotency_key

logger = logging.getLogger(__name__)


class LiveAgentEventSink:
    """Phase 2.6 生产型 AgentEventSink。

    取 ``(sequence_no, event_id, idempotency_key)`` 三元组,落 MySQL + 推 LiveEventBus。
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Any],
        event_repo: Any,
        allocator: SequenceNumberAllocator,
        bus: Any,
        task_internal_id: int,
        task_public_id: Optional[str] = None,
        conversation_internal_id: int,
        user_internal_id: int,
        graph_run_id: str,
        graph_version: Optional[str] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._repo = event_repo
        self._allocator = allocator
        self._bus = bus
        self._task_internal_id = int(task_internal_id)
        self._task_public_id = str(task_public_id or task_internal_id)
        self._conversation_internal_id = int(conversation_internal_id)
        self._user_internal_id = int(user_internal_id)
        self._graph_run_id = graph_run_id
        self._graph_version = graph_version
        self._clock = clock or _default_clock

    async def emit(
        self,
        *,
        task_id: str,
        node_name: str,
        event_type: str,
        title: str,
        content: str,
        payload: Optional[dict] = None,
        graph_run_id: Optional[str] = None,
        event_schema_version: int = 1,
        sequence_no: Optional[int] = None,
        event_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """fan-out:alloc sequence_no → 写 DB (幂等) → 推 LiveEventBus。

        全部失败仅 log,不抛(节点不应因 sink 故障失败)。
        """
        effective_graph_run_id = graph_run_id or self._graph_run_id
        effective_event_id = event_id or self._generate_event_id()
        now = self._clock()

        # 1) 取 sequence_no(若 caller 没给)
        if sequence_no is None:
            try:
                sequence_no = await self._allocator.next(task_internal_id=self._task_internal_id)
            except Exception:
                logger.warning(
                    "LiveAgentEventSink: allocator.next failed (non-fatal); sequence_no=None",
                    exc_info=True,
                )
                sequence_no = None

        if idempotency_key is None and sequence_no is not None:
            idempotency_key = compute_idempotency_key(
                task_id=str(self._task_internal_id),
                graph_run_id=effective_graph_run_id or "",
                node_name=node_name,
                event_type=event_type,
                sequence_no=sequence_no,
            )

        event_dict: dict[str, Any] = {
            "event_id": effective_event_id,
            "sequence_no": sequence_no,
            "event_type": event_type,
            "message_type": event_type,
            "title": title,
            "content": content,
            "payload": dict(payload or {}),
            "task_id": self._task_public_id,
            "node_name": node_name,
            "graph_run_id": effective_graph_run_id,
            "graph_version": self._graph_version,
            "event_schema_version": event_schema_version,
            "idempotency_key": idempotency_key,
            "status": "created",
            "created_at": now.isoformat() if isinstance(now, datetime) else str(now),
        }

        # 2) 写 DB(幂等)
        await self._persist_row(event_dict)

        # 3) 推 LiveEventBus(best-effort)
        logger.info(
            "LiveAgentEventSink.emit: 发布事件 | task=%s | event_type=%s | title=%s",
            self._task_public_id, event_type, title,
        )
        await self._publish(event_dict)

        return event_dict

    async def _persist_row(self, event_dict: dict[str, Any]) -> None:
        try:
            from app.models.agent_event import AgentEvent

            agent_event = AgentEvent(
                public_id=str(event_dict["event_id"]),
                user_id=self._user_internal_id,
                conversation_id=self._conversation_internal_id,
                task_id=self._task_internal_id,
                event_type=str(event_dict["event_type"]),
                message_type=str(event_dict["message_type"]),
                title=str(event_dict.get("title") or ""),
                content=str(event_dict.get("content") or ""),
                payload_json=_serialize_payload(event_dict.get("payload")),
                status="created",
                sequence_no=event_dict.get("sequence_no"),
                graph_run_id=event_dict.get("graph_run_id"),
                graph_version=event_dict.get("graph_version"),
                node_name=event_dict.get("node_name"),
                event_schema_version=int(event_dict.get("event_schema_version") or 1),
                idempotency_key=event_dict.get("idempotency_key"),
                created_at=event_dict.get("created_at"),
            )
            await self._repo.create_with_idempotency(
                event_id=str(event_dict["event_id"]),
                idempotency_key=event_dict.get("idempotency_key"),
                event=agent_event,
            )
        except Exception:
            logger.warning(
                "LiveAgentEventSink: DB row write failed (swallowed)",
                exc_info=True,
            )

    async def _publish(self, event_dict: dict[str, Any]) -> None:
        try:
            await self._bus.publish(
                task_id=self._task_public_id, event=event_dict
            )
        except Exception:
            logger.warning(
                "LiveAgentEventSink: bus.publish failed (swallowed; history in MySQL)",
                exc_info=True,
            )

    @staticmethod
    def _generate_event_id() -> str:
        """UUID7 优先(自然时间序);fallback UUID4。"""
        try:
            return str(uuid.uuid7())
        except AttributeError:
            return str(uuid.uuid4())


def _serialize_payload(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return _json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return None


def _default_clock() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


__all__ = ["LiveAgentEventSink"]
