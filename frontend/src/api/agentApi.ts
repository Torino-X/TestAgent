/**
 * Agent API - task detail / events / confirm / cancel / retry.
 */

import type { AgentTask, SectionAction, SectionItem, TaskStatus } from '@/types'
import { apiGet, apiPost } from './request'

export interface AgentEventRecord {
  event_id: string
  task_id: string
  event_type: string
  title?: string
  message_type?: string
  status?: string
  sequence_no?: number
  /**
   * Phase 2.9A.26+: stable canonical order across mixed legacy rows
   * (`sequence_no IS NULL`) and modern rows.  Front-end timeline
   * assembly MUST sort by this field; never by raw `sequence_no`.
   */
  canonical_order?: number
  graph_run_id?: string
  graph_version?: string
  node_name?: string
  event_schema_version?: number
  content?: string
  payload?: unknown
  created_at?: string
}

interface ApiPage<T> {
  items: T[]
  total?: number
}

interface ApiEventPage {
  events: AgentEventRecord[]
  total?: number
  /**
   * Phase 2.9A.27: cursor 分页字段。
   * - 下次请求带 ``?cursor={next_cursor}`` 拉下一批
   * - ``null`` 表示已拉完
   */
  next_cursor?: number | null
}

interface ApiTask {
  task_id: string
  task_type?: 'test_plan_generation'
  status: TaskStatus
  events_url?: string
  active_run_id?: string | null
  runtime_status?: string | null
  started_at?: string | null
  completed_at?: string | null
  duration_ms?: number | null
  /** Phase 2.9A.30: trigger message public_id (string or null) */
  trigger_message_id?: string | null
  run?: {
    run_id: string
    status: string
    started_at?: string | null
    finished_at?: string | null
    duration_ms?: number | null
  } | null
}

export interface PendingConfirmation {
  confirmationId: string
  confirmationType: string
  sections: SectionItem[]
}

interface ConfirmationClarificationCard {
  id: string
  question: string
}

export interface ConfirmedConfirmation {
  confirmationId: string
  confirmationType: string
  request: {
    sections?: SectionItem[]
    cards?: ConfirmationClarificationCard[]
  }
  response: {
    sections?: SectionItem[]
    answers?: Record<string, string>
    conservativeGapIds?: string[]
  }
  confirmedAt?: string
}

interface ApiPendingConfirmation {
  confirmation_id: string
  confirmation_type: string
  status: string
  request?: {
    sections?: ApiSection[]
  }
  sections?: ApiSection[]
}

interface ApiConfirmedConfirmation {
  confirmation_id: string
  confirmation_type: string
  request?: {
    sections?: ApiSection[]
    cards?: Array<{ id?: string; question?: string }>
  }
  response?: {
    sections?: ApiSection[]
    answers?: Record<string, string>
    conservative_gap_ids?: string[]
  }
  confirmed_at?: string | null
}

interface ApiSection {
  section_id?: string
  id?: string
  section_number?: string
  code?: string
  section_title?: string
  title?: string
  level?: string
  suggested_action?: SectionAction
  suggestedAction?: SectionAction
  action?: SectionAction
  reason?: string
  description?: string
}

interface ConfirmTaskOptions {
  confirmationId?: string
  confirmationType?: string
}

interface ApiConfirmTaskResponse {
  task_id: string
  confirmation_id?: string
  status?: string
  task_status?: TaskStatus
  events_url?: string
}

export type RetryMode = 'from_failed_step' | 'from_beginning'

export type FormatLossDecision = 'accept' | 'retry'

export interface PreparationClarificationResponse {
  task_id: string
  status: string
  events_url?: string
}

export interface FormatLossDecisionResponse {
  task_id: string
  decision: FormatLossDecision | 'timeout'
  next_action: 'complete' | 're_export'
  fallback_to_accept: boolean
}

function mapTask(task: ApiTask): AgentTask {
  return {
    task_id: task.task_id,
    task_type: task.task_type ?? 'test_plan_generation',
    status: task.status,
    events_url: task.events_url ?? `/api/agent/tasks/${task.task_id}/events`,
    activeRunId: task.active_run_id,
    runtimeStatus: task.runtime_status,
    startedAt: task.started_at,
    completedAt: task.completed_at,
    durationMs: task.duration_ms,
    triggerMessageId: task.trigger_message_id ?? null,
    run: task.run
      ? {
          runId: task.run.run_id,
          status: task.run.status,
          startedAt: task.run.started_at,
          finishedAt: task.run.finished_at,
          durationMs: task.run.duration_ms
        }
      : null
  }
}

function mapSection(section: ApiSection, index: number): SectionItem {
  const suggestedAction = section.suggested_action ?? section.suggestedAction ?? 'ai_generate'
  return {
    id: section.section_id ?? section.id ?? `section_${index}`,
    code: section.section_number ?? section.code ?? String(index + 1),
    title: section.section_title ?? section.title ?? `章节 ${index + 1}`,
    level: section.level ?? 'H1',
    suggestedAction,
    action: section.action ?? suggestedAction,
    reason: section.reason ?? section.description ?? ''
  }
}

function mapPendingConfirmation(data: ApiPendingConfirmation): PendingConfirmation {
  const sections = data.request?.sections ?? data.sections ?? []
  return {
    confirmationId: data.confirmation_id,
    confirmationType: data.confirmation_type,
    sections: sections.map(mapSection)
  }
}

function mapConfirmedConfirmation(data: ApiConfirmedConfirmation): ConfirmedConfirmation {
  return {
    confirmationId: data.confirmation_id,
    confirmationType: data.confirmation_type,
    request: {
      sections: (data.request?.sections ?? []).map(mapSection),
      cards: (data.request?.cards ?? [])
        .map((card, index) => ({
          id: card.id ?? `clarification_${index}`,
          question: card.question ?? ''
        }))
        .filter((card) => card.question)
    },
    response: {
      sections: (data.response?.sections ?? []).map(mapSection),
      answers: data.response?.answers ?? {},
      conservativeGapIds: data.response?.conservative_gap_ids ?? []
    },
    confirmedAt: data.confirmed_at ?? undefined
  }
}

export async function fetchTaskDetail(taskId: string): Promise<AgentTask> {
  const task = await apiGet<ApiTask>(`/api/agent/tasks/${taskId}`)
  return mapTask(task)
}

/**
 * Phase 2.9A.27: 拉取任务的全部 events(cursor 循环)。
 *
 * 旧 ``fetchTaskEvents`` 默认只返回 50 条,会把 seq>50 的 task_completed /
 * docx_format_checked 等关键事件截掉,导致 AgentRunCard.runState 退化为
 * running。新函数 while-loop 直到 ``next_cursor === null``,确保 Hydration
 * 拉完所有事件。
 *
 * 设计说明:
 *  - 每次最多 100 条(> 历史 50 默认),整段任务事件数很少超过 200,大多数
 *    1-3 次请求即可拉完。
 *  - 防御:硬上限 5000 条,挡住失控递归 / 异常分页元数据。
 *  - 失败时把累计的 events 一并抛出,让 store 可以部分恢复。
 */
export interface FetchAllTaskEventsResult {
  events: AgentEventRecord[]
  /** Phase 2.9A.35: 最后一次分页的 next_cursor(独立于 SSE cursor)。 */
  lastPageCursor?: number | null
}

export async function fetchAllTaskEvents(
  taskId: string,
  options: { limit?: number; maxTotal?: number; returnPageCursor?: boolean } = {}
): Promise<AgentEventRecord[] | FetchAllTaskEventsResult> {
  const limit = options.limit ?? 100
  const hardCap = options.maxTotal ?? 5000
  const accumulated: AgentEventRecord[] = []
  let cursor: number | null = null
  let lastPageCursor: number | null = null
  // 防御性硬上限,防止 next_cursor 永远不为 null 时的死循环
  for (let safety = 0; safety < 200; safety += 1) {
    const url: string = cursor === null
      ? `/api/agent/tasks/${taskId}/event-list?limit=${limit}`
      : `/api/agent/tasks/${taskId}/event-list?limit=${limit}&cursor=${cursor}`
    const page: AgentEventRecord[] | ApiEventPage = await apiGet<AgentEventRecord[] | ApiEventPage>(url)
    const events: AgentEventRecord[] = Array.isArray(page) ? page : page.events
    const nextCursor: number | null | undefined = Array.isArray(page)
      ? null
      : page.next_cursor ?? null
    // 终止条件 — repo 把 next_cursor 永远设为本批最后一条 canonical_order;
    // 调用方从以下两个信号判定:
    //   1. 本批 events 为空(cursor 越过最大 canonical_order 时返回)
    //   2. 累计已达 total(从首次响应拿到的 total 字段)
    if (events.length === 0) break
    accumulated.push(...events)
    if (nextCursor !== null && nextCursor !== undefined) lastPageCursor = nextCursor
    if (accumulated.length >= hardCap) break
    if (nextCursor === null || nextCursor === undefined) break
    cursor = nextCursor
  }
  if (options.returnPageCursor) {
    return { events: accumulated, lastPageCursor }
  }
  return accumulated
}

export async function fetchTaskEvents(taskId: string): Promise<AgentEventRecord[]> {
  const page = await apiGet<AgentEventRecord[] | ApiPage<AgentEventRecord> | ApiEventPage>(`/api/agent/tasks/${taskId}/event-list`)
  return Array.isArray(page) ? page : ('events' in page ? page.events : page.items)
}

export async function fetchPendingConfirmation(taskId: string): Promise<PendingConfirmation> {
  const pending = await apiGet<ApiPendingConfirmation>(`/api/agent/tasks/${taskId}/pending-confirmation`)
  return mapPendingConfirmation(pending)
}

export async function fetchConfirmedConfirmations(taskId: string): Promise<ConfirmedConfirmation[]> {
  const data = await apiGet<{ confirmations?: ApiConfirmedConfirmation[] }>(
    `/api/agent/tasks/${taskId}/confirmed-confirmations`
  )
  return (data.confirmations ?? []).map(mapConfirmedConfirmation)
}

/**
 * 带重试的 pending-confirmation 查询。
 * 用于实时 SSE 链路:节点层 commit 与事件发布之间存在微小竞态窗口
 * (commit 早于 SSE 抵达,但前端可能在 commit 完成前发起首次查询)。
 * 200ms / 500ms / 1000ms 退避,最多 3 次,总共不超过 1.7 秒。
 */
export async function fetchPendingConfirmationWithRetry(
  taskId: string,
  options?: { signal?: AbortSignal }
): Promise<PendingConfirmation | null> {
  const delays = [0, 200, 500]
  let lastError: unknown = null
  for (let attempt = 0; attempt < delays.length; attempt++) {
    if (options?.signal?.aborted) return null
    if (delays[attempt] > 0) {
      await new Promise<void>((resolve) => setTimeout(resolve, delays[attempt]))
      if (options?.signal?.aborted) return null
    }
    try {
      return await fetchPendingConfirmation(taskId)
    } catch (err) {
      lastError = err
    }
  }
  console.warn('[fetchPendingConfirmationWithRetry] all retries failed', lastError)
  return null
}

export async function confirmTask(
  taskId: string,
  sections: SectionItem[],
  options: ConfirmTaskOptions = {}
): Promise<AgentTask> {
  const response = await apiPost<ApiConfirmTaskResponse>(
    `/api/agent/tasks/${taskId}/confirm`,
    {
      confirmation_type: options.confirmationType ?? 'section_generation_config',
      confirmation_id: options.confirmationId,
      sections: sections.map((section) => ({
        section_id: section.id,
        section_title: section.title,
        action: section.action
      }))
    }
  )

  return mapTask({
    task_id: response.task_id,
    status: response.task_status ?? 'running',
    events_url: response.events_url
  })
}

export async function cancelTask(taskId: string, reason = '用户取消'): Promise<AgentTask> {
  const response = await apiPost<ApiTask>(
    `/api/agent/tasks/${taskId}/cancel`,
    { reason }
  )
  return mapTask(response)
}

export async function retryTask(
  taskId: string,
  retryMode: RetryMode = 'from_failed_step',
  userInstruction?: string
): Promise<AgentTask> {
  const body: Record<string, unknown> = { retry_mode: retryMode }
  if (userInstruction && userInstruction.trim()) {
    body.user_instruction = userInstruction
  }
  const response = await apiPost<ApiTask>(
    `/api/agent/tasks/${taskId}/retry`,
    body
  )
  return mapTask(response)
}

export function createTaskEventSource(taskId: string): { eventsUrl: string } {
  return {
    eventsUrl: `/api/agent/tasks/${taskId}/events`
  }
}

export async function submitFormatLossDecision(
  taskId: string,
  decision: FormatLossDecision,
  note?: string,
): Promise<FormatLossDecisionResponse> {
  const body: Record<string, unknown> = { decision }
  if (note && note.trim()) {
    body.note = note
  }
  return apiPost<FormatLossDecisionResponse>(
    `/api/agent/tasks/${taskId}/format-loss-decision`,
    body,
  )
}

export async function submitPreparationClarification(
  taskId: string,
  answers: Record<string, string>,
  conservativeGapIds: string[]
): Promise<PreparationClarificationResponse> {
  return apiPost<PreparationClarificationResponse>(
    `/api/agent/tasks/${taskId}/preparation-clarification`,
    { answers, conservative_gap_ids: conservativeGapIds }
  )
}
