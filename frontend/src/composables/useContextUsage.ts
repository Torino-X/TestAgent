import { computed, ref } from 'vue'
import type { ContextCompactResponse, ContextUsagePreviewRequest, ContextUsageResponse } from '@/types'

export type ContextUsageRefreshReason =
  | 'conversation-open'
  | 'conversation-switch'
  | 'chat-completed'
  | 'agent-task-terminal'
  | 'manual-compact-success'
  | 'popover-open'

export type ContextCompactionUiState = 'idle' | 'loading' | 'success'

export interface ContextUsageDependencies {
  getConversationId: () => string | null | undefined
  fetchUsage: (
    conversationId: string,
    opts?: { signal?: AbortSignal }
  ) => Promise<ContextUsageResponse>
  previewUsage?: (
    conversationId: string,
    payload: ContextUsagePreviewRequest,
    opts?: { signal?: AbortSignal }
  ) => Promise<ContextUsageResponse>
  compact: (conversationId: string) => Promise<ContextCompactResponse>
  notifySuccess: (message: string) => void
  notifyError: (message: string) => void
}

const POPOVER_REFRESH_INTERVAL_MS = 5000

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

export function useContextUsage(deps: ContextUsageDependencies) {
  const usage = ref<ContextUsageResponse | null>(null)
  const loading = ref(false)
  const unavailable = ref(false)
  const compacting = ref(false)
  const compactionState = ref<ContextCompactionUiState>('idle')
  const controlDisabled = computed(() => false)

  let activeRequest: AbortController | null = null
  let lastPopoverRefreshAt = 0
  // P1-2 收口:per-conversationId in-flight promise map,确保**同一 conversation
  // 并发触发 refresh() 时只产生 1 个 HTTP 请求**(之前 AbortController 方案会先
  // abort 再重发,产生 2 次 HTTP).
  const inflightByConversationId = new Map<string, Promise<ContextUsageResponse | null>>()

  async function refresh(reason: ContextUsageRefreshReason): Promise<void> {
    const conversationId = deps.getConversationId()
    if (!conversationId) {
      activeRequest?.abort()
      usage.value = null
      loading.value = false
      unavailable.value = false
      return
    }

    if (reason === 'popover-open') {
      const now = Date.now()
      if (now - lastPopoverRefreshAt < POPOVER_REFRESH_INTERVAL_MS) return
      lastPopoverRefreshAt = now
    }

    // P1-2 in-flight dedup: 若同一 conversation 已有进行中的请求, 复用.
    const existing = inflightByConversationId.get(conversationId)
    if (existing) {
      loading.value = true
      try {
        const response = await existing
        if (
          response &&
          !activeRequest?.signal.aborted &&
          deps.getConversationId() === conversationId
        ) {
          usage.value = response
          unavailable.value = false
        }
      } catch (error) {
        if (isAbortError(error)) return
        if (deps.getConversationId() === conversationId) unavailable.value = true
      } finally {
        if (deps.getConversationId() === conversationId) loading.value = false
      }
      return
    }

    activeRequest?.abort()
    const controller = new AbortController()
    activeRequest = controller
    const ownerConversationId = conversationId
    loading.value = true

    const promise = (async () => {
      try {
        const response = await deps.fetchUsage(ownerConversationId, { signal: controller.signal })
        return response
      } finally {
        inflightByConversationId.delete(ownerConversationId)
      }
    })()
    inflightByConversationId.set(conversationId, promise)

    try {
      const response = await promise
      if (controller.signal.aborted || deps.getConversationId() !== ownerConversationId) return
      usage.value = response
      unavailable.value = false
    } catch (error) {
      if (isAbortError(error)) return
      if (deps.getConversationId() === ownerConversationId) {
        unavailable.value = true
      }
    } finally {
      if (activeRequest === controller) activeRequest = null
      if (deps.getConversationId() === ownerConversationId) loading.value = false
    }
  }

  async function compactContext(): Promise<void> {
    const conversationId = deps.getConversationId()
    if (!conversationId || compacting.value) return

    compacting.value = true
    compactionState.value = 'loading'
    try {
      const result = await deps.compact(conversationId)
      if (deps.getConversationId() !== conversationId) return
      compactionState.value = 'success'
      const retentionMessage = result.preserved_complete_turns
        ? `，保留最近 ${result.preserved_complete_turns} 轮对话`
        : ''
      deps.notifySuccess(
        result.strategy === 'deep'
          ? `上下文已深度压缩${retentionMessage}`
          : `上下文已轻度压缩${retentionMessage}`
      )
      // Invalidate in-flight map so post-compact refresh actually fires
      inflightByConversationId.delete(conversationId)
      await refresh('manual-compact-success')
    } catch (error) {
      if (deps.getConversationId() === conversationId) {
        compactionState.value = 'idle'
        deps.notifyError(errorMessage(error, '上下文压缩失败'))
      }
    } finally {
      compacting.value = false
    }
  }

  async function preview(payload: ContextUsagePreviewRequest): Promise<void> {
    const conversationId = deps.getConversationId()
    if (!conversationId || !deps.previewUsage) {
      await refresh('conversation-open')
      return
    }

    activeRequest?.abort()
    const controller = new AbortController()
    activeRequest = controller
    loading.value = true
    try {
      const response = await deps.previewUsage(conversationId, payload, {
        signal: controller.signal
      })
      if (controller.signal.aborted || deps.getConversationId() !== conversationId) return
      usage.value = response
      unavailable.value = false
    } catch (error) {
      if (isAbortError(error)) return
      if (deps.getConversationId() === conversationId) unavailable.value = true
    } finally {
      if (activeRequest === controller) activeRequest = null
      if (deps.getConversationId() === conversationId) loading.value = false
    }
  }

  function dispose(): void {
    activeRequest?.abort()
    activeRequest = null
    inflightByConversationId.clear()
  }

  return {
    usage,
    loading,
    unavailable,
    compacting,
    compactionState,
    controlDisabled,
    refresh,
    preview,
    compactContext,
    dispose
  }
}
