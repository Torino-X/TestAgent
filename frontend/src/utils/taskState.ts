import type { AgentEventType, AgentTask, ChatMessage, ConversationState, TaskStatus } from '@/types'

const blockingTaskStatuses: TaskStatus[] = [
  'created',
  'planning',
  'running',
  'waiting_user',
  'waiting_user_confirm',
  'generating',
  'reviewing',
  'exporting'
]

const taskLifecycleEvents = new Set<AgentEventType>([
  'task_created',
  'plan_created',
  'plan_updated',
  'plan_step_started',
  'plan_step_completed',
  'plan_step_failed',
  'tool_started',
  'tool_finished',
  'tool_failed',
  'retrying',
  'requirement_summary',
  'template_summary',
  'knowledge_summary',
  'need_user_confirm',
  'task_waiting',
  'task_resumed',
  'generating_started',
  'generating_progress',
  'review_completed',
  'artifact_created',
  'task_failed',
  'task_completed',
  'task_cancelled',
  'format_loss_confirm_requested',
  'format_loss_decision_recorded',
  'format_loss_resuming',
  'incremental_started',
  'incremental_completed',
  'incremental_failed',
  'incremental_fallback'
])

export function taskEventTimestamps(
  messages: Pick<ChatMessage, 'eventType' | 'createdAt' | 'taskId'>[],
  taskId?: string
) {
  return messages
    .filter((message) => message.eventType && taskLifecycleEvents.has(message.eventType))
    .filter((message) => !taskId || !message.taskId || message.taskId === taskId)
    .map((message) => Date.parse(message.createdAt ?? ''))
    .filter((timestamp) => Number.isFinite(timestamp))
}

export function taskBlocksInput(conversation: { state: ConversationState; latestTask?: AgentTask | null }) {
  const status = conversation.latestTask?.status
  return !!status && blockingTaskStatuses.includes(status)
}

/**
 * A terminal task state must come from the explicit task_completed event.
 * Narrative agent text and artifact messages can arrive before the workflow
 * has actually finished, so their content is not enough to close the run.
 */
export function isTaskCompletedMessage(message: Pick<ChatMessage, 'eventType'>) {
  return message.eventType === 'task_completed'
}

export function taskDurationLabel(state: string | TaskStatus) {
  // Phase 2.9A.35: waiting_user_confirm 显示"等待确认",而非"处理中"
  if (state === 'waiting' || state === 'waiting_user' || state === 'waiting_user_confirm') return '等待确认'
  if (state === 'completed') return '已处理'
  if (state === 'failed') return '执行失败'
  if (state === 'cancelled') return '已取消'
  return '已处理'
}

/**
 * Phase 2.9B.5 — 任务级 runState 权威推导纯函数。
 *
 * 任务失败只能由以下权威来源决定:
 *   - Task Detail.status / Run.status(后端已终态);
 *   - task_failed / run_failed 事件;
 * 局部 Tool 失败(tool_failed)、叙事失败(tool_narrative_failed /
 * task_summary_narrative_failed)、preparation_fallback / repair_fallback
 * 一律不得提升为任务级失败。
 *
 * 权威优先级:
 *   1. Task/Run 明确 completed  → completed
 *   2. Task/Run 明确 failed     → failed
 *   3. Task/Run 明确 cancelled  → cancelled
 *   4. waiting_user_confirm     → waiting
 *   5. 其它(queued/running/resuming/…) → running
 */
export type DerivedTaskRunState = 'completed' | 'failed' | 'cancelled' | 'waiting' | 'running'

export interface TaskRunStateInput {
  /** 权威 Task 状态(来自 reducer block.status / Task Detail)。 */
  taskStatus?: string | null
  /** 是否已看到 task_failed / run_failed 事件消息。 */
  explicitTaskFailed?: boolean
  /** 是否已看到 task_cancelled 事件消息。 */
  explicitCancelled?: boolean
  /** 是否已看到 task_completed 事件消息。 */
  explicitCompleted?: boolean
  /** 是否等待用户确认(章节确认 / format_loss_review)。 */
  waitingConfirmation?: boolean
}

const RUN_STATE_TERMINAL: TaskStatus[] = ['completed', 'failed', 'cancelled']

export function deriveTaskRunState(input: TaskRunStateInput): DerivedTaskRunState {
  const status = input.taskStatus
  // 1~3. 权威 Task 终态优先 — reducer / Task Detail 已经决定终态,
  // 局部失败不得覆盖。
  if (status === 'completed' || input.explicitCompleted) return 'completed'
  if (status === 'failed' || input.explicitTaskFailed) return 'failed'
  if (status === 'cancelled' || input.explicitCancelled) return 'cancelled'
  // 4. 等待用户确认。
  if (status === 'waiting_user' || status === 'waiting_user_confirm' || input.waitingConfirmation) return 'waiting'
  // 5. 其余(created/planning/running/generating/reviewing/exporting/
  // format_loss_review / unknown)一律按进行中。
  return 'running'
}

/** 是否为任务级终态(completed / failed / cancelled)。 */
export function isTerminalTaskStatus(status: string | undefined | null): boolean {
  return !!status && RUN_STATE_TERMINAL.includes(status as TaskStatus)
}

export function taskBusyText(conversation: { state: ConversationState; latestTask?: AgentTask | null }) {
  if (
    conversation.state === 'waiting_user' ||
    conversation.state === 'waiting_user_confirm' ||
    conversation.latestTask?.status === 'waiting_user' ||
    conversation.latestTask?.status === 'waiting_user_confirm'
  ) {
    return '请先完成当前任务的确认'
  }
  return taskBlocksInput(conversation) ? '当前任务执行中，请等待完成' : ''
}
