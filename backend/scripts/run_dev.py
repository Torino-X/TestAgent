"""Windows-safe development server launcher.

Psycopg's async connection cannot run on Windows' Proactor event loop.  This
module sets the Selector policy before Uvicorn creates its serving loop, which
cannot be achieved from inside ``app.main`` when using the Uvicorn CLI.

Usage:

    python scripts/run_dev.py --host 127.0.0.1 --port 8003
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Sequence


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def _ensure_selector_loop_policy() -> None:
    """Set the only Windows event-loop policy supported by async Psycopg."""
    if sys.platform != "win32":
        return

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    print(
        "[run_dev] WindowsSelectorEventLoopPolicy configured before Uvicorn startup",
        flush=True,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start TestAgent with a Windows-safe asyncio event loop."
    )
    parser.add_argument("--host", default=os.environ.get("APP_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("APP_PORT", "8003"))
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("APP_LOG_LEVEL", "info")
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable Uvicorn reload on non-Windows hosts.",
    )
    args = parser.parse_args(argv)
    if sys.platform == "win32" and args.reload:
        parser.error(
            "--reload is not supported on Windows with async Psycopg; "
            "restart this launcher manually after backend changes."
        )
    return args


def main(argv: Sequence[str] | None = None) -> None:
    _ensure_selector_loop_policy()
    args = _parse_args(argv)
    loop = (
        "app.uvicorn_loop:selector_loop_factory"
        if sys.platform == "win32"
        else "auto"
    )

    try:
        import uvicorn
    except ImportError:
        sys.stderr.write("[run_dev] uvicorn is not installed; run `pip install uvicorn[standard]`.\n")
        raise SystemExit(1) from None

    print(
        "[run_dev] starting app.main:app "
        f"host={args.host} port={args.port} reload={args.reload} "
        f"log_level={args.log_level} loop={loop}",
        flush=True,
    )
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        loop=loop,
    )


if __name__ == "__main__":
    main()
