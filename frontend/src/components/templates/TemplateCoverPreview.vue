<template>
  <div ref="root" class="template-cover" :class="[`template-cover--${state}`, `template-cover--${coverKind}`]">
    <span class="template-cover__format">{{ item.fileExt.toUpperCase() }}</span>

    <div v-if="state === 'idle' || state === 'loading'" class="template-cover__paper template-cover__paper--skeleton" role="status">
      <span class="template-cover__skeleton-kicker"></span>
      <span class="template-cover__skeleton-title"></span>
      <span v-for="line in 6" :key="line" class="template-cover__skeleton-line" :style="{ width: `${90 - line * 6}%` }"></span>
      <small>正在读取封面…</small>
    </div>

    <div v-if="state === 'preparing'" class="template-cover__fallback" role="status">
      <n-icon :component="DocumentTextOutline" :size="28" />
      <strong>封面准备中</strong>
      <span>历史模板正在完成一次性封面生成，请稍后刷新</span>
    </div>

    <canvas
      v-show="state === 'ready' && coverKind === 'document'"
      ref="canvas"
      class="template-cover__canvas"
      :aria-label="`${item.name} 首页封面`"
    />

    <div v-if="state === 'ready' && coverKind === 'spreadsheet'" class="template-cover__paper template-cover__sheet">
      <div class="template-cover__sheet-bar"><strong>{{ sheetName }}</strong><span>100%</span></div>
      <table aria-label="模板首页表格缩略图">
        <tbody>
          <tr v-for="(row, rowIndex) in sheetRows" :key="rowIndex">
            <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="state === 'fallback'" class="template-cover__fallback" role="status">
      <n-icon :component="DocumentTextOutline" :size="28" />
      <strong>封面解析暂不可用</strong>
      <span>文件完好，可直接在线预览</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { NIcon } from 'naive-ui'
import { DocumentTextOutline } from '@vicons/ionicons5'
import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy } from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.mjs?url'
import { getAuthToken, resolveApiUrl } from '@/api/request'
import type { TemplateMarketItem, UserTemplateItem } from '@/types/template'
import { withTemplateCoverSlot } from '@/utils/templateCoverQueue'

const props = defineProps<{ item: TemplateMarketItem | UserTemplateItem }>()

type CoverState = 'idle' | 'loading' | 'ready' | 'preparing' | 'fallback'
type CoverKind = 'document' | 'spreadsheet' | 'unknown'

const root = ref<HTMLElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const state = ref<CoverState>('idle')
const pdfDocument = shallowRef<PDFDocumentProxy | null>(null)
const coverKind = ref<CoverKind>('unknown')
let observer: IntersectionObserver | null = null
let cancelled = false

GlobalWorkerOptions.workerSrc = pdfWorkerUrl

const sheetName = computed(() => props.item.cover.spreadsheet?.sheetName || 'Sheet1')
const sheetRows = computed(() => {
  const rows = props.item.cover.spreadsheet?.rows ?? []
  return rows.slice(0, 9).map((row) => row.slice(0, 5))
})

async function loadCover() {
  if (state.value !== 'idle') return
  state.value = 'loading'
  try {
    await withTemplateCoverSlot(async () => {
      if (!props.item.cover.url) throw new Error('Persisted PDF cover is unavailable')
      await renderFirstPage(props.item.cover.url)
    })
  } catch {
    if (!cancelled) state.value = 'fallback'
  }
}

async function renderFirstPage(pdfUrl: string) {
  const token = getAuthToken()
  const loadingTask = getDocument({
    url: resolveApiUrl(pdfUrl),
    httpHeaders: token ? { Authorization: `Bearer ${token}` } : undefined,
    withCredentials: true,
    disableRange: true,
  })
  const document = await loadingTask.promise
  if (cancelled) {
    await document.destroy()
    return
  }
  pdfDocument.value = document
  const page = await document.getPage(1)
  await nextTick()
  if (cancelled || !canvas.value) return

  const stageWidth = root.value?.clientWidth || 320
  const baseViewport = page.getViewport({ scale: 1 })
  const cssWidth = Math.min(174, Math.max(138, stageWidth * 0.52))
  const scale = cssWidth / baseViewport.width
  const viewport = page.getViewport({ scale })
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2)
  const renderViewport = page.getViewport({ scale: scale * pixelRatio })
  const context = canvas.value.getContext('2d')
  if (!context) throw new Error('Canvas is unavailable')

  canvas.value.width = Math.floor(renderViewport.width)
  canvas.value.height = Math.floor(renderViewport.height)
  canvas.value.style.width = `${Math.ceil(viewport.width)}px`
  canvas.value.style.height = `${Math.ceil(viewport.height)}px`
  await page.render({ canvasContext: context, viewport: renderViewport }).promise
  if (!cancelled) state.value = 'ready'
}

onMounted(() => {
  if (props.item.cover.status !== 'ready') {
    state.value = props.item.cover.status === 'failed' ? 'fallback' : 'preparing'
    return
  }
  if (props.item.cover.kind === 'spreadsheet') {
    coverKind.value = 'spreadsheet'
    state.value = 'ready'
    return
  }
  if (props.item.cover.kind !== 'pdf' || !props.item.cover.url) {
    state.value = 'fallback'
    return
  }
  coverKind.value = 'document'
  if (!('IntersectionObserver' in window)) {
    void loadCover()
    return
  }
  observer = new IntersectionObserver((entries) => {
    if (!entries.some((entry) => entry.isIntersecting)) return
    observer?.disconnect()
    observer = null
    void loadCover()
  }, { rootMargin: '180px 0px' })
  if (root.value) observer.observe(root.value)
})

onBeforeUnmount(() => {
  cancelled = true
  observer?.disconnect()
  observer = null
  void pdfDocument.value?.destroy()
})
</script>

<style scoped>
.template-cover{position:relative;display:grid;aspect-ratio:1.45/1;min-height:238px;overflow:hidden;background:#f3f4f6;border-bottom:1px solid #e5e7eb;place-items:center}.template-cover__format{position:absolute;top:16px;right:16px;z-index:2;padding:5px 10px;color:#303030;font-size:11px;font-weight:650;letter-spacing:.04em;background:rgba(255,255,255,.92);border:1px solid #e2e4e7;border-radius:999px}.template-cover--spreadsheet .template-cover__format{color:#07855f;background:#f1fff9;border-color:#aee7d1}.template-cover__canvas,.template-cover__paper{background:#fff;box-shadow:0 4px 12px rgba(18,24,32,.12)}.template-cover__canvas{display:block}.template-cover__paper{box-sizing:border-box;width:min(52%,174px);aspect-ratio:210/297;padding:15px 12px;overflow:hidden}.template-cover__paper--skeleton{display:flex;flex-direction:column;gap:8px}.template-cover__paper--skeleton span{display:block;height:5px;background:linear-gradient(90deg,#eceef0 20%,#f8f8f8 45%,#eceef0 70%);background-size:220% 100%;border-radius:999px;animation:template-cover-shimmer 1.35s ease-in-out infinite}.template-cover__paper--skeleton .template-cover__skeleton-kicker{width:38%;height:4px}.template-cover__paper--skeleton .template-cover__skeleton-title{width:72%;height:11px;margin:8px 0 12px}.template-cover__paper--skeleton small{margin-top:auto;color:#a1a1a1;font-size:9px;text-align:center}.template-cover__sheet{padding:9px 8px}.template-cover__sheet-bar{display:flex;align-items:center;justify-content:space-between;height:18px;padding:0 5px;color:#fff;font-size:6px;background:#0d8a64}.template-cover__sheet-bar span{font-size:5px}.template-cover__sheet table{width:100%;table-layout:fixed;border-collapse:collapse;color:#5f6469;font-size:4.5px}.template-cover__sheet td{height:13px;overflow:hidden;padding:1px 2px;text-overflow:ellipsis;white-space:nowrap;border:1px solid #e2e5e8}.template-cover__sheet tr:nth-child(even){background:#f7f8f8}.template-cover__fallback{display:grid;gap:7px;max-width:190px;color:#8f8f8f;text-align:center;place-items:center}.template-cover__fallback strong{color:#666;font-size:13px;font-weight:560}.template-cover__fallback span{font-size:11px;line-height:1.5}@keyframes template-cover-shimmer{to{background-position:-220% 0}}@media(max-width:720px){.template-cover{min-height:250px}.template-cover__paper{width:min(48%,180px)}}
</style>
