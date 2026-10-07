"""Phase 2.9A.29 审计 — 只读查询 conv_6a59fa05 / task_01949301 真实状态。"""

from __future__ import annotations
import io, sys, json, os
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import text
from app.db.session import sync_engine

CONV = "conv_6a59fa05"
TASK = "task_01949301"

with sync_engine.connect() as conn:
    # 1. Conversation
    print("=== CONVERSATION ===")
    r = conn.execute(text("SELECT id, public_id, user_id, title, status, created_at, updated_at FROM conversations WHERE public_id=:p"), {"p": CONV}).fetchone()
    if r:
        cols = ["id","public_id","user_id","title","status","created_at","updated_at"]
        for c,v in zip(cols, r): print(f"  {c}: {v}")
        conv_internal_id = r[0]
    else:
        print("  NOT FOUND"); sys.exit()

    # 2. Messages
    print("\n=== MESSAGES ===")
    rows = conn.execute(text(
        "SELECT id, public_id, role, message_type, content, conversation_sequence, "
        "reply_to_message_id, payload_json, created_at "
        "FROM messages WHERE conversation_id=:cid AND deleted_at IS NULL "
        "ORDER BY conversation_sequence IS NULL ASC, conversation_sequence ASC, id ASC"
    ), {"cid": conv_internal_id}).fetchall()
    print(f"  total: {len(rows)}")
    for r in rows:
        mid, mpid, role, mt, content, seq, rti, payload, cat = r
        # parse payload
        pstr = ""
        if payload:
            try:
                pd = json.loads(payload) if isinstance(payload, str) else payload
                if isinstance(pd, dict):
                    # show attached_file_ids if present
                    afi = pd.get("attached_file_ids")
                    pstr = f"payload_keys={list(pd.keys())[:6]}"
                    if afi: pstr += f" attached_file_ids={afi}"
                else:
                    pstr = f"payload_type={type(pd).__name__}"
            except: pstr = f"payload_raw={str(payload)[:80]}"
        content_preview = (content or "")[:60]
        print(f"  id={mid} seq={seq} role={role} type={mt} rti={rti} cat={cat}")
        print(f"    content: {content_preview}")
        print(f"    {pstr}")

    # 3. Files
    print("\n=== FILES ===")
    rows = conn.execute(text(
        "SELECT id, public_id, original_name, file_type, file_ext, file_size, conversation_id "
        "FROM uploaded_files WHERE conversation_id=:cid AND deleted_at IS NULL"
    ), {"cid": conv_internal_id}).fetchall()
    print(f"  total: {len(rows)}")
    for r in rows:
        fid, fpid, name, ftype, fext, fsize, fcid = r
        print(f"  id={fid} pid={fpid} name={name} type={ftype} ext={fext} size={fsize}")

    # 4. AgentTasks
    print("\n=== AGENT TASKS ===")
    rows = conn.execute(text(
        "SELECT id, public_id, status, runtime_status, trigger_message_id, "
        "started_at, completed_at, "
        "TIMESTAMPDIFF(MICROSECOND, started_at, completed_at)/1000 AS duration_ms, "
        "conversation_id, engine_type, created_at "
        "FROM agent_tasks WHERE conversation_id=:cid AND deleted_at IS NULL ORDER BY created_at ASC"
    ), {"cid": conv_internal_id}).fetchall()
    print(f"  total: {len(rows)}")
    task_ids = []
    for r in rows:
        tid, tpid, st, rs, tmid, sa, ca, dur, tcid, et, cat = r
        task_ids.append(tid)
        print(f"  id={tid} pid={tpid} status={st} runtime={rs} trigger_msg_id={tmid} dur_ms={dur} engine={et}")

    # 5. Task events (for each task)
    for tid in task_ids:
        print(f"\n=== EVENTS for task_id={tid} ===")
        total = conn.execute(text("SELECT COUNT(*) FROM agent_events WHERE task_id=:tid"), {"tid": tid}).scalar()
        max_seq = conn.execute(text("SELECT MAX(sequence_no) FROM agent_events WHERE task_id=:tid"), {"tid": tid}).scalar()
        max_co_sql = conn.execute(text(
            "SELECT MAX(COALESCE(sequence_no, 1000000000 + rn)) FROM ("
            "  SELECT sequence_no, ROW_NUMBER() OVER (ORDER BY sequence_no IS NULL DESC, created_at ASC, id ASC) AS rn "
            "  FROM agent_events WHERE task_id=:tid"
            ") sub"
        ), {"tid": tid}).scalar()
        print(f"  total={total} max_seq={max_seq} max_canonical_order={max_co_sql}")

        # Show event types
        etype_rows = conn.execute(text(
            "SELECT event_type, COUNT(*) AS cnt, MIN(sequence_no), MAX(sequence_no) "
            "FROM agent_events WHERE task_id=:tid GROUP BY event_type ORDER BY MIN(sequence_no) IS NULL ASC, MIN(sequence_no) ASC"
        ), {"tid": tid}).fetchall()
        for r in etype_rows:
            print(f"    {r[0]}: cnt={r[1]} min_seq={r[2]} max_seq={r[3]}")

        # Show first and last 5 events
        first5 = conn.execute(text(
            "SELECT sequence_no, event_type, title, LEFT(payload_json, 120) FROM agent_events "
            "WHERE task_id=:tid ORDER BY sequence_no IS NULL ASC, sequence_no ASC, id ASC LIMIT 5"
        ), {"tid": tid}).fetchall()
        last5 = conn.execute(text(
            "SELECT sequence_no, event_type, title, LEFT(payload_json, 120) FROM agent_events "
            "WHERE task_id=:tid ORDER BY sequence_no DESC, id DESC LIMIT 5"
        ), {"tid": tid}).fetchall()
        print("  first 5:")
        for r in first5:
            print(f"    seq={r[0]} type={r[1]} title={r[2]}")
        print("  last 5:")
        for r in last5:
            print(f"    seq={r[0]} type={r[1]} title={r[2]}")

    # 6. Artifacts
    print("\n=== ARTIFACTS ===")
    for tid in task_ids:
        rows = conn.execute(text(
            "SELECT id, public_id, artifact_type, file_name, file_size, status, version_no, created_at "
            "FROM artifacts WHERE task_id=:tid AND deleted_at IS NULL"
        ), {"tid": tid}).fetchall()
        for r in rows:
            print(f"  task={tid} id={r[0]} pid={r[1]} type={r[2]} name={r[3]} size={r[4]} status={r[5]} v={r[6]}")

    # 7. Message-File join (attached_files in payload)
    print("\n=== MESSAGE-FILE ATTACHMENTS (from payload_json) ===")
    rows = conn.execute(text(
        "SELECT id, public_id, payload_json FROM messages "
        "WHERE conversation_id=:cid AND deleted_at IS NULL AND payload_json IS NOT NULL"
    ), {"cid": conv_internal_id}).fetchall()
    for r in rows:
        mid, mpid, payload = r
        try:
            pd = json.loads(payload) if isinstance(payload, str) else payload
            if isinstance(pd, dict) and pd.get("attached_file_ids"):
                print(f"  msg={mpid} attached_file_ids={pd['attached_file_ids']}")
        except: pass

    # 8. Check reply_to_message_id chain
    print("\n=== REPLY-TO CHAIN ===")
    rows = conn.execute(text(
        "SELECT m.public_id, m.role, m.reply_to_message_id, m.conversation_sequence, "
        "COALESCE(m2.public_id, 'NONE') AS replied_to_pid "
        "FROM messages m LEFT JOIN messages m2 ON m.reply_to_message_id = m2.id "
        "WHERE m.conversation_id=:cid AND m.deleted_at IS NULL "
        "ORDER BY m.conversation_sequence IS NULL ASC, m.conversation_sequence ASC, m.id ASC"
    ), {"cid": conv_internal_id}).fetchall()
    for r in rows:
        mpid, role, rti, seq, replied_to = r
        print(f"  msg={mpid} role={role} seq={seq} reply_to_internal={rti} replied_to_pid={replied_to}")
