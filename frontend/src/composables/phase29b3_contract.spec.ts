/**
 * Phase 2.9B.3 — details 类型兼容 + 完整动态叙事 reducer 测试。
 *
 * 覆盖:
 *  - 历史 details dict → string[] 兼容(Canonical Parser);
 *  - details null / 非法 → [];
 *  - details string[] 原样清洗;
 *  - 完整五字段动态叙事经 reducer 保留全部字段;
 *  - Live / Hydrate 深度等价;
 *  - 2.9A 固定叙事不回归。
 */
import { describe, it, expect } from 'vitest'
import { extractPublicExecutionUpdate } from '../utils/eventPayload'
import {
  reduceTaskEvent,
  reduceTaskEvents,
  createInitialTaskRunBlock,
  type RawEventInput
} from './useTaskEventReducer'

function fullNarrativeEvent(
  eventId: string,
  details: unknown = ['字段: payment_callback']
): RawEventInput {
  return {
    event_id: eventId,
    event_type: 'agent_decision_update',
    task_id: 'task_9b3',
    canonical_order: 40,
    payload: {
      update_kind: 'agent_decision',
      agent_name: 'PreparationAgent',
      decision_id: 't:preparation:0',
      step_index: 0,
      action: 'finish',
      route: 'finish',
      public_update: {
        headline: '正在检索知识库',
        summary: '模板字段需要知识库确认。',
        impact: '检索结果决定章节策略。',
        next_action: '调用 KnowledgeSearchTool。',
        details
      }
    },
    created_at: '2026-08-02T08:00:00+00:00'
  }
}

describe('Phase 2.9B.3 details 类型兼容', () => {
  it('历史 dict details → string[]', () => {
    const pu = extractPublicExecutionUpdate({
      public_update: {
        headline: 'h',
        summary: 's',
        impact: 'i',
        next_action: 'n',
        details: { 章节数: 43, 表格数: 3 }
      }
    })
    expect(pu).toBeDefined()
    expect(Array.isArray(pu?.details)).toBe(true)
    expect(pu?.details).toContain('章节数: 43')
    expect(pu?.details).toContain('表格数: 3')
  })

  it('details null → []', () => {
    const pu = extractPublicExecutionUpdate({
      public_update: {
        headline: 'h',
        summary: 's',
        impact: 'i',
        next_action: 'n',
        details: null
      }
    })
    expect(pu?.details).toEqual([])
  })

  it('details 非法值 → []', () => {
    const pu = extractPublicExecutionUpdate({
      public_update: {
        headline: 'h',
        summary: 's',
        impact: 'i',
        next_action: 'n',
        details: 'not-an-array'
      }
    })
    expect(pu?.details).toEqual([])
  })

  it('details string[] 原样清洗(trim/删空/限 5 项)', () => {
    const pu = extractPublicExecutionUpdate({
      public_update: {
        headline: 'h',
        summary: 's',
        impact: 'i',
        next_action: 'n',
        details: [' a ', '  ', '', 'b', 'c', 'd', 'e', 'f']
      }
    })
    expect(pu?.details).toEqual(['a', 'b', 'c', 'd', 'e'])
  })
})

describe('Phase 2.9B.3 完整动态叙事 reducer', () => {
  it('完整五字段叙事经 reducer 保留全部字段', () => {
    const block = createInitialTaskRunBlock('task_9b3', 'running', 'live')
    const result = reduceTaskEvent(block, fullNarrativeEvent('evt_full'), 'live')
    expect(result.dynamicNarratives).toHaveLength(1)
    const n = result.dynamicNarratives[0]
    expect(n.publicUpdate.headline).toBe('正在检索知识库')
    expect(n.publicUpdate.summary).toBe('模板字段需要知识库确认。')
    expect(n.publicUpdate.impact).toBe('检索结果决定章节策略。')
    expect(n.publicUpdate.nextAction).toBe('调用 KnowledgeSearchTool。')
    expect(n.publicUpdate.details).toEqual(['字段: payment_callback'])
  })

  it('Live 与 Hydrate 深度等价(完整叙事)', () => {
    const events: RawEventInput[] = [
      fullNarrativeEvent('evt_a'),
      {
        event_id: 'evt_b',
        event_type: 'agent_observation_update',
        task_id: 'task_9b3',
        canonical_order: 41,
        payload: {
          update_kind: 'agent_observation',
          agent_name: 'PreparationAgent',
          decision_id: 't:preparation:obs',
          step_index: 1,
          action: 'observe',
          route: 'continue',
          public_update: {
            headline: '知识库检索完成',
            summary: '命中 3 条规则。',
            impact: '信息充分。',
            next_action: '进入下一步。',
            details: ['规则数: 3']
          }
        },
        created_at: '2026-08-02T08:01:00+00:00'
      }
    ]
    let liveBlock = createInitialTaskRunBlock('task_9b3', 'running', 'live')
    for (const evt of events) liveBlock = reduceTaskEvent(liveBlock, evt, 'live')
    const hydrateBlock = reduceTaskEvents(
      createInitialTaskRunBlock('task_9b3', 'running', 'hydrate'),
      events,
      'hydrate'
    )
    expect(hydrateBlock.dynamicNarratives).toHaveLength(2)
    // sourceMode 标注来源(live/hydrate),业务字段应深度等价。
    function comparable(n: { eventType: string; agentName: string; decisionId: string; stepIndex: number; canonicalOrder?: number; publicUpdate: unknown }) {
      return {
        eventType: n.eventType,
        agentName: n.agentName,
        decisionId: n.decisionId,
        stepIndex: n.stepIndex,
        canonicalOrder: n.canonicalOrder,
        publicUpdate: n.publicUpdate
      }
    }
    expect(liveBlock.dynamicNarratives.map(comparable)).toEqual(
      hydrateBlock.dynamicNarratives.map(comparable)
    )
  })

  it('2.9A Tool 固定叙事不受影响', () => {
    const block = createInitialTaskRunBlock('task_9b3', 'running', 'live')
    const result = reduceTaskEvents(block, [
      {
        event_id: 'evt_tool',
        event_type: 'tool_finished',
        task_id: 'task_9b3',
        canonical_order: 30,
        payload: {
          tool_name: 'RequirementParserTool',
          tool_call_id: 'tc1',
          chunk_index: 0,
          chunk_final: true,
          headline: '需求文档解析完成',
          summary: '识别章节结构。',
          impact: '确定测试范围。',
          next_action: '解析模板。',
          details: ['章节数: 43']
        }
      }
    ], 'live')
    expect(result.toolExecutions).toHaveLength(1)
    expect(result.toolExecutions[0].publicUpdate?.headline).toBe('需求文档解析完成')
    expect(result.dynamicNarratives).toHaveLength(0)
  })
})
