import { afterEach, describe, expect, it, vi } from 'vitest'
import { sendMessage, sendMessageStream } from './messageApi'
import { clearAuthToken } from './request'

function okResponse(data: unknown) {
  return Promise.resolve(
    new Response(JSON.stringify({ code: 0, message: 'success', data }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    })
  )
}

function emptyStreamResponse() {
  return Promise.resolve(
    new Response(
      new ReadableStream({
        start(controller) {
          controller.close()
        }
      }),
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } }
    )
  )
}

describe('messageApi knowledge mode snapshot', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    clearAuthToken()
  })

  it('includes knowledge_mode_snapshot in non-stream sends', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      okResponse({
        message: {
          message_id: 'msg_1',
          role: 'user',
          message_type: 'user_text',
          content: '什么是 Redis？',
          payload: {}
        },
        agent_task: null,
        agent_reply: null
      })
    )
    vi.stubGlobal('fetch', fetchMock)

    await sendMessage('conv_1', '什么是 Redis？', ['file_1'], 'MAAS_STRICT')

    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      content: '什么是 Redis？',
      attached_file_ids: ['file_1'],
      knowledge_mode_snapshot: 'MAAS_STRICT'
    })
  })

  it('includes knowledge_mode_snapshot in stream sends', async () => {
    const fetchMock = vi.fn().mockResolvedValue(emptyStreamResponse())
    vi.stubGlobal('fetch', fetchMock)

    await sendMessageStream('conv_1', '总结这个文档', undefined, {}, undefined, 'AUTO')

    expect(fetchMock.mock.calls[0][1]).toMatchObject({
      credentials: 'include'
    })
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      content: '总结这个文档',
      knowledge_mode_snapshot: 'AUTO'
    })
  })

  it('keeps an agent-shaped stream message on the assistant side', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Promise.resolve(
        new Response(
          [
            'event: agent_reply_created',
            'data: {"message":{"message_id":"agent_1","role":"user","message_type":"agent_text","content":""}}',
            '',
            ''
          ].join('\n'),
          { status: 200, headers: { 'Content-Type': 'text/event-stream' } }
        )
      )
    )
    vi.stubGlobal('fetch', fetchMock)
    const onAgentReplyCreated = vi.fn()

    await sendMessageStream('conv_1', '测试', undefined, { onAgentReplyCreated })

    expect(onAgentReplyCreated).toHaveBeenCalledWith(
      expect.objectContaining({
        message: expect.objectContaining({ id: 'agent_1', role: 'agent', type: 'agent_text' })
      })
    )
  })

  it('finishes at agent_text_done even when the transport never sends EOF', async () => {
    const encoder = new TextEncoder()
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode([
              'event: agent_text_done',
              'data: {"message":{"message_id":"user_1","role":"user","message_type":"user_text","content":"question"},"agent_task":null,"agent_reply":{"message_id":"agent_1","role":"agent","message_type":"agent_text","content":"answer"}}',
              '',
              ''
            ].join('\n')))
            // Deliberately do not close: this mirrors a proxy/streaming
            // transport that has delivered the business terminal event but
            // keeps the HTTP response open.
          }
        }),
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } }
      )
    )
    vi.stubGlobal('fetch', fetchMock)
    const onAgentTextDone = vi.fn()

    const completed = await Promise.race([
      sendMessageStream('conv_1', 'question', undefined, { onAgentTextDone })
        .then(() => 'completed'),
      new Promise((resolve) => setTimeout(() => resolve('timed_out'), 100))
    ])

    expect(completed).toBe('completed')
    expect(onAgentTextDone).toHaveBeenCalledTimes(1)
  })

  it('treats a legacy completed reply envelope as terminal without waiting for EOF', async () => {
    const encoder = new TextEncoder()
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        new ReadableStream({
          start(controller) {
            controller.enqueue(encoder.encode([
              'event: agent_reply_created',
              'data: {"message":{"message_id":"user_1","role":"user","message_type":"user_text","content":"question"},"agent_task":null,"agent_reply":{"message_id":"agent_1","role":"agent","message_type":"agent_text","content":"answer"}}',
              '',
              ''
            ].join('\n')))
            // A legacy backend branch persisted the full reply in this event
            // but did not send agent_text_done or close the transport.
          }
        }),
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } }
      )
    )
    vi.stubGlobal('fetch', fetchMock)
    const onAgentTextDone = vi.fn()

    const completed = await Promise.race([
      sendMessageStream('conv_1', 'question', undefined, { onAgentTextDone })
        .then(() => 'completed'),
      new Promise((resolve) => setTimeout(() => resolve('timed_out'), 100))
    ])

    expect(completed).toBe('completed')
    expect(onAgentTextDone).toHaveBeenCalledWith(
      expect.objectContaining({
        agent_reply: expect.objectContaining({ id: 'agent_1', text: 'answer' })
      })
    )
  })
})
