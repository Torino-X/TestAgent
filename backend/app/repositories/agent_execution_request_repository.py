"""AgentExecutionRequestRepository — Phase 2.8R-B Outbox 队列 CRUD。

设计核心:
  * :meth:`create` — 在 message_service 与 AgentTask 同一事务内写入,
    UNIQUE(idempotency_key) 防重复入队(任务重试时上层负责换 key)。
  * :meth:`claim_next` — Worker 用 ``SELECT ... FOR UPDATE SKIP LOCKED``
    原子领取 next 任务;同时设置 lease_owner + lease_expires_at,双重锁。
  * :meth:`mark_running` / `mark_completed` / `mark_failed` — 状态转换。
  * :meth:`reap_expired_leases` — 回收过期 lease,任务重新进入可领队列。

**约束**:
  * 不直接 commit;所有写都在调用方事务范围内,
    跨表一致(守 #18)。
  * 不发 SSE 事件;事件流由 event_repository 与 event_publisher 处理。
"""

from __future__ import annotations

import hashlib
import json as _json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_execution_request import AgentExecutionRequest


# 领任务后的 lease 默认有效期
DEFAULT_LEASE_TTL_SECONDS = 600


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_idempotency_key(task_public_id: str, request_type: str, suffix: str = "") -> str:
    """生成默认 idempotency_key。

    格式: ``f"{task_public_id}|{request_type}|{suffix}"``
    Phase 2.8R-B 内层默认;调用方如需更精细可传 idempotency_key 覆盖。
    """
    parts = [task_public_id or "", request_type or "new_task", suffix]
    return "|".join(p for p in parts if p)


def _public_id_for_idempotency_key(
    task_public_id: str,
    request_type: str,
    idempotency_key: str,
) -> str:
    """Build a stable, unique, database-safe public id for an outbox row.

    A task can legitimately enqueue more than one request of the same type:
    each preparation-clarification card has a distinct confirmation id in its
    idempotency key. Task id plus request type alone is therefore not unique.
    """
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
    # The database column is VARCHAR(64). Preserve a useful prefix while
    # reserving a deterministic suffix to make different request rows unique.
    return f"exq-{task_public_id[:28]}-{request_type[:14]}-{digest}"


class AgentExecutionRequestRepository:
    """Outbox 表直接 repository(BaseRepository 太轻量;独立写便于 future 扩展)。

    **不继承 BaseRepository** — 因为 status / lease 字段不在 BaseRepository
    默认辅助函数中。
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── 入队 ────────────────────────────────────────────────────

    async def enqueue_new_task(
        self,
        *,
        task_public_id: str,
        task_internal_id: int,
        engine_type: str,
        request_type: str = "new_task",
        graph_name: str | None = None,
        graph_version: str | None = None,
        payload: dict | None = None,
        idempotency_key: str | None = None,
    ) -> AgentExecutionRequest | None:
        """在当前事务内写一行 ``status=queued`` 的 Outbox 条目。

        Phase 2.9A.4 关键修复:**不调 ``session.rollback()``**。
        rollback 会把调用方之前在同一 Session 中的所有写入
        (例如 confirm 流程的 confirmation/task/event 写入)全部回滚,
        而 endpoint 仍会返回 success — 留下"看似成功但 DB 啥都没改"的脏状态。

        这里改用 pre-check + IntegrityError 兜底:
          * 先 SELECT 查 idempotency_key 是否已存在,避免绝大多数冲突;
          * flush 触发 UNIQUE 约束时,让外层 get_db 的 commit 处理
            (IntegrityError 会被 SQLAlchemy 包装,只要我们不主动 rollback,
            整个事务仍可提交,只是这一行没插入)。

        Returns:
            新建对象;若 ``idempotency_key`` 已存在 → 返回 None。
        """
        from sqlalchemy import select as _select

        ikey = idempotency_key or _default_id_idem_key(task_public_id, request_type)
        # Existing JSON payload is the only safe serializable cross-worker
        # carrier.  Store W3C text only; never persist Span/Tracer objects or
        # runtime context, and do not require a database migration.
        payload = dict(payload or {})
        try:
            from app.core.observability import inject_trace_context
            carrier = inject_trace_context()
            if carrier:
                payload.setdefault("_observability", {}).update(carrier)
        except Exception:
            pass

        # 1) Pre-check:多数幂等命中走这里,完全不写 DB。
        # 仅当 session 有 execute 时做 pre-check;真实 AsyncSession 有,
        # 但旧版 FakeSession 测试 mock 可能没实现,fallback 到直接 flush。
        if hasattr(self.session, "execute"):
            try:
                existing_row = await self.session.execute(
                    _select(AgentExecutionRequest).where(
                        AgentExecutionRequest.idempotency_key == ikey
                    ).limit(1)
                )
                if existing_row.scalar_one_or_none() is not None:
                    return None
            except Exception:
                # Pre-check 失败不阻塞入队,继续尝试 INSERT
                pass

        # 2) 尝试插入;并发场景下 INSERT 仍可能撞 UNIQUE → 不 rollback
        row = AgentExecutionRequest(
            public_id=_public_id_for_idempotency_key(
                task_public_id, request_type, ikey
            ),
            task_id=task_internal_id,
            request_type=request_type,
            engine_type=engine_type,
            graph_name=graph_name,
            graph_version=graph_version,
            payload_json=payload or None,
            status="queued",
            attempt_count=0,
            available_at=utcnow(),
            idempotency_key=ikey,
        )
        # Phase 2.9A.4:用 SAVEPOINT 隔离本行写入。
        # 失败时只回滚嵌套事务,不污染外层 confirm 事务。
        # 旧版 FakeSession 测试 mock 不支持 begin_nested → fallback flush。
        if hasattr(self.session, "begin_nested"):
            try:
                async with self.session.begin_nested():
                    # AsyncSession starts a nested transaction by flushing
                    # pending changes. Add the row only after the savepoint is
                    # active so a duplicate cannot poison the caller's whole
                    # confirmation transaction before we can isolate it.
                    self.session.add(row)
                    await self.session.flush()
                return row
            except IntegrityError:
                # 嵌套事务自动回滚;外层 confirm 事务的所有写入仍保留。
                return None
        # 测试环境 fallback:直接 flush,IntegrityError 让调用方决定如何处理
        try:
            self.session.add(row)
            await self.session.flush()
            return row
        except IntegrityError:
            return None

    # ── 原子领取 ──────────────────────────────────────────────

    async def claim_next(
        self,
        *,
        lease_owner: str,
        lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        available_at: datetime | None = None,
    ) -> AgentExecutionRequest | None:
        """Worker 拉 next 任务。

        行为:
          * ``SELECT ... FOR UPDATE SKIP LOCKED`` 锁定下一个
            ``status=queued AND available_at <= NOW()`` 且 lease 未占用的行;
          * 同时 ``UPDATE status=running, lease_owner=:owner, lease_expires_at=NOW()+TTL``;
          * 返回 ORM 对象(或 None 表示空队列)。

        MySQL 8 / PostgreSQL 9.5+ 都支持 SKIP LOCKED。
        """
        now = available_at or utcnow()
        lease_until = now + timedelta(seconds=lease_ttl_seconds)
        # 走 raw SQL 拿锁定行 + UPDATE ——
        # SKIP LOCKED 不被 SQLAlchemy ORM 抽象,所以走 textual
        # dialect 探测是 best-effort;测试用 FakeSession 时可能没有 bind
        try:
            dialect = (
                self.session.bind.dialect.name
                if getattr(self.session, "bind", None) is not None
                else "default"
            )
        except Exception:
            dialect = "default"

        # 走 raw SQL 拿锁定行 + UPDATE ——
        # SKIP LOCKED 不被 SQLAlchemy ORM 抽象,所以走 textual
        if dialect == "mysql":
            sql = text(
                "SELECT id FROM agent_execution_requests "
                "WHERE status = 'queued' AND available_at <= :now "
                "ORDER BY available_at ASC "
                "LIMIT 1 FOR UPDATE SKIP LOCKED"
            )
        else:
            sql = text(
                "SELECT id FROM agent_execution_requests "
                "WHERE status = 'queued' AND available_at <= :now "
                "ORDER BY available_at ASC "
                "LIMIT 1 FOR UPDATE SKIP LOCKED"
            )

        result = await self.session.execute(sql, {"now": now})
        row = result.first()
        if row is None:
            return None

        # SELECT 后立即 UPDATE ——
        target_id = row[0] if isinstance(row, tuple) else row[0]  # SA Row / tuple 兼容
        # 一些 AsyncDB 驱动返回 dict-like Row(row[0] 是 dict key);安全 fallback
        if hasattr(row, "_mapping"):
            try:
                target_id = row._mapping["id"]
            except (KeyError, TypeError):
                target_id = row[0]
        upd = await self.session.execute(
            update(AgentExecutionRequest)
            .where(AgentExecutionRequest.id == target_id)
            .where(AgentExecutionRequest.status == "queued")
            .values(
                status="running",
                lease_owner=lease_owner,
                lease_expires_at=lease_until,
                attempt_count=AgentExecutionRequest.attempt_count + 1,
                started_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if upd.rowcount == 0:
            # 被另一进程抢走
            return None

        # 重新 SELECT 拿到完整对象
        refetch = await self.session.execute(
            select(AgentExecutionRequest).where(AgentExecutionRequest.id == target_id)
        )
        return refetch.scalar_one_or_none()

    # ── 状态转换 ──────────────────────────────────────────────

    async def mark_completed(
        self,
        *,
        execution_id: int,
        finished_at: datetime | None = None,
    ) -> int:
        """完成后写终态。返回影响行数。"""
        now = finished_at or utcnow()
        upd = await self.session.execute(
            update(AgentExecutionRequest)
            .where(AgentExecutionRequest.id == execution_id)
            .values(
                status="completed",
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
            )
        )
        return upd.rowcount or 0

    async def mark_failed(
        self,
        *,
        execution_id: int,
        error_code: str,
        error_message: str,
        finished_at: datetime | None = None,
    ) -> int:
        """失败后写终态(由调用方决定是否重试)。"""
        now = finished_at or utcnow()
        upd = await self.session.execute(
            update(AgentExecutionRequest)
            .where(AgentExecutionRequest.id == execution_id)
            .values(
                status="failed",
                finished_at=now,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=error_code[:64],
                last_error_message=error_message[:4000],
            )
        )
        return upd.rowcount or 0

    async def release_lease_for_retry(
        self,
        *,
        execution_id: int,
        retry_in_seconds: int = 5,
    ) -> int:
        """Worker 主动放弃 lease:把 status 回到 queued,available_at 设为将来时间。

        用于:临时错误重试,远未到 lease 过期。
        """
        next_available = utcnow() + timedelta(seconds=retry_in_seconds)
        upd = await self.session.execute(
            update(AgentExecutionRequest)
            .where(AgentExecutionRequest.id == execution_id)
            .values(
                status="queued",
                lease_owner=None,
                lease_expires_at=None,
                available_at=next_available,
            )
        )
        return upd.rowcount or 0

    async def reap_expired_leases(
        self,
        *,
        now: datetime | None = None,
    ) -> int:
        """回收过期 lease:把 ``lease_expires_at < NOW() AND status='running'`` 的行重新设为 queued。

        Returns:
            回收行数。
        """
        _now = now or utcnow()
        upd = await self.session.execute(
            update(AgentExecutionRequest)
            .where(AgentExecutionRequest.status == "running")
            .where(AgentExecutionRequest.lease_expires_at.is_not(None))
            .where(AgentExecutionRequest.lease_expires_at < _now)
            .values(
                status="queued",
                lease_owner=None,
                lease_expires_at=None,
            )
        )
        return upd.rowcount or 0

    # ── 查询 / 工具方法 ──────────────────────────────────────

    async def get_by_idempotency_key(
        self,
        idempotency_key: str,
    ) -> AgentExecutionRequest | None:
        result = await self.session.execute(
            select(AgentExecutionRequest).where(
                AgentExecutionRequest.idempotency_key == idempotency_key
            )
        )
        return result.scalar_one_or_none()

    async def list_by_task(
        self,
        task_internal_id: int,
        limit: int = 50,
    ) -> list[AgentExecutionRequest]:
        result = await self.session.execute(
            select(AgentExecutionRequest)
            .where(AgentExecutionRequest.task_id == task_internal_id)
            .order_by(AgentExecutionRequest.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


def _default_idem_key(task_public_id: str, request_type: str) -> str:
    return _default_idempotency_key(task_public_id, request_type, suffix="")


__all__ = [
    "AgentExecutionRequestRepository",
    "DEFAULT_LEASE_TTL_SECONDS",
]


# 模块定位:AgentExecutionRequest 仓储(Outbox 队列,Phase 2.8R-B)
#
# 链路:
#   AgentTaskService.create_task → 写 status='queued' 行
#   AgentExecutionWorker.cycle →
#     SELECT FOR UPDATE SKIP LOCKED → claim_next → mark_running →
#     ApiDispatcher.dispatch_* → mark_completed / mark_failed
#
# 关键约束:
#   - SELECT ... FOR UPDATE SKIP LOCKED 是关键;
#     没有的话多 worker 会重复跑同一行;
#   - lease_owner (本机 hostname) 必传;
#   - 重试 / 超时由 release_lease_for_retry() 处理(lease 过期);
#   - 直接 ORM 改 status 是反模式(走本仓原子函数)。
