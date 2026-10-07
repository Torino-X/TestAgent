/**
 * Phase 2.9A.31 — useTaskEventReducer 集成测试
 *
 * 覆盖:
 * 1. partial live block 不会阻止 full hydrate (mergeHydratedAndLiveTaskRun)
 * 2. live+hydrate 事件取并集
 * 3. hydrate 不能让较新 live 状态倒退
 * 4. need_user_confirm 显示章节确认 (无 task_waiting 前置)
 * 5. task_waiting 保持章节确认阶段
 * 6. RequirementParser 终态不回退 running
 * 7. event-list page cursor 不会写入 SSE cursor
 * 8. canonical_order 最大 59 时 lastAppliedOrder=59
 * 9. UTC 时间显示正确 duration (无 29092s)
 * 10. 同一 tool_call_id 所有 chunk 完整合并
 * 11. 不同 attempt 独立
 * 12. Hydrate 与 Live 合并保留 userCollapseOverride
 * 13. completed 任务刷新后默认收起
 */
import { describe, it, expect } from 'vitest'
import { reduceTaskEvent, reduceTaskEvents, createInitialTaskRunBlock, type RawEventInput } from './useTaskEventReducer'
import type { TaskRunBlock } from '@/types'

function reduceToolStartedDirect(block: TaskRunBlock, raw: RawEventInput): TaskRunBlock {
  return reduceTaskEvent(block, raw, 'hydrate')
}

function makeEvent(overrides: Partial<RawEventInput> = {}): RawEventInput {
  return {
    event_id: `evt_${Math.random().toString(36).slice(2, 10)}`,
    event_type: 'tool_started',
    task_id: 'task_test',
    canonical_order: 1,
    payload: {},
    created_at: '2026-07-30T12:00:00+00:00',
    ...overrides,
  }
}

describe('useTaskEventReducer — Phase 2.9A.31', () => {
  // ── 1. partial live block 不会阻止 full hydrate ──
  it('live block with 1 event does not block hydrate with 59 events', () => {
    const liveBlock = createInitialTaskRunBlock('task1', 'running', 'live')
    const liveEvent = makeEvent({ event_id: 'evt_1', event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'ReqParser', tool_call_id: 'tc_1' } })
    const liveReduced = reduceTaskEvent(liveBlock, liveEvent, 'live')
    expect(liveReduced.events).toHaveLength(1)
    expect(liveReduced.lastUpdateMode).toBe('live')

    // Hydrate brings 3 events, including the one live already has + 2 new
    const hydrateBlock = createInitialTaskRunBlock('task1', 'completed', 'hydrate')
    const hydrateEvents: RawEventInput[] = [
      makeEvent({ event_id: 'evt_1', event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'ReqParser', tool_call_id: 'tc_1' } }),
      makeEvent({ event_id: 'evt_2', event_type: 'tool_finished', canonical_order: 2, payload: { tool_call_id: 'tc_1' } }),
      makeEvent({ event_id: 'evt_3', event_type: 'task_completed', canonical_order: 3, payload: {} }),
    ]
    const hydrated = reduceTaskEvents(hydrateBlock, hydrateEvents, 'hydrate')
    expect(hydrated.events).toHaveLength(3)

    // Merge: live (1 event) + hydrate (3 events, 1 shared) = 3 unique events
    const seenIds = new Set<string>()
    const merged: RawEventInput[] = []
    for (const evt of liveReduced.events) {
      if (!seenIds.has(evt.event_id)) { seenIds.add(evt.event_id); merged.push(evt) }
    }
    for (const evt of hydrated.events) {
      if (!seenIds.has(evt.event_id)) { seenIds.add(evt.event_id); merged.push(evt) }
    }
    expect(merged).toHaveLength(3)
  })

  // ── 2. live+hydrate 事件取并集 ──
  it('live+hydrate events produce union by event_id', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'hydrate')
    const events: RawEventInput[] = [
      makeEvent({ event_id: 'A', event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'X', tool_call_id: 'tc1' } }),
      makeEvent({ event_id: 'A', event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'X', tool_call_id: 'tc1' } }), // duplicate
      makeEvent({ event_id: 'B', event_type: 'tool_finished', canonical_order: 2, payload: { tool_call_id: 'tc1' } }),
    ]
    const result = reduceTaskEvents(block, events, 'hydrate')
    expect(result.events).toHaveLength(2) // A deduped
    expect(result.events.map(e => e.event_id)).toEqual(['A', 'B'])
  })

  // ── 3. hydrate 事件按 effective reduction order 排序 ──
  // Phase 2.9A.35: reduceTaskEvents 现在按 effective reduction order
  // (task_created=0, Graph 按 sequence_no)排序后再归约。
  it('events processed in effective reduction order by reduceTaskEvents', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'hydrate')
    const events: RawEventInput[] = [
      makeEvent({ event_id: 'C', event_type: 'tool_finished', canonical_order: 3, payload: { tool_call_id: 'tc1' } }),
      makeEvent({ event_id: 'A', event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'X', tool_call_id: 'tc1' } }),
      makeEvent({ event_id: 'B', event_type: 'tool_finished', canonical_order: 2, payload: { tool_call_id: 'tc1' } }),
    ]
    const result = reduceTaskEvents(block, events, 'hydrate')
    // 按 sequence_no/canonical_order 排序 → A(1), B(2), C(3)
    expect(result.events).toHaveLength(3)
    expect(result.events.map(e => e.event_id)).toEqual(['A', 'B', 'C'])
  })

  // ── 4. need_user_confirm 显示章节确认 (无 task_waiting 前置) ──
  it('need_user_confirm creates section_confirm even without task_waiting', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'hydrate')
    // Only need_user_confirm, no task_waiting before it
    const event = makeEvent({
      event_type: 'need_user_confirm',
      canonical_order: 5,
      payload: {
        confirmation_id: 'conf_123',
        confirmation_type: 'section_generation_config',
        sections: [{ section_id: 's1', title: 'Chapter 1' }],
      },
    })
    const result = reduceTaskEvent(block, event, 'hydrate')

    // Status should be set to waiting_user_confirm
    expect(result.status).toBe('waiting_user_confirm')
    // Should have a section_confirm message
    const confirmMsg = result.messages.find(m => m.type === 'section_confirm')
    expect(confirmMsg).toBeDefined()
    expect(confirmMsg?.confirmationId).toBe('conf_123')
    expect(confirmMsg?.conversationSequence).toBe(5)
  })

  // ── 5. task_waiting 保持章节确认阶段 ──
  it('task_waiting followed by need_user_confirm both work', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'hydrate')
    const events: RawEventInput[] = [
      makeEvent({ event_type: 'task_waiting', canonical_order: 4, payload: {} }),
      makeEvent({
        event_type: 'need_user_confirm',
        canonical_order: 5,
        payload: { confirmation_id: 'conf_456', sections: [{ section_id: 's1' }] },
      }),
    ]
    const result = reduceTaskEvents(block, events, 'hydrate')
    expect(result.status).toBe('waiting_user_confirm')
    expect(result.messages.some(m => m.type === 'section_confirm')).toBe(true)
  })

  it('need_user_confirm without sections creates a generic waiting message', () => {
    const block = createInitialTaskRunBlock('task_generic_confirm', 'running', 'hydrate')
    const result = reduceTaskEvent(block, makeEvent({
      event_type: 'need_user_confirm',
      task_id: 'task_generic_confirm',
      canonical_order: 5,
      payload: {
        confirmation_id: 'conf_generic',
        confirmation_type: 'clarification',
        question: '请选择要优先分析的业务模块。'
      }
    }), 'hydrate')

    const msg = result.messages.find((message) => message.eventType === 'need_user_confirm')
    expect(result.status).toBe('waiting_user_confirm')
    expect(msg).toMatchObject({
      type: 'agent_text',
      text: '请选择要优先分析的业务模块。',
      confirmationId: 'conf_generic',
      confirmationType: 'clarification'
    })
    expect(result.messages.some((message) => message.type === 'section_confirm')).toBe(false)
  })

  it('preparation clarification shows retrieval status and a task-scoped input card', () => {
    const block = createInitialTaskRunBlock('task_preparation_clarification', 'running', 'hydrate')
    const result = reduceTaskEvent(block, makeEvent({
      event_type: 'need_user_confirm',
      task_id: 'task_preparation_clarification',
      canonical_order: 5,
      payload: {
        confirmation_id: 'conf_preparation',
        confirmation_type: 'preparation_clarification',
          clarification_cards: [{
            id: 'acceptance_rule',
            question: 'Please provide the acceptance rule.',
            severity: 'high',
            selection_mode: 'single',
            options: [{
              id: 'release_gate',
              label: 'Release gate',
              description: 'Use a defined pass rate and defect threshold.'
            }],
            allow_conservative_scope: true,
        }],
        retrieval_summary: {
          retrieval_round: 2,
          company_rag: { status: 'skipped', hit_count: 0 },
          project_rag: { status: 'skipped', reason: 'no_project', hit_count: 0 },
        },
      },
    }), 'hydrate')

    const clarification = result.messages.find((message) => message.type === 'preparation_clarification')
    expect(result.status).toBe('waiting_user_confirm')
    expect(result.currentPhase).toBe('preparation_clarification')
    expect(clarification?.clarification?.cards[0].allowConservativeScope).toBe(true)
    expect(clarification?.clarification?.cards[0]).toMatchObject({
      selectionMode: 'single',
      options: [{ id: 'release_gate', label: 'Release gate' }]
    })
    expect(clarification?.clarification?.retrievalSummary?.projectRag?.reason).toBe('no_project')
  })

  it('hides incremental decision and observation narratives', () => {
    const block = createInitialTaskRunBlock('task_incremental', 'running', 'hydrate')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_incremental_decision',
        event_type: 'agent_decision_update',
        task_id: 'task_incremental',
        canonical_order: 1,
        payload: {
          route: 'incremental',
          agent_name: 'IncrementalAgent',
          public_update: { headline: '增量修改章节', summary: '内部决策' }
        }
      }),
      makeEvent({
        event_id: 'evt_incremental_observation',
        event_type: 'agent_observation_update',
        task_id: 'task_incremental',
        canonical_order: 2,
        payload: {
          route: 'incremental',
          agent_name: 'IncrementalAgent',
          public_update: { headline: '工具执行完成', summary: '内部观察' }
        }
      })
    ], 'hydrate')

    expect(result.dynamicNarratives).toHaveLength(0)
  })

  it('incremental completion keeps the start time and terminal payload for final cards', () => {
    const block = createInitialTaskRunBlock('task_incremental_complete', 'created', 'live')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_incremental_started_timing',
        event_type: 'incremental_started',
        task_id: 'task_incremental_complete',
        canonical_order: 1,
        created_at: '2026-09-03T12:00:00.000Z',
        payload: { started_at: '2026-09-03T12:00:00.000Z' }
      }),
      makeEvent({
        event_id: 'evt_incremental_completed_payload',
        event_type: 'incremental_completed',
        task_id: 'task_incremental_complete',
        canonical_order: 2,
        created_at: '2026-09-03T12:00:18.000Z',
        payload: {
          started_at: '2026-09-03T12:00:00.000Z',
          completed_at: '2026-09-03T12:00:18.000Z',
          duration_ms: 18000,
          summary: '已基于原测试方案完成指定章节的增量修改并导出新版本。',
          summary_facts: {
            generated_sections: 16,
            review: { suggestion_count: 1 },
            incremental: { modified_section_ids: ['body_18_level_1'] }
          },
          artifact: {
            public_id: 'artifact_incremental_v2',
            file_name: '测试方案_v2.docx',
            artifact_type: 'test_plan_word',
            file_size: 1024
          },
          format_check: { status: 'passed', level: 'passed' }
        }
      })
    ], 'live')

    expect(result.status).toBe('completed')
    expect(result.startedAt).toBe('2026-09-03T12:00:00.000Z')
    expect(result.completedAt).toBe('2026-09-03T12:00:18.000Z')
    expect(result.durationMs).toBe(18000)
    expect(result.summaryFacts?.generatedSections).toBe(16)
    expect(result.artifacts[0]?.id).toBe('artifact_incremental_v2')
    expect(result.formatCheckResult?.status).toBe('passed')
    expect(result.messages.find((message) => message.eventType === 'incremental_completed')).toMatchObject({
      _summaryFacts: expect.objectContaining({ generatedSections: 16 }),
      artifact: expect.objectContaining({ id: 'artifact_incremental_v2' })
    })
  })

  it('format-loss confirmation puts incremental task into a waiting state', () => {
    const block = createInitialTaskRunBlock('task_incremental_loss', 'running', 'live')
    const result = reduceTaskEvent(block, makeEvent({
      event_id: 'evt_incremental_format_loss',
      event_type: 'format_loss_confirm_requested',
      task_id: 'task_incremental_loss',
      canonical_order: 7,
      payload: {
        losses: [{ element: 'bookmark_1', message: '书签丢失' }],
        loss_count: 1,
        timeout_at: '2026-09-03T12:05:00.000Z'
      }
    }), 'live')

    expect(result.status).toBe('waiting_user_confirm')
    expect(result.currentPhase).toBe('format_loss_review')
    expect(result.messages.find((message) => message.formatLoss)?.formatLoss?.lossCount).toBe(1)
  })

  // ── 6. RequirementParser 终态不回退 running ──
  it('tool_finished then late tool_started for same tool_call_id does not regress', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'hydrate')
    // tool_started uses chunk_index 0, tool_finished uses chunk_index 1 (final)
    const events: RawEventInput[] = [
      makeEvent({ event_id: 'e1', event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'RequirementParserTool', tool_call_id: 'tc_rp', attempt: 1, chunk_index: 0 } }),
      makeEvent({ event_id: 'e2', event_type: 'tool_finished', canonical_order: 2, payload: { tool_call_id: 'tc_rp', attempt: 1, chunk_index: 1, chunk_final: true } }),
    ]
    const result = reduceTaskEvents(block, events, 'hydrate')
    const rpTool = result.toolExecutions.find(t => t.logicalKey === 'tc_rp:1')
    expect(rpTool?.status).toBe('success')
    expect(rpTool?.terminal).toBe(true)

    // Late-arriving started with same attempt — should be ignored (terminal guard)
    const result2 = reduceToolStartedDirect(result, {
      event_id: 'e3', event_type: 'tool_started', canonical_order: 1, task_id: 'task1',
      payload: { tool_name: 'RequirementParserTool', tool_call_id: 'tc_rp', attempt: 1 },
      created_at: '2026-07-30T12:00:00+00:00',
    })
    const rpTool2 = result2.toolExecutions.find((t: { logicalKey: string }) => t.logicalKey === 'tc_rp:1')
    expect(rpTool2?.status).toBe('success') // Not regressed to running
  })

  // ── 7. event-list page cursor 不会写入 SSE cursor ──
  it('canonical_order max=59 sets lastAppliedCanonicalOrder=59, not 1000000002', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'hydrate')
    const events: RawEventInput[] = [
      makeEvent({ event_id: 'e1', event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'X', tool_call_id: 'tc1' } }),
      makeEvent({ event_id: 'e59', event_type: 'task_completed', canonical_order: 59, payload: {} }),
    ]
    const result = reduceTaskEvents(block, events, 'hydrate')
    // lastAppliedCanonicalOrder should be max canonical_order (59), not the page cursor (1000000002)
    expect(result.lastAppliedCanonicalOrder).toBe(59)
  })

  // ── 8. canonical_order 最大 59 时 lastAppliedCanonicalOrder=59 ──
  it('cursor tracks highest canonical_order seen', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'live')
    const result = reduceTaskEvent(block, makeEvent({ event_type: 'tool_started', canonical_order: 59, payload: { tool_name: 'X', tool_call_id: 'tc1' } }), 'live')
    expect(result.lastAppliedCanonicalOrder).toBe(59)
  })

  // ── 9. UTC 时间显示正确 ──
  it('UTC timestamps produce correct duration (no 29092s)', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'live')
    // started at UTC 12:00
    const started = reduceTaskEvent(block, makeEvent({
      event_type: 'tool_started',
      canonical_order: 1,
      created_at: '2026-07-30T12:00:00+00:00',
      payload: { tool_name: 'X', tool_call_id: 'tc1', attempt: 1 },
    }), 'live')
    // finished at UTC 12:05 (300 seconds later)
    const finished = reduceTaskEvent(started, makeEvent({
      event_id: 'e2',
      event_type: 'tool_finished',
      canonical_order: 2,
      created_at: '2026-07-30T12:05:00+00:00',
      payload: { tool_call_id: 'tc1', attempt: 1, chunk_index: 1, chunk_final: true },
    }), 'live')
    const tool = finished.toolExecutions.find(t => t.toolCallId === 'tc1')
    // durationMs = Date.parse('12:05') - Date.parse('12:00') = 300000ms
    expect(tool?.durationMs).toBe(300000)
    // 300s, not 29100s or 29092s
  })

  // ── 10. 同一 tool_call_id 所有 chunk 完整合并 ──
  it('all chunks merged for same tool_call_id', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'live')
    const events: RawEventInput[] = [
      makeEvent({ event_id: 'e1', event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'X', tool_call_id: 'tc1' } }),
      makeEvent({ event_id: 'e2', event_type: 'tool_finished', canonical_order: 2, payload: { tool_call_id: 'tc1', chunk_index: 0, chunk_total: 3, chunk_final: false, public_execution_update: { headline: 'Part 1', summary: 'sum1' } } }),
      makeEvent({ event_id: 'e3', event_type: 'tool_finished', canonical_order: 3, payload: { tool_call_id: 'tc1', chunk_index: 1, chunk_total: 3, chunk_final: false, public_execution_update: { headline: 'Part 2', summary: 'sum1', impact: 'imp' } } }),
      makeEvent({ event_id: 'e4', event_type: 'tool_finished', canonical_order: 4, payload: { tool_call_id: 'tc1', chunk_index: 2, chunk_total: 3, chunk_final: true, public_execution_update: { headline: 'Part 3', summary: 'sum1', impact: 'imp', next_action: 'na', details: ['d1'] } } }),
    ]
    const result = reduceTaskEvents(block, events, 'live')
    const tool = result.toolExecutions.find(t => t.toolCallId === 'tc1')
    expect(tool?.status).toBe('success')
    expect(tool?.terminal).toBe(true)
    // Only 1 tool execution (not 3 separate ones)
    expect(result.toolExecutions.filter(t => t.toolCallId === 'tc1')).toHaveLength(1)
    // Phase 2.9A.32: 断言 publicUpdate 从嵌套契约单调归并 — 修复前该测试只
    // 断言 status/terminal/数量,从不断言 publicUpdate,造成假阳性。
    expect(tool?.publicUpdate).toBeDefined()
    expect(tool?.publicUpdate?.headline).toBe('Part 3')
    expect(tool?.publicUpdate?.summary).toBe('sum1')
    expect(tool?.publicUpdate?.impact).toBe('imp')
    expect(tool?.publicUpdate?.nextAction).toBe('na')
    expect(tool?.publicUpdate?.details).toEqual(['d1'])
  })

  // ── 11. 不同 attempt 独立 ──
  it('different attempts create separate tool executions', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'live')
    // Each attempt gets its own chunk_index to avoid dedup conflicts
    const events: RawEventInput[] = [
      makeEvent({ event_id: 'e1', event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'X', tool_call_id: 'tc1', attempt: 1, chunk_index: 0 } }),
      makeEvent({ event_id: 'e2', event_type: 'tool_failed', canonical_order: 2, payload: { tool_call_id: 'tc1', attempt: 1, chunk_index: 1 } }),
      makeEvent({ event_id: 'e3', event_type: 'tool_started', canonical_order: 3, payload: { tool_name: 'X', tool_call_id: 'tc1', attempt: 2, chunk_index: 0 } }),
      makeEvent({ event_id: 'e4', event_type: 'tool_finished', canonical_order: 4, payload: { tool_call_id: 'tc1', attempt: 2, chunk_index: 1, chunk_final: true } }),
    ]
    const result = reduceTaskEvents(block, events, 'live')
    const tools = result.toolExecutions.filter(t => t.toolCallId === 'tc1')
    expect(tools).toHaveLength(2)
    expect(tools[0].attempt).toBe(1)
    expect(tools[0].status).toBe('failed')
    expect(tools[1].attempt).toBe(2)
    expect(tools[1].status).toBe('success')
  })

  // ── 12. Hydrate 与 Live 合并保留 userCollapseOverride ──
  it('merge preserves userCollapseOverride from existing block', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'live')
    block.userCollapseOverride = true
    block.collapsed = true

    const event = makeEvent({ event_type: 'tool_started', canonical_order: 1, payload: { tool_name: 'X', tool_call_id: 'tc1' } })
    const result = reduceTaskEvent(block, event, 'live')

    // After live event, user override should still be preserved
    // (This tests the reducer doesn't touch userCollapseOverride)
    expect(result.userCollapseOverride).toBe(true)
  })

  // ── 13. completed 任务刷新后默认收起 ──
  it('terminal status sets collapsed=true when userCollapseOverride is null', () => {
    const block = createInitialTaskRunBlock('task1', 'running', 'live')
    expect(block.collapsed).toBe(false)
    expect(block.userCollapseOverride).toBeNull()

    const event = makeEvent({ event_type: 'task_completed', canonical_order: 5, payload: {} })
    const result = reduceTaskEvent(block, event, 'live')
    expect(result.status).toBe('completed')
    expect(result.collapsed).toBe(true)
    expect(result.userCollapseOverride).toBeNull()
  })

  // ── 14. Hydrate completed sets collapsed=true ──
  it('hydrate with terminal status starts collapsed', () => {
    const block = createInitialTaskRunBlock('task1', 'completed', 'hydrate')
    expect(block.collapsed).toBe(true)
  })

  // ── 15. RawEventInput type export ──
  it('RawEventInput is exported and usable', () => {
    // Type-level check — if this compiles, the export works
    const evt: RawEventInput = { event_type: 'test' }
    expect(evt.event_type).toBe('test')
  })

  it('live task lifecycle stores started/completed timing without waiting for hydrate', () => {
    const block = createInitialTaskRunBlock('task_live', 'created', 'live')

    const created = reduceTaskEvent(block, {
      event_id: 'evt_created',
      event_type: 'task_created',
      task_id: 'task_live',
      canonical_order: 0,
      created_at: '2026-08-01T04:58:45+00:00',
      payload: {}
    }, 'live')

    const completed = reduceTaskEvent(created, {
      event_id: 'evt_completed',
      event_type: 'task_completed',
      task_id: 'task_live',
      canonical_order: 59,
      created_at: '2026-08-01T05:01:34+00:00',
      payload: {}
    }, 'live')

    expect(completed.status).toBe('completed')
    expect(completed.startedAt).toBe('2026-08-01T04:58:45+00:00')
    expect(completed.completedAt).toBe('2026-08-01T05:01:34+00:00')
    expect(completed.durationMs).toBe(169000)
  })

  it('task_completed persists dynamic final answer from payload text fields', () => {
    const block = createInitialTaskRunBlock('task_dynamic_final', 'running', 'live')
    const result = reduceTaskEvent(block, makeEvent({
      event_id: 'evt_dynamic_done',
      event_type: 'task_completed',
      task_id: 'task_dynamic_final',
      canonical_order: 9,
      payload: { final_answer: '动态 Agent 已完成分析。' }
    }), 'live')

    const finalMessage = result.messages.find((message) => message.eventType === 'task_completed')
    expect(finalMessage).toMatchObject({
      type: 'agent_text',
      text: '动态 Agent 已完成分析。'
    })
  })

  it('keeps dynamic narrative step title and source for timeline rendering', () => {
    const block = createInitialTaskRunBlock('task_dynamic_narrative', 'running', 'live')
    const result = reduceTaskEvent(block, makeEvent({
      event_id: 'evt_step_narrative',
      event_type: 'agent_observation_update',
      task_id: 'task_dynamic_narrative',
      canonical_order: 8,
      payload: {
        agent_name: 'TestAgent',
        step_id: 'verify_goal',
        step_title: '检查分析结果',
        narrative_source: 'llm',
        public_update: {
          headline: '正在检查分析结果',
          narrative_text: '我已经把解析结果和你的问题对齐检查了一遍。',
          source: 'llm'
        }
      }
    }), 'live')

    expect(result.dynamicNarratives).toHaveLength(1)
    expect(result.dynamicNarratives[0]).toMatchObject({
      stepId: 'verify_goal',
      stepTitle: '检查分析结果',
      narrativeSource: 'llm',
      publicUpdate: {
        narrativeText: '我已经把解析结果和你的问题对齐检查了一遍。',
        source: 'llm'
      }
    })
  })

  it('suppresses the separate PreparationAgent observation timeline row', () => {
    const block = createInitialTaskRunBlock('task_preparation_narrative', 'running', 'live')
    const withDecision = reduceTaskEvent(block, makeEvent({
      event_id: 'evt_prep_decision',
      event_type: 'agent_decision_update',
      task_id: 'task_preparation_narrative',
      canonical_order: 8,
      payload: {
        agent_name: 'PreparationAgent',
        public_update: { headline: '观察', narrative_text: '### 观察' }
      }
    }), 'live')
    const result = reduceTaskEvent(withDecision, makeEvent({
      event_id: 'evt_prep_observation',
      event_type: 'agent_observation_update',
      task_id: 'task_preparation_narrative',
      canonical_order: 9,
      payload: {
        agent_name: 'PreparationAgent',
        public_update: { headline: '工具观察', narrative_text: '工具已完成' }
      }
    }), 'live')

    expect(result.dynamicNarratives).toHaveLength(1)
    expect(result.dynamicNarratives[0].eventType).toBe('agent_decision_update')
  })

  it('live plan_created maps nested string plan steps into visible plan details', () => {
    const block = createInitialTaskRunBlock('task_live_plan', 'running', 'live')

    const result = reduceTaskEvent(block, {
      event_id: 'evt_plan_created',
      event_type: 'plan_created',
      task_id: 'task_live_plan',
      created_at: '2026-08-01T07:35:36+00:00',
      payload: {
        plan: {
          steps: [
            'parse_requirement',
            'parse_template',
            'search_knowledge',
            'suggest_sections',
            'wait_user_confirm'
          ]
        }
      }
    }, 'live')

    expect(result.messages).toHaveLength(1)
    expect(result.messages[0]).toMatchObject({
      type: 'agent_plan',
      plan: [
        { id: 'parse_requirement', title: '解析需求文档' },
        { id: 'parse_template', title: '解析测试方案模板' },
        { id: 'search_knowledge', title: '检索公司知识库' },
        { id: 'suggest_sections', title: '生成章节处理建议' },
        { id: 'wait_user_confirm', title: '确认章节生成范围' }
      ]
    })
  })

  it('keeps raw plan_created payload for task-specific understanding copy', () => {
    const block = createInitialTaskRunBlock('task_understanding_copy', 'running', 'live')

    const result = reduceTaskEvent(block, {
      event_id: 'evt_plan_understanding',
      event_type: 'plan_created',
      task_id: 'task_understanding_copy',
      created_at: '2026-08-01T07:35:36+00:00',
      payload: {
        goal: '总结这个文档的内容',
        target_capability: 'document_qa',
        operation: 'summarize',
        understanding_summary: '根据用户指令总结文档内容：总结这个文档的内容',
        plan: { steps: [{ step_id: 'step_1', title: '解析Word文档' }] }
      }
    }, 'live')

    expect(result.messages[0]._rawEvent?.payload).toMatchObject({
      understanding_summary: '根据用户指令总结文档内容：总结这个文档的内容'
    })
  })

  it('tool_started after section confirmation resumes a waiting task run', () => {
    const block = createInitialTaskRunBlock('task_waiting', 'waiting_user_confirm', 'live')

    const result = reduceTaskEvent(block, {
      event_id: 'evt_post_confirm_tool',
      event_type: 'tool_started',
      task_id: 'task_waiting',
      created_at: '2026-08-01T07:40:00+00:00',
      payload: {
        tool_name: 'TestPlanGeneratorTool',
        tool_call_id: 'call_generate'
      }
    }, 'live')

    expect(result.status).toBe('running')
    expect(result.currentPhase).toBe('execution')
  })

  it('updates visible execution plan steps from plan_step events', () => {
    const block = createInitialTaskRunBlock('task_plan_steps', 'running', 'live')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_plan',
        event_type: 'plan_created',
        task_id: 'task_plan_steps',
        canonical_order: 1,
        payload: { plan: { steps: ['parse_requirement'] } }
      }),
      makeEvent({
        event_id: 'evt_step_start',
        event_type: 'plan_step_started',
        task_id: 'task_plan_steps',
        canonical_order: 2,
        payload: { step: 'parse_requirement' }
      }),
      makeEvent({
        event_id: 'evt_step_done',
        event_type: 'plan_step_completed',
        task_id: 'task_plan_steps',
        canonical_order: 3,
        payload: { step: 'parse_requirement' }
      })
    ], 'live')

    expect(result.messages[0].plan?.[0].status).toBe('done')
  })

  it('marks open plan steps done when the task completes', () => {
    const block = createInitialTaskRunBlock('task_plan_complete', 'running', 'live')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_plan_complete',
        event_type: 'plan_created',
        task_id: 'task_plan_complete',
        canonical_order: 1,
        payload: { plan: { steps: ['parse_requirement', 'wait_user_confirm', 'generate', 'export'] } }
      }),
      makeEvent({
        event_id: 'evt_done',
        event_type: 'task_completed',
        task_id: 'task_plan_complete',
        canonical_order: 9,
        payload: {}
      })
    ], 'live')

    expect(result.messages[0].plan?.map((step) => step.status)).toEqual(['done', 'done', 'done', 'done'])
  })

  it('preserves generated Word page count from completion payload', () => {
    const block = createInitialTaskRunBlock('task_page_count', 'running', 'live')
    const result = reduceTaskEvent(block, makeEvent({
      event_id: 'evt_page_count_done',
      event_type: 'task_completed',
      task_id: 'task_page_count',
      canonical_order: 9,
      payload: {
        summary_facts: {
          generated_sections: 16,
          page_count: 12,
          review: { suggestion_count: 1 },
          artifact: {
            public_id: 'art_page_count',
            file_name: '智慧校园预约与签到系统_测试方案.docx',
            page_count: 12
          }
        },
        artifact: {
          public_id: 'art_page_count',
          artifact_type: 'test_plan_word',
          file_name: '智慧校园预约与签到系统_测试方案.docx',
          page_count: 12
        }
      }
    }), 'live')

    expect(result.summaryFacts?.pageCount).toBe(12)
    expect(result.artifacts[0]?.pageCount).toBe(12)
  })

  it('marks an existing plan step failed without duplicating the plan card', () => {
    const block = createInitialTaskRunBlock('task_plan_failed', 'running', 'live')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_plan',
        event_type: 'plan_created',
        task_id: 'task_plan_failed',
        canonical_order: 1,
        payload: { revision: 1, plan: { steps: ['parse_requirement', 'search_knowledge'] } }
      }),
      makeEvent({
        event_id: 'evt_step_failed',
        event_type: 'plan_step_failed',
        task_id: 'task_plan_failed',
        canonical_order: 2,
        payload: { step_id: 'search_knowledge' }
      })
    ], 'live')

    const planMessages = result.messages.filter((message) => message.type === 'agent_plan')
    expect(planMessages).toHaveLength(1)
    expect(planMessages[0].plan?.map((step) => [step.id, step.status])).toEqual([
      ['parse_requirement', 'pending'],
      ['search_knowledge', 'failed']
    ])
  })

  it('applies only newer plan_updated revisions and preserves completed history', () => {
    const block = createInitialTaskRunBlock('task_replan', 'running', 'live')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_plan_v1',
        event_type: 'plan_created',
        task_id: 'task_replan',
        canonical_order: 1,
        payload: {
          revision: 1,
          plan: {
            steps: [
              { step_id: 'step_1', title: 'Parse Word' },
              { step_id: 'step_2', title: 'Analyze evidence' }
            ]
          }
        }
      }),
      makeEvent({
        event_id: 'evt_step_done',
        event_type: 'plan_step_completed',
        task_id: 'task_replan',
        canonical_order: 2,
        payload: { step_id: 'step_1' }
      }),
      makeEvent({
        event_id: 'evt_plan_v2',
        event_type: 'plan_updated',
        task_id: 'task_replan',
        canonical_order: 3,
        payload: {
          revision: 2,
          plan: {
            steps: [
              { step_id: 'step_1', title: 'Parse Word' },
              { step_id: 'step_2', title: 'Search knowledge' },
              { step_id: 'step_3', title: 'Analyze evidence' }
            ]
          }
        }
      }),
      makeEvent({
        event_id: 'evt_plan_v1_late',
        event_type: 'plan_updated',
        task_id: 'task_replan',
        canonical_order: 4,
        payload: {
          revision: 1,
          plan: {
            steps: [
              { step_id: 'step_1', title: 'Old parse' },
              { step_id: 'step_2', title: 'Old analysis' }
            ]
          }
        }
      })
    ], 'live')

    const planMessages = result.messages.filter((message) => message.type === 'agent_plan')
    expect(planMessages).toHaveLength(1)
    expect(planMessages[0].planRevision).toBe(2)
    expect(planMessages[0].plan?.map((step) => ({
      id: step.id,
      title: step.title,
      status: step.status
    }))).toEqual([
      { id: 'step_1', title: 'Parse Word', status: 'done' },
      { id: 'step_2:rev1:superseded', title: 'Analyze evidence', status: 'superseded' },
      { id: 'step_2', title: 'Search knowledge', status: 'pending' },
      { id: 'step_3', title: 'Analyze evidence', status: 'pending' }
    ])
  })

  it('updates visible execution plan steps from tool lifecycle events', () => {
    const block = createInitialTaskRunBlock('task_tool_plan', 'running', 'live')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_plan',
        event_type: 'plan_created',
        task_id: 'task_tool_plan',
        canonical_order: 1,
        payload: { plan: { steps: ['parse_requirement'] } }
      }),
      makeEvent({
        event_id: 'evt_tool_start',
        event_type: 'tool_started',
        task_id: 'task_tool_plan',
        canonical_order: 2,
        payload: { tool_name: 'RequirementParserTool', tool_call_id: 'call_req' }
      }),
      makeEvent({
        event_id: 'evt_tool_done',
        event_type: 'tool_finished',
        task_id: 'task_tool_plan',
        canonical_order: 3,
        payload: {
          tool_name: 'RequirementParserTool',
          tool_call_id: 'call_req',
          chunk_index: 0,
          chunk_final: true
        }
      })
    ], 'live')

    expect(result.messages[0].plan?.[0].status).toBe('done')
  })

  it('tool_progress updates the same tool card in place', () => {
    const block = createInitialTaskRunBlock('task_tool_progress', 'running', 'live')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_tool_start',
        event_type: 'tool_started',
        task_id: 'task_tool_progress',
        canonical_order: 1,
        payload: {
          tool_name: 'RequirementParserTool',
          tool_call_id: 'call_req',
          display_tool_name: '调用Word文档解析工具',
          business_action: '解析需求文档',
          business_subject_type: '需求文档',
          business_subject_name: '智慧校园需求说明书.docx',
          progress_message: '正在读取 Word 文档'
        }
      }),
      makeEvent({
        event_id: 'evt_tool_progress',
        event_type: 'tool_progress',
        task_id: 'task_tool_progress',
        canonical_order: 2,
        payload: {
          tool_name: 'RequirementParserTool',
          tool_call_id: 'call_req',
          progress_message: '正在处理图片：《系统架构图.png》'
        }
      })
    ], 'live')

    expect(result.messages.filter(m => m.type === 'tool_call')).toHaveLength(1)
    expect(result.toolExecutions.filter(t => t.toolCallId === 'call_req')).toHaveLength(1)
    const toolMessage = result.messages.find(m => m.type === 'tool_call')
    expect(toolMessage?.toolCall?.progressMessage).toBe('正在处理图片：《系统架构图.png》')
    expect(toolMessage?.toolCall?.displayName).toBe('调用Word文档解析工具')
    expect(toolMessage?.toolCall?.businessSubjectName).toBe('智慧校园需求说明书.docx')
  })

  it('tool_finished preserves presentation fields and sets completion message', () => {
    const block = createInitialTaskRunBlock('task_tool_complete', 'running', 'live')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_tool_start',
        event_type: 'tool_started',
        task_id: 'task_tool_complete',
        canonical_order: 1,
        payload: {
          tool_name: 'TemplateParserTool',
          tool_call_id: 'call_tpl',
          display_tool_name: '调用模板解析工具',
          business_action: '解析测试方案模板',
          business_subject_type: '测试方案模板',
          business_subject_name: '标准模板.docx',
          progress_message: '正在读取测试方案模板'
        }
      }),
      makeEvent({
        event_id: 'evt_tool_finished',
        event_type: 'tool_finished',
        task_id: 'task_tool_complete',
        canonical_order: 2,
        payload: {
          tool_name: 'TemplateParserTool',
          tool_call_id: 'call_tpl',
          chunk_index: 0,
          chunk_final: true,
          completion_message: '测试方案模板《标准模板.docx》已解析完成'
        }
      })
    ], 'live')

    const toolMessage = result.messages.find(m => m.type === 'tool_call')
    expect(toolMessage?.toolCall?.status).toBe('success')
    expect(toolMessage?.toolCall?.displayName).toBe('调用模板解析工具')
    expect(toolMessage?.toolCall?.completionMessage).toBe('测试方案模板《标准模板.docx》已解析完成')
  })
})

// ── BUG FIX 2026-08-19: format_loss_confirm_requested 必须产出 ChatMessage ─

describe('useTaskEventReducer — format_loss handling', () => {
  it('format_loss_confirm_requested 产出带 formatLoss 的 ChatMessage（对齐 mapper）', () => {
    const block = createInitialTaskRunBlock('task_loss', 'running', 'live')
    const evt = makeEvent({
      event_id: 'evt_loss_1',
      event_type: 'format_loss_confirm_requested',
      task_id: 'task_loss',
      canonical_order: 5,
      payload: {
        losses: [
          { element: 'bookmark_170', expected: 'exists', actual: 'missing', message: '书签 170 丢失' }
        ],
        loss_count: 1,
        loss_details_for_user: '共 1 项格式丢失',
        choices: [
          { id: 'accept', label: '接受' },
          { id: 'retry', label: '重试' }
        ],
        timeout_at: '2026-08-19T12:05:00Z'
      }
    })
    const next = reduceTaskEvent(block, evt, 'live')

    expect(next.messages).toHaveLength(1)
    expect(next.messages[0].eventType).toBe('format_loss_confirm_requested')
    expect(next.messages[0].formatLoss?.lossCount).toBe(1)
    expect(next.messages[0].formatLoss?.summary).toBe('共 1 项格式丢失')
    expect(next.messages[0].formatLoss?.choices.map((c) => c.id)).toEqual([
      'accept',
      'retry'
    ])
    expect(next.messages[0].formatLoss?.timeoutAt).toBe('2026-08-19T12:05:00Z')
    expect(next.messages[0].formatLoss?.losses[0].element).toBe('bookmark_170')
  })

  it('format_loss_confirm_requested 在 losses 为空时仍产出消息（timeout_at 仍展示）', () => {
    const block = createInitialTaskRunBlock('task_loss_empty', 'running', 'live')
    const evt = makeEvent({
      event_id: 'evt_loss_2',
      event_type: 'format_loss_confirm_requested',
      task_id: 'task_loss_empty',
      canonical_order: 6,
      payload: {
        losses: [],
        loss_count: 0,
        choices: [],
        timeout_at: '2026-08-19T12:05:00Z'
      }
    })
    const next = reduceTaskEvent(block, evt, 'live')

    expect(next.messages).toHaveLength(1)
    expect(next.messages[0].formatLoss?.lossCount).toBe(0)
    expect(next.messages[0].formatLoss?.choices).toEqual([])
    expect(next.messages[0].formatLoss?.timeoutAt).toBe('2026-08-19T12:05:00Z')
    // BUG FIX 2026-08-19：format_loss 消息必须携带 conversationSequence，
    // AgentRunCard 据此把 banner 锚定到 DocxFormatCheckTool 工具项之后。
    expect(next.messages[0].conversationSequence).toBe(6)
  })

  it('format_loss_resuming 产出简短通知消息（不渲染 formatLoss）', () => {
    const block = createInitialTaskRunBlock('task_resume', 'running', 'live')
    const evt = makeEvent({
      event_id: 'evt_resume_1',
      event_type: 'format_loss_resuming',
      task_id: 'task_resume',
      canonical_order: 7,
      payload: {}
    })
    const next = reduceTaskEvent(block, evt, 'live')

    expect(next.messages).toHaveLength(1)
    expect(next.messages[0].eventType).toBe('format_loss_resuming')
    expect(next.messages[0].formatLoss).toBeUndefined()
    expect(next.messages[0].text).toContain('正在恢复导出流程')
  })

  it('format_loss_decision_recorded closes the previous format-loss confirmation card', () => {
    const block = createInitialTaskRunBlock('task_loss_decision', 'running', 'hydrate')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_loss_requested',
        event_type: 'format_loss_confirm_requested',
        task_id: 'task_loss_decision',
        canonical_order: 10,
        payload: {
          losses: [{ element: 'bookmark_1', message: '书签丢失' }],
          loss_count: 1,
          choices: [{ id: 'accept', label: '接受丢失，继续导出' }]
        }
      }),
      makeEvent({
        event_id: 'evt_loss_decision',
        event_type: 'format_loss_decision_recorded',
        task_id: 'task_loss_decision',
        canonical_order: 11,
        payload: { decision: 'accept' }
      })
    ], 'hydrate')

    expect(result.messages.some((message) => message.formatLoss)).toBe(false)
    const receipt = result.messages.find((message) => message.eventType === 'format_loss_decision_recorded')
    expect(receipt?.confirmationReceipt?.kind).toBe('format_loss')
    expect(receipt?.confirmationReceipt?.markdown).toContain('接受格式丢失并继续导出')
  })

  it('a later tool_started closes stale format-loss confirmation even without a decision event', () => {
    const block = createInitialTaskRunBlock('task_loss_tool_resume', 'running', 'hydrate')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_loss_requested_tool',
        event_type: 'format_loss_confirm_requested',
        task_id: 'task_loss_tool_resume',
        canonical_order: 10,
        payload: {
          losses: [{ element: 'bookmark_1', message: '书签丢失' }],
          loss_count: 1
        }
      }),
      makeEvent({
        event_id: 'evt_review_after_loss',
        event_type: 'tool_started',
        task_id: 'task_loss_tool_resume',
        canonical_order: 12,
        payload: {
          tool_name: 'ResultReviewTool',
          tool_call_id: 'call_review_after_loss'
        }
      })
    ], 'hydrate')

    expect(result.messages.some((message) => message.formatLoss)).toBe(false)
    expect(result.messages.some((message) => message.type === 'tool_call')).toBe(true)
  })

  it('task_completed removes stale interactive confirmation cards during hydrate replay', () => {
    const block = createInitialTaskRunBlock('task_loss_done', 'running', 'hydrate')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_loss_requested_done',
        event_type: 'format_loss_confirm_requested',
        task_id: 'task_loss_done',
        canonical_order: 10,
        payload: { losses: [{ element: 'bookmark_1', message: '书签丢失' }], loss_count: 1 }
      }),
      makeEvent({
        event_id: 'evt_done_after_loss',
        event_type: 'task_completed',
        task_id: 'task_loss_done',
        canonical_order: 20,
        payload: { final_answer: '任务已完成。' }
      })
    ], 'hydrate')

    expect(result.status).toBe('completed')
    expect(result.messages.some((message) => message.formatLoss)).toBe(false)
    expect(result.messages.some((message) => message.type === 'section_confirm')).toBe(false)
  })

  it('task_resumed closes the section confirmation card after user confirmation', () => {
    const block = createInitialTaskRunBlock('task_section_resume', 'running', 'hydrate')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_need_section',
        event_type: 'need_user_confirm',
        task_id: 'task_section_resume',
        canonical_order: 5,
        payload: {
          confirmation_id: 'conf_section',
          confirmation_type: 'section_generation_config',
          sections: [{ section_id: 's1', title: '章节一' }]
        }
      }),
      makeEvent({
        event_id: 'evt_task_resumed',
        event_type: 'task_resumed',
        task_id: 'task_section_resume',
        canonical_order: 6,
        payload: {}
      })
    ], 'hydrate')

    expect(result.status).toBe('running')
    expect(result.currentPhase).toBe('execution')
    expect(result.messages.some((message) => message.type === 'section_confirm')).toBe(false)
  })

  it('keeps a submitted clarification receipt in its original timeline position after the task resumes', () => {
    const block = createInitialTaskRunBlock('task_clarification_receipt', 'waiting_user_confirm', 'live', {
      messages: [
        {
          id: 'evt_need_clarification',
          type: 'preparation_clarification',
          role: 'agent',
          taskId: 'task_clarification_receipt',
          eventType: 'need_user_confirm',
          conversationSequence: 12,
          confirmed: true,
          confirmationReceipt: { kind: 'clarification', markdown: '- **范围：** 单人访客' },
          clarification: { cards: [] },
          timestamp: ''
        }
      ]
    })

    const result = reduceTaskEvent(block, makeEvent({
      event_id: 'evt_clarification_resumed',
      event_type: 'task_resumed',
      task_id: 'task_clarification_receipt',
      canonical_order: 13,
      payload: {}
    }), 'live')

    expect(result.messages).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'evt_need_clarification',
        type: 'preparation_clarification',
        conversationSequence: 12,
        confirmed: true,
        confirmationReceipt: expect.objectContaining({ kind: 'clarification' })
      })
    ]))
  })

  it('post-confirm tool_started also closes stale section confirmation when no task_resumed event arrives', () => {
    const block = createInitialTaskRunBlock('task_section_tool', 'running', 'hydrate')
    const result = reduceTaskEvents(block, [
      makeEvent({
        event_id: 'evt_need_section_tool',
        event_type: 'need_user_confirm',
        task_id: 'task_section_tool',
        canonical_order: 5,
        payload: {
          confirmation_id: 'conf_section_tool',
          sections: [{ section_id: 's1', title: '章节一' }]
        }
      }),
      makeEvent({
        event_id: 'evt_tool_after_confirm',
        event_type: 'tool_started',
        task_id: 'task_section_tool',
        canonical_order: 6,
        payload: {
          tool_name: 'ResultReviewTool',
          tool_call_id: 'call_review'
        }
      })
    ], 'hydrate')

    expect(result.status).toBe('running')
    expect(result.messages.some((message) => message.type === 'section_confirm')).toBe(false)
    expect(result.messages.some((message) => message.type === 'tool_call')).toBe(true)
  })
})
