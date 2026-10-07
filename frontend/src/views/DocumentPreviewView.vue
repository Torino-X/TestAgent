<template>
  <AppLayout>
  <main class="document-preview">
    <header class="document-preview__header">
      <div class="document-preview__location">
        <button class="document-preview__close" type="button" aria-label="关闭预览" @click="closePreview">
          <n-icon :component="CloseOutline" :size="21" />
        </button>
        <n-tooltip trigger="hover" :show-arrow="false">
          <template #trigger>
            <button class="document-preview__source" type="button" @click="returnToSource">{{ sourceLabel }}</button>
          </template>
          {{ sourceTooltip }}
        </n-tooltip>
        <span class="document-preview__slash">/</span>
        <strong class="document-preview__name">{{ preview?.item.name || '正在打开文件' }}</strong>
      </div>
      <n-dropdown v-if="preview && !isTemplatePreview" :options="actionOptions" :menu-props="documentActionMenuProps" trigger="click" placement="bottom-end" :show-arrow="false" @select="handleAction">
        <button class="document-preview__more" type="button" aria-label="文件操作"><n-icon :component="EllipsisHorizontal" :size="22" /></button>
      </n-dropdown>
    </header>

    <section v-if="loading" class="document-preview__loading"><n-spin size="small" />正在准备预览…</section>
    <section v-else-if="loadError" class="document-preview__empty"><n-icon :component="AlertCircleOutline" :size="34" /><p>{{ loadError }}</p><n-button @click="returnToSource">返回</n-button></section>

    <section v-else-if="preview" class="document-preview__stage" :class="`document-preview__stage--${preview.viewer}`">
      <div v-if="isPdfViewer" class="document-preview__pdf-canvas-stage" @wheel="zoomPdf">
        <div class="document-preview__pdf-pages" :aria-label="`${preview.item.name} PDF 预览`">
          <figure v-for="page in pdfPages" :key="page.number" class="document-preview__pdf-page" :style="{ width: `${page.width}px`, minHeight: `${page.height}px` }">
            <canvas :ref="setPdfCanvas(page.number)" />
          </figure>
        </div>
        <div class="document-preview__zoom-hint">Ctrl + 滚轮缩放 · {{ Math.round(documentZoom * 100) }}%</div>
      </div>

      <div v-else-if="preview.viewer === 'spreadsheet'" class="document-preview__sheet-wrap">
        <div class="document-preview__sheet-scroll">
          <table class="document-preview__sheet">
            <thead><tr><th class="document-preview__sheet-index">#</th><th v-for="column in preview.spreadsheet?.columns" :key="column">{{ column }}</th></tr></thead>
            <tbody>
              <tr v-for="(row, index) in preview.spreadsheet?.rows" :key="index"><th>{{ index + 1 }}</th><td v-for="(cell, columnIndex) in row" :key="columnIndex">{{ cell }}</td></tr>
            </tbody>
          </table>
        </div>
        <p v-if="preview.spreadsheet?.truncated" class="document-preview__truncated">仅显示前 250 行和 40 列。</p>
      </div>

      <article v-else-if="preview.viewer === 'markdown'" class="document-preview__markdown markdown-content" v-html="markdownHtml" />
      <pre v-else-if="preview.viewer === 'text'" class="document-preview__text">{{ preview.textContent }}</pre>
      <div v-else class="document-preview__empty document-preview__unsupported"><n-icon :component="DocumentOutline" :size="35" /><p>此文件暂不支持在线预览。</p><n-button v-if="!isTemplatePreview" type="primary" @click="downloadCurrent">下载文件</n-button></div>
    </section>
  </main>
  </AppLayout>

  <n-modal v-model:show="renameVisible" preset="card" class="document-preview__rename-modal" title="重命名文件" :mask-closable="false">
    <n-input v-model:value="renameValue" autofocus @keyup.enter="renameCurrent" />
    <template #footer><div class="document-preview__rename-actions"><n-button quaternary @click="renameVisible = false">取消</n-button><n-button type="primary" :loading="renaming" @click="renameCurrent">保存</n-button></div></template>
  </n-modal>
</template>

<script setup lang="ts">
import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NDropdown, NIcon, NInput, NModal, NSpin, NTooltip, useMessage, type DropdownOption } from 'naive-ui'
import { AlertCircleOutline, CloseOutline, CreateOutline, DocumentOutline, DownloadOutline, EllipsisHorizontal, TrashOutline } from '@vicons/ionicons5'
import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy } from 'pdfjs-dist'
import pdfWorkerUrl from 'pdfjs-dist/build/pdf.worker.mjs?url'
import { fetchDocumentPreview } from '@/api/documentPreviewApi'
import { fetchMyTemplatePreview, fetchTemplatePreview } from '@/api/templateApi'
import { getAuthToken, resolveApiUrl } from '@/api/request'
import { deleteLibraryItem, downloadLibraryItem, renameLibraryItem } from '@/api/libraryApi'
import { renderMarkdown } from '@/utils/markdown'
import type { DocumentPreview } from '@/types/library'
import AppLayout from '@/components/layout/AppLayout.vue'

const route = useRoute()
const router = useRouter()
const message = useMessage()
const preview = ref<DocumentPreview | null>(null)
const loading = ref(true)
const loadError = ref('')
const pdfDocument = shallowRef<PDFDocumentProxy | null>(null)
const pdfPages = ref<Array<{ number: number; width: number; height: number }>>([])
const documentZoom = ref(1.35)
const pdfCanvases = new Map<number, HTMLCanvasElement>()
let pdfRenderVersion = 0
let pdfRenderQueue = Promise.resolve()
let previewPollingCancelled = false
const renameVisible = ref(false)
const renameValue = ref('')
const renaming = ref(false)
const isMyTemplatePreview = computed(() => route.name === 'my-template-preview')
const isTemplatePreview = computed(() => route.name === 'template-preview' || isMyTemplatePreview.value)
const itemId = computed(() => String(isMyTemplatePreview.value ? route.params.userTemplateId : isTemplatePreview.value ? route.params.templateId : route.params.itemId || ''))
const isChatOrigin = computed(() => route.query.from === 'chat' && typeof route.query.conversationId === 'string')
const isProjectOrigin = computed(() => route.query.from === 'project' && typeof route.query.projectId === 'string')
const sourceLabel = computed(() => {
  if (isMyTemplatePreview.value) return '我的模板'
  if (isTemplatePreview.value) return '模板市场'
  if (isChatOrigin.value) return String(route.query.conversationTitle || '对话')
  if (isProjectOrigin.value) return String(route.query.projectName || '项目')
  return '资料库'
})
const sourceTooltip = computed(() => isMyTemplatePreview.value ? '返回我的模板' : isTemplatePreview.value ? '返回模板市场' : isChatOrigin.value ? '在对话中查看' : isProjectOrigin.value ? '在项目中查看' : '在资料库中查看')
const markdownHtml = computed(() => renderMarkdown(preview.value?.textContent || ''))
const isPdfViewer = computed(() => preview.value?.viewer === 'word' || preview.value?.viewer === 'pdf')
const actionOptions = computed<DropdownOption[]>(() => [
  option('下载', 'download', DownloadOutline), option('重命名', 'rename', CreateOutline), option('删除', 'delete', TrashOutline, true)
])

function option(label: string, key: string, icon: typeof DownloadOutline, danger = false): DropdownOption {
  return {
    key,
    props: danger ? { class: 'document-preview-action-option--danger' } : undefined,
    label: () => h('span', { class: ['document-preview-action-option', danger ? 'document-preview-action-option--danger' : ''] }, [
      h('span', { class: 'document-preview-action-option__icon' }, [h(NIcon, { component: icon, size: 18 })]),
      h('span', { class: 'document-preview-action-option__label' }, label)
    ])
  }
}
const documentActionMenuProps = () => ({ class: 'document-preview-action-menu' })
GlobalWorkerOptions.workerSrc = pdfWorkerUrl

async function loadPreview() {
  loading.value = true
  loadError.value = ''
  previewPollingCancelled = false
  try {
    preview.value = await fetchActivePreview()
    if (preview.value.viewer === 'word') preview.value = await waitForPreviewReady(preview.value)
    if (preview.value.pdfUrl) await loadPdfDocument(preview.value.pdfUrl)
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '文件预览加载失败。'
  } finally {
    loading.value = false
  }
  await nextTick()
  if (pdfDocument.value) await updatePdfPageLayouts()
}
function fetchActivePreview() {
  if (isMyTemplatePreview.value) return fetchMyTemplatePreview(itemId.value)
  if (isTemplatePreview.value) return fetchTemplatePreview(itemId.value)
  return fetchDocumentPreview(itemId.value)
}
function delay(milliseconds: number) { return new Promise(resolve => window.setTimeout(resolve, milliseconds)) }
async function waitForPreviewReady(current: DocumentPreview): Promise<DocumentPreview> {
  let latest = current
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (latest.previewStatus === 'ready' && latest.pdfUrl) return latest
    if (latest.previewStatus === 'failed') throw new Error(latest.previewError || '文档预览准备失败，请稍后重试')
    await delay(1000)
    if (previewPollingCancelled) throw new DOMException('Preview polling cancelled', 'AbortError')
    latest = await fetchActivePreview()
    preview.value = latest
  }
  throw new Error('文档预览准备超时，请稍后重试')
}
function returnToSource() {
  if (isMyTemplatePreview.value) router.push({ name: 'templates', query: { tab: 'mine' } })
  else if (isTemplatePreview.value) router.push('/templates')
  else if (isChatOrigin.value) router.push(`/chat/${encodeURIComponent(String(route.query.conversationId))}`)
  else if (isProjectOrigin.value) router.push(`/projects/${encodeURIComponent(String(route.query.projectId))}`)
  else router.push('/library')
}
function closePreview() {
  if (window.history.length > 1) router.back()
  else returnToSource()
}
function zoomPdf(event: WheelEvent) {
  if (!event.ctrlKey || !isPdfViewer.value) return
  event.preventDefault()
  const change = event.deltaY < 0 ? 0.06 : -0.06
  documentZoom.value = Math.min(1.8, Math.max(0.75, Number((documentZoom.value + change).toFixed(2))))
  void updatePdfPageLayouts()
}
async function loadPdfDocument(url: string) {
  const token = getAuthToken()
  const task = getDocument({
    url: resolveApiUrl(url),
    httpHeaders: token ? { Authorization: `Bearer ${token}` } : undefined,
    withCredentials: true,
    disableRange: true,
  })
  pdfDocument.value = await task.promise
}
function updatePdfPageLayouts() {
  const renderVersion = ++pdfRenderVersion
  pdfRenderQueue = pdfRenderQueue.catch(() => undefined).then(() => renderPdfPageLayouts(renderVersion))
  return pdfRenderQueue
}
async function renderPdfPageLayouts(renderVersion: number) {
  const document = pdfDocument.value
  if (!document || renderVersion !== pdfRenderVersion) return
  const firstPage = await document.getPage(1)
  const firstViewport = firstPage.getViewport({ scale: documentZoom.value })
  const pages = Array.from({ length: document.numPages }, (_, index) => ({
    number: index + 1,
    width: Math.ceil(firstViewport.width),
    height: Math.ceil(firstViewport.height),
  }))
  if (renderVersion !== pdfRenderVersion) return
  pdfPages.value = pages
  await nextTick()
  if (renderVersion !== pdfRenderVersion) return
  if (!pages.length) return
  await renderPdfPage(pages[0].number, renderVersion)
  if (renderVersion !== pdfRenderVersion) return
  void renderRemainingPdfPages(pages.slice(1), renderVersion)
}
async function renderRemainingPdfPages(pages: Array<{ number: number }>, renderVersion: number) {
  for (const page of pages) {
    await renderPdfPage(page.number, renderVersion)
    if (renderVersion !== pdfRenderVersion) return
  }
}
function setPdfCanvas(pageNumber: number) {
  return (element: unknown) => {
    if (element instanceof HTMLCanvasElement) {
      pdfCanvases.set(pageNumber, element)
    } else pdfCanvases.delete(pageNumber)
  }
}
async function renderPdfPage(pageNumber: number, renderVersion: number) {
  const document = pdfDocument.value
  const canvas = pdfCanvases.get(pageNumber)
  if (!document || !canvas || renderVersion !== pdfRenderVersion) return
  const page = await document.getPage(pageNumber)
  if (renderVersion !== pdfRenderVersion) return
  const pixelRatio = Math.min(window.devicePixelRatio || 1, 2)
  const viewport = page.getViewport({ scale: documentZoom.value })
  const renderViewport = page.getViewport({ scale: documentZoom.value * pixelRatio })
  const layout = pdfPages.value.find(candidate => candidate.number === pageNumber)
  if (layout) {
    layout.width = Math.ceil(viewport.width)
    layout.height = Math.ceil(viewport.height)
  }
  const context = canvas.getContext('2d')
  if (!context) return
  canvas.width = Math.floor(renderViewport.width)
  canvas.height = Math.floor(renderViewport.height)
  canvas.style.width = `${Math.ceil(viewport.width)}px`
  canvas.style.height = `${Math.ceil(viewport.height)}px`
  await page.render({ canvasContext: context, viewport: renderViewport }).promise
}
async function downloadCurrent() { if (preview.value) await downloadLibraryItem(preview.value.item) }
async function handleAction(key: string | number) {
  if (!preview.value) return
  try {
    if (key === 'download') return await downloadCurrent()
    if (key === 'rename') { renameValue.value = preview.value.item.name; renameVisible.value = true; return }
    if (key === 'delete') { await deleteLibraryItem(preview.value.item.id); message.success('文件已移至最近删除'); returnToSource() }
  } catch (error) { message.error(error instanceof Error ? error.message : '操作失败，请稍后重试') }
}
async function renameCurrent() {
  if (!preview.value || !renameValue.value.trim()) return
  renaming.value = true
  try {
    const renamed = await renameLibraryItem(preview.value.item.id, renameValue.value.trim())
    preview.value.item = renamed
    renameVisible.value = false
    message.success('文件已重命名')
  } catch (error) { message.error(error instanceof Error ? error.message : '重命名失败') }
  finally { renaming.value = false }
}
onMounted(() => void loadPreview())
onBeforeUnmount(() => {
  previewPollingCancelled = true
  pdfRenderVersion += 1
  void pdfDocument.value?.destroy()
})
</script>

<style>
.document-preview-action-menu { min-width: 146px; padding: 8px !important; border-radius: 16px !important; box-shadow: 0 14px 38px rgba(20, 20, 20, .14) !important; }
.document-preview-action-menu .n-dropdown-option { min-height: 38px; border-radius: 9px; }
.document-preview-action-menu .n-dropdown-option-body__prefix { display: none; }
.document-preview-action-menu .n-dropdown-option-body__label { width: 100%; }
.document-preview-action-option { display: flex; width: 100%; gap: 10px; align-items: center; }
.document-preview-action-option__icon { display: grid; flex: 0 0 18px; width: 18px; height: 18px; place-items: center; }
.document-preview-action-option__label { min-width: 0; line-height: 20px; white-space: nowrap; }
.document-preview-action-menu .document-preview-action-option--danger,
.document-preview-action-menu .n-dropdown-option.document-preview-action-option--danger { color: #e4202b !important; }
</style>

<style scoped>
.document-preview { display: flex; flex: 1; flex-direction: column; min-width: 0; height: 100%; overflow: hidden; color: #111; background: #fff; }
.document-preview__header { display: flex; flex: 0 0 52px; align-items: center; justify-content: space-between; padding: 0 18px; background: #fff; border-bottom: 1px solid #f4f4f4; }
.document-preview__location { display: flex; min-width: 0; gap: 11px; align-items: center; font-size: 14px; }.document-preview__close, .document-preview__more { display: grid; width: 32px; height: 32px; padding: 0; color: #111; cursor: pointer; background: transparent; border: 0; border-radius: 8px; place-items: center; }.document-preview__close:hover, .document-preview__more:hover { background: #f5f5f5; }.document-preview__source { padding: 0; color: #6b7280; font: inherit; cursor: pointer; background: transparent; border: 0; }.document-preview__source:hover { color: #111; }.document-preview__slash { color: #a3a3a3; }.document-preview__name { min-width: 0; overflow: hidden; font-weight: 500; text-overflow: ellipsis; white-space: nowrap; }
.document-preview__stage { position: relative; flex: 1; min-height: 0; overflow: auto; background: #f1f3f4; }.document-preview__stage--spreadsheet { background: #fff; }.document-preview__stage--markdown, .document-preview__stage--text { background: #fff; }.document-preview__loading, .document-preview__empty { display: grid; flex: 1; gap: 14px; min-height: 0; color: #6b7280; place-content: center; place-items: center; }.document-preview__unsupported { position: absolute; inset: 0; }.document-preview__empty p { margin: 0; color: #4b5563; }
.document-preview__pdf-canvas-stage { min-height: 100%; padding: 20px 40px 76px; overflow: auto; }.document-preview__pdf-pages { display: grid; gap: 24px; justify-content: center; width: max-content; min-width: 100%; }.document-preview__pdf-page { box-sizing: border-box; margin: 0; overflow: hidden; line-height: 0; background: #fff; border: 1px solid #e5e7eb; box-shadow: 0 2px 10px rgba(15, 23, 42, .08); }.document-preview__pdf-page canvas { display: block; max-width: none; background: #fff; }.document-preview__zoom-hint { position: fixed; bottom: 22px; left: 50%; z-index: 2; padding: 7px 11px; color: #666; font-size: 12px; pointer-events: none; background: rgba(255,255,255,.92); border: 1px solid #e8e8e8; border-radius: 999px; transform: translateX(-50%); box-shadow: 0 2px 8px rgba(0,0,0,.06); }
.document-preview__sheet-wrap { min-width: max-content; min-height: 100%; padding: 24px 46px; }.document-preview__sheet-scroll { overflow: auto; max-width: calc(100vw - 92px); border: 1px solid #dfe3e6; border-radius: 15px; box-shadow: 0 2px 10px rgba(15,23,42,.05); }.document-preview__sheet { border-spacing: 0; border-collapse: separate; color: #0e2748; font-size: 14px; }.document-preview__sheet th, .document-preview__sheet td { box-sizing: border-box; min-width: 120px; height: 39px; padding: 0 12px; text-align: left; white-space: pre; background: #fff; border-right: 1px solid #e5e8eb; border-bottom: 1px solid #e5e8eb; }.document-preview__sheet thead th { position: sticky; top: 0; z-index: 2; font-weight: 600; background: #f6f8fa; }.document-preview__sheet th:first-child { position: sticky; left: 0; z-index: 1; min-width: 55px; width: 55px; color: #385779; text-align: right; background: #f8fafb; }.document-preview__sheet thead th:first-child { z-index: 3; text-align: center; }.document-preview__truncated { margin: 12px 0 0; color: #737373; font-size: 12px; }
.document-preview__markdown { width: min(688px, calc(100% - 48px)); margin: 0 auto; padding: 48px 0 80px; color: #18181b; font-size: 15px; line-height: 1.75; }.document-preview__markdown :deep(h1), .document-preview__markdown :deep(h2), .document-preview__markdown :deep(h3) { margin: 28px 0 12px; color: #09090b; line-height: 1.3; }.document-preview__markdown :deep(p) { margin: 0 0 16px; }.document-preview__markdown :deep(code) { padding: 2px 5px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; background: #f1f1f1; border-radius: 4px; }.document-preview__markdown :deep(pre) { padding: 14px; overflow: auto; background: #171717; border-radius: 10px; }.document-preview__markdown :deep(pre code) { padding: 0; color: #f5f5f5; background: transparent; }.document-preview__markdown :deep(table) { width: 100%; border-collapse: collapse; }.document-preview__markdown :deep(th), .document-preview__markdown :deep(td) { padding: 8px 10px; border: 1px solid #e5e5e5; }.document-preview__markdown :deep(th) { background: #f7f7f7; }.document-preview__text { width: min(710px, calc(100% - 48px)); min-height: calc(100% - 80px); margin: 0 auto; padding: 42px 0; overflow: visible; color: #1f2937; font: 14px/1.7 ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; white-space: pre-wrap; word-break: break-word; }
.document-preview__rename-modal { width: min(420px, calc(100vw - 40px)); }.document-preview__rename-actions { display: flex; gap: 8px; justify-content: flex-end; }
.document-preview__sheet-scroll { max-width: calc(100% - 92px); }
@media (max-width: 700px) { .document-preview__header { padding: 0 10px; }.document-preview__pdf-canvas-stage { padding: 14px 12px 60px; }.document-preview__sheet-wrap { padding: 14px; }.document-preview__sheet-scroll { max-width: calc(100vw - 28px); } }
</style>
