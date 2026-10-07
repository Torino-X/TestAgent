import { afterEach, describe, expect, it, vi } from 'vitest'
import { clearHelpApiCache, fetchHelpArticle, fetchHelpCatalog, searchHelp } from './helpApi'

function ok(data: unknown) {
  return Promise.resolve(new Response(JSON.stringify({ code: 0, message: 'success', data }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' }
  }))
}

describe('Help API', () => {
  afterEach(() => {
    clearHelpApiCache()
    vi.unstubAllGlobals()
  })

  it('caches the catalog and individual articles', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(ok({ site: { title: '帮助中心', description: '说明' }, sections: [] }))
      .mockResolvedValueOnce(ok({ slug: 'quick-start', title: '快速开始', description: '', section: { id: 'start', title: '开始' }, markdown: '# 内容', headings: [], reading_time_minutes: 1 }))
    vi.stubGlobal('fetch', fetchMock)

    await fetchHelpCatalog()
    await fetchHelpCatalog()
    await fetchHelpArticle('quick-start')
    await fetchHelpArticle('quick-start')

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/help')
    expect(fetchMock.mock.calls[1][0]).toBe('/api/help/articles/quick-start')
  })

  it('encodes search input and forwards the bounded limit', async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ query: '章节 确认', items: [] }))
    vi.stubGlobal('fetch', fetchMock)

    await searchHelp('章节 确认', 12)

    expect(fetchMock.mock.calls[0][0]).toBe('/api/help/search?q=%E7%AB%A0%E8%8A%82+%E7%A1%AE%E8%AE%A4&limit=12')
  })
})
