import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchLibraryItems } from './libraryApi'

describe('library API contract', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('maps the backend library contract and keeps filters/search parameters', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      code: 0,
      message: 'success',
      data: {
        items: [{
          id: 'art_1', name: 'plan.docx', kind: 'file', source: 'generated',
          mime_type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          extension: '.docx', size_bytes: 2048, modified_at: '2026-09-05T08:00:00Z',
          artifact_type: 'test_plan_word', download_url: '/api/library/items/art_1/download'
        }], total: 76, page: 2, page_size: 25, has_more: true
      }
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchLibraryItems('file', 'plan', { source: 'generated', fileType: 'document', page: 2, pageSize: 25 })

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/library/items?category=file&scope=active&source=generated&file_type=document&page=2&page_size=25&q=plan')
    expect(result).toEqual({
      items: [{
        id: 'art_1', name: 'plan.docx', kind: 'file', source: 'generated',
        mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        extension: '.docx', sizeBytes: 2048, modifiedAt: '2026-09-05T08:00:00Z',
        artifactType: 'test_plan_word', downloadUrl: '/api/library/items/art_1/download'
      }], total: 76, page: 2, pageSize: 25, hasMore: true
    })
  })
})
