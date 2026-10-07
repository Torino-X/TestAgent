"""CE-05 Alert Service — append-only 告警事件。

- context_alert / context_alert_ack / context_alert_resolved（agent_events）
- idempotency：普通 INSERT → UNIQUE 命中 → 查已有 payload_digest →
  同 no-op / 异 409
- payload_digest 存 agent_events.payload_json.payload_digest（保留 key）
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from sqlalchemy import select

logger = logging.getLogger(__name__)


class AlertConflict(Exception):
    """同 key 不同 digest → 409 context.alert.conflict。"""


class AlertIntegrityError(Exception):
    """payload_digest 字段缺失 → DATA_INTEGRITY_ERROR。"""


def _canonical(obj: dict) -> bytes:
    # 排除 created_at/request_id 等非确定性字段
    filtered = {
        k: v for k, v in obj.items()
        if k not in {"created_at", "request_id", "id", "sequence_no"}
    }
    return json.dumps(filtered, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def payload_digest(obj: dict) -> str:
    return hashlib.sha256(_canonical(obj)).hexdigest()


class AlertService:
    """告警事件 append-only 写入（firing/ack/resolved）。"""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _alert_key(alert_id: str, kind: str, actor_public_id: str | None = None) -> str:
        if kind == "firing":
            return f"alert:{alert_id}:firing"
        if kind == "ack":
            return f"alert:{alert_id}:ack:{actor_public_id or '?'}"
        if kind == "resolved":
            return f"alert:{alert_id}:resolved:{actor_public_id or '?'}"
        raise ValueError(f"未知 alert kind: {kind}")

    async def _insert_alert_event(
        self,
        *,
        alert_id: str,
        kind: str,
        payload: dict,
        actor_public_id: str | None,
        user_id: int,
    ) -> dict:
        """普通 INSERT + UNIQUE 冲突 → 查已有 digest → 同 no-op / 异 409。"""
        from app.models.agent_event import AgentEvent
        from sqlalchemy.exc import IntegrityError

        from datetime import datetime, timezone

        key = self._alert_key(alert_id, kind, actor_public_id)
        digest = payload_digest(payload)
        event = AgentEvent(
            public_id=f"alert_{uuid.uuid4().hex[:32]}",
            user_id=user_id,
            conversation_id=0,
            task_id=0,
            event_type=f"context_alert_{kind}",
            message_type=f"context_alert_{kind}",
            title=f"告警 {kind}",
            content=None,
            payload_json={
                "alert_id": alert_id,
                "payload_digest": digest,
                "status": kind,
                **{k: v for k, v in payload.items() if k != "alert_id"},
            },
            status="created",
            idempotency_key=key,
            created_at=datetime.now(timezone.utc),
        )
        async with self._session_factory() as session:
            session.add(event)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # UNIQUE(idempotency_key) 命中 → 查已有事件
                existing = (
                    await session.execute(
                        select(AgentEvent).where(AgentEvent.idempotency_key == key)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise AlertIntegrityError("已有事件缺失")
                existing_digest = (existing.payload_json or {}).get("payload_digest")
                if existing_digest is None:
                    raise AlertIntegrityError("已有事件 payload_digest 缺失")
                if existing_digest == digest:
                    return {"alert_id": alert_id, "status": kind, "idempotent": True}
                raise AlertConflict(f"同 key 不同 digest: {alert_id}")

        return {"alert_id": alert_id, "status": kind, "idempotent": False}

    async def fire(self, *, alert_id: str, rule_id: str, severity: str, user_id: int, **extra) -> dict:
        payload = {"rule_id": rule_id, "severity": severity, **extra}
        return await self._insert_alert_event(
            alert_id=alert_id, kind="firing", payload=payload,
            actor_public_id=None, user_id=user_id,
        )

    async def ack(self, *, alert_id: str, actor_public_id: str, user_id: int, **extra) -> dict:
        return await self._insert_alert_event(
            alert_id=alert_id, kind="ack", payload={"actor": actor_public_id, **extra},
            actor_public_id=actor_public_id, user_id=user_id,
        )

    async def resolve(self, *, alert_id: str, actor_public_id: str, user_id: int, **extra) -> dict:
        return await self._insert_alert_event(
            alert_id=alert_id, kind="resolved", payload={"actor": actor_public_id, **extra},
            actor_public_id=actor_public_id, user_id=user_id,
        )

    async def list_alerts(self, *, user_id: int, limit: int = 50) -> list[dict]:
        from app.models.agent_event import AgentEvent

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(AgentEvent)
                    .where(
                        AgentEvent.event_type.like("context_alert%"),
                        AgentEvent.user_id == user_id,
                    )
                    .order_by(AgentEvent.id.desc())
                    .limit(min(limit, 100))
                )
            ).scalars().all()
        return [
            {
                "event_public_id": r.public_id,
                "event_type": r.event_type,
                "alert_id": (r.payload_json or {}).get("alert_id"),
                "status": (r.payload_json or {}).get("status"),
                "rule_id": (r.payload_json or {}).get("rule_id"),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]


__all__ = ["AlertService", "AlertConflict", "AlertIntegrityError", "payload_digest"]
