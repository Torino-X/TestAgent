"""Conversation service — CRUD operations backed by the conversations table.

Phase 1 (Step 7): 接入 Business Cache Redis — list / detail 走 cache-aside;
写操作 (create / update_title / delete) 通过 bump_generation 失效 list 缓存,
detail 走显式 invalidate.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.repositories.agent_task_repository import AgentTaskRepository
from app.repositories.conversation_repository import ConversationRepository
from app.repositories.file_repository import FileRepository
from app.repositories.message_repository import MessageRepository
from app.utils.datetime import utcnow
from app.utils.ids import generate_public_id

logger = logging.getLogger(__name__)


class ConversationService:
    """Conversation business logic — real database reads and writes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = ConversationRepository(session)
        self._task_repo = AgentTaskRepository(session)
        self._file_repo = FileRepository(session)
        self._msg_repo = MessageRepository(session)

    async def create(self, title: str, user_internal_id: int) -> dict:
        now = utcnow()
        conv = Conversation(
            public_id=generate_public_id("conversation"),
            user_id=user_internal_id,
            title=title or "新会话",
            status="active",
            created_at=now,
            updated_at=now,
        )
        conv = await self._repo.create(conv)

        # P0 收口:cache invalidation 必须严格在 commit 之后.
        # 用 ``register_after_commit`` 挂 hook;rollback 时不触发.
        try:
            from app.cache.domains.conversation_cache import (
                get_conversation_cache,
            )
            from app.db.sync import register_after_commit

            _uid = user_internal_id

            async def _do_bump(_session):
                try:
                    await get_conversation_cache().bump_generation(_uid)
                except Exception as cache_exc:  # noqa: BLE001
                    logger.warning(
                        "ConversationService.create: bump_generation failed | "
                        "user=%s | %s", _uid, cache_exc,
                    )

            register_after_commit(self._session, _do_bump)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ConversationService.create: cache hook register failed | user=%s | %s",
                user_internal_id, exc,
            )

        return self._to_summary(conv, message_count=0, file_count=0)

    async def list_conversations(self, user_internal_id: int) -> tuple[list[dict], int]:
        """List user's conversations with message/file counts.

        Phase 0 optimization: collapsed from ``2 + 2N`` SQL to a flat
        ``<=4`` SQL using GROUP BY batch aggregation.  Order of operations
        preserved (list_by_user first so empty result short-circuits the
        two batch count queries).

        Phase 1 (Step 7): cache-aside read with generation token.
        Hot path (chat sidebar) usually reads a cache hit.

        Total SQL count = 4 in the populated cold-cache path:
          1. ``ConversationRepository.list_by_user``
          2. ``ConversationRepository.count_by_user``
          3. ``MessageRepository.count_group_by_conversation``
          4. ``FileRepository.count_group_by_conversation``

        After Step 7: 0 SQL on warm cache.
        """
        from app.cache.domains.conversation_cache import (
            ConversationListDTO,
            get_conversation_cache,
        )

        async def _load() -> ConversationListDTO:
            convs = await self._repo.list_by_user(user_internal_id)
            total = await self._repo.count_by_user(user_internal_id)
            if not convs:
                return ConversationListDTO(summaries=[], total=total)

            conv_ids = [c.id for c in convs]
            msg_counts = await self._msg_repo.count_group_by_conversation(
                user_internal_id, conv_ids,
            )
            file_counts = await self._file_repo.count_group_by_conversation(
                user_internal_id, conv_ids,
            )
            summaries = [
                self._to_summary(
                    conv,
                    message_count=msg_counts.get(conv.id, 0),
                    file_count=file_counts.get(conv.id, 0),
                )
                for conv in convs
            ]
            return ConversationListDTO(summaries=summaries, total=total)

        cached = await get_conversation_cache().get_or_load_list(
            user_internal_id, _load, query_hash="with-project-v1",
        )
        # cached may be None when bypass path runs and loader returns None —
        # treat as empty list (matches the cold-cache empty semantics).
        if cached is None:
            return [], 0
        return cached.summaries, cached.total

    async def get_detail(self, public_id: str, user_internal_id: int) -> dict:
        """Cache-aside read for conversation detail.

        Cache miss path falls through to the Phase-0-style DB query.
        Detail is independent of the list generation token (per-conv Key),
        but :meth:`update_title` / :meth:`delete` still invalidate it
        explicitly so subsequent reads see fresh state.
        """
        from app.cache.domains.conversation_cache import (
            ConversationDetailDTO,
            get_conversation_cache,
        )

        async def _load() -> ConversationDetailDTO | None:
            conv = await self._repo.get_by_public_id(public_id)
            if not conv or conv.user_id != user_internal_id:
                return None
            detail = {
                "id": conv.public_id,
                "title": conv.title,
                "state": conv.status,
                "status": conv.status,
                "updated_at": conv.updated_at.isoformat() if conv.updated_at else "",
                "message_count": await self._msg_repo.count_by_conversation(conv.id),
                "file_count": await self._file_repo.count_by_conversation(conv.id),
                "files": [],
                "messages": [],
            }
            latest_task = await self._task_repo.get_latest_by_conversation(
                user_internal_id, conv.id,
            )
            detail["latest_task"] = self._to_task_summary(latest_task)
            return ConversationDetailDTO(payload=detail)

        cached = await get_conversation_cache().get_or_load_detail(
            user_internal_id, public_id, _load,
        )
        if cached is None:
            from app.core.exceptions import NotFoundError
            raise NotFoundError("会话")
        return cached.payload

    async def update_title(self, public_id: str, title: str) -> dict:
        await self._repo.update_title(public_id, title)

        # P0 收口:cache invalidation 必须严格在 commit 之后.
        # 用 after_commit hook,rollback 不触发.
        try:
            from app.cache.domains.conversation_cache import (
                get_conversation_cache,
            )
            from app.db.sync import register_after_commit

            conv = await self._repo.get_by_public_id(public_id)
            if conv is not None:
                _uid = conv.user_id
                _pid = public_id

                async def _do_invalidate(_session):
                    try:
                        cache = get_conversation_cache()
                        await cache.bump_generation(_uid)
                        await cache.invalidate_detail(_uid, _pid)
                    except Exception as cache_exc:  # noqa: BLE001
                        logger.warning(
                            "ConversationService.update_title: cache invalidate "
                            "failed | public_id=%s | %s", _pid, cache_exc,
                        )

                register_after_commit(self._session, _do_invalidate)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ConversationService.update_title: cache hook register failed | "
                "public_id=%s | %s", public_id, exc,
            )

        return {"id": public_id, "title": title}

    async def delete(self, public_id: str) -> None:
        # CPS-02: Conversation 删除 → 级联标记关联 Internal RAG 文档 + Chunk
        # 为 deleted_at，使 mysql_authoritative_recheck 在 ACL 阶段立即丢弃
        # 该 Conversation 的所有 chunk（status 仍 indexed，但 deleted_at 非空，
        # recheck 通过 doc.deleted_at IS NULL 校验会被剔除）。
        # ES/Qdrant 物理删除由后续 Retention 处理（不在本轮范围）。
        now = utcnow()
        conv = await self._repo.get_by_public_id(public_id)  # for user_id
        await self._repo.soft_delete(public_id, now)
        await self._invalidate_internal_rag(public_id, now)

        # P0 收口:cache invalidation 必须严格在 commit 之后.
        try:
            from app.cache.domains.conversation_cache import (
                get_conversation_cache,
            )
            from app.db.sync import register_after_commit

            if conv is not None:
                _uid = conv.user_id
                _pid = public_id

                async def _do_invalidate(_session):
                    try:
                        cache = get_conversation_cache()
                        await cache.invalidate_detail(_uid, _pid)
                        await cache.bump_generation(_uid)
                    except Exception as cache_exc:  # noqa: BLE001
                        logger.warning(
                            "ConversationService.delete: cache invalidate failed | "
                            "public_id=%s | %s", _pid, cache_exc,
                        )

                register_after_commit(self._session, _do_invalidate)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ConversationService.delete: cache hook register failed | "
                "public_id=%s | %s", public_id, exc,
            )

    async def _invalidate_internal_rag(self, conversation_public_id: str, now) -> None:
        """CPS-02: 标记该 Conversation 派生 workspace_key 下的所有
        ContextIndexDocument / ContextIndexChunk 为 deleted。

        仅按 (user_id, workspace_key='conversation:{public_id}') 过滤，
        不依赖当前已软删的 Conversation 行（Conversation DELETE 已在 soft_delete
        提交，但 workspace_key 本身是字符串 scope，不受 Conversation.deleted_at 影响）。
        """
        from sqlalchemy import select, update

        from app.models.context_engine import ContextIndexChunk, ContextIndexDocument
        from app.models.user import User
        from app.models.conversation import Conversation

        # 1) 通过 public_id 拿 conversation 内部 id + user_id
        #    （Conversation 已 soft_delete 但行还在；deleted_at IS NULL 过滤会跳过；
        #     这里改用不过滤 + 限定 public_id）
        result = await self._session.execute(
            select(Conversation).where(Conversation.public_id == conversation_public_id)
        )
        conv = result.scalar_one_or_none()
        if conv is None:
            # Conversation 不存在 → 没有关联 RAG 可清理
            return

        ws_key = f"conversation:{conversation_public_id}"

        # 2) UPDATE ContextIndexDocument.deleted_at
        await self._session.execute(
            update(ContextIndexDocument)
            .where(
                ContextIndexDocument.user_id == conv.user_id,
                ContextIndexDocument.workspace_key == ws_key,
                ContextIndexDocument.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )

        # 3) UPDATE ContextIndexChunk.deleted_at（document_id 子查询）
        await self._session.execute(
            update(ContextIndexChunk)
            .where(
                ContextIndexChunk.document_id.in_(
                    select(ContextIndexDocument.id).where(
                        ContextIndexDocument.user_id == conv.user_id,
                        ContextIndexDocument.workspace_key == ws_key,
                    )
                ),
                ContextIndexChunk.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )

    @staticmethod
    def _to_summary(conv: Conversation, message_count: int = 0, file_count: int = 0) -> dict:
        return {
            "id": conv.public_id,
            "title": conv.title,
            "state": conv.status,
            "status": conv.status,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else "",
            "message_count": message_count,
            "file_count": file_count,
            "project_id": getattr(conv, "_project_public_id", None),
            "project_name": getattr(conv, "_project_name", None),
        }

    @staticmethod
    def _to_task_summary(task) -> dict | None:
        if not task:
            return None
        return {
            "task_id": task.public_id,
            "task_type": task.task_type,
            "status": task.status,
            "events_url": f"/api/agent/tasks/{task.public_id}/events",
        }


# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (会话 CRUD + 时间线管理):
#
#   链路 (会话创建):
#     登录后首次打开 / 主动"新建会话":
#       → api/v1/conversations.py POST /conversations
#         → ConversationService.create_conversation(user_id, title?)
#           → Conversation 行落库 (state=empty, message_count=0, file_count=0)
#
#   链路 (会话列表 + 详情):
#     ChatView 打开 / 左侧历史栏渲染:
#       → api/v1/conversations.py GET /conversations
#         → ConversationService.list_conversations(user_id)
#       → 打开某会话:
#       → api/v1/conversations.py GET /conversations/{id}
#         → ConversationService.get_detail(id, user_id)
#
#   链路 (删除/恢复):
#     历史侧栏"删除"按钮 → DELETE /conversations/{id}
#       → ConversationService.soft_delete(...) / hard_delete(...)
#     任务终态:task 已完成后 message_count / file_count 重新统计(本服务也提供)。
#
# 关键约束(供开发者速查):
#   - 会话 list query 在大表上必须用复合索引 (user_id, updated_at DESC);
#   - 任何 conversation 写操作必须校验 owner,避免越权访问;
#   - 主动"归档/隐藏"用 soft_delete,不可物理删除(为了审计可恢复);
#   - timeline 构建由 conversationStore 前端 + get_detail 接口共同完成,
#     后端只提供原子数据,前端 reducer 负责 timeline 拼接。
