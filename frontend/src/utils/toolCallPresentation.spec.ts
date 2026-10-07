import { describe, expect, it } from 'vitest'
import type { ToolCall } from '@/types'
import { buildToolCallPresentation, displayToolNameFor, toolTimelineTitle } from './toolCallPresentation'

function toolCall(overrides: Partial<ToolCall>): ToolCall {
  return {
    id: 'call-1',
    name: 'RequirementParserTool',
    status: 'running',
    input: '读取需求文档并提取结构',
    output: '',
    duration: '',
    ...overrides,
  }
}

describe('tool call presentation', () => {
  it.each([
    ['RequirementParserTool', '调用Word文档解析工具'],
    ['TemplateParserTool', '调用模板解析工具'],
    ['KnowledgeSearchTool', '调用知识库查询工具'],
    ['SectionSuggestionTool', '调用章节建议工具'],
    ['TestPlanGeneratorTool', '调用测试方案生成工具'],
  ])('maps %s to confirmed user-visible display name', (backendName, displayName) => {
    expect(displayToolNameFor(backendName)).toBe(displayName)
  })

  it('keeps dynamic agent and unknown tool names unchanged', () => {
    expect(displayToolNameFor('PlanPreparationAgent')).toBe('PlanPreparationAgent')
    expect(displayToolNameFor('OtherInternalTool')).toBe('OtherInternalTool')
  })

  it('keeps the tool timeline title as the user-visible call title', () => {
    expect(toolTimelineTitle(toolCall({ name: 'RequirementParserTool' }))).toBe('调用Word文档解析工具')
    expect(toolTimelineTitle(toolCall({ name: 'PlanPreparationAgent' }))).toBe('PlanPreparationAgent')
  })

  it('renders running title and real backend progress without debug input/output labels', () => {
    const presentation = buildToolCallPresentation(toolCall({
      displayName: '调用Word文档解析工具',
      businessAction: '解析需求文档',
      businessSubjectType: '需求文档',
      businessSubjectName: '智慧校园需求说明书.docx',
      progressMessage: '正在读取 Word 文档',
    }))

    expect(presentation.statusCode).toBe('RUNNING')
    expect(presentation.title).toBe('正在解析需求文档：《智慧校园需求说明书.docx》')
    expect(presentation.detail).toBe('正在读取 Word 文档')
    expect(presentation.title).not.toContain('RequirementParserTool')
    expect(presentation.detail).not.toContain('Input:')
    expect(presentation.detail).not.toContain('Output:')
  })

  it('renders success from subject and completion message', () => {
    const presentation = buildToolCallPresentation(toolCall({
      status: 'success',
      businessAction: '解析需求文档',
      businessSubjectType: '需求文档',
      businessSubjectName: '智慧校园需求说明书.docx',
      completionMessage: '需求文档《智慧校园需求说明书.docx》已解析完成',
    }))

    expect(presentation.statusCode).toBe('SUCCESS')
    expect(presentation.title).toBe('已完成需求文档解析')
    expect(presentation.detail).toBe('需求文档《智慧校园需求说明书.docx》已解析完成')
  })

  it('preserves source-result line breaks in knowledge retrieval completion messages', () => {
    const presentation = buildToolCallPresentation(toolCall({
      status: 'success',
      name: 'KnowledgeSearchTool',
      completionMessage: [
        '公司知识库未配置，已跳过；将仅依据需求与项目资料继续。',
        '当前会话未关联项目，项目资料检索已跳过。',
      ].join('\n'),
    }))

    expect(presentation.detail).toBe(
      '公司知识库未配置，已跳过；将仅依据需求与项目资料继续。\n当前会话未关联项目，项目资料检索已跳过。',
    )
  })

  it('renders failed state with safe short error summary', () => {
    const presentation = buildToolCallPresentation(toolCall({
      status: 'failed',
      businessAction: '解析需求文档',
      businessSubjectType: '需求文档',
      errorSummary: '无法读取该 Word 文档，请确认文件未损坏',
      output: 'Traceback: C:\\secret\\tool.py',
    }))

    expect(presentation.statusCode).toBe('FAILED')
    expect(presentation.title).toBe('需求文档解析失败')
    expect(presentation.detail).toBe('无法读取该 Word 文档，请确认文件未损坏')
    expect(presentation.detail).not.toContain('Traceback')
    expect(presentation.detail).not.toContain('C:\\')
  })

  it('renders failed state with ResultReviewTool semantic block-issue summary', () => {
    // Phase 2.9A.X: 后端在 ResultReviewTool data.level="failed" 时
    // 生成「审查未通过，结果不符合：\n1. [section_2] 表头键名不符\n2. [section_5] 缺少必含章节\n接下来进入 RepairAgent 定点修复。」
    // — 这个 errorSummary 必须原样透传给前端，不能被通用 fallback 覆盖。
    const blockSummary = [
      '审查未通过，结果不符合：',
      '1. [section_2] 表头键名不符',
      '2. [section_5] 缺少必含章节',
      '接下来进入 RepairAgent 定点修复。',
    ].join('\n')
    const presentation = buildToolCallPresentation(toolCall({
      status: 'failed',
      name: 'ResultReviewTool',
      displayName: '调用结果审查工具',
      businessAction: '审查测试方案',
      businessSubjectType: '测试方案',
      errorSummary: blockSummary,
    }))

    expect(presentation.statusCode).toBe('FAILED')
    // 语义失败:工具执行完成但审查不通过，标题区别于「执行失败」
    expect(presentation.title).toBe('调用结果审查工具执行完成，但审查未通过')
    expect(presentation.detail).toContain('审查未通过')
    expect(presentation.detail).toContain('1. [section_2]')
    expect(presentation.detail).toContain('2. [section_5]')
    expect(presentation.detail).toContain('RepairAgent')
    expect(presentation.detail).not.toContain('请稍后重试')
  })

  it('does not show generic 「请稍后重试」 fallback when errorSummary is empty', () => {
    // 工具执行失败但后端没传 errorSummary 时,前端用更友好的兜底文案
    const presentation = buildToolCallPresentation(toolCall({
      status: 'failed',
      name: 'SomeTool',
      errorSummary: '',
    }))

    expect(presentation.statusCode).toBe('FAILED')
    expect(presentation.detail).not.toContain('请稍后重试')
    expect(presentation.detail).toContain('查看后端日志')
  })
})
