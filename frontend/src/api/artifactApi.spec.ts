import { afterEach, describe, expect, it, vi } from 'vitest'
import { downloadArtifact } from './artifactApi'
import { clearAuthToken, setAuthToken } from './request'

function docxResponse(contentDisposition = 'attachment; filename="server.docx"') {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    headers: {
      get: vi.fn((name: string) => {
        if (name.toLowerCase() === 'content-type') {
          return 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        }
        if (name.toLowerCase() === 'content-disposition') return contentDisposition
        return null
      })
    },
    blob: vi.fn(async () => new Blob(['docx'], {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    }))
  }
}

describe('artifact download', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    clearAuthToken()
  })

  it('uses the authenticated Blob request and cleans up the hidden download link', async () => {
    vi.useFakeTimers()
    setAuthToken('test-token')

    const fetchMock = vi.fn().mockResolvedValue(docxResponse())
    const click = vi.fn()
    const remove = vi.fn()
    const anchor = { href: '', download: '', style: {}, click, remove }
    const appendChild = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('document', {
      createElement: vi.fn(() => anchor),
      body: { appendChild }
    })
    const createObjectURL = vi.fn(() => 'blob:test')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })

    await downloadArtifact('artifact/public id/1', 'fallback.docx')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/artifacts/artifact%2Fpublic%20id%2F1/download',
      expect.objectContaining({
        method: 'GET',
        credentials: 'include',
        headers: expect.objectContaining({ Authorization: 'Bearer test-token' })
      })
    )
    expect(anchor.download).toBe('server.docx')
    expect(anchor.href).toBe('blob:test')
    expect(appendChild).toHaveBeenCalledWith(anchor)
    expect(click).toHaveBeenCalledOnce()
    expect(remove).toHaveBeenCalledOnce()

    vi.runAllTimers()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:test')
  })

  it('decodes UTF-8 Content-Disposition filenames before downloading', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      docxResponse("attachment; filename*=UTF-8''%E6%B5%8B%E8%AF%95%E6%96%B9%E6%A1%88.docx")
    )
    const anchor = { href: '', download: '', style: {}, click: vi.fn(), remove: vi.fn() }
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('document', {
      createElement: vi.fn(() => anchor),
      body: { appendChild: vi.fn() }
    })
    vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:test'), revokeObjectURL: vi.fn() })

    await downloadArtifact('artifact_001')

    expect(anchor.download).toBe('测试方案.docx')
  })

  it('does not turn a JSON error payload into a docx download', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: 'OK',
      headers: {
        get: vi.fn((name: string) => name.toLowerCase() === 'content-type' ? 'application/json' : null)
      },
      blob: vi.fn(async () => new Blob([JSON.stringify({ message: '产物已失效' })], {
        type: 'application/json'
      }))
    })
    const click = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('document', {
      createElement: vi.fn(() => ({ href: '', download: '', style: {}, click, remove: vi.fn() })),
      body: { appendChild: vi.fn() }
    })
    vi.stubGlobal('URL', { createObjectURL: vi.fn(), revokeObjectURL: vi.fn() })

    await expect(downloadArtifact('artifact_001')).rejects.toThrow('产物已失效')
    expect(click).not.toHaveBeenCalled()
  })

  it.each([401, 403, 404, 410, 500])('maps HTTP status %s to an API error', async (status) => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status,
      statusText: 'Error',
      headers: { get: vi.fn(() => 'application/json') },
      json: vi.fn(async () => ({
        code: 'ARTIFACT_UNAVAILABLE',
        message: '产物暂不可用'
      }))
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(downloadArtifact('artifact_001')).rejects.toMatchObject({ status })
  })

  it('rejects an empty artifact id without making a request', async () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    await expect(downloadArtifact('  ')).rejects.toThrow()
    expect(fetchMock).not.toHaveBeenCalled()
  })
})
