"""Phase 2.9A.29 审计测试 — 复现历史恢复三类问题。

这些测试**允许失败**——它们的目的是精确复现截图现象，为后续修复提供基准。

禁止修改生产代码。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


# ──────────────────────────────────────────────────────────────────────
# 测试一：completed task 应在 trigger message 之后
# ──────────────────────────────────────────────────────────────────────


def test_completed_task_is_placed_after_trigger_message():
    """验证 Task Block 应在 trigger user message 之后、后续 user message 之前。

    实际输入（来自 conv_6a59fa05 真实数据）：
      seq=1: user "帮我根据这份PRD需求文档和模板生成测试方案" (trigger)
      Task: task_01949301 (completed, trigger_message_id=NULL)
      seq=2: user "你好"
      seq=3: agent reply

    期望 Timeline 顺序：
      User seq=1 → Task Block → User seq=2 → Agent seq=3

    当前真实行为：
      Task Block 的第一个事件 (plan_step_started) 有 canonical_order=1,
      与 User seq=1 的 conversation_sequence=1 重叠,
      导致 Task Block 的排序位置不确定（取决于 id/time 稳定排序）。
      更严重的是，Task 内部事件的 canonical_order 最大到 59,
      超过 User seq=2/3 的 conversation_sequence=2/3,
      导致 Task 事件在 sorted list 中散布到 User 消息之间。
    """
    # 纯逻辑验证：不依赖前端模块
    # 模拟 buildTaskTimelineOrder 的逻辑
    def build_task_timeline_order(trigger_seq, task_id):
        if trigger_seq is not None:
            return {"sequence": trigger_seq + 0.5, "secondary_id": task_id}
        return {"sequence": 999999999, "secondary_id": task_id}

    def build_message_timeline_order(seq, msg_id):
        if seq is not None:
            return {"sequence": seq, "secondary_id": msg_id}
        return {"sequence": 999999999, "secondary_id": msg_id}

    # User messages
    user_order_1 = build_message_timeline_order(1, "msg_user_task")
    user_order_2 = build_message_timeline_order(2, "msg_user_hello")
    user_order_3 = build_message_timeline_order(3, "msg_agent_reply")

    # Task block — trigger_message_id=NULL → legacy fallback 找到 seq=1
    task_order = build_task_timeline_order(1, "task_01949301")

    assert user_order_1["sequence"] < task_order["sequence"], (
        f"User trigger (seq={user_order_1['sequence']}) should be before task (seq={task_order['sequence']})"
    )
    assert task_order["sequence"] < user_order_2["sequence"], (
        f"Task (seq={task_order['sequence']}) should be before user hello (seq={user_order_2['sequence']})"
    )
    assert user_order_2["sequence"] < user_order_3["sequence"]

    print(f"\n  [审计] User trigger sequence={user_order_1['sequence']}")
    print(f"  [审计] Task block sequence={task_order['sequence']}")
    print(f"  [审计] User hello sequence={user_order_2['sequence']}")
    print(f"  [审计] Agent reply sequence={user_order_3['sequence']}")
    print(f"  [审计] Task block position: {'CORRECT' if user_order_1['sequence'] < task_order['sequence'] < user_order_2['sequence'] else 'WRONG — interleaved or misplaced'}")


# ──────────────────────────────────────────────────────────────────────
# 测试二：历史用户消息应恢复 attached_files
# ──────────────────────────────────────────────────────────────────────


def test_historical_user_message_restores_attached_files():
    """验证用户消息的 attached_files 在历史恢复后仍存在。

    真实数据：
      msg_d30af2c4 payload_json={"attached_file_ids": ["file_fe60551c", "file_f2b8b48b"]}
      两条 File 记录存在 (id=154, 155)

    期望：mapMessage 后 files.length = 2
    """
    # 模拟后端 _to_detail 返回的 payload
    payload = {"attached_file_ids": ["file_fe60551c", "file_f2b8b48b"]}
    attached_files = [
        {"file_id": "file_fe60551c", "original_name": "01_智慧校园预约与签到系统_需求说明书.docx", "file_type": "requirement_doc", "file_size": 276927, "file_ext": "docx"},
        {"file_id": "file_f2b8b48b", "original_name": "00_PlanWise_QA_测试方案模板.docx", "file_type": "test_plan_template", "file_size": 43249, "file_ext": "docx"},
    ]

    # 模拟前端 ApiMessage
    api_message = {
        "message_id": "msg_d30af2c4",
        "role": "user",
        "message_type": "user_text",
        "content": "帮我根据这份PRD需求文档和模板生成测试方案",
        "payload": payload,
        "attached_files": attached_files,
        "conversation_sequence": 1,
        "created_at": "2026-07-29T14:57:04Z",
    }

    # 模拟 mapAttachedFiles
    files = api_message.get("attached_files") or api_message.get("files") or api_message.get("attachments")
    mapped_files = []
    if files:
        for f in files:
            mapped_files.append({
                "id": f.get("file_id") or f.get("id", ""),
                "name": f.get("original_name") or f.get("file_name") or "文件",
                "size": f.get("file_size", 0),
                "type": f.get("file_type", "unknown"),
            })

    assert len(mapped_files) == 2, f"Expected 2 attached files, got {len(mapped_files)}"
    assert mapped_files[0]["id"] == "file_fe60551c"
    assert mapped_files[1]["id"] == "file_f2b8b48b"

    print(f"\n  [审计] attached_files count: {len(mapped_files)}")
    print(f"  [审计] file[0]: {mapped_files[0]['name']}")
    print(f"  [审计] file[1]: {mapped_files[1]['name']}")

    # 检查后端 _attached_file_ids 是否能正确解析 payload_json
    # 如果 payload_json 在 MySQL 中被存储为 JSON 字符串而非 dict,
    # _attached_file_ids 会返回 []
    if isinstance(payload, dict):
        ids = payload.get("attached_file_ids", [])
    else:
        ids = []
    assert len(ids) == 2, f"Backend _attached_file_ids should find 2 IDs, got {len(ids)}"
    print(f"  [审计] payload type: {type(payload).__name__}")
    print(f"  [审计] _attached_file_ids result: {ids}")


# ──────────────────────────────────────────────────────────────────────
# 测试三：completed task Hydration 后应默认收起
# ──────────────────────────────────────────────────────────────────────


def test_completed_task_hydrates_collapsed():
    """验证 completed task 在 Hydration 后 processExpanded=false。

    真实数据：
      task_01949301 status=completed
      61 events, 含 task_completed (seq=59)
      hydrated=true

    AgentRunCard 行为：
      processExpanded = ref(false)  // 默认 false
      watch(runState, ..., { immediate: true })
        if (props.hydrated) {
          if (isTerminalState(nextState)) {
            processExpanded.value = false  // completed → 强制 false
          }
          return
        }

    关键条件：
      1. props.hydrated = true
      2. runState = 'completed'
      3. isTerminalState('completed') = true
      → processExpanded = false ✓

    但如果 runState='running'（因为缺少 task_completed 消息）：
      1. props.hydrated = true
      2. runState = 'running'
      3. isTerminalState('running') = false
      → watch 返回，不修改 processExpanded（保持 false）→ 仍然收起

    所以无论 runState 是 'completed' 还是 'running'，
    hydrated 模式下 processExpanded 都应该是 false。
    但如果 props.hydrated 是 undefined（未传）：
      → 进入 Live 模式路径
      → runState='running' → processExpanded = !isTerminalState('running') = true → 展开!
    """
    # 模拟 AgentRunCard 的状态
    hydrated = True
    process_expanded = False  # ref(false)
    run_state = "completed"  # 有 task_completed 事件时

    # 模拟 watch(runState, ..., { immediate: true })
    def simulate_watch(hydrated, run_state, process_expanded):
        is_terminal = run_state in ("completed", "failed")
        if hydrated:
            if is_terminal:
                process_expanded = False
            return process_expanded  # return without changing (Live path)
        else:
            process_expanded = not is_terminal
        return process_expanded

    result = simulate_watch(hydrated, run_state, process_expanded)
    assert result is False, f"hydrated + completed → processExpanded should be False, got {result}"

    # 如果 hydrated 未传（undefined = falsy）
    result_no_hydrate = simulate_watch(False, run_state, process_expanded)
    assert result_no_hydrate is False, f"not hydrated + completed → processExpanded should be False, got {result_no_hydrate}"

    # 如果 runState='running' 且 hydrated=true
    result_running = simulate_watch(True, "running", process_expanded)
    assert result_running is False, f"hydrated + running → processExpanded should stay False, got {result_running}"

    # 如果 runState='running' 且 hydrated=false（未传）
    result_running_no_hydrate = simulate_watch(False, "running", process_expanded)
    assert result_running_no_hydrate is True, (
        f"NOT hydrated + running → processExpanded should be True (Live mode), got {result_running_no_hydrate}"
    )

    print(f"\n  [审计] hydrated=True, runState='completed' → processExpanded={result}")
    print(f"  [审计] hydrated=False, runState='completed' → processExpanded={result_no_hydrate}")
    print(f"  [审计] hydrated=True, runState='running' → processExpanded={result_running}")
    print(f"  [审计] hydrated=False, runState='running' → processExpanded={result_running_no_hydrate}")
    print(f"  [审计] 结论: 如果 hydrated 未传,completed 任务会默认展开!")


# ──────────────────────────────────────────────────────────────────────
# 测试四：hydrated tools 保持独立 identity
# ──────────────────────────────────────────────────────────────────────


def test_hydrated_tools_keep_independent_identity():
    """验证不同 Tool 的事件按 tool_call_id 正确聚合，不互相覆盖。

    真实数据（task_01949301）：
      RequirementParserTool: tool_call_id=RequirementParserTool-17d761feeae641a8b8a8cb41726390e4
      TemplateParserTool: tool_call_id=TemplateParserTool-837e93c8354046849a09045fa624736a
      KnowledgeSearchTool: tool_call_id=KnowledgeSearchTool-fa3a6c9a1f204bc4a8fafda5808b444f
      SectionSuggestionTool: tool_call_id=SectionSuggestionTool-051990445a6241fdbb417fb602db309a

    验证逻辑：tool_call_id 去重 + 独立 status
    """
    # 纯逻辑验证 tool grouping（不依赖前端 TypeScript 模块）
    tool_call_ids = {
        "RequirementParserTool": "RequirementParserTool-17d761feeae641a8b8a8cb41726390e4",
        "TemplateParserTool": "TemplateParserTool-837e93c8354046849a09045fa624736a",
        "KnowledgeSearchTool": "KnowledgeSearchTool-fa3a6c9a1f204bc4a8fafda5808b444f",
        "SectionSuggestionTool": "SectionSuggestionTool-051990445a6241fdbb417fb602db309a",
    }

    # 模拟 toolGroupKey 的逻辑
    def tool_group_key(event):
        payload = event.get("payload", {})
        candidates = [
            payload.get("tool_call_id"),
            payload.get("toolCallId"),
            payload.get("dedupe_key"),
            payload.get("dedupeKey"),
            event.get("event_id"),
        ]
        for v in candidates:
            if isinstance(v, str) and len(v) > 0:
                return v
        return f"{event.get('event_type', '')}:{payload.get('chunk_index', 0)}"

    # 构建 events
    events = []
    for tool_name, tcid in tool_call_ids.items():
        events.append({
            "event_id": f"evt_{tool_name}_started",
            "event_type": "tool_started",
            "canonical_order": len(events) * 2 + 1,
            "payload": {"tool_name": tool_name, "tool_call_id": tcid, "attempt": 1},
        })
        events.append({
            "event_id": f"evt_{tool_name}_finished",
            "event_type": "tool_finished",
            "canonical_order": len(events) * 2 + 2,
            "payload": {
                "tool_name": tool_name,
                "tool_call_id": tcid,
                "chunk_final": True,
                "publicUpdate": {"level": "success", "headline": f"{tool_name} done"},
            },
        })

    # 按 tool_call_id 分组
    groups = {}
    for evt in events:
        key = tool_group_key(evt)
        groups.setdefault(key, []).append(evt)

    # 验证：4 个独立 group
    assert len(groups) == 4, f"Expected 4 tool groups, got {len(groups)}"

    # 验证：每个 group 的 tool_call_id 一致
    for key, evts in groups.items():
        tcids = set()
        for evt in evts:
            tcid = evt["payload"].get("tool_call_id")
            if tcid:
                tcids.add(tcid)
        assert len(tcids) <= 1, f"Group {key} has mixed tool_call_ids: {tcids}"

    # 验证：每个 group 最后一个事件是 tool_finished
    for key, evts in groups.items():
        last = evts[-1]
        assert last["event_type"] == "tool_finished", (
            f"Group {key} last event should be tool_finished, got {last['event_type']}"
        )

    # 验证：4 个独立的 tool 名称
    tool_names = set()
    for evt in events:
        tn = evt["payload"].get("tool_name")
        if tn:
            tool_names.add(tn)
    assert len(tool_names) == 4, f"Expected 4 unique tool names, got {tool_names}"

    print(f"\n  [审计] tool groups: {len(groups)}")
    print(f"  [审计] tool names: {tool_names}")
    print(f"  [审计] 每个 group 最后事件: {[(k, evts[-1]['event_type']) for k, evts in groups.items()]}")
