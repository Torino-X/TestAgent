<template>
  <div
    v-if="update && update.headline"
    class="public-update"
    :class="{ 'public-update--streaming': isStreaming }"
    role="status"
    aria-live="polite"
    :aria-label="update.headline"
  >
    <template v-if="update.narrativeText">
      <div
        class="public-update__narrative markdown-body"
        v-html="renderedNarrative"
      ></div>
      <span v-if="isStreaming" class="public-update__cursor" aria-hidden="true"></span>
    </template>
    <template v-else>
      <p v-if="update.summary" class="public-update__summary">{{ update.summary }}</p>
      <p v-if="update.impact" class="public-update__impact">影响：{{ update.impact }}</p>
      <p v-if="update.nextAction" class="public-update__next">下一步：{{ update.nextAction }}</p>
    </template>
    <ul v-if="!update.narrativeText && update.details.length" class="public-update__details">
      <li v-for="(detail, index) in update.details" :key="`${update.dedupeKey}-${index}`">
        <span class="public-update__bullet">·</span> {{ detail }}
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { PublicExecutionUpdate } from '@/types'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps<{
  update: PublicExecutionUpdate | undefined
  startTime?: string
}>()

const update = computed(() => props.update)
const renderedNarrative = computed(() => renderMarkdown(update.value?.narrativeText ?? ''))
const isStreaming = computed(() => !!update.value?.narrativeText && update.value.chunkFinal === false)
</script>

<style scoped>
.public-update {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 16px;
  color: #333333;
  font-size: var(--text-md);
  font-weight: var(--font-regular);
  line-height: var(--leading-md);
}

.public-update__narrative,
.public-update__summary,
.public-update__impact,
.public-update__next {
  margin: 0;
}

.public-update__narrative {
  white-space: normal;
}

.public-update__narrative :deep(*) {
  margin-top: 0;
}

.public-update__narrative :deep(*:last-child) {
  margin-bottom: 0;
}

.public-update__narrative :deep(p) {
  margin: 0 0 12px;
}

.public-update__narrative :deep(h3) {
  margin: 12px 0 6px;
  color: var(--ta-text-strong, #1a1c1c);
  font-size: 15px;
  font-weight: var(--font-semibold);
  line-height: 22px;
}

.public-update__narrative :deep(h3:first-child) {
  margin-top: 0;
}

.public-update__narrative :deep(ul),
.public-update__narrative :deep(ol) {
  margin: 8px 0 10px;
  padding-left: 22px;
}

.public-update__narrative :deep(li) {
  margin: 4px 0;
}

.public-update__narrative :deep(code) {
  padding: 1px 4px;
  font-family: var(--font-mono);
  font-size: 0.92em;
  background: #f0f2f5;
  border-radius: 4px;
}

.public-update__cursor {
  display: inline-block;
  width: 7px;
  height: 16px;
  margin-left: 1px;
  vertical-align: -2px;
  background: var(--ta-text, #1a1c1c);
  animation: public-update-caret 1s steps(1, end) infinite;
}

.public-update__impact,
.public-update__next {
  color: var(--text-secondary);
}

.public-update__details {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 8px 0 0 0;
  padding: 0;
  list-style: none;
  color: #333333;
}

.public-update__details li {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}

.public-update__bullet {
  color: var(--ta-text-muted, #7e7576);
  font-size: 18px;
  line-height: 1;
  flex-shrink: 0;
}

@keyframes public-update-caret {
  0%,
  45% {
    opacity: 1;
  }
  46%,
  100% {
    opacity: 0;
  }
}
</style>
