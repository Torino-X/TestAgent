"""event_retention_cli — 管理命令镜像 Phase 2.2 confirmation_timeout_cli.

用法:
  python -m app.agent_runtime.events.event_retention_cli run-once
  python -m app.agent_runtime.events.event_retention_cli run-loop --interval 3600
  python -m app.agent_runtime.events.event_retention_cli inspect

Env:
  AGENT_RUNTIME_EVENT_RETENTION_INTERVAL (run-loop default = 3600)
  AGENT_RUNTIME_EVENT_RETENTION_KEEP_DAYS (default = 30)
  AGENT_RUNTIME_EVENT_RETENTION_KEEP_PER_TASK (default = 5000)
"""

from __future__ import annotations

import argparse
import asyncio
import json as _json
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("event_retention_cli")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="event_retention_cli",
        description="Phase 2.6 agent_events retention policy runner",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run_once = sub.add_parser("run-once", help="run a single cleanup pass")
    p_run_once.add_argument(
        "--keep-days",
        type=int,
        default=int(os.environ.get("AGENT_RUNTIME_EVENT_RETENTION_KEEP_DAYS", "30")),
    )
    p_run_once.add_argument(
        "--keep-per-task",
        type=int,
        default=int(
            os.environ.get("AGENT_RUNTIME_EVENT_RETENTION_KEEP_PER_TASK", "5000")
        ),
    )

    p_loop = sub.add_parser("run-loop", help="run cleanup every N seconds")
    p_loop.add_argument(
        "--interval",
        type=int,
        default=int(
            os.environ.get(
                "AGENT_RUNTIME_EVENT_RETENTION_INTERVAL", str(60 * 60)
            )
        ),
    )

    p_inspect = sub.add_parser(
        "inspect", help="dump current event counts without deleting"
    )

    return parser.parse_args(argv)


async def _inspect() -> int:
    from app.db.session import AsyncSessionLocal

    from .event_retention import EventRetentionConfig, EventRetentionPolicy

    cfg = EventRetentionConfig()
    async with AsyncSessionLocal() as session:
        from sqlalchemy import text

        row = await session.execute(
            text("SELECT COUNT(*) FROM agent_events")
        )
        total = int(row.scalar() or 0)
        row2 = await session.execute(
            text(
                "SELECT MIN(created_at), MAX(created_at) "
                "FROM agent_events"
            )
        )
        ts = row2.first()

    out = {
        "policy": cfg.to_dict(),
        "total_events": total,
        "min_created_at": str(ts[0]) if ts and ts[0] else None,
        "max_created_at": str(ts[1]) if ts and ts[1] else None,
    }
    print(_json.dumps(out, ensure_ascii=False, indent=2))
    return 0


async def _run_once(keep_days: int, keep_per_task: int) -> int:
    from app.db.session import AsyncSessionLocal

    from .event_retention import EventRetentionConfig, EventRetentionPolicy

    cfg = EventRetentionConfig(
        keep_last_days=int(keep_days),
        keep_last_per_task=int(keep_per_task),
    )
    policy = EventRetentionPolicy(session_factory=AsyncSessionLocal, config=cfg)
    summary = await policy.cleanup_once()
    print(_json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


async def _run_loop(interval: int) -> int:
    from app.db.session import AsyncSessionLocal

    from .event_retention import EventRetentionPolicy

    policy = EventRetentionPolicy(session_factory=AsyncSessionLocal)
    interval = max(10, int(interval))
    while True:
        try:
            summary = await policy.cleanup_once()
            logger.info(
                "event_retention_cli: pass complete | deleted=%d | age=%d | per_task=%d",
                summary["total_deleted"],
                summary["deleted_by_age"],
                summary["deleted_by_per_task"],
            )
        except Exception:
            logger.exception("event_retention_cli: pass failed (continuing)")
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            break
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    args = _parse_args(argv)
    if args.cmd == "run-once":
        return asyncio.run(_run_once(args.keep_days, args.keep_per_task))
    if args.cmd == "inspect":
        return asyncio.run(_inspect())
    if args.cmd == "run-loop":
        return asyncio.run(_run_loop(args.interval))
    return 1


if __name__ == "__main__":
    sys.exit(main())
