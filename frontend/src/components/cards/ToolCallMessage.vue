<template>
  <section class="tool-call" :class="{ 'tool-call--expanded': expanded }">
    <div class="tool-call__header" @click="expanded = !expanded">
      <div class="tool-call__header-left">
        <span class="material-symbols-outlined tool-call__icon" style="font-variation-settings: 'FILL' 0">build</span>
        <span class="tool-call__name">{{ toolCall.name }}</span>
      </div>
      <div class="tool-call__header-right">
        <span class="tool-call__status">
          <span class="material-symbols-outlined tool-call__status-icon">{{ statusIcon }}</span>
          {{ statusText }}
        </span>
        <span class="material-symbols-outlined tool-call__chevron">chevron_right</span>
      </div>
    </div>
    <div class="tool-call__content">
      <div class="tool-call__content-inner">
        <div v-if="showStatusBadge" class="tool-call__badges">
          <span class="tool-call__success-badge">[{{ toolCall.status === 'failed' ? 'FAILED' : 'SUCCESS' }}]</span>
          <span v-if="retryAttempt" class="tool-call__retry-badge">第{{ retryAttempt }}次重试</span>
          <span class="tool-call__execution-text">Tool execution completed</span>
        </div>
        <div class="tool-call__grid">
          <div class="tool-call__field">
            <div class="tool-call__label">Input:</div>
            <div class="tool-call__value">{{ toolCall.input }}</div>
          </div>
          <div class="tool-call__field">
            <div class="tool-call__label">Output:</div>
            <div class="tool-call__value">{{ toolCall.output }}</div>
          </div>
        </div>
      </div>
    </div>
    <PublicExecutionUpdate
      v-if="showPublicUpdate && toolCall.publicUpdate"
      :update="toolCall.publicUpdate"
      :start-time="toolCall.startedAt"
    />
    <div v-if="toolCall.status === 'running'" class="tool-call__thinking">
      <span class="tool-call__thinking-dot"></span>
      正在思考...
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import PublicExecutionUpdate from './PublicExecutionUpdate.vue'
import type { ToolCall } from '@/types'
import { resolveToolDisplayStatus, toolDisplayStatusLabel } from '@/utils/toolStatus'

const props = defineProps<{
  toolCall: ToolCall
  retryAttempt?: number
  showPublicUpdate?: boolean
}>()

const expanded = ref(false)

const statusText = computed(() => toolDisplayStatusLabel(
  resolveToolDisplayStatus(props.toolCall.status, props.toolCall.publicUpdate),
))

const statusIcon = computed(() => {
  const status = resolveToolDisplayStatus(props.toolCall.status, props.toolCall.publicUpdate)
  if (status === 'success') return 'check'
  if (status === 'failed') return 'error'
  if (status === 'warning') return 'warning'
  return 'info'
})

const showStatusBadge = computed(() => {
  return props.toolCall.status === 'success' || props.toolCall.status === 'failed'
})

const showPublicUpdate = computed(() => props.showPublicUpdate !== false)
</script>

<style scoped>
.tool-call {
  border: 1px solid var(--ta-border);
  background: var(--ta-surface-low, #f3f3f3);
  border-radius: 2px;
  padding: 16px;
  margin-bottom: 16px;
  cursor: pointer;
  transition: border-color 0.2s;
}

.tool-call:hover {
  border-color: var(--ta-primary);
}

.tool-call__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.tool-call__header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.tool-call__icon {
  color: var(--ta-text-secondary, #5e5e5e);
  font-size: 18px;
}

.tool-call__name {
  color: #202020;
  font-size: var(--text-ui);
  font-weight: var(--font-semibold);
  line-height: var(--leading-ui);
}

.tool-call__header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

.tool-call__status {
  display: flex;
  align-items: center;
  gap: 4px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  line-height: var(--leading-xs);
}

.tool-call__status-icon {
  font-size: 14px;
}

.tool-call__chevron {
  color: var(--ta-text-secondary, #5e5e5e);
  font-size: 20px;
  transition: transform 0.3s ease;
}

.tool-call--expanded .tool-call__chevron {
  transform: rotate(90deg);
}

.tool-call__content {
  max-height: 0;
  overflow: hidden;
  opacity: 0;
  transition: max-height 0.3s ease-out, opacity 0.3s ease-out;
}

.tool-call--expanded .tool-call__content {
  max-height: 1000px;
  opacity: 1;
  margin-top: 12px;
  border-top: 1px solid var(--ta-border);
  padding-top: 16px;
}

.tool-call__content-inner {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.tool-call__badges {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 8px;
}

.tool-call__success-badge {
  color: #2e7d32;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  line-height: var(--leading-xs);
}

.tool-call__retry-badge {
  background: var(--ta-surface-highest, #e2e2e2);
  color: var(--ta-text-secondary, #5e5e5e);
  padding: 2px 12px;
  border-radius: 999px;
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  line-height: var(--leading-xs);
}

.tool-call__execution-text {
  color: #333333;
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
}

.tool-call__grid {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.tool-call__field {
  display: flex;
  gap: 16px;
}

.tool-call__label {
  width: 64px;
  flex-shrink: 0;
  color: #444444;
  font-size: var(--text-meta);
  font-weight: var(--font-medium);
  line-height: var(--leading-meta);
  padding-top: 8px;
}

.tool-call__value {
  flex: 1;
  background: var(--ta-surface-low, #f3f3f3);
  padding: 12px;
  border-radius: 2px;
  border: 1px solid var(--ta-border);
  color: #333333;
  font-family: var(--font-mono);
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
  white-space: pre-wrap;
  word-break: break-word;
}

@media (prefers-reduced-motion: reduce) {
  .tool-call__content,
  .tool-call__chevron {
    transition: none;
  }
  .tool-call__thinking-dot {
    animation: none;
  }
}

.tool-call__thinking {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  color: var(--text-tertiary);
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
  font-style: italic;
}

.tool-call__thinking-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ta-text-secondary, #5e5e5e);
  animation: thinking-pulse 1.5s ease-in-out infinite;
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
