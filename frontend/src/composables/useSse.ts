import { ref, type Ref } from 'vue'
import { getAuthToken } from '@/api/request'

export interface SseCallbacks {
  onMessage?: (eventName: string, data: unknown) => void
  onError?: (error: Event | Error) => void
  onOpen?: () => void
  /** The server ended the response normally (which does not raise fetch). */
  onClose?: () => void
}

export interface UseSseReturn {
  isConnected: Ref<boolean>
  isReconnecting: Ref<boolean>
  lastEventId: Ref<string>
  connect: (url: string, callbacks?: SseCallbacks, initialCursor?: string) => void
  disconnect: () => void
  reconnect: () => void
}

function resolveUrl(url: string): string {
  const base = import.meta.env.VITE_API_BASE_URL as string | undefined
  if (!base || /^https?:\/\//i.test(url)) return url

  const normalizedBase = base.replace(/\/$/, '')
  const normalizedUrl = url.startsWith('/') ? url : `/${url}`
  if (normalizedBase.endsWith('/api') && normalizedUrl.startsWith('/api/')) {
    return `${normalizedBase}${normalizedUrl.slice(4)}`
  }
  return `${normalizedBase}${normalizedUrl}`
}

export interface SseFrame {
  eventName: string
  data?: unknown
  id?: string
  retryMs?: number
}

const DEFAULT_RETRY_MS = 1000
const MIN_RETRY_MS = 250
const MAX_RETRY_MS = 30000

export function parseSseBlock(block: string): SseFrame | null {
  const lines = block.split(/\r?\n/)
  let eventName = 'message'
  let id: string | undefined
  let retryMs: number | undefined
  const dataLines: string[] = []

  for (const line of lines) {
    if (line.startsWith('event:')) eventName = line.slice(6).trim()
    if (line.startsWith('id:')) id = line.slice(3).trim()
    if (line.startsWith('retry:')) {
      const parsed = Number(line.slice(6).trim())
      if (Number.isFinite(parsed) && parsed >= 0) {
        retryMs = Math.min(MAX_RETRY_MS, Math.max(MIN_RETRY_MS, parsed))
      }
    }
    if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
  }

  if (!dataLines.length && id === undefined && retryMs === undefined) return null
  if (!dataLines.length) return { eventName, id, retryMs }

  const raw = dataLines.join('\n')
  try {
    return { eventName, data: JSON.parse(raw), id, retryMs }
  } catch {
    return { eventName, data: raw, id, retryMs }
  }
}

export function useSse(): UseSseReturn {
  const isConnected = ref(false)
  const isReconnecting = ref(false)
  const lastEventId = ref('')

  let url = ''
  let callbacks: SseCallbacks | undefined
  let controller: AbortController | null = null
  let retryDelayMs = DEFAULT_RETRY_MS
  let reconnectTimer: number | null = null
  let intentionallyClosed = false

  async function readStream(targetUrl: string, activeController: AbortController) {
    const token = getAuthToken()
    const response = await fetch(resolveUrl(targetUrl), {
      method: 'GET',
      headers: {
        Accept: 'text/event-stream',
        ...(lastEventId.value ? { 'Last-Event-ID': lastEventId.value } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {})
      },
      credentials: 'include',
      signal: activeController.signal
    })

    if (!response.ok || !response.body) {
      throw new Error(`SSE connection failed: ${response.status}`)
    }

    isConnected.value = true
    callbacks?.onOpen?.()

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (!activeController.signal.aborted) {
      const { value, done } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const blocks = buffer.split(/\r?\n\r?\n/)
      buffer = blocks.pop() ?? ''

      for (const block of blocks) {
        const event = parseSseBlock(block)
        if (!event) continue
        if (event.id !== undefined) lastEventId.value = event.id
        if (event.retryMs !== undefined) retryDelayMs = event.retryMs
        if (event.data !== undefined) callbacks?.onMessage?.(event.eventName, event.data)
      }
    }

    // A clean HTTP/SSE EOF is not a fetch error. Task streams normally close
    // after a terminal event, but a proxy/browser can lose that final frame.
    // Surface the close so callers can reconcile persisted task events.
    if (controller === activeController && !activeController.signal.aborted && !intentionallyClosed) {
      isConnected.value = false
      callbacks?.onClose?.()
    }
  }

  function startConnection(preserveCursor: boolean) {
    controller?.abort()
    if (!preserveCursor) {
      lastEventId.value = ''
      retryDelayMs = DEFAULT_RETRY_MS
    }
    const activeController = new AbortController()
    controller = activeController

    void readStream(url, activeController).catch((error: Error) => {
      if (controller === activeController && !activeController.signal.aborted && !intentionallyClosed) {
        isConnected.value = false
        callbacks?.onError?.(error)
      }
    })
  }

  function connect(nextUrl: string, nextCallbacks?: SseCallbacks, initialCursor?: string) {
    intentionallyClosed = false
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    url = nextUrl
    callbacks = nextCallbacks
    // Phase 2.9A.28: initialCursor 允许调用方在连接时指定起始游标
    // (来自 event-list 的最大 canonical_order)，使 Last-Event-ID header
    // 能正确携带断线前的最后位置。不传时清空游标(旧行为)。
    if (initialCursor) {
      lastEventId.value = initialCursor
      startConnection(true)
    } else {
      startConnection(false)
    }
  }

  function disconnect() {
    intentionallyClosed = true
    if (reconnectTimer !== null) {
      window.clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
    controller?.abort()
    controller = null
    isConnected.value = false
    isReconnecting.value = false
  }

  function reconnect() {
    if (!url || intentionallyClosed) return
    const currentUrl = url
    const currentCallbacks = callbacks
    isReconnecting.value = true
    controller?.abort()
    controller = null
    isConnected.value = false
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null
      isReconnecting.value = false
      url = currentUrl
      callbacks = currentCallbacks
      intentionallyClosed = false
      startConnection(true)
    }, retryDelayMs)
  }

  return {
    isConnected,
    isReconnecting,
    lastEventId,
    connect,
    disconnect,
    reconnect
  }
}
