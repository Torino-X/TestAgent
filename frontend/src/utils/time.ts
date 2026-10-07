/**
 * Phase 2.9A.26+: Server-time parsing helpers.
 *
 * Backend now serializes every datetime as ISO-8601 with explicit UTC
 * suffix (e.g. "2026-07-29T08:11:23.456789Z").  Older fields without
 * the suffix are still accepted; we treat them as naive UTC, which
 * matches what the Python `datetime.isoformat()` call emitted before
 * this phase.
 */

export type ServerTime = string

/**
 * Phase 2.9A.35: 统一时间解析入口 — RFC3339 UTC 或无时区字符串一律按 UTC。
 * 无时区字符串绝不交给浏览器按本地时区解释(避免 +8h 漂移)。
 */
export { parseApiDateTime } from './eventPayload'

/** Parse a server-time string into a JS Date. */
export function parseServerTime(value: string | null | undefined): Date {
  if (!value) return new Date(NaN)
  // Normalize: replace naive ISO strings with explicit UTC.
  // The backend now emits "...Z" suffix; older payloads may omit it.
  let raw = value.trim()
  if (raw && !raw.endsWith('Z') && !/[+-]\d{2}:?\d{2}$/.test(raw)) {
    raw = `${raw}Z`
  }
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) {
    // Last-resort fallback: parse as naive (treat as local) — never
    // silently return an invalid Date.  The frontend sort path then
    // falls back to ``conversation_sequence`` which is the canonical
    // order signal anyway.
    return new Date(value)
  }
  return d
}

/**
 * Format a server-time string for display.
 * Locale-fixed to zh-CN to match the project's display convention.
 */
export function formatDisplayTime(value: string | null | undefined): string {
  const d = parseServerTime(value)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit'
  })
}