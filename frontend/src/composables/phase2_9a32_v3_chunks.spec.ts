/**
 * Phase 2.9A.32 — 真实 v3 扁平五帧分帧归并测试。
 *
 * 基于 task_8716a97a / RequirementParserTool / TemplateParserTool 的
 * 真实后端 builder(split_into_chunks)契约:
 *  - tool_started 不携带 chunk_index(见 test_agent_tool_adapter
 *    _emit_tool_started,payload 只有 tool_name/tool_call_id/attempt/
 *    display_input/input/input_keys)。
 *  - tool_finished 5 帧为「累计快照」:
 *    frame0: headline
 *    frame1: +summary
 *    frame2: +impact
 *    frame3: +next_action
 *    frame4: +details + chunk_final=true
 *  - payload 扁平,字段在顶层。
 */
import { describe, it, expect } from 'vitest'
import { reduceTaskEvent, reduceTaskEvents, createInitialTaskRunBlock, type RawEventInput } from './useTaskEventReducer'
import type { ToolExecutionViewModel } from '@/types'

interface V3Frame {
  index: number
  headline: string
  summary?: string
  impact?: string
  next_action?: string
  details?: string[]
}

function buildV3ToolFinishedFrames(toolName: string, toolCallId: string, frames: V3Frame[]): RawEventInput[] {
  return frames.map((f, i) => ({
    event_id: `evt_${toolCallId}_fin_${i}`,
    event_type: 'tool_finished',
    task_id: 'task_8716a97a',
    canonical_order: 10 + i,
    payload: {
      tool_name: toolName,
      tool_call_id: toolCallId,
      attempt: 1,
      chunk_index: f.index,
      chunk_total: 5,
      chunk_final: f.index === 4,
      duration_ms: 2500,
      display_output: `${toolName} 完成`,
      output: `${toolName} 完成`,
      headline: f.headline,
      summary: f.summary ?? '',
      impact: f.impact ?? '',
      next_action: f.next_action ?? '',
      details: f.details ?? [],
      kind: 'tool_result',
      level: 'success',
      source: 'template',
      dedupe_key: `${toolName}:tool_result:success`
    },
    created_at: '2026-08-01T04:58:45+00:00'
  }))
}

const REQUIREMENT_FRAMES: V3Frame[] = [
  { index: 0, headline: '需求文档解析完成' },
  { index: 1, headline: '需求文档解析完成', summary: '已完成需求文档解析，并识别主要业务结构和表单/图片内容。' },
  {
    index: 2, headline: '需求文档解析完成',
    summary: '已完成需求文档解析，并识别主要业务结构和表单/图片内容。',
    impact: '解析结果将用于确定测试范围以及测试方案章节结构。'
  },
  {
    index: 3, headline: '需求文档解析完成',
    summary: '已完成需求文档解析，并识别主要业务结构和表单/图片内容。',
    impact: '解析结果将用于确定测试范围以及测试方案章节结构。',
    next_action: '接下来将解析测试方案模板。'
  },
  {
    index: 4, headline: '需求文档解析完成',
    summary: '已完成需求文档解析，并识别主要业务结构和表单/图片内容。',
    impact: '解析结果将用于确定测试范围以及测试方案章节结构。',
    next_action: '接下来将解析测试方案模板。',
    details: ['识别章节 43 个，关键表格 7 个', '含 3 张图片的关键文本', '原文约 39×100 字']
  }
]

function toolStarted(toolName: string, toolCallId: string, withChunkIndex = false): RawEventInput {
  return {
    event_id: `evt_${toolCallId}_start`,
    event_type: 'tool_started',
    task_id: 'task_8716a97a',
    canonical_order: 9,
    payload: {
      tool_name: toolName,
      tool_call_id: toolCallId,
      attempt: 1,
      display_input: `${toolName} 输入摘要`,
      input: `${toolName} 输入摘要`,
      // 真实后端 _emit_tool_started 不携带 chunk_index;
      // withChunkIndex=true 时模拟异常/旧数据以验证兼容
      ...(withChunkIndex ? { chunk_index: 0 } : {})
    },
    created_at: '2026-08-01T04:58:40+00:00'
  }
}

describe('tool_started chunk 0 修复', () => {
  it('tool_started 不带 chunk_index 时不登记 receivedChunkIndexes', () => {
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    const started = reduceTaskEvent(block, toolStarted('RequirementParserTool', 'tc_req'), 'live')
    const tool = started.toolExecutions.find((t) => t.toolCallId === 'tc_req')!
    expect(tool.receivedChunkIndexes).toEqual({})
    expect(tool.receivedChunkIndexes[0]).toBeUndefined()
  })

  it('第一个 tool_finished chunk_index=0 不被丢弃', () => {
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    const started = reduceTaskEvent(block, toolStarted('RequirementParserTool', 'tc_req'), 'live')
    const firstFin = reduceTaskEvent(started, buildV3ToolFinishedFrames('RequirementParserTool', 'tc_req', [REQUIREMENT_FRAMES[0]])[0], 'live')
    const tool = firstFin.toolExecutions.find((t) => t.toolCallId === 'tc_req')!
    expect(tool.publicUpdate).toBeDefined()
    expect(tool.publicUpdate?.headline).toBe('需求文档解析完成')
    expect(tool.publicUpdate?.summary).toBe('')
  })
})

describe('RequirementParserTool 五帧归并', () => {
  it('完整五帧产生完整 publicUpdate', () => {
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    const events: RawEventInput[] = [
      toolStarted('RequirementParserTool', 'tc_req'),
      ...buildV3ToolFinishedFrames('RequirementParserTool', 'tc_req', REQUIREMENT_FRAMES)
    ]
    const result = reduceTaskEvents(block, events, 'live')
    const tool = result.toolExecutions.find((t) => t.toolCallId === 'tc_req')!
    expect(tool.publicUpdate?.headline).toBe('需求文档解析完成')
    expect(tool.publicUpdate?.summary).toContain('需求文档解析')
    expect(tool.publicUpdate?.impact).toContain('测试范围')
    expect(tool.publicUpdate?.nextAction).toBe('接下来将解析测试方案模板。')
    expect(tool.publicUpdate?.details).toEqual([
      '识别章节 43 个，关键表格 7 个',
      '含 3 张图片的关键文本',
      '原文约 39×100 字'
    ])
    expect(tool.publicUpdate?.chunkFinal).toBe(true)
    expect(tool.status).toBe('success')
    expect(tool.output).toContain('完成')
    expect(tool.durationMs).toBe(2500)
  })

  it('tool_started + 5 帧只产生一个 ToolExecution', () => {
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    const events: RawEventInput[] = [
      toolStarted('RequirementParserTool', 'tc_req'),
      ...buildV3ToolFinishedFrames('RequirementParserTool', 'tc_req', REQUIREMENT_FRAMES)
    ]
    const result = reduceTaskEvents(block, events, 'live')
    expect(result.toolExecutions.filter((t) => t.toolCallId === 'tc_req')).toHaveLength(1)
  })

  it('重复 chunk 0 被去重, 不产生第二个 ToolExecution', () => {
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    const events: RawEventInput[] = [
      toolStarted('RequirementParserTool', 'tc_req'),
      ...buildV3ToolFinishedFrames('RequirementParserTool', 'tc_req', REQUIREMENT_FRAMES),
      // 重复的 chunk 0 帧
      { ...buildV3ToolFinishedFrames('RequirementParserTool', 'tc_req', [REQUIREMENT_FRAMES[0]])[0], event_id: 'evt_dup_chunk0' }
    ]
    const result = reduceTaskEvents(block, events, 'live')
    expect(result.toolExecutions.filter((t) => t.toolCallId === 'tc_req')).toHaveLength(1)
    expect(result.toolExecutions[0].receivedChunkIndexes[0]).toBe(true)
  })

  it('乱序 chunk 重放结果确定(字段级单调 merge)', () => {
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    const frames = buildV3ToolFinishedFrames('RequirementParserTool', 'tc_req', REQUIREMENT_FRAMES)
    // 打乱顺序: frame4(full) → frame0 → frame2 → frame1 → frame3
    const shuffled: RawEventInput[] = [frames[4], frames[0], frames[2], frames[1], frames[3]]
    const result = reduceTaskEvents(block, [toolStarted('RequirementParserTool', 'tc_req'), ...shuffled], 'live')
    const tool = result.toolExecutions.find((t) => t.toolCallId === 'tc_req')!
    expect(tool.publicUpdate?.headline).toBe('需求文档解析完成')
    expect(tool.publicUpdate?.summary).toContain('需求文档解析')
    expect(tool.publicUpdate?.impact).toContain('测试范围')
    expect(tool.publicUpdate?.nextAction).toBe('接下来将解析测试方案模板。')
    expect(tool.publicUpdate?.details).toHaveLength(3)
  })
})

describe('TemplateParserTool 五帧归并', () => {
  it('完整五帧产生 headline=测试方案模板解析完成', () => {
    const tplFrames: V3Frame[] = [
      { index: 0, headline: '测试方案模板解析完成' },
      { index: 1, headline: '测试方案模板解析完成', summary: '已完成模板解析，识别章节结构与已固化表格。' },
      {
        index: 2, headline: '测试方案模板解析完成',
        summary: '已完成模板解析，识别章节结构与已固化表格。',
        impact: '模板结构将作为后续章节策略与生成边界。'
      },
      {
        index: 3, headline: '测试方案模板解析完成',
        summary: '已完成模板解析，识别章节结构与已固化表格。',
        impact: '模板结构将作为后续章节策略与生成边界。',
        next_action: '接下来将匹配知识库与生成章节处理建议。'
      },
      {
        index: 4, headline: '测试方案模板解析完成',
        summary: '已完成模板解析，识别章节结构与已固化表格。',
        impact: '模板结构将作为后续章节策略与生成边界。',
        next_action: '接下来将匹配知识库与生成章节处理建议。',
        details: ['模板：测试方案模板.docx', '识别模板章节 12 个，内嵌表格 3 个']
      }
    ]
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    const result = reduceTaskEvents(block, [
      toolStarted('TemplateParserTool', 'tc_tpl'),
      ...buildV3ToolFinishedFrames('TemplateParserTool', 'tc_tpl', tplFrames)
    ], 'live')
    const tool = result.toolExecutions.find((t) => t.toolCallId === 'tc_tpl')!
    expect(tool.publicUpdate?.headline).toBe('测试方案模板解析完成')
    expect(tool.publicUpdate?.summary).toBe('已完成模板解析，识别章节结构与已固化表格。')
    expect(tool.publicUpdate?.impact).toContain('章节策略')
    expect(tool.publicUpdate?.nextAction).toContain('知识库')
    expect(tool.publicUpdate?.details.length).toBeGreaterThan(0)
  })
})

describe('Live 与 Hydrate 一致性', () => {
  function runLive(events: RawEventInput[]) {
    let block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    for (const evt of events) block = reduceTaskEvent(block, evt, 'live')
    return block
  }
  function runHydrate(events: RawEventInput[]) {
    return reduceTaskEvents(createInitialTaskRunBlock('task_8716a97a', 'running', 'hydrate'), events, 'hydrate')
  }

  function comparableTool(t: ToolExecutionViewModel) {
    return {
      toolName: t.toolName,
      toolCallId: t.toolCallId,
      attempt: t.attempt,
      status: t.status,
      input: t.input,
      output: t.output,
      durationMs: t.durationMs,
      publicUpdate: t.publicUpdate,
      receivedChunkIndexes: t.receivedChunkIndexes
    }
  }

  it('相同事件集 Live 与 Hydrate 深度等价', () => {
    const events: RawEventInput[] = [
      toolStarted('RequirementParserTool', 'tc_req'),
      toolStarted('TemplateParserTool', 'tc_tpl'),
      ...buildV3ToolFinishedFrames('RequirementParserTool', 'tc_req', REQUIREMENT_FRAMES),
      ...buildV3ToolFinishedFrames('TemplateParserTool', 'tc_tpl', [
        { index: 0, headline: '测试方案模板解析完成' },
        { index: 4, headline: '测试方案模板解析完成', summary: '已完成模板解析', impact: 'i', next_action: 'na', details: ['d'] }
      ])
    ]
    const liveBlock = runLive(events)
    const hydrateBlock = runHydrate(events)
    expect(liveBlock.toolExecutions).toHaveLength(2)
    expect(hydrateBlock.toolExecutions).toHaveLength(2)
    const liveTools = liveBlock.toolExecutions.map(comparableTool).sort((a, b) => a.toolCallId.localeCompare(b.toolCallId))
    const hydrateTools = hydrateBlock.toolExecutions.map(comparableTool).sort((a, b) => a.toolCallId.localeCompare(b.toolCallId))
    expect(hydrateTools).toEqual(liveTools)
  })
})

describe('Phase 2.9B / 动态 Agent 隔离', () => {
  it('2.9A 固定叙事解析不依赖 public_update(2.9B)或任何动态 Agent 开关', () => {
    // 仅含扁平 v3 headline 字段的 tool_finished → 必然产出 publicUpdate,
    // 与 2.9B narrative / Preparation Agent / LLM 调用无关。
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    const result = reduceTaskEvents(block, [
      toolStarted('RequirementParserTool', 'tc_req'),
      ...buildV3ToolFinishedFrames('RequirementParserTool', 'tc_req', REQUIREMENT_FRAMES)
    ], 'live')
    expect(result.toolExecutions[0].publicUpdate?.headline).toBe('需求文档解析完成')
  })

  it('2.9B public_update 仍可通过统一解析器识别(嵌套兼容)', () => {
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    const result = reduceTaskEvent(block, {
      event_id: 'evt_9b_update',
      event_type: 'tool_finished',
      task_id: 'task_8716a97a',
      canonical_order: 20,
      payload: {
        tool_name: 'PreparationAgent',
        tool_call_id: 'tc_prep',
        attempt: 1,
        chunk_index: 0,
        chunk_final: true,
        public_update: {
          headline: 'Preparation 决策',
          summary: '准备生成测试方案',
          impact: '影响下一步',
          next_action: '继续生成',
          details: ['基于解析结果']
        }
      },
      created_at: '2026-08-01T04:58:45+00:00'
    }, 'live')
    const tool = result.toolExecutions.find((t) => t.toolCallId === 'tc_prep')!
    expect(tool.publicUpdate?.headline).toBe('Preparation 决策')
    expect(tool.publicUpdate?.summary).toBe('准备生成测试方案')
  })

  it('Schema Retry / 其他无关事件不清除已有 publicUpdate', () => {
    const block = createInitialTaskRunBlock('task_8716a97a', 'running', 'live')
    let result = reduceTaskEvents(block, [
      toolStarted('RequirementParserTool', 'tc_req'),
      ...buildV3ToolFinishedFrames('RequirementParserTool', 'tc_req', REQUIREMENT_FRAMES)
    ], 'live')
    const before = result.toolExecutions.find((t) => t.toolCallId === 'tc_req')!.publicUpdate

    // 后续到达的普通 tool_finished 帧(无叙事字段)不得清空 update
    result = reduceTaskEvent(result, {
      event_id: 'evt_retry_noise',
      event_type: 'tool_finished',
      task_id: 'task_8716a97a',
      canonical_order: 30,
      payload: {
        tool_name: 'TestPlanGeneratorTool',
        tool_call_id: 'tc_gen',
        attempt: 1,
        chunk_index: 0,
        chunk_final: true,
        display_output: 'retry output',
        output: 'retry output'
      },
      created_at: '2026-08-01T04:58:50+00:00'
    }, 'live')
    const after = result.toolExecutions.find((t) => t.toolCallId === 'tc_req')!.publicUpdate
    expect(after).toBeDefined()
    expect(after?.headline).toBe(before?.headline)
    expect(after?.summary).toBe(before?.summary)
    expect(after?.details).toEqual(before?.details)
  })
})
