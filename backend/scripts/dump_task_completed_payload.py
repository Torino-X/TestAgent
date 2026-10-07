"""Dump full task_completed payload."""

from __future__ import annotations

import io
import sys
import os

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import json
from sqlalchemy import text

from app.db.session import sync_engine

with sync_engine.connect() as conn:
    rows = conn.execute(
        text(
            "SELECT e.sequence_no, e.payload_json "
            "FROM agent_events e "
            "JOIN agent_tasks t ON t.id = e.task_id "
            "WHERE t.public_id = :pid AND e.event_type = 'task_completed'"
        ),
        {"pid": "task_699f34ea"},
    ).fetchall()
    for r in rows:
        seq, payload = r
        print(f"=== seq={seq} ===")
        data = json.loads(payload) if payload else {}
        print(json.dumps(data, ensure_ascii=False, indent=2))