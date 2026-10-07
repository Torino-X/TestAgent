<template>
  <div
    class="file-card"
    :class="[
      `file-card--${file.status}`,
      extensionClass,
      {
        'file-card--compact': compact && !isFloating,
        'file-card--floating': isFloating,
        'file-card--previewable': canPreview
      }
    ]"
    :role="canPreview ? 'button' : undefined"
    :tabindex="canPreview ? 0 : undefined"
    :aria-label="canPreview ? `查看文件 ${file.name}` : undefined"
    @click="openPreview"
    @keydown.enter.prevent="openPreview"
    @keydown.space.prevent="openPreview"
  >
    <template v-if="isFloating">
      <button class="file-card__close" type="button" aria-label="移除文件" @click.stop="emit('remove', file.id)" @keydown.stop>
        <n-icon :component="CloseOutline" />
      </button>

      <div class="file-card__floating-head">
        <div class="file-card__floating-icon" :class="{ 'file-card__floating-icon--asset': hasAssetIcon }">
          <img
            v-if="hasAssetIcon"
            :src="assetIconSrc"
            :alt="`${file.name} 文件类型图标`"
            class="file-card__floating-icon-image"
          />
          <n-icon v-else :component="fallbackIcon" />
        </div>
        <div class="file-card__body">
          <div class="file-card__name">{{ file.name }}</div>
          <div class="file-card__meta">{{ file.size }}</div>
        </div>
      </div>

      <div class="file-card__footer">
        <span
          v-if="showUploadProgress"
          class="file-card__upload-progress"
          :style="uploadProgressStyle"
          :aria-label="`上传进度 ${uploadProgress}%`"
        />
        <span class="file-card__status">
          <span v-if="showStatusDot" class="file-card__status-dot" />
          {{ statusLabel }}
        </span>
      </div>
    </template>

    <template v-else>
      <div class="file-card__icon" :class="{ 'file-card__icon--asset': hasAssetIcon }">
        <img
          v-if="hasAssetIcon"
          :src="assetIconSrc"
          :alt="`${file.name} 文件类型图标`"
          class="file-card__icon-image"
        />
        <n-icon v-else :component="fallbackIcon" />
      </div>
      <div class="file-card__body">
        <div class="file-card__name">{{ file.name }}</div>
        <div class="file-card__meta">{{ file.size }}</div>
        <div v-if="!compact || file.status !== 'confirmed'" class="file-card__status">{{ statusLabel }}</div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { NIcon } from 'naive-ui'
import {
  CloseOutline,
  DocumentOutline,
  DocumentTextOutline,
  ImageOutline,
  ReaderOutline
} from '@vicons/ionicons5'
import type { FileAttachment, FileUploadStatus } from '@/types'
import { resolveFileIcon, isAssetIcon } from '@/utils/fileIcon'
import { canPreviewFileAttachment } from '@/utils/filePreview'

const props = withDefaults(
  defineProps<{
    file: FileAttachment
    compact?: boolean
    variant?: 'default' | 'floating'
  }>(),
  {
    compact: false,
    variant: 'default'
  }
)

const emit = defineEmits<{
  remove: [fileId: string]
  open: [file: FileAttachment]
}>()

const isFloating = computed(() => props.variant === 'floating')
const canPreview = computed(() => canPreviewFileAttachment(props.file))

function openPreview() {
  if (canPreview.value) emit('open', props.file)
}

const statusLabel = computed(() => {
  const labels: Record<FileUploadStatus, string> = {
    uploading: '上传中',
    uploaded: '已上传',
    failed: '上传失败',
    identifying: '已上传',
    need_confirm: '已上传',
    confirmed: '已上传'
  }
  return labels[props.file.status]
})

const extensionClass = computed(() => {
  const extension = props.file.extension.replace('.', '').toLowerCase() || 'unknown'
  return `file-card--ext-${extension}`
})

const fileIcon = resolveFileIcon(props.file.name)
const hasAssetIcon = computed(() => isAssetIcon(fileIcon))
const assetIconSrc = computed(() => fileIcon.src ?? '')
const fallbackIcon = computed(() => {
  const extension = props.file.extension.toLowerCase()
  if (['.png', '.jpg', '.jpeg', '.webp', '.gif'].includes(extension)) return ImageOutline
  if (extension === '.md') return ReaderOutline
  if (extension === '.pdf') return DocumentOutline
  return DocumentTextOutline
})

const showStatusDot = computed(() => props.file.status === 'uploading')
const showUploadProgress = computed(() => isFloating.value && props.file.status === 'uploading')
const uploadProgress = computed(() => {
  const value = props.file.uploadProgress ?? 0
  return Math.max(0, Math.min(100, Math.round(value)))
})
const uploadProgressStyle = computed(() => ({
  '--file-upload-progress': `${uploadProgress.value * 3.6}deg`
}))
</script>

<style scoped>
.file-card {
  display: flex;
  gap: 12px;
  align-items: center;
  box-sizing: border-box;
  width: 260px;
  min-height: 72px;
  padding: 12px;
  background: var(--ta-surface);
  border: 1px solid var(--ta-border);
  border-radius: var(--ta-radius-md);
  box-shadow: var(--ta-shadow-soft);
}

.file-card--previewable {
  cursor: pointer;
  transition:
    border-color 160ms ease,
    box-shadow 160ms ease,
    transform 160ms ease;
}

.file-card--previewable:hover {
  border-color: #c8cdd4;
  box-shadow: 0 3px 12px rgba(17, 24, 39, 0.1);
}

.file-card--previewable:active {
  transform: translateY(1px);
}

.file-card--previewable:focus-visible {
  outline: 2px solid #3d78f6;
  outline-offset: 2px;
}

.file-card--compact {
  width: 224px;
  min-height: 56px;
  padding: 10px;
}

.file-card__icon {
  display: grid;
  width: 40px;
  height: 40px;
  color: var(--ta-primary-container);
  place-items: center;
  background: var(--ta-surface-low);
  border-radius: var(--ta-radius-sm);
}

.file-card__icon-image {
  width: 24px;
  height: 24px;
  object-fit: contain;
}

.file-card__body {
  min-width: 0;
  flex: 1;
}

.file-card__name {
  overflow: hidden;
  color: var(--ta-text-strong);
  font-size: var(--text-ui);
  font-weight: var(--font-medium);
  line-height: var(--leading-ui);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-card__meta,
.file-card__status {
  color: var(--ta-text-muted);
  font-size: var(--text-meta);
  line-height: var(--leading-meta);
}

.file-card__status {
  margin-top: 2px;
}

.file-card--failed {
  border-color: rgba(186, 26, 26, 0.28);
}

.file-card--failed .file-card__status {
  color: var(--ta-error);
}

.file-card--floating {
  position: relative;
  flex-direction: column;
  align-items: stretch;
  flex: 1 1 auto;
  width: 100%;
  min-width: 0;
  min-height: 110px;
  padding: 12px;
  background: var(--ta-surface);
  border-color: var(--ta-border);
  border-radius: var(--ta-radius-md);
  box-shadow: 0 1px 5px rgba(17, 24, 39, 0.12);
}

.file-card--floating.file-card--confirmed {
  height: 110px;
  min-height: 110px;
}

.file-card--floating .file-card__close {
  position: absolute;
  top: -6px;
  right: -6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  color: var(--ta-text-muted);
  cursor: pointer;
  background: var(--ta-surface-high);
  border: 0;
  border-radius: 999px;
  box-shadow: 0 1px 5px rgba(17, 24, 39, 0.12);
  opacity: 0;
  transition:
    color 0.16s ease,
    opacity 0.16s ease;
}

.file-card--floating:hover .file-card__close,
.file-card--floating:focus-within .file-card__close {
  opacity: 1;
}

.file-card--floating .file-card__close:hover {
  color: var(--ta-error);
}

.file-card__floating-head {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  min-width: 0;
}

.file-card__floating-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 24px;
  height: 24px;
  color: var(--ta-text-muted);
  font-size: 22px;
}

.file-card__floating-icon-image {
  width: 24px;
  height: 24px;
  object-fit: contain;
}

.file-card--ext-pdf .file-card__floating-icon {
  color: var(--ta-error);
}

.file-card--ext-png .file-card__floating-icon,
.file-card--ext-jpg .file-card__floating-icon,
.file-card--ext-jpeg .file-card__floating-icon,
.file-card--ext-webp .file-card__floating-icon {
  color: var(--ta-text-strong);
}

.file-card--floating .file-card__name {
  font-size: var(--text-ui);
  line-height: var(--leading-ui);
}

.file-card--floating .file-card__meta {
  margin-top: 1px;
  font-size: var(--text-meta);
  font-weight: var(--font-medium);
  line-height: var(--leading-meta);
}

.file-card__footer {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}

.file-card__upload-progress {
  position: relative;
  display: inline-flex;
  flex: 0 0 auto;
  width: 18px;
  height: 18px;
  background:
    conic-gradient(var(--ta-running) var(--file-upload-progress), #d1d5db 0deg);
  border-radius: 999px;
}

.file-card__upload-progress::after {
  position: absolute;
  inset: 4px;
  content: '';
  background: var(--ta-surface);
  border-radius: inherit;
}

.file-card--floating .file-card__status {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
  margin: 0 0 0 auto;
  color: var(--ta-text-muted);
  font-size: var(--text-xs);
  line-height: var(--leading-xs);
  white-space: nowrap;
}

.file-card--floating.file-card--uploaded .file-card__status,
.file-card--floating.file-card--confirmed .file-card__status {
  color: var(--ta-success);
}

.file-card--floating.file-card--identifying .file-card__status,
.file-card--floating.file-card--need_confirm .file-card__status,
.file-card--floating.file-card--uploading .file-card__status {
  color: var(--ta-running);
}

.file-card--floating.file-card--failed .file-card__status {
  color: var(--ta-error);
}

.file-card__status-dot {
  width: 6px;
  height: 6px;
  background: currentColor;
  border-radius: 999px;
  animation: ta-file-card-pulse 1.4s infinite;
}

@keyframes ta-file-card-pulse {
  0%,
  100% {
    opacity: 0.35;
  }

  50% {
    opacity: 1;
  }
}

@media (max-width: 600px) {
  .file-card,
  .file-card--compact {
    width: 100%;
  }

  .file-card--floating {
    width: 100%;
  }
}
</style>
