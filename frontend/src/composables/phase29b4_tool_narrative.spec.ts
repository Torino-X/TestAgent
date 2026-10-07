/**
 * Phase 2.9B.4 — LLM-first Tool 叙事 reducer 测试。
 *
 * 覆盖:
 *  - started → 创建空 partial;
 *  - delta → 按 field 追加 + chunk 单调去重;
 *  - update → 完整已校验 public_update 覆盖 partial;
 *  - fallback → 确定性 public_update;
 *  - failed → 标记 failed;
 *  - generation reset(新 generationId 覆盖旧 partial);
 *  - Live / Hydrate 深度等价;
 *  - 不进入任务底部(ToolNarrativeState 独立于 messages)。
 */
import { describe, it, expect } from 'vitest'
import {
  reduceTaskEvent,
  reduceTaskEvents,
  createInitialTaskRunBlock,
  type RawEventInput
} from './useTaskEventReducer'

function narrativeEvent(
  eventId: string,
  overrides: {
    event_type?: string
    narrative_id?: string
    generation_id?: string
    generation_no?: number
    source_tool_call_id?: string
    tool_name?: string
    field?: string
    delta?: string
    chunk_index?: number
    public_update?: Record<string, unknown>
    canonical_order?: number
  } = {}
): RawEventInput {
  const eventType = overrides.event_type ?? 'tool_narrative_started'
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: 'task_9b4',
    canonical_order: overrides.canonical_order ?? 40,
    payload: {
      narrative_id: overrides.narrative_id ?? 'nar-1',
      generation_id: overrides.generation_id ?? 'gen-1',
      generation_no: overrides.generation_no ?? 1,
      source_tool_call_id: overrides.source_tool_call_id ?? 'RequirementParserTool-abc',
      tool_name: overrides.tool_name ?? 'RequirementParserTool',
      ...(overrides.field ? { field: overrides.field, delta: overrides.delta, chunk_index: overrides.chunk_index ?? 0 } : {}),
      ...(overrides.public_update ? { public_update: overrides.public_update } : {})
    },
    created_at: '2026-08-02T09:00:00+00:00'
  }
}

describe('Phase 2.9B.4 tool_narrative reducer', () => {
  it('started → 创建空 partial 流式状态', () => {
    const block = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    const result = reduceTaskEvent(block, narrativeEvent('evt_start'), 'live')
    expect(result.toolNarratives).toHaveLength(1)
    const n = result.toolNarratives[0]
    expect(n.narrativeId).toBe('nar-1')
    expect(n.status).toBe('streaming')
    expect(n.sourceToolCallId).toBe('RequirementParserTool-abc')
    // 空 partial: headline/summary 等为空字符串,details 为空数组。
    expect(n.publicUpdate.headline).toBe('')
    expect(n.publicUpdate.summary).toBe('')
    expect(n.publicUpdate.details).toEqual([])
  })

  it('delta → 按 field 追加文本', () => {
    let block = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_start'), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_d1', { event_type: 'tool_narrative_delta', field: 'summary', delta: '已识别需求文档中的', chunk_index: 1 }), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_d2', { event_type: 'tool_narrative_delta', field: 'summary', delta: '主要业务结构。', chunk_index: 2 }), 'live')
    expect(block.toolNarratives[0].publicUpdate.summary).toBe('已识别需求文档中的主要业务结构。')
  })

  it('delta → 重复 chunk 去重(单调)', () => {
    let block = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_start'), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_d1', { event_type: 'tool_narrative_delta', field: 'summary', delta: 'A', chunk_index: 2 }), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_d1b', { event_type: 'tool_narrative_delta', field: 'summary', delta: 'B', chunk_index: 2 }), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_d0', { event_type: 'tool_narrative_delta', field: 'summary', delta: 'C', chunk_index: 0 }), 'live')
    expect(block.toolNarratives[0].publicUpdate.summary).toBe('A')
  })

  it('update → 完整 public_update 覆盖 partial', () => {
    let block = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_start'), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_d1', { event_type: 'tool_narrative_delta', field: 'summary', delta: '旧 partial', chunk_index: 1 }), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_update', {
      event_type: 'tool_narrative_update',
      public_update: {
        headline: '需求文档解析已完成',
        summary: '已识别主要业务结构。',
        impact: '用于确定测试范围。',
        next_action: '接下来解析模板。',
        details: ['识别章节 43 个', '识别表格 7 个']
      }
    }), 'live')
    const n = block.toolNarratives[0]
    expect(n.status).toBe('completed')
    expect(n.source).toBe('llm')
    expect(n.publicUpdate.headline).toBe('需求文档解析已完成')
    expect(n.publicUpdate.summary).toBe('已识别主要业务结构。')
    expect(n.publicUpdate.impact).toBe('用于确定测试范围。')
    expect(n.publicUpdate.nextAction).toBe('接下来解析模板。')
    expect(n.publicUpdate.details).toEqual(['识别章节 43 个', '识别表格 7 个'])
  })

  it('fallback → 确定性 public_update + source=deterministic', () => {
    let block = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_start'), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_fb', {
      event_type: 'tool_narrative_fallback',
      public_update: { headline: '工具执行完成', summary: '确定性回退', impact: 'i', next_action: 'n', details: [] }
    }), 'live')
    const n = block.toolNarratives[0]
    expect(n.status).toBe('fallback')
    expect(n.source).toBe('deterministic')
    expect(n.publicUpdate.headline).toBe('工具执行完成')
  })

  it('failed → 标记 failed(保留已有 partial)', () => {
    let block = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_start'), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_fail', { event_type: 'tool_narrative_failed' }), 'live')
    expect(block.toolNarratives[0].status).toBe('failed')
  })

  it('generation reset → 新 generationId 覆盖旧 partial', () => {
    let block = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_start'), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_d1', { event_type: 'tool_narrative_delta', field: 'summary', delta: '旧文本', chunk_index: 1 }), 'live')
    // 新 generation started(服务重启后 generation_no=2)。
    block = reduceTaskEvent(block, narrativeEvent('evt_start2', { generation_id: 'gen-2', generation_no: 2 }), 'live')
    const n = block.toolNarratives[0]
    expect(n.generationId).toBe('gen-2')
    expect(n.generationNo).toBe(2)
    // 旧 partial 已清除(started 重新初始化空 partial)。
    expect(n.publicUpdate.headline).toBe('')
    expect(n.publicUpdate.summary).toBe('')
    expect(block.toolNarratives).toHaveLength(1)
  })

  it('Live 与 Hydrate 深度等价', () => {
    const events: RawEventInput[] = [
      narrativeEvent('evt_start', { canonical_order: 1 }),
      narrativeEvent('evt_d1', { event_type: 'tool_narrative_delta', field: 'summary', delta: '识别', chunk_index: 1, canonical_order: 2 }),
      narrativeEvent('evt_update', {
        event_type: 'tool_narrative_update',
        canonical_order: 3,
        public_update: { headline: 'H', summary: '识别结构', impact: 'I', next_action: 'N', details: ['a', 'b'] }
      })
    ]
    let liveBlock = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    for (const evt of events) liveBlock = reduceTaskEvent(liveBlock, evt, 'live')
    const hydrateBlock = reduceTaskEvents(
      createInitialTaskRunBlock('task_9b4', 'running', 'hydrate'),
      events,
      'hydrate'
    )
    expect(hydrateBlock.toolNarratives).toHaveLength(1)
    expect(liveBlock.toolNarratives).toEqual(hydrateBlock.toolNarratives)
  })

  it('叙事不进入 messages(不推任务底部)', () => {
    let block = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_start'), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_update', {
      event_type: 'tool_narrative_update',
      public_update: { headline: 'H', summary: 'S', impact: 'I', next_action: 'N', details: [] }
    }), 'live')
    expect(block.messages).toHaveLength(0)
    expect(block.dynamicNarratives).toHaveLength(0)
    expect(block.toolNarratives).toHaveLength(1)
  })

  it('narrative_text delta builds a visible streaming update immediately', () => {
    let block = createInitialTaskRunBlock('task_9b4', 'running', 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_start'), 'live')
    block = reduceTaskEvent(block, narrativeEvent('evt_d_text', {
      event_type: 'tool_narrative_delta',
      field: 'narrative_text',
      delta: '我刚读完需求解析结果',
      chunk_index: 1
    }), 'live')

    const update = block.toolNarratives[0].publicUpdate
    expect(block.toolNarratives[0].status).toBe('streaming')
    expect(update.narrativeText).toBe('我刚读完需求解析结果')
    expect(update.headline).toBe('我刚读完需求解析结果')
    expect(update.chunkFinal).toBe(false)
  })
})
