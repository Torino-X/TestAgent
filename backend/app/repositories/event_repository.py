"""Agent event repository."""

from __future__ import annotations

import json as _json

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_event import AgentEvent
from app.repositories.base import BaseRepository


class EventRepository(BaseRepository[AgentEvent]):
    model = AgentEvent

    def __init__(self, session: AsyncSession):
        super().__init__(session)

    async def list_by_task(
        self,
        task_internal_id: int,
        limit: int = 50,
        *,
        cursor: int | None = None,
        return_total: bool = False,
    ) -> "list[dict] | tuple[list[dict], int | None, int]":
        """列 AgentTask 事件,支持 cursor 分页与真实 total。

        Phase 2.8R-K 降级:MySQL 默认 sort_buffer_size(256K)在 ORDER BY
        coalesce(sequence_no, 0) ASC, id ASC + LIMIT 200 容易 OOM(1038 错)。

        Phase 2.9A.26+: Returns dict list (not ORM hydrated) so we can
        attach a ``canonical_order`` column that is stable across:
          * legacy rows where ``sequence_no IS NULL``
          * mixed batches where some rows have ``sequence_no`` and some don't.
        The canonical order is:
          1. rows with a non-null ``sequence_no`` ordered by it ASC
          2. rows with null ``sequence_no`` ordered by ``created_at, id`` ASC
          3. both buckets merged by their ordinal so the timeline is stable.

        Phase 2.9A.27: Hydration 路径需要拉完全部事件(旧实现仅 ``LIMIT 50``
        会把 seq>50 的 task_completed / docx_format_checked 等截掉,导致
        runState 永远 running)。新增 ``cursor``(基于 canonical_order)+ ``limit``
        参数 + ``return_total`` 开关;默认 ``return_total=False`` 保持旧
        `list[dict]` 签名不变,避免破坏 Phase 2.6 / 2.8R-D 等历史 caller。

        Args:
            task_internal_id: AgentTask.id
            limit: 每次返回的最大事件数(默认 50,前端 Hydrate 显式传 100)
            cursor: 上次分页最后一条事件的 canonical_order;None 表示从头开始
            return_total: True 时额外 COUNT(*) 并返回 (events, next_cursor, total),
              False 时只返回 events(向后兼容)

        Returns:
            ``return_total=False`` → ``list[dict]``(旧默认)。
            ``return_total=True``  → ``(events, next_cursor, total)`` —
            next_cursor 为本批最后一条的 canonical_order;若本批不满 limit
            或已拉完则 next_cursor=None;total 是 DB 真实事件总数
            (COUNT(*),与 limit 解耦,前端可凭此决定是否继续翻页)。
        """
        # 防御性:limit 给个上限,挡住前端误传 10000 引发 MySQL OOM
        safe_limit = max(1, min(int(limit), 500))

        params: dict = {"tid": int(task_internal_id), "lim": safe_limit}
        cursor_clause = ""
        if cursor is not None:
            cursor_clause = "AND co > :cur"
            params["cur"] = int(cursor)

        result = await self.session.execute(
            text(
                f"""
                SELECT
                  public_id, event_type, message_type, title, content,
                  payload_json, status, sequence_no, graph_run_id,
                  graph_version, node_name, event_schema_version,
                  idempotency_key, created_at, eid, co
                FROM (
                  SELECT
                    e.public_id, e.event_type, e.message_type, e.title, e.content,
                    e.payload_json, e.status, e.sequence_no, e.graph_run_id,
                    e.graph_version, e.node_name, e.event_schema_version,
                    e.idempotency_key, e.created_at, e.id AS eid,
                    COALESCE(
                      e.sequence_no,
                      1000000000 + ROW_NUMBER() OVER (
                        PARTITION BY e.task_id
                        ORDER BY e.sequence_no IS NULL DESC, e.created_at ASC, e.id ASC
                      )
                    ) AS co
                  FROM agent_events e
                  WHERE e.task_id = :tid
                ) _co
                WHERE co > -1 {cursor_clause}
                ORDER BY co ASC
                LIMIT :lim
                """
            ),
            params,
        )
        rows = result.fetchall()
        events: list[dict] = [
            {
                "public_id": r[0],
                "event_type": r[1],
                "message_type": r[2],
                "title": r[3],
                "content": r[4],
                "payload_json": r[5],
                "status": r[6],
                "sequence_no": r[7],
                "graph_run_id": r[8],
                "graph_version": r[9],
                "node_name": r[10],
                "event_schema_version": r[11],
                "idempotency_key": r[12],
                "created_at": r[13],
                "internal_id": r[14],  # eid
                "canonical_order": r[15],  # co
            }
            for r in rows
        ]

        if not return_total:
            return events

        # return_total=True: 真实总数 + next_cursor
        total = int(
            (
                await self.session.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_events WHERE task_id = :tid"
                    ),
                    {"tid": int(task_internal_id)},
                )
            ).scalar()
            or 0
        )
        # next_cursor 语义:本批最后一条的 canonical_order;caller 用此翻页。
        # 终止条件由 caller 判定:
        #   - 本批 events 为空(已无更多)→ 停止
        #   - 累计 events 数已达 total → 停止
        # 这样 repo 自身不需要持有 cumulative 状态,避免边界判定不准。
        next_cursor: int | None = None
        if events:
            next_cursor = int(events[-1]["canonical_order"])
        return events, next_cursor, total

    async def list_by_task_since_sequence(
        self,
        task_internal_id: int,
        *,
        last_sequence_no: int | None = None,
        last_public_id: str | None = None,
        last_canonical_order: int | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """SSE Last-Event-ID replay — 返回 dict list(不强制 ORM hydrate)。

        Phase 2.9A.26+: ``last_canonical_order`` 优先于 ``last_sequence_no`` —
        Replay 不能跳过 canonical_order 已经被规范化过的旧事件,否则会重复
        emit ``task_created`` 等本来没有 sequence_no 的事件。

        优先按 sequence_no > last;fallback 按 public_id > last_public_id。
        用于 HistoryDrainer;不在 Phase 2.6 之外使用。
        """
        if last_canonical_order is not None:
            cond = "AND canonical_order > :last_co"
            params = {
                "tid": int(task_internal_id),
                "last_co": int(last_canonical_order),
                "limit": int(limit),
            }
        elif last_sequence_no is not None:
            cond = "AND sequence_no > :last_seq"
            params = {
                "tid": int(task_internal_id),
                "last_seq": int(last_sequence_no),
                "limit": int(limit),
            }
        elif last_public_id:
            cond = "AND public_id > :last_pid"
            params = {
                "tid": int(task_internal_id),
                "last_pid": last_public_id,
                "limit": int(limit),
            }
        else:
            cond = ""
            params = {"tid": int(task_internal_id), "limit": int(limit)}

        result = await self.session.execute(
            text(
                f"""
                SELECT
                  e.public_id, e.event_type, e.message_type, e.title, e.content,
                  e.payload_json, e.status, e.sequence_no, e.graph_run_id,
                  e.graph_version, e.node_name, e.event_schema_version,
                  e.idempotency_key, e.created_at, e.id,
                  COALESCE(
                    e.sequence_no,
                    1000000000 + ROW_NUMBER() OVER (
                      PARTITION BY e.task_id
                      ORDER BY e.sequence_no IS NULL DESC, e.created_at ASC, e.id ASC
                    )
                  ) AS canonical_order
                FROM agent_events e
                WHERE e.task_id = :tid {cond}
                ORDER BY canonical_order ASC
                LIMIT :limit
                """
            ),
            params,
        )
        rows = result.fetchall()
        return [
            {
                "public_id": r[0],
                "event_type": r[1],
                "message_type": r[2],
                "title": r[3],
                "content": r[4],
                "payload_json": r[5],
                "status": r[6],
                "sequence_no": r[7],
                "graph_run_id": r[8],
                "graph_version": r[9],
                "node_name": r[10],
                "event_schema_version": r[11],
                "idempotency_key": r[12],
                "created_at": r[13],
                "internal_id": r[14],
                "canonical_order": r[15],
            }
            for r in rows
        ]

    async def create(self, event: AgentEvent) -> AgentEvent:
        # aiomysql driver chokes on dict params for JSON columns —
        # use raw INSERT with explicit json.dumps instead of ORM flush.
        # Handle both dict and pre-serialised string input.
        await self.session.execute(
            text(
                "INSERT INTO agent_events "
                "(public_id, user_id, conversation_id, task_id, event_type, "
                "message_type, title, content, payload_json, status, "
                "sequence_no, graph_run_id, graph_version, node_name, "
                "event_schema_version, idempotency_key, created_at) "
                "VALUES "
                "(:pid, :uid, :cid, :tid, :et, :mt, :title, :content, :pj, :st, "
                " :seq, :grid, :gver, :nn, :esv, :idem, :now)"
            ),
            {
                "pid": event.public_id,
                "uid": event.user_id,
                "cid": event.conversation_id,
                "tid": event.task_id,
                "et": event.event_type,
                "mt": event.message_type,
                "title": event.title,
                "content": event.content,
                "pj": self._serialize_json(event.payload_json),
                "st": event.status,
                "seq": event.sequence_no,
                "grid": event.graph_run_id,
                "gver": event.graph_version,
                "nn": event.node_name,
                # Phase 2.8R-F: 防止 ORM 字段为 None 时 SQL NOT NULL 约束失败
                "esv": event.event_schema_version if event.event_schema_version is not None else 1,
                "idem": event.idempotency_key,
                "now": event.created_at,
            },
        )
        await self.session.flush()
        return event

    async def create_with_idempotency(
        self,
        *,
        event_id: str | None,
        idempotency_key: str | None,
        event: AgentEvent,
    ) -> bool:
        """幂等写入 — INSERT IGNORE;UNIQUE (task_id, sequence_no) 命中时静默吞。

        Returns:
            True = 首次写入
            False = 重复(已存在)
        """
        try:
            # 设 event.public_id = event_id (复用 AgentEvent 写入路径);
            # event_id 是 LiveAgentEventSink 生成的 UUID7,与 generate_public_id 同形态
            if event_id:
                event.public_id = event_id
            if idempotency_key:
                event.idempotency_key = idempotency_key
            await self.session.execute(
                text(
                    "INSERT IGNORE INTO agent_events "
                    "(public_id, user_id, conversation_id, task_id, event_type, "
                    " message_type, title, content, payload_json, status, "
                    " sequence_no, graph_run_id, graph_version, node_name, "
                    " event_schema_version, idempotency_key, created_at) "
                    "VALUES "
                    "(:pid, :uid, :cid, :tid, :et, :mt, :title, :content, :pj, :st, "
                    " :seq, :grid, :gver, :nn, :esv, :idem, :now)"
                ),
                {
                    "pid": event.public_id,
                    "uid": event.user_id,
                    "cid": event.conversation_id,
                    "tid": event.task_id,
                    "et": event.event_type,
                    "mt": event.message_type,
                    "title": event.title,
                    "content": event.content,
                    "pj": self._serialize_json(event.payload_json),
                    "st": event.status,
                    "seq": event.sequence_no,
                    "grid": event.graph_run_id,
                    "gver": event.graph_version,
                    "nn": event.node_name,
                    "esv": event.event_schema_version,
                    "idem": event.idempotency_key,
                    "now": event.created_at,
                },
            )
            await self.session.flush()
            return True
        except IntegrityError:
            # UNIQUE INDEX 命中 → 已写入 → rollback & return False
            await self.session.rollback()
            return False

    @staticmethod
    def _serialize_json(value: dict | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return _json.dumps(value, ensure_ascii=False, default=str)


# 模块定位:AgentEvent 仓储(细粒度 SSE + 历史)
#
# 链路:
#   LangGraph 节点 emit → EventRepository.save → SSE 推 LiveAgentEventBus
#   api/v1/agent_tasks/{id}/event-list → 历史回查
#
# 关键约束:
#   - idempotency_key UNIQUE 阻止双进程并发 emit;
#   - sequence 在 task_id 内单调增,复合索引;
#   - payload 字段白名单脱敏;
#   - 直接 INSERT 是反模式(必须走本仓,确保统一脱敏)。
