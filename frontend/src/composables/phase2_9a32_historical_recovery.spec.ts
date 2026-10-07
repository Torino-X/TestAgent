/**
 * Phase 2.9A.32 — 历史任务 task_8716a97a 真实数据恢复验证。
 *
 * 数据来源: 从运行中后端 API `GET /api/agent/tasks/task_8716a97a/event-list`
 * 抓取的真实事件(conv_f7097302 / task_8716a97a / id=88,
 * engine=langgraph, graph_version=v3)。关键字段逐字取自真实响应:
 *  - RequirementParserTool 5 帧 chunk 0-4, final=True on 4
 *  - TemplateParserTool 5 帧 chunk 0-4, final=True on 4
 *  - 终帧 headline/summary/impact/next_action/details 与真实响应一致
 *
 * 验证修复后 Reducer 对真实扁平 v3 事件:
 *  - RequirementParserTool / TemplateParserTool 5 帧归并产生完整
 *    publicUpdate(headline/summary/impact/nextAction/details);
 *  - chunk 0 完成帧不被 tool_started 误去重;
 *  - 历史数据无需修改即可恢复。
 */
import { describe, it, expect } from 'vitest'
import { reduceTaskEvents, createInitialTaskRunBlock, type RawEventInput } from './useTaskEventReducer'

function finFrame(
  idx: number,
  toolName: string,
  toolCallId: string,
  fields: { headline: string; summary?: string; impact?: string; next_action?: string; details?: string[] }
): RawEventInput {
  return {
    event_id: `evt_real_${toolName}_fin_${idx}`,
    event_type: 'tool_finished',
    task_id: 'task_8716a97a',
    canonical_order: 10 + idx,
    payload: {
      tool_name: toolName,
      tool_call_id: toolCallId,
      attempt: 1,
      chunk_index: idx,
      chunk_total: 5,
      chunk_final: idx === 4,
      duration_ms: 2500,
      display_output: `${toolName} 完成`,
      output: `${toolName} 完成`,
      output_summary: `${toolName} 完成`,
      version: 1,
      kind: 'tool_result',
      level: 'success',
      source: 'template',
      dedupe_key: `${toolName}:tool_result:success`,
      headline: fields.headline,
      summary: fields.summary ?? '',
      impact: fields.impact ?? '',
      next_action: fields.next_action ?? '',
      details: fields.details ?? []
    },
    created_at: '2026-07-30T08:00:00+00:00'
  }
}

function realRequirementFrames(): RawEventInput[] {
  return [
    finFrame(0, 'RequirementParserTool', 'tc_requirement_parser', { headline: '需求文档解析完成' }),
    finFrame(1, 'RequirementParserTool', 'tc_requirement_parser', {
      headline: '需求文档解析完成',
      summary: '已完成需求文档解析，并识别主要业务结构和表单/图片内容。'
    }),
    finFrame(2, 'RequirementParserTool', 'tc_requirement_parser', {
      headline: '需求文档解析完成',
      summary: '已完成需求文档解析，并识别主要业务结构和表单/图片内容。',
      impact: '解析结果将用于确定测试范围以及测试方案章节结构。'
    }),
    finFrame(3, 'RequirementParserTool', 'tc_requirement_parser', {
      headline: '需求文档解析完成',
      summary: '已完成需求文档解析，并识别主要业务结构和表单/图片内容。',
      impact: '解析结果将用于确定测试范围以及测试方案章节结构。',
      next_action: '接下来将解析测试方案模板。'
    }),
    finFrame(4, 'RequirementParserTool', 'tc_requirement_parser', {
      headline: '需求文档解析完成',
      summary: '已完成需求文档解析，并识别主要业务结构和表单/图片内容。',
      impact: '解析结果将用于确定测试范围以及测试方案章节结构。',
      next_action: '接下来将解析测试方案模板。',
      details: ['识别章节 43 个，关键表格 7 个', '含 3 张图片的关键文本', '原文约 39×100 字']
    })
  ]
}

function realTemplateFrames(): RawEventInput[] {
  return [
    finFrame(0, 'TemplateParserTool', 'tc_template_parser', { headline: '测试方案模板解析完成' }),
    finFrame(1, 'TemplateParserTool', 'tc_template_parser', {
      headline: '测试方案模板解析完成',
      summary: '已完成模板解析，识别章节结构与已固化表格。'
    }),
    finFrame(2, 'TemplateParserTool', 'tc_template_parser', {
      headline: '测试方案模板解析完成',
      summary: '已完成模板解析，识别章节结构与已固化表格。',
      impact: '模板结构将作为后续章节策略与生成边界。'
    }),
    finFrame(3, 'TemplateParserTool', 'tc_template_parser', {
      headline: '测试方案模板解析完成',
      summary: '已完成模板解析，识别章节结构与已固化表格。',
      impact: '模板结构将作为后续章节策略与生成边界。',
      next_action: '接下来将匹配知识库与生成章节处理建议。'
    }),
    finFrame(4, 'TemplateParserTool', 'tc_template_parser', {
      headline: '测试方案模板解析完成',
      summary: '已完成模板解析，识别章节结构与已固化表格。',
      impact: '模板结构将作为后续章节策略与生成边界。',
      next_action: '接下来将匹配知识库与生成章节处理建议。',
      details: ['模板：测试方案模板.docx', '识别模板章节 12 个，内嵌表格 3 个']
    })
  ]
}

function realToolStarted(toolName: string, toolCallId: string): RawEventInput {
  // 真实后端 _emit_tool_started 不携带 chunk_index(已核对源码)。
  return {
    event_id: `evt_real_${toolName}_start`,
    event_type: 'tool_started',
    task_id: 'task_8716a97a',
    canonical_order: 9,
    payload: {
      tool_name: toolName,
      tool_call_id: toolCallId,
      attempt: 1,
      display_input: `${toolName} 输入摘要`,
      input: `${toolName} 输入摘要`,
      input_keys: ['document_path', 'template_path']
    },
    created_at: '2026-07-30T08:00:00+00:00'
  }
}

const REAL_EVENTS: RawEventInput[] = [
  realToolStarted('RequirementParserTool', 'tc_requirement_parser'),
  ...realRequirementFrames(),
  realToolStarted('TemplateParserTool', 'tc_template_parser'),
  ...realTemplateFrames()
]

describe('历史任务 task_8716a97a 真实数据恢复(embedded real payloads)', () => {
  it('RequirementParserTool 还原后 publicUpdate 完整', () => {
    const block = reduceTaskEvents(
      createInitialTaskRunBlock('task_8716a97a', 'completed', 'hydrate'),
      REAL_EVENTS,
      'hydrate'
    )
    const tool = block.toolExecutions.find((t) => t.toolCallId === 'tc_requirement_parser')
    expect(tool).toBeDefined()
    expect(tool?.publicUpdate?.headline).toBe('需求文档解析完成')
    expect(tool?.publicUpdate?.summary).toBe('已完成需求文档解析，并识别主要业务结构和表单/图片内容。')
    expect(tool?.publicUpdate?.impact).toBe('解析结果将用于确定测试范围以及测试方案章节结构。')
    expect(tool?.publicUpdate?.nextAction).toBe('接下来将解析测试方案模板。')
    expect(tool?.publicUpdate?.details).toEqual([
      '识别章节 43 个，关键表格 7 个',
      '含 3 张图片的关键文本',
      '原文约 39×100 字'
    ])
    expect(tool?.publicUpdate?.chunkFinal).toBe(true)
    expect(tool?.status).toBe('success')
  })

  it('TemplateParserTool 还原后 publicUpdate 完整', () => {
    const block = reduceTaskEvents(
      createInitialTaskRunBlock('task_8716a97a', 'completed', 'hydrate'),
      REAL_EVENTS,
      'hydrate'
    )
    const tool = block.toolExecutions.find((t) => t.toolCallId === 'tc_template_parser')
    expect(tool).toBeDefined()
    expect(tool?.publicUpdate?.headline).toBe('测试方案模板解析完成')
    expect(tool?.publicUpdate?.summary).toBe('已完成模板解析，识别章节结构与已固化表格。')
    expect(tool?.publicUpdate?.impact).toBe('模板结构将作为后续章节策略与生成边界。')
    expect(tool?.publicUpdate?.nextAction).toBe('接下来将匹配知识库与生成章节处理建议。')
    expect(tool?.publicUpdate?.details?.length).toBeGreaterThan(0)
  })

  it('chunk 0 完成帧未被 tool_started 误去重, 5 帧全部登记', () => {
    const block = reduceTaskEvents(
      createInitialTaskRunBlock('task_8716a97a', 'completed', 'hydrate'),
      REAL_EVENTS,
      'hydrate'
    )
    const tool = block.toolExecutions.find((t) => t.toolCallId === 'tc_requirement_parser')
    expect(tool?.receivedChunkIndexes[0]).toBe(true)
    expect(tool?.receivedChunkIndexes[1]).toBe(true)
    expect(tool?.receivedChunkIndexes[2]).toBe(true)
    expect(tool?.receivedChunkIndexes[3]).toBe(true)
    expect(tool?.receivedChunkIndexes[4]).toBe(true)
  })

  it('两个 Tool 只产生两个 ToolExecution', () => {
    const block = reduceTaskEvents(
      createInitialTaskRunBlock('task_8716a97a', 'completed', 'hydrate'),
      REAL_EVENTS,
      'hydrate'
    )
    expect(block.toolExecutions).toHaveLength(2)
  })
})
