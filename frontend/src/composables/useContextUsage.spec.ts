import { describe, expect, it, vi } from 'vitest'
import { useContextUsage } from './useContextUsage'
import type { ContextCompactResponse, ContextUsageResponse } from '@/types'

function usage(conversationId: string): ContextUsageResponse {
  return {
    conversation_public_id: conversationId,
    model: {
      name: 'qwen3.7-max',
      context_window_tokens: 128000,
      window_source: 'model_config'
    },
    usage: {
      used_tokens: 86000,
      available_tokens: 42000,
      percent: 67.2,
      count_mode: 'exact',
      estimated: false,
      over_limit: false
    },
    breakdown: {
      conversation_history: 61000,
      project_documents: 12000,
      task_context: 7000,
      user_memory: 4000,
      system_instructions: 2000
    },
    compaction: {
      available: true,
      recommended: false,
      in_progress: false
    },
    available: true,
    snapshot_public_id: 'ctxsnap_123',
    as_of: '2026-08-08T12:00:00Z'
  }
}

function compactResult(): ContextCompactResponse {
  return {
    run_public_id: 'ctxcmp_123',
    summary_public_id: 'ctxsum_456',
    before_used_tokens: 86000,
    after_used_tokens: 32000,
    saved_tokens: 54000,
    summary_updated: true,
    as_of: '2026-08-08T12:05:00Z',
    in_progress: false
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('useContextUsage', () => {
  it('does not let a stale conversation response overwrite the active conversation', async () => {
    let activeConversationId = 'conv_a'
    const first = deferred<ContextUsageResponse>()
    const second = deferred<ContextUsageResponse>()
    const fetchUsage = vi
      .fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)

    const controller = useContextUsage({
      getConversationId: () => activeConversationId,
      fetchUsage,
      compact: vi.fn(),
      notifySuccess: vi.fn(),
      notifyError: vi.fn()
    })

    const firstRefresh = controller.refresh('conversation-open')
    activeConversationId = 'conv_b'
    const secondRefresh = controller.refresh('conversation-switch')

    second.resolve(usage('conv_b'))
    await secondRefresh
    first.resolve(usage('conv_a'))
    await firstRefresh

    expect(controller.usage.value?.conversation_public_id).toBe('conv_b')
  })

  it('refetches usage after a successful manual compaction', async () => {
    const fetchUsage = vi.fn().mockResolvedValue(usage('conv_a'))
    const compact = vi.fn().mockResolvedValue(compactResult())
    const notifySuccess = vi.fn()

    const controller = useContextUsage({
      getConversationId: () => 'conv_a',
      fetchUsage,
      compact,
      notifySuccess,
      notifyError: vi.fn()
    })

    await controller.compactContext()

    expect(compact).toHaveBeenCalledWith('conv_a')
    expect(fetchUsage).toHaveBeenCalledTimes(1)
    expect(controller.compactionState.value).toBe('success')
    expect(notifySuccess).toHaveBeenCalledWith('上下文已轻度压缩')
  })

  it('reports the deep retention tier returned by manual compaction', async () => {
    const notifySuccess = vi.fn()
    const controller = useContextUsage({
      getConversationId: () => 'conv_deep',
      fetchUsage: vi.fn().mockResolvedValue(usage('conv_deep')),
      compact: vi.fn().mockResolvedValue({
        ...compactResult(),
        strategy: 'deep',
        preserved_complete_turns: 12
      }),
      notifySuccess,
      notifyError: vi.fn()
    })

    await controller.compactContext()

    expect(notifySuccess).toHaveBeenCalledWith('上下文已深度压缩，保留最近 12 轮对话')
  })

  it('keeps the widget recoverable when compaction fails', async () => {
    const notifyError = vi.fn()
    const controller = useContextUsage({
      getConversationId: () => 'conv_a',
      fetchUsage: vi.fn(),
      compact: vi.fn().mockRejectedValue(new Error('compact failed')),
      notifySuccess: vi.fn(),
      notifyError
    })

    await controller.compactContext()

    expect(controller.compacting.value).toBe(false)
    expect(controller.compactionState.value).toBe('idle')
    expect(controller.unavailable.value).toBe(false)
    expect(notifyError).toHaveBeenCalledWith('compact failed')
  })

  it('marks usage unavailable without disabling chat-facing controls', async () => {
    const controller = useContextUsage({
      getConversationId: () => 'conv_a',
      fetchUsage: vi.fn().mockRejectedValue(new Error('usage failed')),
      compact: vi.fn(),
      notifySuccess: vi.fn(),
      notifyError: vi.fn()
    })

    await controller.refresh('conversation-open')

    expect(controller.unavailable.value).toBe(true)
    expect(controller.controlDisabled.value).toBe(false)
  })

  // P1-2 收口:真正的 in-flight dedup. 同一个 conversationId 多次 refresh() 只
  // 产生 1 个 fetchUsage 调用。AbortController 方案会先 abort 再重发,产生 2 次 HTTP。
  it('dedups concurrent refresh() calls for the same conversation', async () => {
    const d = deferred<ContextUsageResponse>()
    const fetchUsage = vi.fn().mockReturnValueOnce(d.promise)

    const controller = useContextUsage({
      getConversationId: () => 'conv_dedup',
      fetchUsage,
      compact: vi.fn(),
      notifySuccess: vi.fn(),
      notifyError: vi.fn()
    })

    const r1 = controller.refresh('conversation-open')
    const r2 = controller.refresh('agent-task-terminal')
    const r3 = controller.refresh('popover-open')

    d.resolve(usage('conv_dedup'))
    await Promise.all([r1, r2, r3])

    // 3 个并发 refresh → fetchUsage 只应被调用 1 次
    expect(fetchUsage).toHaveBeenCalledTimes(1)
    expect(controller.usage.value?.conversation_public_id).toBe('conv_dedup')
  })

  it('after first request resolves, second refresh triggers a new fetch', async () => {
    const fetchUsage = vi
      .fn()
      .mockResolvedValueOnce(usage('conv_seq'))
      .mockResolvedValueOnce(usage('conv_seq'))

    const controller = useContextUsage({
      getConversationId: () => 'conv_seq',
      fetchUsage,
      compact: vi.fn(),
      notifySuccess: vi.fn(),
      notifyError: vi.fn()
    })

    await controller.refresh('conversation-open')
    await controller.refresh('agent-task-terminal')

    expect(fetchUsage).toHaveBeenCalledTimes(2)
  })

  it('replaces the idle estimate with a draft-aware next-request preview', async () => {
    const previewUsage = vi.fn().mockResolvedValue({
      ...usage('conv_preview'),
      snapshot_public_id: null,
      source: 'next_request_preview' as const,
      usage: {
        ...usage('conv_preview').usage,
        used_tokens: 3210
      }
    })
    const controller = useContextUsage({
      getConversationId: () => 'conv_preview',
      fetchUsage: vi.fn(),
      previewUsage,
      compact: vi.fn(),
      notifySuccess: vi.fn(),
      notifyError: vi.fn()
    })

    await controller.preview({ content: '继续分析退款项目', attached_file_ids: ['file_1'] })

    expect(previewUsage).toHaveBeenCalledWith(
      'conv_preview',
      { content: '继续分析退款项目', attached_file_ids: ['file_1'] },
      expect.objectContaining({ signal: expect.any(AbortSignal) })
    )
    expect(controller.usage.value?.source).toBe('next_request_preview')
    expect(controller.usage.value?.usage.used_tokens).toBe(3210)
  })
})
