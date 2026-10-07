/**
 * Conversation store — list of conversations + active selection.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import type { AgentTask, Artifact, ChatMessage, Conversation, FileAttachment, KnowledgeMode, TaskRunBlock, TaskStatus } from '@/types'
import * as conversationApi from '@/api/conversationApi'
import * as messageApi from '@/api/messageApi'
import * as fileApi from '@/api/fileApi'
import * as agentApi from '@/api/agentApi'
import * as artifactApi from '@/api/artifactApi'
import { reduceTaskEvent, reduceTaskEvents, createInitialTaskRunBlock } from '@/composables/useTaskEventReducer'
import type { AgentEventRecord, ConfirmedConfirmation } from '@/api/agentApi'
import { normalizeTaskEventPayload } from '@/utils/eventPayload'

const EMPTY_CONVERSATION: Conversation = {
  id: 'conv_new',
  title: '新测试任务',
  subtitle: '空会话',
  state: 'empty',
  updatedAt: '刚刚',
  files: [],
  draftFiles: [],
  messages: [],
  latestTask: null
}

export const useConversationStore = defineStore('conversation', () => {
  const conversations = ref<Conversation[]>([])
  const activeConversationId = ref('conv_new')
  const loading = ref(false)
  const restoringConversation = ref(false)
  // Phase 2.9A.35: 每个会话的自增客户端消息顺序(每次发送只 +1)。
  const clientOrderCounters = ref<Record<string, number>>({})
  const knowledgeModes = ref<Record<string, KnowledgeMode>>({})
  const pendingInitialMessages = new Map<string, string>()
  const pendingInitialFiles = new Map<string, File[]>()
  const pendingInitialAttachments = new Map<string, FileAttachment[]>()
  const knowledgeModeStoragePrefix = 'testagent:knowledge-mode:'
  // Phase 2.9A.28: single-flight token — 只有最近一次 restoreConversation
  // 的结果可以写入 Store。旧的异步结果完成时被丢弃,防止并发 restore
  // 乱序覆盖（用户报告"确认后状态倒退到 RequirementParser"的根因之一）。
  let restoreGeneration = 0
  let newConversationRequest: Promise<Conversation> | null = null

  const activeConversation = computed(() => {
    if (activeConversationId.value === 'conv_new') {
      return EMPTY_CONVERSATION
    }
    return (
      conversations.value.find((c) => c.id === activeConversationId.value) ??
      EMPTY_CONVERSATION
    )
  })

  async function fetchConversations() {
    loading.value = true
    try {
      const summaries = await conversationApi.fetchConversations()
      conversations.value = summaries.map((summary) => {
        const existing = conversations.value.find((conversation) => conversation.id === summary.id)
        if (!existing) return summary
        return {
          ...summary,
          files: existing.files,
          draftFiles: existing.draftFiles,
          messages: existing.messages,
          latestTask: existing.latestTask ?? summary.latestTask
        }
      })
    } finally {
      loading.value = false
    }
  }

  async function fetchConversationDetail(id: string) {
    const detail = await conversationApi.fetchConversationDetail(id)
    const idx = conversations.value.findIndex((c) => c.id === id)
    if (idx >= 0) {
      const existing = conversations.value[idx]
      conversations.value[idx] = {
        ...detail,
        projectId: detail.projectId ?? existing.projectId,
        projectName: detail.projectName ?? existing.projectName
      }
    } else {
      conversations.value.push(detail)
    }
    return detail
  }

  function sortMessagesByCreatedAt(messages: ChatMessage[]) {
    // Phase 2.9A.26+: primary sort key is conversation_sequence
    // (a stable per-conversation monotonic integer assigned by the
    // backend at insert time).  Falls back to createdAt + id when
    // the server omits sequence (legacy rows / pre-migration caches).
    return [...messages].sort((left, right) => {
      const leftSeq = left.conversationSequence ?? Number.MAX_SAFE_INTEGER
      const rightSeq = right.conversationSequence ?? Number.MAX_SAFE_INTEGER
      if (leftSeq !== rightSeq) return leftSeq - rightSeq
      const leftTime = Date.parse(left.createdAt ?? '')
      const rightTime = Date.parse(right.createdAt ?? '')
      if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
        return leftTime - rightTime
      }
      // Final tiebreaker: stable message id.
      return left.id.localeCompare(right.id)
    })
  }

  function isOptimisticMessage(message: ChatMessage): boolean {
    return (
      message.optimistic === true ||
      message.id.startsWith('msg_user_pending_') ||
      message.id.startsWith('msg_agent_thinking_')
    )
  }

  function mergeOptimisticMessages(
    conversationId: string,
    restoredMessages: ChatMessage[]
  ): ChatMessage[] {
    const current = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!current) return restoredMessages

    const restoredIds = new Set(restoredMessages.map((message) => message.id))
    const pendingMessages = current.messages.filter(
      (message) => isOptimisticMessage(message) && !restoredIds.has(message.id)
    )
    return pendingMessages.length > 0
      ? sortMessagesByCreatedAt([...restoredMessages, ...pendingMessages])
      : restoredMessages
  }

  // ── Phase 2.9A.30: full task restore helpers ──────────────────────

  function chunkArray<T>(arr: T[], size: number): T[][] {
    const chunks: T[][] = []
    for (let i = 0; i < arr.length; i += size) {
      chunks.push(arr.slice(i, i + size))
    }
    return chunks
  }

  function isTerminalStatus(status: string): boolean {
    return ['completed', 'failed', 'cancelled'].includes(status)
  }

  function computeDurationMs(startedAt?: string | null, completedAt?: string | null): number | null {
    if (!startedAt || !completedAt) return null
    const start = Date.parse(startedAt)
    const end = Date.parse(completedAt)
    if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null
    return end - start
  }

  function findLatestNonTerminalTaskBlockIndex(blocks: TaskRunBlock[]): number {
    for (let index = blocks.length - 1; index >= 0; index -= 1) {
      if (!isTerminalStatus(blocks[index].status)) return index
    }
    return -1
  }

  function isReusableEmptyConversation(conversation: Conversation): boolean {
    if (conversation.id === 'conv_new') return false
    const messageCount = conversation.messageCount
    return (
      typeof messageCount === 'number'
        ? messageCount === 0
        : conversation.messages.length === 0
    )
  }

  function findReusableEmptyConversation(): Conversation | null {
    const active = conversations.value.find((conversation) => conversation.id === activeConversationId.value)
    if (active && isReusableEmptyConversation(active)) return active
    return conversations.value.find(isReusableEmptyConversation) ?? null
  }

  function formatReceiptSectionName(section: { code: string; title: string }): string {
    const code = section.code.trim()
    const title = section.title.trim()
    return code && !title.startsWith(code) ? `${code} ${title}`.trim() : title
  }

  function formatReceiptList(items: string[], limit = 6): string {
    if (!items.length) return '无'
    const visible = items.slice(0, limit).join('、')
    return items.length > limit ? `${visible} 等 ${items.length} 个章节` : visible
  }

  function truncateReceiptText(value: string, limit: number): string {
    const normalized = value.replace(/\s+/g, ' ').trim()
    return normalized.length > limit ? `${normalized.slice(0, limit)}…` : normalized
  }

  function buildConfirmationReceipt(confirmation: ConfirmedConfirmation) {
    if (confirmation.confirmationType === 'section_generation_config') {
      const sections = confirmation.response.sections ?? confirmation.request.sections ?? []
      const generated = sections
        .filter((section) => section.action === 'ai_generate')
        .map(formatReceiptSectionName)
      const retained = sections
        .filter((section) => section.action !== 'ai_generate')
        .map(formatReceiptSectionName)
      return {
        kind: 'section' as const,
        markdown: [
          `- **AI 生成：** ${formatReceiptList(generated)}`,
          `- **保留模板：** ${formatReceiptList(retained)}`
        ].join('\n')
      }
    }

    const answers = confirmation.response.answers ?? {}
    const conservativeGapIds = confirmation.response.conservativeGapIds ?? []
    const lines = (confirmation.request.cards ?? [])
      .map((card) => {
        const answer = answers[card.id]?.trim()
        if (answer) return `- **${truncateReceiptText(card.question, 34)}：** ${truncateReceiptText(answer, 72)}`
        if (conservativeGapIds.includes(card.id)) return `- **${truncateReceiptText(card.question, 34)}：** 按保守范围生成`
        return ''
      })
      .filter(Boolean)
    return {
      kind: 'clarification' as const,
      markdown: lines.length ? lines.join('\n') : '- **补充结果：** 已提交'
    }
  }

  function hydrateConfirmationReceipts(
    taskId: string,
    confirmations: ConfirmedConfirmation[],
    events: AgentEventRecord[]
  ): ChatMessage[] {
    return confirmations
      .filter((confirmation) => (
        confirmation.confirmationType === 'section_generation_config' ||
        confirmation.confirmationType === 'preparation_clarification'
      ))
      .map((confirmation) => {
      const requestEvent = events.find((event) => {
        if (event.event_type !== 'need_user_confirm') return false
        return normalizeTaskEventPayload(event.payload).confirmation_id === confirmation.confirmationId
      })
      const requestOrder = requestEvent?.canonical_order ?? requestEvent?.sequence_no
      return {
        id: `confirmation_receipt_${confirmation.confirmationId}`,
        type: 'agent_text',
        role: 'agent',
        taskId,
        confirmationId: confirmation.confirmationId,
        confirmationType: confirmation.confirmationType,
        confirmed: true,
        conversationSequence: typeof requestOrder === 'number' ? requestOrder + 0.5 : undefined,
        createdAt: confirmation.confirmedAt,
        timestamp: '',
        confirmationReceipt: buildConfirmationReceipt(confirmation)
      }
    })
  }

  function mergeAgentTask(existing: AgentTask | undefined | null, incoming: AgentTask): AgentTask {
    return {
      ...(existing ?? incoming),
      ...incoming,
      startedAt: incoming.startedAt ?? existing?.startedAt ?? null,
      completedAt: incoming.completedAt ?? existing?.completedAt ?? null,
      durationMs: incoming.durationMs ?? existing?.durationMs ?? null,
      triggerMessageId: incoming.triggerMessageId ?? existing?.triggerMessageId ?? null,
      run: incoming.run ?? existing?.run ?? null,
      runtimeStatus: incoming.runtimeStatus ?? existing?.runtimeStatus ?? null,
      activeRunId: incoming.activeRunId ?? existing?.activeRunId ?? null
    }
  }

  function upsertConversationTask(target: Conversation, task: AgentTask): AgentTask {
    const existingTasks = target.tasks ?? []
    const idx = existingTasks.findIndex((candidate) => candidate.task_id === task.task_id)
    const existing = idx >= 0 ? existingTasks[idx] : target.latestTask?.task_id === task.task_id ? target.latestTask : null
    const merged = mergeAgentTask(existing, task)
    const nextTasks = [...existingTasks]
    if (idx >= 0) {
      nextTasks[idx] = merged
    } else {
      nextTasks.push(merged)
    }
    target.tasks = nextTasks
    return merged
  }

  async function restoreSingleTaskRun(
    task: AgentTask
  ): Promise<TaskRunBlock> {
    const [detailResult, eventsResult, artifactsResult, pendingResult, confirmedResult] = await Promise.allSettled([
      agentApi.fetchTaskDetail(task.task_id),
      agentApi.fetchAllTaskEvents(task.task_id, { returnPageCursor: true }),
      artifactApi.fetchTaskArtifacts(task.task_id),
      task.status === 'waiting_user_confirm' || task.runtimeStatus === 'running'
        ? agentApi.fetchPendingConfirmation(task.task_id)
        : Promise.resolve(null),
      agentApi.fetchConfirmedConfirmations(task.task_id)
    ])

    // Detail — use list fallback if detail fails
    const taskDetail = detailResult.status === 'fulfilled' ? detailResult.value : task
    // Events — empty on failure
    const eventsResultValue = eventsResult.status === 'fulfilled' ? eventsResult.value : null
    const events: AgentEventRecord[] = Array.isArray(eventsResultValue)
      ? eventsResultValue
      : eventsResultValue?.events ?? []
    // 分页 cursor 独立记录(event-list 最后一页 next_cursor)
    const eventListPageCursor = !Array.isArray(eventsResultValue)
      ? eventsResultValue?.lastPageCursor ?? undefined
      : undefined
    // Pending confirmation — authoritative fallback if events can't restore full confirmation
    const pendingConfirmation = pendingResult.status === 'fulfilled' ? pendingResult.value : null
    const confirmedConfirmations = confirmedResult.status === 'fulfilled' ? confirmedResult.value : []
    // Artifacts — empty on failure
    const arts: Artifact[] = artifactsResult.status === 'fulfilled' ? artifactsResult.value : []

    // Build initial block from detail
    const status: TaskStatus = taskDetail.status ?? task.status ?? 'running'
    // Phase 2.9A.35: TaskRunBlock.startedAt 优先级 —
    // run.started_at → task.started_at → 最早有效运行事件时间 → null
    const startedAt =
      taskDetail.run?.startedAt ??
      taskDetail.startedAt ??
      task.startedAt ??
      undefined
    let block = createInitialTaskRunBlock(
      task.task_id,
      status,
      'hydrate',
      {
        taskType: taskDetail.task_type ?? task.task_type,
        triggerMessageId: taskDetail.triggerMessageId ?? task.triggerMessageId ?? null,
        startedAt,
        completedAt: taskDetail.completedAt ?? task.completedAt ?? undefined,
        durationMs: taskDetail.durationMs ?? task.durationMs,
        reviewResult: (taskDetail as any).review_result ?? undefined,
        artifacts: arts ?? [],
        // Phase 2.9A.35: waiting 状态下 currentPhase 直接标记为确认阶段
        currentPhase: status === 'waiting_user_confirm' ? 'section_confirmation' : undefined
      }
    )

    // Run reducer on all events
    if (events.length > 0) {
      block = reduceTaskEvents(block, events, 'hydrate')
    }

    const confirmationReceipts = hydrateConfirmationReceipts(
      task.task_id,
      confirmedConfirmations,
      events
    )
    if (confirmationReceipts.length > 0) {
      block = {
        ...block,
        messages: [
          ...block.messages,
          ...confirmationReceipts.filter(
            (receipt) => !block.messages.some((message) => message.id === receipt.id)
          )
        ]
      }
    }

    // Phase 2.9A.35: event-list 分页 cursor 独立记录(与 SSE 无关)
    if (eventListPageCursor !== undefined) {
      block = { ...block, eventListPageCursor }
    }

    // Phase 2.9A.35: 从 event-list 恢复时,若 reducer 因 payload 契约
    // 未生成完整 confirmation,用 pending-confirmation 接口权威 fallback。
    const hasSectionConfirm = block.messages.some((m) => m.type === 'section_confirm' && m.sections?.length)
    if (status === 'waiting_user_confirm' && !hasSectionConfirm && pendingConfirmation) {
      const confirmMsg: ChatMessage = {
        id: `pending_${pendingConfirmation.confirmationId}`,
        type: 'section_confirm',
        role: 'agent',
        taskId: task.task_id,
        confirmationId: pendingConfirmation.confirmationId,
        confirmationType: pendingConfirmation.confirmationType,
        sections: pendingConfirmation.sections,
        createdAt: startedAt,
        timestamp: ''
      }
      block = {
        ...block,
        messages: [...block.messages, confirmMsg],
        status: 'waiting_user_confirm',
        currentPhase: 'section_confirmation'
      }
    }

    // Override status from detail (detail is authoritative)
    if (isTerminalStatus(status) && block.status !== status) {
      block = { ...block, status }
    }

    // Phase 2.9A.35: Task Detail 权威状态在历史事件重放后最终应用。
    // 业务 task.status 优先于 runtime_status — waiting_user_confirm 必须保持。
    if (status === 'waiting_user_confirm' && block.status !== 'waiting_user_confirm') {
      block = { ...block, status, currentPhase: 'section_confirmation' }
    }

    // Set collapsed based on status
    if (isTerminalStatus(status)) {
      block = { ...block, collapsed: true }
    }

    return block
  }

  function createFallbackTaskRunBlock(task: AgentTask, error: Error): TaskRunBlock {
    return createInitialTaskRunBlock(
      task.task_id,
      (task.status ?? 'running') as TaskStatus,
      'hydrate',
      {
        taskType: task.task_type,
        triggerMessageId: task.triggerMessageId ?? null,
        startedAt: task.startedAt ?? undefined,
        completedAt: task.completedAt ?? undefined,
        durationMs: task.durationMs,
        restoreError: { code: 'RESTORE_FAILED', message: error.message }
      }
    )
  }

  async function restoreAllTaskRuns(tasks: AgentTask[]): Promise<TaskRunBlock[]> {
    const results: TaskRunBlock[] = []
    const chunks = chunkArray(tasks, 3) // concurrency limit
    for (const chunk of chunks) {
      const settled = await Promise.allSettled(
        chunk.map(task => restoreSingleTaskRun(task))
      )
      for (let i = 0; i < settled.length; i++) {
        const result = settled[i]
        if (result.status === 'fulfilled') {
          results.push(result.value)
        } else {
          results.push(createFallbackTaskRunBlock(chunk[i], result.reason instanceof Error ? result.reason : new Error(String(result.reason))))
        }
      }
    }
    return results
  }

  async function restoreConversation(id: string) {
    // Phase 2.9A.28: single-flight guard
    const myGeneration = ++restoreGeneration
    activeConversationId.value = id
    restoringConversation.value = true
    const _diag: Record<string, unknown> = { conversationId: id }
    try {
      const [detail, messages, files] = await Promise.all([
        conversationApi.fetchConversationDetail(id),
        messageApi.fetchMessages(id),
        fileApi.fetchConversationFiles(id)
      ])
      // Phase 2.9A.30: regular messages only (no task events)
      const regularMessages = messages.filter(m => !m.taskId)
      const allTasks: AgentTask[] = detail.tasks ?? (detail.latestTask ? [detail.latestTask] : [])
      _diag.apiMessages = messages.length
      _diag.apiFiles = files.length
      _diag.taskCount = allTasks.length

      // Phase 2.9A.30: restore ALL tasks with detail+events+artifacts
      const taskRunBlocks = await restoreAllTaskRuns(allTasks)

      // Build triggerMessageSequenceMap for timeline assembly
      const triggerMessageSequenceMap = new Map<string, number | undefined>()
      for (const block of taskRunBlocks) {
        if (block.triggerMessageId) {
          const triggerMsg = regularMessages.find(m => m.id === block.triggerMessageId)
          triggerMessageSequenceMap.set(block.taskId, triggerMsg?.conversationSequence)
        }
      }

      // Sort regular messages
      // A history request can finish after sendMessageStream has already
      // inserted local pending messages.  Do not let that stale snapshot
      // erase the message the user is currently waiting on.
      const sortedMessages = mergeOptimisticMessages(
        id,
        sortMessagesByCreatedAt(regularMessages)
      )

      // Find latestTask (last in list — tasks are ordered by created_at ASC)
      const latestTask = allTasks.length > 0 ? allTasks[allTasks.length - 1] : null
      // Keep local composer attachments when a post-navigation restore returns
      // before the user has sent them to the conversation.
      const existingConversation = conversations.value.find((conversation) => conversation.id === id)
      const existingDraftFiles = existingConversation?.draftFiles ?? []

      const restored: Conversation = {
        ...detail,
        projectId: detail.projectId ?? existingConversation?.projectId,
        projectName: detail.projectName ?? existingConversation?.projectName,
        tasks: allTasks,
        latestTask: latestTask ?? detail.latestTask ?? null,
        files,
        draftFiles: existingDraftFiles,
        messages: sortedMessages,
        taskRunBlocks,
      }
      // Single-flight guard
      if (myGeneration !== restoreGeneration) {
        console.log('[restoreConversation] stale generation, discarding', { id, myGeneration, current: restoreGeneration })
        return restored
      }
      const idx = conversations.value.findIndex((c) => c.id === id)
      if (idx >= 0) {
        conversations.value[idx] = restored
      } else {
        conversations.value = [restored, ...conversations.value]
      }
      _diag.finalMessages = sortedMessages.length
      _diag.finalTaskBlocks = taskRunBlocks.length
      _diag.finalFiles = files.length
      console.log('[restoreConversation] done', _diag)
      return restored
    } catch (err) {
      _diag.error = err instanceof Error ? err.message : String(err)
      console.error('[restoreConversation] failed', _diag)
      throw err
    } finally {
      // A previous restore may finish after a newer conversation has started.
      // Only the latest request is allowed to hide the history loading state.
      if (myGeneration === restoreGeneration) {
        restoringConversation.value = false
      }
    }
  }

  // ── Phase 2.9A.30: Store-only task event entry points ────────────

  /**
   * Hydrate entry point — called after restoreConversation builds taskRunBlocks.
   * Protects against old async Hydrate overwriting newer Live state.
   */
  function applyHydrateTaskRun(
    conversationId: string,
    taskRun: TaskRunBlock
  ) {
    const conv = conversations.value.find(c => c.id === conversationId)
    if (!conv) return

    const existing = conv.taskRunBlocks?.find(b => b.taskId === taskRun.taskId)
    // Race protection: if block was updated by Live mode, don't overwrite
    if (existing && existing.lastUpdateMode === 'live' && existing.events.length > 0) {
      return
    }

    if (existing) {
      const idx = conv.taskRunBlocks!.indexOf(existing)
      conv.taskRunBlocks = [...conv.taskRunBlocks!]
      conv.taskRunBlocks[idx] = taskRun
    } else {
      conv.taskRunBlocks = [...(conv.taskRunBlocks ?? []), taskRun]
    }
  }

  /**
   * Live SSE entry point — the ONLY way to update a TaskRunBlock from SSE events.
   * Creates block if it doesn't exist, then runs reduceTaskEvent.
   */
  function applyLiveTaskEvent(
    conversationId: string,
    taskId: string,
    rawEvent: { event_id?: string; event_type?: string; task_id?: string; payload?: unknown; created_at?: string; [key: string]: unknown }
  ) {
    const conv = conversations.value.find(c => c.id === conversationId)
    if (!conv) return

    let block = conv.taskRunBlocks?.find(b => b.taskId === taskId)
    const task =
      conv.tasks?.find(t => t.task_id === taskId) ??
      (conv.latestTask?.task_id === taskId ? conv.latestTask : null)
    if (!block) {
      block = createInitialTaskRunBlock(
        taskId,
        (task?.status ?? 'running') as TaskStatus,
        'live',
        {
          taskType: task?.task_type,
          triggerMessageId: task?.triggerMessageId ?? null,
          startedAt: task?.run?.startedAt ?? task?.startedAt ?? undefined,
          completedAt: task?.completedAt ?? task?.run?.finishedAt ?? undefined,
          durationMs: task?.durationMs ?? task?.run?.durationMs
        }
      )
      conv.taskRunBlocks = [...(conv.taskRunBlocks ?? []), block]
    } else if (!block.triggerMessageId && task?.triggerMessageId) {
      block = {
        ...block,
        triggerMessageId: task.triggerMessageId,
        startedAt: block.startedAt ?? task.run?.startedAt ?? task.startedAt ?? undefined,
        completedAt: block.completedAt ?? task.completedAt ?? task.run?.finishedAt ?? undefined,
        durationMs: block.durationMs ?? task.durationMs ?? task.run?.durationMs
      }
      const blockIdx = conv.taskRunBlocks?.findIndex(b => b.taskId === taskId) ?? -1
      if (blockIdx >= 0 && conv.taskRunBlocks) {
        conv.taskRunBlocks = [...conv.taskRunBlocks]
        conv.taskRunBlocks[blockIdx] = block
      }
    }

    let updated = reduceTaskEvent(block, rawEvent, 'live')
    if (!updated.triggerMessageId && task?.triggerMessageId) {
      updated = { ...updated, triggerMessageId: task.triggerMessageId }
    }
    const idx = conv.taskRunBlocks!.indexOf(block)
    conv.taskRunBlocks = [...conv.taskRunBlocks!]
    conv.taskRunBlocks[idx] = updated
  }

  /**
   * Set collapsed state for a specific task run block.
   */
  function setTaskRunCollapsed(
    conversationId: string,
    taskId: string,
    collapsed: boolean,
    options?: { userInitiated?: boolean }
  ) {
    const conv = conversations.value.find(c => c.id === conversationId)
    if (!conv) return

    const block = conv.taskRunBlocks?.find(b => b.taskId === taskId)
    if (!block) return

    const idx = conv.taskRunBlocks!.indexOf(block)
    conv.taskRunBlocks = [...conv.taskRunBlocks!]
    conv.taskRunBlocks[idx] = {
      ...block,
      collapsed,
      userCollapseOverride: options?.userInitiated ? collapsed : block.userCollapseOverride
    }
  }

  function setActiveConversation(id?: string) {
    activeConversationId.value = id ?? 'conv_new'
  }

  function normalizeKnowledgeMode(value: unknown): KnowledgeMode {
    return value === 'MAAS_STRICT' ? 'MAAS_STRICT' : 'AUTO'
  }

  function readStoredKnowledgeMode(conversationId: string): KnowledgeMode | null {
    if (typeof window === 'undefined') return null
    const stored = window.localStorage.getItem(`${knowledgeModeStoragePrefix}${conversationId}`)
    if (stored == null) return null
    return normalizeKnowledgeMode(stored)
  }

  function writeStoredKnowledgeMode(conversationId: string, mode: KnowledgeMode) {
    if (typeof window === 'undefined') return
    window.localStorage.setItem(`${knowledgeModeStoragePrefix}${conversationId}`, mode)
  }

  function getKnowledgeMode(conversationId?: string): KnowledgeMode {
    const id = conversationId ?? activeConversationId.value
    if (!id || id === 'conv_new') return 'AUTO'
    return knowledgeModes.value[id] ?? 'AUTO'
  }

  async function loadKnowledgeMode(conversationId: string): Promise<KnowledgeMode> {
    if (!conversationId || conversationId === 'conv_new') return 'AUTO'
    try {
      const settings = await conversationApi.fetchConversationContextSettings(conversationId)
      const mode = normalizeKnowledgeMode(settings.knowledge_mode)
      knowledgeModes.value = { ...knowledgeModes.value, [conversationId]: mode }
      writeStoredKnowledgeMode(conversationId, mode)
      return mode
    } catch {
      const fallback = readStoredKnowledgeMode(conversationId) ?? getKnowledgeMode(conversationId)
      knowledgeModes.value = { ...knowledgeModes.value, [conversationId]: fallback }
      return fallback
    }
  }

  async function setKnowledgeMode(conversationId: string, mode: KnowledgeMode): Promise<KnowledgeMode> {
    if (!conversationId || conversationId === 'conv_new') return 'AUTO'
    const previous = getKnowledgeMode(conversationId)
    knowledgeModes.value = { ...knowledgeModes.value, [conversationId]: mode }
    writeStoredKnowledgeMode(conversationId, mode)
    try {
      const settings = await conversationApi.updateConversationKnowledgeMode(conversationId, mode)
      const confirmedMode = normalizeKnowledgeMode(settings.knowledge_mode ?? mode)
      knowledgeModes.value = { ...knowledgeModes.value, [conversationId]: confirmedMode }
      writeStoredKnowledgeMode(conversationId, confirmedMode)
      return confirmedMode
    } catch (err) {
      knowledgeModes.value = { ...knowledgeModes.value, [conversationId]: previous }
      writeStoredKnowledgeMode(conversationId, previous)
      throw err
    }
  }

  async function toggleKnowledgeMode(conversationId: string): Promise<KnowledgeMode> {
    const nextMode: KnowledgeMode = getKnowledgeMode(conversationId) === 'MAAS_STRICT' ? 'AUTO' : 'MAAS_STRICT'
    return setKnowledgeMode(conversationId, nextMode)
  }

  /**
   * Phase 2.9A.35: 为指定会话分配下一个客户端消息顺序号。
   * 每次调用 +1;发送一次消息只调用一次,User 与 Thinking 共享同一值。
   */
  function nextClientOrder(conversationId: string): number {
    const next = (clientOrderCounters.value[conversationId] ?? 0) + 1
    clientOrderCounters.value = { ...clientOrderCounters.value, [conversationId]: next }
    return next
  }

  async function ensureActiveConversation(currentConversationId?: string) {
    if (currentConversationId === 'conv_new' || activeConversationId.value === 'conv_new') {
      return startNewConversation()
    }
    const active = activeConversation.value
    if (active.id !== 'conv_new') {
      return active
    }
    return startNewConversation()
  }

  async function startNewConversation() {
    const reusable = findReusableEmptyConversation()
    if (reusable) {
      activeConversationId.value = reusable.id
      return reusable
    }
    if (newConversationRequest) return newConversationRequest

    const request = createNewConversation()
    newConversationRequest = request
    try {
      return await request
    } finally {
      if (newConversationRequest === request) {
        newConversationRequest = null
      }
    }
  }

  async function createNewConversation() {
    const created = await conversationApi.createConversation()
    conversations.value = [created, ...conversations.value]
    activeConversationId.value = created.id
    return created
  }

  function createConversation() {
    activeConversationId.value = 'conv_new'
  }

  function addDraftFile(conversationId: string, file: Conversation['draftFiles'][number]) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return
    target.draftFiles = [...target.draftFiles, file]
    target.files = [...target.files, file]
    target.fileCount = Math.max(target.fileCount ?? 0, target.files.length)
    target.state = 'uploaded'
  }

  function updateDraftFile(
    conversationId: string,
    fileId: string,
    patch: Partial<Conversation['draftFiles'][number]>
  ) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return
    const applyPatch = (file: Conversation['files'][number]) =>
      file.id === fileId ? { ...file, ...patch } : file
    target.draftFiles = target.draftFiles.map(applyPatch)
    target.files = target.files.map(applyPatch)
  }

  function replaceDraftFile(
    conversationId: string,
    fileId: string,
    file: Conversation['draftFiles'][number]
  ) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return
    const replaceFile = (files: Conversation['files']) => {
      const idx = files.findIndex((item) => item.id === fileId)
      if (idx < 0) return [...files, file]
      const next = [...files]
      next[idx] = file
      return next
    }
    target.draftFiles = replaceFile(target.draftFiles)
    target.files = replaceFile(target.files)
    target.fileCount = target.files.length
    target.state = 'uploaded'
  }

  function clearDraftFiles(conversationId: string) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (target) target.draftFiles = []
  }

  function removeDraftFile(conversationId: string, fileId: string) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return
    const removed = target.draftFiles.find((file) => file.id === fileId)
    target.draftFiles = target.draftFiles.filter((file) => file.id !== fileId)
    if (removed?.localOnly) {
      target.files = target.files.filter((file) => file.id !== fileId)
    }
    target.fileCount = target.files.length
  }

  function updateFile(conversationId: string, file: Conversation['files'][number]) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return
    const mergeFile = (files: Conversation['files']) => {
      const idx = files.findIndex((item) => item.id === file.id)
      if (idx < 0) return [...files, file]
      const next = [...files]
      next[idx] = file
      return next
    }
    target.files = mergeFile(target.files)
    target.draftFiles = mergeFile(target.draftFiles)
    target.fileCount = target.files.length
  }

  function setConversationFiles(conversationId: string, files: Conversation['files']) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return
    target.files = files
    target.fileCount = files.length
  }

  function appendMessages(conversationId: string, messages: Conversation['messages']) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return
    // Phase 2.9A.5:按 id 去重,与 mergeMessages / restoreTaskMessages 语义一致。
    // SSE 历史回放 + 实时事件可能产生相同 id 的消息
    // (例:tool_started 在 pre-confirm / post-confirm 重连时重复 push),
    // 之前不做去重导致 tool 永远显示 running(已完成 finished 也未合并)。
    const existingIds = new Set(target.messages.map((m) => m.id))
    const newMessages = messages.filter((m) => !existingIds.has(m.id))
    if (newMessages.length === 0) return
    target.messages = [...target.messages, ...newMessages]
    target.messageCount = (target.messageCount ?? target.messages.length - newMessages.length) + newMessages.length
    target.state = target.state === 'empty' ? 'planning' : target.state
  }

  function updateMessage(conversationId: string, messageId: string, patch: Partial<Conversation['messages'][number]>) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return false
    const idx = target.messages.findIndex((message) => message.id === messageId)
    if (idx < 0) {
      const blocks = target.taskRunBlocks ?? []
      const blockIdx = blocks.findIndex((block) => block.messages.some((message) => message.id === messageId))
      if (blockIdx < 0) return false
      const block = blocks[blockIdx]
      const messageIdx = block.messages.findIndex((message) => message.id === messageId)
      if (messageIdx < 0) return false
      const nextMessages = [...block.messages]
      nextMessages[messageIdx] = { ...nextMessages[messageIdx], ...patch }
      const nextBlocks = [...blocks]
      nextBlocks[blockIdx] = { ...block, messages: nextMessages }
      target.taskRunBlocks = nextBlocks
      return true
    }
    const messages = [...target.messages]
    messages[idx] = { ...messages[idx], ...patch }
    target.messages = messages
    return true
  }

  function removeMessage(conversationId: string, messageId: string) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return false
    let removed = false
    const next = target.messages.filter((message) => message.id !== messageId)
    const removedFromConversationMessages = next.length !== target.messages.length
    if (removedFromConversationMessages) {
      target.messages = next
      removed = true
    }
    if (target.taskRunBlocks?.length) {
      const nextBlocks = target.taskRunBlocks.map((block) => {
        const blockMessages = block.messages.filter((message) => message.id !== messageId)
        if (blockMessages.length === block.messages.length) return block
        removed = true
        return { ...block, messages: blockMessages }
      })
      if (removed) target.taskRunBlocks = nextBlocks
    }
    if (!removed) return false
    if (removedFromConversationMessages && typeof target.messageCount === 'number') {
      target.messageCount = Math.max(0, target.messageCount - 1)
    }
    return true
  }

  function removeTaskThinkingPlaceholders(conversationId: string, anchorMessageId?: string | null): number {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return 0
    const placeholder = anchorMessageId
      ? target.messages.find((message) => message.id === anchorMessageId && message.thinking)
      : undefined
    const resolvedAnchorMessageId = placeholder?.anchorMessageId ?? anchorMessageId
    const shouldRemove = (message: ChatMessage) => {
      if (message.role !== 'agent' || message.type !== 'agent_text' || !message.thinking) return false
      return (
        !resolvedAnchorMessageId ||
        message.id === resolvedAnchorMessageId ||
        message.anchorMessageId === resolvedAnchorMessageId
      )
    }
    const before = target.messages.length
    const next = target.messages.filter((message) => !shouldRemove(message))
    const removed = before - next.length
    if (removed > 0) target.messages = next
    return removed
  }

  function setConversationState(id: string, state: Conversation['state']) {
    const target = conversations.value.find((conversation) => conversation.id === id)
    if (target) target.state = state
  }

  function mergeConversationSummary(summary: Conversation) {
    const idx = conversations.value.findIndex((conversation) => conversation.id === summary.id)
    if (idx < 0) {
      conversations.value = [summary, ...conversations.value]
      return
    }
    conversations.value[idx] = {
      ...conversations.value[idx],
      title: summary.title || conversations.value[idx].title,
      subtitle: summary.subtitle || conversations.value[idx].subtitle,
      state: summary.state || conversations.value[idx].state,
      updatedAt: summary.updatedAt || conversations.value[idx].updatedAt,
      projectId: summary.projectId ?? conversations.value[idx].projectId,
      projectName: summary.projectName ?? conversations.value[idx].projectName
    }
  }

  function setConversationProject(id: string, project: { id: string; name: string } | null) {
    const target = conversations.value.find((conversation) => conversation.id === id)
    if (!target) return
    target.projectId = project?.id ?? null
    target.projectName = project?.name ?? null
  }

  function queueInitialMessage(conversationId: string, text: string) {
    const value = text.trim()
    if (!conversationId || !value) return
    pendingInitialMessages.set(conversationId, value)
  }

  function takeInitialMessage(conversationId: string): string | null {
    const value = pendingInitialMessages.get(conversationId) ?? null
    pendingInitialMessages.delete(conversationId)
    return value
  }

  function queueInitialFiles(conversationId: string, files: File[]) {
    if (!conversationId || !files.length) return
    pendingInitialFiles.set(conversationId, [...files])
  }

  function takeInitialFiles(conversationId: string): File[] {
    const files = pendingInitialFiles.get(conversationId) ?? []
    pendingInitialFiles.delete(conversationId)
    return [...files]
  }

  function queueInitialAttachments(conversationId: string, files: FileAttachment[]) {
    if (!conversationId || !files.length) return
    pendingInitialAttachments.set(conversationId, [...files])
  }

  function takeInitialAttachments(conversationId: string): FileAttachment[] {
    const files = pendingInitialAttachments.get(conversationId) ?? []
    pendingInitialAttachments.delete(conversationId)
    return [...files]
  }

  function setLatestTask(conversationId: string, task: AgentTask | null) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return
    if (!task) {
      target.latestTask = null
      return
    }
    const merged = upsertConversationTask(target, task)
    target.latestTask = merged
    const block = target.taskRunBlocks?.find((candidate) => candidate.taskId === merged.task_id)
    if (block) {
      const idx = target.taskRunBlocks!.indexOf(block)
      target.taskRunBlocks = [...target.taskRunBlocks!]
      target.taskRunBlocks[idx] = {
        ...block,
        taskType: block.taskType ?? merged.task_type,
        triggerMessageId: block.triggerMessageId ?? merged.triggerMessageId ?? null,
        startedAt: block.startedAt ?? merged.run?.startedAt ?? merged.startedAt ?? undefined,
        completedAt: block.completedAt ?? merged.completedAt ?? merged.run?.finishedAt ?? undefined,
        durationMs: block.durationMs ?? merged.durationMs ?? merged.run?.durationMs
      }
    }
  }

  function updateLatestTaskStatus(
    conversationId: string,
    status: string,
    options?: {
      taskId?: string
      startedAt?: string | null
      completedAt?: string | null
      durationMs?: number | null
    }
  ) {
    const target = conversations.value.find((conversation) => conversation.id === conversationId)
    if (!target) return

    const taskId = options?.taskId ?? target.latestTask?.task_id
    const terminal = isTerminalStatus(status)
    const patchTask = (task: AgentTask): AgentTask => {
      const startedAt = task.startedAt ?? task.run?.startedAt ?? options?.startedAt ?? null
      const completedAt = terminal
        ? (options?.completedAt ?? task.completedAt ?? task.run?.finishedAt ?? new Date().toISOString())
          : (task.completedAt ?? null)
      const durationMs = terminal
        ? (options?.durationMs ?? task.durationMs ?? task.run?.durationMs ?? computeDurationMs(startedAt, completedAt))
        : (task.durationMs ?? null)
      return {
        ...task,
        status: status as AgentTask['status'],
        completedAt,
        durationMs,
        startedAt: startedAt ?? undefined
      }
    }

    const hasExactTask = !!taskId && !!target.tasks?.some((task) => task.task_id === taskId)
    const shouldPatchLatestTask =
      !!target.latestTask &&
      (!taskId ||
        target.latestTask.task_id === taskId ||
        (terminal && !isTerminalStatus(target.latestTask.status)))

    if (target.latestTask && shouldPatchLatestTask) {
      target.latestTask = patchTask(target.latestTask)
    }

    if (target.tasks?.length) {
      target.tasks = target.tasks.map((task) => {
        if (task.task_id === taskId) return patchTask(task)
        if (terminal && !hasExactTask && target.latestTask?.task_id === task.task_id) return patchTask(task)
        return task
      })
    }

    if (target.taskRunBlocks?.length) {
      const exactBlockIndex = taskId
        ? target.taskRunBlocks.findIndex((block) => block.taskId === taskId)
        : -1
      const fallbackBlockIndex = terminal && exactBlockIndex < 0
        ? findLatestNonTerminalTaskBlockIndex(target.taskRunBlocks)
        : -1
      const blockIndexToPatch = exactBlockIndex >= 0 ? exactBlockIndex : fallbackBlockIndex
      if (blockIndexToPatch < 0) return
      target.taskRunBlocks = target.taskRunBlocks.map((block, index) => {
        if (index !== blockIndexToPatch) return block
        const effectiveTaskId = taskId && block.taskId === taskId ? taskId : block.taskId
        const latest = target.latestTask?.task_id === effectiveTaskId ? target.latestTask : undefined
        const completedAt = terminal
          ? (block.completedAt ?? options?.completedAt ?? latest?.completedAt ?? latest?.run?.finishedAt ?? new Date().toISOString())
          : block.completedAt
        const durationMs = terminal
          ? (block.durationMs ?? options?.durationMs ?? latest?.durationMs ?? latest?.run?.durationMs ?? computeDurationMs(block.startedAt ?? latest?.startedAt ?? latest?.run?.startedAt, completedAt))
          : block.durationMs
        return {
          ...block,
          status: status as TaskStatus,
          completedAt: completedAt ?? undefined,
          durationMs,
          startedAt: block.startedAt ?? options?.startedAt ?? latest?.startedAt ?? latest?.run?.startedAt ?? undefined
        }
      })
    }
  }

  async function updateConversationTitle(id: string, title: string) {
    const updated = await conversationApi.updateConversationTitle(id, title)
    const idx = conversations.value.findIndex((c) => c.id === id)
    if (idx >= 0) {
      const existing = conversations.value[idx]
      conversations.value[idx] = {
        ...updated,
        files: existing.files,
        draftFiles: existing.draftFiles,
        messages: existing.messages,
        latestTask: existing.latestTask ?? updated.latestTask
      }
    }
  }

  async function deleteConversation(id: string) {
    await conversationApi.deleteConversation(id)
    conversations.value = conversations.value.filter((c) => c.id !== id)
    if (activeConversationId.value === id) {
      activeConversationId.value = 'conv_new'
    }
  }

  // Eagerly load on first store access
  fetchConversations()

  return {
    conversations,
    activeConversationId,
    activeConversation,
    loading,
    restoringConversation,
    knowledgeModes,
    fetchConversations,
    fetchConversationDetail,
    restoreConversation,
    setActiveConversation,
    getKnowledgeMode,
    loadKnowledgeMode,
    setKnowledgeMode,
    toggleKnowledgeMode,
    nextClientOrder,
    ensureActiveConversation,
    startNewConversation,
    createNewConversation,
    createConversation,
    addDraftFile,
    updateDraftFile,
    replaceDraftFile,
    clearDraftFiles,
    removeDraftFile,
    updateFile,
    setConversationFiles,
    appendMessages,
    updateMessage,
    removeMessage,
    removeTaskThinkingPlaceholders,
    setConversationState,
    mergeConversationSummary,
    setConversationProject,
    queueInitialMessage,
    takeInitialMessage,
    queueInitialFiles,
    takeInitialFiles,
    queueInitialAttachments,
    takeInitialAttachments,
    setLatestTask,
    updateLatestTaskStatus,
    updateConversationTitle,
    deleteConversation,
    // Phase 2.9A.30: task event entry points
    applyHydrateTaskRun,
    applyLiveTaskEvent,
    setTaskRunCollapsed
  }
})
