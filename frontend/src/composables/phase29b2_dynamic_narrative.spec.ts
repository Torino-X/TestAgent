/**
 * Phase 2.9B.2 — 动态叙事 reducer 测试。
 *
 * 覆盖:
 *  - agent_decision_update / agent_observation_update 不再被 Ignored;
 *  - 生成 DynamicNarrative 节点,不创建 ToolExecution,不修改 Task 状态;
 *  - public_update 嵌套契约完整解析(headline/summary/impact/nextAction/details);
 *  - 稳定身份 dynamic-narrative:{task_id}:{event_id};
 *  - Live / Hydrate 深度等价;
 *  - 去重:同 event_id、DB replay + Live 重叠、同 headline 不同 event_id、
 *    同 decision_id 的 decision 与 observation、同 dedupe_key 不同类型;
 *  - 排序:canonical_order / sequence_no 稳定顺序;
 *  - 无有效 headline 不生成空节点。
 */
import { describe, it, expect } from 'vitest'
import {
  reduceTaskEvent,
  reduceTaskEvents,
  createInitialTaskRunBlock,
  type RawEventInput
} from './useTaskEventReducer'

function decisionEvent(
  eventId: string,
  overrides: {
    canonical_order?: number
    sequence_no?: number | null
    agent_name?: string
    decision_id?: string
    step_index?: number
    action?: string
    tool_name?: string | null
    public_update?: Record<string, unknown>
    event_type?: 'agent_decision_update' | 'agent_observation_update'
    payload?: Record<string, unknown>
  } = {}
): RawEventInput {
  return {
    event_id: eventId,
    event_type: overrides.event_type ?? 'agent_decision_update',
    task_id: 'task_9b',
    canonical_order: overrides.canonical_order ?? 100,
    sequence_no: overrides.sequence_no ?? null,
    payload: {
      update_kind: 'agent_decision',
      agent_name: overrides.agent_name ?? 'PreparationAgent',
      decision_id: overrides.decision_id ?? 't:preparation:0',
      step_index: overrides.step_index ?? 0,
      action: overrides.action ?? 'finish',
      tool_name: overrides.tool_name ?? null,
      route: 'finish',
      public_update: overrides.public_update ?? {
        headline: '准备阶段决策',
        summary: '决定继续生成测试方案',
        impact: '影响后续章节生成',
        next_action: '生成章节建议',
        details: ['基于解析结果']
      },
      ...overrides.payload
    },
    created_at: '2026-08-02T06:00:00+00:00'
  }
}

function observationEvent(
  eventId: string,
  overrides: {
    canonical_order?: number
    agent_name?: string
    decision_id?: string
    step_index?: number
    tool_name?: string
    public_update?: Record<string, unknown>
  } = {}
): RawEventInput {
  return decisionEvent(eventId, {
    ...overrides,
    event_type: 'agent_observation_update',
    action: 'observe',
    tool_name: overrides.tool_name ?? 'KnowledgeSearchTool',
    public_update: overrides.public_update ?? {
      headline: '知识库检索完成',
      summary: '检索到 2 条相关知识',
      impact: '可继续生成章节建议',
      next_action: '生成章节建议',
      details: ['hit_count: 2']
    }
  })
}

describe('Phase 2.9B.2 动态叙事 reducer', () => {
  it('agent_decision_update 生成 DynamicNarrative, 不创建 ToolExecution, 不改 Task 状态', () => {
    const block = createInitialTaskRunBlock('task_9b', 'running', 'live')
    const result = reduceTaskEvent(block, decisionEvent('evt_dec_1'), 'live')

    expect(result.dynamicNarratives).toHaveLength(1)
    const n = result.dynamicNarratives[0]
    expect(n.id).toBe('dynamic-narrative:task_9b:evt_dec_1')
    expect(n.sourceEventId).toBe('evt_dec_1')
    expect(n.eventType).toBe('agent_decision_update')
    expect(n.agentName).toBe('PreparationAgent')
    expect(n.decisionId).toBe('t:preparation:0')
    expect(n.stepIndex).toBe(0)
    expect(n.publicUpdate.headline).toBe('准备阶段决策')
    expect(n.publicUpdate.summary).toBe('决定继续生成测试方案')
    expect(n.publicUpdate.impact).toBe('影响后续章节生成')
    expect(n.publicUpdate.nextAction).toBe('生成章节建议')
    expect(n.publicUpdate.details).toEqual(['基于解析结果'])

    // 不创建 ToolExecution / 不修改 Task 状态 / 不生成 chat message。
    expect(result.toolExecutions).toHaveLength(0)
    expect(result.status).toBe('running')
    expect(result.messages).toHaveLength(0)
  })

  it('agent_observation_update 生成 observation 节点', () => {
    const block = createInitialTaskRunBlock('task_9b', 'running', 'live')
    const result = reduceTaskEvent(block, observationEvent('evt_obs_1'), 'live')
    expect(result.dynamicNarratives).toHaveLength(1)
    const n = result.dynamicNarratives[0]
    expect(n.eventType).toBe('agent_observation_update')
    expect(n.publicUpdate.headline).toBe('知识库检索完成')
    expect(n.toolName).toBe('KnowledgeSearchTool')
  })

  it('无有效 headline 不生成空节点, 但事件仍被追踪', () => {
    const block = createInitialTaskRunBlock('task_9b', 'running', 'live')
    const result = reduceTaskEvent(
      block,
      decisionEvent('evt_empty', { public_update: { summary: '无 headline' } }),
      'live'
    )
    expect(result.dynamicNarratives).toHaveLength(0)
    // 事件仍被 seenEventIds 追踪, 不会重复处理。
    const second = reduceTaskEvent(
      result,
      decisionEvent('evt_empty', { public_update: { summary: '无 headline' } }),
      'live'
    )
    expect(second.dynamicNarratives).toHaveLength(0)
  })

  it('相同 event_id 重复到达只生成一个节点(事件级去重)', () => {
    const block = createInitialTaskRunBlock('task_9b', 'running', 'live')
    const once = reduceTaskEvent(block, decisionEvent('evt_dup'), 'live')
    const twice = reduceTaskEvent(once, decisionEvent('evt_dup'), 'live')
    expect(twice.dynamicNarratives).toHaveLength(1)
  })

  it('相同 headline 不同 event_id 生成两个独立节点(不得按 headline 去重)', () => {
    const block = createInitialTaskRunBlock('task_9b', 'running', 'live')
    const a = reduceTaskEvent(block, decisionEvent('evt_a', { public_update: { headline: '同标题', summary: 'A' } }), 'live')
    const b = reduceTaskEvent(a, decisionEvent('evt_b', { public_update: { headline: '同标题', summary: 'B' } }), 'live')
    expect(b.dynamicNarratives).toHaveLength(2)
  })

  it('相同 decision_id 的 decision 与 observation 是两条独立节点(不得合并)', () => {
    const block = createInitialTaskRunBlock('task_9b', 'running', 'live')
    const dec = reduceTaskEvent(
      block,
      decisionEvent('evt_dec', { decision_id: 'shared', step_index: 1 }),
      'live'
    )
    const obs = reduceTaskEvent(
      dec,
      observationEvent('evt_obs', { decision_id: 'shared', step_index: 1 }),
      'live'
    )
    expect(obs.dynamicNarratives).toHaveLength(2)
    expect(obs.dynamicNarratives.map((n) => n.eventType).sort()).toEqual([
      'agent_decision_update',
      'agent_observation_update'
    ])
  })

  it('同 dedupe_key 不同 event type 不得合并', () => {
    const block = createInitialTaskRunBlock('task_9b', 'running', 'live')
    const dec = reduceTaskEvent(
      block,
      decisionEvent('evt_dk1', { public_update: { headline: 'H', dedupe_key: 'same-key' } }),
      'live'
    )
    const obs = reduceTaskEvent(
      dec,
      observationEvent('evt_dk2', { public_update: { headline: 'O', dedupe_key: 'same-key' } }),
      'live'
    )
    expect(obs.dynamicNarratives).toHaveLength(2)
  })

  it('Live 与 Hydrate 深度等价', () => {
    const events: RawEventInput[] = [
      decisionEvent('evt_dec', { canonical_order: 50 }),
      observationEvent('evt_obs', { canonical_order: 60 }),
      decisionEvent('evt_dec2', { canonical_order: 70, agent_name: 'RepairAgent', decision_id: 't:repair:0' })
    ]
    let liveBlock = createInitialTaskRunBlock('task_9b', 'running', 'live')
    for (const evt of events) liveBlock = reduceTaskEvent(liveBlock, evt, 'live')

    const hydrateBlock = reduceTaskEvents(
      createInitialTaskRunBlock('task_9b', 'running', 'hydrate'),
      events,
      'hydrate'
    )

    function comparable(n: { id: string; eventType: string; canonicalOrder?: number; agentName: string; decisionId: string; stepIndex: number; publicUpdate: unknown }) {
      return {
        id: n.id,
        eventType: n.eventType,
        canonicalOrder: n.canonicalOrder,
        agentName: n.agentName,
        decisionId: n.decisionId,
        stepIndex: n.stepIndex,
        publicUpdate: n.publicUpdate
      }
    }
    expect(hydrateBlock.dynamicNarratives).toHaveLength(3)
    expect(liveBlock.dynamicNarratives.map(comparable)).toEqual(
      hydrateBlock.dynamicNarratives.map(comparable)
    )
  })

  it('Hydrate 不覆盖 Live 中已有的同 event_id 节点', () => {
    // Live 先收到;再 hydrate 相同 event_id 不得重复。
    let liveBlock = createInitialTaskRunBlock('task_9b', 'running', 'live')
    liveBlock = reduceTaskEvent(liveBlock, decisionEvent('evt_live'), 'live')
    const hydrateEvents: RawEventInput[] = [decisionEvent('evt_live')]
    const merged = reduceTaskEvents(liveBlock, hydrateEvents, 'hydrate')
    expect(merged.dynamicNarratives).toHaveLength(1)
  })

  it('无动态事件时 dynamicNarratives 为空, 不产生额外节点', () => {
    const block = createInitialTaskRunBlock('task_9b', 'completed', 'hydrate')
    const result = reduceTaskEvents(block, [
      {
        event_id: 'evt_tool',
        event_type: 'tool_finished',
        task_id: 'task_9b',
        canonical_order: 10,
        payload: { tool_name: 'TemplateParserTool', tool_call_id: 'tc1', chunk_final: true, headline: '模板解析完成' }
      }
    ], 'hydrate')
    expect(result.dynamicNarratives).toHaveLength(0)
    // 2.9A Tool 叙事仍不受影响。
    expect(result.toolExecutions).toHaveLength(1)
  })
})

describe('Phase 2.9B.2 动态叙事排序', () => {
  it('canonical_order 决定节点顺序(不按 push 顺序)', () => {
    const block = createInitialTaskRunBlock('task_9b', 'running', 'live')
    // 乱序到达。
    const result = reduceTaskEvents(block, [
      decisionEvent('evt_late', { canonical_order: 90 }),
      decisionEvent('evt_early', { canonical_order: 30 }),
      decisionEvent('evt_mid', { canonical_order: 60 })
    ], 'live')
    expect(result.dynamicNarratives.map((n) => n.canonicalOrder)).toEqual([30, 60, 90])
  })

  it('sequence_no 缺失时回落 canonical_order, 均缺则按 sourceEventId 稳定兜底', () => {
    const block = createInitialTaskRunBlock('task_9b', 'running', 'live')
    const result = reduceTaskEvents(block, [
      decisionEvent('evt_b', { sequence_no: null, canonical_order: 5 }),
      decisionEvent('evt_a', { sequence_no: null, canonical_order: 5 })
    ], 'live')
    expect(result.dynamicNarratives.map((n) => n.sourceEventId)).toEqual(['evt_a', 'evt_b'])
  })
})
