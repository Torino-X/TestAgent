import type { ToolCall } from '@/types'

export interface ToolCallPresentation {
  statusCode: 'RUNNING' | 'SUCCESS' | 'FAILED'
  statusClass: 'running' | 'success' | 'failed'
  title: string
  detail: string
}

const DISPLAY_NAMES: Record<string, string> = {
  RequirementParserTool: '调用Word文档解析工具',
  TemplateParserTool: '调用模板解析工具',
  KnowledgeSearchTool: '调用知识库查询工具',
  SectionSuggestionTool: '调用章节建议工具',
  TestPlanGeneratorTool: '调用测试方案生成工具',
  ResultReviewTool: '调用结果审查工具',
  WordExportTool: '调用Word文档导出工具',
  DocxFormatCheckTool: '调用Word文档格式检查工具',
}

export function displayToolNameFor(backendToolName: string): string {
  return DISPLAY_NAMES[backendToolName] ?? backendToolName
}

export function toolTimelineTitle(toolCall?: Pick<ToolCall, 'name' | 'displayName'> | null): string {
  if (!toolCall) return '调用 Agent Tool'
  return toolCall.displayName || displayToolNameFor(toolCall.name)
}

export function buildToolCallPresentation(toolCall?: ToolCall | null): ToolCallPresentation {
  const status = toolCall?.status === 'failed'
    ? 'failed'
    : toolCall?.status === 'success'
      ? 'success'
      : 'running'
  const displayName = toolCall?.displayName || displayToolNameFor(toolCall?.name || 'Agent Tool')
  const subjectType = cleanText(toolCall?.businessSubjectType)
  const subjectName = cleanText(toolCall?.businessSubjectName || toolCall?.fileName)
  const businessAction = cleanText(toolCall?.businessAction)

  if (status === 'failed') {
    // Phase 2.9A.X: 优先用 backend 在 semantic_failure 时构造的 errorSummary
    // （例如 ResultReviewTool level="failed" → 分点 block_issues 列表），
    // 避免落到通用 fallback 误导用户「请稍后重试」。
    const semantic = cleanText(toolCall?.errorSummary)
    // Phase 2.9A.X: ResultReviewTool 语义失败（工具正常运行但审查不通过）
    // 与工具执行失败区分：标题说"执行完成，但审查未通过"
    const isSemanticReviewFailure =
      toolCall?.name === 'ResultReviewTool' && !!semantic
    return {
      statusCode: 'FAILED',
      statusClass: 'failed',
      title: isSemanticReviewFailure
        ? `${displayName}执行完成，但审查未通过`
        : failedTitle(subjectType, businessAction, displayName),
      detail:
        semantic ||
        safeDetail(toolCall?.output) ||
        '工具执行失败，请查看后端日志或重试任务',
    }
  }

  if (status === 'success') {
    return {
      statusCode: 'SUCCESS',
      statusClass: 'success',
      title: successTitle(subjectType, businessAction, displayName),
      detail: safeDetail(toolCall?.completionMessage) || completionFallback(subjectType, subjectName, displayName),
    }
  }

  return {
    statusCode: 'RUNNING',
    statusClass: 'running',
    title: runningTitle(businessAction, subjectName, displayName),
    detail: safeDetail(toolCall?.progressMessage) || '正在等待工具返回进度',
  }
}

function runningTitle(action: string, subjectName: string, displayName: string): string {
  if (action && subjectName) return `正在${action}：《${subjectName}》`
  if (action) return `正在${action}`
  return `正在执行 ${displayName}`
}

function successTitle(subjectType: string, action: string, displayName: string): string {
  const verb = actionVerb(action)
  if (subjectType && verb) return `已完成${subjectType}${verb}`
  if (action) return `已完成${action}`
  return `${displayName}已完成`
}

function failedTitle(subjectType: string, action: string, displayName: string): string {
  const verb = actionVerb(action)
  if (subjectType && verb) return `${subjectType}${verb}失败`
  if (action) return `${action}失败`
  return `${displayName}执行失败`
}

function actionVerb(action: string): string {
  if (!action) return ''
  if (action.startsWith('解析')) return '解析'
  if (action.startsWith('生成')) return '生成'
  if (action.startsWith('查询')) return '查询'
  if (action.startsWith('检索')) return '检索'
  if (action.startsWith('建议')) return '建议'
  return action.replace(/^正在/, '')
}

function completionFallback(subjectType: string, subjectName: string, displayName: string): string {
  if (subjectType && subjectName) return `${subjectType}《${subjectName}》已处理完成`
  return `${displayName}已完成`
}

function cleanText(value?: string): string {
  return typeof value === 'string' ? value.trim() : ''
}

function safeDetail(value?: string): string {
  const text = cleanText(value)
  if (!text) return ''
  // Tool results can contain independently meaningful source outcomes.
  // Keep explicit line breaks for the card while still normalizing redundant
  // horizontal whitespace within each outcome.
  return text.replace(/[^\S\r\n]+/g, ' ')
}
