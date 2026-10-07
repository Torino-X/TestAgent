import { describe, expect, it } from 'vitest'
import view from './TemplateMarketView.vue?raw'
import picker from '@/components/templates/TemplatePickerDialog.vue?raw'
import upload from '@/components/templates/TemplateUploadDialog.vue?raw'
import preview from './DocumentPreviewView.vue?raw'
import workspace from '@/components/chat/ChatWorkspace.vue?raw'
import cover from '@/components/templates/TemplateCoverPreview.vue?raw'
import templateApi from '@/api/templateApi.ts?raw'

describe('template marketplace UI contracts', () => {
  it('keeps market view and save actions separate', () => {
    expect(view).toContain("@click=\"openPreview(item)\"")
    expect(view).toContain('class="template-card__actions" @click.stop')
    expect(view).toContain('保存模板')
    expect(view).toContain('已保存')
  })

  it('opens My Templates cards through the authorized owner preview route', () => {
    expect(view).toContain('class="template-card template-card--clickable"')
    expect(view).toContain('tabindex="0"')
    expect(view).toContain("if ('sourceType' in item)")
    expect(view).toContain("name: 'my-template-preview'")
    expect(view).toContain("userTemplateId: item.id")
    expect(view).toContain('class="template-card__actions" @click.stop @keydown.stop')
    expect(templateApi).toContain('export async function fetchMyTemplatePreview')
    expect(templateApi).toContain('/api/templates/mine/${encodeURIComponent(userTemplateId)}/preview')
  })

  it('renders the approved responsive card gallery with real cover previews', () => {
    expect(view).toContain('<TemplateCoverPreview :item="item" />')
    expect(view).toContain('class="templates-grid"')
    expect(view).toContain('class="template-card template-card--clickable"')
    expect(view).toContain('grid-template-columns:repeat(3,minmax(0,1fr))')
    expect(view).toContain('transform:translateY(-4px)')
    expect(view).toContain('box-shadow:0 14px 32px rgba(20,20,20,.10)')
    expect(view).toContain('@media(max-width:1100px)')
    expect(view).toContain('@media(max-width:720px)')
  })

  it('loads only visible covers and handles PDF, spreadsheet and fallback states', () => {
    expect(cover).toContain('IntersectionObserver')
    expect(cover).toContain('props.item.cover.url')
    expect(cover).not.toContain('fetchDocumentPreviewFromUrl')
    expect(cover).toContain("from 'pdfjs-dist'")
    expect(cover).toContain('document.getPage(1)')
    expect(cover).toContain('template-cover__sheet')
    expect(cover).toContain('template-cover__fallback')
  })

  it('supports loading, error, empty, upload and My Templates actions', () => {
    expect(view).toContain('templates-skeleton')
    expect(view).toContain('模板加载失败')
    expect(view).toContain('还没有我的模板')
    expect(upload).toContain('accept=".docx,.xlsx"')
    expect(view).toContain('使用模板')
    expect(view).toContain('下载模板')
    expect(view).toContain('取消发布')
  })

  it('aligns every template action with a fixed icon column', () => {
    expect(view).toContain(':menu-props="templateActionMenuProps"')
    expect(view).toContain("class: 'template-action-option__icon'")
    expect(view).toContain('.template-action-menu .n-dropdown-option-body__prefix{display:none')
    expect(view).toContain('.template-action-option__icon{display:grid;flex:0 0 18px')
    expect(view).not.toContain("icon: () => h(NIcon")
  })

  it('reuses an existing empty conversation when using a template', () => {
    expect(view).toContain('const conversation = await conversationStore.startNewConversation()')
    expect(view).toContain('await useTemplate(item.id, conversation.id)')
    expect(view).not.toContain('const result = await useTemplate(item.id);')
  })

  it('shows publisher, publication date, version, size and save count metadata', () => {
    expect(view).toContain('item.author.displayName')
    expect(view).toContain('formatDate(item.publishedAt || item.updatedAt)')
    expect(view).toContain('v{{ item.versionNo }}')
    expect(view).toContain('formatSize(item.fileSize)')
    expect(view).toContain('item.saveCount')
  })

  it('uses the shared custom selector for sorting, source filters, and upload category', () => {
    expect(view).toContain('aria-label="模板排序"')
    expect(view).toContain('aria-label="模板来源"')
    expect(view.match(/<AppSelect/g)?.length).toBe(2)
    expect(upload).toContain('<AppSelect')
    expect(view).not.toMatch(/<select(?:\s|>)/i)
    expect(upload).not.toMatch(/<select(?:\s|>)/i)
  })

  it('states and implements attachment-only picker behavior', () => {
    expect(picker).toContain('不会自动发送')
    expect(workspace).toContain('conversationStore.addDraftFile(result.conversation.id, result.uploadedFile)')
    expect(workspace).not.toContain('attachTemplate(item: UserTemplateItem) {\n  await handleSend')
  })

  it('reuses the document preview renderer in read-only mode', () => {
    expect(preview).toContain("route.name === 'template-preview'")
    expect(preview).toContain('fetchTemplatePreview')
    expect(preview).toContain("route.name === 'my-template-preview'")
    expect(preview).toContain('fetchMyTemplatePreview')
    expect(preview).toContain('preview && !isTemplatePreview')
  })
})
