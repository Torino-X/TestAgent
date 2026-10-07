import type {
  AgentTask,
  ContextCompactResponse,
  ContextUsageResponse,
  ContextUsagePreviewRequest,
  Conversation,
  ConversationState,
  KnowledgeMode
} from '@/types'
import { apiDelete, apiGet, apiPatch, apiPost, type RequestOptions } from './request'

interface ApiConversationTask {
  task_id: string
  task_type?: 'test_plan_generation'
  status: AgentTask['status']
  events_url?: string
  started_at?: string | null
  completed_at?: string | null
  duration_ms?: number | null
  runtime_status?: string | null
  run?: {
    run_id?: string
    runId?: string
    status?: string
    started_at?: string | null
    startedAt?: string | null
    finished_at?: string | null
    finishedAt?: string | null
    duration_ms?: number | null
    durationMs?: number | null
  } | null
  /** Phase 2.9A.30: trigger message public_id (string or null) */
  trigger_message_id?: string | null
}

interface ApiConversation {
  id?: string
  conversation_id?: string
  title: string
  summary?: string
  status?: string
  updated_at?: string
  last_message_at?: string
  message_count?: number
  file_count?: number
  project_id?: string | null
  project_name?: string | null
  latest_task?: ApiConversationTask | null
  tasks?: ApiConversationTask[]
}

interface ApiConversationDetail {
  conversation: ApiConversation
  latest_task?: ApiConversationTask | null
  tasks?: ApiConversationTask[]
}

interface ApiPage<T> {
  items: T[]
  total: number
}

export interface ConversationContextSettings {
  memory_mode: string
  knowledge_mode: KnowledgeMode
  knowledge_mode_persistence?: 'server' | 'client_snapshot'
  knowledge_mode_migration_required?: boolean
  context_workspace_key?: string | null
  context_engine_version?: string | null
  policy_version?: string | null
}

function mapConversationTask(task: ApiConversationTask): AgentTask {
  const run = task.run
  return {
    task_id: task.task_id,
    task_type: task.task_type ?? 'test_plan_generation',
    status: task.status,
    events_url: task.events_url ?? `/api/agent/tasks/${task.task_id}/events`,
    startedAt: task.started_at,
    completedAt: task.completed_at,
    durationMs: task.duration_ms,
    runtimeStatus: task.runtime_status ?? null,
    run: run
      ? {
          runId: run.run_id ?? run.runId ?? '',
          status: run.status ?? '',
          startedAt: run.started_at ?? run.startedAt ?? null,
          finishedAt: run.finished_at ?? run.finishedAt ?? null,
          durationMs: run.duration_ms ?? run.durationMs ?? null
        }
      : null,
    triggerMessageId: task.trigger_message_id ?? null
  }
}

function mapConversationState(status?: string, messageCount?: number): ConversationState {
  if (status === 'active' && messageCount === 0) return 'empty'
  if (!status || status === 'created' || status === 'new') return 'empty'
  const knownStates: ConversationState[] = [
    'empty',
    'uploaded',
    'planning',
    'waiting_user_confirm',
    'generating',
    'completed',
    'failed'
  ]
  return knownStates.includes(status as ConversationState) ? (status as ConversationState) : 'uploaded'
}

export function mapConversation(item: ApiConversation): Conversation {
  const latestTask = item.latest_task ? mapConversationTask(item.latest_task) : null
  const messageCount = typeof item.message_count === 'number' ? item.message_count : undefined
  const fileCount = typeof item.file_count === 'number' ? item.file_count : undefined
  const tasks = Array.isArray(item.tasks)
    ? item.tasks.map(mapConversationTask)
    : latestTask
      ? [latestTask]
      : []
  return {
    id: item.id ?? item.conversation_id ?? '',
    title: item.title,
    projectId: item.project_id ?? null,
    projectName: item.project_name ?? null,
    subtitle: item.summary ?? (item.status === 'completed' ? '已完成' : '进行中'),
    state: mapConversationState(item.status, messageCount),
    updatedAt: item.updated_at ?? item.last_message_at ?? '刚刚',
    messageCount,
    fileCount,
    latestTask,
    tasks,
    files: [],
    draftFiles: [],
    messages: []
  }
}

function normalizeDetail(detail: ApiConversation | ApiConversationDetail): ApiConversation {
  if ('conversation' in detail) {
    return {
      ...detail.conversation,
      latest_task: detail.latest_task ?? detail.conversation.latest_task ?? null,
      tasks: detail.tasks ?? detail.conversation.tasks
    }
  }
  return detail
}

export async function fetchConversations(): Promise<Conversation[]> {
  const page = await apiGet<ApiPage<ApiConversation> | { conversations: ApiConversation[]; total: number }>('/api/conversations')
  const items = 'conversations' in page ? page.conversations : page.items
  return items.map(mapConversation)
}

export async function fetchConversationDetail(id: string): Promise<Conversation> {
  const detail = await apiGet<ApiConversation | ApiConversationDetail>(`/api/conversations/${id}`)
  return {
    ...mapConversation(normalizeDetail(detail)),
    files: [],
    draftFiles: [],
    messages: []
  }
}

export async function createConversation(title = '新会话'): Promise<Conversation> {
  const created = await apiPost<ApiConversation>(
    '/api/conversations',
    { title }
  )
  return mapConversation(created)
}

export async function updateConversationTitle(id: string, title: string): Promise<Conversation> {
  const updated = await apiPatch<ApiConversation>(
    `/api/conversations/${id}`,
    { title }
  )
  return mapConversation(updated)
}

export async function deleteConversation(id: string): Promise<void> {
  await apiDelete(`/api/conversations/${id}`)
}

export async function fetchContextUsage(
  conversationId: string,
  opts?: RequestOptions
): Promise<ContextUsageResponse> {
  return apiGet<ContextUsageResponse>(`/api/conversations/${conversationId}/context-usage`, opts)
}

export async function previewContextUsage(
  conversationId: string,
  payload: ContextUsagePreviewRequest,
  opts?: RequestOptions
): Promise<ContextUsageResponse> {
  return apiPost<ContextUsageResponse>(
    `/api/conversations/${conversationId}/context-usage/preview`,
    payload,
    opts
  )
}

export async function fetchConversationContextSettings(
  conversationId: string,
  opts?: RequestOptions
): Promise<ConversationContextSettings> {
  return apiGet<ConversationContextSettings>(`/api/conversations/${conversationId}/context-settings`, opts)
}

export async function updateConversationKnowledgeMode(
  conversationId: string,
  knowledgeMode: KnowledgeMode,
  opts?: RequestOptions
): Promise<Partial<ConversationContextSettings>> {
  return apiPatch<Partial<ConversationContextSettings>>(
    `/api/conversations/${conversationId}/context-settings`,
    { knowledge_mode: knowledgeMode },
    opts
  )
}

export async function compactConversationContext(
  conversationId: string,
  opts?: RequestOptions
): Promise<ContextCompactResponse> {
  return apiPost<ContextCompactResponse>(`/api/conversations/${conversationId}/context/compact`, {}, opts)
}
