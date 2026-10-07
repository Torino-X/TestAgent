/**
 * Phase 2.9A.35 — 真实复现 fixture。
 *
 * 基于 conv_7fe5806c / task_ed72a8ab / confirm_976a1129 的真实数据。
 * 数据契约(与后端实际输出一致):
 *  - 34 条 Graph 事件(sequence_no 1~34, canonical_order 1~34)
 *  - 4 个 Tool,每个 tool_started + 5 个 tool_finished chunk
 *  - 全部 payload 为 JSON 字符串(event-list 的 payload_json 原始值)
 *  - need_user_confirm 使用嵌套 section_suggestions.sections
 *  - need_user_confirm(seq 33)先于 task_waiting(seq 34)
 *  - Legacy task_created: sequence_no=null, canonical_order=1000000001
 *  - created_at 为无时区 UTC 字符串("YYYY-MM-DD HH:mm:ss")
 *  - Task Detail: status=waiting_user_confirm, started_at 带 +00:00
 */

import type { RawEventInput } from './useTaskEventReducer'
import type { AgentTask } from '@/types'

export const TASK_ID = 'task_ed72a8ab'
export const CONVERSATION_ID = 'conv_7fe5806c'
export const CONFIRMATION_ID = 'confirm_976a1129'

export const TOOL_TIMINGS: Record<string, { name: string; durationMs: number; toolCallId: string }> = {
  requirement: { name: 'RequirementParserTool', durationMs: 25120, toolCallId: 'tc_requirement_parser' },
  template: { name: 'TemplateParserTool', durationMs: 599, toolCallId: 'tc_template_parser' },
  knowledge: { name: 'KnowledgeSearchTool', durationMs: 122, toolCallId: 'tc_knowledge_search' },
  sections: { name: 'SectionSuggestionTool', durationMs: 113, toolCallId: 'tc_section_suggestion' }
}

/** 无时区 UTC 字符串 — 后端 event-list created_at 的原始形态。 */
function utcStr(minuteOffset: number, secondOffset: number): string {
  const base = new Date(Date.UTC(2026, 6, 30, 15, 23, 0))
  const d = new Date(base.getTime() + minuteOffset * 60_000 + secondOffset * 1000)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`
}

const json = (obj: unknown): string => JSON.stringify(obj)

const SECTIONS = Array.from({ length: 17 }, (_, i) => ({
  section_id: `sec_${i + 1}`,
  section_number: String(i + 1),
  section_title: `第 ${i + 1} 章`,
  level: 'H1',
  suggested_action: i % 3 === 0 ? 'keep_template' : 'ai_generate',
  action: i % 3 === 0 ? 'keep_template' : 'ai_generate',
  reason: i % 3 === 0 ? '保留模板原文' : 'AI 生成'
}))

interface FixtureOptions {
  /** 是否把 legacy task_created 也包含进事件列表 */
  includeLegacyTaskCreated?: boolean
}

export function buildFixtureEvents(options: FixtureOptions = {}): RawEventInput[] {
  const { includeLegacyTaskCreated = true } = options
  const events: RawEventInput[] = []
  let seq = 0
  let createdOffset = 0

  const push = (event_type: string, payload: unknown, overrides: Partial<RawEventInput> = {}) => {
    seq += 1
    createdOffset += 1
    events.push({
      event_id: `evt_graph_${seq}`,
      event_type,
      task_id: TASK_ID,
      sequence_no: seq,
      canonical_order: seq,
      payload: json(payload),
      created_at: utcStr(0, createdOffset),
      ...overrides
    })
  }

  // ── plan ──────────────────────────────────────────────────────────
  push('plan_created', {
    steps: [
      { step_id: 'understand', name: '理解任务', status: 'done' },
      { step_id: 'parse_requirement', name: '解析需求文档', status: 'done' },
      { step_id: 'parse_template', name: '解析测试方案模板', status: 'done' },
      { step_id: 'search_knowledge', name: '检索公司知识库', status: 'done' },
      { step_id: 'suggest_sections', name: '生成章节处理建议', status: 'done' },
      { step_id: 'wait_user_confirm', name: '确认章节生成范围', status: 'running' }
    ]
  })
  push('requirement_summary', { summary: { title: '需求解析', description: '已解析需求文档', metrics: [{ label: '章节', value: '17' }] } })
  push('template_summary', { summary: { title: '模板解析', description: '已识别测试方案模板', metrics: [{ label: '模板', value: 'QA' }] } })
  push('knowledge_summary', { summary: { title: '知识库检索', description: '已检索知识库', metrics: [{ label: '命中', value: '3' }] } })

  // ── 每个 Tool: 1 tool_started + 5 tool_finished chunks ─────────────
  for (const key of ['requirement', 'template', 'knowledge', 'sections'] as const) {
    const t = TOOL_TIMINGS[key]
    const startOffset = createdOffset
    const startAt = utcStr(0, startOffset)
    const endAt = utcStr(0, startOffset + Math.max(1, Math.round(t.durationMs / 1000)))
    push('tool_started', {
      tool_name: t.name,
      tool_call_id: t.toolCallId,
      attempt: 1,
      chunk_index: 0,
      display_input: `${t.name} 输入摘要`
    }, { created_at: startAt })
    for (let i = 0; i < 5; i++) {
      const final = i === 4
      push('tool_finished', {
        tool_name: t.name,
        tool_call_id: t.toolCallId,
        attempt: 1,
        chunk_index: i,
        chunk_total: 5,
        chunk_final: final,
        duration_ms: t.durationMs,
        display_output: final ? `${t.name} 完成` : `${t.name} 第 ${i + 1} 段`
      }, { created_at: endAt })
    }
  }

  // ── 补齐到 need_user_confirm=33 / task_waiting=34 ────────────────
  // 当前已有: plan(1) + summaries(2-4) + tools(5-28) = 28 条。
  // 需要 need_user_confirm=33, task_waiting=34,中间补 4 条 plan_step。
  push('plan_step_started', { step: 'understand', status: 'running' })
  push('plan_step_started', { step: 'parse_requirement', status: 'running' })
  push('plan_step_completed', { step: 'understand', status: 'done' })
  push('plan_step_completed', { step: 'parse_requirement', status: 'done' })

  // ── 章节确认流程: need_user_confirm(33) 先于 task_waiting(34) ─────
  push('need_user_confirm', {
    confirmation_id: CONFIRMATION_ID,
    section_count: 17,
    confirmation_type: 'section_generation_config',
    section_suggestions: { sections: SECTIONS }
  })
  push('task_waiting', { confirmation_id: CONFIRMATION_ID })

  // ── Legacy task_created: sequence_no=null, canonical_order 巨大 ────
  if (includeLegacyTaskCreated) {
    events.push({
      event_id: 'evt_legacy_task_created',
      event_type: 'task_created',
      task_id: TASK_ID,
      sequence_no: null,
      canonical_order: 1000000001,
      payload: json({}),
      created_at: '2026-07-30 15:23:39'
    })
  }

  return events
}

/** Task Detail 权威数据 — started_at 带 +00:00, status=waiting_user_confirm。 */
export function buildTaskDetail(): AgentTask {
  return {
    task_id: TASK_ID,
    task_type: 'test_plan_generation',
    status: 'waiting_user_confirm',
    runtimeStatus: 'running',
    events_url: `/api/agent/tasks/${TASK_ID}/events`,
    startedAt: '2026-07-30T15:23:41+00:00',
    triggerMessageId: 'msg_ea9d2611',
    run: {
      runId: 'run_1',
      status: 'running',
      startedAt: '2026-07-30T15:23:41+00:00'
    }
  }
}

/** 真实 need_user_confirm 的 event-list payload(JSON 字符串)。 */
export const REAL_NEED_USER_CONFIRM_PAYLOAD = json({
  confirmation_id: CONFIRMATION_ID,
  section_count: 17,
  confirmation_type: 'section_generation_config',
  section_suggestions: { sections: SECTIONS }
})
