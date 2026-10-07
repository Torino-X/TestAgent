import type { ChatMessage, Conversation, ConversationState, FileAttachment, FileType, FileUploadStatus, KnowledgeMode, MessageType, SendMessageResponse } from '@/types'
import {
  apiGet,
  apiPost,
  apiPut,
  createApiRequestErrorFromResponse,
  getAuthToken,
  resolveApiUrl
} from './request'

interface ApiMessage {
  message_id: string
  role: 'user' | 'agent'
  message_type: MessageType
  content?: string
  payload?: unknown
  created_at?: string
  timestamp?: string
  attached_files?: ApiFile[]
  files?: ApiFile[]
  attachments?: ApiFile[]
  // Phase 2.9A.26+: stable timeline anchor from backend
  conversation_sequence?: number
  reply_to_message_id?: string | null
  reply_to_message_internal_id?: number | null
}

interface ApiFile {
  id?: string
  file_id?: string
  file_name?: string
  original_name?: string
  file_size?: number
  file_ext?: string
  file_type?: FileType
  status?: string
  upload_status?: string
  description?: string
}

interface ApiAgentReply {
  message_id?: string
  role?: 'user' | 'agent'
  message_type: string
  content: string
  created_at?: string
  timestamp?: string
}

interface ApiSendMessageResponse {
  message: ApiMessage
  agent_task: SendMessageResponse['agent_task']
  agent_reply?: ApiAgentReply | null
  conversation?: ApiConversationSummary | null
}

interface ApiConversationSummary {
  id?: string
  conversation_id?: string
  title: string
  state?: string
  status?: string
  updated_at?: string
  last_message_at?: string
}

interface ApiMessageList {
  items: ApiMessage[]
}

export interface MessageStreamHandlers {
  onMessageCreated?: (payload: { message: ChatMessage; conversation?: Conversation }) => void
  onAgentReplyCreated?: (payload: { message: ChatMessage }) => void
  onAgentTextDelta?: (payload: { messageId: string; delta: string }) => void
  onAgentTextDone?: (payload: SendMessageResponse) => void
  onAgentTaskCreated?: (payload: SendMessageResponse) => void
  onError?: (message: string) => void
}

function displayTime(value?: string): string {
  if (!value) return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function messageCreatedAt(message: ApiMessage | ApiAgentReply): string | undefined {
  return message.created_at ?? message.timestamp
}

function messageRoleOrder(message: ApiMessage): number {
  return message.role === 'user' ? 0 : 1
}

function sortApiMessagesByCreatedAt(messages: ApiMessage[]): ApiMessage[] {
  return messages
    .map((message, index) => ({ message, index, time: Date.parse(messageCreatedAt(message) ?? '') }))
    .sort((left, right) => {
      const leftHasTime = Number.isFinite(left.time)
      const rightHasTime = Number.isFinite(right.time)
      if (leftHasTime && rightHasTime && left.time !== right.time) return left.time - right.time
      if (leftHasTime !== rightHasTime) return leftHasTime ? -1 : 1
      const roleOrder = messageRoleOrder(left.message) - messageRoleOrder(right.message)
      if (roleOrder !== 0) return roleOrder
      return left.index - right.index
    })
    .map((item) => item.message)
}

function isBlankHistoricalPlaceholder(message: ApiMessage): boolean {
  return message.role === 'agent' && message.message_type === 'agent_text' && !(message.content ?? '').trim()
}

function formatFileSize(bytes?: number): string {
  if (!bytes) return ''
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

function mapFile(file: ApiFile): FileAttachment {
  const name = file.file_name ?? file.original_name ?? file.file_id ?? file.id ?? '文件'
  return {
    id: file.file_id ?? file.id ?? '',
    name,
    size: formatFileSize(file.file_size),
    extension: file.file_ext ?? (name.includes('.') ? `.${name.split('.').pop() ?? ''}` : ''),
    type: file.file_type ?? 'unknown',
    status: ((file.upload_status ?? file.status) as FileUploadStatus | undefined) ?? 'uploaded',
    description: file.description
  }
}

function mapAttachedFiles(message: ApiMessage): FileAttachment[] | undefined {
  const files = message.attached_files ?? message.files ?? message.attachments
  return files?.map(mapFile)
}

function mapMessage(message: ApiMessage): ChatMessage {
  const createdAt = messageCreatedAt(message)
  const kbFields = extractKbFields(message.payload)
  return {
    id: message.message_id,
    type: message.message_type,
    role: normalizeMessageRole(message.role, message.message_type),
    createdAt,
    // Phase 2.9A.26+: server-side conversation_sequence + reply
    // association.  The new index ``ix_messages_conversation_sequence``
    // on the backend guarantees stable ordering across same-second
    // user/agent pairs.
    conversationSequence: message.conversation_sequence,
    replyToMessageId: message.reply_to_message_id ?? undefined,
    text: message.content,
    files: mapAttachedFiles(message),
    timestamp: displayTime(createdAt),
    ...kbFields,
    // Phase 2.9A.26+: server-side feedback already resolved in list_messages
    feedback: ('my_feedback' in message ? (message as { my_feedback?: string }).my_feedback : undefined) as ChatMessage['feedback'],
  }
}

function normalizeMessageRole(role: ApiMessage['role'], type: MessageType): ChatMessage['role'] {
  if (type === 'user_text' || type === 'user_file') return 'user'
  return role === 'user' ? 'agent' : role
}

function extractKbFields(payload: unknown): {
  kbDirectAnswer?: ChatMessage['kbDirectAnswer']
  documentCitations?: ChatMessage['documentCitations']
  intent?: string
  route?: string
} {
  if (!payload || typeof payload !== 'object') return {}
  const obj = payload as Record<string, unknown>
  const rawKb = obj.kb_direct_answer
  const kbDirectAnswer = rawKb && typeof rawKb === 'object'
    ? (rawKb as ChatMessage['kbDirectAnswer'])
    : undefined
  const rawCitations = obj.document_citations
  const documentCitations = Array.isArray(rawCitations)
    ? rawCitations as ChatMessage['documentCitations']
    : undefined
  return {
    kbDirectAnswer,
    documentCitations,
    intent: typeof obj.intent === 'string' ? obj.intent : undefined,
    route: typeof obj.route === 'string' ? obj.route : undefined
  }
}

function mapConversationState(status?: string): ConversationState {
  if (!status || status === 'active' || status === 'created' || status === 'new') return 'uploaded'
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

function mapConversationSummary(item: ApiConversationSummary): Conversation {
  return {
    id: item.id ?? item.conversation_id ?? '',
    title: item.title,
    subtitle: '',
    state: mapConversationState(item.state ?? item.status),
    updatedAt: item.updated_at ?? item.last_message_at ?? '',
    latestTask: null,
    files: [],
    draftFiles: [],
    messages: []
  }
}

function mapAgentReply(reply?: ApiAgentReply | null): ChatMessage | undefined {
  if (!reply) return undefined
  const createdAt = messageCreatedAt(reply)
  const kbFields = extractKbFields((reply as { payload?: unknown }).payload)
  return {
    id: `msg_agent_${Date.now()}`,
    type: reply.message_type as MessageType,
    role: 'agent',
    createdAt,
    text: reply.content,
    timestamp: displayTime(createdAt),
    ...kbFields
  }
}

function mapSendMessagePayload(payload: ApiSendMessageResponse): SendMessageResponse {
  return {
    message: mapMessage(payload.message),
    agent_task: payload.agent_task,
    agent_reply: payload.agent_reply?.message_id
      ? mapMessage(payload.agent_reply as unknown as ApiMessage)
      : mapAgentReply(payload.agent_reply),
    conversation: payload.conversation ? mapConversationSummary(payload.conversation) : undefined
  }
}

function normalizeMessageList(messages: ApiMessage[] | ApiMessageList): ApiMessage[] {
  return Array.isArray(messages) ? messages : messages.items
}

export async function fetchMessages(conversationId: string): Promise<ChatMessage[]> {
  const messages = await apiGet<ApiMessage[] | ApiMessageList | { messages: ApiMessage[]; total: number }>(`/api/conversations/${conversationId}/messages`)
  const list = 'messages' in messages ? messages.messages : normalizeMessageList(messages)
  return sortApiMessagesByCreatedAt(list).filter((message) => !isBlankHistoricalPlaceholder(message)).map(mapMessage)
}

export async function sendMessage(
  conversationId: string,
  text: string,
  fileIds?: string[],
  knowledgeModeSnapshot?: KnowledgeMode
): Promise<SendMessageResponse> {
  const response = await apiPost<ApiSendMessageResponse>(
    `/api/conversations/${conversationId}/messages`,
    {
      content: text,
      attached_file_ids: fileIds,
      knowledge_mode_snapshot: knowledgeModeSnapshot
    }
  )

  const agentReply: ChatMessage | undefined = response.agent_reply
    ? mapAgentReply(response.agent_reply)
    : undefined

  return {
    message: mapMessage(response.message),
    agent_task: response.agent_task,
    agent_reply: agentReply,
    conversation: response.conversation ? mapConversationSummary(response.conversation) : undefined
  }
}

function parseSseBlock(block: string): { event: string; data: unknown } | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const rawLine of block.split(/\r?\n/)) {
    const line = rawLine.trimEnd()
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('event:')) {
      event = line.slice('event:'.length).trim()
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice('data:'.length).trimStart())
    }
  }
  if (!dataLines.length) return null
  return {
    event,
    data: JSON.parse(dataLines.join('\n'))
  }
}

function dispatchStreamEvent(event: string, data: unknown, handlers: MessageStreamHandlers): boolean {
  const payload = data as Record<string, unknown>
  if (event === 'message_created' && payload.message) {
    handlers.onMessageCreated?.({
      message: mapMessage(payload.message as ApiMessage),
      conversation: payload.conversation
        ? mapConversationSummary(payload.conversation as ApiConversationSummary)
        : undefined
    })
    return false
  }
  // Compatibility guard for legacy/misrouted backend branches that emitted
  // an already-persisted SendMessageResponse under agent_reply_created and
  // then kept the HTTP stream open.  The embedded agent_reply makes this a
  // business-terminal event even when its SSE label is wrong.
  if (event === 'agent_reply_created' && payload.agent_reply) {
    handlers.onAgentTextDone?.(mapSendMessagePayload(payload as unknown as ApiSendMessageResponse))
    return true
  }
  if (event === 'agent_reply_created' && payload.message) {
    handlers.onAgentReplyCreated?.({
      message: mapMessage(payload.message as ApiMessage)
    })
    return false
  }
  if (event === 'agent_text_delta') {
    handlers.onAgentTextDelta?.({
      messageId: String(payload.message_id ?? ''),
      delta: String(payload.delta ?? '')
    })
    return false
  }
  if (event === 'agent_text_done') {
    handlers.onAgentTextDone?.(mapSendMessagePayload(payload as unknown as ApiSendMessageResponse))
    return true
  }
  if (event === 'agent_task_created') {
    handlers.onAgentTaskCreated?.(mapSendMessagePayload(payload as unknown as ApiSendMessageResponse))
    return true
  }
  if (event === 'error') {
    handlers.onError?.(String(payload.message ?? '消息发送失败'))
    return true
  }
  return false
}

// ── Phase 2.9A.26+: message feedback ──────────────────────────────────

export interface FeedbackResponseData {
  message_id: string
  feedback: 'like' | 'dislike' | null
}

/**
 * PUT /api/messages/{messagePublicId}/feedback
 *
 * Sets, switches, or cancels the current user's feedback on an assistant
 * message.  Returns the server-confirmed value so the caller can reconcile
 * the optimistic state.
 */
export async function setMessageFeedback(
  messagePublicId: string,
  feedback: 'like' | 'dislike' | null
): Promise<FeedbackResponseData> {
  return apiPut<FeedbackResponseData>(
    `/api/messages/${messagePublicId}/feedback`,
    { feedback }
  )
}

// ── Phase 2.9A.26+: message regeneration ─────────────────────────────

export interface RegenerateResponseData {
  message_id: string
  generation_id?: string
}

export interface RegenerateStreamHandlers {
  onAgentTextDelta?: (payload: { messageId: string; delta: string }) => void
  onAgentTextDone?: (payload: { messageId: string; text: string }) => void
  onError?: (message: string) => void
}

/**
 * POST /api/messages/{messagePublicId}/regenerate
 *
 * Re-generates the assistant reply for the given message.  Returns an
 * SSE stream that delivers agent_text_delta / agent_text_done events
 * targeting the same messageId, so the front-end can update in-place.
 */
export async function regenerateMessageStream(
  messagePublicId: string,
  handlers: RegenerateStreamHandlers,
  signal?: AbortSignal
): Promise<void> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json'
  }
  const token = getAuthToken()
  if (token) headers.Authorization = `Bearer ${token}`

  const response = await fetch(
    resolveApiUrl(`/api/messages/${messagePublicId}/regenerate`),
    {
      method: 'POST',
      headers,
      body: JSON.stringify({}),
      credentials: 'include',
      signal
    }
  )

  if (!response.ok) {
    throw await createApiRequestErrorFromResponse(response, {
      method: 'POST',
      url: `/api/messages/${messagePublicId}/regenerate`
    })
  }
  if (!response.body) {
    throw new Error('后端没有返回可读取的流式响应')
  }

  const reader: ReadableStreamDefaultReader<Uint8Array> = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() ?? ''
    for (const block of blocks) {
      const parsed = parseSseBlock(block)
      if (!parsed) continue
      const payload = parsed.data as Record<string, unknown>
      if (parsed.event === 'agent_text_delta') {
        handlers.onAgentTextDelta?.({
          messageId: String(payload.message_id ?? ''),
          delta: String(payload.delta ?? '')
        })
      } else if (parsed.event === 'agent_text_done') {
        const agentReply = payload.agent_reply as Record<string, unknown> | undefined
        handlers.onAgentTextDone?.({
          messageId: String(agentReply?.message_id ?? payload.message_id ?? ''),
          text: String(agentReply?.content ?? '')
        })
        reader.releaseLock()
        return
      } else if (parsed.event === 'error') {
        handlers.onError?.(String(payload.message ?? '重新生成失败'))
        reader.releaseLock()
        return
      }
    }
  }
  // Flush remaining buffer
  buffer += decoder.decode()
  if (buffer.trim()) {
    const parsed = parseSseBlock(buffer)
    if (parsed) {
      const payload = parsed.data as Record<string, unknown>
      if (parsed.event === 'agent_text_done') {
        const agentReply = payload.agent_reply as Record<string, unknown> | undefined
        handlers.onAgentTextDone?.({
          messageId: String(agentReply?.message_id ?? payload.message_id ?? ''),
          text: String(agentReply?.content ?? '')
        })
      } else if (parsed.event === 'error') {
        handlers.onError?.(String(payload.message ?? '重新生成失败'))
      }
    }
  }
}

export async function sendMessageStream(
  conversationId: string,
  text: string,
  fileIds: string[] | undefined,
  handlers: MessageStreamHandlers,
  signal?: AbortSignal,
  knowledgeModeSnapshot?: KnowledgeMode
): Promise<void> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json'
  }
  const token = getAuthToken()
  if (token) headers.Authorization = `Bearer ${token}`
  const response = await fetch(resolveApiUrl(`/api/conversations/${conversationId}/messages/stream`), {
    method: 'POST',
    headers,
    body: JSON.stringify({
      content: text,
      attached_file_ids: fileIds,
      knowledge_mode_snapshot: knowledgeModeSnapshot
    }),
    credentials: 'include',
    signal
  })
  if (!response.ok) {
    throw await createApiRequestErrorFromResponse(response, {
      method: 'POST',
      url: `/api/conversations/${conversationId}/messages/stream`
    })
  }
  if (!response.body) {
    throw new Error('后端没有返回可读取的流式响应')
  }

  const reader: ReadableStreamDefaultReader<Uint8Array> = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() ?? ''
    for (const block of blocks) {
      const parsed = parseSseBlock(block)
      if (!parsed) continue
      // The semantic terminal event is authoritative.  Do not keep the
      // composer locked merely because an intermediary keeps the HTTP stream
      // open and delays EOF after it has delivered agent_text_done/error.
      if (dispatchStreamEvent(parsed.event, parsed.data, handlers)) {
        reader.releaseLock()
        return
      }
    }
  }
  buffer += decoder.decode()
  if (buffer.trim()) {
    const parsed = parseSseBlock(buffer)
    if (parsed) dispatchStreamEvent(parsed.event, parsed.data, handlers)
  }
}
