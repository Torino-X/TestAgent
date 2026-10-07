"""Evidence Segment — 从被压缩内容提取的关键证据段落（doc09 §28）。

写入 context_payloads（payload_type='evidence_segment'），内容存 metadata_json
（inline，owner-scope）。Rehydrate 时按需恢复，读取经 user_id ACL 校验。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.repositories.base import ensure_model_id
from app.utils.ids import generate_public_id

_DEFAULT_TTL_DAYS = 7


class EvidenceSegmentStore:
    """Evidence Segment 存取（payload_type='evidence_segment'，inline）。"""

    def __init__(self, session_factory=None) -> None:
        self._session_factory = session_factory

    async def store(
        self,
        *,
        user_id: int,
        workspace_key: str | None = None,
        conversation_id: int | None = None,
        task_id: int | None = None,
        segment_id: str,
        content: str,
        source_ref: str | None = None,
    ) -> int:
        """持久化一个证据段，返回 payload id。"""
        from app.models.context_engine import ContextPayload

        data = json.dumps(
            {"segment_id": segment_id, "content": content, "source_ref": source_ref},
            ensure_ascii=False,
        ).encode("utf-8")
        storage_key = generate_public_id("seg")
        now = datetime.now(timezone.utc)
        async with self._session_factory() as session:
            payload = ContextPayload(
                public_id=generate_public_id("payload"),
                user_id=user_id,
                workspace_key=workspace_key,
                conversation_id=conversation_id,
                task_id=task_id,
                source_type="context_compaction",
                source_public_id=segment_id,
                payload_type="evidence_segment",
                storage_backend="inline",
                storage_key=storage_key,
                storage_key_hash=hashlib.sha256(storage_key.encode("utf-8")).hexdigest(),
                size_bytes=len(data),
                char_count=len(data),
                estimated_tokens=max(1, len(data) // 3),
                sha256=hashlib.sha256(data).hexdigest(),
                encrypted=False,
                status="active",
                metadata_json={"segment_id": segment_id, "content": content, "source_ref": source_ref},
                created_at=now,
                expires_at=now + timedelta(days=_DEFAULT_TTL_DAYS),
            )
            await ensure_model_id(session, ContextPayload, payload)
            session.add(payload)
            await session.flush()
            await session.commit()
            return payload.id

    async def load(
        self,
        *,
        user_id: int,
        payload_id: int | None = None,
        segment_id: str | None = None,
    ) -> str | None:
        """按 payload_id 或 segment_id 读取证据段内容（ACL：user_id 必须匹配）。"""
        from sqlalchemy import select

        from app.models.context_engine import ContextPayload

        async with self._session_factory() as session:
            stmt = select(ContextPayload).where(
                ContextPayload.user_id == user_id,
                ContextPayload.payload_type == "evidence_segment",
                ContextPayload.status == "active",
                ContextPayload.deleted_at.is_(None),
            )
            if payload_id is not None:
                stmt = stmt.where(ContextPayload.id == payload_id)
            if segment_id is not None:
                stmt = stmt.where(ContextPayload.source_public_id == segment_id)
            result = await session.execute(stmt.limit(1))
            payload = result.scalar_one_or_none()
            if payload is None:
                return None
            md = payload.metadata_json or {}
            return md.get("content")


__all__ = ["EvidenceSegmentStore"]
# auto-appended module-level note: 压缩 evidence: 证据链(被压缩 message 引用 + 时间戳)。
# auto-appended module-level note: 压缩 evidence: 证据链(被压缩 message 引用 + 时间戳)。
