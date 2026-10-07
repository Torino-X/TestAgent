/**
 * Markdown 复制工具 — 保留 Markdown 原文,转剪贴板。
 *
 * 复用规范:
 * - 优先 navigator.clipboard.writeText(HTTPS / localhost 可用)
 * - 不可用或失败时,fallback 到 textarea + execCommand('copy')
 * - 全部失败时返回 false,调用方走 Toast 提示
 */

export type CopyResult = 'success' | 'fallback_success' | 'failed' | 'empty'

export async function copyMarkdownText(text: string): Promise<CopyResult> {
  const payload = text ?? ''
  if (!payload) return 'empty'

  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(payload)
      return 'success'
    } catch {
      // 沙箱或非安全上下文下会 throw;继续尝试 fallback
    }
  }

  if (typeof document === 'undefined') return 'failed'
  return legacyCopy(payload)
}

function legacyCopy(text: string): CopyResult {
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '0'
  textarea.style.opacity = '0'
  textarea.style.pointerEvents = 'none'
  document.body.appendChild(textarea)
  textarea.select()
  textarea.setSelectionRange(0, text.length)
  let succeeded = false
  try {
    succeeded = document.execCommand('copy')
  } catch {
    succeeded = false
  }
  document.body.removeChild(textarea)
  return succeeded ? 'fallback_success' : 'failed'
}
