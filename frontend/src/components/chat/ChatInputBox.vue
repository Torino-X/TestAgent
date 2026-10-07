<template>
  <div ref="dockRef" class="input-dock">
    <div class="input-stack">
      <div v-if="files.length" class="input-panel__files">
        <FileAttachmentCard
          v-for="file in files"
          :key="file.id"
          :file="file"
          variant="floating"
          @remove="removeFile"
          @open="openFile"
        />
      </div>

      <div ref="panelRef" class="input-panel">
        <div class="input-panel__textarea-wrap">
          <textarea
            ref="textareaRef"
            v-model="text"
            class="input-panel__textarea"
            :disabled="disabled"
            :placeholder="disabled ? busyText : '上传需求文档和模板，输入生成目标...'"
            rows="1"
            @input="handleTextInput"
            @paste="handlePaste"
            @compositionend="adjustTextareaHeight"
            @keydown.enter.exact.prevent="submit"
          />

        </div>

        <div class="input-panel__actions">
          <div class="input-panel__tools">
            <button
              class="input-panel__tool-button input-panel__upload-button"
              type="button"
              :disabled="disabled"
              aria-label="上传文件"
            >
              <n-icon class="input-panel__tool-icon" :component="AttachOutline" />
              <input
                class="input-panel__file-input"
                type="file"
                multiple
                :disabled="disabled"
                @change="handleFileChange"
              />
            </button>
            <div ref="templateMenuAnchorRef" class="template-menu-anchor">
              <button
                class="input-panel__tool-button input-panel__template-button"
                :class="{ 'input-panel__template-button--active': templateMenuOpen }"
                type="button"
                :disabled="disabled"
                aria-label="选择模板"
                aria-haspopup="menu"
                :aria-expanded="templateMenuOpen"
                @click="templateMenuOpen = !templateMenuOpen"
              >
                <n-icon class="input-panel__tool-icon" :component="GridOutline" />
              </button>
              <div v-if="templateMenuOpen" class="template-quick-menu" role="menu" aria-label="模板快捷菜单">
                <button type="button" role="menuitem" @click="selectTemplateMenu('mine')"><n-icon :component="DocumentsOutline" /><span><strong>我的模板</strong><small>选择模板加入当前对话</small></span></button>
                <button type="button" role="menuitem" @click="selectTemplateMenu('market')"><n-icon :component="StorefrontOutline" /><span><strong>模板市场</strong><small>发现更多测试模板</small></span></button>
                <button type="button" role="menuitem" @click="selectTemplateMenu('library')"><n-icon :component="CloudUploadOutline" /><span><strong>资料库上传</strong><small>选择资料库文档加入对话</small></span></button>
              </div>
            </div>
            <button v-if="false"
              class="input-panel__tool-button input-panel__knowledge-button"
              :class="{ 'input-panel__knowledge-button--active': knowledgeMode === 'MAAS_STRICT' }"
              type="button"
              :disabled="disabled"
              :aria-label="knowledgeModeTitle"
              :aria-pressed="knowledgeMode === 'MAAS_STRICT'"
              :title="knowledgeModeTitle"
              data-source-icon="知识库.svg"
              @click="emit('toggle-knowledge-mode')"
            >
              <img class="input-panel__knowledge-icon" :src="knowledgeBaseIconUrl" alt="" />
            </button>
          </div>

          <div class="input-panel__right">
            <span class="input-panel__model-name">{{ contextModelName }}</span>
            <div ref="contextUsageAnchorRef" class="context-usage-anchor">
              <button
                class="context-usage-ring"
                :class="contextRingClasses"
                type="button"
                :aria-label="contextRingAriaLabel"
                :title="contextRingTitle"
                :style="contextRingStyle"
                data-source-icon="圆环.svg"
                @click="toggleContextPopover"
              >
              </button>
              <div v-if="contextPopoverVisible" class="context-usage-popover" role="dialog" aria-label="上下文使用情况">
                <div class="context-usage-popover__header">
                  <span class="context-usage-popover__title">上下文使用情况</span>
                  <span class="context-usage-popover__total" :class="{ 'context-usage-popover__total--high': contextTone === 'high' }">
                    {{ contextUsageTotal }}
                  </span>
                </div>
                <p v-if="contextUsageUnavailable" class="context-usage-popover__note">上下文用量暂不可用</p>
                  <p v-else-if="contextUsageFallback" class="context-usage-popover__note">尚未生成可用的上下文快照；当前仅估算最近对话，未包含附件、任务和系统指令。</p>
                <p v-else-if="contextUsageUnknown" class="context-usage-popover__note">当前模型未配置上下文窗口</p>
                <div v-else class="context-usage-popover__meter">
                  <span class="context-usage-popover__meter-fill" :style="{ width: `${contextVisualPercent ?? 0}%` }"></span>
                </div>
                <div v-if="contextUsagePercentText" class="context-usage-popover__percent">
                  {{ contextUsagePercentText }}
                </div>
                <div class="context-usage-popover__rows">
                  <div
                    v-for="item in contextBreakdownRows"
                    :key="item.key"
                    class="context-usage-popover__row"
                  >
                    <span class="context-usage-popover__row-label">
                      <img
                        class="context-usage-popover__row-icon"
                        :src="item.icon"
                        :alt="`${item.label} icon`"
                        :data-source-icon="item.iconFile"
                      />
                      {{ item.label }}
                    </span>
                    <span class="context-usage-popover__row-value">
                      <span>{{ item.value }}</span>
                      <span class="context-usage-popover__row-percent">{{ item.percent }}</span>
                    </span>
                  </div>
                </div>
                <div v-if="contextUnattributedTokens > 0" class="context-usage-popover__row context-usage-popover__overhead">
                  <span class="context-usage-popover__row-label">调用封装与未归类内容</span>
                  <span class="context-usage-popover__row-value">
                    <span>{{ formatTokenCount(contextUnattributedTokens) }}</span>
                    <span class="context-usage-popover__row-percent">{{ formatTokenSharePercent(contextUnattributedTokens, contextUsage?.usage.used_tokens) }}</span>
                  </span>
                </div>
                <section v-if="contextEvidenceReceipts.length" class="context-usage-popover__receipt" aria-label="上下文收据">
                  <div class="context-usage-popover__receipt-title">本任务上下文收据（{{ contextEvidenceReceipts.length }} 次调用）</div>
                  <button
                    class="context-usage-popover__receipt-copy"
                    type="button"
                    :aria-label="contextEvidenceCopyLabel"
                    :title="contextEvidenceCopyLabel"
                    @click="copyContextEvidenceReceipts"
                  >
                    <svg viewBox="0 0 24 24" aria-hidden="true">
                      <rect x="9" y="3" width="11" height="14" rx="2"></rect>
                      <path d="M15 17v2a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3"></path>
                    </svg>
                  </button>
                  <span class="context-usage-popover__receipt-copy-status" aria-live="polite">{{ contextEvidenceCopyStatus }}</span>
                  <div class="context-usage-popover__receipt-content">
                    <div v-for="receipt in contextEvidenceReceipts" :key="receipt.snapshot_public_id" class="context-usage-popover__receipt-call">
                    <p class="context-usage-popover__receipt-note">
                      调用：<code>{{ receipt.call_site }}</code>；已纳入 {{ receipt.included_source_count }} 项来源
                      <span v-if="receipt.dropped_source_count">，已舍弃 {{ receipt.dropped_source_count }} 项</span>
                    </p>
                    <ul v-if="receipt.included_sources.length" class="context-usage-popover__receipt-list">
                      <li v-for="source in receipt.included_sources" :key="`${source.kind}:${source.source_type}:${source.reference}`">
                        <span>{{ contextEvidenceKindLabel(source.kind) }} · {{ source.source_type }}</span>
                        <code v-if="source.reference">{{ source.reference }}</code>
                      </li>
                    </ul>
                    </div>
                  </div>
                </section>
                <button
                  class="context-usage-popover__compact"
                  type="button"
                  :disabled="!contextCompactEnabled"
                  @click="emit('compact-context')"
                >
                  <span v-if="contextCompacting" class="context-usage-popover__spinner" aria-hidden="true"></span>
                  {{ contextCompactButtonText }}
                </button>
              </div>
            </div>

            <button
              class="input-panel__primary-button"
              :class="primaryButtonClasses"
              type="button"
              :disabled="disabled || (primaryActionIsSend && (sendDisabled || !text.trim()))"
              :aria-label="primaryButtonAriaLabel"
              @click="handlePrimaryAction"
            >
              <n-icon class="input-panel__primary-icon" :component="primaryActionIsSend ? ArrowUpOutline : Stop" />
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { NIcon } from 'naive-ui'
import { ArrowUpOutline, AttachOutline, CloudUploadOutline, DocumentsOutline, GridOutline, Stop, StorefrontOutline } from '@vicons/ionicons5'
import type { ContextUsageResponse, FileAttachment, KnowledgeMode } from '@/types'
import FileAttachmentCard from '@/components/cards/FileAttachmentCard.vue'
import contextRingIconUrl from '@/assets/icons/圆环.svg'
import conversationHistoryIconUrl from '@/assets/context-usage/对话历史.svg'
import projectDocumentsIconUrl from '@/assets/context-usage/项目资料.svg'
import taskContextIconUrl from '@/assets/context-usage/任务上下文.svg'
import memoryIconUrl from '@/assets/context-usage/Memory.svg'
import systemInstructionsIconUrl from '@/assets/context-usage/系统指令.svg'

const knowledgeBaseIconUrl = '/src/assets/context-usage/知识库.svg'
import { extractClipboardFiles } from '@/utils/clipboardFiles'
import { copyMarkdownText } from '@/utils/clipboard'
import { formatContextEvidenceReceipts } from '@/utils/contextEvidenceReceipts'
import type { ContextCompactionUiState } from '@/composables/useContextUsage'
import {
  CONTEXT_USAGE_BREAKDOWN_ITEMS,
  contextUsageTone,
  formatTokenCount,
  formatTokenSharePercent,
  formatUsagePercent,
  formatUsageTotal,
  isContextUsageFallback,
  isContextUsageUnknown,
  visualContextPercent
} from '@/utils/contextUsage'
import { resetTextareaHeight, resizeTextareaToContent } from '@/utils/textareaAutosize'

const props = withDefaults(
  defineProps<{
    files?: FileAttachment[]
    disabled?: boolean
    sendDisabled?: boolean
    responseActive?: boolean
    runtimeActive?: boolean
    busyText?: string
    draftText?: string
    modelName?: string
    contextUsage?: ContextUsageResponse | null
    contextUsageLoading?: boolean
    contextUsageUnavailable?: boolean
    contextCompacting?: boolean
    contextCompactionState?: ContextCompactionUiState
    contextPopoverOpen?: boolean
    knowledgeMode?: KnowledgeMode
  }>(),
  {
    files: () => [],
    disabled: false,
    sendDisabled: false,
    responseActive: false,
    runtimeActive: false,
    busyText: '',
    draftText: '',
    modelName: '',
    contextUsage: null,
    contextUsageLoading: false,
    contextUsageUnavailable: false,
    contextCompacting: false,
    contextCompactionState: 'idle',
    contextPopoverOpen: false,
    knowledgeMode: 'AUTO'
  }
)

const emit = defineEmits<{
  upload: [files: File[]]
  send: [text: string]
  'draft-change': [text: string]
  stop: []
  'remove-file': [fileId: string]
  'open-file': [file: FileAttachment]
  'paste-files': [files: File[]]
  'context-popover-open': []
  'compact-context': []
  'toggle-knowledge-mode': []
  'open-template-picker': []
  'open-template-market': []
  'open-library-picker': []
  'dock-layout': [layout: { panelTopOffset: number }]
}>()

const text = ref('')
const textareaRef = ref<HTMLTextAreaElement | null>(null)
const dockRef = ref<HTMLElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)
const contextUsageAnchorRef = ref<HTMLElement | null>(null)
const templateMenuAnchorRef = ref<HTMLElement | null>(null)
const templateMenuOpen = ref(false)
const localContextPopoverOpen = ref(false)
const contextEvidenceCopyStatus = ref('')
let contextEvidenceCopyStatusTimer: ReturnType<typeof setTimeout> | undefined
let dockLayoutObserver: ResizeObserver | null = null
const textareaSize = {
  minHeight: 27,
  maxHeight: 180
}

const contextPopoverVisible = computed(() => props.contextPopoverOpen || localContextPopoverOpen.value)
const contextUsageUnknown = computed(() => isContextUsageUnknown(props.contextUsage))
const contextUsageFallback = computed(() => isContextUsageFallback(props.contextUsage))
const contextVisualPercent = computed(() => visualContextPercent(props.contextUsage))
const contextTone = computed(() => contextUsageTone(props.contextUsage, props.contextUsageUnavailable))
const contextModelName = computed(() => (props.contextUsage?.model.name ?? props.modelName.trim()) || '模型未知')
const knowledgeModeTitle = computed(() =>
  props.knowledgeMode === 'MAAS_STRICT'
    ? '知识库：仅依据公司知识库回答'
    : '知识库：智能模式'
)
const runningState = computed(() => props.responseActive || props.runtimeActive)
const hasDraftText = computed(() => !!text.value.trim())
const primaryActionIsSend = computed(() => !runningState.value || hasDraftText.value)
const contextUsageTotal = computed(() => {
  if (props.contextUsageUnavailable) return '上下文用量暂不可用'
  return formatUsageTotal(props.contextUsage)
})
const contextUsagePercentText = computed(() => formatUsagePercent(props.contextUsage))
const contextUnattributedTokens = computed(() => Math.max(0, props.contextUsage?.usage.unattributed_tokens ?? 0))
const contextEvidenceReceipts = computed(() => {
  if (!props.contextUsage?.debug_details_enabled) return []
  const receipts = props.contextUsage?.recent_evidence_receipts
  if (receipts?.length) return receipts
  return props.contextUsage?.evidence_receipt ? [props.contextUsage.evidence_receipt] : []
})
const contextEvidenceReceiptText = computed(() => formatContextEvidenceReceipts(contextEvidenceReceipts.value))
const contextEvidenceCopyLabel = computed(() => contextEvidenceCopyStatus.value || '复制全部上下文收据')
const contextRingAriaLabel = computed(() => `查看上下文用量，${contextUsageTotal.value}`)
const contextRingTitle = computed(() => contextUsageTotal.value)
const primaryButtonClasses = computed(() => ({
  'input-panel__primary-button--send': primaryActionIsSend.value,
  'input-panel__primary-button--stop': !primaryActionIsSend.value
}))
const primaryButtonAriaLabel = computed(() => {
  if (primaryActionIsSend.value) return '发送'
  return '停止任务'
})
const contextRingStyle = computed(() => ({
  '--context-ring-progress': `${(contextVisualPercent.value ?? 0) * 3.6}deg`,
  '--context-ring-mask': `url("${contextRingIconUrl}")`
}))
const contextRingClasses = computed(() => ({
  'context-usage-ring--warning': contextTone.value === 'warning',
  'context-usage-ring--high': contextTone.value === 'high',
  'context-usage-ring--unknown': contextTone.value === 'unknown',
  'context-usage-ring--unavailable': contextTone.value === 'unavailable',
  // Opening the usage popover refreshes its data, but that is not a compaction.
  // Keep the ring still unless the user has actually started a compaction run.
  'context-usage-ring--loading': props.contextCompacting
}))
const contextBreakdownIcons = {
  conversation_history: { url: conversationHistoryIconUrl, file: '对话历史.svg' },
  project_documents: { url: projectDocumentsIconUrl, file: '项目资料.svg' },
  task_context: { url: taskContextIconUrl, file: '任务上下文.svg' },
  user_memory: { url: memoryIconUrl, file: 'Memory.svg' },
  system_instructions: { url: systemInstructionsIconUrl, file: '系统指令.svg' }
}
const contextBreakdownRows = computed(() =>
  CONTEXT_USAGE_BREAKDOWN_ITEMS.map((item) => {
    const tokenCount = props.contextUsage?.breakdown[item.key] ?? 0
    return {
      key: item.key,
      label: item.label,
      icon: contextBreakdownIcons[item.key].url,
      iconFile: contextBreakdownIcons[item.key].file,
      value: formatTokenCount(tokenCount),
      percent: formatTokenSharePercent(tokenCount, props.contextUsage?.usage.used_tokens)
    }
  })
)
function contextEvidenceKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    conversation: '对话历史',
    current_goal: '当前目标',
    task_state: '任务状态',
    evidence: '任务证据',
    knowledge: '项目资料',
    memory: 'Memory',
    system_rules: '系统规则',
    project_instructions: '项目指令',
    call_contract: '调用契约'
  }
  return labels[kind] ?? kind
}
const contextCompactEnabled = computed(
  () =>
    !!props.contextUsage?.compaction.available &&
    !props.contextUsage?.compaction.in_progress &&
    !props.contextCompacting &&
    !props.contextUsageUnavailable
)
const contextCompactButtonText = computed(() => {
  if (props.contextCompacting || props.contextCompactionState === 'loading') return '压缩中...'
  if (props.contextCompactionState === 'success') return '压缩成功'
  return '压缩上下文'
})

watch(
  () => props.draftText,
  (value) => {
    text.value = value
    scheduleTextareaResize()
  }
)

onMounted(() => {
  resetTextarea()
  document.addEventListener('pointerdown', handleDocumentPointerDown)
  void nextTick(() => {
    emitDockLayout()
    if (typeof ResizeObserver === 'undefined') return
    dockLayoutObserver = new ResizeObserver(emitDockLayout)
    if (dockRef.value) dockLayoutObserver.observe(dockRef.value)
    if (panelRef.value) dockLayoutObserver.observe(panelRef.value)
  })
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', handleDocumentPointerDown)
  if (contextEvidenceCopyStatusTimer) clearTimeout(contextEvidenceCopyStatusTimer)
  dockLayoutObserver?.disconnect()
  dockLayoutObserver = null
})

function emitDockLayout() {
  const dockRect = dockRef.value?.getBoundingClientRect()
  const panelRect = panelRef.value?.getBoundingClientRect()
  if (!dockRect || !panelRect) return

  // The floating interaction cards must clear the visible composer panel,
  // rather than the dock's translucent top padding. This also keeps the gap
  // correct when the textarea grows or attachments change the dock layout.
  const panelTopOffset = Math.ceil(dockRect.bottom - panelRect.top)
  if (panelTopOffset > 0) emit('dock-layout', { panelTopOffset })
}

function scheduleTextareaResize() {
  void nextTick(() => {
    adjustTextareaHeight()
  })
}

function adjustTextareaHeight() {
  if (!textareaRef.value) return
  resizeTextareaToContent(textareaRef.value, textareaSize)
}

function handleTextInput() {
  adjustTextareaHeight()
  emit('draft-change', text.value)
}

function resetTextarea() {
  if (!textareaRef.value) return
  resetTextareaHeight(textareaRef.value, textareaSize)
}

function handleFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const selectedFiles = Array.from(input.files ?? [])
  if (selectedFiles.length) emit('upload', selectedFiles)
  input.value = ''
}

function handlePaste(event: ClipboardEvent) {
  const files = extractClipboardFiles(event)
  if (!files.length) return
  event.preventDefault()
  emit('paste-files', files)
  scheduleTextareaResize()
}

function submit() {
  const value = text.value.trim()
  if (
    !value
    || props.disabled
    || props.sendDisabled
  ) return
  emit('send', value)
  text.value = ''
  emit('draft-change', '')
  scheduleTextareaResize()
}

function handlePrimaryAction() {
  if (!primaryActionIsSend.value) {
    emit('stop')
    return
  }
  submit()
}

function toggleContextPopover() {
  localContextPopoverOpen.value = !localContextPopoverOpen.value
  if (localContextPopoverOpen.value) emit('context-popover-open')
}

async function copyContextEvidenceReceipts() {
  const result = await copyMarkdownText(contextEvidenceReceiptText.value)
  contextEvidenceCopyStatus.value = result === 'success' || result === 'fallback_success' ? '已复制' : '复制失败'
  if (contextEvidenceCopyStatusTimer) clearTimeout(contextEvidenceCopyStatusTimer)
  contextEvidenceCopyStatusTimer = setTimeout(() => {
    contextEvidenceCopyStatus.value = ''
  }, 1800)
}

function handleDocumentPointerDown(event: PointerEvent) {
  const target = event.target
  const anchor = contextUsageAnchorRef.value
  if (localContextPopoverOpen.value && anchor && target instanceof Node && !anchor.contains(target)) {
    localContextPopoverOpen.value = false
  }
  const templateAnchor = templateMenuAnchorRef.value
  if (templateMenuOpen.value && templateAnchor && target instanceof Node && !templateAnchor.contains(target)) templateMenuOpen.value = false
}

function selectTemplateMenu(target: 'mine' | 'market' | 'library') {
  templateMenuOpen.value = false
  if (target === 'mine') emit('open-template-picker')
  else if (target === 'market') emit('open-template-market')
  else emit('open-library-picker')
}

function removeFile(fileId: string) {
  emit('remove-file', fileId)
}

function openFile(file: FileAttachment) {
  emit('open-file', file)
}
</script>

<style scoped>
.input-dock {
  position: absolute;
  z-index: 20;
  right: 0;
  bottom: 0;
  left: 0;
  display: flex;
  justify-content: center;
  padding: 40px 24px 24px;
  pointer-events: none;
  background: linear-gradient(180deg, rgba(249, 249, 249, 0), var(--ta-background) 34%);
}

.input-stack {
  width: min(100%, 780px);
  pointer-events: auto;
}

.input-panel {
  width: 100%;
  padding: 0;
  pointer-events: auto;
  overflow: visible;
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-radius: 18px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease;
}

.input-panel__textarea-wrap {
  min-height: 62px;
  padding: 10px 16px;
}

.input-panel__tool-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  padding: 4px;
  color: #424654;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 4px;
  transition:
    color 160ms ease,
    background 160ms ease;
}

.input-panel__tool-button:hover:not(:disabled),
.input-panel__tool-button:focus-visible:not(:disabled) {
  color: #191c1f;
  background: #f2f4f8;
  outline: none;
}

.input-panel__tool-button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.input-panel__tool-icon {
  font-size: 20px;
}

.input-panel__knowledge-button--active {
  color: #191c1f;
  background: #eef2ff;
}

.input-panel__knowledge-icon {
  display: block;
  width: 20px;
  height: 20px;
}

.input-panel__upload-button {
  position: relative;
  overflow: hidden;
}

.input-panel__file-input {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  cursor: pointer;
  opacity: 0;
}

.input-panel:focus-within {
  border-color: #c3c6d7;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
}

.input-panel__files {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  align-items: start;
  padding: 0 0 16px;
  overflow: visible;
}

.input-panel__textarea {
  display: block;
  width: 100%;
  min-height: 27px;
  max-height: 180px;
  padding: 0;
  color: var(--text-body);
  resize: none;
  background: transparent;
  border: 0;
  outline: 0;
  font-size: var(--text-body-size);
  font-weight: var(--font-regular);
  line-height: 24px;
  overflow-y: hidden;
}

.input-panel__textarea::placeholder {
  color: #777777;
}

.input-panel__actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 40px;
  padding: 6px 8px;
  background: #ffffff;
  border-radius: 0 0 18px 18px;
}

.input-panel__tools {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 1 1 auto;
  min-width: 0;
}

.template-menu-anchor {
  position: relative;
  display: flex;
}

.input-panel__template-button--active {
  color: #171717;
  background: #eeeeec;
}

.template-quick-menu {
  position: absolute;
  bottom: 42px;
  left: -8px;
  z-index: 30;
  width: 242px;
  padding: 6px;
  background: #ffffff;
  border: 1px solid #e3e3e3;
  border-radius: 11px;
  box-shadow: 0 14px 38px rgba(0, 0, 0, .13);
}

.template-quick-menu button {
  display: grid;
  grid-template-columns: 28px 1fr;
  gap: 8px;
  align-items: center;
  width: 100%;
  padding: 9px;
  color: #252525;
  text-align: left;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 8px;
}

.template-quick-menu button:hover { background: #f5f5f3; }
.template-quick-menu .n-icon { color: #666; font-size: 19px; }
.template-quick-menu strong, .template-quick-menu small { display: block; }
.template-quick-menu strong { font-size: 13px; font-weight: 570; }
.template-quick-menu small { margin-top: 2px; color: #888; font-size: 11px; }

.input-panel__right {
  display: flex;
  align-items: center;
  flex: 0 0 auto;
  gap: 10px;
  min-width: 0;
}

.input-panel__model-name {
  flex: 0 1 auto;
  max-width: 180px;
  padding: 0 0 0 2px;
  overflow: hidden;
  color: #555555;
  font-size: 13px;
  font-weight: var(--font-regular);
  line-height: var(--leading-meta);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.input-panel__primary-button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 32px;
  height: 32px;
  padding: 0;
  color: #ffffff;
  cursor: pointer;
  background: #191c1f;
  border: 0;
  border-radius: 999px;
  transition:
    opacity 160ms ease,
    background 160ms ease,
    transform 160ms ease;
}

.input-panel__primary-button:hover:not(:disabled),
.input-panel__primary-button:focus-visible:not(:disabled) {
  background: #2d3134;
  outline: none;
}

.input-panel__primary-button:active:not(:disabled) {
  transform: translateY(1px);
}

.input-panel__primary-button:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.input-panel__primary-icon {
  font-size: 18px;
}

.context-usage-anchor {
  position: relative;
  display: flex;
  align-items: center;
}

.context-usage-ring {
  --context-ring-progress: 0deg;

  position: relative;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  cursor: pointer;
  background:
    conic-gradient(#3d78f6 var(--context-ring-progress), #d1d5db 0),
    #ffffff;
  border: 0;
  border-radius: 999px;
  mask: var(--context-ring-mask) center / contain no-repeat;
  -webkit-mask: var(--context-ring-mask) center / contain no-repeat;
}

.context-usage-ring__icon {
  display: none;
}

.context-usage-ring--high {
  background:
    conic-gradient(#ba1a1a var(--context-ring-progress), #ffdad6 0),
    #ffffff;
}

.context-usage-ring--warning {
  background:
    conic-gradient(#d97706 var(--context-ring-progress), #ffedd5 0),
    #ffffff;
}

.context-usage-ring--unknown {
  background: #d1d5db;
  opacity: 1;
  filter: none;
}

.context-usage-ring--unavailable {
  background: #d1d5db;
  opacity: 1;
  filter: none;
}

.context-usage-ring--loading {
  animation: context-ring-spin 900ms linear infinite;
}

.context-usage-popover {
  position: absolute;
  bottom: 32px;
  left: -120px;
  z-index: 20;
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 280px;
  max-height: calc(100dvh - 64px);
  padding: 16px;
  overflow-y: auto;
  overscroll-behavior: contain;
  color: #191c1f;
  font-size: 14px;
  line-height: 20px;
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
}

.context-usage-popover__header,
.context-usage-popover__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.context-usage-popover__title {
  color: #424654;
  font-size: 12px;
  font-weight: 500;
  line-height: 16px;
}

.context-usage-popover__total,
.context-usage-popover__row-value,
.context-usage-popover__percent {
  color: #424654;
  font-variant-numeric: tabular-nums;
}

.context-usage-popover__total {
  font-size: 12px;
  line-height: 16px;
}

.context-usage-popover__total--high {
  color: #ba1a1a;
}

.context-usage-popover__note {
  margin: 0;
  color: #737786;
  font-size: 13px;
  line-height: 18px;
}

.context-usage-popover__meter {
  position: relative;
  width: 100%;
  height: 4px;
  overflow: hidden;
  background: #e0e2e6;
  border-radius: 999px;
}

.context-usage-popover__meter-fill {
  display: block;
  height: 100%;
  background: #3d78f6;
  border-radius: inherit;
}

.context-usage-popover__percent {
  align-self: flex-end;
  margin-top: -2px;
  font-size: 12px;
  line-height: 16px;
}

.context-usage-popover__rows {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.context-usage-popover__row-label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  color: #191c1f;
}

.context-usage-popover__row-value {
  display: grid;
  grid-template-columns: 72px 52px;
  gap: 8px;
  justify-content: end;
  text-align: right;
  white-space: nowrap;
}

.context-usage-popover__row-percent {
  color: #191c1f;
}

.context-usage-popover__row-icon {
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
}

.context-usage-popover__overhead {
  padding-top: 8px;
  border-top: 1px solid #eef0f2;
}

.context-usage-popover__receipt {
  position: relative;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding-top: 8px;
  border-top: 1px solid #eef0f2;
}

.context-usage-popover__receipt-title {
  padding-right: 28px;
  color: #424654;
  font-size: 12px;
  font-weight: 600;
}

.context-usage-popover__receipt-copy {
  position: absolute;
  top: 5px;
  right: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  color: #5a6070;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 5px;
}

.context-usage-popover__receipt-copy:hover,
.context-usage-popover__receipt-copy:focus-visible {
  color: #191c1f;
  background: #f1f3f5;
  outline: none;
}

.context-usage-popover__receipt-copy svg {
  width: 16px;
  height: 16px;
  fill: none;
  stroke: currentColor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.8;
}

.context-usage-popover__receipt-copy-status {
  min-height: 0;
  color: #5a6070;
  font-size: 11px;
  line-height: 0;
  text-align: right;
}

.context-usage-popover__receipt-copy-status:not(:empty) {
  min-height: 16px;
  line-height: 16px;
}

.context-usage-popover__receipt-content {
  max-height: min(240px, 32dvh);
  padding-right: 4px;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-gutter: stable;
}

.context-usage-popover__receipt-note {
  margin: 4px 0 0;
  color: #737786;
  font-size: 12px;
  line-height: 18px;
}

.context-usage-popover__receipt-call + .context-usage-popover__receipt-call {
  padding-top: 8px;
  margin-top: 8px;
  border-top: 1px solid #f1f2f4;
}

.context-usage-popover__receipt-list {
  display: grid;
  gap: 4px;
  padding: 0;
  margin: 6px 0 0;
  list-style: none;
  color: #424654;
  font-size: 12px;
  line-height: 17px;
}

.context-usage-popover__receipt-list li {
  display: grid;
  gap: 2px;
}

.context-usage-popover__receipt code,
.context-usage-popover__receipt-list code {
  color: #5a6070;
  font-size: 11px;
  overflow-wrap: anywhere;
  word-break: break-word;
}

.context-usage-popover__compact {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  min-height: 36px;
  padding: 8px 12px;
  color: #ffffff;
  font-size: 14px;
  font-weight: 500;
  background: #191c1f;
  border: 0;
  border-radius: 8px;
}

.context-usage-popover__compact:disabled {
  cursor: not-allowed;
  color: #424654;
  background: #e0e2e6;
}

.context-usage-popover__spinner {
  width: 16px;
  height: 16px;
  border: 2px solid #737786;
  border-top-color: transparent;
  border-radius: 999px;
  animation: context-ring-spin 900ms linear infinite;
}

@keyframes context-ring-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (max-width: 760px) {
  .input-dock {
    padding: 32px 12px 12px;
  }

  .input-panel__actions {
    gap: 8px;
  }

  .input-panel__right {
    gap: 8px;
  }

  .input-panel__model-name {
    max-width: 112px;
  }

  .input-panel__files {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    padding-bottom: 12px;
  }
}

</style>
