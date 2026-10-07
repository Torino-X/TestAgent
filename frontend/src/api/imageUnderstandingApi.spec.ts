import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getImageUnderstandingConfig,
  testImageUnderstandingConnection,
  updateImageUnderstandingConfig
} from './imageUnderstandingApi'

function okResponse(data: unknown) {
  return new Response(
    JSON.stringify({
      code: 0,
      message: 'success',
      data,
      request_id: 'req_img_understanding'
    }),
    { status: 200, headers: { 'Content-Type': 'application/json' } }
  )
}

describe('imageUnderstandingApi', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('loads image-understanding config through the backend proxy', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        config_id: 'img_understanding_001',
        api_base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        api_key_masked: 'sk-****abcd',
        api_key_set: true,
        model_name: 'qwen-vl-plus',
        timeout_seconds: 60,
        max_tokens: null,
        enable_in_doc_parsing: true
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(getImageUnderstandingConfig()).resolves.toMatchObject({
      config_id: 'img_understanding_001',
      model_name: 'qwen-vl-plus',
      enable_in_doc_parsing: true
    })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/image-understanding/config',
      expect.objectContaining({ method: 'GET' })
    )
  })

  it('does not send a masked API key when saving config', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        config_id: 'img_understanding_001',
        saved: true,
        api_key_set: true,
        api_key_masked: 'sk-****efgh',
        updated_at: '2026-06-30T12:00:00Z'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    await updateImageUnderstandingConfig({
      api_base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1',
      api_key: 'sk-****abcd',
      model_name: 'qwen-vl-plus',
      timeout_seconds: 60,
      max_tokens: null,
      enable_in_doc_parsing: true
    })

    const requestInit = fetchMock.mock.calls[0][1] as RequestInit
    expect(fetchMock.mock.calls[0][0]).toBe('/api/image-understanding/config')
    expect(requestInit.method).toBe('POST')
    const body = JSON.parse(String(requestInit.body))
    expect(body).not.toHaveProperty('api_key')
    expect(body.model_name).toBe('qwen-vl-plus')
    expect(body.enable_in_doc_parsing).toBe(true)
  })

  it('tests the connection through the dedicated test endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        success: true,
        latency_ms: 412,
        status: 'success',
        message: '连接成功',
        error_code: null,
        tested_at: '2026-06-30T12:01:00Z'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await testImageUnderstandingConnection()
    expect(result.success).toBe(true)
    expect(result.status).toBe('success')

    const requestInit = fetchMock.mock.calls[0][1] as RequestInit
    expect(fetchMock.mock.calls[0][0]).toBe('/api/image-understanding/config/test')
    expect(requestInit.method).toBe('POST')
  })

  it('propagates failure payloads from the test endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        success: false,
        latency_ms: 120,
        status: 'failed',
        message: 'API Key 未配置或解密失败',
        error_code: 'IMAGE_UNDERSTANDING_41202',
        tested_at: '2026-06-30T12:02:00Z'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await testImageUnderstandingConnection()
    expect(result.success).toBe(false)
    expect(result.error_code).toBe('IMAGE_UNDERSTANDING_41202')
  })
})
