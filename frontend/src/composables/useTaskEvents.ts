import type {
  AgentEventType,
  ChatMessage,
  MessageType,
  PlanStepStatus,
  ToolCallStatus,
  ToolRetryProgress
} from '@/types'
import {
  normalizeTaskEventPayload,
  normalizeConfirmationSectionsItems,
  extractPublicExecutionUpdate
} from '@/utils/eventPayload'
import { displayToolNameFor } from '@/utils/toolCallPresentation'

export function eventTypeToMessageType(eventType: AgentEventType): MessageType | null {
  const map: Partial<Record<AgentEventType, MessageType>> = {
    task_created: 'agent_text',
    plan_created: 'agent_plan',
    tool_started: 'tool_call',
    tool_progress: 'tool_call',
    tool_finished: 'tool_call',
    tool_failed: 'tool_call',
    retrying: 'tool_retry',
    requirement_summary: 'requirement_summary',
    template_summary: 'template_summary',
    knowledge_summary: 'knowledge_summary',
    need_user_confirm: 'section_confirm',
    task_waiting: 'agent_text',
    task_resumed: 'agent_text',
    generating_started: 'generating_status',
    generating_progress: 'generating_status',
    review_completed: 'review_result',
    artifact_created: 'artifact_download',
    task_failed: 'error',
    task_completed: 'agent_text',
    task_cancelled: 'error',
    agent_decision_update: 'agent_text',
    agent_observation_update: 'agent_text',
    // IncrementalAgent events are routed through the unified task reducer.
    // They still need a MessageType here so terminal status callbacks below
    // remain reachable; the reducer decides which of them are user-visible.
    incremental_started: 'agent_text',
    incremental_decision_made: 'agent_text',
    incremental_tool_finished: 'agent_text',
    incremental_tool_blocked: 'agent_text',
    incremental_completed: 'agent_text',
    incremental_failed: 'agent_text',
    incremental_fallback: 'agent_text',
    // Phase 2.9B.4: LLM-first Tool 叙事 — Live 路径映射为 agent_text
    // (携带 _rawEvent 路由进 reducer 归约 toolNarratives),不渲染可见消息。
    tool_narrative_started: 'agent_text',
    tool_narrative_delta: 'agent_text',
    tool_narrative_update: 'agent_text',
    tool_narrative_failed: 'agent_text',
    tool_narrative_fallback: 'agent_text',
    task_summary_narrative_started: 'agent_text',
    task_summary_narrative_delta: 'agent_text',
    task_summary_narrative_update: 'agent_text',
    task_summary_narrative_failed: 'agent_text',
    task_summary_narrative_fallback: 'agent_text',
    // F025-ext: format-loss banner events.  The confirm-requested
    // event is the one that needs to surface as a ChatMessage with
    // ``formatLoss`` set; the recorded/resuming events render as
    // generic agent text notifications.
    format_loss_confirm_requested: 'agent_text',
    format_loss_decision_recorded: 'agent_text',
    format_loss_resuming: 'agent_text'
  }
  return map[eventType] ?? null
}

export interface TaskEventsOptions {
  taskId?: string
  onAppendMessage: (message: ChatMessage) => void
  onUpdateMessage: (messageId: string, patch: Partial<ChatMessage>) => boolean | void
  onStatusChange?: (
    taskId: string,
    status: string,
    meta?: {
      createdAt?: string
      startedAt?: string
      completedAt?: string
      durationMs?: number
      rawEvent?: Record<string, unknown>
    }
  ) => void
  /**
   * Streaming plan-step updates fired BEFORE the plan_created message.
   * Lets the UI flip the relevant fallback plan entry to
   * ``running`` / ``done`` one beat at a time instead of all three
   * appearing at once when plan_created lands.  ``step`` is the
   * backend-supplied identifier (e.g. ``"understand"``, ``"plan"``).
   */
  onPlanStepStatus?: (step: string, status: PlanStepStatus) => void
  /**
   * Phase 2.9A.23 fix-3: Fires AFTER the SSE frame is parsed but BEFORE
   * any business-level filtering (plan_step buffer, chunk dedup, feature
   * flags).  Used to remove the thinking placeholder on the very first
   * event, even if that event is plan_step_started (which early-returns
   * before reaching onAppendMessage).
   */
  onAnyEvent?: (eventType: string, data: unknown) => void
}

export function createTaskEventHandler(opts: TaskEventsOptions) {
  const toolStartedAtByMessageId = new Map<string, string>()
  const toolInputByMessageId = new Map<string, string>()
  // Buffer plan_step_started/completed events that arrive BEFORE the
  // plan_created message.  Once plan_created lands we replay the
  // accumulated overrides into the plan card so the first render of
  // the plan reflects the streaming states the user already saw.
  const pendingPlanStepStatuses = new Map<string, PlanStepStatus>()
  // Section 24-ext — track the highest chunk_index applied per
  // tool_call message so SSE reconnect / late-arriving frames cannot
  // overwrite a more-progressed render.  Each tool_call message id
  // maps to the latest chunk_index the UI has accepted.  When a new
  // frame arrives with a lower (or equal) index, we drop it.
  const toolChunkIndexByMessageId = new Map<string, number>()
  // A final frame closes the public update. Late replay frames must not
  // reopen the same tool narrative after reconnect or history loading.
  const completedToolMessageIds = new Set<string>()
  let currentPlanMessageId = ''
  let currentPlanSteps: NonNullable<ChatMessage['plan']> = []

  function applyPendingStatuses(steps: Record<string, unknown>[]): Record<string, unknown>[] {
    if (pendingPlanStepStatuses.size === 0) return steps
    return steps.map((step) => {
      const id = getString(step, 'step_id', getString(step, 'id'))
      const pendingStatus = id ? pendingPlanStepStatuses.get(id) : undefined
      if (pendingStatus) {
        return { ...step, status: pendingStatus }
      }
      return step
    })
  }

  function buildPlanMessage(record: Record<string, unknown>, eventType: AgentEventType): ChatMessage {
    const payload = asPayloadRecord(record.payload ?? record.payload_json ?? record)
    const steps = applyPendingStatuses(normalizePlanSteps(payload))
    pendingPlanStepStatuses.clear()
    const plan = steps.map((step, index) => ({
      id: getString(step, 'step_id', getString(step, 'id', `step_${index}`)),
      title: getString(step, 'name', getString(step, 'title', `步骤 ${index + 1}`)),
      detail: getString(step, 'detail', getString(step, 'note')),
      status: normalizePlanStepStatus(getString(step, 'status', 'pending'))
    }))
    const id = getFirstString(record, ['event_id', 'public_id'], `msg_evt_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`)
    currentPlanMessageId = id
    currentPlanSteps = plan
    return {
      id,
      type: 'agent_plan',
      role: 'agent',
      eventType,
      createdAt: getFirstString(record, ['created_at', 'timestamp']) || undefined,
      taskId: getFirstString(record, ['task_id', 'taskId'], opts.taskId ?? '') || undefined,
      timestamp: displayTime(getFirstString(record, ['created_at', 'timestamp'])),
      plan
    }
  }

  return function handleEvent(eventName: string, data: unknown) {
    const record = normalizeEventRecord(data)
    const agentEventType = (eventName === 'message'
      ? getFirstString(record, ['event_type', 'type'], eventName)
      : eventName) as AgentEventType | 'stream_phase_done'

    if (agentEventType === 'stream_phase_done') {
      const taskId = getString(record, 'task_id', opts.taskId ?? '')
      const status = getString(record, 'status')
      if (taskId && status) {
        opts.onStatusChange?.(taskId, status, {
          createdAt: getFirstString(record, ['created_at', 'timestamp']) || undefined,
          rawEvent: record
        })
      }
      return
    }

    // Phase 2.9A.23 fix-3: Fire BEFORE all business-level filtering.
    // plan_step_started early-returns at line 138 without reaching onAppendMessage,
    // so any logic placed there cannot react to the first event.  onAnyEvent is
    // guaranteed to fire for every parseable SSE event.
    if (opts.onAnyEvent) opts.onAnyEvent(agentEventType, data)

    // Streaming plan-step updates: buffer the targeted step/status
    // and notify the consumer (UI) so it can flip the fallback plan
    // entry in real time.  When plan_created eventually lands, the
    // buffered statuses are merged into the produced plan card.
    if (agentEventType === 'plan_step_started' || agentEventType === 'plan_step_completed' || agentEventType === 'plan_step_failed') {
      const payload = asPayloadRecord(record.payload ?? record.payload_json ?? record)
      const stepId = getFirstString(payload, ['step', 'step_id', 'stepId'])
      const status = normalizePlanStepEventStatus(agentEventType, getString(payload, 'status'))
      if (stepId && status) {
        pendingPlanStepStatuses.set(stepId, status)
        if (currentPlanMessageId && currentPlanSteps.length > 0) {
          currentPlanSteps = updatePlanStepStatus(currentPlanSteps, stepId, status)
          opts.onUpdateMessage(currentPlanMessageId, { plan: currentPlanSteps })
        }
        opts.onPlanStepStatus?.(stepId, status)
      }
      return
    }

    const msgType = eventTypeToMessageType(agentEventType)

    if (!msgType) {
      console.warn(`[useTaskEvents] Unmapped event type: ${eventName}`, data)
      return
    }

    let message: ChatMessage | null = null
    if (agentEventType === 'plan_created') {
      message = buildPlanMessage(record, agentEventType)
    } else {
      message = eventDataToMessage(data, agentEventType, msgType, opts.taskId)
    }
    // Phase 2.9A.30: attach raw event for store routing
    if (message) {
      message._rawEvent = record as Record<string, unknown>
    }
    if (message) {
      if (msgType === 'tool_call' && message.toolCall) {
        const eventTime = message.createdAt || new Date().toISOString()
        if (agentEventType === 'tool_started') {
          // 对于正在开始的工具，使用当前时间作为 startedAt
          // 而不是历史事件的 created_at
          const now = new Date().toISOString()
          message.toolCall.startedAt = now
          if (message.toolCall.input.trim()) {
            toolInputByMessageId.set(message.id, message.toolCall.input)
          }
          toolStartedAtByMessageId.set(message.id, now)
        } else if (agentEventType === 'tool_progress') {
          message.toolCall.startedAt = toolStartedAtByMessageId.get(message.id)
          if (!message.toolCall.input.trim()) {
            message.toolCall.input = toolInputByMessageId.get(message.id) ?? ''
          }
        } else {
          message.toolCall.startedAt = toolStartedAtByMessageId.get(message.id)
          message.toolCall.finishedAt = eventTime
          if (!message.toolCall.input.trim()) {
            message.toolCall.input = toolInputByMessageId.get(message.id) ?? ''
          }
          toolStartedAtByMessageId.delete(message.id)
        }
      }

      if (msgType === 'tool_call' && agentEventType !== 'tool_started') {
        const publicUpdate = message.toolCall?.publicUpdate
        if (publicUpdate) {
          // Only public narrative frames have chunk coordinates.  A normal
          // tool_finished event commonly arrives first, and treating it as
          // chunk 0 would cause the real first narrative frame to be dropped.
          if (completedToolMessageIds.has(message.id)) return

          const incomingChunkIndex = publicUpdate.chunkIndex ?? 0
          const previousChunkIndex = toolChunkIndexByMessageId.get(message.id) ?? -1
          if (incomingChunkIndex <= previousChunkIndex) {
            // Late narrative frame — ignore.
            return
          }
          toolChunkIndexByMessageId.set(message.id, incomingChunkIndex)
          if (publicUpdate.chunkFinal) {
            completedToolMessageIds.add(message.id)
            toolChunkIndexByMessageId.delete(message.id)
          }
        } else if (completedToolMessageIds.has(message.id)) {
          // Keep the final public narrative authoritative if an ordinary
          // persistence/replay event arrives after it.
          return
        }
        const updated = opts.onUpdateMessage(message.id, message)
        if (!updated) opts.onAppendMessage(message)
      } else {
        opts.onAppendMessage(message)
      }
    }

    const taskId = getFirstString(record, ['task_id', 'taskId'], opts.taskId ?? '')
    const payload = asPayloadRecord(record.payload ?? record.payload_json ?? record)
    const statusMeta = {
      createdAt: getFirstString(record, ['created_at', 'timestamp']) || undefined,
      startedAt: getFirstString(payload, ['started_at', 'startedAt']) || undefined,
      completedAt: getFirstString(payload, ['completed_at', 'completedAt']) || undefined,
      durationMs: getOptionalNumber(payload, ['duration_ms', 'durationMs']),
      rawEvent: record
    }
    if (agentEventType === 'incremental_started' && taskId) opts.onStatusChange?.(taskId, 'running', statusMeta)
    if (agentEventType === 'task_resumed' && taskId) opts.onStatusChange?.(taskId, 'running', statusMeta)
    if (agentEventType === 'task_waiting' && taskId) opts.onStatusChange?.(taskId, 'waiting_user_confirm', statusMeta)
    if (agentEventType === 'format_loss_confirm_requested' && taskId) {
      opts.onStatusChange?.(taskId, 'waiting_user_confirm', statusMeta)
    }
    if (agentEventType === 'task_completed' && taskId) opts.onStatusChange?.(taskId, 'completed', statusMeta)
    if (agentEventType === 'task_failed' && taskId) opts.onStatusChange?.(taskId, 'failed', statusMeta)
    if (agentEventType === 'task_cancelled' && taskId) opts.onStatusChange?.(taskId, 'cancelled', statusMeta)
    // 增量任务终态事件(subgraph.py emit incremental_completed/failed)
    // 必须触发 onStatusChange,否则 SSE 流里看不到 task_completed/failed,
    // removeTaskThinkingPlaceholders 永远不调用,前端卡在「正在思考...」。
    if (agentEventType === 'incremental_completed' && taskId) opts.onStatusChange?.(taskId, 'completed', statusMeta)
    if (agentEventType === 'incremental_failed' && taskId) opts.onStatusChange?.(taskId, 'failed', statusMeta)
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {}
}

function normalizeEventRecord(value: unknown): Record<string, unknown> {
  const record = asPayloadRecord(value)
  const nested = asPayloadRecord(record.data)
  return Object.keys(nested).length > 0 ? { ...record, ...nested } : record
}

function asPayloadRecord(value: unknown): Record<string, unknown> {
  return normalizeTaskEventPayload(value)
}

function getString(record: Record<string, unknown>, key: string, fallback = ''): string {
  const value = record[key]
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback
}

function getFirstString(record: Record<string, unknown>, keys: string[], fallback = ''): string {
  for (const key of keys) {
    const value = getString(record, key)
    if (value) return value
  }
  return fallback
}

function getNumber(record: Record<string, unknown>, key: string, fallback = 0): number {
  const value = record[key]
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return fallback
}

function getOptionalNumber(record: Record<string, unknown>, keys: string[]): number | undefined {
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (typeof value === 'string' && value.trim()) {
      const parsed = Number(value)
      if (Number.isFinite(parsed)) return parsed
    }
  }
  return undefined
}

function getBoolean(record: Record<string, unknown>, key: string, fallback = false): boolean {
  const value = record[key]
  if (typeof value === 'boolean') return value
  if (typeof value === 'string' && value.trim()) {
    const normalized = value.trim().toLowerCase()
    if (['1', 'true', 'yes', 'on'].includes(normalized)) return true
    if (['0', 'false', 'no', 'off'].includes(normalized)) return false
  }
  if (typeof value === 'number' && Number.isFinite(value)) return value !== 0
  return fallback
}

function displayTime(value?: string): string {
  if (!value) return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function getFirstValue(record: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key]
  }
  return undefined
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value, null, 2)
}

/**
 * Phase 2.9A.25: Normalize artifact payload from SSE events into the
 * canonical Artifact model.  Handles both flat (legacy) and nested
 * (finalize_task_node) structures.
 *
 * Priority: public_id → publicId → artifact_id → id
 */
function normalizeArtifactPayload(raw: unknown): { publicId: string; filename: string; mimeType?: string; sizeBytes?: number; downloadUrl?: string } | null {
  const r = asPayloadRecord(raw)
  const publicId = getString(r, 'public_id') || getString(r, 'publicId') || getString(r, 'artifact_id') || getString(r, 'id')
  if (!publicId) return null
  return {
    publicId,
    filename: getString(r, 'file_name', getString(r, 'filename', '测试方案.docx')),
    mimeType: getString(r, 'mime_type') || undefined,
    sizeBytes: getNumber(r, 'file_size') || undefined,
    downloadUrl: getString(r, 'download_url') || undefined,
  }
}

function formatToolFailureOutput(payload: Record<string, unknown>): string {
  // The legacy channel — error comes in as a plain string field on the payload.
  // Keep that case readable so existing event payloads (e.g. tool_failed with
  // error: "知识库连接失败") keep their message in the UI.
  if (typeof payload.error === 'string' && payload.error.trim()) {
    return payload.error
  }
  const error = asRecord(payload.error)
  const detail = getFirstValue(error, ['details', 'detail', 'message'])
    ?? getFirstValue(payload, ['error_details', 'error_detail', 'error_message', 'message', 'summary', 'output', 'text'])
  const code = getString(error, 'code', getString(payload, 'error_code'))
  const lines = [code ? `错误码: ${code}` : '', formatValue(detail)].filter(Boolean)
  return lines.join('\n') || 'Tool execution failed'
}

function formatDuration(payload: Record<string, unknown>): string {
  const rawMs = getFirstValue(payload, ['duration_ms', 'elapsed_ms'])
  const durationMs = typeof rawMs === 'number'
    ? rawMs
    : typeof rawMs === 'string' && rawMs.trim()
      ? Number(rawMs)
      : NaN
  if (Number.isFinite(durationMs)) {
    const seconds = Math.floor(durationMs / 1000)
    // 最少显示3秒，让用户感受到程序在思考
    const displaySeconds = Math.max(3, seconds)
    return `${displaySeconds}s`
  }
  return getFirstString(payload, ['duration', 'elapsed'])
}

function toolMessageId(data: Record<string, unknown>, payload: Record<string, unknown>, fallbackTaskId = 'task'): string {
  const explicitId = getFirstString(payload, ['tool_call_id', 'toolCallId'], getFirstString(data, ['tool_call_id', 'toolCallId']))
  if (explicitId) return `tool_${explicitId}`
  const taskId = getFirstString(data, ['task_id', 'taskId'], fallbackTaskId)
  const toolName = inferToolName(data, payload).replace(/\s+/g, '')
  return `tool_${taskId}_${toolName}`
}

function inferToolName(data: Record<string, unknown>, payload: Record<string, unknown>): string {
  const explicit = getFirstString(
    payload,
    ['tool_name', 'toolName'],
    getFirstString(data, ['tool_name', 'toolName'])
  )
  if (explicit) return explicit
  const title = getString(data, 'title', getString(data, 'content'))
  const match = title.match(/^([\w.-]+Tool)\b/)
  return match?.[1] ?? 'AgentTool'
}

function normalizePlanSteps(payload: Record<string, unknown>): Record<string, unknown>[] {
  const rawPlan = payload.plan
  if (Array.isArray(payload.steps)) return payload.steps.map(normalizePlanStep)
  if (Array.isArray(rawPlan)) return rawPlan.map(normalizePlanStep)
  const planRecord = asRecord(rawPlan)
  if (Array.isArray(planRecord.steps)) return planRecord.steps.map(normalizePlanStep)
  return []
}

const PLAN_STEP_DEFINITIONS: Record<string, { title: string }> = {
  understand: { title: '理解任务' },
  plan: { title: '生成执行计划' },
  parse_requirement: { title: '解析需求文档' },
  parse_template: { title: '解析测试方案模板' },
  search_knowledge: { title: '检索公司知识库' },
  suggest_sections: { title: '生成章节处理建议' },
  wait_user_confirm: { title: '确认章节生成范围' },
  generate: { title: '生成测试方案' },
  review: { title: '审查生成结果' },
  export: { title: '导出 Word 文档' }
}

function normalizePlanStep(rawStep: unknown, index: number): Record<string, unknown> {
  if (typeof rawStep === 'string' || typeof rawStep === 'number') {
    const id = String(rawStep)
    return {
      step_id: id,
      name: PLAN_STEP_DEFINITIONS[id]?.title ?? id,
      status: 'pending'
    }
  }

  const step = asRecord(rawStep)
  const id = getFirstString(step, ['step_id', 'stepId', 'id', 'step'], `step_${index}`)
  return {
    ...step,
    step_id: id,
    name: getFirstString(step, ['name', 'title'], PLAN_STEP_DEFINITIONS[id]?.title ?? `步骤 ${index + 1}`),
    status: normalizePlanStepStatus(step.status)
  }
}

function normalizePlanStepEventStatus(
  eventType: string,
  rawStatus: string
): PlanStepStatus {
  if (!rawStatus) {
    if (eventType === 'plan_step_started') return 'running'
    if (eventType === 'plan_step_failed') return 'failed'
    return 'done'
  }
  return normalizePlanStepStatus(rawStatus)
}

function normalizePlanStepStatus(raw: unknown): PlanStepStatus {
  const status = typeof raw === 'string' ? raw : ''
  if (status === 'done' || status === 'completed' || status === 'success') return 'done'
  if (status === 'running' || status === 'in_progress' || status === 'waiting' || status === 'waiting_user') return 'running'
  if (status === 'failed' || status === 'error') return 'failed'
  if (status === 'superseded' || status === 'skipped') return 'superseded'
  return 'pending'
}

function updatePlanStepStatus(
  steps: NonNullable<ChatMessage['plan']>,
  stepId: string,
  status: PlanStepStatus
): NonNullable<ChatMessage['plan']> {
  return steps.map((step) => {
    if (step.id !== stepId) return step
    if (status === 'running' && (step.status === 'done' || step.status === 'failed')) return step
    return { ...step, status }
  })
}

function eventDataToMessage(
  rawData: unknown,
  eventType: AgentEventType,
  msgType: MessageType,
  fallbackTaskId = ''
): ChatMessage | null {
  const data = normalizeEventRecord(rawData)
  const payload = asPayloadRecord(data.payload ?? data.payload_json ?? data)
  const createdAt = getFirstString(data, ['created_at', 'timestamp'])
  const timestamp = displayTime(createdAt)
  const taskId = getFirstString(data, ['task_id', 'taskId'], fallbackTaskId)
    const base: ChatMessage = {
      id: getFirstString(data, ['event_id', 'public_id'], `msg_evt_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`),
      type: msgType,
      role: 'agent',
      eventType,
      createdAt: createdAt || undefined,
      taskId: taskId || undefined,
      timestamp
  }

  switch (msgType) {
    case 'agent_plan': {
      const steps = normalizePlanSteps(payload)
      return {
        ...base,
        plan: steps.map((step, index) => ({
          id: getString(step, 'step_id', getString(step, 'id', `step_${index}`)),
          title: getString(step, 'name', getString(step, 'title', `步骤 ${index + 1}`)),
          detail: getString(step, 'detail', getString(step, 'note')),
          status: getString(step, 'status', 'pending') as PlanStepStatus
        }))
      }
    }
    case 'tool_call': {
      // BUG FIX 2026-08-18 (方案 2):tool_finished 且 level=warning(如
      // 知识库降级完成)→ status='warning',让时间线渲染黄色而非绿色。
      const publicUpdate = extractPublicExecutionUpdate(payload)
      const warningLevel = publicUpdate?.level === 'warning' || payload.level === 'warning'
      const status: ToolCallStatus =
        eventType === 'tool_failed' ? 'failed'
        : eventType === 'tool_finished' && warningLevel ? 'warning'
        : eventType === 'tool_finished' ? 'success'
        : 'running'
      const toolCallId = getFirstString(payload, ['tool_call_id', 'toolCallId'], getFirstString(data, ['tool_call_id', 'toolCallId'], base.id))
      const toolName = inferToolName(data, payload)
      return {
        ...base,
        id: toolMessageId(data, payload, fallbackTaskId || 'task'),
        toolCall: {
          id: toolCallId,
          name: toolName,
          status,
          input: formatValue(getFirstValue(payload, ['display_input', 'displayInput', 'input', 'tool_input', 'toolInput', 'input_summary', 'inputSummary', 'args', 'arguments'])),
          output: status === 'failed'
            ? formatToolFailureOutput(payload)
            : formatValue(getFirstValue(payload, ['display_output', 'displayOutput', 'output', 'tool_output', 'toolOutput', 'output_summary', 'outputSummary', 'result', 'summary', 'error', 'text', 'content'])),
          duration: formatDuration(payload),
          displayName: getFirstString(payload, ['display_tool_name', 'displayToolName', 'tool_display_name', 'toolDisplayName']) || displayToolNameFor(toolName),
          businessAction: getFirstString(payload, ['business_action', 'businessAction']) || undefined,
          businessSubjectType: getFirstString(payload, ['business_subject_type', 'businessSubjectType']) || undefined,
          businessSubjectName: getFirstString(payload, ['business_subject_name', 'businessSubjectName']) || undefined,
          filePublicId: getFirstString(payload, ['file_public_id', 'filePublicId', 'file_id', 'fileId']) || undefined,
          fileName: getFirstString(payload, ['file_name', 'fileName', 'filename']) || undefined,
          progressMessage: getFirstString(payload, ['progress_message', 'progressMessage']) || undefined,
          completionMessage: getFirstString(payload, ['completion_message', 'completionMessage']) || undefined,
          errorSummary: getFirstString(payload, ['error_summary', 'errorSummary']) || undefined,
          publicUpdate,
          narrativeExpected: getBoolean(payload, 'narrative_expected', getBoolean(payload, 'narrativeExpected', false))
        }
      }
    }
    case 'requirement_summary':
    case 'template_summary':
    case 'knowledge_summary':
      return {
        ...base,
        summary: {
          title: getFirstString(payload, ['title', 'text'], '解析摘要'),
          description: getFirstString(payload, ['text', 'content']),
          metrics: Object.entries(payload)
            .filter(([key, value]) => key !== 'text' && key !== 'title' && ['string', 'number'].includes(typeof value))
            .map(([label, value]) => ({ label, value: String(value) }))
        }
      }
    case 'section_confirm':
      return {
        ...base,
        confirmationId: getString(payload, 'confirmation_id'),
        confirmationType: getString(payload, 'confirmation_type', 'section_generation_config'),
        sections: normalizeConfirmationSectionsItems(payload)
      }
    case 'generating_status':
      return {
        ...base,
        generating: {
          title: getString(payload, 'title', '正在生成测试方案'),
          currentStep: getString(payload, 'current_step', getString(payload, 'text')),
          currentSection: getString(payload, 'current_section'),
          progress: getNumber(payload, 'progress', getNumber(payload, 'progress_percent'))
        }
      }
    case 'review_result':
      return {
        ...base,
        review: {
          passed: getString(payload, 'level') !== 'error',
          businessModules: getNumber(payload, 'business_module_count'),
          generatedSections: getNumber(payload, 'generated_section_count'),
          keptSections: getNumber(payload, 'kept_section_count'),
          manualSections: Array.from({ length: getNumber(payload, 'manual_section_count') }, (_, index) => `manual_${index + 1}`),
          risks: getString(payload, 'text') ? [getString(payload, 'text')] : []
        }
      }
    case 'artifact_download': {
      // Phase 2.9A.25: Handle both flat payload and nested payload.artifact
      const norm = normalizeArtifactPayload(payload) ?? normalizeArtifactPayload(payload.artifact)
      if (!norm) return null
      return {
        ...base,
        artifact: {
          id: norm.publicId,
          type: 'test_plan_word',
          name: norm.filename,
          size: norm.sizeBytes ? String(norm.sizeBytes) : '',
          generatedAt: timestamp,
          downloadUrl: norm.downloadUrl
        }
      }
    }
    case 'error':
      return { ...base, text: getString(payload, 'text', getString(payload, 'error', getString(data, 'message', '任务执行失败。'))) }
    case 'agent_text': {
      const text = eventType === 'task_completed'
        ? getString(
            payload,
            'summary',
            getString(payload, 'completion_summary', getString(payload, 'text', getString(data, 'message', getString(data, 'content'))))
          )
        : getString(payload, 'text', getString(payload, 'content', getString(data, 'message', getString(data, 'content'))))
      if (eventType === 'agent_decision_update' || eventType === 'agent_observation_update') {
        const update = asPayloadRecord(
          payload.public_update ??
            payload.publicUpdate ??
            data.public_update ??
            data.publicUpdate
        )
        const narrative = [
          getFirstString(update, ['headline', 'title']),
          getFirstString(update, ['summary', 'text', 'content']),
          getFirstString(update, ['impact']),
          getFirstString(update, ['next_action', 'nextAction'])
        ].filter(Boolean).join('\n')
        const fallback = getFirstString(data, ['content', 'message', 'text'])
        return {
          ...base,
          text: narrative || fallback || (
            eventType === 'agent_decision_update'
              ? 'Agent decision updated.'
              : 'Agent observation updated.'
          )
        }
      }
      // Phase 2.9B.4: Tool 叙事流式事件 — 只携带 _rawEvent 供 reducer 归约
      // toolNarratives,不渲染可见 agent_text 消息(叙事锚定在 Tool 卡片下)。
      if (eventType.startsWith('tool_narrative_') || eventType.startsWith('task_summary_narrative_')) {
        return { ...base, text: '' }
      }
      if (eventType === 'format_loss_confirm_requested') {
        const losses = Array.isArray(payload.losses) ? payload.losses.map(asRecord) : []
        const choices = Array.isArray(payload.choices) ? payload.choices.map(asRecord) : []
        return {
          ...base,
          text: getString(payload, 'text', getString(payload, 'title', '检测到格式丢失，请确认如何处理。')),
          formatLoss: {
            taskId,
            losses: losses.map((entry) => ({
              element: getString(entry, 'element'),
              expected: entry.expected,
              actual: entry.actual,
              message: getString(entry, 'message')
            })),
            lossCount: getNumber(payload, 'loss_count', losses.length),
            summary: getString(payload, 'loss_details_for_user', getString(payload, 'summary')),
            choices: choices.map((choice) => ({
              id: getString(choice, 'id') === 'retry' ? 'retry' : 'accept',
              label: getString(choice, 'label'),
              description: getString(choice, 'description')
            })),
            timeoutAt: getString(payload, 'timeout_at')
          }
        }
      }
      if (eventType === 'format_loss_decision_recorded') {
        return {
          ...base,
          text: getString(
            payload, 'text',
            getString(payload, 'title',
              `已记录格式丢失决定：${getString(payload, 'decision')}`)
          )
        }
      }
      if (eventType === 'format_loss_resuming') {
        return {
          ...base,
          text: getString(
            payload, 'text',
            getString(payload, 'title', '正在恢复导出流程…')
          )
        }
      }
      // Phase 2.9A.25: task_completed 返回时附加 artifact + summary_facts
      if (eventType === 'task_completed') {
        const norm = normalizeArtifactPayload(payload.artifact)
        const artifactMsg: Partial<ChatMessage> = {}
        if (norm) {
          artifactMsg.artifact = {
            id: norm.publicId,
            type: 'test_plan_word',
            name: norm.filename,
            size: norm.sizeBytes ? String(norm.sizeBytes) : '',
            generatedAt: timestamp,
            downloadUrl: norm.downloadUrl
          }
        }
        // Attach summary_facts as extra metadata on the message for the UI to render
        const summaryFacts = asPayloadRecord(payload.summary_facts)
        const formatCheck = asPayloadRecord(payload.format_check)
        const artifactFacts = asPayloadRecord(summaryFacts.artifact)
        const extraFields: Record<string, unknown> = {}
        if (Object.keys(summaryFacts).length > 0) {
          extraFields._summaryFacts = {
            generatedSections: summaryFacts.generated_sections ?? 0,
            keptSections: summaryFacts.kept_sections ?? 0,
            businessModules: summaryFacts.business_modules ?? 0,
            review: summaryFacts.review ?? {},
            formatStatus: formatCheck.status ?? artifactFacts.format_status ?? null,
          }
        }
        return text ? { ...base, text, ...artifactMsg, ...extraFields } as ChatMessage : null
      }
      return text ? { ...base, text } : null
    }
    case 'tool_retry': {
      // RETRYING SSE event from orchestrator's _run_tool_with_retry.
      // Carries RetryDecision.to_sse_payload() + last_error message +
      // backend-built public_execution_update.
      const toolName = inferToolName(data, payload)
      const toolCallId = getFirstString(payload, ['tool_call_id', 'toolCallId'], getFirstString(data, ['tool_call_id', 'toolCallId'], toolName))
      return {
        ...base,
        id: `retry_${toolCallId}_${getNumber(payload, 'attempt', Date.now())}`,
        toolRetry: {
          toolName,
          attempt: getNumber(payload, 'attempt', 1),
          maxRetries: getNumber(payload, 'max_retries', 0),
          strategy: getString(payload, 'strategy', 'backoff') as ToolRetryProgress['strategy'],
          reason: getString(payload, 'reason'),
          lastError: getString(payload, 'last_error'),
          backoffSeconds: Number(getFirstValue(payload, ['backoff_seconds']) ?? 0),
          publicUpdate: extractPublicExecutionUpdate(payload)
        }
      }
    }
    default:
      return null
  }
}
