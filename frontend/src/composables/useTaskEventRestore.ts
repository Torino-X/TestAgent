/**
 * Phase 2.9A.26+: Hydration-mode reducer for agent task events.
 *
 * Distinct from ``useTaskEvents.ts`` which is the *live* reducer:
 *  - Live mode accepts SSE events one at a time and lets the UI render
 *    progressive chunks (animation).
 *  - Hydration mode runs once per conversation refresh with the full
 *    event-list payload from ``GET /agent/tasks/{id}/event-list``.
 *    It returns a single ``RestoredTaskRun`` containing a small list of
 *    fully-formed ``ChatMessage``s that the conversation timeline can
 *    render without re-running any UI animations.
 *
 * Behavioural contract:
 *  - tool_finished / tool_started events with the same ``tool_call_id``
 *    are merged into a single ``tool_call`` message.  The merge keeps
 *    the chunk with the highest ``chunk_index`` (or ``chunk_final=true``)
 *    and prefers its ``publicUpdate`` payload.
 *  - chunked public-execution-update: an event with ``publicUpdate`` is
 *    collapsed into the same parent ``tool_call`` so the timeline does
 *    not contain 5 copies of the same tool narrative.
 *  - Status: the ``runState`` is whatever the AgentTaskService
 *    reported (``completed`` / ``running`` / ``waiting_user_confirm`` /
 *    etc.).  The component decides default collapsed / expanded.
 */

import type { ChatMessage, PublicExecutionUpdate } from '@/types'
import {
  normalizeTaskEventPayload,
  normalizeConfirmationSectionsItems,
  extractPublicExecutionUpdate
} from '@/utils/eventPayload'

/**
 * Server-side event as fetched by ``GET /agent/tasks/{id}/event-list``.
 * Mirrors ``AgentEventRecord`` in the front-end API layer.
 */
export interface RawEventRecord {
  event_id: string
  task_id: string
  event_type: string
  title?: string
  message_type?: string
  status?: string
  sequence_no?: number | null
  canonical_order?: number
  graph_run_id?: string | null
  graph_version?: string | null
  node_name?: string | null
  event_schema_version?: number
  content?: string
  payload?: unknown
  created_at?: string
}

export interface RestoredTaskRun {
  taskId: string
  status: string
  startedAt?: string
  completedAt?: string
  messages: ChatMessage[]
  /** Tool-call message ids that already have a final publicUpdate attached. */
  completeToolCallIds: Set<string>
}

/**
 * Group key for tool events that belong to the same tool invocation.
 *
 * ``tool_call_id`` is the canonical key.  When missing, fall back to
 * ``dedupe_key`` (used by chunked public-execution-update frames).
 */
function toolGroupKey(event: RawEventRecord): string {
  const payload = normalizeTaskEventPayload(event.payload)
  const candidates: unknown[] = [
    payload.tool_call_id,
    payload.toolCallId,
    payload.dedupe_key,
    payload.dedupeKey,
    event.event_id
  ]
  for (const value of candidates) {
    if (typeof value === 'string' && value.length > 0) return value
  }
  // Final fallback: synthesize a stable id from event_type + chunk_index.
  const chunkIndex = Number(payload.chunk_index ?? payload.chunkIndex ?? 0)
  return `${event.event_type}:${chunkIndex}`
}

/**
 * Pick the canonical publicUpdate from a list of payloads.
 * Uses the unified extractPublicExecutionUpdate (v3 扁平 + 嵌套兼容).
 * Returns the payload with the highest chunk_index, preferring
 * ``chunk_final=true`` over chunkIndex alone.
 */
function pickPublicUpdate(payloads: Array<Record<string, unknown>>) {
  let best: { pu: PublicExecutionUpdate; idx: number; final: boolean } | null = null
  for (const p of payloads) {
    const pu = extractPublicExecutionUpdate(p)
    if (!pu) continue
    const idx = pu.chunkIndex ?? 0
    const isFinal = pu.chunkFinal ?? false
    if (
      best === null ||
      (isFinal && !best.final) ||
      (isFinal === best.final && idx > best.idx)
    ) {
      best = { pu, idx, final: isFinal }
    }
  }
  return best ? best.pu : undefined
}

/**
 * Build the ChatMessage representing a tool event.  Two events with
 * the same ``toolGroupKey`` are merged: the later event (higher
 * canonical_order) wins, and any ``publicUpdate`` chunks are folded in.
 */
function buildToolCallMessages(
  grouped: Map<string, RawEventRecord[]>,
  formatTime: (iso: string) => string
): { messages: ChatMessage[]; completeToolCallIds: Set<string> } {
  const messages: ChatMessage[] = []
  const completeToolCallIds = new Set<string>()

  for (const [key, events] of grouped) {
    // Sort by canonical_order ASC so later chunks override earlier.
    events.sort((a, b) => {
      const ao = a.canonical_order ?? a.sequence_no ?? Number.MAX_SAFE_INTEGER
      const bo = b.canonical_order ?? b.sequence_no ?? Number.MAX_SAFE_INTEGER
      return ao - bo
    })

    // Use the last (most recent) event as the canonical row.
    const last = events[events.length - 1]
    const payload = normalizeTaskEventPayload(last.payload)

    // Collect all publicUpdate payloads across the chunks
    const publicUpdatePayloads = events
      .map((e) => normalizeTaskEventPayload(e.payload))
      .filter((p) => extractPublicExecutionUpdate(p))
    const publicUpdate = pickPublicUpdate(publicUpdatePayloads)

    const toolName =
      typeof payload.tool_name === 'string'
        ? payload.tool_name
        : typeof payload.name === 'string'
          ? payload.name
          : last.node_name ?? 'tool'

    const statusValue =
      last.event_type === 'tool_finished'
        ? 'success'
        : last.event_type === 'tool_failed'
          ? 'failed'
          : 'running'

    const id =
      typeof payload.tool_call_id === 'string'
        ? payload.tool_call_id
        : key

    const toolCallMessage: ChatMessage = {
      id,
      type: 'tool_call',
      role: 'agent',
      taskId: last.task_id,
      // Pin the tool message's conversationSequence to the canonical
      // order of its first event so timeline assembly can sort it
      // deterministically alongside plan / summary events.
      conversationSequence: events[0]?.canonical_order ?? events[0]?.sequence_no ?? undefined,
      createdAt: events[0]?.created_at,
      toolCall: {
        id,
        name: toolName,
        status: statusValue as 'running' | 'success' | 'failed',
        input: typeof payload.tool_input === 'string' ? payload.tool_input : '',
        output: typeof payload.tool_output === 'string' ? payload.tool_output : '',
        duration: typeof payload.duration === 'string' ? payload.duration : '',
        startedAt: payload.started_at as string | undefined,
        finishedAt: payload.finished_at as string | undefined,
        publicUpdate
      },
      timestamp: formatTime(events[0]?.created_at ?? new Date().toISOString())
    }
    messages.push(toolCallMessage)

    if (publicUpdate && publicUpdate.chunkFinal) {
      completeToolCallIds.add(id)
    }
  }
  return { messages, completeToolCallIds }
}

/**
 * Map a non-tool event into a single ChatMessage.  These events do not
 * need deduplication: task_resumed / plan_step / requirement_summary /
 * template_summary / review_result / generating_status / section_confirm
 * are 1:1 with their ChatMessage.
 */
function buildSingleEventMessage(
  event: RawEventRecord,
  taskStatus: string,
  formatTime: (iso: string) => string
): ChatMessage | null {
  const payload = normalizeTaskEventPayload(event.payload)
  const id = event.event_id

  switch (event.event_type) {
    case 'task_created':
    case 'task_resumed':
    case 'task_waiting':
    case 'task_completed': {
      // Phase 2.9A.27: 与 Live 模式 useTaskEvents.ts:653-680 对齐:
      // 把 task_completed 翻译为 agent_text ChatMessage,并附 _summaryFacts
      // / artifact 字段,AgentRunCard 第 480-483 行据此计算 runState。
      // 修复前 reducer 在这里 return null,导致 Hydration 永远找不到
      // 'task_completed' 事件消息,runState 退化为 'running',顶部
      // "处理中"+ 默认展开(因为 props.hydrated=true 也没传)。
      const payload = normalizeTaskEventPayload(event.payload)
      const summaryFactsRaw =
        (payload.summary_facts as Record<string, unknown> | undefined) ?? {}
      const artifactRaw =
        (payload.artifact as Record<string, unknown> | undefined) ?? {}
      const formatCheckRaw =
        (payload.format_check as Record<string, unknown> | undefined) ?? {}

      const review = (summaryFactsRaw.review as Record<string, unknown>) ?? {}
      const summaryFacts = {
        generatedSections: Number(summaryFactsRaw.generated_sections ?? 0),
        keptSections: Number(summaryFactsRaw.kept_sections ?? 0),
        businessModules: Number(summaryFactsRaw.business_modules ?? 0),
        review: {
          block_count: Number(review.block_count ?? 0),
          warning_count: Number(review.warning_count ?? 0),
          suggestion_count: Number(review.suggestion_count ?? 0),
          level: (review.level as string | null) ?? null,
          passed: Boolean(review.passed ?? false)
        },
        formatStatus:
          (formatCheckRaw.status as string | null) ??
          ((summaryFactsRaw.artifact as Record<string, unknown> | undefined)
            ?.format_status as string | null) ??
          null
      }
      const artifact = artifactRaw.public_id
        ? {
            id: String(artifactRaw.public_id ?? ''),
            type: String(artifactRaw.artifact_type ?? 'test_plan_word'),
            name: String(artifactRaw.file_name ?? ''),
            size: artifactRaw.file_size ? String(artifactRaw.file_size) : '',
            generatedAt: event.created_at ?? '',
            downloadUrl: (artifactRaw.download_url as string | undefined) ?? ''
          }
        : undefined

      // text 字段使用事件 content(任务完成 summary 字符串),与 Live 模式一致
      const text = event.content ?? ''
      const result: ChatMessage = {
        id,
        type: 'agent_text',
        role: 'agent',
        taskId: event.task_id,
        // eventType 必须显式标注 — AgentRunCard 用 isTaskCompletedMessage
        // (匹配 eventType==='task_completed') 推 runState='completed'。
        eventType: 'task_completed',
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        text,
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
      // 透传 _summaryFacts + artifact,AgentRunCard 第 480-483 行查找
      // ``message.type === 'agent_text' && (message as any)._summaryFacts``
      Object.assign(result, { _summaryFacts: summaryFacts, artifact })
      return result
    }
    case 'task_failed':
    case 'task_cancelled':
      // 终态事件仍然产出一个 agent_text,这样错误卡片 / 取消说明也能展示;
      // eventType 标记具体终态供 AgentRunCard 判断。
      return {
        id,
        type: event.event_type === 'task_failed' ? 'error' : 'agent_text',
        role: 'agent',
        taskId: event.task_id,
        eventType: event.event_type,
        text: event.content ?? '',
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    case 'plan_created': {
      const steps = Array.isArray(payload.steps) ? payload.steps : []
      return {
        id,
        type: 'agent_plan',
        role: 'agent',
        taskId: event.task_id,
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        plan: steps.map((step, index) => {
          const s = step as Record<string, unknown>
          return {
            id: String(s.step_id ?? s.id ?? `step_${index}`),
            title: String(s.name ?? s.title ?? `步骤 ${index + 1}`),
            detail: s.detail as string | undefined,
            status: (s.status as 'pending' | 'running' | 'done' | 'failed') ?? 'pending'
          }
        }),
        createdAt: event.created_at, timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    }
    case 'requirement_summary':
      return {
        id,
        type: 'requirement_summary',
        role: 'agent',
        taskId: event.task_id,
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        summary: payload.summary as ChatMessage['summary'],
        createdAt: event.created_at, timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    case 'template_summary':
      return {
        id,
        type: 'template_summary',
        role: 'agent',
        taskId: event.task_id,
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        summary: payload.summary as ChatMessage['summary'],
        createdAt: event.created_at, timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    case 'knowledge_summary':
      return {
        id,
        type: 'knowledge_summary',
        role: 'agent',
        taskId: event.task_id,
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        summary: payload.summary as ChatMessage['summary'],
        createdAt: event.created_at, timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    case 'need_user_confirm':
      // Phase 2.9A.35: 只 surface 当前等待确认的任务。使用统一契约
      // normalizeConfirmationSectionsItems 解析 sections(兼容嵌套
      // section_suggestions.sections)。
      if (taskStatus !== 'waiting_user_confirm') return null
      return {
        id,
        type: 'section_confirm',
        role: 'agent',
        taskId: event.task_id,
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        confirmationId: payload.confirmation_id as string | undefined,
        confirmationType: payload.confirmation_type as string | undefined,
        sections: normalizeConfirmationSectionsItems(payload),
        createdAt: event.created_at, timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    case 'generating_started':
    case 'generating_progress':
      return {
        id,
        type: 'generating_status',
        role: 'agent',
        taskId: event.task_id,
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        generating: payload.status as ChatMessage['generating'],
        createdAt: event.created_at, timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    case 'review_completed':
      return {
        id,
        type: 'review_result',
        role: 'agent',
        taskId: event.task_id,
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        review: payload.review as ChatMessage['review'],
        createdAt: event.created_at, timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    case 'artifact_created':
      // The artifact is also a Run-Block property; we keep the message
      // so the existing renderer path stays the same.
      return {
        id,
        type: 'artifact_download',
        role: 'agent',
        taskId: event.task_id,
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        artifact: payload.artifact as ChatMessage['artifact'],
        createdAt: event.created_at, timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    case 'format_loss_decision_recorded': {
      const decision = payload.decision === 'retry' || payload.decision === 'reject'
        ? payload.decision
        : 'accept'
      const losses = Array.isArray(payload.losses)
        ? payload.losses
          .map((loss) => {
            if (typeof loss === 'string') return loss.trim()
            if (!loss || typeof loss !== 'object') return ''
            const entry = loss as Record<string, unknown>
            for (const candidate of [entry.message, entry.element, entry.name]) {
              if (typeof candidate === 'string' && candidate.trim()) return candidate.trim()
            }
            return ''
          })
          .filter((loss): loss is string => Boolean(loss))
        : []
      const selection = decision === 'retry'
        ? '重新生成文档并再次校验格式'
        : decision === 'reject'
          ? '放弃本次导出'
          : '接受格式丢失并继续导出'
      const nextStep = decision === 'retry'
        ? '正在重新生成文档并再次校验格式。'
        : decision === 'reject'
          ? '已按你的选择终止本次导出。'
          : '已按你的选择继续完成导出流程。'
      return {
        id,
        type: 'agent_text',
        role: 'agent',
        taskId: event.task_id,
        eventType: 'format_loss_decision_recorded',
        confirmed: true,
        conversationSequence: event.canonical_order ?? event.sequence_no ?? undefined,
        confirmationReceipt: {
          kind: 'format_loss',
          markdown: [
            `- **处理方式：** ${selection}`,
            `- **检测到的格式丢失：** ${losses.join('；') || '检测到的格式项已记录'}`,
            `- **后续流程：** ${nextStep}`
          ].join('\n')
        },
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at ?? new Date().toISOString())
      }
    }
    case 'format_loss_confirm_requested':
      // Format-loss events are handled by the AgentRunCard via the
      // SSE/hydrate path; nothing to render here.
      return null
    default:
      return null
  }
}

/**
 * Build a RestoredTaskRun from the raw event-list payload.
 *
 * The returned ``messages`` are deduped, ordered by canonical_order, and
 * contain exactly one tool_call message per ``tool_call_id`` /
 * ``dedupe_key``.  The caller uses ``completeToolCallIds`` to know which
 * tool calls have already received their final ``chunk_final=true``
 * payload so the timeline does not display "loading" placeholders.
 */
export function restoreTaskRun(
  events: RawEventRecord[],
  taskId: string,
  taskStatus: string,
  startedAt: string | undefined,
  completedAt: string | undefined,
  options: { now?: () => string; formatTime?: (iso: string) => string } = {}
): RestoredTaskRun {
  // The ``formatTime`` hook lets the caller preserve the original
  // event timestamp instead of overwriting with ``Date.now()``.
  // Restored messages must sort alongside user/agent messages by
  // their original time so timeline assembly is stable.
  const formatTime = options.formatTime ?? ((iso: string) => {
    try {
      return new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    } catch {
      return ''
    }
  })

  // Group tool events by tool call id / dedupe_key.
  const toolEvents = new Map<string, RawEventRecord[]>()
  const otherEvents: RawEventRecord[] = []

  for (const event of events) {
    if (!event.event_type.startsWith('tool_')) {
      otherEvents.push(event)
      continue
    }
    const key = toolGroupKey(event)
    const bucket = toolEvents.get(key) ?? []
    bucket.push(event)
    toolEvents.set(key, bucket)
  }

  // Sort the "other" events by canonical_order ASC.
  otherEvents.sort((a, b) => {
    const ao = a.canonical_order ?? a.sequence_no ?? Number.MAX_SAFE_INTEGER
    const bo = b.canonical_order ?? b.sequence_no ?? Number.MAX_SAFE_INTEGER
    return ao - bo
  })

  const toolMessages = buildToolCallMessages(toolEvents, formatTime)
  const otherMessages: ChatMessage[] = []
  for (const event of otherEvents) {
    const msg = buildSingleEventMessage(event, taskStatus, formatTime)
    if (msg) otherMessages.push(msg)
  }

  // Combine and re-sort by canonical_order ASC (stable tiebreaker on
  // event_id).  Tool messages keep the canonical_order of their first
  // event so they line up with plan_started / plan_completed etc.
  const combined = [...toolMessages.messages, ...otherMessages]
  combined.sort((a, b) => {
    const ao = a.conversationSequence ?? 0
    const bo = b.conversationSequence ?? 0
    if (ao !== bo) return ao - bo
    return a.id.localeCompare(b.id)
  })

  return {
    taskId,
    status: taskStatus,
    startedAt,
    completedAt,
    messages: combined,
    completeToolCallIds: toolMessages.completeToolCallIds
  }
}

/**
 * Stable ordering key for messages on the timeline.
 *
 * Phase 2.9A.26+: every ChatMessage carries ``conversation_sequence``
 * (from the server) when it comes from ``/conversations/{id}/messages``.
 * Tasks get a synthetic ordinal derived from their trigger message's
 * ``conversation_sequence + 0.5`` so the run block is anchored
 * immediately after the trigger user message and before any agent
 * reply that uses the same trigger.
 *
 * The pair ``(sequence, secondaryId)`` is a stable key that is safe
 * to use in Array.sort even when many messages share the same second.
 */
export interface TimelineOrder {
  /** Primary order: conversation_sequence for messages, derived for runs. */
  sequence: number
  /** Secondary stable id used as tiebreaker. */
  secondaryId: string
}

export function buildMessageTimelineOrder(
  conversationSequence: number | null | undefined,
  messagePublicId: string
): TimelineOrder {
  return {
    sequence: typeof conversationSequence === 'number' ? conversationSequence : Number.MAX_SAFE_INTEGER,
    secondaryId: messagePublicId
  }
}

export function buildTaskTimelineOrder(
  triggerConversationSequence: number | null | undefined,
  taskPublicId: string
): TimelineOrder {
  // Anchor the task block at trigger_sequence + 0.5 so it slots in
  // immediately after the trigger user message but before the agent
  // reply that owns the same trigger.
  return {
    sequence:
      typeof triggerConversationSequence === 'number'
        ? triggerConversationSequence + 0.5
        : Number.MAX_SAFE_INTEGER,
    secondaryId: taskPublicId
  }
}
