"""Phase 2.7 — metrics_cli。

一次性跑 ``MetricsComparator.compare()`` 并落 JSON + markdown。
"""

from __future__ import annotations

import argparse
import asyncio
import json as _json
import logging
import sys
from typing import Any, AsyncIterator, Optional


def _build_session_factory() -> Any:
    try:
        from app.db.session import async_session_factory as real_factory

        return real_factory
    except Exception:
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_factory() -> AsyncIterator[Any]:
            class _S:
                async def execute(self, *a: object, **kw: object) -> Any:
                    class _R:
                        def fetchall(self) -> list:
                            return []

                    return _R()

            yield _S()

        return fake_factory


async def _run_once(args: argparse.Namespace) -> int:
    from app.agent_runtime.canary.metrics_comparator import MetricsComparator

    factory = _build_session_factory()
    comparator = MetricsComparator(session_factory=factory)
    payload = await comparator.compare(window_minutes=args.window)

    if args.output:
        path = await comparator.save_artifact(output_dir=args.output, payload=payload)
        print(f"[metrics_cli] saved: {path}", file=sys.stderr)

    print(_json.dumps(payload, default=str, ensure_ascii=False, indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.agent_runtime.canary.metrics_cli"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_once = sub.add_parser("run-once", help="run metrics compare once")
    p_once.add_argument("--window", type=int, default=60, help="window in minutes")
    p_once.add_argument(
        "--output",
        default="",
        help="optional directory to write canary_metrics_<ts>.json + .md",
    )
    p_once.set_defaults(handler=_run_once)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO)
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(args.handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
