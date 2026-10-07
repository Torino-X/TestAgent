import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  fetchCapabilities,
  fetchCapabilityConfig,
  saveModelSettings,
  saveCapabilityConfig,
  fetchNarrativeSettings,
  saveNarrativeSettings
} from './settingsApi'

function okResponse(data: unknown) {
  return new Response(
    JSON.stringify({
      code: 0,
      message: 'success',
      data,
      request_id: 'req_cap'
    }),
    { status: 200, headers: { 'Content-Type': 'application/json' } }
  )
}

describe('settingsApi capability config', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('fetches all capabilities grouped by type', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        capabilities: {
          chat: [
            {
              configured: true,
              capability_type: 'chat',
              api_base_url: 'https://api.example.com/v1',
              model_name: 'gpt-4o',
              api_key_masked: 'sk-****abcd'
            }
          ],
          embedding: [],
          reranker: []
        }
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchCapabilities()
    expect(result.capabilities.chat).toHaveLength(1)
    expect(result.capabilities.chat[0].capability_type).toBe('chat')
    expect(result.capabilities.embedding).toHaveLength(0)
  })

  it('fetches a single capability config', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        configured: true,
        capability_type: 'embedding',
        api_base_url: 'https://api.example.com/v1',
        model_name: 'text-embedding',
        embedding_dimension: 1536,
        api_key_masked: 'sk-****abcd'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const cfg = await fetchCapabilityConfig('embedding')
    expect(cfg.embedding_dimension).toBe(1536)
    expect(cfg.model_name).toBe('text-embedding')
  })

  it('saves capability config and masks the api key when it is masked', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: true }))
    vi.stubGlobal('fetch', fetchMock)

    await saveCapabilityConfig('embedding', {
      capability_type: 'embedding',
      api_base_url: 'https://api.example.com/v1',
      api_key: 'sk-****masked', // masked → 不发送 api_key
      model_name: 'text-embedding',
      embedding_dimension: 1536
    })

    const [[, init]] = fetchMock.mock.calls
    const body = JSON.parse(init.body as string)
    // masked key 不包含在 payload 中
    expect('api_key' in body).toBe(false)
    expect(body.capability_type).toBe('embedding')
    expect(body.embedding_dimension).toBe(1536)
  })

  it('sends real api_key when not masked', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: true }))
    vi.stubGlobal('fetch', fetchMock)

    await saveCapabilityConfig('reranker', {
      capability_type: 'reranker',
      api_base_url: 'https://api.example.com/v1',
      api_key: 'sk-real-new-key',
      model_name: 'rerank-model',
      pre_rerank_limit: 100
    })

    const [[, init]] = fetchMock.mock.calls
    const body = JSON.parse(init.body as string)
    expect(body.api_key).toBe('sk-real-new-key')
    expect(body.pre_rerank_limit).toBe(100)
  })

  it('saves primary model context window in K units', async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ success: true }))
    vi.stubGlobal('fetch', fetchMock)

    await saveModelSettings({
      apiBaseUrl: 'https://api.example.com/v1',
      apiKey: 'sk-real-new-key',
      modelName: 'qwen3.7-max',
      timeoutSeconds: 120,
      knowledgeBaseUrl: '',
      knowledgeCollection: '',
      enableKnowledgeBase: false,
      maxFileSizeMb: 30,
      maxFilesPerConversation: 6,
      allowedExtensions: [],
      capabilityType: 'chat',
      contextWindowK: 32
    })

    const [[, init]] = fetchMock.mock.calls
    const body = JSON.parse(init.body as string)
    expect(body.context_window_k).toBe(32)
    expect('context_window_tokens' in body).toBe(false)
  })

  it('fetches tool-card narrative visibility with env-backed detail level', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        enabled: false,
        detail_level: 'detailed',
        detail_level_source: 'env'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    const result = await fetchNarrativeSettings()

    expect(result.enabled).toBe(false)
    expect(result.detail_level).toBe('detailed')
  })

  it('saves only tool-card narrative visibility', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        enabled: false,
        detail_level: 'standard',
        detail_level_source: 'env'
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    await saveNarrativeSettings(false)

    const [[, init]] = fetchMock.mock.calls
    const body = JSON.parse(init.body as string)
    expect(body).toEqual({ enabled: false })
  })
})
