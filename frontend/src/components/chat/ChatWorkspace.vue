<template>
  <section
    class="chat-workspace"
    :style="workspaceLayoutStyle"
    @dragenter="handleDragEnter"
    @dragover="handleDragOver"
    @dragleave="handleDragLeave"
    @drop="handleDrop"
  >
    <RouterLink
      v-if="conversation.projectId && conversation.projectName"
      class="chat-project-link"
      :to="{ name: 'projects-detail', params: { projectId: conversation.projectId } }"
      :title="`进入项目：${conversation.projectName}`"
    >
      <img class="chat-project-link__icon" :src="projectIcon" alt="" aria-hidden="true" />
      <span class="chat-project-link__name">{{ conversation.projectName }}</span>
    </RouterLink>
    <div
      ref="chatScrollRef"
      class="chat-scroll"
      :class="{
        'chat-scroll--welcome': showWelcomeState,
        'chat-scroll--with-project': !!conversation.projectId && !!conversation.projectName,
        'chat-scroll--floating-confirmation': Boolean(pendingSectionConfirmation) || Boolean(pendingPreparationClarification) || Boolean(pendingFormatLossConfirmation)
      }"
      @scroll="handleScroll"
    >
      <div ref="chatContentRef" class="chat-scroll__content">
        <EmptyState v-if="showWelcomeState" @select="handleSelectSuggestion" />
        <ChatMessageList
          v-else
        :messages="conversation.messages"
        :conversation-id="conversation.id"
        :conversation-title="conversation.title"
        :task-run-blocks="conversation.taskRunBlocks"
        :tasks="conversation.tasks"
        :latest-task="conversation.latestTask"
        :confirm-sections-ready="confirmSectionsReady"
        :section-confirmation-external="true"
        :format-loss-external="true"
        :hydrated="true"
        @confirm-section="handleConfirmSections"
        @download-artifact="handleDownloadArtifact"
        @retry-task="handleRetryTask"
        @format-loss-decision="handleFormatLossDecision"
        @toast="handleToast"
        @feedback-change="handleFeedbackChange"
        @regenerate-message="handleRegenerateMessage"
        @update-task-collapsed="handleTaskCollapsed"
        @open-file="handleOpenFile"
        />
      </div>
      <!-- 移除取消任务按钮 -->
    </div>
    <PreparationClarificationCard
      v-if="pendingPreparationClarification"
      class="chat-workspace__clarification"
      :clarification="pendingPreparationClarification.clarification!"
      :submitting="pendingPreparationClarification.confirming"
      @submit="handlePendingPreparationClarification"
      @layout-change="handleClarificationCardLayoutChange"
    />
    <SectionConfirmCard
      v-else-if="pendingSectionConfirmation"
      class="chat-workspace__section-confirmation"
      :sections="pendingSectionConfirmation.sections!"
      :confirmed="pendingSectionConfirmation.confirmed"
      :submitting="pendingSectionConfirmation.confirming"
      @confirm="handlePendingSectionConfirmation"
    />
    <FormatLossConfirmCard
      v-else-if="pendingFormatLossConfirmation"
      class="chat-workspace__format-loss-confirmation"
      :confirmation="pendingFormatLossConfirmation.formatLoss!"
      :seconds-remaining="formatLossSecondsRemaining"
      :submitting="pendingFormatLossConfirmation.confirming"
      :error="formatLossDecisionError"
      @decision="handlePendingFormatLossDecision"
    />
    <ChatInputBox
      :files="conversation.draftFiles"
      :draft-text="draftText"
      :send-disabled="inputSendDisabled"
      :response-active="responseActive"
      :runtime-active="inputRuntimeActive"
      :busy-text="inputBusyText"
      :model-name="settingsStore.settings.modelName"
      :context-usage="contextUsage.usage.value"
      :context-usage-loading="contextUsage.loading.value"
      :context-usage-unavailable="contextUsage.unavailable.value"
      :context-compacting="contextUsage.compacting.value"
      :context-compaction-state="contextUsage.compactionState.value"
      :knowledge-mode="currentKnowledgeMode"
      @upload="handleUpload"
      @paste-files="handlePasteFiles"
      @draft-change="handleDraftChange"
      @send="handleSend"
      @stop="handleStopResponse"
      @remove-file="handleRemoveFile"
      @open-file="handleOpenFile"
      @toggle-knowledge-mode="handleToggleKnowledgeMode"
      @context-popover-open="handleContextPopoverOpen"
      @compact-context="handleCompactContext"
      @open-template-picker="openTemplatePicker"
      @open-template-market="openTemplateMarket"
      @open-library-picker="openLibraryPicker"
      @dock-layout="handleComposerDockLayout"
    />
  </section>
  <TemplatePickerDialog
    :show="templatePickerVisible"
    :items="templateStore.myItems"
    :loading="templateStore.loading.mine"
    :error="templateStore.errors.mine"
    @close="templatePickerVisible = false"
    @retry="loadTemplatePicker"
    @open-market="openTemplateMarket"
    @select="attachTemplate"
  />
  <LibraryPickerDialog
    :show="libraryPickerVisible"
    :items="libraryPickerItems"
    :loading="libraryPickerLoading"
    :error="libraryPickerError"
    :attaching-id="libraryAttachingItemId"
    @close="closeLibraryPicker"
    @retry="loadLibraryPicker"
    @select="attachLibraryItem"
  />
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, shallowRef, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import type { ChatMessage, Conversation, FileAttachment, FileType, SectionItem } from '@/types'
import ChatInputBox from './ChatInputBox.vue'
import ChatMessageList from './ChatMessageList.vue'
import PreparationClarificationCard from '@/components/cards/PreparationClarificationCard.vue'
import SectionConfirmCard from '@/components/cards/SectionConfirmCard.vue'
import FormatLossConfirmCard from '@/components/cards/FormatLossConfirmCard.vue'
import EmptyState from './EmptyState.vue'
import TemplatePickerDialog from '@/components/templates/TemplatePickerDialog.vue'
import LibraryPickerDialog from '@/components/library/LibraryPickerDialog.vue'
import { useConversationStore } from '@/stores/conversationStore'
import { useFileStore } from '@/stores/fileStore'
import { useAgentTaskStore } from '@/stores/agentTaskStore'
import { useSettingsStore } from '@/stores/settingsStore'
import { useTemplateStore } from '@/stores/templateStore'
import { useTemplate } from '@/api/templateApi'
import type { UserTemplateItem } from '@/types/template'
import { fetchLibraryItemBlob, fetchLibraryItems } from '@/api/libraryApi'
import type { LibraryItem } from '@/types/library'
import { useSse } from '@/composables/useSse'
import { createTaskEventHandler } from '@/composables/useTaskEvents'
import { isTerminalTaskStatus, taskBlocksInput, taskBusyText } from '@/utils/taskState'
import * as agentApi from '@/api/agentApi'
import * as artifactApi from '@/api/artifactApi'
import {
  compactConversationContext,
  fetchContextUsage,
  fetchConversations as fetchConversationSummaries,
  previewContextUsage
} from '@/api/conversationApi'
import * as messageApi from '@/api/messageApi'
import { extractDroppedFiles, hasFileTransfer, isDirectoryTransfer } from '@/utils/dragDropFiles'
import { useContextUsage } from '@/composables/useContextUsage'
import { normalizeTaskEventPayload } from '@/utils/eventPayload'
import { buildChatFilePreviewLocation, canPreviewFileAttachment } from '@/utils/filePreview'
import projectIcon from '@/assets/Sidebar-svg/项目.svg'

const props = defineProps<{
  conversation: Conversation
}>()

const router = useRouter()
const toast = useMessage()
const conversationStore = useConversationStore()
const fileStore = useFileStore()
const agentTaskStore = useAgentTaskStore()
const settingsStore = useSettingsStore()
const templateStore = useTemplateStore()
const templatePickerVisible = ref(false)
const libraryPickerVisible = ref(false)
const libraryPickerItems = ref<LibraryItem[]>([])
const libraryPickerLoading = ref(false)
const libraryPickerError = ref('')
const libraryAttachingItemId = ref('')
const sse = useSse()
const draftText = ref('')
const chatScrollRef = ref<HTMLElement | null>(null)
const chatContentRef = ref<HTMLElement | null>(null)
const shouldFollowIncomingMessages = ref(true)
// AbortController is an imperative browser handle, not application state.  A
// shallow ref preserves its exact identity for the direct-reply lifecycle.
const activeStreamController = shallowRef<AbortController | null>(null)
const activeStreamConversationId = ref('')
const activeStreamAgentMessageId = ref('')
const activeStreamHasText = ref(false)
const pendingConfirmationRetryAbort = ref<AbortController | null>(null)
const dragDepth = ref(0)
const bottomThreshold = 120
const showWelcomeState = computed(
  () =>
    ['empty', 'uploaded'].includes(props.conversation.state) &&
    props.conversation.messages.length === 0 &&
    !props.conversation.latestTask
)
const confirmSectionsReady = computed(() =>
  props.conversation.latestTask?.status === 'waiting_user_confirm' ||
  props.conversation.messages.some(
    (message) => message.type === 'section_confirm' && !!message.taskId && !message.confirmed
  )
)
const pendingPreparationClarification = computed(() =>
  findLatestPreparationClarificationMessage(props.conversation)
)
const pendingSectionConfirmation = computed(() =>
  findLatestSectionConfirmMessage(props.conversation)
)
const pendingFormatLossConfirmation = computed(() =>
  findLatestFormatLossMessage(props.conversation)
)
const formatLossSecondsRemaining = ref<number | null>(null)
const formatLossDecisionError = ref('')
let formatLossCountdownTimer: ReturnType<typeof setInterval> | null = null
const composerPanelTopOffset = ref(160)
const workspaceLayoutStyle = computed(() => ({
  '--chat-composer-panel-top-offset': `${composerPanelTopOffset.value}px`
}))
const hasUploadingDraftFiles = computed(() =>
  props.conversation.draftFiles.some((file) => file.status === 'uploading')
)
const inputSendDisabled = computed(() => hasUploadingDraftFiles.value)
const inputBusyText = computed(() => (hasUploadingDraftFiles.value ? '文件上传中...' : taskBusyText(props.conversation)))
const responseActive = computed(() => !!activeStreamController.value)
const inputRuntimeActive = computed(() => responseActive.value || taskBlocksInput(props.conversation))
const currentKnowledgeMode = computed(() => conversationStore.getKnowledgeMode(props.conversation.id))
type IncomingFileSource = 'picker' | 'paste' | 'drop'
const thinkingText = '正在思考...'
const terminalTaskStatuses = new Set(['completed', 'failed', 'cancelled'])
const contextUsage = useContextUsage({
  getConversationId: () => props.conversation.id === 'conv_new' ? null : props.conversation.id,
  fetchUsage: fetchContextUsage,
  previewUsage: previewContextUsage,
  compact: compactConversationContext,
  notifySuccess: (text) => toast.success(text),
  notifyError: (text) => toast.error(text)
})
const CONTEXT_PREVIEW_DEBOUNCE_MS = 350
let contextPreviewTimer: ReturnType<typeof setTimeout> | null = null
const _autoSseConnectedTaskId = ref('')

// Phase 2.9A.36: SSE 后端 yield 卡死兜底（agent_text_done / agent_task_created
// / error 长期不到 → 强制 abortstream + 释放 controller。已有文本会保留；
// 首个事件都未到达时显示明确错误，不能把字面量“正在思考...”永久留在消息区。
const STREAM_IDLE_TIMEOUT_MS = 60_000
let streamWatchdogTimer: ReturnType<typeof setTimeout> | null = null
let streamWatchdogConversationId = ''
let streamWatchdogMessageId = ''
let directReplyPersistenceTimer: ReturnType<typeof setTimeout> | null = null
// Sidebar title updates are deliberately asynchronous: they must never delay
// the terminal message lifecycle or keep the composer in a busy state.
const titleRefreshTimers = new Map<string, ReturnType<typeof setTimeout>>()
const MAX_TITLE_REFRESH_ATTEMPTS = 5
const DEFAULT_CONVERSATION_TITLES = new Set(['新会话', '新对话', 'New conversation', 'New Conversation'])

function clearStreamWatchdog(): void {
  if (streamWatchdogTimer !== null) {
    clearTimeout(streamWatchdogTimer)
    streamWatchdogTimer = null
  }
  streamWatchdogConversationId = ''
  streamWatchdogMessageId = ''
}

function clearDirectReplyPersistenceCheck(): void {
  if (directReplyPersistenceTimer !== null) {
    clearTimeout(directReplyPersistenceTimer)
    directReplyPersistenceTimer = null
  }
}

function resetStreamWatchdog(conversationId: string, messageId: string): void {
  clearStreamWatchdog()
  clearDirectReplyPersistenceCheck()
  streamWatchdogConversationId = conversationId
  streamWatchdogMessageId = messageId
  streamWatchdogTimer = setTimeout(() => {
    const cid = streamWatchdogConversationId
    const mid = streamWatchdogMessageId
    streamWatchdogTimer = null
    if (!cid || !mid) return
    if (activeStreamController.value) {
      activeStreamController.value.abort()
    }
    conversationStore.updateMessage(
      cid,
      mid,
      activeStreamHasText.value
        ? { thinking: false, streaming: false }
        : {
            type: 'error',
            role: 'agent',
            text: '响应超时，请重试',
            thinking: false,
            streaming: false
          }
    )
    clearActiveStream()
  }, STREAM_IDLE_TIMEOUT_MS)
}

onMounted(() => {
  if (typeof ResizeObserver === 'undefined' || !chatContentRef.value) return
  contentResizeObserver = new ResizeObserver(() => scheduleFollowToBottom())
  contentResizeObserver.observe(chatContentRef.value)
})

onUnmounted(() => {
  activeStreamController.value?.abort()
  pendingConfirmationRetryAbort.value?.abort()
  contextUsage.dispose()
  sse.disconnect()
  clearStreamWatchdog()
  clearDirectReplyPersistenceCheck()
  contentResizeObserver?.disconnect()
  contentResizeObserver = null
  if (pendingFollowFrame !== null) cancelAnimationFrame(pendingFollowFrame)
  for (const timer of titleRefreshTimers.values()) clearTimeout(timer)
  titleRefreshTimers.clear()
  if (contextPreviewTimer) clearTimeout(contextPreviewTimer)
  if (formatLossCountdownTimer) clearInterval(formatLossCountdownTimer)
})

function scheduleGeneratedTitleRefresh(conversationId: string, attempt = 0): void {
  const priorTimer = titleRefreshTimers.get(conversationId)
  if (priorTimer) clearTimeout(priorTimer)
  titleRefreshTimers.delete(conversationId)

  void (async () => {
    titleRefreshTimers.delete(conversationId)
    try {
      // Title generation deliberately runs out of band so it never delays the
      // first SSE event.  Fetching list summaries is safe here: the store's
      // merge preserves the in-memory messages and task state.
      const summaries = await fetchConversationSummaries()
      const summary = summaries.find((item) => item.id === conversationId)
      if (summary) conversationStore.mergeConversationSummary(summary)
    } catch {
      // A title refresh is cosmetic; the next regular sidebar refresh retries.
      return
    }

    const current = conversationStore.conversations.find((item) => item.id === conversationId)
    if (
      current
      && DEFAULT_CONVERSATION_TITLES.has(current.title.trim())
      && attempt < MAX_TITLE_REFRESH_ATTEMPTS
    ) {
      titleRefreshTimers.set(
        conversationId,
        setTimeout(() => scheduleGeneratedTitleRefresh(conversationId, attempt + 1), 1200 * (attempt + 1))
      )
    }
  })()
}

watch(
  () => props.conversation.id,
  (conversationId) => {
    _autoSseConnectedTaskId.value = '' // Phase 2.9A.28: 切换会话时重置 SSE 追踪
    draftText.value = ''
    if (contextPreviewTimer) clearTimeout(contextPreviewTimer)
    shouldFollowIncomingMessages.value = true
    void conversationStore.loadKnowledgeMode(props.conversation.id)
    void contextUsage.refresh('conversation-switch')
    void nextTick(() => {
      const scrollEl = chatScrollRef.value
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight
    })
    const initialMessage = conversationStore.takeInitialMessage(conversationId)
    const initialFiles = conversationStore.takeInitialFiles(conversationId)
    const initialAttachments = conversationStore.takeInitialAttachments(conversationId)
    initialAttachments.forEach((file) => conversationStore.addDraftFile(conversationId, file))
    if (initialMessage) {
      void nextTick(async () => {
        if (initialFiles.length) await handleIncomingFiles(initialFiles, 'picker')
        await handleSend(initialMessage)
      })
    }
  },
  { immediate: true }
)

watch(
  () => pendingPreparationClarification.value?.id,
  (clarificationId, previousClarificationId) => {
    if (!clarificationId || clarificationId === previousClarificationId) return
    void scrollToBottomAfterRender(true)
  },
  { immediate: true }
)

watch(
  () => pendingSectionConfirmation.value?.id,
  (confirmationId, previousConfirmationId) => {
    if (!confirmationId || confirmationId === previousConfirmationId) return
    void scrollToBottomAfterRender(true)
  },
  { immediate: true }
)

watch(
  () => pendingFormatLossConfirmation.value?.id,
  (confirmationId, previousConfirmationId) => {
    if (!confirmationId || confirmationId === previousConfirmationId) return
    formatLossDecisionError.value = ''
    void scrollToBottomAfterRender(true)
  },
  { immediate: true }
)

watch(
  () => pendingFormatLossConfirmation.value?.formatLoss?.timeoutAt,
  (timeoutAt) => {
    if (formatLossCountdownTimer) {
      clearInterval(formatLossCountdownTimer)
      formatLossCountdownTimer = null
    }
    if (!timeoutAt) {
      formatLossSecondsRemaining.value = null
      return
    }
    const deadline = new Date(timeoutAt).getTime()
    if (Number.isNaN(deadline)) {
      formatLossSecondsRemaining.value = null
      return
    }
    const updateCountdown = () => {
      formatLossSecondsRemaining.value = Math.max(0, Math.ceil((deadline - Date.now()) / 1000))
      if (formatLossSecondsRemaining.value === 0 && formatLossCountdownTimer) {
        clearInterval(formatLossCountdownTimer)
        formatLossCountdownTimer = null
      }
    }
    updateCountdown()
    formatLossCountdownTimer = setInterval(updateCountdown, 1000)
  },
  { immediate: true }
)

watch(
  () => `${props.conversation.id}:${props.conversation.latestTask?.task_id ?? ''}:${props.conversation.latestTask?.status ?? ''}`,
  () => {
    const task = props.conversation.latestTask
    if (!task || !terminalTaskStatuses.has(task.status)) return
    void contextUsage.refresh('agent-task-terminal')
  },
  { immediate: true }
)

// Phase 2.9A.28: 恢复会话后自动恢复实时 SSE。
// 根因：重新进入 running 任务时，restoreConversation 只做 history hydration，
// 不建立实时 SSE → 任务永久停在旧状态（用户报告"RequirementParser 卡在 RUNNING"）。
// 现在监听 latestTask 变化：若任务仍非终态且 SSE 未连接，立即自动 connectTaskEvents。
watch(
  () => props.conversation.latestTask,
  (task) => {
    if (!task) return
    if (terminalTaskStatuses.has(task.status)) return
    // 已经为这个 task 建立过连接 → 不重复
    if (_autoSseConnectedTaskId.value === task.task_id) return
    // SSE 已连接（来自 handleSend 或 handleConfirmSections）→ 不重复
    if (sse.isConnected.value) return
    _autoSseConnectedTaskId.value = task.task_id
    // Phase 2.9A.35: SSE Last-Event-ID 使用 lastSseSequence(只从 sequence_no
    // 非空值计算),绝不能用 lastAppliedCanonicalOrder(可能是 1000000001)。
    const taskBlock = props.conversation.taskRunBlocks?.find(b => b.taskId === task.task_id)
    const cursor = taskBlock?.lastSseSequence
      ? String(taskBlock.lastSseSequence)
      : undefined
    void nextTick(() => {
      connectTaskEvents(props.conversation.id, task.events_url, task.task_id, undefined, cursor)
    })
  },
  { immediate: true }
)

function isNearBottom() {
  const scrollEl = chatScrollRef.value
  if (!scrollEl) return true
  return scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight <= bottomThreshold
}

function handleScroll() {
  shouldFollowIncomingMessages.value = isNearBottom()
}

let contentResizeObserver: ResizeObserver | null = null
let pendingFollowFrame: number | null = null

function scheduleFollowToBottom(force = false): void {
  if (!force && !shouldFollowIncomingMessages.value) return
  if (pendingFollowFrame !== null) cancelAnimationFrame(pendingFollowFrame)
  shouldFollowIncomingMessages.value = true
  pendingFollowFrame = requestAnimationFrame(() => {
    pendingFollowFrame = null
    const scrollEl = chatScrollRef.value
    if (!scrollEl) return
    // Use the actual scroll container as the authority.  This runs after the
    // browser has laid out streamed Markdown, rather than relying on a stale
    // scrollHeight captured during Vue's DOM patch.
    scrollEl.scrollTop = scrollEl.scrollHeight - scrollEl.clientHeight
    shouldFollowIncomingMessages.value = true
  })
}

async function scrollToBottomAfterRender(force = false) {
  if (!force && !shouldFollowIncomingMessages.value) return
  await nextTick()
  scheduleFollowToBottom(force)
}

function connectTaskEvents(
  conversationId: string,
  eventsUrl: string,
  taskId?: string,
  placeholderMessageId?: string,
  initialCursor?: string,
) {
  // Phase 2.9A.2:取消任何进行中的 pending-confirmation retry(新任务/切换 task 时)
  pendingConfirmationRetryAbort.value?.abort()
  pendingConfirmationRetryAbort.value = new AbortController()
  let firstEventReceived = false

  // Phase 2.9A.23 fix-3: 显式绑定占位消息 ID — 不再扫描 store 做兜底。
  // placeholderMessageId 来自 handleSend 的局部变量,通过参数显式传入,
  // 绝不依赖闭包隐式捕获或 activeConversation.messages 时序扫描。
  const boundPlaceholderId = placeholderMessageId ?? ''

  sse.connect(eventsUrl, {
    onMessage: createTaskEventHandler({
      taskId,
      // Phase 2.9A.23 fix-3: onAnyEvent 在所有业务过滤之前触发,
      // 解决 plan_step_started 早退 (useTaskEvents line 138)
      // 导致 onAppendMessage 永远不被首个事件调用的问题。
      onAnyEvent: (eventType) => {
        if (firstEventReceived) return
        firstEventReceived = true

        // 记录删除前状态(开发日志)
        let existsBefore = false
        let countBefore = 0
        if (import.meta.env.DEV) {
          const msgs = conversationStore.activeConversation?.messages ?? []
          countBefore = msgs.length
          existsBefore = msgs.some((message) => message?.id === boundPlaceholderId)
          console.debug('[Phase2.9A.23] thinking_placeholder_clear_started', {
            task_id: taskId ?? null,
            event_type: eventType,
            target_id: boundPlaceholderId,
            message_exists_before_remove: existsBefore,
            store_message_count_before: countBefore,
            active_conversation_id: conversationStore.activeConversationId
          })
        }

        const removedThinkingCount = conversationStore.removeTaskThinkingPlaceholders(
          conversationId,
          boundPlaceholderId || undefined
        )

        if (import.meta.env.DEV) {
          const after = conversationStore.activeConversation?.messages ?? []
          const existsAfter = after.some((message) => message?.id === boundPlaceholderId)
          console.debug('[Phase2.9A.23] thinking_placeholder_clear_completed', {
            task_id: taskId ?? null,
            target_id: boundPlaceholderId,
            message_exists_before_remove: existsBefore,
            message_exists_after_remove: existsAfter,
            removed_thinking_count: removedThinkingCount,
            store_message_count_before: countBefore,
            store_message_count_after: after.length,
            store_message_ids: after.map((m) => m?.id)
          })
        }
      },
      onAppendMessage: (message) => {
        const shouldScroll = shouldFollowIncomingMessages.value
        // Phase 2.9A.30: route task events through store action
        if (message.taskId && message._rawEvent) {
          conversationStore.applyLiveTaskEvent(conversationId, message.taskId, message._rawEvent)
        } else {
          conversationStore.appendMessages(conversationId, [message])
        }
        scrollToBottomAfterRender(shouldScroll || message.type === 'section_confirm')
        // 实时兜底:need_user_confirm 事件 payload 含 confirmation_id + sections,
        // 但 ``GET /pending-confirmation`` 在 commit 完成前可能短暂返回 40401。
        // 触发 retry:200ms / 500ms,成功后用正式 confirmation_id 替换临时消息。
        if (message.type === 'section_confirm' && taskId) {
          void retryPendingConfirmation(conversationId, taskId, message)
        }
      },
      onUpdateMessage: (messageId, patch) => {
        const shouldScroll = shouldFollowIncomingMessages.value
        const updated = conversationStore.updateMessage(conversationId, messageId, patch)
        scrollToBottomAfterRender(shouldScroll)
        return updated
      },
      onStatusChange: (_taskId, status, meta) => {
        if (status === 'waiting_user_confirm') conversationStore.setConversationState(conversationId, 'waiting_user_confirm')
        if (status === 'running') conversationStore.setConversationState(conversationId, 'planning')
        if (status === 'completed') conversationStore.setConversationState(conversationId, 'completed')
        if (status === 'failed') conversationStore.setConversationState(conversationId, 'failed')
        if (status === 'cancelled') conversationStore.setConversationState(conversationId, 'failed')
        conversationStore.updateLatestTaskStatus(conversationId, status, {
          taskId: _taskId,
          startedAt: meta?.startedAt,
          completedAt: meta?.completedAt ?? meta?.createdAt,
          durationMs: meta?.durationMs
        })
        if (status === 'completed' || status === 'failed' || status === 'cancelled') {
          conversationStore.removeTaskThinkingPlaceholders(conversationId)
          _autoSseConnectedTaskId.value = ''
          sse.disconnect()
          void contextUsage.refresh('agent-task-terminal')
        }
        // Phase 2.9A.2:task_waiting 不得清空 pendingConfirmation 状态;
        // section_confirm 卡片由 need_user_confirm 事件驱动,与 task_waiting 解耦。
      }
    }),
    onError: (error) => {
      console.warn('[connectTaskEvents] SSE disconnected, recovering from event-list', {
        conversationId,
        taskId,
        error
      })
      // Phase 2.9A.28: SSE 断开时重置追踪，让 auto-watch 可以重新连接。
      // SSE 只是实时传输通道，任务是否失败必须以 task_failed / task_completed
      // 等后端任务事件为准；通道失败时先用 event-list 恢复已落库进度。
      _autoSseConnectedTaskId.value = ''
      if (taskId) {
        void recoverTaskEventsFromHistory(conversationId, taskId, boundPlaceholderId || undefined)
          .finally(() => reconnectTaskEventsIfStillActive(conversationId, taskId))
      }
    },
    onClose: () => {
      // A normal SSE EOF does not call onError. Reconcile persisted events so
      // a terminal frame lost at stream shutdown cannot leave the composer
      // stuck in its replying state until a page refresh.
      _autoSseConnectedTaskId.value = ''
      if (taskId) {
        void recoverTaskEventsFromHistory(conversationId, taskId, boundPlaceholderId || undefined)
          .finally(() => reconnectTaskEventsIfStillActive(conversationId, taskId))
      }
    }
  }, initialCursor)
}

function reconnectTaskEventsIfStillActive(conversationId: string, taskId: string) {
  const currentTask = props.conversation.latestTask
  if (
    props.conversation.id !== conversationId
    || currentTask?.task_id !== taskId
    || terminalTaskStatuses.has(currentTask.status)
    || sse.isConnected.value
    || sse.isReconnecting.value
  ) {
    return
  }
  // Keep the cursor collected before the disconnect so the server only
  // replays events the history recovery did not already apply.  A temporary
  // worker retry (such as a checkpoint pool timeout) must not leave the task
  // view permanently detached from later events.
  _autoSseConnectedTaskId.value = taskId
  sse.reconnect()
}

async function recoverTaskEventsFromHistory(
  conversationId: string,
  taskId: string,
  placeholderMessageId?: string
) {
  try {
    const result = await agentApi.fetchAllTaskEvents(taskId, { returnPageCursor: true })
    const events = Array.isArray(result) ? result : result.events
    if (!events.length) return

    conversationStore.removeTaskThinkingPlaceholders(conversationId, placeholderMessageId)
    for (const event of events) {
      const eventTaskId = String(event.task_id || taskId)
      conversationStore.applyLiveTaskEvent(conversationId, eventTaskId, { ...event })
      syncTaskStatusFromEvent(conversationId, eventTaskId, event)
    }
    await scrollToBottomAfterRender(true)
  } catch (err) {
    console.warn('[connectTaskEvents] event-list recovery failed', err)
  }
}

function syncTaskStatusFromEvent(
  conversationId: string,
  taskId: string,
  event: agentApi.AgentEventRecord
) {
  const payload = normalizeTaskEventPayload(event.payload) as Record<string, unknown>
  const startedAt = typeof payload.started_at === 'string'
    ? payload.started_at
    : typeof payload.startedAt === 'string' ? payload.startedAt : undefined
  const completedAt = typeof payload.completed_at === 'string'
    ? payload.completed_at
    : typeof payload.completedAt === 'string' ? payload.completedAt : undefined
  const durationRaw = payload.duration_ms ?? payload.durationMs
  const durationMs = typeof durationRaw === 'number'
    ? durationRaw
    : typeof durationRaw === 'string' && durationRaw.trim() ? Number(durationRaw) : undefined
  if (event.event_type === 'incremental_started') {
    conversationStore.setConversationState(conversationId, 'planning')
    conversationStore.updateLatestTaskStatus(conversationId, 'running', {
      taskId,
      startedAt: startedAt ?? event.created_at
    })
    return
  }
  if (event.event_type === 'task_waiting') {
    conversationStore.setConversationState(conversationId, 'waiting_user_confirm')
    conversationStore.updateLatestTaskStatus(conversationId, 'waiting_user_confirm', {
      taskId,
      completedAt: event.created_at
    })
    return
  }
  if (event.event_type === 'format_loss_confirm_requested') {
    conversationStore.setConversationState(conversationId, 'waiting_user_confirm')
    conversationStore.updateLatestTaskStatus(conversationId, 'waiting_user_confirm', {
      taskId,
      startedAt,
      completedAt: undefined
    })
    return
  }
  if (event.event_type === 'task_resumed') {
    conversationStore.setConversationState(conversationId, 'planning')
    conversationStore.updateLatestTaskStatus(conversationId, 'running', {
      taskId,
      completedAt: event.created_at
    })
    return
  }
  if (event.event_type === 'task_completed' || event.event_type === 'incremental_completed') {
    conversationStore.setConversationState(conversationId, 'completed')
    conversationStore.updateLatestTaskStatus(conversationId, 'completed', {
      taskId,
      startedAt,
      completedAt: completedAt ?? event.created_at,
      durationMs: Number.isFinite(durationMs) ? durationMs : undefined
    })
    conversationStore.removeTaskThinkingPlaceholders(conversationId)
    void contextUsage.refresh('agent-task-terminal')
    return
  }
  if (event.event_type === 'task_failed' || event.event_type === 'incremental_failed') {
    conversationStore.setConversationState(conversationId, 'failed')
    conversationStore.updateLatestTaskStatus(conversationId, 'failed', {
      taskId,
      completedAt: event.created_at
    })
    conversationStore.removeTaskThinkingPlaceholders(conversationId)
    void contextUsage.refresh('agent-task-terminal')
    return
  }
  if (event.event_type === 'task_cancelled') {
    conversationStore.setConversationState(conversationId, 'failed')
    conversationStore.updateLatestTaskStatus(conversationId, 'cancelled', {
      taskId,
      completedAt: event.created_at
    })
    conversationStore.removeTaskThinkingPlaceholders(conversationId)
  }
}

async function retryPendingConfirmation(
  conversationId: string,
  taskId: string,
  sectionMessage: ChatMessage
) {
  const controller = pendingConfirmationRetryAbort.value
  if (!controller) return
  try {
    const pending = await agentApi.fetchPendingConfirmationWithRetry(taskId, {
      signal: controller.signal,
    })
    if (!pending) return  // 3 次仍失败,保留事件 payload 中的 sections 数据
    // 成功:用正式 confirmation_id 替换临时消息
    const shouldScroll = shouldFollowIncomingMessages.value
    conversationStore.updateMessage(conversationId, sectionMessage.id, {
      id: `pending_${pending.confirmationId}`,
      confirmationId: pending.confirmationId,
      confirmationType: pending.confirmationType,
      sections: pending.sections,
    })
    scrollToBottomAfterRender(shouldScroll)
  } catch (err) {
    // 已被 abort 或其他失败 — 静默,卡片保留事件 payload 数据
    if ((err as { name?: string })?.name !== 'AbortError') {
      console.warn('[retryPendingConfirmation] failed', err)
    }
  }
}

function handleSelectSuggestion(text: string) {
  draftText.value = text
}

function displayNow() {
  return new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}

function appendOptimisticUserMessage(text: string, files: ChatMessage['files'], clientOrder: number): ChatMessage {
  return {
    id: `msg_user_pending_${Date.now()}`,
    type: 'user_text',
    role: 'user',
    text,
    files,
    timestamp: displayNow(),
    clientOrder,
    timelineRank: 0,
    optimistic: true
  }
}

function createThinkingMessage(clientOrder: number, anchorMessageId: string): ChatMessage {
  const message: ChatMessage = {
    id: `msg_agent_thinking_${Date.now()}`,
    type: 'agent_text',
    role: 'agent',
    text: thinkingText,
    thinking: true,
    streaming: true,
    timestamp: displayNow(),
    clientOrder,
    timelineRank: 1,
    anchorMessageId,
    optimistic: true
  }
  // Phase 2.9A.23 fix-3: 运行时审计 — 输出 Thinking 消息真实结构
  console.debug('[Phase2.9A.23] thinking_placeholder_created', {
    message_id: message.id,
    type: message.type,
    role: message.role,
    thinking: message.thinking,
    streaming: message.streaming,
    taskId: message.taskId ?? null,
    clientOrder: message.clientOrder,
    timelineRank: message.timelineRank,
    anchorMessageId: message.anchorMessageId,
    keys: Object.keys(message)
  })
  return message
}

function mergeStreamMessage(
  optimistic: ChatMessage,
  incoming: ChatMessage,
  role: ChatMessage['role'],
  type: ChatMessage['type']
): ChatMessage {
  return {
    ...optimistic,
    ...incoming,
    id: incoming.id || optimistic.id,
    role,
    type,
    clientOrder: optimistic.clientOrder,
    timelineRank: optimistic.timelineRank,
    anchorMessageId: optimistic.anchorMessageId
  }
}

function incomingFileKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}:${file.type}`
}

function dedupeIncomingFiles(files: File[]): File[] {
  const seen = new Set<string>()
  const uniqueFiles: File[] = []
  for (const file of files) {
    if (!file || (!file.name && file.size === 0)) continue
    const key = incomingFileKey(file)
    if (seen.has(key)) continue
    seen.add(key)
    uniqueFiles.push(file)
  }
  return uniqueFiles
}

function formatLocalFileSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

function createLocalDraftFile(file: File, index: number): FileAttachment {
  const extension = file.name.includes('.') ? `.${file.name.split('.').pop() ?? ''}` : ''
  return {
    id: `local_upload_${Date.now()}_${index}_${Math.random().toString(36).slice(2, 8)}`,
    name: file.name,
    size: formatLocalFileSize(file.size),
    extension,
    type: 'unknown',
    status: 'uploading',
    uploadProgress: 1,
    localOnly: true
  }
}

async function handleIncomingFiles(files: File[], source: IncomingFileSource) {
  const incomingFiles = dedupeIncomingFiles(files)
  if (!incomingFiles.length) return
  if (import.meta.env.DEV) {
    console.debug('[Phase2.9A.38] incoming_files', {
      source,
      count: incomingFiles.length,
      names: incomingFiles.map((file) => file.name)
    })
  }
  try {
    const activeConversation = await conversationStore.ensureActiveConversation(props.conversation.id)
    const uploads = incomingFiles.map(async (file, index) => {
      const localDraft = createLocalDraftFile(file, index)
      conversationStore.addDraftFile(activeConversation.id, localDraft)
      try {
        const uploaded = await fileStore.uploadFile(file, activeConversation.id, undefined, {
          onUploadProgress: (percent) => {
            conversationStore.updateDraftFile(activeConversation.id, localDraft.id, {
              uploadProgress: percent,
              status: 'uploading'
            })
          }
        })
        conversationStore.replaceDraftFile(activeConversation.id, localDraft.id, {
          ...uploaded,
          uploadProgress: 100,
          localOnly: false
        })
      } catch (err) {
        conversationStore.updateDraftFile(activeConversation.id, localDraft.id, {
          status: 'failed',
          uploadProgress: 0,
          description: err instanceof Error ? err.message : '文件上传失败'
        })
        throw err
      }
    })
    const results = await Promise.allSettled(uploads)
    const failedCount = results.filter((result) => result.status === 'rejected').length
    if (failedCount > 0) {
      toast.error(failedCount === 1 ? '文件上传失败' : `${failedCount} 个文件上传失败`)
    }
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '文件上传失败')
  }
}

function handleUpload(files: File[]) {
  void handleIncomingFiles(files, 'picker')
}

function handlePasteFiles(files: File[]) {
  void handleIncomingFiles(files, 'paste')
}

function handleDragEnter(event: DragEvent) {
  if (!hasFileTransfer(event.dataTransfer)) return
  event.preventDefault()
  dragDepth.value += 1
}

function handleDragOver(event: DragEvent) {
  if (!hasFileTransfer(event.dataTransfer)) return
  event.preventDefault()
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
}

function handleDragLeave(event: DragEvent) {
  if (!hasFileTransfer(event.dataTransfer)) return
  dragDepth.value = Math.max(0, dragDepth.value - 1)
}

function handleDrop(event: DragEvent) {
  if (!hasFileTransfer(event.dataTransfer)) return
  event.preventDefault()
  dragDepth.value = 0
  if (isDirectoryTransfer(event.dataTransfer)) {
    toast.error('不支持文件夹上传')
    return
  }
  const files = extractDroppedFiles(event.dataTransfer)
  if (files.length) void handleIncomingFiles(files, 'drop')
}

async function handleSend(text: string) {
  if (inputRuntimeActive.value) {
    try {
      await stopActiveWork()
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '停止上一任务失败')
      return
    }
  }
  if (activeStreamController.value) return
  let thinkingMessageId = ''
  let agentMessageId = ''
  let userMessageId = ''
  let activeConversationId = ''
  let streamedText = ''
  let receivedTerminalEvent = false
  let transportCompletedNormally = false
  const controller = new AbortController()
  try {
    const activeConversation = await conversationStore.ensureActiveConversation(props.conversation.id)
    activeConversationId = activeConversation.id
    const attachedFiles = activeConversation.draftFiles.filter((file) => !file.localOnly && file.status !== 'failed')
    const fileIds = attachedFiles.map((file) => file.id)
    const knowledgeModeSnapshot = conversationStore.getKnowledgeMode(activeConversationId)
    // Phase 2.9A.35: 每次发送只生成一个 clientOrder,User + Thinking 共享
    const clientOrder = conversationStore.nextClientOrder(activeConversationId)
    const optimisticUserMessage = appendOptimisticUserMessage(text, attachedFiles, clientOrder)
    userMessageId = optimisticUserMessage.id
    const thinkingMessage = createThinkingMessage(clientOrder, optimisticUserMessage.id)
    thinkingMessageId = thinkingMessage.id
    agentMessageId = thinkingMessage.id
    activeStreamController.value = controller
    activeStreamConversationId.value = activeConversationId
    activeStreamAgentMessageId.value = agentMessageId
    activeStreamHasText.value = false
    conversationStore.appendMessages(activeConversationId, [optimisticUserMessage, thinkingMessage])
    conversationStore.clearDraftFiles(activeConversationId)
    await scrollToBottomAfterRender(true)
    resetStreamWatchdog(activeConversationId, agentMessageId)

    await messageApi.sendMessageStream(activeConversationId, text, fileIds, {
      onMessageCreated: ({ message, conversation }) => {
        if (conversation) {
          conversationStore.mergeConversationSummary(conversation)
        }
        const streamMessage = mergeStreamMessage(
          optimisticUserMessage,
          message,
          'user',
          'user_text'
        )
        userMessageId = streamMessage.id
        conversationStore.updateMessage(
          activeConversationId,
          optimisticUserMessage.id,
          streamMessage
        )
        conversationStore.updateMessage(activeConversationId, thinkingMessageId, {
          anchorMessageId: streamMessage.id
        })
      },
      onAgentReplyCreated: ({ message }) => {
        if (!isCurrentDirectReply(controller)) return
        streamedText = ''
        const streamMessage = mergeStreamMessage(thinkingMessage, message, 'agent', 'agent_text')
        conversationStore.updateMessage(activeConversationId, thinkingMessageId, {
          ...streamMessage,
          text: thinkingText,
          thinking: true,
          streaming: true
        })
        agentMessageId = streamMessage.id
        activeStreamAgentMessageId.value = streamMessage.id
        resetStreamWatchdog(activeConversationId, agentMessageId)
      },
      onAgentTextDelta: ({ messageId, delta }) => {
        if (!isCurrentDirectReply(controller)) return
        if (messageId) agentMessageId = messageId
        streamedText += delta
        activeStreamAgentMessageId.value = agentMessageId
        activeStreamHasText.value = streamedText.length > 0
        const shouldScroll = shouldFollowIncomingMessages.value
        const conversationId = activeConversationId
        conversationStore.updateMessage(conversationId, agentMessageId, {
          text: streamedText,
          thinking: false,
          streaming: true
        })
        resetStreamWatchdog(conversationId, agentMessageId)
          scheduleDirectReplyPersistenceCheck({
            controller,
            conversationId,
            streamedMessageId: agentMessageId,
            userMessageId
          })
        scrollToBottomAfterRender(shouldScroll)
      },
      onAgentTextDone: (result) => {
        if (!isCurrentDirectReply(controller)) return
        receivedTerminalEvent = true
        finishDirectReply(controller)
        scheduleGeneratedTitleRefresh(activeConversationId)
        const shouldScroll = shouldFollowIncomingMessages.value
        if (!result.agent_reply) {
          conversationStore.updateMessage(activeConversationId, agentMessageId, {
            text: streamedText,
            thinking: false,
            streaming: false
          })
          void contextUsage.refresh('chat-completed')
          scrollToBottomAfterRender(shouldScroll)
          return
        }
        finalizeDirectReplyMessage(activeConversationId, agentMessageId, result.agent_reply)
        void contextUsage.refresh('chat-completed')
        scrollToBottomAfterRender(shouldScroll)
      },
      onAgentTaskCreated: (result) => {
        if (!isCurrentDirectReply(controller)) return
        receivedTerminalEvent = true
        scheduleGeneratedTitleRefresh(activeConversationId)
        // Phase 2.9A.11 + UI fix: task 已创建并连接 SSE 后,
        // 保留 thinking 消息(不清除),让它在第一个 SSE 事件到达时
        // 再被移除。这样避免 task_created 和第一个 tool_started
        // 之间的空白期,用户始终看到"正在思考..."直到有实际内容出现。
        //
        // 唯一需要做的是重置 thinkingMessageId 供后续清理使用;
        // 真正的移除由 connectTaskEvents 的第一个事件触发。
        finishDirectReply(controller)
        if (result.conversation) {
          conversationStore.mergeConversationSummary(result.conversation)
        }
        if (result.agent_task) {
          const liveTask = {
            ...result.agent_task,
            triggerMessageId: result.agent_task.triggerMessageId ?? result.message.id
          }
          conversationStore.setLatestTask(activeConversationId, liveTask)
          conversationStore.setConversationState(activeConversationId, 'planning')
          connectTaskEvents(activeConversationId, liveTask.events_url, liveTask.task_id, thinkingMessageId)
        }
      },
      onError: (message) => {
        if (!isCurrentDirectReply(controller)) return
        receivedTerminalEvent = true
        finishDirectReply(controller)
        scheduleGeneratedTitleRefresh(activeConversationId)
        conversationStore.updateMessage(activeConversationId, agentMessageId, {
          type: 'error',
          role: 'agent',
          text: message,
          thinking: false,
          streaming: false
        })
        clearActiveStream()
      }
    }, controller.signal, knowledgeModeSnapshot)
    transportCompletedNormally = true
  } catch (err) {
    clearStreamWatchdog()
    if (err instanceof DOMException && err.name === 'AbortError') return
    if (activeConversationId && agentMessageId) {
      conversationStore.updateMessage(activeConversationId, agentMessageId, {
        type: 'error',
        role: 'agent',
        text: err instanceof Error ? err.message : '消息发送失败',
        thinking: false,
        streaming: false
      })
    }
    toast.error(err instanceof Error ? err.message : '消息发送失败')
  } finally {
    // A proxy, browser or decoding edge case may allow the HTTP stream to end
    // after the backend has persisted the reply but before this component
    // handles `agent_text_done`.  Do not leave a visual "streaming" row or a
    // Stop button behind in that case: reconcile from the persisted timeline,
    // which is the authoritative terminal state.
    if (
      transportCompletedNormally
      && !receivedTerminalEvent
      && isCurrentDirectReply(controller)
      && activeConversationId
    ) {
      await reconcileDirectReplyAfterTransportEnd({
        conversationId: activeConversationId,
        streamedMessageId: agentMessageId,
        userMessageId,
        streamedText
      })
    }
    finishDirectReply(controller)
  }
}

function isCurrentDirectReply(controller: AbortController): boolean {
  return activeStreamController.value === controller && !controller.signal.aborted
}

function finishDirectReply(controller: AbortController): boolean {
  if (activeStreamController.value !== controller) return false
  clearActiveStream()
  return true
}

function scheduleDirectReplyPersistenceCheck(input: {
  controller: AbortController
  conversationId: string
  streamedMessageId: string
  userMessageId: string
}, attempt = 0): void {
  clearDirectReplyPersistenceCheck()
  // The server persists the full assistant message only after its final text
  // delta.  This is a second terminal authority for the direct-reply
  // protocol: if a browser/proxy drops the final SSE frame, the persisted
  // message still closes the UI without waiting for a refresh or timeout.
  directReplyPersistenceTimer = setTimeout(() => {
    directReplyPersistenceTimer = null
    if (!isCurrentDirectReply(input.controller)) return
    void reconcilePersistedDirectReply(input).then((reconciled) => {
      if (!isCurrentDirectReply(input.controller)) return
      if (!reconciled) {
        // The message write is asynchronous to the final delta.  Poll the
        // authoritative persisted record a few times so a read/write race
        // cannot strand the composer in "replying" state.
        if (attempt < 4) scheduleDirectReplyPersistenceCheck(input, attempt + 1)
        return
      }
      finishDirectReply(input.controller)
      scheduleGeneratedTitleRefresh(input.conversationId)
      void contextUsage.refresh('chat-completed')
      void scrollToBottomAfterRender(true)
    })
  }, 750)
}

async function reconcilePersistedDirectReply(input: {
  conversationId: string
  streamedMessageId: string
  userMessageId: string
}): Promise<boolean> {
  try {
    const persistedMessages = await messageApi.fetchMessages(input.conversationId)
    const persistedReply = persistedMessages.find((message) => message.id === input.streamedMessageId)
      ?? persistedMessages.find((message) =>
        message.role === 'agent'
        && message.type === 'agent_text'
        && !!input.userMessageId
        && message.replyToMessageId === input.userMessageId
      )
    if (!persistedReply) return false
    finalizeDirectReplyMessage(input.conversationId, input.streamedMessageId, persistedReply)
    return true
  } catch {
    return false
  }
}

async function reconcileDirectReplyAfterTransportEnd(input: {
  conversationId: string
  streamedMessageId: string
  userMessageId: string
  streamedText: string
}): Promise<void> {
  if (await reconcilePersistedDirectReply({
    conversationId: input.conversationId,
    streamedMessageId: input.streamedMessageId,
    userMessageId: input.userMessageId
  })) {
    void contextUsage.refresh('chat-completed')
    return
  }

  conversationStore.updateMessage(input.conversationId, input.streamedMessageId, {
    text: input.streamedText,
    thinking: false,
    streaming: false,
    optimistic: false
  })
}

function finalizeDirectReplyMessage(
  conversationId: string,
  streamedMessageId: string,
  finalReply: ChatMessage
): void {
  const finalMessage: ChatMessage = {
    ...finalReply,
    role: 'agent',
    type: 'agent_text',
    thinking: false,
    streaming: false,
    regenerating: false,
    optimistic: false
  }
  const canonicalMessageId = finalMessage.id || streamedMessageId
  if (conversationStore.updateMessage(conversationId, canonicalMessageId, finalMessage)) return
  if (
    canonicalMessageId !== streamedMessageId
    && conversationStore.updateMessage(conversationId, streamedMessageId, finalMessage)
  ) return

  // A stale history restore must never turn a completed reply into a hidden
  // streaming placeholder.  The terminal payload is authoritative, so insert
  // it when the optimistic row was replaced before this final state arrived.
  conversationStore.appendMessages(conversationId, [finalMessage])
}

function clearActiveStream() {
  clearStreamWatchdog()
  activeStreamController.value = null
  activeStreamConversationId.value = ''
  activeStreamAgentMessageId.value = ''
  activeStreamHasText.value = false
}

async function stopActiveWork(): Promise<void> {
  const conversationId = activeStreamConversationId.value
  const messageId = activeStreamAgentMessageId.value
  const hasText = activeStreamHasText.value
  clearStreamWatchdog()
  activeStreamController.value?.abort()
  if (conversationId && messageId) {
    if (hasText) {
      conversationStore.updateMessage(conversationId, messageId, {
        thinking: false,
        streaming: false
      })
    } else {
      conversationStore.removeMessage(conversationId, messageId)
    }
  }
  clearActiveStream()

  // Agent task cancellation is acknowledged before a replacement send starts.
  const taskId = props.conversation.latestTask?.task_id
  if (taskId && !isTerminalTaskStatus(props.conversation.latestTask?.status)) {
    const cancelledTask = await agentApi.cancelTask(taskId, '用户停止')
    const targetConversationId = conversationId || props.conversation.id
    conversationStore.updateLatestTaskStatus(targetConversationId, 'cancelled', {
      taskId: cancelledTask.task_id,
      completedAt: new Date().toISOString()
    })
    conversationStore.setConversationState(targetConversationId, 'failed')
    conversationStore.removeTaskThinkingPlaceholders(targetConversationId)
    pendingConfirmationRetryAbort.value?.abort()
    _autoSseConnectedTaskId.value = ''
    sse.disconnect()
  }
}

function handleStopResponse() {
  void stopActiveWork().catch((err) => {
    toast.error(err instanceof Error ? err.message : '停止任务失败')
  })
}

function handleContextPopoverOpen() {
  void contextUsage.refresh('popover-open')
}

function handleDraftChange(value: string) {
  draftText.value = value
  if (contextPreviewTimer) clearTimeout(contextPreviewTimer)
  const conversationId = props.conversation.id
  if (conversationId === 'conv_new') return
  contextPreviewTimer = setTimeout(() => {
    contextPreviewTimer = null
    if (props.conversation.id !== conversationId) return
    void contextUsage.preview({
      content: value,
      attached_file_ids: props.conversation.draftFiles.map((file) => file.id)
    })
  }, CONTEXT_PREVIEW_DEBOUNCE_MS)
}

async function handleToggleKnowledgeMode() {
  try {
    const activeConversation = await conversationStore.ensureActiveConversation(props.conversation.id)
    const confirmedMode = await conversationStore.toggleKnowledgeMode(activeConversation.id)
    toast.success(
      confirmedMode === 'MAAS_STRICT'
        ? '已开启知识库回答'
        : '已关闭知识库回答'
    )
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '知识库模式切换失败')
  }
}

function handleCompactContext() {
  void contextUsage.compactContext()
}

function handleRemoveFile(fileId: string) {
  conversationStore.removeDraftFile(props.conversation.id, fileId)
}

function handleOpenFile(file: FileAttachment) {
  if (!canPreviewFileAttachment(file)) return
  void router.push(buildChatFilePreviewLocation(
    file,
    props.conversation.id,
    props.conversation.title
  ))
}

async function loadTemplatePicker() {
  try { await templateStore.loadMine() } catch { /* dialog renders the store error */ }
}

function openTemplatePicker() {
  libraryPickerVisible.value = false
  templatePickerVisible.value = true
  void loadTemplatePicker()
}

function openTemplateMarket() {
  templatePickerVisible.value = false
  libraryPickerVisible.value = false
  void router.push('/templates')
}

async function loadLibraryPicker() {
  libraryPickerLoading.value = true
  libraryPickerError.value = ''
  try {
    const result = await fetchLibraryItems('file', '', { scope: 'active', pageSize: 100 })
    libraryPickerItems.value = result.items
  } catch (error) {
    libraryPickerError.value = error instanceof Error ? error.message : '资料库加载失败，请稍后重试。'
  } finally {
    libraryPickerLoading.value = false
  }
}

function openLibraryPicker() {
  templatePickerVisible.value = false
  libraryPickerVisible.value = true
  void loadLibraryPicker()
}

function closeLibraryPicker() {
  if (libraryAttachingItemId.value) return
  libraryPickerVisible.value = false
}

function fileTypeForLibraryItem(item: LibraryItem): FileType {
  return item.extension.toLowerCase() === '.docx' ? 'supplemental_doc' : 'unknown'
}

async function attachLibraryItem(item: LibraryItem) {
  if (libraryAttachingItemId.value) return
  libraryAttachingItemId.value = item.id
  try {
    const conversation = await conversationStore.ensureActiveConversation(props.conversation.id)
    const { blob, filename } = await fetchLibraryItemBlob(item)
    const sourceFile = new File([blob], filename || item.name, {
      type: item.mimeType || blob.type || 'application/octet-stream'
    })
    const uploaded = await fileStore.uploadFile(sourceFile, conversation.id, fileTypeForLibraryItem(item))
    conversationStore.addDraftFile(conversation.id, uploaded)
    libraryPickerVisible.value = false
    toast.success('资料库文件已加入当前对话，请确认后发送。')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '资料库文件添加失败，请稍后重试。')
  } finally {
    libraryAttachingItemId.value = ''
  }
}

async function attachTemplate(item: UserTemplateItem) {
  try {
    const conversation = await conversationStore.ensureActiveConversation(props.conversation.id)
    const result = await useTemplate(item.id, conversation.id)
    conversationStore.mergeConversationSummary(result.conversation)
    conversationStore.addDraftFile(result.conversation.id, result.uploadedFile)
    templatePickerVisible.value = false
    toast.success('模板已加入当前对话，请确认后发送。')
  } catch (error) {
    toast.error(error instanceof Error ? error.message : '模板添加失败，请稍后重试。')
  }
}

async function handleConfirmSections(payload: {
  messageId: string
  taskId: string
  confirmationId?: string
  confirmationType?: string
  sections: SectionItem[]
}) {
  const activeConversation = conversationStore.activeConversation
  const sourceMessage = findConversationMessage(activeConversation, payload.messageId)
  if (
    activeConversation.id !== props.conversation.id
    || sourceMessage?.type !== 'section_confirm'
    || sourceMessage.taskId !== payload.taskId
    || sourceMessage.confirmed
  ) {
    toast.warning('确认信息已失效，请刷新后重试。')
    return
  }
  try {
    conversationStore.updateMessage(activeConversation.id, payload.messageId, { confirming: true })
    const confirmedTask = await agentApi.confirmTask(payload.taskId, payload.sections, {
      confirmationId: payload.confirmationId,
      confirmationType: payload.confirmationType
    })
    conversationStore.updateMessage(activeConversation.id, payload.messageId, {
      confirmed: true,
      confirming: false,
      sections: payload.sections,
      confirmationReceipt: buildSectionConfirmationReceipt(payload.sections)
    })
    conversationStore.setConversationState(activeConversation.id, 'planning')
    conversationStore.setLatestTask(activeConversation.id, confirmedTask)
    // Phase 2.9A.28: 确认后不再调 restoreConversation。
    // 旧代码 restoreConversation 用 event-list 快照 merge → 状态倒退到 RequirementParser。
    // 新流程：确认后只建立 post-confirm SSE，后续事件增量 merge，不全量覆盖。
    // fix-3: 章节确认后不再有新的 thinking 占位(用户正在交互),无需传 placeholderMessageId
    _autoSseConnectedTaskId.value = '' // 重置，让 watch 重新建立 post-confirm 连接
    connectTaskEvents(activeConversation.id, confirmedTask.events_url, confirmedTask.task_id)
  } catch (err) {
    conversationStore.updateMessage(activeConversation.id, payload.messageId, { confirming: false })
    toast.error(err instanceof Error ? err.message : '章节确认提交失败')
  }
}

async function handlePreparationClarification(payload: {
  messageId: string
  taskId: string
  answers: Record<string, string>
  conservativeGapIds: string[]
}) {
  const activeConversation = conversationStore.activeConversation
  if (activeConversation.id !== props.conversation.id) return
  const clarification = findConversationMessage(activeConversation, payload.messageId)?.clarification
  try {
    conversationStore.updateMessage(activeConversation.id, payload.messageId, { confirming: true })
    const response = await agentApi.submitPreparationClarification(
      payload.taskId,
      payload.answers,
      payload.conservativeGapIds
    )
    conversationStore.updateMessage(activeConversation.id, payload.messageId, {
      confirmed: true,
      confirming: false,
      confirmationReceipt: buildPreparationClarificationReceipt(
        clarification,
        payload.answers,
        payload.conservativeGapIds
      )
    })
    conversationStore.setConversationState(activeConversation.id, 'planning')
    connectTaskEvents(
      activeConversation.id,
      response.events_url ?? `/api/agent/tasks/${payload.taskId}/events`,
      payload.taskId
    )
    toast.success('补充信息已提交，正在重新评估需求。')
  } catch (err) {
    conversationStore.updateMessage(activeConversation.id, payload.messageId, { confirming: false })
    toast.error(err instanceof Error ? err.message : '补充信息提交失败')
  }
}

function handlePendingPreparationClarification(payload: {
  answers: Record<string, string>
  conservativeGapIds: string[]
}) {
  const message = pendingPreparationClarification.value
  if (!message?.taskId) return
  void handlePreparationClarification({
    messageId: message.id,
    taskId: message.taskId,
    answers: payload.answers,
    conservativeGapIds: payload.conservativeGapIds
  })
}

function handlePendingSectionConfirmation(sections: SectionItem[]) {
  const message = pendingSectionConfirmation.value
  if (!message?.taskId) return
  void handleConfirmSections({
    messageId: message.id,
    taskId: message.taskId,
    confirmationId: message.confirmationId,
    confirmationType: message.confirmationType,
    sections
  })
}

function handlePendingFormatLossDecision(decision: 'accept' | 'retry') {
  const message = pendingFormatLossConfirmation.value
  if (!message?.taskId) return
  formatLossDecisionError.value = ''
  void handleFormatLossDecision({ taskId: message.taskId, decision })
}

function handleClarificationCardLayoutChange(expanded: boolean) {
  if (expanded) void scrollToBottomAfterRender(true)
}

function handleComposerDockLayout(layout: { panelTopOffset: number }) {
  if (Number.isFinite(layout.panelTopOffset) && layout.panelTopOffset > 0) {
    composerPanelTopOffset.value = layout.panelTopOffset
  }
}

// Phase 2.9A.30: controlled collapse from AgentRunCard
function handleTaskCollapsed(payload: { taskId: string; collapsed: boolean }) {
  conversationStore.setTaskRunCollapsed(props.conversation.id, payload.taskId, payload.collapsed, { userInitiated: true })
}

async function handleDownloadArtifact(artifactId: string, fallbackName?: string) {
  try {
    await artifactApi.downloadArtifact(artifactId, fallbackName)
    toast.success('产物下载已开始')
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '产物下载失败')
  }
}

async function handleRetryTask(taskId: string) {
  try {
    const retried = await agentApi.retryTask(taskId, 'from_failed_step')
    conversationStore.setLatestTask(props.conversation.id, retried)
    conversationStore.setConversationState(props.conversation.id, 'planning')
    // Phase 2.9A.23:thinking 卡死修复 — 同 handleConfirmSections:
    // 重试后也立即重建消息流,不依赖 /events 实时推流。
    void conversationStore.restoreConversation(props.conversation.id)
    connectTaskEvents(props.conversation.id, retried.events_url, retried.task_id)
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '任务重试失败')
  }
}

function findLatestFormatLossMessage(conversation: Conversation): ChatMessage | undefined {
  const taskBlockMessages = (conversation.taskRunBlocks ?? []).flatMap((block) => block.messages)
  return [...conversation.messages, ...taskBlockMessages]
    .reverse()
    .find((message) => message.formatLoss && !message.confirmed)
}

function findLatestPreparationClarificationMessage(conversation: Conversation): ChatMessage | undefined {
  const taskBlockMessages = (conversation.taskRunBlocks ?? []).flatMap((block) => block.messages)
  return [...conversation.messages, ...taskBlockMessages]
    .reverse()
    .find((message) => message.type === 'preparation_clarification' && !!message.clarification && !message.confirmed)
}

function findConversationMessage(conversation: Conversation, messageId: string): ChatMessage | undefined {
  const taskBlockMessages = (conversation.taskRunBlocks ?? []).flatMap((block) => block.messages)
  return [...conversation.messages, ...taskBlockMessages].find((message) => message.id === messageId)
}

function buildSectionConfirmationReceipt(sections: SectionItem[]) {
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

function buildPreparationClarificationReceipt(
  clarification: ChatMessage['clarification'] | undefined,
  answers: Record<string, string>,
  conservativeGapIds: string[]
) {
  const cards = clarification?.cards ?? []
  const answerLines = cards
    .map((card) => {
      const answer = answers[card.id]?.trim()
      if (answer) return `- **${truncateReceiptText(card.question, 34)}：** ${truncateReceiptText(answer, 72)}`
      if (conservativeGapIds.includes(card.id)) return `- **${truncateReceiptText(card.question, 34)}：** 按保守范围生成`
      return ''
    })
    .filter(Boolean)
  return {
    kind: 'clarification' as const,
    markdown: answerLines.length ? answerLines.join('\n') : '- **补充结果：** 已提交'
  }
}

function buildFormatLossConfirmationReceipt(
  decision: 'accept' | 'retry',
  losses: NonNullable<ChatMessage['formatLoss']>['losses']
) {
  const selection = decision === 'retry'
    ? '重新生成文档并再次校验格式'
    : '接受格式丢失并继续导出'
  const lossDetails = losses
    .map((loss) => loss.message?.trim() || loss.element?.trim())
    .filter((loss): loss is string => Boolean(loss))
    .join('；')
  return {
    kind: 'format_loss' as const,
    markdown: [
      `- **处理方式：** ${selection}`,
      `- **检测到的格式丢失：** ${lossDetails || '检测到的格式项已记录'}`,
      `- **后续流程：** ${decision === 'retry' ? '正在重新生成文档并再次校验格式。' : '已按你的选择继续完成导出流程。'}`
    ].join('\n')
  }
}

function formatReceiptSectionName(section: SectionItem): string {
  const code = section.code.trim()
  const title = section.title.trim()
  if (!code) return title
  return title.startsWith(code) ? title : `${code} ${title}`.trim()
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

function findLatestSectionConfirmMessage(conversation: Conversation): ChatMessage | undefined {
  const taskBlockMessages = (conversation.taskRunBlocks ?? []).flatMap((block) => block.messages)
  return [...conversation.messages, ...taskBlockMessages]
    .reverse()
    .find((message) => message.type === 'section_confirm' && !!message.sections?.length && !message.confirmed)
}

async function handleFormatLossDecision(payload: {
  taskId: string
  decision: 'accept' | 'retry'
  note?: string
}) {
  formatLossDecisionError.value = ''
  const task = props.conversation.latestTask
  if (!task || task.task_id !== payload.taskId) {
    toast.warning('任务状态已变化，无法提交该决定。')
    return
  }
  // Find the format-loss message in this conversation and mark it as
  // submitting so the UI flips into a "processing" state immediately.
  const lossMessage = findLatestFormatLossMessage(props.conversation)
  if (lossMessage) {
    conversationStore.updateMessage(props.conversation.id, lossMessage.id, {
      confirming: true,
    })
  }
  try {
    await agentTaskStore.submitFormatLossDecision(payload.taskId, payload.decision, payload.note)
    // Preserve the user's selection in place. The reducer replaces this
    // optimistic receipt with the durable SSE receipt once it arrives.
    if (lossMessage) {
      conversationStore.updateMessage(props.conversation.id, lossMessage.id, {
        confirmed: true,
        confirming: false,
        formatLoss: undefined,
        confirmationReceipt: buildFormatLossConfirmationReceipt(
          payload.decision,
          lossMessage.formatLoss?.losses ?? []
        )
      })
    }
    // Reconnect only the task stream so post-decision progress continues;
    // do not hydrate the whole conversation and trigger the page loader.
    connectTaskEvents(props.conversation.id, task.events_url, task.task_id)
    // Phase 2.9A.23:thinking 卡死修复 — 格式损失决策后立即重建消息流,
    // 同 handleConfirmSections / handleRetryTask。
    toast.success(
      payload.decision === 'accept'
        ? '已接受格式丢失，继续导出。'
        : '已请求重新生成文档。'
    )
  } catch (err) {
    const message = err instanceof Error ? err.message : '格式丢失决定提交失败'
    formatLossDecisionError.value = message
    toast.error(message)
  }
}

function handleToast(payload: { kind: 'success' | 'error'; text: string }) {
  if (payload.kind === 'success') toast.success(payload.text)
  else toast.error(payload.text)
}

/**
 * Phase 2.9A.26: feedback-change wired to PUT /api/messages/{id}/feedback.
 * Optimistic update → API call → confirm or rollback.
 * Uses last-write-wins: rapid clicks are serialized by `feedbackPending`
 * flag — only one in-flight request per message at a time.
 */
async function handleFeedbackChange(payload: {
  message: ChatMessage
  feedback: 'like' | 'dislike' | null
}) {
  const { message, feedback } = payload
  const oldFeedback = message.feedback ?? null
  const conversationId = props.conversation.id

  // Debounce: skip if the same request is already in-flight
  if (message.feedbackPending) return

  // 1) Optimistic local update
  conversationStore.updateMessage(conversationId, message.id, {
    feedback,
    feedbackPending: true,
  })

  try {
    // 2) Persist to backend
    const result = await messageApi.setMessageFeedback(message.id, feedback)

    // 3) Confirm: reconcile with server-confirmed value
    conversationStore.updateMessage(conversationId, message.id, {
      feedback: result.feedback,
      feedbackPending: false,
    })
    if (result.feedback) toast.success('感谢反馈')
  } catch (err) {
    // 4) Rollback on failure: restore the previous value
    conversationStore.updateMessage(conversationId, message.id, {
      feedback: oldFeedback,
      feedbackPending: false,
    })
    toast.error(err instanceof Error ? err.message : '反馈提交失败，请稍后重试')
  }
}

/**
 * Phase 2.9A.26: regenerate-message full implementation.
 *
 * Flow:
 *  1. Save old state (content, feedback) for rollback on failure
 *  2. Set `regenerating: true` on the target message → UI hides buttons,
 *     shows "正在思考..." thinking indicator
 *  3. Call regenerateMessageStream → SSE delivers deltas to the same message
 *  4. On agent_text_done: clear regenerating flag, content is already updated
 *  5. On failure / abort: restore old state, show error toast
 */
async function handleRegenerateMessage(message: ChatMessage) {
  const conversationId = props.conversation.id

  // Reject if the message belongs to an Agent task
  if (message.taskId) {
    toast.warning('当前消息属于 Agent 任务，无法重新生成。')
    return
  }

  // Debounce: ignore if already regenerating
  if (message.regenerating) return

  // 1) Save old state for rollback
  const oldText = message.text ?? ''
  const oldFeedback = message.feedback ?? null

  // 2) Set regenerating flag → thinking UI
  conversationStore.updateMessage(conversationId, message.id, {
    regenerating: true,
    text: '',
    thinking: true,
    streaming: true,
    feedback: null,
    feedbackPending: false,
  })
  await scrollToBottomAfterRender(true)

  const controller = new AbortController()
  activeStreamController.value = controller

  let accumulatedText = ''

  try {
    await messageApi.regenerateMessageStream(message.id, {
      onAgentTextDelta: ({ delta }) => {
        accumulatedText += delta
        conversationStore.updateMessage(conversationId, message.id, {
          text: accumulatedText,
          thinking: false,
          streaming: true,
        })
        const shouldScroll = shouldFollowIncomingMessages.value
        scrollToBottomAfterRender(shouldScroll)
      },
      onAgentTextDone: ({ text: finalText }) => {
        // Final content update — the SSE delta may have already set it,
        // but the done event has the canonical final value.
        conversationStore.updateMessage(conversationId, message.id, {
          text: finalText || accumulatedText,
          thinking: false,
          streaming: false,
          regenerating: false,
          feedback: null, // new generation starts with no feedback
          feedbackPending: false,
        })
        scrollToBottomAfterRender(true)
      },
      onError: (errorMsg) => {
        // Rollback to old state
        conversationStore.updateMessage(conversationId, message.id, {
          text: oldText,
          thinking: false,
          streaming: false,
          regenerating: false,
          feedback: oldFeedback,
          feedbackPending: false,
        })
        toast.error(errorMsg || '重新生成失败，请稍后重试')
      }
    }, controller.signal)
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') return
    // Rollback on unexpected error
    conversationStore.updateMessage(conversationId, message.id, {
      text: oldText,
      thinking: false,
      streaming: false,
      regenerating: false,
      feedback: oldFeedback,
      feedbackPending: false,
    })
    toast.error(err instanceof Error ? err.message : '重新生成失败')
  } finally {
    if (activeStreamController.value === controller) {
      activeStreamController.value = null
    }
  }
}
</script>

<style scoped>
.chat-workspace {
  --chat-input-safe-area: 360px;
  --welcome-input-safe-area: 220px;
  --chat-composer-panel-top-offset: 160px;
  --floating-interaction-gap: 12px;

  position: relative;
  display: flex;
  flex: 1;
  min-width: 0;
  height: 100%;
  overflow: hidden;
  background: var(--ta-surface);
}

.chat-project-link {
  position: absolute;
  top: 12px;
  left: 16px;
  z-index: 4;
  display: flex;
  gap: 8px;
  align-items: center;
  max-width: min(320px, calc(100% - 32px));
  height: 48px;
  padding: 0 14px;
  overflow: hidden;
  color: var(--ta-text-strong);
  background: var(--ta-surface);
  border: 0;
  border-radius: var(--ta-radius-md);
  transition: background 160ms ease;
}

.chat-project-link:hover,
.chat-project-link:focus-visible {
  background: var(--ta-surface-low);
  outline: none;
}

.chat-project-link__icon {
  flex: 0 0 18px;
  width: 18px;
  height: 18px;
  object-fit: contain;
  opacity: 0.78;
}

.chat-project-link__name {
  overflow: hidden;
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-scroll {
  position: relative;
  box-sizing: border-box;
  flex: 1;
  height: 100%;
  padding: 32px 24px var(--chat-input-safe-area);
  scroll-padding-bottom: var(--chat-input-safe-area);
  overflow-y: auto;
  background: var(--ta-surface);
}

/* Keep the native vertical thumb easy to acquire on long conversations.
   Its width remains the global 8px setting; only the minimum thumb height
   changes for this chat-specific scroll container. */
.chat-scroll::-webkit-scrollbar-thumb:vertical {
  min-height: 56px;
}

.chat-scroll--with-project {
  padding-top: 88px;
}

.chat-scroll--floating-confirmation {
  padding-bottom: 640px;
  scroll-padding-bottom: 640px;
}

.chat-scroll__content {
  width: 100%;
  min-width: 0;
}

.chat-scroll--welcome {
  display: flex;
  align-items: center;
  justify-content: center;
  padding-bottom: var(--welcome-input-safe-area);
  overflow: hidden;
}

.chat-workspace__clarification,
.chat-workspace__section-confirmation,
.chat-workspace__format-loss-confirmation {
  position: absolute;
  z-index: 11;
  bottom: calc(var(--chat-composer-panel-top-offset) + var(--floating-interaction-gap));
  left: 50%;
  width: min(calc(100% - 48px), 780px);
  max-height: min(52vh, 470px);
  overflow-y: auto;
  transform: translateX(-50%);
}

.task-controls {
  display: flex;
  justify-content: center;
  width: min(100%, var(--ta-wide-card-width));
  padding: 12px 0 4px;
  margin: 0 auto;
  pointer-events: none;
}

.task-controls__button {
  height: 32px;
  padding: 0 12px;
  color: var(--ta-text-muted);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  pointer-events: auto;
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
  border-radius: var(--ta-radius-sm);
  box-shadow: var(--ta-shadow-soft);
}

.task-controls__button:hover {
  color: var(--ta-error);
  border-color: rgba(186, 26, 26, 0.24);
}

@media (max-width: 860px) {
  .chat-workspace {
    --chat-input-safe-area: 320px;
    --welcome-input-safe-area: 190px;
  }

  .chat-scroll {
    padding: 24px 16px var(--chat-input-safe-area);
  }

  .chat-workspace__clarification,
  .chat-workspace__section-confirmation,
  .chat-workspace__format-loss-confirmation {
    bottom: calc(var(--chat-composer-panel-top-offset) + var(--floating-interaction-gap));
    width: calc(100% - 32px);
    max-height: 50vh;
  }
}
</style>
