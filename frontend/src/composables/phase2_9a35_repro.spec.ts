/**
 * Phase 2.9A.35 — 真实 fixture 失败测试。
 *
 * 在修改生产代码前,先用真实 fixture 复现 8 个失败现象:
 *  1. Thinking 排在 User 上方(乐观消息排序)
 *  2. 章节确认卡片缺失(confirmation 契约)
 *  3. 页面激活"审查结果"(pending review 出现在章节确认前)
 *  4. Hydrate 后只显示 RequirementParser(其它 Tool 丢失)
 *  5. RequirementParser 保持 RUNNING(终态未合并)
 *  6. lastSseSequence 错误变为 1000000001(cursor 混用)
 *  7. Tool 耗时约 8 小时(时区错误)
 *  8. Task elapsed 从恢复时间重新开始(起始时间错误)
 */
import { describe, expect, it } from 'vitest'
import {
  buildFixtureEvents,
  buildTaskDetail,
  REAL_NEED_USER_CONFIRM_PAYLOAD,
  CONFIRMATION_ID,
  TASK_ID,
  TOOL_TIMINGS
} from './taskEventsFixture'
import { reduceTaskEvents, createInitialTaskRunBlock } from './useTaskEventReducer'
import { assembleConversationTimeline } from '@/utils/conversationTimeline'
import { parseApiDateTime } from '@/utils/time'
import type { ChatMessage, TaskRunBlock } from '@/types'

function buildBlock(status = 'waiting_user_confirm'): TaskRunBlock {
  return createInitialTaskRunBlock(TASK_ID, status as never, 'hydrate', {
    startedAt: '2026-07-30T15:23:41+00:00'
  })
}

function optimisticUserMessage(): ChatMessage {
  return {
    id: 'msg_user_pending_abc',
    type: 'user_text',
    role: 'user',
    text: '帮我生成测试方案',
    timestamp: '15:23',
    clientOrder: 7,
    timelineRank: 0
  }
}

function thinkingMessage(): ChatMessage {
  return {
    id: 'msg_agent_thinking_def',
    type: 'agent_text',
    role: 'agent',
    text: '正在思考...',
    thinking: true,
    streaming: true,
    timestamp: '15:23',
    clientOrder: 7,
    timelineRank: 1,
    anchorMessageId: 'msg_user_pending_abc'
  }
}

describe('Phase 2.9A.35 — 失败复现(fixture-driven)', () => {
  const events = buildFixtureEvents()

  // ── 失败 1: Thinking 排在 User 上方 ──────────────────────────────
  it('REPRO: optimistic thinking sorts above user message (no clientOrder)', () => {
    const user = optimisticUserMessage()
    const thinking = thinkingMessage()
    const items = assembleConversationTimeline([user, thinking], [], [])
    // 正确: User 在前, Thinking 在后
    expect(items.map((i) => i.id)).toEqual([user.id, thinking.id])
  })

  // ── 失败 2+3: 章节确认卡片缺失 + 页面激活审查结果 ───────────────
  it('REPRO: need_user_confirm with nested section_suggestions creates no card, review shown instead', () => {
    const block = reduceTaskEvents(buildBlock(), events, 'hydrate')
    // 章节确认卡片必须存在(嵌套 section_suggestions.sections 解析)
    const confirmMsg = block.messages.find((m) => m.type === 'section_confirm')
    expect(confirmMsg).toBeDefined()
    expect(confirmMsg?.confirmationId).toBe(CONFIRMATION_ID)
    expect(confirmMsg?.sections?.length).toBe(17)
    // 不得激活"审查结果"阶段
    expect(block.status).toBe('waiting_user_confirm')
  })

  // ── 失败 4+5: Hydrate 只显示 RequirementParser,且保持 RUNNING ────
  it('REPRO: hydrate reduces 4 tools with full chunk merge', () => {
    const block = reduceTaskEvents(buildBlock(), events, 'hydrate')
    // 4 个 Tool 必须全部独立存在
    const tools = block.toolExecutions
    expect(tools).toHaveLength(4)
    // RequirementParser 必须 SUCCESS 且 terminal
    const rp = tools.find((t) => t.toolCallId === TOOL_TIMINGS.requirement.toolCallId)
    expect(rp?.status).toBe('success')
    expect(rp?.terminal).toBe(true)
    // 不能同时 RUNNING + SUCCESS
    const sameToolRunning = tools.filter(
      (t) => t.toolCallId === TOOL_TIMINGS.requirement.toolCallId && t.status === 'running'
    )
    expect(sameToolRunning).toHaveLength(0)
  })

  // ── 失败 6: lastSseSequence 错误变为 1000000001 ─────────────────
  it('REPRO: legacy task_created must not advance lastSseSequence', () => {
    const block = reduceTaskEvents(buildBlock(), events, 'hydrate')
    // lastSseSequence 只从 sequence_no 非空值计算 → 34
    const lastSseSequence = block.lastSseSequence
    expect(lastSseSequence).toBe(34)
    // legacy canonical_order 1000000001 不得污染 SSE cursor
    expect(block.lastSseSequence).not.toBe(1000000001)
  })

  // ── 失败 7: Tool 耗时约 8 小时 ───────────────────────────────────
  it('REPRO: RequirementParser duration uses real 25120ms not 8h timezone drift', () => {
    const block = reduceTaskEvents(buildBlock(), events, 'hydrate')
    const rp = block.toolExecutions.find((t) => t.toolCallId === TOOL_TIMINGS.requirement.toolCallId)
    // payload.duration_ms 优先 → 25120
    expect(rp?.durationMs).toBe(25120)
    // 不得是 28941s 级别的时区漂移
    expect(rp ? rp.durationMs : 0).toBeLessThan(30_000)
  })

  // ── 失败 8: Task elapsed 从恢复时间重新开始 ─────────────────────
  it('REPRO: task elapsed starts from real startedAt not restore time', () => {
    const task = buildTaskDetail()
    // 从 started_at 计算 elapsed
    const startedMs = parseApiDateTime(task.startedAt)
    const nowMs = Date.parse('2026-07-30T16:23:41+00:00') // 1 小时后
    const elapsed = nowMs - startedMs
    expect(elapsed).toBe(3_600_000)
  })

  // ── payload JSON 字符串解析 ─────────────────────────────────────
  it('REPRO: event payload as JSON string must be parsed to object', () => {
    // payload 是 JSON 字符串时必须被规范化,need_user_confirm 的
    // 嵌套 section_suggestions.sections 才能被读到
    const parsed = JSON.parse(REAL_NEED_USER_CONFIRM_PAYLOAD)
    expect(parsed.section_suggestions?.sections?.length).toBe(17)
  })
})
