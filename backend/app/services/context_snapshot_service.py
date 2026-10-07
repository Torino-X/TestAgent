"""Context snapshot service — saves lightweight audit records for context construction.

F016: records what context was assembled for each LLM call without
saving the full prompt.  Saves included IDs, estimated tokens, and a
short preview.  All failures are swallowed — snapshotting must never
block the user's message flow.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.context_snapshot import ContextSnapshot
from app.repositories.context_snapshot_repository import ContextSnapshotRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)

_MAX_PREVIEW_CHARS = 1000


class ContextSnapshotService:
    """Saves lightweight audit records of context construction."""

    def __init__(self, session: AsyncSession) -> None:
        self._repo = ContextSnapshotRepository(session)

    async def save_snapshot(
        self,
        *,
        user_id: int,
        conversation_id: int | None = None,
        message_id: int | None = None,
        agent_task_id: int | None = None,
        llm_task_type: str,
        context_kind: str,
        included_message_ids: list[str] | None = None,
        included_file_ids: list[str] | None = None,
        included_task_ids: list[str] | None = None,
        summary_id: int | None = None,
        estimated_tokens: int = 0,
        context_preview: str = "",
    ) -> None:
        """Save a lightweight context audit snapshot.

        Never raises — failures are logged and swallowed.
        """
        try:
            now = utcnow()

            # Build a digest from the context composition (not the prompt itself)
            digest_input = _json.dumps(
                {
                    "msgs": included_message_ids or [],
                    "files": included_file_ids or [],
                    "tasks": included_task_ids or [],
                    "summary_id": summary_id,
                    "tokens": estimated_tokens,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            context_digest = hashlib.sha256(digest_input.encode()).hexdigest()[:64]

            # Truncate preview
            preview = (context_preview or "")[:_MAX_PREVIEW_CHARS]

            snapshot = ContextSnapshot(
                public_id=generate_public_id("snapshot"),
                user_id=user_id,
                conversation_id=conversation_id,
                message_id=message_id,
                agent_task_id=agent_task_id,
                llm_task_type=llm_task_type,
                context_kind=context_kind,
                included_message_ids=included_message_ids,
                included_file_ids=included_file_ids,
                included_task_ids=included_task_ids,
                included_artifact_ids=None,
                included_knowledge_ids=None,
                summary_id=summary_id,
                estimated_tokens=estimated_tokens,
                context_digest=context_digest,
                context_preview=preview,
                created_at=now,
            )
            await self._repo.create(snapshot)

            logger.debug(
                "ContextSnapshotService.save_snapshot: 已保存 | kind=%s | tokens=%d",
                context_kind, estimated_tokens,
            )
        except Exception as exc:
            logger.warning(
                "ContextSnapshotService.save_snapshot: 失败（不影响主流程）| kind=%s | error=%s",
                context_kind, exc,
            )


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (Context Assembly 审计快照):
#
#   链路:
#     任何走 ContextEngine 的 LLM 调用前:
#       → ContextEngine.assemble_context(...)
#         → 调用完后调 ContextSnapshotService.save_snapshot(
#             conversation_id, llm_call_id, included_ids, est_tokens, short_preview)
#           → 落库 llm_context_snapshots (status='completed' 或 'partial')
#
#   前端展示:
#     useContextUsage composable
#       → api/v1/conversations.py?include=context_usage
#         → ContextUsageService.get_current_usage(...)
#           → 读最近一次快照
#
# 关键约束(供开发者速查):
#   - F016: 只审计 included_ids + tokens + short_preview,**不存完整 prompt**;
#   - 失败 must be swallowed,绝对不能阻塞 ContextEngine 主流程;
#   - 监控告警:partial 状态过多 = ContextEngine 退化,需触发 CE-04 抢救。
