export type MessageType =
  | 'user_text'
  | 'user_file'
  | 'agent_text'
  | 'agent_plan'
  | 'tool_call'
  | 'tool_retry'
  | 'requirement_summary'
  | 'template_summary'
  | 'knowledge_summary'
  | 'section_confirm'
  | 'preparation_clarification'
  | 'generating_status'
  | 'review_result'
  | 'artifact_download'
  | 'error'

export * from './library'

export type TaskStatus =
  | 'created'
  | 'planning'
  | 'running'
  | 'waiting_user'
  | 'waiting_user_confirm'
  | 'generating'
  | 'reviewing'
  | 'exporting'
  | 'format_loss_review'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type FileType =
  | 'requirement_doc'
  | 'test_plan_template'
  | 'supplemental_doc'
  | 'unknown'

export type FileUploadStatus =
  | 'uploading'
  | 'uploaded'
  | 'failed'
  | 'identifying'
  | 'need_confirm'
  | 'confirmed'

export type KnowledgeMode = 'AUTO' | 'MAAS_STRICT'

export type SectionAction =
  | 'ai_generate'
  | 'keep_template'
  | 'manual_fill'
  | 'skip'

/**
 * Where a suggestion's recommended action came from.
 *
 * - ``'template'`` — the default from the parsed template structure
 *   (mode = ai / keep / manual).
 * - ``'user_prompt'`` — overridden by the user's natural-language
 *   prompt (F022 UserConstraintExtractor).  The UI surfaces these
 *   with a "📌 用户指定" badge so the user can see what was changed
 *   on their behalf.
 */
export type SectionConstraintSource = 'template' | 'user_prompt'

export type ArtifactType = 'test_plan_word'

export type ConversationState =
  | 'empty'
  | 'uploaded'
  | 'planning'
  | 'waiting_user'
  | 'waiting_user_confirm'
  | 'generating'
  | 'completed'
  | 'failed'

export type PlanStepStatus = 'done' | 'running' | 'pending' | 'failed' | 'superseded'
export type ToolCallStatus = 'running' | 'success' | 'failed' | 'warning'

/**
 * Phase 2.9B.4 — LLM-first Tool 叙事流式状态。
 *
 * 由 tool_narrative_started / delta / update / failed / fallback 事件归约。
 * 通过 sourceToolCallId 锚定到对应 Tool 卡片下方,展示在现有
 * PublicExecutionUpdate 区域(逐步增长的 partial publicUpdate)。
 */
export interface ToolNarrativeState {
  narrativeId: string
  generationId: string
  generationNo: number
  sourceEventId?: string
  sourceToolCallId: string
  toolName: string
  attempt: number
  status: 'streaming' | 'completed' | 'failed' | 'fallback'
  source: 'llm' | 'deterministic'
  publicUpdate: PublicExecutionUpdate
  lastChunkIndex: number
  canonicalOrder?: number
  createdAt?: string
  completedAt?: string
}

/**
 * Phase 2.9B.6 — 最终任务总结叙事(LLM-first)状态槽。
 *
 * 由 task_summary_narrative_started / delta / update / failed / fallback 事件
 * 归约,是 TaskRunBlock 上的唯一权威槽位。AgentRunCard 据此选择最终总结:
 *   validLlmTaskSummary ?? deterministicTaskSummary。
 */
export interface TaskSummaryNarrativeState {
  status: 'idle' | 'streaming' | 'completed' | 'failed' | 'fallback'
  narrativeId?: string
  generationId?: string
  generationNo?: number
  narrativeSource?: 'llm' | 'deterministic'
  fallbackUsed?: boolean
  fallbackReason?: string
  publicUpdate?: PublicExecutionUpdate
  lastCanonicalOrder?: number
  createdAt?: string
  completedAt?: string
}

/**
 * Phase 2.9B.2 — 动态叙事节点(Preparation / Repair / Incremental 决策叙事)。
 *
 * 由 agent_decision_update / agent_observation_update 事件生成,是 TaskRunBlock
 * 内的独立叙事节点,渲染为时间线中真实事件位置的一个叙事行,复用现有
 * PublicExecutionUpdate.vue 展示 public_update。不是 Tool 卡片、不是普通
 * conversation message、不是业务消息表实体(不落 DB 消息表)。
 */
export interface DynamicNarrative {
  /** 稳定身份 — dynamic-narrative:{task_id}:{event_id} */
  id: string
  /** 来源事件 id(去重主键)。 */
  sourceEventId: string
  /** 来源事件类型: agent_decision_update / agent_observation_update。 */
  eventType: AgentEventType
  taskId: string
  graphRunId?: string | null
  /** 归约顺序 — canonical_order 优先,sequence_no 次之。 */
  canonicalOrder?: number
  sequenceNo?: number | null
  createdAt?: string
  agentName: string
  stepId?: string
  stepTitle?: string
  narrativeSource?: 'llm' | 'deterministic'
  decisionId: string
  stepIndex: number
  action: string
  toolName?: string | null
  route?: string | null
  /** 规范化的用户可见叙事(经 extractPublicExecutionUpdate 解析)。 */
  publicUpdate: PublicExecutionUpdate
  /** 业务辅助去重键(不得跨事件类型错误合并)。 */
  dedupeKey?: string
  /** 来源模式: live / hydrate。 */
  sourceMode: 'live' | 'hydrate'
}

// ── SSE / Agent event types ──────────────────────────────────────────

export type AgentEventType =
  | 'task_created'
  | 'plan_created'
  | 'plan_updated'
  | 'plan_step_started'
  | 'plan_step_completed'
  | 'plan_step_failed'
  | 'tool_started'
  | 'tool_progress'
  | 'tool_finished'
  | 'tool_failed'
  | 'retrying'
  | 'requirement_summary'
  | 'template_summary'
  | 'knowledge_summary'
  | 'need_user_confirm'
  | 'task_waiting'
  | 'task_resumed'
  | 'generating_started'
  | 'generating_progress'
  | 'review_completed'
  | 'artifact_created'
  | 'task_failed'
  | 'task_completed'
  | 'task_cancelled'
  | 'agent_decision_update'
  | 'agent_observation_update'
  | 'tool_narrative_started'
  | 'tool_narrative_delta'
  | 'tool_narrative_update'
  | 'tool_narrative_failed'
  | 'tool_narrative_fallback'
  | 'task_summary_narrative_started'
  | 'task_summary_narrative_delta'
  | 'task_summary_narrative_update'
  | 'task_summary_narrative_failed'
  | 'task_summary_narrative_fallback'
  | 'format_loss_confirm_requested'
  | 'format_loss_decision_recorded'
  | 'format_loss_resuming'
  // Phase 2.5 / 增量任务终态事件 — incremental subgraph emit,
  // 修复: useTaskEvents 不识别导致 SSE 不关闭 + 前端「正在思考...」卡死
  | 'incremental_started'
  | 'incremental_decision_made'
  | 'incremental_tool_finished'
  | 'incremental_tool_blocked'
  | 'incremental_completed'
  | 'incremental_failed'
  | 'incremental_fallback'

/**
 * F025-ext — payload of the ``format_loss_confirm_requested`` SSE
 * event.  Surfaces one or more user-visible losses (missing
 * bookmarks, fields, hyperlinks, header/footer) so the front-end
 * can render an accept / retry banner.
 */
export interface FormatLossChoice {
  id: 'accept' | 'retry'
  label: string
  description?: string
}

export interface FormatLossConfirmation {
  taskId: string
  losses: Array<{
    element: string
    expected: unknown
    actual: unknown
    message: string
  }>
  lossCount: number
  summary: string
  choices: FormatLossChoice[]
  timeoutAt: string
}

export interface AgentEvent {
  event_id: string
  task_id: string
  event_type: AgentEventType
  data: unknown
  timestamp: string
}

// ── API types ────────────────────────────────────────────────────────

export interface AgentRun {
  runId: string
  status: string
  startedAt?: string | null
  finishedAt?: string | null
  durationMs?: number | null
}

export interface AgentTask {
  task_id: string
  task_type: 'test_plan_generation' | 'dynamic_agent'
  status: TaskStatus
  events_url: string
  activeRunId?: string | null
  runtimeStatus?: string | null
  startedAt?: string | null
  completedAt?: string | null
  durationMs?: number | null
  run?: AgentRun | null
  /** Phase 2.9A.27: task detail 后端已返回 review_result,前端 fallback 用。 */
  review_result?: unknown
  /** Phase 2.9A.30: trigger user message 的 public_id (字符串)。 */
  triggerMessageId?: string | null
}

export interface SendMessageResponse {
  message: ChatMessage
  agent_task: AgentTask | null
  agent_reply?: ChatMessage
  conversation?: Conversation
}

export type ContextWindowSource = 'model_config' | 'snapshot' | 'unknown'
export type ContextCountMode = 'exact' | 'heuristic' | 'unknown'

export interface ContextUsageModel {
  name: string | null
  context_window_tokens: number | null
  window_source: ContextWindowSource
}

export interface ContextUsageSummary {
  used_tokens: number | null
  available_tokens: number | null
  percent: number | null
  count_mode: ContextCountMode
  estimated: boolean | null
  over_limit: boolean
  /** Final prompt envelope / formatting overhead not represented by selected sections. */
  unattributed_tokens?: number
}

export interface ContextUsageBreakdown {
  conversation_history: number
  project_documents: number
  task_context: number
  user_memory: number
  system_instructions: number
}

export interface ContextCompactionState {
  available: boolean
  recommended: boolean
  in_progress: boolean
}

export interface ContextUsageResponse {
  conversation_public_id: string
  model: ContextUsageModel
  usage: ContextUsageSummary
  breakdown: ContextUsageBreakdown
  compaction: ContextCompactionState
  as_of: string | null
  /** True only when the response is backed by a completed active CE snapshot. */
  available: boolean
  snapshot_public_id: string | null
  /** Server-controlled; diagnostic receipts are omitted when false. */
  debug_details_enabled?: boolean
  /** Safe provenance receipt for the completed Context Engine snapshot. */
  evidence_receipt?: ContextEvidenceReceipt | null
  /** Recent completed CE calls from the same agent task, newest first. */
  recent_evidence_receipts?: ContextEvidenceReceipt[] | null
  /** The card normally uses the persistent conversation working-set ledger. */
  source?: 'conversation_context_ledger' | 'next_request_preview' | 'next_request_preview_unavailable' | 'last_completed_snapshot'
  /** Safe, content-free preflight decision for the preview. */
  preflight?: Record<string, unknown> | null
}

export interface ContextUsagePreviewRequest {
  content: string
  attached_file_ids?: string[]
}

export interface ContextEvidenceSource {
  kind: string
  source_type: string
  reference: string
}

export interface ContextEvidenceReceipt {
  snapshot_public_id: string
  call_site: string
  profile_key: string
  profile_version: string
  included_source_count: number
  dropped_source_count: number
  included_sources: ContextEvidenceSource[]
}

export interface ContextCompactResponse {
  run_public_id: string | null
  summary_public_id: string | null
  before_used_tokens: number | null
  after_used_tokens: number | null
  saved_tokens: number | null
  summary_updated: boolean
  as_of: string
  in_progress: boolean
  strategy?: 'light' | 'deep' | null
  preserved_complete_turns?: number | null
}

export interface LoginRequest {
  account: string
  password: string
}

export interface LoginResponse {
  token: string
  user: UserProfile
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
}

export interface ApiError {
  code: string
  message: string
}

// ── Domain interfaces ────────────────────────────────────────────────

export interface SelectOption<T extends string> {
  label: string
  value: T
}

export interface UserProfile {
  id: string
  name: string
  username?: string
  email?: string
  role: string
  status?: string
  avatarUrl?: string | null
}

export interface FileAttachment {
  id: string
  name: string
  size: string
  extension: string
  type: FileType
  status: FileUploadStatus
  description?: string
  uploadProgress?: number
  localOnly?: boolean
}

export interface TaskPlanStep {
  id: string
  title: string
  status: PlanStepStatus
  detail?: string
}

export interface ToolCall {
  id: string
  name: string
  status: ToolCallStatus
  input: string
  output: string
  duration: string
  startedAt?: string
  finishedAt?: string
  displayName?: string
  businessAction?: string
  businessSubjectType?: string
  businessSubjectName?: string
  filePublicId?: string
  fileName?: string
  progressMessage?: string
  completionMessage?: string
  errorSummary?: string
  publicUpdate?: PublicExecutionUpdate
  /** Phase 2.9B.6: 工具执行 attempt,供叙事锚定(防 attempt 2 绑定 attempt 1)。 */
  attempt?: number
  narrativeExpected?: boolean
}

export type RetryStrategy = 'schema_feedback' | 'backoff' | 'degrade' | 'same_inputs' | 'hard_stop' | string

export interface ToolRetryProgress {
  toolName: string
  attempt: number
  maxRetries: number
  strategy: RetryStrategy
  reason: string
  lastError: string
  backoffSeconds: number
  publicUpdate?: PublicExecutionUpdate
}

/**
 * Structured, deterministic user-facing narrative for one tool step.
 * Attached to TOOL_FINISHED / TOOL_FAILED / RETRYING payloads so the
 * UI never has to parse raw tool output.  Built by
 * ``public_execution_update_builder`` from real tool-result fields —
 * no LLM is invoked.
 */
export interface PublicExecutionUpdate {
  version: number
  kind: 'tool_result' | 'tool_retry'
  level: 'success' | 'warning' | 'info' | 'retrying'
  headline: string
  summary: string
  impact: string
  nextAction: string
  details: string[]
  narrativeText?: string
  source: 'template' | 'llm' | 'deterministic'
  dedupeKey: string
  // Section 24-ext — streaming chunk frame coordinates emitted by the
  // orchestrator's progressive payload.  Defaults make legacy
  // single-frame payloads render unchanged.  Frontend uses
  // ``chunk_index`` to discard out-of-order chunks during SSE
  // reconnect, and ``chunkFinal`` to mark the message as fully
  // delivered so the UI can stop reacting to follow-up frames.
  chunkIndex?: number
  chunkTotal?: number
  chunkFinal?: boolean
}

export interface SummaryMetric {
  label: string
  value: string
}

export interface SummaryCardData {
  title: string
  description: string
  metrics: SummaryMetric[]
}

export interface SectionItem {
  id: string
  code: string
  title: string
  level: string
  suggestedAction: SectionAction
  action: SectionAction
  reason: string
  /**
   * F022: optional marker indicating where ``suggestedAction`` came from.
   * ``'user_prompt'`` means the user's natural-language prompt was
   * interpreted by ``UserConstraintExtractor`` and overrode the template
   * default.  Absent / ``'template'`` means the template's mode field
   * drove the suggestion.
   */
  constraintSource?: SectionConstraintSource
}

export interface GeneratingStatus {
  title: string
  currentStep: string
  currentSection: string
  progress: number
}

export interface ReviewResult {
  passed: boolean
  businessModules: number
  generatedSections: number
  keptSections: number
  manualSections: string[]
  risks: string[]
}

export interface Artifact {
  id: string
  type: ArtifactType
  name: string
  size: string
  generatedAt: string
  downloadUrl?: string
  pageCount?: number | null
}

export interface ConfirmationReceipt {
  kind: 'section' | 'clarification' | 'format_loss'
  markdown: string
}

export interface ChatMessage {
  id: string
  type: MessageType
  role: 'user' | 'agent'
  eventType?: AgentEventType
  createdAt?: string
  /** Phase 2.9A.26+: per-conversation monotonic sequence for stable ordering */
  conversationSequence?: number
  /**
   * Phase 2.9A.35: 客户端消息顺序 — 每次发送只生成一个,
   * 用于把乐观消息锚定到它应有的位置(不依赖 Date.now 字典序)。
   */
  clientOrder?: number
  /**
   * Phase 2.9A.35: 同一 clientOrder 内的视觉层级 — user=0, agent thinking=1。
   * 保证 User 消息永远排在 Thinking 之前。
   */
  timelineRank?: number
  /**
   * Phase 2.9A.35: 锚定关联消息 id(thinking 锚定到它跟随的 user 消息)。
  */
  anchorMessageId?: string
  /** Client-only marker used to survive a stale conversation restore. */
  optimistic?: boolean
  /** Phase 2.9A.26+: public_id of the message this one replies to (when role='agent'). */
  replyToMessageId?: string
  taskId?: string
  confirmationId?: string
  confirmationType?: string
  confirmed?: boolean
  confirming?: boolean
  streaming?: boolean
  thinking?: boolean
  text?: string
  files?: FileAttachment[]
  plan?: TaskPlanStep[]
  planRevision?: number
  toolCall?: ToolCall
  toolRetry?: ToolRetryProgress
  summary?: SummaryCardData
  sections?: SectionItem[]
  clarification?: PreparationClarification
  generating?: GeneratingStatus
  review?: ReviewResult
  artifact?: Artifact
  formatLoss?: FormatLossConfirmation
  confirmationReceipt?: ConfirmationReceipt
  timestamp: string
  // F026: knowledge-base direct-answer attribution (shown as a 📚 来源 badge)
  kbDirectAnswer?: KbDirectAnswer
  documentCitations?: DocumentCitation[]
  // F026: routing reason (useful for surfacing "知识库参考（置信度低）" etc.)
  intent?: string
  route?: string
  // Phase 2.9A.25+: user feedback for this assistant message
  feedback?: 'like' | 'dislike' | null
  feedbackPending?: boolean
  // Phase 2.9A.26+: regeneration in progress (buttons disabled + thinking state)
  regenerating?: boolean
  // Phase 2.9A.30: raw SSE event data, used by ChatWorkspace to route through applyLiveTaskEvent
  _rawEvent?: Record<string, unknown>
}

export interface PreparationClarification {
  cards: Array<{
    id: string
    question: string
    severity: 'high' | 'medium' | 'low' | string
    selectionMode?: 'single' | 'multiple'
    options?: Array<{
      id: string
      label: string
      description?: string
    }>
    allowConservativeScope?: boolean
  }>
  retrievalSummary?: {
    retrievalRound?: number
    companyRag?: { status?: string; hitCount?: number }
    projectRag?: { status?: string; hitCount?: number; reason?: string }
  }
}

// ── Phase 2.9A.30: Task run internal models ───────────────────────────

/**
 * Normalized form of a raw SSE/event-list event.
 * Produced by `normalizeTaskEvent()` — pure function, no side effects.
 */
export interface NormalizedTaskEvent {
  event_id: string
  event_type: string
  task_id: string
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
  /** Phase 2.9A.35: 有效归约顺序 — task_created=0, Graph 按 sequence_no。 */
  reductionOrder?: number
}

/**
 * A single tool invocation within a task run.
 * Tracks chunk-level dedup and lifecycle status.
 */
export interface ToolExecutionViewModel {
  /** Dedupe key: tool_call_id + attempt */
  logicalKey: string
  toolCallId: string
  toolName: string
  attempt: number

  status: 'pending' | 'running' | 'retrying' | 'success' | 'failed'
  terminal: boolean

  /** Event-level dedup: event_id → true */
  seenEventIds: Record<string, true>
  /** Chunk-level dedup: chunk_index → true */
  receivedChunkIndexes: Record<number, true>
  chunkTotal?: number

  startedAt?: string
  completedAt?: string
  durationMs?: number
  /** Phase 2.9A.35: startedAt 的 UTC epoch ms,避免 Date.parse 时区漂移。 */
  startedAtMs?: number

  input?: string
  output?: string
  displayName?: string
  businessAction?: string
  businessSubjectType?: string
  businessSubjectName?: string
  filePublicId?: string
  fileName?: string
  progressMessage?: string
  completionMessage?: string
  errorSummary?: string
  publicUpdate?: PublicExecutionUpdate
  error?: { message: string; code?: string }
}

/**
 * Structured summary facts from task_completed event or task detail.
 * Field names are normalized from snake_case (backend) to camelCase.
 */
export interface SummaryFacts {
  generatedSections: number
  keptSections: number
  businessModules: number
  pageCount?: number | null
  review: {
    block_count: number
    warning_count: number
    suggestion_count: number
    level: string | null
    passed: boolean
  }
  formatStatus: string | null
}

/** Phase 2.9A.30+: complete task run block — single source of truth per task. */
export interface TaskRunBlock {
  taskId: string
  taskType?: string
  triggerMessageId: string | null

  status: TaskStatus
  startedAt?: string
  completedAt?: string
  durationMs?: number | null

  /** Last source that updated this block — used for Hydrate/Live race protection. */
  lastUpdateMode: 'hydrate' | 'live'

  /** Controlled collapse state. */
  collapsed: boolean
  /** null = not manually set; true/false = user explicitly toggled. */
  userCollapseOverride: boolean | null

  /** All raw events processed by the reducer. */
  events: NormalizedTaskEvent[]
  /** Event-level dedup: event_id → true. */
  seenEventIds: Record<string, true>
  /**
   * Phase 2.9A.35: 三个 cursor 彻底拆分,禁止再混用一个 lastEventCursor。
   * - eventListPageCursor: event-list 分页游标(next_cursor),与 SSE 无关。
   * - lastAppliedCanonicalOrder: 历史展示/去重用的最大 canonical_order。
   * - lastSseSequence: 只从 event.sequence_no 非空值计算,供 SSE Last-Event-ID。
   */
  eventListPageCursor?: number
  lastAppliedCanonicalOrder?: number
  lastSseSequence?: number
  /** @deprecated Phase 2.9A.35: 被 eventListPageCursor / lastAppliedCanonicalOrder / lastSseSequence 取代。 */
  lastEventCursor?: number

  /** Phase 2.9A.35: 业务阶段 — section_confirmation 等,由 need_user_confirm 驱动。 */
  currentPhase?: string

  /** Messages to render inside AgentRunCard. */
  messages: ChatMessage[]
  /** Tool invocations with chunk-level dedup. */
  toolExecutions: ToolExecutionViewModel[]
  /** Phase 2.9B.2: 动态叙事节点(独立于 Tool 卡片)。 */
  dynamicNarratives: DynamicNarrative[]
  /** Phase 2.9B.4: LLM-first Tool 叙事流式状态(锚定到 Tool 卡片)。 */
  toolNarratives: ToolNarrativeState[]
  /** Phase 2.9B.6: 最终任务总结叙事状态槽(唯一权威槽位)。 */
  taskSummaryNarrative?: TaskSummaryNarrativeState | null

  /** Structured summary from task_completed or task detail. */
  summaryFacts?: SummaryFacts | null
  /** All artifacts (supports multiple产物). */
  artifacts: Artifact[]
  formatCheckResult?: Record<string, unknown>
  reviewResult?: Record<string, unknown>

  /** Non-null when restore failed — block still shown with error state. */
  restoreError?: {
    code: string
    message: string
  } | null
}

/** Phase 2.9A.26+: discriminated timeline item. */
export type TimelineItem =
  | { kind: 'message'; id: string; order: number; message: ChatMessage }
  | { kind: 'task-run'; id: string; order: number; run: TaskRunBlock }

export interface KbDirectAnswer {
  attempted: boolean
  hit: boolean
  confidence: 'high' | 'medium' | 'low' | string
  reason?: string
  errorCode?: string | null
  sourceAttribution?: KbSourceAttribution[]
}

export interface DocumentCitation {
  source_id: string
  document_id: string
  chunk_id: string
  title: string
  version: string
  section?: string | null
  role: string
}

export interface KbSourceAttribution {
  doc_name?: string
  document_name?: string
  title?: string
  chunk_id?: string
  score?: number
  url?: string
  snippet?: string
}

export interface Conversation {
  id: string
  title: string
  subtitle: string
  projectId?: string | null
  projectName?: string | null
  state: ConversationState
  updatedAt: string
  messageCount?: number
  fileCount?: number
  latestTask?: AgentTask | null
  tasks?: AgentTask[]
  files: FileAttachment[]
  draftFiles: FileAttachment[]
  messages: ChatMessage[]
  /**
   * Phase 2.9A.26+: assembled task run blocks.  Front-end renders
   * these as a single AgentRunCard anchored to the trigger user
   * message's conversation_sequence, replacing the old "scatter
   * task events as separate ChatMessages" approach.
   */
  taskRunBlocks?: TaskRunBlock[]
  /**
   * @deprecated Phase 2.9A.30: Use TaskRunBlock.lastEventCursor per-task instead.
   * Kept for backward compatibility; new code must not read this field.
   */
  lastEventCursor?: number
}

export interface SettingsConfig {
  apiBaseUrl: string
  apiKey: string
  modelName: string
  timeoutSeconds: number
  knowledgeBaseUrl: string
  knowledgeCollection: string
  enableKnowledgeBase: boolean
  maxFileSizeMb: number
  maxFilesPerConversation: number
  allowedExtensions: string[]
  capabilityType?: string
  contextWindowTokens?: number | null
  contextWindowK?: number | null
  embeddingDimension?: number | null
  normalizeEmbeddings?: boolean | null
}

export const sectionActionOptions: SelectOption<SectionAction>[] = [
  { label: 'AI 生成', value: 'ai_generate' },
  { label: '保留模板', value: 'keep_template' },
  { label: '手动填写', value: 'manual_fill' },
  { label: '不参与生成', value: 'skip' }
]
