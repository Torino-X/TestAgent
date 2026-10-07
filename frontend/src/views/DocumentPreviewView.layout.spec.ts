import { describe, expect, it } from 'vitest'
import source from './DocumentPreviewView.vue?raw'

describe('DocumentPreviewView layout', () => {
  it('keeps the shared application sidebar visible around the preview', () => {
    expect(source).toContain('<AppLayout>')
    expect(source).toContain("import AppLayout from '@/components/layout/AppLayout.vue'")
    expect(source).toContain('height: 100%')
  })

  it('centers the unsupported-preview notice within the document stage', () => {
    expect(source).toContain('document-preview__empty document-preview__unsupported')
    expect(source).toContain('.document-preview__unsupported { position: absolute; inset: 0; }')
  })

  it('renders Word and PDF previews on application-owned canvas pages', () => {
    expect(source).toContain("import { GlobalWorkerOptions, getDocument, type PDFDocumentProxy } from 'pdfjs-dist'")
    expect(source).toContain('class="document-preview__pdf-canvas-stage"')
    expect(source).toContain('<canvas :ref="setPdfCanvas(page.number)" />')
    expect(source).not.toContain('<iframe')
  })

  it('keeps the PDF.js document instance outside Vue deep reactivity', () => {
    expect(source).toContain("import { computed, h, nextTick, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'")
    expect(source).toContain('const pdfDocument = shallowRef<PDFDocumentProxy | null>(null)')
  })

  it('waits for the page canvases to mount and ignores obsolete zoom renders', () => {
    expect(source).toContain('if (pdfDocument.value) await updatePdfPageLayouts()')
    expect(source).toContain('let pdfRenderVersion = 0')
    expect(source).toContain('const renderVersion = ++pdfRenderVersion')
    expect(source).toContain('if (renderVersion !== pdfRenderVersion) return')
  })

  it('polls a queued DOCX derivative and paints the first page before the remaining pages', () => {
    expect(source).toContain('async function waitForPreviewReady')
    expect(source).toContain("latest.previewStatus === 'ready' && latest.pdfUrl")
    expect(source).toContain('await renderPdfPage(pages[0].number, renderVersion)')
    expect(source).toContain('void renderRemainingPdfPages(pages.slice(1), renderVersion)')
  })

  it('uses the owner-authorized endpoint for My Templates while sharing the same renderer', () => {
    expect(source).toContain("route.name === 'my-template-preview'")
    expect(source).toContain('fetchMyTemplatePreview(itemId.value)')
    expect(source).toContain("if (isMyTemplatePreview.value) return '我的模板'")
    expect(source).toContain("query: { tab: 'mine' }")
  })

  it('returns Project-origin previews to their owning Project workspace', () => {
    expect(source).toContain('const isProjectOrigin = computed')
    expect(source).toContain("route.query.from === 'project'")
    expect(source).toContain("route.query.projectName || '项目'")
    expect(source).toContain('`/projects/${encodeURIComponent(String(route.query.projectId))}`')
  })

  it('uses a fixed icon column for every document action menu item', () => {
    expect(source).toContain(':menu-props="documentActionMenuProps"')
    expect(source).toContain(':show-arrow="false"')
    expect(source).toContain("class: 'document-preview-action-menu'")
    expect(source).toContain("class: 'document-preview-action-option__icon'")
    expect(source).toContain("class: 'document-preview-action-option__label'")
    expect(source).toContain('.document-preview-action-option__icon')
    expect(source).toContain('flex: 0 0 18px')
    expect(source).toContain('.document-preview-action-menu .n-dropdown-option-body__prefix')
    expect(source).not.toContain('icon: () => h(NIcon')
  })
})
