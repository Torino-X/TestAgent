<template>
  <article
    class="assistant-content markdown-body"
    aria-label="AI 回复"
    v-html="html"
    @click="handleContentClick"
  ></article>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount } from 'vue'
import { copyMarkdownText } from '@/utils/clipboard'
import { renderMarkdown } from '@/utils/markdown'

const props = defineProps<{
  content: string
}>()

const emit = defineEmits<{
  toast: [payload: { kind: 'error'; text: string }]
}>()

const html = computed(() => renderMarkdown(props.content))

const copyResetTimers = new Map<HTMLButtonElement, ReturnType<typeof setTimeout>>()

function resetCopyButton(button: HTMLButtonElement) {
  button.classList.remove('markdown-code-block__copy--copied')
  button.setAttribute('aria-label', '复制代码')
  button.setAttribute('title', '复制代码')
  copyResetTimers.delete(button)
}

async function handleContentClick(event: MouseEvent) {
  const target = event.target
  if (!(target instanceof Element)) return

  const button = target.closest<HTMLButtonElement>('[data-code-copy]')
  const currentTarget = event.currentTarget
  if (!button || !(currentTarget instanceof HTMLElement) || !currentTarget.contains(button)) return

  const codeBlock = button.closest<HTMLElement>('.markdown-code-block')
  const codeElement = codeBlock?.querySelector<HTMLElement>('code')
  if (!codeElement) return

  const codeText = codeElement.textContent ?? ''
  const result = await copyMarkdownText(codeText)
  if (result === 'success' || result === 'fallback_success') {
    const activeTimer = copyResetTimers.get(button)
    if (activeTimer) clearTimeout(activeTimer)

    button.classList.add('markdown-code-block__copy--copied')
    button.setAttribute('aria-label', '代码已复制')
    button.setAttribute('title', '已复制')
    copyResetTimers.set(button, setTimeout(() => resetCopyButton(button), 1600))
    return
  }

  if (result === 'failed') {
    emit('toast', { kind: 'error', text: '复制失败，请手动复制' })
  }
}

onBeforeUnmount(() => {
  copyResetTimers.forEach((timer) => clearTimeout(timer))
  copyResetTimers.clear()
})
</script>

<style scoped>
.assistant-content {
  width: 100%;
  min-width: 0;
  padding: 0;
  margin: 0;
  color: var(--text-body);
  font-size: var(--text-body-size);
  font-weight: var(--font-regular);
  line-height: var(--leading-body);
  overflow-wrap: anywhere;
}

.assistant-content :deep(*) {
  margin-top: 0;
}

.assistant-content :deep(*:last-child) {
  margin-bottom: 0;
}

.assistant-content :deep(p) {
  margin: 0 0 12px;
}

.assistant-content :deep(h1),
.assistant-content :deep(h2),
.assistant-content :deep(h3),
.assistant-content :deep(h4),
.assistant-content :deep(h5),
.assistant-content :deep(h6) {
  color: var(--text-primary);
  font-weight: var(--font-semibold);
}

.assistant-content :deep(h1) {
  margin: 28px 0 10px;
  font-size: 20px;
  line-height: 30px;
}

.assistant-content :deep(h2) {
  margin: 26px 0 8px;
  font-size: 18px;
  line-height: 28px;
}

.assistant-content :deep(h3) {
  margin: 24px 0 8px;
  font-size: var(--text-body-size);
  line-height: 26px;
}

.assistant-content :deep(h4),
.assistant-content :deep(h5),
.assistant-content :deep(h6) {
  margin: 24px 0 8px;
  font-size: var(--text-body-size);
  line-height: 26px;
}

.assistant-content :deep(h1:first-child),
.assistant-content :deep(h2:first-child),
.assistant-content :deep(h3:first-child),
.assistant-content :deep(h4:first-child),
.assistant-content :deep(h5:first-child),
.assistant-content :deep(h6:first-child) {
  margin-top: 0;
}

.assistant-content :deep(ul),
.assistant-content :deep(ol) {
  padding-left: 24px;
  margin: 8px 0 14px;
}

.assistant-content :deep(li) {
  padding-left: 2px;
  margin: 4px 0;
  line-height: var(--leading-body);
}

.assistant-content :deep(li > p) {
  margin-bottom: 8px;
}

.assistant-content :deep(blockquote) {
  padding: 8px 12px;
  margin: 0 0 10px;
  color: var(--ta-text-muted);
  background: #f8fafc;
  border-left: 3px solid #c6c6c6;
  border-radius: 6px;
}

.assistant-content :deep(:not(pre) > code) {
  padding: 2px 5px;
  font-family: var(--font-mono);
  font-size: 0.92em;
  color: var(--text-primary);
  background: #ececec;
  border-radius: 5px;
}

.assistant-content :deep(.markdown-code-block) {
  margin: 0 0 14px;
  overflow: hidden;
  background: #f4f4f4;
  border: 1px solid #e7e7e7;
  border-radius: 14px;
}

.assistant-content :deep(.markdown-code-block__header) {
  display: flex;
  min-height: 42px;
  padding: 0 10px 0 16px;
  align-items: center;
  justify-content: space-between;
  color: #555;
  background: #f4f4f4;
  border-bottom: 1px solid #e7e7e7;
}

.assistant-content :deep(.markdown-code-block__language) {
  overflow: hidden;
  font-family: var(--font-mono);
  font-size: 12px;
  font-weight: var(--font-medium);
  line-height: 18px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.assistant-content :deep(.markdown-code-block__copy) {
  position: relative;
  display: inline-flex;
  width: 32px;
  height: 32px;
  flex: 0 0 32px;
  padding: 0;
  align-items: center;
  justify-content: center;
  color: #4b4b4b;
  cursor: pointer;
  background: transparent;
  border: 0;
  border-radius: 8px;
  transition: color 140ms ease, background-color 140ms ease, transform 140ms ease;
}

.assistant-content :deep(.markdown-code-block__copy:hover) {
  color: #171717;
  background: #e5e5e5;
}

.assistant-content :deep(.markdown-code-block__copy:active) {
  transform: scale(0.94);
}

.assistant-content :deep(.markdown-code-block__copy:focus-visible) {
  outline: 2px solid #6b7280;
  outline-offset: 1px;
}

.assistant-content :deep(.markdown-code-block__copy-icon) {
  position: relative;
  display: block;
  width: 17px;
  height: 17px;
}

.assistant-content :deep(.markdown-code-block__copy-icon::before),
.assistant-content :deep(.markdown-code-block__copy-icon::after) {
  position: absolute;
  width: 10px;
  height: 11px;
  content: '';
  border: 1.5px solid currentColor;
  border-radius: 2px;
}

.assistant-content :deep(.markdown-code-block__copy-icon::before) {
  top: 1px;
  left: 2px;
}

.assistant-content :deep(.markdown-code-block__copy-icon::after) {
  top: 5px;
  left: 6px;
  background: #f4f4f4;
}

.assistant-content :deep(.markdown-code-block__copy:hover .markdown-code-block__copy-icon::after) {
  background: #e5e5e5;
}

.assistant-content :deep(.markdown-code-block__copied-icon) {
  display: none;
  color: #237a43;
  font-size: 17px;
  font-weight: var(--font-semibold);
  line-height: 1;
}

.assistant-content :deep(.markdown-code-block__copy--copied .markdown-code-block__copy-icon) {
  display: none;
}

.assistant-content :deep(.markdown-code-block__copy--copied .markdown-code-block__copied-icon) {
  display: block;
}

.assistant-content :deep(.markdown-code-block pre) {
  padding: 14px 18px 18px;
  margin: 0;
  overflow: auto;
  background: #f4f4f4;
  border-radius: 0;
}

.assistant-content :deep(.markdown-code-block pre code) {
  display: block;
  padding: 0;
  color: #242424;
  white-space: pre;
  background: transparent;
  border-radius: 0;
  font-family: var(--font-mono);
  font-size: var(--text-ui);
  font-weight: var(--font-regular);
  line-height: var(--leading-ui);
}

.assistant-content :deep(a) {
  color: var(--text-primary);
  text-decoration: none;
}

.assistant-content :deep(a:hover) {
  text-decoration: underline;
}

.assistant-content :deep(table) {
  display: block;
  width: 100%;
  margin-bottom: 10px;
  overflow-x: auto;
  border-collapse: collapse;
}

.assistant-content :deep(th),
.assistant-content :deep(td) {
  padding: 8px 10px;
  text-align: left;
  border: 1px solid var(--ta-border);
}

.assistant-content :deep(th) {
  font-weight: var(--font-semibold);
  background: #f6f8fc;
}

.assistant-content :deep(strong) {
  color: var(--text-primary);
  font-weight: var(--font-semibold);
}
</style>
