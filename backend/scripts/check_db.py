"""Quick database connectivity check script.

Usage:
    cd backend
    python scripts/check_db.py
"""

from __future__ import annotations

import sys, os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text
from app.db.session import sync_engine
from app.core.config import get_settings

settings = get_settings()


def check() -> None:
    print(f"Sync URL  : {settings.safe_database_url}")
    print(f"Async URL : {settings.async_database_url.replace(settings.database_password, '******')}")
    try:
        with sync_engine.connect() as conn:
            result = conn.execute(text("SELECT 1 AS ok"))
            row = result.fetchone()
            print(f'{{"database": "connected", "ok": {row[0]}}}')
    except Exception as exc:
        print(f'{{"database": "disconnected", "error": "{exc}"}}')
        sys.exit(1)


if __name__ == "__main__":
    check()
