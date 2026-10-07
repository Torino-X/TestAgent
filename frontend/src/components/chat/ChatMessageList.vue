<template>
  <div class="message-list">
    <article
      v-for="item in conversationItems"
      :key="item.id"
      class="message"
      :class="{
        'message-row--user': item.kind === 'user',
        'message-row--assistant': item.kind === 'agent',
        'message-row--agent-run': item.kind === 'agent-run'
      }"
    >
      <template v-if="item.kind === 'user'">
        <div class="user-bubble">{{ item.message.text }}</div>
        <div v-if="item.message.files?.length" class="user-files">
          <FileAttachmentCard
            v-for="file in item.message.files"
            :key="file.id"
            :file="file"
            compact
            @open="(file) => emit('open-file', file)"
          />
        </div>
      </template>

      <template v-else-if="item.kind === 'agent-run'">
        <AgentRunCard
          :messages="item.messages"
          :conversation-id="props.conversationId"
          :conversation-title="props.conversationTitle"
          :task="taskForTaskId(item.taskId, item.block)"
          :collapsed="item.block?.collapsed"
          :dynamic-narratives="item.block?.dynamicNarratives ?? []"
          :tool-narratives="item.block?.toolNarratives ?? []"
          :show-tool-narratives="toolCardNarrativeEnabled"
          :task-summary-narrative="item.block?.taskSummaryNarrative ?? null"
          :confirm-sections-ready="props.confirmSectionsReady"
          :section-confirmation-external="props.sectionConfirmationExternal"
          :format-loss-external="props.formatLossExternal"
          :hydrated="props.hydrated"
          @confirm-section="(payload) => emit('confirm-section', payload)"
          @download-artifact="(artifactId, name) => emit('download-artifact', artifactId, name)"
          @retry-task="(taskId) => emit('retry-task', taskId)"
          @format-loss-decision="(payload) => emit('format-loss-decision', payload)"
          @collapse-change="($event: boolean) => (emit as any)('updateTaskCollapsed', { taskId: item.taskId, collapsed: $event })"
        />
      </template>

      <template v-else>
        <div v-if="isMarkdownAgentText(item.message) && item.message.thinking" class="thinking-status" role="status">
          <span class="thinking-indicator" aria-hidden="true"></span>
          <span class="thinking-text">正在思考...</span>
        </div>
        <template v-else-if="isMarkdownAgentText(item.message)">
          <KbSourceBadge v-if="item.message.kbDirectAnswer" :kb="item.message.kbDirectAnswer" />
          <MarkdownMessage :content="item.message.text ?? ''" @toast="emit('toast', $event)" />
          <DocumentCitationBadge
            v-if="item.message.documentCitations?.length"
            :citations="item.message.documentCitations"
          />
          <footer
            v-if="!item.message.streaming && !item.message.regenerating"
            class="message-actions"
            aria-label="回复操作"
          >
            <button
              type="button"
              class="message-action-button"
              data-tooltip="复制回复"
              aria-label="复制回复"
              :disabled="item.message.regenerating"
              :data-action="`copy-${item.message.id}`"
              @click="copyMessage(item.message)"
            >
              <img
                :src="copyIcon"
                :alt="'复制'"
                class="message-action-icon"
                draggable="false"
              />
            </button>
            <button
              type="button"
              class="message-action-button"
              data-tooltip="喜欢"
              aria-label="点赞回复"
              :disabled="item.message.regenerating"
              :aria-pressed="messageFeedback(item.message) === 'like'"
              :class="{ 'message-action-button--active': messageFeedback(item.message) === 'like' }"
              :data-action="`like-${item.message.id}`"
              @click="toggleLike(item.message)"
            >
              <img
                :src="likeIcon"
                :alt="'点赞'"
                class="message-action-icon"
                draggable="false"
              />
            </button>
            <button
              type="button"
              class="message-action-button"
              data-tooltip="不喜欢"
              aria-label="点踩回复"
              :disabled="item.message.regenerating"
              :aria-pressed="messageFeedback(item.message) === 'dislike'"
              :class="{ 'message-action-button--active': messageFeedback(item.message) === 'dislike' }"
              :data-action="`dislike-${item.message.id}`"
              @click="toggleDislike(item.message)"
            >
              <img
                :src="dislikeIcon"
                :alt="'点踩'"
                class="message-action-icon"
                draggable="false"
              />
            </button>
            <button
              type="button"
              class="message-action-button"
              data-tooltip="重新生成"
              aria-label="重新生成回复"
              :disabled="item.message.regenerating"
              :data-action="`reload-${item.message.id}`"
              @click="emit('regenerate-message', item.message)"
            >
              <img
                :src="reloadIcon"
                :alt="'重新生成'"
                class="message-action-icon"
                draggable="false"
              />
            </button>
          </footer>
          <!-- Thinking state shown while regeneration is in progress -->
          <div
            v-if="item.message.regenerating"
            class="thinking-status"
            role="status"
          >
            <span class="thinking-indicator" aria-hidden="true"></span>
            <span class="thinking-text">正在思考...</span>
          </div>
        </template>
        <AgentPlanCard v-else-if="item.message.type === 'agent_plan' && item.message.plan" :steps="item.message.plan" />
        <ToolCallMessage
          v-else-if="item.message.type === 'tool_call' && item.message.toolCall"
          :tool-call="item.message.toolCall"
          :show-public-update="toolCardNarrativeEnabled"
        />
        <RequirementSummaryCard
          v-else-if="item.message.type === 'requirement_summary' && item.message.summary"
          :summary="item.message.summary"
        />
        <TemplateSummaryCard
          v-else-if="item.message.type === 'template_summary' && item.message.summary"
          :summary="item.message.summary"
        />
        <KnowledgeSearchSummaryCard
          v-else-if="item.message.type === 'knowledge_summary' && item.message.summary"
          :summary="item.message.summary"
        />
        <SectionConfirmCard
          v-else-if="item.message.type === 'section_confirm' && item.message.sections && !props.sectionConfirmationExternal"
          :sections="item.message.sections"
          :confirmed="item.message.confirmed"
          :submitting="item.message.confirming"
          @confirm="(sections) => confirmSection(item.message, sections)"
        />
        <GeneratingStatusCard
          v-else-if="item.message.type === 'generating_status' && item.message.generating"
          :status="item.message.generating"
        />
        <ReviewResultCard
          v-else-if="item.message.type === 'review_result' && item.message.review"
          :review="item.message.review"
        />
        <ArtifactDownloadCard
          v-else-if="(item.message.type === 'artifact_download' || item.message.type === 'agent_text') && item.message.artifact"
          :artifact="item.message.artifact"
          :conversation-id="props.conversationId"
          :conversation-title="props.conversationTitle"
          @download="(artifactId, name) => emit('download-artifact', artifactId, name)"
        />
        <ErrorMessageCard
          v-else-if="item.message.type === 'error'"
          :message="item.message.text ?? '任务执行失败。'"
          :retryable="!!item.message.taskId"
          @retry="item.message.taskId && emit('retry-task', item.message.taskId)"
        />
      </template>
    </article>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import AgentRunCard from '@/components/cards/AgentRunCard.vue'
import AgentPlanCard from '@/components/cards/AgentPlanCard.vue'
import ArtifactDownloadCard from '@/components/cards/ArtifactDownloadCard.vue'
import ErrorMessageCard from '@/components/cards/ErrorMessageCard.vue'
import FileAttachmentCard from '@/components/cards/FileAttachmentCard.vue'
import GeneratingStatusCard from '@/components/cards/GeneratingStatusCard.vue'
import KnowledgeSearchSummaryCard from '@/components/cards/KnowledgeSearchSummaryCard.vue'
import KbSourceBadge from '@/components/chat/KbSourceBadge.vue'
import DocumentCitationBadge from '@/components/chat/DocumentCitationBadge.vue'
import MarkdownMessage from '@/components/chat/MarkdownMessage.vue'
import RequirementSummaryCard from '@/components/cards/RequirementSummaryCard.vue'
import ReviewResultCard from '@/components/cards/ReviewResultCard.vue'
import SectionConfirmCard from '@/components/cards/SectionConfirmCard.vue'
import TemplateSummaryCard from '@/components/cards/TemplateSummaryCard.vue'
import ToolCallMessage from '@/components/cards/ToolCallMessage.vue'
import { useSettingsStore } from '@/stores/settingsStore'
import copyIcon from '@/assets/Reply-svg/复制.svg'
import likeIcon from '@/assets/Reply-svg/点赞.svg'
import dislikeIcon from '@/assets/Reply-svg/点踩.svg'
import reloadIcon from '@/assets/Reply-svg/重载.svg'
import { copyMarkdownText } from '@/utils/clipboard'
import type { AgentTask, ChatMessage, FileAttachment, SectionItem, TaskRunBlock } from '@/types'
import { assembleConversationTimeline } from '@/utils/conversationTimeline'
import type { ConversationItem } from '@/utils/conversationTimeline'

const settingsStore = useSettingsStore()
const props = defineProps<{
  messages: ChatMessage[]
  conversationId?: string
  conversationTitle?: string
  taskRunBlocks?: TaskRunBlock[]
  confirmSectionsReady?: boolean
  sectionConfirmationExternal?: boolean
  formatLossExternal?: boolean
  tasks?: AgentTask[]
  latestTask?: AgentTask | null
  hydrated?: boolean
}>()

const toolCardNarrativeEnabled = computed(() => settingsStore.toolCardNarrativeEnabled)

const emit = defineEmits<{
  'confirm-section': [
    payload: {
      messageId: string
      taskId: string
      confirmationId?: string
      confirmationType?: string
      sections: SectionItem[]
    }
  ]
  'download-artifact': [artifactId: string, fallbackName?: string]
  'open-file': [file: FileAttachment]
  'retry-task': [taskId: string]
  'format-loss-decision': [
    payload: {
      taskId: string
      decision: 'accept' | 'retry'
      note?: string
    }
  ]
  'regenerate-message': [message: ChatMessage]
  'feedback-change': [
    payload: { message: ChatMessage; feedback: 'like' | 'dislike' | null }
  ]
  'updateTaskCollapsed': [payload: { taskId: string; collapsed: boolean }]
  'toast': [payload: { kind: 'success' | 'error'; text: string }]
}>()

function taskForTaskId(taskId?: string, block?: TaskRunBlock): AgentTask | null {
  const task = taskId
    ? props.tasks?.find((candidate) => candidate.task_id === taskId) ??
      (props.latestTask?.task_id === taskId ? props.latestTask : null)
    : props.latestTask ?? null

  if (!block) return task

  return {
    ...(task ?? {
      task_id: block.taskId,
      task_type: 'test_plan_generation' as const,
      status: block.status,
      events_url: `/api/agent/tasks/${block.taskId}/events`
    }),
    status: block.status ?? task?.status ?? 'running',
    startedAt: block.startedAt ?? task?.startedAt ?? null,
    completedAt: block.completedAt ?? task?.completedAt ?? null,
    durationMs: block.durationMs ?? task?.durationMs ?? null,
    triggerMessageId: block.triggerMessageId ?? task?.triggerMessageId ?? null,
    review_result: block.reviewResult ?? task?.review_result
  }
}

function isMarkdownAgentText(message: ChatMessage) {
  return message.role === 'agent' && !message.taskId && message.type === 'agent_text'
}

// Phase 2.9A.30: use assembler — no more scanning task messages
const conversationItems = computed<ConversationItem[]>(() => {
  return assembleConversationTimeline(
    props.messages,
    props.taskRunBlocks ?? [],
    props.tasks ?? []
  )
})

function confirmSection(message: ChatMessage, sections: SectionItem[]) {
  if (!message.taskId) return
  emit('confirm-section', {
    messageId: message.id,
    taskId: message.taskId,
    confirmationId: message.confirmationId,
    confirmationType: message.confirmationType,
    sections
  })
}

async function copyMessage(message: ChatMessage) {
  const text = message.text ?? ''
  const result = await copyMarkdownText(text)
  if (result === 'success' || result === 'fallback_success') {
    emit('toast', { kind: 'success', text: '已复制' })
    return
  }
  if (result === 'failed') {
    emit('toast', { kind: 'error', text: '复制失败，请手动复制' })
  }
}

function messageFeedback(message: ChatMessage) {
  return message.feedback ?? null
}

async function toggleLike(message: ChatMessage) {
  const current = messageFeedback(message)
  const next = current === 'like' ? null : 'like'
  emit('feedback-change', { message, feedback: next as 'like' | 'dislike' | null })
}

async function toggleDislike(message: ChatMessage) {
  const current = messageFeedback(message)
  const next = current === 'dislike' ? null : 'dislike'
  emit('feedback-change', { message, feedback: next as 'like' | 'dislike' | null })
}
</script>

<style scoped>
.message-list {
  display: flex;
  flex-direction: column;
  gap: 28px;
  width: min(100%, var(--ta-content-width));
  margin: 0 auto;
}

.message {
  display: flex;
  flex-direction: column;
}

.message-row--user {
  align-items: flex-end;
}

.message-row--assistant,
.message-row--agent-run {
  width: 100%;
}

/* Keep the dialogue cadence directional: only add breathing room before a new user turn. */
.message-row--assistant + .message-row--user,
.message-row--agent-run + .message-row--user {
  margin-top: 24px;
}

.user-bubble {
  max-width: 70%;
  padding: 10px 16px;
  color: var(--text-primary);
  font-size: var(--text-md);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: #f4f4f4;
  border-radius: 20px;
}

.user-files {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: flex-end;
  margin-top: 12px;
}

.thinking-status {
  display: inline-flex;
  gap: 8px;
  align-items: center;
  min-height: 28px;
  margin: 0;
  padding: 0;
  color: #6b6b6b;
  font-size: 14px;
  line-height: 1.5;
}

.thinking-indicator {
  width: 6px;
  height: 6px;
  flex: 0 0 auto;
  background: currentColor;
  border-radius: 50%;
  animation: thinking-pulse 1.2s ease-in-out infinite;
}

.message-actions {
  display: flex;
  gap: 4px;
  align-items: center;
  margin: 12px 0 0 -4px;
  color: #6e6e80;
}

.message-action-button {
  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  padding: 0;
  color: inherit;
  font-size: 18px;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 8px;
  transition: background-color 150ms ease, color 150ms ease;
}

.message-action-button:hover,
.message-action-button:focus-visible {
  color: #101112;
  background: #f7f7f8;
  outline: none;
}

.message-action-button--active {
  color: var(--ta-primary, #0066ff);
  background: var(--ta-primary-fixed, rgba(0, 102, 255, 0.12));
}

.message-action-button--active:hover,
.message-action-button--active:focus-visible {
  color: var(--ta-primary, #0066ff);
  background: var(--ta-primary-fixed, rgba(0, 102, 255, 0.18));
}

.message-action-icon {
  width: 18px;
  height: 18px;
  pointer-events: none;
  object-fit: contain;
}

.message-action-button::after {
  position: absolute;
  top: calc(100% + 4px);
  left: 50%;
  z-index: 1;
  padding: 4px 8px;
  color: #fff;
  font-size: 12px;
  font-weight: 500;
  line-height: 16px;
  white-space: nowrap;
  pointer-events: none;
  content: attr(data-tooltip);
  background: #000;
  border-radius: 6px;
  opacity: 0;
  transform: translate(-50%, -2px);
  transition: opacity 150ms ease, transform 150ms ease;
}

.message-action-button:hover::after,
.message-action-button:focus-visible::after {
  opacity: 1;
  transform: translate(-50%, 0);
}

@keyframes thinking-pulse {
  0%,
  100% {
    opacity: 0.3;
    transform: scale(0.85);
  }

  50% {
    opacity: 1;
    transform: scale(1);
  }
}

@media (prefers-reduced-motion: reduce) {
  .thinking-indicator {
    animation: none;
  }
}

@media (max-width: 760px) {
  .message-list {
    gap: 22px;
  }

  .message-row--assistant + .message-row--user,
  .message-row--agent-run + .message-row--user {
    margin-top: 17px;
  }

  .user-bubble {
    max-width: 100%;
  }
}
</style>
