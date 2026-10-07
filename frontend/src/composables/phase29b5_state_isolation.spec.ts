/**
 * Phase 2.9B.5 — 任务状态与 Tool 状态隔离 + 真实 KnowledgeSearch 降级场景。
 *
 * 覆盖:
 *  - completed Task + 历史 failed Tool(two tool_call_id)→ completed;
 *  - tool_narrative_failed / task_summary_narrative_failed 不改变 Task 状态;
 *  - task_failed 才改变 Task 状态;
 *  - 同名 Tool 不同 tool_call_id 独立(不串卡);
 *  - Live 与 Hydrate 结果一致;
 *  - 真实 task_05d8a139 核心事件顺序回归。
 */
import { describe, expect, it } from 'vitest'
import {
  reduceTaskEvent,
  reduceTaskEvents,
  createInitialTaskRunBlock,
  type RawEventInput
} from './useTaskEventReducer'

function evt(eventId: string, eventType: string, overrides: Record<string, unknown> = {}): RawEventInput {
  return {
    event_id: eventId,
    event_type: eventType,
    task_id: overrides.task_id as string ?? 'task_05d8a139',
    canonical_order: (overrides.canonical_order as number | undefined) ?? 1,
    payload: overrides.payload ?? {},
    created_at: '2026-08-02T09:00:00+00:00'
  }
}

function toolStarted(callId: string, name: string, order: number, attempt = 1): RawEventInput {
  return evt(`st_${callId}`, 'tool_started', {
    canonical_order: order,
    payload: { tool_name: name, tool_call_id: callId, attempt, display_input: 'x' }
  })
}

function toolFinished(callId: string, name: string, order: number, attempt = 1): RawEventInput {
  return evt(`fin_${callId}`, 'tool_finished', {
    canonical_order: order,
    payload: { tool_name: name, tool_call_id: callId, attempt, chunk_final: true, chunk_index: 0 }
  })
}

function toolFailed(callId: string, name: string, order: number, attempt = 1): RawEventInput {
  return evt(`fail_${callId}`, 'tool_failed', {
    canonical_order: order,
    payload: { tool_name: name, tool_call_id: callId, attempt, error: { code: 'KNOWLEDGE_NOT_CONFIGURED' } }
  })
}

function narrativeStarted(order: number, narrativeId = 'nar-1'): RawEventInput {
  return evt(`nst_${order}`, 'tool_narrative_started', {
    canonical_order: order,
    payload: {
      narrative_id: narrativeId, generation_id: 'gen-1', generation_no: 1,
      source_tool_call_id: 'RequirementParserTool-task_05d8a139',
      tool_name: 'RequirementParserTool', attempt: 1, schema_version: 1,
      narrative_source: 'llm', narrative_kind: 'tool'
    }
  })
}

function narrativeFailed(order: number, narrativeId = 'nar-1'): RawEventInput {
  return evt(`nf_${order}`, 'tool_narrative_failed', {
    canonical_order: order,
    payload: {
      narrative_id: narrativeId, generation_id: 'gen-1', generation_no: 1,
      source_tool_call_id: 'RequirementParserTool-task_05d8a139',
      tool_name: 'RequirementParserTool', attempt: 1,
      schema_version: 1, narrative_source: 'llm', narrative_kind: 'tool',
      failure_category: 'schema_validation_failed'
    }
  })
}

function taskCompleted(order: number): RawEventInput {
  return evt('tc', 'task_completed', {
    canonical_order: order,
    payload: {
      artifact: { public_id: 'art_1', file_name: '报销审批_测试方案.docx', file_size: 48936 },
      summary_facts: { generated_sections: 8 }
    }
  })
}

function taskFailed(order: number): RawEventInput {
  return evt('tf', 'task_failed', {
    canonical_order: order,
    payload: { error: '任务失败' }
  })
}

describe('Phase 2.9B.5 任务/Tool 状态隔离', () => {
  it('completed Task + 历史 failed Tool(两个 tool_call_id)→ completed', () => {
    // 真实 task_05d8a139 顺序: KnowledgeSearchTool call A failed → call B success → completed。
    const events: RawEventInput[] = [
      toolStarted('KnowledgeSearchTool-4b3988f0', 'KnowledgeSearchTool', 31),
      toolFailed('KnowledgeSearchTool-4b3988f0', 'KnowledgeSearchTool', 32),
      toolStarted('KnowledgeSearchTool-269700b3', 'KnowledgeSearchTool', 36),
      toolFinished('KnowledgeSearchTool-269700b3', 'KnowledgeSearchTool', 37),
      taskCompleted(50),
    ]
    let block = createInitialTaskRunBlock('task_05d8a139', 'running', 'live')
    for (const e of events) block = reduceTaskEvent(block, e, 'live')

    // 任务终态 completed。
    expect(block.status).toBe('completed')
    // 两个 tool_call_id 各自独立:一个 failed,一个 success。
    const failed = block.toolExecutions.find(t => t.logicalKey.startsWith('KnowledgeSearchTool-4b3988f0'))
    const success = block.toolExecutions.find(t => t.logicalKey.startsWith('KnowledgeSearchTool-269700b3'))
    expect(failed?.status).toBe('failed')
    expect(success?.status).toBe('success')
    // 失败 Tool 的卡片仍在(failed 信息不抹除)。
    expect(block.toolExecutions.filter(t => t.status === 'failed')).toHaveLength(1)
  })

  it('running Task + 失败 Tool(可降级)→ 任务保持 running', () => {
    const events: RawEventInput[] = [
      toolStarted('KnowledgeSearchTool-4b3988f0', 'KnowledgeSearchTool', 31),
      toolFailed('KnowledgeSearchTool-4b3988f0', 'KnowledgeSearchTool', 32),
      toolStarted('KnowledgeSearchTool-269700b3', 'KnowledgeSearchTool', 36),
    ]
    let block = createInitialTaskRunBlock('task_05d8a139', 'running', 'live')
    for (const e of events) block = reduceTaskEvent(block, e, 'live')
    // 局部 tool_failed 不把任务改为 failed。
    expect(block.status).toBe('running')
  })

  it('waiting Task + 失败 Tool(迟到 tool_failed)→ 任务保持等待确认', () => {
    // tool_failed 只更新 ToolExecution,不触碰任务状态;等待中的任务保持等待。
    let block = createInitialTaskRunBlock('task_05d8a139', 'waiting_user_confirm', 'live')
    block = reduceTaskEvent(block, toolFailed('K-tc1', 'KnowledgeSearchTool', 32), 'live')
    expect(block.status).toBe('waiting_user_confirm')
    expect(block.toolExecutions.some(t => t.status === 'failed')).toBe(true)
  })

  it('task_failed 才把任务改为 failed', () => {
    let block = createInitialTaskRunBlock('task_05d8a139', 'running', 'live')
    block = reduceTaskEvent(block, toolStarted('K-tc1', 'KnowledgeSearchTool', 31), 'live')
    block = reduceTaskEvent(block, toolFailed('K-tc1', 'KnowledgeSearchTool', 32), 'live')
    block = reduceTaskEvent(block, taskFailed(50), 'live')
    expect(block.status).toBe('failed')
  })

  it('tool_narrative_failed 不改变任务状态', () => {
    let block = createInitialTaskRunBlock('task_05d8a139', 'running', 'live')
    block = reduceTaskEvent(block, narrativeStarted(13), 'live')
    block = reduceTaskEvent(block, narrativeFailed(21), 'live')
    // 叙事失败只影响叙事,任务保持 running。
    expect(block.status).toBe('running')
    expect(block.toolNarratives[0].status).toBe('failed')
  })

  it('task_summary_narrative_failed 不改变任务状态', () => {
    let block = createInitialTaskRunBlock('task_05d8a139', 'running', 'live')
    const tsumFail = evt('tsf', 'task_summary_narrative_failed', {
      canonical_order: 80,
      payload: { narrative_id: 'tsum_1', generation_id: 'tg1', generation_no: 1, failure_category: 'schema_validation_failed' }
    })
    block = reduceTaskEvent(block, tsumFail, 'live')
    expect(block.status).toBe('running')
  })

  it('同名 Tool 不同 tool_call_id 独立(不串卡)', () => {
    const events: RawEventInput[] = [
      toolStarted('KnowledgeSearchTool-4b3988f0', 'KnowledgeSearchTool', 31),
      toolFailed('KnowledgeSearchTool-4b3988f0', 'KnowledgeSearchTool', 32),
      toolStarted('KnowledgeSearchTool-269700b3', 'KnowledgeSearchTool', 36),
      toolFinished('KnowledgeSearchTool-269700b3', 'KnowledgeSearchTool', 37),
    ]
    let block = createInitialTaskRunBlock('task_05d8a139', 'running', 'live')
    for (const e of events) block = reduceTaskEvent(block, e, 'live')
    expect(block.toolExecutions).toHaveLength(2)
    const statuses = new Set(block.toolExecutions.map(t => t.status))
    expect(statuses.has('failed')).toBe(true)
    expect(statuses.has('success')).toBe(true)
  })

  it('Live 与 Hydrate 结果一致', () => {
    const events: RawEventInput[] = [
      toolStarted('KnowledgeSearchTool-4b3988f0', 'KnowledgeSearchTool', 31),
      toolFailed('KnowledgeSearchTool-4b3988f0', 'KnowledgeSearchTool', 32),
      toolStarted('KnowledgeSearchTool-269700b3', 'KnowledgeSearchTool', 36),
      toolFinished('KnowledgeSearchTool-269700b3', 'KnowledgeSearchTool', 37),
      taskCompleted(50),
    ]
    let live = createInitialTaskRunBlock('task_05d8a139', 'running', 'live')
    for (const e of events) live = reduceTaskEvent(live, e, 'live')
    const hydrate = reduceTaskEvents(
      createInitialTaskRunBlock('task_05d8a139', 'running', 'hydrate'),
      events,
      'hydrate'
    )
    expect(hydrate.status).toBe('completed')
    expect(live.toolExecutions).toEqual(hydrate.toolExecutions)
    expect(live.status).toBe(hydrate.status)
  })
})
