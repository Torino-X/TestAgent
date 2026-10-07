"""Phase 2.9A.27 真实数据库审计 — 只读 SELECT,绝不修改。

目的:
  1. 确认 task_699f34ea 真实事件数 vs event-list API 截断后的 50 条
  2. 确认 DocxFormatCheckTool / finalize / task_completed 是否落库
  3. 确认 trigger_message_id 当前值
  4. 检查 sequence_no>50 的事件是否真存在

使用方法:
  cd backend
  python scripts/audit_phase_2_9a_27.py
"""

from __future__ import annotations

import io
import sys
import os

# Windows GBK stdout 不支持 emoji; 强制 UTF-8
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text

from app.db.session import sync_engine
from app.core.config import get_settings

settings = get_settings()
TASK_PUBLIC_ID = "task_699f34ea"
CONV_PUBLIC_ID = "conv_fd4ce5e6"


def run() -> None:
    print("=" * 70)
    print("Phase 2.9A.27 只读审计 — task_699f34ea")
    print("=" * 70)

    with sync_engine.connect() as conn:
        # 1) Task 基础信息 + trigger_message_id
        print("\n[1] Task 基础信息 + trigger_message_id")
        rows = conn.execute(
            text(
                "SELECT id, public_id, conversation_id, status, runtime_status, "
                "started_at, completed_at, "
                "TIMESTAMPDIFF(MICROSECOND, started_at, completed_at) / 1000 AS duration_ms, "
                "trigger_message_id, "
                "deleted_at "
                "FROM agent_tasks WHERE public_id = :pid AND deleted_at IS NULL"
            ),
            {"pid": TASK_PUBLIC_ID},
        ).fetchall()
        if not rows:
            print(f"  ❌ 任务 {TASK_PUBLIC_ID} 不存在或已删除")
            return
        cols = ["id", "public_id", "conversation_id", "status",
                "runtime_status", "started_at", "completed_at",
                "duration_ms", "trigger_message_id", "deleted_at"]
        for r in rows:
            for c, v in zip(cols, r):
                print(f"    {c}: {v}")

        # 2) 真实事件总数 + 区间
        print("\n[2] agent_events 真实统计")
        total = conn.execute(
            text(
                "SELECT COUNT(*) FROM agent_events e "
                "JOIN agent_tasks t ON t.id = e.task_id "
                "WHERE t.public_id = :pid "
            ),
            {"pid": TASK_PUBLIC_ID},
        ).scalar()
        print(f"  真实事件总数 = {total}")

        max_seq = conn.execute(
            text(
                "SELECT MAX(sequence_no) FROM agent_events e "
                "JOIN agent_tasks t ON t.id = e.task_id "
                "WHERE t.public_id = :pid "
            ),
            {"pid": TASK_PUBLIC_ID},
        ).scalar()
        print(f"  最大 sequence_no = {max_seq}")

        max_id = conn.execute(
            text(
                "SELECT MAX(e.id) FROM agent_events e "
                "JOIN agent_tasks t ON t.id = e.task_id "
                "WHERE t.public_id = :pid "
            ),
            {"pid": TASK_PUBLIC_ID},
        ).scalar()
        print(f"  最大内部 id = {max_id}")

        # 3) 关键事件是否落库
        print("\n[3] 关键事件存在性检查")
        critical_events = [
            "task_completed",
            "task_failed",
            "task_cancelled",
            "docx_format_checked",
            "tool_started",
            "tool_finished",
            "tool_failed",
            "word_export_finished",
            "word_export_completed",
            "finalize_task",
            "finalize_completed",
            "artifact_created",
            "review_completed",
        ]
        for ev in critical_events:
            count = conn.execute(
                text(
                    "SELECT COUNT(*) FROM agent_events e "
                    "JOIN agent_tasks t ON t.id = e.task_id "
                    "WHERE t.public_id = :pid AND e.event_type = :ev"
                ),
                {"pid": TASK_PUBLIC_ID, "ev": ev},
            ).scalar()
            marker = "✅" if count > 0 else "❌"
            print(f"  {marker} {ev}: {count} 条")

        # 4) 每个 event_type 的统计
        print("\n[4] 每个 event_type 的事件数")
        et_rows = conn.execute(
            text(
                "SELECT e.event_type, COUNT(*) AS cnt, "
                "       MIN(e.sequence_no) AS min_seq, MAX(e.sequence_no) AS max_seq "
                "FROM agent_events e "
                "JOIN agent_tasks t ON t.id = e.task_id "
                "WHERE t.public_id = :pid "
                "GROUP BY e.event_type "
                "ORDER BY min_seq IS NULL, min_seq ASC"
            ),
            {"pid": TASK_PUBLIC_ID},
        ).fetchall()
        for r in et_rows:
            et, cnt, mn, mx = r
            print(f"  {et:<35} cnt={cnt:<4} min_seq={mn} max_seq={mx}")

        # 5) sequence_no>50 是否存在(验证 A 情况)
        print("\n[5] sequence_no > 50 的事件(API 截断点)")
        gt50 = conn.execute(
            text(
                "SELECT e.sequence_no, e.event_type, e.title, e.payload_json "
                "FROM agent_events e "
                "JOIN agent_tasks t ON t.id = e.task_id "
                "WHERE t.public_id = :pid AND e.sequence_no > 50 "
                "ORDER BY e.sequence_no ASC"
            ),
            {"pid": TASK_PUBLIC_ID},
        ).fetchall()
        print(f"  sequence_no > 50 共 {len(gt50)} 条:")
        for r in gt50[:30]:
            seq, et, title, payload = r
            print(f"    seq={seq} type={et} title={title}")
            if payload and len(payload) > 200:
                print(f"      payload[:200] = {payload[:200]}...")

        # 6) task_completed payload 是否带 summary_facts
        print("\n[6] task_completed 事件 payload")
        tc_rows = conn.execute(
            text(
                "SELECT e.sequence_no, e.payload_json, e.title, e.content "
                "FROM agent_events e "
                "JOIN agent_tasks t ON t.id = e.task_id "
                "WHERE t.public_id = :pid AND e.event_type = 'task_completed' "
                "ORDER BY e.sequence_no ASC"
            ),
            {"pid": TASK_PUBLIC_ID},
        ).fetchall()
        if not tc_rows:
            print("  ❌ 没有 task_completed 事件!")
        for r in tc_rows:
            seq, payload, title, content = r
            print(f"  seq={seq} title={title}")
            print(f"  payload[:600] = {(payload or '')[:600]}")
            print(f"  content[:200] = {(content or '')[:200]}")

        # 7) DocxFormatCheckTool 的事件
        print("\n[7] docx_format_checked 事件")
        df_rows = conn.execute(
            text(
                "SELECT e.sequence_no, e.payload_json, e.title "
                "FROM agent_events e "
                "JOIN agent_tasks t ON t.id = e.task_id "
                "WHERE t.public_id = :pid "
                "  AND (e.event_type = 'docx_format_checked' "
                "       OR e.title LIKE '%format%' "
                "       OR e.title LIKE '%Format%') "
                "ORDER BY e.sequence_no ASC"
            ),
            {"pid": TASK_PUBLIC_ID},
        ).fetchall()
        if not df_rows:
            print("  ❌ 没有格式检查相关事件")
        for r in df_rows:
            seq, payload, title = r
            print(f"  seq={seq} title={title}")
            print(f"  payload[:300] = {(payload or '')[:300]}")

        # 8) RequirementParserTool 的 tool_started + tool_finished
        print("\n[8] RequirementParserTool 事件")
        rp_rows = conn.execute(
            text(
                "SELECT e.sequence_no, e.event_type, e.payload_json, e.title "
                "FROM agent_events e "
                "JOIN agent_tasks t ON t.id = e.task_id "
                "WHERE t.public_id = :pid "
                "  AND (e.title LIKE '%RequirementParser%' "
                "       OR JSON_EXTRACT(e.payload_json, '$.tool_name') = 'RequirementParserTool') "
                "ORDER BY e.sequence_no ASC"
            ),
            {"pid": TASK_PUBLIC_ID},
        ).fetchall()
        if not rp_rows:
            print("  ❌ 没有 RequirementParserTool 事件")
        for r in rp_rows:
            seq, et, payload, title = r
            print(f"  seq={seq} type={et} title={title}")
            print(f"  payload[:400] = {(payload or '')[:400]}")

        # 9) Conversation tasks 列表
        print("\n[9] conversation.tasks 列表")
        ts_rows = conn.execute(
            text(
                "SELECT id, public_id, status, runtime_status, "
                "       started_at, completed_at, trigger_message_id "
                "FROM agent_tasks "
                "WHERE conversation_id = ("
                "  SELECT id FROM conversations WHERE public_id = :cid"
                ") AND deleted_at IS NULL "
                "ORDER BY created_at ASC"
            ),
            {"cid": CONV_PUBLIC_ID},
        ).fetchall()
        for r in ts_rows:
            (tid, pid, st, rs, sa, ca, tmid) = r
            print(f"  id={tid} public_id={pid} status={st} runtime={rs} "
                  f"trigger_message_id={tmid}")

        # 10) Conversation messages 列表(看 user message 是否存在 + reply_to_message_id)
        print("\n[10] conversation messages (user + agent text)")
        msg_rows = conn.execute(
            text(
                "SELECT m.id, m.public_id, m.role, m.message_type, "
                "       m.conversation_sequence, m.reply_to_message_id, "
                "       m.created_at "
                "FROM messages m "
                "JOIN conversations c ON c.id = m.conversation_id "
                "WHERE c.public_id = :cid "
                "ORDER BY m.conversation_sequence IS NULL, m.conversation_sequence ASC, m.id ASC"
            ),
            {"cid": CONV_PUBLIC_ID},
        ).fetchall()
        for r in msg_rows:
            (mid, mpid, role, mt, cseq, rti, cat) = r
            print(f"  id={mid} seq={cseq} role={role} type={mt} "
                  f"reply_to={rti} created_at={cat}")

        print("\n" + "=" * 70)
        print("审计完成 — 仅 SELECT,未修改任何数据")
        print("=" * 70)


if __name__ == "__main__":
    run()