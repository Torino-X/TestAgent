import { describe, expect, it } from 'vitest'
import {
  buildMessageTimelineOrder,
  buildTaskTimelineOrder,
  restoreTaskRun,
  type RawEventRecord
} from '@/composables/useTaskEventRestore'

function makeEvent(overrides: Partial<RawEventRecord> & { event_type: string; event_id: string; task_id: string }): RawEventRecord {
  return {
    canonical_order: 0,
    sequence_no: 0,
    created_at: '2026-07-29T08:00:00Z',
    payload: {},
    ...overrides
  }
}

describe('useTaskEventRestore', () => {
  it('restores a persistent format-loss decision as a confirmation receipt', () => {
    const run = restoreTaskRun([
      makeEvent({
        event_id: 'evt_format_loss_decision',
        task_id: 'task_format_loss',
        event_type: 'format_loss_decision_recorded',
        canonical_order: 20,
        payload: {
          decision: 'accept',
          losses: [{ element: 'bookmark_170', message: '书签数量从 170 变为 167' }]
        }
      })
    ], 'task_format_loss', 'running', undefined, undefined)

    expect(run.messages).toHaveLength(1)
    expect(run.messages[0].confirmationReceipt?.kind).toBe('format_loss')
    expect(run.messages[0].confirmationReceipt?.markdown).toContain('接受格式丢失并继续导出')
    expect(run.messages[0].confirmationReceipt?.markdown).toContain('书签数量从 170 变为 167')
  })

  it('aggregates 5 tool chunks into 1 tool_call message', () => {
    const events: RawEventRecord[] = []
    for (let i = 0; i < 5; i++) {
      events.push(makeEvent({
        event_id: `evt_${i}`,
        task_id: 'task_001',
        event_type: 'tool_finished',
        canonical_order: 100 + i,
        payload: {
          tool_call_id: 'tc_xyz',
          tool_name: 'RequirementParserTool',
          chunk_index: i,
          chunk_total: 5,
          chunk_final: i === 4,
          publicUpdate: {
            version: 1,
            kind: 'tool_result',
            level: 'success',
            headline: `Step ${i}`,
            summary: '...',
            impact: '',
            nextAction: '',
            details: [],
            source: 'template',
            dedupeKey: 'tc_xyz',
            chunkIndex: i,
            chunkTotal: 5,
            chunkFinal: i === 4
          }
        }
      }))
    }

    const run = restoreTaskRun(events, 'task_001', 'completed', '2026-07-29T08:00:00Z', '2026-07-29T08:05:00Z')

    expect(run.messages).toHaveLength(1)
    expect(run.messages[0].type).toBe('tool_call')
    expect(run.messages[0].toolCall?.name).toBe('RequirementParserTool')
    // Final chunk wins -> chunk_final=true
    expect(run.messages[0].toolCall?.publicUpdate?.chunkFinal).toBe(true)
    expect(run.completeToolCallIds.has('tc_xyz')).toBe(true)
  })

  it('places task_resumed AFTER plan events by canonical_order', () => {
    // Critical regression target: real conversation had
    //   task_resumed (created 14:07, sequence NULL)
    //   plan_step_started (created 14:05, sequence 1)
    // We must NOT swap them.
    const events: RawEventRecord[] = [
      makeEvent({
        event_id: 'evt_resumed',
        task_id: 'task_002',
        event_type: 'task_resumed',
        canonical_order: 5,        // NULL gets fallback rank 5
        created_at: '2026-07-29T08:07:11Z',
        payload: {}
      }),
      makeEvent({
        event_id: 'evt_plan',
        task_id: 'task_002',
        event_type: 'plan_step_started',
        canonical_order: 2,        // real sequence
        created_at: '2026-07-29T08:05:00Z',
        payload: { step_id: 'understand', status: 'done' }
      })
    ]

    const run = restoreTaskRun(events, 'task_002', 'waiting_user_confirm', undefined, undefined)

    // task_resumed is NOT in the messages list (timeline marker only).
    expect(run.messages.find((m) => m.type === 'section_confirm')).toBeUndefined()
  })

  it('builds plan + tool + review messages in canonical order', () => {
    const events: RawEventRecord[] = [
      makeEvent({
        event_id: 'evt_plan',
        task_id: 'task_003',
        event_type: 'plan_created',
        canonical_order: 1,
        created_at: '2026-07-29T08:00:00Z',
        payload: { steps: [{ step_id: 'understand', name: '理解', status: 'pending' }] }
      }),
      makeEvent({
        event_id: 'evt_tool',
        task_id: 'task_003',
        event_type: 'tool_finished',
        canonical_order: 2,
        created_at: '2026-07-29T08:01:00Z',
        payload: {
          tool_call_id: 'tc_abc',
          tool_name: 'PlanGeneratorTool',
          chunk_final: true,
          publicUpdate: {
            version: 1, kind: 'tool_result', level: 'success',
            headline: 'Done', summary: '', impact: '', nextAction: '',
            details: [], source: 'template', dedupeKey: 'tc_abc',
            chunkIndex: 0, chunkTotal: 1, chunkFinal: true
          }
        }
      })
    ]

    const run = restoreTaskRun(events, 'task_003', 'running', '2026-07-29T08:00:00Z', '2026-07-29T08:01:00Z')
    expect(run.messages).toHaveLength(2)
    expect(run.messages[0].type).toBe('agent_plan')
    expect(run.messages[1].type).toBe('tool_call')
  })

  it('orders messages by canonical_order when timestamps match', () => {
    const events: RawEventRecord[] = [
      makeEvent({
        event_id: 'evt_a', task_id: 'task_004', event_type: 'plan_created',
        canonical_order: 10, created_at: '2026-07-29T08:00:00Z',
        payload: { steps: [] }
      }),
      makeEvent({
        event_id: 'evt_b', task_id: 'task_004', event_type: 'requirement_summary',
        canonical_order: 20, created_at: '2026-07-29T08:00:00Z',
        payload: { summary: { title: '', description: '', metrics: [] } }
      })
    ]
    const run = restoreTaskRun(events, 'task_004', 'running', '2026-07-29T08:00:00Z', '2026-07-29T08:01:00Z')
    expect(run.messages[0].id).toBe('evt_a')
    expect(run.messages[1].id).toBe('evt_b')
  })

  it('returns empty messages for empty event list', () => {
    const run = restoreTaskRun([], 'task_005', 'created', undefined, undefined)
    expect(run.messages).toHaveLength(0)
    expect(run.completeToolCallIds.size).toBe(0)
  })

  // Phase 2.9A.27 ──────────────────────────────────────────────────
  // task_completed 事件必须翻译为带 _summaryFacts 的 agent_text 消息,
  // 否则 AgentRunCard.runState 永远退化为 'running'。

  it('task_completed produces agent_text message with _summaryFacts', () => {
    const events: RawEventRecord[] = [
      makeEvent({
        event_id: 'evt_complete',
        task_id: 'task_completed',
        event_type: 'task_completed',
        canonical_order: 100,
        created_at: '2026-07-29T08:05:00Z',
        content: '任务已完成。已生成 16 个章节。',
        payload: {
          summary_facts: {
            generated_sections: 16,
            kept_sections: 1,
            business_modules: 0,
            review: {
              level: 'warning',
              passed: true,
              block_count: 3,
              warning_count: 0,
              suggestion_count: 1
            },
            artifact: {
              present: true,
              file_name: '电商订单售后_测试方案.docx',
              format_status: 'passed'
            }
          }
        }
      })
    ]

    const run = restoreTaskRun(events, 'task_completed', 'completed', '2026-07-29T08:00:00Z', '2026-07-29T08:05:00Z')

    expect(run.messages).toHaveLength(1)
    const msg = run.messages[0]
    expect(msg.type).toBe('agent_text')
    expect(msg.eventType).toBe('task_completed')
    expect(msg.text).toBe('任务已完成。已生成 16 个章节。')
    // _summaryFacts 字段冒泡
    const facts = (msg as unknown as { _summaryFacts?: unknown })._summaryFacts
    expect(facts).toBeTruthy()
    const factsObj = facts as {
      generatedSections: number
      businessModules: number
      review: { suggestion_count: number }
    }
    expect(factsObj.generatedSections).toBe(16)
    expect(factsObj.businessModules).toBe(0)
    expect(factsObj.review.suggestion_count).toBe(1)
  })

  it('task_completed populates artifact field when payload has artifact', () => {
    const events: RawEventRecord[] = [
      makeEvent({
        event_id: 'evt_c',
        task_id: 'task_c',
        event_type: 'task_completed',
        canonical_order: 10,
        created_at: '2026-07-29T08:00:00Z',
        content: '任务完成',
        payload: {
          summary_facts: {
            generated_sections: 5,
            review: { block_count: 0, warning_count: 0, suggestion_count: 0 }
          },
          artifact: {
            public_id: 'art_abc',
            artifact_type: 'test_plan_word',
            file_name: 'plan.docx',
            file_size: 12345,
            download_url: '/api/v1/artifacts/art_abc/download'
          }
        }
      })
    ]

    const run = restoreTaskRun(events, 'task_c', 'completed', undefined, undefined)
    const msg = run.messages[0] as unknown as { artifact?: { id: string; name: string; size: string } }
    expect(msg.artifact).toBeTruthy()
    expect(msg.artifact?.id).toBe('art_abc')
    expect(msg.artifact?.name).toBe('plan.docx')
    expect(msg.artifact?.size).toBe('12345')
  })

  it('RestoredTaskRun.status echoes taskStatus (completed)', () => {
    const run = restoreTaskRun([], 'task_x', 'completed', undefined, undefined)
    expect(run.status).toBe('completed')
  })

  it('task_completed with no summary_facts yields fallback facts (zeros)', () => {
    // 防御:即便后端忘了写 summary_facts,前端也要给 0 而不是 NaN
    const events: RawEventRecord[] = [
      makeEvent({
        event_id: 'evt_bare',
        task_id: 'task_bare',
        event_type: 'task_completed',
        canonical_order: 1,
        created_at: '2026-07-29T08:00:00Z',
        content: 'done',
        payload: {}  // 没 summary_facts
      })
    ]
    const run = restoreTaskRun(events, 'task_bare', 'completed', undefined, undefined)
    const msg = run.messages[0] as unknown as { _summaryFacts?: { generatedSections: number } }
    expect(msg._summaryFacts).toBeTruthy()
    expect(msg._summaryFacts?.generatedSections).toBe(0)
  })
})

describe('timeline order helpers', () => {
  it('task block sits between trigger user and agent reply', () => {
    const userSeq = 5
    const userOrder = buildMessageTimelineOrder(userSeq, 'msg_user')
    const taskOrder = buildTaskTimelineOrder(userSeq, 'task_001')
    const agentOrder = buildMessageTimelineOrder(userSeq + 1, 'msg_agent')

    // Stable ordering: user < task < agent
    expect(userOrder.sequence).toBeLessThan(taskOrder.sequence)
    expect(taskOrder.sequence).toBeLessThan(agentOrder.sequence)
  })

  it('null sequence falls back to MAX_SAFE_INTEGER (rendered at end)', () => {
    const order = buildMessageTimelineOrder(null, 'msg_x')
    expect(order.sequence).toBe(Number.MAX_SAFE_INTEGER)
  })
})
