<template>
  <article ref="root" class="help-markdown" @click="handleClick" v-html="html"></article>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { renderHelpMarkdown } from '@/utils/helpMarkdown'

const props = defineProps<{ source: string }>()
const router = useRouter()
const root = ref<HTMLElement | null>(null)
const html = computed(() => renderHelpMarkdown(props.source))
let renderSequence = 0

async function renderMermaidBlocks() {
  if (!root.value) return
  const blocks = Array.from(root.value.querySelectorAll<HTMLElement>('[data-help-mermaid]:not([data-rendered])'))
  if (!blocks.length) return
  const { default: mermaid } = await import('mermaid')
  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'strict',
    theme: 'neutral',
    fontFamily: 'Inter, PingFang SC, Microsoft YaHei, sans-serif'
  })

  await Promise.all(blocks.map(async (block) => {
    const source = block.querySelector('pre')?.textContent?.trim() ?? ''
    if (!source) return
    try {
      const id = `help-mermaid-${Date.now()}-${renderSequence++}`
      const { svg } = await mermaid.render(id, source)
      block.innerHTML = svg
      block.dataset.rendered = 'true'
    } catch {
      block.dataset.rendered = 'error'
      block.innerHTML = '<p class="help-mermaid__error">流程图暂时无法渲染，请继续阅读下方正文。</p>'
    }
  }))
}

function handleClick(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  const link = target?.closest<HTMLAnchorElement>('a[data-help-link]')
  if (!link) return
  event.preventDefault()
  void router.push(link.getAttribute('href') ?? '/help')
}

onMounted(() => { void renderMermaidBlocks() })
watch(html, async () => {
  await nextTick()
  await renderMermaidBlocks()
})
</script>

<style scoped>
.help-markdown { color: #32363c; font-size: 15px; line-height: 1.85; }
.help-markdown :deep(h2), .help-markdown :deep(h3) { scroll-margin-top: 28px; color: #16191d; letter-spacing: -.012em; }
.help-markdown :deep(h2) { margin: 46px 0 15px; padding-top: 2px; font-size: 22px; line-height: 31px; }
.help-markdown :deep(h3) { margin: 30px 0 10px; font-size: 17px; line-height: 26px; }
.help-markdown :deep(p) { margin: 0 0 17px; }
.help-markdown :deep(ul), .help-markdown :deep(ol) { margin: 0 0 19px; padding-left: 24px; }
.help-markdown :deep(li) { margin: 5px 0; padding-left: 3px; }
.help-markdown :deep(a) { color: #1b57a6; text-decoration: underline; text-decoration-color: #b8cbe4; text-underline-offset: 3px; }
.help-markdown :deep(a:hover) { color: #0f3e7a; text-decoration-color: currentColor; }
.help-markdown :deep(strong) { color: #20242a; font-weight: 650; }
.help-markdown :deep(code) { padding: 2px 5px; color: #30343a; font-family: var(--font-mono); font-size: .88em; background: #f1f2f4; border: 1px solid #e3e5e8; border-radius: 5px; }
.help-markdown :deep(pre) { margin: 20px 0; padding: 17px 19px; overflow-x: auto; color: #30343a; background: #f5f6f7; border: 1px solid #e1e3e6; border-radius: 10px; }
.help-markdown :deep(pre code) { padding: 0; color: inherit; background: none; border: 0; }
.help-markdown :deep(blockquote) { margin: 20px 0; padding: 4px 0 4px 18px; color: #5f6670; border-left: 3px solid #b8bdc5; }
.help-markdown :deep(table) { width: 100%; margin: 22px 0; overflow: hidden; border-spacing: 0; border-collapse: separate; border: 1px solid #dfe2e6; border-radius: 9px; }
.help-markdown :deep(th), .help-markdown :deep(td) { padding: 10px 12px; text-align: left; border-right: 1px solid #e5e7eb; border-bottom: 1px solid #e5e7eb; }
.help-markdown :deep(th) { color: #31363c; font-size: 13px; background: #f5f6f7; }
.help-markdown :deep(tr:last-child td) { border-bottom: 0; }
.help-markdown :deep(th:last-child), .help-markdown :deep(td:last-child) { border-right: 0; }
.help-markdown :deep(img) { max-width: 100%; height: auto; margin: 18px auto; border: 1px solid #e5e7eb; border-radius: 10px; }
.help-markdown :deep(.help-callout) { margin: 22px 0; padding: 16px 18px; border: 1px solid; border-radius: 10px; }
.help-markdown :deep(.help-callout--tip) { color: #335e4a; background: #f1f8f4; border-color: #cce4d6; }
.help-markdown :deep(.help-callout--warning) { color: #72521f; background: #fff9ed; border-color: #ead7ad; }
.help-markdown :deep(.help-callout__title) { display: block; margin-bottom: 5px; color: inherit; font-size: 13px; }
.help-markdown :deep(.help-callout__body > :last-child) { margin-bottom: 0; }
.help-markdown :deep(.help-mermaid) { display: grid; min-height: 140px; margin: 24px 0; padding: 22px; overflow-x: auto; background: #fafafa; border: 1px solid #e1e3e6; border-radius: 12px; place-items: center; }
.help-markdown :deep(.help-mermaid pre) { margin: 0; }
.help-markdown :deep(.help-mermaid svg) { max-width: 100%; height: auto; }
.help-markdown :deep(.help-mermaid__error) { margin: 0; color: #8a5f20; font-size: 13px; }
.help-markdown :deep(hr) { margin: 36px 0; border: 0; border-top: 1px solid #e5e7eb; }
</style>
