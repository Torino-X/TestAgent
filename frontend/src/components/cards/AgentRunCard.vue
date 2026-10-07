<template>
  <section class="agent-run-shell" :class="`agent-run-shell--${runState}`">
    <div v-if="hasTaskDuration" class="task-duration-header">
      <button
        v-if="canToggleProcess"
        type="button"
        class="task-duration-toggle"
        :aria-expanded="processExpanded"
        aria-controls="task-process"
        :aria-label="processExpanded ? '收起任务执行流程' : '展开任务执行流程'"
        @click="toggleExpanded"
      >
        <span>{{ taskDurationLabel(runState) }} {{ processedDuration }}</span>
        <n-icon :component="processExpanded ? ChevronDown : ChevronForward" />
      </button>
      <div v-else class="task-duration-live" role="status">{{ taskDurationLabel(runState) }} {{ processedDuration }}</div>
    </div>

    <Transition name="task-process">
      <section v-if="shouldShowProcess" id="task-process" class="task-process" :data-state="runState">
        <div class="task-process__body">
        <ol class="agent-run-timeline">
          <li
            v-for="item in timelineItems"
            :key="item.id"
            class="timeline-item"
            :class="[`timeline-item--${item.status}`, { 'timeline-line': item !== timelineItems[timelineItems.length - 1] }]"
          >
            <span class="timeline-marker">
              <n-icon v-if="item.status === 'done'" :component="Checkmark" />
              <span v-else-if="item.status === 'running'" class="timeline-marker__pulse"></span>
              <span v-else-if="item.status === 'waiting'">Ⅱ</span>
            </span>

            <div class="timeline-content">
              <section
                v-if="item.kind === 'confirmation-receipt'"
                class="confirmation-receipt-card"
              >
                <button
                  type="button"
                  class="confirmation-receipt-card__header"
                  :aria-expanded="!isConfirmationReceiptCollapsed(item.message.id)"
                  :aria-label="isConfirmationReceiptCollapsed(item.message.id) ? '展开用户确认详情' : '收起用户确认详情'"
                  @click="toggleConfirmationReceipt(item.message.id)"
                >
                  <span class="confirmation-receipt-card__header-copy">
                    <span class="confirmation-receipt-card__title">已收到用户确认</span>
                    <span class="confirmation-receipt-card__summary">【SUCCESS】{{ confirmationReceiptLabel(item.message) }}，信息如下：</span>
                  </span>
                  <span class="confirmation-receipt-card__toggle" aria-hidden="true">
                    <n-icon :component="isConfirmationReceiptCollapsed(item.message.id) ? ChevronForward : ChevronDown" />
                  </span>
                </button>
                <div v-if="!isConfirmationReceiptCollapsed(item.message.id)" class="confirmation-receipt-card__details">
                  <div class="confirmation-receipt-card__markdown markdown-body" v-html="confirmationReceiptHtml(item.message)"></div>
                </div>
              </section>
              <div v-else-if="item.kind === 'preparation-clarification'" class="preparation-clarification-waiting">
                <p>正在等待用户确认</p>
                <span>请在输入框上方完成关键信息选择。</span>
              </div>
              <div
                v-else-if="item.kind === 'section-strategy' && sectionMessage && sectionConfirmationExternal"
                class="section-confirmation-waiting"
              >
                <p>正在等待用户确认</p>
                <span>请在输入框上方确认章节处理策略。</span>
              </div>
              <div v-else-if="item.kind === 'section-strategy' && sectionMessage" class="section-strategy-node">
                <header class="section-strategy-node__header">
                  <div class="section-strategy-node__title-line">
                    <h3>确认章节处理策略</h3>
                    <div class="section-strategy-stats">
                      <span
                        v-for="stat in sectionStats"
                        :key="stat.action"
                        class="section-strategy-stat"
                        :class="`section-strategy-stat--${stat.action}`"
                      >
                        {{ stat.label }} {{ stat.count }}
                      </span>
                    </div>
                  </div>
                  <p>
                      已从模板中识别 {{ editableSections.length }} 个章节。
                      <span v-if="userConstraintCount > 0" class="section-strategy-user-hint">
                        其中 <strong>{{ userConstraintCount }}</strong> 个按你的提示词指定
                      </span>
                      。请确认哪些章节由 AI 生成，哪些保留模板原文。
                    </p>

                  <div class="section-strategy-actions">
                    <button
                      type="button"
                      class="section-strategy-actions__generate"
                      :disabled="sectionMessage.confirmed"
                      @click="batchUpdateAction('ai_generate')"
                    >
                      全部 AI 生成
                    </button>
                    <button
                      type="button"
                      class="section-strategy-actions__reset"
                      :disabled="sectionMessage.confirmed"
                      @click="restoreSuggestedActions"
                    >
                      恢复建议
                    </button>
                  </div>
                </header>

                <div class="section-strategy-table-wrap">
                  <table class="section-strategy-table">
                    <thead>
                      <tr>
                        <th>章节名称</th>
                        <th>Agent 建议</th>
                        <th>处理动作</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr
                        v-for="(section, index) in editableSections"
                        :key="`${section.id}_${index}`"
                        :class="{ 'section-row--user-constrained': section.constraintSource === 'user_prompt' }"
                      >
                        <td>
                          <div class="section-name-cell">
                            <strong>{{ displaySectionName(section) }}</strong>
                            <span
                              v-if="section.constraintSource === 'user_prompt'"
                              class="section-user-constraint-chip"
                              title="本章节处理方式来自你的提示词"
                            >
                              <span class="material-symbols-outlined">push_pin</span>
                              用户指定
                            </span>
                          </div>
                        </td>
                        <td>
                          <span class="section-suggestion-chip">{{ actionLabel(section.suggestedAction) }}</span>
                        </td>
                        <td>
                          <AppSelect
                            :model-value="section.action"
                            :options="sectionActionChoices"
                            :disabled="sectionMessage.confirmed || sectionMessage.confirming"
                            compact
                            block
                            aria-label="章节处理动作"
                            @update:model-value="handleSectionActionChange(index, $event)"
                          />
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <footer class="section-strategy-footer section-strategy-actions">
                  <button
                    type="button"
                    :disabled="sectionMessage.confirmed || sectionMessage.confirming || !canSubmitSectionConfirmation"
                    @click="confirmActiveSection"
                  >
                    {{ sectionMessage.confirmed ? '已确认章节策略' : '确认章节策略' }}
                  </button>
                </footer>
              </div>

              <div
                v-else-if="item.kind === 'format-loss' && formatLossExternal"
                class="format-loss-waiting"
              >
                <p>正在等待用户确认</p>
                <span>请确认格式丢失触发方式（建议接受丢失）。</span>
              </div>
              <div
                v-else-if="item.kind === 'format-loss' && item.message.formatLoss"
                class="format-loss-banner"
                role="alert"
              >
                <header class="format-loss-banner__header">
                  <h4>格式丢失确认</h4>
                  <div class="format-loss-banner__badges">
                    <span class="format-loss-banner__badge format-loss-banner__badge--warning">WARNING</span>
                    <span class="format-loss-banner__badge">{{ item.message.formatLoss.lossCount }} 项</span>
                    <span class="format-loss-banner__badge">等待确认</span>
                    <span v-if="formatLossSecondsRemaining !== null" class="format-loss-banner__countdown">
                      {{ formatLossSecondsRemaining }}s
                    </span>
                  </div>
                </header>

                <div class="format-loss-banner__table-wrap">
                  <table class="format-loss-banner__table">
                    <thead>
                      <tr>
                        <th>位置</th>
                        <th>影响</th>
                        <th>建议操作</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-if="item.message.formatLoss.losses.length === 0">
                        <td>格式检查</td>
                        <td>{{ item.message.formatLoss.summary || '检测到用户可见的格式丢失' }}</td>
                        <td>可接受丢失继续导出，或重新生成文档</td>
                      </tr>
                      <tr
                        v-for="(loss, idx) in item.message.formatLoss.losses"
                        :key="`${loss.element}-${idx}`"
                      >
                        <td>{{ formatLossLocation(loss) }}</td>
                        <td>{{ formatLossImpact(loss) }}</td>
                        <td>{{ formatLossSuggestedAction() }}</td>
                      </tr>
                    </tbody>
                  </table>
                </div>

                <footer class="format-loss-banner__footer">
                  <button
                    type="button"
                    class="format-loss-banner__btn format-loss-banner__btn--retry"
                    :disabled="submittingLossDecision"
                    @click="submitFormatLossDecision('retry')"
                  >
                    {{ formatLossChoiceLabel('retry') }}
                  </button>
                  <button
                    type="button"
                    class="format-loss-banner__btn format-loss-banner__btn--accept"
                    :disabled="submittingLossDecision"
                    @click="submitFormatLossDecision('accept')"
                  >
                    {{ formatLossChoiceLabel('accept') }}
                  </button>
                </footer>

                <p v-if="lossDecisionError" class="format-loss-banner__error">{{ lossDecisionError }}</p>
              </div>

              <template v-else>
                <div class="timeline-row">
                  <div class="timeline-title-wrap">
                    <p>{{ item.title }}</p>
                    <button
                      v-if="item.kind === 'tool'"
                      class="tool-log-toggle"
                      type="button"
                      @click="toggleToolLog(item.message.id)"
                    >
                      Logs
                    </button>
                    <span
                      v-if="item.kind === 'tool' && !item.retryAttempt && retryBadge(item.message.toolCall?.name || '', item.status)"
                      class="retry-badge"
                      :class="`retry-badge--${retryBadge(item.message.toolCall?.name || '', item.status)!.kind}`"
                    >
                      <span class="retry-badge__label">
                        {{ retryBadge(item.message.toolCall?.name || '', item.status)!.label }}
                      </span>
                      <span
                        v-if="retryBadge(item.message.toolCall?.name || '', item.status)!.strategyLabel"
                        class="retry-badge__strategy"
                      >
                        · {{ retryBadge(item.message.toolCall?.name || '', item.status)!.strategyLabel }}
                      </span>
                    </span>
                    <span v-if="item.kind === 'tool' && item.retryAttempt" class="retry-attempt-badge">
                      第 {{ item.retryAttempt }} 次重试
                    </span>
                    <span v-if="item.status === 'waiting'" class="need-confirm-chip">Need Confirmation</span>
                  </div>
                  <span class="timeline-duration">{{ item.duration }}</span>
                </div>

                <div
                  v-if="item.kind === 'tool'"
                  class="tool-runtime-card"
                  :class="`tool-runtime-card--${toolCardPresentation(item.message).statusClass}`"
                >
                  <button
                    type="button"
                    class="tool-runtime-card__header"
                    :aria-expanded="!isToolCardCollapsed(item.message.id)"
                    :aria-label="isToolCardCollapsed(item.message.id) ? '展开工具调用详情' : '收起工具调用详情'"
                    @click="toggleToolCard(item.message.id)"
                  >
                    <span class="tool-runtime-card__title">
                      <span class="tool-runtime-card__status">【{{ toolCardPresentation(item.message).statusCode }}】</span>
                      {{ toolCardPresentation(item.message).title }}
                    </span>
                    <span class="tool-runtime-card__toggle" aria-hidden="true">
                      <n-icon :component="isToolCardCollapsed(item.message.id) ? ChevronForward : ChevronDown" />
                    </span>
                  </button>
                  <div v-if="!isToolCardCollapsed(item.message.id)" class="tool-runtime-card__content">
                    <div
                      v-if="toolCardPresentation(item.message).detail"
                      class="tool-runtime-card__markdown markdown-body"
                      v-html="toolCardDetailHtml(item.message)"
                    ></div>
                    <div
                      v-if="isToolLogOpen(item.message.id)"
                      class="tool-runtime-card__details"
                    >
                      <div class="tool-runtime-card__field">
                        <span class="tool-runtime-card__label">Input</span>
                        <pre class="tool-runtime-card__value">{{ item.message.toolCall?.input || 'waiting_input=true' }}</pre>
                      </div>
                      <div class="tool-runtime-card__field">
                        <span class="tool-runtime-card__label">Output</span>
                        <pre class="tool-runtime-card__value">{{ toolLogOutput(item.message) }}</pre>
                      </div>
                    </div>
                  </div>
                </div>

                <p v-if="item.detail" class="timeline-detail">{{ item.detail }}</p>

                <div v-if="item.kind === 'step' && item.id === 'step_understand' && item.status === 'done'" class="understanding-result-panel">
                  <button
                    type="button"
                    class="step-result-panel__header"
                    :aria-expanded="!isStepResultCollapsed(item.id)"
                    :aria-label="isStepResultCollapsed(item.id) ? '展开理解结果' : '收起理解结果'"
                    @click="toggleStepResult(item.id)"
                  >
                    <span>理解结果</span>
                    <span class="step-result-panel__toggle" aria-hidden="true">
                      <n-icon :component="isStepResultCollapsed(item.id) ? ChevronForward : ChevronDown" />
                    </span>
                  </button>
                  <p v-if="!isStepResultCollapsed(item.id)">{{ understandingResultText }}</p>
                </div>

                <div
                  v-if="item.kind === 'step' && item.id === 'step_plan' && item.status === 'done' && planDetailSteps.length"
                  class="execution-plan-panel"
                >
                  <button
                    type="button"
                    class="step-result-panel__header"
                    :aria-expanded="!isStepResultCollapsed(item.id)"
                    :aria-label="isStepResultCollapsed(item.id) ? '展开执行计划' : '收起执行计划'"
                    @click="toggleStepResult(item.id)"
                  >
                    <span>执行计划</span>
                    <span class="step-result-panel__summary">
                      <small>{{ completedPlanStepCount }}/{{ planDetailSteps.length }} 完成</small>
                      <span class="step-result-panel__toggle" aria-hidden="true">
                        <n-icon :component="isStepResultCollapsed(item.id) ? ChevronForward : ChevronDown" />
                      </span>
                    </span>
                  </button>
                  <ol v-if="!isStepResultCollapsed(item.id)">
                    <li
                      v-for="(step, stepIndex) in planDetailSteps"
                      :key="step.id"
                      class="execution-plan-step"
                      :class="`execution-plan-step--${step.status}`"
                    >
                      <span class="execution-plan-step__index">{{ stepIndex + 1 }}</span>
                      <span class="execution-plan-step__body">
                        <strong>{{ step.title }}</strong>
                        <em v-if="step.detail">{{ step.detail }}</em>
                      </span>
                      <span class="plan-step-status">{{ planStepStatusLabel(step.status) }}</span>
                    </li>
                  </ol>
                </div>

                <PublicExecutionUpdate
                  v-if="item.kind === 'tool' && displayToolUpdate(item.message) && displayToolUpdate(item.message)!.source === 'deterministic'"
                  :update="displayToolUpdate(item.message)!.update"
                  :start-time="item.message.toolCall?.startedAt"
                />
                <PublicExecutionUpdate
                  v-if="item.kind === 'tool' && displayToolUpdate(item.message) && displayToolUpdate(item.message)!.source === 'llm'"
                  :update="displayToolUpdate(item.message)!.update"
                  :start-time="displayToolUpdate(item.message)!.startTime"
                />
                <PublicExecutionUpdate
                  v-if="item.kind === 'narrative' && item.narrative?.publicUpdate"
                  :update="item.narrative.publicUpdate"
                  :start-time="item.narrative.createdAt"
                />
                <div v-if="item.kind === 'tool' && item.status === 'running' && !toolCardPresentation(item.message).detail" class="tool-thinking">
                  <span class="tool-thinking__dot"></span>
                  正在思考...
                </div>

              </template>
            </div>
          </li>
        </ol>
        </div>
      </section>
    </Transition>

    <article v-if="completionSummary" class="task-completion-summary">
      <div class="task-completion-summary__body markdown-body" v-html="completionSummaryHtml"></div>
    </article>

    <div v-if="hasFinalArea" class="artifact-result-grid">
      <article v-if="showResultSummary" class="result-summary-card">
        <h4>
          <n-icon :component="CheckmarkCircleOutline" />
          生成结果摘要
        </h4>
        <div v-for="metric in resultMetrics" :key="metric.label" class="result-summary-card__row">
          <span>{{ metric.label }}</span>
          <strong>{{ metric.value }}</strong>
        </div>
      </article>

      <article
        v-for="message in displayArtifactMessages"
        :key="message.id"
        class="artifact-card artifact-card--previewable"
        role="button"
        tabindex="0"
        @click="message.artifact && openArtifactPreview(message.artifact.id)"
        @keyup.enter="message.artifact && openArtifactPreview(message.artifact.id)"
      >
        <div class="artifact-card__head">
          <span class="artifact-card__icon" :class="{ 'artifact-card__icon--asset': isAssetIconForMessage(message) }">
            <img
              v-if="isAssetIconForMessage(message)"
              :src="assetIconSrcForMessage(message)"
              :alt="`${message.artifact?.name} 文件类型图标`"
              class="artifact-card__icon-image"
            />
            <n-icon v-else :component="DocumentTextOutline" />
          </span>
          <div>
            <p>{{ message.artifact?.name }}</p>
            <small>{{ message.artifact?.size || '文件大小待同步' }} · Word Document</small>
          </div>
        </div>
        <n-button secondary block @click.stop="message.artifact && emit('download-artifact', message.artifact.id, message.artifact.name)">
          下载产物
        </n-button>
      </article>

      <article v-if="errorMessage" class="error-card">
        <h4>任务执行失败</h4>
        <p>{{ errorMessage.text }}</p>
        <n-button v-if="errorMessage.taskId" secondary size="small" @click="emit('retry-task', errorMessage.taskId)">
          重试任务
        </n-button>
      </article>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NIcon } from 'naive-ui'
import AppSelect from '@/components/common/AppSelect.vue'
import {
  Checkmark,
  CheckmarkCircleOutline,
  ChevronDown,
  ChevronForward,
  DocumentTextOutline
} from '@vicons/ionicons5'
import type { AgentTask, ChatMessage, DynamicNarrative, PlanStepStatus, SectionAction, SectionItem, TaskPlanStep, TaskSummaryNarrativeState, ToolCallStatus, ToolNarrativeState } from '@/types'
import PublicExecutionUpdate from './PublicExecutionUpdate.vue'
import { isTaskCompletedMessage, taskDurationLabel, deriveTaskRunState } from '@/utils/taskState'
import { deriveTaskElapsedSource, formatTaskElapsed, formatToolElapsed, shouldTickTaskElapsed } from '@/utils/taskTiming'
import { resolveFileIcon, isAssetIcon } from '@/utils/fileIcon'
import { formatPublicExecutionUpdateText } from '@/utils/eventPayload'
import { renderMarkdown } from '@/utils/markdown'
import { buildToolCallPresentation, toolTimelineTitle } from '@/utils/toolCallPresentation'
import {
  chooseToolPublicUpdate,
  dynamicNarrativeTitle,
  shouldAppendWorkflowThinkingStep,
  type ToolPublicUpdateChoice
} from '@/utils/agentRunTimeline'
import {
  formatLossAnchorOrder,
  insertAtOrder as timelineAnchorInsertAtOrder
} from '@/utils/timelineAnchor'

const props = defineProps<{
  messages: ChatMessage[]
  conversationId?: string
  conversationTitle?: string
  confirmSectionsReady?: boolean
  sectionConfirmationExternal?: boolean
  formatLossExternal?: boolean
  task?: AgentTask | null
  /** Phase 2.9A.30: controlled collapse state from TaskRunBlock. */
  collapsed?: boolean
  hydrated?: boolean
  /** Phase 2.9B.2: 动态叙事节点(独立于 Tool 卡片)。 */
  dynamicNarratives?: DynamicNarrative[]
  /** Phase 2.9B.4: LLM-first Tool 叙事流式状态(锚定到 Tool 卡片下方)。 */
  toolNarratives?: ToolNarrativeState[]
  showToolNarratives?: boolean
  /** Phase 2.9B.6: 最终任务总结叙事状态槽(唯一权威槽位)。 */
  taskSummaryNarrative?: TaskSummaryNarrativeState | null
}>()

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
  'retry-task': [taskId: string]
  'format-loss-decision': [
    payload: {
      taskId: string
      decision: 'accept' | 'retry'
      note?: string
    }
  ]
  /** Phase 2.9A.30: controlled collapse update. */
  'collapse-change': [collapsed: boolean]
}>()
const router = useRouter()

function openArtifactPreview(artifactId: string) {
  router.push({
    name: 'document-preview',
    params: { itemId: artifactId },
    query: props.conversationId ? { from: 'chat', conversationId: props.conversationId, conversationTitle: props.conversationTitle || '对话' } : { from: 'library' }
  })
}

type TimelineStatus = PlanStepStatus | 'waiting' | 'warning'
type TimelineItem =
  | {
      id: string
      kind: 'step'
      title: string
      status: TimelineStatus
      duration: string
      detail?: string
    }
  | {
      id: string
      kind: 'tool'
      title: string
      status: TimelineStatus
      duration: string
      detail?: string
      message: ChatMessage
      retryAttempt?: number
      retryMax?: number
      retryStrategy?: string
    }
  | {
      id: string
      kind: 'confirmation'
      title: string
      status: TimelineStatus
      duration: string
      detail?: string
    }
  | {
      id: string
      kind: 'template-identified' | 'section-strategy'
      title: string
      status: TimelineStatus
      duration: string
      detail?: string
    }
  | {
      id: string
      kind: 'preparation-clarification'
      title: string
      status: TimelineStatus
      duration: string
      detail?: string
    }
  | {
      id: string
      kind: 'confirmation-receipt'
      title: string
      status: TimelineStatus
      duration: string
      message: ChatMessage
    }
  | {
      id: string
      kind: 'narrative'
      title: string
      status: TimelineStatus
      duration: string
      detail?: string
      narrative: DynamicNarrative
    }
  | {
      id: string
      kind: 'format-loss'
      title: string
      status: TimelineStatus
      duration: string
      detail?: string
      message: ChatMessage
    }

// Phase 2.9A.30: controlled collapse — use prop when provided, else internal ref
const _internalExpanded = ref(false)
const processExpanded = computed(() => {
  if (props.collapsed !== undefined) return !props.collapsed
  return _internalExpanded.value
})

function toggleExpanded() {
  const newExpanded = !processExpanded.value
  if (props.collapsed !== undefined) {
    // Controlled mode: emit to parent
    emit('collapse-change', !newExpanded)
  } else {
    _internalExpanded.value = newExpanded
  }
}

function toRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? value as Record<string, unknown> : {}
}

function hasPositiveNumber(value: unknown): boolean {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

function normalizeOptionalNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return value
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value)
    if (Number.isFinite(parsed) && parsed >= 0) return parsed
  }
  return null
}

function hasNonEmptyArray(value: unknown): boolean {
  return Array.isArray(value) && value.length > 0
}

function hasMeaningfulReviewResult(value: unknown): boolean {
  const review = toRecord(value)
  if (Object.keys(review).length === 0) return false
  return Boolean(
    hasPositiveNumber(review.generatedSections) ||
    hasPositiveNumber(review.generated_sections) ||
    hasPositiveNumber(review.keptSections) ||
    hasPositiveNumber(review.kept_sections) ||
    hasPositiveNumber(review.businessModules) ||
    hasPositiveNumber(review.business_modules) ||
    hasPositiveNumber(review.suggestion_count) ||
    hasNonEmptyArray(review.risks)
  )
}

function hasMeaningfulSummaryFacts(value: unknown): boolean {
  const facts = toRecord(value)
  if (Object.keys(facts).length === 0) return false
  const artifact = toRecord(facts.artifact)
  return Boolean(
    hasPositiveNumber(facts.generatedSections) ||
    hasPositiveNumber(facts.generated_sections) ||
    hasPositiveNumber(facts.keptSections) ||
    hasPositiveNumber(facts.kept_sections) ||
    hasPositiveNumber(facts.pageCount) ||
    hasPositiveNumber(facts.page_count) ||
    hasPositiveNumber(facts.businessModules) ||
    hasPositiveNumber(facts.business_modules) ||
    hasMeaningfulReviewResult(facts.review) ||
    Boolean(artifact.public_id || artifact.filename || artifact.file_name)
  )
}

function firstNonEmptyString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return ''
}

const openToolLogIds = ref(new Set<string>())
const editableSections = ref<SectionItem[]>([])

const planMessage = computed(() => [...props.messages].reverse().find((message) => message.type === 'agent_plan' && message.plan))
const requirementSummaryMessage = computed(() =>
  [...props.messages].reverse().find((message) => message.type === 'requirement_summary' && message.summary)
)
const toolMessages = computed(() => props.messages.filter((message) => message.type === 'tool_call' && message.toolCall))
const retryMessages = computed(() => props.messages.filter((message) => message.type === 'tool_retry' && message.toolRetry))
const retryMessagesByTool = computed(() => {
  const groups = new Map<string, ChatMessage[]>()
  for (const message of retryMessages.value) {
    const toolName = message.toolRetry?.toolName
    if (!toolName) continue
    const messages = groups.get(toolName) ?? []
    messages.push(message)
    groups.set(toolName, messages)
  }
  for (const messages of groups.values()) {
    messages.sort((left, right) => normalizeRetryAttempt(left.toolRetry?.attempt ?? 1) - normalizeRetryAttempt(right.toolRetry?.attempt ?? 1))
  }
  return groups
})
const rawSectionMessage = computed(() =>
  props.messages.find((message) => message.type === 'section_confirm' && message.sections?.length && !message.confirmed)
)
const clarificationMessage = computed(() =>
  props.messages.find((message) => message.type === 'preparation_clarification' && message.clarification && !message.confirmed)
)
const confirmationReceiptMessages = computed(() =>
  props.messages.filter((message) => !!message.confirmationReceipt)
)
const genericNeedUserMessage = computed(() =>
  props.messages.find((message) => message.type === 'agent_text' && message.eventType === 'need_user_confirm' && message.text?.trim())
)
const generatingMessage = computed(() =>
  [...props.messages].reverse().find((message) => message.type === 'generating_status' && message.generating)
)
const reviewMessage = computed(() =>
  [...props.messages].reverse().find((message) => message.type === 'review_result' && message.review)
)
const formatLossMessage = computed(() =>
  [...props.messages].reverse().find((message) => message.formatLoss && !message.confirmed)
)
// Phase 2.9A.25: task_completed 消息携带 _summaryFacts — 真实数据源
const taskCompletedMessage = computed(() =>
  [...props.messages].reverse().find((message) => message.type === 'agent_text' && (message as any)._summaryFacts)
)
const formatLossSecondsRemaining = ref<number | null>(null)

let formatLossCountdownTimer: ReturnType<typeof setInterval> | null = null

function startFormatLossCountdown(timeoutAt: string) {
  const deadline = new Date(timeoutAt).getTime()
  if (Number.isNaN(deadline)) {
    formatLossSecondsRemaining.value = null
    return
  }
  const tick = () => {
    const remainingMs = deadline - Date.now()
    const remaining = Math.max(0, Math.ceil(remainingMs / 1000))
    formatLossSecondsRemaining.value = remaining
    if (remaining <= 0 && formatLossCountdownTimer) {
      clearInterval(formatLossCountdownTimer)
      formatLossCountdownTimer = null
    }
  }
  tick()
  if (formatLossCountdownTimer) clearInterval(formatLossCountdownTimer)
  formatLossCountdownTimer = setInterval(tick, 1000)
}

function stopFormatLossCountdown() {
  if (formatLossCountdownTimer) {
    clearInterval(formatLossCountdownTimer)
    formatLossCountdownTimer = null
  }
  formatLossSecondsRemaining.value = null
}

watch(
  () => formatLossMessage.value?.formatLoss?.timeoutAt,
  (next) => {
    if (next) startFormatLossCountdown(next)
    else stopFormatLossCountdown()
  },
  { immediate: true },
)

const nowMs = ref(Date.now())
let taskDurationTicker: ReturnType<typeof setInterval> | null = null

function startTaskDurationTicker() {
  if (taskDurationTicker) return
  nowMs.value = Date.now()
  taskDurationTicker = setInterval(() => {
    nowMs.value = Date.now()
  }, 1000)
}

function stopTaskDurationTicker() {
  if (!taskDurationTicker) return
  clearInterval(taskDurationTicker)
  taskDurationTicker = null
}

onBeforeUnmount(() => {
  stopFormatLossCountdown()
  stopTaskDurationTicker()
})
const artifactMessages = computed(() => props.messages.filter((message) => message.type === 'artifact_download' && message.artifact))

function artifactIconForMessage(message: ChatMessage) {
  return resolveFileIcon(message.artifact?.name)
}
function isAssetIconForMessage(message: ChatMessage) {
  return isAssetIcon(artifactIconForMessage(message))
}
function assetIconSrcForMessage(message: ChatMessage) {
  return artifactIconForMessage(message).src ?? ''
}
const errorMessage = computed(() => props.messages.find((message) => message.type === 'error'))
const completedTextMessage = computed(() => props.messages.find(isTaskCompletedMessage))

/**
 * Phase 2.9A.27: completionSummary 优先用 task_completed 事件 content;
 * Phase 2.9B.6: 若存在经过事实校验的合法 LLM Task Summary,则 LLM 优先,
 * deterministic(task_completed.summary)兜底。
 *
 * 展示选择: displayTaskSummary = validLlmTaskSummary ?? deterministicTaskSummary
 * 合法 LLM 总结 = taskSummaryNarrative.status==='completed' &&
 *   narrativeSource==='llm' && fallbackUsed!==true && publicUpdate?.summary 非空。
 * 不新增任务总结卡片、不改 .task-completion-summary CSS、不改变 Artifact 卡片。
 */
const validLlmTaskSummary = computed(() => {
  const n = props.taskSummaryNarrative
  if (!n || !n.publicUpdate) return undefined
  const isLlm = n.narrativeSource === 'llm' || n.narrativeSource === undefined
  const isCompleted = n.status === 'completed'
  const notFallback = n.fallbackUsed !== true
  const text = formatPublicExecutionUpdateText(n.publicUpdate).trim()
  if (isLlm && isCompleted && notFallback && text) return text
  return undefined
})
const completionSummary = computed(() => {
  const llm = validLlmTaskSummary.value
  if (llm) return llm
  const explicit = completedTextMessage.value?.text?.trim()
  if (explicit) return explicit
  // fallback: task 已完成但无 task_completed 事件
  if (props.task?.status !== 'completed') return ''
  const rr = ((props.task as any).review_result ?? null) as Record<string, any> | null
  const artifact = (props.task as any).artifact as Record<string, unknown> | undefined
  const parts: string[] = ['任务已完成。']
  if (rr) {
    const genSec = rr.generated_sections ?? rr.generatedSections
    const keptSec = rr.kept_sections ?? rr.keptSections
    if (typeof genSec === 'number') {
      parts.push(`已生成 ${genSec} 个章节` + (typeof keptSec === 'number' ? `，保留 ${keptSec} 个模板章节` : '') + '。')
    }
    const reviewObj = (typeof rr.review === 'object' && rr.review !== null) ? rr.review : null
    const block = reviewObj?.block_count ?? rr.block_count
    const warn = reviewObj?.warning_count ?? rr.warning_count
    if ((block ?? 0) > 0 || (warn ?? 0) > 0) {
      parts.push(`审查发现 ${block ?? 0} 个阻断问题和 ${warn ?? 0} 个警告。`)
    }
  }
  if (artifact?.file_name) {
    parts.push(`已生成产物: ${artifact.file_name}。`)
  }
  return parts.join('')
})
const hasTerminalCompletion = computed(() => !!completedTextMessage.value)
const sectionMessage = computed(() => {
  if (hasTerminalCompletion.value) return undefined
  return rawSectionMessage.value
})

const sectionActionChoices: { label: string; value: SectionAction }[] = [
  { label: 'AI 生成', value: 'ai_generate' },
  { label: '保留原文', value: 'keep_template' },
]

watch(
  sectionMessage,
  (message) => {
    editableSections.value = (message?.sections ?? []).map((section) => ({ ...section }))
  },
  { immediate: true }
)

const fallbackSteps: TaskPlanStep[] = [
  { id: 'understand', title: '理解任务', status: 'done' },
  { id: 'plan', title: '生成执行计划', status: 'done' }
]

const rawPlanDetailSteps = computed<TaskPlanStep[]>(() => planMessage.value?.plan ?? [])
const toolStatusByName = computed(() => {
  const map = new Map<string, ToolCallStatus>()
  for (const message of toolMessages.value) {
    const name = message.toolCall?.name
    const status = message.toolCall?.status
    if (name && status) map.set(name, status)
  }
  if (!errorMessage.value && !isCompleted.value) {
    for (const message of retryMessages.value) {
      const name = message.toolRetry?.toolName
      if (name) map.set(name, 'running')
    }
  }
  return map
})
const planDetailSteps = computed<TaskPlanStep[]>(() =>
  rawPlanDetailSteps.value.map((step) => ({
    ...step,
    status: normalizePlanStepStatus(derivePlanStepStatus(step))
  }))
)
const hasGeneratedPlan = computed(() => planDetailSteps.value.length > 0)
const planEventPayload = computed(() => {
  const rawEvent = planMessage.value?._rawEvent
  return toRecord(rawEvent?.payload ?? rawEvent?.payload_json)
})
const planEventPlan = computed(() => toRecord(planEventPayload.value.plan))
const understandingResultText = computed(() => {
  const requirementSummary = requirementSummaryText(requirementSummaryMessage.value)
  if (requirementSummary) return requirementSummary
  const explicit = firstNonEmptyString(
    planEventPayload.value.understanding_summary,
    planEventPayload.value.understandingSummary
  )
  if (explicit) return explicit
  const stepDetail = planDetailSteps.value[0]?.detail?.trim()
  if (stepDetail) return stepDetail
  const goal = firstNonEmptyString(
    planEventPayload.value.goal,
    planEventPayload.value.dynamic_goal,
    planEventPayload.value.user_instruction,
    planEventPlan.value.goal
  )
  const operation = firstNonEmptyString(planEventPayload.value.operation, planEventPlan.value.operation)
  const target = firstNonEmptyString(planEventPayload.value.target_capability, planEventPlan.value.target_capability)
  if (target === 'document_qa') {
    const operationLabel: Record<string, string> = {
      summarize: '总结文档内容',
      analyze: '分析文档内容',
      extract: '提取文档信息',
      compare: '对比分析文档'
    }
    return `根据上传文档${operationLabel[operation] ?? '回答文档相关问题'}。`
  }
  if (goal) return goal
  return '处理当前输入。'
})
const completedPlanStepCount = computed(() => planDetailSteps.value.filter((step) => step.status === 'done').length)

function requirementSummaryText(message: ChatMessage | undefined): string {
  const summary = message?.summary
  if (!summary) return ''
  const description = firstNonEmptyString(summary.description)
  if (description) return description
  const title = firstNonEmptyString(summary.title)
  const metricText = (summary.metrics ?? [])
    .map((metric) => `${metric.label}${metric.value ? ` ${metric.value}` : ''}`)
    .join('、')
  return firstNonEmptyString(
    title && metricText ? `${title}：${metricText}` : '',
    title,
    metricText
  )
}

const basePlanSteps = computed<TaskPlanStep[]>(() => [
  {
    ...fallbackSteps[0],
    status: hasGeneratedPlan.value ? 'done' : fallbackSteps[0].status,
    detail: hasGeneratedPlan.value ? '已理解用户任务' : fallbackSteps[0].detail
  },
  {
    ...fallbackSteps[1],
    status: hasGeneratedPlan.value ? 'done' : fallbackSteps[1].status,
    detail: hasGeneratedPlan.value ? `已规划 ${planDetailSteps.value.length} 个执行步骤` : fallbackSteps[1].detail
  }
])

const isWaitingConfirm = computed(() =>
  (!!sectionMessage.value && !sectionMessage.value.confirmed) ||
  !!formatLossMessage.value
)
const canSubmitSectionConfirmation = computed(() => props.confirmSectionsReady !== false)
// Phase 2.9B.5: 任务级终态只认权威来源 — task_completed 事件消息 /
// Task Detail.status=completed。局部 Tool 失败(hasFailedTool)不得提升为
// 任务失败。
const isCompleted = computed(() => hasTerminalCompletion.value || props.task?.status === 'completed')
// 任务失败只由权威来源决定: Task Detail.status=failed / task_failed 事件消息。
const isFailed = computed(() => props.task?.status === 'failed' || props.task?.status === 'cancelled' || !!errorMessage.value)
// Phase 2.9B.5: runState 用权威推导 — 删除「hasFailedTool → isFailed →
// runState='failed'」的局部失败提升语义。
const runState = computed(() => deriveTaskRunState({
  taskStatus: props.task?.status,
  explicitTaskFailed: !!errorMessage.value,
  explicitCancelled: props.task?.status === 'cancelled',
  explicitCompleted: isCompleted.value,
  waitingConfirmation: isWaitingConfirm.value,
}))

function isTerminalState(state: string) {
  return state === 'completed' || state === 'failed' || state === 'cancelled'
}

const canToggleProcess = computed(() => isTerminalState(runState.value))
const shouldShowProcess = computed(() => !isTerminalState(runState.value) || processExpanded.value)
const hasTaskDuration = computed(() => props.messages.length > 0)
const taskTimingActive = computed(() => {
  const taskStatus = props.task?.status
  return shouldTickTaskElapsed(taskStatus ?? runState.value)
})

watch(
  taskTimingActive,
  (isActive) => {
    if (isActive) startTaskDurationTicker()
    else stopTaskDurationTicker()
  },
  { immediate: true },
)

watch(
  runState,
  (nextState, previousState) => {
    if (nextState === previousState) return
    // Phase 2.9A.30: controlled mode — don't auto-toggle if collapsed prop is provided
    if (props.collapsed !== undefined) return
    if (props.hydrated) {
      if (isTerminalState(nextState)) {
        _internalExpanded.value = false
      }
      return
    }
    _internalExpanded.value = !isTerminalState(nextState)
  },
  { immediate: true },
)

const allTimelineItems = computed<TimelineItem[]>(() => {
  const items: TimelineItem[] = basePlanSteps.value.map((step) => ({
    id: `step_${step.id}`,
    kind: 'step',
    title: step.title,
    status: step.status,
    duration: '',
    detail: step.detail
  }))
  const attachedRetryIds = new Set<string>()

  // BUG FIX 2026-08-19：narrative / format-loss banner 按 order 锚到
  // timeline 已有节点之后。抽到 utils/timelineAnchor 便于单测。
  function insertAtOrder(newItem: TimelineItem, order: number | undefined) {
    timelineAnchorInsertAtOrder(items, newItem, order)
  }

  for (const message of toolMessages.value) {
    const status = mapToolStatus(message.toolCall?.status)
    const toolName = message.toolCall?.name || 'Agent Tool'
    items.push({
      id: `tool_${message.id}`,
      kind: 'tool',
      title: toolTimelineTitle(message.toolCall),
      status,
      duration: formatToolDuration(message),
      message
    })

    const retries = retryMessagesByTool.value.get(toolName) ?? []
    for (const [retryIndex, retryMessage] of retries.entries()) {
      attachedRetryIds.add(retryMessage.id)
      const retryAttempt = normalizeRetryAttempt(retryMessage.toolRetry?.attempt ?? 1)
      const retryStatus = retryTimelineStatus(retryIndex, retries.length)
      items.push({
        id: `tool_retry_${retryMessage.id}`,
        kind: 'tool',
        title: toolTimelineTitle(message.toolCall),
        status: retryStatus,
        duration: retryMessage.toolRetry?.backoffSeconds
          ? `${retryMessage.toolRetry.backoffSeconds}s`
          : formatToolDuration(buildRetryToolMessage(message, retryMessage, retryStatus)),
        message: buildRetryToolMessage(message, retryMessage, retryStatus),
        retryAttempt,
        retryMax: retryMessage.toolRetry?.maxRetries,
        retryStrategy: retryMessage.toolRetry?.strategy
      })
    }
  }

  for (const retryMessage of retryMessages.value) {
    if (attachedRetryIds.has(retryMessage.id)) continue
    const toolName = retryMessage.toolRetry?.toolName || 'Agent Tool'
    const retryAttempt = normalizeRetryAttempt(retryMessage.toolRetry?.attempt ?? 1)
    const retryStatus = retryTimelineStatus(0, 1)
    items.push({
      id: `tool_retry_${retryMessage.id}`,
      kind: 'tool',
      title: toolTimelineTitle({ name: toolName }),
      status: retryStatus,
      duration: retryMessage.toolRetry?.backoffSeconds
        ? `${retryMessage.toolRetry.backoffSeconds}s`
        : formatToolDuration(buildRetryToolMessage(null, retryMessage, retryStatus)),
      message: buildRetryToolMessage(null, retryMessage, retryStatus),
      retryAttempt,
      retryMax: retryMessage.toolRetry?.maxRetries,
      retryStrategy: retryMessage.toolRetry?.strategy
    })
  }

  // Phase 2.9B.2: 动态叙事插入真实事件位置(§15) — 以 canonical_order /
  // sequence_no 为序,锚定到最后一个 order 小于等于它的 timeline 节点之后;
  // 无匹配锚点(如 prep 决策在 TemplateParser 完成后)落在对应工具之后。
  if (props.dynamicNarratives && props.dynamicNarratives.length > 0) {
    const orderedNarratives = [...props.dynamicNarratives].sort((a, b) => {
      const ao = a.canonicalOrder ?? a.sequenceNo ?? Number.MAX_SAFE_INTEGER
      const bo = b.canonicalOrder ?? b.sequenceNo ?? Number.MAX_SAFE_INTEGER
      if (ao !== bo) return ao - bo
      return a.sourceEventId.localeCompare(b.sourceEventId)
    })
    for (const narrative of orderedNarratives) {
      const narrativeOrder = narrative.canonicalOrder ?? narrative.sequenceNo ?? Number.MAX_SAFE_INTEGER
      const title = dynamicNarrativeTitle(narrative)
      insertAtOrder({
        id: `narrative_${narrative.id}`,
        kind: 'narrative',
        title,
        status: 'done',
        duration: '',
        narrative
      }, narrativeOrder)
    }
  }

  // BUG FIX 2026-08-19：format-loss banner 必须锚定到"调用Word文档格式
  // 自检工具(DocxFormatCheckTool)"完成项之后，而不是渲染在 timeline
  // 顶部。锚定规则：
  //   1. 优先取 DocxFormatCheckTool 工具项的 conversationSequence；
  //   2. 找不到（事件尚未上报）→ 推到 timeline 末尾（与 section-strategy
  //      一致，避免阻塞型确认 banner 永远不显示）；
  //   3. 若 format-loss 消息有 conversationSequence 也参与排序对齐。
  const formatLossForInsert = formatLossMessage.value
  if (formatLossForInsert && formatLossForInsert.formatLoss) {
    const checkToolItem = items.find(
      (item): item is Extract<TimelineItem, { kind: 'tool' }> =>
        item.kind === 'tool' &&
        item.message.toolCall?.name === 'DocxFormatCheckTool',
    )
    const checkOrder = checkToolItem?.message.conversationSequence
    const lossOrder =
      typeof formatLossForInsert.conversationSequence === 'number'
        ? formatLossForInsert.conversationSequence
        : undefined
    const anchorOrder = formatLossAnchorOrder(checkOrder, lossOrder)
    insertAtOrder({
      id: `format_loss_${formatLossForInsert.id}`,
      kind: 'format-loss',
      title: '检测到格式丢失，请确认处理方式',
      status: 'waiting',
      duration: '',
      message: formatLossForInsert
    }, anchorOrder)
  }

  for (const message of confirmationReceiptMessages.value) {
    insertAtOrder({
      id: `confirmation_receipt_${message.id}`,
      kind: 'confirmation-receipt',
      title: '已收到用户确认',
      status: 'done',
      duration: '',
      message
    }, message.conversationSequence)
  }

  if (sectionMessage.value && !sectionMessage.value.confirmed) {
    items.push({
      id: `section_strategy_${sectionMessage.value.id}`,
      kind: 'section-strategy',
      title: '确认章节处理策略',
      status: 'waiting',
      duration: '',
      detail: '需要你确认章节结构后继续。'
    })
  }

  if (clarificationMessage.value && !hasTerminalCompletion.value) {
    items.push({
      id: `preparation_clarification_${clarificationMessage.value.id}`,
      kind: 'preparation-clarification',
      title: '正在等待用户确认',
      status: 'waiting',
      duration: '',
      detail: '请在输入框上方完成关键信息选择。'
    })
  }

  if (!sectionMessage.value && genericNeedUserMessage.value && !hasTerminalCompletion.value) {
    items.push({
      id: `generic_confirm_${genericNeedUserMessage.value.id}`,
      kind: 'confirmation',
      title: '需要你的确认',
      status: 'waiting',
      duration: '',
      detail: genericNeedUserMessage.value.text?.trim()
    })
  }

  if (generatingMessage.value && !isCompleted.value) {
    items.push({
      id: `generating_${generatingMessage.value.id}`,
      kind: 'step',
      title: '正在生成文档...',
      status: 'running',
      duration: '',
      detail: generatingMessage.value.generating?.currentStep || generatingMessage.value.generating?.currentSection
    })
  }

  if (shouldAppendWorkflowThinkingStep({
    isCompleted: isCompleted.value,
    isFailed: isFailed.value,
    hasRunningOrWaitingItem: items.some((item) => item.status === 'running' || item.status === 'waiting')
  })) {
    items.push({
      id: 'workflow_thinking',
      kind: 'step',
      title: '正在思考...',
      status: 'running',
      duration: '',
      detail: ''
    })
  }

  if (shouldAppendGeneratedArtifactTerminalSteps.value && !items.some((item) => item.title.includes('审查'))) {
    items.push(
      { id: 'done_review', kind: 'step', title: '审查结果', status: 'done', duration: '' },
      { id: 'done_export', kind: 'step', title: '导出 Word 文档', status: 'done', duration: '' }
    )
  }

  return items
})

const timelineItems = computed<TimelineItem[]>(() => revealSequentialTimelineItems(allTimelineItems.value))
const taskStartedAtCandidates = computed(() => [
  props.task?.startedAt,
  props.task?.run?.startedAt,
  ...props.messages.map((message) => message.createdAt),
  ...toolMessages.value.map((message) => message.toolCall?.startedAt ?? message.createdAt),
  ...(props.dynamicNarratives ?? []).map((narrative) => narrative.createdAt),
  ...(props.toolNarratives ?? []).map((narrative) => narrative.createdAt),
])
const taskCompletedAtCandidates = computed(() => [
  props.task?.completedAt,
  props.task?.run?.finishedAt,
  ...props.messages.map((message) => message.createdAt),
  ...toolMessages.value.map((message) => message.toolCall?.finishedAt ?? message.createdAt),
  ...(props.dynamicNarratives ?? []).map((narrative) => narrative.createdAt),
  ...(props.toolNarratives ?? []).map((narrative) => narrative.completedAt ?? narrative.createdAt),
])
const processedDuration = computed(() => {
  return formatTaskElapsed(
    deriveTaskElapsedSource(props.task, runState.value, taskStartedAtCandidates.value, taskCompletedAtCandidates.value),
    nowMs.value,
  )
})
const completionSummaryHtml = computed(() => renderMarkdown(completionSummary.value))

function formatToolDuration(message: ChatMessage): string {
  return formatToolElapsed(message.toolCall ?? {}, nowMs.value)
}

const sectionStats = computed(() =>
  sectionActionChoices.map((option) => ({
    ...option,
    action: option.value,
    count: editableSections.value.filter((section) => section.action === option.value).length
  }))
)
// F022: count sections whose suggested_action came from the user's
// natural-language prompt (UserConstraintExtractor override).  Used to
// render a "其中 N 个按你的提示词指定" hint in the strategy header and
// to drive the "📌 用户指定" badge per row.
const userConstraintCount = computed(
  () => editableSections.value.filter((section) => section.constraintSource === 'user_prompt').length
)
const hasGeneratedArtifactFacts = computed(() => {
  const facts = (taskCompletedMessage.value as any)?._summaryFacts
  return Boolean(
    hasMeaningfulSummaryFacts(facts) ||
    hasMeaningfulReviewResult(reviewMessage.value?.review) ||
    hasMeaningfulReviewResult(props.task?.review_result) ||
    artifactMessages.value.length > 0
  )
})
const shouldAppendGeneratedArtifactTerminalSteps = computed(() =>
  isCompleted.value && Boolean(
    hasMeaningfulReviewResult(reviewMessage.value?.review) ||
    hasMeaningfulReviewResult(props.task?.review_result) ||
    artifactMessages.value.length > 0
  )
)
const isIncrementalTask = computed(() => props.messages.some((message) => {
  const eventType = message.eventType
  return eventType === 'incremental_started' ||
    eventType === 'incremental_decision_made' ||
    eventType === 'incremental_tool_finished' ||
    eventType === 'incremental_tool_blocked' ||
    eventType === 'incremental_completed' ||
    eventType === 'incremental_failed' ||
    eventType === 'incremental_fallback'
}))
// Incremental export emits artifact_created before the final LLM summary.
// Keep the artifact facts available for metrics, but reveal the download card
// only after the completed summary is ready so the visual order is stable.
const displayArtifactMessages = computed(() => {
  if (!isIncrementalTask.value) return artifactMessages.value
  return isCompleted.value && completionSummary.value.trim() ? artifactMessages.value : []
})
const showResultSummary = computed(() => hasGeneratedArtifactFacts.value)
const hasFinalArea = computed(() => showResultSummary.value || displayArtifactMessages.value.length > 0 || !!errorMessage.value)
const resultMetrics = computed(() => {
  // Phase 2.9A.27:事实优先级 — 不要用 plan step 数 / event 数冒充章节数。
  // 优先级(高到低):
  //   1. task_completed._summaryFacts(规范化字段,来源 = Summary Facts 构造器)
  //   2. task detail review_result_json(Phase 2.8R 后端持久化)
  //   3. review_result message payload(历史事件直读)
  //   4. 安全 fallback "—"(不是错误数字)
  const facts = (taskCompletedMessage.value as any)?._summaryFacts
  if (hasMeaningfulSummaryFacts(facts)) {
    return [
      { label: '章节数', value: String(facts.generatedSections ?? 0) },
      { label: '文档页数', value: formatPageCount(facts.pageCount ?? facts.page_count ?? facts.artifact?.page_count ?? facts.artifact?.pageCount) },
      { label: '审查建议', value: String(facts.review?.suggestion_count ?? facts.review?.risks?.length ?? 0) }
    ]
  }
  // 第二档:task 详情的 review_result_json(LangGraph Review 节点产物)
  const taskReview = (props.task?.review_result as any) ?? null
  if (hasMeaningfulReviewResult(taskReview)) {
    const generated = taskReview.generatedSections ?? taskReview.generated_sections
    const kept = taskReview.keptSections ?? taskReview.kept_sections
    const chapterCount =
      typeof generated === 'number' && typeof kept === 'number'
        ? generated + kept
        : typeof generated === 'number'
          ? generated
          : '—'
    return [
      { label: '章节数', value: String(chapterCount) },
      { label: '文档页数', value: formatPageCount(taskReview.pageCount ?? taskReview.page_count ?? (props.task as any)?.artifact?.page_count ?? (props.task as any)?.artifact?.pageCount) },
      { label: '审查建议', value: String(taskReview.risks?.length ?? taskReview.suggestion_count ?? '—') }
    ]
  }
  const review = reviewMessage.value?.review
  if (hasMeaningfulReviewResult(review)) {
    const reviewRecord = toRecord(review)
    const generated = Number(reviewRecord.generatedSections ?? 0)
    const kept = Number(reviewRecord.keptSections ?? 0)
    return [
      { label: '章节数', value: String(generated + kept) },
      { label: '文档页数', value: latestArtifactPageCountLabel.value },
      { label: '审查建议', value: String(Array.isArray(reviewRecord.risks) ? reviewRecord.risks.length : '—') }
    ]
  }
  // 真实 fallback — 不再用 doneCount 假冒章节数,用占位符 —
  return [
    { label: '章节数', value: '—' },
    { label: '文档页数', value: latestArtifactPageCountLabel.value },
    { label: '审查建议', value: '—' }
  ]
})

const latestArtifactPageCountLabel = computed(() =>
  formatPageCount([...artifactMessages.value].reverse().find((message) => message.artifact?.pageCount !== undefined)?.artifact?.pageCount)
)

function formatPageCount(value: unknown): string {
  const pageCount = normalizeOptionalNumber(value)
  return pageCount === null ? '—' : String(pageCount)
}

function mapToolStatus(status?: ToolCallStatus): TimelineStatus {
  if (status === 'success') return 'done'
  if (status === 'failed') return 'failed'
  if (status === 'warning') return 'warning'
  return 'running'
}

function normalizeRetryAttempt(attempt: number) {
  return Math.max(1, attempt > 1 ? attempt - 1 : attempt)
}

function retryTimelineStatus(index: number, total: number): TimelineStatus {
  if (isCompleted.value) return 'done'
  if (errorMessage.value) return 'failed'
  return index === total - 1 ? 'running' : 'failed'
}

function retryToolStatus(status: TimelineStatus): ToolCallStatus {
  if (status === 'done') return 'success'
  if (status === 'failed') return 'failed'
  return 'running'
}

function buildRetryToolMessage(baseMessage: ChatMessage | null, retryMessage: ChatMessage, status: TimelineStatus): ChatMessage {
  const retry = retryMessage.toolRetry
  const toolName = retry?.toolName || baseMessage?.toolCall?.name || 'Agent Tool'
  const errorReason = retry?.lastError || retry?.reason || '等待重试结果'
  return {
    ...retryMessage,
    type: 'tool_call',
    toolCall: {
      id: retryMessage.id,
      name: toolName,
      status: retryToolStatus(status),
      input: retry?.reason || baseMessage?.toolCall?.input || 'retry=true',
      output: errorReason,
      duration: retry?.backoffSeconds ? `${retry.backoffSeconds}s` : '',
      publicUpdate: retry?.publicUpdate ?? baseMessage?.toolCall?.publicUpdate
    }
  }
}

function statusFromTool(toolName: string): PlanStepStatus | null {
  const status = toolStatusByName.value.get(toolName)
  if (status === 'success') return 'done'
  if (status === 'failed') return 'failed'
  if (status === 'running') return 'running'
  return null
}

function derivePlanStepStatus(step: TaskPlanStep): PlanStepStatus {
  if (isCompleted.value && step.status !== 'failed' && step.status !== 'superseded') return 'done'
  const id = step.id
  if (id === 'parse_requirement') return statusFromTool('RequirementParserTool') ?? step.status
  if (id === 'extract_modules') return statusFromTool('RequirementParserTool') ?? step.status
  if (id === 'parse_template') return statusFromTool('TemplateParserTool') ?? step.status
  if (id === 'search_knowledge') return statusFromTool('KnowledgeSearchTool') ?? step.status
  if (id === 'confirm_sections' || id === 'wait_user_confirm') {
    if (sectionMessage.value?.confirmed) return 'done'
    if (sectionMessage.value) return 'running'
    return statusFromTool('SectionSuggestionTool') ?? step.status
  }
  if (id === 'generate_test_plan' || id === 'generate') return statusFromTool('TestPlanGeneratorTool') ?? (generatingMessage.value ? 'running' : step.status)
  if (id === 'review_result' || id === 'review') return statusFromTool('ResultReviewTool') ?? (reviewMessage.value ? 'done' : step.status)
  if (id === 'export_word' || id === 'export') return statusFromTool('WordExportTool') ?? (artifactMessages.value.length ? 'done' : step.status)
  return step.status
}

function planStepStatusLabel(status: PlanStepStatus) {
  if (status === 'done') return '完成'
  if (status === 'running') return '运行中'
  if (status === 'failed') return '失败'
  if (status === 'superseded') return '已替换'
  return '待执行'
}

function normalizePlanStepStatus(status: unknown): PlanStepStatus {
  if (status === 'done' || status === 'completed' || status === 'success') return 'done'
  if (status === 'running' || status === 'in_progress' || status === 'waiting' || status === 'waiting_user') return 'running'
  if (status === 'failed' || status === 'error') return 'failed'
  if (status === 'superseded' || status === 'skipped') return 'superseded'
  return 'pending'
}

function toolLogOutput(message: ChatMessage) {
  const toolCall = message.toolCall
  if (!toolCall) return ''
  if (toolCall.output.trim()) return toolCall.output
  return toolCall.status === 'running' ? 'Tool execution is in progress' : ''
}

function isToolLogOpen(messageId: string) {
  return openToolLogIds.value.has(messageId)
}

function toggleToolLog(messageId: string) {
  const next = new Set(openToolLogIds.value)
  if (next.has(messageId)) {
    next.delete(messageId)
  } else {
    next.add(messageId)
    expandToolCard(messageId)
  }
  openToolLogIds.value = next
}

const collapsedToolCardIds = ref(new Set<string>())
const collapsedStepResultIds = ref(new Set<string>())

function isToolCardCollapsed(messageId: string): boolean {
  return collapsedToolCardIds.value.has(messageId)
}

function expandToolCard(messageId: string): void {
  if (!collapsedToolCardIds.value.has(messageId)) return
  const next = new Set(collapsedToolCardIds.value)
  next.delete(messageId)
  collapsedToolCardIds.value = next
}

function toggleToolCard(messageId: string): void {
  const next = new Set(collapsedToolCardIds.value)
  if (next.has(messageId)) next.delete(messageId)
  else next.add(messageId)
  collapsedToolCardIds.value = next
}

function isStepResultCollapsed(itemId: string): boolean {
  return collapsedStepResultIds.value.has(itemId)
}

function toggleStepResult(itemId: string): void {
  const next = new Set(collapsedStepResultIds.value)
  if (next.has(itemId)) next.delete(itemId)
  else next.add(itemId)
  collapsedStepResultIds.value = next
}

const collapsedConfirmationReceiptIds = ref(new Set<string>())

function isConfirmationReceiptCollapsed(messageId: string): boolean {
  return collapsedConfirmationReceiptIds.value.has(messageId)
}

function toggleConfirmationReceipt(messageId: string): void {
  const next = new Set(collapsedConfirmationReceiptIds.value)
  if (next.has(messageId)) next.delete(messageId)
  else next.add(messageId)
  collapsedConfirmationReceiptIds.value = next
}

function confirmationReceiptLabel(message: ChatMessage): string {
  if (message.confirmationReceipt?.kind === 'section') return '用户确认章节处理策略'
  if (message.confirmationReceipt?.kind === 'format_loss') return '用户确认格式丢失处理'
  return '用户补充关键信息'
}

function confirmationReceiptHtml(message: ChatMessage): string {
  return renderMarkdown(message.confirmationReceipt?.markdown ?? '')
}

function toolCardPresentation(message: ChatMessage) {
  return buildToolCallPresentation(message.toolCall)
}

function toolCardDetailHtml(message: ChatMessage): string {
  return renderMarkdown(toolCardPresentation(message).detail)
}

/**
 * Phase 2.9B.6: 单一展示选择器 — 同一个 Tool 最终只显示一份叙事。
 *
 * 优先级:
 *   LLM 叙事合法(completed + narrativeSource=llm + fallbackUsed!==true +
 *   publicUpdate 存在) → 只显示 LLM public_update;
 *   否则(Narrative failed/timeout/invalid/缺失)→ 只显示 deterministic
 *   public update(来自 tool_finished 的公共模板)。
 *
 * 不修改 PublicExecutionUpdate 样式、不新增卡片、不新增 CSS。
 */
function displayToolUpdate(message: ChatMessage): ToolPublicUpdateChoice | undefined {
  if (props.showToolNarratives === false) return undefined
  return chooseToolPublicUpdate(message.toolCall, props.toolNarratives ?? [])
}

function retriesForTool(toolName: string) {
  return retryMessages.value
    .filter((message) => message.toolRetry?.toolName === toolName)
    .map((message) => message.toolRetry!)
}

/**
 * 给步骤卡片用的"重试角标"信息。
 *
 * 语义：
 * - 卡片 status=running 且已有 N 次重试记录 → 当前正在跑第 (N+1) 次，标题旁挂「第 N+1 次重试」
 * - 卡片 status=failed 且已有 N 次重试记录 → 已经把 N 次重试机会都用完了仍未成功
 * - 卡片 status=running 无重试记录 → 不挂角标（首次运行）
 * - 卡片 status=success → 不挂角标（重试成功，无需再提示）
 */
function retryBadge(toolName: string, status: TimelineStatus | undefined) {
  const retries = retriesForTool(toolName)
  if (retries.length === 0) return null
  const last = retries[retries.length - 1]
  const maxRetries = last.maxRetries
  const lastError = last.lastError || ''

  if (status === 'running') {
    return {
      kind: 'retrying' as const,
      label: `第 ${retries.length + 1} 次重试`,
      subLabel: `${retries.length} / ${maxRetries}`,
      lastError,
      strategyLabel: retryStrategyLabel(last.strategy),
    }
  }
  if (status === 'failed') {
    return {
      kind: 'exhausted' as const,
      label: `重试 ${retries.length} / ${maxRetries} 次仍失败`,
      subLabel: `${retries.length} / ${maxRetries}`,
      lastError,
      strategyLabel: retryStrategyLabel(last.strategy),
    }
  }
  return null
}

function retryStrategyLabel(strategy: string) {
  switch (strategy) {
    case 'schema_feedback':
      return 'Schema 反馈'
    case 'backoff':
      return '退避重试'
    case 'degrade':
      return '降级输入'
    case 'same_inputs':
      return '原参数重试'
    case 'hard_stop':
      return '已硬停'
    default:
      return strategy || '重试'
  }
}

function revealSequentialTimelineItems(items: TimelineItem[]) {
  if (isCompleted.value) return items

  const activeIndex = items.findIndex((item) => item.status === 'running' || item.status === 'waiting')
  if (activeIndex >= 0) return items.slice(0, activeIndex + 1)

  // BUG FIX 2026-08-18 (方案 1):失败时**不再截断在第一个 failed item**。
  // 原实现 findIndex(failed) → slice(0, failedIndex+1),假设"第一个失败
  // = 停止点"。但知识库降级失败(not_configured / api_unavailable)不是
  // 停止点,任务会继续执行到真实失败点(如 WordExportTool)。
  // 截断会让前端停在"调用知识库查询工具",隐藏后续真实执行与真实失败。
  // 修复:任务失败时显示全部 items,让用户看到完整链路 + 真实失败位置。
  const pendingIndex = items.findIndex((item) => item.status === 'pending')
  if (pendingIndex >= 0) return items.slice(0, pendingIndex + 1)

  return items
}

function actionLabel(action: SectionAction) {
  return sectionActionChoices.find((option) => option.value === action)?.label ?? action
}

function displaySectionName(section: SectionItem) {
  // 仅显示章节名称（如：修订记录），不显示序号（如 1 修订记录）
  return section.title.trim() || section.code.trim()
}

function handleSectionActionChange(index: number, value: string | number) {
  const action = value as SectionAction
  editableSections.value = editableSections.value.map((section, currentIndex) =>
    currentIndex === index ? { ...section, action } : section
  )
}

function batchUpdateAction(action: SectionAction) {
  editableSections.value = editableSections.value.map((section) => ({ ...section, action }))
}

function restoreSuggestedActions() {
  editableSections.value = editableSections.value.map((section) => ({
    ...section,
    action: section.suggestedAction
  }))
}

function confirmActiveSection() {
  const message = sectionMessage.value
  if (!message?.taskId) return
  if (!canSubmitSectionConfirmation.value) return
  emit('confirm-section', {
    messageId: message.id,
    taskId: message.taskId,
    confirmationId: message.confirmationId,
    confirmationType: message.confirmationType,
    sections: editableSections.value
  })
}

const submittingLossDecision = ref(false)
const lossDecisionError = ref<string | null>(null)
type FormatLossItem = NonNullable<ChatMessage['formatLoss']>['losses'][number]

function formatLossChoiceLabel(decision: 'accept' | 'retry'): string {
  return decision === 'accept'
    ? '继续导出'
    : '重新生成文档'
}

function formatLossLocation(loss: FormatLossItem): string {
  const element = String(loss.element ?? '').trim()
  return element || '模板元素'
}

function formatLossImpact(loss: FormatLossItem): string {
  const message = String(loss.message ?? '').trim()
  return message || '可能影响模板结构或跳转定位'
}

function formatLossSuggestedAction(): string {
  return '可接受丢失继续导出，或重新生成文档'
}

function submitFormatLossDecision(decision: 'accept' | 'retry') {
  const message = formatLossMessage.value
  if (!message?.taskId) return
  if (submittingLossDecision.value) return
  submittingLossDecision.value = true
  lossDecisionError.value = null
  try {
    emit('format-loss-decision', {
      taskId: message.taskId,
      decision,
    })
  } finally {
    // The parent flips the message out of the prop list once the API
    // call returns; until then we keep the buttons disabled.
    setTimeout(() => {
      submittingLossDecision.value = false
    }, 800)
  }
}
</script>

<style scoped>
.agent-run-shell {
  display: grid;
  gap: 18px;
  width: min(100%, var(--ta-wide-card-width));
}

.task-duration-header {
  padding-bottom: 10px;
  border-bottom: 1px solid var(--ta-border);
}

.task-duration-toggle {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  padding: 0;
  color: var(--ta-text-muted);
  font: inherit;
  font-size: 14px;
  line-height: 20px;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.task-duration-toggle:hover {
  color: var(--ta-text-strong);
}

.task-duration-toggle > .n-icon {
  font-size: 15px;
}

.task-duration-live {
  color: var(--ta-text-muted);
  font-size: 14px;
  line-height: 20px;
}

.task-process {
  min-width: 0;
}

.task-process__body {
  padding: 0;
}

.task-process-enter-active,
.task-process-leave-active {
  transition: opacity 180ms ease, transform 180ms ease;
}

.task-process-enter-from,
.task-process-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

.agent-run-card {
  position: relative;
  overflow: hidden;
  background: #ffffff;
  border: 2px solid var(--ta-primary);
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.1), 0 1px 2px rgba(15, 23, 42, 0.06);
}

.agent-run-card--completed {
  border-color: var(--ta-border);
}

.agent-run-card--failed {
  border-color: var(--ta-border);
}

.agent-run-card__progress {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 4px;
  background: transparent;
}

.agent-run-card__progress span {
  display: block;
  height: 100%;
  background: var(--ta-primary);
  border-radius: 0 4px 4px 0;
  transition: width 260ms ease;
}

.agent-run-card--failed .agent-run-card__progress span {
  background: var(--ta-primary);
}

.agent-run-card__header {
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
  padding: 16px;
  background: #f8f9fa;
  border-bottom: 1px solid var(--ta-border);
}

.agent-run-card__title {
  display: flex;
  gap: 12px;
  align-items: center;
  min-width: 0;
}

.agent-run-card__title > .n-icon {
  flex: 0 0 auto;
  color: var(--ta-primary);
  font-size: 20px;
}

.agent-run-card__title h3 {
  margin: 0;
  color: var(--ta-text-strong);
  font-size: 18px;
  font-weight: var(--font-semibold);
  line-height: 26px;
}

.agent-status-chip {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  padding: 3px 8px;
  color: #1f2937;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
  background: #f3f4f6;
  border: 1px solid #d1d5db;
  border-radius: 999px;
}

.agent-status-chip__dot {
  width: 8px;
  height: 8px;
  background: var(--ta-primary);
  border-radius: 999px;
  animation: ta-run-pulse 2s infinite;
}

.agent-run-shell--waiting .agent-status-chip {
  color: #9a3412;
  background: #fffbeb;
  border-color: #fed7aa;
}

.agent-run-shell--waiting .agent-status-chip__dot {
  background: #f97316;
}

.agent-run-shell--completed .agent-status-chip {
  color: #047857;
  background: #ecfdf5;
  border-color: #a7f3d0;
}

.agent-run-shell--completed .agent-status-chip__dot {
  background: var(--ta-success);
}

.agent-run-card__meta {
  display: flex;
  flex-shrink: 0;
  gap: 16px;
  align-items: center;
  color: var(--ta-text-muted);
  font-size: 13px;
}

.agent-run-card__meta button {
  display: grid;
  width: 28px;
  height: 28px;
  padding: 0;
  color: var(--ta-text-muted);
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 8px;
  place-items: center;
}

.agent-run-card__meta button:hover {
  color: var(--ta-primary);
  background: #f3f4f5;
}

.agent-run-card__body {
  padding: 24px;
}

.agent-run-timeline {
  display: grid;
  gap: 0;
  padding: 0;
  margin: 0;
  list-style: none;
}

.timeline-item {
  position: relative;
  display: flex;
  gap: 12px;
  padding-bottom: 18px;
}

.timeline-item:last-child {
  padding-bottom: 0;
}

.timeline-line::before {
  position: absolute;
  top: 16px;
  bottom: -4px;
  left: 6px;
  z-index: 0;
  width: 1px;
  content: "";
  background: #e5e7eb;
}

.timeline-marker {
  z-index: 1;
  display: grid;
  flex: 0 0 14px;
  width: 14px;
  height: 14px;
  margin-top: 3px;
  color: #ffffff;
  font-size: 9px;
  font-weight: var(--font-semibold);
  background: #ffffff;
  border: 1px solid var(--ta-primary);
  border-radius: 999px;
  place-items: center;
}

.timeline-item--done .timeline-marker {
  color: #ffffff;
  background: #22c55e;
  border-color: #22c55e;
}

.timeline-item--waiting .timeline-marker {
  color: #ffffff;
  background: #f59e0b;
  border-color: #f59e0b;
}

.timeline-item--warning .timeline-marker {
  color: #ffffff;
  background: #f59e0b;
  border-color: #f59e0b;
}

.timeline-item--pending .timeline-marker {
  background: #d1d5db;
  border-color: #d1d5db;
}

.timeline-item--failed .timeline-marker {
  color: #ffffff;
  background: #ef4444;
  border-color: #ef4444;
}

.timeline-marker__pulse {
  width: 6px;
  height: 6px;
  background: #22c55e;
  border-radius: 999px;
  animation: ta-run-pulse 2s infinite;
}

.timeline-content {
  flex: 1;
  min-width: 0;
}

.preparation-clarification-waiting p,
.section-confirmation-waiting p,
.format-loss-waiting p {
  margin: 0;
  color: var(--ta-text-strong);
  font-size: 14px;
  font-weight: 600;
  line-height: 20px;
}

.preparation-clarification-waiting span,
.section-confirmation-waiting span,
.format-loss-waiting span {
  display: block;
  margin-top: 4px;
  color: var(--ta-text-muted);
  font-size: 13px;
  line-height: 18px;
}

.confirmation-receipt-card {
  overflow: hidden;
  background: #ffffff;
  border: 1px solid #dfe3e8;
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 10px rgba(15, 23, 42, 0.035);
}

.confirmation-receipt-card__header {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 10px 12px;
  color: inherit;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.confirmation-receipt-card__header:hover {
  background: #fcfcfd;
}

.confirmation-receipt-card__header:focus-visible {
  outline: 2px solid #94a3b8;
  outline-offset: -2px;
}

.confirmation-receipt-card__header-copy {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.confirmation-receipt-card__title {
  color: var(--ta-text-strong);
  font-size: 14px;
  font-weight: 650;
  line-height: 20px;
}

.confirmation-receipt-card__summary {
  color: #087f5b;
  font-size: 12px;
  line-height: 18px;
}

.confirmation-receipt-card__toggle {
  display: grid;
  flex: 0 0 24px;
  width: 24px;
  height: 24px;
  padding: 0;
  color: var(--ta-text-muted);
  place-items: center;
}

.confirmation-receipt-card__details {
  padding: 0 12px 10px;
}

.confirmation-receipt-card__details::before {
  display: block;
  height: 1px;
  margin: 0 0 8px;
  content: '';
  background: #e1e3e4;
}

.timeline-row {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: space-between;
}

.timeline-title-wrap {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  min-width: 0;
}

.timeline-title-wrap p,
.timeline-detail {
  margin: 0;
}

.timeline-title-wrap p {
  color: var(--ta-text-strong);
  font-size: 14px;
  font-weight: 600;
  line-height: 20px;
}

.timeline-item--running .timeline-title-wrap p {
  color: var(--ta-primary);
}

.timeline-item--pending .timeline-title-wrap p,
.timeline-duration,
.timeline-detail {
  color: var(--ta-text-muted);
}

.timeline-duration {
  flex-shrink: 0;
  font-size: 13px;
  line-height: 20px;
}

.timeline-detail {
  margin-top: 4px;
  font-size: 13px;
  line-height: 18px;
}

.understanding-result-panel,
.execution-plan-panel {
  display: grid;
  gap: 10px;
  padding: 12px;
  margin-top: 8px;
  background: #ffffff;
  border: 1px solid #dfe3e8;
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 10px rgba(15, 23, 42, 0.035);
}

.step-result-panel__header {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding-bottom: 8px;
  color: var(--ta-text-muted);
  font-size: 12px;
  font-weight: var(--font-medium);
  line-height: 18px;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-bottom: 1px solid #e1e3e4;
}

.step-result-panel__header:hover {
  color: var(--ta-text-strong);
}

.step-result-panel__header:focus-visible {
  outline: 2px solid #94a3b8;
  outline-offset: 2px;
}

.step-result-panel__header[aria-expanded='false'] {
  padding-bottom: 0;
  border-bottom-color: transparent;
}

.step-result-panel__summary {
  display: inline-flex;
  gap: 8px;
  align-items: center;
  margin-left: auto;
}

.step-result-panel__header small {
  font-size: 12px;
  font-weight: 600;
}

.step-result-panel__toggle {
  display: grid;
  flex: 0 0 24px;
  width: 24px;
  height: 24px;
  color: var(--ta-text-muted);
  place-items: center;
}

.understanding-result-panel p {
  margin: 0;
  color: var(--ta-text-strong);
  font-size: 13px;
  line-height: 20px;
}

.tool-runtime-card {
  width: 100%;
  box-sizing: border-box;
  min-width: 0;
  overflow: hidden;
  margin-top: 8px;
  background: #ffffff;
  border: 1px solid #dfe3e8;
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 10px rgba(15, 23, 42, 0.035);
}

.tool-runtime-card__header {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  min-width: 0;
  padding: 10px 12px;
  color: inherit;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
}

.tool-runtime-card__header:hover {
  background: #fcfcfd;
}

.tool-runtime-card__header:focus-visible {
  outline: 2px solid #94a3b8;
  outline-offset: -2px;
}

.tool-runtime-card__title {
  min-width: 0;
  overflow-wrap: anywhere;
}

.tool-runtime-card__status {
  margin-right: 6px;
  font-weight: var(--font-medium);
}

.tool-runtime-card__toggle {
  display: grid;
  flex: 0 0 24px;
  width: 24px;
  height: 24px;
  color: var(--ta-text-muted);
  place-items: center;
}

.tool-runtime-card--running .tool-runtime-card__status {
  color: var(--ta-primary);
}

.tool-runtime-card--success .tool-runtime-card__status {
  color: #047857;
}

.tool-runtime-card--failed .tool-runtime-card__status {
  color: #b91c1c;
}

.tool-runtime-card__content {
  padding: 0 12px 10px;
}

.tool-runtime-card__content::before {
  display: block;
  height: 1px;
  margin: 0 0 8px;
  content: '';
  background: #e1e3e4;
}

.confirmation-receipt-card__markdown > :deep(ul),
.tool-runtime-card__markdown > :deep(ul) {
  display: grid;
  gap: 4px;
  padding-left: 0;
  margin: 0;
  list-style: none;
}

.confirmation-receipt-card__markdown > :deep(ul > li),
.tool-runtime-card__markdown > :deep(ul > li) {
  position: relative;
  padding-left: 18px;
  list-style: none;
}

.confirmation-receipt-card__markdown > :deep(ul > li)::before,
.tool-runtime-card__markdown > :deep(ul > li)::before {
  position: absolute;
  top: 0;
  left: 0;
  color: var(--ta-primary);
  font-size: 12px;
  line-height: 19px;
  content: '✦';
}

.confirmation-receipt-card__markdown :deep(li),
.confirmation-receipt-card__markdown :deep(p),
.tool-runtime-card__markdown :deep(li),
.tool-runtime-card__markdown :deep(p) {
  min-width: 0;
  margin: 0;
  color: var(--ta-text-body);
  font-size: 13px;
  line-height: 19px;
  overflow-wrap: anywhere;
}

.tool-runtime-card__details {
  display: grid;
  gap: 10px;
  padding-top: 12px;
  margin-top: 12px;
  border-top: 1px solid #e1e3e4;
}

.tool-runtime-card__field {
  display: grid;
  gap: 6px;
}

.tool-runtime-card__label {
  color: var(--ta-text-muted);
  font-size: 11px;
  font-weight: var(--font-medium);
  line-height: 16px;
  text-transform: uppercase;
}

.tool-runtime-card__value {
  min-width: 0;
  padding: 8px 10px;
  margin: 0;
  color: var(--ta-text-strong);
  font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 18px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  background: #ffffff;
  border: 1px solid #e1e3e4;
  border-radius: 6px;
}

.execution-plan-panel ol {
  display: grid;
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.execution-plan-step {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr) auto;
  gap: 10px;
  align-items: start;
  padding: 8px;
  background: #ffffff;
  border: 1px solid #eef0f4;
  border-radius: 6px;
}

.execution-plan-step__index {
  display: grid;
  width: 22px;
  height: 22px;
  color: var(--ta-text-muted);
  font-size: 12px;
  font-weight: var(--font-medium);
  background: #eef0f4;
  border-radius: 999px;
  place-items: center;
}

.execution-plan-step__body {
  display: grid;
  gap: 2px;
  min-width: 0;
}

.execution-plan-step__body strong,
.execution-plan-step__body em {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.execution-plan-step__body strong {
  color: var(--ta-text-strong);
  font-size: 13px;
  font-weight: 600;
  line-height: 18px;
  white-space: nowrap;
}

.execution-plan-step__body em {
  color: var(--ta-text-muted);
  font-size: 12px;
  font-style: normal;
  line-height: 18px;
  white-space: normal;
}

.plan-step-status {
  padding: 2px 6px;
  color: var(--ta-text-muted);
  font-size: 11px;
  font-weight: var(--font-medium);
  line-height: 16px;
  white-space: nowrap;
  background: #f2f4f6;
  border-radius: 999px;
}

.execution-plan-step--done .execution-plan-step__index,
.execution-plan-step--done .plan-step-status {
  color: #047857;
  background: #dcfce7;
}

.execution-plan-step--running .execution-plan-step__index,
.execution-plan-step--running .plan-step-status {
  color: #1f2937;
  background: #e5e7eb;
}

.execution-plan-step--failed .execution-plan-step__index,
.execution-plan-step--failed .plan-step-status {
  color: #1f2937;
  background: #e5e7eb;
}

.execution-plan-step--superseded .execution-plan-step__index,
.execution-plan-step--superseded .plan-step-status {
  color: #6b7280;
  background: #f3f4f6;
}

.execution-plan-step--superseded .execution-plan-step__body strong {
  color: var(--ta-text-muted);
  text-decoration: line-through;
}

.tool-log-toggle {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  color: var(--ta-primary);
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  background: var(--ta-primary-fixed);
  border: 1px solid var(--ta-primary-fixed-dim);
  border-radius: 4px;
}

.tool-log-toggle:hover {
  color: #ffffff;
  background: var(--ta-primary);
}

.need-confirm-chip {
  padding: 2px 6px;
  color: #9a3412;
  font-size: 10px;
  font-weight: var(--font-medium);
  letter-spacing: 0.04em;
  text-transform: uppercase;
  background: #ffedd5;
  border-radius: 4px;
}

.retry-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: var(--font-medium);
  line-height: 18px;
  white-space: nowrap;
  flex-shrink: 0;
}

.retry-badge .material-symbols-outlined {
  font-size: 14px;
}

.retry-badge__label {
  letter-spacing: 0.02em;
}

.retry-badge__strategy {
  font-weight: 500;
  opacity: 0.85;
}

.retry-badge--retrying {
  background: #dbeafe;
  color: #1d4ed8;
  border: 1px solid #93c5fd;
}

.retry-badge--exhausted {
  background: #f3f4f6;
  color: #1f2937;
  border: 1px solid #d1d5db;
}

.retry-attempt-badge {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  padding: 2px 8px;
  color: #1f2937;
  font-size: 12px;
  font-weight: var(--font-medium);
  line-height: 18px;
  white-space: nowrap;
  background: #eef0f4;
  border: 1px solid #d7dce3;
  border-radius: 999px;
}

.retry-progress-panel__head {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  color: #92400e;
  font-size: 12px;
  font-weight: var(--font-medium);
  line-height: 18px;
}

.retry-progress-panel__title {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}

.retry-progress-panel__title .material-symbols-outlined {
  font-size: 16px;
}

.retry-progress-panel__count {
  padding: 2px 8px;
  font-size: 11px;
  color: #92400e;
  background: #fef3c7;
  border-radius: 999px;
}

.retry-progress-list {
  display: grid;
  gap: 6px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.retry-progress-item {
  display: grid;
  grid-template-columns: auto auto 1fr;
  gap: 8px;
  align-items: center;
  padding: 6px 8px;
  font-size: 12px;
  line-height: 18px;
  background: #ffffff;
  border: 1px solid #fde6a3;
  border-radius: 4px;
}

.retry-progress-item__index {
  padding: 2px 6px;
  color: #92400e;
  font-weight: var(--font-medium);
  background: #fef3c7;
  border-radius: 4px;
}

.retry-progress-item__strategy {
  padding: 2px 6px;
  font-size: 11px;
  font-weight: var(--font-medium);
  color: #1f2937;
  background: #f3f4f6;
  border-radius: 4px;
}

.retry-progress-item__backoff {
  color: var(--ta-text-muted);
  font-size: 11px;
  font-style: italic;
}

.retry-progress-item__reason {
  grid-column: 1 / -1;
  color: #b91c1c;
  font-size: 11px;
  line-height: 16px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.retry-progress-item--schema_feedback {
  border-left: 3px solid #7c3aed;
}

.retry-progress-item--backoff {
  border-left: 3px solid #2563eb;
}

.retry-progress-item--degrade {
  border-left: 3px solid #d97706;
}

.retry-progress-item--same_inputs {
  border-left: 3px solid #475569;
}

.retry-progress-item--hard_stop {
  border-left: 3px solid #b91c1c;
}

.section-strategy-node {
  overflow: hidden;
  background: #ffffff;
  border: 1px solid #c3c6d7;
  border-left: 4px solid var(--ta-primary);
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 10px rgba(15, 23, 42, 0.035);
}

.section-strategy-node__header {
  display: grid;
  gap: 12px;
  padding: 16px;
  background: #f7f9fb;
  border-bottom: 1px solid #c3c6d7;
}

.section-strategy-node__header h3 {
  margin: 0;
  color: var(--ta-text-strong);
  font-size: 16px;
  font-weight: var(--font-semibold);
  line-height: 22px;
}

.section-strategy-node__header p {
  margin: 0;
  color: var(--ta-text-muted);
  font-size: 13px;
  line-height: 18px;
}

.section-strategy-footer {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  padding: 12px 16px;
  background: #f2f4f6;
}

.section-strategy-actions button,
.section-strategy-expand,
.template-identified-row button {
  height: 32px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: var(--font-medium);
  cursor: pointer;
  border-radius: 4px;
}

.section-strategy-actions button {
  color: var(--ta-text-muted);
  background: #ffffff;
  border: 1px solid #c3c6d7;
}

.section-strategy-actions button:hover:not(:disabled),
.section-strategy-expand:hover,
.template-identified-row button:hover {
  background: #f3f4f6;
}

.template-identified-row,
.confirmed-section-summary {
  display: flex;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  color: var(--ta-text-muted);
  font-size: 13px;
  line-height: 18px;
  background: #f2f4f6;
  border: 1px solid #c3c6d7;
  border-radius: 4px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 10px rgba(15, 23, 42, 0.035);
}

.template-identified-row > div,
.confirmed-section-summary {
  min-width: 0;
}

.template-identified-row > div {
  display: flex;
  gap: 8px;
  align-items: center;
}

.template-identified-row .material-symbols-outlined,
.confirmed-section-summary .n-icon {
  flex: 0 0 auto;
  font-size: 16px;
}

.template-identified-row code {
  color: var(--ta-text-strong);
  font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.template-identified-row button {
  flex: 0 0 auto;
  height: 26px;
  color: var(--ta-primary);
  background: transparent;
  border: 0;
}

.section-strategy-node {
  border-color: var(--ta-primary);
  border-left-width: 1px;
}

.section-strategy-node__title-line {
  display: flex;
  gap: 16px;
  align-items: center;
  justify-content: space-between;
}

.section-strategy-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
}

.section-strategy-stat {
  padding: 2px 8px;
  font-size: 11px;
  font-weight: var(--font-medium);
  line-height: 16px;
  border-radius: 999px;
}

.section-strategy-stat--ai_generate {
  color: #17305c;
  background: #d3e4fe;
}

.section-strategy-stat--keep_template,
.section-strategy-stat--skip {
  color: var(--ta-text-muted);
  background: #eef0f4;
}

.section-strategy-stat--manual_fill {
  color: #1b3559;
  background: #d5e4f8;
}

.section-strategy-actions {
  display: flex;
  gap: 8px;
}

.section-strategy-actions button {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  height: 28px;
  font-weight: 600;
}

.section-strategy-actions button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.section-strategy-actions__generate {
  color: #ffffff;
  background: #2563eb;
  border: 1px solid #2563eb;
}

.section-strategy-actions__generate:hover:not(:disabled) {
  color: #ffffff;
  background: #1d4ed8;
  border-color: #1d4ed8;
}

.section-strategy-actions__reset {
  color: #475569;
  background: #ffffff;
  border: 1px solid #cbd5e1;
}

.section-strategy-actions__reset:hover:not(:disabled) {
  color: #334155;
  background: #f8fafc;
  border-color: #94a3b8;
}

.section-strategy-footer button {
  color: #ffffff;
  background: #0d0d0d;
  border: 1px solid #0d0d0d;
}

.section-strategy-footer button:hover:not(:disabled) {
  color: #ffffff;
  background: #262626;
  border-color: #262626;
}

.section-strategy-actions .material-symbols-outlined {
  font-size: 14px;
}

.section-strategy-actions__reset {
  margin-left: auto;
}

.section-strategy-table-wrap {
  position: relative;          /* sticky 的定位参考:thead 相对于这个容器固定 */
  max-height: 260px;
  overflow: auto;
}

.section-strategy-table {
  width: 100%;
  min-width: 600px;
  border-collapse: collapse;
}

.section-strategy-table th {
  position: sticky;            /* 滚动时表头始终可见 */
  top: 0;
  z-index: 1;                  /* 覆盖 tbody 内容,不遮挡其他层 */
  height: 36px;
  padding: 0 12px;
  color: var(--ta-text-muted);
  font-size: 12px;
  font-weight: var(--font-medium);
  text-align: left;
  background: #f2f4f6;
  border-bottom: 1px solid #c3c6d7;
}

.section-strategy-table th:nth-child(1) {
  width: 40%;
}

.section-strategy-table th:nth-child(2) {
  width: 25%;
}

.section-strategy-table th:nth-child(3) {
  width: 35%;
}

.section-strategy-table td {
  height: 44px;
  padding: 8px 12px;
  color: var(--ta-text-muted);
  font-size: 12px;
  line-height: 18px;
  border-bottom: 1px solid #eef0f4;
}

.section-strategy-table tr:hover td {
  background: #f7f9fb;
}

.section-strategy-table strong {
  color: var(--ta-text-strong);
  font-size: 13px;
  font-weight: 600;
}

.section-suggestion-chip {
  display: inline-flex;
  align-items: center;
  min-height: 24px;
  padding: 2px 8px;
  color: var(--ta-primary);
  background: var(--ta-surface-low);
  border-radius: 999px;
}

/* F022: user-constraint markers.  A pinned-row style + a small chip
   that surfaces why this row's suggested action diverges from the
   template default. */
.section-name-cell {
  display: flex;
  gap: 8px;
  align-items: center;
}

.section-user-constraint-chip {
  display: inline-flex;
  gap: 2px;
  align-items: center;
  padding: 1px 8px;
  font-size: 11px;
  font-weight: 600;
  line-height: 18px;
  color: #92400e;
  background: #fef3c7;
  border: 1px solid #fcd34d;
  border-radius: 999px;
}

.section-user-constraint-chip .material-symbols-outlined {
  font-size: 12px;
}

.section-row--user-constrained td {
  background: #fffbeb;
}

.section-row--user-constrained:hover td {
  background: #fef3c7;
}

.section-strategy-user-hint strong {
  color: #92400e;
}

.section-strategy-expand {
  display: block;
  width: 100%;
  color: var(--ta-primary);
  background: #ffffff;
  border: 0;
  border-bottom: 1px solid #c3c6d7;
}

.agent-run-card__collapsed {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  align-self: flex-start;
  width: auto;
  padding: 0 0 10px;
  text-align: left;
  cursor: pointer;
  color: var(--ta-text-muted);
  font-size: 14px;
  background: transparent;
  border: 0;
  border-bottom: 1px solid var(--ta-border);
}

.agent-run-card__collapsed > .n-icon {
  font-size: 15px;
}

.task-completion-summary {
  padding: 2px 0 4px;
  color: var(--ta-text-strong);
  font-size: 15px;
  line-height: 1.8;
}

.task-completion-summary :deep(*) {
  margin-top: 0;
}

.task-completion-summary :deep(*:last-child) {
  margin-bottom: 0;
}

.task-completion-summary :deep(p) {
  margin: 0;
}

.task-completion-summary :deep(ul),
.task-completion-summary :deep(ol) {
  margin: 8px 0 10px;
  padding-left: 22px;
}

.task-completion-summary :deep(li) {
  margin: 4px 0;
}

.task-completion-summary :deep(code) {
  padding: 1px 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace;
  font-size: 0.92em;
  background: #f0f2f5;
  border-radius: 4px;
}

.artifact-result-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.result-summary-card,
.artifact-card,
.error-card {
  padding: 20px;
  background: #ffffff;
  border: 1px solid var(--ta-border);
  border-radius: 12px;
  box-shadow: 0 1px 3px rgba(15, 23, 42, 0.1), 0 1px 2px rgba(15, 23, 42, 0.06);
}

.result-summary-card h4 {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 0 0 12px;
  color: var(--ta-text-strong);
  font-size: 14px;
  font-weight: var(--font-medium);
}

.result-summary-card h4 .n-icon {
  color: var(--ta-text-muted);
  font-size: 20px;
}

.result-summary-card__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 0;
  border-bottom: 1px solid #e1e3e4;
}

.result-summary-card__row:last-child {
  border-bottom: 0;
}

.result-summary-card__row span {
  color: var(--ta-text-muted);
  font-size: 13px;
}

.result-summary-card__row strong {
  color: var(--ta-text-strong);
  font-size: 14px;
}

.artifact-card {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  gap: 16px;
}

.artifact-card--previewable {
  cursor: pointer;
  transition: border-color 160ms ease, box-shadow 160ms ease, transform 160ms ease;
}

.artifact-card--previewable:hover {
  border-color: color-mix(in srgb, var(--ta-primary) 28%, var(--ta-border));
  box-shadow: 0 5px 16px rgba(15, 23, 42, 0.1);
  transform: translateY(-1px);
}

.artifact-card--previewable:focus-visible {
  outline: 2px solid var(--ta-primary);
  outline-offset: 3px;
}

.artifact-card__head {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  min-width: 0;
}

.artifact-card__icon {
  display: grid;
  flex: 0 0 40px;
  width: 40px;
  height: 40px;
  color: var(--ta-primary);
  background: var(--ta-surface-low);
  border-radius: 8px;
  place-items: center;
}

.artifact-card__icon-image {
  width: 24px;
  height: 24px;
  object-fit: contain;
}

.artifact-card p,
.artifact-card small {
  display: block;
  min-width: 0;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-card p {
  color: var(--ta-text-strong);
  font-size: 14px;
  font-weight: 600;
}

.artifact-card small {
  margin-top: 4px;
  color: var(--ta-text-muted);
  font-size: 13px;
}

.error-card {
  grid-column: 1 / -1;
  border-color: rgba(186, 26, 26, 0.24);
}

.error-card h4 {
  margin: 0 0 6px;
  color: var(--ta-error);
}

.error-card p {
  margin: 0 0 12px;
  color: var(--ta-text-muted);
}

@keyframes ta-run-pulse {
  0% {
    box-shadow: 0 0 0 0 rgba(0, 0, 0, 0.28);
    transform: scale(0.95);
  }

  70% {
    box-shadow: 0 0 0 6px rgba(0, 0, 0, 0);
    transform: scale(1);
  }

  100% {
    box-shadow: 0 0 0 0 rgba(0, 0, 0, 0);
    transform: scale(0.95);
  }
}

@media (max-width: 760px) {
  .agent-run-card__header,
  .timeline-row {
    align-items: flex-start;
    flex-direction: column;
  }

  .agent-run-card__meta {
    flex-wrap: wrap;
    gap: 8px;
  }

  .artifact-result-grid {
    grid-template-columns: 1fr;
  }

  .agent-run-card__collapsed {
    font-size: 13px;
  }

  .task-completion-summary {
    font-size: 14px;
  }
}

.format-loss-banner {
  margin-bottom: 16px;
  padding: 16px;
  color: #121c28;
  background: #ffffff;
  border: 1px solid #c3c6d7;
  border-radius: 8px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04), 0 4px 10px rgba(15, 23, 42, 0.035);
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.format-loss-banner__header {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  justify-content: space-between;
}

.format-loss-banner__header h4 {
  margin: 0;
  color: #121c28;
  font-size: 16px;
  font-weight: 600;
  line-height: 24px;
}

.format-loss-banner__badges {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  justify-content: flex-end;
}

.format-loss-banner__badge {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  color: #434655;
  font-size: 12px;
  font-weight: 600;
  line-height: 1;
  white-space: nowrap;
  background: #d9e3f4;
  border-radius: 4px;
}

.format-loss-banner__badge--warning {
  color: #2a1700;
  background: #ffddb8;
}

.format-loss-banner__countdown {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  color: #784b00;
  font-size: 12px;
  line-height: 1;
  white-space: nowrap;
  background: #fff7ed;
  border-radius: 4px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
}

.format-loss-banner__table-wrap {
  overflow-x: auto;
  border: 1px solid #c3c6d7;
  border-radius: 4px;
}

.format-loss-banner__table {
  width: 100%;
  min-width: 620px;
  border-collapse: collapse;
  text-align: left;
}

.format-loss-banner__table th {
  width: 33.333%;
  padding: 10px 16px;
  color: #121c28;
  font-size: 13px;
  font-weight: 600;
  line-height: 18px;
  background: #d9e3f4;
  border-bottom: 1px solid #c3c6d7;
}

.format-loss-banner__table td {
  padding: 12px 16px;
  color: #434655;
  font-size: 13px;
  font-weight: 400;
  line-height: 20px;
  vertical-align: top;
  background: #ffffff;
  border-bottom: 1px solid #eef0f4;
}

.format-loss-banner__table tr:last-child td {
  border-bottom: 0;
}

.format-loss-banner__table tr:hover td {
  background: #f8f9ff;
}

.format-loss-banner__footer {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: flex-end;
  padding-top: 16px;
  margin-top: -8px;
  border-top: 1px solid #c3c6d7;
}

.format-loss-banner__btn {
  min-height: 38px;
  padding: 8px 16px;
  border-radius: 8px;
  border: 1px solid transparent;
  font-size: 14px;
  font-weight: 600;
  line-height: 20px;
  cursor: pointer;
  transition: background-color 120ms ease, border-color 120ms ease, opacity 120ms ease;
}

.format-loss-banner__btn:disabled {
  filter: grayscale(0.4);
  opacity: 0.6;
  cursor: not-allowed;
}

.format-loss-banner__btn--accept {
  color: #ffffff;
  background: #006c49;
  border-color: #006c49;
}

.format-loss-banner__btn--retry {
  color: #434655;
  background: transparent;
  border-color: #737686;
}

.format-loss-banner__btn--accept:hover:not(:disabled) {
  background: #005236;
  border-color: #005236;
}

.format-loss-banner__btn--retry:hover:not(:disabled) {
  background: #f8f9ff;
}

.format-loss-banner__error {
  margin: 0;
  font-size: 12px;
  color: #cf1322;
}

.tool-thinking {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  color: var(--ta-text-secondary, #5e5e5e);
  font-size: 13px;
  font-style: italic;
}

.tool-thinking__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ta-text-secondary, #5e5e5e);
  animation: thinking-pulse 1.5s ease-in-out infinite;
}

/* Typography V3 keeps runtime content readable without changing its stateful layout. */
.agent-run-card__title h3,
.section-strategy-node__header h3,
.format-loss-banner__header h4,
.result-summary-card h4,
.error-card h4 {
  color: var(--text-primary);
  font-size: var(--text-md);
  font-weight: var(--font-semibold);
  line-height: var(--leading-ui);
}

.timeline-title-wrap p,
.tool-runtime-card__title,
.execution-plan-step__body strong,
.retry-progress-panel__title,
.section-strategy-table strong,
.result-summary-card__row strong {
  color: #171717;
  font-size: var(--text-md);
  font-weight: var(--font-semibold);
  line-height: var(--leading-ui);
}

.timeline-detail,
.tool-runtime-card p,
.execution-plan-step__body,
.retry-progress-item__reason,
.section-strategy-table td,
.result-summary-card__row,
.tool-thinking {
  color: #333333;
  font-size: var(--text-md);
  font-weight: var(--font-regular);
  line-height: var(--leading-md);
}

.agent-run-card__meta,
.timeline-duration,
.tool-runtime-card__details,
.retry-progress-item__backoff,
.format-loss-banner__countdown {
  color: var(--text-tertiary);
  font-size: var(--text-meta);
  font-weight: var(--font-regular);
  font-variant-numeric: tabular-nums;
  line-height: var(--leading-meta);
}

.agent-status-chip,
.tool-runtime-card__status,
.plan-step-status,
.need-confirm-chip,
.retry-badge,
.retry-attempt-badge,
.format-loss-banner__badge {
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  letter-spacing: 0;
  line-height: var(--leading-xs);
}

.tool-runtime-card__value,
.template-identified-row code,
.task-completion-summary :deep(code) {
  font-family: var(--font-mono);
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
}

.task-completion-summary {
  color: var(--text-body);
  font-size: var(--text-body-size);
  font-weight: var(--font-regular);
  line-height: var(--leading-body);
}

.task-completion-summary :deep(strong) {
  color: var(--text-primary);
  font-weight: var(--font-semibold);
}

.section-strategy-table th,
.format-loss-banner__table th {
  font-size: var(--text-meta);
  font-weight: var(--font-medium);
  line-height: var(--leading-meta);
}

.format-loss-banner__btn,
.section-strategy-actions button {
  font-size: var(--text-ui);
  font-weight: var(--font-medium);
  line-height: var(--leading-ui);
}

@keyframes thinking-pulse {
  0%, 100% {
    opacity: 0.4;
  }
  50% {
    opacity: 1;
  }
}
</style>
