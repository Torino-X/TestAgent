import { describe, expect, it } from 'vitest'
import {
  CONTEXT_USAGE_BREAKDOWN_ITEMS,
  contextUsageTone,
  formatTokenCount,
  formatTokenSharePercent,
  formatUsageTotal,
  isContextUsageFallback,
  isContextUsageHeuristic,
  visualContextPercent
} from './contextUsage'
import type { ContextUsageResponse } from '@/types'

function usage(overrides: Partial<ContextUsageResponse> = {}): ContextUsageResponse {
  return {
    conversation_public_id: 'conv_test',
    model: {
      name: 'qwen3.7-max',
      context_window_tokens: 128000,
      window_source: 'model_config'
    },
    usage: {
      used_tokens: 86000,
      available_tokens: 42000,
      percent: 67.2,
      count_mode: 'exact',
      estimated: false,
      over_limit: false
    },
    breakdown: {
      conversation_history: 61000,
      project_documents: 12000,
      task_context: 7000,
      user_memory: 4000,
      system_instructions: 2000
    },
    compaction: {
      available: true,
      recommended: false,
      in_progress: false
    },
    available: true,
    snapshot_public_id: 'ctxsnap_123',
    as_of: '2026-08-08T12:00:00Z',
    ...overrides
  }
}

describe('context usage formatting and display helpers', () => {
  it('formats token counts without locale-dependent separators', () => {
    expect(formatTokenCount(860)).toBe('860')
    expect(formatTokenCount(12800)).toBe('12.8K')
    expect(formatTokenCount(128000)).toBe('128K')
    expect(formatTokenCount(1000000)).toBe('1.0M')
  })

  it('formats token share percent against used context', () => {
    expect(formatTokenSharePercent(61000, 86000)).toBe('70.9%')
    expect(formatTokenSharePercent(2000, 86000)).toBe('2.3%')
    expect(formatTokenSharePercent(0, 86000)).toBe('0%')
    expect(formatTokenSharePercent(16, 0)).toBe('--')
    expect(formatTokenSharePercent(2400, 2300)).toBe('100%')
  })

  it('clamps visual percent while preserving over-limit semantics', () => {
    expect(visualContextPercent(usage({ usage: { ...usage().usage, percent: 143, over_limit: true } }))).toBe(100)
    expect(contextUsageTone(usage({ usage: { ...usage().usage, percent: 143, over_limit: true } }))).toBe('high')
  })

  it('derives severity from displayed percentage, not the compaction recommendation', () => {
    const at = (percent: number, recommended = false) => usage({
      usage: { ...usage().usage, percent, over_limit: false },
      compaction: { ...usage().compaction, recommended }
    })

    expect(contextUsageTone(at(6.3, true))).toBe('normal')
    expect(contextUsageTone(at(64.99))).toBe('normal')
    expect(contextUsageTone(at(65))).toBe('warning')
    expect(contextUsageTone(at(79.99))).toBe('warning')
    expect(contextUsageTone(at(80))).toBe('high')
  })

  it('treats unknown context window as unknown instead of zero', () => {
    const unknown = usage({
      model: { name: 'qwen3.7-max', context_window_tokens: null, window_source: 'unknown' },
      usage: { ...usage().usage, percent: null }
    })

    expect(visualContextPercent(unknown)).toBeNull()
    expect(contextUsageTone(unknown)).toBe('unknown')
  })

  it('identifies a no-snapshot heuristic so it is never rendered as actual usage', () => {
    const heuristicSnapshot = usage({ usage: { ...usage().usage, count_mode: 'heuristic' } })
    const fallback = usage({
      available: false,
      snapshot_public_id: null,
      usage: { ...usage().usage, count_mode: 'heuristic' }
    })

    expect(isContextUsageHeuristic(heuristicSnapshot)).toBe(true)
    expect(isContextUsageFallback(heuristicSnapshot)).toBe(false)
    expect(isContextUsageFallback(fallback)).toBe(true)
    expect(isContextUsageHeuristic(usage())).toBe(false)
  })

  it('labels provider-reported totals as actual and fallback totals as estimated', () => {
    expect(formatUsageTotal(usage())).toBe('实际 86K / 128K tokens')
    expect(formatUsageTotal(usage({
      usage: { ...usage().usage, count_mode: 'heuristic' }
    }))).toBe('估算 86K / 128K tokens')
  })

  it('exposes only the five productized breakdown rows', () => {
    expect(CONTEXT_USAGE_BREAKDOWN_ITEMS).toEqual([
      { key: 'conversation_history', label: '对话历史' },
      { key: 'project_documents', label: '项目资料' },
      { key: 'task_context', label: '任务上下文' },
      { key: 'user_memory', label: 'Memory' },
      { key: 'system_instructions', label: '系统指令' }
    ])
  })
})
