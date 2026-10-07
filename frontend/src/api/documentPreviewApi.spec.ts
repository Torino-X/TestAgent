import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchDocumentPreview } from './documentPreviewApi'

describe('document preview API contract', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('maps spreadsheet previews without exposing server storage data', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: 0, message: 'success', data: {
        item: { id: 'file_1', name: 'cases.xlsx', kind: 'file', source: 'upload', extension: '.xlsx', modified_at: '2026-09-05T08:00:00Z' },
        viewer: 'spreadsheet', spreadsheet: { sheet_name: '测试表', columns: ['A', 'B'], rows: [['项目名称', 'TestAgent']], truncated: false }
      }
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchDocumentPreview('file_1')).resolves.toMatchObject({
      item: { id: 'file_1', name: 'cases.xlsx', kind: 'file', source: 'upload', extension: '.xlsx', modifiedAt: '2026-09-05T08:00:00Z' },
      viewer: 'spreadsheet', spreadsheet: { sheetName: '测试表', columns: ['A', 'B'], rows: [['项目名称', 'TestAgent']], truncated: false }, pdfUrl: undefined, textContent: undefined
    })
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/library/items/file_1/preview')
  })

  it('maps queued Word-preview state without exposing a PDF URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: 0, message: 'success', data: {
        item: { id: 'file_1', name: 'cases.docx', kind: 'file', source: 'upload', extension: '.docx', modified_at: '2026-09-05T08:00:00Z' },
        viewer: 'word', preview_status: 'processing', preview_error: null
      }
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(fetchDocumentPreview('file_1')).resolves.toMatchObject({
      viewer: 'word', previewStatus: 'processing', pdfUrl: undefined
    })
  })

  it('does not misreport an interrupted preview request as a stopped backend', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    await expect(fetchDocumentPreview('file_1')).rejects.toMatchObject({
      message: '文档预览请求被中断，请稍后重试。',
      status: 0
    })
  })
})
