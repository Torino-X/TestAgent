import type { ContextUsageBreakdown, ContextUsageResponse } from '@/types'

export type ContextUsageTone = 'normal' | 'warning' | 'high' | 'unknown' | 'unavailable'

export const CONTEXT_USAGE_BREAKDOWN_ITEMS: Array<{
  key: keyof ContextUsageBreakdown
  label: string
}> = [
  { key: 'conversation_history', label: '对话历史' },
  { key: 'project_documents', label: '项目资料' },
  { key: 'task_context', label: '任务上下文' },
  { key: 'user_memory', label: 'Memory' },
  { key: 'system_instructions', label: '系统指令' }
]

function trimFixed(value: string): string {
  return value.endsWith('.0') ? value.slice(0, -2) : value
}

export function formatTokenCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '--'
  const sign = value < 0 ? '-' : ''
  const absolute = Math.abs(Math.trunc(value))

  if (absolute < 1000) return `${sign}${absolute}`
  if (absolute < 1_000_000) {
    const scaled = absolute / 1000
    const formatted = scaled < 100 && absolute % 1000 !== 0
      ? trimFixed(scaled.toFixed(1))
      : String(Math.round(scaled))
    return `${sign}${formatted}K`
  }

  return `${sign}${(absolute / 1_000_000).toFixed(1)}M`
}

export function formatTokenSharePercent(
  value: number | null | undefined,
  total: number | null | undefined
): string {
  if (
    value === null ||
    value === undefined ||
    total === null ||
    total === undefined ||
    !Number.isFinite(value) ||
    !Number.isFinite(total) ||
    total <= 0
  ) {
    return '--'
  }
  const percent = Math.max(0, Math.min(100, (value / total) * 100))
  return `${trimFixed(percent.toFixed(1))}%`
}

export function isContextUsageUnknown(usage: ContextUsageResponse | null): boolean {
  return !usage || usage.model.context_window_tokens === null || usage.usage.percent === null
}

export function isContextUsageHeuristic(usage: ContextUsageResponse | null): boolean {
  return usage?.usage.count_mode === 'heuristic'
}

/** True only for the legacy/no-snapshot conversation-only estimate. */
export function isContextUsageFallback(usage: ContextUsageResponse | null): boolean {
  return usage?.available === false && isContextUsageHeuristic(usage)
}

export function visualContextPercent(usage: ContextUsageResponse | null): number | null {
  if (isContextUsageUnknown(usage)) return null
  const percent = usage?.usage.percent
  if (percent === null || percent === undefined || !Number.isFinite(percent)) return null
  return Math.max(0, Math.min(100, percent))
}

export function contextUsageTone(
  usage: ContextUsageResponse | null,
  unavailable = false
): ContextUsageTone {
  if (unavailable) return 'unavailable'
  if (isContextUsageUnknown(usage)) return 'unknown'
  const percent = usage?.usage.percent ?? 0
  if (usage?.usage.over_limit || percent >= 80) return 'high'
  if (percent >= 65) return 'warning'
  return 'normal'
}

export function formatUsageTotal(usage: ContextUsageResponse | null): string {
  if (!usage) return '上下文窗口未知'
  const used = usage.usage.used_tokens
  const window = usage.model.context_window_tokens
  if (used === null || window === null || usage.usage.percent === null) return '上下文窗口未知'
  const total = `${formatTokenCount(used)} / ${formatTokenCount(window)} tokens`
  return isContextUsageHeuristic(usage) ? `估算 ${total}` : `实际 ${total}`
}

export function formatUsagePercent(usage: ContextUsageResponse | null): string {
  const percent = usage?.usage.percent
  if (percent === null || percent === undefined || !Number.isFinite(percent)) return ''
  return `${trimFixed(percent.toFixed(1))}%`
}
