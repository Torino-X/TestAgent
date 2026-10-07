/**
 * Phase 2.9A.30: Conversation Timeline Assembler.
 *
 * Produces ConversationItem[] from regular messages + taskRunBlocks.
 * Uses structured TimelineSortKey for stable ordering — no floating point.
 *
 * AgentRunItem is created directly from TaskRunBlock, not from task messages
 * in the main message list.
 */

import type { AgentTask, ChatMessage, TaskRunBlock } from '@/types'

// ── TimelineSortKey ─────────────────────────────────────────────────

export interface TimelineSortKey {
  /** Primary sort: conversation_sequence (messages) or trigger sequence (Task) */
  sequence: number
  /**
   * Phase 2.9A.35: 客户端顺序 — 乐观消息用它锚定位置。
   * 同一个发送动作产生的 User + Thinking 共享同一 clientOrder。
   */
  clientOrder?: number
  /**
   * Phase 2.9A.35: 同一 clientOrder 内 user=0, agent thinking=1。
   * 保证 User 永远排在 Thinking 之前(不再退化为 stableId 字母序)。
   */
  timelineRank?: number
  /** Same sequence: regular messages (rank=0) before Task (rank=1) */
  rank: number
  /** Same sequence+rank: sort by creation time */
  createdAt: number
  /** Final tiebreaker: stable string comparison */
  stableId: string
}

export function compareTimelineKeys(a: TimelineSortKey, b: TimelineSortKey): number {
  if (a.sequence !== b.sequence) return a.sequence - b.sequence
  if (a.clientOrder !== undefined || b.clientOrder !== undefined) {
    const ao = a.clientOrder ?? Number.MAX_SAFE_INTEGER
    const bo = b.clientOrder ?? Number.MAX_SAFE_INTEGER
    if (ao !== bo) return ao - bo
  }
  if (a.timelineRank !== undefined || b.timelineRank !== undefined) {
    const ar = a.timelineRank ?? 0
    const br = b.timelineRank ?? 0
    if (ar !== br) return ar - br
  }
  if (a.rank !== b.rank) return a.rank - b.rank
  if (a.createdAt !== b.createdAt) return a.createdAt - b.createdAt
  return a.stableId.localeCompare(b.stableId)
}

function safeParseTime(value?: string | null): number {
  if (!value) return 0
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function effectiveMessageSequence(message: ChatMessage, messages: ChatMessage[]): number {
  if (message.conversationSequence !== undefined) return message.conversationSequence
  if (message.clientOrder === undefined) return Number.MAX_SAFE_INTEGER

  // The server sequences the user message, while its local thinking
  // companion has no server sequence yet. Keep the pair together.
  const anchored = messages.find(
    (candidate) =>
      candidate !== message &&
      candidate.clientOrder === message.clientOrder &&
      candidate.conversationSequence !== undefined
  )
  return anchored?.conversationSequence ?? Number.MAX_SAFE_INTEGER
}

// ── ConversationItem types ──────────────────────────────────────────

export type PlainMessageItem = { id: string; kind: 'user' | 'agent'; message: ChatMessage }
export type AgentRunItem = {
  id: string
  kind: 'agent-run'
  runKey: string
  taskId?: string
  messages: ChatMessage[]
  block: TaskRunBlock
}
export type ConversationItem = PlainMessageItem | AgentRunItem

const USER_MESSAGE_TYPES = new Set<ChatMessage['type']>(['user_text', 'user_file'])

/**
 * Message role is the normal source of truth, but the message type is a
 * stronger UI boundary for stream races: assistant-shaped messages must not
 * become right-aligned just because a stale payload carried role=user.
 */
export function conversationMessageKind(message: ChatMessage): 'user' | 'agent' {
  return message.role === 'user' && USER_MESSAGE_TYPES.has(message.type) ? 'user' : 'agent'
}

// ── Assembler ───────────────────────────────────────────────────────

/**
 * Assemble the conversation timeline from regular messages + taskRunBlocks.
 *
 * - messages: only regular messages (no task events)
 * - taskRunBlocks: pre-assembled task run blocks
 * - tasks: AgentTask[] for triggerMessageId lookup
 *
 * Returns ConversationItem[] sorted by TimelineSortKey.
 */
export function assembleConversationTimeline(
  messages: ChatMessage[],
  taskRunBlocks: TaskRunBlock[],
  tasks: AgentTask[]
): ConversationItem[] {
  // Build triggerMessageSequenceMap
  const triggerMap = new Map<string, number | undefined>()
  for (const block of taskRunBlocks) {
    const task = tasks.find(t => t.task_id === block.taskId)
    const triggerMessageId = block.triggerMessageId ?? task?.triggerMessageId ?? null
    if (triggerMessageId) {
      const triggerMsg = messages.find(m => m.id === triggerMessageId)
      triggerMap.set(block.taskId, triggerMsg?.conversationSequence)
    }
  }

  // Build sort keys for regular messages
  const messageItems: Array<{ item: ConversationItem; key: TimelineSortKey }> = messages.map(m => ({
    item: { id: m.id, kind: conversationMessageKind(m), message: m },
    key: {
      sequence: effectiveMessageSequence(m, messages),
      clientOrder: m.clientOrder,
      timelineRank: m.timelineRank,
      rank: 0,
      createdAt: safeParseTime(m.createdAt),
      stableId: m.id
    }
  }))

  // Build sort keys for task run blocks
  const taskItems: Array<{ item: ConversationItem; key: TimelineSortKey }> = taskRunBlocks.map(block => {
    const triggerSeq = triggerMap.get(block.taskId)
    return {
      item: {
        id: `run_${block.taskId}`,
        kind: 'agent-run' as const,
        runKey: block.taskId,
        taskId: block.taskId,
        messages: block.messages,
        block
      },
      key: {
        sequence: triggerSeq ?? Number.MAX_SAFE_INTEGER,
        rank: 1,
        createdAt: safeParseTime(block.startedAt),
        stableId: `task:${block.taskId}`
      }
    }
  })

  // Merge and sort
  const all = [...messageItems, ...taskItems]
  all.sort((a, b) => compareTimelineKeys(a.key, b.key))

  return all.map(entry => entry.item)
}
