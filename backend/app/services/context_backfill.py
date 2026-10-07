"""Context Engine Backfill — Conversation/AgentTask Workspace、Snapshot 状态、Summary Schema。

设计文档 §32 CE-005：
- Conversation Workspace Key（默认 conversation:{conversation.public_id}，不得从文件名推断）
- AgentTask Workspace Key（复制其 Conversation Workspace）
- 旧 Snapshot 未知 call_site 保持 NULL / legacy 标记（不伪造）
- 旧 Summary Schema Version 默认 'v1'

执行约束：
- dry-run / batch / idempotent / resumable / safe log
- 不在单个长事务锁全表（分批提交）
- 可独立执行（CLI 脚本）
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("context_engine.backfill")

_DEFAULT_BATCH_SIZE = 500


@dataclass
class BackfillResult:
    conversation_updated: int = 0
    task_updated: int = 0
    snapshot_marked: int = 0
    summary_marked: int = 0
    skipped_conversation_null_pk: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "BackfillResult") -> None:
        self.conversation_updated += other.conversation_updated
        self.task_updated += other.task_updated
        self.snapshot_marked += other.snapshot_marked
        self.summary_marked += other.summary_marked
        self.skipped_conversation_null_pk += other.skipped_conversation_null_pk
        self.errors.extend(other.errors)


class ContextBackfill:
    """幂等、可恢复、分批的 Context 字段回填。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], batch_size: int = _DEFAULT_BATCH_SIZE) -> None:
        self._session_factory = session_factory
        self._batch_size = batch_size

    # ── Conversation Workspace ──────────────────────────────────────

    async def backfill_conversation_workspace(self, *, dry_run: bool = False) -> BackfillResult:
        """为 context_workspace_key IS NULL 的会话回填 conversation:{public_id}。

        使用 keyset 分页（WHERE id > :last_id）而非 OFFSET：
        回填会就地更新行，OFFSET 在批间过滤集收缩时会跳过行。
        """
        result = BackfillResult()
        last_id = 0
        while True:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id, public_id FROM conversations "
                            "WHERE context_workspace_key IS NULL AND deleted_at IS NULL "
                            "  AND id > :last_id "
                            "ORDER BY id ASC LIMIT :limit"
                        ),
                        {"limit": self._batch_size, "last_id": last_id},
                    )
                ).all()
                if not rows:
                    break
                if not dry_run:
                    for row_id, public_id in rows:
                        await session.execute(
                            text(
                                "UPDATE conversations SET context_workspace_key = :wk "
                                "WHERE id = :id AND context_workspace_key IS NULL"
                            ),
                            {"wk": f"conversation:{public_id}", "id": row_id},
                        )
                    await session.commit()
                result.conversation_updated += len(rows)
                last_id = rows[-1][0]
                logger.info(
                    "conversation workspace backfill | dry_run=%s | batch=%d | updated=%d",
                    dry_run, len(rows), result.conversation_updated,
                )
        return result

    # ── AgentTask Workspace ─────────────────────────────────────────

    async def backfill_task_workspace(self, *, dry_run: bool = False) -> BackfillResult:
        """为 context_workspace_key IS NULL 的 Task 回填其 Conversation Workspace。

        Task 创建时冻结 Conversation 的 Workspace；历史 Task 用会话当前值回填，
        不因会话改绑而改变。keyset 分页避免 OFFSET 就地更新跳行。
        """
        result = BackfillResult()
        last_id = 0
        while True:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT t.id, c.public_id FROM agent_tasks t "
                            "JOIN conversations c ON c.id = t.conversation_id "
                            "WHERE t.context_workspace_key IS NULL AND t.deleted_at IS NULL "
                            "  AND t.id > :last_id "
                            "ORDER BY t.id ASC LIMIT :limit"
                        ),
                        {"limit": self._batch_size, "last_id": last_id},
                    )
                ).all()
                if not rows:
                    break
                if not dry_run:
                    for task_id, conv_public_id in rows:
                        await session.execute(
                            text(
                                "UPDATE agent_tasks SET context_workspace_key = :wk "
                                "WHERE id = :id AND context_workspace_key IS NULL"
                            ),
                            {"wk": f"conversation:{conv_public_id}", "id": task_id},
                        )
                    await session.commit()
                result.task_updated += len(rows)
                last_id = rows[-1][0]
        return result

    # ── Snapshot 状态 ───────────────────────────────────────────────

    async def backfill_snapshot_status(self, *, dry_run: bool = False) -> BackfillResult:
        """旧 Snapshot status IS NULL → 'building'；call_site 保持 NULL（不伪造）。"""
        result = BackfillResult()
        last_id = 0
        while True:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id FROM llm_context_snapshots "
                            "WHERE status IS NULL AND id > :last_id "
                            "ORDER BY id ASC LIMIT :limit"
                        ),
                        {"limit": self._batch_size, "last_id": last_id},
                    )
                ).all()
                if not rows:
                    break
                if not dry_run:
                    for (row_id,) in rows:
                        await session.execute(
                            text(
                                "UPDATE llm_context_snapshots SET status = 'building' "
                                "WHERE id = :id AND status IS NULL"
                            ),
                            {"id": row_id},
                        )
                    await session.commit()
                result.snapshot_marked += len(rows)
                last_id = rows[-1][0]
        return result

    # ── Summary Schema Version ──────────────────────────────────────

    async def backfill_summary_schema_version(self, *, dry_run: bool = False) -> BackfillResult:
        """旧 Summary schema_version 缺失 → 'v1'；summary_type 缺失 → 'conversation'。"""
        result = BackfillResult()
        last_id = 0
        while True:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT id FROM conversation_summaries "
                            "WHERE (schema_version IS NULL OR summary_type IS NULL) "
                            "  AND id > :last_id "
                            "ORDER BY id ASC LIMIT :limit"
                        ),
                        {"limit": self._batch_size, "last_id": last_id},
                    )
                ).all()
                if not rows:
                    break
                if not dry_run:
                    for (row_id,) in rows:
                        await session.execute(
                            text(
                                "UPDATE conversation_summaries SET "
                                "schema_version = COALESCE(schema_version, 'v1'), "
                                "summary_type = COALESCE(summary_type, 'conversation') "
                                "WHERE id = :id"
                            ),
                            {"id": row_id},
                        )
                    await session.commit()
                result.summary_marked += len(rows)
                last_id = rows[-1][0]
        return result

    # ── 汇总 ────────────────────────────────────────────────────────

    async def run_all(self, *, dry_run: bool = False) -> BackfillResult:
        result = BackfillResult()
        for fn in (
            self.backfill_conversation_workspace,
            self.backfill_task_workspace,
            self.backfill_snapshot_status,
            self.backfill_summary_schema_version,
        ):
            try:
                result.merge(await fn(dry_run=dry_run))
            except Exception as exc:  # noqa: BLE001
                logger.error("backfill step failed: %s", exc)
                result.errors.append(f"{fn.__name__}: {exc}")
        return result


def _build_session_factory() -> async_sessionmaker[AsyncSession]:
    """从配置构建 session factory（DATABASE_SYNC_URL → async URL）。"""
    from app.core.config import get_settings
    from app.db.session import engine

    settings = get_settings()
    _ = settings  # ensure config loads
    from sqlalchemy.ext.asyncio import create_async_engine

    async_engine = create_async_engine(settings.async_database_url)
    return async_sessionmaker(async_engine, expire_on_commit=False)


async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Context Engine Backfill")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--step", choices=["all", "conversation", "task", "snapshot", "summary"], default="all")
    parser.add_argument("--batch-size", type=int, default=_DEFAULT_BATCH_SIZE)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    factory = _build_session_factory()
    backfill = ContextBackfill(factory, batch_size=args.batch_size)

    steps = {
        "conversation": backfill.backfill_conversation_workspace,
        "task": backfill.backfill_task_workspace,
        "snapshot": backfill.backfill_snapshot_status,
        "summary": backfill.backfill_summary_schema_version,
    }
    if args.step == "all":
        result = await backfill.run_all(dry_run=args.dry_run)
    else:
        result = await steps[args.step](dry_run=args.dry_run)

    print(f"dry_run={args.dry_run}")
    print(f"conversation_updated={result.conversation_updated}")
    print(f"task_updated={result.task_updated}")
    print(f"snapshot_marked={result.snapshot_marked}")
    print(f"summary_marked={result.summary_marked}")
    print(f"errors={len(result.errors)}")
    for err in result.errors:
        print(f"  ERROR: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
