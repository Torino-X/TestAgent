import { describe, expect, it } from 'vitest'
import cover from './TemplateCoverPreview.vue?raw'

describe('TemplateCoverPreview source contracts', () => {
  it('renders only the first PDF page into a bounded high-DPI canvas', () => {
    expect(cover).toContain('document.getPage(1)')
    expect(cover).not.toContain('for (let page')
    expect(cover).toContain('Math.min(window.devicePixelRatio || 1, 2)')
    expect(cover).toContain('canvas.value.width')
    expect(cover).toContain('canvas.value.height')
  })

  it('uses real spreadsheet cells and deterministic loading/error fallbacks', () => {
    expect(cover).toContain('props.item.cover.spreadsheet?.rows')
    expect(cover).toContain('封面准备中')
    expect(cover).toContain('封面解析暂不可用')
    expect(cover).toContain('文件完好，可直接在线预览')
  })

  it('loads the persisted cover descriptor directly without requesting preview metadata', () => {
    expect(cover).toContain('props.item.cover.url')
    expect(cover).not.toContain('fetchDocumentPreviewFromUrl')
    expect(cover).not.toContain('props.item.previewUrl')
  })
})
