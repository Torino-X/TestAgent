import { describe, expect, it, vi } from 'vitest'
import { parseSseBlock, useSse } from './useSse'

describe('useSse protocol parser', () => {
  it('reports a clean server EOF so task callers can reconcile terminal state', async () => {
    const encoder = new TextEncoder()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode('id: evt_42\\ndata: {"ok":true}\\n\\n'))
          controller.close()
        }
      })
    }))

    const onClose = vi.fn()
    const sse = useSse()
    sse.connect('/events', { onClose })

    await vi.waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    expect(sse.isConnected.value).toBe(false)

    sse.disconnect()
    vi.unstubAllGlobals()
  })

  it('keeps event id and parses the retry directive', () => {
    expect(parseSseBlock([
      'id: evt_42',
      'event: tool_finished',
      'retry: 1200',
      'data: {"tool":"RequirementParserTool"}'
    ].join('\n'))).toEqual({
      eventName: 'tool_finished',
      id: 'evt_42',
      retryMs: 1200,
      data: { tool: 'RequirementParserTool' }
    })
  })

  it('clamps unsafe retry values and accepts id-only frames', () => {
    expect(parseSseBlock('id: heartbeat\nretry: 1')).toEqual({
      eventName: 'message',
      id: 'heartbeat',
      retryMs: 250
    })
    expect(parseSseBlock('retry: 999999')).toEqual({
      eventName: 'message',
      retryMs: 30000
    })
  })

  it('preserves multiline text data when it is not JSON', () => {
    expect(parseSseBlock('event: agent_text_delta\ndata: first\ndata: second')).toEqual({
      eventName: 'agent_text_delta',
      data: 'first\nsecond'
    })
  })

  it('sends the latest backend event id when reconnecting', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('window', {
      setTimeout,
      clearTimeout
    })

    const encoder = new TextEncoder()
    const response = (frame: string) => ({
      ok: true,
      status: 200,
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode(frame))
          controller.close()
        }
      })
    })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response('id: evt_42\ndata: {"ok":true}\n\n'))
      .mockResolvedValueOnce(response('id: evt_43\ndata: {"ok":true}\n\n'))
    vi.stubGlobal('fetch', fetchMock)

    const sse = useSse()
    sse.connect('/events')
    await vi.waitFor(() => expect(sse.lastEventId.value).toBe('evt_42'))

    sse.reconnect()
    await vi.advanceTimersByTimeAsync(1000)
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))

    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      credentials: 'include',
      headers: expect.objectContaining({ 'Last-Event-ID': 'evt_42' })
    })
    expect(sse.lastEventId.value).toBe('evt_43')

    sse.disconnect()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })
})
