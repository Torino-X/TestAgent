import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearAuthToken } from './request'
import { fetchMarketTemplates, fetchMyTemplates, uploadTemplate, useTemplate } from './templateApi'

function ok(data: unknown) { return Promise.resolve(new Response(JSON.stringify({ code: 0, message: 'success', data }), { status: 200, headers: { 'Content-Type': 'application/json' } })) }
const base = {
  id: 'tpl_1', template_id: 'tpl_1', name: 'Web 测试方案', category_code: 'test_plan', description: '描述', tags: ['Web'], file_ext: 'docx', file_size: 2048, version_no: 1,
  author: { id: 'usr_1', display_name: '测试工程师' }, save_count: 7, published_at: '2026-09-14T09:00:00Z', updated_at: '2026-09-15T10:00:00Z', preview_url: '/api/templates/market/tpl_1/preview',
  cover: { status: 'ready', kind: 'pdf', url: '/api/templates/market/tpl_1/preview/pdf', spreadsheet: null }
}

describe('template API', () => {
  afterEach(() => { clearAuthToken(); vi.unstubAllGlobals() })

  it('maps market and mine pagination without exposing snake_case to views', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(ok({ items: [{ ...base, is_saved: true }], total: 1, page: 1, page_size: 20, has_more: false }))
      .mockResolvedValueOnce(ok({ items: [{ ...base, id: 'utpl_1', visibility: 'private', status: 'active', source_type: 'owner', last_used_at: null, download_url: '/api/templates/mine/utpl_1/download', preview_url: '/api/templates/mine/utpl_1/preview' }], total: 1, page: 1, page_size: 20, has_more: false }))
    vi.stubGlobal('fetch', fetchMock)
    const market = await fetchMarketTemplates({ q: 'Web', category: 'test_plan', sort: 'saves' })
    const mine = await fetchMyTemplates({ sourceType: 'owner' })
    expect(market.items[0]).toEqual(expect.objectContaining({ templateId: 'tpl_1', isSaved: true, saveCount: 7, fileExt: 'docx', publishedAt: '2026-09-14T09:00:00Z' }))
    expect(market.items[0].cover).toEqual({ status: 'ready', kind: 'pdf', url: '/api/templates/market/tpl_1/preview/pdf', spreadsheet: null })
    expect(mine.items[0]).toEqual(expect.objectContaining({ id: 'utpl_1', sourceType: 'owner', downloadUrl: '/api/templates/mine/utpl_1/download', previewUrl: '/api/templates/mine/utpl_1/preview' }))
    expect(fetchMock.mock.calls[0][0]).toContain('sort=saves')
    expect(fetchMock.mock.calls[1][0]).toContain('source_type=owner')
  })

  it('uploads the documented multipart fields', async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ ...base, id: 'utpl_1', visibility: 'private', status: 'active', source_type: 'owner', last_used_at: null, download_url: '/download' }))
    vi.stubGlobal('fetch', fetchMock)
    const file = Object.assign(new Blob(['docx'], { type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' }), { name: 'plan.docx', lastModified: 0 }) as File
    await uploadTemplate({ file, name: '测试方案', categoryCode: 'test_plan', description: '说明', tags: ['Web'], publish: true })
    const body = fetchMock.mock.calls[0][1].body as FormData
    expect((body.get('file') as File).name).toBe('plan.docx')
    expect(body.get('category_code')).toBe('test_plan')
    expect(body.get('tags_json')).toBe('["Web"]')
    expect(body.get('publish')).toBe('true')
  })

  it('maps Use into an existing chat attachment without sending a message request', async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({
      conversation: { id: 'conv_1', conversation_id: 'conv_1', title: '新会话', status: 'active', message_count: 0, file_count: 1, updated_at: '2026-09-15T10:00:00Z' },
      uploaded_file: { id: 'file_1', original_name: 'plan.docx', file_ext: '.docx', file_size: 2048, file_type: 'test_plan', upload_status: 'uploaded' }
    }))
    vi.stubGlobal('fetch', fetchMock)
    const result = await useTemplate('utpl_1', 'conv_1')
    expect(result.conversation).toEqual(expect.objectContaining({ id: 'conv_1', state: 'empty' }))
    expect(result.uploadedFile).toEqual(expect.objectContaining({ id: 'file_1', name: 'plan.docx' }))
    expect('localOnly' in result.uploadedFile).toBe(false)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/templates/mine/utpl_1/use')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ conversation_id: 'conv_1' })
  })
})
