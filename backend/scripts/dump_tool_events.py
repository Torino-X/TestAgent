"""Dump RequirementParserTool full events."""

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
            "SELECT e.sequence_no, e.event_type, e.payload_json "
            "FROM agent_events e "
            "JOIN agent_tasks t ON t.id = e.task_id "
            "WHERE t.public_id = :pid "
            "  AND (e.event_type LIKE 'tool_%' "
            "       OR JSON_EXTRACT(e.payload_json, '$.tool_name') = 'RequirementParserTool') "
            "ORDER BY e.sequence_no ASC"
        ),
        {"pid": "task_699f34ea"},
    ).fetchall()
    for r in rows:
        seq, et, payload = r
        print(f"=== seq={seq} type={et} ===")
        data = json.loads(payload) if payload else {}
        # 显示关键字段
        for key in ['tool_name', 'tool_call_id', 'dedupe_key', 'chunk_index',
                    'chunk_final', 'chunk_total', 'attempt', 'publicUpdate']:
            if key in data:
                v = data[key]
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, ensure_ascii=False)[:200]
                print(f"  {key}: {v}")