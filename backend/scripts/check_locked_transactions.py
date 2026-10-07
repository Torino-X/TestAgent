"""Detect and (optionally) kill long-running idle MySQL transactions.

SSE/streaming endpoints that get killed mid-flight (server restart, force
disconnect, OOM) sometimes leave behind a transaction holding row locks.
This script lists them and optionally kills them.

Usage:
    python scripts/check_locked_transactions.py            # list only
    python scripts/check_locked_transactions.py --kill     # list + kill
    python scripts/check_locked_transactions.py --age 600  # threshold seconds (default 300)
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as `python scripts/check_locked_transactions.py`
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import text  # noqa: E402

from app.db.session import AsyncSessionLocal  # noqa: E402


async def list_idle_txns(age_seconds: int) -> list[tuple]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                """
                SELECT
                    trx_id,
                    trx_mysql_thread_id,
                    trx_state,
                    TIMESTAMPDIFF(SECOND, trx_started, NOW()) AS age_secs,
                    trx_rows_locked,
                    trx_query
                FROM information_schema.innodb_trx
                WHERE trx_state = 'RUNNING'
                  AND TIMESTAMPDIFF(SECOND, trx_started, NOW()) > :age
                ORDER BY trx_started
                """
            ),
            {"age": age_seconds},
        )
        return list(result.fetchall())


async def kill_thread(thread_id: int) -> str:
    async with AsyncSessionLocal() as session:
        try:
            await session.execute(text(f"KILL {thread_id}"))
            await session.commit()
            return "killed"
        except Exception as exc:  # noqa: BLE001
            return f"error: {exc}"


async def main(age: int, do_kill: bool) -> int:
    rows = await list_idle_txns(age)
    if not rows:
        print(f"No idle transactions older than {age}s. OK.")
        return 0

    print(f"Found {len(rows)} idle transaction(s) older than {age}s:")
    for r in rows:
        trx_id, thread_id, state, age_secs, rows_locked, query = r
        print(
            f"  trx_id={trx_id} thread_id={thread_id} state={state} "
            f"age={age_secs}s rows_locked={rows_locked}"
        )
        print(f"    last_query={(query or '<idle>')[:200]!r}")

    if not do_kill:
        print("\nDry-run. Pass --kill to terminate them.")
        return 1

    print("\nKilling leaked transaction(s)...")
    for r in rows:
        thread_id = r[1]
        result = await kill_thread(thread_id)
        print(f"  KILL {thread_id}: {result}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--age",
        type=int,
        default=300,
        help="Threshold in seconds; default 300 (5 min)",
    )
    parser.add_argument(
        "--kill",
        action="store_true",
        help="Actually KILL the leaked transactions",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.age, args.kill)))