import { parseApiDateTime } from '@/utils/time'

type TaskTimingSource = {
  status?: string | null
  startedAt?: string | null
  completedAt?: string | null
  durationMs?: number | null
  run?: {
    startedAt?: string | null
    finishedAt?: string | null
    durationMs?: number | null
  } | null
}

type ResolvedTaskElapsedInput = {
  status?: string | null
  startedAt?: string | null
  completedAt?: string | null
  durationMs?: number | null
  nowMs: number
}

type ToolTimingSource = {
  status?: string | null
  duration?: string | null
  startedAt?: string | null
  finishedAt?: string | null
}

export function deriveTaskElapsedSource(
  task: TaskTimingSource | null | undefined,
  fallbackStatus: string,
  candidateStartedAt: Array<string | null | undefined> = [],
  candidateCompletedAt: Array<string | null | undefined> = [],
): TaskTimingSource {
  const source = task ?? {}
  if (source.startedAt || source.completedAt || isNonNegativeFinite(source.durationMs) || source.run?.startedAt) {
    const status = source.status ?? fallbackStatus
    return {
      ...source,
      status,
      completedAt: source.completedAt ?? (isTerminalTaskStatus(status) ? latestValidTime(candidateCompletedAt) : null),
    }
  }

  const fallbackStartedAt = earliestValidTime(candidateStartedAt)
  const fallbackCompletedAt = isTerminalTaskStatus(source.status ?? fallbackStatus)
    ? latestValidTime(candidateCompletedAt)
    : null
  return {
    ...source,
    status: source.status ?? fallbackStatus,
    startedAt: fallbackStartedAt ?? source.startedAt ?? null,
    completedAt: fallbackCompletedAt ?? source.completedAt ?? null,
  }
}

export function formatTaskElapsed(task: TaskTimingSource, nowMs: number): string {
  const run = task.run
  const hasTaskLifecycleTiming =
    !!task.startedAt ||
    !!task.completedAt ||
    isNonNegativeFinite(task.durationMs)
  const elapsedMs = resolveTaskElapsedMs({
    status: task.status,
    startedAt: task.startedAt ?? (hasTaskLifecycleTiming ? null : run?.startedAt) ?? null,
    completedAt: task.completedAt ?? (hasTaskLifecycleTiming ? null : run?.finishedAt) ?? null,
    durationMs: task.durationMs ?? (hasTaskLifecycleTiming ? null : run?.durationMs) ?? null,
    nowMs
  })
  return elapsedMs === null ? '—' : formatElapsedDuration(elapsedMs)
}

export function resolveTaskElapsedMs(input: ResolvedTaskElapsedInput): number | null {
  const status = input.status ?? null
  if (isTerminalTaskStatus(status)) {
    if (isNonNegativeFinite(input.durationMs)) return input.durationMs
    const startedAt = parseTime(input.startedAt)
    const completedAt = parseTime(input.completedAt)
    if (startedAt === null || completedAt === null) return null
    return Math.max(0, completedAt - startedAt)
  }

  const startedAt = parseTime(input.startedAt)
  if (startedAt === null) return null
  return Math.max(0, input.nowMs - startedAt)
}

export function shouldTickTaskElapsed(status?: string | null): boolean {
  return TASK_TIMING_ACTIVE_STATES.has(status ?? '')
}

export function formatToolElapsed(tool: ToolTimingSource, nowMs: number): string {
  const explicitDuration = tool.duration?.trim()
  if (explicitDuration) {
    // 解析 explicitDuration，确保最少显示3秒
    const match = explicitDuration.match(/^(\d+(?:\.\d+)?)s$/)
    if (match) {
      const seconds = Math.floor(parseFloat(match[1]))
      const displaySeconds = Math.max(3, seconds)
      return `${displaySeconds}s`
    }
    return explicitDuration
  }

  const startedAt = parseTime(tool.startedAt)
  if (startedAt === null) return tool.status === 'running' ? '3s' : '--'

  const finishedAt = parseTime(tool.finishedAt)
  const endedAt = tool.status === 'running' || finishedAt === null ? nowMs : finishedAt
  const elapsedMs = Math.max(0, endedAt - startedAt)
  const elapsedSeconds = Math.floor(elapsedMs / 1000)

  // 最少显示3秒，让用户感受到程序在思考
  const displaySeconds = Math.max(3, elapsedSeconds)
  return `${displaySeconds}s`
}

function parseTime(value?: string | null): number | null {
  // Phase 2.9A.35: 无时区字符串按 UTC 解析,避免本地时区漂移
  const parsed = parseApiDateTime(value)
  return Number.isFinite(parsed) ? parsed : null
}

function earliestValidTime(values: Array<string | null | undefined>): string | null {
  let best: { raw: string; ms: number } | null = null
  for (const value of values) {
    if (!value) continue
    const ms = parseTime(value)
    if (ms === null) continue
    if (!best || ms < best.ms) {
      best = { raw: value, ms }
    }
  }
  return best?.raw ?? null
}

function latestValidTime(values: Array<string | null | undefined>): string | null {
  let best: { raw: string; ms: number } | null = null
  for (const value of values) {
    if (!value) continue
    const ms = parseTime(value)
    if (ms === null) continue
    if (!best || ms > best.ms) {
      best = { raw: value, ms }
    }
  }
  return best?.raw ?? null
}

function isNonNegativeFinite(value?: number | null): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
}

function isTerminalTaskStatus(status?: string | null): boolean {
  return status === 'completed' || status === 'failed' || status === 'cancelled' || status === 'canceled'
}

const TASK_TIMING_ACTIVE_STATES = new Set([
  'created',
  'queued',
  'planning',
  'running',
  'waiting',
  'waiting_user_confirm',
  'resuming',
  'generating',
  'reviewing',
  'exporting',
  'format_loss_review'
])

function formatElapsedDuration(durationMs: number): string {
  const totalSeconds = Math.floor(durationMs / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m ${String(seconds).padStart(2, '0')}s`
  return `${minutes}m ${String(seconds).padStart(2, '0')}s`
}
