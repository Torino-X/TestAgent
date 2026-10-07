/**
 * Phase 2.9A.30: Unified TaskRunReducer.
 *
 * Single reducer for both hydrate and live modes.
 * - hydrate: batch processing, no animations
 * - live: incremental, triggers UI updates
 *
 * Contracts:
 * - normalizeTaskEvent() is a pure function (no side effects)
 * - reduceTaskEvent() is the ONLY function that mutates TaskRunBlock state
 * - Both modes produce identical TaskRunBlock structures
 */

import type {
  Artifact,
  ChatMessage,
  DynamicNarrative,
  NormalizedTaskEvent,
  PlanStepStatus,
  PublicExecutionUpdate,
  SummaryFacts,
  TaskPlanStep,
  TaskRunBlock,
  TaskStatus,
  TaskSummaryNarrativeState,
  ToolExecutionViewModel,
  ToolNarrativeState
} from '@/types'
import {
  normalizeTaskEventPayload,
  normalizeConfirmationSectionsItems,
  extractPublicExecutionUpdate,
  mergePublicExecutionUpdate
} from '@/utils/eventPayload'
import { parseApiDateTime } from '@/utils/time'

type ToolPresentationFields = Pick<
  ToolExecutionViewModel,
  | 'displayName'
  | 'businessAction'
  | 'businessSubjectType'
  | 'businessSubjectName'
  | 'filePublicId'
  | 'fileName'
  | 'progressMessage'
  | 'completionMessage'
  | 'errorSummary'
>

// ── normalizeTaskEvent ──────────────────────────────────────────────

/**
 * Normalize a raw SSE/event-list event into NormalizedTaskEvent.
 * Pure function — no side effects, no state mutation.
 *
 * Returns null if the event_id has already been seen (idempotent dedup).
 * Phase 2.9A.35: payload 恒被规范化为 Record<string, unknown>。
 */
export function normalizeTaskEvent(
  block: TaskRunBlock,
  raw: RawEventInput
): NormalizedTaskEvent | null {
  const eventId = raw.event_id ?? ''
  if (!eventId) return null
  // Event-level dedup
  if (block.seenEventIds[eventId]) return null

  return {
    event_id: eventId,
    event_type: raw.event_type ?? '',
    task_id: raw.task_id ?? block.taskId,
    canonical_order: raw.canonical_order,
    sequence_no: raw.sequence_no,
    content: raw.content,
    payload: normalizeTaskEventPayload(raw.payload),
    created_at: raw.created_at,
    title: raw.title,
    status: raw.status,
    graph_run_id: raw.graph_run_id,
    graph_version: raw.graph_version,
    node_name: raw.node_name,
    event_schema_version: raw.event_schema_version,
    reductionOrder: raw.reductionOrder ?? buildEffectiveReductionOrder(raw.event_type ?? '', raw.sequence_no, raw.canonical_order)
  }
}

/** Input shape — accepts both RawEventRecord and SSE record formats. */
export interface RawEventInput {
  event_id?: string
  event_type?: string
  task_id?: string
  canonical_order?: number
  sequence_no?: number | null
  content?: string
  payload?: unknown
  created_at?: string
  title?: string
  status?: string
  graph_run_id?: string | null
  graph_version?: string | null
  node_name?: string | null
  event_schema_version?: number
  /**
   * Phase 2.9A.35: 有效归约顺序 — task_created 强制为 0,
   * Graph 事件按 sequence_no,其它 Legacy 用安全 fallback。
   */
  reductionOrder?: number
}

// ── reduceTaskEvent ─────────────────────────────────────────────────

/**
 * Single reducer — the ONLY function that updates TaskRunBlock state.
 *
 * @param block  Current TaskRunBlock (immutable — returns new object)
 * @param raw    Raw event to process
 * @param mode   'hydrate' for batch, 'live' for incremental
 * @returns      Updated TaskRunBlock (new reference for Vue reactivity)
 */
export function reduceTaskEvent(
  block: TaskRunBlock,
  raw: RawEventInput,
  mode: 'hydrate' | 'live'
): TaskRunBlock {
  const event = normalizeTaskEvent(block, raw)
  if (!event) return block // dedup — return unchanged

  // Phase 2.9A.35: payload 恒为 object(已在 normalizeTaskEvent 规范化)
  const payload = event.payload as Record<string, unknown>
  const now = event.created_at ?? new Date().toISOString()

  // Mark event as seen
  const newSeenEventIds: Record<string, true> = { ...block.seenEventIds, [event.event_id]: true }
  let newEvents = [...block.events, event]
  let newMessages = [...block.messages]
  let newToolExecutions = [...block.toolExecutions]
  let newDynamicNarratives = [...block.dynamicNarratives]
  let newToolNarratives = [...block.toolNarratives]
  let newTaskSummaryNarrative = block.taskSummaryNarrative ?? null
  let newStatus = block.status
  let newSummaryFacts = block.summaryFacts
  let newArtifacts = [...block.artifacts]
  let newReviewResult = block.reviewResult
  let newFormatCheckResult = block.formatCheckResult
  let newLastAppliedCanonicalOrder = block.lastAppliedCanonicalOrder
  let newLastSseSequence = block.lastSseSequence
  let newCurrentPhase = block.currentPhase
  let newStartedAt = block.startedAt
  let newCompletedAt = block.completedAt
  let newDurationMs = block.durationMs

  // ── 三个 cursor 彻底拆分 ──────────────────────────────────────────
  // eventListPageCursor 由调用方在 fetchAllTaskEvents 分页时写入,
  // reducer 不负责推进它(它不属于事件归约,属于分页元数据)。
  const co = event.canonical_order ?? event.sequence_no
  if (typeof co === 'number' && (newLastAppliedCanonicalOrder == null || co > newLastAppliedCanonicalOrder)) {
    newLastAppliedCanonicalOrder = co
  }
  // lastSseSequence 只从 sequence_no 非空值计算(legacy task_created 不得推进)
  if (typeof event.sequence_no === 'number' && (newLastSseSequence == null || event.sequence_no > newLastSseSequence)) {
    newLastSseSequence = event.sequence_no
  }

  // ── 状态单调性: task_created 不得覆盖等待/终态 ───────────────────
  const isTaskCreated = event.event_type === 'task_created'
  const nonOverrideableStatuses: TaskStatus[] = ['waiting_user_confirm', 'completed', 'failed', 'cancelled']
  const statusProtected = isTaskCreated && nonOverrideableStatuses.includes(newStatus)

  // ── need_user_confirm 状态机: 立即进入 section_confirmation ──────
  // 只对尚未终态 / 未恢复的任务生效;历史确认不得覆盖终态或已恢复状态。
  const isNeedUserConfirm = event.event_type === 'need_user_confirm'
  if (isNeedUserConfirm) {
    const terminal = newStatus === 'completed' || newStatus === 'failed' || newStatus === 'cancelled'
    if (!terminal) {
      newStatus = 'waiting_user_confirm'
      newCurrentPhase = payload.confirmation_type === 'preparation_clarification'
        ? 'preparation_clarification'
        : 'section_confirmation'
    }
  }

  switch (event.event_type) {
    // ── Tool lifecycle ────────────────────────────────────────────
    case 'tool_started': {
      if (newStatus !== 'completed' && newStatus !== 'failed' && newStatus !== 'cancelled') {
        newStatus = 'running'
        newCurrentPhase = 'execution'
      }
      const toolResult = reduceToolStarted(block, event, payload, now)
      newToolExecutions = toolResult.toolExecutions
      newMessages = toolResult.messages
      if (block.status === 'waiting_user_confirm') {
        newMessages = removePreparationClarificationMessages(
          removeSectionConfirmationMessages(newMessages)
        )
      }
      newMessages = removeFormatLossMessages(newMessages)
      // Phase 2.9A.35: ResultReviewTool started → 进入 review 阶段
      const toolName = inferToolName(payload, event)
      newMessages = updatePlanMessagesByTool(newMessages, toolName, 'running')
      if (toolName === 'ResultReviewTool') {
        newCurrentPhase = 'review'
      }
      break
    }
    case 'tool_progress': {
      const toolResult = reduceToolProgress(block, event, payload, now)
      newToolExecutions = toolResult.toolExecutions
      newMessages = toolResult.messages
      const toolName = inferToolName(payload, event)
      newMessages = updatePlanMessagesByTool(newMessages, toolName, 'running')
      break
    }
    case 'tool_finished':
    case 'tool_failed': {
      const toolResult = reduceToolFinished(block, event, payload, now)
      newToolExecutions = toolResult.toolExecutions
      newMessages = toolResult.messages
      const toolName = inferToolName(payload, event)
      const final = event.event_type === 'tool_failed' || Boolean(payload.chunk_final ?? payload.chunkFinal ?? true)
      if (final) {
        newMessages = updatePlanMessagesByTool(
          newMessages,
          toolName,
          event.event_type === 'tool_failed' ? 'failed' : 'done'
        )
      }
      break
    }

    // ── Task status transitions ───────────────────────────────────
    case 'task_created':
    case 'task_resumed': {
      if (!statusProtected) {
        newStatus = 'running'
        newCurrentPhase = 'execution'
      }
      if (event.event_type === 'task_resumed') {
        newMessages = removeTransientInteractionMessages(newMessages)
      }
      newStartedAt = newStartedAt ?? taskLifecycleStartedAt(payload, event.created_at)
      const msg = buildTaskLifecycleMessage(event, 'running', payload)
      if (msg) newMessages = [...newMessages, msg]
      break
    }
    case 'task_waiting': {
      // 保持 section_confirmation,不推进到 review
      newStatus = 'waiting_user_confirm'
      if (newCurrentPhase !== 'section_confirmation') newCurrentPhase = 'section_confirmation'
      break
    }
    case 'task_completed': {
      newStatus = 'completed'
      newCurrentPhase = 'completed'
      newStartedAt = newStartedAt ?? taskLifecycleStartedAt(payload)
      newCompletedAt = newCompletedAt ?? taskLifecycleCompletedAt(payload, event.created_at)
      newDurationMs = resolveTaskDurationMs(payload, newStartedAt, newCompletedAt, newDurationMs)
      newMessages = removeTransientInteractionMessages(completePlanStepMessages(newMessages))
      // Extract summary_facts
      newSummaryFacts = extractSummaryFacts(payload)
      // Extract artifact from payload
      const payloadArtifact = extractArtifactFromPayload(payload, now)
      if (payloadArtifact) {
        newArtifacts = mergeArtifacts(newArtifacts, [payloadArtifact])
      }
      // Extract format_check
      const formatCheck = payload.format_check as Record<string, unknown> | undefined
      if (formatCheck) newFormatCheckResult = formatCheck
      // Build completion message
      const msg = buildTaskLifecycleMessage(event, 'completed', payload)
      if (msg) {
        // Attach _summaryFacts and artifact to message for AgentRunCard
        const enriched = {
          ...msg,
          _summaryFacts: newSummaryFacts,
          artifact: payloadArtifact
        }
        newMessages = [...newMessages, enriched as ChatMessage]
      }
      break
    }
    case 'task_failed': {
      newStatus = 'failed'
      newCurrentPhase = 'failed'
      newStartedAt = newStartedAt ?? taskLifecycleStartedAt(payload)
      newCompletedAt = newCompletedAt ?? taskLifecycleCompletedAt(payload, event.created_at)
      newDurationMs = resolveTaskDurationMs(payload, newStartedAt, newCompletedAt, newDurationMs)
      newMessages = removeTransientInteractionMessages(newMessages)
      const msg = buildTaskLifecycleMessage(event, 'failed', payload)
      if (msg) newMessages = [...newMessages, msg]
      break
    }
    case 'task_cancelled': {
      newStatus = 'cancelled'
      newCurrentPhase = 'cancelled'
      newStartedAt = newStartedAt ?? taskLifecycleStartedAt(payload)
      newCompletedAt = newCompletedAt ?? taskLifecycleCompletedAt(payload, event.created_at)
      newDurationMs = resolveTaskDurationMs(payload, newStartedAt, newCompletedAt, newDurationMs)
      newMessages = removeTransientInteractionMessages(newMessages)
      const msg = buildTaskLifecycleMessage(event, 'cancelled', payload)
      if (msg) newMessages = [...newMessages, msg]
      break
    }

    // ── Plan ──────────────────────────────────────────────────────
    case 'plan_created':
    case 'plan_updated': {
      newMessages = upsertPlanMessage(newMessages, event, payload)
      break
    }
    // ── Summaries ─────────────────────────────────────────────────
    case 'requirement_summary':
    case 'template_summary':
    case 'knowledge_summary': {
      const msgType = event.event_type as 'requirement_summary' | 'template_summary' | 'knowledge_summary'
      const msg: ChatMessage = {
        id: event.event_id,
        type: msgType,
        role: 'agent',
        taskId: event.task_id,
        summary: payload.summary as ChatMessage['summary'],
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at)
      }
      newMessages = [...newMessages, msg]
      break
    }

    // ── Section confirm ───────────────────────────────────────────
    case 'need_user_confirm': {
      // Phase 2.9A.35: 不再依赖 task_waiting 前置。need_user_confirm
      // 到达时立即创建确认卡片。使用统一契约解析 sections。
      // 注意: 只对尚未终态 / 未恢复的任务生成确认卡片。历史任务在
      // task_resumed / completed 之后,need_user_confirm 是历史事件,
      // 不得复活可操作的确认卡片。
      const isClarification = payload.confirmation_type === 'preparation_clarification'
      const alreadyResumed = block.status === 'running' && block.messages.some((m) =>
        isClarification ? m.type === 'preparation_clarification' : m.type === 'section_confirm'
      )
      const terminal = block.status === 'completed' || block.status === 'failed' || block.status === 'cancelled'
      if (!terminal && !alreadyResumed) {
        const sections = normalizeConfirmationSectionsItems(payload)
        const rawCards = Array.isArray(payload.clarification_cards) ? payload.clarification_cards : []
        const clarificationCards = rawCards
          .filter((card): card is Record<string, unknown> => typeof card === 'object' && card !== null)
          .map((card, index) => ({
            id: String(card.id ?? `gap_${index}`),
            question: String(card.question ?? ''),
            severity: String(card.severity ?? 'medium'),
            selectionMode: card.selection_mode === 'multiple' || card.selectionMode === 'multiple'
              ? 'multiple' as const
              : 'single' as const,
            options: Array.isArray(card.options)
              ? card.options
                  .filter((option): option is Record<string, unknown> => typeof option === 'object' && option !== null)
                  .map((option, optionIndex) => ({
                    id: String(option.id ?? `option_${optionIndex}`),
                    label: String(option.label ?? ''),
                    description: typeof option.description === 'string' ? option.description : undefined
                  }))
                  .filter((option) => Boolean(option.label))
              : [],
            allowConservativeScope: Boolean(card.allow_conservative_scope ?? card.allowConservativeScope)
          }))
          .filter((card) => Boolean(card.question))
        const rawSummary = payload.retrieval_summary as Record<string, unknown> | undefined
        const company = rawSummary?.company_rag as Record<string, unknown> | undefined
        const project = rawSummary?.project_rag as Record<string, unknown> | undefined
        const text = getFirstString(payload, ['question', 'prompt', 'message', 'content', 'title'], '需要你确认后继续。')
        const msg: ChatMessage = {
          id: event.event_id,
          type: isClarification && clarificationCards.length > 0
            ? 'preparation_clarification'
            : sections.length > 0 ? 'section_confirm' : 'agent_text',
          role: 'agent',
          taskId: event.task_id,
          eventType: event.event_type as ChatMessage['eventType'],
          confirmationId: payload.confirmation_id as string | undefined,
          confirmationType: payload.confirmation_type as string | undefined,
          conversationSequence:
            typeof event.canonical_order === 'number'
              ? event.canonical_order
              : typeof event.sequence_no === 'number'
                ? event.sequence_no
                : undefined,
          sections: sections.length > 0 ? sections : undefined,
          clarification: isClarification && clarificationCards.length > 0
            ? {
                cards: clarificationCards,
                retrievalSummary: {
                  retrievalRound: Number(rawSummary?.retrieval_round ?? 0) || undefined,
                  companyRag: { status: String(company?.status ?? ''), hitCount: Number(company?.hit_count ?? 0) },
                  projectRag: {
                    status: String(project?.status ?? ''),
                    hitCount: Number(project?.hit_count ?? 0),
                    reason: typeof project?.reason === 'string' ? project.reason : undefined
                  }
                }
              }
            : undefined,
          text: sections.length > 0 ? undefined : text,
          createdAt: event.created_at,
          timestamp: formatTime(event.created_at)
        }
        newMessages = [...newMessages, msg]
      }
      break
    }

    // ── Generating status ─────────────────────────────────────────
    case 'generating_started':
    case 'generating_progress': {
      const msg: ChatMessage = {
        id: event.event_id,
        type: 'generating_status',
        role: 'agent',
        taskId: event.task_id,
        generating: payload.status as ChatMessage['generating'],
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at)
      }
      newMessages = [...newMessages, msg]
      break
    }

    // ── Review: 只有真实 review_started 或 ResultReviewTool 才推进 ──
    case 'review_started': {
      newCurrentPhase = 'review'
      newMessages = removeSectionConfirmationMessages(newMessages)
      break
    }

    // ── Review ────────────────────────────────────────────────────
    case 'review_completed': {
      newReviewResult = payload.review as Record<string, unknown> | undefined
      const msg: ChatMessage = {
        id: event.event_id,
        type: 'review_result',
        role: 'agent',
        taskId: event.task_id,
        review: payload.review as ChatMessage['review'],
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at)
      }
      newMessages = [...newMessages, msg]
      break
    }

    // ── Artifact ──────────────────────────────────────────────────
    case 'artifact_created': {
      const artifact = extractArtifactFromPayload(payload, now)
      if (artifact) {
        newArtifacts = mergeArtifacts(newArtifacts, [artifact])
      }
      const msg: ChatMessage = {
        id: event.event_id,
        type: 'artifact_download',
        role: 'agent',
        taskId: event.task_id,
        artifact: artifact as ChatMessage['artifact'],
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at)
      }
      newMessages = [...newMessages, msg]
      break
    }

    // ── Phase 2.9B.2 动态叙事: 独立叙事节点 ──────────────────────────
    case 'incremental_decision_made':
    case 'incremental_tool_finished':
    case 'agent_decision_update':
    case 'agent_observation_update': {
      // IncrementalAgent's decision/observation stream is an internal
      // execution trace.  It must not become a visible timeline node.  Keep
      // the event in events/seenEventIds so live + hydrate remain idempotent.
      if (isIncrementalInternalNarrative(event, payload)) break
      // Preparation keeps audit-level observations, but the visible contract
      // is one combined observation/decision timeline entry.
      if (event.event_type === 'agent_observation_update' && payload.agent_name === 'PreparationAgent') break
      const narrative = reduceDynamicNarrative(event, payload, mode)
      if (narrative) {
        // 同 event_id 已被 seenEventIds 去重;此处再按 sourceEventId 防
        // Live 与 Hydrate 重叠到达。不同 event_id 的 decision/observation
        // 即使共享 decision_id / dedupe_key 也不合并。
        if (!newDynamicNarratives.some((n) => n.sourceEventId === event.event_id)) {
          newDynamicNarratives = [...newDynamicNarratives, narrative]
        }
      }
      break
    }

    // ── Phase 2.9B.4 LLM-first Tool 叙事(流式) ─────────────────────
    case 'tool_narrative_started':
    case 'tool_narrative_delta':
    case 'tool_narrative_update':
    case 'tool_narrative_failed':
    case 'tool_narrative_fallback': {
      newToolNarratives = reduceToolNarrative(newToolNarratives, event, payload)
      break
    }
    // Phase 2.9B.6: 最终任务总结叙事(LLM-first)。归约到唯一权威槽位
    // block.taskSummaryNarrative;AgentRunCard 据此选择合法 LLM 总结或
    // deterministic 兜底。不进入 Tool 卡片、不改任务状态。
    case 'task_summary_narrative_started':
    case 'task_summary_narrative_delta':
    case 'task_summary_narrative_update':
    case 'task_summary_narrative_failed':
    case 'task_summary_narrative_fallback': {
      newTaskSummaryNarrative = reduceTaskSummaryNarrative(
        newTaskSummaryNarrative,
        event,
        payload
      )
      break
    }

    // ── Ignored events ────────────────────────────────────────────
    case 'plan_step_started':
    case 'plan_step_completed':
    case 'plan_step_failed': {
      const stepId = getFirstString(payload, ['step_id', 'stepId', 'step', 'id'])
      if (stepId) {
        newMessages = updatePlanStepMessages(
          newMessages,
          stepId,
          event.event_type === 'plan_step_started'
            ? 'running'
            : event.event_type === 'plan_step_failed'
              ? 'failed'
              : 'done'
        )
      }
      break
    }
    case 'retrying':
    case 'stream_phase_done':
      // No message produced, but event is still tracked
      break
    case 'format_loss_confirm_requested': {
      // BUG FIX 2026-08-19：reducer 必须产出带 formatLoss 的
      // ChatMessage，与 useTaskEvents.ts mapper 行为对齐。
      // 之前空 case 导致 AgentRunCard 永不渲染确认 banner，任务卡在
      // "正在思考..."。
      const asRecord = (v: unknown): Record<string, unknown> =>
        v && typeof v === 'object' && !Array.isArray(v)
          ? (v as Record<string, unknown>)
          : {}
      const asString = (v: unknown, fallback = ''): string =>
        typeof v === 'string' ? v : fallback
      const losses = Array.isArray(payload.losses)
        ? (payload.losses as Array<Record<string, unknown>>).map(asRecord)
        : []
      const choices = Array.isArray(payload.choices)
        ? (payload.choices as Array<Record<string, unknown>>).map(asRecord)
        : []
      const lossCountRaw = payload.loss_count
      const lossCount = typeof lossCountRaw === 'number' ? lossCountRaw : losses.length
      const summary = asString(
        payload.loss_details_for_user,
        asString(payload.summary)
      )
      // Format validation is a real task-level pause, including for the
      // incremental export path.  Keep the card actionable and prevent the
      // run from being rendered as "thinking" while it waits for a choice.
      newStatus = 'waiting_user_confirm'
      newCurrentPhase = 'format_loss_review'
      newMessages = removeSectionConfirmationMessages(newMessages)
      newMessages.push({
        id: event.event_id,
        type: 'agent_text',
        role: 'agent',
        taskId: event.task_id,
        eventType: 'format_loss_confirm_requested',
        // Phase 2.9B.2: conversationSequence 与其它消息保持一致，AgentRunCard
        // 按此顺序把 format-loss banner 锚定到 DocxFormatCheckTool 完成项之后。
        conversationSequence:
          typeof event.canonical_order === 'number'
            ? event.canonical_order
            : typeof event.sequence_no === 'number'
              ? event.sequence_no
              : undefined,
        text: asString(
          payload.text,
          asString(payload.title, '检测到格式丢失，请确认如何处理。')
        ),
        formatLoss: {
          taskId: event.task_id,
          losses: losses.map((entry) => ({
            element: asString(entry.element),
            expected: entry.expected,
            actual: entry.actual,
            message: asString(entry.message)
          })),
          lossCount,
          summary,
          choices: choices.map((choice) => ({
            id: asString(choice.id) === 'retry' ? 'retry' : 'accept',
            label: asString(choice.label),
            description: asString(choice.description)
          })),
          timeoutAt: asString(payload.timeout_at)
        },
        createdAt: now,
        timestamp: formatTime(event.created_at)
      })
      break
    }
    case 'format_loss_decision_recorded': {
      newMessages = removeFormatLossMessages(newMessages)
      newMessages = removeFormatLossConfirmationReceipts(newMessages)
      const decision = normalizeFormatLossDecision(payload.decision)
      newMessages.push({
        id: event.event_id,
        type: 'agent_text',
        role: 'agent',
        taskId: event.task_id,
        eventType: event.event_type as ChatMessage['eventType'],
        confirmed: true,
        conversationSequence:
          typeof event.canonical_order === 'number'
            ? event.canonical_order
            : typeof event.sequence_no === 'number'
              ? event.sequence_no
              : undefined,
        confirmationReceipt: buildFormatLossConfirmationReceipt(
          decision,
          normalizeFormatLosses(payload.losses)
        ),
        createdAt: now,
        timestamp: formatTime(event.created_at)
      })
      break
    }
    case 'format_loss_resuming': {
      // 通知型事件 — 产出简短 agent_text 但不渲染 formatLoss
      newMessages = removeFormatLossMessages(newMessages)
      const asString = (v: unknown, fallback = ''): string =>
        typeof v === 'string' ? v : fallback
      const fallbackText =
        event.event_type === 'format_loss_resuming'
          ? '正在恢复导出流程…'
          : `已记录格式丢失决定：${asString(payload.decision)}`
      newMessages.push({
        id: event.event_id,
        type: 'agent_text',
        role: 'agent',
        taskId: event.task_id,
        eventType: event.event_type as ChatMessage['eventType'],
        text: asString(
          payload.text,
          asString(payload.title, fallbackText)
        ),
        createdAt: now,
        timestamp: formatTime(event.created_at)
      })
      break
    }

    // ── Incremental Agent 事件(R5:subgraph.py 现在会 emit 终态) ──
    case 'incremental_started': {
      newStartedAt = newStartedAt ?? taskLifecycleStartedAt(payload, event.created_at)
      const startedMsg: ChatMessage = {
        id: event.event_id,
        type: 'agent_text',
        role: 'agent',
        taskId: event.task_id,
        eventType: event.event_type as ChatMessage['eventType'],
        text: event.content ?? event.title ?? '增量任务已启动',
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at)
      }
      newMessages = [...newMessages, startedMsg]
      break
    }

    case 'incremental_fallback': {
      const fallbackMsg: ChatMessage = {
        id: event.event_id,
        type: 'agent_text',
        role: 'agent',
        taskId: event.task_id,
        eventType: event.event_type as ChatMessage['eventType'],
        text: event.content ?? event.title ?? '增量任务已降级到 legacy regen+re-export',
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at)
      }
      newMessages = [...newMessages, fallbackMsg]
      break
    }

    case 'incremental_completed': {
      newStatus = 'completed'
      newCurrentPhase = 'completed'
      newStartedAt = newStartedAt ?? taskLifecycleStartedAt(payload, event.created_at)
      newCompletedAt = newCompletedAt ?? taskLifecycleCompletedAt(payload, event.created_at)
      newDurationMs = resolveTaskDurationMs(payload, newStartedAt, newCompletedAt, newDurationMs)
      newMessages = removeTransientInteractionMessages(completePlanStepMessages(newMessages))
      newSummaryFacts = extractSummaryFacts(payload)
      const payloadArtifact = extractArtifactFromPayload(payload, now)
      if (payloadArtifact) {
        newArtifacts = mergeArtifacts(newArtifacts, [payloadArtifact])
      }
      const formatCheck = payload.format_check as Record<string, unknown> | undefined
      if (formatCheck) newFormatCheckResult = formatCheck
      const msg = block.status === 'completed' && block.summaryFacts
        ? null
        : buildTaskLifecycleMessage(event, 'completed', payload)
      if (msg) {
        const enriched = {
          ...msg,
          _summaryFacts: newSummaryFacts,
          artifact: payloadArtifact
        }
        newMessages = [...newMessages, enriched as ChatMessage]
      }
      break
    }

    case 'incremental_failed': {
      newStatus = 'failed'
      newCurrentPhase = 'failed'
      newStartedAt = newStartedAt ?? taskLifecycleStartedAt(payload, event.created_at)
      newCompletedAt = newCompletedAt ?? taskLifecycleCompletedAt(payload, event.created_at)
      newDurationMs = resolveTaskDurationMs(payload, newStartedAt, newCompletedAt, newDurationMs)
      newMessages = removeTransientInteractionMessages(newMessages)
      const msg = buildTaskLifecycleMessage(event, 'failed', payload)
      if (msg) {
        newMessages = [...newMessages, msg]
      }
      break
    }

    default:
      break
  }

  // Update collapsed on task_completed (if user hasn't manually set)
  let newCollapsed = block.collapsed
  let newUserCollapseOverride = block.userCollapseOverride
  if (event.event_type === 'task_completed' || event.event_type === 'task_failed' || event.event_type === 'task_cancelled' || event.event_type === 'incremental_completed' || event.event_type === 'incremental_failed') {
    if (newUserCollapseOverride === null) {
      newCollapsed = true
    }
  }

  return {
    ...block,
    lastUpdateMode: mode,
    status: newStatus as TaskStatus,
    events: newEvents,
    seenEventIds: newSeenEventIds,
    lastAppliedCanonicalOrder: newLastAppliedCanonicalOrder,
    lastSseSequence: newLastSseSequence,
    messages: newMessages,
    toolExecutions: newToolExecutions,
    dynamicNarratives: newDynamicNarratives,
    toolNarratives: newToolNarratives,
    taskSummaryNarrative: newTaskSummaryNarrative,
    summaryFacts: newSummaryFacts,
    artifacts: newArtifacts,
    reviewResult: newReviewResult,
    formatCheckResult: newFormatCheckResult,
    currentPhase: newCurrentPhase,
    startedAt: newStartedAt,
    completedAt: newCompletedAt,
    durationMs: newDurationMs,
    collapsed: newCollapsed,
    userCollapseOverride: newUserCollapseOverride
  }
}

// ── Tool lifecycle reducers ─────────────────────────────────────────

/**
 * 显式 chunk_index 提取。
 *
 * 仅在 payload 明确包含合法整数 chunk_index / chunkIndex 时返回该整数,
 * 否则返回 null。用于区分「属性不存在」与「属性存在且值为 0」:
 * tool_started 不得把缺失的 chunk_index 默认成 0,否则会把真正的
 * 第一个 tool_finished 分帧(chunk_index=0)误判为重复事件。
 */
function explicitChunkIndex(payload: Record<string, unknown>): number | null {
  const raw = payload.chunk_index ?? payload.chunkIndex
  if (typeof raw === 'number' && Number.isInteger(raw)) return raw
  if (typeof raw === 'string' && raw.trim()) {
    const parsed = Number(raw)
    if (Number.isInteger(parsed)) return parsed
  }
  return null
}

function reduceToolStarted(
  block: TaskRunBlock,
  event: NormalizedTaskEvent,
  payload: Record<string, unknown>,
  now: string
): { toolExecutions: ToolExecutionViewModel[]; messages: ChatMessage[] } {
  const toolCallId = getFirstString(payload, ['tool_call_id', 'toolCallId'], event.event_id)
  const attempt = Number(payload.attempt ?? 1)
  const logicalKey = `${toolCallId}:${attempt}`
  const toolName = inferToolName(payload, event)
  const chunkIndex = explicitChunkIndex(payload)
  const startedMs = parseApiDateTime(now) || parseApiDateTime(event.created_at)
  const presentation = extractToolPresentation(payload, toolName)

  const existing = block.toolExecutions.find(t => t.logicalKey === logicalKey)

  if (existing) {
    // Don't restart a terminal tool
    if (existing.terminal) return { toolExecutions: block.toolExecutions, messages: block.messages }

    // Chunk dedup — 仅当 payload 显式携带 chunk_index 时才按 chunk 去重。
    // tool_started 通常不带 chunk_index(不属于叙事分帧),不得按 0 去重。
    if (chunkIndex !== null && existing.receivedChunkIndexes[chunkIndex]) {
      return { toolExecutions: block.toolExecutions, messages: block.messages }
    }

    // Update existing
    const updated: ToolExecutionViewModel = {
      ...existing,
      ...mergeToolPresentation(existing, presentation),
      receivedChunkIndexes: chunkIndex !== null
        ? { ...existing.receivedChunkIndexes, [chunkIndex]: true }
        : existing.receivedChunkIndexes,
      startedAt: existing.startedAt ?? now,
      status: 'running'
    }
    return {
      toolExecutions: block.toolExecutions.map(t => t.logicalKey === logicalKey ? updated : t),
      messages: block.messages
    }
  }

  // Create new tool execution
  const toolExec: ToolExecutionViewModel = {
    logicalKey,
    toolCallId,
    toolName,
    attempt,
    status: 'running',
    terminal: false,
    seenEventIds: { [event.event_id]: true },
    receivedChunkIndexes: chunkIndex !== null ? { [chunkIndex]: true } : {},
    startedAt: now,
    startedAtMs: startedMs,
    input: formatToolInput(payload),
    ...presentation,
    publicUpdate: extractPublicExecutionUpdate(payload)
  }

  // Build tool_call message
  const msg: ChatMessage = {
    id: toolCallId,
    type: 'tool_call',
    role: 'agent',
    taskId: event.task_id,
    // Phase 2.9B.2: 携带事件 canonical order,供 AgentRunCard 把动态叙事
    // 锚定到真实事件位置(§15)。
    conversationSequence:
      typeof event.canonical_order === 'number'
        ? event.canonical_order
        : typeof event.sequence_no === 'number'
          ? event.sequence_no
          : undefined,
    toolCall: {
      id: toolCallId,
      name: toolName,
      status: 'running',
      input: toolExec.input ?? '',
      output: '',
      duration: '',
      startedAt: now,
      ...presentation,
      publicUpdate: toolExec.publicUpdate,
      narrativeExpected: getFirstBoolean(payload, ['narrative_expected', 'narrativeExpected'])
    },
    createdAt: event.created_at,
    timestamp: formatTime(event.created_at)
  }

  return {
    toolExecutions: [...block.toolExecutions, toolExec],
    messages: [...block.messages, msg]
  }
}

function reduceToolFinished(
  block: TaskRunBlock,
  event: NormalizedTaskEvent,
  payload: Record<string, unknown>,
  now: string
): { toolExecutions: ToolExecutionViewModel[]; messages: ChatMessage[] } {
  const toolCallId = getFirstString(payload, ['tool_call_id', 'toolCallId'], event.event_id)
  const attempt = Number(payload.attempt ?? 1)
  const logicalKey = `${toolCallId}:${attempt}`
  const chunkIndex = Number(payload.chunk_index ?? payload.chunkIndex ?? 0)
  const isChunkFinal = Boolean(payload.chunk_final ?? payload.chunkFinal)
  const isFailed = event.event_type === 'tool_failed'
  const toolName = inferToolName(payload, event)
  const presentation = extractToolPresentation(payload, toolName)
  // Phase 2.9A.35: duration 优先级 — payload.duration_ms 优先
  const payloadDurationMs = typeof payload.duration_ms === 'number' && Number.isFinite(payload.duration_ms)
    ? payload.duration_ms
    : typeof payload.duration_ms === 'string' && payload.duration_ms.trim()
      ? Number(payload.duration_ms)
      : NaN

  const existing = block.toolExecutions.find(t => t.logicalKey === logicalKey)

  if (existing) {
    // Chunk dedup — don't process same chunk twice
    if (existing.receivedChunkIndexes[chunkIndex]) {
      return { toolExecutions: block.toolExecutions, messages: block.messages }
    }

    // Don't update a terminal tool (except for final chunk)
    if (existing.terminal && !isChunkFinal) {
      return { toolExecutions: block.toolExecutions, messages: block.messages }
    }

    const newStatus = isFailed ? 'failed' : isChunkFinal ? 'success' : existing.status
    const newTerminal = isFailed || isChunkFinal

    // duration: payload.duration_ms → completedAt-startedAt → running 时 now-startedAt
    let durationMs: number | undefined
    if (Number.isFinite(payloadDurationMs)) {
      durationMs = payloadDurationMs
    } else if (newTerminal && existing.startedAtMs) {
      const finishedMs = parseApiDateTime(now) || Date.now()
      durationMs = Math.max(0, finishedMs - existing.startedAtMs)
    } else if (existing.startedAtMs) {
      const finishedMs = parseApiDateTime(now) || Date.now()
      durationMs = Math.max(0, finishedMs - existing.startedAtMs)
    }

    const updated: ToolExecutionViewModel = {
      ...existing,
      ...mergeToolPresentation(existing, presentation),
      receivedChunkIndexes: { ...existing.receivedChunkIndexes, [chunkIndex]: true },
      chunkTotal: Number(payload.chunk_total ?? payload.chunkTotal ?? existing.chunkTotal),
      status: newStatus,
      terminal: newTerminal,
      completedAt: newTerminal ? now : existing.completedAt,
      durationMs,
      publicUpdate: mergePublicExecutionUpdate(existing.publicUpdate, extractPublicExecutionUpdate(payload)),
      output: formatToolOutput(payload) ?? existing.output,
      error: isFailed ? { message: String(payload.error_summary ?? payload.error ?? payload.message ?? '') } : existing.error
    }

    // Update the corresponding message
    const newMessages = block.messages.map(msg => {
      if (msg.id !== toolCallId) return msg
      return {
        ...msg,
        toolCall: {
          id: toolCallId,
          name: msg.toolCall?.name ?? inferToolName(payload, event),
          status: newStatus as 'running' | 'success' | 'failed',
          attempt,
          input: msg.toolCall?.input ?? '',
          output: updated.output ?? msg.toolCall?.output ?? '',
          duration: newTerminal
            ? formatDurationMs(durationMs ?? 0)
            : msg.toolCall?.duration ?? '',
          startedAt: msg.toolCall?.startedAt,
          finishedAt: newTerminal ? now : msg.toolCall?.finishedAt,
          displayName: updated.displayName ?? msg.toolCall?.displayName,
          businessAction: updated.businessAction ?? msg.toolCall?.businessAction,
          businessSubjectType: updated.businessSubjectType ?? msg.toolCall?.businessSubjectType,
          businessSubjectName: updated.businessSubjectName ?? msg.toolCall?.businessSubjectName,
          filePublicId: updated.filePublicId ?? msg.toolCall?.filePublicId,
          fileName: updated.fileName ?? msg.toolCall?.fileName,
          progressMessage: updated.progressMessage ?? msg.toolCall?.progressMessage,
          completionMessage: updated.completionMessage ?? msg.toolCall?.completionMessage,
          errorSummary: updated.errorSummary ?? msg.toolCall?.errorSummary,
          publicUpdate: updated.publicUpdate ?? msg.toolCall?.publicUpdate,
          narrativeExpected: getFirstBoolean(
            payload,
            ['narrative_expected', 'narrativeExpected'],
            msg.toolCall?.narrativeExpected ?? false
          )
        }
      }
    })

    return {
      toolExecutions: block.toolExecutions.map(t => t.logicalKey === logicalKey ? updated : t),
      messages: newMessages
    }
  }

  // No existing tool — create from finish event (handles late arrival)
  const toolExec: ToolExecutionViewModel = {
    logicalKey,
    toolCallId,
    toolName,
    attempt,
    status: isFailed ? 'failed' : isChunkFinal ? 'success' : 'running',
    terminal: isFailed || isChunkFinal,
    seenEventIds: { [event.event_id]: true },
    receivedChunkIndexes: { [chunkIndex]: true },
    chunkTotal: Number(payload.chunk_total ?? payload.chunkTotal),
    startedAt: payload.started_at as string | undefined,
    startedAtMs: parseApiDateTime(payload.started_at as string | undefined) || undefined,
    completedAt: isFailed || isChunkFinal ? now : undefined,
    durationMs: Number.isFinite(payloadDurationMs) ? payloadDurationMs : undefined,
    input: formatToolInput(payload),
    output: formatToolOutput(payload),
    ...presentation,
    publicUpdate: extractPublicExecutionUpdate(payload),
    error: isFailed ? { message: String(payload.error_summary ?? payload.error ?? payload.message ?? '') } : undefined
  }

  const msg: ChatMessage = {
    id: toolCallId,
    type: 'tool_call',
    role: 'agent',
    taskId: event.task_id,
    toolCall: {
      id: toolCallId,
      name: toolExec.toolName,
      status: toolExec.status as 'running' | 'success' | 'failed',
      input: toolExec.input ?? '',
      output: toolExec.output ?? '',
      duration: toolExec.completedAt
        ? formatDurationMs(toolExec.durationMs ?? 0)
        : '',
      startedAt: toolExec.startedAt,
      finishedAt: toolExec.completedAt,
      displayName: toolExec.displayName,
      businessAction: toolExec.businessAction,
      businessSubjectType: toolExec.businessSubjectType,
      businessSubjectName: toolExec.businessSubjectName,
      filePublicId: toolExec.filePublicId,
      fileName: toolExec.fileName,
      progressMessage: toolExec.progressMessage,
      completionMessage: toolExec.completionMessage,
      errorSummary: toolExec.errorSummary,
      publicUpdate: toolExec.publicUpdate,
      narrativeExpected: getFirstBoolean(payload, ['narrative_expected', 'narrativeExpected'])
    },
    createdAt: event.created_at,
    timestamp: formatTime(event.created_at)
  }

  return {
    toolExecutions: [...block.toolExecutions, toolExec],
    messages: [...block.messages, msg]
  }
}

function reduceToolProgress(
  block: TaskRunBlock,
  event: NormalizedTaskEvent,
  payload: Record<string, unknown>,
  now: string
): { toolExecutions: ToolExecutionViewModel[]; messages: ChatMessage[] } {
  const toolCallId = getFirstString(payload, ['tool_call_id', 'toolCallId'], event.event_id)
  const attempt = Number(payload.attempt ?? 1)
  const logicalKey = `${toolCallId}:${attempt}`
  const toolName = inferToolName(payload, event)
  const presentation = extractToolPresentation(payload, toolName)
  const existing = block.toolExecutions.find(t => t.logicalKey === logicalKey)

  if (!existing) {
    return reduceToolStarted(block, event, payload, now)
  }
  if (existing.terminal) {
    return { toolExecutions: block.toolExecutions, messages: block.messages }
  }

  const updated: ToolExecutionViewModel = {
    ...existing,
    ...mergeToolPresentation(existing, presentation),
    status: 'running',
    terminal: false,
  }
  const newMessages = block.messages.map(msg => {
    if (msg.id !== toolCallId || !msg.toolCall) return msg
    return {
      ...msg,
      toolCall: {
        ...msg.toolCall,
        status: 'running' as const,
        displayName: updated.displayName ?? msg.toolCall.displayName,
        businessAction: updated.businessAction ?? msg.toolCall.businessAction,
        businessSubjectType: updated.businessSubjectType ?? msg.toolCall.businessSubjectType,
        businessSubjectName: updated.businessSubjectName ?? msg.toolCall.businessSubjectName,
        filePublicId: updated.filePublicId ?? msg.toolCall.filePublicId,
        fileName: updated.fileName ?? msg.toolCall.fileName,
        progressMessage: updated.progressMessage ?? msg.toolCall.progressMessage,
        completionMessage: updated.completionMessage ?? msg.toolCall.completionMessage,
        errorSummary: updated.errorSummary ?? msg.toolCall.errorSummary,
      }
    }
  })
  return {
    toolExecutions: block.toolExecutions.map(t => t.logicalKey === logicalKey ? updated : t),
    messages: newMessages
  }
}

// ── Helpers ─────────────────────────────────────────────────────────

function buildTaskLifecycleMessage(
  event: NormalizedTaskEvent,
  lifecycle: 'running' | 'completed' | 'failed' | 'cancelled',
  payload: Record<string, unknown>
): ChatMessage | null {
  if (lifecycle === 'running') {
    // task_created/task_resumed don't produce a visible message
    return null
  }
  const type = lifecycle === 'failed' ? 'error' : 'agent_text'
  return {
    id: event.event_id,
    type,
    role: 'agent',
    taskId: event.task_id,
    eventType: event.event_type as ChatMessage['eventType'],
    text: event.content ?? getFirstString(payload, ['final_answer', 'finalAnswer', 'answer', 'message', 'summary', 'output']),
    createdAt: event.created_at,
    timestamp: formatTime(event.created_at)
  }
}

function extractSummaryFacts(payload: Record<string, unknown>): SummaryFacts {
  const raw = (payload.summary_facts as Record<string, unknown>) ?? {}
  const review = (raw.review as Record<string, unknown>) ?? {}
  const artifact = (raw.artifact as Record<string, unknown>) ?? {}
  return {
    generatedSections: Number(raw.generated_sections ?? raw.generatedSections ?? 0),
    keptSections: Number(raw.kept_sections ?? raw.keptSections ?? 0),
    businessModules: Number(raw.business_modules ?? raw.businessModules ?? 0),
    pageCount: normalizeOptionalNumber(raw.page_count ?? raw.pageCount ?? artifact.page_count ?? artifact.pageCount),
    review: {
      block_count: Number(review.block_count ?? 0),
      warning_count: Number(review.warning_count ?? 0),
      suggestion_count: Number(review.suggestion_count ?? 0),
      level: String(review.level ?? ''),
      passed: Boolean(review.passed ?? false)
    },
    formatStatus: String(raw.format_status ?? raw.formatStatus ?? '')
  }
}

function extractArtifactFromPayload(payload: Record<string, unknown>, now: string): Artifact | null {
  const raw = (payload.artifact as Record<string, unknown>) ?? {}
  if (!raw.public_id) return null
  return {
    id: String(raw.public_id ?? ''),
    type: String(raw.artifact_type ?? 'test_plan_word') as Artifact['type'],
    name: String(raw.file_name ?? ''),
    size: raw.file_size ? String(raw.file_size) : '',
    generatedAt: now,
    downloadUrl: (raw.download_url as string | undefined) ?? '',
    pageCount: normalizeOptionalNumber(raw.page_count ?? raw.pageCount)
  }
}

function normalizeOptionalNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return value
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    if (Number.isFinite(parsed) && parsed >= 0) return parsed
  }
  return null
}

function mergeArtifacts(existing: Artifact[], incoming: Artifact[]): Artifact[] {
  const seen = new Set(existing.map(a => a.id))
  const result = [...existing]
  for (const art of incoming) {
    if (!seen.has(art.id)) {
      result.push(art)
      seen.add(art.id)
    }
  }
  return result
}

function normalizePlanSteps(payload: Record<string, unknown>): Record<string, unknown>[] {
  const rawPlan = payload.plan
  if (Array.isArray(payload.steps)) return payload.steps.map(normalizePlanStep)
  if (Array.isArray(rawPlan)) return rawPlan.map(normalizePlanStep)
  const planRecord = rawPlan && typeof rawPlan === 'object' ? rawPlan as Record<string, unknown> : {}
  if (Array.isArray(planRecord.steps)) return planRecord.steps.map(normalizePlanStep)
  return []
}

function upsertPlanMessage(
  messages: ChatMessage[],
  event: NormalizedTaskEvent,
  payload: Record<string, unknown>
): ChatMessage[] {
  const incomingRevision = normalizePlanRevision(payload)
  const incomingPlan = normalizePlanSteps(payload).map(toTaskPlanStep)
  const existingIndex = messages.findIndex((message) => message.type === 'agent_plan')

  if (existingIndex < 0) {
    return [
      ...messages,
      {
        id: event.event_id,
        type: 'agent_plan',
        role: 'agent',
        taskId: event.task_id,
        plan: incomingPlan,
        planRevision: incomingRevision,
        createdAt: event.created_at,
        timestamp: formatTime(event.created_at),
        _rawEvent: event as unknown as Record<string, unknown>
      }
    ]
  }

  const existing = messages[existingIndex]
  const existingRevision = existing.planRevision ?? 0
  if (incomingRevision <= existingRevision) return messages

  const nextPlan = mergePlanRevision(existing.plan ?? [], incomingPlan, existingRevision)
  return messages.map((message, index) => {
    if (index !== existingIndex) return message
    return {
      ...message,
      plan: nextPlan,
      planRevision: incomingRevision,
      createdAt: message.createdAt ?? event.created_at,
      timestamp: message.timestamp ?? formatTime(event.created_at),
      _rawEvent: event as unknown as Record<string, unknown>
    }
  })
}

function normalizePlanRevision(payload: Record<string, unknown>): number {
  const rawPlan = payload.plan && typeof payload.plan === 'object'
    ? payload.plan as Record<string, unknown>
    : {}
  const raw = payload.revision ?? payload.plan_revision ?? payload.planRevision ?? rawPlan.revision
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw
  if (typeof raw === 'string' && raw.trim()) {
    const parsed = Number(raw)
    if (Number.isFinite(parsed)) return parsed
  }
  return 1
}

function toTaskPlanStep(step: Record<string, unknown>, index: number): TaskPlanStep {
  return {
    id: String(step.step_id ?? step.id ?? `step_${index}`),
    title: String(step.name ?? step.title ?? `Step ${index + 1}`),
    detail: typeof step.detail === 'string' ? step.detail : undefined,
    status: normalizePlanStepStatus(step.status)
  }
}

function normalizePlanStepStatus(raw: unknown): PlanStepStatus {
  const status = typeof raw === 'string' ? raw : ''
  if (status === 'done' || status === 'completed' || status === 'success') return 'done'
  if (status === 'running' || status === 'in_progress' || status === 'waiting' || status === 'waiting_user') return 'running'
  if (status === 'failed' || status === 'error') return 'failed'
  if (status === 'superseded' || status === 'skipped') return 'superseded'
  return 'pending'
}

function mergePlanRevision(
  existingPlan: TaskPlanStep[],
  incomingPlan: TaskPlanStep[],
  existingRevision: number
): TaskPlanStep[] {
  const incomingById = new Map(incomingPlan.map((step) => [step.id, step]))
  const result: TaskPlanStep[] = []
  const addedIds = new Set<string>()

  for (const oldStep of existingPlan) {
    if (oldStep.status === 'superseded') {
      result.push(oldStep)
      addedIds.add(oldStep.id)
      continue
    }

    const incomingStep = incomingById.get(oldStep.id)
    if (incomingStep && oldStep.status === 'done') {
      result.push({ ...incomingStep, status: 'done', detail: incomingStep.detail ?? oldStep.detail })
      addedIds.add(incomingStep.id)
      continue
    }

    const replaced = !incomingStep || (incomingStep.title !== oldStep.title && oldStep.status !== 'done' && oldStep.status !== 'failed')
    if (replaced && oldStep.status !== 'done' && oldStep.status !== 'failed') {
      const supersededId = buildSupersededPlanStepId(oldStep.id, existingRevision)
      result.push({ ...oldStep, id: supersededId, status: 'superseded' })
      addedIds.add(supersededId)
      continue
    }

    if (incomingStep) {
      result.push({
        ...incomingStep,
        status: oldStep.status === 'failed' ? 'failed' : incomingStep.status,
        detail: incomingStep.detail ?? oldStep.detail
      })
      addedIds.add(incomingStep.id)
    }
  }

  for (const incomingStep of incomingPlan) {
    if (addedIds.has(incomingStep.id)) continue
    result.push(incomingStep)
    addedIds.add(incomingStep.id)
  }

  return result
}

function buildSupersededPlanStepId(stepId: string, revision: number): string {
  return `${stepId}:rev${Math.max(1, revision)}:superseded`
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

const PLAN_STEP_BY_TOOL: Record<string, string> = {
  RequirementParserTool: 'parse_requirement',
  TemplateParserTool: 'parse_template',
  KnowledgeSearchTool: 'search_knowledge',
  SectionSuggestionTool: 'suggest_sections',
  TestPlanGeneratorTool: 'generate',
  ResultReviewTool: 'review',
  WordExportTool: 'export',
  DocxFormatCheckTool: 'export',
  TestPlanRegenTool: 'generate'
}

function isIncrementalInternalNarrative(
  event: NormalizedTaskEvent,
  payload: Record<string, unknown>
): boolean {
  if (event.event_type === 'incremental_decision_made' || event.event_type === 'incremental_tool_finished') {
    return true
  }
  if (event.event_type !== 'agent_decision_update' && event.event_type !== 'agent_observation_update') {
    return false
  }
  const route = getFirstString(payload, ['route']).toLowerCase()
  const agentName = getFirstString(payload, ['agent_name', 'agentName']).toLowerCase()
  return route === 'incremental' || agentName === 'incrementalagent'
}

function updatePlanMessagesByTool(
  messages: ChatMessage[],
  toolName: string,
  status: PlanStepStatus
): ChatMessage[] {
  const stepId = PLAN_STEP_BY_TOOL[toolName]
  if (!stepId) return messages
  return updatePlanStepMessages(messages, stepId, status)
}

function updatePlanStepMessages(
  messages: ChatMessage[],
  stepId: string,
  status: PlanStepStatus
): ChatMessage[] {
  return messages.map((message) => {
    if (message.type !== 'agent_plan' || !Array.isArray(message.plan)) return message
    let changed = false
    const plan = message.plan.map((step) => {
      if (step.id !== stepId) return step
      if (step.status === status) return step
      if (status === 'running' && (step.status === 'done' || step.status === 'failed')) {
        return step
      }
      changed = true
      return { ...step, status }
    })
    return changed ? { ...message, plan } : message
  })
}

function completePlanStepMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((message) => {
    if (message.type !== 'agent_plan' || !Array.isArray(message.plan)) return message
    let changed = false
    const plan = message.plan.map((step) => {
      if (step.status === 'done' || step.status === 'failed' || step.status === 'superseded') {
        return step
      }
      changed = true
      return { ...step, status: 'done' as PlanStepStatus }
    })
    return changed ? { ...message, plan } : message
  })
}

function removeSectionConfirmationMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.filter((message) =>
    message.type !== 'section_confirm' || message.confirmed || message.confirmationReceipt
  )
}

function removeFormatLossMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.filter((message) => !message.formatLoss)
}

function removeFormatLossConfirmationReceipts(messages: ChatMessage[]): ChatMessage[] {
  return messages.filter((message) => message.confirmationReceipt?.kind !== 'format_loss')
}

function normalizeFormatLossDecision(value: unknown): 'accept' | 'retry' | 'reject' {
  return value === 'retry' || value === 'reject' ? value : 'accept'
}

function normalizeFormatLosses(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((loss) => {
      if (typeof loss === 'string') return loss.trim()
      if (!loss || typeof loss !== 'object') return ''
      const entry = loss as Record<string, unknown>
      for (const candidate of [entry.message, entry.element, entry.name]) {
        if (typeof candidate === 'string' && candidate.trim()) return candidate.trim()
      }
      return ''
    })
    .filter(Boolean)
}

function buildFormatLossConfirmationReceipt(
  decision: 'accept' | 'retry' | 'reject',
  losses: string[]
) {
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
  const detected = losses.length ? losses.join('；') : '检测到的格式项已记录'
  return {
    kind: 'format_loss' as const,
    markdown: [
      `- **处理方式：** ${selection}`,
      `- **检测到的格式丢失：** ${detected}`,
      `- **后续流程：** ${nextStep}`
    ].join('\n')
  }
}

function removePreparationClarificationMessages(messages: ChatMessage[]): ChatMessage[] {
  return messages.filter((message) =>
    message.type !== 'preparation_clarification' || message.confirmed || message.confirmationReceipt
  )
}

function removeTransientInteractionMessages(messages: ChatMessage[]): ChatMessage[] {
  return removeFormatLossMessages(
    removePreparationClarificationMessages(removeSectionConfirmationMessages(messages))
  )
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

  const step = rawStep && typeof rawStep === 'object' ? rawStep as Record<string, unknown> : {}
  const id = getFirstString(step, ['step_id', 'stepId', 'id', 'step'], `step_${index}`)
  return {
    ...step,
    step_id: id,
    name: getFirstString(step, ['name', 'title'], PLAN_STEP_DEFINITIONS[id]?.title ?? `步骤 ${index + 1}`)
  }
}

function inferToolName(payload: Record<string, unknown>, event: NormalizedTaskEvent): string {
  if (typeof payload.tool_name === 'string') return payload.tool_name
  if (typeof payload.name === 'string') return payload.name
  if (event.node_name) return event.node_name
  return 'tool'
}

function extractToolPresentation(payload: Record<string, unknown>, toolName: string): Partial<ToolPresentationFields> {
  return {
    displayName: getFirstString(payload, ['display_tool_name', 'displayToolName', 'tool_display_name', 'toolDisplayName']) || defaultDisplayToolName(toolName),
    businessAction: getFirstString(payload, ['business_action', 'businessAction']),
    businessSubjectType: getFirstString(payload, ['business_subject_type', 'businessSubjectType']),
    businessSubjectName: getFirstString(payload, ['business_subject_name', 'businessSubjectName']),
    filePublicId: getFirstString(payload, ['file_public_id', 'filePublicId', 'file_id', 'fileId']),
    fileName: getFirstString(payload, ['file_name', 'fileName', 'filename']),
    progressMessage: getFirstString(payload, ['progress_message', 'progressMessage']),
    completionMessage: getFirstString(payload, ['completion_message', 'completionMessage']),
    errorSummary: getFirstString(payload, ['error_summary', 'errorSummary']),
  }
}

function mergeToolPresentation(
  existing: Partial<ToolPresentationFields>,
  incoming: Partial<ToolPresentationFields>
): Partial<ToolPresentationFields> {
  return {
    displayName: incoming.displayName || existing.displayName,
    businessAction: incoming.businessAction || existing.businessAction,
    businessSubjectType: incoming.businessSubjectType || existing.businessSubjectType,
    businessSubjectName: incoming.businessSubjectName || existing.businessSubjectName,
    filePublicId: incoming.filePublicId || existing.filePublicId,
    fileName: incoming.fileName || existing.fileName,
    progressMessage: incoming.progressMessage || existing.progressMessage,
    completionMessage: incoming.completionMessage || existing.completionMessage,
    errorSummary: incoming.errorSummary || existing.errorSummary,
  }
}

function defaultDisplayToolName(toolName: string): string {
  const names: Record<string, string> = {
    RequirementParserTool: '调用Word文档解析工具',
    TemplateParserTool: '调用模板解析工具',
    KnowledgeSearchTool: '调用知识库查询工具',
    SectionSuggestionTool: '调用章节建议工具',
    TestPlanGeneratorTool: '调用测试方案生成工具',
  }
  return names[toolName] ?? toolName
}

function formatToolInput(payload: Record<string, unknown>): string {
  const value = payload.display_input ?? payload.displayInput ?? payload.input ?? payload.tool_input ?? payload.toolInput ?? ''
  return typeof value === 'string' ? value : ''
}

function formatToolOutput(payload: Record<string, unknown>): string {
  const value = payload.display_output ?? payload.displayOutput ?? payload.output ?? payload.tool_output ?? payload.toolOutput ?? ''
  return typeof value === 'string' ? value : ''
}

function getFirstString(record: Record<string, unknown>, keys: string[], fallback = ''): string {
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'string' && value) return value
    if (typeof value === 'number') return String(value)
  }
  return fallback
}

function getFirstBoolean(record: Record<string, unknown>, keys: string[], fallback = false): boolean {
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'boolean') return value
    if (typeof value === 'string' && value.trim()) {
      const normalized = value.trim().toLowerCase()
      if (['1', 'true', 'yes', 'on'].includes(normalized)) return true
      if (['0', 'false', 'no', 'off'].includes(normalized)) return false
    }
    if (typeof value === 'number' && Number.isFinite(value)) return value !== 0
  }
  return fallback
}

function getFirstFiniteNumber(record: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
    if (typeof value === 'string' && value.trim()) {
      const parsed = Number(value)
      if (Number.isFinite(parsed)) return parsed
    }
  }
  return null
}

function taskLifecycleStartedAt(payload: Record<string, unknown>, fallback?: string): string | undefined {
  return getFirstString(payload, [
    'started_at',
    'startedAt',
    'run_started_at',
    'runStartedAt'
  ], fallback ?? '') || undefined
}

function taskLifecycleCompletedAt(payload: Record<string, unknown>, fallback?: string): string | undefined {
  return getFirstString(payload, [
    'completed_at',
    'completedAt',
    'finished_at',
    'finishedAt',
    'run_finished_at',
    'runFinishedAt'
  ], fallback ?? '') || undefined
}

function resolveTaskDurationMs(
  payload: Record<string, unknown>,
  startedAt?: string,
  completedAt?: string,
  existing?: number | null
): number | null | undefined {
  if (typeof existing === 'number' && Number.isFinite(existing) && existing >= 0) return existing
  const payloadDurationMs = getFirstFiniteNumber(payload, ['duration_ms', 'durationMs', 'elapsed_ms', 'elapsedMs'])
  if (payloadDurationMs !== null && payloadDurationMs >= 0) return payloadDurationMs
  const startedMs = parseApiDateTime(startedAt)
  const completedMs = parseApiDateTime(completedAt)
  if (!Number.isFinite(startedMs) || !Number.isFinite(completedMs)) return existing
  return Math.max(0, completedMs - startedMs)
}

function formatTime(iso?: string): string {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  } catch {
    return ''
  }
}

function formatDurationMs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return ''
  const totalSeconds = Math.floor(ms / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, '0')}s`
  return `${seconds}s`
}

// ── Dynamic narrative reducer (Phase 2.9B.2) ─────────────────────────

/**
 * 把 agent_decision_update / agent_observation_update 归约为独立的
 * 动态叙事节点。规则(§11-§16):
 *  - 复用 Canonical extractPublicExecutionUpdate 解析 public_update(嵌套兼容);
 *  - 无有效 headline 时不生成节点(但事件仍被 seenEventIds 追踪);
 *  - 不创建 ToolExecution;不修改任务 / 工具状态;
 *  - 稳定身份 = dynamic-narrative:{task_id}:{event_id};
 *  - 排序字段 canonicalOrder/sequenceNo 保留,供组件按真实事件位置排序。
 */
function reduceDynamicNarrative(
  event: NormalizedTaskEvent,
  payload: Record<string, unknown>,
  mode: 'live' | 'hydrate'
): DynamicNarrative | null {
  const publicUpdate = extractPublicExecutionUpdate(payload)
  if (!publicUpdate) return null

  const eventType = event.event_type as DynamicNarrative['eventType']
  return {
    id: `dynamic-narrative:${event.task_id}:${event.event_id}`,
    sourceEventId: event.event_id,
    eventType,
    taskId: event.task_id,
    graphRunId: event.graph_run_id,
    canonicalOrder: typeof event.canonical_order === 'number' ? event.canonical_order : undefined,
    sequenceNo: typeof event.sequence_no === 'number' ? event.sequence_no : null,
    createdAt: event.created_at,
    agentName: getFirstString(payload, ['agent_name', 'agentName'], 'Agent'),
    stepId: getFirstString(payload, ['step_id', 'stepId']) || undefined,
    stepTitle: getFirstString(payload, ['step_title', 'stepTitle']) || undefined,
    narrativeSource: (getFirstString(payload, ['narrative_source', 'narrativeSource']) || undefined) as DynamicNarrative['narrativeSource'],
    decisionId: getFirstString(payload, ['decision_id', 'decisionId'], ''),
    stepIndex: getFirstFiniteNumber(payload, ['step_index', 'stepIndex']) ?? 0,
    action: getFirstString(payload, ['action'], ''),
    toolName: getFirstString(payload, ['tool_name', 'toolName']) || null,
    route: getFirstString(payload, ['route']) || null,
    publicUpdate,
    dedupeKey: publicUpdate.dedupeKey,
    sourceMode: mode
  }
}

// ── Tool narrative reducer (Phase 2.9B.4) ─────────────────────────────

/**
 * 空 PublicExecutionUpdate(流式 partial 的起始值)。
 * 保持完整合同类型(version/kind/level/source 等用安全默认)。
 */
function emptyPublicExecutionUpdate(): PublicExecutionUpdate {
  return {
    version: 1,
    kind: 'tool_result',
    level: 'info',
    headline: '',
    summary: '',
    impact: '',
    nextAction: '',
    details: [],
    narrativeText: '',
    source: 'template',
    dedupeKey: '',
    chunkIndex: 0,
    chunkTotal: 1,
    chunkFinal: false
  }
}

/**
 * 归约 tool_narrative_started / delta / update / failed / fallback 事件
 * 为 ToolNarrativeState(锚定 sourceToolCallId)。
 *
 * 规则:
 *  - started: 创建空 partial publicUpdate;
 *  - delta: 按 field 追加文本,chunk_index 单调去重;
 *  - update: 用最终已校验 public_update 覆盖 partial;
 *  - fallback: 用确定性 public_update;
 *  - failed: 标记 failed;
 *  - reset 语义由 generationId 变化体现: 新 generation 覆盖旧 partial。
 */
function reduceToolNarrative(
  existing: ToolNarrativeState[],
  event: NormalizedTaskEvent,
  payload: Record<string, unknown>
): ToolNarrativeState[] {
  const narrativeId = String(payload.narrative_id ?? '')
  if (!narrativeId) return existing
  const eventType = event.event_type as string

  const findIdx = existing.findIndex((n) => n.narrativeId === narrativeId)
  const base = findIdx >= 0 ? existing[findIdx] : null

  // started: 创建新 partial(或 reset 新 generation)。
  if (eventType === 'tool_narrative_started') {
    const generationId = String(payload.generation_id ?? '')
    const generationNo = Number(payload.generation_no ?? 1)
    const sourceToolCallId = String(payload.source_tool_call_id ?? '')
    const toolName = String(payload.tool_name ?? '')
    const state: ToolNarrativeState = {
      narrativeId,
      generationId,
      generationNo,
      sourceToolCallId,
      toolName,
      attempt: Number(payload.attempt ?? 1),
      status: 'streaming',
      source: 'llm',
      publicUpdate: emptyPublicExecutionUpdate(),
      lastChunkIndex: -1,
      canonicalOrder:
        typeof event.canonical_order === 'number'
          ? event.canonical_order
          : typeof event.sequence_no === 'number'
            ? event.sequence_no
            : undefined,
      createdAt: event.created_at
    }
    const rest = findIdx >= 0 ? existing.filter((n) => n.narrativeId !== narrativeId) : existing
    return [...rest, state]
  }

  if (!base) return existing

  // delta: 按 field 追加 + chunk 单调去重。
  if (eventType === 'tool_narrative_delta') {
    const chunkIndex = Number(payload.chunk_index ?? 0)
    if (chunkIndex <= base.lastChunkIndex) return existing
    const field = String(payload.field ?? '')
    const delta = String(payload.delta ?? '')
    const next: ToolNarrativeState = { ...base, lastChunkIndex: chunkIndex }
    if (field === 'headline') next.publicUpdate = { ...base.publicUpdate, headline: delta }
    else if (field === 'summary') next.publicUpdate = { ...base.publicUpdate, summary: (base.publicUpdate.summary ?? '') + delta }
    else if (field === 'impact') next.publicUpdate = { ...base.publicUpdate, impact: (base.publicUpdate.impact ?? '') + delta }
    else if (field === 'next_action') next.publicUpdate = { ...base.publicUpdate, nextAction: (base.publicUpdate.nextAction ?? '') + delta }
    else if (field === 'narrative_text') {
      const narrativeText = (base.publicUpdate.narrativeText ?? '') + delta
      next.publicUpdate = {
        ...base.publicUpdate,
        narrativeText,
        headline: base.publicUpdate.headline || narrativeText.slice(0, 80),
        chunkFinal: false,
      }
    }
    else if (field === 'details') {
      const current = Array.isArray(base.publicUpdate.details) ? [...base.publicUpdate.details] : []
      current.push(delta)
      next.publicUpdate = { ...base.publicUpdate, details: current }
    }
    return existing.map((n, i) => (i === findIdx ? next : n))
  }

  // update / fallback: 用完整已校验 public_update 覆盖 partial。
  if (eventType === 'tool_narrative_update' || eventType === 'tool_narrative_fallback') {
    const rawUpdate = payload.public_update
    const parsed = extractPublicExecutionUpdate(rawUpdate)
    const next: ToolNarrativeState = {
      ...base,
      status: eventType === 'tool_narrative_fallback' ? 'fallback' : 'completed',
      source: eventType === 'tool_narrative_fallback' ? 'deterministic' : 'llm',
      publicUpdate: parsed ?? base.publicUpdate,
      completedAt: event.created_at
    }
    return existing.map((n, i) => (i === findIdx ? next : n))
  }

  // failed: 标记失败(保留已有 partial 以便展示确定性回退前的内容)。
  if (eventType === 'tool_narrative_failed') {
    const next: ToolNarrativeState = { ...base, status: 'failed', completedAt: event.created_at }
    return existing.map((n, i) => (i === findIdx ? next : n))
  }

  return existing
}

// ── Task Summary narrative reducer (Phase 2.9B.6) ──────────────────────

/**
 * 归约 task_summary_narrative_started / delta / update / failed / fallback
 * 事件到 TaskRunBlock.taskSummaryNarrative(唯一权威槽位)。
 *
 * 规则(§八):
 *  - started: 初始化 streaming 状态(旧 generation 不能覆盖新 generation);
 *  - delta: 幂等聚合(按 chunk_index 单调去重,追加到 partial publicUpdate);
 *  - update: 写入完整已校验 public_update + narrative_source/fallback_used;
 *  - fallback: 标记 fallback,保存确定性 public_update;
 *  - failed: 标记 failed;
 *  - event_id 去重由 seenEventIds 完成;canonical_order 单调更新;
 *  - task_completed 不得清空本槽(它不在此 reducer 里);
 *  - Narrative failed 不得修改 task.status。
 */
function reduceTaskSummaryNarrative(
  existing: TaskSummaryNarrativeState | null,
  event: NormalizedTaskEvent,
  payload: Record<string, unknown>
): TaskSummaryNarrativeState | null {
  const eventType = event.event_type as string
  const co = typeof event.canonical_order === 'number'
    ? event.canonical_order
    : typeof event.sequence_no === 'number'
      ? event.sequence_no
      : undefined

  // started: 初始化 streaming(或重置新 generation)。
  if (eventType === 'task_summary_narrative_started') {
    const generationId = String(payload.generation_id ?? '')
    const generationNo = Number(payload.generation_no ?? 1)
    const narrativeId = String(payload.narrative_id ?? '')
    const narrativeSource = String(payload.narrative_source ?? 'llm') as 'llm' | 'deterministic'
    // 旧 generation 不能覆盖新 generation。
    if (
      existing &&
      existing.generationNo != null &&
      existing.generationNo > generationNo
    ) {
      return existing
    }
    return {
      status: 'streaming',
      narrativeId,
      generationId,
      generationNo,
      narrativeSource,
      publicUpdate: emptyPublicExecutionUpdate(),
      lastCanonicalOrder: co ?? existing?.lastCanonicalOrder,
      createdAt: event.created_at
    }
  }

  if (!existing) return existing

  // delta: 幂等聚合(事件级去重由 seenEventIds 保证,field 追加)。
  if (eventType === 'task_summary_narrative_delta') {
    const base = existing.publicUpdate ?? emptyPublicExecutionUpdate()
    const next: TaskSummaryNarrativeState = { ...existing, lastCanonicalOrder: co ?? existing.lastCanonicalOrder }
    const field = String(payload.field ?? '')
    const delta = String(payload.delta ?? '')
    if (field === 'headline') {
      next.publicUpdate = { ...base, headline: delta }
    } else if (field === 'summary') {
      next.publicUpdate = { ...base, summary: (base.summary ?? '') + delta }
    } else if (field === 'impact') {
      next.publicUpdate = { ...base, impact: (base.impact ?? '') + delta }
    } else if (field === 'next_action') {
      next.publicUpdate = { ...base, nextAction: (base.nextAction ?? '') + delta }
    } else if (field === 'narrative_text') {
      const narrativeText = (base.narrativeText ?? '') + delta
      next.publicUpdate = {
        ...base,
        narrativeText,
        headline: base.headline || narrativeText.slice(0, 80),
        chunkFinal: false,
      }
    } else if (field === 'details') {
      const current = Array.isArray(base.details) ? [...base.details] : []
      current.push(delta)
      next.publicUpdate = { ...base, details: current }
    }
    return next
  }

  // update / fallback: 写入完整已校验 public_update。
  if (eventType === 'task_summary_narrative_update' || eventType === 'task_summary_narrative_fallback') {
    const rawUpdate = payload.public_update
    const parsed = extractPublicExecutionUpdate(rawUpdate)
    const isFallback = eventType === 'task_summary_narrative_fallback'
    const next: TaskSummaryNarrativeState = {
      ...existing,
      status: isFallback ? 'fallback' : 'completed',
      narrativeSource: isFallback ? 'deterministic' : 'llm',
      fallbackUsed: isFallback
        ? true
        : Boolean(payload.fallback_used),
      fallbackReason: String(payload.fallback_reason ?? payload.failure_category ?? '') || undefined,
      publicUpdate: parsed ?? existing.publicUpdate,
      lastCanonicalOrder: co ?? existing.lastCanonicalOrder,
      completedAt: event.created_at
    }
    return next
  }

  // failed: 标记失败(保留已有 partial)。
  if (eventType === 'task_summary_narrative_failed') {
    return {
      ...existing,
      status: 'failed',
      fallbackReason: String(payload.failure_category ?? payload.fallback_reason ?? '') || existing.fallbackReason,
      completedAt: event.created_at
    }
  }

  return existing
}

// ── Batch reducer (hydrate mode) ────────────────────────────────────

/**
 * Phase 2.9A.35: 计算事件的有效归约顺序。
 *  - task_created → 0(必须先于所有 Graph 事件处理)
 *  - sequence_no 非空 → 按 sequence_no
 *  - 其它 Legacy 事件 → canonical_order(或安全 fallback)
 */
export function buildEffectiveReductionOrder(
  eventType: string,
  sequenceNo: number | null | undefined,
  canonicalOrder: number | undefined
): number {
  if (eventType === 'task_created') return 0
  if (typeof sequenceNo === 'number' && Number.isFinite(sequenceNo)) return sequenceNo
  if (typeof canonicalOrder === 'number' && Number.isFinite(canonicalOrder)) return canonicalOrder
  return Number.MAX_SAFE_INTEGER
}

/**
 * Process a batch of events into a complete TaskRunBlock.
 * Used by hydrate path.
 * Phase 2.9A.35: 按 effective reduction order 排序后再归约,
 * 保证 task_created 先处理、Graph 事件按 sequence_no。
 */
export function reduceTaskEvents(
  initialBlock: TaskRunBlock,
  events: RawEventInput[],
  mode: 'hydrate' | 'live' = 'hydrate'
): TaskRunBlock {
  const sorted = [...events].sort((a, b) => {
    const orderA = a.reductionOrder ?? buildEffectiveReductionOrder(a.event_type ?? '', a.sequence_no, a.canonical_order)
    const orderB = b.reductionOrder ?? buildEffectiveReductionOrder(b.event_type ?? '', b.sequence_no, b.canonical_order)
    if (orderA !== orderB) return orderA - orderB
    // 同序稳定: 用 canonical_order/sequence_no 兜底
    const coA = a.canonical_order ?? a.sequence_no ?? 0
    const coB = b.canonical_order ?? b.sequence_no ?? 0
    if (coA !== coB) return coA - coB
    return (a.event_id ?? '').localeCompare(b.event_id ?? '')
  })
  let block = initialBlock
  for (const event of sorted) {
    block = reduceTaskEvent(block, event, mode)
  }
  return block
}

/**
 * Create an empty TaskRunBlock for a given task.
 */
export function createInitialTaskRunBlock(
  taskId: string,
  status: TaskStatus,
  mode: 'hydrate' | 'live',
  extra?: Partial<Omit<TaskRunBlock, 'triggerMessageId'>> & { triggerMessageId?: string | null }
): TaskRunBlock {
  const isTerminal = ['completed', 'failed', 'cancelled'].includes(status)
  return {
    taskId,
    triggerMessageId: null,
    status,
    lastUpdateMode: mode,
    collapsed: mode === 'hydrate' ? isTerminal : false,
    userCollapseOverride: null,
    events: [],
    seenEventIds: {},
    messages: [],
    toolExecutions: [],
    dynamicNarratives: [],
    toolNarratives: [],
    taskSummaryNarrative: null,
    artifacts: [],
    ...extra
  }
}
